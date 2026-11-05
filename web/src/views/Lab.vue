<!-- 视频实验室：每路视频单独开关视频功能，并排对比开启前后的差异。
     对比模式下 Worker 额外编码一路「全部功能关闭」的基准流（ai_ref/<流名>），左右同时播放，
     下方曲线同时画两路的码率 / QP，开关切换的时刻画竖线。 -->
<template>
  <div class="lab">
    <div class="toolbar">
      <el-select v-model="deviceId" size="small" placeholder="选择设备" style="width: 160px">
        <el-option v-for="d in devices" :key="d.id" :label="d.name" :value="d.id" />
      </el-select>
      <el-radio-group v-model="mode" size="small">
        <el-radio-button value="webrtc">WebRTC</el-radio-button>
        <el-radio-button value="fmp4">HTTP-fMP4</el-radio-button>
      </el-radio-group>
      <el-button size="small" :disabled="!dev" @click="replay">重新起播（测起播耗时）</el-button>
      <span class="muted" v-if="worker">Worker CPU {{ fmt(stats.cpu, 0) }}%<template v-if="dev?.video.compare">（含对比编码）</template></span>
    </div>

    <el-empty v-if="!dev" description="没有在线且开启 AI 的设备" />
    <div v-else class="body">
      <div class="side">
        <div class="feature">
          <div class="frow">
            <span class="fname">动态关键帧</span>
            <el-switch :model-value="feat('dyn_gop')" :loading="saving === 'dyn_gop'" @change="v => setFeature('dyn_gop', v)" />
          </div>
          <div class="fdesc">
            关键帧间隔从固定 2 秒拉长到 10 秒；以下情况立即插入关键帧：有人开始播放、画面从无目标变为有目标、触发告警。
            关键帧最占码率，间隔拉长后，省下的码率留给了普通帧。
          </div>
          <div class="fhint">看：<b>平均 QP</b>（越低越清晰），码率被封顶在 1200 kbps 时，省下的码率体现为 QP 下降；<b>关键帧间隔</b>；<b>起播耗时</b>不应变慢。</div>
        </div>
        <div class="feature">
          <div class="frow">
            <span class="fname">ROI 编码</span>
            <el-switch :model-value="feat('roi')" :loading="saving === 'roi'" @change="v => setFeature('roi', v)" />
          </div>
          <div class="fdesc">
            检测框（四周外扩 20%）作为感兴趣区域，编码时降低 QP（约 −10）。码率被封顶时，编码器会提高背景的 QP 来平衡，等于把码率从背景挪给目标。
          </div>
          <div class="fhint">看：<b>目标区域 PSNR</b> 上升、<b>背景 PSNR</b> 下降（对比模式下才测量，单位 dB，越高越接近原画面）。</div>
        </div>
        <div class="feature more muted">跳过静止画面推理等功能：后续加入</div>
        <el-divider />
        <div class="feature">
          <div class="frow">
            <span class="fname">对比模式</span>
            <el-switch :model-value="feat('compare')" :loading="saving === 'compare'" @change="v => setFeature('compare', v)" />
          </div>
          <div class="fdesc">
            同一画面再编码一路「全部功能关闭」的基准流，左右并排播放，并把两路输出解码后测量画质（PSNR）。每路多占约 5–15% CPU；离开本页自动关闭。
          </div>
        </div>
        <div v-if="verdict" class="verdict">
          <div class="vtitle">最近 1 分钟对比</div>
          <div v-for="l in verdict" :key="l">{{ l }}</div>
        </div>
      </div>

      <div class="main">
        <div class="players" :class="{ two: dev.video.compare }">
          <div v-for="p in players" :key="p.key" class="pcell">
            <div class="plabel"><i :style="{ background: p.color }" />{{ p.label }}</div>
            <div class="pbox">
              <Player :key="p.key + playKey + mode" :urls="p.urls" :mode="mode" :before-start="p.beforeStart"
                      @firstframe="ms => onFirstFrame(p.key, ms)" />
            </div>
            <div class="metrics">
              <div><span>码率</span><b>{{ fmt(p.s?.out_kbps, 0) }}</b> kbps</div>
              <div><span>平均 QP</span><b>{{ fmt(p.s?.qp, 1) }}</b></div>
              <div><span>关键帧间隔</span><b>{{ fmt(p.s?.key_interval_s, 1) }}</b> 秒</div>
              <div><span>关键帧大小</span><b>{{ fmt(p.s?.key_kb, 1) }}</b> KB</div>
              <div><span>普通帧大小</span><b>{{ fmt(p.s?.frame_kb, 1) }}</b> KB</div>
              <div><span>编码耗时</span><b>{{ fmt(p.s?.enc_ms, 1) }}</b> ms</div>
              <div><span>起播耗时</span><b>{{ startup[p.key] ?? '-' }}</b> ms</div>
              <template v-if="dev.video.compare">
                <div><span>目标区域 PSNR</span><b>{{ fmt(p.s?.psnr_roi, 1) }}</b> dB</div>
                <div><span>背景 PSNR</span><b>{{ fmt(p.s?.psnr_bg, 1) }}</b> dB</div>
              </template>
              <div class="wide"><span>关键帧来源（20 秒内）</span>{{ reasons(p.s?.key_reasons) }}</div>
            </div>
          </div>
        </div>

        <div class="charts">
          <LineChart title="码率" unit="kbps" :series="chartSeries('out_kbps')" :markers="markers" />
          <LineChart :title="dev.video.roi ? '平均 QP（ROI 开启时只代表背景）' : '平均 QP（越低画质越好）'" :digits="1" :zero="false" :series="chartSeries('qp')" :markers="markers" />
          <LineChart v-if="dev.video.compare" title="目标区域 PSNR（dB，越高越清晰）" unit="dB" :digits="1" :zero="false"
                     :series="chartSeries('psnr_roi')" :markers="markers" />
          <LineChart v-if="dev.video.compare" title="背景 PSNR（dB）" unit="dB" :digits="1" :zero="false"
                     :series="chartSeries('psnr_bg')" :markers="markers" />
        </div>
        <div class="note muted">
          数值为最近 20 秒的滑动平均，切换开关后约 20 秒曲线才完全反映新状态。两路画面和码率控制完全相同（CRF 23，码率上限 1200 kbps），只有功能开关不同。
          PSNR：Worker 把两路输出各自解码，和编码前的画面比较（亮度），两路使用同一组目标区域；对比模式下额外占用少量 CPU。
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import Player from '../components/Player.vue'
import LineChart from '../components/LineChart.vue'
import { loadDevices, store } from '../store'
import { api } from '../api'

