/**
 * 税额输入框的**浏览器**冒烟 —— 真 Chromium 里跑一遍完整用户路径。
 *
 * 为什么需要它:仓里其他验证都是结构性的 —— 构建通过、状态与 payload 的单元
 * 测试通过、AST 检查确认 JSX 嵌套正确。那是「代码看起来对」,不是「用户真的
 * 看得到」。这个功能的核心交付物就是表单里那个输入框;若因任何原因没渲染
 * (条件写错、被父容器挡住、Dialog 分支没走到),前面所有测试照样全绿。
 *
 * 实际抓到过的坑:测试脚本自身三次定位失败(placeholder 是 i18n 值、
 * `button[type=submit]` 属性并不存在因为 `.type` 默认值就是它、
 * 三个 combobox 里 `.first()` 撞到账本选择器)。所以定位尽量用**与语言无关**
 * 的语义锚点。
 *
 * ── 怎么跑 ──────────────────────────────────────────────────────────────
 * 前置:本脚本需要 playwright,而**本仓没有把它列为依赖**(它会带来一个
 * 浏览器二进制,不该塞进应用依赖树)。所以是一次性验证脚本,不是 CI 用例。
 *
 *   # 1. 起服务(前端要指向 dist)
 *   cd frontend && pnpm -C apps/web build
 *   WEB_STATIC_DIR=$PWD/frontend/apps/web/dist .venv/bin/uvicorn server:app --port 8873
 *
 *   # 2. 拿一个 PAT 当 Bearer token(Web 控制台 → 设置 → 开发者 → 新建)
 *   export BC_TOKEN=bcmcp_xxx
 *
 *   # 3. 装 playwright 并跑
 *   npm i -D playwright && npx playwright install chromium
 *   BC_BASE_URL=http://127.0.0.1:8873 BC_TOKEN=$BC_TOKEN node scripts/ui-smoke-tax.mjs
 *
 * 退出码 0 = 全通过;1 = 有断言失败。
 */

// 为什么需要:之前的验证都是结构性的(构建通过、状态与 payload 的单元测试
// 通过、AST 检查确认 JSX 嵌套正确)—— 那是「代码看起来对」,不是「用户真的
// 看得到」。这个功能的核心交付物就是那个输入框。
//
// 定位不依赖 i18n 文案(placeholder/label 随语言变),靠 inputmode="decimal"
// —— 我特意加的移动端数字键盘标记,顺带成了与语言无关的稳定锚点。
import { createRequire } from 'node:module'
import fs from 'node:fs'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright')

const BASE = process.env.BC_BASE_URL || 'http://127.0.0.1:8873'
const TOKEN = (process.env.BC_TOKEN || '').trim()
const TOKEN_KEY = 'beecount.token./api/v1'
const NEW_BTN = /新建|新增|记一笔|Add/
const INCOME_OPT = /收入|Income/

const fails = []
const ok = (l, c, d = '') => {
  console.log(`  [${c ? 'PASS' : 'FAIL'}] ${l}` + (c || !d ? '' : ` — ${d}`))
  if (!c) fails.push(l)
}
const taxIn = (dlg) => dlg.locator('input[inputmode="decimal"]')

const browser = await chromium.launch()
const ctx = await browser.newContext({ locale: 'zh-CN' })
await ctx.addInitScript(([k, t]) => window.localStorage.setItem(k, t), [TOKEN_KEY, TOKEN])
const page = await ctx.newPage()
const jsErrors = []
page.on('pageerror', (e) => jsErrors.push(String(e)))

console.log('\n=== 1. 打开交易页,新建一笔支出 ===')
await page.goto(`${BASE}/app/transactions`, { waitUntil: 'networkidle' })
await page.getByRole('button', { name: NEW_BTN }).first().click()
let dialog = page.getByRole('dialog')
await dialog.waitFor({ state: 'visible', timeout: 15000 })
await dialog.locator('input').first().fill('3280')

// expense 必须选分类(前端硬校验 `categoryRequired`),否则提交会被拦、
// 交易根本没建 —— 那样后面「税额没落库」的断言就会给出误导性的结论。
await dialog.getByRole('button', { name: /分类名称|分类/ }).first().click()
const picker = page.getByRole('dialog').last()
await picker.waitFor({ state: 'visible', timeout: 10000 })
await picker.getByText('餐饮', { exact: false }).first().click()
await page.waitForTimeout(500)

