#!/usr/bin/env python3
"""
STOCK SCANNER - DASHBOARD (Flask)
Zapisz jako dashboard.py w folderze stock-scanner

Uruchomienie (osobny terminal):
    python dashboard.py

Dostęp:
    http://localhost:5000

Historia zmian:
    v1.0 — pierwsza wersja, dark theme terminal-style
"""

import sqlite3
import json
from datetime import datetime, date
from flask import Flask, jsonify, render_template_string
from config import CONFIG, now_chicago

app = Flask(__name__)

DB_PATH = CONFIG.get('db_path', 'scanner.db')


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ==================== API ENDPOINTS ====================

@app.route('/api/signals/today')
def signals_today():
    conn = get_db()
    try:
        today = date.today().isoformat()
        c = conn.cursor()
        c.execute('''
            SELECT id, ticker, timestamp, verdict, confidence,
                   price, volume, volume_ratio, score, reasons,
                   justification, risk, outcome_1h, outcome_4h,
                   monitoring, closed
            FROM signals
            WHERE timestamp LIKE ?
            ORDER BY timestamp DESC
        ''', (f'{today}%',))
        rows = c.fetchall()
        signals = []
        for row in rows:
            reasons = []
            try:
                reasons = json.loads(row['reasons'] or '[]')
            except Exception:
                pass
            signals.append({
                'id':           row['id'],
                'ticker':       row['ticker'],
                'time':         row['timestamp'][11:16],
                'verdict':      row['verdict'],
                'confidence':   row['confidence'],
                'price':        row['price'],
                'volume':       row['volume'],
                'volume_ratio': row['volume_ratio'],
                'score':        row['score'],
                'reasons':      reasons,
                'justification': row['justification'],
                'risk':         row['risk'],
                'outcome_1h':   row['outcome_1h'],
                'outcome_4h':   row['outcome_4h'],
                'monitoring':   row['monitoring'],
                'closed':       row['closed'],
            })
        return jsonify(signals)
    finally:
        conn.close()


@app.route('/api/stats')
def stats():
    conn = get_db()
    try:
        c = conn.cursor()
        today = date.today().isoformat()

        # Dzisiaj
        c.execute('''
            SELECT verdict, COUNT(*) as cnt
            FROM signals WHERE timestamp LIKE ?
            GROUP BY verdict
        ''', (f'{today}%',))
        today_counts = {row['verdict']: row['cnt'] for row in c.fetchall()}

        # Performance BUY
        c.execute('''
            SELECT AVG(outcome_1h) as avg_1h,
                   AVG(outcome_4h) as avg_4h,
                   SUM(CASE WHEN outcome_1h > 0 THEN 1 ELSE 0 END) as wins,
                   COUNT(*) as cnt
            FROM signals
            WHERE verdict = 'BUY' AND outcome_1h IS NOT NULL
        ''')
        perf = c.fetchone()

        # Aktywne BUY
        c.execute('''
            SELECT COUNT(*) as cnt FROM signals
            WHERE monitoring = 1 AND closed = 0
        ''')
        active = c.fetchone()['cnt']

        # Łącznie sygnałów
        c.execute('SELECT COUNT(*) as cnt FROM signals')
        total = c.fetchone()['cnt']

        win_rate = 0
        if perf['cnt']:
            win_rate = round(perf['wins'] / perf['cnt'] * 100, 1)

        return jsonify({
            'today': {
                'buy':   today_counts.get('BUY', 0),
                'watch': today_counts.get('WATCH', 0),
                'avoid': today_counts.get('AVOID', 0),
                'total': sum(today_counts.values()),
            },
            'performance': {
                'win_rate_1h': win_rate,
                'avg_1h':      round(perf['avg_1h'] or 0, 2),
                'avg_4h':      round(perf['avg_4h'] or 0, 2),
                'total_signals': perf['cnt'],
            },
            'active_monitoring': active,
            'total_signals':     total,
            'time': now_chicago().strftime('%H:%M:%S CST'),
        })
    finally:
        conn.close()


