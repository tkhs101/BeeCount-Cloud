import { describe, expect, it } from 'vitest'

import {
  autorepayRunMessage,
  type AutorepayRunStatus,
  type MessageKey,
} from './autorepayRunMessage'

/**
 * ⚠️ 这里**真跑**映射,不是 `expect(src).toContain(...)`。
 *
 * CLAUDE.md 记着「前端 45% 的护栏是假的」:字符串存在性检查证明不了
 * 「代码被执行到了」。所以每一条断言都调真的那个函数。
 */

/** `repay_one` 的 10 个 status —— 抄自 `src/services/credit_card/repay.py`。 */
const ALL_STATUSES: AutorepayRunStatus[] = [
  'done',
  'failed',
  'skipped_already_repaid',
  'skipped_disabled',
  'skipped_invalid_config',
  'skipped_locked',
  'skipped_no_funds',
  'skipped_no_schedule',
  'skipped_not_due',
  'skipped_zero_outstanding',
]

describe('autorepayRunMessage', () => {
  it('真还了钱 -> runDone(唯一值得单独说「成功」的)', () => {
    expect(autorepayRunMessage('done')).toBe('detail.autoRepay.runDone')
  })

  it('钱不够 -> runNoFunds(最常见的「点了没反应」)', () => {
    expect(autorepayRunMessage('skipped_no_funds')).toBe(
      'detail.autoRepay.runNoFunds',
    )
  })

  it('没启用 / 配置坏 -> runNotEnabled(该提示去设置)', () => {
    expect(autorepayRunMessage('skipped_disabled')).toBe(
      'detail.autoRepay.runNotEnabled',
    )
    expect(autorepayRunMessage('skipped_invalid_config')).toBe(
      'detail.autoRepay.runNotEnabled',
    )
  })

  it('后端理论上不送 200 的两个 -> runFailed(真遇上了别装作成功)', () => {
    expect(autorepayRunMessage('failed')).toBe('detail.autoRepay.runFailed')
  })

  it('其余 5 个 skipped -> runSkipped(判断链走完、这期不用扣钱)', () => {
    const neutral: AutorepayRunStatus[] = [
      'skipped_already_repaid',
      'skipped_locked',
      'skipped_no_schedule',
      'skipped_not_due',
      'skipped_zero_outstanding',
    ]
    for (const s of neutral) {
      expect(autorepayRunMessage(s), s).toBe('detail.autoRepay.runSkipped')
    }
  })

  // ⚠️ 下面这条是**完整性护栏**,对应后端那三张「默认红」登记表。
  // 后端加了第 11 个 status 而这里没补时,先在这里红,
  // 再由 `Record<>` 让 `tsc -b` 也红 —— 两道门各拦一层。
  it('**10 个 status 一个都不能漏映射**(加新的必须补到这里)', () => {
    const unhandled = ALL_STATUSES.filter(
      (s) => autorepayRunMessage(s) === 'detail.autoRepay.runOk',
    )
    expect(unhandled).toEqual([])
    expect(ALL_STATUSES.length).toBe(10)
  })

  it('每个 status 都落在一个具体的 key 上,没有一个走兜底', () => {
    const seen = new Set<MessageKey>(
      ALL_STATUSES.map(autorepayRunMessage),
    )
    // 兜底的 runOk 只该留给「不认识的值」
    expect(seen.has('detail.autoRepay.runOk')).toBe(false)
  })

  it('不认识的 status 回落到 runOk,**不是** runFailed', () => {
    // ⚠️ 这是本文件最关键的一条:「我不认识」≠「它是坏的」。
    // 后端加了新状态、或老版本压根没这个字段,都不该被谎报成失败。
    expect(autorepayRunMessage('skipped_some_future_reason')).toBe(
      'detail.autoRepay.runOk',
    )
  })

  it('null / undefined / 非字符串 / 空串 都回落到 runOk', () => {
    expect(autorepayRunMessage(null)).toBe('detail.autoRepay.runOk')
    expect(autorepayRunMessage(undefined)).toBe('detail.autoRepay.runOk')
    expect(autorepayRunMessage('')).toBe('detail.autoRepay.runOk')
    expect(autorepayRunMessage({} as unknown as string)).toBe(
      'detail.autoRepay.runOk',
    )
    expect(autorepayRunMessage(42 as unknown as string)).toBe(
      'detail.autoRepay.runOk',
    )
  })

  it('`done` 和各种 `skipped` **不共用同一个 key**(否则等于没分档)', () => {
    expect(autorepayRunMessage('done')).not.toBe(
      autorepayRunMessage('skipped_not_due'),
    )
    expect(autorepayRunMessage('skipped_no_funds')).not.toBe(
      autorepayRunMessage('skipped_not_due'),
    )
  })
})