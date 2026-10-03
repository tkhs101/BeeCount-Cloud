"""自动还款配置的**应用层校验**(阶段 2)。

## 为什么校验在这里而不是数据库 CHECK 约束

`billing_day` / `payment_due_day` / `credit_limit` 是上游就有的列。本 fork
之前**服务端零校验**(只有前端拦),因为它们当时只是展示字段,脏值无害。

绑了自动还款之后它们变成**驱动资金操作**的字段:

| 脏值 | 后果 |
|---|---|
| `billing_day=99` | `clamp_to_month_end` 静默钳成月末,账期算错但不报错 |
| `payment_due_day=0` | 调度**永远不触发**,用户以为在自动还款 |
| `credit_limit=-100` | 额度显示荒谬(目前只在前端拦) |

而 CHECK 约束表达不了大部分规则:「扣款账户与信用卡同币种」需要 JOIN
accounts 表;「`autorepay_enabled` 时账单日/还款日必填」是跨列条件。
而且给上游列加 CHECK 会让**既有脏数据**在迁移时炸掉,用户无法启动。

真正的兜底是调度器:配置非法时该卡**跳过并报可见的错**,而不是崩溃。
见 `scheduler.py` 的 `_validate_rule`。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import UserAccountProjection

# 账户类型里,「能作为信用卡」的。绑定时 card 必须是这个类型。
CREDIT_CARD_TYPES = frozenset({"credit_card"})


class AutoRepayConfigError(ValueError):
    """自动还款配置非法。

    继承 `ValueError` 是刻意的:写路径的 mutator 用
    `write validation failed: ...` 前缀捕获它,前端因此拿到 400 而不是 500。
    """


@dataclass(frozen=True)
class CardConfig:
    """一张卡的自动还款配置(已解析)。"""

    card_sync_id: str
    enabled: bool
    from_account_sync_id: str | None
    last_period: str | None
    billing_day: int | None
    payment_due_day: int | None
    currency: str | None

    @property
    def is_due_configured(self) -> bool:
        """有账期锚点。没有账单日/还款日就没有「什么时候还」的语义。"""
        return self.billing_day is not None and self.payment_due_day is not None


def validate_autorepay_config(
    db: Session,
    *,
    user_id: str,
    card_account_sync_id: str,
    from_account_sync_id: str | None,
) -> None:
    """绑定/更新自动还款配置前的校验。非法直接抛 `AutoRepayConfigError`。

    五条规则:

    1. 卡片存在
    2. 卡片是 `credit_card` 类型
    3. 账单日、还款日都在 1..31(顺延月末由 `billing.py` 负责,这里只查范围)
    4. 扣款账户存在、**不是自己**、**与卡同币种**
    5. 额度非负(上游从未校验过)

    第 4 条的「同币种」是**硬约束**:transfer 不带 `native_amount`,两端共用
    同一个 `amount`,跨币种会把 ¥10000 原样加到 JPY 卡上。汇率基建
    (`ExchangeRateCache`)只服务「交易折本位币」和「净值折主币种」,
    **没有任何一处支持转账两端换算**。
    """
    card = _get_account(db, user_id=user_id, sync_id=card_account_sync_id)
    if card is None:
        raise AutoRepayConfigError(f"card account {card_account_sync_id} not found")
    if (card.account_type or "") not in CREDIT_CARD_TYPES:
        raise AutoRepayConfigError(
            f"auto-repay only applies to credit_card accounts, "
            f"got {card.account_type!r}"
        )

    if card.billing_day is None or card.payment_due_day is None:
        raise AutoRepayConfigError(
            "auto-repay needs both billing_day and payment_due_day on the card"
        )
    for name, value in (("billing_day", card.billing_day),
                        ("payment_due_day", card.payment_due_day)):
        if not (1 <= int(value) <= 31):
            raise AutoRepayConfigError(
                f"{name} must be 1..31, got {value}"
            )

    if card.credit_limit is not None and card.credit_limit < 0:
        # 上游从未在服务端校验过,只有前端拦。这里补上 —— 绑定自动还款后
        # 额度是「还能刷多少」的分母,负数会让显示荒谬。
        raise AutoRepayConfigError(
            f"credit_limit must be non-negative, got {card.credit_limit}"
        )

    if not from_account_sync_id:
        raise AutoRepayConfigError("auto-repay requires a source account")

    source = _get_account(db, user_id=user_id, sync_id=from_account_sync_id)
    if source is None:
        raise AutoRepayConfigError(
            f"source account {from_account_sync_id} not found"
        )
    if from_account_sync_id == card_account_sync_id:
        raise AutoRepayConfigError(
            "source account must differ from the credit card itself"
        )
    if (source.currency or "") != (card.currency or ""):
        raise AutoRepayConfigError(
            f"cross-currency auto-repay is not supported: card is "
            f"{card.currency!r} but source is {source.currency!r}. "
            f"Transfers carry no native_amount and both ends share one amount, "
            f"so a cross-currency payment would credit the wrong value."
        )


def _get_account(db: Session, *, user_id: str,
                 sync_id: str) -> UserAccountProjection | None:
    return db.scalar(
        select(UserAccountProjection).where(
            UserAccountProjection.user_id == user_id,
            UserAccountProjection.sync_id == sync_id,
        )
    )


def load_card_config(db: Session, *, user_id: str,
                     card_account_sync_id: str) -> CardConfig | None:
    """调度器用:读配置。账户不存在返回 None(已删除,跳过即可)。"""
    card = _get_account(db, user_id=user_id, sync_id=card_account_sync_id)
    if card is None:
        return None
    return CardConfig(
        card_sync_id=card.sync_id,
        enabled=bool(card.autorepay_enabled),
        from_account_sync_id=card.autorepay_from_account_sync_id,
        last_period=card.autorepay_last_period,
        billing_day=card.billing_day,
        payment_due_day=card.payment_due_day,
        currency=card.currency,
    )


def enabled_cards(db: Session, *, user_id: str) -> list[CardConfig]:
    """调度器用:该用户所有启用了自动还款的卡。

    只按 `enabled` 过滤,**不做**配置校验 —— 校验在调度执行时逐个进行,
    一张卡配置非法不该让其他卡的还款一起停摆。
    """
    rows = db.scalars(
        select(UserAccountProjection)
        .where(UserAccountProjection.user_id == user_id)
        .where(UserAccountProjection.autorepay_enabled.is_(True))
    ).all()
    return [
        CardConfig(
            card_sync_id=r.sync_id,
            enabled=True,
            from_account_sync_id=r.autorepay_from_account_sync_id,
            last_period=r.autorepay_last_period,
            billing_day=r.billing_day,
            payment_due_day=r.payment_due_day,
            currency=r.currency,
        )
        for r in rows
    ]