// PTY harness — boots ONE UI (the real binary) over a real node-pty PTY at
// 120×40 with the fake gateway substituted via HERMES_PYTHON, drains the master
// side tightly, samples /proc/PID externally on fixture-message boundaries, and
// (optionally) wraps the UI in a cgroup-v2 scope via systemd-run.
// Methodology: docs/plans/opentui-bench-suite.md. No tmux anywhere.

import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, readSync, statSync, unlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import pty from 'node-pty'

const here = dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = resolve(here, '..')
const FAKE_GATEWAY = join(here, 'fake-gateway.mjs')

export const NODE26_BIN = process.env.BENCH_NODE_BIN
  || join(process.env.HOME ?? '', '.local/share/fnm/node-versions/v26.3.0/installation/bin/node')

const sleep = ms => new Promise(r => setTimeout(r, ms))
const now = () => Date.now()

// ── /proc readers (UI PID only — never the gateway child) ──────────────
function readProcSample(pid) {
  try {
    const rollup = readFileSync(`/proc/${pid}/smaps_rollup`, 'utf8')
    const status = readFileSync(`/proc/${pid}/status`, 'utf8')
    const stat = readFileSync(`/proc/${pid}/stat`, 'utf8')
    const kb = (text, key) => {
      const m = text.match(new RegExp(`^${key}:\\s+(\\d+) kB`, 'm'))
      return m ? Number(m[1]) : null
    }
    // stat: fields after the parenthesized comm; utime=14 stime=15 (1-indexed).
    const afterComm = stat.slice(stat.lastIndexOf(')') + 2).split(' ')
    return {
      rss_kb: kb(rollup, 'Rss'),
      pss_kb: kb(rollup, 'Pss'),
      private_dirty_kb: kb(rollup, 'Private_Dirty'),
      vmhwm_kb: kb(status, 'VmHWM'),
      utime_ticks: Number(afterComm[11]),
      stime_ticks: Number(afterComm[12])
    }
  } catch {
    return null // process gone
  }
}

function readCgroup(pid) {
  try {
    const line = readFileSync(`/proc/${pid}/cgroup`, 'utf8').trim()
    const path = line.split('::')[1]
    if (!path) return null
    return `/sys/fs/cgroup${path}`
  } catch {
    return null
  }
}

function readCgroupStats(cgPath) {
  if (!cgPath) return null
  try {
    const read = f => {
      try {
        return readFileSync(join(cgPath, f), 'utf8').trim()
      } catch {
        return null
      }
    }
    const events = read('memory.events')
    const oomKill = events ? Number(events.match(/^oom_kill (\d+)$/m)?.[1] ?? 0) : null
    return {
      current: Number(read('memory.current') ?? NaN) || null,
      peak: Number(read('memory.peak') ?? NaN) || null,
      oom_kill: oomKill
    }
  } catch {
    return null
  }
}

function childrenOf(pid) {
  try {
    return readFileSync(`/proc/${pid}/task/${pid}/children`, 'utf8').trim().split(/\s+/).filter(Boolean).map(Number)
  } catch {
    return []
  }
}

function commOf(pid) {
  try {
    return readFileSync(`/proc/${pid}/comm`, 'utf8').trim()
  } catch {
    return ''
  }
}

// ── ANSI strip for the determinism digest ──────────────────────────────
// Removes CSI/OSC/DCS/SOS/PM/APC sequences, single ESC sequences, and control
// chars, then normalizes whitespace. Good enough to compare final rendered
// transcript text across replays of the SAME UI.
export function stripAnsi(text) {
  return text
    .replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, '') // OSC
    .replace(/\x1b[PX^_][^\x1b]*\x1b\\/g, '') // DCS/SOS/PM/APC
    .replace(/\x1b\[[0-9;:<=>?]*[ -/]*[@-~]/g, '') // CSI
    .replace(/\x1b[@-Z\\-_]/g, '') // single ESC
    .replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n{2,}/g, '\n')
    .trim()
}

// Digest normalization: the final screen contains a 1Hz uptime clock (OpenTUI
// status bar `up: Ns`) whose incremental repaints trail the full post-resize
// frame. The transcript region paints deterministically; the clock does not.
// Cut everything after the composer hint (the last stable screen region) and
// normalize the uptime token inside the kept prefix.
export function normalizeForDigest(text) {
  const marker = 'Type your message'
  const idx = text.indexOf(marker)
  const head = idx >= 0 ? text.slice(0, idx + marker.length) : text
  return head.replace(/up: \d+s/g, 'up: Ns')
}

