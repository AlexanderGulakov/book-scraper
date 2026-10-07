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
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse, unquote

import bundles
from fetcher import Fetcher, describe_block
from models import Ad

log = logging.getLogger("olx")

# window.__PRERENDERED_STATE__ = "...";  (значення — JSON-рядок, тобто JSON усередині JSON)
_STATE_RE = re.compile(r'window\.__PRERENDERED_STATE__\s*=\s*("(?:[^"\\]|\\.)*")')


class OlxError(RuntimeError):
    pass


# У посиланні OLX показує НЕ той id, що в даних: `-ID11lZx2.html` — це число
# 936150588, записане в base62 з алфавітом 0-9a-zA-Z. Перевірено на живих
# оголошеннях 2026-09-30.
#
# ⚠ Через це `--mark-ru <посилання>` мовчки не працював: він клав у базу ключ
# «11lZx2», а стан ключується числовим id, тож позначка нікуди не потрапляла.
# Кнопка в Telegram працювала, бо несе id з даних.
# ⚠ Це НЕ звичайний base62. В алфавіті OLX `v` і `w` переставлені місцями — і в
# нижньому регістрі, і у верхньому: ...stu**wv**xyz... і ...STU**WV**XYZ.
# Схоже на описку в їхній власній константі, але вона стабільна.
#
# Коштувало це тихо зіпсованих позначок: зі звичайним base62 близько чверті
# кодів розкодовувались у чуже число, і `--exclude <посилання>` клав у базу
# ключ неіснуючого оголошення — без жодної помилки й без жодного ефекту.
# Перевірено 2026-10-02 на 2889 парах (id ↔ код) з живої видачі:
# звичайний base62 — 734 помилки, переставлені лише великі — 278, обидва — 0.
_B62 = "0123456789abcdefghijklmnopqrstuwvxyzABCDEFGHIJKLMNOPQRSTUWVXYZ"
_URL_ID_RE = re.compile(r"-ID([0-9a-zA-Z]+)\.html")


def ad_id_from(raw: str) -> str | None:
    """Числовий id оголошення з посилання, коду з посилання або самого id.

    Повертає None, якщо розпізнати не вдалось — краще сказати «не знаю», ніж
    позначити в базі неіснуючий ключ і вважати справу зробленою.
    """
    s = str(raw or "").strip()
    if not s:
        return None
    m = _URL_ID_RE.search(s)
    if m:
        s = m.group(1)
    elif "/" in s or ".html" in s:
        return None                      # це посилання, але не на оголошення
    if s.isdigit():
        return s                         # уже id з даних
    n = 0
    for ch in s:
        i = _B62.find(ch)
        if i < 0:
            return None
        n = n * 62 + i
    return str(n)


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
        refreshed_time=a.get("lastRefreshTime"),
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
              min_repeats: int = 2, watch_name: str = "",
              catalog: Iterable[dict[str, Any]] = ()) -> bool:
    """Схоже, що в оголошенні не одна книжка, а кілька. Логіка — у `bundles`.

    Потрібно, бо за комплект люди готові платити більше, ніж за окрему книжку,
    і одна межа `max_price` на такий watch не працює.
    """
    return bundles.looks_like_bundle(ad.title or "", watch_name, catalog=catalog,
                                     include=include,
                                     bundle_keywords=bundle_keywords,
                                     min_repeats=min_repeats)


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


# ─────────────────────────────────────────────── JSON-пошук замість HTML

SEARCH_API = "https://www.olx.ua/api/v1/offers/"

# Шлях категорії в URL watch'а → її id для API. Словник, а не константа, бо
# невідому категорію ми мусимо вміти РОЗПІЗНАТИ і відкотитись на HTML:
# без `category_id` той самий запит «кідрук» віддає 203 оголошення замість 194,
# і серед зайвих — техніка, іграшки й усе, що просто містить це слово.
CATEGORY_IDS = {"hobbi-otdyh-i-sport/knigi-zhurnaly": 49}

API_PAGE = 50          # стеля OLX на один виклик; більше він однаково не дає


def api_params(url: str, *, page: int = 1, limit: int = API_PAGE) -> dict[str, Any] | None:
    """Параметри JSON-пошуку з URL watch'а, або None якщо URL не розкладається.

    None — це не помилка, а сигнал «цей watch лишаємо на HTML». Саме так і
    треба: мовчазна підміна запиту іншим страшніша за зайві дві секунди.
    """
    parts = urlparse(url)
    segs = [s for s in parts.path.split("/") if s]
    query = next((s[2:] for s in segs if s.startswith("q-")), None)
    if not query:
        return None
    cat_path = "/".join(s for s in segs if not s.startswith("q-") and s != "uk")
    cat_id = CATEGORY_IDS.get(cat_path)
    if cat_id is None:
        return None
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    return {
        "query": unquote(query).replace("-", " "),
        "category_id": cat_id,
        "currency": q.get("currency", "UAH"),
        # У HTML це search[order]=created_at:desc, в API — sort_by. Назви різні,
        # значення те саме.
        "sort_by": q.get("search[order]", "created_at:desc"),
        "limit": limit,
        "offset": (max(1, page) - 1) * limit,
    }


