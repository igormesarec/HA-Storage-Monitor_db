"""Independent Storage Monitor: local hourly metadata, never changes production DBs."""
from __future__ import annotations
from datetime import datetime, timedelta
from functools import partial
import logging
from pathlib import Path
import time
from zoneinfo import ZoneInfo
import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers import entity_registry as er
from .const import DOMAIN, DEFAULT_INFLUX_SLUG, ALARM_HOURS, GROWTH_SOURCES, resolved_options
from .engine import Sample, SnapshotDB, evaluate, utc_hour, allow_notification, allowed_acknowledgement_sources, disk_low_evidence, DAY, HOUR
from .sources import collect_local, HOME_ENERGY_INTELLIGENCE_SOURCE
from .supervisor import get_influx_usage
from .writers import (WRITER_PAGE_SIZE, WRITER_RANKING_LIMITS,
                      collect_writer_checkpoint, disabled_writer_summary,
                      local_checkpoint_window, summarize_writer_windows)

_LOGGER = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Ljubljana")


def next_influx_probe_opportunity(last_attempt_at: int | None, now: int) -> int | None:
    """Return the next local checkpoint that also satisfies six elapsed hours."""
    reference = int(last_attempt_at) if last_attempt_at is not None else int(now)
    local = datetime.fromtimestamp(reference,TZ)
    for day_offset in range(4):
        day = local.date() + timedelta(days=day_offset)
        for checkpoint_hour in ALARM_HOURS:
            candidate = datetime(day.year,day.month,day.day,checkpoint_hour,tzinfo=TZ)
            timestamp = int(candidate.timestamp())
            if last_attempt_at is None:
                if timestamp >= int(now):
                    return timestamp
            elif timestamp > int(last_attempt_at) and timestamp-int(last_attempt_at) >= 6*HOUR:
                return timestamp
    return None


