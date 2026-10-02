/**
 * 年度报告的聚合口径 —— 与 server 的 `workspace_analytics` 必须一致。
 *
 * 这条路径修过一个真 bug：聚合一律用**原币** `amount`，单币种账本看不出
 * 问题，一旦账本里有外币交易，CNY 和 JPY 会被直接加在一起 —— 年度总收支、
 * 月度趋势、时段分布、分类排行全部错。
 *
 * 与 `CategoryDetailDialog.aggregate` 是同一个 bug 的两个副本，所以两边一起修。
 * 两处都漏掉，说明「客户端重复实现 server 口径」这个模式本身值得盯。
 */
import { describe, expect, it } from 'vitest'

import { aggregate } from '@beecount/web-features'

type Lite = {
  id: string
  txType: 'expense' | 'income' | 'transfer'
  amount: number
  nativeAmount?: number | null
  taxAmount?: number | null
  happenedAt: string
  note: string | null
  categoryName: string | null
  categoryKind: string | null
  accountName: string | null
  tagsList: string[]
}

function lite(over: Partial<Lite> = {}): Lite {
  return {
    id: 't1',
    txType: 'expense',
    amount: 100,
    happenedAt: '2026-03-15T12:00:00+00:00',
    note: null,
    categoryName: '餐饮',
    categoryKind: 'expense',
    accountName: '现金',
    tagsList: [],
    ...over,
  }
}

function run(txs: Lite[]) {
  return aggregate({
    thisYearTxs: txs,
    prevYearTxs: [],
    year: 2026,
    ledger: { id: 'lg1', name: 'L', currency: 'JPY' },
  })
}

describe('年度报告聚合 —— 多币种口径', () => {
  it('外币交易按折本位币汇总,不把两种币直接相加', () => {
    // 100 JPY 本位 + 50 CNY 折 1000 JPY → 总支出应是 1100,不是 150
    const data = run([
      lite({ amount: 100, nativeAmount: 100 }),
      lite({ amount: 50, nativeAmount: 1000 }),
    ])
    expect(data.totalExpense).toBe(1100)
  })

  it('nativeAmount 缺失时回退原币(旧数据 / 单币种账本)', () => {
    const data = run([
      lite({ amount: 100, nativeAmount: null }),
      lite({ amount: 250, nativeAmount: undefined }),
    ])
    expect(data.totalExpense).toBe(350)
  })

  it('收入同样走折算口径', () => {
    const data = run([
      lite({ txType: 'income', amount: 100, nativeAmount: 100 }),
      lite({ txType: 'income', amount: 50, nativeAmount: 1000 }),
    ])
    expect(data.totalIncome).toBe(1100)
    expect(data.netSavings).toBe(1100)
  })

  it('transfer 不计入收支', () => {
    const data = run([
      lite({ amount: 100, nativeAmount: 100 }),
      lite({ txType: 'transfer', amount: 99999, nativeAmount: 99999 }),
    ])
    expect(data.totalExpense).toBe(100)
    expect(data.totalIncome).toBe(0)
  })

  it('年度总额 = 实付总额,不剥税(D4 同理)', () => {
    // 税额的拆分只发生在分类饼图那类分析视图;报表口径保持总额 = 实付
    const data = run([lite({ amount: 3280, nativeAmount: 3280, taxAmount: 298 })])
    expect(data.totalExpense).toBe(3280)
  })

  it('空输入不炸', () => {
    const data = run([])
    expect(data.totalExpense).toBe(0)
    expect(data.totalIncome).toBe(0)
    expect(data.monthlyData).toHaveLength(12)
  })
})