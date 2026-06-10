#!/usr/bin/env node
// Report renderer — reads bench/results/*.json and emits ONE self-contained
// bench/report.html (inline SVG, zero CDN/network) plus PNG exports of each
// chart to bench/report-assets/ (rasterized with the resvg-js available at
// ~/.claude/skills/tmux-pane-screenshot/scripts/node_modules — see README).
// Real data only: cells with no results render as "not run".

import { createRequire } from 'node:module'
import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const RESULTS_DIR = join(here, 'results')
const ASSETS_DIR = join(here, 'report-assets')
const OUT_HTML = join(here, 'report.html')

const CAP_MB = 2048

// ── data load ───────────────────────────────────────────────────────────
function loadResults() {
  let files = []
  try {
    files = readdirSync(RESULTS_DIR).filter(f => f.endsWith('.json'))
  } catch {
    return []
  }
  const out = []
  for (const f of files.sort()) {
    try {
      const r = JSON.parse(readFileSync(join(RESULTS_DIR, f), 'utf8'))
      r._file = f
      out.push(r)
    } catch {
      /* skip unparseable */
    }
  }
  return out
}

// ── stats ───────────────────────────────────────────────────────────────
const quantile = (sorted, q) => {
  if (!sorted.length) return null
  const pos = (sorted.length - 1) * q
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}
const median = xs => quantile(xs.slice().sort((a, b) => a - b), 0.5)
const iqr = xs => {
  const s = xs.slice().sort((a, b) => a - b)
  return [quantile(s, 0.25), quantile(s, 0.75)]
}
const fmt = (x, d = 1) => (x === null || x === undefined || Number.isNaN(x) ? '—' : Number(x).toFixed(d))
const fmtMedIqr = (xs, d = 1) => {
  if (!xs.length) return 'not run'
  const [lo, hi] = iqr(xs)
  return `${fmt(median(xs), d)} <span class="iqr">[${fmt(lo, d)}–${fmt(hi, d)}]</span>`
}

// least-squares slope of rss_mb vs msgs over points
function lsSlope(points) {
  if (points.length < 3) return null
  const n = points.length
  let sx = 0
  let sy = 0
  let sxx = 0
  let sxy = 0
  for (const [x, y] of points) {
    sx += x
    sy += y
    sxx += x * x
    sxy += x * y
  }
  const denom = n * sxx - sx * sx
  if (denom === 0) return null
  return (n * sxy - sx * sy) / denom // MB per message
}

// Per-run back-half slope (MB/1k msgs): fit over msgs >= max(500, maxMsgs/2)
// — warmup (first 500 msgs) always excluded per protocol.
function runSlope(run) {
  const pts = run.samples
    .filter(s => s.kind === 'boundary' && s.msgs != null && s.rss_kb != null)
    .map(s => [s.msgs, s.rss_kb / 1024])
  if (pts.length < 4) return null
  const maxMsgs = pts[pts.length - 1][0]
  const cut = Math.max(500, maxMsgs / 2)
  const back = pts.filter(([x]) => x >= cut)
  const slope = lsSlope(back)
  return slope === null ? null : slope * 1000
}

// plateau: median RSS over the final quartile of boundary samples
function runPlateau(run) {
  const pts = run.samples.filter(s => s.kind === 'boundary' && s.rss_kb != null).map(s => s.rss_kb / 1024)
  if (pts.length < 4) return null
  return median(pts.slice(Math.floor(pts.length * 0.75)))
}

// ── SVG primitives ──────────────────────────────────────────────────────
const COLORS = { ink: '#e06c75', 'otui-capped': '#61afef', 'otui-uncapped': '#56b6c2', other: '#c678dd' }
const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