def influx_measurement_health(state: dict, item, now: int) -> dict:
    """Evaluate six-hour probe health independently of hourly cache age."""
    success=state.get("last_success") or {}
    outcome=state.get("last_attempt_outcome") or {}
    attempted_at=state.get("last_attempt_at")
    measured_at=success.get("measured_at")
    interruption=state.get("last_interruption") or {}
    next_eligible=next_influx_probe_opportunity(attempted_at,now)
    failures=int(state.get("consecutive_failures") or 0)
    overdue=bool(attempted_at is not None and next_eligible is not None
                 and int(now)>=int(next_eligible)+HOUR)
    item_status=(item.get("status") if isinstance(item,dict)
                 else getattr(item,"status",None))
    if failures>=2:
        status="prolonged_unavailable"
        reason=f"{failures} consecutive eligible Supervisor probes failed"
    elif overdue:
        status="prolonged_unavailable"
        reason="An eligible scheduled Supervisor probe opportunity is overdue"
    elif outcome.get("status")=="interrupted":
        status="probe_interrupted"
        reason=("Last eligible Supervisor probe was interrupted; waiting for "
                "the next scheduled opportunity")
    elif outcome.get("status")=="failure":
        status="probe_failed"
        reason="Last eligible Supervisor probe failed; waiting for the next scheduled opportunity"
    elif outcome.get("status")=="in_progress":
        status="probe_in_progress"
        reason="An eligible Supervisor probe is currently in progress"
    elif item_status=="ok":
        status="fresh"
        reason="Fresh Supervisor measurement"
    elif item_status=="cached":
        status="cached"
        reason="Waiting for next eligible Supervisor probe"
    elif item_status=="stale" and success:
        status="stale_waiting"
        reason="Stale historical measurement; waiting for next eligible Supervisor probe"
    else:
        status="unavailable"
        reason="No verified InfluxDB add-on size measurement is available"
    return {
        "status":status,
        "reason":reason,
        "should_alert":failures>=2 or overdue,
        "consecutive_failures":failures,
        "last_success_at":measured_at,
        "last_attempt_at":attempted_at,
        "last_attempt_status":outcome.get("status") or "unknown",
        "last_attempt_detail":outcome.get("detail") or "No durable probe outcome recorded",
        "last_interrupted_at":interruption.get("attempted_at"),
        "interruption_detected_at":interruption.get("detected_at"),
        "measurement_age_seconds":(max(0,int(now)-int(measured_at))
                                   if measured_at is not None else None),
        "next_eligible_at":next_eligible,
    }

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register commands before a config entry loads, no production changes."""
    websocket_api.async_register_command(hass, ws_summary)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_writers)
    websocket_api.async_register_command(hass, ws_acknowledge)
    websocket_api.async_register_command(hass, ws_retire_source)
    websocket_api.async_register_command(hass, ws_reactivate_source)
    static_file = Path(__file__).parent/"frontend"/"storage-monitor-card.js"
    await hass.http.async_register_static_paths([
        StaticPathConfig("/ha_storage_monitor/storage-monitor-card.js",str(static_file),False)
    ])
    return True

class StorageMonitor:
    def __init__(self,hass,entry):
        self.hass=hass
        self.entry=entry
        self.db=SnapshotDB(hass.config.path(".storage","ha_storage_monitor_history.sqlite3"))
        self.unsub=None
        self._running=False
        self._pending_check=False
        self._last_disk_low=False
        self.options=resolved_options(entry.data,entry.options)
        self.writer_diagnostics_enabled=False
        self.writer_diagnostics_enabled_at: int | None=None

    async def start(self):
        await self.hass.async_add_executor_job(self.db.initialize)
        self.writer_diagnostics_enabled=bool(
            self.options["enable_recorder_writer_diagnostics"])
        self.writer_diagnostics_enabled_at=await self.hass.async_add_executor_job(
            self.db.configure_writer_diagnostics,
            self.writer_diagnostics_enabled,int(time.time()))
        self.unsub = async_track_time_change(self.hass,self._hourly,minute=0,second=10)
        # Cold start updates current measurements without firing historical growth alerts.
        self.entry.async_create_background_task(
            self.hass,self.sample(False,startup=True),"Storage Monitor startup")

    @callback
    def _hourly(self, now: datetime):
        local = now.astimezone(TZ)
        # The scheduler itself is pinned to minute 00. If the event loop runs
        # this callback late within the same hour, do not silently lose a
        # 00/06/12/18 checkpoint merely because ``local.minute`` is nonzero.
        self.entry.async_create_background_task(
            self.hass,self.sample(local.hour in ALARM_HOURS),
            "Storage Monitor hourly sample")

    async def sample(self, check: bool, *, startup: bool=False):
        if self._running:
            # A cold-start sample can overlap the first scheduled checkpoint.
            # Queue that checkpoint instead of dropping it; the immutable
            # writer-window key still prevents duplicate Recorder reads.
            self._pending_check = self._pending_check or check
            return
        self._running=True
        try:
            now=int(time.time())
            hour=utc_hour(now)
            local_now=datetime.fromtimestamp(now,TZ)
            samples=await self.hass.async_add_executor_job(collect_local,self.hass.config.config_dir,hour,now)
            # A physically absent optional database is quiet until it has
            # genuinely been measured. The durable marker lives only in our
            # monitor SQLite and is seeded from existing snapshots on upgrade.
            for index, sample in enumerate(samples):
                if (sample.source == HOME_ENERGY_INTELLIGENCE_SOURCE and
                        sample.status == "not_configured"):
                    was_measured = await self.hass.async_add_executor_job(
                        self.db.source_was_successful, sample.source)
                    if was_measured:
                        samples[index] = Sample(
                            hour, sample.source, None, "unavailable",
                            sample.method, None, detail=(
                                "Previously measured Home Energy Intelligence "
                                "database is now missing"))
            # A startup or local 00/06/12/18 checkpoint may request the
            # expensive Supervisor scan. Reserve the attempt durably first:
            # failures and quick restarts still observe the strict six elapsed
            # hour minimum. Exact checkpoint alignment deliberately yields to
            # this safety limit when the two requirements conflict.
            requested_influx = startup or check
            fetch_influx = False
            if requested_influx:
                fetch_influx=await self.hass.async_add_executor_job(
                    self.db.reserve_influx_probe,now,6*HOUR)
            probe_state=await self.hass.async_add_executor_job(
                self.db.influx_probe_state,now)
            if fetch_influx:
                try:
                    usage=await get_influx_usage(
                        async_get_clientsession(self.hass),DEFAULT_INFLUX_SLUG)
                    current=Sample(hour,"influx_addon",usage.size,usage.status,
                                   "supervisor_addons_data_total",
                                   now if usage.status=="ok" else None,
                                   detail=usage.detail)
                except Exception as err:
                    current=Sample(hour,"influx_addon",None,"unavailable",
                                   "supervisor_addons_data_total",None,
                                   detail=f"Supervisor probe failed: {type(err).__name__}")
                samples.append(current)
                await self.hass.async_add_executor_job(
                    self.db.save_influx_result,now,current)
            else:
                cached=probe_state["last_success"]
                if cached:
                    age=max(0,now-int(cached["measured_at"]))
                    status="cached" if age < 7*HOUR else "stale"
                    outcome=probe_state.get("last_attempt_outcome") or {}
                    if outcome.get("status")=="interrupted":
                        reason=("Last eligible Supervisor probe was interrupted; "
                                "showing only the previous verified historical size")
                    elif outcome.get("status")=="failure":
                        reason=("Last eligible Supervisor probe failed; showing only "
                                "the previous verified historical size")
                    elif outcome.get("status")=="in_progress":
                        reason=("Supervisor probe is in progress; showing the previous "
                                "verified historical size until it completes")
                    elif requested_influx:
                        reason=("Waiting for next eligible Supervisor probe; "
                                "six-hour safety minimum has not elapsed")
                    else:
                        reason="Waiting for next eligible Supervisor probe"
                    samples.append(Sample(
                        hour,"influx_addon",int(cached["size"]),status,
                        str(cached.get("method") or "supervisor_addons_data_total"),
                        int(cached["measured_at"]),cached.get("allocated"),
                        detail=reason))
                else:
                    detail=("Six-hour safety minimum deferred this requested probe; "
                            "no successful result is cached" if requested_influx else
                            "No successful Supervisor probe is cached")
                    samples.append(Sample(hour,"influx_addon",None,"unavailable",
                                          "supervisor_addons_data_total",None,detail=detail))
            # Retirement is monitor-owned metadata. Reappearing retired files
            # remain excluded until an administrator explicitly reactivates
            # them, so old and new evidence is never mixed silently.
            retired=await self.hass.async_add_executor_job(
                self.db.retired_source_keys)
            detected={s.source for s in samples
                      if s.source in retired and s.status != "not_configured"}
            if detected:
                await self.hass.async_add_executor_job(
                    self.db.note_retired_detected,detected,now)
            samples=self._exclude_retired_sources(samples,retired,hour,now)

            # Record disappeared active local SQLite files as unavailable, not
            # as a false zero. Explicitly retired sources are intentionally
            # absent and must not invalidate the active aggregate.
            former=await self.hass.async_add_executor_job(self.db.latest)
            discovered={s.source for s in samples}
            missing_databases = False
            for old in former:
                key=old["source"]
                if (key.startswith("database:") and key not in retired and
                        key not in discovered):
                    missing_databases = True
                    samples.append(Sample(hour,key,None,"unavailable","file_stat",None,
                                          detail="previously monitored database not found"))
            if missing_databases:
                samples = [Sample(hour,"database_total",None,"unavailable","sum_discovered_local_sqlite_files",None,
                           detail="Previously monitored database missing") if s.source=="database_total" else s for s in samples]
            await self.hass.async_add_executor_job(self.db.save,samples)
            if check:
                if self.writer_diagnostics_enabled:
                    # Four bounded LOCAL checkpoint windows per day. Never
                    # query an interval that began before explicit opt-in.
                    start_ts, end_ts = local_checkpoint_window(local_now)
                    already = await self.hass.async_add_executor_job(
                        self.db.writer_window_exists,end_ts)
                    diagnostic = await self.hass.async_add_executor_job(partial(
                        collect_writer_checkpoint,
                        self.hass.config.path("home-assistant_v2.db"),
                        start_ts,end_ts,enabled=True,
                        enabled_at=self.writer_diagnostics_enabled_at,
                        already_exists=already))
                    if diagnostic is not None:
                        await self.hass.async_add_executor_job(
                            self.db.save_writer_window,diagnostic)
                await self.check_growth(hour,local_now)
            await self.check_disk(samples,hour,local_now)
            await self.check_stale(samples,hour,local_now)
            # Only snapshots owned by THIS integration may be pruned.
            if local_now.hour==3 and check is False:
                retain=int(self.options.get("retention_days",365))
                await self.hass.async_add_executor_job(self.db.compact_retention,hour-retain*DAY)
                await self.hass.async_add_executor_job(self.db.prune_writer_windows,hour-9*DAY)
        except Exception:
            _LOGGER.exception("Storage Monitor sample failed; production data untouched")
        finally:
            self._running=False
            if self._pending_check:
                self._pending_check=False
                self.entry.async_create_background_task(
                    self.hass,self.sample(True),
                    "Storage Monitor deferred checkpoint")

    @staticmethod
    def _exclude_retired_sources(samples: list[Sample], retired: set[str],
                                 hour: int, now: int) -> list[Sample]:
        """Exclude retired current evidence and correct the generic total."""
        if not retired:
            return samples
        generic=[s for s in samples if s.source.startswith("database:") and
                 s.source != HOME_ENERGY_INTELLIGENCE_SOURCE]
        active=[s for s in generic if s.source not in retired]
        retired_discovered=[s for s in generic if s.source in retired]
        result=[]
        for sample in samples:
            if sample.source in retired:
                continue
            if sample.source != "database_total":
                result.append(sample)
                continue
            can_recalculate=(
                sample.status == "ok" or
                (sample.detail == "A currently discovered database is unreadable" and
                 all(item.status == "ok" for item in active) and
                 any(item.status != "ok" for item in retired_discovered)))
            if can_recalculate:
                allocated=(sum(int(item.allocated) for item in active)
                           if all(item.allocated is not None for item in active)
                           else None)
                result.append(Sample(
                    hour,"database_total",sum(int(item.size) for item in active),
                    "ok","sum_discovered_local_sqlite_files",now,allocated,
                    detail="Currently discovered non-retired local SQLite files"))
            else:
                result.append(sample)
        return result

    @staticmethod
    def _latest_without_retired(items: list[dict], retired: set[str]) -> list[dict]:
        """Return an exact current view without rewriting stored evidence."""
        if not retired:
            return items
        total=next((row for row in items if row["source"]=="database_total"),None)
        generic=[row for row in items if row["source"].startswith("database:")
                 and row["source"] != HOME_ENERGY_INTELLIGENCE_SOURCE]
        active=[row for row in generic if row["source"] not in retired]
        allowed_detail={"Previously monitored database missing",
                        "A currently discovered database is unreadable"}
        exact=(total is not None and
               (total["status"]=="ok" or total.get("detail") in allowed_detail) and
               all(row["status"]=="ok" and row["hour"]==total["hour"]
                   for row in active))
        adjusted=None
        if exact:
            adjusted=dict(total)
            adjusted.update({
                "size":sum(int(row["size"]) for row in active),
                "status":"ok",
                "measured_at":int(total["hour"]),
                "allocated":(sum(int(row["allocated"]) for row in active)
                             if all(row["allocated"] is not None for row in active)
                             else None),
                "detail":"Currently discovered non-retired local SQLite files",
            })
        return [adjusted if row["source"]=="database_total" and adjusted else row
                for row in items if row["source"] not in retired]

    async def check_growth(self,hour: int,local_now: datetime):
        keys=["recorder_total","influx_addon"]
        latest=await self.hass.async_add_executor_job(self.db.latest)
        retired=await self.hass.async_add_executor_job(self.db.retired_source_keys)
        keys.extend(x["source"] for x in latest
                    if x["source"].startswith("database:") and
                    x["source"] not in retired)
        window=f"{local_now.date().isoformat()}:{(local_now.hour//6)*6:02d}"
        for key in keys:
            evidence=await self.hass.async_add_executor_job(
                partial(self.db.growth_evidence,key,hour,
                        hourly_source=key!="influx_addon"))
            reactivated=await self.hass.async_add_executor_job(
                self.db.reactivated_at,key)
            if reactivated is not None and hour < reactivated + 8*DAY:
                evidence={"points":[],"observed_shrink":False,
                          "hourly_coverage_ok":False}
            ev=evaluate(key,evidence["points"],hour,
                min_excess_bytes=int(self.options.get("min_excess_mib",0))*1024*1024,
                cadence_hours=6,
                observed_shrink=evidence["observed_shrink"],
                hourly_coverage_ok=evidence["hourly_coverage_ok"])
            previous=await self.hass.async_add_executor_job(self.db.get_alert,key)
            if ev.status=="alarm":
                permitted=allow_notification(previous,window,hour)
                should_notify=permitted and (not previous["active"] or bool(self.options.get("repeat_notifications",False)))
                if should_notify:
                    # Persist the cooldown before handing off to HA's
                    # notification service. A transient service failure must
                    # not turn an hourly sample into a notification storm.
                    await self._set_alert(key,active=True,window=window,
                                          now=hour,notified=True)
                    await self._notify_safely("growth:"+key,"Storage growth warning",
                        f"{key}: last 24h +{ev.delta24/1024**2:.1f} MiB, prior 7d daily "
                        f"+{ev.baseline_daily/1024**2:.1f} MiB, "
                        f"{ev.excess_ratio*100:+.1f}% difference. Verified {local_now.isoformat()}. "
                        "Open Storage Monitor dashboard for details.")
                else:
                    await self._set_alert(key,active=True,
                                          window=previous["last_window"],now=hour)
            elif ev.status in ("normal","maintenance"):
                # Maintenance resets the alert. Learning does not silently resolve it.
                await self._set_alert(key,active=False,
                                      window=previous["last_window"],now=hour)

    async def check_disk(self,samples: list[Sample],hour: int,local_now: datetime):
        sizes={s.source:s.size for s in samples if s.status=="ok"}
        free=sizes.get("disk_free")
        recorder=sizes.get("recorder_total")
        if free is None:
            return
        is_low,threshold=disk_low_evidence(
            free,recorder,int(self.options.get("low_disk_gib",15))*1024**3)
        assert is_low is not None
        prev=await self.hass.async_add_executor_job(self.db.get_alert,"disk_low")
        window=f"{local_now.date().isoformat()}:{(local_now.hour//6)*6:02d}"
        notify=is_low and allow_notification(prev,window,hour) and (not prev["active"] or
                            bool(self.options.get("repeat_notifications",False)))
        if notify:
            await self._set_alert("disk_low",active=True,window=window,
                                  now=hour,notified=True)
            await self._notify_safely("disk_low","CRITICAL: Home Assistant disk space",
                f"Free on HA /config mount: {free/1024**3:.2f} GiB, "
                f"warning below {threshold/1024**3:.2f} GiB. Includes a minimum "
                "space requirement equal to the Recorder DB footprint for safe repacking. "
                "Do not delete live WAL/SHM files.")
        else:
            await self._set_alert("disk_low",active=is_low,
                                  window=prev["last_window"],now=hour)

    async def check_stale(self,samples: list[Sample],hour: int,local_now: datetime):
        # Keep the two-consecutive-hour rule only for genuinely hourly sources.
        # InfluxDB has a separate attempt-aware six-hour health policy below.
        keys={"recorder_total","disk_free"}
        keys.update(s.source for s in samples if s.source.startswith("database:"))
        bykey={s.source:s for s in samples}
        window=f"{local_now.date().isoformat()}:{(local_now.hour//6)*6:02d}"
        for key in keys:
            item=bykey.get(key)
            if not item: continue
            previous=await self.hass.async_add_executor_job(self.db.get_alert,"stale:"+key)
            if item.status == "not_configured":
                if previous["active"]:
                    await self._set_alert("stale:"+key,active=False,
                                          window=previous["last_window"],now=hour)
                    await self._dismiss_safely("stale_"+key)
                continue
            fresh=item.status=="ok"
            last=await self.hass.async_add_executor_job(self.db.last_two,key)
            reactivated=await self.hass.async_add_executor_job(
                self.db.reactivated_at,key)
            if reactivated is not None:
                first_new_hour=utc_hour(reactivated)+HOUR
                last=[row for row in last if row["hour"]>=first_new_hour]
            stale=(not fresh and len(last)==2 and
                   last[0]["hour"]-last[1]["hour"]==HOUR and
                   all(x["status"] in ("unavailable","stale") for x in last))
            notify=stale and allow_notification(previous,window,hour) and (
                 not previous["active"] or bool(self.options.get("repeat_notifications",False)))
            if notify:
                await self._set_alert("stale:"+key,active=True,window=window,
                                      now=hour,notified=True)
                await self._notify_safely("stale_"+key,"Storage Monitor measurement unavailable",
                    f"{key} has missed two consecutive hourly samples. "
                    "Treat its size as unknown, not zero. Check permissions and monitoring status.")
            if stale or fresh:
                if not notify:
                    await self._set_alert("stale:"+key,active=stale,
                                          window=previous["last_window"],now=hour)
        influx=bykey.get("influx_addon")
        if influx is not None:
            await self.check_influx_health(influx,hour,local_now)

    async def check_influx_health(self,item: Sample,hour: int,local_now: datetime):
        """Alert on failed/overdue probe opportunities, never cache aging alone."""
        state=await self.hass.async_add_executor_job(
            self.db.influx_probe_state,int(local_now.timestamp()))
        now=int(local_now.timestamp())
        health=influx_measurement_health(state,item,now)
        key="stale:influx_addon"
        previous=await self.hass.async_add_executor_job(self.db.get_alert,key)
        outcome=health["last_attempt_status"]
        active=bool(health["should_alert"] or (
            previous["active"] and health["consecutive_failures"]>0
            and outcome!="success"))
        window=f"{local_now.date().isoformat()}:{(local_now.hour//6)*6:02d}"
        notify=active and allow_notification(previous,window,hour) and (
            not previous["active"] or bool(self.options.get("repeat_notifications",False)))
        if notify:
            await self._set_alert(key,active=True,window=window,now=hour,notified=True)
            success=(datetime.fromtimestamp(health["last_success_at"],TZ).isoformat()
                     if health["last_success_at"] is not None else "Unavailable")
            attempted=(datetime.fromtimestamp(health["last_attempt_at"],TZ).isoformat()
                       if health["last_attempt_at"] is not None else "Unavailable")
            next_probe=(datetime.fromtimestamp(health["next_eligible_at"],TZ).isoformat()
                        if health["next_eligible_at"] is not None else "Undetermined")
            age=(f"{health['measurement_age_seconds']/HOUR:.1f} hours"
                 if health["measurement_age_seconds"] is not None else "Unknown")
            await self._notify_safely(
                "stale_influx_addon","InfluxDB add-on size measurement unavailable",
                f"Storage Monitor cannot currently verify the InfluxDB add-on disk usage. "
                f"Last successful measurement: {success}. Last attempted probe: {attempted} "
                f"({outcome}: {health['last_attempt_detail']}). Measurement age: {age}. "
                f"Reason: {health['reason']}. Next eligible probe opportunity: {next_probe}. "
                "This reports measurement availability, not whether the InfluxDB service is running.")
        elif active:
            await self._set_alert(key,active=True,window=previous["last_window"],now=hour)
        elif previous["active"]:
            await self._set_alert(key,active=False,window=previous["last_window"],now=hour)
            await self._dismiss_safely("stale_influx_addon")

    async def _set_alert(self,source: str,*,active: bool,window: str | None,
                         now: int,notified: bool=False):
        """Write only to the monitor DB using HA's positional executor API."""
        await self.hass.async_add_executor_job(partial(
            self.db.set_alert,source,active=active,window=window,now=now,
            notified=notified))

    async def _notify(self,ident,title,message):
        await self.hass.services.async_call("persistent_notification","create",
             {"title":title,"message":message,"notification_id":"ha_storage_monitor_"+ident.replace(":","_")},blocking=False)

    async def _notify_safely(self,ident,title,message):
        """Deliver after durable cooldown state; do not retry on every sample."""
        try:
            await self._notify(ident,title,message)
        except Exception:
            _LOGGER.exception(
                "Storage Monitor notification delivery failed; cooldown persisted")

    async def _dismiss_safely(self,ident):
        """Dismiss only this integration's own resolved persistent notification."""
        try:
            await self.hass.services.async_call(
                "persistent_notification","dismiss",
                {"notification_id":"ha_storage_monitor_"+ident.replace(":","_")},
                blocking=False)
        except Exception:
            _LOGGER.exception("Storage Monitor resolved notification dismissal failed")

    async def retire_source(self,source: str) -> dict:
        result=await self.hass.async_add_executor_job(
            self.db.retire_source,source,int(time.time()))
        # Durable alert state is cleared in the same monitor-DB transaction;
        # notification delivery state cannot undo that safety guarantee.
        await self._dismiss_safely("growth:"+source)
        await self._dismiss_safely("stale_"+source)
        return result

    async def reactivate_source(self,source: str) -> dict:
        return await self.hass.async_add_executor_job(
            self.db.reactivate_source,source,int(time.time()))

    @staticmethod
    def _writer_checkpoint_end(now: int) -> int:
        local_now=datetime.fromtimestamp(int(now),TZ)
        checkpoint=local_now.replace(hour=(local_now.hour//6)*6,
                                     minute=0,second=0,microsecond=0)
        return int(checkpoint.timestamp())

    async def writer_ranking(self,limit: int,page: int) -> dict:
        """Return one bounded page derived only from monitor-owned history."""
        if type(limit) is not int or limit not in WRITER_RANKING_LIMITS:
            raise ValueError("Invalid writer ranking limit")
        if type(page) is not int or page < 1:
            raise ValueError("Invalid writer ranking page")
        if not self.writer_diagnostics_enabled:
            result=disabled_writer_summary()
            result.update({"selected_limit":limit,"page":1,
                           "page_size":WRITER_PAGE_SIZE,"total_pages":0,
                           "selected_entities_count":0,
                           "ranked_entities_available":0,
                           "grouped_by_integration":[]})
            return result
        checkpoint_end=self._writer_checkpoint_end(int(time.time()))
        rows=await self.hass.async_add_executor_job(
            self.db.writer_window_history,checkpoint_end-9*DAY,checkpoint_end)
        result=summarize_writer_windows(rows,checkpoint_end,limit)
        registry=er.async_get(self.hass)
        grouped={}
        for item in result["entities"]:
            reg=registry.async_get(item["entity_id"])
            platform=reg.platform if reg else "unknown"
            item["integration"]=platform
            group=grouped.setdefault(platform,{"integration":platform,
                "rows_6h":0,"rows_24h":0 if result["last24_complete"] else None,
                "entities_in_selected":0})
            group["rows_6h"] += int(item.get("rows_6h") or 0)
            if result["last24_complete"]:
                group["rows_24h"] += int(item.get("rows_24h") or 0)
            group["entities_in_selected"] += 1
        result["grouped_by_integration"]=sorted(
            grouped.values(),key=lambda row:(
                -(row["rows_24h"] if row["rows_24h"] is not None
                  else row["rows_6h"]),row["integration"]))
        total=len(result["entities"])
        total_pages=(total+WRITER_PAGE_SIZE-1)//WRITER_PAGE_SIZE
        if page > max(1,total_pages):
            raise ValueError("Writer ranking page exceeds available results")
        start=(page-1)*WRITER_PAGE_SIZE
        result["entities"]=result["entities"][start:start+WRITER_PAGE_SIZE]
        result.update({"page":page,"page_size":WRITER_PAGE_SIZE,
                       "total_pages":total_pages})
        return result

    async def summary(self):
        now=utc_hour(int(time.time()))
        checkpoint_end=self._writer_checkpoint_end(now)
        retired_sources=await self.hass.async_add_executor_job(
            self.db.retired_sources)
        retired={row["source"] for row in retired_sources}
        all_items=await self.hass.async_add_executor_job(self.db.latest)
        items=self._latest_without_retired(all_items,retired)
        keys=[s["source"] for s in items]
        alerts={}
        growth={}
        for key in keys:
            alerts[key]=await self.hass.async_add_executor_job(self.db.get_alert,key)
            if key in GROWTH_SOURCES or key.startswith("database:"):
                evidence=await self.hass.async_add_executor_job(
                    partial(self.db.growth_evidence,key,checkpoint_end,
                            hourly_source=key!="influx_addon"))
                reactivated=await self.hass.async_add_executor_job(
                    self.db.reactivated_at,key)
                if (reactivated is not None and
                        checkpoint_end < reactivated + 8*DAY):
                    evidence={"points":[],"observed_shrink":False,
                              "hourly_coverage_ok":False}
                growth[key]=evaluate(key,evidence["points"],checkpoint_end,cadence_hours=6,
                    observed_shrink=evidence["observed_shrink"],
                    hourly_coverage_ok=evidence["hourly_coverage_ok"]).as_json()
        alerts["disk_low"]=await self.hass.async_add_executor_job(self.db.get_alert,"disk_low")
        for key in keys:
            alerts["stale:"+key]=await self.hass.async_add_executor_job(self.db.get_alert,"stale:"+key)
        influx_item=next((item for item in items if item["source"]=="influx_addon"),None)
        health_now=int(time.time())
        influx_state=await self.hass.async_add_executor_job(
            self.db.influx_probe_state,health_now)
        influx_health=(influx_measurement_health(influx_state,influx_item,health_now)
                       if influx_item is not None else None)
        if influx_health is not None:
            influx_health["alert_active"]=bool(
                alerts.get("stale:influx_addon",{}).get("active"))
            if influx_health["alert_active"]:
                influx_health["status"]="prolonged_unavailable"
        # One-hour history is in OUR sqlite, independent of HA Recorder retention.
        changes={}
        current_items={row["source"]:row for row in items}
        membership_change=await self.hass.async_add_executor_job(
            self.db.latest_source_membership_change)
        membership_hour=(utc_hour(membership_change)+HOUR
                         if membership_change is not None else None)
        for key in keys:
            hist=await self.hass.async_add_executor_job(self.db.history,key,now-24*HOUR,26)
            samples_by_hour={x["hour"]:x for x in hist if x["size"] is not None and x["status"]=="ok"}
            displayed=current_items.get(key)
            current=(displayed if displayed and displayed["hour"]==now and
                     displayed["size"] is not None and displayed["status"]=="ok"
                     else None)
            reactivated=await self.hass.async_add_executor_job(
                self.db.reactivated_at,key)
            first_new_hour=(utc_hour(reactivated)+HOUR
                            if reactivated is not None else None)
            changes[key]={}
            for delta in (6,24):
                older=samples_by_hour.get(now-delta*HOUR)
                changes[key]["delta"+str(delta)+"h"] = (
                    current["size"]-older["size"]
                    if (current and older and
                        (first_new_hour is None or older["hour"]>=first_new_hour) and
                        (key!="database_total" or membership_hour is None or
                         (current["hour"]>=membership_hour and
                          older["hour"]>=membership_hour)))
                    else None)
        # Disabled means no Recorder access and no historical results presented
        # as current. Enabled dashboards read only the monitor's own summaries.
        if self.writer_diagnostics_enabled:
            window_rows = await self.hass.async_add_executor_job(
                self.db.writer_window_history,checkpoint_end-9*DAY,checkpoint_end)
            writers = summarize_writer_windows(window_rows,checkpoint_end)
            writers["enabled_at"] = self.writer_diagnostics_enabled_at
            # Entity rows have a separate bounded/paginated WebSocket route.
            # Keep periodic summary refreshes small and Recorder-independent.
            writers["entities"] = []
        else:
            writers = disabled_writer_summary()
        return {"samples":items,"alerts":alerts,"growth":growth,"writers":writers,
                "retired_sources":retired_sources,
                "influx_health":influx_health,
                "now":int(time.time()),"timezone":"Europe/Ljubljana","changes":changes,
                "writer_diagnostics_enabled":self.writer_diagnostics_enabled,
                "effective_options":{
                    "low_disk_gib":int(self.options["low_disk_gib"]),
                    "min_excess_mib":int(self.options["min_excess_mib"]),
                    "retention_days":int(self.options["retention_days"]),
                    "repeat_notifications":bool(self.options["repeat_notifications"]),
                    "enable_recorder_writer_diagnostics":self.writer_diagnostics_enabled,
                },
                "baseline":"24h size change compared with average DAILY growth of preceding seven days; strictly >5%",
                "scope":"Filesystem file stat reports apparent bytes, disk usage is scoped to /config path, Supervisor is add-on total"}

async def async_setup_entry(hass,entry):
    monitor=StorageMonitor(hass,entry)
    hass.data.setdefault(DOMAIN,{})[entry.entry_id]=monitor
    await monitor.start()
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True

async def _reload(hass,entry):
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass,entry):
    monitor=hass.data.get(DOMAIN,{}).pop(entry.entry_id,None)
    if monitor and monitor.unsub:
        monitor.unsub()
    return True


