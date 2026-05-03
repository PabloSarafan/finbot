import uuid
import logging
import time
import re
from decimal import Decimal
from typing import List, Optional

from aiogram import Router, F
from aiogram.filters import Command, or_f
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from sqlalchemy import select, desc, and_
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


def _currency_from_text(raw: str) -> str:
    s = raw.strip().lower()
    if s in ("", "руб", "руб.", "rur", "rub", "₽"):
        return "RUB"
    if s in ("usd", "$", "доллар", "доллары"):
        return "USD"
    if s in ("eur", "€", "евро"):
        return "EUR"
    if s in ("uzs", "сум", "сумов", "сумы"):
        return "UZS"
    return raw.strip().upper()


def _parse_savings_shortcut(text: str) -> Optional[dict]:
    m = re.match(
        r"^\s*копилка\b[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)\s*([A-Za-zА-Яа-я$€₽]*)\s*$",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    amount = Decimal(m.group(1).replace(",", "."))
    if amount <= 0:
        return None
    currency = _currency_from_text(m.group(2))
    return {
        "amount": amount,
        "currency": currency or "RUB",
        "type": "income",
        "category": SAVINGS_CATEGORY,
        "description": "Копилка",
    }


def _is_currency_tail_token(raw: str) -> bool:
    """True if trailing word after amount is a currency marker (not arbitrary text)."""
    if not raw.strip():
        return True
    s = raw.strip().lower()
    if s in (
        "руб", "руб.", "р.", "rur", "rub", "₽",
        "usd", "$", "доллар", "доллары",
        "eur", "€", "евро",
        "uzs", "сум", "сумов", "сумы",
        "kzt", "₸", "тенге",
        "gbp", "£", "фунт",
        "cny", "¥", "юань",
    ):
        return True
    t = raw.strip()
    return len(t) == 3 and t.isalpha() and t.encode().isascii()


def _resolve_currency_tail(raw: str, default_currency: Optional[str]) -> str:
    base = (default_currency or "RUB").upper()
    if not raw.strip():
        return base
    if not _is_currency_tail_token(raw):
        return base
    code = _currency_from_text(raw).upper()
    if len(code) == 3 and code.encode().isascii() and code.isalpha():
        return code
    return base


def _guess_tx_type_from_description(desc: str) -> str:
    low = desc.lower().strip()
    income_markers = (
        "зарплата",
        "аванс",
        "премия",
        "доход",
        "получил",
        "получила",
        "перевели",
        "перевод",
        "возврат",
        "кэшбэк",
        "кешбэк",
        "фриланс",
        "инвест",
        "дивиденд",
        "проценты",
    )
    for m in income_markers:
        if low.startswith(m) or f" {m}" in low:
            return "income"
    return "expense"


def _guess_category_simple(desc: str, user: User, tx_type: str) -> str:
    pool = _category_pool_for_user(user, tx_type)
    if not pool:
        return "Прочее 📦" if tx_type == "expense" else "Прочее доход 💰"
    d = desc.lower()
    d_tokens = [w for w in re.findall(r"[a-zа-яё0-9]+", d) if len(w) >= 2]
    best: Optional[str] = None
    best_score = 0
    for c in pool:
        cl = c.lower()
        score = 0
        for w in d_tokens:
            if len(w) >= 3 and w in cl:
                score += 3
            elif len(w) >= 2 and w in cl:
                score += 1
        for w in re.findall(r"[a-zа-яё]+", cl):
            if len(w) >= 3 and w in d:
                score += 2
        if score > best_score:
            best_score = score
            best = c
    if best is not None and best_score > 0:
        return best
    for c in pool:
        lowc = c.lower()
        if "прочее" in lowc or "разное" in lowc:
            return c
    return pool[0]


def _parse_simple_money_line(text: str, user: User) -> Optional[dict]:
    """
    Fallback parser for lines like 'Самса 48000 сум' or 'кофе 200 руб' when the LLM returns nothing.
    Expects a numeric amount at the end of the string, optional currency token.
    """
    text = text.strip()
    if not text or len(text) < 3:
        return None
    m = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*([A-Za-zА-Яа-яЁё$€₽]{0,14})?\s*$", text)
    if not m:
        return None
    desc = text[: m.start()].strip()
    if not desc:
        return None
    try:
        amount = Decimal(m.group(1).replace(",", "."))
    except Exception:
        return None
    if amount <= 0:
        return None
    curr_raw = (m.group(2) or "").strip()
    if curr_raw and not _is_currency_tail_token(curr_raw):
        return None
    currency = _resolve_currency_tail(curr_raw, user.default_currency)
    tx_type = _guess_tx_type_from_description(desc)
    category = _guess_category_simple(desc, user, tx_type)
    return {
        "amount": amount,
        "currency": currency,
        "type": tx_type,
        "category": category,
        "description": desc[:500],
    }


class CategoryEditState(StatesGroup):
    waiting_for_custom = State()


class TransactionEditState(StatesGroup):
    waiting_for_value = State()


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


def _tx_preview(text: str, limit: int = 18) -> str:
    value = (text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _tx_pick_kb(items: list[Transaction]) -> InlineKeyboardMarkup:
    rows = []
    for idx, tx in enumerate(items, start=1):
        icon = "💸" if tx.type == TransactionType.expense else "💰"
        rows.append([
            InlineKeyboardButton(
                text=f"✏️ {idx}. {icon} {_tx_preview(tx.description)}",
                callback_data=f"txe:pick:{_compact(tx.id)}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _tx_fields_kb(tx_id: uuid.UUID) -> InlineKeyboardMarkup:
    c = _compact(tx_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Тип", callback_data=f"txe:field:type:{c}"),
                InlineKeyboardButton(text="Сумма", callback_data=f"txe:field:amount:{c}"),
            ],
            [
                InlineKeyboardButton(text="Валюта", callback_data=f"txe:field:currency:{c}"),
                InlineKeyboardButton(text="Категория", callback_data=f"txe:field:category:{c}"),
            ],
            [InlineKeyboardButton(text="Описание", callback_data=f"txe:field:description:{c}")],
            [InlineKeyboardButton(text="🗑 Удалить запись", callback_data=f"txe:delete:{c}")],
        ]
    )


def _tx_type_kb(tx_id: uuid.UUID) -> InlineKeyboardMarkup:
    c = _compact(tx_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💸 Расход", callback_data=f"txe:type:expense:{c}"),
                InlineKeyboardButton(text="💰 Доход", callback_data=f"txe:type:income:{c}"),
            ],
            [InlineKeyboardButton(text="↩️ Назад к полям", callback_data=f"txe:pick:{c}")],
        ]
    )


def _normalize_currency(text: str) -> str:
    code = _currency_from_text(text)
    if len(code) != 3 or not code.isascii() or not code.isalpha():
        raise ValueError("Нужен 3-буквенный код валюты, например RUB или USD.")
    return code


async def _recompute_rub_fields(tx: Transaction) -> None:
    amount_rub, exchange_rate = await convert_to_rub(tx.amount_original, tx.currency_original)
    tx.amount_rub = amount_rub
    tx.exchange_rate = exchange_rate


async def _tx_edit_text(tx: Transaction, user: Optional[User]) -> str:
    created = tx.created_at.strftime("%d.%m %H:%M") if tx.created_at else "—"
    icon = "💸" if tx.type == TransactionType.expense else "💰"
    base_currency = ((user.default_currency if user else "RUB") or "RUB").upper()
    original = format_amount(tx.amount_original, tx.currency_original)
    if base_currency == tx.currency_original:
        amount_line = original
    else:
        base_amount = await convert_from_rub(tx.amount_rub, base_currency)
        amount_line = f"{original} (~{format_amount(base_amount, base_currency)})"
    return (
        f"✏️ Редактирование записи\n"
        f"Дата: {created}\n"
        f"Тип: {icon} `{tx.type.value}`\n"
        f"Категория: *{tx.category}*\n"
        f"Описание: {tx.description}\n"
        f"Сумма: {amount_line}"
    )


@router.message(or_f(Command("last10"), F.text == "📝 Последние 10"))
async def cmd_last10(message: Message, session: AsyncSession, user: User = None) -> None:
    if user is None:
        return
    result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user.telegram_id)
        .order_by(desc(Transaction.created_at))
        .limit(10)
    )
    items = result.scalars().all()
    if not items:
        await message.answer("Пока нет сохранённых операций.")
        return

    lines = ["🧾 Последние 10 операций:"]
    for idx, tx in enumerate(items, start=1):
        icon = "💸" if tx.type == TransactionType.expense else "💰"
        created = tx.created_at.strftime("%d.%m %H:%M") if tx.created_at else "—"
        lines.append(
            f"{idx}. {icon} {created} — {format_amount(tx.amount_original, tx.currency_original)} | "
            f"{tx.category} | {_tx_preview(tx.description, 28)}"
        )
    await message.answer(
        "\n".join(lines) + "\n\nВыбери запись для редактирования:",
        reply_markup=_tx_pick_kb(items),
    )


