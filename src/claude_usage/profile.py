"""Account identity and profile discovery.

Two problems this solves:

1. A profile *directory* is not an account. The same account can be logged in
   to several directories, which would double-count it and waste rate limit on
   duplicate polls. account.uuid from /api/oauth/profile is the real identity.

2. Identity must be obtained without invoking the `claude` CLI. Running
   `claude auth status` would report the email, but the CLI writes a
   .claude.json into the config dir as a side effect, which would break the
   read-only guarantee. So we read the token from disk and ask the API.

Like the usage endpoint, /api/oauth/profile is undocumented.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .credentials import CredentialError, read_credentials

PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
OAUTH_BETA = "oauth-2025-04-20"


@dataclass
class AccountProfile:
    uuid: str | None
    email: str | None
    display_name: str | None
    rate_limit_tier: str | None
    has_max: bool
    raw: dict[str, Any]

    @property
    def identity(self) -> str:
        """Stable dedup key. Falls back to email, then to nothing."""
        return self.uuid or self.email or ""

    @property
    def local_part(self) -> str:
        if not self.email:
            return self.display_name or "unknown"
        return self.email.split("@", 1)[0]

    @property
    def domain(self) -> str:
        if not self.email or "@" not in self.email:
            return ""
        return self.email.split("@", 1)[1].split(".", 1)[0]


@dataclass
class DiscoveredProfile:
    """A candidate profile directory found on disk."""

    config_dir: Path
    has_credentials: bool
    error: str | None = None
    profile: AccountProfile | None = None
    enrolled_as: str | None = None      # label in accounts.json, if any
    duplicate_of: str | None = None     # another dir with the same identity

    @property
    def name(self) -> str:
        return self.config_dir.name

    @property
    def status_text(self) -> str:
        if self.error:
            return self.error
        if self.profile and self.profile.email:
            return self.profile.email
        return "unknown account"


def fetch_profile(
    config_dir: Path, user_agent: str, client: httpx.Client | None = None
) -> AccountProfile:
    """Read the profile's token and ask the API who it belongs to."""
    creds = read_credentials(config_dir)  # raises CredentialError
    headers = {
        "Authorization": "Bearer " + creds.access_token,
        "User-Agent": user_agent,
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
    }
    owned = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        response = client.get(PROFILE_URL, headers=headers)
    finally:
        del headers, creds
        if owned:
            client.close()

    if response.status_code in (401, 403):
        raise CredentialError("token rejected (HTTP %d)" % response.status_code)
    if response.status_code == 429:
        raise CredentialError("rate limited while identifying this profile")
    if response.status_code >= 400:
        raise CredentialError("HTTP %d from profile endpoint" % response.status_code)

    data = response.json()
    account = data.get("account") or {}
    org = data.get("organization") or {}
    return AccountProfile(
        uuid=account.get("uuid"),
        email=account.get("email"),
        display_name=account.get("display_name") or account.get("full_name"),
        rate_limit_tier=org.get("rate_limit_tier"),
        has_max=bool(account.get("has_claude_max")),
        raw=data,
    )


def scan_profile_dirs(home: Path | None = None) -> list[Path]:
    """Every ~/.claude* directory that actually holds a credential store."""
    home = home or Path.home()
    found = []
    for path in sorted(home.glob(".claude*")):
        if path.is_dir() and (path / ".credentials.json").exists():
            found.append(path)
    return found


def discover(
    user_agent: str,
    enrolled: dict[str, str] | None = None,
    home: Path | None = None,
    identify: bool = True,
) -> list[DiscoveredProfile]:
    """Find profiles, identify them, and flag duplicates.

    `enrolled` maps resolved config_dir string -> label from accounts.json.
    Set identify=False to skip all network calls.
    """
    enrolled = enrolled or {}
    results: list[DiscoveredProfile] = []
    seen_identity: dict[str, str] = {}

    client = httpx.Client(timeout=20.0) if identify else None
    try:
        for config_dir in scan_profile_dirs(home):
            entry = DiscoveredProfile(
                config_dir=config_dir,
                has_credentials=True,
                enrolled_as=enrolled.get(str(config_dir.resolve())),
            )
            if identify:
                try:
                    entry.profile = fetch_profile(config_dir, user_agent, client)
                except CredentialError as exc:
                    entry.error = str(exc)
                except httpx.HTTPError as exc:
                    entry.error = "network error: %s" % exc
                except Exception as exc:  # never let one bad profile stop the scan
                    entry.error = "%s: %s" % (type(exc).__name__, exc)

            if entry.profile and entry.profile.identity:
                key = entry.profile.identity
                if key in seen_identity:
                    entry.duplicate_of = seen_identity[key]
                else:
                    seen_identity[key] = entry.name
            results.append(entry)
    finally:
        if client is not None:
            client.close()

    return results


def suggest_labels(entries: list[DiscoveredProfile]) -> dict[str, str]:
    """Email local-part labels, disambiguated by domain only on collision."""
    # Collisions are counted across distinct ACCOUNTS, not directories. Three
    # directories holding one account is not a name clash and must not force
    # the domain suffix onto an otherwise unambiguous label.
    identities: dict[str, list[DiscoveredProfile]] = {}
    for entry in entries:
        if entry.profile is None:
            continue
        identities.setdefault(entry.profile.identity, []).append(entry)

    by_local: dict[str, list[str]] = {}
    for identity, group in identities.items():
        by_local.setdefault(group[0].profile.local_part, []).append(identity)

    labels: dict[str, str] = {}
    for identity, group in identities.items():
        profile = group[0].profile
        local = profile.local_part
        ambiguous = len(by_local.get(local, [])) > 1
        label = "%s@%s" % (local, profile.domain) if ambiguous and profile.domain else local
        for entry in group:
            labels[str(entry.config_dir)] = label

    # Anything unidentified falls back to the directory name.
    for entry in entries:
        labels.setdefault(str(entry.config_dir), entry.name.lstrip("."))
    return labels
