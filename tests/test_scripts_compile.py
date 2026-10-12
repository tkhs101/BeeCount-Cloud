"""护栏:仓库里的**脚本与 YAML 配置**必须是**合法**的。

## 为什么要有这条(179 轮实测踩到)

`scripts/` 下的 8 个脚本**曾经一道门都没有**:

| 门 | 覆盖吗 |
|---|---|
| `pytest tests/` | ❌ **不 import** 它们(实测 \`grep -rn "scripts/" tests/*.py\` -> **0** 命中) |
| `ruff check src tests alembic` | ❌ **不含** \`scripts/\`(CI 里写死的那三个目录) |

结果:179 轮我给 \`mutation_check.py\` 加了段 AST 检查,**只跑了 pytest**
就提交 —— 而 pytest 根本不碰那个脚本,于是推上主干的是

    SyntaxError: unterminated string literal (detected at line 179)

## 判据落在 \`compile()\` 上

\`compile()\` 走的是**真** Python 解析器 —— 语法错就抛 \`SyntaxError\`。
不依赖 ruff、不依赖 import、不产生任何副作用。

⚠️ **不要用 \`ast.parse\` 判据 + import 断言的组合**:真正的门是
「这文件能不能被 Python 解析」,别的都不如它直接。

> 📌 与「分层测试全绿 ≠ 拼起来能工作」同源,只是这次**被测对象是脚本**:
> **测试全绿对脚本类改动什么也证明不了。**
"""
from __future__ import annotations

import shlex
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[1]
_CI = _REPO / ".github" / "workflows" / "ci.yml"

# 「门覆盖到的」顶层目录 —— 每一项都要**有理由**在列表里。
# ⚠️ 这是**白名单**而不是黑名单:新增顶层目录时**默认漏网**,
# 那正是要防的情况。
# ⚠️ 必须与 ci.yml 的 `ruff check` 参数**保持一致** ——
# 本条护栏就是那道命令的「覆盖面」自省版。

# 第三方 / 构建产物 / 缓存 —— 不是「漏网」,是**本来就不该扫**。
_SKIP_PARTS = frozenset({
    ".venv", "venv", "node_modules", ".git", "__pycache__",
    ".pytest_cache", ".mypy_cache", "dist", "build", ".ruff_cache",
})

def _ruff_paths_from_ci() -> set[str]:
    """**直接从 ci.yml 解析** `ruff check` 的路径参数。

    📌 **唯一来源是 CI 配置本身**。181 轮我第一版在测试里维护了
    一份白名单副本 —— 于是「两份清单要保持一致」变成一句**没有强制力**
    的叮嘱:改 ci.yml 的人不会看到它。

    副本必然漂移 —— 这与「一致的口径要有唯一来源」是同一条。
    """
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    found: set[str] = set()
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            for line in ((step or {}).get("run") or "").splitlines():
                parts = shlex.split(line.strip())
                if len(parts) >= 3 and parts[0] == "ruff" and parts[1] == "check":
                    found.update(parts[2:])
    return found


_SCRIPTS = sorted((_REPO / "scripts").rglob("*.py"))


def _uncovered() -> list[str]:
    """所有**不在任何一道门**覆盖范围内的 .py。"""
    out: list[str] = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO)
        if _SKIP_PARTS & set(rel.parts):
            continue
        gated = _ruff_paths_from_ci()
        posix = rel.as_posix()
        if posix in gated or any(
            posix == g or posix.startswith(g.rstrip("/") + "/") for g in gated
        ):
            continue
        out.append(rel.as_posix())
    return out


def test_no_python_file_falls_outside_every_gate() -> None:
    """仓库里**每一个** .py 都必须落在某道门的覆盖范围内。

    ⚠️ 判据方向是**白名单**:新加一个顶层目录/文件时,它**默认漏网**
    —— 而「没人在门里」正是本条要防的情况。

    实测抓到过两个真漏网:仓根的 `server.py`(生产 ASGI 入口)和一个
    `new-apple-ui/.local/` 下的验证脚本。
    """
    leaks = _uncovered()
    assert not leaks, (
        "这些 .py **不在任何一道门**的覆盖范围内(ruff / pytest 都不碰):\n  "
        + "\n  ".join(leaks)
        + "\n→ 要么把它们加进 `_GATED_DIRS`/`_GATED_FILES`,"
          "要么加进 `_SKIP_PARTS`(并写明为什么它本来就不该扫)。"
    )


