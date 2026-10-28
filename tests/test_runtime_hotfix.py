"""HA runtime regressions using the real positional-only executor-call shape."""
from __future__ import annotations

import ast
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "ha_storage_monitor"
GIB = 1024**3
MIB = 1024**2


def _load_runtime(monkeypatch):
    """Load the integration with small HA API stubs, not a permissive mock."""
    for name in tuple(sys.modules):
        if name == "ha_storage_monitor_runtime" or name.startswith(
                "ha_storage_monitor_runtime."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    websocket_api = types.ModuleType("homeassistant.components.websocket_api")
    websocket_api.async_register_command = lambda hass, command: None
    websocket_api.require_admin = lambda func: func
    websocket_api.websocket_command = lambda schema: lambda func: func
    websocket_api.async_response = lambda func: func
    http = types.ModuleType("homeassistant.components.http")

    class StaticPathConfig:
        def __init__(self, *args):
            self.args = args

    http.StaticPathConfig = StaticPathConfig
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda func: func
    helpers = types.ModuleType("homeassistant.helpers")
    aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client.async_get_clientsession = lambda hass: object()
    event = types.ModuleType("homeassistant.helpers.event")
    event.async_track_time_change = lambda *args, **kwargs: (lambda: None)
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = lambda hass: types.SimpleNamespace(
        async_get=lambda entity_id: None)
    helpers.entity_registry = entity_registry

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.websocket_api": websocket_api,
        "homeassistant.components.http": http,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.aiohttp_client": aiohttp_client,
        "homeassistant.helpers.event": event,
        "homeassistant.helpers.entity_registry": entity_registry,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Required = lambda key, **kwargs: key
    voluptuous.Optional = lambda key, **kwargs: key
    voluptuous.In = lambda values: values
    monkeypatch.setitem(sys.modules, "voluptuous", voluptuous)

    name = "ha_storage_monitor_runtime"
    spec = importlib.util.spec_from_file_location(
        name, INTEGRATION / "__init__.py",
        submodule_search_locations=[str(INTEGRATION)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StrictHass:
    """Matches HA Core: executor accepts a callable and positional args only."""

    def __init__(self, path, *, notification_failure=False):
        self.config = types.SimpleNamespace(
            config_dir=str(path), path=lambda *parts: str(path.joinpath(*parts)))
        self.data = {}
        self.executor_calls = []
        self.notification_calls = []
        self.notification_failure = notification_failure
        self.services = types.SimpleNamespace(async_call=self._service_call)

    async def async_add_executor_job(self, target, *args):
        self.executor_calls.append((target, args))
        return target(*args)

    async def _service_call(self, domain, service, data, *, blocking):
        self.notification_calls.append((domain, service, data, blocking))
        if self.notification_failure:
            raise RuntimeError("simulated notification service failure")


class Entry:
    def __init__(self, *, options=None):
        self.data = {}
        self.options = options or {}
        self.entry_id = "storage-monitor-test"
        self.tasks = []

    def async_create_background_task(self, hass, coro, name):
        self.tasks.append(coro)
        return coro


def _run(coro):
    """Drive immediate offline coroutines without requiring a Windows socket loop."""
    try:
        yielded = coro.send(None)
    except StopIteration as finished:
        return finished.value
    coro.close()
    raise AssertionError(f"offline coroutine unexpectedly blocked on {yielded!r}")


def _samples(runtime, hour, *, free=20 * GIB, recorder=2 * GIB,
             recorder_status="ok"):
    measured = hour if recorder_status == "ok" else None
    recorder_size = recorder if recorder_status == "ok" else None
    return [
        runtime.Sample(hour, "recorder_total", recorder_size,
                       recorder_status, "file_stat", measured),
        runtime.Sample(hour, "disk_total", 40 * GIB, "ok", "disk_usage", hour),
        runtime.Sample(hour, "disk_free", free, "ok", "disk_usage", hour),
    ]


def _unavailable_influx(runtime):
    async def get_usage(session, slug):
        return types.SimpleNamespace(
            size=None, status="unavailable",
            detail="HA Core Supervisor API unavailable")
    return get_usage


def test_no_executor_call_passes_keywords_directly():
    tree = ast.parse((INTEGRATION / "__init__.py").read_text(encoding="utf-8"))
    executor_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute)
                      and node.func.attr == "async_add_executor_job"]
    assert executor_calls
    assert all(not node.keywords for node in executor_calls)


def test_startup_writer_off_normal_disk_unavailable_influx_and_summary(
        tmp_path, monkeypatch):
    runtime = _load_runtime(monkeypatch)
    end = datetime(2026, 11, 10, 18, tzinfo=runtime.TZ)
    now = int(end.timestamp()) // runtime.HOUR * runtime.HOUR
    monkeypatch.setattr(runtime.time, "time", lambda: now)
    monkeypatch.setattr(runtime, "collect_local",
                        lambda config, hour, measured: _samples(runtime, hour))
    monkeypatch.setattr(runtime, "get_influx_usage", _unavailable_influx(runtime))
    monkeypatch.setattr(
        runtime, "collect_writer_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Recorder must not be opened while disabled")))
    hass = StrictHass(tmp_path)
    entry = Entry()
    monitor = runtime.StorageMonitor(hass, entry)

    async def scenario():
        await monitor.start()
        for task in tuple(entry.tasks):
            await task
        summary = await monitor.summary()
        connection = types.SimpleNamespace(
            results=[], errors=[],
            send_result=lambda ident, result: connection.results.append((ident, result)),
            send_error=lambda *args: connection.errors.append(args))
        hass.data[runtime.DOMAIN] = {entry.entry_id: monitor}
        await runtime.ws_summary(hass, connection, {"id": 7})
        return summary, connection

    summary, connection = _run(scenario())
    latest = {row["source"]: row for row in monitor.db.latest()}
    assert monitor.writer_diagnostics_enabled is False
    assert latest["influx_addon"]["status"] == "unavailable"
    assert monitor.db.get_alert("disk_low")["active"] == 0
    assert not hass.notification_calls
    assert summary["writers"]["reason"] == "Recorder writer diagnostics disabled"
    assert summary["effective_options"] == {
        "low_disk_gib": 15,
        "min_excess_mib": 0,
        "retention_days": 365,
        "repeat_notifications": False,
        "enable_recorder_writer_diagnostics": False,
    }
    assert connection.results[0][0] == 7
    assert connection.results[0][1]["writer_diagnostics_enabled"] is False
    assert not connection.errors


