"""Savings goal fields on users

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-10

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("savings_goal_name", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("savings_goal_amount_rub", sa.Numeric(18, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "savings_goal_amount_rub")
    op.drop_column("users", "savings_goal_name")
