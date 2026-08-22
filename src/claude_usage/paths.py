"""Where the app reads from and where it is allowed to write."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Clauculate"


def app_data_dir() -> Path:
    """The ONLY tree this app writes to. Never a Claude config dir."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / APP_NAME


def db_path() -> Path:
    return app_data_dir() / "history.sqlite3"


def log_path() -> Path:
    return app_data_dir() / "monitor.log"


def default_accounts_path() -> Path:
    """Find accounts.json, preferring the copy next to the app.

    When frozen, __file__ lives inside PyInstaller's extraction directory, so
    the source-tree path is meaningless and the exe's own folder is what the
    user actually sees. Checked in order; the app data dir is the fallback and
    is also where a missing file is reported against.
    """
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "accounts.json")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "accounts.json")
    candidates.append(Path.cwd() / "accounts.json")

    fallback = app_data_dir() / "accounts.json"
    candidates.append(fallback)

    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return fallback
