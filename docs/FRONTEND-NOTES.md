# 前端打包与浏览器验证笔记

> 这份文档是 `CLAUDE.md` 的展开。前端**没有 README**,这里是打包体积、
> 首屏度量方式、i18n 约束与浏览器探针的细节。

### 前端产物体积(2026-10-12 重新实测,旧表已作废)

⚠️ **这张表上一版几乎每一行都是错的** —— 写在 i18n 按需加载之前,
`index` 那行差 200 kB,而且列的 `recharts-*.js` **已经不存在了**
(`ls recharts-*.js` -> 0 个,被 i18n 那轮的重构带走了)。

`dist/assets/` 按大小排(raw,实测):

| chunk | raw kB | 说明 |
|---|---|---|
| `index-*.js` | **1058** | 应用壳 + 未被 manualChunks 命中的部分 |
| `react-vendor-*.js` | **176** | 长期可缓存 |
| `framer-motion-*.js` | **125** | 年度报告/动画(**不在首屏**) |
| `zh-TW-*.js` / `zh-CN-*.js` | **82** 各 | i18n 词典,**按需加载**,不进首屏 |
| `AnnualReportEntry-*.js` | 56 | 路由级懒加载 |
| `OverviewPage-*.js` | 54 | 路由级懒加载 |
| `cmdk-*.js` | 43 | 命令面板(共享动态 chunk) |

📌 **别拿这张表当现状** —— 下次 build 后先 `ls -S dist/assets/*.js`
重新量一遍。它在这场会话里已经过期过一次。

### ⚠️ 首屏要看 `index.html` 的 rel,不是看 assets 目录(**已修一处**)

**首屏真实下载** = 入口 `<script>` + 所有 `rel="modulepreload"`。
只看 `dist/assets/` 的文件列表**看不出这件事**:文件都在,
但哪些是首屏的**只有 HTML 说了算**。

验证方法(不装任何东西):build 完 `grep 'modulepreload' dist/index.html`,
或起真服务器把引用的 `/assets/*` 逐个取回来。

#### 🔴 抓的时候**别写精确的属性正则**(214 轮实测)

我第一版用这个量「首屏有几个文件」:

```python
# ❌ 结果:0 个 —— 而实际有 3 条 preload
re.findall(r'modulepreload\" href=\"/assets/([^\"]+)\"', html)
```

真实标签是 **`<link rel="modulepreload" crossorigin href="/assets/...">`** ——
`href` **不在 `rel` 后面紧邻**。于是**三条 preload 一条都没匹配到**。

⚠️ **我差点因此记下「首屏配置回归了」** —— 而实际首屏是
**1316.1 kB raw / 4 个文件**,与基线一致。

> 📌 这就是「**探针的否定结果不能当证据**」的一个具体形态:
> 我拿到的是「**零条 preload**」,读成「首屏配置被改了」——
> 而**真实原因是我的正则假设了属性顺序**。

**正确抓法:抓路径,不抓属性**

```bash
grep -oE '/assets/[a-zA-Z0-9_-]+\.js' dist/index.html | sed 's|/assets/||' | sort -u
```

一条就够,**不关心它在哪个标签、什么属性顺序**。

⚠️ 别用 Python 的 `re` 复刻这个 —— 在 Windows 上写临时文件再读回来时,
`$TEMP` 的路径两边表示法不同(MSYS 的 `/tmp` vs Python 看到的别的),
`FileNotFoundError` 会让人以为是正则写错了。

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

| 变体 | preload | 首屏 |
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

| 阶段 | 首屏 JS |
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
- 控制台 0 错误 / 当时 307 测试通过 / `tsc` 0 错
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

#### ⚠️ 前端是**两道独立的门**,`vitest` 绿 **不等于** `tsc` 绿

`manualChunks` 那条护栏第一版写的是:

```ts
viteConfig.build?.rollupOptions?.output?.manualChunks
```

`vitest` **完全绿**(319 全过)。但 rollup 的类型里 `output` 是**联合类型**
`OutputOptions | OutputOptions[]` —— `pnpm build` 里的 `tsc -b` 立刻报
**TS2339**。

根因:**vitest 走 esbuild,只转译不做类型检查**。所以:

| 门 | 跑什么 | 抓什么 |
|---|---|---|
| `pnpm -C apps/web test` | vitest | 运行时行为 |
| `pnpm -C apps/web build` | **`tsc -b`** + vite build | **类型** + 产物 |

⚠️ 只跑 vitest 就提交,会把**类型不过的代码**推上远端。**两道都要跑。**

⚠️ 同一次里我还栽了**假阴性**:查「三个包单独编译有没有错」时,`find` 没找到
`tsc`,但 `grep -c "error TS"` 在**空输出**上数出 **0**,于是输出
「0 个错误」—— **命令根本没跑成**。换成 `node apps/web/node_modules/
typescript/bin/tsc`(真的有 5.9.3)才拿到真结果。
**「0 个错误」和「没有错误」不是一回事**,尤其在管道里。

（这两道门在 CI 里是同一个 `build` + `test` 步骤,但本地别只跑一个。）

#### 三个 `packages/*/tsconfig.json` 是**装饰性配置**(实测,别再加门)

查「`tsc -b` 到底检查不检查 packages」的结果:

- `apps/web/tsconfig.json` 的 **`include` 显式列了三个包的 `src`**,
  `paths` 也映射了 —— 所以 `tsc -b` **本来就检查这三个包**
- 三个包**各自**的 `tsconfig.json` **没有任何脚本或 CI 引用**
(`package.json` 里只有 `apps/web` 的 `tsc -b`)

⚠️ 实测它们单独编译**也是 0 错误**,但那有点侥幸:包自己的配置**没写
`lib: ["DOM"]`**(apps/web 写了)。DOM 类型是从 `@types/*` **传递泄漏**进来的
—— 实测 `document.title` 在包配置下**不报错**就是证据。哪天依赖树一变
就可能翻车。

⚠️ `packages/ui/tsconfig.json` 还**漏了 `noEmit: true`**(另外两个有),
像复制粘贴时漏的。因为没人跑它,一直没暴露。

📌 **决定不加 `typecheck:packages` 脚本** —— 包**已经被 `tsc -b` 覆盖**,
单独跑一遍验证的只是那三份装饰配置本身。**别给零收益加仪式。**

#### `manualChunks` 的**行为护栏**(突变验证过,1/1 抓到)

护栏:`frontend/apps/web/src/viteManualChunks.behavior.test.ts`。

`vite.config.ts` 里那段「⚠️ **不要**给 recharts 配 manualChunks」的注释
记着一次**实测省下 399 kB** 的改动,但**注释不强制任何东西** ——
加回去首屏就涨回 409 kB,而 `build` 成功、`tsc` 0 错、**测试全绿**。
这正是 bundle 那几轮反复出现的失败模式:**产物变了,但没有任何一道门会红。**

⚠️ 判据必须是**真调那个函数**(import 真实 config + 用真实 pnpm
node_modules 路径调用),**不能**写 `expect(src).not.toContain('recharts')`
—— 注释里就写着 recharts,那是**假护栏**(CLAUDE.md 记的「前端 45% 的护栏
是假的」那一族)。

三条断言:recharts 与 `d3-*` 必须返回 `undefined`;**react 核心仍然进
`react-vendor`**(防「为了躲第一条把整个 manualChunks 删了」的过度修正);
应用源码永远 `undefined`。

