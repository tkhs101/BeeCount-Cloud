# 信用卡自动还款 —— 实施方案

> 状态：待批准。调研由 3 个 agent-team 并行完成，关键断言已 Lead 复核。
> 基线 commit `5f9ee2a`（alembic head `0021_tx_splits`）。

## 0. 用户已拍板的决策

| 问题 | 决策 |
|---|---|
| 还多少 | **按 `billing_day` 分账期算本期应还**（非终身累计） |
| 触发时机 | **定时：服务器跑**，到还款日自动还 |
| 钱不够 | **部分还款** |
| 手动已还过 | **识别并跳过** |
| 短月（账单日 31 遇 2 月） | **顺延到当月最后一天** |

## 1. 为什么这个功能不能照着现有代码「加个开关」

三条硬事实（均带 `文件:行号`，已复核）：

1. **`credit_limit` / `billing_day` / `payment_due_day` 是纯装饰**。全仓 73 处
   命中，全是 schema/CRUD/透传，**零处参与任何计算**。`credit_limit` 服务端连
   非负都没校验。绑定自动还款后它们从「展示字段」升级为「**驱动资金操作**的
   字段」—— 脏值直接变成错误扣款。

2. **信用卡「已用额度」= `-balance`（终身累计），不是本期账单**。
   `AccountDetailDialog.tsx:207-209` 的原注释自认：

   > 这是粗略估算 — 没考虑账单周期,只看终身累计。…**后续要精确版本应该按
   > billing_day 分账期算**。

   用户选了按账期算，所以**这段语义要整个换掉**，不是叠加。

3. **transfer 是天然载体且语义正确**：`_shared.py:486` 主查询
   `tx_type.in_(["income","expense"])` 把 transfer 排除在收支统计外，
   `:494-506` 用 `_transfer_legs` 单独对余额做 `−`/`+`。自动还款 = 一笔
   `from=储蓄卡 → to=信用卡` 的 transfer。载体已存在。

## 2. 核心算法：「本期应还」

这是整个方案的心脏。**四条规则，漏一条就静默算错**（无异常、无日志）。

### 2.1 账期窗口

```
本期账单日 = 今天之前(含)最近的 billing_day；短月顺延到当月末（已拍板）
窗口 = (上期账单日, 本期账单日]     ← 左开右闭
```

> 顺延先例可抄 `AccountDetailDialog.tsx:332-347` 的 `daysUntilDay()`。
> ⚠️ **但预算口径相反** —— `month_start_day` 在读端被**钳到 [1,28]**
> （`_shared.py:702-704`）。预算是「保守钳 28」，自动还款是「顺延月末」，
> 两套口径**不许互相引用**。

### 2.2 金额（按重要性排序的四条规则）

```
bill  = max(0, -(余额@账单日))              账单 = 账单日那一刻的欠款，欠款滚存
应还  = max(0, bill - 账单日之后的还款)       手动早还的部分要扣掉
```

| # | 规则 | 不遵守的后果 |
|---|---|---|
| 1 | 用 `amount` **原币**，禁用 `coalesce(native_amount, amount)` | 账户维度余额**故意**读原币（`_shared.py:455-456` 注释「账户维度仍 amount」），`credit_limit`/`initial_balance` 也是原币 |
| 2 | **必须 join 组合支付腿**（`load_tx_splits`） | 0021 起有腿时父交易 `account_sync_id` 被强制清空（`snapshot_mutator.py:439-446`），直接 WHERE 会漏掉腿金额 |
| 3 | **不过滤** `exclude_from_stats` / `exclude_from_budget` | 那是「不计统计」标记，不是「没刷这笔钱」。过滤掉就少还 |
| 4 | 账单 = `余额@账单日`（含往期滚存），不减窗口内还款 | 窗口内的还款是还**上期**账单。减了会少还 |

### 2.3 边界（这些不是理论问题，是必测项）

- `billing_day=31` 遇 2 月 → 顺延 28（已拍板）。**漂移不累积**（已推演 15 个月验证，每年 2 月短一个月，其余月份归位）
- `billing_day > payment_due_day`（账单日在还款日之后，如 25 号账单 / 5 号还款）→ 还款属于**下一个**账单周期
- 信用卡余额为**正**（溢缴）→ 无应还，跳过。`assetAggregation.ts:60-64` 有过溢缴导致的历史 bug
- 扣款账户就是这张卡本身 → 配置时拒
- 扣款账户与卡**不同币种** → 配置时拒（见 §3.3）

## 3. 架构决策

### 3.1 调度：复用现有 APScheduler，不新造

`services/backup/scheduler.py` 已有 `BackgroundScheduler` + `CronTrigger` +
时区解析 + `coalesce/max_instances` + DB 驱动 schedule 表 + 启动钩子。

**不复用 systemd timer 调 curl**：凭据过期后 cron 只看 exit code → **静默失败**，
用户以为在自动还款其实半年没还；补跑语义要自己实现；与 backup 的 schedule CRUD
重复造轮子。

