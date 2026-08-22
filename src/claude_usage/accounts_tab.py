"""The Accounts tab: discover profiles, enroll them, spot duplicates.

Boundary that matters: this tab never logs anyone in. It cannot -- signing in
means handling a password and writing a credential file, which this app does
not do. What it does is remove the tedium around the login: it works out the
right command, hands it to you, and can open a terminal with CLAUDE_CONFIG_DIR
already set. You run the login; Claude Code writes the credentials; this tab
notices the new profile on the next scan.

It only ever writes accounts.json, which is this app's own config.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any

from . import clawd, scale
from .profile import discover, suggest_labels

BG = "#171717"
FG = "#ededed"
FG_DIM = "#8f8f8f"
FG_MUTED = "#6b6b6b"
BG_CARD = "#212121"
BG_TRACK = "#333333"
CORAL = clawd.BODY_COLOR
OK = "#2e9e4f"
WARN = "#d99100"
BAD = "#cc3333"

# Design px, resolved through scale.py at widget-creation time.
def FONT():        return scale.font(12)
def FONT_BOLD():   return scale.font(13, bold=True)
def FONT_MONO():   return scale.font(12, mono=True)
def FONT_SMALL():  return scale.font(11)
def FONT_SECTION():return scale.font(10, bold=True)
def PAD():         return scale.px(14)
def GAP():         return scale.px(8)


IS_WINDOWS = sys.platform.startswith("win")

# On Windows use claude.cmd: PowerShell's execution policy blocks the .ps1 shim
# on a default install, and the .cmd shim is not subject to it.
CLI = "claude.cmd" if IS_WINDOWS else "claude"


def login_command(config_dir: Path, email: str = "you@example.com") -> str:
    """The exact command to enroll one account, for this platform's shell."""
    if IS_WINDOWS:
        return (
            '$env:CLAUDE_CONFIG_DIR = "%s"; '
            "%s auth login --email %s; "
            "%s auth status --text"
        ) % (config_dir, CLI, email, CLI)
    return (
        'export CLAUDE_CONFIG_DIR="%s" && '
        "%s auth login --email %s && "
        "%s auth status --text"
    ) % (config_dir, CLI, email, CLI)


