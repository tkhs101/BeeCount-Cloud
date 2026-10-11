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

        ## 三个独立信号

        1. `WEB_CONCURRENCY` / `GUNICORN_CMD_ARGS` / `UVICORN_WORKERS`
        2. `AUTOREPAY_SCHEDULER` 显式开关 —— 运维说了算,覆盖自动判断
        3. `multiprocessing.parent_process()` 非 None —— 被 spawn 出来的 worker

        ## 为什么加第 3 条

        `uvicorn --workers 4` **不写** `WEB_CONCURRENCY`。
        `uvicorn/config.py` 是 `if workers is None and "WEB_CONCURRENCY" in
        os.environ`,CLI 的 `--workers` 只读不设置。而 uvicorn 的
        `Multiprocess` 用 **spawn** context,worker 里
        `multiprocessing.parent_process().name` 是 `"SpawnProcess-1"`,
        根本不是 `master` —— 原来的名字判定匹配不到,4 个调度器全开。

        `parent_process() is not None` 就是「我是一个被 spawn 出来的 worker」的
        直接证据。uvicorn 只在 workers > 1 时走 Multiprocess,
        `--workers 1` 与单进程里它都是 None,不会误报。

        ## 为什么还有第 2 条

        自动检测不可能穷尽:`systemd Type=forking` 起两份实例、pm2 cluster、
        同机跑两个进程,都检测不到。**真正的兜底是运维显式关掉**,
        runbook 里有这一条。文档的取舍仍然是宁可**误报禁用**也不误判放过 ——
        误报只是少一次自动还款(有手动触发端点兜底),误判是重复扣钱。
        """
        import os

        # 运维显式说了算,覆盖自动判断。
        explicit = os.environ.get("AUTOREPAY_SCHEDULER", "").strip().lower()
        if explicit in ("off", "0", "false", "no"):
            return True
        if explicit in ("on", "1", "true", "yes"):
            return False

        for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
            raw = os.environ.get(var, "").strip()
            if raw and raw not in ("0", "1"):
                return True
            if "workers" in raw and "1" not in raw:
                return True
        try:
            import multiprocessing

            # gunicorn 的 worker 是 fork 出来的,这里也是 None;
            # uvicorn 的 worker 是 spawn 出来的,这里是 SpawnProcess。
            if multiprocessing.parent_process() is not None:
                return True
        except Exception:  # noqa: BLE001 —— 检测失败不等于多进程
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
                # gauge 置 0 —— 否则 `/metrics` 上看不出「自动还款其实没在跑」,
                # 而 ring buffer 只有 1000 条纯内存日志,重启即丢。
                try:
                    from ...metrics import metrics

                    metrics.set_gauge(
                        "beecount_autorepay_scheduler_running", 0.0)
                except Exception:  # noqa: BLE001
                    pass
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
        # **带回看**。03:00 那一刻撞上 SQLite 写锁（用户正在记账 /
        # 定时备份在 `VACUUM INTO`）→ 写失败 → `_release_period()` 撤回
        # 期号 → 那一天的还款就没了。而 `days = [today]` 意味着第二天
        # 不是还款日,`skipped_not_due`,那一期**永久不还**。
        #
        # 真实后果:真实信用卡逾期 → 利息 / 滞纳金 / 征信。
        # 而 App 里唯一的痕迹是 ring buffer 里一条 INFO(1000 条、纯内存、
        # 重启即丢),admin 面板默认还筛 ERROR。
        #
        # 回看不会导致重复扣钱 —— 防线 1(账期查重)/ 2(last_period)/
        # 4(条件 UPDATE 抢锁)本来就是为「同账期只做一次」设计的,
        # 跑到同一个已处理的账期只会得到 skipped_already_repaid。
        # 防线 3 的幂等键带金额,同金额的重试仍然幂等。
        _safe_run(self._today, catch_up=False)

    def _catch_up(self) -> None:
        _safe_run(self._today, catch_up=True)

    def _today(self, *, catch_up: bool) -> list[dict[str, Any]]:
        """跑今天该还的,并把结果写进日志。

        `catch_up=True` 时额外回看最近 `CATCH_UP_DAYS` 天 —— 处理
        「机器关了两周」的情况。回看窗口必须有界,否则长期停机的库会
        一次性补几十笔。

        `catch_up=False`(每日定点那次)也回看 `RETRY_DAYS` 天,理由见
        `_tick` 的注释:一次失败不该等于「那一期永远不还」。
        """
        from sqlalchemy import select

        from ...models import User
        from .repay import run_daily
        from .selfcall import make_self_call

        self_call = make_self_call()
        today = datetime.now(self._timezone).date()
        days = _days_for(catch_up=catch_up, now=today)

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
                    level = _log_level_for(o.status)
                    if level >= logging.WARNING:
                        # 余额不足 = 今天真的没还上钱。用户界面上唯一能看到的
                        # 「上次自动还款:2026-09」会停在上一期,于是判断「坏了」
                        # 去反复开关配置。必须有一条能被人看见的记录。
                        logger.warning(
                            "autorepay card=%s skipped_no_funds period=%s "
                            "%s —— 扣款账户余额不足,本期未还。"
                            "充值后下一次跑批会补上(每日回看 %d 天)",
                            o.card_sync_id, o.period or "-", o.detail,
                            RETRY_DAYS)
                    elif o.status not in _QUIET_STATUSES:
                        logger.info(
                            "autorepay card=%s status=%s amount=%.2f "
                            "period=%s %s", o.card_sync_id, o.status,
                            o.amount, o.period or "-", o.detail)
                    results.append({
                        "user_id": user_id, "card": o.card_sync_id,
                        "status": o.status, "amount": o.amount,
                        "period": o.period,
                    })
        _publish_run_metrics(results)
        return results


#: 这些状态属于「正常无事发生」,只记 debug 不记 info,免得刷屏。
#:
#: `skipped_no_funds` **不在**这里 —— 扣款账户余额 ≤ 0 是「今天真的没还
#: 上钱」,静默跳过会让用户以为还在正常跑。这个状态要记 WARN。
_QUIET_STATUSES = frozenset({
    "skipped_not_due", "skipped_disabled", "skipped_zero_outstanding",
    "skipped_already_repaid",
})

#: 启动补跑回看的天数。**必须有界** —— 无界补跑会让长期停机的库
#: 一次性补几十笔交易。
CATCH_UP_DAYS = 7

#: **每日定点那次**也回看的天数。3 天覆盖「当天撞锁失败 → 隔一两天补上」
#: 的常见窗口,又不至于把每天的成本放大太多(多跑的是 3 条索引友好的
#: `skipped_not_due`,金额计算在 `is_due_on` 之后)。
RETRY_DAYS = 3


def _days_for(*, catch_up: bool, now: date) -> list[date]:
    """这一轮要处理哪些日期。**今天在最前面。**

    `catch_up=True`(启动补跑):回看 `CATCH_UP_DAYS` 天。
    `catch_up=False`(每日定点):回看 `RETRY_DAYS` 天。

    抽成模块级函数而不是内联在 `_today` 里 —— 内联的话测试要想验证这段
    逻辑只能自己重新写一遍推导,那是**自证**(把实现抄一遍,改错也不会红)。
    """
    span = CATCH_UP_DAYS if catch_up else RETRY_DAYS
    return [now - timedelta(days=i) for i in range(0, span)]


def _log_level_for(status: str) -> int:
    """这个 outcome 状态该用什么日志等级。

    ## 为什么单独一个函数

    `skipped_no_funds` 的严重度判断值得被测试直接覆盖 —— 它曾经被关在
    安静名单里,「怕刷屏」这个理由看起来完全合理。抽出来才能写
    `test_no_funds_is_not_quiet` 之外的**行为**断言,而不是只断言集合成员
    关系(集合成员关系明天就可能被重构掉)。

    ## 等级表

    - WARNING:今天真的没还上钱(`skipped_no_funds`)—— 必须被人看见
    - INFO:真的还了(`done`)/ 有问题(`failed` / `skipped_invalid_config` /
      `skipped_locked`)—— 不是「无事发生」,但也不是紧急
    - DEBUG:正常无事发生(`skipped_not_due` 等)—— 安静名单管这个
    """
    if status == "skipped_no_funds":
        return logging.WARNING
    if status in _QUIET_STATUSES:
        return logging.DEBUG
    return logging.INFO


def _publish_run_metrics(results: list[dict[str, Any]]) -> None:
    """把本轮结果推到 `/metrics`。

    ## 为什么需要

    `/healthz` 绿、`/metrics` 正常、所有交易正常,**只有自动还款悄悄不跑**
    —— 这三种情况下(半夜重启 / apscheduler 导入失败只 `logger.warning`
    就放过 / 时区 fallback 到 UTC),用户几个月后才发现。

    三个 gauge 就够,**不值得**为此建 `/admin/scheduler-status` 端点 + 状态表
    + 前端面板。

    `APScheduler` 的导入失败是**惰性**的:`start()` 里 `try: from apscheduler
    ...` 失败只 warning。gauge 停在 0.0 就是「调度器没起来」的信号。
    """
    try:
        from ...metrics import metrics
    except Exception:  # noqa: BLE001 — 指标系统不该让跑批失败
        return
    metrics.set_gauge("beecount_autorepay_scheduler_running", 1.0)
    metrics.set_gauge("beecount_autorepay_last_run_ts",
                     datetime.now(timezone.utc).timestamp())
    done = sum(1 for r in results if r.get("status") == "done")
    metrics.set_gauge("beecount_autorepay_last_run_repaid", float(done))


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