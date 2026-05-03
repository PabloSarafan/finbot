import json
import logging
from decimal import Decimal
from typing import List, Optional

import httpx
from openai import AsyncOpenAI

from config import settings

logger = logging.getLogger(__name__)


def _extract_json_object(raw: str) -> str:
    """
    Model output sometimes includes markdown fences or extra prose.
    Return the substring from the first '{' to the last '}' inclusive.
    """
    s = (raw or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        return s[start : end + 1]
    return s

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
CATEGORIES_INCOME = ["Зарплата 💼", "Фриланс 💻", "Инвестиции 📈", "Копилка 🏦", "Прочее доход 💰"]

SYSTEM_PROMPT_DEFAULT = f"""
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


def _system_prompt_for_user_categories(names: List[str]) -> str:
    joined = "\n".join(f"  - {c}" for c in names)
    return f"""
Ты — умный финансовый ассистент. Пользователь пишет о трате или доходе.
Извлеки из сообщения:
- amount: число (сумма)
- currency: код валюты (RUB, USD, EUR, UZS, KZT, GBP, CNY и т.д.)
- type: "expense" (расход) или "income" (доход)
- category: РОВНО одна строка из списка пользователя ниже (скопируй текст без изменений)
- description: краткое описание (2-4 слова)

Список категорий пользователя (только они, ничего другого):
{joined}

Правила:
- Выбери категорию по смыслу (и для расхода, и для дохода — из этого же списка).
- Если валюта не указана явно — предполагай RUB. "сум" или "сумов" = UZS
- Верни ТОЛЬКО валидный JSON без markdown

Формат ответа:
{{"amount": 200.0, "currency": "RUB", "type": "expense", "category": "<одна строка из списка>", "description": "Кофе"}}
"""


def _normalize_category_to_allowed(raw: str, allowed: List[str]) -> str:
    if not allowed:
        return raw
    s = (raw or "").strip()
    if s in allowed:
        return s
    low = s.lower()
    for a in allowed:
        if a.lower() == low:
            return a
    logger.warning("LLM category %r not in user list; falling back to %r", s, allowed[0])
    return allowed[0]


async def parse_transaction(
    user_message: str,
    custom_category_names: Optional[List[str]] = None,
    default_currency: str = "RUB",
) -> Optional[dict]:
    """
    Returns dict with keys: amount, currency, type, category, description
    Returns None if message is not a transaction.
    """
    allowed: Optional[List[str]] = None
    if custom_category_names:
        allowed = [str(x).strip() for x in custom_category_names if str(x).strip()]
        if not allowed:
            allowed = None

    system_prompt = (
        _system_prompt_for_user_categories(allowed)
        if allowed
        else SYSTEM_PROMPT_DEFAULT
    )
    base_currency = (default_currency or "RUB").upper()
    if base_currency != "RUB":
        system_prompt += (
            f"\nДополнительное правило: если валюта не указана явно, используй {base_currency}."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    response = None
    for use_json_object in (True, False):
        try:
            kwargs = dict(
                model=settings.openai_model_categorize,
                messages=messages,
                temperature=0,
                max_tokens=150,
            )
            if use_json_object:
                kwargs["response_format"] = {"type": "json_object"}
            response = await client.chat.completions.create(**kwargs)
            break
        except Exception:
            if use_json_object:
                logger.warning(
                    "LLM categorize JSON mode failed; retrying without response_format"
                )
                continue
            logger.exception("LLM categorize request failed")
            return None

    content = (response.choices[0].message.content or "").strip()
    try:
        data = json.loads(_extract_json_object(content))
        # Validate required fields
        required = {"amount", "currency", "type", "category", "description"}
        if not required.issubset(data.keys()):
            return None
        if data["type"] not in ("income", "expense"):
            return None
        data["amount"] = Decimal(str(data["amount"]))
        data["currency"] = str(data["currency"]).upper()
        if allowed:
            data["category"] = _normalize_category_to_allowed(str(data.get("category", "")), allowed)
        return data
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        logger.warning(
            "Failed to parse transaction JSON from model; snippet=%r",
            content[:400],
        )
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
Ты — AI-финансовый аналитик в продукте CapitalMind.
Твоя задача — находить конкретные поведенческие паттерны в тратах пользователя и давать прикладные рекомендации.

ВАЖНО:
- Не пиши общие советы (например: "сократи расходы", "копи больше")
- Каждый совет должен основываться на данных пользователя
- Каждый совет должен содержать конкретное действие
- Пиши кратко, без воды

Финансовая цель пользователя:
{goal}

Статистика за {month}:
- Доход: {income} ₽
- Расход: {expenses} ₽
- Баланс: {balance} ₽
- Категории:
{categories}
- Лимиты по категориям (если есть):
{limits}

Сформируй 3-5 инсайтов. Используй ТОЛЬКО такие типы:

1. Поведенческий паттерн (например: повторяющиеся траты, рост категории, пики)
2. Аномалия (необычные траты или отклонения)
3. Инсайт по цели (прогноз достижения, ускорение или риск)

ФОРМАТ КАЖДОГО ИНСАЙТА:
- Наблюдение (конкретный факт из данных)
- Короткая интерпретация (что это значит)
- Конкретное действие (что сделать)

Пример формата:
"Ты тратишь на еду 450 000 ₽ в месяц (+30% к прошлому периоду).
Это самая быстрорастущая категория.
Попробуй снизить лимит до 350 000 ₽."

ОГРАНИЧЕНИЯ:
- Не более 2 предложений на инсайт
- Без вводных фраз ("возможно", "стоит задуматься")
- Не повторяй одни и те же идеи
- Максимум 180 слов
"""


async def generate_monthly_advice(
    goal: str,
    month: str,
    income: Decimal,
    expenses: Decimal,
    balance: Decimal,
    categories: list[tuple[str, Decimal]],
    limits: Optional[list[tuple[str, Decimal]]] = None,
) -> str:
    categories_str = "\n".join(
        f"  • {cat}: {amount:,.0f} ₽" for cat, amount in categories
    )
    limits_str = "нет лимитов"
    if limits:
        limits_str = "\n".join(
            f"  • {cat}: {amount:,.0f} ₽" for cat, amount in limits
        )
    prompt = MONTHLY_ADVICE_PROMPT.format(
        goal=goal or "не указана",
        month=month,
        income=f"{income:,.0f}",
        expenses=f"{expenses:,.0f}",
        balance=f"{balance:,.0f}",
        categories=categories_str,
        limits=limits_str,
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
