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


def test_detect_gaps_never_calls_repay_one(monkeypatch) -> None:
    """缺口检测**只告警**,绝不动钱 —— 判据是**拦截调用**,不是查字符串。

    ⚠️ 这条原来叫 `..._never_moves_money`,但它只做了:
    `assert "repay_one" not in inspect.getsource(AutoRepayScheduler._detect_gaps)`。
    **名字承诺的是行为,机制验的是字符串** —— 这是 151-156 轮那一族里
    最刺眼的一个:连**它自己叫什么**都在说「这条不测钱」。

    📌 **源码文本判据证明不了「钱没动」**。文本里没有 `repay_one`,
    不等于执行时不会调到它 —— 比如经由别的模块别名、或新加的一行。

    现在改成:把 `repay_one` 换成会记账的假实现,真跑一次 `_detect_gaps`,
    断言**一次都没被调到**。这是可观测行为,不是文本。
    """
    import datetime as dt

    import src.services.credit_card.scheduler as sched_mod
    from src.services.credit_card.scheduler import AutoRepayScheduler

    calls: list[int] = []

    def _tripwire(*a, **k):
        calls.append(1)
        raise AssertionError("缺口检测调用了 repay_one —— 它只能告警")

    # ⚠️ `raising=False`:实现里可能压根没 import 这个名字,
    # 那正是「现在不调」的事实,不该让测试 ERROR。
    monkeypatch.setattr(sched_mod, "repay_one", _tripwire, raising=False)
    monkeypatch.setattr(
        sched_mod, "datetime",
        type("D", (), {"now": staticmethod(lambda tz=None: dt.datetime(2026, 6, 28, 3, 0))}),
        raising=False,
    )

    sched = AutoRepayScheduler.__new__(AutoRepayScheduler)
    sched._timezone = None
    sched._detect_gaps(catch_up=False)

    assert not calls, "缺口检测调用了 repay_one —— 那会真的扣钱"


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
    from src.models import Ledger, User, UserAccountProjection

    with TS() as db:
        # ⚠️ **必须建 User 行** —— `_detect_gaps` 遍历 `User` 表拿 user_ids,
        # 没有 User 行就一个卡都看不到,`gaps` 恒空。
        # 我第一版漏了这一条,测试在真实 bug 下全绿、还原后也全绿 ——
        # 两种情况下它都没在验证任何东西。
        db.add(User(id="u1", email="gap@e2e.com", password_hash="x"))
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


def test_detect_gaps_runs_through_safe_run(monkeypatch) -> None:
    """缺口检测必须能**经 `_safe_run` 执行** —— 真服务就是这条路调它的。

    ## 这条是补一个真实事故

    第一版 `_detect_gaps(self)` 不接受 `catch_up`,而 `_safe_run` 无条件用
    `fn(catch_up=catch_up)` 调它驱动的每个函数。真服务一启动就抛:

        TypeError: _detect_gaps() got an unexpected keyword argument 'catch_up'

    而当时 **848 条单测全绿** —— 因为它们全都直接调 `self._detect_gaps()`,
    从没走过 `_safe_run`。

    这正是 `CLAUDE.md` 写的「分层测试全绿 != 拼起来能工作」。
    """
    import src.services.credit_card.scheduler as sched_mod
    from src.services.credit_card.scheduler import AutoRepayScheduler, _safe_run

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)
    monkeypatch.setattr(sched_mod, "SessionLocal", TS)
    _setup_stale_card(TS, last_period="2026-04")

    sched = AutoRepayScheduler()

    # ⚠️ **不要 monkeypatch 掉 `_detect_gaps`** —— 我第一版这么做了,
    # 结果这条测试在真实 bug 下**照样全绿**:被换掉的是 wrapper,
    # 签名对不对根本没被验证。
    #
    # 这里走**真函数**:它内部会读库(SessionLocal 已指向上面那个内存库),
    # 所以签名不对时 `_safe_run` 会在 `fn(catch_up=...)` 那一刻抛 TypeError
    # —— 而 `_safe_run` 的 `except Exception: logger.exception(...); return`
    # 把它**吞掉并 return**,所以外面看到的是「静默什么都没发生」。
    #
    # 因此断言必须落在**「真的产出了缺口」**上:签名错时 gaps 恒为空。
    gaps: list = []

    # 这个包装函数**也**必须接受 catch_up ——  无条件传它。
    # （我第一版写成无参的,结果自己撞上了同一个 TypeError。）
    def _probe(*, catch_up: bool = False) -> None:
        gaps.extend(sched._detect_gaps(catch_up=catch_up))

    _safe_run(_probe, catch_up=False)

    assert gaps, (
        "经 `_safe_run` 跑出来是空的。第一版 monkeypatch 掉了 `_detect_gaps`,"
        "签名错也照样全绿 —— 被换掉的是 wrapper。"
        "而 `_safe_run` 的 `except Exception: return` 会把 TypeError "
        "**吞掉**,外面只看到「什么都没发生」。"
        "最可能的原因:`_detect_gaps` 不接受 `_safe_run` 无条件传的 "
        "`catch_up=`。")


def test_detect_gaps_accepts_the_catch_up_kwarg() -> None:
    """它**必须**接受 `catch_up`(`_safe_run` 无条件传它)。"""
    import inspect

    from src.services.credit_card.scheduler import AutoRepayScheduler

    sig = inspect.signature(AutoRepayScheduler._detect_gaps)
    assert "catch_up" in sig.parameters, (
        f"签名缺 catch_up:{sig} —— `_safe_run` 会传它,真服务上直接 TypeError")
