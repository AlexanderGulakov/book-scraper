"""Фетч і парсинг сторінок пошуку OLX.ua.

OLX рендерить результати пошуку на сервері й кладе їх у сторінку як
`window.__PRERENDERED_STATE__ = "<json-рядок>";`. Тому не потрібен ні
браузер, ні реверс-інжиніринг приватного API — достатньо GET + regex + json.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Iterable
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from fetcher import Fetcher, describe_block
from models import Ad

log = logging.getLogger("olx")

# window.__PRERENDERED_STATE__ = "...";  (значення — JSON-рядок, тобто JSON усередині JSON)
_STATE_RE = re.compile(r'window\.__PRERENDERED_STATE__\s*=\s*("(?:[^"\\]|\\.)*")')


class OlxError(RuntimeError):
    pass


def build_session() -> Fetcher:
    return Fetcher()


def with_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parts = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    q.append(("page", str(page)))
    return urlunparse(parts._replace(query=urlencode(q)))


def fetch_html(session: Fetcher, url: str, *, timeout: int = 30, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            # Перед першим запитом заходимо на головну — так робить живий браузер,
            # і саме там видаються кукі, без яких пошук інколи віддає 403.
            session.warmup()
            status, body = session.get(url, timeout=timeout, referer=fetcher_home())
            if status == 200:
                return body
            if status in (403, 429, 503):
                who = describe_block(body)
                raise OlxError(f"OLX відповів {status} (блокує: {who})")
            raise OlxError(f"HTTP {status}")
        except Exception as exc:  # noqa: BLE001 - хочемо ретраїти будь-що мережеве
            last = exc
            if attempt < retries:
                session.reset()  # нова сесія = нові кукі, інколи цього досить
                sleep = attempt * 5 + random.uniform(0, 4)
                log.warning("Спроба %s/%s не вдалась (%s), повтор через %.1fс", attempt, retries, exc, sleep)
                time.sleep(sleep)
    raise OlxError(f"Не вдалось завантажити {url}: {last}")


def fetcher_home() -> str:
    from fetcher import HOME
    return HOME


def parse_ads(html: str) -> list[Ad]:
    m = _STATE_RE.search(html)
    if not m:
        raise OlxError(
            "У відповіді немає __PRERENDERED_STATE__ — імовірно, віддали капчу або "
            "OLX змінив розмітку."
        )
    state = json.loads(json.loads(m.group(1)))
    raw = (state.get("listing") or {}).get("listing", {}).get("ads") or []
    return [_normalize(a) for a in raw]


def _normalize(a: dict[str, Any]) -> Ad:
    price = a.get("price") or {}
    regular = price.get("regularPrice") or {}
    value = regular.get("value")
    reason = a.get("searchReason")
    return Ad(
        id=str(a.get("id")),
        title=(a.get("title") or "").strip(),
        url=a.get("url") or "",
        price=float(value) if isinstance(value, (int, float)) else None,
        currency=regular.get("currencyCode"),
        price_text=price.get("displayValue") or ("Безкоштовно" if price.get("free") else "—"),
        negotiable=bool(regular.get("negotiable")),
        promoted=bool(a.get("isPromoted")) or reason == "promoted",
        condition=a.get("itemCondition"),
        city=(a.get("location") or {}).get("cityName"),
        created_time=a.get("createdTime"),
        photo=(a.get("photos") or [None])[0],
        # OLX тримає точні збіги в listing.ads (searchReason: organic|promoted),
        # а "схоже на ваш запит" — в окремому expansionListing, який ми не читаємо.
        # Перевірка нижче — просто запобіжник на випадок нових значень.
        similar=reason not in (None, "organic", "promoted"),
        reason=reason,
        source="olx",
    )


def fetch_watch(session: Fetcher, url: str, pages: int = 1, *, pause: float = 2.0) -> list[Ad]:
    """Повертає унікальні оголошення з перших `pages` сторінок пошуку."""
    seen: dict[str, Ad] = {}
    for page in range(1, max(1, pages) + 1):
        page_url = with_page(url, page)
        html = fetch_html(session, page_url)
        ads = parse_ads(html)
        log.info("  сторінка %s: %s оголошень", page, len(ads))
        for ad in ads:
            seen.setdefault(ad.id, ad)
        if not ads:
            break
        if page < pages:
            time.sleep(pause + random.uniform(0, 1.5))
    return list(seen.values())


def matches(ad: Ad, *, include: Iterable[str] = (), exclude: Iterable[str] = (),
            cities: Iterable[str] = (), skip_promoted: bool = False,
            allow_similar: bool = False) -> bool:
    """Фільтри, не пов'язані з ціною (застосовуються ДО відстеження стану)."""
    if ad.similar and not allow_similar:
        return False
    if skip_promoted and ad.promoted:
        return False
    haystack = f"{ad.title}".casefold()
    inc = [w.casefold() for w in include if w]
    if inc and not any(w in haystack for w in inc):
        return False
    if any(w.casefold() in haystack for w in exclude if w):
        return False
    cts = [c.casefold() for c in cities if c]
    if cts and (ad.city or "").casefold() not in cts:
        return False
    return True


def price_ok(ad: Ad, *, max_price: float | None, min_price: float | None,
             currency: str | None, allow_no_price: bool = False) -> bool:
    if ad.price is None:
        return allow_no_price
    if currency:
        # Невідома валюта — теж привід відмовити: краще пропустити оголошення,
        # ніж прийняти 220 zł за 220 грн.
        if not ad.currency or ad.currency.upper() != currency.upper():
            return False
    if max_price is not None and ad.price > max_price:
        return False
    if min_price is not None and ad.price < min_price:
        return False
    return True
