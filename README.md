# Home Assistant Storage Monitor

**Independent prototype:** `0.2.2-beta.1` (28 October 2026)

## Existing installation, DO NOT repeat completed work

- Home Assistant OS with Recorder SQLite and InfluxDB v1 add-on.
- Existing `HA Recorder Database Size` SQL sensor was already installed and displayed **5,247.2 MiB** on 2026-10-25. Expected entity ID: `sensor.ha_recorder_database_size`, **verify** actual ID in Developer Tools. Do not add another copy.
- That SQL reading is SQLite `page_count × page_size`. It is not WAL, SHM, used disk blocks, or InfluxDB.
- Existing Recorder repack is complete; the SI Grid Monitor Influx exclusion rules must stay unchanged. Existing Influx historic measurements since 2024 should remain intact.
- Independent of `igormesarec/si-grid-monitor`, particularly PR #19. This monitors any supported accessible data source, without making assumptions about the integration that created it.
- Controlled HA OS 2026.10.4 tests exposed the beta.1 executor-call failure and the beta.2 frontend custom-element construction failure. Beta.3 fixes both known runtime blockers; production data was untouched. The beta.3 frontend hotfix has offline DOM coverage but still requires a separately approved controlled Core restart and smoke test. Do not restart HA, change settings, or delete production data without owner approval.

## Included

- `custom_components/ha_storage_monitor`: independently installable, read-only-source HA custom integration with UI config flow, admin-only websocket API, native Lovelace card, and own hourly SQLite snapshot history.
- `engine.py`: 1-hour UTC-identity snapshots, 1y/2y/forever retention, persistent alerts/acknowledgments, strict growth threshold, and durable InfluxDB probe attempt/success metadata.
- `sources.py`: Recorder main + WAL + SHM apparent/allocated file bytes, bounded generic SQLite file discovery (`*.db`, `*.sqlite`, `*.sqlite3`) in `/config` and `/config/.storage` at directory depth one, additional local SQLite files aggregated separately; excludes Recorder and the monitor snapshot database, HA `/config` filesystem capacity.
- `supervisor.py`: 6-hour interval Supervisor `GET /host/disks/default/usage?max_depth=2` for specific `a0d7b954_influxdb` subtree. Returns `unavailable`, **not zero**, without exact authorized size. Accepts `apps_data` and `addons_data` Supervisor IDs. This may represent other components in the same InfluxDB add-on, not solely TSM shard files.
- `writers.py`: optional bounded Recorder diagnostics, **disabled by default**. While disabled, Recorder is never opened for entity-writer queries and the dashboard reports `Recorder writer diagnostics disabled`, not zero rows. When explicitly enabled, one read-only query runs per local six-hour checkpoint with the existing timestamp index guard, 4-second progress budget, 500,000-row/5,000-entity/256-KiB payload caps, and compact counts stored in our own SQLite. The query streams indexed rows without SQL `GROUP BY` or a temporary grouping B-tree. No missed or pre-opt-in interval is queried retroactively. Dashboard ranking can select Top 20/50/100/500 from those completed monitor-owned summaries without another Recorder query.
- `frontend/storage-monitor-card.js`: responsive seven-tab light/dark HA dashboard: Overview, Databases, Charts, Top Writers, Disk Usage, Alerts and Settings; real sampled history for 1/7/30/90 days; English/Slovenian switching; optional read-only display of the existing Recorder SQL sensor; no cloud dependencies. Top 100/500 writer rankings use fixed 50-entity pages; integration groupings are explicitly partial totals for the selected Top N.
- Writer ranking pagination bounds entity rows transferred and rendered, not the server-side aggregation of retained summaries. Each selection reads at most 40 completed rows from the monitor's own `writer_windows` table; it never reads Recorder. An offline 32-window/600-entity synthetic run averaged 2.497 ms for Top 500 and produced an 8,741-byte 50-entity page before integration grouping. This is not an HA OS performance guarantee.
- `tests/`: offline, deterministic Python tests. These are **not** a replacement for HA OS integration tests.

### What the strict 5% alarm means

Let `S(t)` be source size in bytes at an actual observed UTC hour.

