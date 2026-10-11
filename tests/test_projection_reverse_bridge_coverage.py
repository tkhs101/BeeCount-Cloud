"""反向桥的**运行期**覆盖检查(这一族已栽 5 次)

## 为什么必须**真跑**,不能读源码

`_projection_row_to_tx_dict` 是全仓最危险的函数:它产出的 dict 是
Web PATCH 的**唯一世界模型**。少一个 key → mutator 看不到旧值 →
`upsert_tx` 写成 NULL → **用户数据被静默抹掉,HTTP 200**。

已栽 5 次:`nativeAmount`(上游)、`taxAmount`(0020)、`splits`(0021)、
`autorepay_*`(snapshot_builder)、账本 internal/external id 混淆。

**此前每一次都是事后补一条专用测试** —— 那是症状处理。第 6 列被加进来
时同样的事会再发生一次,仍然要等用户发现数据被抹了才有人去查。

## ⚠️ 第一版(静态判据)是假的,变异时才发现

第一版断言「列名是否出现在函数源码里」。变异:把

```python
item["taxAmount"] = row.tax_amount     # <- 删掉这一行
```

改成别的,结果**所有测试绿**。因为上一行

```python
if row.tax_amount is not None:         # <- 仍然含 row.tax_amount
```

还在 —— 「这个名字在文件里出现过」不等于「这个值被复制进了输出」。

(我第一版还断言过「交易侧能用静态判据、账户侧不行,因为每个列只有唯一一处
`row.<列>` 引用」。**那个判断也是错的** —— 上面那个 `if` 就是第二处。
两轮之后才发现。)

真跑一次就没有这种歧义:输出 dict 里有没有那个 key 是确定的。

## 白名单而不是黑名单

黑名单在加新列时**默认通过**(而那正是要防的情况),白名单默认**拒绝**,
新列必须显式论证为什么可以不接。**反转默认值才是防线起作用的方式。**

白名单带数量上限,防止有人为了让测试绿把真列塞进去。
"""
from __future__ import annotations

import pytest

from src.models import ReadTxProjection
from src.routers.write._shared import _projection_row_to_tx_dict

#: 列(排除身份/簿记/派生)-> 返回 dict 里应���的 key。
#:
#: 加新列时**必须**在这里加一行,否则下面那条会红并指出缺哪个 key。
#: 映射写错(camelCase 拼错)同样会红。
COLUMN_TO_ITEM_KEY = {
    "sync_id": "syncId",
    "tx_type": "type",
    "amount": "amount",
    "happened_at": "happenedAt",
    "note": "note",
    "category_sync_id": "categoryId",
    "category_name": "categoryName",
    "category_kind": "categoryKind",
    "account_sync_id": "accountId",
    "account_name": "accountName",
    "from_account_sync_id": "fromAccountId",
    "from_account_name": "fromAccountName",
    "to_account_sync_id": "toAccountId",
    "to_account_name": "toAccountName",
    "tags_csv": "tags",
    # 这两列在 bridge 里是 **JSON 解析后**的产物,不是原样字符串 ——
    # `tag_sync_ids_json` -> `tagIds`(list),`attachments_json` -> 附件 dict 列表。
    # 解析失败会被 `except json.JSONDecodeError: pass` **静默跳过**,所以
    # 上面 `_full_row` 给的是合法 JSON。运行期判据立刻把这两列报了出来
    # —— 静态判据下它们看起来「已被引用」,实际值有没有传出去测不到。
    "tag_sync_ids_json": "tagIds",
    "attachments_json": "attachments",
    "exclude_from_stats": "excludeFromStats",
    "exclude_from_budget": "excludeFromBudget",
    "tax_amount": "taxAmount",
}

#: 身份 / 同步簿记 —— 不属于快照 item。
NOT_IN_SNAPSHOT_ITEM = {
    "id",
    "ledger_id",
    "user_id",
    "source_change_id",
    "tx_index",
    "created_by_user_id",
    "last_edited_by_user_id",
}

#: 由 `upsert_tx` 依据账本汇率重算的派生列 —— 复制进 payload 会造成
#: **两份真相**(mobile 同步来的旧折算值会覆盖服务端刚算好的)。
#: 上游 `nativeAmount` 当年就栽在这里。
DERIVED_BY_MUTATOR = {
    "currency_code",
    "native_amount",
}


def _full_row(**over):
    """造一个「每个列都有值」的 projection 行 —— 键存在时才有判别力。

    每列都给非空值:函数里有大量 `if row.X is not None:` 分支,
    值全空时输出 dict 里一�� key 都没有,测试会误判成「全部漏了」。
    """
    from datetime import datetime, timezone

    base = dict(
        ledger_id="lg",
        sync_id="tx-1",
        user_id="u1",
        tx_type="expense",
        amount=1000.0,
        happened_at=datetime(2026, 5, 13, 9, 0, tzinfo=timezone.utc),
        note="午餐",
        category_sync_id="cat-1",
        category_name="餐饮",
        category_kind="expense",
        account_sync_id="acc-1",
        account_name="招行信用卡",
        from_account_sync_id="frm-1",
        from_account_name="招行储蓄卡",
        to_account_sync_id="to-1",
        to_account_name="现金",
        tags_csv="工作,午餐",
        tag_sync_ids_json='["t1","t2"]',
        attachments_json='[{"id":"a1"}]',
        tx_index=0,
        created_by_user_id="u1",
        last_edited_by_user_id="u1",
        source_change_id=1,
        exclude_from_stats=False,
        exclude_from_budget=False,
        currency_code="JPY",
        native_amount=1000.0,
        tax_amount=380.0,
    )
    base.update(over)
    return ReadTxProjection(**base)


