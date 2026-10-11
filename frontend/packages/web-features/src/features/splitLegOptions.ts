/**
 * 拆分腿的账户候选 —— 纯逻辑,可直接测。
 *
 * ## 为什么要 pin 住当前账户
 *
 * `accounts` 已被调用方按**表单当前币种**过滤。编辑一笔 JPY 组合支付时
 * 把币种切成 CNY,两条腿的 `accountId` 还在 form state 里,而两个下拉
 * 都只显示占位符「账户」。用户以为腿丢了 → 重新选一遍。
 *
 * **若不重新选直接保存**,`parseSplits` 仍会用旧的 `accountId` 提交 ——
 * **界面显示空、实际提交旧值**。
 *
 * 主账户下拉早用 `accountOptionsWithPinned` 解决过同一个问题,这里照抄了
 * 那一半(另一半 = 标注「不在当前筛选内」)。
 */

/** 下拉里的一项。`pinned` = 这是腿**当前**的账户,只是不在候选里。 */
export interface SplitLegOption {
  id: string
  name: string
  /** 该项是为当前腿额外插进来的(原本不在 `rows` 里) */
  pinned: boolean
}

/** 腿当前持有的账户(前端表单态,不是库里字段)。 */
export interface SplitLegRef {
  accountId: string
  accountName: string
}

/**
 * 给一条腿算下拉候选:把腿当前的账户 pin 进去。
 *
 * @param rows 已按币种/可见性过滤的账户
 * @param leg 这条腿当前的账户
 */
export function legOptions(
  rows: ReadonlyArray<{ id: string; name: string }>,
  leg: SplitLegRef,
): SplitLegOption[] {
  const out: SplitLegOption[] = rows.map((a) => ({
    id: a.id,
    name: a.name,
    pinned: false,
  }))
  if (leg.accountId && !out.some((r) => r.id === leg.accountId)) {
    // **unshift**:钉住的当前值放最上面 —— 用户打开下拉第一眼看到的是
    // 自己原来选的那个,而不是一屏陌生账户。
    out.unshift({
      id: leg.accountId,
      name: leg.accountName || leg.accountId,
      pinned: true,
    })
  }
  return out
}