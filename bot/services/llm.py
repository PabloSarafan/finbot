import json
import logging
from decimal import Decimal
from typing import Optional

import httpx
from openai import AsyncOpenAI

from config import settings

logger = logging.getLogger(__name__)

client_kwargs = {"api_key": settings.openai_api_key}
if settings.openai_base_url:
    client_kwargs["base_url"] = settings.openai_base_url
http_client_kwargs = {"timeout": settings.llm_request_timeout_sec}
if settings.openai_https_proxy:
    # Route OpenAI traffic via explicit proxy for restricted regions.
    http_client_kwargs["proxy"] = settings.openai_https_proxy
    logger.info("OpenAI client initialized with HTTPS proxy")
client_kwargs["http_client"] = httpx.AsyncClient(**http_client_kwargs)
client = AsyncOpenAI(**client_kwargs)

CATEGORIES_EXPENSE = [
    "Еда 🛒", "Кафе ☕", "Транспорт 🚗", "ЖКХ 🏠",
    "Здоровье 💊", "Развлечения 🎬", "Одежда 👕",
    "Техника 💻", "Образование 📚", "Путешествия ✈️", "Прочее 📦",
]
CATEGORIES_INCOME = ["Зарплата 💼", "Фриланс 💻", "Инвестиции 📈", "Прочее доход 💰"]

SYSTEM_PROMPT = f"""
Ты — умный финансовый ассистент. Пользователь пишет о трате или доходе.
Извлеки из сообщения:
- amount: число (сумма)
- currency: код валюты (RUB, USD, EUR, UZS, KZT, GBP, CNY и т.д.)
- type: "expense" (расход) или "income" (доход)
- category: одна категория из списка ниже
- description: краткое описание (2-4 слова)

Категории расходов: {", ".join(CATEGORIES_EXPENSE)}
Категории доходов: {", ".join(CATEGORIES_INCOME)}

Правила:
- Если валюта не указана явно — предполагай RUB
- Если слово на русском — валюта RUB. "сум" или "сумов" = UZS
- Верни ТОЛЬКО валидный JSON без markdown

Формат ответа:
{{"amount": 200.0, "currency": "RUB", "type": "expense", "category": "Кафе ☕", "description": "Кофе"}}
"""


async def parse_transaction(user_message: str) -> Optional[dict]:
    """
    Returns dict with keys: amount, currency, type, category, description
    Returns None if message is not a transaction.
    """
    try:
        response = await client.chat.completions.create(
            model=settings.openai_model_categorize,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0,
            max_tokens=150,
        )
    except Exception:
        logger.exception("LLM categorize request failed")
        return None

    content = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(content)
        # Validate required fields
        required = {"amount", "currency", "type", "category", "description"}
        if not required.issubset(data.keys()):
            return None
        if data["type"] not in ("income", "expense"):
            return None
        data["amount"] = Decimal(str(data["amount"]))
        return data
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        logger.exception("Failed to parse transaction JSON from model")
        return None


async def assert_llm_ready() -> None:
    """
    Fail-fast startup check for LLM availability.
    Raises exception if model/api/base_url configuration is invalid or unavailable.
    """
    await client.chat.completions.create(
        model=settings.openai_model_categorize,
        messages=[
            {"role": "system", "content": "Reply with: ok"},
            {"role": "user", "content": "ping"},
        ],
        temperature=0,
        max_tokens=5,
    )
    logger.info("LLM startup check passed for model '%s'", settings.openai_model_categorize)


MONTHLY_ADVICE_PROMPT = """
Ты — персональный финансовый советник. Проанализируй траты пользователя за месяц и дай 3-5 конкретных советов.

Финансовая цель пользователя: {goal}

Статистика за {month}:
- Общие доходы: {income} ₽
- Общие расходы: {expenses} ₽
- Баланс: {balance} ₽
- Топ категорий расходов:
{categories}

Дай советы на русском языке, учитывая цель. Будь конкретным и мотивирующим. Максимум 200 слов.
"""


async def generate_monthly_advice(
    goal: str,
    month: str,
    income: Decimal,
    expenses: Decimal,
    balance: Decimal,
    categories: list[tuple[str, Decimal]],
) -> str:
    categories_str = "\n".join(
        f"  • {cat}: {amount:,.0f} ₽" for cat, amount in categories
    )
    prompt = MONTHLY_ADVICE_PROMPT.format(
        goal=goal or "не указана",
        month=month,
        income=f"{income:,.0f}",
        expenses=f"{expenses:,.0f}",
        balance=f"{balance:,.0f}",
        categories=categories_str,
    )
    try:
        response = await client.chat.completions.create(
            model=settings.openai_model_report,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=400,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception:
        logger.exception("LLM monthly advice request failed")
        return (
            "1) Определи недельный лимит на топ-1 категорию расходов.\n"
            "2) Откладывай минимум 10% каждого дохода в день поступления.\n"
            "3) Раз в неделю сверяй план с фактом и корректируй лимиты."
        )
