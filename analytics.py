"""Аналітика цін: за скільки книжки реально продаються, а з якої ціни лежать.

Ідея. OLX не каже «продано». Але він каже «оголошення знято» (HTTP 410), і
`main.sold_report()` щогодини це перевіряє й складає історію в `state["sold"]`.
Цей модуль перетворює історію на числа, з якими можна жити:

    продані     — оголошення, які зникли з сайту; вважаємо, що їх купили;
    довгожителі — оголошення, які ВСЕ ЩЕ у видачі і висять довше 14 днів;
                  це і є «з цієї ціни не рухається взагалі».

Дві множини не перетинаються за означенням і відповідають на два різні
питання. Медіана проданих каже, за скільки беруть; мінімум довгожителів —
з якої ціни перестають брати.

⚠ Три чесні застереження, які варто пам'ятати, дивлячись на звіт:

1. «Знято» ≠ «продано». Продавець міг передумати, а OLX сам архівує
   оголошення приблизно через 30 днів. Тому оголошення, які провисіли
   довго, у статистиці проданих важать менше (див. `sold_max_age_days`).
2. Ціна в оголошенні — не ціна угоди. Торг ми не бачимо.
3. Вибірка мала. Поки проданих менше за `min_sold`, чесна відповідь —
   «не вдалося визначити», і саме її модуль і дає.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import bundles

log = logging.getLogger("analytics")

# --------------------------------------------------------------- налаштування

DEFAULTS: dict[str, Any] = {
    "analytics_enabled": True,
    "analytics_window_days": 30,     # яку глибину історії проданих беремо
    "stalled_days": 14,              # з якого віку оголошення вважається «лежить»
    "stalled_seen_hours": 48,        # …і ще має бути у видачі, інакше воно просто зникло
    "sold_max_age_days": 29,         # висіло довше — зникнення вже не доводить продажу
    "min_sold": 1,                   # менше — «не вдалося визначити»
    "min_stalled": 1,                # менше — межу «задорого» по довгожителях не рахуємо
    "analytics_min_price": 20,       # нижче — сміття («100 книг по 1 грн»)
    "report_max_books": 60,          # стеля позицій у блоці
    "report_max_messages": 4,        # на скільки повідомлень можна різати звіт
    "daily_report_hour": 9,          # за Києвом; None/false — вимкнути денний звіт
    "daily_report_tz": "Europe/Kyiv",
}

# Російськомовні видання нас не цікавлять. `defaults.exclude_keywords` ловить
# явні маркери в заголовку, але не назви, написані російською («Гарри Поттер»,
# «Код да Винчи»). Тут — другий рубіж, уже для статистики: одна така книжка
# в вибірці зсуває медіану, бо російські видання коштують помітно менше.
RUSSIAN_MARKERS = [
    # літери, яких в українській немає взагалі — найнадійніша ознака
    "ы", "ъ", "э", "ё",
    # російські написання того, за чим стежимо
    "гарри", "стивен", "кинг", "ведьмак", "риггз", "дом странных", "странных",
    "библиотека душ", "город пустых", "кассандра клэр", "орудия смерти",
    "код да винчи", "ангелы и демоны", "точка обмана", "инферно",
    "утраченный символ", "цифровая крепость", "происхождение",
    "зов кукушки", "шелкопряд", "смертельная белизна", "на службе зла",
    # книжкова «обкладинка» російською
    "мягкая", "твердая", "обложка", "издание", "книги на русском", "русском",
    "детская", "подростков", "разные книги", "художественная",
]

# Збірні лоти чужих книжок: «Комплект книг 41 шт. Частина 3» за 7300 грн
# лежить у видачі «Гаррі Поттер», але ціна в ньому не про Гаррі Поттера.
#
# ⚠ Корені тут мають бути вужчі, ніж хочеться. Спокусливе «бібліотек» ловило
# не лише «книги з домашньої бібліотеки», а й «Бібліотеку душ» Ренсома Ріггза —
# цілу книжку викидало зі статистики через слово в назві.
LOT_MARKERS = [
    "домашньої бібліотеки", "домашней библиотеки", "домашня бібліотека",
    "з бібліотеки", "из библиотеки",
    "різні книги", "разные книги", "книги різних", "книг різних",
    "лот ", " лот", "лот:", "одним лотом",
    "грн/шт", "грн за шт", "гривень за шт", "по 100 грн",
    "зібрання книг", "повне зібрання", "собрание сочинений", "макулатур",
]
_LOT_COUNT_RE = re.compile(r"\b(\d{2,3})\s*(?:шт|кн|книг)", re.IGNORECASE)
LOT_MIN_COUNT = 10           # «41 шт.» — точно не одна книжка


def _opts(cfg: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **(cfg.get("defaults") or {})}


# ------------------------------------------------------------------- фільтри

def is_russian(title: str, markers: Iterable[str] = ()) -> bool:
    """Схоже на російськомовне видання.

    Свідомо асиметрично: краще викинути кілька українських оголошень, ніж
    підмішати російські ціни. Статистика від втрати п'яти спостережень
    страждає менше, ніж від систематичного зсуву вниз.
    """
    t = (title or "").casefold()
    return any(m in t for m in (markers or RUSSIAN_MARKERS) if m)


def is_lot(title: str, markers: Iterable[str] = ()) -> bool:
    """Оголошення схоже на гуртовий лот чужих книжок, а не на книжку."""
    t = (title or "").casefold()
    if any(m in t for m in (markers or LOT_MARKERS) if m):
        return True
    m = _LOT_COUNT_RE.search(t)
    return bool(m and int(m.group(1)) >= LOT_MIN_COUNT)


def title_is_bundle(title: str, watch_name: str = "", *,
                    catalog: Iterable[dict[str, Any]] = (),
                    include: Iterable[str] = (),
                    bundle_keywords: Iterable[str] = (), min_repeats: int = 2) -> bool:
    """Комплект однієї серії (а не гуртовий лот). Уся логіка — у `bundles`."""
    return bundles.looks_like_bundle(title, watch_name, catalog=catalog,
                                     include=include,
                                     bundle_keywords=bundle_keywords,
                                     min_repeats=min_repeats)


# ------------------------------------------------------- ключ книги (каталог)

_entry_matches = bundles.entry_matches


def book_match(title: str, watch_name: str, *, catalog: Iterable[dict[str, Any]] = (),
               include: Iterable[str] = (), bundle_keywords: Iterable[str] = ()
               ) -> tuple[str, bool]:
    """(назва книги, чи збіглось із каталогом).

    Гібрид, як і домовлялись: спершу явний каталог `books:` з watches.yaml
    (перший збіг у порядку файлу виграє), інакше — назва watch'а. Комплект
    в обох випадках їде в окрему позицію: за три томи просять інші гроші,
    і мішати їх з одиночкою означає отримати медіану, за якою не існує
    жодного реального оголошення.

    Прапорець «з каталогу» потрібен для дедуплікації: одне оголошення
    трапляється в кількох пошуках одразу, і з двох назв ми лишаємо
    конкретнішу («Клер: Місто скла», а не «Клер: за авторкою»).
    """
    bundle = title_is_bundle(title, watch_name, catalog=catalog, include=include,
                             bundle_keywords=bundle_keywords)
    for entry in bundles.matching_entries(title or "", watch_name, catalog):
        name = str(entry.get("name") or watch_name)
        if entry.get("bundle"):
            return name, True                # позиція каталогу вже про комплект
        return (f"{name} · комплект" if bundle else name), True
    return (f"{watch_name} · комплект" if bundle else watch_name), False


def book_key(title: str, watch_name: str, **kw: Any) -> str:
    return book_match(title, watch_name, **kw)[0]


def book_entry(title: str, watch_name: str,
               catalog: Iterable[dict[str, Any]] = ()) -> dict[str, Any] | None:
    """Сама позиція каталогу, а не лише її назва.

    Потрібна `rules.py`: пороги Telegram і бази живуть саме в позиції книги
    («Бот — до 500», «Прокляте дитя — до 180»), і без доступу до неї шар
    правил довелося б дублювати логікою зіставлення.
    """
    hits = bundles.matching_entries(title or "", watch_name, catalog)
    return hits[0] if hits else None


# ------------------------------------------------------------ збір вибірок

def _dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _age_days(rec: dict[str, Any], at: datetime) -> float | None:
    born = _dt(rec.get("created")) or _dt(rec.get("first"))
    if born is None:
        return None
    return max((at - born).total_seconds() / 86400, 0)


def _watch_index(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """name → конфіг watch'а. Ключ стану — це name, і в історії `sold` теж name."""
    out: dict[str, dict[str, Any]] = {}
    for i, w in enumerate(cfg.get("watches") or []):
        out[str(w.get("name") or w.get("id") or f"watch-{i}")] = w
        if w.get("id"):
            out.setdefault(str(w["id"]), w)
    return out


