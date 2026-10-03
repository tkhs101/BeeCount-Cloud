/**
 * 护栏：阻止「客户端重复实现 server 金额口径」的**第四个副本**。
 *
 * ## 为什么需要它
 *
 * 这个 bug 模式已经出现过三次，而且**三次都是单币种账本完全正常**，靠肉眼看
 * 不出来：
 *
 *   1. `CategoryDetailDialog.aggregate` —— 税额没按比率折本位币
 *   2. `annual-report/data/aggregate.ts` —— 一律用原币，外币交易把 CNY 与 JPY 相加
 *   3. 分类详情不过滤 `exclude_from_stats`，笔数用原始数组长度
 *
 * 修了两个才发现第三个。第三次是主动 grep `reduce((s` 才找全的 —— 靠人记
 * 不住，所以这里把它变成一条会失败的检查。
 *
 * ## 判定方式
 *
 * 扫描所有前端源码，找出**参与金额运算的 `.amount` 属性访问**（加法、`+=`、
 * `reduce`、`Math.abs` 等）。命中就必须满足其一：
 *
 *   - 该文件从 `amountBasis` 引入了 `baseAmount` / `taxInBaseCurrency`；或
 *   - 该文件在 `ALLOW` 表里，且写明理由（预算 / 账户余额等本来就该用原币的
 *     场景 —— server 侧对账户维度聚合有明确的反向契约，见
 *     `read/_shared.py` 的注释）。
 *
 * 手法与 `hookPlacement.test.ts` 相同：抓的正是 tsc / build 抓不到的那类
 * 静默错误 —— 类型完全合法，只是数字错了。
 */
import * as fs from 'node:fs'
import * as path from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

// __dirname 就是 apps/web/src
const WEB_SRC = __dirname
const WEB_FEATURES = path.resolve(WEB_SRC, '../../../packages/web-features/src')

/**
 * 允许直接用 `.amount` 参与运算的文件,附理由。
 *
 * 这张表同时是一份**已复核记录**:每条都写清了「为什么这里用原币是对的」或
 * 「为什么命中的是误报」。护栏刻意做得宽(宁可多报),代价就是每加一个真实
 * 聚合点都可能需要往这里补一条 —— 但总比第四个 bug 悄悄溜过去强。
 */
const ALLOW: Record<string, string> = {
  'lib/txSplits.ts':
    'splitsRemainder 做的是**同币种内**的运算:表单里用户输入的总额 vs 用户输入的'
    + '各腿之和,两者都是原币,不涉及折算(拆分腿结构上就没有 currency 字段 —— '
    + 'currency 只有父交易有)。它算的是「还差多少没分配」的提示值,不是聚合'
    + '多笔交易。真正的校验在 server 的 snapshot_mutator._normalize_splits。',
  'components/dashboard/BudgetUsagePanel.tsx':
    '预算额度与用量都是服务端按本位币算好的聚合结果,前端只做减法比较,不聚合交易',
  'pages/sections/BudgetsPage.tsx':
    '预算是服务端聚合结果,前端只做减法;这里的 .amount 是预算行自己的额度',
  'pages/sections/OverviewPage.tsx':
    '只比较预算额度与已用量,两者都来自服务端聚合,不碰交易原币',
  'features/BudgetsPanel.tsx':
    '预算是服务端聚合结果,前端只做减法;这里的 .amount 是预算行自己的额度',
  'components/dialogs/CategoryDetailDialog.tsx':
    '本文件已从共享口径引 taxInBaseCurrency,聚合全部走它;命中的那几行比较的是 ' +
    'monthlyMap / accountMap 里**已经聚合好**的桶值(v.amount),不是交易原币',
  'components/dialogs/TransactionDetailDialog.tsx':
    '单笔详情展示「税前 = 实付 − 税额」,三者同为原币单位,相减正确;' +
    '上方大数字也显示原币并把本位币作为「≈」副标,与 server 的读出口径一致',
}

/** 收集参与运算的 `.amount` 属性访问所在行。 */
function amountArithmeticLines(sf: ts.SourceFile): Set<number> {
  const hits = new Set<number>()

  const record = (n: ts.Node) => {
    const { line } = sf.getLineAndCharacterOfPosition(n.getStart())
    hits.add(line + 1)
  }

  const isAmountAccess = (n: ts.Node): n is ts.PropertyAccessExpression =>
    ts.isPropertyAccessExpression(n) && n.name.text === 'amount'

  const visit = (n: ts.Node) => {
    // a + b / a - b —— 任一侧是 .amount 即命中
    if (
      (ts.isBinaryExpression(n) &&
        (n.operatorToken.kind === ts.SyntaxKind.PlusToken ||
          n.operatorToken.kind === ts.SyntaxKind.MinusToken)) &&
      (isAmountAccess(n.left) || isAmountAccess(n.right))
    ) {
      record(isAmountAccess(n.left) ? n.left : n.right)
    }
    // x += .amount / -= .amount
    if (
      ts.isBinaryExpression(n) &&
      (n.operatorToken.kind === ts.SyntaxKind.PlusEqualsToken ||
        n.operatorToken.kind === ts.SyntaxKind.MinusEqualsToken) &&
      isAmountAccess(n.right)
    ) {
      record(n.right)
    }
    // Math.abs(...amount...) / Math.max / Math.min
    if (
      ts.isCallExpression(n) &&
      ts.isPropertyAccessExpression(n.expression) &&
      ['abs', 'max', 'min', 'round'].includes(n.expression.name.text) &&
      n.arguments.some(isAmountAccess)
    ) {
      record(n.arguments.find(isAmountAccess)!)
    }
    ts.forEachChild(n, visit)
  }
  ts.forEachChild(sf, visit)
  return hits
}

function* sourceFiles(root: string): Generator<string> {
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name === 'dist') continue
      yield* sourceFiles(full)
    } else if (/\.tsx?$/.test(entry.name) && !entry.name.endsWith('.test.ts')) {
      yield full
    }
  }
}

describe('金额口径护栏', () => {
  const offenders: string[] = []

  for (const root of [WEB_SRC, WEB_FEATURES]) {
    for (const file of sourceFiles(root)) {
      const rel = path.relative(root, file).split(path.sep).join('/')
      if (rel in ALLOW) continue
      const src = fs.readFileSync(file, 'utf8')
      // 已经在用共享口径的,跳过 —— 它们的 .amount 用法是被 baseAmount 包住的
      // 用共享口径的文件:看它引入了哪些标识符(统一从 '@beecount/web-features')
      if (/baseAmount|taxInBaseCurrency|splitTax/.test(src)) continue
      if (!/\.amount\b/.test(src)) continue

      const sf = ts.createSourceFile(file, src, ts.ScriptTarget.ESNext, true)
      const lines = amountArithmeticLines(sf)
      if (lines.size > 0) {
        offenders.push(
          `${rel}:${[...lines].sort((a, b) => a - b).join(',')} —— ` +
            `交易金额参与运算却没用 baseAmount()。` +
            `请从 '@beecount/web-features' 引入，或在 ALLOW 里写明理由。`,
        )
      }
    }
  }

  it('没有绕过共享口径的金额运算', () => {
    expect(
      offenders,
      `\n${offenders.join('\n')}\n\n` +
        `背景:这个 bug 已经出现过三次(详见本文件头)。单币种账本全部正常,\n` +
        `所以只能靠这条检查拦。判定口径 = server 的 coalesce(native_amount, amount)。`,
    ).toEqual([])
  })


})