const ON = '#3987e5'      // feature stream (validated pair for the dark surface)
const REF = '#d95926'     // baseline stream
const FEATURE_NAMES = { dyn_gop: '动态关键帧', roi: 'ROI 编码', compare: '对比模式' }
const REASONS = { gop: '定时', viewer: '有人播放', activity: '目标出现', alarm: '告警', x264: '编码器' }

const devices = computed(() => store.devices.filter(d => d.online && d.ai_enabled))
const deviceId = ref(null)
const dev = computed(() => devices.value.find(d => d.id === deviceId.value))
watch(devices, ds => {
  if (!ds.find(d => d.id === deviceId.value)) deviceId.value = ds[0]?.id ?? null
}, { immediate: true })

const mode = ref('webrtc')
const playKey = ref(0)
const saving = ref('')
const startup = reactive({})
const worker = computed(() => {
  const w = dev.value && store.workers[dev.value.stream]
  return w && w.state !== 'stopped' && w.stats ? w : null
})
const stats = computed(() => worker.value?.stats || {})

const players = computed(() => {
  const d = dev.value
  if (!d) return []
  const anyOn = d.video.dyn_gop || d.video.roi
  const q = stats.value.quality || {}
  const main = {
    key: 'main', color: ON, urls: d.play.ai, s: { ...stats.value, ...q.main },
    label: anyOn ? `功能开启 · ai/${d.stream}` : `当前输出（功能全部关闭）· ai/${d.stream}`,
    beforeStart: () => api.keyframe(d.id),
  }
  if (!d.video.compare) return [main]
  return [main, {
    key: 'ref', color: REF, urls: d.play.ai_ref, s: stats.value.ref && { ...stats.value.ref, ...q.ref },
    label: `基准（全部功能关闭）· ai_ref/${d.stream}`, beforeStart: null,
  }]
})

