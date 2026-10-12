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
- Web: 前端源码在 `frontend/apps/web/`(pnpm workspace:`apps/web` +
  `packages/{ui,web-features,api-client}`)。**没有 README** —— 命令与
  约定以本文件为准(尤其「前端是两道独立的门」那一节)

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

**四条容易踩的**(`CLAUDE.md` 上游那节讲的是移动端同步契约,这几个是本 fork 独有的):

1. **`_projection_row_to_tx_dict` 漏了税额** → Web PATCH 更新会**静默抹掉它
   且不报错**。上游 `nativeAmount` 当年就踩过同一个坑,注释还在 `:895-899`。
2. **金额运算不要写 `t.amount`** → 那是原币,多币种账本会把 CNY 和 JPY 直接
   相加。聚合一律走 `baseAmount()`。已有护栏
   `frontend/apps/web/src/amountBasisGuard.test.ts` 会拦。
3. **hook 别插进函数体** → 语法完全合法但永不可达,`tsc` 和 build 都抓不到。
   护栏 `frontend/apps/web/src/hookPlacement.test.ts`。
4. **`tax_amount = 0` 在三条 MCP 入口里含义不同**(实测踩过,改错会**弄坏清除功能**):

   | 入口 | `0` 的含义 | 判据 |
   |---|---|---|
   | `create_transaction` | 无意义(mutator 拒绝) | `tax <= 0` |
   | `create_transactions` | 同上 | `tax <= 0` |
   | **`update_transaction`** | **清除信号**(PATCH 的显式 null 传不进来) | `tax < 0` |

   update 的 `0` 在几行之前就被转成 `patch["tax_amount"] = None`,**根本到不了
   那条校验** —— 所以那里写 `<= 0` 会**拦下「清除这笔税额」**。
   `tests/test_tx_tax_amount.py::test_mcp_update_zero_means_clear` 立刻变红。

   > 📌 **同一个字段在不同入口可以有不同语义,别把它们当同一处代码「统一」。**
   > 判断「哪个对」之前**先找有没有测试已经写死它** —— 有测试 = 有意为之,不是漏。
   > 护栏:`tests/test_write_request_fields_are_used.py` 里那三条分别断言。

## fork 修复:管理面板「备份」

`POST /admin/backups/create` 原先去找一条 `entity_type == "ledger_snapshot"` 的
SyncChange,找不到就 404。方案 B 之后没有任何代码再写那种行,所以**对新账本必然
失败**。已改成 `snapshot_builder.build(db, ledger)` 现场构建。

其余两条备份路径(定时 rclone 的 `VACUUM INTO`、`scripts/backup_sqlite.sh`)不
经过 snapshot,一直正常。

## 新增:MCP 附件

`attach_receipt` / `create_transaction_with_receipt`。REST 层
(`/attachments/upload`)是上游就有的,本 fork 只补了 MCP 接线。

## CI 与前端打包:细节在 docs/

CI 三道门的**实际状态**(ruff/mypy 都有存量、`pytest` 曾因 step 中止而**从未执行**、
pnpm / Node 版本的唯一来源、lockfile 不许绕过的道理、Dockerfile 里的副本)
都写在 **[docs/CI-AND-BUILD.md](./docs/CI-AND-BUILD.md)**。

前端打包体积、**首屏要看 `index.html` 的 `rel` 而不是 assets 目录**、i18n 两条别踩的、
`playwright-cli` 探针不可靠的三个实例,都写在
**[docs/FRONTEND-NOTES.md](./docs/FRONTEND-NOTES.md)**。

**一句话版本**:这三道门里 `ruff` / `mypy` 挂着存量违规(所以已标 advisory),
真正的必过门是 `pytest`;前端是**两道独立的门**(`vitest` 与 `tsc -b`),只跑前者会漏类型错误。

## 逐轮验证日志 -> [docs/aegis/verification-log.md](./docs/aegis/verification-log.md)

每轮的实测记录(测了什么、哪个探针坏在我自己身上、哪条护栏做了变异验证)
都在那个文件里。**细节不留在本文件** —— 本文件是速查表,下面是它的展开:

