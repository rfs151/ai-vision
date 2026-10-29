<!-- Draw / edit zones (polygons) and lines on top of a video.
     Coordinates are normalized to 0..1 of the frame, same as worker/rules.py,
     so the container must have the video's aspect ratio and the video must use object-fit: fill. -->
<template>
  <canvas ref="cv" class="rule-canvas" :class="tool" tabindex="0"
          @pointerdown="down" @pointermove="move" @pointerup="up" @dblclick="dbl"
          @contextmenu.prevent="ctx" @keydown.esc="cancelDraft" />
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  rules: { type: Object, required: true },        // reactive {zones, lines}; mutated in place
  tool: { type: String, default: 'select' },      // select | zone | line
  selected: { type: Object, default: null },      // {kind: 'zone'|'line', id}
})
const emit = defineEmits(['select', 'done', 'change'])

const cv = ref(null)
const HIT = 9                    // px tolerance
let W = 1, H = 1, ro = null
const draft = ref([])           // points of the shape being drawn (normalized)
const mouse = ref(null)
let drag = null                 // {kind, id, vertex?, last}

// ---------- geometry ----------
const px = p => [p[0] * W, p[1] * H]
const norm = (x, y) => [Math.min(1, Math.max(0, x / W)), Math.min(1, Math.max(0, y / H))]
const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1])

function segDist(p, a, b) {
  const [dx, dy] = [b[0] - a[0], b[1] - a[1]]
  const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)))
  return dist(p, [a[0] + t * dx, a[1] + t * dy])
}

function inPoly(p, poly) {
  let inside = false
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j]
    if ((yi > p[1]) !== (yj > p[1]) && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside
  }
  return inside
}

const nextId = (list, prefix) => {
  let i = 1
  while (list.some(s => s.id === `${prefix}${i}`)) i++
  return `${prefix}${i}`
}

// Vertex lists of each shape, in pixels, selected shape first (its handles win hit tests)
function shapes() {
  const out = [
    ...props.rules.zones.map(z => ({ kind: 'zone', id: z.id, pts: z.polygon })),
    ...props.rules.lines.map(l => ({ kind: 'line', id: l.id, pts: [l.p1, l.p2] })),
  ]
  const s = props.selected
  return s ? out.sort((a, b) => (b.kind === s.kind && b.id === s.id) - (a.kind === s.kind && a.id === s.id)) : out
}

function hitTest(p) {
  for (const s of shapes()) {
    const i = s.pts.findIndex(v => dist(p, px(v)) < HIT)
    if (i >= 0) return { ...s, vertex: i }
  }
  for (const s of shapes()) {
    if (s.kind === 'line' && segDist(p, px(s.pts[0]), px(s.pts[1])) < HIT) return s
    if (s.kind === 'zone' && inPoly(p, s.pts.map(px))) return s
  }
  return null
}

const find = (kind, id) => kind === 'zone'
  ? props.rules.zones.find(z => z.id === id)
  : props.rules.lines.find(l => l.id === id)

const pointsOf = (kind, obj) => kind === 'zone' ? obj.polygon : [obj.p1, obj.p2]

// ---------- events ----------
const pos = e => {
  const r = cv.value.getBoundingClientRect()
  return [e.clientX - r.left, e.clientY - r.top]
}

function down(e) {
  if (e.button !== 0) return
  cv.value.focus()
  const p = pos(e)
  if (props.tool === 'zone') {
    if (draft.value.length >= 3 && dist(p, px(draft.value[0])) < HIT * 1.5) return finishZone()
    draft.value.push(norm(...p))
  } else if (props.tool === 'line') {
    draft.value.push(norm(...p))
    if (draft.value.length === 2) finishLine()
  } else {
    const h = hitTest(p)
    emit('select', h ? { kind: h.kind, id: h.id } : null)
    if (h) {
      drag = { kind: h.kind, id: h.id, vertex: h.vertex, last: norm(...p) }
      cv.value.setPointerCapture(e.pointerId)
    }
  }
  draw()
}

