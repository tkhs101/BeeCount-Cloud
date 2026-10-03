"""「本期应还」的四条金额规则(阶段 1b)。

## 这个测试文件的作用

`statement.py` 的四条规则里,任何一条写错都**不会抛异常、不会打日志** ——
只表现为还少了或多还了钱。所以每条规则都有一个**判别性**测试:
把该条规则改错,只有那一条变红。

| 规则 | 测试 | 改错后的现象 |
|---|---|---|
| 1 用 `amount` 原币 | `test_rule1_uses_amount_not_native_amount` | 多币种账本里账单金额翻倍/对不上额度 |
| 2 必须 join 组合支付腿 | `test_rule2_split_legs_are_counted` | 一张卡 + 现金的组合支付**整段漏算**,少还 |
| 3 不过滤 `exclude_from_*` | `test_rule3_exclude_flags_do_not_hide_spend` | 用户标记「不计统计」的刷款漏掉,少还 |
| 4 账单不减窗口内还款 | `test_rule4_rolling_debt_is_preserved` | 上期未还被吃掉,**欠款逐月变小** |

## 为什么规则 2 值得单独一条

组合支付(0021)在本 fork 里的契约是「有腿时父交易的 `account_sync_id` 被
强制清空」(否则余额双倍扣)。所以任何 `WHERE account_sync_id = <卡>` 的
统计都**扫不到腿**。这正是「组合支付功能反过来咬自动还款一口」——
两个各自都正确的设计,在组合时产生了漏洞。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models import (
    ReadTxProjection,
    ReadTxSplitProjection,
    UserAccountProjection,
)
from src.services.credit_card.billing import (
    current_billing_period,
    period_for_payment_due_date,
)
from src.services.credit_card.statement import compute_statement


# --------------------------------------------------------------------------- #
# 夹具                                                                        #
# --------------------------------------------------------------------------- #


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TS() as session:
        yield session


LEDGER = "lg1"
CARD = "acc-card"


def _account(db, *, sync_id=CARD, initial=0.0, currency="JPY",
             name="招行信用卡", account_type="credit_card"):
    db.add(UserAccountProjection(
        user_id="u1", sync_id=sync_id, name=name, account_type=account_type,
        currency=currency, initial_balance=initial, source_change_id=1,
    ))


def _tx(db, sync_id, *, tx_type="expense", amount=0.0, when: date,
        account=CARD, native=None, exclude_stats=False,
        exclude_budget=False, frm=None, to=None):
    """直接写 projection 行(绕过 mutator)—— 这样能精确控制字段值。

    真实写入路径会强制清空组合支付父交易的 `account_sync_id`,这里为了
    构造各种边界直接写库。要构造「组合支付」用 `_split` 而不是这个函数。
    """
    db.add(ReadTxProjection(
        ledger_id=LEDGER, sync_id=sync_id, user_id="u1",
        tx_type=tx_type, amount=amount,
        account_sync_id=account, account_name=None,
        from_account_sync_id=frm, to_account_sync_id=to,
        happened_at=datetime(when.year, when.month, when.day,
                              tzinfo=timezone.utc),
        native_amount=native,
        exclude_from_stats=exclude_stats,
        exclude_from_budget=exclude_budget,
        source_change_id=1,
    ))


def _split(db, tx_sync_id, *, seq, account, amount, when: date):
    """造一笔组合支付:父交易 account_sync_id 为空,腿挂在卡上。"""
    db.add(ReadTxProjection(
        ledger_id=LEDGER, sync_id=tx_sync_id, user_id="u1",
        tx_type="expense", amount=amount,
        account_sync_id=None,          # ← 0021 的契约:有腿时父账户为空
        happened_at=datetime(when.year, when.month, when.day,
                             tzinfo=timezone.utc),
        source_change_id=1,
    ))
    db.add(ReadTxSplitProjection(
        ledger_id=LEDGER, tx_sync_id=tx_sync_id, seq=seq,
        account_sync_id=account, amount=amount,
    ))


def _period(today=date(2026, 10, 25), bd=10, dd=25):
    """**到期日 `today` 偿还的那张账单**对应的账期。

    ⚠️ 这里必须用**反向**查找 `period_for_payment_due_date`,不能用正向的
    `current_billing_period`。到期日 10/25 属于账单日之后的**下一期**
    (10/10 ~ 11/10),而「10/25 要还的是 9/10 ~ 10/10 那张账单」。

    调度器也走反向 —— 这正是阶段 1a 修掉的那个「差一期」bug,
    在金额层同样会咬人。
    """
    p = period_for_payment_due_date(today, billing_day=bd, due_day=dd)
    assert p is not None, (today, bd, dd)
    return p


def _calc(db, *, today=date(2026, 10, 25), bd=10, dd=25, card=CARD):
    return compute_statement(
        db, user_id="u1", ledger_id=LEDGER,
        card_account_sync_id=card, period=_period(today, bd, dd), today=today,
    )


# --------------------------------------------------------------------------- #
# 基线:一切正常时                                                             #
# --------------------------------------------------------------------------- #


def test_baseline_full_repayment(db) -> None:
    """上期还清 + 本期刷 5000 → 账单 5000,应还 5000。"""
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))     # 窗口内
    db.commit()
    r = _calc(db)
    assert r.bill == Decimal(5000), r.as_dict()
    assert r.outstanding == Decimal(5000), r.as_dict()


def test_baseline_nothing_spent(db) -> None:
    _account(db)
    db.commit()
    assert _calc(db).outstanding == Decimal(0)


def test_overspend_is_not_a_bill(db) -> None:
    """溢缴(余额为正)→ 无账单。不加这个 max(0,...) 就会算出负的应还。"""
    _account(db, initial=10000.0)
    db.commit()
    r = _calc(db)
    assert r.bill == Decimal(0), r.as_dict()
    assert r.outstanding == Decimal(0), r.as_dict()


# --------------------------------------------------------------------------- #
# 规则 1:用 amount 原币,不用 native_amount                                   #
# --------------------------------------------------------------------------- #


def test_rule1_uses_amount_not_native_amount(db) -> None:
    """日元卡刷美元:账单按**原币**算,不按折算后的 native_amount。

    `credit_limit` / `initial_balance` 都是原币。折算值是**账本维度**口径
    (`read/_shared.py:452-455`:账户维度「故意」不折)。混用会让多币种账本
    的账单金额对不上额度。
    """
    _account(db, currency="JPY")
    _tx(db, "t1", amount=100.0, native=150.0, when=date(2026, 9, 20))
    db.commit()
    r = _calc(db)
    assert r.period_spend == Decimal(100), (
        f"账单用了折算值 native_amount=150,应使用原币 amount=100。"
        f"实际 {r.as_dict()}"
    )
    assert r.bill == Decimal(100), r.as_dict()


# --------------------------------------------------------------------------- #
# 规则 2:必须 join 组合支付腿                                                 #
# --------------------------------------------------------------------------- #


def test_rule2_split_legs_are_counted(db) -> None:
    """组合支付:父交易 `account_sync_id` 为空,腿挂在卡上 → 必须计入账单。

    这是 0021 与自动还款的**交叉漏洞**:两个设计各自都正确,组合起来时
    「有腿清空父账户」让 `WHERE account_sync_id = 卡` 扫不到腿。
    """
    _account(db)
    _split(db, "t1", seq=0, account=CARD, amount=3000.0, when=date(2026, 9, 20))
    db.commit()
    r = _calc(db)
    assert r.period_spend == Decimal(3000), (
        f"组合支付腿被漏算(父交易 account_sync_id 为空)。实际 {r.as_dict()}"
    )
    assert r.bill == Decimal(3000), r.as_dict()


def test_rule2_split_leg_on_other_account_not_counted(db) -> None:
    """腿挂在**别的**账户上 → 不该计入这张卡。"""
    _account(db)
    _account(db, sync_id="acc-cash", name="现金", account_type="cash")
    _split(db, "t1", seq=0, account="acc-cash", amount=3000.0,
           when=date(2026, 9, 20))
    db.commit()
    assert _calc(db).bill == Decimal(0), "计入了别的账户的腿"


def test_rule2_split_uses_parent_tx_time_for_cutoff(db) -> None:
    """腿的时间**继承父交易** —— 腿表自己不存时间。

    账单日之后的组合支付**不计入**这张账单(它是下一期的消费)。断言这一点
    就是在钉住「按父交易 happened_at 截止」这个语义。

    注:早于窗口的腿(比如 8/20)**要**计入账单 —— 累计口径下它确实还是
    欠着的钱。账单日之前的一切消费都欠着,不管落在哪个窗口。
    """
    _account(db)
    _split(db, "t_late", seq=0, account=CARD, amount=3000.0,
           when=date(2026, 10, 20))     # 账单日(10/10)之后
    db.commit()
    assert _calc(db).bill == Decimal(0), (
        "账单日之后的组合支付被算进了这张账单 —— 它属于下一期")

    db.query(ReadTxSplitProjection).delete()
    db.query(ReadTxProjection).delete()
    _split(db, "t_early", seq=0, account=CARD, amount=3000.0,
           when=date(2026, 8, 20))      # 窗口之前,但账单日之前
    db.commit()
    assert _calc(db).bill == Decimal(3000), (
        "账单日之前的组合支付腿漏算了 —— 累计口径下它仍然欠着钱")


def test_rule2_split_and_plain_spend_add_up(db) -> None:
    _account(db)
    _tx(db, "t1", amount=2000.0, when=date(2026, 9, 20))
    _split(db, "t2", seq=0, account=CARD, amount=3000.0, when=date(2026, 9, 21))
    db.commit()
    assert _calc(db).period_spend == Decimal(5000), (
        f"普通消费与组合支付腿没有正确相加:{_calc(db).as_dict()}"
    )


# --------------------------------------------------------------------------- #
# 规则 3:不过滤 exclude_from_*                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("flag", ["exclude_stats", "exclude_budget"])
def test_rule3_exclude_flags_do_not_hide_spend(db, flag) -> None:
    """「不计收支统计 / 不计预算」≠「没刷这笔钱」。银行照样要还。

    这两个字段的语义在 `models.py` 里写得很清楚 —— 它们只影响**统计与
    预算**,不影响余额和账单。
    """
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20), **{flag: True})
    db.commit()
    r = _calc(db)
    assert r.bill == Decimal(5000), (
        f"{flag}=True 的消费被排除出账单 —— 那是统计标记,不是「没刷」。"
        f"实际 {r.as_dict()}"
    )


# --------------------------------------------------------------------------- #
# 规则 4:账单不减窗口内还款(欠款滚存)                                       #
# --------------------------------------------------------------------------- #


def test_rule4_rolling_debt_is_preserved(db) -> None:
    """上期没还 + 本期又刷 → 本期账单要**包含**上期欠款。

    场景:期初欠 3000(负余额),本期再刷 5000。
    账单日那一刻欠 8000,账单就是 8000 —— 而不是 5000。
    """
    _account(db, initial=-3000.0)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    db.commit()
    r = _calc(db)
    assert r.period_spend == Decimal(5000), r.as_dict()
    assert r.bill == Decimal(8000), (
        f"上期欠的 3000 被吃掉了 —— 账单应为 3000+5000=8000。实际 {r.as_dict()}"
    )


def test_rule4_payment_in_window_reduces_owed_but_not_bill(db) -> None:
    """窗口内的还款**还的是上期账单**,所以它减少欠款但已被账单吸收。

    期初欠 3000,窗口内还了 3000(还的是更早的账单),本期又刷 5000。
    账单日时余额 = -5000 → 账单 5000。
    """
    _account(db, initial=-3000.0)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    _tx(db, "p1", tx_type="transfer", amount=3000.0, when=date(2026, 9, 15),
        account=None, to=CARD)
    db.commit()
    r = _calc(db)
    assert r.period_repayment == Decimal(3000), r.as_dict()
    assert r.bill == Decimal(5000), (
        f"窗口内还款被重复扣了一次(它还的是上期账单,已反映在期初里)。"
        f"实际 {r.as_dict()}"
    )


def test_repaid_after_statement_reduces_outstanding(db) -> None:
    """账单日**之后**已还的金额要扣掉 → 手动还过就自动跳过。

    这是「识别手动已还过并跳过」的实现点:用户在账单日之后、还款日之前
    手动还了一部分,应还就相应减少。
    """
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    _tx(db, "p1", tx_type="transfer", amount=2000.0,
        when=date(2026, 10, 12),          # 账单日(10/10)之后,还款日(10/25)之前
        account=None, to=CARD)
    db.commit()
    r = _calc(db, today=date(2026, 10, 25))
    assert r.bill == Decimal(5000), r.as_dict()
    assert r.repaid_after_statement == Decimal(2000), r.as_dict()
    assert r.outstanding == Decimal(3000), r.as_dict()


def test_fully_repaid_manually_yields_zero_outstanding(db) -> None:
    """用户提前全额还清 → `outstanding == 0` → 调度跳过。"""
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    _tx(db, "p1", tx_type="transfer", amount=5000.0, when=date(2026, 10, 12),
        account=None, to=CARD)
    db.commit()
    r = _calc(db, today=date(2026, 10, 25))
    assert r.bill == Decimal(5000), r.as_dict()
    assert r.outstanding == Decimal(0), (
        f"用户已全额还清,应还应为 0(调度据此跳过)。实际 {r.as_dict()}"
    )


# --------------------------------------------------------------------------- #
# 窗口边界                                                                    #
# --------------------------------------------------------------------------- #


def test_window_is_left_open_right_closed(db) -> None:
    """窗口左端**不含**、右端(含账单日)**含**。

    左开右闭保证一笔消费恰好属于一个账期。左右都闭会让边界日被算两次,
    都不开会让边界日谁都不算。
    """
    _account(db)
    _tx(db, "t_left", amount=1000.0, when=date(2026, 9, 10))    # 左端
    _tx(db, "t_right", amount=2000.0, when=date(2026, 10, 10))   # 右端
    _tx(db, "t_out", amount=4000.0, when=date(2026, 10, 11))     # 右端次日
    db.commit()
    r = _calc(db)
    assert r.period_spend == Decimal(2000), (
        f"窗口边界处理不对,只应计入右端那笔 2000。实际 {r.as_dict()}"
    )


def test_repayment_inside_window_reduces_bill(db) -> None:
    """账单日**之前**的还款会减少账单 —— 那是在账单结算前还掉的。

    还款日 2026-10-25 偿还的账单窗口是 `(2026-09-10, 2026-10-10]`,
    所以 9/15 的还款在窗口内、9/5 的在窗口外(属于更早那一期)。
    """
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    _tx(db, "p0", tx_type="transfer", amount=1000.0, when=date(2026, 9, 15),
        account=None, to=CARD)
    db.commit()
    r = _calc(db)
    assert r.period_repayment == Decimal(1000), r.as_dict()
    assert r.bill == Decimal(4000), (
        f"账单日之前的还款没有减少欠款:{r.as_dict()}"
    )


def test_repayment_before_window_still_reduces_bill(db) -> None:
    """窗口**之前**的还款同样减少账单 —— 它还的是更早的账单。

    这条专门钉住「累计口径」。第一版把余额算成「期初 + 窗口内变动」,
    结果 9/5 那笔还款被整个丢掉,账单多算 1000。
    """
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    _tx(db, "p0", tx_type="transfer", amount=1000.0, when=date(2026, 9, 5),
        account=None, to=CARD)
    db.commit()
    r = _calc(db)
    assert r.period_repayment == Decimal(0), (
        f"9/5 在窗口外,不该计入 period_repayment:{r.as_dict()}")
    assert r.bill == Decimal(4000), (
        f"窗口之前的还款被丢掉了 —— 欠款算多了。实际 {r.as_dict()}"
    )


# --------------------------------------------------------------------------- #
# 收入/退款                                                                  #
# --------------------------------------------------------------------------- #


def test_refund_reduces_bill(db) -> None:
    """退款冲减欠款是常态,必须计入。"""
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    _tx(db, "i1", tx_type="income", amount=1200.0, when=date(2026, 9, 25))
    db.commit()
    assert _calc(db).bill == Decimal(3800), _calc(db).as_dict()


def test_transfer_out_of_card_increases_debt(db) -> None:
    """从卡里**转出** → 欠款**增加**。这不是 bug,是信用卡语义。

    账户余额的机械规则对所有账户统一(`read/_shared.py:494-506`:
    转出减、转入加)。对信用卡来说:

    - **转出**卡 = **取现 / 套现** → 钱从卡里拿走 → 欠更多 → 余额更负
    - **转入**卡 = **还款** → 钱还到卡上 → 欠款减少 → 余额向 0 靠近

    还信用卡必须记成「储蓄卡 → 信用卡」的**转入**,记成反向就变成取现了。
    这条钉住「累计口径要算转出」—— 第一版只累加 expense/income/transfer-in,
    转出卡的钱凭空消失。
    """
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    # transfer 用 from/to 两端,**不用** account_sync_id(那是 expense/income 的字段)
    _tx(db, "o1", tx_type="transfer", amount=1000.0, when=date(2026, 9, 25),
        account=None, frm=CARD, to="acc-savings")
    db.commit()
    assert _calc(db).bill == Decimal(6000), (
        f"从卡里转出 1000 相当于取现,欠款应从 5000 变成 6000:"
        f"{_calc(db).as_dict()}"
    )


def test_transfer_into_card_is_the_repayment_direction(db) -> None:
    """还信用卡 = **转入**这张卡。欠款减少。"""
    _account(db)
    _tx(db, "t1", amount=5000.0, when=date(2026, 9, 20))
    _tx(db, "p1", tx_type="transfer", amount=1000.0, when=date(2026, 9, 15),
        account=None, frm="acc-savings", to=CARD)
    db.commit()
    assert _calc(db).bill == Decimal(4000), _calc(db).as_dict()


# --------------------------------------------------------------------------- #
# 输出形态                                                                    #
# --------------------------------------------------------------------------- #


def test_as_dict_is_json_friendly(db) -> None:
    _account(db)
    _tx(db, "t1", amount=1234.56, when=date(2026, 9, 20))
    db.commit()
    d = _calc(db).as_dict()
    assert set(d) == {"bill", "repaid_after_statement", "period_spend",
                      "period_repayment", "outstanding"}
    assert all(isinstance(v, float) for v in d.values()), d


def test_decimal_avoids_float_drift(db) -> None:
    """逐期累加的金额必须用 Decimal —— 一个月漏 0.01,十二个月就是 1.2 円。"""
    _account(db)
    # 10 笔 0.1,浮点累加会得到 0.9999999999999999
    for i in range(10):
        _tx(db, f"t{i}", amount=0.1, when=date(2026, 9, 20 + (i % 3)))
    db.commit()
    assert _calc(db).period_spend == Decimal(1), (
        f"十笔 0.1 累加不等于 1:{_calc(db).as_dict()}"
    )
