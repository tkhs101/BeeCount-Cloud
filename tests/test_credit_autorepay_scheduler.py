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

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
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


def test_catch_up_runs_today_first() -> None:
    """补跑必须**先跑今天** —— 顺序反了会让今天的还款被历史账期挤掉。

    直接调 `_days_for`,不自己重新实现一遍推导 —— 那会是**自证
    测试**(把实现抄一遍,实现改错了它也不会红)。
    """
    import datetime as dt

    from src.services.credit_card.scheduler import CATCH_UP_DAYS, _days_for

    days = _days_for(catch_up=True, now=dt.date(2026, 10, 4))
    assert len(days) == CATCH_UP_DAYS
    assert days[0] == dt.date(2026, 10, 4), (
        f"补跑顺序错了,第一个是 {days[0]} 而不是今天")
    assert days == [dt.date(2026, 10, 4) - dt.timedelta(days=i)
                    for i in range(CATCH_UP_DAYS)]


# --------------------------------------------------------------------------- #
# 日志噪音                                                                    #
# --------------------------------------------------------------------------- #


def test_quiet_statuses_cover_no_op_paths() -> None:
    """「正常无事发生」的状态必须进安静名单,否则每天刷屏。"""
    from src.services.credit_card.scheduler import _QUIET_STATUSES

    # 这些是「没发生什么」的分支
    for s in ("skipped_not_due", "skipped_disabled", "skipped_zero_outstanding",
              "skipped_already_repaid"):
        assert s in _QUIET_STATUSES, f"{s} 会每天刷 info 日志"
    # 这些是「有问题」或「真的还了」,必须可见
    for s in ("done", "failed", "skipped_invalid_config", "skipped_locked"):
        assert s not in _QUIET_STATUSES, f"{s} 被静音了 —— 用户看不到出了什么问题"

# --------------------------------------------------------------------------- #
# 补跑窗口外的缺口检测                                                        #
# --------------------------------------------------------------------------- #


def test_gap_needs_two_months_behind_not_one() -> None:
    """落后 1 期**不报**,落后 2 期才报。

    落后 1 期是正常的:本月还没到还款日,`last_period` 自然是上个月。
    把它算成缺口会每天刷一条 WARNING,用户很快就学会无视告警 ——
    而真正需要被看见的那条(真的漏了一期)夹在里面。
    """
    from src.services.credit_card.scheduler import _months_behind

    assert _months_behind("2026-09", "2026-10") == 1, "差一个月"
    assert _months_behind("2026-09", "2026-11") == 2, "差两个月"
    assert _months_behind("2026-10", "2026-10") == 0, "同期"
    # 跨年
    assert _months_behind("2025-11", "2026-01") == 2, "跨年要算对"
    # 解析不了不报警 —— 误报比漏报更伤(用户会学会无视)
    assert _months_behind("garbage", "2026-10") == 0
    assert _months_behind("", "2026-10") == 0


def test_detect_gaps_never_moves_money() -> None:
    """缺口检测**只读**,不写任何交易、不改任何配置。

    计划文档明确反对无界补跑(补几十笔真钱),所以这一轮只告警。
    若哪天有人给它加了 `repay_one` 调用,这条会红。
    """
    import inspect

    from src.services.credit_card.scheduler import AutoRepayScheduler

    src = inspect.getsource(AutoRepayScheduler._detect_gaps)
    assert "repay_one" not in src, (
        "缺口检测里出现了 repay_one —— 它只能告警,不能自动扣钱")
    assert "run_daily" not in src, src
    # 只应该有一条 warning 把缺口说出来
    assert "log.warning" in src


def test_detect_gaps_flags_two_months_behind(monkeypatch) -> None:
    """`last_period` 落后** 2 期**必须被报出来,并带用户能行动的信息。

    场景:4 月 25 号该还,VPS 关机,5 月 8 号开机。
    `CATCH_UP_DAYS = 7` 只回看到 5 月 1 号 → 4 月那期**永远补不上** ——
    而真实信用卡那边已经是逾期。用户界面上零提示。

    这里用 monkeypatch 把「今天」钉住,让账期推导可预测。
    """
    import datetime as dt

    import src.services.credit_card.scheduler as sched_mod
    from src.services.credit_card.scheduler import (
        AutoRepayScheduler,
        _months_behind,
    )

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)
    monkeypatch.setattr(sched_mod, "SessionLocal", TS)

    # bd=10 / dd=25。今天钉在 2026-06-28。
    # 把「现在」钉在 2026-06-28。直接 monkeypatch 模块级 datetime ——
    # `_detect_gaps` 内部用的是 `datetime.now(self._timezone).date()`。
    # (第一版去 patch 一个叫 `_tz_now` 的方法,而实现里没这个方法,
    #  `raising=False` 让它静默通过,于是测试跑的是真实当天。)
    _fixed = dt.datetime(2026, 6, 28, 3, 0)

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _fixed.replace(tzinfo=tz) if tz else _fixed

    monkeypatch.setattr(sched_mod, "datetime", _FrozenDatetime)

    _setup_stale_card(TS, last_period="2026-04")   # 落后 2 期
    gaps = AutoRepayScheduler()._detect_gaps()

    # 落后 2 期 = 命中
    assert gaps, "落后 2 期没被检测到 —— 那一期永远不会被还,而用户看不到"
    g = gaps[0]
    assert g["last_period"] == "2026-04", g
    assert g["current_period"] == "2026-06", g  # 上一个已过还款日是 6/25
    assert g["card"], g
    # 告警里要点名是哪张卡、落后到哪,用户才知道该去手动补哪一期
    assert "name" in g, g


