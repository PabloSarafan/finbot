"""User category mappings

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_category_mappings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_id"), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.UniqueConstraint("user_id", "keyword", name="uq_user_keyword"),
    )
    op.create_index("ix_user_category_mappings_user_id", "user_category_mappings", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_category_mappings")
