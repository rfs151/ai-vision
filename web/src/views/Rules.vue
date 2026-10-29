<template>
  <div class="page rules">
    <div class="toolbar">
      <el-select v-model="deviceId" placeholder="选择设备" style="width: 200px" @change="go">
        <el-option v-for="d in store.devices" :key="d.id" :label="`${d.name} (${d.stream})`" :value="d.id" />
      </el-select>
      <el-radio-group v-model="tool" size="small" :disabled="!rules">
        <el-radio-button value="select">选择/编辑</el-radio-button>
        <el-radio-button value="zone">画区域</el-radio-button>
        <el-radio-button value="line">画越线</el-radio-button>
      </el-radio-group>
      <el-button size="small" :disabled="!dirty" @click="load">撤销修改</el-button>
      <el-button size="small" type="primary" :disabled="!dirty" :loading="saving" @click="save">保存并下发</el-button>
      <el-tag v-if="dirty" type="warning" size="small">有未保存的修改</el-tag>
      <span v-if="device" class="muted">规则版本 v{{ device.rules_version }} · 保存后 Worker 5 秒内热更新，无需重启</span>
    </div>

    <el-empty v-if="!device" description="请选择设备" />
    <div v-else class="body">
      <div class="stage-wrap">
        <div class="stage" :style="stageStyle">
          <Player :key="device.stream + mode" :urls="device.play.source" :mode="mode" fit="fill" @size="s => (size = s)" />
          <RuleCanvas v-if="rules" :rules="rules" :tool="tool" :selected="selected"
                      @select="s => (selected = s)" @done="onDone" @change="dirty = true" />
        </div>
        <div class="muted hint">{{ hint }}</div>
      </div>

      <div class="panel">
        <el-card shadow="never" header="全局参数">
          <el-form v-if="rules" label-width="96px" size="small">
            <el-form-item label="连续确认帧">
              <el-input-number v-model="rules.confirm_frames" :min="1" :max="50" @change="dirty = true" />
            </el-form-item>
            <el-form-item label="冷却时间(秒)">
              <el-input-number v-model="rules.cooldown_sec" :min="0" :step="10" @change="dirty = true" />
            </el-form-item>
            <el-form-item label="画面">
              <el-radio-group v-model="mode" size="small">
                <el-radio-button value="fmp4">fMP4</el-radio-button>
                <el-radio-button value="webrtc">WebRTC</el-radio-button>
              </el-radio-group>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never" style="margin-top: 12px">
          <template #header>
            <div class="card-head">
              <span>{{ cur ? (selected.kind === 'zone' ? '区域属性' : '越线属性') : '规则列表' }}</span>
              <el-button v-if="cur" link size="small" @click="selected = null">返回列表</el-button>
            </div>
          </template>

          <div v-if="!cur && rules">
            <el-empty v-if="!rules.zones.length && !rules.lines.length" description="还没有规则，选择上方工具在画面上绘制" :image-size="50" />
            <div v-for="z in rules.zones" :key="z.id" class="item" @click="selected = { kind: 'zone', id: z.id }">
              <el-tag size="small" type="warning">区域</el-tag> {{ z.name }}
              <span class="muted">{{ zoneSummary(z) }}</span>
            </div>
            <div v-for="l in rules.lines" :key="l.id" class="item" @click="selected = { kind: 'line', id: l.id }">
              <el-tag size="small">越线</el-tag> {{ l.name }}
              <span class="muted">{{ l.alarm_on ? `告警: ${ALARM_ON[l.alarm_on]}` : '仅计数' }}</span>
            </div>
          </div>

          <el-form v-else-if="cur" label-width="96px" size="small" @input="dirty = true" @change="dirty = true">
            <el-form-item label="ID"><span>{{ cur.id }}</span></el-form-item>
            <el-form-item label="名称"><el-input v-model="cur.name" /></el-form-item>
            <el-form-item label="目标类别">
              <el-select v-model="cur.classes" multiple filterable allow-create placeholder="全部" style="width: 100%"
                         @change="dirty = true">
                <el-option v-for="c in CLASSES" :key="c" :label="c" :value="c" />
              </el-select>
            </el-form-item>
            <template v-if="selected.kind === 'zone'">
              <el-form-item label="入侵告警"><el-switch v-model="cur.intrusion" @change="dirty = true" /></el-form-item>
              <el-form-item label="逗留(秒)">
                <el-input-number v-model="cur.dwell_sec" :min="1" :step="5" placeholder="不启用" controls-position="right"
                                 @change="dirty = true" />
              </el-form-item>
              <el-form-item label="人数上限">
                <el-input-number v-model="cur.max_count" :min="0" placeholder="不启用" controls-position="right"
                                 @change="dirty = true" />
              </el-form-item>
              <el-form-item label="顶点">{{ cur.polygon.length }} 个</el-form-item>
            </template>
            <template v-else>
              <el-form-item label="越线告警">
                <el-select v-model="cur.alarm_on" @change="dirty = true">
                  <el-option :value="null" label="不告警，仅计数" />
                  <el-option v-for="(t, k) in ALARM_ON" :key="k" :value="k" :label="t" />
                </el-select>
              </el-form-item>
              <el-form-item label="方向">
                <el-button size="small" @click="swapLine">反转方向</el-button>
              </el-form-item>
            </template>
            <el-form-item>
              <el-button type="danger" size="small" @click="removeCur">删除</el-button>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card v-if="stats" shadow="never" header="实时统计" style="margin-top: 12px">
          <div v-for="(n, z) in stats.zones" :key="z" class="muted">区域 {{ z }}：当前 {{ n }} 个目标</div>
          <div v-for="(c, l) in stats.lines" :key="l" class="muted">越线 {{ l }}：进 {{ c.in }} / 出 {{ c.out }}</div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import Player from '../components/Player.vue'
