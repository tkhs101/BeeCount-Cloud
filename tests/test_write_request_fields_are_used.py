"""`Write*Request` 声明的字段,必须在 `src/` 其它地方真的被用到。

## 它防什么(与已发生的两件事同族)

同一个交易字段要在多个 model / 多层实现里各写一遍,漏一处就**静默失效**:

1. `BatchTransactionItem` 缺 `tax_amount` —— pydantic 边界 `extra='ignore'`,
   MCP 传了,`req.model_dump()` 时已经没了(`test_tx_field_parity.py` 记的)
2. `create_transaction_with_receipt` 独缺 `splits` —— docstring 写着
   「参数与 create 完全一致」,实际不一致(`test_mcp_tool_signature_parity.py` 记的)

第 1 次有护栏、第 2 次靠人工比对才发现。**这个文件把那类人工比对变成默认红的门。**

## 判据为什么选「只在 schemas.py 里出现过」

- **宽判据**(字段名在整个 `src/` 的任何地方出现过)假阴性太多:任何 attribute
  同名就算「用过」,而 `amount` / `name` 这类到处都是
- **窄判据**(字段名**只在** `src/schemas.py` 里出现)才是真正的问题形状:
  声明了,但**没有任何一处代码读它**

⚠️ 所以要同时扫 `Attribute` / `Name` / 字符串常量 —— 因为 payload 构造有三种写法:
`req.amount`(Attribute)、局部变量(Name)、以及 `item["amount"]`(字符串下标)。
只认前两种会漏掉第三种,那就成了**假绿**。

## 不检查什么

**不要求「模型之间字段集合相等」** —— 本仓的模型**本就设计不同**
(`account_id` + `account_name` 成对、`WriteTransactionUpdateRequest` 有 `splits`
而批量没有)。要求相等会产出一大批假阳性,而恒定的假阳性会让人习惯性忽略整条报错
(第 130 轮实测过这个坑)。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SCHEMAS = SRC / "schemas.py"


def _declared_write_fields() -> set[str]:
    """所有 `Write*Request` 模型直接声明的字段名。"""
    tree = ast.parse(SCHEMAS.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.name.startswith("Write") or not node.name.endswith("Request"):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                out.add(stmt.target.id)
    return out


def _names_used_outside_schemas() -> set[str]:
    """`src/` 下除 `schemas.py` 外出现过的所有标识符与字符串常量。"""
    used: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name == "schemas.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 源码坏了应由别处报
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                used.add(node.value)
    return used


DECLARED = _declared_write_fields()
USED_ELSEWHERE = _names_used_outside_schemas()


def test_criterion_actually_scanned_enough_fields() -> None:
    """⚠️ **下限自检** —— 「没有孤儿」在扫到 0 个字段时也是真的。

    同 CLAUDE.md 记的「正则失效要自检」「tokenize 恒真」那几条:
    **让判据恒真的护栏,比没有护栏更糟**,因为它让人以为这里有保护。
    """
    assert len(DECLARED) >= 40, f"只扫到 {len(DECLARED)} 个字段,判据可能失效"


def test_no_write_request_field_is_declared_and_forgotten() -> None:
    """声明了却没有任何代码读 -> 调用方传了、拿到 200、什么都没发生。"""
    orphans = sorted(DECLARED - USED_ELSEWHERE)
    assert not orphans, (
        "这些字段只在 src/schemas.py 里出现过,没有任何代码读它们:\n"
        + json.dumps(orphans, ensure_ascii=False, indent=2)
        + "\n\n→ 要么端点漏了把它放进 payload(调用方传了不生效),"
        "要么这个字段根本不该存在。"
    )