| 文档 | 装什么 |
|---|---|
| `docs/CI-AND-BUILD.md` | CI 三道门的实际状态、pnpm/Node 唯一来源、lockfile、Dockerfile |
| `docs/FRONTEND-NOTES.md` | 打包体积、首屏度量、i18n 约束、浏览器探针 |
| `docs/aegis/verification-log.md` | 逐轮验证记录 |
| `docs/SYNC_ARCHITECTURE.md` | 同步契约 |
| `docs/SELFHOST-RUNBOOK.md` | 部署 |

📌 **为什么必须拆**:单轮上下文预算 **65244 字节**,而这个文件一度到 **93 kB**
—— **每轮丢掉约 28 KB**,丢掉的正是「三个会烧钱的配置陷阱」
「时区已栽五次」「全仓库只能有**一个** `TestClient(app)`」这类
**绕不过去的契约**,也就是本文件存在的意义。
`test_claude_md_fits_the_context_budget` 会在超限时变红。

📌 **判据:一句话里出现「第 NN 轮」,它多半属于日志。**
不是按重要性分,是按**是否还会过期**分 —— 长期有效才配进指南。

## 部署

照 [`docs/SELFHOST-RUNBOOK.md`](docs/SELFHOST-RUNBOOK.md) 走（源码安装，不用
Docker；含 systemd、Caddy、备份、升级、排查表、部署后浏览器复验）。

## 环境相关的坑

- **源码安装不用 Docker**:`WEB_STATIC_DIR` 默认是 `/app/static`(Docker 路径),
  源码装必须改指向 `frontend/apps/web/dist`,否则 Web 面板 404
  —— ⚠️ **`grep -rn "WEB_STATIC_DIR" src/` 什么都搜不到,这是正常的。**
  `src/config.py:19` 定义的字段叫 `web_static_dir`,环境变量名是 Pydantic
  Settings 按约定大写出来的(没设 `env_prefix`)。我查静态文件服务时因为
  「搜不到」就断定文档过时,差点把这条**正确**的部署指引删掉。
  护栏:`tests/test_config_env_mapping.py`
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

### 静默丢失的坑(都踩过,都有测试)

| 坑 | 后果 |
|---|---|
| `upsert_tx` 无条件 `replace_tx_splits(None)` | `/sync/push` 部分更新把腿全删 → 改成 `if "splits" in payload` |
| mutator 用 `pop("splits")` 表示清除 | 键消失 = projection 的「不动」→ `splits: []` **清不掉**。改写空 list 当信号 |
| `delete_account` 守卫只数父交易三列 | 有腿时那三列是空的 → 守卫失效 → 账户可删 → 子表悬空 → 余额永久漂移 |
| `_truncate_ledger` 漏子表 | rebuild / 还原后父交易没腿、旧腿还在 → 余额「凭空多出来」 |
| CSV 不导出 Splits 列 | 导出→导入后组合支付变「无账户的支出」,账户余额全错 |
| `delete_tx` 只删父行、不删腿 | **已修,205 轮验证**:批量删除带腿交易后**腿会跟着删干净**(护栏 `test_tx_split_invariants.py::test_batch_delete_cascades_split_legs`,变异验证过)。注意这条以前**只有注释、没有测试** —— 注释不是护栏 |

**通用护栏(加新列时立刻红)**:`tests/test_projection_reverse_bridge_coverage.py`
枚举 `ReadTxProjection.__table__.columns` 全部列,减去白名单后要求**每一列**
都被反向桥引用 —— 加新列忘了改反向桥,这条立刻点名漏了哪一列。

用**白名单**而不是黑名单是关键:黑名单在加新列时**默认通过**(而那正是要防的
情况),白名单默认**拒绝**,新列必须显式论证为什么可以不接。
白名单带数量上限,防止有人为了「让测试绿」把真列塞进去。

(此前每一次都是**事后**补一条专用测试 —— 那是症状处理不是解决方案。
第 6 列被加进来时同样的事会再发生一次,仍然要等用户发现数据被抹了才有人查。)

专用护栏:`tests/test_tx_split_invariants.py`(23 条)、
`tests/test_account_balance_paths.py`(两条余额路径必须一致)、
`frontend/apps/web/src/txSplitsParity.test.ts`(两条提交路径不许只改一处)。

### 无关编辑**不再**抹掉自相矛盾的既有腿(已修)

