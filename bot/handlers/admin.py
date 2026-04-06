import secrets
import string

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import InviteCode, Transaction, User

router = Router()

ALPHABET = string.ascii_uppercase + string.digits


def _is_admin(telegram_id: int) -> bool:
    return telegram_id in settings.admin_ids


def _generate_code(length: int = 10) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


@router.message(Command("create_invite"))
async def cmd_create_invite(message: Message, session: AsyncSession) -> None:
    if not _is_admin(message.from_user.id):
        return  # Silently ignore non-admins

    code = _generate_code()
    invite = InviteCode(
        code=code,
        created_by_admin_id=message.from_user.id,
    )
    session.add(invite)
    await session.commit()

    await message.answer(
        f"✅ Инвайт-код создан:\n\n`{code}`\n\n"
        f"Ссылка для активации:\n"
        f"`/start {code}`",
        parse_mode="Markdown",
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    if not _is_admin(message.from_user.id):
        return

    total_users = await session.scalar(select(func.count()).select_from(User))
    active_users = await session.scalar(
        select(func.count()).select_from(User).where(User.is_active == True)
    )
    total_txs = await session.scalar(select(func.count()).select_from(Transaction))

    await message.answer(
        f"📊 *Статистика бота*\n\n"
        f"👤 Всего пользователей: {total_users}\n"
        f"✅ Активных: {active_users}\n"
        f"💳 Транзакций: {total_txs}",
        parse_mode="Markdown",
    )
