"""账户反向桥(`snapshot_builder`)的**通用**覆盖检查

## 同一个 bug 族,另一个入口

`tests/test_projection_reverse_bridge_coverage.py` 管的是**交易**那侧
(`_projection_row_to_tx_dict`)。`autorepay_*`(0022)当初漏在**另一处**:
`snapshot_builder` 里账户的反向桥。

漏了之后:任何一次**无关字段**的账户 PATCH(Web 上改个备注)都会把
自动还款配置清空 —— HTTP 200,不报错,用户半年后才发现卡没自动还。

两条路径同源同形,但**各自需要自己的护栏**:一边改了不代表另一边改了。

## 为什么这条必须**真跑**,不能读源码

第一版守卫用「列名是否出现在 `build` 的源码里」。删掉桥的那一行
`acc["autorepayLastPeriod"] = ...` 之后,列名**仍然出现**在:
SELECT 列表、函数参数、注释、`if` 判断 —— **守卫 0 条变红,完全没咬住**。

静态判据在这里太弱:「这个名字在文件里出现过」≠「这个值被复制进了
输出」。真跑一次就没有这种歧义 —— 输出 dict 里有没有那个 key 是确定的。

(交易侧那条能用静态判据,是因为每个列在 `_projection_row_to_tx_dict`
里只有**唯一**一处 `row.<列>` 引用。**两边的判据不能照抄**。)

## `autorepay_last_period` 是**系统写的**,但仍必须往返

它由还款执行器在成功还款后写回,不是用户输入。走的是「读回来 → 原样带回
→ 写回去」的往返。PATCH 时若丢掉它,「这一期已还」的标记被抹 →
**下次重复还同一期**。所以它不在白名单里。
"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models import Ledger, UserAccountProjection

#: 账户列(排除身份/簿记)→ `build` 产出的 item dict 里的 key。
#:
#: 加新列时**必须**在这里加一行;不登记,`test_every_account_column_survives_the_bridge`
#: 会红并指出缺哪个 key。映射写错(比如 camelCase 拼错)同样会红。
COLUMN_TO_ITEM_KEY = {
    "name": "name",
    "account_type": "type",
    "currency": "currency",
    "initial_balance": "initialBalance",
    "note": "note",
    "credit_limit": "creditLimit",
    "billing_day": "billingDay",
    "payment_due_day": "paymentDueDay",
    "autorepay_enabled": "autorepayEnabled",
    "autorepay_from_account_sync_id": "autorepayFromAccountSyncId",
    "autorepay_last_period": "autorepayLastPeriod",
    "bank_name": "bankName",
    "card_last_four": "cardLastFour",
    "hidden": "hidden",
}

#: 身份 / 同步簿记 —— 不属于快照 item,反向桥不复制。
NOT_IN_SNAPSHOT_ITEM = {
    "user_id",
    "sync_id",
    "source_change_id",
}


def _built_account() -> dict:
    """真跑一次 `snapshot_builder.build`,返回它产出的那个账户 item。"""
    from src.snapshot_builder import build

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)

    with TS() as db:
        db.add(Ledger(id="lg", user_id="u1", external_id="e", name="日常",
                      currency="JPY"))
        db.add(UserAccountProjection(
            user_id="u1", sync_id="acc-1", name="招行信用卡",
            account_type="credit_card", currency="JPY",
            initial_balance=1234.0, billing_day=10, payment_due_day=25,
            autorepay_enabled=True,
            autorepay_from_account_sync_id="acc-src",
            autorepay_last_period="2026-04", bank_name="招行",
            card_last_four="1234", credit_limit=100000.0,
            hidden=True, note="主卡", source_change_id=1))
        db.commit()
        ledger = db.scalar(select(Ledger).where(Ledger.id == "lg"))
        snap = build(db, ledger)

    items = snap.get("items") or snap.get("accounts") or []
    for it in items:
        if isinstance(it, dict) and it.get("syncId") == "acc-1":
            return it
    raise AssertionError(
        f"build 没产出 acc-1 —— 夹具或 build 的输出格式变了。"
        f"items={items}")


def test_every_account_column_survives_the_bridge() -> None:
    """**加新列忘了改账户反向桥** → 输出的 dict 里没那个 key → 立刻红。

    真跑 `build`,不看源码。源码里出现过列名不代表值被复制出去了。
    """
    item = _built_account()
    missing = sorted(
        col for col, key in COLUMN_TO_ITEM_KEY.items() if key not in item
    )
    assert not missing, (
        f"账户反向桥漏了这些列: {missing}\n\n"
        f"漏一列 = 任何一次**无关字段**的账户 PATCH 都会把它写成 NULL,"
        f"HTTP 200,用户配置静默消失 —— `autorepay_*`(0022)当初就是这么"
        f"的。\n实际产出的 key: {sorted(item)}\n"
        f"修法:在 `snapshot_builder` 里把它写进 acc[…],并在上方 "
        f"`COLUMN_TO_ITEM_KEY` 登记对应 key。")
    # 映射表不能漏登记新列 —— 否则加列时这条测试会假绿
    modelled = set(COLUMN_TO_ITEM_KEY) | NOT_IN_SNAPSHOT_ITEM
    real = {c.name for c in UserAccountProjection.__table__.columns}
    unmodelled = sorted(real - modelled)
    assert not unmodelled, (
        f"这些模型列既没登记在 COLUMN_TO_ITEM_KEY 也没在白名单里: "
        f"{unmodelled}\n必须论证它走不走反向桥 —— 默认应当走。")


def test_autorepay_columns_actually_travel() -> None:
    """点名 `autorepay_*` —— 已栽过一次,且 `last_period` 关系到重复还款。

    `autorepay_last_period` 由执行器在成功还款后写回。PATCH 时若丢掉它,
    「这一期已还」的标记被抹 → **下次重复还同一期**。
    """
    item = _built_account()
    assert item.get("autorepayEnabled") is True, item.get("autorepayEnabled")
    assert item.get("autorepayFromAccountSyncId") == "acc-src", item
    assert item.get("autorepayLastPeriod") == "2026-04", (
        "autorepayLastPeriod 没跟着走 —— PATCH 会抹掉它,下次重复还款")


def test_guard_mechanism_itself_works() -> None:
    """守卫自己必须能咬住 —— 用一个确实不存在的 key。

    这套代码里已经吃过两次「看着在跑、其实永远通过」的断言亏,
    见 CLAUDE.md「源码级断言自己也会恒真」。
    """
    item = _built_account()
    ghost_key = "definitelyNotARealKeyXyz"
    detected_missing = [
        c for c, k in {"ghost": ghost_key}.items() if k not in item
    ]
    assert detected_missing == ["ghost"], "检测逻辑对虚构 key 失效"
    # 反向对照:一个真 key 确实在输出里
    assert "autorepayEnabled" in item, "反向对照失败"