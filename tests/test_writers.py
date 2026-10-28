"""Offline verification of four locally scheduled writer windows per day."""
from datetime import datetime, timedelta
import pytest
import sqlite3
from zoneinfo import ZoneInfo

from ha_storage_monitor.engine import SnapshotDB, DAY, HOUR
from ha_storage_monitor.writers import (
    collect_writer_checkpoint, collect_writer_window, disabled_writer_summary,
    local_checkpoint_window, summarize_writer_windows,
)

TZ = ZoneInfo("Europe/Ljubljana")


def checkpoint(day=26, month=10, year=2026):
    return datetime(year, month, day, 18, tzinfo=TZ)


def windows(end_local, old_count=10, recent_count=11):
    records=[]
    for i in range(32):
        current=end_local-timedelta(hours=6*i)
        start,end=local_checkpoint_window(current)
        count=recent_count if i<4 else old_count
        records.append({"start_ts":start,"end_ts":end,"status":"ok",
                        "reason":"","total_rows":count+1,
                        "counts":{"sensor.test":count,"sensor.other":1}})
    return records


def ranked_windows(end_local, entity_count):
    counts={f"sensor.entity_{index:03d}":1000-index//2
            for index in range(entity_count)}
    records=[]
    for i in range(32):
        start,end=local_checkpoint_window(end_local-timedelta(hours=6*i))
        records.append({"start_ts":start,"end_ts":end,"status":"ok",
                        "reason":"","total_rows":sum(counts.values()),
                        "counts":dict(counts)})
    return records


def test_four_daily_checkpoints_and_32_eight_day_windows():
    end=checkpoint()
    records=windows(end)
    assert len(records)==32
    assert len({r['end_ts'] for r in records})==32
    result=summarize_writer_windows(records,int(end.timestamp()))
    assert result['status']=='ok'
    assert result['intervals_verified']==32
    assert result['last24_complete'] and result['previous7_complete']
    top=result['entities'][0]
    assert top['entity_id']=='sensor.test'
    assert top['rows_6h']==11
    assert top['rows_24h']==44
    assert top['previous7_daily']==40
    assert top['ratio']==1.1
    assert result['all_recorder_rows_24h']==48


@pytest.mark.parametrize("limit",(20,50,100,500))
def test_configurable_writer_ranking_limits_are_stable(limit):
    result=summarize_writer_windows(
        ranked_windows(checkpoint(),600),int(checkpoint().timestamp()),limit)
    assert len(result['entities'])==limit
    assert result['selected_limit']==limit
    assert result['ranked_entities_available']==600
    assert result['selected_entities_count']==limit
    # Equal counts are deterministically ordered by entity ID.
    assert [row['entity_id'] for row in result['entities'][:4]]==[
        'sensor.entity_000','sensor.entity_001',
        'sensor.entity_002','sensor.entity_003']


@pytest.mark.parametrize("limit",(0,19,21,499,501,True,None,"20"))
def test_invalid_writer_ranking_limits_are_rejected(limit):
    with pytest.raises(ValueError):
        summarize_writer_windows(windows(checkpoint()),
                                 int(checkpoint().timestamp()),limit)


def test_ranking_returns_only_genuine_entities_when_fewer_than_limit_exist():
    result=summarize_writer_windows(
        ranked_windows(checkpoint(),13),int(checkpoint().timestamp()),500)
    assert len(result['entities'])==13
    assert result['selected_entities_count']==13
    assert result['ranked_entities_available']==13


def test_incomplete_latest_interval_never_invents_ranked_counts_for_large_limit():
    records=ranked_windows(checkpoint(),120)[1:]
    result=summarize_writer_windows(records,int(checkpoint().timestamp()),500)
    assert result['status']=='learning'
    assert result['ranking_period']=='unavailable'
    assert result['entities']==[]
    assert result['all_recorder_rows_24h'] is None


def test_missing_window_never_misrepresents_complete_24h():
    end=checkpoint()
    records=windows(end)[1:]
    result=summarize_writer_windows(records,int(end.timestamp()))
    assert result['status']=='learning'
    assert result['intervals_missing']==1
    assert result['intervals_failed']==0
    assert not result['last24_complete']
    assert result['entities']==[]
    assert result['all_recorder_rows_24h'] is None


def test_old_missing_window_keeps_24h_but_not_weekly_baseline():
    end=checkpoint()
    records=windows(end)
    records.pop(9)
    result=summarize_writer_windows(records,int(end.timestamp()))
    assert result['status']=='learning'
    assert result['last24_complete']
    assert not result['previous7_complete']
    assert result['entities'][0]['rows_24h']==44
    assert result['entities'][0]['previous7_daily'] is None