改一个**无关字段**(比如备注)时,自相矛盾的既有腿曾经被静默抹成 `[]` ——
**整笔金额从所有账户余额上消失,而它还照样进统计**。

实测:`update_transaction(snap, "tx_1", {"note": "..."})` -> `splits` 变 `[]`,
只有一条 WARNING,API 返回成功。

**为什么这是严重问题**(`_split_legs` 只按账户聚合腿、从不检查合计;
有腿时父 `account_*` 是空的,父对余额零贡献):

| 父 5000 / 腿合计 4999 | 账户余额被扣 | 支出合计−账户变动 缺口 |
|---|---|---|
| 抹掉腿(旧) | **0** | **5000** |
| 保留(现在) | 4999 | **1** |

而「不变式」本来就**已经**被破坏 —— 实测 `_merge_tx_splits_after_merge`
与 `replace_tx_splits` **都不校验合计**,脏腿能从 `/sync/push` 直接落库。

#### 现在的规则:**只在 payload 真的改了 `amount` 时才作废腿**

> ⚠️ 我第一版写成**无条件保留**,`test_patch_amount_invalidates_splits`
> 当场变红 —— 把 5000 改成 6000 之后腿还停在 5000,交易声称 6000 却只动 5000,
> **那才是真的不一致**。**腿是「总额变了才失效」的,不是「碰了这笔就失效」。**

用户**显式**改腿时仍然被拒(`_normalize_splits` 抛出的异常不被那个 try 捕获)。

#### 修复之后,用户**看得见**也**改得掉**(实测闭环)

「保留脏腿」单独看只是少丢数据;它真正的价值是**形成了闭环**:

| 环节 | 实测 |
|---|---|
| 脏腿留在库里 | 165 轮:读库两条腿都在 |
| 前端**回显**两条腿 | `splitsToInput([3000, 1999])` -> 两条都在 |
| UI **提示**差额 | `splitsRemainder(5000, rows)` -> **`1`**(非 null) |
| 用户**改得掉** | 显式改腿会走 `_normalize_splits`,凑齐 5000 即成功 |

`TransactionsPanel.tsx:682` 真的渲染了 `t('transactions.splits.remainder')`
—— **有渲染出口,不是死代码**(这一条专门查过:函数存在不等于被用,
仓库里死掉的纯函数不少)。

**CSV 备份往返也是通的**(实测):`_parse_splits` **不校验合计**(只数腿数 >= 2),
所以脏腿能原样导出再导入 —— 用户搬机器/换库不会把它变成「无账户的支出」。

> 📌 测这个**必须用真实导出器**造输入(`_splits_cell`,格式是
> `招行卡:3000.00|现金:1999.00` 的 `|` 分隔)。我第一版凭印象写成 JSON
> 二元组,`_parse_splits` 返回 `None`,差点记成「往返断了」的**假缺陷** ——
> 导出器 `workspace.py:325` 的 docstring 里就写着格式。

> 📌 修复前这条路是**断**的:腿被抹掉 -> 用户只看到一笔 5000 的支出 ->
> 余额却少了 4999 -> **无任何提示**。所以「保留」不只是不丢数据,
> 它是「让用户有机会修」的**前提**。

两条契约各有护栏,变异验证 2/2 抓到:
`tests/test_tx_split_invariants.py::test_unrelated_edit_keeps_inconsistent_legs`
与 `::test_patch_amount_invalidates_splits`。

### ⚠️ 删分类:两条路径**严格程度不一致**(实测,未改)

同一个操作「删分类」,两个实现给了**两种答案**:

| 路径 | 有子分类时 |
|---|---|
| `snapshot_mutator.delete_category`(Web/MCP) | **拒绝删除**,要求先迁移 —— 注释明说「比允许删除并 orphan 更好,避免无主交易污染 ledger」 |
| `sync_applier._delete_user_category` | **照删**。只清 icon 附件,**子分类留下悬空 `parent_sync_id`** |

这是「**同一件事两份实现,一份严一份松**」那一族。

> 📌 本 fork **不用官方 App**(CLAUDE.md:部署形态是自托管云端 + Web/PWA),
> 所以 sync 删除路径基本不被走 —— **实际影响低**。
> 但不一致本身是隐患:哪天接了 mobile 或加了「从 mobile 删」的入口,
> 就会出现「同一件事两种结果」。

