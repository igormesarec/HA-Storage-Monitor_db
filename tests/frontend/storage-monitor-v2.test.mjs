import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {fileURLToPath} from 'node:url';
import {JSDOM} from 'jsdom';

const source=readFileSync(fileURLToPath(new URL(
  '../../custom_components/ha_storage_monitor/frontend/storage-monitor-card.js',
  import.meta.url,
)),'utf8');
const GIB=1024**3, MIB=1024**2, NOW=1_799_424_000;

function summary(overrides={}) {
  const rows=[
    ['recorder_total',2*GIB,'ok','sum_apparent_file_stat'],
    ['recorder_main',2*GIB-3*MIB,'ok','file_stat_apparent'],
    ['recorder_wal',3*MIB,'ok','file_stat_apparent'],
    ['recorder_shm',32*1024,'ok','file_stat_apparent'],
    ['influx_addon',null,'unavailable','supervisor_host_disk_usage'],
    ['database_total',9*MIB,'ok','sum_discovered_local_sqlite_files'],
    ['database:an-extremely-long-independent-database-name-for-layout.sqlite3',9*MIB,'ok','sqlite_main_wal_shm_file_stat'],
    ['disk_total',320*GIB,'ok','config_path_disk_usage'],
    ['disk_used',100*GIB,'ok','config_path_disk_usage'],
    ['disk_free',200*GIB,'ok','config_path_disk_usage'],
  ];
  return {
    samples:rows.map(([source,size,status,method])=>({source,size,status,method,measured_at:NOW})),
    alerts:{disk_low:{active:0},'stale:influx_addon':{active:1,acknowledged_until:0}},
    growth:{recorder_total:{status:'learning',delta24:null,baseline_daily:null,excess_ratio:null}},
    writers:{status:'disabled',reason:'Recorder writer diagnostics disabled'},
    changes:{recorder_total:{delta6h:1024,delta24h:null},disk_free:{delta6h:0,delta24h:0}},
    effective_options:{low_disk_gib:15,min_excess_mib:0,retention_days:365,repeat_notifications:false,enable_recorder_writer_diagnostics:false},
    writer_diagnostics_enabled:false,now:NOW,scope:'Test filesystem scope',...overrides,
  };
}

function cardWith(data=summary()) {
  const dom=new JSDOM('<!doctype html><body></body>',{runScripts:'outside-only',url:'http://homeassistant.local/'});
  dom.window.setInterval=()=>1; dom.window.clearInterval=()=>{}; dom.window.eval(source);
  const card=dom.window.document.createElement('ha-storage-monitor-card');
  card.setConfig({language:'en'}); card._data=data; card._points=[]; card.render();
  return {dom,card};
}

test('all seven views render nulls, long names, learning, alerts and units',()=>{
  const {dom,card}=cardWith();
  const tabs=[...card.shadowRoot.querySelectorAll('[data-tab]')];
  assert.deepEqual(tabs.map(tab=>tab.dataset.tab),['overview','databases','charts','writers','disk','alerts','settings']);
  for(const key of tabs.map(tab=>tab.dataset.tab)) {
    card._tab=key; card.render();
    assert.ok(card.shadowRoot.textContent.length>40,`${key} should render`);
  }
  card._tab='charts'; card.render();
  assert.deepEqual([...card.shadowRoot.querySelectorAll('[data-days]')].map(button=>button.dataset.days),['1','7','30','90']);
  card._tab='databases'; card.render();
  assert.match(card.shadowRoot.textContent,/Unavailable/);
  assert.equal(card.shadowRoot.querySelector('[data-source^="database:"] .title').getAttribute('title'),'an-extremely-long-independent-database-name-for-layout.sqlite3');
  card._tab='writers'; card.render();
  assert.match(card.shadowRoot.textContent,/Recorder writer diagnostics disabled/);
  assert.doesNotMatch(card.shadowRoot.textContent,/0 rows/);
  card._tab='alerts'; card.render();
  assert.match(card.shadowRoot.textContent,/learning/i);
  card._tab='disk'; card.render();
  assert.match(card.shadowRoot.textContent,/20\.00 GiB/);
  assert.match(card.shadowRoot.textContent,/reserved or unavailable/);
  dom.window.close();
});

