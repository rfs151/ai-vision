<!-- Live player for ZLMediaKit streams.
     mode=webrtc: ZLMRTCClient.js (served by ZLM itself), lowest latency
     mode=fmp4:   HTTP-fMP4 in a plain <video>, with a latency chaser
     Both retry automatically, so a stream that comes online later just starts playing. -->
<template>
  <div class="player">
    <video ref="video" muted autoplay playsinline :style="{ objectFit: fit }" @loadedmetadata="onMeta" />
    <div v-if="status !== 'playing'" class="status">{{ statusText }}</div>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = defineProps({
  urls: { type: Object, required: true },    // { webrtc, fmp4, ... } from /api/devices play.*
  mode: { type: String, default: 'webrtc' },
  fit: { type: String, default: 'contain' },
})
const emit = defineEmits(['size'])
const video = ref(null)
const status = ref('connecting')
const statusText = computed(() => ({ connecting: '连接中…', retry: '无信号，重试中…' }[status.value] || ''))

let rtc = null, timer = null, chaser = null, alive = true

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
  if (rtc) { try { rtc.close() } catch { /* ignore */ } rtc = null }
  const v = video.value
  if (v) { v.pause(); v.removeAttribute('src'); v.srcObject = null; v.load() }
}

function retry() {
  if (!alive) return
  status.value = 'retry'
  stop()
  timer = setTimeout(start, 3000)
}

async function start() {
  stop()
  status.value = 'connecting'
  const v = video.value
  v.onplaying = () => { status.value = 'playing' }
  if (props.mode === 'webrtc') {
    try { await loadRTCClient() } catch { return retry() }
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
  } else {
    v.onerror = retry
    v.onended = retry
    v.src = props.urls.fmp4
    v.play().catch(() => {})
    // HTTP streaming drifts behind live. Only act once playback has really started:
    // before that currentTime is 0 while buffered starts at the stream's timestamp (e.g. 438 s),
    // and seeking every second then stalls loading for good (the bug that froze fMP4 playback).
    // Small lag: play 10% faster. Large lag: jump, at most once per 5 s.
    let lastSeek = 0
    chaser = setInterval(() => {
      const b = v.buffered
      if (!b.length || v.paused || v.readyState < 3 || v.currentTime === 0) return
      const lag = b.end(b.length - 1) - v.currentTime
      if (lag > 3 && Date.now() - lastSeek > 5000) {
        lastSeek = Date.now()
        v.currentTime = b.end(b.length - 1) - 0.5
      } else {
        v.playbackRate = lag > 1.2 ? 1.1 : 1.0
      }
    }, 1000)
  }
}

function onMeta() {
  const v = video.value
  if (v.videoWidth) emit('size', { w: v.videoWidth, h: v.videoHeight })
}

watch(() => [props.mode, props.urls.webrtc, props.urls.fmp4], start)
onMounted(start)
onBeforeUnmount(() => { alive = false; stop() })
</script>

<style scoped>
.player { position: relative; width: 100%; height: 100%; background: #000; overflow: hidden; }
video { width: 100%; height: 100%; display: block; }
.status { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #888; font-size: 13px; }
</style>
