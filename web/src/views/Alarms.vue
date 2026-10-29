<template>
  <div class="page">
    <div class="cards">
      <el-card shadow="never" class="stat">
        <div class="muted">近 24 小时告警</div>
        <div class="num">{{ total24 }}</div>
      </el-card>
      <el-card shadow="never" class="stat danger">
        <div class="muted">未处理</div>
        <div class="num">{{ stats.unhandled ?? 0 }}</div>
      </el-card>
      <el-card v-for="(t, k) in ALARM_TYPES" :key="k" shadow="never" class="stat">
        <div class="muted">{{ t.label }}</div>
        <div class="num small">{{ stats.by_type?.[k] ?? 0 }}</div>
      </el-card>
    </div>

    <div class="toolbar">
      <el-select v-model="f.stream" placeholder="全部设备" clearable style="width: 150px">
        <el-option v-for="d in store.devices" :key="d.id" :label="d.name" :value="d.stream" />
      </el-select>
      <el-select v-model="f.type" placeholder="全部类型" clearable style="width: 130px">
        <el-option v-for="(t, k) in ALARM_TYPES" :key="k" :label="t.label" :value="k" />
      </el-select>
      <el-select v-model="f.status" placeholder="全部状态" clearable style="width: 120px">
        <el-option v-for="(t, k) in ALARM_STATUS" :key="k" :label="t.label" :value="k" />
      </el-select>
      <el-date-picker v-model="f.range" type="datetimerange" range-separator="至" start-placeholder="开始时间"
                      end-placeholder="结束时间" value-format="YYYY-MM-DDTHH:mm:ss" style="width: 360px" />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="reset">重置</el-button>
      <el-switch v-model="live" active-text="实时追加" style="margin-left: 8px" />
    </div>

    <el-table :data="items" v-loading="loading" border @row-dblclick="r => (detail = r)">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column label="截图" width="130">
        <template #default="{ row }">
          <el-image v-if="row.snapshot_url" :src="row.snapshot_url" fit="cover" class="thumb"
                    :preview-src-list="[row.snapshot_url]" preview-teleported />
        </template>
      </el-table-column>
      <el-table-column label="类型" width="110">
        <template #default="{ row }">
          <el-tag :type="ALARM_TYPES[row.type]?.tag" size="small">{{ ALARM_TYPES[row.type]?.label || row.type }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="stream" label="设备" width="110" />
      <el-table-column label="规则" min-width="120">
        <template #default="{ row }">{{ row.rule_name }} <span class="muted">({{ row.rule_id }})</span></template>
      </el-table-column>
      <el-table-column label="目标" width="120">
        <template #default="{ row }">{{ row.track_id ? `#${row.track_id} ${row.cls}` : '-' }}</template>
      </el-table-column>
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="ALARM_STATUS[row.status]?.tag" size="small">{{ ALARM_STATUS[row.status]?.label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button size="small" @click="detail = row">详情</el-button>
          <el-button v-if="row.status === 'new'" size="small" type="success" @click="setStatus(row, 'ack')">确认</el-button>
          <el-button v-if="row.status === 'new'" size="small" type="info" @click="setStatus(row, 'false')">误报</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination v-model:current-page="page" v-model:page-size="size" :total="total" :page-sizes="[20, 50, 100]"
                   layout="total, sizes, prev, pager, next" style="margin-top: 12px; justify-content: flex-end"
                   @current-change="fetch" @size-change="fetch" />

    <AlarmDialog v-model="detail" @changed="onChanged" />
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import AlarmDialog from '../components/AlarmDialog.vue'
import { ALARM_STATUS, ALARM_TYPES, api, fmtTime } from '../api'
import { onEvent, store } from '../store'

const f = reactive({ stream: '', type: '', status: '', range: null })
const items = ref([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const loading = ref(false)
const stats = ref({})
const detail = ref(null)
const live = ref(true)

const total24 = computed(() => Object.values(stats.value.by_type || {}).reduce((a, b) => a + b, 0))
const filtered = () => f.stream || f.type || f.status || f.range

async function fetch() {
  loading.value = true
  try {
    const d = await api.alarms({
      stream: f.stream, type: f.type, status: f.status, page: page.value, size: size.value,
      since: f.range?.[0], until: f.range?.[1],
    })
    items.value = d.items
    total.value = d.total
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function loadStats() {
  try { stats.value = await api.alarmStats(24) } catch { /* keep old */ }
}

function search() { page.value = 1; fetch() }
function reset() { Object.assign(f, { stream: '', type: '', status: '', range: null }); search() }

async function setStatus(row, status) {
  try {
    onChanged(await api.setAlarmStatus(row.id, status))
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function onChanged(a) {
  const i = items.value.findIndex(x => x.id === a.id)
  if (i >= 0) items.value[i] = a
  loadStats()
}

// New alarms: prepend when looking at the unfiltered first page
const off = onEvent((type, data) => {
  if (type !== 'alarm') return
  loadStats()
  if (live.value && page.value === 1 && !filtered()) {
    items.value.unshift(data)
    items.value.splice(size.value)
    total.value++
  }
})
onBeforeUnmount(off)

fetch()
loadStats()
</script>

<style scoped>
.cards { display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.stat { min-width: 120px; }
.stat :deep(.el-card__body) { padding: 12px 16px; }
.num { font-size: 26px; font-weight: 600; margin-top: 4px; }
.num.small { font-size: 20px; }
.danger .num { color: var(--el-color-danger); }
.thumb { width: 108px; height: 64px; border-radius: 3px; }
</style>
