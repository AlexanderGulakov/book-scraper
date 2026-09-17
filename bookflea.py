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


_ANCHOR_RE = re.compile(r'<a\s+href="(/ads/[^"]+)"', re.I)
_ALT_RE = re.compile(r'<img[^>]*\salt="([^"]*)"', re.I)
_SPAN_SM_RE = re.compile(r'<span[^>]*class="[^"]*\btext-sm\b[^"]*"[^>]*>(.*?)</span>', re.I | re.S)
_SPAN_BASE_RE = re.compile(r'<span[^>]*class="[^"]*\btext-base\b[^"]*"[^>]*>(.*?)</span>', re.I | re.S)
_B_RE = re.compile(r"<b>(.*?)</b>", re.I | re.S)
_TAGS_RE = re.compile(r"<[^>]+>")


def _soup(html: str) -> BeautifulSoup:
    """lxml розбирає «брудний» HTML як браузер; html.parser — запасний варіант."""
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:  # noqa: BLE001 - lxml може бути не встановлений
            continue
    return BeautifulSoup(html, "html.parser")


def _clean(s: str | None) -> str | None:
    if not s:
        return None
    import html as _html
    return _html.unescape(_TAGS_RE.sub("", s)).strip() or None


def parse_from_raw(html: str) -> dict[str, dict[str, str | None]]:
    """Витягує поля прямо з тексту сторінки, не покладаючись на дерево DOM.

    Запасний шлях: якщо парсер HTML з якоїсь причини не зібрав картку
    (інша версія бібліотеки, зламана розмітка, обрізана відповідь), розмітка
    Букфлі достатньо машинна, щоб прочитати її регулярками.
    """
    out: dict[str, dict[str, str | None]] = {}
    matches = list(_ANCHOR_RE.finditer(html))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(html), m.end() + 4000)
        block = html[m.start():end]
        ad_id = m.group(1).rstrip("/").split("/")[-1]
        if ad_id in out:
            continue
        alt = _ALT_RE.search(block)
        alt_title = alt_author = None
        if alt:
            parts = [p.strip() for p in _clean(alt.group(1)).split("—")] if _clean(alt.group(1)) else []
            alt_title = parts[0] if parts else None
            alt_author = parts[1] if len(parts) > 1 else None
        sm = _SPAN_SM_RE.search(block)
        base = _SPAN_BASE_RE.search(block)
        b = _B_RE.search(block)
        out[ad_id] = {
            "author": _clean(sm.group(1)) if sm else alt_author,
            "title": _clean(base.group(1)) if base else alt_title,
            "price": _clean(b.group(1)) if b else None,
        }
    return out


def parse_listings(html: str) -> list[Ad]:
    soup = _soup(html)
    anchors = soup.select('a[href^="/ads/"]')
    if not anchors and "bookflea" not in html.lower():
        raise BookfleaError("Відповідь не схожа на сторінку Bookflea (капча чи редирект?)")

    # Текстовий розбір тієї ж сторінки — страховка, якщо дерево DOM зібралось не так.
    raw = parse_from_raw(html)

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
        price_raw = b.get_text(strip=True) if b else ""

        # Якщо дерево дало порожню картку — беремо те, що видно в самому тексті.
        fb = raw.get(ad_id)
        if fb:
            author = author or fb.get("author")
            title = title or fb.get("title")
            price_raw = price_raw or (fb.get("price") or "")

        price, currency, price_text = _parse_price(price_raw)

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

    blank = [a.id for a in out if a.title == "(без назви)"]
    if blank:
        # Це не має траплятись. Якщо трапилось — залишаємо в логу все, що
        # потрібно, аби зрозуміти, що саме віддав сервер цього разу.
        first = _ANCHOR_RE.search(html)
        log.warning(
            "⚠ %s з %s карток без назви. len(html)=%s, "
            "text-base у тексті=%s, img alt у тексті=%s, парсер=%s",
            len(blank), len(out), len(html),
            html.count("text-base"), html.count("alt=\""), type(_soup("<i></i>").builder).__name__,
        )
        if first:
            log.warning("  зразок розмітки: %s", html[first.start():first.start() + 400])
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
        named = sum(1 for a in page if a.title != "(без назви)")
        log.info("  найновіші %s (з назвою %s): збігів %s", len(page), named, len(found))
        if page and not found:
            log.info("  ключові слова: %s", ", ".join(keywords) or "(порожньо!)")
            log.info("  перші 3 картки: %s",
                     " | ".join(f"{a.author or '?'} — {a.title}" for a in page[:3]))
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
