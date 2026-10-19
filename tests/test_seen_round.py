"""Зрізана мітка «востаннє бачили» (2026-10-19).

Запис стану коштував 11 с із 64 на проході, де на ринку не сталось нічого:
`seen` писався з точністю до мікросекунди, тож усі 469 оголошень щохвилини
відрізнялись від збережених. Тут перевіряється те, заради чого зріз і
зроблено, і те, що він міг зламати тихо: порядок рядків (за ним працює
`prune`) і запис справжніх змін, який затримуватись НЕ сміє.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as app  # noqa: E402
import storage  # noqa: E402


def _dt(h, m, s=7, us=123456):
    return datetime(2026, 10, 19, h, m, s, us, tzinfo=timezone.utc)


def test_зріз_до_чверті_години():
    assert app.floor_minutes(_dt(8, 21), 15) == "2026-10-19T08:15:00+00:00"
    assert app.floor_minutes(_dt(8, 14), 15) == "2026-10-19T08:00:00+00:00"
    assert app.floor_minutes(_dt(8, 59), 15) == "2026-10-19T08:45:00+00:00"


def test_одиниця_вимикає_зріз():
    """Шлях відступу: `seen_round_minutes: 1` — рівно колишня поведінка."""
    assert app.floor_minutes(_dt(8, 21), 1) == _dt(8, 21).isoformat()
    assert app.floor_minutes(_dt(8, 21), 0) == _dt(8, 21).isoformat()


def test_мітки_лишаються_порівнюваними_рядками():
    """`prune()` порівнює `seen` як РЯДОК — зріз не сміє зламати порядок."""
    a = app.floor_minutes(_dt(8, 14), 15)
    b = app.floor_minutes(_dt(8, 21), 15)
    c = app.floor_minutes(_dt(9, 1), 15)
    assert a < b < c


def test_у_межах_чверті_запис_не_змінюється():
    """Суть правки: два проходи в одній чверті дають ОДНАКОВИЙ запис."""
    first = app.floor_minutes(_dt(8, 16, 3), 15)
    later = app.floor_minutes(_dt(8, 29, 58), 15)
    assert first == later


def test_зміна_ціни_пишеться_одразу(mongo_client):
    """Зріз не сміє затримати справжню зміну: інакше це вже буфер, а не мітка."""
    st = storage.MongoStore("mongodb://fake", "t", client=mongo_client)
    seen = app.floor_minutes(_dt(8, 16), 15)
    state = {"version": storage.STATE_VERSION, "watches": {
        "w": {"seeded": True, "ads": {"1": {"price": 100.0, "cur": "UAH",
                                            "title": "Книга", "seen": seen,
                                            "first": seen}}}}}
    st.save(state)

    # Той самий прохід-близнюк усередині чверті: писати нема чого.
    state["watches"]["w"]["ads"]["1"]["seen"] = app.floor_minutes(_dt(8, 29), 15)
    assert st.save(state)["ads"] == 0

    # А ось ціна змінилась — і це їде в базу тієї ж секунди.
    state["watches"]["w"]["ads"]["1"]["price"] = 80.0
    counts = st.save(state)
    assert counts["ads"] == 1
    assert counts["prices"] == 1
