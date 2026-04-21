import uuid
import logging
import time
from decimal import Decimal
from typing import List, Optional

from aiogram import Router, F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Transaction, TransactionType, User, UserCategoryMapping
from bot.services.llm import parse_transaction
from bot.services.currency import convert_to_rub, convert_from_rub, format_amount
from bot.handlers.start import MAIN_KEYBOARD, OnboardingStates

router = Router()
logger = logging.getLogger(__name__)

CATEGORIES_EXPENSE = [
    "Еда 🛒", "Кафе ☕", "Транспорт 🚗", "ЖКХ 🏠",
    "Здоровье 💊", "Развлечения 🎬", "Одежда 👕",
    "Техника 💻", "Образование 📚", "Путешествия ✈️", "Прочее 📦",
]
CATEGORIES_INCOME = ["Зарплата 💼", "Фриланс 💻", "Инвестиции 📈", "Прочее доход 💰"]
SAVINGS_CATEGORY = "Копилка 🏦"


def _category_pool_for_user(user: Optional[User], tx_type: str) -> List[str]:
    if user and user.custom_categories:
        pool = [str(x).strip() for x in user.custom_categories if str(x).strip()]
        if pool:
            if tx_type == "income" and SAVINGS_CATEGORY not in pool:
                return pool + [SAVINGS_CATEGORY]
            return pool
    return CATEGORIES_EXPENSE if tx_type == "expense" else (CATEGORIES_INCOME + [SAVINGS_CATEGORY])


class CategoryEditState(StatesGroup):
    waiting_for_custom = State()


def _compact(tx_id: uuid.UUID) -> str:
    return tx_id.hex  # 32 chars


def _expand(s: str) -> uuid.UUID:
    return uuid.UUID(s)


def _confirm_kb(tx_id: uuid.UUID) -> InlineKeyboardMarkup:
    c = _compact(tx_id)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Верно", callback_data=f"cat:ok:{c}"),
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"cat:ch:{c}"),
    ]])


def _categories_kb(tx_id: uuid.UUID, pool: List[str]) -> InlineKeyboardMarkup:
    c = _compact(tx_id)
    rows = []
    for i in range(0, len(pool), 2):
        row = []
        for j in range(i, min(i + 2, len(pool))):
            label = pool[j]
            if len(label) > 40:
                label = label[:37] + "..."
            row.append(InlineKeyboardButton(text=label, callback_data=f"cat:{j}:{c}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="➕ Своя категория", callback_data=f"cat:cu:{c}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"cat:ok:{c}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _save_rule_kb(tx_id: uuid.UUID) -> InlineKeyboardMarkup:
    c = _compact(tx_id)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, запомнить", callback_data=f"cat:sy:{c}"),
        InlineKeyboardButton(text="Нет", callback_data=f"cat:sn:{c}"),
    ]])


async def _custom_category(session: AsyncSession, user_id: int, description: str) -> Optional[str]:
    result = await session.execute(
        select(UserCategoryMapping).where(UserCategoryMapping.user_id == user_id)
    )
    mappings = result.scalars().all()
    desc = description.lower()
    for m in mappings:
        if m.keyword in desc or desc in m.keyword:
            return m.category
    return None