function chart({ title, w = 860, h = 440, xLabel, yLabel, xMax, yMax, series, capLine, markers = [], note }) {
  const padL = 64
  const padR = 16
  const padT = 40
  const padB = 64
  const pw = w - padL - padR
  const ph = h - padT - padB
  const X = x => padL + (x / xMax) * pw
  const Y = y => padT + ph - (y / yMax) * ph
  const parts = []
  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" font-family="ui-monospace,monospace">`)
  parts.push(`<rect width="${w}" height="${h}" fill="#11151c"/>`)
  parts.push(`<text x="${padL}" y="24" fill="#e8eaf0" font-size="15" font-weight="bold">${esc(title)}</text>`)
  // grid + axes
  const xticks = 6
  const yticks = 5
  for (let i = 0; i <= xticks; i++) {
    const xv = (xMax / xticks) * i
    const x = X(xv)
    parts.push(`<line x1="${x}" y1="${padT}" x2="${x}" y2="${padT + ph}" stroke="#262c38" stroke-width="1"/>`)
    parts.push(`<text x="${x}" y="${padT + ph + 18}" fill="#8b93a7" font-size="11" text-anchor="middle">${Math.round(xv)}</text>`)
  }
  for (let i = 0; i <= yticks; i++) {
    const yv = (yMax / yticks) * i
    const y = Y(yv)
    parts.push(`<line x1="${padL}" y1="${y}" x2="${padL + pw}" y2="${y}" stroke="#262c38" stroke-width="1"/>`)
    parts.push(`<text x="${padL - 8}" y="${y + 4}" fill="#8b93a7" font-size="11" text-anchor="end">${Math.round(yv)}</text>`)
  }
  parts.push(`<text x="${padL + pw / 2}" y="${h - 26}" fill="#aab2c5" font-size="12" text-anchor="middle">${esc(xLabel)}</text>`)
  parts.push(
    `<text x="16" y="${padT + ph / 2}" fill="#aab2c5" font-size="12" text-anchor="middle" transform="rotate(-90 16 ${padT + ph / 2})">${esc(yLabel)}</text>`
  )
  if (capLine != null && capLine <= yMax) {
    parts.push(
      `<line x1="${padL}" y1="${Y(capLine)}" x2="${padL + pw}" y2="${Y(capLine)}" stroke="#e5c07b" stroke-width="1.5" stroke-dasharray="7 5"/>`
    )
    parts.push(`<text x="${padL + pw - 4}" y="${Y(capLine) - 6}" fill="#e5c07b" font-size="11" text-anchor="end">cap ${capLine} MB</text>`)
  }
  let legendY = padT + 8
  for (const s of series) {
    if (!s.points.length) continue
    const d = s.points.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${X(Math.min(x, xMax)).toFixed(1)},${Y(Math.min(y, yMax)).toFixed(1)}`).join(' ')
    parts.push(`<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width ?? 2}" opacity="${s.opacity ?? 1}"/>`)
    if (s.label) {
      parts.push(`<rect x="${padL + pw - 190}" y="${legendY - 9}" width="12" height="3" fill="${s.color}"/>`)
      parts.push(`<text x="${padL + pw - 172}" y="${legendY - 3}" fill="#cdd3e0" font-size="11">${esc(s.label)}</text>`)
      legendY += 16
    }
  }
  for (const m of markers) {
    const x = X(Math.min(m.x, xMax))
    const y = Y(Math.min(m.y, yMax))
    parts.push(`<text x="${x}" y="${y + 5}" fill="${m.color ?? '#e5c07b'}" font-size="16" text-anchor="middle" font-weight="bold">×</text>`)
    if (m.label) parts.push(`<text x="${x}" y="${y - 10}" fill="${m.color ?? '#e5c07b'}" font-size="10" text-anchor="middle">${esc(m.label)}</text>`)
  }
  if (note) parts.push(`<text x="${padL}" y="${h - 8}" fill="#6f7689" font-size="10">${esc(note)}</text>`)
  parts.push('</svg>')
  return parts.join('\n')
}

