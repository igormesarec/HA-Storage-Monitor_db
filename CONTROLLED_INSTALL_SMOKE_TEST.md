# Controlled Home Assistant OS 2026.10.4 smoke test

This checklist is documentation only. Installation, file copying and the required Home Assistant restart must receive explicit owner approval first. It never authorizes database cleanup, YAML changes, Recorder/InfluxDB writes, or SI Grid Monitor changes.

## Initial installation: writer diagnostics off

1. Confirm an ordinary Home Assistant backup exists using the owner's established process.
2. Install or update **Home Assistant Storage Monitor `0.1.2-beta.3`** through HACS (or copy only `custom_components/ha_storage_monitor` into the corresponding `/config/custom_components` directory). Downloading files does not activate new Python or JavaScript code.
3. After separate owner approval, perform one controlled Home Assistant Core restart. A config-entry reload alone does not import upgraded custom-component Python.
4. For a new installation, add **Home Assistant Storage Monitor** from Settings > Devices & Services. For an update, confirm the existing config entry and `.storage/ha_storage_monitor_history.sqlite3` remain in place; do not delete or recreate them.
5. Leave **Enable bounded Recorder writer diagnostics** off. It defaults to off.
6. Add or retain the dashboard resource/card only after the integration loads without errors. Hard-refresh the dashboard once after the approved restart so the browser does not retain the beta.2 JavaScript resource.
7. Verify the Top Writers view says **Recorder writer diagnostics disabled**. It must not show zero rows or a fabricated ranking.
8. Verify Home Assistant logs do not contain `async_add_executor_job() got an unexpected keyword argument` or `Storage Monitor sample failed` during the startup sample. Verify the browser console does not contain `The result must not have attributes` and Lovelace does not report `setConfig is not a function`.
9. Verify the following continue independently:
   - Recorder main/WAL/SHM apparent sizes and their total;
   - `/config` disk total/free/used scope;
   - InfluxDB add-on total only when HA Core receives an authorized exact Supervisor subtree, otherwise `Unavailable`;
   - generic nonrecursive local SQLite discovery;
   - hourly history and growth state (`learning` until genuine history exists);
   - independent disk-low and stale-data status.
10. Review the dashboard summary after the startup sample. Confirm existing history, alert acknowledgements and cooldown state were preserved. Do not add an index, install diagnostic tooling, change Recorder configuration, or restart again merely to force a result.

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
- An options change performs a config-entry reload. It does not require a restart and must not be used as evidence that newly downloaded integration Python was loaded.
- To roll back integration code, use HACS to redownload the previously approved version, preserve the existing monitor SQLite, and activate that code only with a separately approved controlled Core restart.
- Remove the Storage Monitor config entry and dashboard resource for functional rollback.
- Removing integration files, deleting the monitor's own SQLite file, or restarting Home Assistant requires separate explicit approval.
- Rollback never changes the existing Recorder SQL sensor or production Recorder, WAL/SHM, InfluxDB, SI Grid Monitor archives, retention or indexes.
