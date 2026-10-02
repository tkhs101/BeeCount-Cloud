# 自托管二改计划：消费税税额 + MCP 附件 + 自建云端

- 日期：2026-10-03
- 基线：`3d9f64b`（= tag `1.6.7`，与上游 `TNT-Likely/BeeCount-Cloud` 完全同步，0 ahead / 0 behind）
- 目标读者：**无本仓历史上下文的工程师**

---

## Aegis Visibility

本计划要加一个新持久化字段并让它贯穿三条写入路径（web write / MCP self-call / backup restore），
同时新增两个 MCP 工具和 Web 端 UI。`read_tx_projection` 是唯一的读权威源，一旦漏掉某个触点
会出现**静默丢数据**（见 T4.1 风险 R1）。这类改动必须有持久化边界和明确验证。

---

## 1. Goal

在自托管的 BeeCount-Cloud 上实现三项能力，全部通过自建云端 + Web/PWA 使用，**不使用官方 App**：

| 编号 | 能力 | 对应上游 issue |
|---|---|---|
| F1 | 交易可记录消费税税额；统计中「税与保险」独立成类 | #510 |
| F2 | 默认分类体系补「税与保险」大类（**以用户自建方式落地，不改默认分类**） | #512 |
| F3 | MCP 可给交易附加小票图片 | #513 |

附带修复：MCP `create_transactions` 批量接口不可用。

## 2. Architecture

### 2.1 部署形态

- **SQLite 单容器**，跟随官方 `docker-compose.yml` 默认形态（不选 Postgres：单用户无收益，只多一个容器要运维）
- 全部持久化数据在 `/data` 一个 volume：DB / 附件 / 备份 / JWT 密钥
- VPS 前置 Caddy 反代自动签 TLS（MCP 用 Bearer token，必须 HTTPS）
- Web 端 PWA 已完整实现（`display: standalone` + `share_target` + shortcuts），iPhone 加主屏即独立 App 体验

### 2.2 写入链路（**已实测确认，决定了工作量分布**）

Web 面板与 MCP **共用同一条写入路径**：

```
Web 表单 ─┐
          ├→ POST/PATCH /api/v1/write/ledgers/{id}/transactions
MCP tool ─┘   （MCP 走进程内 httpx ASGITransport self-call，不出 TCP）
               ↓
      _commit_create_tx_fast (_shared.py:532)   ← create，O(1) 快路径
      _commit_write_fast_tx  (_shared.py:665)   ← update
               ↓
      snapshot_mutator.{create,update}_transaction   ← payload → camelCase snapshot item
               ↓
      projection.upsert_tx (projection.py:182)       ← 唯一的行写入实现
               ↓
      sync_changes（事件流，只 INSERT）+ read_tx_projection（读权威源）
```

**关键推论**：改 write endpoint 一处即同时覆盖 MCP + Web。但 **MCP 工具入参签名不走 Pydantic**，
要让 LLM 传得进来，`src/mcp/server.py` 的签名和 docstring 必须另外改。

### 2.3 统计聚合链路

**分类金额不在 SQL 里聚合**，是 `workspace.py:1058-1068` 的 Python dict 累加：

```python
expense_total += amt; slot["expense"] += amt          # 总额/趋势 —— 全额
category_slot["expense"] += amt                       # 分类扇区 —— 唯一需要拆分的
```

**不变式来源**：只拆 `category_slot`、总额照旧加 `amt`，则「月支出总额 = 实付金额」自动成立。

### 2.4 已确认的关键约束

| 约束 | 值 | 证据 |
|---|---|---|
| Web 饼图扇区上限 | **Top 5** + 灰「其他」 | `HomeMonthCategoryDonut.tsx:26,43-44` |
| 预算口径 | 独立 SQL，按全额，不受分类显示影响 | `ledgers.py:587-610` |
| 账户维度聚合 | 刻意读原币 `amount`，**不要**跟随 | `read/_shared.py:436-437` 契约注释 |
| 金额列类型 | 一律 `sa.Float()`，全仓无 `Numeric` | `0018_tx_multi_currency.py:41` |
| 附件存储 | 本地磁盘，无 S3；sha256 去重 | `attachments.py:32-36,103-127` |
| 附件上限 | 64MB，env `ATTACHMENT_MAX_UPLOAD_BYTES` | `config.py:34` |
| MCP 认证 | PAT **只能**进 `/api/v1/mcp`；普通端点显式拒绝 | `deps.py:290-297` |

## 3. Tech Stack

