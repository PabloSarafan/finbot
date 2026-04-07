from datetime import datetime, timezone
import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, Command, or_f
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User

router = Router()
logger = logging.getLogger(__name__)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Отчёт за сегодня"), KeyboardButton(text="📅 Месячный отчёт")],
        [KeyboardButton(text="🎯 Изменить цель"), KeyboardButton(text="📋 Последние 5")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Напиши трату или доход...",
)


class OnboardingStates(StatesGroup):
    waiting_for_goal = State()


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

    await state.clear()
    await message.answer(
        "✅ Отлично! Можем начинать.\n\n"
        "Просто пиши свои траты и доходы в свободной форме:\n"
        "• `кофе 200 руб` — расход\n"
        "• `зарплата 150000` — доход\n"
        "• `такси 50000 сум` — расход в узбекских сумах\n\n"
        "Я сам определю категорию и конвертирую в рубли 🤖",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(or_f(Command("goal"), F.text == "🎯 Изменить цель"))
async def cmd_goal(message: Message, session: AsyncSession, state: FSMContext, user: User = None) -> None:
    await state.set_state(OnboardingStates.waiting_for_goal)
    current = user.goal if user else "не задана"
    await message.answer(
        f"🎯 Текущая цель: *{current}*\n\nНапиши новую финансовую цель:",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
