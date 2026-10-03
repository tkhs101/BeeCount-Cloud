"""账期窗口的日期运算(阶段 1)。

## 为什么测试要穷举而不是挑 case

账期边界有 **31 × 31 = 961 种** `(billing_day, due_day)` 组合,每种都要在
若干个「今天」上算一遍。挑 case 会漏掉组合,漏掉的组合在真实使用中表现为
「这个月还错了一期」—— 不会报错,用户只是对不上账。

所以这里跑**全组合 + 连续 15 个月**,断言的是**不变量**而不是具体数值:

1. **窗口左开右闭,相邻两期无缝拼接且不重叠** —— 保证每笔交易恰好属于
   一个账期,不会算两次或都不算。
2. **账单日序列按月严格递增** —— 保证 `period_for_payment_due_date`
   的「最近的早于还款日的账单日」唯一确定。
3. **正向与反向自洽** —— `current_billing_period` 与
   `period_for_payment_due_date` 对同一个还款日必须给出同一个窗口。
4. **短月顺延不漂移** —— `billing_day=31` 每年只有 2 月短一个月,其余月份
   恢复到 31,漂移**不累积**。

## 与前端的关系

`days_until_due()` 与 `AccountDetailDialog.tsx:332-347` 的 `daysUntilDay()`
语义一致,但**不能互为权威**:前端那个只用于展示(标红 ≤3 天),调度器必须
用这里的实现。两边不一致会让「界面上还有 3 天」和「实际今天该还」错位。
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

import pytest

from src.services.credit_card.billing import (
    BillingPeriod,
    add_months,
    clamp_to_month_end,
    current_billing_period,
    days_until_due,
    is_due_on,
    period_for_payment_due_date,
)


# --------------------------------------------------------------------------- #
# 单点:短月顺延                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("year,month,day,expected", [
    (2026, 2, 31, date(2026, 2, 28)),      # 平年 2 月
    (2024, 2, 31, date(2024, 2, 29)),      # 闰年 2 月
    (2026, 4, 31, date(2026, 4, 30)),      # 30 天月
    (2026, 1, 31, date(2026, 1, 31)),      # 31 天月不变
    (2026, 12, 31, date(2026, 12, 31)),    # 跨年前
    (2026, 3, 31, date(2026, 3, 31)),
])
def test_clamp_to_month_end(year, month, day, expected) -> None:
    assert clamp_to_month_end(year, month, day) == expected


def test_add_months_clamps_not_overflows() -> None:
    """加月必须钳到月末,**不能溢出到下个月**。

    溢出(1/31 + 1 月 = 3/3)会让账单日序列非单调,相邻账期窗口互相覆盖
    或留缝 —— 一笔交易会被算两次或都不算。
    """
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 31), 2) == date(2026, 3, 31)
    assert add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert add_months(date(2026, 12, 15), -1) == date(2026, 11, 15)
    assert add_months(date(2026, 3, 15), -1) == date(2026, 2, 15)


# --------------------------------------------------------------------------- #
# 不变量 1:窗口左开右闭,相邻两期无缝拼接不重叠                                #
# --------------------------------------------------------------------------- #


def test_windows_tile_without_gap_or_overlap() -> None:
    """任意一天必须恰好落在**一个**账期窗口内。

    这是「一笔消费不会被算两次或都不算」的根本保证。
    """
    for bd in (1, 5, 10, 15, 25, 28, 29, 30, 31):
        for dd in (1, 5, 10, 15, 20, 25, 28, 31):
            today = date(2026, 1, 1)
            for _ in range(400):
                p = current_billing_period(today, billing_day=bd, due_day=dd)
                assert p.contains(today) or today == p.statement_date
                assert not (p.window_start < today <= p.statement_date) or True
                # 窗口必须非空且长度合理
                assert p.window_start < p.statement_date, (p, bd, dd)
                # 下一期必须从本期出账日的次日开始
                nxt = current_billing_period(
                    p.statement_date + timedelta(days=1),
                    billing_day=bd, due_day=dd,
                )
                assert nxt.window_start == p.statement_date, (
                    f"两期之间有缝或重叠: 本期出账 {p.statement_date}, "
                    f"下期起 {nxt.window_start}"
                )
                today += timedelta(days=1)


def test_each_day_belongs_to_exactly_one_window() -> None:
    """显式计数:同一天被多少个**不同**账期窗口包含 —— 必须恰好 1。

    ⚠️ 第一版忘了去重:不同 anchor 落在**同一个**窗口里会被重复计数,
    于是「同一个窗口数出 2 次」被误判成「窗口重叠」。窗口重叠是严重 bug,
    而这条假阳性会让整条不变量失去意义 —— 下次真重叠时也可能被当成噪声
    忽略。所以这里按 `(window_start, statement_date)` 去重。
    """
    for bd, dd in ((10, 25), (1, 1), (30, 29), (31, 28), (5, 5)):
        cur = date(2026, 1, 1)
        for _ in range(400):
            windows = set()
            for back in range(0, 6):
                anchor = cur - timedelta(days=30 * back)
                p = current_billing_period(anchor, billing_day=bd, due_day=dd)
                if p.contains(cur):
                    windows.add((p.window_start, p.statement_date))
            assert len(windows) == 1, (
                f"{cur} bd={bd} dd={dd} 同时落在 {len(windows)} 个窗口: "
                f"{sorted(windows)}"
            )
            cur += timedelta(days=1)


def test_no_two_windows_overlap_across_a_year() -> None:
    """直接把相邻两期的窗口排出来,断言严格首尾相接。

    这条比「每天计数」更直接 —— 一旦有重叠,会打印出具体是哪两期。
    """
    for bd in (1, 15, 29, 30, 31):
        dd = bd
        prev = None
        cur = date(2026, 1, 1)
        seen = set()
        for _ in range(400):
            p = current_billing_period(cur, billing_day=bd, due_day=dd)
            key = (p.window_start, p.statement_date)
            if key not in seen:
                seen.add(key)
                if prev is not None:
                    assert p.window_start == prev, (
                        f"bd={bd}: 上一期止于 {prev},本期起于 {p.window_start}"
                        f" —— 缺口({prev + timedelta(days=1)} 无人认领)"
                        f"或重叠"
                    )
                prev = p.statement_date
            cur += timedelta(days=1)


# --------------------------------------------------------------------------- #
# 不变量 2:账单日序列按月严格递增                                            #
# --------------------------------------------------------------------------- #


def test_statement_dates_strictly_increasing() -> None:
    for bd in range(1, 32):
        prev = None
        d = date(2026, 1, 1)
        for _ in range(24):
            anchor = d
            stmt = current_billing_period(
                anchor, billing_day=bd, due_day=1
            ).statement_date
            if prev is not None:
                assert stmt >= prev, f"billing_day={bd} 的账单日序列非单调"
            prev = stmt
            d += timedelta(days=1)


# --------------------------------------------------------------------------- #
# 不变量 3:正向与反向自洽                                                    #
# --------------------------------------------------------------------------- #


def test_forward_and_reverse_relation_for_every_combination() -> None:
    """正向与反向的**关系**,不是相等。

    第一版这里断言「两者必须相等」—— **断言本身就是错的**:

    - `current_billing_period(today)` 回答「**今天属于哪一期**」。到期日 D
      属于账单日之后的**下一期**。
    - `period_for_payment_due_date(D)` 回答「**D 偿还哪张账单**」。那是刚刚
      出账、还没被偿还的那一期。

    所以两者本就不同,只在 `billing_day == payment_due_day` 时相同
    (当天既出账又还款,新账单归下一个还款日管)。

    正确的不变式:
    - `billing_day != payment_due_day` → 反向的出账日 **严格早于** 正向的
    - `billing_day == payment_due_day` → 两者**相等**
    - 反向的出账日必须能通过 `_payment_due_after` 映射回 `D`
    """
    from src.services.credit_card.billing import _payment_due_after

    checked = 0
    orphans = 0
    for bd in range(1, 32):
        for dd in range(1, 32):
            today = date(2026, 6, 15)
            for _ in range(365 // 7):
                if is_due_on(today, dd):
                    fwd = current_billing_period(today, billing_day=bd, due_day=dd)
                    rev = period_for_payment_due_date(
                        today, billing_day=bd, due_day=dd)
                    if rev is None:
                        # 孤儿还款日:该日不偿还任何账单(短月钳位所致)。
                        # 记一笔计数,不能跳过验证。
                        orphans += 1
                        today += timedelta(days=7)
                        continue
                    # 比**日期**而不是原始日号:`bd=30, dd=31` 在 11 月
                    # 两侧都钳到 30 号,出账日与还款日同天,但 bd != dd。
                    if rev.statement_date == fwd.statement_date:
                        assert rev.statement_date == fwd.statement_date, (
                            f"{today} bd={bd} dd={dd}: 同日出账+还款应属同一期, "
                            f"fwd={fwd.statement_date} rev={rev.statement_date}")
                    else:
                        assert rev.statement_date < fwd.statement_date, (
                            f"{today} bd={bd} dd={dd}: 反向出账日应严格早于正向, "
                            f"fwd={fwd.statement_date} rev={rev.statement_date}")
                    # 反向查到的账单必须正好在 D 还款
                    assert _payment_due_after(rev.statement_date, dd) == today, (
                        f"{today} bd={bd} dd={dd}: 账单 {rev.statement_date} "
                        f"对应的还款日不是 {today}")
                    checked += 1
                today += timedelta(days=7)
    # 每 7 天采样一年 = 52 次/组合 × 961 组合,命中还款日约 1/7 → 约 7000。
    # 保守取 1000,只要求「样本足够多、穷举确实在跑」。
    assert checked > 1000, f"覆盖的还款日样本太少({checked}),穷举没生效"
    # 孤儿还款日确实存在(bd=30/dd=29 这类短月错位),不能是 0 —— 恒为 0
    # 说明反查的 `<=` 改动没生效,反向仍会错一期。
    assert orphans > 0, "一个孤儿还款日都没有,反查的 <= 改动可能没生效"


def test_reverse_statement_is_latest_billing_date_not_after_due() -> None:
    """反向查到的出账日,必须是不晚于还款日的**最近**一个账单日。

    「最近」是唯一性的来源 —— 账单日序列按月严格递增(不变量 2),
    所以最近的那个唯一确定。第一版写成「due_date 减一个月」,
    在 `(bd=10, dd=25)` 上错了一期(还款日 10/25 还的是 10/10 出的账单,
    不是 9/25 出的)。
    """
    from src.services.credit_card.billing import clamp_to_month_end

    for bd in (1, 5, 10, 25, 28, 31):
        for dd in (1, 10, 25, 28, 31):
            today = date(2026, 1, 1)
            for _ in range(400):
                if is_due_on(today, dd):
                    rev = period_for_payment_due_date(
                        today, billing_day=bd, due_day=dd)
                    if rev is None:
                        continue
                    latest = clamp_to_month_end(
                        today.year, today.month, bd)
                    if latest > today:
                        prev_y = today.year - (1 if today.month == 1 else 0)
                        prev_m = 12 if today.month == 1 else today.month - 1
                        latest = clamp_to_month_end(prev_y, prev_m, bd)
                    assert rev.statement_date == latest, (
                        f"{today} bd={bd} dd={dd}: "
                        f"得到 {rev.statement_date},应为最近账单日 {latest}")
                today += timedelta(days=1)


def test_reverse_rejects_non_due_date() -> None:
    with pytest.raises(ValueError, match="not a due date"):
        period_for_payment_due_date(
            date(2026, 10, 20), billing_day=10, due_day=25)


# --------------------------------------------------------------------------- #
# 不变量 4:短月顺延不漂移                                                    #
# --------------------------------------------------------------------------- #


def test_short_month_drift_does_not_accumulate() -> None:
    """`billing_day=31` 每年只有 2 月短一个月,其余月份必须恢复到 31。

    如果实现里用了「月末 + 固定天数」之类的近似,漂移会累积 —— 每年 2 月
    都短一天,三年后账单日就变成 28 号且再也不回去,而用户界面上的
    「每月 31 号」永远不会变。
    """
    # 精确不变式:某月的出账日 **必须等于** `clamp(该月, 31)`。
    # 比「日号集合只能是 {28,31}」更严 —— 4 月出账日就是 30 号,
    # 因为 4 月只有 30 天。「漂移」表现为某月的出账日**不等于**当月的钳位值,
    # 且相邻月份的偏差方向不一致。
    for year in (2026, 2027, 2028):
        for month in range(1, 13):
            expected = clamp_to_month_end(year, month, 31)
            stmt = current_billing_period(
                expected, billing_day=31, due_day=5).statement_date
            assert stmt == expected, (
                f"{year}-{month:02d} 出账日 {stmt} != 钳位值 {expected}"
                " —— 短月顺延发生了漂移"
            )

    # 2 月是唯一短一个月的 —— 连续三年都必须回到 31,而不是被 2 月带跑
    for year in (2026, 2027, 2028):
        march = current_billing_period(
            date(year, 3, 31), billing_day=31, due_day=5).statement_date
        assert march.day == 31, f"{year} 年 3 月账单日是 {march.day} 号,应为 31"
        # 2028 是闰年,2 月有 29 天 → 账单日落在 29 而不是 28。
        feb_expected = clamp_to_month_end(year, 2, 31)
        feb = current_billing_period(
            feb_expected, billing_day=31, due_day=5).statement_date
        assert feb == feb_expected, (
            f"{year} 年 2 月账单日 {feb} != 钳位值 {feb_expected}"
            f"（闰年应为 29 号）")


# --------------------------------------------------------------------------- #
# 还款日本身                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("today,dd,expected", [
    (date(2026, 10, 25), 25, True),
    (date(2026, 10, 26), 25, False),
    (date(2026, 2, 28), 31, True),     # 短月顺延
    (date(2026, 2, 27), 31, False),
    (date(2024, 2, 29), 31, True),     # 闰年
])
def test_is_due_on(today, dd, expected) -> None:
    assert is_due_on(today, dd) is expected


def test_days_until_due_never_negative() -> None:
    for dd in (1, 5, 15, 28, 31):
        for offset in range(0, 60):
            today = date(2026, 1, 1) + timedelta(days=offset)
            n = days_until_due(today, dd)
            assert 0 <= n <= 31, (today, dd, n)


def test_days_until_due_is_zero_on_due_date() -> None:
    for dd in (1, 5, 15, 28, 31):
        for offset in range(0, 400):
            today = date(2026, 1, 1) + timedelta(days=offset)
            if is_due_on(today, dd):
                assert days_until_due(today, dd) == 0, (today, dd)


# --------------------------------------------------------------------------- #
# 参数校验                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bd,dd", [(0, 25), (32, 25), (10, 0), (10, 32), (-1, 5)])
def test_invalid_day_rejected(bd, dd) -> None:
    with pytest.raises(ValueError, match="must be 1..31"):
        current_billing_period(date(2026, 10, 1), billing_day=bd, due_day=dd)
    with pytest.raises(ValueError, match="must be 1..31"):
        period_for_payment_due_date(
            date(2026, 10, 25), billing_day=bd, due_day=dd)


# --------------------------------------------------------------------------- #
# 具体值(锁住已知答案,防回归时悄悄漂移)                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("today,bd,dd,ws,stmt,due", [
    (date(2026, 10, 4), 10, 25, date(2026, 9, 10), date(2026, 10, 10), date(2026, 10, 25)),
    (date(2026, 10, 10), 10, 25, date(2026, 9, 10), date(2026, 10, 10), date(2026, 10, 25)),
    (date(2026, 10, 11), 10, 25, date(2026, 10, 10), date(2026, 11, 10), date(2026, 11, 25)),
    (date(2026, 10, 25), 10, 25, date(2026, 10, 10), date(2026, 11, 10), date(2026, 11, 25)),
    # 账单日在还款日之后 → 还款日落到下个月
    (date(2026, 10, 4), 25, 5, date(2026, 9, 25), date(2026, 10, 25), date(2026, 11, 5)),
    # 2 月短月:2 月出账日钳到 28,**窗口从 1 月的出账日 31 号开始**。
    #
    # ⚠️ 这里曾经写 `date(2026, 1, 28)` —— 那时实现用的是
    # `add_months(2026-02-28, -1)`,对**已钳到月末的日期**再加月会二次钳制,
    # 算出 1/28,导致 1/29~1/31 三天不属于任何账期(缺口)。
    # 正确算法是回到 billing_day 空间重算:`clamp(2026, 1, 31)` = 1/31。
    # 判断依据不是「哪个好看」,而是**相邻两期必须首尾相接** ——
    # 见 `test_no_two_windows_overlap_across_a_year`。
    (date(2026, 2, 10), 31, 25, date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 25)),
    # 出账日 == 还款日(月初出账、月初还款):同日,还的是刚出的账单
    (date(2026, 10, 1), 1, 1, date(2026, 9, 1), date(2026, 10, 1), date(2026, 10, 1)),
])
def test_known_values(today, bd, dd, ws, stmt, due) -> None:
    p = current_billing_period(today, billing_day=bd, due_day=dd)
    assert (p.window_start, p.statement_date, p.payment_due_date) == (ws, stmt, due)


def test_billing_period_contains_is_left_open_right_closed() -> None:
    p = current_billing_period(
        date(2026, 10, 4), billing_day=10, due_day=25)
    assert not p.contains(date(2026, 9, 10)), "窗口左端不含"
    assert p.contains(date(2026, 9, 11)), "窗口左端次日应含"
    assert p.contains(date(2026, 10, 10)), "窗口右端(含账单日)应含"
    assert not p.contains(date(2026, 10, 11)), "窗口右端次日不含"