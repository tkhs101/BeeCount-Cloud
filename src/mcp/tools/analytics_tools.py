"""MCP 分析与批量操作工具。

## 为什么单独一个模块

官方 18 个 tool 里能回答的都是「**某笔是多少**」:

- 「餐饮花了多少」→ `get_analytics_summary` 的分类 top10 能答
- 「星巴克那笔 38 块改成 42」→ `update_transaction` 能答
- 「查一下星巴克」→ `search` 能**找到**那几笔,但**没法聚合**

答不上来的是这一类:

- 「这个月比上月多花了多少?」→ 没有任何工具做期间对比
- 「我在星巴克一共花了多少?」→ `search` 只能逐笔列出来,不能求和
- 「交通费里加油占多少?」→ 没有按标签/账户的拆分
- 「把这周的 12 笔重复记录删掉」→ `delete_transaction` 只能一笔一笔

这些才是记账 App 的真正价值 —— 单笔数据用户自己就有,「钱去哪了、变了没」才是
问 AI 的理由。

## 口径

全部复用 server 的既有约定,不自己另立一套:

- 金额取 `coalesce(native_amount, amount)`(账本维度折本位币,0018)
- 排除 `exclude_from_stats` 的交易(与 `workspace_analytics` 的 SQL 过滤一致)
- 消费税沿用 `routers/read/_shared.tax_in_base_currency`(0020)

## 性能

`compare_periods` 会扫两次账本(两个期间),`get_spending_breakdown` 扫一次后在
Python 里聚合。都用 SQL 先把行拉窄(时间范围 + 类型 + 排除标记),再在 Python
里做 groupby —— 标签拆分会炸(1:N),SQL 做不了。
"""
from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from ...database import SessionLocal
from ...models import ReadTxProjection, User
from ...routers.read._shared import tax_in_base_currency
from .read_tools import _resolve_ledger

logger = logging.getLogger(__name__)

_NATIVE = func.coalesce(ReadTxProjection.native_amount, ReadTxProjection.amount)


# --------------------------------------------------------------------------- #
# 期间范围                                                                      #
# --------------------------------------------------------------------------- #


def period_range(
    scope: str, period: str | None, *, now: datetime | None = None
) -> tuple[datetime, datetime, str]:
    """`(scope, period)` → `(start, end, label)`。`scope='all'` 时 start 为 epoch。

    `get_analytics_summary` 原本把这段逻辑内联写了两遍(scope=month / scope=year),
    这里抽出来给三个工具共用 —— 期间口径散在多处最容易出现「A 工具按自然月、
    B 工具按账本月」的偏差。
    """
    now = now or datetime.now(timezone.utc)
    if scope == "all":
        return datetime(1970, 1, 1, tzinfo=timezone.utc), now, "all"
    if scope == "month":
        year, month = now.year, now.month
        if period:
            try:
                year, month = [int(x) for x in period.split("-")[:2]]
            except Exception:
                pass
        last = calendar.monthrange(year, month)[1]
        return (
            datetime(year, month, 1, tzinfo=timezone.utc),
            datetime(year, month, last, tzinfo=timezone.utc) + timedelta(days=1),
            f"{year:04d}-{month:02d}",
        )
    if scope == "year":
        year = now.year
        if period:
            try:
                year = int(period[:4])
            except Exception:
                pass
        return (
            datetime(year, 1, 1, tzinfo=timezone.utc),
            datetime(year + 1, 1, 1, tzinfo=timezone.utc),
            f"{year:04d}",
        )
    raise ValueError(f"Invalid scope: {scope!r}. Use 'month' | 'year' | 'all'.")


def _shift(start: datetime, end: datetime, *, months: int = 0, years: int = 0):
    """把一个期间平移(用于环比 / 同比)。按日历加减,自动处理月末。"""
    def _add_months(d: datetime, n: int) -> datetime:
        m = d.month - 1 + n
        y = d.year + m // 12
        m = m % 12 + 1
        return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))

    if years:
        return _add_months(start, 12 * years), _add_months(end, 12 * years)
    return _add_months(start, months), _add_months(end, months)


