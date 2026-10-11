"""自动还款**真实写路径**的端到端测试。

## 为什么单独一个文件

`tests/test_credit_autorepay_e2e.py` 的 `WriteThrough` 夹具**直接
`wdb.add(ReadTxProjection(...))`** —— 它绕过了:

- `_commit_write` 的整套链路(tx_id 生成 / `snapshot_mutator` 校验 /
  `projection.upsert_tx` / rename cascade)
- `sync_changes` 事件流(增量同步的源头)
- `audit_logs`
- `sync_push_idempotency`(幂等键落库)
- WS 广播

也就是说 **`selfcall.py` 那条唯一会动钱的链路,此前零自动化覆盖**。
而 CLAUDE.md 记录的这个功能两轮「测试全绿但跑不通」,第二轮的三个根因
(`asyncio.run()` 在运行中的 loop 里炸、selfcall 连错三次、失败被吞成
HTTP 200)**全都在这条链路上**。

## 本文件测什么

用**真的** `make_self_call()` 打**真的** ASGI app(ASGITransport,不走
TCP),断言钱真的按预期动了:

1. 库里多出一笔 `transfer`,`from`/`to` 两端正确
2. 金额正确(不是 0,不是父交易总额)
3. `sync_changes` / `audit_logs` 都落了行
4. 幂等键里**带金额**(防线 3 的关键属性)
5. 第二次相同调用返回 **replay**,不产生新行
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.models import (
    AuditLog,
    ReadTxProjection,
    SyncChange,
    SyncPushIdempotency,
    UserAccountProjection,
)
from src.services.credit_card.repay import repay_one
from src.services.credit_card.selfcall import call_async, make_self_call

LEDGER = "lg-money"


@pytest.fixture
def env():
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
    client = TestClient(app)
    try:
        yield client, TS
    finally:
        app.dependency_overrides.clear()


def _register(client: TestClient, email: str) -> dict[str, str]:
    r = client.post("/api/v1/auth/register", json={
        "email": email, "password": "MoneyTest1!x", "client_type": "web",
        "device_name": "d", "platform": "test",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}",
            "X-Device-ID": "d", "Content-Type": "application/json"}


def _balances(client: TestClient, H: dict[str, str]) -> dict[str, float]:
    return {a["name"]: a["balance"] for a in
            client.get("/api/v1/read/workspace/accounts", headers=H,
                       params={"ledger_id": LEDGER}).json()}


@pytest.fixture
def money_env(env):
    """账本 + 信用卡 + 储蓄卡,并绑好自动还款。

    ⚠️ Savings 的 `initial_balance` 在创建时一次给足 100000 —— 别像第一版
    那样先按循环里的默认 0 建一遍、再用 100000 补一次:第二次 POST 被
    幂等挡掉,余额停在 0,于是所有用例都 `skipped_no_funds`,
    **看起来像功能坏了,其实是夹具建了两遍账**。
    """
    client, TS = env
    H = _register(client, "money@t.com")
    assert client.post("/api/v1/write/ledgers", headers=H, json={
        "ledger_id": LEDGER, "ledger_name": "Daily", "currency": "JPY"}
    ).status_code == 200

    assert client.post(f"/api/v1/write/ledgers/{LEDGER}/accounts",
                       headers=H, json={
                           "base_change_id": 0, "name": "Card",
                           "account_type": "credit_card", "initial_balance": 0.0,
                           "billing_day": 4, "payment_due_day": 4,
                           "credit_limit": 200000.0}).status_code == 200
    assert client.post(f"/api/v1/write/ledgers/{LEDGER}/accounts",
                       headers=H, json={
                           "base_change_id": 0, "name": "Savings",
                           "account_type": "bank_card",
                           "initial_balance": 100000.0}).status_code == 200

    accs = client.get("/api/v1/read/workspace/accounts", headers=H,
                      params={"ledger_id": LEDGER}).json()
    ids = {a["name"]: a["id"] for a in accs}
    r = client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['Card']}",
                     headers=H, json={"base_change_id": 0,
                                      "autorepay_enabled": True,
                                      "autorepay_from_account_id":
                                          ids["Savings"]})
    assert r.status_code == 200, r.text
    return client, TS, H, ids


# --------------------------------------------------------------------------- #
# 1. 钱真的按预期动了                                                        #
# --------------------------------------------------------------------------- #


def test_real_self_call_moves_the_money(money_env) -> None:
    """真 `make_self_call()` → 真 `_commit_write` → 库里真的多一笔 transfer。"""
    client, TS, H, ids = money_env

    # 记一笔消费:让卡欠 8000
    assert client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions",
                       headers=H, json={
                           "base_change_id": 0, "tx_type": "expense",
                           "amount": 8000.0,
                           "happened_at": "2026-10-04T09:00:00+00:00",
                           "account_id": ids["Card"], "category_name": "Shop",
                           "category_kind": "expense"}).status_code == 200

    before = _balances(client, H)
    assert before["Card"] == pytest.approx(-8000.0), before

    # 用**真的** self_call 跑执行器(同步上下文 → make_self_call)
    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["Card"]))
        out = repay_one(db, user_id=card.user_id, card=card,
                        today=datetime(2026, 10, 4).date(),
                        self_call=make_self_call())

    assert out.status == "done", out
    assert out.amount == 8000.0, out

    after = _balances(client, H)
    assert after["Card"] == pytest.approx(0.0), f"卡没清零:{after}"
    assert after["Savings"] == pytest.approx(92000.0), f"储蓄卡没扣:{after}"


def test_real_path_creates_transfer_with_both_ends(money_env) -> None:
    """落库的那笔必须是 `transfer` 且 from/to 都填对。"""
    client, TS, H, ids = money_env
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-10-04T09:00:00+00:00",
        "account_id": ids["Card"], "category_name": "Shop",
        "category_kind": "expense"})

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["Card"]))
        repay_one(db, user_id=card.user_id, card=card,
                  today=datetime(2026, 10, 4).date(),
                  self_call=make_self_call())

    with TS() as db:
        rows = db.scalars(select(ReadTxProjection).where(
            ReadTxProjection.tx_type == "transfer")).all()
    assert len(rows) == 1, f"应有 1 笔 transfer,实际 {len(rows)} 笔"
    row = rows[0]
    assert row.from_account_sync_id == ids["Savings"], (
        "转出端应是储蓄卡 —— 方向反了意味着**卡反而被多扣一次**")
    assert row.to_account_sync_id == ids["Card"], "转入端应是信用卡"
    assert float(row.amount) == 5000.0, row.amount
    assert (row.note or "").startswith("自动还款"), (
        "备注是用户在交易列表里认出这笔的唯一线索")


def test_real_path_writes_sync_change_and_audit_log(money_env) -> None:
    """`sync_changes` + `audit_logs` 都必须落行。

    这两个表是**别的客户端**拿到这笔钱的唯一途径 —— mobile 靠
    `sync_changes` 增量同步,admin 靠 `audit_logs` 追溯。`WriteThrough`
    夹具两个都不写,所以它们此前的状态完全是未经验证的。
    """
    client, TS, H, ids = money_env
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-10-04T09:00:00+00:00",
        "account_id": ids["Card"], "category_name": "Shop",
        "category_kind": "expense"})

    with TS() as db:
        before_changes = db.scalar(select(func.count()).select_from(SyncChange))
        before_audit = db.scalar(select(func.count()).select_from(AuditLog))

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["Card"]))
        repay_one(db, user_id=card.user_id, card=card,
                  today=datetime(2026, 10, 4).date(),
                  self_call=make_self_call())

    with TS() as db:
        after_changes = db.scalar(select(func.count()).select_from(SyncChange))
        after_audit = db.scalar(select(func.count()).select_from(AuditLog))

    assert after_changes > before_changes, (
        "sync_changes 没落行 —— 其它设备/App 拿不到这笔自动还款")
    assert after_audit > before_audit, (
        "audit_logs 没落行 —— admin 面板里这笔动钱的操作无迹可查")


def test_real_path_stores_idempotency_key_with_amount(money_env) -> None:
    """`sync_push_idempotency` 里必须有一条带金额的键。

    防线 3 的幂等键是 `auto-repay:{card}:{period}:{amount}`。服务端会把
    payload 一起 hash,**同键不同金额 → 409**。少了金额会把这一期卡死。
    """
    client, TS, H, ids = money_env
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-10-04T09:00:00+00:00",
        "account_id": ids["Card"], "category_name": "Shop",
        "category_kind": "expense"})

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["Card"]))
        repay_one(db, user_id=card.user_id, card=card,
                  today=datetime(2026, 10, 4).date(),
                  self_call=make_self_call())

    with TS() as db:
        keys = [k for (k,) in db.execute(
            select(SyncPushIdempotency.idempotency_key)).all()]
    auto = [k for k in keys if k.startswith("auto-repay:")]
    assert auto, f"没有落任何自动还款幂等键:{keys}"
    assert any(k.endswith(":5000.00") for k in auto), (
        f"幂等键里没有金额:{auto} —— 同键不同金额会撞 409 把这一期卡死")


def test_real_path_is_idempotent_on_repeat(money_env) -> None:
    """第二次相同调用必须 replay,不产生新行。"""
    client, TS, H, ids = money_env
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-10-04T09:00:00+00:00",
        "account_id": ids["Card"], "category_name": "Shop",
        "category_kind": "expense"})

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["Card"]))
        first = repay_one(db, user_id=card.user_id, card=card,
                          today=datetime(2026, 10, 4).date(),
                          self_call=make_self_call())
        # 同一账期重跑一次(模拟回看窗口撞上同一期)
        second = repay_one(db, user_id=card.user_id, card=card,
                           today=datetime(2026, 10, 4).date(),
                           self_call=make_self_call())

    assert first.status == "done", first
    assert second.status in ("skipped_already_repaid",
                             "skipped_zero_outstanding"), second

    with TS() as db:
        n_transfers = db.scalar(select(func.count()).select_from(
            ReadTxProjection).where(ReadTxProjection.tx_type == "transfer"))
    assert n_transfers == 1, f"产生了 {n_transfers} 笔 transfer —— 重复扣钱"


def test_real_path_via_async_context(money_env) -> None:
    """**async 上下文**(FastAPI 端点)也必须跑得通。

    这两个上下文的处境相反:APScheduler 线程里没有 loop,端点里**有**。
    第一版只在同步上下文里验证过,端点上第一次调用就炸
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。
    """
    client, TS, H, ids = money_env
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 3000.0,
        "happened_at": "2026-10-04T09:00:00+00:00",
        "account_id": ids["Card"], "category_name": "Shop",
        "category_kind": "expense"})

    # 用 async 的 self_call,并**在已有事件循环里**调 repay_one ——
    # 这正是 FastAPI 端点的处境。
    async def in_running_loop():
        with TS() as db:
            card = db.scalar(select(UserAccountProjection).where(
                UserAccountProjection.sync_id == ids["Card"]))
            return repay_one(db, user_id=card.user_id, card=card,
                             today=datetime(2026, 10, 4).date(),
                             self_call=call_async)

    out = asyncio.run(in_running_loop())
    assert out.status == "done", out
    assert out.amount == 3000.0, out

    after = _balances(client, H)
    assert after["Card"] == pytest.approx(0.0), after
    assert after["Savings"] == pytest.approx(97000.0), after
