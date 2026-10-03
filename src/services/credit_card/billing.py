"""信用卡账期窗口的**纯日期运算**(阶段 1)。

## 为什么单独拆一个零 IO 的模块

「本期应还」的金额部分必须查库(交易、拆分腿),但**日期部分不需要**。
把两者拆开的好处是:日期边界可以在没有数据库、没有账本、没有交易的
情况下被穷举验证 —— 而日期边界恰恰是最容易错的地方(2 月、短月、
账单日与还款日跨月)。

## 三个口径,禁止互相引用

本模块定义两个口径:

- **顺延月末**(`billing_day` / `payment_due_day`):目标日 > 当月最大天数
  时取当月最后一天。**用户已拍板**。
- 短月推演结论:`billing_day=31` 遇 2 月 → 账单日 28,**漂移不累积**
  (每年 2 月短一个月,其余月份归位)。已用连续 15 个月推演验证。

⚠️ **预算的口径相反**:`month_start_day` 在读端被**钳到 [1,28]**
(`read/_shared.py:702-704`),2 月按 28 号算。预算是「保守钳 28」,
自动还款是「顺延月末」。**两个模块互相引用会把语义搅浑** —— 禁止。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


def clamp_to_month_end(year: int, month: int, day: int) -> date:
    """目标日超过当月最大天数时取**当月最后一天**。

    `billing_day=31` + 2026-02 → 2026-02-28。不是「跳过这个月」,也不是
    「顺延到 3/3」—— 这两种都会让账单周期长度失真。
    """
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def add_months(d: date, n: int) -> date:
    """加 n 个月,目标日超出当月时钳到月末。

    `2026-01-31 + 1 月 = 2026-02-28`。钳到月末(而不是溢出到 3/3)是必要的:
    账单日序列必须单调且不重不漏,否则账期窗口会互相覆盖或留缝。
    """
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return clamp_to_month_end(y, m, d.day)


def is_due_on(today: date, due_day: int) -> bool:
    """今天是否还款日。

    短月顺延:`due_day=31` 在 2 月 → 顺延到 28,当天仍算还款日。
    """
    return today == clamp_to_month_end(today.year, today.month, due_day)


def days_until_due(today: date, due_day: int) -> int:
    """距下一个还款日的天数(0 = 今天就是还款日)。

    与前端 `AccountDetailDialog.tsx:332-347` 的 `daysUntilDay()` 语义一致,
    但那个函数只用于**展示**(标红 ≤3 天),不能作为执行日期的依据 ——
    这里才是调度用的权威实现。
    """
    this_month = clamp_to_month_end(today.year, today.month, due_day)
    if today <= this_month:
        return (this_month - today).days
    return (add_months(this_month, 1) - today).days


@dataclass(frozen=True)
class BillingPeriod:
    """一个账期窗口。

    `window_start` **不含**(左开),`statement_date` **含**(右闭)——
    即 `(window_start, statement_date]`。左开右闭保证相邻两期的交易
    归属唯一,不会有一笔落在两个窗口里(被算两次)或都不落(漏算)。

    `payment_due_date` 是这张账单该还的日子。它可能**落在下一个自然月**:
    `billing_day=25` / `due_day=5` → 25 号出账,次月 5 号还款。
    """

    statement_date: date
    window_start: date
    payment_due_date: date

    def contains(self, d: date) -> bool:
        """`d` 是否落在本窗口内(左开右闭)。

        用到账期的**时间戳**要转成本地日期后再调。
        """
        return self.window_start < d <= self.statement_date


def current_billing_period(
    today: date, *, billing_day: int, due_day: int
) -> BillingPeriod:
    """算出 `today` 所处的账期。

    「今天之前(含)最近的账单日」作为本期 `statement_date`。注意是**含今天**:
    账单日当天产生的消费属于**本期**账单(银行的口径),不是下一期。

    `window_start` = 上期账单日(同样钳到月末),左开。
    `payment_due_date` = 本期账单日的下一个月里的还款日。
    """
    if not (1 <= billing_day <= 31):
        raise ValueError(f"billing_day must be 1..31, got {billing_day}")
    if not (1 <= due_day <= 31):
        raise ValueError(f"payment_due_day must be 1..31, got {due_day}")

    this_month_anchor = clamp_to_month_end(today.year, today.month, billing_day)
    # 账单日当天含今天 → `>` 而不是 `>=`
    statement = this_month_anchor if today <= this_month_anchor else add_months(
        this_month_anchor, 1
    )
    # 上一期出账日 —— 必须在 billing_day 空间重算,不能 add_months(statement, -1)
    window_start = _previous_statement(statement, billing_day)

    # 还款日跟着账单日走 —— 与反查路径共用同一个 `_payment_due_after`。
    # 两处各写一套是重复实现的温床:第一版正向用 `> statement`、反向用
    # `>= statement`,在 `billing_day == payment_due_day` 时两者给出不同
    # 的窗口,且都不报错。
    due = _payment_due_after(statement, due_day)
    return BillingPeriod(
        statement_date=statement, window_start=window_start, payment_due_date=due
    )


def _previous_statement(statement: date, billing_day: int) -> date:
    """上一期的出账日。

    ⚠️ **不能**写成 `add_months(statement, -1)` —— 那是对**已经钳到月末的**
    日期再加月,会**二次钳制**,导致账期窗口重叠。

    实例(`billing_day=29`):
    - 1 月出账日 = `clamp(2026, 1, 29)` = 2026-01-29
    - 2 月出账日 = `clamp(2026, 2, 29)` = 2026-02-28
    - 若用 `add_months(2026-02-28, -1)` → 再钳一次 → **2026-01-28**

    于是 2 月窗口从 1/28 开始,而 1 月窗口到 1/29 结束 —— **重叠 2 天**,
    落在那两天的消费会被两个账期同时算,即**还款额翻倍**。不报错。

    正确做法:回到**原始 `billing_day` 空间**,在上一个月重新钳一次。
    """
    y, m = _shift_month(statement.year, statement.month, -1)
    return clamp_to_month_end(y, m, billing_day)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    m = month - 1 + delta
    return year + m // 12, m % 12 + 1


def period_for_payment_due_date(
    due_date: date, *, billing_day: int, due_day: int
) -> BillingPeriod | None:
    """反查:某张账单(还款日 = `due_date`)对应的账期窗口。

    调度器用这个 —— 它先算出「今天是还款日」,再反查**该还的是哪一期**的账单。
    正向 `current_billing_period(today)` 会得到**下一期**(出账日已经过去了),
    还款对象会错一期。这是这个函数存在的唯一理由。

    返回 `None` 表示**孤儿还款日** —— 该还款日不偿还任何账单,调度应跳过。
    见下面 `_payment_due_after` 不匹配时的说明。

    算法:找到不晚于 `due_date` 的最近一个账单日。它就是本期出账日 ——
    因为 `clamp_to_month_end` 产生的账单日序列按月严格递增,所以「最近的
    那个」唯一确定。

    反向查找**不能**用「due_date 减一个月」—— 账单日与还款日的日号不同
    (`billing_day=10` / `due_day=25` 时,10/25 还的是 10/10 出的账单,
    不是 9/25 出的)。第一版就是这么写的,结果错了一期。
    """
    if not (1 <= billing_day <= 31):
        raise ValueError(f"billing_day must be 1..31, got {billing_day}")
    if not (1 <= due_day <= 31):
        raise ValueError(f"payment_due_day must be 1..31, got {due_day}")
    if not is_due_on(due_date, due_day):
        raise ValueError(f"{due_date} is not a due date for due_day={due_day}")

    for back in range(0, 4):
        y, m = _shift_month(due_date.year, due_date.month, -back)
        cand = clamp_to_month_end(y, m, billing_day)
        # `<=` 而非 `<`:`billing_day == payment_due_day` 时账单日**就是**
        # 还款日(月初出账、月初还款),`cand < due_date` 永远不成立,
        # 反查会一路抛到 4 个月上限。用 `<=` 后两种情形统一,
        # 再靠下面的自校验确认唯一性。
        if cand <= due_date:
            actual = _payment_due_after(cand, due_day)
            if actual != due_date:
                # **孤儿还款日**:`billing_day=30, due_day=29` 时,2 月账单
                # 被钳到 2/28 出账、同日(2/28)还款,3 月账单 4/29 才还 ——
                # 于是 **3/29 不偿还任何账单**。这是「两侧都钳到月末」的
                # 必然结果,不是配置错误。
                #
                # 正确处理是**如实返回「本月无需还款」**,让调度跳过。
                # 抛异常会让整个调度任务被这个账户拖垮。
                return None
            return BillingPeriod(
                statement_date=cand,
                window_start=_previous_statement(cand, billing_day),
                payment_due_date=due_date,
            )
    raise ValueError(  # pragma: no cover - 4 个月找不到账单日不可能发生
        f"no statement date within 3 months before {due_date} "
        f"(billing_day={billing_day})"
    )


def _payment_due_after(statement: date, due_day: int) -> date:
    """出账日 `statement` 对应的还款日 = 首个**不早于** `statement` 的
    `due_day` 钳位日。

    ## 允许同日(`billing_day == payment_due_day`)

    `billing_day=1 / due_day=1` 时出账日和还款日同一天。这是**合法配置**
    ——「月初出账、月初还款」在现实里很常见。所以这里用 `>=` 而不是 `>`。

    同日时还的是**本期**账单:账单在 `dd` 号出账,当天还款,「当月出当月还」
    是现实里最常见的配置。

    ⚠️ 短月会打破直觉:`billing_day=31, due_day=28`,2027 年 2 月的账单日
    钳到 2/28,还款日也是 2/28 —— 同日。这里比较的是**钳位之后的日期**,
    不是原始日号,所以判断依然正确。
    """
    due_in_month = clamp_to_month_end(statement.year, statement.month, due_day)
    return due_in_month if due_in_month >= statement else add_months(due_in_month, 1)