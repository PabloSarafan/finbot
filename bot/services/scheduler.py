import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Transaction, TransactionType, User, UserCategoryLimit
from db.session import AsyncSessionFactory
from bot.services.charts import build_pie_chart, build_waterfall_chart
from bot.services.llm import generate_monthly_advice

logger = logging.getLogger(__name__)


def _month_start(today: date) -> datetime:
    return datetime(today.year, today.month, 1, tzinfo=timezone.utc)


def _next_month_start(today: date) -> datetime:
    if today.month == 12:
        return datetime(today.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(today.year, today.month + 1, 1, tzinfo=timezone.utc)


async def _read_limits_map(session: AsyncSession, user_id: int) -> dict[str, Decimal]:
    result = await session.execute(
        select(UserCategoryLimit).where(UserCategoryLimit.user_id == user_id)
    )
    limits = result.scalars().all()
    return {x.category: x.monthly_limit_rub for x in limits}


async def send_daily_report(bot: Bot, user: User, session: AsyncSession) -> None:
    today = date.today()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    month_start = _month_start(today)

    result = await session.execute(
        select(Transaction)
        .where(
            and_(
                Transaction.user_id == user.telegram_id,
                Transaction.created_at >= start,
            )
        )
    )
    txs = result.scalars().all()

    expenses = [t for t in txs if t.type == TransactionType.expense]
    incomes = [t for t in txs if t.type == TransactionType.income]

    total_exp = sum((t.amount_rub for t in expenses), Decimal("0"))
    total_inc = sum((t.amount_rub for t in incomes), Decimal("0"))

    exp_by_cat: dict[str, float] = {}
    for t in expenses:
        exp_by_cat[t.category] = exp_by_cat.get(t.category, 0) + float(t.amount_rub)

    inc_by_cat: dict[str, float] = {}
    for t in incomes:
        inc_by_cat[t.category] = inc_by_cat.get(t.category, 0) + float(t.amount_rub)

    exp_lines = "\n".join(
        f"  • {cat} — {amt:,.0f} ₽"
        for cat, amt in sorted(exp_by_cat.items(), key=lambda x: -x[1])
    )
    inc_lines = "\n".join(
        f"  • {cat} — {amt:,.0f} ₽"
        for cat, amt in sorted(inc_by_cat.items(), key=lambda x: -x[1])
    )

    balance = total_inc - total_exp
    sign = "+" if balance >= 0 else ""

    text = (
        f"📊 *Итог за {today.strftime('%d %B')}*\n\n"
        f"💸 Расходы: *{total_exp:,.0f} ₽*"
    )
    if exp_lines:
        text += f"\n{exp_lines}"
    text += f"\n\n💰 Доходы: *{total_inc:,.0f} ₽*"
    if inc_lines:
        text += f"\n{inc_lines}"
    text += f"\n\n📈 Баланс дня: *{sign}{balance:,.0f} ₽*"
    mtd_result = await session.execute(
        select(Transaction)
        .where(
            and_(
                Transaction.user_id == user.telegram_id,
                Transaction.created_at >= month_start,
                Transaction.type == TransactionType.expense,
            )
        )
    )
    mtd_expenses = mtd_result.scalars().all()
    mtd_total = sum((t.amount_rub for t in mtd_expenses), Decimal("0"))
    limits_map = await _read_limits_map(session, user.telegram_id)
    plan_total = sum(limits_map.values()) if limits_map else Decimal("0")
    if plan_total > 0:
        pct = mtd_total / plan_total * Decimal("100")
        text += (
            f"\n\n📅 *Траты за месяц (на сегодня):* {mtd_total:,.0f} ₽ / {plan_total:,.0f} ₽ "
            f"({pct:,.0f}%)"
        )
    await bot.send_message(user.telegram_id, text, parse_mode="Markdown")


async def send_monthly_report(bot: Bot, user: User, session: AsyncSession) -> None:
    today = date.today()
    # Report is for previous month
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1

    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = _next_month_start(date(year, month, 1))

    result = await session.execute(
        select(Transaction)
        .where(
            and_(
                Transaction.user_id == user.telegram_id,
                Transaction.created_at >= start,
                Transaction.created_at < end,
            )
        )
    )
    txs = result.scalars().all()

    if not txs:
        await bot.send_message(
            user.telegram_id,
            f"📅 За {month:02d}/{year} трат не зафиксировано.",
        )
        return

    expenses = [t for t in txs if t.type == TransactionType.expense]
    incomes = [t for t in txs if t.type == TransactionType.income]

    total_exp = sum((t.amount_rub for t in expenses), Decimal("0"))
    total_inc = sum((t.amount_rub for t in incomes), Decimal("0"))
    balance = total_inc - total_exp

    by_cat: dict[str, float] = {}
    for t in expenses:
        by_cat[t.category] = by_cat.get(t.category, 0) + float(t.amount_rub)

    by_cat_dec = {k: Decimal(str(v)) for k, v in by_cat.items()}

    month_label = start.strftime("%B %Y")

    # Pie chart
    pie_bytes = build_pie_chart(by_cat_dec, f"Расходы за {month_label}")
    # Waterfall chart
    wf_bytes = build_waterfall_chart(total_inc, by_cat_dec, month_label)

    advice = ""
    if user.goal:
        sorted_cats = sorted(by_cat_dec.items(), key=lambda x: -x[1])[:5]
        advice = await generate_monthly_advice(
            goal=user.goal,
            month=month_label,
            income=total_inc,
            expenses=total_exp,
            balance=balance,
            categories=sorted_cats,
        )

    sign = "+" if balance >= 0 else ""
    limits_map = await _read_limits_map(session, user.telegram_id)
    plan_lines: list[str] = []
    if limits_map:
        for cat, spent in sorted(by_cat_dec.items(), key=lambda x: -x[1]):
            plan = limits_map.get(cat, Decimal("0"))
            if plan > 0:
                pct = spent / plan * Decimal("100")
                plan_lines.append(f"  • {cat}: {spent:,.0f} ₽ / {plan:,.0f} ₽ ({pct:,.0f}%)")
            else:
                plan_lines.append(f"  • {cat}: {spent:,.0f} ₽ / —")
    limits_block = ""
    if plan_lines:
        limits_block = "\n\n📌 *По категориям (факт vs план):*\n" + "\n".join(plan_lines[:12])

    summary = (
        f"📅 *Месячный отчёт за {month_label}*\n\n"
        f"💰 Доходы: *{total_inc:,.0f} ₽*\n"
        f"💸 Расходы: *{total_exp:,.0f} ₽*\n"
        f"📊 Баланс: *{sign}{balance:,.0f} ₽*"
    )
    if advice:
        summary += f"\n\n🎯 *Советы по финансам:*\n{advice}"
    summary += limits_block

    await bot.send_photo(
        user.telegram_id,
        photo=BufferedInputFile(pie_bytes, filename="pie.png"),
        caption="🥧 Расходы по категориям",
    )
    await bot.send_photo(
        user.telegram_id,
        photo=BufferedInputFile(wf_bytes, filename="waterfall.png"),
        caption="📊 Доходы / расходы",
    )
    await bot.send_message(user.telegram_id, summary, parse_mode="Markdown")


async def _run_daily_reports(bot: Bot) -> None:
    async with AsyncSessionFactory() as session:
        result = await session.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()
        for user in users:
            try:
                await send_daily_report(bot, user, session)
            except Exception as e:
                logger.error(f"Daily report failed for {user.telegram_id}: {e}")


async def _run_monthly_reports(bot: Bot) -> None:
    async with AsyncSessionFactory() as session:
        result = await session.execute(select(User).where(User.is_active == True))
        users = result.scalars().all()
        for user in users:
            try:
                await send_monthly_report(bot, user, session)
            except Exception as e:
                logger.error(f"Monthly report failed for {user.telegram_id}: {e}")


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Daily report at configured hour (UTC)
    scheduler.add_job(
        _run_daily_reports,
        CronTrigger(hour=settings.daily_report_hour, minute=0),
        args=[bot],
        id="daily_report",
        replace_existing=True,
    )

    # Monthly report on 1st of each month at 09:00 UTC
    scheduler.add_job(
        _run_monthly_reports,
        CronTrigger(day=1, hour=9, minute=0),
        args=[bot],
        id="monthly_report",
        replace_existing=True,
    )

    return scheduler
