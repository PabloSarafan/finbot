import logging
from datetime import date, datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Transaction, TransactionType, User
from db.session import AsyncSessionFactory
from bot.services.charts import build_pie_chart, build_waterfall_chart
from bot.services.llm import generate_monthly_advice

logger = logging.getLogger(__name__)


async def send_daily_report(bot: Bot, user: User, session: AsyncSession) -> None:
    today = date.today()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

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

    if not txs:
        await bot.send_message(user.telegram_id, "📊 Сегодня трат не зафиксировано.")
        return

    expenses = [t for t in txs if t.type == TransactionType.expense]
    incomes = [t for t in txs if t.type == TransactionType.income]

    total_exp = sum(t.amount_rub for t in expenses)
    total_inc = sum(t.amount_rub for t in incomes)

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
        f"💸 Расходы: *{total_exp:,.0f} ₽*\n"
        f"{exp_lines}\n\n"
        f"💰 Доходы: *{total_inc:,.0f} ₽*\n"
        f"{inc_lines}\n\n"
        f"📈 Баланс дня: *{sign}{balance:,.0f} ₽*"
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
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

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

    total_exp = sum(t.amount_rub for t in expenses)
    total_inc = sum(t.amount_rub for t in incomes)
    balance = total_inc - total_exp

    by_cat: dict[str, float] = {}
    for t in expenses:
        by_cat[t.category] = by_cat.get(t.category, 0) + float(t.amount_rub)

    from decimal import Decimal
    by_cat_dec = {k: Decimal(str(v)) for k, v in by_cat.items()}

    month_label = start.strftime("%B %Y")

    # Pie chart
    pie_bytes = build_pie_chart(by_cat_dec, f"Расходы за {month_label}")
    # Waterfall chart
    wf_bytes = build_waterfall_chart(total_inc, by_cat_dec, month_label)

    # LLM advice
    sorted_cats = sorted(by_cat_dec.items(), key=lambda x: -x[1])[:5]
    advice = await generate_monthly_advice(
        goal=user.goal or "",
        month=month_label,
        income=total_inc,
        expenses=total_exp,
        balance=balance,
        categories=sorted_cats,
    )

    sign = "+" if balance >= 0 else ""
    summary = (
        f"📅 *Месячный отчёт за {month_label}*\n\n"
        f"💰 Доходы: *{total_inc:,.0f} ₽*\n"
        f"💸 Расходы: *{total_exp:,.0f} ₽*\n"
        f"📊 Баланс: *{sign}{balance:,.0f} ₽*\n\n"
        f"🎯 *Советы по финансам:*\n{advice}"
    )

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
