from datetime import date, datetime, timezone
from decimal import Decimal
import re
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command, or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, BufferedInputFile
from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Transaction, TransactionType, User, UserCategoryLimit
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


class LimitsStates(StatesGroup):
    waiting_for_limits = State()


class SavingsStates(StatesGroup):
    waiting_for_goal = State()


SAVINGS_CATEGORY = "Копилка 🏦"


def _month_start(today: date) -> datetime:
    return datetime(today.year, today.month, 1, tzinfo=timezone.utc)


def _next_month_start(today: date) -> datetime:
    if today.month == 12:
        return datetime(today.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(today.year, today.month + 1, 1, tzinfo=timezone.utc)


def _parse_limit_lines(text: str) -> list[tuple[str, Decimal]]:
    # Supports "Категория 10000", "Категория: 10000", "Категория - 10000"
    rows: list[tuple[str, Decimal]] = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^\s*(.+?)\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)\s*$", line)
        if not m:
            continue
        category = m.group(1).strip()
        value = Decimal(m.group(2).replace(",", "."))
        if value < 0:
            continue
        rows.append((category, value))
    return rows


async def _read_limits_map(session: AsyncSession, user_id: int) -> dict[str, Decimal]:
    result = await session.execute(
        select(UserCategoryLimit).where(UserCategoryLimit.user_id == user_id)
    )
    limits = result.scalars().all()
    return {x.category: x.monthly_limit_rub for x in limits}


def _parse_savings_goal(text: str) -> Optional[tuple[str, Decimal]]:
    raw = text.strip()
    if not raw:
        return None
    # "<name>; <amount>" or just "<amount>"
    if ";" in raw:
        name, amount_part = raw.split(";", 1)
        name = name.strip() or "Копилка"
        amount_part = amount_part.strip()
    else:
        name = "Копилка"
        amount_part = raw
    m = re.match(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*$", amount_part)
    if not m:
        return None
    val = Decimal(m.group(1).replace(",", "."))
    if val <= 0:
        return None
    return name, val


async def _read_savings_stats(
    session: AsyncSession, user_id: int, month_start: datetime
) -> tuple[Decimal, Decimal]:
    all_time_result = await session.execute(
        select(Transaction).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.type == TransactionType.income,
                Transaction.category == SAVINGS_CATEGORY,
            )
        )
    )
    all_time = all_time_result.scalars().all()
    total_saved = sum(t.amount_rub for t in all_time)

    month_result = await session.execute(
        select(Transaction).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.type == TransactionType.income,
                Transaction.category == SAVINGS_CATEGORY,
                Transaction.created_at >= month_start,
            )
        )
    )
    month_saved = sum(t.amount_rub for t in month_result.scalars().all())
    return total_saved, month_saved


@router.message(or_f(Command("report"), F.text == "📊 Отчёт за сегодня"))
async def cmd_report(message: Message, session: AsyncSession, user: User = None) -> None:
    if user is None:
        return

    today = date.today()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    month_start = _month_start(today)

    result = await session.execute(
        select(Transaction).where(
            and_(
                Transaction.user_id == user.telegram_id,
                Transaction.created_at >= start,
            )
        )
    )
    txs = result.scalars().all()

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
    if not txs:
        parts.append("📭 Сегодня транзакций нет.")
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

    # Month-to-date expenses vs configured monthly budget
    mtd_result = await session.execute(
        select(Transaction).where(
            and_(
                Transaction.user_id == user.telegram_id,
                Transaction.created_at >= month_start,
                Transaction.type == TransactionType.expense,
            )
        )
    )
    mtd_expenses = mtd_result.scalars().all()
    mtd_total = sum(t.amount_rub for t in mtd_expenses)
    limits_map = await _read_limits_map(session, user.telegram_id)
    plan_total = sum(limits_map.values()) if limits_map else Decimal("0")
    if plan_total > 0:
        pct = (mtd_total / plan_total * Decimal("100"))
        parts.append(
            f"📅 *Траты за месяц (на сегодня):* {mtd_total:,.0f} ₽ / {plan_total:,.0f} ₽ "
            f"({pct:,.0f}%)"
        )

    total_saved, month_saved = await _read_savings_stats(session, user.telegram_id, month_start)
    if total_saved > 0 or user.savings_goal_amount_rub:
        savings_line = f"🏦 *Копилка:* {total_saved:,.0f} ₽"
        if user.savings_goal_amount_rub and user.savings_goal_amount_rub > 0:
            progress = total_saved / user.savings_goal_amount_rub * Decimal("100")
            savings_line += (
                f" / {user.savings_goal_amount_rub:,.0f} ₽ ({progress:,.0f}%)"
                f"\n📈 За месяц в копилку: {month_saved:,.0f} ₽"
            )
        parts.append(savings_line)

    await message.answer("\n\n".join(parts), parse_mode="Markdown")


