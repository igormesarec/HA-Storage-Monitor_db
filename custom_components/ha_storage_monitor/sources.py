"""Read-only local file probes. Never open monitored DBs for writing."""
from __future__ import annotations
from pathlib import Path
import shutil
from .engine import Sample


HOME_ENERGY_INTELLIGENCE_RELATIVE = Path(
    ".storage/home_energy_intelligence/home_energy_intelligence.sqlite3"
)
HOME_ENERGY_INTELLIGENCE_SOURCE = (
    "database:" + HOME_ENERGY_INTELLIGENCE_RELATIVE.as_posix()
)


def file_stat(path: Path, source: str, hour: int, now: int, *, missing_zero=False):
    try:
        # Refuse symlinks; no recursive traversal, no file content read.
        if path.is_symlink():
            raise PermissionError("symlink not allowed")
        st = path.stat()
        if not path.is_file():
            raise ValueError("not a regular file")
        return Sample(hour, source, st.st_size, "ok", "file_stat_apparent",now,
                      getattr(st,"st_blocks",0)*512 if hasattr(st,"st_blocks") else None)
    except FileNotFoundError:
        if missing_zero:
            # SQLite checkpointing can legitimately remove WAL/SHM sidecars.
            return Sample(hour,source,0,"ok","file_absent_verified",now,0)
        return Sample(hour,source,None,"unavailable","file_stat",None,detail="file missing")
    except (OSError,ValueError) as err:
        return Sample(hour,source,None,"unavailable","file_stat",None,detail=type(err).__name__)


def sqlite_footprint(path: Path, source: str, hour: int, now: int) -> Sample:
    """Measure a SQLite main file and existing sidecars using metadata only."""
    parts = [file_stat(path, source, hour, now)]
    if parts[0].status == "ok":
        for ending in ("-wal", "-shm"):
            parts.append(file_stat(Path(str(path) + ending), source, hour, now,
                                   missing_zero=True))
    if all(part.status == "ok" for part in parts):
        total_allocated = (sum(part.allocated for part in parts)
                           if all(part.allocated is not None for part in parts)
                           else None)
        return Sample(hour, source, sum(part.size for part in parts), "ok",
                      "sqlite_main_wal_shm_file_stat", now, total_allocated)
    return Sample(hour, source, None, "unavailable",
                  "sqlite_main_wal_shm_file_stat", None,
                  detail="Local database file or sidecar unreadable")


def authorized_home_energy_intelligence(config: Path, hour: int,
                                        now: int) -> Sample:
    """Measure only the explicitly authorized HEI path, never a subtree scan."""
    path = config / HOME_ENERGY_INTELLIGENCE_RELATIVE
    # The exact path is authorized, but no symlink in its fixed path may widen
    # that authorization. Missing components fall through to an unavailable
    # main-file result; no directory is traversed or database opened.
    components = (
        config,
        config / ".storage",
        config / ".storage" / "home_energy_intelligence",
        path,
    )
    try:
        if any(component.is_symlink() for component in components):
            raise PermissionError("symlink not allowed in authorized path")
    except OSError as err:
        return Sample(hour, HOME_ENERGY_INTELLIGENCE_SOURCE, None,
                      "unavailable", "sqlite_main_wal_shm_file_stat", None,
                      detail=type(err).__name__)
    return sqlite_footprint(path, HOME_ENERGY_INTELLIGENCE_SOURCE, hour, now)


def collect_local(config_dir: str, hour: int, now: int) -> list[Sample]:
    config = Path(config_dir)
    main = file_stat(config/"home-assistant_v2.db","recorder_main",hour,now)
    items = [main]
    for suffix, key in (("-wal","recorder_wal"),("-shm","recorder_shm")):
        items.append(file_stat(config/("home-assistant_v2.db"+suffix),key,hour,now,
                               missing_zero=main.status=="ok"))
    if all(s.status=="ok" for s in items):
        items.append(Sample(hour,"recorder_total",sum(s.size for s in items),"ok",
                            "sum_apparent_file_stat",now,
                            sum(s.allocated for s in items) if all(s.allocated is not None for s in items) else None))
    else:
        items.append(Sample(hour,"recorder_total",None,"unavailable","sum_apparent_file_stat",None,
                            detail="component unavailable"))
    try:
        # The /config mount may be the HA OS data disk, but scope is explicit.
        usage = shutil.disk_usage(config_dir)
        total = usage.total
        free = usage.free
        items.extend((Sample(hour,"disk_total",total,"ok","config_path_disk_usage",now),
                      Sample(hour,"disk_free",free,"ok","config_path_disk_usage",now),
                      Sample(hour,"disk_used",usage.used,"ok","config_path_disk_usage",now)))
    except OSError as err:
        items.extend((Sample(hour,"disk_total",None,"unavailable","config_path_disk_usage",None,detail=type(err).__name__),
                      Sample(hour,"disk_free",None,"unavailable","config_path_disk_usage",None,detail=type(err).__name__),
                      Sample(hour,"disk_used",None,"unavailable","config_path_disk_usage",None,detail=type(err).__name__)))
    # This one nested database is an exact, separately authorized path. Keep it
    # out of database_total, whose label and semantics are "other local SQLite".
    # The source still uses the database:* pipeline for history and alerts.
    items.append(authorized_home_energy_intelligence(config, hour, now))

    # Inspect only two authorized, non-recursive directories. Protected add-on
    # filesystems are *not* accessible here. Never follow symlink directories.
    # Exclude Recorder and our own snapshot database to avoid double-counting
    # or producing a feedback loop by monitoring the monitor's history.
    discovered = []
    scan_incomplete = False
    allowed_suffixes = {".db", ".sqlite", ".sqlite3"}
    for folder in (config, config / ".storage"):
        if folder.is_symlink():
            scan_incomplete = True
            continue
        if not folder.is_dir():
            # The optional .storage folder may not exist on a new HA instance.
            if folder == config:
                scan_incomplete = True
            continue
        try:
            # Bounded set of file names; no recursion, file content reads,
            # user-controlled arbitrary traversal, or database connections.
            candidates = sorted(p for p in folder.iterdir()
                                if p.suffix.lower() in allowed_suffixes)
        except OSError:
            scan_incomplete = True
            continue
        if len(candidates) > 64:
            # A truncated total is not an exact measurement.
            scan_incomplete = True
        for path in candidates[:64]:
            if path.name == "home-assistant_v2.db" and folder == config:
                continue
            if path.name == "ha_storage_monitor_history.sqlite3" and folder == config / ".storage":
                continue
            relative = path.relative_to(config).as_posix()
            key = "database:" + relative
            discovered.append(sqlite_footprint(path, key, hour, now))
    items.extend(discovered)
    if not scan_incomplete and all(s.status == "ok" for s in discovered):
        items.append(Sample(hour, "database_total", sum(s.size for s in discovered),
                            "ok", "sum_discovered_local_sqlite_files", now,
                            sum(s.allocated for s in discovered)
                            if all(s.allocated is not None for s in discovered) else None))
    else:
        items.append(Sample(hour, "database_total", None, "unavailable",
                            "sum_discovered_local_sqlite_files", None,
                            detail="Directory listing incomplete, >64 candidates, or a database is unreadable"))
    return items