def test_disk_low_cooldown_persists_before_failed_notification(
        tmp_path, monkeypatch):
    runtime = _load_runtime(monkeypatch)
    end = datetime(2026, 11, 10, 18, tzinfo=runtime.TZ)
    now = int(end.timestamp()) // runtime.HOUR * runtime.HOUR
    monkeypatch.setattr(runtime.time, "time", lambda: now)
    monkeypatch.setattr(runtime, "collect_local",
                        lambda config, hour, measured:
                        _samples(runtime, hour, free=1 * GIB))
    monkeypatch.setattr(runtime, "get_influx_usage", _unavailable_influx(runtime))
    hass = StrictHass(tmp_path, notification_failure=True)
    entry = Entry()
    monitor = runtime.StorageMonitor(hass, entry)

    async def scenario():
        await monitor.start()
        for task in tuple(entry.tasks):
            await task
        await monitor.sample(False)

    _run(scenario())
    alert = monitor.db.get_alert("disk_low")
    assert alert["active"] == 1
    assert alert["last_window"] is not None
    assert alert["last_notify_at"] == now
    assert len(hass.notification_calls) == 1


def _populate_growth(runtime, monitor, now):
    rows = []
    for index in range(8 * 24 + 1):
        hour = now - 8 * runtime.DAY + index * runtime.HOUR
        day = index / 24
        if day <= 7:
            size = 1000 * MIB + int(day * 200 * MIB)
        else:
            size = 2400 * MIB + int((day - 7) * 211 * MIB)
        rows.append(runtime.Sample(
            hour, "recorder_total", size, "ok", "file_stat", hour))
    monitor.db.save(rows)