console.log('\n=== 2. 税额输入框必须渲染 ===')
ok('expense 下税额输入框存在', (await taxIn(dialog).count()) === 1)
ok('带 inputMode=decimal(iPhone 数字键盘)',
   (await taxIn(dialog).getAttribute('inputmode')) === 'decimal')
await taxIn(dialog).fill('298')
ok('填了税额后出现「税前」提示', (await dialog.getByText(/税前|Excl\. tax/).count()) > 0)

console.log('\n=== 3. 提交后回服务端确认 ===')
// 注意:不能用 button[type=submit] —— HTMLButtonElement.type 默认就是
// "submit",属性并不真的存在,属性选择器匹配不到。按文案定位,scope 到 dialog
// 以免撞上页面上的同名触发按钮。
await dialog.getByRole('button', { name: /新建交易|保存交易|Save/ }).last().click()
await page.waitForTimeout(2500)
const res = await page.request.get(`${BASE}/api/v1/read/workspace/transactions`, {
  headers: { Authorization: `Bearer ${TOKEN}` },
  params: { limit: 200 },
})
const body = await res.json()
const created = (body.items || []).find((t) => t.amount === 3280)
ok('服务端能读到这笔', !!created)
// 记下这本账本 —— 饼图播种要用它,见 seedPieData 里的说明
const usedLedgerId = created?.ledger_id || ''
ok('税额已落库 = 298', created?.tax_amount === 298, `got ${created?.tax_amount}`)

const a = await page.request.get(`${BASE}/api/v1/read/workspace/analytics`, {
  headers: { Authorization: `Bearer ${TOKEN}` },
})
const ranks = (await a.json()).category_ranks || []
// 断言写成「切片 == 库里所有税额之和」而不是硬编码 298 —— 这个脚本可能
// 重复跑(库里有几笔就加几次),硬编码会给出误导性的失败。
const taxSum = (body.items || []).reduce((s, t) => s + (t.tax_amount || 0), 0)
const hit = ranks.find((r) => r.category_name === '税与保险')
ok('统计切片 == 库里所有税额之和', !!hit && Math.abs(hit.total - taxSum) < 1e-6,
   `slice=${hit?.total} expected=${taxSum}`)
ok('且该切片确实 > 0(不是恰好为 0 的巧合)', !!hit && hit.total > 0)

console.log('\n=== 4. income 下不显示税额输入框 ===')
await page.getByRole('button', { name: NEW_BTN }).first().click()
dialog = page.getByRole('dialog')
await dialog.waitFor({ state: 'visible', timeout: 15000 })
// 对话框里有三个 combobox:账本 / 类型 / 账户。类型是第 2 个 ——
// 用 .first() 会点到账本选择器上(它的当前值是账本名),然后找不到
// 「收入」选项。用当前值来定位更稳。
await dialog.locator('button[role="combobox"]').nth(1).click()
await page.getByRole('option', { name: INCOME_OPT }).click()
await page.waitForTimeout(700)
ok('income 下税额输入框不渲染', (await taxIn(dialog).count()) === 0)

ok('页面无未捕获异常', jsErrors.length === 0, jsErrors.join(' | '))

/**
 * 饼图是否真的显示了税额切片。
 *
 * 为什么要单独验:R5 那个「钉住」的修复,之前只用纯函数测试 + AST 接线断言
 * 验证过 —— 都是「代码看起来对」。而税额扇区的金额天然最小(日本月消费税通常
 * 几千日元,比日常分类小一两个数量级),**按金额排名取前 N 必然把它挤进
 * 「其他」**,界面还不会有任何异常表现。所以必须在渲染层确认。
 *
 * 数据前提:构造若干大额分类 + 一笔小额含税,让税额排在最后一位。
 * 数据由调用方准备好(见文件头的跑法),这里只断言渲染结果。
 */
