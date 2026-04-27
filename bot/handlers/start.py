from datetime import datetime, timezone
import logging
from decimal import Decimal
import re

from aiogram import Router, F
from aiogram.filters import CommandStart, Command, or_f, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User, UserCategoryLimit
from bot.services.currency import convert_to_rub

router = Router()
logger = logging.getLogger(__name__)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Отчёт за сегодня"), KeyboardButton(text="📅 Месячный отчёт")],
        [KeyboardButton(text="🏦 Копилка"), KeyboardButton(text="📌 Лимиты")],
        [KeyboardButton(text="📝 Последние 10"), KeyboardButton(text="🎯 Изменить цель")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Напиши трату или доход...",
)


class OnboardingStates(StatesGroup):
    waiting_for_currency = State()
    waiting_for_custom_currency = State()
    waiting_for_goal = State()
    waiting_for_custom_categories = State()


ONBOARDING_DONE_TEXT = (
    "✅ Отлично! Можем начинать.\n\n"
    "Просто пиши свои траты и доходы в свободной форме:\n"
    "• `кофе 200 руб` — расход\n"
    "• `зарплата 150000` — доход\n"
    "• `такси 50000 сум` — расход в узбекских сумах\n\n"
    "Несколько трат в одном сообщении — с новой строки или через `;`.\n\n"
    "После каждой записи можно подтвердить категорию или изменить её ✏️"
)


def _parse_category_lines(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in text.replace("\r", "\n").split("\n"):
        for part in line.split(","):
            name = part.strip()
            if not name or name.lower() == "/skip":
                continue
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out[:40]


def _onboarding_categories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Пропустить — авто-категории",
                    callback_data="onb:cat_skip",
                )
            ]
        ]
    )


def _onboarding_currency_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Рубль (RUB)", callback_data="onb:cur:RUB"),
                InlineKeyboardButton(text="Доллар (USD)", callback_data="onb:cur:USD"),
            ],
            [InlineKeyboardButton(text="Своя валюта", callback_data="onb:cur:CUSTOM")],
        ]
    )


def _onboarding_goal_skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Пропустить цель", callback_data="onb:goal_skip")]
        ]
    )


