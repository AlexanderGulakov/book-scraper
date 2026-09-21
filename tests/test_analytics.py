"""Тести аналітики цін.

Тут перевіряється не стільки арифметика, скільки рішення, на яких легко
зекономити й потім півроку дивуватись звіту: що межа «задорого» не може
опинитись нижче за ціни, за якими книжки реально розходились; що одне
оголошення, яке бачать три пошуки, — це одне спостереження, а не три; і
що без продажів модуль каже «не вдалося визначити», а не вгадує.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analytics  # noqa: E402

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


CATALOG = [
    {"name": "Кідрук: Колонія + Колапс", "watch": ["Кідрук"],
     "all": ["колоні", "колапс"], "bundle": True},
    {"name": "Кідрук: Колонія", "watch": ["Кідрук"], "all": ["колоні"]},
    {"name": "Мак-Фадден: Служниця", "watch": ["Служниця"], "all": ["служниц"],
     "not": ["секрет", "весілля"]},
]


# ------------------------------------------------------------------- фільтри

@pytest.mark.parametrize("title", [
    "Гарри Поттер и Философский камень",
    "Комплект книг Ден Браун. Код да Винчи",
    "Книга «Тайна из тайн» — Ден Браун (твердая на русском)",
])
def test_russian_editions_are_dropped(title):
    assert analytics.is_russian(title)


@pytest.mark.parametrize("title", [
    "Дім дивних дітей — Ренсом Ріґґз",
    "Служниця Фріда Мак-Фадден, тверда обкладинка",
    "Відьмак. Останнє бажання",
])
def test_ukrainian_editions_survive(title):
    assert not analytics.is_russian(title)


@pytest.mark.parametrize("title", [
    "Книги фантастика, фентезі, класика. 41 шт. Частина 3",
    "Набір книг з домашньої бібліотеки 2 - 8.09.26",
    "Сучасні романи, детективи, фентезі 100 грн/шт",
])
def test_bulk_lots_are_dropped(title):
    """Купа чужих книжок за 7300 грн не має формувати ціну на Гаррі Поттера."""
    assert analytics.is_lot(title)


@pytest.mark.parametrize("title", [
    "Книга Служниця Фріда Мак-Фадден",
    # «бібліотек» як маркер лота викидав цілу книжку зі статистики
    "Ренсом Ріґґз «Бібліотека душ»",
    "Комплект книг Ренсома Ріггза «Дім дивних дітей»",
])
def test_real_books_are_not_lots(title):
    assert not analytics.is_lot(title)


# --------------------------------------------------------------- ключ книги

def test_catalog_order_decides():
    """Конкретніша позиція стоїть вище й забирає збіг собі."""
    name, matched = analytics.book_match(
        "Колонія / Колапс (Макс Кідрук)", "Кідрук", catalog=CATALOG)
    assert (name, matched) == ("Кідрук: Колонія + Колапс", True)


def test_bundle_gets_its_own_bucket():
    """За комплект просять інші гроші, тож він не мішається з одиночкою."""
    single, _ = analytics.book_match("Колонія, Макс Кідрук", "Кідрук", catalog=CATALOG)
    bundle, _ = analytics.book_match("Комплект «Колонія» Макс Кідрук", "Кідрук",
                                     catalog=CATALOG)
    assert single == "Кідрук: Колонія"
    assert bundle == "Кідрук: Колонія · комплект"


def test_not_list_blocks_the_entry():
    name, matched = analytics.book_match("Секрет служниці", "Служниця", catalog=CATALOG)
    assert (name, matched) == ("Служниця", False)      # фолбек на назву watch'а


def test_catalog_entry_is_scoped_to_its_watch():
    name, matched = analytics.book_match("Колонія", "Гаррі Поттер", catalog=CATALOG)
    assert (name, matched) == ("Гаррі Поттер", False)


# ------------------------------------------------------------------ профілі

def pool(sold_prices=(), stalled_prices=(), days=None):
    return {
        "sold": [{"price": p, "days": days, "title": "", "url": ""} for p in sold_prices],
        "stalled": [{"price": p, "days": 20, "title": "", "url": ""} for p in stalled_prices],
    }


OPT = dict(analytics.DEFAULTS)


def test_dead_zone_never_undercuts_real_sales():
    """Головна пастка: наївний перцентиль довгожителів давав межу «задорого»
    нижчу за ціни, за якими книжки демонстративно продавались."""
    p = analytics.profile(pool(sold_prices=[300, 400, 500, 600],
                               stalled_prices=[150, 180, 200]), OPT)
    assert p["ok_max"] is not None
    assert p["dead_from"] is None          # усе, що лежить, дешевше за продані


def test_dead_zone_counts_only_what_hangs_above_the_sales():
    p = analytics.profile(pool(sold_prices=[300, 400, 500, 600],
                               stalled_prices=[150, 800, 900, 1000]), OPT)
    assert p["dead_from"] is not None
    assert p["dead_from"] > p["ok_max"]


def test_without_sales_the_basis_is_different():
    p = analytics.profile(pool(stalled_prices=[200, 300, 400, 500]), OPT)
    assert p["has_sold"] is False
    assert p["dead_basis"] == "stalled"
    assert p["dead_from"] == pytest.approx(275)


def test_thin_sample_stays_silent():
    p = analytics.profile(pool(sold_prices=[100, 200], stalled_prices=[900]), OPT)
    assert p["deal_max"] is None and p["dead_from"] is None
    assert p["confident"] is False


# ----------------------------------------------------------------- вердикти

def test_four_verdicts():
    p = analytics.profile(pool(sold_prices=[200, 300, 400, 500],
                               stalled_prices=[700, 800, 900]), OPT)
    assert analytics.verdict(210, p, OPT) == analytics.DEAL
    assert analytics.verdict(400, p, OPT) == analytics.GOOD
    assert analytics.verdict(900, p, OPT) == analytics.PRICEY
    assert analytics.verdict(300, None, OPT) == analytics.UNKNOWN


def test_cheap_ad_without_sales_is_not_a_recommendation():
    """Без жодного продажу сказати «беріть» нема на чому — тільки «задорого»."""
    p = analytics.profile(pool(stalled_prices=[500, 600, 700]), OPT)
    assert analytics.verdict(100, p, OPT) == analytics.UNKNOWN
    assert analytics.verdict(800, p, OPT) == analytics.PRICEY


def test_ad_without_price_is_unknown():
    p = analytics.profile(pool(sold_prices=[200, 300, 400, 500]), OPT)
    assert analytics.verdict(None, p, OPT) == analytics.UNKNOWN


# --------------------------------------------------------------- збір даних

def cfg_with(*watch_names, **defaults):
    return {
        "defaults": {"currency": "UAH", **defaults},
        "books": CATALOG,
        "watches": [{"name": n, "max_price": 2000} for n in watch_names],
    }


def test_same_ad_seen_by_two_watches_counts_once():
    """«Клер: Місто скла» і «Клер: за авторкою» бачать ті самі оголошення."""
    cfg = cfg_with("Кідрук", "Служниця")
    rec = {"price": 300.0, "cur": "UAH", "title": "Колонія, Макс Кідрук",
           "seen": ago(hours=1), "created": ago(days=20)}
    state = {"sold": [], "watches": {
        "Кідрук": {"ads": {"a1": rec}},
        "Служниця": {"ads": {"a1": dict(rec)}},
    }}
    books = analytics.collect(cfg, state, now=NOW)
    assert sum(len(b["stalled"]) for b in books.values()) == 1
    assert books["Кідрук: Колонія"]["stalled"][0]["price"] == 300.0


def test_vanished_ad_is_not_a_longtimer():
    """Якщо оголошення давно не бачили у видачі, воно не «лежить» — воно зникло."""
    cfg = cfg_with("Кідрук")
    state = {"sold": [], "watches": {"Кідрук": {"ads": {"a1": {
        "price": 300.0, "cur": "UAH", "title": "Колонія",
        "seen": ago(days=5), "created": ago(days=20),
    }}}}}
    assert analytics.collect(cfg, state, now=NOW) == {}


def test_young_ad_is_not_a_longtimer():
    cfg = cfg_with("Кідрук")
    state = {"sold": [], "watches": {"Кідрук": {"ads": {"a1": {
        "price": 300.0, "cur": "UAH", "title": "Колонія",
        "seen": ago(hours=1), "created": ago(days=3),
    }}}}}
    assert analytics.collect(cfg, state, now=NOW) == {}


def test_overpriced_ad_is_out_of_the_sample():
    """Оголошення дорожче за max_price watch'а нас не цікавить і як статистика."""
    cfg = cfg_with("Кідрук")
    state = {"sold": [], "watches": {"Кідрук": {"ads": {"a1": {
        "price": 45000.0, "cur": "UAH", "title": "Колонія",
        "seen": ago(hours=1), "created": ago(days=20),
    }}}}}
    assert analytics.collect(cfg, state, now=NOW) == {}


