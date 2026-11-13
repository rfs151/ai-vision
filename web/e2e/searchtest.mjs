// Headless Chrome check of AI 查询: the bar on 监控大屏 leads to the search page, which shows the
// parsed conditions and result cards with crops.  usage: node e2e/searchtest.mjs <chrome-bin> <outdir>
import puppeteer from 'puppeteer-core'

const [bin, out] = process.argv.slice(2)
const BASE = process.env.BASE || 'http://127.0.0.1:8090/'
const Q = process.env.Q || '超市里穿绿色外套的顾客'
const browser = await puppeteer.launch({
  executablePath: bin, headless: 'new',
  args: ['--no-sandbox', '--window-size=1500,1000', '--no-proxy-server'],
  defaultViewport: { width: 1500, height: 1000 },
})
const page = await browser.newPage()
const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)) })
await page.goto(BASE + '#/wall', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.ai-bar input')
if (out) await page.screenshot({ path: `${out}/ai-wall.png`, clip: { x: 0, y: 0, width: 1500, height: 260 } })
await page.type('.ai-bar input', Q)
await page.keyboard.press('Enter')
await page.waitForFunction(() => location.hash.startsWith('#/search'))
await page.waitForSelector('.understood', { timeout: 30000 })
await page.waitForFunction(() => document.querySelectorAll('.card img').length > 0 || document.querySelector('.el-empty'),
  { timeout: 30000 })
await new Promise(r => setTimeout(r, 1500))     // images load
const r = await page.evaluate(() => ({
  hash: decodeURIComponent(location.hash),
  understood: document.querySelector('.understood').innerText.replace(/\s+/g, ' '),
  meta: document.querySelector('.meta').innerText,
  cards: document.querySelectorAll('.card').length,
  broken: [...document.querySelectorAll('.card img')].filter(i => !i.complete || !i.naturalWidth).length,
  menuFirst: document.querySelector('.el-menu-item')?.innerText,
}))
console.log(JSON.stringify(r, null, 1))
if (out) await page.screenshot({ path: `${out}/ai-search.png` })
await page.click('.card')
await page.waitForSelector('.el-dialog .frame', { timeout: 5000 }).catch(() => {})
await new Promise(r => setTimeout(r, 800))
if (out) await page.screenshot({ path: `${out}/ai-detail.png` })
if (errors.length) console.log('console errors:', errors)
await browser.close()
process.exit(r.cards > 0 && r.broken === 0 ? 0 : 1)
