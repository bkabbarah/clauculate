"""Entry point and wiring."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import readonly_guard
from . import __version__
from .config import ConfigError, load_accounts
from .formatting import (
    format_age,
    format_percent,
    format_reset_absolute,
    format_reset_relative,
    format_value,
)
from .logging_setup import setup_logging
from .paths import app_data_dir, db_path, default_accounts_path, log_path
from .poller import Poller
from .store import HistoryStore


def _install_guard(accounts) -> None:
    """Make every Claude profile directory unwritable for this process."""
    roots = [a.config_dir for a in accounts]
    roots.append(Path.home() / ".claude")
    # Any sibling profile dir the user may add later is covered too.
    for path in Path.home().glob(".claude*"):
        if path.is_dir():
            roots.append(path)
    readonly_guard.protect(*roots)
    readonly_guard.install()


class _AppState:
    """The narrow surface the Accounts tab is allowed to touch."""

    def __init__(self, accounts_path, poller, logger, on_reload):
        self.accounts_path = accounts_path
        self.poller = poller
        self.logger = logger
        self._on_reload = on_reload

    @property
    def user_agent(self) -> str:
        return self.poller.user_agent

    def accounts(self):
        return self.poller.accounts

    @staticmethod
    def expand(raw: str) -> Path:
        return Path(os.path.expandvars(str(raw))).expanduser()

    def on_accounts_changed(self) -> None:
        """Reload accounts.json and re-point the poller at the new set."""
        try:
            updated = load_accounts(self.accounts_path)
        except ConfigError as exc:
            self.logger.error("accounts.json reload failed: %s", exc)
            return
        self.poller.replace_accounts(updated)
        _install_guard(updated)
        self.logger.info("accounts reloaded: %d configured", len(updated))
        try:
            self._on_reload()
        except Exception:
            self.logger.exception("post-reload refresh failed")


def _text_report(poller: Poller) -> str:
    out: list[str] = []
    for account in poller.accounts:
        status = poller.status(account.label)
        out.append("=" * 78)
        out.append("%s   (%s)" % (account.label, account.config_dir))
        if status is None:
            out.append("  no status")
            continue
        out.append(
            "  updated: %s   http=%s"
            % (format_age(status.age_seconds), status.http_status)
        )
        if status.error:
            out.append("  ERROR [%s]: %s" % (status.error_kind, status.error))
        snapshot = status.snapshot
        if snapshot is None:
            continue

        out.append("  -- windows --")
        for window in snapshot.windows:
            out.append(
                "    %-24s %6s  %-18s %s"
                % (
                    window.key,
                    format_percent(window.utilization),
                    format_reset_relative(window.resets_at),
                    format_reset_absolute(window.resets_at),
                )
            )
        if snapshot.limits:
            out.append("  -- limits --")
            for limit in snapshot.limits:
                out.append(
                    "    %-34s %6s  %-9s %-18s %s"
                    % (
                        limit.display_name,
                        format_percent(limit.percent),
                        limit.severity or "",
                        format_reset_relative(limit.resets_at),
                        "ACTIVE" if limit.is_active else "",
                    )
                )
        for block in snapshot.blocks:
            out.append("  -- %s --" % block.key)
            for key, value in block.fields:
                out.append("    %-34s %s" % (key, format_value(value)))
        if snapshot.scalars:
            out.append("  -- other --")
            for key, value in snapshot.scalars:
                out.append("    %-34s %s" % (key, format_value(value)))
        if snapshot.null_keys:
            out.append(
                "  -- reported but null (%d) --\n    %s"
                % (len(snapshot.null_keys), ", ".join(snapshot.null_keys))
            )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clauculate",
        description="Read-only usage monitor for multiple Claude accounts.",
    )
    parser.add_argument("--accounts", type=Path, default=None)
    parser.add_argument("--interval", type=float, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version",
                        version="Clauculate " + __version__)
    parser.add_argument(
        "--once", action="store_true",
        help="poll every account once, print a text report, exit (no GUI)",
    )
    args = parser.parse_args(argv)

    app_data_dir().mkdir(parents=True, exist_ok=True)
    logger = setup_logging(log_path(), verbose=args.verbose)

    accounts_path = args.accounts or default_accounts_path()
    try:
        accounts = load_accounts(accounts_path)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        logger.error("config error: %s", exc)
        return 2

    _install_guard(accounts)
    logger.info(
        "starting with %d account(s); read-only roots: %s",
        len(accounts), readonly_guard.protected_roots(),
    )

    store = HistoryStore(db_path())
    pruned = store.prune()
    if pruned:
        logger.info("pruned %d history rows older than retention window", pruned)

    poller = Poller(
        accounts,
        store=store,
        logger=logger,
        interval=args.interval or 0,
    )
    logger.info("User-Agent: %s", poller.user_agent)

    if args.once:
        import httpx
        with httpx.Client(timeout=30.0) as client:
            for i, account in enumerate(accounts):
                if i:
                    time.sleep(1.0)  # stagger even in one-shot mode
                poller._poll_account(client, account)
        print(_text_report(poller))
        store.close()
        return 0

    # ---- GUI mode
    from .dpi import enable_dpi_awareness

    enable_dpi_awareness()

    import tkinter as tk

    from .panel import Panel
    from .tray import Tray

    root = tk.Tk()
    root.withdraw()

    app_state = _AppState(
        accounts_path=accounts_path,
        poller=poller,
        logger=logger,
        on_reload=lambda: (root.after(0, panel.refresh), tray.update()),
    )

    panel = Panel(root, poller, store, app_state=app_state)

    state = {"running": True}

    def shutdown():
        if not state["running"]:
            return
        state["running"] = False
        logger.info("shutting down")
        poller.stop()
        tray.stop()
        try:
            store.close()
        except Exception:
            pass
        root.quit()
        root.destroy()

    tray = Tray(
        poller,
        on_open=lambda: root.after(0, panel.show),
        on_quit=lambda: root.after(0, shutdown),
        on_refresh=poller.request_refresh,
    )

    def on_update():
        tray.update()

    poller.set_update_callback(on_update)
    poller.start()
    tray.start()

    # Prune once a day while running.
    def daily_prune():
        try:
            store.prune()
        except Exception:
            logger.exception("prune failed")
        root.after(24 * 60 * 60 * 1000, daily_prune)

    root.after(24 * 60 * 60 * 1000, daily_prune)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
