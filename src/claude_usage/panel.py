"""The detail window: app bar, tile board, and a selection-driven drawer.

Three rules drive the structure:

1. **Never rebuild to refresh.** Tk has no double-buffering, so destroying and
   recreating the tree makes the whole window flash. Widgets are created once
   and their values updated in place. A structural rebuild happens only when
   the set of keys an account reports changes, which is rare.

2. **Glanceable first, complete on demand.** The board shows every account at
   once. Selecting one fills the drawer below with everything the endpoint
   returned for it.

3. **One scale factor.** Every number below is design px from the handoff,
   passed through `scale.px`. Fonts go through `scale.font`, which sizes them
   in pixels rather than points. Mixing DPI-scaled fonts with raw-pixel
   dimensions is what made an earlier build drift.

Design baseline: a 940 x 655 artboard, 44px app bar, 451 x 150 tiles.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from . import chrome, clawd, scale
from .accounts_tab import AccountsTab
from .formatting import (
    COLOR_AMBER,
    COLOR_RED,
    color_for,
    format_age,
    format_duration,
    format_percent,
    format_reset_absolute,
    format_reset_relative,
)
from .poller import ErrorKind, Poller

APP_NAME = "Clauculate"

BG = "#171717"
FG = "#ededed"
FG_DIM = "#8f8f8f"
FG_MUTED = "#6b6b6b"
BG_CARD = "#212121"
BG_CARD_HOVER = "#282828"
BG_TRACK = "#333333"
BG_DETAIL = "#1c1c1c"
CORAL = clawd.BODY_COLOR

BG_BAR = "#101010"
BG_STRIP = "#141414"
BG_SELECTED = "#2b2b2b"
BG_SEGMENT_ON = "#2f2f2f"
BAR_TRACK = "#2a2a2a"
DIVIDER = "#2a2a2a"
HAIRLINE = "#3a3a3a"
RAW_KEY = "#5c5c5c"
ACTIVE_LIMIT = "#ffd479"

# ---- design px
ART_W, ART_H = 940, 655
BAR_H = 44
BAR_PAD = 14
BAR_GAP = 14
STRIP_PAD_X, STRIP_PAD_Y = 14, 8
BOARD_PAD = 8
TILE_W, TILE_H = 451, 150
TILE_GAP = 6
SPINE = 3
TILE_PAD_X, TILE_PAD_Y = 14, 12
COL_W, COL_TRACK_H, COL_GAP = 24, 64, 8
DRAWER_PAD = 14
MAX_COLUMNS = 3

AGG_MODES = ("worst", "average", "most free")


def _stamp(status) -> tuple[str, str]:
    """Freshness text and colour. Live and stale must never look alike."""
    if status.last_success is None:
        return "never updated", COLOR_RED
    if status.is_stale:
        return "STALE " + format_duration(status.age_seconds), COLOR_AMBER
    return format_age(status.age_seconds), FG_MUTED


def _error_text(status) -> str:
    if status.error_kind != ErrorKind.RATE_LIMIT:
        return status.error or ""
    text = "rate limited (429) - retrying in " + format_duration(
        status.backoff_remaining
    )
    if status.snapshot is not None:
        text += "   [figures are from the last good poll, not current]"
    return text


def _short_problem(status) -> str | None:
    kind = status.error_kind
    if kind is None:
        return None
    return {
        ErrorKind.RATE_LIMIT: "rate limited",
        ErrorKind.AUTH: "needs re-auth",
        ErrorKind.NETWORK: "offline",
        ErrorKind.SHAPE: "bad response",
        ErrorKind.HTTP: "HTTP error",
    }.get(kind, "error")


def limit_display_name(row) -> str:
    """The /usage vocabulary, applied on top of shape classification.

    The caller renders the raw key beside this, so a renamed or new key stays
    visible rather than being silently re-titled.
    """
    if row.kind == "session":
        return "Session"
    if row.kind == "weekly_all":
        return "Weekly (all models)"
    if row.kind == "weekly_scoped" and row.scope_label:
        return "Weekly (%s)" % row.scope_label
    return row.display_name


def window_display_name(key: str) -> str:
    return {"five_hour": "Session", "seven_day": "Weekly (all models)"}.get(key, key)


def style_widgets(widget) -> None:
    """Dark ttk scrollbars. The default theme draws a light Windows trough."""
    style = ttk.Style(widget)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    for orient in ("Vertical", "Horizontal"):
        name = "Clau.%s.TScrollbar" % orient
        # Drop the stepper arrows: the layout keeps only trough and thumb.
        try:
            style.layout(
                name,
                [(
                    "%s.Scrollbar.trough" % orient,
                    {
                        "children": [(
                            "%s.Scrollbar.thumb" % orient,
                            {"expand": "1", "sticky": "nswe"},
                        )],
                        "sticky": "ns" if orient == "Vertical" else "ew",
                    },
                )],
            )
        except tk.TclError:
            pass
        style.configure(
            name,
            background="#3a3a3a", troughcolor=BG, bordercolor=BG,
            darkcolor="#3a3a3a", lightcolor="#3a3a3a",
            arrowcolor=FG_MUTED, relief="flat",
            width=scale.px(7),
        )
        style.map(name, background=[("active", "#4d4d4d")])


class HBar:
    """A horizontal progress bar, updated rather than recreated."""

    def __init__(self, parent, width: int, height: int = 8, bg: str = BG_DETAIL):
        self.width = scale.px(width)
        self.height = scale.px(height)
        self.canvas = tk.Canvas(
            parent, width=self.width, height=self.height, bg=bg,
            highlightthickness=0,
        )
        self.canvas.create_rectangle(
            0, 0, self.width, self.height, fill=BAR_TRACK, outline=""
        )
        self.fill = self.canvas.create_rectangle(
            0, 0, 0, self.height, fill=BAR_TRACK, outline=""
        )

    def set(self, percent, color: str | None = None) -> None:
        if percent is None:
            self.canvas.coords(self.fill, 0, 0, 0, self.height)
            return
        filled = max(0.0, min(100.0, float(percent))) / 100.0 * self.width
        self.canvas.coords(self.fill, 0, 0, filled, self.height)
        self.canvas.itemconfigure(self.fill, fill=color or color_for(percent))

    def set_bg(self, color: str) -> None:
        self.canvas.configure(bg=color)


class ColumnBar:
    """A vertical column: value above, 24x64 track, caption below."""

    def __init__(self, parent, bg: str = BG_CARD):
        self.w = scale.px(COL_W)
        self.h = scale.px(COL_TRACK_H)
        self.frame = tk.Frame(parent, bg=bg)
        self.value = tk.Label(
            self.frame, text="--", bg=bg, fg=FG_MUTED,
            font=scale.font(12, bold=True), pady=0, bd=0, highlightthickness=0,
        )
        self.value.pack()
        self.canvas = tk.Canvas(
            self.frame, width=self.w, height=self.h, bg=bg, highlightthickness=0
        )
        self.canvas.pack(pady=(scale.px(3), scale.px(3)))
        self.canvas.create_rectangle(0, 0, self.w, self.h, fill=BAR_TRACK, outline="")
        # Anchored to the bottom so the column fills upward.
        self.fill = self.canvas.create_rectangle(
            0, self.h, self.w, self.h, fill=BAR_TRACK, outline=""
        )
        self.caption = tk.Label(
            self.frame, text="", bg=bg, fg=FG_MUTED, font=scale.font(9),
            pady=0, bd=0, highlightthickness=0,
        )
        self.caption.pack()

    def set(self, caption: str, percent) -> None:
        self.caption.configure(text=caption)
        if percent is None:
            self.value.configure(text="--", fg=FG_MUTED)
            self.canvas.coords(self.fill, 0, self.h, self.w, self.h)
            return
        self.value.configure(text=format_percent(percent), fg=color_for(percent))
        filled = max(0.0, min(100.0, float(percent))) / 100.0 * self.h
        self.canvas.coords(self.fill, 0, self.h - filled, self.w, self.h)
        self.canvas.itemconfigure(self.fill, fill=color_for(percent))

    def set_bg(self, color: str) -> None:
        for widget in (self.frame, self.value, self.caption):
            widget.configure(bg=color)
        self.canvas.configure(bg=color)


class AccountTile:
    """One account on the board. Built once, updated in place."""

    METRICS = 3

    def __init__(self, parent: tk.Widget, panel: "Panel", status):
        self.panel = panel
        self.label = status.label
        self._status = status
        self.selected = False
        self._hovering = False

        self.shell = tk.Frame(parent, bg=BG)
        self.spine = tk.Frame(self.shell, bg=BAR_TRACK, width=scale.px(SPINE))
        self.spine.pack(side="left", fill="y")
        self.body = tk.Frame(self.shell, bg=BG_CARD)
        self.body.pack(side="left", fill="both", expand=True)

        top = tk.Frame(self.body, bg=BG_CARD)
        top.pack(fill="x", padx=scale.px(TILE_PAD_X), pady=(scale.px(TILE_PAD_Y), 0))
        self.top = top

        cell = scale.px(4)   # 4 design px per sprite cell -> 56x56
        width, height = clawd.sprite_size(cell)
        self.sprite = tk.Canvas(
            top, width=width, height=height, bg=BG_CARD, highlightthickness=0
        )
        self.sprite.pack(side="left", padx=(0, scale.px(10)))
        self.panel.register_sprite(self.sprite, cell, self._mood)

        # Columns reserve their width first; a long identity line would
        # otherwise consume it and clip the captions.
        columns = tk.Frame(top, bg=BG_CARD)
        columns.pack(side="right", anchor="n")
        self.columns_frame = columns
        self.columns = []
        for _ in range(self.METRICS):
            bar = ColumnBar(columns)
            bar.frame.pack(side="left", padx=(scale.px(COL_GAP), 0))
            self.columns.append(bar)

        identity = tk.Frame(top, bg=BG_CARD)
        identity.pack(side="left", anchor="n")
        self.identity = identity
        self.name = tk.Label(
            identity, text=self.label, bg=BG_CARD, fg=FG,
            font=scale.font(15, bold=True), anchor="w",
            pady=0, bd=0, highlightthickness=0,
        )
        self.name.pack(anchor="w")
        self.mood = tk.Label(
            identity, text="", bg=BG_CARD, fg=CORAL, font=scale.font(11),
            anchor="w", pady=0, bd=0, highlightthickness=0,
        )
        self.mood.pack(anchor="w", pady=(scale.px(3), 0))
        self.stamp = tk.Label(
            identity, text="", bg=BG_CARD, fg=FG_MUTED, font=scale.font(11),
            anchor="w", pady=0, bd=0, highlightthickness=0,
        )
        self.stamp.pack(anchor="w", pady=(scale.px(2), 0))

        bottom = tk.Frame(self.body, bg=BG_CARD)
        bottom.pack(
            fill="x", padx=scale.px(TILE_PAD_X),
            pady=(scale.px(10), scale.px(TILE_PAD_Y)),
        )
        self.bottom = bottom
        self.reset_label = tk.Label(
            bottom, text="", bg=BG_CARD, fg=FG_MUTED, font=scale.font(11),
            anchor="w", pady=0, bd=0, highlightthickness=0,
        )
        self.reset_label.pack(side="left")
        self.chevron = tk.Label(
            bottom, text="", bg=BG_CARD, fg=FG_MUTED, font=scale.font(11),
            anchor="e", pady=0, bd=0, highlightthickness=0,
        )
        self.chevron.pack(side="right")
        self.hairline = HBar(bottom, width=90, height=2, bg=BG_CARD)
        self.hairline.canvas.pack(side="left", padx=scale.px(10), expand=True, fill="x")

        for widget in self._clickable():
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

    def _clickable(self):
        return (
            self.body, self.top, self.identity, self.bottom, self.sprite,
            self.name, self.mood, self.stamp, self.reset_label, self.chevron,
            self.columns_frame,
        )

    def _tinted(self):
        return (
            self.body, self.top, self.identity, self.bottom, self.name,
            self.mood, self.stamp, self.reset_label, self.chevron,
            self.columns_frame,
        )

    def _mood(self) -> str:
        status = self._status
        return clawd.mood_for(
            status.snapshot.worst_utilization if status.snapshot else None,
            status.error_kind,
            status.is_stale,
        )

    def _on_click(self, _event=None) -> None:
        self.panel.select(self.label)

    def _on_enter(self, _event=None) -> None:
        self._hovering = True
        self._paint()

    def _on_leave(self, _event=None) -> None:
        self._hovering = False
        self._paint()

    def _paint(self) -> None:
        if self.selected:
            color = BG_SELECTED
        elif self._hovering:
            color = BG_CARD_HOVER
        else:
            color = BG_CARD
        for widget in self._tinted():
            widget.configure(bg=color)
        self.sprite.configure(bg=color)
        self.hairline.set_bg(color)
        for bar in self.columns:
            bar.set_bg(color)

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self._paint()

    def update(self, status) -> None:
        self._status = status
        snapshot = status.snapshot

        worst = snapshot.worst_utilization if snapshot else None
        self.spine.configure(bg=color_for(worst) if snapshot else COLOR_RED)

        self.mood.configure(text=clawd.MOOD_CAPTIONS.get(self._mood(), ""))
        text, color = _stamp(status)
        self.stamp.configure(text=text, fg=color)

        metrics = snapshot.headline_metrics() if snapshot else []
        for i, bar in enumerate(self.columns):
            if i < len(metrics):
                name, percent, _ = metrics[i]
                bar.set(name, percent)
            else:
                bar.set(("session", "weekly", "scoped")[i], None)

        self._update_bottom(status, snapshot, metrics)

    def _update_bottom(self, status, snapshot, metrics) -> None:
        if snapshot is None:
            self.reset_label.configure(
                text=_short_problem(status) or "no data", fg=COLOR_AMBER
            )
            self.hairline.set(None)
            self.chevron.configure(
                text="shown below ▾" if self.selected else "0 keys ▸",
                fg=CORAL if self.selected else FG_MUTED,
            )
            return

        soonest = None
        for name, _pct, resets_at in metrics:
            if resets_at is None:
                continue
            if soonest is None or resets_at < soonest[1]:
                soonest = (name, resets_at)

        if soonest is None:
            self.reset_label.configure(text="nothing to reset", fg=FG_MUTED)
            self.hairline.set(None)
        else:
            name, resets_at = soonest
            remaining = (resets_at - snapshot.fetched_at).total_seconds()
            self.reset_label.configure(
                text="%s resets in %s" % (name, format_duration(remaining)),
                fg=FG_MUTED,
            )
            span = 5 * 3600.0 if name == "session" else 7 * 86400.0
            elapsed = max(0.0, min(1.0, (span - remaining) / span)) * 100.0
            self.hairline.set(elapsed, CORAL if remaining < 3600 else HAIRLINE)

        keys = (
            len(snapshot.windows) + len(snapshot.limits) + len(snapshot.blocks)
            + len(snapshot.scalars) + len(snapshot.null_keys)
        )
        if self.selected:
            self.chevron.configure(text="shown below ▾", fg=CORAL)
        else:
            self.chevron.configure(text="%d keys ▸" % keys, fg=FG_MUTED)

    def destroy(self) -> None:
        self.shell.destroy()


class Drawer:
    """Everything the endpoint returned, for the selected account only."""

    def __init__(self, parent: tk.Widget, panel: "Panel"):
        self.panel = panel
        self.frame = tk.Frame(parent, bg=BG_DETAIL)
        self.label: str | None = None
        self._signature = None
        self._updaters: list[tuple[tk.Widget, Any]] = []
        self._absolute_cells: list[tk.Widget] = []
        self.compact = False

        head = tk.Frame(self.frame, bg=BG_DETAIL)
        head.pack(fill="x", padx=scale.px(DRAWER_PAD), pady=(scale.px(10), scale.px(8)))
        self.spine = tk.Frame(
            head, bg=BAR_TRACK, width=scale.px(SPINE), height=scale.px(13)
        )
        self.spine.pack(side="left", padx=(0, scale.px(10)))
        self.title = tk.Label(
            head, text="", bg=BG_DETAIL, fg=FG, font=scale.font(12, bold=True),
            pady=0, bd=0, highlightthickness=0,
        )
        self.title.pack(side="left", padx=(0, scale.px(10)))
        self.path = tk.Label(
            head, text="", bg=BG_DETAIL, fg=FG_MUTED, font=scale.font(11, mono=True),
            pady=0, bd=0, highlightthickness=0,
        )
        self.path.pack(side="left", padx=(0, scale.px(10)))
        self.stamp = tk.Label(
            head, text="", bg=BG_DETAIL, fg=FG_MUTED, font=scale.font(11),
            pady=0, bd=0, highlightthickness=0,
        )
        self.stamp.pack(side="left")
        tk.Button(
            head, text="Copy raw JSON", command=self._copy,
            bg=BG_TRACK, fg=FG, font=scale.font(11), relief="flat",
            activebackground="#404040", activeforeground=FG,
            padx=scale.px(9), pady=scale.px(3), borderwidth=0, highlightthickness=0,
        ).pack(side="right")

        self.body = tk.Frame(self.frame, bg=BG_DETAIL)
        self.body.pack(fill="both", expand=True)

    def set_compact(self, compact: bool) -> None:
        """Drop the absolute reset time when the window is too narrow for it.

        The relative time carries the same fact in fewer characters, so it is
        the one that survives.
        """
        if compact == self.compact:
            return
        self.compact = compact
        for cell in self._absolute_cells:
            try:
                cell.grid_remove() if compact else cell.grid()
            except tk.TclError:
                pass

    def _copy(self) -> None:
        if self.label:
            self.panel.copy_raw(self.label)

    def show(self, status) -> None:
        signature = (status.label, self._key_signature(status))
        self.label = status.label

        worst = status.snapshot.worst_utilization if status.snapshot else None
        self.spine.configure(bg=color_for(worst) if status.snapshot else COLOR_RED)
        self.title.configure(text=status.label)
        self.path.configure(text=str(status.config_dir))
        text, color = _stamp(status)
        self.stamp.configure(text=text, fg=color)

        if signature != self._signature:
            self._build(status)
            self._signature = signature
            return

        for entry in list(self._updaters):
            widget, produce = entry
            try:
                if not widget.winfo_exists():
                    self._updaters.remove(entry)
                    continue
                widget.configure(**produce())
            except tk.TclError:
                pass

    @staticmethod
    def _key_signature(status) -> tuple:
        snapshot = status.snapshot
        if snapshot is None:
            return ("none", status.error_kind, bool(status.raw_text))
        return (
            tuple(w.key for w in snapshot.windows),
            tuple(x.display_name for x in snapshot.limits),
            tuple((b.key, tuple(k for k, _ in b.fields)) for b in snapshot.blocks),
            tuple(k for k, _ in snapshot.scalars),
            tuple(snapshot.null_keys),
        )

    def _build(self, status) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self._updaters.clear()
        self._absolute_cells.clear()

        pad = scale.px(DRAWER_PAD)
        snapshot = status.snapshot
        if snapshot is None:
            tk.Label(
                self.body, text="! " + (_error_text(status) or "no data yet"),
                bg=BG_DETAIL, fg=COLOR_RED, font=scale.font(12), anchor="w",
                justify="left", wraplength=scale.px(800),
            ).pack(fill="x", padx=pad, pady=(0, scale.px(8)))
            if status.raw_text:
                box = tk.Text(
                    self.body, height=5, bg="#111111", fg=FG,
                    font=scale.font(11, mono=True), relief="flat", wrap="none",
                )
                box.insert("1.0", status.raw_text)
                box.configure(state="disabled")
                box.pack(fill="x", padx=pad, pady=(0, scale.px(8)))
            return

        if status.error:
            tk.Label(
                self.body, text="! " + _error_text(status), bg=BG_DETAIL,
                fg=COLOR_AMBER, font=scale.font(12), anchor="w",
                wraplength=scale.px(800),
            ).pack(fill="x", padx=pad, pady=(0, scale.px(4)))

        grid = tk.Frame(self.body, bg=BG_DETAIL)
        grid.pack(fill="x", padx=pad, pady=(0, scale.px(8)))
        grid.columnconfigure(5, weight=1)

        tk.Label(
            grid, text="LIMITS", bg=BG_DETAIL, fg=FG_MUTED,
            font=scale.font(10, bold=True), pady=0, bd=0, highlightthickness=0,
        ).grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, scale.px(3)))

        row = 1
        shown = set()
        for limit in snapshot.limits:
            name = limit_display_name(limit)
            shown.add(name)
            row = self._limit_row(
                grid, row, name, limit.kind or "", limit.percent,
                limit.resets_at, limit.is_active,
            )
        for window in snapshot.windows:
            name = window_display_name(window.key)
            if name in shown:
                continue
            row = self._limit_row(
                grid, row, name, window.key, window.utilization,
                window.resets_at, False,
            )

        total = (
            len(snapshot.windows) + len(snapshot.limits) + len(snapshot.blocks)
            + len(snapshot.scalars) + len(snapshot.null_keys)
        )
        tk.Label(
            self.body,
            text="%d keys returned · %d reported null" % (total, len(snapshot.null_keys)),
            bg=BG_DETAIL, fg=FG_MUTED, font=scale.font(11), anchor="w",
            pady=0, bd=0, highlightthickness=0,
        ).pack(fill="x", padx=pad, pady=(0, scale.px(10)))

    def _limit_row(self, grid, row, name, raw_key, percent, resets_at, active) -> int:
        gap = scale.px(12)
        cell = tk.Frame(grid, bg=BG_DETAIL)
        cell.grid(row=row, column=0, sticky="w", padx=(0, gap), pady=scale.px(2))
        tk.Label(
            cell, text=name, bg=BG_DETAIL, fg=ACTIVE_LIMIT if active else FG,
            font=scale.font(12), pady=0, bd=0, highlightthickness=0,
        ).pack(side="left")
        # The raw key stays visible so a renamed key is never silently retitled.
        tk.Label(
            cell, text="  " + raw_key, bg=BG_DETAIL, fg=RAW_KEY,
            font=scale.font(10, mono=True), pady=0, bd=0, highlightthickness=0,
        ).pack(side="left")

        tk.Label(
            grid, text=format_percent(percent), bg=BG_DETAIL, fg=color_for(percent),
            font=scale.font(13, bold=True), anchor="e", width=5,
            pady=0, bd=0, highlightthickness=0,
        ).grid(row=row, column=1, sticky="e", padx=(0, scale.px(8)))

        bar = HBar(grid, width=84)
        bar.set(percent)
        bar.canvas.grid(row=row, column=2, padx=(0, gap))

        relative = tk.Label(
            grid, text=format_reset_relative(resets_at), bg=BG_DETAIL, fg=FG,
            font=scale.font(12), anchor="w", pady=0, bd=0, highlightthickness=0,
        )
        relative.grid(row=row, column=3, sticky="w", padx=(0, gap))
        self._updaters.append(
            (relative, lambda r=resets_at: {"text": format_reset_relative(r)})
        )

        absolute = tk.Label(
            grid, text=format_reset_absolute(resets_at), bg=BG_DETAIL, fg=FG_MUTED,
            font=scale.font(12), anchor="w", pady=0, bd=0, highlightthickness=0,
        )
        absolute.grid(row=row, column=4, sticky="w")
        self._absolute_cells.append(absolute)
        if self.compact:
            absolute.grid_remove()
        return row + 1


class Panel:
    def __init__(
        self, root: tk.Tk, poller: Poller, store: Any = None, app_state: Any = None
    ):
        self.root = root
        self.poller = poller
        self.store = store
        self.app_state = app_state
        self.accounts_tab = None
        self.window: tk.Toplevel | None = None

        self._tiles: dict[str, AccountTile] = {}
        self._sprites: list[tuple[Any, int, Any]] = []
        self._frame = 0
        self._refresh_job = None
        self._anim_job = None
        self._resize_job = None
        self._columns = 2
        self._bar_level = None

        self.selected_label: str | None = None
        self.agg_mode = 0
        self.sort_mode = "headroom"
        self.active_tab = "Usage"

    # ----------------------------------------------------------- window mgmt

    def show(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
            if self._refresh_job is None:
                self.refresh()
            if self._anim_job is None:
                self._animate()
            return
        self._build_window()
        self.refresh()
        self._animate()

    def hide(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.withdraw()
        for attr in ("_refresh_job", "_anim_job"):
            handle = getattr(self, attr)
            if handle is not None:
                self.root.after_cancel(handle)
                setattr(self, attr, None)

    def register_sprite(self, canvas, cell, mood_of) -> None:
        self._sprites.append((canvas, cell, mood_of))

    def copy_raw(self, label: str) -> None:
        status = self.poller.status(label)
        text = (status.raw_text if status else None) or "(no response captured yet)"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _build_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title(APP_NAME)
        win.configure(bg=BG)
        self.window = win

        scale.init(win)
        style_widgets(win)

        # Open at the design size, but never larger than the display.
        want_w, want_h = scale.px(ART_W), scale.px(ART_H)
        max_w = int(win.winfo_screenwidth() * 0.92)
        max_h = int(win.winfo_screenheight() * 0.90)
        win.geometry("%dx%d" % (min(want_w, max_w), min(want_h, max_h)))
        # One tile plus the drawer still has to fit.
        win.minsize(
            min(scale.px(TILE_W + SPINE + BOARD_PAD * 2 + 30), max_w),
            min(scale.px(430), max_h),
        )
        win.protocol("WM_DELETE_WINDOW", self.hide)

        try:
            self._icon = clawd.tk_icon(tk, max(1, scale.px(2)))
            win.iconphoto(False, self._icon)
        except tk.TclError:
            pass
        chrome.use_dark_title_bar(win)

        self._build_app_bar(win)
        self._build_status_strip(win)

        content = tk.Frame(win, bg=BG)
        content.pack(fill="both", expand=True)
        self.usage_frame = tk.Frame(content, bg=BG)
        self.accounts_frame = tk.Frame(content, bg=BG)
        self.usage_frame.pack(fill="both", expand=True)

        if self.app_state is not None:
            self.accounts_tab = AccountsTab(self.accounts_frame, self.app_state)
            self.accounts_tab.frame.pack(fill="both", expand=True)
        else:
            tk.Label(
                self.accounts_frame, text="Account management unavailable here.",
                bg=BG, fg=FG_DIM, font=scale.font(12),
            ).pack(padx=scale.px(DRAWER_PAD), pady=scale.px(DRAWER_PAD))

        self._build_board(self.usage_frame)
        self.select_tab(self.active_tab)
        self._on_sort(self.sort_mode)

    # -------------------------------------------------------------- app bar

    def _build_app_bar(self, parent) -> None:
        bar = tk.Frame(parent, bg=BG_BAR, height=scale.px(BAR_H))
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.bar = bar

        pad = scale.px(BAR_PAD)
        gap = scale.px(BAR_GAP)

        cell = max(1, scale.px(2))   # 2 design px per cell -> 28x28
        width, height = clawd.sprite_size(cell)
        lead = tk.Canvas(bar, width=width, height=height, bg=BG_BAR,
                         highlightthickness=0)
        # Everything in the bar is centred on the same axis.
        lead.pack(side="left", padx=(pad, scale.px(10)))
        self.register_sprite(lead, cell, self._worst_mood)

        # Tk cannot colour part of one label's text, so the wordmark is two.
        mark = tk.Frame(bar, bg=BG_BAR)
        mark.pack(side="left", padx=(0, gap))
        for text, colour in (("Clau", CORAL), ("culate", FG)):
            tk.Label(
                mark, text=text, bg=BG_BAR, fg=colour,
                font=scale.font(13, bold=True), padx=0, pady=0, bd=0,
                highlightthickness=0,
            ).pack(side="left")

        tk.Frame(bar, bg=DIVIDER, width=scale.px(1), height=scale.px(18)).pack(
            side="left", padx=(0, gap)
        )

        self._tab_widgets = {}
        for name in ("Usage", "Accounts"):
            holder = tk.Frame(bar, bg=BG_BAR)
            holder.pack(side="left", padx=(0, scale.px(12)), fill="y")
            label = tk.Label(
                holder, text=name, bg=BG_BAR, fg=FG_MUTED,
                font=scale.font(12, bold=True), padx=0, pady=0, bd=0,
                highlightthickness=0,
            )
            # expand centres the text; the rule sits flush on the bar's edge.
            label.pack(expand=True)
            underline = tk.Frame(holder, bg=BG_BAR, height=scale.px(2))
            underline.pack(side="bottom", fill="x")
            for widget in (holder, label):
                widget.bind("<Button-1>", lambda _e, n=name: self.select_tab(n))
            self._tab_widgets[name] = (label, underline)

        self.refresh_button = self._ghost_button(bar, "Refresh", self._on_refresh)
        self.refresh_button.pack(side="right", padx=(scale.px(10), pad))

        self.sort_widgets = self._segmented(bar, ("headroom", "name"), self._on_sort)
        self.sort_widgets["frame"].pack(side="right", padx=(scale.px(10), 0))

        self._build_chip(bar)

    def _build_chip(self, bar) -> None:
        chip = tk.Frame(bar, bg=BG_DETAIL)
        chip.pack(side="right")
        self.chip = chip
        square = scale.px(7)
        self.chip_dot = tk.Canvas(
            chip, width=square, height=square, bg=BG_DETAIL, highlightthickness=0
        )
        self.chip_dot_id = self.chip_dot.create_rectangle(
            0, 0, square, square, fill=FG_MUTED, outline=""
        )
        self.chip_dot.pack(side="left", padx=(scale.px(9), scale.px(7)),
                           pady=scale.px(3))
        self.chip_value = tk.Label(
            chip, text="--", bg=BG_DETAIL, fg=FG, font=scale.font(11, bold=True),
            padx=0, pady=0, bd=0, highlightthickness=0,
        )
        self.chip_value.pack(side="left", padx=(0, scale.px(5)))
        self.chip_mode = tk.Label(
            chip, text=AGG_MODES[0], bg=BG_DETAIL, fg=FG, font=scale.font(11),
            padx=0, pady=0, bd=0, highlightthickness=0,
        )
        self.chip_mode.pack(side="left", padx=(0, scale.px(5)))
        self.chip_subject = tk.Label(
            chip, text="", bg=BG_DETAIL, fg=FG_MUTED, font=scale.font(11),
            padx=0, pady=0, bd=0, highlightthickness=0,
        )
        self.chip_subject.pack(side="left", padx=(0, scale.px(5)))
        self.chip_caret = tk.Label(
            chip, text="▾", bg=BG_DETAIL, fg=FG_MUTED, font=scale.font(9),
            padx=0, pady=0, bd=0, highlightthickness=0,
        )
        self.chip_caret.pack(side="left", padx=(0, scale.px(9)))
        for widget in self._chip_parts():
            widget.bind("<Button-1>", self._cycle_agg)
            widget.bind("<Enter>", lambda _e: self._chip_bg("#262626"))
            widget.bind("<Leave>", lambda _e: self._chip_bg(BG_DETAIL))

    def _chip_parts(self):
        return (self.chip, self.chip_dot, self.chip_value, self.chip_mode,
                self.chip_subject, self.chip_caret)

    def _chip_bg(self, color: str) -> None:
        for widget in self._chip_parts():
            widget.configure(bg=color)

    def _ghost_button(self, parent, text, command) -> tk.Widget:
        """Tk cannot colour a border, so a 1px frame wraps the button."""
        border = tk.Frame(parent, bg=BG_TRACK)
        inner = tk.Button(
            border, text=text, command=command, bg=BG_BAR, fg=FG,
            font=scale.font(11), relief="flat", activebackground="#1c1c1c",
            activeforeground=FG, padx=scale.px(11), pady=scale.px(3),
            borderwidth=0, highlightthickness=0,
        )
        inner.pack(padx=scale.px(1), pady=scale.px(1))
        border.inner = inner
        return border

    def _segmented(self, parent, options, command) -> dict:
        frame = tk.Frame(parent, bg=BG_DETAIL)
        widgets = {"frame": frame, "labels": {}}
        for option in options:
            label = tk.Label(
                frame, text=option, bg=BG_DETAIL, fg=FG_DIM, font=scale.font(11),
                padx=scale.px(10), pady=scale.px(4), bd=0, highlightthickness=0,
            )
            label.pack(side="left")
            label.bind("<Button-1>", lambda _e, o=option: command(o))
            widgets["labels"][option] = label
        return widgets

    def _build_status_strip(self, parent) -> None:
        strip = tk.Frame(parent, bg=BG_STRIP)
        strip.pack(fill="x")
        self.strip_left = tk.Label(
            strip, text="", bg=BG_STRIP, fg=FG_MUTED, font=scale.font(11),
            anchor="w", pady=0, bd=0, highlightthickness=0,
        )
        self.strip_left.pack(
            side="left", padx=scale.px(STRIP_PAD_X), pady=scale.px(STRIP_PAD_Y)
        )
        self.strip_right = tk.Label(
            strip, text="", bg=BG_STRIP, fg=FG_MUTED, font=scale.font(11),
            anchor="e", pady=0, bd=0, highlightthickness=0,
        )
        self.strip_right.pack(
            side="right", padx=scale.px(STRIP_PAD_X), pady=scale.px(STRIP_PAD_Y)
        )

    # ---------------------------------------------------------------- board

    def _build_board(self, parent) -> None:
        pad = scale.px(BOARD_PAD)

        # The drawer is packed against the bottom first, so however many
        # accounts exist the board can never push it off screen.
        self.drawer = Drawer(parent, self)
        self.drawer.frame.pack(side="bottom", fill="x", padx=pad, pady=(0, pad))

        board = tk.Frame(parent, bg=BG)
        board.pack(side="top", fill="both", expand=True, padx=pad, pady=(pad, pad))

        canvas = tk.Canvas(board, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(
            board, orient="vertical", command=canvas.yview,
            style="Clau.Vertical.TScrollbar",
        )
        inner = tk.Frame(canvas, bg=BG)
        item = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )

        def on_canvas_configure(event):
            canvas.itemconfigure(item, width=event.width)
            self._schedule_reflow(event.width)

        canvas.bind("<Configure>", on_canvas_configure)
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

        self.canvas = canvas
        self.inner = inner

    def _schedule_reflow(self, width: int) -> None:
        """Debounce resize: a drag fires <Configure> dozens of times."""
        columns = self._columns_for(width)
        level = self._bar_level_for(width)
        if columns == self._columns and level == self._bar_level:
            return
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(
            60, lambda: self._apply_layout(columns, level)
        )

    @staticmethod
    def _bar_level_for(width: int) -> int:
        """How much of the app bar fits. 3 = everything, 0 = the least."""
        if width >= scale.px(880):
            return 3
        if width >= scale.px(720):
            return 2
        if width >= scale.px(560):
            return 1
        return 0

    def _apply_bar_level(self, level: int) -> None:
        """Shed the least useful controls first, rather than letting Tk clip.

        Order of loss: the chip's subject, then the sort segments, then the
        chip. Refresh always stays.
        """
        self._bar_level = level
        pad = scale.px(BAR_PAD)

        for widget in (self.refresh_button, self.sort_widgets["frame"], self.chip):
            widget.pack_forget()

        self.refresh_button.pack(side="right", padx=(scale.px(10), pad))
        if level >= 2 and self.active_tab == "Usage":
            self.sort_widgets["frame"].pack(side="right", padx=(scale.px(10), 0))
        if level >= 1:
            self.chip.pack(side="right")
        self.chip_subject.pack_forget()
        if level >= 3:
            self.chip_subject.pack(
                side="left", padx=(0, scale.px(5)), before=self.chip_caret
            )
        self.drawer.set_compact(level < 3)

    def _apply_layout(self, columns: int, level: int) -> None:
        self._resize_job = None
        if level != self._bar_level:
            self._apply_bar_level(level)
        if columns != self._columns:
            self._columns = columns
            self._relayout_tiles()

    def _columns_for(self, width: int) -> int:
        # TILE_W already includes the spine; adding it again cost a column.
        tile = scale.px(TILE_W + TILE_GAP)
        return max(1, min(MAX_COLUMNS, width // tile)) if tile else 1

    # -------------------------------------------------------------- actions

    def select(self, label: str) -> None:
        if self.selected_label == label:
            return
        self.selected_label = label
        for name, tile in self._tiles.items():
            tile.set_selected(name == label)
        status = self.poller.status(label)
        if status is not None:
            self.drawer.show(status)

    def select_tab(self, name: str) -> None:
        self.active_tab = name
        for tab, (label, underline) in self._tab_widgets.items():
            active = tab == name
            label.configure(fg=FG if active else FG_MUTED)
            underline.configure(bg=CORAL if active else BG_BAR)
        if name == "Usage":
            self.accounts_frame.pack_forget()
            self.usage_frame.pack(fill="both", expand=True)
            self.refresh_button.inner.configure(
                text="Refresh", command=self._on_refresh
            )
            if (self._bar_level or 3) >= 2:
                self.sort_widgets["frame"].pack(
                    side="right", padx=(scale.px(10), 0), before=self.chip
                )
        else:
            self.usage_frame.pack_forget()
            self.accounts_frame.pack(fill="both", expand=True)
            self.refresh_button.inner.configure(
                text="Scan for profiles",
                command=lambda: self.accounts_tab and self.accounts_tab.scan(),
            )
            self.sort_widgets["frame"].pack_forget()

    def _on_refresh(self) -> None:
        self.poller.request_refresh()

    def _on_sort(self, mode: str) -> None:
        self.sort_mode = mode
        for option, label in self.sort_widgets["labels"].items():
            active = option == mode
            label.configure(
                bg=BG_SEGMENT_ON if active else BG_DETAIL,
                fg=FG if active else FG_DIM,
            )
        self._relayout_tiles()

    def _cycle_agg(self, _event=None) -> None:
        self.agg_mode = (self.agg_mode + 1) % len(AGG_MODES)

    # -------------------------------------------------------------- refresh

    def refresh(self) -> None:
        if self.window is None or not self.window.winfo_exists():
            self._refresh_job = None
            return

        statuses = self.poller.statuses()
        self._sync_tiles(statuses)
        for label, tile in self._tiles.items():
            status = statuses.get(label)
            if status is not None:
                tile.update(status)

        if self.selected_label is None and statuses:
            self.select(self._default_selection(statuses))
        elif self.selected_label in statuses:
            self.drawer.show(statuses[self.selected_label])

        self._update_chip(statuses)
        self._update_strip(statuses)

        self._refresh_job = self.root.after(1000, self.refresh)

    def _default_selection(self, statuses: dict) -> str:
        worst_label, worst_value = None, -1.0
        for account in self.poller.accounts:
            status = statuses.get(account.label)
            if status is None or status.snapshot is None:
                continue
            value = status.snapshot.worst_utilization
            if value is not None and value > worst_value:
                worst_label, worst_value = account.label, value
        return worst_label or self.poller.accounts[0].label

    def _ordered_labels(self, statuses: dict) -> list[str]:
        labels = [a.label for a in self.poller.accounts if a.label in statuses]
        if self.sort_mode == "name":
            return sorted(labels, key=str.lower)

        def headroom(label):
            snapshot = statuses[label].snapshot
            if snapshot is None or snapshot.worst_utilization is None:
                return -1.0
            return 100.0 - snapshot.worst_utilization

        return sorted(labels, key=headroom)

    def _sync_tiles(self, statuses: dict) -> None:
        wanted = [a.label for a in self.poller.accounts if a.label in statuses]
        changed = False
        for label in list(self._tiles):
            if label not in wanted:
                self._tiles.pop(label).destroy()
                changed = True
        for label in wanted:
            if label not in self._tiles:
                self._tiles[label] = AccountTile(self.inner, self, statuses[label])
                changed = True
        if changed:
            self._relayout_tiles()

    def _relayout_tiles(self) -> None:
        """Grid the tiles at their design width, centred.

        Stretching a 451px tile across a 2700px window leaves the content
        stranded at both edges. Instead the tile columns keep their design
        width and two weighted spacer columns absorb whatever is left over.
        """
        statuses = self.poller.statuses()
        columns = max(1, self._columns)
        tile_w = scale.px(TILE_W)

        self.inner.columnconfigure(0, weight=1, minsize=0, uniform="")
        for index in range(MAX_COLUMNS):
            self.inner.columnconfigure(
                index + 1,
                weight=0,
                minsize=tile_w if index < columns else 0,
                uniform="",
            )
        self.inner.columnconfigure(MAX_COLUMNS + 1, weight=1, minsize=0, uniform="")

        gap = max(1, scale.px(TILE_GAP) // 2)
        for index, label in enumerate(self._ordered_labels(statuses)):
            tile = self._tiles.get(label)
            if tile is None:
                continue
            tile.shell.grid(
                row=index // columns,
                column=1 + (index % columns),
                sticky="nsew", padx=gap, pady=gap,
            )
            tile.set_selected(label == self.selected_label)

    def _update_chip(self, statuses: dict) -> None:
        worsts = []
        for label, status in statuses.items():
            if status.snapshot is None:
                continue
            value = status.snapshot.worst_utilization
            if value is not None:
                worsts.append((label, value))

        self.chip_mode.configure(text=AGG_MODES[self.agg_mode])
        if not worsts:
            self.chip_value.configure(text="--")
            self.chip_subject.configure(text="no data")
            self.chip_dot.itemconfigure(self.chip_dot_id, fill=FG_MUTED)
            return

        mode = AGG_MODES[self.agg_mode]
        if mode == "worst":
            label, value = max(worsts, key=lambda x: x[1])
            shown, subject, tone = value, label, value
        elif mode == "average":
            value = sum(v for _, v in worsts) / len(worsts)
            shown, subject, tone = value, "%d accounts" % len(worsts), value
        else:
            label, value = min(worsts, key=lambda x: x[1])
            shown, subject, tone = 100.0 - value, label, value

        self.chip_value.configure(text=format_percent(shown))
        self.chip_subject.configure(text=subject)
        self.chip_dot.itemconfigure(self.chip_dot_id, fill=color_for(tone))

    def _update_strip(self, statuses: dict) -> None:
        ages = [s.age_seconds for s in statuses.values() if s.age_seconds is not None]
        parts = ["polled %s" % (format_age(min(ages)) if ages else "never")]
        due = self.poller.next_due_seconds()
        if due is not None:
            parts.append("next in %s" % format_duration(due))
        problems = [
            "%s %s" % (label, _short_problem(status))
            for label, status in statuses.items()
            if status.error_kind is not None
        ]
        parts.extend(problems[:2])
        self.strip_left.configure(text=" · ".join(parts))
        self.strip_right.configure(
            text="%d account%s" % (len(statuses), "" if len(statuses) == 1 else "s")
        )

    def _worst_mood(self) -> str:
        rank = {
            clawd.MOOD_FRESH: 0, clawd.MOOD_HEALTHY: 1, clawd.MOOD_BUSY: 2,
            clawd.MOOD_SLEEPING: 3, clawd.MOOD_STRAINED: 4,
            clawd.MOOD_CRITICAL: 5, clawd.MOOD_SAD: 6,
        }
        worst = clawd.MOOD_FRESH
        for status in self.poller.statuses().values():
            mood = clawd.mood_for(
                status.snapshot.worst_utilization if status.snapshot else None,
                status.error_kind, status.is_stale,
            )
            if rank.get(mood, 0) > rank.get(worst, 0):
                worst = mood
        return worst

    def _animate(self) -> None:
        if self.window is None or not self.window.winfo_exists():
            self._anim_job = None
            return
        self._frame += 1
        for entry in list(self._sprites):
            canvas, cell, mood_of = entry
            try:
                if not canvas.winfo_exists():
                    self._sprites.remove(entry)
                    continue
                clawd.draw_clawd(canvas, self._frame, mood_of(), cell=cell)
            except tk.TclError:
                pass
        self._anim_job = self.root.after(125, self._animate)
