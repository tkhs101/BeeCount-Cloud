/**
 * `legOptions` 的**真行为**测试。
 *
 * ## 变异审计(2026-10-12)里这一块是假的
 *
 *   变异 1:`if (false)`(不再 pin 住当前账户)→ 所有测试绿
 *   变异 2:`{a.pinned ? ... : null}` 恒假(去掉标注)→ 所有测试绿
 *
 * 原因:两条护栏都只是检查源码里有没有某个字符串,而 `legOptions` 当时
 * 写在 React 组件内部,只能用正则读到 —— 正则证明不了「代码被执行到了」。
 *
 * 现在它是模块级纯函数(`splitLegOptions.ts`),可以直接调。
 * 组件那边只剩 `legOptions(accounts, leg)`,保留的字符串断言负责
 * 盯住「组件确实调了它」+「标注的 i18n 键存在」,两者互补。
 */
import { describe, expect, it } from 'vitest'

import { legOptions } from '../../../packages/web-features/src/features/splitLegOptions'

const JPY = [
  { id: 'card-jpy', name: '招行信用卡' },
  { id: 'cash-jpy', name: '现金' },
]
/** 币种切到 CNY 之后,按币种过滤出来的候选里没有那两张 JPY 卡。 */
const CNY = [{ id: 'bank-cny', name: '招行储蓄卡' }]

describe('legOptions —— 拆分腿的账户候选', () => {
  it('腿的账户在候选里时,原样返回、不加 pinned', () => {
    const out = legOptions(JPY, { accountId: 'card-jpy', accountName: '招行信用卡' })
    expect(out).toHaveLength(2)
    expect(out.every((o) => o.pinned)).toBe(false)
  })

  it('**腿的账户不在候选里时必须 pin 进去** —— 这条是整个函数存在的理由', () => {
    // 编辑 JPY 组合支付时把币种切成 CNY:腿的 accountId 还在 form state,
    // 但候选里没有它。不 pin 的后果 = **界面显示空、实际提交旧值**。
    const out = legOptions(CNY, { accountId: 'card-jpy', accountName: '招行信用卡' })
    expect(out.map((o) => o.id)).toContain('card-jpy')
    expect(out.find((o) => o.id === 'card-jpy')!.pinned).toBe(true)
  })

  it('pinned 项排在**最前面** —— 打开下拉第一眼看到的是自己原来选的那个', () => {
    const out = legOptions(CNY, { accountId: 'card-jpy', accountName: '招行信用卡' })
    expect(out[0].id).toBe('card-jpy')
  })

  it('pinned 项优先用 leg 上的名字,没有则退回 id', () => {
    const withName = legOptions(CNY, { accountId: 'x', accountName: '招行信用卡' })
    expect(withName[0].name).toBe('招行信用卡')
    const noName = legOptions(CNY, { accountId: 'x', accountName: '' })
    expect(noName[0].name).toBe('x')
  })

  it('不重复插入:腿的账户已在候选里就只有一个', () => {
    const out = legOptions(JPY, { accountId: 'cash-jpy', accountName: '现金' })
    expect(out.filter((o) => o.id === 'cash-jpy')).toHaveLength(1)
  })

  it('候选为空时也把腿的账户放进去 —— 否则下拉完全空白,用户以为腿丢了', () => {
    const out = legOptions([], { accountId: 'card-jpy', accountName: '招行信用卡' })
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ id: 'card-jpy', pinned: true })
  })

  it('腿还没选账户时不插空项', () => {
    const out = legOptions(JPY, { accountId: '', accountName: '' })
    expect(out).toHaveLength(2)
    expect(out.every((o) => o.pinned)).toBe(false)
  })

  it('不改原数组(React state 被就地修改是难查的 bug)', () => {
    const rows = [...CNY]
    legOptions(rows, { accountId: 'card-jpy', accountName: '招行信用卡' })
    expect(rows).toHaveLength(1)
  })
})