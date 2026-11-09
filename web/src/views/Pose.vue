<!-- 姿态识别: devices running the pose model (yolo11n-pose, 17 keypoints per person) and the fall rule.
     The worker draws skeletons and posture labels into the ai/ stream, so the player shows that stream. -->
<template>
  <div class="pose">
    <div class="main">
      <div class="toolbar">
        <el-select v-model="deviceId" placeholder="选择姿态识别设备" style="width: 200px" size="small">
          <el-option v-for="d in poseDevices" :key="d.id" :label="d.name" :value="d.id">
            <span class="dot" :class="{ on: d.online }" />{{ d.name }}
          </el-option>
        </el-select>
        <el-radio-group v-model="mode" size="small">
          <el-radio-button value="webrtc">WebRTC</el-radio-button>
          <el-radio-button value="fmp4">HTTP-fMP4</el-radio-button>
        </el-radio-group>
        <el-popover placement="bottom" :width="300" trigger="click">
          <template #reference><el-button size="small">管理姿态识别设备</el-button></template>
          <div class="muted" style="margin-bottom: 8px">
            姿态识别只识别人（不识别车辆），每人输出 17 个关键点。切换后 Worker 会自动重启。
          </div>
          <div v-for="d in store.devices" :key="d.id" class="dev-row">
            <span>{{ d.name }}</span>
            <el-switch :model-value="d.task === 'pose'" :loading="switching === d.id"
                       active-text="姿态" inactive-text="人车" inline-prompt @change="v => setTask(d, v)" />
          </div>
        </el-popover>
      </div>

      <el-empty v-if="!dev" description="还没有姿态识别设备：推一路名为 fall 的流，或在「管理姿态识别设备」里切换" />
      <template v-else>
        <div class="video">
          <Player v-if="dev.online" :key="dev.stream + mode" :urls="dev.play.ai" :mode="mode"
                  :before-start="() => api.keyframe(dev.id)" />
          <div v-else class="offline">设备离线（设备管理 → 测试推流，打开 {{ dev.stream }}）</div>
        </div>
        <div class="stats">
          <div><span>人数</span><b>{{ people.length }}</b></div>
          <div><span>摔倒次数</span><b class="red">{{ stats.falls ?? 0 }}</b></div>
          <div><span>推理</span><b>{{ stats.infer_ms ?? '-' }}</b> ms</div>
          <div><span>每秒推理</span><b>{{ dev.det_fps }}</b> 次</div>
          <div><span>画面</span><b>{{ stats.out_fps ?? '-' }}</b> fps</div>
          <div class="people">
            <el-tag v-for="p in people" :key="p.id" :type="POSE[p.pose]?.tag || 'info'" effect="dark">
              #{{ p.id }} {{ POSE[p.pose]?.label || p.pose || '-' }}
            </el-tag>
          </div>
        </div>
      </template>
    </div>

    <div class="side">
      <template v-if="dev">
        <div class="side-title">摔倒检测</div>
        <el-form label-width="96px" size="small">
          <el-form-item label="启用"><el-switch v-model="fall.enabled" /></el-form-item>
          <el-form-item label="倒地确认">
            <el-input-number v-model="fall.confirm_sec" :min="0.5" :max="30" :step="0.5" /> <span class="unit">秒</span>
          </el-form-item>
          <el-form-item label="倒下用时 ≤">
            <el-input-number v-model="fall.window_sec" :min="0.5" :max="10" :step="0.5" /> <span class="unit">秒</span>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :disabled="!dirty" :loading="saving" @click="save">保存</el-button>
            <el-button :disabled="!dirty" @click="loadFall">还原</el-button>
          </el-form-item>
        </el-form>
        <div class="explain">
          <p>每个人按身体外框和躯干（肩 → 胯）判断姿态：<b>站立</b>（框窄高、躯干竖直）、
            <b>弯腰</b>（蹲下、坐下、弯腰捡东西）、<b>躺地</b>（框比人宽，或躯干接近水平）。</p>
          <p>从站立到躺地用时不超过「倒下用时」，并且躺地持续「倒地确认」秒，才算摔倒。
            慢慢躺下、摔倒后马上站起来都不报警。</p>
          <p>告警冷却时间 <b>{{ dev.rules.cooldown_sec }}</b> 秒（在「规则配置」里改）：冷却期内再次摔倒不告警，但计入摔倒次数。</p>
          <p class="muted">演示视频来自 UR Fall Detection Dataset（University of Rzeszów），CC BY-NC-SA 4.0，仅限非商业用途。</p>
        </div>

        <div class="side-title" style="margin-top: 16px">摔倒告警</div>
        <el-empty v-if="!alarms.length" description="暂无摔倒告警" :image-size="50" />
        <div v-for="a in alarms" :key="a.id" class="alarm" @click="detail = a">
          <el-image v-if="a.snapshot_url" :src="a.snapshot_url" fit="cover" class="thumb" />
          <div class="info">
            <div><el-tag type="danger" size="small">摔倒</el-tag> 目标 #{{ a.track_id }}</div>
            <div class="muted">{{ fmtTime(a.created_at) }}</div>
          </div>
        </div>
      </template>
    </div>

    <AlarmDialog v-model="detail" />
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import Player from '../components/Player.vue'
import AlarmDialog from '../components/AlarmDialog.vue'
import { loadDevices, store } from '../store'
import { api, fmtTime } from '../api'

