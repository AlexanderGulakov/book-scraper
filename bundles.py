"""Комплект чи одна книжка — одна відповідь на всі модулі.

Питання «скільки книжок в оголошенні» виникає в трьох місцях (`olx`, `rules`,
`analytics`), і до 2026-10-01 кожне відповідало на нього власною копією однієї
й тієї ж евристики: «ключове слово watch'а трапилось у заголовку двічі — отже
книжок дві». Евристика була не просто дубльована, вона була хибна.

## Чим була погана стара евристика

`include_keywords` watch'а — це НЕ список книжок. Там лежить усе, чим
оголошення можна впізнати: вісім написань прізвища автора, назви всіх книжок
серії, англійські відповідники. Тому два збіги набирає звичайнісіньке
оголошення на одну книжку:

    «Дім дивних дітей. Ренсом Ріґґз»  → «дивних дітей» + «ріґґз» = 2
    «Гаррі Поттер і Келих Вогню (Harry Potter and the Goblet of Fire)»
                                      → українська назва + англійська = 2
    «Ярмарок нічних жахіть»           → «ярмарок» + «ярмарок нічних жахіть» = 2

Заміряно на стані за 2026-09-22: з 1208 оголошень 242 вважались комплектами,
і 120 з них — без жодного слова на кшталт «комплект», тобто суто через цей
повтор. У Ґалбрейта комплектом було 38 оголошень з 42, у Ріггза — 25 з 27.

Ціна помилки не косметична. Назва книжки в аналітиці отримує суфікс
«· комплект», тобто одна книжка розпадається на дві різні позиції з окремими
медіанами, окремими порогами 🟢/🔴 і окремим «найдешевше зараз». Саме так під
двома оголошеннями на «Дім дивних дітей», що прийшли з інтервалом у сім
хвилин, поїхали різні мінімуми (150 і 280 грн): одне оголошення згадувало
автора в заголовку, друге — ні, і вони опинились у різних книжках.

## Що робимо натомість

Рахуємо не повтори ключових слів, а **різні позиції каталогу `books:`**, які
згадані в тексті. Каталог — це і є список книжок, і автор у ньому не позиція.
Отже:

    «Дім дивних дітей. Ренсом Ріггз»            → 1 книжка  → одиночка
    «Гаррі Поттер і Келих Вогню (Goblet of Fire)» → 1 книжка  → одиночка
    «Дім дивних дітей / Бібліотека душ / Місто порожніх» → 3 → комплект

Плюс дві поправки, без яких воно теж бреше:

1. **Роздільник переліку обов'язковий** (`_ENUM`). Назва серії збігається з
   назвою першої книжки, тому «Книга Дім дивних дітей. Книга 6. Спустошення
   Диявольського акра» згадує дві позиції каталогу, лишаючись однією книжкою.
   Справжній перелік майже завжди розділений комою, скісною або «+»; серія
   поруч із томом — ні. Кома перед цифрами не рахується: «"Бот. Атакамська
   криза", 2024р.» — це рік видання, а не друга книжка.
2. **Уточнення каталогу не подвоюють книжку.** `books:` навмисне містить
   і «Кідрук: Бот», і «Кідрук: Бот. Атакамська криза» — конкретніша позиція
   стоїть вище й виграє. Для підрахунку це одна книжка, і розрізняє їх префікс
   назви.

Якщо каталог про цей текст не знає нічого (Кінг і Клер у каталозі відсутні —
там watch і так дорівнює книжці), лишається стара евристика, але полагоджена:
збіги ключових слів зливаються в **неперекривні ділянки** тексту, тож
«ярмарок» усередині «ярмарок нічних жахіть» більше не рахується другою
книжкою. І роздільник потрібен так само.

Явні слова («комплект», «усі частини», «3 книги») лишились першою і найдешевшою
перевіркою: коли продавець сказав прямо, рахувати нема чого.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

__all__ = ["BUNDLE_WORDS", "entry_matches", "matching_entries",
           "distinct_books", "looks_like_bundle"]

# Пряма вказівка в тексті. Список спільний для всіх модулів: до об'єднання
# `rules` знав «одним лотом» і «3 книги», а `analytics` — ні, і те саме
# оголошення було комплектом для сповіщення й одиночкою для статистики.
BUNDLE_WORDS: tuple[str, ...] = (
    "комплект", "набір", "набор", "збірник", "зібрання", "усі частини",
    "всі частини", "цикл", "серія книг", "семитомник", "трилогія", "томи",
    "одним лотом", "дві книги", "три книги", "чотири книги", "п'ять книг",
    "5 книг", "4 книги", "3 книги", "2 книги",
)

# Роздільник переліку. Тире НЕ входить навмисне: «Стівен Кінг - Талісман» —
# це автор і назва, а не дві книжки. Кома перед числом — теж ні: там рік,
# наклад або «2024р.».
_ENUM = re.compile(r"[+/;&|]|\s(?:та|і|й)\s|,(?!\s*\d)")

# Пам'ять у межах процесу: той самий заголовок питають і правила, і аналітика,
# і індекс найдешевших, а каталог — це 68 позицій на кожен виклик. Ключ тримає
# сам каталог, щоб id не перевикористався після збирання сміття.
_CACHE: dict[tuple[int, str, str], tuple[Any, tuple[dict[str, Any], ...]]] = {}
_CACHE_MAX = 20000


def _entry_hit(entry: dict[str, Any], t: str, watch_name: str) -> bool:
    watches = entry.get("watch")
    if watches and watch_name not in list(watches):
        return False
    all_of = [w.casefold() for w in (entry.get("all") or []) if w]
    if all_of and not all(w in t for w in all_of):
        return False
    any_of = [w.casefold() for w in (entry.get("any") or []) if w]
    if any_of and not any(w in t for w in any_of):
        return False
    none_of = [w.casefold() for w in (entry.get("not") or []) if w]
    if any(w in t for w in none_of):
        return False
    return bool(all_of or any_of)


def entry_matches(entry: dict[str, Any], title: str, watch_name: str) -> bool:
    """Чи описує позиція каталогу цей заголовок (з огляду на `watch:`)."""
    return _entry_hit(entry, (title or "").casefold(), watch_name)


def matching_entries(text: str, watch_name: str,
                     catalog: Iterable[dict[str, Any]] = ()) -> tuple[dict[str, Any], ...]:
    """Усі позиції каталогу, які згадані в тексті, у порядку файлу.

    Порядок збережено, бо на ньому тримається `analytics.book_match`: перший
    збіг виграє, тож конкретніші позиції стоять у `watches.yaml` вище.
    """
    if not catalog:
        return ()
    if not isinstance(catalog, Sequence):
        catalog = list(catalog)
    key = (id(catalog), watch_name or "", text or "")
    hit = _CACHE.get(key)
    if hit is not None and hit[0] is catalog:
        return hit[1]
    t = (text or "").casefold()
    out = tuple(e for e in catalog if _entry_hit(e, t, watch_name or ""))
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (catalog, out)
    return out


def distinct_books(entries: Iterable[dict[str, Any]]) -> int:
    """Скільки РІЗНИХ книжок серед позицій, що збіглися.

    Уточнення каталогу («Кідрук: Бот» і «Кідрук: Бот. Атакамська криза»)
    описують одну книжку, і впізнаються за префіксом назви на межі слова.
    """
    names = sorted({str(e.get("name") or "") for e in entries if e.get("name")},
                   key=len)
    kept: list[str] = []
    for n in names:
        if any(n.startswith(k) and (len(n) == len(k) or not n[len(k)].isalnum())
               for k in kept):
            continue
        kept.append(n)
    return len(kept)


def _regions(t: str, include: Iterable[str]) -> int:
    """Скільки неперекривних ділянок тексту закривають ключові слова.

    Саме неперекривних: «ярмарок» лежить усередині «ярмарок нічних жахіть», і
    стара сума `count()` бачила тут дві книжки замість одної.
    """
    spans: list[tuple[int, int]] = []
    for w in include:
        w = (w or "").casefold()
        if not w:
            continue
        i = t.find(w)
        while i >= 0:
            spans.append((i, i + len(w)))
            i = t.find(w, i + 1)
    if not spans:
        return 0
    spans.sort()
    n, end = 0, -1
    for s, e in spans:
        if s >= end:
            n += 1
            end = e
        else:
            end = max(end, e)
    return n


def looks_like_bundle(text: str, watch_name: str = "", *,
                      catalog: Iterable[dict[str, Any]] = (),
                      include: Iterable[str] = (),
                      bundle_keywords: Iterable[str] = (),
                      min_repeats: int = 2) -> bool:
    """Чи схоже, що в оголошенні кілька книжок. Єдина відповідь на всі модулі."""
    t = (text or "").casefold()
    if not t:
        return False

    # 1. Продавець сказав прямо — рахувати нема чого.
    if any(w.casefold() in t for w in (bundle_keywords or BUNDLE_WORDS) if w):
        return True

    # 2. Каталог: позиція, яка сама по собі про комплект («Ріггз: Дім дивних
    #    дітей · цикл»), вирішує питання без жодних роздільників.
    entries = matching_entries(text, watch_name, catalog)
    if any(e.get("bundle") for e in entries):
        return True

    n = distinct_books(entries) if entries else _regions(t, include)
    return n >= min_repeats and bool(_ENUM.search(t))
