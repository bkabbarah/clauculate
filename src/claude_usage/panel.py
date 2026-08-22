"""The detail window.

Design rule: everything the endpoint returned is on screen. No truncation, no
collapsing, no disclosure toggles. The window scrolls and resizes instead.
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import Any

from .formatting import (
    COLOR_AMBER,
    COLOR_GREEN,
    COLOR_GREY,
    COLOR_RED,
    color_for,
    format_age,
    format_percent,
    format_reset_absolute,
    format_reset_relative,
    format_value,
)
from . import clawd
from .accounts_tab import AccountsTab
from .poller import ErrorKind, Poller

APP_NAME = "Clauculate"

BG = "#171717"
FG = "#ededed"
FG_DIM = "#8f8f8f"
FG_MUTED = "#6b6b6b"
BG_CARD = "#212121"
BG_TRACK = "#333333"
CORAL = clawd.BODY_COLOR  # Clawd's shell, used as the accent throughout
RULE = "#2e2e2e"

# One spacing scale, used everywhere, so nothing looks hand-placed.
PAD = 14
GAP = 8

FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_MONO = ("Consolas", 9)
FONT_SMALL = ("Segoe UI", 8)
FONT_SECTION = ("Segoe UI", 8, "bold")
FONT_TITLE = ("Segoe UI", 13, "bold")


def _style_notebook(widget) -> None:
    """ttk defaults to the native light theme; force the tab strip dark."""
    style = ttk.Style(widget)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Clau.TNotebook", background=BG, borderwidth=0, tabmargins=(6, 6, 6, 0))
    style.configure(
        "Clau.TNotebook.Tab", background=BG, foreground=FG_DIM,
        padding=(14, 6), borderwidth=0, font=("Segoe UI", 9, "bold"),
    )
    style.map(
        "Clau.TNotebook.Tab",
        background=[("selected", BG_CARD)],
        foreground=[("selected", CORAL)],
    )


def _stamp_kwargs(status) -> dict:
    """Freshness stamp. Live data and stale data must never look alike."""
    if status.last_success is None:
        return {"text": "never updated   ", "fg": COLOR_RED}
    age = format_age(status.age_seconds)
    if status.is_stale:
        return {"text": "STALE - " + age + "   ", "fg": COLOR_AMBER}
    return {"text": "updated " + age + "   ", "fg": FG_DIM}


def _error_text(status) -> str:
    if status.error_kind != ErrorKind.RATE_LIMIT:
        return status.error or ""
    from .formatting import format_duration

    text = "rate limited (HTTP 429) - retrying in %s" % format_duration(
        status.backoff_remaining
    )
    if status.snapshot is not None:
        text += "   [figures below are from the last good poll, not current]"
    return text


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
        self.inner: tk.Frame | None = None
        self._auto_refresh_job = None
        # Widgets whose text is time-derived, refreshed in place each second so
        # the tree is not torn down (which would flicker and reset scrolling).
        self._tickers: list[tuple[tk.Widget, Any]] = []
        self._signature: tuple | None = None
        # (canvas, cell_size, mood_provider) for every Clawd on screen.
        self._sprites: list[tuple[Any, int, Any]] = []
        self._frame = 0
        self._anim_job = None

    # ----------------------------------------------------------- window mgmt

    def show(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
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
        # Stop both loops while hidden: a tray app must not burn CPU idle.
        if self._auto_refresh_job is not None:
            self.root.after_cancel(self._auto_refresh_job)
            self._auto_refresh_job = None
        if self._anim_job is not None:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None

    def _build_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title(APP_NAME)
        win.configure(bg=BG)
        win.geometry("1180x780")
        win.minsize(640, 320)
        win.protocol("WM_DELETE_WINDOW", self.hide)

        _style_notebook(win)
        notebook = ttk.Notebook(win, style="Clau.TNotebook")
        notebook.pack(fill="both", expand=True)

        usage_tab = tk.Frame(notebook, bg=BG)
        accounts_host = tk.Frame(notebook, bg=BG)
        notebook.add(usage_tab, text="  Usage  ")
        notebook.add(accounts_host, text="  Accounts  ")
        self.notebook = notebook

        if self.app_state is not None:
            self.accounts_tab = AccountsTab(accounts_host, self.app_state)
            self.accounts_tab.frame.pack(fill="both", expand=True)
        else:
            tk.Label(
                accounts_host,
                text="Account management is unavailable in this context.",
                bg=BG, fg=FG_DIM, font=FONT,
            ).pack(padx=PAD, pady=PAD)

        # Header stays put; only the account list scrolls. Two stacked rows so
        # the totals line can never collide with the buttons.
        header = tk.Frame(usage_tab, bg=BG)
        header.pack(fill="x", padx=12, pady=(10, 4))
        self.header_top = tk.Frame(header, bg=BG)
        self.header_top.pack(fill="x")
        self.header_bottom = tk.Frame(header, bg=BG)
        self.header_bottom.pack(fill="x")
        self.header = header

        body = tk.Frame(usage_tab, bg=BG)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        hbar = ttk.Scrollbar(usage_tab, orient="horizontal", command=canvas.xview)
        inner = tk.Frame(canvas, bg=BG)

        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x", before=body)

        def _on_inner_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            # Fill the viewport when there is room, but never squeeze content
            # narrower than it needs -- the h-scrollbar handles the overflow so
            # nothing is ever clipped.
            natural = inner.winfo_reqwidth()
            canvas.itemconfigure(window_id, width=max(event.width, natural))

        inner.bind("<Configure>", _on_inner_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self.window = win
        self.canvas = canvas
        self.inner = inner

    # -------------------------------------------------------------- refresh

    def _structure_signature(self, statuses: dict) -> tuple:
        """What must change for a full rebuild to be warranted.

        Deliberately excludes anything derived from the clock, so a rebuild
        happens on a new poll -- roughly every 180s -- not every second.
        """
        parts = []
        for account in self.poller.accounts:
            status = statuses.get(account.label)
            if status is None:
                parts.append((account.label, None))
                continue
            snapshot = status.snapshot
            parts.append(
                (
                    account.label,
                    status.error_kind,
                    status.last_success,
                    status.http_status,
                    status.backoff_index,
                    tuple(w.key for w in snapshot.windows) if snapshot else (),
                    tuple(x.display_name for x in snapshot.limits) if snapshot else (),
                    tuple(b.key for b in snapshot.blocks) if snapshot else (),
                    tuple(snapshot.null_keys) if snapshot else (),
                )
            )
        return tuple(parts)

    def refresh(self) -> None:
        if self.window is None or not self.window.winfo_exists():
            return

        statuses = self.poller.statuses()
        signature = self._structure_signature(statuses)

        if signature != self._signature:
            self._rebuild(statuses)
            self._signature = signature
        else:
            self._tick()

        if self._auto_refresh_job is not None:
            self.root.after_cancel(self._auto_refresh_job)
        # Tick once a second so relative times and backoff countdowns stay live.
        self._auto_refresh_job = self.root.after(1000, self.refresh)

    def _animate(self) -> None:
        """Drive every Clawd. ~8 fps is plenty for pixel art and costs little."""
        if self.window is None or not self.window.winfo_exists():
            self._anim_job = None
            return
        self._frame += 1
        for canvas, cell, mood_of in list(self._sprites):
            try:
                if not canvas.winfo_exists():
                    self._sprites.remove((canvas, cell, mood_of))
                    continue
                clawd.draw_clawd(canvas, self._frame, mood_of(), cell=cell)
            except tk.TclError:
                pass
        self._anim_job = self.root.after(125, self._animate)

    def _tick(self) -> None:
        """Update only the time-derived text. No widgets are destroyed."""
        for widget, produce in list(self._tickers):
            try:
                if not widget.winfo_exists():
                    self._tickers.remove((widget, produce))
                    continue
                widget.configure(**produce())
            except tk.TclError:
                pass

    def _rebuild(self, statuses: dict) -> None:
        # Preserve the viewport so a poll landing does not yank the user back
        # to the top of the list.
        try:
            y_pos = self.canvas.yview()[0]
            x_pos = self.canvas.xview()[0]
        except (AttributeError, tk.TclError):
            y_pos = x_pos = 0.0

        self._tickers.clear()
        self._sprites.clear()
        for child in self.header_top.winfo_children():
            child.destroy()
        for child in self.header_bottom.winfo_children():
            child.destroy()
        for child in self.inner.winfo_children():
            child.destroy()

        self._build_header(statuses)
        for account in self.poller.accounts:
            status = statuses.get(account.label)
            if status is not None:
                self._build_account(self.inner, status)
        self._build_footer(self.inner)

        self.canvas.update_idletasks()
        self.canvas.yview_moveto(y_pos)
        self.canvas.xview_moveto(x_pos)

    # --------------------------------------------------------------- header

    def _build_header(self, statuses: dict) -> None:
        # A larger Clawd reflecting the worst account, so the headline state is
        # readable from across the room.
        cell = 6
        w, h = clawd.sprite_size(cell)
        lead = tk.Canvas(
            self.header_top, width=w, height=h, bg=BG, highlightthickness=0
        )
        lead.pack(side="left", padx=(0, GAP + 4))
        self._sprites.append((lead, cell, self._worst_mood))
        clawd.draw_clawd(lead, self._frame, self._worst_mood(), cell=cell)

        tk.Label(
            self.header_top,
            text=APP_NAME,
            bg=BG, fg=FG, font=FONT_TITLE,
        ).pack(side="left")

        tk.Button(
            self.header_top, text="Refresh now", command=self._request_refresh,
            bg=BG_CARD, fg=FG, font=FONT_SMALL, relief="flat",
            activebackground=BG_TRACK, activeforeground=FG, padx=8,
        ).pack(side="right")

        tk.Label(
            self.header_bottom, text=self._totals_line(statuses), bg=BG, fg=FG_DIM,
            font=FONT_SMALL, justify="left", anchor="w",
        ).pack(fill="x", pady=(4, 0))

    def _worst_mood(self) -> str:
        """Mood of the account in the most trouble, failures included."""
        worst_mood = clawd.MOOD_FRESH
        rank = {
            clawd.MOOD_FRESH: 0, clawd.MOOD_HEALTHY: 1, clawd.MOOD_BUSY: 2,
            clawd.MOOD_SLEEPING: 3, clawd.MOOD_STRAINED: 4,
            clawd.MOOD_CRITICAL: 5, clawd.MOOD_SAD: 6,
        }
        for status in self.poller.statuses().values():
            mood = clawd.mood_for(
                status.snapshot.worst_utilization if status.snapshot else None,
                status.error_kind,
                status.is_stale,
            )
            if rank.get(mood, 0) > rank.get(worst_mood, 0):
                worst_mood = mood
        return worst_mood

    def _totals_line(self, statuses: dict) -> str:
        """Display only. This app never acts on which account has headroom."""
        five_hour = []
        weekly = []
        for label, status in statuses.items():
            snap = status.snapshot
            if snap is None:
                continue
            window = snap.window("five_hour")
            if window and window.headroom is not None:
                five_hour.append((label, window.headroom))
            weekly_window = snap.window("seven_day")
            if weekly_window and weekly_window.headroom is not None:
                weekly.append((label, weekly_window.headroom))

        if not five_hour:
            return "No successful polls yet - totals unavailable."

        total = sum(h for _, h in five_hour)
        avg = total / len(five_hour)
        best_label, best_headroom = max(five_hour, key=lambda x: x[1])

        parts = [
            "Combined 5h headroom: %.0f%% across %d accounts (avg %.0f%% each)"
            % (total, len(five_hour), avg),
            "Most 5h headroom: %s (%.0f%% free)" % (best_label, best_headroom),
        ]
        if weekly:
            weekly_total = sum(h for _, h in weekly)
            parts.append(
                "Combined weekly headroom: %.0f%% across %d"
                % (weekly_total, len(weekly))
            )
        return "   |   ".join(parts)

    def _request_refresh(self) -> None:
        self.poller.request_refresh()

    # -------------------------------------------------------------- account

    def _build_account(self, parent: tk.Frame, status) -> None:
        worst = status.snapshot.worst_utilization if status.snapshot else None

        # A coloured spine on the left edge gives each card a state read even
        # before you parse any text.
        shell = tk.Frame(parent, bg=BG, bd=0)
        shell.pack(fill="x", expand=True, padx=4, pady=GAP // 2)
        tk.Frame(shell, bg=color_for(worst), width=3).pack(side="left", fill="y")
        card = tk.Frame(shell, bg=BG_CARD, bd=0)
        card.pack(side="left", fill="both", expand=True)

        # ---- account header line
        head = tk.Frame(card, bg=BG_CARD)
        head.pack(fill="x", padx=PAD, pady=(GAP, 2))

        mood = clawd.mood_for(worst, status.error_kind, status.is_stale)
        cell = 4
        sprite_w, sprite_h = clawd.sprite_size(cell)
        sprite = tk.Canvas(
            head, width=sprite_w, height=sprite_h, bg=BG_CARD, highlightthickness=0
        )
        sprite.pack(side="left", padx=(0, GAP + 2))
        clawd.draw_clawd(sprite, self._frame, mood, cell=cell)
        # Re-read the mood each frame so the pose follows the live numbers.
        self._sprites.append(
            (
                sprite,
                cell,
                lambda s=status: clawd.mood_for(
                    s.snapshot.worst_utilization if s.snapshot else None,
                    s.error_kind,
                    s.is_stale,
                ),
            )
        )

        tk.Label(
            head, text=status.label, bg=BG_CARD, fg=FG, font=FONT_BOLD
        ).pack(side="left")

        caption = tk.Label(
            head, text="  " + clawd.MOOD_CAPTIONS.get(mood, ""), bg=BG_CARD,
            fg=CORAL, font=FONT_SMALL,
        )
        caption.pack(side="left")
        self._tickers.append(
            (
                caption,
                lambda s=status: {
                    "text": "  " + clawd.MOOD_CAPTIONS.get(
                        clawd.mood_for(
                            s.snapshot.worst_utilization if s.snapshot else None,
                            s.error_kind,
                            s.is_stale,
                        ),
                        "",
                    )
                },
            )
        )

        meta = []
        if status.subscription_type:
            meta.append(status.subscription_type)
        if status.rate_limit_tier:
            meta.append(status.rate_limit_tier)
        if meta:
            tk.Label(
                head, text="   " + " / ".join(meta), bg=BG_CARD, fg=FG_MUTED,
                font=FONT_SMALL,
            ).pack(side="left")

        tk.Button(
            head, text="Copy raw JSON",
            command=lambda s=status: self._copy_raw(s),
            bg=BG_TRACK, fg=FG, font=FONT_SMALL, relief="flat",
            activebackground=BG_TRACK, activeforeground=FG, padx=6,
        ).pack(side="right")

        # Staleness is stated explicitly so live and stale never look alike.
        stamp = tk.Label(head, bg=BG_CARD, font=FONT_SMALL, **_stamp_kwargs(status))
        stamp.pack(side="right")
        self._tickers.append((stamp, lambda s=status: _stamp_kwargs(s)))

        tk.Label(
            card, text=str(status.config_dir), bg=BG_CARD, fg=FG_DIM,
            font=FONT_SMALL, anchor="w",
        ).pack(fill="x", padx=PAD)

        if status.error:
            self._build_error(card, status)

        if status.snapshot is None:
            if status.raw_text:
                self._build_raw_fallback(card, status)
            return

        snapshot = status.snapshot
        grid = tk.Frame(card, bg=BG_CARD)
        grid.pack(fill="x", padx=PAD, pady=(GAP, 4))
        grid.columnconfigure(5, weight=1)

        row = 0
        row = self._section(grid, row, "WINDOWS")
        for window in snapshot.windows:
            row = self._usage_row(
                grid, row,
                name=window.key,
                percent=window.utilization,
                resets_at=window.resets_at,
                note=self._extras_note(window.extras),
            )
        if not snapshot.windows:
            row = self._note_row(grid, row, "endpoint returned no window objects")

        if snapshot.limits:
            row = self._section(grid, row, "LIMITS")
            for limit in snapshot.limits:
                suffix = []
                if limit.severity and limit.severity != "normal":
                    suffix.append(limit.severity)
                if limit.is_active:
                    suffix.append("ACTIVE")
                if limit.group:
                    suffix.append("group=" + limit.group)
                row = self._usage_row(
                    grid, row,
                    name=limit.display_name,
                    percent=limit.percent,
                    resets_at=limit.resets_at,
                    note="  ".join(suffix),
                    emphasize=limit.is_active,
                )

        for block in snapshot.blocks:
            row = self._section(grid, row, block.key.upper())
            for key, value in block.fields:
                row = self._kv_row(grid, row, key, value)

        if snapshot.scalars:
            row = self._section(grid, row, "OTHER")
            for key, value in snapshot.scalars:
                row = self._kv_row(grid, row, key, value)

        if snapshot.null_keys:
            row = self._section(grid, row, "REPORTED BUT NULL")
            tk.Label(
                grid,
                text=", ".join(snapshot.null_keys),
                bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL,
                wraplength=820, justify="left", anchor="w",
            ).grid(row=row, column=0, columnspan=6, sticky="w", pady=(0, 4))
            row += 1

        self._build_fable_note(card, snapshot)
        self._build_sparkline(card, status)

    def _extras_note(self, extras: dict) -> str:
        if not extras:
            return ""
        return "  ".join("%s=%s" % (k, format_value(v)) for k, v in extras.items())

    # ------------------------------------------------------------- row kinds

    def _section(self, grid: tk.Frame, row: int, title: str) -> int:
        tk.Label(
            grid, text=title, bg=BG_CARD, fg=FG_MUTED, font=FONT_SECTION
        ).grid(row=row, column=0, columnspan=6, sticky="w", pady=(8, 2))
        return row + 1

    def _usage_row(
        self, grid: tk.Frame, row: int, name: str, percent, resets_at,
        note: str = "", emphasize: bool = False,
    ) -> int:
        fg = FG if not emphasize else "#ffd479"
        tk.Label(
            grid, text=name, bg=BG_CARD, fg=fg, font=FONT_MONO, anchor="w"
        ).grid(row=row, column=0, sticky="w", padx=(0, 10))

        tk.Label(
            grid, text=format_percent(percent), bg=BG_CARD, fg=color_for(percent),
            font=FONT_BOLD, width=6, anchor="e",
        ).grid(row=row, column=1, sticky="e", padx=(0, 8))

        self._bar(grid, percent).grid(row=row, column=2, padx=(0, 10))

        relative = tk.Label(
            grid, text=format_reset_relative(resets_at), bg=BG_CARD, fg=FG,
            font=FONT, anchor="w",
        )
        relative.grid(row=row, column=3, sticky="w", padx=(0, 12))
        self._tickers.append(
            (relative, lambda r=resets_at: {"text": format_reset_relative(r)})
        )

        tk.Label(
            grid, text=format_reset_absolute(resets_at), bg=BG_CARD, fg=FG_DIM,
            font=FONT, anchor="w",
        ).grid(row=row, column=4, sticky="w", padx=(0, 12))

        if note:
            tk.Label(
                grid, text=note, bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL, anchor="w"
            ).grid(row=row, column=5, sticky="w", padx=(8, 0))
        return row + 1

    def _kv_row(self, grid: tk.Frame, row: int, key: str, value) -> int:
        tk.Label(
            grid, text=key, bg=BG_CARD, fg=FG_DIM, font=FONT_MONO, anchor="w"
        ).grid(row=row, column=0, sticky="w", padx=(0, 10))
        tk.Label(
            grid, text=format_value(value), bg=BG_CARD, fg=FG, font=FONT, anchor="w"
        ).grid(row=row, column=1, columnspan=5, sticky="w")
        return row + 1

    def _note_row(self, grid: tk.Frame, row: int, text: str) -> int:
        tk.Label(
            grid, text=text, bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL, anchor="w"
        ).grid(row=row, column=0, columnspan=6, sticky="w")
        return row + 1

    def _bar(self, parent: tk.Frame, percent, width: int = 140, height: int = 11):
        canvas = tk.Canvas(
            parent, width=width, height=height, bg=BG_CARD, highlightthickness=0
        )
        canvas.create_rectangle(0, 0, width, height, fill=BG_TRACK, outline="")
        if percent is not None:
            filled = max(0.0, min(100.0, float(percent))) / 100.0 * width
            if filled > 0:
                canvas.create_rectangle(
                    0, 0, filled, height, fill=color_for(percent), outline=""
                )
        return canvas

    # -------------------------------------------------------------- extras

    def _build_error(self, card: tk.Frame, status) -> None:
        color = {
            ErrorKind.AUTH: COLOR_RED,
            ErrorKind.RATE_LIMIT: COLOR_AMBER,
            ErrorKind.NETWORK: COLOR_AMBER,
            ErrorKind.SHAPE: COLOR_AMBER,
            ErrorKind.HTTP: COLOR_RED,
        }.get(status.error_kind, COLOR_RED)

        banner = tk.Frame(card, bg=BG_CARD)
        banner.pack(fill="x", padx=PAD, pady=(4, 0))
        label = tk.Label(
            banner, text="! " + _error_text(status), bg=BG_CARD, fg=color,
            font=FONT, anchor="w", justify="left", wraplength=1100,
        )
        label.pack(fill="x")
        if status.error_kind == ErrorKind.RATE_LIMIT:
            self._tickers.append(
                (label, lambda s=status: {"text": "! " + _error_text(s)})
            )

    def _build_raw_fallback(self, card: tk.Frame, status) -> None:
        """Unexpected shape: show the response rather than crash or show zeros."""
        tk.Label(
            card, text="Raw response (could not be interpreted):",
            bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL, anchor="w",
        ).pack(fill="x", padx=PAD, pady=(GAP, 2))
        box = tk.Text(
            card, height=8, bg="#111111", fg=FG, font=FONT_MONO,
            relief="flat", wrap="none",
        )
        box.insert("1.0", status.raw_text or "")
        box.configure(state="disabled")
        box.pack(fill="x", padx=PAD, pady=(0, GAP))

    def _build_fable_note(self, card: tk.Frame, snapshot) -> None:
        names = snapshot.scoped_model_names
        if names:
            text = (
                "Model-scoped caps reported by the endpoint: %s. "
                "These are shown above as their own rows under LIMITS, with their "
                "own reset clock. The percentage is of that model's scoped cap, "
                "not of the whole weekly pool." % ", ".join(names)
            )
        else:
            text = (
                "No model-scoped window was returned for this account. Model usage "
                "(including Fable) sits inside the weekly figure above and is not "
                "separately exposed by this endpoint."
            )
        tk.Label(
            card, text=text, bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL,
            wraplength=880, justify="left", anchor="w",
        ).pack(fill="x", padx=PAD, pady=(GAP, GAP))

    def _build_sparkline(self, card: tk.Frame, status) -> None:
        if self.store is None:
            return
        try:
            points = self.store.series(status.label, "seven_day", days=7)
        except Exception:
            return

        holder = tk.Frame(card, bg=BG_CARD)
        holder.pack(fill="x", padx=PAD, pady=(0, PAD))
        tk.Label(
            holder, text="seven_day, last 7 days", bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL
        ).pack(side="left", padx=(0, 8))

        width, height = 300, 34
        canvas = tk.Canvas(
            holder, width=width, height=height, bg="#1b1b1b", highlightthickness=0
        )
        canvas.pack(side="left")

        if len(points) < 2:
            canvas.create_text(
                width / 2, height / 2,
                text="collecting history (%d sample%s so far)"
                     % (len(points), "" if len(points) == 1 else "s"),
                fill=FG_DIM, font=FONT_SMALL,
            )
            return

        times = [p[0] for p in points]
        t_min, t_max = min(times), max(times)
        span = max(1, t_max - t_min)
        coords = []
        for ts, value in points:
            x = (ts - t_min) / span * (width - 4) + 2
            y = height - 2 - (max(0.0, min(100.0, value)) / 100.0 * (height - 4))
            coords.extend([x, y])
        canvas.create_line(*coords, fill=color_for(points[-1][1]), width=2, smooth=False)

    def _copy_raw(self, status) -> None:
        text = status.raw_text
        if not text:
            text = "(no response captured yet for %s)" % status.label
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # --------------------------------------------------------------- footer

    def _build_footer(self, parent: tk.Frame) -> None:
        tk.Label(
            parent,
            text=(
                "Read-only telemetry. This app never makes inference calls, never "
                "writes to any Claude config directory, and never selects or routes "
                "between accounts. Endpoint is undocumented and may change without "
                "notice."
            ),
            bg=BG, fg=FG_DIM, font=FONT_SMALL,
            wraplength=900, justify="left", anchor="w",
        ).pack(fill="x", padx=8, pady=(10, 4))
