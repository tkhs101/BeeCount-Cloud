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
import csv
import importlib.util
import io
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


def _web_create_expect(client, hdr, ledger_id, base, payload):
    """建账本后发一笔可能非法的交易,返回原始 response(不做 200 断言)。"""
    return client.post(
        f"/api/v1/write/ledgers/{ledger_id}/transactions",
        headers={**hdr, "Content-Type": "application/json"},
        json={"base_change_id": base, **payload},
    )


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


def test_migration_0020_adds_nullable_column_and_preserves_rows(tmp_path):
    """**迁移的行为**,不是源码里有没有某个字符串。

    上一版这个测试是 `"nullable=True" in src`(源码 grep)+ `assert not
    hasattr(mod, "BACKFILL_STATEMENT")` —— 那个属性从来就不存在,换任何实现
    都会通过;真写了内联回填它照样绿。等于什么都没锁。

    这里真跑一遍迁移:先建一张**不含** tax_amount 的 read_tx_projection,
    塞几行存量数据,再用 alembic 的 op 执行 upgrade(),然后断言:
      1. 列被加上了
      2. 可空
      3. **存量行原样保留**(无回填 → 存量 = 无税)
    """
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mod = _load_migration_0020()
    assert mod.revision == "0020_tx_tax_amount"
    assert mod.down_revision == "0019_account_hidden"

    db_path = tmp_path / "m.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    meta = sa.MetaData()
    sa.Table(
        "read_tx_projection", meta,
        sa.Column("ledger_id", sa.String(64), primary_key=True),
        sa.Column("sync_id", sa.String(255), primary_key=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("native_amount", sa.Float(), nullable=True),
    )
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO read_tx_projection (ledger_id, sync_id, amount) "
            "VALUES ('L1', 'tx1', 100.0)"
        ))
        conn.execute(sa.text(
            "INSERT INTO read_tx_projection (ledger_id, sync_id, amount) "
            "VALUES ('L1', 'tx2', 250.0)"
        ))

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()

    insp = sa.inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("read_tx_projection")}
    assert "tax_amount" in cols, sorted(cols)
    assert cols["tax_amount"]["nullable"] is True

    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT sync_id, amount, tax_amount FROM read_tx_projection "
            "ORDER BY sync_id"
        )).all()
    assert len(rows) == 2, rows
    for sync_id, _amount, tax in rows:
        assert tax is None, f"{sync_id} 的 tax 应为 NULL(无回填),got {tax!r}"
    assert [r[1] for r in rows] == [100.0, 250.0], "存量金额不能被动过"


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
        # 用 csv 模块解析,别裸 split(",") —— 备注里出现逗号就会整行错位,
        # 而错位时断言可能仍然「看起来通过」。
        rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
        header = [h.strip() for h in rows[0]]
        assert header[-1] == "Tax"
        tax_col = header.index("Tax")
        amount_col = header.index("Amount")

        taxed = next(x for x in rows[1:] if x[tax_col] == "298.00")
        assert taxed[amount_col] == "3280.00", "amount 列必须是实付总额"
        untaxed = next(x for x in rows[1:] if x[tax_col] == "")
        assert untaxed[amount_col] == "100.00", "无税写空串,不是 0"
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


