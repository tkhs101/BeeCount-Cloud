"""护栏:`scripts/*.py` 必须**能编译**。

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

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# 「门覆盖到的」顶层目录 —— 每一项都要**有理由**在列表里。
# ⚠️ 这是**白名单**而不是黑名单:新增顶层目录时**默认漏网**,
# 那正是要防的情况。
# ⚠️ 必须与 ci.yml 的 `ruff check` 参数**保持一致** ——
# 本条护栏就是那道命令的「覆盖面」自省版。
_GATED_DIRS = (
    "src", "tests", "alembic", "scripts",
    "new-apple-ui",  # Apple 客户端 + .local 验证脚本
)
# 白名单里的**单文件**例外(每个都要能说清为什么它自己够格)。
_GATED_FILES = (
    "server.py",  # 生产 ASGI 入口;语法坏了服务**直接起不来**
)

# 第三方 / 构建产物 / 缓存 —— 不是「漏网」,是**本来就不该扫**。
_SKIP_PARTS = frozenset({
    ".venv", "venv", "node_modules", ".git", "__pycache__",
    ".pytest_cache", ".mypy_cache", "dist", "build", ".ruff_cache",
})

_SCRIPTS = sorted((_REPO / "scripts").rglob("*.py"))


def _uncovered() -> list[str]:
    """所有**不在任何一道门**覆盖范围内的 .py。"""
    out: list[str] = []
    for path in sorted(_REPO.rglob("*.py")):
        rel = path.relative_to(_REPO)
        if _SKIP_PARTS & set(rel.parts):
            continue
        if rel.parts[0] in _GATED_DIRS or rel.as_posix() in _GATED_FILES:
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
