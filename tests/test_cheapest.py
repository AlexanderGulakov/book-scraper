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


def _ad(title, price, url, **kw):
    rec = {"title": title, "price": price, "cur": "UAH", "seen": NOW,
           "first": NOW, "url": url}
    rec.update(kw)
    return rec


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


# ─────────────────────────────────────────── посилання, яке вже не відкривається

def test_an_ad_missing_from_the_feed_is_not_the_cheapest():
    """Реальна хиба 2026-09-30: під новим оголошенням за 175 грн поїхало
    «найдешевше: 150» на оголошення, знятe ще до відправки повідомлення.

    Прапорець `miss` ставить сам прогін тому, чого не було у видачі, — тобто
    сигнал був, його просто не питали. Тут перевіряємо, що тепер питають.
    """
    state = {"watches": {"Макс Кідрук": {"seeded": True, "ads": {
        "1": _ad("Кідрук Не озирайся і мовчи", 150, "https://olx.ua/1", miss=NOW),
        "2": _ad("Кідрук Не озирайся і мовчи", 175, "https://olx.ua/2"),
    }}}}
    best = analytics.cheapest_now(_cfg(), state)
    assert best["Кідрук: Не озирайся і мовчи"]["price"] == 175
    # І воно не просто зникло з переможців, а взагалі не в черзі:
    ranked = analytics.cheapest_ranked(_cfg(), state)
    assert [r["id"] for r in ranked["Кідрук: Не озирайся і мовчи"]] == ["2"]


def test_ranked_keeps_the_runner_up_and_knows_its_watch():
    """Один переможець не рятує: якщо він помер, потрібен наступний за ціною,
    а щоб його прибрати зі стану — ще й ключ watch'а."""
    state = {"watches": {"Макс Кідрук": {"seeded": True, "ads": {
        "1": _ad("Кідрук Не озирайся і мовчи", 150, "https://olx.ua/1"),
        "2": _ad("Кідрук Не озирайся і мовчи", 175, "https://olx.ua/2"),
    }}}}
    rows = analytics.cheapest_ranked(_cfg(), state)["Кідрук: Не озирайся і мовчи"]
    assert [r["price"] for r in rows] == [150, 175]
    assert rows[0]["watch"] == "Макс Кідрук"


def test_a_dead_link_is_replaced_and_buried(monkeypatch):
    """Перевірка наживо: 410 на найдешевшому не має давати порожній рядок —
    має давати наступне за ціною. А сам мрець їде в історію проданих, бо це
    той самий висновок, що й у звіті про зняті, тільки раніше."""
    import main, olx
    from models import Ad

    state = {"watches": {"Макс Кідрук": {"seeded": True, "ads": {
        "1": _ad("Кідрук Не озирайся і мовчи", 150, "https://olx.ua/1"),
        "2": _ad("Кідрук Не озирайся і мовчи", 175, "https://olx.ua/2"),
    }}}}
    monkeypatch.setattr(olx, "ad_state",
                        lambda s, url, **kw: "gone" if url.endswith("/1") else "alive")

    cheap = main.CheapestNow(_cfg(), state, session=object())
    ad = Ad(id="99", title="Кідрук Не озирайся і мовчи", url="https://olx.ua/99",
            price=300, currency="UAH", price_text="300 грн.")
    row = cheap.for_ad("Макс Кідрук", ad)

    assert row["price"] == 175, "мертвого треба замінити, а не промовчати"
    assert "1" not in state["watches"]["Макс Кідрук"]["ads"], "мерця прибрано зі стану"
    assert [s["id"] for s in state["sold"]] == ["1"], "і записано в історію"
    assert cheap.buried == 1


def test_a_network_hiccup_does_not_hide_the_cheapest(monkeypatch):
    """`unknown` — це «не вдалось спитати», а не «немає». Мовчати через
    тайм-аут гірше, ніж показати рядок: ту саму помилку вже робив
    fetch_description, поки не навчився розрізняти ці випадки."""
    import main, olx
    from models import Ad

    state = {"watches": {"Макс Кідрук": {"seeded": True, "ads": {
        "1": _ad("Кідрук Не озирайся і мовчи", 150, "https://olx.ua/1"),
    }}}}
    monkeypatch.setattr(olx, "ad_state", lambda s, url, **kw: "unknown")

    cheap = main.CheapestNow(_cfg(), state, session=object())
    ad = Ad(id="99", title="Кідрук Не озирайся і мовчи", url="", price=300,
            currency="UAH", price_text="")
    assert cheap.for_ad("Макс Кідрук", ad)["price"] == 150
    assert cheap.buried == 0


def test_verification_has_a_budget(monkeypatch):
    """Стеля потрібна, бо інакше поганий день на OLX (усе знято) перетворює
    кілька повідомлень на десятки запитів посеред прогону."""
    import main, olx
    from models import Ad

    ads = {str(i): _ad("Кідрук Не озирайся і мовчи", 100 + i, f"https://olx.ua/{i}")
           for i in range(6)}
    state = {"watches": {"Макс Кідрук": {"seeded": True, "ads": ads}}}
    calls = []

    def dead(s, url, **kw):
        calls.append(url)
        return "gone"

    monkeypatch.setattr(olx, "ad_state", dead)
    cheap = main.CheapestNow(_cfg(), state, session=object(), budget=2, depth=5)
    ad = Ad(id="99", title="Кідрук Не озирайся і мовчи", url="", price=300,
            currency="UAH", price_text="")
    cheap.for_ad("Макс Кідрук", ad)
    assert len(calls) == 2, "після вичерпання бюджету кандидат вважається живим"


def test_a_missing_ad_gets_a_second_chance_when_we_can_check_it(monkeypatch):
    """`miss` ≠ «знято»: старе оголошення просто з'їжджає зі сторінок, які ми
    читаємо. Якщо є чим перевірити — перевіряємо, і живе повертається в гру.
    Інакше вийшло б навпаки: чесний мінімум ринку ховали б від очей."""
    import main, olx
    from models import Ad

    state = {"watches": {"Макс Кідрук": {"seeded": True, "ads": {
        "1": _ad("Кідрук Не озирайся і мовчи", 150, "https://olx.ua/1", miss=NOW),
        "2": _ad("Кідрук Не озирайся і мовчи", 175, "https://olx.ua/2"),
    }}}}
    monkeypatch.setattr(olx, "ad_state", lambda s, url, **kw: "alive")

    ad = Ad(id="99", title="Кідрук Не озирайся і мовчи", url="", price=300,
            currency="UAH", price_text="")

    live = main.CheapestNow(_cfg(), state, session=object())
    assert live.for_ad("Макс Кідрук", ad)["price"] == 150, "живе — значить показуємо"

    # А з вимкненою перевіркою — обережність: зниклий у чергу не потрапляє.
    blind = main.CheapestNow(_cfg(), state, session=object(), verify=False)
    assert blind.for_ad("Макс Кідрук", ad)["price"] == 175
