"""read.py 的共享层。

原 src/routers/read.py 1705 行,按路由组拆成 3 个子模块后,各 endpoint 都
依赖的 imports + helper(_resolve_ledger_name / _owner_map_for_ledgers /
_get_latest_change_id / snapshot_cache 相关 / Flutter ↔ server 字段转换 等)
集中在这里。

子模块各自关注一类资源:
  - ledgers.py    /ledgers / /ledgers/{id}/*      账本维度读
  - workspace.py  /workspace/*                    跨账本聚合读
  - summary.py    /summary                        单独小端点

修改某条 read 端点的具体查询,进对应子模块;修改共享字段映射 / 账本可见性
/ projection 读辅助,改 _shared.py 一处。
"""

import json
import math
from datetime import datetime, timedelta, timezone
from collections.abc import Sequence
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.orm import Session

from ...config import get_settings
from ...database import get_db
from ...deps import get_current_user, require_any_scopes, require_scopes
from ...ledger_access import (
    get_accessible_ledger_by_external_id,
)
from ...models import (
    AttachmentFile,
    Ledger,
    LedgerMember,
    ReadBudgetProjection,
    ReadTxProjection,
    ReadTxSplitProjection,
    SyncChange,
    User,
    UserAccountProjection,
    UserCategoryProjection,
    UserProfile,
    UserTagProjection,
)
from ...schemas import (
    AnalyticsMetric,
    AnalyticsScope,
    ReadAccountOut,
    ReadBudgetOut,
    ReadBudgetUsageItemOut,
    ReadBudgetUsageOut,
    ReadCategoryOut,
    ReadLedgerDetailOut,
    ReadLedgerOut,
    ReadSummaryOut,
    ReadTagOut,
    ReadTransactionOut,
    TxSplit,
    WorkspaceAccountOut,
    WorkspaceAnalyticsAnomalyAttributionOut,
    WorkspaceAnalyticsAnomalyMonthOut,
    WorkspaceAnalyticsCategoryRankOut,
    WorkspaceAnalyticsOut,
    WorkspaceAnalyticsRangeOut,
    WorkspaceAnalyticsSeriesItemOut,
    WorkspaceAnalyticsSummaryOut,
    WorkspaceCategoryOut,
    WorkspaceLedgerCountsOut,
    WorkspaceTagOut,
    WorkspaceTransactionOut,
    WorkspaceTransactionPageOut,
)
from ...security import SCOPE_APP_WRITE, SCOPE_WEB_READ
from ... import snapshot_cache

router = APIRouter()
settings = get_settings()
_READ_SCOPE_DEP = (
    require_any_scopes(SCOPE_WEB_READ, SCOPE_APP_WRITE)
    if settings.allow_app_rw_scopes
    else require_scopes(SCOPE_WEB_READ)
)


def _is_admin(current_user: User) -> bool:
    """单用户隔离模型下,read 路由永远按 current_user 过滤 —— admin 角色只
    作用于 /admin/* 管理面板(用户列表、备份、日志等),不给读账本/交易/分类/
    标签/账户开"看所有用户数据"的后门。之前 admin 用户注册成第一个账号会
    自动被提升为 admin(见 alembic 0007_admin_bootstrap),结果 User B 登录
    就看到 User A 所有账本 —— 单用户自部署场景下这是 bug,不是 feature。
    """
    _ = current_user
    return False


