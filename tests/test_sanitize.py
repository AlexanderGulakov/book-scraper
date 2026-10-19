"""Разова чистка бази (`--sanitize`, 2026-10-19).

Годинний звіт має бюджет 60 перевірок і поріг у 14 днів, тож усе, що зникло
давніше, він не питає НІКОЛИ — такі оголошення лежать у базі, псують
«найдешевше зараз» і медіани, доки `prune_days` не забуде їх через місяць.
Чистка знімає обидва обмеження. Ламається вона тихо саме там, де щось зайве
потрапить у чергу: живе оголошення, яке просто щойно бачили.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as app  # noqa: E402


def _state(ads):
    return {"version": 1, "watches": {"Тест": {"seeded": True, "ads": ads}}}


def _cfg():
    return {"defaults": {}, "watches": [
        {"id": "t", "name": "Тест", "url": "https://www.olx.ua/uk/list/q-t/"}]}


def _ad(seen_hours_ago, *, miss=None, days=3):
    now = datetime.now(timezone.utc)
    rec = {"price": 100.0, "cur": "UAH", "title": "Книга",
           "url": "https://www.olx.ua/d/uk/obyavlenie/x-ID1.html",
           "seen": (now - timedelta(hours=seen_hours_ago)).isoformat(),
           "first": (now - timedelta(days=days)).isoformat()}
    if miss:
        rec["miss"] = (now - timedelta(days=miss)).isoformat()
    return rec


def test_свіже_оголошення_не_потрапляє_в_чергу(monkeypatch):
    """Головна пастка: те, що щойно бачили у видачі, чіпати не можна."""
    seen = []
    monkeypatch.setattr(app, "_check_alive",
                        lambda batch, *a, **k: seen.append(batch) or ["alive"] * len(batch))
    state = _state({"1": _ad(seen_hours_ago=0.2)})
    stats = app.sanitize(_cfg(), state, object(), pause=0, workers=1)
    assert stats["queued"] == 0 and stats["marked"] == 0
    assert not seen, "перевіряти живе оголошення не було за що"


def test_застарілий_seen_ставить_miss(monkeypatch):
    """Watch давно не ходив — `miss` ніхто не поставив, а оголошення мертве."""
    monkeypatch.setattr(app, "_check_alive", lambda batch, *a, **k: ["gone"] * len(batch))
    state = _state({"1": _ad(seen_hours_ago=200)})
    stats = app.sanitize(_cfg(), state, object(), pause=0, workers=1, stale_hours=48)
    assert stats["marked"] == 1 and stats["gone"] == 1
    assert state["watches"]["Тест"]["ads"] == {}, "зняте мало піти з watch'а"
    assert state["sold"][0]["id"] == "1", "…і лягти в sold, а не зникнути"


def test_старе_зникле_перевіряється_попри_вікно_в_14_днів(monkeypatch):
    """Рівно те, чого не вміє годинний звіт: `max_age_days` тут не діє."""
    monkeypatch.setattr(app, "_check_alive", lambda batch, *a, **k: ["gone"] * len(batch))
    state = _state({"1": _ad(seen_hours_ago=1, miss=25, days=40)})
    stats = app.sanitize(_cfg(), state, object(), pause=0, workers=1)
    assert stats["gone"] == 1


def test_живе_за_посиланням_повертається_в_базу(monkeypatch):
    """`alive` означає «просто з'їхало з першої сторінки» — не видаляти."""
    monkeypatch.setattr(app, "_check_alive", lambda batch, *a, **k: ["alive"] * len(batch))
    state = _state({"1": _ad(seen_hours_ago=200)})
    stats = app.sanitize(_cfg(), state, object(), pause=0, workers=1)
    assert stats["gone"] == 0
    assert "1" in state["watches"]["Тест"]["ads"]
    assert "miss" not in state["watches"]["Тест"]["ads"]["1"], "мітку мали зняти"
