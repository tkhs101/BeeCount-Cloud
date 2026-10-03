"""MCP 分析与批量工具(compare_periods / breakdown / pattern / batch delete / CSV)。

重点验**算法结果的具体数字** —— 这几个工具的价值全在「算得对不对」,
只看返回结构通过没有意义。全部用固定数据把期望值写死。

口径基线(与 server 一致):金额取 `coalesce(native_amount, amount)`、排除
`exclude_from_stats`、支出按**净额**(剥税),所以分类切片能加回总额。

注意:分析工具是**同步**的(纯 DB 读),批量/导出是异步的 —— `asyncio.run`
只用在异步那几个。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.mcp.tools import (
    analytics_tools,
    bulk_tools,
    entity_tools,
    read_tools,
    write_tools,
)
from src.models import User
from src.security import SCOPE_APP_WRITE, SCOPE_WEB_WRITE, _create_token


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


def _wire(monkeypatch, TS):
    for mod in (write_tools, entity_tools, read_tools, analytics_tools, bulk_tools):
        monkeypatch.setattr(mod, "SessionLocal", TS)
    monkeypatch.setattr(
        write_tools, "_internal_token",
        lambda u: _create_token(
            sub=u.id, token_type="access", expires_delta=timedelta(seconds=60),
            scopes=[SCOPE_APP_WRITE, SCOPE_WEB_WRITE], client_type="app",
        ),
    )


def _setup(monkeypatch, email: str):
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "AnTest1!x", "client_type": "web",
              "device_name": "d", "platform": "test"},
    )
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    hdr = {"Authorization": f"Bearer {tok}", "X-Device-ID": "d",
           "Content-Type": "application/json"}
    r = client.post("/api/v1/write/ledgers", headers=hdr,
                    json={"ledger_id": "lg1", "ledger_name": "L",
                          "currency": "JPY"})
    assert r.status_code == 200, r.text
    client.post("/api/v1/write/ledgers/lg1/categories", headers=hdr,
                json={"base_change_id": 0, "name": "餐饮", "kind": "expense"})
    with TS() as db:
        user = db.scalar(select(User).where(User.email == email))
        db.expunge(user)
    return client, TS, hdr, user


def _tx(client, hdr, **kw):
    payload = {"base_change_id": 0, "type": "expense", "category_name": "餐饮"}
    payload.update(kw)
    r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=hdr,
                    json=payload)
    assert r.status_code == 200, r.text
    return r.json().get("entity_id")


def _at(y=2026, m=10, d=1, hour=12, minute=0):
    return datetime(y, m, d, hour, minute, tzinfo=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# compare_periods                                                               #
# --------------------------------------------------------------------------- #


def test_compare_periods_month_over_month(monkeypatch) -> None:
    client, _TS, hdr, user = _setup(monkeypatch, "cmp1@t.com")
    try:
        _tx(client, hdr, amount=30000.0, happened_at=_at(m=9, d=10), note="A")
        _tx(client, hdr, amount=45000.0, happened_at=_at(m=10, d=3), note="B")

        out = analytics_tools.compare_periods(
            user, scope="month", period="2026-10", compare="previous"
        )
        assert abs(out["expense"]["current"] - 45000.0) < 1e-6, out["expense"]
        assert abs(out["expense"]["previous"] - 30000.0) < 1e-6, out["expense"]
        assert abs(out["expense"]["delta"] - 15000.0) < 1e-6, out["expense"]
        assert abs(out["expense"]["percent_change"] - 50.0) < 1e-6, out["expense"]
        assert out["current_period"]["label"] == "2026-10", out["current_period"]
        assert out["previous_period"]["label"] == "2026-09", out["previous_period"]
        movers = {m["category_name"]: m["delta"] for m in out["largest_changes"]}
        assert abs(movers["餐饮"] - 15000.0) < 1e-6, movers
    finally:
        app.dependency_overrides.clear()


def test_compare_periods_tax_is_deducted(monkeypatch) -> None:
    """支出口径是**净额** —— 与饼图切片一致,否则切片加不回总额。"""
    client, _TS, hdr, user = _setup(monkeypatch, "cmp2@t.com")
    try:
        _tx(client, hdr, amount=10000.0, happened_at=_at(m=9, d=5))
        _tx(client, hdr, amount=10298.0, tax_amount=298.0,
            happened_at=_at(m=10, d=5))
        out = analytics_tools.compare_periods(user, scope="month", period="2026-10")
        assert abs(out["expense"]["current"] - 10000.0) < 1e-6, out["expense"]
        assert abs(out["expense"]["delta"]) < 1e-6, out["expense"]
    finally:
        app.dependency_overrides.clear()


def test_compare_periods_year_over_year(monkeypatch) -> None:
    client, _TS, hdr, user = _setup(monkeypatch, "cmp3@t.com")
    try:
        _tx(client, hdr, amount=1000.0, happened_at=_at(y=2025, m=10, d=5))
        _tx(client, hdr, amount=1500.0, happened_at=_at(y=2026, m=10, d=5))
        out = analytics_tools.compare_periods(
            user, scope="month", period="2026-10", compare="last_year"
        )
        assert out["previous_period"]["label"] == "2025-10", out["previous_period"]
        assert abs(out["expense"]["delta"] - 500.0) < 1e-6, out["expense"]
        assert abs(out["expense"]["percent_change"] - 50.0) < 1e-6, out["expense"]
    finally:
        app.dependency_overrides.clear()


def test_compare_periods_excludes_flagged(monkeypatch) -> None:
    client, _TS, hdr, user = _setup(monkeypatch, "cmp4@t.com")
    try:
        _tx(client, hdr, amount=5000.0, happened_at=_at(m=10, d=1))
        _tx(client, hdr, amount=99999.0, happened_at=_at(m=10, d=2),
            exclude_from_stats=True)
        out = analytics_tools.compare_periods(user, scope="month", period="2026-10")
        assert abs(out["expense"]["current"] - 5000.0) < 1e-6, (
            f"exclude_from_stats 的交易被算进去了:{out['expense']}"
        )
    finally:
        app.dependency_overrides.clear()


def test_compare_periods_rejects_bad_input(monkeypatch) -> None:
    client, _TS, _hdr, user = _setup(monkeypatch, "cmp5@t.com")
    try:
        bad = analytics_tools.compare_periods(user, scope="all")
        assert "error" in bad, bad
        with pytest.raises(ValueError, match="Invalid compare"):
            analytics_tools.compare_periods(user, scope="month", compare="nope")
    finally:
        app.dependency_overrides.clear()


def test_period_range_boundaries() -> None:
    """月末边界:2026-02 应到 3/1 —— 漏一天或多一天都会让环比算错。"""
    start, end, label = analytics_tools.period_range("month", "2026-02")
    assert label == "2026-02"
    assert start == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 1, tzinfo=timezone.utc)

    _s, e, lbl = analytics_tools.period_range("year", "2026")
    assert lbl == "2026" and e.year == 2027

    with pytest.raises(ValueError, match="Invalid scope"):
        analytics_tools.period_range("decade", None)


# --------------------------------------------------------------------------- #
# get_spending_breakdown                                                        #
# --------------------------------------------------------------------------- #


def test_breakdown_by_merchant_sums(monkeypatch) -> None:
    """`search` 只能逐笔找到,这个工具要能求和。"""
    client, _TS, hdr, user = _setup(monkeypatch, "br1@t.com")
    try:
        for amt in (300.0, 420.0, 280.0):
            _tx(client, hdr, amount=amt, happened_at=_at(m=10, d=2), note="星巴克")
        _tx(client, hdr, amount=1500.0, happened_at=_at(m=10, d=3), note="罗森")

        out = analytics_tools.get_spending_breakdown(
            user, by="merchant", scope="month", period="2026-10"
        )
        got = {i["name"]: i for i in out["items"]}
        assert abs(got["星巴克"]["total"] - 1000.0) < 1e-6, got
        assert got["星巴克"]["count"] == 3, got["星巴克"]
        assert abs(out["total"] - 2500.0) < 1e-6, out["total"]

        out2 = analytics_tools.get_spending_breakdown(
            user, by="merchant", scope="month", period="2026-10", q="星巴"
        )
        assert [i["name"] for i in out2["items"]] == ["星巴克"], out2["items"]
    finally:
        app.dependency_overrides.clear()


def test_breakdown_by_tag_explodes_many_to_many(monkeypatch) -> None:
    """一笔交易挂多个标签 → 每个标签都分到这笔金额(1:N,SQL 拆不开)。"""
    client, _TS, hdr, user = _setup(monkeypatch, "br2@t.com")
    try:
        _tx(client, hdr, amount=1000.0, happened_at=_at(m=10, d=1),
            tags=["出差", "交通"])
        _tx(client, hdr, amount=500.0, happened_at=_at(m=10, d=2), tags=["出差"])

        out = analytics_tools.get_spending_breakdown(
            user, by="tag", scope="month", period="2026-10"
        )
        got = {i["name"]: i["total"] for i in out["items"]}
        assert abs(got["出差"] - 1500.0) < 1e-6, got
        assert abs(got["交通"] - 1000.0) < 1e-6, got
    finally:
        app.dependency_overrides.clear()


def test_breakdown_min_amount_filter(monkeypatch) -> None:
    client, _TS, hdr, user = _setup(monkeypatch, "br3@t.com")
    try:
        _tx(client, hdr, amount=50.0, happened_at=_at(m=10, d=1), note="便利店")
        _tx(client, hdr, amount=5000.0, happened_at=_at(m=10, d=2), note="电器")
        out = analytics_tools.get_spending_breakdown(
            user, by="merchant", scope="month", period="2026-10", min_amount=1000
        )
        assert [i["name"] for i in out["items"]] == ["电器"], out["items"]
        assert abs(out["total"] - 5000.0) < 1e-6, out["total"]
    finally:
        app.dependency_overrides.clear()


def test_breakdown_rejects_bad_dimension(monkeypatch) -> None:
    client, _TS, _hdr, user = _setup(monkeypatch, "br4@t.com")
    try:
        with pytest.raises(ValueError, match="Invalid by"):
            analytics_tools.get_spending_breakdown(user, by="nope")
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# get_spending_pattern                                                          #
# --------------------------------------------------------------------------- #


def test_spending_pattern_buckets(monkeypatch) -> None:
    client, _TS, hdr, user = _setup(monkeypatch, "pat1@t.com")
    try:
        # 2026-10-05 是周一;2026-10-03 是周六
        _tx(client, hdr, amount=1000.0, happened_at=_at(m=10, d=5, hour=9))
        _tx(client, hdr, amount=300.0, happened_at=_at(m=10, d=3, hour=23))
        _tx(client, hdr, amount=20000.0, happened_at=_at(m=10, d=5, hour=14))

        out = analytics_tools.get_spending_pattern(
            user, scope="month", period="2026-10"
        )
        assert abs(out["total_expense"] - 21300.0) < 1e-6, out["total_expense"]
        wd = {d["weekday"]: d["total"] for d in out["by_weekday"]}
        assert abs(wd["周一"] - 21000.0) < 1e-6, wd
        assert abs(wd["周六"] - 300.0) < 1e-6, wd
        bands = {b["band"]: b["count"] for b in out["by_amount_band"]}
        assert bands["0-100"] == 0, bands
        assert bands["100-500"] == 1, bands
        assert bands["10000+"] == 1, bands
        hours = {h["hour_range"] for h in out["by_hour"]}
        assert "22-24" in hours, hours
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# batch delete                                                                  #
# --------------------------------------------------------------------------- #


def test_batch_delete_two_step_and_cap(monkeypatch) -> None:
    client, _TS, hdr, user = _setup(monkeypatch, "bd1@t.com")
    try:
        ids = [_tx(client, hdr, amount=100.0, happened_at=_at(m=10, d=1))
               for _ in range(3)]

        out = asyncio.run(bulk_tools.delete_transactions_batch(user, tx_ids=ids))
        assert out["status"] == "confirmation_required", out
        assert out["tx_ids"] == ids, out
        r = client.get("/api/v1/read/workspace/transactions", headers=hdr)
        assert len(r.json()["items"]) == 3, "没确认就把数据删了"

        out2 = asyncio.run(bulk_tools.delete_transactions_batch(
            user, tx_ids=ids, confirm=True))
        assert out2["deleted"] == 3, out2
        assert out2["failed"] == [], out2
        r = client.get("/api/v1/read/workspace/transactions", headers=hdr)
        assert r.json()["items"] == []

        with pytest.raises(ValueError, match="Too many"):
            asyncio.run(bulk_tools.delete_transactions_batch(
                user, tx_ids=[f"t{i}" for i in range(201)], confirm=True))
        with pytest.raises(ValueError, match="must not be empty"):
            asyncio.run(bulk_tools.delete_transactions_batch(user, tx_ids=[]))
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# CSV export                                                                    #
# --------------------------------------------------------------------------- #


def test_export_csv_includes_tax_column(monkeypatch) -> None:
    client, _TS, hdr, user = _setup(monkeypatch, "csv1@t.com")
    try:
        _tx(client, hdr, amount=3280.0, tax_amount=298.0,
            happened_at=_at(m=10, d=3), note="KING BEAR NOW")
        out = asyncio.run(bulk_tools.export_transactions_csv(
            user, date_from="2026-10-01", date_to="2026-10-31", lang="en"))
        assert out["row_count"] == 1, out
        assert out["columns"][-1] == "Tax", out["columns"]
        line = out["csv"].splitlines()[1]
        assert line.endswith(",298.00"), line
    finally:
        app.dependency_overrides.clear()


def test_export_csv_respects_filters(monkeypatch) -> None:
    client, _TS, hdr, user = _setup(monkeypatch, "csv2@t.com")
    try:
        _tx(client, hdr, amount=100.0, happened_at=_at(m=10, d=1), note="keep")
        _tx(client, hdr, amount=900.0, happened_at=_at(m=10, d=2), note="drop")
        out = asyncio.run(bulk_tools.export_transactions_csv(
            user, min_amount=500.0, lang="en"))
        assert out["row_count"] == 1, out
        assert "drop" in out["csv"] and "keep" not in out["csv"], out["csv"]

        bad = asyncio.run(bulk_tools.export_transactions_csv(
            user, category="不存在的分类"))
        assert "error" in bad, bad
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 注册                                                                          #
# --------------------------------------------------------------------------- #


def test_new_tools_registered_with_ledger_id() -> None:
    from src.mcp.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("compare_periods", "get_spending_breakdown",
                 "get_spending_pattern", "delete_transactions_batch",
                 "export_transactions_csv"):
        assert name in tools, f"{name} 未注册"
        assert "ledger_id" in tools[name].inputSchema["properties"], name


def test_sync_analysis_tools_wrapped_in_to_thread() -> None:
    """分析工具是**同步**的(纯 DB 读),必须像 read_tools 一样包
    `asyncio.to_thread` —— `_logged_call` 里是 `await body(user)`,直接塞
    同步函数会在运行时抛 "object dict can't be used in 'await'"。

    这条断言锁的是上一轮真的踩过的坑,别删。
    """
    import inspect

    import src.mcp.server as srv

    src_text = inspect.getsource(srv)
    for name in ("compare_periods", "get_spending_breakdown",
                 "get_spending_pattern"):
        assert f"asyncio.to_thread(analytics_tools.{name}" in src_text, (
            f"{name} 是同步函数,注册时必须包 asyncio.to_thread"
        )
