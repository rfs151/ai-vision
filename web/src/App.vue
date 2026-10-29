<template>
  <el-container style="height: 100%">
    <el-aside width="190px" class="aside">
      <div class="brand">
        <span class="dot" :class="{ on: store.connected }" />
        ai-vision
      </div>
      <el-menu :default-active="activeMenu" router>
        <el-menu-item index="/wall"><el-icon><Monitor /></el-icon>监控大屏</el-menu-item>
        <el-menu-item index="/devices"><el-icon><VideoCamera /></el-icon>设备管理</el-menu-item>
        <el-menu-item index="/rules"><el-icon><EditPen /></el-icon>规则配置</el-menu-item>
        <el-menu-item index="/alarms">
          <el-icon><Bell /></el-icon>告警中心
          <el-badge v-if="unread" :value="unread" :max="99" class="badge" />
        </el-menu-item>
      </el-menu>
      <div class="muted foot">{{ store.connected ? '实时连接正常' : '实时连接已断开，重连中…' }}</div>
    </el-aside>
    <el-main style="padding: 0">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElNotification } from 'element-plus'
import { Bell, EditPen, Monitor, VideoCamera } from '@element-plus/icons-vue'
import { connectWS, loadDevices, loadRecentAlarms, store } from './store'
import { ALARM_TYPES } from './api'

const route = useRoute()
const router = useRouter()
const activeMenu = computed(() => '/' + (route.path.split('/')[1] || 'wall'))
const unread = ref(0)

watch(() => store.lastAlarm, la => {
  if (!la) return
  const a = la.alarm
  if (route.path !== '/alarms') unread.value++
  ElNotification({
    type: a.type === 'intrusion' ? 'error' : 'warning',
    title: `${ALARM_TYPES[a.type]?.label || a.type} · ${a.stream}`,
    message: `规则「${a.rule_name}」${a.track_id ? ` 目标 #${a.track_id}` : ''}${a.detail?.count ? ` 人数 ${a.detail.count}` : ''}`,
    duration: 4000,
    onClick: () => router.push('/alarms'),
  })
})
watch(() => route.path, p => { if (p === '/alarms') unread.value = 0 })

onMounted(() => {
  connectWS()
  loadDevices().catch(() => {})
  loadRecentAlarms().catch(() => {})
})
</script>

<style scoped>
.aside { border-right: 1px solid var(--el-border-color); display: flex; flex-direction: column; }
.aside .el-menu { border-right: none; flex: 1; }
.brand { font-size: 18px; font-weight: 600; padding: 18px 20px; display: flex; align-items: center; gap: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: #f56c6c; }
.dot.on { background: #67c23a; }
.badge { margin-left: 8px; }
.foot { padding: 12px 20px; }
</style>
