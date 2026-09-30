"""«Найдешевше зараз» — індекс під кожним сповіщенням.

Обидва тести написані з живих помилок, які користувач приніс 2026-09-25.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analytics  # noqa: E402

NOW = datetime.now(timezone.utc).isoformat()


def _cfg(**kw):
    cfg = {
        "defaults": {"currency": "UAH", "analytics_min_price": 20},
        "books": [
            {"name": "Кідрук: Не озирайся і мовчи", "any": ["не озирайся"],
             "watch": ["Макс Кідрук", "Букфлі"]},
        ],
        "watches": [
            {"name": "Макс Кідрук", "url": "u", "max_price": 2000},
            {"name": "Бункер", "url": "u", "max_price": 2000,
             "include_keywords": ["бункер"]},
            {"name": "Букфлі", "source": "bookflea", "max_price": 2000},
        ],
    }
    cfg.update(kw)
    return cfg


def _ad(title, price, url):
    return {"title": title, "price": price, "cur": "UAH", "seen": NOW,
            "first": NOW, "url": url}


def test_one_watch_is_not_one_book():
    """Реальна помилка: під оголошенням «Бункер» Г'ю Хауї найдешевшим показали
    «Зимою в бункер» — іншу книжку, яка просто теж містить слово «бункер».
    Причина: без позиції каталогу ключем ставала назва watch'а, спільна для
    всього, що той watch ловить."""
    state = {"watches": {"Бункер": {"seeded": True, "ads": {
        "1": _ad("Бункер. Г'ю Хауї", 300, "https://olx.ua/1"),
        "2": _ad("Книга Зимою в бункер мова українська", 90, "https://olx.ua/2"),
    }}}}
    best = analytics.cheapest_now(_cfg(), state)
    assert "Бункер" not in best, "watch без каталогу не сміє давати «найдешевше»"
    assert best == {}


def test_bookflea_ad_is_compared_against_olx():
    """Друга помилка того ж дня: під оголошенням Букфлі стояло посилання на
    іншу картку Букфлі. Порівнювати треба з OLX — там ринок більший."""
    state = {"watches": {
        "Макс Кідрук": {"seeded": True, "ads": {
            "10": _ad("Макс Кідрук Не озирайся і мовчи", 180, "https://olx.ua/10"),
            "11": _ad("Макс Кідрук Не озирайся і мовчи", 260, "https://olx.ua/11"),
        }},
        "Букфлі": {"seeded": True, "ads": {
            "20": _ad("Не озирайся і мовчи", 150, "https://bookflea.co/20"),
        }},
    }}
    best = analytics.cheapest_now(_cfg(), state)
    row = best["Кідрук: Не озирайся і мовчи"]
    assert row["price"] == 180, "має перемогти найдешевше OLX, а не Букфлі"
    assert "olx.ua" in row["url"]


def test_bookflea_alone_gives_no_answer():
    """Якщо в OLX цієї книжки зараз немає — рядка просто не буде. Це чесніше,
    ніж показати картку Букфлі під оголошенням Букфлі."""
    state = {"watches": {"Букфлі": {"seeded": True, "ads": {
        "20": _ad("Не озирайся і мовчи", 150, "https://bookflea.co/20"),
    }}}}
    assert analytics.cheapest_now(_cfg(), state) == {}


# ─────────────────────────────────────────── чужі книжки з тією самою назвою

def test_book_title_alone_is_not_enough_to_be_potter():
    """Реальні хиби 2026-09-25: під «Напівкровним принцом» найдешевшим стало
    видання в'єтнамською, а під «Таємною кімнатою» — «Школа без нудьги.
    Таємна кімната». Назви книжок Поттера не унікальні."""
    import olx
    from models import Ad

    INC = ["таємна кімната", "філософський камінь", "напівкровний"]
    REQ = ["поттер", "потер", "potter", "ролін", "rowling", "hogwarts"]

    def ok(title):
        ad = Ad(id="1", title=title, url="", price=100, currency="UAH", price_text="")
        return olx.matches(ad, include=INC, require=REQ)

    assert not ok("Школа без нудьги. Таємна кімната")
    assert not ok("Сергій Сартаков. Філософський камінь(1977р)")
    assert ok("Книга \"Гаррі Поттер і таємна кімната\"")
    assert ok("Harry Potter MinaLima — Таємна кімната")
    # Помилка в імені прізвища не псує: саме так і було в «Гіррі Поттер».
    assert ok("Гіррі Поттер Напівкровний принц")


def test_foreign_language_edition_is_dropped():
    import rules
    d = rules.decide(title="Гаррі Поттер і напівкровний принц вʼєтнамською",
                     price=70, watch={"max_price": 2000, "drop_foreign": True})
    assert d.action == "skip" and "в'єтнам" in d.reason
    # Українською — навпаки, саме те, що треба.
    keep = rules.decide(title="Гаррі Поттер і напівкровний принц українською",
                        price=300, watch={"max_price": 2000, "drop_foreign": True})
    assert keep.notify


# ─────────────────────────────────────────── комплекти без назви книжки

def test_a_bundle_names_no_book_and_must_still_pass():
    """Реальний пропуск 2026-09-30: «Продам комплект книг Гаррі Поттер» за
    1100 грн. Збиральний watch існує саме заради комплектів — і саме він їх
    відкидав, бо include вимагав назву книжки, а комплект її не називає."""
    import olx
    from models import Ad

    BOOKS = ["таємна кімната", "напівкровний", "прокляте дитя"]
    BUNDLE = ["комплект", "усі частини", "7 книг"]
    REQ = ["поттер", "потер", "potter", "ролін", "rowling"]

    def ok(title, include):
        ad = Ad(id="1", title=title, url="", price=1100, currency="UAH", price_text="")
        return olx.matches(ad, include=include, require=REQ)

    assert not ok("Продам комплект книг Гаррі Поттер", BOOKS), "так було до виправлення"
    assert ok("Продам комплект книг Гаррі Поттер", BUNDLE + BOOKS)
    assert ok("Усі частини Гаррі Поттер комплект", BUNDLE + BOOKS)
    # Вимога згадати автора лишається: чужі комплекти не проходять.
    assert not ok("Комплект книг Шерлок Холмс", BUNDLE + BOOKS)


def test_precise_watches_keep_the_narrow_list():
    """У восьми точкових пошуках назва книжки вже стоїть у запиті, тож
    «комплект» там лише додав би шуму — список має лишитись вузьким."""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "watches.yaml")
                         .read_text(encoding="utf-8"))
    byname = {w["name"]: w for w in cfg["watches"]}
    collective = byname["Гаррі Поттер"]["include_keywords"]
    precise = byname["Поттер: Келих вогню"]["include_keywords"]
    bundles = byname["Поттер: комплекти"]["include_keywords"]
    assert any("комплект" in k for k in collective)
    assert not any("комплект" in k for k in precise)
    # Список комплектів не має містити жодної назви книжки — інакше окремий
    # пошук за комплектами почав би ловити ще й одиночні томи.
    assert not (set(bundles) & set(precise))
