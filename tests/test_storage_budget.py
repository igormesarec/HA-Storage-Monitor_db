"""Synthetic, offline storage-size budget checks (not production measurements)."""
from ha_storage_monitor.engine import SnapshotDB, Sample, HOUR


def test_one_year_normal_snapshot_workload_stays_below_ten_mib(tmp_path):
    """Model 12 hourly sources plus nine retained days of compact writer data."""
    path = tmp_path / "monitor.sqlite3"
    db = SnapshotDB(path)
    db.initialize()
    sources = (
        "recorder_main", "recorder_wal", "recorder_shm", "recorder_total",
        "disk_total", "disk_free", "disk_used", "influx_addon",
        "database_total", "database:.storage/archive_a.db",
        "database:.storage/archive_b.db", "database:other.sqlite3",
    )
    for day in range(365):
        batch = []
        for hour_in_day in range(24):
            hour = (day * 24 + hour_in_day) * HOUR
            batch.extend(Sample(hour, source, 1_000_000 + day * 1000,
                                "ok", "file_stat_apparent", hour)
                         for source in sources)
        db.save(batch)
    counts = {f"sensor.synthetic_{index}": index + 1 for index in range(100)}
    for checkpoint in range(36):
        end_ts = (365 * 24 + checkpoint * 6) * HOUR
        db.save_writer_window({"start_ts": end_ts - 6 * HOUR,
                               "end_ts": end_ts, "status": "ok",
                               "reason": "", "total_rows": sum(counts.values()),
                               "counts": counts})
    assert path.stat().st_size < 10 * 1024 * 1024
