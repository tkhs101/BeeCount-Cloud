import { describe, expect, it } from 'vitest'

import {
  isSplitPayment,
  parseSplits,
  splitsRemainder,
  splitsToInput,
} from '@beecount/web-features'

/** 组合支付拆分的表单转换(0021)。
 *
 * 守住两条约定:
 *
 * 1. **round-trip** —— `splitsToInput → parseSplits` 必须回到同一批腿。
 *    漏了会变成「编辑一笔组合支付再保存,腿少了/金额变字符串」这类静默损坏。
 * 2. **前端不重复实现 server 校验** —— `sum(legs) !== amount` 时**不能拦截**。
 *    server 侧 `snapshot_mutator._normalize_splits` 已经强制了;前端再来一套,
 *    两边规则会漂移(比如前端按显示精度放过 4999.5、server 拒了),
 *    用户只看到一个没头没尾的 400。
 *
 * 与 `taxForm.test.ts` 同一套约定。
 */

const L3 = [
  { accountId: 'acc-a', accountName: '招行卡', amount: '3000' },
  { accountId: 'acc-b', accountName: '现金', amount: '2000' },
]

describe('parseSplits', () => {
  it('只做形状转换:snake_case + 数字化', () => {
    expect(parseSplits(L3)).toEqual([
      { account_id: 'acc-a', account_name: '招行卡', amount: 3000 },
      { account_id: 'acc-b', account_name: '现金', amount: 2000 },
    ])
  })

  it('丢掉没填完整的行', () => {
    const rows = [
      { accountId: 'acc-a', accountName: '卡', amount: '3000' },
      { accountId: '', accountName: '没选账户', amount: '500' },
      { accountId: 'acc-c', accountName: '', amount: '' },
    ]
    expect(parseSplits(rows)).toEqual([
      { account_id: 'acc-a', account_name: '卡', amount: 3000 },
    ])
  })

  it('金额非法 / <=0 的行也丢', () => {
    const rows = [
      { accountId: 'a', accountName: 'A', amount: 'abc' },
      { accountId: 'b', accountName: 'B', amount: '0' },
      { accountId: 'c', accountName: 'C', amount: '-5' },
    ]
    expect(parseSplits(rows)).toBeNull()
  })

  it('空 / undefined 返回 null —— 不是空数组', () => {
    // 返回 null 与 `[]` 语义不同:update 时 `splits: []` 是「清除全部腿」,
    // 不传才是「不动」。混淆会清空用户拆好的支付方式。
    expect(parseSplits([])).toBeNull()
    expect(parseSplits(undefined)).toBeNull()
    expect(parseSplits(null)).toBeNull()
  })

  it('不校验总和 —— 那是 server 的事', () => {
    // 3000 + 100 ≠ 5000,前端照样放行
    const out = parseSplits([
      { accountId: 'a', accountName: 'A', amount: '3000' },
      { accountId: 'b', accountName: 'B', amount: '100' },
    ])
    expect(out).toHaveLength(2)
  })
})

describe('splitsToInput / round-trip', () => {
  it('payload → 表单 的 camel/snake 转换', () => {
    expect(splitsToInput([
      { account_id: 'acc-a', account_name: '卡', amount: 3000 },
    ])).toEqual([{ accountId: 'acc-a', accountName: '卡', amount: '3000' }])
  })

  it('空 / null 返回空数组(不是 null)', () => {
    expect(splitsToInput([])).toEqual([])
    expect(splitsToInput(undefined)).toEqual([])
    expect(splitsToInput(null)).toEqual([])
  })

  it('round-trip 保持腿数与金额', () => {
    const payload = parseSplits(L3)
    expect(payload).not.toBeNull()
    const back = splitsToInput(payload)
    expect(back).toHaveLength(2)
    expect(parseSplits(back)).toEqual(payload)
  })

  it('容忍 camelCase 输入(编辑回显的两种形状都收)', () => {
    expect(splitsToInput([
      { accountId: 'acc-a', accountName: '卡', amount: 100 },
    ] as never)).toEqual([{ accountId: 'acc-a', accountName: '卡', amount: '100' }])
  })
})

describe('splitsRemainder', () => {
  it('凑齐了返回 null(不显示提示)', () => {
    expect(splitsRemainder('5000', L3)).toBeNull()
  })

  it('差多少返回多少', () => {
    expect(splitsRemainder('5000', [
      { accountId: 'a', accountName: 'A', amount: '3000' },
      { accountId: 'b', accountName: 'B', amount: '1000' },
    ])).toBe(1000)
  })

  it('多分了返回负数(调用方取 abs 展示)', () => {
    expect(splitsRemainder('5000', [
      { accountId: 'a', accountName: 'A', amount: '3000' },
      { accountId: 'b', accountName: 'B', amount: '3000' },
    ])).toBe(-1000)
  })

  it('小于半分钱当 0', () => {
    expect(splitsRemainder('5000', [
      { accountId: 'a', accountName: 'A', amount: '3000' },
      { accountId: 'b', accountName: 'B', amount: '2000.004' },
    ])).toBeNull()
  })

  it('没有有效腿 / 总额非法 → null', () => {
    expect(splitsRemainder('5000', [])).toBeNull()
    expect(splitsRemainder('', L3)).toBeNull()
    expect(splitsRemainder('0', L3)).toBeNull()
  })
})

describe('isSplitPayment', () => {
  it('>=2 条有效腿才算组合支付', () => {
    expect(isSplitPayment(L3)).toBe(true)
    expect(isSplitPayment([L3[0]])).toBe(false)
    expect(isSplitPayment([])).toBe(false)
  })

  it('算上被丢弃的空行', () => {
    // 3 行但只有 1 行填完整 → 不是组合支付
    expect(isSplitPayment([L3[0], { accountId: '', accountName: '', amount: '' }]))
      .toBe(false)
  })
})