function barChart({ title, w = 860, h = 360, groups, yLabel, note }) {
  // groups: [{label, bars: [{name, value, lo, hi, color}]}]
  const padL = 64
  const padR = 16
  const padT = 40
  const padB = 64
  const pw = w - padL - padR
  const ph = h - padT - padB
  const vals = groups.flatMap(g => g.bars.map(b => b.hi ?? b.value)).filter(v => v != null)
  if (!vals.length) return null
  const yMax = Math.max(...vals) * 1.15
  const Y = y => padT + ph - (y / yMax) * ph
  const parts = []
  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" font-family="ui-monospace,monospace">`)
  parts.push(`<rect width="${w}" height="${h}" fill="#11151c"/>`)
  parts.push(`<text x="${padL}" y="24" fill="#e8eaf0" font-size="15" font-weight="bold">${esc(title)}</text>`)
  for (let i = 0; i <= 5; i++) {
    const yv = (yMax / 5) * i
    parts.push(`<line x1="${padL}" y1="${Y(yv)}" x2="${padL + pw}" y2="${Y(yv)}" stroke="#262c38"/>`)
    parts.push(`<text x="${padL - 8}" y="${Y(yv) + 4}" fill="#8b93a7" font-size="11" text-anchor="end">${yv >= 100 ? Math.round(yv) : yv.toFixed(1)}</text>`)
  }
  parts.push(
    `<text x="16" y="${padT + ph / 2}" fill="#aab2c5" font-size="12" text-anchor="middle" transform="rotate(-90 16 ${padT + ph / 2})">${esc(yLabel)}</text>`
  )
  const gw = pw / groups.length
  groups.forEach((g, gi) => {
    const bw = Math.min(46, (gw - 24) / Math.max(1, g.bars.length))
    g.bars.forEach((b, bi) => {
      if (b.value == null) return
      const x = padL + gi * gw + gw / 2 - (g.bars.length * bw) / 2 + bi * bw
      parts.push(`<rect x="${x + 3}" y="${Y(b.value)}" width="${bw - 6}" height="${padT + ph - Y(b.value)}" fill="${b.color}" opacity="0.85"/>`)
      if (b.lo != null && b.hi != null) {
        const cx = x + bw / 2
        parts.push(`<line x1="${cx}" y1="${Y(b.lo)}" x2="${cx}" y2="${Y(b.hi)}" stroke="#e8eaf0" stroke-width="1.5"/>`)
      }
      parts.push(`<text x="${x + bw / 2}" y="${Y(b.value) - 5}" fill="#cdd3e0" font-size="10" text-anchor="middle">${b.value >= 100 ? Math.round(b.value) : b.value.toFixed(1)}</text>`)
      parts.push(`<text x="${x + bw / 2}" y="${padT + ph + 16}" fill="#8b93a7" font-size="10" text-anchor="middle">${esc(b.name)}</text>`)
    })
    parts.push(`<text x="${padL + gi * gw + gw / 2}" y="${padT + ph + 34}" fill="#aab2c5" font-size="11" text-anchor="middle">${esc(g.label)}</text>`)
  })
  if (note) parts.push(`<text x="${padL}" y="${h - 8}" fill="#6f7689" font-size="10">${esc(note)}</text>`)
  parts.push('</svg>')
  return parts.join('\n')
}

// ── chart builders ──────────────────────────────────────────────────────
function rssChart(results) {
  const runs = results.filter(
    r => (r.meta.cell.startsWith('mem') || r.meta.cell.startsWith('slope')) && !r.meta.instrumented && r.meta.mode === 'mem'
  )
  if (!runs.length) return null
  let xMax = 0
  let yMax = CAP_MB * 1.05
  const series = []
  const markers = []
  const seen = new Set()
  for (const r of runs) {
    const pts = r.samples.filter(s => s.kind === 'boundary' && s.msgs != null && s.rss_kb != null).map(s => [s.msgs, s.rss_kb / 1024])
    if (!pts.length) continue
    xMax = Math.max(xMax, pts[pts.length - 1][0])
    yMax = Math.max(yMax, ...pts.map(p => p[1]))
    const color = COLORS[r.meta.config] ?? COLORS.other
    const key = r.meta.config
    series.push({ points: pts, color, width: r.meta.cell.startsWith('slope') ? 2.5 : 1.5, opacity: 0.8, label: seen.has(key) ? null : `${key}` })
    seen.add(key)
    if (r.summary.cap_hit) {
      const last = pts[pts.length - 1]
      markers.push({ x: last[0], y: last[1], label: `OOM @${last[0]}`, color: '#e5c07b' })
    } else if (r.summary.result === 'died' || r.summary.result === 'crashed_after_stream') {
      const last = pts[pts.length - 1]
      markers.push({ x: last[0], y: last[1], label: `crash @${last[0]}`, color: '#e06c75' })
    }
  }
  return chart({
    title: 'RSS vs fixture messages (clean memory runs, all reps + slope runs)',
    xLabel: 'fixture messages (rowsPerTurn accounting)',
    yLabel: 'RSS (MB)',
    xMax: Math.max(xMax, 1000),
    yMax: yMax * 1.08,
    series,
    capLine: CAP_MB,
    markers,
    note: '2GB cgroup cap (systemd-run, MemorySwapMax=0). × = cgroup OOM kill. Samples every 100 msgs from /proc/PID/smaps_rollup.'
  })
}

