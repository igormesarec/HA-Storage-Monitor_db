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
import time
from urllib.parse import quote
from zoneinfo import ZoneInfo

INDEX = "ix_states_last_updated_ts"
TZ = ZoneInfo("Europe/Ljubljana")
HOUR = 3600
# Fail rather than silently truncate entity rankings or inflate monitor storage.
MAX_ENTITIES = 5000
MAX_JSON_BYTES = 256 * 1024


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
                          max_json_bytes: int = MAX_JSON_BYTES) -> dict:
    """Collect a COMPLETE interval, or return unavailable (never partial).

    Uses the existing Recorder timestamp index and SQLite progress handler.
    No CREATE/UPDATE/PRAGMA changes to Recorder; no new production indexes.
    A time limit bounds execution time, not absolute SQLite memory usage.
    """
    if not 0 < end - start <= 7 * HOUR:
        return _failed(start, end, "Invalid or overly broad time window")
    if budget_seconds <= 0:
        return _failed(start, end, "Nonpositive execution budget")
    target = Path(recorder_file)
    if target.is_symlink() or not target.is_file():
        return _failed(start, end, "Recorder SQLite file unavailable")
    uri = "file:" + quote(str(target), safe="/") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=500")
            # Keep GROUP BY's temporary B-tree in process memory, not in a
            # potentially large on-disk temp file. Connection-local only.
            db.execute("PRAGMA temp_store=MEMORY")
            indexes = {r[1] for r in db.execute("PRAGMA index_list('states')")}
            if INDEX not in indexes:
                return _failed(start, end, "Required time index missing; scan refused")
            deadline = time.monotonic() + budget_seconds
            db.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
            try:
                # GROUP BY necessarily aggregates qualifying rows. No ORDER BY,
                # no 8-day scan, and no LIMIT that would hide incomplete totals.
                cursor = db.execute(f"""
                    SELECT m.entity_id, COUNT(*)
                    FROM states AS s INDEXED BY {INDEX}
                    JOIN states_meta AS m ON m.metadata_id = s.metadata_id
                    WHERE s.last_updated_ts >= ? AND s.last_updated_ts < ?
                    GROUP BY s.metadata_id
                """, (start, end))
                counts: dict[str, int] = {}
                for entity_id, count in cursor:
                    if len(counts) >= max_entities:
                        return _failed(start, end, "Entity count safety cap reached; not a complete ranking")
                    counts[str(entity_id)] = int(count)
                payload = json.dumps(counts, separators=(",", ":"))
                if len(payload.encode("utf-8")) > max_json_bytes:
                    return _failed(start, end, "Summary storage safety cap reached")
                return {"start_ts": start, "end_ts": end, "status": "ok",
                        "reason": "", "total_rows": sum(counts.values()), "counts": counts}
            finally:
                db.set_progress_handler(None, 0)
    except (sqlite3.Error, OSError) as err:
        return _failed(start, end, f"Read-only query failed or timed out: {type(err).__name__}")


def summarize_writer_windows(rows: list[dict], end_ts: int) -> dict:
    """Summarize four saved local checkpoints/day over eight real days.

    Each interval is checked for exact UTC start/end and success. DST days
    contain 23 or 25 elapsed hours, which is disclosed. This diagnostic is
    not used for the strict 24-hour STORAGE SIZE growth alert.
    """
    end_local = datetime.fromtimestamp(end_ts, TZ)
    if end_local.hour not in (0, 6, 12, 18) or end_local.minute != 0:
        return {"status": "unavailable", "reason": "Not at a scheduled checkpoint", "entities": []}
    by_end = {int(row["end_ts"]): row for row in rows}
    valid: list[dict | None] = []
    for i in range(32):
        checkpoint = end_local - timedelta(hours=6 * i)
        expected_start, expected_end = local_checkpoint_window(checkpoint)
        row = by_end.get(expected_end)
        if (row is None or row["status"] != "ok" or
                int(row["start_ts"]) != expected_start):
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
    for name, count in sorted(rank.items(), key=lambda x: (-x[1], x[0]))[:20]:
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
        "last24_complete": last24 is not None,
        "previous7_complete": previous7 is not None,
        "last24_elapsed_hours": last24_hours,
        "ranking_period": period,
        "all_recorder_rows_24h": last24[1] if last24 else None,
        "entities": entities,
        "caveat": "Recorder state ROW counts, not physical bytes. Last four local checkpoints span 23/25h on DST transitions; missing windows are never interpolated.",
    }