FastAPI + SQLAlchemy + alembic（SQLite/PG 双方言）· React 18 + Vite 5 + pnpm workspace
（`apps/web` + `packages/{api-client,ui,web-features}`）· shadcn/ui + Tailwind · FastMCP（18 工具）
· pytest（16,683 行）+ vitest（11 个纯函数测试文件）

## 4. Baseline / Authority Refs

| 依据 | 路径 |
|---|---|
| 同步契约（**改前必读**） | `docs/SYNC_ARCHITECTURE.md` §4.2 LWW / §4.4 rename cascade / §4.5 增量 merge |
| 改 tx 字段的完整先例 | `alembic/versions/0018_tx_multi_currency.py` + `tests/test_tx_multi_currency.py`（24 个 test） |
| 仓库协作规范 | `CLAUDE.md` |
| 上游原始诉求 | issues #510 / #512 / #513 |

## 5. Compatibility Boundary

| 面 | 边界 |
|---|---|
| 旧数据 | `tax_amount` nullable。存量行 = 无税，行为与今天**完全一致**，无 backfill |
| 官方 App | **放弃支持**。不保证 `/sync/*` 端点的 mobile 兼容。官方 App 读不到税额（其本地表无此列、饼图是本地 SQL `SUM`） |
| backup restore | **必须保真** → `snapshot_builder` 与 `_LEDGER_MERGE_SPECS` 仍需改（`projection.py:663` `rebuild_from_snapshot` 依赖它们） |
| MCP 现有工具 | 18 个签名保持向后兼容；新增工具为增量 |
| REST API | 只增不改，不删端点、不改既有字段语义 |

## 6. 已批准的架构决策

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 税额在饼图的呈现 | **归入一级分类「税与保险」，其下二级「消费税」** | Top 5 名额紧张；与 F2 的分类结构一致；「税与保险」一个扇区同时容纳从消费拆出的消费税 + 单独记的住民税/国保 |
| D2 | 多币种 | 只存原币 `tax_amount`；折本位币值**读时推导**，不落库 | 避免重演 `native_amount` 的联动 bug（`SYNC_ARCHITECTURE.md` §4.5） |
| D3 | 改 `amount` 时税额 | **不联动**，保持独立 + 校验 `0 < tax < amount` | 税是小票上的绝对值，等比缩放会算出 0.5 円 |
| D4 | 预算口径 | **不动**（按全额） | 预算语义是「实际花了多少」；改了会让预算总额与月支出总额对不上。不变式优先于口径统一 |
| D5 | 分享图片记账 | **独立阶段**（阶段 5），不并入阶段 2 | 用户决定 |
| D6 | 部署形态 | SQLite 单容器 + Caddy，跟随官方 | 单用户场景 Postgres 无收益 |
| D7 | 默认分类 | **不改 `seed_service` 语义**（那是 Flutter 仓）；「税与保险」由用户在自建实例自建 | 避免预置地区特有分类 |

## 7. TDD Route

- **TDD mode: off**（会话配置）；decision: **skipped**
- authority: 无用户显式 TDD 要求
- test posture: **变更 + 回归测试同批提交**，不写 test-first RED/GREEN
- reason: 项目要求是「`pytest tests/` 全过才能合代码」（`CLAUDE.md`），属验证门而非 TDD 路线
- verification: 每任务附具体命令与断言（见 §9）

## 8. Ripple Signal Triage

**触发**（schema / contract / persistence / public API / migration / producer+consumer 全中）。

| 角色 | 归属 |
|---|---|
| **真相源** | `read_tx_projection`（`models.py:469`）—— 唯一读权威 |
| **行写入唯一实现** | `projection.upsert_tx`（`projection.py:182`）—— 无绕过路径（已 grep 确认无裸 `db.add(ReadTxProjection)`） |
| **projection→snapshot 反向桥** | `_projection_row_to_tx_dict`（`write/_shared.py:842`）—— **最易漏，见 R1** |
| **下游消费者** | `workspace_analytics`（统计）、`list_budgets_usage`（预算，不受影响）、`CategoryDetailDialog.aggregate`（**第二条浏览器端聚合路径**）、CSV 导出 |
| **双生产端** | Web 表单（2 处 payload 组装重复）、MCP（3 处签名独立） |

**退休决策**：
- **保留** `sync_changes` 事件流 —— Web 写入内部依赖它做 diff，不是 mobile 专属
- **保留** `snapshot_builder` / `_MERGE_SPECS` —— backup restore 依赖
- **不退休** `/sync/push|pull|full` 端点（留着，只是不测试不维护）；退休动作留待未来，本轮不做
- **不新增任何 fallback / adapter**

