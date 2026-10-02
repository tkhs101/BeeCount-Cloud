"""消费税税额(tax_amount,0020)Cloud 端契约。

背景见 docs/aegis/plans/2026-10-03-selfhost-tax-feature-fork.md(T4)。

设计要点:
- **amount 语义不变,仍是实付总额**。税额是叠加维度,统计时从 amount 里剥出
  归入「税与保险」,两块相加恒等于实付 —— 这是整个功能的核心不变式。
- **只存原币**。折本位币的税额由统计侧按
  `native_amount * (tax_amount / amount)` 推导,不落库,避免重演
  native_amount 当年「改了 amount 忘了改折算值」的联动 bug
  (docs/SYNC_ARCHITECTURE.md §4.5)。
- **不与 amount 联动**。税是小票上的绝对值,各家舍入不同
  (1780 ÷ 1.08 = 1648.15 与收银机显示的 1649 差 1 円),等比缩放只会
  制造 0.5 円这种对不上账的数。
- **nullable 无回填**。存量行 = 无税,统计与升级前完全一致。
- **预算恒按全额**(D4),不跟随分类切片口径。

测试基建与 test_tx_multi_currency.py 同套。
"""
from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import get_settings
from src.database import Base, get_db
from src.main import app
from src.models import Ledger, ReadTxProjection, User

TAX_BUCKET = get_settings().tax_category_name.strip() or "税与保险"


def _make_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


def _iso(dt=None):
    return (dt or datetime.now(timezone.utc)).isoformat()


def _register_and_token(client, email, *, device_id, client_type) -> str:
    creds = {
        "email": email,
        "password": "Pa$$word1!",
        "device_id": device_id,
        "client_type": client_type,
        "device_name": f"pytest-{client_type}",
        "platform": "test",
    }
    client.post("/api/v1/auth/register", json=creds)
    r = client.post("/api/v1/auth/login", json=creds)
    return r.json()["access_token"]


def _two_tokens(client, email):
    return (
        _register_and_token(client, email, device_id="d-app", client_type="app"),
        _register_and_token(client, email, device_id="d-web", client_type="web"),
    )


