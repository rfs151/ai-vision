<template>
  <div class="page">
    <el-card shadow="never" style="max-width: 820px">
      <template #header>
        <span>告警推送</span>
        <span class="muted" style="margin-left: 10px">企业微信、钉钉群机器人，每个渠道每分钟最多 {{ cfg.rate_per_min }} 条</span>
      </template>
      <el-form :model="cfg" label-width="130px" v-loading="loading">
        <el-form-item label="启用推送">
          <el-switch v-model="cfg.enabled" />
        </el-form-item>
        <el-form-item label="推送的告警类型">
          <el-checkbox-group v-model="cfg.types">
            <el-checkbox v-for="(t, k) in ALARM_TYPES" :key="k" :value="k">{{ t.label }}</el-checkbox>
          </el-checkbox-group>
          <div class="hint">都不勾选表示全部推送</div>
        </el-form-item>

        <el-divider content-position="left">企业微信</el-divider>
        <el-form-item label="Webhook 地址">
          <el-input v-model="cfg.wecom_webhook" clearable
                    placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." />
          <div class="hint">群聊 → 右上角 → 消息推送（群机器人）→ 添加 → 复制 Webhook 地址</div>
        </el-form-item>
        <el-form-item label="附带截图">
          <el-switch v-model="cfg.wecom_image" />
          <span class="hint inline">截图作为图片消息直接发送，不需要公网地址</span>
        </el-form-item>

        <el-divider content-position="left">钉钉</el-divider>
        <el-form-item label="Webhook 地址">
          <el-input v-model="cfg.dingtalk_webhook" clearable
                    placeholder="https://oapi.dingtalk.com/robot/send?access_token=..." />
          <div class="hint">群设置 → 机器人 → 添加自定义机器人。安全设置选"加签"，或者自定义关键词填"告警"</div>
        </el-form-item>
        <el-form-item label="加签密钥">
          <el-input v-model="cfg.dingtalk_secret" clearable show-password placeholder="SEC 开头，不用加签就留空" />
        </el-form-item>

        <el-divider content-position="left">其他</el-divider>
        <el-form-item label="平台访问地址">
          <el-input v-model="cfg.public_url" clearable placeholder="例如 http://192.168.1.10:8090" />
          <div class="hint">手机能访问到的平台地址。填写后消息里会带"查看详情"链接，钉钉消息也能显示截图（钉钉只支持图片链接）</div>
        </el-form-item>
        <el-form-item label="每分钟上限">
          <el-input-number v-model="cfg.rate_per_min" :min="1" :max="60" />
          <span class="hint inline">超出的告警不推送，只在平台里记录</span>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存</el-button>
          <el-button :loading="testing" @click="test">保存并发送测试消息</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card shadow="never" style="max-width: 820px; margin-top: 14px">
      <template #header>推送状态</template>
      <el-descriptions :column="1" border size="small">
        <el-descriptions-item v-for="(name, ch) in CHANNELS" :key="ch" :label="name">
          <template v-if="status.last?.[ch]">
            <el-tag :type="status.last[ch].ok ? 'success' : 'danger'" size="small">
              {{ status.last[ch].ok ? '成功' : '失败' }}
            </el-tag>
            <span style="margin-left: 8px">{{ fmtTime(status.last[ch].time) }}</span>
            <span v-if="!status.last[ch].ok" class="err">{{ status.last[ch].msg }}</span>
          </template>
          <span v-else class="muted">还没有发送过</span>
        </el-descriptions-item>
        <el-descriptions-item label="因限流未推送">{{ status.dropped ?? 0 }} 条</el-descriptions-item>
      </el-descriptions>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ALARM_TYPES, api, fmtTime } from '../api'

const CHANNELS = { wecom: '企业微信', dingtalk: '钉钉' }
const cfg = ref({ enabled: false, types: [], wecom_webhook: '', wecom_image: true, dingtalk_webhook: '',
  dingtalk_secret: '', public_url: '', rate_per_min: 20 })
const status = ref({})
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)

function apply(d) {
  const { status: s, ...rest } = d
  cfg.value = rest
  status.value = s || {}
}

async function load() {
  try { apply(await api.getNotify()) } catch (e) { ElMessage.error(e.message) } finally { loading.value = false }
}

async function save(quiet) {
  saving.value = true
  try {
    apply(await api.putNotify(cfg.value))
    if (quiet !== true) ElMessage.success('已保存')
    return true
  } catch (e) {
    ElMessage.error(e.message)
    return false
  } finally {
    saving.value = false
  }
}

async function test() {
  if (!cfg.value.wecom_webhook && !cfg.value.dingtalk_webhook) return ElMessage.warning('请先填写 Webhook 地址')
  testing.value = true
  try {
    if (!(await save(true))) return
    const r = await api.testNotify()
    status.value = r.status
    const failed = Object.entries(r.result).filter(([, ok]) => !ok).map(([ch]) => CHANNELS[ch])
    failed.length ? ElMessage.error(`发送失败：${failed.join('、')}，原因见下方推送状态`) : ElMessage.success('测试消息已发送')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    testing.value = false
  }
}

load()
</script>

<style scoped>
.hint { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; width: 100%; }
.hint.inline { width: auto; margin-left: 10px; }
.err { color: var(--el-color-danger); margin-left: 10px; font-size: 12px; }
</style>