def test_detect_gaps_one_month_behind_is_not_a_gap(monkeypatch) -> None:
    """落后 1 期是**正常**的(本月还没到还款日),不许报。"""
    import datetime as dt

    import src.services.credit_card.scheduler as sched_mod
    from src.services.credit_card.scheduler import AutoRepayScheduler

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)
    monkeypatch.setattr(sched_mod, "SessionLocal", TS)

    # 把「现在」钉在 2026-06-28。直接 monkeypatch 模块级 datetime ——
    # `_detect_gaps` 内部用的是 `datetime.now(self._timezone).date()`。
    # (第一版去 patch 一个叫 `_tz_now` 的方法,而实现里没这个方法,
    #  `raising=False` 让它静默通过,于是测试跑的是真实当天。)
    _fixed = dt.datetime(2026, 6, 28, 3, 0)

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _fixed.replace(tzinfo=tz) if tz else _fixed

    monkeypatch.setattr(sched_mod, "datetime", _FrozenDatetime)

    _setup_stale_card(TS, last_period="2026-05")   # 只落后 1 期
    gaps = AutoRepayScheduler()._detect_gaps()

    assert gaps == [], (
        f"落后 1 期被当成缺口 —— 那是正常状态,每天刷 WARNING 会让用户"
        f"学会无视告警,真正需要看见的那条夹在里面。gaps={gaps}")


def _setup_stale_card(TS, *, last_period):
    """造一张「配了自动还款、`last_period` 落后」的卡。

    不能从 `test_credit_autorepay_repay`  import 它的 `_setup` ——
    那边用模块级 `db` fixture,而这里的 SessionLocal 被 monkeypatch 掉了,
    必须用同一份 sessionmaker 才看得见数据。
    """
    from src.models import Ledger, UserAccountProjection

    with TS() as db:
        db.add(Ledger(id="lg-gap", user_id="u1", external_id="g", name="G",
                      currency="JPY"))
        db.add(UserAccountProjection(
            user_id="u1", sync_id="acc-gap", name="招行信用卡",
            account_type="credit_card", currency="JPY",
            initial_balance=0.0, billing_day=10, payment_due_day=25,
            autorepay_enabled=True,
            autorepay_from_account_sync_id="acc-src",
            autorepay_last_period=last_period,
            source_change_id=1))
        db.commit()


@pytest.mark.parametrize("today,due_day,expected", [
    # 还款日已过 → 就是本月那个
    (dt.date(2026, 6, 25), 25, "2026-06-25"),
    (dt.date(2026, 6, 30), 25, "2026-06-25"),
    # 还款日还没到 → 上一个月的
    (dt.date(2026, 6, 24), 25, "2026-05-25"),
    # **跨年**:1 月 5 号早上开机,上一个还款日在去年 12 月。
    # 第一版手写 `today.month - 1` → month=0 → calendar 抛
    # IllegalMonthError —— 而这函数「今天可能是任意一天」,1 月必踩。
    (dt.date(2026, 1, 5), 25, "2025-12-25"),
    # **短月钳位**:还款日 31、2 月 20 号 → 上一个是 1/31(2 月没有 31 号)
    (dt.date(2026, 2, 20), 31, "2026-01-31"),
    # 恰好是月末当天 → 含今天
    (dt.date(2026, 3, 31), 31, "2026-03-31"),
    (dt.date(2026, 4, 30), 30, "2026-04-30"),
])
def test_last_past_due_date(today, due_day, expected) -> None:
    """最近一个已过去的还款日 —— 缺口检测的基准。

    这个基准选错会**每天一条假告警**(把「上月已还、本月还没到期」的正常
    状态判成落后两期),用户很快就学会无视日志。
    """
    from src.services.credit_card.scheduler import _last_past_due_date

    got = _last_past_due_date(today, due_day=due_day)
    assert got.isoformat() == expected, f"{today} due={due_day}: {got}"


def test_gap_detection_runs_on_non_due_day(monkeypatch) -> None:
    """**非还款日**也必须能跑(第一版在这里直接抛异常被吞)。

    用户绝大多数日子打开都不是还款日,而 catch-up 调度也在任意一天跑。
    检测在那些日子失效 = 恰好在需要它的场合不工作。
    """
    import src.services.credit_card.scheduler as sched_mod
    from src.services.credit_card.scheduler import AutoRepayScheduler

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)
    monkeypatch.setattr(sched_mod, "SessionLocal", TS)

    _fixed = dt.datetime(2026, 6, 26, 3, 0)   # 26 号,还款日是 25 → 非还款日

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _fixed.replace(tzinfo=tz) if tz else _fixed

    monkeypatch.setattr(sched_mod, "datetime", _FrozenDatetime)

    _setup_stale_card(TS, last_period="2026-04")   # 落后 2 期
    gaps = AutoRepayScheduler()._detect_gaps()
    assert gaps, "非还款日上缺口检测没有结果 —— 它只在一个月的 1/25 有效"
