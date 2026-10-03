"""自动还款的端到端验证(阶段 3c)。

前面几层的测试都是分层的(纯日期 / 金额 SQL / 执行器 / 调度器)。这个文件
**穿过全部四层**,从 HTTP 请求到库里真的多出一笔转账。

## 为什么必须有这一层

分层测试全都绿、拼起来却不工作,是这类功能的经典失败模式:

- 写路径漏传 `autorepay_from_account_id` → 配置永远没落库
- projection 漏登记新列 → 配置写进去读不出来
- mutator 字段映射漏了一行 → `autorepay_enabled` 静默不生效
- 端点调用的 ledger id 口径与余额聚合不一致 → 余额算成 0

这四个都不会让任何单层测试变红。

## 验证的完整链路

1. `POST /write/ledgers` 建账本
2. `POST /write/ledgers/{id}/accounts` 建信用卡(带账单日/还款日)和储蓄卡
3. `PATCH` 绑定自动还款
4. 记一笔消费
5. **重跑投影**(模拟真实读路径)
6. 调执行器 → 库里真的多出一笔 `from=储蓄卡 → to=信用卡` 的 transfer
7. 储蓄卡余额真的少了,信用卡欠款真的清了
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
from src.models import ReadTxProjection, UserAccountProjection
from src.services.credit_card.repay import repay_one

LEDGER = "lg-e2e"


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


def _register(client, email: str):
    r = client.post("/api/v1/auth/register", json={
        "email": email, "password": "E2eRepay1!x", "client_type": "web",
        "device_name": "d", "platform": "test",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}",
            "X-Device-ID": "d", "Content-Type": "application/json"}


class WriteThrough:
    """把执行器发出的 self-call **真的写进库**,模拟 `_commit_write`。

    ⚠️ `ledger_id` 必须是 **internal**(uuid),因为 `read_tx_projection.ledger_id`
    存的是它。第一版传了 external(测试常量),交易就写进了「另一个账本」,
    余额读出来纹丝不动 —— 而所有断言都只看余额,看不出是写入失败还是计算错。
    """

    def __init__(self, TS, ledger_id: str, user_id: str):
        self.TS, self.ledger_id, self.user_id = TS, ledger_id, user_id
        self.n = 0
        self.headers_seen: list[dict] = []

    def __call__(self, db, *, method, path, body, headers):
        self.n += 1
        self.headers_seen.append(headers)
        # 路径里的必须是 **external id**(用户可见的那个),不是 internal。
        # 这条断言本身就是为了钉死 internal/external 的区分 ——
        # 混淆会让每次自动还款都 404。
        assert path == f"/api/v1/write/ledgers/{LEDGER}/transactions", (
            f"路径里的账本 id 不对(应为 external {LEDGER!r}):{path}")
        with self.TS() as wdb:
            wdb.add(ReadTxProjection(
                ledger_id=self.ledger_id, sync_id=f"tx-auto-{self.n}",
                user_id=self.user_id, tx_type=body["tx_type"],
                amount=body["amount"], account_sync_id=None,
                from_account_sync_id=body["from_account_id"],
                to_account_sync_id=body["to_account_id"],
                happened_at=datetime.fromisoformat(body["happened_at"]),
                source_change_id=1))
            wdb.commit()
        return {"entity_id": f"tx-auto-{self.n}"}


@pytest.fixture
def wired(env):
    """建好账本 + 两张账户,并绑定自动还款。返回 (client, TS, H, ids)。"""
    client, TS = env
    H = _register(client, f"e2e-{len(str(TS))}-{id(client) % 1000}@t.com")
    r = client.post("/api/v1/write/ledgers", headers=H, json={
        "ledger_id": LEDGER, "ledger_name": "日常", "currency": "JPY"})
    assert r.status_code == 200, r.text

    client.post(f"/api/v1/write/ledgers/{LEDGER}/accounts", headers=H, json={
        "base_change_id": 0, "name": "招行信用卡", "account_type": "credit_card",
        "initial_balance": 0.0, "billing_day": 10, "payment_due_day": 25})
    client.post(f"/api/v1/write/ledgers/{LEDGER}/accounts", headers=H, json={
        "base_change_id": 0, "name": "招行储蓄卡", "account_type": "bank_card",
        "initial_balance": 100000.0})

    accs = client.get("/api/v1/read/workspace/accounts", headers=H,
                      params={"ledger_id": LEDGER}).json()
    ids = {a["name"]: a["id"] for a in accs}
    return client, TS, H, ids


def _internal_of(TS) -> str:
    """库里真实的 internal ledger id。

    测试里到处用 `LEDGER` 那个 external 常量,而 projection 存的是 internal ——
    两者的混用会让写入落到「另一个账本」,读余额时纹丝不动。
    """
    with TS() as db:
        row = db.scalar(select(ReadTxProjection.ledger_id).limit(1))
        return str(row) if row else ""


def _balances(client, H):
    return {a["name"]: a["balance"] for a in
            client.get("/api/v1/read/workspace/accounts", headers=H,
                       params={"ledger_id": LEDGER}).json()}


# --------------------------------------------------------------------------- #
# 链路 1:绑定 → 落库 → 可读回                                                #
# --------------------------------------------------------------------------- #


def test_binding_is_persisted_and_readable(wired) -> None:
    client, TS, H, ids = wired
    r = client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                     headers=H, json={
                         "base_change_id": 0, "autorepay_enabled": True,
                         "autorepay_from_account_id": ids["招行储蓄卡"]})
    assert r.status_code == 200, r.text

    with TS() as db:
        row = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        assert row.autorepay_enabled is True, (
            "绑定没有落库 —— mutator 字段映射可能漏了 autorepay_enabled")
        assert row.autorepay_from_account_sync_id == ids["招行储蓄卡"], (
            "扣款账户没落库,或存的是名字而不是 sync_id")


def test_binding_with_wrong_currency_is_rejected(wired) -> None:
    """跨币种绑定当场被拒 —— 不能留到执行时。"""
    client, TS, H, ids = wired
    client.post(f"/api/v1/write/ledgers/{LEDGER}/accounts", headers=H, json={
        "base_change_id": 0, "name": "人民币卡", "account_type": "bank_card",
        "initial_balance": 50000.0, "currency": "CNY"})
    accs = client.get("/api/v1/read/workspace/accounts", headers=H,
                      params={"ledger_id": LEDGER}).json()
    cny = next(a for a in accs if a["name"] == "人民币卡")
    r = client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                     headers=H, json={
                         "base_change_id": 0, "autorepay_enabled": True,
                         "autorepay_from_account_id": cny["id"]})
    assert r.status_code in (400, 409, 422), (
        f"跨币种绑定应被拒绝,实际 {r.status_code}:{r.text[:200]}")


def test_binding_to_itself_is_rejected(wired) -> None:
    client, TS, H, ids = wired
    r = client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                     headers=H, json={
                         "base_change_id": 0, "autorepay_enabled": True,
                         "autorepay_from_account_id": ids["招行信用卡"]})
    assert r.status_code in (400, 409, 422), r.status_code


def test_pause_keeps_config(wired) -> None:
    """暂停是 `enabled=false`,**保留**配置 —— 用户「这个月先不还」期望
    关掉而不是删掉重填。"""
    client, TS, H, ids = wired
    client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                 headers=H, json={"base_change_id": 0, "autorepay_enabled": True,
                                  "autorepay_from_account_id": ids["招行储蓄卡"]})
    r = client.patch(
        f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
        headers=H, json={"base_change_id": 0, "autorepay_enabled": False})
    assert r.status_code == 200, r.text
    with TS() as db:
        row = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        assert row.autorepay_enabled is False
        assert row.autorepay_from_account_sync_id == ids["招行储蓄卡"], (
            "暂停把配置也清了 —— 用户下次要全部重填")


# --------------------------------------------------------------------------- #
# 链路 2:还款真的产生交易、真的改余额                                        #
# --------------------------------------------------------------------------- #


def test_repayment_flows_end_to_end(wired) -> None:
    client, TS, H, ids = wired
    # 绑定
    r = client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                     headers=H, json={
                         "base_change_id": 0, "autorepay_enabled": True,
                         "autorepay_from_account_id": ids["招行储蓄卡"]})
    assert r.status_code == 200, r.text

    # 记一笔 5000 消费(9/20,在 9/10~10/10 这期)
    r = client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-09-20T12:00:00+00:00",
        "account_id": ids["招行信用卡"], "category_name": "购物",
        "category_kind": "expense"})
    assert r.status_code == 200, r.text

    before = _balances(client, H)
    assert before["招行信用卡"] == pytest.approx(-5000.0), before
    # 自检:`read_tx_projection.ledger_id` 存的是 **internal id**(uuid),
    # 不是 `LEDGER` 那个 external id。写对断言的前提。
    with TS() as chk:
        _rows = chk.scalars(select(ReadTxProjection)).all()
        assert len(_rows) == 1, [(r.sync_id, r.tx_type) for r in _rows]
        internal_ledger = _rows[0].ledger_id
        assert internal_ledger != LEDGER, "internal id 居然等于 external id"

    # 跑自动还款(10/25 还 9/10~10/10 那期)
    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        user_id = card.user_id
        call = WriteThrough(TS, internal_ledger, user_id)
        out = repay_one(db, user_id=user_id, card=card,
                        today=datetime(2026, 10, 25, tzinfo=timezone.utc).date(),
                        self_call=call)

    assert out.status == "done", out
    assert out.amount == 5000.0, out

    after = _balances(client, H)
    assert after["招行信用卡"] == pytest.approx(0.0), (
        f"信用卡欠款没有清掉:{after}")
    assert after["招行储蓄卡"] == pytest.approx(95000.0), (
        f"储蓄卡没有扣款:{after}")

    # 幂等键与 device_id 正确
    assert call.headers_seen[0]["Idempotency-Key"].startswith("auto-repay:"), \
        call.headers_seen[0]
    assert call.headers_seen[0]["X-Device-ID"] == "auto-repay", \
        ("用了 web-console 会让 Web 端看不见这笔 —— "
         "sync/pull.py:78-79 会过滤同设备的变更")


def test_second_run_does_not_double_charge(wired) -> None:
    """同日重跑不会重复扣钱 —— 这是整个功能最关键的性质。"""
    client, TS, H, ids = wired
    client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                 headers=H, json={"base_change_id": 0, "autorepay_enabled": True,
                                  "autorepay_from_account_id": ids["招行储蓄卡"]})
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-09-20T12:00:00+00:00",
        "account_id": ids["招行信用卡"], "category_name": "购物",
        "category_kind": "expense"})
    today = datetime(2026, 10, 25, tzinfo=timezone.utc).date()
    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        uid = card.user_id
        call = WriteThrough(TS, _internal_of(TS), uid)
        r1 = repay_one(db, user_id=uid, card=card, today=today, self_call=call)
        r2 = repay_one(db, user_id=uid, card=card, today=today, self_call=call)

    assert r1.status == "done", r1
    assert r2.status in ("skipped_already_repaid", "skipped_zero_outstanding"), r2
    assert call.n == 1, f"写入了 {call.n} 笔 —— 重复扣钱"
    assert _balances(client, H)["招行储蓄卡"] == pytest.approx(95000.0)


def test_manual_repayment_suppresses_auto(wired) -> None:
    """用户自己先还了 → 自动任务跳过。"""
    client, TS, H, ids = wired
    client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                 headers=H, json={"base_change_id": 0, "autorepay_enabled": True,
                                  "autorepay_from_account_id": ids["招行储蓄卡"]})
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-09-20T12:00:00+00:00",
        "account_id": ids["招行信用卡"], "category_name": "购物",
        "category_kind": "expense"})
    # 用户手动还(10/12,账单日之后)
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "transfer", "amount": 5000.0,
        "happened_at": "2026-10-12T12:00:00+00:00",
        "from_account_id": ids["招行储蓄卡"], "to_account_id": ids["招行信用卡"]})

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        uid = card.user_id
        call = WriteThrough(TS, _internal_of(TS), uid)
        out = repay_one(db, user_id=uid, card=card,
                        today=datetime(2026, 10, 25, tzinfo=timezone.utc).date(),
                        self_call=call)

    assert out.status == "skipped_zero_outstanding", out
    assert call.n == 0, "用户已还清,不该再自动还一次"
    assert _balances(client, H)["招行储蓄卡"] == pytest.approx(95000.0)


def test_partial_repayment_end_to_end(wired) -> None:
    """储蓄卡只有 2000,卡欠 5000 → 还 2000,储蓄卡不为负。"""
    client, TS, H, ids = wired
    client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                 headers=H, json={"base_change_id": 0, "autorepay_enabled": True,
                                  "autorepay_from_account_id": ids["招行储蓄卡"]})
    # 把储蓄卡压到 2000
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 98000.0,
        "happened_at": "2026-09-05T12:00:00+00:00",
        "account_id": ids["招行储蓄卡"], "category_name": "买房",
        "category_kind": "expense"})
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-09-20T12:00:00+00:00",
        "account_id": ids["招行信用卡"], "category_name": "购物",
        "category_kind": "expense"})

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        uid = card.user_id
        call = WriteThrough(TS, _internal_of(TS), uid)
        out = repay_one(db, user_id=uid, card=card,
                        today=datetime(2026, 10, 25, tzinfo=timezone.utc).date(),
                        self_call=call)

    assert out.status == "done", out
    assert out.amount == 2000.0, f"应部分还款 2000:{out}"
    bal = _balances(client, H)
    assert bal["招行储蓄卡"] == pytest.approx(0.0), bal
    assert bal["招行信用卡"] == pytest.approx(-3000.0), bal


def test_not_due_day_does_nothing(wired) -> None:
    client, TS, H, ids = wired
    client.patch(f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
                 headers=H, json={"base_change_id": 0, "autorepay_enabled": True,
                                  "autorepay_from_account_id": ids["招行储蓄卡"]})
    client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H, json={
        "base_change_id": 0, "tx_type": "expense", "amount": 5000.0,
        "happened_at": "2026-09-20T12:00:00+00:00",
        "account_id": ids["招行信用卡"], "category_name": "购物",
        "category_kind": "expense"})
    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        uid = card.user_id
        call = WriteThrough(TS, _internal_of(TS), uid)
        out = repay_one(db, user_id=uid, card=card,
                        today=datetime(2026, 10, 20, tzinfo=timezone.utc).date(),
                        self_call=call)
    assert out.status == "skipped_not_due", out
    assert call.n == 0
    assert _balances(client, H)["招行储蓄卡"] == pytest.approx(100000.0)

def test_patch_of_unrelated_field_preserves_autorepay(wired) -> None:
    """改**别的**字段不能让自动还款配置消失。

    ## 这是本 fork 的第三次同类 bug

    - 第一次:`tax_amount` 在 `_projection_row_to_tx_dict` 里被漏掉
    - 第二次:`splits` 在同一个函数里被漏掉
    - 第三次(本条):`autorepay_*` 在 **`snapshot_builder`** 里被漏掉

    同一类形状:某条「projection → 内存结构」的反向桥漏了新列。
    漏了之后,任何一次**无关字段的更新**都会把用户配置静默清空 ——
    HTTP 200,不报错,而下个月还款日就不会扣款了。

    这条测试是唯一能抓住它的护栏:配置绑定之后 PATCH 一下 `note`,
    再读回来,配置必须还在。
    """
    client, TS, H, ids = wired
    r = client.patch(
        f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
        headers=H, json={"base_change_id": 0, "autorepay_enabled": True,
                         "autorepay_from_account_id": ids["招行储蓄卡"]})
    assert r.status_code == 200, r.text

    # 只改备注,完全不提自动还款
    r = client.patch(
        f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
        headers=H, json={"base_change_id": 0, "note": "顺手改个备注"})
    assert r.status_code == 200, r.text

    with TS() as db:
        row = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        assert row.autorepay_enabled is True, (
            "只改 note 就把 autherpay_enabled 清掉了 —— "
            "snapshot_builder 漏了这三列")
        assert row.autorepay_from_account_sync_id == ids["招行储蓄卡"], (
            "只改 note 就把扣款账户清掉了 —— snapshot_builder 漏了这三列")


def test_autorepay_config_survives_a_transaction_write(wired) -> None:
    """记一笔交易也不能把配置清掉。

    写路径每笔交易都会重建一次快照 —— 如果构建器漏了列,用户记一笔账
    就会丢掉自动还款配置。这条比上一条更贴近日常使用。
    """
    client, TS, H, ids = wired
    client.patch(
        f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
        headers=H, json={"base_change_id": 0, "autorepay_enabled": True,
                         "autorepay_from_account_id": ids["招行储蓄卡"]})
    r = client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H,
                    json={"base_change_id": 0, "tx_type": "expense",
                          "amount": 800.0,
                          "happened_at": "2026-09-20T12:00:00+00:00",
                          "account_id": ids["招行储蓄卡"],
                          "category_name": "餐饮", "category_kind": "expense"})
    assert r.status_code == 200, r.text
    with TS() as db:
        row = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == ids["招行信用卡"]))
        assert row.autorepay_enabled is True, (
            "记一笔交易就把自动还款配置清掉了")
        assert row.autorepay_from_account_sync_id == ids["招行储蓄卡"]
