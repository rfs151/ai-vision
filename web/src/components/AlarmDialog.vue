<template>
  <el-dialog :model-value="!!modelValue" width="860px" :title="title" @close="emit('update:modelValue', null)">
    <template v-if="modelValue">
      <el-tabs v-model="tab">
        <el-tab-pane label="告警截图" name="snap">
          <el-image v-if="modelValue.snapshot_url" :src="modelValue.snapshot_url" fit="contain"
                    :preview-src-list="[modelValue.snapshot_url]" style="width: 100%; max-height: 500px" />
          <el-empty v-else description="没有截图" :image-size="60" />
        </el-tab-pane>
        <el-tab-pane name="clip">
          <template #label>告警录像<span class="muted" style="margin-left: 4px">±10s</span></template>
          <video v-if="modelValue.clip_url && tab === 'clip'" :src="modelValue.clip_url" controls autoplay muted
                 style="width: 100%; max-height: 500px; background: #000" />
          <el-empty v-else-if="!modelValue.clip_url" :image-size="60"
                    :description="recent ? '录像生成中（告警后约 13 秒）…' : '没有录像'" />
        </el-tab-pane>
      </el-tabs>
      <el-descriptions :column="3" border size="small" style="margin-top: 12px">
        <el-descriptions-item label="设备">{{ modelValue.stream }}</el-descriptions-item>
        <el-descriptions-item label="规则">{{ modelValue.rule_name }} ({{ modelValue.rule_id }})</el-descriptions-item>
        <el-descriptions-item label="时间">{{ fmtTime(modelValue.created_at) }}</el-descriptions-item>
        <el-descriptions-item label="目标">{{ modelValue.track_id ? `#${modelValue.track_id} ${modelValue.cls}` : '-' }}</el-descriptions-item>
        <el-descriptions-item label="置信度">{{ modelValue.score ? modelValue.score.toFixed(2) : '-' }}</el-descriptions-item>
        <el-descriptions-item label="详情">{{ detailText }}</el-descriptions-item>
      </el-descriptions>
    </template>
    <template #footer>
      <el-button @click="emit('update:modelValue', null)">关闭</el-button>
      <el-button type="info" @click="set('false')">标记误报</el-button>
      <el-button type="success" @click="set('ack')">确认处理</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ALARM_TYPES, api, fmtTime } from '../api'

const props = defineProps({ modelValue: Object, startTab: { type: String, default: 'snap' } })
const emit = defineEmits(['update:modelValue', 'changed'])
const tab = ref(props.startTab)
watch(() => props.modelValue?.id, () => { tab.value = props.startTab })
// Clip is cut ~13 s after the alarm; a newer alarm without one is still "being generated"
const recent = computed(() => props.modelValue && Date.now() - new Date(props.modelValue.created_at) < 60000)
const title = computed(() => props.modelValue ? `告警 #${props.modelValue.id} · ${ALARM_TYPES[props.modelValue.type]?.label || props.modelValue.type}` : '')
const detailText = computed(() => {
  const d = props.modelValue?.detail || {}
  if (d.seconds != null) return `停留 ${d.seconds} 秒`
  if (d.count != null) return `${d.count} 人（上限 ${d.max}）`
  if (d.in != null) return `累计 进 ${d.in} / 出 ${d.out}`
  return '-'
})

async function set(status) {
  try {
    const a = await api.setAlarmStatus(props.modelValue.id, status)
    ElMessage.success('已更新')
    emit('changed', a)
    emit('update:modelValue', null)
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>
