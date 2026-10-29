// Headless Chrome playback test for the ai-vision wall page.
// usage: node e2e/playtest.mjs <chrome-bin> <outdir> [seconds]
// Needs a running system (API :8090, ZLM, a live stream). CASES=ai:webrtc,source:fmp4 to pick cases.
// Prints decoded frames per 5 s per case: a steady fps*5 means smooth playback, 0 means frozen.
import puppeteer from 'puppeteer-core'

const [bin, out, secsArg] = process.argv.slice(2)
const SECS = Number(secsArg || 40)
const BASE = 'http://127.0.0.1:8090/'

const browser = await puppeteer.launch({
  executablePath: bin,
  headless: 'new',
  args: ['--no-sandbox', '--autoplay-policy=no-user-gesture-required', '--use-fake-ui-for-media-stream',
         '--window-size=1600,900', '--no-proxy-server'],
  defaultViewport: { width: 1600, height: 900 },
})

async function runCase(source, mode) {
  const page = await browser.newPage()
  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })
  page.on('pageerror', e => errors.push('pageerror: ' + String(e).slice(0, 200)))
  await page.goto(BASE + '#/wall', { waitUntil: 'networkidle2' })
  await page.evaluate((source, mode) => {
    localStorage.setItem('wall.layout', '1')
    localStorage.setItem('wall.source', JSON.stringify(source))
    localStorage.setItem('wall.mode', JSON.stringify(mode))
    localStorage.setItem('wall.picked', '{}')
  }, source, mode)
  await page.reload({ waitUntil: 'networkidle2' })
  const rows = []
  for (let t = 0; t <= SECS; t += 5) {
    const s = await page.evaluate(() => {
      const v = document.querySelector('video')
      if (!v) return null
      const q = v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : {}
      const st = document.querySelector('.player .status')
      return { ct: +v.currentTime.toFixed(1), frames: q.totalVideoFrames, dropped: q.droppedVideoFrames,
               rs: v.readyState, paused: v.paused, w: v.videoWidth, status: st ? st.textContent : '' }
    })
    rows.push({ t, ...s })
    if (t < SECS) await new Promise(r => setTimeout(r, 5000))
  }
  await page.screenshot({ path: `${out}/wall_${source}_${mode}.png` })
  await page.close()
  // frames decoded in each 5 s window
  const deltas = rows.slice(1).map((r, i) => (r.frames ?? 0) - (rows[i].frames ?? 0))
  console.log(`\n== ${source} / ${mode}: frames per 5s = [${deltas.join(', ')}]`)
  console.log(rows.map(r => `t=${r.t}s ct=${r.ct} frames=${r.frames} rs=${r.rs} paused=${r.paused} ${r.w}px ${r.status}`).join('\n'))
  if (errors.length) console.log('console errors:\n  ' + [...new Set(errors)].slice(0, 8).join('\n  '))
}

const cases = (process.env.CASES || 'ai:webrtc,source:webrtc,ai:fmp4,source:fmp4').split(',')
for (const c of cases) {
  const [s, m] = c.split(':')
  await runCase(s, m)
}

// Screenshots of the other pages
for (const p of ['devices', 'rules/1', 'alarms']) {
  const page = await browser.newPage()
  await page.goto(BASE + '#/' + p, { waitUntil: 'networkidle2' })
  await new Promise(r => setTimeout(r, 4000))
  await page.screenshot({ path: `${out}/page_${p.replace('/', '_')}.png` })
  await page.close()
}
await browser.close()
