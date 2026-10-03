"""BeeCount Cloud MCP server — 注册所有 18 个 tool,导出 ASGI app。

设计:.docs/mcp-server-design.md。

挂载入口:`src.main` 里 `app.mount(f"{api_prefix}/mcp", mcp_server.app)`。
完整对外 URL:`/api/v1/mcp/sse`(SSE channel)+ `/api/v1/mcp/messages/` (POST 消息回信道)。

鉴权:`PATAuthMiddleware` 在 ASGI 层校验 `Authorization: Bearer bcmcp_…`,
注入 `scope['bc_mcp_user']` 和 `scope['bc_mcp_scopes']`,tool 函数从
`ctx.request_context.request` 拿。详见 `.auth`。

Tool 注册分两类:
  - read:`require_mcp_scope(ctx, mcp:read)` 后调 `read_tools.py` 同名函数,
    sync 函数用 `asyncio.to_thread` 包一下避免阻塞 event loop。
  - write:`require_mcp_scope(ctx, mcp:write)` 后调 `write_tools.py` 同名
    async 函数(内部用 in-process httpx 调 write router endpoint)。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, TypedDict

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from ..database import SessionLocal
from ..models import MCPCallLog, User
from ..security import SCOPE_MCP_READ, SCOPE_MCP_WRITE
from .auth import (
    PATAuthMiddleware,
    get_mcp_call_meta_from_context,
    get_mcp_user_from_context,
    require_mcp_scope,
)
from .tools import (
    analytics_tools,
    bulk_tools,
    entity_tools,
    read_tools,
    write_tools,
)
from .tools.write_tools import BatchTxItem

logger = logging.getLogger(__name__)


# ============================================================================
# Call logging — 每个 tool call 落一行到 MCPCallLog,Web 设置页"调用历史"读
# ============================================================================

_ARG_SUMMARY_MAX_TOTAL = 200
_ARG_VALUE_MAX_LEN = 30
# 自由文本类 / 隐私敏感 / 大块数据,做 summary 时**整字段跳过**
_ARG_SKIP_FIELDS = {"note", "text"}


def _summarize_args(kwargs: dict[str, Any]) -> str | None:
    """脱敏摘要 — 保留 tool name 调试价值,不存自由文本。"""
    if not kwargs:
        return None
    parts: list[str] = []
    for k, v in kwargs.items():
        if v is None or k in _ARG_SKIP_FIELDS:
            continue
        if isinstance(v, str):
            shown = v if len(v) <= _ARG_VALUE_MAX_LEN else v[: _ARG_VALUE_MAX_LEN - 1] + "…"
        elif isinstance(v, (list, tuple)):
            shown = f"[{len(v)}]"
        elif isinstance(v, dict):
            shown = f"{{...{len(v)}}}"
        else:
            shown = repr(v)
        parts.append(f"{k}={shown}")
    if not parts:
        return None
    s = ", ".join(parts)
    return s if len(s) <= _ARG_SUMMARY_MAX_TOTAL else s[: _ARG_SUMMARY_MAX_TOTAL - 1] + "…"


def _write_call_log(
    *,
    user_id: str,
    pat_id: str | None,
    pat_prefix: str | None,
    pat_name: str | None,
    tool_name: str,
    status: str,
    error: BaseException | None,
    args_summary: str | None,
    duration_ms: int,
    client_ip: str | None,
) -> None:
    """同步落库 — INSERT 单行,毫秒级,在 thread 里跑不阻塞 event loop。
    失败静默(日志告警即可,不阻塞 tool 主流程)。
    """
    try:
        with SessionLocal() as db:
            err_msg: str | None = None
            if error is not None:
                detail = f"{error.__class__.__name__}: {error}"
                err_msg = detail[:500]
            db.add(
                MCPCallLog(
                    user_id=user_id,
                    pat_id=pat_id,
                    pat_prefix=pat_prefix,
                    pat_name=pat_name,
                    tool_name=tool_name,
                    status=status,
                    error_message=err_msg,
                    args_summary=args_summary,
                    duration_ms=duration_ms,
                    client_ip=client_ip,
                    called_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
    except Exception:
        logger.exception("mcp: failed to write call log for tool=%s", tool_name)


async def _logged_call(
    ctx: Context,
    *,
    name: str,
    scope: str,
    kwargs: dict[str, Any],
    body: Callable[[User], Awaitable[Any]],
) -> Any:
    """所有 tool 共用的封装 — scope check + 计时 + 审计落库。

    body 是个接收 user 返回 result 的 coroutine factory。我们在 body 之前
    做 scope 校验,之后无论成功失败都打 log。
    """
    user = get_mcp_user_from_context(ctx)
    require_mcp_scope(ctx, scope)
    meta = get_mcp_call_meta_from_context(ctx)
    summary = _summarize_args(kwargs)
    start = time.perf_counter()
    err: BaseException | None = None
    try:
        return await body(user)
    except BaseException as exc:  # noqa: BLE001 — 兜底打 log,然后 re-raise
        err = exc
        raise
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        # 放到 thread 跑 — DB 写不阻塞 LLM 拿结果
        asyncio.create_task(
            asyncio.to_thread(
                _write_call_log,
                user_id=user.id,
                pat_id=meta.get("pat_id"),
                pat_prefix=meta.get("pat_prefix"),
                pat_name=meta.get("pat_name"),
                tool_name=name,
                status="error" if err is not None else "ok",
                error=err,
                args_summary=summary,
                duration_ms=duration_ms,
                client_ip=meta.get("client_ip"),
            )
        )

# FastMCP 默认 host=127.0.0.1 时会自动开 DNS rebinding 保护,allowed_hosts
# 限定 `127.0.0.1:* / localhost:* / [::1]:*`。问题是我们的 server 实际是
# 挂在 BeeCount-Cloud 的 FastAPI 后面(反代 / 自定义域名 / docker 内网,
# Host header 是任意值),保护一开必报 421/500。这层校验跟我们的 PAT
# Bearer + CORS 是重叠的,关掉,把 host/origin 校验留给上游反代。
mcp = FastMCP(
    "BeeCount Cloud",
    # Streamable HTTP transport:无状态(适合反代 / 无粘性负载均衡)+ 单次
    # JSON 响应(纯 request-response 的 tool 调用不需要 server 主动推流)。
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)


# ============================================================================
# Read tools — 11 个,mcp:read scope
# ============================================================================


class _CreateBudgetKw(TypedDict):
    """`create_budget` 的 kwargs。

    用 TypedDict 而不是 `dict(...)`:mypy 会拿它核对 `**kw` 展开与目标函数签名
    是否匹配。写成 `dict(...)` 时 mypy 只看到 `dict[str, object]`,**每一行
    展开都报一遍 arg-type** —— 上游 `parse_and_create_from_text` 就是这么留下
    存量的 mypy 错误。新写的工具不沿用这个模式。
    """

    amount: float
    budget_type: str
    category: str | None
    period: str
    enabled: bool
    ledger_id: str | None


class _AttachReceiptKw(TypedDict):
    """`attach_receipt` 的 kwargs(见 _CreateBudgetKw 的说明)。"""

    sync_id: str
    image_base64: str
    file_name: str | None
    mime_type: str | None


class _CreateTxKw(TypedDict):
    """`create_transaction` 的 kwargs(见 _CreateBudgetKw 的说明)。"""

    amount: float
    tx_type: str
    category: str | None
    account: str | None
    happened_at: str | None
    note: str | None
    tags: list[str] | None
    ledger_id: str | None
    currency: str | None
    tax_amount: float | None


class _CreateTxReceiptKw(TypedDict):
    """`create_transaction_with_receipt` 的 kwargs(见 _CreateBudgetKw 的说明)。"""

    amount: float
    image_base64: str
    tx_type: str
    category: str | None
    account: str | None
    happened_at: str | None
    note: str | None
    tags: list[str] | None
    ledger_id: str | None
    currency: str | None
    tax_amount: float | None
    file_name: str | None
    mime_type: str | None


class _CreateTxsKw(TypedDict):
    """`create_transactions` 的 kwargs(见 _CreateBudgetKw 的说明)。"""

    transactions: list[BatchTxItem]
    ledger_id: str | None

class _CreateAccountKw(TypedDict):
    """`create_account` 的 kwargs。TypedDict 而非 dict(...) —— 否则 mypy 只会看到
    `dict[str, object]`,`**kw` 展开的每一行都报 arg-type。"""

    name: str
    account_type: str
    currency: str | None
    initial_balance: float | None
    note: str | None
    credit_limit: float | None
    billing_day: int | None
    payment_due_day: int | None
    bank_name: str | None
    card_last_four: str | None
    ledger_id: str | None


class _UpdateAccountKw(TypedDict):
    account_id: str | None
    account: str | None
    name: str | None
    currency: str | None
    initial_balance: float | None
    note: str | None
    credit_limit: float | None
    ledger_id: str | None


class _DeleteAccountKw(TypedDict):
    account_id: str | None
    account: str | None
    confirm: bool
    ledger_id: str | None


class _GetBalanceKw(TypedDict):
    account_id: str | None
    account: str | None
    ledger_id: str | None


class _CreateTagKw(TypedDict):
    name: str
    color: str | None
    ledger_id: str | None


class _UpdateTagKw(TypedDict):
    tag_id: str | None
    tag: str | None
    name: str | None
    color: str | None
    ledger_id: str | None


class _DeleteTagKw(TypedDict):
    tag_id: str | None
    tag: str | None
    confirm: bool
    ledger_id: str | None


class _UpdateCategoryKw(TypedDict):
    category_id: str | None
    category: str | None
    name: str | None
    kind: str | None
    icon: str | None
    parent_name: str | None
    ledger_id: str | None


class _DeleteCategoryKw(TypedDict):
    category_id: str | None
    category: str | None
    confirm: bool
    ledger_id: str | None


class _DeleteBudgetKw(TypedDict):
    budget_id: str
    confirm: bool
    ledger_id: str | None


@mcp.tool()
async def list_ledgers(ctx: Context) -> list[dict[str, Any]]:
    """List all ledgers for the authenticated BeeCount user.

    Returns each ledger's id (external_id), name, currency, and created_at.
    Use the returned id when calling other tools that take ledger_id.
    """
    return await _logged_call(
        ctx, name="list_ledgers", scope=SCOPE_MCP_READ, kwargs={},
        body=lambda user: asyncio.to_thread(read_tools.list_ledgers, user),
    )


@mcp.tool()
async def get_active_ledger(ctx: Context) -> dict[str, Any] | None:
    """Get the user's primary/default ledger.

    Use this when the user doesn't specify which ledger they're talking about.
    Returns null if the user has no ledgers.
    """
    return await _logged_call(
        ctx, name="get_active_ledger", scope=SCOPE_MCP_READ, kwargs={},
        body=lambda user: asyncio.to_thread(read_tools.get_active_ledger, user),
    )


@mcp.tool()
async def list_transactions(
    ctx: Context,
    ledger_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    category: str | None = None,
    account: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    q: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Query transactions with rich filters.

    Args:
        ledger_id: Optional. If omitted, uses the active ledger.
        date_from, date_to: ISO dates (YYYY-MM-DD) or full ISO datetimes.
        category: Exact category name match.
        account: Exact account name match (matches account/from_account/to_account).
        min_amount, max_amount: Filter by absolute amount.
        q: Substring match against note.
        limit: Max items returned (1..200, default 50).
    """
    kw = dict(
        ledger_id=ledger_id, date_from=date_from, date_to=date_to,
        category=category, account=account, min_amount=min_amount,
        max_amount=max_amount, q=q, limit=limit,
    )
    return await _logged_call(
        ctx, name="list_transactions", scope=SCOPE_MCP_READ, kwargs=kw,
        body=lambda user: asyncio.to_thread(read_tools.list_transactions, user, **kw),
    )