📌 **本轮只记录,没有改** —— 收紧 sync 侧会让「删了父分类」这个
**mobile 已经做过**的操作开始 4xx,那是破坏性变更;要改得两侧一起定契约。

实测到的形状:`read_tx_split_projection` 是全库**唯一**的真子表
(唯一带 `tx_sync_id` 指父行的表),其余 `*_sync_id` 列都只是**引用**不是**归属** ——
所以「删父行要连带清子表」这件事**只需要保证 `delete_tx` 一处**。

### 统计口径 vs 余额口径:实测**一致**,不用改(负结果)

174 轮查了一个能藏 bug 的地方:「月支出」和「账户余额」口径不同源吗?

`workspace_analytics` 的 `expense_total` 直接用**父 `amount`**,
查询只 select 五列(`tx_type` / `native_amount` 或 `amount` /
`happened_at` / `category_name`),**不 join 腿表**。

看着可疑,量了之后**成立**:

| 口径 | 组合支付 5000 = 卡 3000 + 现金 2000 |
|---|---|
| 统计 | 父 amount **5000** |
| 余额 | 腿 3000 + 2000 = **5000** |

**一致** —— 「花了多少」本来就该用总额,腿只是「钱从哪几个账户出」。

⚠️ 另一个更险的假设也验掉了:**统计查询**只按时间过滤、没有账户过滤**
(`where` 只有 `happened_at` 两条)。所以有腿时父 `account_sync_id`
为空**不会**让组合支付从统计里消失 —— 如果哪天给统计加了按账户过滤,
这一条就会变成「整笔支出凭空不见」的真 bug。

📌 **负结果也是结果** —— 记下来是为了**别让后面的人重新查一遍**。

⚠️ **174 轮标的那颗雷,175 轮**没验成** —— 如实记下来,别当成已拆。

想验证「给统计加账户过滤 -> 组合支付从统计消失」,两次都栽在探针上:

| # | 我干了什么 | 实际 |
|---|---|---|
| 1 | 用 `grep -c "^FAILED"` 数失败数 | 跑出的是 **ERROR**,不是 FAILED -> 数到 **0**,差点读成「没失败」 |
| 2 | 把 `where` 插到 `tx_query = tx_query.where(` 之后 | 插进了某个 `if` 块**中间** -> **SyntaxError**,变异根本没生效 |

**176 轮的进展(半个结论 + 一条新护栏)**

用 `mutation_check` 把变异做**对**了(上一轮的 SyntaxError 修掉了):
给统计加 `account_sync_id.is_not(None)` -> **27 条测试全绿,一条没红**。

- ✅ **确实没有护栏**覆盖「组合支付计入统计」—— 原来那条
  `expense_total == 6000` 的 6000 来自**普通交易**,不是组合支付。
  已补 `test_split_payment_is_counted_in_analytics`(行为判据:
  普通 1000 + 组合 5000 -> `expense_total == 6000`)。
- ⚠️ **但那颗雷仍然没拆**:新护栏在同一个变异下**也绿**。
  说明**那个变异并没有改变 `expense_total`** —— 大概率它落到的
  不是 `expense_total` 真正使用的那条查询分支。

### ✅ **那颗雷是真的,护栏也咬得住(178 轮结案,174-177 全程都没验对)**

| 轮 | 做了什么 | 结果 |
|---|---|---|
| 174 | 读代码推断「统计无账户过滤 -> 会有雷」 | 雷**标出来了**,但没验 |
| 175 | 两次探针都坏(只数 FAILED / 变异插进 if 块) | 没验成 |
| 176 | mutation_check 做对了变异 | **假绿** —— 护栏其实**没抓到** |
| 177 | 反推取值点,推论「父账户不是 NULL」 | **推论是错的** |
| 178 | 用 pytest 量父账户 + 换锚点 | **雷为真,护栏有效** |

**真相**:`account_sync_id` / `account_name` **都是 None**(不变式 2 成立,
用 pytest 跑 `_setup` 量到的)。176/177 轮的变异之所以无效,是因为锚点那行
在 `if start_at is not None:` **里面** —— 而测试走 `scope=all`,
`start_at` 是 None,**那一段从未执行**。