test('chart reports constant and small fluctuations without hiding precision',()=>{
  const {dom,card}=cardWith();
  card._points=[0,1,2].map(index=>({hour:NOW+(index-2)*3600,size:5*GIB,status:'ok'}));
  let model=card._chartModel();
  assert.equal(model.range,0); assert.equal(model.segments.length,1);
  assert.match(card.graph(),/No measured change/);
  card._points=[
    {hour:NOW-7200,size:5*GIB,status:'ok'},
    {hour:NOW-3600,size:5*GIB+64*1024,status:'ok'},
    {hour:NOW,size:5*GIB+32*1024,status:'ok'},
  ];
  model=card._chartModel();
  assert.equal(model.range,64*1024); assert.equal(model.net,32*1024);
  const graph=card.graph();
  assert.match(graph,/0\.063 MiB/); assert.match(graph,/0\.031 MiB/);
  assert.match(graph,/Measured decrease/);
  dom.window.close();
});

test('chart leaves hourly gaps, excludes caches and treats DST by elapsed UTC time',()=>{
  const {dom,card}=cardWith();
  card._points=[
    {hour:NOW-2*3600,size:100*MIB,status:'ok'},
    {hour:NOW,size:101*MIB,status:'ok'},
  ];
  let model=card._chartModel();
  assert.equal(model.segments.length,2,'one missing hourly observation must create a gap');
  card._points=[
    {hour:NOW-5*3600,size:100*MIB,status:'ok'},
    {hour:NOW,size:100*MIB,status:'ok'},
  ];
  model=card._chartModel();
  assert.equal(model.segments.length,2,'multiple missing hourly observations must create one visible gap');
  card._points=[
    {hour:NOW-2*3600,size:100*MIB,status:'ok'},
    {hour:NOW-3600,size:100*MIB,status:'cached'},
    {hour:NOW,size:101*MIB,status:'ok'},
  ];
  model=card._chartModel();
  assert.equal(model.segments.length,2,'a cached hourly value must not bridge missing measurements');
  assert.equal(model.cached,1);
  card._selected='influx_addon';
  card._points=[
    {hour:NOW-6*3600,size:7*GIB,status:'ok'},
    {hour:NOW-5*3600,size:7*GIB+MIB,status:'cached'},
    {hour:NOW,size:7*GIB+2*MIB,status:'ok'},
  ];
  model=card._chartModel();
  assert.equal(model.segments.length,1,'normal six-hour Influx cadence stays connected');
  assert.equal(model.cached,1); assert.equal(model.measured.length,2);
  assert.match(card.graph(),/cached values excluded/);
  card._points=[
    {hour:NOW-12*3600,size:7*GIB,status:'ok'},
    {hour:NOW,size:7*GIB+2*MIB,status:'ok'},
  ];
  assert.equal(card._chartModel().segments.length,2,'a missing six-hour Influx measurement creates a gap');
  const fallback=Date.parse('2026-10-24T22:00:00Z')/1000;
  card._points=[
    {hour:fallback,size:7*GIB,status:'ok'},
    {hour:fallback+3600,size:7*GIB+MIB,status:'cached'},
    {hour:fallback+7*3600,size:7*GIB+2*MIB,status:'ok'},
  ];
  model=card._chartModel();
  assert.equal(model.segments.length,1,'seven-hour autumn fallback Influx interval stays connected');
  const spring=Date.parse('2026-03-28T23:00:00Z')/1000;
  card._points=[
    {hour:spring,size:7*GIB,status:'ok'},
    {hour:spring+3600,size:7*GIB+MIB,status:'cached'},
    {hour:spring+5*3600,size:7*GIB+2*MIB,status:'ok'},
  ];
  assert.equal(card._chartModel().segments.length,1,'five-hour spring-forward Influx interval stays connected');
  card._selected='recorder_total';
  card._points=[0,1,2].map(index=>({hour:spring+index*3600,size:100*MIB+index,status:'ok'}));
  assert.equal(card._chartModel().segments.length,1,'spring-forward UTC hours stay continuous');
  card._points=[0,1,2].map(index=>({hour:fallback+index*3600,size:100*MIB+index,status:'ok'}));
  assert.equal(card._chartModel().segments.length,1,'autumn fallback UTC hours stay continuous');
  dom.window.close();
});