@mcp.tool()
async def get_transaction(ctx: Context, sync_id: str) -> dict[str, Any] | None:
    """Get a single transaction by its sync_id (cross-ledger lookup)."""
    return await _logged_call(
        ctx, name="get_transaction", scope=SCOPE_MCP_READ, kwargs={"sync_id": sync_id},
        body=lambda user: asyncio.to_thread(read_tools.get_transaction, user, sync_id),
    )


@mcp.tool()
async def list_categories(
    ctx: Context, kind: str | None = None
) -> list[dict[str, Any]]:
    """List user's categories. kind is one of: expense, income, transfer."""
    return await _logged_call(
        ctx, name="list_categories", scope=SCOPE_MCP_READ, kwargs={"kind": kind},
        body=lambda user: asyncio.to_thread(read_tools.list_categories, user, kind=kind),
    )


@mcp.tool()
async def list_accounts(
    ctx: Context, account_type: str | None = None
) -> list[dict[str, Any]]:
    """List user's accounts. account_type filters by type (bank_card, credit_card, cash, ...)."""
    return await _logged_call(
        ctx, name="list_accounts", scope=SCOPE_MCP_READ, kwargs={"account_type": account_type},
        body=lambda user: asyncio.to_thread(read_tools.list_accounts, user, account_type=account_type),
    )


