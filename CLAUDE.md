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
| `pytest -q` | 绿 | **绿** | **885 passed** |
| `mypy src` | 89 errors | **60 errors** | 比上游还少 29 个;**这条线到此为止**,见下文 |
| `ruff check src tests alembic` | 1421 | **1253** | **1420 里 1131 条(80%)是同一条 `F405`**(桶导入)—— 系统性问题。已清掉可自动修复的 168 处(`F401` + `I001`) |
| 前端 `build` + `test` | 绿 | **绿** | **316 passed**(2026-10-12 实跑 build,12s) |

### ⚠️ ruff 的 1420 条:先看构成再决定动不动(2026-10-12)

`ruff check src tests alembic` 报 1420 条,**看起来像一堆技术债,其实不是**:

| 规则 | 条数 | 性质 |
|---|---|---|
| `F405` undefined-local-with-import-star-usage | **1131** | **80% 都是这一条** —— CLAUDE.md 定的 `_shared.py` + `__init__.py` 桶导入模式的必然结果。**要动就得改路由组织约定,不是清理能解决的** |
| `I001` unsorted-imports | 86 | 纯排序 |
| `F401` unused-import | 75 | 未用 import |
| `E741` ambiguous-variable-name | 35 | `l` / `O` / `I` 这类 |
| `E402` module-import-not-at-top | 26 | 多是**函数内延迟 import**(故意的) |
| `B904` raise-without-from-inside-except | 20 | 见下 |

**已做**:`--select F401,I001 --fix` 一次清掉 168 处,1420 -> **1253**,
净删 32 行。验证:885 测试全过 + 主要模块全部能 import + mypy 不变。

⚠️ **自动删 import 的风险在运行时动态引用**,所以只跑单测**不够** ——
补了 `import src.main / projection / sync_applier / mcp.server / routers.admin`。

**查过但决定不动的三类**(别再查一遍):

- **`F811`(3 条)** 函数内 import 遮蔽模块级 import —— 同一个类,无语义差异,
  **风格问题不是 bug**
- **`B904`(20 条)** `except` 里 raise 不带 `from` —— ⚠️ **这不是丢根因**。
  Python 本来就隐式链接(原异常在 `__context__` 和 traceback 里),
  `raise ... from None` 反而是**抑制**它。
  **与「异常处理器自己不许再抛」不是一回事** —— 后者是 `except` 块内的代码
  自己 crash,这个只是链没写显式
- **`F405` 那 1131 条** —— 系统性的,动它等于改 CLAUDE.md 定的架构约定

📌 **ruff 这条线没有 mypy 那种「混着真 bug」的情况** —— 先按规则分组看构成,
别被总数吓到。

### 前端产物体积(2026-10-12 实测,别重复排查)

`dist/assets/` 按大小排:

| chunk | raw | gzip | 说明 |
|---|---|---|---|
| `index-*.js` | **850 kB** | **243 kB** | 应用壳 + 未被manualChunks 命中的部分 |
| `recharts-*.js` | 400 kB | 118 kB | 已拆出,**懒加载** |
| `react-vendor-*.js` | 180 kB | 59 kB | 长期可缓存 |
| `framer-motion-*.js` | 128 kB | 42 kB | 年度报告/动画 |
| `poster-*.js` | 60 kB | 20 kB | html-to-image + qrcode |
| `cmdk-*.js` | 44 kB | 14 kB | 命令面板 |

### ⚠️ 首屏要看 `index.html` 的 rel,不是看 assets 目录(**已修一处**)

**首屏真实下载** = 入口 `<script>` + 所有 `rel="modulepreload"`。
只看 `dist/assets/` 的文件列表**看不出这件事**:文件都在,
但哪些是首屏的**只有 HTML 说了算**。

验证方法(不装任何东西):build 完 `grep 'modulepreload' dist/index.html`,
或起真服务器把引用的 `/assets/*` 逐个取回来。

#### 已修(2 处):先改 import 图,不是改 chunk 名字

首屏从 **1672 kB 降到 1479 kB**(预取 5 条 -> 3 条)。两处的教训一样:

| 库 | 大小 | 为什么进首屏 | 怎么修 |
|---|---|---|---|
| recharts | 409 kB | `manualChunks` 把它提升进入口静态图 | **去掉那条 chunk 规则** |
| framer-motion | 126 kB | `Amount.tsx` 只为 `useReducedMotion` 一个钩子静态 import 了它 | **换原生 `matchMedia`**(见 `lib/useReducedMotion.ts`) |

⚠️ **改 `manualChunks` 只对第一处有效。** 第二处我先试了去掉
framer-motion 的 chunk 规则 —— 首屏**一点没变**。

因为问题不在 chunk 划分,而在**谁静态 import 了它**:
chunk 叫什么名字不影响它在不在入口的 import 图里。

**流程**:发现某个库在首屏 -> 先查**谁静态 import 了它**
(`grep -rln "from '<lib>" src/` 再往上追到 shell) -> 改那处 import ->
重新 build,看 `modulepreload` 条数与字节数是否真的降了。

第二处尤其典型:`useReducedMotion` 的实现**本身就是一行
`matchMedia('(prefers-reduced-motion: reduce)')`** ——
而 `prefers-reduced-motion` 本来就是给 CSS 媒体查询用的。
为了这一行,一个**全站每个页面都要用的组件**(`Amount`)拖进了 126 kB 动画引擎。

#### 还剩两个 eager 的(已查清原因,未动)

| 库 | 大小 | 为什么在首屏 |
|---|---|---|
| `react-vendor` | 176 kB | **合理** —— React 本身,长期可缓存 |
| `cmdk` | 44 kB | `AppHeader.tsx` **静态** import 了 `CommandPalette.tsx`(命令面板本该按需打开) |
| `poster` | 36 kB | `html-to-image` + `qrcode`,只被 `PosterDialog` / `TwoFactorAuthSection` 引用,而这两处**都在 lazy 路由下** —— 按理不该 eager ⚠️ 见下 |

#### ⚠️「桶引用导致 eager」这条假设 —— **已实验推翻,别再试**

上一轮我怀疑 `@beecount/web-features` 的 `export *` 巨型桶(43 条导出、
53 个文件整包引用)是 `poster` 进首屏的原因。**这一轮验了,不成立**:

| | preload | 首屏 |
|---|---|---|
| 基线(桶引用) | 3 条 | 1479 kB |
| 把 `CommandPalette` 换成**子路径**引用 | 3 条 | **1479 kB** |

**完全没变。** 也就是说 rollup 对 `export *` 链的 tree-shaking 是有效的 ——
桶不是原因。

顺带查清的两件事:

