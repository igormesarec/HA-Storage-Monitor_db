/* Storage Monitor v0.2: local, read-only dashboard; no external resources. */
class HAStorageMonitorCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: 'open'});
    this._tab = 'overview';
    this._days = 7;
    this._selected = 'recorder_total';
    this._language = 'en';
    this._data = null;
    this._points = [];
  }
  setConfig(config) { this._config = config || {}; this._language = config?.language === 'sl' ? 'sl' : 'en'; }
  set hass(hass) { this._hass = hass; if (!this._started) { this._started = true; this.reload(); } }
  connectedCallback() { if (!this._timer) this._timer = setInterval(() => this._hass && this.reload(), 300000); }
  disconnectedCallback() { if (this._timer) clearInterval(this._timer); this._timer = null; }
  getCardSize() { return 9; }
  t(en, sl) { return this._language === 'sl' ? sl : en; }
  esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  size(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return this.t('Unavailable', 'Ni na voljo');
    const n = Number(value), unit = n >= 2**30 ? 'GiB' : n >= 2**20 ? 'MiB' : n >= 1024 ? 'KiB' : 'B';
    const divisor = unit === 'GiB' ? 2**30 : unit === 'MiB' ? 2**20 : unit === 'KiB' ? 1024 : 1;
    return `${(n / divisor).toFixed(unit === 'B' ? 0 : 2)} ${unit}`;
  }
  signed(value) { return value == null ? this.t('Unavailable', 'Ni na voljo') : `${value >= 0 ? '+' : '−'}${this.size(Math.abs(value))}`; }
  when(epoch) { return epoch ? new Date(epoch * 1000).toLocaleString(this._language === 'sl' ? 'sl-SI' : 'en-GB') : this.t('Unavailable', 'Ni na voljo'); }
  label(key) {
    return ({recorder_main:'Recorder main SQLite',recorder_wal:'Recorder WAL',recorder_shm:'Recorder SHM',recorder_total:'Recorder total',influx_addon:'InfluxDB add-on total',database_total:'Other local SQLite total',disk_free:'Available /config space',disk_total:'Total /config filesystem',disk_used:'Used /config filesystem'})[key] || (key.startsWith('database:') ? key.slice(9) : key);
  }
  samples() { return Object.fromEntries((this._data?.samples || []).map(row => [row.source, row])); }
  async reload() {
    try { this._data = await this._hass.callWS({type:'ha_storage_monitor/summary'}); this._error = null; await this.loadHistory(); }
    catch (error) { this._error = error?.message || String(error); this.render(); }
  }
  async loadHistory() {
    try { const result = await this._hass.callWS({type:'ha_storage_monitor/history', source:this._selected, days:this._days}); this._points = result.samples || []; }
    catch (_) { this._points = []; }
    this.render();
  }
  styles() { return `<style>
    :host{display:block;color:var(--primary-text-color)}*{box-sizing:border-box}.wrap{font-family:var(--paper-font-body1_-_font-family,system-ui);padding:16px}h2,h3{margin:0 0 10px}
    nav,.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}nav{overflow-x:auto;margin-bottom:14px;flex-wrap:nowrap;padding-bottom:3px}
    button,select,a.action{padding:8px 12px;border-radius:9px;border:1px solid var(--divider-color,#bbb);background:var(--card-background-color,#fff);color:var(--primary-text-color);font:inherit;cursor:pointer;text-decoration:none;white-space:nowrap}button[aria-current="true"]{border:2px solid var(--primary-color,#03a9f4);font-weight:700}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:10px}.wide{grid-column:1/-1}.box{padding:13px;border:1px solid var(--divider-color,#ccc);border-radius:12px;background:var(--card-background-color,#fff);min-width:0}.title{font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.big{font-size:clamp(19px,3vw,27px);font-weight:720;margin:5px 0}
    .muted{font-size:12px;color:var(--secondary-text-color,#666);line-height:1.5}.badge{display:inline-block;padding:2px 7px;border-radius:10px;background:var(--secondary-background-color,#eee);font-size:12px}.alert{color:var(--error-color,#d33);font-weight:700}.okay{color:var(--success-color,#38905e);font-weight:650}.warning{color:var(--warning-color,#ca8500);font-weight:650}.help{border-left:3px solid var(--primary-color,#03a9f4);padding:9px 11px;margin:10px 0;background:var(--secondary-background-color,transparent);line-height:1.45}
    .table-scroll{overflow:auto}.table{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}th,td{text-align:left;border-bottom:1px solid var(--divider-color,#ddd);padding:8px}th{color:var(--secondary-text-color,#666)}svg{display:block;width:100%;height:auto;max-height:310px}.chart-meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:10px 0}.metric{padding:8px;border-radius:8px;background:var(--secondary-background-color,transparent)}.metric b{display:block}.section{margin-top:15px}
    @media(max-width:520px){.wrap{padding:10px}.box{padding:10px}.grid{grid-template-columns:1fr}nav{margin-inline:-4px}.table{font-size:12px}}
  </style>`; }
  render() {
    const tabs = [['overview',this.t('Overview','Pregled')],['databases',this.t('Databases','Podatkovne baze')],['charts',this.t('Charts','Grafi')],['writers',this.t('Top Writers','Največ zapisov')],['disk',this.t('Disk Usage','Poraba diska')],['alerts',this.t('Alerts','Opozorila')],['settings',this.t('Settings','Nastavitve')]];
    const views = {overview:()=>this.overview(),databases:()=>this.databases(),charts:()=>this.charts(),writers:()=>this.writers(),disk:()=>this.disk(),alerts:()=>this.alerts(),settings:()=>this.settings()};
    const body = this._error ? `<p class="alert">${this.esc(this._error)}</p>` : !this._data ? this.t('Loading…','Nalaganje…') : views[this._tab]();
    this.shadowRoot.innerHTML = `${this.styles()}<ha-card><div class="wrap"><h2>${this.t('Home Assistant Storage Monitor','Nadzor prostora podatkovnih baz')}</h2><nav>${tabs.map(([key,name]) => `<button data-tab="${key}" aria-current="${key === this._tab}">${name}</button>`).join('')}</nav>${body}</div></ha-card>`;
    this.shadowRoot.querySelectorAll('[data-tab]').forEach(el => el.onclick = () => { this._tab = el.dataset.tab; this.render(); });
    this.shadowRoot.querySelectorAll('[data-days]').forEach(el => el.onclick = () => { this._days = Number(el.dataset.days); this.loadHistory(); });
    const source = this.shadowRoot.querySelector('#source'); if (source) source.onchange = event => { this._selected = event.target.value; this.loadHistory(); };
    const refresh = this.shadowRoot.querySelector('#refresh'); if (refresh) refresh.onclick = () => this.reload();
    const language = this.shadowRoot.querySelector('#language'); if (language) language.onchange = event => { this._language = event.target.value; this.render(); };
    this.shadowRoot.querySelectorAll('[data-ack]').forEach(el => el.onclick = async () => { const hours = Number(el.dataset.hours || 24); if (!window.confirm(this.t('Mute this alert?','Utišam to opozorilo?'))) return; await this._hass.callWS({type:'ha_storage_monitor/acknowledge',source:el.dataset.ack,hours}); await this.reload(); });
  }
  sourceCard(key) {
    const row = this.samples()[key]; if (!row) return '';
    const change = this._data.changes?.[key] || {};
    const deltas = [['6h',change.delta6h],['24h',change.delta24h]].filter(([,value]) => value != null).map(([period,value]) => `${period} ${this.signed(value)}`).join(' · ');
    const state = row.status === 'ok' ? 'okay' : row.status === 'cached' ? 'warning' : 'alert';
    return `<article class="box" data-source="${this.esc(key)}"><div class="title" title="${this.esc(this.label(key))}">${this.esc(this.label(key))}</div><div class="big">${this.size(row.size)}</div>${deltas?`<div>${deltas}</div>`:''}<div class="muted"><span class="${state}">${this.esc(row.status)}</span> · ${this.esc(row.method)}<br>${this.t('Last successful','Zadnja uspešna')}: ${this.when(row.measured_at)}${row.detail?`<br>${this.esc(row.detail)}`:''}</div></article>`;
  }
  overview() {
    const samples=this.samples(), active=Object.values(this._data.alerts||{}).filter(alert => alert?.active).length;
    const writer=this._data.writer_diagnostics_enabled?this.t('Enabled: bounded six-hour reads','Omogočeno: omejene šesturne poizvedbe'):this.t('Recorder writer diagnostics disabled','Diagnostika zapisovalcev Recorderja je izklopljena');
    const main=['recorder_total','influx_addon','database_total','disk_free'].filter(key => samples[key]);
    return `<div class="row"><span class="badge ${active?'alert':'okay'}">${active?`${active} ${this.t('active alerts','aktivnih opozoril')}`:this.t('No active alerts','Ni aktivnih opozoril')}</span><span class="badge">${this.esc(writer)}</span><button id="refresh">${this.t('Refresh','Osveži')}</button></div><div class="grid section">${main.map(key=>this.sourceCard(key)).join('')}<section class="box wide"><h3>${this.t('Storage trend','Trend porabe')}</h3>${this.graph()}</section><section class="box"><h3>${this.t('Growth status','Stanje rasti')}</h3>${this.growthSummary()}</section><section class="box"><h3>${this.t('Top writers','Največ zapisov')}</h3>${this.writerSummary()}</section></div>`;
  }
  databases() {
    const samples=this.samples();
    const recorder=['recorder_total','recorder_main','recorder_wal','recorder_shm'].filter(key=>samples[key]);
    const other=Object.keys(samples).filter(key=>key==='database_total'||key.startsWith('database:'));
    return `<div class="help">${this.t('Recorder, History, Logbook and long-term statistics share one Recorder database; they are not separate file sizes. Values are apparent file bytes.','Recorder, Zgodovina, Dnevnik in dolgoročna statistika uporabljajo isto bazo; niso ločene velikosti. Prikazane so navidezne velikosti datotek.')}</div><h3>Recorder SQLite</h3><div class="grid">${recorder.map(key=>this.sourceCard(key)).join('')}</div><h3 class="section">InfluxDB</h3><div class="grid">${samples.influx_addon?this.sourceCard('influx_addon'):`<div class="box">${this.t('Unavailable','Ni na voljo')}</div>`}</div><h3 class="section">${this.t('Discovered local SQLite','Odkrite lokalne SQLite baze')}</h3><div class="grid">${other.length?other.map(key=>this.sourceCard(key)).join(''):`<div class="box">${this.t('No verified result','Ni potrjenega rezultata')}</div>`}</div><p class="muted">${this.esc(this._data.scope)}</p>`;
  }
  historyControls() {
    const options=Object.keys(this.samples()).filter(key=>!key.startsWith('disk_')).map(key=>`<option value="${this.esc(key)}" ${key===this._selected?'selected':''}>${this.esc(this.label(key))}</option>`).join('');
    return `<div class="row"><label>${this.t('Source','Vir')} <select id="source">${options}</select></label>${[1,7,30,90].map(days=>`<button data-days="${days}" aria-current="${days===this._days}">${days}d</button>`).join('')}</div>`;
  }
  _chartModel() {
    const measured=(this._points||[]).filter(row=>row.status==='ok'&&row.size!=null).sort((a,b)=>a.hour-b.hour);
    if(!measured.length)return {measured,segments:[],missing:0,cached:(this._points||[]).filter(row=>row.status==='cached').length};
    const values=measured.map(row=>Number(row.size)), min=Math.min(...values), max=Math.max(...values), net=values.at(-1)-values[0], range=max-min;
    const threshold=this._selected==='influx_addon'?7*3600:2*3600, segments=[]; let segment=[],missing=0;
    for(const row of measured){if(segment.length&&row.hour-segment.at(-1).hour>threshold){segments.push(segment);segment=[];missing++;}segment.push(row);} if(segment.length)segments.push(segment);
    const padding=range===0?Math.max(Math.abs(max)*.0005,1024):Math.max(range*.12,1);
    return {measured,segments,missing,cached:(this._points||[]).filter(row=>row.status==='cached').length,min,max,net,range,low:min-padding,high:max+padding};
  }
  graph() {
    const model=this._chartModel();
    if(model.measured.length<2)return `<p>${this.t('Not enough independently measured points. Cached or unavailable values are not plotted.','Ni dovolj neodvisno izmerjenih točk. Predpomnjene in nedostopne vrednosti niso narisane.')}</p>`;
    const start=model.measured[0].hour,end=model.measured.at(-1).hour,dx=Math.max(end-start,1),dy=Math.max(model.high-model.low,1),xy=row=>[48+(row.hour-start)/dx*824,238-(row.size-model.low)/dy*188];
    const paths=model.segments.map(segment=>`<polyline points="${segment.map(row=>xy(row).map(n=>n.toFixed(1)).join(',')).join(' ')}" fill="none" stroke="var(--primary-color,#03a9f4)" stroke-width="2.5"/>`).join('');
    const shrinks=model.measured.slice(1).map((row,index)=>row.size<model.measured[index].size?xy(row):null).filter(Boolean).map(([x,y])=>`<circle cx="${x}" cy="${y}" r="4" fill="var(--warning-color,#ca8500)"><title>Measured decrease</title></circle>`).join('');
    const precision=model.range<1024**2?3:2,mib=value=>(value/1024**2).toFixed(precision);
    const note=model.range===0?this.t('No measured change; a small neutral axis margin is shown.','Ni izmerjene spremembe; prikazan je majhen nevtralen rob osi.'):this.t('Axis adapts to measured data; use the exact range and net values below.','Os se prilagaja meritvam; upoštevajte točen razpon in neto spremembo spodaj.');
    return `<svg viewBox="0 0 900 285" role="img" aria-label="${this.t('Measured storage history; gaps are not interpolated','Izmerjena zgodovina; vrzeli niso interpolirane')}"><line x1="48" y1="238" x2="872" y2="238"/><line x1="48" y1="42" x2="48" y2="238"/><text x="50" y="30">${this.esc(this.size(model.high))}</text><text x="50" y="258">${this.esc(this.size(model.low))}</text><text x="48" y="280">${this.esc(this.when(start))}</text><text x="872" y="280" text-anchor="end">${this.esc(this.when(end))}</text>${paths}${shrinks}</svg><div class="chart-meta"><div class="metric"><span>Min</span><b>${mib(model.min)} MiB</b></div><div class="metric"><span>Max</span><b>${mib(model.max)} MiB</b></div><div class="metric"><span>${this.t('Range','Razpon')}</span><b>${mib(model.range)} MiB</b></div><div class="metric"><span>${this.t('Net change','Neto sprememba')}</span><b>${model.net>=0?'+':''}${mib(model.net)} MiB</b></div></div><div class="muted">${model.measured.length} ${this.t('measured samples','izmerjenih vzorcev')} · ${model.missing} ${this.t('visible gaps','vidnih vrzeli')} · ${model.cached} ${this.t('cached values excluded','predpomnjenih vrednosti izločenih')}<br>${note}</div>`;
  }
  charts(){return `${this.historyControls()}<section class="box section"><h3 title="${this.esc(this.label(this._selected))}">${this.esc(this.label(this._selected))}</h3>${this.graph()}</section>`;}
  growthSummary() {
    const entries=Object.entries(this._data.growth||{});
    if(!entries.length)return `<p>${this.t('Learning: genuine history is still accumulating.','Učenje: dejanska zgodovina se še zbira.')}</p>`;
    return entries.slice(0,5).map(([key,growth])=>`<div class="row"><span class="title" title="${this.esc(this.label(key))}">${this.esc(this.label(key))}</span><span class="badge ${growth.status==='alarm'?'alert':growth.status==='learning'?'warning':'okay'}">${this.esc(growth.status)}</span></div>`).join('');
  }
  writerSummary() {
    const writer=this._data.writers||{};
    return writer.status==='disabled'?`<p><strong>${this.t('Recorder writer diagnostics disabled','Diagnostika zapisovalcev Recorderja je izklopljena')}</strong></p><p class="muted">${this.t('No Recorder query is made and no missed interval is backfilled.','Recorder se ne poizveduje in zamujeni intervali se ne dopolnjujejo.')}</p>`:`<p>${this.t('Status','Stanje')}: <strong>${this.esc(writer.status||'learning')}</strong><br>${this.t('Verified intervals','Potrjeni intervali')}: ${this.esc(writer.intervals_verified??'—')}/${this.esc(writer.intervals_required??'—')}</p>`;
  }
  writers() {
    const writer=this._data.writers||{};
    if(writer.status==='disabled')return `<div class="help"><strong>${this.t('Recorder writer diagnostics disabled','Diagnostika zapisovalcev Recorderja je izklopljena')}</strong><br>${this.t('Size monitoring, disk alarms, Supervisor probes and independent history continue. No Recorder query is made and no missed interval is queried retroactively.','Meritve velikosti, alarmi prostora, poizvedbe Supervisorja in neodvisna zgodovina delujejo naprej. Recorder se ne poizveduje in zamujeni intervali se ne pridobivajo za nazaj.')}</div>`;
    const entities=(writer.entities||[]).map((row,index)=>`<tr><td>${index+1}</td><td title="${this.esc(row.entity_id)}">${this.esc(row.entity_id)}</td><td>${this.esc(row.integration||'unknown')}</td><td>${this.esc(row.rows_6h??'—')}</td><td>${this.esc(row.rows_24h??'—')}</td><td>${row.share_percent==null?'—':this.esc(row.share_percent)+'%'}</td></tr>`).join('');
    const integrations=(writer.top20_grouped_by_integration||[]).map(row=>`<tr><td>${this.esc(row.integration)}</td><td>${this.esc(row.rows_6h??'—')}</td><td>${this.esc(row.rows_24h??'—')}</td><td>${this.esc(row.entities_in_top20)}</td></tr>`).join('');
    return `<div class="help">${this.t('Recorder state row counts, not attributable storage bytes. Only complete bounded six-hour intervals are shown; missing intervals stay missing.','Število vrstic stanj Recorderja, ne pripisljivi bajti. Prikazani so le zaključeni omejeni šesturni intervali; manjkajoči ostanejo manjkajoči.')}</div><h3>${this.t('Integrations in top 20','Integracije med prvimi 20')}</h3><div class="table-scroll"><table class="table"><thead><tr><th>Integration</th><th>6h</th><th>${this.t('Local day','Lokalni dan')}</th><th>Entities</th></tr></thead><tbody>${integrations}</tbody></table></div><h3 class="section">${this.t('Entities','Entitete')}</h3><div class="table-scroll"><table class="table"><thead><tr><th>#</th><th>Entity ID</th><th>Integration</th><th>6h</th><th>${this.t('Local day','Lokalni dan')}</th><th>Share</th></tr></thead><tbody>${entities}</tbody></table></div>`;
  }
  disk() {
    const samples=this.samples(), total=samples.disk_total?.size, used=samples.disk_used?.size, available=samples.disk_free?.size;
    const difference=[total,used,available].every(value=>value!=null)?total-used-available:null;
    const configured=(this._data.effective_options?.low_disk_gib??15)*1024**3, recorder=samples.recorder_total?.size, effective=recorder==null?configured:Math.max(configured,recorder);
    return `<div class="grid">${['disk_total','disk_used','disk_free'].filter(key=>samples[key]).map(key=>this.sourceCard(key)).join('')}<article class="box"><div class="title">${this.t('Accounting difference','Razlika obračuna')}</div><div class="big">${difference!=null&&difference>=0?this.size(difference):this.t('Unavailable','Ni na voljo')}</div><div class="muted">${this.t('On Linux, total − used − available can represent blocks reserved or unavailable to this process. It is derived for explanation, not a separately measured storage category.','V Linuxu lahko total − porabljeno − razpoložljivo predstavlja bloke, rezervirane ali nedostopne temu procesu. To je izračun za pojasnilo, ne ločeno izmerjena kategorija.')}</div></article></div><div class="help"><strong>${this.t('Disk-low threshold','Prag pomanjkanja prostora')}: ${this.size(effective)}</strong><br>${this.t('The active threshold is the larger of the configured value and the current Recorder footprint.','Aktivni prag je večja vrednost med nastavitvijo in trenutno velikostjo Recorderja.')}</div>`;
  }
  alertRow(key,alert) {
    const name=key.startsWith('stale:')?`${this.t('Stale','Zastarelo')}: ${this.label(key.slice(6))}`:this.label(key);
    return `<article class="box"><div class="title" title="${this.esc(key)}">${this.esc(name)}</div><div class="${alert?.active?'alert':'okay'}">${alert?.active?this.t('Active','Aktivno'):this.t('Clear','Brez alarma')}</div>${alert?.active?`<div class="row"><button data-ack="${this.esc(key)}" data-hours="24">${this.t('Mute 24h','Utišaj 24h')}</button><button data-ack="${this.esc(key)}" data-hours="168">${this.t('Mute 7d','Utišaj 7d')}</button></div>`:''}${alert?.acknowledged_until>this._data.now?`<div class="muted">${this.t('Acknowledged until','Potrjeno do')} ${this.when(alert.acknowledged_until)}</div>`:''}</article>`;
  }
  alerts() {
    const alerts=this._data.alerts||{}, growth=Object.entries(this._data.growth||{});
    return `<div class="help">${this.t('Growth alarms require eight elapsed UTC days of genuine data and trigger only when positive 24h growth is strictly greater than 1.05 × the positive prior-seven-day daily average. Exactly 5% does not alert; shrink resets the baseline.','Alarm rasti zahteva osem pretečenih UTC dni dejanskih podatkov in se sproži le, ko je pozitivna 24-urna rast strogo večja od 1,05 × pozitivnega dnevnega povprečja prejšnjih sedmih dni. Točno 5 % ne sproži alarma; zmanjšanje ponastavi osnovo.')}</div><div class="grid">${Object.entries(alerts).filter(([key])=>key==='disk_low'||key.startsWith('stale:')).map(([key,value])=>this.alertRow(key,value)).join('')}</div><h3 class="section">${this.t('Growth evaluation','Ocena rasti')}</h3><div class="table-scroll"><table class="table"><thead><tr><th>${this.t('Source','Vir')}</th><th>${this.t('Status','Stanje')}</th><th>24h</th><th>${this.t('Prior 7d/day','Prejšnjih 7d/dan')}</th><th>${this.t('Deviation','Odstopanje')}</th></tr></thead><tbody>${growth.map(([key,item])=>`<tr><td>${this.esc(this.label(key))}</td><td class="${item.status==='alarm'?'alert':item.status==='learning'?'warning':'okay'}">${this.esc(item.status)}</td><td>${this.signed(item.delta24)}</td><td>${this.signed(item.baseline_daily)}</td><td>${item.excess_ratio==null?'—':(item.excess_ratio*100).toFixed(1)+'%'}</td></tr>`).join('')}</tbody></table></div>`;
  }
  settings() {
    const options=this._data.effective_options||{}, yes=value=>value?this.t('Yes','Da'):this.t('No','Ne');
    const access=Object.entries(this.samples()).map(([key,row])=>`<tr><td title="${this.esc(this.label(key))}">${this.esc(this.label(key))}</td><td>${this.esc(row.status)}</td><td>${this.esc(row.method)}</td></tr>`).join('');
    return `<div class="help">${this.t('These are effective, non-secret options already held by the integration. Change them through Home Assistant Devices & Services; this card does not edit configuration.','To so veljavne, neskrivne možnosti, ki jih integracija že uporablja. Spremenite jih prek Naprav in storitev; kartica ne spreminja nastavitev.')}</div><div class="grid"><article class="box"><h3>${this.t('Effective options','Veljavne možnosti')}</h3><p>${this.t('Disk-low','Malo prostora')}: <b>${this.esc(options.low_disk_gib??'—')} GiB</b><br>${this.t('Minimum excess','Najmanjši presežek')}: <b>${this.esc(options.min_excess_mib??'—')} MiB</b><br>${this.t('History retention','Hranjenje zgodovine')}: <b>${this.esc(options.retention_days??'—')} d</b><br>${this.t('Repeat notifications','Ponavljanje obvestil')}: <b>${yes(options.repeat_notifications)}</b><br>${this.t('Writer diagnostics','Diagnostika zapisovalcev')}: <b>${yes(options.enable_recorder_writer_diagnostics)}</b></p><a class="action" href="/config/integrations/dashboard">${this.t('Open Devices & Services','Odpri Naprave in storitve')}</a></article><article class="box"><h3>${this.t('Dashboard','Nadzorna plošča')}</h3><label>${this.t('Language','Jezik')} <select id="language"><option value="en" ${this._language==='en'?'selected':''}>English</option><option value="sl" ${this._language==='sl'?'selected':''}>Slovenščina</option></select></label><p class="muted">${this.t('Keep the existing user-created Lovelace dashboard and JavaScript resource. The integration does not create a duplicate sidebar entry.','Ohranite obstoječo uporabniško nadzorno ploščo Lovelace in vir JavaScript. Integracija ne ustvari podvojenega vnosa v stranski vrstici.')}</p></article></div><h3 class="section">${this.t('Current source access','Trenutni dostop do virov')}</h3><div class="table-scroll"><table class="table"><thead><tr><th>${this.t('Source','Vir')}</th><th>${this.t('Status','Stanje')}</th><th>${this.t('Method','Metoda')}</th></tr></thead><tbody>${access}</tbody></table></div><p class="muted">${this.esc(this._data.scope)}</p>`;
  }
}
customElements.define('ha-storage-monitor-card', HAStorageMonitorCard);
window.customCards = window.customCards || [];
window.customCards.push({type:'ha-storage-monitor-card',name:'Storage Monitor',description:'Read-only storage growth, history, disk accounting and alerts'});