def test_failed_interval_is_not_counted_as_verified():
    end=checkpoint()
    records=windows(end)
    records[0]['status']='unavailable'
    result=summarize_writer_windows(records,int(end.timestamp()))
    assert result['intervals_verified']==31
    assert result['intervals_failed']==1
    assert result['intervals_missing']==0
    assert not result['last24_complete']


def test_dst_fall_back_has_seven_hour_window_without_double_count():
    # Ljubljana falls back at 03:00 on Sunday, 25 Oct 2026.
    morning=datetime(2026,10,25,6,tzinfo=TZ)
    start,end=local_checkpoint_window(morning)
    assert end-start==7*HOUR
    records=windows(checkpoint())
    for older,newer in zip(records[1:],records[:-1]):
        assert older['end_ts']==newer['start_ts']
    result=summarize_writer_windows(records,int(checkpoint().timestamp()))
    assert result['status']=='ok'
    assert result['last24_elapsed_hours']==24


def test_dst_spring_forward_has_five_hour_window():
    morning=datetime(2026,3,29,6,tzinfo=TZ)
    start,end=local_checkpoint_window(morning)
    assert end-start==5*HOUR
    records=windows(datetime(2026,3,29,18,tzinfo=TZ))
    result=summarize_writer_windows(records,int(datetime(2026,3,29,18,tzinfo=TZ).timestamp()))
    assert result['last24_elapsed_hours']==23


def test_invalid_or_overlong_windows_refused(tmp_path):
    f=tmp_path/'recorder.db'
    f.touch()
    assert collect_writer_window(str(f),0,8*HOUR)['status']=='unavailable'
    assert collect_writer_window(str(f),100,100)['status']=='unavailable'


