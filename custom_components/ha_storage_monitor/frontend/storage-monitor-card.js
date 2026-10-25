/* Storage Monitor beta: no CDN, no external requests, no production actions.
   All data is obtained from admin-only Home Assistant WebSocket commands. */
class HAStorageMonitorCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.tab = 'overview'; this.days = 7; this.selected = 'recorder_total';
    this.lang = 'en'; this.data = null; this.points = [];
  }
  setConfig(config) {
    this.config = config || {};
    this.lang = config.language === 'sl' ? 'sl' : 'en';
  }
  set hass(hass) {
    this._hass = hass;
    if (!this.started) { this.started = true; this.reload(); }
  }
  connectedCallback() { this.timer = setInterval(() => { if (this._hass) this.reload(); }, 300000); }
  disconnectedCallback() { if (this.timer) clearInterval(this.timer); }
  getCardSize() { return 7; }
  t(en, sl) { return this.lang === 'sl' ? sl : en; }
  esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  size(bytes) {
    if (bytes === null || bytes === undefined || Number.isNaN(Number(bytes))) return this.t('Unavailable','Ni na voljo');
    const n = Number(bytes);
    const unit = n >= 2**30 ? 'GiB' : n >= 2**20 ? 'MiB' : n >= 1024 ? 'KiB' : 'B';
    const divisor = unit==='GiB' ? 2**30 : unit==='MiB' ? 2**20 : unit==='KiB' ? 1024 : 1;
    return (n/divisor).toFixed(unit==='B'?0:2) + ' ' + unit;
  }
  signed(bytes) { return bytes==null?'n/a':(bytes>=0?'+':'-')+this.size(Math.abs(bytes)); }
  when(epoch) { return epoch ? new Date(epoch*1000).toLocaleString(this.lang==='sl'?'sl-SI':'en-GB'):'n/a'; }
  async reload() {
    try {
      this.data = await this._hass.callWS({type:'ha_storage_monitor/summary'});
      this.error = null;
      await this.loadHistory();
    } catch(e) { this.error = e.message || String(e); this.render(); }
  }
  async loadHistory() {
    try {
      const res = await this._hass.callWS({type:'ha_storage_monitor/history',source:this.selected,days:this.days});
      this.points = res.samples || [];
    } catch(_) { this.points = []; }
    this.render();
  }
  styles() { return `<style>
    :host{display:block;color:var(--primary-text-color)}
    .wrap{font-family:var(--paper-font-body1_-_font-family,system-ui);box-sizing:border-box}
    nav{display:flex;gap:8px;overflow-x:auto;margin-bottom:12px;flex-wrap:wrap}
    button,select{padding:9px 13px;border-radius:9px;border:1px solid var(--divider-color,#bbb);background:var(--card-background-color,#fff);color:var(--primary-text-color);cursor:pointer;font:inherit}
    button[aria-current="true"]{border:2px solid var(--primary-color,#03a9f4);font-weight:650}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,210px),1fr));gap:12px}
    .box{padding:14px;border:1px solid var(--divider-color,#ccc);border-radius:11px;background:var(--card-background-color,#fff);min-width:0}
    .big{font-size:clamp(18px,3vw,26px);font-weight:680;overflow-wrap:anywhere}
    .muted{font-size:12px;color:var(--secondary-text-color,#666);line-height:1.55}
    .alert{color:var(--error-color,#d33);font-weight:700}.okay{color:var(--success-color,#38905e)}
    .warning{color:var(--warning-color,#ca8500)}
    .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:10px 0}
    .table-scroll{overflow:auto}.table{width:100%;border-collapse:collapse;font-size:13px;white-space:nowrap}
    th,td{text-align:left;border-bottom:1px solid var(--divider-color,#ddd);padding:9px 8px}
    th{color:var(--secondary-text-color,#666)}
    svg{display:block;width:100%;height:auto;max-height:300px}
    .help{border-left:3px solid var(--primary-color,#03a9f4);padding:10px 12px;margin:12px 0;background:var(--secondary-background-color,transparent)}
    @media(max-width:380px){.box{padding:9px}.grid{grid-template-columns:1fr}.table{font-size:12px}}
  </style>`; }
  render() {
    const d = this.data;
    const nav = [ ['overview',this.t('Overview','Pregled')],['growth',this.t('Growth & alarms','Rast in alarmi')],['writers',this.t('Top writers','Največ zapisov')],['settings',this.t('Settings / Access','Nastavitve / Dostop')] ];
    const body = this.error ? `<p class="alert">${this.esc(this.error)}</p>` : !d ? this.t('Loading...','Nalaganje...') :
      this.tab === 'overview' ? this.overview() : this.tab==='growth'?this.growth():this.tab==='writers'?this.writers():this.settings();
    this.shadowRoot.innerHTML = this.styles()+`<ha-card><div class="wrap" style="padding:16px"><h2 style="margin:0 0 12px">${this.t('Home Assistant Storage Monitor','Nadzor prostora podatkovnih baz')}</h2><nav>${nav.map(([k,v])=>`<button data-tab="${k}" aria-current="${k===this.tab}">${v}</button>`).join('')}</nav>${body}</div></ha-card>`;
    this.shadowRoot.querySelectorAll('[data-tab]').forEach(el => el.onclick=()=>{this.tab=el.dataset.tab;this.render();});
    this.shadowRoot.querySelectorAll('[data-days]').forEach(el => el.onclick=()=>{this.days=Number(el.dataset.days);this.loadHistory();});
    const sel = this.shadowRoot.querySelector('#source');
    if(sel) sel.onchange=e=>{this.selected=e.target.value;this.loadHistory();};
    this.shadowRoot.querySelectorAll('[data-ack]').forEach(el => el.onclick=async()=>{
      const hours = Number(el.dataset.hours || 24);
      if(!window.confirm(this.t(`Mute this alert for ${hours===168?'7 days':'24 hours'}?`,
        `Utišam to opozorilo za ${hours===168?'7 dni':'24 ur'}?`)))return;
      await this._hass.callWS({type:'ha_storage_monitor/acknowledge',source:el.dataset.ack,hours});
      await this.reload();
    });
    const refresh=this.shadowRoot.querySelector('#refresh');if(refresh)refresh.onclick=()=>this.reload();
    const lang=this.shadowRoot.querySelector('#lang');if(lang)lang.onchange=e=>{this.lang=e.target.value;this.render();};
  }
  samples(){return Object.fromEntries((this.data?.samples||[]).map(x=>[x.source,x]));}
  label(key){
    return ({recorder_main:'Recorder SQLite',recorder_wal:'Recorder WAL',recorder_shm:'Recorder SHM',recorder_total:'Recorder Total',influx_addon:'InfluxDB add-on total',database_total:'Other local SQLite DBs',disk_free:'Free /config disk',disk_total:'Total /config disk',disk_used:'Used /config disk'})[key]||key.replace(/^database:/,'SQLite: ');
  }
  overview(){
    const s=this.samples();
    const order=['disk_free','disk_used','disk_total','recorder_total','recorder_main','recorder_wal','recorder_shm','influx_addon','database_total',...Object.keys(s).filter(k=>k.startsWith('database:'))];
    const sqlEntity = this.config?.sql_entity || 'sensor.ha_recorder_database_size';
    const existing = this._hass.states?.[sqlEntity];
    const existingNote=existing?`<p class="muted">${this.t('Existing SQL sensor (cross-check; not recreated)','Obstoječi SQL senzor (kontrola; ni ustvarjen ponovno)')}: ${this.esc(sqlEntity)} = ${this.esc(existing.state)} ${this.esc(existing.attributes?.unit_of_measurement||'')}</p>`:
          `<p class="muted">${this.t('Original SQL sensor ID not confirmed; set sql_entity in card configuration if different.','ID obstoječega SQL senzorja ni potrjen; po potrebi nastavite sql_entity v kartici.')}</p>`;
    const cards=order.filter(k=>s[k]).map(k=>{
      const r=s[k], stale = !r.measured_at || this.data.now-r.measured_at > (k==='influx_addon'?7:2)*3600;
      const delta = this.data.changes?.[k] || {};
      const staleState = this.data.alerts?.['stale:'+k];
      const staleAlarm = staleState?.active;
      const muteButtons = staleAlarm ? `<div class="row"><button data-ack="${this.esc('stale:'+k)}" data-hours="24">${this.t('Mute 24h','Utišaj 24h')}</button><button data-ack="${this.esc('stale:'+k)}" data-hours="168">${this.t('Mute 7d','Utišaj 7d')}</button></div>${staleState.acknowledged_until>this.data.now?`<span class="muted">${this.t('Muted until','Utišano do')} ${this.when(staleState.acknowledged_until)}</span>`:''}` : '';
      return `<article class="box"><div>${this.esc(this.label(k))}</div><div class="big">${this.size(r.size)}</div><div class="muted">6h ${this.signed(delta.delta6h)} · 24h ${this.signed(delta.delta24h)}<br>${staleAlarm?'<b class="alert">MISSING MEASUREMENTS · </b>':''}${this.esc(r.status)}${stale?' · STALE':''} · ${this.esc(r.method)}<br>${this.when(r.measured_at)}</div>${muteButtons}</article>`;
    }).join('');
    const available=s.disk_total?.size && s.disk_free?.size!=null ? ((100*s.disk_free.size/s.disk_total.size).toFixed(1)+'%'):'n/a';
    return `<div class="help">${this.t('One SQLite database supplies Recorder, History, Logbook and statistics. These are NOT separate file sizes. The original SQL sensor is preserved.','Recorder, Zgodovina, Dnevnik in statistika so v eni podatkovni bazi. Obstoječi SQL senzor ostaja nespremenjen.')}</div>
       <div class="row"><strong>${this.t('Free space','Prosti prostor')}: ${available}</strong> <button id="refresh">${this.t('Refresh','Osveži')}</button></div>
       ${existingNote}<div class="grid">${cards}</div><div class="muted" style="margin-top:14px">${this.esc(this.data.scope)}</div>`;
  }
  graph(){
    const rows=this.points.filter(x=>x.status==='ok'&&x.size!=null);
    if(rows.length<2)return `<p>${this.t('Not enough independently measured points yet. Cached Influx values are not counted as measurements.','Še ni dovolj dejanskih meritev. Predpomnjene vrednosti Influx niso upoštevane.')}</p>`;
    const values=rows.map(x=>x.size), lo=Math.min(...values), hi=Math.max(...values), span=hi-lo || 1;
    const start=rows[0].hour, end=rows[rows.length-1].hour, dx=end-start || 1;
    let last=-1;const paths=[];let current=[];
    for(const x of rows){
      if(last>=0&&x.hour-last > (this.selected==='influx_addon'?7:2)*3600){if(current.length)paths.push(current);current=[];}
      const px=45+(x.hour-start)/dx*835, py=240-(x.size-lo)/span*190;
      current.push(`${px.toFixed(1)},${py.toFixed(1)}`);last=x.hour;
    }
    if(current.length)paths.push(current);
    const strokes=paths.map(p=>`<polyline points="${p.join(' ')}" fill="none" stroke="var(--primary-color,#03a9f4)" stroke-width="2.5"/>`).join('');
    return `<svg viewBox="0 0 900 290" aria-label="Hourly measured file size history" role="img">
       <line x1="45" y1="240" x2="880" y2="240" stroke="var(--secondary-text-color,#777)"/>
       <line x1="45" y1="45" x2="45" y2="240" stroke="var(--secondary-text-color,#777)"/>
       <text x="46" y="35" fill="var(--primary-text-color)">${this.esc(this.size(hi))}</text>
       <text x="46" y="260" fill="var(--primary-text-color)">${this.esc(this.size(lo))}</text>
       <text x="45" y="282" fill="var(--secondary-text-color)">${this.esc(this.when(start))}</text>
       <text x="880" y="282" text-anchor="end" fill="var(--secondary-text-color)">${this.esc(this.when(end))}</text>${strokes}
      </svg><div class="muted">${rows.length} ${this.t('measured samples','dejanskih meritev')} · ${this.t('gaps are not interpolated','vrzeli niso interpolirane')}</div>`;
  }
  growth(){
    const s=this.samples(), keys=Object.keys(this.data.growth||{});
    const options=Object.keys(s).map(k=>`<option ${k===this.selected?'selected':''} value="${this.esc(k)}">${this.esc(this.label(k))}</option>`).join('');
    const scores=keys.map(k=>{
      const g=this.data.growth[k], a=this.data.alerts[k];
      return `<tr><td>${this.esc(this.label(k))}</td><td class="${g.status==='alarm'?'alert':''}">${this.esc(g.status)}</td><td>${this.signed(g.delta24)}</td><td>${g.baseline_daily==null?'n/a':this.signed(g.baseline_daily)}</td><td>${g.excess_ratio==null?'n/a':(g.excess_ratio*100).toFixed(1)+'%'}</td><td>${a?.active?`<button data-ack="${this.esc(k)}">${this.t('Mute 24h','Utišaj 24h')}</button>`:' '}</td></tr>`;
    }).join('');
    const disk=this.data.alerts.disk_low?.active ? `<p class="alert">${this.t('CRITICAL: disk low','KRITIČNO: zmanjkuje prostora')}</p>`:'';
    const missing = Object.entries(this.data.alerts||{}).filter(([k,a])=>k.startsWith('stale:')&&a?.active);
    const missingControls=missing.length ? `<h4>${this.t('Missing measurements','Manjkajoče meritve')}</h4>`+missing.map(([k,a])=>
      `<div class="row"><strong class="alert">${this.esc(this.label(k.slice(6)))}</strong><button data-ack="${this.esc(k)}" data-hours="24">${this.t('Mute 24h','Utišaj 24h')}</button><button data-ack="${this.esc(k)}" data-hours="168">${this.t('Mute 7d','Utišaj 7d')}</button>${a.acknowledged_until>this.data.now?this.t('Muted until','Utišano do')+' '+this.when(a.acknowledged_until):''}</div>`).join('') : '';
    const prediction=keys.map(k=>{const g=this.data.growth[k],current=s[k];if(!g || !current || g.baseline_daily==null || g.baseline_daily<=0 || current.size==null) return '';
       return `<tr><td>${this.esc(this.label(k))}</td><td>${this.size(current.size+7*g.baseline_daily)}</td><td>${this.size(current.size+30*g.baseline_daily)}</td></tr>`;
    }).filter(Boolean).join('');
    return `<div class="help">${this.t('Alarm checks at 00:00, 06:00, 12:00, 18:00 Europe/Ljubljana. Last 24h positive growth must be STRICTLY greater than 1.05 × the previous seven days average daily growth. First 8 days are learning. Database decreases reset the baseline.','Preverjanje ob 00, 06, 12, 18. Rast zadnjih 24 ur mora biti STROGO večja od 1,05-kratnika povprečne dnevne rasti predhodnih sedmih dni. Prvih 8 dni je učenje.')}</div>${disk}${missingControls}
      <div class="table-scroll"><table class="table"><thead><tr><th>${this.t('Source','Vir')}</th><th>${this.t('Status','Stanje')}</th><th>24h</th><th>${this.t('Previous 7d/day','Prejšnjih 7d/dan')}</th><th>${this.t('Deviation','Odstopanje')}</th><th></th></tr></thead><tbody>${scores}</tbody></table></div>
      <h4>${this.t('Simple continuation forecast (observed baseline, not a guaranteed forecast)','Enostavna projekcija glede na izmerjeno rast')}</h4>
      <div class="table-scroll"><table class="table"><thead><tr><th>${this.t('Source','Vir')}</th><th>+7d</th><th>+30d</th></tr></thead><tbody>${prediction}</tbody></table></div>
      <div class="row"><label>${this.t('Source','Vir')}: <select id="source">${options}</select></label>${[1,7,30,90].map(n=>`<button data-days="${n}" aria-current="${n===this.days}">${n}d</button>`).join('')}</div>${this.graph()}`;
  }
  writers(){
    const d=this.data.writers||{}, ent=d.entities||[];
    const table=ent.map((x,i)=>`<tr><td>${i+1}</td><td>${this.esc(x.entity_id)}</td><td>${this.esc(x.integration)}</td><td>${Number(x.rows_1h).toLocaleString()}</td><td>${Number(x.rows_24h).toLocaleString()} ${x.high_writer?'⚠':''}</td><td>${x.share_percent}%</td><td>${x.rate_per_min}</td><td>${x.previous7_daily}</td><td>${x.ratio==null?'n/a':x.ratio+'×'}</td></tr>`).join('');
    const grouped = (d.top20_grouped_by_integration||[]).map(x=>`<tr><td>${this.esc(x.integration)}</td><td>${x.rows_1h}</td><td>${x.rows_24h}</td><td>${x.entities_in_top20}</td></tr>`).join('');
    return `<div class="help">${this.t('Recorder state ROWS, not attributable disk bytes. Entities excluded from Recorder may still write to InfluxDB or generate events. Queries run once every 6 hours, time-limited and read-only.','Število VRSTIC v Recorderju, ne poraba bajtov. Izključene entitete lahko še vedno zapisujejo v InfluxDB. Poizvedbe se izvajajo največ vsake 6 ur.')}</div>
      <p>${this.t('Last diagnostic','Zadnji pregled')}: ${this.when(d.hour)} · ${this.esc(d.status||'n/a')} ${this.esc(d.reason||'')}</p>
      <h4>${this.t('Top-20 grouped by integration (partial totals)','Največ zapisov po integracijah (delni seštevek)')}</h4>
      <div class="table-scroll"><table class="table"><thead><tr><th>Integration</th><th>1h</th><th>24h</th><th>Entities</th></tr></thead><tbody>${grouped}</tbody></table></div>
      <h4>${this.t('Top entities','Najpogostejše entitete')}</h4>
      <div class="table-scroll"><table class="table"><thead><tr><th>#</th><th>Entity ID</th><th>Integration</th><th>1h</th><th>24h rows</th><th>Share</th><th>Rows/min</th><th>7d/day</th><th>Ratio</th></tr></thead><tbody>${table}</tbody></table></div>`;
  }
  settings(){return `<div class="help">${this.t('Read-only monitoring. The optional InfluxDB figure is valid only when the Supervisor returns an exact add-on subtree. Hourly snapshots are stored in a separate SQLite database under /config/.storage. To change thresholds: Settings → Devices & Services → Storage Monitor → Configure.','Spremljanje samo za branje. Meritve se shranjujejo v ločeno bazo /config/.storage. Pragove spremenite v Nastavitvah integracije.')}</div>
    <label>${this.t('Dashboard language','Jezik')}: <select id="lang"><option value="en" ${this.lang==='en'?'selected':''}>English</option><option value="sl" ${this.lang==='sl'?'selected':''}>Slovenščina</option></select></label>
    <p class="muted">${this.t('This monitor never purges, vacuums or edits Home Assistant Recorder, WAL/SHM, InfluxDB, or any monitored SQL database files. The monitoring history database is independent. It does not modify PR #19.','Ta nadzornik ne briše ali spreminja produkcijskih baz, WAL/SHM ali drugih spremljanih podatkovnih baz.')}</p>`;}
}
customElements.define('ha-storage-monitor-card', HAStorageMonitorCard);
window.customCards = window.customCards || [];
window.customCards.push({type:'ha-storage-monitor-card', name:'Storage Monitor',description:'Read-only storage growth, hourly history and alerts'});