---

## 9. Tasks

> 执行顺序有依赖：T2 依赖 T1（自托管跑通才能验证）；T4 依赖 T3（分类是税额的归属目标）。

---

### T1 · 自托管基线（0.5 天，**源码安装，不用 Docker**）

**Change Necessity**：无业务代码改动，纯部署。用户明确不使用 Docker，VPS 直接源码安装。

`Dockerfile` 只是打包便利 —— `src/main.py:223-244` 自带静态文件伺服，
`WEB_STATIC_DIR` 指到 `pnpm build` 产物即可，完全不需要容器。

**安装步骤（VPS）**：

```bash
# 1. 系统依赖
apt install -y python3.12 python3.12-venv nodejs npm   # node 20+ 供 pnpm 构建

# 2. 源码 + Python 依赖
git clone <你的 fork> /opt/beecount && cd /opt/beecount
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. 前端构建（产物给 FastAPI 伺服）
cd frontend && corepack enable && pnpm -C apps/web build && cd ..
#   → 产物在 frontend/apps/web/dist

# 4. 配置
cat > .env <<EOF
JWT_SECRET=<32+ 字节随机串>          # 必填，否则首启自动生成到 data/.jwt_secret
DATABASE_URL=sqlite:////opt/beecount/data/beecount.db
WEB_STATIC_DIR=/opt/beecount/frontend/apps/web/dist   # ← 指向 dist，不是 /app/static
DATA_DIR=/opt/beecount/data
ATTACHMENT_STORAGE_DIR=/opt/beecount/data/attachments
BACKUP_STORAGE_DIR=/opt/beecount/data/backups
REGISTRATION_ENABLED=false
EOF

# 5. 迁移 + 启动
.venv/bin/alembic upgrade head      # 期望停在 0019_account_hidden
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8869
```

**systemd 单元**（`/etc/systemd/system/beecount.service`）：
`ExecStart=/opt/beecount/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8869`、
`WorkingDirectory=/opt/beecount`、`Restart=always`、`Environment=TZ=Asia/Tokyo`。

**注意 `WEB_STATIC_DIR`**：`config.py:19` 默认是 `/app/static`（Docker 路径），
源码安装**必须**改指向 `frontend/apps/web/dist`，否则 Web 面板 404。

**前置动作**：临时置 `REGISTRATION_ENABLED=true` 注册首个账号，建完改回 `false`
（单用户自托管的默认姿态）。

**反代**：Caddy（或 Nginx + certbot）终止 TLS 后反代到 `127.0.0.1:8869`。
**MCP 必须走 HTTPS** —— PAT 是 Bearer token，明文传输等于裸奔。

**MCP 切换**：Web 端 `设置 → 开发者` 建 PAT（scope 勾 `mcp:read + mcp:write`，
有效期「永不」），客户端改指 `https://your-domain.com/api/v1/mcp`。
明文 PAT 只显示一次，关闭后只剩 prefix。

**Commands**：
```bash
.venv/bin/alembic current                        # 0019_account_hidden
curl -fsS http://127.0.0.1:8869/healthz
curl -fsS https://your-domain.com/               # 应返回 SPA HTML（验证 WEB_STATIC_DIR）
scripts/backup_sqlite.sh /opt/beecount/data/beecount.db ./backups/sqlite
```

**Acceptance**：
- Web 能登录、能建账本、能记一笔
- MCP `list_ledgers` 返回自建账本
- 备份脚本产出单文件（**不能用 `cp`** —— WAL 模式未提交写入会丢，见脚本头注释）

**Rollback**：`systemctl stop` + 切回上一个 commit + 重新 `pip install`/`pnpm build`。
数据在 `data/`，不受影响。

**Risk**：Caddy 证书、DNS、宿主防火墙/云安全组 443 —— 与代码无关，但阻塞验收。
升级路径是 `git pull && pip install && pnpm build && systemctl restart`（无镜像 tag 可回滚，
靠 git commit 回退）。

---

### T2 · 修 MCP 批量接口（1 小时）

**Change Necessity**：`create_transactions` 当前**完全不可用**（任何输入都失败），不是体验问题。

