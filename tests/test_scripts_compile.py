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

_SCRIPTS = sorted((Path(__file__).resolve().parents[1] / "scripts").rglob("*.py"))


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
