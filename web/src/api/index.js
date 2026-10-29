async function request(method, url, body) {
  const opt = { method, headers: {} }
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json'
    opt.body = JSON.stringify(body)
  }
  const r = await fetch(url, opt)
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`
    try {
      const d = await r.json()
      msg = typeof d.detail === 'string' ? d.detail
        : Array.isArray(d.detail) ? d.detail.map(e => `${e.loc.slice(1).join('.')}: ${e.msg}`).join('; ')
        : msg
    } catch { /* not json */ }
    throw new Error(msg)
  }
  return r.status === 204 ? null : r.json()
}

const qs = params => {
  const p = new URLSearchParams()
  Object.entries(params || {}).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v) })
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const api = {
  devices: () => request('GET', '/api/devices'),
  device: id => request('GET', `/api/devices/${id}`),
  createDevice: d => request('POST', '/api/devices', d),
  updateDevice: (id, d) => request('PATCH', `/api/devices/${id}`, d),
  deleteDevice: id => request('DELETE', `/api/devices/${id}`),
  workerAction: (id, action) => request('POST', `/api/devices/${id}/worker/${action}`),
  getRules: id => request('GET', `/api/devices/${id}/rules`),
  putRules: (id, rules) => request('PUT', `/api/devices/${id}/rules`, rules),
  alarms: params => request('GET', `/api/alarms${qs(params)}`),
  alarmStats: hours => request('GET', `/api/alarms/stats${qs({ hours })}`),
  setAlarmStatus: (id, status) => request('PATCH', `/api/alarms/${id}`, { status }),
  workers: () => request('GET', '/api/workers'),
  system: () => request('GET', '/api/system'),
}

export const ALARM_TYPES = {
  intrusion: { label: '区域入侵', tag: 'danger' },
  dwell: { label: '逗留超时', tag: 'warning' },
  crowd: { label: '人数超限', tag: 'warning' },
  line_in: { label: '越线(进)', tag: 'primary' },
  line_out: { label: '越线(出)', tag: 'primary' },
}

export const ALARM_STATUS = {
  new: { label: '未处理', tag: 'danger' },
  ack: { label: '已确认', tag: 'success' },
  false: { label: '误报', tag: 'info' },
}

export const fmtTime = s => (s ? String(s).replace('T', ' ').slice(0, 19) : '')
