from datetime import date, datetime, timezone
from decimal import Decimal
import logging
import re
from typing import Optional

from aiogram import Router, F
from aiogram.filters import Command, or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Transaction, TransactionType, User, UserCategoryLimit
from bot.services.charts import build_pie_chart, build_waterfall_chart
from bot.services.llm import generate_monthly_advice
from bot.services.currency import convert_from_rub, convert_to_rub, format_amount
from bot.handlers.start import MAIN_KEYBOARD

router = Router()
logger = logging.getLogger(__name__)
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
    waiting_for_delete_limit = State()


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
    normalized = text.replace("\r", "\n").replace(";", "\n").replace(",", "\n")
    for raw in normalized.split("\n"):
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


def _limits_skip_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="/skip"), KeyboardButton(text="🗑 Удалить лимит")]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Еда 30000, Кафе 15000",
    )


def _delete_limits_pick_kb(categories: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    current: list[KeyboardButton] = []
    for cat in categories:
        current.append(KeyboardButton(text=cat))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append([KeyboardButton(text="/skip")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выбери категорию для удаления",
    )


def _limits_actions_kb(has_limits: bool) -> InlineKeyboardMarkup:
    if not has_limits:
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="limits:skip")]]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Сбросить лимиты", callback_data="limits:clear")],
            [InlineKeyboardButton(text="Пропустить", callback_data="limits:skip")],
        ]
    )


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
    total_saved = sum((t.amount_rub for t in all_time), Decimal("0"))

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
    month_saved = sum((t.amount_rub for t in month_result.scalars().all()), Decimal("0"))
    return total_saved, month_saved


@router.message(or_f(Command("report"), F.text == "📊 Отчёт за сегодня"))
async def cmd_report(message: Message, session: AsyncSession, user: User = None) -> None:
    if user is None:
        return
    try:
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

        total_exp = sum((t.amount_rub for t in expenses), Decimal("0"))
        total_inc = sum((t.amount_rub for t in incomes), Decimal("0"))

        exp_by_cat: dict[str, float] = {}
        for t in expenses:
            exp_by_cat[t.category] = exp_by_cat.get(t.category, 0) + float(t.amount_rub)

        inc_by_cat: dict[str, float] = {}
        for t in incomes:
            inc_by_cat[t.category] = inc_by_cat.get(t.category, 0) + float(t.amount_rub)

        base = (user.default_currency or "RUB").upper()
        total_exp_base = await convert_from_rub(total_exp, base)
        total_inc_base = await convert_from_rub(total_inc, base)
        exp_lines_list: list[str] = []
        for cat, amt in sorted(exp_by_cat.items(), key=lambda x: -x[1]):
            val = await convert_from_rub(Decimal(str(amt)), base)
            exp_lines_list.append(f"  • {cat} — {format_amount(val, base)}")
        exp_lines = "\n".join(exp_lines_list)

        inc_lines_list: list[str] = []
        for cat, amt in sorted(inc_by_cat.items(), key=lambda x: -x[1]):
            val = await convert_from_rub(Decimal(str(amt)), base)
            inc_lines_list.append(f"  • {cat} — {format_amount(val, base)}")
        inc_lines = "\n".join(inc_lines_list)

        report_date = f"{today.day} {MONTHS_RU_GENITIVE[today.month]}"
        parts: list[str] = [f"📊 Отчёт за {report_date}"]
        if not txs:
            parts.append("📭 Сегодня транзакций нет.")
        if total_exp > 0:
            exp_block = f"💸 Траты за день: {format_amount(total_exp_base, base)}"
            if exp_lines:
                exp_block += f"\n{exp_lines}"
            parts.append(exp_block)
        if total_inc > 0:
            inc_block = f"💰 Доходы за день: {format_amount(total_inc_base, base)}"
            if inc_lines:
                inc_block += f"\n{inc_lines}"
            parts.append(inc_block)

        # Month-to-date expenses vs configured monthly budget + by-category breakdown.
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
        mtd_total = sum((t.amount_rub for t in mtd_expenses), Decimal("0"))
        mtd_by_cat: dict[str, Decimal] = {}
        for t in mtd_expenses:
            mtd_by_cat[t.category] = mtd_by_cat.get(t.category, Decimal("0")) + t.amount_rub
        limits_map = await _read_limits_map(session, user.telegram_id)
        plan_total = sum(limits_map.values()) if limits_map else Decimal("0")
        mtd_total_base = await convert_from_rub(mtd_total, base)
        if plan_total > 0:
            pct = (mtd_total / plan_total * Decimal("100"))
            plan_total_base = await convert_from_rub(plan_total, base)
            parts.append(
                f"📅 Траты за месяц (на сегодня): {format_amount(mtd_total_base, base)} / "
                f"{format_amount(plan_total_base, base)} ({pct:,.0f}%)"
            )
        elif mtd_total > 0:
            parts.append(f"📅 Траты за месяц (на сегодня): {format_amount(mtd_total_base, base)}")

        if mtd_by_cat:
            month_cat_lines: list[str] = []
            for cat, spent in sorted(mtd_by_cat.items(), key=lambda x: -x[1]):
                spent_base = await convert_from_rub(spent, base)
                plan = limits_map.get(cat, Decimal("0"))
                if plan > 0:
                    plan_base = await convert_from_rub(plan, base)
                    pct = spent / plan * Decimal("100")
                    month_cat_lines.append(
                        f"  • {cat}: {format_amount(spent_base, base)} / {format_amount(plan_base, base)} ({pct:,.0f}%)"
                    )
                else:
                    month_cat_lines.append(f"  • {cat}: {format_amount(spent_base, base)}")
            parts.append("📌 За месяц по категориям:\n" + "\n".join(month_cat_lines[:12]))

        total_saved, month_saved = await _read_savings_stats(session, user.telegram_id, month_start)
        if total_saved > 0 or user.savings_goal_amount_rub:
            total_saved_base = await convert_from_rub(total_saved, base)
            savings_line = f"🏦 Копилка: {format_amount(total_saved_base, base)}"
            if user.savings_goal_amount_rub and user.savings_goal_amount_rub > 0:
                progress = total_saved / user.savings_goal_amount_rub * Decimal("100")
                goal_base = await convert_from_rub(user.savings_goal_amount_rub, base)
                month_saved_base = await convert_from_rub(month_saved, base)
                savings_line += (
                    f" / {format_amount(goal_base, base)} ({progress:,.0f}%)"
                    f"\n📈 За месяц в копилку: {format_amount(month_saved_base, base)}"
                )
            parts.append(savings_line)

        await message.answer("\n\n".join(parts), parse_mode=None)
    except Exception:
        logger.exception("Failed to build daily report user_id=%s", user.telegram_id)
        await message.answer("Не получилось собрать отчёт за сегодня. Попробуй ещё раз через минуту.")


