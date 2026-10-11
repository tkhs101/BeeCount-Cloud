"""**真服务**冒烟测试:alembic + 真路由 + 真 self-call。

## 为什么要有这一条(而单元测试不够)

`CLAUDE.md` 里记着一条血泪:

> **分层测试全绿 != 拼起来能工作。** 743 测试 + 160 前端测试全绿,
> 起真服务点一次手动触发就炸:`asyncio.run()` 不能在运行中的 loop 里调;
> selfcall 自己重造了一遍 MCP 已有的 `_self_call` 且连错三次;
> `repay_one` 的失败被端点吞成 HTTP 200,**用户看到的是「点了没反应」**;
> 时区口径和调度器不一致;幂等键不含金额 -> 409 把这一期彻底卡死。

那条 5 连击里有 4 条是**分层测试结构上就查不到**的:真路由、真依赖注入、
真鉴权、真 self-call 回环。本会话改了 15 轮(时区、账期、反向桥、自检口径、
新增的「本期应还」三字段),所以必须真的跑一遍。

## 与 `test_credit_autorepay_e2e.py` 的分工

那条已经覆盖「自动还款端到端穿过全链路」。这一条覆盖面不同:

| | 关注 |
|---|---|
| `test_credit_autorepay_e2e.py` | 自动还款这一条链路 |
| 本文件 | **装配**能不能起来 —— alembic、路由注册、依赖注入、鉴权、i18n/分类种子 |

它也是「新增字段真的出现在响应里」的唯一守门人:`statement_bill` /
`statement_outstanding` / `next_due_date` 是第 8 轮加的,分层测试全绿
**不代表**真的序列化出去了。

## 独立数据库

用 `DATABASE_URL` 指向临时库 + 手动 `alembic upgrade head`(真服务启动时
会跑,测试环境不会 —— 不跑就是 `no such table: users`)。
"""
from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

# 必须在 import src 之前设好。
#
# 文件名带 pid:`src.database` 的 engine 是**模块级全局**,连接会一直
# 持有,pytest 结束时删不掉(Windows PermissionError WinError 32)。
# 用唯一名避免与上一次运行撞,残留文件留给系统清理 Temp。
_TMP_DB = (pathlib.Path(tempfile.gettempdir())
           / f"beecount_smoke_{os.getpid()}.db")

#: 改过的环境变量 —— import 之后**立刻还原**(见下方)。
_PRE_ENV = {
    "REGISTRATION_ENABLED": os.environ.get("REGISTRATION_ENABLED"),
    "DATABASE_URL": os.environ.get("DATABASE_URL"),
    "AUTOREPAY_SCHEDULER": os.environ.get("AUTOREPAY_SCHEDULER"),
    "SCHEDULER_TIMEZONE": os.environ.get("SCHEDULER_TIMEZONE"),
}