def _monitor(hass):
    return next(iter(hass.data.get(DOMAIN,{}).values()),None)

@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"):"ha_storage_monitor/summary"})
@websocket_api.async_response
async def ws_summary(hass,connection,msg):
    m=_monitor(hass)
    if m is None:
        connection.send_error(msg["id"],"not_loaded","Storage Monitor not installed")
        return
    connection.send_result(msg["id"],await m.summary())

@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"):"ha_storage_monitor/history",
                                   vol.Required("source"):str,
                                   vol.Optional("days",default=7):vol.In((1,7,30,90))})
@websocket_api.async_response
async def ws_history(hass,connection,msg):
    m=_monitor(hass)
    if m is None:
        connection.send_error(msg["id"],"not_loaded","Monitor not installed")
        return
    source=msg["source"]
    # Only exact, previously observed keys from monitor's own store.
    current=await hass.async_add_executor_job(m.db.latest)
    if source not in {x["source"] for x in current}:
        connection.send_error(msg["id"],"unknown_source","Unknown source")
        return
    rows=await hass.async_add_executor_job(m.db.history,source,
                                           utc_hour(time.time())-int(msg["days"])*DAY,2200)
    connection.send_result(msg["id"],{"source":source,"samples":rows})

@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"):"ha_storage_monitor/writers",
                                   vol.Optional("limit",default=20):vol.In(
                                       WRITER_RANKING_LIMITS),
                                   vol.Optional("page",default=1):int})
