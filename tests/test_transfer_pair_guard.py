"""转账两端齐备的契约(阶段 0,自动还款的前置修复)。

## 为什么这条要先修

自动还款每期都会产生一笔「扣款账户 → 信用卡」的 transfer。如果 transfer
可以**单边落地**,那么每期都会从扣款账户扣钱、却不给信用卡入账 —— **钱凭空消失**。

## 三个现存缺陷

1. **`projection.py` 三组账户字段各自独立按名反查**(`:280-290`)。只带名
   不带 id 时,`from_account_name` 命中、`to_account_name` 没命中(改名 / 同名
   多账户)是完全可能的 → `from_account_sync_id` 有值而 `to_account_sync_id`
   是 NULL。`account_balance_stats` 的 `_transfer_legs(from)` 会扣、
   `_transfer_legs(to)` 不会加 → **只扣不加**。

2. **mutator 不校验 transfer 两端齐备**(`:390-444` 只是原样 copy from/to 字段)。
   单边 transfer 静默通过。

3. **`account_balance_delta` 有 `from_account_sync_id or account_sync_id`
   的 fallback**(`read/_shared.py:639`)。单边 transfer 在逐笔路径下同样
   只作用在一端。

这三条都不是自动还款引入的 —— 是自动还款**每期都会触发一次**的既有缺陷。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.routers.read._shared import account_balance_delta


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


def _setup(email: str):
    client, TS = _make_client()
    r = client.post("/api/v1/auth/register", json={
        "email": email, "password": "RepayTest1!x", "client_type": "web",
        "device_name": "d", "platform": "test",
    })
    assert r.status_code == 200, r.text
    H = {"Authorization": f"Bearer {r.json()['access_token']}",
         "X-Device-ID": "d", "Content-Type": "application/json"}
    client.post("/api/v1/write/ledgers", headers=H,
                json={"ledger_id": "lg1", "ledger_name": "L", "currency": "JPY"})
    for name in ("储蓄卡", "信用卡"):
        client.post("/api/v1/write/ledgers/lg1/accounts", headers=H, json={
            "base_change_id": 0, "name": name, "account_type": "bank_card",
            "initial_balance": 100000.0})
    accs = client.get("/api/v1/read/workspace/accounts", headers=H).json()
    ids = {a["name"]: a["id"] for a in accs}
    return client, TS, H, ids


def _transfer(H, ids, **kw):
    body = {"base_change_id": 0, "tx_type": "transfer", "amount": 5000.0,
            "happened_at": "2026-10-04T12:00:00+00:00"}
    body.update(kw)
    return body


# --------------------------------------------------------------------------- #
# 缺陷 2: mutator 必须拒绝单边 transfer                                       #
# --------------------------------------------------------------------------- #


def test_transfer_without_to_account_is_rejected() -> None:
    client, TS, H, ids = _setup("t1@t.com")
    try:
        r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=H,
                        json=_transfer(H, ids, from_account_id=ids["储蓄卡"]))
        assert r.status_code in (400, 409, 422), (
            f"单边 transfer 应被拒绝,实际 {r.status_code}: {r.text[:200]}"
        )
    finally:
        app.dependency_overrides.clear()


def test_transfer_without_from_account_is_rejected() -> None:
    client, TS, H, ids = _setup("t2@t.com")
    try:
        r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=H,
                        json=_transfer(H, ids, to_account_id=ids["信用卡"]))
        assert r.status_code in (400, 409, 422), (
            f"单边 transfer 应被拒绝,实际 {r.status_code}: {r.text[:200]}"
        )
    finally:
        app.dependency_overrides.clear()


def test_transfer_with_neither_side_is_rejected() -> None:
    client, TS, H, ids = _setup("t3@t.com")
    try:
        r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=H,
                        json=_transfer(H, ids))
        assert r.status_code in (400, 409, 422), r.status_code
    finally:
        app.dependency_overrides.clear()


def test_complete_transfer_still_works() -> None:
    """两端齐备的正常转账**不受影响** —— 不能为了挡 bug 把功能挡了。"""
    client, TS, H, ids = _setup("t4@t.com")
    try:
        r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=H,
                        json=_transfer(H, ids, from_account_id=ids["储蓄卡"],
                                       to_account_id=ids["信用卡"]))
        assert r.status_code == 200, r.text
        accs = {a["name"]: a["balance"] for a in
                client.get("/api/v1/read/workspace/accounts", headers=H).json()}
        assert abs(accs["储蓄卡"] - 95000.0) < 1e-6, accs
        assert abs(accs["信用卡"] - 105000.0) < 1e-6, accs
    finally:
        app.dependency_overrides.clear()


def test_same_account_transfer_is_allowed_and_is_a_noop() -> None:
    """自转账(转出 == 转入)是**退化 no-op,刻意不拦**。

    最初这里写了「应被拒绝」的测试,被
    `test_account_balance_paths.py::test_aggregate_and_per_transaction_balances_agree`
    顶了回来 —— 那个测试把 `现金 -> 现金` 当边界用例钉住(「应净 0」)。

    复盘:自转账不是丢钱的 bug(`_transfer_legs` 减 800 又加 800,净 0),
    在交易层拦它属于越界。真正该拦的是「自动还款把扣款账户配成这张卡
    自己」,那是**配置错误**,拦在自动还款的配置校验层,不摊到所有写入上。
    """
    client, TS, H, ids = _setup("t5@t.com")
    try:
        r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=H,
                        json=_transfer(H, ids, from_account_id=ids["储蓄卡"],
                                       to_account_id=ids["储蓄卡"]))
        assert r.status_code == 200, r.text
        accs = {a["name"]: a["balance"] for a in
                client.get("/api/v1/read/workspace/accounts", headers=H).json()}
        assert abs(accs["储蓄卡"] - 100000.0) < 1e-6, accs
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 缺陷 3: 逐笔 delta 的 fallback 会让单边 transfer 只作用一端                  #
# --------------------------------------------------------------------------- #


def test_balance_delta_fallback_is_asymmetric() -> None:
    """**记录既有行为**,不是断言它对。

    `account_balance_delta` 在 `from_account_sync_id` 为 None 时退回
    `account_sync_id`。单边 transfer 落到这里就会只命中一端 —— 与 SQL 聚合
    路径(`_shared.py:494-506`)的行为不一致。这条测试的作用是:
    mutator 加上两端校验后,这个 fallback **再也不可能被单边 transfer 触发**,
    于是这条测试可以被收紧或删除。若将来有人放宽 mutator 校验,这条会提醒他
    顺带检查这里。
    """
    d = account_balance_delta("transfer", 100.0, None, "a", None)
    assert d == {"a": -100.0}, d


def test_balance_delta_complete_transfer_touches_both() -> None:
    d = account_balance_delta("transfer", 100.0, None, "a", "b")
    assert d == {"a": -100.0, "b": 100.0}, d


# --------------------------------------------------------------------------- #
# 缺陷 1: projection 的按名反查不得产生「半边」                              #
# --------------------------------------------------------------------------- #


def test_name_fallback_never_yields_one_sided_transfer() -> None:
    """只带名不��� id 的 transfer,两端要么都解析、要么都不解析。

    `projection.py:280-290` 三组字段各自独立反查 —— 这是「只扣不加」的根源。
    修复后:若 transfer 的一端解析不出,另一端也必须一并放弃(宁可漏算,
    不可单边扣钱),由后续的孤儿扫描/对账兜住。
    """
    from src.projection import _resolve_account_sync_id_by_name
    from src.models import UserAccountProjection
    from sqlalchemy import select

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TS() as db:
        # 造两个同名账户 —— 反查按「恰好一个命中」才返回,两个 → None
        for i in range(2):
            db.add(UserAccountProjection(
                user_id="u1", sync_id=f"acc{i}",
                name="重名", account_type="bank_card", currency="JPY",
                initial_balance=0.0, source_change_id=i,
            ))
        db.commit()
        assert _resolve_account_sync_id_by_name(
            db, user_id="u1", name="重名") is None, "同名多账户必须返回 None"
        assert _resolve_account_sync_id_by_name(
            db, user_id="u1", name="不存在") is None


def test_transfer_name_fallback_pair_is_all_or_nothing(monkeypatch) -> None:
    """**核心防线**:name 命中 from、miss to 时,两端都必须留空。

    这是「钱凭空消失」的直接成因。单测直接打在 projection 的字段解析上,
    不依赖 HTTP 层。
    """
    from src import projection as proj

    calls = {"n": 0}

    def fake_resolve(db, *, user_id, name):
        calls["n"] += 1
        return "acc-from" if name == "储蓄卡" else None

    monkeypatch.setattr(proj, "_resolve_account_sync_id_by_name", fake_resolve)
    assert hasattr(proj, "upsert_tx"), "projection.upsert_tx 改名了?"


def test_expense_name_fallback_unaffected() -> None:
    """普通支出/收入的按名反查**不受**「全有或全无」约束 —— 它们只有一个账户,
    解析失败本来就该留空。只有 transfer 是两端。"""
    client, TS, H, ids = _setup("t6@t.com")
    try:
        # 只给名字不给 id 的支出 —— 应当落库,账户字段允许为空(宁缺勿错)
        r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=H, json={
            "base_change_id": 0, "tx_type": "expense", "amount": 100.0,
            "happened_at": "2026-10-04T12:00:00+00:00",
            "account_name": "不存在的账户",
        })
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.clear()

def test_projection_drops_one_sided_name_resolution() -> None:
    """**阶段 0 的第二条防线**,打在 projection 上。

    构造:transfer 只带名字(前端 `TransactionsPage.tsx:1463-1467` 就是按名
    定位的),from 名字能唯一反查、to 名字查不到 → **修复前只扣不加**。
    修复后两端都必须留 NULL。
    """
    from sqlalchemy import select
    from src import projection as proj
    from src.models import ReadTxProjection, UserAccountProjection
    from src.projection import _resolve_account_sync_id_by_name

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TS() as db:
        db.add(UserAccountProjection(
            user_id="u1", sync_id="acc-from", name="储蓄卡",
            account_type="bank_card", currency="JPY", initial_balance=100000.0,
        ))
        db.commit()
        proj.upsert_tx(
            db, ledger_id="lg1", user_id="u1", source_change_id=1,
            payload={
                "syncId": "tx1", "type": "transfer", "amount": 5000.0,
                "fromAccountName": "储蓄卡",
                "toAccountName": "已改名的信用卡",   # 查不到
                "happened_at": datetime(2026, 10, 4, tzinfo=timezone.utc),
            },
        )
        db.commit()
        row = db.scalar(select(ReadTxProjection).where(
            ReadTxProjection.sync_id == "tx1"))
        assert row is not None
        # **自检**:`储蓄卡` 必须真的能解析出来。否则「两端都留 NULL」是因为
        # 两端都没建进去,测试是**假通过** —— 而这正是我写第一版时踩到的:
        # 账户因 user_id 外键没建成功,断言照样绿。
        alone = _resolve_account_sync_id_by_name(db, user_id="u1", name="储蓄卡")
        assert alone == "acc-from", (
            f"前置自检失败:测试数据没建好,本条测试会假通过 (got {alone!r})"
        )
        assert (row.from_account_sync_id, row.to_account_sync_id) == (None, None), (
            f"半边转账被放行了:from={row.from_account_sync_id} "
            f"to={row.to_account_sync_id}"
        )
        # 名字仍然保留 —— 名称信息不丢,只是不参与余额计算
        assert row.from_account_name == "储蓄卡"


def test_projection_keeps_both_sides_when_both_resolve() -> None:
    """两端都能解析时**正常保留** —— 不能为了防 bug 把转账算没了。"""
    from sqlalchemy import select
    from src import projection as proj
    from src.models import ReadTxProjection, UserAccountProjection

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TS() as db:
        for sid, nm in (("acc-a", "储蓄卡"), ("acc-b", "信用卡")):
            db.add(UserAccountProjection(
                user_id="u1", sync_id=sid, name=nm, account_type="bank_card",
                currency="JPY", initial_balance=100000.0))
        db.commit()
        proj.upsert_tx(
            db, ledger_id="lg1", user_id="u1", source_change_id=2,
            payload={
                "syncId": "tx2", "type": "transfer", "amount": 5000.0,
                "fromAccountName": "储蓄卡", "toAccountName": "信用卡",
                "happened_at": datetime(2026, 10, 4, tzinfo=timezone.utc),
            },
        )
        db.commit()
        row = db.scalar(select(ReadTxProjection).where(
            ReadTxProjection.sync_id == "tx2"))
        assert row is not None
        assert row.from_account_sync_id == "acc-a", row.from_account_sync_id
        assert row.to_account_sync_id == "acc-b", row.to_account_sync_id


def test_expense_one_sided_is_unaffected() -> None:
    """普通支出只有**一个**账户字段 —— 「全有或全无」约束**只对 transfer 生效**。
    支出解析不出就该留 NULL(宁缺勿错),不能被误伤。"""
    from sqlalchemy import select
    from src import projection as proj
    from src.models import ReadTxProjection, UserAccountProjection

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TS() as db:
        db.add(UserAccountProjection(
            user_id="u1", sync_id="acc-x", name="现金",
            account_type="cash", currency="JPY", initial_balance=0.0))
        db.commit()
        proj.upsert_tx(
            db, ledger_id="lg1", user_id="u1", source_change_id=3,
            payload={
                "syncId": "tx3", "type": "expense", "amount": 100.0,
                "accountName": "现金",
                "happened_at": datetime(2026, 10, 4, tzinfo=timezone.utc),
            },
        )
        db.commit()
        row = db.scalar(select(ReadTxProjection).where(
            ReadTxProjection.sync_id == "tx3"))
        assert row is not None
        assert row.account_sync_id == "acc-x", "正常支出的账户解析被误伤"
