#!/usr/bin/env python3
"""Uptime tracker web dashboard — serves a real-time monitoring page on port 9090."""

import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

DB_PATH       = os.environ.get('UPTIME_DB',     '/opt/uptime-tracker/data/uptime.db')
CONFIG_PATH   = os.environ.get('UPTIME_CONFIG', '/opt/uptime-tracker/config.json')
DEFAULT_HOST  = os.environ.get('UPTIME_HOST',   '8.8.8.8')
PORT          = int(os.environ.get('UPTIME_PORT', '9090'))
LATENCY_WARN_MS = float(os.environ.get('UPTIME_LATENCY_WARN', '100'))
LATENCY_CRIT_MS = float(os.environ.get('UPTIME_LATENCY_CRIT', '300'))

PRESETS = [
    {'value': '8.8.8.8', 'label': '8.8.8.8 — Google DNS'},
    {'value': '1.1.1.1', 'label': '1.1.1.1 — Cloudflare'},
    {'value': '9.9.9.9', 'label': '9.9.9.9 — Quad9'},
]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config() -> dict:
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
                if 'host' in cfg:
                    return cfg
    except Exception:
        pass
    return {'host': DEFAULT_HOST}


def write_config(host: str) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    cfg = get_config()
    cfg['host'] = host
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f)


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------

def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_known_hosts() -> list:
    """All distinct hosts that have ever been pinged, for the dropdown."""
    if not os.path.exists(DB_PATH):
        return []
    conn = _conn()
    rows = conn.execute('SELECT DISTINCT host FROM pings ORDER BY host').fetchall()
    conn.close()
    return [r['host'] for r in rows]


