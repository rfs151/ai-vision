<!-- Draws live track boxes (from the worker, via WebSocket) and the device's zones/lines on a
     canvas over the raw video, instead of burning them into a re-encoded stream.

     Timing (WebRTC only): a message describes a frame that is on screen at about
         receive time - p (worker's processing time) + delay (the WebRTC jitter buffer)
     Measured: WebRTC shows a frame ~5 ms after receiving it, so this lines up well. HTTP-fMP4
     buffers 1-3 s in ways the page cannot measure precisely, so the wall disables overlays there.
     Between messages (5/s) boxes move with the Kalman velocity, so they glide at display rate. -->
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
})

const canvas = ref(null)
const MAX_EXTRAPOLATE = 0.5      // s; beyond this a box is more guess than measurement
const STALE = 1.5                // s without a newer message: draw no boxes
let raf = 0, unsub = null

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

// The message whose frame is on screen now, and how long ago that frame was shown
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

  const cur = current(performance.now())
  if (!cur) return
  const dt = Math.min(cur.age, MAX_EXTRAPOLATE)
  ctx.lineWidth = 2
  ctx.font = '12px sans-serif'
  for (const [id, cls, score, x1, y1, x2, y2, vx, vy, hot] of cur.msg.tr) {
    const dx = vx * dt, dy = vy * dt
    const x = r.x + (x1 + dx) * r.w, y = r.y + (y1 + dy) * r.h
    const w = (x2 - x1) * r.w, h = (y2 - y1) * r.h
    const col = hot ? '#f56c6c' : color(id)
    ctx.strokeStyle = col
    ctx.lineWidth = hot ? 3 : 2
    ctx.strokeRect(x, y, w, h)
    const label = `#${id} ${cls} ${score.toFixed(2)}`
    const tw = ctx.measureText(label).width + 6
    ctx.fillStyle = col
    ctx.fillRect(x, Math.max(r.y, y - 16), tw, 16)
    ctx.fillStyle = '#000'
    ctx.fillText(label, x + 3, Math.max(r.y, y - 16) + 12)
  }
}

onMounted(() => {
  unsub = subscribeTracks(props.stream)
  raf = requestAnimationFrame(frame)
})
onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  unsub?.()
})
</script>

<style scoped>
.track-overlay { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
</style>
