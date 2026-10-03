# BeeCount Cloud —— AI 助手/新人阅读指南

本文件给 AI 编码助手(Claude Code / Copilot 等)和第一次进本仓的人类开发者
一个快速定位,告诉你**改什么在哪里改**、**绕不过去的契约**、**哪类修改
最容易出 bug**。

## 改代码之前必读

**如果要改跟 mobile ↔ server 或 web ↔ server 同步相关的任何逻辑**,
先读:

### [docs/SYNC_ARCHITECTURE.md](./docs/SYNC_ARCHITECTURE.md)

里面有:
- 核心路由目录与职责(`routers/sync/` `routers/write/` `routers/read/` +
  `sync_applier.py` + `ws.py`)
- 4 条核心数据流(mobile→web / web→mobile / mobile 首次同步 / web 读)
- **契约部分**(最容易踩坑):
  - user-global vs ledger-scoped 实体的 `ledger_id` 通道区分
  - LWW 冲突决胜规则
  - rename cascade 在 push / write 两条路径上的实现
  - 增量 push 的 merge 字段语义
  - change_id 单调性
  - `lock_ledger_for_materialize` 锁粒度
- debug 清单 + 修改前自检清单

**这块代码历史上出过几次难复现的 bug**(2026-04 修过两次 ledger_id
误用 + budget import path 错误),根因都是"有隐式契约但没在契约点强制"。
动之前花 5 分钟读完 `SYNC_ARCHITECTURE.md` 省几小时 debug。

## 代码约定(server 端)

### 路由组织

每个 HTTP API 组是 `src/routers/<group>/` 包形式,结构:

```
<group>/
  __init__.py     聚合 router,main.py 的 import 不变
  _shared.py      共享 imports / helpers / 常量 / router 实例
                  __all__ 显式列表(wildcard 默认不带下划线名字)
  <entity>.py     按资源拆分的 endpoint 文件,3 个 HTTP 方法(POST/PATCH/DELETE)
                  或按逻辑分组的 GET
```

修改某个 endpoint → 进对应 entity 文件,修改跨 endpoint 的共享逻辑 →
改 `_shared.py`。不要把业务加回到 `__init__.py`。

### 分 snapshot / projection / event log

同步层有三种存储形态,**不要混用**:

- `sync_changes`(事件流):append-only,`change_id` 自增,pull 增量同步
  的源头。永远只插入,从不 UPDATE。
- `read_*_projection`(5 张 denorm 表):读路径唯一权威源。LWW / rename
  cascade 落盘到这里。
- `ledger_snapshot`(JSON blob):方案 B 之后基本不写,`/sync/full` 按需
  从 projection 懒构建。**新代码不要再主动写 ledger_snapshot。**

### 新增 entity

如果要加一种新的 sync 实体(比如 "recurring_transaction"):

1. 新建 `read_*_projection` 表 + alembic migration
2. `src/projection.py` 加 upsert_* / delete_* / rename_cascade_* (如需)
3. `src/sync_applier.py` 登记 `_MERGE_SPECS` + `_UPSERT_DISPATCH` +
   `_DELETE_DISPATCH` 三张表
4. `src/routers/write/<entity>.py` 加 POST/PATCH/DELETE endpoints
5. `src/routers/read/ledgers.py` 或 `workspace.py` 加读端点
6. 补 pytest(`tests/test_projection_consistency.py` 已有
   mixed-entities 模板可参考)

### 测试

- `pytest tests/` 全过才能合代码
- 多账本场景至少有一个测试覆盖(一个 sync_id 在多个账本的 projection 里
  同时出现,dedup 行为)
- 添加新 entity 必须添加一条 `test_mobile_push_<entity>_partial_update_keeps_existing_fields`
  风格的 merge 契约测试 —— 防 2026-04 踩过的"漏 merge 某字段"类 bug

### 日志

- 同步决策点用 `logger.info("sync.push.accept entity=...")` 结构化日志
- 错误 path 用 `logger.exception` 带上 entity_type / action / sync_id /
  payload,方便 /sync/push 500 时定位到具体哪条 change 炸的