**不复用「惰性计算」（打开页面时补算）**：让 GET `/read/*` 有副作用，直接撞
`SYNC_ARCHITECTURE.md:176-189` 的读路径定义，且挂载点分散在 4 个模块，漏一个
就数据不自洽 —— **静默的**。它的优点（停机自愈）用「启动时补跑」吸收即可。

**每天跑一次**（如 03:00），扫所有启用规则判断「今天是不是还款日」。
N 条规则共用 1 个 job，而不是每张卡一个 cron —— 短月顺延逻辑在代码里，不在 cron 表达式里。

### 3.2 写入：照抄 MCP 的 self-call，**不直接操作 DB**

`mcp/tools/write_tools.py:71-92` + `_mcp_internal_client.py:33-40` 是仓库里
唯一的「非用户 HTTP 触发，但完整走 `_commit_write` 全套」先例。

直接操作 DB 就要重实现 `write/_shared.py:532-678` 的：tx_id 生成、
`snapshot_mutator` 字段规范化、`projection.upsert_tx`、`AuditLog`、
`broadcast_to_ledger`。**漏任何一步都命中 CLAUDE.md 记的那几个静默丢失坑**
（税额被抹 / splits 被删 / nativeAmount 变 NULL），且不报错。

**必须用专用 `device_id`（如 `auto-repay`）** —— `sync/pull.py:78-79` 会过滤掉
**同设备**的变更。冒用 `web-console` 的话用户根本看不见自动还款。

### 3.3 幂等：`Idempotency-Key` **不够**，必须业务层判据

`SyncPushIdempotency`（`models.py:359-372`）唯一键 `(user_id, device_id,
idempotency_key)`，但 **TTL 只有 24 小时**（`_shared.py:636`）。关机两天后补跑
→ key 已 purge → 重复写入。**这是自动还款直接烧钱的场景，不能赌。**

两道防线：

1. **账期查重**（自愈，天然幂等）：生成前算一遍应还，`应还 == 0` 就跳过。
   用户手动还过了 → 已还 > 0 → 应还归零 → 跳过。**这一条就满足了「识别手动已还过」**。
2. **`last_repaid_period`**（恰好一次）：防止「自动还完、同期内又刷了一笔」导致同周期还两次。

### 3.4 部分还款：转出账户不得被扣成负数

用户拍板「部分还款」，但**必须加一条保护**：信用卡还款把储蓄卡扣成负数
= 新增一笔负债，风险翻倍。

```
可还 = min(应还, max(0, 扣款账户当前余额))
可还 <= 0 → 跳过
```

### 3.5 SQLite 下必须容错

`lock_ledger_for_materialize` 在 SQLite 上是 **no-op**（`concurrency.py:19-21`
只对 postgres 加锁）。SQLite 靠 `busy_timeout=5000`，超时直接抛
`OperationalError: database is locked`。**定时任务必须捕获并退避重试**。

### 3.6 多 worker 是定时炸弹（前置防线）

`get_scheduler()` 是**进程内单例**（`scheduler.py:43-48`），内存 jobstore 无跨进程锁。
当前部署单 worker 安全（`SELFHOST-RUNBOOK.md:78` 无 `--workers`），但
`database.py:16-18` 注释明说作者预期过多 worker。

**加 guard**：检测到多进程时禁用调度器，否则将来有人加 `--workers 4` 会重复扣钱 4 次。

## 4. 必须先修的三个现存 bug（不修会被自动还款放大）

| # | Bug | 位置 | 被放大的后果 |
|---|---|---|---|
| 1 | **转账两端同名歧义** → `projection.py:161-180` 按名反查，多命中/零命中一律 `None` | 若 `from_id` 落了 `to_id` 是 NULL → **只扣不加，钱凭空消失** | 自动还款每期都抽一次钱 |
| 2 | **服务端不校验 transfer 两端齐备**，`account_balance_delta` 有 `from or account_sync_id` fallback（`_shared.py:639`） | 单边 transfer 静默只扣不加 | 同上 |
| 3 | **前端转账按账户名定位**（`TransactionsPage.tsx:1463-1467` 建 `accountByName`） | 改名 → `_id` 静默变 `null` → 退回按名反查 → 撞 #1 | 配置「绑储蓄卡」后用户改个名，规则悬空 |

**修法**：自动还款路径**强制显式传 `from_account_id` / `to_account_id`**，
且在 mutator 层加 transfer 两端齐备校验（正面修 #1/#2）。

## 5. 数据模型

在 `user_account_projection` 上加 3 列（1:1 关系，不另开表）：

| 列 | 类型 | 说明 |
|---|---|---|
| `autorepay_enabled` | bool | 默认 false |
| `autorepay_from_account_sync_id` | str? | 扣款来源账户。**存 sync_id 不存名字**（见 §4 #3） |
| `autorepay_last_period` | str? | `YYYY-MM`，恰好一次守卫 |

