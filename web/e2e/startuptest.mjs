// Start-up time of the two players on the 视频实验室 page (compare mode), per playback mode.
// usage: node e2e/startuptest.mjs <chrome-bin> [stream]     env: ROUNDS (default 8), MODES (webrtc,fmp4)
// Needs a running system. Uses the device's current switches, only turns compare on (and back).
import puppeteer from 'puppeteer-core'

const [bin, stream] = process.argv.slice(2)
const BASE = process.env.BASE || 'http://127.0.0.1:8090/'
const ROUNDS = Number(process.env.ROUNDS || 8)
const MODES = (process.env.MODES || 'webrtc,fmp4').split(',')
const sleep = ms => new Promise(r => setTimeout(r, ms))

const browser = await puppeteer.launch({
  executablePath: bin, headless: 'new', protocolTimeout: 30000,
  args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required', '--window-size=1600,1000', '--no-proxy-server'],
  defaultViewport: { width: 1600, height: 1000 },
})
const page = await browser.newPage()
const api = (method, path, body) => page.evaluate(async (method, path, body) => {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) })
  return r.json()
}, method, path, body)

await page.goto(BASE + '#/lab', { waitUntil: 'networkidle2' })
const devs = await api('GET', '/api/devices')
const dev = devs.find(d => d.online && d.ai_enabled && (!stream || d.stream === stream))
const orig = { ...dev.video }
if (!orig.compare) await api('PUT', `/api/devices/${dev.id}/video`, { ...orig, compare: true })
// pick the device in the page's select
await page.evaluate(async name => {
  document.querySelector('.toolbar .el-select').click()
  await new Promise(r => setTimeout(r, 300))
  ;[...document.querySelectorAll('.el-select-dropdown__item')].find(e => e.textContent.trim() === name)?.click()
}, dev.name)
await sleep(4000)
console.log('device', dev.stream, (await api('GET', `/api/devices/${dev.id}`)).video)

// Per player: start-up ms (as the page shows it) and, once playing, how far behind live it is
const read = () => page.evaluate(() => [...document.querySelectorAll('.pcell')].map(c => {
  const v = c.querySelector('video')
  const b = v?.buffered
  return {
    ms: Number((c.querySelector('.metrics').innerText.match(/起播耗时\s*(\d+)/) || [])[1]),
    lag: b && b.length ? +(b.end(b.length - 1) - v.currentTime).toFixed(2) : null,
  }
}))
const avg = a => { const x = a.filter(Number.isFinite); return x.length ? Math.round(x.reduce((p, q) => p + q, 0) / x.length) : null }

for (const mode of MODES) {
  await page.evaluate(m => [...document.querySelectorAll('.toolbar .el-radio-button')]
    .find(e => e.textContent.includes(m === 'webrtc' ? 'WebRTC' : 'fMP4'))?.click(), mode)
  await sleep(4000)
  const res = { main: [], ref: [], lagMain: [], lagRef: [] }
  for (let i = 0; i < ROUNDS; i++) {
    await page.click('.toolbar .el-button')
    await sleep(5000)
    const [m, r] = await read()
    res.main.push(m?.ms); res.ref.push(r?.ms); res.lagMain.push(m?.lag); res.lagRef.push(r?.lag)
    await sleep(500 + Math.random() * 2000)        // land at different points of the keyframe cycle
  }
  console.log(`${mode}: start-up ms  main ${JSON.stringify(res.main)} avg ${avg(res.main)}  |  ref ${JSON.stringify(res.ref)} avg ${avg(res.ref)}`)
  if (mode === 'fmp4') console.log(`      buffered ahead s  main ${JSON.stringify(res.lagMain)}  |  ref ${JSON.stringify(res.lagRef)}`)
}
await api('PUT', `/api/devices/${dev.id}/video`, orig)
await browser.close()
