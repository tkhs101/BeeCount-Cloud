# 测试与护栏的经验

> 这份文档收集「**怎么把一条契约变成一道会红的门**」的经验。
> 它们原先挂在 `CLAUDE.md` 的「新增:信用卡自动还款」标题下面 ——
> **和信用卡毫无关系**。
>
> ⚠️ **根因不是「删了章节导致孤儿」**(我 203 轮就是这么写的,**错的**)。
> 204 轮查 git 历史:`### 时区` 那一节**首次出现**时(`ba53c72b`,
> CSV 时区修复那一轮)**前面最近的 `##` 就是「信用卡自动还款」**,
> 而且那一版只有 13 个 `##` —— **它从一出现就挂在那里**。
>
> **真实原因:往文档末尾追加内容时没有新建 `##` 章节**,
> 于是 markdown 层级自动把它挂成了前一节的子节。**没有任何东西被删过**
> —— 133 次提交里 `##` 标题集合基本没变。
>
> 📌 **为什么这个错误推断值得写下来**:「某个章节被删了 ->
> 剩下的挂到了前一个标题下」是个**听起来很合理**的故事,我先写了它、
> 改了文档才去查。而 CLAUDE.md 里早写着「**别先给字段编语义,再去套现象**」
> —— 同一个坑的两种外形:**先编一个因果,再拿它解释眼前的结构。**
>
> **`CLAUDE.md` 是速查表,这里才是护栏作者的手册。**

## 里面有什么

| 主题 | 小节 |
|---|---|
| 时区(五处归一,已栽五次) | `_to_iso8601` / `_parse_happened_at` / `_to_utc` / `_parse_dt` / `_localize_naive` |
| 护栏自身的陷阱 | 源码级断言恒真(tokenize)、变异审计、变异没被检出的三种原因 |
| 测试的四种「单跑绿全量红」 | 模块级 env、模块级单例、全局 engine、连接未释放 |
| 静默损坏 | 反向桥漏列、CSV 往返、`/sync/push` 丢字段 |
| 工具 | 变异审计脚本、数东西用权威来源、验证回退真的生效 |

---

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

**还有第三种原因:测的根本是别的路径。** 173 轮实测:把 delete_tx 里
那句 delete_tx_splits 去掉, `test_backup_restore_data.py` 的 **4 条全绿** ——
因为那 4 条走的是 `_truncate_ledger`/`rebuild`,**压根不调 `delete_tx`**。
换成 `test_tx_split_invariants.py` 立刻变红。

📌 **「变异没被检出」先问三件事:护栏失效?被上层挡住?还是**它测的是
另一条路**?第三种最容易被误判成第一种。

不是护栏失效,是**上层还有一道**:`_merge_tx_splits_after_merge` 总会把
`splits` 补进 merged（要么来自 payload,要么来自现有腿）,所以同步路径
走到 `upsert_tx` 时那个条件**恒真**。

那条守卫保护的是**不走 merge 的调用方**（REST 写路径）。变异审计要么换个
调用面,要么在注释里写明它防的是谁 —— 否则下一个人会以为它没作用而删掉。

### ⚠️ 别先给字段编语义,再去套现象

断言一个数之前,**先确认那个字段到底算什么**。

本会话在一个「为什么读端点算 60000」的现象上连推翻七个假设
(窗口 / 两份实现 / 账期方向 / 粒度 / 落库 / 入参 / 窗口被改宽),
**每一个看起来都有证据支撑** —— 探针完整、入参一致、落库正常、数据全对。
真因是:**`statement_bill` 是「截止账单日的累计变动」,不是「窗口内的和」**
(见上面信用卡那节的不变式 1)。

**没有任何一个环节看起来有问题**,所以每个假设都能自圆其说 ——
这比明显的错误难推翻得多,多花了十几轮。

📌 **动手改之前,先读那个字段/函数的定义或注释**,别先假设一个语义
再拿现象去套。同族:「已读源码」≠「路径可达」,「跑通了」≠「验过了」。

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
「下一次同样的事还会发生」。所以现在**四张**表全部有结构性护栏:

| 登记表 | 漏登记的后果 | 护栏 |
|---|---|---|
| `_projection_row_to_tx_dict`(反向桥) | Web PATCH 静默抹掉新列 | `test_projection_reverse_bridge_coverage.py` |
| `_truncate_ledger`(清理清单) | rebuild / 还原后余额「凭空多出来」 | `test_truncate_ledger_coverage.py` |
| `_LEDGER_MERGE_SPECS`(同步 merge) | **`/sync/push` 静默丢弃新列** | `test_sync_merge_spec_coverage.py` |

三者都是**白名单式**:加新东西时**默认红**,要求显式论证为什么可以不登记。
黑名单在加东西时**默认通过** —— 而那正是要防的情况。
每条白名单都带**上限**,防止有人往里塞真列来「让测试绿」。

**第三张(`_truncate_ledger`)用 `ast` 解析取 `for model in (...)` 里的
标识符名** —— 不是源码文本匹配,所以对「改名就绕过」免疫。实测:从那个
元组里删掉一张表 -> **两条测试变红**。

**同步 merge 那张的失败模式最隐蔽**:mobile 推带新字段的交易,服务端返回 **200**、
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

`tests/test_backup_restore.py` 的 10 条全是 `status.json` 读法、
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

### 数东西要用**权威来源**,别用方便的那个

| 要数的 | ✅ 用 | ❌ 别用 |
|---|---|---|
| 测试条数 | `pytest --collect-only -q` | `grep -c "^def test_"` |
| 首屏体积 | 起真服务器看 `index.html` 的 `rel` | `du -sh dist/` |
| chunk 大小 | `ls -S dist/assets/*.js` + `stat` | 手抄旧表 |

⚠️ **`grep "^def test_"` 会漏掉 `class` 里的方法** —— 本会话靠它差点把一个
**正确**的「20 条」改错。`pytest --collect-only` 才是权威。

📌 **由此得到一条更通用的规则**:

> **两个量法给出不同结果时,先怀疑量法,别先怀疑对象。**

我那次先判 `test_tx_split_invariants` 错(直觉方向反了),
再判 `test_backup_restore` 错(碰巧对)。**按直觉先改那一条,
就会把一份准确的文档改错,而且看不出任何异常。**

⚠️ 同一类里还有:某探针返回 `0` 或空,可能是**输出格式不同**而不是
「没有这个东西」。至少两种探针形态这么坑过我:命令没跑成(静默失败)、
`-q` 模式输出格式与预期不同。

### 验证护栏时先确认回退真的生效

我做 6 次「回退看会不会变红」,3 次没变红 —— 全是**替换脚本没匹配上**,
文件压根没被改。差点把「护栏失效」写进文档。**先确认文件变了,再判护栏。**
