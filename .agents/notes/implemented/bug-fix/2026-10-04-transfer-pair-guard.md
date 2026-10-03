# Agent Note: 转账两端守卫

Status: implemented

## Problem

自动还款每期产生一笔「扣款账户 → 信用卡」的 transfer。调研时发现**三个既有缺陷**
会被它从「偶尔触发」放大成「每期触发」：

1. **`projection.py:280-290` 三组账户字段各自独立按名反查**。只带名不带 id 时，
   `from_account_name` 命中、`to_account_name` 没命中（账户改名、同名多账户）
   完全可能发生 → `from_account_sync_id` 有值而 `to_account_sync_id` 是 NULL。
   余额影响是 `_transfer_legs(from)` 减、`_transfer_legs(to)` 加
   （`read/_shared.py:494-506`），**只扣不加 —— 钱凭空消失，无异常、无日志、余额永久漂移**。
2. **mutator 不校验 transfer 两端齐备**（`snapshot_mutator.py` 只原样 copy
   from/to 字段）。单边 transfer 静默通过。
3. **前端转账按账户名定位**（`TransactionsPage.tsx:1463-1467` 建
   `accountByName` 小写映射）。用户给账户改名 → `_id` 静默变 `null` → 退回按名
   反查 → 撞上 #1。

第 3 条是前端的定位方式问题，自动还款路径会直接绕开它（强制传 id），
但既有的手工转账仍会踩。把它记在这里是因为它解释了 #1 为什么会真实发生。

## Decision

在两个层面各加一道防线。

### 防线一：mutator 要求两端字段齐备

`snapshot_mutator.create_transaction` 在 `tx_type == "transfer"` 时要求
`from_account_id`/`from_account_name` 与 `to_account_id`/`to_account_name`
**两侧都非空**，否则 `write validation failed`（400）。

名字或 id 任一存在即可 —— 后续按名反查会补 `sync_id`。这条挡的是「调用方
根本没给对端」，是最常见也最容易修的情形。

### 防线二：projection 的按名反查改为「全有或全无」

`snapshot_mutator` 挡得住「字段没给」，挡不住「字段给了但按名反查失败」。
所以 `projection.upsert_tx` 里，先各自解析，transfer 若**恰有一端**解析不出，
**把另一端也一并撤回**（两端的 `_sync_id` 都置 NULL），并打 `logger.warning`。

取舍：宁可**整笔漏算**，不可**单边扣钱**。漏算的后果是余额少算一笔，
后续 `data_cleanup` 的孤儿扫描 / 对账能兜住；单边扣钱的后果是钱从一个账户
消失却没进另一个账户，**没有任何机制会发现**。

普通支出 / 收入只有**一个**账户字段，「全有或全无」约束**只对 transfer 生效**
 —— 支出解析不出本来就该留 NULL（#41 的「宁缺勿错」语义），不能被误伤。

### 刻意不拦「转出 == 转入」

自转账是退化 no-op：`_transfer_legs` 对同一账户减 800 又加 800，净 0，
只是 `count` 虚增 2。它不是丢钱的 bug，既有测试
`test_account_balance_paths.py` 已把它钉为边界用例（「应净 0」）。

最初这里写了「自转账应被拒绝」，被那个测试顶了回来。复盘：在交易层拦它
属于**越界** —— 真正该拦的是「自动还款把扣款账户配成这张卡自己」，
那是**配置错误**，拦在自动还款的配置校验层，不摊到所有交易写入上。

### 护栏

`tests/test_transfer_pair_guard.py`（13 条）。反向验证：

- 移除防线一 → **4 条**变红
- 移除防线二 → **1 条**变红

其中 `test_projection_drops_one_sided_name_resolution` 带一条**前置自检**：
先断言「只给一端名字时能解析出 `acc-from`」，再断言两端最终都是 NULL。

这条自检是必要的:第一版测试因为 `UserAccountProjection` 的 `user_id` 外键
没建成功，**两端都没解析出来**，于是「都留 NULL」自然成立 —— 测试**假通过**。
没有自检的话，防线二看起来是被测住了，其实根本没走到那条分支。

## Alternatives considered

**A. 只在 projection 层做「全有或全无」，不加 mutator 校验。**
理由：改动面更小，一处代码解决单边场景。

否决理由：mutator 的校验挡住的是「调用方根本没给对端」，这个比「给了名字
但查不到」常见得多，也更容易被上游解释。不挡的话，400 之外还会有一批
带 NULL 的 transfer 落库，等到余额对不上才发现。

**B. 把「半边」当数据污染处理 —— 照常落库但打 warning，让对账兜。**
理由：不丢数据，历史数据保持原样。

否决理由：半边 transfer 的后果不是「数据脏」而是「余额错」，且**永久漂移**
（每次重算都错一次）。`data_cleanup` 的孤儿扫描扫的是「引用了已删实体」，
扫不到「引用了不存在的实体」。留在库里只会持续产生错误余额。

**C. 顺着 `account_balance_delta` 的 `from or account_sync_id` fallback 修。**
理由：逐笔路径也可以改成对称的。

否决理由：fallback 本身有别的用途（expense 的单账户路径）。
对称性应该在**写入侧保证数据合法**，而不是在每一处读取侧兜底 ——
读取侧有 SQL 聚合和逐笔两条路径，兜两次仍会漏第三处。

## Consequences

**收益**

- 三个既有缺陷在**被自动还款放大之前**挡住。
- 新增的 `write validation failed` 错误信息直接说明后果（"money vanishes
  silently"），调用方不必去猜。

**代价**

- mutator 新增校验：**依赖单边 transfer 的存量客户端会开始 400**。本 fork
  不使用官方 App，Web 端 UI 已强制两端必填（`TransactionsPage.tsx:1447-1457`），
  实际影响面为零；但 `sync/push` 路径若曾收到过单边 transfer，会被这条校验拒绝。
- 「全有或全无」改变了部分历史数据的余额表现：改过名的账户上的历史 transfer
  从「一端生效」变成「整笔漏算」。这是有意的取舍（漏算可被对账发现，
  单边扣钱不能），但确实改变了行为。

**验证缺口**

- 同名歧义在**真实数据**下的分布没有统计。Web 端 UI 禁重名
  （`AccountsPage.tsx:226-234`），但 MCP `create_account` **不禁** ——
  这条路径仍能造出同名账户，落进防线二被整笔撤回。
- `test_balance_delta_fallback_is_asymmetric` 记录了逐笔路径 fallback 的
  既有行为（不对称）。加上防线一后，单边 transfer 已经到不了那里；该测试
  的作用是提醒后来人：放宽 mutator 校验时要顺带检查这里。