@router.callback_query(F.data.startswith("txe:pick:"))
async def cb_pick_tx(callback: CallbackQuery, session: AsyncSession) -> None:
    cid = callback.data[9:]
    tx_id = _expand(cid)
    result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = result.scalar_one_or_none()
    if tx is None or tx.user_id != callback.from_user.id:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    user_result = await session.execute(select(User).where(User.telegram_id == tx.user_id))
    user = user_result.scalar_one_or_none()
    await callback.message.answer(
        await _tx_edit_text(tx, user),
        parse_mode="Markdown",
        reply_markup=_tx_fields_kb(tx.id),
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^txe:field:(type|amount|currency|category|description):"))
async def cb_pick_field(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    _, _, field, cid = callback.data.split(":", 3)
    tx_id = _expand(cid)
    result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = result.scalar_one_or_none()
    if tx is None or tx.user_id != callback.from_user.id:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    if field == "type":
        await callback.message.answer("Выбери новый тип операции:", reply_markup=_tx_type_kb(tx_id))
        await callback.answer()
        return

    await state.set_state(TransactionEditState.waiting_for_value)
    await state.update_data(edit_tx_id=cid, edit_field=field)
    prompts = {
        "amount": "Введи новую сумму (например: `2500` или `2500.50`).",
        "currency": "Введи новую валюту (3 буквы, например: `RUB`, `USD`, `EUR`).",
        "category": "Введи новую категорию текстом.",
        "description": "Введи новое описание операции.",
    }
    await callback.message.answer(prompts[field], parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data.startswith("txe:type:"))
async def cb_set_type(callback: CallbackQuery, session: AsyncSession) -> None:
    _, _, value, cid = callback.data.split(":", 3)
    if value not in ("income", "expense"):
        await callback.answer("Неверный тип.", show_alert=True)
        return
    tx_id = _expand(cid)
    result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = result.scalar_one_or_none()
    if tx is None or tx.user_id != callback.from_user.id:
        await callback.answer("Запись не найдена.", show_alert=True)
        return
    tx.type = TransactionType(value)
    await session.commit()

    user_result = await session.execute(select(User).where(User.telegram_id == tx.user_id))
    user = user_result.scalar_one_or_none()
    await callback.message.answer(
        "✅ Тип обновлён.\n\n" + await _tx_edit_text(tx, user),
        parse_mode="Markdown",
        reply_markup=_tx_fields_kb(tx.id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("txe:delete:"))
async def cb_delete_tx(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    cid = callback.data[11:]
    tx_id = _expand(cid)
    result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = result.scalar_one_or_none()
    if tx is None or tx.user_id != callback.from_user.id:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    await session.delete(tx)
    await session.commit()
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ Запись удалена.")
    await callback.answer()


@router.message(TransactionEditState.waiting_for_value)
async def process_edit_value(
    message: Message, session: AsyncSession, state: FSMContext, user: User = None
) -> None:
    if user is None:
        await state.clear()
        return
    data = await state.get_data()
    cid = data.get("edit_tx_id")
    field = data.get("edit_field")
    if not cid or field not in ("amount", "currency", "category", "description"):
        await state.clear()
        await message.answer("Сессия редактирования устарела. Вызови /last10 снова.")
        return

    tx_id = _expand(cid)
    result = await session.execute(
        select(Transaction).where(
            and_(Transaction.id == tx_id, Transaction.user_id == user.telegram_id)
        )
    )
    tx = result.scalar_one_or_none()
    if tx is None:
        await state.clear()
        await message.answer("Запись не найдена. Вызови /last10 снова.")
        return

    value = message.text.strip()
    try:
        if field == "amount":
            amount = Decimal(value.replace(",", "."))
            if amount <= 0:
                raise ValueError("Сумма должна быть больше 0.")
            tx.amount_original = amount
            await _recompute_rub_fields(tx)
        elif field == "currency":
            tx.currency_original = _normalize_currency(value)
            await _recompute_rub_fields(tx)
        elif field == "category":
            if not value:
                raise ValueError("Категория не может быть пустой.")
            tx.category = value
        elif field == "description":
            if not value:
                raise ValueError("Описание не может быть пустым.")
            tx.description = value
        await session.commit()
    except Exception as e:
        await message.answer(f"Не удалось обновить поле: {e}")
        return

    await state.clear()
    await message.answer(
        "✅ Запись обновлена.\n\n" + await _tx_edit_text(tx, user),
        parse_mode="Markdown",
        reply_markup=_tx_fields_kb(tx.id),
    )


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
        parsed = _parse_savings_shortcut(item)
        if parsed is None:
            parsed = await parse_transaction(
                item,
                custom_for_llm,
                default_currency=user.default_currency,
            )
        if parsed is None:
            parsed = _parse_simple_money_line(item, user)
            if parsed is not None:
                logger.info(
                    "Transaction parsed via simple money line user_id=%s item='%s'",
                    user.telegram_id,
                    item,
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
            "Transaction saved user_id=%s tx_id=%s type=%s category=%s description=%s amount_rub=%s db_commit_ms=%s",
            user.telegram_id,
            tx_id,
            tx_type,
            category,
            description,
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

        reply_markup = None if custom_cat else _confirm_kb(tx_id)
        await message.answer(
            f"{icon} *{category}*\n"
            f"{description} — {orig_str}{conversion_note}\n"
            f"✅ Сохранено",
            parse_mode="Markdown",
            reply_markup=reply_markup,
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
async def cb_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    cid = callback.data[7:]
    tx_id = _expand(cid)
    result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
    tx = result.scalar_one_or_none()
    if tx:
        keyword = tx.description.lower().strip()
        if keyword:
            existing_result = await session.execute(
                select(UserCategoryMapping).where(
                    UserCategoryMapping.user_id == tx.user_id,
                    UserCategoryMapping.keyword == keyword,
                )
            )
            existing = existing_result.scalar_one_or_none()
            if existing:
                existing.category = tx.category
            else:
                session.add(
                    UserCategoryMapping(
                        user_id=tx.user_id,
                        keyword=keyword,
                        category=tx.category,
                    )
                )
            await session.commit()
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
