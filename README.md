# Home Assistant Storage Monitor

**Independent prototype:** `0.1.0-beta.2` (25 October 2026)

## Existing installation, DO NOT repeat completed work

- Home Assistant OS with Recorder SQLite and InfluxDB v1 add-on.
- Existing `HA Recorder Database Size` SQL sensor was already installed and displayed **5,247.2 MiB** on 2026-10-25. Expected entity ID: `sensor.ha_recorder_database_size`, **verify** actual ID in Developer Tools. Do not add another copy.
- That SQL reading is SQLite `page_count × page_size`. It is not WAL, SHM, used disk blocks, or InfluxDB.
- Existing Recorder repack is complete; the SI Grid Monitor Influx exclusion rules must stay unchanged. Existing Influx historic measurements since 2024 should remain intact.
- Independent of `igormesarec/si-grid-monitor`, particularly PR #19. This monitors any supported accessible data source, without making assumptions about the integration that created it.
- New code has **not been installed or tested against the live HA Core**. Do not reboot, restart HA, install, change settings, or delete any production data without owner approval.

## Included

- `custom_components/ha_storage_monitor`: independently installable, read-only-source HA custom integration with UI config flow, admin-only websocket API, native Lovelace card, and own hourly SQLite snapshot history.
- `engine.py`: 1-hour UTC-identity snapshots, 1y/2y/forever retention, persistent alerts/acknowledgments, strict growth threshold.
- `sources.py`: Recorder main + WAL + SHM apparent/allocated file bytes, bounded generic SQLite file discovery (`*.db`, `*.sqlite`, `*.sqlite3`) in `/config` and `/config/.storage` at directory depth one, additional local SQLite files aggregated separately; excludes Recorder and the monitor snapshot database, HA `/config` filesystem capacity.
- `supervisor.py`: 6-hour interval Supervisor `GET /host/disks/default/usage?max_depth=2` for specific `a0d7b954_influxdb` subtree. Returns `unavailable`, **not zero**, without exact authorized size. Accepts `apps_data` and `addons_data` Supervisor IDs. This may represent other components in the same InfluxDB add-on, not solely TSM shard files.
- `writers.py`: read-only Recorder SQL, four-second per-query timeout, existing time index guard, top 20 entities by 24h state-row counts with last-hour counts, previous seven-day average, registry platform attribution where available; grouped by integration **for top20 only**, not all recorder writes.
- `frontend/storage-monitor-card.js`: responsive light/dark HA dashboard with Overview, Growth & Alarms, Top Writers, Settings / Access; real sampled history 1/7/30/90 days; language switch EN/SL; manual refresh; no cloud dependencies.
- `tests/`: offline, deterministic Python tests. These are **not** a replacement for HA OS integration tests.

### What the strict 5% alarm means

Let `S(t)` be source size in bytes at an actual observed UTC hour.

- `D24 = S(now) - S(now-24h)`
- `B7 = [S(now-24h)-S(now-8*24h)]/7` (mean of seven previous daily increases).
- Alert **only when** `D24 > B7*1.05`, `B7 > 0`, all eight days have reliable measurements and no one-day file shrinkage was observed.
- 200 MiB/day baseline: exactly 210 MiB/day is **not** an alarm; 211 MiB/day **is**.
- Default optional absolute excess threshold = zero (no weakening of strict >5%). Optional 6h repeat notifications OFF by default.
- A source lacking eight days of good data remains **learning**. A purge, repack, missing day, zero/negative baseline or stale data blocks anomalous-growth warnings rather than producing false positives.
- InfluxDB is *measured* only every 6 hours (to limit Supervisor directory walks); intermediate hourly rows have `cached` status and are **never treated as fresh measurements** for the comparator. Around DST transitions, if local 6-hour sampling no longer aligns with UTC day boundaries, the comparator conservatively returns `learning` until valid aligned windows exist. Other sources are measured hourly.
- Schedules: at minute zero plus ten seconds every hour; alarm evaluations at 00:00, 06:00, 12:00, 18:00 **Europe/Ljubljana**, based on local hour. Snapshots use UTC hours, preserving both 02:00 observations during autumn DST fallback.
- Disk-low alarm is independent of warm-up and checked hourly; threshold is the larger of the configured limit (default **15 GiB**) and size of Recorder files (safe repack minimum). Missed-readings warnings use two failed consecutive hour snapshots. Notifications use persistent notifications, idempotent durable 6h cooldown, and UI 24h acknowledgment.

## Installation instructions, AFTER permission to make HA changes

These are instructions only, **not actions already performed**.

