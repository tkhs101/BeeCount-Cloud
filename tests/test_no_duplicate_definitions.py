"""测试文件里**不许有同名顶层定义** —— 后者会静默覆盖前者。

## 为什么这条值得存在

我刚在 `test_smoke_real_app.py` 里踩过:重构 engine 时在文件上方插入了
新的 `client` fixture,**旧的还留在下面**。

Python 取**后定义者**,所以:

- pytest 正常收集、测试正常跑、计数正常
- 但我前面三轮改的都是**从未生效的那份**
- 报错指向三个完全无关的方向(`AUTH_EMAIL_EXISTS` /
  `no such table: users` / `can only be called once`)

**没有任何工具会报这个** —— `pytest --collect-only` 只会看到一份,
因为它看到的也是后定义的那份。

`systematic-debugging` 的「三次失败就停下来质疑架构」是最后把我拽出来的;
在那之前我一直在猜环境变量、猜 DB 残留、猜 TestClient 数量,
**从来没想过「我改的东西是不是根本没跑」**。

## 判据:AST 真扫,不是字符串

用 `ast.parse` 取**模块顶层**的 `FunctionDef`/`AsyncFunctionDef`
(含带 `@pytest.fixture` 装饰器的),按名字计数。字符串包含判断会被注释、
docstring、字符串字面量里的同名骗到。
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def test_no_duplicate_top_level_definitions() -> None:
    dups: list[str] = []
    for f in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        names = [
            n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for name, count in Counter(names).items():
            if count > 1:
                dups.append(f"{f.name}: {name} x{count}")

    assert not dups, (
        "同名顶层定义,后者覆盖前者 —— pytest 只会看到后定义的那份,"
        "我上一轮因此连错三轮:\n  " + "\n  ".join(dups)
    )


def test_the_scanner_actually_detects_duplicates() -> None:
    """判据自检:造一个真有重复的文件,确认扫得出来。

    没有这条,scanner 逻辑写错了也会全绿 —— 「扫不出东西」和
    「没有东西」在断言上长得一模一样。
    """
    src = ("def a():\n    pass\n\n\n"
           "def a():\n    pass\n\n\n"
           "async def b():\n    pass\n\n\n"
           "def b():\n    pass\n")
    tree = ast.parse(src)
    names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    dups = {n: c for n, c in Counter(names).items() if c > 1}
    assert dups == {"a": 2, "b": 2}, f"自检失败,scanner 逻辑有问题:{dups}"


def test_the_scanner_ignores_non_top_level() -> None:
    """嵌套的同名 `def` 不算重复 —— 只有模块顶层的会互相覆盖。"""
    src = ("def outer():\n"
           "    def helper():\n        pass\n"
           "    return helper\n")
    tree = ast.parse(src)
    names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert Counter(names).get("helper") is None, names