**根因（已实测复现）**：`list[dict[str, Any]]` 生成的 JSON Schema 是
`{"type":"object","additionalProperties":true}` —— 无 `properties`、无 `required`、无类型约束，
LLM 传 `{"amount":"38.00"}` 时 `Any` 不做转换，撞上 `write_tools.py:472-473` 的 `isinstance` 校验。
单条没事是因为 `server.py:345` 声明 `amount: float`，pydantic 会强转。

**改动**（2 行注解 + 1 个类型）：

1. 新增 `BatchTxItem(TypedDict)`，字段沿用循环体已在读的键：
   `amount: float` / `tx_type` / `category` / `account` / `happened_at` / `note` / `tags` / `currency`（全 `NotRequired`，除 `amount`）
2. `src/mcp/server.py:385` → `transactions: list[BatchTxItem]`
3. `src/mcp/tools/write_tools.py:431` → 同上
4. `src/mcp/tools/write_tools.py:467-501` 循环体**一行不改**

**为什么用 TypedDict 而不是 BaseModel**：BaseModel 会把 item 变成模型实例，`raw.get("amount")` 全炸。
TypedDict 实测同时满足三点 —— schema 带 `"amount":{"type":"number"}` + `"required":["amount"]`；
`"38.00"` 强转 `38.0`；validate 后仍是 plain dict。

**测试**：`tests/test_mcp_tools.py` 现有 12 个测试**完全没覆盖** `create_transactions`，
补：`test_create_transactions_accepts_numeric_string_amount`（传 `"38.00"` 断言成功落库）
+ `test_create_transactions_schema_declares_numeric_amount`（断言生成的 schema 有 `required: [amount]`）。

**Commands**：`python -m pytest tests/test_mcp_tools.py -q`
**Acceptance**：批量传字符串金额成功；schema 含 `required: ["amount"]`。

---

### T3 · 建「税与保险」分类（1 小时，零代码）

**Change Necessity**：分类是 user-global 实体，MCP `create_category` 直接建，**不改任何源码**。

```
税与保险 (expense, 一级)
├── 消费税
├── 所得税      ← 住民税
└── 社会保险    ← 国民健康保険
```

- 不加「财产税」（用户决定：各地差异大，自建即可）
- 命名说明：叫「税与保险」而非「税务」，因为国民健康保険属**社会保険料**不是税金
- 小瑕疵（可选修）：后端 `services/category_icon.py:212-214` 有 `"税" → receipt_long` 规则，
  但前端 `web-features/src/lib/categoryIconMap.ts` 的 `KNOWN_NAMES` 不含该项，会 fallback 成 `category` 字形图标

**Acceptance**：MCP `list_categories` 能看到该分类及其子分类；Web 分类选择器可选中。
**这是 T4 的前置** —— 没有它，T4 的税额降级为独立扇区（仍可用，但形态与 D1 不符）。

---

### T4 · 消费税税额字段（3 天）

#### T4.1 后端字段贯通（1 天）

> 照 `0018_tx_multi_currency` 的完整改动面。先例注释见 `snapshot_builder.py:68-70`。

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `alembic/versions/0020_tx_tax_amount.py`（**新建**） | `revision="0020_tx_tax_amount"`, `down_revision="0019_account_hidden"`；`add_column("read_tx_projection", sa.Column("tax_amount", sa.Float(), nullable=True))`；**无 backfill**（存量=无税）；downgrade 单列 drop |
| 2 | `src/models.py:514` 后 | `tax_amount: Mapped[float \| None] = mapped_column(Float, nullable=True)` |
| 3 | `src/schemas.py:792` / `:818` | `WriteTransactionCreateRequest` / `WriteTransactionUpdateRequest` 各加 `tax_amount: float \| None = None` |
| 4 | `src/schemas.py:520` 附近 | `ReadTransactionOut` 加 `tax_amount: float \| None = None`（`WorkspaceTransactionOut` 继承自动获得） |
| 5 | `src/snapshot_mutator.py:331-337` | create：`if payload.get("tax_amount") is not None: item["taxAmount"] = float(...)`（snapshot 用 camelCase，NULL 不产生 key） |
| 6 | `src/snapshot_mutator.py:457-462` | update：在已有 `for req_key, snapshot_key in (...)` 元组加 `("tax_amount", "taxAmount")`，复用 PATCH 语义（不传=不变） |
| 7 | **`src/routers/write/_shared.py:900-903`** | **`_projection_row_to_tx_dict`** 加 `if row.tax_amount is not None: item["taxAmount"] = row.tax_amount`。**并照 `:895-899` 的先例补注释**说明漏了会怎样 |
| 8 | `src/projection.py:232-272` | `upsert_tx` 的 `values` 加 `"tax_amount": _to_optional_float(payload.get("taxAmount"))` |
| 9 | `src/sync_applier.py:178-210` | `_LEDGER_MERGE_SPECS["transaction"]` 加 `("taxAmount", "tax_amount")` |
| 10 | `src/snapshot_builder.py:50/79/129` | select 列清单 + tuple 解包 + dict 序列化**三处**，位置严格对齐（注释 `:68-70` 已警告） |
| 11 | `src/routers/read/workspace.py:186` + `src/routers/read/ledgers.py:301` | 构造 `ReadTransactionOut` / `WorkspaceTransactionOut` 时传 `tax_amount=row.tax_amount` |