def _rows_between(
    db, ledger_id: str, start: datetime, end: datetime,
    *, tx_type: str | None = None,
):
    """拉一段时间的交易行,口径与 server 一致(折本位币 + 排除标记笔)。

    收 session 参数而不是返回 Select —— SQLAlchemy 2.0 的 `Select` 没有
    `.all()`,必须在 session 里 `execute`。
    """
    q = select(
        ReadTxProjection.tx_type,
        _NATIVE.label("amount"),
        ReadTxProjection.amount.label("raw_amount"),
        ReadTxProjection.tax_amount,
        ReadTxProjection.happened_at,
        ReadTxProjection.category_name,
        ReadTxProjection.account_name,
        ReadTxProjection.tags_csv,
        ReadTxProjection.note,
    ).where(
        ReadTxProjection.ledger_id == ledger_id,
        ReadTxProjection.exclude_from_stats == False,  # noqa: E712 — SQL 布尔
        ReadTxProjection.happened_at >= start,
        ReadTxProjection.happened_at < end,
    )
    if tx_type:
        q = q.where(ReadTxProjection.tx_type == tx_type)
    return db.execute(q).all()


def _sum_expense(rows) -> float:
    """支出合计(**净额**:剥掉税额)。

    与 `workspace_analytics` 同口径 —— 分类切片是税前,所以总额也应该税前,
    否则「餐饮 2982 + 税与保险 298」加不回总额。
    """
    total = 0.0
    for _t, amt, raw, tax, *_rest in rows:
        total += amt - tax_in_base_currency(tax, raw, amt)
    return total


def _sum_income(rows) -> float:
    return sum(float(r[1] or 0.0) for r in rows)


# --------------------------------------------------------------------------- #
# 期间对比                                                                     #
# --------------------------------------------------------------------------- #


def compare_periods(
    user: User,
    *,
    scope: str = "month",
    period: str | None = None,
    compare: str = "previous",
    ledger_id: str | None = None,
    top: int = 5,
) -> dict[str, Any]:
    """期间对比:环比(与上一期比)或同比(与去年同期比)。

    `get_analytics_summary` 只能看单个期间,「这个月比上月多花了多少」「餐饮
    同比涨了多少」这类问题答不了 —— 而这正是复盘时最想问的。

    Args:
        scope: `month` | `year` | `all`。
        period: 基准期间,`month` 时形如 `'2026-10'`,`year` 时形如 `'2026'`。
            省略则用当前期间。
        compare: `previous`(环比 / 上一期)或 `last_year`(同比 / 去年同期)。
        ledger_id: 可选;多账本时不传会返回澄清请求而不是瞎猜。
        top: 每个分类返回涨跌前 N 项。
    """
    with SessionLocal() as db:
        led = _resolve_ledger(db, user.id, ledger_id)
        if led is None:
            return {"error": "No ledger found"}
        internal_id = led.id
        base_currency = led.currency
        ext = led.external_id

    if compare not in ("previous", "last_year"):
        raise ValueError(f"Invalid compare: {compare!r}. Use 'previous' | 'last_year'.")

    start, end, label = period_range(scope, period)
    if start == datetime(1970, 1, 1, tzinfo=timezone.utc):
        return {"error": "compare_periods needs a concrete period; scope='all' has no baseline."}
    if compare == "last_year":
        c_start, c_end = _shift(start, end, years=-1)
        c_label = f"{c_start.year:04d}" + (f"-{c_start.month:02d}" if scope == "month" else "")
    else:
        c_start, c_end = _shift(start, end, months=-1 if scope == "month" else -12)
        c_label = f"{c_start.year:04d}" + (f"-{c_start.month:02d}" if scope == "month" else "")

    def _agg(a: datetime, b: datetime) -> dict[str, Any]:
        # 不用 `as db` —— 这个 with 只是给查询一个 session,并不直接用 db
        with SessionLocal() as db:
            rows = _rows_between(db, internal_id, a, b)
            cur_by_cat: dict[str, float] = defaultdict(float)
            for t, amt, raw, tax, _ts, cat, *_ in rows:
                v = float(amt or 0.0)
                if t == "income":
                    continue
                if t != "expense":
                    continue
                cur_by_cat[(cat or "未分类").strip()] += (
                    v - tax_in_base_currency(tax, raw, v)
                )
            return {
                "expense": round(_sum_expense([r for r in rows if r[0] == "expense"]), 2),
                "income": round(_sum_income([r for r in rows if r[0] == "income"]), 2),
                "count": len(rows),
                "_by_cat": dict(cur_by_cat),
            }

    cur = _agg(start, end)
    prev = _agg(c_start, c_end)

    def _delta(a: float, b: float) -> dict[str, Any]:
        diff = round(a - b, 2)
        pct = round(diff / abs(b) * 100, 1) if b else None
        return {"current": round(a, 2), "previous": round(b, 2),
                "delta": diff, "percent_change": pct}

    cats = set(cur["_by_cat"]) | set(prev["_by_cat"])
    rows_out = []
    for name in cats:
        c, p = cur["_by_cat"].get(name, 0.0), prev["_by_cat"].get(name, 0.0)
        rows_out.append({
            "category_name": name,
            "current": round(c, 2), "previous": round(p, 2),
            "delta": round(c - p, 2),
            "percent_change": round((c - p) / abs(p) * 100, 1) if p else None,
        })
    rows_out.sort(key=lambda r: -abs(r["delta"]))
    biggest = sorted(rows_out, key=lambda r: -r["current"])[:top]

    return {
        "ledger": ext,
        "base_currency": base_currency,
        "scope": scope,
        "compare": compare,
        "current_period": {"label": label, "start": start.date().isoformat(),
                           "end": (end - timedelta(days=1)).date().isoformat()},
        "previous_period": {"label": c_label,
                            "start": c_start.date().isoformat(),
                            "end": (c_end - timedelta(days=1)).date().isoformat()},
        "expense": _delta(cur["expense"], prev["expense"]),
        "income": _delta(cur["income"], prev["income"]),
        "transaction_count": {"current": cur["count"], "previous": prev["count"]},
        "largest_changes": rows_out[:top],
        "top_categories_now": biggest,
        "_note": (
            "Amounts are net of consumption tax (same as the pie chart slices), so "
            "category slices add up to the expense total. percent_change is null "
            "when the baseline is zero."
        ),
    }