import RuleCanvas from '../components/RuleCanvas.vue'
import { api } from '../api'
import { loadDevices, store } from '../store'

const CLASSES = ['person', 'bicycle', 'car', 'motorcycle', 'bus', 'truck']
const ALARM_ON = { in: '进入时', out: '离开时', both: '双向' }

const route = useRoute()
const router = useRouter()
const deviceId = ref(route.params.id ? Number(route.params.id) : null)
const rules = ref(null)
const tool = ref('select')
const selected = ref(null)
const dirty = ref(false)
const saving = ref(false)
const mode = ref('fmp4')
const size = ref({ w: 16, h: 9 })

const device = computed(() => store.devices.find(d => d.id === deviceId.value))
const stats = computed(() => device.value && store.workers[device.value.stream]?.stats)
const cur = computed(() => {
  const s = selected.value
  if (!s || !rules.value) return null
  return (s.kind === 'zone' ? rules.value.zones : rules.value.lines).find(x => x.id === s.id) || null
})
const hint = computed(() => ({
  select: '拖动顶点调整形状，拖动内部整体移动；双击区域边线插入顶点，右键顶点删除',
  zone: '单击添加顶点；单击起点或双击完成；右键撤销上一个点；Esc 取消',
  line: '单击两次确定越线；箭头指向的一侧为"进"(IN)',
}[tool.value]))

// Keep the video's aspect ratio when height-limited: shapes are stored relative to the frame
const stageStyle = computed(() => ({
  aspectRatio: `${size.value.w} / ${size.value.h}`,
  width: `min(100%, calc((100vh - 140px) * ${size.value.w / size.value.h}))`,
}))

const zoneSummary = z => [z.intrusion && '入侵', z.dwell_sec && `逗留${z.dwell_sec}s`, z.max_count != null && `上限${z.max_count}`]
  .filter(Boolean).join(' · ') || '仅统计'

async function load() {
  if (!deviceId.value) return
  try {
    const r = await api.getRules(deviceId.value)
    // Plain arrays (pydantic tuples come back as lists) so the canvas can mutate points in place
    rules.value = JSON.parse(JSON.stringify(r))
    selected.value = null
    dirty.value = false
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function save() {
  saving.value = true
  try {
    rules.value = await api.putRules(deviceId.value, rules.value)
    dirty.value = false
    await loadDevices()
    ElMessage.success(`已保存，规则版本 v${device.value?.rules_version}`)
  } catch (e) {
    ElMessage.error(`保存失败：${e.message}`)
  } finally {
    saving.value = false
  }
}

async function confirmDiscard() {
  if (!dirty.value) return true
  try {
    await ElMessageBox.confirm('有未保存的修改，确定放弃吗？', '提示', { type: 'warning' })
    return true
  } catch {
    return false
  }
}

async function go(id) {
  router.replace(`/rules/${id}`)
}

function onDone(s) {
  tool.value = 'select'
  if (s) selected.value = s
}

function removeCur() {
  const s = selected.value
  const list = s.kind === 'zone' ? rules.value.zones : rules.value.lines
  list.splice(list.findIndex(x => x.id === s.id), 1)
  selected.value = null
  dirty.value = true
}

function swapLine() {
  const l = cur.value
  ;[l.p1, l.p2] = [l.p2, l.p1]
  dirty.value = true
}

watch(() => route.params.id, async (id, old) => {
  if (id === old) return
  deviceId.value = id ? Number(id) : null
  await load()
})
onBeforeRouteLeave(confirmDiscard)
onBeforeRouteUpdate(async () => {
  const ok = await confirmDiscard()
  if (!ok) deviceId.value = Number(route.params.id)   // revert the dropdown
  return ok
})

if (!store.devices.length) loadDevices().catch(() => {})
if (!deviceId.value) {
  // No device in URL: pick the first one once devices are loaded
  const stop = watch(() => store.devices, ds => {
    if (ds.length && !deviceId.value) { go(ds[0].id); stop() }
  }, { immediate: true })
}
load()
</script>

<style scoped>
.rules { display: flex; flex-direction: column; }
.body { display: flex; gap: 16px; flex: 1; min-height: 0; }
.stage-wrap { flex: 1; min-width: 0; }
.stage { position: relative; background: #000; margin: 0 auto; }
.hint { margin-top: 6px; }
.panel { width: 330px; flex-shrink: 0; overflow-y: auto; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.item { padding: 6px 4px; cursor: pointer; display: flex; gap: 8px; align-items: center; border-radius: 4px; }
.item:hover { background: var(--el-fill-color-light); }
</style>
