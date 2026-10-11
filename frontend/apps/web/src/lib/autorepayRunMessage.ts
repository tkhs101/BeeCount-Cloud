/**
 * 「立即试跑一次」的响应 -> 该给用户看哪句提示。
 *
 * ## 为什么需要它
 *
 * 端点 `POST .../autorepay/run` 现在返回 `AutoRepayRunResult`,**带 `status`**。
 * 在那之前界面只有一句「跑完了,去交易列表看结果」——
 * 用户点完按钮**不知道到底还了没有钱、还是被跳过了**,而这恰恰是
 * 「用户点手动触发,接口什么都不说」那个问题想解决的东西。
 *
 * ## 为什么是纯函数、单独一个模块
 *
 * CLAUDE.md 记着「前端 45% 的护栏是假的」那一族:字符串存在性检查证明不了
 * 「代码被执行到了」。把这段映射提到模块级,`autorepayRunMessage.test.ts`
 * 就能**真跑**每一种 status,而不是 `expect(src).toContain(...)`。
 * 组件里只留一次调用。
 *
 * ## 契约:返回 i18n **key**,不返回文案
 *
 * 组件负责 `t(key)`。这样这个模块不需要 import 任何 i18n 运行时,
 * 测试也不必关心文案是什么 —— 只关心「哪种 status 落到哪个 key」。
 */

/** `repay_one` 返回的 10 个 status(见 `src/services/credit_card/repay.py`)。 */
export type AutorepayRunStatus =
  | 'done'
  | 'failed'
  | 'skipped_already_repaid'
  | 'skipped_disabled'
  | 'skipped_invalid_config'
  | 'skipped_locked'
  | 'skipped_no_funds'
  | 'skipped_no_schedule'
  | 'skipped_not_due'
  | 'skipped_zero_outstanding'

export type MessageKey =
  | 'detail.autoRepay.runDone'
  | 'detail.autoRepay.runNoFunds'
  | 'detail.autoRepay.runSkipped'
  | 'detail.autoRepay.runNotEnabled'
  | 'detail.autoRepay.runFailed'
  | 'detail.autoRepay.runOk'

/**
 * 全部 status 的映射。
 *
 * ⚠️ **未知 status 一律回落到 `runOk`,不猜。**
 *
 * 为什么不用 `runFailed`:后端把 `failed` 映射成 **502**、
 * `skipped_invalid_config` 映射成 **400** —— 这两个**根本不会带着 200
 * 走到界面**。所以「看到不认识的 status」更可能是**后端加了新状态 /
 * 连老版本都没有这个字段**,而不是失败 —— 回落到中性文案比谎报失败安全。
 *
 * 这是「探针的否定结果不能当证据」在前端的对应:
 * **「我不认识这个值」≠「它是坏值」**。
 *
 * 📌 **故意用 `Record<AutorepayRunStatus, MessageKey>` 而不是 `switch`**:
 * 这样后端 `repay_one` 加了第 11 个 status、而这里没补映射时,
 * **`tsc -b` 直接编译不过** —— 拿到一道**编译期**的完整性护栏,
 * 比运行时的 `default` 兜底强得多(兜底只是不崩,不会提醒你)。
 *
 * ⚠️ `tsc -b` 是前端**第二道门**,`vitest` 抓不到 —— 记得两道都跑。
 */
const BY_STATUS: Record<AutorepayRunStatus, MessageKey> = {
  // 唯一「真扣了钱」的那一个 —— 值得单独说一句
  done: 'detail.autoRepay.runDone',

  // 钱不够:这是最常见的「点了没反应」的原因,值得单独提示
  skipped_no_funds: 'detail.autoRepay.runNoFunds',

  // 没启用 / 配置坏了:一次配好就不用一直点,提示要指向「去设置」
  skipped_disabled: 'detail.autoRepay.runNotEnabled',
  skipped_invalid_config: 'detail.autoRepay.runNotEnabled',

  // 后端理论上不把这两个送到 200(见上),但真遇上了别装作成功
  failed: 'detail.autoRepay.runFailed',

  // 其余都是「判断链正常走完、这一期不需要扣钱」:
  // 已还过 / 非还款日 / 无欠款 / 没有排期 / 被别处抢先还了
  skipped_already_repaid: 'detail.autoRepay.runSkipped',
  skipped_locked: 'detail.autoRepay.runSkipped',
  skipped_no_schedule: 'detail.autoRepay.runSkipped',
  skipped_not_due: 'detail.autoRepay.runSkipped',
  skipped_zero_outstanding: 'detail.autoRepay.runSkipped',
}

/**
 * status -> i18n key。**不认识的值回落到 `runOk`**(理由见 `BY_STATUS`)。
 *
 * 参数放宽到 `string | null | undefined`:真实响应是 `unknown` 形状,
 * 而 `typeof` 那道判断就是**唯一的入口防护** —— 别为了「类型好看」
 * 把它写成 `status as AutorepayRunStatus` 直通。
 */
export function autorepayRunMessage(
  status: string | null | undefined,
): MessageKey {
  if (typeof status !== 'string') return 'detail.autoRepay.runOk'
  // `??` 而不是直接索引:后端加了新状态而这里还没补时,映射表里没有这个键
  return BY_STATUS[status as AutorepayRunStatus] ?? 'detail.autoRepay.runOk'
}