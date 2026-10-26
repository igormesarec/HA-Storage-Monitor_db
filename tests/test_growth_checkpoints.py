"""Four size checkpoints/day, while retaining hourly purge evidence."""
from ha_storage_monitor.engine import SnapshotDB, Sample, evaluate, HOUR, DAY

MiB=1024**2
T=10_000*HOUR


def populate(db, *, missing=None, shrink=False):
    rows=[]
    for i in range(8*24+1):
        hour=T-8*DAY+i*HOUR
        day=i/24
        if day<=7:
            size=1000*MiB+int(day*200*MiB)
        else:
            size=2400*MiB+int((day-7)*211*MiB)
        if shrink and i==45:  # Non-checkpoint: 45 % 6 != 0
            size-=500*MiB
        if missing is None or i not in missing:
            rows.append(Sample(hour,'recorder_total',size,'ok','stat',hour))
    db.save(rows)


def test_evaluation_reads_33_checkpoints_not_193_hourly_rows(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize();populate(db)
    e=db.growth_evidence('recorder_total',T,hourly_source=True)
    assert len(e['points'])==33
    assert e['hourly_coverage_ok']
    assert not e['observed_shrink']
    result=evaluate('recorder_total',e['points'],T,cadence_hours=6,
                    observed_shrink=e['observed_shrink'],
                    hourly_coverage_ok=e['hourly_coverage_ok'])
    assert result.status=='alarm'
    assert result.delta24==211*MiB
    assert result.baseline_daily==200*MiB


def test_hourly_purge_between_checkpoints_is_not_hidden(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize();populate(db,shrink=True)
    e=db.growth_evidence('recorder_total',T,hourly_source=True)
    assert len(e['points'])==33
    assert e['observed_shrink']
    assert evaluate('recorder_total',e['points'],T,cadence_hours=6,
                    observed_shrink=e['observed_shrink']).status=='maintenance'


def test_missing_five_noncheckpoint_hours_blocks_growth_alarm(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    populate(db,missing={49,50,51,52,53})
    e=db.growth_evidence('recorder_total',T,hourly_source=True)
    assert len(e['points'])>=32
    assert not e['hourly_coverage_ok']
    assert evaluate('recorder_total',e['points'],T,cadence_hours=6,
                    hourly_coverage_ok=e['hourly_coverage_ok']).status=='learning'


def test_cached_measurements_cannot_form_verified_baseline(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize();populate(db)
    hour=T-6*DAY
    db.save([Sample(hour,'recorder_total',None,'unavailable','stat',None)])
    # A verified observation cannot be overwritten by an unavailable sample.
    e=db.growth_evidence('recorder_total',T,hourly_source=True)
    assert len(e['points'])==33
    assert all(x['status']=='ok' for x in e['points'])
