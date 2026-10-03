"""账户余额的两条计算路径必须给出一致结果。

## 为什么需要

余额在本仓有**两种算法形态**:

- `account_balance_stats` —— SQL 聚合(`GROUP BY account_sync_id`),给账户
  列表 / MCP `get_account_balance` / 首页用
- `account_balance_delta` —— Python 逐笔,给净值历史按时间推进用

两者由人分别维护。之前它们是**三份各自独立的实现**(workspace 的 SQL、
MCP 的重复 SQL、净值历史的 `_apply`),只有 workspace 和 MCP 那两份在
注释里互相承诺「口径逐字对应」—— 没有任何机制强制。

组合支付(0021)会同时改这两条路径:一笔支出拆成多条腿,聚合要 UNION 子表、
逐笔要展开 split。这正是最容易「改一条忘一条」的场合,而漏改的表现是
**账户页显示的余额和净值曲线上的余额不一样** —— 两者各自的单测都绿。

所以这里直接断言:**同一批数据,两条路径算出的每个账户余额必须相等。**

## 为什么不能只靠现有测试

`test_account_hidden_sync.py` / `test_mcp_entity_tools.py` /
`test_net_worth_history.py` 各自测各自端点、各自断言具体数字,没有任何一条
把两个端点放在一起比。拆分之后它们仍然会各自通过。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.routers.read._shared import (
    account_balance_delta,
    account_balance_from_stats,
    account_balance_stats,
)


def _make_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app), TS


def _seed(client: TestClient):
    r = client.post("/api/v1/auth/register", json={
        "email": "balpaths@t.com", "password": "BalPaths1!x",
        "client_type": "web", "device_name": "d", "platform": "test",
    })
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    H = {"Authorization": f"Bearer {tok}", "X-Device-ID": "d",
         "Content-Type": "application/json"}
    r = client.post("/api/v1/write/ledgers", headers=H,
                    json={"ledger_id": "lg1", "ledger_name": "L", "currency": "JPY"})
    assert r.status_code == 200, r.text
    return H


def _accounts(client: TestClient, H) -> dict[str, str]:
    rows = client.get("/api/v1/read/workspace/accounts", headers=H).json()
    return {a["name"]: a["id"] for a in rows}


def _tx(client, H, **kw):
    # 注意是 `tx_type` 不是 `type` —— schema 用 extra='ignore',写错会静默变成 expense。
    # 我第一版就写错了,而测试照样绿(两边都在比 expense),自检才抓到。
    payload = {"base_change_id": 0, "tx_type": "expense", "happened_at":
               "2026-10-03T12:00:00+00:00"}
    payload.update(kw)
    r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=H, json=payload)
    assert r.status_code == 200, r.text
    return r.json().get("entity_id")


def _at(d):
    return f"2026-10-{d:02d}T12:00:00+00:00"


# --------------------------------------------------------------------------- #
# 核心不变式                                                                  #
# --------------------------------------------------------------------------- #


def test_aggregate_and_per_transaction_balances_agree():
    """**SQL 聚合 == 逐笔 delta** —— 逐账户比对。

    覆盖 income / expense / transfer 三种交易,以及「转账两端同一账户」
    「金额 0」「交易金额为负」这些边界。
    """
    client, TS = _make_client()
    try:
        H = _seed(client)
        client.post("/api/v1/write/ledgers/lg1/accounts", headers=H, json={
            "base_change_id": 0, "name": "工资卡", "account_type": "bank_card",
            "initial_balance": 100000.0})
        client.post("/api/v1/write/ledgers/lg1/accounts", headers=H, json={
            "base_change_id": 0, "name": "现金", "account_type": "cash",
            "initial_balance": 30000.0})
        client.post("/api/v1/write/ledgers/lg1/accounts", headers=H, json={
            "base_change_id": 0, "name": "信用卡", "account_type": "credit_card"})

        ids = _accounts(client, H)
        for d, spec in (
            (1, {"tx_type": "income", "amount": 385000.0, "account_name": "工资卡"}),
            (2, {"tx_type": "expense", "amount": 3280.0, "account_name": "现金",
                 "happened_at": _at(2)}),
            (3, {"tx_type": "expense", "amount": 118000.0, "account_name": "工资卡",
                 "happened_at": _at(3)}),
            (4, {"tx_type": "transfer", "amount": 5000.0,
                 "from_account_name": "工资卡", "to_account_name": "信用卡",
                 "happened_at": _at(4)}),
            # 转回
            (5, {"tx_type": "transfer", "amount": 2000.0,
                 "from_account_name": "信用卡", "to_account_name": "工资卡",
                 "happened_at": _at(5)}),
            # 转给自己(两端同一账户)—— 应净 0
            (6, {"tx_type": "transfer", "amount": 800.0,
                 "from_account_name": "现金", "to_account_name": "现金",
                 "happened_at": _at(6)}),
            # 金额 0
            (7, {"tx_type": "expense", "amount": 0.0, "account_name": "现金",
                 "happened_at": _at(7)}),
        ):
            _tx(client, H, **spec)

        ledger_internal = None
        with TS() as db:
            from sqlalchemy import select
            from src.models import Ledger
            ledger_internal = db.scalar(
                select(Ledger).where(Ledger.external_id == "lg1")).id

            # 路径 A:SQL 聚合
            stats = account_balance_stats(db, [ledger_internal])
            from src.models import UserAccountProjection
            accts = db.scalars(
                select(UserAccountProjection)).all()
            agg = {
                a.sync_id: account_balance_from_stats(
                    str(a.sync_id), float(a.initial_balance or 0.0), stats)
                for a in accts
            }

            # 路径 B:逐笔 delta
            from src.models import ReadTxProjection
            rows = db.scalars(
                select(ReadTxProjection).order_by(
                    ReadTxProjection.happened_at.asc())
            ).all()
            deltas: dict[str, float] = {}
            for row in rows:
                for sid, d in account_balance_delta(
                    row.tx_type, float(row.amount or 0.0),
                    row.account_sync_id, row.from_account_sync_id,
                    row.to_account_sync_id,
                ).items():
                    deltas[sid] = deltas.get(sid, 0.0) + d
            per = {
                a.sync_id: float(a.initial_balance or 0.0) + deltas.get(str(a.sync_id), 0.0)
                for a in accts
            }

        # **先自检**:转账两端若没解析出 sync_id,下面那个比对会因为两条路径
        # 都跳过 src 而「恰好一致」—— 护栏变成空跑。第一版就栽在这:
        # 断言一直绿,改坏 account_balance_delta 的转账符号也没抓到。
        transfer_rows = [r for r in rows if r.tx_type == "transfer"]
        assert transfer_rows, "没有转账数据,比对照不到转账路径"
        assert all(r.from_account_sync_id for r in transfer_rows), (
            "转账的 from_account_sync_id 没解析出来,比对无效"
        )
        assert all(r.to_account_sync_id for r in transfer_rows), (
            "转账的 to_account_sync_id 没解析出来,比对无效"
        )

        assert agg == per, {
            k: {"sql_aggregate": agg[k], "per_tx_delta": per.get(k)}
            for k in agg if agg[k] != per.get(k)
        }
    finally:
        app.dependency_overrides.clear()


def test_transfer_to_self_is_net_zero():
    """转出转入同一账户 → 净 0(但 count +2)。"""
    from src.routers.read._shared import account_balance_delta
    d = account_balance_delta("transfer", 800.0, None, "现金", "现金")
    assert abs(sum(d.values())) < 1e-9, d


def test_unknown_tx_type_has_no_effect():
    from src.routers.read._shared import account_balance_delta
    assert account_balance_delta("recurring", 100.0, "a") == {}
    assert account_balance_delta(None, 100.0, "a") == {}


def test_zero_amount_is_noop():
    from src.routers.read._shared import account_balance_delta
    assert account_balance_delta("expense", 0.0, "a") == {}


def test_transfer_missing_from_falls_back_to_primary_account():
    """老数据可能只有主账户没有 from/to —— 转出应退回主账户,不能丢。"""
    from src.routers.read._shared import account_balance_delta
    d = account_balance_delta("transfer", 500.0, "工资卡", None, None)
    assert d == {"工资卡": -500.0}, d


def test_account_balance_from_stats_missing_row_returns_initial():
    from src.routers.read._shared import account_balance_from_stats
    assert account_balance_from_stats("nope", 1234.0, {}) == 1234.0
    assert account_balance_from_stats("nope", 1234.0, None) == 1234.0
