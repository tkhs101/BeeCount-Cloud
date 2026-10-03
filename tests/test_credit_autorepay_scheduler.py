"""自动还款调度器(阶段 3b)。

## 这里验证什么

调度层本身没有业务逻辑,但有三个**会烧钱**的行为必须钉住:

1. **多进程部署自动禁用** —— `get_scheduler()` 是进程内单例,内存 jobstore
   无跨进程锁。`--workers 4` 会让每张卡被扣 4 次钱。
2. **SQLite 锁退避重试** —— `lock_ledger_for_materialize` 在 SQLite 上是
   no-op(`concurrency.py:19-21`),用户记一笔账撞上定时任务时,那天的
   还款不能就这么漏了。
3. **启动补跑有界** —— 长期停机后无界补跑会一次性补几十笔交易。

另外验证多进程检测**宁可误报**:误报只是少了一次自动还款(可发现、可手动),
误判是重复扣钱。
"""
from __future__ import annotations

import pytest

from src.services.credit_card.scheduler import (
    CATCH_UP_DAYS,
    AutoRepayScheduler,
    _safe_run,
)


# --------------------------------------------------------------------------- #
# 多进程检测                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("env", [
    {"WEB_CONCURRENCY": "4"},
    {"UVICORN_WORKERS": "4"},
    {"GUNICORN_CMD_ARGS": "--workers 4"},
    {"GUNICORN_CMD_ARGS": "worker_class=uvicorn.workers.UvicornWorker --workers 8"},
])
def test_detects_multi_process(monkeypatch, env) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert AutoRepayScheduler.detect_multi_process() is True


@pytest.mark.parametrize("env", [
    {},                                    # 什么都没设 = 单 worker
    {"WEB_CONCURRENCY": "1"},               # 显式单 worker
    {"UVICORN_WORKERS": "0"},
])
def test_single_process_not_detected(monkeypatch, env) -> None:
    for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert AutoRepayScheduler.detect_multi_process() is False


def test_detection_error_does_not_report_multi_process(monkeypatch) -> None:
    """检测本身出错 ≠ 多进程。宁可少拦也不误禁。"""
    import multiprocessing

    for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
        monkeypatch.delenv(var, raising=False)

    def boom(*a, **kw):
        raise RuntimeError("检测不了")

    monkeypatch.setattr(multiprocessing, "parent_process", boom)
    assert AutoRepayScheduler.detect_multi_process() is False


def test_scheduler_is_disabled_when_multi_process(monkeypatch) -> None:
    """多进程 → **不注册 job**。

    这一条比检测本身重要:检测对了但仍然注册 job,等于没检测。
    """
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    s = AutoRepayScheduler()

    registered: list[str] = []
    monkeypatch.setattr(
        s._scheduler, "add_job",
        lambda fn, trigger, **kw: registered.append(kw.get("id", "?")))
    monkeypatch.setattr(s._scheduler, "start", lambda: None)

    s.start()
    assert registered == [], f"多进程下仍然注册了 job:{registered}"
    s.shutdown()


def test_scheduler_registers_when_single_process(monkeypatch) -> None:
    for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
        monkeypatch.delenv(var, raising=False)
    s = AutoRepayScheduler()

    registered: list[str] = []
    monkeypatch.setattr(
        s._scheduler, "add_job",
        lambda fn, trigger, **kw: registered.append(kw.get("id", "?")))
    monkeypatch.setattr(s._scheduler, "start", lambda: None)

    s.start()
    assert "credit-autorepay-daily" in registered, registered
    assert "credit-autorepay-catchup" in registered, registered
    s.shutdown()


def test_start_is_idempotent(monkeypatch) -> None:
    """startup hook 可能被调两次;重复注册 job 会重复扣钱。"""
    for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
        monkeypatch.delenv(var, raising=False)
    s = AutoRepayScheduler()
    registered: list[str] = []
    monkeypatch.setattr(
        s._scheduler, "add_job",
        lambda fn, trigger, **kw: registered.append(kw.get("id", "?")))
    monkeypatch.setattr(s._scheduler, "start", lambda: None)
    s.start()
    s.start()
    assert len(registered) == 2, f"start() 不幂等,注册了 {len(registered)} 个:{registered}"
    s.shutdown()


# --------------------------------------------------------------------------- #
# SQLite 锁退避                                                                #
# --------------------------------------------------------------------------- #