配置校验（绑定时当场拒，不留到执行时失败）：

- `billing_day` 与 `payment_due_day` 必填（1-31）
- `credit_limit >= 0`（服务端目前**完全没校验**，前端才拦）
- 扣款账户存在、未隐藏或允许隐藏、**与卡同币种**、不是卡本身

## 6. 跨币种：显式禁止

理由三条：

1. transfer **不带** `native_amount` —— 前端切 transfer 清 `currency`（`TransactionsPanel.tsx:374-392`），MCP 对 transfer 跳过 `_build_currency_fields`（`write_tools.py:398-403` 注释「本阶段不支持跨币种转账」）
2. 转账两端共用**同一个 `amount`** → 跨币种会把 ¥10000 原样加到 JPY 卡上
3. 前端转账下拉**没按币种过滤** → **今天就能手工造出这种脏数据**

汇率基建（`ExchangeRateCache` 等）只服务「交易折本位币」和「净值折主币种」，
**没有任何一处支持转账两端换算** —— 不要指望它。

第一版直接禁，并写进 UI 提示。

## 7. 前端

- **入口**：不新增一级导航。挂在信用卡**账户详情弹窗**内 —— 还款日那一格下方
  加一行「→ 每月 25 日从「招行储蓄卡」自动还」。点击进绑定表单。
- **绑定表单**：复用 `AccountsPanel.tsx:1268-1310` 的信用卡灰盒子样式，
  加扣款账户下拉 + 启用开关（复用 `:1358-1383` 的 `role="switch"`）。
  **不填 `payment_due_day` 的卡不渲染这个区块** —— 没还款日就没有锚点。
- **候选账户用全量（含 hidden）**：`TransactionsPanel.tsx:306-309` 把 hidden
  账户排除在所有选择器外。若规则指向一个后来被隐藏的账户，**用户在界面上
  完全看不见这条规则在跑**。
- **失败可见**：卡面加一条细条「⚡ 25 日自动还款 · 来源：招行储蓄卡」；
  失败（余额不足/账户被删）时变红 + 「还款失败」，**不能静默不发生**。
- **i18n**：三份文件各加，`en.ts` 是 source of truth，`i18n.test.ts` 的 parity
  测试会兜住漏译。注意模板字符串 key 编译期和测试期都查不到。

## 8. 护栏

**后端**
- `tests/test_credit_repay_period.py` — 账期边界（15 个月连续推演 + 短月 +
  `billing_day > payment_due_day`）
- `tests/test_credit_repay_amount.py` — §2.2 四条规则**各一个测试**，任何一条
  改动立刻红。含「组合支付腿被计入」和「exclude 标记不被过滤」两个针对性用例
- `tests/test_credit_repay_idempotency.py` — 重跑/重启/同周期重复刷 不重复扣钱
- `tests/test_credit_repay_transfer_guard.py` — §4 的三个 bug 修复的回归

**前端**
- `apps/web/src/creditRepayParity.test.ts` — 配置字段两条提交路径不许只改一处
  （照抄 `txSplitsParity.test.ts` 的 AST 手法）
- `amountBasisGuard` 可能抓到账期金额运算 → 视情况登记 ALLOW 并写明理由

## 9. 阶段划分

| 阶段 | 内容 | 产出判据 |
|---|---|---|
| **0** | 修 §4 的三个现存 bug | 回归测试变红→转绿 |
| **1** | 账期 + 应还算法（**纯函数，零 IO**） | §2.2 四条规则各有测试 |
| **2** | 数据模型 + 配置 API + 校验 | alembic `0022`，配置往返测试 |
| **3** | 调度器 + 幂等 + 部分还款 + 手动跳过 | §8 后端四条护栏全绿 |
| **4** | 前端 UI + 失败态 | 前端测试 + 0 TS 错误 |
| **5** | 文档（`CLAUDE.md` 改动面表 + 坑表；runbook 验收项） | — |

## 10. 明确不做（第一版）

- 跨币种还款
- 最低还款额 / 分期
- 还款提醒通知（上游 mobile 有产品形态但只存本地、不同步，server 侧无先例）
- 多张卡的账单日对齐/合并还款

## 11. 待确认的风险

1. **`tzlocal` 不在 `requirements.txt`**（`scheduler.py:69` 是可选导入）。
   还款日是**本地语义**，必须显式配 `SCHEDULER_TIMEZONE`，否则 UTC 下会差一天。
2. **`sync_changes` 无 retention**（`main.py:365-375` 只观测不删）。自动还款
   量级 = 3 张卡 × 12 月 = 36 行/年，相对现状可忽略，但已列入观察。
3. **历史数据**：窗口右界取 `min(本期账单日, 今天)`。首次配置时若窗口内无交易，
   **不要动 `initial_balance`**，让用户自己录一笔负数期初（前端 `Math.max(0, -balance)`
   已按「负数=欠款」渲染）。