改锚到循环前(无条件执行)后,**护栏变红**:

    tx_query = tx_query.where(account_sync_id.is_not(None))  # 加在 for 之前
    -> test_split_payment_is_counted_in_analytics FAILED ✅

> 📌 **「变异没被检出」的**第四种**原因 —— 也是最危险的一种:
> **变异本身是死的**(锚点落在测试**不会进入**的条件分支里)。
>
> 它和「护栏失效」在观测上**完全一样**(都表现为:变异后全绿),
> 但结论相反 —— 前者要**换锚点**,后者要**补护栏**。
>
> **怎么区分**:先确认**变异真的执行了**(量一个可观测副作用),
> 再谈护栏强不强。177 轮我就是把「死变异」误判成了「数据不对」——
> **拿错误前提去解释观察,等于把两个错误绑在一起。**

### ✅ 工具已补上「锚点可能在死分支里」提示(178 轮)

`scripts/mutation_check.py` 现在会 **AST 解析锚点所在行**,若它被包在
`If`/`For`/`While`/`Try`/`With` 的**块体内部**(不含块头自己),
就打印一条提示,并给出「先确认变异真的执行了」的判据。

**边界要说清楚**:它只能看到「**某一层**分支」,**不区分嵌套**。所以
「在分支里」**不等于**「测试进不去」—— 178 轮实测里,一个**确实**会执行
的锚点也落在分支内(它在 `if start_at` 的**上一层**分支里)。**所以这是
**提示**,不是判据。**

### ⚠️ 改**工具**之后:必须**跑工具本身**,不能只跑 pytest(179 轮)

178 轮补完 AST 检查,**只跑了 pytest** 就提交 —— 而 pytest **根本不 import
`scripts/mutation_check.py`**,于是那个 commit 推上远端的是**语法错误**的脚本。

179 轮第一次真跑它才发现:

    SyntaxError: unterminated string literal (detected at line 179)

根因:我用 python 字符串替换改文案,源里的 `
` 被写成**真换行**,
把一个字符串字面量劈成两半。

> 📌 **「测试全绿」对脚本类改动**什么也证明不了** —— pytest 只收集
> `tests/`。改 `scripts/` 之后要**直接执行那个脚本**。
> 与「分层测试全绿 ≠ 拼起来能工作」同源,只是这次**被测对象是工具**。

### ✅ `scripts/` 之前**一道门都没有**(180 轮补上)

| 门 | 覆盖 `scripts/` 吗 |
|---|---|
| `pytest tests/` | ❌ **不 import** —— 实测 `grep -rn "scripts/" tests/` -> **0** 命中 |
| `ruff check src tests alembic` | ❌ CI 里**写死**这三个目录,`scripts/` 不在内 |

**两处都补上了:**
1. `ci.yml` 两处改成 `ruff check src tests alembic scripts`(实测
   `scripts/` 8 个文件**零违规**,所以**基线不变**,仍是 1255)
2. 新增 `tests/test_scripts_compile.py` —— 对每个脚本调 **`compile()`**
   (真 Python 解析器),语法错就抛

变异验证:往 `mutation_check.py` **追加一段语法错** -> **变红**;
还原后恢复绿 ✅

> 📌 **空集合的断言恒真**,比没有护栏更糟 —— 所以那条护栏**先断言
> 「扫到了东西」**(`_SCRIPTS` 非空),再断言「都能编译」。

### ✅ 再进一步:「门覆盖了哪些文件」本身要能自省(181 轮)

180 轮补的是**已知**缺口。真正该问的是:仓库里**每一个** `.py` 是不是
都在某道门的范围内?

枚举全仓 257 个 `.py`,减去 `src`/`tests`/`alembic`/`scripts` ->
**3 个漏网**:

| 漏网 | 是什么 |
|---|---|
| ~~探针残留~~ | 我自己的临时文件(已删,现在**没有**这一条) |
| **`server.py`** | ⚠️ **生产 ASGI 入口** —— 语法坏了服务**直接起不来** |
| `new-apple-ui/.local/verify_csv_split.py` | Apple 客户端的验证脚本 |

📌 **最后那个「探针残留」是 181 轮的快照**,磁盘上早已删掉 —— 写在这里
是为了如实记录「当时有 3 个」。