def _push(client, hdr, ledger_id, entity_type, sync_id, payload, *, action="upsert"):
    body = {
        "ledger_id": ledger_id,
        "entity_type": entity_type,
        "entity_sync_id": sync_id,
        "action": action,
        "updated_at": _iso(),
        "payload": payload,
    }
    r = client.post(
        "/api/v1/sync/push",
        headers=hdr,
        json={"device_id": "d-app", "changes": [body]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _ledger_internal_id(TS, external_id):
    with TS() as db:
        return db.scalar(select(Ledger.id).where(Ledger.external_id == external_id))


def _get_tx(TS, ledger_internal_id, sync_id):
    with TS() as db:
        return db.scalar(
            select(ReadTxProjection).where(
                ReadTxProjection.ledger_id == ledger_internal_id,
                ReadTxProjection.sync_id == sync_id,
            )
        )


def _fetch_user(TS, email):
    with TS() as db:
        row = db.scalar(select(User).where(User.email == email))
        assert row is not None
        db.expunge(row)
        return row


def _web_create(client, hdr, ledger_id, *, base, payload):
    r = client.post(
        f"/api/v1/write/ledgers/{ledger_id}/transactions",
        headers=hdr,
        json={"base_change_id": base, **payload},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _web_ledger(client, hdr, ledger_id="lg1", currency="CNY", name="TaxLedger"):
    r = client.post(
        "/api/v1/write/ledgers",
        headers=hdr,
        json={"ledger_id": ledger_id, "ledger_name": name, "currency": currency},
    )
    assert r.status_code == 200, r.text
    return r.json()["new_change_id"]


def _load_migration_0020():
    path = (
        Path(__file__).parent.parent / "alembic" / "versions" / "0020_tx_tax_amount.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0020", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _seed_taxed_expense(client, hdr, *, amount=3280.0, tax=298.0, category="餐饮"):
    """mobile push 一笔含税支出:实付 3280 / 税 298(税率约 10%)。"""
    _push(
        client, hdr, "lg1", "transaction", "tx1",
        {"syncId": "tx1", "type": "expense", "amount": amount,
         "happenedAt": _iso(), "categoryId": "c1", "categoryName": category,
         "categoryKind": "expense", "taxAmount": tax},
    )


# --------------------------------------------------------------------------- #
# 1. 模型 / 迁移                                                              #
# --------------------------------------------------------------------------- #


def test_projection_has_tax_amount_column():
    """read_tx_projection 带 tax_amount 列且可空(NULL = 无税)。"""
    client, TS = _make_client()
    try:
        with TS() as db:
            cols = {c["name"]: c for c in sa.inspect(db.get_bind()).get_columns("read_tx_projection")}
        assert "tax_amount" in cols, sorted(cols)
        assert cols["tax_amount"]["nullable"] is True
    finally:
        app.dependency_overrides.clear()


def test_migration_0020_is_nullable_add_column_no_backfill():
    """0020 是纯 nullable 加列,**不做回填** —— 存量行保持无税。"""
    mod = _load_migration_0020()
    assert mod.revision == "0020_tx_tax_amount"
    assert mod.down_revision == "0019_account_hidden"
    src = Path(
        Path(__file__).parent.parent / "alembic" / "versions" / "0020_tx_tax_amount.py"
    ).read_text(encoding="utf-8")
    assert "nullable=True" in src
    assert "tax_amount\"" in src
    # 迁移链连续:0020 的上家必须是当前 head
    assert not hasattr(mod, "BACKFILL_STATEMENT")


# --------------------------------------------------------------------------- #
# 2. 写入:projection upsert + snapshot 往返                                     #
# --------------------------------------------------------------------------- #


def test_upsert_tx_writes_tax_amount():
    """payload 带 taxAmount → 落 tax_amount 列。"""
    client, TS = _make_client()
    try:
        app_token, _ = _two_tokens(client, "tax-upsert@t.com")
        hdr = {"Authorization": f"Bearer {app_token}"}
        _seed_taxed_expense(client, hdr)
        row = _get_tx(TS, _ledger_internal_id(TS, "lg1"), "tx1")
        assert row is not None
        assert row.tax_amount == 298.0
    finally:
        app.dependency_overrides.clear()


def test_upsert_tx_legacy_payload_leaves_null():
    """旧 payload(无 taxAmount)→ NULL,不产生 0。统计端按「无税」处理。"""
    client, TS = _make_client()
    try:
        app_token, _ = _two_tokens(client, "tax-legacy@t.com")
        hdr = {"Authorization": f"Bearer {app_token}"}
        _push(client, hdr, "lg1", "transaction", "txA",
              {"syncId": "txA", "type": "expense", "amount": 100.0,
               "happenedAt": _iso(), "categoryName": "餐饮"})
        row = _get_tx(TS, _ledger_internal_id(TS, "lg1"), "txA")
        assert row is not None
        assert row.tax_amount is None
    finally:
        app.dependency_overrides.clear()


def test_mutator_create_with_tax():
    """snapshot_mutator.create_transaction → item 落 camelCase taxAmount;
    不传则不产生 key。"""
    from src.snapshot_mutator import create_transaction

    out, tx_id = create_transaction(
        {"items": [], "count": 0},
        {"tx_type": "expense", "amount": 3280.0, "happened_at": _iso(), "tax_amount": 298.0},
    )
    assert out["items"][0]["taxAmount"] == 298.0

    out2, _ = create_transaction(
        {"items": [], "count": 0},
        {"tx_type": "expense", "amount": 100.0, "happened_at": _iso()},
    )
    assert "taxAmount" not in out2["items"][0]


def test_mutator_update_patch_semantics():
    """PATCH:不传 = 不变;显式传 null = 清除;传值 = 覆盖。"""
    from src.snapshot_mutator import update_transaction

    snap = {"items": [{"syncId": "tx1", "type": "expense", "amount": 3280.0,
                       "happenedAt": _iso(), "taxAmount": 298.0}], "count": 1}
    # 不传 tax_amount → 不变
    out = update_transaction(snap, "tx1", {"amount": 3500.0})
    assert out["items"][0]["taxAmount"] == 298.0, "改 amount 不得联动缩放税额"
    assert out["items"][0]["amount"] == 3500.0

    # 显式 null → 清除
    out2 = update_transaction(snap, "tx1", {"tax_amount": None})
    assert "taxAmount" not in out2["items"][0]

    # 传值 → 覆盖
    out3 = update_transaction(snap, "tx1", {"tax_amount": 315.0})
    assert out3["items"][0]["taxAmount"] == 315.0


def test_mutator_rejects_inconsistent_tax():
    """0 / 负数 / >= amount / 非 expense 上的税 —— 一律拒。"""
    import pytest

    from src.snapshot_mutator import create_transaction

    with pytest.raises(ValueError, match="must be positive"):
        create_transaction({"items": [], "count": 0},
                           {"tx_type": "expense", "amount": 100.0,
                            "happened_at": _iso(), "tax_amount": 0.0})
    with pytest.raises(ValueError, match="must be positive"):
        create_transaction({"items": [], "count": 0},
                           {"tx_type": "expense", "amount": 100.0,
                            "happened_at": _iso(), "tax_amount": -5.0})
    with pytest.raises(ValueError, match="less than amount"):
        create_transaction({"items": [], "count": 0},
                           {"tx_type": "expense", "amount": 100.0,
                            "happened_at": _iso(), "tax_amount": 100.0})
    with pytest.raises(ValueError, match="only allowed on expense"):
        create_transaction({"items": [], "count": 0},
                           {"tx_type": "income", "amount": 100.0,
                            "happened_at": _iso(), "tax_amount": 10.0})


def test_projection_row_to_tx_dict_carries_tax_amount():
    """**R1 回归锁** —— projection→snapshot 反向桥必须带上税额。

    漏了它:web PATCH update_tx 快路径的 prev_item 缺 taxAmount →
    snapshot_mutator 看不到旧税额 → upsert 写 NULL,**用户记的税额被
    静默抹掉且不报错**。nativeAmount 当年就踩过这个坑。
    """
    from src.routers.write._shared import _projection_row_to_tx_dict

    client, TS = _make_client()
    try:
        app_token, _ = _two_tokens(client, "tax-bridge@t.com")
        hdr = {"Authorization": f"Bearer {app_token}"}
        _seed_taxed_expense(client, hdr)
        row = _get_tx(TS, _ledger_internal_id(TS, "lg1"), "tx1")
        item = _projection_row_to_tx_dict(row)
        assert item.get("taxAmount") == 298.0, (
            "反向桥丢了税额 —— web PATCH 会静默抹掉它"
        )
    finally:
        app.dependency_overrides.clear()


def test_snapshot_builder_keeps_tax_amount():
    """snapshot_builder.build 的 select/解包/序列化三处对齐,税额不丢
    (backup restore 与 /sync/full 依赖它)。"""
    from src.snapshot_builder import build

    client, TS = _make_client()
    try:
        app_token, _ = _two_tokens(client, "tax-snap@t.com")
        hdr = {"Authorization": f"Bearer {app_token}"}
        _seed_taxed_expense(client, hdr)
        lid = _ledger_internal_id(TS, "lg1")
        with TS() as db:
            ledger = db.get(Ledger, lid)
            snap = build(db, ledger)
        tx = next(i for i in snap["items"] if i["syncId"] == "tx1")
        assert tx.get("taxAmount") == 298.0
    finally:
        app.dependency_overrides.clear()


def test_mobile_push_partial_update_keeps_tax_amount():
    """**merge 契约**(CLAUDE.md 硬要求)—— 增量 push 只带别的字段时,
    已有税额不能被 merge 丢掉。这就是 2026-04 那类「漏 merge 某字段」bug
    的标准形态。"""
    client, TS = _make_client()
    try:
        app_token, _ = _two_tokens(client, "tax-merge@t.com")
        hdr = {"Authorization": f"Bearer {app_token}"}
        _seed_taxed_expense(client, hdr)
        # 只改 note,不带 taxAmount
        _push(client, hdr, "lg1", "transaction", "tx1",
              {"syncId": "tx1", "type": "expense", "amount": 3280.0,
               "happenedAt": _iso(), "categoryId": "c1", "categoryName": "餐饮",
               "categoryKind": "expense", "note": "改个备注"})
        row = _get_tx(TS, _ledger_internal_id(TS, "lg1"), "tx1")
        assert row.tax_amount == 298.0, "partial push 把税额 merge 丢了"
        assert row.note == "改个备注"
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 3. Web 写路径 / 读路径                                                       #
# --------------------------------------------------------------------------- #


def test_web_create_and_read_roundtrip():
    """Web 建一笔含税交易 → 两个读端点都暴露 tax_amount。"""
    client, TS = _make_client()
    try:
        web_token = _register_and_token(client, "tax-web@t.com", device_id="d-web", client_type="web")
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        base = _web_ledger(client, hdr_web)
        res = _web_create(client, hdr_web, "lg1", base=base, payload={
            "tx_type": "expense", "amount": 3280.0, "happened_at": _iso(),
            "category_id": "c1", "category_name": "餐饮", "category_kind": "expense",
            "tax_amount": 298.0,
        })
        tx_id = res["entity_id"]
        row = _get_tx(TS, _ledger_internal_id(TS, "lg1"), tx_id)
        assert row.tax_amount == 298.0

        r1 = client.get("/api/v1/read/ledgers/lg1/transactions", headers=hdr_web)
        assert r1.status_code == 200, r1.text
        assert r1.json()[0]["tax_amount"] == 298.0

        r2 = client.get("/api/v1/read/workspace/transactions", headers=hdr_web)
        assert r2.status_code == 200, r2.text
        assert r2.json()["items"][0]["tax_amount"] == 298.0
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 4. 统计:核心不变式                                                          #
# --------------------------------------------------------------------------- #


def _analytics(client, hdr_web, **params):
    r = client.get("/api/v1/read/workspace/analytics", headers=hdr_web,
                   params={"scope": "all", "metric": "expense", **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_analytics_total_includes_full_amount_and_slices_split_tax():
    """**核心不变式**:总额 = 实付 3280;分类切片 餐饮 2982 + 税与保险 298
    = 3280。饼图两块相加等于实际付的钱。"""
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-ana@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        _seed_taxed_expense(client, hdr_app, amount=3280.0, tax=298.0, category="餐饮")

        body = _analytics(client, hdr_web)
        assert abs(body["summary"]["expense_total"] - 3280.0) < 1e-6, (
            "总额必须是实付金额,不能被税额拆分影响"
        )
        ranks = {row["category_name"]: row["total"] for row in body["category_ranks"]}
        assert abs(ranks["餐饮"] - 2982.0) < 1e-6, ranks
        assert abs(ranks[TAX_BUCKET] - 298.0) < 1e-6, ranks
        # 不变式:两块相加 = 实付
        assert abs(sum(ranks.values()) - 3280.0) < 1e-6
    finally:
        app.dependency_overrides.clear()


def test_analytics_unchanged_when_no_tax_data():
    """没有税额数据时,统计与升级前**逐字段一致**,不冒出空的税扇区。"""
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-noop@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        _push(client, hdr_app, "lg1", "transaction", "txN",
              {"syncId": "txN", "type": "expense", "amount": 500.0,
               "happenedAt": _iso(), "categoryName": "餐饮"})
        body = _analytics(client, hdr_web)
        assert abs(body["summary"]["expense_total"] - 500.0) < 1e-6
        names = [row["category_name"] for row in body["category_ranks"]]
        assert names == ["餐饮"], names
        assert TAX_BUCKET not in names
    finally:
        app.dependency_overrides.clear()


def test_analytics_tax_merges_with_manual_tax_category():
    """D1 语义:手动记在「税与保险」下的住民税,与从消费剥出的消费税
    **合并进同一个扇区**。"""
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-merge2@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        _seed_taxed_expense(client, hdr_app, amount=3280.0, tax=298.0, category="餐饮")
        # 住民税:独立支出,分类就是税与保险,无 tax_amount
        _push(client, hdr_app, "lg1", "transaction", "tx2",
              {"syncId": "tx2", "type": "expense", "amount": 50000.0,
               "happenedAt": _iso(), "categoryName": TAX_BUCKET})

        body = _analytics(client, hdr_web)
        assert abs(body["summary"]["expense_total"] - 53280.0) < 1e-6
        ranks = {row["category_name"]: row["total"] for row in body["category_ranks"]}
        assert abs(ranks[TAX_BUCKET] - (298.0 + 50000.0)) < 1e-6, ranks
        assert abs(ranks["餐饮"] - 2982.0) < 1e-6, ranks
        assert abs(sum(ranks.values()) - 53280.0) < 1e-6
    finally:
        app.dependency_overrides.clear()


def test_analytics_foreign_currency_tax_scales_with_rate():
    """外币交易:税额按比例法折本位币,与主金额同一个汇率,不漂移。"""
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-fx@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        # 原币 50 CNY,税 5 CNY,汇率 20 → 本位币 1000,税折 100
        _push(client, hdr_app, "lg1", "transaction", "txF",
              {"syncId": "txF", "type": "expense", "amount": 50.0,
               "happenedAt": _iso(), "categoryName": "餐饮",
               "currencyCode": "CNY", "nativeAmount": 1000.0, "taxAmount": 5.0})
        body = _analytics(client, hdr_web)
        assert abs(body["summary"]["expense_total"] - 1000.0) < 1e-6
        ranks = {row["category_name"]: row["total"] for row in body["category_ranks"]}
        assert abs(ranks["餐饮"] - 900.0) < 1e-6, ranks
        assert abs(ranks[TAX_BUCKET] - 100.0) < 1e-6, ranks
        assert abs(sum(ranks.values()) - 1000.0) < 1e-6
    finally:
        app.dependency_overrides.clear()


def test_excluded_from_stats_drops_tax_slice():
    """exclude_from_stats=True 的整笔(含税)都不计,税扇区不受影响。"""
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-excl@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        _seed_taxed_expense(client, hdr_app, amount=3280.0, tax=298.0)
        _push(client, hdr_app, "lg1", "transaction", "txX",
              {"syncId": "txX", "type": "expense", "amount": 1000.0,
               "happenedAt": _iso(), "categoryName": "餐饮",
               "taxAmount": 100.0, "excludeFromStats": True})
        body = _analytics(client, hdr_web)
        assert abs(body["summary"]["expense_total"] - 3280.0) < 1e-6
        ranks = {row["category_name"]: row["total"] for row in body["category_ranks"]}
        assert TAX_BUCKET in ranks and abs(ranks[TAX_BUCKET] - 298.0) < 1e-6
    finally:
        app.dependency_overrides.clear()


def test_budget_usage_still_full_amount():
    """D4 回归锁:预算用量恒按全额(含税),不跟随分类切片口径。"""
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-bud@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        _seed_taxed_expense(client, hdr_app, amount=3280.0, tax=298.0)
        _push(client, hdr_app, "lg1", "budget", "bud1",
              {"syncId": "bud1", "type": "total", "amount": 10000.0,
               "period": "monthly", "startDay": 1, "enabled": True})
        r = client.get("/api/v1/read/ledgers/lg1/budgets/usage", headers=hdr_web)
        assert r.status_code == 200, r.text
        items = {x["budget_id"]: x["used"] for x in r.json()["items"]}
        assert abs(items["bud1"] - 3280.0) < 1e-6, (
            f"预算必须按实付全额 3280,got {items.get('bud1')}"
        )
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 5. MCP 契约(用户的记账入口)                                                 #
# --------------------------------------------------------------------------- #


def test_mcp_write_tools_expose_tax_amount():
    """三个写工具的 MCP schema 都得带 tax_amount —— 用户所有记账走 MCP,
    schema 缺了 LLM 就永远传不进来。"""
    import asyncio

    from src.mcp.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("create_transaction", "update_transaction"):
        assert "tax_amount" in tools[name].inputSchema["properties"], name
    batch = tools["create_transactions"].inputSchema
    item_def = batch["$defs"]["BatchTxItem"]
    assert "tax_amount" in item_def["properties"], sorted(item_def["properties"])
    # 只有 amount 必填;税额可选(大多数消费没有税)
    assert item_def.get("required") == ["amount"], item_def.get("required")


def test_mcp_read_tools_report_tax_amount():
    """读工具回读要带 tax_amount,且**免税/未记税必须是 None 而不是 0** ——
    折成 0 就分不清「免税商品」和「没记税」。"""
    from src.mcp.tools.read_tools import _serialize_tx

    class FakeRow:
        sync_id = "tx1"
        tx_type = "expense"
        amount = 3280.0
        tax_amount = 298.0
        happened_at = datetime(2026, 10, 3, tzinfo=timezone.utc)
        note = "KING BEAR NOW"
        category_name = "餐饮"
        account_name = None
        from_account_name = None
        to_account_name = None
        tags_csv = ""
        currency_code = None
        native_amount = None
        attachments_json = None

    out = _serialize_tx(FakeRow(), None)
    assert out["amount"] == 3280.0, "amount 必须是实付总额"
    assert out["tax_amount"] == 298.0

    FakeRow.tax_amount = None
    assert _serialize_tx(FakeRow(), None)["tax_amount"] is None


def test_mcp_update_zero_means_clear(monkeypatch) -> None:
    """update_transaction 的「传 0 = 清除」约定:0 → patch 里的显式 null。

    PATCH 的 null 与「没传」在 MCP 层无法区分,所以用 0 当清除信号。
    """
    from src.mcp.tools import write_tools

    client, TS = _make_client()
    # write_tools 直接 `with SessionLocal()` 查 projection —— 必须打到测试库
    monkeypatch.setattr(write_tools, "SessionLocal", TS)
    try:
        app_token, _ = _two_tokens(client, "tax-clear@t.com")
        hdr = {"Authorization": f"Bearer {app_token}"}
        _seed_taxed_expense(client, hdr, amount=3280.0, tax=298.0)
        user = _fetch_user(TS, "tax-clear@t.com")

        captured: dict = {}

        async def fake_self_call(method, path, u, **kwargs):
            captured.update(kwargs.get("json") or {})
            return {"entity_id": "tx1"}

        monkeypatch.setattr(write_tools, "_self_call", fake_self_call)
        asyncio.run(write_tools.update_transaction(user, sync_id="tx1", tax_amount=0))

        assert "tax_amount" in captured, captured
        assert captured["tax_amount"] is None, (
            "0 必须翻译成显式 null(server 侧靠它区分「清除」)"
        )
    finally:
        app.dependency_overrides.clear()


def test_mcp_update_rejects_tax_not_below_amount(monkeypatch) -> None:
    """MCP 层也要挡明显非法的税额,给 LLM 可读报错而不是等 server 500。"""
    from src.mcp.tools import write_tools

    client, TS = _make_client()
    monkeypatch.setattr(write_tools, "SessionLocal", TS)
    try:
        app_token, _ = _two_tokens(client, "tax-mcp-bad@t.com")
        hdr = {"Authorization": f"Bearer {app_token}"}
        _seed_taxed_expense(client, hdr, amount=3280.0, tax=298.0)
        user = _fetch_user(TS, "tax-mcp-bad@t.com")

        async def boom(*a, **k):
            raise AssertionError("不应发出 self-call")

        monkeypatch.setattr(write_tools, "_self_call", boom)
        with pytest.raises(ValueError, match="less than amount"):
            asyncio.run(write_tools.update_transaction(
                user, sync_id="tx1", tax_amount=9999.0))
    finally:
        app.dependency_overrides.clear()

# --------------------------------------------------------------------------- #
# 6. CSV 导出 / 导入往返                                                      #
# --------------------------------------------------------------------------- #


def test_csv_export_includes_tax_column():
    """导出第 13 列是税额;amount 列仍是实付总额。"""
    from src.routers.read.workspace import _CSV_HEADERS_BY_LANG

    for lang, expected in (("zh-CN", "税额"), ("zh-TW", "稅額"), ("en", "Tax")):
        headers = _CSV_HEADERS_BY_LANG[lang]
        assert headers[-1] == expected, (lang, headers[-1:])
        # 前 12 列位置不打乱(mobile 导出对齐)
        assert len(headers) == 13, headers
        assert headers[3] in {"金额", "金額", "Amount"}, headers[3]


def test_csv_export_row_carries_tax(monkeypatch):
    """有税写数值、无税写空串。"""
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-csv@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        _seed_taxed_expense(client, hdr_app, amount=3280.0, tax=298.0)
        _push(client, hdr_app, "lg1", "transaction", "txNoTax",
              {"syncId": "txNoTax", "type": "expense", "amount": 100.0,
               "happenedAt": _iso(), "categoryName": "餐饮"})

        r = client.get("/api/v1/read/workspace/transactions.csv",
                       headers=hdr_web, params={"lang": "en"})
        assert r.status_code == 200, r.text
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        header = lines[0].lstrip("﻿").split(",")
        assert header[-1] == "Tax"
        tax_col = len(header) - 1
        amount_col = header.index("Amount")

        rows = {ln.split(",")[0] + "|" + ln.split(",")[3]: ln.split(",")
                for ln in lines[1:]}
        taxed = next(v for k, v in rows.items() if v[tax_col] == "298.00")
        assert taxed[tax_col] == "298.00"
        assert taxed[amount_col] == "3280.00", "amount 列必须是实付总额"
        untaxed = next(v for k, v in rows.items() if v[tax_col] == "")
        assert untaxed[tax_col] == "", "无税写空串,不是 0"
    finally:
        app.dependency_overrides.clear()


def _beecount_csv(*rows: list[str], headers: list[str] | None = None) -> str:
    """按导出端的真实表头拼 CSV。

    列顺序必须跟 `workspace._CSV_HEADERS_BY_LANG["zh-CN"]` 一致(时间列值带
    前后各两空格,那是导出端的既有格式)。按列构造而不是手写逗号串 ——
    手数逗号很容易错位,而且错位时 parser 不会报错,只会静默解析成空值。
    """
    header = headers or ["类型", "分类", "二级分类", "金额", "币种", "账户",
                         "转出账户", "转入账户", "备注", "时间", "标签", "附件", "税额"]
    lines = [",".join(header)]
    for row in rows:
        assert len(row) == len(header), (len(row), len(header), row)
        lines.append(",".join(row))
    return "\n".join(lines)


def test_csv_import_roundtrip_restores_tax():
    """导出 → 导入 → 税额还原。只导出不做导入会造成「往返静默丢税额」。"""
    from src.services.import_data.parser import parse_csv_text
    from src.services.import_data.transformer import _transform_row

    csv_text = _beecount_csv(
        ["支出", "餐饮", "", "3280.00", "", "", "", "",
         "KING BEAR NOW", "  2026-10-03 00:00:00  ", "", "", "298.00"],
        ["支出", "餐饮", "", "100.00", "", "", "", "",
         "无税", "  2026-10-03 00:00:00  ", "", "", ""],
    )
    data = parse_csv_text(raw_text=csv_text)
    assert str(data.source_format) in ("beecount", "SourceFormat.BEECOUNT"), data.source_format

    tax_col = data.suggested_mapping.tax_amount
    assert tax_col == "税额", "导出文件里的税额列没被映射到"

    txs = [_transform_row(row, data.suggested_mapping) for row in data.rows]
    assert all(t is not None for t in txs), "两行都应解析成功"
    assert txs[0].tax_amount == 298.0
    assert txs[1].tax_amount is None, "空税额列 = 无税"

    # 旧导出文件(12 列,无税额列)必须仍能被识别 —— 新增第 13 列不改变解析
    old = _beecount_csv(
        ["支出", "餐饮", "", "3280.00", "", "", "", "",
         "KING BEAR NOW", "  2026-10-03 00:00:00  ", "", ""],
        headers=["类型", "分类", "二级分类", "金额", "币种", "账户",
                 "转出账户", "转入账户", "备注", "时间", "标签", "附件"],
    )
    old_data = parse_csv_text(raw_text=old)
    assert str(old_data.source_format) in ("beecount", "SourceFormat.BEECOUNT")
    assert old_data.suggested_mapping.tax_amount is None
    old_tx = _transform_row(old_data.rows[0], old_data.suggested_mapping)
    assert old_tx.tax_amount is None, "旧文件无税列 → 不导入税额"


def test_csv_import_tax_tolerates_garbage():
    """导入侧宽容:脏值当无税,不阻断整份 CSV。"""
    from src.services.import_data.transformer import _parse_tax_amount

    assert _parse_tax_amount(None, 100.0, "expense") is None
    assert _parse_tax_amount("", 100.0, "expense") is None
    assert _parse_tax_amount("N/A", 100.0, "expense") is None
    assert _parse_tax_amount("-5", 100.0, "expense") is None
    assert _parse_tax_amount("0", 100.0, "expense") is None
    assert _parse_tax_amount("100", 100.0, "expense") is None, ">= amount"
    assert _parse_tax_amount("10", -100.0, "expense") is None, "负金额"
    assert _parse_tax_amount("10", 100.0, "income") is None, "income 无税"
    assert _parse_tax_amount("1,234.50", 5000.0, "expense") == 1234.50
    assert _parse_tax_amount("¥10", 100.0, "expense") == 10.0


def test_tax_category_self_count_not_double():
    """边界:一笔支出**本身就记在「税与保险」分类下**且带税额。

    此时 tax_slot 与 category_slot 是同一个 dict —— 金额天然合并
    (净额 + 税额 = 实付),但 count 若也加两次,一笔会显示成两笔。
    这个 case 语义上少见(「消费税」是拆分出来的税,不是一笔独立支出),
    但不能因此让 tx_count 说谎。"""
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-self@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        _seed_taxed_expense(client, hdr_app, amount=3280.0, tax=298.0,
                            category=TAX_BUCKET)

        body = _analytics(client, hdr_web)
        assert abs(body["summary"]["expense_total"] - 3280.0) < 1e-6
        rows = {r["category_name"]: r for r in body["category_ranks"]}
        assert list(rows) == [TAX_BUCKET], rows
        assert abs(rows[TAX_BUCKET]["total"] - 3280.0) < 1e-6, rows
        assert rows[TAX_BUCKET]["tx_count"] == 1, (
            f"一笔不能被算成两笔,got {rows[TAX_BUCKET]['tx_count']}"
        )
    finally:
        app.dependency_overrides.clear()
