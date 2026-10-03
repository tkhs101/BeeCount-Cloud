# 自托管二改计划：消费税税额 + MCP 附件 + 自建云端

- 日期：2026-10-03
- 基线：`3d9f64b`（= tag `1.6.7`，与上游 `TNT-Likely/BeeCount-Cloud` 完全同步，0 ahead / 0 behind）
- 目标读者：**无本仓历史上下文的工程师**
- **执行状态：T2 / T4 / T5 / T6 已完成并验证；T1 / T3 待用户在 VPS 上执行**

---

## 0. 执行结果摘要

| 阶段 | 内容 | 状态 | 证据 |
|---|---|---|---|
| T2 | MCP 批量接口修复 | ✅ | 反向验证：bug 版本下 2 个测试变红 |
| T4 | 消费税税额（后端 11 触点 + 统计切片 + Web + MCP） | ✅ | 反向验证：漏 `_projection_row_to_tx_dict` → R1 测试变红；不做切片 → 3 个测试变红 |
| T5 | MCP 附件接线 | ✅ | 13 个测试走**真实 self-call** 未打桩 |
| T6 | 附件添加入口 + PWA 分享小票 | ✅ | `pnpm build` 通过 |
| CSV | 导出/导入双向税额 | ✅ | 端点级测试：upload → preview(回传 mapping) → execute → 落库 |
| — | **独立审查** | ✅ | 见 §0b，3 个 🔴 + 2 个 🟡 已修 |
| **端到端** | 真实服务 + 全新迁移 + 构建后前端 | ✅ **29/29** | 见下 |
| T1 | 源码安装部署到 VPS | ⏸ 待用户 | 本机无 docker；VPS/DNS/证书在用户侧 |
| T3 | 用 MCP 建「税与保险」分类 | ⏸ 待用户（部署后） | 依赖 MCP 指向自建实例 |

**最终验证**：后端 505 passed · 前端 build 通过 / 79 passed 1 failed（存量 i18n
parity）· 真实服务上税额校验返回 **400 + 可读报错**（非 500）· CSV 导出第 13 列
税额正确落值。

### 端到端验证结果（真实 uvicorn + 全新 SQLite）

```
餐饮 2982（税前） + 税与保险 298（消费税） = 3280（实付）
```

### 0b. 独立审查发现并已修复的问题

审查代理只报告不修改，本计划逐条**独立核实**后确认属实，再修。审查者指出的
Y2（tx_count 重复计数）在我读码自查时已先行修掉。

| 级别 | 问题 | 状态 |
|---|---|---|
| 🔴 R1 | 两个 write **快路径**缺 `ValueError → HTTPException(400)` 包装（慢路径有）。税额校验是第一条能真正穿透到快路径的 mutator ValueError，于是全部变成 **500** | ✅ 已修 + 5 场景回归测试 |
| 🔴 R2 | 分享小票的自动挂载 `useEffect` 被插进箭头函数体内（try/catch 都 return → 不可达语句）。`tsc`/`vite` 抓不到 | ✅ 已修 + AST 守护测试 |
| 🔴 R3 | `FieldMappingPayload.to_internal()` 漏 `tax_amount` → 用户点一次「应用列映射」整份导入的税额静默丢失 | ✅ 已修 + 端点级测试 |
| 🟡 Y1 | MCP `get_analytics_summary` 不剥税 → MCP 报「餐饮 3280」而界面显示「餐饮 2982 + 税与保险 298」 | ✅ 已修：换算函数提到 `read/_shared.py` 两边共用同一函数对象 |
| 🟡 Y2 | 税额分类与自身同名时 `tx_count` 被加两次 | ✅ 已修（审查期间自查先行发现） |
| 🟡 Y3 | 「税与保险」是虚拟扇区，点进详情页合计必然小于扇区值 | ✅ 已修：标题下加说明 |
| 🟡 Y6 | `create_transaction_with_receipt` 把 base64 解码两遍（峰值内存翻倍） | ✅ 已修：拆 `_attach_receipt_bytes` |
| 🟡 Y7 | 历史脏数据（`/sync/push` 推入）会让整笔交易无法编辑 | ✅ 已修：降级为「剔除并告警」而非抛错 |
| 🔵 B1/B3/B4/B5/B8/B11 | 死 i18n key、附件解析重复、`get_transaction` 冗余裸 `json.loads`、返回里的 `_meta` 噪音、迁移文件缺换行、未用变量 | ✅ 全部清理 |
| 🔵 B9 | 迁移测试是「源码字符串 grep」，换任何实现都会通过 | ✅ 重写为**真执行迁移**：建表 + 塞存量行 + 跑 `upgrade()` + 断言可空且存量原样保留。已验证鉴别力：加一段内联回填即变红 |
| 🔵 B10 | `tax_in_base_currency` 的 6 个防御分支**零覆盖** —— 而它们正是「脏数据下不变式仍成立」的唯一保证 | ✅ 全部补齐。顺带发现 **NaN/inf 会静默穿过** `tax <= 0` 守卫（NaN 与任何数比较都返回 False），已显式 `math.isfinite` 挡掉 |
| 🔵 B12 | CSV 测试用裸 `split(",")` 解析整行 | ✅ 改用 `csv` 模块 |
| — | **R5 饼图扇区上限（原计划标为「用户反馈后再定」）** | ✅ **发现这是会让功能目标落空的硬伤，已修** —— 见下 |