def test_scripts_dir_is_not_empty() -> None:
    """先确认这条护栏**扫到了东西** —— 否则它会永远绿。"""
    assert _SCRIPTS, (
        "scripts/ 下没找到任何 .py —— 要么目录被移走了,要么 glob 写错了。"
        "**空集合的断言恒真**,比没有护栏更糟。"
    )


def test_every_script_compiles() -> None:
    """每个脚本都必须能被 Python 解析。"""
    broken: list[str] = []
    for path in _SCRIPTS:
        src = path.read_text(encoding="utf-8")
        try:
            compile(src, str(path), "exec")
        except SyntaxError as exc:
            broken.append(f"{path.relative_to(path.parents[1])}:{exc.lineno} {exc.msg}")
    assert not broken, "这些脚本**有语法错误**:\n  " + "\n  ".join(broken)


# ----------------------------------------------------------------------
# YAML 必须能被解析(183 轮)
# ----------------------------------------------------------------------

_YAML_GLOBS = ("*.yml", "*.yaml")
_YAML_SKIP_PARTS = _SKIP_PARTS | {"node_modules"}


def _yaml_files() -> list[Path]:
    out: list[Path] = []
    for pat in _YAML_GLOBS:
        out.extend(_REPO.rglob(pat))
    return sorted(
        p for p in out
        if not (_YAML_SKIP_PARTS & set(p.relative_to(_REPO).parts))
    )


def test_yaml_files_are_not_empty() -> None:
    """先确认这条护栏**扫到了东西** —— 空集合的断言恒真。"""
    assert _yaml_files(), "没找到任何 .yml/.yaml —— glob 写错了。"


def test_every_yaml_file_parses() -> None:
    """每个 YAML 都必须能被 `yaml.safe_load` 解析。

    🔴 这条是 182 轮那次事故的直接产物:我用 python 字符串替换改
    `ci.yml`,注释里的换行**没转义** -> 整个文件**解析不了**,
    **CI workflow 直接失效**,而我当时**只跑了 ruff**(它读命令行参数,
    **不读 YAML**)所以完全没发现。

    > 📌 **「我改了配置」要验配置能被解析**,不是验那行命令跑不跑通。
    > ruff 绿 != workflow 是合法 YAML。

    GitHub Actions **不做本地 YAML 校验** —— 写坏了要等**手动触发**
    才发现。而本仓 `ci.yml` 是 `workflow_dispatch`,平时根本不跑。

    覆盖 16 个文件:5 个 workflow + 6 个 issue 模板 + issue-lint 配置
    + 2 个 docker-compose + pnpm workspace/lockfile。
    """
    broken: list[str] = []
    for path in _yaml_files():
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # yaml.YAMLError 及其子类
            broken.append(f"{path.relative_to(_REPO)}: {exc}")
    detail = "\n  ".join(broken)
    assert not broken, f"这些 YAML **解析不了**:\n  {detail}"


# ----------------------------------------------------------------------
# CLAUDE.md 的 Markdown 表格不能有**空首列**(184 轮)
# ----------------------------------------------------------------------

def test_claude_md_tables_have_no_empty_first_column() -> None:
    """CLAUDE.md 是**本仓的 AI 阅读指南** —— 它坏了,下一个助手就被误导。

    🔴 184 轮抓到**四处真实损坏**,全是 bash 把反引号里的标识符吃掉留下的:

        | | preload | 首屏 |          <- 「变体」没了
        | | 首屏 JS |                  <- 「阶段」没了
        实测到的形状: 是全库唯一的真子表  <- read_tx_split_projection 没了

    这些痕迹**已经躺在文件里好几轮了**,因为 CLAUDE.md **没有任何门**。

    ⚠️ 判据只查「表格首列为空」这一种**已实际发生**的损坏形态 ——
    不试图猜「这里本来该写什么」,那需要人来判断。

    同族:代码块 ``` 必须**配对**(奇数说明有未闭合的围栏)。
    """
    text = (_REPO / "CLAUDE.md").read_text(encoding="utf-8")
    bad = [
        f"L{i}: {line[:60]}"
        for i, line in enumerate(text.splitlines(), 1)
        if line.startswith("|") and line.split("|")[1].strip() == ""
    ]
    detail = "\n  ".join(bad)
    assert not bad, f"CLAUDE.md 有表格首列为空(bash 吃掉过标识符):\n  {detail}"

    assert text.count("\n```") % 2 == 0, "``` 代码块围栏**没有配对**"