- `packages/web-features/package.json` **没有 `exports` 字段**,只有
  `main`/`types` 指向 `src/index.ts` —— 所以**所有消费者只能用根桶**。
  想用子路径还得先给 `tsconfig.json` 加 `"@beecount/web-features/*"`
  通配(实测能过 `tsc` + `vite build`,但**对首屏毫无影响**)
- `cmdk` 的原因**是验过的**:`AppHeader.tsx` 静态 import 了
  `CommandPalette.tsx`,而后者用 cmdk。命令面板本该按需打开,
  这是唯一一条已定位且可改的

**`cmdk` 和 `poster` 是同一类,import 图上已经没有可改的了**

i18n 那轮改完重新查,发现 `CommandPalette` 在 `AppHeader` 里**本来就是
`React.lazy` 的**(`AppHeader.tsx:26`),而且:

- 全仓 `grep -rnE "^\s*import .*CommandPalette"` -> **零结果**(那几处命中全是**注释**)
- 全仓 `from 'cmdk'` 只有 `CommandPalette.tsx` 一处
- i18n 改完后,入口块里**已经不再静态 import cmdk**

**结论:这两个不是「谁静态引了它」的问题,而是 Vite 会给「入口的动态 import
之间的共享 chunk」发 `modulepreload`。** `cmdk`(只被命令面板用)和
`poster`(html-to-image + qrcode,被年报页和设置页共用)都是这种情况。

⚠️ 所以别再花时间查「谁静态引了 cmdk/poster」—— 查不到,因为没人静态引。
唯一的杠杆是关掉 `build.modulePreload`,而那**要拿掉所有预取**(见下),不做。

⚠️ 查的时候注意**注释里出现的名字会让 grep 误报**:
`GlobalSharedLedgerDialogs.tsx:13` 和两个 `lib/*Events.ts` 的文件头
都写着 `CommandPalette`,但那是**说明文字**,不是 import 语句。
必须用 `grep -rnE "^\s*import ..."` 只认 import 行。

**`poster`(36 kB)为什么还在首屏 —— 范围已收窄**

从 `dist/index.html` 那一行入手,实测到的事实:

```bash
grep -oE 'from"\./[a-zA-Z-]+\.js"' <入口块>.js | sort -u
# 只输出:cmdk-BeCp-skv.js
```

**入口块只静态 import 了 `cmdk`,并没有 import `poster`** ——
但 `poster` 依然拿到了 `modulepreload`。

所以它不在「入口静态依赖」这一类里。剩下的合理解释:
**Vite 会给「入口的**动态** import 之间的共享 chunk」发 `modulepreload`**。
`html-to-image` + `qrcode` 被年度报告页和设置页共用,而那两页都是
`React.lazy` —— 共享动态 chunk 被 Vite 一并预取了。

⚠️ **别急着关掉 `build.modulePreload`**。那会把预取**全部**去掉,
对真正首屏需要的 chunk(`cmdk` 之类)也是损失 —— 收益 36 kB,
代价是所有预取都没了。这属于**用我验不了的性能权衡**去换确定的字节数,
不做。真要优化得先有 RUM 或浏览器的 network 面板数据。

#### ✅ 浏览器复验已具备(`playwright-cli`),改打包配置后**必须**跑一次

本会话我改了两次打包配置(去掉 recharts 的 `manualChunks` 规则、
换掉 `Amount.tsx` 的 framer-motion import),**之前一直没在浏览器里验证过
应用还能不能启动**。第 27 轮那个部署复验只看了 HTTP 200 ——
**坏掉的 bundle 同样返回 200**。

现在用 `playwright-cli` 补上了:

```bash
# 起真服务器(WEB_STATIC_DIR 指向 dist),然后:
playwright-cli open http://127.0.0.1:8921/
playwright-cli console error          # -> Total messages: 0
playwright-cli --raw eval "JSON.stringify(performance.getEntriesByType('resource')
  .filter(r=>r.name.endsWith('.js'))
  .map(r=>({f:r.name.split('/').pop(),kb:Math.round(r.encodedBodySize/1024)})))"
```

实测结果:应用**正常启动并跳到 `/login`**,标题 `蜜蜂记账 · Web`,
**控制台 0 错误**,浏览器实际下载 4 个 JS:

| 文件 | kB |
|---|---|
| `index` | 1223 |
| `react-vendor` | 176 |
| `cmdk` | 44 |
| `poster` | 36 |
| **合计** | **1479 kB** |

⚠️ **这个 1479 和上面三轮用 `modulepreload` 推算的完全一致** ——
所以那套「看 `index.html` 的 rel,不看 assets 目录」的度量方法是**对的**,
可以放心用。反过来,**光看 HTTP 200 证明不了 bundle 能跑**,别再拿它当验收。

#### ✅ i18n 按需加载 —— **已做**(首屏再省 164 kB,浏览器实测)

| | 首屏 JS |
|---|---|
| 本会话开始 | 1672 kB |
| 去掉 recharts 的 chunk 规则 | 1479 kB |
| 去掉 `Amount.tsx` 的 framer-motion | 1479 kB |
| **i18n 按需加载** | **1315 kB** |

**累计省了 357 kB(21%)。**

三处改动:`i18n/index.ts`(en 静态 + 其余动态 import + `prepareInitialDictionary`)、
`LocaleProvider.tsx`(可选 `loadDictionary` + 内部可增长缓存)、
`main.tsx`(首屏渲染**前** await 目标语言)。

### 两条别踩的

**① `en` 必须静态打包。** `t()` 是
`active[key] || fallback[key] || key`,fallback 就是 `dictionaries.en`。
少了它,任何词典没覆盖到的 key 会**直接显示原始 key 名**。

**② 「词典是否已加载」不能写 `if (!dictionaries[locale])`。**
未加载的语言在 `dictionaries` 里是 `{}`,而 **`{}` 是真值** ——
于是 `setLocale` 跳过加载,用户永远停在一屏英文上,
而且**没有任何报错、没有任何信号**。判据要用 `Object.keys().length > 0`。

⚠️ `setLocale` 必须是**先拿到词典再切语言**。反过来会闪一屏英文 ——
这正是我推迟三轮时担心的那个失败模式。

### 实测(`playwright-cli`)

- zh-CN 用户首屏直接中文(无英文闪烁、无原始 key)
- 切换 zh-CN -> zh-TW -> en -> zh-CN 全通
- **`zh-CN-*.js` / `zh-TW-*.js` 只在切到该语言时才下载**
- 控制台 0 错误 / 307 测试通过 / `tsc` 0 错
- 页面上「形如 `shell.xxx` 的原始 key」实测为 `[]`

⚠️ **验证时我两次误判「切换坏了」**,其实是我自己的探针坏了:
`playwright-cli` 用逗号分隔的 CSS 选择器会**静默失败**,
以及没等 React 重渲染就取 `innerText`。**先分清是产品坏了还是探针坏了。**