def _require_ledger(
    db: Session,
    *,
    user_id: str,
    ledger_external_id: str,
    is_admin: bool,
) -> tuple[Ledger, None]:
    """Resolve a ledger for the caller.

    Returns ``(ledger, None)`` — the second slot used to hold a LedgerMember
    row and is retained for back-compat with callers that destructure.

    `is_admin` 参数保留兼容上游签名,但**不再触发跨用户旁路** —— 历史上 admin
    分支只按 external_id 查会随机命中第一行(Ledger.external_id 是
    (user_id, external_id) 复合唯一,不是全局唯一),导致 admin 通过 mobile
    访问时把自己的数据错挂到其他用户的同 external_id 账本。admin 跨用户访问
    需求应该通过专门的管理后台 endpoint(带显式 user_id 参数)实现。
    """
    del is_admin  # 历史参数,不再用作旁路
    row = get_accessible_ledger_by_external_id(
        db,
        user_id=user_id,
        ledger_external_id=ledger_external_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Ledger not found")
    ledger_row, _ = row
    if _is_ledger_deleted(db, ledger_id=ledger_row.id):
        raise HTTPException(status_code=404, detail="Ledger not found")
    return row


# ---------------------------------------------------------------------------
# Snapshot helpers — replaces all Web*Projection queries
# ---------------------------------------------------------------------------

def _get_latest_snapshot(db: Session, *, ledger_id: str) -> dict[str, Any] | None:
    """Return the parsed snapshot from the most recent ledger_snapshot SyncChange.

    payload_json 形状是 `{"content": "<json-string>", "metadata": {...}}`,
    content 是真正的 snapshot(ledgerName / items / accounts / categories / tags /
    budgets)。

    热路径:
    - 先只查该 ledger 的 `ledger_snapshot` 最大 change_id(很轻,命中索引,不读 blob)
    - 拿进程内 `snapshot_cache` 按 (ledger_id, change_id) 对账,命中直接返回 —
      跳过 3MB 行读 + 几十毫秒的 json.loads
    - 未命中才读 payload + parse + 回灌缓存

    命中率:单用户日常 dashboard 连环打 5-6 次 `/read/*`,首次 miss、其余全 hit,
    累计耗时从 ~250ms 降到 ~50ms(一次 parse 摊薄)。
    """
    latest_change_id_for_snapshot = db.scalar(
        select(func.max(SyncChange.change_id)).where(
            SyncChange.ledger_id == ledger_id,
            SyncChange.entity_type == "ledger_snapshot",
        )
    )
    if latest_change_id_for_snapshot is None:
        return None

    cached = snapshot_cache.get(ledger_id, int(latest_change_id_for_snapshot))
    if cached is not None:
        return cached

    row = db.scalar(
        select(SyncChange.payload_json)
        .where(
            SyncChange.ledger_id == ledger_id,
            SyncChange.entity_type == "ledger_snapshot",
            SyncChange.change_id == latest_change_id_for_snapshot,
        )
        .limit(1)
    )
    if row is None:
        return None
    if isinstance(row, str):
        row = json.loads(row)
    parsed: dict[str, Any] | None = None
    if isinstance(row, dict):
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                parsed = row  # fallback:把原 payload_json 返回,维持老行为
        else:
            parsed = row
    if parsed is not None:
        snapshot_cache.put(ledger_id, int(latest_change_id_for_snapshot), parsed)
    return parsed


def _get_latest_change_id(db: Session, *, ledger_id: str) -> int:
    val = db.scalar(
        select(func.max(SyncChange.change_id)).where(SyncChange.ledger_id == ledger_id)
    )
    return int(val or 0)


def _user_info_map(
    db: Session, user_ids: set[str]
) -> dict[str, tuple[str | None, str | None, str | None, int]]:
    """Return {user_id: (email, display_name, avatar_file_id, avatar_version)}
    给共享账本 tx 列表展示 "X 创建 · Y 编辑" 时回填创建者/编辑者头像 + name 用。

    avatar_file_id 是 server attachment id;web 拼成 `/api/v1/profile/avatar/...`
    访问。display_name 缺失时 caller fallback 到 email split('@')[0]。
    """
    if not user_ids:
        return {}
    users = db.execute(
        select(User.id, User.email).where(User.id.in_(user_ids))
    ).all()
    profiles = db.execute(
        select(
            UserProfile.user_id,
            UserProfile.display_name,
            UserProfile.avatar_file_id,
            UserProfile.avatar_version,
        ).where(UserProfile.user_id.in_(user_ids))
    ).all()
    email_by_uid = {row[0]: row[1] for row in users}
    profile_by_uid: dict[str, tuple[str | None, str | None, int]] = {
        row[0]: (row[1], row[2], int(row[3] or 0)) for row in profiles
    }
    out: dict[str, tuple[str | None, str | None, str | None, int]] = {}
    for uid in user_ids:
        email = email_by_uid.get(uid)
        prof = profile_by_uid.get(uid, (None, None, 0))
        out[uid] = (email, prof[0], prof[1], prof[2])
    return out


def _owner_map_for_ledgers(
    db: Session, ledgers: list[Ledger]
) -> dict[str, tuple[str, str | None]]:
    """Return {ledger_external_id: (user_id, email)} for the given ledgers.
    Single-user-per-ledger: every entity in a ledger was created by its owner,
    so this is the right attribution for the web tables.
    """
    user_ids = {lg.user_id for lg in ledgers}
    if not user_ids:
        return {}
    rows = db.execute(
        select(User.id, User.email).where(User.id.in_(user_ids))
    ).all()
    email_by_uid = {row[0]: row[1] for row in rows}
    return {
        lg.external_id: (lg.user_id, email_by_uid.get(lg.user_id))
        for lg in ledgers
    }


def _is_ledger_deleted(db: Session, *, ledger_id: str) -> bool:
    """True iff the latest ledger_snapshot sync change for this ledger is a
    tombstone (``action='delete'``). Used to filter / 404 deleted ledgers in
    read endpoints without dropping the underlying rows (we keep the audit
    trail under the soft-delete model)."""
    latest_action = db.scalar(
        select(SyncChange.action)
        .where(
            SyncChange.ledger_id == ledger_id,
            SyncChange.entity_type == "ledger_snapshot",
        )
        .order_by(SyncChange.change_id.desc())
        .limit(1)
    )
    return latest_action == "delete"


def _visible_workspace_ledgers(
    db: Session,
    *,
    current_user: User,
    is_admin: bool,
    ledger_id: str | None = None,
    user_id: str | None = None,
) -> list[Ledger]:
    """workspace.py 跨账本聚合端点共用的账本可见性解析。

    统一三件事(此前 7 个端点各自 copy-paste 一份 ledger_conditions):
      - ``ledger_id``(external_id)限定单账本;
      - admin + ``user_id`` 限定某用户;非 admin 走 ``LedgerMember``;
      - **排除软删账本**(issue #31):软删账本被写入"复活"出来的交易不应再出现
        在跨账本 / tag / 统计视图里 —— 跟 ``/read/ledgers`` 的 `_is_ledger_deleted`
        过滤口径保持一致(此前 workspace 不过滤,导致"tag 里看得到、账本列表里
        却没有"的幽灵交易)。
    """
    conditions: list[Any] = []
    if ledger_id:
        conditions.append(Ledger.external_id == ledger_id)
    if is_admin:
        if user_id:
            conditions.append(Ledger.user_id == user_id)
    else:
        # 共享账本:走 LedgerMember 维度,Editor 也能看到 Owner 的账本数据。
        conditions.append(
            Ledger.id.in_(
                select(LedgerMember.ledger_id).where(
                    LedgerMember.user_id == current_user.id
                )
            )
        )
    ledgers = list(
        db.execute(
            select(Ledger).where(and_(*conditions) if conditions else true())
        ).scalars().all()
    )
    return [lg for lg in ledgers if not _is_ledger_deleted(db, ledger_id=lg.id)]


def _snapshot_ledger_info(
    snapshot: dict[str, Any] | None,
    *,
    ledger: Ledger,
) -> tuple[str, str]:
    """Return (ledger_name, currency) from a snapshot, with fallbacks."""
    if snapshot:
        name = (snapshot.get("ledgerName") or "").strip()
        currency = (snapshot.get("currency") or "").strip()
    else:
        name = ""
        currency = ""
    if not name:
        name = (ledger.name or ledger.external_id).strip() or ledger.external_id
    if not currency:
        currency = "CNY"
    return name, currency


def _resolve_ledger_name(db: Session, *, ledger: Ledger) -> str:
    """Ledger.name 是权威源。边缘 case(老数据 name 为 NULL,0020 迁移错过,
    或 mobile 没 push ledger entity 过):回退到 sync_changes,并自愈写回 Ledger.name。
    自愈写入用独立 commit —— 读 endpoint 默认不提交,我们显式提交以持久化。
    """
    if ledger.name and ledger.name.strip():
        return ledger.name.strip()

    resolved: str | None = None

    # Fallback 1:最近一条 ledger entity SyncChange 的 ledgerName
    recent_ledger = db.scalar(
        select(SyncChange.payload_json).where(
            SyncChange.ledger_id == ledger.id,
            SyncChange.entity_type == "ledger",
            SyncChange.action == "upsert",
        ).order_by(SyncChange.change_id.desc()).limit(1)
    )
    if recent_ledger is not None:
        payload = recent_ledger
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict):
            name = (payload.get("ledgerName") or "").strip()
            if name:
                resolved = name

    # Fallback 2:历史 ledger_snapshot 的 content.ledgerName(Plan B 前的数据)
    if resolved is None:
        recent_snap = db.scalar(
            select(SyncChange.payload_json).where(
                SyncChange.ledger_id == ledger.id,
                SyncChange.entity_type == "ledger_snapshot",
                SyncChange.action == "upsert",
            ).order_by(SyncChange.change_id.desc()).limit(1)
        )
        if recent_snap is not None:
            payload = recent_snap
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = None
            if isinstance(payload, dict):
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    try:
                        snap = json.loads(content)
                        if isinstance(snap, dict):
                            name = (snap.get("ledgerName") or "").strip()
                            if name:
                                resolved = name
                    except json.JSONDecodeError:
                        pass

    if resolved is None:
        return ledger.external_id

    # 自愈持久化:显式 commit 让下次 read 直接命中 Ledger.name 快路径
    try:
        ledger.name = resolved[:255]
        db.commit()
    except Exception:  # noqa: BLE001 — 自愈失败不影响读本次返回
        db.rollback()
    return resolved


