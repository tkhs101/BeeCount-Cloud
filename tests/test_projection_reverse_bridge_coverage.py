"""反向桥的**通用**覆盖检查(这一族已栽 5 次)

## 为什么要有这条测试

`_projection_row_to_tx_dict` 是全仓最危险的函数:它产出的 dict 是
Web PATCH 的**唯一世界模型**。少一个 key → mutator 看不到旧值 →
`upsert_tx` 写成 NULL → **用户数据被静默抹掉,HTTP 200**。

这个 bug 族已经栽了 **5 次**:

| 列 | 漏在哪 | 后果 |
|---|---|---|
| `native_amount` | 上游 `_projection_row_to_tx_dict` | 多币种折算被抹 |
| `tax_amount` | `_projection_row_to_tx_dict` | 税额被抹 |
| `splits` | `_projection_row_to_tx_dict`(子表) | 组合支付余额永久漂移 |
| `autorepay_*` | `snapshot_builder` | 自动还款配置被清空 |
| (还有) | | |

**此前每一次都是事后补一条专用测试**(`test_tx_tax_amount.py` /
`test_tx_split_invariants.py` / `test_credit_autorepay_e2e.py` …)。

那不是解决方案 —— 那是**症状处理**。第 6 列被加进来时,同样的事会再发生
一次,仍然要等用户发现数据被抹了才有人去查。

## 这条测试做什么

枚举 `ReadTxProjection.__table__.columns` 的**全部**列,减去「身份/簿记」
白名单(那些不属于快照 item),剩下的**每一列都必须**在反向桥的源码里
被引用。

加新列时忘了改反向桥 → **这条立刻红**,并列出漏掉的列名。

## 白名单为什么是白名单而不是黑名单

黑名单(列出「已知可以漏的列」)在加新列时会**默认通过** —— 而那正是
我们要防的情况。白名单(列出「确实不属于快照 item 的列」)则默认**拒绝**,
新列必须显式论证为什么可以不接。反转默认值是关键。
"""
from __future__ import annotations

import inspect
import re

import pytest

from src.models import ReadTxProjection
from src.routers.write._shared import _projection_row_to_tx_dict

#: 身份与簿记列 —— **不属于快照 item**,反向桥不需要(也不应该)复制它们。
#:
#: 新增一列若落在本名单里,必须在这里加一行 + 写清理由。
NOT_IN_SNAPSHOT_ITEM = {
    # 主键 / 外键:由 upsert_tx 从 row 主键推导,不由 payload 决定
    "id",
    "ledger_id",
    "user_id",
    "sync_id",
    # 同步簿记
    "source_change_id",
    "tx_index",
    "created_by_user_id",
    "last_edited_by_user_id",
}

#: 同名透传但**不复制**的列 —— 它们由 `upsert_tx` 依据 `native_amount`
#: 与账本汇率重算,复制反而会造成两份真相。
#:
#: ⚠️ 这一条是 `currency_code`/`native_amount` 出现在白名单里的理由:
#: 它们是**账本维度的派生值**,由 mutator 按账本本位币重算。
#: 若反向桥把它们当用户输入回传,mobile 同步过来的旧折算值会覆盖
#: 服务端刚算好的值 —— 上游 `nativeAmount` 当年就栽在这里。
DERIVED_BY_MUTATOR = {
    "currency_code",
    "native_amount",
}


def _bridge_source() -> str:
    return inspect.getsource(_projection_row_to_tx_dict)


def test_every_projection_column_is_covered_by_the_bridge() -> None:
    """**加新列忘了改反向桥** → 这条立刻红,并指出漏了哪列。"""
    src = _bridge_source()
    # 只看代码,不看注释 —— 注释里出现的列名不算「被复制」。
    # 上界踩过:`" ".join(token.string ...)` 会把 `row.note` 拆成
    # `row . note`,join 后匹配不上原样的 `row.note`,断言恒真。
    # 正确写法是 untokenize。
    import io
    import tokenize

    kept = [t for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING)]
    code = tokenize.untokenize(kept)

    missing = sorted(
        c.name
        for c in ReadTxProjection.__table__.columns
        if c.name not in NOT_IN_SNAPSHOT_ITEM
        and c.name not in DERIVED_BY_MUTATOR
        and f"row.{c.name}" not in code
    )
    assert not missing, (
        f"反向桥漏了这些列: {missing}\n\n"
        f"每一列都必须写进返回的 dict —— 漏一列 = 任何一次**无关字段**的 "
        f"Web PATCH 都会把它写成 NULL,HTTP 200,用户数据静默消失。\n"
        f"这一族已栽 5 次(nativeAmount / taxAmount / splits / autorepay_* …)。\n"
        f"若确认某列**不该**走反向桥,把它加进 NOT_IN_SNAPSHOT_ITEM "
        f"或 DERIVED_BY_MUTATOR,并在那个名单里写明理由 —— "
        f"别直接删掉断言。")


def test_guard_mechanism_itself_works() -> None:
    """**这条守卫自己必须能咬住** —— 用一个确实不存在的列名。

    没有这一条,上面那条可能因为任何原因恒真(我在这套代码里遇到过
    三种「看着在跑、其实永远通过」的断言,详见 CLAUDE.md)。
    """
    import io
    import tokenize

    src = _bridge_source()
    kept = [t for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING)]
    code = tokenize.untokenize(kept)

    ghost = "definitely_not_a_real_column_xyz"
    assert ghost not in {c.name for c in ReadTxProjection.__table__.columns}
    # 同样的检测逻辑,对一个「本该被判为 missing」的列必须给出 missing
    detected_missing = ghost not in NOT_IN_SNAPSHOT_ITEM         and ghost not in DERIVED_BY_MUTATOR         and f"row.{ghost}" not in code
    assert detected_missing, "检测逻辑对虚构列失效 —— 上面的守卫可能恒真"
    # 反向对照:一个**真列**被引用时不该被判 missing
    assert "row.tax_amount" in code, "反��对照失败:tax_amount 确实在反向桥里"


def test_whitelists_do_not_swallow_real_columns() -> None:
    """白名单不能大到把真实列吞掉。

    如果有人为了「让测试绿」把 `tax_amount` 塞进白名单,用户数据的税额
    又会被静默抹掉 —— 而测试全绿。所以白名单必须有上限。
    """
    assert len(NOT_IN_SNAPSHOT_ITEM) <= 10, (
        "身份/簿记白名单变长了 —— 每加一列都要写明为什么它不是用户数据")
    assert len(DERIVED_BY_MUTATOR) <= 3, (
        "mutator 派生的白名单变长了 —— 派生列复制进 payload 会造成两份真相。"
        "先证明这不是在绕过测试。")


def test_tax_amount_and_splits_are_actually_copied() -> None:
    """点名这两个**已经栽过**的列 —— 它们必须一直被复制。

    通用检查会覆盖它们,但历史上它们各栽一次,值得单独钉死:
    将来若有人为了「简化」把它们加进白名单,这条会给出更明确的信号。
    """
    src = _bridge_source()
    assert "row.tax_amount" in src, "taxAmount 又漏了(0020)"
    # splits 不在 row 上,在 `splits` 参数里
    assert "item_splits" in src, (
        "splits 又漏了(0021)—— 组合支付余额会永久漂移")