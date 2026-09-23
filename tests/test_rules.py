"""Тести на шар правил — писані з живих оголошень, які приносив користувач.

Кожен тест названий по тому рішенню, яке він захищає, а не по функції.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rules


KIDRUK = {
    "max_price": 2000,
    "needs_description": True,
    "bundle_notify": True,
    "bundle_analytics": False,
    "notify_max_price": 150,
    "notify_min_price_high": 1000,
}

HP = {
    "max_price": 250,
    "needs_description": True,
    "drop_russian": True,
    "drop_keywords": ["альманах", "ілюстрован", "illustrated"],
    "notify_max_price": 180,
}

CLARE = {"max_price": 450, "notify_max_price": 250}

BOT = {"name": "Кідрук: Бот", "any": ["бот"], "notify_max": 500, "store_max": 2000}
CURSED = {"name": "Поттер: Прокляте дитя", "any": ["прокляте дитя"],
          "notify_max": 180, "store_max": 250}


# ─────────────────────────────────────────────── Кідрук

def test_bot_under_500_goes_to_telegram():
    d = rules.decide(title="Макс Кідрук Бот Атакамська криза 2024р",
                     price=450, watch=KIDRUK, entry=BOT)
    assert d.notify and d.analytics


def test_bot_over_500_is_stored_but_silent():
    """Гуаякільський парадокс за 600: купувати не хочемо, а чи продасться —
    цікаво. Саме заради цього випадку з'явився результат 'store'."""
    d = rules.decide(title="Макс Кідрук Бот Гуаякільський парадокс тверда обкладинка",
                     price=600, watch=KIDRUK, entry=BOT)
    assert d.action == "store"
    assert d.store and not d.notify
    assert d.analytics


def test_minor_kidruk_book_is_silent_in_the_middle_band():
    d = rules.decide(title="Макс Кідрук Не озирайся і мовчи",
                     price=300, watch=KIDRUK, entry=None)
    assert d.action == "store"


def test_minor_kidruk_book_notifies_when_cheap():
    d = rules.decide(title="Макс Кідрук Не озирайся і мовчи",
                     price=140, watch=KIDRUK, entry=None)
    assert d.notify


def test_minor_kidruk_book_notifies_when_absurdly_expensive():
    """Дорогі лоти шлемо не щоб купити, а щоб бачити стелю ринку."""
    d = rules.decide(title="Макс Кідрук Не озирайся і мовчи",
                     price=1500, watch=KIDRUK, entry=None)
    assert d.notify and d.tag == "high"


def test_bundle_from_description_notifies_but_stays_out_of_statistics():
    """Ціна комплекту ні про що не говорить: у кожному наборі свій склад."""
    d = rules.decide(title="Макс Кідрук книги",
                     description="Продам усі три книги одним лотом, комплект",
                     price=900, watch=KIDRUK, entry=None)
    assert d.notify
    assert d.analytics is False


def test_bundle_beats_the_price_band():
    """900 грн — це і не ≤150, і не ≥1000. Без правила комплекту промовчали б."""
    d = rules.decide(title="Комплект книг Макса Кідрука",
                     price=900, watch=KIDRUK, entry=None)
    assert d.notify


# ─────────────────────────────────────────────── Гаррі Поттер

def test_russian_edition_is_dropped_by_its_letters():
    d = rules.decide(title="Гарри Поттер и узник Азкабана",
                     price=100, watch=HP, entry=None)
    assert d.action == "skip"
    assert not d.store


def test_rosmen_in_description_is_dropped():
    """У заголовку українською, видавництво видно тільки з опису."""
    d = rules.decide(title="Гаррі Поттер і келих вогню",
                     description="Видавництво Росмен, стан гарний",
                     price=120, watch=HP, entry=None)
    assert d.action == "skip"


def test_almanac_is_not_a_book_from_the_series():
    d = rules.decide(title="Гаррі Поттер Чаклунський альманах",
                     price=90, watch=HP, entry=None)
    assert d.action == "skip"


def test_illustrated_edition_is_dropped():
    d = rules.decide(title="Гаррі Поттер і філософський камінь ілюстроване видання",
                     price=900, watch=HP, entry=None)
    assert d.action == "skip"


def test_cursed_child_telegram_and_analytics_bands():
    cheap = rules.decide(title="Книга Гаррі Поттер прокляте дитя",
                         price=170, watch=HP, entry=CURSED)
    middle = rules.decide(title="Книга Гаррі Поттер прокляте дитя",
                          price=230, watch=HP, entry=CURSED)
    dear = rules.decide(title="Книга Гаррі Поттер прокляте дитя",
                        price=400, watch=HP, entry=CURSED)
    assert cheap.notify
    assert middle.action == "store" and middle.analytics
    # Дорожче за межу статистики оголошення все одно пам'ятаємо — інакше не буде
    # з чим порівняти, якщо продавець завтра скине ціну до 170.
    assert dear.action == "store" and dear.analytics is False


# ─────────────────────────────────────────────── Клер

def test_clare_bands():
    assert rules.decide(title="Книга Місто впалих янголів Кассандра Клер",
                        price=240, watch=CLARE).notify
    assert rules.decide(title="Книга Місто впалих янголів Кассандра Клер",
                        price=300, watch=CLARE).action == "store"
    dear = rules.decide(title="Книга Місто впалих янголів Кассандра Клер",
                        price=600, watch=CLARE)
    assert dear.action == "store" and dear.analytics is False


