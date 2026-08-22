"""The detail window.

Two rules drive the structure:

1. **Never rebuild to refresh.** Tk has no double-buffering, so destroying and
   recreating the tree makes the whole window flash. Widgets are created once
   and their values updated in place; a structural rebuild happens only when
   the SET of keys an account reports actually changes, which is rare.

2. **Glanceable first, complete on demand.** Each account collapses to one row
   carrying the numbers you actually check. Everything the endpoint returned is
   still reachable -- click the row to expand it -- but it is not all on screen
   at once demanding a scroll.
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

BG = "#171717"
FG = "#ededed"
FG_DIM = "#8f8f8f"
FG_MUTED = "#6b6b6b"
BG_CARD = "#212121"
BG_CARD_HOVER = "#282828"
BG_TRACK = "#333333"
BG_DETAIL = "#1c1c1c"
CORAL = clawd.BODY_COLOR

FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_MONO = ("Consolas", 9)
FONT_SMALL = ("Segoe UI", 8)
FONT_SECTION = ("Segoe UI", 8, "bold")
FONT_TITLE = ("Segoe UI", 13, "bold")

PAD = 14
GAP = 8


def _style_notebook(widget) -> None:
    """ttk defaults to the native light theme; force the tab strip dark."""
    style = ttk.Style(widget)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Clau.TNotebook", background=BG, borderwidth=0, tabmargins=(6, 6, 6, 0)
    )
    style.configure(
        "Clau.TNotebook.Tab", background=BG, foreground=FG_DIM,
        padding=(14, 6), borderwidth=0, font=("Segoe UI", 9, "bold"),
    )
    style.map(
        "Clau.TNotebook.Tab",
        background=[("selected", BG_CARD)],
        foreground=[("selected", CORAL)],
    )


def _stamp(status) -> tuple[str, str]:
    """Freshness text and colour. Live and stale must never look alike."""
    if status.last_success is None:
        return "never updated", COLOR_RED
    if status.is_stale:
        # Drop the "ago" here; the word STALE already carries the meaning and
        # the column has to fit "never updated" too.
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


def _short_note(status) -> tuple[str, str]:
    """A failure tag short enough to sit on one row, or Clawd's read."""
    kind = status.error_kind
    if kind == ErrorKind.RATE_LIMIT:
        return "429 retry " + format_duration(status.backoff_remaining), COLOR_AMBER
    if kind == ErrorKind.AUTH:
        return "re-auth", COLOR_RED
    if kind == ErrorKind.NETWORK:
        return "offline", COLOR_AMBER
    if kind == ErrorKind.SHAPE:
        return "bad response", COLOR_AMBER
    if kind == ErrorKind.HTTP:
        return "HTTP %s" % (status.http_status or "error"), COLOR_RED
    if status.snapshot is None:
        return "no data yet", FG_MUTED
    return clawd.MOOD_CAPTIONS.get(
        clawd.mood_for(
            status.snapshot.worst_utilization, status.error_kind, status.is_stale
        ),
        "",
    ), CORAL


class Bar:
    """A progress bar that is updated, never recreated."""

    def __init__(self, parent, width: int = 38, height: int = 8, bg: str = BG_CARD):
        self.width = width
        self.height = height
        self.canvas = tk.Canvas(
            parent, width=width, height=height, bg=bg, highlightthickness=0
        )
        self.canvas.create_rectangle(0, 0, width, height, fill=BG_TRACK, outline="")
        self.fill = self.canvas.create_rectangle(
            0, 0, 0, height, fill=BG_TRACK, outline=""
        )

    def set(self, percent) -> None:
        if percent is None:
            self.canvas.coords(self.fill, 0, 0, 0, self.height)
            return
        filled = max(0.0, min(100.0, float(percent))) / 100.0 * self.width
        self.canvas.coords(self.fill, 0, 0, filled, self.height)
        self.canvas.itemconfigure(self.fill, fill=color_for(percent))

    def set_bg(self, color: str) -> None:
        self.canvas.configure(bg=color)


