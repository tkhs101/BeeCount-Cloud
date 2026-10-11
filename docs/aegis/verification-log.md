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

## 第 61 轮:CLAUDE.md 一度 82 kB,超了单轮上下文预算

每轮开头都有一条 `truncated CLAUDE.md from 86550 to 65243 bytes`,
**我一路当噪声跳过了 60 轮**。一份「阅读指南」如果每次都被截断,
它就没在履行职能 —— 而它超预算正是我自己造成的。

先量构成:84 kB 里带「第 NN 轮」的小节只有 11 kB(13%),
**大头 73 kB 是长期内容,不能删**。所以问题不是日志膨胀。

结构上有一条很清晰的分界:**225-541 行是长期指南**(bundle / i18n /
前端两道门),**542 行之后全是逐轮验证记录**。

把后者移到本文件,CLAUDE.md 82 -> 59 kB,一条不删。

📌 **判据不是「重要性」,是「会不会过期」**:一句话里出现「第 NN 轮」,
它多半属于日志。下一个人做决策需要的是「这个功能有什么契约、
哪里容易坏」,而不是「60 轮前某次探针坏在哪」。

## 第 62 轮:CI 表里的数字过期了

`CLAUDE.md` 的 CI 状态表写着 `885 passed`,实际已经是 **889**
(第 57/58 轮补了两条护栏)。**那是大多数人查现状会看的地方。**

已改成 889,并加了一句:数字是快照,以实际跑出来的为准。

⚠️ 写这句时又栽了本会话第 49 轮记过的坑:**bash heredoc 把中文「的」
弄成乱码**。虽然换了 Python(比 heredoc 可靠),但**光换工具不够 ——
还得看结果**。工具只能降低概率,确认才是保证。

📌 这三轮是同一个形状的三个维度:

| 轮 | 维度 | 漂移形态 |
|---|---|---|
| 58 | 护栏引用 | 文档**声称有**,实际**裸奔** |
| 59 | 文件存在性 | 51 个引用里 1 个失效(且带「如有」限定词) |
| 62 | **数字** | 表格给的是过时快照 |

第 59 轮我审计完说「不是系统性现象」—— **那是对文件存在性这一个维度
说的**,数字准确度是另一维度,同样会漂。**一个维度清了不等于清了。**

⚠️ **不要自动化这件事**:写一条「pytest 数 == 885」的测试,每加一次测试
就会红。正确的做法是那句提醒 —— **数字是快照**。

## 第 64 轮:mock 的状态是不是真实可达的?

第 58 轮那条护栏是 **mock `repay_one`** 做的。而 CLAUDE.md 写着
「凡是替换掉真实 IO 的测试夹具,证明的只是算法对,不是接线对」。

所以留下一个没验证的疑点:**真实的 `repay_one` 到底会不会产出
`status="failed"`?** 如果它从不产出,那条 502 就是死代码 ——
护栏会永远绿,而真实故障路径根本走不到。

查了,闭环:

| 位置 | 事实 |
|---|---|
| `repay.py:153` | `repay_one` **整个函数体**包在 `try:` 里 |
| `repay.py:161-164` | `except Exception` -> `RepayOutcome(..., "failed", ...)` |
| `accounts.py` | `if outcome.status == "failed"` -> `HTTPException(502)` |
| 护栏 | mock 的正是这一格 |

**所以 mock 的是一个真实可达的状态,不是虚构的。**

📌 **但这只证明「状态可达」,不证明「端点接线对」** —— 后者靠的是
`tests/test_smoke_real_app.py` 那条真服务冒烟。两者分工:mock 测
「拿到 failed 就翻成 502」,真跑测「真的能走到 failed」。

⚠️ **别把「我验证了 mock 的合法性」当成「我验证了整条链」** —— 前者
排除了「护栏在测虚构状态」,后者仍需真跑。这与「探针覆盖不到 ≠ 不存在」
同族:**验了一个环节不等于验了全链。**

## 第 65 轮:真服务冒烟对「手动触发」只断言「没报错」

上一轮我说「端点接线由 `test_smoke_real_app.py` 那条真服务冒烟负责」。
查了 —— **那个说法不成立**。

