"""MCP 交易附件(F3 / 上游 issue #513)。

REST 层(`/attachments/upload`、sha256 去重、`attachment_files` 表、
`attachments_json` 关联、孤儿 GC、Web 端展示)早已存在且在跑,缺的只是 MCP
那一层接线。这里锁的是接线本身。

三个易错点:
- `deps.py:290-297` 拒绝 PAT 进普通端点 → 只能走 self-call 短期 JWT
- `_self_call` 是 `**kwargs` 透传,multipart 的 `files=`/`data=` **本来就能用**
  (早期调查误判为只支持 json=),实测确认
- `AttachmentRef.cloudFileId` 是孤儿 GC 的反向引用依据,字段名写错会让
  刚传的文件被当成孤儿清掉
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.mcp.server import mcp
from src.mcp.tools import write_tools
from src.models import AttachmentFile, Ledger, ReadTxProjection, User

# 一张 1x1 的合法 PNG,当"小票照片"的替身
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


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
        "email": email, "password": "Pa$$word1!",
        "device_id": device_id, "client_type": client_type,
        "device_name": f"pytest-{client_type}", "platform": "test",
    }
    client.post("/api/v1/auth/register", json=creds)
    return client.post("/api/v1/auth/login", json=creds).json()["access_token"]


def _push(client, hdr, ledger_id, entity_type, sync_id, payload):
    r = client.post(
        "/api/v1/sync/push", headers=hdr,
        json={"device_id": "d-app", "changes": [{
            "ledger_id": ledger_id, "entity_type": entity_type,
            "entity_sync_id": sync_id, "action": "upsert",
            "updated_at": _iso(), "payload": payload,
        }]},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _fetch_user(TS, email):
    with TS() as db:
        row = db.scalar(select(User).where(User.email == email))
        assert row is not None
        db.expunge(row)
        return row


def _setup(client, TS, monkeypatch, email):
    """注册 + 建分类 + 推一笔交易,并把 write_tools 接到测试库。

    `_internal_token` 被换成同时带 `web_write` 的:本仓 `tests/conftest.py`
    把 `ALLOW_APP_RW_SCOPES` 钉成 `false`(`_WRITE_SCOPE_DEP` 因此退化成
    `require_scopes(WEB_WRITE)`),而 MCP self-call 默认只签 `app_write`。
    **这是测试环境的刻意收紧,生产默认 True**(Dockerfile `ALLOW_APP_RW_SCOPES=true`)。
    换 scope 只影响「能不能过依赖」,被测的附件逻辑(UPLOAD multipart →
    file_id → PATCH → attachments_json 落库)完全走真实 self-call,未打桩。
    """
    token = _register_and_token(client, email, device_id="d-app", client_type="app")
    hdr = {"Authorization": f"Bearer {token}"}
    _push(client, hdr, "lg1", "category", "c1",
          {"syncId": "c1", "name": "餐饮", "kind": "expense", "level": 1})
    _push(client, hdr, "lg1", "transaction", "tx1",
          {"syncId": "tx1", "type": "expense", "amount": 3280.0,
           "happenedAt": _iso(), "categoryId": "c1", "categoryName": "餐饮",
           "categoryKind": "expense"})

    from datetime import timedelta

    from src.security import SCOPE_APP_WRITE, SCOPE_WEB_WRITE, _create_token

    monkeypatch.setattr(write_tools, "SessionLocal", TS)
    monkeypatch.setattr(
        write_tools, "_internal_token",
        lambda u: _create_token(
            sub=u.id, token_type="access", expires_delta=timedelta(seconds=60),
            scopes=[SCOPE_APP_WRITE, SCOPE_WEB_WRITE], client_type="app",
        ),
    )
    return _fetch_user(TS, email)


# --------------------------------------------------------------------------- #
# base64 解码与大小护栏                                                        #
# --------------------------------------------------------------------------- #


def test_decode_base64_accepts_raw_and_data_url():
    """裸 base64 与 data: 前缀都要收 —— 与官方同名工具行为一致。"""
    raw, mime = write_tools._decode_base64_image(base64.b64encode(_PNG).decode())
    assert raw == _PNG and mime is None

    raw2, mime2 = write_tools._decode_base64_image(
        "data:image/png;base64," + base64.b64encode(_PNG).decode()
    )
    assert raw2 == _PNG
    assert mime2 == "image/png"


def test_decode_base64_rejects_garbage():
    with pytest.raises(ValueError):
        write_tools._decode_base64_image("")
    with pytest.raises(ValueError):
        write_tools._decode_base64_image("data:image/png;base64,")


def test_decode_base64_rejects_oversized_before_decoding(monkeypatch):
    """超大 base64 必须在**解码前**就被拒 —— 否则一个几百 MB 的串会先把
    内存吃满再报错。"""
    monkeypatch.setattr(write_tools, "get_settings", lambda: type(
        "S", (), {"attachment_max_upload_bytes": 1024})())
    # 声称 100KB,但不给真实内容 —— 只测「解码前就被拒」这条路径
    huge = "A" * (100_000 * 4 // 3)
    with pytest.raises(ValueError, match="too large"):
        write_tools._decode_base64_image(huge)


def test_guess_mime_from_extension_and_fallback():
    assert write_tools._guess_mime("receipt.png", None) == "image/png"
    assert write_tools._guess_mime(None, "image/heic") == "image/heic"
    assert write_tools._guess_mime("x.bin", None) == "application/octet-stream"
    assert write_tools._guess_mime(None, None) == "image/jpeg"


def test_load_attachments_tolerates_bad_json():
    assert write_tools._load_attachments(None) == []
    assert write_tools._load_attachments("{not json") == []
    assert write_tools._load_attachments('{"a":1}') == []  # 不是 list
    assert write_tools._load_attachments('[{"cloudFileId":"x"},"bad"]') == [
        {"cloudFileId": "x"}
    ]


def test_build_attachment_ref_shape():
    """AttachmentRef 字段名必须与前端 types.ts:126-135 一致 ——
    `cloudFileId` 是孤儿 GC 的反向引用依据,写错会被当孤儿清掉。"""
    ref = write_tools._build_attachment_ref(
        {"file_id": "abc-123", "sha256": "ff" * 32, "size": 999,
         "file_name": "receipt.jpg"},
        sort_order=2,
    )
    assert ref["cloudFileId"] == "abc-123"
    assert ref["cloudSha256"] == "ff" * 32
    assert ref["fileName"] == "abc-123_receipt.jpg", "fileName 是 <file_id>_<原名>"
    assert ref["originalName"] == "receipt.jpg"
    assert ref["sortOrder"] == 2
    with pytest.raises(ValueError, match="no file_id"):
        write_tools._build_attachment_ref({}, sort_order=0)


# --------------------------------------------------------------------------- #
# MCP 注册                                                                    #
# --------------------------------------------------------------------------- #


def test_mcp_registers_attachment_tools():
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in ("attach_receipt", "create_transaction_with_receipt"):
        assert name in tools, sorted(tools)
        assert "image_base64" in tools[name].inputSchema["properties"], name
    assert "tax_amount" in tools["create_transaction_with_receipt"].inputSchema["properties"]


# --------------------------------------------------------------------------- #
# 端到端:self-call multipart 真的能走通                                       #
# --------------------------------------------------------------------------- #


def test_attach_receipt_uploads_and_links(monkeypatch) -> None:
    """真跑一遍:upload multipart → 拿 file_id → PATCH 交易 → attachments_json
    落库 → attachment_files 有行。走真实 self-call,不打桩。"""
    client, TS = _make_client()
    try:
        user = _setup(client, TS, monkeypatch, "att-ok@t.com")
        out = asyncio.run(write_tools.attach_receipt(
            user, sync_id="tx1", image_base64=base64.b64encode(_PNG).decode(),
            file_name="kingbear.png",
        ))
        assert out["sync_id"] == "tx1"
        assert out["attachment_count"] == 1
        assert out["attachment"]["cloudFileId"]

        with TS() as db:
            led = db.scalar(select(Ledger).where(Ledger.external_id == "lg1"))
            tx = db.scalar(select(ReadTxProjection).where(
                ReadTxProjection.ledger_id == led.id,
                ReadTxProjection.sync_id == "tx1"))
            atts = json.loads(tx.attachments_json)
            assert len(atts) == 1
            assert atts[0]["cloudFileId"] == out["attachment"]["cloudFileId"]
            # 孤儿 GC 靠这个字段反查,必须真有行
            stored = db.scalar(select(AttachmentFile).where(
                AttachmentFile.id == atts[0]["cloudFileId"]))
            assert stored is not None
            assert stored.sha256 == out["attachment"]["cloudSha256"]
    finally:
        app.dependency_overrides.clear()


def test_attach_receipt_appends_not_replaces(monkeypatch) -> None:
    """再传一张 → 追加,sortOrder 递增,不是覆盖。"""
    client, TS = _make_client()
    try:
        user = _setup(client, TS, monkeypatch, "att-append@t.com")
        first = asyncio.run(write_tools.attach_receipt(
            user, sync_id="tx1", image_base64=base64.b64encode(_PNG).decode()))
        second = asyncio.run(write_tools.attach_receipt(
            user, sync_id="tx1",
            image_base64=base64.b64encode(_PNG + b"\x00").decode()))
        assert second["attachment_count"] == 2
        assert second["attachment"]["sortOrder"] == 1

        with TS() as db:
            led = db.scalar(select(Ledger).where(Ledger.external_id == "lg1"))
            tx = db.scalar(select(ReadTxProjection).where(
                ReadTxProjection.ledger_id == led.id,
                ReadTxProjection.sync_id == "tx1"))
            atts = json.loads(tx.attachments_json)
        assert [a["sortOrder"] for a in atts] == [0, 1]
        assert first["attachment"]["cloudFileId"] != second["attachment"]["cloudFileId"]
    finally:
        app.dependency_overrides.clear()


def test_attach_receipt_rejects_unknown_tx(monkeypatch) -> None:
    """附加**不是创建** —— sync_id 不存在必须报错,不能偷偷建一笔。"""
    client, TS = _make_client()
    try:
        user = _setup(client, TS, monkeypatch, "att-missing@t.com")
        with pytest.raises(ValueError, match="not found"):
            asyncio.run(write_tools.attach_receipt(
                user, sync_id="nope",
                image_base64=base64.b64encode(_PNG).decode()))
    finally:
        app.dependency_overrides.clear()


def test_create_transaction_with_receipt(monkeypatch) -> None:
    """一步建交易 + 附图。"""
    client, TS = _make_client()
    try:
        user = _setup(client, TS, monkeypatch, "att-new@t.com")

        out = asyncio.run(write_tools.create_transaction_with_receipt(
            user, amount=3280.0, tax_amount=298.0, category="餐饮",
            note="KING BEAR NOW",
            image_base64=base64.b64encode(_PNG).decode(),
        ))
        assert out["sync_id"] and out["sync_id"] != "tx1"
        assert out["attachment"]["cloudFileId"]

        with TS() as db:
            led = db.scalar(select(Ledger).where(Ledger.external_id == "lg1"))
            tx = db.scalar(select(ReadTxProjection).where(
                ReadTxProjection.ledger_id == led.id,
                ReadTxProjection.sync_id == out["sync_id"]))
        assert tx.amount == 3280.0
        assert tx.tax_amount == 298.0
        assert len(json.loads(tx.attachments_json)) == 1
    finally:
        app.dependency_overrides.clear()


def test_receipt_image_failure_does_not_lose_the_transaction(monkeypatch) -> None:
    """建交易成功但传图失败 → 必须报「已建 + 补传指引」,而不是整笔失败。

    否则 LLM 会以为没建成而**重复建账** —— 那才是真正的数据事故。
    """
    client, TS = _make_client()
    try:
        user = _setup(client, TS, monkeypatch, "att-fail@t.com")

        async def boom(*a, **k):
            raise RuntimeError("object storage unreachable")

        monkeypatch.setattr(write_tools, "_upload_attachment", boom)
        out = asyncio.run(write_tools.create_transaction_with_receipt(
            user, amount=100.0, category="餐饮",
            image_base64=base64.b64encode(_PNG).decode(),
        ))
        assert out["sync_id"], "交易必须已经建好"
        assert "attachment_error" in out
        assert "do NOT create the transaction again" in out["attachment_hint"]
    finally:
        app.dependency_overrides.clear()


def test_serialize_tx_includes_attachments(monkeypatch) -> None:
    """列表读也要带附件 —— 否则 LLM 判断不了「这笔有没有小票」。"""
    from src.mcp.tools.read_tools import _serialize_tx

    class FakeRow:
        sync_id = "tx1"
        tx_type = "expense"
        amount = 3280.0
        tax_amount = 298.0
        happened_at = datetime(2026, 10, 3, tzinfo=timezone.utc)
        note = None
        category_name = "餐饮"
        account_name = None
        from_account_name = None
        to_account_name = None
        tags_csv = ""
        currency_code = None
        native_amount = None
        attachments_json = json.dumps([
            {"fileName": "a_x.jpg", "cloudFileId": "a", "sortOrder": 0}
        ])

    out = _serialize_tx(FakeRow(), None)
    assert len(out["attachments"]) == 1
    assert out["attachments"][0]["cloudFileId"] == "a"

    FakeRow.attachments_json = "{broken"
    assert _serialize_tx(FakeRow(), None)["attachments"] == []

def test_attach_receipt_cannot_cross_users(monkeypatch) -> None:
    """**安全锁**:`attach_receipt` 按 `user_id` 过滤交易,别人的 sync_id 一律
    「not found」,绝不能把附件挂到不属于自己的交易上。

    顺带锁住一个**功能限制**:共享账本里 projection.user_id 是**账本所有者**,
    所以非所有者成员用 MCP 无法给共享账本的交易附图。对单用户自托管无影响
    (user.id == ledger.user_id),但值得写进测试免得以后误以为是 bug。"""
    client, TS = _make_client()
    try:
        owner = _setup(client, TS, monkeypatch, "att-owner@t.com")

        # 另起一个用户,复用同一批 token 补丁但用不同账号
        other_token = _register_and_token(
            client, "att-other@t.com", device_id="d-app2", client_type="app")
        with TS() as db:
            other = db.scalar(select(User).where(User.email == "att-other@t.com"))
            db.expunge(other)

        async def boom(*a, **k):
            raise AssertionError("不应发出 self-call —— 越权时必须在上传前就拒")

        monkeypatch.setattr(write_tools, "_upload_attachment", boom)
        with pytest.raises(ValueError, match="not found"):
            asyncio.run(write_tools.attach_receipt(
                other, sync_id="tx1",
                image_base64=base64.b64encode(_PNG).decode()))
        assert owner is not None
    finally:
        app.dependency_overrides.clear()
