"""Розкладка часу проходу (2026-10-19).

Прохід знову переріс хвилину, а лог казав лише «довший за період» — без
відповіді, куди пішов час. `Timings` цю відповідь дає, і ламається вона тихо:
стаття, вкладена в іншу статтю, порахується двічі, і «решта» поїде в мінус,
нікому про це не сказавши. Тому тут перевіряється саме арифметика, а не те,
що рядок узагалі друкується.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as app  # noqa: E402
import olx  # noqa: E402


def test_стаття_накопичується_за_кілька_викликів():
    """Статей десяток, а викликів на прохід сотні — вони мусять складатись."""
    tm = app.Timings()
    for _ in range(3):
        with tm.took("мережа"):
            time.sleep(0.01)
    assert tm["мережа"] >= 0.03


def test_час_рахується_навіть_коли_всередині_виняток():
    """Запит, що впав, теж коштував часу — інакше повільні помилки невидимі."""
    tm = app.Timings()
    with pytest.raises(ValueError):
        with tm.took("мережа"):
            time.sleep(0.01)
            raise ValueError("мережа лягла")
    assert tm["мережа"] >= 0.01


def test_решта_не_йде_в_мінус():
    """Сума статей не сміє перевищити загальний час проходу."""
    tm = app.Timings()
    with tm.took("видача"):
        time.sleep(0.06)       # більше за поріг 0.05, інакше стаття не в рядку
    line = tm.line(1.0)
    assert "видача" in line
    rest = float(line.split("решта ")[1].split()[0])
    assert rest >= 0


def test_дрібні_статті_не_засмічують_рядок():
    """Нуль по статті — нормальний стан (тихий прохід), і в рядку його нема."""
    tm = app.Timings()
    with tm.took("звіти"):
        pass
    assert "звіти" not in tm.line(0.5)


def test_run_заповнює_передану_розкладку(monkeypatch):
    """Головне: `run()` пише саме в той лічильник, який йому дали."""
    cfg = {
        "defaults": {"currency": "UAH", "analytics_enabled": False,
                     "seed_notify_max": 0, "watch_workers": 2,
                     "pause_between_watches": 0},
        "watches": [
            {"id": "a", "name": "A", "url": "https://www.olx.ua/uk/list/q-a/",
             "max_price": 600},
            {"id": "b", "name": "Б", "url": "https://www.olx.ua/uk/list/q-b/",
             "max_price": 600},
        ],
    }
    state = {"version": 1, "watches": {}}

    def fake_fetch(session, url, pages=1, **kw):
        time.sleep(0.01)
        return []

    monkeypatch.setattr(olx, "fetch_watch", fake_fetch)
    monkeypatch.setattr(app.olx, "fetch_watch", fake_fetch)
    monkeypatch.setattr(app.olx, "build_session", lambda *a, **k: object())

    tm = app.Timings()
    app.run(cfg, state, dry_run=True, reports=False, timings=tm)
    assert tm.get("видача", 0) >= 0.01, "забір видачі мав потрапити в розкладку"