def _parse_categories_and_limits(text: str) -> tuple[list[str], dict[str, Decimal]]:
    names: list[str] = []
    limits: dict[str, Decimal] = {}
    seen: set[str] = set()
    parts = text.replace("\r", "\n").replace(";", "\n").replace(",", "\n").split("\n")
    for raw in parts:
        line = raw.strip()
        if not line or line.lower() == "/skip":
            continue
        m = re.match(r"^\s*(.+?)\s*[:\-]?\s*([0-9]+(?:[.,][0-9]+)?)\s*$", line)
        if m:
            category = m.group(1).strip()
            value = Decimal(m.group(2).replace(",", "."))
            if category and value >= 0:
                limits[category] = value
                if category not in seen:
                    seen.add(category)
                    names.append(category)
            continue
        if line not in seen:
            seen.add(line)
            names.append(line)
    return names[:40], limits


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    tg_id = message.from_user.id
    logger.info("Received /start from user_id=%s", tg_id)

    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    if user and user.is_active:
        logger.info("User already active user_id=%s", tg_id)
        await message.answer(
            "✅ Ты уже зарегистрирован!\n\n"
            "Просто напиши трату или доход, например:\n"
            "• `кофе 200 руб`\n"
            "• `зарплата 150000`\n"
            "• `такси 5000 сум`",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if user is None:
        logger.info("Registering new user user_id=%s", tg_id)
        user = User(
            telegram_id=tg_id,
            username=message.from_user.username,
            full_name=message.from_user.full_name,
            is_active=True,
            activated_at=datetime.now(timezone.utc),
        )
        session.add(user)
    else:
        logger.info("Reactivating existing user user_id=%s", tg_id)
        user.is_active = True
        user.activated_at = datetime.now(timezone.utc)

    await session.commit()

    await message.answer(
        "🎉 Добро пожаловать!\n\n"
        "Я *Бобби* — твой помощник по личным финансам.\n"
        "Помогу вести учёт расходов и доходов и достигать финансовых целей.\n\n"
        "Я умею вести и категоризировать твои расходы и доходы.",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(OnboardingStates.waiting_for_currency)
    await message.answer(
        "💱 Поддерживаю разные валюты и по умолчанию буду приводить все суммы к основной валюте.\n"
        "Выбери основную валюту:",
        reply_markup=_onboarding_currency_keyboard(),
    )


async def _ask_goal_text(message: Message, state: FSMContext, with_categories_step: bool) -> None:
    await state.set_state(OnboardingStates.waiting_for_goal)
    await state.update_data(onboarding_categories_step=with_categories_step)
    await message.answer(
        "🎯 Для более персонализированного подхода опиши свою финансовую цель.\n"
        "Например: *«Хочу откладывать 25% зарплаты, формировать пассивный доход, к концу года "
        "накопить 3 млн рублей, дальше — на первый взнос на ипотеку»*.\n\n"
        "Чем точнее цель, тем полезнее рекомендации.\n\n"
        "Напиши цель или нажми `Пропустить цель`.",
        parse_mode="Markdown",
        reply_markup=_onboarding_goal_skip_keyboard(),
    )


@router.callback_query(
    F.data.regexp(r"^onb:cur:"),
    StateFilter(OnboardingStates.waiting_for_currency),
)
async def onboarding_pick_currency(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    tg_id = callback.from_user.id
    value = callback.data.split(":")[-1]
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        await callback.answer("Пользователь не найден")
        return
    if value == "CUSTOM":
        await state.set_state(OnboardingStates.waiting_for_custom_currency)
        await callback.answer()
        await callback.message.answer(
            "Введи код основной валюты (например: `EUR`, `KZT`, `UZS`).",
            parse_mode="Markdown",
        )
        return
    user.default_currency = value
    await session.commit()
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(f"✅ Основная валюта: *{value}*", parse_mode="Markdown")
    await _ask_goal_text(callback.message, state, with_categories_step=True)


@router.message(OnboardingStates.waiting_for_custom_currency)
async def onboarding_custom_currency(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    code = message.text.strip().upper()
    if not code.isalpha() or len(code) != 3:
        await message.answer("Нужен 3-буквенный код валюты, например `EUR`.", parse_mode="Markdown")
        return
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user:
        user.default_currency = code
        await session.commit()
    await message.answer(f"✅ Основная валюта: *{code}*", parse_mode="Markdown")
    await _ask_goal_text(message, state, with_categories_step=True)


@router.message(OnboardingStates.waiting_for_goal)
async def process_goal(message: Message, session: AsyncSession, state: FSMContext) -> None:
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    goal_text = message.text.strip()
    logger.info("Processing goal input user_id=%s skipped=%s", tg_id, goal_text.lower() == "/skip")
    if user:
        user.goal = None if goal_text.lower() == "/skip" else goal_text
        await session.commit()

    data = await state.get_data()
    do_categories = bool(data.get("onboarding_categories_step"))

    if do_categories:
        await message.answer("✅ Отлично, цель зафиксирована. Давай перейдём к категориям.")
        await _go_to_categories_step(message, state)
    else:
        await state.clear()
        await message.answer(
            ONBOARDING_DONE_TEXT,
            reply_markup=MAIN_KEYBOARD,
        )


async def _go_to_categories_step(message: Message, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.waiting_for_custom_categories)
    await message.answer(
        "📂📌 *Категории и лимиты*\n\n"
        "На этом шаге можно сразу задать категории и лимиты по ним.\n"
        "Форматы:\n"
        "• только категория: `Еда`\n"
        "• категория + лимит: `Еда 30000`\n"
        "Можно отправлять с новой строки или через запятую.\n\n"
        "Пример:\n"
        "`Еда 30000, Кафе 15000, Транспорт, Подписки, Зарплата`\n\n"
        "Я буду выбирать только из этого списка. Потом всё равно можно поправить категорию "
        "кнопкой *✏️ Изменить* под записью.\n\n"
        "Если не хочешь настраивать сейчас — нажми *Пропустить*.\n"
        "Категории и лимиты можно задать позже через `/limits` и в редактировании категорий.",
        parse_mode="Markdown",
        reply_markup=_onboarding_categories_keyboard(),
    )


@router.callback_query(
    F.data == "onb:goal_skip",
    StateFilter(OnboardingStates.waiting_for_goal),
)
async def onboarding_skip_goal(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    tg_id = callback.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user:
        user.goal = None
        await session.commit()
    data = await state.get_data()
    do_categories = bool(data.get("onboarding_categories_step"))
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if do_categories:
        await callback.message.answer(
            "✅ Продолжаем без цели.\n"
            "Рекомендации по цели отключены, но аналитику и отчёты ты получишь."
        )
        await _go_to_categories_step(callback.message, state)
    else:
        await state.clear()
        await callback.message.answer(
            "✅ Продолжаем без цели.\n"
            "Буду показывать аналитику по доходам и расходам без персональных рекомендаций.",
            reply_markup=MAIN_KEYBOARD,
        )


@router.message(OnboardingStates.waiting_for_custom_categories)
async def process_custom_categories(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    tg_id = message.from_user.id
    text = message.text.strip()
    logger.info("Processing custom categories user_id=%s skipped=%s", tg_id, text.lower() == "/skip")
    if text in ("📊 Отчёт за сегодня", "/report", "📅 Месячный отчёт", "/month"):
        await state.clear()
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if user:
            from bot.handlers.reports import cmd_report, cmd_month
            if text in ("📊 Отчёт за сегодня", "/report"):
                await cmd_report(message, session, user)
            else:
                await cmd_month(message, session, user)
        return

    if text.lower() == "/skip":
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if user:
            user.custom_categories = None
            await session.execute(
                delete(UserCategoryLimit).where(UserCategoryLimit.user_id == user.telegram_id)
            )
            await session.commit()
        await state.clear()
        await message.answer(
            "✅ Пропустили настройку категорий и лимитов.\n\n" + ONBOARDING_DONE_TEXT,
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    names, limits_map = _parse_categories_and_limits(text)
    if not names:
        await message.answer(
            "Не вижу ни одной категории. Напиши список через запятую/с новой строки "
            "или отправь /skip."
        )
        return

    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user:
        user.custom_categories = names
        await session.execute(
            delete(UserCategoryLimit).where(UserCategoryLimit.user_id == user.telegram_id)
        )
        for category, value in limits_map.items():
            val_rub, _ = await convert_to_rub(value, (user.default_currency or "RUB").upper())
            session.add(
                UserCategoryLimit(
                    user_id=user.telegram_id,
                    category=category,
                    monthly_limit_rub=val_rub,
                )
            )
        await session.commit()

    await state.clear()
    preview = ", ".join(names[:8])
    if len(names) > 8:
        preview += "…"
    limits_note = f"\n\n✅ Лимиты сохранены: {len(limits_map)}." if limits_map else ""
    await message.answer(
        f"✅ Категории сохранены: {preview}{limits_note}\n\n{ONBOARDING_DONE_TEXT}",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


@router.callback_query(
    F.data == "onb:cat_skip",
    StateFilter(OnboardingStates.waiting_for_custom_categories),
)
async def onboarding_skip_categories(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext
) -> None:
    tg_id = callback.from_user.id
    logger.info("Onboarding skip custom categories user_id=%s", tg_id)
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user:
        user.custom_categories = None
        await session.execute(
            delete(UserCategoryLimit).where(UserCategoryLimit.user_id == user.telegram_id)
        )
        await session.commit()
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "✅ Пропустили настройку категорий и лимитов.\n\n" + ONBOARDING_DONE_TEXT,
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(or_f(Command("goal"), F.text == "🎯 Изменить цель"))
async def cmd_goal(message: Message, session: AsyncSession, state: FSMContext, user: User = None) -> None:
    await state.set_state(OnboardingStates.waiting_for_goal)
    await state.update_data(onboarding_categories_step=False)
    current = user.goal if user else "не задана"
    await message.answer(
        f"🎯 Текущая цель: *{current}*\n\n"
        "Напиши новую финансовую цель или нажми `Пропустить цель`.",
        parse_mode="Markdown",
        reply_markup=_onboarding_goal_skip_keyboard(),
    )