### 第四轮：后端同模式排查 + 护栏

用同一把尺子扫了后端所有 `ReadTxProjection.amount` 的使用点，**没有第四个副本**：

| 位置 | 判定 |
|---|---|
| `workspace.py:581/584` 账户维度聚合用原币 | **契约明确要求**如此（`read/_shared.py:436-437`：「账户维度仍读 amount 原币，不要仿此改」）—— 不是 bug |
| `snapshot_builder.py:53` 序列化、净值历史 | 原币本就正确（snapshot 契约就是原币） |
| `workspace.py:1033` 税额比率来源 | 本 fork 新增，取原币是**必需的** |
| `amount_min/max` 过滤器用原币 | **不是 bug**。查过 `TransactionRow.tsx`：交易列表的主数字就是**原币**（外币才加 `≈ 本位币` 副标）。所以「按金额筛选」用原币与用户看到的完全一致；若擅自改成本位币反而会引入「筛不出自己看到的条目」的新 bug |

**「先查清再改」这条在这里救了场** —— 看代码时 `amount_min/max` 用原币而聚合用本位币，
第一反应是漏改；查完才发现显示侧本来就是原币，两者是一致的。

### 第四轮：金额口径集中 + 护栏

识别出「客户端重复实现 server 口径」会出现三次之后，两个 helper 还散在不同文件里 ——
等于**没给后来者一个正确的落点**。补上：

- `frontend/packages/web-features/src/lib/amountBasis.ts` 收拢 `baseAmount` /
  `taxInBaseCurrency` / `splitTax`，两处原有实现改为引用，公式不再有两份
- `frontend/apps/web/src/amountBasisGuard.test.ts` 扫全部前端源码，找出**参与金额
  运算的 `.amount`**（加法 / `+=` / `reduce` / `Math.abs|max|min`），命中就必须
  引共享口径或在 ALLOW 表写明理由

**护栏刚立就抓到两个新的**（年报「最大支出」卡用原币显示却配本位币符号，
选择逻辑已改而显示没改，同一功能内部前后不一致）—— 已修。

ALLOW 表刻意做得宽（宁可多报），代价是每加一个真实聚合点可能要补一条 ——
但它同时是一份**已复核记录**。已验证：塞一个 `reduce((s, t) => s + t.amount)`
进去，护栏立刻变红。

### 第九轮：PWA 分享链路（又抓到一个自己写的 bug）

补上最后一个「已实现但从未端到端验证」的功能:相册分享小票 → SW 缓存 →
自动挂成附件。浏览器冒烟一跑就发现它是**坏的**:流程打开了快速新建对话框,
但附件从未挂上 —— 交易建出来了,`attachments` 是 null,服务器上还留了个没人
引用的孤儿文件。

**根因是 React 异步 effect 的顺序写反了**(我自己写的):

