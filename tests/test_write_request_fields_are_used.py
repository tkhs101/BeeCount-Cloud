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

import src.schemas as S
from src.mcp.tools.write_tools import BatchTxItem

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

# --------------------------------------------------------------------------- #
# 组合支付(0021)的「登记点必须齐全」                                            #
# --------------------------------------------------------------------------- #
#
# `splits` 要在**两个不同的模型体系**里各登记一次,缺一个就有一半用户用不了:
#
# | 模型 | 身份 | 缺了会怎样 |
# |---|---|---|
# | `schemas.WriteTransactionCreateRequest`(Pydantic) | REST 收得下 | 400 / 字段被丢 |
# | `mcp.BatchTxItem`(**TypedDict**) | **LLM 看得见的 JSON Schema** | LLM 根本不知道能建 |
# | `transactions_batch.BatchTransactionItem`(Pydantic) | 批量端点收得下 | 批量导时腿被静默丢 |
#
# 📌 **前两轮分别踩了后两个**:139 轮修了 TypedDict 之外的,141 轮才发现 TypedDict
# 自己也没有。它们看起来都叫「批量」,但**是两个类**。
#
# 判据:**所有「交易类」模型(同时含 amount 与 tx_type/happened_at)都必须带 splits**。
# `test_tx_field_parity` 只查 `tax_amount` 一个字段;splits 是第二个,而且
# 加它的时候两条护栏都自动生效了 —— 说明这个判据可以推广到更多字段。

def _tx_like_fields(model) -> set[str]:
    """TypedDict 的字段在 `__annotations__`,Pydantic 的在 `model_fields` —— 两条都要。"""
    ann = set(getattr(model, "__annotations__", {}) or {})
    return ann or set(getattr(model, "model_fields", {}) or {})


def _collect_tx_like_models() -> list[tuple[str, type]]:
    """所有「交易类」模型:字段里同时有 amount 与 (tx_type | happened_at)。"""
    from src.routers.write.transactions_batch import BatchTransactionItem

    out: list[tuple[str, type]] = []
    for name in dir(S):
        obj = getattr(S, name)
        if not isinstance(obj, type):
            continue
        fields = _tx_like_fields(obj)
        if "amount" in fields and ("tx_type" in fields or "happened_at" in fields):
            out.append((f"schemas.{name}", obj))

    fields = _tx_like_fields(BatchTransactionItem)
    if "amount" in fields and ("tx_type" in fields or "happened_at" in fields):
        out.append(("transactions_batch.BatchTransactionItem", BatchTransactionItem))
    return out


TX_LIKE = _collect_tx_like_models()


def test_collects_the_expected_tx_models() -> None:
    """⚠️ **下限自检** —— 收集到 0 个时,下面那条会**恒真**。"""
    assert len(TX_LIKE) >= 4, (
        f"只收集到 {len(TX_LIKE)} 个交易模型:{[n for n, _ in TX_LIKE]}"
    )


def test_every_tx_like_model_declares_splits() -> None:
    """交易类模型必须带 `splits`。

    缺了不会报错:请求照收、金额照记,只是**分摊明细没了** ——
    而「无账户的支出」正是它本来的样子,看不出异常。
    """
    missing = []
    for name, model in TX_LIKE:
        fields = (
            set(getattr(model, "model_fields", {}) or {})
            if hasattr(model, "model_fields")
            else _tx_like_fields(model)
        )
        if "splits" not in fields:
            missing.append(name)

    assert not missing, (
        f"这些交易模型没有 splits 字段:{missing}\n"
        "  -> 加新交易模型时,**两个模型体系**都要登记"
        "(Pydantic 请求模型 + MCP 的 TypedDict)。"
    )


def test_mcp_batch_typeddict_declares_splits() -> None:
    """`BatchTxItem` 是 TypedDict,**同时是 LLM 看的 JSON Schema**。

    ⚠️ 它不会出现在 `model_fields` 里(那是 Pydantic 的属性),
    所以上面那条按 `model_fields` 查的判据**抓不到它** —— 必须单独确认。
    """
    assert "splits" in BatchTxItem.__annotations__, (
        "MCP 的 BatchTxItem(TypedDict) 没有 splits —— "
        "它是给 LLM 看的 JSON Schema 声明,不写 LLM 就看不到这个能力"
    )


