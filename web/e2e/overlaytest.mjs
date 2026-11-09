// Headless Chrome test for 前端画框 alignment: how far drawn boxes are from the true boxes of the
// frame on screen.
// usage: node e2e/overlaytest.mjs <chrome-bin> [stream=ped] [seconds=20]
// Needs a running system (API :8090, ZLM, a live stream with AI).
//
// Truth: the worker's result for exactly the frame on screen (message r == the frame's RTP
// timestamp, or z == its fMP4 media time). For every repaint we compare the drawn boxes with the
// truth of the frame shown at that moment (box centers, in pixels of the video). Runs:
//   webrtc / fmp4  x  by frame stamp / old delay estimate (WebRTC only)  x  normal / WebSocket jitter
// Jitter: messages are held back in order (like a TCP stall): 80% +0-50 ms, 20% +300-800 ms.
import puppeteer from 'puppeteer-core'

const [bin, stream = 'ped', secs = '20'] = process.argv.slice(2)
const BASE = process.env.BASE || 'http://127.0.0.1:8090/'
const sleep = ms => new Promise(r => setTimeout(r, ms))

const browser = await puppeteer.launch({
  executablePath: bin, headless: 'new',
  args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required', '--no-proxy-server'],
  defaultViewport: { width: 1280, height: 800 },
})

async function run(mode, forceTime, jitter) {
  const page = await browser.newPage()
  await page.evaluateOnNewDocument(jitter => {
    window.__msgs = []
    // TARGET=ms: hold WebRTC frames that long (experiment)
    const target = Number(new URLSearchParams(location.search).get('target') || 0)
    if (target) {
      const PC = window.RTCPeerConnection
      window.RTCPeerConnection = class extends PC {
        constructor(...a) { super(...a); this.addEventListener('track', e => { e.receiver.jitterBufferTarget = target }) }
      }
    }
    const Native = window.WebSocket
    window.WebSocket = class extends Native {
      set onmessage(fn) {
        let last = 0
        super.onmessage = ev => {
          try {
            const m = JSON.parse(ev.data)
            if (m.type === 'tracks') window.__msgs.push({ ...m.data, recv: performance.now() })
          } catch { /* not json */ }
          if (!jitter) return fn(ev)
          const extra = Math.random() < 0.2 ? 300 + Math.random() * 500 : Math.random() * 50
          last = Math.max(last, performance.now() + extra)     // in order, like TCP
          setTimeout(() => fn(ev), last - performance.now())
        }
      }
    }
  }, jitter)
  await page.goto(BASE + (process.env.TARGET ? `?target=${process.env.TARGET}` : '') + '#/wall', { waitUntil: 'networkidle2' })
  const dev = (await page.evaluate(() => fetch('/api/devices').then(r => r.json()))).find(d => d.stream === stream)
  await page.evaluate((id, mode) => {
    localStorage.setItem('wall.layout', '1')
    localStorage.setItem('wall.source', '"overlay"')
    localStorage.setItem('wall.mode', JSON.stringify(mode))
    localStorage.setItem('wall.picked', JSON.stringify({ 0: id }))
  }, dev.id, mode)
  await page.reload({ waitUntil: 'networkidle2' })
  await sleep(mode === 'fmp4' ? 8000 : 5000)          // fMP4 needs a few seconds of buffer
  await page.evaluate(ft => { window.__overlayForceTime = ft; window.__overlayLog = [] }, forceTime)
  await sleep(Number(secs) * 1000)
  const res = await page.evaluate((mode, stream) => {
    const log = window.__overlayLog.filter(e => e.s === stream)
    window.__overlayLog = null
    const msgs = window.__msgs.filter(m => m.s === stream)
    const byR = new Map(msgs.filter(m => m.r != null).map(m => [m.r, m]))
    const truthOf = e => mode === 'webrtc' ? byR.get(e.rtp)
      : msgs.find(m => m.z != null && e.media != null && Math.abs(m.z - e.media) < 0.02)
    const errs = []
    let frames = 0, missing = 0, extra = 0
    for (const e of log) {
      const t = truthOf(e)
      if (!t) continue                  // no detection on this frame: nothing to compare
      frames++
      const drawn = new Map(e.b.map(b => [b[0], b]))
      for (const tr of t.tr) {
        const b = drawn.get(tr[0])
        if (!b) { missing++; continue }
        const dx = ((b[3] + b[5]) - (tr[3] + tr[5])) / 2 * t.w, dy = ((b[4] + b[6]) - (tr[4] + tr[6])) / 2 * t.h
        errs.push(Math.hypot(dx, dy))
      }
      extra += e.b.filter(b => !t.tr.some(tr => tr[0] === b[0])).length
    }
    // How late results come: receive time - time their frame was first shown (> 0: frame shown first)
    const shown = new Map()
    for (const e of log) if (mode === 'webrtc' && e.rtp != null && !shown.has(e.rtp)) shown.set(e.rtp, e.t)
    const lags = msgs.filter(m => shown.has(m.r)).map(m => m.recv - shown.get(m.r)).sort((a, b) => a - b)
    const lq = p => lags.length ? Math.round(lags[Math.min(lags.length - 1, Math.floor(lags.length * p))]) : '-'
    errs.sort((a, b) => a - b)
    const q = p => errs.length ? errs[Math.min(errs.length - 1, Math.floor(errs.length * p))].toFixed(1) : '-'
    const mean = errs.length ? (errs.reduce((a, b) => a + b, 0) / errs.length).toFixed(1) : '-'
    return { modeUsed: [...new Set(log.map(e => e.mode))].join(','), frames, boxes: errs.length, missing, extra,
             mean, p50: q(0.5), p95: q(0.95), max: q(1), stamped: msgs.length ? msgs.filter(m => m.r != null).length / msgs.length : 0,
             w: msgs[0]?.w, lag: `${lq(0.05)}/${lq(0.5)}/${lq(0.95)}`, hold: window.__overlay?.[stream]?.holdMs }
  }, mode, stream)
  await page.close()
  return res
}

const rows = []
for (const jitter of [false, true]) {
  const runs = process.env.ONLY_WEBRTC ? [['webrtc', false]] : [['webrtc', false], ['webrtc', true], ['fmp4', false]]
  for (const [mode, forceTime] of runs) {
    const r = await run(mode, forceTime, jitter)
    const row = { mode, align: forceTime ? '延迟估算(旧)' : '帧时间戳(新)', ws: jitter ? '抖动' : '正常', ...r }
    rows.push(row)
    console.log(JSON.stringify(row))
  }
}
console.table(rows.map(r => ({ 播放: r.mode, 对齐: r.align, WS: r.ws, 实际: r.modeUsed, 对比帧: r.frames,
  '平均偏差px': r.mean, p50: r.p50, p95: r.p95, 最大: r.max, 漏画: r.missing, 多画: r.extra, '结果晚到ms p5/50/95': r.lag, '缓冲ms': r.hold })))
await browser.close()