⚠️ 卸载 `html-to-image`/qrcode 那类共享 chunk 的取舍**仍然不要做**(见上节)。

#### ✅ 登录后的应用主体也验过了(i18n 改动的真正战场)

i18n 改动碰的是**渲染 bootstrap**,只验落地页/登录页不够。用浏览器走完:

| 检查 | 结果 |
|---|---|
| 登录 | 成功,`/app/overview` |
| 应用壳 | `蜜蜂记账 v1.0.0 新建账本 首页 交易 资产 分类 标签 搜索 …` |
| **应用内漏出原始 key** | **`[]`(零)** |
| 控制台 | 仅 1 条:`403 /api/v1/admin/users` —— 非管理员探测 admin 端点,**预期** |

**应用内的语言菜单也验通了**(上一轮的缺口):

| 操作 | 结果 |
|---|---|
| 打开用户菜单 -> 切 English | `BeeCount v1.0.0 Create ledger Home Transactions …` |
| 再切回 简体中文 | `蜜蜂记账 v1.0.0 新建账本 首页 交易 …` |
| 应用内原始 key | **`[]`(零)** |
| **按需下载** | **只下了 `zh-CN-*.js`,`zh-TW` 没下**(这次没切过去) |

**「按需加载在登录后的应用里同样成立」** —— 这是最关键的一条:
之前只在落地页验过。

⚠️ **上轮点不动是因为菜单没打开**。那三个按钮在收起的 popover 里
(`visibility: hidden`)。正确顺序是**先点开菜单,再点选项**:

```bash
playwright-cli snapshot --depth=6     # 拿可见元素的 ref
playwright-cli click f1e37            # 用户菜单(那个 "B" 头像按钮)
# 此时 button[title='English'] 的 visibility 从 hidden 变 visible,才能点
```

### ⚠️ 浏览器探针不可靠 —— 我连着三次误判「产品坏了」

同一个 `playwright-cli` 会话里,我三次把**自己的探针故障**当成产品缺陷:

| # | 探针怎么坏的 | 症状 |
|---|---|---|
| 1 | 逗号分隔的 CSS 选择器**静默失败** | 点 3 次没反应 |
| 2 | 没等 React 重渲染就取 `innerText` | 读到旧文案 |
| 3 | 点**收起菜单里**的按钮(`visibility:hidden`) | 「语言切换坏了」 |

三次都是**产品好的**。

**规则:下结论说「某个功能坏了」之前,先证明按钮/选择器本身是有效的** ——
检查元素**可见性**、确认选择器**确实命中**、等 React 提交完成再取值。
这与「grep 搜不到 ≠ 不存在」是同一条纪律:**探针的否定结果不能当证据。**

**可用的诊断动作**:
`getComputedStyle(e).visibility` / `e.getBoundingClientRect().width` /
`document.querySelectorAll(sel).length` —— 先看探针命中了几个、有没有尺寸。

⚠️ 应用内语言菜单是 **popover**(标题就是语言名本身,如 `title="English"`),
**不是**落地页那种循环切换按钮(`title="切换语言: 简体"`)。
按「切换语言」找会找不到 —— 这是两种不同的组件。

#### i18n 按需加载的**护栏**(突变验证过,3/3 抓到)

护栏:`frontend/apps/web/src/i18nLazy.test.ts`。⚠️ 原来的 `i18n.test.ts`
**静态导入三个词典**,按需加载那条路一条都没测 —— 等于上线后零回归保护。

三条断言各自锁住一种**上线后才暴露、且不报错**的失效:

| 变异 | 后果 |
|---|---|
| `loadDictionary('zh-CN')` 悄悄返回 `en` | 界面全英文,**无任何报错** |
| zh-CN 退回静态 import(不再是空壳) | 懒加载失效,首屏又变大 |
| `initialDictionaries.en` 变空 | `t()` 兜底失效 -> **漏 `shell.language` 这种原始 key 名** |

**都是真跑**(vitest 直接 import 模块 + 动态 `import()`),
不是字符串存在性检查 —— 这正是上面那条「前端 45% 的护栏是假的」的反面例子。

⚠️ 顺便断言了 `Boolean({}) === true` —— 因为这**就是**
`LocaleProvider` 里必须用 `Object.keys().length` 而不能写
`if (!dictionaries[locale])` 的原因。空壳「看起来像没值」但**它是真的**。

#### `useReducedMotion` 的**护栏**(突变验证过,2/2 抓到)

护栏:`frontend/apps/web/src/useReducedMotion.behavior.test.ts`。
这个钩子是为把 126 kB framer-motion 换出首屏才写的,**之前一直没有测试**。

它要 React 渲染环境而仓里**没有 jsdom** —— 按「纯逻辑提到模块级就能真跑」
那条做法,把两个函数提取成导出(`prefersReducedMotion` /
`subscribeReducedMotion`),在 `apps/web/src/` **真跑**它们。

⚠️ 顺带发现它**之前没从 `@beecount/web-features` 桶导出**,只能组件内部用。

锁住的是**老 WebKit 的 `addListener` 兜底分支** —— 删掉它,旧 Safari 上
用户改「减弱动画」会静默失效,**没有任何报错**。

**⚠️ 这条补护栏的过程中我把自己的探针搞坏了两次,两次都是同一类错:**

1. **只 stub `matchMedia` 不够** —— vitest 没开 jsdom,**`window` 本身不存在**,
   被测代码第一行 `typeof window === 'undefined'` 就早退了。
   症状是「明明 stub 了却拿到 false」,看着像产品坏了
2. **同时改源码和测试**做突变验证 —— 测试当然绿。
   **那不是「护栏没抓到」,是变异根本没生效。** 只改源码重做才正确变红

**这与上面那条「先确认回退真的生效再判护栏」是同一条纪律** ——
探针的否定结果不能当证据,变异也一样。

#### (存档)已修:`manualChunks` 给 recharts 的规则反而害了它

改前首屏预取 **781 kB**,改后 **382 kB**(这是第一处修复)(产物总量 2045 -> 2048 kB,
没有重复打包)。**少下载 399 kB**。

机制:`manualChunks` 把 recharts 提升进**入口的静态 import 图**,而 Vite
只给「入口静态依赖」的 chunk 发 `modulepreload` ——

```html
<link rel="modulepreload" href="/assets/recharts-CEpsFnzg.js">   <!-- 409 kB -->
```

路由是 lazy 的**完全没用**。改前入口块里能直接看到
`from"./recharts-CEpsFnzg.js"`。

我把四条 `manualChunks` 规则**逐条**去掉各测一次 —— 只有 recharts 有效果
(781 -> 382 kB),再去掉 cmdk / poster / framer-motion **首屏不变**,
所以那三条保留。

⚠️ 这也说明:**我盯了三轮的「主块 850 kB」不是真正的大头** ——
它只占首屏的一半,真正被白拉进去的是 recharts。
盯一个指标之前先确认它是不是**最大的那个**。