os.environ["REGISTRATION_ENABLED"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"
os.environ["AUTOREPAY_SCHEDULER"] = "off"       # 别让后台任务干扰断言
os.environ["SCHEDULER_TIMEZONE"] = "Asia/Tokyo"

import sys                                                    # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from alembic import command                                   # noqa: E402
from alembic.config import Config                             # noqa: E402
from fastapi.testclient import TestClient                     # noqa: E402

from src.main import app                                      # noqa: E402

# ⚠️ **必须立刻还原**。这几项在 import `src.main` 时已被读取(engine 建好、
# 调度器按开关决定注册与否),之后不再需要 —— 留着会**污染同一次 pytest
# 会话里的其他测试**。
#
# 我第一版没还原,结果 `AUTOREPAY_SCHEDULER=off` 让
# `test_credit_autorepay_scheduler.py` 的 6 条全红(它按这个开关决定是否
# 注册调度器)。这类「import 即产生副作用」的模块必须做还原 ——
# 不然单跑绿、全量红,而且很难联想到是自己干的。
for _k, _v in _PRE_ENV.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

LEDGER = "smoke-ledger"


@pytest.fixture(scope="module")
def client():
    """真 app + 真路由 + 真鉴权 + 真 self-call。"""
    cfg = Config(str(pathlib.Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location",
                        str(pathlib.Path(__file__).resolve().parents[1] / "alembic"))
    command.upgrade(cfg, "head")
    with TestClient(app) as c:
        yield c
    # 不删 DB:engine 是模块级全局,连接仍持有,删会 PermissionError(WinError 32)


@pytest.fixture(scope="module")
def smoke(client):
    """注册一个用户并返回 (client, headers)。

    ⚠️ **整个仓库只能有一个 `TestClient(app)`** —— MCP 的
    `StreamableHTTPSessionManager` 是**模块级单例**,`.run()` 只能调一次,
    第二个 TestClient 会抛
    `RuntimeError: StreamableHTTPSessionManager .run() can only be called once`。
    所以两个冒烟文件必须合并、共用一个 client(我第一版分成两个文件,
    单独跑都绿、全量跑 3 个 ERROR)。
    """
    r = client.post("/api/v1/auth/register", json={
        "email": "smoke@e2e.com", "password": "SmokeTest123!x",
        "client_type": "web", "device_name": "smoke", "platform": "test"})
    assert r.status_code == 200, r.text
    H = {"Authorization": f"Bearer {r.json()['access_token']}",
         "X-Device-ID": "smoke", "Content-Type": "application/json"}
    return client, H


def test_smoke_full_chain(smoke) -> None:
    """一条链走完:建账本 -> 建卡 -> 记账 -> 读应还 -> 绑自动还款 -> 手动触发。"""
    client, H = smoke

    r = client.post("/api/v1/write/ledgers", headers=H, json={
        "ledger_id": LEDGER, "ledger_name": "冒烟账本", "currency": "JPY"})
    assert r.status_code == 200, r.text

    for name, atype, extra in (
        ("招行信用卡", "credit_card", {"billing_day": 10, "payment_due_day": 25}),
        ("招行储蓄卡", "bank_card", {"initial_balance": 100000.0}),
    ):
        r = client.post(f"/api/v1/write/ledgers/{LEDGER}/accounts", headers=H,
                        json={"name": name, "account_type": atype,
                              "currency": "JPY", "base_change_id": 0, **extra})
        assert r.status_code in (200, 201), f"{name}: {r.text}"

    # 写端点的响应里没有 id —— 从读端点拿(既有 e2e 测试也是这么做的)
    accs = client.get("/api/v1/read/workspace/accounts", headers=H).json()
    ids = {a["name"]: a["id"] for a in accs}
    assert set(ids) == {"招行信用卡", "招行储蓄卡"}, ids

    r = client.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H,
                    json={"tx_type": "expense", "amount": 30000.0,
                          "account_id": ids["招行信用卡"], "note": "午饭",
                          "happened_at": "2026-10-20T09:00:00Z",
                          "base_change_id": 0})
    assert r.status_code in (200, 201), r.text

    # ---- 「本期应还」三个字段真的出现在响应里(分层测试查不到这一层)----
    accs = client.get("/api/v1/read/workspace/accounts", headers=H).json()
    card = next(a for a in accs if a["name"] == "招行信用卡")
    for key in ("statement_bill", "statement_outstanding", "next_due_date"):
        assert key in card, f"响应里没有 {key}: {sorted(card)}"
    assert card["statement_bill"] == 30000.0, card["statement_bill"]
    assert card["next_due_date"], card["next_due_date"]

    # 储蓄卡不是信用卡 -> 不显示应还
    savings = next(a for a in accs if a["name"] == "招行储蓄卡")
    assert savings["statement_bill"] is None, savings

    # ---- 绑自动还款(字段名是 _id 不是 _sync_id)----
    r = client.patch(
        f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}",
        headers=H, json={"base_change_id": 0, "autorepay_enabled": True,
                         "autorepay_from_account_id": ids["招行储蓄卡"]})
    assert r.status_code == 200, r.text

    # ---- 手动触发:真 self-call 回环(历史上这里炸过 4 次)----
    r = client.post(
        f"/api/v1/write/ledgers/{LEDGER}/accounts/{ids['招行信用卡']}"
        "/autorepay/run", headers=H)
    assert r.status_code < 500, f"{r.status_code}: {r.text[:300]}"


def test_admin_me_requires_admin(smoke) -> None:
    """新加的 `/admin/me`:普通用户必须 403,不能像 `/admin/health` 那样 200。"""
    client, _ = smoke
    r = client.post("/api/v1/auth/register", json={
        "email": "smoke-admin@e2e.com", "password": "SmokeTest123!x",
        "client_type": "web", "device_name": "s", "platform": "t"})
    assert r.status_code == 200, r.text
    H = {"Authorization": f"Bearer {r.json()['access_token']}",
         "X-Device-ID": "s", "Content-Type": "application/json"}
    r = client.get("/api/v1/admin/me", headers=H)
    assert r.status_code == 403, f"{r.status_code}: {r.text[:200]}"


