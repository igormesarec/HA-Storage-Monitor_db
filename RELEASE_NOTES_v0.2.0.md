# Home Assistant Storage Monitor 0.2.0-beta.1

This release redesigns the existing Lovelace card without creating a new dashboard, sidebar entry, integration instance, SQL sensor, or history store.

## Dashboard

- Seven responsive internal tabs: Overview, Databases, Charts, Top Writers, Disk Usage, Alerts and Settings.
- Compact source cards distinguish current size, available 6h/24h changes, last successful sample, status, measurement method and scope. Unknown values remain unavailable, never zero.
- The chart uses measured timestamps, adaptive precision and explicit Min/Max/Range/Net MiB figures. It never interpolates missing intervals, excludes cached Influx values and marks measured decreases.
- Recorder main/WAL/SHM remain separate components of one Recorder total. Recorder, History, Logbook and statistics are not presented as separate databases.
- Disk accounting explains why Linux/Python Total, Used and Available may not add up. The difference is derived explanatory context, not an invented measurement.
- Writer diagnostics remain opt-in and disabled by default. Disabled mode explicitly says so and never displays a zero-row ranking.
- Top Writers restores entity integration, verified six-hour rows, last complete local-day rows, share, records/minute, prior-seven-day daily average, growth ratio and the high-writer indicator. Insufficient coverage says `Learning`; integration totals are explicitly partial and derived only from the Top 20.
- Databases can compare Recorder main-file bytes with the current state of the existing `sensor.ha_recorder_database_size` (or a configured existing entity ID). Missing/unavailable states remain unavailable. The card neither creates nor polls a SQL sensor, and the comparison can be hidden with `show_sql_cross_check: false`.
- English and Slovenian views, alert acknowledgement controls and the existing 1/7/30/90-day history selection are preserved.

## Backend compatibility

The summary WebSocket response adds only `effective_options`, a non-secret copy of options already resolved in memory: disk threshold, minimum excess, retention, repeat-notification setting and writer-diagnostics state. This supports the Settings and Disk Usage views. It performs no new Recorder, filesystem or Supervisor query. Existing response fields and history commands are unchanged.

InfluxDB Supervisor scans now persist both the last attempted scan and last successful result in Storage Monitor's own SQLite. Startup and local 00/06/12/18 checkpoints request a scan, but the strict six-elapsed-hour minimum always takes precedence. For example, a startup scan at 18:34 makes midnight ineligible (5h26 elapsed), so midnight is cached or unavailable and 06:00 is the next eligible target. Spring 00→06 is five elapsed hours and is deferred; autumn 00→06 is seven and is eligible. Cached values retain their original measurement timestamp, stale and unavailable remain distinct, and only genuine fresh results enter the eight-day comparator.

The first beta.3-to-v0.2 startup migrates this metadata from existing monitor-owned Influx snapshots before deciding whether to scan. Exact successful timestamps are retained. A newer ambiguous legacy `unavailable` row uses the end of its UTC snapshot hour as a conservative possible-attempt bound. This may defer one scan but avoids an extra scan inside an uncertain six-hour window. Fresh installations without historical rows are unaffected. No Recorder or InfluxDB query is used for migration.

## Controlled update and rollback

Download the HACS update without deleting `/config/.storage/ha_storage_monitor_history.sqlite3`. Activation of updated integration Python requires a separately approved Home Assistant Core restart; a browser hard refresh then loads the new card resource. Keep the existing Lovelace dashboard and `/ha_storage_monitor/storage-monitor-card.js` resource.

To roll back, use HACS to redownload the previously approved beta.3 files while preserving the monitor SQLite history, then activate them only with a separately approved controlled restart. Do not remove or alter Recorder, InfluxDB, the existing SQL sensor or SI Grid Monitor.

No live Home Assistant installation, restart or production database operation was performed while preparing this release.
