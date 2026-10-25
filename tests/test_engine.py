from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from ha_storage_monitor.engine import Sample, SnapshotDB, evaluate, allow_notification, HOUR, DAY, utc_hour

MiB=1024**2
T=10_000*HOUR

def history(growth=211,baseline=200,missing=None,shrink_day=None,cadence=1):
    """193 hourly points with 7 days baseline and final 24h anomalous growth."""
    data=[]
    for offset in range(8*24//cadence+1):
        hour=T-8*DAY+offset*cadence*HOUR
        days_elapsed=offset*cadence/24
        if days_elapsed<=7:
            size=1000*MiB + int(days_elapsed*baseline*MiB)
        else:
            size=1000*MiB+7*baseline*MiB+int((days_elapsed-7)*growth*MiB)
        if missing is None or hour not in missing:
            data.append(dict(hour=hour,size=size,status='ok',measured_at=hour))
    return data

def test_strict_threshold_exactly_five_percent():
    val=evaluate('recorder_total',history(growth=210),T)
    assert val.status=='normal'
    assert val.delta24==210*MiB

def test_threshold_above_five_percent():
    val=evaluate('recorder_total',history(growth=211),T)
    assert val.status=='alarm'
    assert val.baseline_daily==200*MiB
    assert round(val.excess_ratio*100,2)==5.5

def test_early_learning_needs_eight_days():
    assert evaluate('recorder_total',history()[24:],T).status=='learning'

def test_missing_interior_hour_causes_learning_if_large_gap():
    bad={T-2*DAY+j*HOUR for j in range(12)}
    assert evaluate('recorder_total',history(missing=bad),T).status=='learning'

def test_missing_boundary_causes_learning():
    assert evaluate('recorder_total',history(missing={T-7*DAY}),T).status=='learning'

def test_maintenance_even_with_recent_growth():
    points=history()
    for p in points:
        if p['hour']>=T-4*DAY: p['size']-=2000*MiB
    assert evaluate('recorder_total',points,T).status=='maintenance'

def test_zero_baseline_learning():
    assert evaluate('recorder_total',history(growth=211,baseline=0),T).status=='learning'

def test_extra_floor_default_disabled_and_optional_floor():
    assert evaluate('x',history(),T).status=='alarm'
    assert evaluate('x',history(),T,min_excess_bytes=20*MiB).status=='normal'

def test_six_hour_verified_supervisor_samples():
    assert evaluate('influx_addon',history(cadence=6),T,cadence_hours=6).status=='alarm'

def test_caching_does_not_fake_influx_measurement():
    rows=history(cadence=6)
    rows[3]['status']='cached'
    assert evaluate('influx_addon',rows,T,cadence_hours=6).status=='learning'

def test_dst_fall_back_two_unique_utc_hour_keys():
    tz=ZoneInfo('Europe/Ljubljana')
    a=datetime(2026,10,25,2,0,tzinfo=tz,fold=0).timestamp()
    b=datetime(2026,10,25,2,0,tzinfo=tz,fold=1).timestamp()
    assert utc_hour(a)!=utc_hour(b)
    assert abs(utc_hour(a)-utc_hour(b))==HOUR

def test_cooldown_and_acknowledgment(tmp_path):
    db=SnapshotDB(tmp_path/'archive.sqlite3');db.initialize()
    window='2026-10-25:18'
    assert allow_notification(db.get_alert('a'),window,T)
    db.set_alert('a',active=True,window=window,now=T,notified=True)
    assert not allow_notification(db.get_alert('a'),window,T+100)
    # Persisted after constructing an entirely new store instance.
    db2=SnapshotDB(tmp_path/'archive.sqlite3')
    assert not allow_notification(db2.get_alert('a'),'2026-10-26:00',T+3*HOUR)
    assert allow_notification(db2.get_alert('a'),'2026-10-26:00',T+6*HOUR)
    db2.acknowledge('a',T+7*HOUR)
    assert not allow_notification(db.get_alert('a'),'2026-10-26:06',T+6*HOUR)

def test_zero_is_distinct_from_unavailable_and_idempotent(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    db.save([Sample(T,'recorder_wal',0,'ok','stat',T),Sample(T,'influx_addon',None,'unavailable','supervisor',None)])
    db.save([Sample(T,'recorder_wal',1,'ok','stat',T)])
    latest={x['source']:x for x in db.latest()}
    assert latest['recorder_wal']['size']==1
    assert latest['influx_addon']['size'] is None
    assert len(db.history('recorder_wal',T))==1

def test_snapshot_history_survives_restart_and_retention_only_own(tmp_path):
    p=tmp_path/'own.sqlite3';db=SnapshotDB(p);db.initialize()
    db.save([Sample(T-DAY,'disk_free',123,'ok','stat',T-DAY),Sample(T,'disk_free',124,'ok','stat',T)])
    db2=SnapshotDB(p)
    assert len(db2.history('disk_free',T-DAY))==2
    db2.compact_retention(T)
    assert len(db2.history('disk_free',T-DAY))==1


def test_verified_sample_is_not_downgraded_by_cached_restart(tmp_path):
    db=SnapshotDB(tmp_path/'monitor.sqlite3');db.initialize()
    db.save([Sample(T,'influx_addon',100,'ok','supervisor',T)])
    db.save([Sample(T,'influx_addon',100,'cached','supervisor',T-HOUR)])
    row=db.latest()[0]
    assert row['size']==100 and row['status']=='ok' and row['measured_at']==T
    db.save([Sample(T,'influx_addon',None,'unavailable','supervisor',None)])
    assert db.latest()[0]['status']=='ok'
    # A new verified measurement in the same hour is allowed to replace the old one.
    db.save([Sample(T,'influx_addon',105,'ok','supervisor',T+100)])
    assert db.latest()[0]['size']==105


def test_own_database_connection_is_closed_after_read(tmp_path, monkeypatch):
    from ha_storage_monitor import engine
    real_connect=engine.sqlite3.connect
    calls=[]
    class ConnectionProxy:
        def __init__(self, conn): self.conn=conn
        def __enter__(self): self.conn.__enter__(); return self
        def __exit__(self,*args): return self.conn.__exit__(*args)
        def __getattr__(self,name): return getattr(self.conn,name)
        def close(self): calls.append('closed');self.conn.close()
    def tracked(*args,**kwargs): return ConnectionProxy(real_connect(*args,**kwargs))
    monkeypatch.setattr(engine.sqlite3,'connect',tracked)
    db=SnapshotDB(tmp_path/'own.sqlite3')
    db.initialize();db.latest();db.get_alert('a')
    assert len(calls)==3


def test_downgraded_hour_does_not_poison_eight_day_baseline(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    db.save([Sample(r['hour'],'recorder_total',r['size'],'ok','stat',r['hour']) for r in history()])
    db.save([Sample(T,'recorder_total',None,'unavailable','stat',None)])
    assert evaluate('recorder_total',db.history('recorder_total',T-8*DAY),T).status=='alarm'
