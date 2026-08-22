"""Read-only access to a Claude Code profile's OAuth credential store.

Verified schema (Claude Code 2.1.239, Windows):

    {"claudeAiOauth": {
        "accessToken": str,
        "refreshToken": str,
        "expiresAt": int,               # epoch milliseconds
        "refreshTokenExpiresAt": int,   # epoch milliseconds
        "scopes": [str, ...],
        "subscriptionType": str,        # e.g. "max"
        "rateLimitTier": str            # e.g. "default_claude_max_20x"
    }}

Token values never leave this module except as the Bearer header value.
They are never logged, never persisted, never rendered.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


class CredentialError(Exception):
    """Credential store missing, malformed, or expired."""


@dataclass(frozen=True)
class Credentials:
    access_token: str
    expires_at_ms: int | None
    subscription_type: str | None
    rate_limit_tier: str | None

    @property
    def is_expired(self) -> bool:
        if not self.expires_at_ms:
            return False
        return time.time() * 1000 >= self.expires_at_ms

    def __repr__(self) -> str:  # never leak the token via repr/traceback
        return (
            f"Credentials(access_token=<redacted len={len(self.access_token)}>, "
            f"expires_at_ms={self.expires_at_ms}, "
            f"subscription_type={self.subscription_type!r})"
        )


# macOS keeps Claude Code's OAuth token in the login Keychain rather than in a
# file. Evidence: the installed binary contains
# "security find-generic-password -s anthropic-api -w" plus KeychainPrefetch /
# KeychainPrefetchCompleted / KeychainAsync telemetry names.
#
# The exact service name is NOT confirmed, so every plausible one is tried and
# the first that yields a parseable credential object wins. Reading the
# Keychain is a read; nothing here writes.
_KEYCHAIN_SERVICES = (
    "Claude Code-credentials",
    "Claude Code",
    "anthropic-api",
)


def _read_macos_keychain() -> dict | None:
    """Ask the login Keychain for the credential blob. Returns None if absent.

    Untested: no macOS machine was available. Written defensively so a wrong
    guess degrades to "no credentials found" rather than an exception.
    """
    if sys.platform != "darwin":
        return None
    for service in _KEYCHAIN_SERVICES:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            data = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "claudeAiOauth" in data:
            return data
    return None


def read_credentials(config_dir: Path) -> Credentials:
    """Open the credential file read-only. This never creates or mutates it."""
    path = Path(config_dir) / ".credentials.json"

    data = None
    if not path.exists():
        data = _read_macos_keychain()
        if data is None:
            raise CredentialError(
                f"no .credentials.json in {config_dir} - "
                f'run `claude auth login` with CLAUDE_CONFIG_DIR="{config_dir}"'
            )

    try:
        if data is None:
            # Explicit read-binary. readonly_guard rejects any write mode here.
            with open(path, "rb") as fh:
                data = json.loads(fh.read().decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CredentialError(f"credential store is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise CredentialError(f"cannot read credential store: {exc}") from exc

    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise CredentialError(
            "credential store has no 'claudeAiOauth' object - "
            "this profile may use an API key rather than a Claude subscription"
        )

    token = oauth.get("accessToken")
    if not token or not isinstance(token, str):
        raise CredentialError("credential store has no usable accessToken")

    expires_at = oauth.get("expiresAt")
    creds = Credentials(
        access_token=token,
        expires_at_ms=int(expires_at) if isinstance(expires_at, (int, float)) else None,
        subscription_type=oauth.get("subscriptionType"),
        rate_limit_tier=oauth.get("rateLimitTier"),
    )

    if creds.is_expired:
        raise CredentialError(
            f're-auth needed: run `claude` with CLAUDE_CONFIG_DIR="{config_dir}"'
        )
    return creds
