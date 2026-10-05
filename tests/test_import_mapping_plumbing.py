"""导入字段映射的**接线完整性**护栏。

## 这条护栏在防什么

`ImportFieldMapping`（`src/services/import_data/schema.py`）是 CSV 导入的内部表示。
它要先穿过三层**显式枚举字段**的管道才能到达 `transformer`：

```
FieldMappingPayload(Pydantic)  →  to_internal()  →  ImportFieldMapping
                                                          ↕
       客户端拿到的 suggested_mapping ← _mapping_to_payload()  ←┘
```

三层里**任何一层漏掉一个字段**，Pydantic 的 `extra=ignore` 会**静默丢弃**它，
客户端传回来的映射就此消失 —— 不报错、不告警、HTTP 200。

`tax_amount`（0020）与 `splits`（0021）**各踩过一次**。作者在 `tax_amount`
旁边已经写下了警告「前端映射编辑器会把整个 mapping 原样回传，这里少一个字段
就等于用户点一次『应用』把税额全丢掉」——但 0021 加字段时没人把这条警告
一并复制过去，于是 splits 又丢了一次。

真实后果（2026-10-05 由新 Web UI 的端到端验证发现）：一笔 ¥5000 的组合支付
（招行卡 3000 + 现金 2000）导入后变成「招行卡上的 ¥5000 普通支出」，
账户余额直接错。而导出侧 `_splits_cell` 一直会写这一列，所以
「导出 → 编辑 → 导入」这条**最常见的往返路径必然踩到**。

## 所以护栏断言的不是「splits 在不在」

断言某个具体字段，下一个人加第 22 个字段时它照样会红不了。这里断言的是
**不变量**：`ImportFieldMapping` 的每一个字段，都必须出现在三层管道里。
新增字段忘了接线 → 这条测试立刻红，且指名道姓告诉你是哪一层漏了。

这是「用一条测试防住一整类 bug」的写法，而不是「为一次 bug 补一条测试」。
"""

from __future__ import annotations

import dataclasses

import pytest

from src.routers.import_data.endpoints import FieldMappingPayload, _mapping_to_payload
from src.services.import_data.schema import ImportFieldMapping


def _internal_field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(ImportFieldMapping)}


def _payload_model_field_names() -> set[str]:
    """`FieldMappingPayload` 显式声明的字段名。

    ⚠️ 必须用 `model_fields` 而不是 `__fields__`：后者在 Pydantic v1 里已废弃，
    v2 上还留着但带 deprecation 警告，早晚会消失。
    """
    return set(FieldMappingPayload.model_fields.keys())


def test_payload_model_covers_every_internal_field() -> None:
    """`FieldMappingPayload` 必须声明 `ImportFieldMapping` 的每一个字段。

    漏一个 → 客户端回传的映射在这一层就被 Pydantic 静默丢掉。
    """
    internal = _internal_field_names()
    declared = _payload_model_field_names()
    missing = internal - declared

    assert not missing, (
        "ImportFieldMapping 有字段没在 FieldMappingPayload 上声明，"
        "客户端传回的映射会被 Pydantic extra=ignore 静默丢弃："
        f"{sorted(missing)}"
    )


def _sentinels_for(name: str) -> list[object]:
    """给一个字段造一组候选哨兵值。

    ⚠️ **不要**靠 `dataclasses.fields()[i].type` 猜类型：这个 dataclass 用了
    `from __future__ import annotations`，`f.type` 拿到的是**字符串**`'int | None'`，
    而 `tz_offset_minutes` 与其它字段的运行时类型完全不同 —— 早先那版用
    「非 bool 就塞字符串」的启发式，在它上面直接抛 ValidationError。
    正确做法是让**字段自己**决定接受什么：挨个试，挑第一个构造得出来的。
    """
    candidates: list[object] = [
        ["哨兵标签"],       # list[str]
        True,              # bool（注意必须排在 1 之前：bool 是 int 的子类）
        1,                 # int
        "哨兵",            # str
    ]
    ok: list[object] = []
    for candidate in candidates:
        try:
            FieldMappingPayload(**{name: candidate})
        except Exception:  # noqa: BLE001 —— 这里就是要「构造失败就换下一个」
            continue
        ok.append(candidate)
    assert ok, f"字段 {name} 一个哨兵值都构造不出来，护栏本身写错了"
    return ok


def test_to_internal_covers_every_internal_field() -> None:
    """`to_internal()` 必须逐字段搬完 —— 少一个，那一层之后拿不到它。"""
    missing = []
    for f in dataclasses.fields(ImportFieldMapping):
        name = f.name
        # 取第一个能构造的哨兵；如果 to_internal 丢了它，另一个哨兵也过不去。
        if not any(
            getattr(FieldMappingPayload(**{name: s}).to_internal(), name) == s
            for s in _sentinels_for(name)
        ):
            missing.append(name)

    assert not missing, (
        "这些字段过不了 FieldMappingPayload.to_internal()，"
        f"也就是说客户端传回的映射在这一层就丢了：{sorted(missing)}"
    )


def test_mapping_to_payload_covers_every_internal_field() -> None:
    """`_mapping_to_payload()` 必须把每个字段回吐 —— 少一个，客户端看不见它。"""
    missing = []
    for f in dataclasses.fields(ImportFieldMapping):
        name = f.name
        if not any(
            _mapping_to_payload(ImportFieldMapping(**{name: s})).get(name) == s
            for s in _sentinels_for(name)
        ):
            missing.append(name)

    assert not missing, (
        "_mapping_to_payload() 没有回吐这些字段，客户端拿不到它们："
        f"{sorted(missing)}"
    )


@pytest.mark.parametrize(
    ("field", "cell", "expected"),
    [
        ("splits", "招行卡:3000.00|现金:2000.00", [("招行卡", 3000.0), ("现金", 2000.0)]),
        # 非对称的旧格式故意不支持：一条腿会走普通单账户路径，
        # 静默接受会让用户以为分账生效了。
        ("splits", "招行卡:3000.00", None),
        ("splits", "", None),
        ("splits", None, None),
    ],
)
def test_splits_cell_round_trips_through_the_pipe(
    field: str, cell: str | None, expected: list[tuple[str, float]] | None
) -> None:
    """`splits` 一个字段的端到端往返：客户端 cell → payload → internal → 解析。

    这条是上面三条不变量在真实字段上的具体化。`_parse_splits` 的行为本身
    由 `tests/test_tx_split_invariants.py` 守，这里只管**管道不吞它**。
    """
    from src.services.import_data.transformer import _parse_splits

    client_sends = FieldMappingPayload(**{field: cell})
    internal_obj = client_sends.to_internal()
    back_to_client = _mapping_to_payload(internal_obj)

    assert back_to_client.get(field) == cell, "映射原样回吐"
    assert _parse_splits(getattr(internal_obj, field)) == expected