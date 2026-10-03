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

from datetime import datetime, timedelta, timezone

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