def _api_url(params: dict[str, Any]) -> str:
    return f"{SEARCH_API}?{urlencode(params)}"


def _api_price(raw: dict[str, Any]) -> dict[str, Any]:
    for p in raw.get("params") or []:
        if p.get("key") == "price":
            return p.get("value") or {}
    return {}


def _api_param(raw: dict[str, Any], key: str) -> str | None:
    for p in raw.get("params") or []:
        if p.get("key") == key:
            v = p.get("value") or {}
            return v.get("label") or v.get("key")
    return None


def _normalize_api(raw: dict[str, Any], *, promoted: bool, organic: bool,
                   desc_limit: int = 1200) -> Ad:
    price = _api_price(raw)
    value = price.get("value")
    photos = raw.get("photos") or []
    photo = (photos[0] or {}).get("link") if photos else None
    if photo:
        # OLX віддає шаблон із {width}x{height}; без підстановки це не посилання.
        photo = photo.replace("{width}", "1000").replace("{height}", "700")
    desc = raw.get("description")
    if isinstance(desc, str):
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", desc)).strip()[:desc_limit]
    else:
        desc = None
    return Ad(
        id=str(raw.get("id")),
        title=(raw.get("title") or "").strip(),
        url=raw.get("url") or "",
        price=float(value) if isinstance(value, (int, float)) else None,
        currency=price.get("currency"),
        price_text=price.get("label") or "—",
        negotiable=bool(price.get("negotiable")),
        promoted=promoted,
        condition=_api_param(raw, "state"),
        city=((raw.get("location") or {}).get("city") or {}).get("name"),
        created_time=raw.get("created_time"),
        refreshed_time=raw.get("last_refresh_time"),
        photo=photo,
        # У HTML «схоже на ваш запит» лежить в окремому expansionListing, якого
        # ми не читаємо. В API той самий поділ дає metadata.source: усе, що не
        # organic і не promoted, — не точний збіг.
        similar=not (promoted or organic),
        reason="promoted" if promoted else ("organic" if organic else "similar"),
        source="olx",
        description=desc,
    )


def parse_ads_api(body: str) -> list[Ad]:
    data = json.loads(body)
    items = data.get("data") or []
    src = ((data.get("metadata") or {}).get("source") or {})
    promoted = set(src.get("promoted") or [])
    organic = set(src.get("organic") or [])
    # Якщо metadata.source немає — вважаємо всі точними збігами: краще зайве
    # оголошення, ніж мовчазна втрата всієї видачі через зміну формату.
    blind = not promoted and not organic
    return [_normalize_api(raw, promoted=i in promoted,
                           organic=blind or i in organic)
            for i, raw in enumerate(items)]


def fetch_watch_api(session: Fetcher, url: str, pages: int = 1, *,
                    pause: float = 2.0, limit: int = API_PAGE) -> list[Ad]:
    """Те саме, що `fetch_watch`, але через JSON-пошук.

    Навіщо. Заміряно 2026-10-03 з раннера GitHub: HTML-сторінка пошуку — 1.85 с
    і 3.1 МБ без описів; той самий запит через API — 0.4-0.8 с, 0.3-0.4 МБ і
    описи ВСІХ оголошень. Тобто зникає і три чверті ваги, і цілий клас запитів
    (`fetch_description`), і потреба в другій сторінці: `limit=50` перекриває
    HTML-сторінку з запасом.

    Звірено на запиті «кідрук»: HTML віддав 41 оголошення, API з тією самою
    категорією — 51, і всі 41 серед них. Строга надмножина, не інша вибірка.
    """
    seen: dict[str, Ad] = {}
    for page in range(1, max(1, pages) + 1):
        params = api_params(url, page=page, limit=limit)
        if params is None:
            raise OlxError(f"URL watch'а не розкладається в параметри API: {url}")
        status, body = session.get(_api_url(params), timeout=30,
                                   referer=fetcher_home())
        if status != 200:
            raise OlxError(f"API відповів {status}")
        try:
            ads = parse_ads_api(body)
        except ValueError as exc:
            raise OlxError(f"API віддав не JSON: {exc}") from exc
        log.info("  сторінка %s (api): %s оголошень", page, len(ads))
        for ad in ads:
            seen.setdefault(ad.id, ad)
        if not ads:
            break
        if page < pages:
            time.sleep(pause + random.uniform(0, 1.5))
    return list(seen.values())
