/**
 * 预览表列判据的**真行为**测试。
 *
 * ## 为什么真跑而不是读源码
 *
 * 变异审计(2026-10-12)里这条是**假的**:
 *
 *   变异:`const hasSplits = false`(列永远不出现)→ 所有测试绿
 *
 * 因为护栏只检查源码里有没有 `import.preview.col.splits` 这个字符串。
 * 字符串在,不代表代码可达 —— 这与 CLAUDE.md 记的「hook 插进函数体,
 * 语法合法但永不可达」是同一类,只是这次在**断言**里。
 *
 * `previewColumns` 已抽成模块级纯函数,现在能直接调。
 */
import { describe, expect, it } from 'vitest'

import type { ImportTransactionSample } from '@beecount/api-client'

import { previewColumns } from './components/import/TransactionsPreviewCard'

function tx(over: Partial<ImportTransactionSample> = {}): ImportTransactionSample {
  return {
    tx_type: 'expense',
    amount: '1000',
    happened_at: '2026-05-13T10:00:00Z',
    note: null,
    category_name: null,
    parent_category_name: null,
    account_name: null,
    from_account_name: null,
    to_account_name: null,
    tag_names: [],
    source_row_number: 2,
    ...over,
  }
}

describe('previewColumns —— 预览表两列的显示判据', () => {
  it('都没有数据时两列都不出现(否则把 8 列表格撑成 10 列)', () => {
    const r = previewColumns([tx(), tx()])
    expect(r.hasTax).toBe(false)
    expect(r.hasSplits).toBe(false)
  })

  it('空样本不炸', () => {
    expect(previewColumns([])).toEqual({ hasTax: false, hasSplits: false })
  })

  it('任一行有税额就出现税额列', () => {
    const r = previewColumns([tx(), tx({ tax_amount: 380 })])
    expect(r.hasTax).toBe(true)
  })

  it('税额为 0 **不**出现(没有信息量的列不占位)', () => {
    expect(previewColumns([tx({ tax_amount: 0 })]).hasTax).toBe(false)
  })

  it('税额为 null / undefined 不出现', () => {
    expect(previewColumns([tx({ tax_amount: null })]).hasTax).toBe(false)
    expect(previewColumns([tx({ tax_amount: undefined })]).hasTax).toBe(false)
  })

  it('**恰好 2 条腿**就必须出现拆分列(阈值不是 3)', () => {
    // 0021 的不变式:至少 2 条腿。2 条腿的组合支付最常见,
    // 阈值写成 >= 3 会让「卡 + 现金」这种最典型的场景看不到拆分列。
    const r = previewColumns([
      tx({ splits: [['招行信用卡', '3000'], ['现金', '2000']] }),
    ])
    expect(r.hasSplits).toBe(true)
  })

  it('只有 1 条腿不出现(1 条等于没拆,走普通单账户路径)', () => {
    expect(previewColumns([tx({ splits: [['招行信用卡', '3000']] })]).hasSplits)
      .toBe(false)
  })

  it('splits 为 null / undefined 不出现', () => {
    expect(previewColumns([tx({ splits: null })]).hasSplits).toBe(false)
    expect(previewColumns([tx({ splits: undefined })]).hasSplits).toBe(false)
  })

  it('只看样本 —— 后面第 500 行有腿也不影响列的出现', () => {
    // 全量扫两个布尔值不划算,而样本之后没数据本来也看不到。
    const many = Array.from({ length: 60 }, () => tx())
    many.push(tx({ splits: [['卡', '1'], ['现金', '1']] }))
    expect(previewColumns(many).hasSplits).toBe(true)
  })
})