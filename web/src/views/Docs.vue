<!-- 开发文档: the planning docs (docs/yolo-acceleration-*.md), built by scripts/build_docs.py into
     public/docs/*.html with their own layout and Mermaid charts, shown in a frame. -->
<template>
  <div class="docs">
    <div class="bar">
      <el-radio-group :model-value="doc.key" size="small" @change="k => router.replace('/docs/' + k)">
        <el-radio-button v-for="d in DOCS" :key="d.key" :value="d.key">{{ d.title }}</el-radio-button>
      </el-radio-group>
      <span class="muted">{{ doc.desc }}</span>
      <a class="open" :href="doc.src" target="_blank" rel="noopener">新窗口打开</a>
    </div>
    <iframe :key="doc.key" :src="doc.src" :title="doc.title" />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const DOCS = [
  { key: 'summary', title: '推理加速速览', src: 'docs/summary.html', desc: '分层结构、已用方案、方案总表、实验数据、自然语言查询告警' },
  { key: 'inference', title: '推理加速与多模态', src: 'docs/inference.html', desc: '完整版：加速方案、实验数据、动作识别、多模态应用、实施顺序' },
]
const route = useRoute()
const router = useRouter()
const doc = computed(() => DOCS.find(d => d.key === route.params.doc) || DOCS[0])
</script>

<style scoped>
.docs { display: flex; flex-direction: column; height: 100%; }
.bar { display: flex; align-items: center; gap: 12px; padding: 8px 16px; border-bottom: 1px solid var(--el-border-color-lighter); flex-wrap: wrap; }
.bar .muted { font-size: 12px; }
.open { margin-left: auto; font-size: 13px; color: var(--el-color-primary); text-decoration: none; }
iframe { flex: 1; width: 100%; border: 0; background: #fff; }
</style>
