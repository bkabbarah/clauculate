"""Runtime enforcement of the read-only guarantee.

This app reads Claude Code credential stores. It must never write to them.
Rather than relying on code review alone, we intercept the two syscall entry
points Python uses to open files and hard-fail on any write that resolves
inside a protected root.

Install this before anything else touches the filesystem.
"""

from __future__ import annotations

import builtins
import os
from pathlib import Path

_ORIG_OPEN = builtins.open
_ORIG_OS_OPEN = os.open

_protected_roots: list[Path] = []
_violations: list[str] = []
_installed = False

# Any of these in a mode string means the handle can mutate the file.
_WRITE_MODE_CHARS = frozenset("wxa+")

_WRITE_FLAGS = (
    os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
)


class ReadOnlyViolation(RuntimeError):
    """Raised when something attempts to write inside a protected root."""


def _resolve(path) -> Path | None:
    try:
        return Path(os.fspath(path)).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        # File descriptors and exotic objects are not paths we can protect.
        return None


def _is_protected(path) -> bool:
    target = _resolve(path)
    if target is None:
        return False
    for root in _protected_roots:
        if target == root or root in target.parents:
            return True
    return False


def _deny(path, how: str):
    msg = f"read-only guard blocked {how} on protected path: {path}"
    _violations.append(msg)
    raise ReadOnlyViolation(msg)


def _guarded_open(file, mode="r", *args, **kwargs):
    if _WRITE_MODE_CHARS & set(str(mode)) and _is_protected(file):
        _deny(file, f"open(mode={mode!r})")
    return _ORIG_OPEN(file, mode, *args, **kwargs)


def _guarded_os_open(path, flags, *args, **kwargs):
    if (flags & _WRITE_FLAGS) and _is_protected(path):
        _deny(path, f"os.open(flags={flags})")
    return _ORIG_OS_OPEN(path, flags, *args, **kwargs)


def protect(*roots) -> None:
    """Mark directories as never-writable for the lifetime of the process."""
    for root in roots:
        resolved = _resolve(root)
        if resolved is not None and resolved not in _protected_roots:
            _protected_roots.append(resolved)


def install() -> None:
    global _installed
    if _installed:
        return
    builtins.open = _guarded_open
    os.open = _guarded_os_open
    _installed = True


def protected_roots() -> list[str]:
    return [str(p) for p in _protected_roots]


def violations() -> list[str]:
    return list(_violations)
