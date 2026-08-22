"""SQLite history.

The endpoint is a snapshot only, so history has to be accumulated locally.
This is the ONLY database the app writes, and it lives under %LOCALAPPDATA%,
never inside a Claude config directory.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import UsageSnapshot

RETENTION_DAYS = 90

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,       -- epoch seconds, UTC
    account     TEXT    NOT NULL,
    window_key  TEXT    NOT NULL,
    utilization REAL,
    resets_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_lookup
    ON samples (account, window_key, ts);
"""


class HistoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, account: str, snapshot: UsageSnapshot) -> None:
        """Persist every window and every limits entry from one successful poll."""
        ts = int(snapshot.fetched_at.timestamp())
        rows: list[tuple] = []

        for window in snapshot.windows:
            rows.append(
                (
                    ts,
                    account,
                    window.key,
                    window.utilization,
                    window.resets_at.isoformat() if window.resets_at else None,
                )
            )

        # limits[] entries are stored under a distinct namespace so they cannot
        # collide with a top-level window that happens to share a name.
        for row in snapshot.limits:
            key = "limits:" + row.display_name
            rows.append(
                (
                    ts,
                    account,
                    key,
                    row.percent,
                    row.resets_at.isoformat() if row.resets_at else None,
                )
            )

        if not rows:
            return

        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples (ts, account, window_key, utilization, resets_at)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def series(self, account: str, window_key: str, days: int = 7) -> list[tuple[int, float]]:
        """Time-ordered (ts, utilization) points for a sparkline."""
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        with self._lock:
            cur = self._conn.execute(
                "SELECT ts, utilization FROM samples"
                " WHERE account = ? AND window_key = ? AND ts >= ?"
                "   AND utilization IS NOT NULL"
                " ORDER BY ts",
                (account, window_key, cutoff),
            )
            return [(int(r[0]), float(r[1])) for r in cur.fetchall()]

    def series_bucketed(
        self, account: str, window_key: str, days: int = 7,
        bucket_seconds: int = 1200,
    ) -> list[tuple[int, float]]:
        """Downsampled series for the chart.

        Seven days at the 180s poll floor is roughly 3,300 rows per window per
        account. Bucketing in SQL keeps that out of the panel entirely; the
        chart only ever holds a few hundred points.
        """
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
        bucket = max(60, int(bucket_seconds))
        with self._lock:
            cur = self._conn.execute(
                "SELECT (ts / ?) * ? AS bucket, AVG(utilization)"
                " FROM samples"
                " WHERE account = ? AND window_key = ? AND ts >= ?"
                "   AND utilization IS NOT NULL"
                " GROUP BY bucket ORDER BY bucket",
                (bucket, bucket, account, window_key, cutoff),
            )
            return [(int(r[0]), float(r[1])) for r in cur.fetchall()]

    def rename_account(self, old_label: str, new_label: str) -> int:
        """Carry an account's history across a label change.

        History rows are keyed by label, so renaming in accounts.json without
        this would orphan every sample and restart the sparkline from nothing.
        """
        if old_label == new_label:
            return 0
        with self._lock:
            cur = self._conn.execute(
                "UPDATE samples SET account = ? WHERE account = ?",
                (new_label, old_label),
            )
            self._conn.commit()
            return cur.rowcount

    def known_window_keys(self, account: str) -> list[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT DISTINCT window_key FROM samples WHERE account = ?"
                " ORDER BY window_key",
                (account,),
            )
            return [r[0] for r in cur.fetchall()]

    def prune(self, retention_days: int = RETENTION_DAYS) -> int:
        cutoff = int(
            (datetime.now(timezone.utc) - timedelta(days=retention_days)).timestamp()
        )
        with self._lock:
            cur = self._conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