// ── env / argv composition (mirrors hermes_cli/main.py _launch_tui) ────
function composeEnv({ ui, opentuiCap, heapMb, fakeEnv, activeSessionFile }) {
  const keep = ['HOME', 'USER', 'LANG', 'LC_ALL', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'SHELL']
  const env = {}
  for (const k of keep) if (process.env[k]) env[k] = process.env[k]
  env.PATH = `${dirname(NODE26_BIN)}:/usr/bin:/bin:/usr/local/bin`
  env.TERM = 'xterm-256color'
  env.NODE_ENV = 'production'
  env.HERMES_PYTHON = FAKE_GATEWAY
  env.HERMES_PYTHON_SRC_ROOT = REPO_ROOT
  env.HERMES_CWD = REPO_ROOT
  env.HERMES_TUI_ACTIVE_SESSION_FILE = activeSessionFile
  // Launcher parity: NODE_OPTIONS carries the V8 heap cap (8192 on an
  // unconstrained host; _resolve_tui_heap_mb sizes it under a cgroup limit).
  env.NODE_OPTIONS = `--max-old-space-size=${heapMb}`
  if (ui === 'opentui') {
    env.HERMES_TUI_MOUSE = '1'
    if (opentuiCap != null) env.HERMES_TUI_MAX_MESSAGES = String(opentuiCap)
  }
  Object.assign(env, fakeEnv)
  return env
}

function uiArgv(ui) {
  if (ui === 'ink') {
    return { file: NODE26_BIN, args: ['--expose-gc', join(REPO_ROOT, 'ui-tui/dist/entry.js')], cwd: join(REPO_ROOT, 'ui-tui') }
  }
  return {
    file: NODE26_BIN,
    args: ['--experimental-ffi', '--no-warnings', join(REPO_ROOT, 'ui-opentui/dist/main.js')],
    cwd: join(REPO_ROOT, 'ui-opentui')
  }
}

// ── the scenario runner ────────────────────────────────────────────────
/**
 * opts:
 *   ui: 'ink' | 'opentui'
 *   configName: 'ink' | 'otui-capped' | 'otui-uncapped'
 *   opentuiCap: number|null            (HERMES_TUI_MAX_MESSAGES)
 *   mode: 'mem' | 'cpu-paced' | 'scroll' | 'startup' | 'digest'
 *   fixturePath, fixtureMsgs, fixtureSha
 *   memoryMax: string|null             ('2G' → systemd-run --user --scope)
 *   heapMb: number                     (--max-old-space-size)
 *   sampleEvery: number                (default 100)
 *   scroll: { hz, seconds }            (scroll mode)
 *   pacedRate: number                  (cpu-paced mode, events/s)
 *   cell, rep, outFile
 *   startDelayMs, quiesceMs, runTimeoutMs
 */
