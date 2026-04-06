import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from config import settings
from db.session import engine, AsyncSessionFactory
from db.models import Base
from bot.handlers import start, admin, transactions, reports
from bot.middlewares.auth import AuthMiddleware
from bot.services.scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    logger.info("Bot starting, running migrations...")
    from alembic.config import Config
    from alembic import command
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    logger.info("Migrations done")

    await bot.set_my_commands([
        BotCommand(command="start", description="Начать / перезапустить"),
        BotCommand(command="report", description="Отчёт за сегодня"),
        BotCommand(command="month", description="Месячный отчёт"),
        BotCommand(command="last", description="Последние 5 транзакций"),
        BotCommand(command="goal", description="Изменить финансовую цель"),
        BotCommand(command="stats", description="Статистика (админ)"),
        BotCommand(command="create_invite", description="Создать инвайт (админ)"),
    ])


async def main() -> None:
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Session middleware — inject DB session into every handler
    from aiogram import BaseMiddleware
    from typing import Any, Awaitable, Callable
    from aiogram.types import TelegramObject

    class SessionMiddleware(BaseMiddleware):
        async def __call__(
            self,
            handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
            event: TelegramObject,
            data: dict[str, Any],
        ) -> Any:
            async with AsyncSessionFactory() as session:
                data["session"] = session
                return await handler(event, data)

    dp.update.middleware(SessionMiddleware())
    dp.message.middleware(AuthMiddleware())

    # Register routers (order matters — more specific handlers first)
    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(reports.router)
    dp.include_router(transactions.router)

    # Setup scheduler
    scheduler = setup_scheduler(bot)
    scheduler.start()

    dp.startup.register(on_startup)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"FATAL: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