def _price_cap(w: dict[str, Any], opt: dict[str, Any], bundle: bool) -> float | None:
    cap = w.get("max_price", opt.get("max_price"))
    if bundle and w.get("max_price_bundle") is not None:
        cap = w["max_price_bundle"]
    return None if cap is None else float(cap)


def _usable(title: str, price: Any, cur: Any, w: dict[str, Any], opt: dict[str, Any],
            *, watch_name: str = "", catalog: Iterable[dict[str, Any]] = ()) -> bool:
    if price is None or not isinstance(price, (int, float)):
        return False
    if (cur or "UAH").upper() != str(opt.get("currency", "UAH")).upper():
        return False
    if float(price) < float(opt["analytics_min_price"]):
        return False
    if is_russian(title, opt.get("russian_markers") or RUSSIAN_MARKERS):
        return False
    if is_lot(title, opt.get("lot_markers") or LOT_MARKERS):
        return False
    bundle = title_is_bundle(title, watch_name or str(w.get("name") or ""),
                             catalog=catalog,
                             include=w.get("include_keywords") or [],
                             bundle_keywords=w.get("bundle_keywords") or [])
    cap = _price_cap(w, opt, bundle)
    return cap is None or float(price) <= cap


def collect(cfg: dict[str, Any], state: dict[str, Any], *,
            now: datetime | None = None,
            stats: dict[str, Any] | None = None) -> dict[str, dict[str, list]]:
    """Дві вибірки на кожну книгу: `sold` і `stalled`.

    `sold` — з історії `state["sold"]`, яку наповнює `main.sold_report()`.
    `stalled` — з живого стану: оголошення, яке ми БАЧИЛИ у видачі щойно
    (`seen` свіжий) і яке висить довше за `stalled_days`. Саме «бачили щойно»
    відрізняє «лежить, бо дорого» від «зникло, а ми ще не перевірили».
    """
    now = now or datetime.now(timezone.utc)
    opt = _opts(cfg)
    catalog = cfg.get("books") or []
    watches = _watch_index(cfg)

    # Одне оголошення — одне спостереження. Пошуки перетинаються навмисне
    # («Клер: Місто скла» і «Клер: за авторкою» бачать ті самі книжки), і без
    # цього три watch'і робили з п'яти оголошень п'ятнадцять. Із двох назв
    # лишаємо конкретнішу — ту, що прийшла з каталогу.
    picked: dict[tuple[str, str], tuple[str, bool, dict[str, Any]]] = {}

    def offer(pool: str, ad_id: str, name: str, from_catalog: bool,
              row: dict[str, Any]) -> None:
        cur = picked.get((pool, ad_id))
        if cur is None or (from_catalog and not cur[1]):
            picked[(pool, ad_id)] = (name, from_catalog, row)

    def classify(title: str, wname: str, w: dict[str, Any]) -> tuple[str, bool]:
        return book_match(title, wname, catalog=catalog,
                          include=w.get("include_keywords") or [],
                          bundle_keywords=w.get("bundle_keywords") or [])

    # ---- продані
    too_old = [0]
    since = now - timedelta(days=float(opt["analytics_window_days"]))
    for rec in state.get("sold") or []:
        gone = _dt(rec.get("gone"))
        if gone is None or gone < since:
            continue
        days = rec.get("days")
        if days is not None and float(days) > float(opt["sold_max_age_days"]):
            # Висіло довше за поріг — зникнення вже нічого не доводить. Не
            # видаляємо (журнал зникнень лишається повним), але й за продаж не
            # рахуємо. Лічильник потрібен, щоб у звіті було видно, скільки саме
            # відрізало: інакше тихе звуження вибірки виглядає як «ринок стих».
            too_old[0] += 1
            continue
        wname = str(rec.get("watch"))
        w = watches.get(wname)
        if w is None:
            continue
        title = str(rec.get("title") or "")
        # `an: false` ставить шар правил (rules.py) — комплекти й оголошення
        # без ціни. Вони варті повідомлення, але не медіани.
        if rec.get("an") is False:
            continue
        if not _usable(title, rec.get("price"), rec.get("cur"), w, opt,
                       watch_name=wname, catalog=catalog):
            continue
        name, from_catalog = classify(title, wname, w)
        offer("sold", str(rec.get("id") or rec.get("url") or title), name, from_catalog,
              {"price": float(rec["price"]), "days": days,
               "title": title, "url": rec.get("url")})

    # ---- довгожителі
    fresh_after = now - timedelta(hours=float(opt["stalled_seen_hours"]))
    for wname, ws in (state.get("watches") or {}).items():
        w = watches.get(wname)
        if w is None:
            continue
        for ad_id, rec in (ws.get("ads") or {}).items():
            seen = _dt(rec.get("seen"))
            if seen is None or seen < fresh_after:
                continue                  # у видачі його вже нема — це не «лежить»
            age = _age_days(rec, now)
            if age is None or age < float(opt["stalled_days"]):
                continue
            title = str(rec.get("title") or "")
            if rec.get("an") is False:
                continue
            if not _usable(title, rec.get("price"), rec.get("cur"), w, opt,
                           watch_name=wname, catalog=catalog):
                continue
            name, from_catalog = classify(title, wname, w)
            offer("stalled", str(ad_id), name, from_catalog,
                  {"price": float(rec["price"]), "days": age,
                   "title": title, "url": rec.get("url")})

    if stats is not None:
        stats["too_old"] = too_old[0]

    books: dict[str, dict[str, list]] = {}
    for (pool, _ad_id), (name, _cat, row) in picked.items():
        books.setdefault(name, {"sold": [], "stalled": []})[pool].append(row)
    return books


