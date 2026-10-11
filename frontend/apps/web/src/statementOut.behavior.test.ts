/**
 * 「本期应还」派生函数的**真行为**测试。
 *
 * ## 为什么这一份是真跑,不是源码正则
 *
 * 2026-10-12 的变异审计里,这一块 11 条护栏有 5 条是**假的** ——
 * 全是「字符串存在性」检查。具体:
 *
 *   变异 A:`statementOut` 恒 `return null`  → 所有测试绿(功能整个失效)
 *   变异 B:去掉 `account_type` 守卫        → 所有测试绿(非信用卡也显示)
 *
 * 根因:正则只能证明「代码被写下来了」,证明不了「代码被执行到了」。
 * 这与 CLAUDE.md 记的「hook 插进函数体,语法合法但永不可达」是同一类问题,
 * 只是这次发生在**测试断言**而不是产品代码里。
 *
 * 之前一直以为前端测试只能做源码正则(没有 jsdom / testing-library)。
 * 那是**假设**,不是限制 —— vitest 能直接 import 模块,`statementOut`
 * 是纯函数(无 React / 无 DOM),完全可以直接调。
 *
 * 同文件里那些 i18n 键存在性检查继续保留:它们各自能抓自己的东西
 * (把 label 换成别的 key 那条变异就是它们抓到的)。
 */
import { describe, expect, it } from 'vitest'

import {
  statementOut,
  type AccountWithStats,
} from './components/dialogs/AccountDetailDialog'

/** 造一个最小可用的信用卡账户。 */
function card(over: Partial<AccountWithStats> = {}): AccountWithStats {
  return {
    id: 'acc-1',
    name: '招行信用卡',
    account_type: 'credit_card',
    currency: 'JPY',
    statement_bill: 30000,
    statement_outstanding: 30000,
    next_due_date: '2026-11-25',
    ...over,
  } as AccountWithStats
}

describe('statementOut —— 本期应还派生(真跑,不是读源码)', () => {
  it('有完整数据时给出金额、账单、还款日', () => {
    const out = statementOut(card())
    expect(out).not.toBeNull()
    expect(out!.outstanding).toContain('30,000')
    expect(out!.bill).toContain('30,000')
    expect(out!.due).toBe('2026-11-25')
    // 没差额 → 不是「已还一部分」
    expect(out!.partial).toBe(false)
  })

  it('已手动还一部分时 partial = true', () => {
    const out = statementOut(
      card({ statement_bill: 30000, statement_outstanding: 12000 }),
    )
    expect(out!.partial).toBe(true)
    expect(out!.outstanding).toContain('12,000')
    expect(out!.bill).toContain('30,000')
  })

  it('欠款 0 也返回结果 —— 已还清是**有效状态**,空白会被理解成「没数据」', () => {
    const out = statementOut(
      card({ statement_bill: 0, statement_outstanding: 0 }),
    )
    expect(out).not.toBeNull()
    expect(out!.partial).toBe(false)
    expect(out!.outstanding).toContain('0.00')
  })

  it('非信用卡返回 null(不该给银行储蓄卡显示「本期应还」)', () => {
    expect(statementOut(card({ account_type: 'bank_card' }))).toBeNull()
    expect(statementOut(card({ account_type: null }))).toBeNull()
    expect(statementOut(card({ account_type: '' }))).toBeNull()
  })

  it('三缺一就返回 null(部分缺失 = 数据不完整,显示一半更让人困惑)', () => {
    expect(statementOut(card({ statement_bill: null }))).toBeNull()
    expect(statementOut(card({ statement_outstanding: null }))).toBeNull()
    expect(statementOut(card({ next_due_date: null }))).toBeNull()
    expect(statementOut(card({ next_due_date: undefined }))).toBeNull()
  })

  it('字段类型不对(后端给了字符串)也返回 null,不显示垃圾', () => {
    // 后端 JSON 里数字是 number;若某天变成字符串,宁可整块不显示
    expect(
      statementOut(card({ statement_outstanding: '30000' as never })),
    ).toBeNull()
  })

  it('小数按两位格式化', () => {
    const out = statementOut(
      card({ statement_bill: 1234.5, statement_outstanding: 1234.5 }),
    )
    expect(out!.outstanding).toContain('1,234.50')
  })
})