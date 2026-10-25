from ha_storage_monitor.sources import collect_local
from ha_storage_monitor.supervisor import extract_addon_usage
from ha_storage_monitor.writers import top_writers
from pathlib import Path
import sqlite3


def test_local_files_and_missing_sidecars(tmp_path):
    (tmp_path/'home-assistant_v2.db').write_bytes(b'12345')
    samples=collect_local(str(tmp_path),3600,3610)
    m={s.source:s for s in samples}
    assert m['recorder_main'].size==5
    assert m['recorder_wal'].size==0
    assert m['recorder_shm'].size==0
    assert m['recorder_total'].size==5
    assert m['disk_free'].size is not None

def test_readonly_generic_sqlite_discovery(tmp_path):
    storage=tmp_path/'.storage';storage.mkdir()
    (tmp_path/'home-assistant_v2.db').write_bytes(b'12345')
    (storage/'energy_history.db').write_bytes(b'12')
    samples=collect_local(str(tmp_path),3600,3610)
    assert {s.source:s.size for s in samples}['database:.storage/energy_history.db']==2
    assert {s.source:s.size for s in samples}['database_total']==2


def test_permission_scope_influx_exact_subtree():
    payload={'result':'ok','data':{'children':[
        {'id':'addons_data','used_bytes':1000,'children':[
            {'id':'a0d7b954_influxdb','used_bytes':301},
            {'id':'different_addon','used_bytes':699}]}]}}
    x=extract_addon_usage(payload,'a0d7b954_influxdb')
    assert x.size==301 and x.status=='ok'
    assert extract_addon_usage(payload,'unknown').size is None
    assert extract_addon_usage({'result':'ok','data':{'children':[{'id':'addons_data','used_bytes':1000}]}},'a0d7b954_influxdb').status=='unavailable'
    assert extract_addon_usage({'result':'error'},'a0d7b954_influxdb').status=='unavailable'


def test_writer_query_uses_index_and_returns_counts(tmp_path):
    dbp=tmp_path/'home-assistant_v2.db'
    with sqlite3.connect(dbp) as db:
        db.execute('CREATE TABLE states(state_id INTEGER PRIMARY KEY, metadata_id INTEGER, last_updated_ts REAL)')
        db.execute('CREATE TABLE states_meta(metadata_id INTEGER PRIMARY KEY, entity_id TEXT)')
        db.execute('CREATE INDEX ix_states_last_updated_ts ON states(last_updated_ts)')
        db.executemany('INSERT INTO states_meta VALUES(?,?)',[(1,'sensor.a'),(2,'sensor.b')])
        db.executemany('INSERT INTO states(metadata_id,last_updated_ts) VALUES(?,?)',[(1,10000),(1,10001),(2,10002),(1,10003)])
    now=10020
    result=top_writers(str(dbp),now)
    assert result['status']=='ok'
    assert result['entities'][0]['entity_id']=='sensor.a'
    assert result['entities'][0]['rows_24h']==3


def test_missing_index_refuses_expensive_full_scan(tmp_path):
    dbp=tmp_path/'home-assistant_v2.db'
    with sqlite3.connect(dbp) as db:
        db.execute('CREATE TABLE states(state_id INTEGER PRIMARY KEY, metadata_id INTEGER, last_updated_ts REAL)')
    assert top_writers(str(dbp),10000)['status']=='unavailable'


def test_new_supervisor_v2_apps_data_key():
    data={'data':{'children':[{'id':'apps_data','children':[{'id':'a0d7b954_influxdb','used_bytes':9876}]}]}}
    assert extract_addon_usage(data,'a0d7b954_influxdb').size==9876


def test_generic_sqlite_wal_shm_are_included(tmp_path):
    (tmp_path / "home-assistant_v2.db").write_bytes(b"recorder")
    (tmp_path / "other.sqlite3").write_bytes(b"abc")
    (tmp_path / "other.sqlite3-wal").write_bytes(b"12345")
    (tmp_path / "other.sqlite3-shm").write_bytes(b"12")
    samples = {s.source:s for s in collect_local(str(tmp_path),3600,3610)}
    assert samples["database:other.sqlite3"].size == 10
    assert samples["database_total"].size == 10
    assert "database:home-assistant_v2.db" not in samples


def test_excludes_own_monitor_history(tmp_path):
    (tmp_path / "home-assistant_v2.db").write_bytes(b"r")
    store = tmp_path / ".storage"
    store.mkdir()
    (store / "ha_storage_monitor_history.sqlite3").write_bytes(b"a" * 100)
    (store / "another.db").write_bytes(b"b" * 7)
    samples = {s.source:s for s in collect_local(str(tmp_path),3600,3610)}
    assert "database:.storage/ha_storage_monitor_history.sqlite3" not in samples
    assert samples["database_total"].size == 7


def test_protects_from_symlinked_database(tmp_path):
    (tmp_path / "home-assistant_v2.db").write_bytes(b"r")
    outside = tmp_path / "real.db"
    outside.write_bytes(b"123")
    (tmp_path / "link.db").symlink_to(outside)
    samples = {s.source:s for s in collect_local(str(tmp_path),3600,3610)}
    assert samples["database:link.db"].status == "unavailable"
    assert samples["database_total"].status == "unavailable"


def test_generic_sqlite_total_zero_when_directories_scanned_and_empty(tmp_path):
    (tmp_path/'home-assistant_v2.db').write_bytes(b'recorder')
    (tmp_path/'.storage').mkdir()
    data={s.source:s for s in collect_local(str(tmp_path),3600,3610)}
    assert data['database_total'].status=='ok'
    assert data['database_total'].size==0


def test_generic_sqlite_total_never_reports_truncated_scan_as_exact(tmp_path):
    (tmp_path/'home-assistant_v2.db').write_bytes(b'recorder')
    for i in range(65):
        (tmp_path/f'other{i:03}.db').write_bytes(b'x')
    data={s.source:s for s in collect_local(str(tmp_path),3600,3610)}
    assert data['database_total'].status=='unavailable'
    assert data['database_total'].size is None
    assert sum(s.source.startswith('database:') for s in data.values())<=64


def test_generic_sqlite_directory_symlink_not_traversed(tmp_path):
    (tmp_path/'home-assistant_v2.db').write_bytes(b'recorder')
    other=tmp_path/'external';other.mkdir()
    (other/'data.db').write_bytes(b'abc')
    (tmp_path/'.storage').symlink_to(other,target_is_directory=True)
    data={s.source:s for s in collect_local(str(tmp_path),3600,3610)}
    assert data['database_total'].status=='unavailable'
    assert 'database:.storage/data.db' not in data
