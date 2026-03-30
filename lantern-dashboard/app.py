from flask import Flask, jsonify, request
from pymongo import MongoClient
import requests, os, time, struct, math, datetime

app = Flask(__name__)

MONGO_URI   = os.getenv('MONGO_URI',   'mongodb://lantern:changeme@lantern-mongodb-1:27017/CURRENT_MONITOR?authSource=admin')
AUTH_CODE   = os.getenv('AUTH_CODE',   'DuybleRmR8KGLDi2DUnLsHm34WWNW779KPr2nTbqH2mEBJmBU1VEpFa3klpXlpy/xEnbTlzSf/925vbbSi5lxbtvGhiPP8JmR/95wvQ6T+U=')
LANTERN_URL = os.getenv('LANTERN_URL', 'http://lantern-tomcat-1:8080/currentmonitor')

client = MongoClient(MONGO_URI)
db     = client['CURRENT_MONITOR']

_config_cache    = None
_config_cache_at = 0
CONFIG_TTL       = 60

def get_config():
    global _config_cache, _config_cache_at
    if _config_cache is None or (time.time() - _config_cache_at) > CONFIG_TTL:
        try:
            resp = requests.get(f"{LANTERN_URL}/config",
                                headers={'auth_code': AUTH_CODE}, timeout=5)
            _config_cache    = resp.json()
            _config_cache_at = time.time()
        except Exception:
            return _config_cache or {}
    return _config_cache

def flatten_groups(groups):
    for g in (groups or []):
        for b in (g.get('breakers') or []):
            if b.get('hub', 0) > 0 and b.get('port', 0) > 0:
                yield g['name'], b
        yield from flatten_groups(g.get('sub_groups'))

