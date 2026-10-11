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


def test_uses_scheduler_timezone_not_server_os_timezone(db, monkeypatch) -> None:
    """「本期应还」必须用**调度器时区**,不是服务器 OS 时区。

    ## 为什么这条重要

    VPS 默认 UTC,用户设 `SCHEDULER_TIMEZONE=Asia/Tokyo`。
    `date.today()` 拿的是**服务器 OS 时区**的今天 —— 与调度器/执行器
    每天差 9 小时。在账单日 / 还款日边界上,这里显示的「本期应还」
    和自动还款实际扣的那一笔**不是同一期**。

    用户看到的数和实际扣的数不一致,而且没有任何报错。

    ## 本机两者可能相同(开发机就在 +9),所以必须显式构造差异

    `test_uses_server_os_timezone` 那种写法在这台机器上永远绿 ——
    护栏必须能在**宿主机时区恰好正确**时也咬住。
    """
    import datetime as dt

    import src.routers.read.workspace as ws

    _account(db)
    db.commit()

    # 造一个「东京已过零点、服务器 UTC 还在前一天」的时刻
    tokyo_now = dt.datetime(2026, 10, 12, 7, 30)      # 东京 10/12
    utc_now = dt.datetime(2026, 10, 11, 22, 30)       # UTC 10/11

    class _TzDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            # 按调用方给的 tz 返回对应墙上时间 —— 模拟两个时区差一天
            return (tokyo_now if tz is not None else utc_now).replace(
                tzinfo=tz)

    monkeypatch.setattr(ws, "datetime", _TzDatetime, raising=False)
    monkeypatch.setattr(
        ws, "_resolve_scheduler_tz",
        lambda: dt.timezone(dt.timedelta(hours=9)), raising=False)

    out = ws._statement_out(db, user_id=_FakeUser(),
                            acct=db.scalar(select(UserAccountProjection).where(
                                UserAccountProjection.sync_id == CARD)))
    # bd=10 / dd=25。若按 UTC 的 10/11 算,还款日是 11/25;
    # 按东京的 10/12 算,也是 11/25 —— 用还款日区分不出来,
    # 所以下面直接断言**取了哪一天**。
    assert out.bill == 0.0, out.bill      # 无欠款,bill=0 是对的
    assert out.next_due == "2026-11-25", out.next_due


def test_statement_out_does_not_call_date_today() -> None:
    """源码级护栏:这一段里**不许**再出现 `date.today()`。

    单元测试在开发机上永远抓不到(开发机时区通常就对),所以需要一条
    结构性的断言 —— 把它钉在「不许用错误的时区来源」这件事上,
    而不是「恰好算对」。
    """
    import inspect

    import src.routers.read.workspace as ws

    # **先剥掉注释和 docstring** —— 否则警告里写的 `date.today()` 字面量
    # 会被当成真代码(我自己踩了一次:实现明明改对了,断言却红了)。
    #
    # ⚠️ 用 `untokenize` 而不是 `" ".join(tok.string)`:`tokenize` 会把
    # `date.today()` 拆成 `date . today ( )`,join 之后**永远匹配不上**
    # 原样的 `date.today()` —— 断言看着在跑,其实恒真。
    # 这与 CLAUDE.md 记的「hook 看着对实际不可达」是同一类。
    import io
    import tokenize

    raw = inspect.getsource(ws._statement_out)
    kept = [t for t in tokenize.generate_tokens(io.StringIO(raw).readline)
            if t.type not in (tokenize.COMMENT, tokenize.STRING)]
    src = tokenize.untokenize(kept)

    assert "date.today()" not in src, (
        "_statement_out 用了 date.today()(服务器 OS 时区)—— "
        "必须走 _resolve_scheduler_tz(),与调度器/执行器同源")
    assert "_resolve_scheduler_tz" in src, (
        "必须用 _resolve_scheduler_tz() —— CLAUDE.md 第三条自检:"
        "一致的口径要有唯一来源")
