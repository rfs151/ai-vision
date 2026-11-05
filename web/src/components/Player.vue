<!-- Live player for ZLMediaKit streams.
     mode=webrtc: ZLMRTCClient.js (served by ZLM itself), lowest latency
     mode=fmp4:   HTTP-fMP4 in a plain <video>, with a latency chaser
     Both retry automatically, so a stream that comes online later just starts playing. -->
<template>
  <div class="player">
    <video ref="video" muted autoplay playsinline :style="{ objectFit: fit }" @loadedmetadata="onMeta" />
    <TrackOverlay v-if="overlay && !videoGate.paused" :stream="overlay.stream" :rules="overlay.rules" :video="video" :delay="delay" />
    <div v-if="videoGate.paused" class="status paused" @click="resumeVideo">
      <div>⏸ {{ videoGate.reason === 'hidden' ? '页面在后台，已暂停播放' : '长时间无操作，已暂停播放' }}</div>
      <div class="hint">为节省流量 · 点击继续</div>
    </div>
    <div v-else-if="status !== 'playing'" class="status">{{ statusText }}</div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import TrackOverlay from './TrackOverlay.vue'
import { resumeVideo, videoGate } from '../idle'

const props = defineProps({
  urls: { type: Object, required: true },    // { webrtc, fmp4, ... } from /api/devices play.*
  mode: { type: String, default: 'webrtc' },
  fit: { type: String, default: 'contain' },
  overlay: { type: Object, default: null },  // { stream, rules }: draw live track boxes over the video
  // Called (not awaited for long) before each connect, e.g. to ask the worker for a keyframe now
  beforeStart: { type: Function, default: null },
})
// firstframe: ms from connect to the first picture on screen (start-up time)
const emit = defineEmits(['size', 'firstframe'])
const video = ref(null)
const status = ref('connecting')
const statusText = computed(() => ({ connecting: '连接中…', retry: '无信号，重试中…' }[status.value] || ''))

let rtc = null, timer = null, chaser = null, watchdog = null, delayTimer = null, alive = true, gen = 0
// How far behind live the picture is (s), for aligning overlay boxes with the video
const delay = ref(0)
const STALL_MS = 6000

function loadRTCClient() {
  if (window.ZLMRTCClient) return Promise.resolve()
  if (!window.__zlmLoading) {
    window.__zlmLoading = new Promise((resolve, reject) => {
      const s = document.createElement('script')
      s.src = new URL('/ZLMRTCClient.js', props.urls.webrtc).href   // from ZLM's own web root
      s.onload = resolve
      s.onerror = reject
      document.head.appendChild(s)
    })
  }
  return window.__zlmLoading
}

function stop() {
  clearTimeout(timer)
  clearInterval(chaser)
  clearInterval(watchdog)
  clearInterval(delayTimer)
  if (rtc) { try { rtc.close() } catch { /* ignore */ } rtc = null }
  const v = video.value
  if (v) { v.pause(); v.removeAttribute('src'); v.srcObject = null; v.load() }
}

function retry(reason) {
  if (!alive) return
  console.info('[player] retry in 3s:', typeof reason === 'string' ? reason : reason?.type || reason, props.urls.fmp4)
  status.value = 'retry'
  stop()
  timer = setTimeout(start, 3000)
}

