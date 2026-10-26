# Controlled Home Assistant OS 2026.10.4 smoke test

This checklist is documentation only. Installation, file copying and the required Home Assistant restart must receive explicit owner approval first. It never authorizes database cleanup, YAML changes, Recorder/InfluxDB writes, or SI Grid Monitor changes.

## Initial installation: writer diagnostics off

1. Confirm an ordinary Home Assistant backup exists using the owner's established process.
2. Copy only `custom_components/ha_storage_monitor` into the corresponding `/config/custom_components` directory after approval, then perform the separately approved Home Assistant restart.
3. Add **Home Assistant Storage Monitor** from Settings > Devices & Services.
4. Leave **Enable bounded Recorder writer diagnostics** off. It defaults to off.
5. Add the dashboard resource/card only after the integration loads without errors.
6. Verify the Top Writers view says **Recorder writer diagnostics disabled**. It must not show zero rows or a fabricated ranking.
7. Verify the following continue independently:
   - Recorder main/WAL/SHM apparent sizes and their total;
   - `/config` disk total/free/used scope;
   - InfluxDB add-on total only when HA Core receives an authorized exact Supervisor subtree, otherwise `Unavailable`;
   - generic nonrecursive local SQLite discovery;
   - hourly history and growth state (`learning` until genuine history exists);
   - independent disk-low and stale-data status.
8. Review Home Assistant logs for Storage Monitor exceptions. Do not add an index, install diagnostic tooling, change Recorder configuration, or restart again merely to force a result.

## Later optional writer-diagnostics opt-in

This is a separate decision after the disabled-mode smoke test succeeds.

1. Open Settings > Devices & Services > Storage Monitor > Configure and enable **Enable bounded Recorder writer diagnostics**.
2. Saving options reloads only the config entry; it does not authorize a Home Assistant restart.
3. If the current local six-hour interval began before opt-in, that interval is marked unavailable and Recorder is not queried. There is no catch-up or retroactive scan.
4. At the first full post-opt-in 00/06/12/18 Europe/Ljubljana checkpoint, the existing bounded collector may run: read-only URI, `query_only`, required timestamp index, four-second deadline, 500,000 rows, 5,000 entities and 256 KiB JSON maximum.
5. A timeout, missing index, cap, shutdown or permission failure must display unavailable, never a partial or zero result.

## Disable and rollback

- Turn writer diagnostics off in Configure to stop all future Recorder writer queries. Size, disk, InfluxDB, independent history, dashboard and alarms remain active.
- Previously saved writer summaries stay only in the monitor's own SQLite retention window but are not displayed as current while disabled.
- Remove the Storage Monitor config entry and dashboard resource for functional rollback.
- Removing integration files, deleting the monitor's own SQLite file, or restarting Home Assistant requires separate explicit approval.
- Rollback never changes the existing Recorder SQL sensor or production Recorder, WAL/SHM, InfluxDB, SI Grid Monitor archives, retention or indexes.