def test_long_hanging_sale_is_probably_an_archive():
    """Знято на 60-й день — це радше OLX прибрав, ніж хтось купив."""
    cfg = cfg_with("Кідрук")
    state = {"watches": {}, "sold": [
        {"id": "s1", "watch": "Кідрук", "title": "Колонія", "price": 300.0,
         "cur": "UAH", "days": 60, "gone": ago(days=1)},
        {"id": "s2", "watch": "Кідрук", "title": "Колонія", "price": 320.0,
         "cur": "UAH", "days": 2, "gone": ago(days=1)},
    ]}
    books = analytics.collect(cfg, state, now=NOW)
    assert [s["price"] for s in books["Кідрук: Колонія"]["sold"]] == [320.0]


def test_sales_outside_the_window_are_ignored():
    cfg = cfg_with("Кідрук", analytics_window_days=30)
    state = {"watches": {}, "sold": [
        {"id": "s1", "watch": "Кідрук", "title": "Колонія", "price": 300.0,
         "cur": "UAH", "days": 2, "gone": ago(days=99)},
    ]}
    assert analytics.collect(cfg, state, now=NOW) == {}


# -------------------------------------------------------------- звіт і розклад

def test_report_fits_into_one_telegram_message():
    cfg = cfg_with(*[f"Пошук {i}" for i in range(40)])
    state = {"watches": {}, "sold": [
        {"id": f"s{i}-{j}", "watch": f"Пошук {i}", "title": f"Книжка {i}" * 5,
         "price": 100.0 + j * 50, "cur": "UAH", "days": 2, "gone": ago(days=1)}
        for i in range(40) for j in range(6)
    ]}
    text = analytics.build_report(cfg, state, now=NOW)
    assert len(text) <= 4096