- `D24 = S(now) - S(now-24h)`
- `B7 = [S(now-24h)-S(now-8*24h)]/7` (mean of seven previous daily increases).
- Alert **only when** `D24 > B7*1.05`, `B7 > 0`, all eight days have reliable measurements and no one-day file shrinkage was observed.
- 200 MiB/day baseline: exactly 210 MiB/day is **not** an alarm; 211 MiB/day **is**.
- Default optional absolute excess threshold = zero (no weakening of strict >5%). Optional 6h repeat notifications OFF by default.
- A source lacking eight days of good data remains **learning**. A purge, repack, missing day, zero/negative baseline or stale data blocks anomalous-growth warnings rather than producing false positives.
- InfluxDB targets startup and the local 00/06/12/18 checkpoints, but an expensive Supervisor scan is permitted only after **six elapsed hours since the last attempted scan**. The attempt is committed to Storage Monitor's own SQLite before the request, and the last successful size and its original timestamp are also durable. A quick restart therefore cannot repeat a successful or failed scan. Intermediate rows are `cached`; a cache at least seven hours old is `stale`; no prior success is `unavailable`. Cached/stale values are **never treated as fresh measurements** by the eight-day comparator.
- There is an unavoidable scheduling trade-off: a startup scan at 18:34 is only 5h26 old at midnight, so the midnight target is recorded from cache (or unavailable) rather than scanned. The next safe local checkpoint is 06:00. Similarly, the spring DST 00→06 interval is five elapsed hours and is deferred; the autumn interval is seven hours and is eligible. The integration never invents or backfills the skipped checkpoint merely to preserve exact wall-clock alignment.
- On the first upgrade from beta.3, v0.2 seeds its new probe metadata solely from existing `influx_addon` rows in Storage Monitor's own SQLite. A genuine `ok` row preserves its exact `measured_at`, size and method. If a newer legacy `unavailable` row could represent either a failed attempt or an hourly copy, the migration conservatively treats the end of that UTC snapshot hour as the possible attempt time. This one-time ambiguity can postpone the first v0.2 probe, but prevents an unsafe duplicate scan; it never queries a production database or fabricates a measurement. A fresh archive with no Influx history remains immediately eligible for its first probe.
- Schedules: at minute zero plus ten seconds every hour; alarm evaluations at 00:00, 06:00, 12:00, 18:00 **Europe/Ljubljana**, based on local hour. Snapshots use UTC hours, preserving both 02:00 observations during autumn DST fallback.
- Disk-low alarm is independent of warm-up and checked hourly; threshold is the larger of the configured limit (default **15 GiB**) and size of Recorder files (safe repack minimum). Recorder, disk and other genuinely hourly sources retain the two-consecutive-hour missing-sample rule. InfluxDB uses the cadence-aware policy below. Notifications use persistent notifications, idempotent durable 6h cooldown, and UI 24h acknowledgment.

### InfluxDB measurement-health policy

- An intentionally deferred request is not a failed probe. Cached values keep the original successful timestamp; after seven hours they display as stale while explicitly waiting for the next eligible local checkpoint. Cache aging alone never activates the Influx measurement alert.
- Every eligible attempt is reserved before the Supervisor request and its outcome is saved in the monitor SQLite. A failed authorization, timeout, unavailable response or unexpected exception increments a durable consecutive-failure count. Restarting inside the six-hour minimum neither scans nor increments the count.
- If Home Assistant stops after reserving a probe but before saving its result, the attempt remains `in_progress` for a two-minute grace period (longer than the 65-second HTTP timeout) and is then durably classified as `interrupted`. It counts as one failed eligible attempt while remaining distinct from an HTTP/API failure. The original reservation still enforces the six-elapsed-hour minimum, and the previous genuine size and timestamp remain unchanged.
- The alert activates after **two consecutive failed eligible probe attempts**, or when a scheduled eligible checkpoint remains unrecorded for one full hour. Once active, it stays active until a genuine successful probe resets the counter. A success also dismisses only Storage Monitor's own `ha_storage_monitor_stale_influx_addon` persistent notification.
- The notification reports the exact last successful and attempted timestamps, attempt outcome, measurement age, reason and next eligible checkpoint. It says only that Storage Monitor cannot verify add-on disk usage; it does not claim that the InfluxDB service stopped.

## Installation instructions, AFTER permission to make HA changes

These are instructions only, **not actions already performed**.

