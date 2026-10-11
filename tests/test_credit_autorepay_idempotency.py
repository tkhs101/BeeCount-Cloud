"""同一期自动还款**连跑两次** —— 只能扣一次。

## 为什么这条最该有

`CLAUDE.md` 写自动还款是「会花钱的代码」,并列了**四道幂等防线**,
其中两道在代码里存在但**零测试覆盖**:

| # | 防线 | 测试覆盖 |
|---|---|---|
| 1 | 账期查重(`outstanding == 0` 就跳过) | 有 |
| 2 | `autorepay_last_period`(恰好一次) | 有 |
| 3 | `Idempotency-Key: auto-repay:{card}:{period}:{amount}` | **零** |
| 4 | 数据库条件更新抢锁 | **零** |

防线 3 在 `repay.py:267`,防线 4 在 `repay.py:224-236`
(`WHERE last_period IS NOT pkey` + `rowcount != 1` 就放弃)。

**同时**跑两次是最贴近现实的并发模型 —— 定时任务与手动触发撞车、
或用户手点了两次。它不需要真的多进程就能验:第二次调用走的是同一段
代码,`rowcount` 会是 0。

## 断言落在「多扣了多少钱」上

不测内部状态(那会跟着实现漂),**测账户余额与交易条数** ——
用户真正在意的是「钱少没少错」。

## 还测了「失败后要能重试」

`_release_period` 写交易失败时把 `last_period` 撤回。若那条路径坏了,
用户会看到「这一期显示还过了,但实际没扣钱」—— 同样是花钱的 bug,
方向相反。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

#  有  这一关 ——
# **today 必须正好是还款日**,不是「过了还款日」。取错日期会得到
# skipped_not_due,而那不是 bug(我为此白折腾两轮)。
#
# 账期窗口(billing_day=1)= 2026-10-01~10-31,还款日 11-15。
TODAY = date(2026, 11, 15)


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    from src.models import Base
    Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)
    yield TS
    engine.dispose()


def _setup(TS):
    """建账本 + 一张卡 + 一笔消费,并把卡绑好自动还款。"""
    from src.models import Ledger, ReadTxProjection, UserAccountProjection

    with TS() as db:
        db.add(Ledger(id="lg", user_id="u1", external_id="e",
                      name="L", currency="JPY"))
        db.add(UserAccountProjection(
            sync_id="card1", user_id="u1", name="招行卡",
            account_type="credit_card", currency="JPY", hidden=False,
            billing_day=1, payment_due_day=15,
            autorepay_enabled=True,
            autorepay_from_account_sync_id="bank1",
            autorepay_last_period=None))
        db.add(UserAccountProjection(
            sync_id="bank1", user_id="u1", name="工资卡",
            account_type="bank_card", currency="JPY", hidden=False,
            # 扣款账户**必须有钱**,否则走到 skipped_no_funds ——
            # 那样根本走不到锁与幂等那几步,测的就不是防线了。
            initial_balance=Decimal("100000")))
        db.add(ReadTxProjection(
            sync_id="tx1", ledger_id="lg", user_id="u1", tx_type="expense",
            amount=30000.0, note="消费", account_sync_id="card1",
            account_name="招行卡",
            happened_at=__import__("datetime").datetime(2026, 10, 5, 9, 0)))
        db.commit()


def _balance(TS, sync_id: str) -> float:
    from src.models import UserAccountProjection
    with TS() as db:
        row = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == sync_id))
        return float(row.initial_balance or 0)


def _tx_count(TS, ledger_id: str) -> int:
    from src.models import ReadTxProjection
    with TS() as db:
        return db.scalar(select(func.count()).select_from(ReadTxProjection)
                         .where(ReadTxProjection.ledger_id == ledger_id))


def test_running_the_same_period_twice_charges_once(env) -> None:
    """同一期跑两次 —— 只能扣一次钱。"""
    from src.models import UserAccountProjection
    from src.services.credit_card.repay import repay_one

    TS = env
    _setup(TS)

    calls: list[dict] = []

    # 真实调用形状:
    # —— 我第一版猜成 ,直接 TypeError,
    # 而 repay_one 把异常吞成 status=failed,报错信息绕了一圈才看到。
    def fake_self_call(db, *, method, path, body, headers, user, **kw):
        calls.append(dict(body))
        from src.projection import upsert_tx
        upsert_tx(db, ledger_id="lg", user_id="u1", source_change_id=999,
                  payload={"id": f"gen-{len(calls)}",
                           "type": body["tx_type"],
                           "amount": body["amount"],
                           "happenedAt": "2026-11-15T09:00:00Z",
                           "fromAccountId": body.get("from_account_id")
                           or body.get("fromAccountId"),
                           "toAccountId": body.get("to_account_id")
                           or body.get("toAccountId"),
                           "note": "自动还款"})
        db.commit()
        return {"entity_id": f"gen-{len(calls)}"}

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == "card1"))
        db.refresh(card)
        r1 = repay_one(db, user_id="u1", card=card, today=TODAY,
                       self_call=fake_self_call)

    assert r1.status == "done", f"第一次没扣成功:{r1}"
    assert len(calls) == 1, f"第一次应该写一笔交易,写了 {len(calls)}"

    # ---- 第二次:同一期 ----
    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == "card1"))
        db.refresh(card)
        r2 = repay_one(db, user_id="u1", card=card, today=TODAY,
                       self_call=fake_self_call)

    assert r2.status != "done", (
        f"第二次又扣了一次!status={r2.status} —— "
        f"这是**重复扣钱**,四道幂等防线里至少有一道失效。"
        f"实际写了 {len(calls)} 笔交易。")
    assert len(calls) == 1, (
        f"self_call 被调了 {len(calls)} 次,应该只有 1 次 —— "
        f"防重复扣钱的机制完全没生效")


def test_lock_is_released_when_the_write_fails(env) -> None:
    """写交易失败时必须撤回 `last_period`,让本期还能重试。

    否则用户会看到「这一期显示还过了,但实际没扣钱」——
    同样是花钱的 bug,方向相反。
    """
    from src.models import UserAccountProjection
    from src.services.credit_card.repay import repay_one

    TS = env
    _setup(TS)

    def boom_self_call(db, *, method, path, body, headers, user, **kw):
        raise RuntimeError("模拟 self-call 失败")

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == "card1"))
        db.refresh(card)
        r1 = repay_one(db, user_id="u1", card=card, today=TODAY,
                       self_call=boom_self_call)

    assert r1.status == "failed", f"失败应该被如实上报,得到 {r1.status}"

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == "card1"))
        assert card.autorepay_last_period is None, (
            f"失败后 `last_period` 停在 {card.autorepay_last_period!r} —— "
            f"本期会被永久标记成「已还」,用户实际没扣钱。"
            f"(`_release_period` 没跑或跑失败)")


def test_idempotency_key_contains_the_amount(env) -> None:
    """幂等键**必须带金额**。

    服务端会把 payload 一起 hash —— 同键不同金额会 409,
    把这一期**彻底卡死**(而 TTL 只有 24h,过后又是重复扣钱的风险)。
    """
    from pathlib import Path

    src = Path("src/services/credit_card/repay.py")
    text = src.read_text(encoding="utf-8")
    idx = text.find('"Idempotency-Key"')
    assert idx > 0, "源码里找不到 Idempotency-Key —— 防线 3 没了?"

    line = text[idx: text.find("\n", idx)]
    assert "_amount_key" in line, (
        f"幂等键里没有金额:{line.strip()}\n"
        f"同键不同金额 -> 服务端 hash 不同 -> 409 把这一期卡死;")

def test_partial_payment_still_cannot_double_charge(env) -> None:
    """**部分还款**时,第二道(账期查重)不会拦 —— 防线 4 必须独立生效。

    ## 为什么必须单独构造这个场景

    全额还款后 `outstanding == 0`,第二次调用被**防线 1(账期查重)**挡掉,
    防线 4 根本没被测到 —— 我把它改成 `if False:` 重跑,测试**照样全绿**。

    这不是护栏无效,是**被另一道挡住了**(与 `upsert_tx` 那个
    `if "splits" in payload` 同一形状)。

    要单独验防线 4,必须让防线 1 **失效**:卡还有欠款、但扣款账户钱不够
    → 本期只能还一部分 → `outstanding` 仍 > 0 → 第二次调用会一路走到锁。

    这也是**真实场景**:用户工资卡余额不够,是自动还款最常见的情形。
    """
    from src.models import UserAccountProjection
    from src.services.credit_card.repay import repay_one

    TS = env
    _setup(TS)
    # 扣款账户只有 5000,而账单是 30000 -> 只能部分还款
    with TS() as db:
        bank = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == "bank1"))
        bank.initial_balance = Decimal("5000")
        db.commit()

    calls: list[dict] = []

    def fake_self_call(db, *, method, path, body, headers, user, **kw):
        calls.append(dict(body))
        from src.projection import upsert_tx
        upsert_tx(db, ledger_id="lg", user_id="u1", source_change_id=999,
                  payload={"id": f"gen-{len(calls)}",
                           "type": body["tx_type"],
                           "amount": body["amount"],
                           "happenedAt": "2026-11-15T09:00:00Z",
                           "fromAccountId": body.get("from_account_id"),
                           "toAccountId": body.get("to_account_id"),
                           "note": "自动还款"})
        db.commit()
        return {"entity_id": f"gen-{len(calls)}"}

    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == "card1"))
        db.refresh(card)
        r1 = repay_one(db, user_id="u1", card=card, today=TODAY,
                       self_call=fake_self_call)

    assert r1.status == "done", f"部分还款也应该执行:{r1}"
    assert len(calls) == 1
    assert float(r1.amount) == 5000.0, (
        f"应该只还 5000(账户里就这么多),得到 {r1.amount}")
    assert "部分还款" in (r1.detail or ""), (
        f"应该标记为部分还款,detail={r1.detail!r} —— "
        f"否则用户以为还清了,实际还欠 25000")

    # 第二次前先给扣款账户**再充一笔钱**(模拟月中又发工资)。
    #
    # 这一步是隔离防线 4 的关键:第一版没充,于是第二次调用在
    # `payable = min(outstanding, source_balance)` 处就因为**余额已归零**
    # 返回 `skipped_no_funds` —— 根本走不到锁。
    # 把 `rowcount` 检查改成 `if False:` 重跑,测试**照样全绿**。
    with TS() as db:
        bank = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == "bank1"))
        bank.initial_balance = Decimal("10000")
        db.commit()

    # 第二次:卡**仍有欠款**、账户**又有钱** —— 防线 1 与余额检查都不拦,
    # 唯一能挡住重复扣钱的就是防线 4 的条件锁。
    with TS() as db:
        card = db.scalar(select(UserAccountProjection).where(
            UserAccountProjection.sync_id == "card1"))
        db.refresh(card)
        r2 = repay_one(db, user_id="u1", card=card, today=TODAY,
                       self_call=fake_self_call)

    assert len(calls) == 1, (
        f"部分还款后又扣了一次(self_call 共 {len(calls)} 次)—— "
        f"**重复扣钱**。防线 4 的条件锁在这一场景下没生效。")
    assert r2.status != "done", f"第二次不该成功:{r2}"


def test_barrier4_is_only_reachable_under_true_concurrency() -> None:
    """防线 4(条件锁)**顺序调用永远测不到** —— 这一条把事实钉住。

    ## 实测过程(三次尝试才搞清,别重走)

    我把 `if getattr(claimed, "rowcount", 0) != 1:` 改成 `if False:`
    重跑,测试**全绿**。三次调整前置条件仍抓不到:

    1. 全额还款 → 防线 1(`outstanding == 0`)挡住
    2. 部分还款 → 扣款账户**余额归零**,`payable <= 0` 挡住
    3. 部分还款 + 给账户**再充钱** → **防线 2 挡住**

    读源码才看清:`repay.py:196-199` 的**防线 2 在防线 4 之前**。
    顺序调用时 `last_period` 已经等于 `pkey`,防线 2 先返回
    `skipped_already_repaid`,根本走不到 `db.execute(update(...))`。

    ## 所以这不是护栏弱,是**场景不可达**

    防线 4 防的是**真正的并发**:两个进程同时读到旧的 `last_period`,
    都通过防线 2,再竞争那一条条件 UPDATE。要复现必须**两个独立
    session 交错执行**,顺序测试原理上产生不了这个状态。

    ## 为什么要写这条测试

    否则下一个人会拿顺序测试「验证」防线 4,拿到全绿,以为它有效 ——
    而它一次都没被真正执行过。**没测到的防线比坏掉的防线更危险**,
    因为它给人虚假的安全感(与 CLAUDE.md 记的「比没有护栏更糟」同源)。

    真要验它,得上两个真连接 + 显式交错(且 SQLite 还要处理锁竞争)。
    """
    from pathlib import Path

    src = Path("src/services/credit_card/repay.py").read_text(encoding="utf-8")

    i_bar2 = src.find('if cfg.last_period == pkey:')
    i_bar4 = src.find("getattr(claimed, \"rowcount\"")
    assert i_bar2 > 0, "没找到防线 2 —— 代码结构变了,本文件的结论要重审"
    assert i_bar4 > 0, "没找到防线 4 —— 它可能被删了或改名了"

    assert i_bar2 < i_bar4, (
        "防线 4 现在排在防线 2 **之前** —— 顺序调用就能拦住重复扣钱。"
        f"若这是有意调整,请同步更新本文件的结论与 CLAUDE.md 的说明。")
