"""Рядок «створено N хвилин тому» в повідомленні про знахідку.

Сенс рядка: за заміром 2026-10-07 (`claude/notification-latency.md`) індекс
OLX віддає оголошення з запізненням 5-10 хвилин. Поки цифра не перед очима,
«бот повільний» і «OLX ще не проіндексував» виглядають однаково, і крайнім
щоразу виявляється бот.

Головна пастка, на яку тут більшість тестів: **`created_time` ≠ дата
публікації**. Це дата першого створення оголошення, і у видачі трапляються
2016-й та 2019-й роки — лот підняли сьогодні, а створили давно. Тому
«створено» показуємо лише тоді, коли воно збігається з підняттям.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import notify


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone(timedelta(hours=3)))


class FakeAd:
    def __init__(self, created=None, refreshed=None):
        self.created_time = created
        self.refreshed_time = refreshed


def iso(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


def age(created=None, refreshed=None) -> str:
    return notify.format_age(FakeAd(created, refreshed), now=NOW)


# --- справді нове оголошення ----------------------------------------------

def test_fresh_ad_shows_minutes():
    # Підняття через хвилину після створення — так OLX робить сам,
    # це те саме оголошення, а не повторний продаж.
    assert age(iso(minutes=7), iso(minutes=6)) == "🕒 створено 7 хв тому"


def test_under_a_minute_is_just_now():
    assert age(iso(seconds=20), iso(seconds=20)) == "🕒 створено щойно"


def test_clock_skew_does_not_produce_negative_age():
    """Годинник OLX буває трохи попереду нашого. «створено -2 хв тому» —
    це баг, який одразу помітить користувач, а не ми."""
    future = (NOW + timedelta(minutes=2)).isoformat()
    assert age(future, future) == "🕒 створено щойно"


def test_hours_after_an_hour():
    assert age(iso(hours=5), iso(hours=5)) == "🕒 створено 5 год тому"


def test_date_after_a_day():
    old = iso(days=3)
    assert age(old, old).startswith("🕒 створено 04.10.2026")


# --- підняте старе оголошення ---------------------------------------------

def test_bumped_old_ad_shows_both():
    """Саме цей випадок ламав би наївну версію: «створено 2 роки тому» на
    оголошенні, яке щойно виринуло у видачі."""
    out = age(created="2024-03-14T10:00:00+02:00", refreshed=iso(minutes=6))
    assert out == "🕒 піднято 6 хв тому · створено 14.03.2024"


def test_olx_own_refresh_right_after_creation_is_not_a_bump():
    """Живий випадок 2026-10-07: створено 21:53:11, підняття 21:56:43.

    OLX сам торкається оголошення через кілька хвилин після публікації. З
    порогом у 2 хвилини (так було спершу) справді нове оголошення підписувалось
    як «піднято 17 год тому · створено 17 год тому» — рядок ні про що.
    """
    assert age(iso(minutes=9), iso(minutes=5)) == "🕒 створено 9 хв тому"


def test_real_bump_is_shown_as_bump():
    assert "піднято" in age(iso(days=40), iso(minutes=5))


def test_identical_looking_dates_collapse():
    """Різні позначки часу, однаковий текст — показуємо один раз."""
    assert age(iso(hours=17, minutes=30), iso(hours=17)) == "🕒 створено 17 год тому"


# --- чого бракує -----------------------------------------------------------

def test_only_refresh_known():
    assert age(refreshed=iso(minutes=4)) == "🕒 піднято 4 хв тому"


def test_no_dates_no_line():
    """Букфлі дат не віддає — рядок просто не з'являється, а не «None»."""
    assert age() == ""


def test_broken_date_is_ignored_not_raised():
    """Повідомлення про знахідку важливіше за рядок із датою."""
    assert age("не дата", "теж не дата") == ""
    assert age("не дата", iso(minutes=3)) == "🕒 піднято 3 хв тому"


def test_naive_timestamp_does_not_crash():
    """На випадок джерела без таймзони — не падаємо з
    `can't subtract offset-naive and offset-aware datetimes`."""
    assert age("2026-10-07T11:50:00", "2026-10-07T11:50:00") != ""


# --- місце в повідомленні --------------------------------------------------

def test_line_lands_in_the_event_message():
    # Тут час справжній: `format_event` рахує вік від «зараз», і саме цю
    # зв'язку — що він узагалі викликає `format_age` — тест і стереже.
    real = datetime.now(timezone.utc) - timedelta(minutes=9)
    ad = FakeAd(real.isoformat(), real.isoformat())
    ad.id, ad.title, ad.url = "1", "Книга", "https://olx.ua/x"
    ad.price, ad.currency, ad.price_text = 130.0, "UAH", "130 грн."
    ad.negotiable = ad.promoted = False
    ad.city, ad.condition, ad.source = "Шостка", "Вживане", "olx"
    text = notify.format_event("new", "Поттер: Філософський камінь", ad)
    assert "🕒 створено 9 хв тому" in text
    # Нижче ціни й назви: цифра пояснює знахідку, а не конкурує з нею.
    assert text.index("130 грн.") < text.index("🕒")