1. In Home Assistant, verify that the existing SQL size sensor is still present. **Do not create another SQL sensor.** Check its exact entity ID.
2. Determine its automatic polling setting at **Settings > Devices & services > SQL > three-dot menu > System options**. Home Assistant SQL normally polls every 30 seconds. If approved, disable `Enable polling for updates` for the existing SQL config entry, then create the one-hour `homeassistant.update_entity` automation in `existing-sql-hourly-automation.yaml` using the verified entity ID. Do not enable that automation if its entity ID is not confirmed.
3. Make an ordinary backup using your established process. No backup or configuration is modified by these files.
4. On explicit approval, copy the directory `custom_components/ha_storage_monitor` to `/config/custom_components/ha_storage_monitor` using File editor / Samba or SSH. Do not merge any SI Grid Monitor branch. Installing custom integrations requires an **approved** HA restart to load code. Before approval, inspect the code but leave HA running.
5. After approved restart: Settings > Devices & services > Add Integration > Home Assistant Storage Monitor. The flow creates only one instance; choose disk-low threshold and monitoring retention. Leave **Enable bounded Recorder writer diagnostics** off for the first controlled installation. Size measurements, InfluxDB probing, disk monitoring, independent history, dashboard and alarms continue with this option off.
6. In Settings > Dashboards, create a **NEW** dashboard named `Storage Monitor` with its own sidebar entry, and add its JavaScript module resource URL: `/ha_storage_monitor/storage-monitor-card.js`. Paste the one-view YAML from `lovelace-dashboard-example.yaml`, or add `custom:ha-storage-monitor-card` manually. Dashboard is admin-only by design because it contains filesystem and entity diagnostics.
7. Confirm the measured `Recorder SQLite` main-file value versus the existing SQL reading, check `Recorder WAL` and `Recorder SHM` separately, check `/config` disk scope, ensure Influx size is either exact `InfluxDB add-on total` or visibly cached/stale/unavailable, and discover additional readable local SQLite database filenames. The card reads only the current state of `sensor.ha_recorder_database_size` by default; set card option `sql_entity` to the verified existing entity ID, or `show_sql_cross_check: false` to hide it. It never creates or polls a sensor.
8. Read the `Growth & alarms` page: it will show `learning` for the first eight days. **Do not force-fill eight days of fake history.** Independent disk-low warnings can trigger immediately.
9. Confirm the Top Writers view says **Recorder writer diagnostics disabled** and does not show zero rows. Follow [the controlled installation smoke test](CONTROLLED_INSTALL_SMOKE_TEST.md). Enabling writer diagnostics is a later, separate opt-in decision. The first partial interval after enabling is recorded unavailable without querying Recorder; collection begins only after a full post-opt-in local checkpoint interval.
10. To stop writer queries after they have been enabled, turn the option off under Settings > Devices & Services > Storage Monitor > Configure. The config entry reloads and future writer queries stop; size/disk/Influx/history monitoring continues. Full rollback removes the Storage Monitor config entry and dashboard resource through HA UI. Any file deletion or HA restart requires separate approval. The original SQL sensor and production databases remain unchanged.

## Scope of generic local SQL file discovery

- The generic **Other local SQLite databases** section is not hardcoded to any specific Home Assistant integration. It checks only `/config` and `/config/.storage` non-recursively for regular files with `.db`, `.sqlite`, or `.sqlite3` extension. Each SQLite file's main, WAL and SHM footprints are combined without reading its contents.
- This is **not** an exhaustive scan of HA OS, protected Supervisor add-on directories, MariaDB/PostgreSQL services, remote SQL databases, or other volumes. Unknown paths and unavailable protected stores remain unavailable, never zero. Future configured additional directories and supported read-only service-specific metrics require separate access review.
- Existing Recorder SQL sensor is preserved, and our own monitoring history DB is excluded to prevent recursive monitoring growth.
- A discovered database file is monitored using filesystem metadata, not opened for SQL queries, and no production database is ever edited.

## Read-only capability probe, Terminal access verified, Core access pending

A read-only Supervisor GET from Terminal & SSH succeeded on 2026-10-25. `capability-probe.sh` documents the same supported method and must not be run repeatedly without need. Terminal & SSH permissions may differ from Home Assistant Core, so Core access must still be validated after explicit installation approval.

On supported Supervisor versions the response has top-level `apps_data` (newer) or `addons_data` (legacy). The subtree whose `id` ends exactly `a0d7b954_influxdb` must have a real numeric `used_bytes`. If absent, the dashboard must show `Unavailable`, not an inference from series count, InfluxQL, or a backup's size.

API documentation:
- https://contributors.home-assistant.io/apps/supervisor-api/host/
- https://www.home-assistant.io/integrations/sql
- https://contributors.home-assistant.io/data/guides/query-the-database/

## Limits and known gaps, deliberately not claimed as completed

