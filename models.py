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
    photo: str | None = None
    similar: bool = False
    reason: str | None = None
    author: str | None = None    # Bookflea вказує автора окремим полем
    source: str = "olx"          # яке джерело віддало оголошення
    matched: str | None = None   # яке ключове слово спрацювало (Bookflea)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
