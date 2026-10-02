/**
 * 表单 ↔ API 的税额字段转换 —— 这是「用户在输入框里打的字」与
 * 「发给 server 的 JSON」之间唯一的闸门。
 *
 * 为什么值得单独测：R3 那类 bug(导入映射漏字段导致税额静默丢失)就是死在
 * 这一层的转换函数上。而且两条提交路径(TransactionsPage / GlobalEditDialogs)
 * 是复制粘贴关系,共用这一个函数正是为了避免两边漂移 —— 有了测试才敢这么
 * 合。
 *
 * 约定：
 *   - `parseTaxAmount('')` → null = **无税**。PATCH 时 server 靠
 *     `exclude_unset` 区分「不传 = 不变」与「显式 null = 清除」,所以空串
 *     必须老老实实变成 null,不能变成 0(0 会被 server 当成非法的正数校验)。
 *   - 不做正负与「小于金额」的拦截 —— 那是 server 的权威口径,前端重复实现
 *     一遍只会造成两边规则漂移。
 */
import { describe, expect, it } from 'vitest'

import { parseTaxAmount, taxAmountToInput } from '@beecount/web-features'

describe('parseTaxAmount —— 输入框字符串 → payload', () => {
  it('正常数字', () => {
    expect(parseTaxAmount('298')).toBe(298)
    expect(parseTaxAmount('0.5')).toBe(0.5)
  })

  it('容忍前后空白(小票复制常带空格)', () => {
    expect(parseTaxAmount('  298  ')).toBe(298)
    expect(parseTaxAmount('\t298\n')).toBe(298)
  })

  it('空串 / 空白 / null / undefined → null(= 无税)', () => {
    expect(parseTaxAmount('')).toBeNull()
    expect(parseTaxAmount('   ')).toBeNull()
    expect(parseTaxAmount(null)).toBeNull()
    expect(parseTaxAmount(undefined)).toBeNull()
  })

  it('非数字 → null,不抛', () => {
    // 小票 OCR / 用户手抖都可能塞进奇怪的东西,绝不能让整个表单崩掉
    expect(parseTaxAmount('N/A')).toBeNull()
    expect(parseTaxAmount('三〇八')).toBeNull()
    expect(parseTaxAmount('12,000')).toBeNull()
  })

  it('不拦负数与超界值 —— 那是 server 的权威口径', () => {
    // 刻意放行:前端重复实现一遍校验只会造成前后端两套规则漂移,
    // 而 server 会返回 400 + 可读报错(见 test_web_invalid_tax_returns_400)
    expect(parseTaxAmount('-5')).toBe(-5)
    expect(parseTaxAmount('99999')).toBe(99999)
  })

  it('0 会原样传出去(不是 null)—— server 负责拒绝它', () => {
    // 0 ≠ 没填。混成 null 会让「用户填了个非法 0」被当成「没填」静默放过
    expect(parseTaxAmount('0')).toBe(0)
  })
})

describe('taxAmountToInput —— payload → 输入框字符串(编辑回显)', () => {
  it('正常回显', () => {
    expect(taxAmountToInput(298)).toBe('298')
    expect(taxAmountToInput(0.5)).toBe('0.5')
  })

  it('null / undefined / 非有限数 → 空串(= 输入框留空)', () => {
    expect(taxAmountToInput(null)).toBe('')
    expect(taxAmountToInput(undefined)).toBe('')
    expect(taxAmountToInput(Number.NaN)).toBe('')
    expect(taxAmountToInput(Number.POSITIVE_INFINITY)).toBe('')
  })

  it('往返一致:parse(回显(x)) === x', () => {
    for (const v of [298, 0.5, 1234.56]) {
      expect(parseTaxAmount(taxAmountToInput(v))).toBe(v)
    }
    // 无税往返后仍是 null,不会被变成 0
    expect(parseTaxAmount(taxAmountToInput(null))).toBeNull()
  })
})