⚠️ 185 轮加了「死链」护栏(CLAUDE.md 点名的每个 `.py` 都必须在磁盘上)
之后,这条记录**自己成了全仓唯一一条死链** —— **文档引用一个不存在的
文件,正是它最不该做的事**:读者会去找。

所以改成删除线,并且**连名字都不再写**(护栏只认带反引号的路径,
但写出来就是在教读者去找一个已经没有的东西)。

**护栏**:`tests/test_scripts_compile.py::test_no_python_file_falls_outside_every_gate`
枚举全仓 `.py`,落在门外的**必须为空**。CI 也同步改成
`ruff check src tests alembic scripts server.py new-apple-ui`。

📌 **判据方向是白名单**:新加顶层目录时**默认漏网** —— 而那正是要防的。
黑名单在这种情况下**默认通过**,等于没有护栏。

⚠️ 白名单**必须与 ci.yml 的参数保持一致** —— 我第一版只改了一边,
护栏立刻**点名**了 `new-apple-ui`。**两份清单要当一条契约看。**

变异验证:新增一个仓根 `.py` -> **变红**;删掉 -> 恢复绿 ✅
ruff 基线 1254 -> **1257**(多出的 3 条是这两个新纳入的文件)。

### 🔴 182 轮抓到:**181 轮把 `ci.yml` 写坏了**(已修)

181 轮我用 python 字符串替换改 `ci.yml`,注释里的换行**没转义** ->
两行变成了

```yaml
        run: # ⚠️ scripts/ 也要查 ……
# 所以它们**没有任何门**…
ruff check src tests alembic scripts server.py new-apple-ui
```

**后果**:YAML **整个文件解析不了** —— CI workflow 直接失效。而 181 轮
**只跑了 ruff**(它读的是命令行参数,不读 YAML),**完全没发现**。

> 📌 **「我改了配置」要验配置**能**被解析**,不是验那行命令跑不跑通。
> ruff 绿 ≠ workflow 是合法 YAML。

### ✅ 「YAML 能不能解析」也变成护栏(183 轮)

182 轮只修了 `ci.yml` **一个**文件 —— **没问其它 workflow 是不是也坏了**。
枚举全仓 **16 个** YAML(5 workflow + 6 issue 模板 + issue-lint 配置
+ 2 个 docker-compose + pnpm workspace/lockfile),**全部可解析**。

护栏 `tests/test_scripts_compile.py::test_every_yaml_file_parses` 全部纳入。
⚠️ GitHub Actions **不做本地 YAML 校验**,写坏了要等**手动触发**才发现 ——
而本仓 `ci.yml` 恰恰是 `workflow_dispatch`,**平时根本不跑**。

**变异验证 = 原样复现 182 轮那次事故** -> **变红** ✅;还原 -> 恢复绿 ✅。

> 📌 **把事故变成护栏**:不是「记一条纪律」,而是**让同一个错误再也
> 溜不过去**。判据能**复现那次具体事故**,才算护栏真的接上了。

📌 又踩一次 `
` 的坑(heredoc 里被写成真换行,把字符串劈成语法错误)——
这是本会话**第三次**。**含 `
` 的代码一律用 `edit` 工具写。**


### ✅ 唯一来源:护栏**从 ci.yml 解析**,不再维护副本(182 轮)

`test_no_python_file_falls_outside_every_gate` 现在用 `yaml.safe_load` +
`shlex.split` **从 `ci.yml` 里读出 `ruff check` 的路径参数**,用它判断漏网。

📌 181 轮我第一版在测试里维护了**一份白名单副本**,然后写了一句
「必须与 ci.yml 保持一致」—— **那是一句没有任何强制力的叮嘱**:
改 ci.yml 的人不会看到它。**副本必然漂移。**

变异验证:从 `ci.yml` 删掉 `new-apple-ui` -> 护栏**变红** ✅
(证明它真跟着 CI 走,不是靠副本)




### ✅ 这条工具检查自己也配了护栏(179 轮,变异验证过)

`tests/test_mutation_check_dead_branch.py` —— 真跑 CLI,断言**输出**。
变异(把 `if isinstance(...)` 改成 `if False`)-> **变红**。

📌 **写这条护栏时自己错了三次,都值得记:**
1. **用 `tmp_path` 造 fixture** -> 工具先跑 `git diff`,临时文件**不在 git 里**,
   exit=2,**永远走不到** AST 检查。**给工具写护栏要用工具自己认的输入。**