// ---- history for the charts: one sample per heartbeat ----
const history = ref([])          // [{ t, main, ref }]
const markers = ref([])          // [{ t, label }]
watch(() => worker.value?.stats, s => {
  if (!s) return
  const q = s.quality || {}
  history.value.push({ t: Date.now(), main: { ...s, ...q.main }, ref: s.ref && { ...s.ref, ...q.ref } })
  const cut = Date.now() - 10 * 60 * 1000
  while (history.value.length && history.value[0].t < cut) history.value.shift()
})
watch(deviceId, (id, old) => {
  Object.keys(pending).forEach(k => delete pending[k])
  history.value = []
  markers.value = []
  Object.keys(startup).forEach(k => delete startup[k])
  if (old) compareOff(old)
})
// A marker whenever a feature changes (here or in another browser)
watch(() => dev.value && { ...dev.value.video }, (v, o) => {
  if (!v || !o || dev.value?.id !== deviceId.value) return
  const now = Date.now()
  for (const k of Object.keys(FEATURE_NAMES)) {
    if (v[k] === o[k]) continue
    const label = `${FEATURE_NAMES[k]} ${v[k] ? '开' : '关'}`
    const prev = markers.value[markers.value.length - 1]
    // Switches flipped within a few seconds share one line (their labels would overlap)
    if (prev && now - prev.t < 3000) prev.label += `，${label}`
    else markers.value.push({ t: now, label })
  }
}, { deep: true })

const chartSeries = key => {
  const out = [{ name: dev.value?.video.compare ? '功能开启' : 'ai 输出', color: ON,
                 points: history.value.map(h => ({ t: h.t, v: h.main?.[key] })) }]
  if (history.value.some(h => h.ref)) {
    out.push({ name: '基准', color: REF, points: history.value.map(h => ({ t: h.t, v: h.ref?.[key] })) })
  }
  return out
}

// Plain-language summary of the last minute, only while both streams have data
const verdict = computed(() => {
  const recent = history.value.filter(h => h.t > Date.now() - 60000 && h.ref && h.main)
  if (recent.length < 3) return null
  const avg = (side, k) => {
    const v = recent.map(h => h[side][k]).filter(x => x != null)
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null
  }
  const kb = [avg('main', 'out_kbps'), avg('ref', 'out_kbps')]
  const qp = [avg('main', 'qp'), avg('ref', 'qp')]
  const ki = [avg('main', 'key_interval_s'), avg('ref', 'key_interval_s')]
  const lines = []
  if (kb[0] != null && kb[1]) {
    const d = (kb[0] - kb[1]) / kb[1] * 100
    lines.push(Math.abs(d) < 3 ? `码率基本相同（${kb[0].toFixed(0)} / ${kb[1].toFixed(0)} kbps）`
      : `码率${d < 0 ? '省' : '多'} ${Math.abs(d).toFixed(0)}%（${kb[0].toFixed(0)} / ${kb[1].toFixed(0)} kbps）`)
  }
  if (qp[0] != null && qp[1] != null && dev.value?.video.roi) {
    // x264 reports the frame's base QP; with ROI the targets sit ~10 below it. Not comparable.
    lines.push('ROI 开启时「平均 QP」只代表背景（目标区域低约 10），画质请看 PSNR')
  } else if (qp[0] != null && qp[1] != null) {
    const d = qp[1] - qp[0]
    lines.push(Math.abs(d) < 0.3 ? `平均 QP 基本相同（${qp[0].toFixed(1)}）`
      : `平均 QP ${d > 0 ? '低' : '高'} ${Math.abs(d).toFixed(1)}（${qp[0].toFixed(1)} / ${qp[1].toFixed(1)}），画质${d > 0 ? '更好' : '更差'}`)
  }
  if (ki[0] != null && ki[1] != null) lines.push(`关键帧间隔 ${ki[0].toFixed(1)} / ${ki[1].toFixed(1)} 秒`)
  for (const [k, name] of [['psnr_roi', '目标区域'], ['psnr_bg', '背景']]) {
    const v = [avg('main', k), avg('ref', k)]
    if (v[0] == null || v[1] == null) continue
    const d = v[0] - v[1]
    lines.push(`${name} PSNR ${d >= 0 ? '+' : ''}${d.toFixed(1)} dB（${v[0].toFixed(1)} / ${v[1].toFixed(1)}）`)
  }
  return lines
})

