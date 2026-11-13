<template>
  <div class="wall">
    <div class="main">
      <form class="ai-bar" @submit.prevent="aiSearch">
        <span class="ai-label"><el-icon><MagicStick /></el-icon>AI 查询</span>
        <input v-model="aiText" placeholder="用一句话找人找车，例如：超市里穿绿色外套的顾客" />
        <button type="submit">查询</button>
        <span class="ai-ex">
          <a v-for="e in AI_EXAMPLES" :key="e" @click="aiText = e; aiSearch()">{{ e }}</a>
        </span>
      </form>
      <div class="toolbar">
        <el-radio-group v-model="layout" size="small">
          <el-radio-button :value="1">1 画面</el-radio-button>
          <el-radio-button :value="4">4 画面</el-radio-button>
          <el-radio-button :value="9">9 画面</el-radio-button>
        </el-radio-group>
        <el-radio-group v-model="source" size="small">
          <el-radio-button value="ai">AI 画面</el-radio-button>
          <el-tooltip content="浏览器在原始画面上画框：每组框带检测帧的时间戳，和正在显示的那一帧对齐（WebRTC、fMP4 都支持）">
            <el-radio-button value="overlay">原始 + 前端画框</el-radio-button>
          </el-tooltip>
          <el-radio-button value="source">原始画面</el-radio-button>
        </el-radio-group>
        <el-radio-group v-model="mode" size="small">
          <el-radio-button value="webrtc">WebRTC</el-radio-button>
          <el-radio-button value="fmp4">HTTP-fMP4</el-radio-button>
        </el-radio-group>
        <span class="muted">在线 {{ onlineCount }} / {{ store.devices.length }}</span>
      </div>

      <div class="grid" :style="gridStyle">
        <div v-for="(slot, i) in slots" :key="i" class="cell" :class="{ flash: flashing[slot?.stream] }">
          <template v-if="slot">
            <!-- ai: boxes burned in by the worker (re-encoded stream); overlay: raw stream + boxes
                 drawn here from live track data (sharper picture, boxes glide at display rate) -->
            <Player :key="slot.stream + source + mode" :urls="slot.play[source === 'ai' ? 'ai' : 'source']" :mode="mode"
                    :before-start="source === 'ai' ? () => api.keyframe(slot.id) : null"
                    :overlay="source === 'overlay' ? { stream: slot.stream, rules: slot.rules } : null" />
            <div class="overlay top">
              <span class="name">
                <span class="dot" :class="{ on: slot.online }" />{{ slot.name }}
              </span>
              <el-select v-model="picked[i]" size="small" class="pick" placeholder="选择设备">
                <el-option v-for="d in onlineDevices" :key="d.id" :label="d.name" :value="d.id" />
              </el-select>
            </div>
            <div v-if="workerOf(slot)" class="overlay bottom">
              <span>{{ workerOf(slot).stats.out_fps ?? '-' }} fps</span>
              <span>推理 {{ workerOf(slot).stats.infer_ms ?? '-' }} ms</span>
              <span>目标 {{ workerOf(slot).stats.tracks ?? 0 }}</span>
              <span v-for="(n, z) in workerOf(slot).stats.zones" :key="z">区域 {{ zoneName(slot, z) }}: {{ n }}</span>
              <span v-for="(c, l) in workerOf(slot).stats.lines" :key="l">{{ lineName(slot, l) }} 进 {{ c.in }} / 出 {{ c.out }}</span>
            </div>
            <div v-else-if="slot.ai_enabled" class="overlay bottom"><span>AI 未运行</span></div>
          </template>
          <div v-else class="empty">
            <el-select v-model="picked[i]" size="small" placeholder="选择设备" style="width: 160px">
              <el-option v-for="d in onlineDevices" :key="d.id" :label="d.name" :value="d.id" />
            </el-select>
          </div>
        </div>
      </div>
    </div>

    <div class="side">
      <div class="side-title">实时告警</div>
      <el-empty v-if="!store.recentAlarms.length" description="暂无告警" :image-size="60" />
      <div v-for="a in store.recentAlarms" :key="a.id" class="alarm" @click="detail = a">
        <el-image v-if="a.snapshot_url" :src="a.snapshot_url" fit="cover" class="thumb" />
        <div class="info">
          <el-tag :type="ALARM_TYPES[a.type]?.tag" size="small">{{ ALARM_TYPES[a.type]?.label || a.type }}</el-tag>
          <div>{{ a.stream }} · {{ a.rule_name }}</div>
          <div class="muted">{{ fmtTime(a.created_at) }}</div>
        </div>
      </div>
    </div>

    <AlarmDialog v-model="detail" />
  </div>
</template>

<script setup>
import { MagicStick } from '@element-plus/icons-vue'
import { useRouter as useAiRouter } from 'vue-router'
const AI_EXAMPLES = ['超市里穿绿色外套的顾客', '超市里戴帽子的男人']
const aiRouter = useAiRouter()
const aiText = ref('')
function aiSearch() {
  aiRouter.push({ path: '/search', query: aiText.value.trim() ? { q: aiText.value.trim() } : {} })
}
import { computed, reactive, ref, watch } from 'vue'
import Player from '../components/Player.vue'
import AlarmDialog from '../components/AlarmDialog.vue'
import { store } from '../store'
import { ALARM_TYPES, api, fmtTime } from '../api'