`tests/test_smoke_real_app.py:189` 对手动触发只有一句:

```python
assert r.status_code < 500, f"{r.status_code}: {r.text[:300]}"
```

这正是 CLAUDE.md 明令禁止的那种断言 —— *「断言必须落在**产出物**上,
不能是「没抛异常」」*。它对 `paid` 与 `skipped_not_due` **完全不区分**:

| 它证明了 | 它没证明 |
|---|---|
| 端点可达、不 5xx | **真的还了钱** |

⚠️ 所以目前的状态是:**502 那条护栏测「状态 -> HTTP 码」,
冒烟测「不崩」,中间那截「真的会走到 failed / 真的会还钱」没有断言。**

📌 要补的话判据很明确:建卡时把 `payment_due_day` 设成**今天的日**,
这样 `is_due_on` 才为真(CLAUDE.md 记过这个坑:`today` 必须是**正好**
还款日,不是「过了还款日」),然后断言**真的多了一笔扣款交易**。

⚠️ 还没补 —— 建卡 + 凑够账期窗口的夹具不便宜,且它会碰
「真实扣钱」的路径。**先记下准确规格,别塞一个半成品进去。**

📌 顺带一条方法论:**上一轮那句「接线由冒烟负责」是我自己下的结论,
没查就写进文档了** —— 和第 59/62 轮同一个形状(单维度的证据推出全局结论)。
**给自己刚写的东西做核查,和给自己刚写的代码做测试,是同一件事。**

## 第 66 轮:试补那条护栏,**没做成,撤回了**

第 65 轮记下了规格(建卡时 `payment_due_day` = 今天的日,断言真的多一笔
扣款交易)。这轮动手,**两次失败后撤回**,但换来了具体信息。

三个我踩到的坑(都是**夹具**问题,不是产品问题):

1. **`smoke` fixture 返回 `(client, H)`**,第二项是**请求头**不是
   `LEDGER`(`LEDGER` 是模块常量 `"smoke-ledger"`)。我按 `(client, LEDGER)`
   解包,结果把整个 headers 拼进了 URL -> 404
2. **`smoke` 每测试注册一个新用户**,账本得**在测试里自己建** ->
   否则 `LEDGER_NOT_FOUND`
3. ⚠️ **真正难的那个**:账期窗口。`payment_due_day` 就算等于今天的日,
   那笔测试支出**可能落在账期窗口之外** —— `billing_day=10` 而今天是 11 号
   时,今天的支出算进**下一期**,于是 `outstanding == 0` ->
   `skipped_zero_outstanding` -> **一笔交易都不产生**(实测 1 -> 1)。

📌 所以第 65 轮那句「夹具不便宜」是**低估了**:不只是要凑日期,
还得让**支出落在当前账期窗口内**。`window_start` 那条不变式
(在原始 `billing_day` 空间重算、不能二次钳制)在这里是拦路的。

**撤回而不是留半成品** —— CLAUDE.md 写着「别给零收益加仪式」,
一条永远红的测试比没有更糟。而**失败过程本身要记下来**,否则下一个人
会以为「规格很清楚,照做就行」。

⚠️ 这轮还印证了本会话反复记的那条:**连着两次修复失败时,该停下质疑的
往往不是环境,是自己的假设** —— 我该先量账期窗口再动手,而不是先写代码。

## 第 67 轮:上一轮那个因果解释是错的,真正的结构性原因在这

第 66 轮我把 1->1 归因于「支出落在账期窗口外」。**量了之后:那个解释是错的。**

`current_billing_period` 的 `statement_date` 是**下一个**账单日(在未来),
不是最近一张已出账单 —— docstring 明写「10/11 看账单日 10 号的卡,
本期窗口是 (10/10, 11/10]」。实测四种配置:

| billing_day | 窗口 | 今天在内 | is_due_on |
|---|---|---|---|
| 10 | (10-10, 11-10] | ✅ | ✅ |
| 11 | (09-11, 10-11] | ✅ | ✅ |
| 12 | (09-12, 10-12] | ✅ | ✅ |
| 20 | (09-20, 10-20] | ✅ | ✅ |