@app.route('/api/active')
def active_signals():
    conn = get_db()
    try:
        c = conn.cursor()
        c.execute('''
            SELECT id, ticker, timestamp, price, volume_ratio,
                   justification, monitoring_end
            FROM signals
            WHERE monitoring = 1 AND closed = 0
            ORDER BY timestamp DESC
        ''')
        rows = c.fetchall()
        signals = []
        for row in rows:
            signals.append({
                'id':             row['id'],
                'ticker':         row['ticker'],
                'time':           row['timestamp'][11:16],
                'price':          row['price'],
                'volume_ratio':   row['volume_ratio'],
                'justification':  row['justification'],
                'monitoring_end': row['monitoring_end'][11:16] if row['monitoring_end'] else '—',
            })
        return jsonify(signals)
    finally:
        conn.close()


# ==================== HTML DASHBOARD ====================

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stock Scanner</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0D0F12;
    --panel:     #1A1D23;
    --border:    #2A2D35;
    --buy:       #00D4AA;
    --avoid:     #FF4D6D;
    --watch:     #FFB800;
    --text:      #E8EAF0;
    --muted:     #8B8FA8;
    --mono:      'JetBrains Mono', monospace;
    --sans:      'Inter', sans-serif;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.5;
    min-height: 100vh;
  }

  /* ---- HEADER ---- */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
  }

  .logo {
    font-family: var(--mono);
    font-size: 15px;
    font-weight: 700;
    letter-spacing: .06em;
    color: var(--buy);
  }

  .logo span { color: var(--muted); font-weight: 400; }

  #clock {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--muted);
  }

  .status-dot {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--buy);
    margin-right: 6px;
    animation: pulse 2s infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: .3; }
  }

  /* ---- LAYOUT ---- */
  .container { padding: 20px 24px; max-width: 1600px; margin: 0 auto; }

  /* ---- STAT CARDS ---- */
  .stats-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }

  .stat-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
  }

  .stat-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--muted);
    margin-bottom: 6px;
  }

  .stat-value {
    font-family: var(--mono);
    font-size: 24px;
    font-weight: 700;
    line-height: 1;
  }

  .stat-value.buy   { color: var(--buy); }
  .stat-value.watch { color: var(--watch); }
  .stat-value.avoid { color: var(--avoid); }
  .stat-value.neutral { color: var(--text); }

  .stat-sub {
    font-size: 11px;
    color: var(--muted);
    margin-top: 4px;
    font-family: var(--mono);
  }

  /* ---- SECTION HEADERS ---- */
  .section-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }

  .section-title {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: var(--muted);
    font-weight: 600;
  }

  .badge {
    font-family: var(--mono);
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 3px;
    background: var(--border);
    color: var(--muted);
  }

  /* ---- SIGNAL TABLE ---- */
  .signal-table {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--mono);
    font-size: 12px;
    margin-bottom: 24px;
  }

  .signal-table th {
    text-align: left;
    padding: 6px 10px;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    font-family: var(--sans);
    font-weight: 600;
    white-space: nowrap;
  }

  .signal-table td {
    padding: 7px 10px;
    border-bottom: 1px solid rgba(42,45,53,.5);
    vertical-align: middle;
    white-space: nowrap;
  }

  .signal-table tr:hover td { background: rgba(255,255,255,.02); }

  /* verdict pill */
  .verdict {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 8px;
    border-radius: 3px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .05em;
  }

  .verdict.BUY   { background: rgba(0,212,170,.12); color: var(--buy); }
  .verdict.WATCH { background: rgba(255,184,0,.12);  color: var(--watch); }
  .verdict.AVOID { background: rgba(255,77,109,.12); color: var(--avoid); }

  .verdict::before {
    content: '';
    width: 5px; height: 5px;
    border-radius: 50%;
    background: currentColor;
  }

  /* confidence dots */
  .conf { color: var(--muted); letter-spacing: .1em; }

  /* outcome badge */
  .outcome {
    font-size: 11px;
    padding: 1px 5px;
    border-radius: 3px;
  }
  .outcome.pos { color: var(--buy);   background: rgba(0,212,170,.1); }
  .outcome.neg { color: var(--avoid); background: rgba(255,77,109,.1); }
  .outcome.na  { color: var(--muted); }

  /* RVOL bar */
  .rvol-bar {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .rvol-track {
    width: 50px; height: 4px;
    background: var(--border);
    border-radius: 2px;
    overflow: hidden;
  }
  .rvol-fill {
    height: 100%;
    border-radius: 2px;
    background: var(--buy);
    transition: width .3s;
  }
  .rvol-fill.high { background: var(--watch); }
  .rvol-fill.vhigh { background: var(--avoid); }

  /* flags */
  .flag {
    display: inline-block;
    font-size: 10px;
    padding: 1px 5px;
    border-radius: 2px;
    margin-right: 3px;
    background: var(--border);
    color: var(--muted);
  }
  .flag.news     { background: rgba(0,212,170,.1);  color: var(--buy); }
  .flag.options  { background: rgba(255,184,0,.1);  color: var(--watch); }
  .flag.darkpool { background: rgba(102,126,234,.1); color: #667EEA; }
  .flag.earnings { background: rgba(255,77,109,.1); color: var(--avoid); }

  /* reason tooltip */
  .reason-cell {
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--muted);
    font-family: var(--sans);
    font-size: 11px;
    white-space: nowrap;
  }

  /* ---- ACTIVE MONITORING ---- */
  .active-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 10px;
    margin-bottom: 24px;
  }

  .active-card {
    background: var(--panel);
    border: 1px solid var(--buy);
    border-left: 3px solid var(--buy);
    border-radius: 6px;
    padding: 12px 14px;
  }

  .active-ticker {
    font-family: var(--mono);
    font-size: 16px;
    font-weight: 700;
    color: var(--buy);
  }

  .active-meta {
    display: flex;
    gap: 12px;
    margin-top: 4px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
  }

  .active-reason {
    margin-top: 8px;
    font-size: 11px;
    color: var(--muted);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  /* ---- EMPTY STATE ---- */
  .empty {
    text-align: center;
    padding: 40px;
    color: var(--muted);
    font-family: var(--mono);
    font-size: 12px;
  }

  /* ---- BACKGROUND $ SIGNS ---- */
  .bg-symbols {
    position: fixed;
    inset: 0;
    pointer-events: none;
    overflow: hidden;
    z-index: 0;
  }

  .bg-symbol {
    position: absolute;
    font-family: var(--mono);
    font-weight: 700;
    color: rgba(0, 212, 170, 0.03);
    user-select: none;
    line-height: 1;
  }

  .container, header { position: relative; z-index: 1; }

  /* ---- SCROLLBAR ---- */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* ---- AUTO REFRESH ---- */
  #refresh-bar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    height: 2px;
    background: var(--border);
  }
  #refresh-progress {
    height: 100%;
    background: var(--buy);
    width: 100%;
    transition: width linear;
  }
