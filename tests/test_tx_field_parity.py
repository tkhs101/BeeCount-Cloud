"""后端护栏：交易写入模型的字段必须对齐。

## 为什么需要

批量导入路径上，税额字段被 pydantic 边界**静默吃掉过两次**：

1. `BatchTransactionItem` 没有 `tax_amount` 字段 —— pydantic 默认
   `extra='ignore'`，MCP 明明传了，`req.model_dump()` 时已经没了
2. `_build_tx_payload` 是白名单式构造 payload，schema 补上了它也没写

两次都不报错。而单条 `create_transaction` 走的是另一个 schema，**测单条完全
发现不了** —— 只有批量丢。

根因是「同一个交易字段要在多个 model 里各写一遍」，没有任何机制保证它们同步。
CSV 导入的列映射漏 `tax_amount` 也是同一形态（那个是跨端点，不是跨 model）。

## 判定方式

静态扫 `src/**` 里所有 `class X(BaseModel)`,挑出**交易类**模型
（同时含 `amount` 且含 `tx_type` 或 `happened_at`）,断言它们都有 `tax_amount`。

用 AST 而不是 import —— 扫全部模块会触发 `src.main` 的 app 创建等副作用。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

# 交易类模型的判定:金额字段 + 至少一个交易特征字段
_TX_MARKERS = {"tx_type", "happened_at"}
_AMOUNT_FIELDS = {"amount"}
# 税额字段在本仓的两种命名(snake 进 schema / camel 进 snapshot item)
_TAX_FIELDS = {"tax_amount", "taxAmount"}


def _class_fields(node: ast.ClassDef) -> set[str]:
    """收集类体里直接声明的字段名(含 AnnAssign 和带赋值的 Assign)。"""
    out: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    out.add(tgt.id)
    return out


def _is_base_model(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = None
        if isinstance(base, ast.Name):
            name = base.id
        elif isinstance(base, ast.Attribute):
            name = base.attr
        if name in {"BaseModel", "WriteBaseRequest"}:
            return True
    return False


def _transaction_models() -> list[tuple[str, str]]:
    """返回 [(相对路径, 类名), ...] —— 所有交易类 pydantic 模型。"""
    found: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        rel = path.relative_to(SRC.parent).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _is_base_model(node):
                continue
            fields = _class_fields(node)
            if fields & _AMOUNT_FIELDS and fields & _TX_MARKERS:
                found.append((rel, node.name))
    return found


TX_MODELS = _transaction_models()


def test_scanner_found_transaction_models() -> None:
    """护栏自身的自检:扫描器得真的扫到东西,否则「全都有」是空集通过。"""
    names = {name for _, name in TX_MODELS}
    assert len(TX_MODELS) >= 5, TX_MODELS
    for expected in (
        "WriteTransactionCreateRequest",
        "WriteTransactionUpdateRequest",
        "BatchTransactionItem",
    ):
        assert expected in names, sorted(names)


@pytest.mark.parametrize(
    "rel,cls", TX_MODELS, ids=[f"{c}" for _, c in TX_MODELS]
)
def test_transaction_write_models_declare_tax_amount(rel: str, cls: str) -> None:
    """每个交易类模型都必须声明税额字段。

    加新字段时漏掉某个 model,这条会立刻红 —— 批量导入丢字段那次就是
    「schema 补了、白名单构造忘了」,只测单条完全发现不了。
    """
    path = SRC.parent / rel
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == cls
    )
    fields = _class_fields(node)
    assert fields & _TAX_FIELDS, (
        f"{rel}:{cls} 是交易类模型但没有 tax_amount —— "
        f"字段会在这个边界被静默丢弃(批量导入丢过一次)"
    )