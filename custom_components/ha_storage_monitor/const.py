"""Constants for the independent Home Assistant Storage Monitor."""
DOMAIN = "ha_storage_monitor"
DEFAULT_INFLUX_SLUG = "a0d7b954_influxdb"
DEFAULT_DISK_LOW_GIB = 15
DEFAULT_RETENTION_DAYS = 365
DEFAULT_MIN_EXCESS_MIB = 0
DEFAULT_NOTIFY_PERSISTENT = True
DEFAULT_ENABLE_RECORDER_WRITER_DIAGNOSTICS = False
HOUR = 3600
DAY = 24 * HOUR
ALARM_HOURS = (0, 6, 12, 18)
SOURCE_RECORDER_TOTAL = "recorder_total"
SOURCE_RECORDER_MAIN = "recorder_main"
SOURCE_RECORDER_WAL = "recorder_wal"
SOURCE_RECORDER_SHM = "recorder_shm"
SOURCE_DISK_FREE = "disk_free"
SOURCE_DISK_TOTAL = "disk_total"
SOURCE_INFLUX = "influx_addon"
GROWTH_SOURCES = {SOURCE_RECORDER_TOTAL, SOURCE_INFLUX}

DEFAULT_OPTIONS = {
    "low_disk_gib": DEFAULT_DISK_LOW_GIB,
    "min_excess_mib": DEFAULT_MIN_EXCESS_MIB,
    "retention_days": DEFAULT_RETENTION_DAYS,
    "repeat_notifications": False,
    "enable_recorder_writer_diagnostics": DEFAULT_ENABLE_RECORDER_WRITER_DIAGNOSTICS,
}


def resolved_options(data, options) -> dict:
    """Merge config-entry data/options with conservative defaults."""
    return {**DEFAULT_OPTIONS, **dict(data), **dict(options)}
