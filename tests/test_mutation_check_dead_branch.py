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
import pathlib
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
_DEAD_FIND_TUPLE = (_DEAD_FIND, _DEAD_REPL)

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


def test_run_leaves_the_target_file_byte_identical() -> None:
    """跑一次工具之后,目标文件必须**逐字节**未变(含行尾)。

    🔴 210 轮实测:`read_text` + `write_text` 在 Windows 上**读一次写一次
    就把 LF 文件变成 CRLF** —— `read_text` 做 universal newlines
    (CRLF -> LF),`write_text` 又把 LF 转回 `os.linesep`(CRLF)。

    它**三重隐蔽**,所以只能靠字节级断言发现:

    | # | 它骗过了什么 |
    |---|---|
    | 1 | 工具 **exit 0**,输出还打印「✓ 已还原」 |
    | 2 | **`git diff` 为空** —— `.gitattributes` 的 `eol=lf` 让 git 规范化比较 |
    | 3 | **内容逐字节相同**,只有行尾变了,没有任何 diff 可看 |

    ⚠️ 而后果是真的:全仓 LF 是 209 轮刚统一的,每次跑这个测试就把
    `src/routers/read/workspace.py` 打回 CRLF,而且**只有行尾检查能看见**。

    > 📌 这也是为什么**判据要比现象更细**:「内容没变」不等于「文件没变」。
    > 只断言 `read_text() == original` 的话,**这个 bug 会被漏掉** ——
    > 因为它比较的正是那两个被规范化过的字符串。

    ⚠️ **第二版的第一版判据也有盲区**(210 轮自己踩到):先断言
    `before == after`(before 是本测试自己读的)。但**污染发生在更早的
    测试里** —— 轮到本测试时文件**已经是 CRLF**,于是 before/after 都脏、
    两者相等 -> **变异后仍全绿**。
    > 📌 **判据不能依赖「跑之前的状态是干净的」** —— 那正是被测对象
    > 可能已经破坏掉的东西。所以改成**与 `git show HEAD:<file>` 的 blob 比**:
    > 权威、不依赖执行顺序、顺带能抓「内容被改」。
    """
    target = pathlib.Path(_TARGET)
    pristine = subprocess.run(
        ["git", "show", f"HEAD:{_TARGET}"], capture_output=True, check=True
    ).stdout
    _run(*_DEAD_FIND_TUPLE)
    after = target.read_bytes()
    assert after == pristine, (
        "工具跑完之后目标文件**不再与 git 里存的一模一样**。\n"
        f"  git 里 : CRLF={pristine.count(bytes([13, 10]))}\n"
        f"  跑之后: CRLF={after.count(bytes([13, 10]))}\n"
        "  内容相同但行尾不同 = 210 轮那个 read_text/write_text 转换。"
        "\n  ⚠️ `git diff` 看不见它(.gitattributes 的 eol=lf 会规范化比较),"
        "\n     所以只能靠这里的字节级断言。"
    )