@router.message(or_f(Command("month"), F.text == "📅 Месячный отчёт"))
async def cmd_month(message: Message, session: AsyncSession, user: User = None) -> None:
    if user is None:
        return
    try:
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

        total_exp = sum((t.amount_rub for t in expenses), Decimal("0"))
        total_inc = sum((t.amount_rub for t in incomes), Decimal("0"))
        balance = total_inc - total_exp
        base = (user.default_currency or "RUB").upper()
        total_exp_base = await convert_from_rub(total_exp, base)
        total_inc_base = await convert_from_rub(total_inc, base)
        balance_base = await convert_from_rub(balance, base)

        by_cat: dict[str, Decimal] = {}
        for t in expenses:
            by_cat[t.category] = by_cat.get(t.category, Decimal("0")) + t.amount_rub

        month_label = start.strftime("%B %Y")

        pie_bytes = build_pie_chart(by_cat, f"Расходы за {month_label}")
        wf_bytes = build_waterfall_chart(total_inc, by_cat, month_label)

        limits_map = await _read_limits_map(session, user.telegram_id)
        advice = ""
        if user.goal:
            sorted_cats = sorted(by_cat.items(), key=lambda x: -x[1])[:5]
            limits_for_llm = sorted(limits_map.items(), key=lambda x: x[0].lower()) if limits_map else []
            advice = await generate_monthly_advice(
                goal=user.goal,
                month=month_label,
                income=total_inc,
                expenses=total_exp,
                balance=balance,
                categories=sorted_cats,
                limits=limits_for_llm,
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

        sign = "+" if balance >= 0 else "-"
        plan_lines: list[str] = []
        if limits_map:
            for cat, spent in sorted(by_cat.items(), key=lambda x: -x[1]):
                plan = limits_map.get(cat, Decimal("0"))
                if plan > 0:
                    pct = (spent / plan * Decimal("100"))
                    spent_base = await convert_from_rub(spent, base)
                    plan_base = await convert_from_rub(plan, base)
                    plan_lines.append(
                        f"  • {cat}: {format_amount(spent_base, base)} / {format_amount(plan_base, base)} ({pct:,.0f}%)"
                    )
                else:
                    spent_base = await convert_from_rub(spent, base)
                    plan_lines.append(f"  • {cat}: {format_amount(spent_base, base)} / —")

        limits_block = ""
        if plan_lines:
            limits_block = "\n\n📌 По категориям (факт vs план):\n" + "\n".join(plan_lines[:12])

        total_saved, month_saved = await _read_savings_stats(session, user.telegram_id, start)
        savings_block = ""
        if total_saved > 0 or user.savings_goal_amount_rub:
            goal_name = user.savings_goal_name or "Копилка"
            total_saved_base = await convert_from_rub(total_saved, base)
            month_saved_base = await convert_from_rub(month_saved, base)
            savings_block = (
                f"\n\n🏦 {goal_name}: {format_amount(total_saved_base, base)}"
                f"\n📈 За месяц в копилку: {format_amount(month_saved_base, base)}"
            )
            if user.savings_goal_amount_rub and user.savings_goal_amount_rub > 0:
                remaining = max(Decimal("0"), user.savings_goal_amount_rub - total_saved)
                progress = total_saved / user.savings_goal_amount_rub * Decimal("100")
                goal_base = await convert_from_rub(user.savings_goal_amount_rub, base)
                remaining_base = await convert_from_rub(remaining, base)
                tip = (
                    "Отличный темп, продолжай!" if remaining == 0 else
                    f"Осталось {format_amount(remaining_base, base)}. Попробуй увеличить ежемесячный вклад в копилку."
                )
                savings_block += (
                    f"\n🎯 Цель: {format_amount(goal_base, base)} ({progress:,.0f}%)"
                    f"\n💡 {tip}"
                )

        text = (
            f"📅 {month_label}\n\n"
            f"💰 Доходы: {format_amount(total_inc_base, base)}\n"
            f"💸 Расходы: {format_amount(total_exp_base, base)}\n"
            f"📊 Баланс: {sign}{format_amount(balance_base.copy_abs(), base)}"
        )
        if advice:
            text += f"\n\n🎯 Советы по финансам:\n{advice}"
        text += f"{limits_block}{savings_block}"
        await message.answer(text, parse_mode=None)
    except Exception:
        logger.exception("Failed to build monthly report user_id=%s", user.telegram_id)
        await message.answer("Не получилось собрать месячный отчёт. Попробуй ещё раз через минуту.")


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

    # Allow main menu actions while stash goal state is active.
    if text in ("📌 Лимиты", "/limits"):
        await state.clear()
        await cmd_limits(message, session, state, user)
        return
    if text in ("📊 Отчёт за сегодня", "/report"):
        await state.clear()
        await cmd_report(message, session, user)
        return
    if text in ("📅 Месячный отчёт", "/month"):
        await state.clear()
        await cmd_month(message, session, user)
        return
    if text in ("🎯 Изменить цель", "/goal"):
        await state.clear()
        await message.answer("Режим копилки закрыт. Обнови цель через /goal.")
        return

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
    base = (user.default_currency or "RUB").upper()
    limits_map = await _read_limits_map(session, user.telegram_id)
    lines = [
        "📌 *Лимиты по категориям (в месяц)*",
        "",
        f"Лимиты считаются в основной валюте: *{base}*.",
        "Формат ввода: `Категория 15000` (с новой строки или через запятую).",
        "Пример:",
        "`Еда 30000, Кафе 15000`",
        "",
        "Отправь `/skip`, чтобы выйти из настройки лимитов.",
    ]
    if limits_map:
        lines.extend(["", "Текущие лимиты:"])
        for cat, val in sorted(limits_map.items(), key=lambda x: x[0].lower()):
            val_base = await convert_from_rub(val, base)
            lines.append(f"• {cat} — {format_amount(val_base, base)}")

        today = date.today()
        start = _month_start(today)
        mtd_result = await session.execute(
            select(Transaction).where(
                and_(
                    Transaction.user_id == user.telegram_id,
                    Transaction.type == TransactionType.expense,
                    Transaction.created_at >= start,
                )
            )
        )
        mtd_expenses = mtd_result.scalars().all()
        spent_by_cat: dict[str, Decimal] = {}
        for tx in mtd_expenses:
            spent_by_cat[tx.category] = spent_by_cat.get(tx.category, Decimal("0")) + tx.amount_rub

        lines.extend(["", "Факт / лимит за текущий месяц:"])
        for cat, limit_rub in sorted(limits_map.items(), key=lambda x: x[0].lower()):
            spent_rub = spent_by_cat.get(cat, Decimal("0"))
            spent_base = await convert_from_rub(spent_rub, base)
            limit_base = await convert_from_rub(limit_rub, base)
            pct = (spent_rub / limit_rub * Decimal("100")) if limit_rub > 0 else Decimal("0")
            lines.append(
                f"• {cat}: {format_amount(spent_base, base)} / {format_amount(limit_base, base)} ({pct:,.0f}%)"
            )
    await state.set_state(LimitsStates.waiting_for_limits)
    await message.answer(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_limits_skip_kb(),
    )
    await message.answer(
        "Выбери действие:",
        reply_markup=_limits_actions_kb(bool(limits_map)),
    )


@router.message(LimitsStates.waiting_for_limits)
async def process_limits(message: Message, session: AsyncSession, state: FSMContext, user: User = None) -> None:
    if user is None:
        await state.clear()
        return
    text = message.text.strip()
    if text in ("📊 Отчёт за сегодня", "/report"):
        await state.clear()
        await cmd_report(message, session, user)
        return
    if text in ("📅 Месячный отчёт", "/month"):
        await state.clear()
        await cmd_month(message, session, user)
        return
    if text.lower() == "/skip":
        await state.clear()
        await message.answer("✅ Вышел из режима настройки лимитов.", reply_markup=MAIN_KEYBOARD)
        return
    if text == "🗑 Удалить лимит":
        limits_map = await _read_limits_map(session, user.telegram_id)
        if not limits_map:
            await message.answer("Лимитов пока нет.", reply_markup=_limits_skip_kb())
            return
        await state.set_state(LimitsStates.waiting_for_delete_limit)
        cats = sorted(limits_map.keys(), key=lambda x: x.lower())
        await message.answer(
            "Выбери категорию, лимит которой нужно удалить:",
            reply_markup=_delete_limits_pick_kb(cats),
        )
        return

    parsed = _parse_limit_lines(text)
    if not parsed:
        await message.answer(
            "Не смог разобрать лимиты. Используй формат:\n`Еда 30000, Кафе 15000`\nили по одной строке.",
            parse_mode="Markdown",
        )
        return

    base = (user.default_currency or "RUB").upper()
    existing_result = await session.execute(
        select(UserCategoryLimit).where(UserCategoryLimit.user_id == user.telegram_id)
    )
    existing_limits = {x.category: x for x in existing_result.scalars().all()}
    updated = 0
    created = 0
    for cat, val in parsed:
        val_rub, _ = await convert_to_rub(val, base)
        existing = existing_limits.get(cat)
        if existing:
            existing.monthly_limit_rub = val_rub
            updated += 1
        else:
            session.add(
                UserCategoryLimit(
                    user_id=user.telegram_id,
                    category=cat,
                    monthly_limit_rub=val_rub,
                )
            )
            created += 1
    await session.commit()
    await message.answer(
        f"✅ Лимиты обновлены: {updated}, добавлены: {created}.",
        reply_markup=_limits_skip_kb(),
    )


@router.callback_query(F.data == "limits:clear")
async def cb_limits_clear(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, user: User = None
) -> None:
    if user is None:
        await state.clear()
        await callback.answer()
        return
    await session.execute(
        delete(UserCategoryLimit).where(UserCategoryLimit.user_id == user.telegram_id)
    )
    await session.commit()
    await state.clear()
    await callback.answer("Лимиты удалены")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("✅ Все лимиты удалены.", reply_markup=MAIN_KEYBOARD)


@router.callback_query(F.data == "limits:skip")
async def cb_limits_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("✅ Вышел из режима настройки лимитов.", reply_markup=MAIN_KEYBOARD)


@router.message(LimitsStates.waiting_for_delete_limit)
async def process_delete_limit_pick(
    message: Message, session: AsyncSession, state: FSMContext, user: User = None
) -> None:
    if user is None:
        await state.clear()
        return
    text = message.text.strip()
    if text.lower() == "/skip":
        await state.set_state(LimitsStates.waiting_for_limits)
        await message.answer("Ок, без удаления. Возвращаю в режим лимитов.", reply_markup=_limits_skip_kb())
        return
    result = await session.execute(
        select(UserCategoryLimit).where(
            and_(
                UserCategoryLimit.user_id == user.telegram_id,
                UserCategoryLimit.category == text,
            )
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        await message.answer("Не нашёл такой лимит. Выбери категорию из кнопок или /skip.")
        return
    await session.delete(item)
    await session.commit()
    await state.set_state(LimitsStates.waiting_for_limits)
    await message.answer(f"✅ Лимит для категории `{text}` удалён.", parse_mode="Markdown", reply_markup=_limits_skip_kb())