def test_report_stays_silent_when_there_is_nothing_to_say():
    """Щоденне «даних поки нема» — найшвидший спосіб привчити не читати звіт."""
    text = analytics.build_report(cfg_with("Кідрук"), {"watches": {}, "sold": []}, now=NOW)
    assert text == ""


NIGHT = {**analytics.DEFAULTS, "daily_report_at": "03:45"}


def kyiv(h, m=0, day=21):
    """UTC-мить, якій відповідає вказаний київський час (влітку UTC+3)."""
    return (datetime(2026, 9, day, 0, 0, tzinfo=timezone.utc)
            + timedelta(hours=h - 3, minutes=m))


def test_daily_report_fires_once_a_day():
    state: dict = {}
    assert analytics.report_due(state, NIGHT, now=kyiv(3, 52))
    analytics.mark_reported(state, NIGHT, now=kyiv(3, 52))
    assert not analytics.report_due(state, NIGHT, now=kyiv(11, 0))
    assert analytics.report_due(state, NIGHT, now=kyiv(3, 52, day=22))


def test_minutes_are_respected():
    """О 03:37 ще рано — інакше «нічний» звіт легко з'їжджав би на пів години."""
    assert not analytics.report_due({}, NIGHT, now=kyiv(3, 37))
    assert analytics.report_due({}, NIGHT, now=kyiv(3, 45))


def test_deep_night_is_still_yesterday_for_the_schedule():
    assert not analytics.report_due({}, NIGHT, now=kyiv(1, 0))


def test_old_hour_setting_still_works():
    opt = {**analytics.DEFAULTS, "daily_report_at": None, "daily_report_hour": 9}
    assert not analytics.report_due({}, opt, now=kyiv(8, 59))
    assert analytics.report_due({}, opt, now=kyiv(9, 1))


def test_daily_report_can_be_switched_off():
    off = {**analytics.DEFAULTS, "daily_report_at": False, "daily_report_hour": None}
    assert analytics.report_time(off) is None
    assert not analytics.report_due({}, off)
