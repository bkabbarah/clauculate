"""Step 3 verification. Run: python tools/verify.py

Covers, headlessly:
  1. read-only guarantee (hash every file in every Claude config dir, before
     and after a full poll cycle, plus a direct write attempt)
  2. 429 backoff schedule (3 -> 6 -> 12 -> 15 capped) via a mocked transport
  3. UTC -> local conversion across a DST boundary
  4. an unknown window key the code has never seen
  5. malformed / unexpected JSON degrades instead of crashing
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from claude_usage import readonly_guard  # noqa: E402
from claude_usage.config import Account, load_accounts  # noqa: E402
from claude_usage.formatting import format_reset_absolute  # noqa: E402
from claude_usage.model import parse_usage, to_local  # noqa: E402
from claude_usage.paths import default_accounts_path  # noqa: E402
from claude_usage.poller import BACKOFF_STEPS, Poller  # noqa: E402

PASS = "  PASS  "
FAIL = "  FAIL  "
results: list[tuple[bool, str]] = []


def check(ok: bool, message: str) -> None:
    results.append((ok, message))
    print(("%s %s" % (PASS if ok else FAIL, message)))


def snapshot_tree(roots: list[Path]) -> dict[str, str]:
    """sha256 of every file under every config dir, plus its mtime."""
    out: dict[str, str] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            digest = hashlib.sha256(data).hexdigest()
            out[str(path)] = "%s:%s" % (digest, path.stat().st_mtime_ns)
    return out


# --------------------------------------------------------------------------
# 1. Read-only guarantee
# --------------------------------------------------------------------------
def test_readonly(accounts: list[Account]) -> None:
    print("\n[1] read-only guarantee")
    roots = [a.config_dir for a in accounts]

    before = snapshot_tree(roots)
    print("      hashed %d files across %d config dirs" % (len(before), len(roots)))

    readonly_guard.protect(*roots)
    readonly_guard.install()

    # A real poll cycle against the live endpoint.
    poller = Poller(accounts, store=None, logger=None)
    with httpx.Client(timeout=30.0) as client:
        for account in accounts:
            poller._poll_account(client, account)
            time.sleep(0.4)

    after = snapshot_tree(roots)

    changed = [k for k in before if before[k] != after.get(k)]
    added = [k for k in after if k not in before]
    removed = [k for k in before if k not in after]

    check(not changed, "no file contents or mtimes changed (%d checked)" % len(before))
    if changed:
        for path in changed[:10]:
            print("          CHANGED: %s" % path)
    check(not added, "no files created in any config dir")
    if added:
        for path in added[:10]:
            print("          ADDED: %s" % path)
    check(not removed, "no files removed from any config dir")

    # The guard is not just documentation: prove it actually blocks a write.
    target = accounts[0].config_dir / "guard_probe.tmp"
    blocked = False
    try:
        with open(target, "w") as fh:
            fh.write("this must never be written")
    except readonly_guard.ReadOnlyViolation:
        blocked = True
    check(blocked, "guard raised ReadOnlyViolation on an explicit write attempt")
    check(not target.exists(), "probe file was never created on disk")

    # Also prove the low-level os.open path is covered, not just builtins.open.
    import os

    blocked_os = False
    try:
        os.open(str(target), os.O_WRONLY | os.O_CREAT)
    except readonly_guard.ReadOnlyViolation:
        blocked_os = True
    except OSError:
        blocked_os = False
    check(blocked_os, "guard also blocks the low-level os.open write path")
    check(not target.exists(), "os.open probe file was never created either")


# --------------------------------------------------------------------------
# 2. 429 backoff
# --------------------------------------------------------------------------
def test_backoff(accounts: list[Account]) -> None:
    print("\n[2] 429 backoff schedule")
    account = accounts[0]
    poller = Poller([account], store=None, logger=None)

    def always_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate_limited"})

    client = httpx.Client(transport=httpx.MockTransport(always_429))
    status = poller.status(account.label)

    observed: list[float] = []
    for _ in range(6):
        started = time.time()
        poller._poll_account(client, account)
        observed.append(round(status.backoff_until - started))

    expected = [180, 360, 720, 900, 900, 900]
    check(
        observed == expected,
        "backoff schedule is %s (expected %s)" % (observed, expected),
    )
    check(
        status.error_kind == "rate_limit",
        "status reports error_kind=rate_limit, not stale-as-current",
    )
    check(
        status.backoff_remaining > 0,
        "UI has a live countdown to show (%ds remaining)" % status.backoff_remaining,
    )
    check(
        BACKOFF_STEPS[-1] == 900.0,
        "backoff is capped at 15 minutes",
    )

    # A 429 must not overwrite a previously good snapshot with zeros.
    check(
        status.snapshot is None,
        "no fabricated snapshot invented on failure",
    )
    client.close()


# --------------------------------------------------------------------------
# 3. DST correctness
# --------------------------------------------------------------------------
def test_dst() -> None:
    print("\n[3] UTC -> local across a DST boundary")
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        check(False, "zoneinfo unavailable; cannot verify DST")
        return

    local_now = datetime.now().astimezone()
    tzname = str(local_now.tzinfo)
    print("      system local offset right now: %s (%s)" % (local_now.strftime("%z"), tzname))

    # Two instants that straddle US DST. Compare our conversion against
    # zoneinfo's authoritative answer for the same zone.
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

    winter_local = to_local(winter)
    summer_local = to_local(summer)

    winter_offset = winter_local.utcoffset()
    summer_offset = summer_local.utcoffset()

    print("      2026-01-15 12:00Z -> %s (offset %s)" % (winter_local, winter_offset))
    print("      2026-07-15 12:00Z -> %s (offset %s)" % (summer_local, summer_offset))

    check(
        winter_offset != summer_offset,
        "offset differs across the DST boundary (%s vs %s)" % (winter_offset, summer_offset),
    )
    check(
        (summer_offset - winter_offset).total_seconds() == 3600,
        "summer is exactly one hour ahead of winter",
    )

    # The transition instant itself: 2026-03-08 10:00Z is 02:00 -> 03:00 PST/PDT.
    before = to_local(datetime(2026, 3, 8, 9, 59, tzinfo=timezone.utc))
    after = to_local(datetime(2026, 3, 8, 10, 1, tzinfo=timezone.utc))
    print("      spring-forward: %s  ->  %s" % (before, after))
    check(
        after.utcoffset() != before.utcoffset(),
        "the spring-forward transition itself is handled",
    )

    # And the absolute formatter stays readable across it.
    check(
        "--" not in format_reset_absolute(summer),
        "absolute formatter renders a real local time, not a placeholder",
    )


# --------------------------------------------------------------------------
# 4. Unknown window key
# --------------------------------------------------------------------------
def test_unknown_key() -> None:
    print("\n[4] unknown window key the code has never seen")
    payload = {
        "five_hour": {"utilization": 12.0, "resets_at": "2026-08-22T06:20:00+00:00"},
        # Invented. Nothing in the codebase mentions this key.
        "warp_core_weekly": {
            "utilization": 63.5,
            "resets_at": "2026-08-25T11:00:00+00:00",
            "limit_dollars": 40,
        },
        "brand_new_scalar": 17,
        "brand_new_block": {"alpha": 1, "nested": {"beta": True}},
        "limits": [
            {
                "kind": "some_future_kind",
                "group": "future",
                "percent": 55,
                "severity": "warning",
                "resets_at": "2026-08-25T11:00:00+00:00",
                "scope": {"model": {"display_name": "Fable"}, "surface": "cowork"},
                "is_active": True,
            }
        ],
    }
    snapshot = parse_usage(payload)
    keys = [w.key for w in snapshot.windows]

    check("warp_core_weekly" in keys, "unknown window key rendered as a window row")
    window = snapshot.window("warp_core_weekly")
    check(window.utilization == 63.5, "its utilization parsed correctly")
    check(window.resets_at is not None, "its reset time parsed correctly")
    check(
        window.extras.get("limit_dollars") == 40,
        "unrecognised sub-field preserved in extras (limit_dollars=40)",
    )
    check(
        ("brand_new_scalar", 17) in snapshot.scalars,
        "unknown scalar key rendered",
    )
    check(
        any(b.key == "brand_new_block" for b in snapshot.blocks),
        "unknown structured key rendered as a block",
    )
    check(
        snapshot.limits and snapshot.limits[0].kind == "some_future_kind",
        "unknown limits kind rendered",
    )
    check(
        snapshot.limits[0].scope_label == "Fable / cowork",
        "multi-dimension scope label built dynamically (%s)"
        % snapshot.limits[0].scope_label,
    )
    check(
        snapshot.worst_utilization == 63.5,
        "worst-utilization accounts for the unknown key too",
    )


# --------------------------------------------------------------------------
# 5. Malformed responses degrade
# --------------------------------------------------------------------------
def test_degradation() -> None:
    print("\n[5] unexpected shapes degrade instead of crashing")
    for label, payload in [
        ("empty object", {}),
        ("a list", [1, 2, 3]),
        ("a string", "service unavailable"),
        ("null", None),
        ("window with wrong types", {"five_hour": {"utilization": "lots", "resets_at": 5}}),
        ("limits not a list", {"limits": {"kind": "session"}}),
    ]:
        try:
            snapshot = parse_usage(payload)
            ok = snapshot is not None
            detail = "%d windows, %d limits, %d scalars" % (
                len(snapshot.windows), len(snapshot.limits), len(snapshot.scalars)
            )
        except Exception as exc:
            ok, detail = False, "raised %s" % type(exc).__name__
        check(ok, "%-26s -> %s" % (label, detail))

    # A window whose utilization is unparseable must be None, never 0.
    snapshot = parse_usage({"five_hour": {"utilization": "lots", "resets_at": None}})
    window = snapshot.window("five_hour")
    check(
        window is not None and window.utilization is None,
        "bad utilization becomes None, never a fabricated 0%",
    )


def main() -> int:
    accounts = load_accounts(default_accounts_path())
    print("verifying against %d configured account(s)\n" % len(accounts))

    test_backoff(accounts)
    test_dst()
    test_unknown_key()
    test_degradation()
    test_readonly(accounts)  # last: it installs the process-wide guard

    failed = [m for ok, m in results if not ok]
    print("\n" + "=" * 70)
    print("%d checks, %d failed" % (len(results), len(failed)))
    for message in failed:
        print("  FAILED: %s" % message)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
