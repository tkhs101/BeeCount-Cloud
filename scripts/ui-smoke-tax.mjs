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
})
const body = await res.json()
const created = (body.items || []).find((t) => t.amount === 3280)
ok('服务端能读到这笔', !!created)
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
await browser.close()
console.log('\n' + (fails.length ? `FAILED: ${fails.join('; ')}` : 'UI SMOKE ALL PASSED'))
process.exit(fails.length ? 1 : 0)