test('growth alarms expose durable 24-hour and seven-day acknowledgments',async()=>{
  const active=summary({
    alerts:{disk_low:{active:0},recorder_total:{active:1,acknowledged_until:0}},
    growth:{recorder_total:{status:'alarm',delta24:220*MIB,baseline_daily:200*MIB,excess_ratio:.1}},
  });
  const {dom,card}=cardWith(active);
  dom.window.confirm=()=>true;
  const calls=[];
  card._hass={callWS:async message=>{
    calls.push(message);
    active.alerts.recorder_total.acknowledged_until=NOW+Number(message.hours)*3600;
  }};
  card.reload=async()=>{card._data=active;card.render();};
  card._tab='alerts'; card.render();
  let controls=[...card.shadowRoot.querySelectorAll('[data-ack="recorder_total"]')];
  assert.deepEqual(controls.map(button=>button.dataset.hours),['24','168']);
  assert.match(card.shadowRoot.textContent,/Not acknowledged/);
  controls[0].click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(calls[0].type,'ha_storage_monitor/acknowledge');
  assert.equal(calls[0].source,'recorder_total');
  assert.equal(calls[0].hours,24);
  assert.match(card.shadowRoot.textContent,/Acknowledged until/);
  controls=[...card.shadowRoot.querySelectorAll('[data-ack="recorder_total"]')];
  controls[1].click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(calls[1].hours,168);
  card._data=summary({
    alerts:{disk_low:{active:0},recorder_total:{active:1,acknowledged_until:NOW+168*3600}},
    growth:active.growth,
  });
  card.render();
  assert.match(card.shadowRoot.textContent,/Acknowledged until/,'returned acknowledgment state must survive refresh');
  card._data.alerts.recorder_total={active:0,acknowledged_until:NOW-1};
  card._data.growth.recorder_total={status:'normal',delta24:10*MIB,baseline_daily:20*MIB,excess_ratio:null};
  card.render();
  assert.equal(card.shadowRoot.querySelectorAll('[data-ack="recorder_total"]').length,0);
  assert.match(card.shadowRoot.textContent,/Clear/);
  dom.window.close();
});

test('enabled writer data and language switching remain functional',()=>{
  const enabled=summary({
    writer_diagnostics_enabled:true,
    writers:{status:'ok',intervals_verified:32,intervals_required:32,selected_limit:20,page:1,page_size:50,total_pages:1,entities:[{entity_id:'sensor.test',integration:'demo',rows_6h:12,rows_24h:48,share_percent:50,rate_per_min:.033,previous7_daily:24,ratio:2,high_writer:true}],grouped_by_integration:[{integration:'demo',rows_6h:12,rows_24h:48,entities_in_selected:1}]},
    effective_options:{...summary().effective_options,enable_recorder_writer_diagnostics:true},
  });
  const {dom,card}=cardWith(enabled);
  card._tab='writers'; card.render();
  assert.match(card.shadowRoot.textContent,/sensor\.test/);
  assert.match(card.shadowRoot.textContent,/Partial integration totals derived from selected Top 20/);
  assert.match(card.shadowRoot.textContent,/Records\/min/);
  assert.match(card.shadowRoot.textContent,/Prior 7d daily avg/);
  assert.match(card.shadowRoot.textContent,/2×/);
  assert.match(card.shadowRoot.textContent,/Warning/);
  card._data.writers.entities[0]={entity_id:'sensor.learning',integration:'demo',rows_6h:3,rows_24h:null,share_percent:null,rate_per_min:null,previous7_daily:null,ratio:null,high_writer:false};
  card.render();
  assert.match(card.shadowRoot.textContent,/sensor\.learning/);
  assert.match(card.shadowRoot.textContent,/Learning/);
  card._tab='settings'; card.render(); const select=card.shadowRoot.querySelector('#language');
  select.value='sl'; select.dispatchEvent(new dom.window.Event('change'));
  assert.equal(card._language,'sl'); assert.match(card.shadowRoot.textContent,/Veljavne možnosti/);
  dom.window.close();
});

