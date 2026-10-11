"""备份/还原的**数据**往返 —— 组合支付的腿必须活下来。

## 为什么这条不可省

`CLAUDE.md` 把「`_truncate_ledger` 漏子表 → rebuild / 还原后父交易没腿、
旧腿还在 → 余额**凭空多出来**」列为组合支付的静默丢失之一。

而 `tests/test_backup_restore.py` 里 11 条测的**全是状态文件管理**
(`list_restores` / `cleanup` / `status.json` 读法),
`grep -c "split|autorepay|tax_amount"` = **0**。

也就是说:**还原的真实数据路径没有任何覆盖**。

代码目前是对的(我实测过),但「当前对」和「改坏了有人拦」是两件事。
这正是 `CLAUDE.md` 反复记的那个形状:每次都**事后**补测试,
而下一列被加进来时同样的事会再发生一次。

## 覆盖

`rebuild_from_snapshot` 是 `admin restore_backup` 走的同一条路
(`snapshot_builder.build` -> `_truncate_ledger` -> `upsert_tx`):

- `snapshot_builder.build` 必须把腿**写进** snapshot(否则还原后腿没了)
- `_truncate_ledger` 必须清掉**旧腿**(否则残留腿和重建腿叠加,
  余额「凭空多出来」)
- 有腿时父交易 `account_name` 必须仍为空(0021 不变式,
  否则余额双倍扣)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def TS():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    from src.models import Base
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def _seed_split_tx(db, ledger_id="lg"):
    from src.models import Ledger, ReadTxProjection, ReadTxSplitProjection

    db.add(Ledger(id=ledger_id, user_id="u1", external_id="ext",
                  name="L", currency="JPY"))
    db.add(ReadTxProjection(
        sync_id="tx1", ledger_id=ledger_id, user_id="u1",
        tx_type="expense", amount=5000.0, note="便利店",
        # 有腿时父交易的账户列必须为空(0021 不变式)
        account_sync_id=None, account_name=None,
        category_name="餐饮",
        happened_at=datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)))
    db.add(ReadTxSplitProjection(ledger_id=ledger_id, tx_sync_id="tx1", seq=0,
                                 account_sync_id="a1", amount=3000.0))
    db.add(ReadTxSplitProjection(ledger_id=ledger_id, tx_sync_id="tx1", seq=1,
                                 account_sync_id="a2", amount=2000.0))
    db.commit()


def test_snapshot_builder_emits_splits(TS) -> None:
    """snapshot 必须把腿写进去 —— 否则还原后父交易变成「无账户的支出」。"""
    from src import snapshot_builder
    from src.models import Ledger

    with TS() as db:
        _seed_split_tx(db)
        snap = snapshot_builder.build(db, db.get(Ledger, "lg"))

    items = snap.get("items") or []
    assert len(items) == 1, f"交易没进 snapshot:{items}"
    legs = items[0].get("splits")
    assert legs, (
        "snapshot 里没有 splits —— 还原后组合支付会变成「无账户的支出」,"
        "两个账户的余额全错,且不报错。"
        f"(实际字段:{sorted(items[0])})")


def test_rebuild_restores_the_legs_exactly_once(TS) -> None:
    """build -> truncate -> rebuild:腿必须**恰好** 2 条。

    「恰好」是重点 —— 多出来就是 `CLAUDE.md` 记的「余额凭空多出来」。
    """
    from src import snapshot_builder
    from src.models import Ledger, ReadTxProjection, ReadTxSplitProjection
    from src.projection import rebuild_from_snapshot

    with TS() as db:
        _seed_split_tx(db)
        snap = snapshot_builder.build(db, db.get(Ledger, "lg"))

    with TS() as db:
        rebuild_from_snapshot(db, ledger_id="lg", user_id="u1",
                              snapshot=snap, source_change_id=1)
        db.commit()

    with TS() as db:
        legs = db.scalars(
            select(ReadTxSplitProjection)
            .where(ReadTxSplitProjection.ledger_id == "lg")
            .order_by(ReadTxSplitProjection.seq)).all()

    got = sorted((x.account_sync_id, round(float(x.amount), 6)) for x in legs)
    assert got == [("a1", 3000.0), ("a2", 2000.0)], (
        f"还原后腿是 {got},期望恰好 [('a1',3000.0),('a2',2000.0)]。"
        f"多了 = `_truncate_ledger` 没清旧腿(余额凭空多出来);"
        f"少了 = snapshot 丢了 splits。")


def test_rebuild_keeps_parent_account_empty_when_split(TS) -> None:
    """还原后父交易 `account_name` 仍为空 —— 否则余额双倍扣(0021 不变式)。"""
    from src import snapshot_builder
    from src.models import Ledger, ReadTxProjection
    from src.projection import rebuild_from_snapshot

    with TS() as db:
        _seed_split_tx(db)
        snap = snapshot_builder.build(db, db.get(Ledger, "lg"))

    with TS() as db:
        rebuild_from_snapshot(db, ledger_id="lg", user_id="u1",
                              snapshot=snap, source_change_id=1)
        db.commit()
        row = db.scalar(select(ReadTxProjection)
                        .where(ReadTxProjection.sync_id == "tx1"))

    assert row.account_sync_id is None and row.account_name is None, (
        f"还原后父交易带上了账户列(account_name={row.account_name!r})"
        f" —— 有腿的父交易带账户列会让余额**双倍扣**")


def test_truncate_clears_the_split_subtable(TS) -> None:
    """`_truncate_ledger` 必须清子表 —— 这条是上面「恰好 2 条」的根因。"""
    from src.models import ReadTxSplitProjection
    from src.projection import _truncate_ledger

    with TS() as db:
        _seed_split_tx(db)
        _truncate_ledger(db, "lg")
        db.commit()
        left = db.scalars(select(ReadTxSplitProjection)).all()

    assert left == [], (
        f"_truncate_ledger 漏了子表,还剩 {len(left)} 条 —— "
        f"CLAUDE.md 记的「还原后余额凭空多出来」。")