### (存档)首屏曾是 ~1.6 MB,不是主块的 850 kB

这一条纠正了**整整三轮**我一直在优化的错误目标。

`dist/index.html` 里除了入口脚本,还有 **5 个 `rel="modulepreload"`**:

| 首屏预取 | 大小 | 本该懒加载吗 |
|---|---|---|
| `index-*.js` | 891 kB | 入口 |
| `react-vendor-*.js` | 181 kB | 是(长期可缓存) |
| **`recharts-*.js`** | **409 kB** | **是** —— 只有 OverviewPage 等少量页用 |
| **`cmdk-*.js`** | **45 kB** | **是** —— 命令面板 |
| **`framer-motion-*.js`** | **129 kB** | **是** —— 年度报告 / 动画 |
| **`poster-*.js`** | **37 kB** | **是** —— html-to-image + qrcode |
| CSS | 87 kB | |

**改前首屏 JS 合计 ~1615 kB 未 gzip(约 468 kB gzip)+ CSS。**
(修掉 recharts 那条 modulepreload 后降到约 1216 kB)

`manualChunks` 把它们拆成独立 chunk 了,路由也是 lazy 的 —— 但只要 chunk
还在**入口的静态 import 图**里,Vite 就给它发 `modulepreload`,
浏览器**首屏就下载**,懒加载完全失效。

**所以「优化主块」这个目标本身选错了** —— 主块只占首屏的 55%。
真正的大头是 `recharts` 那 409 kB,以及三个本该按需的库。

**怎么验证的**(不用装任何东西):

1. 起真服务器(`uvicorn`,`WEB_STATIC_DIR` 指向 `dist`)
2. `curl` 首页,把 `<link>`/`<script>` 引用的 `/assets/*` 逐个取回来
3. **看 `rel` 属性** —— `modulepreload` = 首屏下载,`prefetch` = 空闲时下载

⚠️ 只看 `dist/assets/` 的文件列表**看不出这件事**:文件都在,
但哪些是首屏的只有 HTML 说了算。

### ⚠️ 这里有一条**我写错过、后来更正**的结论(别再踩)

我第一轮量 i18n 时跑的是:

```bash
du -sh packages/ui/src/locale      # -> 1.3 kB
```

于是写进文档:「i18n 不是问题」。**那量错了地方** —— 那个目录只有
`LanguageToggle` / `LocaleProvider` 三个小文件。真正的词典在:

| 文件 | 源大小 | 进包后 |
|---|---|---|
| `apps/web/src/i18n/en.ts` | 92.5 kB | **90.2 kB** |
| `apps/web/src/i18n/zh-CN.ts` | 93.0 kB | **70.9 kB** |
| `apps/web/src/i18n/zh-TW.ts` | 91.9 kB | **70.0 kB** |
| | | **合计 231 kB** |

而 `apps/web/src/i18n/index.ts` 顶部是**静态导入**:

```ts
import en from './en'
import zhCN from './zh-CN'
import zhTW from './zh-TW'
```

**所有用户都下载全部三种语言,哪怕只读一种。** 这占主块的 **28%**。

**教训**:「排除一个嫌疑」要确认量的是**同一个东西**。我量的路径和
结论指向的路径不是同一个,却写得很笃定 —— 与「grep 搜不到就断定不存在」
是同一类错误。

### 已排除的嫌疑(这些量对了)

- **`lucide-react` 不是问题**:临时给它加一条 `manualChunks` 重新 build,
  主块**大小不变**(849.99 kB)、也没生成对应分块 —— 它已被 tree-shake
  到几乎为零。53 处桶导入**不用改**。

### 主块的完整构成(rollup generateBundle 实测)

不装新依赖:在 `build.rollupOptions.plugins` 里临时挂一个
`generateBundle` 钩子,把 `bundle[...].modules` 写出去即可
(配置是 ESM,用 `import { writeFileSync } from 'node:fs'`,
**`require` 不可用**)。

| 项 | 大小 |
|---|---|
| 依赖(全在 `.pnpm` 下,已拆成独立 chunk 的除外) | ~607 kB |
| **i18n 三种语言** | **231 kB** |
| `AccountsPanel.tsx` | 48.7 kB |
| `CategoriesPanel.tsx` | 32.4 kB |
| `TransactionsPanel.tsx` | 31.3 kB |

### 要不要做 i18n 按需加载(**有意暂缓**)

改成 `() => import('./zh-CN')` 能把首屏从 231 kB 降到约 90 kB,
对「只用 Web/PWA + 多为移动网络」这个部署形态是实打实的收益。

**但我没做**,原因是风险收益比不合适:它会改 `LocaleProvider` 的加载
时机,而**每一个渲染出来的字符串**都依赖它 —— 词典没到位就是满屏 key 或
闪一下原文。`zh-CN` 用户首屏会多一次往返。

要动的话按这个顺序:先给 `LocaleProvider` 加「词典未加载」的显式状态
(别靠 `?? key` 兜底 —— 那会把加载失败变成满屏原始 key 而没人发现),
再加按需 `import`,最后**实测首屏 chunk 变小**再提交。

⚠️ `dist/` 是 **gitignore** 的,不在仓库里 —— 部署时现 `pnpm build`。
本地产物过期无所谓,但这也意味着 **CI 的 build 结果才是发布依据**。

### mypy 存量错误里**混着真 bug**(2026-10-12 两轮实测)

我把 80 条存量错误当「存量门」放着,错了 —— 按类型筛一遍发现:

| 轮次 | 处理 | mypy |
|---|---|---|
| 1 | `backup/runner.py` 的未判空(14 条 `union-attr`) | 80 -> **70** |
| 2 | `mcp/server.py` 的 `**kw` 噪音(14 条 `arg-type`) | 70 -> **63** |

第 1 轮是**真 bug**:`except` 块自身没保护,失败的备份记不下来(见上节)。
第 2 轮是纯类型问题,但改的过程中**又发现一处配套缺失**:

`_logged_call(kwargs: dict[str, Any])` 会把**每个**传 TypedDict 的 tool
判成不兼容 —— TypedDict 在类型层面不是 dict 的子类。改成
`Mapping[str, Any]` 才是对的。**只加 TypedDict 不改它,14 条变 20 条,
而且下一个人会把 TypedDict 全删掉** —— 一个「修错工具反而逼人用回坏模式」的坑。

⚠️ `_UpdateTransactionKw` 第一遍**漏写了 `tx_type`**,mypy 当场报出来。
这就是这个模式的价值:**字段写漏/写错名/类型不匹配,都变成编译期错误。**

**⚠️ 走不通的一条(试过了,别再试):给 `_MergeSpec.model` 换精确类型**

