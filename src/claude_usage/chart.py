"""The seven-day chart in the drawer.

Drawn on one canvas whose items are created once and moved afterwards, so a
hover does not rebuild anything. All geometry is design px through scale.py.

Design: 340 x 118 plot, guides at 80% and 50%, y = 114 - 1.1 * utilization,
area fill under the line tinted by the latest value.
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from typing import Any

from . import scale
from .formatting import color_for

BG_PLOT = "#141414"
BG_DETAIL = "#1c1c1c"
BG_SEGMENT_ON = "#2f2f2f"
FG = "#ededed"
FG_DIM = "#8f8f8f"
FG_MUTED = "#6b6b6b"
FG_DISABLED = "#4a4a4a"
GUIDE_80 = "#3a3a3a"
GUIDE_50 = "#242424"

PLOT_W, PLOT_H = 340, 118

# Area fills, picked by the same threshold as the line colour.
AREA_FILL = {
    "#2e9e4f": "#16301f",
    "#d99100": "#33260a",
    "#cc3333": "#331111",
    "#8a8a8a": "#242424",
}


def window_options(snapshot) -> list[tuple[str, str]]:
    """(caption, history key) for the three switchable series.

    History rows for limits[] live under a "limits:" prefix, so the scoped
    series has to be looked up by the limit's display name, not the raw key.
    """
    options: list[tuple[str, str]] = []
    if snapshot is None:
        return options

    if snapshot.window("five_hour") is not None:
        options.append(("session", "five_hour"))
    else:
        options.append(("session", "limits:session"))

    if snapshot.window("seven_day") is not None:
        options.append(("weekly", "seven_day"))
    else:
        options.append(("weekly", "limits:weekly_all"))

    scoped = [r for r in snapshot.limits if r.scope_label and r.percent is not None]
    if scoped:
        worst = max(scoped, key=lambda r: r.percent)
        options.append((worst.scope_label, "limits:" + worst.display_name))
    return options


class Chart:
    def __init__(self, parent: tk.Widget, on_switch=None):
        self.on_switch = on_switch
        self.points: list[tuple[int, float]] = []
        self.hover_index: int | None = None
        self._options: list[tuple[str, str]] = []
        self._selected = 0
        self._available: dict[str, bool] = {}

        self.w = scale.px(PLOT_W)
        self.h = scale.px(PLOT_H)

        self.frame = tk.Frame(parent, bg=BG_DETAIL)

        head = tk.Frame(self.frame, bg=BG_DETAIL)
        head.pack(fill="x")
        self.heading = tk.Label(
            head, text="LAST 7 DAYS", bg=BG_DETAIL, fg=FG_MUTED,
            font=scale.font(10, bold=True), pady=0, bd=0, highlightthickness=0,
        )
        self.heading.pack(side="left")
        self.readout = tk.Label(
            head, text="", bg=BG_DETAIL, fg=FG, font=scale.font(12, bold=True),
            pady=0, bd=0, highlightthickness=0,
        )
        self.readout.pack(side="left", padx=(scale.px(8), 0))

        self.segments = tk.Frame(head, bg=BG_DETAIL)
        self.segments.pack(side="right")
        self._segment_labels: list[tk.Label] = []

        self.canvas = tk.Canvas(
            self.frame, width=self.w, height=self.h, bg=BG_PLOT,
            highlightthickness=0,
        )
        self.canvas.pack(pady=(scale.px(6), 0))

        self.footer = tk.Label(
            self.frame, text="", bg=BG_DETAIL, fg=FG_MUTED, font=scale.font(10),
            anchor="w", pady=0, bd=0, highlightthickness=0,
        )
        self.footer.pack(fill="x", pady=(scale.px(5), 0))
        self.note = tk.Label(
            self.frame, text="", bg=BG_DETAIL, fg=FG_MUTED, font=scale.font(10),
            anchor="w", pady=0, bd=0, highlightthickness=0,
        )
        self.note.pack(fill="x", pady=(scale.px(2), 0))

        self._build_static()
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

    # ---------------------------------------------------------------- canvas

    def _y(self, value: float) -> float:
        """Design mapping: y = 114 - 1.1 * utilization, scaled."""
        return scale.px(114 - 1.1 * max(0.0, min(100.0, value)))

    def _build_static(self) -> None:
        c = self.canvas
        # Guides first so the series draws over them.
        c.create_line(
            0, self._y(80), self.w, self._y(80),
            fill=GUIDE_80, dash=(scale.px(3), scale.px(3)),
        )
        c.create_line(0, self._y(50), self.w, self._y(50), fill=GUIDE_50)
        c.create_text(
            self.w - scale.px(3), self._y(80) - scale.px(6), text="80%",
            fill=FG_MUTED, font=scale.font(10), anchor="e",
        )
        c.create_text(
            self.w - scale.px(3), self._y(50) - scale.px(6), text="50%",
            fill=FG_MUTED, font=scale.font(10), anchor="e",
        )

        self.area = c.create_polygon(0, 0, 0, 0, 0, 0, fill="", outline="")
        self.line = c.create_line(0, 0, 0, 0, fill=FG_MUTED, width=scale.px(2))
        self.empty_text = c.create_text(
            self.w / 2, self.h / 2, text="", fill=FG_MUTED, font=scale.font(11)
        )
        # Hover furniture, hidden until the pointer is over the plot.
        self.crosshair = c.create_line(
            0, 0, 0, self.h, fill=FG_MUTED, state="hidden"
        )
        dot = scale.px(3)
        self.dot = c.create_oval(
            -dot, -dot, dot, dot, fill=FG, outline="", state="hidden"
        )

    def _x(self, index: int, count: int) -> float:
        if count <= 1:
            return scale.px(2)
        span = self.w - scale.px(4)
        return scale.px(2) + span * index / (count - 1)

    # ----------------------------------------------------------------- state

    def set_options(self, options: list[tuple[str, str]], available: dict) -> None:
        """Rebuild the segmented switch only when the captions change."""
        captions = [o[0] for o in options]
        if captions == [o[0] for o in self._options] and available == self._available:
            return
        self._options = options
        self._available = dict(available)
        for label in self._segment_labels:
            label.destroy()
        self._segment_labels = []
        for index, (caption, key) in enumerate(options):
            usable = available.get(key, False)
            label = tk.Label(
                self.segments, text=caption, bg=BG_DETAIL,
                fg=FG_DIM if usable else FG_DISABLED, font=scale.font(10),
                padx=scale.px(8), pady=scale.px(3), bd=0, highlightthickness=0,
            )
            label.pack(side="left")
            if usable:
                label.bind("<Button-1>", lambda _e, i=index: self._select(i))
            self._segment_labels.append(label)
        if self._selected >= len(options):
            self._selected = 0
        self._paint_segments()

    def _select(self, index: int) -> None:
        self._selected = index
        self._paint_segments()
        if self.on_switch:
            self.on_switch(self.selected_key())

    def _paint_segments(self) -> None:
        for index, label in enumerate(self._segment_labels):
            if index >= len(self._options):
                continue
            key = self._options[index][1]
            usable = self._available.get(key, False)
            active = index == self._selected and usable
            label.configure(
                bg=BG_SEGMENT_ON if active else BG_DETAIL,
                fg=FG if active else (FG_DIM if usable else FG_DISABLED),
            )

    def selected_key(self) -> str | None:
        if not self._options:
            return None
        index = min(self._selected, len(self._options) - 1)
        return self._options[index][1]

    def selected_caption(self) -> str:
        if not self._options:
            return ""
        return self._options[min(self._selected, len(self._options) - 1)][0]

    def choose_available(self, available: dict) -> None:
        """Fall back to a series that has history when the current one lacks it."""
        key = self.selected_key()
        if key is not None and available.get(key):
            return
        for index, (_caption, candidate) in enumerate(self._options):
            if available.get(candidate):
                self._selected = index
                self._paint_segments()
                return

    # ------------------------------------------------------------------ draw

    def set_series(self, points: list[tuple[int, float]], total_samples: int) -> None:
        self.points = points
        self.heading.configure(text=self.selected_caption().upper() + " · LAST 7 DAYS")

        if len(points) < 2:
            self.canvas.itemconfigure(self.line, state="hidden")
            self.canvas.itemconfigure(self.area, state="hidden")
            self.canvas.itemconfigure(
                self.empty_text, state="normal",
                text="collecting history (%d sample%s)"
                     % (len(points), "" if len(points) == 1 else "s"),
            )
            self.footer.configure(text="")
            self.note.configure(text="history builds as polls land")
            return

        self.canvas.itemconfigure(self.empty_text, state="hidden")
        self.canvas.itemconfigure(self.line, state="normal")
        self.canvas.itemconfigure(self.area, state="normal")

        count = len(points)
        coords: list[float] = []
        for index, (_ts, value) in enumerate(points):
            coords.extend([self._x(index, count), self._y(value)])

        stroke = color_for(points[-1][1])
        self.canvas.coords(self.line, *coords)
        self.canvas.itemconfigure(self.line, fill=stroke)

        # Close the polygon along the bottom edge for the area fill.
        area = list(coords)
        area.extend([self._x(count - 1, count), self.h, self._x(0, count), self.h])
        self.canvas.coords(self.area, *area)
        self.canvas.itemconfigure(self.area, fill=AREA_FILL.get(stroke, GUIDE_50))

        peak = max(v for _t, v in points)
        self.footer.configure(
            text="7d ago    %s peak    today"
            % (("%d%%" % round(peak)) if peak else "0%")
        )
        self.note.configure(
            text="hover the chart · %d points at 20-min resolution, from ~%d polls"
            % (count, total_samples)
        )

    # ----------------------------------------------------------------- hover

    def _on_motion(self, event) -> None:
        if len(self.points) < 2:
            return
        count = len(self.points)
        span = self.w - scale.px(4)
        ratio = (event.x - scale.px(2)) / span if span else 0
        index = int(round(ratio * (count - 1)))
        index = max(0, min(count - 1, index))
        if index == self.hover_index:
            return
        self.hover_index = index

        ts, value = self.points[index]
        x, y = self._x(index, count), self._y(value)
        self.canvas.coords(self.crosshair, x, 0, x, self.h)
        self.canvas.itemconfigure(self.crosshair, state="normal")
        dot = scale.px(3)
        self.canvas.coords(self.dot, x - dot, y - dot, x + dot, y + dot)
        self.canvas.itemconfigure(self.dot, state="normal")

        stamp = datetime.fromtimestamp(ts).strftime("%a %H:%M")
        self.heading.configure(text=stamp)
        self.readout.configure(text="%d%%" % round(value), fg=color_for(value))

        # Change over the hour before this sample, in percentage points.
        previous = None
        for older_ts, older_value in self.points[:index]:
            if ts - older_ts >= 3600:
                previous = older_value
        if previous is None and index > 0:
            previous = self.points[0][1]
        if previous is not None:
            delta = value - previous
            self.footer.configure(
                text="%+.1f pts in the hour before" % delta
            )

    def _on_leave(self, _event=None) -> None:
        self.hover_index = None
        self.canvas.itemconfigure(self.crosshair, state="hidden")
        self.canvas.itemconfigure(self.dot, state="hidden")
        self.readout.configure(text="")
        self.set_series(self.points, self._last_total)

    _last_total = 0

    def render(self, points, total_samples: int) -> None:
        self._last_total = total_samples
        self.set_series(points, total_samples)