function nodesChart(results) {
  const runs = results.filter(r => r.meta.cell.startsWith('nodes'))
  if (!runs.length) return null
  const series = []
  let xMax = 0
  let yMax = 0
  for (const r of runs) {
    let pts = []
    if (r.node_samples?.length) {
      // Ink fd-3 sampler: align wall-clock node samples to msg boundaries.
      const t0 = Date.parse(r.meta.utc)
      const bounds = r.samples.filter(s => s.kind === 'boundary' && s.msgs != null)
      pts = r.node_samples.map(ns => {
        const el = ns.t - t0
        let msgs = 0
        for (const b of bounds) if (b.t_ms <= el) msgs = b.msgs
        return [msgs, ns.yoga]
      })
      // collapse to last sample per msg count
      const byMsg = new Map()
      for (const [m, y] of pts) byMsg.set(m, y)
      pts = [...byMsg.entries()].sort((a, b) => a[0] - b[0])
      series.push({ points: pts, color: COLORS.ink, label: 'ink live yoga nodes (fd-3 sampler)' })
    } else if (r.samples.some(s => s.renderables != null)) {
      pts = r.samples.filter(s => s.renderables != null).map(s => [s.msgs, s.renderables])
      series.push({
        points: pts,
        color: COLORS[r.meta.config] ?? COLORS.other,
        label: `${r.meta.config} renderables (headless walk)`
      })
    }
    for (const [x, y] of pts) {
      xMax = Math.max(xMax, x)
      yMax = Math.max(yMax, y)
    }
  }
  if (!series.length) return null
  return chart({
    title: 'Mounted node count vs messages (instrumented runs — mechanism witness, never headlined)',
    xLabel: 'fixture messages',
    yLabel: 'live nodes',
    xMax: Math.max(xMax, 100),
    yMax: yMax * 1.1,
    series,
    note: 'instrumented:true. Ink: HERMES_TUI_MEMSAMPLE_FD walk of the forked reconciler root. OpenTUI: scripts/mem-bench.tsx renderable walk (headless, diagnostic-only).'
  })
}

function scrollCdfChart(results) {
  const runs = results.filter(r => r.meta.cell.startsWith('scroll') && r.summary.scroll_latencies_ms?.length)
  if (!runs.length) return null
  const transcriptMsgs = runs[0].meta.fixture?.msgs ?? '?'
  const byConfig = {}
  for (const r of runs) {
    ;(byConfig[r.meta.config] ??= []).push(...r.summary.scroll_latencies_ms)
  }
  const series = []
  let xMax = 0
  for (const [config, lats] of Object.entries(byConfig)) {
    const s = lats.slice().sort((a, b) => a - b)
    xMax = Math.max(xMax, quantile(s, 0.995))
    const pts = s.map((v, i) => [v, ((i + 1) / s.length) * 100])
    series.push({ points: pts, color: COLORS[config] ?? COLORS.other, label: `${config} (n=${s.length})` })
  }
  return chart({
    title: `Scroll latency CDF — SGR wheel 30Hz×15s on ${transcriptMsgs}-msg transcript (all reps pooled)`,
    xLabel: 'write → first output byte (ms)',
    yLabel: 'percentile (%)',
    xMax: Math.max(1, xMax),
    yMax: 100,
    series,
    note: 'Latency = PTY write of the wheel event to the next PTY output chunk.'
  })
}

