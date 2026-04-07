from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import settings

engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args={
        "prepared_statement_cache_size": 0,
        "timeout": settings.db_connect_timeout_sec,
        "command_timeout": settings.db_command_timeout_sec,
    },
)

AsyncSessionFactory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_session() -> AsyncSession:
    async with AsyncSessionFactory() as session:
        yield session
