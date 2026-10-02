"""MCP 工具测试.

read tools 的 happy-path 直接开 session 测。write tools 走 HTTP self-call
(in-process ASGI),其中**不需要真正落库**的部分(参数声明 / 归一化 / 汇率
字段推导)直接调内部函数覆盖,完整写入链路留给 e2e。

`create_transactions` 的批量类型契约见下方
`test_create_transactions_schema_declares_numeric_amount` —— 该 bug 曾长期
存在正是因为这里没有任何覆盖。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.mcp.server import mcp
from src.mcp.tools import read_tools, write_tools
from src.models import User


def _make_client_and_engine(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # read_tools 直接 `with SessionLocal() as db:` —— 不走 dep tree。
    monkeypatch.setattr(read_tools, "SessionLocal", testing_session)
    return TestClient(app), testing_session


def _register(client: TestClient, email: str = "tools@example.com") -> dict:
    res = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "123456",
            "client_type": "web",
            "device_name": "pytest-web",
            "platform": "web",
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


def _make_ledger(client: TestClient, token: str, name: str = "Main") -> str:
    res = client.post(
        "/api/v1/write/ledgers",
        json={"ledger_name": name, "currency": "CNY"},
        headers={"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"},
    )
    assert res.status_code == 200, res.text
    return res.json()["entity_id"]


def _login_token(client: TestClient, email: str = "tools@example.com") -> str:
    r = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "123456", "client_type": "web",
              "device_name": "pytest-web", "platform": "web"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _fetch_user(session_maker, email: str) -> User:
    with session_maker() as db:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        db.expunge(user)
        return user


def test_list_ledgers_returns_user_ledgers(monkeypatch) -> None:
    client, session_maker = _make_client_and_engine(monkeypatch)
    try:
        u = _register(client, email="lister@example.com")
        token = u["access_token"]
        _make_ledger(client, token, "Family")
        _make_ledger(client, token, "Personal")
        user = _fetch_user(session_maker, "lister@example.com")

        ledgers = read_tools.list_ledgers(user)
        assert len(ledgers) == 2
        names = {l["name"] for l in ledgers}
        assert names == {"Family", "Personal"}
        for led in ledgers:
            assert led["id"]
            assert led["currency"] == "CNY"
    finally:
        app.dependency_overrides.clear()


def test_get_active_ledger_returns_earliest(monkeypatch) -> None:
    client, session_maker = _make_client_and_engine(monkeypatch)
    try:
        u = _register(client, email="active@example.com")
        token = u["access_token"]
        _make_ledger(client, token, "First")
        _make_ledger(client, token, "Second")
        user = _fetch_user(session_maker, "active@example.com")

        active = read_tools.get_active_ledger(user)
        assert active is not None
        assert active["name"] == "First"
    finally:
        app.dependency_overrides.clear()


def test_get_active_ledger_returns_none_for_no_ledger(monkeypatch) -> None:
    client, session_maker = _make_client_and_engine(monkeypatch)
    try:
        _register(client, email="empty@example.com")
        user = _fetch_user(session_maker, "empty@example.com")

        active = read_tools.get_active_ledger(user)
        assert active is None
    finally:
        app.dependency_overrides.clear()


def test_list_transactions_empty_ledger(monkeypatch) -> None:
    client, session_maker = _make_client_and_engine(monkeypatch)
    try:
        u = _register(client, email="emptytx@example.com")
        token = u["access_token"]
        _make_ledger(client, token, "Empty")
        user = _fetch_user(session_maker, "emptytx@example.com")

        result = read_tools.list_transactions(user, limit=10)
        assert result["total"] == 0
        assert result["items"] == []
        assert result["ledger"] == "Empty"
    finally:
        app.dependency_overrides.clear()


def test_get_ledger_stats_returns_zero_for_fresh_ledger(monkeypatch) -> None:
    client, session_maker = _make_client_and_engine(monkeypatch)
    try:
        u = _register(client, email="stats@example.com")
        token = u["access_token"]
        _make_ledger(client, token, "Stats")
        user = _fetch_user(session_maker, "stats@example.com")

        stats = read_tools.get_ledger_stats(user)
        assert stats is not None
        assert stats["ledger"] == "Stats"
        assert stats["transaction_count"] == 0
        # 分类/账户/tag 在创建账本时 ensure_default 可能种,允许 >= 0
        assert stats["category_count"] >= 0
        assert stats["account_count"] >= 0
    finally:
        app.dependency_overrides.clear()


def test_search_empty_query_returns_empty(monkeypatch) -> None:
    client, session_maker = _make_client_and_engine(monkeypatch)
    try:
        u = _register(client, email="search@example.com")
        token = u["access_token"]
        _make_ledger(client, token, "S")
        user = _fetch_user(session_maker, "search@example.com")

        # 空 query 直接短路
        out = read_tools.search(user, q="")
        assert out == []
        out = read_tools.search(user, q="   ")
        assert out == []
    finally:
        app.dependency_overrides.clear()


def test_merge_default_tag_dedupes_and_preserves_order() -> None:
    """单元层校验 MCP 默认标签合并逻辑 — 不需要 DB。"""
    from src.mcp.tools.write_tools import _MCP_DEFAULT_TAG, _merge_default_tag

    # LLM 没传 → 只有 MCP
    assert _merge_default_tag(None) == [_MCP_DEFAULT_TAG]
    assert _merge_default_tag([]) == [_MCP_DEFAULT_TAG]

    # LLM 传若干 → MCP 在末尾
    assert _merge_default_tag(["coffee", "work"]) == ["coffee", "work", _MCP_DEFAULT_TAG]

    # LLM 已经传了 MCP → 不重复
    out = _merge_default_tag([_MCP_DEFAULT_TAG, "coffee"])
    assert out == [_MCP_DEFAULT_TAG, "coffee"]

    # 空白 / 空字符串过滤
    assert _merge_default_tag(["  ", "", "x"]) == ["x", _MCP_DEFAULT_TAG]


def test_get_analytics_summary_empty_ledger(monkeypatch) -> None:
    client, session_maker = _make_client_and_engine(monkeypatch)
    try:
        u = _register(client, email="analytics@example.com")
        token = u["access_token"]
        _make_ledger(client, token, "A")
        user = _fetch_user(session_maker, "analytics@example.com")

        summary = read_tools.get_analytics_summary(user, scope="month")
        assert summary["ledger"] == "A"
        assert summary["income"] == 0
        assert summary["expense"] == 0
        assert summary["balance"] == 0
        assert summary["transaction_count"] == 0
        assert summary["top_categories"] == []
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# v30 交易级多币种:MCP 记账折算 helper(_build_currency_fields)
# ---------------------------------------------------------------------------


def test_mcp_currency_fields_base_currency_no_fields(monkeypatch) -> None:
    """交易币种==账本本位币 → 不产生两字段(server 落 NULL,统计 COALESCE 回退)。"""
    import asyncio
    from src.mcp.tools import write_tools

    client, sm = _make_client_and_engine(monkeypatch)
    monkeypatch.setattr(write_tools, "SessionLocal", sm)
    try:
        reg = _register(client)
        user = _fetch_user(sm, "tools@example.com")
        fields = asyncio.run(write_tools._build_currency_fields(
            user, ledger_base="CNY", account_currency="CNY",
            currency_arg=None, amount=100.0,
        ))
        assert fields == {}
    finally:
        app.dependency_overrides.clear()


def test_mcp_currency_fields_foreign_auto_rate(monkeypatch) -> None:
    """外币无 override、走自动源:1 CNY = 0.14 USD(fetcher base→quote) →
    12 USD 折 CNY = 12 / 0.14 ≈ 85.7(方向:quote 金额折 base 要除)。"""
    import asyncio
    from src.mcp.tools import write_tools
    from src.services.exchange_rate import fetcher as rate_fetcher

    client, sm = _make_client_and_engine(monkeypatch)
    monkeypatch.setattr(write_tools, "SessionLocal", sm)

    class _Row:
        payload_json = {"USD": 0.14, "JPY": 20.0}

    async def fake_get_rates(db, base):
        assert base == "CNY"
        return _Row(), False

    monkeypatch.setattr(rate_fetcher, "get_rates", fake_get_rates)
    try:
        _register(client)
        user = _fetch_user(sm, "tools@example.com")
        fields = asyncio.run(write_tools._build_currency_fields(
            user, ledger_base="CNY", account_currency="USD",
            currency_arg=None, amount=12.0,
        ))
        assert fields["currency_code"] == "USD"
        assert abs(fields["native_amount"] - 12.0 / 0.14) < 1e-6
    finally:
        app.dependency_overrides.clear()


def test_mcp_currency_fields_missing_rate_falls_back_to_amount(monkeypatch) -> None:
    """外币且拉不到汇率 → native=amount(1:1),currency_code 仍落
    (Web 改主币种重算 / App L11 横幅可捞回,绝不丢币种)。"""
    import asyncio
    from src.mcp.tools import write_tools
    from src.services.exchange_rate import fetcher as rate_fetcher

    client, sm = _make_client_and_engine(monkeypatch)
    monkeypatch.setattr(write_tools, "SessionLocal", sm)

    async def boom(db, base):
        raise RuntimeError("rate source down")

    monkeypatch.setattr(rate_fetcher, "get_rates", boom)
    try:
        _register(client)
        user = _fetch_user(sm, "tools@example.com")
        fields = asyncio.run(write_tools._build_currency_fields(
            user, ledger_base="CNY", account_currency="THB",
            currency_arg=None, amount=500.0,
        ))
        assert fields["currency_code"] == "THB"
        assert fields["native_amount"] == 500.0
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# create_transactions 批量类型契约
#
# 历史 bug:工具签名是 `list[dict[str, Any]]`,生成的 JSON Schema 为
# `{"type":"object","additionalProperties":true}` —— 无 properties / required /
# amount 类型约束。LLM 照账单字面值传 `{"amount":"38.00"}`,`Any` 不强转,
# 原始 str 撞上 normalize 循环的 `isinstance(amount,(int,float))`,报出
# `transactions[0]: amount must be a positive number` —— 与真实原因无关的误导
# 错误,而完全相同的数据走单条 create_transaction 一次就成功。
# 签名改成 `list[BatchTxItem]`(TypedDict)后 pydantic 会强转且 validate 后仍是
# plain dict。下面两个测试分别锁「schema 契约」和「字符串金额真能落库」。
# ---------------------------------------------------------------------------


def _tool_schema(name: str) -> dict:
    """从 FastMCP 真实注册表读工具 schema —— LLM 看到的就是它。"""
    tools = asyncio.run(mcp.list_tools())
    tool = next(t for t in tools if t.name == name)
    return tool.inputSchema


def _resolve_item_schema(tool_schema: dict) -> dict:
    """展开 transactions.items 的 $ref,拿回 BatchTxItem 的实际定义。"""
    items = tool_schema["properties"]["transactions"]["items"]
    ref = items.get("$ref")
    assert ref, f"expected $ref for items, got {items}"
    defs = tool_schema.get("$defs") or tool_schema.get("definitions") or {}
    return defs[ref.split("/")[-1]]


def test_create_transactions_schema_declares_numeric_amount() -> None:
    """回归锁:批量 item 的 amount 必须声明为 number 且 required。

    这正是原 bug 的根因 —— 声明缺失让 schema 退化成无约束 object。
    断言挂在真实 MCP 注册表上,不是挂在类型标注上,所以真有人把签名改回
    `list[dict[str, Any]]` 会立刻红。
    """
    item = _resolve_item_schema(_tool_schema("create_transactions"))
    assert item["properties"]["amount"]["type"] == "number"
    assert "amount" in item["required"]
    # 其余字段必须显式列出,LLM 才知道能传什么
    for key in ("tx_type", "category", "account", "happened_at", "note", "tags"):
        assert key in item["properties"], f"{key} missing from item schema"


def test_create_transaction_schema_still_numeric() -> None:
    """单条路径本就正常,这里防 TypedDict 改动波及它。"""
    schema = _tool_schema("create_transaction")
    assert schema["properties"]["amount"]["type"] == "number"


def test_batch_item_schema_coerces_numeric_string() -> None:
    """`"38.00"` 经 schema 校验后必须是 float,才能过 isinstance 守卫。

    走 pydantic TypeAdapter —— 与 FastMCP 生成/校验 tool 入参同一条路径。
    """
    import pydantic

    item = _resolve_item_schema(_tool_schema("create_transactions"))
    # 用真实的注册 schema 反推校验行为:amount 声明为 number 时字符串被强转
    adapter = pydantic.TypeAdapter(list[write_tools.BatchTxItem])
    parsed = adapter.validate_python([{"amount": "38.00", "category": "Food"}])
    assert isinstance(parsed, list)
    assert isinstance(parsed[0], dict), "must stay plain dict for raw.get()"
    amount = parsed[0]["amount"]
    assert isinstance(amount, float) and amount == 38.0
    assert not isinstance(amount, bool)
    # 原 bug 的守卫现在必须通过
    assert isinstance(amount, (int, float))
    assert parsed[0].get("category") == "Food"
    # schema 里 amount 确实是 number —— 强转行为由此而来
    assert item["properties"]["amount"]["type"] == "number"


def test_batch_item_schema_enforces_required_and_numeric() -> None:
    """schema 层只负责两件事:`amount` 必填 + 必须是 number。

    **正负不在 schema 层** —— `amount: float` 会放行 0 / 负数(实测确认)。
    `amount <= 0` 的守卫在 normalize 循环
    (`write_tools.create_transactions` 里 `isinstance` 之后那一行),属于运行时
    校验。这里如实锁住边界,避免误以为 schema 已经拦了非正数。

    (若日后想把 `exclusiveMinimum: 0` 也提到 schema —— 让 LLM 直接看到约束而
    不是等运行时错误 —— 需改 `BatchTxItem.amount` 为
    `Annotated[float, Field(gt=0)]`,届时补断言。)
    """
    import pydantic

    adapter = pydantic.TypeAdapter(list[write_tools.BatchTxItem])
    # 必填:缺失即拒
    with pytest.raises(pydantic.ValidationError):
        adapter.validate_python([{"category": "Food"}])
    # 类型:非数字串(如对账单里混入的 "N/A")在 schema 层就拒
    with pytest.raises(pydantic.ValidationError):
        adapter.validate_python([{"amount": "N/A"}])
    # 正负:schema 放行,留给运行时守卫
    assert adapter.validate_python([{"amount": 0}])[0]["amount"] == 0.0
    assert adapter.validate_python([{"amount": -1}])[0]["amount"] == -1.0


def _seed_category(client, hdr, sync_id: str, name: str = "餐饮") -> None:
    client.post(
        "/api/v1/sync/push",
        headers=hdr,
        json={"device_id": "d-app", "changes": [{
            "ledger_id": "lg1", "entity_type": "category",
            "entity_sync_id": sync_id, "action": "upsert",
            "updated_at": "2026-10-03T00:00:00+00:00",
            "payload": {"syncId": sync_id, "name": name,
                        "kind": "expense", "level": 1},
        }]},
    )


def test_mcp_create_budget(monkeypatch) -> None:
    """MCP 建总预算 —— 此前 MCP 只有 update_budget,新建预算必须去 Web,
    而记账全走 MCP 时这是个别扭的断点。"""
    import asyncio
    from datetime import timedelta

    from src.mcp.tools import write_tools
    from src.security import SCOPE_APP_WRITE, SCOPE_WEB_WRITE, _create_token

    client, session_maker = _make_client_and_engine(monkeypatch)
    monkeypatch.setattr(write_tools, "SessionLocal", session_maker)
    monkeypatch.setattr(
        write_tools, "_internal_token",
        lambda u: _create_token(
            sub=u.id, token_type="access", expires_delta=timedelta(seconds=60),
            scopes=[SCOPE_APP_WRITE, SCOPE_WEB_WRITE], client_type="app",
        ),
    )
    try:
        _register(client)
        user = _fetch_user(session_maker, "tools@example.com")
        hdr = {"Authorization": "Bearer x"}
        # 建账本 + 分类
        r = client.post("/api/v1/write/ledgers",
                        headers={"Authorization": "Bearer " + _login_token(client),
                                 "X-Device-ID": "d-web"},
                        json={"ledger_id": "lg1", "ledger_name": "Main",
                              "currency": "CNY"})
        assert r.status_code == 200, r.text

        out = asyncio.run(write_tools.create_budget(user, amount=30000.0))
        assert out["sync_id"], out
        assert out["budget_type"] == "total"
        assert out["amount"] == 30000.0
    finally:
        app.dependency_overrides.clear()


def test_mcp_create_budget_validation() -> None:
    """参数校验要发生在 self-call 之前,给 LLM 可读报错。"""
    import asyncio

    from src.mcp.tools import write_tools

    with pytest.raises(ValueError, match="budget_type"):
        asyncio.run(write_tools.create_budget(None, amount=100.0, budget_type="bogus"))
    with pytest.raises(ValueError, match="period"):
        asyncio.run(write_tools.create_budget(None, amount=100.0, period="daily"))
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(write_tools.create_budget(None, amount=0.0))
    with pytest.raises(ValueError, match="category is required"):
        asyncio.run(write_tools.create_budget(
            None, amount=100.0, budget_type="category"))


def test_mcp_create_budget_registered() -> None:
    """MCP 注册表里必须有 create_budget,且参数完整 —— schema 缺参数 LLM 就传不进来。"""
    import asyncio

    from src.mcp.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "create_budget" in tools, sorted(tools)
    props = tools["create_budget"].inputSchema["properties"]
    for key in ("amount", "budget_type", "category", "period"):
        assert key in props, sorted(props)