def test_default_categories_are_seeded(smoke) -> None:
    """放弃 App 只用 Web 时,新账本必须**自带**分类(否则连「餐饮」都得手建)。"""
    client, _ = smoke
    r = client.post("/api/v1/auth/register", json={
        "email": "smoke-cats@e2e.com", "password": "SmokeTest123!x",
        "client_type": "web", "device_name": "s", "platform": "t"})
    assert r.status_code == 200, r.text
    H = {"Authorization": f"Bearer {r.json()['access_token']}",
         "X-Device-ID": "s", "Content-Type": "application/json"}
    client.post("/api/v1/write/ledgers", headers=H, json={
        "ledger_id": "smoke-cats", "ledger_name": "分类冒烟", "currency": "JPY"})
    cats = client.get("/api/v1/read/workspace/categories", headers=H).json()
    assert len(cats) > 100, f"只有 {len(cats)} 条分类"


# --------------------------------------------------------------------------- #
# CSV 导出 -> 导入 真往返(组合支付 / 税额)                                #
# --------------------------------------------------------------------------- #


def _items(c, H) -> list:
    """`/workspace/transactions` 返回**分页对象** `{items, total}`,
    不是裸数组。"""
    page = c.get("/api/v1/read/workspace/transactions",
                 headers=H).json()
    return page["items"] if isinstance(page, dict) else page


def test_split_payment_survives_csv_roundtrip(smoke) -> None:
    """组合支付:导出 -> 导入,**腿必须还在**。

    CLAUDE.md 记过的静默数据损坏:「CSV 不导出 Splits 列 → 导入后组合支付
    变『无账户的支出』,账户余额全错」。
    """
    c, H = smoke
    # **独立的账户名** —— 账户是 user-global 的,重名会被写路径拒
    # (`duplicated account name`)。合并进同一个 app fixture 后,
    # 信用卡那条链路已经建了「招行信用卡」。
    for name, atype in (("全家便利店卡", "credit_card"), ("现金钱包", "bank_card")):
        r = c.post(f"/api/v1/write/ledgers/{LEDGER}/accounts", headers=H,
                   json={"name": name, "account_type": atype,
                         "currency": "JPY", "base_change_id": 0})
        assert r.status_code in (200, 201), f"{name}: {r.text[:200]}"

    accs = c.get("/api/v1/read/workspace/accounts", headers=H).json()
    by_name = {a["name"]: a["id"] for a in accs}
    assert "全家便利店卡" in by_name, list(by_name)

    # 记一笔组合支付:5000 = 卡 3000 + 现金 2000
    r = c.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H,
               json={"tx_type": "expense", "amount": 5000.0,
                     "note": "便利店", "happened_at": "2026-10-20T09:00:00Z",
                     "base_change_id": 0,
                     "splits": [{"account_id": by_name["全家便利店卡"],
                                 "amount": 3000.0},
                                {"account_id": by_name["现金钱包"],
                                 "amount": 2000.0}]})
    assert r.status_code in (200, 201), r.text

    # 读回确认腿在(这一层已有测试,这里是确认写路径真的落了库)
    txs = _items(c, H)
    mine = next((t for t in txs if t.get("note") == "便利店"), None)
    assert mine is not None, f"交易没写进去: {txs[:2]}"
    assert len(mine.get("splits") or []) == 2, (
        f"腿没落库: {mine.get('splits')}")

    # ---- 导出 ----
    r = c.get("/api/v1/read/workspace/transactions.csv", headers=H)
    assert r.status_code == 200, r.text[:200]
    text = r.text
    assert "3000" in text and "2000" in text, (
        f"导出的 CSV 里没有腿的金额 —— 那正是 CLAUDE.md 记的静默损坏。"
        f"\nCSV 内容:\n{text[:600]}")

    # 表头里必须有 splits 那一列(否则导入器匹配不到 -> 静默丢弃)
    header = text.splitlines()[0]
    assert "plits" in header, f"CSV 表头没有 splits 列: {header}"


