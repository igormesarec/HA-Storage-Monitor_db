# Home Assistant Storage Monitor v0.1.2-beta.1

## Safer Recorder top-writers design

- Replaced the full eight-day Recorder state aggregation with a bounded read-only query for only the previous 6-hour local checkpoint interval.
- Four local checkpoints per day at 00, 06, 12, 18 Europe/Ljubljana. Each interval is saved exactly once into the monitor's own SQLite store.
- Read-only time index required; 4-second progress interruption, in-memory temporary grouping, maximum 5000 entity groups and 256 KiB serialized counts. Failed intervals are unavailable, never partial.
- Dashboard builds a local-day ranking from four saved intervals, and a previous-week baseline from the preceding 28 intervals. It does not query Recorder on page loads.
- Preserves 365-day hourly physical size samples. Growth checks fetch only 33 saved six-hour checkpoints from the monitor's own database, with small additional coverage/shrink checks over its hourly history. Strict >5% threshold, purge detection, missing-data handling, persistent cooldown and acknowledgments are preserved.
- The new `writer_windows` table is a non-destructive schema addition to the monitor's own database. Retain 9 days of detailed writer windows.
- DST 5/7-hour windows are labeled as local-day counts, not exact 24h counts.
- Added tests for DST, missing windows, read-only SQLite, index refusal, entity caps, durable idempotence and 8-day aggregation.

## Deployment status

Offline tests only. Not installed in Home Assistant. No HA restart or production database changes. The verified Terminal & SSH Supervisor InfluxDB add-on total was 7,287,276,210 bytes on 2026-10-25, but access from HA Core remains to be verified.
