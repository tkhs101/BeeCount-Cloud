"""手动触发自动还款时,**失败必须变成非 200**。

## 为什么需要

`POST /write/ledgers/{lid}/accounts/{aid}/autorepay/run` 里,
`repay_one` 把所有异常包成 `status="failed"` **返回**,不抛。
端点若照常返回 200,用户看到的就是「点了没反应」——
**HTTP 200、余额没动、没有任何提示**。

这个故障真实发生过(CLAUDE.md 记的信用卡自动还款「两轮测试全绿但
跑不通」之一),修法是端点把两种状态翻成错误码:

| `repay_one` 的状态 | HTTP |
|---|---|
| `failed` | **502** |
| `skipped_invalid_config` | **400** |

⚠️ **这条护栏以前不存在**:`grep -rln "502" tests/*.py` 零结果。
CLAUDE.md 把这个映射写得像已受保护,其实裸奔。

## 为什么可以 mock `repay_one`

被测对象是**端点把 outcome 翻成 HTTP 码的那几行**,不是 `repay_one`
的算法(那部分有 `test_credit_autorepay_e2e.py` 和分层测试)。
要造出 `status="failed"` 只能靠真实异常,而这里要验的恰恰是
**拿到 `failed` 之后端点做了什么** —— 直接注入状态才是精确判据。

⚠️ `repay_one` 在端点里是**函数内 import**,所以必须 patch
`src.services.credit_card.repay.repay_one`(定义处),
patch `src.routers.write.accounts.repay_one` 无效(那个名字不存在)。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override():
        db = TS()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _card(client):
    r = client.post("/api/v1/auth/register", json={
        "email": "ar502@t.com", "password": "Ar502Pass1!x",
        "client_type": "web", "device_name": "d", "platform": "test"})
    assert r.status_code == 200, r.text
    H = {"Authorization": f"Bearer {r.json()['access_token']}",
         "X-Device-ID": "d", "Content-Type": "application/json"}
    assert client.post("/api/v1/write/ledgers", headers=H, json={
        "ledger_id": "lg1", "ledger_name": "L", "currency": "CNY"
    }).status_code == 200
    assert client.post("/api/v1/write/ledgers/lg1/accounts", headers=H, json={
        "base_change_id": 0, "name": "招行卡", "account_type": "credit_card",
        "initial_balance": 0.0}).status_code == 200
    rows = client.get("/api/v1/read/workspace/accounts", headers=H).json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    return H, [a for a in rows if a["name"] == "招行卡"][0]["id"]


def _run(client, H, aid):
    return client.post(
        f"/api/v1/write/ledgers/lg1/accounts/{aid}/autorepay/run", headers=H)


def test_failed_repay_becomes_502_not_200(client, monkeypatch):
    """`status="failed"` 必须变成 502 —— 这是「用户点了没反应」那条。"""
    H, aid = _card(client)
    import src.services.credit_card.repay as R
    monkeypatch.setattr(
        R, "repay_one",
        lambda *a, **k: SimpleNamespace(status="failed", detail="boom",
                                  tx_sync_id=None))

    r = _run(client, H, aid)
    assert r.status_code == 502, (
        f"expected 502, got {r.status_code} —— 失败被吞成 200 了,"
        f"用户看到的就是「点了没反应」")


def test_invalid_config_becomes_400(client, monkeypatch):
    """`skipped_invalid_config` 必须变成 400,而不是 200。"""
    H, aid = _card(client)
    import src.services.credit_card.repay as R
    monkeypatch.setattr(
        R, "repay_one",
        lambda *a, **k: SimpleNamespace(status="skipped_invalid_config",
                                       detail="no billing_day",
                                       tx_sync_id=None))

    r = _run(client, H, aid)
    assert r.status_code == 400, f"expected 400, got {r.status_code}"


def test_skipped_disabled_stays_200(client, monkeypatch):
    """**反向护栏**:「卡上自动还款本来就是关着的」是正常的无事可做,
    必须仍然 200 —— 否则这条护栏会逼着人把正常跳过也改成报错。"""
    H, aid = _card(client)
    import src.services.credit_card.repay as R
    monkeypatch.setattr(
        R, "repay_one",
        lambda *a, **k: SimpleNamespace(status="skipped_disabled",
                                       detail="off", tx_sync_id=None))

    r = _run(client, H, aid)
    assert r.status_code == 200, (
        f"skipped_disabled 被误报成 {r.status_code} —— "
        f"用户自己关掉的开关,不该报错")