def get_current(host: str):
    if not os.path.exists(DB_PATH):
        return None
    conn = _conn()
    row = conn.execute(
        'SELECT * FROM pings WHERE host = ? ORDER BY timestamp DESC LIMIT 1',
        (host,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    age = time.time() - row['timestamp']
    loss = row['packet_loss']
    avg  = row['avg_ms']
    if loss >= 100:
        status = 'down'
    elif loss > 0 or (avg and avg >= LATENCY_CRIT_MS):
        status = 'degraded'
    elif avg and avg >= LATENCY_WARN_MS:
        status = 'degraded'
    else:
        status = 'up'
    return {
        'timestamp':   row['timestamp'],
        'host':        row['host'],
        'min_ms':      row['min_ms'],
        'avg_ms':      row['avg_ms'],
        'max_ms':      row['max_ms'],
        'packet_loss': row['packet_loss'],
        'status':      status,
        'stale':       age > 30,
    }


def get_history(host: str, hours: int = 24) -> list:
    """Adaptive resolution: 1-min buckets for >1h ago, raw 5-sec for last hour."""
    if not os.path.exists(DB_PATH):
        return []

    now           = time.time()
    since         = now - hours * 3600
    one_hour_ago  = now - 3600

    conn = _conn()

    older = conn.execute(
        '''SELECT
               CAST(timestamp / 60 AS INTEGER) * 60 AS bucket,
               MIN(CASE WHEN min_ms IS NOT NULL THEN min_ms END) AS min_ms,
               AVG(CASE WHEN avg_ms IS NOT NULL THEN avg_ms END) AS avg_ms,
               MAX(CASE WHEN max_ms IS NOT NULL THEN max_ms END) AS max_ms,
               CAST(SUM(packets_sent - packets_recv) * 100.0
                    / MAX(SUM(packets_sent), 1) AS REAL) AS packet_loss,
               COUNT(*) AS samples
           FROM pings
           WHERE timestamp > ? AND timestamp <= ? AND host = ?
           GROUP BY bucket
           ORDER BY bucket''',
        (since, one_hour_ago, host),
    ).fetchall()

    recent = conn.execute(
        '''SELECT
               timestamp AS bucket,
               min_ms, avg_ms, max_ms, packet_loss,
               1 AS samples
           FROM pings
           WHERE timestamp > ? AND host = ?
           ORDER BY timestamp''',
        (one_hour_ago, host),
    ).fetchall()

    conn.close()
    return [dict(r) for r in older] + [dict(r) for r in recent]


def get_stats(history: list) -> dict:
    if not history:
        return {'uptime_pct': None, 'avg_ms': None, 'max_ms': None,
                'outage_count': 0, 'total': 0}

    total    = len(history)
    up       = sum(1 for r in history if r['packet_loss'] < 100)
    all_avg  = [r['avg_ms'] for r in history if r['avg_ms'] is not None]
    all_max  = [r['max_ms'] for r in history if r['max_ms'] is not None]

    outages, in_outage = 0, False
    for r in history:
        if r['packet_loss'] >= 100:
            if not in_outage:
                outages += 1
                in_outage = True
        else:
            in_outage = False

    return {
        'uptime_pct':   round(up / total * 100, 2) if total else None,
        'avg_ms':       round(sum(all_avg) / len(all_avg), 1) if all_avg else None,
        'max_ms':       round(max(all_max), 1) if all_max else None,
        'outage_count': outages,
        'total':        total,
    }


def get_events(history: list) -> list:
    events, current = [], None
    for bucket in history:
        loss = bucket['packet_loss']
        ts   = bucket['bucket']
        if loss > 0:
            if current is None:
                current = {
                    'start':    ts,
                    'end':      ts + 60,
                    'max_loss': loss,
                    'kind':     'outage' if loss >= 100 else 'degraded',
                    'samples':  1,
                }
            else:
                current['end']      = ts + 60
                current['max_loss'] = max(current['max_loss'], loss)
                current['samples'] += 1
                if loss >= 100:
                    current['kind'] = 'outage'
        else:
            if current is not None:
                current['duration_s'] = current['end'] - current['start']
                # Skip single-sample degraded events — likely one dropped packet (noise)
                if current['max_loss'] >= 100 or current['samples'] >= 3:
                    events.append(current)
                current = None

    if current is not None:
        current['end']        = None
        current['duration_s'] = None
        if current['max_loss'] >= 100 or current['samples'] >= 3:
            events.append(current)

    events.reverse()
    return events[:50]


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Uptime Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e;
    --green: #3fb950; --yellow: #d29922; --red: #f85149; --blue: #58a6ff;
  }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace; font-size: 14px; line-height: 1.5; }

  header { display: flex; align-items: center; gap: 10px; padding: 14px 24px; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
  header h1 { font-size: 15px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); margin-left: 4px; }
  #status-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); flex-shrink: 0; }
  #status-dot.degraded { background: var(--yellow); }
  #status-dot.down     { background: var(--red); }
  #status-dot.unknown  { background: var(--muted); }
  #status-text { font-weight: 600; font-size: 15px; min-width: 80px; }
  #status-text.degraded { color: var(--yellow); }
  #status-text.down     { color: var(--red); }
  #latency-now { color: var(--blue); font-weight: 600; font-family: monospace; min-width: 70px; }
  #clock { margin-left: auto; color: var(--muted); font-size: 13px; font-family: monospace; white-space: nowrap; }

  /* Host selector */
  #host-wrap { display: flex; align-items: center; gap: 6px; }
  #host-select {
    background: var(--card); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 4px 8px; font-size: 13px; cursor: pointer;
    font-family: monospace; max-width: 220px;
  }
  #host-select:focus { outline: none; border-color: var(--blue); }
  #custom-input {
    display: none; background: var(--card); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px;
    padding: 4px 8px; font-size: 13px; width: 170px; font-family: monospace;
  }
  #custom-input:focus { outline: none; border-color: var(--blue); }
  #custom-btn {
    display: none; background: var(--blue); color: #0d1117; border: none;
    border-radius: 6px; padding: 4px 10px; cursor: pointer;
    font-weight: 600; font-size: 13px;
  }
  #custom-btn:hover { opacity: .85; }

  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; padding: 20px 24px 0; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px 20px; }
  .card-label { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 6px; }
  .card-value { font-size: 28px; font-weight: 700; font-family: monospace; }
  .card-value.green  { color: var(--green); }
  .card-value.yellow { color: var(--yellow); }
  .card-value.red    { color: var(--red); }
  .card-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }

  .section { padding: 20px 24px; }
  .section-title { font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 12px; }
  .chart-wrap { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  #latency-canvas { height: 200px !important; }
  #loss-canvas    { height: 100px !important; }

  .events-table { width: 100%; border-collapse: collapse; }
  .events-table th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); border-bottom: 1px solid var(--border); }
  .events-table td { padding: 9px 12px; border-bottom: 1px solid var(--border); font-family: monospace; font-size: 13px; }
  .events-table tr:last-child td { border-bottom: none; }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge.outage   { background: rgba(248,81,73,.15); color: var(--red); }
  .badge.degraded { background: rgba(210,153,34,.15); color: var(--yellow); }
  .badge.ongoing  { animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  #no-events { color: var(--muted); font-size: 13px; padding: 16px 0; }
  #clear-events-btn { background: none; border: 1px solid var(--border); color: var(--muted); border-radius: 4px; padding: 2px 8px; font-size: 11px; cursor: pointer; letter-spacing: .04em; text-transform: uppercase; }
  #clear-events-btn:hover { color: var(--text); border-color: var(--muted); }

  @media (max-width: 700px) { .grid { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>

<header>
  <div id="status-dot" class="unknown"></div>
  <span id="status-text">—</span>
  <div id="host-wrap">
    <select id="host-select" onchange="onHostSelectChange()"></select>
    <input  id="custom-input" type="text" placeholder="hostname or IP" onkeydown="if(event.key==='Enter')applyCustomHost()">
    <button id="custom-btn" onclick="applyCustomHost()">Apply</button>
  </div>
  <span id="latency-now"></span>
  <h1>Uptime Tracker</h1>
  <span id="clock"></span>
</header>

<div class="grid">
  <div class="card">
    <div class="card-label">Uptime (24h)</div>
    <div class="card-value" id="stat-uptime">—</div>
    <div class="card-sub"  id="stat-uptime-sub"></div>
  </div>
  <div class="card">
    <div class="card-label">Avg Latency (24h)</div>
    <div class="card-value" id="stat-avg">—</div>
    <div class="card-sub">milliseconds</div>
  </div>
  <div class="card">
    <div class="card-label">Max Latency (24h)</div>
    <div class="card-value" id="stat-max">—</div>
    <div class="card-sub">milliseconds</div>
  </div>
  <div class="card">
    <div class="card-label">Outages (24h)</div>
    <div class="card-value" id="stat-outages">—</div>
    <div class="card-sub"  id="stat-outages-sub"></div>
  </div>
</div>

<div class="section">
  <div class="section-title">Latency &mdash; last 24h &nbsp;&middot;&nbsp; <span style="font-size:11px;color:var(--muted);text-transform:none;letter-spacing:0">&gt;1h ago: 1-min avg &nbsp;|&nbsp; last hour: 5-sec raw</span></div>
  <div class="chart-wrap"><canvas id="latency-canvas"></canvas></div>
</div>

<div class="section" style="padding-top:0">
  <div class="section-title">Packet Loss &mdash; last 24h &nbsp;&middot;&nbsp; <span style="font-size:11px;color:var(--muted);text-transform:none;letter-spacing:0">&gt;1h ago: 1-min avg &nbsp;|&nbsp; last hour: 5-sec raw</span></div>
  <div class="chart-wrap"><canvas id="loss-canvas"></canvas></div>
</div>

<div class="section" style="padding-top:0">
  <div class="section-title" style="display:flex;align-items:center;gap:10px">Events <button id="clear-events-btn" onclick="clearEvents()">Clear</button></div>
  <div class="card">
    <div id="no-events" style="display:none">No events in the last 24 hours.</div>
    <table class="events-table" id="events-table">
      <thead><tr><th>Type</th><th>Started</th><th>Ended</th><th>Duration</th><th>Max Loss</th></tr></thead>
      <tbody id="events-body"></tbody>
    </table>
  </div>
</div>

<script>
const WARN_MS = __WARN_MS__;
const CRIT_MS = __CRIT_MS__;
const PRESETS = __PRESETS__;

let latencyChart  = null;
let lossChart     = null;
let lastHistory   = [];
let lastEvents    = [];
let clearedBefore = 0;

// ---- Formatting helpers ----

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}
function fmtTimeFull(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
function fmtDateTime(ts) {
  return new Date(ts * 1000).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
function fmtDuration(s) {
  if (s === null) return 'ongoing';
  if (s < 60)    return s + 's';
  if (s < 3600)  return Math.round(s / 60) + 'm ' + (s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + Math.round((s % 3600) / 60) + 'm';
}
function latencyColor(ms) {
  if (ms === null) return 'var(--muted)';
  if (ms >= CRIT_MS) return 'var(--red)';
  if (ms >= WARN_MS) return 'var(--yellow)';
  return 'var(--green)';
}
function uptimeColor(pct) {
  if (pct === null) return '';
  if (pct >= 99.9) return 'green';
  if (pct >= 99)   return 'yellow';
  return 'red';
}

// ---- Host selector ----

async function initHostSelector() {
  const resp = await fetch('/api/config');
  const cfg  = await resp.json();
  populateSelect(cfg.host, cfg.known_hosts);
}

function populateSelect(currentHost, knownHosts) {
  const sel = document.getElementById('host-select');
  const presetValues = PRESETS.map(p => p.value);

  sel.innerHTML = '';

  // Common presets group
  const pg = document.createElement('optgroup');
  pg.label = 'Common';
  PRESETS.forEach(p => {
    const o = document.createElement('option');
    o.value = p.value; o.textContent = p.label;
    pg.appendChild(o);
  });
  sel.appendChild(pg);

  // Previously used hosts not already in presets
  const others = (knownHosts || []).filter(h => !presetValues.includes(h));
  if (others.length) {
    const og = document.createElement('optgroup');
    og.label = 'Previously used';
    others.forEach(h => {
      const o = document.createElement('option');
      o.value = h; o.textContent = h;
      og.appendChild(o);
    });
    sel.appendChild(og);
  }

  // Custom entry option
  const co = document.createElement('option');
  co.value = '__custom__'; co.textContent = 'Custom…';
  sel.appendChild(co);

  // Select current host; if it isn't listed yet, prepend it
  sel.value = currentHost;
  if (sel.value !== currentHost) {
    const o = document.createElement('option');
    o.value = currentHost; o.textContent = currentHost;
    sel.insertBefore(o, sel.firstChild);
    sel.value = currentHost;
  }
}

function onHostSelectChange() {
  const sel = document.getElementById('host-select');
  if (sel.value === '__custom__') {
    document.getElementById('custom-input').style.display = '';
    document.getElementById('custom-btn').style.display  = '';
    document.getElementById('custom-input').focus();
    return;
  }
  applyHost(sel.value);
}

function applyCustomHost() {
  const input = document.getElementById('custom-input');
  const host  = input.value.trim();
  if (!host) return;
  input.value = '';
  document.getElementById('custom-input').style.display = 'none';
  document.getElementById('custom-btn').style.display   = 'none';
  applyHost(host);
}

async function applyHost(host) {
  await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({host}),
  });
  // Re-populate dropdown so the new host appears in the list
  const resp = await fetch('/api/config');
  const cfg  = await resp.json();
  populateSelect(cfg.host, cfg.known_hosts);
  refresh();
}

// ---- Dashboard update ----

function updateStatus(cur) {
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  const lat = document.getElementById('latency-now');
  if (!cur) {
    dot.className = 'unknown'; txt.textContent = 'NO DATA'; txt.className = ''; lat.textContent = ''; return;
  }
  dot.className = cur.status;
  txt.className = cur.status;
  txt.textContent = cur.stale ? 'STALE' : {up:'ONLINE', degraded:'DEGRADED', down:'OFFLINE'}[cur.status];
  if (cur.avg_ms !== null) {
    lat.textContent = cur.avg_ms.toFixed(1) + 'ms';
    lat.style.color = latencyColor(cur.avg_ms);
  } else {
    lat.textContent = 'no response';
    lat.style.color = 'var(--red)';
  }
}

function updateStats(stats) {
  const upEl = document.getElementById('stat-uptime');
  if (stats.uptime_pct !== null) {
    upEl.textContent = stats.uptime_pct.toFixed(2) + '%';
    upEl.className   = 'card-value ' + uptimeColor(stats.uptime_pct);
  }
  document.getElementById('stat-uptime-sub').textContent = stats.total + ' samples';

  const avgEl = document.getElementById('stat-avg');
  if (stats.avg_ms !== null) {
    avgEl.textContent = stats.avg_ms.toFixed(1);
    avgEl.className   = 'card-value ' + (stats.avg_ms >= CRIT_MS ? 'red' : stats.avg_ms >= WARN_MS ? 'yellow' : 'green');
  }

  const maxEl = document.getElementById('stat-max');
  if (stats.max_ms !== null) {
    maxEl.textContent = stats.max_ms.toFixed(1);
    maxEl.className   = 'card-value ' + (stats.max_ms >= CRIT_MS ? 'red' : stats.max_ms >= WARN_MS ? 'yellow' : 'green');
  }

  const outEl  = document.getElementById('stat-outages');
  outEl.textContent = stats.outage_count;
  outEl.className   = 'card-value ' + (stats.outage_count === 0 ? 'green' : 'red');
  document.getElementById('stat-outages-sub').textContent =
    stats.outage_count === 1 ? '1 interruption' : stats.outage_count + ' interruptions';
}

function buildCharts(history) {
  lastHistory = history;
  const labels    = history.map(d => fmtTime(d.bucket));
  const avgData   = history.map(d => d.packet_loss >= 100 ? null : (d.avg_ms  !== null ? +d.avg_ms.toFixed(2)  : null));
  const minData   = history.map(d => d.packet_loss >= 100 ? null : (d.min_ms  !== null ? +d.min_ms.toFixed(2)  : null));
  const maxData   = history.map(d => d.packet_loss >= 100 ? null : (d.max_ms  !== null ? +d.max_ms.toFixed(2)  : null));
  const lossData  = history.map(d => +d.packet_loss.toFixed(1));
  const lossColors = history.map(d =>
    d.packet_loss === 0 ? 'rgba(63,185,80,0.7)' :
    d.packet_loss < 100 ? 'rgba(210,153,34,0.8)' : 'rgba(248,81,73,0.9)'
  );

  const tickColor = '#8b949e';
  const gridColor = 'rgba(48,54,61,0.8)';

  const tooltipDefaults = {
    backgroundColor: '#161b22', borderColor: '#30363d', borderWidth: 1,
    titleColor: '#c9d1d9', bodyColor: '#8b949e',
    callbacks: { title: items => fmtTimeFull(lastHistory[items[0].dataIndex].bucket) },
  };

  const commonOptions = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: { legend: { display: false }, tooltip: tooltipDefaults },
  };

  if (latencyChart) latencyChart.destroy();
  latencyChart = new Chart(document.getElementById('latency-canvas'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Min', data: minData, borderColor: 'transparent', backgroundColor: 'transparent', pointRadius: 0, fill: false, spanGaps: false, order: 2 },
        { label: 'Avg', data: avgData, borderColor: '#58a6ff', backgroundColor: 'rgba(88,166,255,0.12)', borderWidth: 2, pointRadius: 0, spanGaps: false, fill: false, order: 1 },
        { label: 'Max', data: maxData, borderColor: 'transparent', backgroundColor: 'rgba(88,166,255,0.08)', pointRadius: 0, spanGaps: false, fill: '-2', order: 3 },
      ],
    },
    options: {
      ...commonOptions,
      plugins: {
        ...commonOptions.plugins,
        tooltip: { ...tooltipDefaults, callbacks: { ...tooltipDefaults.callbacks, label: ctx => ctx.raw === null ? `${ctx.dataset.label}: no response` : `${ctx.dataset.label}: ${ctx.raw} ms` } },
      },
      scales: {
        x: { ticks: { color: tickColor, maxTicksLimit: 12, maxRotation: 0 }, grid: { color: gridColor } },
        y: { ticks: { color: tickColor, callback: v => v + ' ms' }, grid: { color: gridColor } },
      },
    },
  });

  if (lossChart) lossChart.destroy();
  lossChart = new Chart(document.getElementById('loss-canvas'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label: 'Packet Loss', data: lossData, backgroundColor: lossColors, borderWidth: 0, barPercentage: 1.0, categoryPercentage: 1.0 }],
    },
    options: {
      ...commonOptions,
      plugins: {
        ...commonOptions.plugins,
        tooltip: { ...tooltipDefaults, callbacks: { ...tooltipDefaults.callbacks, label: ctx => `Loss: ${ctx.raw}%` } },
      },
      scales: {
        x: { ticks: { color: tickColor, maxTicksLimit: 12, maxRotation: 0 }, grid: { color: gridColor } },
        y: { min: 0, max: 100, ticks: { color: tickColor, callback: v => v + '%', maxTicksLimit: 5 }, grid: { color: gridColor } },
      },
    },
  });
}

