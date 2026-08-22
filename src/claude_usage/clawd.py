"""Clawd, rendered on a Tk canvas.

Sprite data (body matrix, eye variants, anchors, body colour) is ported from
the clawd-animation skill's template.html. The skill draws to an HTML canvas
via px(gx, gy, colour); a Tk canvas rectangle per cell is the same model, so
the pixel art is preserved exactly rather than approximated.

Clawd is not decoration here: the pose encodes account state, so a glance at
the crab tells you the same thing the percentages do.
"""

from __future__ import annotations

import math

BODY_COLOR = "#CD6E58"
EYE_COLOR = "#000000"

# Clauculate's own mark: an accountant's eyeshade on the stock sprite. Two
# tones so it reads as a cap with a brim rather than a green stripe. Used for
# the window icon, the tray and the header only; panel tiles stay plain.
VISOR_BAND = "#14532a"
VISOR_BRIM = "#2e9e4f"

# (row offset from the body top, columns, colour)
VISOR_ROWS = (
    (-2, range(4, 10), VISOR_BAND),
    (-1, range(3, 11), VISOR_BRIM),
)

# 14x8 flat wide body. 1 = body cell.
CLAWD_BODY = [
    [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
    [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
    [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0],
    [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0],
    [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
    [0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0],
    [0, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0, 0],
    [0, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 0, 0],
]

BODY_W = 14
BODY_H = 8

EYE_LEFT = (4, 1)
EYE_RIGHT = (9, 1)

# dx/dy offsets from the eye anchors. "blink" hides both eyes.
EYES = {
    "forward": {"left": (0, 0), "right": (0, 0)},
    "look_right": {"left": (1, 0), "right": (1, 0)},
    "look_left": {"left": (-1, 0), "right": (-1, 0)},
    "look_down": {"left": (0, 1), "right": (0, 1)},
    "blink": {"hidden": True},
}

# 5x4 heart, used sparingly for a healthy account.
HEART = [
    [1, 0, 1, 0, 0],
    [1, 1, 1, 1, 0],
    [0, 1, 1, 1, 0],
    [0, 0, 1, 0, 0],
]

# Moods, chosen by account state rather than at random.
MOOD_FRESH = "fresh"          # < 25%   plenty left
MOOD_HEALTHY = "healthy"      # 25-50%  comfortable
MOOD_BUSY = "busy"            # 50-80%  working, watchful
MOOD_STRAINED = "strained"    # 80-95%  worried
MOOD_CRITICAL = "critical"    # >= 95%  nearly out
MOOD_SLEEPING = "sleeping"    # rate limited or stale
MOOD_SAD = "sad"              # auth / network / shape failure

# Shown next to the sprite so the pose is never a guessing game.
MOOD_CAPTIONS = {
    MOOD_FRESH: "plenty left",
    MOOD_HEALTHY: "comfortable",
    MOOD_BUSY: "working",
    MOOD_STRAINED: "running low",
    MOOD_CRITICAL: "nearly out",
    MOOD_SLEEPING: "waiting",
    MOOD_SAD: "check it",
}


def mood_for(utilization: float | None, error_kind: str | None, stale: bool) -> str:
    """Map account state onto a pose. Never random -- it has to mean something."""
    if error_kind == "rate_limit":
        return MOOD_SLEEPING
    if error_kind in ("auth", "http", "network", "shape"):
        return MOOD_SAD
    if stale or utilization is None:
        return MOOD_SLEEPING
    if utilization >= 95:
        return MOOD_CRITICAL
    if utilization > 80:
        return MOOD_STRAINED
    if utilization >= 50:
        return MOOD_BUSY
    if utilization >= 25:
        return MOOD_HEALTHY
    return MOOD_FRESH


def eye_for(mood: str, frame: int) -> str:
    """Pick the eye variant for this frame. Blinks are periodic, not random."""
    if mood == MOOD_SLEEPING:
        return "blink"
    if mood == MOOD_SAD:
        # A slow, heavy blink.
        return "blink" if (frame % 24) < 6 else "look_down"
    if mood == MOOD_CRITICAL:
        # Wide-eyed and darting: can't settle.
        return ("look_left", "forward", "look_right", "forward")[(frame // 2) % 4]
    if mood == MOOD_STRAINED:
        return "blink" if (frame % 40) == 0 else "look_down"

    # Healthy / busy: blink briefly every ~5s.
    if (frame % 40) in (0, 1):
        return "blink"
    if mood == MOOD_BUSY:
        # Glancing around, as if keeping an eye on things.
        phase = (frame // 10) % 4
        return {0: "forward", 1: "look_right", 2: "forward", 3: "look_left"}[phase]
    return "forward"


def bob_for(mood: str, frame: int) -> int:
    """Vertical bob in whole cells, so the pixel grid never breaks."""
    if mood in (MOOD_SLEEPING, MOOD_SAD):
        return 1 if (frame % 32) < 16 else 0
    if mood == MOOD_CRITICAL:
        return 1 if (frame % 4) < 2 else 0
    if mood == MOOD_STRAINED:
        # A faster, tighter jitter.
        return 1 if (frame % 6) < 3 else 0
    return 1 if math.sin(frame / 5.0) > 0 else 0


def sprite_size(cell: int) -> tuple[int, int]:
    """Canvas size needed for the body plus one cell of bob and accessories."""
    return (BODY_W * cell, (BODY_H + 6) * cell)


def draw_clawd(
    canvas,
    frame: int,
    mood: str,
    cell: int = 4,
    body_color: str = BODY_COLOR,
    tag: str = "clawd",
    visor: bool = False,
) -> None:
    """Repaint Clawd. Clears only its own tag, so other canvas items survive."""
    canvas.delete(tag)

    oy = bob_for(mood, frame) + 5  # leave headroom for accessories
    ox = 0

    def px(gx, gy, color):
        x0, y0 = gx * cell, gy * cell
        canvas.create_rectangle(
            x0, y0, x0 + cell, y0 + cell, fill=color, outline="", tags=tag
        )

    for r in range(BODY_H):
        for c in range(BODY_W):
            if CLAWD_BODY[r][c]:
                px(ox + c, oy + r, body_color)

    variant = EYES.get(eye_for(mood, frame), EYES["forward"])
    if not variant.get("hidden"):
        lx, ly = variant["left"]
        rx, ry = variant["right"]
        px(ox + EYE_LEFT[0] + lx, oy + EYE_LEFT[1] + ly, EYE_COLOR)
        px(ox + EYE_RIGHT[0] + rx, oy + EYE_RIGHT[1] + ry, EYE_COLOR)

    if visor:
        for row_offset, columns, colour in VISOR_ROWS:
            for column in columns:
                px(ox + column, oy + row_offset, colour)

    _draw_accessory(px, frame, mood, oy)


def _draw_accessory(px, frame: int, mood: str, oy: int) -> None:
    """A small state cue above Clawd: hearts when healthy, zZ when asleep."""
    if mood == MOOD_FRESH and (frame % 48) < 24:
        # A heart drifting up, one cell at a time.
        rise = (frame % 48) // 12
        hy = oy - 2 - rise
        if hy >= 0:
            for r in range(len(HEART)):
                for c in range(len(HEART[r])):
                    if HEART[r][c]:
                        px(10 + c, hy + r, "#CD6E58")

    elif mood == MOOD_SLEEPING:
        # A pixel "z" drifting upward.
        drift = (frame // 8) % 3
        zy = oy - 3 - drift
        if zy >= 0:
            for c in range(4):
                px(10 + c, zy, "#8a8a8a")          # top bar
            px(12, zy + 1, "#8a8a8a")              # upper diagonal
            px(11, zy + 2, "#8a8a8a")              # lower diagonal
            for c in range(4):
                px(10 + c, zy + 3, "#8a8a8a")      # bottom bar

    elif mood == MOOD_STRAINED and (frame % 20) < 10:
        # A single alarm pixel, pulsing.
        px(7, oy - 2, "#d99100")

    elif mood == MOOD_CRITICAL and (frame % 6) < 3:
        # Three red pixels, blinking fast.
        for c in (5, 7, 9):
            px(c, oy - 2, "#cc3333")


def tk_icon(tk_module, cell: int = 2):
    """Build a Tk PhotoImage of Clawd for the window/taskbar icon.

    Tk's iconphoto needs a tk.PhotoImage, so the sprite is written pixel by
    pixel. Small enough that the cost does not matter.
    """
    width, height = BODY_W * cell, BODY_H * cell
    image = tk_module.PhotoImage(width=width, height=height)
    # Transparent background, then the body and eyes on top.
    for r in range(BODY_H):
        for c in range(BODY_W):
            if not CLAWD_BODY[r][c]:
                continue
            for dy in range(cell):
                for dx in range(cell):
                    image.put(BODY_COLOR, (c * cell + dx, r * cell + dy))
    for (ex, ey) in (EYE_LEFT, EYE_RIGHT):
        for dy in range(cell):
            for dx in range(cell):
                image.put(EYE_COLOR, (ex * cell + dx, ey * cell + dy))
    return image


def tk_icon_marked(tk_module, cell: int = 2):
    """The window icon: Clawd wearing the visor, on a square canvas."""
    width = BODY_W * cell
    height = (BODY_H + 2) * cell          # two rows for the visor
    side = max(width, height)
    image = tk_module.PhotoImage(width=side, height=side)
    ox = (side - width) // 2
    oy = (side - height) // 2 + 2 * cell  # body sits below the visor rows

    def paint(gx, gy, colour):
        for dy in range(cell):
            for dx in range(cell):
                x, y = gx * cell + dx + ox, gy * cell + dy + oy
                if 0 <= x < side and 0 <= y < side:
                    image.put(colour, (x, y))

    for r in range(BODY_H):
        for c in range(BODY_W):
            if CLAWD_BODY[r][c]:
                paint(c, r, BODY_COLOR)
    for (ex, ey) in (EYE_LEFT, EYE_RIGHT):
        paint(ex, ey, EYE_COLOR)
    for row_offset, columns, colour in VISOR_ROWS:
        for column in columns:
            paint(column, row_offset, colour)
    return image