/** 给饼图检查铺数据:若干大额分类 + 一笔小额含税,让税额排在最后一位。 */
async function seedPieData(page, token, lid) {
  const H = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json',
              'X-Device-ID': 'smoke' }
  // 播种进**同一个账本** —— 必须是第 1 步实际用到的那本。
  //   - 新建账本:概览页默认展示的是活动账本,数据撒在别处饼图看不到
  //   - 取 ledgers[0]:「第一个」未必是活动账本(重跑时列表顺序会变)
  // 两种都让断言落到一个空账本上,给出「图例里找不到税与保险」这种
  // 完全看不出原因���失败。
  const catRes = await page.request.get(`${BASE}/api/v1/read/workspace/categories`, { headers: H })
  const existing = new Set(((await catRes.json()) || []).map((c) => c.name))
  for (const cat of ['住房', '交通', '购物', '日用', '通讯']) {
    if (existing.has(cat)) continue
    await page.request.post(`${BASE}/api/v1/write/ledgers/${lid}/categories`, {
      headers: H, data: { base_change_id: 0, name: cat, kind: 'expense' },
    })
  }
  // 税额必须是最小的一项 —— 这样「按金额取前 N」的实现必然丢它,
  // 只有「钉住」才留得住,这条断言才有意义。
  const rows = [['住房', 95000, null], ['餐饮', 52000, null], ['交通', 14000, null],
                ['购物', 9000, null], ['日用', 5000, null], ['通讯', 3000, null],
                ['餐饮', 3280, 298]]
  for (const [cat, amount, tax] of rows) {
    await page.request.post(`${BASE}/api/v1/write/ledgers/${lid}/transactions`, {
      headers: H,
      data: {
        base_change_id: 0, tx_type: 'expense', amount,
        happened_at: new Date().toISOString(),
        category_name: cat, category_kind: 'expense',
        ...(tax ? { tax_amount: tax } : {}),
      },
    })
  }
}

async function checkPie(page, token) {
  console.log('\n=== 5. 首页饼图必须显示「税与保险」切片 ===')
  await seedPieData(page, token, usedLedgerId)
  if (!usedLedgerId) { ok('拿到活动账本 id', false, '无法播种饼图数据'); return }
  await page.goto(`${BASE}/app/overview`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2500)

  // 整页有**两个**带分类名的列表(饼图图例 + TopCategoriesList 排行榜),
  // 直接 `ul li` 会匹配到两倍数量。锚点用「同时含 conic-gradient 和图例 ul
  // 的那个 flex 容器」—— conic-gradient 是饼图独有的。
  const legend = await page.evaluate(() => {
    const grad = document.querySelector('[style*="conic-gradient"]')
    if (!grad) return null
    let n = grad
    while (n && !(n.tagName === 'DIV' && n.querySelector('ul'))) n = n.parentElement
    const ul = n && n.querySelector('ul')
    return ul
      ? [...ul.querySelectorAll('li')].map((li) => li.innerText.replace(/\s+/g, ' ').trim())
      : null
  })
  console.log('  图例:', (legend || []).join(' | ').slice(0, 220))

  ok('饼图图例已渲染', Array.isArray(legend) && legend.length > 0)
  ok('图例里出现「税与保险」', (legend || []).some((t) => t.includes('税与保险')), '未找到')
  ok('其余分类都在(未被 Top-N 挤掉)',
     ['住房', '交通', '购物'].every((n) => (legend || []).some((t) => t.includes(n))))
  ok('切片数 = 播种的分类数(未触发 Top-N 合并)',
     (legend || []).length === 7, `实际 ${(legend || []).length}`)
  const taxItem = (legend || []).find((t) => t.includes('税与保险')) || ''
  // 898 / 177379 ≈ 0.51% —— 断言「小于 1%」而不是写死 0.5%:
  // 播种数据一旦调整,写死的百分比会给出误导性的失败。
  const pct = Number((taxItem.match(/([\d.]+)%/) || [])[1])
  ok('税额切片是最小的一项(百分比 <1%)', pct > 0 && pct < 1, taxItem)

  // 排行榜用完整数字,顺便确认它显示的是**税前**值。
  // 期望值从服务端算,不写死 —— 本脚本第 3 步也会建一笔含税交易,写死的数字
  // 会在重跑时给出误导性的失败(第一版就踩了这个)。
  // **必须给 limit** —— 该端点默认只返回 20 条。播种会写 7 笔,加上脚本
  // 前面步骤建的几笔,一旦累计超过 20,期望值就会按「前 20 笔」算小,
  // 然后给出「排行榜没找到该行」这种完全误导性的失败(第一版就踩了)。
  const txRes = await page.request.get(`${BASE}/api/v1/read/workspace/transactions`, {
    headers: { Authorization: `Bearer ${token}` },
    params: { limit: 200 },
  })
  const txs = (await txRes.json()).items || []
  const netOf = (cat) => txs
    .filter((t) => t.category_name === cat)
    .reduce((s, t) => s + (t.amount - (t.tax_amount || 0)), 0)
  const grossOf = (cat) => txs
    .filter((t) => t.category_name === cat)
    .reduce((s, t) => s + t.amount, 0)
  const cat = txs.find((t) => t.tax_amount)?.category_name || '餐饮'
  const wantNet = Math.round(netOf(cat))
  const wantGross = Math.round(grossOf(cat))
  const netRow = await page.evaluate((needle) => {
    const lis = [...document.querySelectorAll('li')].map((li) =>
      li.innerText.replace(/\s+/g, ' ').trim()
    )
    return lis.find((t) => t.includes(needle) && t.includes(needle)) || null
  }, String(wantNet).replace(/\B(?=(\d{3})+(?!\d))/g, ','))
  ok(
    `排行榜「${cat}」显示税前 ${wantNet.toLocaleString()}(含税则是 ${wantGross.toLocaleString()})`,
    !!netRow,
    netRow || '未找到该行',
  )
}

