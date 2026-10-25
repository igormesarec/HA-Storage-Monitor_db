"""Bounded, read-only diagnosis of entities creating Recorder state rows."""
from __future__ import annotations
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote

INDEX = "ix_states_last_updated_ts"


def top_writers(recorder_file: str, now: int, *, budget_seconds: float = 4.0) -> dict:
    """Use existing time index, refuse a full scan, stop if query exceeds budget.

    WARNING: state row count is NOT per-integration stored bytes. 8-day
    aggregation is scheduled only once each six hours, never on UI rendering.
    """
    target = Path(recorder_file)
    if not target.is_file() or target.is_symlink():
        return {"status":"unavailable","reason":"Recorder SQLite file absent","entities":[]}
    dburi = "file:" + quote(str(target), safe="/") + "?mode=ro"
    try:
        with sqlite3.connect(dburi,uri=True,timeout=1) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=1000")
            indices = {r[1] for r in db.execute("PRAGMA index_list('states')")}
            if INDEX not in indices:
                return {"status":"unavailable","reason":"No suitable time index; refusing full scan","entities":[]}
            deadline = time.monotonic() + budget_seconds
            db.set_progress_handler(lambda: 1 if time.monotonic()>deadline else 0,5000)
            # Aggregate raw writes in 8 days. At most 20 entities returned.
            results = db.execute(f"""
                SELECT m.entity_id,
                  SUM(CASE WHEN s.last_updated_ts>=? THEN 1 ELSE 0 END) AS last_hour,
                  SUM(CASE WHEN s.last_updated_ts>=? THEN 1 ELSE 0 END) AS today,
                  SUM(CASE WHEN s.last_updated_ts<? THEN 1 ELSE 0 END) AS prev7,
                  SUM(SUM(CASE WHEN s.last_updated_ts>=? THEN 1 ELSE 0 END)) OVER() AS total24
                FROM states s INDEXED BY {INDEX}
                JOIN states_meta m ON m.metadata_id=s.metadata_id
                WHERE s.last_updated_ts>=? AND s.last_updated_ts<?
                GROUP BY s.metadata_id
                ORDER BY today DESC LIMIT 20
              """, (now-3600,now-86400,now-86400,now-86400,now-8*86400,now)).fetchall()
            db.set_progress_handler(None,0)
            entities=[]
            total = int(results[0][4]) if results else 0
            for name,last_hour,last24,prev7,total_rows in results:
                baseline = prev7/7.0
                entities.append({"entity_id":name,"rows_1h":last_hour,"rows_24h":last24,
                                 "previous7_daily":round(baseline,1),
                                 "rate_per_min":round(last24/1440,3),
                                 "share_percent":round(100*last24/total,2) if total else 0,
                                 "high_writer":bool(last24>100_000 or (baseline>0 and last24>10*baseline)),
                                 "ratio":round(last24/baseline,2) if baseline>0 else None})
            return {"status":"ok","entities":entities,"all_recorder_rows_24h":total,
                    "caveat":"Physical bytes cannot be attributed to an entity from state-row counts"}
    except (sqlite3.Error,OSError) as exc:
        return {"status":"unavailable","reason":f"Read-only SQL unavailable or time budget exceeded: {type(exc).__name__}","entities":[]}
