"""Тести на реальній структурі даних OLX (фікстура знята з живої сторінки).

Запуск:  python -m pytest -q      або      python tests/test_watcher.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as app  # noqa: E402
import notify  # noqa: E402
import olx  # noqa: E402

SAMPLE = json.loads((Path(__file__).parent / "sample_ads.json").read_text(encoding="utf-8"))


def fixture_html(state: dict | None = None) -> str:
    """Відтворює те, як OLX вбудовує стан у сторінку: JSON усередині JSON-рядка."""
    blob = json.dumps(json.dumps(state or SAMPLE, ensure_ascii=False))
    return f"<!doctype html><html><body><script>window.__PRERENDERED_STATE__ = {blob};</script></body></html>"


def test_parse():
    ads = olx.parse_ads(fixture_html())
    assert len(ads) == 5
    by_id = {a.id: a for a in ads}

    a = by_id["932087782"]
    assert a.price == 1990 and a.currency == "UAH"
    assert a.city == "Запоріжжя" and a.condition == "Вживане"
    assert a.promoted is False and a.similar is False
    assert a.price_text == "1 990 грн."

    assert by_id["916996334"].promoted is True
    assert by_id["929314548"].negotiable is True
    assert by_id["700000001"].price is None  # обмін


def test_no_state():
    try:
        olx.parse_ads("<html><body>капча</body></html>")
    except olx.OlxError:
        return
    raise AssertionError("мала бути OlxError")


def test_pagination_url():
    u = "https://www.olx.ua/uk/list/q-test/?currency=UAH&page=7"
    assert olx.with_page(u, 1) == u
    assert "page=3" in olx.with_page(u, 3) and "page=7" not in olx.with_page(u, 3)


def test_filters():
    ads = olx.parse_ads(fixture_html())
    kept = [a for a in ads if olx.matches(a, exclude=["копія", "друк на замовлення"], skip_promoted=True)]
    ids = {a.id for a in kept}
    assert "916996334" not in ids, "просунуте оголошення мало відсіятись"
    assert "848280249" not in ids, "КОПІЯ мала відсіятись (регістр не має значення)"
    assert ids == {"932087782", "929314548", "700000001"}

    cheap = [a for a in kept if olx.price_ok(a, max_price=600, min_price=None, currency="UAH")]
    assert {a.id for a in cheap} == {"929314548"}

    with_exchange = [
        a for a in kept
        if olx.price_ok(a, max_price=600, min_price=None, currency="UAH", allow_no_price=True)
    ]
    assert "700000001" in {a.id for a in with_exchange}

    assert not olx.price_ok(kept[0], max_price=999999, min_price=None, currency="USD")


def test_first_run_is_silent_then_detects_new_and_drop(monkeypatch, tmp_path=None):
    cfg = {
        "defaults": {"currency": "UAH", "min_drop_percent": 3},
        "watches": [{
            "id": "t", "name": "Тест", "url": "https://www.olx.ua/uk/list/q-test/",
            "max_price": 600, "exclude_keywords": ["копія"], "skip_promoted": True,
        }],
    }
    state = {"version": 1, "watches": {}}

    payloads = []

    def fake_fetch(session, url, pages=1, **kw):
        return olx.parse_ads(fixture_html(payloads.pop(0)))

    monkeypatch.setattr(olx, "fetch_watch", fake_fetch)
    monkeypatch.setattr(app.olx, "fetch_watch", fake_fetch)

    # 1) перший запуск — лише запам'ятовує
    payloads.append(SAMPLE)
    msgs, err = app.run(cfg, state, dry_run=False)
    assert err == 0 and msgs == [], "перший запуск не має слати сповіщення"
    assert state["watches"]["t"]["seeded"] is True
    assert "929314548" in state["watches"]["t"]["ads"]

    # 2) з'явилось нове дешеве оголошення
    second = json.loads(json.dumps(SAMPLE))
    second["listing"]["listing"]["ads"].append({
        "id": 999, "title": "Фундація, перше видання", "url": "https://www.olx.ua/d/uk/obyavlenie/new-ID999.html",
        "isPromoted": False, "createdTime": "2026-09-17T09:00:00+03:00", "itemCondition": "Вживане",
        "price": {"displayValue": "300 грн.", "regularPrice": {"value": 300, "currencyCode": "UAH"}},
        "photos": [], "location": {"cityName": "Одеса"}, "searchReason": "organic",
    })
    payloads.append(second)
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert len(msgs) == 1 and "Нове оголошення" in msgs[0] and "300 грн." in msgs[0]

    # 3) ціна існуючого впала: 450 -> 380
    third = json.loads(json.dumps(second))
    for ad in third["listing"]["listing"]["ads"]:
        if ad["id"] == 929314548:
            ad["price"]["regularPrice"]["value"] = 380
            ad["price"]["displayValue"] = "380 грн."
    payloads.append(third)
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert len(msgs) == 1 and "Ціна впала" in msgs[0]
    assert "450" in msgs[0] and "380 грн." in msgs[0] and "−16%" in msgs[0]

    # 4) нічого не змінилось — тиша
    payloads.append(third)
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert msgs == []

    # 5) мікро-падіння 380 -> 375 (1.3%) нижче порогу 3% — теж тиша
    fifth = json.loads(json.dumps(third))
    for ad in fifth["listing"]["listing"]["ads"]:
        if ad["id"] == 929314548:
            ad["price"]["regularPrice"]["value"] = 375
            ad["price"]["displayValue"] = "375 грн."
    payloads.append(fifth)
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert msgs == []


def test_drop_below_threshold_notifies_even_if_it_was_expensive(monkeypatch):
    """Оголошення було дорожче за max_price, потім подешевшало — має спрацювати."""
    cfg = {"defaults": {"min_drop_percent": 3}, "watches": [
        {"id": "t2", "name": "Тест2", "url": "https://x", "max_price": 600, "currency": "UAH"}
    ]}
    state = {"version": 1, "watches": {}}
    payloads = []
    monkeypatch.setattr(app.olx, "fetch_watch", lambda s, u, pages=1, **k: olx.parse_ads(fixture_html(payloads.pop(0))))

    payloads.append(SAMPLE)
    app.run(cfg, state, dry_run=False)  # seed: 1990 грн. записано, але не сповіщено

    cheaper = json.loads(json.dumps(SAMPLE))
    for ad in cheaper["listing"]["listing"]["ads"]:
        if ad["id"] == 932087782:
            ad["price"]["regularPrice"]["value"] = 550
            ad["price"]["displayValue"] = "550 грн."
    payloads.append(cheaper)
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert len(msgs) == 1 and "Ціна впала" in msgs[0] and "550 грн." in msgs[0]


def test_html_escaping():
    ad = olx.parse_ads(fixture_html())[1]
    ad.title = 'Книга <b>"Фундація"</b> & Ко'
    msg = notify.format_event("new", "W & W", ad)
    assert "&lt;b&gt;" in msg and "&amp;" in msg
    assert "<b>Нове оголошення</b>" in msg  # наша власна розмітка лишається


def test_exclude_keywords_merge_defaults_and_watch(monkeypatch):
    """Спільний чорний список з defaults має ДОДАВАТИСЬ до власного, а не зникати."""
    cfg = {
        "defaults": {"exclude_keywords": ["фігурк"]},
        "watches": [{"id": "m", "name": "M", "url": "https://x", "exclude_keywords": ["копія"]}],
    }
    state = {"version": 1, "watches": {}}

    data = json.loads(json.dumps(SAMPLE))
    data["listing"]["listing"]["ads"].append({
        "id": 555, "title": "Відьмак ФІГУРКА Ґеральта", "url": "https://x/555",
        "isPromoted": False, "itemCondition": "Нове", "createdTime": "2026-09-17T09:00:00+03:00",
        "price": {"displayValue": "200 грн.", "regularPrice": {"value": 200, "currencyCode": "UAH"}},
        "photos": [], "location": {"cityName": "Київ"}, "searchReason": "organic",
    })
    payloads = [data]
    monkeypatch.setattr(app.olx, "fetch_watch", lambda s, u, pages=1, **k: olx.parse_ads(fixture_html(payloads.pop(0))))

    app.run(cfg, state, dry_run=False)
    seen = state["watches"]["m"]["ads"]
    assert "555" not in seen, "фігурка з defaults мала відсіятись"
    assert "848280249" not in seen, "КОПІЯ з власного списку мала відсіятись"
    assert "932087782" in seen, "звичайна книжка мала лишитись"


def test_prune():
    ws = {"ads": {"a": {"seen": "2000-01-01T00:00:00+00:00"}, "b": {"seen": "2999-01-01T00:00:00+00:00"}}}
    assert app.prune(ws, 30) == 1
    assert list(ws["ads"]) == ["b"]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_only_and_skip_select_watches():
    """Букфлі крутиться вдома (потрібен український IP), OLX — у хмарі."""
    import main as app

    cfg = {"watches": [
        {"name": "Відьмак", "url": "u"},
        {"name": "Букфлі", "source": "bookflea", "keywords": []},
    ]}
    pick = lambda **kw: [w["name"] for i, w in enumerate(cfg["watches"])
                         if app.wanted(w, i, kw.get("only", []), kw.get("skip", []))]

    assert pick() == ["Відьмак", "Букфлі"]
    assert pick(skip=["Букфлі"]) == ["Відьмак"]
    assert pick(only=["Букфлі"]) == ["Букфлі"]
    assert pick(skip=["bookflea"]) == ["Відьмак"], "можна і за source"
    assert pick(only=["букфлі"]) == ["Букфлі"], "регістр не має значення"
    assert pick(only=["Немає такого"]) == []


def test_forget_orphans_drops_renamed_watches():
    """Ключ стану — це name, тож перейменування лишає по собі мертвий запис."""
    import main as app

    cfg = {"watches": [{"name": "Роналду"}, {"name": "Вимкнений", "enabled": False}]}
    state = {"version": 1, "watches": {
        "Рональду": {"ads": {"x": {}}},      # стара назва з м'яким знаком
        "Роналду": {"ads": {}},
        "Вимкнений": {"ads": {"y": {}}},
    }}

    assert app.forget_orphans(cfg, state) == ["Рональду"]
    assert set(state["watches"]) == {"Роналду", "Вимкнений"}, "вимкнений watch стан зберігає"