test('Top Writers dropdown preserves limits and pages at most 50 monitor-history rows',async()=>{
  const enabled=summary({
    writer_diagnostics_enabled:true,
    writers:{status:'ok',intervals_verified:32,intervals_required:32,entities:[]},
    effective_options:{...summary().effective_options,enable_recorder_writer_diagnostics:true},
  });
  const ranking=(limit,page)=>{
    const total=limit,totalPages=Math.ceil(total/50),start=(page-1)*50;
    return {status:'ok',selected_limit:limit,selected_entities_count:total,
      ranked_entities_available:600,page,page_size:50,total_pages:totalPages,
      entities:Array.from({length:Math.min(50,total-start)},(_,offset)=>{
        const index=start+offset;
        return {entity_id:`sensor.writer_${String(index).padStart(3,'0')}`,
          integration:index%2?'beta':'alpha',rows_6h:1000-index,
          rows_24h:4000-index*4,share_percent:1,rate_per_min:1,
          previous7_daily:3500-index*3.5,ratio:1.1,high_writer:false};
      }),
      grouped_by_integration:[
        {integration:'alpha',rows_6h:123,rows_24h:456,entities_in_selected:Math.ceil(limit/2)},
        {integration:'beta',rows_6h:122,rows_24h:455,entities_in_selected:Math.floor(limit/2)},
      ]};
  };
  const {dom,card}=cardWith(enabled),calls=[];
  card._hass={callWS:async message=>{calls.push(message);assert.equal(message.type,'ha_storage_monitor/writers');return ranking(message.limit,message.page);}};
  card._writerData=ranking(20,1);card._tab='writers';card.render();
  const limitSelect=card.shadowRoot.querySelector('#writer-limit');
  assert.deepEqual([...limitSelect.options].map(option=>option.value),['20','50','100','500']);
  assert.equal(limitSelect.value,'20');

  limitSelect.value='500';limitSelect.dispatchEvent(new dom.window.Event('change'));
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(calls.at(-1).type,'ha_storage_monitor/writers');
  assert.equal(calls.at(-1).limit,500);assert.equal(calls.at(-1).page,1);
  assert.equal(card.shadowRoot.querySelectorAll('tbody')[1].querySelectorAll('tr').length,50);
  assert.match(card.shadowRoot.textContent,/Partial integration totals derived from selected Top 500/);
  assert.match(card.shadowRoot.textContent,/Page 1\/10/);

  card._tab='overview';card.render();card._tab='writers';card.render();
  assert.equal(card.shadowRoot.querySelector('#writer-limit').value,'500','selection survives tab navigation');
  card.shadowRoot.querySelector('[data-writer-page="2"]').click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(calls.at(-1).type,'ha_storage_monitor/writers');
  assert.equal(calls.at(-1).limit,500);assert.equal(calls.at(-1).page,2);
  assert.match(card.shadowRoot.textContent,/sensor\.writer_050/);
  assert.equal(card.shadowRoot.querySelectorAll('tbody')[1].querySelectorAll('tr').length,50);

  card._language='sl';card.render();
  assert.match(card.shadowRoot.textContent,/Razvrstitev entitet/);
  assert.match(card.shadowRoot.textContent,/Delni seštevki integracij iz izbranih prvih 500/);
  assert.ok(calls.every(call=>call.type==='ha_storage_monitor/writers'),
    'changing ranking must only read monitor-owned writer history');
  dom.window.close();
});

test('existing SQL sensor cross-check is read-only and never substitutes zero',()=>{
  const {dom,card}=cardWith();
  card._tab='databases';
  card._hass={states:{}};
  card.render();
  assert.match(card.shadowRoot.textContent,/sensor\.ha_recorder_database_size was not found/);
  assert.match(card.shadowRoot.textContent,/No zero value is assumed/);
  card._hass.states['sensor.ha_recorder_database_size']={
    state:'unavailable',attributes:{unit_of_measurement:'MiB'},
  };
  card.render();
  assert.match(card.shadowRoot.textContent,/Comparison unavailable/);
  assert.doesNotMatch(card.shadowRoot.textContent,/0\.00 MiB/);
  card._hass.states['sensor.ha_recorder_database_size']={
    state:'2045',attributes:{unit_of_measurement:'MiB'},
  };
  card.render();
  assert.match(card.shadowRoot.textContent,/2045 MiB/);
  assert.match(card.shadowRoot.textContent,/Recorder main minus SQL sensor/);
  const websocketCalls=[];
  card._hass.callWS=async message=>{websocketCalls.push(message);return {samples:[]};};
  card.render();
  assert.equal(websocketCalls.length,0,'rendering the comparison must not poll or query a sensor');
  card.setConfig({language:'en',show_sql_cross_check:false});
  card.render();
  assert.doesNotMatch(card.shadowRoot.textContent,/Existing SQL sensor cross-check/);
  dom.window.close();
});

