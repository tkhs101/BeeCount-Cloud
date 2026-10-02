/**
 * 分类详情弹窗的客户端聚合 —— 这条路径必须和 server 的
 * `workspace_analytics` 保持同一口径,否则「点进分类看到的合计」和
 * 「饼图上的切片」对不上。
 *
 * 三处口径差异（都是聚合函数的职责，不能靠调用方自觉）：
 *   1. 基数取 `native_amount ?? amount`（外币折本位币）
 *   2. 净额 = 基数 − 税额（饼图切片是税前）
 *   3. 排除 `exclude_from_stats` 的交易（server 在 SQL 层就滤了）
 *
 * 第 3 条是这轮补的存量缺陷：之前这里不过滤，用户把一笔大额标记成
 * 「不计入收支统计」之后，饼图里没有它、分类详情里却有，点进去对不上账。
 */
import { describe, expect, it } from 'vitest'

import type { WorkspaceTransaction } from '@beecount/api-client'

import { aggregate } from './CategoryDetailDialog'

// 直接用真实类型,而不是手搓一个近似体 —— 否则测试里造的假交易和生产里的
// 真实交易会悄悄漂移(而且 tsc 会因为类型不匹配直接报错,逼你改对)。
type Tx = WorkspaceTransaction

const TX_DEFAULTS = {
  id: 'tx1',
  tx_index: 0,
  tx_type: 'expense' as const,
  amount: 100,
  happened_at: '2026-10-03T12:00:00+00:00',
  note: null,
  category_name: '餐饮',
  category_kind: 'expense',
  account_name: '现金',
  from_account_name: null,
  to_account_name: null,
  tags: null,
  tags_list: [],
  attachments: null,
  last_change_id: 1
}

function tx(over: Partial<Tx> = {}): Tx {
  return { ...TX_DEFAULTS, ...over } as Tx
}

describe('aggregate —— 与 server 口径对齐', () => {
  it('排除标记「不计入收支统计」的交易', () => {
    const stats = aggregate([
      tx({ amount: 1000 }),
      tx({ amount: 5000, exclude_from_stats: true }),
    ])
    // 饼图里只有 1000 那笔；详情页也必须只有 1000
    expect(stats.total).toBe(1000)
    expect(stats.count).toBe(1)
  })

  it('税前口径:净额 = 基数 − 税额,并给出含税合计', () => {
    const stats = aggregate([tx({ amount: 3280, tax_amount: 298 })])
    expect(stats.total).toBe(2982)
    expect(stats.taxTotal).toBe(298)
    expect(stats.grossTotal).toBe(3280)
  })

  it('无税时 total === grossTotal', () => {
    const stats = aggregate([tx({ amount: 100 })])
    expect(stats.total).toBe(100)
    expect(stats.grossTotal).toBe(100)
    expect(stats.taxTotal).toBe(0)
  })

  it('外币按折本位币聚合(native_amount ?? amount)', () => {
    const stats = aggregate([
      tx({ amount: 50, native_amount: 1000, tax_amount: 5 }),
    ])
    // 1000 − 1000 × (5/50) = 900
    expect(stats.total).toBe(900)
    expect(stats.taxTotal).toBe(100)
  })

  it('税额脏数据(大于金额)不会算出负数', () => {
    const stats = aggregate([tx({ amount: 100, tax_amount: 500 })])
    expect(stats.total).toBeGreaterThanOrEqual(0)
    // 脏税额被夹到基数以内,净额不可能为负
    expect(stats.grossTotal).toBe(100)
  })

  it('笔均 / 单笔最高 都基于净额', () => {
    const stats = aggregate([
      tx({ amount: 3280, tax_amount: 298 }),
      tx({ amount: 100 }),
    ])
    // 最高那笔是税前口径
    expect(stats.max.amount).toBe(2982)
    expect(stats.avg).toBeCloseTo((2982 + 100) / 2, 6)
  })

  it('空列表不炸', () => {
    const stats = aggregate([])
    expect(stats.count).toBe(0)
    expect(stats.total).toBe(0)
    expect(stats.avg).toBe(0)
    expect(stats.monthly).toEqual([])
    expect(stats.peak).toBeNull()
  })
})