**四种全都包含今天**。窗口根本不是原因 —— 我把一个没验证的解释写进了文档。

⚠️ 又栽在**签名**上:`current_billing_period` 是 `(today, *, billing_day,
due_day)` —— 关键字参数。我按位置传 -> TypeError。**读定义,别推**
(这是本会话重复次数最多的一条)。

## 真正的结构性原因:端点**不暴露 outcome.status**

`WriteCommitMeta`(`src/schemas.py:823`)只有 6 个字段:`ledger_id` /
`base_change_id` / `new_change_id` / `server_timestamp` /
`idempotency_replayed` / `entity_id` —— **没有 status**。

`repay_one` 辛苦分出的 8 个状态,在端点里被**消费成 502/400 就丢掉了**,
成功路径上一个都不返回。这解释了之前所有的困惑:

| 现象 | 真正的原因 |
|---|---|
| 冒烟只能断言 `< 500` | 响应里**没有可断言的东西** |
| 我的测试只能数交易条数 | 同上 |
| **用户点了「手动触发」** | **还了 30000 和什么都没还,响应一模一样** |

⚠️ 最后一行是**产品问题**,和 CLAUDE.md 记的那条静默失败**同族但方向相反**:
那条是「失败被报成 200」,这条是「**成功到什么程度都不告诉你**」。

📌 要补那条护栏,判据不该是「数交易条数」,而应该是**先把 status
暴露出来**(响应加字段,或端点至少在 body 里带上 `skipped_*` 的原因),
再断言它。**现在的响应形状下,任何护栏都只能间接推断** —— 这才是
第 65/66/67 三轮都做不成的根本原因,不是夹具难。

📌 **教训(比这条测试本身重要)**:我把一个**没验证的解释**写进了文档,
下一轮据此又改了两次代码方向。**记录原因时要标明它是「实测」还是「推测」**
—— 我那条写得像实测。

## 第 68 轮:为什么这件事耗了三轮 —— 因为**没有信号可跟**

`repay_one` 全函数只有两处日志,**都只在异常路径**:

| 行 | 分支 | 状态 |
|---|---|---|
| `repay.py:158` | `except AutoRepayConfigError` | `skipped_invalid_config` |
| `repay.py:163` | `except Exception` | `failed` |

**其余 8 个状态全部静默返回,一行日志都没有**:
`paid` / `partial` / `skipped_not_due` / `skipped_already_repaid` /
`skipped_disabled` / `skipped_no_funds` / `skipped_zero_outstanding` /
`skipped_locked` / `skipped_no_schedule`。

## 两条发现合起来是一个完整的「不可观测」

| | 现状 |
|---|---|
| **API 响应** | `WriteCommitMeta` 没有 `status`(第 67 轮实测) |
| **服务端日志** | 只有 `failed` / `invalid_config` 有,**正常与跳过全静默** |

**合起来:「我的钱为什么没动」这个问题,系统给不出任何答案。** ——
接口不说,日志也不说。三轮下来我只能靠**猜**(`is_due_on` 猜一次、
账期窗口猜一次、夹具猜一次),而每一次猜错都得再花一轮去证伪。

⚠️ 这不是「我不够仔细」,是**缺可观测性**。CLAUDE.md 早就写着
「一致的口径要有唯一来源」「失败不许被吞」,但这条讲的是**异常**;
**正常路径上的「什么都没发生」同样需要说一句** —— 否则用户和排查者
面对的是同一种沉默。

📌 **两个都很小的修法**(都还没做,记下来):

1. `repay_one` 里加一行 `logger.info("autorepay.done card=%s status=%s detail=%s", ...)`
   —— 一次就覆盖全部状态,手动触发和调度器都受益
2. `POST .../autorepay/run` 的响应体带上 `status` —— 让调用方(前端、
   冒烟测试、未来的护栏)不必靠数交易条数反推

📌 **第 66 轮我花了三轮才发现「不是夹具难」** —— 更准确的说法是:
**在 status 不可观测的前提下,任何护栏都只能间接推断,而间接推断
必然靠猜。** 先修可观测性,再补断言,顺序不能反。

## 第 69 轮:加了那条日志 —— **动手比预想的难**

