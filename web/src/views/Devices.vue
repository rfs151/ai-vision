<template>
  <div class="page">
    <div class="toolbar">
      <el-button type="primary" :icon="Plus" @click="openEdit()">添加设备</el-button>
      <el-button :icon="Refresh" @click="refresh">刷新</el-button>
      <el-button :icon="VideoCamera" @click="openPush">测试推流</el-button>
      <span class="muted">推流到 live/&lt;名称&gt; 的流会被自动注册为设备；摄像头请填写拉流地址</span>
    </div>

    <el-table :data="store.devices" v-loading="loading" border>
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column label="名称 / 流" min-width="160">
        <template #default="{ row }">
          <div>{{ row.name }}</div>
          <div class="muted">{{ row.app }}/{{ row.stream }}</div>
        </template>
      </el-table-column>
      <el-table-column label="来源" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">{{ row.source_url || '推流' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.online ? 'success' : 'info'" size="small">{{ row.online ? '在线' : '离线' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="AI 分析" min-width="230">
        <template #default="{ row }">
          <el-switch :model-value="row.ai_enabled" size="small" @change="v => toggleAI(row, v)" />
          <template v-if="worker(row)">
            <el-tag :type="stateTag(worker(row).state)" size="small" style="margin-left: 8px">{{ worker(row).state }}</el-tag>
            <span v-if="worker(row).stats?.out_fps != null" class="muted" style="margin-left: 6px">
              {{ worker(row).stats.out_fps }}fps · {{ worker(row).stats.infer_ms }}ms
              <template v-if="worker(row).restarts"> · 重启 {{ worker(row).restarts }}</template>
            </span>
          </template>
        </template>
      </el-table-column>
      <el-table-column label="规则" width="110">
        <template #default="{ row }">{{ row.rules.zones.length }} 区域 / {{ row.rules.lines.length }} 线</template>
      </el-table-column>
      <el-table-column label="操作" width="330" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="$router.push(`/rules/${row.id}`)">规则</el-button>
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-dropdown size="small" style="margin: 0 12px" @command="a => worker_(row, a)">
            <el-button size="small">Worker<el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="restart">重启</el-dropdown-item>
                <el-dropdown-item command="stop">停止</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <el-popconfirm title="删除设备及其全部告警？" @confirm="remove(row)">
            <template #reference><el-button size="small" type="danger">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="push.visible" title="测试推流" width="560px" @closed="stopPoll">
      <p class="muted" style="margin-top: 0">循环推送示例视频，模拟摄像头。打开后约 5 秒开始推流，并自动登记为设备；关闭后停止推流，设备变为离线，监控大屏不再显示。</p>
      <el-table :data="push.list" v-loading="push.loading" size="small">
        <el-table-column prop="name" label="流名称" width="110" />
        <el-table-column prop="file" label="视频文件" min-width="200" show-overflow-tooltip />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.pushing" type="success" size="small">推流中</el-tag>
            <el-tag v-else-if="row.enabled" type="warning" size="small">启动中</el-tag>
            <el-tag v-else type="info" size="small">未推流</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="推流" width="80">
          <template #default="{ row }">
            <el-switch :model-value="row.enabled" size="small" :loading="push.busy === row.name"
                       @change="v => togglePush(row, v)" />
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <el-dialog v-model="edit.visible" :title="edit.id ? '编辑设备' : '添加设备'" width="520px">
      <el-form :model="edit.form" label-width="96px">
        <el-form-item label="名称" required><el-input v-model="edit.form.name" /></el-form-item>
        <el-form-item label="流名称" required>
          <el-input v-model="edit.form.stream" :disabled="!!edit.id" placeholder="字母、数字、_、-">
            <template #prepend>live/</template>
          </el-input>
        </el-form-item>
        <el-form-item label="拉流地址">
          <el-input v-model="edit.form.source_url" placeholder="rtsp://user:pass@camera/stream（推流设备留空）" />
        </el-form-item>
        <el-form-item label="AI 分析"><el-switch v-model="edit.form.ai_enabled" /></el-form-item>
        <el-form-item label="检测帧率">
          <el-input-number v-model="edit.form.det_fps" :min="1" :max="30" :step="1" /> <span class="muted" style="margin-left: 8px">次/秒</span>
        </el-form-item>
        <el-form-item label="检测类别">
          <el-select v-model="edit.form.classList" multiple filterable allow-create style="width: 100%">
            <el-option v-for="c in CLASSES" :key="c" :label="c" :value="c" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="edit.visible = false">取消</el-button>
        <el-button type="primary" :loading="edit.saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowDown, Plus, Refresh, VideoCamera } from '@element-plus/icons-vue'
import { api } from '../api'
import { loadDevices, store } from '../store'

const CLASSES = ['person', 'bicycle', 'car', 'motorcycle', 'bus', 'truck', 'dog', 'cat', 'backpack', 'handbag', 'suitcase']
const loading = ref(false)
const edit = reactive({ visible: false, saving: false, id: null, form: {} })
const push = reactive({ visible: false, loading: false, busy: '', list: [] })
let pollTimer = null

const worker = row => store.workers[row.stream]
const stateTag = s => ({ running: 'success', starting: 'warning', backoff: 'danger', stopped: 'info' }[s])

async function refresh() {
  loading.value = true
  try { await loadDevices() } catch (e) { ElMessage.error(e.message) } finally { loading.value = false }
}

function openEdit(row) {
  edit.id = row?.id || null
  edit.form = row
    ? { name: row.name, stream: row.stream, source_url: row.source_url || '', ai_enabled: row.ai_enabled,
        det_fps: row.det_fps, classList: row.classes.split(',').filter(Boolean) }
    : { name: '', stream: '', source_url: '', ai_enabled: true, det_fps: 5,
        classList: ['person', 'bicycle', 'car', 'motorcycle', 'bus', 'truck'] }
  edit.visible = true
}

async function save() {
  const f = edit.form
  const body = { name: f.name, source_url: f.source_url || null, ai_enabled: f.ai_enabled,
                 det_fps: f.det_fps, classes: f.classList.join(',') }
  edit.saving = true
  try {
    if (edit.id) await api.updateDevice(edit.id, body)
    else await api.createDevice({ ...body, stream: f.stream })
    ElMessage.success('已保存')
    edit.visible = false
    await refresh()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    edit.saving = false
  }
}

async function toggleAI(row, v) {
  try { await api.updateDevice(row.id, { ai_enabled: v }); await refresh() } catch (e) { ElMessage.error(e.message) }
}

async function worker_(row, action) {
  try {
    const st = await api.workerAction(row.id, action)
    if (st) store.workers[row.stream] = st
    ElMessage.success(action === 'stop' ? '已停止' : '已重启')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// Test streams: the pusher applies a change within ~5 s, so poll while the dialog is open
async function loadPush() {
  try { push.list = await api.testStreams() } catch (e) { ElMessage.error(e.message) }
}

async function openPush() {
  push.visible = true
  push.loading = true
  await loadPush()
  push.loading = false
  stopPoll()
  pollTimer = setInterval(() => { loadPush(); loadDevices().catch(() => {}) }, 2000)
}

function stopPoll() {
  clearInterval(pollTimer)
  pollTimer = null
}

async function togglePush(row, v) {
  push.busy = row.name
  try { push.list = await api.setTestStream(row.name, v) } catch (e) { ElMessage.error(e.message) } finally { push.busy = '' }
}

onBeforeUnmount(stopPoll)

async function remove(row) {
  try { await api.deleteDevice(row.id); ElMessage.success('已删除'); await refresh() } catch (e) { ElMessage.error(e.message) }
}

refresh()
</script>
