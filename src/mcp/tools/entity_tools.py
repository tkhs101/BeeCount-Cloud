"""MCP 实体管理工具:账户 / 标签 / 分类 / 预算。

## 为什么单独一个模块

官方 18 个 tool 里,账户和标签**只有读、没有写** —— 而转账必须选账户、打标签
是分析的常规操作。缺了这两个,用户只能先去 Web 端手建,MCP 对话就断了。

服务端 `routers/write/{accounts,tags,categories,budgets}.py` 的写端点本来就齐,
这里只补 MCP 接线。

`write_tools.py` 已经有 1300+ 行(交易相关),实体管理另起一个文件,和
`routers/write/` 按实体拆包的既有风格一致。

## 两处刻意的设计

**按名字查找。** LLM 手里通常只有「招行卡」这种名字,没有 sync_id。所以
account / tag 都支持传名字,查不到时报错**并列出可选项** —— 让 LLM 自我纠正,
而不是瞎猜一个 id 反复重试。

**删除一律二次确认。** 和 `delete_transaction` 同口径:`confirm=False` 返回
`confirmation_required` 占位符,LLM 必须跟用户确认后带 `confirm=True` 再调。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from ...config import get_settings
from ...database import SessionLocal
from ...models import (
    Ledger,
    User,
    UserAccountProjection,
    UserCategoryProjection,
    UserTagProjection,
)
from .write_tools import _resolve_write_ledger, _self_call

logger = logging.getLogger(__name__)

# 与 mobile lib/data/db.dart 的账户类型对齐(AccountType enum)
VALID_ACCOUNT_TYPES = {
    "cash", "bank_card", "credit_card", "alipay", "wechat", "loan",
    "investment", "insurance", "social_fund", "vehicle", "real_estate",
    "other_account",
}


# --------------------------------------------------------------------------- #
# 内部helpers                                                                  #
# --------------------------------------------------------------------------- #


def _require_confirm(confirm: bool, what: str, ident: str) -> dict[str, Any] | None:
    """删除类操作的二次确认闸门。返回 None = 放行。"""
    if confirm:
        return None
    return {
        "status": "confirmation_required",
        "message": (
            f"Deleting {what} requires explicit confirmation. Please confirm with "
            f"the user, then call again with confirm=true."
        ),
        "id": ident,
    }


def _resolve_target(
    db, user: User, ledger_id: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """解析写操作的账本 → `(external_id, 状态dict)`。状态非 None 表示要直接返回。"""
    led, ledger_status = _resolve_write_ledger(db, user, ledger_id)
    if ledger_status is not None:
        return None, ledger_status
    if led is None:
        return None, {"error": "No ledger found"}
    return led.external_id, None


def _lookup_account(db, user_id: str, account: str) -> str:
    """按名字找账户 sync_id(大小写不敏感)。查不到就列出可选项。"""
    rows = db.scalars(
        select(UserAccountProjection).where(
            UserAccountProjection.user_id == user_id
        )
    ).all()
    for row in rows:
        if (row.name or "").strip().lower() == account.strip().lower():
            return str(row.sync_id)
    names = sorted({(r.name or "") for r in rows if r.name})
    raise ValueError(f"Account not found: {account!r}. Available: {names}")


def _lookup_tag(db, user_id: str, tag: str) -> str:
    rows = db.scalars(
        select(UserTagProjection).where(UserTagProjection.user_id == user_id)
    ).all()
    for row in rows:
        if (row.name or "").strip().lower() == tag.strip().lower():
            return str(row.sync_id)
    names = sorted({(r.name or "") for r in rows if r.name})
    raise ValueError(f"Tag not found: {tag!r}. Available: {names}")


def _lookup_category(
    db, user_id: str, category: str, kind: str | None = None
) -> str:
    rows = db.scalars(
        select(UserCategoryProjection).where(
            UserCategoryProjection.user_id == user_id
        )
    ).all()
    for row in rows:
        same_name = (row.name or "").strip().lower() == category.strip().lower()
        if not same_name:
            continue
        if kind is not None and (row.kind or "") != kind:
            continue
        return str(row.sync_id)
    names = sorted({(r.name or "") for r in rows if r.name})
    hint = f" (kind={kind})" if kind else ""
    raise ValueError(f"Category not found: {category!r}{hint}. Available: {names}")


def _write_body(pairs: tuple[tuple[str, Any], ...]) -> dict[str, Any]:
    """只把非 None 的字段塞进 payload(省略 = 不变,PATCH 语义)。"""
    body: dict[str, Any] = {"base_change_id": 0}
    for key, value in pairs:
        if value is not None:
            body[key] = value
    return body


# --------------------------------------------------------------------------- #
# 账户                                                                          #
# --------------------------------------------------------------------------- #


async def create_account(
    user: User,
    *,
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
    """新建账户(现金 / 银行卡 / 信用卡 / 支付宝 …)。

    转账必须选账户 —— 没有这个工具,用户只能先去 Web 端手建。

    Args:
        name: 账户名,账本内唯一。
        account_type: cash / bank_card / credit_card / alipay / wechat /
            loan / investment / insurance / social_fund / vehicle /
            real_estate / other_account。
        currency: 账户币种(ISO)。**多币种账本里「人民币工资卡」这类账户
            必须显式写**,否则会按本位币记账,金额就错了。
        initial_balance: 建账时的一次性期初余额(记成一条期初交易)。
        note: 备注。
        credit_limit: 信用卡额度。
        billing_day / payment_due_day: 账单日 / 还款日(1-31)。
        bank_name / card_last_four: 开户行 / 卡号后四位。
        ledger_id: 可选;多账本时不传会要求先澄清。
    """
    if account_type not in VALID_ACCOUNT_TYPES:
        raise ValueError(
            f"Invalid account_type: {account_type!r}. Valid: {sorted(VALID_ACCOUNT_TYPES)}"
        )
    with SessionLocal() as db:
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None

    body = _write_body((
        ("name", name), ("account_type", account_type), ("currency", currency),
        ("initial_balance", initial_balance), ("note", note),
        ("credit_limit", credit_limit), ("billing_day", billing_day),
        ("payment_due_day", payment_due_day), ("bank_name", bank_name),
        ("card_last_four", card_last_four),
    ))
    body["account_type"] = account_type
    settings = get_settings()
    result = await _self_call(
        "POST", f"{settings.api_prefix}/write/ledgers/{ext}/accounts", user, json=body
    )
    return {
        "sync_id": result.get("entity_id"), "name": name,
        "account_type": account_type, "currency": currency, "_meta": result,
    }


async def update_account(
    user: User,
    *,
    account_id: str | None = None,
    account: str | None = None,
    name: str | None = None,
    currency: str | None = None,
    initial_balance: float | None = None,
    note: str | None = None,
    credit_limit: float | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """改账户。只改传入的字段,省略的保持不变。

    Args:
        account_id: 账户 sync_id;不传就用 account(名字)。
        account: 账户**名字**(LLM 通常只有名字)。
        name / currency / initial_balance / note / credit_limit: 要改的字段,
            至少给一个。
        ledger_id: 可选。
    """
    if not account_id and not account:
        raise ValueError("Provide either account_id or account (name)")
    with SessionLocal() as db:
        aid = account_id or _lookup_account(db, user.id, account or "")
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None

    body = _write_body((
        ("name", name), ("currency", currency),
        ("initial_balance", initial_balance), ("note", note),
        ("credit_limit", credit_limit),
    ))
    if len(body) == 1:
        raise ValueError("Nothing to update — pass at least one field to change")

    settings = get_settings()
    result = await _self_call(
        "PATCH", f"{settings.api_prefix}/write/ledgers/{ext}/accounts/{aid}",
        user, json=body,
    )
    return {
        "sync_id": aid,
        "updated": sorted(k for k in body if k != "base_change_id"),
        "_meta": result,
    }


async def delete_account(
    user: User,
    *,
    account_id: str | None = None,
    account: str | None = None,
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """删账户。**危险操作** —— confirm=false 时返「待确认」,LLM 必须跟用户确认后
    带 confirm=true 再调一次。

    账户上还挂着交易时服务端会拒绝(防孤儿数据),报错会原样带回。

    Args:
        account_id: 账户 sync_id;不传就用 account(名字)。
        account: 账户名字。
        confirm: 必须为 true 才会真删。
        ledger_id: 可选。
    """
    if not account_id and not account:
        raise ValueError("Provide either account_id or account (name)")
    if not confirm:
        return _require_confirm(False, "an account", account_id or account or "") or {}
    with SessionLocal() as db:
        aid = account_id or _lookup_account(db, user.id, account or "")
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None
    settings = get_settings()
    result = await _self_call(
        "DELETE", f"{settings.api_prefix}/write/ledgers/{ext}/accounts/{aid}",
        user, json={"base_change_id": 0, "confirm": True},
    )
    return {"sync_id": aid, "deleted": True, "_meta": result}


async def get_account_balance(
    user: User,
    *,
    account_id: str | None = None,
    account: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """查账户余额,以及全部账户的合计。

    问答「我现在还有多少钱」「这张卡还欠多少」只能靠它 —— `list_accounts`
    只列账户元信息,没有余额。

    口径与 server 的 `/read/workspace/accounts` 逐字对应:
        期初余额 + 收入 − 支出 − 转出 + 转入
    转账对余额的影响按 `from_account_sync_id` / `to_account_sync_id` 分别计,
    与服务端 `list_workspace_accounts` 的 transfer 分支一致。

    Args:
        account_id / account: 指定一个账户;都不传则返回全部 + 合计。
        ledger_id: 可选;多账本时不传会要求先澄清。
    """
    from ...routers.read._shared import (
        account_balance_from_stats,
        account_balance_stats,
    )

    with SessionLocal() as db:
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
        assert ext is not None
        ledger = db.scalar(select(Ledger).where(Ledger.external_id == ext))
        if ledger is None:
            raise ValueError(f"Ledger not found: {ext}")
        want = account_id
        if not want and account:
            want = _lookup_account(db, user.id, account)

        accounts = db.scalars(
            select(UserAccountProjection).where(
                UserAccountProjection.user_id == user.id
            )
        ).all()
        if want:
            accounts = [a for a in accounts if str(a.sync_id) == want]
            if not accounts:
                raise ValueError(f"Account not found: {want!r}")

        # 余额变动委托给 read/_shared.account_balance_stats —— 这段 SQL 原本
        # 在这里是**第二份逐字重复**的实现(第一份在 read/workspace.py)。
        # 漏改一处的表现是「LLM 说的余额和 Web 显示的不一致」,而两边的单测
        # 各自都绿(它们测的是不同端点)。
        stats = account_balance_stats(db, [ledger.id])

        base_currency = ledger.currency
        out: list[dict[str, Any]] = []
        total = 0.0
        for acct in accounts:
            sid = str(acct.sync_id)
            bal = account_balance_from_stats(
                sid, float(acct.initial_balance or 0.0), stats
            )
            total += bal
            out.append({
                "sync_id": sid,
                "name": acct.name,
                "account_type": acct.account_type,
                "currency": acct.currency or base_currency,
                "initial_balance": float(acct.initial_balance or 0.0),
                "balance": round(bal, 2),
                "credit_limit": acct.credit_limit,
                "hidden": bool(acct.hidden),
            })
        out.sort(key=lambda r: -abs(r["balance"]))

    return {
        "ledger": ext,
        "base_currency": base_currency,
        "accounts": out,
        "total_balance": round(total, 2),
        "_note": (
            "balance = initial_balance + income - expense - transfers out + transfers in. "
            "Credit cards go negative when you owe money."
        ),
    }


# --------------------------------------------------------------------------- #
# 标签                                                                          #
# --------------------------------------------------------------------------- #


async def create_tag(
    user: User, *, name: str, color: str | None = None, ledger_id: str | None = None
) -> dict[str, Any]:
    """新建标签(#咖啡 / #出差 …)。标签是**跨账本的用户级**实体。

    Args:
        name: 标签名。
        color: 十六进制颜色,如 '#3B82F6';省略则用默认色。
        ledger_id: 可选。
    """
    with SessionLocal() as db:
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None
    body = _write_body((("name", name), ("color", color)))
    settings = get_settings()
    result = await _self_call(
        "POST", f"{settings.api_prefix}/write/ledgers/{ext}/tags", user, json=body
    )
    return {
        "sync_id": result.get("entity_id"), "name": name, "color": color,
        "_meta": result,
    }


async def update_tag(
    user: User,
    *,
    tag_id: str | None = None,
    tag: str | None = None,
    name: str | None = None,
    color: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """改标签(改名 / 换色)。只改传入的字段。

    Args:
        tag_id / tag: 二选一;LLM 通常只有名字。
        name / color: 要改的字段,至少给一个。
        ledger_id: 可选。
    """
    if not tag_id and not tag:
        raise ValueError("Provide either tag_id or tag (name)")
    with SessionLocal() as db:
        tid = tag_id or _lookup_tag(db, user.id, tag or "")
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None
    body = _write_body((("name", name), ("color", color)))
    if len(body) == 1:
        raise ValueError("Nothing to update — pass name and/or color")
    settings = get_settings()
    result = await _self_call(
        "PATCH", f"{settings.api_prefix}/write/ledgers/{ext}/tags/{tid}", user, json=body
    )
    return {
        "sync_id": tid,
        "updated": sorted(k for k in body if k != "base_change_id"),
        "_meta": result,
    }


async def delete_tag(
    user: User,
    *,
    tag_id: str | None = None,
    tag: str | None = None,
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """删标签。**危险操作** —— 二次确认,同 delete_transaction。

    Args:
        tag_id / tag: 二选一。
        confirm: 必须为 true 才真删。
        ledger_id: 可选。
    """
    if not tag_id and not tag:
        raise ValueError("Provide either tag_id or tag (name)")
    if not confirm:
        return _require_confirm(False, "a tag", tag_id or tag or "") or {}
    with SessionLocal() as db:
        tid = tag_id or _lookup_tag(db, user.id, tag or "")
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None
    settings = get_settings()
    result = await _self_call(
        "DELETE", f"{settings.api_prefix}/write/ledgers/{ext}/tags/{tid}", user,
        json={"base_change_id": 0, "confirm": True},
    )
    return {"sync_id": tid, "deleted": True, "_meta": result}


# --------------------------------------------------------------------------- #
# 分类                                                                          #
# --------------------------------------------------------------------------- #


async def update_category(
    user: User,
    *,
    category_id: str | None = None,
    category: str | None = None,
    name: str | None = None,
    kind: str | None = None,
    icon: str | None = None,
    parent_name: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """改分类(改名 / 换类型 / 换图标 / 挂到别的父分类下)。

    改名会自动 cascade 到该分类下的历史交易(服务端行为)。

    Args:
        category_id / category: 二选一;LLM 通常只有名字。
        kind: expense / income / transfer。
        icon: Material 图标名,如 'restaurant'。
        parent_name: 挂到这个父分类下;传空字符串表示提到一级。
        ledger_id: 可选。
    """
    if not category_id and not category:
        raise ValueError("Provide either category_id or category (name)")
    if kind is not None and kind not in {"expense", "income", "transfer"}:
        raise ValueError(f"Invalid kind: {kind!r}")
    with SessionLocal() as db:
        cid = category_id or _lookup_category(db, user.id, category or "")
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None
    body = _write_body((
        ("name", name), ("kind", kind), ("icon", icon),
        ("parent_name", parent_name),
    ))
    if len(body) == 1:
        raise ValueError("Nothing to update — pass at least one field to change")
    settings = get_settings()
    result = await _self_call(
        "PATCH", f"{settings.api_prefix}/write/ledgers/{ext}/categories/{cid}",
        user, json=body,
    )
    return {
        "sync_id": cid,
        "updated": sorted(k for k in body if k != "base_change_id"),
        "_meta": result,
    }


async def delete_category(
    user: User,
    *,
    category_id: str | None = None,
    category: str | None = None,
    confirm: bool = False,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """删分类。**危险操作** —— 二次确认。

    分类下还有交易、或还有子分类时服务端会拒绝,报错会原样带回。

    Args:
        category_id / category: 二选一。
        confirm: 必须为 true 才真删。
        ledger_id: 可选。
    """
    if not category_id and not category:
        raise ValueError("Provide either category_id or category (name)")
    if not confirm:
        return _require_confirm(False, "a category", category_id or category or "") or {}
    with SessionLocal() as db:
        cid = category_id or _lookup_category(db, user.id, category or "")
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None
    settings = get_settings()
    result = await _self_call(
        "DELETE", f"{settings.api_prefix}/write/ledgers/{ext}/categories/{cid}",
        user, json={"base_change_id": 0, "confirm": True},
    )
    return {"sync_id": cid, "deleted": True, "_meta": result}


# --------------------------------------------------------------------------- #
# 预算                                                                          #
# --------------------------------------------------------------------------- #


async def delete_budget(
    user: User, *, budget_id: str, confirm: bool = False, ledger_id: str | None = None
) -> dict[str, Any]:
    """删预算。**危险操作** —— 二次确认。

    Args:
        budget_id: 预算 sync_id(用 list_budgets 查)。
        confirm: 必须为 true 才真删。
        ledger_id: 可选。
    """
    if not confirm:
        return _require_confirm(False, "a budget", budget_id) or {}
    with SessionLocal() as db:
        ext, status = _resolve_target(db, user, ledger_id)
        if status is not None:
            return status
    assert ext is not None
    settings = get_settings()
    result = await _self_call(
        "DELETE", f"{settings.api_prefix}/write/ledgers/{ext}/budgets/{budget_id}",
        user, json={"base_change_id": 0, "confirm": True},
    )
    return {"sync_id": budget_id, "deleted": True, "_meta": result}
