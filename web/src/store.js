// Shared live state fed by the /ws WebSocket (reconnects automatically).
import { reactive } from 'vue'
import { api } from './api'

export const store = reactive({
  connected: false,
  devices: [],
  workers: {},          // stream -> worker status (state, stats...)
  recentAlarms: [],     // newest first, capped
  lastAlarm: null,      // {alarm, at}: views watch this for popups / flashing
})

const listeners = new Set()
export const onEvent = fn => { listeners.add(fn); return () => listeners.delete(fn) }

export async function loadDevices() {
  store.devices = await api.devices()
  for (const d of store.devices) if (d.worker) store.workers[d.stream] = d.worker
  return store.devices
}

export async function loadRecentAlarms() {
  store.recentAlarms = (await api.alarms({ size: 30 })).items
}

// Live track boxes for browser-side overlays. Deliberately NOT reactive: 5 messages/s per stream
// would make Vue re-render for nothing; TrackOverlay reads them in its animation loop.
// stream -> [{recv: performance.now(), msg}], newest last, last ~5 s
export const trackBuffers = new Map()
const subCount = new Map()       // stream -> number of overlays showing it

function sendSubs() {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ sub: [...subCount.keys()] }))
}

export function subscribeTracks(stream) {
  subCount.set(stream, (subCount.get(stream) || 0) + 1)
  sendSubs()
  return () => {
    const n = (subCount.get(stream) || 1) - 1
    if (n > 0) subCount.set(stream, n)
    else { subCount.delete(stream); trackBuffers.delete(stream) }
    sendSubs()
  }
}

let ws, retry = 1000
export function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws`)
  ws.onopen = () => { store.connected = true; retry = 1000; sendSubs() }
  ws.onclose = () => {
    store.connected = false
    setTimeout(connectWS, retry)
    retry = Math.min(retry * 2, 15000)
  }
  ws.onmessage = ev => {
    const { type, data } = JSON.parse(ev.data)
    if (type === 'tracks') {
      const buf = trackBuffers.get(data.s) || []
      const now = performance.now()
      buf.push({ recv: now, msg: data })
      while (buf.length && now - buf[0].recv > 5000) buf.shift()
      trackBuffers.set(data.s, buf)
      return
    }
    if (type === 'alarm') {
      store.recentAlarms.unshift(data)
      store.recentAlarms.splice(50)
      store.lastAlarm = { alarm: data, at: Date.now() }
    } else if (type === 'alarm_clip') {
      for (const a of store.recentAlarms) if (data.ids.includes(a.id)) a.clip_url = data.clip_url
    } else if (type === 'worker' && data) {
      store.workers[data.stream] = data
    } else if (type === 'stream' || type === 'rules' || type === 'device') {
      loadDevices().catch(() => {})
    }
    listeners.forEach(fn => fn(type, data))
  }
}