@mcp.tool()
async def list_tags(ctx: Context) -> list[dict[str, Any]]:
    """List all of the user's tags."""
    return await _logged_call(
        ctx, name="list_tags", scope=SCOPE_MCP_READ, kwargs={},
        body=lambda user: asyncio.to_thread(read_tools.list_tags, user),
    )


@mcp.tool()
async def list_budgets(
    ctx: Context, ledger_id: str | None = None
) -> list[dict[str, Any]]:
    """List budgets for a ledger with current-month spent/remaining/percent_used."""
    return await _logged_call(
        ctx, name="list_budgets", scope=SCOPE_MCP_READ, kwargs={"ledger_id": ledger_id},
        body=lambda user: asyncio.to_thread(read_tools.list_budgets, user, ledger_id=ledger_id),
    )


@mcp.tool()
async def get_ledger_stats(
    ctx: Context, ledger_id: str | None = None
) -> dict[str, Any] | None:
    """Get summary stats for a ledger (transaction/category/account/tag/budget counts)."""
    return await _logged_call(
        ctx, name="get_ledger_stats", scope=SCOPE_MCP_READ, kwargs={"ledger_id": ledger_id},
        body=lambda user: asyncio.to_thread(read_tools.get_ledger_stats, user, ledger_id=ledger_id),
    )


