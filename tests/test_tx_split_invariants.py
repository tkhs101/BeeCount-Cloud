"""组合支付(0021)的核心不变式。

一笔 5000 円 的订单,招行卡付 3000、现金付 2000。四条要求:

1. 两个账户各扣自己那份
2. **支出总额只算一次 5000**(不是 5000+5000,也不是 3000+2000 两次分类统计)
3. 有腿时父交易的账户字段为空(否则余额双倍扣)
4. 改一笔组合支付交易的备注,腿不能丢

## 为什么第 4 条要单独测

`_projection_row_to_tx_dict` 是全仓库最危险的函数 —— 漏一个字段,Web PATCH
就会**静默抹掉**它,HTTP 200 不报错。上游 nativeAmount 当年踩过,0020 的
taxAmount 又踩过一次。splits 比那两次更隐蔽:它们在**子表**里,函数签名只看
一个 ORM row,不主动去查就一定漏。

这里用「改备注」当探针 —— 用户最常做的无害操作,恰好是最容易触发静默丢失的
操作。
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
from src.routers.read._shared import account_balance_delta
from src.snapshot_mutator import create_transaction as mut_create
from src.snapshot_mutator import update_transaction as mut_update


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
        "email": email, "password": "SplitTest1!x", "client_type": "web",
        "device_name": "d", "platform": "test",
    })
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    H = {"Authorization": f"Bearer {tok}", "X-Device-ID": "d",
         "Content-Type": "application/json"}
    r = client.post("/api/v1/write/ledgers", headers=H,
                    json={"ledger_id": "lg1", "ledger_name": "L", "currency": "JPY"})
    assert r.status_code == 200, r.text
    for name, kind in (("招行卡", "bank_card"), ("现金", "cash")):
        client.post("/api/v1/write/ledgers/lg1/accounts", headers=H, json={
            "base_change_id": 0, "name": name, "account_type": kind,
            "initial_balance": 100000.0})
    accs = client.get("/api/v1/read/workspace/accounts", headers=H).json()
    ids = {a["name"]: a["id"] for a in accs}
    return client, TS, H, ids


def _create_split_tx(client, H, ids, legs, amount=5000.0, note="组合支付"):
    r = client.post("/api/v1/write/ledgers/lg1/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": amount,
        "happened_at": "2026-10-03T12:00:00+00:00",
        "category_name": "购物", "category_kind": "expense", "note": note,
        "splits": legs,
    })
    return r


# --------------------------------------------------------------------------- #
# 1 + 2: 余额各扣自己那份,总额只算一次                                       #
# --------------------------------------------------------------------------- #


def test_split_payment_balance_and_total(monkeypatch) -> None:
    client, TS, H, ids = _setup("sp1@t.com")
    try:
        # 显式同时传 account 和 splits —— 前端表单很容易两个都填上。
        # mutator 必须强制清空父账户字段。
        r = _create_split_tx(client, H, ids, [
            {"account_id": ids["招行卡"], "amount": 3000.0},
            {"account_id": ids["现金"], "amount": 2000.0},
        ])
        assert r.status_code == 200, r.text
        tx_id = r.json().get("entity_id")

        with TS() as db:
            from src.models import ReadTxProjection, ReadTxSplitProjection
            tx = db.scalar(select(ReadTxProjection).where(
                ReadTxProjection.sync_id == tx_id))
            assert tx is not None
            # 不变式 3:有腿时父账户字段为空
            assert tx.account_sync_id is None, tx.account_sync_id
            assert tx.account_name is None, tx.account_name
            # 不变式 1 的存储侧:腿确实落了子表
            legs = db.scalars(select(ReadTxSplitProjection).where(
                ReadTxSplitProjection.tx_sync_id == tx_id).order_by(
                ReadTxSplitProjection.seq)).all()
            assert len(legs) == 2, legs
            assert abs(sum(float(x.amount) for x in legs) - 5000.0) < 1e-6

        accs = {a["name"]: a for a in
                client.get("/api/v1/read/workspace/accounts", headers=H).json()}
        # 不变式 1:各扣自己那份
        assert abs(accs["招行卡"]["balance"] - 97000.0) < 1e-6, accs["招行卡"]
        assert abs(accs["现金"]["balance"] - 98000.0) < 1e-6, accs["现金"]

        # 不变式 2:支出总额只算一次 5000
        a = client.get("/api/v1/read/workspace/analytics", headers=H,
                       params={"scope": "all", "metric": "expense"}).json()
        assert abs(a["summary"]["expense_total"] - 5000.0) < 1e-6, a["summary"]
        ranks = {x["category_name"]: x["total"] for x in a["category_ranks"]}
        assert abs(ranks["购物"] - 5000.0) < 1e-6, ranks
    finally:
        app.dependency_overrides.clear()


def test_split_legs_counted_for_tx_count() -> None:
    """腿要计入 tx_count —— 否则 `HomeTopAccounts` 按 `count > 0` 过滤时,
    涉及的账户会整块消失(不是算错,是不显示,单测最难发现的那种)。"""
    client, TS, H, ids = _setup("sp2@t.com")
    try:
        r = _create_split_tx(client, H, ids, [
            {"account_id": ids["招行卡"], "amount": 3000.0},
            {"account_id": ids["现金"], "amount": 2000.0},
        ])
        assert r.status_code == 200, r.text
        accs = {a["name"]: a for a in
                client.get("/api/v1/read/workspace/accounts", headers=H).json()}
        assert (accs["招行卡"]["tx_count"] or 0) >= 1, accs["招行卡"]
        assert (accs["现金"]["tx_count"] or 0) >= 1, accs["现金"]
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 4: PATCH 不能丢腿(反向桥)                                                 #
# --------------------------------------------------------------------------- #


def test_patch_note_preserves_splits() -> None:
    """改备注后腿必须还在 —— 这是反向桥最容易漏的一条。"""
    client, TS, H, ids = _setup("sp3@t.com")
    try:
        r = _create_split_tx(client, H, ids, [
            {"account_id": ids["招行卡"], "amount": 3000.0},
            {"account_id": ids["现金"], "amount": 2000.0},
        ])
        tx_id = r.json().get("entity_id")

        base = client.get("/api/v1/read/ledgers/lg1/transactions",
                          headers=H).json()
        base_id = next(t["id"] for t in base if t["amount"] == 5000.0)

        r = client.patch(f"/api/v1/write/ledgers/lg1/transactions/{base_id}",
                         headers=H, json={"base_change_id": 0, "note": "只改备注"})
        assert r.status_code == 200, r.text

        after = client.get("/api/v1/read/ledgers/lg1/transactions",
                           headers=H).json()
        tx = next(t for t in after if t["id"] == base_id)
        assert tx["note"] == "只改备注", tx
        assert len(tx.get("splits") or []) == 2, (
            f"PATCH 备注把 splits 抹掉了:{tx.get('splits')}"
        )
        accs = {a["name"]: a for a in
                client.get("/api/v1/read/workspace/accounts", headers=H).json()}
        assert abs(accs["招行卡"]["balance"] - 97000.0) < 1e-6, (
            f"余额被改了 —— 腿丢了:{accs['招行卡']}"
        )
    finally:
        app.dependency_overrides.clear()


def test_patch_clears_splits_with_explicit_empty_list() -> None:
    client, TS, H, ids = _setup("sp4@t.com")
    try:
        _create_split_tx(client, H, ids, [
            {"account_id": ids["招行卡"], "amount": 3000.0},
            {"account_id": ids["现金"], "amount": 2000.0},
        ])
        tx_id = client.get("/api/v1/read/ledgers/lg1/transactions",
                           headers=H).json()[0]["id"]
        r = client.patch(f"/api/v1/write/ledgers/lg1/transactions/{tx_id}",
                         headers=H, json={"base_change_id": 0, "splits": []})
        assert r.status_code == 200, r.text
        tx = client.get("/api/v1/read/ledgers/lg1/transactions",
                        headers=H).json()[0]
        assert not (tx.get("splits") or []), tx.get("splits")
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 删账户守卫                                                                  #
# --------------------------------------------------------------------------- #


def test_delete_account_referenced_by_split_is_refused() -> None:
    """删被拆分腿引用的账户必须被拒绝。

    守卫原本只数 `accountName` / `fromAccountName` / `toAccountName`,而拆分父
    交易这三个全空 → **守卫直接失效** → 账户可删 → 子表悬挂 → 余额永久漂移,
    且 `data_cleanup` 的孤儿扫描也扫不到子表,全程零报错。
    """
    client, TS, H, ids = _setup("sp5@t.com")
    try:
        _create_split_tx(client, H, ids, [
            {"account_id": ids["招行卡"], "amount": 3000.0},
            {"account_id": ids["现金"], "amount": 2000.0},
        ])
        r = client.request(
            "DELETE", f"/api/v1/write/ledgers/lg1/accounts/{ids['招行卡']}",
            headers=H, json={"base_change_id": 0})
        assert r.status_code in (400, 409), (
            f"删被拆分引用的账户应被拒绝,实际 {r.status_code}: {r.text[:200]}"
        )
        accs = client.get("/api/v1/read/workspace/accounts", headers=H).json()
        assert any(a["name"] == "招行卡" for a in accs), "账户被误删了"
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 校验拒绝                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("legs,amount,tx_type,why", [
    ([{"account_id": "a", "amount": 3000.0}], 3000.0, "expense", "单条腿"),
    ([{"account_id": "a", "amount": 3000.0},
      {"account_id": "b", "amount": 1999.0}], 5000.0, "expense", "和不等"),
    ([{"account_id": "a", "amount": 3000.0},
      {"account_id": "b", "amount": 2000.0}], 5000.0, "income", "收入拆分"),
    ([{"amount": 3000.0}, {"account_id": "b", "amount": 2000.0}],
     5000.0, "expense", "缺 account_id"),
    ([{"account_id": "a", "amount": 0.0}, {"account_id": "b", "amount": 5000.0}],
     5000.0, "expense", "金额为 0"),
])
def test_invalid_splits_rejected(legs, amount, tx_type, why) -> None:
    from src.snapshot_mutator import _normalize_splits

    with pytest.raises(ValueError, match="write validation failed"):
        _normalize_splits(legs, amount=amount, tx_type=tx_type)


def test_splits_need_no_currency_field() -> None:
    """腿没有币种字段 —— 与父交易同币种是**结构保证**,不是运行时校验。

    腿金额只与父 `amount`(原币)比较,余额聚合读的也是原币。所以一笔外币交易
    (currency_code=USD, native_amount 折算)拆成两条 USD 腿是对的,不需要
    也不应该拦。写这条是防止以后有人「顺手加个跨币种校验」—— 那是无法表达
    的检查,只会给出虚假的安心感。
    """
    from src.snapshot_mutator import _normalize_splits

    legs = [{"accountId": "a", "amount": 30.0}, {"accountId": "b", "amount": 20.0}]
    out = _normalize_splits(legs, amount=50.0, tx_type="expense")
    assert out is not None and len(out) == 2, out


def test_split_balance_delta_matches_leg_sum() -> None:
    """逐笔 delta 与 SQL 聚合对腿的口径一致。"""
    legs = [{"accountId": "a", "amount": 30.0}, {"accountId": "b", "amount": 20.0}]
    d = account_balance_delta("expense", 50.0, None, None, None, splits=legs)
    assert d == {"a": -30.0, "b": -20.0}, d
    assert abs(sum(d.values()) + 50.0) < 1e-9


def test_mutator_clears_parent_account_even_when_provided() -> None:
    """显式同时传 account_id 和 splits 时,父账户字段必须被清空。"""
    snap, _ = mut_create({"items": [], "count": 0}, {
        "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-10-03T12:00:00+00:00",
        "account_id": "acc-legacy", "account_name": "不该保留",
        "splits": [{"account_id": "a", "amount": 3000.0},
                   {"account_id": "b", "amount": 2000.0}],
    })
    item = snap["items"][0]
    assert "accountId" not in item, item.get("accountId")
    assert "accountName" not in item, item.get("accountName")
    assert item["amount"] == 5000.0


def test_mutator_keeps_account_when_no_splits() -> None:
    snap, tid = mut_create({"items": [], "count": 0}, {
        "tx_type": "expense", "amount": 100.0,
        "happened_at": "2026-10-03T12:00:00+00:00",
        "account_id": "acc-keep", "account_name": "普通账户",
    })
    assert snap["items"][0]["accountId"] == "acc-keep"
    s2 = mut_update(snap, tid, {"note": "x"})
    assert s2["items"][0]["accountId"] == "acc-keep"


def test_patch_amount_invalidates_splits() -> None:
    """改总额时腿必须被重新校验 —— 这是反向桥真正承重的场景。

    为什么单靠 projection 的「键缺失 = 不动」守卫兜不住:

    - 改**备注**:守卫生效,腿保住(反向桥坏了也看不出来)
    - 改 **amount**:守卫仍然放过,但腿之和已经和总额对不上 ——
      总额显示 6000、余额却按 5000 的腿扣,**对不上账**

    有反向桥时 `prev_item` 带着腿进 mutator,自洽性校验会发现
    `sum(legs) != 新 amount` 而降级丢弃;没有桥就留下一笔自相矛盾的数据。
    """
    client, TS, H, ids = _setup("sp6@t.com")
    try:
        _create_split_tx(client, H, ids, [
            {"account_id": ids["招行卡"], "amount": 3000.0},
            {"account_id": ids["现金"], "amount": 2000.0},
        ])
        tx_id = client.get("/api/v1/read/ledgers/lg1/transactions",
                           headers=H).json()[0]["id"]

        r = client.patch(f"/api/v1/write/ledgers/lg1/transactions/{tx_id}",
                         headers=H, json={"base_change_id": 0, "amount": 6000.0})
        assert r.status_code == 200, r.text

        tx = client.get("/api/v1/read/ledgers/lg1/transactions",
                        headers=H).json()[0]
        # 总额改了,旧的 3000+2000 腿不再自洽 -> 必须被丢弃,
        # 不能留着让「总额 6000 / 余额按 5000 扣」长期对不上。
        assert not (tx.get("splits") or []), (
            f"改了总额却留着旧腿(总额 {tx['amount']},腿 {tx.get('splits')})"
        )
        # 账本总额与余额仍自洽
        accs = {a["name"]: a["balance"] for a in
                client.get("/api/v1/read/workspace/accounts", headers=H).json()}
        a = client.get("/api/v1/read/workspace/analytics", headers=H,
                       params={"scope": "all", "metric": "expense"}).json()
        assert abs(a["summary"]["expense_total"] - 6000.0) < 1e-6, a["summary"]
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# CSV 往返                                                                    #
# --------------------------------------------------------------------------- #


def test_csv_split_cell_format():
    """导出格式 `招行卡:3000.00|现金:2000.00` —— 导入侧是对称解析。"""
    from src.routers.read.workspace import _splits_cell

    cell = _splits_cell(
        [{"account_id": "a1", "amount": 3000.0}, {"account_id": "b2", "amount": 2000.0}],
        {"a1": "招行卡", "b2": "现金"},
    )
    assert cell == "招行卡:3000.00|现金:2000.00", cell
    # 找不到账户名时原样用 sync_id(不截断 —— 截断会让导入侧对不上号)
    assert _splits_cell([{"account_id": "zzz", "amount": 1.0}], {}) == "zzz:1.00"
    assert _splits_cell([], {}) == ""


def test_csv_splits_parse_is_symmetric():
    """导出格式必须能被导入解析器读回去 —— 否则往返即丢数据。"""
    from src.routers.read.workspace import _splits_cell
    from src.services.import_data.transformer import _parse_splits

    cell = _splits_cell(
        [{"account_id": "a", "amount": 3000.0}, {"account_id": "b", "amount": 2000.0}],
        {"a": "招行卡", "b": "现金"},
    )
    assert _parse_splits(cell) == [("招行卡", 3000.0), ("现金", 2000.0)]


def test_csv_splits_parse_is_lenient():
    """宽容优先:一行脏数据不该让整份 CSV 失败。"""
    from src.services.import_data.transformer import _parse_splits

    # 单条腿 → 不是组合支付
    assert _parse_splits("招行卡:3000.00") is None
    assert _parse_splits("") is None
    assert _parse_splits(None) is None
    # 垃圾段被丢掉,剩下不足 2 条 → None
    assert _parse_splits("垃圾|也是垃圾") is None
    assert _parse_splits("招行卡:abc|现金:100") is None
    assert _parse_splits(":100|现金:100") is None
    assert _parse_splits("招行卡:0|现金:100") is None
    # 一条坏 + 两条好 → 保留好的
    assert _parse_splits("垃圾|招行卡:3000|现金:2000") == [
        ("招行卡", 3000.0), ("现金", 2000.0)]


def test_csv_header_has_splits_column():
    """三语表头都要有拆分列,且排在税额之后(不打乱前 12 列)。"""
    from src.routers.read.workspace import _CSV_HEADERS_BY_LANG

    for lang, tax, splits in (("zh-CN", "税额", "拆分"),
                              ("zh-TW", "稅額", "拆分"),
                              ("en", "Tax", "Splits")):
        headers = _CSV_HEADERS_BY_LANG[lang]
        assert len(headers) == 14, (lang, headers)
        assert headers[12] == tax, (lang, headers[12:])
        assert headers[13] == splits, (lang, headers[13:])
        # 前 12 列位置不动(mobile 导出对齐)
        assert headers[3] in {"金额", "金額", "Amount"}, (lang, headers[3])


def test_csv_import_alias_registered():
    """导入侧表头别名要认「拆分」,否则整列被当未知列丢掉。"""
    from src.services.import_data.parsers.beecount import _HEADER_ALIASES

    assert "splits" in _HEADER_ALIASES
    assert "拆分" in _HEADER_ALIASES["splits"]


# --------------------------------------------------------------------------
# 20: MCP update_transaction 的 splits —— 这条链此前**三层都漏了**
# --------------------------------------------------------------------------
# create 有 splits、update 没有,于是 agent 建了拆分交易却改不了,只能删了重建。
# 底层 mutator 一直支持(payload 是 dict)。
#
# ⚠️ 三件事必须对齐,否则测试会「看起来在跑、其实什么都没执行」:
#
#   1. **它是 async** —— 同步调用只会拿到一个**从未被 await 的 coroutine**,
#      什么都没发生,而测试照样往下走。要 `asyncio.run`。
#   2. **`write_tools` 用模块级 `SessionLocal` 做预检查**,不是 `get_db`
#      —— 不注入测试库的话它查的是另一个库,报 "Transaction not found"。
#   3. **sync_id 既不在写响应、也不在读响应里** —— MCP 按 `(user_id, sync_id)`
#      查,只能从库里取。


def _mcp_leg_shape(client, H):
    """读列表端点挑出那笔 5000 的拆分交易,返回 (读端 id, {account_id: 金额})。"""
    txs = client.get("/api/v1/read/ledgers/lg1/transactions", headers=H).json()
    tx = next(t for t in txs if t["amount"] == 5000.0)
    legs = {s["account_id"]: float(s["amount"]) for s in (tx.get("splits") or [])}
    return tx["id"], legs


def _split_tx_sync_id(db, user):
    from src.models import ReadTxProjection
    row = (
        db.query(ReadTxProjection)
        .filter(ReadTxProjection.user_id == user.id,
                ReadTxProjection.amount == 5000.0)
        .order_by(ReadTxProjection.happened_at.desc())
        .first()
    )
    assert row is not None, "找不到那笔拆分交易"
    return row.sync_id


def test_mcp_update_transaction_can_rewrite_splits(monkeypatch):
    """MCP update 能改腿:旧的两条被替换,不是追加。"""
    from src.mcp.tools import write_tools

    client, TS, H, ids = _setup("mcpup1@t.com")
    monkeypatch.setattr(write_tools, "SessionLocal", TS)
    try:
        r = _create_split_tx(client, H, ids, [
            {"account_id": ids["招行卡"], "amount": 3000.0},
            {"account_id": ids["现金"], "amount": 2000.0},
        ])
        assert r.status_code == 200, r.text
        _, before = _mcp_leg_shape(client, H)
        assert before == {ids["招行卡"]: 3000.0, ids["现金"]: 2000.0}

        from src.models import User
        with TS() as db:
            user = db.query(User).filter(User.email == "mcpup1@t.com").one()
            sync_id = _split_tx_sync_id(db, user)

        # ⚠️ **必须同时传 amount**:update 路径用 `exclude_unset=True`,
        # 不传 amount 时 mutator 的 `_normalize_splits` 拿不到父总额 ->
        # 判为不自洽 -> **静默丢弃整份 splits** 并告警
        # (snapshot_mutator.py:638)。不报错、不返回失败,腿就是没变。
        asyncio.run(write_tools.update_transaction(
            user, sync_id=sync_id,
            amount=5000.0,
            splits=[{"account_id": ids["现金"], "amount": 4000.0},
                    {"account_id": ids["招行卡"], "amount": 1000.0}],
        ))

        _, after = _mcp_leg_shape(client, H)
        assert after == {ids["现金"]: 4000.0, ids["招行卡"]: 1000.0}
    finally:
        app.dependency_overrides.clear()


def test_mcp_update_transaction_omitting_splits_keeps_legs(monkeypatch):
    """省略 splits = 不动。传 [] 才是清除 —— 键语义不能弄反。"""
    from src.mcp.tools import write_tools

    client, TS, H, ids = _setup("mcpup2@t.com")
    monkeypatch.setattr(write_tools, "SessionLocal", TS)
    try:
        r = _create_split_tx(client, H, ids, [
            {"account_id": ids["招行卡"], "amount": 3000.0},
            {"account_id": ids["现金"], "amount": 2000.0},
        ])
        assert r.status_code == 200, r.text
        _, before = _mcp_leg_shape(client, H)
        assert before == {ids["招行卡"]: 3000.0, ids["现金"]: 2000.0}

        from src.models import User
        with TS() as db:
            user = db.query(User).filter(User.email == "mcpup2@t.com").one()
            sync_id = _split_tx_sync_id(db, user)

        # 只改备注 —— 完全不碰 splits
        asyncio.run(write_tools.update_transaction(
            user, sync_id=sync_id, note="改个备注"))

        _, after = _mcp_leg_shape(client, H)
        assert after == before, f"只改备注不该动腿:{before} -> {after}"
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# 24: 批量入口也能建组合支付(此前 `BatchTransactionItem` 独缺 splits)   #
# --------------------------------------------------------------------------
#
# 它声称「跟 WriteTransactionCreateRequest 同 schema」,而那个 schema 有 splits。
# 缺了之后:MCP 批量导组合支付 → 父交易金额照记、账户照扣,**分摊明细没了** ——
# 而「无账户的支出」正是它本来的样子,**看不出异常**。
#
# ⚠️ 字段对照(`test_tx_field_parity`)能证明「进了 payload」,
# 这一条证明**真的落库了** —— 前者是结构,后者是行为。


def test_batch_create_supports_split_payment():
    client, TS, H, ids = _setup("batchsplit@t.com")
    try:
        r = client.post(
            "/api/v1/write/ledgers/lg1/transactions/batch",
            headers=H,
            json={
                "base_change_id": 0,
                "transactions": [{
                    "tx_type": "expense",
                    "amount": 5000.0,
                    "happened_at": "2026-10-03T12:00:00+00:00",
                    "category_name": "购物",
                    "category_kind": "expense",
                    "note": "批量组合支付",
                    "splits": [
                        {"account_id": ids["招行卡"], "amount": 3000.0},
                        {"account_id": ids["现金"], "amount": 2000.0},
                    ],
                }],
            },
        )
        assert r.status_code == 200, r.text

        txs = client.get("/api/v1/read/ledgers/lg1/transactions", headers=H).json()
        tx = next(t for t in txs if t["amount"] == 5000.0)
        legs = {s["account_id"]: float(s["amount"]) for s in (tx.get("splits") or [])}
        assert legs == {ids["招行卡"]: 3000.0, ids["现金"]: 2000.0}, (
            f"批量建的组合支付没落库腿:{legs}"
        )
    finally:
        app.dependency_overrides.clear()


def test_unrelated_edit_keeps_inconsistent_legs() -> None:
    """改一个**无关字段**不许抹掉既有腿(哪怕它们自相矛盾)。

    ## 为什么

    旧行为在 `snapshot_mutator.update_transaction` 末尾那个 try/except 里
    把不自洽的既有腿写成 `[]`。实测代价完全不成比例:

    | 父 5000 / 腿合计 4999 | 账户余额被扣 | 支出合计−账户变动 缺口 |
    |---|---|---|
    | 抹掉腿(旧) | **0** | **5000** |
    | 保留(现在) | 4999 | **1** |

    `_split_legs` 只按账户聚合腿、**从不检查合计**,而**有腿时父
    `account_*` 是空的** —— 父对余额零贡献。抹掉腿等于让整笔金额
    从所有账户余额上消失,而它还照样进统计。

    而「不变式」本来就**已经**被破坏:实测 `_merge_tx_splits_after_merge`
    与 `replace_tx_splits` **都不校验合计**,脏腿能从 `/sync/push` 落库。

    📌 用户**显式**改腿时仍然被拒(那条 `_normalize_splits` 抛出的异常
    不被这个 try 捕获)—— 这条测的正是「无关编辑不该毁数据」。
    """
    from src.snapshot_mutator import update_transaction

    snap = {"items": [], "accounts": [], "categories": [], "tags": []}
    snap["items"].append({
        "syncId": "tx_1", "type": "expense", "amount": 5000.0,
        "categoryName": "购物", "note": "旧备注",
        "accountName": None, "accountId": None,
        "happenedAt": "2026-10-03T12:00:00Z",
        "splits": [{"accountId": "acc-1", "amount": 3000.0},
                   {"accountId": "acc-2", "amount": 1999.0}],   # 4999 != 5000
    })

    new_snap = update_transaction(snap, "tx_1", {"note": "改成新备注"})
    item = new_snap["items"][0]

    assert item["note"] == "改成新备注", "无关字段本身必须更新成功"
    legs = item.get("splits")
    assert legs, (
        "改个备注把用户的腿抹掉了 —— 整笔金额会从所有账户余额上消失,"
        "而它还照样进统计(缺口 5000,而不是 1)"
    )
    assert len(legs) == 2, f"腿的数量被改了:{legs}"


def test_dirty_splits_survive_a_rest_note_patch() -> None:
    """**接线验证**:脏腿穿过真 REST PATCH 也不该被抹掉。

    ## 为什么需要这条

    163 轮修了「无关编辑抹掉脏腿」,但那条护栏只**直接调 mutator**。
    接线那一段(真 HTTP -> 真鉴权 -> 真 projection)没人验过。

    ## 为什么只有这条能造出「脏腿」

    REST / MCP 两条写路径都经过 `_normalize_splits`,**自相矛盾会被拒**。
    脏腿只能从 `/sync/push` 进来(`_merge_tx_splits_after_merge` 只看
    腿的形状,从不校验合计)—— 那条路径要 app scope,在本文件不可用。
    所以这里**真建一笔合法交易,再直接改库把腿改成不自洽**,
    等价于 sync 推脏腿之后的库状态。

    ⚠️ 我第一版把这条写进 `test_smoke_real_app.py`,那里的 token 是
    app scope,读端点和 PATCH **全 403**。**选错文件比写错断言更难查** ——
    断言写对了也是红的。
    """
    from sqlalchemy import select

    from src.models import ReadTxSplitProjection

    client, TS, H, ids = _setup("sp-dirty-patch@t.com")
    # ⚠️ `_create_split_tx` 返回的是 **response**,不是 tx_id
    # —— 我第一版直接当 tx_id 用,`KeyError: 0`。
    r0 = _create_split_tx(client, H, ids, legs=[
        {"account_id": ids["招行卡"], "amount": 3000.0},
        {"account_id": ids["现金"], "amount": 2000.0},
    ], amount=5000.0, note="原始备注")
    assert r0.status_code == 200, r0.text
    tx_id = r0.json().get("entity_id")
    assert tx_id, r0.json()

    def _balances() -> dict[str, float]:
        """真读余额(SQL 聚合路径),不是手算。"""
        from sqlalchemy import select as _sel

        from src.models import Ledger, UserAccountProjection
        from src.routers.read._shared import (
            account_balance_from_stats,
            account_balance_stats,
        )

        with TS() as db:
            lid = db.scalar(_sel(Ledger).where(
                Ledger.external_id == "lg1")).id
            stats = account_balance_stats(db, [lid])
            return {
                str(a.sync_id): account_balance_from_stats(
                    str(a.sync_id), float(a.initial_balance or 0.0), stats)
                for a in db.scalars(_sel(UserAccountProjection)).all()
            }

    # 造出 sync 会推上来的那种脏腿:3000 + 1999 != 5000
    # ⚠️ 必须同时限定 **seq** —— 我第一版只按 tx_sync_id 过滤,
    # 结果**两条腿**都被改成 1999(合计 3998),断言直接扑空。
    with TS() as db:
        changed = db.query(ReadTxSplitProjection).filter(
            ReadTxSplitProjection.tx_sync_id == tx_id,
            ReadTxSplitProjection.seq == 1,
        ).update({ReadTxSplitProjection.amount: 1999.0})
        db.commit()
        assert changed == 1, f"只该改到一条腿,实际改了 {changed} 条"

    # ⚠️ 161 轮那个「缺口 1 vs 5000」是我在探针里**手算**的。
    # 修复真正要保护的东西是**余额**,所以这里真读一次:
    # 无关编辑前后,两个账户的余额必须**一模一样**。
    before_bal = _balances()

    r = client.patch(
        f"/api/v1/write/ledgers/lg1/transactions/{tx_id}",
        # ⚠️ PATCH 必须带 base_change_id —— 漏了是 422 VALIDATION_ERROR,
        # 而不是 400,报错信息也不说缺哪个字段。
        json={"base_change_id": 0, "note": "Web 上改的备注"}, headers=H)
    assert r.status_code == 200, r.text
    # ⚠️ **不要**断言 `r.json()["note"]` —— 响应体里根本没有这个键,
    # 我第一版写了它,拿到 None。断言要落在**数据库**这个权威处。

    from src.models import ReadTxProjection

    with TS() as db:
        legs = db.scalars(
            select(ReadTxSplitProjection)
            .where(ReadTxSplitProjection.tx_sync_id == tx_id)
            .order_by(ReadTxSplitProjection.seq)).all()
        note = db.scalar(select(ReadTxProjection.note).where(
            ReadTxProjection.sync_id == tx_id))

    assert note == "Web 上改的备注", f"备注本身没更新成功:{note!r}"

    assert len(legs) == 2, (
        "改个备注把用户的腿抹掉了 —— 整笔金额会从所有账户余额上消失,"
        f"而它还照样进统计。现在库里剩:{legs}"
    )
    assert abs(sum(float(x.amount) for x in legs) - 4999.0) < 1e-6, legs

    after_bal = _balances()
    assert after_bal == before_bal, (
        f"改个备注让账户余额变了:{before_bal} -> {after_bal}。"
        "抹掉腿的话,整笔金额会从所有账户余额上消失。"
    )
    # 余额必须真的**含**了这笔腿(不是碰巧相等)
    moved = sum(before_bal.values()) - 2 * 100000.0
    assert abs(moved - (-4999.0)) < 1e-6, (
        f"两条腿合计 4999,余额合计应该减少 4999,实得 {moved}"
    )


def test_delete_tx_also_removes_its_legs() -> None:
    """删掉一笔组合支付交易,**腿也必须跟着删**。

    ## 实测过的严重 bug(修复前)

        删前腿数 = 2
        DELETE -> **200**(成功)
        删后腿数 = **2**   <- 悬空腿还在

    SQLite 默认**不开** FK cascade,而 `_split_legs`(`read/_shared.py`)
    按账户聚合**全部**腿、从不看父行还在不在 —— 于是**整笔金额永远扣在
    账户余额上**。用户删了交易,钱回不来。**HTTP 200,不报错,零信号。**

    这是 163 轮那个 bug 的**镜像**(那次是「无关编辑抹掉腿」,
    这次是「删除交易留下腿」)。CLAUDE.md 原来**没有**记这一条。

    ⚠️ DELETE 必须带 `base_change_id`,否则 **422**;而且 `client.delete()`
    这个 httpx 版本**不收 json**,得用 `client.request("DELETE", ...)`。
    """
    from sqlalchemy import func, select

    from src.models import ReadTxSplitProjection

    client, TS, H, ids = _setup("sp-del-probe@t.com")
    r0 = _create_split_tx(client, H, ids, legs=[
        {"account_id": ids["招行卡"], "amount": 3000.0},
        {"account_id": ids["现金"], "amount": 2000.0},
    ], amount=5000.0, note="待删")
    assert r0.status_code == 200, r0.text
    tx_id = r0.json().get("entity_id")

    d = client.request("DELETE", f"/api/v1/write/ledgers/lg1/transactions/{tx_id}",
                      headers=H, json={"base_change_id": 0})
    assert d.status_code == 200, d.text

    with TS() as db:
        after = db.scalar(select(func.count()).select_from(
            ReadTxSplitProjection).where(
            ReadTxSplitProjection.tx_sync_id == tx_id))

    assert after == 0, f"删掉交易后还剩 {after} 条悬空腿"


def test_split_payment_is_counted_in_analytics() -> None:
    """组合支付必须**整笔**计入统计(有腿时父 `account_sync_id` 是空的)。

    ## 为什么要有这条(实测出来的缺口)

    174 轮查到:统计查询**只按时间过滤、没有账户过滤**,所以组合支付
    现在**不会**从统计里消失。175 轮想验「有人加了账户过滤会怎样」,
    两次探针都坏了(只数 FAILED 漏掉 ERROR / 变异插进 if 块中间变 SyntaxError)。

    176 轮用 mutation_check 把变异做**对**了:给统计加
    `account_sync_id.is_not(None)` —— **26 条测试全绿,一条都没红**。

    > 📌 说明「组合支付计入统计」这件事**根本没有护栏**。
    > 之前那条 `expense_total == 6000` 的 6000 来自**普通交易**,不是组合支付。

    ⚠️ 这颗雷是真的:哪天给统计加了「按账户筛选」,组合支付会因为
    父账户为空而**整笔从统计里消失**,而余额那边照扣 ——
    用户会看到「账户少了 5000,但这个月支出里没有它」。

    ## 判据落在**读回来的数字**上

    一笔普通支出 1000 + 一笔组合支付 5000 -> `expense_total` 必须是 **6000**。
    掉到 5000 就说明组合支付被过滤掉了。
    """
    client, TS, H, ids = _setup("sp-analytics@t.com")

    client.post("/api/v1/write/ledgers/lg1/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 1000.0,
        "happened_at": "2026-10-03T12:00:00+00:00",
        "category_name": "餐饮", "category_kind": "expense",
        "note": "普通支出", "account_id": ids["招行卡"],
    })

    r0 = _create_split_tx(client, H, ids, legs=[
        {"account_id": ids["招行卡"], "amount": 3000.0},
        {"account_id": ids["现金"], "amount": 2000.0},
    ], amount=5000.0, note="组合支付")
    assert r0.status_code == 200, r0.text

    a = client.get("/api/v1/read/workspace/analytics", headers=H,
                   params={"scope": "all", "metric": "expense"}).json()
    assert abs(a["summary"]["expense_total"] - 6000.0) < 1e-6, (
        f"统计里没有把组合支付算进去(或被账户过滤掉了):{a['summary']}"
    )