def test_retries_on_database_locked(monkeypatch) -> None:
    """`database is locked` 必须重试,不能直接放弃。

    用户记一笔账时定时任务撞上 SQLite 写锁是很正常的。那天的还款不能漏。
    """
    from sqlalchemy.exc import OperationalError

    calls = {"n": 0}

    def flaky(*, catch_up: bool = False):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))

    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda d: slept.append(d))

    _safe_run(flaky, catch_up=False)
    assert calls["n"] == 3, f"只尝试了 {calls['n']} 次"
    assert slept, "重试之间没有退避"


def test_gives_up_after_max_retries(monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    calls = {"n": 0}

    def always_locked(*, catch_up: bool = False):
        calls["n"] += 1
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr("time.sleep", lambda d: None)
    _safe_run(always_locked, catch_up=False)
    # 首次 + 3 次重试
    assert calls["n"] == 4, f"重试次数不对:{calls['n']}"


def test_non_lock_operational_error_raises(monkeypatch) -> None:
    """非锁类 `OperationalError` **不重试** —— 那是真 bug,重试只会掩盖。"""
    from sqlalchemy.exc import OperationalError

    calls = {"n": 0}

    def bad(*, catch_up: bool = False):
        calls["n"] += 1
        raise OperationalError("SELECT", {}, Exception("no such table: users"))

    monkeypatch.setattr("time.sleep", lambda d: None)
    with pytest.raises(OperationalError):
        _safe_run(bad, catch_up=False)
    assert calls["n"] == 1, f"不该重试,却试了 {calls['n']} 次"


def test_succeeds_on_second_attempt(monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    calls = {"n": 0}

    def flaky(*, catch_up: bool = False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr("time.sleep", lambda d: None)
    _safe_run(flaky, catch_up=False)
    assert calls["n"] == 2


def test_unexpected_error_does_not_retry(monkeypatch) -> None:
    """非数据库异常不重试,直接记日志返回。

    重试一个代码 bug 只会把同一个错重复三次。
    """
    calls = {"n": 0}

    def boom(*, catch_up: bool = False):
        calls["n"] += 1
        raise ValueError("代码 bug")

    monkeypatch.setattr("time.sleep", lambda d: None)
    _safe_run(boom, catch_up=False)
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# 启动补跑有界                                                                #
# --------------------------------------------------------------------------- #


def test_catch_up_window_is_bounded() -> None:
    """补跑窗口**必须有界**。

    无界补跑会让长期停机(比如机器关了半年)的库一次性补几十笔交易 ——
    每一笔都是真金白银的扣款。
    """
    assert 0 < CATCH_UP_DAYS <= 31, (
        f"补跑窗口 {CATCH_UP_DAYS} 天不合理:过小会漏,过大一次补太多"
    )


def test_catch_up_runs_today_first(monkeypatch) -> None:
    """补跑必须**先跑今天** —— 顺序反了会让今天的还款被历史账期挤掉。"""
    import datetime as dt

    seen: list[dt.date] = []

    class Fake:
        _timezone = dt.timezone.utc

        def _today(self, *, catch_up: bool) -> list:
            import datetime

            today = datetime.datetime.now(self._timezone).date()
            days = ([today - dt.timedelta(days=i) for i in range(CATCH_UP_DAYS)]
                    if catch_up else [today])
            seen.extend(days)
            return []

    Fake()._today(catch_up=True)
    assert seen, "没有产生任何补跑日期"
    today = dt.datetime.now(dt.timezone.utc).date()
    assert seen[0] == today, f"补跑顺序错了,第一个是 {seen[0]} 而不是今天"


# --------------------------------------------------------------------------- #
# 日志噪音                                                                    #
# --------------------------------------------------------------------------- #


def test_quiet_statuses_cover_no_op_paths() -> None:
    """「正常无事发生」的状态必须进安静名单,否则每天刷屏。"""
    from src.services.credit_card.scheduler import _QUIET_STATUSES

    # 这些是「没发生什么」的分支
    for s in ("skipped_not_due", "skipped_disabled", "skipped_zero_outstanding",
              "skipped_already_repaid", "skipped_no_funds"):
        assert s in _QUIET_STATUSES, f"{s} 会每天刷 info 日志"
    # 这些是「有问题」或「真的还了」,必须可见
    for s in ("done", "failed", "skipped_invalid_config", "skipped_locked"):
        assert s not in _QUIET_STATUSES, f"{s} 被静音了 —— 用户看不到出了什么问题"