function startupChart(results) {
  const runs = results.filter(r => r.meta.cell === 'startup')
  if (!runs.length) return null
  const byConfig = {}
  for (const r of runs) {
    const c = (byConfig[r.meta.config] ??= { fb: [], sc: [] })
    if (r.summary.first_byte_ms != null) c.fb.push(r.summary.first_byte_ms)
    if (r.summary.session_create_ms != null) c.sc.push(r.summary.session_create_ms)
  }
  const groups = Object.entries(byConfig).map(([config, v]) => ({
    label: config,
    bars: [
      { name: 'first byte', value: median(v.fb), lo: iqr(v.fb)[0], hi: iqr(v.fb)[1], color: COLORS[config] ?? COLORS.other },
      { name: 'session.create', value: median(v.sc), lo: iqr(v.sc)[0], hi: iqr(v.sc)[1], color: '#98c379' }
    ]
  }))
  return barChart({
    title: 'Startup (fake gateway) — median of 10 reps, whiskers = IQR',
    yLabel: 'ms from spawn',
    groups,
    note: 'first byte = first PTY output; session.create = the UI reaches its session bootstrap RPC.'
  })
}

function ptyRateChart(results) {
  const runs = results.filter(r => r.meta.cell.startsWith('cpu') && r.summary.stream_done)
  if (!runs.length) return null
  const byConfig = {}
  for (const r of runs) {
    const done = r.samples.filter(s => s.kind === 'done')[0]
    const start = r.summary.stream_start_ms
    if (!done || start == null) continue
    const secs = (done.t_ms - start) / 1000
    const c = (byConfig[r.meta.config] ??= { rate: [], cpu: [] })
    c.rate.push(done.pty_bytes / secs / 1024)
    // CPU ms per event over the streaming window
    const sb = r.samples.filter(s => s.kind === 'boundary')
    if (sb.length >= 2) {
      const first = sb[0]
      const last = sb[sb.length - 1]
      const ticks = last.utime_ticks + last.stime_ticks - first.utime_ticks - first.stime_ticks
      const events = (last.events ?? 0) - (first.events ?? 0)
      if (events > 0) c.cpu.push((ticks * 10) / events) // 100Hz ticks → ms
    }
  }
  const groups = Object.entries(byConfig).map(([config, v]) => ({
    label: config,
    bars: [
      { name: 'PTY KiB/s', value: median(v.rate), lo: iqr(v.rate)[0], hi: iqr(v.rate)[1], color: COLORS[config] ?? COLORS.other },
      { name: 'CPU ms/event', value: median(v.cpu), lo: iqr(v.cpu)[0], hi: iqr(v.cpu)[1], color: '#d19a66' }
    ]
  }))
  return barChart({
    title: 'Paced streaming (30 ev/s): PTY output rate + CPU per event — median±IQR over reps',
    yLabel: 'KiB/s · ms/event',
    groups,
    note: 'CPU from /proc/PID/stat utime+stime deltas across the stream window (UI process only).'
  })
}

