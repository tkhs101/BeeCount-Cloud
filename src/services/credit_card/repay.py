"""自动还款执行器(阶段 3)。

## 四道防线,缺一道就可能重复扣钱

| # | 防线 | 挡什么 |
|---|---|---|
| 1 | **账期查重**:算出的 `outstanding == 0` 就跳过 | 用户手动还过了 → 自动跳过(这一条本身就满足用户需求) |
| 2 | **`autorepay_last_period`**:本期已还过就跳过 | 同账期内又刷了一笔 → 不会还两次 |
| 3 | **`Idempotency-Key: auto-repay:{card}:{YYYY-MM}`** | 短时间重试(机器重启等) |
| 4 | **数据库条件更新抢锁** | 多进程重复执行 |

第 3 道**不能**作为唯一保障:`SyncPushIdempotency` 的 TTL 只有 24 小时
(`write/_shared.py:636`),关机两天后补跑 → key 已 purge → 重复写入。
第 1、2 道是业务判据,跨重启、跨补跑窗口都有效。

第 4 道是必须的:`get_scheduler()` 是**进程内单例**
(`backup/scheduler.py:43-48`),内存 jobstore 无跨进程锁。当前部署单 worker
安全,但 `database.py:16-18` 注释明说作者预期过多 worker。加上条件更新
之后,即使将来有人加 `--workers 4`,每个账期也只会有一个进程真正执行。

## 写入路径:self-call,不是直接操作 DB

`mcp/tools/write_tools.py:71-92` + `_mcp_internal_client.py:33-40` 是仓库里
唯一的「非用户 HTTP 触发,但完整走 `_commit_write` 全套」先例。

直接操作 DB 就要重实现 `write/_shared.py:532-678` 的:tx_id 生成、
`snapshot_mutator` 字段规范化、`projection.upsert_tx`、`AuditLog`、
`broadcast_to_ledger`。漏任何一步都命中 CLAUDE.md 记的静默丢失坑
(税额被抹 / splits 被删 / nativeAmount 变 NULL),且不报错。

**必须用专用 `device_id`**:`sync/pull.py:78-79` 会过滤掉**同设备**的变更。
冒用 `web-console` 的话用户在自己页面上根本看不见自动还款。

## 部分还款与「不得扣成负数」

用户拍板「余额不足时部分还款」,但**必须加一条保护**:信用卡还款把储蓄卡
扣成负数 = 新增一笔负债,风险翻倍。

```
可还 = min(outstanding, max(0, 扣款账户余额))
可还 <= 0 → 跳过
```
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from ...models import UserAccountProjection
from .billing import is_due_on, period_for_payment_due_date
from .config import AutoRepayConfigError, CardConfig, load_card_config, validate_autorepay_config
from .statement import compute_statement

logger = logging.getLogger("beecount.credit_autorepay")

#: 自动任务专用 device_id。**不能**用 `web-console` —— `sync/pull.py:78-79`
#: 会过滤同设备的变更,用户在页面上就看不到自动还款产生的交易。
AUTO_REPAY_DEVICE_ID = "auto-repay"


@dataclass(frozen=True)
class RepayOutcome:
    """一张卡的执行结果。**所有**分支都必须返回一个 outcome**,
    不能静默 return —— 静默就是「用户以为还了,其实没还」。"""

    card_sync_id: str
    #: `skipped_no_schedule` 没配账期 / `skipped_not_due` 今天不是还款日 /
    #: `skipped_disabled` 没启用 / `skipped_already_repaid` 已还过 /
    #: `skipped_zero_outstanding` 无应还 / `skipped_no_funds` 扣款账户没钱 /
    #: `skipped_invalid_config` 配置非法 / `skipped_locked` 别的进程在跑
    #: `done` 成功(可能部分还款) / `failed` 异常
    status: str
    amount: float = 0.0
    period: str | None = None
    tx_sync_id: str | None = None
    detail: str = ""

    @property
    def repaid(self) -> bool:
        return self.status == "done" and self.amount > 0


def period_key(due_date: date) -> str:
    """账期键 `YYYY-MM`。

    用**还款日**而不是出账月 —— 还款日唯一确定一张账单,且对
    `billing_day == payment_due_day` 的情形稳定(出账日和还款日同月时,
    用出账月会让相邻两期的键撞车)。
    """
    return f"{due_date.year:04d}-{due_date.month:02d}"


def run_daily(db: Session, *, user_id: str, today: date,
              self_call: Any) -> list[RepayOutcome]:
    """跑一次「今天该还的全部卡」。

    `self_call` 是 MCP 那套进程内 HTTP 客户端(`_mcp_internal_client`),
    负责真正写交易。这样执行器**不碰 projection**,只算钱和做决策。
    """
    from sqlalchemy import select

    cards = db.scalars(
        select(UserAccountProjection)
        .where(UserAccountProjection.user_id == user_id)
        .where(UserAccountProjection.autorepay_enabled.is_(True))
    ).all()

    out: list[RepayOutcome] = []
    for row in cards:
        out.append(repay_one(
            db, user_id=user_id, card=row, today=today, self_call=self_call,
        ))
    return out


def repay_one(db: Session, *, user_id: str, card: UserAccountProjection,
              today: date, self_call: Any) -> RepayOutcome:
    card_id = card.sync_id
    try:
        return _repay_inner(db, user_id=user_id, card=card, today=today,
                            self_call=self_call)
    except AutoRepayConfigError as e:
        # 配置非法 → 跳过这张卡 + 可见的错误,**不让整个批次崩掉**
        logger.warning(
            "autorepay.skip card=%s invalid_config: %s", card_id, e)
        return RepayOutcome(card_id, "skipped_invalid_config", detail=str(e))
    except Exception as e:  # noqa: BLE001 - 一张卡失败不该拖垮整个批次
        db.rollback()
        logger.exception("autorepay.failed card=%s", card_id)
        return RepayOutcome(card_id, "failed", detail=f"{type(e).__name__}: {e}")


def _repay_inner(db: Session, *, user_id: str, card: UserAccountProjection,
                 today: date, self_call: Any) -> RepayOutcome:
    card_id = card.sync_id
    cfg: CardConfig | None = load_card_config(
        db, user_id=user_id, card_account_sync_id=card_id)
    if cfg is None:
        return RepayOutcome(card_id, "skipped_disabled", detail="账户不存在")
    if not cfg.enabled:
        return RepayOutcome(card_id, "skipped_disabled")
    if not cfg.is_due_configured or not cfg.from_account_sync_id:
        return RepayOutcome(card_id, "skipped_no_schedule", detail="未绑定扣款账户或账期")

    # 配置校验放在「今天是不是还款日」之前 —— 非法配置每天都该报一次可见的错,
    # 而不是等到还款日才暴露。
    validate_autorepay_config(db, user_id=user_id, card_account_sync_id=card_id,
                              from_account_sync_id=cfg.from_account_sync_id)

    if not is_due_on(today, int(cfg.payment_due_day or 0)):
        return RepayOutcome(card_id, "skipped_not_due")

    period = period_for_payment_due_date(
        today, billing_day=int(cfg.billing_day or 0),
        due_day=int(cfg.payment_due_day or 0))
    if period is None:
        # 孤儿还款日:该日不偿还任何账单(短月钳位所致)。
        return RepayOutcome(card_id, "skipped_not_due",
                            detail="该还款日不偿还账单(短月顺延所致)")

    pkey = period_key(today)

    # ---- 防线 2:`last_period` 恰好一次 ------------------------------------ #
    if cfg.last_period == pkey:
        return RepayOutcome(card_id, "skipped_already_repaid", period=pkey,
                            detail=f"{pkey} 已自动还过")

    # ---- 防线 1:账期查重(自愈,天然幂等) --------------------------------- #
    stmt = compute_statement(
        db, user_id=user_id, ledger_id=_ledger_of(db, user_id=user_id,
                                                   account_sync_id=card_id),
        card_account_sync_id=card_id, period=period, today=today)
    outstanding = stmt.outstanding
    if outstanding <= 0:
        return RepayOutcome(card_id, "skipped_zero_outstanding", period=pkey,
                            detail="无应还(可能已手动还清)")

    # ---- 部分还款:不得把扣款账户扣成负数 -------------------------------- #
    source_balance = _balance_of(db, user_id=user_id,
                                 account_sync_id=cfg.from_account_sync_id)
    if source_balance <= 0:
        return RepayOutcome(card_id, "skipped_no_funds", period=pkey,
                            detail="扣款账户余额不足")
    payable = min(outstanding, Decimal(str(source_balance)))
    if payable <= 0:
        return RepayOutcome(card_id, "skipped_no_funds", period=pkey)

    # ---- 防线 4:数据库条件更新抢锁(多进程防重) -------------------------- #
    # `WHERE last_period IS DISTINCT FROM <pkey>` 让并发下只有一个进程能更新成功。
    # 这条不能省:`get_scheduler()` 是进程内单例,多 worker 会重复扣钱。
    claimed = db.execute(
        update(UserAccountProjection)
        .where(UserAccountProjection.user_id == user_id)
        .where(UserAccountProjection.sync_id == card_id)
        .where(UserAccountProjection.autorepay_last_period.is_not(pkey))
        .values(autorepay_last_period=pkey)
    )
    # `db.execute` 的返回类型标注不含 rowcount(SQLAlchemy 把它放在
    # `Result` 的子类型上),运行时是有的 —— 用 getattr 消音,不要 cast 成
    # 错误的类型。
    if getattr(claimed, "rowcount", 0) != 1:
        db.rollback()
        return RepayOutcome(card_id, "skipped_locked", period=pkey,
                            detail="另一个进程正在处理本期")
    db.commit()

    # ---- 真正写交易(走 self-call,完整走 _commit_write 全套) -------------- #
    body = {
        "base_change_id": 0,
        "tx_type": "transfer",
        "amount": float(payable),
        "happened_at": _iso(today),
        "note": f"自动还款 {pkey}",
        # **强制显式传两端 sync_id** —— 只传名字会有同名歧义 → 只扣不加。
        # (键名不能用裸 `from_account_id`:from 是 Python 关键字,只能走 **kwargs)
        **{
            "from_account_id": cfg.from_account_sync_id,
            "to_account_id": card_id,
        },
    }
    headers = {
        "Idempotency-Key": f"auto-repay:{card_id}:{pkey}",
        "X-Device-ID": AUTO_REPAY_DEVICE_ID,
    }
    try:
        resp = self_call(db, method="POST",
                         path=f"/api/v1/write/ledgers/{_ledger_of(db, user_id=user_id, account_sync_id=card_id)}/transactions",
                         body=body, headers=headers)
    except Exception:
        # 写失败 → 把 last_period 撤回,否则这张卡这一期永远不会再还
        db.rollback()
        _release_period(db, user_id=user_id, card_id=card_id, pkey=pkey)
        raise

    tx_id = None
    if isinstance(resp, dict):
        tx_id = resp.get("entity_id")
    partial = payable < outstanding
    return RepayOutcome(
        card_id, "done", amount=float(payable), period=pkey, tx_sync_id=tx_id,
        detail=("部分还款:扣款账户余额不足" if partial else ""),
    )


def _release_period(db: Session, *, user_id: str, card_id: str,
                    pkey: str) -> None:
    """写交易失败时撤回 `last_period`,让本期还能重试。"""
    try:
        db.execute(
            update(UserAccountProjection)
            .where(UserAccountProjection.user_id == user_id)
            .where(UserAccountProjection.sync_id == card_id)
            .where(UserAccountProjection.autorepay_last_period == pkey)
            .values(autorepay_last_period=None)
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("autorepay: failed to release last_period card=%s", card_id)


def _ledger_of(db: Session, *, user_id: str,
               account_sync_id: str) -> str:
    """卡所属的账本 internal id。

    account 是 user-global 的,但交易必须挂到某个 ledger 上。取该卡最近
    一笔交易的 ledger;没有交易就用用户的第一个账本。
    """
    from sqlalchemy import select
    from ...models import Ledger, ReadTxProjection

    row = db.scalar(
        select(ReadTxProjection.ledger_id)
        .where(ReadTxProjection.user_id == user_id)
        .where(
            (ReadTxProjection.account_sync_id == account_sync_id)
            | (ReadTxProjection.from_account_sync_id == account_sync_id)
            | (ReadTxProjection.to_account_sync_id == account_sync_id)
        )
        .order_by(ReadTxProjection.happened_at.desc())
        .limit(1)
    )
    if row:
        return str(row)
    led = db.scalar(
        select(Ledger.id)
        .where(Ledger.user_id == user_id)
        .order_by(Ledger.created_at.asc())
        .limit(1)
    )
    return str(led) if led else ""


def _balance_of(db: Session, *, user_id: str, account_sync_id: str) -> float:
    """扣款账户当前余额(原币)。

    复用 `read/_shared.account_balance_stats` —— 那是余额的**唯一权威**实现。
    自己再写一份就是余额算法分散化的又一步(阶段 0 刚收敛过)。

    ⚠️ 两个容易踩的契约:
    1. 返回 `{account_sync_id: {...}}` —— **dict**,不是 list
    2. `balance` 是**变动额,不含期初** —— 调用方自己加 `initial_balance`
       (函数 docstring 明写)。漏了会把「只有期初、没有交易」的账户算成 0,
       于是**永远判定余额不足、从不自动还款**,而且不报任何错。
    """
    from sqlalchemy import select

    from ...models import UserAccountProjection
    from ...routers.read._shared import (
        account_balance_from_stats,
        account_balance_stats,
    )

    initial = db.scalar(
        select(UserAccountProjection.initial_balance).where(
            UserAccountProjection.user_id == user_id,
            UserAccountProjection.sync_id == account_sync_id,
        )
    )
    initial_f = float(initial or 0.0)
    # `account_balance_from_stats` 接受**整个** stats dict 并自己 `.get()`,
    # 账户没有交易时返回期初。所以不要再提前 `.get()` —— 那样既传错类型,
    # 又会把「只有期初、没有交易」的账户算成 0。
    stats = account_balance_stats(db, _ledgers_of(db, user_id=user_id))
    return account_balance_from_stats(account_sync_id, initial_f, stats)


def _ledgers_of(db: Session, *, user_id: str) -> list[str]:
    from sqlalchemy import select
    from ...models import Ledger

    return [str(r) for r in db.scalars(
        select(Ledger.id).where(Ledger.user_id == user_id)
    ).all()]


def _iso(d: date) -> str:
    return f"{d.isoformat()}T12:00:00+00:00"