- On 2026-10-25, Terminal & SSH obtained a successful Supervisor API response for the exact InfluxDB add-on subtree: **7,287,276,210 bytes (6.79 GiB)**. This is add-on data usage, not TSM-only bytes. **Access from HA Core remains unverified.**
- Existing SQL sensor showed 5,247.2 MiB at the earlier check. Terminal & SSH separately showed a Recorder main file about 5.1 GiB, WAL 7.4 MiB and SHM 32 KiB on 2026-10-25. These are not measurements produced by the integration; Core integration measurement remains unverified.
- No production HA files, databases, YAML, retention, indexes, PR #19, exclusions, or backups were modified.
- No automation that purges, shrinks or repairs DBs. No high-rate sensors; all measurements in own local SQLite, not in Influx. No installed custom sidebar registration; the separate sidebar dashboard is created through supported Home Assistant UI.
- Dedicated Supervisor host disk (`/host/info`) vs `/config` filesystem used/free breakdown not yet cross-validated. Labels show scope.
- No Recorder `PRAGMA freelist_count` periodical breakdown yet. No separately measured actual backup archive sizes, per-table exact physical byte usage, or per-entity attributable disk bytes (impossible to assert from state counts alone).
- No live measured query benchmark against the 5.11 GiB Recorder database yet. On 2026-10-26 the current `core-ssh` shell returned no path for `command -v sqlite3`, and `/usr/bin/sqlite3` was absent, contradicting the earlier tooling observation. No query ran. Do not install tooling or use a privileged workaround without explicit approval. Once executable tooling is safely available, the integration query self-cancels at four seconds and a timed-out query returns unavailable.
- Storage overhead has not been measured on HA OS. An offline synthetic test modeling 12 hourly sources for 365 days plus 36 retained six-hour writer summaries with 100 entities each produced a 7,131,136-byte SQLite file (about 6.80 MiB) on the test platform. This supports, but does not guarantee, the `<10 MiB/year` normal-load target; more discovered databases, longer labels, filesystem allocation and near-cap writer payloads increase usage.
- Admin-only WebSocket command and panel integration still require a live Home Assistant API compatibility check; not certified ready for unattended production deployment.
- Notification transport is HA persistent_notification, optional phone/mobile destinations and incident timeline UI are future work.

## v0.1.1-beta.1 safety and repository update

- The monitoring SQLite store now **closes every connection** after each operation, preventing file descriptor accumulation during hourly sampling and frequent dashboard reads.
- Generic SQLite totals now show verified **0 bytes** only when both authorized directories were scanned successfully and no additional databases exist. More than 64 candidates, an inaccessible directory, or a symlinked directory produces an **unavailable/incomplete total**, not a misleading partial sum.
- A later cached/unavailable observation in the **same UTC hour** cannot overwrite an already verified `ok` observation. This protects eight-day baseline quality after a restart or duplicate callback.
- Recorder writer diagnostic SQLite connections are explicitly closed. They remain `mode=ro`, query-only, time-index guarded, and capped by a four-second progress handler.
- GitHub Actions workflow belongs at `.github/workflows/tests.yml` and is deliberately **not** at repository root. Workflow execution on GitHub must be verified separately.
- GitHub/HACS icon assets in the repository root are source assets, **not proof that the HACS catalog or Home Assistant brand registry has accepted branding**. Publication to the relevant branding repository may be required.
- This remains a controlled prototype. HA Core testing found and confirmed the beta.1 executor-call incompatibility; browser testing of beta.2 then found the reflected `HTMLElement.lang` constructor violation. Beta.3 has not yet been live-tested. Terminal Supervisor access and Recorder index/query plans were verified separately and do not prove HA Core access.


## v0.1.2-beta.1: Four local writer checkpoints per day

The earlier version queried eight days of Recorder rows at each 6-hour alarm check.
That design is retired. v0.1.2 reads **only the interval since the previous
local 00/06/12/18 checkpoint** from Recorder. Each read uses the existing
`ix_states_last_updated_ts` index, SQLite read-only/query-only mode, a 4-second
progress-interrupt budget, row streaming without SQL grouping, and hard caps of
500,000 rows, 5,000 entity IDs and 256 KiB serialized counts. A timeout,
missing index, or cap exceedance
records **unavailable**, never partial totals. No production indexes are added.

