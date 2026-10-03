"""「本期应还」的金额计算(阶段 1b)。

## 四条规则,漏一条就静默算错

**没有异常、没有日志**,只表现为还少了或多还了钱。这是整个自动还款方案里
最容易出错的地方,所以每条都有独立测试。

### 规则 1:用 `amount` **原币**,禁用 `coalesce(native_amount, amount)`

账户维度余额**故意**读原币 —— `read/_shared.py:452-455` 的口径注释:

> 金额读 **`amount` 原币**,不折本位币 —— 与 `_projection_totals` 的
> 账本维度口径**故意不同**,那里折本位币,这里不折(账户是原币余额)。

`credit_limit` / `initial_balance` 也是原币。只有同币种才对得上。

### 规则 2:**必须 join 组合支付腿**

0021(组合支付)起,有腿时父交易的 `account_sync_id` 被 mutator **强制清空**
(`snapshot_mutator.py`,防止余额双倍扣)。所以

```sql
WHERE account_sync_id = <卡>
```

会**整段漏掉**组合支付刷的金额 —— 而组合支付恰恰是「一张卡 + 现金」这种
最常见的分期场景。必须另外查 `read_tx_split_projection`。

### 规则 3:**不过滤** `exclude_from_stats` / `exclude_from_budget`

那两个字段的语义是「不计收支统计 / 不计预算用量」,**不是「没刷这笔钱」**。
银行照样要你还。过滤掉就少还。

### 规则 4:账单金额**不减**窗口内的还款

窗口 `(prev_statement, statement]` 内的转账**还的是上一期账单**。如果从
本期账单里减掉它,会每月少还上期滚存的部分,**且逐月累积** —— 不报错,
只是欠款慢慢变大。

## 公式

```
bill  = max(0, -(余额@账单日))           账单 = 账单日那一刻的欠款,欠款滚存
应还  = max(0, bill - 账单日之后的还款)    手动早还的部分要扣掉
```

用「余额@账单日」而不是「窗口内消费」,是为了让上期未还的部分**滚入**
本期账单,与真实信用卡一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .billing import BillingPeriod

# 金额容差。与 `snapshot_mutator._SPLIT_SUM_TOLERANCE` 同一个量级。
_TOL = Decimal("0.005")


def _to_decimal(v: Any) -> Decimal:
    """转 Decimal,避免浮点累积误差。

    信用卡账单是**逐期累加**的:一个月漏 0.01、十二个月就是 1.2 円。
    浮点在这种场景下的误差会被时间放大,所以这里全程 Decimal,
    只在最后返回 float(给 API 用)。
    """
    if v is None:
        return Decimal(0)
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001 - 脏数据不该让整个账单算不出来
        return Decimal(0)


@dataclass(frozen=True)
class StatementAmount:
    """一个账期的账单构成。字段全部是**原币**。"""

    #: 账单日那一刻的欠款(正数 = 要还多少)。已滚存上期未还部分。
    bill: Decimal
    #: 账单日**之后**已还的金额(手动早还 / 之前自动还过)
    repaid_after_statement: Decimal
    #: 本期新增消费(窗口内的 expense,含组合支付腿)
    period_spend: Decimal
    #: 窗口内转入该卡的转账(还的是**上期**账单,不抵本期)
    period_repayment: Decimal

    @property
    def outstanding(self) -> Decimal:
        """本期还欠多少。0 表示无需还款 —— 调度直接跳过。"""
        return max(Decimal(0), self.bill - self.repaid_after_statement)

    def as_dict(self) -> dict[str, float]:
        return {
            "bill": float(self.bill),
            "repaid_after_statement": float(self.repaid_after_statement),
            "period_spend": float(self.period_spend),
            "period_repayment": float(self.period_repayment),
            "outstanding": float(self.outstanding),
        }


def _local_date(dt: datetime | None) -> date | None:
    """时间戳 → 本地日期。

    `happened_at` 存的是 UTC。账期是**本地语义** —— 用户在东京的 25 号
    刷卡,无论服务器在哪个时区,都应该落进 25 号那期。所以这里取
    `dt.date()`(SQLAlchemy 读出来的 datetime 已经带时区信息,
    `.date()` 给出的是该 datetime 对象自身表示的日期)。
    """
    return dt.date() if dt is not None else None


def compute_statement(
    db: Session,
    *,
    user_id: str,
    ledger_id: str,
    card_account_sync_id: str,
    period: BillingPeriod,
    today: date,
) -> StatementAmount:
    """算出这个账期的账单构成。

    **四条规则的实现点都在这里**,每条都有独立测试:
    规则 1 → `amount` 而非 `coalesce(native_amount, amount)`
    规则 2 → `_split_spend_in_period` 单独查腿
    规则 3 → 查询**不带** `exclude_from_*` 条件
    规则 4 → `period_repayment` 与 `repaid_after_statement` 分开算
    """
    win_start_dt = datetime.combine(period.window_start, datetime.min.time())
    stmt_dt = datetime.combine(period.statement_date, datetime.max.time())
    today_dt = datetime.combine(today, datetime.max.time())

    initial = _initial_balance(db, user_id=user_id,
                               account_sync_id=card_account_sync_id)

    # ---- 规则 1:金额一律读 `amount` 原币 -------------------------------- #
    # 账户维度余额是原币口径(见模块头)。`native_amount` 是**账本维度**的
    # 折算值,在这里混用会让多币种账本里「日元卡刷美元」算成折算后金额,
    # 与 `credit_limit`(原币)对不上。
    #
    # ⚠️ **累计口径,不是窗口口径**。第一版写成「期初 + 窗口内变动」,
    # 结果丢掉两类真实存在的钱:
    #   1. 窗口**之前**的转账(还了更早的账单)—— 欠款因此减少,账单应变小
    #   2. 从卡里**转出**的钱 —— 余额增加,欠款同样减少
    # 两者都不是「统计口径」问题,是**算错了欠款**,且不报错。
    initial = _initial_balance(db, user_id=user_id,
                               account_sync_id=card_account_sync_id)
    balance_at_statement = _balance_at(
        db, ledger_id=ledger_id, account_sync_id=card_account_sync_id,
        initial=initial, as_of=stmt_dt, as_of_date=period.statement_date,
    )

    # ---- 规则 3:不过滤 exclude_from_* ---------------------------------- #
    # `exclude_from_stats` = 不计收支统计,`exclude_from_budget` = 不计预算。
    # 都不是「没刷这笔钱」。银行照样要还。下面所有查询都不带这两个条件。
    #
    # 以下两项是**展示用**(UI 上告诉用户「账单是怎么来的」),
    # 不参与账单计算 —— 账单由上面的累计余额唯一决定。
    period_repayment = _transfer_in(
        db, ledger_id=ledger_id, account_sync_id=card_account_sync_id,
        start_dt=win_start_dt, end_dt=stmt_dt)
    period_spend = _period_spend(db, ledger_id=ledger_id,
                                 account_sync_id=card_account_sync_id,
                                 start_dt=win_start_dt, end_dt=stmt_dt)
    split_spend = _split_spend_in_period(
        db, ledger_id=ledger_id, account_sync_id=card_account_sync_id,
        start_date=period.window_start, end_date=period.statement_date)

    # 规则 4:账单日**之后**的还款(手动早还)要扣掉。窗口内的还款已经
    # 反映在累计余额里(它还的是更早的账单),这里**不能**再减一次 ——
    # 那会让上期滚存的欠款被吃掉,欠款逐月变小却不报错。
    repaid_after = _transfer_in(
        db, ledger_id=ledger_id, account_sync_id=card_account_sync_id,
        start_dt=stmt_dt, end_dt=today_dt)

    bill = max(Decimal(0), -balance_at_statement)

    return StatementAmount(
        bill=bill,
        repaid_after_statement=repaid_after,
        period_spend=period_spend + split_spend,
        period_repayment=period_repayment,
    )


def _balance_at(db: Session, *, ledger_id: str, account_sync_id: str,
                initial: Decimal, as_of: datetime,
                as_of_date: date) -> Decimal:
    """**截止 `as_of`** 的账户余额(含期初)。

    规则 1(读 `amount` 原币)、规则 2(组合支付腿)、规则 3(不过滤
    `exclude_from_*`)全部体现在这几条查询里。

    方向(信用卡余额为负 = 欠款):
      expense      → `-`(消费增加欠款)
      income       → `+`(退款冲减)
      transfer in  → `+`(还款)
      transfer out → `-`(从卡里转出,余额增加、欠款减少)

    组合支付腿**按父交易的时间**归期 —— 腿表本身不存时间。
    """
    from ...models import ReadTxProjection

    rows = db.execute(
        select(
            ReadTxProjection.tx_type,
            ReadTxProjection.amount,
            ReadTxProjection.account_sync_id,
            ReadTxProjection.from_account_sync_id,
            ReadTxProjection.to_account_sync_id,
        ).where(ReadTxProjection.ledger_id == ledger_id)
        .where(ReadTxProjection.happened_at <= as_of)
    ).all()

    balance = initial
    for tx_type, amount, acct, frm, to in rows:
        amt = _to_decimal(amount)
        if acct == account_sync_id:
            if tx_type == "expense":
                balance -= amt
            elif tx_type == "income":
                balance += amt
        elif frm == account_sync_id:
            balance -= amt          # 转出卡 → 余额增加 → 欠款减少
        elif to == account_sync_id:
            balance += amt          # 转入卡 → 还款

    balance -= _split_spend_upto(
        db, ledger_id=ledger_id, account_sync_id=account_sync_id,
        as_of=as_of, as_of_date=as_of_date)
    return balance


def _split_spend_upto(db: Session, *, ledger_id: str,
                      account_sync_id: str, as_of: datetime,
                      as_of_date: date) -> Decimal:
    """规则 2:截止 `as_of` 挂在该卡上的组合支付腿合计。

    0021 起组合支付父交易的 `account_sync_id` 被强制清空
    (`snapshot_mutator.py`,防止余额双倍扣),所以上面那条按
    `account_sync_id` 扫的查询**完全看不到腿**。不单独查这一笔,
    「一张卡 + 现金」这种最常见的分期场景会整段漏算 —— 少还钱且不报错。

    分两步:腿表**不存时间**,时间只在父交易上。
    """
    from ...models import ReadTxProjection, ReadTxSplitProjection

    rows = db.execute(
        select(ReadTxSplitProjection.amount)
        .join(
            ReadTxProjection,
            (ReadTxProjection.ledger_id == ReadTxSplitProjection.ledger_id)
            & (ReadTxProjection.sync_id == ReadTxSplitProjection.tx_sync_id),
        )
        .where(ReadTxSplitProjection.ledger_id == ledger_id)
        .where(ReadTxSplitProjection.account_sync_id == account_sync_id)
        .where(ReadTxProjection.happened_at <= as_of)
    ).all()
    return sum((_to_decimal(r[0]) for r in rows), Decimal(0))


# --------------------------------------------------------------------------- #
# 下面全是单表小查询。每条对应上面一条规则,不做泛化。                        #
# --------------------------------------------------------------------------- #


def _initial_balance(db: Session, *, user_id: str,
                     account_sync_id: str) -> Decimal:
    from ...models import UserAccountProjection

    row = db.scalar(
        select(UserAccountProjection.initial_balance)
        .where(UserAccountProjection.user_id == user_id)
        .where(UserAccountProjection.sync_id == account_sync_id)
    )
    return _to_decimal(row)


def _period_spend(db: Session, *, ledger_id: str, account_sync_id: str,
                  start_dt: datetime, end_dt: datetime) -> Decimal:
    """窗口内该卡的支出合计(规则 1 + 规则 3)。"""
    from ...models import ReadTxProjection

    rows = db.scalars(
        select(ReadTxProjection.amount)
        .where(ReadTxProjection.ledger_id == ledger_id)
        .where(ReadTxProjection.account_sync_id == account_sync_id)
        .where(ReadTxProjection.tx_type == "expense")
        .where(ReadTxProjection.happened_at > start_dt)
        .where(ReadTxProjection.happened_at <= end_dt)
    ).all()
    return sum((_to_decimal(r) for r in rows), Decimal(0))


def _income_in(db: Session, *, ledger_id: str, account_sync_id: str,
               start_dt: datetime, end_dt: datetime) -> Decimal:
    from ...models import ReadTxProjection

    rows = db.scalars(
        select(ReadTxProjection.amount)
        .where(ReadTxProjection.ledger_id == ledger_id)
        .where(ReadTxProjection.account_sync_id == account_sync_id)
        .where(ReadTxProjection.tx_type == "income")
        .where(ReadTxProjection.happened_at > start_dt)
        .where(ReadTxProjection.happened_at <= end_dt)
    ).all()
    return sum((_to_decimal(r) for r in rows), Decimal(0))


def _transfer_in(db: Session, *, ledger_id: str, account_sync_id: str,
                 start_dt: datetime, end_dt: datetime) -> Decimal:
    """区间内**转入**该卡的转账合计。

    注意区间是 `(start, end]` —— 与账期窗口左开右闭一致。
    """
    from ...models import ReadTxProjection

    rows = db.scalars(
        select(ReadTxProjection.amount)
        .where(ReadTxProjection.ledger_id == ledger_id)
        .where(ReadTxProjection.to_account_sync_id == account_sync_id)
        .where(ReadTxProjection.tx_type == "transfer")
        .where(ReadTxProjection.happened_at > start_dt)
        .where(ReadTxProjection.happened_at <= end_dt)
    ).all()
    return sum((_to_decimal(r) for r in rows), Decimal(0))


def _split_spend_in_period(db: Session, *, ledger_id: str,
                           account_sync_id: str,
                           start_date: date, end_date: date) -> Decimal:
    """规则 2:窗口内**挂在该卡上的组合支付腿**合计。

    0021 起组合支付父交易的 `account_sync_id` 被强制清空,所以 `_period_spend`
    完全扫不到腿。必须单独查 `read_tx_split_projection`,再按父交易的
    `happened_at` 归入账期。

    分两步是因为腿表本身**不存时间** —— 时间只存在于父交易上。

    ⚠️ 这是本模块里唯一的 N+1 风险点:按账期算一笔账单需要把所有父交易
    的 sync_id 取出来做 IN 查询。当前账期最多几十条,可接受;若将来
    单账期超过几千笔,需要改成 JOIN。
    """
    from ...models import ReadTxProjection, ReadTxSplitProjection

    tx_rows = db.execute(
        select(
            ReadTxSplitProjection.tx_sync_id,
            ReadTxSplitProjection.amount,
        )
        .join(
            ReadTxProjection,
            (ReadTxProjection.ledger_id == ReadTxSplitProjection.ledger_id)
            & (ReadTxProjection.sync_id == ReadTxSplitProjection.tx_sync_id),
        )
        .where(ReadTxSplitProjection.ledger_id == ledger_id)
        .where(ReadTxSplitProjection.account_sync_id == account_sync_id)
        .where(ReadTxProjection.happened_at > datetime.combine(
            start_date, datetime.min.time()))
        .where(ReadTxProjection.happened_at <= datetime.combine(
            end_date, datetime.max.time()))
    ).all()
    return sum((_to_decimal(r[1]) for r in tx_rows), Decimal(0))