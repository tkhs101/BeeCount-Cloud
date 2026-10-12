"""护栏:`mutation_check.py` 的「死分支提示」必须真的被执行到。

## 为什么要有这条(不是仪式)

CLAUDE.md 记着一条:「**代码在文件里 ≠ 代码被执行到了**」。178 轮查那颗
雷时,四轮(174-177)都栽在**死变异**上 —— 锚点落在测试进不去的分支里,
变异**永远绿**,与「护栏失效」在观测上完全一样。

那个 AST 检查是**专门用来防这件事的**。⚠️ **如果它自己变成死代码**,
后面每一次变异验证都会给**假绿**,而且**没有任何信号**。

⚠️ 本条护栏第一版**自己就是坏的**,而且坏法很典型:用 `tmp_path` 造
fixture 文件,但工具先跑 `git diff` 确认变异落地 —— **临时文件不在 git 里**,
于是 exit=2,**永远走不到 AST 检查**。第一版两条「该报」用例**全红**
才暴露出来。

> 📌 **给工具写护栏,要用工具自己认的输入** —— 别想当然造输入。
> 这与「断言要落在产物上」同源:先确认**你的输入真的走到了那段代码**。

## 判据落在 CLI 的**真实输出**上

不是「源码里有这段 ast.walk」那种字符串断言 —— 那种断言在检查被注释掉
之后照样绿(CLAUDE.md:「源码级断言自己也会恒真」)。

⚠️ 必须用**仓库内真实文件**:工具靠 `git diff` 判「变异有没有落地」。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TOOL = _REPO / "scripts" / "mutation_check.py"
_TARGET = "src/routers/read/workspace.py"
# 「死分支」三字的 **UTF-8 字节** —— 不经过任何编解码,跨环境稳定。
_WARN = "死分支".encode("utf-8")

# 刻意用**仓库里真实存在**的锚点 —— 这正是 178 轮手工验过的两组位置。
_DEAD_FIND = (
    "            tx_query = tx_query.where("
    "ReadTxProjection.happened_at >= start_at)"
)
_DEAD_REPL = _DEAD_FIND + "  # v"

# 模块**顶层**赋值 —— 一定无条件执行,不该有提示(反例要先量,见测试注释)。
_TOP = (
    "_CSV_HEADERS_BY_LANG: dict[str, list[str]] = {",
    "_CSV_HEADERS_BY_LANG: dict[str, list[str]] = {  # v",
)



def _run(find: str, repl: str) -> str:
    proc = subprocess.run(
        [
            sys.executable, str(_TOOL),
            "--file", _TARGET,
            "--find", find, "--replace", repl,
            "--test", "tests/test_mcp_tools.py",
        ],
        capture_output=True,
        cwd=_REPO, timeout=600,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
    )
    # ⚠️ **按字节判**,不要先解码 —— 控制台是 GBK,解码会随环境变。
    return proc.stdout + proc.stderr


def test_warns_when_the_anchor_is_inside_a_block() -> None:
    out = _run(_DEAD_FIND, _DEAD_REPL)
    assert _WARN in out, (
        "锚点位于块体内部却**没有**提示 —— AST 检查可能已成死代码。\n"
        f"输出:\n{out[-800:]}"
    )


def test_quiet_for_a_module_level_anchor() -> None:
    """模块顶层的锚点**一定**无条件执行 —— 不该有任何提示。

    ⚠️ 第二版我**换错了锚点**:原本用 `workspace_analytics` 里那个 `for`
    循环头当「无条件位置」,但它其实落在外层 `If`(第 1086 行)**块体内部**
    —— 工具**提示得对**,是我的断言错了。

    > 📌 **拿一个没验证过的位置当反例**,等于把工具的**已知局限**
    > 写成它的**缺陷**。反例也要**先量**。
    """
    out = _run(*_TOP)
    assert _WARN not in out, (
        "模块顶层的锚点被误报 —— 会教人忽略这条提示。\n"
        f"输出:\n{out[-800:]}"
    )