# ----------------------------------------------------------------------
# CLAUDE.md 引用的 .py 必须**真的存在**(185 轮)
# ----------------------------------------------------------------------

def test_claude_md_python_refs_are_not_dead_links() -> None:
    """CLAUDE.md 里点名的每个 `.py` 都必须**在磁盘上**(或按 basename 找得到)。

    🔴 185 轮实测抓到**一条真死链**:181 轮我删掉了探针残留
    `bc_fixture_src.py`,但 CLAUDE.md「3 个漏网」那张表里**还在写它的名字**
    —— 磁盘上早已没有,读者去找会找不到。

    > 📌 **文档引用一个不存在的文件,正是它最不该做的事** ——
    > 而这个文件的**全部存在意义**就是告诉读者「去哪儿改」。

    ⚠️ 判据用 **basename 兜底**,因为 CLAUDE.md 大量使用**相对路径简写**
    (`projection.py` 实际在 `src/`、`test_mcp_tools.py` 在 `tests/`)。
    第一版我直接判 `Path(p).exists()`,得到 **20 条「不存在」** ——
    其中 19 条是简写,只有 1 条是真的。**别把路径没写全当成死链。**
    """
    import re

    skip = {".venv", "venv", "node_modules", ".git", "__pycache__",
            ".pytest_cache", ".mypy_cache", "dist", "build", ".ruff_cache"}
    by_name = {
        p.name for ext in ("py", "ts", "tsx")
        for p in _REPO.rglob(f"*.{ext}")
        if not (skip & set(p.relative_to(_REPO).parts))
    }
    # ⚠️ **包含前端**:CLAUDE.md 那几大段(bundle 优化 / i18n / hookPlacement)
    # 全在点名 `.ts` / `.tsx`。只查 `.py` 的话,前端改名/搬走**完全没人管**。
    # 实测 186 轮:26 条前端引用按 basename **全部解析成功**(0 死链)。
    text = (_REPO / "CLAUDE.md").read_text(encoding="utf-8")
    refs = sorted(set(re.findall(
        r"`([A-Za-z0-9_./-]+\.(?:py|ts|tsx))`", text
    )))
    dead = [
        r for r in refs
        if not (_REPO / r).exists() and r.rsplit("/", 1)[-1] not in by_name
    ]
    detail = "\n  ".join(dead)
    assert not dead, f"CLAUDE.md 引用了**磁盘上不存在**的源码文件:\n  {detail}"


# ----------------------------------------------------------------------
# markdown 链接不能是死链(188 轮)
# ----------------------------------------------------------------------

_MD_SKIP_PARTS = _SKIP_PARTS


def test_markdown_links_are_not_dead() -> None:
    """CLAUDE.md 与 `docs/` 里的**本地 markdown 链接**必须指向真实文件。

    185/186 轮的护栏只认**反引号**里的路径 —— 但 CLAUDE.md 通篇用的是
    `[x](./docs/y.md)` 这种 markdown 链接语法,**一条都没被查过**。
    两套语法的引用会指向不同的东西,只查一套等于留一半在外面。

    实测(188 轮):CLAUDE.md 4 条链接 + docs/ 2 条,**全部有效**(0 死链),
    所以这条护栏是**当场绿**的 —— 它的价值在**以后**:
    `docs/SELFHOST-RUNBOOK.md` 改名或删除时立刻红。

    ⚠️ 跳过 `http(s)://` 与 `mailto:`(外链查不了),
    也跳过 `#anchor`(同文档内跳转)。
    ⚠️ 187 轮实测:`../BeeCount/CLAUDE.md` 指向**mobile 仓**(本仓之外),
    **别把它当成死链** —— 那是**有意的跨仓引用**。
    """
    import re

    roots = [(_REPO / "CLAUDE.md")] + sorted(
        p for p in (_REPO / "docs").rglob("*.md")
        if not (_MD_SKIP_PARTS & set(p.relative_to(_REPO).parts))
    )
    assert roots, "没找到任何 markdown —— glob 写错了。"

    bad: list[str] = []
    for path in roots:
        for link in re.findall(r"\]\(([^)#][^)]*)\)", path.read_text(encoding="utf-8")):
            if link.startswith(("http://", "https://", "mailto:", "../")):
                continue
            if not (path.parent / link).exists():
                bad.append(f"{path.relative_to(_REPO)} -> {link}")
    detail = "\n  ".join(bad)
    assert not bad, f"markdown 链接指向**不存在**的目标:\n  {detail}"


