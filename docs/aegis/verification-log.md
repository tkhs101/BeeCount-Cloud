# 验证日志

#### 用 `seed-demo.py` 看概览页(第 52 轮)—— 以及一个**不具区分度**的测试

灌 225 笔 / 193 天后,概览页实测:本月结余 `¥4.66万`、本月收入 `¥38.5万`、
账本 `日常`、货币 **JPY**(`¥` 是日元符号,不是人民币 —— 别当成币种错)。

#### 全量门状态(第 60 轮实测)—— 以及我自己的 lint 债

四道门一次性跑完:

| 门 | 结果 |
|---|---|
| `pytest tests/` | **889 passed** |
| `mypy src` | 60 errors(已定稿的存量线) |
| `ruff check src tests alembic` | **1253** |
| `pnpm -C apps/web test` | **319 passed** |
| `pnpm -C apps/web build` | **`tsc -b` 0 错 + `built in 9.25s`** |
| 首屏 JS | **4 个文件 / 1314 kB** |

⚠️ **ruff 一度变成 1254,是我自己两轮前埋的**:第 57 轮编辑
`test_account_balance_paths.py` 时带进一个 `I001`(import 排序),
而第 58/59 轮我**没跑 ruff**,直到这轮全量体检才看到。

📌 **教训:改了任何被 lint 覆盖的文件,提交前把 lint 也跑一遍** ——
「这轮只改了测试」不等于「lint 不会变」。更具体:**按文件跑 ruff
会漏**(`ruff check tests/新文件.py` 报 All checks passed,问题在
**另一个**我改过的文件里)。**要么全量跑,要么确认你改的每个文件都被覆盖。**

⚠️ build 输出里有一条 framer-motion 的
`Module level directives cause errors when bundled, "use client"` ——
**是打包器的已知提示,不是错误**(build 成功、`tsc` 0 错)。别去追。

#### 审计:CLAUDE.md 里的「护栏」引用有没有指向不存在的文件

第 58 轮发现「failed→502」**声称有保护、实际裸奔**,于是怀疑这是**系统性**的。
把 CLAUDE.md 里所有 `.py/.ts/.tsx/.md/.sh` 文件引用抽出来逐个核对:

| | |
|---|---|
| 引用总数 | **51** |
| **实际不存在** | **1**(`frontend/README.md`,且原文带了「(如有)」限定词) |

✅ **结论:不是系统性现象,第 58 轮那一条是孤例。** 已把那条悬空引用改掉。

⚠️ **审计本身也有假阳性**(和「探针不可靠」同族):正则
`(tests|src|frontend|...)[\w/.\-]*\.(py|ts...)` 把 `apps/web/src/i18n/en.ts`
的**尾部** `src/i18n/en.ts` 当成完整路径 -> 报 7 个缺失,**6 个是假的**。
**路径类断言要确认命中的是完整路径**,不是子串。

📌 **但这条审计只查「文件存在」,查不出「护栏够不够」** ——
存在 ≠ 断言了文档声称的东西(第 53 轮「探针覆盖不到 ≠ 护栏不存在」
是同一族)。**别把「51 个引用都在」当成「51 条保护都有效」。**

#### ✅ 自动还款「失败必须可见」的护栏(第 58 轮,变异验证过)—— **之前裸奔**

CLAUDE.md 一直写着「现在 failed→502 / 配错→400」,**读起来像已受保护**。
实测:`grep -rln "502" tests/*.py` **零结果** —— 没有任何护栏。
而这正是文档记的那条**静默失败**(「用户看到的是『点了没反应』」)。

护栏:`tests/test_autorepay_failure_is_visible.py`(3 条)
变异:把 502 那个 `raise` 换成 `if False: pass` -> 变红,
报 `expected 502, got 200` —— **正是那个故障本身**。

📌 **第三条是反向护栏**:`skipped_disabled` **必须仍然 200**。
用户自己关掉的开关,「无事可做」不是错误 —— 没有它,前两条会逼着人
把正常跳过也改成报错。

⚠️ **探针把我带偏了一次**(值得记):我想「触发一个失败的配置」,
用一张**没配自动还款的卡**去打,拿到 200,差点判成「修复没生效」。
**真因是我的前提错了** —— `skipped_disabled` 才是那张卡的状态,
返回 200 是正确的。`repay_one` 一共有 8 个状态,只有 `failed` 和
`skipped_invalid_config` 该报错。**先列全状态再设计探针。**

⚠️ `repay_one` 在端点里是**函数内 import** —— 必须 patch
`src.services.credit_card.repay.repay_one`(定义处),
patch `src.routers.write.accounts.repay_one` 无效(那个名字不存在)。

⚠️ 这里 mock `repay_one` 是**正当的**:被测对象是「端点拿到 outcome 之后
翻成什么 HTTP 码」,不是 `repay_one` 的算法(那部分另有 e2e + 分层测试)。
**用 mock 不等于测了假的** —— 前提是 mock 的东西不是被测对象。

