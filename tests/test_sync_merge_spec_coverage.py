"""`_LEDGER_MERGE_SPECS['transaction']` 必须覆盖**所有**该由 sync 承载的列。

## 这是同一族的第三张「手工登记表」

本会话已经为两张表建立了「加东西必须同步登记」的护栏:

| 登记表 | 漏了会怎样 |
|---|---|
| `_projection_row_to_tx_dict`(反向桥) | Web PATCH 静默抹掉新列 |
| `_truncate_ledger`(清理清单) | rebuild / 还原后余额「凭空多出来」 |
| **`_LEDGER_MERGE_SPECS`(本文件)** | **`/sync/push` 静默丢弃新列** |

第三张的失败模式最隐蔽:mobile 推一笔带新字段的交易,服务端**接受**、
返回 200、`accepted` 计数正常 —— 但那个字段根本没落库。
下一次 pull 回来还是没有,两端「看起来同步了」。

## 白名单式判据

新列加进 `ReadTxProjection` 时,这条**默认红**,要求显式论证它该不该进
merge spec。理由和白名单的其他两处一致:黑名单在加列时**默认通过**,
而那正是要防的情况。

## 白名单里为什么只有三个

`ledger_id` / `user_id` / `source_change_id` 由服务端从 change 信封与
会话上下文赋值 —— 它们**不在 payload 里**,也不该被客户端覆盖。
`account_name` 之类则是真·payload 字段,必须登记。
"""
from __future__ import annotations

from src import models
from src.sync_applier import _LEDGER_MERGE_SPECS

#: 服务端赋值的列 —— **不在 payload 里**,客户端推什么都不该覆盖它们。
SERVER_ASSIGNED = frozenset({
    "ledger_id",          # 来自 change.ledger_id(路由强制)
    "user_id",            # 来自会话
    "source_change_id",   # 来自 SyncChange 行
})

#: 上限:白名单被人一路塞真列进去时这里会拦住(防止「让测试绿」)
_SERVER_ASSIGNED_MAX = 5


def _spec() -> object:
    assert "transaction" in _LEDGER_MERGE_SPECS, (
        f"transaction 不在 _LEDGER_MERGE_SPECS 里:{sorted(_LEDGER_MERGE_SPECS)}")
    return _LEDGER_MERGE_SPECS["transaction"]


def test_the_spec_exists_and_is_not_empty() -> None:
    """判据自检:spec 真的取到了、且有字段。

    没有这条,`_LEDGER_MERGE_SPECS` 改名或结构变了,下面那条会拿到空
    集合 =「所有列都缺」或「都不缺」,两种都可能全绿。
    """
    spec = _spec()
    assert len(spec.fields) > 10, (
        f"merge spec 只有 {len(spec.fields)} 个字段 —— 取错了?"
        f"(结构可能变了,判据要跟着改)")


def test_every_client_carried_column_is_in_the_merge_spec() -> None:
    """每列要么在 merge spec 里,要么在「服务端赋值」白名单里。"""
    spec = _spec()
    covered = {f[1] for f in spec.fields}
    cols = set(models.ReadTxProjection.__table__.columns.keys())

    assert len(SERVER_ASSIGNED) <= _SERVER_ASSIGNED_MAX, (
        f"白名单涨到 {len(SERVER_ASSIGNED)} 项 —— "
        f"新列想蒙混过关,先怀疑是这里被放宽了")

    missing = sorted(cols - covered - SERVER_ASSIGNED)
    assert not missing, (
        f"这些列不在 `_LEDGER_MERGE_SPECS['transaction']` 里:{missing}\n"
        f"后果:**静默丢弃** —— mobile 推上来返回 200、accepted 计数正常,"
        f"但那个字段不落库;下次 pull 回来还是没有,两端「看起来同步了」。\n"
        f"若它本就该服务端赋值,把它加进本文件的 `SERVER_ASSIGNED` 并写明理由。")


def test_whitelist_entries_are_all_real_columns() -> None:
    """白名单里的名字得**真的是列** —— 否则白名单会掩盖漏登记。"""
    cols = set(models.ReadTxProjection.__table__.columns.keys())
    ghost = sorted(SERVER_ASSIGNED - cols)
    assert not ghost, (
        f"白名单里有 {ghost},但 `ReadTxProjection` 里没有这些列 —— "
        f"重命名后忘了同步,白名单会掩盖漏登记的列。")


def test_merge_spec_entries_all_map_to_real_columns() -> None:
    """spec 里的每个目标列都得真实存在 —— 拼错列名会静默写不进去。"""
    cols = set(models.ReadTxProjection.__table__.columns.keys())
    ghost = sorted({f[1] for f in _spec().fields} - cols)
    assert not ghost, (
        f"merge spec 指向不存在的列:{ghost} —— "
        f"静态判据抓不到「拼错」,这条是运行期兜底。")


def test_splits_are_not_expected_in_the_merge_spec() -> None:
    """`splits` 在**子表**里,故意不进 merge spec。

    它由 `_merge_tx_splits_after_merge` 后处理(那份表按
    `getattr(existing, db_attr)` 读**模型列**,而 splits 没有列)。
    这里显式记一笔,免得下一个人看到「列都在但腿不在」而误加。
    """
    spec = _spec()
    assert "splits" not in {f[1] for f in spec.fields}, (
        "splits 进 merge spec 了 —— `_MergeSpec` 按模型列读,"
        "子表没有列,会在这里炸。应该走 `_merge_tx_splits_after_merge`。")
    from src import sync_applier
    assert hasattr(sync_applier, "_merge_tx_splits_after_merge"), (
        "处理 splits 的后处理函数不见了 —— 腿会被静默丢弃。")