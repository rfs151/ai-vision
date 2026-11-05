<!-- Small live line chart (inline SVG, no chart library).
     One y-axis; up to a few series, each with a legend entry and a label at the line's end;
     vertical markers for events (a feature switched on/off); hover = crosshair + values. -->
<template>
  <div class="chart" ref="box">
    <div class="head">
      <span class="title">{{ title }}</span>
      <span class="legend">
        <span v-for="s in series" :key="s.name" class="key"><i :style="{ background: s.color }" />{{ s.name }}</span>
      </span>
    </div>
    <svg :width="W" :height="H" @pointermove="onMove" @pointerleave="hover = null">
      <!-- grid + y ticks -->
      <g v-for="y in ticks" :key="'g' + y">
        <line :x1="PL" :x2="W - PR" :y1="sy(y)" :y2="sy(y)" class="grid" />
        <text :x="PL - 6" :y="sy(y) + 4" class="axis" text-anchor="end">{{ fmt(y) }}</text>
      </g>
      <text :x="PL" :y="H - 4" class="axis">-{{ Math.round(windowMs / 60000) }} 分钟</text>
      <text :x="W - PR" :y="H - 4" class="axis" text-anchor="end">现在</text>
      <!-- event markers -->
      <g v-for="(m, i) in visibleMarkers" :key="'m' + i">
        <line :x1="sx(m.t)" :x2="sx(m.t)" :y1="PT" :y2="H - PB" class="marker" />
        <!-- near the right edge: label to the left of the line, so it is not cut off -->
        <text :x="sx(m.t) + (sx(m.t) > W * 0.55 ? -4 : 4)" :y="PT + 10 + (i % 2) * 12" class="mlabel"
              :text-anchor="sx(m.t) > W * 0.55 ? 'end' : 'start'">{{ m.label }}</text>
      </g>
      <!-- series -->
      <g v-for="s in series" :key="s.name">
        <polyline :points="path(s)" fill="none" :stroke="s.color" stroke-width="2"
                  stroke-linejoin="round" stroke-linecap="round" />
        <template v-if="last(s)">
          <circle :cx="sx(last(s).t)" :cy="sy(last(s).v)" r="4" :fill="s.color" class="ring" />
          <text :x="sx(last(s).t) + 8" :y="sy(last(s).v) + 4" class="end">{{ fmt(last(s).v) }}</text>
        </template>
      </g>
      <!-- hover -->
      <g v-if="hover">
        <line :x1="hover.x" :x2="hover.x" :y1="PT" :y2="H - PB" class="cross" />
        <template v-for="h in hover.values" :key="h.name">
          <circle :cx="sx(h.t)" :cy="sy(h.v)" r="4" :fill="h.color" class="ring" />
        </template>
      </g>
    </svg>
    <div v-if="hover" class="tip" :style="{ left: Math.min(hover.x + 12, W - 150) + 'px' }">
      <div class="muted">{{ new Date(hover.t).toLocaleTimeString() }}</div>
      <div v-for="h in hover.values" :key="h.name"><i :style="{ background: h.color }" />{{ h.name }}
        <b>{{ fmt(h.v) }}</b> {{ unit }}</div>
    </div>
    <div v-if="!hasData" class="empty muted">等待数据（Worker 每 5 秒上报一次）…</div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

const props = defineProps({
  title: String,
  unit: { type: String, default: '' },
  series: { type: Array, default: () => [] },     // [{ name, color, points: [{ t, v }] }]
  markers: { type: Array, default: () => [] },    // [{ t, label }]
  windowMs: { type: Number, default: 5 * 60 * 1000 },
  digits: { type: Number, default: 0 },
  zero: { type: Boolean, default: true },          // y-axis starts at 0
})

const box = ref(null)
const W = ref(480)
const H = 180
const PL = 44, PR = 52, PT = 8, PB = 18
const now = ref(Date.now())
const hover = ref(null)
let timer, ro

