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
            require: Iterable[str] = (), cities: Iterable[str] = (),
            skip_promoted: bool = False, allow_similar: bool = False) -> bool:
    """Фільтри, не пов'язані з ціною (застосовуються ДО відстеження стану).

    `include` і `require` — два незалежні списки, кожен по АБО всередині, але
    між собою по І. Одного списку тут мало, і це коштувало двох хибних
    спрацювань 2026-09-25: назви книжок Поттера не унікальні («Таємна кімната»
    є ще й у «Школи без нудьги», «Філософський камінь» — у Сартакова), а
    вимагати саме «поттер» не можна, бо тоді загубляться оголошення, підписані
    лише назвою. Разом: назва книжки І хоч якась згадка автора чи серії.

    Заміряно перед тим, як це вводити: зі 122 оголошень, що збіглись по назві
    книжки, 118 згадують Поттера/Ролінґ/Hogwarts. Решта чотири — Сартаков,
    «Школа без нудьги», гуртовий лот і колекційне MinaLima (це вже ловиться
    латинським «potter»). Тобто вимога не коштує майже нічого.
    """
    if ad.similar and not allow_similar:
        return False
    if skip_promoted and ad.promoted:
        return False
    # Автор входить у пошук нарівні з назвою: Букфлі віддає його окремим полем,
    # і без цього exclude_keywords не може відсіяти чужого автора («Кінгфішер»,
    # «Векс Кінг»), бо в назві книжки його прізвища немає. В OLX author=None,
    # тож там нічого не змінюється.
    haystack = f"{ad.title} {ad.author or ''}".casefold()
    inc = [w.casefold() for w in include if w]
    if inc and not any(w in haystack for w in inc):
        return False
    req = [w.casefold() for w in require if w]
    if req and not any(w in haystack for w in req):
        return False
    if any(w.casefold() in haystack for w in exclude if w):
        return False
    cts = [c.casefold() for c in cities if c]
    if cts and (ad.city or "").casefold() not in cts:
        return False
    return True


def is_bundle(ad: Ad, *, include: Iterable[str] = (), bundle_keywords: Iterable[str] = (),
              min_repeats: int = 2) -> bool:
    """Схоже, що в оголошенні не одна книжка, а кілька.

    Дві ознаки, обидві по заголовку:
    1. пряма вказівка — «комплект», «набір», «всі частини»;
    2. ключове слово повторюється: «Служниця спостерігає, Весілля служниці,
       Секрет служниці» — три згадки «служниц», отже три книжки. Одна книжка
       згадує себе один раз.

    Потрібно, бо за комплект люди готові платити більше, ніж за окрему книжку,
    і одна межа `max_price` на такий watch не працює.
    """
    title = (ad.title or "").casefold()
    if any(w.casefold() in title for w in bundle_keywords if w):
        return True
    hits = sum(title.count(w.casefold()) for w in include if w)
    return hits >= min_repeats


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


# ------------------------------------------------------------------ опис

def fetch_description(session: Fetcher, url: str, *, timeout: int = 20,
                      limit: int = 1200) -> str | None:
    """Опис оголошення зі сторінки або None, якщо не вдалось.

    Навіщо. У видачі опису немає — лише заголовок, а заголовок бреше. Саме
    в описі стоїть видавництво («Росмен» = російське видання) і те, що в
    лоті кілька книжок, хоча в назві одна. Коштує це +1 запит, тому
    викликається тільки для НОВИХ оголошень і тільки в тих watch'ах, де від
    опису справді залежить рішення (`needs_description: true`).

    None означає «не вдалось спитати», а не «опису немає»: викликач мусить
    розрізняти ці випадки, інакше мережевий збій тихо перетвориться на
    «видавництво не російське».
    """
    try:
        status, body = session.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        log.debug("Не вдалось дістати опис %s: %s", url, exc)
        return None
    if status != 200:
        return None
    m = _STATE_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(json.loads(m.group(1)))
    except ValueError:
        return None
    ad = (data.get("ad") or {}).get("ad") or data.get("ad") or {}
    desc = ad.get("description")
    if not isinstance(desc, str):
        return None
    # HTML з опису нам не потрібен — правила дивляться на слова.
    text = re.sub(r"<[^>]+>", " ", desc)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# ------------------------------------------------------- чи оголошення ще живе

def ad_state(session: Fetcher, url: str, *, timeout: int = 20) -> str:
    """'alive' | 'gone' | 'unknown' — чи висить ще це оголошення на OLX.

    Навіщо. Зникнення з першої сторінки пошуку НЕ означає продаж: свіжі
    оголошення просто виштовхують старі вниз. Єдиний надійний спосіб —
    спитати саму сторінку оголошення. Перевірено 2026-09-19:

        живе     → HTTP 200, у __PRERENDERED_STATE__ ad.status == "active"
        знятe    → HTTP 410 Gone
        не було  → HTTP 404

    Текст сторінки для цього не годиться: слова «видалено», «404», «сторінку
    не знайдено» лежать у локалізаційному бандлі й присутні навіть на живій
    сторінці.
    """
    try:
        status, body = session.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        log.debug("Не вдалось перевірити %s: %s", url, exc)
        return "unknown"

    if status in (404, 410):
        return "gone"
    if status != 200:
        return "unknown"

    # 200 буває і в знятого оголошення, якщо OLX вирішив показати заглушку,
    # тому дивимось ще й на сам статус у даних сторінки.
    m = _STATE_RE.search(body)
    if not m:
        return "unknown"
    try:
        data = json.loads(json.loads(m.group(1)))
    except ValueError:
        return "unknown"
    ad = (data.get("ad") or {}).get("ad") or data.get("ad") or {}
    status_field = ad.get("status")
    if status_field is None and not ad.get("title"):
        return "unknown"
    return "alive" if status_field in (None, "active") else "gone"