`sync_applier.py` 的 4 条 `"type" has no attribute "ledger_id"` 看着像该修
(`model: type` 太宽,mypy 不知道上面有什么属性)。我试了三版:

| 做法 | mypy |
|---|---|
| 原地(`model: type`) | 63 |
| 一个 Protocol(只要 `ledger_id` + `sync_id`) | **65** |
| 两个 Protocol + union + 两处 `cast` | **81** |

**三条都变差**,已全部回退。原因:

1. 单个 Protocol 会把**四张 user-global 表**判成不兼容 ——
   `UserAccountProjection` / `UserTagProjection` / `UserCategoryProjection` /
   `UserExchangeRateProjection` **没有 `ledger_id`**,它们走 `_merge_user_scoped`。
   我第一版根本没意识到**两条路径的模型形状不同**,这是我的错,不是代码的错
2. 加了 union 之后,整张 `_LEDGER_MERGE_SPECS` 登记表(几十个 `_MergeSpec(...)`
   字面量)和它的全部下游都被拖进来 —— 7 条变 25 条

**结论:这 4 条 attr-defined 的净收益是负的,保持原样。**
`model: type` 虽然宽,但它诚实地描述了「运行时就是一个类」——
比半个协议 + 两个 cast 更贴近真相。

⚠️ **教训:改类型标注前先量「会波及多少下游」。** 我凭直觉以为
「收紧一个标注只会少几条」,结果整张登记表一起被拖进来。
**先量再改**,和查首屏大小一个道理。

**✅ 筛错误类型时看最稀有的那一类,不是最多的**

给 `_MergeSpec.model` 换精确类型是净负的(63 -> 81),说明 `[arg-type]` /
`[attr-defined]` 这些**大类大多是系统性噪音**,硬改只会拖更多下游进来。

反过来筛 `[valid-type]` / `[misc]`(一共 6 条),两处都是真问题:

| 位置 | 问题 |
|---|---|
| `services/data_cleanup/cleaner.py` | `list[callable]` —— 把内置**函数**当类型用,后面 `# type: ignore` 压着,`op()` 报「callable? not callable」 |
| `routers/admin.py` `data_cleanup_clean` | 塞 `dict` + `# type: ignore`,靠 Pydantic 强转 —— **给 `DataCleanupFailure` 加个必填字段就变运行时 422,而 mypy 因为有 ignore 不报** |

第二条与本仓反复出现的「同一件事两份实现 / 契约没在契约点强制」是同一族:
**`# type: ignore` 就是在契约点放弃强制**。

**⚠️ 剩下的稀有类型我逐条验过 —— 3 条全是假阳性,别加 cast 去消**

| 位置 | mypy 说 | 运行时为什么安全 |
|---|---|---|
| `routers/profile.py:344` | `Path / None` | 上一行 `not (profile.avatar_file_id or "").strip()` 已经排除 None(None -> `""` -> falsy -> raise 404)。mypy 窄化不出 `or ""` 蕴含「非 None」 |
| `routers/write/_shared.py:775` | `object` 不可索引 | `minimal_snap["items"][0]` 看着像 IndexError,但 `snapshot_mutator._find_by_sync_id` 找不到时**必然抛 `KeyError`**(调用方捕获成 404),返回时 `items` 恒为 1 个元素 |
| `routers/import_data/endpoints.py:862` | 两个 list 类型不匹配 | 见下 |

**⚠️ 给这三处加 `# type: ignore` 或 cast 是错的** —— 那会把
「mypy 窄化不出来」变成「类型检查彻底放弃这一行」。假阳性要**记下来**，
不是要**消掉**。

📌 **这条 mypy 线到此为止(60 条,会话开始 80,上游 89)**:
稀有类型已验完(2 真 3 假),大类里唯一有价值的 `union-attr` 也处理过了,
剩下的 `arg-type` / `attr-defined` 是系统性噪音,硬改会拖更多下游
(见上面 `_MergeSpec.model` 那条 63->81 的实测)。

**规则:错误数量多的那一类基本是系统性噪音;数量少的(尤其
`valid-type` / `misc`)才值得逐条读。** 先按类型分组,
再按「条数升序」决定从哪看起。

**规则:mypy 的 `| None` 与 `**kw` arg-type 要当真读** ——
前者常常是真能走到的路径,后者是「同一形状写了很多遍」的指示器。

mypy 那边本 fork 用 `TypedDict` 声明各工具的 kwargs（`kw = dict(...)` 会让
`**kw` 展开每一行都报 arg-type，上游 `parse_and_create_from_text` 就是这么
留着存量错误的）。新写工具别再沿用 `dict(...)`。

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

### 五个静默丢失的坑(都踩过,都有测试)

| 坑 | 后果 |
|---|---|
| `upsert_tx` 无条件 `replace_tx_splits(None)` | `/sync/push` 部分更新把腿全删 → 改成 `if "splits" in payload` |
| mutator 用 `pop("splits")` 表示清除 | 键消失 = projection 的「不动」→ `splits: []` **清不掉**。改写空 list 当信号 |
| `delete_account` 守卫只数父交易三列 | 有腿时那三列是空的 → 守卫失效 → 账户可删 → 子表悬空 → 余额永久漂移 |
| `_truncate_ledger` 漏子表 | rebuild / 还原后父交易没腿、旧腿还在 → 余额「凭空多出来」 |
| CSV 不导出 Splits 列 | 导出→导入后组合支付变「无账户的支出」,账户余额全错 |

**通用护栏(加新列时立刻红)**:`tests/test_projection_reverse_bridge_coverage.py`
枚举 `ReadTxProjection.__table__.columns` 全部列,减去白名单后要求**每一列**
都被反向桥引用 —— 加新列忘了改反向桥,这条立刻点名漏了哪一列。

用**白名单**而不是黑名单是关键:黑名单在加新列时**默认通过**(而那正是要防的
情况),白名单默认**拒绝**,新列必须显式论证为什么可以不接。
白名单带数量上限,防止有人为了「让测试绿」把真列塞进去。

(此前每一次都是**事后**补一条专用测试 —— 那是症状处理不是解决方案。
第 6 列被加进来时同样的事会再发生一次,仍然要等用户发现数据被抹了才有人查。)

专用护栏:`tests/test_tx_split_invariants.py`(20 条)、
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

### 时区:每一层都得输出 UTC(已栽五次,改之前先跑一遍排查)

本仓现在有 **5 个**解析 ISO 时间的点,全部必须输出 **UTC**:

| 位置 | 契约 |
|---|---|
| `snapshot_mutator._to_iso8601` | 归一 |
| `projection._parse_happened_at` | 归一 |
| `read/_shared._to_utc` | naive 当 UTC(读路径兜底) |
| `mcp/read_tools._parse_dt` | 归一(值**直接进 SQL**) |
| `import_data/transformer._localize_naive` | 归一(CSV) |

