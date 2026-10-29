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

let ws, retry = 1000
export function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws`)
  ws.onopen = () => { store.connected = true; retry = 1000 }
  ws.onclose = () => {
    store.connected = false
    setTimeout(connectWS, retry)
    retry = Math.min(retry * 2, 15000)
  }
  ws.onmessage = ev => {
    const { type, data } = JSON.parse(ev.data)
    if (type === 'alarm') {
      store.recentAlarms.unshift(data)
      store.recentAlarms.splice(50)
      store.lastAlarm = { alarm: data, at: Date.now() }
    } else if (type === 'worker' && data) {
      store.workers[data.stream] = data
    } else if (type === 'stream' || type === 'rules' || type === 'device') {
      loadDevices().catch(() => {})
    }
    listeners.forEach(fn => fn(type, data))
  }
}