- 服务端有 admin 日志面板(web header 的 📜 按钮,admin 可见),默认筛
  ERROR 级别

## Frontend

Mobile 端(Flutter)和 Web 端(React)各自有仓,各自有 CLAUDE.md:

- Mobile: `../BeeCount/CLAUDE.md`
- Web: 前端源码在 `frontend/apps/web/`,README 见 `frontend/README.md`
  (如有)

跟服务端同步相关的 mobile 契约(`ChangeTracker.recordUserGlobalChange` /
`recordLedgerChange`)在 mobile 仓 CLAUDE.md 里。Server 端的契约在上面
链的 `docs/SYNC_ARCHITECTURE.md` 里。

## 工具

- `python -m pytest tests/` 跑全部服务端测试
- 本地 dev server:`uvicorn src.main:app --reload`
- 本地 DB 默认 SQLite:`beecount.db`(仓根),可用 `sqlite3` CLI 直接查
- 生产部署见 `docs/DEPLOYMENT.md`

---

# ⚠️ fork 特有改动(非上游内容)

本仓是 `tkhs101/BeeCount-Cloud`,从 `TNT-Likely/BeeCount-Cloud` 的
`3d9f64b`(tag `1.6.7`)分叉。**部署形态是自托管云端 + Web/PWA,不使用官方
App**。改这个仓之前先读
[`docs/aegis/plans/2026-10-03-selfhost-tax-feature-fork.md`](docs/aegis/plans/2026-10-03-selfhost-tax-feature-fork.md),
里面有全部决策依据、验证证据和踩过的坑。

## 新增:消费税税额(`tax_amount`,alembic `0020`)

一笔支出可记录税额。**`amount` 语义不变,仍是实付总额**;税额是叠加维度。

| 要改的地方 | 文件 |
|---|---|
| 模型列 | `src/models.py` `ReadTxProjection.tax_amount` |
| 写入校验 | `src/snapshot_mutator.py` `_normalize_tax_amount` |
| projection 落库 | `src/projection.py` `upsert_tx` |
| **反向桥(最易漏)** | `src/routers/write/_shared.py` `_projection_row_to_tx_dict` |
| merge 契约 | `src/sync_applier.py` `_LEDGER_MERGE_SPECS` |
| 统计切片 | `src/routers/read/_shared.py` `tax_in_base_currency`(MCP 与 Web 共用) |
| 前端口径 | `frontend/packages/web-features/src/lib/amountBasis.ts` |
| MCP 工具 | `create_transaction` / `update_transaction` / `create_transactions` / `get_analytics_summary` |

**三条容易踩的**(`CLAUDE.md` 上游那节讲的是移动端同步契约,这几个是本 fork 独有的):

1. **`_projection_row_to_tx_dict` 漏了税额** → Web PATCH 更新会**静默抹掉它
   且不报错**。上游 `nativeAmount` 当年就踩过同一个坑,注释还在 `:895-899`。
2. **金额运算不要写 `t.amount`** → 那是原币,多币种账本会把 CNY 和 JPY 直接
   相加。聚合一律走 `baseAmount()`。已有护栏
   `frontend/apps/web/src/amountBasisGuard.test.ts` 会拦。
3. **hook 别插进函数体** → 语法完全合法但永不可达,`tsc` 和 build 都抓不到。
   护栏 `frontend/apps/web/src/hookPlacement.test.ts`。

## fork 修复:管理面板「备份」

`POST /admin/backups/create` 原先去找一条 `entity_type == "ledger_snapshot"` 的
SyncChange,找不到就 404。方案 B 之后没有任何代码再写那种行,所以**对新账本必然
失败**。已改成 `snapshot_builder.build(db, ledger)` 现场构建。

其余两条备份路径(定时 rclone 的 `VACUUM INTO`、`scripts/backup_sqlite.sh`)不
经过 snapshot,一直正常。

## 新增:MCP 附件