**为什么每一层都要归一(而不是最后一层做一次就够)**:
SQLite 在 `upsert_tx` 落库时存的是**墙钟字符串**。带 +09:00 的 datetime
落库 → 库里存 `18:00`,而库里其它交易存的是 UTC 墙钟 → **同一列两套口径**,
统计/排序/筛选全乱。归一必须发生在**进 ORM 之前**。

**CSV 自带时区标记时以它为准**,不要再用 `tz_offset_minutes` 二次偏移 ——
`2026-05-13T18:00:00+09:00` 已经锚定了绝对时刻,再减一次会挪 9 小时。
`tz_offset_minutes` 只适用于**裸墙钟**。这条还有一个暗坑:`"%Y-%m-%dT%H:%M:%SZ"`
这个 strptime 候选格式会把 `Z` 当**字面量**吃掉(产出 naive),于是
CSV 写了 `Z` 反而丢掉 UTC 标记 —— 该格式已改用 `%z`。

**改任何一处之前先跑**:

```bash
grep -rn "fromisoformat\|strptime" src/ --include=*.py
grep -rn "replace(tzinfo=timezone.utc)" src/ --include=*.py
```

⚠️ **纯 Python 里两个 aware datetime 比较会自动归一**,所以
`datetime(...,9,tzinfo=utc) >= datetime(...,18,tzinfo=+9)` 在 REPL 里是
`True`,**看不出问题**。只有落到 SQL 驱动才炸。护栏必须有**一条穿过真
SQLite** 的断言,否则这类 bug 能在测试全绿下活下来。

### 时区:`_parse_happened_at` 必须归一到 UTC(第四次同类)

SQLite 没有原生 datetime 类型,SQLAlchemy 存的是**墙钟字符串**。写一个
aware `18:00+09:00`(= UTC 09:00)进去,库里落 `18:00` —— **那 9 个小时
消失了**;读回来是 naive,读路径的 `_to_utc` 一律「假设是 UTC」→
序列化成 `18:00Z` → 前端按本地时区渲染成**次日 03:00**。

`snapshot_mutator._to_iso8601` 早就归一,而 `projection._parse_happened_at`
(写 ORM 的那个)**没有** —— 又一次「同一个转换两份实现,一份对一份错」。
这条影响**所有非 UTC 时区用户的每一笔交易**,新 UI 团队实测发现。

护栏:`tests/test_happened_at_timezone.py`,含一条**真 DB round-trip**
(单元测试只能证明函数返回值对,丢掉时区那一步发生在 SQLite 驱动里)。

### 源码级断言自己也会恒真(tokenize 的坑)

剥注释比 `src` 里 `date.today()` 时,**别**这么写:

```python
" ".join(t.string for t in tokens if t.type not in (COMMENT, STRING))
```

`tokenize` 把 `date.today()` 拆成 `date . today ( )` —— join 之后
**永远匹配不上**原样的 `date.today()`。断言看着在跑,其实恒真。

正确写法是 `tokenize.untokenize(kept)`(保留原有空白与括号间距)。

这与上面那条「hook 插进函数体」是**同一类**:语法完全合法、看起来在
起保护作用、实际永远通过。做源码级断言时,**必须对它自己做一次变异**
(把违规代码写回去,确认它变红)—— 我在修 bug 的过程中刚又犯了一次。

### 护栏本身也要做变异审计(2026-10-12 实测:前端 45% 是假的)

写完一批护栏后,**对每条做一次定向变异**,看有没有任何测试变红。
本会话实测:11 个变异里 **5 个没被检出**。

失败模式完全一致 —— 前端护栏几乎全是「字符串存在性」检查:

```ts
expect(src).toContain('function statementOut(')          // 代码被写下来了
expect(src).toMatch(/import\.preview\.col\.splits/)    // 字符串在文件里
```

这证明不了「代码被执行到了」:`hasSplits = false` 让整列永远不出现,
护栏全绿 —— **比没有护栏更糟**,因为它让人以为这里有保护。

这与「hook 插进函数体,语法合法但永不可达」是**同一类**,
只是这次发生在**测试断言**里。

### ⚠️「前端只能做源码正则」是假设,不是限制

没有 jsdom / `@testing-library/react` 时很容易退化成读源码文本。
但 **vitest 能直接 import 模块**(`apps/web/src/lib/aiConfigMerge.test.ts`
早就在这么做了),而大部分派生逻辑是**纯函数,无 React / 无 DOM**。

把这类逻辑从组件内提到模块级 export,就能**真跑**:

| 抽出 | 测试文件 |
|---|---|
| `statementOut` | `statementOut.behavior.test.ts` |
| `previewColumns` | `previewColumns.behavior.test.ts` |
| `legOptions` → `features/splitLegOptions.ts` | `splitLegOptions.behavior.test.ts` |

**字符串断言不要全删** —— 它们能抓真跑抓不到的东西:
渲染出口(`{statement ? (`)、调用点(`legOptions(accounts, leg)`)、
i18n 键存在性(纯函数测试不渲染组件)。分层:**纯逻辑真跑,接口与出口查源码**。

### 反向桥的通用护栏必须**运行期**(静态判据有个致命盲区)

两个通用护栏都在跑真数据、不断言输出:

| 侧 | 护栏 | 判据 |
|---|---|---|
| 交易 | `tests/test_projection_reverse_bridge_coverage.py` | 真跑 `_projection_row_to_tx_dict`,查输出 dict |
| 账户 | `tests/test_account_reverse_bridge_coverage.py` | 真跑 `snapshot_builder.build`,查输出 dict |

⚠️ **别退回「列名是否出现在源码里」**。那个判据被

```python
if row.tax_amount is not None:      # 列名在这里
    item["taxAmount"] = row.tax_amount   # 删掉这一行,护栏照样绿
```

骗得死死的 —— 后端 10 个变异里唯一漏掉的就是这条,而它是**核心**那条。

静态判据还**原理上**抓不到两类:key 拼错(`taxamount` vs `taxAmount`)、
复制了但取错列(`item["note"] = row.category_name`)。只有真跑才知道。

运行期判据还立刻报出了我映射表漏登记的 `tag_sync_ids_json` /
`attachments_json` —— 它们在 bridge 里是 **JSON 解析后**的产物,解析失败
会被 `except: pass` 静默跳过,静态判据下「看起来已被引用」。

**教训**:我曾写下「交易侧能用静态判据,因为每个列只有唯一一处 `row.<列>`
引用」,并拿它解释账户侧为什么不行 —— 结果那句话本身是错的,那个 `if`
就是第二处。**拿一个没验证过的假设去解释另一个观察**,等于把两个错误
绑在一起。

### ⚠️ 「变异没被检出」先别判护栏失效 —— 先问是不是被另一道挡住了

