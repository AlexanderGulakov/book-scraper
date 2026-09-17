"""Фетч і парсинг bookflea.co.

Сайт на Next.js із серверним рендерингом: HTML уже містить картки оголошень,
тому вистачає GET + розбір DOM. Структура однієї картки:

    <a href="/ads/<id>">
      <img alt="Назва — Автор">
      <span class="text-sm ...">Автор</span>
      <span class="text-base ...">Назва</span>
      <span class="text-xl ..."><b>450 грн</b></span>
    </a>

Власний пошук сайту (`?q=`) нечіткий: на «Бункер» він віддає «Альманах Сталкер»,
на «Фундація» — Беґбедера. Тому він тут використовується лише як генератор
кандидатів, а остаточне рішення ухвалює локальна перевірка `matches_keyword()`.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Iterable
from urllib.parse import quote, unquote, urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from fetcher import Fetcher
from models import Ad

log = logging.getLogger("bookflea")

BASE = "https://www.bookflea.co"
SEARCH = BASE + "/search"

_PRICE_RE = re.compile(r"(\d[\d\s  ]*)(?:[.,](\d+))?\s*(грн|₴|\$|€|usd|eur)?", re.I)
_CURRENCY = {"грн": "UAH", "₴": "UAH", "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR"}


class BookfleaError(RuntimeError):
    pass


# ----------------------------------------------------------------- нормалізація

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'", "`": "'", "´": "'", "ʼ": "'"})


def norm(s: str | None) -> str:
    """Для порівнянь: нижній регістр, єдиний апостроф, стиснуті пробіли."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", s.translate(_APOSTROPHES).casefold()).strip()


def matches_keyword(ad: Ad, keywords: Iterable[str]) -> str | None:
    """Повертає перше ключове слово, яке реально є в авторі або назві."""
    hay = norm(f"{ad.author or ''} {ad.title or ''}")
    for kw in keywords:
        if kw and norm(kw) in hay:
            return kw
    return None


# ---------------------------------------------------------------------- парсинг

def _parse_price(text: str) -> tuple[float | None, str | None, str]:
    raw = (text or "").strip()
    m = _PRICE_RE.search(raw)
    if not m:
        return None, None, raw or "—"
    whole = re.sub(r"[\s  ]", "", m.group(1))
    frac = m.group(2) or "0"
    try:
        value = float(f"{whole}.{frac}")
    except ValueError:
        return None, None, raw
    return value, _CURRENCY.get((m.group(3) or "грн").lower(), "UAH"), raw


def _original_photo(img) -> str | None:
    """Next.js віддає /_next/image?url=<справжня адреса> — дістаємо оригінал."""
    if img is None:
        return None
    src = img.get("src") or ""
    if not src and img.get("srcset"):
        src = img["srcset"].split(",")[0].strip().split(" ")[0]
    if not src:
        return None
    if "/_next/image" in src:
        qs = parse_qs(urlparse(src).query)
        if qs.get("url"):
            return unquote(qs["url"][0])
    return urljoin(BASE, src)


def parse_listings(html: str) -> list[Ad]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select('a[href^="/ads/"]')
    if not anchors and "bookflea" not in html.lower():
        raise BookfleaError("Відповідь не схожа на сторінку Bookflea (капча чи редирект?)")

    out: list[Ad] = []
    seen: set[str] = set()
    for a in anchors:
        href = a.get("href") or ""
        ad_id = href.rstrip("/").split("/")[-1]
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)

        author = title = None
        # Спершу за класами Tailwind — вони стабільніші за порядок елементів.
        el = a.select_one("span.text-sm")
        if el:
            author = el.get_text(strip=True)
        el = a.select_one("span.text-base")
        if el:
            title = el.get_text(strip=True)

        img = a.find("img")
        if (not title or not author) and img and img.get("alt"):
            # alt має вигляд "Назва — Автор" і рятує, якщо класи змінились.
            parts = [p.strip() for p in img["alt"].split("—")]
            title = title or (parts[0] if parts else None)
            author = author or (parts[1] if len(parts) > 1 else None)

        if not title:
            spans = [s.get_text(strip=True) for s in a.find_all("span")]
            spans = [s for s in spans if s]
            if len(spans) >= 2:
                author, title = author or spans[0], spans[1]

        b = a.find("b")
        price, currency, price_text = _parse_price(b.get_text(strip=True) if b else "")

        out.append(Ad(
            id=ad_id,
            title=title or "(без назви)",
            url=urljoin(BASE, href),
            price=price,
            currency=currency,
            price_text=price_text,
            author=author,
            photo=_original_photo(img),
            source="bookflea",
        ))
    return out


# ------------------------------------------------------------------------ фетч

def _get(session: Fetcher, url: str, *, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            status, body = session.get(url, referer=BASE + "/")
            if status == 200:
                return body
            raise BookfleaError(f"HTTP {status}")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                sleep = attempt * 4 + random.uniform(0, 3)
                log.warning("Спроба %s/%s не вдалась (%s), повтор через %.1fс", attempt, retries, exc, sleep)
                time.sleep(sleep)
    raise BookfleaError(f"Не вдалось завантажити {url}: {last}")


def search(session: Fetcher, keyword: str, *, page_size: int = 24) -> list[Ad]:
    url = f"{SEARCH}?pageSize={page_size}&q={quote(keyword)}"
    return parse_listings(_get(session, url))


def latest(session: Fetcher, *, page_size: int = 24) -> list[Ad]:
    return parse_listings(_get(session, f"{SEARCH}?pageSize={page_size}"))


def collect(session: Fetcher, keywords: list[str], *, mode: str = "search",
            page_size: int = 24, pause: float = 2.5) -> tuple[list[Ad], list[str]]:
    """Повертає (збіги, усі побачені id).

    mode="latest"  — ОДИН запит за найновішими `page_size`, збіги шукаються
                     локально. Перевірено: /search віддає суворо від найновіших
                     до найстаріших, тож перша сторінка — це справді свіжина.
    mode="search"  — окремий запит на кожне слово: бачить увесь каталог, але
                     й навантаження вдесятеро більше.

    Другий елемент — повний список id зі сканованої сторінки. Він потрібен
    для перевірки «чи не проґавили ми щось»: якщо між запусками вікно
    провернулось повністю, значить page_size замалий.
    """
    found: dict[str, Ad] = {}

    if mode == "latest":
        page = latest(session, page_size=page_size)
        for ad in page:
            kw = matches_keyword(ad, keywords)
            if kw:
                ad.matched = kw
                found[ad.id] = ad
        log.info("  найновіші %s: збігів %s", len(page), len(found))
        return list(found.values()), [a.id for a in page]

    window: list[str] = []
    for i, kw in enumerate(keywords):
        try:
            candidates = search(session, kw, page_size=page_size)
        except BookfleaError as exc:
            log.warning("  «%s»: %s", kw, exc)
            continue
        hits = 0
        for ad in candidates:
            window.append(ad.id)
            if norm(kw) in norm(f"{ad.author or ''} {ad.title or ''}"):
                ad.matched = kw
                found.setdefault(ad.id, ad)
                hits += 1
        log.info("  «%s»: віддано %s, справжніх збігів %s", kw, len(candidates), hits)
        if i + 1 < len(keywords):
            time.sleep(pause + random.uniform(0, 1.5))

    return list(found.values()), window
