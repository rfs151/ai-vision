<!-- Draws live track boxes (from the worker, via WebSocket) and the device's zones/lines on a
     canvas over the raw video, instead of burning them into a re-encoded stream.

     Timing, by frame timestamps (worker/zlmclock.py): every message carries the timestamp of the
     frame it was detected on, on the player's own clock: r = RTP timestamp (WebRTC), z = fMP4
     media time. requestVideoFrameCallback (Player.vue) says which frame is on screen, so boxes are
     drawn for exactly that frame, whatever the network or the jitter buffer does:
       - a frame with its own result (every 2nd-3rd frame at 5 detections/s): those boxes
       - frames in between: boxes interpolated between the results before and after
       - no later result yet (the worker is a frame or two behind the player): moved on with the
         Kalman velocity, at most 0.5 s
       - video frozen: no new frame, so the boxes stay where they are
       - results late (WebSocket stalled): last boxes moved on for 0.5 s, nothing after 1.5 s
     Fallback (messages without r, browsers without rtpTimestamp, WebRTC only): the old estimate
         shown at = receive time - p (worker processing time) + delay (WebRTC jitter buffer) -->
<template>
  <canvas ref="canvas" class="track-overlay" />
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { subscribeTracks, trackBuffers } from '../store'

const props = defineProps({
  stream: { type: String, required: true },
  video: { type: Object, default: null },          // the <video> element
  rules: { type: Object, default: null },          // device rules: zones / lines (normalized)
  delay: { type: Number, default: 0 },             // seconds the player shows video behind live
  // Frame on screen, from requestVideoFrameCallback: { rtp, media } (plain object, updated in place)
  frame: { type: Object, default: null },
  mode: { type: String, default: 'webrtc' },       // webrtc: match r / rtp; fmp4: match z / media
})

const canvas = ref(null)
const MAX_EXTRAPOLATE = 0.5      // s; beyond this a box is more guess than measurement
const POSE_LABEL = { stand: '站立', bend: '弯腰', lying: '躺地', FALL: '摔倒' }
const STALE = 1.5                // s without a newer message: draw no boxes
let raf = 0, unsub = null
// For tests / debugging: window.__overlay[stream] = { mode: 'frame' | 'time', frames, exact }
const stats = { mode: '', frames: 0, exact: 0, holdMs: 0 }

const COLORS = ['#4fc3f7', '#81c784', '#ffb74d', '#ba68c8', '#4db6ac', '#f06292', '#aed581', '#7986cb']
const color = id => COLORS[id % COLORS.length]

// Where the picture is inside the <video> box (object-fit: contain leaves bars)
function contentRect(v, cw, ch) {
  const vw = v.videoWidth, vh = v.videoHeight
  if (!vw || !vh) return null
  const s = Math.min(cw / vw, ch / vh)
  const w = vw * s, h = vh * s
  return { x: (cw - w) / 2, y: (ch - h) / 2, w, h }
}

const signed32 = d => ((d % 4294967296) + 6442450944) % 4294967296 - 2147483648

// Seconds from the frame on screen to the message's frame (> 0: a later frame), null if unknown
function rel(msg) {
  const f = props.frame
  if (props.mode === 'webrtc') return f?.rtp == null || msg.r == null ? null : signed32(msg.r - f.rtp) / 90000
  return f?.media == null || msg.z == null ? null : msg.z - f.media
}

// How late results come after their frame arrived (WebRTC; ms, last 50). The player holds frames
// back by about that much (Player.vue holdMs), so the right result is usually there when the frame
// is shown. p90 + 40 ms, at most 600 ms.
const lags = [], lagSeen = new Set()
function measureLag(buf) {
  const recvAt = props.frame?.recvAt
  if (!recvAt || props.mode !== 'webrtc') return
  for (const { recv, msg } of buf) {
    if (msg.r == null || lagSeen.has(msg.r)) continue
    const t = recvAt.get(msg.r)
    if (t == null) continue
    lagSeen.add(msg.r)
    if (lagSeen.size > 500) lagSeen.clear()
    lags.push(recv - t)
    if (lags.length > 50) lags.shift()
  }
  if (lags.length >= 5) {
    const s = [...lags].sort((a, b) => a - b)
    props.frame.holdMs = Math.min(600, Math.max(0, s[Math.floor(s.length * 0.9)] + 40))
    stats.holdMs = Math.round(props.frame.holdMs)
  }
}

// Results around the frame on screen: { m0, d0 (s since m0's frame), m1, d1 (s until m1's frame) }.
// undefined = no frame timestamps (use the time estimate); null = nothing to draw.
function byFrame() {
  const buf = trackBuffers.get(props.stream)
  if (!buf?.length) return null
  measureLag(buf)
  let m0 = null, d0 = 0, m1 = null, d1 = 0, near = false
  for (const { msg } of buf) {
    const d = rel(msg)
    if (d == null) return undefined
    if (Math.abs(d) < 10) near = true
    if (d <= 0.001) { if (!m0 || -d < d0) { m0 = msg; d0 = Math.max(0, -d) } }
    else if (!m1 || d < d1) { m1 = msg; d1 = d }
  }
  // Every result > 10 s away from the picture: the stamps do not belong to this playback
  // (e.g. the source restarted and the worker has not reconnected yet): use the time estimate
  if (!near) return undefined
  if (!m0 || d0 > STALE) return null
  return { m0, d0, m1, d1 }
}

// Boxes to draw now: [id, cls, score, x1, y1, x2, y2, hot, pose] normalized
function boxes(now) {
  const out = boxesNow(now)
  // Tests: window.__overlayLog = [] records what is drawn on which frame (web/e2e/overlaytest.mjs)
  if (window.__overlayLog) window.__overlayLog.push({ t: now, s: props.stream, rtp: props.frame?.rtp, media: props.frame?.media, mode: stats.mode, b: out })
  return out
}

