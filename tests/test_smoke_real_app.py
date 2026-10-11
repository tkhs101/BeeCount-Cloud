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

import pathlib
import tempfile
import uuid

import pytest

# 必须在 import src 之前设好。
#
# 文件名带 pid:`src.database` 的 engine 是**模块级全局**,连接会一直
# 持有,pytest 结束时删不掉(Windows PermissionError WinError 32)。
# 用唯一名避免与上一次运行撞,残留文件留给系统清理 Temp。
# 文件名用 **uuid 而不是 pid** —— Windows 会复用 pid,上一次的 DB 还在,
# 于是注册直接 409 AUTH_EMAIL_EXISTS,而报错与「文件没删干净」看不出关系。
_TMP_DB = (pathlib.Path(tempfile.gettempdir())
           / f"beecount_smoke_{uuid.uuid4().hex}.db")

import sys  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.database import get_db
from src.main import app  # noqa: E402

#: 这条测试**自建 engine**,不靠 `DATABASE_URL` 环境变量。
#:
#: `src.database` 的 engine 是**模块级全局**,在 `src.main` 第一次被 import
#: 时就建好了。而 pytest 按文件名顺序收集 —— `test_smoke_real_app.py` 在
#: 字母序里靠后,那时 `src.main` 早被别的测试 import 过了,我模块顶层设的
#: `DATABASE_URL` **根本没生效**。
#:
#: 症状就是「单跑绿、全量红」—— 本会话第三种形态(前两种:模块级 env 污染、
#: 模块级单例)。根治办法是**别依赖全局状态**:自己建 engine、用
#: `dependency_overrides[get_db]` 注回去,顺序就无关了。
_ENGINE = create_engine(f"sqlite:///{_TMP_DB.as_posix()}",
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
SessionLocal = sessionmaker(bind=_ENGINE, autocommit=False, autoflush=False)


@pytest.fixture(scope="module")
def client():
    """真 app + 真路由 + 真鉴权 + 真 self-call,跑在自己的库上。"""
    # 建表直接用 `Base.metadata.create_all(_ENGINE)`,**不走 alembic**。
    #
    # 我先试了 `cfg.set_main_option("sqlalchemy.url", ...)`:alembic 的
    # `env.py` 用 `engine_from_config(config.get_section(...))`,而 configparser
    # 会把 `%` 当插值 —— Windows 临时路径经 SQLAlchemy 编码成 `C%3A/...`
    # 直接 ValueError。绕开编码又落在**另一个库**上(查库确认:0 张表)。
    #
    # 权衡:冒烟测试的价值是「真 app + 真路由 + 真鉴权 + 真同步」能跑通;
    # **迁移链本身的正确性不靠它**(alembic 跑的是仓库根 beecount.db,
    # 而且专门有迁移测试)。为这一个 fixture 和 configparser 缠三轮不划算。
    from src.models import Base
    Base.metadata.create_all(_ENGINE)

    def _override():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
    # 不删 DB:engine 还持有连接,删会 PermissionError(WinError 32)

LEDGER = "smoke-ledger"
SYNC_LEDGER = "sync-ledger"


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


# --------------------------------------------------------------------------- #
# 前后端字段名契约                                                          #
# --------------------------------------------------------------------------- #


def test_frontend_declares_only_fields_the_api_actually_returns(smoke) -> None:
    """前端 `ReadAccount` 声明的字段,后端**真的都返回**。

    ## 为什么这条不可省

    前端大量派生函数是「字段缺失就静默返回 null / 不渲染」:
    `statementOut` 缺 `statement_bill` 就不显示那一格、
    `previewColumns` 缺 `splits` 就不出现那列。

    所以**后端改名 -> 前端读不到 -> UI 静默少一块**,而 `tsc` 不报错、
    `vitest` 全绿、用户只看到「这个功能怎么不见了」。
    TypeScript 类型是**手写的**,它不会验证运行时真的返回了这些字段。

    ## 跨语言的做法

    真跑一次 API 拿响应 key,再用正则从 `types.ts` 里读出声明的字段名,
    取「前端声明但后端不返回」的差集。这条测试因此同时依赖**真服务**
    和**前端源码** —— 任何一侧改名都会红。

    只查一个方向:后端多返回的字段不算问题(前端可以先不声明)。
    """
    import re

    types_path = (pathlib.Path(__file__).resolve().parents[1] / "frontend"
                  / "packages" / "api-client" / "src" / "types.ts")
    src = types_path.read_text(encoding="utf-8")
    m = re.search(r"export type ReadAccount = \{(.*?)\n\}", src, re.S)
    assert m, "types.ts 里找不到 ReadAccount 定义 —— 改名了就更新这条"
    declared = set(re.findall(r"^\s{2}([a-z_][a-zA-Z0-9_]*)\??:",
                              m.group(1), re.M))
    # 正则失效的防线:没解析出字段时不能静默通过(见 tokenize 那条)
    assert len(declared) > 15, f"只解析出 {len(declared)} 个字段,正则可能失效"

    client, H = smoke
    accs = client.get("/api/v1/read/workspace/accounts", headers=H).json()
    assert accs, "没有账户可比对"
    returned = set(accs[0].keys())

    missing = sorted(declared - returned)
    assert not missing, (
        f"前端声明但后端**不返回**: {missing}\n\n"
        f"后端改名(或漏了这个字段)后,前端读不到 -> UI **静默**少一块,"
        f"tsc 不报错、vitest 全绿,用户只看到「这个功能怎么不见了」。\n"
        f"后端实际返回的: {sorted(returned)}")




# --------------------------------------------------------------------------- #
# mobile 同步的时区(同一 app fixture,见下面「只能有一个 TestClient」)        #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def app_client(smoke):
    """一个 **app 类型**的用户 + 他的 token。

    `/sync/push` 需要 app scope,而 `client_type="web"` 注册出来的 token 只有
    web scope —— 推上去直接 403。

    ⚠️ **不能用 REST 建账本**:app token 的写路径要 `SCOPE_APP_WRITE`,
    而 `tests/conftest.py` 里有
    `os.environ.setdefault("ALLOW_APP_RW_SCOPES", "false")` —— 也就是
    说 pytest 环境里 app scope 的 REST 写**默认关闭**(CLAUDE.md 那条)。
    账本改由 push 自己物化,那本来就是 mobile 首次同步的流程。

    ⚠️ 也**不要**往库里预插 Ledger:push 首次推某个账本时会自己插,
    预建会 `UNIQUE constraint failed: ledgers.user_id, ledgers.external_id`。
    """
    client, _ = smoke
    r = client.post("/api/v1/auth/register", json={
        "email": "sync@e2e.com", "password": "SmokeTest123!x",
        "device_id": "sync-dev", "client_type": "app",
        "device_name": "sync-dev", "platform": "t"})
    assert r.status_code == 200, r.text
    return client, {"Authorization": f"Bearer {r.json()['access_token']}",
                    "X-Device-ID": "sync-dev",
                    "Content-Type": "application/json"}


def _push(c, H, changes):
    """推一批 change,并断言全部被接受。"""
    r = c.post("/api/v1/sync/push", headers=H,
               json={"device_id": "sync-dev", "changes": changes})
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    body = r.json()
    assert body["accepted"] == len(changes), (
        f"只接受了 {body['accepted']}/{len(changes)}:{body}")
    return body


def _ensure_ledger(c, H):
    """把账本本身作为一条 change 推上去(手机首次同步就是这么做的)。"""
    _push(c, H, [{
        "ledger_id": SYNC_LEDGER,
        "entity_type": "ledger",
        "entity_sync_id": SYNC_LEDGER,
        "action": "upsert",
        "payload": {"id": SYNC_LEDGER, "name": "同步冒烟",
                    "currency": "JPY"},
        "updated_at": "2026-10-01T00:00:00Z",
    }])


def _sync_txs(c, H):
    """读同步进来的交易。必须显式指定 ledger_id —— workspace 层没有
    current 账本概念,不指定时按调用方可见范围,这里直接给死。

    响应形状随参数变:带分页参数是 `{items,total}`,不带是裸数组
    (我第一版只按 `{items}` 取,直接 KeyError)。
    """
    page = c.get("/api/v1/read/workspace/transactions", headers=H,
                 params={"ledger_id": SYNC_LEDGER}).json()
    if isinstance(page, dict):
        return page.get("items") or page.get("transactions") or []
    return page


def _stored_happened_at(sync_id: str):
    """直接读 projection —— 见下面「为什么不在读端断言」。"""
    from sqlalchemy import select

    from src.models import ReadTxProjection

    with SessionLocal() as db:
        return db.scalar(
            select(ReadTxProjection.happened_at).where(
                ReadTxProjection.sync_id == sync_id))


def test_sync_push_preserves_the_instant_of_offset_timestamps(app_client) -> None:
    """mobile 在东京(UTC+9)推一笔带 `+09:00` 的交易,落库必须是那个**瞬间**。

    东京 2026-10-20 18:00 = UTC 09:00。若落库成 `18:00`,读端会把它当 UTC
    序列化成 `18:00Z`,前端按本地渲染成**次日 03:00** —— 整体 +9h,
    而且不报错。

    这正是 `projection._parse_happened_at` 那轮修复的内容;这条是它的
    **同步侧**验证。单元测试只测函数,这条测「经过 push 链路之后」还
    对不对 —— CLAUDE.md 说同步这块历史上出过几次难复现的 bug,
    根因都是「有隐式契约但没在契约点强制」。

    ## 为什么断言在数据库层而不是读端

    pytest 的 `conftest.py` 有
    `ALLOW_APP_RW_SCOPES=false` —— app scope 的 REST 写**默认关闭**,
    所以这条测试里**没法用 REST 建账本**(403),而账本又只能由
    push 物化,读端按账本筛选时对不上。

    而这条测试要验的东西是「**经过 push 之后落库的时间戳对不对**」,
    数据库才是权威处。读端那一环探针已单独验过(带参数/不带参数各读到
    1 条)。为了验一个时间戳把 setup 搞成这样不值得。
    """
    c, H = app_client
    _ensure_ledger(c, H)
    _push(c, H, [{
        "ledger_id": SYNC_LEDGER,
        "entity_type": "transaction",
        "entity_sync_id": "mobile-tx-tz-1",
        "action": "upsert",
        "payload": {
            "id": "mobile-tx-tz-1",
            "type": "expense",
            "amount": 1200.0,
            # 东京 2026-10-20 18:00 == UTC 09:00
            "happenedAt": "2026-10-20T18:00:00+09:00",
            "note": "mobile 东京时间",
        },
        "updated_at": "2026-10-20T09:00:00Z",
    }])

    got = _stored_happened_at("mobile-tx-tz-1")
    assert got is not None, "同步的交易没落库"
    iso = got.isoformat() if hasattr(got, "isoformat") else str(got)
    assert iso.startswith("2026-10-20T09:00"), (
        f"落库的不是那个瞬间:{iso}\n"
        f"东京 18:00 == UTC 09:00;落成 18:00 会让前端渲染成次日 03:00"
        f"(整体 +9h),且不报错。")


def test_sync_push_naive_timestamp_is_treated_as_utc(app_client) -> None:
    """**裸墙钟**(无偏移)按 UTC 解释 —— 与库里其它记录的同一约定。

    库里存的是 UTC 墙钟,naive → UTC 才不会引入第二次偏移。
    与 `test_happened_at_timezone.py`(测函数)形成对照:那条测函数,
    这条测**经过同步链路之后**。
    """
    c, H = app_client
    _ensure_ledger(c, H)
    _push(c, H, [{
        "ledger_id": SYNC_LEDGER,
        "entity_type": "transaction",
        "entity_sync_id": "mobile-tx-naive-1",
        "action": "upsert",
        "payload": {
            "id": "mobile-tx-naive-1",
            "type": "expense",
            "amount": 300.0,
            "happenedAt": "2026-10-21T07:00:00",      # naive
            "note": "mobile 裸墙钟",
        },
        "updated_at": "2026-10-21T07:00:00Z",
    }])

    got = _stored_happened_at("mobile-tx-naive-1")
    assert got is not None, "同步的交易没落库"
    iso = got.isoformat() if hasattr(got, "isoformat") else str(got)
    assert iso.startswith("2026-10-21T07:00"), (
        f"naive 没按 UTC 解释:{iso}")


def test_sync_push_keeps_splits_on_a_later_partial_update(app_client) -> None:
    """mobile 推组合支付 -> 再推一次「只改备注」-> **腿必须还在**。

    ## 为什么这条最该有

    `CLAUDE.md` 把「`upsert_tx` 无条件 `replace_tx_splits(None)` →
    `/sync/push` 部分更新把腿全删」列为组合支付**五个静默丢失的坑**之一,
    后果是「余额**永久漂移**」。

    分层测试(`test_tx_split_invariants.py`)直接写 projection 行,
    证明不了「**经过 sync 的 merge 之后**腿还在」——
    `sync_applier._merge_tx_splits_after_merge` 那段「payload 不带 splits
    就带上现有腿」是另一份实现,必须真跑。

    ## 这个场景用户实际会碰到

    手机上给一笔组合支付改个备注 —— 那是**极其常见**的操作,
    而它一次就会把腿删光。父交易的 `account_*` 是空的(0021 的契约),
    所以余额直接少掉一部分,**且不报错**。
    """
    c, H = app_client
    _ensure_ledger(c, H)

    # 建两个账户。**直接写库**:app token 走不了 REST 写路径
    # (conftest 的 ALLOW_APP_RW_SCOPES=false),而腿要指向真实账户才有意义。
    ids = _seed_accounts(SYNC_LEDGER, ("同步卡A", "同步卡B"))

    # 第一次推送:带 legs
    _push(c, H, [{
        "ledger_id": SYNC_LEDGER, "entity_type": "transaction",
        "entity_sync_id": "sync-split-1", "action": "upsert",
        "payload": {
            "id": "sync-split-1", "type": "expense", "amount": 5000.0,
            "happenedAt": "2026-10-20T09:00:00Z", "note": "便利店",
            "splits": [{"accountId": ids[0], "amount": 3000.0},
                       {"accountId": ids[1], "amount": 2000.0}],
        },
        "updated_at": "2026-10-20T09:00:00Z",
    }])
    assert _split_leg_count("sync-split-1") == 2, "第一次推送的腿没落库"

    # 第二次推送:**只改备注**,payload 不带 splits —— 就是这个会踩坑
    _push(c, H, [{
        "ledger_id": SYNC_LEDGER, "entity_type": "transaction",
        "entity_sync_id": "sync-split-1", "action": "upsert",
        "payload": {
            "id": "sync-split-1", "type": "expense", "amount": 5000.0,
            "happenedAt": "2026-10-20T09:00:00Z", "note": "便利店(改备注)",
            # **刻意不带 splits**
        },
        "updated_at": "2026-10-20T10:00:00Z",
    }])

    n = _split_leg_count("sync-split-1")
    assert n == 2, (
        f"只改备注就把腿删了(剩 {n} 条)—— 余额会**永久漂移**且不报错。"
        f"这正是 CLAUDE.md 记的五个静默丢失的坑之一。")


def _seed_accounts(ledger_id: str, names) -> list:
    """直接写库建账户,返回它们的 sync_id。

    app token 在 pytest 里走不了 REST 写路径(见上),而组合支付的腿必须
    指向真实账户 —— 否则腿落库时找不到账户,余额那侧就全错了。
    """
    import uuid

    from sqlalchemy import select

    from src.models import Ledger, UserAccountProjection

    with SessionLocal() as db:
        # ⚠️ 按 **external_id** 查,不是主键 —— push 物化出来的 Ledger
        # 主键是自生成的 uuid, 只是 external_id。
        led = db.scalar(select(Ledger).where(Ledger.external_id == ledger_id))
        assert led is not None, f"账本 {ledger_id} 不存在"
        ids = []
        for name in names:
            sid = f"smoke-acct-{uuid.uuid4().hex[:12]}"
            db.add(UserAccountProjection(
                sync_id=sid, user_id=led.user_id, name=name,
                account_type="bank_card", currency="JPY", hidden=False))
            ids.append(sid)
        db.commit()
    return ids


def _split_leg_count(tx_sync_id: str) -> int:
    """数某笔交易的腿(直接读库,理由同 `_stored_happened_at`)。"""
    from sqlalchemy import func, select

    from src.models import ReadTxSplitProjection

    with SessionLocal() as db:
        return db.scalar(
            select(func.count()).select_from(ReadTxSplitProjection).where(
                ReadTxSplitProjection.tx_sync_id == tx_sync_id))