// Switches flipped in quick succession: each save must include the earlier, not yet saved ones,
// or it would send the old value back. Unsaved changes live in `pending` (shown at once) and the
// saves go out one after another.
const pending = reactive({})
const feat = k => pending[k] ?? dev.value?.video[k]
let saveChain = Promise.resolve()
function setFeature(k, v) {
  const id = dev.value.id
  pending[k] = v
  saveChain = saveChain.then(async () => {
    saving.value = k
    try {
      const d = store.devices.find(x => x.id === id)
      await api.putVideo(id, { ...d.video, ...pending })
      await loadDevices()
    } catch (e) {
      ElMessage.error(`保存失败：${e.message}`)
    } finally {
      if (pending[k] === v) delete pending[k]
      saving.value = ''
    }
  })
}

function compareOff(id) {
  const d = store.devices.find(x => x.id === id)
  if (d?.video.compare) api.putVideo(id, { ...d.video, compare: false }).then(loadDevices).catch(() => {})
}
onBeforeUnmount(() => { if (deviceId.value) compareOff(deviceId.value) })

function replay() {
  Object.keys(startup).forEach(k => delete startup[k])
  playKey.value++
}
const onFirstFrame = (key, ms) => { startup[key] = ms }

const fmt = (v, d) => (v == null ? '-' : Number(v).toFixed(d))
const reasons = r => (r && Object.keys(r).length
  ? Object.entries(r).map(([k, n]) => `${REASONS[k] || k}×${n}`).join('  ') : '-')
</script>

<style scoped>
.lab { height: 100%; display: flex; flex-direction: column; padding: 12px; box-sizing: border-box; gap: 10px; overflow: auto; }
.toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
.body { display: flex; gap: 14px; min-height: 0; }
.side { width: 260px; flex-shrink: 0; font-size: 13px; }
.feature { margin-bottom: 14px; }
.frow { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.fname { font-weight: 600; font-size: 14px; }
.fdesc { color: var(--el-text-color-regular); line-height: 1.6; }
.fhint { color: var(--el-text-color-secondary); line-height: 1.6; margin-top: 6px; font-size: 12px; }
.more { font-size: 12px; }
.verdict { background: var(--el-fill-color-light); border-radius: 4px; padding: 10px; line-height: 1.8; color: var(--el-text-color-regular); }
.vtitle { font-weight: 600; color: var(--el-text-color-primary); }
.main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 12px; }
.players { display: grid; grid-template-columns: minmax(0, 720px); gap: 10px; }
.players.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.pcell { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.plabel { font-size: 13px; display: flex; align-items: center; gap: 6px; color: var(--el-text-color-primary); }
.plabel i { width: 10px; height: 10px; border-radius: 2px; }
.pbox { aspect-ratio: 4 / 3; background: #000; border-radius: 4px; overflow: hidden; }
.metrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 4px 12px; font-size: 12px; color: var(--el-text-color-regular); }
.metrics span { color: var(--el-text-color-secondary); margin-right: 6px; }
.metrics b { color: var(--el-text-color-primary); font-weight: 600; font-variant-numeric: tabular-nums; }
.metrics .wide { grid-column: 1 / -1; }
.charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.note { font-size: 12px; }
</style>