def create_test_recorder(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE states_meta (metadata_id INTEGER PRIMARY KEY, entity_id TEXT);
            CREATE TABLE states (metadata_id INTEGER, last_updated_ts REAL);
            CREATE INDEX ix_states_last_updated_ts ON states(last_updated_ts);
            INSERT INTO states_meta VALUES(1,'sensor.one'),(2,'sensor.two');
            INSERT INTO states VALUES(1,3600),(1,3601),(2,4000),(1,7200);
        ''')


def test_query_collects_only_bounded_interval_read_only(tmp_path):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    first=collect_writer_window(str(p),3600,7200)
    assert first['status']=='ok'
    assert first['total_rows']==3
    assert first['counts']=={'sensor.one':2,'sensor.two':1}
    assert collect_writer_window(str(p),7200,10800)['total_rows']==1
    with sqlite3.connect(p) as db:
        assert db.execute('SELECT COUNT(*) FROM states').fetchone()[0]==4


def test_missing_time_index_refuses_query(tmp_path):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    with sqlite3.connect(p) as db:
        db.execute('DROP INDEX ix_states_last_updated_ts')
    result=collect_writer_window(str(p),3600,7200)
    assert result['status']=='unavailable'
    assert 'index' in result['reason'].lower()


def test_entity_cap_does_not_publish_partial_counts(tmp_path):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    result=collect_writer_window(str(p),3600,7200,max_entities=1)
    assert result['status']=='unavailable'
    assert result['counts']=={}
    assert result['total_rows'] is None


def test_writer_windows_durable_and_idempotent(tmp_path):
    p=tmp_path/'own.sqlite3'
    db=SnapshotDB(p);db.initialize()
    rec=windows(checkpoint())[0]
    db.save_writer_window(rec)
    db.save_writer_window({**rec,'total_rows':999,'counts':{'wrong':999}})
    new=SnapshotDB(p)
    assert new.writer_window_exists(rec['end_ts'])
    rows=new.writer_window_history(rec['end_ts']-DAY,rec['end_ts'])
    assert len(rows)==1
    assert rows[0]['total_rows']==12
    assert rows[0]['counts']['sensor.test']==11
    new.prune_writer_windows(rec['end_ts']+1)
    assert not new.writer_window_exists(rec['end_ts'])


def test_zero_rows_are_verified_zero_not_missing(tmp_path):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    result=collect_writer_window(str(p),10000,20000)
    assert result['status']=='ok'
    assert result['total_rows']==0
    assert result['counts']=={}


def test_payload_cap_does_not_publish_partial_counts(tmp_path):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    result=collect_writer_window(str(p),3600,7200,max_json_bytes=4)
    assert result['status']=='unavailable'
    assert result['total_rows'] is None
    assert result['counts']=={}


def test_time_budget_interrupts_query_deterministically(tmp_path,monkeypatch):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    readings=iter((100.0,101.0))
    monkeypatch.setattr('ha_storage_monitor.writers.monotonic',lambda:next(readings))
    result=collect_writer_window(str(p),3600,7200,budget_seconds=0.5)
    assert result['status']=='unavailable'
    assert result['counts']=={}


def test_non_destructive_v1_monitor_store_upgrade(tmp_path):
    p=tmp_path/'monitor.sqlite3'
    with sqlite3.connect(p) as db:
        db.executescript('''
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT INTO metadata VALUES('schema','1');
            CREATE TABLE writers(hour INTEGER PRIMARY KEY,data TEXT NOT NULL);
            INSERT INTO writers VALUES(42,'{"status":"ok"}');
        ''')
    store=SnapshotDB(p)
    store.initialize()
    assert store.writer_window_history(0,999)==[]
    with sqlite3.connect(p) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0]=='2'
        assert db.execute('SELECT hour FROM writers').fetchone()[0]==42


def test_row_safety_cap_fails_closed_without_partial_results(tmp_path):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    result=collect_writer_window(str(p),3600,7200,max_rows=2)
    assert result['status']=='unavailable'
    assert result['total_rows'] is None
    assert result['counts']=={}
    assert 'row safety cap' in result['reason'].lower()


def test_invalid_caps_fail_closed(tmp_path):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    for kwargs in ({'max_rows':0},{'max_entities':0},{'max_json_bytes':0}):
        assert collect_writer_window(str(p),3600,7200,**kwargs)['status']=='unavailable'


def test_writer_query_does_not_need_temporary_grouping(tmp_path):
    p=tmp_path/'recorder.db';create_test_recorder(p)
    with sqlite3.connect(f'file:{p}?mode=ro',uri=True) as db:
        plan=' '.join(str(x[3]) for x in db.execute("""
            EXPLAIN QUERY PLAN SELECT m.entity_id
            FROM states AS s INDEXED BY ix_states_last_updated_ts
            JOIN states_meta AS m ON m.metadata_id=s.metadata_id
            WHERE s.last_updated_ts >= 3600 AND s.last_updated_ts < 7200
        """))
    assert 'USING INDEX ix_states_last_updated_ts' in plan
    assert 'TEMP B-TREE' not in plan


def test_mismatched_interval_is_reported_separately():
    end=checkpoint()
    records=windows(end)
    records[0]['start_ts']+=1
    result=summarize_writer_windows(records,int(end.timestamp()))
    assert result['intervals_mismatched']==1
    assert result['intervals_missing']==0
    assert result['intervals_failed']==0
    assert result['status']=='learning'


def test_disabled_checkpoint_never_invokes_recorder_collector():
    def forbidden(*args):
        raise AssertionError("Recorder collector must not run while disabled")
    result=collect_writer_checkpoint('/missing/recorder.db',0,6*HOUR,
        enabled=False,enabled_at=None,collector=forbidden)
    assert result is None
    summary=disabled_writer_summary()
    assert summary['status']=='disabled'
    assert summary['reason']=='Recorder writer diagnostics disabled'
    assert summary['all_recorder_rows_24h'] is None


def test_enabled_checkpoint_invokes_existing_bounded_collector():
    calls=[]
    def collector(path,start,end):
        calls.append((path,start,end))
        return {'start_ts':start,'end_ts':end,'status':'ok','reason':'',
                'total_rows':1,'counts':{'sensor.test':1}}
    result=collect_writer_checkpoint('/recorder.db',6*HOUR,12*HOUR,
        enabled=True,enabled_at=6*HOUR,collector=collector)
    assert result['status']=='ok'
    assert calls==[('/recorder.db',6*HOUR,12*HOUR)]


def test_newly_enabled_checkpoint_refuses_retroactive_interval():
    def forbidden(*args):
        raise AssertionError("A partial pre-opt-in interval must not be queried")
    result=collect_writer_checkpoint('/recorder.db',6*HOUR,12*HOUR,
        enabled=True,enabled_at=6*HOUR+1,collector=forbidden)
    assert result['status']=='unavailable'
    assert result['total_rows'] is None
    assert 'retroactive query refused' in result['reason']


def test_existing_checkpoint_is_not_queried_again():
    def forbidden(*args):
        raise AssertionError("An immutable checkpoint must not be queried twice")
    assert collect_writer_checkpoint('/recorder.db',6*HOUR,12*HOUR,
        enabled=True,enabled_at=0,already_exists=True,collector=forbidden) is None
