"""read_tx_projection: tax_amount — 消费税税额

设计见 docs/aegis/plans/2026-10-03-selfhost-tax-feature-fork.md(T4)。

tax_amount = 一笔支出中包含的税额(日本「消費税」)。**amount 语义不变,仍是
实付总额**;税额是叠加在上面的附加维度,统计时从 amount 里剥出来归入
「税与保险」分类,所以「月支出总额 = 实付金额」这个不变式不受影响。

与税率的关系:税率**不存**。日本小票印「合計 / 消費税等」两个绝对值,
不印税率(或各家标注位置不一),且各家舍入方式不同 —— 1780 ÷ 1.08 = 1648.15
会与收银机显示的 1649 差 1 円。存绝对值天然对得上收银机,税率可由
tax_amount / (amount - tax_amount) 反推展示。

多币种:**只存原币**,不存折本位币值。折算后的税额在统计循环里按
`native_amount * (tax_amount / amount)` 推导,避免复制 native_amount
那个「改了 amount 没改折算值」的联动 bug(SYNC_ARCHITECTURE §4.5)。

nullable 无需回填:存量行 = 无税,行为与升级前完全一致,饼图不出现税额切片。

Revision ID: 0020_tx_tax_amount
Revises: 0019_account_hidden
Create Date: 2026-10-03
"""

import sqlalchemy as sa
from alembic import op


revision = "0020_tx_tax_amount"
down_revision = "0019_account_hidden"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "read_tx_projection",
        sa.Column("tax_amount", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("read_tx_projection", "tax_amount")