const saved = (k, d) => { try { return JSON.parse(localStorage.getItem('wall.' + k)) ?? d } catch { return d } }
const layout = ref(saved('layout', 4))
const source = ref(saved('source', 'ai'))
const mode = ref(saved('mode', 'webrtc'))
const picked = reactive(saved('picked', {}))          // slot index -> device id
const flashing = reactive({})
const detail = ref(null)

watch([layout, source, mode, picked], () => {
  try {
    localStorage.setItem('wall.layout', JSON.stringify(layout.value))
    localStorage.setItem('wall.source', JSON.stringify(source.value))
    localStorage.setItem('wall.mode', JSON.stringify(mode.value))
    localStorage.setItem('wall.picked', JSON.stringify(picked))
  } catch { /* storage unavailable */ }
}, { deep: true })

// Only devices with a live stream are offered and shown (a stopped test stream disappears)
const onlineDevices = computed(() => store.devices.filter(d => d.online))
const byId = computed(() => Object.fromEntries(onlineDevices.value.map(d => [d.id, d])))
const onlineCount = computed(() => onlineDevices.value.length)

// Fill slots: explicitly picked devices first, then online devices not shown yet
const slots = computed(() => {
  const out = Array(layout.value).fill(null)
  const used = new Set()
  for (let i = 0; i < layout.value; i++) {
    const d = byId.value[picked[i]]
    if (d) { out[i] = d; used.add(d.id) }
  }
  const rest = onlineDevices.value.filter(d => !used.has(d.id))
  for (let i = 0; i < layout.value; i++) if (!out[i] && rest.length) out[i] = rest.shift()
  return out
})

const gridStyle = computed(() => {
  const n = Math.sqrt(layout.value)
  return { gridTemplateColumns: `repeat(${n}, 1fr)`, gridTemplateRows: `repeat(${n}, 1fr)` }
})

const workerOf = d => {
  const w = store.workers[d.stream]
  return w && w.state !== 'stopped' && w.stats ? w : null
}
const zoneName = (d, id) => d.rules.zones.find(z => z.id === id)?.name || id
const lineName = (d, id) => d.rules.lines.find(l => l.id === id)?.name || id

watch(() => store.lastAlarm, la => {
  if (!la) return
  const s = la.alarm.stream
  flashing[s] = false
  requestAnimationFrame(() => { flashing[s] = true })
  setTimeout(() => { flashing[s] = false }, 3000)
})
</script>

<style scoped>
.wall { display: flex; height: 100%; }
.main { flex: 1; padding: 12px; display: flex; flex-direction: column; min-width: 0; }
.grid { flex: 1; display: grid; gap: 6px; min-height: 0; }
.cell { position: relative; background: #000; border-radius: 4px; overflow: hidden; min-height: 0; }
.overlay { position: absolute; left: 0; right: 0; padding: 6px 8px; display: flex; gap: 10px; align-items: center;
  font-size: 12px; color: #fff; background: linear-gradient(rgba(0,0,0,.55), rgba(0,0,0,0)); }
.overlay.top { top: 0; justify-content: space-between; }
.overlay.bottom { bottom: 0; flex-wrap: wrap; background: linear-gradient(rgba(0,0,0,0), rgba(0,0,0,.6)); }
.name { font-weight: 600; display: flex; align-items: center; gap: 6px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #909399; }
.dot.on { background: #67c23a; }
.pick { width: 120px; opacity: 0; transition: opacity .2s; }
.cell:hover .pick { opacity: 1; }
.empty { height: 100%; display: flex; align-items: center; justify-content: center; }
.side { width: 280px; border-left: 1px solid var(--el-border-color); overflow-y: auto; padding: 12px; box-sizing: border-box; }
.side-title { font-weight: 600; margin-bottom: 10px; }
.alarm { display: flex; gap: 8px; padding: 8px; border-radius: 4px; cursor: pointer; font-size: 13px; }
.alarm:hover { background: var(--el-fill-color-light); }
.thumb { width: 96px; height: 64px; border-radius: 3px; flex-shrink: 0; }
.info { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.ai-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; padding: 6px 6px 6px 14px; border-radius: 22px;
  border: 1px solid transparent; flex-wrap: wrap;
  background: linear-gradient(var(--el-bg-color), var(--el-bg-color)) padding-box,
              linear-gradient(90deg, #409eff, #955cff) border-box; }
.ai-label { display: flex; align-items: center; gap: 4px; font-weight: 600; white-space: nowrap;
  background: linear-gradient(90deg, #79bbff, #b48cff); -webkit-background-clip: text; background-clip: text; color: transparent; }
.ai-label .el-icon { color: #79bbff; }
.ai-bar input { flex: 1; min-width: 200px; background: none; border: 0; outline: none; color: var(--el-text-color-primary); font-size: 14px; }
.ai-bar button { border: 0; border-radius: 16px; padding: 6px 18px; color: #fff; cursor: pointer;
  background: linear-gradient(90deg, #409eff, #955cff); }
.ai-ex { display: flex; gap: 10px; font-size: 12px; }
.ai-ex a { color: var(--el-text-color-secondary); cursor: pointer; }
.ai-ex a:hover { color: var(--el-color-primary); }
</style>
