from datetime import datetime, timezone
import logging

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User

router = Router()
logger = logging.getLogger(__name__)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Отчёт за сегодня"), KeyboardButton(text="📅 Месячный отчёт")],
        [KeyboardButton(text="🏦 Копилка"), KeyboardButton(text="📌 Лимиты")],
        [KeyboardButton(text="🎯 Изменить цель")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Напиши трату или доход...",
)


class OnboardingStates(StatesGroup):
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

    await state.set_state(OnboardingStates.waiting_for_goal)
    await state.update_data(onboarding_categories_step=True)
    await message.answer(
        "🎉 Добро пожаловать!\n\n"
        "Для персонализированных советов расскажи о своей финансовой цели.\n"
        "Например: *«Накопить на квартиру за 2 года»* или *«Снизить расходы на 20%»*\n\n"
        "Напиши свою цель (или /skip чтобы пропустить):",
        parse_mode="Markdown",
    )


@router.message(OnboardingStates.waiting_for_goal)
async def process_goal(message: Message, session: AsyncSession, state: FSMContext) -> None:
    tg_id = message.from_user.id
    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()

    goal_text = message.text.strip()
    logger.info("Processing goal input user_id=%s skipped=%s", tg_id, goal_text.lower() == "/skip")
    if goal_text.lower() != "/skip" and user:
        user.goal = goal_text
        await session.commit()

    data = await state.get_data()
    do_categories = bool(data.get("onboarding_categories_step"))

    if do_categories:
        await state.set_state(OnboardingStates.waiting_for_custom_categories)
        await message.answer(
            "📂 *Категории*\n\n"
            "Напиши свои категории для трат и доходов — каждую с новой строки или через запятую.\n"
            "Пример:\n"
            "`Еда, Кафе, Транспорт, Подписки, Зарплата`\n\n"
            "Я буду выбирать только из этого списка. Потом всё равно можно поправить категорию "
            "кнопкой *✏️ Изменить* под записью.\n\n"
            "Или нажми *Пропустить*, чтобы категории подбирались автоматически.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        await message.answer(
            "Выбери действие:",
            reply_markup=_onboarding_categories_keyboard(),
        )
    else:
        await state.clear()
        await message.answer(
            "✅ Цель обновлена.",
            reply_markup=MAIN_KEYBOARD,
        )


@router.message(OnboardingStates.waiting_for_custom_categories)
async def process_custom_categories(
    message: Message, session: AsyncSession, state: FSMContext
) -> None:
    tg_id = message.from_user.id
    text = message.text.strip()
    logger.info("Processing custom categories user_id=%s skipped=%s", tg_id, text.lower() == "/skip")

    if text.lower() == "/skip":
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()
        if user:
            user.custom_categories = None
            await session.commit()
        await state.clear()
        await message.answer(
            ONBOARDING_DONE_TEXT + "\n\n_Категории: автоматический подбор._",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    names = _parse_category_lines(text)
    if not names:
        await message.answer(
            "Не вижу ни одной категории. Напиши список через запятую или с новой строки, "
            "или отправь /skip для авто-категорий."
        )
        return

    result = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if user:
        user.custom_categories = names
        await session.commit()

    await state.clear()
    preview = ", ".join(names[:8])
    if len(names) > 8:
        preview += "…"
    await message.answer(
        ONBOARDING_DONE_TEXT + f"\n\n_Твои категории:_ {preview}",
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
        await session.commit()
    await state.clear()
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        ONBOARDING_DONE_TEXT + "\n\n_Категории: автоматический подбор._",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(or_f(Command("goal"), F.text == "🎯 Изменить цель"))
async def cmd_goal(message: Message, session: AsyncSession, state: FSMContext, user: User = None) -> None:
    await state.set_state(OnboardingStates.waiting_for_goal)
    await state.update_data(onboarding_categories_step=False)
    current = user.goal if user else "не задана"
    await message.answer(
        f"🎯 Текущая цель: *{current}*\n\nНапиши новую финансовую цель:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