</style>
</head>
<body>

<div class="bg-symbols" id="bg-symbols"></div>

<header>
  <div class="logo">STOCK SCANNER <span>v1.0</span></div>
  <div style="display:flex;align-items:center;gap:16px">
    <span><span class="status-dot"></span><span style="color:var(--muted);font-size:11px">LIVE</span></span>
    <span id="clock" style="font-family:var(--mono)">--:--:-- CST</span>
  </div>
</header>

<div class="container">

  <!-- STAT CARDS -->
  <div class="stats-row" id="stats-row">
    <div class="stat-card">
      <div class="stat-label">BUY dziś</div>
      <div class="stat-value buy" id="stat-buy">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">WATCH dziś</div>
      <div class="stat-value watch" id="stat-watch">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">AVOID dziś</div>
      <div class="stat-value avoid" id="stat-avoid">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Monitorowane</div>
      <div class="stat-value neutral" id="stat-active">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win rate 1h</div>
      <div class="stat-value buy" id="stat-winrate">—</div>
      <div class="stat-sub" id="stat-winrate-sub"></div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Avg wynik 1h</div>
      <div class="stat-value neutral" id="stat-avg1h">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Łącznie sygnałów</div>
      <div class="stat-value neutral" id="stat-total">—</div>
    </div>
  </div>

  <!-- AKTYWNE BUY -->
  <div class="section-header">
    <span class="section-title">Aktywne BUY</span>
    <span class="badge" id="active-count">0</span>
  </div>
  <div class="active-grid" id="active-grid">
    <div class="empty">Brak aktywnych sygnałów BUY</div>
  </div>

  <!-- SYGNAŁY DZISIAJ -->
  <div class="section-header">
    <span class="section-title">Sygnały dzisiaj</span>
    <span class="badge" id="today-count">0</span>
  </div>
  <div style="overflow-x:auto">
    <table class="signal-table">
      <thead>
        <tr>
          <th>Czas</th>
          <th>Ticker</th>
          <th>Werdykt</th>
          <th>Pewność</th>
          <th>Cena</th>
          <th>RVOL</th>
          <th>Score</th>
          <th>Flags</th>
          <th>Wynik 1h</th>
          <th>Powód</th>
        </tr>
      </thead>
      <tbody id="signals-tbody">
        <tr><td colspan="10" class="empty">Ładowanie...</td></tr>
      </tbody>
    </table>
  </div>