def _load_owner_identity(db: Session, *, ledger: Ledger) -> tuple[str, str | None, str | None, str | None, int]:
    """Return (user_id, email, display_name, avatar_url, avatar_version) for
    the ledger owner. Single-user-per-ledger model: every row in the ledger
    was created by the owner."""
    row = db.execute(
        select(
            User.id,
            User.email,
            UserProfile.display_name,
            UserProfile.avatar_file_id,
            UserProfile.avatar_version,
        )
        .join(UserProfile, UserProfile.user_id == User.id, isouter=True)
        .where(User.id == ledger.user_id)
    ).first()
    if row is None:
        return ledger.user_id, None, None, None, 0
    return row[0], row[1], row[2], (
        f"/api/v1/attachments/{row[3]}" if row[3] else None
    ), int(row[4] or 0)


def _tags_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def account_balance_stats(
    db: Session, ledger_internal_ids: Sequence[str]
) -> dict[str, dict[str, float | int]]:
    """按 `account_sync_id` 聚合账户的收支/转账/笔数 → 余额变动。

    返回 `{account_sync_id: {"count", "income", "expense", "balance"}}`,
    其中 `balance` 是**变动额**(不含期初),调用方自己加 `initial_balance`。

    ## 为什么抽出来

    这段 SQL 之前在仓库里有**两份逐字重复**的实现:

    - `read/workspace.py` 的 `list_workspace_accounts`
    - `mcp/tools/entity_tools.py` 的 `get_account_balance`(我加的,注释里
      还写「口径与 server 的 /read/workspace/accounts 逐字对应」)

    两处互相担保、却各自演化。以后要改余额语义(比如加组合支付拆分),
    漏改一处的表现是「账户页对、净值曲线错」或「LLM 说的余额和 Web 显示的
    不一致」—— 而两边的单测各自都绿(它们测的是不同端点)。所以收敛到一处。

    ## 口径(两处原本就必须一致的东西)

    - 金额读 **`amount` 原币**,不折本位币 —— 与 `_projection_totals` 的
      账本维度口径**故意不同**,那里折本位币,这里不折(账户是原币余额)。
    - 跨账本聚合:`ledger_id IN (...)`,按 `account_sync_id` 分组。
      不能按 `(ledger, account)` 分桶,否则后面的 dedup 会跟 tx 聚合对不上。
    - 转账对两端分别计:`from_account` 减、`to_account` 加。
    """
    from sqlalchemy import case as sa_case

    if not ledger_internal_ids:
        return {}

    stats: dict[str, dict[str, float | int]] = {}

    def _bucket(acc: str) -> dict[str, float | int]:
        return stats.setdefault(
            acc, {"count": 0, "income": 0.0, "expense": 0.0, "balance": 0.0}
        )

    for acc, cnt, inc, exp in db.execute(
        select(
            ReadTxProjection.account_sync_id,
            func.count().label("cnt"),
            func.coalesce(func.sum(sa_case(
                (ReadTxProjection.tx_type == "income", ReadTxProjection.amount),
                else_=0.0)), 0.0).label("income"),
            func.coalesce(func.sum(sa_case(
                (ReadTxProjection.tx_type == "expense", ReadTxProjection.amount),
                else_=0.0)), 0.0).label("expense"),
        ).where(
            ReadTxProjection.ledger_id.in_(ledger_internal_ids),
            ReadTxProjection.account_sync_id.is_not(None),
            ReadTxProjection.tx_type.in_(["income", "expense"]),
        ).group_by(ReadTxProjection.account_sync_id)
    ).all():
        stats[acc] = {
            "count": int(cnt), "income": float(inc),
            "expense": float(exp), "balance": float(inc) - float(exp),
        }

    for acc, cnt, amt in _transfer_legs(
        db, ledger_internal_ids, ReadTxProjection.from_account_sync_id
    ):
        b = _bucket(acc)
        b["count"] = int(b["count"]) + int(cnt)
        b["balance"] = float(b["balance"]) - float(amt)

    for acc, cnt, amt in _transfer_legs(
        db, ledger_internal_ids, ReadTxProjection.to_account_sync_id
    ):
        b = _bucket(acc)
        b["count"] = int(b["count"]) + int(cnt)
        b["balance"] = float(b["balance"]) + float(amt)

    # 组合支付(0021):拆分腿。
    #
    # 父交易**不参与**这笔扣款(mutator 在有腿时强制清空了它的 account_sync_id,
    # 所以上面那个 `account_sync_id IS NOT NULL` 的查询自然就漏过了它)。
    # 每条腿各扣自己那个账户 —— 这正是「一笔订单两个支付方式」要的效果。
    #
    # `count` 也 +1:否则 `HomeTopAccounts` 按 `count > 0` 过滤时,纯靠卡支付的
    # 组合支付订单涉及的账户会**整块消失**(不是算错,是不显示,最难测)。
    for acc, cnt, amt in _split_legs(db, ledger_internal_ids):
        b = _bucket(acc)
        b["count"] = int(b["count"]) + int(cnt)
        b["expense"] = float(b["expense"]) + float(amt)
        b["balance"] = float(b["balance"]) - float(amt)

    return stats


