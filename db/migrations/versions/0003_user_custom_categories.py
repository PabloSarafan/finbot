"""User custom categories JSON and widen category text columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("custom_categories", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column(
        "transactions",
        "category",
        existing_type=sa.String(64),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "user_category_mappings",
        "category",
        existing_type=sa.String(64),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "user_category_mappings",
        "category",
        existing_type=sa.Text(),
        type_=sa.String(64),
        existing_nullable=False,
    )
    op.alter_column(
        "transactions",
        "category",
        existing_type=sa.Text(),
        type_=sa.String(64),
        existing_nullable=False,
    )
    op.drop_column("users", "custom_categories")