第 68 轮写的修法一,`repay_one` 里加一条覆盖全部状态的日志。
**四次才改对**,记下来因为过程本身有教训:

| 次 | 错在哪 |
|---|---|
| 1 | 把 `return _repay_inner(...)` 改成先赋值再 return,**`try/except` 结构被拆断** |
| 2 | 用局部字符串拼接补括号,**把 f-string 切成 `...{e}""`** |
| 3 | 插入条件写成「本行是 `try:` 且下一行含 `_repay_inner`」—— **跨行条件永远不匹配**,helper 根本没插进去 |
| 4 | 恢复备份 + **整行精确替换**,一次过 |

⚠️ 第 3 条是本会话最基础的一条:**条件写在同一行**,别指望它去看下一行。
`ln.startswith("    try:") and "_repay_inner" in ln` —— 同一行里永远
两个条件都不成立,于是「没报错」+「helper 不存在」= `NameError`。

📌 **四次里有三次是因为我在拼接字符串而不是替换整行。**
对已经跑通的代码做微改,**逐个 assert 锚点存在**、一次只改一处 ——
这跟护栏变异验证是同一套纪律:先确认锚点匹配上,再动。

## 结果

`repay_one` 现在**每次都留一句** `autorepay.done card= status= detail=`,
覆盖全部 10 个状态;手动触发与调度器共用这一条。

889 passed / ruff 干净。**「我的钱为什么没动」现在服务端日志能回答了** ——
响应还是不说 status(那是下一个修法),但**日志能说了**。

⚠️ 我**没有**顺手把 status 加进响应 —— 那是改 API 形状,影响前端
`authedPost<WriteCommitMeta>` 的类型,得单独验。**一次只做一件事。**

## 第 70 轮:验证那条日志**真的生效** —— 顺带抓到我自己又一个没核实的说法

上一轮加了日志就提交了。这次按 CLAUDE.md 那条
「**先确认回退真的生效再判护栏**」去验,发现两层。

## 一、第一次量到「0 次」—— 是 pytest 藏了 INFO

`pytest` **默认只显示 WARNING 及以上**,`logger.info` 根本不输出。
加 `--log-cli-level=INFO` 才看得到:

```
INFO beecount.credit_autorepay:repay.py:162 autorepay.done
     card=acc_34cfcb69c97f status=done ...
```

⚠️ **这次「0」不是改动没生效,是我的探针量错了层级。** 与第 59 轮
那个正则截断路径是同一族:**探针的否定结果不能当证据**。

📌 顺带:`test_credit_autorepay_repay.py` 里 **`repay_one` 出现 0 次** ——
它压根不调被测函数。所以「跑它看不到日志」是正常的,别据此怀疑改动。

## 二、抓到我自己第 68 轮写的状态列表**是推测的**

日志里成功状态叫 **`done`**,**不是 `paid`** —— 我在第 68 轮那张表里
写 `paid`,那是**照着字面想的**,没核。

第 68 轮我只 `grep` 到 8 个 `skipped_*`,**没有 grep 到成功那个叫什么**,
却把表写成了「其余 8 个状态」并暗示完整。这和「把推测当实测写进文档」
是同一个毛病,只是这次更隐蔽 —— 因为**大部分条目是对的**,错的那一个
藏在措辞里。

📌 **修正后的判据**:要枚举一组值,得先确认**全集** ——
`grep '"skipped_'` 只覆盖前缀为 skipped 的,不代表「其余都是」。
**grep 到的和没 grep 到的,要分开说。**

## 结论

日志改动**确实生效**(status=`done` 已实测)。本轮无代码改动,
889 passed / ruff 1253 状态不变。

## 第 71 轮:换一种**更强**的判据 —— 结构性覆盖,不是「跑一次看到了」

第 70 轮我用「跑测试看到日志」确认了改动生效。但那只是**一次观测**。

这轮问了个更根本的问题:**`repay_one` 里有没有哪条返回路径绕过了那条日志?**

```
awk '/^def repay_one/,/^def _repay_inner/' | grep -E "return |_done\("
```

结果:**三个 `return` 全部包在 `_done(...)` 里,一条都没漏** ——