test('Influx card and Alerts distinguish cache age from probe failures',()=>{
  const data=summary({
    alerts:{disk_low:{active:0},'stale:influx_addon':{active:0,acknowledged_until:0}},
    influx_health:{status:'cached',reason:'Waiting for next eligible Supervisor probe',alert_active:false,last_success_at:NOW-6*3600,last_attempt_at:NOW-6*3600,last_attempt_status:'success',last_attempt_detail:'verified',measurement_age_seconds:6*3600,next_eligible_at:NOW+3600},
  });
  const influx=data.samples.find(row=>row.source==='influx_addon');
  influx.size=7*GIB;influx.status='cached';influx.measured_at=NOW-6*3600;
  const {dom,card}=cardWith(data);
  card._tab='databases';card.render();
  data.influx_health={...data.influx_health,status:'fresh',reason:'Fresh Supervisor measurement'};
  influx.status='ok';card.render();
  assert.match(card.shadowRoot.textContent,/Fresh measurement/);
  data.influx_health={...data.influx_health,status:'cached',reason:'Waiting for next eligible Supervisor probe'};
  influx.status='cached';card.render();
  assert.match(card.shadowRoot.textContent,/Cached measurement/);
  assert.match(card.shadowRoot.textContent,/Waiting for next eligible Supervisor probe/);
  data.influx_health={...data.influx_health,status:'stale_waiting',reason:'Stale historical measurement; waiting for next eligible Supervisor probe',measurement_age_seconds:8*3600};
  influx.status='stale';card.render();
  assert.match(card.shadowRoot.textContent,/Stale — awaiting eligible probe/);
  data.influx_health={...data.influx_health,status:'probe_in_progress',reason:'An eligible Supervisor probe is currently in progress',last_attempt_status:'in_progress'};
  card.render();assert.match(card.shadowRoot.textContent,/Eligible probe in progress/);
  data.influx_health={...data.influx_health,status:'probe_interrupted',reason:'Last eligible Supervisor probe was interrupted; waiting for the next scheduled opportunity',last_attempt_status:'interrupted',last_attempt_detail:'Supervisor probe was interrupted before an outcome was recorded'};
  card.render();assert.match(card.shadowRoot.textContent,/Eligible probe interrupted/);
  data.influx_health={...data.influx_health,status:'probe_failed',reason:'Last eligible Supervisor probe failed; waiting for the next scheduled opportunity',last_attempt_status:'failure',last_attempt_detail:'Supervisor HTTP 403'};
  card.render();assert.match(card.shadowRoot.textContent,/Eligible probe failed/);
  data.influx_health={...data.influx_health,status:'prolonged_unavailable',reason:'2 consecutive eligible Supervisor probes failed',alert_active:true};
  data.alerts['stale:influx_addon'].active=1;
  card._tab='alerts';card.render();
  assert.match(card.shadowRoot.textContent,/Prolonged measurement unavailability/);
  assert.match(card.shadowRoot.textContent,/This is measurement health, not proof that the InfluxDB service stopped/);
  assert.match(card.shadowRoot.textContent,/Supervisor HTTP 403/);
  data.influx_health={...data.influx_health,status:'unavailable',reason:'No verified InfluxDB add-on size measurement is available',alert_active:false,last_success_at:null,measurement_age_seconds:null};
  data.alerts['stale:influx_addon'].active=0;
  influx.size=null;influx.status='unavailable';card._tab='databases';card.render();
  assert.match(card.shadowRoot.textContent,/Measurement unavailable/);
  card._language='sl';card.render();
  assert.match(card.shadowRoot.textContent,/Meritev ni na voljo/);
  data.influx_health={...data.influx_health,status:'probe_interrupted',reason:'prekinjeno',last_attempt_status:'interrupted'};
  card.render();
  assert.match(card.shadowRoot.textContent,/Upravičeni preizkus je bil prekinjen/);
  data.influx_health={...data.influx_health,status:'prolonged_unavailable',reason:'2 consecutive eligible Supervisor probes failed',alert_active:true,last_success_at:NOW-8*3600,measurement_age_seconds:8*3600};
  data.alerts['stale:influx_addon'].active=1;card._tab='alerts';card.render();
  assert.match(card.shadowRoot.textContent,/Dolgotrajna nedostopnost meritve/);
  assert.match(card.shadowRoot.textContent,/To je stanje meritve/);
  dom.window.close();
});
