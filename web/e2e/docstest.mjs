// Headless Chrome check of the 开发文档 page: both docs load in the frame and every Mermaid chart renders.
// usage: node e2e/docstest.mjs <chrome-bin> <outdir>   (needs the API on :8090 serving web/dist)
import puppeteer from 'puppeteer-core'

const [bin, out] = process.argv.slice(2)
const BASE = process.env.BASE || 'http://127.0.0.1:8090/'
const browser = await puppeteer.launch({
  executablePath: bin, headless: 'new',
  args: ['--no-sandbox', '--window-size=1600,1000', '--no-proxy-server'],
  defaultViewport: { width: 1600, height: 1000 },
})
const page = await browser.newPage()
const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })
let failed = false
for (const [key, charts] of [['summary', 2], ['inference', 6]]) {
  await page.goto(BASE + '#/docs/' + key, { waitUntil: 'networkidle0' })
  const frame = await (await page.waitForSelector('iframe')).contentFrame()
  await frame.waitForFunction(n => document.querySelectorAll('pre.mermaid svg').length >= n, { timeout: 20000 }, charts)
    .catch(() => {})
  const r = await frame.evaluate(() => ({
    title: document.title,
    charts: document.querySelectorAll('pre.mermaid').length,
    svgs: document.querySelectorAll('pre.mermaid svg').length,
    tables: document.querySelectorAll('table').length,
    toc: document.querySelectorAll('.index a').length,
  }))
  const ok = r.charts === charts && r.svgs === charts
  failed ||= !ok
  console.log(ok ? 'OK  ' : 'FAIL', key, JSON.stringify(r))
  await frame.evaluate(() => document.querySelector('pre.mermaid')?.scrollIntoView())
  if (out) await page.screenshot({ path: `${out}/docs-${key}.png` })
}
if (errors.length) console.log('console errors:', errors)
await browser.close()
process.exit(failed ? 1 : 0)