export async function runScenario(opts) {
  const {
    ui,
    configName,
    opentuiCap = null,
    mode,
    fixturePath = '',
    fixtureMsgs = 0,
    fixtureSha = '',
    memoryMax = null,
    heapMb = 8192,
    sampleEvery = 100,
    scroll = { hz: 30, seconds: 15 },
    pacedRate = 30,
    cell = 'E1',
    rep = 0,
    outFile = null,
    startDelayMs = 1500,
    quiesceMs = 800,
    runTimeoutMs = 30 * 60 * 1000
  } = opts

  const runId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
  const progressFile = join(tmpdir(), `hermes-bench-progress-${runId}.ndjson`)
  const activeSessionFile = join(tmpdir(), `hermes-bench-session-${runId}.json`)
  writeFileSync(progressFile, '')

  const fakeEnv = {
    HERMES_FAKE_FIXTURE: fixturePath,
    HERMES_FAKE_MODE: mode === 'cpu-paced' ? 'paced' : mode === 'scroll' ? 'load-then-idle' : 'burst',
    HERMES_FAKE_RATE: String(pacedRate),
    HERMES_FAKE_START_DELAY_MS: String(startDelayMs),
    HERMES_FAKE_SAMPLE_EVERY: String(sampleEvery),
    HERMES_FAKE_PROGRESS: progressFile
  }
  const env = composeEnv({ ui, opentuiCap, heapMb, fakeEnv, activeSessionFile })
  const { file, args, cwd } = uiArgv(ui)

  // Instrumented node-count runs (Ink): open fd 3 onto an NDJSON file via a
  // shell wrapper (node-pty cannot pass extra fds) and gate the in-process
  // sampler with HERMES_TUI_MEMSAMPLE_FD=3. NEVER combined with headline
  // memory runs — results carry instrumented:true.
  let nodeSampleFile = null
  let spawnFile = file
  let spawnArgs = args
  if (opts.inkNodeSampler) {
    nodeSampleFile = join(tmpdir(), `hermes-bench-nodes-${runId}.ndjson`)
    writeFileSync(nodeSampleFile, '')
    env.HERMES_TUI_MEMSAMPLE_FD = '3'
    env.HERMES_TUI_MEMSAMPLE_MS = '200'
    const quoted = [file, ...args].map(a => `'${a.replace(/'/g, `'\\''`)}'`).join(' ')
    spawnFile = '/bin/sh'
    spawnArgs = ['-c', `exec 3>>'${nodeSampleFile}'; exec ${quoted}`]
  }

  const unitName = `hermes-bench-${runId}.scope`
  if (memoryMax) {
    const innerFile = spawnFile
    const innerArgs = spawnArgs
    spawnFile = 'systemd-run'
    spawnArgs = [
      '--user',
      '--scope',
      '--quiet',
      '--collect',
      `--unit=${unitName.replace(/\.scope$/, '')}`,
      '-p',
      `MemoryMax=${memoryMax}`,
      '-p',
      'MemorySwapMax=0',
      '--',
      innerFile,
      ...innerArgs
    ]
  }

  const t0 = now()
  const term = pty.spawn(spawnFile, spawnArgs, {
    name: 'xterm-256color',
    cols: 120,
    rows: 40,
    cwd,
    env
  })
  term.resize(120, 40) // explicit TIOCSWINSZ per protocol

  // ── tight drain loop ────────────────────────────────────────────────
  let bytesOut = 0
  let dataWrites = 0
  let firstByteAt = null
  let lastDataAt = null
  const dataTimestamps = [] // for scroll latency (epoch ms of each data chunk)
  let recordDataTimestamps = false
  let tailBuf = []
  let tailLen = 0
  const TAIL_MAX = 4 * 1024 * 1024
  term.onData(d => {
    const t = now()
    bytesOut += Buffer.byteLength(d)
    dataWrites++
    if (firstByteAt === null) firstByteAt = t
    lastDataAt = t
    if (recordDataTimestamps) dataTimestamps.push(t)
    tailBuf.push(d)
    tailLen += d.length
    while (tailLen > TAIL_MAX && tailBuf.length > 1) tailLen -= tailBuf.shift().length
  })
  const resetTail = () => {
    tailBuf = []
    tailLen = 0
  }

  // Drain-starvation probe: if OUR event loop stalls, the PTY master isn't
  // being drained. 5ms cadence; any observed gap >10ms is recorded (assert).
  let maxLoopLagMs = 0
  let lagViolations = 0
  let lastTick = now()
  const lagTimer = setInterval(() => {
    const t = now()
    const lag = t - lastTick - 5
    if (lag > maxLoopLagMs) maxLoopLagMs = lag
    if (lag > 10) lagViolations++
    lastTick = t
  }, 5)

  // ── exit tracking ───────────────────────────────────────────────────
  let exited = null
  const exitPromise = new Promise(res => {
    term.onExit(({ exitCode, signal }) => {
      exited = { exitCode, signal, t: now() - t0 }
      res(exited)
    })
  })

  // ── UI PID discovery ────────────────────────────────────────────────
  // `systemd-run --scope` (and the /bin/sh sampler wrapper) EXEC the target in
  // place, so the pty child PID *is* the UI once its comm flips to 'node'.
  // Wait for that flip (scope registration / sh exec take a moment); fall back
  // to a child walk in case a future wrapper forks instead.
  let uiPid = term.pid
  if (memoryMax || opts.inkNodeSampler) {
    uiPid = null
    // Node 26 names its main thread: comm is 'node-MainThread'.
    const isNode = pid => commOf(pid).startsWith('node')
    for (let i = 0; i < 200 && !exited; i++) {
      if (isNode(term.pid)) {
        uiPid = term.pid
        break
      }
      const nodeKid = childrenOf(term.pid).find(k => isNode(k))
      if (nodeKid) {
        uiPid = nodeKid
        break
      }
      await sleep(25)
    }
  }
  // containerCap: the harness itself runs INSIDE a memory-capped container
  // (E3) — the container cgroup is the cap, no systemd-run involved.
  const cgPath = opts.containerCap ? '/sys/fs/cgroup' : memoryMax && uiPid ? readCgroup(uiPid) : null

  // ── sampling state ──────────────────────────────────────────────────
  const samples = []
  const events = []
  let lastCg = null
  let streamDone = false
  let streamStartT = null
  let doneInfo = null
  let sessionCreateAt = null
  let gwPid = null

  const takeSample = (kind, msgs, evCount) => {
    if (!uiPid) return
    const proc = readProcSample(uiPid)
    if (!proc) return
    const cg = readCgroupStats(cgPath)
    if (cg) lastCg = cg
    samples.push({
      kind,
      t_ms: now() - t0,
      msgs: msgs ?? null,
      events: evCount ?? null,
      pty_bytes: bytesOut,
      pty_writes: dataWrites,
      ...proc,
      ...(cg ? { cg_current: cg.current, cg_peak: cg.peak, cg_oom_kill: cg.oom_kill } : {})
    })
  }

  const tailProgress = (() => {
    let offset = 0
    return () => {
      let size
      try {
        size = statSync(progressFile).size
      } catch {
        return []
      }
      if (size <= offset) return []
      const fd = openSync(progressFile, 'r')
      try {
        const buf = Buffer.alloc(size - offset)
        readSync(fd, buf, 0, buf.length, offset)
        offset = size
        const out = []
        let text = buf.toString('utf8')
        const lastNl = text.lastIndexOf('\n')
        if (lastNl < text.length - 1) {
          offset -= Buffer.byteLength(text.slice(lastNl + 1), 'utf8')
          text = text.slice(0, lastNl + 1)
        }
        for (const line of text.split('\n')) {
          if (!line.trim()) continue
          try {
            out.push(JSON.parse(line))
          } catch {
            /* skip malformed */
          }
        }
        return out
      } finally {
        closeSync(fd)
      }
    }
  })()

  const handleProgress = item => {
    if (item.k === 'start') gwPid = item.pid
    if (item.k === 'req') {
      events.push({ kind: 'rpc', method: item.method, t_ms: now() - t0 })
      if (item.method === 'session.create' && sessionCreateAt === null) sessionCreateAt = now()
    }
    if (item.k === 'stream_start') streamStartT = now()
    if (item.k === 'boundary') takeSample('boundary', item.msgs, item.events)
    if (item.k === 'done') {
      streamDone = true
      doneInfo = { msgs: item.msgs, events: item.events }
      takeSample('done', item.msgs, item.events)
    }
  }

  // main poll loop driver
  let pollTimer = null
  const startPolling = () => {
    let lastPeriodic = 0
    pollTimer = setInterval(() => {
      for (const item of tailProgress()) handleProgress(item)
      const t = now()
      if (t - lastPeriodic >= 1000) {
        lastPeriodic = t
        takeSample('periodic', doneInfo?.msgs ?? null, null)
      }
    }, 25)
  }
  startPolling()

  const waitFor = async (cond, timeoutMs, pollMs = 50) => {
    const start = now()
    while (!cond()) {
      if (exited) return false
      if (now() - start > timeoutMs) return false
      await sleep(pollMs)
    }
    return true
  }

  // Wait until no PTY output for `ms`, bounded: idle-frame UIs still repaint
  // periodically (1Hz status clock), so a quiesce can never be unbounded.
  const quiesce = async (ms, maxWaitMs = 15_000) => {
    const deadline = now() + maxWaitMs
    for (;;) {
      if (exited) return
      const last = lastDataAt ?? t0
      const idle = now() - last
      if (idle >= ms) return
      if (now() > deadline) return
      await sleep(Math.min(ms - idle + 10, 200))
    }
  }

  let quitRequested = false
  const gracefulQuit = async () => {
    if (exited) return
    quitRequested = true
    try {
      term.write('\x03')
      await sleep(150)
      term.write('\x03')
    } catch {
      /* already gone */
    }
    await Promise.race([exitPromise, sleep(3000)])
    if (!exited) {
      try {
        term.kill('SIGTERM')
      } catch {
        /* ignore */
      }
      await Promise.race([exitPromise, sleep(2000)])
    }
    if (!exited) {
      try {
        term.kill('SIGKILL')
      } catch {
        /* ignore */
      }
      await Promise.race([exitPromise, sleep(2000)])
    }
  }

  // ── mode flows ──────────────────────────────────────────────────────
  const result = {}
  const scrollLatencies = []
  let digest = null
  let digestText = null

  const sessionStarted = await waitFor(() => sessionCreateAt !== null, 30_000)
  if (!sessionStarted && !exited) {
    events.push({ kind: 'error', message: 'no session.create within 30s', t_ms: now() - t0 })
  }

  if (mode === 'startup') {
    // settle: boot RPCs done + paint quiet
    await quiesce(quiesceMs)
    takeSample('final', 0, 0)
  } else {
    // wait for the stream to finish (or the UI to die — cap-hit IS a result)
    const ok = await waitFor(() => streamDone, runTimeoutMs, 100)
    if (ok) {
      await quiesce(quiesceMs)
      takeSample('final', doneInfo?.msgs ?? null, doneInfo?.events ?? null)
    }
  }

  if (mode === 'scroll' && !exited && streamDone) {
    // SGR wheel bursts at scroll.hz for scroll.seconds: first half UP, second half DOWN.
    const totalEvents = Math.round(scroll.hz * scroll.seconds)
    const interval = 1000 / scroll.hz
    const writeTimes = []
    recordDataTimestamps = true
    const cpuBefore = readProcSample(uiPid)
    const tScroll0 = now()
    for (let i = 0; i < totalEvents && !exited; i++) {
      const target = tScroll0 + i * interval
      const wait = target - now()
      if (wait > 0) await sleep(wait)
      const btn = i < totalEvents / 2 ? 64 : 65
      term.write(`\x1b[<${btn};60;20M`)
      writeTimes.push(now())
    }
    await quiesce(500)
    recordDataTimestamps = false
    const cpuAfter = readProcSample(uiPid)
    // latency: for each write, first data timestamp >= write time
    let j = 0
    for (const wt of writeTimes) {
      while (j < dataTimestamps.length && dataTimestamps[j] < wt) j++
      if (j < dataTimestamps.length) scrollLatencies.push(dataTimestamps[j] - wt)
    }
    result.scroll = {
      events_sent: writeTimes.length,
      responses: scrollLatencies.length,
      cpu_ticks: cpuBefore && cpuAfter ? cpuAfter.utime_ticks + cpuAfter.stime_ticks - cpuBefore.utime_ticks - cpuBefore.stime_ticks : null
    }
  }

  if (mode === 'digest' && !exited && streamDone) {
    // Force a full repaint via resize-jiggle, then digest the post-resize text.
    resetTail()
    term.resize(120, 39)
    await sleep(400)
    resetTail()
    term.resize(120, 40)
    await sleep(1200) // fixed window — a 1Hz status clock means true silence never comes
    digestText = normalizeForDigest(stripAnsi(tailBuf.join('')))
    digest = createHash('sha256').update(digestText).digest('hex')
  }

  await gracefulQuit()
  clearInterval(pollTimer)
  clearInterval(lagTimer)

  // cap-hit determination
  const finalCg = lastCg
  let capHit = false
  let capHitBasis = null
  if ((memoryMax || opts.containerCap) && exited) {
    if ((finalCg?.oom_kill ?? 0) > 0) {
      capHit = true
      capHitBasis = 'memory.events oom_kill'
    } else {
      // journal fallback: systemd logs OOM kills on the scope
      try {
        const log = execFileSync(
          'journalctl',
          ['--user', '-q', '--no-pager', '-u', unitName, '--since', '-30min'],
          { encoding: 'utf8' }
        )
        if (/oom|OOM/i.test(log)) {
          capHit = true
          capHitBasis = 'journalctl scope oom record'
        }
      } catch {
        /* journal unavailable */
      }
      if (!capHit && exited.signal === 9 && !streamDone) {
        capHit = true
        capHitBasis = 'SIGKILL before stream completion (inferred)'
      }
    }
  }

  const lastSample = samples[samples.length - 1] ?? null
  const summary = {
    result: capHit
      ? 'cap_hit'
      : exited && !quitRequested && mode !== 'startup'
        ? streamDone
          ? 'crashed_after_stream'
          : 'died'
        : 'completed',
    cap_hit: capHit,
    cap_hit_basis: capHitBasis,
    at_messages: capHit ? (samples.filter(s => s.kind === 'boundary').at(-1)?.msgs ?? null) : null,
    exit: exited,
    stream_done: streamDone,
    msgs_streamed: doneInfo?.msgs ?? samples.filter(s => s.kind === 'boundary').at(-1)?.msgs ?? 0,
    events_streamed: doneInfo?.events ?? null,
    pty_bytes_total: bytesOut,
    pty_data_callbacks: dataWrites,
    first_byte_ms: firstByteAt ? firstByteAt - t0 : null,
    session_create_ms: sessionCreateAt ? sessionCreateAt - t0 : null,
    stream_start_ms: streamStartT ? streamStartT - t0 : null,
    vmhwm_kb: lastSample?.vmhwm_kb ?? null,
    cg_peak: finalCg?.peak ?? null,
    drain_max_loop_lag_ms: maxLoopLagMs,
    drain_lag_violations: lagViolations,
    drain_ok: lagViolations === 0,
    digest,
    scroll_latencies_ms: scrollLatencies.length ? scrollLatencies : undefined,
    ...result
  }

  const out = {
    meta: {
      cell,
      ui,
      config: configName,
      mode,
      rep,
      run_id: runId,
      utc: new Date(t0).toISOString(),
      sha: gitSha(),
      node: NODE26_BIN,
      node_version: nodeVersion(),
      pty: { cols: 120, rows: 40, term: 'xterm-256color' },
      heap_mb: heapMb,
      memory_max: memoryMax,
      container_cap: Boolean(opts.containerCap),
      container_memory: opts.containerMemory ?? null,
      opentui_cap: opentuiCap,
      fixture: { path: fixturePath, msgs: fixtureMsgs, sha256: fixtureSha },
      sample_every: sampleEvery,
      mode_params: mode === 'cpu-paced' ? { rate: pacedRate } : mode === 'scroll' ? scroll : {},
      ui_pid: uiPid,
      gw_pid: gwPid,
      cgroup: cgPath,
      load_avg_at_start: loadAvg(),
      instrumented: Boolean(opts.inkNodeSampler)
    },
    samples,
    events,
    summary
  }
  if (digestText !== null) out.digest_text = digestText
  // Postmortem: keep the stripped tail of the PTY stream for any run that
  // didn't complete cleanly (crash diagnostics — small, bounded).
  if (summary.result !== 'completed') out.pty_tail = stripAnsi(tailBuf.join('')).slice(-4000)
  if (nodeSampleFile) {
    try {
      out.node_samples = readFileSync(nodeSampleFile, 'utf8')
        .split('\n')
        .filter(Boolean)
        .map(l => JSON.parse(l))
    } catch {
      out.node_samples = []
    }
    try {
      unlinkSync(nodeSampleFile)
    } catch {
      /* ignore */
    }
  }

  try {
    unlinkSync(progressFile)
  } catch {
    /* ignore */
  }
  try {
    unlinkSync(activeSessionFile)
  } catch {
    /* ignore */
  }

  if (outFile) {
    mkdirSync(dirname(outFile), { recursive: true })
    writeFileSync(outFile, JSON.stringify(out, null, 1))
  }
  return out
}

let _sha = null
function gitSha() {
  if (_sha) return _sha
  try {
    _sha = execFileSync('git', ['-C', REPO_ROOT, 'rev-parse', '--short', 'HEAD'], { encoding: 'utf8' }).trim()
  } catch {
    _sha = 'unknown'
  }
  return _sha
}

function nodeVersion() {
  try {
    return execFileSync(NODE26_BIN, ['--version'], { encoding: 'utf8' }).trim()
  } catch {
    return 'unknown'
  }
}

export function loadAvg() {
  try {
    return readFileSync('/proc/loadavg', 'utf8').split(' ').slice(0, 3).map(Number)
  } catch {
    return null
  }
}

export function fixtureCacheDir() {
  const dir = join(here, '.cache')
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true })
  return dir
}
