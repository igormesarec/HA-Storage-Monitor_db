# Home Assistant Storage Monitor v0.1.2-beta.1

## Safer Recorder top-writers design

- Replaced the full eight-day Recorder state aggregation with a bounded read-only query for only the previous 6-hour local checkpoint interval.
- Four local checkpoints per day at 00, 06, 12, 18 Europe/Ljubljana. Each interval is saved exactly once into the monitor's own SQLite store.
- Read-only time index required; rows are streamed without SQL grouping or a temporary B-tree. A 4-second progress interruption plus hard limits of 500,000 rows, 5,000 entities and 256 KiB serialized counts fail closed. Failed intervals are unavailable, never partial.
- Dashboard builds a local-day ranking from four saved intervals, and a previous-week baseline from the preceding 28 intervals. It does not query Recorder on page loads.
- Preserves 365-day hourly physical size samples. Growth checks fetch only 33 saved six-hour checkpoints from the monitor's own database, with small additional coverage/shrink checks over its hourly history. Strict >5% threshold, purge detection, missing-data handling, persistent cooldown and acknowledgments are preserved.
- The new `writer_windows` table is a non-destructive schema addition to the monitor's own database. Retain 9 days of detailed writer windows.
- DST 5/7-hour windows are labeled as local-day counts, not exact 24h counts.
- Added tests for DST, missing windows, read-only SQLite, index refusal, entity caps, durable idempotence and 8-day aggregation.
- Declares the Home Assistant `http` and `websocket_api` runtime dependencies used during setup, matching the Core 2026.10.4 API pattern.
- A cold-start sample can no longer cause a simultaneous scheduled checkpoint to be silently dropped; the checkpoint is queued and remains idempotent.
- Added a synthetic storage-budget regression: 12 hourly sources for one year plus nine days of 100-entity writer summaries measured 7,131,136 bytes (about 6.80 MiB) offline. This is an estimate workload, not an HA OS measurement.

## Deployment status

Offline tests only. Not installed in Home Assistant. No HA restart or production database changes. The verified Terminal & SSH Supervisor InfluxDB add-on total was 7,287,276,210 bytes on 2026-10-25, but access from HA Core remains to be verified.