def test_tax_zero_boundary_is_checked_per_entry_point() -> None:
    """`tax_amount == 0` 的判据**按入口分开**,不能一刀切。

    | 入口 | 0 的含义 | 该用什么 |
    |---|---|---|
    | `create_transaction` | 无意义(mutator 拒绝) | `tax <= 0` |
    | `create_transactions` | 同上 | `tax <= 0` |
    | `update_transaction` | **清除信号**(CLAUDE.md 记载) | `tax < 0` |

    ⚠️ 145 轮我把这个差异当成 bug「统一」了,结果 `update` 连清除税额都做不了,
    `test_mcp_update_zero_means_clear` 当场变红 —— **已有测试就是契约**。

    📌 **同一个字段在不同入口可以有不同语义。** 判断「哪个对」之前,
    先找有没有测试已经写死它(有测试 = 有意为之,不是漏)。
    """
    import inspect

    from src.mcp.tools import write_tools as w
    from src.snapshot_mutator import _normalize_tax_amount

    mutator = inspect.getsource(_normalize_tax_amount)
    assert "tax <= 0" in mutator, "mutator 的判据变了 —— 本护栏要跟着更新"

    # 批量:`tax` 局部变量名
    bulk = inspect.getsource(w.create_transactions)
    assert "if tax <= 0:" in bulk, (
        "批量预校验必须用 `tax <= 0` —— 与 mutator 同口径,"
        "否则 tax_amount=0 被放行后炸成与真实原因无关的错误"
    )

    # 单条:update 是「清除」语义,必须放行 0
    single = inspect.getsource(w.create_transaction)
    assert "if tax_amount <= 0:" in single, (
        "create 单条预校验必须用 `tax_amount <= 0`"
    )

    upd = inspect.getsource(w.update_transaction)
    assert 'if tax_amount == 0:' in upd, (
        "update 的 `0 = 清除` 分支不见了 —— 那是 CLAUDE.md 记的契约"
    )
    assert "if tax_amount < 0:" in upd, (
        "update 必须是 `tax < 0`:`0` 在上面已被转成清除信号,"
        "若这里写 `<= 0` 就会拦下「清除税额」这个请求"
    )


def test_every_leg_writer_resolves_account_names_or_refuses() -> None:
    """**每个把腿写进库的口子**都必须二选一:解析名字,或拒绝。

    ⚠️ 149 轮实测:`create_transaction`(MCP)把 `account`(**名字**)直接塞进
    `account_id`,而 mutator **只查非空、从不验证账户存在** —— 结果余额聚合
    按 account_id join 不上,**一分不扣、零报错**。

    同一仓的 `import_data` 却做得**完全正确**:它显式解析名字,并且
    **宁可整批失败也不把腿悄悄丢掉**。同一个坑,一条路有、一条路没有。

    📌 所以判据不是「有没有校验」,而是「**是不是每一个口子都有校验**」。
    这一条把三个写腿的口子同时圈住。
    """
    import inspect

    from src.mcp.tools import write_tools

    # ① MCP create_transaction:必须经过名字解析
    create = inspect.getsource(write_tools.create_transaction)
    assert "leg_name and not leg_id" in create, (
        "MCP create_transaction 的 splits 不再区分 `account`(名字)与 "
        "`account_id` —— 名字会被直接塞进 account_id,余额 join 不上"
    )

    # ② REST 写路径:腿只接受 account_id(Pydantic 必填,传名字会 **422** ——
    #    响亮失败,不会静默)。这是「第三条路」,靠**类型**而不是靠自觉。
    from src.schemas import TxSplit
    assert "account_id" in TxSplit.model_fields, "TxSplit 必须要求 account_id"

    # ③ CSV 导入:显式解析 + 查不到就整批失败
    from src.routers.import_data import endpoints as imp_ep

    imp_src = inspect.getsource(imp_ep)
    assert "split legs reference unknown accounts" in imp_src, (
        "CSV 导入不再对查不到的账户名整批失败 —— "
        "那正是 149 轮那个静默丢余额的同款"
    )
