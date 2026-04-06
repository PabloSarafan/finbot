from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User


class AuthMiddleware(BaseMiddleware):
    """Injects the User object into handler data if the user exists in DB."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        session: AsyncSession = data.get("session")
        if session is None:
            return await handler(event, data)

        tg_id = event.from_user.id
        result = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = result.scalar_one_or_none()

        if user and user.is_active:
            data["user"] = user

        return await handler(event, data)
