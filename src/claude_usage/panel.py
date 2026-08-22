"""The detail window: app bar, tile board, and a selection-driven drawer.

Two rules drive the structure:

1. **Never rebuild to refresh.** Tk has no double-buffering, so destroying and
   recreating the tree makes the whole window flash. Widgets are created once
   and their values updated in place. A structural rebuild happens only when
   the set of keys an account reports changes, which is rare.

2. **Glanceable first, complete on demand.** The board shows every account at
   once. Selecting one fills the drawer below with everything the endpoint
   returned for it.

Layout follows the design handoff (Turn 2: board + app bar). Design sizes are
CSS px at 96dpi; Tk font sizes are points, so pt = px * 0.75.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Any

from . import clawd
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
    format_value,
)
from .poller import ErrorKind, Poller

APP_NAME = "Clauculate"

# --- existing tokens
BG = "#171717"
FG = "#ededed"
FG_DIM = "#8f8f8f"
FG_MUTED = "#6b6b6b"
BG_CARD = "#212121"
BG_CARD_HOVER = "#282828"
BG_TRACK = "#333333"
BG_DETAIL = "#1c1c1c"
CORAL = clawd.BODY_COLOR

# --- new in the redesign
BG_BAR = "#101010"
BG_STRIP = "#141414"
BG_SELECTED = "#2b2b2b"
BG_SEGMENT_ON = "#2f2f2f"
BAR_TRACK = "#2a2a2a"
DIVIDER = "#2a2a2a"
HAIRLINE = "#3a3a3a"
RAW_KEY = "#5c5c5c"

FONT = ("Segoe UI", 9)            # 12px
FONT_BOLD = ("Segoe UI", 10, "bold")   # 13px
FONT_MONO = ("Consolas", 9)
FONT_SMALL = ("Segoe UI", 8)      # 10-11px
FONT_TINY = ("Segoe UI", 7)       # 9px
FONT_SECTION = ("Segoe UI", 8, "bold")
FONT_TILE = ("Segoe UI", 11, "bold")   # 15px
FONT_TAB = ("Segoe UI", 9, "bold")     # 12px

PAD = 14
GAP = 8

BAR_HEIGHT = 44
DRAWER_HEIGHT = 300   # pinned; the board gets whatever is left

AGG_MODES = ("worst", "average", "most free")


def _stamp(status, compact: bool = False) -> tuple[str, str]:
    """Freshness text and colour. Live and stale must never look alike.

    compact=True is for the tile, where the line is shared with the mood
    caption and a long string clips mid-word.
    """
    if status.last_success is None:
        return ("no data" if compact else "never updated"), COLOR_RED
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
    """A few words naming what is wrong, for the status strip."""
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

    The raw key is rendered beside this by the caller, so a renamed or new key
    stays visible rather than being silently re-titled.
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


class HBar:
    """A horizontal progress bar, updated rather than recreated."""

    def __init__(self, parent, width: int = 84, height: int = 8, bg: str = BG_DETAIL):
        self.width = width
        self.height = height
        self.canvas = tk.Canvas(
            parent, width=width, height=height, bg=bg, highlightthickness=0
        )
        self.canvas.create_rectangle(0, 0, width, height, fill=BAR_TRACK, outline="")
        self.fill = self.canvas.create_rectangle(
            0, 0, 0, height, fill=BAR_TRACK, outline=""
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
    """A vertical 24x64 column with the value above and a caption below."""

    WIDTH = 24
    TRACK_H = 36

    def __init__(self, parent, bg: str = BG_CARD):
        self.frame = tk.Frame(parent, bg=bg)
        self.value = tk.Label(
            self.frame, text="--", bg=bg, fg=FG_MUTED, font=("Segoe UI", 9, "bold")
        )
        self.value.pack()
        self.canvas = tk.Canvas(
            self.frame, width=self.WIDTH, height=self.TRACK_H, bg=bg,
            highlightthickness=0,
        )
        self.canvas.pack(pady=(1, 1))
        self.canvas.create_rectangle(
            0, 0, self.WIDTH, self.TRACK_H, fill=BAR_TRACK, outline=""
        )
        # Anchored to the bottom, so the column fills upward.
        self.fill = self.canvas.create_rectangle(
            0, self.TRACK_H, self.WIDTH, self.TRACK_H, fill=BAR_TRACK, outline=""
        )
        self.caption = tk.Label(self.frame, text="", bg=bg, fg=FG_MUTED, font=FONT_TINY)
        self.caption.pack()

    def set(self, caption: str, percent) -> None:
        self.caption.configure(text=caption)
        if percent is None:
            self.value.configure(text="--", fg=FG_MUTED)
            self.canvas.coords(self.fill, 0, self.TRACK_H, self.WIDTH, self.TRACK_H)
            return
        self.value.configure(text=format_percent(percent), fg=color_for(percent))
        height = max(0.0, min(100.0, float(percent))) / 100.0 * self.TRACK_H
        self.canvas.coords(
            self.fill, 0, self.TRACK_H - height, self.WIDTH, self.TRACK_H
        )
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
        self.spine = tk.Frame(self.shell, bg=BAR_TRACK, width=3)
        self.spine.pack(side="left", fill="y")
        self.body = tk.Frame(self.shell, bg=BG_CARD)
        self.body.pack(side="left", fill="both", expand=True)

        top = tk.Frame(self.body, bg=BG_CARD)
        top.pack(fill="x", padx=PAD, pady=(10, 0))
        self.top = top

        cell = 4
        width, height = clawd.sprite_size(cell)
        self.sprite = tk.Canvas(
            top, width=width, height=height, bg=BG_CARD, highlightthickness=0
        )
        self.sprite.pack(side="left", padx=(0, 10))
        self.panel.register_sprite(self.sprite, cell, self._mood)

        columns = tk.Frame(top, bg=BG_CARD)
        columns.pack(side="right", anchor="n")
        self.columns_frame = columns
        self.columns = []
        for _ in range(self.METRICS):
            bar = ColumnBar(columns)
            bar.frame.pack(side="left", padx=(GAP, 0))
            self.columns.append(bar)

        identity = tk.Frame(top, bg=BG_CARD)
        identity.pack(side="left", anchor="n")
        self.identity = identity
        self.name = tk.Label(
            identity, text=self.label, bg=BG_CARD, fg=FG, font=FONT_TILE, anchor="w"
        )
        self.name.pack(anchor="w")
        meta = tk.Frame(identity, bg=BG_CARD)
        meta.pack(anchor="w")
        self.meta = meta
        self.mood = tk.Label(
            meta, text="", bg=BG_CARD, fg=CORAL, font=FONT_SMALL, anchor="w"
        )
        self.mood.pack(side="left")
        self.dot = tk.Label(
            meta, text=" · ", bg=BG_CARD, fg=FG_MUTED, font=FONT_SMALL
        )
        self.dot.pack(side="left")
        self.stamp = tk.Label(
            meta, text="", bg=BG_CARD, fg=FG_MUTED, font=FONT_SMALL, anchor="w"
        )
        self.stamp.pack(side="left")

        bottom = tk.Frame(self.body, bg=BG_CARD)
        bottom.pack(fill="x", padx=PAD, pady=(6, 10))
        self.bottom = bottom
        self.reset_label = tk.Label(
            bottom, text="", bg=BG_CARD, fg=FG_MUTED, font=FONT_SMALL, anchor="w"
        )
        self.reset_label.pack(side="left")
        self.hairline = HBar(bottom, width=90, height=2, bg=BG_CARD)
        self.hairline.canvas.pack(side="left", padx=10)
        self.chevron = tk.Label(
            bottom, text="", bg=BG_CARD, fg=FG_MUTED, font=FONT_SMALL, anchor="e"
        )
        self.chevron.pack(side="right")

        for widget in self._clickable():
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

    def _clickable(self):
        return (
            self.body, self.top, self.identity, self.bottom, self.sprite,
            self.name, self.mood, self.stamp, self.reset_label, self.chevron,
            self.columns_frame, self.meta, self.dot,
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
        for widget in (self.body, self.top, self.identity, self.bottom,
                       self.name, self.mood, self.stamp, self.reset_label,
                       self.chevron, self.columns_frame, self.meta, self.dot):
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
        # No snapshot at all reads as a problem, not as "unknown".
        self.spine.configure(bg=color_for(worst) if snapshot else COLOR_RED)

        self.mood.configure(text=clawd.MOOD_CAPTIONS.get(self._mood(), ""))
        text, color = _stamp(status, compact=True)
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
        if status.error and snapshot is None:
            self.reset_label.configure(text=_short_problem(status) or "", fg=COLOR_AMBER)
            self.hairline.set(None)
            self.chevron.configure(
                text="0 keys ▸" if not self.selected else "shown below ▾"
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
            remaining = (resets_at - status.snapshot.fetched_at).total_seconds()
            self.reset_label.configure(
                text="%s resets in %s" % (name, format_duration(remaining)),
                fg=FG_MUTED,
            )
            # Span is 5h for a session window, 7d for anything weekly.
            span = 5 * 3600.0 if name == "session" else 7 * 86400.0
            elapsed = max(0.0, min(1.0, (span - remaining) / span)) * 100.0
            self.hairline.set(
                elapsed, CORAL if remaining < 3600 else HAIRLINE
            )

        keys = 0
        if snapshot is not None:
            keys = (
                len(snapshot.windows) + len(snapshot.limits)
                + len(snapshot.blocks) + len(snapshot.scalars)
                + len(snapshot.null_keys)
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

        head = tk.Frame(self.frame, bg=BG_DETAIL)
        head.pack(fill="x", padx=PAD, pady=(10, GAP))
        self.spine = tk.Frame(head, bg=BAR_TRACK, width=3, height=13)
        self.spine.pack(side="left", padx=(0, 10))
        self.title = tk.Label(
            head, text="", bg=BG_DETAIL, fg=FG, font=("Segoe UI", 9, "bold")
        )
        self.title.pack(side="left", padx=(0, 10))
        self.path = tk.Label(
            head, text="", bg=BG_DETAIL, fg=FG_MUTED, font=("Consolas", 8)
        )
        self.path.pack(side="left", padx=(0, 10))
        self.stamp = tk.Label(
            head, text="", bg=BG_DETAIL, fg=FG_MUTED, font=FONT_SMALL
        )
        self.stamp.pack(side="left")
        tk.Button(
            head, text="Copy raw JSON", command=self._copy,
            bg=BG_TRACK, fg=FG, font=FONT_SMALL, relief="flat",
            activebackground="#404040", activeforeground=FG, padx=9, pady=1,
        ).pack(side="right")

        self.body = tk.Frame(self.frame, bg=BG_DETAIL)
        self.body.pack(fill="both", expand=True)

    def _copy(self) -> None:
        if self.label:
            self.panel.copy_raw(self.label)

    def show(self, status) -> None:
        """Point the drawer at an account. Rebuilds only when its keys change."""
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

        snapshot = status.snapshot
        if snapshot is None:
            message = _error_text(status) or "no data for this account yet"
            tk.Label(
                self.body, text="! " + message, bg=BG_DETAIL, fg=COLOR_RED,
                font=FONT, anchor="w", justify="left", wraplength=900,
            ).pack(fill="x", padx=PAD, pady=(0, GAP))
            if status.raw_text:
                box = tk.Text(
                    self.body, height=5, bg="#111111", fg=FG, font=FONT_MONO,
                    relief="flat", wrap="none",
                )
                box.insert("1.0", status.raw_text)
                box.configure(state="disabled")
                box.pack(fill="x", padx=PAD, pady=(0, GAP))
            return

        if status.error:
            tk.Label(
                self.body, text="! " + _error_text(status), bg=BG_DETAIL,
                fg=COLOR_AMBER, font=FONT, anchor="w", wraplength=900,
            ).pack(fill="x", padx=PAD, pady=(0, 4))

        grid = tk.Frame(self.body, bg=BG_DETAIL)
        grid.pack(fill="x", padx=PAD, pady=(0, GAP))
        grid.columnconfigure(5, weight=1)
        row = 0

        tk.Label(
            grid, text="LIMITS", bg=BG_DETAIL, fg=FG_MUTED, font=FONT_SECTION
        ).grid(row=row, column=0, columnspan=6, sticky="w", pady=(0, 3))
        row += 1

        shown = set()
        for limit in snapshot.limits:
            name = limit_display_name(limit)
            shown.add(name)
            row = self._limit_row(
                grid, row, name, limit.kind or "",
                limit.percent, limit.resets_at, limit.is_active,
            )
        for window in snapshot.windows:
            # five_hour and session are the same limit reported twice; show it
            # once. Anything without a limits[] twin still gets its own row.
            name = window_display_name(window.key)
            if name in shown:
                continue
            row = self._limit_row(
                grid, row, name, window.key,
                window.utilization, window.resets_at, False,
            )

        counts = "%d keys returned · %d reported null" % (
            len(snapshot.windows) + len(snapshot.limits) + len(snapshot.blocks)
            + len(snapshot.scalars) + len(snapshot.null_keys),
            len(snapshot.null_keys),
        )
        tk.Label(
            self.body, text=counts, bg=BG_DETAIL, fg=FG_MUTED, font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", padx=PAD, pady=(0, 10))

    def _limit_row(self, grid, row, name, raw_key, percent, resets_at, active) -> int:
        cell = tk.Frame(grid, bg=BG_DETAIL)
        cell.grid(row=row, column=0, sticky="w", padx=(0, 12))
        tk.Label(
            cell, text=name, bg=BG_DETAIL, fg="#ffd479" if active else FG, font=FONT
        ).pack(side="left")
        # The raw key stays visible so a renamed key is never silently retitled.
        tk.Label(
            cell, text="  " + raw_key, bg=BG_DETAIL, fg=RAW_KEY, font=("Consolas", 8)
        ).pack(side="left")

        tk.Label(
            grid, text=format_percent(percent), bg=BG_DETAIL, fg=color_for(percent),
            font=FONT_BOLD, width=5, anchor="e",
        ).grid(row=row, column=1, sticky="e", padx=(0, GAP))

        bar = HBar(grid, width=84)
        bar.set(percent)
        bar.canvas.grid(row=row, column=2, padx=(0, 12))

        relative = tk.Label(
            grid, text=format_reset_relative(resets_at), bg=BG_DETAIL, fg=FG,
            font=FONT, anchor="w",
        )
        relative.grid(row=row, column=3, sticky="w", padx=(0, 12))
        self._updaters.append(
            (relative, lambda r=resets_at: {"text": format_reset_relative(r)})
        )

        tk.Label(
            grid, text=format_reset_absolute(resets_at), bg=BG_DETAIL, fg=FG_MUTED,
            font=FONT, anchor="w",
        ).grid(row=row, column=4, sticky="w")
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

    # -------------------------------------------------------------- app bar

    def _build_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title(APP_NAME)
        win.configure(bg=BG)
        win.geometry("1120x760")
        win.minsize(860, 420)
        win.protocol("WM_DELETE_WINDOW", self.hide)
        self.window = win

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
                bg=BG, fg=FG_DIM, font=FONT,
            ).pack(padx=PAD, pady=PAD)

        self._build_board(self.usage_frame)

        # Paint the initial control states.
        self.select_tab(self.active_tab)
        self._on_sort(self.sort_mode)

    def _build_app_bar(self, parent) -> None:
        bar = tk.Frame(parent, bg=BG_BAR, height=BAR_HEIGHT)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.bar = bar

        cell = 2
        width, height = clawd.sprite_size(cell)
        lead = tk.Canvas(bar, width=width, height=height, bg=BG_BAR,
                         highlightthickness=0)
        lead.pack(side="left", padx=(PAD, 10))
        self.register_sprite(lead, cell, self._worst_mood)

        # Two labels rather than one: Tk cannot colour part of a label's text.
        mark = tk.Frame(bar, bg=BG_BAR)
        mark.pack(side="left", padx=(0, PAD))
        tk.Label(mark, text="Clau", bg=BG_BAR, fg=CORAL, font=FONT_BOLD,
                 padx=0).pack(side="left")
        tk.Label(mark, text="culate", bg=BG_BAR, fg=FG, font=FONT_BOLD,
                 padx=0).pack(side="left")

        tk.Frame(bar, bg=DIVIDER, width=1, height=18).pack(side="left", padx=(0, PAD))

        self._tab_widgets = {}
        for name in ("Usage", "Accounts"):
            holder = tk.Frame(bar, bg=BG_BAR)
            holder.pack(side="left", padx=(0, 12))
            label = tk.Label(holder, text=name, bg=BG_BAR, fg=FG_MUTED, font=FONT_TAB)
            label.pack(pady=(12, 0))
            underline = tk.Frame(holder, bg=BG_BAR, height=2)
            underline.pack(fill="x", pady=(8, 0))
            for widget in (holder, label):
                widget.bind("<Button-1>", lambda _e, n=name: self.select_tab(n))
            self._tab_widgets[name] = (label, underline)

        # Right-hand controls, packed right-to-left.
        self.refresh_button = self._ghost_button(bar, "Refresh", self._on_refresh)
        self.refresh_button.pack(side="right", padx=(GAP, PAD))

        self.sort_widgets = self._segmented(
            bar, ("headroom", "name"), self._on_sort
        )
        self.sort_widgets["frame"].pack(side="right", padx=(GAP, 0))

        self.chip = tk.Frame(bar, bg=BG_DETAIL)
        self.chip.pack(side="right")
        self.chip_dot = tk.Canvas(self.chip, width=7, height=7, bg=BG_DETAIL,
                                  highlightthickness=0)
        self.chip_dot_id = self.chip_dot.create_rectangle(
            0, 0, 7, 7, fill=FG_MUTED, outline=""
        )
        self.chip_dot.pack(side="left", padx=(9, 7), pady=4)
        self.chip_value = tk.Label(self.chip, text="--", bg=BG_DETAIL, fg=FG,
                                   font=("Segoe UI", 8, "bold"))
        self.chip_value.pack(side="left", padx=(0, 5))
        self.chip_mode = tk.Label(self.chip, text=AGG_MODES[0], bg=BG_DETAIL, fg=FG,
                                  font=FONT_SMALL)
        self.chip_mode.pack(side="left", padx=(0, 5))
        self.chip_subject = tk.Label(self.chip, text="", bg=BG_DETAIL, fg=FG_MUTED,
                                     font=FONT_SMALL)
        self.chip_subject.pack(side="left", padx=(0, 5))
        tk.Label(self.chip, text="▾", bg=BG_DETAIL, fg=FG_MUTED,
                 font=FONT_TINY).pack(side="left", padx=(0, 9))
        for widget in (self.chip, self.chip_dot, self.chip_value, self.chip_mode,
                       self.chip_subject):
            widget.bind("<Button-1>", self._cycle_agg)
            widget.bind("<Enter>", lambda _e: self._chip_bg("#262626"))
            widget.bind("<Leave>", lambda _e: self._chip_bg(BG_DETAIL))

    def _chip_bg(self, color: str) -> None:
        for widget in (self.chip, self.chip_value, self.chip_mode, self.chip_subject):
            widget.configure(bg=color)
        self.chip_dot.configure(bg=color)
        for child in self.chip.winfo_children():
            child.configure(bg=color)

    def _ghost_button(self, parent, text, command) -> tk.Widget:
        """Tk cannot colour a border, so wrap a button in a 1px frame."""
        border = tk.Frame(parent, bg=BG_TRACK)
        inner = tk.Button(
            border, text=text, command=command, bg=BG_BAR, fg=FG, font=FONT_SMALL,
            relief="flat", activebackground="#1c1c1c", activeforeground=FG,
            padx=11, pady=2, borderwidth=0, highlightthickness=0,
        )
        inner.pack(padx=1, pady=1)
        border.inner = inner
        return border

    def _segmented(self, parent, options, command) -> dict:
        frame = tk.Frame(parent, bg=BG_DETAIL)
        widgets = {"frame": frame, "labels": {}}
        for option in options:
            label = tk.Label(
                frame, text=option, bg=BG_DETAIL, fg=FG_DIM, font=FONT_SMALL,
                padx=10, pady=3,
            )
            label.pack(side="left")
            label.bind("<Button-1>", lambda _e, o=option: command(o))
            widgets["labels"][option] = label
        return widgets

    def _build_status_strip(self, parent) -> None:
        strip = tk.Frame(parent, bg=BG_STRIP)
        strip.pack(fill="x")
        self.strip_left = tk.Label(
            strip, text="", bg=BG_STRIP, fg=FG_MUTED, font=FONT_SMALL, anchor="w"
        )
        self.strip_left.pack(side="left", padx=PAD, pady=GAP)
        self.strip_right = tk.Label(
            strip, text="", bg=BG_STRIP, fg=FG_MUTED, font=FONT_SMALL, anchor="e"
        )
        self.strip_right.pack(side="right", padx=PAD, pady=GAP)

    # ---------------------------------------------------------------- board

    def _build_board(self, parent) -> None:
        # The drawer is packed first, against the bottom, so the board can
        # never push it off screen no matter how many accounts there are.
        self.drawer = Drawer(parent, self)
        self.drawer.frame.configure(height=DRAWER_HEIGHT)
        self.drawer.frame.pack(side="bottom", fill="x", padx=GAP, pady=(6, GAP))
        self.drawer.frame.pack_propagate(False)

        board = tk.Frame(parent, bg=BG)
        board.pack(side="top", fill="both", expand=True, padx=GAP, pady=(GAP, 0))

        canvas = tk.Canvas(board, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(board, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        item = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )
        inner.columnconfigure(0, weight=1, uniform="tile")
        inner.columnconfigure(1, weight=1, uniform="tile")

        self.canvas = canvas
        self.inner = inner

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
        else:
            self.usage_frame.pack_forget()
            self.accounts_frame.pack(fill="both", expand=True)
            self.refresh_button.inner.configure(
                text="Scan for profiles",
                command=lambda: self.accounts_tab and self.accounts_tab.scan(),
            )

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
        """Open on the account in the most trouble, else the first configured."""
        worst_label, worst_value = None, -1.0
        for account in self.poller.accounts:
            status = statuses.get(account.label)
            if status is None or status.snapshot is None:
                continue
            value = status.snapshot.worst_utilization
            if value is not None and value > worst_value:
                worst_label, worst_value = account.label, value
        if worst_label:
            return worst_label
        return self.poller.accounts[0].label

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
        """Two columns. Only runs on membership or sort changes, never on tick."""
        statuses = self.poller.statuses()
        for index, label in enumerate(self._ordered_labels(statuses)):
            tile = self._tiles.get(label)
            if tile is None:
                continue
            tile.shell.grid(
                row=index // 2, column=index % 2, sticky="nsew", padx=3, pady=3
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

        if not worsts:
            self.chip_value.configure(text="--")
            self.chip_subject.configure(text="no data")
            self.chip_dot.itemconfigure(self.chip_dot_id, fill=FG_MUTED)
            self.chip_mode.configure(text=AGG_MODES[self.agg_mode])
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
        self.chip_mode.configure(text=mode)
        self.chip_subject.configure(text=subject)
        self.chip_dot.itemconfigure(self.chip_dot_id, fill=color_for(tone))

    def _update_strip(self, statuses: dict) -> None:
        ages = [s.age_seconds for s in statuses.values() if s.age_seconds is not None]
        parts = []
        parts.append("polled %s" % (format_age(min(ages)) if ages else "never"))
        due = self.poller.next_due_seconds()
        if due is not None:
            parts.append("next in %s" % format_duration(due))
        problems = [
            "%s %s" % (label, _short_problem(status))
            for label, status in statuses.items()
            if status.error_kind is not None
        ]
        if problems:
            parts.append(" · ".join(problems[:2]))
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
