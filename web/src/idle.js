// 省流量：标签页切到后台超过 HIDDEN_MS 就断开所有视频流，切回来自动恢复。
// 暂停 = 播放器断开连接（WebRTC 关闭、HTTP 流取消），不是 video.pause()，下行流量直接归零。
// （以前还有「页面无操作 N 分钟就暂停」，已去掉：页面开着就一直播放。）
import { reactive } from 'vue'

const HIDDEN_MS = 30 * 1000

export const videoGate = reactive({
  paused: false,
  reason: '',          // 'hidden'
})

let hiddenTimer = null

function pause(reason) {
  if (videoGate.paused) return
  console.info('[idle] pause video:', reason)
  videoGate.paused = true
  videoGate.reason = reason
}

export function resumeVideo() {
  if (!videoGate.paused) return
  console.info('[idle] resume video')
  videoGate.paused = false
  videoGate.reason = ''
}

let started = false
export function startIdleWatch() {
  if (started) return
  started = true
  document.addEventListener('visibilitychange', () => {
    clearTimeout(hiddenTimer)
    if (document.visibilityState === 'hidden') hiddenTimer = setTimeout(() => pause('hidden'), HIDDEN_MS)
    else resumeVideo()
  })
}
