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


def _all_workflows() -> list[tuple[str, dict]]:
    """仓里**每一个** workflow,不只是 ci.yml。

    🔴 193 轮:191/192 轮把 pnpm 版本来源修了 `ci.yml` 就收工 ——
    **从没打开过 `release.yml`**。而 `release.yml` 是 **tag push 自动触发**
    的(不像 `ci.yml` 要手动),它里面有一模一样的 `version: 9` 和
    `|| pnpm install --no-frozen-lockfile` fallback。

    > 📌 **我一直在修一个文件,没问过其它文件有没有同样的问题** ——
    > 这正是 181 轮「门覆盖面要能自省」的精神,只是当时只用它查了 .py。

    ⚠️ 解析不了的 workflow 由 `test_every_yaml_file_parses` 负责报错,
    这里跳过(否则两个异常叠在一起,看不懂哪个是哪个)。
    """
    out: list[tuple[str, dict]] = []
    for path in sorted((_REPO / ".github" / "workflows").glob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(doc, dict) and (doc.get("jobs") or {}):
            out.append((path.name, doc))
    return out


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
    checked = 0
    for wf_name, doc in _all_workflows():
        # 判据**故意只管 Ruff / Mypy 两条**,不是「Pytest 前的所有 run」。
        # 第一版我写成「所有排在 Pytest 前、没标 advisory 的 run step」——
        # 它把 `Install deps` / `Migrate` 也算成 blocker,**而那是对的**:
        # 依赖没装上 / 迁移没跑,pytest 本来也跑不了。
        #
        # 要防的是**明知会以 exit 1 失败的存量检查**,不是「一切可能失败的东西」。
        # 判据太宽 = 逼着人给必过步骤也加 `continue-on-error`,
        # 那才是把门真的拆了。
        known_stock = {'Ruff', 'Mypy'}
        for name, job in (doc.get('jobs') or {}).items():
            steps = (job or {}).get('steps') or []
            names = [((s or {}).get('name') or (s or {}).get('uses') or '')
                     for s in steps]
            if 'Pytest' not in names:
                continue
            checked += 1
            for step in steps[: names.index('Pytest')]:
                label = step.get('name') or ''
                if label in known_stock:
                    assert step.get('continue-on-error'), (
                        f'{wf_name} job {name}: `{label}` 排在 Pytest 前且带存量'
                        f'违规(exit 1),没标 continue-on-error 的话 '
                        f'**pytest 永远跑不到**'
                    )
    assert checked, '没找到任何带 Pytest 的 job —— workflow 结构变了?'


def test_ci_install_does_not_bypass_the_lockfile() -> None:
    """CI 装依赖必须用 `--frozen-lockfile`,不能用 `--no-frozen-lockfile`。

    📌 190 轮实测:`--no-frozen-lockfile` **恰好绕过 lockfile 一致性检查** ——
    依赖漂移时 CI 会**静默装上新版本**而不是报错。那不是「更宽松」,
    是让 CI 在**跑一个别人没测过的依赖组合**。

    同理也不能干脆去掉 flag(默认行为取决于 pnpm 版本与 CI 环境)。

    护栏锁住的是「CI 装的必须是 lockfile 里那个版本」。
    """
    installs = [
        (st or {}).get("run") or ""
        for _wf, doc in _all_workflows()
        for job in (doc.get("jobs") or {}).values()
        for st in ((job or {}).get("steps") or [])
        if "pnpm install" in ((st or {}).get("run") or "")
    ]
    assert installs, "ci.yml 里没有 pnpm install —— 结构变了?"
    bad = [c for c in installs if "--no-frozen-lockfile" in c or "--frozen-lockfile" not in c]
    detail = "\n  ".join(bad)
    assert not bad, f"CI 装依赖没锁 lockfile:\n  {detail}"


def test_ci_pnpm_version_comes_from_package_json() -> None:
    """CI 的 pnpm 版本**唯一来源**必须是 `frontend/package.json`。

    🔴 191 轮实测到版本漂移:`ci.yml` 写死 `version: 9`,而
    `frontend/package.json` 的 `packageManager` 是 `pnpm@10.15.1`
    (本地实测也是 10.15.1,`lockfileVersion: '9.0'`)。

    写死版本就是 `packageManager` 的**副本**,必然漂移 —— 与 182 轮
    「白名单副本 vs ci.yml」是同一个形状。

    两条断言:
    1. `packageManager` 必须存在 —— 否则「不指定 version」让 CI 无处可跟随
    2. `pnpm/action-setup` **不得**带 `version:`(它会去读 packageManager)
    """
    import json

    manifest = _REPO / "frontend" / "package.json"
    pkg = json.loads(manifest.read_text(encoding="utf-8"))
    pm = pkg.get("packageManager") or ""
    assert pm.startswith("pnpm@"), (
        "frontend/package.json 缺 packageManager —— "
        "删掉 CI 里的 pnpm version 后就无从跟随了"
    )

    bad: list[str] = []
    for wf_name, doc in _all_workflows():
      for name, job in (doc.get("jobs") or {}).items():
        for step in ((job or {}).get("steps") or []):
            uses = (step or {}).get("uses") or ""
            if "pnpm/action-setup" not in uses:
                continue
            opts = (step or {}).get("with") or {}
            if opts.get("version"):
                bad.append(f"{wf_name} job {name}: pnpm/action-setup 硬编码了 version")
            # 🔴 192 轮:191 轮我只删了 version 就完事,**那是错的** ——
            # action-setup 读的是 `package_json_file`(默认 `package.json`,
            # **相对仓库根**),而本仓**仓库根没有 package.json**,
            # `packageManager` 在 `frontend/package.json`。
            # 两个来源同时缺失 -> CI 直接报错。
            # action **没有 `working-directory`**,只能在输入里指路径。
            pjf = opts.get("package_json_file") or "package.json"
            pjf_path = _REPO / pjf
            if not pjf_path.exists():
                bad.append(f"{wf_name} job {name}: package_json_file={pjf} 不存在")
            elif not json.loads(pjf_path.read_text(encoding="utf-8")).get("packageManager"):
                bad.append(f"{wf_name} job {name}: {pjf} 里没有 packageManager")
    detail = "\n  ".join(bad)
    assert not bad, (
        "CI 的 pnpm 版本来源有问题(要么写死了,要么跟着一个不存在的 "
        "package.json):\n  " + detail
    )


def test_ci_node_version_comes_from_nvmrc() -> None:
    """CI 的 Node 版本唯一来源必须是 `frontend/.nvmrc`,不能写死在 workflow。

    🔴 194 轮实测到的形状(与 191 轮 pnpm 那个**一模一样**):
    `ci.yml` 写 `node-version: 20`、`release.yml` 写 `node-version: '20'`
    —— **两份副本**,而仓库里**没有任何 Node 版本声明**,
    本地实测是 Node **26.8.1**。

    没有声明 = 没有唯一来源 = 迟早漂移。pnpm 那条 193 轮已经证明了。

    为什么用 `.nvmrc` 而不是 `node-version-file: package.json`:
    setup-node 文档列了 `package.json` 这个文件名,但**没写它读哪个字段**
    (volta? engines?)。**不凭记忆选一个含义不明的输入** ——
    `.nvmrc` 的内容就是一行版本号,没有歧义。

    选 20 的依据:lockfile 实测依赖里最高的 node 要求是
    `react-router-dom` 的 `>=20.0.0`,而 20 是 CI 一直在用、
    **已验证能跑**的值(本地更高,同样满足 `>=`)。
    """
    nvmrc = _REPO / "frontend" / ".nvmrc"
    assert nvmrc.exists(), "frontend/.nvmrc 不存在 —— CI 的 node 版本会没来源"
    assert nvmrc.read_text(encoding="utf-8").strip(), ".nvmrc 是空的"

    bad: list[str] = []
    for wf_name, doc in _all_workflows():
        for name, job in (doc.get("jobs") or {}).items():
            for step in ((job or {}).get("steps") or []):
                if "actions/setup-node" not in ((step or {}).get("uses") or ""):
                    continue
                opts = (step or {}).get("with") or {}
                if opts.get("node-version"):
                    bad.append(f"{wf_name} job {name}: 写死了 node-version")
                nvf = opts.get("node-version-file") or ""
                if nvf and not (_REPO / nvf).exists():
                    bad.append(f"{wf_name} job {name}: node-version-file={nvf} 不存在")
    detail = "\n  ".join(bad)
    assert not bad, f"CI 的 node 版本必须来自 frontend/.nvmrc:\n  {detail}"


def test_dockerfile_has_no_bypass_and_no_hardcoded_node() -> None:
    """Dockerfile 同样不许绕过 lockfile,Node 版本也不许写死。

    🔴 195 轮实测:Dockerfile 里同时有两处「副本 / 绕过」——
    - `FROM node:20-alpine` —— Node 版本的**副本**。194 轮建了
      `frontend/.nvmrc` 当唯一来源,但**base image 的 tag 不是
      `setup-node` 能管的地方**,得靠 build arg 传进去。
    - `RUN pnpm install --frozen-lockfile || pnpm install --no-frozen-lockfile`
      —— 193 轮在 release.yml 里删掉的同一个 fallback,**这里还有一个**。
      而这个文件**每次 tag push 都会跑**,fallback 影响的是
      **真正发布出去的镜像**。

    > 📌 **我修了 5 轮 workflow,却从没打开过 Dockerfile** ——
    > 同一个错误能在没被检查的地方一直躺着。
    """
    dockerfile = (_REPO / "Dockerfile").read_text(encoding="utf-8")

    # ⚠️ 判据**只看真正的 RUN 行**。第一版直接对整个文件做字符串包含,
    # 结果**被我自己写的注释触发** —— 注释里为了说明问题写了那个命令,
    # 护栏当场变红。
    #
    # 这与「变异让测试报错 != 护栏抓到了变异」同族:**护栏被非代码内容
    # 触发**。文档里写示例是很正常的,不该让护栏把它当违规。
    run_lines = [
        ln for ln in dockerfile.splitlines()
        if ln.strip().startswith("RUN ")
    ]
    bad_runs = [ln for ln in run_lines if "--no-frozen-lockfile" in ln]
    detail = "\n  ".join(bad_runs)
    assert not bad_runs, (
        "Dockerfile 的 RUN 行里有 `--no-frozen-lockfile` fallback —— "
        "它会**静默装上新版本**并把没测过的依赖组合打进发布的镜像:\n  "
        + detail
    )

    # `FROM node:<version>-alpine` 里 <version> 不能是字面量数字。
    bad = [
        ln for ln in dockerfile.splitlines()
        if ln.startswith("FROM node:") and "-alpine" in ln
        and not ln.split("FROM node:")[1].split("-alpine")[0].startswith("${")
    ]
    detail = "\n  ".join(bad)
    assert not bad, (
        "Dockerfile 的 base image 写死了 Node 版本(副本):\n  " + detail
        + "\n  唯一来源应是 frontend/.nvmrc,靠 `--build-arg NODE_VERSION=` 传入"
    )


def test_workflow_build_args_use_step_outputs_not_shell() -> None:
    """workflow 的 `with:` 值里**不得**出现 shell 命令替换 `$(`。

    🔴 196 轮抓到 195 轮自己写错的一行:`build-args:` 里写了
    `NODE_VERSION=$(cat frontend/.nvmrc)`。`with:` 的值是**字面量**,
    只有 `run:` 才走 shell —— 那样会把 `$(cat ...)` 原样当 build-arg 传进去。
    而 release 是 **tag push 自动触发**的。

    📌 **判据刻意不写成「with: 不展开 shell」这条断言** —— 我查了两次
    GitHub 文档都没抓到正文(页面 JS 渲染),**没有证据就不写成判据**,
    否则那正是 CLAUDE.md 记的「45% 假护栏」那一族。

    判据落在**写法**上:`$(` 只允许出现在 `run:` 里,并且要用
    `$GITHUB_OUTPUT` + `${{ }}` 传值。这个写法**无论语义如何都正确**。
    """
    bad: list[str] = []
    for wf_name, doc in _all_workflows():
        for name, job in (doc.get("jobs") or {}).items():
            for step in ((job or {}).get("steps") or []):
                opts = (step or {}).get("with") or {}
                for key, val in opts.items():
                    if "$(" in str(val):
                        bad.append(
                            f"{wf_name} job {name}: with.{key} 里有 shell "
                            f"命令替换 `$(` —— with 的值是字面量,不展开 shell"
                        )
    detail = "\n  ".join(bad)
    assert not bad, f"workflow 的 with: 里不得用 shell 命令替换:\n  {detail}"


def test_workflow_step_output_references_are_not_dangling() -> None:
    """`${{ steps.X.outputs.Y }}` 里的 `X` 必须是**真实存在**的 step id。

    🔴 196 轮的错误是「引用了拿不到的值」那一族。GitHub Actions 对
    不存在的 step 求值成**空字符串**,**不报错** —— 所以拼错的 id 只会
    让 `NODE_VERSION=` 静默变成空值,然后 Docker 拿默认版本。

    ⚠️ 196 轮我修的是 `$(...)`(shell 展开)那类;但「引用了不存在的 step」
    是**同一个后果的另一个入口**,而且它**完全可本地判定** —— 不需要 CI。

    本仓实测(197 轮):只有 `release.yml` 用了 step 引用,3 个 id
    (`meta` / `nodever` / `vars`)**全部有定义**,零悬空。
    其它 6 个 job 一个 step 引用都没有。
    """
    import re

    bad: list[str] = []
    checked = 0
    for wf_name, doc in _all_workflows():
        for jn, job in (doc.get("jobs") or {}).items():
            steps = (job or {}).get("steps") or []
            ids = {(s or {}).get("id") for s in steps if (s or {}).get("id")}
            refs: set[str] = set()
            for s in steps:
                blobs = [(s or {}).get(k) for k in ("with", "run", "env")]
                for blob in blobs:
                    if isinstance(blob, dict):
                        blob = " ".join(str(v) for v in blob.values())
                    refs |= set(re.findall(
                        r"steps\.([A-Za-z0-9_-]+)\.outputs", str(blob or "")
                    ))
            if not refs:
                continue
            checked += 1
            for r in sorted(refs - ids):
                bad.append(f"{wf_name} job {jn}: 引用了不存在的 step id '{r}'")
    detail = "\n  ".join(bad)
    assert not bad, (
        f"workflow 引用了不存在的 step id"
        f"(Actions 会**静默求值成空字符串**,不报错):\n  " + detail
    )
    assert checked, "没扫到任何 step 引用 —— 正则或结构变了?"


def test_markdown_files_have_no_raw_control_bytes() -> None:
    """仓里任何 `.md` 都不许含**裸**控制字节。

    🔴 199 轮:`docs/aegis/verification-log.md` 第 2086 行有三个真实控制字节
    (`0x00` / `0x08` / `0x0b`) —— 某个轮次用 bash heredoc 写日志时,
    把 `` `/[\x00-\x08...]/` `` 这样的正则字面量**真的解释成了字节**。

    **后果不是「显示乱码」而已**:grep 判该文件为 **binary** ->
    **搜索与审阅会整个跳过它** —— 而 CLAUDE.md 恰恰让读者去那里查证。
    `file` 仍报 "UTF-8 text"(它按 BOM/启发式判断),**只有 grep 的
    binary 判定会暴露**。

    ⚠️ 这正是本文件顶部那条纪律的第一个实例:「含反斜杠的代码一律用
    `write`/`edit` 工具写,不用 bash heredoc」—— 199 轮自己又犯了,
    所以要有护栏,而不是只记一条纪律。

    判据:除 `\t` / `\n` / `\r` 外,0x00-0x1F 一律不许出现。
    """
    offenders: list[str] = []
    for md in sorted(_REPO.rglob("*.md")):
        parts = md.relative_to(_REPO).parts
        if "node_modules" in parts or ".git" in parts or "new-apple-ui" in parts:
            continue
        raw = md.read_bytes()
        bad = sorted({
            c for c in range(0x20)
            if c not in (0x09, 0x0A, 0x0D) and bytes([c]) in raw
        })
        if bad:
            offenders.append(
                f"{md.relative_to(_REPO).as_posix()}: "
                f"{[hex(c) for c in bad]}"
            )
    detail = "\n  ".join(offenders)
    assert not offenders, (
        f"markdown 里有裸控制字节(grep 会判 binary,搜索时整个跳过):\n  "
        + detail
    )


# 单轮上下文预算。这个数不是拍脑袋 —— CLAUDE.md 自己写着
# 「单轮上下文预算只有 65 kB —— 每轮开头都是 truncated CLAUDE.md from
# 86550 to 65243 bytes」,实测截断阈值就是 65243。
_CONTEXT_BUDGET = 65244


def test_claude_md_fits_the_context_budget() -> None:
    """CLAUDE.md 必须放得进单轮上下文预算,否则它作为「阅读指南」失效。

    🔴 202 轮实测的后果:CLAUDE.md 一度 **93347 B**,而预算 65244 ——
    **每轮静默丢掉约 28 KB**。丢掉的正是这个文件存在的意义:
    「三个会烧钱的配置陷阱」「时区:已栽五次」「全仓库只能有**一个**
    `TestClient(app)`」「两个 scope 事实(踩过才知道)」。

    **没有任何报错** —— 表现只是每轮开头那句 `truncated ...`,
    以及「我明明记得 CLAUDE.md 里有这条」的错觉。
    我因此有**二十几轮是在只读到 2/3 的前提下做判断的**。

    修法是拆(`docs/CI-AND-BUILD.md` / `docs/FRONTEND-NOTES.md`),
    这条护栏负责让它**不再复发** —— 少了余量时下一轮就会红,
    而不用等几十轮后从「怎么文档又少了」倒推。

    > 📌 判据方向是**硬上限**,不是风格偏好:超了就会**丢内容**,
    > 那不是「写得不够好」,是**功能坏了**。
    """
    size = (_REPO / "CLAUDE.md").stat().st_size
    slack = _CONTEXT_BUDGET - size
    assert size <= _CONTEXT_BUDGET, (
        f"CLAUDE.md 有 {size} 字节,超过单轮上下文预算 {_CONTEXT_BUDGET}"
        f"(超出 {size - _CONTEXT_BUDGET})。"
        f"\n  后果:每轮开头静默丢掉这部分,**没有任何报错**。"
        f"\n  修法:细节移到 docs/(见文首那张表),别在 CLAUDE.md 里堆。"
    )
    # 留一点余量提示,但**不因此变红** —— 余量小只是提醒,不是违规。
    assert slack >= 0
