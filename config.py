from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Telegram
    telegram_bot_token: str

    # OpenAI
    openai_api_key: str
    openai_base_url: Optional[str] = None
    openai_https_proxy: Optional[str] = None
    openai_model_categorize: str = "gpt-4o-mini"
    openai_model_report: str = "gpt-4o"
    llm_strict_startup_check: bool = True
    run_migrations_on_startup: bool = True

    # Database
    database_url: str  # postgresql+asyncpg://user:pass@host/db
    db_connect_timeout_sec: int = 15
    db_command_timeout_sec: int = 60
    startup_migration_retries: int = 5
    startup_migration_retry_delay_sec: int = 5

    @property
    def async_database_url(self) -> str:
        """Adds ssl=require for Supabase if not already present."""
        url = self.database_url
        if ("supabase.co" in url or "supabase.com" in url) and "ssl=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}ssl=require"
        return url

    # Admin IDs (comma-separated)
    admin_telegram_ids: str = ""

    # Exchange rates
    exchangerate_api_key: str

    # App settings
    daily_report_hour: int = 21  # UTC hour for daily reports
    llm_request_timeout_sec: int = 40
    currency_request_timeout_sec: int = 10

    @property
    def admin_ids(self) -> list[int]:
        if not self.admin_telegram_ids:
            return []
        return [int(x.strip()) for x in self.admin_telegram_ids.split(",") if x.strip()]

    model_config = ConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