2. **拿 `for` 循环头当「无条件位置」的反例** -> 它其实落在外层 `If` 块**内部**,
   工具**提示得对**,是我断言错了。**反例也要先量。**
3. **用 python 写含 `
` 的多行文档字符串** -> 又一次被劈成语法错误。
   **写中文文档/多行字符串一律用 `write` 工具,不用 bash heredoc。**

📌 **做这个检查时自己踩了两个坑**(都在实现里修掉了,值得记住):
1. `ast.walk` 会走到**没有 `end_lineno`** 的节点(`arguments`/`alias`)——
   直接取属性会 `AttributeError`。**先 `getattr` 再用。**
2. 块头**自己**的那行(`for x in ...:`)是「无条件执行到」的 ——
   只判「在块范围内」会把**循环头本身**误报成死分支。


📌 **这两条各有一个通用教训:**
- **只数 `FAILED` 会把 `ERROR` 当成「没问题」** —— 与「空输出不等于通过」同源。
  数失败要 `FAILED` **和** `ERROR` 一起数。
- **变异「让测试报错」不等于「护栏抓到了变异」** —— SyntaxError 只能证明
  文件变了,证明不了判据在起作用。**要看它红在**断言**上,不是红在**解析**上。

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

### 静默丢失的坑(「同一件事两份实现」这一族)

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

### ⚠️ 异常处理器自己不许再抛(备份服务实测踩到)

`services/backup/runner.py` 的 `_push_one` 里,`except` 块**自身没有任何保护**:

```python
except Exception as exc:
    logger.exception("push to %s failed", remote.name)
    target_db = sub.get(BackupRunTarget, target.id)   # 可能是 None
    target_db.status = "failed"                       # AttributeError
    sub.commit()                                      # session 坏掉时也会失败
```

两处会抛:

1. 目标行在「列举之后、推送之前」被删 -> `get()` 返回 `None`
2. **原始异常来自数据库** -> session 已处于坏状态,`commit()` 也失败

任何一处再抛,都会**顶掉原始异常并让整个 backup run 崩掉** ——
结果「这次备份失败了」这件事**根本没被记下来**。
运维看到的是 run 中断,而不是一条 failed 记录。

**通用规则:`except` 块里的每一行都可能抛**。写失败记录的时候,
那一段要么自己再包一层 try,要么先判空。不要假设「我已经在 except 里了,
这里不会再出错」—— 错上加错的路径上,**没有任何东西是安全的**。

⚠️ 这与「失败不许被吞」是同一族的**另一半**:那一半讲的是
「别把 `failed` 报成 200」,这一半讲的是「**报 failed 的代码本身
不能先崩掉**」。两条都做到了,失败才是真的被记录了。

这 14 条 mypy 错误(全在 `union-attr`)不是噪音,是同一处未判空的 14 个实例。
修完 mypy **80 -> 70**。⚠️ **mypy 报 `| None` 要当真读** ——
它指出的每一次都可能是真能走到的那次。

### 写这种「会花钱」的代码,三条自检

1. **别重造已存在的模式** —— 先搜。MCP 的 `_self_call` 早就做完了自造那套
   重写一遍的每行都是新的出错机会
2. **失败不许被吞** —— `repay_one` 返回 `status="failed"`,端点若照常
   200,用户什么都看不到。现在 failed→502 / 配错→400
3. **一致的口径要有唯一来源** —— 时区走 `_resolve_scheduler_tz()`,
   别用 `date.today()`

### 四道幂等防线:防线 4 要**重入**才测得到(顺序法测不到)

防线 4(`rowcount != 1` 才放弃)防的是**多 worker 抢同一期**。

用「同一期调两次」这种顺序法**永远测不到**:防线 2
(`last_period == pkey`)排在它**前面**,第二次调用在防线 2 就返回
`skipped_already_repaid`。实测三次前置条件都抓不到:

| 前置条件 | 谁挡住了 |
|---|---|
| 全额还款 | 防线 1(`outstanding == 0`) |
| 部分还款 | 扣款账户余额归零,`payable <= 0` |
| 部分还款 + 再充钱 | **防线 2** |

