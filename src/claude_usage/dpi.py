"""Per-monitor DPI awareness.

Without this, Windows bitmap-stretches the Tk window on any display scaled
above 100%, which makes text blurry and makes screen coordinates disagree with
widget coordinates.
"""

from __future__ import annotations

import sys


def enable_dpi_awareness() -> bool:
    """Opt into per-monitor DPI. Safe to call more than once. Windows-only."""
    if not sys.platform.startswith("win"):
        return False
    import ctypes

    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Windows 8.1+)
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except (AttributeError, OSError):
        return False
