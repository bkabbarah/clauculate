"""Read-only polling of the OAuth usage endpoint.

Rules this module exists to enforce:
  * never faster than MIN_INTERVAL (180s) per account
  * polls staggered across accounts so they never burst together
  * per-account exponential backoff on 429: 3 -> 6 -> 12 -> capped at 15 min
  * every outcome logged
  * no inference calls, no token refresh, no writes to any Claude config dir
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import Account
from .credentials import CredentialError, read_credentials
from .model import UsageSnapshot, parse_usage

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"

MIN_INTERVAL = 180.0
BACKOFF_STEPS = (180.0, 360.0, 720.0, 900.0)  # 3, 6, 12, cap 15 minutes

# Used only if the installed version cannot be detected. A wrong User-Agent
# provokes aggressive rate limiting, so detection is strongly preferred.
FALLBACK_VERSION = "2.1.239"

_VERSION_CANDIDATES = (
    Path.home() / "AppData/Roaming/npm/node_modules/@anthropic-ai/claude-code/package.json",
    Path.home() / ".npm-global/lib/node_modules/@anthropic-ai/claude-code/package.json",
    Path("/usr/lib/node_modules/@anthropic-ai/claude-code/package.json"),
    Path("/usr/local/lib/node_modules/@anthropic-ai/claude-code/package.json"),
)


def claude_code_version() -> tuple[str, bool]:
    """Read the installed Claude Code version from disk. Never executes it.

    Returns (version, detected).
    """
    for candidate in _VERSION_CANDIDATES:
        try:
            if candidate.exists():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                version = data.get("version")
                if version:
                    return str(version), True
        except (OSError, json.JSONDecodeError):
            continue
    return FALLBACK_VERSION, False


class ErrorKind:
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    SHAPE = "shape"
    HTTP = "http"


@dataclass
class AccountStatus:
    """Everything the UI needs to render one account honestly."""

    label: str
    config_dir: Path
    snapshot: UsageSnapshot | None = None
    raw_json: Any = None
    raw_text: str | None = None

    last_attempt: float | None = None
    last_success: float | None = None

    error: str | None = None
    error_kind: str | None = None
    http_status: int | None = None

    backoff_until: float | None = None
    backoff_index: int = 0

    subscription_type: str | None = None
    rate_limit_tier: str | None = None

    @property
    def is_stale(self) -> bool:
        if self.last_success is None:
            return True
        return (time.time() - self.last_success) > (MIN_INTERVAL * 2)

    @property
    def age_seconds(self) -> float | None:
        if self.last_success is None:
            return None
        return time.time() - self.last_success

    @property
    def backoff_remaining(self) -> float:
        if self.backoff_until is None:
            return 0.0
        return max(0.0, self.backoff_until - time.time())


class Poller:
    def __init__(
        self,
        accounts: list[Account],
        store: Any = None,
        logger: Any = None,
        interval: float = MIN_INTERVAL,
    ):
        self.accounts = accounts
        self.store = store
        self.log = logger
        # A floor, not a preference. Never poll faster than this.
        self.interval = max(MIN_INTERVAL, float(interval))

        self._statuses: dict[str, AccountStatus] = {
            a.label: AccountStatus(label=a.label, config_dir=a.config_dir)
            for a in accounts
        }
        self._next_due: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_update = None

        version, detected = claude_code_version()
        self.user_agent = "claude-code/" + version
        if self.log and not detected:
            self.log.warning(
                "could not detect installed Claude Code version; "
                "falling back to User-Agent %s", self.user_agent
            )

        # Stagger the first poll of each account across the interval so N
        # accounts never fire simultaneously.
        now = time.time()
        spacing = self.interval / max(1, len(accounts))
        for i, account in enumerate(accounts):
            self._next_due[account.label] = now + (i * spacing)

    # ---------------------------------------------------------------- public

    def set_update_callback(self, callback) -> None:
        self._on_update = callback

    def statuses(self) -> dict[str, AccountStatus]:
        with self._lock:
            return dict(self._statuses)

    def status(self, label: str) -> AccountStatus | None:
        with self._lock:
            return self._statuses.get(label)

    def worst_utilization(self) -> float | None:
        values = []
        for status in self.statuses().values():
            if status.snapshot is not None:
                worst = status.snapshot.worst_utilization
                if worst is not None:
                    values.append(worst)
        return max(values) if values else None

    def replace_accounts(self, accounts: list[Account]) -> None:
        """Swap the monitored set after accounts.json changes.

        Existing statuses are carried over so an account already polled does
        not lose its data -- or its place in the TTL schedule -- just because
        an unrelated account was added.
        """
        now = time.time()
        spacing = self.interval / max(1, len(accounts))
        with self._lock:
            statuses: dict[str, AccountStatus] = {}
            for i, account in enumerate(accounts):
                previous = self._statuses.get(account.label)
                if previous is not None and previous.config_dir == account.config_dir:
                    statuses[account.label] = previous
                else:
                    statuses[account.label] = AccountStatus(
                        label=account.label, config_dir=account.config_dir
                    )
                    self._next_due[account.label] = now + (i * spacing)
            self._statuses = statuses
            self.accounts = accounts
            for label in list(self._next_due):
                if label not in statuses:
                    del self._next_due[label]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def request_refresh(self, label: str | None = None) -> None:
        """Ask for a poll on the next tick, still respecting TTL and backoff."""
        now = time.time()
        with self._lock:
            targets = [label] if label else list(self._next_due)
            for target in targets:
                status = self._statuses.get(target)
                if status is None:
                    continue
                earliest = now
                if status.last_success:
                    earliest = max(earliest, status.last_success + self.interval)
                if status.backoff_until:
                    earliest = max(earliest, status.backoff_until)
                self._next_due[target] = earliest

    # --------------------------------------------------------------- internal

    def _run(self) -> None:
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            while not self._stop.is_set():
                now = time.time()
                for account in self.accounts:
                    if self._stop.is_set():
                        break
                    due = self._next_due.get(account.label, 0.0)
                    if now >= due:
                        self._poll_account(client, account)
                        if self._on_update:
                            try:
                                self._on_update()
                            except Exception:  # UI must never kill the poller
                                if self.log:
                                    self.log.exception("update callback failed")
                self._stop.wait(5.0)

    def _schedule_next(self, label: str, seconds: float) -> None:
        self._next_due[label] = time.time() + seconds

    def _poll_account(self, client: httpx.Client, account: Account) -> None:
        label = account.label
        status = self._statuses[label]
        status.last_attempt = time.time()

        # 1. Read this profile's token. Read-only, never refreshed by us.
        try:
            creds = read_credentials(account.config_dir)
        except CredentialError as exc:
            self._fail(status, ErrorKind.AUTH, str(exc))
            self._schedule_next(label, self.interval)
            if self.log:
                self.log.warning("%s: credential error: %s", label, exc)
            return

        status.subscription_type = creds.subscription_type
        status.rate_limit_tier = creds.rate_limit_tier

        headers = {
            "Authorization": "Bearer " + creds.access_token,
            "User-Agent": self.user_agent,
            "anthropic-beta": OAUTH_BETA,
            "Accept": "application/json",
        }

        # 2. One GET. No retries here -- retrying is what gets you rate limited.
        try:
            response = client.get(USAGE_URL, headers=headers)
        except httpx.HTTPError as exc:
            self._fail(status, ErrorKind.NETWORK, "network error: " + str(exc))
            self._schedule_next(label, self.interval)
            if self.log:
                self.log.warning("%s: network error: %s", label, exc)
            return
        finally:
            del headers, creds  # drop the token reference promptly

        status.http_status = response.status_code

        if response.status_code == 429:
            self._enter_backoff(status, response)
            return

        if response.status_code in (401, 403):
            self._fail(
                status,
                ErrorKind.AUTH,
                'HTTP %d - re-auth: run `claude` with CLAUDE_CONFIG_DIR="%s"'
                % (response.status_code, account.config_dir),
            )
            self._schedule_next(label, self.interval)
            if self.log:
                self.log.warning("%s: HTTP %d unauthorized", label, response.status_code)
            return

        if response.status_code >= 400:
            self._fail(
                status,
                ErrorKind.HTTP,
                "HTTP %d: %s" % (response.status_code, response.text[:200]),
            )
            self._schedule_next(label, self.interval)
            if self.log:
                self.log.warning("%s: HTTP %d", label, response.status_code)
            return

        # 3. Success path. Unexpected JSON must degrade, never crash.
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            status.raw_text = response.text
            status.raw_json = None
            self._fail(
                status, ErrorKind.SHAPE, "response was not JSON: " + str(exc)
            )
            self._schedule_next(label, self.interval)
            if self.log:
                self.log.error("%s: non-JSON response", label)
            return

        try:
            snapshot = parse_usage(payload, datetime.now(timezone.utc))
        except Exception as exc:  # parser bug must not take the app down
            status.raw_json = payload
            status.raw_text = json.dumps(payload, indent=2)
            self._fail(status, ErrorKind.SHAPE, "could not interpret response: " + str(exc))
            self._schedule_next(label, self.interval)
            if self.log:
                self.log.exception("%s: parse failure", label)
            return

        status.snapshot = snapshot
        status.raw_json = payload
        status.raw_text = json.dumps(payload, indent=2, sort_keys=False)
        status.last_success = time.time()
        status.error = None
        status.error_kind = None
        status.backoff_until = None
        status.backoff_index = 0

        if self.store is not None:
            try:
                self.store.record(label, snapshot)
            except Exception:
                if self.log:
                    self.log.exception("%s: history write failed", label)

        self._schedule_next(label, self.interval)
        if self.log:
            self.log.info(
                "%s: ok worst=%s windows=%d limits=%d",
                label,
                snapshot.worst_utilization,
                len(snapshot.windows),
                len(snapshot.limits),
            )

    def _enter_backoff(self, status: AccountStatus, response: httpx.Response) -> None:
        index = min(status.backoff_index, len(BACKOFF_STEPS) - 1)
        delay = BACKOFF_STEPS[index]

        # Honour Retry-After when the server sends a longer one.
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass

        status.backoff_index = min(status.backoff_index + 1, len(BACKOFF_STEPS) - 1)
        status.backoff_until = time.time() + delay
        status.error_kind = ErrorKind.RATE_LIMIT
        status.error = "rate limited (HTTP 429)"
        self._next_due[status.label] = status.backoff_until

        if self.log:
            self.log.warning(
                "%s: HTTP 429, backing off %.0fs (step %d)",
                status.label, delay, status.backoff_index,
            )

    @staticmethod
    def _fail(status: AccountStatus, kind: str, message: str) -> None:
        status.error_kind = kind
        status.error = message