1. In Home Assistant, verify that the existing SQL size sensor is still present. **Do not create another SQL sensor.** Check its exact entity ID.
2. Determine its automatic polling setting at **Settings > Devices & services > SQL > three-dot menu > System options**. Home Assistant SQL normally polls every 30 seconds. If approved, disable `Enable polling for updates` for the existing SQL config entry, then create the one-hour `homeassistant.update_entity` automation in `existing-sql-hourly-automation.yaml` using the verified entity ID. Do not enable that automation if its entity ID is not confirmed.
3. Make an ordinary backup using your established process. No backup or configuration is modified by these files.
4. On explicit approval, copy the directory `custom_components/ha_storage_monitor` to `/config/custom_components/ha_storage_monitor` using File editor / Samba or SSH. Do not merge any SI Grid Monitor branch. Installing custom integrations requires an **approved** HA restart to load code. Before approval, inspect the code but leave HA running.
5. After approved restart: Settings > Devices & services > Add Integration > Home Assistant Storage Monitor. The flow creates only one instance; choose disk-low threshold and monitoring retention.
6. In Settings > Dashboards, create a **NEW** dashboard named `Storage Monitor` with its own sidebar entry, and add its JavaScript module resource URL: `/ha_storage_monitor/storage-monitor-card.js`. Paste the one-view YAML from `lovelace-dashboard-example.yaml`, or add `custom:ha-storage-monitor-card` manually. Dashboard is admin-only by design because it contains filesystem and entity diagnostics.
7. Confirm the measured `Recorder SQLite` main-file value versus original SQL reading, check `Recorder WAL` and `Recorder SHM` separately, check `/config` disk scope, ensure Influx size is either exact `InfluxDB add-on total` or visibly unavailable, and discover additional readable local SQLite database filenames.
8. Read the `Growth & alarms` page: it will show `learning` for the first eight days. **Do not force-fill eight days of fake history.** Independent disk-low warnings can trigger immediately.
9. Confirm `Top writers` query remains under four seconds; if it times out it safely returns `unavailable`. Check HA logs and do not add indexes to production Recorder.
10. Roll back by removing the Storage Monitor config entry and dashboard resource through HA UI. Any file deletion or HA restart should be separately approved. The original SQL sensor and production databases remain unchanged.

## Scope of generic local SQL file discovery

- The generic **Other local SQLite databases** section is not hardcoded to any specific Home Assistant integration. It checks only `/config` and `/config/.storage` non-recursively for regular files with `.db`, `.sqlite`, or `.sqlite3` extension. Each SQLite file's main, WAL and SHM footprints are combined without reading its contents.
- This is **not** an exhaustive scan of HA OS, protected Supervisor add-on directories, MariaDB/PostgreSQL services, remote SQL databases, or other volumes. Unknown paths and unavailable protected stores remain unavailable, never zero. Future configured additional directories and supported read-only service-specific metrics require separate access review.
- Existing Recorder SQL sensor is preserved, and our own monitoring history DB is excluded to prevent recursive monitoring growth.
- A discovered database file is monitored using filesystem metadata, not opened for SQL queries, and no production database is ever edited.

## Read-only capability probe, BEFORE Influx can be marked verified

Use `capability-probe.sh` once in Terminal & SSH (only with user approval). This sends a single read-only GET, does not print the token, and does not touch Influx data. The Terminal & SSH app's Supervisor API role may differ from Home Assistant Core, so a Terminal permission failure is **not** definitive proof the integration lacks permission.

On supported Supervisor versions the response has top-level `apps_data` (newer) or `addons_data` (legacy). The subtree whose `id` ends exactly `a0d7b954_influxdb` must have a real numeric `used_bytes`. If absent, the dashboard must show `Unavailable`, not an inference from series count, InfluxQL, or a backup's size.

API documentation:
- https://contributors.home-assistant.io/apps/supervisor-api/host/
- https://www.home-assistant.io/integrations/sql
- https://contributors.home-assistant.io/data/guides/query-the-database/

## Limits and known gaps, deliberately not claimed as completed

- No live HA OS/Supervisor permission proof or live InfluxDB footprint is available here. The 6.61 GB historical Influx figure is **not** a new measurement.
- Main Recorder logical SQL sensor still reports 5,247.2 MiB at the known check; actual filesystem main file, WAL, SHM, additional local database files and free disk sizes have not been measured by this prototype on your machine.
- No production HA files, databases, YAML, retention, indexes, PR #19, exclusions, or backups were modified.
- No automation that purges, shrinks or repairs DBs. No high-rate sensors; all measurements in own local SQLite, not in Influx. No installed custom sidebar registration; the separate sidebar dashboard is created through supported Home Assistant UI.
- Dedicated Supervisor host disk (`/host/info`) vs `/config` filesystem used/free breakdown not yet cross-validated. Labels show scope.
- No Recorder `PRAGMA freelist_count` periodical breakdown yet. No separately measured actual backup archive sizes, per-table exact physical byte usage, or per-entity attributable disk bytes (impossible to assert from state counts alone).
- No live measured query benchmark against the 5.11 GiB Recorder database yet. Query self-cancels at four seconds; a timed-out query returns unavailable.
- Admin-only WebSocket command and panel integration still require a live Home Assistant API compatibility check; not certified ready for unattended production deployment.
- Notification transport is HA persistent_notification, optional phone/mobile destinations and incident timeline UI are future work.

## Local development tests

```sh
python -m pytest tests -q
python -m compileall -q custom_components/ha_storage_monitor
node --check custom_components/ha_storage_monitor/frontend/storage-monitor-card.js
```

Safety principle: monitoring must never become the new high-writing integration.
