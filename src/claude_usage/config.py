"""accounts.json loading. Contains labels and config dirs. Never tokens."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Account:
    label: str
    config_dir: Path

    @property
    def credentials_path(self) -> Path:
        return self.config_dir / ".credentials.json"


def _expand(raw: str) -> Path:
    return Path(os.path.expandvars(str(raw))).expanduser()


def load_accounts(path: Path) -> list[Account]:
    if not path.exists():
        raise ConfigError(
            f"accounts.json not found at {path}. "
            "Copy accounts.json.example and edit it."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"accounts.json is not valid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise ConfigError("accounts.json must be a JSON array of objects.")

    accounts: list[Account] = []
    seen_labels: set[str] = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ConfigError(f"accounts.json entry {i} is not an object.")
        label = entry.get("label")
        config_dir = entry.get("config_dir")
        if not label or not config_dir:
            raise ConfigError(
                f"accounts.json entry {i} needs both 'label' and 'config_dir'."
            )
        for forbidden in ("token", "access_token", "accessToken"):
            if forbidden in entry:
                raise ConfigError(
                    f"accounts.json entry {i} contains '{forbidden}'. "
                    "This app reads tokens from each profile at poll time; "
                    "never store them in config."
                )
        if label in seen_labels:
            raise ConfigError(f"accounts.json has duplicate label {label!r}.")
        seen_labels.add(label)
        accounts.append(Account(label=str(label), config_dir=_expand(config_dir)))

    if not accounts:
        raise ConfigError("accounts.json is empty.")
    return accounts
