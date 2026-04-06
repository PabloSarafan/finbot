"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-06

"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("full_name", sa.String(256), nullable=True),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("default_currency", sa.String(8), nullable=False, server_default="RUB"),
        sa.Column("daily_report_time", sa.Time(), nullable=False, server_default="21:00:00"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "invite_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("code", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by_admin_id", sa.BigInteger(), nullable=False),
        sa.Column("used_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_invite_codes_code", "invite_codes", ["code"])

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_id"), nullable=False),
        sa.Column("type", sa.Enum("income", "expense", name="transactiontype"), nullable=False),
        sa.Column("amount_original", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency_original", sa.String(8), nullable=False),
        sa.Column("amount_rub", sa.Numeric(18, 2), nullable=False),
        sa.Column("exchange_rate", sa.Numeric(18, 6), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_transactions_user_id", "transactions", ["user_id"])
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])


def downgrade() -> None:
    op.drop_table("transactions")
    op.drop_table("invite_codes")
    op.drop_table("users")
    op.execute("DROP TYPE IF EXISTS transactiontype")
