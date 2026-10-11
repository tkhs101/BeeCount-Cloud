"""`_shared.__all__` 是**第三张手工登记表**,这条让它默认红。

## 为什么需要

CLAUDE.md 已经记了三张手工登记表(`_projection_row_to_tx_dict` /
`_truncate_ledger` / `_LEDGER_MERGE_SPECS`),每张都有「白名单式」的护栏 ——
**加新东西时默认红**,要求显式论证为什么可以不登记。

`_shared.__all__` 是**第四张**,而且它**没有护栏**。

## 失效形状(本会话真的踩了)

`src/routers/write/accounts.py` 用 `from ._shared import *`。给
`run_autorepay_run` 加一个从 `src/schemas.py` 透出的新类型
(`AutoRepayRunResult`)时,我:

1. 在 `_shared.py` 的 `from ...schemas import (...)` 里加了名字 ✅
2. **忘了加进 `__all__`** ❌

于是 `accounts.py` 里 `response_model=AutoRepayRunResult` 直接
`NameError` —— **57 个测试文件 collection 失败**。

⚠️ 这个失效形状特别恶劣:报错指向**使用者**(`accounts.py`),
而真正漏掉的是 `_shared.__all__`。顺着报错找不到源头。

## 为什么这条是「白名单式」而不是黑名单

`_shared.__all__` 有 **100+ 个**名字,而 schemas 只贡献其中一部分。
黑名单(「这些名字不需要登记」)在**加新名字时默认通过** ——
而那正是要防的情况。

所以判据反过来:**`_shared` 从 `schemas` 导入的每一个公开名字,
必须出现在 `__all__` 里**。实测当前状态:

| | |
|---|---|
| schemas 导入的公开名 | **14** |
| 其中不在 `__all__` 的 | **0** |

**现状是干净的,但「新加一个」会变红** —— 这正是要的形状。

⚠️ 判据用 `ast` 解析**真实的 import 语句与真实的 `__all__`**,不是
字符串查找 —— `WriteCommitMeta` 在 `_shared.py` 里出现了 15 次,其中
大多是文档字符串和函数注解,`expect(src).toContain(...)` 那种判据在这里
会恒真(CLAUDE.md 记的「前端 45% 的护栏是假的」同一族)。

## 自检:判据失效时必须自己红

`test_criterion_detects_a_missing_name` 把 `AutoRepayRunResult` 加进
「应登记」名单来模拟漏登记,确认判据真的会红 —— 否则上面那条可能
因为「什么都没找到」而恒绿。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_SHARED = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "routers" / "write" / "_shared.py")


def _parse() -> tuple[set[str], set[str]]:
    """返回 (从 schemas 导入的公开名, __all__ 里的名字)。"""
    tree = ast.parse(_SHARED.read_text(encoding="utf-8"))
    imported: list[str] = []
    allnames: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "schemas" in node.module:
            imported += [a.name for a in node.names]
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "__all__" for t in node.targets
        ):
            allnames = {
                e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return {n for n in imported if not n.startswith("_")}, allnames


def test_schemas_names_are_exported_via_all() -> None:
    """`_shared` 从 schemas 导入的每个公开名,都必须出现在 `__all__` 里。

    `accounts.py` 靠 `from ._shared import *` 拿到它们 —— 不在 `__all__`
    就是 `NameError`,而且**报错指向使用者**,不指向真正漏掉的这一层。
    """
    imported, allnames = _parse()
    # 下限断言:判据找不到东西时不能静默通过(同 CLAUDE.md 记的 tokenize 那条)
    assert len(imported) >= 10, (
        f"只解析到 {len(imported)} 个 schemas 名 —— 正则/解析失效了,"
        f"这条护栏会恒绿")
    missing = sorted(imported - allnames)
    assert not missing, (
        f"这些从 src/schemas.py 导入的名字不在 _shared.__all__ 里:"
        f"{missing}\n"
        f"用 `from ._shared import *` 的模块会拿到 NameError。"
        f"加进 __all__,或者不要导入。")


@pytest.mark.parametrize("simulated", ["AutoRepayRunResult"])
def test_criterion_detects_a_missing_name(simulated: str) -> None:
    """**自检**:判据必须真的会红,否则上面那条可能恒绿。

    模拟「加了个新类型但忘了加 `__all__`」—— 这正是本会话踩的那次。
    """
    imported, allnames = _parse()
    assert simulated not in imported or simulated not in allnames, (
        "这个测试用的名字已经存在了,换一个")
    missing = sorted((imported | {simulated}) - allnames)
    assert simulated in missing, "判据对漏登记没反应 —— 它可能恒绿"