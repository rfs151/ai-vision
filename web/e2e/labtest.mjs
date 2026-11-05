// Headless Chrome test for the 视频实验室 page: switches, side-by-side playback, start-up times.
// usage: node e2e/labtest.mjs <chrome-bin> <outdir>
// Needs a running system (API :8090, ZLM, a live stream with AI). Restores the device's switches.
import puppeteer from 'puppeteer-core'

const [bin, out] = process.argv.slice(2)
const BASE = process.env.BASE || 'http://127.0.0.1:8090/'
const ROUNDS = Number(process.env.ROUNDS || 5)
const sleep = ms => new Promise(r => setTimeout(r, ms))

const browser = await puppeteer.launch({
  executablePath: bin,
  headless: 'new',
  protocolTimeout: 30000,
  args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required', '--window-size=1600,1000', '--no-proxy-server'],
  defaultViewport: { width: 1600, height: 1000 },
})
const page = await browser.newPage()
const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })
page.on('pageerror', e => errors.push('pageerror: ' + String(e).slice(0, 200)))

const api = (method, path, body) => page.evaluate(async (method, path, body) => {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body) })
  return r.json()
}, method, path, body)

await page.goto(BASE + '#/lab', { waitUntil: 'networkidle2' })
await page.waitForSelector('.feature .el-switch')
const dev = (await api('GET', '/api/devices')).find(d => d.online && d.ai_enabled)
await api('PUT', `/api/devices/${dev.id}/video`, { dyn_gop: false, roi: false, compare: false })
await sleep(1500)
console.log('device', dev.stream)

// Switches by clicking, like a user: 动态关键帧, ROI 编码, 对比模式
const SW = { dyn_gop: 0, roi: 1, compare: 2 }
const click = async k => (await page.$$('.feature .el-switch'))[SW[k]].click()
// Two switches flipped back to back: the second save must not undo the first
await click('roi'); await click('dyn_gop')
await sleep(2500)
const fast = (await api('GET', `/api/devices/${dev.id}`)).video
await click('roi'); await click('dyn_gop')
await sleep(2500)
const fast2 = (await api('GET', `/api/devices/${dev.id}`)).video
console.log('rapid toggles ->', fast, fast2, fast.roi && fast.dyn_gop && !fast2.roi && !fast2.dyn_gop ? 'OK' : 'FAIL')
if (process.env.QUICK) {
  await api('PUT', `/api/devices/${dev.id}/video`, dev.video)
  await browser.close()
  process.exit(0)
}
await click('compare')
await sleep(1500)
console.log('compare on ->', (await api('GET', `/api/devices/${dev.id}`)).video)
await sleep(30000)                       // baseline period: both sides identical
await click('roi')
await sleep(1500)
console.log('roi on ->', (await api('GET', `/api/devices/${dev.id}`)).video)
await sleep(45000)
console.log('roi phase:', await page.evaluate(() => document.querySelector('.verdict')?.innerText.replace(/\n/g, ' | ')))
await page.screenshot({ path: `${out}/lab_roi.png` })
await click('roi')
await click('dyn_gop')
await sleep(1500)
console.log('dyn_gop on, roi off ->', (await api('GET', `/api/devices/${dev.id}`)).video)
await sleep(60000)                       // feature period

const state = () => page.evaluate(() => ({
  videos: [...document.querySelectorAll('.pbox video')].map(v => ({ w: v.videoWidth, t: +v.currentTime.toFixed(1),
    frames: v.getVideoPlaybackQuality?.().totalVideoFrames })),
  labels: [...document.querySelectorAll('.plabel')].map(e => e.textContent.trim()),
  metrics: [...document.querySelectorAll('.metrics')].map(e => e.innerText.replace(/\s+/g, ' ')),
  verdict: document.querySelector('.verdict')?.innerText.replace(/\n/g, ' | '),
}))
console.log(JSON.stringify(await state(), null, 1))
await page.screenshot({ path: `${out}/lab.png` })

// Start-up time: reconnect both players several times
const ups = { main: [], ref: [] }
for (let i = 0; i < ROUNDS; i++) {
  await page.click('.toolbar .el-button')
  await sleep(6000)
  const ms = await page.evaluate(() => [...document.querySelectorAll('.metrics')].map(e =>
    Number((e.innerText.match(/起播耗时\s*(\d+)/) || [])[1])))
  ups.main.push(ms[0]); ups.ref.push(ms[1])
  await sleep(1000 + Math.random() * 2000)   // land at different points of the keyframe cycle
}
const avg = a => Math.round(a.filter(Number.isFinite).reduce((x, y) => x + y, 0) / a.filter(Number.isFinite).length)
console.log('startup ms main', ups.main, 'avg', avg(ups.main), '| ref', ups.ref, 'avg', avg(ups.ref))

// Leaving the page turns compare off; then switch dyn_gop off too
await page.goto(BASE + '#/wall', { waitUntil: 'networkidle2' })
await sleep(1500)
console.log('after leaving ->', (await api('GET', `/api/devices/${dev.id}`)).video)
await api('PUT', `/api/devices/${dev.id}/video`, { ...dev.video, compare: false })
console.log('errors:', errors.length ? errors : 'none')
await browser.close()
