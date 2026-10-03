"""自动还款的定时调度(阶段 3b)。

## 为什么复用现有 APScheduler 而不是新建

`services/backup/scheduler.py` 已有 `BackgroundScheduler` + `CronTrigger` +
**时区解析** + `coalesce/max_instances/misfire_grace_time` + DB 驱动
schedule 表 + 启动钩子,是仓库里唯一的周期任务先例。

**时区是被解决的坑**:还款日是**本地语义**,cron 的 `H` 字段在 UTC 与
Asia/Tokyo 差 9 小时。`_resolve_scheduler_tz()`(`scheduler.py:51-82`)已经把
这个坑填平并处理了 UTC fallback 告警。

## 为什么每天一个 job 而不是每张卡一个 cron

短月顺延(账单日 31 遇 2 月 → 28)写不进 cron 表达式。判断逻辑必须在代码里,
所以 N 张卡共用 1 个「每天扫一遍」的 job。

## 两个必须处理的风险

### 1. 多进程 = 定时炸弹

`get_scheduler()` 是**进程内单例**,内存 jobstore 无跨进程锁。当前部署单
worker 安全(`SELFHOST-RUNBOOK.md:78` 无 `--workers`),但
`src/database.py:16-18` 的注释明写作者预期过多 worker。

检测到多进程时**禁用调度器**并打 error 日志。不禁用的话,将来有人加
`--workers 4`,每张卡会被扣 4 次钱。(执行器里的条件更新抢锁是第二道保险,
但让它生效比不产生重复请求更好。)

### 2. SQLite 会锁

`lock_ledger_for_materialize` 在 SQLite 上是 **no-op**(`concurrency.py:19-21`
只对 postgres 加锁)。SQLite 靠 `busy_timeout=5000`,超时抛
`OperationalError: database is locked`。

定时任务与用户写入是**互斥但可能失败**,不是「安全并发」。必须捕获并
退避重试 —— 否则用户正在记一笔账时,自动还款任务直接失败,那天的还款就漏了。

## 启动时补跑

机器关停期间漏掉的账期,在服务启动时补齐。吸收了「惰性计算(打开页面时补算)」
的自愈优点,而不让任何 GET 端点有副作用 —— 那会撞
`docs/SYNC_ARCHITECTURE.md:176-189` 的读路径契约。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from ...database import SessionLocal
from ..backup.scheduler import _resolve_scheduler_tz

logger = logging.getLogger("beecount.credit_autorepay")

#: 每天几点扫一遍。时区取 `SCHEDULER_TIMEZONE`(未配则本地时区,再退 UTC)。
#:
#: 选 03:00 —— 用户不会在这个点记账,SQLite 写锁冲突概率最低。
RUN_HOUR = 3

#: 退避重试:`database is locked` 时重试 3 次,间隔递增。
_LOCK_RETRY_DELAYS = (2.0, 5.0, 15.0)

_lock = threading.Lock()
_singleton: "AutoRepayScheduler | None" = None


class AutoRepayScheduler:
    def __init__(self) -> None:
        self._timezone = _resolve_scheduler_tz()
        self._scheduler = BackgroundScheduler(
            timezone=self._timezone,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                # 比备份的 3600 更长:漏掉的账期要靠启动补跑兜,不希望
                # 「差几分钟没跑」被直接丢弃。
                "misfire_grace_time": 6 * 3600,
            },
        )
        self._started = False
        self._multi_process = False

    # -- 多进程检测 ------------------------------------------------------- #

    @staticmethod
    def detect_multi_process() -> bool:
        """当前是不是多 worker 部署。

        两个独立信号:
        1. `WEB_CONCURRENCY` / `GUNICORN_CMD_ARGS` / `UVICORN_WORKERS` —
           常见的「我要开多 worker」环境变量
        2. 父进程名里带 master(gunicorn/uWSGI 的主进程)

        宁可**误报禁用**也不误判放过 —— 误报只是少了一次自动还款,
        误判是重复扣钱。
        """
        import os

        for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
            raw = os.environ.get(var, "").strip()
            if raw and raw not in ("0", "1"):
                return True
            if "workers" in raw and "1" not in raw:
                return True
        try:
            import multiprocessing

            parent = multiprocessing.parent_process()
            if parent is not None and "master" in parent.name.lower():
                return True
        except Exception:  # noqa: BLE001 - 检测失败不等于多进程
            pass
        return False

    # -- 生命周期 --------------------------------------------------------- #

    def start(self) -> None:
        with _lock:
            if self._started:
                return
            self._multi_process = self.detect_multi_process()
            if self._multi_process:
                # **故意禁用**。不启用的话多 worker 会每张卡扣 N 次钱。
                # 执行器里有条件更新抢锁作为第二道保险,但不产生重复请求
                # 永远比事后兜底好。
                logger.error(
                    "autorepay.scheduler disabled: detected multi-process "
                    "deployment (WEB_CONCURRENCY/GUNICORN_CMD_ARGS or parent "
                    "process is a master). Auto-repayment will NOT run. Run a "
                    "single worker, or call the repay endpoint manually."
                )
                self._started = True
                return

            self._scheduler.add_job(
                self._tick,
                CronTrigger(hour=RUN_HOUR, timezone=self._timezone),
                id="credit-autorepay-daily",
                replace_existing=True,
            )
            self._scheduler.start()
            self._started = True
            logger.info(
                "autorepay.scheduler started tz=%s hour=%s",
                self._timezone, RUN_HOUR,
            )

            # 启动时补跑:关停期间漏掉的账期在这里补齐。
            self._scheduler.add_job(
                self._catch_up, "date", id="credit-autorepay-catchup",
                replace_existing=True,
            )

    def shutdown(self) -> None:
        with _lock:
            if self._started and not self._multi_process:
                try:
                    self._scheduler.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    logger.warning("autorepay.scheduler shutdown failed",
                                   exc_info=True)
            self._started = False

    # -- 执行 ------------------------------------------------------------- #

    def _tick(self) -> None:
        _safe_run(self._today, catch_up=False)

    def _catch_up(self) -> None:
        _safe_run(self._today, catch_up=True)

    def _today(self, *, catch_up: bool) -> list[dict[str, Any]]:
        """跑今天该还的,并把结果写进日志。

        `catch_up=True` 时额外回看最近 `CATCH_UP_DAYS` 天 —— 处理
        「机器关了两周」的情况。回看窗口必须有界,否则长期停机的库会
        一次性补几十笔。
        """
        from sqlalchemy import select

        from ...models import User
        from .repay import run_daily
        from .selfcall import make_self_call

        self_call = make_self_call()
        today = datetime.now(self._timezone).date()
        days = [today]
        if catch_up:
            days = [today - timedelta(days=i) for i in range(0, CATCH_UP_DAYS)]

        results: list[dict[str, Any]] = []
        for day in days:
            with SessionLocal() as idb:
                user_ids = [str(r) for r in idb.scalars(
                    select(User.id)).all()]
            for user_id in user_ids:
                with SessionLocal() as db:
                    outcomes = run_daily(
                        db, user_id=user_id, today=day, self_call=self_call)
                for o in outcomes:
                    if o.status not in _QUIET_STATUSES:
                        logger.info(
                            "autorepay card=%s status=%s amount=%.2f "
                            "period=%s %s", o.card_sync_id, o.status,
                            o.amount, o.period or "-", o.detail)
                    results.append({
                        "user_id": user_id, "card": o.card_sync_id,
                        "status": o.status, "amount": o.amount,
                        "period": o.period,
                    })
        return results


#: 这些状态属于「正常无事发生」,只记 debug 不记 info,免得刷屏。
_QUIET_STATUSES = frozenset({
    "skipped_not_due", "skipped_disabled", "skipped_zero_outstanding",
    "skipped_already_repaid", "skipped_no_funds",
})

#: 启动补跑回看的天数。**必须有界** —— 无界补跑会让长期停机的库
#: 一次性补几十笔交易。
CATCH_UP_DAYS = 7


def _safe_run(fn, *, catch_up: bool) -> None:
    """执行 + SQLite 锁退避重试。

    `lock_ledger_for_materialize` 在 SQLite 上是 no-op
    (`concurrency.py:19-21`),SQLite 靠 `busy_timeout=5000`,超时抛
    `OperationalError: database is locked`。用户正在记一笔账时,自动还款
    可能撞上 —— 那天的还款不能就这么漏了。
    """
    import time

    from sqlalchemy.exc import OperationalError

    last: Exception | None = None
    for delay in (0.0, *_LOCK_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            fn(catch_up=catch_up)
            return
        except OperationalError as e:
            last = e
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            logger.warning(
                "autorepay: database is locked, retry in %.0fs (%s)",
                delay, e)
        except Exception:  # noqa: BLE001
            logger.exception("autorepay: run failed")
            return
    logger.error("autorepay: giving up after retries: %s", last)


def get_scheduler() -> AutoRepayScheduler:
    global _singleton
    with _lock:
        if _singleton is None:
            _singleton = AutoRepayScheduler()
        return _singleton