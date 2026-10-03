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
    expect(src).toMatch(/!isTransfer \?/)
  })

  it('读类型的 api-client 声明了 splits(否则前端读不到腿)', () => {
    const types = fs.readFileSync(
      path.resolve(WEB_SRC, '../../../packages/api-client/src/types.ts'), 'utf8')
    expect(types).toMatch(/splits\?:\s*TransactionSplit\[\]/)
    expect(types).toMatch(/splits\?:\s*TransactionSplitPayload\[\] \| null/)
  })
})
