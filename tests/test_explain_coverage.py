"""`--explain` має казати не лише «що зробили б правила», а й «чи бачимо взагалі».

Привід (2026-10-08): «Детективний роман Кувала зозуля» — правила приймали його
без питань, але жоден watch не мав його у видачі, бо в оголошенні немає ані
автора, ані серії. Рішення правил виглядало як норма, і діру було не видно.
Див. claude/search-coverage.md.
"""

import logging

import main
import olx


def _ad(ad_id: str) -> olx.Ad:
    return olx.Ad(id=ad_id, title="Детективний роман Кувала зозуля",
                  url=f"https://www.olx.ua/d/uk/obyavlenie/x-ID{ad_id}.html",
                  price=400.0, currency="UAH", price_text="400 грн.",
                  source="olx")


def _watches():
    return [("Роберт Ґалбрейт (Страйк)", {"name": "Роберт Ґалбрейт (Страйк)",
                                          "url": "https://www.olx.ua/uk/q-галбрейт/"}),
            ("Ґалбрейт: Кувала зозуля", {"name": "Ґалбрейт: Кувала зозуля",
                                         "url": "https://www.olx.ua/uk/q-кувала-зозуля/"})]


def test_каже_коли_жоден_watch_не_бачить(monkeypatch, caplog):
    """Приймаємо за фільтрами, але у видачі немає — це треба назвати вголос."""
    monkeypatch.setattr(olx, "fetch_watch_api",
                        lambda *a, **k: [_ad("111"), _ad("222")])
    with caplog.at_level(logging.INFO, logger=main.log.name):
        main.explain_coverage(object(), {}, _watches(), "936970750", pause=0)
    out = caplog.text
    assert "ПРИЙМАЮ, АЛЕ У ВИДАЧІ НЕМАЄ" in out
    assert "діра в покритті" in out


def test_мовчить_про_діру_коли_хтось_бачить(monkeypatch, caplog):
    """Якщо хоч один watch бачить оголошення — причина в правилах, не в покритті."""
    def fake(session, url, **kw):
        return [_ad("936970750")] if "зозуля" in url else [_ad("111")]

    monkeypatch.setattr(olx, "fetch_watch_api", fake)
    with caplog.at_level(logging.INFO, logger=main.log.name):
        main.explain_coverage(object(), {}, _watches(), "936970750", pause=0)
    out = caplog.text
    assert "✓ є у видачі" in out
    assert "діра в покритті" not in out


def test_падіння_одного_запиту_не_ламає_перевірку(monkeypatch, caplog):
    """Мережа могла й не відповісти — решту watch'ів це зупиняти не має."""
    def fake(session, url, **kw):
        if "галбрейт" in url:
            raise olx.OlxError("API відповів 429")
        return [_ad("936970750")]

    monkeypatch.setattr(olx, "fetch_watch_api", fake)
    monkeypatch.setattr(olx, "fetch_watch", fake)
    with caplog.at_level(logging.INFO, logger=main.log.name):
        main.explain_coverage(object(), {}, _watches(), "936970750", pause=0)
    out = caplog.text
    assert "не вдалось перевірити" in out
    assert "✓ є у видачі" in out


def test_html_фолбек_коли_api_не_розкладається(monkeypatch, caplog):
    """URL без q- у шлях API не кладеться — тоді беремо HTML, а не мовчимо."""
    calls = []
    monkeypatch.setattr(olx, "fetch_watch_api",
                        lambda *a, **k: (_ for _ in ()).throw(olx.OlxError("не розкладається")))
    monkeypatch.setattr(olx, "fetch_watch",
                        lambda s, url, **k: calls.append(url) or [_ad("936970750")])
    with caplog.at_level(logging.INFO, logger=main.log.name):
        main.explain_coverage(object(), {}, _watches()[:1], "936970750", pause=0)
    assert calls, "мав відкотитись на HTML"
    assert "✓ є у видачі" in caplog.text


def test_pages_береться_з_налаштувань_watcha(monkeypatch):
    """`pages: 2` у конфізі має доїхати до запиту — інакше перевіряємо не те."""
    seen = {}

    def fake(session, url, *, pages=1, **kw):
        seen[url] = pages
        return []

    monkeypatch.setattr(olx, "fetch_watch_api", fake)
    ws = [("Ґалбрейт: На службі зла",
           {"name": "Ґалбрейт: На службі зла",
            "url": "https://www.olx.ua/uk/q-на-службі-зла/", "pages": 2})]
    main.explain_coverage(object(), {"pages": 1}, ws, "936970750", pause=0)
    assert list(seen.values()) == [2]
