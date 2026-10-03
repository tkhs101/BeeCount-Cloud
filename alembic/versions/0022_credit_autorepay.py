"""user_account_projection: 信用卡自动还款配置

设计见 docs/aegis/plans/2026-10-04-credit-card-auto-repayment.md。

一笔信用卡绑一个扣款账户，到还款日自动生成一笔
`from=扣款账户 → to=信用卡` 的 transfer。

## 为什么加在这张表上而不是新建一张规则表

规则与卡是 1:1(一张卡最多一条规则),新建表要自己维护与账户的
ON DELETE CASCADE、rename cascade、以及 sync push 的 merge 登记。
加列可以直接复用已有的 `_LEDGER_MERGE_SPECS` / `rename_cascade_account`
/ 孤儿扫描四套机制。

## 三个字段的语义

| 列 | 语义 |
|---|---|
| `autorepay_enabled` | 是否启用。为 false 时下面两个字段仍然保留,「暂停」不等于「删除配置」 |
| `autorepay_from_account_sync_id` | 扣款来源账户。**存 sync_id 不存名字** |
| `autorepay_last_period` | 上次已自动还款的账期(`YYYY-MM`)。恰好一次守卫 |

## 为什么必须存 sync_id 而不存名字

前端转账是**按账户名**定位的(`TransactionsPage.tsx:1463-1467` 建
`accountByName` 小写映射)。用户给账户改个名,`account_id` 静默变 null,
退回按名反查 —— 而 `projection._resolve_account_sync_id_by_name` 在
同名多账户时返回 None,于是只剩一端生效,**钱凭空消失**。

自动还款每期都要动钱,不能有这条路。配置必须用 sync_id。

## 为什么 `autorepay_last_period` 不能靠 Idempotency-Key

`SyncPushIdempotency` 的 TTL 只有 24 小时(`write/_shared.py:636`),
跨月补跑必然失效。这列是**业务层**的恰好一次判据。

## 跨币种约束(应用层强制,不加约束)

扣款账户必须与信用卡**同币种**。transfer 不带 `native_amount` 且两端共用
同一个 `amount`,跨币种会把 ¥10000 原样加到 JPY 卡上。这一条用 CHECK
约束表达不了(要 JOIN accounts 表),放在应用层校验。
"""
from alembic import op
import sqlalchemy as sa

revision = "0022_credit_autorepay"
down_revision = "0021_tx_splits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_account_projection") as batch:
        batch.add_column(sa.Column(
            "autorepay_enabled", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ))
        batch.add_column(sa.Column(
            "autorepay_from_account_sync_id", sa.String(255), nullable=True,
        ))
        batch.add_column(sa.Column(
            "autorepay_last_period", sa.String(7), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table("user_account_projection") as batch:
        batch.drop_column("autorepay_last_period")
        batch.drop_column("autorepay_from_account_sync_id")
        batch.drop_column("autorepay_enabled")


# --------------------------------------------------------------------------- #
# 为什么没有 CHECK 约束                                                          #
#                                                                            #
# `billing_day` / `payment_due_day` / `credit_limit` 是上游就有的列,本 fork   #
# 之前**服务端零校验**(只有前端拦)。它们当时只是展示字段,脏值无害。         #
#                                                                          #
# 绑了自动还款之后它们变成**驱动资金操作**的字段 —— 账单日填 99 会让          #
# `clamp_to_month_end` 静默钳成月末,还款日填 0 会让调度永远不触发。           #
# 所以校验补在**应用层**(account 写路径),而不是在这里加 CHECK 约束:         #
#                                                                          #
# 1. 上游列加 CHECK 会让**既有脏数据**在迁移时炸掉,用户无法启动           #
# 2. 已有数据里可能有 0 / NULL 的历史值,加约束需要先洗数据                   #
# 3. 真正的兜底是调度器:配置非法时该卡**跳过并报可见的错**,而不是崩溃        #
#                                                                          #
# 同理,`autorepay_enabled` 时 `billing_day`/`payment_due_day` 非空、扣款账户 #
# 存在且同币种,这些都在应用层做,见 src/services/credit_card/config.py。     #
# --------------------------------------------------------------------------- #