def _built(**over) -> dict:
    """真跑一次反向桥,返回它产出的 item dict。"""
    return _projection_row_to_tx_dict(_full_row(**over))


def test_every_projection_column_survives_the_bridge() -> None:
    """**加新列忘了改反向桥** -> 输出的 dict 里没那个 key -> 立刻红。

    真跑,不看源码。第一版用「列名是否出现在源码里」,被
    `if row.X is not None:` 这种**只判断不赋值**的行骗过去了。
    """
    item = _built()
    missing = sorted(
        col for col, key in COLUMN_TO_ITEM_KEY.items() if key not in item
    )
    assert not missing, (
        f"反向桥漏了这些列: {missing}\n\n"
        f"漏一列 = 任何一次**无关字段**的 Web PATCH 都会把它写成 NULL,"
        f"HTTP 200,用户数据静默消失。这一族已栽 5 次。\n"
        f"实际产出的 key: {sorted(item)}\n"
        f"修法:在 `_projection_row_to_tx_dict` 里把它写进 item[...],"
        f"并在上方 `COLUMN_TO_ITEM_KEY` 登记对应 key。")

    # 映射表本身也要被守 —— 否则「忘了改桥」和「忘了更新表」同时发生,
    # 测试反而假绿。
    modelled = (set(COLUMN_TO_ITEM_KEY) | NOT_IN_SNAPSHOT_ITEM
                | DERIVED_BY_MUTATOR)
    real = {c.name for c in ReadTxProjection.__table__.columns}
    unmodelled = sorted(real - modelled)
    assert not unmodelled, (
        f"这些模型列既没登记在 COLUMN_TO_ITEM_KEY、也没进白名单: "
        f"{unmodelled}\n必须论证它走不走反向桥 —— 默认应当走。")


def test_tax_amount_and_splits_actually_travel() -> None:
    """点名这两个**已经栽过**的列。

    `taxAmount`(0020):Web PATCH 改个备注就把税额抹掉。
    `splits`(0021):在子表里,漏了 -> 组合支付余额**永久漂移**。
    """
    item = _built()
    assert item["taxAmount"] == 380.0, item.get("taxAmount")
    # splits 不在 row 上,是第二个参数;有腿时必须进 item
    with_splits = _projection_row_to_tx_dict(
        _full_row(), [{"accountId": "acc-1", "amount": 300.0},
                     {"accountId": "acc-2", "amount": 700.0}])
    assert with_splits.get("splits"), with_splits.get("splits")


@pytest.mark.parametrize("col,key", [
    ("tax_amount", "taxAmount"),
    ("note", "note"),
    ("category_name", "categoryName"),
])
def test_a_column_is_not_just_mentioned_but_actually_copied(
        col: str, key: str) -> None:
    """**点名 + 真值**:证明那一列的值真进了输出,不只是被 `if` 提到。

    这是对第一版假判据的直接回应:`if row.tax_amount is not None:` 里
    含有 `row.tax_amount`,但那不等于值被复制出去了。
    """
    item = _built()
    assert key in item, f"{col} 没进输出"
    assert item[key] == getattr(_full_row(), col), (
        f"{key} 的值不对 —— 被复制的不是该列的值")


def test_whitelists_do_not_swallow_real_columns() -> None:
    """白名单不能大到把真列吞掉。

    有人为了「让测试绿」把 `tax_amount` 塞进白名单,用户数据的税额又会被
    静默抹掉 —— 而测试全绿。所以白名单必须有上限。
    """
    assert len(NOT_IN_SNAPSHOT_ITEM) <= 10, (
        "身份/簿记白名单变长了 —— 每加一列都要写明为什么它不是用户数据")
    assert len(DERIVED_BY_MUTATOR) <= 3, (
        "mutator 派生的白名单变长了 —— 派生列复制进 payload 会造成两份真相。"
        "先证明这不是在绕过测试。")
    assert "tax_amount" not in NOT_IN_SNAPSHOT_ITEM
    assert "tax_amount" not in DERIVED_BY_MUTATOR


def test_guard_mechanism_itself_works() -> None:
    """守卫自己必须能咬住 —— 用一个确实不存在的 key。"""
    item = _built()
    ghost = "definitelyNotARealKeyXyz"
    assert [k for k in (ghost,) if k not in item] == [ghost], (
        "检测逻辑对虚构 key 失效")
    assert "taxAmount" in item, "反向对照失败"