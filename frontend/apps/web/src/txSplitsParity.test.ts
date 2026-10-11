import * as fs from 'node:fs'
import * as path from 'node:path'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

/** 护栏:`splits` 在前端的**每一条路径**上都要出现。
 *
 * ## 为什么需要
 *
 * 前端有**两条复制粘贴关系**的提交路径:
 *
 * - `pages/sections/TransactionsPage.tsx` 的新建/编辑提交
 * - `components/GlobalEditDialogs.tsx` 的全局编辑弹窗
 *
 * 代码注释里已经自认「R3:改一处漏一处会静默丢字段」。后端也有同形态的坑
 * (`tests/test_tx_field_parity.py` 记着 `tax_amount` 被 pydantic `extra='ignore'`
 * 和批量白名单各丢一次)。
 *
 * `splits` 比 `tax_amount` 更危险:漏掉**回显**那一步 → 用户编辑一笔组合支付
 * 保存 → payload 里 `splits: []` → server 当成「清除全部腿」→ 分摊额消失,
 * 余额漂移,HTTP 200,不报错。
 *
 * 本次开发里 TS 类型系统先抓到了一处(`TxForm` 加了字段但两处字面量没跟上),
 * 但那只覆盖**编译期**;接线层的遗漏(编辑回显、applyTxType 清理、编辑器真的
 * 被渲染)类型系统抓不到。
 */

const WEB_SRC = __dirname
const FEATURES_SRC = path.resolve(WEB_SRC, '../../../packages/web-features/src')

function _read(rel: string): string {
  return fs.readFileSync(path.resolve(WEB_SRC, rel), 'utf8')
}