**MCP 侧另需 3 处**（工具签名不走 Pydantic，改 write endpoint ≠ MCP 能传进来）：
- `src/mcp/server.py:342-379` `create_transaction` 签名 + Google-style docstring（LLM 读的就是这段 Args）
- `src/mcp/tools/write_tools.py:142-158` 内部函数签名
- `src/mcp/tools/write_tools.py:190-223` body 组装（有值才加 key）
- 同理 `update_transaction`（`server.py:412` / `write_tools.py:240`）

**校验**（D3）：`0 < tax_amount < amount` 且仅 `tx_type == "expense"` 允许非空。
在 `snapshot_mutator.create_transaction` / `update_transaction` 抛 `ValueError`（沿用现有
`write validation failed: invalid transaction type` 的写法，`snapshot_mutator.py:374`）。

**注意：不做 rescale 联动**（D3）。`rescale_native_amount`（`snapshot_mutator.py:346`）只管
`nativeAmount`，`taxAmount` 改 `amount` 时**不动**。

#### T4.2 统计切片（0.5 天）

只改 `src/routers/read/workspace.py` 一处。

**改动点**：`workspace.py:1012` 的 `tx_query` select 增加 `ReadTxProjection.tax_amount`；
`:1046` 的 tuple 解包同步；`:1058-1068` 的分类累加改为：

```python
if tx_type_val == "expense":
    tax_native = <推导，见 D2>
    if tax_native and TAX_BUCKET_NAME in category_map-ish:
        category_slot["expense"] += amt - tax_native      # 原分类记税前
        tax_slot["expense"] += tax_native                 # 「税与保险」记税额
    else:
        category_slot["expense"] += amt
```

**不要动**的三行（不变式来源）：`workspace.py:1050-1056` 的 `expense_total` / `slot["expense"]`。

**D2 推导式**：`tax_native = native_amount * (tax_amount / amount)`，在 Python 循环里算，不落库。
`amount == 0` 时按 0 处理（不产生税切片）。

**`bucket_cat`（异常归因，`:1066-1068`）**：口径需一并明确 —— 建议税前进原分类、税额进「税与保险」，
与主切片一致，否则异常归因会和饼图对不上。

**必须单独处理的第二条聚合路径**：
`frontend/apps/web/src/components/dialogs/CategoryDetailDialog.tsx:600-669` 的 `aggregate()`
从**原始交易数组**在浏览器 `reduce`，且：读 `tx.amount`（原币，不过滤 `exclude_from_stats`）、
1000 条截断（`CATEGORY_STATS_LIMIT`，`GlobalEditDialogs.tsx:34`）。
→ 「点进分类详情」看到的数会和饼图对不上。**T4.2 必须一并修**（至少让 expense 切片读折本位币并减税）。

**不受影响（明确不做）**：预算（`ledgers.py:587-610` 独立 SQL，D4）、账户维度聚合（`workspace.py:569-608`，
契约要求读原币）、`_projection_totals`（`read/_shared.py:429-480`，summary 保持全额）。

#### T4.3 Web 前端（1 天）

| 改动 | 位置 | 要点 |
|---|---|---|
| 类型 | `packages/api-client/src/types.ts:568` `TxPayload` + `:170` `ReadTransaction` | 各加 `tax_amount?: number \| null` |
| 表单状态 | `packages/web-features/src/forms.ts:3` `TxForm` + `:95` `txDefaults()` | 加 `tax_amount: string`（字符串，同 `amount`）+ 默认 `''` |
| **输入框** | `packages/web-features/src/features/TransactionsPanel.tsx:454-481` | 金额字段下方加「税额」框，仅 `expense` 显示 |
| 详情弹窗 | `apps/web/src/components/dialogs/TransactionDetailDialog.tsx:89-121` | Hero 区大金额下方加一行：`税前 X · 消費税 Y · 合计 Z` |
| i18n | `apps/web/src/i18n/{en,zh-CN,zh-TW}.ts` | **三语强制 parity**，`en.ts` 是 source of truth |

