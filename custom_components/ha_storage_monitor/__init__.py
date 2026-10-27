"""Independent Storage Monitor: local hourly metadata, never changes production DBs."""
from __future__ import annotations
from datetime import datetime
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
from .sources import collect_local
from .supervisor import get_influx_usage
from .writers import collect_writer_checkpoint, disabled_writer_summary, local_checkpoint_window, summarize_writer_windows

_LOGGER = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Ljubljana")

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register commands before a config entry loads, no production changes."""
    websocket_api.async_register_command(hass, ws_summary)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_acknowledge)
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
        self.cache_influx: Sample | None=None
        self.last_influx_probe: int | None=None
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
            self.hass,self.sample(False),"Storage Monitor startup")

    @callback
    def _hourly(self, now: datetime):
        local = now.astimezone(TZ)
        # The scheduler itself is pinned to minute 00. If the event loop runs
        # this callback late within the same hour, do not silently lose a
        # 00/06/12/18 checkpoint merely because ``local.minute`` is nonzero.
        self.entry.async_create_background_task(
            self.hass,self.sample(local.hour in ALARM_HOURS),
            "Storage Monitor hourly sample")

    async def sample(self, check: bool):
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
            # Directory walk capped at once per 6h (or cold boot), never every hour.
            # Do not walk the Supervisor directory tree more than once in any
            # six elapsed hours, including immediately after cold startup.
            fetch_influx=(self.last_influx_probe is None or
                          (check and now-self.last_influx_probe>=6*HOUR))
            if fetch_influx:
                self.last_influx_probe=now
                usage=await get_influx_usage(async_get_clientsession(self.hass),DEFAULT_INFLUX_SLUG)
                self.cache_influx=Sample(hour,"influx_addon",usage.size,usage.status,
                                         "supervisor_addons_data_total",now if usage.status=="ok" else None,
                                         detail=usage.detail)
            if self.cache_influx and not fetch_influx and self.cache_influx.status=="ok":
                samples.append(Sample(hour,"influx_addon",self.cache_influx.size,"cached",
                                      "supervisor_addons_data_total",self.cache_influx.measured_at,
                                      detail="Previously measured; not a fresh hourly read"))
            else:
                src=self.cache_influx
                samples.append(Sample(hour,"influx_addon",src.size if src else None,
                                      src.status if src else "unavailable","supervisor_addons_data_total",
                                      src.measured_at if src else None,detail=src.detail if src else "No probe"))
            # Record disappeared local SQLite files as unavailable, not as a false zero.
            former=await self.hass.async_add_executor_job(self.db.latest)
            discovered={s.source for s in samples}
            missing_databases = False
            for old in former:
                key=old["source"]
                if key.startswith("database:") and key not in discovered:
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

    async def check_growth(self,hour: int,local_now: datetime):
        keys=["recorder_total","influx_addon"]
        latest=await self.hass.async_add_executor_job(self.db.latest)
        keys.extend(x["source"] for x in latest if x["source"].startswith("database:"))
        window=f"{local_now.date().isoformat()}:{(local_now.hour//6)*6:02d}"
        for key in keys:
            evidence=await self.hass.async_add_executor_job(
                partial(self.db.growth_evidence,key,hour,
                        hourly_source=key!="influx_addon"))
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
        # Two missing consecutive hourly observations. Cached Influx is only
        # temporarily accepted if its last verified age is <=7 hours.
        keys={"recorder_total","disk_free","influx_addon"}
        keys.update(s.source for s in samples if s.source.startswith("database:"))
        bykey={s.source:s for s in samples}
        window=f"{local_now.date().isoformat()}:{(local_now.hour//6)*6:02d}"
        for key in keys:
            item=bykey.get(key)
            if not item: continue
            fresh=(item.status=="ok" or (key=="influx_addon" and item.status=="cached"
                    and item.measured_at is not None and hour-item.measured_at < 7*HOUR))
            previous=await self.hass.async_add_executor_job(self.db.get_alert,"stale:"+key)
            last=await self.hass.async_add_executor_job(self.db.last_two,key)
            stale=(not fresh and len(last)==2 and last[0]["hour"]-last[1]["hour"]==HOUR
                   and all(x["status"]=="unavailable" or (x["measured_at"] is None)
                           for x in last))
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

    async def summary(self):
        now=utc_hour(int(time.time()))
        local_now = datetime.fromtimestamp(now,TZ)
        checkpoint_local = local_now.replace(hour=(local_now.hour//6)*6,
                                             minute=0,second=0,microsecond=0)
        checkpoint_end = int(checkpoint_local.timestamp())
        items=await self.hass.async_add_executor_job(self.db.latest)
        keys=[s["source"] for s in items]
        alerts={}
        growth={}
        for key in keys:
            alerts[key]=await self.hass.async_add_executor_job(self.db.get_alert,key)
            if key in GROWTH_SOURCES or key.startswith("database:"):
                evidence=await self.hass.async_add_executor_job(
                    partial(self.db.growth_evidence,key,checkpoint_end,
                            hourly_source=key!="influx_addon"))
                growth[key]=evaluate(key,evidence["points"],checkpoint_end,cadence_hours=6,
                    observed_shrink=evidence["observed_shrink"],
                    hourly_coverage_ok=evidence["hourly_coverage_ok"]).as_json()
        alerts["disk_low"]=await self.hass.async_add_executor_job(self.db.get_alert,"disk_low")
        for key in keys:
            alerts["stale:"+key]=await self.hass.async_add_executor_job(self.db.get_alert,"stale:"+key)
        # One-hour history is in OUR sqlite, independent of HA Recorder retention.
        changes={}
        for key in keys:
            hist=await self.hass.async_add_executor_job(self.db.history,key,now-24*HOUR,26)
            samples_by_hour={x["hour"]:x for x in hist if x["size"] is not None and x["status"]=="ok"}
            current=samples_by_hour.get(now)
            changes[key]={}
            for delta in (6,24):
                older=samples_by_hour.get(now-delta*HOUR)
                changes[key]["delta"+str(delta)+"h"] = (
                    current["size"]-older["size"] if current and older else None)
        # Disabled means no Recorder access and no historical results presented
        # as current. Enabled dashboards read only the monitor's own summaries.
        if self.writer_diagnostics_enabled:
            window_rows = await self.hass.async_add_executor_job(
                self.db.writer_window_history,checkpoint_end-9*DAY,checkpoint_end)
            writers = summarize_writer_windows(window_rows,checkpoint_end)
            writers["enabled_at"] = self.writer_diagnostics_enabled_at
        else:
            writers = disabled_writer_summary()
        registry = er.async_get(self.hass)
        grouped = {}
        for item in writers.get("entities",[]):
            reg=registry.async_get(item["entity_id"])
            platform=reg.platform if reg else "unknown"
            item["integration"]=platform
            group=grouped.setdefault(platform,{"integration":platform,
                "rows_6h":0,"rows_24h":0 if writers["last24_complete"] else None,
                "entities_in_top20":0})
            group["rows_6h"] += int(item.get("rows_6h") or 0)
            if writers["last24_complete"]:
                group["rows_24h"] += int(item.get("rows_24h") or 0)
            group["entities_in_top20"] += 1
        writers["top20_grouped_by_integration"]=sorted(
            grouped.values(),key=lambda r:-(r["rows_24h"] if r["rows_24h"] is not None else r["rows_6h"]))
        return {"samples":items,"alerts":alerts,"growth":growth,"writers":writers,
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