onMounted(() => {
  timer = setInterval(() => { now.value = Date.now() }, 1000)
  ro = new ResizeObserver(([e]) => { W.value = Math.max(260, Math.floor(e.contentRect.width)) })
  ro.observe(box.value)
})
onBeforeUnmount(() => { clearInterval(timer); ro?.disconnect() })

const t0 = computed(() => now.value - props.windowMs)
const pts = s => s.points.filter(p => p.t >= t0.value && p.v != null)
const hasData = computed(() => props.series.some(s => pts(s).length))
const range = computed(() => {
  const vs = props.series.flatMap(s => pts(s).map(p => p.v))
  if (!vs.length) return [0, 1]
  let lo = props.zero ? 0 : Math.min(...vs), hi = Math.max(...vs)
  if (hi === lo) hi = lo + 1
  const pad = (hi - lo) * 0.1
  return [props.zero ? 0 : lo - pad, hi + pad]
})
const ticks = computed(() => {
  const [lo, hi] = range.value
  const step = niceStep((hi - lo) / 3)
  const out = []
  for (let y = Math.ceil(lo / step) * step; y <= hi; y += step) out.push(+y.toFixed(6))
  return out
})
function niceStep(x) {
  const p = 10 ** Math.floor(Math.log10(x))
  const m = x / p
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p
}
const sx = t => PL + (t - t0.value) / props.windowMs * (W.value - PL - PR)
const sy = v => { const [lo, hi] = range.value; return PT + (1 - (v - lo) / (hi - lo)) * (H - PT - PB) }
const path = s => pts(s).map(p => `${sx(p.t).toFixed(1)},${sy(p.v).toFixed(1)}`).join(' ')
const last = s => { const p = pts(s); return p[p.length - 1] }
const fmt = v => (v == null ? '-' : Number(v).toFixed(props.digits))
const visibleMarkers = computed(() => props.markers.filter(m => m.t >= t0.value))

function onMove(ev) {
  const x = ev.offsetX
  if (x < PL || x > W.value - PR) { hover.value = null; return }
  const t = t0.value + (x - PL) / (W.value - PL - PR) * props.windowMs
  const values = []
  for (const s of props.series) {
    let best = null
    for (const p of pts(s)) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p
    if (best && Math.abs(best.t - t) < 15000) values.push({ name: s.name, color: s.color, t: best.t, v: best.v })
  }
  hover.value = values.length ? { x, t, values } : null
}
</script>

<style scoped>
.chart { position: relative; width: 100%; }
.head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; gap: 8px; }
.title { font-size: 13px; color: var(--el-text-color-primary); }
.legend { display: flex; gap: 12px; font-size: 12px; color: var(--el-text-color-secondary); }
.key i, .tip i { display: inline-block; width: 10px; height: 3px; border-radius: 2px; margin-right: 5px; vertical-align: middle; }
svg { display: block; }
.grid { stroke: var(--el-border-color-lighter); stroke-width: 1; }
.axis { fill: var(--el-text-color-placeholder); font-size: 11px; }
.end { fill: var(--el-text-color-regular); font-size: 11px; }
.marker { stroke: var(--el-text-color-placeholder); stroke-dasharray: 3 3; }
.mlabel { fill: var(--el-text-color-secondary); font-size: 11px; }
.cross { stroke: var(--el-text-color-secondary); stroke-width: 1; }
.ring { stroke: var(--el-bg-color-overlay); stroke-width: 2; }
.tip { position: absolute; top: 28px; pointer-events: none; background: var(--el-bg-color-overlay);
  border: 1px solid var(--el-border-color); border-radius: 4px; padding: 6px 8px; font-size: 12px;
  color: var(--el-text-color-regular); min-width: 130px; }
.empty { position: absolute; inset: 24px 0 0; display: flex; align-items: center; justify-content: center; font-size: 12px; }
</style>
