# Финансовый Telegram-бот — контекст проекта

## Стек
- Python 3.9 (важно: синтаксис `X | None` не работает, использовать `Optional[X]` из `typing`)
- aiogram 3.13.1 (async Telegram bot framework)
- PostgreSQL 16 + SQLAlchemy 2.0 async + Alembic
- OpenAI API (gpt-4o-mini — категоризация, gpt-4o — месячные отчёты)
- exchangerate-api.com — курсы валют
- matplotlib — графики (pie + waterfall)
- APScheduler — ежедневные/месячные отчёты
- Docker + CapRover (Yandex Cloud) — прод

## Структура
```
bot/
  main.py              — точка входа, регистрация роутеров и middleware
  handlers/
    start.py           — /start, регистрация, /goal, клавиатура MAIN_KEYBOARD
    admin.py           — /create_invite, /stats (только для ADMIN_TELEGRAM_IDS)
    transactions.py    — ввод трат/доходов, inline-кнопки категорий
    reports.py         — /report (сегодня), /month (месячный)
  middlewares/
    auth.py            — инжектит User в data["user"] если пользователь активен
  services/
    llm.py             — parse_transaction (OpenAI), generate_monthly_advice
    currency.py        — convert_to_rub, format_amount
    charts.py          — build_pie_chart, build_waterfall_chart (без эмодзи в лейблах)
    scheduler.py       — ежедневный отчёт в DAILY_REPORT_HOUR UTC, месячный 1-го в 09:00 UTC
db/
  models.py            — User, Transaction, UserCategoryMapping (+ InviteCode — не используется)
  session.py           — AsyncSessionFactory
  migrations/versions/ — Alembic миграции (0001_initial, 0002_category_mappings)
config.py              — pydantic-settings, читает .env
```

## Ключевые архитектурные решения

### Middleware
- `SessionMiddleware` — создаёт async DB сессию на каждый апдейт, кладёт в `data["session"]`
- `AuthMiddleware` — ищет пользователя в БД, кладёт в `data["user"]`. Если нет — user=None
- Порядок в main.py: `dp.update.middleware(SessionMiddleware())`, `dp.message.middleware(AuthMiddleware())`

### Порядок роутеров (критично!)
```python
dp.include_router(start.router)
dp.include_router(admin.router)
dp.include_router(reports.router)    # до transactions — иначе кнопки клавиатуры перехватит handle_transaction
dp.include_router(transactions.router)
```

### Состояния FSM
- `OnboardingStates.waiting_for_goal` — в start.py, ждёт цель после /start
- `CategoryEditState.waiting_for_custom` — в transactions.py, ждёт название своей категории
- Обработчик с state-фильтром должен регистрироваться ДО общего `handle_transaction`

### Кастомные категории (UserCategoryMapping)
- После сохранения транзакции — inline-кнопки "✅ Верно" / "✏️ Изменить"
- При изменении: список категорий или "➕ Своя категория"
- Сохраняет правило: keyword (description.lower()) → category
- При новой транзакции `_custom_category()` проверяет маппинги через substring match

### Клавиатура
- `MAIN_KEYBOARD` определена в `start.py`, импортируется в `transactions.py`
- Кнопки: "📊 Отчёт за сегодня", "📅 Месячный отчёт", "🎯 Изменить цель", "📋 Последние 5"
- Привязаны через `or_f(Command("..."), F.text == "текст кнопки")` в каждом обработчике

## .env переменные
```
TELEGRAM_BOT_TOKEN=     # от @BotFather
OPENAI_API_KEY=         # platform.openai.com
DATABASE_URL=postgresql+asyncpg://finbot:finbot_dev_password@localhost:5432/finbot
ADMIN_TELEGRAM_IDS=     # твой Telegram ID (через запятую если несколько)
EXCHANGERATE_API_KEY=   # exchangerate-api.com
DAILY_REPORT_HOUR=21    # UTC час ежедневного отчёта (21 = 00:00 Ташкент UTC+5)
```

## Локальный запуск
```bash
# 1. Поднять PostgreSQL
brew services start postgresql@16
/opt/homebrew/opt/postgresql@16/bin/createuser -s finbot
/opt/homebrew/opt/postgresql@16/bin/createdb finbot -O finbot
/opt/homebrew/opt/postgresql@16/bin/psql -d postgres -c "ALTER USER finbot WITH PASSWORD 'finbot_dev_password';"

# 2. Запустить (миграции применяются автоматически при старте)
export $(cat .env | xargs) && python -m bot.main
```

## Деплой
- Push в `main` → GitHub Actions → CapRover (Yandex Cloud)
- Секреты: `CAPROVER_SERVER`, `CAPROVER_APP_NAME`, `CAPROVER_APP_TOKEN`
- Env-переменные на проде настраиваются в CapRover dashboard

## Частые ловушки
- Python 3.9: везде `Optional[X]` вместо `X | None`, включая возвращаемые типы функций
- aiogram: `Command("x") | F.text == "y"` — нужны скобки или `or_f(Command("x"), F.text == "y")`
- parse_mode: глобально `ParseMode.MARKDOWN`, не смешивать с HTML
- SQLAlchemy async: `expire_on_commit=False` уже настроен в session.py
- Миграции: запускать через `export $(cat .env | xargs) && alembic upgrade head`