async function start() {
  stop()
  const my = ++gen                        // a newer start() (retry, props change) supersedes this one
  if (videoGate.paused) return            // 省流量暂停中：不连接，恢复时由下面的 watch 重新 start
  status.value = 'connecting'
  const v = video.value
  const t0 = performance.now()
  let first = true
  v.onplaying = () => {
    status.value = 'playing'
    if (first) { first = false; emit('firstframe', Math.round(performance.now() - t0)) }
  }
  if (props.beforeStart) {
    // Short cap: a slow API must not delay the picture
    await Promise.race([Promise.resolve().then(props.beforeStart).catch(() => {}), new Promise(r => setTimeout(r, 300))])
    if (!alive || videoGate.paused || my !== gen) return
  }
  // Freeze watchdog (both modes): visible + supposed to be playing + position not moving -> reconnect.
  // Catches silent freezes: publisher restarted without a WebRTC state change, stalled HTTP stream...
  let lastCt = -1, lastMove = Date.now()
  watchdog = setInterval(() => {
    if (document.visibilityState !== 'visible' || status.value !== 'playing') { lastMove = Date.now(); return }
    if (v.currentTime !== lastCt) { lastCt = v.currentTime; lastMove = Date.now(); return }
    if (Date.now() - lastMove > STALL_MS) retry('stalled ' + STALL_MS / 1000 + 's')
  }, 1000)
  if (props.mode === 'webrtc') {
    try { await loadRTCClient() } catch { return retry() }
    if (!alive || videoGate.paused) return  // 等脚本加载期间被暂停/卸载
    const Z = window.ZLMRTCClient
    rtc = new Z.Endpoint({
      element: v, debug: false, zlmsdpUrl: props.urls.webrtc, simulcast: false,
      useCamera: false, audioEnable: true, videoEnable: true, recvOnly: true, usedatachannel: false,
    })
    rtc.on(Z.Events.WEBRTC_OFFER_ANWSER_EXCHANGE_FAILED, retry)   // stream not found etc.
    rtc.on(Z.Events.WEBRTC_ICE_CANDIDATE_ERROR, retry)
    rtc.on(Z.Events.WEBRTC_ON_CONNECTION_STATE_CHANGE, s => {
      if (s === 'failed' || s === 'disconnected' || s === 'closed') retry()
    })
    // WebRTC delay ~ the receiver's jitter buffer (average of the last 2 s)
    let last = null
    delayTimer = setInterval(async () => {
      if (!props.overlay || !rtc?.pc) return
      try {
        const st = await rtc.pc.getStats()
        st.forEach(r => {
          if (r.type !== 'inbound-rtp' || r.kind !== 'video') return
          if (last && r.jitterBufferEmittedCount > last.n) {
            delay.value = (r.jitterBufferDelay - last.d) / (r.jitterBufferEmittedCount - last.n)
          }
          last = { d: r.jitterBufferDelay, n: r.jitterBufferEmittedCount }
        })
      } catch { /* closed */ }
    }, 2000)
  } else {
    v.onerror = retry
    v.onended = retry
    v.src = props.urls.fmp4
    v.play().catch(() => {})
    // HTTP streaming drifts behind live. Only act once playback has really started:
    // before that currentTime is 0 while buffered starts at the stream's timestamp (e.g. 438 s),
    // and seeking every second then stalls loading for good (the bug that froze fMP4 playback).
    // Chrome itself keeps ~2.5 s buffered, so "lag" below that is normal.
    // Small extra lag: play 10% faster. Large lag: jump, at most once per 5 s.
    let lastSeek = 0
    chaser = setInterval(() => {
      const b = v.buffered
      if (!b.length || v.paused || v.readyState < 3 || v.currentTime === 0) return
      const lag = b.end(b.length - 1) - v.currentTime
      if (lag > 5 && Date.now() - lastSeek > 5000) {
        lastSeek = Date.now()
        v.currentTime = b.end(b.length - 1) - 1
      } else {
        v.playbackRate = lag > 3.5 ? 1.1 : 1.0
      }
    }, 1000)
  }
}

function onMeta() {
  const v = video.value
  if (v.videoWidth) emit('size', { w: v.videoWidth, h: v.videoHeight })
}

// Multi-source watch compares each value. (A getter returning a new array changes on every
// device-list refresh and restarted the player even though the URLs were the same.)
watch([() => props.mode, () => props.urls.webrtc, () => props.urls.fmp4], (n, o) => {
  console.info('[player] restart: props changed', JSON.stringify(o), '->', JSON.stringify(n))
  start()
})

// 省流量：暂停时断开连接（流量归零），恢复时重连
watch(() => videoGate.paused, p => {
  if (!alive) return
  if (p) { stop(); status.value = 'connecting' } else start()
})

// Background tabs: Chrome suspends video-only media (our ai/ streams have no audio) and
// throttles timers, so an HTTP live stream comes back far behind live or frozen.
// When the tab is visible again, reconnect to jump straight to live. WebRTC keeps running.
function onVisibility() {
  if (document.visibilityState === 'visible' && props.mode === 'fmp4' && alive && !videoGate.paused) {
    console.info('[player] restart: tab visible again, jump to live')
    start()
  }
}

onMounted(() => {
  document.addEventListener('visibilitychange', onVisibility)
  start()
})
onBeforeUnmount(() => {
  alive = false
  document.removeEventListener('visibilitychange', onVisibility)
  stop()
})
</script>

<style scoped>
.player { position: relative; width: 100%; height: 100%; background: #000; overflow: hidden; }
video { width: 100%; height: 100%; display: block; }
.status { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #888; font-size: 13px; }
.status.paused { flex-direction: column; gap: 4px; color: #ccc; background: rgb(0 0 0 / .75); cursor: pointer; z-index: 2; }
.status.paused .hint { color: #888; font-size: 12px; }
</style>
