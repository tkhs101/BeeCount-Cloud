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


def test_smoke_full_chain(client) -> None:
    """一条链走完:注册 -> 建账本 -> 建卡 -> 记账 -> 读应还 -> 绑自动还款 -> 手动触发。"""
    r = client.post("/api/v1/auth/register", json={
        "email": "smoke@e2e.com", "password": "SmokeTest123!x",
        "client_type": "web", "device_name": "smoke", "platform": "test"})
    assert r.status_code == 200, r.text
    H = {"Authorization": f"Bearer {r.json()['access_token']}",
         "X-Device-ID": "smoke", "Content-Type": "application/json"}

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


def test_admin_me_requires_admin(client) -> None:
    """新加的 `/admin/me`:普通用户必须 403,不能像 `/admin/health` 那样 200。"""
    r = client.post("/api/v1/auth/register", json={
        "email": "smoke-admin@e2e.com", "password": "SmokeTest123!x",
        "client_type": "web", "device_name": "s", "platform": "t"})
    assert r.status_code == 200, r.text
    H = {"Authorization": f"Bearer {r.json()['access_token']}",
         "X-Device-ID": "s", "Content-Type": "application/json"}
    r = client.get("/api/v1/admin/me", headers=H)
    assert r.status_code == 403, f"{r.status_code}: {r.text[:200]}"


def test_default_categories_are_seeded(client) -> None:
    """放弃 App 只用 Web 时,新账本必须**自带**分类(否则连「餐饮」都得手建)。"""
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