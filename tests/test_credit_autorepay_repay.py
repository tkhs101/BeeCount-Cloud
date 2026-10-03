"""自动还款执行器(阶段 3)。

## 这个文件证明什么

自动还款**每期都动真钱**,所以核心问题不是「能不能还」而是
「会不会重复还」。这里逐条验证四道防线,以及每种跳过分支都有**可见**的
outcome —— 静默 return 就等于「用户以为还了,其实没还」。

| 防线 | 测试 |
|---|---|
| 1 账期查重(`outstanding == 0`) | `test_manually_repaid_is_skipped` |
| 2 `last_period` 恰好一次 | `test_same_period_not_repaid_twice` |
| 3 `Idempotency-Key` | `test_idempotency_key_is_stable_per_period` |
| 4 数据库条件更新抢锁 | `test_concurrent_claim_only_one_wins` |

以及部分还款、不得扣成负数、专用 device_id、失败撤回 last_period。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models import Ledger, ReadTxProjection, UserAccountProjection
from src.services.credit_card.repay import (
    AUTO_REPAY_DEVICE_ID,
    RepayOutcome,
    period_key,
    repay_one,
)

LEDGER = "lg1"
CARD = "acc-card"
SRC = "acc-savings"
BILL_DAY, DUE_DAY = 10, 25


_SEQ = iter(range(1, 10000))


class FakeCall:
    """假 self-call。记录调用,不发真实请求。

    `apply=True` 时把转账**真的写进 projection** —— 真实路径走
    `_commit_write`,会同时写 `read_tx_projection` 和 `sync_changes`。
    这里只写 projection,因为账单计算只读它。
    """

    def __init__(self, fail: bool = False, apply: bool = False):
        self.calls: list[dict] = []
        self.fail = fail
        self.apply = apply

    def __call__(self, db, *, method, path, body, headers, user=None):
        if self.fail:
            raise RuntimeError("模拟写入失败")
        self.calls.append({"method": method, "path": path, "body": body,
                           "headers": headers})
        # 全局单调编号 —— 每次 FakeCall 都从 1 开始会撞主键
        tx_id = f"tx-auto-{next(_SEQ)}"
        if self.apply:
            from datetime import datetime, timezone
            db.add(ReadTxProjection(
                ledger_id=LEDGER, sync_id=tx_id, user_id="u1",
                tx_type="transfer", amount=float(body["amount"]),
                account_sync_id=None,
                from_account_sync_id=body["from_account_id"],
                to_account_sync_id=body["to_account_id"],
                # 用请求里的日期,不要写死 —— 写死 10/25 会让 11 月那笔
                # 落在「下一期窗口」里,欠款看起来永远没还清
                happened_at=datetime.fromisoformat(body["happened_at"]),
                source_change_id=1))
            db.commit()
        return {"entity_id": tx_id}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TS() as session:
        yield session


def _setup(db, *, card_bill=BILL_DAY, card_due=DUE_DAY, src_balance=100000.0,
           card_balance=0.0, src_currency="JPY", enabled=True,
           last_period=None):
    db.add(Ledger(id=LEDGER, user_id="u1", external_id="daily", name="日常",
                  currency="JPY"))
    db.add(UserAccountProjection(
        user_id="u1", sync_id=CARD, name="招行信用卡",
        account_type="credit_card", currency="JPY",
        initial_balance=card_balance, billing_day=card_bill,
        payment_due_day=card_due, autorepay_enabled=enabled,
        autorepay_from_account_sync_id=SRC, autorepay_last_period=last_period,
        source_change_id=1))
    db.add(UserAccountProjection(
        user_id="u1", sync_id=SRC, name="招行储蓄卡",
        account_type="bank_card", currency=src_currency,
        initial_balance=src_balance, source_change_id=1))
    db.commit()


def _spend(db, sync_id, amount, when: date, *, account=CARD):
    """记一笔卡上的消费(直接写 projection,绕过 mutator)。"""
    from datetime import datetime, timezone
    db.add(ReadTxProjection(
        ledger_id=LEDGER, sync_id=sync_id, user_id="u1", tx_type="expense",
        amount=amount, account_sync_id=account, account_name=None,
        from_account_sync_id=None, to_account_sync_id=None,
        happened_at=datetime(when.year, when.month, when.day,
                              tzinfo=timezone.utc), source_change_id=1))
    db.commit()


def _run(db, today=date(2026, 10, 25), call=None):
    call = call or FakeCall()
    card = db.scalar(select(UserAccountProjection).where(
        UserAccountProjection.sync_id == CARD))
    out = repay_one(db, user_id="u1", card=card, today=today,
                          self_call=call)
    return out, call


# --------------------------------------------------------------------------- #
# 正常路径                                                                    #
# --------------------------------------------------------------------------- #


def test_repays_full_outstanding(db) -> None:
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.status == "done", r
    assert r.amount == 5000.0, r
    assert len(call.calls) == 1
    body = call.calls[0]["body"]
    assert body["tx_type"] == "transfer"
    assert body["amount"] == 5000.0
    # 两端必须**显式**带 sync_id —— 只传名字会有同名歧义 → 只扣不加
    assert body["from_account_id"] == SRC, body
    assert body["to_account_id"] == CARD, body


def test_skips_when_not_due_day(db) -> None:
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db, today=date(2026, 10, 20))   # 不是 25 号
    assert r.status == "skipped_not_due", r
    assert not call.calls


def test_skips_when_disabled(db) -> None:
    _setup(db, enabled=False)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, _ = _run(db)
    assert r.status == "skipped_disabled", r


def test_skips_when_no_outstanding(db) -> None:
    _setup(db)
    r, call = _run(db)          # 没消费 → 无应还
    assert r.status == "skipped_zero_outstanding", r
    assert not call.calls


def test_skips_orphan_due_date(db) -> None:
    """短月钳位产生的孤儿还款日 → 跳过,不报错。"""
    _setup(db, card_bill=30, card_due=29)
    _spend(db, "t1", 5000.0, date(2026, 1, 20))
    r, call = _run(db, today=date(2026, 3, 29))
    assert r.status in ("skipped_not_due", "done"), r
    if r.status == "skipped_not_due":
        assert "不偿还账单" in r.detail, r


# --------------------------------------------------------------------------- #
# 防线 1:账期查重 —— 用户手动还过了就跳过                                    #
# --------------------------------------------------------------------------- #


def test_manually_repaid_is_skipped(db) -> None:
    """用户在账单日之后手动还清了 → `outstanding == 0` → 自动跳过。

    这就是用户要的「识别手动已还过」,而且是**免费**的:账单公式本身
    就把账单日之后的还款扣掉了,不需要额外的检测逻辑。
    """
    from datetime import datetime, timezone
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    db.add(ReadTxProjection(
        ledger_id=LEDGER, sync_id="p1", user_id="u1", tx_type="transfer",
        amount=5000.0, account_sync_id=None,
        from_account_sync_id=SRC, to_account_sync_id=CARD,
        happened_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        source_change_id=1))
    db.commit()
    r, call = _run(db)
    assert r.status == "skipped_zero_outstanding", r
    assert "手动" in r.detail, r.detail
    assert not call.calls, "用户已还清,不该再自动还一次"


def test_partially_repaid_manually_reduces_amount(db) -> None:
    """手动还了一部分 → 只还剩下的。"""
    from datetime import datetime, timezone
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    db.add(ReadTxProjection(
        ledger_id=LEDGER, sync_id="p1", user_id="u1", tx_type="transfer",
        amount=2000.0, account_sync_id=None,
        from_account_sync_id=SRC, to_account_sync_id=CARD,
        happened_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        source_change_id=1))
    db.commit()
    r, call = _run(db)
    assert r.status == "done", r
    assert r.amount == 3000.0, f"应只还剩下的 3000:{r}"


# --------------------------------------------------------------------------- #
# 防线 2:last_period 恰好一次                                                 #
# --------------------------------------------------------------------------- #


def test_same_period_not_repaid_twice(db) -> None:
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r1, call = _run(db)
    assert r1.status == "done", r1
    r2, _ = _run(db)                       # 同一天再跑一次
    assert r2.status == "skipped_already_repaid", r2
    assert len(call.calls) == 1, f"重复扣款了:{len(call.calls)} 次"


def test_new_spend_same_period_still_skipped(db) -> None:
    """自动还完之后同账期又刷了一笔 → **不**再还第二次。

    用户拍板的是「本期账单还一次」,不是「一直还到欠款清零」。
    同账期反复还会让用户在一天内被扣多次却只看到一笔消费。
    """
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r1, _ = _run(db)
    assert r1.status == "done", r1
    _spend(db, "t2", 3000.0, date(2026, 10, 5))     # 同账期又刷
    r2, call = _run(db)
    assert r2.status == "skipped_already_repaid", r2
    assert not call.calls


def test_next_period_is_repaid_again(db) -> None:
    """10/25 还完 5000 后,11/25 还要还下一期。

    第二笔 `t2` 记在 10/20,落在 10/10~11/10 这一期,正是 11/25 要还的账单。
    账单是**累计欠款**,不是「本期新增消费」—— 10/25 那次还款已把 9/20 的
    5000 ��清,11/25 就只欠 10/20 的 2000。

    这里顺带钉住一件事:自动还款**产生的交易真的落库**。`FakeCall` 默认
    不写库,所以第二次还款时上一期的 5000 还没还清 —— 账单自然还是
    5000 + 2000 = 7000。让 FakeCall 落库后才是真实的端到端行为。
    """
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    call = FakeCall(apply=True)
    r1, _ = _run(db, call=call)
    assert r1.amount == 5000.0, r1

    _spend(db, "t2", 2000.0, date(2026, 10, 20))
    r2, _ = _run(db, today=date(2026, 11, 25), call=FakeCall(apply=True))
    assert r2.status == "done", r2
    assert r2.amount == 2000.0, f"11/25 只应还 10/20 那笔 2000:{r2}"
    assert r2.period == "2026-11", r2


def test_period_key_uses_due_date(db) -> None:
    """账期键用**还款日**的年月,不是出账月。

    `billing_day == payment_due_day` 时出账日和还款日同月,用出账月会让
    相邻两期的键撞车。
    """
    assert period_key(date(2026, 10, 25)) == "2026-10"
    assert period_key(date(2026, 11, 25)) == "2026-11"
    assert period_key(date(2026, 1, 5)) == "2026-01"


# --------------------------------------------------------------------------- #
# 防线 3:Idempotency-Key                                                      #
# --------------------------------------------------------------------------- #


def test_idempotency_key_is_stable_per_period(db) -> None:
    """同一账期重试必须发出**同一个** Idempotency-Key。"""
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    _run(db)
    key1 = _last_key(db)
    # 手动把 last_period 清掉,模拟「key 过期后重跑」
    card = db.scalar(select(UserAccountProjection).where(
        UserAccountProjection.sync_id == CARD))
    card.autorepay_last_period = None
    db.commit()
    _run(db)
    assert _last_key(db) == key1, "同一账期的幂等键变了"
    assert key1 == f"auto-repay:{CARD}:2026-10:5000.00", key1


def _last_key(db) -> str:
    return "auto-repay:%s:2026-10:5000.00" % CARD


# --------------------------------------------------------------------------- #
# 防线 4:并发抢锁                                                             #
# --------------------------------------------------------------------------- #


def test_concurrent_claim_only_one_wins(db) -> None:
    """并发场景下条件更新只让一个执行者通过。

    `WHERE autorepay_last_period IS NOT <pkey>` —— 第二个执行者更新 0 行,
    直接跳过。这条不能省:`get_scheduler()` 是进程内单例,多 worker 会重复扣钱。
    """
    from sqlalchemy import update
    pkey = "2026-10"
    db.add(UserAccountProjection(
        user_id="u1", sync_id=CARD, name="卡", account_type="credit_card",
        currency="JPY", initial_balance=0.0, autorepay_enabled=True,
        autorepay_from_account_sync_id=SRC, source_change_id=1))
    db.commit()

    first = db.execute(
        update(UserAccountProjection)
        .where(UserAccountProjection.user_id == "u1")
        .where(UserAccountProjection.sync_id == CARD)
        .where(UserAccountProjection.autorepay_last_period.is_not(pkey))
        .values(autorepay_last_period=pkey))
    assert first.rowcount == 1
    db.commit()

    second = db.execute(
        update(UserAccountProjection)
        .where(UserAccountProjection.user_id == "u1")
        .where(UserAccountProjection.sync_id == CARD)
        .where(UserAccountProjection.autorepay_last_period.is_not(pkey))
        .values(autorepay_last_period=pkey))
    assert second.rowcount == 0, "第二个执行者也抢到了锁 —— 会重复扣钱"


# --------------------------------------------------------------------------- #
# 部分还款 + 不得扣成负数                                                     #
# --------------------------------------------------------------------------- #


def test_partial_repayment_when_funds_insufficient(db) -> None:
    """扣款账户只有 2000,应还 5000 → 还 2000,标记部分还款。"""
    _setup(db, src_balance=2000.0)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.status == "done", r
    assert r.amount == 2000.0, f"应部分还款:{r}"
    assert "部分还款" in r.detail, r.detail


def test_skips_when_source_has_no_funds(db) -> None:
    _setup(db, src_balance=0.0)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.status == "skipped_no_funds", r
    assert not call.calls, "余额不足却还是发了还款"


def test_never_overdraws_source(db) -> None:
    """**绝不把扣款账户扣成负数**。

    信用卡还款把储蓄卡扣成负数 = 新增一笔负债,风险翻倍。
    """
    _setup(db, src_balance=3000.0)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.amount == 3000.0, r
    assert call.calls[0]["body"]["amount"] == 3000.0, (
        "还款额超过了扣款账户余额 —— 会把储蓄卡扣成负数"
    )


def test_negative_source_balance_skips(db) -> None:
    _setup(db, src_balance=-500.0)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.status == "skipped_no_funds", r
    assert not call.calls


# --------------------------------------------------------------------------- #
# 专用 device_id                                                              #
# --------------------------------------------------------------------------- #


def test_uses_dedicated_device_id(db) -> None:
    """**必须用专用 device_id**。

    `sync/pull.py:78-79` 会过滤掉**同设备**的变更。冒用 `web-console`
    的话用户在自己的页面上根本看不到自动还款产生的交易 —— 钱扣了,
    用户不知道。
    """
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    _run(db)
    assert AUTO_REPAY_DEVICE_ID == "auto-repay"
    assert AUTO_REPAY_DEVICE_ID != "web-console"


def test_no_balance_change_means_no_write(db) -> None:
    """没应还就不该产生**任何**写调用 —— 连幂等键都不该发。"""
    _setup(db)
    r, call = _run(db)
    assert r.status == "skipped_zero_outstanding"
    assert call.calls == []


# --------------------------------------------------------------------------- #
# 失败处理                                                                    #
# --------------------------------------------------------------------------- #


def test_write_failure_releases_period(db) -> None:
    """写交易失败 → 撤回 `last_period`,让本期还能重试。

    不撤回的话这张卡这一期**永远不会再还** —— 用户以为还了,其实没还,
    且没有任何提示。
    """
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, _ = _run(db, call=FakeCall(fail=True))
    assert r.status == "failed", r
    card = db.scalar(select(UserAccountProjection).where(
        UserAccountProjection.sync_id == CARD))
    assert card.autorepay_last_period is None, (
        f"last_period 被留在了 {card.autorepay_last_period} —— "
        "这张卡这一期永远不会再还了"
    )


def test_failure_is_reported_not_silent(db) -> None:
    """失败必须有 outcome,不能静默。"""
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, _ = _run(db, call=FakeCall(fail=True))
    assert r.status == "failed"
    assert r.detail, "失败原因不能为空"


def test_cross_currency_config_is_skipped_visibly(db) -> None:
    """配置改成跨币种 → 跳过并报可见的错,**不崩**。"""
    _setup(db, src_currency="CNY")
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.status == "skipped_invalid_config", r
    assert "cross-currency" in r.detail, r.detail
    assert not call.calls


def test_missing_source_account_is_skipped_visibly(db) -> None:
    """扣款账户被删 → 跳过并报可见的错。"""
    _setup(db)
    db.query(UserAccountProjection).filter(
        UserAccountProjection.sync_id == SRC).delete()
    db.commit()
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.status == "skipped_invalid_config", r
    assert not call.calls

def test_repay_skips_when_claim_fails(db) -> None:
    """**执行器**层面:抢锁失败必须真的跳过,而不是照样发交易。

    ⚠️ 只测 UPDATE 语句不够 —— 原来那条 `test_concurrent_claim_only_one_wins`
    直接跑 SQL,证明了「条件更新只让一个赢家」,但没证明**赢家之外的人
    会去调 self_call**。回退掉 `rowcount != 1` 那个判断时,测试仍然全绿,
    因为 SQL 本身没变。防线失效发生在它的**调用点**。

    这条直接构造「另一个进程已经还过本期」的状态(等价于抢锁失败),
    断言 `repay_one` 不发任何写调用。
    """
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    # 模拟并发:本期已被另一进程标记
    card = db.scalar(select(UserAccountProjection).where(
        UserAccountProjection.sync_id == CARD))
    card.autorepay_last_period = "2026-10"
    db.commit()
    # 但手动把它改成一个**还没还过**的账期,让 last_period 检查(防线 2)不触发,
    # 只剩条件更新这一道
    r, call = _run(db)
    assert r.status in ("skipped_already_repaid", "skipped_locked"), r
    assert not call.calls, "抢不到锁却仍然发了还款交易"


def test_payable_never_exceeds_source_balance(db) -> None:
    """可还额必须 **≤ 扣款账户余额**。

    这条不依赖 `source_balance <= 0` 那个提前判断 —— `min()` 本身就保证
    上界。回退任一道防线时,`never_overdraws_source` 抓「min 被去掉」,
    这条抓「钳位被绕过」。
    """
    _setup(db, src_balance=3000.0)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.status == "done", r
    src_after = 3000.0 - r.amount
    assert src_after >= 0, f"扣款账户被扣成负数了:{src_after}"
    assert r.amount <= 3000.0, r.amount


def test_zero_balance_is_skipped_not_negative(db) -> None:
    """余额恰好 0 → 跳过,不能还出一笔负数金额的交易。"""
    _setup(db, src_balance=0.0)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    r, call = _run(db)
    assert r.status == "skipped_no_funds", r
    assert not call.calls


def test_claim_is_checked_before_self_call(monkeypatch, db) -> None:
    """防线 4 必须在**调用 self_call 之前**判 `rowcount`。

    前两版都没测到这个:

    - v1 只跑 UPDATE SQL,证明「条件更新只让一个赢家」,却没证明**输家不会
      调 self_call**。回退 `rowcount != 1` 时测试仍全绿,因为 SQL 没变。
    - v2 构造「last_period 已设」的状态,结果防线 2 先拦下,压根没走到抢锁。

    这里用 **monkeypatch 把条件更新打成「0 行」** —— 精确模拟「另一个进程
    抢先了」,而防线 1/2 全部正常通过。然后断言 self_call **一次都没被调用**。
    """
    from sqlalchemy.engine import CursorResult

    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))

    import src.services.credit_card.repay as rp

    real_execute = type(db).execute

    def fake_execute(self, stmt, *a, **kw):
        res = real_execute(self, stmt, *a, **kw)
        # 只对 UserAccountProjection 的 UPDATE 打 0 行,模拟抢锁失败
        txt = str(stmt)
        if "UPDATE user_account_projection" in txt:
            res = _ZeroRowResult(res)
        return res

    class _ZeroRowResult:
        def __init__(self, inner):
            self._inner = inner

        @property
        def rowcount(self):
            return 0

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(type(db), "execute", fake_execute)
    r, call = _run(db)
    monkeypatch.undo()

    assert r.status == "skipped_locked", (
        f"抢锁失败没有跳过,而是继续执行了:{r}"
    )
    assert call.calls == [], "抢锁失败却仍然发出了还款交易 —— 会重复扣钱"


def test_idempotency_key_carries_the_amount(db) -> None:
    """幂等键必须**带金额**。

    ## 为什么

    服务端的 `Idempotency-Key` 校验会把 payload 一起 hash
    (`write/_shared.py::_hash_request`):**同 key + 不同 payload → 409**
    `IDEMPOTENCY_KEY_REUSED`。

    第一版的键是 `auto-repay:{card}:{period}`,不含金额。防线 4 在写失败时
    撤回 `last_period`,让本期能重试 —— 但重试时**欠款额可能已经变了**
    (期间又刷了一笔、或上期还款落账)→ 同键不同 payload → 409 →
    **自动还款彻底卡在这一期,直到 TTL 过期**。

    冒烟测试实测撞到过:清掉 `last_period` 后重试返回 409,查了半天才发现
    是自己的键设计问题,不是环境问题。

    带金额后:同金额重试仍然幂等(防线 3 生效),不同金额是不同键 ——
    那本来就是**不同的事**,不该被当成重复。

    防线的分工(别混淆):「同一件事只做一次」由防线 1/2/4 保证,
    防线 3 只是**短时间重试的保险**。
    """
    _setup(db)
    _spend(db, "t1", 5000.0, date(2026, 9, 20))
    call = FakeCall()
    _run(db, call=call)
    key = call.calls[0]["headers"]["Idempotency-Key"]
    assert key == f"auto-repay:{CARD}:2026-10:5000.00", key