</div>

<div id="refresh-bar"><div id="refresh-progress"></div></div>

<script>
const REFRESH = 30; // sekund

function confDots(conf) {
  const map = { 'WYSOKA': '●●●', 'ŚREDNIA': '●●○', 'NISKA': '●○○' };
  return `<span class="conf">${map[conf] || '○○○'}</span>`;
}

function outcome(val) {
  if (val === null || val === undefined) return '<span class="outcome na">—</span>';
  const cls = val > 0 ? 'pos' : 'neg';
  const sign = val > 0 ? '+' : '';
  return `<span class="outcome ${cls}">${sign}${val.toFixed(1)}%</span>`;
}

function rvolBar(ratio) {
  const pct   = Math.min(ratio / 6 * 100, 100);
  const cls   = ratio > 5 ? 'vhigh' : ratio > 3 ? 'high' : '';
  return `<div class="rvol-bar">
    <div class="rvol-track"><div class="rvol-fill ${cls}" style="width:${pct}%"></div></div>
    <span>${ratio ? ratio.toFixed(1) : '—'}x</span>
  </div>`;
}

function flags(reasons) {
  if (!reasons || !reasons.length) return '<span class="flag">—</span>';
  let html = '';
  const text = reasons.join(' ').toLowerCase();
  if (text.includes('news') || text.includes('katalizator')) html += '<span class="flag news">NEWS</span>';
  if (text.includes('options') || text.includes('uw unusual') || text.includes('call')) html += '<span class="flag options">OPT</span>';
  if (text.includes('dark pool')) html += '<span class="flag darkpool">DP</span>';
  if (text.includes('earnings')) html += '<span class="flag earnings">ERN</span>';
  if (text.includes('volume')) html += '<span class="flag">VOL</span>';
  return html || '<span class="flag">—</span>';
}

async function loadStats() {
  const r = await fetch('/api/stats');
  const d = await r.json();
  document.getElementById('stat-buy').textContent    = d.today.buy;
  document.getElementById('stat-watch').textContent  = d.today.watch;
  document.getElementById('stat-avoid').textContent  = d.today.avoid;
  document.getElementById('stat-active').textContent = d.active_monitoring;
  document.getElementById('stat-total').textContent  = d.total_signals;
  document.getElementById('clock').textContent       = d.time;

  const wr = d.performance.win_rate_1h;
  document.getElementById('stat-winrate').textContent    = wr ? wr + '%' : '—';
  document.getElementById('stat-winrate-sub').textContent = d.performance.total_signals
    ? `z ${d.performance.total_signals} BUY` : '';

  const avg = d.performance.avg_1h;
  const avgEl = document.getElementById('stat-avg1h');
  avgEl.textContent = avg ? (avg > 0 ? '+' : '') + avg + '%' : '—';
  avgEl.className   = 'stat-value ' + (avg > 0 ? 'buy' : avg < 0 ? 'avoid' : 'neutral');
}

