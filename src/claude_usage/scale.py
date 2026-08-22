"""One scale factor for the whole UI.

The design is specified in CSS px at 96dpi. Tk renders point-sized fonts
through the display DPI, so on a 200% display a font doubles while a canvas
width written as a raw number does not. Mixing the two is what makes a layout
drift: the text grows, the bars and padding stay put.

Everything here is expressed in design px and multiplied by one factor, so the
window is proportionally identical to the artboard at any DPI.

    S = display_dpi / 96

Call `init(widget)` once after the first Tk window exists, then use `px()` for
every dimension and `font()` for every font.
"""

from __future__ import annotations

_scale = 1.0
_ready = False

FAMILY = "Segoe UI"
MONO = "Consolas"


def init(widget) -> float:
    """Read the display DPI from a live widget. Safe to call more than once."""
    global _scale, _ready
    try:
        dpi = float(widget.winfo_fpixels("1i"))
    except Exception:
        dpi = 96.0
    if dpi <= 0:
        dpi = 96.0
    _scale = dpi / 96.0
    _ready = True
    return _scale


def scale() -> float:
    return _scale


def is_ready() -> bool:
    return _ready


def px(design_px: float) -> int:
    """A design-px dimension in real pixels. Never returns 0 for a visible size."""
    value = int(round(design_px * _scale))
    if design_px > 0 and value < 1:
        return 1
    return value


def font(design_px: float, bold: bool = False, mono: bool = False):
    """A font sized in design px.

    Tk treats a NEGATIVE size as pixels, which sidesteps the point conversion
    and its rounding (9px would land on 6.75pt). Scaling the pixel size by S
    keeps fonts in step with every other dimension.
    """
    family = MONO if mono else FAMILY
    size = -px(design_px)
    return (family, size, "bold") if bold else (family, size)
