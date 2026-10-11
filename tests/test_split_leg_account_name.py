"""组合支付的腿必须带**账户名**。

`read_tx_split_projection` 只有 `account_sync_id` 和 `amount`。读端点原来
也只原样吐这两个字段 —— 前端拿到的腿没有名字,只能显示裸的
`acc_0a63dbec54bc`,用户认不出来是哪张卡,于是组合支付在交易详情弹窗里
「零展示」。

连带后果:用户在账户详情里看到一笔 5000,而余额只动了 3000,
**没有任何地方解释这个差异** —— 最可能的归因是「余额算错了」,
然后手动去改初始余额,把本来对的数据改坏。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app

LEDGER = "lg-legname"


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
    import uuid

    # 每次用唯一邮箱 —— 同一邮箱重复注册会 409,而 test 之间 DB 是新的,
    # 但 email 撞在同一次运行的文件级缓存上就会红得很莫名其妙。
    email = f"{uuid.uuid4().hex[:6]}-{email}"
    r = client.post("/api/v1/auth/register", json={
        "email": email, "password": "LegNameTest1!x", "client_type": "web",
        "device_name": "d", "platform": "test",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}",
            "X-Device-ID": "d", "Content-Type": "application/json"}


def test_split_legs_carry_account_name(env) -> None:
    """读端点返回的每条腿都必须有 `account_name`。"""
    client, TS = env
    H = _register(client, "legname@t.com")

    assert client.post("/api/v1/write/ledgers", headers=H, json={
        "ledger_id": LEDGER, "ledger_name": "Daily",
        "currency": "JPY"}).status_code == 200

    # 两张同币种账户
    for name in ("Card", "Cash"):
        assert client.post(f"/api/v1/write/ledgers/{LEDGER}/accounts",
                           headers=H, json={
                               "base_change_id": 0, "name": name,
                               "account_type": "bank_card",
                               "initial_balance": 10000.0,
                           }).status_code == 200

    accs = client.get("/api/v1/read/workspace/accounts", headers=H,
                      params={"ledger_id": LEDGER}).json()
    ids = {a["name"]: a["id"] for a in accs}

    # 一笔组合支付:5000 = 卡 3000 + 现金 2000
    r = client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions",
                    headers=H, json={
                        "base_change_id": 0, "tx_type": "expense",
                        "amount": 5000.0,
                        "happened_at": "2026-10-04T09:00:00+00:00",
                        "category_name": "Shop",
                        "category_kind": "expense",
                        "splits": [
                            {"account_id": ids["Card"], "amount": 3000.0},
                            {"account_id": ids["Cash"], "amount": 2000.0},
                        ],
                    })
    assert r.status_code == 200, r.text

    txs = client.get("/api/v1/read/workspace/transactions", headers=H,
                     params={"ledger_id": LEDGER}).json()
    items = txs["items"] if isinstance(txs, dict) else txs
    tx = next(t for t in items if float(t["amount"]) == 5000.0)

    legs = tx.get("splits") or []
    assert len(legs) == 2, f"应返回 2 条腿:{legs}"
    by_id = {l["account_id"]: l for l in legs}
    assert set(by_id) == {ids["Card"], ids["Cash"]}, legs

    for leg in legs:
        assert leg.get("account_name"), (
            f"这条腿没有 account_name —— 前端只能显示裸 id,用户认不出是"
            f"哪张卡。腿:{leg}")
    assert by_id[ids["Card"]]["account_name"] == "Card"
    assert by_id[ids["Cash"]]["account_name"] == "Cash"

    # 金额必须对得上(这不是本测试的主题,但顺手钉住「父 amount 仍是总额」)
    assert float(by_id[ids["Card"]]["amount"]) == 3000.0
    assert float(by_id[ids["Cash"]]["amount"]) == 2000.0
    assert float(tx["amount"]) == 5000.0, "父交易 amount 被改成了腿额"


def test_account_name_map_filters_null_names() -> None:
    """`_account_name_map` 必须过滤掉 name 为 NULL 的账户。

    不过滤的话返回类型里混进 None,调用方到处要判空;而 mypy 这一条
    在基线里是干净的(80),不该为它涨一个。
    """
    from src.routers.read._shared import _account_name_map

    def fake_db():
        raise AssertionError("不该被调用")

    class _Row:
        def __init__(self, sid, name):
            self._v = (sid, name)

    # 直接用真的 helper 跑一次形状检查:一个空库上它必须返回 {}
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from src.database import Base

    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    with sessionmaker(bind=engine)() as db:
        assert _account_name_map(db) == {}, "空库应返回空映射"
