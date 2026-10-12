"""215 轮:汇率覆盖的**持久性**契约。

## 先说清楚我查错了什么(免得下一个人重走)

我一度以为「`/read/exchange-rates` 返回代理值而不用覆盖」= **覆盖功能对读端无效**,
差点按 bug 记下来。真读完前端才发现**它是对的**:

`frontend/packages/web-features/src/lib/currencies.ts::_effectiveRates`
**同时**取两个端点,然后

    out[q] = 1 / auto[q]      # 代理值取倒数(1 base = rate quote)
    out[q] = override[q]      # 手动值不取倒数(1 quote = rate base),且**优先**

后端 `src/routers/read/workspace.py` 的 `rates_to_base` 也**应用覆盖**
(先填缓存、**后**用覆盖覆盖上去)。**所以金额换算是对的。**

## 那这个测试守什么

守**覆盖的持久性**:写进去 -> 落进 projection -> 能原样读回来。

这两步一旦坏掉,前端的合并逻辑就无从谈起 —— 它依赖
`/read/exchange-rate-overrides` 返回的是**用户设的那个值**。
原来只测「PUT 返回 200 且 projection 有一行」,**没有测读回来那一步**。

## 一条**没写下来**的契约(本轮最该记的)

| 端点 | 返回 |
|---|---|
| `GET /read/exchange-rates?base=X` | **只有代理源的值**,**不含**用户覆盖 |
| `GET /read/exchange-rate-overrides` | 用户覆盖列表 |

所以**调用方必须自己合并**,不能只调前一个。
前端 `_effectiveRates` 就是这么做的。

> 📌 「两个端点都返回汇率」很容易被读成「前一个已经包含了后一个」。
> 215 轮我就是这样误判的 —— **端点名字相邻 + 返回体都叫 rates**。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.models import UserExchangeRateProjection


def _setup(email: str):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
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
    client = TestClient(app)
    r = client.post("/api/v1/auth/register", json={
        "email": email, "password": "RateDir1!x", "client_type": "web",
        "device_name": "d", "platform": "test"})
    return client, TS, {"Authorization": f"Bearer {r.json()['access_token']}",
                        "Content-Type": "application/json"}


def test_override_survives_write_and_reads_back_unchanged() -> None:
    client, TS, H = _setup("rate215c@t.com")

    w = client.put("/api/v1/write/exchange-rate-overrides", headers=H, json={
        "base_currency": "cny", "quote_currency": "usd", "rate": "7.20"})
    assert w.status_code == 200, w.text
    assert w.json()["rate"] == "7.20", w.text

    with TS() as db:
        rows = db.scalars(select(UserExchangeRateProjection)).all()
    assert [(r.base_currency, r.quote_currency, r.rate) for r in rows] == [
        ("CNY", "USD", "7.20")], "落库的值/大小写不对"

    # 🔴 原来缺的就是这一步:读回来必须**还是用户设的那个值**,
    # 因为前端 `_effectiveRates` 直接拿它当 `rates_to_base` 用。
    got = client.get("/api/v1/read/exchange-rate-overrides", headers=H).json()
    assert len(got) == 1, got
    assert got[0]["base_currency"] == "CNY"
    assert got[0]["quote_currency"] == "USD"
    assert got[0]["rate"] == "7.20", (
        f"读回来的 rate 不等于写入值: {got[0]['rate']!r} "
        f"(前端把它直接当 1 quote = rate base 用,变了就是金额算错)")


def test_exchange_rates_endpoint_does_not_include_overrides() -> None:
    """**记录现状**,不是判定它应该这样。

    `/read/exchange-rates` 只返回代理源的值。前端必须自己再取
    `/read/exchange-rate-overrides` 并让覆盖优先(见 `currencies.ts`)。

    ⚠️ 这条**随时可能失效** —— 哪天后端决定「顺便把覆盖合进去」,
    它就会红。那不一定是 bug,**要顺手改前端**再改这条注释。
    """
    client, _TS, H = _setup("rate215d@t.com")
    client.put("/api/v1/write/exchange-rate-overrides", headers=H, json={
        "base_currency": "CNY", "quote_currency": "USD", "rate": "7.20"})

    body = client.get("/api/v1/read/exchange-rates", headers=H,
                      params={"base": "CNY"}).json()
    assert set(body) == {"base", "rate_date", "source", "fetched_at",
                         "stale", "rates"}, sorted(body)
    auto_usd = body["rates"].get("USD")
    assert auto_usd is not None, "测试前提不成立:返回里没有 USD"
    assert float(auto_usd) not in (7.20, round(1 / 7.20, 8)), (
        "这个端点居然返回了覆盖值 —— 那么前端 _effectiveRates 的"
        "「手动优先」就是多余的,契约变了,顺手改前端和这条注释。")