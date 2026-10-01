"""Комплект чи одна книжка.

Привід (2026-10-01): під двома оголошеннями на «Дім дивних дітей», що прийшли
з різницею в сім хвилин, поїхали різні «найдешевше зараз» — 150 і 280 грн.
Виявилось, що книжка була не одна: заголовок з автором («Дім дивних дітей.
Ренсом Ріґґз») набирав два збіги `include_keywords` і ставав «· комплект»,
а заголовок без автора лишався одиночкою. Дві різні позиції статистики, два
різні мінімуми, два різні пороги 🟢/🔴.

Тести перевіряють не арифметику, а саме це рішення: що книжку рахує каталог,
а не повтор ключового слова, і що перелік книжок має виглядати як перелік.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analytics          # noqa: E402
import bundles            # noqa: E402

RIGGS_WATCHES = ["Ренсом Ріггз (автор)", "Ріггз: Дім дивних дітей (за серією)"]

CATALOG = [
    {"name": "Ріггз: Дім дивних дітей · цикл", "watch": RIGGS_WATCHES,
     "all": ["дивних"], "any": ["комплект", "цикл", "серія", "серії"], "bundle": True},
    {"name": "Ріггз: Місто порожніх", "watch": RIGGS_WATCHES, "all": ["місто порожніх"]},
    {"name": "Ріггз: Бібліотека душ", "watch": RIGGS_WATCHES, "all": ["бібліотека душ"]},
    {"name": "Ріггз: Спустошення Диявольського акра", "watch": RIGGS_WATCHES,
     "any": ["спустошення", "диявольського акра", "диявольского акра"]},
    {"name": "Ріггз: Дім дивних дітей", "watch": RIGGS_WATCHES,
     "any": ["дім дивних", "дому дивних", "будинок дивних"]},
    # Уточнення тієї самої книжки: конкретніша позиція стоїть вище й виграє.
    {"name": "Кідрук: Бот. Атакамська криза", "watch": ["Макс Кідрук"], "all": ["атакамськ"]},
    {"name": "Кідрук: Бот", "watch": ["Макс Кідрук"], "all": ["бот"], "not": ["робот"]},
]

# Те, що реально лежить у watch'і: вісім написань прізвища ПЛЮС назви книжок.
# Саме через цю суміш і ламалась стара евристика «два збіги = дві книжки».
RIGGS_INCLUDE = ["ріггз", "ріґґз", "рігз", "riggs", "ренсом", "дивних дітей",
                 "peculiar children", "місто порожніх", "бібліотека душ",
                 "спустошення диявольськ", "диявольського акра"]


def _bundle(title: str, watch: str = RIGGS_WATCHES[0], include=None) -> bool:
    return bundles.looks_like_bundle(title, watch, catalog=CATALOG,
                                     include=RIGGS_INCLUDE if include is None else include)


def _key(title: str, watch: str = RIGGS_WATCHES[0]) -> str:
    return analytics.book_key(title, watch, catalog=CATALOG, include=RIGGS_INCLUDE)


# ---------------------------------------------------------------- головне

def test_author_in_the_title_does_not_make_a_bundle():
    """Той самий баг, з якого все почалось: автор — не друга книжка."""
    assert not _bundle("Дім дивних дітей. Ренсом Ріґґз")
    assert not _bundle("Ренсом Ріггз - Дім дивних дітей")


def test_the_same_book_gets_the_same_key_with_or_without_the_author():
    """Підпис продавця не сміє розводити одну книжку по двох позиціях.

    Саме через це під одним оголошенням стояло «найдешевше 150», а під
    сусіднім, на ту саму книжку, — «280».
    """
    assert _key("Дім дивних дітей. Ренсом Ріґґз") == "Ріггз: Дім дивних дітей"
    assert _key("Дім дивних дітей", RIGGS_WATCHES[1]) == "Ріггз: Дім дивних дітей"


def test_a_bilingual_title_is_one_book():
    """Українська й англійська назви — це одна книжка, а не дві.

    Рятує саме каталог: обидві назви веде одна позиція. Без каталогу (там, де
    watch і так дорівнює книжці) двомовний заголовок лишається слабким місцем.
    """
    catalog = [{"name": "Поттер: Келих вогню", "watch": ["Гаррі Поттер"],
                "any": ["келих вогню", "goblet of fire"]}]
    include = ["гаррі поттер", "harry potter", "келих вогню", "goblet of fire"]
    assert not bundles.looks_like_bundle(
        "Гаррі Поттер і Келих Вогню (Harry Potter and the Goblet of Fire)",
        "Гаррі Поттер", catalog=catalog, include=include)


def test_a_keyword_inside_a_longer_keyword_counts_once():
    """«ярмарок» лежить усередині «ярмарок нічних жахіть» — ділянка одна."""
    include = ["ярмарок", "ярмарок нічних жахіть", "кінг"]
    assert not bundles.looks_like_bundle("Ярмарок нічних жахіть",
                                         "Кінг: Ярмарок нічних жахіть", include=include)
    assert not bundles.looks_like_bundle("Стівен Кінг Ярмарок нічних жахіть. Ксд",
                                         "Кінг: Ярмарок нічних жахіть", include=include)


def test_the_series_name_next_to_a_volume_is_not_a_bundle():
    """Назва серії збігається з назвою першої книжки — і це не дві книжки."""
    title = "Книга Дім дивних дітей. Книга 6. Спустошення Диявольского Акра Ренсом Ріггз"
    assert not _bundle(title)
    assert _key(title) == "Ріггз: Спустошення Диявольського акра"


def test_a_catalog_refinement_does_not_double_the_book():
    """«Кідрук: Бот» і «Кідрук: Бот. Атакамська криза» — одна книжка."""
    assert not bundles.looks_like_bundle(
        'Макс Кідрук "Бот. Атакамська криза" / тверда обкладинка',
        "Макс Кідрук", catalog=CATALOG, include=["кідрук", "бот", "атакамськ"])


def test_a_comma_before_a_number_is_not_an_enumeration():
    """«", 2024р."» — це рік видання, а не друга книжка."""
    assert not bundles.looks_like_bundle(
        'Макс Кідрук "Бот. Атакамська криза", 2024р.',
        "Макс Кідрук", catalog=CATALOG, include=["кідрук", "бот", "атакамськ"])


# ---------------------------------------------------- справжні комплекти

def test_a_real_list_of_books_is_still_a_bundle():
    assert _bundle("Ренсом Ріггз — Дім дивних дітей / Бібліотека душ / Місто порожніх")
    assert _bundle('Книги "Дім дивних дітей" + "Місто порожніх" (Ренсом Ріггз)')


def test_an_explicit_word_wins_without_any_separator():
    assert _bundle("Комплект Ренсом Ріггз")
    assert bundles.looks_like_bundle("продам усі три книги однією посилкою", "Макс Кідрук")


def test_a_catalog_entry_about_a_set_wins_on_its_own():
    """Позиція з `bundle: true` не потребує ні роздільників, ні підрахунку."""
    assert _bundle("Книги з серії дім дивних дітей Ренсом Рігз")
    assert _key("Книги з серії дім дивних дітей Ренсом Рігз") == "Ріггз: Дім дивних дітей · цикл"


# -------------------------------------------------------------- дрібниці

def test_without_a_catalog_the_old_heuristic_still_works():
    """Кінг і Клер у каталозі відсутні — там рахуються ділянки тексту."""
    include = ["ярмарок нічних жахіть", "чорний дім", "про письменство", "піднесення"]
    assert bundles.looks_like_bundle(
        "Ярмарок нічних жахіть/чорний дім/про письменство/піднесення стівен кінг",
        "Кінг: Відродження", include=include)


def test_the_cache_does_not_confuse_two_catalogs():
    other = [{"name": "Інша книжка", "watch": RIGGS_WATCHES, "any": ["дім дивних"]}]
    assert analytics.book_key("Дім дивних дітей", RIGGS_WATCHES[0],
                              catalog=CATALOG) == "Ріггз: Дім дивних дітей"
    assert analytics.book_key("Дім дивних дітей", RIGGS_WATCHES[0],
                              catalog=other) == "Інша книжка"


def test_an_entry_limited_to_other_watches_does_not_match():
    assert analytics.book_key("Дім дивних дітей", "Букфлі",
                              catalog=CATALOG) == "Букфлі"
