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

# --------------------------------------------------------------------------- #
# 白名单式 payload 构造                                                        #
# --------------------------------------------------------------------------- #
#
# 批量导入丢税额有**两个**丢失点,上一个护栏只覆盖了第一个:
#   ① BatchTransactionItem 没有 tax_amount 字段 → pydantic 静默丢
#   ② _build_tx_payload 是白名单式构造,schema 补上了它也没写
#
# 只测「schema 有没有字段」抓不到 ②。下面这条按**实际调用**逐字段核对:
# 把 item 的每个字段都填上非空值,断言每个字段名都出现在产出的 payload 里。


def test_batch_payload_builder_copies_every_item_field() -> None:
    """`_build_tx_payload` 必须把 item 的**每一个**非空字段带进 payload。

    白名单式构造的固有风险:schema 加了字段、这里忘了写,字段就在这个边界
    静默消失且不报错。税额丢过一次。
    """
    from datetime import datetime, timezone

    from src.models import User
    from src.routers.write.transactions_batch import (
        BatchTransactionItem,
        _build_tx_payload,
    )

    # 每个字段都给非空值 —— 用类型对应的「合法填充值」
    full: dict = {
        "tx_type": "expense",
        "amount": 3280.0,
        "happened_at": datetime(2026, 10, 3, tzinfo=timezone.utc),
        "note": "KING BEAR NOW",
        "category_name": "餐饮",
        "category_kind": "expense",
        "account_name": "现金",
        "from_account_name": "招行",
        "to_account_name": "支付宝",
        "category_id": "cat-1",
        "account_id": "acc-1",
        "from_account_id": "acc-from",
        "to_account_id": "acc-to",
        "tags": ["MCP"],
        "currency_code": "CNY",
        "native_amount": 3280.0,
        "tax_amount": 298.0,
    }
    item = BatchTransactionItem(**full)
    # schema 里没有的字段不许出现在 fixture 里,否则这条测试会跟着实现漂移
    assert set(full) == set(BatchTransactionItem.model_fields), (
        f"fixture 与 model 字段不同步: "
        f"缺 {set(BatchTransactionItem.model_fields) - set(full)}, "
        f"多 {set(full) - set(BatchTransactionItem.model_fields)}"
    )

    user = User(id="u1", email="a@b.com", password_hash="x", is_admin=False,
                is_enabled=True)
    payload = _build_tx_payload(
        item=item, auto_tag_names=[], attachment_dict=None, actor_user=user
    )

    missing = [
        name for name, value in full.items()
        if value is not None and name not in payload
    ]
    assert not missing, (
        f"_build_tx_payload 白名单漏掉了这些字段:{missing} —— "
        f"它们会在这个边界被静默丢弃(批量导入的税额丢过一次)"
    )

# 注:曾写过一条「None 字段不该出现在 payload」的断言,后来删掉了 ——
# 它测的是实现细节而非行为。`_build_tx_payload` 的初始 dict 会无条件带上
# `note: None`,但这完全无害:`snapshot_mutator.create_transaction` 每个字段都是
# `if payload.get(x) is not None` 才写 key,None 到那里会被跳过。
# 「字段为 None 时会不会产生空值」这个问题由 mutator 统一保证,
# 在 payload 构造层重复断言只会让人误以为那里也有语义。
