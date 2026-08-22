"""Screenshot the Accounts tab against the real profiles on this machine.

Read-only: it scans and renders, but never clicks Add/Remove.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tkinter as tk  # noqa: E402

from demo_panel import FakePoller  # noqa: E402

from claude_usage.config import load_accounts  # noqa: E402
from claude_usage.dpi import enable_dpi_awareness  # noqa: E402
from claude_usage.panel import Panel  # noqa: E402
from claude_usage.poller import claude_code_version  # noqa: E402


class DemoState:
    def __init__(self, accounts_path: Path):
        self.accounts_path = accounts_path
        version, _ = claude_code_version()
        self.user_agent = "claude-code/" + version

    def accounts(self):
        return load_accounts(self.accounts_path)

    @staticmethod
    def expand(raw: str) -> Path:
        import os
        return Path(os.path.expandvars(str(raw))).expanduser()

    def on_accounts_changed(self) -> None:
        print("(accounts.json changed)")


# Deterministic stand-ins so a published screenshot shows a realistic layout
# without publishing anybody's address.
_FAKE = [
    ("personal", "gmail.com"), ("work", "gmail.com"), ("school", "berkeley.edu"),
    ("side-project", "gmail.com"), ("research", "gmail.com"),
    ("spare", "gmail.com"), ("archive", "gmail.com"),
]


def _install_redaction() -> None:
    """Swap real emails for stand-ins, keeping identity/dedup behaviour intact."""
    from claude_usage import accounts_tab as tab_module

    real_discover = tab_module.discover
    assigned: dict[str, tuple[str, str]] = {}

    def redacted(*a, **kw):
        entries = real_discover(*a, **kw)
        for entry in entries:
            if entry.profile is None:
                continue
            key = entry.profile.identity
            if key not in assigned:
                assigned[key] = _FAKE[len(assigned) % len(_FAKE)]
            local, domain = assigned[key]
            entry.profile.email = "%s@%s" % (local, domain)
            entry.profile.display_name = local
            if entry.enrolled_as:
                entry.enrolled_as = local
        return entries

    tab_module.discover = redacted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shot", type=Path, default=None)
    parser.add_argument("--width", type=int, default=1180)
    parser.add_argument("--height", type=int, default=860)
    parser.add_argument("--wait", type=float, default=6.0)
    parser.add_argument("--redact", action="store_true",
                        help="replace real emails with stand-ins for screenshots")
    args = parser.parse_args()

    if args.redact:
        _install_redaction()

    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()

    state = DemoState(Path(__file__).resolve().parent.parent / "accounts.json")
    panel = Panel(root, FakePoller(3), store=None, app_state=state)
    panel.show()
    panel.window.geometry("%dx%d+30+20" % (args.width, args.height))
    panel.select_tab("Accounts")
    panel.accounts_tab.scan()         # live identity scan

    if args.shot:
        def capture():
            from PIL import ImageGrab
            win = panel.window
            win.attributes("-topmost", True)
            win.lift()
            win.update()
            time.sleep(0.4)
            x, y = win.winfo_rootx(), win.winfo_rooty()
            w, h = win.winfo_width(), win.winfo_height()
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(args.shot)
            print("saved", args.shot)
            root.quit()

        root.after(int(args.wait * 1000), capture)

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