function boxesNow(now) {
  // Tests: window.__overlayForceTime = true uses the old delay estimate, for comparison
  const f = window.__overlayForceTime ? undefined : byFrame()
  stats.mode = f === undefined ? 'time' : 'frame'
  if (f === null) return []
  if (f) {
    stats.frames++
    if (f.d0 < 0.001) stats.exact++
    const next = f.m1 ? new Map(f.m1.tr.map(t => [t[0], t])) : null
    return f.m0.tr.map(([id, cls, score, x1, y1, x2, y2, vx, vy, hot, pose]) => {
      const b = next?.get(id)
      if (b) {      // interpolate towards the next result
        const k = f.d0 / (f.d0 + f.d1)
        return [id, cls, score, x1 + (b[3] - x1) * k, y1 + (b[4] - y1) * k, x2 + (b[5] - x2) * k, y2 + (b[6] - y2) * k, hot, pose]
      }
      const dt = Math.min(f.d0, MAX_EXTRAPOLATE)
      return [id, cls, score, x1 + vx * dt, y1 + vy * dt, x2 + vx * dt, y2 + vy * dt, hot, pose]
    })
  }
  if (props.mode !== 'webrtc') return []    // fMP4 has no delay estimate to fall back on
  const cur = current(now)
  if (!cur) return []
  const dt = Math.min(cur.age, MAX_EXTRAPOLATE)
  return cur.msg.tr.map(([id, cls, score, x1, y1, x2, y2, vx, vy, hot, pose]) =>
    [id, cls, score, x1 + vx * dt, y1 + vy * dt, x2 + vx * dt, y2 + vy * dt, hot, pose])
}

// Fallback: the message whose frame is on screen now (estimated), and how long ago it was shown
function current(now) {
  const buf = trackBuffers.get(props.stream)
  if (!buf?.length) return null
  for (let i = buf.length - 1; i >= 0; i--) {
    const shownAt = buf[i].recv + (props.delay - (buf[i].msg.p || 0)) * 1000
    if (shownAt <= now) {
      const age = (now - shownAt) / 1000
      return age > STALE ? null : { msg: buf[i].msg, age }
    }
  }
  return null
}

function drawRules(ctx, r) {
  const rules = props.rules
  if (!rules) return
  ctx.lineWidth = 2
  ctx.font = '12px sans-serif'
  for (const z of rules.zones || []) {
    if (!z.polygon?.length) continue
    ctx.beginPath()
    z.polygon.forEach(([x, y], i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, r.x + x * r.w, r.y + y * r.h))
    ctx.closePath()
    ctx.fillStyle = 'rgba(64,158,255,0.12)'
    ctx.strokeStyle = 'rgba(64,158,255,0.9)'
    ctx.fill()
    ctx.stroke()
    const [x0, y0] = z.polygon[0]
    ctx.fillStyle = '#409eff'
    ctx.fillText(z.name || z.id, r.x + x0 * r.w + 4, r.y + y0 * r.h + 14)
  }
  for (const l of rules.lines || []) {
    const ax = r.x + l.p1[0] * r.w, ay = r.y + l.p1[1] * r.h
    const bx = r.x + l.p2[0] * r.w, by = r.y + l.p2[1] * r.h
    ctx.strokeStyle = '#e6a23c'
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke()
    ctx.fillStyle = '#e6a23c'
    ctx.fillText(l.name || l.id, (ax + bx) / 2 + 4, (ay + by) / 2 - 4)
  }
}

function frame() {
  raf = requestAnimationFrame(frame)
  const c = canvas.value, v = props.video
  if (!c || !v) return
  const dpr = window.devicePixelRatio || 1
  const cw = c.clientWidth, ch = c.clientHeight
  if (c.width !== Math.round(cw * dpr) || c.height !== Math.round(ch * dpr)) {
    c.width = Math.round(cw * dpr)
    c.height = Math.round(ch * dpr)
  }
  const ctx = c.getContext('2d')
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, cw, ch)
  const r = contentRect(v, cw, ch)
  if (!r || v.paused || v.readyState < 2) return
  drawRules(ctx, r)

  ctx.lineWidth = 2
  ctx.font = '12px sans-serif'
  for (const [id, cls, score, x1, y1, x2, y2, hot, pose] of boxes(performance.now())) {
    const x = r.x + x1 * r.w, y = r.y + y1 * r.h
    const w = (x2 - x1) * r.w, h = (y2 - y1) * r.h
    const col = hot || pose === 'FALL' ? '#f56c6c' : color(id)
    ctx.strokeStyle = col
    ctx.lineWidth = hot ? 3 : 2
    ctx.strokeRect(x, y, w, h)
    // pose: posture from a pose worker (stand / bend / lying / FALL)
    const label = pose ? `#${id} ${POSE_LABEL[pose] || pose}` : `#${id} ${cls} ${score.toFixed(2)}`
    const tw = ctx.measureText(label).width + 6
    ctx.fillStyle = col
    ctx.fillRect(x, Math.max(r.y, y - 16), tw, 16)
    ctx.fillStyle = '#000'
    ctx.fillText(label, x + 3, Math.max(r.y, y - 16) + 12)
  }
}

onMounted(() => {
  unsub = subscribeTracks(props.stream)
  ;(window.__overlay ||= {})[props.stream] = stats
  raf = requestAnimationFrame(frame)
})
onBeforeUnmount(() => {
  if (props.frame) props.frame.holdMs = 0     // overlay off: back to lowest latency
  cancelAnimationFrame(raf)
  unsub?.()
})
</script>

<style scoped>
.track-overlay { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
</style>