function updateEvents(events) {
  lastEvents = events || [];
  const body    = document.getElementById('events-body');
  const noEvtEl = document.getElementById('no-events');
  const tableEl = document.getElementById('events-table');
  const visible = lastEvents.filter(e => !(e.end !== null && e.end < clearedBefore));
  if (!visible.length) {
    noEvtEl.style.display = ''; tableEl.style.display = 'none'; return;
  }
  noEvtEl.style.display = 'none'; tableEl.style.display = '';
  body.innerHTML = visible.map(e => {
    const ongoing = e.end === null;
    const cls = (e.kind === 'outage' ? 'outage' : 'degraded') + (ongoing ? ' ongoing' : '');
    return `<tr>
      <td><span class="badge ${cls}">${e.kind.toUpperCase()}</span></td>
      <td>${fmtDateTime(e.start)}</td>
      <td>${ongoing ? '<span style="color:var(--red)">ongoing</span>' : fmtDateTime(e.end)}</td>
      <td>${fmtDuration(e.duration_s)}</td>
      <td style="color:${e.max_loss >= 100 ? 'var(--red)' : 'var(--yellow)'}">${e.max_loss.toFixed(0)}%</td>
    </tr>`;
  }).join('');
}

function clearEvents() {
  clearedBefore = Date.now() / 1000;
  updateEvents(lastEvents);
}

