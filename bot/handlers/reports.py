from datetime import date, datetime, timezone

from aiogram import Router, F
from aiogram.filters import Command, or_f
from aiogram.types import Message, BufferedInputFile
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Transaction, TransactionType, User
from bot.services.charts import build_pie_chart, build_waterfall_chart
from bot.services.llm import generate_monthly_advice

router = Router()
MONTHS_RU_GENITIVE = {
    1: "Января",
    2: "Февраля",
    3: "Марта",
    4: "Апреля",
    5: "Мая",
    6: "Июня",
    7: "Июля",
    8: "Августа",
    9: "Сентября",
    10: "Октября",
    11: "Ноября",
    12: "Декабря",
}


@router.message(or_f(Command("report"), F.text == "📊 Отчёт за сегодня"))
async def cmd_report(message: Message, session: AsyncSession, user: User = None) -> None:
    if user is None:
        return

    today = date.today()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    result = await session.execute(
        select(Transaction).where(
            and_(
                Transaction.user_id == user.telegram_id,
                Transaction.created_at >= start,
            )
        )
    )
    txs = result.scalars().all()

    if not txs:
        await message.answer("📭 Сегодня транзакций нет.")
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

    report_date = f"{today.day} {MONTHS_RU_GENITIVE[today.month]}"
    parts: list[str] = [f"📊 *Отчёт за {report_date}*"]
    if total_exp > 0:
        exp_block = f"💸 *Траты за день: {total_exp:,.0f} ₽*"
        if exp_lines:
            exp_block += f"\n{exp_lines}"
        parts.append(exp_block)
    if total_inc > 0:
        inc_block = f"💰 *Доходы за день: {total_inc:,.0f} ₽*"
        if inc_lines:
            inc_block += f"\n{inc_lines}"
        parts.append(inc_block)

    await message.answer("\n\n".join(parts), parse_mode="Markdown")


@router.message(or_f(Command("month"), F.text == "📅 Месячный отчёт"))
async def cmd_month(message: Message, session: AsyncSession, user: User = None) -> None:
    if user is None:
        return

    today = date.today()
    start = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
    if today.month == 12:
        end = datetime(today.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(today.year, today.month + 1, 1, tzinfo=timezone.utc)

    result = await session.execute(
        select(Transaction).where(
            and_(
                Transaction.user_id == user.telegram_id,
                Transaction.created_at >= start,
                Transaction.created_at < end,
            )
        )
    )
    txs = result.scalars().all()

    if not txs:
        await message.answer("📭 В этом месяце транзакций нет.")
        return

    await message.answer("⏳ Формирую отчёт, подожди...")

    expenses = [t for t in txs if t.type == TransactionType.expense]
    incomes = [t for t in txs if t.type == TransactionType.income]

    total_exp = sum(t.amount_rub for t in expenses)
    total_inc = sum(t.amount_rub for t in incomes)
    balance = total_inc - total_exp

    from decimal import Decimal
    by_cat: dict[str, Decimal] = {}
    for t in expenses:
        by_cat[t.category] = by_cat.get(t.category, Decimal("0")) + t.amount_rub

    month_label = start.strftime("%B %Y")

    pie_bytes = build_pie_chart(by_cat, f"Расходы за {month_label}")
    wf_bytes = build_waterfall_chart(total_inc, by_cat, month_label)

    sorted_cats = sorted(by_cat.items(), key=lambda x: -x[1])[:5]
    advice = await generate_monthly_advice(
        goal=user.goal or "не задана",
        month=month_label,
        income=total_inc,
        expenses=total_exp,
        balance=balance,
        categories=sorted_cats,
    )

    if pie_bytes:
        await message.answer_photo(
            BufferedInputFile(pie_bytes, filename="pie.png"),
            caption="🥧 Расходы по категориям",
        )
    if wf_bytes:
        await message.answer_photo(
            BufferedInputFile(wf_bytes, filename="waterfall.png"),
            caption="📊 Доходы / расходы",
        )

    sign = "+" if balance >= 0 else ""
    await message.answer(
        f"📅 *{month_label}*\n\n"
        f"💰 Доходы: *{total_inc:,.0f} ₽*\n"
        f"💸 Расходы: *{total_exp:,.0f} ₽*\n"
        f"📊 Баланс: *{sign}{balance:,.0f} ₽*\n\n"
        f"🎯 *Советы по финансам:*\n{advice}",
        parse_mode="Markdown",
    )
