/**
 * 消费税归属的分类名 —— 前端侧的单一来源。
 *
 * ## 217 轮:后端已经把值下发,这里不再是硬编码
 *
 * `/read/workspace/analytics` 的响应多了 `tax_category_name`
 * (`WorkspaceAnalyticsOut` 里加的),它就是后端 `TAX_CATEGORY_NAME` 的
 * **原值**。所以:
 *
 * - 拿得到响应 -> 用 `taxCategoryName(analytics.tax_category_name)`
 * - 拿不到(旧后端 / 请求失败) -> 退回 `DEFAULT_TAX_CATEGORY_NAME`
 *
 * ⚠️ **比较时逐字符比**,后端**不做**任何大小写或别名归一。
 *
 * ## 为什么要改(原来那句「Web 端拿不到那个 env」已经不成立了)
 *
 * 以前这里是写死的 `'税与保险'`,而**后端三处**都读同一个 settings 值
 * (读端归集、MCP、默认分类播种)。所以改了 env 之后:
 *
 * - 后端把税额归到新名字 ✅
 * - 前端**找不到那个分类** -> 税额从扇区**掉进「其他」** ❌
 *   (统计数字本身仍然正确,因为后端用的是同一个值)
 *
 * 一个**用户可见**的配置项只对后端生效 —— 那就是半个功能。
 *
 * 用法:凡是需要「这是消费税分类」的地方都从这里取,不要各处硬编码字符串 ——
 * 改一处就要全仓搜一遍,漏了就是两处对不上。
 */

/** 兜底值:后端没给(或旧版本后端)时用。**别**把它当权威。 */
export const DEFAULT_TAX_CATEGORY_NAME = '税与保险'

/**
 * 选一个:后端下发的优先,空/缺省才退回默认值。
 *
 * ⚠️ 参数叫 `fromBackend` 而不是裸值 —— 让调用点读起来
 * 「这个值是后端给的」,不会有人以为它又是那个硬编码常量。
 */
export function taxCategoryName(fromBackend?: string | null): string {
  const v = (fromBackend ?? '').trim()
  return v || DEFAULT_TAX_CATEGORY_NAME
}