async function loadActive() {
  const r = await fetch('/api/active');
  const signals = await r.json();
  const grid = document.getElementById('active-grid');
  document.getElementById('active-count').textContent = signals.length;

  if (!signals.length) {
    grid.innerHTML = '<div class="empty">Brak aktywnych sygnałów BUY</div>';
    return;
  }

  grid.innerHTML = signals.map(s => `
    <div class="active-card">
      <div class="active-ticker">${s.ticker}</div>
      <div class="active-meta">
        <span>${s.time} CST</span>
        <span>$${s.price}</span>
        <span>${s.volume_ratio ? s.volume_ratio.toFixed(1) + 'x' : '—'}</span>
        <span>do ${s.monitoring_end}</span>
      </div>
      <div class="active-reason">${s.justification || '—'}</div>
    </div>
  `).join('');
}

async function loadSignals() {
  const r = await fetch('/api/signals/today');
  const signals = await r.json();
  const tbody = document.getElementById('signals-tbody');
  document.getElementById('today-count').textContent = signals.length;

  if (!signals.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">Brak sygnałów dziś</td></tr>';
    return;
  }

  tbody.innerHTML = signals.map(s => `
    <tr>
      <td style="color:var(--muted)">${s.time}</td>
      <td style="font-weight:600;color:var(--text)">${s.ticker}</td>
      <td><span class="verdict ${s.verdict}">${s.verdict}</span></td>
      <td>${confDots(s.confidence)}</td>
      <td>$${s.price ? s.price.toFixed(2) : '—'}</td>
      <td>${rvolBar(s.volume_ratio)}</td>
      <td style="color:var(--muted)">${s.score || '—'}</td>
      <td>${flags(s.reasons)}</td>
      <td>${outcome(s.outcome_1h)}</td>
      <td><div class="reason-cell" title="${(s.justification || '').replace(/"/g, '&quot;')}">${s.justification || '—'}</div></td>
    </tr>
  `).join('');
}

async function refresh() {
  await Promise.all([loadStats(), loadActive(), loadSignals()]);
}

// Pasek postępu odświeżania
function startProgress() {
  const bar = document.getElementById('refresh-progress');
  bar.style.transition = 'none';
  bar.style.width = '100%';
  setTimeout(() => {
    bar.style.transition = `width ${REFRESH}s linear`;
    bar.style.width = '0%';
  }, 100);
}

// Generuj $ w tle
(function() {
  const container = document.getElementById('bg-symbols');
  const sizes = [12, 16, 20, 28, 36, 48, 64, 80, 96, 120];
  const count = 35;
  for (let i = 0; i < count; i++) {
    const el = document.createElement('span');
    el.className = 'bg-symbol';
    el.textContent = '$';
    const size = sizes[Math.floor(Math.random() * sizes.length)];
    el.style.fontSize = size + 'px';
    el.style.left = (Math.random() * 100) + '%';
    el.style.top  = (Math.random() * 100) + '%';
    // Różne odcienie — niektóre jaśniejsze
    const opacity = (Math.random() * 0.04 + 0.01).toFixed(3);
    el.style.color = `rgba(0, 212, 170, ${opacity})`;
    container.appendChild(el);
  }
})();

// Clock
setInterval(() => {
  fetch('/api/stats').then(r => r.json()).then(d => {
    document.getElementById('clock').textContent = d.time;
  }).catch(() => {});
}, 1000);

// Auto refresh
refresh();
startProgress();
setInterval(() => {
  refresh();
  startProgress();
}, REFRESH * 1000);
</script>
</body>
</html>'''


@app.route('/')
def index():
    return render_template_string(DASHBOARD_HTML)


if __name__ == '__main__':
    print("\n" + "="*50)
    print("  STOCK SCANNER DASHBOARD")
    print("  http://localhost:5000")
    print("  Ctrl+C aby zatrzymać")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False)
