"""Pure data model, storage, and strict 24-hour vs prior-seven-day alert logic.

No Home Assistant imports: straightforward deterministic testing. The monitor stores
observations in its OWN SQLite; it never writes to a monitored database.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
from typing import Iterable

HOUR = 3600
DAY = 86400

@dataclass(frozen=True)
class Sample:
    hour: int  # UTC epoch-hour identity, including separate DST fallback 02:00s
    source: str
    size: int | None
    status: str  # ok / cached / unavailable
    method: str
    measured_at: int | None
    allocated: int | None = None
    detail: str = ""

@dataclass(frozen=True)
class Evaluation:
    source: str
    status: str  # learning, maintenance, normal, alarm
    delta24: int | None = None
    baseline_daily: float | None = None
    excess_ratio: float | None = None
    evidence: str = ""

    def as_json(self):
        return asdict(self)


def utc_hour(ts: int | float) -> int:
    return int(ts) // HOUR * HOUR


@contextmanager
def _raw_connection(path: str | Path):
    """Always close the monitor's own SQLite connection after the transaction.

    A bare ``with sqlite3.connect(...)`` commits/rolls back but DOES NOT close
    the connection. The hourly monitor must not leak file descriptors.
    """
    db = sqlite3.connect(path, timeout=1.0)
    try:
        db.execute("PRAGMA busy_timeout=1000")
        with db:
            yield db
    finally:
        db.close()


class SnapshotDB:
    """Small independent metadata-only archive, backed up with HA config."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _raw_connection(self.path) as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS snapshots (
                hour INTEGER NOT NULL,
                source TEXT NOT NULL,
                size INTEGER,
                status TEXT NOT NULL,
                method TEXT NOT NULL,
                measured_at INTEGER,
                allocated INTEGER,
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (hour, source)
              ) WITHOUT ROWID;
              CREATE TABLE IF NOT EXISTS alerts (
                source TEXT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 0,
                last_window TEXT,
                acknowledged_until INTEGER NOT NULL DEFAULT 0,
                opened_at INTEGER,
                last_notify_at INTEGER
              );
              CREATE TABLE IF NOT EXISTS writers (
                hour INTEGER PRIMARY KEY,
                data TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS writer_windows (
                end_ts INTEGER PRIMARY KEY,
                start_ts INTEGER NOT NULL,
                status TEXT NOT NULL,
                total_rows INTEGER,
                counts_json TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
              );
              CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
              );
            """)
            db.execute("""INSERT INTO metadata(key,value) VALUES('schema','2')
                ON CONFLICT(key) DO UPDATE SET value='2'""")

    def save(self, samples: Iterable[Sample]):
        rows = list(samples)
        if not rows:
            return
        with _raw_connection(self.path) as db:
            db.executemany("""INSERT INTO snapshots
               (hour,source,size,status,method,measured_at,allocated,detail)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(hour,source) DO UPDATE SET
                 size=excluded.size, status=excluded.status,
                 method=excluded.method, measured_at=excluded.measured_at,
                 allocated=excluded.allocated, detail=excluded.detail
               WHERE NOT (snapshots.status='ok' AND excluded.status!='ok')
             """, [(s.hour,s.source,s.size,s.status,s.method,s.measured_at,s.allocated,s.detail[:150]) for s in rows])

    def latest(self):
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT s.* FROM snapshots s
               JOIN (SELECT source, MAX(hour) hour FROM snapshots GROUP BY source) m
               ON m.source=s.source AND m.hour=s.hour ORDER BY s.source""").fetchall()
            return [dict(r) for r in rows]

    def history(self, source: str, since: int, limit: int = 2200):
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT hour, size, status, measured_at, allocated
            FROM snapshots WHERE source=? AND hour>=?
            ORDER BY hour DESC LIMIT ?""", (source, since, min(limit, 2200))).fetchall()
            return [dict(r) for r in reversed(rows)]

    def growth_evidence(self, source: str, now: int, *, hourly_source: bool) -> dict:
        """Return just 33 verified/failed 6h checkpoint rows from OUR SQLite.

        A separate bounded SQL check over the same small hourly history detects
        observed shrinks hidden between checkpoints. For hourly sources we
        additionally require >=20/24 genuine measurements in each UTC day.
        No Recorder query is performed here.
        """
        now = utc_hour(now)
        start = now - 8 * DAY
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            points = [dict(r) for r in db.execute("""
                SELECT hour,size,status,measured_at FROM snapshots
                WHERE source=? AND hour>=? AND hour<=?
                  AND (hour - ?) % ? = 0
                ORDER BY hour
                LIMIT 33
            """, (source, start, now, now, 6 * HOUR))]
            # Window function scans at most 193 hourly records from the
            # monitor's tiny own history, returning only a boolean.
            shrink = db.execute("""
                SELECT 1 FROM (
                  SELECT size, LAG(size) OVER (ORDER BY hour) AS previous_size
                  FROM snapshots
                  WHERE source=? AND hour>=? AND hour<=?
                    AND status='ok' AND size IS NOT NULL
                    AND measured_at IS NOT NULL AND ABS(hour-measured_at)<=1500
                ) WHERE previous_size > size LIMIT 1
            """, (source, start, now)).fetchone() is not None
            coverage_ok = True
            if hourly_source:
                counts = dict(db.execute("""
                    SELECT (hour - ?) / ? AS bucket, COUNT(*)
                    FROM snapshots WHERE source=? AND hour>=? AND hour<?
                      AND status='ok' AND size IS NOT NULL
                      AND measured_at IS NOT NULL AND ABS(hour-measured_at)<=1500
                    GROUP BY bucket
                """, (start, DAY, source, start, now)).fetchall())
                coverage_ok = all(counts.get(i, 0) >= 20 for i in range(8))
            return {"points": points, "observed_shrink": shrink,
                    "hourly_coverage_ok": coverage_ok}

    def last_two(self, source: str):
        with _raw_connection(self.path) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute(
                "SELECT hour,status,measured_at FROM snapshots WHERE source=? ORDER BY hour DESC LIMIT 2", (source,))]

    def compact_retention(self, cutoff: int):
        # Deletes only OUR OWN monitor samples, never monitored production data.
        with _raw_connection(self.path) as db:
            db.execute("DELETE FROM snapshots WHERE hour < ?", (cutoff,))
            db.execute("DELETE FROM writers WHERE hour < ?", (cutoff,))
            # Detailed entity counts are needed for only eight days, plus
            # one day of recovery margin. Do not accumulate years of IDs.
            # Retention never deletes or changes Home Assistant Recorder.
            db.execute("DELETE FROM writer_windows WHERE end_ts < ?", (cutoff,))

    def get_alert(self, source: str):
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            r = db.execute("SELECT * FROM alerts WHERE source=?", (source,)).fetchone()
            return dict(r) if r else dict(source=source,active=0,last_window=None,acknowledged_until=0,opened_at=None,last_notify_at=None)

    def set_alert(self, source: str, *, active: bool, window: str | None,
                  now: int, notified: bool = False):
        with _raw_connection(self.path) as db:
            db.execute("""INSERT INTO alerts(source,active,last_window,opened_at,last_notify_at)
                VALUES(?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET
                active=excluded.active,
                last_window=COALESCE(excluded.last_window,alerts.last_window),
                opened_at=CASE WHEN excluded.active=0 THEN NULL
                       WHEN alerts.active=1 THEN alerts.opened_at
                       ELSE excluded.opened_at END,
                last_notify_at=COALESCE(excluded.last_notify_at,alerts.last_notify_at)
              """, (source,int(active),window,now if active else None,now if notified else None))

    def acknowledge(self, source: str, until: int):
        with _raw_connection(self.path) as db:
            db.execute("""INSERT INTO alerts(source,acknowledged_until) VALUES(?,?)
                ON CONFLICT(source) DO UPDATE SET acknowledged_until=excluded.acknowledged_until""", (source,until))

    def writer_window_exists(self, end_ts: int) -> bool:
        with _raw_connection(self.path) as db:
            return db.execute("SELECT 1 FROM writer_windows WHERE end_ts=?", (end_ts,)).fetchone() is not None

    def save_writer_window(self, data: dict):
        """Immutable once written; duplicate schedule callbacks do not rescan."""
        with _raw_connection(self.path) as db:
            db.execute("""INSERT OR IGNORE INTO writer_windows
                (end_ts,start_ts,status,total_rows,counts_json,reason)
                VALUES (?,?,?,?,?,?)""", (
                    int(data["end_ts"]), int(data["start_ts"]), data["status"],
                    data["total_rows"], json.dumps(data["counts"], separators=(',',':')),
                    str(data.get("reason", ""))[:180],
                ))

    def writer_window_history(self, since: int, until: int) -> list[dict]:
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT * FROM writer_windows
                WHERE end_ts>=? AND end_ts<=? ORDER BY end_ts DESC LIMIT 40""",
                (since, until)).fetchall()
            return [{**dict(r), "counts": json.loads(r["counts_json"])} for r in rows]

    def prune_writer_windows(self, cutoff: int):
        """Keep nine days of detailed writer IDs regardless of size retention."""
        with _raw_connection(self.path) as db:
            db.execute("DELETE FROM writer_windows WHERE end_ts < ?", (cutoff,))