把 `upsert_tx` 里的 `if "splits" in payload:` 改成 `if True:`
（复现 `CLAUDE.md` 记的那个坑),**没有任何测试变红**。

不是护栏失效,是**上层还有一道**:`_merge_tx_splits_after_merge` 总会把
`splits` 补进 merged（要么来自 payload,要么来自现有腿）,所以同步路径
走到 `upsert_tx` 时那个条件**恒真**。

那条守卫保护的是**不走 merge 的调用方**（REST 写路径）。变异审计要么换个
调用面,要么在注释里写明它防的是谁 —— 否则下一个人会以为它没作用而删掉。

### ⚠️ 「grep 搜不到」不等于「不存在」

`WEB_STATIC_DIR` 在 `src/` 里搜不到 —— 但它**确实生效**。字段名是
`web_static_dir`,环境变量名由 Pydantic Settings 按约定大写出来。

框架约定夹在中间时,grep 的否定结果**不能当证据**。下结论前先问:
「中间有没有一层会做名字映射的东西?」

### ⚠️ 断言越靠近进程全局状态,越容易被别人的测试影响

断言默认值时直接 `Settings()` —— 单跑绿、**全量 4 条红**。根因不在那
条测试:别的测试文件设过 `os.environ[...]`,进程环境已经不干净了。

改读 `Settings.model_fields["web_static_dir"].default` ——
**声明本身**与环境状态无关。这是本会话「单跑绿、全量红」的第四种形态,
规律一致。

### ⚠️ 连错三轮的根因:同一个 fixture 定义了两次

`tests/test_smoke_real_app.py` 里 `client` fixture 出现了**两次** ——
重构 engine 时插入了新的,旧的留在下面。Python 取**后定义者**,所以前面
三轮改的都是**从未生效的那份**,报错还指向别处。

症状依次是 `AUTH_EMAIL_EXISTS`、`no such table: users`、
`can only be called once` —— **全都不是这些错误看起来的样子**。

**规则:改动之前先确认「我改的代码是不是真的在跑」。** 连续三次修复失败时,
该停下质疑的往往不是环境,是自己的假设。

另两处真坑:

- **建表别走 alembic**:`env.py` 用
  `engine_from_config(config.get_section(...))`,configparser 把 `%` 当插值,
  Windows 临时路径经 SQLAlchemy 编码成 `C%3A/...` 直接 ValueError;
  绕开编码后迁移又落在**另一个库**上(查库确认 0 张表)。
  冒烟测试改用 `Base.metadata.create_all(_ENGINE)` —— 迁移链本身的正确性
  有专门的迁移测试,不靠这个 fixture
- **`Ledger` 按 `external_id` 查**:push 物化时主键是自生成 uuid,
  `ledger_id` 只是 external_id

### 「分层测试全绿 != 拼起来能工作」—— 定期真跑一遍

`tests/test_smoke_real_app.py` 起真 app(跑 alembic + 真路由 + 真鉴权 +
真 self-call 回环),走完:注册 -> 建账本 -> 建卡 -> 记账 -> 读应还 ->
绑自动还款 -> 手动触发。

**不可替代**:新增字段「加进 schema 了但没接到读端点」这类失败,分层测试
**结构上就查不到**。变异验证确认它能抓到(字段全不返回 / 字段在但值错,
两条都红)。

它与 `test_credit_autorepay_e2e.py` 分工:后者管自动还款那条链路,
本条管**装配**能不能起来(alembic、路由注册、依赖注入、鉴权、分类种子)。

### 新增「由既有调度器驱动的函数」—— 单测几乎必然漏(实测)

`_safe_run(fn, *, catch_up)` 用 `fn(catch_up=catch_up)` 调它驱动的**每个**
函数。第一版 `_detect_gaps(self)` 没这个参数 —— 缺口检测**一次都跑不起来**:

    TypeError: _detect_gaps() got an unexpected keyword argument 'catch_up'

而当时 **848 条单测全绿**:它们全都直接调 `self._detect_gaps()`。
**真服务冒烟测试一跑就撞上它** —— 隔一轮就证明了它自己的价值。

更糟的是 `_safe_run` 的 `except Exception: logger.exception(...); return`
**把 TypeError 吞掉并正常返回** —— 外面只看到「静默什么都没发生」。
所以断言必须落在**产出物**上,不能是「没抛异常」。

写护栏时我连犯三错,**每一个都让测试在真 bug 下照样全绿**:

1. monkeypatch 掉了被测函数 —— 换掉的是 wrapper,签名根本没被验证
2. fixture 忘了建 `User` 行 —— 缺口检测遍历 `User` 表,没有行就恒空
3. 连自己的包装函数都得接受 `catch_up`,否则撞上和被测代码一样的错

**规则:新增函数的调用点也该被测**,不只是它的逻辑。

### 改了 `projection.py` 就要想一遍 sync

`sync/push` 的写入路径是 `sync_applier` -> `upsert_tx` -> **`_parse_happened_at`**。
所以**任何对 `projection.py` 的改动都直接改变同步语义**,而同步是
`CLAUDE.md` 里 bug 密度最高的模块。

本会话第 7 轮修 UTC 归一时没意识到这一点,隔了 11 轮才补上同步侧的
真跑验证(`test_smoke_real_app.py` 里的
`test_sync_push_preserves_the_instant_of_offset_timestamps`)。结论是对的,
但**确认它需要单独一条测试** —— 单元测试测函数,测不到「经过 push 之后」。

### 两个 scope 事实(踩过才知道)

- `/sync/push` 要 **app scope** —— `client_type="web"` 注册的 token 直接 403
- app token 在 **pytest 里不能走 REST 写路径**:`tests/conftest.py` 有
  `ALLOW_APP_RW_SCOPES=false`,把 app scope 的写关掉了(生产加固同理)。
  判断的是 **scope 不是 client_type** —— 所以「注册成 app 就什么都能写」
  是错的

### 前后端字段名契约(跨语言护栏)

前端大量派生函数是「字段缺失就**静默**返回 null / 不渲染」:
`statementOut` 缺 `statement_bill` 就不显示那一格、`previewColumns`
缺 `splits` 就不出现那列。

而 **TypeScript 类型是手写的** —— 它不会验证运行时真的返回了这些字段。
所以「后端改名 → 前端读不到 → UI 静默少一块」这条路上,`tsc` 不报错、
`vitest` 全绿、用户只看到「这个功能怎么不见了」。

`tests/test_smoke_real_app.py` 里有一条跨语言护栏:真跑一次 API 拿响应 key,
再用正则从 `types.ts` 读出声明的字段名,取**「前端声明但后端不返回」**
的差集。只查这一个方向 —— 后端多返回的字段不算问题(前端可以先不声明)。

变异验证:后端改名 / 后端漏接线,**两条都变红**。