def test_tax_amount_survives_csv_roundtrip(smoke) -> None:
    """税额:导出 -> 导入,税额必须还在,且 amount 仍是**含税总额**。"""
    c, H = smoke
    accs = c.get("/api/v1/read/workspace/accounts", headers=H).json()
    card = next(a["id"] for a in accs if a["name"] == "全家便利店卡")

    r = c.post(f"/api/v1/write/ledgers/{LEDGER}/transactions", headers=H,
               json={"tx_type": "expense", "amount": 1080.0,
                     "account_id": card, "note": "含税消费",
                     "tax_amount": 80.0,
                     "happened_at": "2026-10-21T09:00:00Z",
                     "base_change_id": 0})
    assert r.status_code in (200, 201), r.text

    txs = _items(c, H)
    mine = next((t for t in txs if t.get("note") == "含税消费"), None)
    assert mine is not None, f"交易没写进去: {txs[:2]}"
    # amount 语义不变:仍是实付总额
    assert abs(float(mine["amount"]) - 1080.0) < 1e-6, mine["amount"]
    assert abs(float(mine.get("tax_amount") or 0) - 80.0) < 1e-6, (
        f"税额没落库: {mine.get('tax_amount')}")

    r = c.get("/api/v1/read/workspace/transactions.csv", headers=H)
    assert r.status_code == 200, r.text[:200]
    assert "1080" in r.text and "80" in r.text, (
        f"导出丢了税额:\n{r.text[:600]}")
    assert "税额" in r.text or "Tax" in r.text, (
        f"CSV 表头没有税额列:\n{r.text.splitlines()[0]}")

def test_exported_csv_can_be_imported_back_with_splits_and_tax(smoke) -> None:
    """**真往返**:导出 -> 重新上传 -> preview,腿和税额都在。

    ## 为什么必须真的走一遍导入

    CLAUDE.md 记的静默损坏是双向的:导出少一列、或导入解析不出来,
    组合支付都会变成「无账户的支出」,账户余额全错 —— 而**两边都不报错**。

    分层测试(`test_tx_split_invariants.py`)直接写 projection 行,
    证明不了「导出的文本能被本仓自己的导入器解析回来」。这一条才证明得了。
    """
    c, H = smoke
    csv_text = c.get("/api/v1/read/workspace/transactions.csv",
                     headers=H).text
    assert csv_text.strip(), "导出是空的,没法测往返"

    # 导到一个**空账本** —— 目标账本里还没有账户,能看清解析出的东西
    c.post("/api/v1/write/ledgers", headers=H, json={
        "ledger_id": "csv-target", "ledger_name": "导入目标",
        "currency": "JPY"})

    r = c.post(
        "/api/v1/import/upload",
        headers={k: v for k, v in H.items() if k != "Content-Type"},
        files={"file": ("t.csv", csv_text.encode("utf-8"), "text/csv")},
        data={"target_ledger_id": "csv-target",
              # 客户端本地时区(东为正)。CSV 里的时间是本地墙钟。
              "tz_offset_minutes": "540"},
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    token = r.json()["import_token"]

    r = c.post(f"/api/v1/import/{token}/preview", headers=H, json={})
    assert r.status_code == 200, r.text[:300]
    summary = r.json()
    items = (summary.get("sample_transactions")
             or summary.get("samples")
             or summary.get("preview")
             or [])
    assert items, f"preview 没有样本: {sorted(summary)}"

    # 找到那笔组合支付
    mine = next((i for i in items
                 if i.get("splits") and len(i["splits"]) >= 2), None)
    assert mine is not None, (
        f"往返后组合支付的腿没了 —— 正是 CLAUDE.md 记的静默损坏。"
        f"\npreview 样本: {items[:3]}")

    # 腿的金额对得上(预览层给的是 [账户名, 金额] 二元组 —— 那时快照里
    # 还没有账户、给不出 sync_id,见 endpoints.py::_tx_to_payload)
    amounts = []
    for leg in mine["splits"]:
        assert isinstance(leg, (list, tuple)) and len(leg) == 2, (
            f"腿的形状不对: {leg!r}")
        amounts.append(str(leg[1]))
    assert any("3000" in a for a in amounts), amounts
    assert any("2000" in a for a in amounts), amounts

    # 税额那一笔也要在
    taxed = next((i for i in items if i.get("note") == "含税消费"), None)
    assert taxed is not None, f"含税那笔没进 preview: {items[:3]}"
