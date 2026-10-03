"""MCP 数据导出与批量操作。

补的是「单笔工具跑不动真实工作量」的两类:

- 对账单 / Excel 导入后要改 → `update_transaction` 只能一笔一笔
- 想拿去 Excel 做二次分析 → HTTP 有 CSV 导出端点,MCP 没有工具
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from ...config import get_settings
from ...database import SessionLocal
from ...models import User
from .read_tools import _resolve_ledger
from .write_tools import _resolve_write_ledger, _self_call

logger = logging.getLogger(__name__)


async def delete_transactions_batch(
    user: User,
    *,
    tx_ids: list[str],
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """批量删除交易。**危险操作** —— confirm=false 时返「待确认」。

    真实工作量问题:从 Excel / 对账单导进来发现重复或记错了,要一次清掉,
    `delete_transaction` 只能一笔一笔调。服务端单次上限 200 笔。

    返回里 `failed` 列出会被跳过的 id 和原因(not_found / permission_denied /
    conflict)—— **不是全成功或全失败**,所以必须把 failed 带回给用户看。

    Args:
        tx_ids: 要删的交易 sync_id 列表,1-200 个。
        confirm: 必须为 true 才真删。
        ledger_id: 可选;多账本时不传会要求先澄清。
    """
    if not tx_ids:
        raise ValueError("tx_ids must not be empty")
    if len(tx_ids) > 200:
        raise ValueError(
            f"Too many tx_ids ({len(tx_ids)} > 200). Split into multiple calls."
        )
    if not confirm:
        return {
            "status": "confirmation_required",
            "message": (
                f"Deleting {len(tx_ids)} transactions requires explicit "
                f"confirmation. Please confirm with the user, then call again "
                f"with confirm=true."
            ),
            "tx_ids": tx_ids,
        }

    with SessionLocal() as db:
        led, status = _resolve_write_ledger(db, user, ledger_id)
        if status is not None:
            return status
        assert led is not None
        external_id = led.external_id

    settings = get_settings()
    path = (
        f"{settings.api_prefix}/write/ledgers/{external_id}/transactions/batch/delete"
    )
    result = await _self_call(
        "POST", path, user, json={"tx_ids": list(tx_ids), "base_change_id": 0}
    )
    return {
        "ledger": external_id,
        "requested": len(tx_ids),
        "deleted": len(result.get("deleted_tx_ids") or []),
        "deleted_tx_ids": result.get("deleted_tx_ids") or [],
        "failed": result.get("failed") or [],
        "_note": (
            "Not atomic: ids that could not be deleted come back in `failed`. "
            "Report those to the user."
        ),
    }


async def export_transactions_csv(
    user: User,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    tx_type: str | None = None,
    category: str | None = None,
    account: str | None = None,
    q: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    lang: str = "en",
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """导出 CSV 文本(不落文件)。

    拿去 Excel / Numbers 做二次分析,或者导入另一套工具。

    ⚠️ **含消费税的导出带 `Tax` 列**(本 fork 新增)。反过来导入本系统的 CSV
    也会认这一列 —— 所以「导出 → 用 Excel 改 → 再导回」是闭环的,税额不会丢。

    Args:
        date_from / date_to: ISO 日期边界(含端点)。
        tx_type: expense / income / transfer。
        category / account: 按名字过滤。
        q: 关键词(备注 / 分类 / 账户名模糊匹配)。
        min_amount / max_amount: 金额区间。
        lang: 表头语言,`en` / `zh-CN` / `zh-TW`。
        ledger_id: 可选。
    """
    from ...models import UserCategoryProjection

    with SessionLocal() as db:
        led = _resolve_ledger(db, user.id, ledger_id)
        if led is None:
            return {"error": "No ledger found"}
        external_id = led.external_id
        cat_id = None
        if category:
            rows = db.scalars(
                select(UserCategoryProjection).where(
                    UserCategoryProjection.user_id == user.id
                )
            ).all()
            for r in rows:
                if (r.name or "").strip().lower() == category.strip().lower():
                    cat_id = str(r.sync_id)
                    break
            if cat_id is None:
                return {"error": f"Category not found: {category!r}"}

    # ⚠️ **参数名必须对齐端点**,端点是 `amount_min` / `amount_max`
    # (MCP 工具对外叫 `min_amount` / `max_amount`)。写成和工具同名的话
    # FastAPI 会把未知 query 参数**静默忽略** —— 过滤器看着传了却没生效,
    # 导出结果比预期多,而且不报错。列表工具那侧的 `min_amount` 是另一个端点的。
    params: dict[str, Any] = {"lang": lang}
    for key, value in (
        ("date_from", date_from), ("date_to", date_to), ("tx_type", tx_type),
        ("category_sync_id", cat_id), ("account_name", account), ("q", q),
        ("amount_min", min_amount), ("amount_max", max_amount),
        ("ledger_id", external_id),
    ):
        if value is not None:
            params[key] = value

    settings = get_settings()
    url = f"{settings.api_prefix}/read/workspace/transactions.csv"
    # self-call 只回 JSON,CSV 是文本流 —— 这里直接取原始 content。
    #
    # **不能用 write_tools._internal_token**:它只签 `SCOPE_APP_WRITE`,而读端点
    # 要求 `SCOPE_WEB_READ`,会 403 AUTH_INSUFFICIENT_SCOPE。
    from ..._mcp_internal_client import get_internal_client

    client = get_internal_client()
    resp = await client.get(
        url,
        params=params,
        headers={
            "Authorization": f"Bearer {_internal_read_token(user)}",
            "X-Device-ID": "mcp-internal",
        },
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"export failed: {resp.status_code} {resp.text[:200]}")
    text = resp.text
    lines = text.lstrip("﻿").splitlines()
    return {
        "ledger": external_id,
        "filename": f"beecount-{external_id}.csv",
        "row_count": max(0, len(lines) - 1),
        "columns": lines[0].split(",") if lines else [],
        "csv": text,
        "_note": (
            "Full CSV text is included so it can be written to a file directly. "
            "The Tax column (consumption tax) round-trips through the importer."
        ),
    }


def _internal_read_token(user: User) -> str:
    """self-call **读**端点用的短期 token。

    `write_tools._internal_token` 只签 `SCOPE_APP_WRITE`;读端点的
    `require_scopes(SCOPE_WEB_READ)` 不认它,会 403。
    """
    from datetime import timedelta as _td

    from ...security import SCOPE_WEB_READ, _create_token

    return _create_token(
        sub=user.id, token_type="access", expires_delta=_td(seconds=60),
        scopes=[SCOPE_WEB_READ], client_type="app",
    )
