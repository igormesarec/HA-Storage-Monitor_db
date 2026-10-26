# HA OS Recorder writer diagnostic preflight (read-only)

This is a **manual**, non-destructive preflight for the Home Assistant OS Terminal & SSH add-on. Do not run it while Recorder is already under heavy load. It does **not** install Storage Monitor or touch the SI Grid Monitor integration. It uses the Recorder timestamp index, never an eight-day scan.

The schema/index and query plan were verified earlier on this installation. A repeat check on 2026-10-26 in the current `core-ssh` shell found that `command -v sqlite3` returned no path and `/usr/bin/sqlite3` did not exist. That newer observation means the performance probe is currently **blocked by missing tooling**. Do not install a package, enter another container, or use a privileged workaround without explicit owner approval.

## 1. Index/query-plan guard

First verify that the executable exists:

```sh
command -v sqlite3
```

If this prints no path, stop. The remaining commands must not be attempted.

```sh
sqlite3 -readonly /config/home-assistant_v2.db <<'SQL'
PRAGMA query_only=ON;
SELECT name FROM sqlite_master
WHERE type='index' AND tbl_name='states'
  AND name='ix_states_last_updated_ts';
EXPLAIN QUERY PLAN
SELECT m.entity_id
FROM states AS s INDEXED BY ix_states_last_updated_ts
JOIN states_meta AS m ON m.metadata_id=s.metadata_id
WHERE s.last_updated_ts >= unixepoch('now')-21600
  AND s.last_updated_ts < unixepoch('now')
LIMIT 50000;
SQL
```

The plan must include `SEARCH s USING INDEX ix_states_last_updated_ts` and `SEARCH m USING INTEGER PRIMARY KEY`. Stop if it says `SCAN s` or `USE TEMP B-TREE`.

## 2. Minimal bounded performance probe

Run only when Home Assistant is not already under heavy load and only if both `timeout` and `sqlite3` are already present. This reads **at most 50,000 rows** from the last six elapsed hours. It is a conservative smoke test, not a complete interval count or production certification.

```sh
if command -v timeout >/dev/null 2>&1 && command -v sqlite3 >/dev/null 2>&1; then
  time timeout 6s sqlite3 -readonly /config/home-assistant_v2.db <<'SQL'
PRAGMA query_only=ON;
PRAGMA busy_timeout=500;
SELECT COUNT(*) AS sampled_rows
FROM (
  SELECT m.entity_id
  FROM states AS s INDEXED BY ix_states_last_updated_ts
  JOIN states_meta AS m ON m.metadata_id=s.metadata_id
  WHERE s.last_updated_ts >= unixepoch('now')-21600
    AND s.last_updated_ts < unixepoch('now')
  LIMIT 50000
);
SQL
else
  echo 'timeout or sqlite3 unavailable: stop; do not install or run an unbounded query'
fi
```

A returned count is a count of **sampled state rows**, not database bytes and not the complete six-hour total when it reaches 50,000. Exit status 124 means the six-second wall-clock limit interrupted the command. Stop testing if it is slow or Home Assistant is under load. Share the sampled row count, elapsed time and exit status. Never run `VACUUM`, `REINDEX`, purge or file deletion as part of this preflight.

## Collector safeguards

The collector streams the **previous local six-hour interval** (5 or 7 elapsed hours on DST transition days) from the existing Recorder index, without SQL grouping. It fails closed on any of: more than 4 seconds, more than 500,000 matched rows, more than 5,000 distinct entity IDs, more than 256 KiB of JSON counts, a missing index, or a SQLite error. Failed or missed intervals remain unavailable, are never reported as zero, and are not automatically retried. Eight days of writer trends come solely from the monitor's own SQLite store.

InfluxDB size growth uses **exact elapsed 24-hour UTC boundaries**, not a 23/25-hour local day. Across a DST offset change, six-hour local Supervisor measurements cannot always supply those boundaries. Growth evaluation remains `learning` rather than interpolating or triggering an unsupported alert; a clean eight-day baseline can form after the transition. The independent disk-low alert is unaffected.
