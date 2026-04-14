import uuid
from datetime import datetime, time
from typing import Any, Optional
from decimal import Decimal
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, JSON, Numeric,
    String, Text, Time, Enum, Integer, func, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TransactionType(str, PyEnum):
    income = "income"
    expense = "expense"


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    full_name: Mapped[Optional[str]] = mapped_column(String(256))
    goal: Mapped[Optional[str]] = mapped_column(Text)
    savings_goal_name: Mapped[Optional[str]] = mapped_column(Text)
    savings_goal_amount_rub: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2))
    custom_categories: Mapped[Optional[list[Any]]] = mapped_column(JSON, nullable=True)
    default_currency: Mapped[str] = mapped_column(String(8), default="RUB")
    daily_report_time: Mapped[time] = mapped_column(Time, default=time(21, 0))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")
    category_limits: Mapped[list["UserCategoryLimit"]] = relationship(back_populates="user")


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_by_admin_id: Mapped[int] = mapped_column(BigInteger)
    used_by_telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int] = mapped_column(Integer, default=1)


class UserCategoryMapping(Base):
    __tablename__ = "user_category_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    keyword: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("user_id", "keyword", name="uq_user_keyword"),)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType))
    amount_original: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    currency_original: Mapped[str] = mapped_column(String(8))
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    exchange_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    category: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    user: Mapped["User"] = relationship(back_populates="transactions")


class UserCategoryLimit(Base):
    __tablename__ = "user_category_limits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    category: Mapped[str] = mapped_column(Text)
    monthly_limit_rub: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="category_limits")

    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_user_category_limit"),)
