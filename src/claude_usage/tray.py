"""System tray icon.

The icon colour reflects the WORST utilization across all accounts, so a single
glance answers "is any account in trouble".
"""

from __future__ import annotations

import threading

import pystray
from PIL import Image, ImageDraw

from .formatting import color_for, format_percent
from .poller import Poller

ICON_SIZE = 64

# Shell_NotifyIcon caps the tooltip at 128 characters. Past that Windows
# silently truncates, so we truncate deliberately and say we did.
TOOLTIP_LIMIT = 127


def _render_icon(worst: float | None) -> Image.Image:
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, ICON_SIZE - 3, ICON_SIZE - 3), fill=color_for(worst))

    if worst is not None:
        text = str(int(round(worst)))
        # Centre the number without depending on a bundled font file.
        try:
            box = draw.textbbox((0, 0), text)
            tw, th = box[2] - box[0], box[3] - box[1]
        except Exception:
            tw, th = 0, 0
        draw.text(
            ((ICON_SIZE - tw) / 2, (ICON_SIZE - th) / 2 - 1),
            text, fill="#ffffff",
        )
    return image


def _shorten(line: str, label_width: int) -> str:
    """Trim the label side of "label: value", never the numbers."""
    label, _, rest = line.partition(": ")
    if not rest or len(label) <= label_width:
        return line
    return label[: label_width - 1] + "…: " + rest


def _fit_tooltip(lines: list[str]) -> str:
    """Fit every account into the Windows tooltip budget.

    Long labels are shortened before any account is dropped, because losing a
    whole account silently is far worse than losing a few characters of its
    name. Only if shortening is not enough does it fall back to "+N more",
    which at least says that something was left out.
    """
    if not lines:
        return "no accounts configured"

    for width in (64, 22, 18, 14, 11, 8):
        candidate = "\n".join(_shorten(line, width) for line in lines)
        if len(candidate) <= TOOLTIP_LIMIT:
            return candidate

    # Still too long: keep as many as fit and say how many were dropped.
    kept: list[str] = []
    for line in (_shorten(x, 8) for x in lines):
        remaining = len(lines) - len(kept) - 1
        suffix = "\n+%d more" % remaining if remaining > 0 else ""
        trial = "\n".join(kept + [line]) + suffix
        if len(trial) > TOOLTIP_LIMIT:
            break
        kept.append(line)
    dropped = len(lines) - len(kept)
    if not kept:
        return "%d accounts (tooltip too small)" % len(lines)
    return "\n".join(kept) + ("\n+%d more" % dropped if dropped else "")


class Tray:
    def __init__(self, poller: Poller, on_open, on_quit, on_refresh):
        self.poller = poller
        self._on_open = on_open
        self._on_quit = on_quit
        self._on_refresh = on_refresh
        self._thread: threading.Thread | None = None

        self.icon = pystray.Icon(
            "clauculate",
            _render_icon(None),
            "Clauculate - waiting for first poll",
            menu=pystray.Menu(
                pystray.MenuItem("Open panel", self._open, default=True),
                pystray.MenuItem("Refresh now", self._refresh),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            ),
        )

    # pystray invokes these on its own thread; hand off to the Tk thread.
    def _open(self, *_args):
        self._on_open()

    def _refresh(self, *_args):
        self._on_refresh()

    def _quit(self, *_args):
        self._on_quit()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.icon.run, name="tray", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass

    def update(self) -> None:
        """Recolour the icon and rebuild the tooltip from current state."""
        statuses = self.poller.statuses()
        worst = self.poller.worst_utilization()

        lines: list[str] = []
        for account in self.poller.accounts:
            status = statuses.get(account.label)
            if status is None:
                continue
            if status.error_kind == "rate_limit":
                lines.append("%s: 429 backoff" % account.label)
                continue
            if status.snapshot is None:
                lines.append("%s: %s" % (account.label, status.error_kind or "no data"))
                continue
            five = status.snapshot.window("five_hour")
            week = status.snapshot.window("seven_day")
            marker = "!" if status.is_stale else ""
            lines.append(
                "%s: %s / %s%s"
                % (
                    account.label,
                    format_percent(five.utilization if five else None),
                    format_percent(week.utilization if week else None),
                    marker,
                )
            )

        tooltip = _fit_tooltip(lines)

        try:
            self.icon.icon = _render_icon(worst)
            self.icon.title = tooltip
        except Exception:
            # Never let a tray hiccup take down polling.
            pass