def decode_readings(data):
    vals = []
    for i in range(min(60, len(data) // 4)):
        v = struct.unpack_from('>f', data, i * 4)[0]
        if math.isfinite(v) and abs(v) > 0.01:
            vals.append(v)
    return vals

@app.route('/api/readings')
def api_readings():
    config = get_config()
    latest = {doc['key']: doc for doc in db.breaker_power.find()}
    result = []
    for name, b in flatten_groups(config.get('breaker_groups')):
        key = f"{b['panel']}-{b['space']}"
        doc = latest.get(key, {})
        result.append({
            'name':     name,
            'power':    doc.get('power'),
            'polarity': b.get('polarity', 'NORMAL'),
            'hub':      b.get('hub'),
            'panel':    b.get('panel'),
            'space':    b.get('space'),
        })
    return jsonify(result)

@app.route('/api/history/<int:hub>/<int:panel>/<int:space>')
def api_history(hub, panel, space):
    hours     = min(int(request.args.get('hours', 2)), 168)
    now_min   = int(time.time() / 60)
    start_min = now_min - hours * 60
    docs = db.hub_power_minute.find(
        {'hub': hub, 'minute': {'$gte': start_min}},
        {'minute': 1, 'breakers': 1}
    ).sort('minute', 1)
    result = []
    for doc in docs:
        for b in (doc.get('breakers') or []):
            if b.get('panel') == panel and b.get('space') == space:
                vals = decode_readings(bytes(b['readings']))
                if vals:
                    result.append({'ts': doc['minute'] * 60 * 1000,
                                   'power': sum(vals) / len(vals)})
    return jsonify(result)

@app.route('/api/sparklines')
def api_sparklines():
    minutes   = min(int(request.args.get('minutes', 30)), 120)
    now_min   = int(time.time() / 60)
    start_min = now_min - minutes
    result = {}
    for hub in [1, 2]:
        docs = db.hub_power_minute.find(
            {'hub': hub, 'minute': {'$gte': start_min}},
            {'minute': 1, 'breakers': 1}
        ).sort('minute', 1)
        for doc in docs:
            for b in (doc.get('breakers') or []):
                key  = f"{hub}-{b['panel']}-{b['space']}"
                vals = decode_readings(bytes(b['readings']))
                result.setdefault(key, []).append(
                    sum(vals) / len(vals) if vals else 0)
    return jsonify(result)

@app.route('/api/energy')
def api_energy():
    # tz_offset param (hours from UTC, e.g. -5 for CDT) controls local midnight
    tz_offset = float(request.args.get('tz_offset', -5))
    now_utc   = datetime.datetime.now(datetime.timezone.utc)
    local_now = now_utc + datetime.timedelta(hours=tz_offset)
    local_midnight = datetime.datetime(local_now.year, local_now.month, local_now.day)
    utc_midnight   = local_midnight - datetime.timedelta(hours=tz_offset)
    start_min = int(utc_midnight.timestamp() / 60)

    config = get_config()
    polarity_map = {}
    for name, b in flatten_groups(config.get('breaker_groups')):
        polarity_map[f"{b['hub']}-{b['panel']}-{b['space']}"] = b.get('polarity', 'NORMAL')

    solar_wh = load_wh = 0.0
    for hub in [1, 2]:
        docs = db.hub_power_minute.find(
            {'hub': hub, 'minute': {'$gte': start_min}},
            {'breakers': 1})
        for doc in docs:
            for b in (doc.get('breakers') or []):
                vals = decode_readings(bytes(b['readings']))
                if not vals: continue
                wh  = abs(sum(vals) / len(vals)) / 60
                key = f"{hub}-{b['panel']}-{b['space']}"
                if polarity_map.get(key) == 'SOLAR':
                    solar_wh += wh
                else:
                    load_wh += wh
    return jsonify({'solar_kwh': round(solar_wh / 1000, 2),
                    'load_kwh':  round(load_wh  / 1000, 2)})

@app.route('/api/total_history')
def api_total_history():
    """Aggregate total load + solar power per minute for the system-wide chart."""
    hours     = min(int(request.args.get('hours', 2)), 168)
    now_min   = int(time.time() / 60)
    start_min = now_min - hours * 60
    config    = get_config()
    polarity_map = {}
    for name, b in flatten_groups(config.get('breaker_groups')):
        polarity_map[f"{b['hub']}-{b['panel']}-{b['space']}"] = b.get('polarity', 'NORMAL')

    by_minute = {}
    for hub in [1, 2]:
        docs = db.hub_power_minute.find(
            {'hub': hub, 'minute': {'$gte': start_min}},
            {'minute': 1, 'breakers': 1}
        ).sort('minute', 1)
        for doc in docs:
            m = doc['minute']
            by_minute.setdefault(m, {'solar': 0.0, 'load': 0.0})
            for b in (doc.get('breakers') or []):
                vals = decode_readings(bytes(b['readings']))
                if not vals: continue
                avg = sum(vals) / len(vals)
                key = f"{hub}-{b['panel']}-{b['space']}"
                if polarity_map.get(key) == 'SOLAR':
                    by_minute[m]['solar'] += abs(avg)
                else:
                    by_minute[m]['load'] += abs(avg)

    result = [{'ts': m * 60 * 1000,
               'solar': round(v['solar'], 1),
               'load':  round(v['load'],  1)}
              for m, v in sorted(by_minute.items())]
    return jsonify(result)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lantern Power</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f18;color:#dde1f0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
header{background:#131623;padding:12px 20px;border-bottom:1px solid #1e2235;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
h1{font-size:.95rem;font-weight:700;color:#fff;white-space:nowrap}
.stats{display:flex;gap:16px;margin-left:auto;flex-wrap:wrap}
.stat{text-align:right;cursor:pointer}
.stat-label{font-size:.6rem;color:#556;text-transform:uppercase;letter-spacing:.08em}
.stat-value{font-size:.95rem;font-weight:700}
.c-solar{color:#22d3ee}.c-load{color:#f97316}.c-export{color:#4ade80}.c-import{color:#f87171}.c-muted{color:#556}
.energy-bar{width:100%;background:#131623;border-bottom:1px solid #1a1d2e;padding:6px 20px;display:flex;gap:20px;font-size:.72rem;color:#667;flex-wrap:wrap}
.energy-bar span{color:#aab}
.controls{padding:8px 20px;display:flex;gap:8px;align-items:center;background:#0d0f18;border-bottom:1px solid #1a1d2e;flex-wrap:wrap}
.btn{background:#1e2235;border:1px solid #2a2d45;color:#aab;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:.75rem;transition:background .15s}
.btn:hover{background:#252a40}.btn.active{background:#2563eb;border-color:#2563eb;color:#fff}
.btn.warn.active{background:#c2410c;border-color:#c2410c}
main{max-width:900px;margin:0 auto;padding:12px 20px}
/* System chart */
.sys-chart-wrap{max-width:900px;margin:0 auto 0;padding:0 20px 12px;display:none}
.sys-chart-wrap.open{display:block}
.sys-chart-inner{background:#131623;border:1px solid #1a1d2e;border-radius:6px;padding:12px}
.sys-chart-header{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.sys-chart-title{font-size:.8rem;font-weight:600;color:#aab;flex:1}
.legend-dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:4px}
.sys-chart-canvas-wrap{position:relative;height:160px}
/* Rows */
.row{display:grid;grid-template-columns:180px 80px 1fr 92px;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #131623;cursor:pointer;transition:background .1s;border-radius:4px}
.row:hover{background:#131623}
.row.expanded{background:#131623}
.rname{font-size:.83rem;color:#ccd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sparkline-wrap{width:80px;height:22px;flex-shrink:0}
.bar-wrap{background:#1a1d2e;border-radius:3px;height:8px;overflow:hidden}
.bar{height:100%;border-radius:3px;transition:width .5s ease;min-width:0}
.bar-load{background:linear-gradient(90deg,#ea580c,#f97316)}.bar-solar{background:linear-gradient(90deg,#0891b2,#22d3ee)}.bar-idle{background:#1e2235;width:100%!important}
.rwatts{text-align:right;font-size:.83rem;font-variant-numeric:tabular-nums;color:#ccd}
.rwatts.solar{color:#22d3ee}.rwatts.idle{color:#334}
.chart-panel{display:none;padding:12px 0 4px;grid-column:1/-1}
.chart-panel.open{display:block}
.chart-header{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.chart-title{font-size:.8rem;font-weight:600;color:#aab;flex:1}
.range-btn{background:#1a1d2e;border:1px solid #252840;color:#778;padding:3px 9px;border-radius:3px;cursor:pointer;font-size:.72rem}
.range-btn:hover{background:#252840}.range-btn.active{background:#1d4ed8;border-color:#1d4ed8;color:#fff}
.chart-wrap{position:relative;height:180px}
#updated{position:fixed;bottom:8px;right:12px;font-size:.62rem;color:#223}
.hub-pill{display:inline-block;font-size:.58rem;padding:1px 5px;border-radius:3px;margin-left:4px;background:#1e2235;color:#556;vertical-align:middle}
</style>
</head>
<body>
<header>
  <h1>⚡ Lantern Power</h1>
  <div class="stats">
    <div class="stat" title="Click to view system chart" onclick="toggleSysChart()">
      <div class="stat-label">Solar</div>
      <div class="stat-value c-solar" id="s-solar">—</div>
    </div>
    <div class="stat" onclick="toggleSysChart()">
      <div class="stat-label">Consuming</div>
      <div class="stat-value c-load" id="s-load">—</div>
    </div>
    <div class="stat">
      <div class="stat-label">Grid</div>
      <div class="stat-value" id="s-net">—</div>
    </div>
  </div>
</header>
<div class="energy-bar" id="energy-bar">Today: loading…</div>

<!-- System-wide chart (hidden until Solar/Load clicked) -->
<div class="sys-chart-wrap" id="sys-chart-wrap">
  <div class="sys-chart-inner">
    <div class="sys-chart-header">
      <div class="sys-chart-title">
        System Power
        <span style="font-size:.65rem;color:#556;margin-left:8px">
          <span class="legend-dot" style="background:#22d3ee"></span>Solar
          &nbsp;
          <span class="legend-dot" style="background:#f97316"></span>Load
        </span>
      </div>
      <div class="range-btn active" data-sh="1" onclick="setSysRange(1,this)">1H</div>
      <div class="range-btn" data-sh="6" onclick="setSysRange(6,this)">6H</div>
      <div class="range-btn" data-sh="24" onclick="setSysRange(24,this)">24H</div>
    </div>
    <div class="sys-chart-canvas-wrap"><canvas id="sys-canvas"></canvas></div>
  </div>
</div>

<div class="controls">
  <div class="btn active" id="sort-power" onclick="setSort('power')">⚡ By Power</div>
  <div class="btn" id="sort-name" onclick="setSort('name')">A–Z</div>
  <div class="btn" id="btn-hub1" onclick="setHub(1)">Hub 1</div>
  <div class="btn" id="btn-hub2" onclick="setHub(2)">Hub 2</div>
  <div class="btn" id="btn-hide-idle" onclick="toggleIdle()">Hide Idle</div>
</div>
<main><div id="rows"></div></main>
<div id="updated">connecting…</div>

<script>
const fmtW = w => {
  const a = Math.abs(w);
  return a >= 1000 ? (a/1000).toFixed(2)+' kW' : Math.round(a)+' W';
};
const fmtKwh = v => v.toFixed(2)+' kWh';

let state       = { breakers: [], sparklines: {}, sort: 'power', hubFilter: 0, hideIdle: false };
let openIdx     = null;
let chartInst   = null;
let peakWatts   = 500;
let sysChartInst = null;
let sysChartOpen = false;
let sysChartHours = 1;

// ── Controls ─────────────────────────────────────────────────────────────────
function setSort(s) {
  state.sort = s;
  document.getElementById('sort-power').classList.toggle('active', s==='power');
  document.getElementById('sort-name').classList.toggle('active', s==='name');
  renderList();
}

function setHub(h) {
  state.hubFilter = (state.hubFilter === h) ? 0 : h;
  document.getElementById('btn-hub1').classList.toggle('active', state.hubFilter===1);
  document.getElementById('btn-hub2').classList.toggle('active', state.hubFilter===2);
  renderList();
}

function toggleIdle() {
  state.hideIdle = !state.hideIdle;
  document.getElementById('btn-hide-idle').classList.toggle('active', state.hideIdle);
  renderList();
}

// ── Sparklines ───────────────────────────────────────────────────────────────
function sparklineSVG(data, isSolar) {
  if (!data || data.length < 2) return `<svg width="80" height="22"></svg>`;
  const abs = data.map(Math.abs);
  const mx  = Math.max(...abs, 1);
  const pts = abs.map((v,i) => {
    const x = (i/(abs.length-1))*78+1;
    const y = 20-(v/mx)*18;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const col = isSolar ? '#22d3ee' : '#f97316';
  return `<svg width="80" height="22" viewBox="0 0 80 22"><polyline fill="none" stroke="${col}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" points="${pts}"/></svg>`;
}

async function fetchSparklines() {
  try {
    state.sparklines = await fetch('/api/sparklines?minutes=30').then(r=>r.json());
    renderList();
  } catch(e) {}
}

// ── Energy totals ─────────────────────────────────────────────────────────────
async function fetchEnergy() {
  try {
    const tzOffset = -(new Date().getTimezoneOffset() / 60);
    const d = await fetch('/api/energy?tz_offset='+tzOffset).then(r=>r.json());
    const bar = document.getElementById('energy-bar');
    const todayStr = new Date().toLocaleDateString(undefined, {month:'short',day:'numeric'});
    const net = d.solar_kwh - d.load_kwh;
    const netStr = net >= 0
      ? `<span style="color:#4ade80">↑ ${fmtKwh(net)} exported</span>`
      : `<span style="color:#f87171">↓ ${fmtKwh(-net)} imported</span>`;
    bar.innerHTML = `${todayStr}: <span>☀ ${fmtKwh(d.solar_kwh)} generated</span> &nbsp;|&nbsp; <span>🏠 ${fmtKwh(d.load_kwh)} consumed</span> &nbsp;|&nbsp; ${netStr}`;
  } catch(e) {}
}

// ── Main readings ─────────────────────────────────────────────────────────────
async function fetchReadings() {
  try {
    const data = await fetch('/api/readings').then(r=>r.json());
    state.breakers = data;
    data.forEach(b => { if (b.power!=null) peakWatts = Math.max(peakWatts, Math.abs(b.power)); });
    renderList();
    updateHeader(data);
    document.getElementById('updated').textContent = 'updated '+new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('updated').textContent = 'error: '+e.message;
  }
}

function updateHeader(data) {
  let solar=0, load=0;
  data.forEach(b => {
    if (b.power==null) return;
    if (b.polarity==='SOLAR') solar+=Math.abs(b.power);
    else load+=Math.abs(b.power);
  });
  document.getElementById('s-solar').textContent = fmtW(solar);
  document.getElementById('s-load').textContent  = fmtW(load);
  const net = load-solar;
  const el  = document.getElementById('s-net');
  el.textContent = (net>0?'▲ ':'▼ ')+fmtW(Math.abs(net));
  el.className   = 'stat-value '+(net>0?'c-import':'c-export');
}

// ── Render list ───────────────────────────────────────────────────────────────
function renderList() {
  let visible = [...state.breakers];
  if (state.hubFilter) visible = visible.filter(b => b.hub === state.hubFilter);
  if (state.hideIdle)  visible = visible.filter(b => b.power != null && Math.abs(b.power) >= 5);

  if (state.sort==='power')
    visible.sort((a,b)=>(Math.abs(b.power||0))-(Math.abs(a.power||0)));
  else
    visible.sort((a,b)=>a.name.localeCompare(b.name));

  const container = document.getElementById('rows');

  // Hide rows not in visible set
  container.querySelectorAll('.row').forEach(el => {
    const b = state.breakers[parseInt(el.dataset.origIdx)];
    if (!b || !visible.includes(b)) el.style.display = 'none';
    else el.style.display = '';
  });

  visible.forEach((b) => {
    const origIdx = state.breakers.indexOf(b);
    const isSolar = b.polarity==='SOLAR';
    const pwr     = b.power;
    const isNull  = pwr==null;
    const isIdle  = !isNull && Math.abs(pwr)<5;
    const abs     = isNull?0:Math.abs(pwr);
    const pct     = Math.min(100,(abs/peakWatts)*100);
    const spKey   = `${b.hub}-${b.panel}-${b.space}`;
    const spData  = state.sparklines[spKey];

    let el = document.getElementById('br-'+origIdx);
    if (!el) {
      el = document.createElement('div');
      el.className = 'row';
      el.id = 'br-'+origIdx;
      el.dataset.origIdx = origIdx;
      el.innerHTML =
        `<div class="rname"></div>`+
        `<div class="sparkline-wrap"></div>`+
        `<div class="bar-wrap"><div class="bar"></div></div>`+
        `<div class="rwatts"></div>`+
        `<div class="chart-panel" id="cp-${origIdx}">`+
          `<div class="chart-header">`+
            `<div class="chart-title"></div>`+
            `<div class="range-btn active" data-h="1">1H</div>`+
            `<div class="range-btn" data-h="6">6H</div>`+
            `<div class="range-btn" data-h="24">24H</div>`+
          `</div>`+
          `<div class="chart-wrap"><canvas id="cv-${origIdx}"></canvas></div>`+
        `</div>`;

      el.querySelector('.rname').addEventListener('click', ()=>toggleChart(origIdx, b));
      el.querySelector('.sparkline-wrap').addEventListener('click', ()=>toggleChart(origIdx, b));
      el.querySelector('.bar-wrap').addEventListener('click', ()=>toggleChart(origIdx, b));
      el.querySelector('.rwatts').addEventListener('click', ()=>toggleChart(origIdx, b));
      el.querySelectorAll('.range-btn').forEach(btn => {
        btn.addEventListener('click', e=>{
          e.stopPropagation();
          el.querySelectorAll('.range-btn').forEach(b2=>b2.classList.remove('active'));
          btn.classList.add('active');
          loadChart(origIdx, b, parseInt(btn.dataset.h));
        });
      });
      container.appendChild(el);
    }

    container.appendChild(el);

    // Hub pill on name
    const hubPill = b.hub ? `<span class="hub-pill">H${b.hub}</span>` : '';
    el.querySelector('.rname').innerHTML = `${b.name}${hubPill}`;
    el.querySelector('.sparkline-wrap').innerHTML = sparklineSVG(spData, isSolar);

    const bar   = el.querySelector('.bar');
    const watts = el.querySelector('.rwatts');
    if (isNull||isIdle) {
      bar.className     = 'bar bar-idle';
      watts.className   = 'rwatts idle';
      watts.textContent = isNull ? '—' : fmtW(pwr);
    } else {
      bar.className     = 'bar '+(isSolar?'bar-solar':'bar-load');
      bar.style.width   = pct+'%';
      watts.className   = 'rwatts'+(isSolar?' solar':'');
      watts.textContent = (isSolar?'−':'')+fmtW(pwr);
    }

    el.querySelector('.chart-title').textContent = b.name;
  });
}

// ── System chart ──────────────────────────────────────────────────────────────
function toggleSysChart() {
  sysChartOpen = !sysChartOpen;
  document.getElementById('sys-chart-wrap').classList.toggle('open', sysChartOpen);
  if (sysChartOpen) loadSysChart(sysChartHours);
  else if (sysChartInst) { sysChartInst.destroy(); sysChartInst=null; }
}

function setSysRange(h, btn) {
  sysChartHours = h;
  document.querySelectorAll('[data-sh]').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  loadSysChart(h);
}

async function loadSysChart(hours) {
  try {
    const data = await fetch('/api/total_history?hours='+hours).then(r=>r.json());
    const canvas = document.getElementById('sys-canvas');
    if (sysChartInst) { sysChartInst.destroy(); sysChartInst=null; }
    sysChartInst = new Chart(canvas, {
      type: 'line',
      data: {
        datasets: [
          {
            label: 'Solar',
            data: data.map(d=>({x:d.ts, y:d.solar})),
            borderColor: '#22d3ee',
            backgroundColor: '#22d3ee18',
            borderWidth: 1.5,
            pointRadius: 0,
            fill: true,
            tension: 0.3,
          },
          {
            label: 'Load',
            data: data.map(d=>({x:d.ts, y:d.load})),
            borderColor: '#f97316',
            backgroundColor: '#f9731618',
            borderWidth: 1.5,
            pointRadius: 0,
            fill: true,
            tension: 0.3,
          }
        ]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: {display:false} },
        scales: {
          x: {
            type: 'time',
            time: { unit: hours<=2?'minute':hours<=6?'hour':'hour',
                    displayFormats: {minute:'h:mm a', hour:'h a'} },
            grid: { color:'#1a1d2e' },
            ticks: { color:'#556', maxTicksLimit:6 }
          },
          y: {
            grid: { color:'#1a1d2e' },
            ticks: { color:'#556',
                     callback: v => v>=1000?(v/1000).toFixed(1)+'kW':Math.round(v)+'W' }
          }
        }
      }
    });
  } catch(e) { console.error('sys chart error', e); }
}

// ── Per-breaker chart ─────────────────────────────────────────────────────────
function toggleChart(idx, breaker) {
  const panel = document.getElementById('cp-'+idx);
  const row   = document.getElementById('br-'+idx);
  if (openIdx===idx) {
    panel.classList.remove('open');
    row.classList.remove('expanded');
    if (chartInst) { chartInst.destroy(); chartInst=null; }
    openIdx = null;
    return;
  }
  if (openIdx!==null) {
    const old = document.getElementById('cp-'+openIdx);
    if (old) old.classList.remove('open');
    const oldRow = document.getElementById('br-'+openIdx);
    if (oldRow) oldRow.classList.remove('expanded');
    if (chartInst) { chartInst.destroy(); chartInst=null; }
  }
  openIdx = idx;
  panel.classList.add('open');
  row.classList.add('expanded');
  panel.querySelectorAll('.range-btn').forEach(b=>b.classList.remove('active'));
  panel.querySelector('[data-h="1"]').classList.add('active');
  loadChart(idx, breaker, 1);
}

async function loadChart(idx, breaker, hours) {
  const canvas = document.getElementById('cv-'+idx);
  if (!canvas) return;
  try {
    const data = await fetch(`/api/history/${breaker.hub}/${breaker.panel}/${breaker.space}?hours=${hours}`).then(r=>r.json());
    if (chartInst) { chartInst.destroy(); chartInst=null; }
    const isSolar = breaker.polarity==='SOLAR';
    const col = isSolar ? '#22d3ee' : '#f97316';
    chartInst = new Chart(canvas, {
      type: 'line',
      data: {
        datasets: [{
          data: data.map(d=>({x:d.ts, y:Math.abs(d.power)})),
          borderColor: col,
          backgroundColor: col+'22',
          borderWidth: 1.5,
          pointRadius: hours<=2 ? 2 : 0,
          fill: true,
          tension: 0.3,
        }]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: {display:false} },
        scales: {
          x: {
            type: 'time',
            time: { unit: hours<=2?'minute':hours<=6?'hour':'hour',
                    displayFormats: {minute:'h:mm a', hour:'h a'} },
            grid: { color:'#1a1d2e' },
            ticks: { color:'#556', maxTicksLimit:6 }
          },
          y: {
            grid: { color:'#1a1d2e' },
            ticks: { color:'#556',
                     callback: v => v>=1000?(v/1000).toFixed(1)+'k':Math.round(v) }
          }
        }
      }
    });
  } catch(e) { console.error(e); }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
fetchReadings();
fetchSparklines();
fetchEnergy();
setInterval(fetchReadings,   3000);
setInterval(fetchSparklines, 30000);
setInterval(fetchEnergy,     300000);
</script>
</body>
</html>"""

@app.route('/')
def index():
    return HTML

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8082)