**三个必踩的坑**：

1. **payload 组装重复两处** —— `TransactionsPage.tsx:1456-1477` 和 `GlobalEditDialogs.tsx:270`。
   只改一处 → 全局编辑弹窗（CmdK / CalendarPage 入口）**静默丢字段**。编辑回显同理：
   `TransactionsPage.tsx:1974` + `GlobalEditDialogs.tsx:138`。校验逻辑也是复制粘贴两份
   （`TransactionsPage.tsx:1375-1403` / `GlobalEditDialogs.tsx:204-234`），新字段校验要写两遍。

2. **金额输入框是 text 型** —— `TransactionsPanel.tsx:456-460` 无 `type="number"` / `inputMode`。
   iPhone PWA 上税额框**必须自己补 `inputMode="decimal"`**，否则弹全键盘。仓内 3 处先例：
   `TxDraftList.tsx:367-369`、`TransactionsPage.tsx:2144-2148`、`AccountsPanel.tsx:1263-1264`。

3. **`input.tsx` 不注入任何属性** —— `packages/ui/src/ui/input.tsx:5-15` 只包 className + `{...props}`，
   `type` / `inputMode` 得自己写。

**饼图**：D1 决定归入「税与保险」单一扇区 → 分类名已含它，**`HomeMonthCategoryDonut.tsx` 无需改动**。
若后续想单独看消费税，才需要动 `TopCategoriesList.tsx:20`。

#### T4.4 测试（0.5 天）

照 `tests/test_tx_multi_currency.py`（24 个 test，24 类覆盖）模板写：

- **迁移**：`test_projection_has_tax_amount_column`（`sa.inspect` 断言列存在 + nullable）
- **写入**：`test_upsert_tx_writes_tax_amount` / `test_upsert_tx_legacy_payload_leaves_null` /
  `test_web_create_tx_with_tax_lands_in_projection`
- **merge 契约（CLAUDE.md 硬要求）**：`test_mobile_push_transaction_partial_update_keeps_tax_amount`
  —— 防 2026-04 那类「漏 merge 某字段」bug
- **反向转换**：`test_projection_row_to_tx_dict_carries_tax_amount`（锁住 R1）
- **快照**：`test_snapshot_builder_keeps_tax_amount`
- **读端**：`test_read_transactions_expose_tax_amount`（同时打 `/read/ledgers/{id}/transactions` 和 `/read/workspace/transactions`）
- **不变式**：`test_analytics_total_includes_tax`（总额 = 实付）+ `test_analytics_category_slices_split_tax`
- **降级**：`test_analytics_tax_fallback_bucket_when_no_tax_category`
- **校验**：`test_reject_tax_not_less_than_amount` / `test_reject_tax_on_income`
- **回归锁**：`test_budget_usage_unchanged_by_tax`（D4）/ `test_account_dimension_keeps_amount`
- **CSV**：`test_csv_export_includes_tax_column`
- **前端**：`pnpm -C apps/web test`（i18n parity 会红，必须三语齐）

**Commands**：
```bash
python -m pytest tests/ -q
cd frontend && pnpm -C apps/web test
```

---

### T5 · MCP 附件（F3 / #513，1 天）

> **落盘时补记**：F3 在 §1 声明为目标却没有对应任务，且原 T5 错误引用了
> 「T2 已实现的上传路径」（T2 是批量修复，不含上传）。这是计划自身的缺陷。

**Change Necessity**：REST 层**全部现成** —— `/attachments/upload`（multipart
`ledger_id` + `file`）、sha256 去重、`attachment_files` 表、`attachments_json`
关联、孤儿 GC、Web 端展示链路都已在跑。缺的只是 MCP 那一层接线。这不是
「帮我加个功能」，而是「你们已经有这个能力，只是没接到 MCP 上」。

**真正的阻塞点**（调查结论已修正）：

1. **`deps.py:290-297` 的 `get_current_user` 显式拒绝 PAT** ——「PAT can only be
   used for MCP endpoints」。所以 MCP **不能**直接调 `/attachments/upload`，
   必须走 §2.2 那套 self-call 短期 JWT（scope `SCOPE_APP_WRITE`，正好满足
   `attachments.py:28-29` 的 `_WRITE_SCOPE_DEP`）。