def cheapest_now(cfg: dict[str, Any], state: dict[str, Any], *,
                 now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Найдешевше на кожну книжку — лише переможець. Див. `cheapest_ranked`."""
    return {k: v[0] for k, v in cheapest_ranked(cfg, state, now=now).items() if v}


def cheapest_ranked(cfg: dict[str, Any], state: dict[str, Any], *,
                    now: datetime | None = None, limit: int = 5,
                    include_missing: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Найдешевші ЖИВІ оголошення на кожну книжку: {ключ книги: [{price,url,…}, …]}.

    Навіщо. Саме по собі «нове оголошення за 300 грн» нічого не каже: може, це
    найдешевше на ринку, а може, поруч лежить таке саме за 180. Цей індекс
    дозволяє підписати кожне сповіщення поточним мінімумом і посиланням на
    нього.

    Рахується зі стану, **без жодного запиту в OLX** — тому не залежить від
    того, чи вдався конкретний прогін, і не коштує нічого.

    Що не бере:
    - оголошення, книжку яких не впізнав каталог `books:` — див. нижче;
    - оголошення з Букфлі: там інший (і менший) ринок;
    - оголошення, позначені `miss`, тобто ті, яких не було в останній видачі —
      див. нижче, це найважливіший фільтр тут. `include_missing=True` лишає їх
      у списку з прапорцем `miss`: це для того, хто вміє перевірити посилання
      наживо і сам вирішить. Без перевірки їм тут не місце;
    - оголошення, яких давно не бачили у видачі (`stalled_seen_hours`): мертве
      посилання гірше за відсутність рядка;
    - усе, що шар правил позначив `an: false` — комплекти й лоти. Порівнювати
      ціну однієї книжки з ціною набору безглуздо;
    - російськомовні й гуртові лоти — тим самим фільтром, що й статистика.

    Повертає СПИСОК на книжку, а не переможця, і саме тому: посилання може
    померти між прогоном і відправкою, і тоді потрібен наступний за ціною, а не
    порожній рядок. Хто хоче перевірити його наживо — `main.CheapestNow`.
    """
    opt = _opts(cfg)
    watches = _watch_index(cfg)
    catalog = cfg.get("books") or []
    now = now or datetime.now(timezone.utc)
    fresh_after = now - timedelta(hours=float(opt["stalled_seen_hours"]))

    pool: dict[str, list[dict[str, Any]]] = {}
    for wname, ws in (state.get("watches") or {}).items():
        w = watches.get(wname)
        if w is None:
            continue
        # Тільки OLX. Букфлі — інший ринок і менший: дешевше за OLX там наразі
        # не буває, тож порівнювати варто з OLX в обидві сторони. Заразом це
        # прибирає посилання на картку Букфлі під оголошенням Букфлі.
        if str(w.get("source", "olx")).lower() != "olx":
            continue
        for ad_id, rec in (ws.get("ads") or {}).items():
            if rec.get("an") is False:
                continue
            # 🔑 `miss` ставить сам прогін тому оголошенню, якого цього разу не
            # було у видачі. Це найраніший доступний сигнал «воно зникло», і
            # раніше його тут не питали зовсім: рішення трималось лише на
            # `seen`, тобто зникле оголошення лишалось «найдешевшим» ще до 48
            # годин. Саме так 2026-09-30 під оголошенням за 175 грн поїхало
            # посилання на давно зняте за 150.
            #
            # ⚠ Але `miss` ≠ «знято»: оголошення могло просто з'їхати з тих
            # сторінок, які ми читаємо, — свіжі виштовхують старі. Тому воно не
            # викидається наосліп, а лише тоді, коли перевірити нема чим.
            missing = bool(rec.get("miss"))
            if missing and not include_missing:
                continue
            seen = _dt(rec.get("seen"))
            if seen is None or seen < fresh_after:
                continue
            title = str(rec.get("title") or "")
            if not _usable(title, rec.get("price"), rec.get("cur"), w, opt,
                           watch_name=wname, catalog=catalog):
                continue
            name, from_catalog = book_match(title, wname, catalog=catalog,
                                            include=w.get("include_keywords") or [],
                                            bundle_keywords=w.get("bundle_keywords") or [])
            # 🔑 Лише коли книжку впізнав КАТАЛОГ. Інакше ключем стає назва
            # watch'а, а вона об'єднує все, що той watch ловить: у «Бункері»
            # під одним ключем опинялись і Г'ю Хауї, і «Зимою в бункер», і
            # найдешевшим ставала чужа книжка. Краще не показати рядок, ніж
            # показати посилання на іншу книжку.
            if not from_catalog:
                continue
            pool.setdefault(name, []).append({
                "price": float(rec["price"]), "url": rec.get("url"),
                "title": title, "id": str(ad_id),
                # Ключ watch'а в стані — щоб той, хто перевірить посилання й
                # побачить 410, міг прибрати запис, а не лише промовчати.
                "watch": wname, "miss": missing,
            })

    return {name: sorted(rows, key=lambda r: r["price"])[:max(1, limit)]
            for name, rows in pool.items()}


# ------------------------------------------------------------------- профілі

def quantile(values: list[float], q: float) -> float | None:
    """Перцентиль без numpy (лінійна інтерполяція, як у statistics.quantiles)."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def profile(pool: dict[str, list], opt: dict[str, Any]) -> dict[str, Any]:
    """Числа, з яких складається вердикт по одній книзі.

    Найтонше місце — межа «задорого». Наївне «25-й перцентиль довгожителів»
    дає нісенітницю: у Гаррі Поттера половина дешевих оголошень висить
    тижнями просто тому, що їх там сотні, і межа виходила нижчою за ціни,
    за якими книжки демонстративно продавались («нормально до 675, але з
    248 не рухається»). Тому:

      • коли продажі Є — мертвою зоною вважаємо лише те, що лежить ВИЩЕ за
        75-й перцентиль проданих. Нижче цієї позначки лежання нічого не
        доводить: там і купують теж;
      • коли продажів НЕМА — беремо 25-й перцентиль усіх довгожителів. Це
        вже інше твердження: «навіть за стільки не беруть», і саме так воно
        й підписане у звіті.

    25-й перцентиль, а не мінімум — щоб одне забуте оголошення з абсурдною
    ціною не визначало межу для всіх.
    """
    sold = [s["price"] for s in pool["sold"]]
    stalled = [s["price"] for s in pool["stalled"]]
    days = [float(s["days"]) for s in pool["sold"] if s.get("days") is not None]

    enough_sold = len(sold) >= int(opt["min_sold"])
    enough_stalled = len(stalled) >= int(opt["min_stalled"])
    ok_max = quantile(sold, 0.75) if enough_sold else None

    dead_from = None
    dead_basis = None
    if enough_sold:
        above = [p for p in stalled if ok_max is not None and p > ok_max]
        if len(above) >= int(opt["min_stalled"]):
            dead_from = quantile(above, 0.25)
            dead_basis = "sold"
    elif enough_stalled:
        dead_from = quantile(stalled, 0.25)
        dead_basis = "stalled"

    return {
        "n_sold": len(sold),
        "n_stalled": len(stalled),
        "deal_max": quantile(sold, 0.25) if enough_sold else None,
        "median": quantile(sold, 0.5) if enough_sold else None,
        "ok_max": ok_max,
        "low": min(sold) if sold else None,
        "high": max(sold) if sold else None,
        "stalled_low": min(stalled) if stalled else None,
        "stalled_high": max(stalled) if stalled else None,
        # Найдешевше з тих, що ЛЕЖАТЬ, разом із посиланням. Саме його видно в
        # магазині просто зараз, тож це єдина ціна зі звіту, за якою можна піти
        # й купити. Медіани описують минуле, а це — теперішнє.
        "cheapest_stalled": min(pool["stalled"], key=lambda r: r["price"],
                                default=None) if pool["stalled"] else None,
        "dead_from": dead_from,
        "dead_basis": dead_basis,       # "sold" — вище за реальні продажі;
                                        # "stalled" — продажів не бачили взагалі
        "median_days": quantile(days, 0.5) if days else None,
        "has_sold": enough_sold,
        "confident": enough_sold or enough_stalled,
    }


def profiles(cfg: dict[str, Any], state: dict[str, Any], *,
             now: datetime | None = None) -> dict[str, dict[str, Any]]:
    opt = _opts(cfg)
    return {name: profile(pool, opt) for name, pool in collect(cfg, state, now=now).items()}


# ------------------------------------------------------------------ вердикти

DEAL = "🟢 Брати не думаючи"
GOOD = "🙂 Непогана ціна"
PRICEY = "🔴 Задорого"
UNKNOWN = "🤷 Не вдалося визначити"


def verdict(price: float | None, prof: dict[str, Any] | None,
            opt: dict[str, Any] | None = None) -> str:
    """Короткий коментар до конкретного оголошення.

    Порядок перевірок не випадковий: межа «задорого» береться з довгожителів
    (це прямий доказ, що за такі гроші не беруть) і має пріоритет над
    перцентилями проданих, які на малій вибірці стрибають.
    """
    opt = {**DEFAULTS, **(opt or {})}
    if price is None or not prof:
        return UNKNOWN
    dead = prof.get("dead_from")

    if prof.get("has_sold"):
        if dead is not None and price >= dead:
            return PRICEY
        if prof.get("deal_max") is not None and price <= prof["deal_max"]:
            return DEAL
        if prof.get("ok_max") is not None and price <= prof["ok_max"]:
            return GOOD
        return PRICEY                    # дорожче за 75% усього, що розійшлось

    # Продажів не бачили. Сказати «беріть» тут нема на чому — ми не знаємо
    # жодної ціни, за якою цю книжку хтось забрав. А от «задорого» сказати
    # можна: якщо за такі гроші вона лежить тижнями, це вже спостереження.
    if dead is not None and price >= dead:
        return PRICEY
    return UNKNOWN


def verdict_for_ad(ad: Any, watch_name: str, cfg: dict[str, Any],
                   prof_map: dict[str, dict[str, Any]]) -> str | None:
    """Вердикт для щойно знайденого оголошення. None — якщо аналітика вимкнена."""
    opt = _opts(cfg)
    if not opt.get("analytics_enabled", True):
        return None
    w = _watch_index(cfg).get(watch_name) or {}
    key = book_key(getattr(ad, "title", "") or "", watch_name,
                   catalog=cfg.get("books") or [],
                   include=w.get("include_keywords") or [],
                   bundle_keywords=w.get("bundle_keywords") or [])
    return verdict(getattr(ad, "price", None), prof_map.get(key), opt)


# --------------------------------------------------------------------- звіт

def _plural(n: int, one: str, few: str, many: str) -> str:
    """Українські форми: 1 позиція, 2 позиції, 5 позицій."""
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    return one if last == 1 else few if 2 <= last <= 4 else many


def _money(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f}".replace(",", " ")


def _days(v: float | None) -> str:
    if v is None:
        return ""
    return f"{round(v * 24)} год" if v < 1 else f"{v:.0f} дн."


def _link(price: float | None, url: str | None, esc=str) -> str:
    """Ціна посиланням, якщо посилання є."""
    label = _money(price)
    return f"<a href=\"{esc(url)}\">{esc(label)}</a>" if url else esc(label)


def split_messages(head: list[str], blocks: list[str], foot: list[str], *,
                   limit: int = 3900, max_parts: int = 4) -> list[str]:
    """Пакує блоки в кілька повідомлень, бо Telegram ріже на 4096 символів.

    Раніше зайве просто викидалось (`body.pop()` у циклі), і звіт приїжджав
    обрізаним на півслові — половина книжок не доходила взагалі. Різати текст
    наосліп теж не можна: HTML-тег, розірваний навпіл, ламає все повідомлення,
    тому межа проходить лише між цілими блоками.

    Блок, довший за ліміт сам по собі, їде окремим повідомленням як є: це вже
    не питання пакування, і мовчки його загубити було б гірше.
    """
    parts: list[list[str]] = [[]]
    size = len("\n".join(head)) + len("\n".join(foot))
    for block in blocks:
        cost = len(block) + 1
        if parts[-1] and size + cost > limit:
            if len(parts) >= max_parts:
                parts[-1].append(f"\n…і ще {len(blocks) - sum(len(p) for p in parts)} "
                                 f"позицій не влізло.")
                break
            parts.append([])
            size = len("\n".join(head)) + len("\n".join(foot))
        parts[-1].append(block)
        size += cost

    parts = [p for p in parts if p]
    if not parts:
        return []
    out = []
    for i, chunk in enumerate(parts, 1):
        mark = f" ({i}/{len(parts)})" if len(parts) > 1 else ""
        first = [head[0] + mark] + head[1:] if head else []
        # Підпис і хвіст — лише в останньому: повторювати легенду в кожному
        # шматку означає з'їдати місце тим самим текстом.
        tail = foot if i == len(parts) else []
        out.append("\n".join(first + chunk + tail))
    return out


def build_report(cfg: dict[str, Any], state: dict[str, Any], *,
                 now: datetime | None = None, esc=str) -> list[str]:
    """Денний звіт для Telegram (HTML) — список повідомлень.

    Список, а не рядок: звіт переріс 4096 символів, і стара поведінка
    «викинути хвіст» мовчки з'їдала половину книжок.
    """
    now = now or datetime.now(timezone.utc)
    opt = _opts(cfg)
    stats: dict[str, Any] = {}
    pools = collect(cfg, state, now=now, stats=stats)
    rows = [(name, profile(pool, opt)) for name, pool in pools.items()]

    # Два блоки, бо це два різні за силою твердження, і мішати їх в один
    # список означає видавати здогад за висновок.
    priced = sorted([r for r in rows if r[1]["has_sold"]],
                    key=lambda r: (-r[1]["n_sold"], r[0]))
    stuck = sorted([r for r in rows if not r[1]["has_sold"] and r[1]["dead_from"] is not None],
                   key=lambda r: (-r[1]["n_stalled"], r[0]))
    thin = [r for r in rows
            if not r[1]["has_sold"] and r[1]["dead_from"] is None
            and (r[1]["n_sold"] or r[1]["n_stalled"])]

    total_sold = sum(r[1]["n_sold"] for r in rows)
    total_stalled = sum(r[1]["n_stalled"] for r in rows)

    head = [
        f"📊 <b>Скільки за що дають</b> · {now.strftime('%d.%m')}",
        f"За {int(opt['analytics_window_days'])} дн. зникло <b>{total_sold}</b> оголошень · "
        f"висять понад {int(opt['stalled_days'])} дн. <b>{total_stalled}</b>",
    ]
    # Скільки зникнень не пішло в рахунок продажів. Без цього рядка звуження
    # вибірки виглядає як «ринок стих», а насправді це наш власний поріг.
    if stats.get("too_old"):
        head.append(f"<i>ще {stats['too_old']} зникло після "
                    f"{int(opt['sold_max_age_days'])} дн. — за продаж не рахую</i>")
    # Нема висновків — нема повідомлення. Щоденне «статистика ще набирається»
    # нічого не додає до того, що вже каже пульс, а привчає гортати звіт не
    # читаючи — і справжній звіт потім поїде туди ж.
    if not priced and not stuck:
        return []

    body: list[str] = []
    if priced:
        body.append("\n<b>━━ Розходяться ━━</b>")
    for name, p in priced[:int(opt["report_max_books"])]:
        # ⚠ З `min_sold: 1` один продаж дає deal_max == ok_max, і наївне
        # форматування друкує «🟢 до 250 · 🙂 до 250» — два різні значки на
        # одне й те саме число. Це виглядає як поламаний звіт і, гірше, вдає
        # точність, якої в одному спостереженні нема. Тому одна ціна — один
        # рядок, і сказано прямо, що її саме взяли.
        same = p["deal_max"] == p["ok_max"]
        bits = ([f"🟢 взяли за {_money(p['deal_max'])}"] if same else
                [f"🟢 до {_money(p['deal_max'])}", f"🙂 до {_money(p['ok_max'])}"])
        if p["dead_from"] is not None:
            bits.append(f"🔴 від {_money(p['dead_from'])}")

        # Медіани кажуть «скільки приблизно», але не кажуть розкиду: «зникали
        # за 19 год» однаково описує і ринок 150–180, і ринок 150–900, а це
        # два різні рішення. Тому діапазон реальних продажів — окремим рядком.
        tail = [f"{p['n_sold']} шт."]
        if not same and p["low"] is not None and p["low"] != p["high"]:
            tail.append(f"брали {_money(p['low'])}–{_money(p['high'])}")
        if p["median_days"] is not None:
            tail.append(f"{'зникло' if p['n_sold'] == 1 else 'зникали'} за "
                        f"{_days(p['median_days'])}")
        block = f"\n<b>{esc(name)}</b>\n{' · '.join(bits)}\n<i>{esc(' · '.join(tail))}</i>"

        # Єдина ціна у звіті, за якою можна піти й купити просто зараз.
        cheap = p["cheapest_stalled"]
        if cheap:
            block += (f"\n🔻 {p['n_stalled']} "
                      f"{_plural(p['n_stalled'], 'лежить', 'лежать', 'лежать')}, "
                      f"найдешевше {_link(cheap['price'], cheap.get('url'), esc)}")
        body.append(block)

    if stuck:
        body.append("\n\n<b>━━ Не рухається зовсім ━━</b>")
        body.append("<i>за час спостережень не зникло жодного; стоп-ціна — з якої точно ні</i>")
        for name, p in stuck[:int(opt["report_max_books"])]:
            line = (f"🔴 <b>{esc(name)}</b> — {p['n_stalled']} "
                    f"{_plural(p['n_stalled'], 'лежить', 'лежать', 'лежать')}, "
                    f"стоп-ціна {_money(p['dead_from'])}")
            cheap = p["cheapest_stalled"]
            if cheap:
                line += f", найдешевше {_link(cheap['price'], cheap.get('url'), esc)}"
            body.append(line)

    foot: list[str] = []
    extra = max(len(priced) - int(opt["report_max_books"]), 0) + \
            max(len(stuck) - int(opt["report_max_books"]), 0)
    if extra:
        foot.append(f"\n…і ще {extra} {_plural(extra, 'позиція', 'позиції', 'позицій')} "
                    f"не влізло в повідомлення.")
    if thin:
        names = ", ".join(sorted(n for n, _ in thin))
        foot.append(f"\n🤷 Мало даних: {esc(names[:400])}"
                    + ("…" if len(names) > 400 else ""))
    foot.append("\n🟢 беріть · 🙂 нормально · 🔴 за ці гроші лежить")

    return split_messages(head, body, foot,
                          max_parts=int(opt.get("report_max_messages", 4)))


# ------------------------------------------------------------------ розклад

def report_due(state: dict[str, Any], opt: dict[str, Any], *,
               now: datetime | None = None) -> bool:
    """Чи час слати денний звіт: настала потрібна година і сьогодні ще не слали."""
    hour = opt.get("daily_report_hour", DEFAULTS["daily_report_hour"])
    if hour is None or hour is False:
        return False
    now = now or datetime.now(timezone.utc)
    local = _to_local(now, str(opt.get("daily_report_tz", DEFAULTS["daily_report_tz"])))
    if local.hour < int(hour):
        return False
    return state.get("book_report_date") != local.date().isoformat()


def mark_reported(state: dict[str, Any], opt: dict[str, Any], *,
                  now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    local = _to_local(now, str(opt.get("daily_report_tz", DEFAULTS["daily_report_tz"])))
    state["book_report_date"] = local.date().isoformat()


def _to_local(now: datetime, tz: str) -> datetime:
    """Київський час. Без tzdata (буває на голому Windows) — падаємо на UTC+3."""
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(tz))
    except Exception:  # noqa: BLE001
        return now.astimezone(timezone(timedelta(hours=3)))