@router.message(or_f(Command("month"), F.text == "📅 Месячный отчёт"))
async def cmd_month(message: Message, session: AsyncSession, user: User = None) -> None:
    if user is None:
        return

    today = date.today()
    start = _month_start(today)
    end = _next_month_start(today)

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
    limits_map = await _read_limits_map(session, user.telegram_id)
    plan_lines: list[str] = []
    if limits_map:
        for cat, spent in sorted(by_cat.items(), key=lambda x: -x[1]):
            plan = limits_map.get(cat, Decimal("0"))
            if plan > 0:
                pct = (spent / plan * Decimal("100"))
                plan_lines.append(f"  • {cat}: {spent:,.0f} ₽ / {plan:,.0f} ₽ ({pct:,.0f}%)")
            else:
                plan_lines.append(f"  • {cat}: {spent:,.0f} ₽ / —")

    limits_block = ""
    if plan_lines:
        limits_block = "\n\n📌 *По категориям (факт vs план):*\n" + "\n".join(plan_lines[:12])

    total_saved, month_saved = await _read_savings_stats(session, user.telegram_id, start)
    savings_block = ""
    if total_saved > 0 or user.savings_goal_amount_rub:
        goal_name = user.savings_goal_name or "Копилка"
        savings_block = (
            f"\n\n🏦 *{goal_name}:* {total_saved:,.0f} ₽"
            f"\n📈 За месяц в копилку: {month_saved:,.0f} ₽"
        )
        if user.savings_goal_amount_rub and user.savings_goal_amount_rub > 0:
            remaining = max(Decimal("0"), user.savings_goal_amount_rub - total_saved)
            progress = total_saved / user.savings_goal_amount_rub * Decimal("100")
            tip = (
                "Отличный темп, продолжай!" if remaining == 0 else
                f"Осталось {remaining:,.0f} ₽. Попробуй увеличить ежемесячный вклад в копилку."
            )
            savings_block += (
                f"\n🎯 Цель: {user.savings_goal_amount_rub:,.0f} ₽ ({progress:,.0f}%)"
                f"\n💡 {tip}"
            )

    await message.answer(
        f"📅 *{month_label}*\n\n"
        f"💰 Доходы: *{total_inc:,.0f} ₽*\n"
        f"💸 Расходы: *{total_exp:,.0f} ₽*\n"
        f"📊 Баланс: *{sign}{balance:,.0f} ₽*\n\n"
        f"🎯 *Советы по финансам:*\n{advice}"
        f"{limits_block}"
        f"{savings_block}",
        parse_mode="Markdown",
    )