2. ~~`_self_call` 只支持 `json=`~~ —— **这条是错的**。`write_tools.py:79` 是
   `client.request(method, path, headers=headers, **kwargs)` 直接透传，httpx
   原生支持 `files=` / `data=`，multipart 无需改底层。已实测确认 httpx
   `request()` 签名含 `data` / `files`。

**改动**：

| # | 文件 | 改动 |
|---|---|---|
| 1 | `write_tools.py` | 新增 `_upload_attachment(...)`：算 sha256 → self-call multipart(`files=`/`data=`) → 返回 `AttachmentUploadOut` |
| 2 | `write_tools.py` | 新增 `attach_receipt(user, *, sync_id, image_base64, ...)`：base64 → bytes → 上传 → 组装 `AttachmentRef` → PATCH 交易的 `attachments` |
| 3 | `write_tools.py` | 新增 `create_transaction_with_receipt(...)`：建交易 + 附图（一步到位，避免「先建后补」中间态） |
| 4 | `server.py` | 注册两个新工具 + docstring（LLM 读的就是这段 Args） |
| 5 | `read_tools.py` | `list_transactions` 的 `_serialize_tx` 当前**不含 attachments**（只有 `get_transaction` 有）→ 补上，否则列表里看不到图 |

**附件对象结构**（`types.ts:126-135` 为准，服务端 `schemas.py:519` 是弱类型
`list[dict[str, Any]]`，零校验）：`{fileName, originalName, fileSize, sortOrder,
cloudFileId, cloudSha256}`。`fileName` 存的是 `<file_id>_<原名>` 拼接形式
（见 `TransactionsPage.tsx:1329-1333`）。

**base64 输入处理**：接受裸 base64 与 `data:image/jpeg;base64,` 前缀两种
（与官方已上线的同名工具保持一致）。**必须在上传前解码 + 校验大小**
（`attachment_max_upload_bytes`，默认 64MB），否则一个超大 base64 串会在
解码时吃满内存。

**孤儿 GC 注意**：`projection.py:768-788` 的 `_extract_tx_cloud_file_ids` 按
`cloudFileId` 反查，`gc_orphan_attachments` 会清掉没被任何交易引用的文件。
所以**上传和 PATCH 必须在同一个操作里完成**，中间态太长会有极小概率被 GC 扫走
（默认延迟，且 `attach_receipt` 是同步的，不构成实际风险）。

**测试**：新增 `tests/test_mcp_attachments.py`
- `test_self_call_supports_multipart`（打桩验证 files/data 透传）
- `test_attach_receipt_uploads_and_links`
- `test_create_transaction_with_receipt_creates_tx_and_attachment`
- `test_attach_receipt_rejects_oversized`
- `test_attach_receipt_rejects_unknown_tx`

**Commands**：`python -m pytest tests/test_mcp_attachments.py -q`

**验收**：MCP 传一张小票图 → 交易带上附件，Web 详情弹窗能看到缩略图。

---

### T6 · PWA 分享图片记账（0.5 天，独立阶段 — D5）

**Change Necessity**：接收管道已铺好，只差接上（上游自己留的 TODO，`ShareIncomingPage.tsx:103`）。

- 现状：service worker 已缓存 share-target 投递的文件 → `ShareIncomingPage.tsx:40-107` 能读到图片，
  但 `:106` 直接弹 `t('pwa.share.imageNotYet')` 就跳转
- 接上：拿到 `File` → 走**前端**已有的 `uploadAttachment`（`packages/api-client/src/attachments.ts`）
  + `createTransaction` → 建交易 + 写 `attachments_json`
- 依赖：前端上传链路已现成，**不依赖 T5**（T5 是 MCP 侧接线，前端各走各的）
- 新增 i18n key 三语：`pwa.share.imageReady` / `pwa.share.imageFailed`

**验收**：iPhone 相册长按分享小票给 BeeCount → 落地一笔带图交易。
**Risk**：iOS share target 的 `launchQueue` 时序（`ShareIncomingPage.tsx:121-122` 已注释说明有 fallback 竞态）。

---

