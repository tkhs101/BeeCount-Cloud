/** 组合支付拆分的「输入框 ↔ payload」转换(0021)。
 *
 * ## 为什么不放 `amountBasis.ts`
 *
 * `amountBasis` 的职责是**折账本本位币**的口径。拆分的核心约束是
 * 「各腿之和 == 父交易 amount」,这在**同一币种内**成立,不涉及折算 ——
 * 放进去会污染那个模块的单一职责,也会让 `amountBasisGuard` 的 ALLOW 判定
 * 变模糊。
 *
 * ## 刻意不做的事
 *
 * **不校验 `sum(legs) === amount`。** 与 `parseTaxAmount` 同样的取舍:
 * server 侧 `snapshot_mutator._normalize_splits` 已经强制了,前端再来一套
 * 就会出现「前后端规则不同步」—— 比如前端按显示精度四舍五入放过 4999.5,
 * server 拒了,用户只看到一个没头没尾的 400。
 *
 * 这里只负责「把用户填的东西变成能发出去的形状」:丢空行、数字归一。
 */

/** 表单里的一条腿。金额是 string —— 表单一切是 string(`forms.ts` 的约定)。 */
export type SplitForm = {
  accountId: string
  accountName: string
  amount: string
}

export type SplitPayload = {
  account_id: string
  account_name?: string | null
  amount: number
}

function toNumber(raw: string): number | null {
  const t = String(raw ?? '').trim()
  if (!t) return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

/** 丢弃没填完整的行;金额非法或 ≤0 的行也丢(让用户看到「少了一条」而不是报 400)。
 *
 *  返回 `null` 表示「没有有效腿」,调用方据此**不传 splits 字段** —— 传空数组
 *  在 update 语义里是「清除全部」,两者含义完全不同。
 */
export function parseSplits(rows: SplitForm[] | undefined | null): SplitPayload[] | null {
  if (!Array.isArray(rows)) return null
  const out: SplitPayload[] = []
  for (const r of rows) {
    const accountId = String(r?.accountId ?? '').trim()
    const amount = toNumber(r?.amount ?? '')
    if (!accountId || amount === null || amount <= 0) continue
    const name = String(r?.accountName ?? '').trim()
    out.push({
      account_id: accountId,
      account_name: name || null,
      amount,
    })
  }
  return out.length > 0 ? out : null
}

/** payload → 表单。编辑回显用,注意 `account_id` → `accountId` 的 camel 转换。 */
export function splitsToInput(
  legs: Array<{ account_id?: string | null; accountId?: string | null;
               account_name?: string | null; accountName?: string | null;
               amount?: number | null }> | undefined | null,
): SplitForm[] {
  if (!Array.isArray(legs) || legs.length === 0) return []
  return legs.map((l) => ({
    accountId: String(l?.account_id ?? l?.accountId ?? ''),
    accountName: String(l?.account_name ?? l?.accountName ?? ''),
    // 已是 number,直接 String;不要在这里格式化(会引入 locale 差异)
    amount: l?.amount == null ? '' : String(l.amount),
  }))
}

/** 还差多少没分配 —— 给 UI 提示用,不是校验(校验在 server)。
 *
 *  返回 `null` 表示「凑齐了」或「没有有效腿」,前端据此不显示提示。
 */
export function splitsRemainder(
  total: string | number | undefined | null,
  rows: SplitForm[] | undefined | null,
): number | null {
  const totalNum = toNumber(typeof total === 'number' ? String(total) : total ?? '')
  if (totalNum === null || totalNum <= 0) return null
  const legs = parseSplits(rows)
  if (!legs) return null
  const sum = legs.reduce((a, l) => a + l.amount, 0)
  const diff = totalNum - sum
  // 小于半分钱当 0 —— 用户看不见 ±0.001 的提示,只会被当成 bug
  return Math.abs(diff) < 0.005 ? null : Math.round(diff * 100) / 100
}

/** 这批腿是否构成「组合支付」—— 少于 2 条不算(一条腿就是普通交易)。 */
export function isSplitPayment(rows: SplitForm[] | undefined | null): boolean {
  return (parseSplits(rows)?.length ?? 0) >= 2
}
