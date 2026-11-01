// 省流量：没人看的时候断开所有视频流。
//   - 页面上 IDLE_MS 内没有点击/按键/滚动/移动鼠标 → 暂停，点一下或按任意键恢复
//   - 标签页切到后台超过 HIDDEN_MS → 暂停，切回来自动恢复
// 暂停 = 播放器断开连接（WebRTC 关闭、HTTP 流取消），不是 video.pause()，下行流量直接归零。
// 时长可用 URL 参数覆盖，方便演示：#/wall?idle=1 表示 1 分钟。
import { reactive } from 'vue'

const DEFAULT_IDLE_MIN = Number(import.meta.env.VITE_VIDEO_IDLE_MIN || 2)
const HIDDEN_MS = 30 * 1000

export const videoGate = reactive({
  paused: false,
  reason: '',          // 'idle' | 'hidden'
})

let lastActive = Date.now()
let hiddenTimer = null

function idleMs() {
  const m = Number(new URLSearchParams(location.hash.split('?')[1] || '').get('idle'))
  return (m > 0 ? m : DEFAULT_IDLE_MIN) * 60 * 1000
}

function pause(reason) {
  if (videoGate.paused) return
  console.info('[idle] pause video:', reason)
  videoGate.paused = true
  videoGate.reason = reason
}

export function resumeVideo() {
  lastActive = Date.now()
  if (!videoGate.paused) return
  console.info('[idle] resume video')
  videoGate.paused = false
  videoGate.reason = ''
}

let started = false
export function startIdleWatch() {
  if (started) return
  started = true
  // 移动鼠标只算“有人在”，不触发恢复；点击 / 按键才恢复（避免碰一下鼠标就重新拉流）
  let lastMove = 0
  const seen = () => { lastActive = Date.now() }
  window.addEventListener('pointermove', () => {
    const now = Date.now()
    if (now - lastMove > 1000) { lastMove = now; if (!videoGate.paused) seen() }
  }, { passive: true })
  window.addEventListener('wheel', () => { if (!videoGate.paused) seen() }, { passive: true })
  for (const ev of ['pointerdown', 'keydown', 'touchstart']) {
    window.addEventListener(ev, resumeVideo, { passive: true, capture: true })
  }

  document.addEventListener('visibilitychange', () => {
    clearTimeout(hiddenTimer)
    if (document.visibilityState === 'hidden') {
      hiddenTimer = setTimeout(() => pause('hidden'), HIDDEN_MS)
    } else if (videoGate.reason === 'hidden') {
      resumeVideo()
    } else {
      lastActive = Date.now()
    }
  })

  setInterval(() => {
    if (document.visibilityState === 'visible' && Date.now() - lastActive > idleMs()) pause('idle')
  }, 5000)
}
