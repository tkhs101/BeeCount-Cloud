/**
 * 消费税归属的分类名 —— 前端侧的单一来源。
 *
 * 与服务端 `config.tax_category_name`(`TAX_CATEGORY_NAME` env)保持一致。
 * Web 端拿不到那个 env,只能对默认值「税与保险」;改了 env 的部署会退化成
 * 「税额扇区不被钉住」,数字仍然正确,只是有可能落进「其他」。这一条写在
 * `docs/aegis/plans/2026-10-03-selfhost-tax-feature-fork.md` 的已知缺口里。
 *
 * 用法:凡是需要「这是消费税分类」的地方都从这里取,不要各处硬编码字符串 ——
 * 改一处就要全仓搜一遍,漏了就是两处对不上。
 */
export const TAX_CATEGORY_NAME = '税与保险'