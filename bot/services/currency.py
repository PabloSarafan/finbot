import time
from decimal import Decimal

import httpx

from config import settings

_cache: dict[str, tuple[float, Decimal]] = {}  # currency -> (timestamp, rate_to_rub)
CACHE_TTL = 3600  # 1 hour
_http_client = httpx.AsyncClient(timeout=settings.currency_request_timeout_sec)


async def get_rate_to_rub(currency: str) -> Decimal:
    """Return exchange rate: how many RUB per 1 unit of currency."""
    currency = currency.upper()
    if currency == "RUB":
        return Decimal("1")

    now = time.time()
    if currency in _cache:
        ts, rate = _cache[currency]
        if now - ts < CACHE_TTL:
            return rate

    rate = await _fetch_rate(currency)
    _cache[currency] = (now, rate)
    return rate


async def _fetch_rate(currency: str) -> Decimal:
    url = f"https://v6.exchangerate-api.com/v6/{settings.exchangerate_api_key}/pair/{currency}/RUB"
    resp = await _http_client.get(url)
    resp.raise_for_status()
    data = resp.json()
    if data.get("result") != "success":
        raise ValueError(f"Exchange rate API error: {data}")
    return Decimal(str(data["conversion_rate"]))


async def convert_to_rub(amount: Decimal, currency: str) -> tuple[Decimal, Decimal]:
    """Returns (amount_rub, exchange_rate)."""
    rate = await get_rate_to_rub(currency)
    return (amount * rate).quantize(Decimal("0.01")), rate


async def convert_from_rub(amount_rub: Decimal, target_currency: str) -> Decimal:
    target = (target_currency or "RUB").upper()
    if target == "RUB":
        return amount_rub.quantize(Decimal("0.01"))
    rate_to_rub = await get_rate_to_rub(target)
    if rate_to_rub == 0:
        return amount_rub.quantize(Decimal("0.01"))
    return (amount_rub / rate_to_rub).quantize(Decimal("0.01"))


CURRENCY_SYMBOLS = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
    "UZS": "сум",
    "KZT": "₸",
    "GBP": "£",
    "CNY": "¥",
}


def format_amount(amount: Decimal, currency: str) -> str:
    symbol = CURRENCY_SYMBOLS.get(currency.upper(), currency)
    return f"{amount:,.0f} {symbol}"
