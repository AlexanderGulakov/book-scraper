"""Спільна модель оголошення для всіх джерел (OLX, Bookflea, …)."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Ad:
    id: str
    title: str
    url: str
    price: float | None          # числове значення або None (обмін / договірна без суми)
    currency: str | None
    price_text: str              # як показує сайт, напр. "1 990 грн."
    negotiable: bool = False
    promoted: bool = False
    condition: str | None = None
    city: str | None = None
    created_time: str | None = None
    # Час останнього підняття. Саме за ним OLX сортує «від найновіших» —
    # `created_time` у видачі буває й 2016 роком (оголошення створили давно,
    # а продають знову). Тримаємо обидва: різниця між ними і відрізняє справді
    # нове оголошення від піднятого старого.
    refreshed_time: str | None = None
    photo: str | None = None
    similar: bool = False
    reason: str | None = None
    author: str | None = None    # Bookflea вказує автора окремим полем
    source: str = "olx"          # яке джерело віддало оголошення
    matched: str | None = None   # яке ключове слово спрацювало (Bookflea)
    # Опис приходить разом із видачею лише в JSON-пошуку OLX; у HTML-видачі
    # його немає, і там він добувається окремим запитом (`fetch_description`).
    # None означає «не знаємо», а не «опису немає» — на цій різниці тримаються
    # мовні фільтри.
    description: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