# ─────────────────────────────────────────────── загальне

def test_catalog_entry_overrides_the_watch_threshold():
    """У watch'і Кідрука поріг 150, але для Бота каталог каже 500."""
    d = rules.decide(title="Кідрук Бот", price=400, watch=KIDRUK, entry=BOT)
    assert d.notify


def test_skip_entry_wins_over_everything():
    entry = {"name": "Поттер: сувеніри", "any": ["герміона"], "skip": True}
    d = rules.decide(title="Новий Герміона Hermione Гаррі Поттер подарунок магія шоу",
                     price=50, watch=HP, entry=entry)
    assert d.action == "skip"


def test_ad_without_price_is_remembered_but_silent():
    d = rules.decide(title="Кідрук Бот обмін", price=None, watch=KIDRUK, entry=BOT)
    assert d.store and not d.notify
    assert d.analytics is False


def test_ukrainian_title_survives_the_russian_filter():
    d = rules.decide(title="Гаррі Поттер і в'язень Азкабану",
                     price=150, watch=HP, entry=None)
    assert d.notify


def test_watch_without_telegram_threshold_behaves_as_before():
    """Регресія 2026-09-22: без notify_max_price десяток watch'ів замовк би
    цілком, і це виглядало б як зламаний скрапер, а не як зміна конфігу."""
    old_style = {"max_price": 2000}
    assert rules.decide(title="Кінг Талісман", price=500, watch=old_style).notify
    over = rules.decide(title="Кінг Талісман", price=2500, watch=old_style)
    assert not over.notify and over.analytics is False


def test_high_marker_alone_does_not_open_the_cheap_gate():
    """Якщо заданий тільки верхній маркер, дешеве НЕ має їхати в Telegram."""
    w = {"max_price": 2000, "notify_min_price_high": 1000}
    assert rules.decide(title="х", price=100, watch=w).action == "store"
    assert rules.decide(title="х", price=1500, watch=w).tag == "high"


# ─────────────────────────────────────────────── мова оголошення

KING = {"max_price": 2000, "silent_russian": True, "drop_english": True}

RU_DESC = ("Продам книгу в отличном состоянии, издательство АСТ, 480 страниц, "
           "твердый переплет. Отправка Новой почтой по предоплате.")
EN_DESC = ("Stephen King, Black House. Hardcover, first edition, very good "
           "condition. Shipping available across the country by post.")
UA_DESC = ("Продам книжку у гарному стані, видавництво КСД, тверда палітурка. "
           "Надсилаю Новою поштою по всій Україні.")


def test_russian_ad_is_kept_for_history_but_never_sent():
    """Кінг і Ріггз російською трапляються постійно: ціну ринку вони таки
    показують, а в Telegram їм робити нічого."""
    d = rules.decide(title="Стівен Кінг Безсоння", description=RU_DESC,
                     price=195, watch=KING)
    assert d.action == "store"
    assert not d.notify and d.store


def test_english_ad_is_not_kept_at_all():
    """Ціни на англійські видання живуть своїм життям і зсувають медіану."""
    d = rules.decide(title="Black House Stephen King Чорний дім Стівен Кінг",
                     description=EN_DESC, price=400, watch=KING)
    assert d.action == "skip"


def test_bilingual_title_with_ukrainian_description_survives():
    """Пів заголовка англійською — норма для української книгарні."""
    d = rules.decide(title="Black House Stephen King Чорний дім Стівен Кінг",
                     description=UA_DESC, price=400, watch=KING)
    assert d.notify


def test_english_check_needs_a_long_enough_text():
    """Два англійські слова в назві — це ще не англомовне видання."""
    d = rules.decide(title="Stephen King", description=None, price=300, watch=KING)
    assert d.action != "skip"


# ─────────────────────────────────────────────── комплект із власним порогом

STAND = {"name": "Кінг: Протистояння", "any": ["протистояння"],
         "notify_max": 350, "notify_max_bundle": 700}


def test_the_stand_single_volume_threshold():
    assert rules.decide(title="Стівен Кінг Протистояння том I", price=340,
                        watch=KING, entry=STAND).notify
    assert not rules.decide(title="Стівен Кінг Протистояння том I", price=500,
                            watch=KING, entry=STAND).notify


def test_the_stand_two_volume_set_gets_the_double_threshold():
    """«2 книги, томи I–II» за 1700 — мовчимо; ті самі два томи за 690 — пишемо."""
    cheap = rules.decide(title="Стівен Кінг «Протистояння» — 2 книги, томи I–II",
                         price=690, watch=KING, entry=STAND)
    dear = rules.decide(title="Стівен Кінг «Протистояння» — 2 книги, томи I–II",
                        price=1700, watch=KING, entry=STAND)
    assert cheap.notify
    assert not dear.notify
    assert dear.store          # статистику по продажах збираємо все одно


def test_foundation_sends_everything_including_ads_without_a_price():
    w = {"max_price": 100000, "notify_max_price": 100000, "allow_no_price": True}
    assert rules.decide(title="Азімов Фундація", price=1500, watch=w).notify
    assert rules.decide(title="Азімов Фундація обмін", price=None, watch=w).notify