async function refresh() {
  try {
    const resp = await fetch('/api/data');
    if (!resp.ok) return;
    const data = await resp.json();
    updateStatus(data.current);
    updateStats(data.stats);
    buildCharts(data.history);
    updateEvents(data.events);
  } catch (e) {
    console.error('Refresh error', e);
  }
}

// Clock
setInterval(() => {
  document.getElementById('clock').textContent =
    new Date().toLocaleString([], {weekday:'short', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'});
}, 1000);
document.getElementById('clock').textContent =
  new Date().toLocaleString([], {weekday:'short', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'});

initHostSelector();
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

WIDGET_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Uptime Status</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e;
    --green: #3fb950; --yellow: #d29922; --red: #f85149; --blue: #58a6ff;
  }
  html, body { height: 100%; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: 1fr 1fr; gap: 8px; padding: 8px; height: 100%; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; display: flex; flex-direction: column; justify-content: center; min-width: 0; overflow: hidden; }
  .lbl { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 4px; }
  .val { font-size: 22px; font-weight: 700; font-family: monospace; }
  .val.green  { color: var(--green); }
  .val.yellow { color: var(--yellow); }
  .val.red    { color: var(--red); }
  .val.blue   { color: var(--blue); }
  .sub { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .status-row { display: flex; align-items: center; gap: 6px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--muted); }
  .dot.up       { background: var(--green); }
  .dot.degraded { background: var(--yellow); }
  .dot.down     { background: var(--red); }
</style>
</head>
<body>
<div class="grid">
  <div class="card">
    <div class="lbl">Status</div>
    <div class="status-row">
      <div class="dot" id="dot"></div>
      <div class="val" id="status-text" style="font-size:17px">—</div>
    </div>
    <div class="sub" id="latency-now"></div>
  </div>
  <div class="card">
    <div class="lbl">Avg Latency (24h)</div>
    <div class="val blue" id="stat-avg">—</div>
    <div class="sub">milliseconds</div>
  </div>
  <div class="card">
    <div class="lbl">Max Latency (24h)</div>
    <div class="val" id="stat-max">—</div>
    <div class="sub">milliseconds</div>
  </div>
  <div class="card">
    <div class="lbl">Outages (24h)</div>
    <div class="val" id="stat-outages">—</div>
    <div class="sub" id="stat-outages-sub"></div>
  </div>
</div>
<script>
const WARN_MS = __WARN_MS__;
const CRIT_MS = __CRIT_MS__;

function latClass(ms) {
  if (ms === null) return 'blue';
  return ms >= CRIT_MS ? 'red' : ms >= WARN_MS ? 'yellow' : 'green';
}

async function refresh() {
  try {
    const r = await fetch('/api/data');
    if (!r.ok) return;
    const { current: cur, stats } = await r.json();

    const dot = document.getElementById('dot');
    const txt = document.getElementById('status-text');
    const lat = document.getElementById('latency-now');
    if (!cur) {
      dot.className = 'dot'; txt.textContent = 'NO DATA'; txt.className = 'val'; lat.textContent = '';
    } else {
      dot.className   = 'dot ' + cur.status;
      txt.className   = 'val ' + (cur.status === 'up' ? 'green' : cur.status === 'degraded' ? 'yellow' : 'red');
      txt.textContent = cur.stale ? 'STALE' : { up: 'ONLINE', degraded: 'DEGRADED', down: 'OFFLINE' }[cur.status];
      lat.textContent = cur.avg_ms !== null ? cur.avg_ms.toFixed(1) + ' ms now' : 'no response';
    }

    if (stats.avg_ms !== null) {
      const el = document.getElementById('stat-avg');
      el.textContent = stats.avg_ms.toFixed(1);
      el.className   = 'val ' + latClass(stats.avg_ms);
    }
    if (stats.max_ms !== null) {
      const el = document.getElementById('stat-max');
      el.textContent = stats.max_ms.toFixed(1);
      el.className   = 'val ' + latClass(stats.max_ms);
    }
    const outEl = document.getElementById('stat-outages');
    outEl.textContent = stats.outage_count ?? '—';
    outEl.className   = 'val ' + (stats.outage_count === 0 ? 'green' : 'red');
    document.getElementById('stat-outages-sub').textContent =
      stats.outage_count === 1 ? '1 interruption' : (stats.outage_count || 0) + ' interruptions';
  } catch (_) {}
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/api/data':
            host    = get_config()['host']
            history = get_history(host, 24)
            self.send_json({
                'current': get_current(host),
                'stats':   get_stats(history),
                'history': history,
                'events':  get_events(history),
            })
            return

        if path == '/api/config':
            cfg = get_config()
            cfg['known_hosts'] = get_known_hosts()
            cfg['presets']     = PRESETS
            self.send_json(cfg)
            return

        if path == '/api/health':
            self.send_json({'ok': True})
            return

        if path == '/widget':
            html = (WIDGET_HTML
                    .replace('__WARN_MS__', str(LATENCY_WARN_MS))
                    .replace('__CRIT_MS__', str(LATENCY_CRIT_MS)))
            body = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ('/', '/index.html'):
            html = (DASHBOARD_HTML
                    .replace('__WARN_MS__', str(LATENCY_WARN_MS))
                    .replace('__CRIT_MS__', str(LATENCY_CRIT_MS))
                    .replace('__PRESETS__', json.dumps(PRESETS)))
            body = html.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == '/api/config':
            length = int(self.headers.get('Content-Length', 0))
            try:
                body = json.loads(self.rfile.read(length))
                host = str(body.get('host', '')).strip()
                if not host:
                    self.send_json({'error': 'host required'}, 400)
                    return
                write_config(host)
                cfg = get_config()
                cfg['known_hosts'] = get_known_hosts()
                self.send_json(cfg)
            except Exception as exc:
                self.send_json({'error': str(exc)}, 400)
            return

        self.send_response(404)
        self.end_headers()


def main():
    httpd = HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Dashboard running on http://0.0.0.0:{PORT}  (db={DB_PATH})', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