| 返回路径 | 状态 | 过日志 |
|---|---|---|
| `_repay_inner` 正常返回 | `done` 等 | ✅ |
| `except AutoRepayConfigError` | `skipped_invalid_config` | ✅ |
| `except Exception` | `failed` | ✅ |

📌 **这比「跑一次看到了」强**:一次观测只证明「这条路上有日志」,
**结构检查证明「所有路上都有日志」** —— 后者才是要的那个性质。

## 从源码穷举出来的准确清单(不是推测)

```
grep -ohE 'RepayOutcome\([^,]+, *"[^"]+"' src/services/credit_card/*.py
```

得到 **9 个**:`failed` / `skipped_already_repaid` / `skipped_disabled` /
`skipped_invalid_config` / `skipped_locked` / `skipped_no_funds` /
`skipped_no_schedule` / `skipped_not_due` / `skipped_zero_outstanding`,
**加上运行期观测到的 `done`,共 10 个**。

✅ `partial`(部分还款)**已查清:它不是状态**。`repay.py:300` 里
`partial = payable < outstanding` 是一个**局部布尔值**;
`payable = min(outstanding, source_balance)`(233 行)—— 余额不足就少还,
但**状态仍是 `done`**,靠 detail 表达「还了部分」。

所以:**10 个状态的清单是对的**,CLAUDE.md 那句「余额不足 → 部分还款」
在语义上也准确。**上一轮留的疑点到此关闭** —— 当时写「很可能」,
这次查过了。

📌 这正是上一轮该做而没做完的一步:**别把「很可能」留在文档里**。
一个查一下就 5 秒钟的疑问,留成「很可能」就会在下一轮被当成事实用。

📌 顺带:`repay.py:321` 还有一处 `logger.exception("autorepay: failed to
release ...")` —— 那是**锁释放**的异常,不是状态机的一部分。
**grep 状态时会带上它,别混进清单。**

## 本轮无代码改动,889 passed / ruff 1253 不变。

## 第 73 轮:量了「响应加 status」的真实风险 —— 并给出该怎么做

第 68 轮修法的第二步(响应暴露 `status`)一直没做。我当时的理由是
「改 API 形状会影响前端类型,得单独验」。**这轮把风险量出来**:

| 事实 | 值 |
|---|---|
| `WriteCommitMeta` 被引用 | **17 个文件**(共享 schema) |
| 前端声明 | `frontend/packages/api-client/src/types.ts:117`(**手写**类型) |
| 现有可选字段写法 | `entity_id: str \| None = None` / `idempotency_replayed: bool = False` |

**风险比我原先判的低**:加一个 `autorepay_status: str | None = None`
完全沿用现有模式 —— 服务端 17 处引用**不受影响**(有默认值),
前端**会直接忽略**多出来的字段(TS 不校验运行时形状)。

⚠️ **但技术上可行 ≠ 该这么做。** 为一个端点的可观测性去改一个
17 文件共享的 schema 是**设计坏味道**:以后每个端点想带点自己的信息,
都要往这个公共壳里加字段。

📌 **推荐做法:给 `run_autorepay_now` 一个自己的响应类型**,而不是
扩 `WriteCommitMeta`。代价是多一个 schema 类,收益是这个公共壳保持干净、
语义诚实(`WriteCommitMeta` 本来就不该承载「还款结果」)。

📌 顺带:第 66-72 轮我连着七轮基本只在写验证日志,真正的代码改动只有
第 69 轮那条日志。**这本身是个信号** —— 该动手的时候不要只写文档。
剩下这一件事需要「改 schema + 改端点 + 跑前端 build + 跑全量后端」,
一轮做不完整,留给有干净上下文的一轮。

**已闭环**:服务端日志已能回答「为什么没动」(第 69 轮 + 第 71 轮结构性验证)。
**未闭环**:响应仍不暴露 status;「手动触发真的会还钱」仍无断言 ——
但**现在可以写了**,因为 status 一旦暴露,判据就不再需要间接推断。

## 第 74 轮:动手了,**没做成,撤回** —— 但暴露了一个真机制

