<!-- 视频实验室：每路视频单独开关视频功能，并排对比开启前后的差异。
     YUV 直通：Worker 跳过整帧 YUV↔BGR 来回转换（worker/draw.py Yuv420），这里显示每帧转换耗时。
     矩形输入：推理输入只补到 32 的倍数（16:9 为 640×384），不再补成 640×640。INT8：换用量化后的模型。
     这两项开着且对比模式开启时，Worker 每 10 次推理用基准（FP32 模型、正方形输入）再算一次同一帧
     （detector.InferProbe），这里对比耗时和检测结果。
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
        <div class="feature">
          <div class="frow">
            <span class="fname">画面静止时跳过推理</span>
            <el-switch :model-value="feat('skip_static')" :loading="saving === 'skip_static'" @change="v => setFeature('skip_static', v)" />
          </div>
          <div class="fdesc">
            直接用摄像头编码时算好的运动矢量判断画面有没有变化（不做像素计算，每帧约 0.06 ms）。
            两次检测之间画面变化不到 1% 就不跑 YOLO，沿用上一次的检测结果，跟踪和规则照常运行；最长 2 秒必跑一次。不影响画面。
          </div>
          <div class="fhint">看：<b>每秒推理次数</b>和 <b>Worker CPU</b> 下降；画面有人走动时推理次数应回到每秒 5 次。</div>
          <div class="metrics">
            <div><span>画面运动</span><b>{{ fmt(stats.motion_pct, 1) }}</b> %</div>
            <div><span>每秒推理</span><b>{{ fmt(stats.infer_per_s, 1) }}</b> 次</div>
            <div><span>跳过</span><b>{{ fmt(stats.skip_pct, 0) }}</b> %</div>
            <div><span>Worker CPU</span><b>{{ fmt(stats.cpu, 0) }}</b> %</div>
          </div>
        </div>
        <div class="feature">
          <div class="frow">
            <span class="fname">YUV 直通（少做颜色转换）</span>
            <el-switch :model-value="feat('yuv_direct')" :loading="saving === 'yuv_direct'" @change="v => setFeature('yuv_direct', v)" />
          </div>
          <div class="fdesc">
            默认每帧都要做两次整帧颜色转换：解码后 YUV→BGR（为了画框，并拷贝一份给推理），编码前再 BGR→YUV。
            开启后，框直接画在 YUV 画面上，原样送去编码；推理只转换要检测的那几帧，并且缩放和转 BGR 一步完成。画面不变，检测结果不变。
          </div>
          <div class="fhint">看：<b>每帧颜色转换</b>耗时和 <b>Worker CPU</b> 下降，分辨率越高越明显（实测 1080p 每帧约 10 ms → 1.5 ms）。</div>
          <div class="metrics">
            <div><span>分辨率</span><b>{{ stats.resolution || '-' }}</b></div>
            <div><span>每帧颜色转换</span><b>{{ fmt(stats.conv_ms, 2) }}</b> ms</div>
            <div><span>推理前转换</span><b>{{ fmt(stats.prep_ms, 2) }}</b> ms/次</div>
            <div><span>Worker CPU</span><b>{{ fmt(stats.cpu, 0) }}</b> %</div>
          </div>
        </div>
        <div class="feature">
          <div class="frow">
            <span class="fname">矩形输入（少算灰边）</span>
            <el-switch :model-value="feat('rect_input')" :loading="saving === 'rect_input'" @change="v => setFeature('rect_input', v)" />
          </div>
          <div class="fdesc">
            默认每帧缩放后补灰成 640×640 再推理，16:9 画面有 44% 是灰边。开启后只补到 32 的倍数：16:9 输入 640×384，4:3 输入 640×480（不用补边）。
            人和车缩放后的大小不变，只是少算灰边。不用重新训练；模型要按动态尺寸导出（scripts/export_model.sh）。
          </div>
          <div class="fhint">看：<b>单次推理耗时</b>和 <b>Worker CPU</b> 下降；打开<b>对比模式</b>看同一帧的对比（下方）。</div>
          <div v-if="stats.rect_ok === false" class="fhint warn">当前模型是固定 640×640 导出的，不能改输入尺寸，开关不生效：运行 scripts/export_model.sh 重新导出后重启 Worker。</div>
          <div class="metrics">
            <div><span>推理输入</span><b>{{ stats.det_input || '-' }}</b></div>
            <div><span>单次推理</span><b>{{ fmt(stats.infer_avg_ms, 1) }}</b> ms</div>
          </div>
        </div>
        <div class="feature">
          <div class="frow">
            <span class="fname">INT8 量化模型</span>
            <el-switch :model-value="feat('int8')" :loading="saving === 'int8'" @change="v => setFeature('int8', v)" />
          </div>
          <div class="fdesc">
            模型权重和中间计算从 32 位浮点改成 8 位整数（NNCF 训练后量化，不用重新训练，用样例视频的几百帧做校准），模型约缩小到 1/4。
            检测头的坐标解码保持浮点。可以和矩形输入同时开。
          </div>
          <div class="fhint">看：<b>单次推理耗时</b>和 <b>Worker CPU</b> 下降；对比模式下看<b>检测框一致</b>和<b>置信度变化</b>，确认精度没有明显下降。</div>
          <div v-if="stats.int8_ok === false" class="fhint warn">没有找到 INT8 模型，开关不生效：运行 .venv-export/bin/python scripts/quantize_int8.py 后重启 Worker。</div>
          <div class="metrics">
            <div><span>当前模型</span><b>{{ stats.model || '-' }}</b></div>
            <div><span>单次推理</span><b>{{ fmt(stats.infer_avg_ms, 1) }}</b> ms</div>
          </div>
        </div>
        <div v-if="stats.probe_n" class="feature probe">
          <div class="fname">同一帧对比：当前设置 vs 基准</div>
          <div class="fhint">基准 = FP32 模型 + 640×640 正方形输入。每 10 次推理用基准再算一次同一帧，最近 20 秒的结果：</div>
          <div class="metrics">
            <div><span>当前（{{ stats.model }} · {{ stats.det_input }}）</span><b>{{ fmt(stats.probe_ms, 1) }}</b> ms</div>
            <div><span>基准</span><b>{{ fmt(stats.probe_base_ms, 1) }}</b> ms</div>
            <div><span>检测框一致</span><b>{{ fmt(stats.probe_found_pct, 0) }}</b> %</div>
            <div><span>置信度变化</span><b>{{ signed(stats.probe_score_delta, 3) }}</b></div>
          </div>
        </div>
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
          <LineChart title="单次推理耗时（最近 20 秒平均）" unit="ms" :digits="1" :series="mainSeries('infer_avg_ms', '推理')" :markers="markers" />
          <LineChart v-if="history.some(h => h.main?.probe_n)" title="同一帧推理耗时：当前设置 vs 基准（FP32 + 正方形）" unit="ms" :digits="1"
                     :series="probeSeries" :markers="markers" />
          <LineChart title="每秒推理次数" unit="次/秒" :digits="1" :series="mainSeries('infer_per_s', '推理')" :markers="markers" />
          <LineChart title="每帧颜色转换（解码后 + 编码前，不含对比编码）" unit="ms" :digits="2" :series="mainSeries('conv_ms', '转换')" :markers="markers" />
          <LineChart title="Worker CPU" unit="%" :series="mainSeries('cpu', 'CPU')" :markers="markers" />
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
const FEATURE_NAMES = { dyn_gop: '动态关键帧', roi: 'ROI 编码', skip_static: '跳过静止', yuv_direct: 'YUV 直通', rect_input: '矩形输入', int8: 'INT8', compare: '对比模式' }
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
  const anyOn = d.video.dyn_gop || d.video.roi || d.video.yuv_direct
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

