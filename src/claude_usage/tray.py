"""System tray icon.

The icon colour reflects the WORST utilization across all accounts, so a single
glance answers "is any account in trouble".
"""

from __future__ import annotations

import threading

import pystray
from PIL import Image, ImageDraw

from . import clawd
from .formatting import color_for, format_percent
from .poller import Poller

ICON_SIZE = 64

# Shell_NotifyIcon caps the tooltip at 128 characters. Past that Windows
# silently truncates, so we truncate deliberately and say we did.
TOOLTIP_LIMIT = 127


RING_WIDTH = 7          # at ICON_SIZE; Windows downscales to 16/24/32
RING_TRACK = "#3a3a3a"


def _render_icon(worst: float | None) -> Image.Image:
    """A ring gauge around Clawd.

    The arc reads as a dial at a glance and survives downscaling better than a
    number, which is illegible below 32px. Clawd keeps his coral so the icon
    stays recognisable as this app; the arc alone carries severity.
    """
    image = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # No margin: the ring reaches the bitmap edge so the icon occupies as
    # much of the tray slot as Windows will give it. The half-pixel inset is
    # only what the stroke needs to avoid being clipped.
    # PIL strokes inward from the outline, so a zero inset still draws the
    # full ring width and the bitmap is used edge to edge.
    box = (0, 0, ICON_SIZE - 1, ICON_SIZE - 1)
    draw.ellipse(box, outline=RING_TRACK, width=RING_WIDTH)

    if worst is not None:
        sweep = 360.0 * max(0.0, min(100.0, float(worst))) / 100.0
        if sweep > 0:
            # Start at 12 o'clock and run clockwise.
            draw.arc(box, start=-90, end=-90 + sweep,
                     fill=color_for(worst), width=RING_WIDTH)

    _draw_clawd(image, cell=3)
    return image


def _draw_clawd(image: Image.Image, cell: int) -> None:
    """Paint the sprite centred inside the ring."""
    draw = ImageDraw.Draw(image)
    width = clawd.BODY_W * cell
    height = clawd.BODY_H * cell
    ox = (ICON_SIZE - width) // 2
    oy = (ICON_SIZE - height) // 2 + cell   # room above for the visor

    for r in range(clawd.BODY_H):
        for c in range(clawd.BODY_W):
            if not clawd.CLAWD_BODY[r][c]:
                continue
            x, y = ox + c * cell, oy + r * cell
            draw.rectangle([x, y, x + cell - 1, y + cell - 1],
                           fill=clawd.BODY_COLOR)
    for (ex, ey) in (clawd.EYE_LEFT, clawd.EYE_RIGHT):
        x, y = ox + ex * cell, oy + ey * cell
        draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=clawd.EYE_COLOR)

    # The visor is what makes this Clauculate's mark rather than the mascot.
    for row_offset, columns, colour in clawd.VISOR_ROWS:
        for column in columns:
            x, y = ox + column * cell, oy + row_offset * cell
            draw.rectangle([x, y, x + cell - 1, y + cell - 1], fill=colour)


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