class AccountsTab:
    def __init__(self, parent: tk.Widget, app_state: Any):
        """app_state supplies: accounts_path, user_agent, on_accounts_changed."""
        self.state = app_state
        self.frame = tk.Frame(parent, bg=BG)
        self.entries: list = []
        self.labels: dict[str, str] = {}
        self._scanning = False
        self.editing: str | None = None
        self._build()

    # ------------------------------------------------------------------ build

    def _build(self) -> None:
        # The app bar carries the title and the Scan button, so this tab does
        # not repeat them.
        self.scan_button = None

        self.status = tk.Label(
            self.frame,
            text="Scan to find Claude profiles on this machine.",
            bg=BG, fg=FG_DIM, font=FONT_SMALL(), anchor="w", justify="left",
        )
        self.status.pack(fill="x", padx=PAD(), pady=(PAD(), GAP()))

        self.banner_host = tk.Frame(self.frame, bg=BG)
        self.banner_host.pack(fill="x")

        # --- scrollable list of discovered profiles
        body = tk.Frame(self.frame, bg=BG)
        body.pack(fill="both", expand=True, padx=scale.px(10), pady=GAP())
        canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(
            body, orient="vertical", command=canvas.yview,
            style="Clau.Vertical.TScrollbar",
        )
        inner = tk.Frame(canvas, bg=BG)
        item = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(item, width=e.width),
        )
        self.list_frame = inner
        self.canvas = canvas

        self._build_add_section()

    def _build_add_section(self) -> None:
        box = tk.Frame(self.frame, bg=BG_CARD)
        box.pack(fill="x", padx=PAD(), pady=(0, PAD()))

        tk.Label(
            box, text="ADD A NEW ACCOUNT", bg=BG_CARD, fg=FG_MUTED, font=FONT_SECTION()
        ).pack(anchor="w", padx=PAD(), pady=(GAP(), 2))

        tk.Label(
            box,
            text=(
                "This app cannot sign you in -- that needs your password, which it "
                "never handles. It prepares the command; you run the login and "
                "approve it in the browser."
            ),
            bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL(),
            wraplength=900, justify="left", anchor="w",
        ).pack(fill="x", padx=PAD())

        form = tk.Frame(box, bg=BG_CARD)
        form.pack(fill="x", padx=PAD(), pady=GAP())

        tk.Label(
            form, text="Profile folder", bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL()
        ).grid(row=0, column=0, sticky="w", padx=(0, GAP()))
        self.dir_var = tk.StringVar(value=".claude-new")
        tk.Entry(
            form, textvariable=self.dir_var, width=26, bg="#141414", fg=FG,
            insertbackground=FG, relief="flat", font=FONT_MONO(),
        ).grid(row=0, column=1, sticky="w", padx=(0, PAD()))

        tk.Label(
            form, text="Email", bg=BG_CARD, fg=FG_DIM, font=FONT_SMALL()
        ).grid(row=0, column=2, sticky="w", padx=(0, GAP()))
        self.email_var = tk.StringVar(value="")
        tk.Entry(
            form, textvariable=self.email_var, width=30, bg="#141414", fg=FG,
            insertbackground=FG, relief="flat", font=FONT_MONO(),
        ).grid(row=0, column=3, sticky="w")

        buttons = tk.Frame(box, bg=BG_CARD)
        buttons.pack(fill="x", padx=PAD(), pady=(0, GAP()))
        for text, command in (
            ("Copy login command", self._copy_command),
            ("Open terminal here" if not IS_WINDOWS else "Open PowerShell here",
             self._open_terminal),
        ):
            tk.Button(
                buttons, text=text, command=command, bg=BG_TRACK, fg=FG,
                font=FONT_SMALL(), relief="flat", activebackground="#404040",
                activeforeground=FG, padx=scale.px(10), pady=scale.px(3),
            ).pack(side="left", padx=(0, GAP()))

        self.add_hint = tk.Label(
            box,
            text=(
                "Sign out of claude.ai first, or use a private window -- otherwise "
                "the browser silently re-grants the account you are already signed "
                "in as, and you get a duplicate."
            ),
            bg=BG_CARD, fg=WARN, font=FONT_SMALL(),
            wraplength=900, justify="left", anchor="w",
        )
        self.add_hint.pack(fill="x", padx=PAD(), pady=(0, PAD()))

    # ------------------------------------------------------------------- scan

    def scan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        if self.scan_button is not None:
            self.scan_button.configure(text="Scanning...", state="disabled")
        self.status.configure(
            text="Identifying each profile against the API...", fg=FG_DIM
        )

        def work():
            try:
                enrolled = {
                    str(Path(a.config_dir).resolve()): a.label
                    for a in self.state.accounts()
                }
            except Exception:
                enrolled = {}
            try:
                entries = discover(self.state.user_agent, enrolled=enrolled)
                labels = suggest_labels(entries)
                error = None
            except Exception as exc:
                entries, labels, error = [], {}, "%s: %s" % (type(exc).__name__, exc)
            self.frame.after(0, lambda: self._render(entries, labels, error))

        threading.Thread(target=work, daemon=True).start()

    def _render(self, entries, labels, error) -> None:
        self._scanning = False
        if self.scan_button is not None:
            self.scan_button.configure(text="Scan for profiles", state="normal")
        self.entries, self.labels = entries, labels
        self.editing = None

        for child in self.list_frame.winfo_children():
            child.destroy()
        for child in self.banner_host.winfo_children():
            child.destroy()

        if error:
            self.status.configure(text="Scan failed: " + error, fg=BAD)
            return

        identities = {
            e.profile.identity for e in entries if e.profile and e.profile.identity
        }
        dupes = [e for e in entries if e.duplicate_of]
        unenrolled = [e for e in entries if not e.enrolled_as and not e.duplicate_of]

        summary = "%d profile folder%s, %d distinct account%s" % (
            len(entries), "" if len(entries) == 1 else "s",
            len(identities), "" if len(identities) == 1 else "s",
        )
        if dupes:
            summary += "   |   %d duplicate folder%s" % (
                len(dupes), "" if len(dupes) == 1 else "s"
            )
        if unenrolled:
            summary += "   |   %d not yet monitored" % len(unenrolled)
        self.status.configure(text=summary, fg=FG_DIM)

        if dupes:
            self._render_banner(len(dupes))

        for entry, indent in self._nested_order(entries):
            self._render_row(entry, indent)

    @staticmethod
    def _nested_order(entries):
        """Order duplicates directly under the folder they duplicate.

        Putting the relationship in the layout is the point: a doubled account
        is obvious from the shape of the list, not just from a label.
        """
        children: dict[str, list] = {}
        for entry in entries:
            if entry.duplicate_of:
                children.setdefault(entry.duplicate_of, []).append(entry)

        ordered = []
        for entry in entries:
            if entry.duplicate_of:
                continue
            ordered.append((entry, 0))
            for child in children.get(entry.name, []):
                ordered.append((child, 1))
        # Any duplicate whose parent is missing still has to appear.
        placed = {id(e) for e, _ in ordered}
        for entry in entries:
            if id(entry) not in placed:
                ordered.append((entry, 1))
        return ordered

    def _render_banner(self, count: int) -> None:
        banner = tk.Frame(self.banner_host, bg="#241f14")
        banner.pack(fill="x", padx=PAD(), pady=(0, scale.px(10)))
        tk.Frame(banner, bg=WARN, width=scale.px(3)).pack(
            side="left", fill="y", padx=(0, GAP())
        )
        tk.Label(
            banner,
            text="%d folder%s hold a second login to an account you already "
                 "monitor. Each one doubles that account's polls against the "
                 "same rate limit." % (count, "" if count == 1 else "s"),
            bg="#241f14", fg=WARN, font=FONT_SMALL(), anchor="w",
            justify="left", wraplength=scale.px(700),
        ).pack(side="left", pady=GAP())

    def _render_row(self, entry, indent: int = 0) -> None:
        if entry.error:
            accent, body = BAD, BG_CARD
        elif entry.duplicate_of:
            accent, body = WARN, "#1e1c17"
        elif entry.enrolled_as:
            accent, body = OK, BG_CARD
        else:
            accent, body = FG_MUTED, BG_CARD

        shell = tk.Frame(self.list_frame, bg=BG)
        shell.pack(fill="x", pady=scale.px(2), padx=(scale.px(22 * indent), 0))
        tk.Frame(shell, bg=accent, width=scale.px(3)).pack(side="left", fill="y")
        row = tk.Frame(shell, bg=body)
        row.pack(side="left", fill="both", expand=True)

        top = tk.Frame(row, bg=body)
        top.pack(fill="x", padx=PAD(), pady=(scale.px(10), 0))
        top.columnconfigure(3, weight=1)

        tk.Label(
            top, text=entry.name, bg=body, fg=FG, font=FONT_MONO(), anchor="w",
            pady=0, bd=0, highlightthickness=0,
        ).grid(row=0, column=0, sticky="w", padx=(0, GAP() + 2))

        tk.Label(
            top, text=entry.status_text, bg=body,
            fg=BAD if entry.error else FG, font=FONT(), anchor="w",
            pady=0, bd=0, highlightthickness=0,
        ).grid(row=0, column=1, sticky="w", padx=(0, GAP() + 2))

        if entry.profile and entry.profile.rate_limit_tier:
            tier = entry.profile.rate_limit_tier.replace("default_claude_", "")
            chip = tk.Label(
                top, text=tier, bg="#2a2a2a", fg=FG_DIM,
                font=scale.font(10, mono=True), padx=scale.px(6),
                pady=scale.px(2), bd=0, highlightthickness=0,
            )
            chip.grid(row=0, column=2, sticky="w")

        # --- right-hand controls
        if entry.duplicate_of:
            tk.Label(
                top, text="duplicate of %s" % entry.duplicate_of, bg=body,
                fg=WARN, font=FONT_SMALL(), anchor="e",
                pady=0, bd=0, highlightthickness=0,
            ).grid(row=0, column=4, sticky="e", padx=(GAP(), 0))
        elif entry.enrolled_as:
            self._alias_control(top, entry, body, entry.enrolled_as, "monitored as")
            tk.Button(
                top, text="Remove", command=lambda e=entry: self._remove(e),
                bg=BG_TRACK, fg=FG_DIM, font=FONT_SMALL(), relief="flat",
                activebackground="#404040", activeforeground=FG,
                padx=scale.px(8), pady=scale.px(2), bd=0, highlightthickness=0,
            ).grid(row=0, column=5, sticky="e", padx=(GAP(), 0))
        elif not entry.error:
            suggested = self.labels.get(str(entry.config_dir), entry.name.lstrip("."))
            self._alias_control(top, entry, body, suggested, "label")
            tk.Button(
                top, text="Add to monitor", command=lambda e=entry: self._add(e),
                bg=CORAL, fg="#1a1a1a", font=scale.font(11, bold=True),
                relief="flat", activebackground="#e08670",
                activeforeground="#1a1a1a", padx=scale.px(11), pady=scale.px(3),
                bd=0, highlightthickness=0,
            ).grid(row=0, column=5, sticky="e", padx=(GAP(), 0))

        second = tk.Frame(row, bg=body)
        second.pack(fill="x", padx=PAD(), pady=(scale.px(5), scale.px(10)))
        tk.Label(
            second, text=str(entry.config_dir), bg=body, fg=FG_MUTED,
            font=scale.font(11, mono=True), anchor="w",
            pady=0, bd=0, highlightthickness=0,
        ).pack(side="left")

        note, tone = self._row_note(entry)
        if note:
            tk.Label(
                second, text=note, bg=body, fg=tone, font=FONT_SMALL(), anchor="e",
                pady=0, bd=0, highlightthickness=0,
            ).pack(side="right")

    @staticmethod
    def _row_note(entry):
        if entry.duplicate_of:
            return "same account.uuid — not polled", WARN
        if entry.error:
            if "re-auth" in entry.error or "expired" in entry.error:
                return "token expired — re-run claude here", WARN
            return ".credentials.json missing or unreadable", BAD
        return "", FG_MUTED

    # ----------------------------------------------------------------- alias

    def _alias_control(self, parent, entry, body, current: str, prefix: str) -> None:
        """Click the alias to rename it. Only accounts.json ever changes."""
        holder = tk.Frame(parent, bg=body)
        holder.grid(row=0, column=4, sticky="e", padx=(GAP(), 0))

        if self.editing == entry.name:
            var = tk.StringVar(value=current)
            box = tk.Entry(
                holder, textvariable=var, width=16, bg="#141414", fg=FG,
                insertbackground=FG, relief="flat", font=FONT_MONO(),
                highlightthickness=scale.px(1), highlightbackground=CORAL,
                highlightcolor=CORAL,
            )
            box.pack(side="left", padx=(0, GAP()))
            box.focus_set()
            box.select_range(0, "end")
            commit = lambda _e=None: self._commit_alias(entry, var.get())
            cancel = lambda _e=None: self._cancel_alias()
            box.bind("<Return>", commit)
            box.bind("<Escape>", cancel)
            tk.Button(
                holder, text="Save", command=commit, bg=CORAL, fg="#1a1a1a",
                font=scale.font(11, bold=True), relief="flat",
                activebackground="#e08670", activeforeground="#1a1a1a",
                padx=scale.px(9), pady=scale.px(2), bd=0, highlightthickness=0,
            ).pack(side="left", padx=(0, GAP()))
            tk.Label(
                holder, text="Cancel", bg=body, fg=FG_DIM, font=FONT_SMALL(),
                cursor="hand2", pady=0, bd=0, highlightthickness=0,
            ).pack(side="left")
            holder.winfo_children()[-1].bind("<Button-1>", cancel)
            return

        tk.Label(
            holder, text=prefix, bg=body, fg=FG_MUTED, font=FONT_SMALL(),
            pady=0, bd=0, highlightthickness=0,
        ).pack(side="left", padx=(0, scale.px(5)))
        alias = tk.Label(
            holder, text=current, bg=body,
            fg=OK if entry.enrolled_as else FG_DIM, font=FONT(),
            cursor="xterm", pady=0, bd=0, highlightthickness=0,
        )
        alias.pack(side="left")
        underline = tk.Frame(holder, bg="#4a4a4a", height=scale.px(1))
        alias.bind("<Button-1>", lambda _e, e=entry: self._start_alias(e))
        alias.bind("<Enter>", lambda _e: alias.configure(fg=FG))
        alias.bind(
            "<Leave>",
            lambda _e: alias.configure(fg=OK if entry.enrolled_as else FG_DIM),
        )
        del underline

    def _start_alias(self, entry) -> None:
        self.editing = entry.name
        self._render(self.entries, self.labels, None)

    def _cancel_alias(self) -> None:
        self.editing = None
        self._render(self.entries, self.labels, None)

    def _commit_alias(self, entry, raw: str) -> None:
        """Rename in accounts.json, and carry the history rows with it."""
        label = "-".join(raw.split()).strip()
        if not label:
            self._cancel_alias()
            return

        data = self._load_raw()
        target = str(Path(entry.config_dir).resolve()).lower()
        existing = set()
        old_label = None
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                path = str(
                    Path(self.state.expand(item.get("config_dir", ""))).resolve()
                ).lower()
            except OSError:
                path = ""
            if path == target:
                old_label = item.get("label")
            else:
                existing.add(str(item.get("label")))

        unique, n = label, 2
        while unique in existing:
            unique, n = "%s-%d" % (label, n), n + 1

        if old_label is None:
            # Not monitored yet: remember the alias for the Add button.
            self.labels[str(entry.config_dir)] = unique
            self.editing = None
            self._render(self.entries, self.labels, None)
            return

        for item in data:
            if isinstance(item, dict) and item.get("label") == old_label:
                item["label"] = unique

        if self._save_raw(data):
            store = getattr(self.state, "store", None)
            if store is not None and old_label != unique:
                try:
                    moved = store.rename_account(old_label, unique)
                    self.status.configure(
                        text="Renamed %s to %s, carrying %d history row%s."
                        % (old_label, unique, moved, "" if moved == 1 else "s"),
                        fg=OK,
                    )
                except Exception:
                    self.status.configure(
                        text="Renamed to %s, but history could not be moved."
                        % unique,
                        fg=WARN,
                    )
        self.editing = None
        self.scan()

    # ---------------------------------------------------------------- actions

    def _load_raw(self) -> list:
        path = Path(self.state.accounts_path)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_raw(self, data: list) -> bool:
        path = Path(self.state.accounts_path)
        try:
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            self.status.configure(text="Could not write accounts.json: %s" % exc, fg=BAD)
            return False
        self.state.on_accounts_changed()
        return True

    def _add(self, entry) -> None:
        data = self._load_raw()
        label = self.labels.get(str(entry.config_dir), entry.name.lstrip("."))
        existing = {str(item.get("label")) for item in data if isinstance(item, dict)}
        unique, n = label, 2
        while unique in existing:
            unique, n = "%s-%d" % (label, n), n + 1
        data.append({"label": unique, "config_dir": str(entry.config_dir)})
        if self._save_raw(data):
            self.status.configure(text="Added %s to the monitor." % unique, fg=OK)
            self.scan()

    def _remove(self, entry) -> None:
        target = str(Path(entry.config_dir).resolve()).lower()
        data = [
            item for item in self._load_raw()
            if not (
                isinstance(item, dict)
                and str(Path(self.state.expand(item.get("config_dir", ""))).resolve()).lower()
                == target
            )
        ]
        if self._save_raw(data):
            self.status.configure(
                text="Removed %s. The folder and its login were left untouched."
                % entry.name,
                fg=FG_DIM,
            )
            self.scan()

    def _target_dir(self) -> Path:
        name = self.dir_var.get().strip() or ".claude-new"
        if not name.startswith("."):
            name = "." + name
        return Path.home() / name

    def _copy_command(self) -> None:
        email = self.email_var.get().strip() or "you@example.com"
        command = login_command(self._target_dir(), email)
        self.frame.clipboard_clear()
        self.frame.clipboard_append(command)
        self.status.configure(
            text="Command copied. Paste it into PowerShell, finish the browser "
                 "login, then Scan again.",
            fg=OK,
        )

    def _open_terminal(self) -> None:
        """Open a shell with CLAUDE_CONFIG_DIR set. Runs no login itself."""
        target = self._target_dir()
        email = self.email_var.get().strip()
        suffix = (" --email " + email) if email else ""
        try:
            if IS_WINDOWS:
                hint = (
                    "Write-Host 'CLAUDE_CONFIG_DIR is set for this window.' "
                    "-Foreground Green; "
                    "Write-Host 'Run:  %s auth login%s' -Foreground Cyan; "
                    "Write-Host 'Then: %s auth status --text' -Foreground Cyan"
                ) % (CLI, suffix, CLI)
                setup = '$env:CLAUDE_CONFIG_DIR = "%s"; %s' % (target, hint)
                subprocess.Popen(
                    ["powershell.exe", "-NoExit", "-NoProfile", "-Command", setup],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
            elif sys.platform == "darwin":
                script = 'export CLAUDE_CONFIG_DIR="%s"; echo "Run: %s auth login%s"' % (
                    target, CLI, suffix
                )
                subprocess.Popen([
                    "osascript", "-e",
                    'tell application "Terminal" to do script "%s"' % script.replace('"', '\\"'),
                    "-e", 'tell application "Terminal" to activate',
                ])
            else:
                self.status.configure(
                    text="Terminal launching is not wired up for this platform. "
                         "Use Copy login command instead.",
                    fg=WARN,
                )
                return
            self.status.configure(
                text="Terminal opened for %s. Sign in there, then Scan again."
                % target.name,
                fg=OK,
            )
        except OSError as exc:
            self.status.configure(text="Could not open a terminal: %s" % exc, fg=BAD)