#### ✅ 组合支付:两条余额路径的「互相比对」补上了(第 57 轮,变异验证过)

第 56 轮记的缺口已补:`tests/test_account_balance_paths.py`
`test_two_paths_agree_on_split_payments` —— 一笔 5000 的支出拆成
卡 3000 + 现金 2000,外加一笔普通支出,然后**SQL 聚合与逐笔 delta
逐账户比对**。

📌 **变异验证:把 `account_balance_delta` 的 `if splits is not None`
改成 `if False`,测试变红** —— 两个账户的 `per_tx_delta` 变成 `0.0`。

这个失败形状值得记:**父交易的 `account_sync_id` 被清空了**,所以
「忘了展开腿」不等于「扣在父账户上」,而是**这笔支出对所有账户零影响**,
账户页余额显示 0。**不报错,只是数字错。**

⚠️ **补这条时踩的三个坑(都是猜签名猜出来的)**:

| 我写的 | 实际 | 报什么 |
|---|---|---|
| `account_balance_stats(db, "lg1")` | 收 **`Sequence[内部 ledger id]`** | 字符串被逐字符迭代 -> `IN expression list` ArgumentError |
| 直接用 `agg[sid]` | 返回**嵌套 dict** `{sid: {count, income, expense, balance}}` | KeyError / 取到 dict |
| 自己查账户取 `initial_balance` | 用 `UserAccountProjection` + `account_balance_from_stats` | 多余代码 |

⚠️ **`ledger_internal` 要自己查**:`Ledger.external_id == "lg1"` -> `.id`,
**不能把 `external_id` 直接传给内部函数**。

⚠️ 变异验证完**第一件事是确认源码真的恢复了**(`git diff --stat` 应为空)
—— 我第一版 `cp` 的备份忘了确认,差点把变异留在仓库里。

#### ✅ 默认分类种子实测:0 → 181,且**只播撒一次**(第 55 轮)

本 fork 最关键的部署前提:**放弃 App 只用 Web 时,新账本不能一个分类都没有**。
实测(全新库、真注册、真建账本):

| 时点 | 分类数 |
|---|---|
| 注册后、还没建账本 | **0** |
| `POST /write/ledgers` 之后 | **181** |
| **再建第二个账本** | **181**(不是 362) |

含 `餐饮` / `税与保险` / `住房` —— 正是 CLAUDE.md 记的那 181 条(官方 177 +
本 fork 新增 4)。

📌 **「只播撒一次」这条有区分度**:再��一次 -> 181 不变,而不是 362。
按名字判幂等做不到尊重用户改名,所以实现是「他一个分类都没有时才播撒」
—— 这条判据正是它成立的原因。

⚠️ 别把 `公共交通` / `医疗` 不在 181 里当 bug —— 那两个是
`scripts/seed-demo.py` 自己加的演示分类,**不属于官方 181 条**。

⚠️ 端点参数全是可选的(`ledger_id` / `user_id` / `q` / `limit` / `offset`),
所以 401 **不是参数问题** —— 我第一次用了 `/auth/register` 的 token 就 401,
换 `/auth/login` 的就通了。⚠️ **两个端点返的 token 作用域可能不同**,
排查鉴权问题时先确认拿的是哪个端点的 token。

#### ✅ 资产页余额**逐笔对账**通过(第 54 轮,JPY 账本)

seed 灌 225 笔后,**用 API 字段独立算出应有余额**,再和 UI 对:

| 账户 | 独立算的 | UI | |
|---|---|---|---|
| 现金 | 34,880.00 | `¥3.49万` | ✅ |
| 招行储蓄卡 | 2,142,870.00 | `¥214.29万` | ✅ |
| 招行信用卡(负债) | −567,590.00 | 负债 `¥56.76万` | ✅ |

**负债没有混进资产**:净值 `¥161.02万` = 资产 `217.78万` − 负债 `56.76万`,
且 `56.76万 ≈ 567,590`。这是「余额是累计口径 + 负债单独处理」的正确表现。

📌 **这是有区分度的测试**:数字是**我自己独立算的**,不是抄 UI。
资产页的余额**不在 accounts 响应里**(只有 `initial_balance`),
是前端聚合的 —— 正是 CLAUDE.md 警告「不要写 `t.amount`,要用 `baseAmount()`」
的地方。**独立算出基准再对**,才证明聚合口径没错。

⚠️ 对账时我又栽了字段名:**账户响应里没有 `balance`**,只有
`initial_balance`。`a.get("balance")` 返回 `None` 看着像「余额是 null」——
**先打印 `sorted(a.keys())` 再取字段**(与第 50 轮 snake/camelCase 同族)。

✅ **「税与保险扇区钉住」早就验过了 —— 我上一轮的「未验证」结论是错的。**

第 52 轮我只在浏览器里看(恰好不具区分度:该分类排第 2,有没有钉住规则
结果一样),就写下「没验成」。**其实 `donutSlices.test.ts` 里有两条护栏**:

