"""Тести Bookflea на розмітці, знятій з живої сторінки.

Запуск:  python -m pytest -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bookflea  # noqa: E402
import main as app  # noqa: E402
import notify  # noqa: E402

HTML = (Path(__file__).parent / "sample_bookflea.html").read_text(encoding="utf-8")

KEYWORDS = ["Роналду", "Відьмак", "Бункер", "Ден Браун", "Азімов",
            "Фундація", "Служниця", "Кідрук", "Гаррі Поттер"]


def test_parse_cards():
    ads = bookflea.parse_listings(HTML)
    by_id = {a.id: a for a in ads}

    assert len(ads) == 6, "дубль того самого /ads/ не має подвоювати оголошення"
    assert all(a.source == "bookflea" for a in ads)

    a = by_id["u6x6e2e9y23tc3o84ln658dy"]
    assert a.author == "Анджей Сапковський"
    assert a.title == "Відьмак. Останнє бажання. Книга 1"
    assert a.price == 185 and a.currency == "UAH"
    assert a.url == "https://www.bookflea.co/ads/u6x6e2e9y23tc3o84ln658dy"
    # з /_next/image дістаємо оригінал на CDN, а не проксі-посилання
    assert a.photo == "https://bookflea.b-cdn.net/ads/11111111-1111-1111-1111-111111111111.jpeg"

    assert by_id["t0g4szaa5e8ynkktc804kxam"].price == 1250, "пробіл у тисячах"
    assert by_id["ekhwhp7l6cgzocuv76gpqhz6"].price is None, "«Договірна» — не число"
    assert by_id["ekhwhp7l6cgzocuv76gpqhz6"].price_text == "Договірна"

    # fallback на alt, коли класи зникли
    f = by_id["onlyaltfallback00000001"]
    assert f.title == "Фундація" and f.author == "Айзек Азімов" and f.price == 330


def test_local_verification_rejects_fuzzy_search_noise():
    """Головне: сайт на «Бункер» віддає «Альманах Сталкер» — це має відсіятись."""
    ads = bookflea.parse_listings(HTML)
    noise = next(a for a in ads if a.id == "ynoelzyzn196r3kijjkzv6ww")
    assert bookflea.matches_keyword(noise, KEYWORDS) is None

    witcher = next(a for a in ads if a.id == "u6x6e2e9y23tc3o84ln658dy")
    assert bookflea.matches_keyword(witcher, KEYWORDS) == "Відьмак"


def test_keyword_can_match_author():
    ads = bookflea.parse_listings(HTML)
    brown = next(a for a in ads if a.id == "i7z3xyj5k6kxyyjkuzhi17a4")
    assert bookflea.matches_keyword(brown, KEYWORDS) == "Ден Браун"


def test_apostrophe_and_case_insensitive():
    ads = bookflea.parse_listings(HTML)
    hp = next(a for a in ads if a.id == "ekhwhp7l6cgzocuv76gpqhz6")
    assert bookflea.matches_keyword(hp, ["гаррі поттер"]) == "гаррі поттер"
    assert bookflea.matches_keyword(hp, ["в'язень"]) == "в'язень", "’ і ' мають бути одним"


def test_collect_latest_mode(monkeypatch):
    monkeypatch.setattr(bookflea, "_get", lambda *a, **k: HTML)
    ads, window = bookflea.collect(None, KEYWORDS, mode="latest")
    ids = {a.id for a in ads}
    assert len(window) == 6, "вікно = всі оголошення сторінки, не лише збіги"
    assert "ynoelzyzn196r3kijjkzv6ww" in window
    assert "ynoelzyzn196r3kijjkzv6ww" not in ids, "шум не мав пройти"
    assert ids == {
        "u6x6e2e9y23tc3o84ln658dy",   # Відьмак
        "t0g4szaa5e8ynkktc804kxam",   # Відьмак
        "i7z3xyj5k6kxyyjkuzhi17a4",   # Ден Браун
        "ekhwhp7l6cgzocuv76gpqhz6",   # Гаррі Поттер
        "onlyaltfallback00000001",    # Фундація / Азімов
    }
    assert all(a.matched for a in ads)


def test_collect_search_mode_dedupes_across_keywords(monkeypatch):
    """Фундація і Азімов — різні слова, але одне оголошення. Має бути один раз."""
    calls = []

    def fake_get(session, url, **kw):
        calls.append(url)
        return HTML

    monkeypatch.setattr(bookflea, "_get", fake_get)
    monkeypatch.setattr(bookflea.time, "sleep", lambda *_: None)
    ads, _ = bookflea.collect(None, ["Фундація", "Азімов"], mode="search")
    assert len(calls) == 2, "по запиту на кожне слово"
    assert len(ads) == 1 and ads[0].id == "onlyaltfallback00000001"


def test_end_to_end_seed_then_notify(monkeypatch):
    cfg = {
        "defaults": {"exclude_keywords": ["фігурк"]},
        "watches": [{
            "id": "bf", "name": "Букфлі", "source": "bookflea",
            "mode": "latest", "use_default_excludes": False, "keywords": KEYWORDS,
        }],
    }
    state = {"version": 1, "watches": {}}
    monkeypatch.setattr(bookflea, "_get", lambda *a, **k: HTML)
    monkeypatch.setattr(app.olx, "build_session", lambda: None)

    msgs, err = app.run(cfg, state, dry_run=False)
    assert err == 0 and msgs == [], "перший запуск мовчить"
    assert len(state["watches"]["bf"]["ads"]) == 5

    # той самий набір ще раз — жодного повідомлення
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert msgs == []

    # нове оголошення з тим самим ключовим словом — має прилетіти
    extra = HTML.replace(
        '<a href="/auth?redirect=%2Fme">Кабінет</a>',
        '<a href="/ads/brandnew0000000000000001"><div>'
        '<img alt="Відьмак. Кров ельфів — Анджей Сапковський" src="/x.jpg">'
        '<span class="text-sm">Анджей Сапковський</span>'
        '<span class="text-base">Відьмак. Кров ельфів</span><b>300 грн</b></div></a>'
    )
    monkeypatch.setattr(bookflea, "_get", lambda *a, **k: extra)
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert len(msgs) == 1
    assert "Нове оголошення" in msgs[0] and "Відьмак. Кров ельфів" in msgs[0]
    assert "Анджей Сапковський" in msgs[0] and "«Відьмак»" in msgs[0]


def test_warns_when_window_rotated_completely(monkeypatch):
    """Якщо між прогонами зі стрічки зникло геть усе — могли щось проґавити."""
    cfg = {"defaults": {}, "watches": [{
        "id": "bf", "name": "Букфлі", "source": "bookflea",
        "mode": "latest", "keywords": KEYWORDS,
    }]}
    state = {"version": 1, "watches": {}}
    monkeypatch.setattr(bookflea, "_get", lambda *a, **k: HTML)
    app.run(cfg, state, dry_run=False)          # seed
    assert state["watches"]["bf"]["window"]

    # повністю інша сторінка: жодного спільного id
    other = HTML.replace("/ads/", "/ads/zz")
    monkeypatch.setattr(bookflea, "_get", lambda *a, **k: other)
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert any("змінилась уся стрічка" in m for m in msgs)


def test_interval_minutes_skips_run():
    from datetime import datetime, timedelta, timezone
    ws = {"last_run": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
    assert app.due(ws, 60) is False
    assert app.due(ws, 5) is True
    assert app.due(ws, None) is True
    assert app.due({}, 60) is True, "перший раз — завжди"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
