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

test('chart leaves missing-hour gaps and treats DST by elapsed UTC time',()=>{
  const {dom,card}=cardWith();
  card._points=[
    {hour:NOW-5*3600,size:100*MIB,status:'ok'},
    {hour:NOW-4*3600,size:101*MIB,status:'ok'},
    {hour:NOW-3600,size:99*MIB,status:'ok'},
    {hour:NOW,size:100*MIB,status:'ok'},
  ];
  let model=card._chartModel();
  assert.equal(model.segments.length,2); assert.equal(model.missing,1); assert.equal(model.net,0);
  card._selected='influx_addon';
  const fallback=Date.parse('2026-10-25T00:00:00Z')/1000;
  card._points=[
    {hour:fallback,size:7*GIB,status:'ok'},
    {hour:fallback+3600,size:7*GIB+MIB,status:'cached'},
    {hour:fallback+6*3600,size:7*GIB+2*MIB,status:'ok'},
  ];
  model=card._chartModel();
  assert.equal(model.segments.length,1,'six elapsed UTC hours across a DST transition stay connected');
  assert.equal(model.cached,1); assert.equal(model.measured.length,2);
  assert.match(card.graph(),/cached values excluded/);
  const spring=Date.parse('2026-03-29T00:00:00Z')/1000;
  card._selected='recorder_total';
  card._points=[0,1,2].map(index=>({hour:spring+index*3600,size:100*MIB+index,status:'ok'}));
  assert.equal(card._chartModel().segments.length,1,'spring-forward UTC hours stay continuous');
  dom.window.close();
});

test('enabled writer data and language switching remain functional',()=>{
  const enabled=summary({
    writer_diagnostics_enabled:true,
    writers:{status:'learning',intervals_verified:1,intervals_required:32,entities:[{entity_id:'sensor.test',integration:'demo',rows_6h:12,rows_24h:null,share_percent:50}],top20_grouped_by_integration:[{integration:'demo',rows_6h:12,rows_24h:null,entities_in_top20:1}]},
    effective_options:{...summary().effective_options,enable_recorder_writer_diagnostics:true},
  });
  const {dom,card}=cardWith(enabled);
  card._tab='writers'; card.render(); assert.match(card.shadowRoot.textContent,/sensor\.test/);
  card._tab='settings'; card.render(); const select=card.shadowRoot.querySelector('#language');
  select.value='sl'; select.dispatchEvent(new dom.window.Event('change'));
  assert.equal(card._language,'sl'); assert.match(card.shadowRoot.textContent,/Veljavne možnosti/);
  dom.window.close();
});