按第 73 轮的决定给 `run_autorepay_now` 单独的响应类型。四处都改对了
(schema 加 `AutoRepayRunResult(WriteCommitMeta)`、端点 `response_model`
与 `status=outcome.status`、`_shared` 的 import),**但跑不起来**。

## 真机制:`_shared.__all__` 是**白名单**,且不止一层

```
NameError: name 'AutoRepayRunResult' is not defined
```

查到的链路是三跳:

| 层 | 做了什么 |
|---|---|
| `accounts.py` | `from ._shared import *` —— 桶导入 |
| `_shared.py` | `from ...schemas import (...)` —— **显式列举** |
| `_shared.py:1325` | `__all__ = [...]` —— **显式白名单** |

所以「在 import 块里加了名字」**不够**,还要进 `__all__`。我补了
`__all__` 之后**从 57 个 error 变成 60 个** —— 说明还有别的层没对上,
而我上下文不够继续查。

📌 **这解释了本仓 `F405` 为什么有 1131 条** —— CLAUDE.md 一直说
「桶导入模式的必然结果」,但没说清它其实是**两层白名单**:
`_shared` 的 import 列举 + `__all__`。**加一个从 schemas 透出的新名字,
两处都要动**,漏一处就是 `NameError`,而且报错指向使用者、
不指向真正漏掉的那一层。

⚠️ 这也说明:`_shared.__all__` 是**第三张手工登记表**(CLAUDE.md 已经记了
`_projection_row_to_tx_dict` / `_truncate_ledger` / `_LEDGER_MERGE_SPECS`),
**但它没有默认红的护栏**。加 schema 名字忘了加 `__all__`,要等运行时炸。

## 撤回,不留半成品

`git checkout` 三个文件 -> **889 passed / ruff 1253**,diff 全 0。
57-60 个 collection error 的状态**绝不能留在仓库里**。

📌 **给下一轮的具体做法**:端点改成 `response_model=AutoRepayRunResult`
之后,**不要靠桶导入** —— 在 `accounts.py` 里显式
`from ...schemas import AutoRepayRunResult`,跳过 `_shared` 那一跳。
或者干脆给 `__all__` 加一条护栏(枚举 schemas 里所有 `*Result`/`*Meta`
类,要求都在 `__all__` 里),这样这类错误**默认红**。

## 本轮无代码改动(已撤回),状态不变。

## 第 75 轮:把那张**没有护栏**的登记表护住(变异验证过)

第 74 轮栽在 `_shared.__all__` 上,而它是**第四张手工登记表** ——
CLAUDE.md 已记的三张(反向桥 / `_truncate_ledger` / `_LEDGER_MERGE_SPECS`)
都有「白名单式、加新东西默认红」的护栏,**这张一张都没有**。

护栏:`tests/test_shared_all_registry.py`(2 条)
判据:`_shared` 从 `src/schemas.py` 导入的每个公开名,必须在 `__all__` 里。

实测现状:导入 **14** 个公开名,**不在 `__all__` 的 0 个** ——
**现状干净,但「新加一个」会变红**,这正是要的形状。

📌 **判据为什么用 ast 而不是字符串查找**:`WriteCommitMeta` 在
`_shared.py` 里出现 **15 次**,大多是文档字符串和函数注解 ——
`expect(src).toContain(...)` 那种判据在这里**恒真**
(CLAUDE.md 记的「前端 45% 的护栏是假的」同一族)。

## ⚠️ 变异验证时我**第一次把变异做反了**

第一次变异:把 `WriteCommitMeta` 从 **import 块**里删掉 ->
**护栏没红**。

因为那样 `_parse()` 根本不会再收集到这个名字,`missing` 自然是空 ——
**测的根本不是我担心的那个方向**。

真正的 bug 是「**导入了,但不在 `__all__` 里**」,所以变异必须是
**从 `__all__` 里删**。改对之后:`FAILED test_schemas_names_are_exported_via_all`。

📌 **又一次「变异没生效就差点下结论」** —— 第一次我看到「没红」,
如果就此收工,就会把一个**没验证过的护栏**当成已验证。
CLAUDE.md 早就记了:「先确认回退真的生效,再判护栏」。
**这次多花了一轮,值得。**

## 891 passed / ruff 1253。
