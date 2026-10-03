# Agent Note: 账期金额口径与执行时序（proposed）

Status: proposed

## Problem

用户要求「信用卡在还款日自动还款」，选定**按账单日分账期算本期应还**，而不是
按终身累计欠款；触发方式选**服务器定时**。

现有代码里没有「账期」这个概念。信用卡的「已用额度」是
`max(0, -balance)`（`frontend/apps/web/src/components/dialogs/AccountDetailDialog.tsx:207-209`），
即**终身累计**。该处注释自己写明：

> 这是粗略估算 — 没考虑账单周期,只看终身累计。…**后续要精确版本应该按
> billing_day 分账期算**。

所以这不是给现有逻辑加开关，而是要**新增一套金额口径**。难点在于这条口径有四条
容易漏的规则，任何一条漏掉都**静默算错** —— 没有异常、没有日志，只表现为还少了
或多还了钱。

触发时序上另有三个约束：调度复用面、幂等强度、跨币种能力边界。已在
[转账两端守卫](../../implemented/bug-fix/2026-10-04-transfer-pair-guard.md)
里落地的部分不再重复。

## Proposal

### 账期窗口

```
本期账单日 = 今天之前(含)最近的 billing_day；短月顺延到当月月末
窗口       = (上期账单日, 本期账单日]        左开右闭
```

短月顺延（账单日 31 遇 2 月 → 28）已有前端先例可抄：
`AccountDetailDialog.tsx:332-347` 的 `daysUntilDay()`。

**但预算的月份起始日口径相反** —— `month_start_day` 在读端被钳到 `[1,28]`
（`src/routers/read/_shared.py:702-704`）。预算是「保守钳 28」，自动还款是
「顺延月末」，两套口径**禁止互相引用**。

已用固定时间原点连续推演 15 个月验证**漂移不累积**：每年 2 月短一个月，
其余月份都归位。

### 金额公式

```
bill  = max(0, -(余额@账单日))           账单 = 账单日那一刻的欠款，欠款滚存
应还  = max(0, bill - 账单日之后的还款)    手动早还的部分要扣掉
```

用「余额@账单日」而不是「窗口内消费」，是为了让**上期未还的部分滚入本期账单**，
与真实信用卡一致。

### 四条必须同时成立的规则

| # | 规则 | 依据 | 违反后果 |
|---|---|---|---|
| 1 | 用 `amount` **原币**，禁止 `coalesce(native_amount, amount)` | 账户维度余额**故意**读原币，`src/routers/read/_shared.py:455-456` 注释「账户维度仍 amount」；`credit_limit` / `initial_balance` 也是原币 | 多币种账本里把折算值与原币额度相减 |
| 2 | **必须 join 组合支付腿**（`load_tx_splits`，`_shared.py:525-555`） | 0021 起有腿时父交易 `account_sync_id` 被 mutator 强制清空（`src/snapshot_mutator.py:439-446`） | 组合支付刷的金额**整段漏算**，少还 |
| 3 | **不过滤** `exclude_from_stats` / `exclude_from_budget` | 那两个字段的语义是「不计收支统计 / 不计预算用量」，不是「没刷这笔钱」 | 把用户标记为不计统计的刷款漏掉，少还 |
| 4 | 账单金额**不减**窗口内的还款 | 窗口内的还款还的是**上期**账单 | 每月少还上期滚存的部分，且**逐月累积** |

第 4 条最隐蔽：算错不会立刻暴露，只是欠款慢慢变大。

### 幂等：`Idempotency-Key` 不够

`SyncPushIdempotency`（`models.py:359-372`）唯一键是
`(user_id, device_id, idempotency_key)`，但 **TTL 只有 24 小时**
（`write/_shared.py:636`）。关机两天后补跑 → key 已 purge → **重复扣钱**。
这是直接烧钱的场景，不能赌 HTTP 幂等键。

两道防线：

1. **账期查重（自愈）**：生成前先算一遍应还，`应还 == 0` 就跳过。
   用户手动还过了 → 账单日之后的还款 > 0 → 应还归零 → **自动跳过**。
   这一条本身就满足「识别手动已还过」的需求，不需要额外的检测逻辑。
2. **`autorepay_last_period`（`YYYY-MM`）恰好一次守卫**：防止「自动还完之后、
   同一账期内又刷了一笔」导致同周期还两次。

### 跨币种：显式禁止

transfer 不带 `native_amount`（前端切 transfer 时清 `currency`，
`TransactionsPanel.tsx:374-392`；MCP 对 transfer 跳过 `_build_currency_fields`，
`write_tools.py:398-403` 注释「本阶段不支持跨币种转账」），且转账两端共用**同一个
`amount`** —— 跨币种会把 ¥10000 原样加到 JPY 卡上。

汇率基建（`ExchangeRateCache` / `UserExchangeRateProjection`）只服务「交易折本位币」
与「净值折主币种」，**没有任何一处支持转账两端换算**，不要指望它。

第一版在配置时当场拒绝，并写进 UI 提示。

### 边界

- 信用卡余额为正（溢缴）→ 无应还，跳过。`assetAggregation.ts:60-64` 记录过
  溢缴账户导致的历史 bug。
- 扣款账户余额不足 → 部分还款，且**不得把扣款账户扣成负数**
  （信用卡还款把储蓄卡扣成负数 = 新增一笔负债，风险翻倍）。
