"""`/workspace/accounts` 的「本期应还」展示字段。

## 为什么用户需要它

`compute_statement` 此前只被 `repay_one` 调用过一次 —— 用来决定扣多少,
**没有任何出口给用户**。详情弹窗只有「账单日 X / 还款日 Y / 还有 N 天」
这类静态日号,没有一个金额。

用户在还款日之前**完全不知道这期要还多少**。绑了自动还款之后,
「会自动扣多少」也是同一笔钱,同样看不到。

## 两个容易写错的点

1. **必须能在非还款日算出来**。`period_for_payment_due_date` 是给执行器
   用的(它先 `is_due_on` 确认今天是还款日),对非还款日会直接 raise。
   展示用 `current_billing_period` —— 正向的,任何一天都能算。
   第一版用错了,非还款日全部算不出。
2. **必须跨全部账本**(与 `repay.py` 同一口径)。账户是 user-global,
   第二個账本的消费也要进账单。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models import Ledger, ReadTxProjection, UserAccountProjection
from src.routers.read.workspace import _statement_out

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


def _account(db, *, account_type="credit_card", billing_day=10,
             payment_due_day=25):
    db.add(UserAccountProjection(
        user_id="u1", sync_id=CARD, name="Card", account_type=account_type,
        currency="JPY", initial_balance=0.0, billing_day=billing_day,
        payment_due_day=payment_due_day, source_change_id=1,
    ))


def _spend(db, *, ledger_id, sync_id, amount, when):
    db.add(ReadTxProjection(
        ledger_id=ledger_id, sync_id=sync_id, user_id="u1",
        tx_type="expense", amount=amount, account_sync_id=CARD,
        happened_at=datetime(when.year, when.month, when.day,
                             tzinfo=timezone.utc),
        source_change_id=1,
    ))


class _FakeUser:
    id = "u1"


def test_non_due_day_still_shows_the_amount(db) -> None:
    """**非还款日**也必须算出「本期应还」。

    `period_for_payment_due_date` 对非还款日会 raise(它是给执行器用的,
    执行器先 `is_due_on` 确认今天是还款日)。展示要用
    `current_billing_period` —— 正向的,任何一天都能算。

    第一版用错了,于是用户在还款日之前**永远看不到金额**,
    而那正是他最需要看的时候。
    """
    _account(db)
    db.add(Ledger(id="led", user_id="u1", external_id="d", name="D",
                  currency="JPY"))
    db.commit()
    # bd=10 / dd=25:10/11 时本期窗口 = (10/10, 11/10](见
    # `test_known_values`:10/11 → statement_date 11/10)。
    # ⚠️ `statement_date` 是窗口**右端**(下一个账单日),不是已出的那个 ——
    # 这个命名有歧义,消费要落在窗口内才是本期的。
    _spend(db, ledger_id="led", sync_id="t1", amount=4000.0,
           when=date(2026, 10, 20))
    db.commit()

    out = _statement_out(db, user_id=_FakeUser(),
                         acct=db.scalar(select(UserAccountProjection).where(
                             UserAccountProjection.sync_id == CARD)))
    assert out.bill == 4000.0, out.bill
    assert out.outstanding == 4000.0
    # 还款日仍给出,让界面能显示「X 号还」
    assert out.next_due == date(2026, 11, 25).isoformat(), out.next_due


def test_not_a_credit_card_returns_all_none(db) -> None:
    """非信用卡不算 —— 否则每张卡都扫一遍累计查询,把接口拖慢。"""
    _account(db, account_type="bank_card")
    db.commit()
    out = _statement_out(db, user_id=_FakeUser(),
                         acct=db.scalar(select(UserAccountProjection).where(
                             UserAccountProjection.sync_id == CARD)))
    assert out.bill is None and out.outstanding is None and out.next_due is None


def test_missing_due_day_returns_all_none(db) -> None:
    """没填还款日的卡不算 —— 没有「这期」的概念。"""
    _account(db, payment_due_day=0)
    db.commit()
    out = _statement_out(db, user_id=_FakeUser(),
                         acct=db.scalar(select(UserAccountProjection).where(
                             UserAccountProjection.sync_id == CARD)))
    assert out.bill is None and out.next_due is None


def test_no_debt_gives_zero_not_none(db) -> None:
    """没欠款时给 **0**,不是 None。

    None 的语义是「不算」,0 的语义是「这期不用还」。界面要能区分:
    「¥0(已还清)」和「不显示」给用户的信息完全不同。
    """
    _account(db)
    db.commit()
    out = _statement_out(db, user_id=_FakeUser(),
                         acct=db.scalar(select(UserAccountProjection).where(
                             UserAccountProjection.sync_id == CARD)))
    assert out.bill == 0.0, out.bill
    assert out.outstanding == 0.0
    assert out.next_due is not None


def test_spans_all_ledgers_of_the_user(db) -> None:
    """跨账本聚合 —— 与 `repay.py` 同一口径,两边必须一致。"""
    _account(db)
    db.add(Ledger(id="led-a", user_id="u1", external_id="a", name="A",
                  currency="JPY"))
    db.add(Ledger(id="led-b", user_id="u1", external_id="b", name="B",
                  currency="JPY"))
    db.commit()
    _spend(db, ledger_id="led-a", sync_id="t1", amount=3000.0,
           when=date(2026, 10, 20))
    _spend(db, ledger_id="led-b", sync_id="t2", amount=2000.0,
           when=date(2026, 10, 22))
    db.commit()

    out = _statement_out(db, user_id=_FakeUser(),
                         acct=db.scalar(select(UserAccountProjection).where(
                             UserAccountProjection.sync_id == CARD)))
    assert out.bill == 5000.0, (
        f"第二个账本的 2000 没算进来。bill={out.bill}")
