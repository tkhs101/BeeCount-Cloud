from __future__ import annotations

import logging
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


def _new_sync_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


_LEGACY_SYNC_ID_PATTERN = re.compile(r"^(tx|acc|cat|tag)_(\d+)_([A-Za-z0-9]+)$")


def _to_iso8601(raw: object) -> str:
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        return raw.astimezone(timezone.utc).isoformat()
    if isinstance(raw, str) and raw.strip():
        value = raw.strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            return datetime.now(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _to_float(raw: object) -> float:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return 0.0
    return 0.0


def _to_optional_float(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _to_optional_int(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _to_optional_str(raw: object) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


# 扩展字段映射:web/server payload(snake_case) → snapshot(camelCase 跟 mobile
# lib/data/db.dart Account 字段名对齐)。`apply` 只处理 payload 里 explicitly
# 提供的 key —— 这样 update 时不传该字段就不动它,跟旧字段(name/account_type
# /currency/initial_balance)的语义一致。
_ACCOUNT_OPTIONAL_FIELD_MAP: tuple[tuple[str, str, str], ...] = (
    # (payload_key, snapshot_key, kind)
    ("note", "note", "str"),
    ("credit_limit", "creditLimit", "float"),
    ("billing_day", "billingDay", "int"),
    ("payment_due_day", "paymentDueDay", "int"),
    ("bank_name", "bankName", "str"),
    ("card_last_four", "cardLastFour", "str"),
    # ---- 信用卡自动还款(0022)-------------------------------------------- #
    # 走同一张表 = 自动获得「缺键保留」语义(update 不传就不动)。
    # 这一点很重要:Web 编辑账户表单一次提交全部字段,若改成无脑覆盖,
    # 任何一次没带 `autorepay_enabled` 的更新都会把用户的配置静默清掉。
    ("autorepay_enabled", "autorepayEnabled", "bool"),
    ("autorepay_from_account_id", "autorepayFromAccountSyncId", "str"),
    ("autorepay_last_period", "autorepayLastPeriod", "str"),
    # 账户隐藏(issue #240):Web create/update 请求体带 hidden(bool)时才写;
    # 不带 key → 保留原值(不冲掉已有隐藏标记,契约对齐 mobile push 的 merge
    # 缺键保留语义)。
    ("hidden", "hidden", "bool"),
)


def _apply_account_optional_fields(account: dict, payload: dict) -> None:
    """payload 里如果带这些 key 就写到 snapshot,空字符串 / None 视作 null。

    update 路径调用同一函数:`payload` 不带某 key → 保留原值。带 key 但 value
    是 None / 空串 → 显式清空(对应 mobile 编辑时把 note 清掉的场景)。
    """
    for payload_key, snapshot_key, kind in _ACCOUNT_OPTIONAL_FIELD_MAP:
        if payload_key not in payload:
            continue
        raw = payload.get(payload_key)
        if kind == "float":
            account[snapshot_key] = _to_optional_float(raw)
        elif kind == "int":
            account[snapshot_key] = _to_optional_int(raw)
        elif kind == "bool":
            # hidden 是投影里的 NOT NULL 布尔列,没有"清空"语义 —— 显式带
            # key 但 value 是 None 时按 False(未隐藏)处理,不留 null。
            account[snapshot_key] = bool(raw) if raw is not None else False
        else:
            account[snapshot_key] = _to_optional_str(raw)


def _ensure_list(snapshot: dict, key: str) -> list[dict]:
    raw = snapshot.get(key)
    if not isinstance(raw, list):
        raw = []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
    snapshot[key] = out
    return out


def _ensure_sync_id(items: list[dict], prefix: str) -> None:
    for item in items:
        sync_id = item.get("syncId")
        if not isinstance(sync_id, str) or not sync_id.strip():
            item["syncId"] = _new_sync_id(prefix)


def ensure_snapshot_v2(snapshot: dict | None) -> dict:
    target = deepcopy(snapshot) if isinstance(snapshot, dict) else {}
    target["ledgerName"] = str(target.get("ledgerName") or "Untitled")
    target["currency"] = str(target.get("currency") or "CNY")

    items = _ensure_list(target, "items")
    accounts = _ensure_list(target, "accounts")
    categories = _ensure_list(target, "categories")
    tags = _ensure_list(target, "tags")

    _ensure_sync_id(items, "tx")
    _ensure_sync_id(accounts, "acc")
    _ensure_sync_id(categories, "cat")
    _ensure_sync_id(tags, "tag")

    for item in items:
        item["type"] = str(item.get("type") or "expense")
        item["amount"] = _to_float(item.get("amount"))
        item["happenedAt"] = _to_iso8601(item.get("happenedAt"))
    for account in accounts:
        account["name"] = str(account.get("name") or "").strip()
        account["type"] = str(account.get("type") or "") or None
        account["currency"] = str(account.get("currency") or "") or None
        if "initialBalance" in account:
            account["initialBalance"] = _to_float(account.get("initialBalance"))
    for category in categories:
        category["name"] = str(category.get("name") or "").strip()
        category["kind"] = str(category.get("kind") or "expense").strip()
    for tag in tags:
        tag["name"] = str(tag.get("name") or "").strip()

    target["count"] = len(items)
    return target


def _legacy_sync_id(sync_id: str) -> tuple[str, int] | None:
    match = _LEGACY_SYNC_ID_PATTERN.fullmatch(sync_id.strip())
    if match is None:
        return None
    prefix, index, _suffix = match.groups()
    return prefix, int(index)


def _find_by_sync_id(
    items: list[dict], sync_id: str, *, expected_prefix: str | None = None
) -> tuple[int, dict]:
    normalized_id = sync_id.strip()
    for idx, item in enumerate(items):
        if str(item.get("syncId")) == normalized_id:
            return idx, item

    legacy = _legacy_sync_id(normalized_id)
    if legacy is not None:
        prefix, legacy_index = legacy
        if (expected_prefix is None or prefix == expected_prefix) and 0 <= legacy_index < len(items):
            fallback_item = items[legacy_index]
            fallback_item["syncId"] = normalized_id
            return legacy_index, fallback_item
    raise KeyError("entity not found")


def _actor_user_id(payload: dict) -> str | None:
    raw = payload.get("__actor_user_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _actor_is_admin(payload: dict) -> bool:
    # 单用户隔离:admin 不再拥有"跨用户改别人账本"的权限,这个 helper 保留
    # 只是为了老代码调用点不报错,恒返回 False。
    _ = payload
    return False


def _assert_actor_can_modify(item: dict, payload: dict) -> None:
    actor_user_id = _actor_user_id(payload)
    if actor_user_id is None:
        return
    if _actor_is_admin(payload):
        return
    # 共享账本:caller 是该账本的 Owner / Editor(_TRANSACTION_WRITE_ROLES
    # 已在 endpoint 层放行)→ 可以改任何 member 的 tx / category / tag /
    # account / budget。__actor_in_shared_ledger 由 _payload_with_actor
    # 注入,基于 caller 在 LedgerMember 表的存在性 + role。
    if payload.get("__actor_in_shared_ledger") is True:
        return
    created_by = item.get("createdByUserId")
    if isinstance(created_by, str) and created_by.strip() and created_by.strip() != actor_user_id:
        raise PermissionError("write role forbidden: entity owner mismatch")


def _mark_entity_actor(item: dict, payload: dict, *, create: bool) -> None:
    actor_user_id = _actor_user_id(payload)
    if actor_user_id is None:
        return
    if create:
        item["createdByUserId"] = actor_user_id
    elif not isinstance(item.get("createdByUserId"), str) or not str(item.get("createdByUserId")).strip():
        item["createdByUserId"] = actor_user_id
    item["updatedByUserId"] = actor_user_id


def _normalize_tx_tags(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        tags = [part.strip() for part in raw.split(",") if part.strip()]
        if not tags:
            return None
        return ",".join(dict.fromkeys(tags))
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            value = str(item).strip()
            if value:
                parts.append(value)
        if not parts:
            return None
        return ",".join(dict.fromkeys(parts))
    return None


def _sort_transactions(snapshot: dict) -> None:
    items = _ensure_list(snapshot, "items")
    items.sort(key=lambda item: _to_iso8601(item.get("happenedAt")), reverse=True)


def _normalize_tax_amount(raw: object, amount: float, tx_type: str) -> float | None:
    """校验并归一 tax_amount(消费税税额,0020)。None = 无税(合法)。

    规则:
      - 仅 expense 允许有税额 —— income / transfer 上的「税」不是消费税语义
      - 必须 > 0
      - 必须 < amount —— 税额不可能等于或超过实付总额

    **不与 amount 联动**:税是从小票抄下来的绝对值,各家舍入方式不同
    (1780 ÷ 1.08 = 1648.15 与收银机显示的 1649 差 1 円),等比缩放只会
    制造出 0.5 円这种对不上账的数。所以改 amount 时 tax 保持不动,只在这里
    校验两者是否仍然自洽。
    """
    if raw is None:
        return None
    tax = _to_optional_float(raw)
    if tax is None:
        return None
    if tx_type != "expense":
        raise ValueError(
            "write validation failed: tax_amount is only allowed on expense transactions"
        )
    if tax <= 0:
        raise ValueError("write validation failed: tax_amount must be positive")
    if amount <= 0 or tax >= amount:
        raise ValueError("write validation failed: tax_amount must be less than amount")
    return tax


# 拆分金额求和的容差。JPY 没有小数,CNY/USD 有 —— 各腿四舍五入后与总额
# 可能有分位误差,但绝不允许到「差 1 円」那种对不上账的程度。
_SPLIT_SUM_TOLERANCE = 1e-6


def _normalize_splits(
    raw: object,
    *,
    amount: float,
    tx_type: str,
) -> list[dict[str, Any]] | None:
    """校验并归一 splits(组合支付,0021)。返回 None = 不是拆分交易。

    四条不变式:

    1. `None` 或 `[]` → 不是拆分(返回 None)
    2. 至少 **2** 条腿 —— 1 条等于没拆,应当走普通单账户路径
    3. `sum(leg.amount) == amount`,容差 1e-6
    4. 每条腿 `amount > 0`,且 `account_id` 非空

    为什么第 3 条要报错而不自动补差:5000 拆成 3000 + 1999 时,差的那 1 円
    补到哪条腿都是猜的,补错了用户要自己发现。宁可写不进去。

    **不与 amount 联动**:和 `_normalize_tax_amount` 同样的理由 —— 不等就拒绝,
    不按比例缩放。改 amount 而 splits 不变时,mutator 会按现有腿数重新校验,
    不自洽就整体丢弃 splits(见 update 路径的降级块),而不是悄悄改用户填的数字。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("write validation failed: splits must be a list")
    if len(raw) < 2:
        if not raw:
            return None
        raise ValueError(
            "write validation failed: splits needs at least 2 legs "
            "(a single leg is a normal transaction)"
        )
    if tx_type != "expense":
        raise ValueError(
            "write validation failed: splits is only allowed on expense transactions"
        )
    if amount <= 0:
        raise ValueError(
            "write validation failed: split transaction needs a positive amount"
        )

    legs: list[dict[str, Any]] = []
    total = 0.0
    for i, raw_leg in enumerate(raw):
        if not isinstance(raw_leg, dict):
            raise ValueError(f"write validation failed: splits[{i}] must be an object")
        account_id = str(
            raw_leg.get("account_id") or raw_leg.get("accountId") or ""
        ).strip()
        if not account_id:
            raise ValueError(
                f"write validation failed: splits[{i}] is missing account_id"
            )
        leg_amount = _to_optional_float(raw_leg.get("amount"))
        if leg_amount is None or leg_amount <= 0:
            raise ValueError(
                f"write validation failed: splits[{i}] amount must be positive"
            )
        name = raw_leg.get("account_name") or raw_leg.get("accountName")
        legs.append({
            "accountId": account_id,
            "accountName": str(name).strip() if name else None,
            "amount": leg_amount,
        })
        total += leg_amount

    if abs(total - amount) > _SPLIT_SUM_TOLERANCE:
        raise ValueError(
            "write validation failed: splits must sum to the transaction amount "
            f"(legs={total:g}, amount={amount:g})"
        )
    return legs


def create_transaction(snapshot: dict, payload: dict) -> tuple[dict, str]:
    target = ensure_snapshot_v2(snapshot)
    tx_type = str(payload.get("tx_type") or "expense")
    if tx_type not in {"expense", "income", "transfer"}:
        raise ValueError("write validation failed: invalid transaction type")

    tx_id = _new_sync_id("tx")
    item: dict[str, object] = {
        "syncId": tx_id,
        "type": tx_type,
        "amount": _to_float(payload.get("amount")),
        "happenedAt": _to_iso8601(payload.get("happened_at")),
    }
    # 交易级多币种(0018):Web 币种录入显式传入才写;不传不产生 key
    # (upsert 落 NULL → 统计 COALESCE 回退,旧行为)。
    if payload.get("currency_code") is not None:
        item["currencyCode"] = str(payload.get("currency_code")).upper()
    if payload.get("native_amount") is not None:
        item["nativeAmount"] = _to_float(payload.get("native_amount"))
    # 消费税税额(0020):显式传入才写;不传不产生 key(upsert 落 NULL = 无税)。
    # amount 语义不变 —— 仍是实付总额,税额只是叠加维度。
    tax = _normalize_tax_amount(
        payload.get("tax_amount"), _to_float(item["amount"]), tx_type
    )
    if tax is not None:
        item["taxAmount"] = tax
    if payload.get("note") is not None:
        item["note"] = str(payload.get("note"))
    if payload.get("category_name") is not None:
        item["categoryName"] = str(payload.get("category_name"))
    if payload.get("category_kind") is not None:
        item["categoryKind"] = str(payload.get("category_kind"))
    if payload.get("category_id") is not None:
        item["categoryId"] = str(payload.get("category_id"))
    if payload.get("account_name") is not None:
        item["accountName"] = str(payload.get("account_name"))
    if payload.get("account_id") is not None:
        item["accountId"] = str(payload.get("account_id"))
    if payload.get("from_account_name") is not None:
        item["fromAccountName"] = str(payload.get("from_account_name"))
    if payload.get("from_account_id") is not None:
        item["fromAccountId"] = str(payload.get("from_account_id"))
    if payload.get("to_account_name") is not None:
        item["toAccountName"] = str(payload.get("to_account_name"))
    if payload.get("to_account_id") is not None:
        item["toAccountId"] = str(payload.get("to_account_id"))
    # 转账两端齐备(阶段 0,自动还款的前置修复)。
    #
    # 为什么必须在这里挡:transfer 的余额影响是 `_transfer_legs(from)` 减、
    # `_transfer_legs(to)` 加(`read/_shared.py:494-506`)。两端不齐时只有
    # 一端生效 —— **钱凭空消失,且没有任何异常**。
    #
    # 这不是新引入的缺陷:`projection.py:280-290` 三组账户字段**各自独立**按名
    # 反查,「from 命中 / to 没命中」(账户改名、同名多账户)完全可能发生。
    # 自动还款每期产生一笔 transfer,会把这个既有缺陷**每期触发一次**,
    # 所以在 mutator 层正面挡掉。
    #
    # 名字或 id 任一存在即可(后续按名反查会补 sync_id),但**两端都必须有**。
    if tx_type == "transfer":
        has_from = bool(
            payload.get("from_account_id") or payload.get("from_account_name")
        )
        has_to = bool(
            payload.get("to_account_id") or payload.get("to_account_name")
        )
        if not (has_from and has_to):
            raise ValueError(
                "write validation failed: transfer requires both from_account "
                "and to_account; a one-sided transfer would deduct from one "
                "account without crediting the other (money vanishes silently)"
            )
        # **刻意不拦「转出 == 转入」**。自转账是退化 no-op:`_transfer_legs`
        # 对同一账户减 800 又加 800,净 0,只是 count 虚增 2。既有测试
        # `test_account_balance_paths.py` 把它当边界用例钉住了(「应净 0」),
        # 它不是丢钱的 bug,拦它属于越界。
        # 真正要拦的场景是**自动还款把扣款账户配成这张卡自己** —— 那属于
        # 配置错误,拦在自动还款的配置校验层,不摊到所有交易写入上。
    # 组合支付(0021):有腿时**父交易的账户字段必须为空** —— 否则余额聚合里
    # 父行会再动一次账户,两处叠加双倍扣。所以在这里强制 pop,而不是信任
    # 调用方不传(前端表单很容易两个都填上)。
    splits = _normalize_splits(
        payload.get("splits"),
        amount=_to_float(item["amount"]), tx_type=tx_type,
    )
    if splits is not None:
        item["splits"] = splits
        item.pop("accountName", None)
        item.pop("accountId", None)
    tags = _normalize_tx_tags(payload.get("tags"))
    if tags is not None:
        item["tags"] = tags
    tag_ids_raw = payload.get("tag_ids")
    if isinstance(tag_ids_raw, list):
        tag_ids: list[str] = []
        for raw in tag_ids_raw:
            value = str(raw).strip()
            if value and value not in tag_ids:
                tag_ids.append(value)
        if tag_ids:
            item["tagIds"] = tag_ids
    attachments = payload.get("attachments")
    if isinstance(attachments, list):
        item["attachments"] = attachments
    # 账单标记(.docs/transaction-flags):snapshot 用 camelCase,跟 mobile
    # serializer + projection.upsert_tx(读 excludeFromStats)对齐。create 默认 False。
    item["excludeFromStats"] = bool(payload.get("exclude_from_stats"))
    item["excludeFromBudget"] = bool(payload.get("exclude_from_budget"))
    _mark_entity_actor(item, payload, create=True)

    _ensure_list(target, "items").append(item)
    # 跳过 _sort_transactions(方案 B):snapshot 不写回,排序徒劳
    target["count"] = len(_ensure_list(target, "items"))
    return target, tx_id


def rescale_native_amount(
    old_amount: float, old_native: float, new_amount: float
) -> float:
    """L14 唯一权威实现:amount 变化时 nativeAmount 的联动规则。

    - 同币种/未折算(old_native == old_amount,隐含汇率 1)→ 跟随新 amount
    - 外币 → 按该笔隐含汇率等比缩放(old_native / old_amount * new_amount),
      保持记账时汇率不漂移
    - old_amount == 0 无法推汇率 → 退化 = 新 amount(1:1,App L11 可捞回)

    调用方:本文件 update_transaction(Web 写路径)与 sync_applier.
    _sync_native_amount_after_merge(旧 App push 路径)。App 端 sync apply 的
    「缺键退化 1:1」是有意的另一规则(旧客户端场景宁可退化让 L11 捞),不共用。
    """
    if old_amount == 0.0 or old_native == old_amount:
        return new_amount
    return old_native / old_amount * new_amount


def update_transaction(snapshot: dict, tx_id: str, payload: dict) -> dict:
    target = ensure_snapshot_v2(snapshot)
    items = _ensure_list(target, "items")
    _, item = _find_by_sync_id(items, tx_id, expected_prefix="tx")
    _assert_actor_can_modify(item, payload)

    if "tx_type" in payload:
        tx_type = str(payload.get("tx_type") or "")
        if tx_type not in {"expense", "income", "transfer"}:
            raise ValueError("write validation failed: invalid transaction type")
        item["type"] = tx_type
    if "amount" in payload:
        new_amount = _to_float(payload.get("amount"))
        # 交易级多币种(L14,.docs/multi-currency-ledger):item 带折算快照
        # (nativeAmount,新 App 记的交易都有)时,改 amount 必须联动,否则
        # 账本统计(读 native_amount)会一直显示旧金额。规则:
        #   同币种/未折算(old_native == old_amount)→ 跟随新 amount;
        #   外币 → 按该笔隐含汇率等比缩放(保持记账时汇率);
        #   old_amount == 0 无法推汇率 → 退化 = 新 amount(1:1)。
        # item 无该 key(旧 App 记的存量交易)→ 不产生,upsert 落 NULL,
        # 统计端 COALESCE 回退新 amount。payload 显式带 native_amount 时
        # 以传入为准(下方统一写入),跳过联动。
        if "nativeAmount" in item and payload.get("native_amount") is None:
            old_amount = _to_float(item.get("amount"))
            old_native = _to_float(item.get("nativeAmount"))
            if new_amount != old_amount:
                item["nativeAmount"] = rescale_native_amount(
                    old_amount, old_native, new_amount)
        item["amount"] = new_amount
    if payload.get("native_amount") is not None:
        # 显式传入优先(Web 折算录入);None = 不变。
        item["nativeAmount"] = _to_float(payload.get("native_amount"))
    if payload.get("currency_code") is not None:
        item["currencyCode"] = str(payload.get("currency_code")).upper()
    if "happened_at" in payload:
        item["happenedAt"] = _to_iso8601(payload.get("happened_at"))

    # 消费税税额(0020)。放在 amount / tx_type 落定**之后**:
    #   - 显式传 null → 清除税额(patch 语义;update 端点用 exclude_unset,
    #     所以「不传」= 键不在 payload = 不变,「传 null」= 清除)
    #   - 只改 amount / tx_type 不碰税额时,也要校验既有税额是否仍自洽
    #     (如把 expense 改成 transfer 却留着税)。不做联动缩放,理由见
    #     _normalize_tax_amount 的 docstring。
    if "tax_amount" in payload:
        if payload.get("tax_amount") is None:
            item.pop("taxAmount", None)
        else:
            item["taxAmount"] = _normalize_tax_amount(
                payload.get("tax_amount"),
                _to_float(item.get("amount")),
                str(item.get("type") or "expense"),
            )
    if "taxAmount" in item:
        # 对**既有**税额只做自洽性检查,不抛。
        #
        # 这一段校验的是「调用方没碰过的字段」—— 用户改个备注,不该因为一笔
        # 历史脏数据(税额 >= amount,或 income 上带税)就打不开这笔交易。脏数据
        # 真实可达:`/sync/push` 不经过本 mutator、merge 也不校验,推上来什么
        # 样的 taxAmount 都收。这里降级为「剔除并告警」:统计侧的
        # `tax_in_base_currency` 本来就把结果夹在 [0, base] 内,不会算出负
        # 税额,所以丢掉这一笔的税额是安全降级,远好过整笔卡死。
        try:
            _normalize_tax_amount(
                item["taxAmount"],
                _to_float(item.get("amount")),
                str(item.get("type") or "expense"),
            )
        except ValueError:
            logger.warning(
                "update_transaction: dropping inconsistent taxAmount tx_id=%s "
                "tax=%r amount=%r type=%r",
                tx_id, item.get("taxAmount"), item.get("amount"), item.get("type"),
            )
            item.pop("taxAmount", None)

    # 组合支付(0021)。三种语义与 attachments 一致:
    #   键不存在 = 不动;传 [] = 清空全部腿;传 N 条 = 替换。
    if "splits" in payload:
        raw_splits = payload.get("splits")
        if raw_splits is None or (isinstance(raw_splits, list) and not raw_splits):
            # **写空 list 而不是 pop** —— projection 侧的 `upsert_tx` 用
            # `if "splits" in payload` 决定动不动腿(pop 掉键 = 键不存在 = 不动),
            # 所以「清除」必须留一个空 list 当信号,否则 PATCH `splits: []`
            # 会看起来成功、实际腿一条没删。
            item["splits"] = []
            # 清空腿之后父账户字段本来就没有意义,但如果之前是有腿的状态,
            # 这里不主动恢复 —— 调用方若要变回单账户交易应显式传 account_id。
        elif isinstance(raw_splits, list):
            item["splits"] = _normalize_splits(
                raw_splits,
                amount=_to_float(item.get("amount")),
                tx_type=str(item.get("type") or "expense"),
            )
            item.pop("accountName", None)
            item.pop("accountId", None)
    if "splits" in item:
        # 对**既有**legs 只做自洽性检查,不抛 —— 和上面 taxAmount 同样的理由:
        # `/sync/push` 不经过本 mutator,merge 也不校验,脏数据真实可达。
        # 用户改个备注不该因为一笔历史脏 splits(和 != amount)就打不开它。
        try:
            _normalize_splits(
                item["splits"],
                amount=_to_float(item.get("amount")),
                tx_type=str(item.get("type") or "expense"),
            )
        except ValueError:
            logger.warning(
                "update_transaction: dropping inconsistent splits tx_id=%s "
                "legs=%r amount=%r type=%r",
                tx_id, item.get("splits"), item.get("amount"), item.get("type"),
            )
            # 同样写空 list(见上面「清除」处的注释):键必须留着,
            # 否则 projection 收不到信号,脏腿会留在库里。
            item["splits"] = []
            # 腿丢了但父账户字段已被清空 → 这笔会变成「无账户的支出」。
            # 回填不了(不知道原来该挂哪),所以只告警;账本总额仍正确。
    mapping = {
        "note": "note",
        "category_name": "categoryName",
        "category_kind": "categoryKind",
        "category_id": "categoryId",
        "account_name": "accountName",
        "account_id": "accountId",
        "from_account_name": "fromAccountName",
        "from_account_id": "fromAccountId",
        "to_account_name": "toAccountName",
        "to_account_id": "toAccountId",
    }
    for req_key, snapshot_key in mapping.items():
        if req_key in payload:
            value = payload.get(req_key)
            if value is None or str(value).strip() == "":
                item.pop(snapshot_key, None)
            else:
                item[snapshot_key] = str(value)
    if "tags" in payload:
        raw_tags = payload.get("tags")
        normalized = _normalize_tx_tags(raw_tags)
        logger.info(
            "update_transaction.tags tx_id=%s raw=%r normalized=%r",
            tx_id, raw_tags, normalized,
        )
        if normalized is None:
            item.pop("tags", None)
        else:
            item["tags"] = normalized
    else:
        logger.info("update_transaction.tags tx_id=%s 'tags' key NOT in payload", tx_id)
    if "tag_ids" in payload:
        raw = payload.get("tag_ids")
        if isinstance(raw, list):
            tag_ids: list[str] = []
            for value in raw:
                text = str(value).strip()
                if text and text not in tag_ids:
                    tag_ids.append(text)
            if tag_ids:
                item["tagIds"] = tag_ids
            else:
                item.pop("tagIds", None)
        elif raw is None:
            item.pop("tagIds", None)
    if "attachments" in payload:
        attachments = payload.get("attachments")
        if isinstance(attachments, list):
            item["attachments"] = attachments
        elif attachments is None:
            item.pop("attachments", None)
    # 账单标记(.docs/transaction-flags):web update 请求里 None = 不变(由
    # exclude_unset 的 payload 控制:不传该 key 就不进 payload)。带显式布尔
    # 才写。snapshot 用 camelCase。
    for req_key, snapshot_key in (
        ("exclude_from_stats", "excludeFromStats"),
        ("exclude_from_budget", "excludeFromBudget"),
    ):
        if req_key in payload and payload.get(req_key) is not None:
            item[snapshot_key] = bool(payload.get(req_key))
    _mark_entity_actor(item, payload, create=False)

    # 方案 B 后 snapshot 不写回 DB,items 排序只对 mutator 内部无意义 → 跳过(原 30ms/5k)。
    # projection 读路径走 SQL ORDER BY,顺序由 index 保证。
    target["count"] = len(items)
    return target


def delete_transaction(snapshot: dict, tx_id: str, payload: dict | None = None) -> dict:
    target = ensure_snapshot_v2(snapshot)
    items = _ensure_list(target, "items")
    idx, item = _find_by_sync_id(items, tx_id, expected_prefix="tx")
    _assert_actor_can_modify(item, payload or {})
    items.pop(idx)
    # 方案 B 后 snapshot 不写回 DB,items 排序只对 mutator 内部无意义 → 跳过(原 30ms/5k)。
    # projection 读路径走 SQL ORDER BY,顺序由 index 保证。
    target["count"] = len(items)
    return target


def _normalize_name(raw: object) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("write validation failed: name is required")
    return value


def create_account(snapshot: dict, payload: dict) -> tuple[dict, str]:
    target = ensure_snapshot_v2(snapshot)
    accounts = _ensure_list(target, "accounts")
    name = _normalize_name(payload.get("name"))
    if any(str(row.get("name", "")).strip().lower() == name.lower() for row in accounts):
        raise ValueError("write validation failed: duplicated account name")
    sync_id = _new_sync_id("acc")
    account = {
        "syncId": sync_id,
        "name": name,
        "type": str(payload.get("account_type") or "") or None,
        "currency": str(payload.get("currency") or "") or None,
        "initialBalance": _to_float(payload.get("initial_balance")),
    }
    # 扩展字段:跟 mobile lib/data/db.dart Account 表 schema 对齐(driftCamel:
    # creditLimit / billingDay / paymentDueDay / bankName / cardLastFour /
    # note)。前端 web 字段是 snake_case,这里转 camelCase 写入 snapshot。
    _apply_account_optional_fields(account, payload)
    _mark_entity_actor(account, payload, create=True)
    accounts.append(account)
    return target, sync_id


def update_account(snapshot: dict, account_id: str, payload: dict) -> dict:
    target = ensure_snapshot_v2(snapshot)
    accounts = _ensure_list(target, "accounts")
    _, account = _find_by_sync_id(accounts, account_id, expected_prefix="acc")
    _assert_actor_can_modify(account, payload)
    old_name = str(account.get("name") or "").strip()

    if "name" in payload:
        new_name = _normalize_name(payload.get("name"))
        if any(
            str(row.get("syncId")) != account_id
            and str(row.get("name", "")).strip().lower() == new_name.lower()
            for row in accounts
        ):
            raise ValueError("write validation failed: duplicated account name")
        account["name"] = new_name
    if "account_type" in payload:
        value = payload.get("account_type")
        account["type"] = str(value) if value else None
    if "currency" in payload:
        value = payload.get("currency")
        account["currency"] = str(value) if value else None
    if "initial_balance" in payload:
        account["initialBalance"] = _to_float(payload.get("initial_balance"))
    _apply_account_optional_fields(account, payload)

    new_name = str(account.get("name") or "").strip()
    if old_name and new_name and old_name != new_name:
        for tx in _ensure_list(target, "items"):
            if tx.get("accountName") == old_name:
                tx["accountName"] = new_name
            if tx.get("fromAccountName") == old_name:
                tx["fromAccountName"] = new_name
            if tx.get("toAccountName") == old_name:
                tx["toAccountName"] = new_name
    _mark_entity_actor(account, payload, create=False)
    return target


def delete_account(snapshot: dict, account_id: str, payload: dict | None = None) -> dict:
    target = ensure_snapshot_v2(snapshot)
    accounts = _ensure_list(target, "accounts")
    idx, account = _find_by_sync_id(accounts, account_id, expected_prefix="acc")
    _assert_actor_can_modify(account, payload or {})
    old_name = str(account.get("name") or "").strip()
    # 安全检查:任何关联交易都拒绝删除(用户决定:不要 warn-and-orphan 模式)。
    # 客户端必须先把交易改/删/迁走,账户的 tx_count 回到 0 才允许删。
    # mobile 自己走 sync_applier 路径不经过 snapshot_mutator,这条 guard 只对
    # web write API 生效;mobile 现有行为(orphan)保留不变。
    if old_name:
        linked = sum(
            1
            for tx in _ensure_list(target, "items")
            if (
                tx.get("accountName") == old_name
                or tx.get("fromAccountName") == old_name
                or tx.get("toAccountName") == old_name
                # 组合支付(0021):拆分父交易的 accountName **被强制清空**
                # (否则余额双倍扣),所以上面三个条件全不命中 → 这个账户
                # 会被判定成「无关联交易」而放行删除 → 子表留下悬空引用 →
                # 余额永久漂移。`data_cleanup` 的孤儿扫描也只扫父表,
                # 全程零报错。必须显式检查腿。
                or any(
                    str(leg.get("accountName") or "") == old_name
                    or str(leg.get("accountId") or "") == account_id
                    for leg in (tx.get("splits") or [])
                    if isinstance(leg, dict)
                )
            )
        )
        if linked > 0:
            raise ValueError(
                "write validation failed: account has linked transactions; "
                f"reassign or delete the {linked} transactions first"
            )
    accounts.pop(idx)
    if old_name:
        for tx in _ensure_list(target, "items"):
            if tx.get("accountName") == old_name:
                tx.pop("accountName", None)
            if tx.get("fromAccountName") == old_name:
                tx.pop("fromAccountName", None)
            if tx.get("toAccountName") == old_name:
                tx.pop("toAccountName", None)
    return target


def create_category(snapshot: dict, payload: dict) -> tuple[dict, str]:
    target = ensure_snapshot_v2(snapshot)
    categories = _ensure_list(target, "categories")
    name = _normalize_name(payload.get("name"))
    kind = str(payload.get("kind") or "expense").strip()
    if kind not in {"expense", "income", "transfer"}:
        raise ValueError("write validation failed: invalid category kind")
    if any(
        str(row.get("name", "")).strip().lower() == name.lower()
        and str(row.get("kind", "")).strip() == kind
        for row in categories
    ):
        raise ValueError("write validation failed: duplicated category")
    sync_id = _new_sync_id("cat")
    category = {
        "syncId": sync_id,
        "name": name,
        "kind": kind,
        "level": payload.get("level"),
        "sortOrder": payload.get("sort_order"),
        "icon": payload.get("icon"),
        "iconType": payload.get("icon_type"),
        "customIconPath": payload.get("custom_icon_path"),
        "iconCloudFileId": payload.get("icon_cloud_file_id"),
        "iconCloudSha256": payload.get("icon_cloud_sha256"),
        "parentName": payload.get("parent_name"),
    }
    _mark_entity_actor(category, payload, create=True)
    categories.append(category)
    return target, sync_id


def update_category(snapshot: dict, category_id: str, payload: dict) -> dict:
    target = ensure_snapshot_v2(snapshot)
    categories = _ensure_list(target, "categories")
    _, category = _find_by_sync_id(categories, category_id, expected_prefix="cat")
    _assert_actor_can_modify(category, payload)
    old_name = str(category.get("name") or "").strip()
    old_kind = str(category.get("kind") or "").strip()

    if "name" in payload:
        category["name"] = _normalize_name(payload.get("name"))
    if "kind" in payload:
        kind = str(payload.get("kind") or "").strip()
        if kind not in {"expense", "income", "transfer"}:
            raise ValueError("write validation failed: invalid category kind")
        category["kind"] = kind
    for req_key, snapshot_key in [
        ("level", "level"),
        ("sort_order", "sortOrder"),
        ("icon", "icon"),
        ("icon_type", "iconType"),
        ("custom_icon_path", "customIconPath"),
        ("icon_cloud_file_id", "iconCloudFileId"),
        ("icon_cloud_sha256", "iconCloudSha256"),
        ("parent_name", "parentName"),
    ]:
        if req_key in payload:
            category[snapshot_key] = payload.get(req_key)

    new_name = str(category.get("name") or "").strip()
    new_kind = str(category.get("kind") or "").strip()
    if any(
        str(row.get("syncId")) != category_id
        and str(row.get("name", "")).strip().lower() == new_name.lower()
        and str(row.get("kind", "")).strip() == new_kind
        for row in categories
    ):
        raise ValueError("write validation failed: duplicated category")

    if old_name and old_kind and (old_name != new_name or old_kind != new_kind):
        for tx in _ensure_list(target, "items"):
            if tx.get("categoryName") == old_name and tx.get("categoryKind") == old_kind:
                tx["categoryName"] = new_name
                tx["categoryKind"] = new_kind
    _mark_entity_actor(category, payload, create=False)
    return target


def delete_category(snapshot: dict, category_id: str, payload: dict | None = None) -> dict:
    target = ensure_snapshot_v2(snapshot)
    categories = _ensure_list(target, "categories")
    idx, category = _find_by_sync_id(categories, category_id, expected_prefix="cat")
    _assert_actor_can_modify(category, payload or {})
    old_name = str(category.get("name") or "").strip()
    old_kind = str(category.get("kind") or "").strip()
    # 严格策略(跟 AccountsPage / mobile 对齐):有子分类或关联交易时拒绝删除,
    # 要求用户先迁移这些数据。比"允许删除并 orphan"安全 — 避免误删导致一堆
    # 无主交易污染 ledger。前端也有同款拦截,这里是兜底服务端校验防止旧客户
    # 端 / 直接 API 调用绕过。
    if old_name and old_kind:
        child_count = sum(
            1
            for row in categories
            if str(row.get("syncId") or "") != category_id
            and str(row.get("parentName") or "").strip() == old_name
            and str(row.get("kind") or "").strip() == old_kind
        )
        if child_count > 0:
            raise ValueError(
                f"write validation failed: category has {child_count} child categories"
            )
        tx_count = sum(
            1
            for tx in _ensure_list(target, "items")
            if tx.get("categoryName") == old_name
            and tx.get("categoryKind") == old_kind
        )
        if tx_count > 0:
            raise ValueError(
                f"write validation failed: category has {tx_count} transactions"
            )
    categories.pop(idx)
    return target


def _split_tags(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw.strip():
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _join_tags(tags: list[str]) -> str | None:
    if not tags:
        return None
    return ",".join(dict.fromkeys(tags))


def create_tag(snapshot: dict, payload: dict) -> tuple[dict, str]:
    target = ensure_snapshot_v2(snapshot)
    tags = _ensure_list(target, "tags")
    name = _normalize_name(payload.get("name"))
    if any(str(row.get("name", "")).strip().lower() == name.lower() for row in tags):
        raise ValueError("write validation failed: duplicated tag")
    sync_id = _new_sync_id("tag")
    item = {"syncId": sync_id, "name": name, "color": payload.get("color")}
    _mark_entity_actor(item, payload, create=True)
    tags.append(item)
    return target, sync_id


def update_tag(snapshot: dict, tag_id: str, payload: dict) -> dict:
    target = ensure_snapshot_v2(snapshot)
    tags = _ensure_list(target, "tags")
    _, tag = _find_by_sync_id(tags, tag_id, expected_prefix="tag")
    _assert_actor_can_modify(tag, payload)
    old_name = str(tag.get("name") or "").strip()
    if "name" in payload:
        new_name = _normalize_name(payload.get("name"))
        if any(
            str(row.get("syncId")) != tag_id
            and str(row.get("name", "")).strip().lower() == new_name.lower()
            for row in tags
        ):
            raise ValueError("write validation failed: duplicated tag")
        tag["name"] = new_name
    if "color" in payload:
        tag["color"] = payload.get("color")

    new_name = str(tag.get("name") or "").strip()
    if old_name and new_name and old_name != new_name:
        for tx in _ensure_list(target, "items"):
            tx_tags = _split_tags(tx.get("tags"))
            if not tx_tags:
                continue
            updated = [new_name if tag_name == old_name else tag_name for tag_name in tx_tags]
            merged = _join_tags(updated)
            if merged is None:
                tx.pop("tags", None)
            else:
                tx["tags"] = merged
    _mark_entity_actor(tag, payload, create=False)
    return target


def delete_tag(snapshot: dict, tag_id: str, payload: dict | None = None) -> dict:
    target = ensure_snapshot_v2(snapshot)
    tags = _ensure_list(target, "tags")
    idx, tag = _find_by_sync_id(tags, tag_id, expected_prefix="tag")
    _assert_actor_can_modify(tag, payload or {})
    old_name = str(tag.get("name") or "").strip()

    # 拦截关联交易:有交易引用此 tag 时禁止删除,让用户先把标签从交易里
    # 摘掉(或删交易)再来删标签。之前是"静默把 tag 从所有引用它的 tx
    # 里抽走",数据上可恢复但用户无感知,跟 app 行为(确认对话框 + 阻止)
    # 不一致,容易误删。
    # ValueError 由路由层抓出来翻译成 4xx 响应,error message 走 i18n。
    if old_name:
        in_use = sum(
            1
            for tx in _ensure_list(target, "items")
            if old_name in _split_tags(tx.get("tags"))
        )
        if in_use > 0:
            raise ValueError(
                f"write validation failed: tag has {in_use} linked transactions"
            )

    tags.pop(idx)
    return target


# ============================================================================
# Budgets —— 跟 mobile lib/data/db.dart Budget 表对齐:type / categoryId /
# amount / period / startDay / enabled。snapshot 用 driftCamel(categoryId,
# startDay)。type='total' 在每个账本只允许一条;'category' 同一 categoryId
# 也只允许一条(对应 mobile budget_edit_page._saveBudget 的 unique check)。
# ============================================================================


def _normalize_budget_period(raw: object) -> str:
    """空 / 无效时回退到 'monthly'(跟 mobile budget_repository 的 default 一致)。"""
    s = str(raw or "").strip().lower()
    if s in ("monthly", "weekly", "yearly"):
        return s
    return "monthly"


def _normalize_budget_type(raw: object) -> str:
    s = str(raw or "").strip().lower()
    if s == "category":
        return "category"
    return "total"


def create_budget(snapshot: dict, payload: dict) -> tuple[dict, str]:
    target = ensure_snapshot_v2(snapshot)
    budgets = _ensure_list(target, "budgets")
    btype = _normalize_budget_type(payload.get("type"))
    category_id = _to_optional_str(payload.get("category_id"))
    period = _normalize_budget_period(payload.get("period"))
    # 唯一性:total 只一条;category 按 categoryId 唯一(对齐 mobile)。
    if btype == "total":
        if any(_normalize_budget_type(row.get("type")) == "total" for row in budgets):
            raise ValueError("write validation failed: total budget already exists")
    else:
        if not category_id:
            raise ValueError("write validation failed: category budget requires category_id")
        if any(
            _normalize_budget_type(row.get("type")) == "category"
            and str(row.get("categoryId") or "") == category_id
            for row in budgets
        ):
            raise ValueError("write validation failed: category budget already exists")
    amount = _to_optional_float(payload.get("amount"))
    if amount is None or amount <= 0:
        raise ValueError("write validation failed: budget amount must be > 0")
    start_day = _to_optional_int(payload.get("start_day"))
    if start_day is None:
        start_day = 1
    if start_day < 1 or start_day > 28:
        raise ValueError("write validation failed: start_day out of range")
    sync_id = _new_sync_id("bgt")
    enabled_raw = payload.get("enabled")
    enabled = bool(enabled_raw) if enabled_raw is not None else True
    # ledgerSyncId 必须显式带,mobile _applyBudgetChange 用它解析本地 ledger id;
    # 不带则 mobile 永远 skip 这条 change。
    ledger_sync_id = _to_optional_str(target.get("ledgerSyncId")) or _to_optional_str(
        payload.get("ledger_sync_id")
    )
    budget = {
        "syncId": sync_id,
        "type": btype,
        "categoryId": category_id if btype == "category" else None,
        "amount": amount,
        "period": period,
        "startDay": start_day,
        "enabled": enabled,
    }
    if ledger_sync_id:
        budget["ledgerSyncId"] = ledger_sync_id
    _mark_entity_actor(budget, payload, create=True)
    budgets.append(budget)
    return target, sync_id


def update_budget(snapshot: dict, budget_id: str, payload: dict) -> dict:
    target = ensure_snapshot_v2(snapshot)
    budgets = _ensure_list(target, "budgets")
    _, budget = _find_by_sync_id(budgets, budget_id, expected_prefix="bgt")
    _assert_actor_can_modify(budget, payload)
    # 历史 budget(snapshot_builder 从 projection 重建已经带 ledgerSyncId,但
    # 老 SyncChange 里的 payload 可能没带)被 update 时,补齐 ledgerSyncId,
    # 否则 mobile 收到这条 update change 还是因为缺 ledgerSyncId 直接 skip。
    if "ledgerSyncId" not in budget:
        ledger_sync_id = _to_optional_str(target.get("ledgerSyncId"))
        if ledger_sync_id:
            budget["ledgerSyncId"] = ledger_sync_id
    if "amount" in payload:
        amount = _to_optional_float(payload.get("amount"))
        if amount is None or amount <= 0:
            raise ValueError("write validation failed: budget amount must be > 0")
        budget["amount"] = amount
    if "period" in payload:
        budget["period"] = _normalize_budget_period(payload.get("period"))
    if "start_day" in payload:
        start_day = _to_optional_int(payload.get("start_day"))
        if start_day is None:
            start_day = 1
        if start_day < 1 or start_day > 28:
            raise ValueError("write validation failed: start_day out of range")
        budget["startDay"] = start_day
    if "enabled" in payload:
        budget["enabled"] = bool(payload.get("enabled"))
    # 不允许改 type 和 categoryId(语义混乱:从 total 改成 category 等于
    # 删一条新建一条,UI 走删除 + 新建路径更直观)。
    _mark_entity_actor(budget, payload, create=False)
    return target


def delete_budget(snapshot: dict, budget_id: str, payload: dict | None = None) -> dict:
    target = ensure_snapshot_v2(snapshot)
    budgets = _ensure_list(target, "budgets")
    idx, budget = _find_by_sync_id(budgets, budget_id, expected_prefix="bgt")
    _assert_actor_can_modify(budget, payload or {})
    budgets.pop(idx)
    return target