# ----------------------------------------------------------------------
# CI 里 Pytest 必须**真的能跑到**(189 轮)
# ----------------------------------------------------------------------

def test_pytest_step_is_not_blocked_by_advisory_failures() -> None:
    """任何**排在 Pytest 之前**的 step 失败,都不能让 Pytest 跑不到。

    🔴 189 轮实测出来的问题:Actions 在 step 失败时**默认中止后续 step**,
    而 `ruff check` 带 1257 条存量违规(`exit 1`)、`mypy` 带 60 条
    (`exit 1`) —— 于是 **`pytest` 在 CI 上一次都没执行过**。

    CLAUDE.md 那张表里唯一那行「pytest 绿 / 1361 passed」,
    在 CI 上是**没执行**,不是绿。

    修法是把 Ruff / Mypy 标成 `continue-on-error: true`(advisory):
    检查强度**一点没减**(仍跑、仍报告),但真正的必过门终于能跑到。

    ⚠️ 这条护栏不是「仪式」:它锁住的是「pytest 到底会不会被跑」。
    """
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    checked = 0
    # ⚠️ 判据**故意只管 Ruff / Mypy 两条**,不是「Pytest 前的所有 run」。
    # 第一版我写成「所有排在 Pytest 前、没标 advisory 的 run step」——
    # 它把 `Install deps` / `Migrate` 也算成 blocker,**而那是对的**:
    # 依赖没装上 / 迁移没跑,pytest 本来也跑不了。
    #
    # 要防的是**明知会以 exit 1 失败的存量检查**,不是「一切可能失败的东西」。
    # 判据太宽 = 逼着人给真正的必过步骤也加 `continue-on-error`,
    # 那才是把门真的拆了。
    known_stock = {"Ruff", "Mypy"}
    for name, job in (doc.get("jobs") or {}).items():
        steps = (job or {}).get("steps") or []
        names = [((s or {}).get("name") or (s or {}).get("uses") or "") for s in steps]
        if "Pytest" not in names:
            continue
        checked += 1
        for step in steps[: names.index("Pytest")]:
            label = step.get("name") or ""
            if label in known_stock:
                assert step.get("continue-on-error"), (
                    f"job {name}: `{label}` 排在 Pytest 前且带存量违规(exit 1),"
                    f"没标 continue-on-error 的话 **pytest 永远跑不到**"
                )
    assert checked, "没找到任何带 Pytest 的 job —— ci.yml 结构变了?"


def test_ci_install_does_not_bypass_the_lockfile() -> None:
    """CI 装依赖必须用 `--frozen-lockfile`,不能用 `--no-frozen-lockfile`。

    📌 190 轮实测:`--no-frozen-lockfile` **恰好绕过 lockfile 一致性检查** ——
    依赖漂移时 CI 会**静默装上新版本**而不是报错。那不是「更宽松」,
    是让 CI 在**跑一个别人没测过的依赖组合**。

    同理也不能干脆去掉 flag(默认行为取决于 pnpm 版本与 CI 环境)。

    护栏锁住的是「CI 装的必须是 lockfile 里那个版本」。
    """
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    installs = [
        (s or {}).get("run") or ""
        for job in (doc.get("jobs") or {}).values()
        for s in ((job or {}).get("steps") or [])
        if "pnpm install" in ((s or {}).get("run") or "")
    ]
    assert installs, "ci.yml 里没有 pnpm install —— 结构变了?"
    bad = [c for c in installs if "--no-frozen-lockfile" in c or "--frozen-lockfile" not in c]
    detail = "\n  ".join(bad)
    assert not bad, f"CI 装依赖没锁 lockfile:\n  {detail}"
