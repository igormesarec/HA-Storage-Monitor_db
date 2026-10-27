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
