"""Read-only, bounded Recorder diagnostics, one local six-hour window at a time.

Never scan the preceding eight days in Recorder. Only one interval is read
at each 00/06/12/18 Europe/Ljubljana checkpoint. Persist compact counts in
Storage Monitor's OWN SQLite and compute trends from those saved records.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from time import monotonic
from urllib.parse import quote
from zoneinfo import ZoneInfo

INDEX = "ix_states_last_updated_ts"
TZ = ZoneInfo("Europe/Ljubljana")
HOUR = 3600
# Fail rather than silently truncate entity rankings or inflate monitor storage.
MAX_ENTITIES = 5000
MAX_JSON_BYTES = 256 * 1024
# A hard cap on rows visited avoids sustained Recorder reads during a write storm.
MAX_ROWS = 500_000
WRITER_RANKING_LIMITS = (20, 50, 100, 500)
WRITER_PAGE_SIZE = 50


def local_checkpoint_window(when: datetime) -> tuple[int, int]:
    """Previous local 6-hour checkpoint to this one; 5/7 elapsed h on DST.

    Local checkpoint hours 00, 06, 12, 18 are unambiguous in Ljubljana.
    UTC epoch boundaries preserve real elapsed time without double-counting.
    """
    local = when.astimezone(TZ).replace(minute=0, second=0, microsecond=0)
    if local.hour not in (0, 6, 12, 18):
        raise ValueError("Not a local six-hour checkpoint")
    previous = local - timedelta(hours=6)
    start, end = int(previous.timestamp()), int(local.timestamp())
    if not 5 * HOUR <= end - start <= 7 * HOUR:
        raise ValueError("Unexpected checkpoint duration")
    return start, end


def _failed(start: int, end: int, reason: str) -> dict:
    return {"start_ts": start, "end_ts": end, "status": "unavailable",
            "reason": reason, "total_rows": None, "counts": {}}


def collect_writer_window(recorder_file: str, start: int, end: int, *,
                          budget_seconds: float = 4.0,
                          max_entities: int = MAX_ENTITIES,
                          max_json_bytes: int = MAX_JSON_BYTES,
                          max_rows: int = MAX_ROWS) -> dict:
    """Collect a COMPLETE interval, or return unavailable (never partial).

    Uses the existing Recorder timestamp index and SQLite progress handler.
    No CREATE/UPDATE/PRAGMA changes to Recorder; no new production indexes.
    Stream matching entity IDs instead of creating a GROUP BY temporary B-tree.
    Bound wall time, scanned rows, distinct entities, and serialized output.
    Failure produces an unavailable interval, never an incomplete ranking.
    """
    if not 0 < end - start <= 7 * HOUR:
        return _failed(start, end, "Invalid or overly broad time window")
    if budget_seconds <= 0:
        return _failed(start, end, "Nonpositive execution budget")
    if max_rows < 1 or max_entities < 1 or max_json_bytes < 1:
        return _failed(start, end, "Invalid safety cap")
    target = Path(recorder_file)
    if target.is_symlink() or not target.is_file():
        return _failed(start, end, "Recorder SQLite file unavailable")
    uri = "file:" + quote(str(target), safe="/") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=500")
            indexes = {r[1] for r in db.execute("PRAGMA index_list('states')")}
            if INDEX not in indexes:
                return _failed(start, end, "Required time index missing; scan refused")
            deadline = monotonic() + budget_seconds
            db.set_progress_handler(lambda: 1 if monotonic() >= deadline else 0, 1000)
            try:
                if monotonic() >= deadline:
                    return _failed(start, end, "Recorder read time budget exceeded")
                # Index-bounded streaming query. No GROUP BY, ORDER BY, or
                # SQLite temporary B-tree; Python counters have explicit caps.
                cursor = db.execute(f"""
                    SELECT m.entity_id
                    FROM states AS s INDEXED BY {INDEX}
                    JOIN states_meta AS m ON m.metadata_id = s.metadata_id
                    WHERE s.last_updated_ts >= ? AND s.last_updated_ts < ?
                """, (start, end))
                counts: dict[str, int] = {}
                total_rows = 0
                for (entity_id,) in cursor:
                    total_rows += 1
                    if total_rows > max_rows:
                        return _failed(start, end, "Recorder row safety cap reached; no partial ranking")
                    # The SQLite progress handler checks the deadline during
                    # execution. Check it here as well between cursor steps.
                    if total_rows % 256 == 0 and monotonic() >= deadline:
                        return _failed(start, end, "Recorder read time budget exceeded")
                    name = str(entity_id)
                    if name not in counts and len(counts) >= max_entities:
                        return _failed(start, end, "Entity count safety cap reached; no partial ranking")
                    counts[name] = counts.get(name, 0) + 1
                if monotonic() >= deadline:
                    return _failed(start, end, "Recorder read time budget exceeded")
                payload = json.dumps(counts, separators=(",", ":"))
                if len(payload.encode("utf-8")) > max_json_bytes:
                    return _failed(start, end, "Summary storage safety cap reached")
                return {"start_ts": start, "end_ts": end, "status": "ok",
                        "reason": "", "total_rows": total_rows, "counts": counts}
            finally:
                db.set_progress_handler(None, 0)
    except (sqlite3.Error, OSError) as err:
        return _failed(start, end, f"Read-only query failed or timed out: {type(err).__name__}")


def collect_writer_checkpoint(recorder_file: str, start: int, end: int, *,
                              enabled: bool, enabled_at: int | None,
                              already_exists: bool = False,
                              collector=collect_writer_window) -> dict | None:
    """Gate the Recorder collector and refuse retroactive interval reads."""
    if not enabled or already_exists:
        return None
    if enabled_at is None or enabled_at > start:
        return _failed(start, end,
                       "Diagnostics enabled after interval start; retroactive query refused")
    return collector(recorder_file, start, end)


def disabled_writer_summary() -> dict:
    """Represent opt-out explicitly; never imply a verified zero-row result."""
    return {
        "status": "disabled",
        "reason": "Recorder writer diagnostics disabled",
        "entities": [],
        "intervals_verified": None,
        "intervals_required": 32,
        "intervals_missing": None,
        "intervals_failed": None,
        "intervals_mismatched": None,
        "last24_complete": False,
        "previous7_complete": False,
        "last24_elapsed_hours": None,
        "ranking_period": "disabled",
        "all_recorder_rows_24h": None,
        "caveat": "Recorder was not opened or queried for writer diagnostics.",
    }


def summarize_writer_windows(rows: list[dict], end_ts: int,
                             limit: int = 20) -> dict:
    """Summarize four saved local checkpoints/day over eight real days.

    Each interval is checked for exact UTC start/end and success. DST days
    contain 23 or 25 elapsed hours, which is disclosed. This diagnostic is
    not used for the strict 24-hour STORAGE SIZE growth alert.
    """
    if type(limit) is not int or limit not in WRITER_RANKING_LIMITS:
        raise ValueError("Writer ranking limit must be 20, 50, 100, or 500")
    end_local = datetime.fromtimestamp(end_ts, TZ)
    if end_local.hour not in (0, 6, 12, 18) or end_local.minute != 0:
        return {"status": "unavailable", "reason": "Not at a scheduled checkpoint", "entities": []}
    by_end = {int(row["end_ts"]): row for row in rows}
    valid: list[dict | None] = []
    missing_intervals = 0
    failed_intervals = 0
    mismatched_intervals = 0
    for i in range(32):
        checkpoint = end_local - timedelta(hours=6 * i)
        expected_start, expected_end = local_checkpoint_window(checkpoint)
        row = by_end.get(expected_end)
        if row is None:
            missing_intervals += 1
            valid.append(None)
        elif row["status"] != "ok":
            failed_intervals += 1
            valid.append(None)
        elif int(row["start_ts"]) != expected_start:
            mismatched_intervals += 1
            valid.append(None)
        else:
            valid.append(row)

    def totals(records: list[dict | None]) -> tuple[dict[str, int], int] | None:
        if any(x is None for x in records):
            return None
        result: dict[str, int] = {}
        total_rows = 0
        for rec in records:
            assert rec is not None
            total_rows += int(rec["total_rows"])
            for entity_id, count in rec["counts"].items():
                result[entity_id] = result.get(entity_id, 0) + int(count)
        return result, total_rows

    last6 = totals(valid[:1])
    last24 = totals(valid[:4])
    previous7 = totals(valid[4:32])
    available = sum(x is not None for x in valid)
    if last24:
        rank = last24[0]
        period = "last_four_checkpoints"
    elif last6:
        rank = last6[0]
        period = "last_checkpoint_only"
    else:
        rank = {}
        period = "unavailable"
    entities = []
    for name, count in sorted(rank.items(), key=lambda x: (-x[1], x[0]))[:limit]:
        prior = previous7[0].get(name, 0) / 7 if previous7 else None
        count24 = last24[0].get(name, 0) if last24 else None
        count6 = last6[0].get(name, 0) if last6 else None
        entities.append({
            "entity_id": name,
            "rows_6h": count6,
            "rows_24h": count24,
            "previous7_daily": round(prior, 1) if prior is not None else None,
            "ratio": round(count24 / prior, 2) if prior and count24 is not None else None,
            "share_percent": round(100 * count24 / last24[1], 2) if last24 and last24[1] else None,
            "rate_per_min": round(count24 / ((sum(int(x["end_ts"])-int(x["start_ts"]) for x in valid[:4])) / 60), 3)
                            if last24 and count24 is not None else None,
            "high_writer": bool(count24 is not None and
                                (count24 > 100_000 or (prior is not None and prior > 0 and count24 > 10 * prior))),
        })
    last24_hours = (sum(int(x["end_ts"])-int(x["start_ts"]) for x in valid[:4]) / HOUR
                    if last24 else None)
    return {
        "hour": end_ts,
        "status": "ok" if available == 32 else "learning",
        "reason": "Complete 8-day writer history" if available == 32
                  else f"Verified {available}/32 local six-hour intervals; no partial totals represented as full history",
        "intervals_verified": available,
        "intervals_required": 32,
        "intervals_missing": missing_intervals,
        "intervals_failed": failed_intervals,
        "intervals_mismatched": mismatched_intervals,
        "last24_complete": last24 is not None,
        "previous7_complete": previous7 is not None,
        "last24_elapsed_hours": last24_hours,
        "ranking_period": period,
        "selected_limit": limit,
        "ranked_entities_available": len(rank),
        "selected_entities_count": len(entities),
        "all_recorder_rows_24h": last24[1] if last24 else None,
        "entities": entities,
        "caveat": "Recorder state ROW counts, not physical bytes. Last four local checkpoints span 23/25h on DST transitions; missing windows are never interpolated.",
    }