await checkPie(page, TOKEN)
await checkShare(page)
/**
 * PWA「分享小票」端到端:相册分享 → SW 缓存 → 自动挂成附件。
 *
 * 为什么必须验:这条链路里有�� **React 异步 effect 的经典陷阱**,单测和 tsc
 * 全都抓不到 —— 见 `checkShare` 里 effect 那段注释。
 */
async function checkShare(page) {
  console.log('\n=== 6. PWA 分享小票 → 自动挂成附件 ===')
  console.log('\n=== 1. 加载应用让 service worker 接管 ===')
  await page.goto(`${BASE}/app/overview`,{waitUntil:'networkidle'})
  // SW 首次注册后需要一次 reload 才进入 controlling 状态
  await page.waitForTimeout(1500)
  await page.reload({waitUntil:'networkidle'})
  await page.waitForTimeout(1500)
  const swState = await page.evaluate(async () => {
    const reg = await navigator.serviceWorker.getRegistration()
    return { has: !!reg, active: !!reg?.active, controls: !!navigator.serviceWorker.controller }
  })
  console.log('  SW:', JSON.stringify(swState))
  ok('service worker 处于 active 且已接管页面', swState.active && swState.controls, JSON.stringify(swState))

  console.log('\n=== 2. 模拟 PWA 分享:POST /share-receive ===')
  const png = fs.readFileSync('./receipt.png').toString('base64')
  const resp = await page.evaluate(async (b64) => {
    const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0))
    const fd = new FormData()
    fd.append('files', new File([bin], 'receipt.png', { type: 'image/png' }))
    fd.append('title', '')
    fd.append('text', '')
    fd.append('url', '')
    const r = await fetch('/share-receive', { method: 'POST', body: fd, redirect: 'follow' })
    return { status: r.status, url: r.url, redirected: r.redirected }
  }, png)
  console.log('  POST 结果:', JSON.stringify(resp))
  ok('分享请求被 SW 接住并 303 跳到处理页', resp.url.includes('/app/share-incoming'), resp.url)

  console.log('\n=== 3. 跳到分享处理页 ===')
  await page.goto(`${BASE}/app/share-incoming`,{waitUntil:'networkidle'})
  await page.waitForTimeout(2000)
  ok('页面最终落到交易页并打开快速新建', page.url().includes('/app/transactions'), page.url())
  ok('出现快速新建对话框', (await page.getByRole('dialog').count()) > 0)

  console.log('\n=== 4. 分享的小票应已挂在表单上 ===')
  const dlg = page.getByRole('dialog')
  const attachCount = await dlg.locator('input[type="file"]').count()
  ok('表单里有附件选择器', attachCount > 0)
  const badge = await dlg.getByText(/已附\s*\d+\s*个|1\s*attached/).count()
  ok('附件计数显示已挂 1 个', badge > 0, `badge=${badge}`)

}

await browser.close()
console.log('\n' + (fails.length ? `FAILED: ${fails.join('; ')}` : 'UI SMOKE ALL PASSED'))
process.exit(fails.length ? 1 : 0)
