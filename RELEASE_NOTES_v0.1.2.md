# Home Assistant Storage Monitor v0.1.2-beta.2

## HA OS runtime hotfix

- Fixed all direct keyword-argument calls to HA Core's positional
  `async_add_executor_job()` API by wrapping keyword-only database operations
  in `functools.partial()`.
- Covered `check_disk()`, `check_growth()`, `check_stale()` and dashboard
  `summary()` with a strict executor stub and a source-level regression check.
- Added offline startup-cycle tests with Recorder writer diagnostics disabled,
  normal and low disk states, unavailable Supervisor InfluxDB data, six-hour
  growth alerts, two-hour stale detection, persistent alert state and the
  admin WebSocket summary.
- Alert/cooldown state is now durable before persistent-notification delivery.
  A notification service failure is logged without retrying once per hourly
  sample inside the same six-hour cooldown window.
- The schema and path of the monitor-owned SQLite are unchanged. Existing
  snapshots, writer summaries, acknowledgements, cooldowns and configuration
  remain in place. Recorder writer diagnostics remain disabled by default.

### HACS update

Download `0.1.2-beta.2` through HACS, then perform one separately approved,
controlled Home Assistant Core restart to import the upgraded Python. A config
entry reload is sufficient for option changes but not for activating downloaded
custom-component code. Keep Recorder writer diagnostics off for the first
controlled smoke test. Do not delete
`.storage/ha_storage_monitor_history.sqlite3` during update or rollback.

## Safer Recorder top-writers design

- Replaced the full eight-day Recorder state aggregation with a bounded read-only query for only the previous 6-hour local checkpoint interval.
- Added `enable_recorder_writer_diagnostics`, default `false`. Disabled mode never opens Recorder for entity-writer diagnostics and remains explicit on the dashboard; hourly size, disk, InfluxDB, history and alarm functions continue.
- Enabling is non-retroactive: an interval that began before opt-in is stored as unavailable without running the Recorder query. Disable/re-enable starts a new consent period, while an ordinary enabled reload preserves it.
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

The beta.2 hotfix has offline tests only. A controlled beta.1 HA Core 2026.10.4 test confirmed the executor-call failure and left production data untouched. Beta.2 has not been installed or restarted in Home Assistant, and no production database changes were made. The verified Terminal & SSH Supervisor InfluxDB add-on total was 7,287,276,210 bytes on 2026-10-25, but access from HA Core remains to be verified.

The live Recorder performance probe remains unexecuted. A current `core-ssh` check on 2026-10-26 found no `sqlite3` executable (`command -v` returned nothing and `/usr/bin/sqlite3` was absent), so the attempted commands exited before opening Recorder. Installing diagnostic tooling requires separate owner approval.
