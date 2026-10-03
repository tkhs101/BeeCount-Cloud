"""自动还款配置校验(阶段 2)。

## 核心事实:这三个字段从「装饰」升级为「驱动资金操作」

`billing_day` / `payment_due_day` / `credit_limit` 在本 fork 之前
**服务端零校验**(全仓 73 处命中全是 schema/CRUD/透传),只有前端拦。
它们当时只是展示字段,脏值无害 —— 账单日填 99,顶多显示错。

绑了自动还款之后:

| 脏值 | 后果 |
|---|---|
| `billing_day=99` | `clamp_to_month_end` 静默钳成月末 → 账期算错,账单金额不对,**不报错** |
| `payment_due_day=0` | 调度**永远不触发** → 用户以为在自动还款,实际半年没还 |
| `credit_limit=-100` | 额度是「还能刷多少」的分母,负数显示荒谬 |
| 扣款账户与卡不同币种 | transfer 两端共用一个 `amount` → **把 ¥10000 原样加到 JPY 卡上** |

最后一条最危险:**没有异常、没有日志**,只是钱记错了。

## 为什么校验在应用层而不是 CHECK 约束

1. 「扣款账户与卡同币种」需要 JOIN accounts 表,CHECK 表达不了
2. 「`autorepay_enabled` 时账单日/还款日必填」是跨列条件
3. 给上游列加 CHECK 会让**既有脏数据**在迁移时炸掉,用户无法启动

真正的兜底是调度器:配置非法时**跳过该卡并报可见的错**,而不是让
整个调度任务崩掉。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models import UserAccountProjection
from src.services.credit_card.config import (
    AutoRepayConfigError,
    enabled_cards,
    load_card_config,
    validate_autorepay_config,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TS() as session:
        yield session


def _acc(db, sync_id, *, account_type="credit_card", currency="JPY",
         billing_day=10, payment_due_day=25, credit_limit=None,
         enabled=False, from_acct=None, name=None):
    db.add(UserAccountProjection(
        user_id="u1", sync_id=sync_id, name=name or sync_id,
        account_type=account_type, currency=currency,
        initial_balance=0.0, credit_limit=credit_limit,
        billing_day=billing_day, payment_due_day=payment_due_day,
        autorepay_enabled=enabled, autorepay_from_account_sync_id=from_acct,
        source_change_id=1,
    ))


def _ok_pair(db, *, card_kwargs=None, src_currency="JPY", src_id="acc-savings"):
    """一套合法配置:卡 + 储蓄卡。返回 (card_id, source_id)。"""
    card = dict(billing_day=10, payment_due_day=25, enabled=True,
                from_acct=src_id)
    card.update(card_kwargs or {})
    _acc(db, "acc-card", **card)
    _acc(db, src_id, account_type="bank_card", currency=src_currency,
         billing_day=None, payment_due_day=None)
    db.commit()
    return "acc-card", src_id


def _validate(db, card="acc-card", src="acc-savings"):
    validate_autorepay_config(db, user_id="u1",
                              card_account_sync_id=card,
                              from_account_sync_id=src)


# --------------------------------------------------------------------------- #
# 正常路径                                                                    #
# --------------------------------------------------------------------------- #


def test_valid_config_passes(db) -> None:
    _ok_pair(db)
    _validate(db)   # 不抛 = 通过


def test_card_without_billing_day_is_rejected(db) -> None:
    """没账单日就没有「什么时候还」的锚点 —— 拒绝。"""
    _ok_pair(db, card_kwargs={"billing_day": None})
    with pytest.raises(AutoRepayConfigError, match="billing_day and payment_due_day"):
        _validate(db)


@pytest.mark.parametrize("bad", [0, 32, -1])
def test_billing_day_out_of_range_is_rejected(db, bad) -> None:
    _ok_pair(db, card_kwargs={"billing_day": bad})
    with pytest.raises(AutoRepayConfigError, match="billing_day must be 1..31"):
        _validate(db)


@pytest.mark.parametrize("bad", [0, 32, -5])
def test_payment_due_day_out_of_range_is_rejected(db, bad) -> None:
    """`payment_due_day=0` 尤其阴险:调度**永远不触发**,
    用户以为在自动还款,实际半年没还,而且没有任何报错。"""
    _ok_pair(db, card_kwargs={"payment_due_day": bad})
    with pytest.raises(AutoRepayConfigError, match="payment_due_day must be 1..31"):
        _validate(db)


def test_negative_credit_limit_is_rejected(db) -> None:
    """上游从未在服务端校验过额度非负 —— 只有前端拦。"""
    _ok_pair(db, card_kwargs={"credit_limit": -100.0})
    with pytest.raises(AutoRepayConfigError, match="credit_limit must be non-negative"):
        _validate(db)


# --------------------------------------------------------------------------- #
# 跨币种:最危险的一条                                                         #
# --------------------------------------------------------------------------- #


def test_cross_currency_is_rejected(db) -> None:
    """日元卡 + 人民币储蓄卡 → 拒绝。

    transfer **不带** `native_amount`,两端共用同一个 `amount` —— 跨币种
    还款会把 ¥10000 **原样**加到 JPY 卡上。没有异常、没有日志,只是钱记错了。
    """
    _ok_pair(db, src_currency="CNY")
    with pytest.raises(AutoRepayConfigError, match="cross-currency"):
        _validate(db)


def test_cross_currency_error_explains_why(db) -> None:
    """错误信息要说清原因,不然用户只会看到「配置无效」。"""
    _ok_pair(db, src_currency="CNY")
    with pytest.raises(AutoRepayConfigError) as e:
        _validate(db)
    assert "native_amount" in str(e.value), str(e.value)


# --------------------------------------------------------------------------- #
# 其他                                                                        #
# --------------------------------------------------------------------------- #


def test_source_must_differ_from_card(db) -> None:
    """扣款账户配成这张卡自己 → 无效操作(凭空增删自己)。"""
    _acc(db, "acc-card", billing_day=10, payment_due_day=25)
    db.commit()
    with pytest.raises(AutoRepayConfigError, match="must differ"):
        _validate(db, card="acc-card", src="acc-card")


def test_missing_card_is_rejected(db) -> None:
    db.commit()
    with pytest.raises(AutoRepayConfigError, match="not found"):
        _validate(db, card="不存在")


def test_missing_source_is_rejected(db) -> None:
    _acc(db, "acc-card", billing_day=10, payment_due_day=25, enabled=True)
    db.commit()
    with pytest.raises(AutoRepayConfigError, match="not found"):
        _validate(db, src="不存在")


def test_non_credit_card_type_is_rejected(db) -> None:
    """普通银行卡不能绑自动还款 —— 没有账单周期概念。"""
    _acc(db, "acc-card", account_type="bank_card", billing_day=10,
         payment_due_day=25)
    _acc(db, "acc-savings", account_type="cash", billing_day=None,
         payment_due_day=None)
    db.commit()
    with pytest.raises(AutoRepayConfigError, match="only applies to credit_card"):
        _validate(db)


def test_no_source_is_rejected(db) -> None:
    _acc(db, "acc-card", billing_day=10, payment_due_day=25)
    db.commit()
    with pytest.raises(AutoRepayConfigError, match="requires a source account"):
        _validate(db, src=None)


# --------------------------------------------------------------------------- #
# 调度器用的读取                                                               #
# --------------------------------------------------------------------------- #


def test_enabled_cards_filters_disabled(db) -> None:
    _ok_pair(db)
    _acc(db, "acc-card2", billing_day=5, payment_due_day=20, enabled=False)
    db.commit()
    got = enabled_cards(db, user_id="u1")
    assert [c.card_sync_id for c in got] == ["acc-card"], got


def test_enabled_cards_does_not_validate(db) -> None:
    """**故意不校验** —— 一张卡配置非法不该让其他卡的还款一起停摆。

    校验在调度执行时逐卡进行,非法就跳过该卡 + 报可见的错。
    """
    _acc(db, "acc-bad", billing_day=None, payment_due_day=None, enabled=True,
         from_acct="acc-x")
    _acc(db, "acc-good", billing_day=10, payment_due_day=25, enabled=True,
         from_acct="acc-x")
    _acc(db, "acc-x", account_type="cash", billing_day=None,
         payment_due_day=None)
    db.commit()
    got = {c.card_sync_id for c in enabled_cards(db, user_id="u1")}
    assert got == {"acc-bad", "acc-good"}, "读取阶段不应过滤掉配置非法的卡"


def test_load_card_config_returns_none_for_missing(db) -> None:
    db.commit()
    assert load_card_config(db, user_id="u1", card_account_sync_id="nope") is None


def test_card_config_roundtrip(db) -> None:
    _ok_pair(db)
    cfg = load_card_config(db, user_id="u1", card_account_sync_id="acc-card")
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.from_account_sync_id == "acc-savings"
    assert cfg.billing_day == 10 and cfg.payment_due_day == 25
    assert cfg.is_due_configured is True


# --------------------------------------------------------------------------- #
# 改名免疫                                                                    #
# --------------------------------------------------------------------------- #


def test_config_survives_account_rename(db) -> None:
    """配置存 sync_id → **账户改名天然免疫**。

    这是相对「存账户名」的核心优势。前端转账是按名字定位的
    (`TransactionsPage.tsx:1463-1467`),改名后 `account_id` 静默变 null,
    退回按名反查,同名时返回 None → 只扣不加。
    """
    _ok_pair(db)
    row = db.scalar(select(UserAccountProjection).where(
        UserAccountProjection.sync_id == "acc-card"))
    row.name = "招行白金卡"
    db.commit()
    cfg = load_card_config(db, user_id="u1", card_account_sync_id="acc-card")
    assert cfg is not None
    assert cfg.from_account_sync_id == "acc-savings", (
        "改名后扣款账户指向丢了 —— 说明配置里存的是名字而不是 sync_id"
    )