def test_web_invalid_tax_returns_400_not_500(monkeypatch) -> None:
    """**R1 回归锁** —— 税额校验失败必须是 400,不是 500。

    两个 write 快路径(`_commit_create_tx_fast` / `_commit_write_fast_tx`)原先
    直接调 mutator,没有慢路径 `_commit_write` 那层
    `ValueError → HTTPException(400)` 包装。这个缺口一直存在但不显形:mutator
    原先唯一的 ValueError(invalid tx_type)被 pydantic 的 Literal 以 422 挡在
    外面;0020 的税额校验是第一条能真正穿透到快路径的 ValueError。

    前端刻意不做客户端校验(parseTaxAmount 只解析不拦),所以这条路径就是
    Web 录入的主路径 —— 不包装的话用户抄错税额只会看到泛泛的「内部错误」,
    WRITE_VALIDATION_FAILED 那条错误码链路一次都不触发。
    """
    client, TS = _make_client()
    try:
        token = _register_and_token(client, "tax-400@t.com", device_id="d-web",
                                    client_type="web")
        hdr = {"Authorization": f"Bearer {token}", "X-Device-ID": "web"}
        base = _web_ledger(client, hdr)

        # 1) create: tax >= amount
        r = _web_create_expect(client, hdr, "lg1", base, {
            "tx_type": "expense", "amount": 100.0, "happened_at": _iso(),
            "category_name": "餐饮", "tax_amount": 100.0,
        })
        assert r.status_code == 400, f"create 应 400,got {r.status_code}: {r.text[:200]}"
        assert "tax_amount" in r.text

        # 2) create: 负数税
        r = _web_create_expect(client, hdr, "lg1", base, {
            "tx_type": "expense", "amount": 100.0, "happened_at": _iso(),
            "category_name": "餐饮", "tax_amount": -5.0,
        })
        assert r.status_code == 400, r.status_code

        # 3) create: income 上带税
        r = _web_create_expect(client, hdr, "lg1", base, {
            "tx_type": "income", "amount": 100.0, "happened_at": _iso(),
            "category_name": "工资", "tax_amount": 10.0,
        })
        assert r.status_code == 400, r.status_code

        # 4) PATCH: 把税改成超过金额
        res = _web_create(client, hdr, "lg1", base=base, payload={
            "tx_type": "expense", "amount": 3280.0, "happened_at": _iso(),
            "category_name": "餐饮", "tax_amount": 298.0,
        })
        tx_id = res["entity_id"]
        r = client.patch(f"/api/v1/write/ledgers/lg1/transactions/{tx_id}",
                         headers={**hdr, "Content-Type": "application/json"},
                         json={"base_change_id": base, "tax_amount": 9999.0})
        assert r.status_code == 400, f"PATCH 应 400,got {r.status_code}: {r.text[:200]}"

        # 5) 合法路径仍然 200
        r = client.patch(f"/api/v1/write/ledgers/lg1/transactions/{tx_id}",
                         headers={**hdr, "Content-Type": "application/json"},
                         json={"base_change_id": base, "tax_amount": 350.0})
        assert r.status_code == 200, r.text[:200]
        # 6) PATCH: expense → income 而既有税额变得不自洽(放最后,
        #    因为它把这笔改成 income,之后的合法税额路径就不再合法了)。
        #    这里**故意不是 400** —— 调用方并没有要求改税额,税额是上一轮写
        #    进去的;把它当校验错误抛回去等于要求用户先手工清一次税额才能改
        #    类型。降级策略是「剔除这笔税额并告警」(见 Y7 测试),静默但可恢复。
        r = client.patch(f"/api/v1/write/ledgers/lg1/transactions/{tx_id}",
                         headers={**hdr, "Content-Type": "application/json"},
                         json={"base_change_id": base, "tx_type": "income"})
        assert r.status_code == 200, r.text[:200]
        after = next(t for t in client.get("/api/v1/read/workspace/transactions",
                                          headers=hdr).json()["items"]
                     if t["id"] == tx_id)
        assert after["tx_type"] == "income"
        assert after["tax_amount"] is None, "income 上的税额应被剔除"

    finally:
        app.dependency_overrides.clear()