- `billing_day > payment_due_day`（账单日在还款日之后）→ 该次还款属于**下一个**
  账单周期。
- **历史数据**：窗口右界取 `min(本期账单日, 今天)`。首次配置时若窗口内查不到
  任何交易，**不动 `initial_balance`**，由用户自己录一笔负数期初 ——
  前端 `max(0, -balance)` 已按「负数 = 欠款」渲染。

### SQLite 容错与多 worker 防线

- `lock_ledger_for_materialize` 在 SQLite 上是 **no-op**（`concurrency.py:19-21`
  只对 postgres 加锁）。SQLite 靠 `busy_timeout=5000`，超时直接抛
  `OperationalError: database is locked`。**任务必须捕获并退避重试**。
- `get_scheduler()` 是**进程内单例**（`backup/scheduler.py:43-48`），内存 jobstore
  无跨进程锁。当前单 worker 部署安全，但 `database.py:16-18` 注释明说作者预期
  过多 worker。加启动 guard：检测到多进程时禁用调度器。

## Alternatives considered

**A. 沿用终身累计 `max(0, -balance)`。**
理由：与现有 UI 显示的「当前欠款」完全一致，不需要新增账期概念，实现量小一个数量级。

否决理由：用户明确选了按账期。终身累计下，上期未还 + 本期消费会混成一个数，
用户无法核对「为什么这个月比上个月还得多」；对分期账单更是直接算错。

**B. 只算窗口内新增消费，不含上期滚存。**
理由：语义最简单 —— 「这个账单周期的消费就是这笔账单」。

否决理由：上期没还清时，本期账单会比真实金额小，欠款被「吃掉」。用户会以为还清了，
实际卡上还有欠款 —— 这是比多算更危险的错误方向。

**C. systemd timer 调内部 HTTP API（触发时机）。**
理由：与 app 生命周期解耦 —— app 挂了定时器还在。

否决理由：把一个需要 DB 表 + UI 配置的功能降级成运维自己维护凭据的 curl 脚本。
凭据过期后 cron **只看 exit code → 静默失败**，用户以为在自动还款其实半年没还。
补跑语义要自己实现，且与 `admin_backup.py` 已有的 schedule CRUD 重复造轮子。

**D. 惰性计算 —— 打开页面时才补算应还未还的。**
理由：**零进程模型风险**，而且自愈 —— 停机一个月后首次打开自动补齐。

否决理由：让 GET `/read/*` 有副作用，直接撞 `docs/SYNC_ARCHITECTURE.md:176-189`
明确定义的「读路径不经过 snapshot / sync_changes」。而且这个破坏是**静默且分散**的：
挂载点分散在 `read/ledgers.py` / `read/workspace.py` / `read/summary.py` /
`mcp/tools/read_tools.py`，漏一个就数据不自洽。浏览器 prefetch / 反代重试 /
连点都会放大重复生成，而 GET 的幂等假设在网关层根本没有保证。
它的优点（自愈）已由「启动时补跑」吸收，**收益相同，零契约破坏**。

**E. 复用现有 `Idempotency-Key` + 稳定 key（`auto-repay:{rule}:{YYYY-MM}`）。**
理由：不新增字段，机制现成。

否决理由：24h TTL 在「关机两天后补跑」场景下必然失效。key 设计得再稳定，
过期就是过期。可以作为短时间重试的第二道保险，但不能作为唯一保障。

## Acceptance criteria

- [ ] `bill` / `应还` 两条公式在纯函数层实现，零 IO
- [ ] 四条规则**各有一个独立测试** —— 任何一条被改动立刻红
- [ ] 组合支付腿被计入的针对性用例
- [ ] `exclude_from_*` 不被过滤的针对性用例
- [ ] 账期边界测试：连续 15 个月推演 + 短月 + `billing_day > payment_due_day`
- [ ] 重跑 / 服务重启 / 同账期重复触发，都**不重复扣钱**
- [ ] 用户手动还过 → 自动跳过
- [ ] 扣款账户余额不足 → 部分还款，且扣款账户**不被扣成负数**
- [ ] 跨币种绑定在配置时即被拒绝
- [ ] SQLite `database is locked` 时退避重试，不丢任务
- [ ] 多进程部署时调度器被禁用并有日志

## Risks

1. **规则 4 最难被测出来** —— 表现为欠款逐月变大，不会报错。需要专门的滚存测试。
2. **历史数据与新口径不一致** —— 用户可能在启用前已按终身累计理解自己的欠款，
   切换后「本期应还」数字会变。需要在 UI 上说清切换的是口径不是账。
3. **`billing_day` / `payment_due_day` / `credit_limit` 目前是纯装饰字段**，
   服务端零校验（`credit_limit` 连非负都没校验，前端才拦）。它们将从展示字段
   升级为**驱动资金操作**的字段，脏值直接变成错误扣款。必须补配置校验。
4. **`tzlocal` 不在 `requirements.txt`**（`backup/scheduler.py:69` 是可选导入）。
   还款日是**本地语义**，未显式配 `SCHEDULER_TIMEZONE` 时会按 UTC 算，**差一整天**。
5. **`sync_changes` 无 retention**（`main.py:365-375` 只在启动时观测行数，不删）。
   自动还款量级 = 3 张卡 × 12 月 = 36 行/年，相对现状（25k 行/月）可忽略，
   但已列入观察范围。