正则本身要有下限断言(`len(declared) > 15`)—— 正则失效时不能静默通过
(同「tokenize 那条」)。

### ⚠️ 全仓库只能有**一个** `TestClient(app)`

MCP 的 `StreamableHTTPSessionManager` 是**模块级单例**,`.run()` 只能调一次:

    RuntimeError: StreamableHTTPSessionManager .run() can only be called
    once per instance.

所以真服务冒烟**必须合并成一个文件**、共用一个 module 级 `client` fixture。
我第一版写成两个文件,单独跑都绿、全量跑 3 个 ERROR。

另外账户是 **user-global** 的:同一个用户建两个同名账户会被写路径拒
(`duplicated account name`)。合并后各测试要用**独立账户名**。

这两条加上上面那条「模块顶层改 env」,是同一族问题:
**「单跑绿、全量红」的四种来源**:

| # | 来源 | 症状 |
|---|---|---|
| 1 | 模块级改 `os.environ` | 污染同会话其他测试的判断 |
| 2 | 模块级单例(MCP session manager) | 第二个 `TestClient(app)` 直接 RuntimeError |
| 3 | 模块级全局 DB engine | 第二个测试文件设的 `DATABASE_URL` **根本没生效** |
| 4 | 全局 engine 还持有连接 | 删临时库 `PermissionError (WinError 32)` |

第 3 条最隐蔽:pytest **按文件名顺序收集**,字母序靠后的文件设
`DATABASE_URL` 时 `src.main` 早被别的测试 import 过了,engine 早已建好。

根治(第 3 条):**别依赖全局状态** —— 自建 engine,用
`app.dependency_overrides[get_db]` 注回去,import 顺序就无关了。

### CSV 往返要真跑(静默损坏)

`tests/test_smoke_real_app.py` 里有一条**导出 -> 重新 upload -> preview**
的真往返:组合支付的腿以 `[账户名, 金额]` 二元组回来、金额 3000/2000 都在,
税额的 `amount` 仍是含税总额。

`test_tx_split_invariants.py` 直接写 projection 行,证明不了
「真的导出了这一列」,更证明不了「**本仓自己的导入器解析得回来**」。

变异验证:导出不再写 Splits 列 / 导入不解析 splits,**两个方向都变红** ——
正是 `CLAUDE.md` 记的那个静默损坏。

### ⚠️ 测试文件之间也会互相看不见

第一版在模块顶层设 `AUTOREPAY_SCHEDULER=off`(想让后台任务别干扰断言),
**污染了整个 pytest 会话**:`test_credit_autorepay_scheduler.py` 6 条全红 ——
它正是按这个开关决定是否注册调度器。

表现是**单跑绿、全量红**,而且很难联想到是自己干的(`src.main` 是
import 即产生副作用的模块)。

```python
_PRE = {k: os.environ.get(k) for k in (...)}   # 改之前先存
# ... 设环境变量、import src ...
for k, v in _PRE.items():                       # import 后立刻还原
    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
```

凡是**模块顶层改全局状态**(env、cwd、sys.path、event loop、模块单例)的
测试文件,都必须做这个还原。

还有一条:`src.database` 的 engine 是**模块级全局**,连接一直持有,
pytest 结束时删临时库会 `PermissionError (WinError 32)`。DB 文件名带 pid,
残留交给系统清理 Temp。

### 加列 / 加表时:三张**手工登记表**,各有默认红的护栏

本会话前两条组合支付的坑都是**事后**补测试 —— 而下面这句自己就写着
「下一次同样的事还会发生」。所以现在三张表全部有结构性护栏:

| 登记表 | 漏登记的后果 | 护栏 |
|---|---|---|
| `_projection_row_to_tx_dict`(反向桥) | Web PATCH 静默抹掉新列 | `test_projection_reverse_bridge_coverage.py` |
| `_truncate_ledger`(清理清单) | rebuild / 还原后余额「凭空多出来」 | `test_truncate_ledger_coverage.py` |
| `_LEDGER_MERGE_SPECS`(同步 merge) | **`/sync/push` 静默丢弃新列** | `test_sync_merge_spec_coverage.py` |

三者都是**白名单式**:加新东西时**默认红**,要求显式论证为什么可以不登记。
黑名单在加东西时**默认通过** —— 而那正是要防的情况。
每条白名单都带**上限**,防止有人往里塞真列来「让测试绿」。

**第三张的失败模式最隐蔽**:mobile 推带新字段的交易,服务端返回 **200**、
`accepted` 计数**正常**,但字段**没落库**;下次 pull 回来还是没有,
两端「看起来同步了」。没有报错、没有日志、没有任何信号。

`splits` 故意不在 merge spec 里(子表没有列),由
`_merge_tx_splits_after_merge` 后处理 —— 有测试显式记这笔,免得有人误加。

另外 `test_backup_restore_data.py` 补的是**还原路径真的丢数据**那条
(与上面三张表不同,它验的是端到端结果而非登记完整性)。

`_truncate_ledger` 那条是**白名单式**:新增 `Read*` 表时默认红。
用「有 `ledger_id` 列」区分 ledger-scoped 与 per-user ——
`UserAccountProjection` 是跨账本共享的,按 `ledger_id` 清会**误删别的账本**。

⚠️ 写这类判据时我又一次犯了「恒真」:第一版把
`isinstance(sub.target, ast.Tuple)` 当条件,而 `for model in (...)` 的
target 是 **Name** —— 于是「什么都没找到」= 空集合 = 「没有表被清」=
**全绿**。所以每条判据都要有**自检**:判据找不到东西时必须自己红。

### 备份/还原:原来只测了状态文件管理

`tests/test_backup_restore.py` 的 11 条全是 `status.json` 读法、
目录清理、排序 —— `admin restore_backup` **真正走的数据路径零覆盖**。

而还原是**运维真会执行**的操作:出事时数据已经错了,不是界面难看。
新增的 `test_backup_restore_data.py` 补的是 build -> truncate -> rebuild
的完整往返(腿恰好 2 条、父 `account_name` 仍为空)。

### 变异审计脚本

`tests/` 之外的一次性脚本即可,要点:

- **绝对路径** + 每步 `finally` 还原(用 `cd ..` 拼路径极易出错,
  我因此把文件还原失败过)
- 子进程用 `npx.cmd` 而非 `npx`(Windows 下 `subprocess` 找不到)
- **先打印「锚点没匹配上」并单列** —— 锚点没匹配 ≠ 护栏有效
  (CLAUDE.md 下面那条记的就是这个)

### 验证护栏时先确认回退真的生效

我做 6 次「回退看会不会变红」,3 次没变红 —— 全是**替换脚本没匹配上**,
文件压根没被改。差点把「护栏失效」写进文档。**先确认文件变了,再判护栏。**