@router.message(CategoryEditState.waiting_for_custom)
async def process_custom_category(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    data = await state.get_data()
    cid = data.get("tx_id")
    keyword = data.get("keyword", "")
    new_category = message.text.strip()

    await state.clear()

    if cid:
        tx_id = _expand(cid)
        result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
        tx = result.scalar_one_or_none()
        if tx:
            tx.category = new_category

        result = await session.execute(
            select(UserCategoryMapping).where(
                UserCategoryMapping.user_id == message.from_user.id,
                UserCategoryMapping.keyword == keyword,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.category = new_category
        else:
            session.add(UserCategoryMapping(
                user_id=message.from_user.id,
                keyword=keyword,
                category=new_category,
            ))
        await session.commit()

    await message.answer(
        f"✅ Категория *{new_category}* сохранена\n"
        f"Правило: «{keyword}» → *{new_category}*",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(
    F.text & ~F.text.startswith("/"),
    StateFilter(None),
)
async def handle_transaction(
    message: Message, session: AsyncSession, state: FSMContext, user: User = None
) -> None:
    if user is None:
        logger.warning("Ignoring transaction message from unauthorized user_id=%s", message.from_user.id)
        return

    text = message.text.strip()
    logger.info("Handling transaction message user_id=%s text_len=%s", user.telegram_id, len(text))
    await message.bot.send_chat_action(message.chat.id, "typing")

    # Support multi-expense input: split by newlines and semicolons.
    raw_items = [
        part.strip()
        for line in text.replace("\r", "\n").split("\n")
        for part in line.split(";")
        if part.strip()
    ]
    items = raw_items or [text]

    custom_for_llm: Optional[List[str]] = None
    if user.custom_categories:
        custom_for_llm = [
            str(x).strip() for x in user.custom_categories if str(x).strip()
        ]
        if SAVINGS_CATEGORY not in custom_for_llm:
            custom_for_llm.append(SAVINGS_CATEGORY)
        if not custom_for_llm:
            custom_for_llm = None

    saved = 0
    failed: list[str] = []

    for item in items:
        t0 = time.perf_counter()
        parsed = await parse_transaction(
            item,
            custom_for_llm,
            default_currency=user.default_currency,
        )
        llm_ms = int((time.perf_counter() - t0) * 1000)
        logger.info("LLM parse duration user_id=%s ms=%s item='%s'", user.telegram_id, llm_ms, item)
        if parsed is None:
            logger.info("Transaction parsing failed user_id=%s item='%s'", user.telegram_id, item)
            failed.append(item)
            continue

        amount_orig = parsed["amount"]
        currency = parsed["currency"].upper()
        tx_type = parsed["type"]
        category = parsed["category"]
        description = parsed["description"]

        # Apply custom mapping if exists
        custom_cat = await _custom_category(session, user.telegram_id, description)
        if custom_cat:
            category = custom_cat

        try:
            t1 = time.perf_counter()
            amount_rub, exchange_rate = await convert_to_rub(amount_orig, currency)
            fx_ms = int((time.perf_counter() - t1) * 1000)
            logger.info("FX convert duration user_id=%s ms=%s currency=%s", user.telegram_id, fx_ms, currency)
        except Exception:
            logger.exception(
                "Currency conversion failed user_id=%s currency=%s amount=%s",
                user.telegram_id,
                currency,
                amount_orig,
            )
            failed.append(item)
            continue

        tx_id = uuid.uuid4()
        tx = Transaction(
            id=tx_id,
            user_id=user.telegram_id,
            type=TransactionType(tx_type),
            amount_original=amount_orig,
            currency_original=currency,
            amount_rub=amount_rub,
            exchange_rate=exchange_rate,
            category=category,
            description=description,
        )
        session.add(tx)
        t2 = time.perf_counter()
        await session.commit()
        db_ms = int((time.perf_counter() - t2) * 1000)
        logger.info(
            "Transaction saved user_id=%s tx_id=%s type=%s category=%s amount_rub=%s db_commit_ms=%s",
            user.telegram_id,
            tx_id,
            tx_type,
            category,
            amount_rub,
            db_ms,
        )

        icon = "💸" if tx_type == "expense" else "💰"
        base_currency = (user.default_currency or "RUB").upper()
        base_amount = await convert_from_rub(amount_rub, base_currency)
        orig_str = format_amount(amount_orig, currency)
        conversion_note = (
            f" (~{format_amount(base_amount, base_currency)})"
            if currency != base_currency
            else ""
        )

        await message.answer(
            f"{icon} *{category}*\n"
            f"{description} — {orig_str}{conversion_note}\n"
            f"✅ Сохранено",
            parse_mode="Markdown",
            reply_markup=_confirm_kb(tx_id),
        )
        saved += 1

    if saved == 0:
        await message.answer(
            "🤔 Не смог разобрать трату. Попробуй в формате:\n"
            "• `кофе 200 руб`\n"
            "• `зарплата 150000`\n"
            "• `такси 50000 сум`",
            parse_mode="Markdown",
        )
    elif failed:
        failed_list = "\n".join(f"• {item}" for item in failed)
        await message.answer(
            "Следующие строки не удалось разобрать, я их пропустил:\n"
            f"{failed_list}",
            parse_mode="Markdown",
        )


# ── Callback: confirm (close keyboard) ───────────────────────────────────────

@router.callback_query(F.data.startswith("cat:ok:"))
async def cb_confirm(callback: CallbackQuery) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


# ── Callback: show category list ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("cat:ch:"))
async def cb_change(callback: CallbackQuery, session: AsyncSession) -> None:
    cid = callback.data[7:]
    tx_id = _expand(cid)

    result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = result.scalar_one_or_none()
    if tx is None:
        await callback.answer("Транзакция не найдена.")
        return

    result_u = await session.execute(select(User).where(User.telegram_id == tx.user_id))
    u = result_u.scalar_one_or_none()
    pool = _category_pool_for_user(u, tx.type.value)

    await callback.message.edit_reply_markup(
        reply_markup=_categories_kb(tx_id, pool)
    )
    await callback.answer()


# ── Callback: select standard category ───────────────────────────────────────

@router.callback_query(F.data.regexp(r"^cat:\d+:"))
async def cb_set_category(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    parts = callback.data.split(":", 2)
    idx = int(parts[1])
    cid = parts[2]
    tx_id = _expand(cid)

    result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = result.scalar_one_or_none()
    if tx is None:
        await callback.answer("Транзакция не найдена.")
        return

    result_u = await session.execute(select(User).where(User.telegram_id == tx.user_id))
    u = result_u.scalar_one_or_none()
    pool = _category_pool_for_user(u, tx.type.value)
    if idx < 0 or idx >= len(pool):
        await callback.answer("Неверная категория.")
        return
    new_category = pool[idx]

    old_category = tx.category
    tx.category = new_category
    await session.commit()

    keyword = tx.description.lower()
    await state.update_data(tx_id=cid, keyword=keyword, category=new_category)

    await callback.message.edit_text(
        f"✅ Категория изменена: *{old_category}* → *{new_category}*\n\n"
        f"Запомнить правило?\n«{keyword}» → *{new_category}*",
        parse_mode="Markdown",
        reply_markup=_save_rule_kb(tx_id),
    )
    await callback.answer()


# ── Callback: custom category input ──────────────────────────────────────────

@router.callback_query(F.data.startswith("cat:cu:"))
async def cb_custom_start(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    cid = callback.data[7:]
    tx_id = _expand(cid)

    result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = result.scalar_one_or_none()
    if tx is None:
        await callback.answer("Транзакция не найдена.")
        return

    await state.set_state(CategoryEditState.waiting_for_custom)
    await state.update_data(tx_id=cid, keyword=tx.description.lower())

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
    await callback.message.answer("Напиши название своей категории:")


# ── Callback: save rule yes/no ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("cat:sy:"))
async def cb_save_yes(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    data = await state.get_data()
    keyword = data.get("keyword", "")
    category = data.get("category", "")
    await state.clear()

    if keyword and category:
        result = await session.execute(
            select(UserCategoryMapping).where(
                UserCategoryMapping.user_id == callback.from_user.id,
                UserCategoryMapping.keyword == keyword,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.category = category
        else:
            session.add(UserCategoryMapping(
                user_id=callback.from_user.id,
                keyword=keyword,
                category=category,
            ))
        await session.commit()

    await callback.message.edit_text(
        callback.message.text + "\n\n✅ Правило сохранено!",
        parse_mode="Markdown",
        reply_markup=None,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cat:sn:"))
async def cb_save_no(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()