**但「顺序调用测不到」不等于「测不到」** —— 我第一版把「难做」当成了
「不可做」,白留一句「得上两个真连接 + 显式交错」就收工。换**重入**即可:

```
防线 2 -> 防线 1(compute_statement) -> payable -> 防线 4 -> self_call
```

把 `compute_statement` 包一层,在**它里面**把另一个 session 的
`repay_one` 整个跑完。A 过了防线 2 → 调 `compute_statement` 时 B 完整跑完
(拿锁、提交、写交易)→ A 回到主线,拿**过期的**余额去抢锁 →
`WHERE last_period IS NOT pkey` 匹配 0 行 → `skipped_locked`。

⚠️ **必须两个独立 session**:文件库 + 两个引擎。`StaticPool` 的内存库
所有 session 共用一个连接,未提交事务对另一个「看得见」,造不出竞态。

护栏:`tests/test_credit_autorepay_idempotency.py`
`test_barrier4_blocks_a_real_interleaved_race`(变异验证:去掉 `rowcount`
检查 → 变红,A 会再扣一次)。

**规则:测试方法受阻时,先分清「这个场景造不出来」和「这个方法造不出来」。**

### 调 `repay_one` 的两个坑(白折腾两轮才搞清)

1. **`today` 必须正好是还款日** —— `is_due_on(today, payment_due_day)`
   这一关卡的是「今天**是**还款日」,不是「过了还款日」。取错日期得到
   `skipped_not_due`
2. **`self_call` 的真实签名**是 `(db, *, method, path, body, headers, user)`
   —— 猜错会被 `repay_one` 吞成 `status="failed"`,报错信息绕一圈才看到

### 缺口检测只告警,永不自动补跑

停机超过 `CATCH_UP_DAYS = 7` 天时,那一期**永远补不上**(补跑窗口已过),
而真实信用卡那边已经是逾期。catch-up 跑完会检测「`autorepay_last_period`
落后 **≥ 2 期**」并打 `WARNING`。

**只告警,不自动扣** —— 计划文档反对无界补跑,理由是对的:补几十笔真钱。
用户可以用 `POST /write/accounts/.../autorepay/run` 手动补。

基准是「**最近一个已过去的还款日**」(`_last_past_due_date`),**不用**
`current_billing_period` / `period_for_payment_due_date`:
后者只对**还款日当天**成立(其余日子直接 raise),前者指向**窗口右端**
(下期)—— 两者都会把「上月已还、本月还没到期」判成落后两期,每天一条
假告警。用户很快学会无视日志,真正该看见的那条就夹在里面了。

阈值必须 **2 不是 1**:落后 1 期是正常状态(本月还没到还款日)。

## 测试与护栏的经验 -> [docs/TESTING-GUARDRAILS.md](./docs/TESTING-GUARDRAILS.md)

「**怎么把一条契约变成一道会红的门**」整套经验都在那里。改业务时不必读;
**要加护栏、或者测试出现「单跑绿、全量红」/ 变异没被检出时必读**。

最常踩的四条:

1. **源码级断言会恒真** —— `tokenize` 把 `date.today()` 拆成 `date . today ( )`,
   join 之后永远匹配不上;要用 `untokenize`
2. **「变异没被检出」先问三件事**:护栏失效?被上层挡住?还是它测的是**另一条路**?
   第三种最容易被误判成第一种
3. **「单跑绿、全量红」有四种来源**:模块级改 `os.environ` / 模块级单例 /
   全局 engine(import 顺序决定有没有生效)/ 连接未释放
4. **静态判据骗得死死的**:反向桥那类护栏必须**真跑**并检查输出 dict ——
   「列名在源码里出现过」不证明它接上了

⚠️ **但加列 / 加表时必须知道的三张登记表**(细节在
[docs/TESTING-GUARDRAILS.md](./docs/TESTING-GUARDRAILS.md),这里是索引):
`_projection_row_to_tx_dict`(反向桥)、`_truncate_ledger`(清理清单)、
`_LEDGER_MERGE_SPECS`(同步 merge)。三者都是**白名单式** —— 加新东西时
**默认红**,必须显式论证为什么可以不登记。漏 `_LEDGER_MERGE_SPECS` 的后果
最隐蔽:`/sync/push` 返回 **200**、`accepted` 计数**正常**,但字段**没落库**。