function move(e) {
  const p = pos(e)
  mouse.value = norm(...p)
  if (drag) {
    const obj = find(drag.kind, drag.id)
    if (!obj) return
    const pts = pointsOf(drag.kind, obj)
    const n = norm(...p)
    if (drag.vertex != null) {
      pts[drag.vertex][0] = n[0]
      pts[drag.vertex][1] = n[1]
    } else {
      // Move the whole shape, but keep every vertex inside the frame
      let dx = n[0] - drag.last[0], dy = n[1] - drag.last[1]
      for (const v of pts) {
        dx = Math.min(Math.max(dx, -v[0]), 1 - v[0])
        dy = Math.min(Math.max(dy, -v[1]), 1 - v[1])
      }
      for (const v of pts) { v[0] += dx; v[1] += dy }
      drag.last = n
    }
    emit('change')
  } else if (props.tool === 'select') {
    cv.value.style.cursor = hitTest(p) ? (hitTest(p).vertex != null ? 'move' : 'grab') : 'default'
  }
  draw()
}

function up() { drag = null }

function dbl(e) {
  const p = pos(e)
  if (props.tool === 'zone') {
    draft.value.pop()               // dblclick also fired a click: drop the duplicate point
    if (draft.value.length >= 3) finishZone()
    return
  }
  if (props.tool !== 'select') return
  // Insert a vertex on the nearest edge of the selected zone
  const s = props.selected
  const z = s?.kind === 'zone' && find('zone', s.id)
  if (!z) return
  const poly = z.polygon
  for (let i = 0; i < poly.length; i++) {
    const a = px(poly[i]), b = px(poly[(i + 1) % poly.length])
    if (segDist(p, a, b) < HIT) {
      poly.splice(i + 1, 0, norm(...p))
      emit('change')
      draw()
      return
    }
  }
}

function ctx(e) {
  const p = pos(e)
  if (draft.value.length) { draft.value.pop(); draw(); return }
  const h = hitTest(p)
  if (h?.kind === 'zone' && h.vertex != null && h.pts.length > 3) {
    find('zone', h.id).polygon.splice(h.vertex, 1)
    emit('change')
    draw()
  }
}

function cancelDraft() {
  draft.value = []
  emit('done', null)
  draw()
}

function finishZone() {
  const id = nextId(props.rules.zones, 'z')
  props.rules.zones.push({ id, name: `区域${id.slice(1)}`, polygon: draft.value.map(p => [...p]),
                           classes: ['person'], intrusion: true, dwell_sec: null, max_count: null })
  draft.value = []
  emit('done', { kind: 'zone', id })
  emit('change')
  draw()
}

function finishLine() {
  const id = nextId(props.rules.lines, 'l')
  const [p1, p2] = draft.value
  props.rules.lines.push({ id, name: `越线${id.slice(1)}`, p1: [...p1], p2: [...p2], classes: ['person'], alarm_on: null })
  draft.value = []
  emit('done', { kind: 'line', id })
  emit('change')
  draw()
}

// ---------- rendering ----------
function label(ctx, text, x, y, bg) {
  ctx.font = '12px sans-serif'
  const w = ctx.measureText(text).width + 8
  ctx.fillStyle = bg
  ctx.fillRect(x, y - 16, w, 18)
  ctx.fillStyle = '#fff'
  ctx.fillText(text, x + 4, y - 3)
}

