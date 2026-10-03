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
| `pytest -q` | 绿 | **绿** | 521 passed |
| `mypy src` | 89 errors | **83 errors** | 比上游还少 6 个 |
| `ruff check src tests alembic` | 1421 | **1420** | `ruff>=0.5.5` 未锁版本，新版 ruff 对这份存量代码报得极多；**非本次改动引入** |
| 前端 `build` + `test` | 绿 | **绿** | 117 passed |

mypy 那边本 fork 用 `TypedDict` 声明各工具的 kwargs（`kw = dict(...)` 会让
`**kw` 展开每一行都报 arg-type，上游 `parse_and_create_from_text` 就是这么
留着存量错误的）。新写工具别再沿用 `dict(...)`。

## 环境相关的坑

- **源码安装不用 Docker**:`WEB_STATIC_DIR` 默认是 `/app/static`(Docker 路径),
  源码装必须改指向 `frontend/apps/web/dist`,否则 Web 面板 404
- `TAX_CATEGORY_NAME` env 决定税额归到哪个分类名(默认「税与保险」);
  **前端拿不到这个 env**,改它会让税额扇区不再被钉住显示
