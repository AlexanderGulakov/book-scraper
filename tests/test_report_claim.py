"""Звіт має надсилатись рівно один раз, скільки б прогонів не йшло паралельно.

Прогонів у проєкті два незалежні: OLX у GitHub Actions і Букфлі вдома
Планувальником Windows. Обидва читають одну базу й обидва рахують звіти.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage  # noqa: E402

mongomock = pytest.importorskip("mongomock")


@pytest.fixture()
def store():
    st = storage.MongoStore.__new__(storage.MongoStore)
    st.db = mongomock.MongoClient().db
    return st


def test_only_one_run_claims_the_report(store):
    """Той самий момент часу дістається одному — другий мовчить."""
    now = "2026-09-29T16:04:00+00:00"
    assert store.claim("sold_report_last", now) is True
    assert store.claim("sold_report_last", now) is False


def test_a_later_run_claims_again(store):
    """Наступна година — знову можна."""
    assert store.claim("sold_report_last", "2026-09-29T16:04:00+00:00") is True
    assert store.claim("sold_report_last", "2026-09-29T17:04:00+00:00") is True


def test_an_earlier_timestamp_never_wins(store):
    """Прогін із відсталим годинником не має права переграти свіжішу мітку."""
    assert store.claim("sold_report_last", "2026-09-29T17:00:00+00:00") is True
    assert store.claim("sold_report_last", "2026-09-29T16:00:00+00:00") is False


def test_claim_is_written_immediately_not_at_save(store):
    """Суть виправлення: мітка лягає в базу ОДРАЗУ, а не наприкінці прогону.
    Саме півторахвилинна щілина між перевіркою й записом і давала дублі."""
    store.claim("sold_report_last", "2026-09-29T16:04:00+00:00")
    doc = store.db.meta.find_one({"_id": "state"})
    assert doc["sold_report_last"] == "2026-09-29T16:04:00+00:00"


def test_storage_failure_stays_silent(store, monkeypatch):
    """Краще пропустити звіт, ніж надіслати дубль."""
    def boom(*a, **k):
        raise RuntimeError("Atlas недоступний")
    monkeypatch.setattr(store.db.meta, "find_one_and_update", boom)
    assert store.claim("sold_report_last", "2026-09-29T16:04:00+00:00") is False


def test_daily_report_uses_the_same_lock(store):
    assert store.claim("book_report_date", "2026-09-29") is True
    assert store.claim("book_report_date", "2026-09-29") is False
    assert store.claim("book_report_date", "2026-09-30") is True


# ─────────────────────────────────────────── --no-reports

def test_no_reports_silences_both_global_reports(monkeypatch):
    """Звіти глобальні: вони перебирають ВЕСЬ стан, а не watch'і цього прогону.
    Домашня джоба Букфлі складала їх нарівні з Actions — звідси дублі."""
    import main as app
    import olx

    called = []
    monkeypatch.setattr(app, "sold_report", lambda *a, **k: called.append("sold") or [])
    monkeypatch.setattr(app, "daily_book_report", lambda *a, **k: called.append("daily") or [])
    monkeypatch.setattr(app.olx, "build_session", lambda: None)
    monkeypatch.setattr(olx, "fetch_watch", lambda *a, **k: [])
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)

    cfg = {"defaults": {"sold_report_minutes": 1},
           "watches": [{"name": "Тест", "url": "u", "max_price": 100}]}

    app.run(cfg, {"version": 1, "watches": {}}, dry_run=False, reports=False)
    assert called == [], "з --no-reports не має бути жодного звіту"

    app.run(cfg, {"version": 1, "watches": {}}, dry_run=False, reports=True)
    assert "sold" in called and "daily" in called, "без прапорця — як раніше"