`attach_receipt` / `create_transaction_with_receipt`。REST 层
(`/attachments/upload`)是上游就有的,本 fork 只补了 MCP 接线。

## CI 的实际状态（手动触发,别被吓到）

`ci.yml` 是 `workflow_dispatch` **手动触发**（上游为省 Actions 分钟数刻意关了
自动跑），所以 push 不会突然变红。但**手动跑会挂在两个存量门上**：

| 步骤 | 上游基线 | 本 fork 现在 | 说明 |
|---|---|---|---|
| `pytest -q` | 绿 | **绿** | 743 passed |
| `mypy src` | 89 errors | **80 errors** | 比上游还少 9 个 |
| `ruff check src tests alembic` | 1421 | **1420** | `ruff>=0.5.5` 未锁版本，新版 ruff 对这份存量代码报得极多；**非本次改动引入** |
| 前端 `build` + `test` | 绿 | **绿** | 160 passed |

mypy 那边本 fork 用 `TypedDict` 声明各工具的 kwargs（`kw = dict(...)` 会让
`**kw` 展开每一行都报 arg-type，上游 `parse_and_create_from_text` 就是这么
留着存量错误的）。新写工具别再沿用 `dict(...)`。

## 部署

照 [`docs/SELFHOST-RUNBOOK.md`](docs/SELFHOST-RUNBOOK.md) 走（源码安装，不用
Docker；含 systemd、Caddy、备份、升级、排查表、部署后浏览器复验）。

## 环境相关的坑

- **源码安装不用 Docker**:`WEB_STATIC_DIR` 默认是 `/app/static`(Docker 路径),
  源码装必须改指向 `frontend/apps/web/dist`,否则 Web 面板 404
- `TAX_CATEGORY_NAME` env 决定税额归到哪个分类名(默认「税与保险」);
  **前端拿不到这个 env**,改它会让税额扇区不再被钉住显示

## 新增:默认分类种子

上游的默认分类**只存在于 Flutter App**(`lib/services/data/seed_service.dart`),
靠首次启动时本地建好再 sync push 上行。服务端和 Web 端一个都没有 ——
实测新建账本后 `/read/workspace/categories` 返回 `[]`。放弃 App 只用 Web 时,
新账本会**一个分类都没有**,连「餐饮」都得自己手建。

本 fork 在 `POST /write/ledgers` 时播撒默认分类
(`src/services/default_categories.py`):

- **181 条 = 官方 177 + 本 fork 新增 4**。官方那部分从 `seed_service.dart` 的
  `hierarchicalExpenseCategories` / `hierarchicalIncomeCategories` 与
  `app_zh.arb` 的 `categoryExpense*` / `categoryIncome*` **程序化提取**,提取时校验
  「l10n 段数 == key 数」零不匹配。别手抄,也不要在本 fork 里另编一套。