class MetricCell:
    """One "name  bar  pct" group on a collapsed row."""

    def __init__(self, parent, column: int):
        self.column = column
        self.frame = tk.Frame(parent, bg=BG_CARD)
        self.name = tk.Label(
            self.frame, text="", bg=BG_CARD, fg=FG_MUTED, font=FONT_SMALL,
            width=4, anchor="e",
        )
        self.name.pack(side="left", padx=(0, 5))
        self.bar = Bar(self.frame)
        self.bar.canvas.pack(side="left")
        self.value = tk.Label(
            self.frame, text="", bg=BG_CARD, fg=FG, font=FONT_BOLD,
            width=4, anchor="w",
        )
        self.value.pack(side="left", padx=(6, 0))

    def set(self, name: str, percent) -> None:
        self.name.configure(text=name)
        self.bar.set(percent)
        self.value.configure(text=format_percent(percent), fg=color_for(percent))

    def set_bg(self, color: str) -> None:
        for widget in (self.frame, self.name, self.value):
            widget.configure(bg=color)
        self.bar.set_bg(color)

    def show(self) -> None:
        self.frame.grid(row=0, column=self.column, sticky="w", padx=(0, GAP))

    def hide(self) -> None:
        self.frame.grid_remove()


class AccountCard:
    """One account: a collapsed summary row plus an expandable detail pane."""

    MAX_METRICS = 3

    def __init__(self, parent: tk.Widget, panel: "Panel", status):
        self.panel = panel
        self.label = status.label
        self.expanded = False
        self._detail_signature = None
        self._detail_updaters: list[tuple[tk.Widget, Any]] = []
        self._status = status

        self.shell = tk.Frame(parent, bg=BG)
        self.shell.pack(fill="x", padx=4, pady=2)

        self.spine = tk.Frame(self.shell, bg=BG_TRACK, width=3)
        self.spine.pack(side="left", fill="y")

        self.body = tk.Frame(self.shell, bg=BG_CARD)
        self.body.pack(side="left", fill="both", expand=True)

        self._build_header()
        self.detail = tk.Frame(self.body, bg=BG_DETAIL)  # packed only when open

    # ------------------------------------------------------------- collapsed

    def _build_header(self) -> None:
        # grid, not pack: with pack, the left group and the right group overlap
        # once the row runs out of width, silently mangling both.
        head = tk.Frame(self.body, bg=BG_CARD)
        head.pack(fill="x", padx=PAD, pady=5)
        # The weight goes on an EMPTY spacer column. Putting it on a content
        # column means grid shrinks that column first when the row is tight,
        # silently collapsing the text inside it to nothing.
        head.columnconfigure(6, weight=1)
        self.head = head

        self.chevron = tk.Label(
            head, text="▸", bg=BG_CARD, fg=FG_MUTED, font=("Segoe UI", 9)
        )
        self.chevron.grid(row=0, column=0, padx=(0, GAP))

        cell = 3
        width, height = clawd.sprite_size(cell)
        self.sprite = tk.Canvas(
            head, width=width, height=height, bg=BG_CARD, highlightthickness=0
        )
        self.sprite.grid(row=0, column=1, padx=(0, GAP + 2))
        self.panel.register_sprite(self.sprite, cell, self._mood)

        self.name = tk.Label(
            head, text=self.label, bg=BG_CARD, fg=FG, font=FONT_BOLD,
            width=10, anchor="w",
        )
        self.name.grid(row=0, column=2, sticky="w", padx=(0, GAP))

        self.metrics = [MetricCell(head, column=3 + i) for i in range(self.MAX_METRICS)]

        self.spacer = tk.Frame(head, bg=BG_CARD, height=1)
        self.spacer.grid(row=0, column=6, sticky="ew")

        self.note = tk.Label(
            head, text="", bg=BG_CARD, fg=CORAL, font=FONT_SMALL,
            width=12, anchor="e",
        )
        self.note.grid(row=0, column=7, sticky="e", padx=(GAP, PAD))

        self.stamp = tk.Label(
            head, text="", bg=BG_CARD, fg=FG_MUTED, font=FONT_SMALL,
            width=11, anchor="e",
        )
        self.stamp.grid(row=0, column=8, sticky="e")

        # The whole row is the click target, not just the chevron.
        for widget in (head, self.chevron, self.name, self.sprite):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)

    def _mood(self) -> str:
        status = self._status
        return clawd.mood_for(
            status.snapshot.worst_utilization if status.snapshot else None,
            status.error_kind,
            status.is_stale,
        )

    def _on_click(self, _event=None) -> None:
        self.toggle()

    def _on_enter(self, _event=None) -> None:
        self._set_row_bg(BG_CARD_HOVER)

    def _on_leave(self, _event=None) -> None:
        self._set_row_bg(BG_CARD)

    def _set_row_bg(self, color: str) -> None:
        for widget in (self.body, self.head, self.chevron, self.name,
                       self.stamp, self.note, self.spacer):
            widget.configure(bg=color)
        self.sprite.configure(bg=color)
        for metric in self.metrics:
            metric.set_bg(color)

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self.chevron.configure(text="▾" if self.expanded else "▸")
        if self.expanded:
            self.detail.pack(fill="x", pady=(0, 2))
            self._sync_detail(self._status, force=True)
        else:
            self.detail.pack_forget()
        self.panel.on_layout_changed()

    # ---------------------------------------------------------------- update

    def update(self, status) -> None:
        """In-place value refresh. Destroys nothing, so nothing flashes."""
        self._status = status

        worst = status.snapshot.worst_utilization if status.snapshot else None
        self.spine.configure(bg=color_for(worst))

        text, color = _stamp(status)
        self.stamp.configure(text=text, fg=color)

        metrics = status.snapshot.headline_metrics() if status.snapshot else []
        for i, cell in enumerate(self.metrics):
            if i < len(metrics):
                name, percent, _ = metrics[i]
                cell.set(name, percent)
                cell.show()
            else:
                cell.hide()

        note, note_color = _short_note(status)
        self.note.configure(text=note, fg=note_color)

        if self.expanded:
            self._sync_detail(status)

    # ---------------------------------------------------------------- detail

    @staticmethod
    def _signature(status) -> tuple:
        """What must change before the detail pane is worth rebuilding."""
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

    def _sync_detail(self, status, force: bool = False) -> None:
        signature = self._signature(status)
        if force or signature != self._detail_signature:
            self._build_detail(status)
            self._detail_signature = signature
            return
        for entry in list(self._detail_updaters):
            widget, produce = entry
            try:
                if not widget.winfo_exists():
                    self._detail_updaters.remove(entry)
                    continue
                widget.configure(**produce())
            except tk.TclError:
                pass

    def _build_detail(self, status) -> None:
        for child in self.detail.winfo_children():
            child.destroy()
        self._detail_updaters.clear()

        tools = tk.Frame(self.detail, bg=BG_DETAIL)
        tools.pack(fill="x", padx=PAD, pady=(GAP, 0))
        tk.Label(
            tools, text=str(status.config_dir), bg=BG_DETAIL, fg=FG_MUTED,
            font=FONT_SMALL, anchor="w",
        ).pack(side="left")
        tk.Button(
            tools, text="Copy raw JSON",
            command=lambda: self.panel.copy_raw(self.label),
            bg=BG_TRACK, fg=FG, font=FONT_SMALL, relief="flat",
            activebackground="#404040", activeforeground=FG, padx=8,
        ).pack(side="right")

        if status.error:
            tk.Label(
                self.detail, text="! " + _error_text(status), bg=BG_DETAIL,
                fg=COLOR_AMBER, font=FONT, anchor="w", justify="left",
                wraplength=1000,
            ).pack(fill="x", padx=PAD, pady=(4, 0))

        snapshot = status.snapshot
        if snapshot is None:
            if status.raw_text:
                tk.Label(
                    self.detail, text="Raw response (could not be interpreted):",
                    bg=BG_DETAIL, fg=FG_MUTED, font=FONT_SMALL, anchor="w",
                ).pack(fill="x", padx=PAD, pady=(GAP, 2))
                box = tk.Text(
                    self.detail, height=6, bg="#111111", fg=FG, font=FONT_MONO,
                    relief="flat", wrap="none",
                )
                box.insert("1.0", status.raw_text)
                box.configure(state="disabled")
                box.pack(fill="x", padx=PAD, pady=(0, GAP))
            return

        grid = tk.Frame(self.detail, bg=BG_DETAIL)
        grid.pack(fill="x", padx=PAD, pady=(GAP, GAP))
        grid.columnconfigure(5, weight=1)
        row = 0

        row = self._section(grid, row, "WINDOWS")
        for window in snapshot.windows:
            row = self._usage_row(
                grid, row, window.key, window.utilization, window.resets_at,
                self._extras(window.extras),
            )
        if not snapshot.windows:
            row = self._plain(grid, row, "endpoint returned no window objects")

        if snapshot.limits:
            row = self._section(grid, row, "LIMITS")
            for limit in snapshot.limits:
                bits = []
                if limit.severity and limit.severity != "normal":
                    bits.append(limit.severity)
                if limit.is_active:
                    bits.append("ACTIVE")
                if limit.group:
                    bits.append("group=" + limit.group)
                row = self._usage_row(
                    grid, row, limit.display_name, limit.percent, limit.resets_at,
                    "  ".join(bits), emphasize=limit.is_active,
                )

        for block in snapshot.blocks:
            row = self._section(grid, row, block.key.upper())
            for key, value in block.fields:
                row = self._kv(grid, row, key, value)

        if snapshot.scalars:
            row = self._section(grid, row, "OTHER")
            for key, value in snapshot.scalars:
                row = self._kv(grid, row, key, value)

        if snapshot.null_keys:
            row = self._section(grid, row, "REPORTED BUT NULL")
            tk.Label(
                grid, text=", ".join(snapshot.null_keys), bg=BG_DETAIL,
                fg=FG_MUTED, font=FONT_SMALL, wraplength=900,
                justify="left", anchor="w",
            ).grid(row=row, column=0, columnspan=6, sticky="w")
            row += 1

        names = snapshot.scoped_model_names
        note = (
            "Model-scoped caps reported: %s. Shown above under LIMITS with their "
            "own reset clock. The percentage is of that model's scoped cap, not "
            "of the whole weekly pool." % ", ".join(names)
            if names else
            "No model-scoped window was returned. Model usage (including Fable) "
            "sits inside the weekly figure and is not separately exposed here."
        )
        tk.Label(
            self.detail, text=note, bg=BG_DETAIL, fg=FG_MUTED, font=FONT_SMALL,
            wraplength=1000, justify="left", anchor="w",
        ).pack(fill="x", padx=PAD, pady=(0, GAP))

        self._build_sparkline(status)

    @staticmethod
    def _extras(extras: dict) -> str:
        if not extras:
            return ""
        return "  ".join("%s=%s" % (k, format_value(v)) for k, v in extras.items())

    @staticmethod
    def _section(grid, row: int, title: str) -> int:
        tk.Label(
            grid, text=title, bg=BG_DETAIL, fg=FG_MUTED, font=FONT_SECTION
        ).grid(row=row, column=0, columnspan=6, sticky="w", pady=(GAP, 2))
        return row + 1

    def _usage_row(
        self, grid, row, name, percent, resets_at, note="", emphasize=False
    ) -> int:
        tk.Label(
            grid, text=name, bg=BG_DETAIL, fg="#ffd479" if emphasize else FG,
            font=FONT_MONO, anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(0, GAP + 2))

        tk.Label(
            grid, text=format_percent(percent), bg=BG_DETAIL,
            fg=color_for(percent), font=FONT_BOLD, width=6, anchor="e",
        ).grid(row=row, column=1, sticky="e", padx=(0, GAP))

        bar = Bar(grid, width=110, bg=BG_DETAIL)
        bar.set(percent)
        bar.canvas.grid(row=row, column=2, padx=(0, GAP + 2))

        relative = tk.Label(
            grid, text=format_reset_relative(resets_at), bg=BG_DETAIL, fg=FG,
            font=FONT, anchor="w",
        )
        relative.grid(row=row, column=3, sticky="w", padx=(0, GAP + 4))
        self._detail_updaters.append(
            (relative, lambda r=resets_at: {"text": format_reset_relative(r)})
        )

        tk.Label(
            grid, text=format_reset_absolute(resets_at), bg=BG_DETAIL,
            fg=FG_MUTED, font=FONT, anchor="w",
        ).grid(row=row, column=4, sticky="w", padx=(0, GAP))

        if note:
            tk.Label(
                grid, text=note, bg=BG_DETAIL, fg=FG_MUTED, font=FONT_SMALL,
                anchor="w",
            ).grid(row=row, column=5, sticky="w")
        return row + 1

    @staticmethod
    def _kv(grid, row, key, value) -> int:
        tk.Label(
            grid, text=key, bg=BG_DETAIL, fg=FG_MUTED, font=FONT_MONO, anchor="w"
        ).grid(row=row, column=0, sticky="w", padx=(0, GAP + 2))
        tk.Label(
            grid, text=format_value(value), bg=BG_DETAIL, fg=FG, font=FONT,
            anchor="w",
        ).grid(row=row, column=1, columnspan=5, sticky="w")
        return row + 1

    @staticmethod
    def _plain(grid, row, text) -> int:
        tk.Label(
            grid, text=text, bg=BG_DETAIL, fg=FG_MUTED, font=FONT_SMALL, anchor="w"
        ).grid(row=row, column=0, columnspan=6, sticky="w")
        return row + 1

    def _build_sparkline(self, status) -> None:
        store = self.panel.store
        if store is None:
            return
        try:
            points = store.series(status.label, "seven_day", days=7)
        except Exception:
            return

        holder = tk.Frame(self.detail, bg=BG_DETAIL)
        holder.pack(fill="x", padx=PAD, pady=(0, PAD))
        tk.Label(
            holder, text="seven_day, last 7 days", bg=BG_DETAIL, fg=FG_MUTED,
            font=FONT_SMALL,
        ).pack(side="left", padx=(0, GAP))

        width, height = 280, 30
        canvas = tk.Canvas(
            holder, width=width, height=height, bg="#141414", highlightthickness=0
        )
        canvas.pack(side="left")

        if len(points) < 2:
            canvas.create_text(
                width / 2, height / 2,
                text="collecting history (%d sample%s)"
                     % (len(points), "" if len(points) == 1 else "s"),
                fill=FG_MUTED, font=FONT_SMALL,
            )
            return

        times = [p[0] for p in points]
        earliest = min(times)
        span = max(1, max(times) - earliest)
        coords = []
        for ts, value in points:
            x = (ts - earliest) / span * (width - 4) + 2
            y = height - 2 - (max(0.0, min(100.0, value)) / 100.0 * (height - 4))
            coords.extend([x, y])
        canvas.create_line(*coords, fill=color_for(points[-1][1]), width=2)

    def destroy(self) -> None:
        self.shell.destroy()


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
        self._cards: dict[str, AccountCard] = {}
        self._sprites: list[tuple[Any, int, Any]] = []
        self._frame = 0
        self._refresh_job = None
        self._anim_job = None

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
        # Stop both loops while hidden: a tray app must not burn CPU idle.
        for attr in ("_refresh_job", "_anim_job"):
            handle = getattr(self, attr)
            if handle is not None:
                self.root.after_cancel(handle)
                setattr(self, attr, None)

    def register_sprite(self, canvas, cell, mood_of) -> None:
        self._sprites.append((canvas, cell, mood_of))

    def on_layout_changed(self) -> None:
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def copy_raw(self, label: str) -> None:
        status = self.poller.status(label)
        text = (status.raw_text if status else None) or "(no response captured yet)"
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _build_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.title(APP_NAME)
        win.configure(bg=BG)
        win.geometry("1180x520")
        win.minsize(720, 240)
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
                accounts_host, text="Account management unavailable here.",
                bg=BG, fg=FG_DIM, font=FONT,
            ).pack(padx=PAD, pady=PAD)

        header = tk.Frame(usage_tab, bg=BG)
        header.pack(fill="x", padx=PAD, pady=(GAP, 4))

        cell = 5
        width, height = clawd.sprite_size(cell)
        lead = tk.Canvas(
            header, width=width, height=height, bg=BG, highlightthickness=0
        )
        lead.pack(side="left", padx=(0, GAP + 2))
        self.register_sprite(lead, cell, self._worst_mood)

        tk.Label(header, text=APP_NAME, bg=BG, fg=FG, font=FONT_TITLE).pack(
            side="left"
        )

        tk.Button(
            header, text="Refresh now", command=self.poller.request_refresh,
            bg=BG_CARD, fg=FG, font=FONT_SMALL, relief="flat",
            activebackground=BG_TRACK, activeforeground=FG, padx=10, pady=2,
        ).pack(side="right")

        self.totals = tk.Label(
            header, text="", bg=BG, fg=FG_DIM, font=FONT_SMALL, anchor="e"
        )
        self.totals.pack(side="right", padx=(0, PAD))

        body = tk.Frame(usage_tab, bg=BG)
        body.pack(fill="both", expand=True, padx=GAP, pady=(0, GAP))
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
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

        tk.Label(
            usage_tab,
            text="Read-only. No inference calls, no writes to any Claude config "
                 "directory, no account switching. Undocumented endpoint.",
            bg=BG, fg=FG_MUTED, font=FONT_SMALL, anchor="w",
        ).pack(fill="x", padx=PAD, pady=(0, GAP))

        self.window = win
        self.canvas = canvas
        self.inner = inner

    # --------------------------------------------------------------- refresh

    def refresh(self) -> None:
        if self.window is None or not self.window.winfo_exists():
            self._refresh_job = None
            return

        statuses = self.poller.statuses()
        self._sync_cards(statuses)
        for label, card in self._cards.items():
            status = statuses.get(label)
            if status is not None:
                card.update(status)
        self.totals.configure(text=self._totals_line(statuses))

        self._refresh_job = self.root.after(1000, self.refresh)

    def _sync_cards(self, statuses: dict) -> None:
        """Add and remove cards only. Existing cards are never recreated."""
        wanted = [a.label for a in self.poller.accounts if a.label in statuses]

        for label in list(self._cards):
            if label not in wanted:
                self._cards.pop(label).destroy()

        added = False
        for label in wanted:
            if label not in self._cards:
                self._cards[label] = AccountCard(self.inner, self, statuses[label])
                added = True

        if added:
            # Re-pack in configured order. Only runs when the set changed, so
            # it cannot cause a flash during ordinary refreshes.
            for label in wanted:
                self._cards[label].shell.pack_forget()
                self._cards[label].shell.pack(fill="x", padx=4, pady=2)

    def _totals_line(self, statuses: dict) -> str:
        """Display only. This app never acts on which account has headroom."""
        headroom = []
        for label, status in statuses.items():
            if status.snapshot is None:
                continue
            window = status.snapshot.window("five_hour")
            if window and window.headroom is not None:
                headroom.append((label, window.headroom))
        if not headroom:
            return "no successful polls yet"
        best, free = max(headroom, key=lambda x: x[1])
        return "most 5h headroom: %s (%.0f%% free)   |   %d accounts" % (
            best, free, len(headroom)
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