Each completed 6-hour local interval is stored **once** in the independent
`/config/.storage/ha_storage_monitor_history.sqlite3` table `writer_windows`.
The dashboard reads only these saved summaries. Four local checkpoints produce
a local-day ranking; 32 intervals produce the preceding 8-day writer baseline.
Detailed entity counts are kept for 9 days, while hourly **file size** snapshots
retain the configured 365/730-day history. The old `writers` table is no longer
used and is not migrated as valid six-hour evidence. A missing interval is
never fabricated or backfilled by rescanning eight days of Recorder.

**Size growth calculation:** Hourly sizes are still saved for charts and
reliability checks. Each six-hour growth evaluation retrieves only **33 UTC
six-hour checkpoint values** from the monitor's own SQLite (four per day
plus the starting boundary). Two small SQL checks against that same monitor
SQLite verify hourly coverage and detect a measured shrink between checkpoints.
No Recorder history is queried for size-growth calculations.

**DST:** A local 00-to-06 window may span 5 or 7 elapsed hours. The UI
labels the number of elapsed hours and calls the result a local day, **not**
a strict 24-hour measurement on those transitions. The original storage-size
alert continues to use strict 24-hour UTC elapsed windows and remains separate.

**Writer learning:** A complete local-day ranking requires four verified
intervals. Prior-week comparison requires 32 consecutive verified intervals.
A query timeout, missed HA shutdown checkpoint, or permission failure keeps
comparisons in learning until a clean eight-day window exists. Entity row counts
are not attributable physical bytes. No additional SQL sensor is created.

Writer diagnostics are opt-in and default to disabled. Configuration changes
reload the entry. Disabling immediately prevents future writer queries while
all storage-size monitoring continues. Re-enabling starts a new consent period;
an interval that began before that time is not queried or backfilled.

**Operational caution:** The 4-second limit is a query progress budget, not
a guarantee of zero memory/CPU impact. Test a single six-hour window on HA OS
before approving production installation. InfluxDB Core permissions and
frontend WebSocket compatibility remain unverified.

## v0.1.2-beta.2: Home Assistant executor hotfix

This release fixes the HA Core 2026.10.4 startup failure caused by passing
keyword arguments directly to `HomeAssistant.async_add_executor_job()`. Calls
to keyword-only monitor-database methods are now wrapped with
`functools.partial()`. The audit covers disk, growth, stale-data and dashboard
summary paths. Alert state and the six-hour notification cooldown are committed
to the monitor's own SQLite before notification delivery is attempted, so a
notification service failure cannot generate a retry on every hourly sample.

For an existing HACS installation, download `0.1.2-beta.2` through HACS first.
Downloading files does not activate the Python change. After separate owner
approval, perform one controlled **Home Assistant Core restart** and verify the
version and smoke-test results. A config-entry reload alone is appropriate for
changing Storage Monitor options, including keeping Recorder writer diagnostics
off, but it is not a substitute for the restart needed to import upgraded
custom-component Python. Do not delete or replace
`.storage/ha_storage_monitor_history.sqlite3`; the hotfix keeps its snapshots,
writer history, alert acknowledgements and cooldown state.

## v0.1.2-beta.3: Lovelace custom-element initialization hotfix

The card now stores its language choice in the component-owned `_language`
property instead of assigning `HTMLElement.lang` during construction. The
previous assignment reflected a `lang` HTML attribute before the custom-element
constructor returned, which browsers reject with `NotSupportedError` and leaves
Lovelace without a working `setConfig()` method.

The frontend test suite now evaluates the shipped card in jsdom, registers the
custom element, constructs it through
`document.createElement('ha-storage-monitor-card')`, configures it, assigns a
fake Home Assistant connection, switches Slovenian/English, and exercises
disconnect/reconnect timer cleanup. CI retains `node --check`, but the DOM test
is mandatory because syntax validation alone cannot detect constructor-rule
violations.

## Local development tests

```sh
python -m pytest tests -q
python -m compileall -q custom_components/ha_storage_monitor
node --check custom_components/ha_storage_monitor/frontend/storage-monitor-card.js
pnpm install --frozen-lockfile
pnpm test:frontend
```

Safety principle: monitoring must never become the new high-writing integration.

### Safety preflight for the v0.1.2 writer collector

Before enabling writer diagnostics on HA OS, follow [the bounded read-only Recorder preflight](RECORDER_PREFLIGHT_READONLY.md). The collector streams indexed rows without a temporary `GROUP BY` tree and refuses incomplete intervals if its wall-time, row-count, entity-count, or JSON-size limit is reached. DST transitions can leave InfluxDB growth in learning mode until a complete eight-day exact-UTC baseline is available.
