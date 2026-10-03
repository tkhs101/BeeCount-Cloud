"""read_tx_split_projection: 组合支付（一笔订单多个支付方式）

设计见 docs/aegis/plans/2026-10-03-selfhost-tax-feature-fork.md(组合支付段)。

一笔 5000 円 的订单，卡里付 3000、现金付 2000。**父交易仍然是一条**:
`read_tx_projection.amount` 仍是 5000（实付总额不变），`account_*` 为空。
拆分金额落在本子表，每行一条腿。

## 为什么要独立子表，而不是父行的 JSON 列

余额要在 SQL 里 `GROUP BY account_sync_id`。塞进 JSON 列就得把所有交易行捞回
Python 再展开 —— 现在余额是纯 SQL 聚合的，1 万笔交易差一个数量级。

而 SQLite 与 PostgreSQL 的 JSON 展开函数完全不同（`json_each` vs
`jsonb_to_recordset`），本仓是**双方言**的，写 SQL 方言分支不值得。

## 为什么只存 account_sync_id，不存 account_name

存名字就得维护 `projection.rename_cascade_account` 的子表 UPDATE —— 账户
改名时子表里的名字会变旧（余额聚合按 sync_id 仍然对，但展示和 CSV 会显示
旧名）。这是个容易漏的点，不如干脆不存：展示时回查账户名即可。

`delete_account` 的关联交易守卫会拦住删除被引用的账户（见
`snapshot_mutator.delete_account`），所以不会出现悬空引用。

## 不变式

1. `sum(splits.amount) == 父交易.amount`（容差 1e-6），否则 400
   —— 5000 拆 3000+1999 会让对账永远差 1 円，宁可报错也不自动补差
2. 有 splits 时父交易 `account_sync_id` / `account_name` 必须为 NULL
   —— 否则父行再动一次账户，余额双倍
3. 只支持 `tx_type == 'expense'` —— 收入拆分本质是转账，走转账模型更对
4. 各 split 与父交易**同一币种** —— 跨币种拆分要引入汇率时点问题

外键级联删除：父交易没了，拆分腿也不该留。

Revision ID: 0021_tx_splits
Revises: 0020_tx_tax_amount
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op


revision = "0021_tx_splits"
down_revision = "0020_tx_tax_amount"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "read_tx_split_projection",
        sa.Column("ledger_id", sa.String(36), nullable=False),
        sa.Column("tx_sync_id", sa.String(255), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("account_sync_id", sa.String(255), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("ledger_id", "tx_sync_id", "seq"),
        sa.ForeignKeyConstraint(
            ["ledger_id", "tx_sync_id"],
            ["read_tx_projection.ledger_id", "read_tx_projection.sync_id"],
            ondelete="CASCADE",
        ),
    )
    # 余额聚合的核心查询:按账户分组求和。复合 PK 前缀是 (ledger_id, tx_sync_id)，
    # 这条查询要的是 account_sync_id，所以必须单列索引。
    op.create_index(
        "ix_read_tx_split_account",
        "read_tx_split_projection",
        ["ledger_id", "account_sync_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_read_tx_split_account", table_name="read_tx_split_projection")
    op.drop_table("read_tx_split_projection")