@router.message(or_f(Command("stash"), F.text == "🏦 Копилка"))
async def cmd_stash(message: Message, session: AsyncSession, state: FSMContext, user: User = None) -> None:
    if user is None:
        return
    month_start = _month_start(date.today())
    total_saved, month_saved = await _read_savings_stats(session, user.telegram_id, month_start)

    lines = [
        "🏦 *Копилка*",
        f"Накоплено всего: *{total_saved:,.0f} ₽*",
        f"За текущий месяц: *{month_saved:,.0f} ₽*",
    ]
    if user.savings_goal_amount_rub and user.savings_goal_amount_rub > 0:
        progress = total_saved / user.savings_goal_amount_rub * Decimal("100")
        lines.extend([
            f"Цель: *{user.savings_goal_amount_rub:,.0f} ₽* ({progress:,.0f}%)",
            f"Название цели: *{user.savings_goal_name or 'Копилка'}*",
        ])
    lines.extend([
        "",
        "Чтобы установить/обновить цель, отправь:",
        "`Название цели; 500000` или просто `500000`",
        "Отправь `/skip`, чтобы убрать цель.",
    ])
    await state.set_state(SavingsStates.waiting_for_goal)
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(SavingsStates.waiting_for_goal)
async def process_stash_goal(message: Message, session: AsyncSession, state: FSMContext, user: User = None) -> None:
    if user is None:
        await state.clear()
        return
    text = message.text.strip()
    if text.lower() == "/skip":
        user.savings_goal_name = None
        user.savings_goal_amount_rub = None
        await session.commit()
        await state.clear()
        await message.answer("✅ Цель копилки очищена.")
        return

    parsed = _parse_savings_goal(text)
    if not parsed:
        await message.answer(
            "Не смог разобрать цель. Используй формат:\n`Квартира; 500000`\nили `500000`",
            parse_mode="Markdown",
        )
        return
    name, amount = parsed
    user.savings_goal_name = name
    user.savings_goal_amount_rub = amount
    await session.commit()
    await state.clear()
    await message.answer(f"✅ Цель копилки сохранена: *{name}* — *{amount:,.0f} ₽*", parse_mode="Markdown")


@router.message(or_f(Command("limits"), F.text == "📌 Лимиты"))
async def cmd_limits(message: Message, session: AsyncSession, state: FSMContext, user: User = None) -> None:
    if user is None:
        return
    limits_map = await _read_limits_map(session, user.telegram_id)
    lines = [
        "📌 *Лимиты по категориям (в месяц)*",
        "",
        "Формат ввода: `Категория 15000` (каждую категорию с новой строки).",
        "Пример:",
        "`Еда 30000`",
        "`Кафе 15000`",
        "",
        "Отправь `/skip`, чтобы очистить все лимиты.",
    ]
    if limits_map:
        lines.extend(["", "Текущие лимиты:"])
        for cat, val in sorted(limits_map.items(), key=lambda x: x[0].lower()):
            lines.append(f"• {cat} — {val:,.0f} ₽")
    await state.set_state(LimitsStates.waiting_for_limits)
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(LimitsStates.waiting_for_limits)
async def process_limits(message: Message, session: AsyncSession, state: FSMContext, user: User = None) -> None:
    if user is None:
        await state.clear()
        return
    text = message.text.strip()
    if text.lower() == "/skip":
        await session.execute(
            delete(UserCategoryLimit).where(UserCategoryLimit.user_id == user.telegram_id)
        )
        await session.commit()
        await state.clear()
        await message.answer("✅ Все лимиты удалены.")
        return

    parsed = _parse_limit_lines(text)
    if not parsed:
        await message.answer(
            "Не смог разобрать лимиты. Используй формат:\n`Еда 30000`\n`Кафе 15000`",
            parse_mode="Markdown",
        )
        return

    await session.execute(
        delete(UserCategoryLimit).where(UserCategoryLimit.user_id == user.telegram_id)
    )
    for cat, val in parsed:
        session.add(
            UserCategoryLimit(
                user_id=user.telegram_id,
                category=cat,
                monthly_limit_rub=val,
            )
        )
    await session.commit()
    await state.clear()
    await message.answer(f"✅ Сохранил лимиты: {len(parsed)} категорий.")
