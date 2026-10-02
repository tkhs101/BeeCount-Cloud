"""MCP write tools — 6 个,LLM 用来修改用户数据。

**实现策略**:write tools 通过 **HTTP self-call** 调现有的 `/api/v1/write/*`
router endpoint,而不是直接动 DB。原因:
  1. 复用所有 idempotency / sync_change 登记 / WebSocket 推送等已有逻辑
  2. 跟 web/mobile 走完全相同代码路径,行为一致,bug 修一处全部受益
  3. write router 内部是 snapshot mutator 模式,直接绕过会丢失关键逻辑

为了让 self-call 通过 auth,我们为当前 PAT user 临时签发一个**仅本进程内**
的短期 JWT(60 秒过期),作为 self-call 的 access token。这个 JWT 不出
进程,scope 严格限制为 SCOPE_APP_WRITE,client_type='app'。

危险操作(delete)需要二次确认 — LLM 调用时如果 confirm=False 返回
"待确认"状态,LLM 跟用户确认后带 confirm=True 调一次。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, NotRequired, TypedDict

from sqlalchemy import select

from ...config import get_settings
from ...database import SessionLocal
from ...models import (
    Ledger,
    ReadBudgetProjection,
    ReadTxProjection,
    User,
    UserAccountProjection,
    UserCategoryProjection,
    UserExchangeRateProjection,
    UserTagProjection,
)
from ...security import SCOPE_APP_WRITE, _create_token
from .read_tools import (
    _parse_dt,
    _resolve_ledger,
    _safe_attachments as _load_attachments,
    live_ledgers,
)

logger = logging.getLogger(__name__)

# self-call 内部 JWT — 仅当前进程当下用,60 秒有效。比给 PAT 永久 SCOPE_APP_
# WRITE 安全 — PAT 只有 mcp:* scope,不能走 web/app 路径;但 self-call 的
# 短期 JWT 模拟 'app' client,让 write router 收的就是普通 mobile 提交。
_SELF_TOKEN_TTL_SEC = 60

# MCP 记账自动打的标签 —— 跟 mobile AI 记账(zh `AI记账` / en `AI`)区分开,
# 用户事后能在标签筛选里一键看出"哪些是 LLM 客户端帮我记的"。
# 跟 LLM 调用时传的 tags 是**并集**关系 — LLM 传 ["coffee"] 最终落地为
# ["coffee", "MCP"]。LLM 也可以显式不要某个 tag,但 MCP 这个默认永远会带。
_MCP_DEFAULT_TAG = "MCP"
# MCP 标签的默认颜色(cyan)— 避开 AI 记账 #9C27B0(purple),用户标签管理
# 页一眼可分辨"哪些是 LLM 创建的"和"我自己手动建的 AI 记账标签"。
_MCP_DEFAULT_TAG_COLOR = "#00BCD4"


def _internal_token(user: User) -> str:
    return _create_token(
        sub=user.id,
        token_type="access",
        expires_delta=timedelta(seconds=_SELF_TOKEN_TTL_SEC),
        scopes=[SCOPE_APP_WRITE],
        client_type="app",
    )


async def _self_call(method: str, path: str, user: User, **kwargs: Any) -> dict[str, Any]:
    """异步 HTTP self-call 到本进程的 router endpoint。

    用 ASGI in-process transport 避免真起 socket — 仍然走完整 FastAPI
    dep tree + middleware,但不出 TCP。
    """
    from ..._mcp_internal_client import get_internal_client  # late import 防循环

    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {_internal_token(user)}"
    headers.setdefault("X-Device-ID", "mcp-internal")

    client = get_internal_client()
    resp = await client.request(method, path, headers=headers, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"self-call {method} {path} -> {resp.status_code} {resp.text[:300]}"
        )
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except Exception:
        return {"_raw": resp.text}


# ---------- 交易附件(F3 / #513)------------------------------------------------

# base64 解码前先按编码长度估算解码后大小:一个超大 base64 串如果先解码
# 再判断,会先吃满内存。4 个 base64 字符 → 3 字节,留 4% 余量给 padding。
_B64_DECODE_RATIO = 0.75
_B64_HEADROOM = 1.05

# 不猜 MIME 时按扩展名兜底。image/* 走这一档;其余按二进制。
_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".pdf": "application/pdf",
}


def _decode_base64_image(image_base64: str) -> tuple[bytes, str | None]:
    """base64 图片 → ``(raw_bytes, mime_from_prefix)``。

    同时接受裸 base64 和 ``data:image/jpeg;base64,`` 前缀(官方已上线的同名
    工具两种都收,保持一致)。**解码前**先按编码长度估算大小并比对上限 ——
    超限直接拒,不在内存里展开。

    上限取 `attachment_max_upload_bytes`(默认 64MB)再放宽 1%,因为
    attachment 端点判的是解码后大小,而 base64 本身膨胀 ~33%。
    """
    import base64
    import binascii

    text = (image_base64 or "").strip()
    mime: str | None = None
    if text.lower().startswith("data:"):
        header, _, payload = text.partition(",")
        if not _:
            raise ValueError("image_base64: malformed data URL")
        mime = header[len("data:"):].split(";")[0] or None
        text = payload.strip()
    if not text:
        raise ValueError("image_base64 is empty")

    max_bytes = int(get_settings().attachment_max_upload_bytes)
    estimated = len(text) * _B64_DECODE_RATIO / _B64_HEADROOM
    if estimated > max_bytes:
        raise ValueError(
            f"Attachment too large: ~{int(estimated)} bytes exceeds the "
            f"{max_bytes} byte limit"
        )
    try:
        raw = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"image_base64 is not valid base64: {exc}") from exc
    if not raw:
        raise ValueError("image_base64 decoded to zero bytes")
    # base64 长度只是估算,解码后再按真值判一次
    if len(raw) > max_bytes:
        raise ValueError(
            f"Attachment too large: {len(raw)} bytes exceeds the {max_bytes} byte limit"
        )
    return raw, mime


def _guess_mime(filename: str | None, fallback: str | None) -> str | None:
    if fallback:
        return fallback
    if not filename:
        return "image/jpeg"
    lower = filename.lower()
    for ext, mime in _EXT_MIME.items():
        if lower.endswith(ext):
            return mime
    return "application/octet-stream"


async def _upload_attachment(
    user: User, ledger_external_id: str, raw: bytes, filename: str, mime: str | None
) -> dict[str, Any]:
    """把字节流交给 `/attachments/upload`(multipart self-call)。

    返回 `AttachmentUploadOut`:`{file_id, sha256, size, mime_type, file_name, ...}`。
    sha256 去重由端点自己做(命中已有文件直接返回,不重复落盘)。
    """
    settings = get_settings()
    path = f"{settings.api_prefix}/attachments/upload"
    # multipart:`data` 走普通字段,`files` 走文件部分。别和 `json=` 同时给。
    return await _self_call(
        "POST", path, user,
        data={"ledger_id": ledger_external_id},
        files={"file": (filename, raw, mime or "application/octet-stream")},
    )



def _ext_for_mime(mime: str | None) -> str:
    """MIME → 扩展名(给默认文件名用)。认不出就给 .jpg(小票绝大多数是 jpeg)。"""
    if not mime:
        return "jpg"
    for ext, known in _EXT_MIME.items():
        if known == mime:
            return ext.lstrip(".")
    return "jpg"


def _build_attachment_ref(upload: dict[str, Any], sort_order: int) -> dict[str, Any]:
    """`AttachmentUploadOut` → 写进 `attachments_json` 的 `AttachmentRef`。

    字段名以 `frontend/packages/api-client/src/types.ts:126-135` 为准
    (camelCase);`fileName` 存的是 `<file_id>_<原名>` 拼接形式,见
    `TransactionsPage.tsx:1329-1333`。这些字段是 GC 的反向引用依据 ——
    `projection.py:768-788` 按 `cloudFileId` 查有没有交易引用它。
    """
    file_id = str(upload.get("file_id") or "")
    if not file_id:
        raise ValueError(f"Attachment upload returned no file_id: {upload}")
    original = str(upload.get("file_name") or "attachment")
    return {
        "fileName": f"{file_id}_{original}",
        "originalName": original,
        "fileSize": upload.get("size"),
        "sortOrder": sort_order,
        "cloudFileId": file_id,
        "cloudSha256": upload.get("sha256"),
    }


# ---------- ledger resolution for writes ------------------------------------


def _resolve_write_ledger(
    db, user: User, ledger_id: str | None
) -> tuple[Ledger | None, dict[str, Any] | None]:
    """为**写**操作解析目标账本。返回 ``(ledger, None)`` 表示成功;
    ``(None, status_dict)`` 表示调用方 / LLM 必须先澄清,**不应写入**。

    issue #31 两条规则:
      - 软删账本不可作为写入目标(`_resolve_ledger` 已排除,B1/B2);
      - 不指定 ``ledger_id`` 且有 **>1 个 live 账本**时**拒绝瞎猜**(B5):返回
        候选列表,逼 LLM 显式带 ``ledger_id`` —— 避免静默落到"最早创建"的幽灵
        默认账本(报告里"写进了不存在的默认账本"的根因)。

    返回的 status_dict 跟 `delete_transaction` 的 ``confirmation_required`` 一样,
    是给 LLM 看的结构化信号,不是错误。
    """
    if ledger_id:
        led = _resolve_ledger(db, user.id, ledger_id)
        if led is None:
            return None, {
                "status": "ledger_not_found",
                "message": f"Ledger not found or has been deleted: {ledger_id}",
                "ledger_id": ledger_id,
            }
        return led, None

    live = live_ledgers(db, user.id)
    if not live:
        return None, {
            "status": "no_ledger",
            "message": "You have no ledger yet. Create one in BeeCount first.",
        }
    if len(live) == 1:
        return live[0], None
    return None, {
        "status": "ledger_required",
        "message": (
            "You have multiple ledgers — refusing to guess which one to write to. "
            "Re-call this tool with an explicit `ledger_id` (the `id` field of one "
            "of the candidates below)."
        ),
        "candidates": [{"id": led.external_id, "name": led.name} for led in live],
    }


# ---------- tools -----------------------------------------------------------


async def create_transaction(
    user: User,
    *,
    amount: float,
    tx_type: str = "expense",
    category: str | None = None,
    account: str | None = None,
    happened_at: str | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
    ledger_id: str | None = None,
    currency: str | None = None,
    tax_amount: float | None = None,
) -> dict[str, Any]:
    """新建一笔交易。category / account 用名字。happened_at 不传 = 当前时间。

    currency(v30 多币种):记外币时传 ISO code(如 USD/JPY)。不传则:有账户
    随账户币种、无账户随账本主币种。外币会按当前汇率折算到账本主币种。

    tax_amount(0020,消费税):**照抄小票上「消費税等」那一行的绝对值**,
    不要按税率倒算 —— 日本各家舍入方式不同(合計 1780 按 8% 反推是
    1648.15,收银机显示的是 1649,差 1 円)。不传 = 无税。仅 expense 有效。
    amount 仍是**实付总额**,税额只是叠加维度:统计时从分类里剥出归入
    「税与保险」,两块相加仍等于实付。
    例:合計 3280 / 消費税 298 → amount=3280, tax_amount=298。"""
    if tx_type not in {"expense", "income", "transfer"}:
        raise ValueError(f"Invalid tx_type: {tx_type}")
    if amount <= 0:
        raise ValueError("amount must be positive")

    with SessionLocal() as db:
        led, ledger_status = _resolve_write_ledger(db, user, ledger_id)
        if ledger_status is not None:
            # 多账本未指定 / 账本不存在或已删 —— 交回 LLM 澄清,不写入。
            return ledger_status
        assert led is not None  # 契约:_resolve_write_ledger 的 status 为 None ⟺ led 命中
        if category:
            _lookup_category_sync_id(db, user.id, category, tx_type)
        if account:
            _lookup_account_sync_id(db, user.id, account)
        ledger_external_id = led.external_id
        ledger_name = led.name
        led_internal_id = led.id  # 出 with 块后 led 会 detach,提前取值
        ledger_base_ccy = (led.currency or "CNY").strip().upper()  # v30 折算基准
        acc_ccy = _account_currency(db, user.id, account) if account else None
        mcp_tag_missing = _is_tag_missing_in_ledger(
            db, user_id=user.id, ledger_id=led_internal_id, tag_name=_MCP_DEFAULT_TAG,
        )

    # 标签管理页是从 UserTagProjection 读的 —— 只往 tx.tags_csv 写 "MCP" 不够,
    # 必须额外建一个独立 tag 实体行,Tags 页 / mobile / 同步才能识别。
    # 幂等:如果已存在就跳过。
    if mcp_tag_missing:
        await _ensure_mcp_tag(user, ledger_external_id)

    happened = _parse_dt(happened_at) if happened_at else datetime.now(timezone.utc)
    body: dict[str, Any] = {
        "base_change_id": 0,
        "tx_type": tx_type,
        "amount": float(amount),
        "happened_at": happened.isoformat(),
    }
    if note:
        body["note"] = note
    if category:
        body["category_name"] = category
        body["category_kind"] = tx_type
    if account:
        if tx_type == "transfer":
            body["from_account_name"] = account
        else:
            body["account_name"] = account
    # 消费税(0020):有值才发字段。硬校验在 server(snapshot_mutator),这里
    # 只挡明显非法的负数,好让 LLM 拿到可读的报错而不是 500。
    if tax_amount is not None:
        if tax_amount < 0:
            raise ValueError("tax_amount must be positive")
        if tax_amount >= amount:
            raise ValueError("tax_amount must be less than amount")
        if tx_type != "expense":
            raise ValueError("tax_amount is only allowed on expense transactions")
        body["tax_amount"] = float(tax_amount)
    # 始终注入 MCP 默认标签;跟 LLM 传的 tags 并集去重,顺序保持 LLM 给的在前
    final_tags = _merge_default_tag(tags)
    body["tags"] = final_tags
    # 同时把对应 sync_id 也喂给 server —— 两个字段一起填,Tags 详情弹窗
    # (走 tag_sync_ids_json 精确过滤)才能找到这笔 tx。
    with SessionLocal() as db:
        tag_ids = _lookup_tag_sync_ids(
            db, user_id=user.id, ledger_id=led_internal_id, names=final_tags,
        )
    if tag_ids:
        body["tag_ids"] = tag_ids

    # v30 多币种:非转账才折算(转账币种恒=账户币种,本阶段不支持跨币种转账)
    if tx_type != "transfer":
        body.update(await _build_currency_fields(
            user, ledger_base=ledger_base_ccy, account_currency=acc_ccy,
            currency_arg=currency, amount=float(amount),
        ))

    settings = get_settings()
    path = f"{settings.api_prefix}/write/ledgers/{ledger_external_id}/transactions"
    result = await _self_call("POST", path, user, json=body)
    return {
        "sync_id": result.get("entity_id"),
        "ledger": ledger_name,
        "tx_type": tx_type,
        "amount": amount,
        "happened_at": happened.isoformat(),
        "category": category,
        "account": account,
        "_meta": result,
    }


async def attach_receipt(
    user: User,
    *,
    sync_id: str,
    image_base64: str,
    file_name: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """给**已有**交易附上一张小票图片(不改金额/分类)。

    image_base64:裸 base64 或 `data:image/jpeg;base64,` 前缀都行。
    上限受 server 的 `attachment_max_upload_bytes` 约束(默认 64MB);
    超限在解码前就会被拒,不会先把超大串展开进内存。

    同一张图重复传不会占两份空间 —— server 按 sha256 去重。
    新图追加到该笔已有附件之后(sortOrder 递增)。

    **不存在的 sync_id 会报错,不会新建交易** —— 附加不是创建,两者语义不同。"""
    raw, prefix_mime = _decode_base64_image(image_base64)
    return await _attach_receipt_bytes(
        user,
        sync_id=sync_id,
        raw=raw,
        mime=_guess_mime(file_name, mime_type or prefix_mime),
        file_name=file_name,
    )


async def _assert_can_write_ledger(user: User, ledger_external_id: str) -> None:
    """上传前确认用户对该账本有写权限(Owner / Editor)。

    共享账本里 projection.user_id 是**账本所有者**,所以只看 `user_id ==
    tx.user_id` 挡不住 Viewer 成员 —— 他们能查到这笔交易,却无权 PATCH。
    """
    from ...ledger_access import WRITABLE_ROLES, get_accessible_ledger_by_external_id

    with SessionLocal() as db:
        row = get_accessible_ledger_by_external_id(
            db, user_id=user.id, ledger_external_id=ledger_external_id
        )
    if row is None:
        raise ValueError(f"Ledger not found: {ledger_external_id}")
    ledger, role = row
    if role not in WRITABLE_ROLES:
        raise PermissionError(
            f"Only ledger owners/editors can attach files (your role: {role!r})"
        )


async def _attach_receipt_bytes(
    user: User,
    *,
    sync_id: str,
    raw: bytes,
    mime: str | None,
    file_name: str | None,
) -> dict[str, Any]:
    """`attach_receipt` 的字节版 —— base64 只解一次。

    `create_transaction_with_receipt` 也需要走这条路:它先把图解出来做大小
    预检,如果再把**原始 base64 串**传给 `attach_receipt`,就会被解第二遍 ——
    峰值内存 ≈ 2× 解码后字节,恰好抵消掉 `_decode_base64_image`「解码前预估
    大小」的一半意义。
    """
    with SessionLocal() as db:
        existing = db.scalar(
            select(ReadTxProjection).where(
                ReadTxProjection.user_id == user.id,
                ReadTxProjection.sync_id == sync_id,
            )
        )
        if existing is None:
            raise ValueError(f"Transaction not found: {sync_id}")
        led = db.scalar(select(Ledger).where(Ledger.id == existing.ledger_id))
        if led is None:
            raise ValueError("Ledger missing for this tx")
        ledger_external_id = led.external_id
        current = _load_attachments(existing.attachments_json)

    # **上传之前**先验写权限。
    #
    # 附件没有删除端点,而孤儿 GC 只在删交易 / 删附件时触发 —— 所以如果
    # 「先上传后 PATCH」里的 PATCH 因权限失败,那个 blob 和 AttachmentFile
    # 行就成了删不掉的孤儿。这里把可预见的鉴权失败提前到上传之前。
    # (真正的写权限检查在 write endpoint 的
    # `_prepare_write(required_roles={OWNER, EDITOR})`,这里用同一份
    # WRITABLE_ROLES 常量,避免两套口径。)
    await _assert_can_write_ledger(user, ledger_external_id)

    name = file_name or f"receipt-{sync_id}.{_ext_for_mime(mime)}"
    upload = await _upload_attachment(
        user, ledger_external_id, raw, name, mime,
    )
    ref = _build_attachment_ref(upload, sort_order=len(current))
    merged = [*current, ref]

    settings = get_settings()
    path = (
        f"{settings.api_prefix}/write/ledgers/{ledger_external_id}"
        f"/transactions/{sync_id}"
    )
    await _self_call("PATCH", path, user, json={"base_change_id": 0, "attachments": merged})
    return {
        "sync_id": sync_id,
        "attachment": ref,
        "attachment_count": len(merged),
        # 不回传整个 upload 响应:对 LLM 是噪音(它要的是 file_id / sha256,
        # 上面 attachment 里已经给了)。存储路径这类内部字段更不该进上下文。
    }


async def create_transaction_with_receipt(
    user: User,
    *,
    amount: float,
    image_base64: str,
    tx_type: str = "expense",
    category: str | None = None,
    account: str | None = None,
    happened_at: str | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
    ledger_id: str | None = None,
    currency: str | None = None,
    tax_amount: float | None = None,
    file_name: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """**建交易 + 附小票图**一步到位 —— 拍完小票记一笔的推荐入口。

    等价于 create_transaction 再 attach_receipt,但少了中间的「已建未附」
    状态,也少一次 LLM 往返。

    参数与 create_transaction 完全一致,另加 image_base64(裸 base64 或
    `data:image/jpeg;base64,` 前缀)。tax_amount 语义相同:消费税绝对值,
    照抄小票不要按税率倒算。

    如果建交易成功但传图失败,会**明确报错并给出已建的 sync_id** —— 图丢了
    但账没丢,补传用 attach_receipt 即可,不要重复建交易。"""
    # 只解一次;下面 attach 阶段复用同一份 bytes
    raw, prefix_mime = _decode_base64_image(image_base64)
    mime = _guess_mime(file_name, mime_type or prefix_mime)

    created = await create_transaction(
        user,
        amount=amount,
        tx_type=tx_type,
        category=category,
        account=account,
        happened_at=happened_at,
        note=note,
        tags=tags,
        ledger_id=ledger_id,
        currency=currency,
        tax_amount=tax_amount,
    )
    sync_id = created.get("sync_id")
    if not sync_id:
        raise ValueError(f"Transaction created but no sync_id returned: {created}")

    name = file_name or f"receipt-{sync_id}.{_ext_for_mime(mime)}"
    try:
        attached = await _attach_receipt_bytes(
            user,
            sync_id=str(sync_id),
            raw=raw,
            mime=mime,
            file_name=name,
        )
    except Exception as exc:
        # 账已经落库,图失败不能报成「整笔失败」—— 那会诱导 LLM 重复建账。
        return {
            **created,
            "attachment_error": str(exc),
            "attachment_hint": (
                f"Transaction {sync_id} WAS created; only the image upload failed. "
                f"Retry the image with attach_receipt(sync_id={sync_id!r}, ...) — "
                f"do NOT create the transaction again."
            ),
        }
    return {**created, "attachment": attached.get("attachment")}


async def update_transaction(
    user: User,
    *,
    sync_id: str,
    amount: float | None = None,
    tx_type: str | None = None,
    category: str | None = None,
    account: str | None = None,
    happened_at: str | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
    tax_amount: float | None = None,
) -> dict[str, Any]:
    """更新现有交易。只更新传入的字段。

    tax_amount(0020,消费税):省略 = 不变;**传 0 = 清除这笔的税额**(PATCH 的
    显式 null 在 MCP 层没法与「没传」区分,所以用 0 当清除信号);正数 = 覆盖。
    照抄小票绝对值,不要按税率倒算(各家舍入不同,倒推会对不上收银机)。
    """
    with SessionLocal() as db:
        existing = db.scalar(
            select(ReadTxProjection).where(
                ReadTxProjection.user_id == user.id,
                ReadTxProjection.sync_id == sync_id,
            )
        )
        if existing is None:
            raise ValueError(f"Transaction not found: {sync_id}")
        led = db.scalar(select(Ledger).where(Ledger.id == existing.ledger_id))
        if led is None:
            raise ValueError("Ledger missing for this tx")
        ledger_external_id = led.external_id
        effective_tx_type = tx_type or existing.tx_type
        if category:
            _lookup_category_sync_id(db, user.id, category, effective_tx_type)
        if account:
            _lookup_account_sync_id(db, user.id, account)

    patch: dict[str, Any] = {"base_change_id": 0}
    if amount is not None:
        if amount <= 0:
            raise ValueError("amount must be positive")
        patch["amount"] = float(amount)
    if tx_type is not None:
        if tx_type not in {"expense", "income", "transfer"}:
            raise ValueError(f"Invalid tx_type: {tx_type}")
        patch["tx_type"] = tx_type
    # 消费税(0020):0 当「清除」信号(PATCH 的显式 null 与「没传」在 MCP 层
    # 无法区分);正数覆盖。amount 取本次生效值做 < amount 校验。
    if tax_amount is not None:
        if tax_amount < 0:
            raise ValueError("tax_amount must be positive")
        if tax_amount == 0:
            patch["tax_amount"] = None
        else:
            effective_amount = float(amount) if amount is not None else float(existing.amount)
            if effective_amount <= 0 or tax_amount >= effective_amount:
                raise ValueError("tax_amount must be less than amount")
            if effective_tx_type != "expense":
                raise ValueError("tax_amount is only allowed on expense transactions")
            patch["tax_amount"] = float(tax_amount)
    if happened_at is not None:
        patch["happened_at"] = _parse_dt(happened_at).isoformat()
    if note is not None:
        patch["note"] = note
    if category is not None:
        patch["category_name"] = category
        patch["category_kind"] = effective_tx_type
    if account is not None:
        if effective_tx_type == "transfer":
            patch["from_account_name"] = account
        else:
            patch["account_name"] = account
    if tags is not None:
        patch["tags"] = list(tags)

    settings = get_settings()
    path = f"{settings.api_prefix}/write/ledgers/{ledger_external_id}/transactions/{sync_id}"
    result = await _self_call("PATCH", path, user, json=patch)
    return {
        "sync_id": sync_id,
        "updated": [k for k in patch.keys() if k != "base_change_id"],
        "_meta": result,
    }


async def delete_transaction(
    user: User,
    *,
    sync_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """删除一笔交易。**危险操作** — confirm=False 时返"待确认"状态,LLM 必须
    跟用户确认后带 confirm=True 再调一次。
    """
    if not confirm:
        return {
            "status": "confirmation_required",
            "message": (
                "Delete transaction requires explicit confirmation. "
                "Please confirm with the user, then call again with confirm=true."
            ),
            "sync_id": sync_id,
        }

    with SessionLocal() as db:
        existing = db.scalar(
            select(ReadTxProjection).where(
                ReadTxProjection.user_id == user.id,
                ReadTxProjection.sync_id == sync_id,
            )
        )
        if existing is None:
            raise ValueError(f"Transaction not found: {sync_id}")
        led = db.scalar(select(Ledger).where(Ledger.id == existing.ledger_id))
        if led is None:
            raise ValueError("Ledger missing for this tx")
        ledger_external_id = led.external_id

    settings = get_settings()
    path = f"{settings.api_prefix}/write/ledgers/{ledger_external_id}/transactions/{sync_id}"
    await _self_call("DELETE", path, user, json={"base_change_id": 0})
    return {"status": "deleted", "sync_id": sync_id}


async def create_category(
    user: User,
    *,
    name: str,
    kind: str = "expense",
    parent_name: str | None = None,
    icon: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """新建一个分类(罕见 — LLM 一般用现有分类)。"""
    if kind not in {"expense", "income", "transfer"}:
        raise ValueError(f"Invalid kind: {kind}")

    with SessionLocal() as db:
        led = _resolve_ledger(db, user.id, ledger_id)
        if led is None:
            raise ValueError("No ledger found")
        ledger_external_id = led.external_id

    body: dict[str, Any] = {
        "base_change_id": 0,
        "name": name,
        "kind": kind,
        "level": 2 if parent_name else 1,
    }
    if parent_name:
        body["parent_name"] = parent_name
    if icon:
        body["icon"] = icon

    settings = get_settings()
    path = f"{settings.api_prefix}/write/ledgers/{ledger_external_id}/categories"
    result = await _self_call("POST", path, user, json=body)
    return {
        "sync_id": result.get("entity_id"),
        "name": name,
        "kind": kind,
        "_meta": result,
    }


async def create_budget(
    user: User,
    *,
    amount: float,
    budget_type: str = "total",
    category: str | None = None,
    period: str = "monthly",
    enabled: bool = True,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """新建预算。之前 MCP 只有 update_budget,新建预算必须去 Web —— 而记账
    走 MCP 时这是个很别扭的断点。

    budget_type: 'total'(总预算,不分类)或 'category'(分类预算)。
    category: 分类预算必填 —— 用**分类名**(与 create_transaction 的 category
        同口径),服务端反查 sync_id;分类不存在会明确报错。
    period: 'monthly'(默认)/ 'weekly' / 'yearly'。**实际周期跟随账本的
        month_start_day**(D5 之后 start_day 已废弃),传 period 只是标注。
    """
    if budget_type not in {"total", "category"}:
        raise ValueError(f"Invalid budget_type: {budget_type}")
    if period not in {"monthly", "weekly", "yearly"}:
        raise ValueError(f"Invalid period: {period}")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if budget_type == "category" and not category:
        raise ValueError("category is required when budget_type is 'category'")

    with SessionLocal() as db:
        led, ledger_status = _resolve_write_ledger(db, user, ledger_id)
        if ledger_status is not None:
            return ledger_status
        assert led is not None
        ledger_external_id = led.external_id
        ledger_name = led.name
        category_sync_id = (
            _lookup_category_sync_id(db, user.id, category, "expense")
            if budget_type == "category"
            else None
        )

    body: dict[str, Any] = {
        "base_change_id": 0,
        "type": budget_type,
        "amount": float(amount),
        "period": period,
        "enabled": bool(enabled),
    }
    if category_sync_id:
        body["category_id"] = category_sync_id

    settings = get_settings()
    path = f"{settings.api_prefix}/write/ledgers/{ledger_external_id}/budgets"
    result = await _self_call("POST", path, user, json=body)
    return {
        "sync_id": result.get("entity_id"),
        "ledger": ledger_name,
        "budget_type": budget_type,
        "category": category,
        "amount": amount,
        "period": period,
    }


async def update_budget(
    user: User,
    *,
    budget_id: str,
    amount: float,
) -> dict[str, Any]:
    """更新预算金额。"""
    if amount <= 0:
        raise ValueError("amount must be positive")

    with SessionLocal() as db:
        existing = db.scalar(
            select(ReadBudgetProjection).where(
                ReadBudgetProjection.user_id == user.id,
                ReadBudgetProjection.sync_id == budget_id,
            )
        )
        if existing is None:
            raise ValueError(f"Budget not found: {budget_id}")
        led = db.scalar(select(Ledger).where(Ledger.id == existing.ledger_id))
        if led is None:
            raise ValueError("Ledger missing for this budget")
        ledger_external_id = led.external_id

    settings = get_settings()
    path = f"{settings.api_prefix}/write/ledgers/{ledger_external_id}/budgets/{budget_id}"
    result = await _self_call(
        "PATCH",
        path,
        user,
        json={"base_change_id": 0, "amount": float(amount)},
    )
    return {"sync_id": budget_id, "amount": amount, "_meta": result}


# 一次 self-call /transactions/batch 最多塞多少笔(端点自身上限 50)。
_BATCH_CHUNK = 50
# 单次 create_transactions 调用的总上限 —— 防 LLM 一次塞几千笔把单个 tool call
# 拖死;超过让它分多次调。
_BULK_MAX_TOTAL = 200


class BatchTxItem(TypedDict):
    """create_transactions 单条 item 的声明类型。

    **为什么必须是 TypedDict 而不是 `list[dict[str, Any]]`**:
    泛型 dict 生成的 JSON Schema 是 `{"type":"object","additionalProperties":true}`
    —— 没有 properties、没有 required、amount 无类型约束。LLM 看到这种 schema
    会照着账单/Excel 的字面值传字符串(`{"amount":"38.00"}`),而 `Any` 不做任何
    强转,原始 str 一路带到下面 `isinstance(amount,(int,float))` 被拒,报出
    `transactions[0]: amount must be a positive number` —— 与真实原因无关的
    误导性错误。单条 create_transaction 不受影响,只因它的 `amount` 声明为
    `float`,pydantic 会强转。**两条路径的唯一差别就是这里的类型声明。**

    TypedDict 同时满足三点:schema 带 `"amount":{"type":"number"}` +
    `"required":["amount"]`(给 LLM 明确指引)、`"38.00"` 被强转成 38.0、
    validate 后仍是 plain dict(下面 normalize 循环的 `raw.get()` 一行不用改)。
    换成 BaseModel 会让 item 变成模型实例,`raw.get()` 全部炸。

    字段名沿用下面循环体已经在读的键,新增字段时两边同步。
    """

    amount: float
    tx_type: NotRequired[str]
    category: NotRequired[str]
    account: NotRequired[str]
    happened_at: NotRequired[str]
    note: NotRequired[str]
    tags: NotRequired[list[str]]
    currency: NotRequired[str]
    tax_amount: NotRequired[float]


async def create_transactions(
    user: User,
    *,
    transactions: list[BatchTxItem],
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """批量新建交易(Excel / 对账单导入等)。

    比循环调 `create_transaction` 高效得多:走 server 的 `/transactions/batch`
    端点,每 ≤50 笔**一次 commit + 一次 WS 广播**,避免 N 次全量 snapshot 重建
    (issue #31 A3 —— 报告里"批量 MCP 写入服务端短时无响应 / 部分失败"的正解)。

    每个 item 字段(跟 create_transaction 一致):
      amount(必填 >0)、tx_type(expense|income|transfer,默认 expense)、
      category、account、happened_at(ISO,缺省=now)、note、tags(list)。
    """
    if not transactions:
        raise ValueError("transactions must be a non-empty list")
    if len(transactions) > _BULK_MAX_TOTAL:
        raise ValueError(
            f"Too many transactions in one call ({len(transactions)} > "
            f"{_BULK_MAX_TOTAL}). Split into multiple calls."
        )

    # 1. 解析 + 校验目标账本(B5:多账本不瞎猜)
    with SessionLocal() as db:
        led, ledger_status = _resolve_write_ledger(db, user, ledger_id)
        if ledger_status is not None:
            return ledger_status
        assert led is not None  # 契约:_resolve_write_ledger 的 status 为 None ⟺ led 命中
        ledger_external_id = led.external_id
        ledger_name = led.name
        led_internal_id = led.id
        batch_ledger_base = (led.currency or "CNY").strip().upper()  # v30 折算基准

    # 2. 规范化每笔 + 基础校验;收集要校验的 category / account 名
    norm_items: list[dict[str, Any]] = []
    cat_needed: set[str] = set()
    acc_needed: set[str] = set()
    for i, raw in enumerate(transactions):
        amount = raw.get("amount")
        tx_type = raw.get("tx_type") or "expense"
        if tx_type not in {"expense", "income", "transfer"}:
            raise ValueError(f"transactions[{i}]: invalid tx_type {tx_type!r}")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
            raise ValueError(f"transactions[{i}]: amount must be a positive number")
        happened_at = raw.get("happened_at")
        happened = _parse_dt(happened_at) if happened_at else datetime.now(timezone.utc)
        item: dict[str, Any] = {
            "tx_type": tx_type,
            "amount": float(amount),
            "happened_at": happened.isoformat(),
        }
        if raw.get("note"):
            item["note"] = str(raw["note"])
        category = raw.get("category")
        if category:
            item["category_name"] = str(category)
            item["category_kind"] = tx_type
            cat_needed.add(str(category))
        account = raw.get("account")
        if account:
            if tx_type == "transfer":
                item["from_account_name"] = str(account)
            else:
                item["account_name"] = str(account)
            acc_needed.add(str(account))
        # 用户 tags ∪ MCP 默认标签;batch 端点按名建实体 / 反查 sync_id。
        item["tags"] = _merge_default_tag(raw.get("tags"))
        # v30 多币种:暂存本笔的显式币种 + 账户名,第 3.5 步统一折算
        item["__ccy_arg"] = (str(raw["currency"]).strip().upper()
                             if raw.get("currency") else None)
        item["__acc_name"] = str(account) if account else None
        # 消费税(0020):照抄小票绝对值。校验口径跟单条一致,给 LLM 可读报错。
        tax = raw.get("tax_amount")
        if tax is not None:
            if tax < 0:
                raise ValueError(f"transactions[{i}]: tax_amount must be positive")
            if tax >= float(amount):
                raise ValueError(f"transactions[{i}]: tax_amount must be less than amount")
            if tx_type != "expense":
                raise ValueError(
                    f"transactions[{i}]: tax_amount is only allowed on expense transactions"
                )
            item["tax_amount"] = float(tax)
        norm_items.append(item)

    # 3. 预校验 category / account 名是否存在(O(1) 查询,给 LLM 清晰报错,
    #    跟单笔 create_transaction 的 _lookup_* 校验同口径)
    with SessionLocal() as db:
        _validate_names_exist(db, user.id, categories=cat_needed, accounts=acc_needed)
        mcp_tag_missing = _is_tag_missing_in_ledger(
            db, user_id=user.id, ledger_id=led_internal_id, tag_name=_MCP_DEFAULT_TAG,
        )

    # 3.5 v30 多币种折算:预取涉及账户的币种 map,逐笔定币种 + 折 native。
    #     account_currency 走 map(不逐笔查库);_build_currency_fields 内部
    #     只在外币时才拉汇率(fetcher 有 server 端缓存,同 base 复用)。
    if acc_needed:
        with SessionLocal() as db:
            acc_ccy_map = {
                name: (
                    db.scalar(
                        select(UserAccountProjection.currency).where(
                            UserAccountProjection.user_id == user.id,
                            UserAccountProjection.name == name,
                        ).limit(1)
                    ) or ""
                ).strip().upper() or None
                for name in acc_needed
            }
    else:
        acc_ccy_map = {}
    for item in norm_items:
        ccy_arg = item.pop("__ccy_arg", None)
        acc_name = item.pop("__acc_name", None)
        if item["tx_type"] == "transfer":
            continue  # 转账不折算(同币种守卫)
        fields = await _build_currency_fields(
            user,
            ledger_base=batch_ledger_base,
            account_currency=acc_ccy_map.get(acc_name),
            currency_arg=ccy_arg,
            amount=item["amount"],
        )
        item.update(fields)

    # 4. 确保 MCP tag 实体存在(带专属颜色),batch 端点随后复用同名 tag。
    if mcp_tag_missing:
        await _ensure_mcp_tag(user, ledger_external_id)

    # 5. 分块 self-call /transactions/batch
    settings = get_settings()
    path = f"{settings.api_prefix}/write/ledgers/{ledger_external_id}/transactions/batch"
    created_ids: list[str] = []
    for start in range(0, len(norm_items), _BATCH_CHUNK):
        chunk = norm_items[start : start + _BATCH_CHUNK]
        result = await _self_call(
            "POST",
            path,
            user,
            json={
                "base_change_id": 0,
                "transactions": chunk,
                "auto_ai_tag": False,  # MCP 用自己的 MCP 标签,不要"AI 记账"标签
            },
        )
        created_ids.extend(result.get("created_sync_ids") or [])

    return {
        "status": "created",
        "ledger": ledger_name,
        "created_count": len(created_ids),
        "sync_ids": created_ids,
    }


async def parse_and_create_from_text(
    user: User,
    *,
    text: str,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """让 BeeCount AI 自己解析自然语言并创建交易。

    LLM 偷懒选项 — 直接转发用户原话 → BeeCount AI parse → 自动 create。
    要求用户已配 AI chat provider(profile.ai_config_json),否则报错。
    """
    # B5(issue #31):先把目标账本定死,多账本不指定则不猜(返回候选),也避免
    # 拿一个软删 / 幽灵账本去跑 AI 解析。pin 到 external_id 后贯穿 parse + create。
    with SessionLocal() as db:
        led, ledger_status = _resolve_write_ledger(db, user, ledger_id)
        if ledger_status is not None:
            return ledger_status
        assert led is not None  # 契约:_resolve_write_ledger 的 status 为 None ⟺ led 命中
        ledger_id = led.external_id

    settings = get_settings()
    path = f"{settings.api_prefix}/ai/parse-tx-text"
    parsed = await _self_call(
        "POST",
        path,
        user,
        json={"text": text, "ledger_id": ledger_id, "locale": "zh"},
    )

    drafts = parsed.get("tx_drafts") or []
    if not drafts:
        return {
            "status": "parse_failed",
            "message": "AI did not extract any draft",
            "parsed": parsed,
        }
    draft = drafts[0]

    amount = draft.get("amount")
    if not isinstance(amount, (int, float)) or amount == 0:
        return {
            "status": "parse_failed",
            "message": "No valid amount in draft",
            "parsed": parsed,
        }
    tx_type = draft.get("tx_type") or "expense"
    category = draft.get("category_name")
    account = draft.get("account_name")
    happened_at = draft.get("happened_at")
    note = draft.get("note") or text
    # 多币种(.docs/multi-currency-ai A9):draft 的 currency 已被 server 端
    # _norm_currency 校验成 ISO 码或 "";空串要传 None 才会走「随账户/本位币」。
    currency = (draft.get("currency") or "").strip().upper() or None

    created = await create_transaction(
        user,
        amount=abs(float(amount)),
        tx_type=tx_type,
        category=category,
        account=account,
        happened_at=happened_at,
        note=note,
        ledger_id=ledger_id,
        currency=currency,
    )
    return {"status": "created", "parsed": draft, "transaction": created}


# ---------- internal helpers ------------------------------------------------


def _is_tag_missing_in_ledger(
    db, *, user_id: str, ledger_id: str, tag_name: str
) -> bool:
    """检查 UserTagProjection 里这个 user 是否已有同名 tag(tag 是 user-global)。"""
    del ledger_id  # tag 是 user-global,不再按 ledger 过滤
    existing = db.scalar(
        select(UserTagProjection).where(
            UserTagProjection.user_id == user_id,
            UserTagProjection.name == tag_name,
        )
    )
    return existing is None


def _lookup_tag_sync_ids(
    db, *, user_id: str, ledger_id: str, names: list[str]
) -> list[str]:
    """把 tag 名字解析成 sync_id(同 ledger)。没找到的名字直接丢弃。

    write router 接收 `tags` (CSV name) 时只填 tx.tags_csv,**不会**自动反查
    sync_id 填 tx.tag_sync_ids_json。导致 Tags 详情弹窗(用 tag_sync_ids_json
    精确过滤)看不到通过 name 创建的 tx。MCP 这里显式查一次 sync_id 一起传,
    两边索引都喂饱。
    """
    if not names:
        return []
    del ledger_id  # tag 是 user-global,跨账本统一
    rows = db.execute(
        select(UserTagProjection.name, UserTagProjection.sync_id).where(
            UserTagProjection.user_id == user_id,
            UserTagProjection.name.in_(names),
        )
    ).all()
    # 同一 name 同 ledger 应当唯一,但稳妥起见去重
    by_name: dict[str, str] = {}
    for n, sid in rows:
        by_name.setdefault(n, sid)
    # 保持 names 的输入顺序
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        sid = by_name.get(n)
        if sid and sid not in seen:
            out.append(sid)
            seen.add(sid)
    return out


async def _ensure_mcp_tag(user: User, ledger_external_id: str) -> None:
    """通过 write router self-call 建一个 MCP tag 实体。失败不阻塞主流程
    (例如 race condition 两个 tool call 同时建,第二个会拿 conflict,忽略即可
    —— tag 反正存在了)。
    """
    body = {
        "base_change_id": 0,
        "name": _MCP_DEFAULT_TAG,
        "color": _MCP_DEFAULT_TAG_COLOR,
    }
    settings = get_settings()
    path = f"{settings.api_prefix}/write/ledgers/{ledger_external_id}/tags"
    try:
        await _self_call("POST", path, user, json=body)
    except RuntimeError as exc:
        # 重复名 / race 会返 409 / 4xx;tag 既然存在或被并发创建了就 OK
        logger.info("mcp: ensure tag fallthrough — %s", exc)


def _merge_default_tag(tags: list[str] | None) -> list[str]:
    """把 `_MCP_DEFAULT_TAG` 并入用户给的 tags,去重保序(LLM 给的在前)。"""
    seen: dict[str, None] = {}
    if tags:
        for t in tags:
            v = (t or "").strip()
            if v:
                seen.setdefault(v, None)
    seen.setdefault(_MCP_DEFAULT_TAG, None)
    return list(seen.keys())


# ---------- internal lookups ------------------------------------------------


def _validate_names_exist(
    db, user_id: str, *, categories: set[str], accounts: set[str]
) -> None:
    """批量校验 category / account 名都存在(各一条 IN 查询),不存在的一次性报全。

    给 create_transactions 用 —— 单笔 create 走 _lookup_* 逐个校验,批量则一次
    查清,避免 N 次查询,且能在一条错误里列出所有未知名字。
    """
    if categories:
        found = {
            n
            for (n,) in db.execute(
                select(UserCategoryProjection.name).where(
                    UserCategoryProjection.user_id == user_id,
                    UserCategoryProjection.name.in_(categories),
                )
            ).all()
        }
        missing = sorted(categories - found)
        if missing:
            raise ValueError(
                f"Unknown categories: {missing}. Use existing category names "
                "(call list_categories) or create them first."
            )
    if accounts:
        found = {
            n
            for (n,) in db.execute(
                select(UserAccountProjection.name).where(
                    UserAccountProjection.user_id == user_id,
                    UserAccountProjection.name.in_(accounts),
                )
            ).all()
        }
        missing = sorted(accounts - found)
        if missing:
            raise ValueError(
                f"Unknown accounts: {missing}. Use existing account names "
                "(call list_accounts)."
            )


def _lookup_category_sync_id(db, user_id: str, name: str | None, tx_type: str | None) -> str | None:
    if not name:
        return None
    query = select(UserCategoryProjection).where(
        UserCategoryProjection.user_id == user_id,
        UserCategoryProjection.name == name,
    )
    if tx_type and tx_type in {"expense", "income", "transfer"}:
        query = query.where(UserCategoryProjection.kind == tx_type)
    row = db.scalar(query.limit(1))
    if row is None:
        raise ValueError(f"Category not found: {name}")
    return row.sync_id


def _account_currency(db, user_id: str, name: str | None) -> str | None:
    """账户名 → 币种(大写)。查不到返回 None。"""
    if not name:
        return None
    row = db.scalar(
        select(UserAccountProjection.currency)
        .where(
            UserAccountProjection.user_id == user_id,
            UserAccountProjection.name == name,
        )
        .limit(1)
    )
    return (row or "").strip().upper() or None


async def _build_currency_fields(
    user: User,
    *,
    ledger_base: str,
    account_currency: str | None,
    currency_arg: str | None,
    amount: float,
) -> dict[str, Any]:
    """v30 交易级多币种:MCP 记账时定交易币种 + 折账本本位币快照。

    币种优先级:显式 currency 参数 > 账户币种 > 账本本位币(与 App 一致)。
    折算方向:手动 override(1 quote = rate base,乘) > 自动源 fetcher
    (1 base = x quote,除),缺汇率退化 =amount(1:1,currency_code 仍落,
    Web 改主币种重算 / App L11 横幅可捞回)。返回要并进 body 的字段 dict。
    """
    base = ledger_base.strip().upper()
    cc = (currency_arg or account_currency or base).strip().upper()
    if cc == base:
        # 本位币:body 不带两字段(server 落 NULL,统计 COALESCE 回退 amount)
        return {}
    # override 优先(user-global,同步表)
    with SessionLocal() as db:
        ov = db.scalar(
            select(UserExchangeRateProjection.rate).where(
                UserExchangeRateProjection.user_id == user.id,
                UserExchangeRateProjection.base_currency == base,
                UserExchangeRateProjection.quote_currency == cc,
            ).limit(1)
        )
    native: float | None = None
    if ov is not None:
        try:
            r = float(ov)
            if r > 0:
                native = amount * r  # 1 cc = r base
        except (TypeError, ValueError):
            native = None
    if native is None:
        # 自动源(server 汇率代理);拉不到就退化 1:1
        try:
            from ...services.exchange_rate import fetcher as _rf
            with SessionLocal() as db:
                row, _stale = await _rf.get_rates(db, base)
            raw = dict(row.payload_json).get(cc) or dict(row.payload_json).get(cc.lower())
            x = float(raw) if raw is not None else 0.0
            native = amount / x if x > 0 else amount  # 1 base = x cc → cc 折 base 要除
        except Exception:
            native = amount
    return {"currency_code": cc, "native_amount": native}


def _lookup_account_sync_id(db, user_id: str, name: str | None) -> str | None:
    if not name:
        return None
    row = db.scalar(
        select(UserAccountProjection)
        .where(
            UserAccountProjection.user_id == user_id,
            UserAccountProjection.name == name,
        )
        .limit(1)
    )
    if row is None:
        raise ValueError(f"Account not found: {name}")
    return row.sync_id