// ── tables ──────────────────────────────────────────────────────────────
function matrixTable(results) {
  const memRuns = results.filter(r => r.meta.cell.startsWith('mem') && r.meta.mode === 'mem' && !r.meta.instrumented)
  const slopeRuns = results.filter(r => r.meta.cell.startsWith('slope'))
  const scrollRuns = results.filter(r => r.meta.cell.startsWith('scroll'))
  const cpuRuns = results.filter(r => r.meta.cell.startsWith('cpu'))
  const configs = ['ink', 'otui-capped', 'otui-uncapped']
  const rows = []
  for (const config of configs) {
    const mem = memRuns.filter(r => r.meta.config === config)
    const slopes = mem.map(runSlope).filter(s => s != null)
    const plateaus = mem.map(runPlateau).filter(s => s != null)
    const vmhwm = mem.map(r => r.summary.vmhwm_kb).filter(Boolean).map(k => k / 1024)
    const slope10k = slopeRuns.filter(r => r.meta.config === config).map(runSlope).filter(s => s != null)
    const lat = scrollRuns.filter(r => r.meta.config === config).flatMap(r => r.summary.scroll_latencies_ms ?? [])
    const latS = lat.slice().sort((a, b) => a - b)
    const cpu = []
    for (const r of cpuRuns.filter(x => x.meta.config === config)) {
      const sb = r.samples.filter(s => s.kind === 'boundary')
      if (sb.length >= 2) {
        const f = sb[0]
        const l = sb[sb.length - 1]
        const ev = (l.events ?? 0) - (f.events ?? 0)
        if (ev > 0) cpu.push(((l.utime_ticks + l.stime_ticks - f.utime_ticks - f.stime_ticks) * 10) / ev)
      }
    }
    const capHits = [...mem, ...slopeRuns.filter(r => r.meta.config === config)].filter(r => r.summary.cap_hit)
    rows.push(`<tr>
      <td><b style="color:${COLORS[config]}">${config}</b></td>
      <td>${fmtMedIqr(slopes, 2)}</td>
      <td>${slope10k.length ? fmt(median(slope10k), 2) : 'not run'}</td>
      <td>${fmtMedIqr(plateaus, 0)}</td>
      <td>${fmtMedIqr(vmhwm, 0)}</td>
      <td>${latS.length ? `${fmt(quantile(latS, 0.5), 1)} / ${fmt(quantile(latS, 0.9), 1)} / ${fmt(quantile(latS, 0.99), 1)}` : 'not run'}</td>
      <td>${fmtMedIqr(cpu, 2)}</td>
      <td>${capHits.length ? capHits.map(r => `${r.meta.cell}@${r.summary.at_messages ?? '?'}msgs`).join('<br>') : mem.length ? 'none' : 'not run'}</td>
    </tr>`)
  }
  return `<table>
    <tr><th>config</th><th>slope MB/1k msgs<br>(3000-msg runs)</th><th>slope MB/1k<br>(10k run)</th><th>plateau RSS MB<br>(final quartile)</th><th>VmHWM MB</th><th>scroll p50/p90/p99 ms</th><th>CPU ms/event<br>(paced)</th><th>cap hits (2GB)</th></tr>
    ${rows.join('\n')}
  </table>`
}

function survivalTable(results) {
  const runs = results.filter(r => r.meta.cell.startsWith('e3'))
  if (!runs.length) return '<p class="notrun">E3 (memory-constrained Docker survival): not run.</p>'
  const rows = runs.map(
    r => `<tr><td>${esc(r.meta.cell)}</td><td>${esc(r.meta.config)}</td><td>${esc(String(r.meta.memory_max ?? r.meta.container_memory ?? '?'))}</td>
    <td>${r.summary.result}</td><td>${r.summary.at_messages ?? r.summary.msgs_streamed ?? '—'}</td>
    <td>${fmt((r.summary.vmhwm_kb ?? 0) / 1024, 0)} MB</td><td>${esc(r.summary.cap_hit_basis ?? '—')}</td></tr>`
  )
  return `<table><tr><th>cell</th><th>config</th><th>limit</th><th>result</th><th>msgs survived</th><th>VmHWM</th><th>basis</th></tr>${rows.join('')}</table>`
}

function gateTable(results) {
  const runs = results.filter(r => r.meta.cell === 'gate')
  if (!runs.length) return '<p class="notrun">Determinism gate: not run.</p>'
  const byConfig = {}
  for (const r of runs) (byConfig[r.meta.config] ??= []).push(r.summary.digest)
  const rows = Object.entries(byConfig).map(([c, ds]) => {
    const ok = ds.length >= 2 && ds.every(d => d && d === ds[0])
    return `<tr><td>${esc(c)}</td><td>${ds.map(d => (d ?? '∅').slice(0, 16)).join(' · ')}</td><td style="color:${ok ? '#98c379' : '#e06c75'}">${ok ? 'PASS' : 'FAIL'}</td></tr>`
  })
  return `<table><tr><th>config</th><th>replay digests</th><th>gate</th></tr>${rows.join('')}</table>`
}

function drainTable(results) {
  const bad = results.filter(r => r.summary && r.summary.drain_ok === false)
  if (!bad.length) return '<p>PTY drain assertion: no run exceeded the 10ms event-loop starvation budget.</p>'
  const rows = bad.map(
    r => `<tr><td>${esc(r._file)}</td><td>${fmt(r.summary.drain_max_loop_lag_ms, 0)} ms</td><td>${r.summary.drain_lag_violations}</td></tr>`
  )
  return `<p style="color:#e5c07b">⚠ drain assertion violations (runs kept, flagged):</p>
  <table><tr><th>run</th><th>max loop lag</th><th>violations &gt;10ms</th></tr>${rows.join('')}</table>`
}