@mcp.tool()
async def get_analytics_summary(
    ctx: Context,
    scope: str = "month",
    period: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Income/expense/balance plus top-10 spending categories.

    Args:
        scope: 'month' | 'year' | 'all'.
        period: For month: 'YYYY-MM'. For year: 'YYYY'. Defaults to current.
        ledger_id: Optional, uses active ledger if omitted.
    """
    kw = {"scope": scope, "period": period, "ledger_id": ledger_id}
    return await _logged_call(
        ctx, name="get_analytics_summary", scope=SCOPE_MCP_READ, kwargs=kw,
        body=lambda user: asyncio.to_thread(read_tools.get_analytics_summary, user, **kw),
    )


@mcp.tool()
async def search(ctx: Context, q: str, limit: int = 20) -> list[dict[str, Any]]:
    """Full-text fuzzy search across transaction notes, category names, account names."""
    return await _logged_call(
        ctx, name="search", scope=SCOPE_MCP_READ, kwargs={"q": q, "limit": limit},
        body=lambda user: asyncio.to_thread(read_tools.search, user, q=q, limit=limit),
    )


# ============================================================================
# Write tools — 7 个,mcp:write scope
# ============================================================================


@mcp.tool()
async def create_transaction(
    ctx: Context,
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
    """Create a new transaction.

    Args:
        amount: Positive number; type captured separately via tx_type. For an
            expense this is the TOTAL PAID (tax-inclusive), not the pre-tax
            price.
        tx_type: 'expense' (default), 'income', or 'transfer'.
        category: Existing category name (server rejects unknown names).
        account: Existing account name. For transfers this is the from-account.
        happened_at: ISO date or datetime. Defaults to now.
        note: Optional memo.
        tags: Optional list of tag names.
        ledger_id: Optional; uses active ledger if omitted.
        currency: ISO 4217 code (e.g. 'USD', 'JPY') when the amount is in a
            foreign currency. Omit to follow the account's currency, or the
            ledger's base currency when no account is given. The server converts
            to the ledger base at current rates and stores both amounts.
        tax_amount: Consumption tax contained in `amount` (Japan's 消費税). Pass
            the ABSOLUTE figure printed on the receipt — do NOT derive it from
            a tax rate: rounding differs between retailers (a 合計 of 1780 at
            8% back-computes to 1648.15, while the register shows 1649 — off by
            one yen). Omit when there is no tax. Expense only. `amount` stays
            the total paid; statistics move the tax into a "tax & insurance"
            slice, and the two still add up to `amount`.
    """
    kw: _CreateTxKw = {
        "amount": amount, "tx_type": tx_type, "category": category,
        "account": account, "happened_at": happened_at, "note": note,
        "tags": tags, "ledger_id": ledger_id, "currency": currency,
        "tax_amount": tax_amount,
    }
    return await _logged_call(
        ctx, name="create_transaction", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: write_tools.create_transaction(user, **kw),
    )


@mcp.tool()
async def attach_receipt(
    ctx: Context,
    sync_id: str,
    image_base64: str,
    file_name: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Attach a receipt photo to an EXISTING transaction.

    Use this when the expense is already recorded and you only have the
    picture to add. To record the expense AND the picture together, prefer
    create_transaction_with_receipt.

    Args:
        sync_id: The transaction to attach to. Must already exist — this
            never creates a transaction.
        image_base64: Raw base64, or a `data:image/jpeg;base64,` prefix.
            Re-sending the same image does not store it twice (the server
            deduplicates on sha256).
        file_name: Optional display name; inferred from the prefix or
            extension when omitted.
        mime_type: Optional; inferred when omitted.
    """
    kw: _AttachReceiptKw = {
        "sync_id": sync_id, "image_base64": image_base64,
        "file_name": file_name, "mime_type": mime_type,
    }
    return await _logged_call(
        ctx, name="attach_receipt", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: write_tools.attach_receipt(user, **kw),
    )


@mcp.tool()
async def create_transaction_with_receipt(
    ctx: Context,
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
    """Create a transaction AND attach its receipt photo in one step.

    This is the preferred entry point when the user hands you a receipt
    picture: one call instead of create_transaction followed by attach_receipt.

    Args:
        amount: Positive number; for an expense this is the TOTAL PAID
            (tax-inclusive), not the pre-tax price.
        image_base64: Raw base64, or a `data:image/jpeg;base64,` prefix.
        tx_type: 'expense' (default), 'income', or 'transfer'.
        category: Existing category name (server rejects unknown names).
        account: Existing account name; the from-account for transfers.
        happened_at: ISO date or datetime. Defaults to now.
        note: Optional memo — the merchant name usually goes here.
        tags: Optional list of tag names.
        ledger_id: Optional; uses active ledger if omitted.
        currency: ISO 4217 code for foreign-currency amounts.
        tax_amount: Consumption tax inside `amount` (Japan's 消費税), as the
            ABSOLUTE figure printed on the receipt — do not derive it from a
            rate, retailers round differently. Expense only.
        file_name: Optional display name; inferred when omitted.
        mime_type: Optional; inferred when omitted.

    Returns the transaction plus `attachment`. If only the image upload
    failed, the result carries `attachment_error` and an `attachment_hint`:
    the transaction WAS created — retry the image with attach_receipt and do
    NOT create the transaction again.
    """
    kw: _CreateTxReceiptKw = {
        "amount": amount, "image_base64": image_base64, "tx_type": tx_type,
        "category": category, "account": account, "happened_at": happened_at,
        "note": note, "tags": tags, "ledger_id": ledger_id,
        "currency": currency, "tax_amount": tax_amount,
        "file_name": file_name, "mime_type": mime_type,
    }
    return await _logged_call(
        ctx, name="create_transaction_with_receipt", scope=SCOPE_MCP_WRITE,
        kwargs=dict(kw),
        body=lambda user: write_tools.create_transaction_with_receipt(user, **kw),
    )


@mcp.tool()
async def create_transactions(
    ctx: Context,
    transactions: list[BatchTxItem],
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Create many transactions at once — use this for bulk imports.

    Far more efficient than calling create_transaction in a loop: routes through
    the server's batch endpoint (one commit + one notification per ~50 rows),
    avoiding the per-row overhead that makes large imports slow/unreliable.

    Args:
        transactions: list of objects, each like create_transaction's args —
            {amount (>0, a NUMBER — pass 38.00 not "38.00"), tx_type
             (expense|income|transfer, default expense), category, account,
             happened_at (ISO, default now), note, tags,
             currency (ISO 4217, only for foreign-currency amounts)}.
            category/account must be existing names (server rejects unknown ones).
        ledger_id: Optional. If omitted and you have multiple ledgers, the tool
            refuses to guess and returns the candidate list — re-call with an id.
            Max 200 transactions per call; split larger imports across calls.
    """
    kw: _CreateTxsKw = {
        "transactions": transactions, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="create_transactions", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: write_tools.create_transactions(user, **kw),
    )


@mcp.tool()
async def update_transaction(
    ctx: Context,
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
    """Patch an existing transaction. Only the fields you pass are changed.

    `tax_amount` (consumption tax): omit to leave it unchanged, pass 0 to CLEAR
    it, pass a positive number to set it. Pass the absolute figure from the
    receipt rather than deriving it from a rate — retailers round differently.
    """
    kw = dict(
        sync_id=sync_id, amount=amount, tx_type=tx_type, category=category,
        account=account, happened_at=happened_at, note=note, tags=tags,
        tax_amount=tax_amount,
    )
    return await _logged_call(
        ctx, name="update_transaction", scope=SCOPE_MCP_WRITE, kwargs=kw,
        body=lambda user: write_tools.update_transaction(user, **kw),
    )


@mcp.tool()
async def delete_transaction(
    ctx: Context, sync_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Delete a transaction.

    **Destructive — two-step confirmation required.** Calling with confirm=False
    returns a `confirmation_required` placeholder; you must then prompt the user,
    and only call again with confirm=true after they explicitly agree.
    """
    kw = {"sync_id": sync_id, "confirm": confirm}
    return await _logged_call(
        ctx, name="delete_transaction", scope=SCOPE_MCP_WRITE, kwargs=kw,
        body=lambda user: write_tools.delete_transaction(user, **kw),
    )


@mcp.tool()
async def create_category(
    ctx: Context,
    name: str,
    kind: str = "expense",
    parent_name: str | None = None,
    icon: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Create a new category. Usually unnecessary — prefer existing categories."""
    kw = dict(name=name, kind=kind, parent_name=parent_name, icon=icon, ledger_id=ledger_id)
    return await _logged_call(
        ctx, name="create_category", scope=SCOPE_MCP_WRITE, kwargs=kw,
        body=lambda user: write_tools.create_category(user, **kw),
    )


@mcp.tool()
async def create_budget(
    ctx: Context,
    amount: float,
    budget_type: str = "total",
    category: str | None = None,
    period: str = "monthly",
    enabled: bool = True,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Create a spending budget.

    Args:
        amount: Positive number in the ledger's base currency.
        budget_type: 'total' (whole-ledger cap) or 'category' (cap for one
            category). Defaults to 'total'.
        category: Existing category NAME; required when budget_type is
            'category'. Use update_budget to change an existing budget's
            amount, or delete_transaction-style flows for other edits.
        period: 'monthly' (default), 'weekly', or 'yearly'. In practice the
            billing period follows the ledger's month-start-day setting.
        enabled: Create it paused when False.
        ledger_id: Optional; uses active ledger if omitted.
    """
    kw: _CreateBudgetKw = {
        "amount": amount, "budget_type": budget_type, "category": category,
        "period": period, "enabled": enabled, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="create_budget", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: write_tools.create_budget(user, **kw),
    )


@mcp.tool()
async def update_budget(ctx: Context, budget_id: str, amount: float) -> dict[str, Any]:
    """Update a budget's amount."""
    kw = {"budget_id": budget_id, "amount": amount}
    return await _logged_call(
        ctx, name="update_budget", scope=SCOPE_MCP_WRITE, kwargs=kw,
        body=lambda user: write_tools.update_budget(user, **kw),
    )


@mcp.tool()
async def parse_and_create_from_text(
    ctx: Context, text: str, ledger_id: str | None = None
) -> dict[str, Any]:
    """Have BeeCount AI parse free-form natural-language text into a transaction.

    Useful when the user gives a sentence like "上午星巴克花了 38" and you want
    BeeCount's own AI prompt + ledger context to do the heavy lifting. Requires
    the user to have configured an AI chat provider in their profile.
    """
    kw = {"text": text, "ledger_id": ledger_id}
    return await _logged_call(
        ctx, name="parse_and_create_from_text", scope=SCOPE_MCP_WRITE, kwargs=kw,
        body=lambda user: write_tools.parse_and_create_from_text(user, **kw),
    )


# ============================================================================
# ASGI mount — wrap FastMCP's Streamable HTTP app with PAT auth middleware
# ============================================================================


def _build_app():
    """Build the ASGI app to mount at `/api/v1/mcp`.

    Streamable HTTP transport(单端点 `POST /api/v1/mcp`),取代 MCP 官方已弃用
    的老式 SSE(`GET /sse` + `POST /messages/`)。

    `streamable_http_app()` 只调一次用来懒创建 session manager;它返回的
    Starlette 我们不用 —— 那个把 handler 挂在子路径 `/mcp` 且自带 lifespan,
    `app.mount()` 后 Starlette 不会传播子 app 的 lifespan。这里改取路径无关的
    `StreamableHTTPASGIApp` 直接挂在 mount 根,对外端点就干净地是
    `/api/v1/mcp`(与 `.well-known/oauth-protected-resource` 的 resource 对齐)。

    session manager 的 `.run()` 由 `src.main` 的 startup/shutdown 负责进入/退出。

    外层套 `PATAuthMiddleware`,每个请求都要 `Authorization: Bearer bcmcp_…`。
    """
    mcp.streamable_http_app()  # 触发 session manager 懒创建(返回的 Starlette 不用)
    return PATAuthMiddleware(StreamableHTTPASGIApp(mcp.session_manager))


# 模块级 ASGI app:`src.main` 直接 `app.mount(prefix, mcp_server.app)`。
app = _build_app()
# reload trigger
# --------------------------------------------------------------------------- #
# 实体管理:账户 / 标签 / 分类 / 预算                                            #
# --------------------------------------------------------------------------- #
# 官方 18 个 tool 里账户和标签只有读、没有写 —— 转账必须选账户,打标签是分析
# 的常规操作,缺了这两个用户只能先去 Web 手建。服务端的写端点本来就齐,这里只
# 补 MCP 接线。实现见 tools/entity_tools.py。


@mcp.tool()
async def create_account(
    ctx: Context,
    name: str,
    account_type: str = "cash",
    currency: str | None = None,
    initial_balance: float | None = None,
    note: str | None = None,
    credit_limit: float | None = None,
    billing_day: int | None = None,
    payment_due_day: int | None = None,
    bank_name: str | None = None,
    card_last_four: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Create a bank/cash/credit-card account.

    Transfers need an account, so without this the user has to leave the chat
    and create one in the web UI first.

    Args:
        name: Account name, unique within the ledger.
        account_type: cash / bank_card / credit_card / alipay / wechat /
            loan / investment / insurance / social_fund / vehicle /
            real_estate / receivable / other_account. 本 fork 另加:
            `bank_account`(银行普通存款户口,只记余额 / 振込)与 `point_card`
            (积分卡,口径 1 积分 = 1 日元)。
        currency: ISO code. **Set it explicitly for foreign-currency
            accounts** (e.g. a CNY salary card in a JPY ledger) — otherwise
            amounts get booked in the ledger's base currency.
        initial_balance: One-off opening balance, booked as a transaction.
        note: Free-form memo.
        credit_limit: Credit limit, for credit cards.
        billing_day / payment_due_day: Statement / payment day, 1-31.
        bank_name / card_last_four: Issuer name / last four digits.
        ledger_id: Optional; omitted when you have several ledgers returns a
            clarification request instead of guessing.
    """
    kw: _CreateAccountKw = {
        "name": name, "account_type": account_type, "currency": currency,
        "initial_balance": initial_balance, "note": note,
        "credit_limit": credit_limit, "billing_day": billing_day,
        "payment_due_day": payment_due_day, "bank_name": bank_name,
        "card_last_four": card_last_four, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="create_account", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.create_account(user, **kw),
    )


@mcp.tool()
async def update_account(
    ctx: Context,
    account_id: str | None = None,
    account: str | None = None,
    name: str | None = None,
    currency: str | None = None,
    initial_balance: float | None = None,
    note: str | None = None,
    credit_limit: float | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Update an account. Only the fields you pass are changed.

    Args:
        account_id: Account sync_id; omit and use `account` instead.
        account: Account **name** — usually all you have from the user.
        name / currency / initial_balance / note / credit_limit: Fields to
            change; pass at least one.
        ledger_id: Optional.
    """
    kw: _UpdateAccountKw = {
        "account_id": account_id, "account": account, "name": name,
        "currency": currency, "initial_balance": initial_balance,
        "note": note, "credit_limit": credit_limit, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="update_account", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.update_account(user, **kw),
    )


@mcp.tool()
async def delete_account(
    ctx: Context,
    account_id: str | None = None,
    account: str | None = None,
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Delete an account. **Destructive — two-step confirmation required.**

    Calling with confirm=false returns a `confirmation_required` placeholder;
    you must then confirm with the user and call again with confirm=true.
    The server refuses while transactions still reference the account.

    Args:
        account_id: Account sync_id; omit and use `account` instead.
        account: Account **name**.
        confirm: Must be true for the delete to actually happen.
        ledger_id: Optional.
    """
    kw: _DeleteAccountKw = {
        "account_id": account_id, "account": account,
        "confirm": confirm, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="delete_account", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.delete_account(user, **kw),
    )


@mcp.tool()
async def get_account_balance(
    ctx: Context,
    account_id: str | None = None,
    account: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Account balances, plus the total across all accounts.

    The only way to answer "how much do I have left" or "how much do I owe
    on this card" — `list_accounts` carries metadata but no balances.

    Computed as: opening balance + income − expense − transfers out +
    transfers in. Credit cards go negative when you owe money.

    Args:
        account_id / account: Narrow to one account; omit both for all.
        ledger_id: Optional.
    """
    kw: _GetBalanceKw = {
        "account_id": account_id, "account": account, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="get_account_balance", scope=SCOPE_MCP_READ, kwargs=dict(kw),
        body=lambda user: entity_tools.get_account_balance(user, **kw),
    )


@mcp.tool()
async def create_tag(
    ctx: Context, name: str, color: str | None = None, ledger_id: str | None = None
) -> dict[str, Any]:
    """Create a tag (#coffee, #travel …). Tags are user-level, shared across ledgers.

    Args:
        name: Tag name.
        color: Hex colour such as '#3B82F6'; a default is used when omitted.
        ledger_id: Optional.
    """
    kw: _CreateTagKw = {"name": name, "color": color, "ledger_id": ledger_id}
    return await _logged_call(
        ctx, name="create_tag", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.create_tag(user, **kw),
    )


@mcp.tool()
async def update_tag(
    ctx: Context,
    tag_id: str | None = None,
    tag: str | None = None,
    name: str | None = None,
    color: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Rename a tag or change its colour. Only the fields you pass are changed.

    Args:
        tag_id: Tag sync_id; omit and use `tag` instead.
        tag: Tag **name** — usually all you have from the user.
        name / color: What to change; pass at least one.
        ledger_id: Optional.
    """
    kw: _UpdateTagKw = {
        "tag_id": tag_id, "tag": tag, "name": name, "color": color,
        "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="update_tag", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.update_tag(user, **kw),
    )


@mcp.tool()
async def delete_tag(
    ctx: Context,
    tag_id: str | None = None,
    tag: str | None = None,
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Delete a tag. **Destructive — two-step confirmation required**, same as
    delete_transaction.

    Args:
        tag_id: Tag sync_id; omit and use `tag` instead.
        tag: Tag **name**.
        confirm: Must be true for the delete to actually happen.
        ledger_id: Optional.
    """
    kw: _DeleteTagKw = {
        "tag_id": tag_id, "tag": tag, "confirm": confirm, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="delete_tag", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.delete_tag(user, **kw),
    )


@mcp.tool()
async def update_category(
    ctx: Context,
    category_id: str | None = None,
    category: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    icon: str | None = None,
    parent_name: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Rename a category, change its type or icon, or move it under a parent.

    Renaming cascades to the category's existing transactions server-side.

    Args:
        category_id: Category sync_id; omit and use `category` instead.
        category: Category **name** — usually all you have from the user.
        name: New name.
        kind: expense / income / transfer.
        icon: Material icon name, e.g. 'restaurant'.
        parent_name: Move under this parent; pass an empty string to promote
            back to top level.
        ledger_id: Optional.
    """
    kw: _UpdateCategoryKw = {
        "category_id": category_id, "category": category, "name": name,
        "kind": kind, "icon": icon, "parent_name": parent_name,
        "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="update_category", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.update_category(user, **kw),
    )


@mcp.tool()
async def delete_category(
    ctx: Context,
    category_id: str | None = None,
    category: str | None = None,
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Delete a category. **Destructive — two-step confirmation required.**

    The server refuses while transactions or child categories still reference
    it; the error is passed back verbatim.

    Args:
        category_id: Category sync_id; omit and use `category` instead.
        category: Category **name**.
        confirm: Must be true for the delete to actually happen.
        ledger_id: Optional.
    """
    kw: _DeleteCategoryKw = {
        "category_id": category_id, "category": category,
        "confirm": confirm, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="delete_category", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.delete_category(user, **kw),
    )


@mcp.tool()
async def delete_budget(
    ctx: Context,
    budget_id: str,
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Delete a budget. **Destructive — two-step confirmation required.**

    Args:
        budget_id: Budget sync_id — use list_budgets to look it up.
        confirm: Must be true for the delete to actually happen.
        ledger_id: Optional.
    """
    kw: _DeleteBudgetKw = {"budget_id": budget_id, "confirm": confirm,
                          "ledger_id": ledger_id}
    return await _logged_call(
        ctx, name="delete_budget", scope=SCOPE_MCP_WRITE, kwargs=dict(kw),
        body=lambda user: entity_tools.delete_budget(user, **kw),
    )


# --------------------------------------------------------------------------- #
# 分析与批量操作                                                                #
# --------------------------------------------------------------------------- #
# 官方 18 个 tool 能回答的都是「某笔是多少」,答不上来的是「钱去哪了、变了没」
# —— 而这才是问 AI 记账 App 的理由。实现见 tools/{analytics,bulk}_tools.py。

class _ComparePeriodsKw(TypedDict):
    scope: str
    period: str | None
    compare: str
    ledger_id: str | None
    top: int


class _SpendingBreakdownKw(TypedDict):
    by: str
    scope: str
    period: str | None
    limit: int
    min_amount: float | None
    q: str | None
    ledger_id: str | None


class _SpendingPatternKw(TypedDict):
    scope: str
    period: str | None
    ledger_id: str | None


class _BatchDeleteKw(TypedDict):
    tx_ids: list[str]
    confirm: bool
    ledger_id: str | None


class _ExportCsvKw(TypedDict):
    date_from: str | None
    date_to: str | None
    tx_type: str | None
    category: str | None
    account: str | None
    q: str | None
    min_amount: float | None
    max_amount: float | None
    lang: str
    ledger_id: str | None


@mcp.tool()
async def compare_periods(
    ctx: Context,
    scope: str = "month",
    period: str | None = None,
    compare: str = "previous",
    ledger_id: str | None = None,
    top: int = 5,
) -> dict[str, Any]:
    """Compare spending against a baseline period — month-over-month or year-over-year.

    `get_analytics_summary` only shows one period, so "how much more did I
    spend this month?", "did dining out go up year over year?" cannot be
    answered. This is the question people ask when reviewing spending.

    Args:
        scope: `month` | `year` | `all` (`all` has no baseline and errors).
        period: Baseline period, e.g. '2026-10' for month, '2026' for year.
            Defaults to the current period.
        compare: `previous` for the preceding period, `last_year` for the same
            period a year ago.
        ledger_id: Optional.
        top: How many largest movers to report per category.
    """
    kw: _ComparePeriodsKw = {
        "scope": scope, "period": period, "compare": compare,
        "ledger_id": ledger_id, "top": top,
    }
    return await _logged_call(
        ctx, name="compare_periods", scope=SCOPE_MCP_READ, kwargs=dict(kw),
        body=lambda user: asyncio.to_thread(analytics_tools.compare_periods, user, **kw),
    )


@mcp.tool()
async def get_spending_breakdown(
    ctx: Context,
    by: str = "merchant",
    scope: str = "month",
    period: str | None = None,
    limit: int = 10,
    min_amount: float | None = None,
    q: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Break spending down by merchant, tag, account or category.

    `search` can FIND "Starbucks" transactions but cannot SUM them. This tool
    answers "how much did I spend at Starbucks in total?".

    Args:
        by: `merchant` (groups by the note field — that is where most people
            write the merchant), `tag`, `account`, or `category`.
        scope: `month` | `year` | `all`.
        period: e.g. '2026-10' for month, '2026' for year.
        limit: How many groups to return.
        min_amount: Only count expenses at or above this amount — handy for
            "where does the big money go".
        q: Substring filter on the group name.
        ledger_id: Optional.
    """
    kw: _SpendingBreakdownKw = {
        "by": by, "scope": scope, "period": period, "limit": limit,
        "min_amount": min_amount, "q": q, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="get_spending_breakdown", scope=SCOPE_MCP_READ, kwargs=dict(kw),
        body=lambda user: asyncio.to_thread(analytics_tools.get_spending_breakdown, user, **kw),
    )


@mcp.tool()
async def get_spending_pattern(
    ctx: Context,
    scope: str = "year",
    period: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Spending habits: distribution by weekday, hour of day and amount band.

    Answers "do I spend more on weekends?", "am I a night owl spender?", "is
    my spending many small things or few big ones?" — none of which the other
    tools can.

    Args:
        scope: `month` | `year` | `all`.
        period: e.g. '2026-10' for month, '2026' for year.
        ledger_id: Optional.
    """
    kw: _SpendingPatternKw = {
        "scope": scope, "period": period, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="get_spending_pattern", scope=SCOPE_MCP_READ, kwargs=dict(kw),
        body=lambda user: asyncio.to_thread(analytics_tools.get_spending_pattern, user, **kw),
    )


@mcp.tool()
async def delete_transactions_batch(
    ctx: Context,
    tx_ids: list[str],
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """Delete many transactions at once. **Destructive — two-step confirmation
    required**, same as delete_transaction.

    For real workloads: after importing a statement you discover duplicates and
    need to clear them in one go — delete_transaction only handles one id at a
    time. Server cap is 200 ids per call.

    NOT atomic: ids that could not be deleted come back under `failed` with a
    reason (not_found / permission_denied / conflict). Report those.

    Args:
        tx_ids: Transaction sync_ids to delete, 1-200 of them.
        confirm: Must be true for the delete to actually happen.
        ledger_id: Optional.
    """
    kw: _BatchDeleteKw = {
        "tx_ids": tx_ids, "confirm": confirm, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="delete_transactions_batch", scope=SCOPE_MCP_WRITE,
        kwargs=dict(kw),
        body=lambda user: bulk_tools.delete_transactions_batch(user, **kw),
    )


@mcp.tool()
async def export_transactions_csv(
    ctx: Context,
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
    """Export transactions as CSV text (nothing is written to disk).

    For opening in Excel / Numbers, or piping into other tooling.

    The export includes a `Tax` column (consumption tax) and the importer
    understands it again — so "export → edit in a spreadsheet → re-import"
    round-trips without losing tax amounts.

    Args:
        date_from / date_to: ISO date bounds, inclusive.
        tx_type: expense / income / transfer.
        category / account: Filter by name.
        q: Keyword match against note / category / account.
        min_amount / max_amount: Amount range.
        lang: Header language: `en` / `zh-CN` / `zh-TW`.
        ledger_id: Optional.
    """
    kw: _ExportCsvKw = {
        "date_from": date_from, "date_to": date_to, "tx_type": tx_type,
        "category": category, "account": account, "q": q,
        "min_amount": min_amount, "max_amount": max_amount,
        "lang": lang, "ledger_id": ledger_id,
    }
    return await _logged_call(
        ctx, name="export_transactions_csv", scope=SCOPE_MCP_READ, kwargs=dict(kw),
        body=lambda user: bulk_tools.export_transactions_csv(user, **kw),
    )
