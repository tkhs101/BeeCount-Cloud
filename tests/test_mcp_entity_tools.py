"""MCP 实体管理工具(账户 / 标签 / 分类 / 预算)。

补的是官方 18 个 tool 的洞:账户和标签**只有读、没有写** —— 转账必须选账户,
打标签是分析的常规操作,缺了这两个用户只能先去 Web 手建。

重点验三件事:
1. **两阶段确认**:delete_* 在 confirm=false 时必须只返占位符、不动数据
2. **按名字查找**:LLM 手里通常只有「招行卡」这种名字,没有 sync_id
3. **余额公式**:期初 + 收入 − 支出 − 转出 + 转入,转账两侧分别计
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.models import User
from src.main import app
from src.mcp.tools import entity_tools, write_tools
from src.security import SCOPE_APP_WRITE, SCOPE_WEB_READ, SCOPE_WEB_WRITE, _create_token


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
    """MCP 的 self-call 打内部 HTTP;这里的短期 token 要同时带 web scope,
    测试环境的 `ALLOW_APP_RW_SCOPES` 被 conftest 钉成 false(见 conftest.py)。"""
    from src.mcp.tools import read_tools
    monkeypatch.setattr(write_tools, "SessionLocal", TS)
    monkeypatch.setattr(entity_tools, "SessionLocal", TS)
    monkeypatch.setattr(read_tools, "SessionLocal", TS)
    monkeypatch.setattr(
        write_tools, "_internal_token",
        lambda u: _create_token(
            sub=u.id, token_type="access", expires_delta=timedelta(seconds=60),
            scopes=[SCOPE_APP_WRITE, SCOPE_WEB_WRITE], client_type="app",
        ),
    )


def _register(client: TestClient, email: str) -> tuple[str, User]:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "EntTest1!x", "client_type": "web",
              "device_name": "d", "platform": "test"},
    )
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    hdr = {"Authorization": f"Bearer {tok}", "X-Device-ID": "d"}
    r = client.post("/api/v1/write/ledgers", headers={**hdr, "Content-Type": "application/json"},
                    json={"ledger_id": "lg1", "ledger_name": "L", "currency": "JPY"})
    assert r.status_code == 200, r.text
    return tok, hdr


def _user(TS, email: str) -> User:
    with TS() as db:
        row = db.scalar(select(User).where(User.email == email))
        assert row is not None
        db.expunge(row)
        return row


# --------------------------------------------------------------------------- #
# 账户                                                                          #
# --------------------------------------------------------------------------- #


def test_create_and_list_account(monkeypatch) -> None:
    client, TS = _make_client()
    monkeypatch.setattr(write_tools, "SessionLocal", TS)
    _wire(monkeypatch, TS)
    try:
        _tok, hdr = _register(client, "acc1@t.com")
        user = _user(TS, "acc1@t.com")

        out = asyncio.run(entity_tools.create_account(
            user, name="招行卡", account_type="bank_card",
            currency="JPY", initial_balance=50000.0, bank_name="招商银行"))
        assert out["sync_id"], out

        from src.mcp.tools import read_tools
        got = [a["name"] for a in read_tools.list_accounts(user)]
        assert "招行卡" in got, got
    finally:
        app.dependency_overrides.clear()


def test_create_account_rejects_bad_type(monkeypatch) -> None:
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _register(client, "acc2@t.com")
        user = _user(TS, "acc2@t.com")
        with pytest.raises(ValueError, match="Invalid account_type"):
            asyncio.run(entity_tools.create_account(user, name="x", account_type="nope"))
    finally:
        app.dependency_overrides.clear()


def test_update_account_by_name(monkeypatch) -> None:
    """LLM 手里通常只有名字,没有 sync_id。"""
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _register(client, "acc3@t.com")
        user = _user(TS, "acc3@t.com")
        asyncio.run(entity_tools.create_account(user, name="招行卡",
                                               account_type="bank_card"))
        out = asyncio.run(entity_tools.update_account(user, account="招行卡",
                                                      credit_limit=50000.0))
        assert out["updated"] == ["credit_limit"], out

        from src.mcp.tools import read_tools
        got = {a["name"]: a for a in read_tools.list_accounts(user)}
        assert got["招行卡"].get("credit_limit") == 50000.0, got["招行卡"]
    finally:
        app.dependency_overrides.clear()


def test_update_account_unknown_name_lists_options(monkeypatch) -> None:
    """查不到时报错要**列出可选项**,让 LLM 自我纠正而不是瞎猜。"""
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _register(client, "acc4@t.com")
        user = _user(TS, "acc4@t.com")
        asyncio.run(entity_tools.create_account(user, name="招行卡",
                                               account_type="bank_card"))
        with pytest.raises(ValueError) as exc:
            asyncio.run(entity_tools.update_account(user, account="不存在的卡",
                                                      note="x"))
        assert "招行卡" in str(exc.value), str(exc.value)
    finally:
        app.dependency_overrides.clear()


def test_account_balance_formula(monkeypatch) -> None:
    """期初 1000 + 收入 500 − 支出 200 − 转出 300 + 转入 700 = 1700。"""
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _tok, hdr = _register(client, "bal@t.com")
        user = _user(TS, "bal@t.com")
        J = {**hdr, "Content-Type": "application/json"}

        a = asyncio.run(entity_tools.create_account(
            user, name="工资卡", account_type="bank_card", initial_balance=1000.0))
        b = asyncio.run(entity_tools.create_account(
            user, name="消费卡", account_type="credit_card"))
        assert a["sync_id"] and b["sync_id"]

        ts = "2026-10-03T12:00:00+00:00"
        for payload in (
            {"tx_type": "income", "amount": 500.0, "account_name": "工资卡",
             "happened_at": ts},
            {"tx_type": "expense", "amount": 200.0, "account_name": "工资卡",
             "happened_at": ts},
            {"tx_type": "transfer", "amount": 300.0, "happened_at": ts,
             "from_account_name": "工资卡", "to_account_name": "消费卡"},
            {"tx_type": "transfer", "amount": 700.0, "happened_at": ts,
             "from_account_name": "消费卡", "to_account_name": "工资卡"},
        ):
            r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=J,
                            json={"base_change_id": 0, **payload})
            assert r.status_code == 200, r.text

        out = asyncio.run(entity_tools.get_account_balance(user))
        by = {a["name"]: a["balance"] for a in out["accounts"]}
        # 工资卡:1000 +500 -200 -300 +700 = 1700
        assert by["工资卡"] == 1700.0, by
        # 消费卡:0 +0 -0 -700 +300 = -400(信用卡欠款为负)
        assert by["消费卡"] == -400.0, by
        assert out["total_balance"] == 1300.0, out["total_balance"]
    finally:
        app.dependency_overrides.clear()


def test_account_balance_single_account(monkeypatch) -> None:
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _tok, hdr = _register(client, "bal2@t.com")
        user = _user(TS, "bal2@t.com")
        asyncio.run(entity_tools.create_account(user, name="A",
                                               account_type="cash",
                                               initial_balance=100.0))
        asyncio.run(entity_tools.create_account(user, name="B",
                                               account_type="cash",
                                               initial_balance=999.0))
        out = asyncio.run(entity_tools.get_account_balance(user, account="A"))
        assert len(out["accounts"]) == 1, out
        assert out["accounts"][0]["name"] == "A"
        assert out["total_balance"] == 100.0
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 标签                                                                          #
# --------------------------------------------------------------------------- #


def test_tag_crud(monkeypatch) -> None:
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _register(client, "tag1@t.com")
        user = _user(TS, "tag1@t.com")

        created = asyncio.run(entity_tools.create_tag(user, name="咖啡",
                                                     color="#3B82F6"))
        assert created["sync_id"], created

        from src.mcp.tools import read_tools
        assert "咖啡" in [t["name"] for t in read_tools.list_tags(user)]

        upd = asyncio.run(entity_tools.update_tag(user, tag="咖啡", name="咖啡日"))
        assert upd["updated"] == ["name"], upd
        assert "咖啡日" in [t["name"] for t in read_tools.list_tags(user)]
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 两阶段确认(删除类)                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", ["account", "tag", "category", "budget"])
def test_deletes_require_confirmation(monkeypatch, kind: str) -> None:
    """confirm=false 时只返占位符,**不动数据**;确认后才真删。"""
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _tok, hdr = _register(client, f"del-{kind}@t.com")
        user = _user(TS, f"del-{kind}@t.com")

        if kind == "account":
            ident = asyncio.run(entity_tools.create_account(
                user, name="待删账户", account_type="cash"))["sync_id"]
            fn = entity_tools.delete_account
            kwargs = {"account_id": ident}
        elif kind == "tag":
            ident = asyncio.run(entity_tools.create_tag(user, name="待删标签"))["sync_id"]
            fn = entity_tools.delete_tag
            kwargs = {"tag_id": ident}
        elif kind == "category":
            ident = asyncio.run(write_tools.create_category(
                user, name="待删分类"))["sync_id"]
            fn = entity_tools.delete_category
            kwargs = {"category_id": ident}
        else:
            ident = asyncio.run(write_tools.create_budget(
                user, amount=1000.0))["sync_id"]
            fn = entity_tools.delete_budget
            kwargs = {"budget_id": ident}

        # 第一次:不确认
        out = asyncio.run(fn(user, confirm=False, **kwargs))
        assert out.get("status") == "confirmation_required", out
        assert "confirm=true" in out["message"], out

        # 第二次:确认
        out2 = asyncio.run(fn(user, confirm=True, **kwargs))
        assert out2.get("deleted") is True, out2
    finally:
        app.dependency_overrides.clear()


def test_delete_account_refused_while_transactions_exist(monkeypatch) -> None:
    """账户上还有交易时服务端会拒绝 —— 错误必须原样带回给 LLM,而不是吞掉。"""
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _tok, hdr = _register(client, "delbusy@t.com")
        user = _user(TS, "delbusy@t.com")
        J = {**hdr, "Content-Type": "application/json"}
        asyncio.run(entity_tools.create_account(user, name="忙账户",
                                               account_type="bank_card"))
        r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=J,
                        json={"base_change_id": 0, "tx_type": "expense",
                              "amount": 100.0, "account_name": "忙账户",
                              "happened_at": "2026-10-03T12:00:00+00:00"})
        assert r.status_code == 200, r.text

        with pytest.raises(RuntimeError):
            asyncio.run(entity_tools.delete_account(user, account="忙账户",
                                                   confirm=True))
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 分类 / 预算                                                                   #
# --------------------------------------------------------------------------- #


def test_update_category_by_name(monkeypatch) -> None:
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _register(client, "cat1@t.com")
        user = _user(TS, "cat1@t.com")
        asyncio.run(write_tools.create_category(user, name="旧名字"))
        out = asyncio.run(entity_tools.update_category(user, category="旧名字",
                                                       name="新名字"))
        assert out["updated"] == ["name"], out

        from src.mcp.tools import read_tools
        names = {c["name"] for c in read_tools.list_categories(user, kind="expense")}
        assert "新名字" in names and "旧名字" not in names, names
    finally:
        app.dependency_overrides.clear()


def test_budget_crud_roundtrip(monkeypatch) -> None:
    """create → list → update → delete 全链路(补齐预算的 delete 缺口)。"""
    client, TS = _make_client()
    _wire(monkeypatch, TS)
    try:
        _register(client, "bud1@t.com")
        user = _user(TS, "bud1@t.com")
        created = asyncio.run(write_tools.create_budget(user, amount=30000.0))
        bid = created["sync_id"]
        assert bid

        from src.mcp.tools import read_tools
        assert any(b["id"] == bid for b in read_tools.list_budgets(user))

        asyncio.run(write_tools.update_budget(user, budget_id=bid, amount=40000.0))
        got = {b["id"]: b for b in read_tools.list_budgets(user)}
        assert got[bid]["amount"] == 40000.0, got[bid]

        out = asyncio.run(entity_tools.delete_budget(user, budget_id=bid, confirm=True))
        assert out.get("deleted") is True
        assert not any(b["id"] == bid
                       for b in read_tools.list_budgets(user))
    finally:
        app.dependency_overrides.clear()


def test_scope_enforcement(monkeypatch) -> None:
    """get_account_balance 是 mcp:read 工具,其余都是 mcp:write。"""
    from src.mcp.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    # 注册表存在性
    for name in ("create_account", "update_account", "delete_account",
                 "get_account_balance", "create_tag", "update_tag",
                 "delete_tag", "update_category", "delete_category",
                 "delete_budget"):
        assert name in tools, f"{name} 未注册"
        props = tools[name].inputSchema["properties"]
        if name != "delete_budget":
            assert "ledger_id" in props, f"{name} 缺 ledger_id"
