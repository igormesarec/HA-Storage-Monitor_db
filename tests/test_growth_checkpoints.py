"""Four size checkpoints/day, while retaining hourly purge evidence."""
from ha_storage_monitor.engine import SnapshotDB, Sample, evaluate, HOUR, DAY

MiB=1024**2
T=10_000*HOUR


def populate(db, *, missing=None, shrink=False, source='recorder_total'):
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
            rows.append(Sample(hour,source,size,'ok','stat',hour))
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
    assert e['verified_days']==8
    assert e['verified_checkpoints']==33
    assert e['missing_required_measurements']==0


def test_home_energy_intelligence_uses_hourly_eight_day_growth_evidence(tmp_path):
    source='database:.storage/home_energy_intelligence/home_energy_intelligence.sqlite3'
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    populate(db,source=source)
    evidence=db.growth_evidence(source,T,hourly_source=True)
    result=evaluate(source,evidence['points'],T,cadence_hours=6,
                    observed_shrink=evidence['observed_shrink'],
                    hourly_coverage_ok=evidence['hourly_coverage_ok'])
    assert len(evidence['points'])==33
    assert evidence['hourly_coverage_ok'] is True
    assert result.status=='alarm'


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


def test_exactly_twenty_hourly_measurements_per_utc_day_is_sufficient(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    populate(db,missing={49,50,51,52})
    evidence=db.growth_evidence('recorder_total',T,hourly_source=True)
    assert evidence['hourly_coverage_ok'] is True
    assert min(day['verified'] for day in evidence['day_coverage'])==20
    result=evaluate('recorder_total',evidence['points'],T,cadence_hours=6,
                    hourly_coverage_ok=evidence['hourly_coverage_ok'],
                    diagnostics=evidence)
    assert result.status=='alarm'


def test_one_missing_checkpoint_reports_exact_diagnostics(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    populate(db,missing={6})
    evidence=db.growth_evidence('recorder_total',T,hourly_source=True)
    result=evaluate('recorder_total',evidence['points'],T,cadence_hours=6,
                    hourly_coverage_ok=evidence['hourly_coverage_ok'],
                    diagnostics=evidence)
    assert result.status=='learning'
    assert result.reason_code=='missing_checkpoints'
    assert result.missing_required_measurements==1
    assert result.verified_checkpoints==32
    assert result.verified_days==7
    assert result.last_shrink_at is None


def test_cached_measurements_cannot_form_verified_baseline(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize();populate(db)
    hour=T-6*DAY
    db.save([Sample(hour,'recorder_total',None,'unavailable','stat',None)])
    # A verified observation cannot be overwritten by an unavailable sample.
    e=db.growth_evidence('recorder_total',T,hourly_source=True)
    assert len(e['points'])==33
    assert all(x['status']=='ok' for x in e['points'])


def test_influx_local_checkpoints_across_dst_fail_closed(tmp_path):
    """Do not invent an exact elapsed 24h value across a shifted UTC phase."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('Europe/Ljubljana')
    end=datetime(2026,10,30,18,tzinfo=tz)
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    samples=[]
    for i in range(33):
        local=end-timedelta(hours=6*i)
        hour=int(local.timestamp())//HOUR*HOUR
        samples.append(Sample(hour,'influx_addon',int((10_000-i*50)*MiB),
                              'ok','supervisor',hour))
    db.save(samples)
    now=int(end.timestamp())//HOUR*HOUR
    evidence=db.growth_evidence('influx_addon',now,hourly_source=False)
    result=evaluate('influx_addon',evidence['points'],now,cadence_hours=6,
                    observed_shrink=evidence['observed_shrink'])
    assert result.status=='learning'
    assert len(evidence['points']) < 33


def test_influx_local_checkpoints_after_dst_rebuild_clean_baseline(tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('Europe/Ljubljana')
    end=datetime(2026,11,10,18,tzinfo=tz)
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    samples=[]
    for i in range(33):
        local=end-timedelta(hours=6*i)
        hour=int(local.timestamp())//HOUR*HOUR
        samples.append(Sample(hour,'influx_addon',int((10_000-i*50)*MiB),
                              'ok','supervisor',hour))
    db.save(samples)
    now=int(end.timestamp())//HOUR*HOUR
    evidence=db.growth_evidence('influx_addon',now,hourly_source=False)
    result=evaluate('influx_addon',evidence['points'],now,cadence_hours=6,
                    observed_shrink=evidence['observed_shrink'])
    assert len(evidence['points'])==33
    assert result.status=='normal'


def test_shrink_ages_out_after_eight_clean_days(tmp_path):
    source='database:active.sqlite3'
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize()
    db.save([Sample(T-9*DAY,source,2_000*MiB,'ok','stat',T-9*DAY),
             Sample(T-9*DAY+HOUR,source,1_000*MiB,'ok','stat',T-9*DAY+HOUR)])
    populate(db,source=source)
    evidence=db.growth_evidence(source,T,hourly_source=True)
    result=evaluate(source,evidence['points'],T,cadence_hours=6,
                    observed_shrink=evidence['observed_shrink'],
                    hourly_coverage_ok=evidence['hourly_coverage_ok'],
                    diagnostics=evidence)
    assert evidence['last_shrink'] is None
    assert result.status=='alarm'


def test_actual_shrink_timestamp_and_last_24h_remain_visible(tmp_path):
    db=SnapshotDB(tmp_path/'own.sqlite3');db.initialize();populate(db,shrink=True)
    evidence=db.growth_evidence('recorder_total',T,hourly_source=True)
    result=evaluate('recorder_total',evidence['points'],T,cadence_hours=6,
                    observed_shrink=evidence['observed_shrink'],
                    hourly_coverage_ok=evidence['hourly_coverage_ok'],
                    diagnostics=evidence)
    assert result.status=='maintenance'
    assert result.reason_code=='observed_shrink'
    assert result.last_shrink_at==T-8*DAY+45*HOUR
    assert result.last_shrink_from is not None
    assert result.last_shrink_to is not None
    assert result.delta24==211*MiB
    assert result.baseline_eligible_not_before>result.last_shrink_at


def test_cached_and_unavailable_influx_checkpoints_have_specific_reasons(tmp_path):
    now=T
    for status,reason in [('cached','latest_cached'),
                          ('unavailable','latest_unavailable')]:
        db=SnapshotDB(tmp_path/f'{status}.sqlite3');db.initialize()
        rows=[]
        for index,hour in enumerate(range(now-8*DAY,now+1,6*HOUR)):
            current=status if hour==now else 'ok'
            rows.append(Sample(hour,'influx_addon',
                None if current=='unavailable' else 1000*MiB+index*MiB,
                current,'supervisor',hour if current=='ok' else None))
        db.save(rows)
        evidence=db.growth_evidence('influx_addon',now,hourly_source=False)
        result=evaluate('influx_addon',evidence['points'],now,cadence_hours=6,
                        diagnostics=evidence)
        assert result.status=='learning'
        assert result.reason_code==reason
        assert result.missing_required_measurements==1


def test_offset_startup_measurement_is_not_a_checkpoint(tmp_path):
    db=SnapshotDB(tmp_path/'offset.sqlite3');db.initialize()
    rows=[Sample(hour,'influx_addon',1000*MiB+index*MiB,'ok',
                 'supervisor',hour)
          for index,hour in enumerate(range(T-8*DAY,T,6*HOUR))]
    rows.append(Sample(T,'influx_addon',1100*MiB,'ok','supervisor',
                       T+34*60))
    db.save(rows)
    evidence=db.growth_evidence('influx_addon',T,hourly_source=False)
    result=evaluate('influx_addon',evidence['points'],T,cadence_hours=6,
                    diagnostics=evidence)
    assert evidence['latest_checkpoint_status']=='off_checkpoint'
    assert result.reason_code=='latest_off_checkpoint'
    assert result.status=='learning'


def test_existing_history_gains_diagnostics_without_rewriting_rows(tmp_path):
    path=tmp_path/'existing.sqlite3'
    original=SnapshotDB(path);original.initialize();populate(original)
    before=original.history('recorder_total',T-8*DAY)
    restarted=SnapshotDB(path);restarted.initialize()
    evidence=restarted.growth_evidence('recorder_total',T,hourly_source=True)
    assert restarted.history('recorder_total',T-8*DAY)==before
    assert evidence['verified_days']==8
    assert evidence['verified_checkpoints']==33
    assert evidence['last_shrink'] is None