def test_mcp_analytics_matches_web_slice(monkeypatch):
    """**Y1 回归锁** —— MCP 的 `get_analytics_summary` 必须和 Web 的
    `workspace_analytics` **同一套切片**。

    用户的记账入口是 MCP。如果 MCP 不剥税,LLM 会照着 MCP 的数回答
    「餐饮花了 3280」,而界面饼图显示「餐饮 2982 + 税与保险 298」——
    同一个问题两个答案。expense 总额两边都保持全额。
    """
    from src.mcp.tools import read_tools

    client, TS = _make_client()
    # read_tools 直接 `with SessionLocal()` 查 projection —— 必须打到测试库
    monkeypatch.setattr(read_tools, "SessionLocal", TS)
    try:
        app_token, web_token = _two_tokens(client, "tax-mcp-ana@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        _seed_taxed_expense(client, hdr_app, amount=3280.0, tax=298.0)
        _push(client, hdr_app, "lg1", "transaction", "tx2",
              {"syncId": "tx2", "type": "expense", "amount": 50000.0,
               "happenedAt": _iso(), "categoryName": TAX_BUCKET})

        user = _fetch_user(TS, "tax-mcp-ana@t.com")
        mcp_out = read_tools.get_analytics_summary(user, scope="all")
        mcp_ranks = {r["name"]: r["total"] for r in mcp_out["top_categories"]}

        web_out = _analytics(client, hdr_web)
        web_ranks = {r["category_name"]: r["total"] for r in web_out["category_ranks"]}

        assert abs(mcp_out["expense"] - web_out["summary"]["expense_total"]) < 1e-6
        assert abs(mcp_out["expense"] - 53280.0) < 1e-6, "总额仍全额"
        for name in ("餐饮", TAX_BUCKET):
            assert name in mcp_ranks, (name, mcp_ranks)
            assert abs(mcp_ranks[name] - web_ranks[name]) < 1e-2, (
                f"MCP 与 Web 对「{name}」口径不一致: {mcp_ranks[name]} vs {web_ranks[name]}"
            )
        assert abs(mcp_out["tax_total"] - 298.0) < 1e-6, mcp_out["tax_total"]
    finally:
        app.dependency_overrides.clear()


def test_legacy_dirty_tax_does_not_block_editing(monkeypatch, caplog):
    """**Y7** —— 历史脏数据(经 `/sync/push` 进来的)不能把整笔交易卡死。

    `/sync/push` 不经过 snapshot_mutator、merge 也不校验,所以 `taxAmount`
    大于 amount、或 income 上带税,都能直接落进 projection。这类数据落到
    Web 上,用户改个备注都会 400 —— 而且叠加 R1 就是「整笔打不开」。

    预期:改备注**成功**,同时把这笔的脏税额剔除并告警。统计侧本来就会把
    税额夹在 [0, base] 内,剔除是安全降级。
    """
    client, TS = _make_client()
    try:
        app_token, web_token = _two_tokens(client, "tax-dirty@t.com")
        hdr_app = {"Authorization": f"Bearer {app_token}"}
        hdr_web = {"Authorization": f"Bearer {web_token}"}
        # 脏数据:税 500 > 金额 100
        _push(client, hdr_app, "lg1", "transaction", "txDirty",
              {"syncId": "txDirty", "type": "expense", "amount": 100.0,
               "happenedAt": _iso(), "categoryName": "餐饮", "taxAmount": 500.0})
        base = 0

        # 真实 PATCH:只改备注,完全不提税额
        r = client.patch(
            "/api/v1/write/ledgers/lg1/transactions/txDirty",
            headers={**hdr_web, "Content-Type": "application/json"},
            json={"base_change_id": base, "note": "只是改个备注"},
        )
        assert r.status_code == 200, f"改备注不该被脏税额卡死: {r.text[:250]}"
        row = _get_tx(TS, _ledger_internal_id(TS, "lg1"), "txDirty")
        assert row.note == "只是改个备注"
        assert row.tax_amount is None, "不自洽的税额应被剔除"

        # 统计不受影响:总额仍是 100
        body = _analytics(client, hdr_web)
        assert abs(body["summary"]["expense_total"] - 100.0) < 1e-6
        ranks = {r["category_name"]: r["total"] for r in body["category_ranks"]}
        assert TAX_BUCKET not in ranks, ranks
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 7. 换算函数的防御分支                                                          #
# --------------------------------------------------------------------------- #


def test_tax_conversion_defensive_branches():
    """**B10** —— `tax_in_base_currency` 的每个分支都要有测试。

    这几个分支不是「保险起见」,它们是「脏数据下不变式依然成立」的**唯一
    保证**:`/sync/push` 不经过 snapshot_mutator、merge 也不校验,所以
    `taxAmount` 大于 amount、负数、非数字,甚至金额为负,都能直接落进
    projection。没有这些分支,就会算出负税额切片,「分类税前 + 税切片 = 实付」
    这条不变式当场破掉。

    核心断言只有一个:**返回值永远落在 [0, base_amount] 内**。
    """
    from src.routers.read._shared import tax_in_base_currency as conv

    # 正常比例法
    assert conv(298.0, 3280.0, 3280.0) == 298.0
    assert conv(5.0, 50.0, 1000.0) == 100.0, "外币按比例折算"

    # tax_amount 为 None(免税 / 未记录)→ 0
    assert conv(None, 3280.0, 3280.0) == 0.0

    # tax 非法类型 / 非法值
    assert conv("N/A", 3280.0, 3280.0) == 0.0, "非数字字符串"
    assert conv("", 3280.0, 3280.0) == 0.0
    assert conv(0.0, 3280.0, 3280.0) == 0.0
    assert conv(-298.0, 3280.0, 3280.0) == 0.0, "负税额"
    # NaN / inf:显式挡掉。靠「NaN 和任何数比较都 False」兜底不可靠 ——
    # `tax <= 0` 守卫会静默放行,然后 NaN 一路传染到 JSON 响应
    # (FastAPI 序列化 NaN 会产出非法 JSON)。
    assert conv(float("nan"), 3280.0, 3280.0) == 0.0
    assert conv(float("inf"), 3280.0, 3280.0) == 0.0
    assert conv(298.0, float("nan"), 3280.0) == 298.0, "raw 异常 → 退化 1:1"
    assert conv(298.0, 3280.0, float("inf")) == 0.0, "base 非有限 → 不产生切片"

    # 金额非正 → 不产生税切片(否则会算出负税额)
    assert conv(298.0, -100.0, -100.0) == 0.0, "负金额"
    assert conv(298.0, 0.0, 0.0) == 0.0, "零金额"

    # 税额超过金额 → 夹到 base(脏数据可达:sync/push 不校验)
    assert conv(5000.0, 100.0, 100.0) == 100.0, "tax>amount 必须夹住,不能溢出"
    assert 0.0 <= conv(5000.0, 100.0, 100.0) <= 100.0

    # 原币金额推不出比率 → 退化 1:1 并夹住
    assert conv(298.0, 0.0, 500.0) == 298.0, "raw=0 时按 1:1"
    assert conv(9999.0, 0.0, 500.0) == 500.0, "1:1 也要夹到 base"

    # base_amount 为 None / 非数字
    assert conv(298.0, 3280.0, None) == 0.0

    # 不变式的最终形式:任意输入,结果都在 [0, base] 内
    for tax, raw, base in [
        (298.0, 3280.0, 3280.0), (5000.0, 100.0, 100.0), (-1.0, -1.0, -1.0),
        (0.0, 0.0, 0.0), (1e18, 1.0, 1.0), (298.0, None, 3280.0),
    ]:
        got = conv(tax, raw, base)
        assert 0.0 <= got <= max(0.0, base), (tax, raw, base, got)


def test_category_budget_on_tax_category_excludes_extracted_tax():
    """**语义边界** —— 给「税与保险」设分类预算时,从其他分类剥出来的消费税
    **不计入**预算用量。

        饼图 税与保险 = 1298   (住民税 1000 + 从餐饮剥出的消费税 298)
        预算 used    = 1000   (只有手动记在分类下的那笔)

    这是**刻意保留**的边界,不是 bug,理由是另一条性质更值钱:

    预算口径(D4)是「每笔实际支出恰好被计一次」。餐饮预算看到 3280(全额)、
    税与保险预算看到 1000(手动记的税),两者相加 = 4280 = 真实总支出,
    是一个干净的分区。如果让税与保险预算把 298 也算进去,同一个 298 就被
    餐饮(全额)和税与保险(抽取)**各计一次**,总额变成 4578 —— 分区性质被
    破坏,两个预算都不可信了。

    代价:用户在饼图和预算页会看到两个不同的「税与保险」数字。饼图是**分析
    视角**(回答「我交了多少钱税」),预算是**记账口径**(回答「我按分类承诺了
    多少」)。这里把行为锁死,免得以后被无意改掉。

    想要「本月税务支出上限」这个数字,正确做法是看饼图 / MCP 的
    `get_analytics_summary().tax_total`,不是分类预算。"""
    client, TS = _make_client()
    try:
        token = _register_and_token(client, "tax-budget@t.com", device_id="d-web",
                                    client_type="web")
        hdr = {"Authorization": f"Bearer {token}", "X-Device-ID": "web"}
        J = {**hdr, "Content-Type": "application/json"}
        base = _web_ledger(client, hdr)
        for name in ("餐饮", TAX_BUCKET):
            client.post("/api/v1/write/ledgers/lg1/categories", headers=J,
                        json={"base_change_id": base, "name": name, "kind": "expense"})
        cats = client.get("/api/v1/read/workspace/categories", headers=hdr).json()
        tax_id = next(c["id"] for c in cats if c["name"] == TAX_BUCKET)

        # 餐饮含税 3280(298 消费税)+ 税与保险下的住民税 1000
        client.post("/api/v1/write/ledgers/lg1/transactions", headers=J, json={
            "base_change_id": base, "tx_type": "expense", "amount": 3280.0,
            "happened_at": _iso(), "category_name": "餐饮",
            "category_kind": "expense", "tax_amount": 298.0})
        client.post("/api/v1/write/ledgers/lg1/transactions", headers=J, json={
            "base_change_id": base, "tx_type": "expense", "amount": 1000.0,
            "happened_at": _iso(), "category_name": TAX_BUCKET,
            "category_id": tax_id, "category_kind": "expense"})

        client.post("/api/v1/write/ledgers/lg1/budgets", headers=J, json={
            "base_change_id": base, "type": "category", "category_id": tax_id,
            "amount": 50000.0, "period": "monthly", "enabled": True})

        usage = client.get("/api/v1/read/ledgers/lg1/budgets/usage", headers=hdr).json()
        used = {x["budget_id"]: x["used"] for x in usage["items"]}
        body = _analytics(client, hdr)
        pie = {r["category_name"]: r["total"] for r in body["category_ranks"]}

        # 预算只看到手动记在分类下的 1000
        assert list(used.values()) == [1000.0], used
        # 饼图看到 1298(含剥出来的 298)
        assert abs(pie[TAX_BUCKET] - 1298.0) < 1e-6, pie
        # 差额正好是那笔被剥走的消费税 —— 两边都没算错,只是口径不同
        assert abs((pie[TAX_BUCKET] - 1000.0) - 298.0) < 1e-6
    finally:
        app.dependency_overrides.clear()


def test_admin_backup_restore_preserves_tax(monkeypatch):
    """**备份 → 还原整条链路保住税额** —— 而且顺带修掉一个上游缺陷。

    原来的 `create_backup` 去 `sync_changes` 里找一条 `ledger_snapshot` 行。
    方案 B(projection-as-authority)之后**没有任何代码再写那种行**
    (`_commit_write` / `/sync/push` 都不写,`SYNC_ARCHITECTURE.md` §1 有说明),
    所以对任何新建账本都必然 404 "No snapshot for ledger" —— 管理面板的
    「备份」按钮在方案 B 之后是坏的。

    自托管最怕「以为备份了其实没有」,所以这条必须端到端验证:建备份 → 删掉
    交易 → 还原 → 税额与统计切片都回到原样。
    """
    client, TS = _make_client()
    try:
        token = _register_and_token(client, "bk-restore@t.com", device_id="d-web",
                                    client_type="web")
        hdr = {"Authorization": f"Bearer {token}", "X-Device-ID": "web"}
        J = {**hdr, "Content-Type": "application/json"}
        base = _web_ledger(client, hdr)
        client.post("/api/v1/write/ledgers/lg1/categories", headers=J,
                    json={"base_change_id": base, "name": "餐饮", "kind": "expense"})
        res = _web_create(client, hdr, "lg1", base=base, payload={
            "tx_type": "expense", "amount": 3280.0, "happened_at": _iso(),
            "category_name": "餐饮", "category_kind": "expense",
            "tax_amount": 298.0, "note": "KING BEAR NOW"})
        tx_id = res["entity_id"]

        before = _analytics(client, hdr)
        before_ranks = {r["category_name"]: r["total"] for r in before["category_ranks"]}
        assert abs(before_ranks["餐饮"] - 2982.0) < 1e-6
        assert abs(before_ranks[TAX_BUCKET] - 298.0) < 1e-6

        # 1) 建备份 —— 旧写法在这里就 404
        r = client.post("/api/v1/admin/backups/create", headers=J,
                        json={"ledger_id": "lg1", "note": "含税"})
        assert r.status_code == 200, f"备份应可用: {r.text[:200]}"
        snapshot_id = r.json()["snapshot_id"]

        # 2) 破坏数据
        r = client.request("DELETE", f"/api/v1/write/ledgers/lg1/transactions/{tx_id}",
                           headers=J, json={"base_change_id": base, "confirm": True})
        assert r.status_code == 200, r.text[:200]
        assert _analytics(client, hdr)["category_ranks"] == []

        # 3) 还原
        r = client.post("/api/v1/admin/backups/restore", headers=J,
                        json={"snapshot_id": snapshot_id, "device_id": "web"})
        assert r.status_code == 200, f"还原应成功: {r.text[:200]}"

        # 4) 税额与统计切片都回到原样
        after = _analytics(client, hdr)
        after_ranks = {r["category_name"]: r["total"] for r in after["category_ranks"]}
        assert abs(after["summary"]["expense_total"] - 3280.0) < 1e-6, after
        assert abs(after_ranks["餐饮"] - 2982.0) < 1e-6, after_ranks
        assert abs(after_ranks[TAX_BUCKET] - 298.0) < 1e-6, after_ranks

        tx = next((t for t in client.get("/api/v1/read/workspace/transactions",
                                        headers=hdr).json()["items"]
                   if t["id"] == tx_id), None)
        assert tx is not None, "交易没被还原"
        assert tx["tax_amount"] == 298.0, "还原后税额丢了"
        assert tx["note"] == "KING BEAR NOW"
    finally:
        app.dependency_overrides.clear()


def test_mcp_batch_import_keeps_tax(monkeypatch) -> None:
    """**批量导入的税额不能被 pydantic 边界吃掉。**

    `BatchTransactionItem` 原本没有 `tax_amount` 字段,而 pydantic 默认
    `extra='ignore'` —— MCP 明明在 body 里传了 `tax_amount`,到
    `req.model_dump()` 就已经被丢掉,后面所有环节都拿不到,**且不报错**。
    单条 `create_transaction` 走的是另一个 schema(`WriteTransactionCreateRequest`,
    我加了字段),所以只有批量这条路会丢 —— 很难靠「测单条能不能记」发现。

    这与 issue #513 那类「穿过 pydantic 边界静默丢字段」是同一个 bug 形态。
    """
    from datetime import timedelta

    from src.mcp.tools import write_tools
    from src.security import SCOPE_APP_WRITE, SCOPE_WEB_WRITE, _create_token

    client, TS = _make_client()
    monkeypatch.setattr(write_tools, "SessionLocal", TS)
    monkeypatch.setattr(
        write_tools, "_internal_token",
        lambda u: _create_token(
            sub=u.id, token_type="access", expires_delta=timedelta(seconds=60),
            scopes=[SCOPE_APP_WRITE, SCOPE_WEB_WRITE], client_type="app",
        ),
    )
    try:
        token = _register_and_token(client, "batch-tax@t.com", device_id="d-app",
                                    client_type="web")
        hdr = {"Authorization": f"Bearer {token}", "X-Device-ID": "web"}
        J = {**hdr, "Content-Type": "application/json"}
        _web_ledger(client, hdr)
        client.post("/api/v1/write/ledgers/lg1/categories", headers=J,
                    json={"base_change_id": 0, "name": "餐饮", "kind": "expense"})
        user = _fetch_user(TS, "batch-tax@t.com")

        import asyncio

        out = asyncio.run(write_tools.create_transactions(
            user,
            transactions=[
                {"amount": 3280.0, "category": "餐饮", "tax_amount": 298.0,
                 "happened_at": _iso(), "note": "含税"},
                {"amount": 100.0, "category": "餐饮", "happened_at": _iso()},
            ],
        ))
        assert out, out

        items = client.get("/api/v1/read/workspace/transactions",
                           headers=hdr).json()["items"]
        assert len(items) == 2, items
        taxed = next(t for t in items if t["amount"] == 3280.0)
        plain = next(t for t in items if t["amount"] == 100.0)
        assert taxed["tax_amount"] == 298.0, (
            f"批量导入的税额丢了(实际 {taxed['tax_amount']!r}) —— "
            f"检查 BatchTransactionItem 有没有 tax_amount 字段"
        )
        assert plain["tax_amount"] is None

        # 统计切片也要跟着对
        ranks = {r["category_name"]: r["total"] for r in _analytics(client, hdr)["category_ranks"]}
        assert abs(ranks["餐饮"] - (2982 + 100)) < 1e-6, ranks
        assert abs(ranks[TAX_BUCKET] - 298.0) < 1e-6, ranks
    finally:
        app.dependency_overrides.clear()


def test_batch_transaction_item_keeps_tax_field() -> None:
    """直接锁 schema:pydantic 不许把 tax_amount 当未知字段丢掉。"""
    from src.routers.write.transactions_batch import BatchTransactionItem

    item = BatchTransactionItem(
        tx_type="expense", amount=3280.0, happened_at=_iso(), tax_amount=298.0)
    assert item.model_dump(mode="json")["tax_amount"] == 298.0
    # 不传时是 None,不是被丢掉的缺失键
    assert BatchTransactionItem(
        tx_type="expense", amount=1.0, happened_at=_iso()
    ).model_dump(mode="json")["tax_amount"] is None