// Worker-wide numbers (not per output stream): one series
const mainSeries = (key, name) => [{ name, color: ON, points: history.value.map(h => ({ t: h.t, v: h.main?.[key] })) }]
// rect_input probe: both inputs on the same frames
const probeSeries = computed(() => [
  { name: '当前设置', color: ON, points: history.value.map(h => ({ t: h.t, v: h.main?.probe_ms })) },
  { name: '基准', color: REF, points: history.value.map(h => ({ t: h.t, v: h.main?.probe_base_ms })) },
])

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
  const sq = avg('main', 'probe_base_ms'), rc = avg('main', 'probe_ms')
  if (sq && rc != null) {
    const found = avg('main', 'probe_found_pct'), ds = avg('main', 'probe_score_delta')
    const cur = [dev.value?.video.rect_input && '矩形输入', dev.value?.video.int8 && 'INT8'].filter(Boolean).join(' + ')
    lines.push(`${cur}：同一帧推理 ${sq.toFixed(1)} → ${rc.toFixed(1)} ms（少 ${((1 - rc / sq) * 100).toFixed(0)}%）` +
      (found != null ? `，检测框一致 ${found.toFixed(0)}%` : '') + (ds != null ? `，置信度 ${ds >= 0 ? '+' : ''}${ds.toFixed(3)}` : ''))
  }
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
const signed = (v, d) => (v == null ? '-' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(d)}`)
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
.warn { color: var(--el-color-warning); }
.probe { background: var(--el-fill-color-light); border-radius: 4px; padding: 10px; }
.probe .fname { margin-bottom: 4px; }
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
