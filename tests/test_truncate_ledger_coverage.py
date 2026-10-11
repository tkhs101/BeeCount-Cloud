"""`_truncate_ledger` 必须覆盖**所有** ledger-scoped 的 projection 表。

## 这是「加新表必然再漏一次」的结构性防法

`CLAUDE.md` 把「`_truncate_ledger` 漏子表 → rebuild / 还原后父交易没腿、
旧腿还在 → 余额**凭空多出来**」列为组合支付的静默丢失之一。

那次是**事后**补的 —— 因为漏了要等用户发现余额不对才有人查。
加第 4 个 projection 表(比如 `read_recurring_projection`)时,同样的事
会再发生一次,而那时没人记得去改这个手写的模型列表。

所以用**白名单**式判据(与 `test_projection_reverse_bridge_coverage.py`
同一思路):

- 新增 `Read*` 表时,这条**默认红**,要求显式论证它该不该被清
- 白名单里的每一项都必须真的在 `_truncate_ledger` 里被清

## 为什么用「枚举 Read* 前缀」而不是列死模型

黑名单(直接断言列表里有这 3 个)在新表出现时**默认通过** ——
而那正是要防的情况。白名单默认**拒绝**。

## ledger-scoped vs user-global

`UserAccountProjection` / `UserCategoryProjection` 是 **per-user** 表,
跨账本共享,按 `ledger_id` 清会**误删别的账本的数据** —— 所以它们
**故意不在**这里。本测试用「有 `ledger_id` 列」来区分,而不是手写分类。
"""
from __future__ import annotations

import ast
from pathlib import Path

from src import models
from src import projection as projection_mod

PROJECTION_SRC = Path(projection_mod.__file__)


def _read_projection_tables() -> dict[str, type]:
    return {
        name: obj
        for name, obj in vars(models).items()
        if isinstance(obj, type)
        and name.startswith("Read")
        and hasattr(obj, "__table__")
    }


def _models_truncated_by_truncate_ledger() -> set[str]:
    """真读 AST 拿 `_truncate_ledger` 里那个模型元组 —— 不靠字符串匹配。

    源码级断言自己会恒真(CLAUDE.md 记过 tokenize 那条):`" ".join(…)`
    匹配不上原样的 `date.today()`。这里用 `ast` 取「for model in (…,)」
    里的**标识符名**,判据是「这个模型真的被遍历到了」。
    """
    tree = ast.parse(PROJECTION_SRC.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_truncate_ledger":
            continue
        for sub in ast.walk(node):
            # `for model in (A, B, C):` —— target 是 **Name**(model),
            # iter 才是 Tuple。写成 target 是 Tuple 会永远匹配不上,
            # 静态判据于是「什么都没找到」= 全绿。
            if (isinstance(sub, ast.For)
                    and isinstance(sub.target, ast.Name)
                    and isinstance(sub.iter, ast.Tuple)):
                for elt in sub.iter.elts:
                    if isinstance(elt, ast.Name):
                        names.add(elt.id)
    return names


def test_the_ast_extractor_finds_the_loop() -> None:
    """判据自检:真的找得到那个循环。

    没有这条,`_truncate_ledger` 改名/重构后这条测试会**静默通过**
    —— 找不到循环 = 找到空集合 = 「没有表被清」= 全绿。
    """
    found = _models_truncated_by_truncate_ledger()
    assert "ReadTxProjection" in found, (
        f"AST 没解析出 `_truncate_ledger` 的模型列表:{found} —— "
        f"判据失效了(函数改名? 用了别名? 循环换了写法?)")


def test_every_ledger_scoped_projection_is_truncated() -> None:
    """每个 ledger-scoped 的 projection 都必须被清。"""
    ledger_scoped = {
        name for name, obj in _read_projection_tables().items()
        if "ledger_id" in obj.__table__.columns
    }
    assert ledger_scoped, "一张都没找到 —— 判据可能失效了"

    cleared = _models_truncated_by_truncate_ledger()
    missing = sorted(ledger_scoped - cleared)

    assert not missing, (
        f"这些 ledger-scoped projection 没被 `_truncate_ledger` 清:{missing}\n"
        f"加新 projection 表时**必须**在这里登记 —— 漏了的话,rebuild / "
        f"admin restore_backup 会留下旧行,余额「凭空多出来」且不报错。"
        f"如果是 per-user 表(不该按 ledger 清),那它不该有 ledger_id 列。")


def test_whitelist_entries_are_all_really_truncated() -> None:
    """反向:列表里的每个模型都得真被清,别留僵尸条目。"""
    cleared = _models_truncated_by_truncate_ledger()
    known = set(_read_projection_tables())
    zombie = sorted(cleared - known)
    assert not zombie, (
        f"`_truncate_ledger` 里有 {zombie},但 `src/models.py` 里找不到对应模型"
        f" —— 重命名后忘了同步这里。")
    assert cleared, "列表空了 —— 说明 AST 判据失效"


def test_truncate_actually_deletes_rows_at_runtime() -> None:
    """运行期确认:每张表塞一行,`_truncate_ledger` 后真的没了。

    AST 判据只能证明「模型名出现在那个元组里」——
    万一循环体被改坏(比如加了 `if False:`),静态判据看不出来。
    """
    from datetime import datetime, timezone

    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(bind=engine)
    TS = sessionmaker(bind=engine)

    with TS() as db:
        db.add(models.Ledger(id="lg", user_id="u1", external_id="e",
                             name="L", currency="JPY"))
        db.add(models.ReadTxProjection(
            sync_id="t1", ledger_id="lg", user_id="u1", tx_type="expense",
            amount=1.0, note="n", account_sync_id=None, account_name=None,
            happened_at=datetime(2026, 10, 20, tzinfo=timezone.utc)))
        db.add(models.ReadTxSplitProjection(
            ledger_id="lg", tx_sync_id="t1", seq=0,
            account_sync_id="a1", amount=1.0))
        db.commit()

        cleared = _models_truncated_by_truncate_ledger()
        before = {
            n: db.scalar(select(func.count()).select_from(obj))
            for n, obj in _read_projection_tables().items()
            if n in cleared
        }
        # 只要求**我预置过的那两张**有行;budget 本来就没数据,
        # 它照样要被清(空表被清是 no-op,不影响判据)。
        seeded = ("ReadTxProjection", "ReadTxSplitProjection")
        assert all(before[n] > 0 for n in seeded), (
            f"预置数据没进去:{before} —— 测试自己失效了")
        assert len(before) >= 2, f"清单太短,判据形同虚设:{before}"

        projection_mod._truncate_ledger(db, "lg")
        db.commit()

        after = {
            n: db.scalar(select(func.count()).select_from(obj))
            for n, obj in _read_projection_tables().items()
            if n in cleared
        }

    assert after == {k: 0 for k in before}, (
        f"运行期还有残留:{after} —— 静态判据过了但真删不掉。"
        f"(循环体可能被改坏,比如加了条件)")