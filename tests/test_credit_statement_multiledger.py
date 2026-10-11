"""账单必须跨**该用户全部账本**聚合。

## 为什么单独一个文件

账户是 **user-global**(`UserAccountProjection` PK = user_id + sync_id),
同一张卡可以同时用在多个账本里。它的真实欠款是跨账本之和 —— Web 的
workspace 读端点也是这么聚合的(`read/workspace.py` 把该用户可见的账本
全部纳入)。

而第一阶段实现里 `compute_statement(ledger_id=<单个>)`,那个 id 由
`repay._ledger_internal_of` **猜**出来(「最近一笔交易所在的账本,
否则用户第一个账本」)。于是:

    用户在第二个账本刷了 2000
      → 不出现在账单里
      → outstanding 偏小
      → **自动还款还得不够**

不报错、不告警,只是静静地少还。而用户看着界面上的欠款数额、自动还款却
按另一个数扣,只会得出「这软件算错了」。

## 分工:算账用全部账本,写交易仍用一个账本

- **算账** → `_all_ledger_ids(user_id)`:该用户的全部账本
- **写交易** → `_ledger_internal_of(...)`:猜一个(最近一笔交易所属账本)

这两个职责不能混。「写进哪个账本」确实只能选一个(一笔交易必须落在某个
账本里),但「有多少欠款」是所有账本之和。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models import (
    Ledger,
    ReadTxProjection,
    UserAccountProjection,
)
from src.services.credit_card.billing import current_billing_period
from src.services.credit_card.repay import _all_ledger_ids
from src.services.credit_card.statement import compute_statement

LEDGER_A = "led-a"
LEDGER_B = "led-b"
CARD = "acc-card"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TS() as session:
        yield session


def _account(db, *, sync_id=CARD, account_type="credit_card", initial=0.0,
             billing_day=10, payment_due_day=25):
    db.add(UserAccountProjection(
        user_id="u1", sync_id=sync_id, name=sync_id,
        account_type=account_type, currency="JPY",
        initial_balance=initial, billing_day=billing_day,
        payment_due_day=payment_due_day, source_change_id=1,
    ))


def _tx(db, *, ledger_id, amount, when: date, account=CARD):
    db.add(ReadTxProjection(
        ledger_id=ledger_id, sync_id=f"tx-{ledger_id}-{amount}",
        user_id="u1", tx_type="expense", amount=amount,
        account_sync_id=account, happened_at=datetime(
            when.year, when.month, when.day, tzinfo=timezone.utc),
        source_change_id=1,
    ))


def _calc(db, *, period, ledger_ids):
    return compute_statement(
        db, user_id="u1", ledger_ids=ledger_ids,
        card_account_sync_id=CARD, period=period, today=period.statement_date,
    )


# --------------------------------------------------------------------------- #
# 算账:全部账本                                                              #
# --------------------------------------------------------------------------- #


def test_bill_spans_both_ledgers(db) -> None:
    """同一张卡在两个账本的消费必须都进账单。"""
    _account(db)
    period = current_billing_period(date(2026, 10, 25), billing_day=10,
                                     due_day=25)
    assert period.statement_date == date(2026, 11, 10)

    _tx(db, ledger_id=LEDGER_A, amount=3000.0, when=date(2026, 10, 20))
    _tx(db, ledger_id=LEDGER_B, amount=2000.0, when=date(2026, 10, 25))
    db.commit()

    stmt = _calc(db, period=period, ledger_ids=[LEDGER_A, LEDGER_B])
    assert stmt.bill == 5000.0, (
        f"账本 B 的 2000 没算进来 —— 少还 2000 而不报错。bill={stmt.bill}")
    assert stmt.outstanding == 5000.0

    # 反证:只给一个账本时确实只有那一个。
    # 没有这条,上面的 5000 可能只是"两本都查了全表"的巧合。
    only_a = _calc(db, period=period, ledger_ids=[LEDGER_A])
    assert only_a.bill == 3000.0, only_a.bill


def test_all_ledger_ids_returns_every_ledger_of_the_user(db) -> None:
    """`_all_ledger_ids` 必须给出该用户的全部账本,且不含别人的。"""
    for lid in (LEDGER_A, LEDGER_B):
        db.add(Ledger(id=lid, user_id="u1", external_id=lid, name=lid,
                      currency="JPY", created_at=datetime.now(timezone.utc)))
    # 别的用户的账本不能混进来
    db.add(Ledger(id="led-other", user_id="u2", external_id="lo", name="lo",
                  currency="JPY", created_at=datetime.now(timezone.utc)))
    db.commit()

    ids = _all_ledger_ids(db, user_id="u1")
    assert set(ids) == {LEDGER_A, LEDGER_B}, ids


def test_empty_ledger_list_means_no_debt(db) -> None:
    """没有账本时不能炸,也不能算出个正数账单。"""
    _account(db)
    period = current_billing_period(date(2026, 10, 25), billing_day=10,
                                     due_day=25)
    stmt = _calc(db, period=period, ledger_ids=[])
    assert stmt.bill == 0.0, stmt.bill
    assert stmt.outstanding == 0.0


def test_split_legs_span_ledgers_too(db) -> None:
    """组合支付的腿也要跨账本 —— 腿表的 ledger_id 是**父交易**那个。"""
    from src.models import ReadTxSplitProjection

    _account(db)
    period = current_billing_period(date(2026, 10, 25), billing_day=10,
                                     due_day=25)

    # 账本 A 一笔普通消费
    _tx(db, ledger_id=LEDGER_A, amount=1000.0, when=date(2026, 10, 20))
    # 账本 B 一笔组合支付:2000 全在腿上,父交易 account_sync_id 为空
    db.add(ReadTxProjection(
        ledger_id=LEDGER_B, sync_id="tx-split", user_id="u1",
        tx_type="expense", amount=2000.0, account_sync_id=None,
        happened_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
        source_change_id=1,
    ))
    db.add(ReadTxSplitProjection(
        ledger_id=LEDGER_B, tx_sync_id="tx-split", seq=0,
        account_sync_id=CARD, amount=2000.0,
    ))
    db.commit()

    stmt = _calc(db, period=period, ledger_ids=[LEDGER_A, LEDGER_B])
    assert stmt.bill == 3000.0, (
        f"账本 B 腿上的 2000 漏了 —— 规则 2 在多账本下也必须是跨账本的。"
        f"bill={stmt.bill}")
