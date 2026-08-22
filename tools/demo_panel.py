"""Render the panel with synthetic accounts and screenshot it.

Usage:
    python tools/demo_panel.py --accounts 6 --shot panel6.png

Exercises the cases that are awkward to reach with live data: one account,
many accounts, an unknown window key, and every failure state at once.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tkinter as tk  # noqa: E402

from claude_usage.config import Account  # noqa: E402
from claude_usage.dpi import enable_dpi_awareness  # noqa: E402
from claude_usage.model import parse_usage  # noqa: E402
from claude_usage.panel import Panel  # noqa: E402
from claude_usage.poller import AccountStatus, ErrorKind  # noqa: E402


def iso(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def payload(five: float, week: float, fable: float | None = None, exotic: bool = False):
    data = {
        "five_hour": {"utilization": five, "resets_at": iso(3.2)},
        "seven_day": {"utilization": week, "resets_at": iso(29)},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "tangelo": None,
        "nimbus_quill": {"utilization": 0.0, "resets_at": None},
        "extra_usage": {
            "is_enabled": exotic,
            "monthly_limit": 200 if exotic else None,
            "used_credits": 47.5 if exotic else None,
            "utilization": 23.75 if exotic else None,
            "currency": "USD" if exotic else None,
        },
        "limits": [
            {"kind": "session", "group": "session", "percent": five,
             "severity": "normal", "resets_at": iso(3.2), "scope": None,
             "is_active": False},
            {"kind": "weekly_all", "group": "weekly", "percent": week,
             "severity": "normal", "resets_at": iso(29), "scope": None,
             "is_active": False},
        ],
        "spend": {"used": {"amount_minor": 0, "currency": "USD", "exponent": 2},
                  "percent": 0, "severity": "normal", "enabled": False},
        "member_dashboard_available": False,
    }
    if fable is not None:
        data["limits"].append({
            "kind": "weekly_scoped", "group": "weekly", "percent": fable,
            "severity": "critical" if fable > 90 else "warning",
            "resets_at": iso(29),
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": True,
        })
    if exotic:
        # A window key that appears nowhere in the source tree.
        data["warp_core_weekly"] = {
            "utilization": 63.5, "resets_at": iso(70), "limit_dollars": 40,
        }
        data["quantum_flux_capacity"] = 9000
        data["unheard_of_block"] = {"alpha": 1, "nested": {"beta": True}}
    return data


class FakePoller:
    """Stands in for Poller so the panel can be driven without network calls."""

    def __init__(self, count: int):
        self.accounts = []
        self._statuses = {}
        now = time.time()

        for i in range(count):
            label = "account-%d" % (i + 1)
            account = Account(label=label, config_dir=Path.home() / (".claude-demo%d" % i))
            self.accounts.append(account)
            status = AccountStatus(label=label, config_dir=account.config_dir)
            status.subscription_type = "max"
            status.rate_limit_tier = "default_claude_max_20x"

            if i == 1 and count > 1:
                # rate limited, with a stale-but-labelled previous snapshot
                status.snapshot = parse_usage(payload(41, 55, fable=88))
                status.raw_text = "{}"
                status.last_success = now - 900
                status.error_kind = ErrorKind.RATE_LIMIT
                status.error = "rate limited (HTTP 429)"
                status.backoff_until = now + 431
                status.backoff_index = 3
            elif i == 2 and count > 2:
                # expired credentials
                status.error_kind = ErrorKind.AUTH
                status.error = (
                    're-auth needed: run `claude` with '
                    'CLAUDE_CONFIG_DIR="%s"' % account.config_dir
                )
            elif i == 3 and count > 3:
                # unparseable response -> raw fallback
                status.error_kind = ErrorKind.SHAPE
                status.error = "response was not JSON: Expecting value line 1 column 1"
                status.raw_text = "<html><body>502 Bad Gateway</body></html>"
            elif i == 4 and count > 4:
                # exotic: unknown keys + extra_usage enabled
                status.snapshot = parse_usage(payload(72, 91, fable=96, exotic=True))
                status.raw_text = "{}"
                status.last_success = now - 45
            else:
                status.snapshot = parse_usage(
                    payload(8 + i * 11, 20 + i * 9, fable=30 + i * 14)
                )
                status.raw_text = "{}"
                status.last_success = now - (12 + i * 5)

            self._statuses[label] = status

    def statuses(self):
        return dict(self._statuses)

    def status(self, label):
        return self._statuses.get(label)

    def worst_utilization(self):
        values = [
            s.snapshot.worst_utilization
            for s in self._statuses.values()
            if s.snapshot is not None and s.snapshot.worst_utilization is not None
        ]
        return max(values) if values else None

    def request_refresh(self, label=None):
        pass

    def next_due_seconds(self):
        return 172.0


def _seed_store(poller):
    """A throwaway SQLite store with a week of plausible history."""
    import math
    import tempfile
    from claude_usage.store import HistoryStore

    path = Path(tempfile.gettempdir()) / "clauculate_demo_history.sqlite3"
    if path.exists():
        path.unlink()
    store = HistoryStore(path)
    now = int(time.time())
    rows = []
    for account in poller.accounts:
        for key, base in (("five_hour", 30), ("seven_day", 55),
                          ("limits:weekly_scoped [Fable]", 70)):
            for i in range(7 * 24 * 20):          # 7 days at 3-minute steps
                ts = now - i * 180
                wave = math.sin(i / 90.0) * 18 + math.sin(i / 17.0) * 6
                value = max(0.0, min(100.0, base + wave))
                rows.append((ts, account.label, key, value, None))
    with store._lock:
        store._conn.executemany(
            "INSERT INTO samples (ts,account,window_key,utilization,resets_at)"
            " VALUES (?,?,?,?,?)", rows)
        store._conn.commit()
    print("seeded %d history rows" % len(rows))
    return store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accounts", type=int, default=6)
    parser.add_argument("--shot", type=Path, default=None)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--history", action="store_true",
                        help="seed a temp store so the chart has data")
    parser.add_argument("--expand", type=int, default=None,
                        help="1-based index of a card to expand")
    parser.add_argument("--scroll", type=float, default=0.0,
                        help="scroll fraction 0..1 before the screenshot")
    args = parser.parse_args()

    enable_dpi_awareness()

    root = tk.Tk()
    root.withdraw()

    poller = FakePoller(args.accounts)
    store = _seed_store(poller) if args.history else None
    panel = Panel(root, poller, store=store)
    panel.show()
    panel.window.geometry("%dx%d+30+20" % (args.width, args.height))
    if args.expand:
        root.update_idletasks()
        panel.refresh()
        list(panel._cards.values())[args.expand - 1].toggle()

    if args.shot:
        def capture():
            from PIL import ImageGrab
            win = panel.window
            # Front the window first, or the grab captures whatever is on top
            # of it at those screen coordinates.
            win.attributes("-topmost", True)
            win.lift()
            win.focus_force()
            win.update()
            win.update_idletasks()
            if args.scroll:
                # Recompute the scrollregion first, or the canvas scrolls
                # against a stale bbox and renders torn rows.
                canvas = panel.canvas
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.update_idletasks()
                canvas.yview_moveto(args.scroll)
                canvas.update_idletasks()
                win.update()
                time.sleep(0.5)
                win.update()
            time.sleep(0.4)
            x, y = win.winfo_rootx(), win.winfo_rooty()
            w, h = win.winfo_width(), win.winfo_height()
            image = ImageGrab.grab(bbox=(x, y, x + w, y + h))
            args.shot.parent.mkdir(parents=True, exist_ok=True)
            image.save(args.shot)
            print("saved %s (%dx%d, %d accounts)" % (args.shot, w, h, args.accounts))
            root.quit()

        root.after(1200, capture)

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
