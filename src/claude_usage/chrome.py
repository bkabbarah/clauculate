"""Native window chrome that Tk does not expose.

Tk draws its own content but Windows draws the title bar, so a dark app gets a
white caption strip with Tk's default feather icon. Both are fixable without
giving up the native frame.
"""

from __future__ import annotations

import ctypes
import sys

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


def use_dark_title_bar(window) -> bool:
    """Ask DWM to draw this window's title bar dark. Windows 10 1809+."""
    if not sys.platform.startswith("win"):
        return False
    try:
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        value = ctypes.c_int(1)
        for attribute in (
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
        ):
            result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            )
            if result == 0:
                return True
    except (AttributeError, OSError):
        pass
    return False