const POSE = {
  stand: { label: '站立', tag: 'success' },
  bend: { label: '弯腰', tag: 'warning' },
  lying: { label: '躺地', tag: 'warning' },
  FALL: { label: '摔倒', tag: 'danger' },
}

const saved = (k, d) => { try { return JSON.parse(localStorage.getItem('pose.' + k)) ?? d } catch { return d } }
const mode = ref(saved('mode', 'webrtc'))
const deviceId = ref(saved('device', null))
watch([mode, deviceId], () => {
  try {
    localStorage.setItem('pose.mode', JSON.stringify(mode.value))
    localStorage.setItem('pose.device', JSON.stringify(deviceId.value))
  } catch { /* storage unavailable */ }
})

const poseDevices = computed(() => store.devices.filter(d => d.task === 'pose'))
const dev = computed(() => poseDevices.value.find(d => d.id === deviceId.value))
// Default / fallback: the first online pose device
watch(poseDevices, list => {
  if (!dev.value && list.length) deviceId.value = (list.find(d => d.online) || list[0]).id
}, { immediate: true })

const stats = computed(() => (dev.value && store.workers[dev.value.stream]?.stats) || {})
const people = computed(() => Object.entries(stats.value.poses || {}).map(([id, pose]) => ({ id, pose })))

// Fall rule form (part of the device rules; saved with PUT /rules, the worker hot-reloads it)
const DEFAULT_FALL = { enabled: true, confirm_sec: 2, window_sec: 3 }
const fall = reactive({ ...DEFAULT_FALL })
const savedFall = ref({ ...DEFAULT_FALL })
const dirty = computed(() => JSON.stringify({ ...fall }) !== JSON.stringify(savedFall.value))
const saving = ref(false)
function loadFall() {
  savedFall.value = { ...DEFAULT_FALL, ...(dev.value?.rules.fall || {}) }
  Object.assign(fall, savedFall.value)
}
watch(() => dev.value?.id, loadFall, { immediate: true })

async function save() {
  saving.value = true
  try {
    const rules = await api.getRules(dev.value.id)
    await api.putRules(dev.value.id, { ...rules, fall: { ...fall } })
    savedFall.value = { ...fall }
    ElMessage.success('已保存，5 秒内生效')
    loadDevices().catch(() => {})
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

const switching = ref(null)
async function setTask(d, pose) {
  switching.value = d.id
  try {
    // A fall lasts under a second: sample pose streams more often
    await api.updateDevice(d.id, { task: pose ? 'pose' : 'detect', det_fps: pose ? 8 : 5 })
    await loadDevices()
    if (pose) deviceId.value = d.id
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    switching.value = null
  }
}

// Fall alarms of this device: loaded once, then live from the WebSocket
const alarms = ref([])
const detail = ref(null)
watch(() => dev.value?.id, async id => {
  alarms.value = []
  if (id) alarms.value = (await api.alarms({ device_id: id, type: 'fall', size: 20 }).catch(() => ({ items: [] }))).items
}, { immediate: true })
watch(() => store.lastAlarm, la => {
  const a = la?.alarm
  if (a && a.type === 'fall' && a.device_id === dev.value?.id) alarms.value = [a, ...alarms.value].slice(0, 20)
})
watch(() => store.recentAlarms.map(a => a.clip_url), () => {
  // clip URLs arrive later (alarm_clip); copy them over
  for (const a of alarms.value) {
    const r = store.recentAlarms.find(x => x.id === a.id)
    if (r?.clip_url) a.clip_url = r.clip_url
  }
})
</script>

<style scoped>
.pose { display: flex; height: 100%; }
.main { flex: 1; padding: 12px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.video { flex: 1; position: relative; background: #000; border-radius: 4px; overflow: hidden; min-height: 240px; }
.offline { color: #ccc; height: 100%; display: flex; align-items: center; justify-content: center; }
.stats { display: flex; gap: 22px; align-items: center; flex-wrap: wrap; font-size: 13px; }
.stats > div > span { color: var(--el-text-color-secondary); margin-right: 6px; }
.stats b { font-size: 18px; }
.stats .red { color: var(--el-color-danger); }
.people { display: flex; gap: 6px; flex-wrap: wrap; }
.side { width: 300px; border-left: 1px solid var(--el-border-color); overflow-y: auto; padding: 12px; box-sizing: border-box; }
.side-title { font-weight: 600; margin-bottom: 10px; }
.unit { margin-left: 6px; color: var(--el-text-color-secondary); }
.explain { font-size: 12px; line-height: 1.6; color: var(--el-text-color-regular); }
.explain p { margin: 0 0 6px; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #909399; margin-right: 6px; }
.dot.on { background: #67c23a; }
.dev-row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; }
.alarm { display: flex; gap: 8px; padding: 6px; border-radius: 4px; cursor: pointer; font-size: 13px; }
.alarm:hover { background: var(--el-fill-color-light); }
.thumb { width: 96px; height: 64px; border-radius: 3px; flex-shrink: 0; }
.info { display: flex; flex-direction: column; gap: 4px; }
</style>