| 护栏 | 判据 |
|---|---|
| 纯函数 | 显式构造 **9 个分类、让「税与保险」排第 9**,断言仍被显示 —— **这才是有区分度的用例** |
| **接线** | 解析 `HomeMonthCategoryDonut.tsx` 的 **AST**,断言 `buildDonutSlices` 的调用实参**带着钉住名单** |

📌 **接线那条是关键**:纯函数测试自己显式传参,组件忘传也照样全绿 ——
税额扇区会静默不钉住。这与「护栏只测纯函数、漏了调用点」是同一族,
手法和 `hookPlacement.test.ts` 相同。

⚠️ **教训:说「某功能没验证」之前,先确认护栏真的不存在** ——
我只看了浏览器表现就下结论,而**只测一端不能推出另一端没测**。
这是本会话「探针的否定结果不能当证据」的**又一个形态**:
探针覆盖不到 ≠ 护栏不存在。

#### ⚠️ 动手写工具前先看仓库里有没有(第 51 轮,连栽三轮)

`curl` 发中文失败之后,我**手写了三段 UTF-8 正确的 API 客户端**(48/49/50 轮
各一次)。而 `scripts/seed-demo.py` **早就在仓库里**,而且:

- 用 Python —— UTF-8 by construction,不会被 shell 编码坑
- 用 `BC_BASE_URL` 环境变量,可指向任意部署
- 覆盖更全:实测一次灌入 **225 笔交易 / 10 个分类 / 含税额**(含「税与保险」
  钉住项),比我手搓的 3 笔强得多

**我的反应是「自己写一个更快的」,而不是「仓库里是不是已经有」。**
结果:三轮 vs 一次,而且三轮里还额外踩了 3 个坑。

⚠️ 这与「grep 搜不到 ≠ 不存在」是同一族,但**更该警惕**:那一条是
「搜不到就以为不存在」,这一条是**「根本没搜」**。

**动手写任何工具/脚本/夹具之前先 `ls scripts/` `grep -rn "关键词"`。**

📌 顺带查清:`scripts/seed-demo.py`(6 个月演示数据)与
`scripts/seed_demo.py`(**部署引导用户**,且注释写明演示账本部分已被移除)
**职责不同,不是重复实现** —— 我一度以为是「同一件事两份实现」要合并,
读了文件头才发现不是。**别凭文件名猜。**

用法:

```bash
BC_BASE_URL=http://127.0.0.1:8951 python scripts/seed-demo.py
# -> 225 笔交易;登录 demo@beecount.local / demo123456
```

#### ✅ 真实金额 + 时区在浏览器里验过了(第 49 轮)

建了 3 笔真实交易(含税额),在 `playwright-cli` 里看渲染结果:

| 输入(UTC) | 页面显示 | 核对 |
|---|---|---|
| `2026-10-10T12:00:00Z` | `2026-10-10 21:00` | +9 ✅ |
| `2026-10-11T09:30:00Z` | `2026-10-11 18:30` | +9 ✅ |
| `2026-10-12T08:00:00Z` | `2026-10-12 17:00` | +9 ✅ |

金额:`-42.50` / `-88,000.00` / `-1,234.56` —— 千分位与两位小数都对。
分类(交通/购物/餐饮)、账户(招行卡)、备注都正常,`rawKeys: 0`。

⚠️ **浏览器在 Asia/Tokyo,所以 +9 是正确的** —— 别看到「时间变了」就当 bug。
**先确认时区再下结论**(与「浏览器探针不可靠」同族)。

**这条验的是 CLAUDE.md 记了四次同类事故的那条 UTC 归一链** —— 单测
`test_happened_at_timezone.py` 只证明函数返回值对,这条证明了**经过
写入 → 投影 → 读端 → 前端渲染**整条链之后时刻仍然正确。

⚠️ **用 `curl` 建带中文的数据会失败**(报 `error parsing the body`)——
**不是产品 bug**。判决性对照:同一个端点、ASCII 账本名**成功**、
中文名失败 = shell 编码把中文弄成了非法 UTF-8。
**发中文 payload 用 Python `urllib` 显式 `.encode("utf-8")`**。

⚠️ **REST 响应的字段是 snake_case,查的时候别用 camelCase。** 第 50 轮查
税额时用 `"taxAmount" in x` 判定,拿到 **False**,一度以为是 CLAUDE.md 警告的
「反向桥漏新列」静默丢失。真名是 **`tax_amount`** —— camelCase 转换在前端
`api-client` 里做,不在服务端。

三层证据排除了 bug(schema 有该字段 → DB 里 `113.56` → 响应里
`tax_amount: 113.56`)。**税额链路写入/投影/读端全通。**

⚠️ 写路径的真实路径是 `/write/ledgers/{ledger_id}/transactions`,
字段是 **snake_case**(`category_name` / `account_name` / `base_change_id`
必填),**不是 camelCase** —— 猜字段名会连撞 400 和 405。

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