# --------------------------------------------------------------------------- #
# 维度拆分                                                                      #
# --------------------------------------------------------------------------- #


def get_spending_breakdown(
    user: User,
    *,
    by: str = "merchant",
    scope: str = "month",
    period: str | None = None,
    limit: int = 10,
    min_amount: float | None = None,
    q: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """按维度拆解支出:商户 / 标签 / 账户 / 分类。

    `search` 只能**找到**「星巴克」那几笔,求和得自己加;这个工具直接给
    「在星巴克一共花了多少」。标签是 1:N,SQL 拆不开,所以拉上来在 Python 里
    groupby(单次扫描,不是 N+1)。

    Args:
        by: `merchant`(按备注 —— 用户记账时商家名通常写在备注里)/
            `tag` / `account` / `category`。
        scope: `month` | `year` | `all`。
        period: 期间,`month` 形如 `'2026-10'`。
        limit: 返回前 N 项。
        min_amount: 只看金额 ≥ 此值的支出(筛掉几日元的小额,适合看大额去向)。
        q: 关键词过滤(配合 `by=merchant` 时就是「名字里含这个」)。
        ledger_id: 可选。
    """
    if by not in ("merchant", "tag", "account", "category"):
        raise ValueError(
            f"Invalid by: {by!r}. Use 'merchant' | 'tag' | 'account' | 'category'."
        )
    with SessionLocal() as db:
        led = _resolve_ledger(db, user.id, ledger_id)
        if led is None:
            return {"error": "No ledger found"}
        internal_id, base_currency, ext = led.id, led.currency, led.external_id
    start, end, label = period_range(scope, period)

    with SessionLocal() as db:
        rows = _rows_between(db, internal_id, start, end, tx_type="expense")

    buckets: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    needle = (q or "").strip().lower()

    for _t, amt, raw, tax, _ts, cat, acct, tags_csv, note in rows:
        v = float(amt or 0.0)
        net = v - tax_in_base_currency(tax, raw, v)
        if net < (min_amount or 0.0):
            continue
        if by == "category":
            keys = [(cat or "未分类").strip()]
        elif by == "account":
            keys = [(acct or "未指定账户").strip()]
        elif by == "tag":
            # tags_csv 是逗号分隔(见 read/_shared._tags_list)
            keys = [t.strip() for t in (tags_csv or "").split(",") if t.strip()] or ["(无标签)"]
        else:  # merchant
            keys = [(note or "").strip() or "(无备注)"]
        for k in keys:
            if needle and needle not in k.lower():
                continue
            buckets[k] += net
            counts[k] += 1

    total = sum(buckets.values())
    ranked = sorted(buckets.items(), key=lambda kv: -kv[1])[:limit]
    return {
        "ledger": ext,
        "base_currency": base_currency,
        "period": {"label": label, "scope": scope},
        "by": by,
        "total": round(total, 2),
        "items": [
            {
                "name": name,
                "total": round(amount, 2),
                "count": counts[name],
                "percent": round(amount / total * 100, 1) if total else 0.0,
            }
            for name, amount in ranked
        ],
        "_note": (
            "Amounts are net of consumption tax, matching the pie chart. "
            "For `merchant`, the merchant name is whatever you wrote in the note."
        ),
    }


def get_spending_pattern(
    user: User, *, scope: str = "year", period: str | None = None,
    ledger_id: str | None = None,
) -> dict[str, Any]:
    """消费习惯画像:按星期几 / 时段 / 金额区间的分布。

    「我周末花得多吗」「习惯晚上消费吗」「小额高频还是大额低频」—— 这类问题
    现有工具一个都答不了。

    Args:
        scope: `month` | `year` | `all`。
        period: 期间。
        ledger_id: 可选。
    """
    with SessionLocal() as db:
        led = _resolve_ledger(db, user.id, ledger_id)
        if led is None:
            return {"error": "No ledger found"}
        internal_id, base_currency, ext = led.id, led.currency, led.external_id
    start, end, label = period_range(scope, period)

    with SessionLocal() as db:
        rows = _rows_between(db, internal_id, start, end, tx_type="expense")

    weekday: dict[int, list[float | int]] = defaultdict(lambda: [0.0, 0])
    hour: dict[str, list[float | int]] = defaultdict(lambda: [0.0, 0])
    # 金额分段只统计笔数,固定 5 段
    band_names = ("0-100", "100-500", "500-2000", "2000-10000", "10000+")
    band_counts = [0, 0, 0, 0, 0]
    total = 0.0
    for _t, amt, raw, tax, ts, *_ in rows:
        v = float(amt or 0.0)
        net = v - tax_in_base_currency(tax, raw, v)
        total += net
        local = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        # 0=周一 … 6=周日
        weekday[local.weekday()][0] += net
        weekday[local.weekday()][1] += 1
        bucket = "22-24" if local.hour >= 22 else f"{local.hour:02d}-{local.hour + 1:02d}"
        hour[bucket][0] += net
        hour[bucket][1] += 1
        if net < 100:
            i = 0
        elif net < 500:
            i = 1
        elif net < 2000:
            i = 2
        elif net < 10000:
            i = 3
        else:
            i = 4
        band_counts[i] += 1

    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return {
        "ledger": ext,
        "base_currency": base_currency,
        "period": {"label": label, "scope": scope},
        "total_expense": round(total, 2),
        "by_weekday": [
            {"weekday": names[d], "total": round(v[0], 2), "count": v[1]}
            for d, v in sorted(weekday.items())
        ],
        "by_hour": [
            {"hour_range": h, "total": round(v[0], 2), "count": v[1]}
            for h, v in sorted(hour.items())
        ],
        "by_amount_band": [
            {"band": name, "count": c,
             "share": round(c / len(rows) * 100, 1) if rows else 0.0}
            for name, c in zip(band_names, band_counts, strict=True)
        ],
        "_note": "Weekday and hour use UTC to match how the rest of the app buckets time.",
    }
