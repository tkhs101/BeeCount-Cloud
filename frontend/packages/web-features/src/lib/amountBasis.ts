/**
 * 金额口径的唯一落点 —— **不要在别处再写一遍**。
 *
 * ## 为什么有这个文件
 *
 * 前端有三处需要「按 server 的口径」处理交易金额，而 server 的口径是
 * SQL 里的 `coalesce(native_amount, amount)` 与消费税拆分。这里曾各自
 * 就地实现，结果同一个 bug 出现了**三次**：
 *
 *   1. `CategoryDetailDialog.aggregate` —— 税额没按比率折本位币
 *   2. `annual-report/data/aggregate.ts` —— 一律用原币，外币交易把 CNY 和 JPY 加在一起
 *   3. 分类详情不过滤 `exclude_from_stats`，笔数用原始数组长度
 *
 * 三次都是「单币种账本完全正常」，所以靠肉眼看不出来。
 *
 * ## 规则
 *
 * **聚合一律用 `baseAmount(t)`**，不要写 `t.amount`。
 * `t.amount` 是用户输入的原币（可能是 50 CNY），`baseAmount` 是折账本本位币
 * 后可与其它分类相加的金额（1000 JPY）。
 *
 * **需要展示税额拆分时用 `taxInBaseCurrency`**，且总额保持全额
 * （「税前 + 税 = 实付」不变式）。
 *
 * 单笔展示（详情页大数字、列表行）用 `t.amount` 配 `nativeAmount` 作为
 * 「≈ 本位币」副标 —— 那不是聚合，原币才是用户当时看到的数。
 */

type AmountBasis = {
  /** 原币金额(用户输入的数) */
  amount: number
  /** 折账本本位币的快照;NULL/缺失时回退 amount */
  nativeAmount?: number | null
}

/** 聚合口径:折账本本位币。与 server 的 `coalesce(native_amount, amount)` 逐字对应。 */
export function baseAmount(t: AmountBasis): number {
  return t.nativeAmount ?? t.amount
}

type TaxBasis = AmountBasis & {
  /** 消费税税额(原币)。NULL/缺失 = 无税 */
  taxAmount?: number | null
}

/**
 * 原币税额 → 折本位币税额。
 *
 * `tax / amount` 的比率与币种无关,按该笔自身的隐含汇率换算,保证税额和主
 * 金额走**同一个汇率**、不会各自漂移。与 server 的
 * `routers/read/_shared.tax_in_base_currency` 同一套公式。
 *
 * 结果夹在 `[0, base]` 内 —— 脏数据（税额大于金额、经 /sync/push 推入的
 * 垃圾数据）也保证「净额 ≥ 0」且「净额 + 税 ≤ 实付」，不变式不被打破。
 */
export function taxInBaseCurrency(
  taxAmount: number | null | undefined,
  rawAmount: number | null | undefined,
  baseAmountValue: number
): number {
  if (taxAmount == null) return 0
  const tax = Math.abs(Number(taxAmount) || 0)
  if (!(tax > 0)) return 0
  const base = Math.abs(Number(baseAmountValue) || 0)
  if (!(base > 0)) return 0
  const raw = Math.abs(Number(rawAmount) || 0)
  if (!(raw > 0)) return Math.min(tax, base)
  return Math.max(0, Math.min(base, base * (tax / raw)))
}

/** 一次算出「实付 / 税额 / 税前」三件套，避免各处重复推导。 */
export function splitTax(t: TaxBasis): {
  gross: number
  tax: number
  net: number
} {
  const gross = baseAmount(t)
  const tax = taxInBaseCurrency(t.taxAmount, t.amount, gross)
  return { gross, tax, net: gross - tax }
}