@websocket_api.async_response
async def ws_writers(hass,connection,msg):
    m=_monitor(hass)
    if m is None:
        connection.send_error(msg["id"],"not_loaded","Monitor not installed")
        return
    try:
        result=await m.writer_ranking(int(msg["limit"]),int(msg["page"]))
    except (TypeError,ValueError):
        connection.send_error(msg["id"],"invalid_ranking_request",
                              "Invalid writer limit or page")
        return
    connection.send_result(msg["id"],result)

@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"):"ha_storage_monitor/acknowledge",
                                   vol.Required("source"):str,
                                   vol.Optional("hours",default=24):vol.In((24,168))})
@websocket_api.async_response
async def ws_acknowledge(hass,connection,msg):
    m=_monitor(hass)
    if m is None:
        connection.send_error(msg["id"],"not_loaded","Monitor not installed")
        return
    observed_sources={x["source"] for x in await hass.async_add_executor_job(m.db.latest)}
    valid_sources=allowed_acknowledgement_sources(observed_sources)
    if msg["source"] not in valid_sources:
        connection.send_error(msg["id"],"unknown_source","Unknown source")
        return
    await hass.async_add_executor_job(m.db.acknowledge,msg["source"],int(time.time())+int(msg["hours"])*HOUR)
    connection.send_result(msg["id"],{"acknowledged":True,"hours":msg["hours"]})

@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"):"ha_storage_monitor/retire_source",
                                   vol.Required("source"):str})
@websocket_api.async_response
async def ws_retire_source(hass,connection,msg):
    m=_monitor(hass)
    if m is None:
        connection.send_error(msg["id"],"not_loaded","Monitor not installed")
        return
    try:
        result=await m.retire_source(msg["source"])
    except ValueError as err:
        connection.send_error(msg["id"],"invalid_source",str(err))
        return
    connection.send_result(msg["id"],result)

@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"):"ha_storage_monitor/reactivate_source",
                                   vol.Required("source"):str})
@websocket_api.async_response
async def ws_reactivate_source(hass,connection,msg):
    m=_monitor(hass)
    if m is None:
        connection.send_error(msg["id"],"not_loaded","Monitor not installed")
        return
    try:
        result=await m.reactivate_source(msg["source"])
    except ValueError as err:
        connection.send_error(msg["id"],"invalid_source",str(err))
        return
    connection.send_result(msg["id"],result)
