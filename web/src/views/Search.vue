<!-- AI 查询: one sentence -> people / vehicles seen by the cameras (api/search.py).
     DeepSeek (or the rule parser) turns the question into conditions, shown as tags so the
     visitor sees how it was understood; Chinese-CLIP ranks the crops by the appearance. -->
<template>
  <div class="page search">
    <section class="hero">
      <div class="title"><el-icon><MagicStick /></el-icon>AI 查询</div>
      <div class="sub">用一句话在所有摄像头里找人、找车：时间、地点、类别交给大模型解析，衣着外观交给 Chinese-CLIP 比对截图</div>
      <form class="ask" @submit.prevent="run(text)">
        <el-input v-model="text" size="large" clearable placeholder="例如：超市里穿绿色外套的顾客" :prefix-icon="Search" />
        <el-button type="primary" size="large" native-type="submit" :loading="loading">查询</el-button>
      </form>
      <div class="examples">
        <span class="muted">试试：</span>
        <el-tag v-for="e in status.examples || []" :key="e" effect="plain" round class="ex" @click="run(e)">{{ e }}</el-tag>
      </div>
      <div class="muted stat">
        已记录 {{ status.sightings ?? '-' }} 个目标（重复出现合并，共 {{ status.seen ?? '-' }} 次，保留 {{ status.keep_days }} 天）
        · 解析：{{ status.llm ? `DeepSeek（${status.llm_model}）` : '规则解析（未配置 DEEPSEEK_API_KEY）' }}
        · 外观匹配：{{ status.clip ? 'Chinese-CLIP' : '未安装（scripts/export_clip.py）' }}
      </div>
    </section>

    <section v-if="res" class="answer">
      <div class="understood">
        <span class="label">理解为</span>
        <el-tag v-if="c.since_minutes" type="info">时间：{{ since(c.since_minutes) }}</el-tag>
        <el-tag v-for="d in c.devices || []" :key="d" type="success">摄像头：{{ camName(d) }}</el-tag>
        <el-tag v-for="k in c.classes || []" :key="k" type="warning">类别：{{ CLS[k] || k }}</el-tag>
        <el-tag v-for="t in c.tags || []" :key="t" type="danger">{{ TAG[t] || t }}</el-tag>
        <el-tag v-if="c.appearance" effect="dark">外观：{{ c.appearance }}</el-tag>
        <span v-if="!Object.keys(c).length" class="muted">没有识别出条件，按时间列出最近的目标</span>
      </div>
      <div class="muted meta">
        {{ res.source === 'deepseek' ? 'DeepSeek 解析' : '规则解析' }} {{ res.parse_ms }} ms
        · 条件筛出 {{ res.candidates }} 个 · 显示 {{ res.results.length }} 个
        <template v-if="c.appearance && res.results.length">（按外观相似度排序）</template>
        · 共 {{ res.total_ms }} ms
        <span v-if="res.source === 'rules' && status.llm" class="warn">（DeepSeek 调用失败，已改用规则：{{ res.llm_error }}）</span>
        <span v-if="res.note" class="warn">{{ res.note }}</span>
      </div>

      <el-empty v-if="!res.results.length" :description="emptyText" />
      <div v-else class="grid">
        <div v-for="(r, i) in res.results" :key="r.id" class="card" @click="open = r">
          <div class="pic">
            <img :src="r.crop" loading="lazy" />
            <span class="rank">{{ i + 1 }}</span>
            <span v-if="r.count > 1" class="count">×{{ r.count }}</span>
          </div>
          <div v-if="r.similarity != null" class="bar"><i :style="{ width: barWidth(r) }" /></div>
          <div class="info">
            <div class="cam">{{ shortCam(r) }}</div>
            <div class="muted">{{ ago(r.last_seen) }}<template v-if="r.similarity != null"> · 相似度 {{ (r.similarity * 100).toFixed(1) }}</template></div>
            <el-tag v-if="r.tags.includes('riding')" size="small" type="danger">骑行</el-tag>
          </div>
        </div>
      </div>
    </section>

    <el-dialog :model-value="!!open" width="720px" :title="open ? `${shortCam(open)} · ${CLS[open.cls] || open.cls}` : ''" @close="open = null">
      <template v-if="open">
        <img v-if="open.frame" :src="open.frame" class="frame" />
        <div class="detail">
          <img :src="open.crop" class="crop" />
          <div>
            <p>摄像头：{{ open.camera }}（{{ open.stream }}）</p>
            <p>首次出现：{{ fmt(open.first_seen) }}</p>
            <p>最近出现：{{ fmt(open.last_seen) }}</p>
            <p v-if="open.count > 1">出现 {{ open.count }} 次（演示视频循环播放，同一个人按外观合并）</p>
            <p v-if="open.similarity != null">和"{{ c.appearance }}"的相似度：{{ (open.similarity * 100).toFixed(1) }}</p>
            <el-button size="small" @click="$router.push('/wall')">去监控大屏看实时画面</el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { MagicStick, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const CLS = { person: '人', car: '小汽车', truck: '货车', bus: '公交车', motorcycle: '摩托车 / 电动车' }
const TAG = { riding: '骑行' }
const route = useRoute()
const router = useRouter()
const text = ref(route.query.q || '')
const status = ref({})
const res = ref(null)
const loading = ref(false)
const open = ref(null)
const c = computed(() => res.value?.conditions || {})
const emptyText = computed(() => status.value.sightings
  ? '没有符合条件的目标：换个说法，或去掉时间、摄像头条件试试'
  : '还没有记录：Worker 每看到一个人或车，离开画面后入库一条，稍等片刻再查')

async function get(url) {
  const r = await fetch(url)
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText)
  return r.json()
}
async function loadStatus() {
  try { status.value = await get('/api/search/status') } catch { /* page still works */ }
}
async function run(q) {
  q = (q || '').trim()
  if (!q) return
  text.value = q
  if (route.query.q !== q) router.replace({ query: { q } })
  loading.value = true
  try {
    res.value = await get('/api/search?q=' + encodeURIComponent(q))
  } catch (e) {
    ElMessage.error('查询失败：' + e.message)
  } finally {
    loading.value = false
  }
  loadStatus()
}

