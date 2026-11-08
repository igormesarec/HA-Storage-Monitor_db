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
INFLUX_PROBE_ABANDONED_AFTER = 2 * 60

@dataclass(frozen=True)
class Sample:
    hour: int  # UTC epoch-hour identity, including separate DST fallback 02:00s
    source: str
    size: int | None
    status: str  # ok / cached / stale / unavailable / not_configured
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
    reason_code: str = ""
    verified_days: int = 0
    required_days: int = 8
    verified_checkpoints: int = 0
    required_checkpoints: int = 33
    missing_required_measurements: int = 33
    hourly_verified_measurements: int | None = None
    hourly_required_measurements: int | None = None
    last_shrink_at: int | None = None
    last_shrink_from: int | None = None
    last_shrink_to: int | None = None
    baseline_eligible_not_before: int | None = None
    latest_checkpoint_status: str | None = None

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
              CREATE TABLE IF NOT EXISTS source_lifecycle (
                source TEXT PRIMARY KEY,
                retired INTEGER NOT NULL DEFAULT 0,
                retired_at INTEGER,
                reactivated_at INTEGER,
                last_detected_at INTEGER
              );
            """)
            db.execute("""INSERT INTO metadata(key,value) VALUES('schema','3')
                ON CONFLICT(key) DO UPDATE SET value='3'""")
            self._migrate_influx_probe_metadata(db)

    @staticmethod
    def _migrate_influx_probe_metadata(db):
        """Seed v0.2 probe state from the monitor's own beta.3 snapshots.

        Successful snapshots contain an exact genuine ``measured_at``. A
        legacy unavailable row is ambiguous: beta.3 used that status both for
        an attempted failure and subsequent hourly copies. When it is newer
        than the last success, conservatively use the end of its UTC hour as
        the possible attempt time. This one-time fallback can postpone a scan,
        but can never cause an extra expensive scan during an uncertain window.
        """
        keys = {row[0] for row in db.execute("""SELECT key FROM metadata
            WHERE key IN ('influx_last_attempt_at','influx_last_success')""")}
        if len(keys) == 2:
            return
        db.row_factory = sqlite3.Row
        success = db.execute("""SELECT hour,size,method,measured_at,allocated,detail
            FROM snapshots WHERE source='influx_addon' AND status='ok'
              AND size IS NOT NULL AND measured_at IS NOT NULL
            ORDER BY measured_at DESC,hour DESC LIMIT 1""").fetchone()
        if success is not None and "influx_last_success" not in keys:
            value = json.dumps({
                "size": int(success["size"]),
                "measured_at": int(success["measured_at"]),
                "method": success["method"],
                "allocated": success["allocated"],
                "detail": success["detail"],
            }, separators=(",", ":"))
            db.execute("INSERT INTO metadata(key,value) VALUES(?,?)",
                       ("influx_last_success", value))
        if "influx_last_attempt_at" in keys:
            return
        candidate = int(success["measured_at"]) if success is not None else None
        latest = db.execute("""SELECT hour,status FROM snapshots
            WHERE source='influx_addon' ORDER BY hour DESC LIMIT 1""").fetchone()
        if latest is not None and latest["status"] == "unavailable":
            uncertain_upper_bound = int(latest["hour"]) + HOUR - 1
            candidate = (uncertain_upper_bound if candidate is None else
                         max(candidate, uncertain_upper_bound))
        if candidate is not None:
            db.execute("INSERT INTO metadata(key,value) VALUES(?,?)",
                       ("influx_last_attempt_at", str(candidate)))

    def save(self, samples: Iterable[Sample]):
        rows = list(samples)
        if not rows:
            return
        with _raw_connection(self.path) as db:
            retired={row[0] for row in db.execute(
                "SELECT source FROM source_lifecycle WHERE retired=1")}
            rows=[sample for sample in rows if sample.source not in retired]
            if not rows:
                return
            db.executemany("""INSERT INTO snapshots
               (hour,source,size,status,method,measured_at,allocated,detail)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(hour,source) DO UPDATE SET
                 size=excluded.size, status=excluded.status,
                 method=excluded.method, measured_at=excluded.measured_at,
                 allocated=excluded.allocated, detail=excluded.detail
               WHERE NOT (snapshots.status='ok' AND excluded.status!='ok')
             """, [(s.hour,s.source,s.size,s.status,s.method,s.measured_at,s.allocated,s.detail[:150]) for s in rows])
            # Durable source identity survives snapshot retention. This lets an
            # optional source distinguish "never configured" from "measured
            # before, now missing" without consulting any production database.
            successful_sources = {sample.source for sample in rows
                                  if sample.status == "ok" and
                                  sample.size is not None}
            db.executemany("""INSERT INTO metadata(key,value) VALUES(?, '1')
                ON CONFLICT(key) DO UPDATE SET value='1'""",
                [("source_ever_ok:" + source,) for source in successful_sources])

    def reserve_influx_probe(self, now: int, minimum_elapsed: int = 6 * HOUR) -> bool:
        """Atomically reserve one expensive Supervisor scan.

        The attempt is persisted before network I/O so a failed probe or quick
        Home Assistant restart cannot immediately repeat the directory scan.
        """
        now = int(now)
        with _raw_connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            # Reconcile an older unfinished reservation before considering a
            # new one.  This is especially important after a long HA outage:
            # otherwise each new reservation could overwrite the only proof
            # that the preceding Supervisor request was interrupted.
            self._reconcile_influx_interruption(
                db, now, INFLUX_PROBE_ABANDONED_AFTER)
            row = db.execute(
                "SELECT value FROM metadata WHERE key='influx_last_attempt_at'"
            ).fetchone()
            if row is not None:
                try:
                    if now - int(row[0]) < int(minimum_elapsed):
                        return False
                except (TypeError, ValueError):
                    # Replace malformed legacy metadata; do not lose all
                    # storage sampling because one monitor-owned key is bad.
                    pass
            db.execute("""INSERT INTO metadata(key,value)
                VALUES('influx_last_attempt_at',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(now),))
            outcome = json.dumps({
                "attempted_at": now,
                "status": "in_progress",
                "detail": "Supervisor probe reserved; outcome not yet recorded",
            }, separators=(",", ":"))
            db.execute("""INSERT INTO metadata(key,value)
                VALUES('influx_last_attempt_outcome',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (outcome,))
            return True

    @staticmethod
    def _reconcile_influx_interruption(db, now: int,
                                       abandoned_after: int) -> bool:
        """Durably finish an abandoned reservation as ``interrupted``.

        A two-minute grace exceeds the Supervisor HTTP client's 65-second
        total timeout.  Recent reservations therefore remain ``in_progress``
        during a legitimate request or an immediate Home Assistant restart.
        """
        row = db.execute("""SELECT value FROM metadata
            WHERE key='influx_last_attempt_outcome'""").fetchone()
        if row is None:
            return False
        try:
            outcome = json.loads(row[0])
            attempted_at = int(outcome["attempted_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False
        if (not isinstance(outcome, dict)
                or outcome.get("status") != "in_progress"
                or int(now) - attempted_at < int(abandoned_after)):
            return False
        failure_row = db.execute("""SELECT value FROM metadata
            WHERE key='influx_consecutive_failures'""").fetchone()
        try:
            failures = max(0, int(failure_row[0])) if failure_row else 0
        except (TypeError, ValueError):
            failures = 0
        detail = ("Supervisor probe was interrupted before an outcome was "
                  "recorded; no new measurement was accepted")
        interrupted = {
            "attempted_at": attempted_at,
            "status": "interrupted",
            "detail": detail,
            "detected_at": int(now),
        }
        encoded = json.dumps(interrupted, separators=(",", ":"))
        db.execute("""INSERT INTO metadata(key,value)
            VALUES('influx_last_attempt_outcome',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (encoded,))
        db.execute("""INSERT INTO metadata(key,value)
            VALUES('influx_last_interruption',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (encoded,))
        db.execute("""INSERT INTO metadata(key,value)
            VALUES('influx_consecutive_failures',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(failures + 1),))
        return True

    def save_influx_result(self, attempted_at: int, sample: Sample):
        """Persist an eligible probe outcome and consecutive-failure state."""
        if sample.source != "influx_addon" or sample.status not in ("ok", "unavailable"):
            raise ValueError("Invalid InfluxDB probe result")
        if sample.status == "ok" and (sample.size is None or sample.measured_at is None):
            raise ValueError("A successful probe requires a genuine measurement")
        attempted_at = int(attempted_at)
        with _raw_connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            outcome_row = db.execute("""SELECT value FROM metadata
                WHERE key='influx_last_attempt_outcome'""").fetchone()
            current_outcome = None
            if outcome_row is not None:
                try:
                    parsed = json.loads(outcome_row[0])
                    if isinstance(parsed, dict):
                        current_outcome = parsed
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            row = db.execute("""SELECT value FROM metadata
                WHERE key='influx_consecutive_failures'""").fetchone()
            try:
                previous_failures = max(0, int(row[0])) if row else 0
            except (TypeError, ValueError):
                previous_failures = 0
            success = sample.status == "ok"
            already_counted = bool(
                current_outcome
                and current_outcome.get("attempted_at") == attempted_at
                and current_outcome.get("status") in ("failure", "interrupted"))
            failures = (0 if success else previous_failures if already_counted
                        else previous_failures + 1)
            outcome = json.dumps({
                "attempted_at": attempted_at,
                "status": "success" if success else "failure",
                "detail": str(sample.detail)[:300],
            }, separators=(",", ":"))
            db.execute("""INSERT INTO metadata(key,value)
                VALUES('influx_last_attempt_outcome',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (outcome,))
            db.execute("""INSERT INTO metadata(key,value)
                VALUES('influx_consecutive_failures',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(failures),))
            if success:
                value = json.dumps({
                    "size": int(sample.size),
                    "measured_at": int(sample.measured_at),
                    "method": sample.method,
                    "allocated": sample.allocated,
                    "detail": sample.detail,
                }, separators=(",", ":"))
                db.execute("""INSERT INTO metadata(key,value)
                    VALUES('influx_last_success',?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (value,))

    def save_influx_success(self, sample: Sample):
        """Compatibility wrapper for a genuine successful probe."""
        self.save_influx_result(int(sample.measured_at or sample.hour), sample)

    def influx_probe_state(self, now: int | None = None,
                           abandoned_after: int = INFLUX_PROBE_ABANDONED_AFTER) -> dict:
        """Return durable state and optionally reconcile an abandoned probe."""
        with _raw_connection(self.path) as db:
            if now is not None:
                db.execute("BEGIN IMMEDIATE")
                self._reconcile_influx_interruption(
                    db, int(now), int(abandoned_after))
            rows = dict(db.execute("""SELECT key,value FROM metadata
                WHERE key IN ('influx_last_attempt_at','influx_last_success',
                              'influx_last_attempt_outcome','influx_consecutive_failures',
                              'influx_last_interruption')""").fetchall())
        attempt = None
        success = None
        outcome = None
        interruption = None
        failures = 0
        try:
            attempt = int(rows["influx_last_attempt_at"])
        except (KeyError, TypeError, ValueError):
            pass
        try:
            parsed = json.loads(rows["influx_last_success"])
            if (isinstance(parsed, dict) and isinstance(parsed.get("size"), int)
                    and isinstance(parsed.get("measured_at"), int)):
                success = parsed
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            parsed = json.loads(rows["influx_last_attempt_outcome"])
            if (isinstance(parsed, dict)
                    and parsed.get("status") in
                        ("in_progress", "success", "failure", "interrupted")
                    and isinstance(parsed.get("attempted_at"), int)):
                outcome = parsed
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            parsed = json.loads(rows["influx_last_interruption"])
            if (isinstance(parsed, dict)
                    and parsed.get("status") == "interrupted"
                    and isinstance(parsed.get("attempted_at"), int)
                    and isinstance(parsed.get("detected_at"), int)):
                interruption = parsed
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            failures = max(0, int(rows.get("influx_consecutive_failures", "0")))
        except (TypeError, ValueError):
            failures = 0
        return {"last_attempt_at": attempt, "last_success": success,
                "last_attempt_outcome": outcome,
                "last_interruption": interruption,
                "consecutive_failures": failures}

    def latest(self):
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT s.* FROM snapshots s
               JOIN (SELECT source, MAX(hour) hour FROM snapshots GROUP BY source) m
               ON m.source=s.source AND m.hour=s.hour ORDER BY s.source""").fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _validate_retirable_source(source: str):
        if (not isinstance(source, str) or len(source) > 512 or
                not source.startswith("database:") or "\x00" in source):
            raise ValueError("Only observed local SQLite sources may be retired")

    def retire_source(self, source: str, now: int) -> dict:
        """Retire one observed local SQLite source in monitor metadata only."""
        self._validate_retirable_source(source)
        with _raw_connection(self.path) as db:
            observed = db.execute(
                "SELECT 1 FROM snapshots WHERE source=? LIMIT 1", (source,)
            ).fetchone()
            if observed is None:
                raise ValueError("Source has no Storage Monitor history")
            db.execute("""INSERT INTO source_lifecycle
                (source,retired,retired_at,reactivated_at,last_detected_at)
                VALUES(?,1,?,NULL,NULL)
                ON CONFLICT(source) DO UPDATE SET retired=1,
                  retired_at=CASE WHEN source_lifecycle.retired=1
                    THEN source_lifecycle.retired_at ELSE excluded.retired_at END,
                  reactivated_at=NULL,
                  last_detected_at=CASE WHEN source_lifecycle.retired=1
                    THEN source_lifecycle.last_detected_at ELSE NULL END""",
                (source, int(now)))
            db.execute("""UPDATE alerts SET active=0,opened_at=NULL
                WHERE source IN (?,?)""", (source, "stale:" + source))
            retired_at=db.execute("""SELECT retired_at FROM source_lifecycle
                WHERE source=?""", (source,)).fetchone()[0]
        return {"source": source, "retired": True,
                "retired_at": int(retired_at)}

    def reactivate_source(self, source: str, now: int) -> dict:
        """Explicitly reactivate; old snapshots stay historical evidence only."""
        self._validate_retirable_source(source)
        with _raw_connection(self.path) as db:
            changed = db.execute("""UPDATE source_lifecycle
                SET retired=0,reactivated_at=?
                WHERE source=? AND retired=1""", (int(now), source)).rowcount
            if changed != 1:
                raise ValueError("Source is not retired")
        return {"source": source, "retired": False,
                "reactivated_at": int(now)}

    def retired_sources(self) -> list[dict]:
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT source,retired_at,last_detected_at
                FROM source_lifecycle WHERE retired=1
                ORDER BY retired_at DESC,source""").fetchall()
            return [dict(row) for row in rows]

    def retired_source_keys(self) -> set[str]:
        return {row["source"] for row in self.retired_sources()}

    def note_retired_detected(self, sources: Iterable[str], now: int):
        keys = list(set(sources))
        if not keys:
            return
        with _raw_connection(self.path) as db:
            db.executemany("""UPDATE source_lifecycle SET last_detected_at=?
                WHERE source=? AND retired=1""",
                [(int(now), source) for source in keys])

    def reactivated_at(self, source: str) -> int | None:
        with _raw_connection(self.path) as db:
            row = db.execute("""SELECT reactivated_at FROM source_lifecycle
                WHERE source=? AND retired=0""", (source,)).fetchone()
            return int(row[0]) if row and row[0] is not None else None

    def latest_source_membership_change(self) -> int | None:
        with _raw_connection(self.path) as db:
            row=db.execute("""SELECT MAX(changed_at) FROM (
                SELECT retired_at AS changed_at FROM source_lifecycle
                UNION ALL
                SELECT reactivated_at AS changed_at FROM source_lifecycle
            )""").fetchone()
            return int(row[0]) if row and row[0] is not None else None

    def source_was_successful(self, source: str) -> bool:
        """Return durable success knowledge, seeding it from legacy history."""
        key = "source_ever_ok:" + source
        with _raw_connection(self.path) as db:
            known = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
            if known is not None:
                return known[0] == "1"
            historical = db.execute("""SELECT 1 FROM snapshots
                WHERE source=? AND status='ok' AND size IS NOT NULL LIMIT 1""",
                (source,)).fetchone()
            value = "1" if historical is not None else "0"
            db.execute("""INSERT INTO metadata(key,value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, value))
            return historical is not None

    def history(self, source: str, since: int, limit: int = 2200):
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("""SELECT hour, size, status, measured_at, allocated
            FROM snapshots WHERE source=? AND hour>=?
            ORDER BY hour DESC LIMIT ?""", (source, since, min(limit, 2200))).fetchall()
            return [dict(r) for r in reversed(rows)]

    def growth_evidence(self, source: str, now: int, *, hourly_source: bool) -> dict:
        """Return bounded growth evidence from OUR small SQLite history only."""
        now = utc_hour(now)
        start = now - 8 * DAY
        with _raw_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            # At most 193 rows: eight elapsed days plus the current boundary.
            rows=[dict(row) for row in db.execute("""
                SELECT hour,size,status,measured_at,detail FROM snapshots
                WHERE source=? AND hour>=? AND hour<=?
                ORDER BY hour LIMIT 193
            """, (source,start,now))]
        genuine=[row for row in rows if row["status"]=="ok" and
                 row["size"] is not None and row["measured_at"] is not None and
                 abs(int(row["hour"])-int(row["measured_at"]))<=25*60]
        genuine_by_hour={int(row["hour"]):row for row in genuine}
        points=[row for row in rows if (int(row["hour"])-now)%(6*HOUR)==0]
        required_hours=list(range(start,now+1,6*HOUR))
        missing_hours=[hour for hour in required_hours
                       if hour not in genuine_by_hour]
        checkpoint_rows={int(row["hour"]):row for row in points}
        latest=checkpoint_rows.get(now)
        latest_status=("missing" if latest is None else
                       "off_checkpoint" if latest["status"]=="ok" and
                       now not in genuine_by_hour else str(latest["status"]))

        last_shrink=None
        for previous,current in zip(genuine,genuine[1:]):
            if int(current["size"]) < int(previous["size"]):
                last_shrink={
                    "previous_hour":int(previous["hour"]),
                    "hour":int(current["hour"]),
                    "from_size":int(previous["size"]),
                    "to_size":int(current["size"]),
                }

        day_coverage=[]
        hourly_verified_total=0
        missing_hourly=[]
        for index in range(8):
            left=start+index*DAY
            right=left+DAY
            expected=(list(range(left,right,HOUR)) if hourly_source else
                      list(range(left,right,6*HOUR)))
            verified=sum(1 for hour in expected if hour in genuine_by_hour)
            required=20 if hourly_source else 4
            if hourly_source:
                hourly_verified_total+=verified
                missing_hourly.extend(hour for hour in expected
                                      if hour not in genuine_by_hour)
            checkpoints=list(range(left,right,6*HOUR))
            complete=(verified>=required and
                      all(hour in genuine_by_hour for hour in checkpoints) and
                      right in genuine_by_hour)
            day_coverage.append({"start":left,"end":right,
                                 "verified":verified,"required":required,
                                 "complete":complete})
        coverage_ok=all(day["verified"]>=day["required"]
                        for day in day_coverage)
        candidates=[]
        if genuine:
            candidates.append(int(genuine[0]["hour"])+8*DAY)
        if last_shrink:
            candidates.append(int(last_shrink["hour"])+8*DAY+1)
        if missing_hours:
            candidates.append(max(missing_hours)+8*DAY+1)
        if missing_hourly:
            candidates.append(max(missing_hourly)+8*DAY+1)
        return {
            "points":points,
            "observed_shrink":last_shrink is not None,
            "last_shrink":last_shrink,
            "hourly_coverage_ok":coverage_ok,
            "day_coverage":day_coverage,
            "verified_days":sum(1 for day in day_coverage if day["complete"]),
            "verified_checkpoints":len(required_hours)-len(missing_hours),
            "required_checkpoints":len(required_hours),
            "missing_required_measurements":len(missing_hours),
            "missing_checkpoint_hours":missing_hours,
            "hourly_verified_measurements":(
                hourly_verified_total if hourly_source else None),
            "hourly_required_measurements":160 if hourly_source else None,
            "latest_checkpoint_status":latest_status,
            "baseline_eligible_not_before":max(candidates) if candidates else None,
        }

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
            lifecycle_source=(source[6:] if source.startswith("stale:")
                              else source)
            if active and lifecycle_source.startswith("database:"):
                retired=db.execute("""SELECT 1 FROM source_lifecycle
                    WHERE source=? AND retired=1""",
                    (lifecycle_source,)).fetchone()
                if retired is not None:
                    active=False
                    notified=False
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

    def configure_writer_diagnostics(self, enabled: bool, now: int) -> int | None:
        """Persist opt-in state and return when the current enabled run began.

        Ordinary restarts/reloads preserve the original timestamp. An explicit
        disable followed by re-enable starts a new run, preventing a later
        checkpoint from querying a Recorder interval that predates consent.
        """
        with _raw_connection(self.path) as db:
            current = db.execute(
                "SELECT value FROM metadata WHERE key='writer_diagnostics_enabled'"
            ).fetchone()
            if not enabled:
                db.execute("""INSERT INTO metadata(key,value)
                    VALUES('writer_diagnostics_enabled','0')
                    ON CONFLICT(key) DO UPDATE SET value='0'""")
                db.execute("DELETE FROM metadata WHERE key='writer_diagnostics_enabled_at'")
                return None
            if current and current[0] == "1":
                existing = db.execute(
                    "SELECT value FROM metadata WHERE key='writer_diagnostics_enabled_at'"
                ).fetchone()
                if existing:
                    return int(existing[0])
            enabled_at = int(now)
            db.execute("""INSERT INTO metadata(key,value)
                VALUES('writer_diagnostics_enabled','1')
                ON CONFLICT(key) DO UPDATE SET value='1'""")
            db.execute("""INSERT INTO metadata(key,value)
                VALUES('writer_diagnostics_enabled_at',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(enabled_at),))
            return enabled_at

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


def disk_low_evidence(free_bytes: int | None, recorder_bytes: int | None,
                      configured_bytes: int) -> tuple[bool | None, int]:
    """Return an independent disk alarm decision and its effective threshold."""
    threshold = max(max(0, int(configured_bytes)), max(0, int(recorder_bytes or 0)))
    return (None if free_bytes is None else int(free_bytes) < threshold), threshold


def evaluate(source: str, samples: list[dict], now: int, *, min_excess_bytes: int = 0,
             cadence_hours: int = 1, observed_shrink: bool = False,
             hourly_coverage_ok: bool = True,
             diagnostics: dict | None = None) -> Evaluation:
    """Evaluate only complete measured UTC windows, never cached/interpolated values.

    Exactly 8 elapsed days of coverage is required. Each previous day needs
    >=20 of 24 measured hourly points (>=4 of 4 for 6h collection).
    Any *observed* size decrease between verified hourly/6h readings in
    the eight-day interval marks maintenance, even if daily net growth is
    positive after a purge followed by regrowth. Unobserved purges cannot
    be inferred from hourly snapshots.
    """
    now = utc_hour(now)
    diagnostics=diagnostics or {}
    measured = {int(x["hour"]): int(x["size"]) for x in samples
                if x.get("size") is not None and x.get("status")=="ok"
                and x.get("measured_at") is not None
                and abs(int(x["hour"])-int(x["measured_at"])) <= 25*60}
    step = max(1,cadence_hours)*HOUR
    required_hours=list(range(now-8*DAY,now+1,step))
    missing=[hour for hour in required_hours if hour not in measured]
    required_checkpoints=int(diagnostics.get("required_checkpoints",
                                              len(required_hours)))
    verified_checkpoints=int(diagnostics.get(
        "verified_checkpoints",required_checkpoints-len(missing)))
    missing_required=int(diagnostics.get(
        "missing_required_measurements",len(missing)))
    required_per_day=20 if cadence_hours==1 else max(1,math.ceil(.8*DAY/step))
    derived_days=0
    for index in range(8):
        left=now-8*DAY+index*DAY
        count=sum(1 for hour in range(left,left+DAY,step) if hour in measured)
        if (count>=required_per_day and left in measured and
                left+DAY in measured):
            derived_days+=1
    verified_days=int(diagnostics.get("verified_days",derived_days))
    common={
        "verified_days":verified_days,
        "verified_checkpoints":verified_checkpoints,
        "required_checkpoints":required_checkpoints,
        "missing_required_measurements":missing_required,
        "hourly_verified_measurements":diagnostics.get(
            "hourly_verified_measurements"),
        "hourly_required_measurements":diagnostics.get(
            "hourly_required_measurements"),
        "baseline_eligible_not_before":diagnostics.get(
            "baseline_eligible_not_before"),
        "latest_checkpoint_status":diagnostics.get(
            "latest_checkpoint_status"),
    }
    bounds = [now - x*DAY for x in range(9)]
    delta24=(measured[bounds[0]]-measured[bounds[1]]
             if bounds[0] in measured and bounds[1] in measured else None)

    # Check every pair of consecutive genuine observations. Missing rows are
    # never sizes and therefore cannot themselves be interpreted as shrinkage.
    observed = sorted((hour, size) for hour, size in measured.items()
                      if now - 8*DAY <= hour <= now)
    derived_shrink=None
    for (previous_hour,earlier),(hour,later) in zip(observed,observed[1:]):
        if later < earlier:
            derived_shrink={"previous_hour":previous_hour,"hour":hour,
                            "from_size":earlier,"to_size":later}
    shrink=diagnostics.get("last_shrink") or derived_shrink
    if shrink:
        common.update({"last_shrink_at":int(shrink["hour"]),
                       "last_shrink_from":int(shrink["from_size"]),
                       "last_shrink_to":int(shrink["to_size"])})
    forced_reason=diagnostics.get("forced_reason")
    if forced_reason:
        return Evaluation(source,"learning",delta24=delta24,
                          evidence=str(forced_reason),
                          reason_code=str(diagnostics.get(
                              "forced_reason_code") or "learning"),**common)
    if observed_shrink or shrink:
        return Evaluation(source,"maintenance",delta24=delta24,
                          evidence=("Observed size decrease within the rolling "
                                    "eight-day interval; waiting for a clean baseline"),
                          reason_code="observed_shrink",**common)

    latest_status=common["latest_checkpoint_status"]
    # Runtime growth evaluation uses six-hour checkpoint evidence. Direct
    # hourly evaluation deliberately permits up to four missing observations
    # per UTC day as long as all daily boundaries remain genuine.
    if missing_required and cadence_hours>1:
        if latest_status=="cached":
            reason="Latest required checkpoint is cached, not a fresh measurement"
            code="latest_cached"
        elif latest_status=="off_checkpoint":
            reason=("Latest measurement occurred more than 25 minutes from "
                    "the required UTC checkpoint")
            code="latest_off_checkpoint"
        elif latest_status in ("unavailable","stale","missing"):
            label="missing" if latest_status=="missing" else latest_status
            reason=f"Latest required checkpoint is {label}, not a fresh measurement"
            code="latest_"+str(latest_status)
        else:
            reason=(f"Missing {missing_required} required "
                    f"{cadence_hours}-hour checkpoint"
                    f"{'s' if missing_required!=1 else ''}")
            code="missing_checkpoints"
        return Evaluation(source,"learning",delta24=delta24,evidence=reason,
                          reason_code=code,**common)
    if not hourly_coverage_ok:
        verified=common["hourly_verified_measurements"]
        required=common["hourly_required_measurements"]
        detail=(f" ({verified}/{required} hourly measurements)"
                if verified is not None and required is not None else "")
        return Evaluation(source,"learning",delta24=delta24,
                          evidence=("Insufficient verified hourly coverage; need "
                                    ">=20/24 per UTC day"+detail),
                          reason_code="hourly_coverage",**common)
    if any(t not in measured for t in bounds):
        return Evaluation(source,"learning",delta24=delta24,
                          evidence="Fresh samples are required at all nine 24h UTC boundaries",
                          reason_code="missing_boundaries",**common)
    for i in range(8):
        left, right = bounds[i+1], bounds[i]
        count = sum(1 for t in range(left, right, step) if t in measured)
        if count < required_per_day:
            return Evaluation(source,"learning",delta24=delta24,
                              evidence=(f"Incomplete measured coverage in UTC day "
                                        f"window {i+1}: {count}/{DAY//step}"),
                              reason_code="day_coverage",**common)
    daily_deltas = [measured[bounds[i]]-measured[bounds[i+1]] for i in range(8)]
    if any(v<0 for v in daily_deltas):
        return Evaluation(source,"maintenance",delta24=daily_deltas[0],
                          evidence="Database file shrank in a daily window; waiting for a clean baseline",
                          reason_code="observed_shrink",**common)
    delta24=daily_deltas[0]
    baseline = sum(daily_deltas[1:])/7.0
    if baseline<=0:
        zero_common={**common,"baseline_eligible_not_before":None}
        return Evaluation(source,"learning",delta24=delta24,
                          evidence="Previous seven-day baseline is zero or negative",
                          reason_code="zero_or_negative_baseline",**zero_common)
    excess_ratio = delta24/baseline-1
    if delta24>baseline*1.05 and delta24-baseline > min_excess_bytes:
        return Evaluation(source,"alarm",delta24,baseline,excess_ratio,
                          "Strictly more than 5% above previous 7-day average daily growth",
                          "alarm",**common)
    return Evaluation(source,"normal",delta24,baseline,excess_ratio,
                      "Valid eight-day comparison is within the configured threshold",
                      "normal",**common)


def allow_notification(alert: dict, window: str, now: int) -> bool:
    """Persisted per-source per-window cooldown (handles HA restart)."""
    return (alert.get("last_window") != window
            and int(alert.get("acknowledged_until") or 0) <= now
            and (alert.get("last_notify_at") is None or now-int(alert["last_notify_at"])>=6*HOUR))