```js
setPendingAttachmentUpload(null)   // 同步清空 → 立刻重渲染 → cleanup 置 cancelled
;(async () => {
  const uploaded = await onUploadTxAttachments([file])
  if (cancelled) return             // ← 结果被丢弃
  setTxForm(...)                    // ← 永远执行不到
})
```

附带 `onUploadTxAttachments` 不是 `useCallback`,身份每次渲染都变,effect 反复
重跑。**单测和 tsc 抓不到**:类型合法,逻辑看起来也合理。

这条路径上此前已经有一次同类事故(R2,effect 被插进函数体)。两次都发生在
同一个文件、同一个异步 effect 上 —— 教训是这个文件里的异步 effect 需要按
「状态更新顺序」逐个复核,不能靠读起来通顺。

---

### 第五轮：备份/还原链路（发现一个上游缺陷）

排查税额字段的备份链路时发现:管理面板的「备份」按钮**对任何方案 B 之后新建
的账本必然 404**。

`create_backup` 去 `sync_changes` 找一条 `entity_type == "ledger_snapshot"` 的
行,但方案 B(projection-as-authority)之后 `_commit_write` 和 `/sync/push` 都
不再写这种行(`SYNC_ARCHITECTURE.md` §1 明确说了)。实测新账本：

```
LEDGER_SNAPSHOT_ROWS=0     ALL_ENTITY_TYPES=['ledger','transaction']
BACKUP_CREATE_STATUS=404   "No snapshot for ledger"
```

已修:改用 `snapshot_builder.build(db, ledger)` 从 projection **现场构建**
(这才是文档指定的权威来源,`/sync/full` 走的就是它),顺带比原来更正确 ——
旧写法读的是可能很旧的存量行,现场构建拿到的才是当前状态。

只改这一处硬失败。`admin.py` 的快照信息端点有优雅降级、`sync/full` 只把旧行
用于墓碑检测,那两处正常。

**三条备份路径现在的状态**：

| 路径 | 机制 | 状态 |
|---|---|---|
| 管理面板「备份」 | projection → snapshot | ✅ 本轮修复 |
| 定时 rclone 备份 | SQLite `VACUUM INTO` | ✅ 不依赖 snapshot |
| `scripts/backup_sqlite.sh` | `sqlite3 .backup` | ✅ 不依赖 snapshot |

自托管最怕「以为备份了其实没有」,所以补了端到端往返测试:建备份 → 删交易 →
还原 → 断言税额、备注、统计切片、总额全部回到原样。这条链路此前零测试。

---

### 第四轮：给 fork 加维护者索引

`CLAUDE.md` 追加「fork 特有改动」一节：税额字段的改动地图、三条本 fork 独有的坑
（反向桥静默抹数据 / 别写 `t.amount` / hook 别插进函数体）、以及两个环境坑
（`WEB_STATIC_DIR` 与 `TAX_CATEGORY_NAME`）。目的是让未来的维护者（含 AI 助手）
不必重新踩一遍。

---

### 第三轮：系统性排查「客户端重复实现 server 口径」

上一轮修了 `CategoryDetailDialog.aggregate` 的**原币直接相加**。这轮用
`grep -rn "reduce((s"` 把前端所有金额聚合点扫了一遍，发现**同一个 bug 的第二个
副本**，以及一个更值得记的结论：

| 位置 | 问题 | 状态 |
|---|---|---|
| `annual-report/data/aggregate.ts` | 年度总收支 / 月度趋势 / 时段分布 / 周末支出 / 分类排行 **全部**用原币 `t.amount`。单币种账本无感，一有外币交易就把 CNY 和 JPY 加在一起 | ✅ 修（15 处聚合点统一走 `baseAmount()`）|
| `CategoryDetailDialog.aggregate` | 税额没按比率折本位币（50 CNY / 税 5 / 汇率 20 → 算出 995 而非 900）| ✅ 修 |
| `CategoryDetailDialog.aggregate` | 不过滤 `exclude_from_stats`，笔数用 `transactions.length` | ✅ 修 |
| `CategoryDetailDialog` / `annual-report` | **完全没有测试** —— bug 藏身之处 | ✅ 补 13 个 |