function draw() {
  const c = cv.value
  if (!c) return
  const ctx = c.getContext('2d')
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0)
  ctx.clearRect(0, 0, W, H)
  const sel = props.selected

  for (const z of props.rules.zones) {
    const isSel = sel?.kind === 'zone' && sel.id === z.id
    const pts = z.polygon.map(px)
    ctx.beginPath()
    pts.forEach((p, i) => (i ? ctx.lineTo(...p) : ctx.moveTo(...p)))
    ctx.closePath()
    ctx.fillStyle = isSel ? 'rgba(64,158,255,.25)' : 'rgba(255,160,0,.18)'
    ctx.fill()
    ctx.lineWidth = isSel ? 3 : 2
    ctx.strokeStyle = isSel ? '#409eff' : '#ffa000'
    ctx.stroke()
    const top = pts.reduce((a, b) => (b[1] < a[1] ? b : a))
    label(ctx, z.name || z.id, top[0], top[1], isSel ? '#409eff' : '#b37000')
    if (isSel) handles(ctx, pts)
  }

  for (const l of props.rules.lines) {
    const isSel = sel?.kind === 'line' && sel.id === l.id
    const a = px(l.p1), b = px(l.p2)
    ctx.beginPath()
    ctx.moveTo(...a)
    ctx.lineTo(...b)
    ctx.lineWidth = isSel ? 4 : 3
    ctx.strokeStyle = isSel ? '#409eff' : '#ffeb3b'
    ctx.stroke()
    // IN arrow: left-hand normal (-dy, dx) in image coordinates, same as worker/rules.py
    const m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]
    let n = [-(b[1] - a[1]), b[0] - a[0]]
    const len = Math.hypot(...n) || 1
    n = [n[0] / len * 36, n[1] / len * 36]
    arrow(ctx, m, [m[0] + n[0], m[1] + n[1]], ctx.strokeStyle)
    label(ctx, `${l.name || l.id} (IN →)`, a[0] + 6, a[1] + 18, isSel ? '#409eff' : '#8a7d00')
    if (isSel) handles(ctx, [a, b])
  }

  // Shape being drawn, with a rubber band to the mouse
  if (draft.value.length) {
    const pts = draft.value.map(px)
    if (mouse.value) pts.push(px(mouse.value))
    ctx.beginPath()
    pts.forEach((p, i) => (i ? ctx.lineTo(...p) : ctx.moveTo(...p)))
    ctx.setLineDash([6, 4])
    ctx.lineWidth = 2
    ctx.strokeStyle = '#67c23a'
    ctx.stroke()
    ctx.setLineDash([])
    handles(ctx, draft.value.map(px), '#67c23a')
  }
}

function handles(ctx, pts, color = '#409eff') {
  for (const p of pts) {
    ctx.beginPath()
    ctx.arc(p[0], p[1], 5, 0, Math.PI * 2)
    ctx.fillStyle = '#fff'
    ctx.fill()
    ctx.lineWidth = 2
    ctx.strokeStyle = color
    ctx.stroke()
  }
}

function arrow(ctx, from, to, color) {
  const ang = Math.atan2(to[1] - from[1], to[0] - from[0])
  ctx.beginPath()
  ctx.moveTo(...from)
  ctx.lineTo(...to)
  ctx.lineTo(to[0] - 10 * Math.cos(ang - 0.45), to[1] - 10 * Math.sin(ang - 0.45))
  ctx.moveTo(...to)
  ctx.lineTo(to[0] - 10 * Math.cos(ang + 0.45), to[1] - 10 * Math.sin(ang + 0.45))
  ctx.lineWidth = 2
  ctx.strokeStyle = color
  ctx.stroke()
}

function resize() {
  const c = cv.value
  const r = c.getBoundingClientRect()
  W = Math.max(r.width, 1)
  H = Math.max(r.height, 1)
  c.width = Math.round(W * devicePixelRatio)
  c.height = Math.round(H * devicePixelRatio)
  draw()
}

watch(() => props.tool, () => { draft.value = []; draw() })
watch(() => [props.rules, props.selected], draw, { deep: true })
onMounted(() => {
  ro = new ResizeObserver(resize)
  ro.observe(cv.value)
  resize()
})
onBeforeUnmount(() => ro?.disconnect())
defineExpose({ cancelDraft })
</script>

<style scoped>
.rule-canvas { position: absolute; inset: 0; width: 100%; height: 100%; outline: none; touch-action: none; }
.rule-canvas.zone, .rule-canvas.line { cursor: crosshair; }
</style>