def test_six_hour_growth_alert_and_websocket_summary(tmp_path, monkeypatch):
    runtime = _load_runtime(monkeypatch)
    end = datetime(2026, 11, 10, 18, tzinfo=runtime.TZ)
    now = int(end.timestamp()) // runtime.HOUR * runtime.HOUR
    monkeypatch.setattr(runtime.time, "time", lambda: now)
    monkeypatch.setattr(runtime, "collect_local",
                        lambda config, hour, measured: _samples(
                            runtime, hour, recorder=2611 * MIB))
    monkeypatch.setattr(runtime, "get_influx_usage", _unavailable_influx(runtime))
    monkeypatch.setattr(
        runtime, "collect_writer_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("six-hour size checks must not enable Recorder diagnostics")))
    hass = StrictHass(tmp_path)
    entry = Entry()
    monitor = runtime.StorageMonitor(hass, entry)
    monitor.db.initialize()
    monitor.db.configure_writer_diagnostics(False, now)
    _populate_growth(runtime, monitor, now)

    async def scenario():
        await monitor.sample(True)
        await monitor.sample(True)
        connection = types.SimpleNamespace(
            result=None, send_result=lambda ident, result: setattr(
                connection, "result", result),
            send_error=lambda *args: (_ for _ in ()).throw(AssertionError(args)))
        hass.data[runtime.DOMAIN] = {entry.entry_id: monitor}
        await runtime.ws_summary(hass, connection, {"id": 9})
        return connection.result

    summary = _run(scenario())
    growth_notifications = [call for call in hass.notification_calls
                            if call[2]["notification_id"] ==
                            "ha_storage_monitor_growth_recorder_total"]
    assert len(growth_notifications) == 1
    assert monitor.db.get_alert("recorder_total")["active"] == 1
    assert summary["growth"]["recorder_total"]["status"] == "alarm"
    assert summary["writers"]["status"] == "disabled"


def test_two_hour_stale_path_updates_persistent_alert(tmp_path, monkeypatch):
    runtime = _load_runtime(monkeypatch)
    end = datetime(2026, 11, 10, 18, tzinfo=runtime.TZ)
    now = int(end.timestamp()) // runtime.HOUR * runtime.HOUR
    monkeypatch.setattr(runtime.time, "time", lambda: now)
    monkeypatch.setattr(runtime, "collect_local",
                        lambda config, hour, measured: _samples(
                            runtime, hour, recorder_status="unavailable"))
    monkeypatch.setattr(runtime, "get_influx_usage", _unavailable_influx(runtime))
    hass = StrictHass(tmp_path)
    entry = Entry()
    monitor = runtime.StorageMonitor(hass, entry)
    monitor.db.initialize()
    monitor.db.save([runtime.Sample(
        now - runtime.HOUR, "recorder_total", None, "unavailable",
        "file_stat", None)])

    async def scenario():
        await monitor.sample(False)
        await monitor.sample(False)

    _run(scenario())
    stale = monitor.db.get_alert("stale:recorder_total")
    notifications = [call for call in hass.notification_calls
                     if call[2]["notification_id"] ==
                     "ha_storage_monitor_stale_recorder_total"]
    assert stale["active"] == 1
    assert stale["last_notify_at"] == now
    assert len(notifications) == 1


def test_influx_startup_checkpoint_restart_failure_and_dst_scheduling(
        tmp_path, monkeypatch):
    runtime = _load_runtime(monkeypatch)
    clock = [int(datetime(2026, 11, 10, 18, 34,
                          tzinfo=runtime.TZ).timestamp())]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    monkeypatch.setattr(runtime, "collect_local",
                        lambda config, hour, measured: _samples(runtime, hour))
    calls = []

    async def successful(session, slug):
        calls.append(clock[0])
        return types.SimpleNamespace(
            size=7 * GIB + len(calls), status="ok", detail="verified")

    monkeypatch.setattr(runtime, "get_influx_usage", successful)
    path = tmp_path / "restart"
    path.mkdir()
    first = runtime.StorageMonitor(StrictHass(path), Entry())
    first.db.initialize()
    _run(first.sample(False, startup=True))
    first_result = {x["source"]: x for x in first.db.latest()}["influx_addon"]
    assert first_result["status"] == "ok"
    assert first_result["measured_at"] == calls[0]

    # A new runtime instance uses the same monitor SQLite and cannot repeat the
    # expensive scan after a quick restart.
    clock[0] = int(datetime(2026, 11, 10, 19, 0,
                            tzinfo=runtime.TZ).timestamp())
    restarted = runtime.StorageMonitor(StrictHass(path), Entry())
    _run(restarted.sample(False, startup=True))
    assert len(calls) == 1
    cached = {x["source"]: x for x in restarted.db.latest()}["influx_addon"]
    assert cached["status"] == "cached"
    assert cached["measured_at"] == first_result["measured_at"]

    # Midnight is a requested local checkpoint, but only 5h26 has elapsed
    # since the 18:34 attempt. Safety wins; the genuine timestamp is retained.
    clock[0] = int(datetime(2026, 11, 11, 0, 0,
                            tzinfo=runtime.TZ).timestamp())
    _run(restarted.sample(True))
    assert len(calls) == 1
    midnight = {x["source"]: x for x in restarted.db.latest()}["influx_addon"]
    assert midnight["status"] == "cached"
    assert midnight["measured_at"] == first_result["measured_at"]
    assert "Waiting for next eligible Supervisor probe" in midnight["detail"]

    # By 06:00 more than six elapsed hours have passed, so a fresh probe runs.
    clock[0] = int(datetime(2026, 11, 11, 6, 0,
                            tzinfo=runtime.TZ).timestamp())
    _run(restarted.sample(True))
    assert len(calls) == 2
    six = {x["source"]: x for x in restarted.db.latest()}["influx_addon"]
    assert six["status"] == "ok" and six["measured_at"] == clock[0]

    # Failed attempts are reserved too, preventing restart retry storms.
    failed_path = tmp_path / "failed"
    failed_path.mkdir()
    failure_calls = []

    async def failed(session, slug):
        failure_calls.append(clock[0])
        return types.SimpleNamespace(size=None, status="unavailable",
                                     detail="Supervisor denied access")

    monkeypatch.setattr(runtime, "get_influx_usage", failed)
    clock[0] = int(datetime(2026, 12, 1, 0, 0,
                            tzinfo=runtime.TZ).timestamp())
    failed_monitor = runtime.StorageMonitor(StrictHass(failed_path), Entry())
    failed_monitor.db.initialize()
    _run(failed_monitor.sample(True))
    clock[0] += 60
    failed_restart = runtime.StorageMonitor(StrictHass(failed_path), Entry())
    _run(failed_restart.sample(False, startup=True))
    assert len(failure_calls) == 1
    unavailable = {x["source"]: x for x in failed_restart.db.latest()}["influx_addon"]
    assert unavailable["status"] == "unavailable"
    assert unavailable["size"] is None

    # Local checkpoint intervals are five elapsed hours in spring and seven in
    # autumn. The strict six-hour rule skips the former and permits the latter.
    for folder, start, end, expected in [
        ("spring", datetime(2026, 3, 29, 0, tzinfo=runtime.TZ),
         datetime(2026, 3, 29, 6, tzinfo=runtime.TZ), 1),
        ("autumn", datetime(2026, 10, 25, 0, tzinfo=runtime.TZ),
         datetime(2026, 10, 25, 6, tzinfo=runtime.TZ), 2),
    ]:
        dst_path = tmp_path / folder
        dst_path.mkdir()
        dst_calls = []

        async def dst_success(session, slug):
            dst_calls.append(clock[0])
            return types.SimpleNamespace(size=GIB, status="ok", detail="verified")

        monkeypatch.setattr(runtime, "get_influx_usage", dst_success)
        dst_monitor = runtime.StorageMonitor(StrictHass(dst_path), Entry())
        dst_monitor.db.initialize()
        clock[0] = int(start.timestamp())
        _run(dst_monitor.sample(True))
        clock[0] = int(end.timestamp())
        _run(dst_monitor.sample(True))
        assert len(dst_calls) == expected
        assert dst_monitor.db.get_alert("stale:influx_addon")["active"] == 0


def test_v02_upgrade_startup_reuses_recent_beta3_influx_snapshot(
        tmp_path, monkeypatch):
    runtime = _load_runtime(monkeypatch)
    now = int(datetime(2026, 11, 10, 18, 34,
                       tzinfo=runtime.TZ).timestamp())
    measured = now - 2 * runtime.HOUR
    monkeypatch.setattr(runtime.time, "time", lambda: now)
    monkeypatch.setattr(runtime, "collect_local",
                        lambda config, hour, actual: _samples(runtime, hour))
    probes = []

    async def forbidden_extra_probe(session, slug):
        probes.append(now)
        return types.SimpleNamespace(size=8 * GIB, status="ok", detail="new")

    monkeypatch.setattr(runtime, "get_influx_usage", forbidden_extra_probe)
    monitor = runtime.StorageMonitor(StrictHass(tmp_path), Entry())
    monitor.db.initialize()
    monitor.db.save([runtime.Sample(
        runtime.utc_hour(measured), "influx_addon", 7 * GIB, "ok",
        "supervisor_addons_data_total", measured,
        detail="genuine beta.3 probe")])

    async def scenario():
        await monitor.start()
        for task in tuple(monitor.entry.tasks):
            await task

    _run(scenario())
    assert probes == []
    row = {item["source"]: item for item in monitor.db.latest()}["influx_addon"]
    assert row["status"] == "cached"
    assert row["size"] == 7 * GIB
    assert row["measured_at"] == measured
    state = monitor.db.influx_probe_state()
    assert state["last_attempt_at"] == measured
    assert state["last_success"]["measured_at"] == measured


def test_influx_expected_deferral_aging_and_next_success_do_not_alert(
        tmp_path, monkeypatch):
    runtime = _load_runtime(monkeypatch)
    clock = [int(datetime(2026, 10, 27, 18, 34, 50,
                          tzinfo=runtime.TZ).timestamp())]
    monkeypatch.setattr(runtime.time, "time", lambda: clock[0])
    monkeypatch.setattr(runtime, "collect_local",
                        lambda config, hour, actual: _samples(runtime, hour))
    probes = []

    async def successful(session, slug):
        probes.append(clock[0])
        return types.SimpleNamespace(size=7 * GIB + len(probes),
                                     status="ok",detail="verified")

    monkeypatch.setattr(runtime, "get_influx_usage", successful)
    hass=StrictHass(tmp_path);monitor=runtime.StorageMonitor(hass,Entry())
    monitor.db.initialize()
    _run(monitor.sample(False,startup=True))
    measured=clock[0]
    clock[0]=int(datetime(2026,10,28,0,0,tzinfo=runtime.TZ).timestamp())
    _run(monitor.sample(True))
    assert len(probes)==1
    for local_hour in (1,2,3,4,5):
        clock[0]=int(datetime(2026,10,28,local_hour,0,
                              tzinfo=runtime.TZ).timestamp())
        _run(monitor.sample(False))
    latest={row["source"]:row for row in monitor.db.latest()}["influx_addon"]
    assert latest["status"]=="stale"
    assert latest["measured_at"]==measured
    assert "Waiting for next eligible Supervisor probe" in latest["detail"]
    assert monitor.db.get_alert("stale:influx_addon")["active"]==0
    assert not [call for call in hass.notification_calls
                if call[1]=="create" and call[2]["notification_id"]==
                "ha_storage_monitor_stale_influx_addon"]
    clock[0]=int(datetime(2026,10,28,6,0,tzinfo=runtime.TZ).timestamp())
    _run(monitor.sample(True))
    latest={row["source"]:row for row in monitor.db.latest()}["influx_addon"]
    assert len(probes)==2
    assert latest["status"]=="ok" and latest["measured_at"]==clock[0]
    assert monitor.db.get_alert("stale:influx_addon")["active"]==0


def test_influx_failed_opportunities_alert_once_survive_restart_and_recover(
        tmp_path, monkeypatch):
    runtime = _load_runtime(monkeypatch)
    clock=[int(datetime(2026,10,27,18,34,50,
                        tzinfo=runtime.TZ).timestamp())]
    monkeypatch.setattr(runtime.time,"time",lambda:clock[0])
    monkeypatch.setattr(runtime,"collect_local",
                        lambda config,hour,actual:_samples(runtime,hour))
    mode=["success"]
    probes=[]

    async def provider(session,slug):
        probes.append((clock[0],mode[0]))
        if mode[0]=="raise":
            raise RuntimeError("simulated timeout")
        if mode[0]=="failure":
            return types.SimpleNamespace(size=None,status="unavailable",
                                         detail="Supervisor HTTP 403")
        return types.SimpleNamespace(size=7*GIB,status="ok",detail="verified")

    monkeypatch.setattr(runtime,"get_influx_usage",provider)
    hass=StrictHass(tmp_path);monitor=runtime.StorageMonitor(hass,Entry())
    monitor.db.initialize();_run(monitor.sample(False,startup=True))
    verified_at=clock[0]
    mode[0]="failure"
    clock[0]=int(datetime(2026,10,28,6,0,tzinfo=runtime.TZ).timestamp())
    _run(monitor.sample(True))
    assert monitor.db.influx_probe_state()["consecutive_failures"]==1
    assert monitor.db.get_alert("stale:influx_addon")["active"]==0
    mode[0]="raise"
    clock[0]=int(datetime(2026,10,28,12,0,tzinfo=runtime.TZ).timestamp())
    _run(monitor.sample(True))
    state=monitor.db.influx_probe_state()
    assert state["consecutive_failures"]==2
    assert state["last_attempt_outcome"]["status"]=="failure"
    alert=monitor.db.get_alert("stale:influx_addon")
    assert alert["active"]==1 and alert["last_notify_at"]==runtime.utc_hour(clock[0])
    creates=[call for call in hass.notification_calls if call[1]=="create"
             and call[2]["notification_id"]=="ha_storage_monitor_stale_influx_addon"]
    assert len(creates)==1
    assert creates[0][2]["title"]=="InfluxDB add-on size measurement unavailable"
    assert "not whether the InfluxDB service is running" in creates[0][2]["message"]
    assert "Last successful measurement" in creates[0][2]["message"]
    assert "RuntimeError" in creates[0][2]["message"]

    # A restart inside the six-hour cooldown neither scans nor notifies again.
    clock[0]+=60
    restarted=runtime.StorageMonitor(hass,Entry())
    _run(restarted.sample(False,startup=True))
    assert len(probes)==3
    assert len([call for call in hass.notification_calls if call[1]=="create"
                and call[2]["notification_id"]==
                "ha_storage_monitor_stale_influx_addon"])==1
    historical={row["source"]:row for row in restarted.db.latest()}["influx_addon"]
    assert historical["size"]==7*GIB
    assert historical["measured_at"]==verified_at
    assert historical["status"]=="stale"

    # A third failure keeps the alert durable without notification flooding.
    mode[0]="failure"
    clock[0]=int(datetime(2026,10,28,18,0,tzinfo=runtime.TZ).timestamp())
    _run(restarted.sample(True))
    assert restarted.db.influx_probe_state()["consecutive_failures"]==3
    assert restarted.db.get_alert("stale:influx_addon")["active"]==1
    assert len([call for call in hass.notification_calls if call[1]=="create"
                and call[2]["notification_id"]==
                "ha_storage_monitor_stale_influx_addon"])==1
    health=_run(restarted.summary())["influx_health"]
    assert health["status"]=="prolonged_unavailable"
    assert health["alert_active"] is True
    assert health["last_success_at"]==verified_at
    assert health["last_attempt_status"]=="failure"
    assert health["consecutive_failures"]==3
    assert health["next_eligible_at"]==int(datetime(
        2026,10,29,0,0,tzinfo=runtime.TZ).timestamp())

    # A later genuine success resets health, clears the durable alert and
    # dismisses only Storage Monitor's own persistent notification.
    mode[0]="success"
    clock[0]=int(datetime(2026,10,29,0,0,tzinfo=runtime.TZ).timestamp())
    _run(restarted.sample(True))
    assert restarted.db.influx_probe_state()["consecutive_failures"]==0
    assert restarted.db.get_alert("stale:influx_addon")["active"]==0
    dismisses=[call for call in hass.notification_calls if call[1]=="dismiss"]
    assert len(dismisses)==1
    assert dismisses[0][2]["notification_id"]=="ha_storage_monitor_stale_influx_addon"


def test_influx_interrupted_attempts_alert_with_cooldown_and_recover(
        tmp_path, monkeypatch):
    runtime=_load_runtime(monkeypatch)
    clock=[int(datetime(2026,10,27,18,0,tzinfo=runtime.TZ).timestamp())]
    monkeypatch.setattr(runtime.time,"time",lambda:clock[0])
    monkeypatch.setattr(runtime,"collect_local",
                        lambda config,hour,actual:_samples(runtime,hour))
    probes=[]

    async def successful(session,slug):
        probes.append(clock[0])
        return types.SimpleNamespace(size=8*GIB,status="ok",detail="recovered")

    monkeypatch.setattr(runtime,"get_influx_usage",successful)
    hass=StrictHass(tmp_path);monitor=runtime.StorageMonitor(hass,Entry())
    monitor.db.initialize()
    genuine=runtime.Sample(runtime.utc_hour(clock[0]),"influx_addon",7*GIB,
                           "ok","supervisor_addons_data_total",clock[0],
                           detail="previous genuine measurement")
    assert monitor.db.reserve_influx_probe(clock[0])
    monitor.db.save_influx_result(clock[0],genuine)
    genuine_at=clock[0]

    # Simulate a crash after the midnight reservation but before a result.
    first=clock[0]+6*runtime.HOUR
    assert monitor.db.reserve_influx_probe(first)
    clock[0]=first+60
    restarted=runtime.StorageMonitor(hass,Entry())
    _run(restarted.sample(False,startup=True))
    state=restarted.db.influx_probe_state(clock[0])
    assert probes==[]
    assert state["last_attempt_outcome"]["status"]=="in_progress"
    assert state["consecutive_failures"]==0
    assert restarted.db.get_alert("stale:influx_addon")["active"]==0

    clock[0]=first+120
    _run(restarted.sample(False))
    state=restarted.db.influx_probe_state(clock[0])
    assert state["last_attempt_outcome"]["status"]=="interrupted"
    assert state["consecutive_failures"]==1
    historical={row["source"]:row for row in restarted.db.latest()}["influx_addon"]
    assert historical["size"]==7*GIB
    assert historical["measured_at"]==genuine_at
    assert "interrupted" in historical["detail"]

    # A second interrupted eligible attempt must activate the same durable
    # alert used by HTTP failures, with no extra Supervisor request.
    second=first+6*runtime.HOUR
    assert restarted.db.reserve_influx_probe(second)
    clock[0]=second+120
    _run(restarted.sample(False))
    state=restarted.db.influx_probe_state(clock[0])
    assert state["last_attempt_outcome"]["status"]=="interrupted"
    assert state["consecutive_failures"]==2
    assert probes==[]
    alert=restarted.db.get_alert("stale:influx_addon")
    assert alert["active"]==1
    creates=[call for call in hass.notification_calls if call[1]=="create"
             and call[2]["notification_id"]==
             "ha_storage_monitor_stale_influx_addon"]
    assert len(creates)==1
    assert "interrupted" in creates[0][2]["message"]

    # Further hourly checks are inside the durable cooldown and neither scan
    # nor send another notification.
    clock[0]=second+runtime.HOUR
    _run(restarted.sample(False))
    assert probes==[]
    assert len([call for call in hass.notification_calls if call[1]=="create"
                and call[2]["notification_id"]==
                "ha_storage_monitor_stale_influx_addon"])==1

    # The next eligible real success clears the alert and dismisses only this
    # integration's own notification.
    clock[0]=second+6*runtime.HOUR
    _run(restarted.sample(True))
    recovered=restarted.db.influx_probe_state(clock[0])
    assert probes==[clock[0]]
    assert recovered["consecutive_failures"]==0
    assert recovered["last_attempt_outcome"]["status"]=="success"
    assert restarted.db.get_alert("stale:influx_addon")["active"]==0
    dismisses=[call for call in hass.notification_calls if call[1]=="dismiss"]
    assert len(dismisses)==1
    assert dismisses[0][2]["notification_id"]=="ha_storage_monitor_stale_influx_addon"


def test_influx_overdue_policy_and_dst_checkpoint_selection(monkeypatch):
    runtime=_load_runtime(monkeypatch)
    success={"size":7*GIB,"measured_at":0,
             "method":"supervisor_addons_data_total","detail":"verified"}
    spring_attempt=int(datetime(2026,3,29,0,0,tzinfo=runtime.TZ).timestamp())
    spring_next=runtime.next_influx_probe_opportunity(
        spring_attempt,spring_attempt)
    assert spring_next==int(datetime(2026,3,29,12,0,
                                     tzinfo=runtime.TZ).timestamp())
    autumn_attempt=int(datetime(2026,10,25,0,0,tzinfo=runtime.TZ).timestamp())
    autumn_next=runtime.next_influx_probe_opportunity(
        autumn_attempt,autumn_attempt)
    assert autumn_next==int(datetime(2026,10,25,6,0,
                                     tzinfo=runtime.TZ).timestamp())
    # An interrupted outcome does not alter elapsed-time checkpoint selection
    # on either DST transition.
    interrupted={"attempted_at":spring_attempt,"status":"interrupted",
                 "detail":"probe interrupted","detected_at":spring_attempt+120}
    interrupted_state={"last_attempt_at":spring_attempt,
                       "last_success":{**success,"measured_at":spring_attempt},
                       "last_attempt_outcome":interrupted,
                       "last_interruption":interrupted,
                       "consecutive_failures":1}
    spring_health=runtime.influx_measurement_health(
        interrupted_state,{"status":"stale"},spring_attempt+120)
    assert spring_health["status"]=="probe_interrupted"
    assert spring_health["should_alert"] is False
    assert spring_health["next_eligible_at"]==int(datetime(
        2026,3,29,12,0,tzinfo=runtime.TZ).timestamp())
    state={"last_attempt_at":autumn_attempt,
           "last_success":{**success,"measured_at":autumn_attempt},
           "last_attempt_outcome":{"attempted_at":autumn_attempt,
                                   "status":"success","detail":"verified"},
           "consecutive_failures":0}
    waiting=runtime.influx_measurement_health(
        state,{"status":"stale"},autumn_next+runtime.HOUR-1)
    assert waiting["should_alert"] is False
    assert waiting["status"]=="stale_waiting"
    overdue=runtime.influx_measurement_health(
        state,{"status":"stale"},autumn_next+runtime.HOUR)
    assert overdue["should_alert"] is True
    assert overdue["status"]=="prolonged_unavailable"
