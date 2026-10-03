"""JSON-пошук OLX замість HTML-сторінки (2026-10-03).

Фікстура `sample_olx_api.json` — справжня відповідь `/api/v1/offers/` за
запитом «кідрук», знята 2026-10-03. Вигадані дані тут були б марними: уся
цінність переходу в тому, що формат саме такий, а не такий, як нам зручно.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import olx  # noqa: E402

FIXTURE = (Path(__file__).resolve().parent / "sample_olx_api.json").read_text(encoding="utf-8")
WATCH_URL = ("https://www.olx.ua/uk/hobbi-otdyh-i-sport/knigi-zhurnaly/"
             "q-%D0%BA%D1%96%D0%B4%D1%80%D1%83%D0%BA/"
             "?currency=UAH&search%5Border%5D=created_at%3Adesc")


# ───────────────────────────────────────────── URL watch'а → параметри API

def test_watch_url_becomes_api_params():
    p = olx.api_params(WATCH_URL)
    assert p["query"] == "кідрук"
    assert p["category_id"] == 49
    assert p["currency"] == "UAH"
    # У HTML це search[order], в API — sort_by. Назви різні, значення те саме.
    assert p["sort_by"] == "created_at:desc"


def test_category_is_mandatory_not_optional():
    """🔑 Без `category_id` той самий запит «кідрук» віддає 203 оголошення
    замість 194 — туди домішується все, що просто містить це слово. Тому
    невідома категорія означає «лишаємось на HTML», а не «спитаємо без неї»."""
    assert olx.api_params("https://www.olx.ua/uk/transport/q-bmw/") is None
    assert olx.api_params("https://www.olx.ua/uk/hobbi-otdyh-i-sport/knigi-zhurnaly/") is None


def test_pages_become_offsets():
    assert olx.api_params(WATCH_URL, page=1)["offset"] == 0
    assert olx.api_params(WATCH_URL, page=2)["offset"] == 50


# ───────────────────────────────────────────── розбір відповіді

def test_every_field_the_watcher_needs_survives_the_parse():
    ads = {a.id: a for a in olx.parse_ads_api(FIXTURE)}
    a = ads["936739415"]
    assert a.title == "Доки світло не згасне назавжди — Макс Кідрук"
    assert a.price == 200.0 and a.currency == "UAH"
    assert a.price_text == "200 грн."
    assert a.condition == "Вживане"
    assert a.city == "Ужгород"
    assert a.created_time.startswith("2026-10-03")
    assert a.url.endswith("-ID11osIf.html"), "посилання має бути з id, його бачить людина"


def test_the_description_comes_with_the_listing():
    """Заради цього все й робилось: окремий запит на опис зникає, а разом з ним
    і стеля `description_max_fetch`, через яку решта оголошень проходила мовні
    фільтри наосліп."""
    ads = olx.parse_ads_api(FIXTURE)
    assert all(a.description for a in ads)
    assert "Макса Кідрука" in ads[1].description
    # HTML з опису прибрано — правила дивляться на слова, не на розмітку.
    assert "<br" not in ads[0].description


def test_promoted_comes_from_metadata_not_guesswork():
    """В HTML це `searchReason`, в API — індекси в metadata.source. Переплутати
    легко, а ціна — `skip_promoted`, що мовчки відсіює не те."""
    ads = olx.parse_ads_api(FIXTURE)
    assert ads[0].promoted is True and ads[0].reason == "promoted"
    assert all(not a.promoted for a in ads[1:])
    assert all(not a.similar for a in ads), "усе з видачі — точні збіги"


def test_an_unknown_metadata_shape_keeps_the_ads():
    """Якщо OLX колись прибере metadata.source, мовчки втратити всю видачу
    гірше, ніж пропустити кілька промо: позначаємо все точними збігами."""
    data = json.loads(FIXTURE)
    data["metadata"].pop("source")
    ads = olx.parse_ads_api(json.dumps(data))
    assert len(ads) == 5 and all(not a.similar for a in ads)


def test_the_photo_template_is_filled_in():
    """OLX віддає посилання з {width}x{height}; без підстановки це не URL."""
    ads = olx.parse_ads_api(FIXTURE)
    assert "{width}" not in ads[0].photo and "s=1000x700" in ads[0].photo


def test_negotiable_survives():
    ads = {a.id: a for a in olx.parse_ads_api(FIXTURE)}
    assert ads["925482422"].negotiable is True
    assert ads["936739415"].negotiable is False


# ───────────────────────────────────────────── відкат на HTML

def test_a_failed_api_call_raises_so_the_caller_can_fall_back(monkeypatch):
    class Dead:
        def get(self, url, **kw):
            return 403, "nope"
    with pytest.raises(olx.OlxError):
        olx.fetch_watch_api(Dead(), WATCH_URL)


def test_non_json_is_an_error_not_an_empty_feed(monkeypatch):
    """Порожня видача і зламана відповідь — різні речі. Якби ми повертали
    порожній список, прогін вирішив би, що оголошень просто немає, і наступний
    побачив би їх усі як нові."""
    class Html:
        def get(self, url, **kw):
            return 200, "<html>не те</html>"
    with pytest.raises(olx.OlxError):
        olx.fetch_watch_api(Html(), WATCH_URL)
