"""Паралельна перевірка знятих оголошень.

Перевіряються не потоки як такі, а три речі, на яких паралелізм ламається
тихо: порядок результатів, цілісність стану і те, що сесію ніхто не ділить.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app  # noqa: E402
import olx  # noqa: E402


def _batch(n):
    return [("w", f"a{i}", {"url": f"https://www.olx.ua/d/uk/obyavlenie/x-ID{i}.html"})
            for i in range(n)]


def test_verdicts_keep_the_order_of_the_batch(monkeypatch):
    """pool.map зберігає порядок, але покластись на це треба явно: викликач
    зводить вердикти з оголошеннями через zip."""
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)
    monkeypatch.setattr(olx, "build_session", lambda: object())
    monkeypatch.setattr(olx, "ad_state",
                        lambda s, url: "gone" if url.endswith("ID3.html") else "alive")

    got = app._check_alive(_batch(8), object(), workers=4, pause=0)
    assert got == ["alive"] * 3 + ["gone"] + ["alive"] * 4


def test_one_session_is_never_used_by_two_threads_at_once(monkeypatch):
    """curl_cffi тримає всередині один curl-хендл: паралельні запити з однієї
    сесії — гонка, яка проявиться не тестом, а випадковим 403 раз на тиждень."""
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)
    monkeypatch.setattr(olx, "build_session", lambda: object())

    busy: dict[int, bool] = {}
    lock = threading.Lock()
    clashes = []

    def ad_state(session, url):
        sid = id(session)
        with lock:
            if busy.get(sid):
                clashes.append(sid)
            busy[sid] = True
        try:
            return "alive"
        finally:
            with lock:
                busy[sid] = False

    monkeypatch.setattr(olx, "ad_state", ad_state)
    app._check_alive(_batch(40), object(), workers=4, pause=0)
    assert not clashes, f"сесію ділили одночасно {len(clashes)} разів"


def test_a_single_worker_behaves_exactly_as_before(monkeypatch):
    """workers: 1 має лишатись точно старою послідовною поведінкою — це шлях
    відступу, якщо паралелізм колись почне ловити 403."""
    seen = []
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)
    monkeypatch.setattr(olx, "ad_state", lambda s, url: seen.append((s, url)) or "alive")

    sentinel = object()
    app._check_alive(_batch(5), sentinel, workers=1, pause=0)
    assert len(seen) == 5
    assert all(s is sentinel for s, _ in seen), "мала використовуватись передана сесія"


def test_state_is_only_touched_on_the_main_thread(monkeypatch):
    """_check_alive не сміє чіпати стан: інакше два потоки видаляли б з одного
    словника. Перевіряємо, що батч приходить назад незмінним."""
    monkeypatch.setattr(app.time, "sleep", lambda *_: None)
    monkeypatch.setattr(olx, "build_session", lambda: object())
    monkeypatch.setattr(olx, "ad_state", lambda s, url: "gone")

    batch = _batch(6)
    before = [dict(rec) for _, _, rec in batch]
    app._check_alive(batch, object(), workers=3, pause=0)
    assert [rec for _, _, rec in batch] == before