/** 取某个对象字面量的顶层键。 */
function _objectLiteralKeys(src: string, anchor: string): Set<string> | null {
  const sf = ts.createSourceFile(
    't.ts', src, ts.ScriptTarget.ESNext, true,
  )
  let found: Set<string> | null = null
  const walk = (n: ts.Node) => {
    if (found) return
    if (ts.isObjectLiteralExpression(n)) {
      const text = n.getText(sf)
      if (text.includes(anchor) && text.length < 4000) {
        found = new Set(
          n.properties
            .map((p) => (ts.isPropertyAssignment(p) ? p.name.getText(sf) : null))
            .filter((k): k is string => !!k)
            .map((k) => k.replace(/^['"]|['"]$/g, '')),
        )
      }
    }
    ts.forEachChild(n, walk)
  }
  ts.forEachChild(sf, walk)
  return found
}

const TX_PAGE = 'pages/sections/TransactionsPage.tsx'
const GLOBAL_DIALOG = 'components/GlobalEditDialogs.tsx'
const PANEL = path.resolve(FEATURES_SRC, 'features/TransactionsPanel.tsx')

describe('splits 字段对齐护栏', () => {
  it('TxForm 声明了 splits', () => {
    const forms = fs.readFileSync(
      path.resolve(FEATURES_SRC, 'forms.ts'), 'utf8')
    expect(forms).toMatch(/splits:\s*SplitForm\[\]/)
  })

  it('txDefaults 初始化了 splits(漏了 → onFormChange 展开出 undefined)', () => {
    const forms = fs.readFileSync(
      path.resolve(FEATURES_SRC, 'forms.ts'), 'utf8')
    const defaults = forms.slice(forms.indexOf('export const txDefaults'))
    expect(defaults).toMatch(/splits:\s*\[\]/)
  })

  it('两条提交路径的 payload 都带 splits', () => {
    const page = _objectLiteralKeys(_read(TX_PAGE), 'isSplitPayment(txForm.splits)')
    const dialog = _objectLiteralKeys(
      _read(GLOBAL_DIALOG), 'isSplitPayment(editTxForm.splits)')
    expect(page, 'TransactionsPage 的提交 payload 找不到').not.toBeNull()
    expect(dialog, 'GlobalEditDialogs 的提交 payload 找不到').not.toBeNull()
    expect([...(page as Set<string>)]).toContain('splits')
    expect([...(dialog as Set<string>)]).toContain('splits')
  })

  it('两条编辑回显路径都带 splits', () => {
    // 回显漏了 → 编辑保存时 splits: [] → server 当成「清除全部腿」
    for (const rel of [TX_PAGE, GLOBAL_DIALOG]) {
      const src = _read(rel)
      expect(src, `${rel} 的编辑回显缺少 splitsToInput(tx.splits)`)
        .toMatch(/splits:\s*splitsToInput\(tx\.splits/)
    }
  })

  it('applyTxType 切类型时清空 splits', () => {
    // 和 currency 同一个坑:控件隐藏了但脏值留在 form 里跟着 payload 上行。
    // server 侧 splits 只允许 expense → 残留腿会让「改个交易类型」报 400。
    const src = fs.readFileSync(PANEL, 'utf8')
    const apply = src.slice(
      src.indexOf('const applyTxType = '),
      src.indexOf('const applyTxType = ') + 2000,
    )
    const clears = apply.match(/splits:\s*(\[\]|nextType === 'expense' \? form\.splits : \[\])/g)
    expect(clears && clears.length,
      'applyTxType 里没有清空 splits 的分支').toBeTruthy()
  })

  it('拆分编辑器真的被渲染了(接线层,不是只有纯函数)', () => {
    const src = fs.readFileSync(PANEL, 'utf8')
    expect(src).toMatch(/transactions\.splits\.title/)
    expect(src).toMatch(/transactions\.splits\.add/)
    // 少于 2 条腿在 server 端会拒绝 → UI 不该在非 expense 时出现
    // 条件是 **tx_type === expense**,不是 `!isTransfer`。
    // 服务端只允许 expense 有腿(`snapshot_mutator`: "splits is only
    // allowed on expense transactions")。原来这里写 `!isTransfer` ——
    // 于是 income 也会渲染编辑器,用户先选 income 再加腿,提交必得 400。
    // 这条护栏把那个宽松条件当正确行为锁了很久,现在改的是条件本身。
    expect(src).toMatch(/form\.tx_type === 'expense' \? \(/)
  })

  it('读类型的 api-client 声明了 splits(否则前端读不到腿)', () => {
    const types = fs.readFileSync(
      path.resolve(WEB_SRC, '../../../packages/api-client/src/types.ts'), 'utf8')
    expect(types).toMatch(/splits\?:\s*TransactionSplit\[\]/)
    expect(types).toMatch(/splits\?:\s*TransactionSplitPayload\[\] \| null/)
  })
})


/**
 * 找出「以某个表达式作为门条件的 JSX 那一行」,**原样返回整行**。
 *
 * 为什么需要:只查 `toMatch(/splits\?\.length \?\? 0/)` 的话,
 * 把代码改成 `{false && (tx.splits?.length ?? 0) >= 2 ? (...)}` 照样通过 ——
 * 子串还在,渲染已经死了。本仓没有 jsdom(跑不了真渲染),
 * 行首锚定是当前能做到的最强判断。
 */
function _gatingLine(src: string, needle: string): string {
  const NEWLINE = String.fromCharCode(10)
  const line = src.split(NEWLINE).find(
    (l) => l.includes(needle) && l.includes('? ('),
  )
  if (!line) throw new Error(`找不到以 ${needle} 为门条件的那一行`)
  return line.trim()
}

describe('splits —— 真的渲染到屏幕上(补 2026-10-11 审计的盲区)', () => {
  const _read = (rel: string) =>
    fs.readFileSync(path.resolve(__dirname, rel), 'utf8')

  // 这三个护栏抓的是同一类问题:**类型合法、字段也在 payload 里,
  // 但没有任何渲染代码**。所以「组合支付在详情弹窗零展示」能一直活着。
  // 断言一律要求条件分支本身存在,而不是只查字符串出现过。

  it('详情弹窗在腿数 >= 2 时渲染腿列表', () => {
    const src = _read('components/dialogs/TransactionDetailDialog.tsx')
    // ⚠️ **只查「模式出现过」是不够的** —— 我第一版就写成
    //   `expect(src).toMatch(/\(tx\.splits\?\.length \?\? 0\) >= 2/)`,
    //   然后用 `{false && (...)} 包一层照样通过:子串还在,代码死了。
    //   这正是本次审计批评的那类护栏(「声明存在性」而非「真的渲染」)。
    //
    //   所以改成**行首锚定**:那一行的花括号后必须紧跟条件本身,
    //   前面不许有 false/0/!true 之类的前缀。
    expect(_gatingLine(src, 'tx.splits?.length ?? 0')).toBe(
      '{(tx.splits?.length ?? 0) >= 2 ? (',
    )
    // 每行要显示账户名或 id,不能只显示金额
    expect(src).toMatch(/leg\.account_name \|\| leg\.account_id/)
  })

  it('详情弹窗显示「合计」以守住「父 amount 仍是总额」', () => {
    // 用户看到腿加起来 == 上面那个大金额,才不会以为少了一笔
    const src = _read('components/dialogs/TransactionDetailDialog.tsx')
    expect(src).toMatch(/splitTotal/)
    expect(src).toMatch(/reduce\(\(s, l\) => s \+ l\.amount, 0\)/)
  })

  it('TransactionRow 在有腿时打组合支付徽标', () => {
    // 没有它,账户详情里 5000 vs 余额只动 3000 无法解释,
    // 用户最可能的归因是「余额算错了」→ 手动改初始余额。
    const src = _read('../../../packages/web-features/src/components/TransactionRow.tsx')
    // 同上:行首锚定,不许被 `false &&` 之类的前缀绕过
    expect(_gatingLine(src, 'row.splits?.length ?? 0')).toBe(
      '{(row.splits?.length ?? 0) >= 2 ? (',
    )
    expect(src).toMatch(/transactions\.splits\.badge/)
  })

  it('腿的金额是原币,不折算', () => {
    // 组合支付跨币种被服务端刻意拒绝(转账两端共用一个 amount),
    // 所以腿与父必然同币种 —— 折算反而会引入汇率时点问题。
    //
    // ⚠️ 只查**腿那块**,不查整个文件:弹窗别处合法地显示 native_amount
    //   (“≈ 本位币”换算行)。第一版查整份文件,被自己误报成红。
    const src = _read('components/dialogs/TransactionDetailDialog.tsx')
    const block = src.slice(src.indexOf('组合支付(0021)'))
    expect(block).not.toMatch(/native_amount/)
    expect(block).not.toMatch(/baseAmount|toBaseCurrency|convertToBase/)
  })

  it('i18n 三语都有 detail.transaction.splitTitle / splitTotal', () => {
    for (const f of ['en.ts', 'zh-CN.ts', 'zh-TW.ts']) {
      const src = _read(path.join('i18n', f))
      for (const k of ['splitTitle', 'splitTotal']) {
        expect(src, `${f} 缺 detail.transaction.${k}`)
          .toMatch(new RegExp(`'detail\.transaction\.${k}'`))
      }
    }
  })

  it('i18n 三语都有 transactions.splits.badge', () => {
    for (const f of ['en.ts', 'zh-CN.ts', 'zh-TW.ts']) {
      const src = _read(path.join('i18n', f))
      expect(src, `${f} 缺 transactions.splits.badge`)
        .toMatch(/'transactions\.splits\.badge'/)
    }
  })
})


describe('splits —— 两个「控件打架」的组合', () => {
  const _read = (rel: string) =>
    fs.readFileSync(path.resolve(__dirname, rel), 'utf8')
  const PANEL = '../../../packages/web-features/src/features/TransactionsPanel.tsx'

  it('拆分编辑器只对 expense 显示', () => {
    // 服务端:`splits is only allowed on expense transactions`
    // 原先是 `!isTransfer` —— income 也渲染,用户先选 income 再加腿,
    // 带着 2 条腿提交 → 400 + 一句英文技术串。
    const src = _read(PANEL)
    expect(_gatingLine(src, "form.tx_type")).toContain(
      "form.tx_type === 'expense'",
    )
    // 不许再退回 `!isTransfer`
    expect(src).not.toMatch(/\{!isTransfer \? \(/)
  })

  it('有腿时隐藏账户下拉,而不是让用户白填', () => {
    // 服务端在有腿时 `pop('accountName')` —— 静默丢弃。
    // 用户回列表看到这笔「无账户」,而他明明选过,没有任何提示。
    // 隐藏比提示彻底:看不到就不会去填。
    const src = _read(PANEL)
    expect(src).toMatch(/\) : hasSplits \? \(/)
    expect(src).toMatch(/transactions\.splits\.accountCleared/)
  })

  it('hasSplits 用的是 SplitForm 的驼峰字段名', () => {
    // 写成下划线(account_name)编译能过、恒为 false —— 于是分支永远不进,
    // 用户照旧看到那个会吞掉他输入的账户下拉。
    const src = _read(PANEL)
    expect(src).toMatch(/l\.accountName/)
    expect(src).not.toMatch(/l\.account_name/)
  })

  it('i18n 三语都有 transactions.splits.accountCleared', () => {
    for (const f of ['en.ts', 'zh-CN.ts', 'zh-TW.ts']) {
      const src = _read(path.join('i18n', f))
      expect(src, `${f} 缺 transactions.splits.accountCleared`)
        .toMatch(/'transactions\.splits\.accountCleared'/)
    }
  })
})
