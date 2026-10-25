"""Read-only local file probes. Never open monitored DBs for writing."""
from __future__ import annotations
from pathlib import Path
import os
from .engine import Sample


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
        f = os.statvfs(config_dir)
        block = f.f_frsize
        total = f.f_blocks*block
        free = f.f_bavail*block
        items.extend((Sample(hour,"disk_total",total,"ok","config_mount_statvfs",now),
                      Sample(hour,"disk_free",free,"ok","config_mount_statvfs",now),
                      Sample(hour,"disk_used",total-f.f_bfree*block,"ok","config_mount_statvfs",now)))
    except OSError as err:
        items.extend((Sample(hour,"disk_total",None,"unavailable","config_mount_statvfs",None,detail=type(err).__name__),
                      Sample(hour,"disk_free",None,"unavailable","config_mount_statvfs",None,detail=type(err).__name__),
                      Sample(hour,"disk_used",None,"unavailable","config_mount_statvfs",None,detail=type(err).__name__)))
    # Inspect only two authorized, non-recursive directories. Protected add-on
    # filesystems are *not* accessible here. Never follow symlink directories.
    # Exclude Recorder and our own snapshot database to avoid double-counting
    # or producing a feedback loop by monitoring the monitor's history.
    discovered = []
    allowed_suffixes = {".db", ".sqlite", ".sqlite3"}
    for folder in (config, config / ".storage"):
        if not folder.is_dir() or folder.is_symlink():
            continue
        try:
            # Bounded set of file names; no recursion, file content reads,
            # user-controlled arbitrary traversal, or database connections.
            candidates = sorted(p for p in folder.iterdir()
                                if p.suffix.lower() in allowed_suffixes)
        except OSError:
            continue
        for path in candidates[:64]:
            if path.name == "home-assistant_v2.db" and folder == config:
                continue
            if path.name == "ha_storage_monitor_history.sqlite3" and folder == config / ".storage":
                continue
            relative = path.relative_to(config).as_posix()
            key = "database:" + relative
            parts = [file_stat(path, key, hour, now)]
            if parts[0].status == "ok":
                for ending in ("-wal", "-shm"):
                    # WAL and SHM count toward the file's footprint when present.
                    parts.append(file_stat(Path(str(path) + ending), key, hour, now,
                                           missing_zero=True))
            if all(part.status == "ok" for part in parts):
                total_allocated = (sum(part.allocated for part in parts)
                                   if all(part.allocated is not None for part in parts) else None)
                discovered.append(Sample(hour, key, sum(part.size for part in parts),
                                         "ok", "sqlite_main_wal_shm_file_stat", now,
                                         total_allocated))
            else:
                discovered.append(Sample(hour, key, None, "unavailable",
                                         "sqlite_main_wal_shm_file_stat", None,
                                         detail="Local database file or sidecar unreadable"))
    items.extend(discovered)
    if discovered and all(s.status == "ok" for s in discovered):
        items.append(Sample(hour, "database_total", sum(s.size for s in discovered),
                            "ok", "sum_discovered_local_sqlite_files", now,
                            sum(s.allocated for s in discovered)
                            if all(s.allocated is not None for s in discovered) else None))
    else:
        items.append(Sample(hour, "database_total", None, "unavailable",
                            "sum_discovered_local_sqlite_files", None,
                            detail="No additional accessible SQLite files or one is unreadable"))
    return items