- 新增的 4 条是 `tax_insurance`「税与保险」+ 消费税 / 所得税 / 社会保险
  (上游 issue #512)。名字必须与 `config.tax_category_name` 默认值一致。
- **syncId 与 App 同算法**:uuid v5,命名空间 `b3e7c0de-0000-4000-8000-beec00000001`,
  名字 `cat:{kind}:{level}:{key}`。mutator 不认 payload 里的 syncId(它自己生成),
  所以 seeder 在快照上**后置改写** —— 不去动 mutator,那是所有写入方共用的路径。
- 播撒规则:**每个用户只播撒一次**(他一个分类都没有时)。按名字判幂等做不到
  尊重用户改名。
- 开关:`SEED_DEFAULT_CATEGORIES=false`。

## 新增:组合支付(alembic `0021`,`read_tx_split_projection`)

一笔订单多个支付方式:5000 円 = 招行卡 3000 + 现金 2000。**父交易仍是一条**,
`amount` 仍是 5000(实付总额),`account_*` 被强制清空;拆分金额落子表。

| 要改的地方 | 文件 |
|---|---|
| 校验 + 写入 | `src/snapshot_mutator.py` `_normalize_splits` / `create_transaction` / `update_transaction` |
| 落库 | `src/projection.py` `replace_tx_splits`(delete-all + bulk insert) |
| **反向桥(最易漏)** | `src/routers/write/_shared.py` `_projection_row_to_tx_dict` + `_tx_splits_for` |
| 同步 merge | `src/sync_applier.py` `_merge_tx_splits_after_merge`(**不能**进 `_MERGE_SPECS`) |
| 余额聚合 | `src/routers/read/_shared.py` `_split_legs`(SQL) |
| 余额逐笔 | `src/routers/read/_shared.py` `account_balance_delta(splits=…)` |
| 读端返回 | `read/ledgers.py` / `read/workspace.py` / MCP `_serialize_tx` |
| CSV | 导出 `_splits_cell`;导入 `_parse_splits`(对称) |
| 前端 | `lib/txSplits.ts` + `TransactionsPanel.tsx` 拆分编辑器 |

### 四个必守的不变式(有护栏)

1. `sum(legs) == 父.amount`,容差 1e-6,**不等就 400**(不自动补差)
2. 有腿时父 `account_sync_id`/`account_name` **必须为空**(否则余额双倍扣)
3. 只支持 `tx_type == 'expense'`(收入拆分本质是转账)
4. 至少 **2** 条腿(1 条等于没拆,走普通单账户路径)

### 五个静默丢失的坑(都踩过,都有测试)

| 坑 | 后果 |
|---|---|
| `upsert_tx` 无条件 `replace_tx_splits(None)` | `/sync/push` 部分更新把腿全删 → 改成 `if "splits" in payload` |
| mutator 用 `pop("splits")` 表示清除 | 键消失 = projection 的「不动」→ `splits: []` **清不掉**。改写空 list 当信号 |
| `delete_account` 守卫只数父交易三列 | 有腿时那三列是空的 → 守卫失效 → 账户可删 → 子表悬空 → 余额永久漂移 |
| `_truncate_ledger` 漏子表 | rebuild / 还原后父交易没腿、旧腿还在 → 余额「凭空多出来」 |
| CSV 不导出 Splits 列 | 导出→导入后组合支付变「无账户的支出」,账户余额全错 |

护栏:`tests/test_tx_split_invariants.py`(20 条)、
`tests/test_account_balance_paths.py`(两条余额路径必须一致)、
`frontend/apps/web/src/txSplitsParity.test.ts`(两条提交路径不许只改一处)。

**腿没有 currency 字段** —— 与父交易同币种是**结构保证**,不是运行时校验。
别加「跨币种拆分」检查:那无法表达,只会给出虚假的安全感。

## 新增:信用卡自动还款(alembic `0022`)

一张信用卡绑一个扣款账户,到还款日自动生成一笔
`from=扣款账户 → to=信用卡` 的 transfer。**父交易就是普通转账**,不新增交易类型。

| 要改的地方 | 文件 |
|---|---|
| 账期窗口(纯日期) | `src/services/credit_card/billing.py` |
| 应还金额(四条规则) | `src/services/credit_card/statement.py` |
| 配置校验 | `src/services/credit_card/config.py` |
| 执行器(四道防线) | `src/services/credit_card/repay.py` |
| 定时任务 | `src/services/credit_card/scheduler.py` |
| 进程内 self-call | `src/services/credit_card/selfcall.py` |
| 写路径校验 + 手动触发 | `src/routers/write/accounts.py` |
| **反向桥(最易漏)** | `src/snapshot_builder.py` |
| sync merge | `src/sync_applier.py` |
| 前端 | `lib/autoRepay.ts` + `AccountDetailDialog.tsx` |

### 四条金额规则(漏一条静默算错)

1. 用 `amount` **原币**,禁 `coalesce(native_amount, amount)`
2. **必须 join 组合支付腿** —— 0021 有腿时父交易 `account_sync_id` 被清空
3. **不过滤** `exclude_from_stats` / `exclude_from_budget`
4. 账单不减窗口内还款(那是还上期账单)

### 五个必守的不变式

1. **余额是累计口径**:`balance@账单日 = 期初 + 截止账单日的全部变动`。
   写成「期初 + 窗口内变动」会丢掉窗口前的还款和转出卡的钱
2. **`window_start` 在原始 `billing_day` 空间重算**,不能
   `add_months(statement, -1)`(二次钳制 → 窗口重叠 → 还款额翻倍)
3. 正向 `current_billing_period` 与反向 `period_for_payment_due_date`
   **共用 `_payment_due_after()`** —— 各写一套会在
   `billing_day == payment_due_day` 时给出不同窗口
4. **孤儿还款日返回 `None`**(`bd=30, dd=29` 时 3/29 不偿还任何账单)
5. **`autorepay_from_account_sync_id` 存 sync_id 不存名字** —— 改名会让
   按名定位静默失效 → 同名歧义 → **只扣不加**

### 四道幂等防线(任缺一道可能重复扣钱)

1. 账期查重(`outstanding == 0` 就跳过 —— 这一条顺带满足「手动已还过就跳过」)
2. `autorepay_last_period`(恰好一次)
3. `Idempotency-Key: auto-repay:{card}:{YYYY-MM}:{amount}`(TTL 只有
   24h,**不能**作唯一保障;**必须带金额** —— 服务端会把 payload 一起
   hash,同键不同金额 → 409 把这一期卡死)
4. 数据库条件更新抢锁(`get_scheduler()` 是进程内单例,多 worker 会重复扣钱)

### 三个会烧钱的配置陷阱

| 陷阱 | 后果 |
|---|---|
| 多进程部署 | `BackgroundScheduler` 每进程一份 → 每张卡扣 N 次钱。**自动禁用** |
| `payment_due_day=0` | 调度**永远不触发**,用户半年后才发现没还过 |
| 跨币种还款 | transfer 两端共用一个 `amount` → ¥10000 原样加到 JPY 卡 |

### 静默丢失的坑(第三次同类)

`tax_amount` 在 `_projection_row_to_tx_dict` 被漏 → `splits` 在同一函数被漏
→ **`autorepay_*` 在 `snapshot_builder` 被漏**。同一形状:「projection →
内存结构」的反向桥漏了新列。漏了之后任何一次**无关字段的更新**都会把用户
配置清空,HTTP 200,不报错。

护栏:`tests/test_credit_autorepay_e2e.py`(端到端穿过全链路,分层测试全绿时
只有它能抓到)。

**分层测试全绿 ≠ 拼起来能工作。** 这个功能有两轮「测试全绿但跑不通」:

1. internal/external 账本 id 混淆 —— 分层测试用 FakeCall,没人真发过那个请求
2. **端点整个调不通** —— 743 测试 + 160 前端测试全绿,起真服务点一次手动触发
   就炸:`asyncio.run()` 不能在运行中的 loop 里调;selfcall 自己重造了一遍
   MCP 已有的 `_self_call` 且连错三次;`repay_one` 的失败被端点吞成 HTTP 200,
   **用户看到的是「点了没反应」**;时区口径和调度器不一致;幂等键不含金额 →
   409 把这一期彻底卡死

所以:凡是**替换掉真实 IO 的测试夹具**(FakeCall / mock / 内存 DB),
它证明的只是「算法对」,不是「接线对」。接线只能靠真跑一遍。

### 写这种「会花钱」的代码,三条自检

1. **别重造已存在的模式** —— 先搜。MCP 的 `_self_call` 早就做完了自造那套
   重写一遍的每行都是新的出错机会
2. **失败不许被吞** —— `repay_one` 返回 `status="failed"`,端点若照常
   200,用户什么都看不到。现在 failed→502 / 配错→400
3. **一致的口径要有唯一来源** —— 时区走 `_resolve_scheduler_tz()`,
   别用 `date.today()`

### 验证护栏时先确认回退真的生效

我做 6 次「回退看会不会变红」,3 次没变红 —— 全是**替换脚本没匹配上**,
文件压根没被改。差点把「护栏失效」写进文档。**先确认文件变了,再判护栏。**
