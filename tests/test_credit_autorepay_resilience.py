"""自动还款的**韧性**与**可观测性**。

这三件事都是「钱没还上,而用户完全不知道」的具体形态:

1. **还款日当天失败 = 那一期永久不还**。`_tick` 原来只跑今天,
   03:00 撞上 SQLite 写锁就撤回期号,而第二天不是还款日 →
   `skipped_not_due` → 那一期就这样过去了。真实信用卡那边是逾期。
2. **多进程检测漏掉 `uvicorn --workers N`**。CLI 的 `--workers` 只读
   `WEB_CONCURRENCY` 不写它,而 spawn 出来的 worker 进程名不是 `master`
   —— 原来的两条信号都匹配不到,4 个调度器全开。
3. **没有任何可观测面**。`/healthz` 绿、`/metrics` 正常、所有交易正常,
   而只有自动还款悄悄不跑。用户几个月后才发现。

三条都便宜(合计不到 60 行),合起来把「坏了就能看见,跑了就能看见」补齐。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal


# --------------------------------------------------------------------------- #
# 1. 每日定点也回看 —— 一次失败不等于那一期永久不还                            #
# --------------------------------------------------------------------------- #


def test_daily_tick_looks_back_recent_days() -> None:
    """`_days_for(catch_up=False)` 必须是「今天 + 往前几天」,不能只有今天。"""
    from src.services.credit_card.scheduler import RETRY_DAYS, _days_for

    now = dt.date(2026, 10, 4)
    days = _days_for(catch_up=False, now=now)
    assert days == [now - dt.timedelta(days=i) for i in range(RETRY_DAYS)]
    assert days[0] == now, "第一个必须是今天 —— 今天的还款优先级最高"
    assert len(days) >= 2, (
        "回看窗口太窄,一次失败仍然可能漏掉整个账期")
    assert len(days) <= 7, (
        "回看太宽会把每日成本放大太多;长回看应该由 catch_up 负责")


def test_catch_up_looks_back_seven_days() -> None:
    from src.services.credit_card.scheduler import CATCH_UP_DAYS, _days_for

    now = dt.date(2026, 10, 4)
    days = _days_for(catch_up=True, now=now)
    assert len(days) == CATCH_UP_DAYS
    assert days[0] == now


def test_retry_window_is_safe_because_of_defenses() -> None:
    """回看不重复扣钱,靠四道防线而不是靠窗口窄。

    这条是**设计依据**的护栏:免得将来有人以为「回看 3 天有风险」,
    把它改回 `[today]`,把这个 bug 又放出来。
    """
    import inspect

    from src.services.credit_card.repay import _amount_key

    src = inspect.getsource(__import__(
        "src.services.credit_card.repay", fromlist=["x"]))
    # 防线 1 + 2:账期查重 + last_period「恰好一次」
    assert "autorepay_last_period" in src
    # 防线 4:数据库条件更新抢锁
    assert "rowcount" in src
    # 防线 3:幂等键带金额 —— 同金额重试仍幂等,不同金额是不同键
    assert _amount_key(Decimal("3000")) == "3000.00"


# --------------------------------------------------------------------------- #
# 2. 多进程检测                                                                #
# --------------------------------------------------------------------------- #


def test_explicit_override_beats_autodetect(monkeypatch) -> None:
    """`AUTOREPAY_SCHEDULER` 显式开关必须覆盖自动判断。

    自动检测不可能穷尽:`systemd Type=forking` 起两份实例、pm2 cluster、
    同机跑两个进程,**运维显式关掉才是真正的兜底**。
    """
    from src.services.credit_card.scheduler import AutoRepayScheduler

    for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
        monkeypatch.delenv(var, raising=False)

    # 有自动信号时,显式 on 仍然优先 —— 运维的意图是最高优先级
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("AUTOREPAY_SCHEDULER", "on")
    assert AutoRepayScheduler.detect_multi_process() is False

    monkeypatch.setenv("AUTOREPAY_SCHEDULER", "off")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert AutoRepayScheduler.detect_multi_process() is True


def test_env_signals_are_detected(monkeypatch) -> None:
    from src.services.credit_card.scheduler import AutoRepayScheduler

    monkeypatch.delenv("AUTOREPAY_SCHEDULER", raising=False)
    for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
        monkeypatch.delenv(var, raising=False)

    # 单进程主进程里必须 False,否则调度器根本不会启动
    assert AutoRepayScheduler.detect_multi_process() is False

    for var, val in (("WEB_CONCURRENCY", "4"), ("UVICORN_WORKERS", "3")):
        monkeypatch.setenv(var, val)
        assert AutoRepayScheduler.detect_multi_process() is True, (
            f"{var}={val} 没检测到 —— 多 worker 会每张卡扣 N 次钱")
        monkeypatch.delenv(var, raising=False)


def test_spawned_worker_is_flagged(monkeypatch) -> None:
    """被 spawn 出来的 worker 必须被判为多进程。

    `uvicorn --workers 4` **不写** `WEB_CONCURRENCY`(`config.py` 是
    `if workers is None and "WEB_CONCURRENCY" in os.environ`,CLI 的
    `--workers` 只读不设置),而 uvicorn 的 `Multiprocess` 用 spawn context,
    worker 里 `parent_process().name` 是 `SpawnProcess-1` 而不是 `master`
    —— 原来那条名字判定匹配不到,**4 个调度器全开**。

    `parent_process() is not None` 是「我是被 spawn 出来的 worker」的
    直接证据,不依赖猜进程名。

    这里 monkeypatch 而不是真的 spawn —— 「spawn 出来的 worker 里
    `parent_process()` 非 None」是 Python/uvicorn 的属性,已单独用真 spawn
    子进程验证过(见 commit 说明);本测试该覆盖的是**我的判定逻辑**。
    """
    import multiprocessing

    from src.services.credit_card.scheduler import AutoRepayScheduler

    for var in ("WEB_CONCURRENCY", "GUNICORN_CMD_ARGS", "UVICORN_WORKERS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("AUTOREPAY_SCHEDULER", raising=False)

    class _FakeParent:
        name = "SpawnProcess-1"

    # 主进程:parent_process() 是 None → 不是多进程(调度器要能启动)
    monkeypatch.setattr(multiprocessing, "parent_process", lambda: None)
    assert AutoRepayScheduler.detect_multi_process() is False, (
        "单进程主进程里也报多进程 —— 调度器根本不会启动")

    # spawn worker:parent_process() 非 None → 多进程
    monkeypatch.setattr(multiprocessing, "parent_process",
                        lambda: _FakeParent())
    assert AutoRepayScheduler.detect_multi_process() is True, (
        "spawn worker 里没检测到 —— uvicorn --workers N 会开 N 个调度器")


def test_no_funds_is_not_quiet() -> None:
    """余额不足**不能**静默。

    `skipped_no_funds` 发生在抢锁之前,所以 `autorepay_last_period` 不变
    —— 界面上的「上次自动还款:2026-09」停在上一期,于是用户判定「坏了」,
    去反复开关配置,而没有任何地方告诉他「今天因为没钱没还」。

    它曾经在安静名单里,理由是「怕刷屏」。但一条真的没还上钱的记录
    被降级成 debug,换来的安静不值得。
    """
    from src.services.credit_card.scheduler import _QUIET_STATUSES

    assert "skipped_no_funds" not in _QUIET_STATUSES, (
        "余额不足又被静音了 —— 用户界面上唯一的信号会停在上一期,"
        "而他看不到原因")


def test_no_funds_logs_a_warning() -> None:
    """光不静音不够 —— 必须真的产出一条 WARN。

    `_today` 里对 `skipped_no_funds` 单独打 warning 并带上「充值后下一次
    跑批会补上」,否则用户只知道没还,不知道还会不会补。
    """
    import logging

    from src.services.credit_card.scheduler import _log_level_for

    assert _log_level_for("skipped_no_funds") == logging.WARNING
    # 其它状态不该被升级成 warning,否则真的会刷屏
    assert _log_level_for("skipped_not_due") == logging.DEBUG
    assert _log_level_for("done") == logging.INFO
    assert _log_level_for("failed") == logging.INFO


# --------------------------------------------------------------------------- #
# 3. 可观测性                                                                  #
# --------------------------------------------------------------------------- #


def test_metrics_expose_scheduler_liveness() -> None:
    """`/metrics` 上必须能看出「调度器还活着吗」。

    `/healthz` 绿、`/metrics` 正常、所有交易正常,而只有自动还款悄悄不跑 ——
    半夜重启 / apscheduler 导入失败只 `logger.warning` 就放过 / 时区 fallback
    到 UTC,这三种情况下用户几个月后才发现。三个 gauge 就够。
    """
    from src.metrics import metrics
    from src.services.credit_card.scheduler import _publish_run_metrics

    _publish_run_metrics([
        {"status": "done", "card": "a", "amount": 1.0, "period": "2026-10"},
        {"status": "skipped_not_due", "card": "b", "amount": 0.0, "period": ""},
        {"status": "done", "card": "c", "amount": 2.0, "period": "2026-10"},
    ])
    out = metrics.render_prometheus()

    for key in ("beecount_autorepay_scheduler_running",
                "beecount_autorepay_last_run_ts",
                "beecount_autorepay_last_run_repaid"):
        assert key in out, f"{key} 不在 /metrics —— 可观测缺口"

    def val(key: str) -> float:
        for line in out.splitlines():
            if line.startswith(key + " "):
                return float(line.split()[-1])
        raise AssertionError(f"{key} 没渲染出来")

    assert val("beecount_autorepay_scheduler_running") == 1.0
    assert val("beecount_autorepay_last_run_repaid") == 2.0, (
        "真的还了的笔数算错了")
    assert val("beecount_autorepay_last_run_ts") > 0


def test_multi_process_disable_zeroes_the_gauge() -> None:
    """调度器被禁用时 gauge 必须置 0。

    否则 `/metrics` 上看不出「自动还款其实没在跑」,而 ring buffer 只有
    1000 条纯内存日志、重启即丢,那句 `scheduler disabled` 的 error
    是用户唯一的部署形态预警。
    """
    import inspect

    src = inspect.getsource(__import__(
        "src.services.credit_card.scheduler", fromlist=["x"]))

    i = src.index("autorepay.scheduler disabled")
    # error 日志之后、return 之前,必须把 gauge 归零
    window = src[i:i + 700]
    assert "set_gauge" in window and "0.0" in window, (
        "禁用分支没有把 gauge 置 0 —— /metrics 上看不出来它被关了")