// ── PNG export ──────────────────────────────────────────────────────────
function exportPng(name, svg) {
  try {
    const require2 = createRequire(import.meta.url)
    const resvgPath = join(homedir(), '.claude/skills/tmux-pane-screenshot/scripts/node_modules/@resvg/resvg-js')
    const { Resvg } = require2(resvgPath)
    const png = new Resvg(svg, { fitTo: { mode: 'width', value: 1280 } }).render().asPng()
    writeFileSync(join(ASSETS_DIR, `${name}.png`), png)
    return true
  } catch (e) {
    process.stderr.write(`png export failed for ${name}: ${e.message}\n`)
    return false
  }
}

// ── main ────────────────────────────────────────────────────────────────
const results = loadResults()
mkdirSync(ASSETS_DIR, { recursive: true })

const charts = [
  ['rss-vs-msgs', rssChart(results)],
  ['node-count', nodesChart(results)],
  ['scroll-cdf', scrollCdfChart(results)],
  ['startup', startupChart(results)],
  ['pty-rate', ptyRateChart(results)]
]

const pngs = []
for (const [name, svg] of charts) {
  if (svg && exportPng(name, svg)) pngs.push(`${name}.png`)
}

const metaRuns = results.length
  ? `${results.length} result files · sha ${esc(results[0].meta.sha ?? '?')} · node ${esc(results.find(r => r.meta.node_version)?.meta.node_version ?? '?')}`
  : 'no results yet'

const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Hermes TUI bench — Ink vs OpenTUI</title>
<style>
  body { background:#0b0e14; color:#cdd3e0; font-family:ui-monospace,SFMono-Regular,monospace; margin:0; padding:32px; max-width:980px; margin-inline:auto; }
  h1 { color:#e8eaf0; font-size:22px; } h2 { color:#e8eaf0; font-size:16px; margin-top:40px; border-bottom:1px solid #262c38; padding-bottom:6px;}
  table { border-collapse:collapse; margin:12px 0; font-size:12px; width:100%; }
  th,td { border:1px solid #262c38; padding:6px 10px; text-align:left; vertical-align:top;}
  th { background:#161b24; color:#aab2c5; }
  .iqr { color:#6f7689; font-size:10px; }
  .notrun { color:#6f7689; font-style:italic; }
  svg { margin:12px 0; border-radius:6px; }
  p, li { font-size:13px; line-height:1.5; }
  code { color:#98c379; }
</style></head><body>
<h1>Hermes TUI benchmark — Ink (ui-tui) vs OpenTUI (ui-opentui)</h1>
<p>${metaRuns} · generated ${new Date().toISOString()}<br>
Methodology: <code>docs/plans/opentui-bench-suite.md</code>. Real binaries over a real node-pty PTY (120×40,
xterm-256color), fake gateway via <code>HERMES_PYTHON</code> (zero UI changes), external /proc sampling on
100-msg boundaries, 2GB cgroup-v2 caps via <code>systemd-run --user --scope</code>. Median±IQR throughout;
instrumented node-count runs are flagged and never headlined.</p>

<h2>Determinism gate</h2>
${gateTable(results)}

<h2>Headline: RSS vs messages</h2>
${charts[0][1] ?? '<p class="notrun">not run</p>'}

<h2>Result matrix (median ± IQR)</h2>
${matrixTable(results)}

<h2>Mechanism: mounted node count (instrumented)</h2>
${charts[1][1] ?? '<p class="notrun">not run</p>'}

<h2>Scroll latency</h2>
${charts[2][1] ?? '<p class="notrun">not run</p>'}

<h2>Startup</h2>
${charts[3][1] ?? '<p class="notrun">not run</p>'}

<h2>Streaming CPU / PTY throughput</h2>
${charts[4][1] ?? '<p class="notrun">not run</p>'}

<h2>E3 survival (memory-constrained Docker)</h2>
${survivalTable(results)}

<h2>Run health</h2>
${drainTable(results)}

</body></html>
`

writeFileSync(OUT_HTML, html)
process.stdout.write(`report → ${OUT_HTML}\npngs → ${pngs.join(', ') || '(none)'}\n`)