**结论比单个修复更重要**：客户端重复实现 server 的金额口径，这个模式已经
出现**三次**。前两次是修完一个才发现另一个，第三次是主动 grep 才找全。建议
后续加聚合逻辑时，要么从 server 拿聚合结果，要么复用已抽出的
`baseAmount` / `taxInBaseCurrency`，不要就地写 `t.amount`。

配套还发现 `TransactionLite` 这个精简类型**根本没带折算字段** —— 聚合想用
`native_amount` 也用不了，已补上。

### 其余本轮修复

- **i18n parity（存量失败）**：`accounts.balance.adjust.*` 8 条 + 1 条按钮文案，
  en 和 zh-CN 都有、唯独 zh-TW 没有。它让 `pnpm test` 一直是红的 —— 意味着
  「测试失败」这个信号被稀释,真问题混在里面容易被忽略。补齐后前端 **114 全绿**。

---

### R5 · 饼图扇区上限：会让核心诉求直接落空

扇区是**按金额排序**取前 N（原本 N=5），而消费税扇区的金额天然最小。真实数据
实测：

```
金额降序排名: 住房 95000 → 餐饮 54982 → 交通 14000 → 购物 9000 → 税与保险 298
```

税额比倒数第二名小 **30 倍**。只要再有几个分类，它就会被并进灰色「其他」，
**界面不会有任何异常表现** —— 用户最初说的「UI 上更清楚我交了多少钱税」在
原实现下根本看不到。

**两步修复，第二步才是关键**：

1. 上限 5 → 8，与官方 App 的 `category_pie_chart._maxSlices = 8` 对齐；调色盘
   扩到 8 色；排行榜 `TopCategoriesList` 同步提到 8（两处对不上会让同一份数据
   呈现两种口径）。
2. **钉住**：`buildDonutSlices` 新增 `alwaysShow`，消费税分类不参与排名竞争，
   恒占一个扇区。

**只做第 1 步不够** —— 我先只做了 1，自己写的测试立刻抓出来：9 个分类时税额
排第 9，Top 8 照样装不下。排名制对「天生就小的扇区」本质上不可靠，必须显式钉住。

**又一次测试盲区**：纯函数测试全绿时，把组件里的 `[TAX_CATEGORY_NAME]` 清成
`[]` 依然不会被发现（测试自己显式传参）。补了一条解析组件 AST 的接线断言，
已验证去掉接线即变红。扇区切分逻辑也从 `useMemo` 里抽成纯函数 `buildDonutSlices`
—— 此前只能靠肉眼看图形。

**刻意保留（已评估，非遗漏）**：

| 项 | 理由 |
|---|---|
| Y4 `attach_receipt` 先上传后 PATCH | 失败会留孤儿文件。单用户自托管下 PATCH 几乎不会失败；彻底修需要 append-only 的附件端点或写前鉴权，改动面远大于收益 |
| **Y5 共享账本里非所有者成员无法用 MCP 改/删/附图** | 影响 `update_transaction` / `delete_transaction` / `attach_receipt` **三个**工具 —— 它们都用 `ReadTxProjection.user_id == user.id` 找交易，而 projection 的 `user_id` 写的是**账本所有者**（`upsert_tx(user_id=ledger.user_id)`），所以 Editor 成员会一律拿到 "Transaction not found" | **不改**。单用户自托管零影响；改鉴权语义要动三个函数的授权路径，风险高于收益，而本项目部署形态根本用不到共享账本。已用 `test_attach_receipt_cannot_cross_users` 锁住「不越权」这一侧 |
| Y8 `/sync/push` 无法清除税额 | `_merge_from_spec` 丢掉 payload 的 `None`，与 `nativeAmount` 同语义，可接受 |
| B7 sniff 的 `"tax"` 别名可能误判英文账单为 BeeCount 格式 | 阈值 8/12 仍需命中 8 个表头，误判概率极低；改别名反而可能漏掉真导出 |
| B9 迁移测试是弱测试 | 断言源码字符串。改为对 alembic op 打桩属另一个工作量级，当前至少锁住了「无回填」这个关键属性 |
| B12 CSV 行用裸 `split(",")` 解析 | 仅测试代码；已在新写的测试里改用按列构造 |

