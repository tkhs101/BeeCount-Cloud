/** 信用卡自动还款的前端纯逻辑。
 *
 * ## 与 `accountTypeCaps.ts` 的分工
 *
 * `accountTypeCaps` 回答「**账户类型**有没有某字段能力」。
 * 自动还款回答「**这个账户**能不能绑自动还款」—— 那取决于账户**当前
 * 的字段值**(有没有还款日),不只是类型。所以是另一层谓词,不该塞进
 * `accountTypeCaps`。
 *
 * ## 为什么不校验跨币种
 *
 * 服务端 `services/credit_card/config.py` 已经在**写之前**校验过了,
 * 前端再来一套只会两边规则漂移(见 `txSplits.ts` 里同样的取舍)。
 * 这里只做**前置提示**:让用户在选下拉框时就知道哪些账户会被拒,
 * 而不是提交后看到一个没头没尾的 400。
 */

/** 自动还款在账户上的配置(与后端 `user_account_projection` 对应)。 */
export type AutoRepayConfig = {
  enabled: boolean
  /** 扣款账户 sync_id。**不是名字** —— 改名会让按名定位静默失效。 */
  from_account_sync_id: string | null
  /** 上次已自动还款的账期 `YYYY-MM`,仅展示用。 */
  last_period: string | null
}

export const EMPTY_AUTOREPAY: AutoRepayConfig = {
  enabled: false,
  from_account_sync_id: null,
  last_period: null,
}

/** 这张卡能不能绑自动还款。
 *
 * 没有 `payment_due_day` 就没有「什么时候还」的锚点 —— 不渲染绑定 UI,
 * 否则用户填完提交才被服务端拒。
 */
export function canAutoRepay(account: {
  account_type?: string | null
  payment_due_day?: number | null
}): boolean {
  if ((account.account_type || '') !== 'credit_card') return false
  return typeof account.payment_due_day === 'number'
    && account.payment_due_day >= 1
    && account.payment_due_day <= 31
}

/** 候选扣款账户的筛选 —— **必须包含 hidden 账户**。
 *
 * `TransactionsPanel.tsx` 把 hidden 账户排除在所有选择器之外。若沿用那套
 * 逻辑,用户无法给已隐藏的卡绑还款 —— 而已有规则可能正指向一个后来被
 * 隐藏的账户,结果是**一条规则在跑,用户在界面上完全看不见**。
 *
 * 所以这里单独一份:排除卡自己、排除跨币种,但**保留 hidden**。
 */
export function repaySourceCandidates(
  accounts: Array<{
    id: string
    name: string
    currency?: string | null
    hidden?: boolean | null
  }>,
  card: { id: string; currency?: string | null },
): typeof accounts {
  const cardCurrency = (card.currency || '').toUpperCase()
  return accounts.filter((a) => {
    if (a.id === card.id) return false                       // 不能绑自己
    if ((a.currency || '').toUpperCase() !== cardCurrency) return false // 跨币种
    return true                                              // hidden 保留
  })
}

/** 扣款账户在候选里不存在时的可见问题。
 *
 * 服务端会拒,但那是个 400。这里给出**可读的**提示,并说明该怎么办 ——
 * 账户被删或被隐藏之后,一条还在跑的规则用户看不见。
 */
export function sourceAccountIssue(
  config: AutoRepayConfig,
  candidates: Array<{ id: string }>,
): 'ok' | 'missing' | 'currency-mismatch' {
  if (!config.enabled) return 'ok'
  if (!config.from_account_sync_id) return 'missing'
  return candidates.some((c) => c.id === config.from_account_sync_id)
    ? 'ok'
    : 'missing'
}