const camName = s => status.value.cameras?.[s] || s
const shortCam = r => (r.camera || r.stream).split(/[（(]/)[0]
function since(m) {
  if (m % 1440 === 0) return `最近 ${m / 1440} 天`
  if (m % 60 === 0) return `最近 ${m / 60} 小时`
  return `最近 ${m} 分钟`
}
function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  return fmt(iso)
}
const fmt = iso => iso.replace('T', ' ')
function barWidth(r) {
  const sims = res.value.results.map(x => x.similarity)
  const hi = Math.max(...sims), lo = Math.min(...sims)
  return (hi === lo ? 100 : 30 + 70 * (r.similarity - lo) / (hi - lo)) + '%'
}

onMounted(() => {
  loadStatus()
  if (text.value) run(text.value)
})
watch(() => route.query.q, q => { if (q && q !== res.value?.question) run(q) })
</script>

<style scoped>
.search { max-width: 1280px; margin: 0 auto; }
.hero { padding: 28px 24px 18px; border-radius: 12px; margin-bottom: 16px;
  background: radial-gradient(120% 140% at 0% 0%, rgba(64, 158, 255, .22), transparent 60%),
              radial-gradient(120% 140% at 100% 0%, rgba(149, 92, 255, .20), transparent 60%), var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter); }
.title { font-size: 26px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.sub { color: var(--el-text-color-secondary); margin: 6px 0 16px; }
.ask { display: flex; gap: 10px; max-width: 760px; }
.ask :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px var(--el-color-primary) inset; }
.examples { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.ex { cursor: pointer; }
.ex:hover { border-color: var(--el-color-primary); color: var(--el-color-primary); }
.stat { margin-top: 12px; }
.understood { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.label { font-weight: 600; margin-right: 4px; }
.meta { margin: 8px 0 14px; }
.warn { color: var(--el-color-warning); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
.card { background: var(--el-bg-color); border: 1px solid var(--el-border-color-lighter); border-radius: 8px;
  overflow: hidden; cursor: pointer; transition: border-color .15s, transform .15s; }
.card:hover { border-color: var(--el-color-primary); transform: translateY(-2px); }
.pic { position: relative; height: 210px; background: #111; display: flex; align-items: center; justify-content: center; }
.pic img { max-width: 100%; max-height: 100%; object-fit: contain; }
.rank { position: absolute; left: 6px; top: 6px; background: rgba(0, 0, 0, .65); color: #fff; font-size: 12px;
  border-radius: 10px; padding: 1px 7px; }
.count { position: absolute; right: 6px; top: 6px; background: rgba(64, 158, 255, .85); color: #fff; font-size: 12px;
  border-radius: 10px; padding: 1px 7px; }
.bar { height: 3px; background: var(--el-fill-color); }
.bar i { display: block; height: 100%; background: linear-gradient(90deg, #409eff, #955cff); }
.info { padding: 8px; display: flex; flex-direction: column; gap: 3px; }
.cam { font-size: 13px; }
.frame { width: 100%; border-radius: 6px; }
.detail { display: flex; gap: 16px; margin-top: 12px; }
.detail .crop { height: 160px; border-radius: 4px; background: #111; }
.detail p { margin: 0 0 6px; }
@media (max-width: 640px) { .ask { flex-direction: column; } .detail { flex-direction: column; } }
</style>
