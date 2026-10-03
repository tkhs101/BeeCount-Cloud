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

## self_call 为什么可能是 coroutine

self-call 底层是 `httpx.AsyncClient` + `ASGITransport`,而 ASGI transport
在发请求时会访问**当前线程**的事件循环。两个调用点的处境**相反**:

- **APScheduler 线程**:没有运行中的 loop → 直接给一个 `asyncio.run` 包装
- **FastAPI 端点**:已经在 async 上下文 → 必须复用它那个 loop

所以执行器不假设 self_call 是同步还是异步:调用后用 `inspect.isawaitable`
判断,必要时在**独立线程**里跑完那个 coroutine。

⚠️ 不用 `new_event_loop().run_until_complete()` —— 端点那个线程里已经
有一个 loop 在跑,httpx 会找到它并抛
`RuntimeError: Cannot run the event loop while another loop is running`。
**换线程**才能拿到一个干净的 loop。

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
import concurrent.futures
import inspect
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


def _amount_key(amount: Decimal) -> str:
    """金额的稳定字符串形式,用于拼幂等键。

    量化到分(0.01)—— 避免浮点尾差把同一笔钱变成两个不同的键,
    那会让防线 3 失效。`Decimal` 量化后 `str` 不会带尾随零。
    """
    return str(amount.quantize(Decimal("0.01")))


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
        db, user_id=user_id,
        ledger_id=_ledger_internal_of(db, user_id=user_id,
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
    # 幂等键里**带上金额**。
    #
    # 服务端的 `Idempotency-Key` 校验会把 payload 一起 hash
    # (`write/_shared.py::_hash_request`):同 key + 不同 payload → 409
    # `IDEMPOTENCY_KEY_REUSED`。而本键原本只有 `{card}:{period}`,一旦
    # 防线 4 撤回 `last_period` 后重试,**欠款额可能已经变了**(期间又刷了
    # 一笔、或上期还款落账)→ 撞 409,自动还款彻底卡住。
    #
    # 带上金额后:同金额的重试仍然幂等(防线 3 生效),不同金额是不同键
    # (那是**不同的事**,本就不该被当成重复)。防线 1/2/4 才是「同一件事
    # 只做一次」的真正保证,防线 3 只是短时间重试的保险。
    headers = {
        "Idempotency-Key": f"auto-repay:{card_id}:{pkey}:{_amount_key(payable)}",
        "X-Device-ID": AUTO_REPAY_DEVICE_ID,
    }
    from ...models import User
    actor = db.get(User, user_id)
    try:
        resp = _resolve(self_call(db, method="POST",
                         path=f"/api/v1/write/ledgers/{_ledger_external_of(db, user_id=user_id, account_sync_id=card_id)}/transactions",
                         body=body, headers=headers, user=actor))
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


def _ledger_internal_of(db: Session, *, user_id: str,
                        account_sync_id: str) -> str:
    """卡所属账本的 **internal id**(`Ledger.id`)。

    用于 `compute_statement` —— `read_tx_projection.ledger_id` 存的就是它。
    """
    from sqlalchemy import select

    from ...models import Ledger, ReadTxProjection

    internal = db.scalar(
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
    if internal:
        return str(internal)
    led = db.scalar(
        select(Ledger.id).where(Ledger.user_id == user_id)
        .order_by(Ledger.created_at.asc()).limit(1))
    return str(led) if led else ""


def _ledger_external_of(db: Session, *, user_id: str,
                        account_sync_id: str) -> str:
    """卡所属账本的 **external id** —— 写路由的路径参数要的是它。

    ⚠️ **external 与 internal 是两个不同的 id**,混淆会让每次自动还款 404。

    - `Ledger.id` = internal(uuid),`read_tx_projection.ledger_id` 存它
    - `Ledger.external_id` = 用户可见的那个(如 `daily`),路由
      `/write/ledgers/{ledger_external_id}/transactions` 要它

    第一版只留了一个 `_ledger_of`,按调用点需要返回不同口径 —— 结果
    修一处崩另一处:给了 external,`compute_statement` 就查不到交易、算出
    账单 0;给了 internal,真实请求就 404。**所有分层测试都是绿的**,
    因为它们用 FakeCall,没人真发过这个请求。

    这就是必须有端到端层的理由。
    """
    from sqlalchemy import select

    from ...models import Ledger

    internal = _ledger_internal_of(db, user_id=user_id,
                                   account_sync_id=account_sync_id)
    led = None
    if internal:
        led = db.scalar(
            select(Ledger.external_id)
            .where(Ledger.user_id == user_id)
            .where(Ledger.id == internal)
        )
    if not led:
        led = db.scalar(
            select(Ledger.external_id)
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


def _resolve(value: Any) -> Any:
    """self_call 的结果可能是 coroutine —— 是就**换线程**跑完。

    不能在当前线程 `run_until_complete`:FastAPI 端点上下文里已经有运行中
    的 loop,httpx 的 ASGI transport 会找到它并抛
    `RuntimeError: Cannot run the event loop while another loop is running`。

    换到一个全新的线程,那里没有任何 loop,`asyncio.run()` 干净可用。
    这个 coroutine 内部只做 HTTP,不依赖外层上下文,在线程里跑是安全的。
    """
    if not inspect.isawaitable(value):
        return value
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_await(value))).result()


async def _await(coro: Any) -> Any:
    return await coro