def allowed_acknowledgement_sources(sources: Iterable[str]) -> set[str]:
    """Only observed sources, their stale alarms, and the independent disk alarm."""
    observed = set(sources)
    return observed | {"disk_low"} | {"stale:" + source for source in observed}


def evaluate(source: str, samples: list[dict], now: int, *, min_excess_bytes: int = 0,
             cadence_hours: int = 1, observed_shrink: bool = False,
             hourly_coverage_ok: bool = True) -> Evaluation:
    """Evaluate only complete measured UTC windows, never cached/interpolated values.

    Exactly 8 elapsed days of coverage is required. Each previous day needs
    >=20 of 24 measured hourly points (>=4 of 4 for 6h collection).
    Any *observed* size decrease between verified hourly/6h readings in
    the eight-day interval marks maintenance, even if daily net growth is
    positive after a purge followed by regrowth. Unobserved purges cannot
    be inferred from hourly snapshots.
    """
    now = utc_hour(now)
    if observed_shrink:
        return Evaluation(source,"maintenance",evidence="Observed shrink in hourly saved measurements between checkpoints")
    if not hourly_coverage_ok:
        return Evaluation(source,"learning",evidence="Insufficient verified hourly coverage (need >=20/24 per day)")
    measured = {int(x["hour"]): int(x["size"]) for x in samples
                if x.get("size") is not None and x.get("status")=="ok"
                and x.get("measured_at") is not None
                and abs(int(x["hour"])-int(x["measured_at"])) <= 25*60}
    # Check every pair of consecutive verified observations, not only the
    # eight midnight-to-midnight net deltas. A purge/repack may shrink the
    # file at 10:00 and regrow beyond its old size by 24:00.
    # Even with gaps, an observed negative change is real evidence.
    observed = sorted((hour, size) for hour, size in measured.items()
                      if now - 8*DAY <= hour <= now)
    for (_, earlier), (_, later) in zip(observed, observed[1:]):
        if later < earlier:
            return Evaluation(source, "maintenance",
                evidence="Observed size decrease within eight-day interval; waiting for a clean baseline")
    bounds = [now - x*DAY for x in range(9)]
    if any(t not in measured for t in bounds):
        return Evaluation(source,"learning",evidence="Need fresh size samples at all nine 24h UTC boundaries (8 days)")
    step = max(1,cadence_hours)*HOUR
    required = 20 if cadence_hours == 1 else max(1, math.ceil(0.8*DAY/step))
    for i in range(8):
        left, right = bounds[i+1], bounds[i]
        count = sum(1 for t in range(left, right, step) if t in measured)
        if count < required:
            return Evaluation(source,"learning",evidence=f"Incomplete measured coverage in day window {i+1}: {count}/{DAY//step}")
    daily_deltas = [measured[bounds[i]]-measured[bounds[i+1]] for i in range(8)]
    if any(v<0 for v in daily_deltas):
        return Evaluation(source,"maintenance",evidence="Database file shrank in at least one daily window; waiting for clean baseline")
    delta24 = daily_deltas[0]
    baseline = sum(daily_deltas[1:])/7.0
    if baseline<=0:
        return Evaluation(source,"learning",delta24=delta24,evidence="Previous seven-day baseline is zero or negative")
    excess_ratio = delta24/baseline-1
    if delta24>baseline*1.05 and delta24-baseline > min_excess_bytes:
        return Evaluation(source,"alarm",delta24,baseline,excess_ratio,"Strictly more than 5% above previous 7-day average daily growth")
    return Evaluation(source,"normal",delta24,baseline,excess_ratio)


def allow_notification(alert: dict, window: str, now: int) -> bool:
    """Persisted per-source per-window cooldown (handles HA restart)."""
    return (alert.get("last_window") != window
            and int(alert.get("acknowledged_until") or 0) <= now
            and (alert.get("last_notify_at") is None or now-int(alert["last_notify_at"])>=6*HOUR))