def load_tx_splits(
    db: Session, ledger_id: str, tx_sync_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """批量读一批交易的拆分腿(0021) → `{tx_sync_id: [腿, ...]}`。

    **一次查询 + 分组**,不按行查 —— 交易列表一页 20 条,逐行查就是 N+1。

    只返回有腿的交易;调用方 `splits_by_tx.get(row.sync_id)` 拿不到就是无腿。
    """
    if not tx_sync_ids:
        return {}
    from ...models import ReadTxSplitProjection

    out: dict[str, list[dict[str, Any]]] = {}
    for r in db.execute(
        select(
            ReadTxSplitProjection.tx_sync_id,
            ReadTxSplitProjection.account_sync_id,
            ReadTxSplitProjection.amount,
        )
        .where(
            ReadTxSplitProjection.ledger_id == ledger_id,
            ReadTxSplitProjection.tx_sync_id.in_(list(tx_sync_ids)),
        )
        .order_by(
            ReadTxSplitProjection.tx_sync_id.asc(),
            ReadTxSplitProjection.seq.asc(),
        )
    ).all():
        out.setdefault(r[0], []).append({"account_id": r[1], "amount": float(r[2])})
    return out


def _split_legs(db: Session, ledger_internal_ids: Sequence[str]):
    """组合支付的腿按账户聚合 (account_sync_id, count, amount)。"""
    return db.execute(
        select(
            ReadTxSplitProjection.account_sync_id,
            func.count().label("cnt"),
            func.coalesce(func.sum(ReadTxSplitProjection.amount), 0.0).label("amt"),
        ).where(
            ReadTxSplitProjection.ledger_id.in_(ledger_internal_ids),
        ).group_by(ReadTxSplitProjection.account_sync_id)
    ).all()


def _transfer_legs(db: Session, ledger_internal_ids: Sequence[str], column: Any):
    """转账某一端(转出/转入)按账户聚合的 (sync_id, count, amount)。"""
    return db.execute(
        select(
            column,
            func.count().label("cnt"),
            func.coalesce(func.sum(ReadTxProjection.amount), 0.0).label("amt"),
        ).where(
            ReadTxProjection.ledger_id.in_(ledger_internal_ids),
            ReadTxProjection.tx_type == "transfer",
            column.is_not(None),
        ).group_by(column)
    ).all()


def account_balance_from_stats(
    account_sync_id: str,
    initial_balance: float,
    stats: dict[str, dict[str, float | int]] | None,
) -> float:
    """期初 + 变动 → 余额。`stats` 没有该账户时返回期初。"""
    row = (stats or {}).get(account_sync_id)
    if not row:
        return initial_balance
    return initial_balance + float(row["balance"])


def account_balance_delta(
    tx_type: str | None,
    amount: float,
    account_sync_id: str | None,
    from_account_sync_id: str | None = None,
    to_account_sync_id: str | None = None,
    splits: list[dict[str, Any]] | None = None,
) -> dict[str, float]:
    """一笔交易对**各个账户**余额的影响 -> `{account_sync_id: delta}`。

    这是 `account_balance_stats` 的「逐笔版本」。两者**必须同口径** ——
    前者给账户列表 / MCP 做整体聚合,后者给净值历史按时间逐笔推进。

    口径:
    - income   -> 主账户 +
    - expense  -> 主账户 -
    - transfer -> 转出账户 -,转入账户 +(转出缺失时退回主账户)
    - 其它 tx_type -> 无影响

    不判断账户是否存在(调用方按自己的集合过滤)。净值历史原本对未知账户
    静默丢弃,这里保持这个行为 —— delta 算全,让调用方决定丢不丢。
    """
    if not amount:
        return {}
    out: dict[str, float] = {}
    if splits is not None:
        # 组合支付(0021):有腿时父交易的 account_sync_id 已被 mutator 强制清空,
        # 所以这里按腿分摊。`sum(splits)` == amount(mutter 层校验过)。
        for leg in splits:
            sid = str(leg.get("accountId") or leg.get("account_id") or "").strip()
            if not sid:
                continue
            out[sid] = out.get(sid, 0.0) - float(leg.get("amount") or 0.0)
        return out
    if tx_type == "income":
        if account_sync_id:
            out[account_sync_id] = out.get(account_sync_id, 0.0) + amount
    elif tx_type == "expense":
        if account_sync_id:
            out[account_sync_id] = out.get(account_sync_id, 0.0) - amount
    elif tx_type == "transfer":
        src = from_account_sync_id or account_sync_id
        dst = to_account_sync_id
        if src:
            out[src] = out.get(src, 0.0) - amount
        if dst:
            out[dst] = out.get(dst, 0.0) + amount
    return out


def _projection_totals(
    db: Session, ledger_internal_id: str
) -> tuple[int, float, float, float, datetime | None]:
    """从 read_tx_projection 聚合出 (count, income_total, expense_total, latest)。
    SQLite / PostgreSQL 通用:用 SQLAlchemy 的 `case` 做条件 sum。

    账本维度口径(交易级多币种,0018):折账本本位币,读 native_amount,
    NULL(旧 App 推的 / 存量)回退 amount。单币种账本 native==amount,结果不变。
    账户维度(workspace/accounts 按 account_sync_id 聚合)仍读 amount 原币,不要仿此改。

    收支 SUM 排除 exclude_from_stats=True 的标记笔(#340 D1,补 0017 的遗漏:
    此前只有 analytics 过滤了,账本卡片没过滤 → 两处统计对不上)。tx_count /
    latest 不过滤 —— D1 语义:标记只排收支金额,仍计入账单列表与笔数。

    返回 (count, income_ex, expense_ex, balance_all, latest):balance_all 是
    **不排除**标记笔的收支差 —— 「余额=钱的位置」必须含标记笔(D5,与 App
    getLedgerStats 口径一致),否则跨端余额对不上。"""
    from sqlalchemy import case as sa_case
    from sqlalchemy import false as sa_false

    _native = func.coalesce(ReadTxProjection.native_amount, ReadTxProjection.amount)
    _counted = ReadTxProjection.exclude_from_stats == sa_false()
    row = db.execute(
        select(
            func.count(ReadTxProjection.sync_id),
            func.coalesce(func.sum(
                sa_case(((ReadTxProjection.tx_type == "income") & _counted, _native),
                        else_=0.0)
            ), 0.0),
            func.coalesce(func.sum(
                sa_case(((ReadTxProjection.tx_type == "expense") & _counted, _native),
                        else_=0.0)
            ), 0.0),
            # balance_all:不排除标记笔(D5「余额=钱的位置」)
            func.coalesce(func.sum(
                sa_case(
                    (ReadTxProjection.tx_type == "income", _native),
                    (ReadTxProjection.tx_type == "expense", -_native),
                    else_=0.0,
                )
            ), 0.0),
            func.max(ReadTxProjection.happened_at),
        ).where(ReadTxProjection.ledger_id == ledger_internal_id)
    ).one()
    tx_count, income_total, expense_total, balance_all, latest_raw = row
    return (
        int(tx_count or 0),
        float(income_total or 0),
        float(expense_total or 0),
        float(balance_all or 0),
        _to_utc(latest_raw) if latest_raw else None,
    )


def _clamp_month_start_day(value: int | None) -> int:
    """月度起始日统一钳到 [1, 28](2 月安全上限);None/0 → 1(自然月)。"""
    return max(1, min(28, value or 1))


def _bucket_key(
    scope: AnalyticsScope,
    happened_at: datetime,
    tz_offset_minutes: int = 0,
    month_start_day: int = 1,
) -> str:
    """按用户本地时区把 happened_at 折成 month-bucket(YYYY-MM-DD)或 year-bucket(YYYY-MM)。

    跟 `_analytics_range` 同一原因 —— UTC 会把 CST `2026-04-16 00:00` 当成 UTC
    `2026-04-15 16:00`,落到 4/15 桶里;日历 / analytics 跟用户感知错位一天。
    `tz_offset_minutes` 跟 `_analytics_range` 同符号(JavaScript
    `-new Date().getTimezoneOffset()`),CST 传 +480。默认 0 走 UTC,跟老客户端
    行为保持一致。

    `month_start_day`(1-28):year scope 按「周期标签月」分桶 —— 本地日期
    day >= start_day 归当月,否则归上月(周期按起始月命名)。默认 1 = 自然月,
    老调用行为不变。
    """
    from datetime import timedelta

    normalized = _to_utc(happened_at)
    # 偏移到用户本地时区(naive 化),strftime 就是本地日期
    local = normalized + timedelta(minutes=tz_offset_minutes)
    if scope == "month":
        return local.strftime("%Y-%m-%d")
    day = _clamp_month_start_day(month_start_day)
    if local.day >= day:
        return local.strftime("%Y-%m")
    prev = local.replace(day=1) - timedelta(days=1)
    return prev.strftime("%Y-%m")


def _analytics_range(
    *,
    scope: AnalyticsScope,
    period: str | None,
    tz_offset_minutes: int = 0,
    month_start_day: int = 1,
) -> tuple[datetime | None, datetime | None, str | None]:
    """计算 analytics 的 [start, end) UTC 范围。

    边界对应用户**本地**的月份/年份,不是 UTC —— UTC 计算会把 CST "2026-04-01 00:00"
    当成 UTC "2026-03-31 16:00",落到 3 月,导致月初那笔支出没算进 4 月。

    `tz_offset_minutes`:客户端传的本地时区偏移(正数 = 东半球),与 JavaScript
    `-new Date().getTimezoneOffset()` 同符号。中国是 +480。默认 0 = UTC(老客户端不传
    这个参数时的行为保持 UTC 切月)。

    `month_start_day`(1-28):周期按起始月命名 —— "2026-06"(msd=10) 表示本地
    2026-06-10 ~ 2026-07-10。year scope 同理:[当年1月周期起点, 次年1月周期起点)。
    默认 1 = 自然月/年,所有行为与改造前逐位一致。
    """
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    tz = timezone(timedelta(minutes=tz_offset_minutes))

    if scope == "all":
        return None, None, None

    if scope == "month":
        day = _clamp_month_start_day(month_start_day)
        if isinstance(period, str) and period.strip():
            target = period.strip()
        else:
            # 默认 = 「当前周期」标签:今天还没到起始日时属上个标签月
            local_now = now.astimezone(tz)
            if local_now.day < day:
                local_now = local_now.replace(day=1) - timedelta(days=1)
            target = local_now.strftime("%Y-%m")
        try:
            year_part, month_part = target.split("-", 1)
            year = int(year_part)
            month = int(month_part)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Invalid analytics period") from exc
        if month < 1 or month > 12:
            raise HTTPException(status_code=400, detail="Invalid analytics period")
        # 本地时区周期起点/终点 → 转 UTC 给 SQL 用;周期按起始月命名
        start_local = datetime(year, month, day, tzinfo=tz)
        end_local = (
            datetime(year + 1, 1, day, tzinfo=tz)
            if month == 12
            else datetime(year, month + 1, day, tzinfo=tz)
        )
        return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), f"{year:04d}-{month:02d}"

    day = _clamp_month_start_day(month_start_day)
    if isinstance(period, str) and period.strip():
        target = period.strip()
    else:
        local_now = now.astimezone(tz)
        target_year = local_now.year
        # 只有 1 月里还没到起始日时才归上一年度周期;2-12 月无论 day 与 msd
        # 关系如何,年标签都是当前日历年(勿仿 month scope 把借位推广到全月)。
        if local_now.month == 1 and local_now.day < day:
            target_year -= 1
        target = str(target_year)
    try:
        year = int(target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid analytics period") from exc
    start_local = datetime(year, 1, day, tzinfo=tz)
    end_local = datetime(year + 1, 1, day, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), f"{year:04d}"


# ---------------------------------------------------------------------------
# CSV 导出辅助
# ---------------------------------------------------------------------------

import re as _re

_CSV_NEEDS_QUOTE = (",", '"', "\n", "\r")
_FILENAME_BAD = _re.compile(r'[\\/:*?"<>|\r\n]')


def _csv_field(value: Any) -> str:
    """RFC 4180 字段转义。None / "" → 空;含 , " \\n \\r → 双引号包裹 + 转义。"""
    if value is None:
        return ""
    s = str(value)
    if s == "":
        return ""
    if any(c in s for c in _CSV_NEEDS_QUOTE):
        return '"' + s.replace('"', '""') + '"'
    return s


def _sanitize_filename(name: str | None, max_len: int = 64) -> str:
    """文件系统安全的文件名片段。Windows 上 / \\ : * ? \" < > | 都禁;两端空格点也清。"""
    safe = _FILENAME_BAD.sub("_", (name or "").strip()) or "ledger"
    safe = safe.strip(" .") or "ledger"
    return safe[:max_len]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


__all__ = [
    'json',
    'datetime',
    'timedelta',
    'timezone',
    'Any',
    'cast',
    'APIRouter',
    'Depends',
    'HTTPException',
    'Query',
    'and_',
    'func',
    'or_',
    'select',
    'true',
    'Session',
    'get_settings',
    'get_db',
    'get_current_user',
    'require_any_scopes',
    'require_scopes',
    'get_accessible_ledger_by_external_id',
    'AttachmentFile',
    'Ledger',
    'LedgerMember',
    'ReadBudgetProjection',
    'ReadTxProjection',
    'UserAccountProjection',
    'UserCategoryProjection',
    'UserTagProjection',
    'SyncChange',
    'User',
    'UserProfile',
    'AnalyticsMetric',
    'AnalyticsScope',
    'ReadAccountOut',
    'ReadBudgetOut',
    'ReadBudgetUsageItemOut',
    'ReadBudgetUsageOut',
    'ReadCategoryOut',
    'ReadLedgerDetailOut',
    'ReadLedgerOut',
    'ReadSummaryOut',
    'ReadTagOut',
    'ReadTransactionOut',
    'TxSplit',
    'WorkspaceAccountOut',
    'WorkspaceAnalyticsAnomalyAttributionOut',
    'WorkspaceAnalyticsAnomalyMonthOut',
    'WorkspaceAnalyticsCategoryRankOut',
    'WorkspaceAnalyticsOut',
    'WorkspaceAnalyticsRangeOut',
    'WorkspaceAnalyticsSeriesItemOut',
    'WorkspaceAnalyticsSummaryOut',
    'WorkspaceCategoryOut',
    'WorkspaceLedgerCountsOut',
    'WorkspaceTagOut',
    'WorkspaceTransactionOut',
    'WorkspaceTransactionPageOut',
    'SCOPE_APP_WRITE',
    'SCOPE_WEB_READ',
    'snapshot_cache',
    'router',
    'settings',
    '_READ_SCOPE_DEP',
    '_is_admin',
    '_require_ledger',
    '_get_latest_snapshot',
    '_get_latest_change_id',
    '_owner_map_for_ledgers',
    '_user_info_map',
    '_is_ledger_deleted',
    # 消费税(0020):原币税额 → 折本位币税额。workspace.py 与 MCP 的
    # get_analytics_summary 共用同一实现,否则两边口径会打架。
    'tax_in_base_currency',
    '_visible_workspace_ledgers',
    '_snapshot_ledger_info',
    '_resolve_ledger_name',
    '_load_owner_identity',
    '_tags_list',
    '_to_utc',
    'account_balance_delta',
    'load_tx_splits',
    'account_balance_from_stats',
    'account_balance_stats',
    '_projection_totals',
    '_bucket_key',
    '_analytics_range',
    '_csv_field',
    '_sanitize_filename',
]


def tax_in_base_currency(
    tax_amount: object, raw_amount: object, base_amount: float
) -> float:
    """原币税额 → 折账本本位币的税额(0020)。

    **比例法**:`tax / amount` 的比率与币种无关,用该笔自身的隐含汇率换算,
    保证税额和主金额走**同一个汇率**、不会各自漂移。汇率快照本身已经存在
    `native_amount` 里,这里只是复用,不额外存一份折算后的税额 ——
    存两份就等于复制 native_amount 当年「改了 amount 忘了改折算值」的坑。

    - `tax_amount` 为 NULL(免税 / 未记录)→ 0
    - `base_amount <= 0`(金额非正)→ 0,不产生税切片
    - `raw_amount <= 0` 推不出比率 → 退化 1:1 并夹到 base_amount,与
      native_amount 既有退化规则同口径

    结果永远夹在 `[0, base_amount]` 内,保证「分类税前 + 税切片 = 实付」
    这条不变式在任何数据下都成立,不会因为脏数据算出负税额。
    """
    if tax_amount is None:
        return 0.0
    try:
        tax = float(tax_amount)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # NaN / inf 必须显式挡掉:它们和任何数比较都返回 False,`tax <= 0` 这种
    # 守卫会**静默放行**,然后污染出 NaN 金额并一路传染到 JSON 响应
    # (FastAPI 序列化 NaN 会变成非法 JSON)。靠比较的偶然顺序兜底不可靠。
    if not math.isfinite(tax) or tax <= 0:
        return 0.0
    # amount 非正 → 不产生税切片。全链路只有 budget 有 >0 校验,交易金额
    # 本身不校验正负;web write 走 mutator 会拒(快照校验),但 /sync/push
    # 不经过 mutator,脏数据能进来。这里守住,免得负金额算出负税额。
    try:
        base = float(base_amount or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(base) or base <= 0:
        return 0.0
    try:
        raw = float(raw_amount or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raw = 0.0
    if not math.isfinite(raw) or raw <= 0:
        return min(tax, base)
    return min(base, max(0.0, base * (tax / raw)))