**R2 的守护测试自己也踩了两次坑**：第一版用「parent 在直接语句里」判定 →
`const [x,setX]=useState()` 全是假阳性；第二版「不钻进嵌套函数」→ 恰恰漏掉
藏在嵌套函数里的那个 bug。把 bug 放回去验证过，现在会红。

**R2 的守护测试自己也踩了两次坑**：第一版用「parent 在直接语句里」判定 →
`const [x,setX]=useState()` 全是假阳性；第二版「不钻进嵌套函数」→ 恰恰漏掉
藏在嵌套函数里的那个 bug。把 bug 放回去验证过，现在会红。

**过程中的一次操作失误**：误跑 `ruff check --fix` 全仓，自动改了 74 个与本次
改动无关的存量文件。已全部回滚，并修掉其中一处两条 import 被挤到同一行的
**语法错误**（靠 `.pyc` 缓存侥幸未在测试中暴露）。最终 diff 干净。

### 过程中修正的自身错误

1. **T2**：第一版测试断言「schema 拒绝 0/负数」——实测 `float` 会放行，只有 `required` 生效。改为如实锁定边界。
2. **计划漏项**：F3（MCP 附件）在 §1 声明为目标却无对应任务，且 T5 错误引用「T2 已实现的上传路径」。已补 T5 任务并修正引用。
3. **调查误判**：曾记「`_self_call` 只支持 `json=`，需先加 multipart」。实测是 `**kwargs` 透传，httpx 原生支持 `files=`/`data=`。计划已更正。
4. **Y2 的第一版修复是错的**：把金额和 count 一起跳过了，被测试当场抓住（总额变成 2982 而非 3280）。

### 自审中发现并修掉的真实边界

**交易金额全链路没有正数校验**（只有 budget 有 `> 0`），而 `/sync/push` 不经过
`snapshot_mutator`，负金额 + 正税额的脏数据能直接落进 projection，原换算函数会
算出**负税额切片**。现在 `tax_in_base_currency` 对 `base <= 0` 返回 0，并把结果
夹在 `[0, base]` 内。

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
| `CategoryDetailDialog.aggregate` 的原币口径 | T4.3 **修**（改读折本位币 + 减税）。同源的「不过滤 `exclude_from_stats`」属既有缺陷，**本轮不修**（会动到其它口径），留 TODO |
| `pwa.share.imageNotYet` | **保留未删** —— OCR 识别能力确实仍只在 mobile 端，现在只是至少能把原图存下来 |
| 上游 issue #510 / #512 / #513 | 保留为需求来源。本计划不改写它们的状态 |

## 11b. 已知缺口（本轮不做，明确记录）

| 缺口 | 影响 | 状态 |
|---|---|---|
| ~~MCP 无 `create_budget` 工具~~ | — | ✅ 本轮已补（MCP 工具 20 → 21） |
| **分类预算看不到被剥走的消费税** | 饼图「税与保险」= 1298（住民税 1000 + 消费税 298），分类预算 used = 1000 | **刻意保留**的语义边界，已用测试锁死。理由：预算是「每笔支出恰好计一次」的分区（餐饮 3280 + 税与保险 1000 = 4280 = 真实总支出）；让预算也吃那 298 会让同一笔被计两次，总额变 4578，分区性质被破坏。想要「本月税务支出」这个数字应看饼图或 MCP 的 `tax_total`，不是分类预算 |
| ~~`CategoryDetailDialog` 不过滤 `exclude_from_stats`~~ | 分类详情合计含「不计入统计」的交易，与饼图对不上 | ✅ 本轮已修（顺带修了外币税额未折本位币的 bug） |
| `CategoryDetailDialog` 1000 条截断 | 大分类的统计只算前 1000 笔 | 存量缺陷，已有 `setCategoryStatsTruncated` 提示 |
| MCP 批量接口不支持附件 | `create_transactions` 不能带图 | 需逐笔 `attach_receipt` 或用 `create_transaction_with_receipt` |
| i18n parity 测试存量失败 | CI 的 `pnpm test` 目前就是红的（`accounts.balance.adjust.*` 缺失，与本改动无关） | 存量，本轮未修 |

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