## 10. Risks

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| **R1** | 漏改 `_projection_row_to_tx_dict`（`write/_shared.py:842`） | **Web PATCH 更新静默抹掉税额**，不报错。先例：`:895-899` 注释记录 `nativeAmount` 同样踩过 | T4.1 #7 强制改 + 加注释 + `test_projection_row_to_tx_dict_carries_tax_amount` |
| **R2** | 漏改 `_MERGE_SPECS` / `snapshot_builder` | mobile 增量 push 或 backup restore 时税额丢失 | T4.1 #9/#10 + merge 契约测试 |
| **R3** | 前端 payload 只改一处 | 全局编辑弹窗静默丢字段 | T4.3 坑 1 + 逐处核对 |
| **R4** | 忘记 `CategoryDetailDialog` 第二条聚合路径 | 分类详情与饼图数字对不上 | T4.2 显式任务项 |
| **R5** | Top 5 名额 | 「税与保险」挤掉真实分类进「其他」 | D1 已缓解（1 个扇区装全部税）；若仍不够，后续可把上限提到 6-8 |
| **R6** | 上游继续更新导致 merge 冲突 | 长期维护成本 | 每阶段独立 commit；改动尽量写成可上游接受的形状；`git remote -v` 保留 upstream 定期 rebase |
| **R7** | VPS 数据丢失 | 全部账本 + 附件丢失 | T1 验收含备份脚本；配 cron 定期跑 + 异地（`scripts/backup_sqlite.sh`） |

## 11. Retirement

| 项 | 处置 |
|---|---|
| MCP `create_transactions` 的 `isinstance` 校验 | **保留**。TypedDict 修的是 schema/强转，校验本身是防御性边界 |
| `/sync/push` `/sync/pull` `/sync/full` mobile 端点 | **保留不删**，本轮不测试不维护。退休动作需等确认不再使用 App 且 backup restore 已验证保真，另开任务 |
| `CategoryDetailDialog.aggregate` 的原币口径缺陷 | T4.2 **修**（改读折本位币 + 减税）。同源的「不过滤 `exclude_from_stats`」属既有缺陷，**本轮不修**（超出范围，会动到其他口径），留 TODO |
| 上游 issue #510 / #512 / #513 | 保留为需求来源。本计划不改写它们的状态 |

## 12. 阶段顺序与工作量

> **执行顺序已调整（用户决策：全部改完再部署）**。原计划把部署放最前是为了尽早暴露
> 镜像构建/迁移/证书类环境风险，但这些风险与功能改动**可分离** —— 本地
> `pytest` / `alembic upgrade head`（本地 SQLite）/ `pnpm build` / `pnpm test`
> 均不需要 docker，覆盖了绝大部分验证面。部署收束到最后一次完成。

| 阶段 | 内容 | 量 | 执行者 | 解除的痛点 |
|---|---|---|---|---|
| **T2** | MCP 批量 bug | **1 小时** | 本地 ✅ | **批量导入不通** |
| **T4** | 消费税字段 | 3 天 | 本地 ✅ | 饼图看到税 |
| **T5** | MCP 附件 | 1 天 | 本地 | 每笔附小票 |
| **T6** | PWA 分享图片 | 0.5 天 | 本地 | 小票拍照进账 |
| **T1** | 一次性部署到 VPS | 0.5 天 | **用户** | 摆脱官方托管 |
| **T3** | 用 MCP 建「税与保险」分类 | 1 小时 | **用户**（部署后） | 住民税/国保有归处 |

**T3 移到部署后的原因**：分类是 user-global 实体，MCP `create_category` 写入的是**当前连接**的实例。
当前 MCP 连的是官方托管版（仓库代码无附件 MCP 工具即为佐证），部署前建会建到错误的实例。

### 12.1 本地验证面（不依赖 docker）

| 验证 | 命令 | 覆盖 |
|---|---|---|
| 后端全量测试 | `python -m pytest tests/ -q` | 后端全部行为 |
| 迁移可跑通 | `alembic upgrade head`（本地 SQLite） | `0020_tx_tax_amount` 正确性 |
| 前端类型 + 构建 | `pnpm -C apps/web build` | TS 编译 + 产物 |
| 前端测试 | `pnpm -C apps/web test` | i18n 三语 parity 等 |

**本地跑不了、只能等部署的**：`docker build`、Caddy TLS、VPS 网络与持久化。

## 13. Execution Route

- **inline** —— T2/T3 小且与主仓强耦合；T4 的 4 个子任务共享同一批文件，拆给多个 agent 会产生写冲突。
- **T1 / T3 交用户执行**：VPS、DNS、证书、docker 均不在本机（`docker` 命令不可用），
  T3 另需 MCP 指向自建实例。
- `User confirmation required: no`（执行路线无需额外授权；T1 的 VPS 环境细节已在 T1 Acceptance 列为用户检查项）