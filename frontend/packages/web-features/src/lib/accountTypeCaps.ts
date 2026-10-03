/** 账户类型的「能力」判定 —— 两个正交维度，不是一个 `isBankOrCredit` 二元判断。
 *
 * ## 为什么拆开
 *
 * 原来只有一个 `isBankOrCredit = isCreditCard || accountType === 'bank_card'`,
 * 控制着「开户行」和「卡号后四位」两个字段的显示。它散落在**三个文件、四段
 * 代码**里(AccountDetailDialog 两处、AccountsPage 两处、AccountsPanel 两处),
 * 只改其中三处就会出现「新类型能填开户行但编辑弹窗不渲染该输入框」或
 * 「切类型时把已填的 bank_name 清掉」。
 *
 * 拆分后维度是正交的:
 *
 * | 类型 | 有开户行 | 有卡号后四位 |
 * |---|---|---|
 * | `bank_card` / `credit_card` | ✓ | ✓ |
 * | `bank_account`(普通存款,没卡) | ✓ | ✗ |
 * | 其余 | ✗ | ✗ |
 *
 * `hasCardLastFour ⊆ hasBankName` 恒成立 —— 有卡号必然也有开户行。
 */
export const BANK_NAME_TYPES = new Set(['bank_card', 'credit_card', 'bank_account'])

export const CARD_LAST_FOUR_TYPES = new Set(['bank_card', 'credit_card'])

/** 该类型是否显示「开户行」。`bank_account` 要显示但没有卡号。 */
export function hasBankName(type: string): boolean {
  return BANK_NAME_TYPES.has(type)
}

/** 该类型是否显示「卡号后四位」。只有真正的卡才有。 */
export function hasCardLastFour(type: string): boolean {
  return CARD_LAST_FOUR_TYPES.has(type)
}
