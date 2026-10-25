"""Constants for the independent Home Assistant Storage Monitor."""
DOMAIN = "ha_storage_monitor"
DEFAULT_INFLUX_SLUG = "a0d7b954_influxdb"
DEFAULT_DISK_LOW_GIB = 15
DEFAULT_RETENTION_DAYS = 365
DEFAULT_MIN_EXCESS_MIB = 0
DEFAULT_NOTIFY_PERSISTENT = True
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
