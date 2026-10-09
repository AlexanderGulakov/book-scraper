"""Паралельний забір видачі і рання зупинка пагінації (2026-10-09).

Прохід на 45 watch'ах тривав 75 с, з них ~45 с — сон між запитами, а самі
запити нічого не знають один про одного. Тут перевіряється те, що на цьому
паралелізмі ламається тихо: спільна сесія, загублений watch, помилка однієї
видачі, яка потягла б за собою решту.
"""

import threading

import pytest

import main as app
import olx


def _ad(i):
    return olx.Ad(id=str(i), title=f"Книга {i}", url=f"https://olx.ua/{i}",
                  price=100.0, currency="UAH", price_text="100 грн", source="olx")


def _jobs(n):
    return [(f"w{i}", f"https://www.olx.ua/uk/q-{i}/", 1) for i in range(n)]


@pytest.mark.parametrize("workers", [1, 3])
def test_кожен_watch_забрано_рівно_раз(monkeypatch, workers):
    """Ні загублених, ні подвоєних запитів — скільки watch'ів, стільки видач."""
    calls = []
    lock = threading.Lock()

    def fake(session, url, pages=1, **kw):
        with lock:
            calls.append(url)
        return [_ad(url[-2])]

    monkeypatch.setattr(olx, "fetch_watch_api", fake)
    jobs = _jobs(7)
    out = app._prefetch_feeds(jobs, object(), workers=workers, pause=0,
                              search_api=True, api_fallback=[0])
    assert sorted(out) == sorted(k for k, _u, _p in jobs)
    assert len(calls) == len(jobs) == len(set(calls))


def test_помилка_однієї_видачі_не_валить_решту(monkeypatch):
    """Виняток повертаємо як значення — головний цикл обробить його сам.

    Інакше впав би весь прохід, і watch'і після зламаного мовчали б — рівно
    той сорт тиші, через який тут уже двічі горіли.
    """
    def fake(session, url, pages=1, **kw):
        if url.endswith("3/"):
            raise olx.OlxError("API відповів 403")
        return [_ad(1)]

    monkeypatch.setattr(olx, "fetch_watch_api", fake)
    monkeypatch.setattr(olx, "fetch_watch", fake)
    out = app._prefetch_feeds(_jobs(5), object(), workers=3, pause=0,
                              search_api=True, api_fallback=[0])
    assert isinstance(out["w3"], olx.OlxError)
    assert all(isinstance(out[f"w{i}"], list) for i in (0, 1, 2, 4))


def test_сесія_не_ділиться_між_потоками(monkeypatch):
    """curl_cffi тримає один curl-хендл: дві одночасні вимоги з однієї сесії —
    гонка, яка проявиться випадковим 403, а не падінням. Тому одна сесія —
    одне завдання за раз."""
    busy: dict[int, bool] = {}
    lock = threading.Lock()
    clashes = []

    def fake(session, url, pages=1, **kw):
        sid = id(session)
        with lock:
            if busy.get(sid):
                clashes.append(sid)
            busy[sid] = True
        # досить довго, щоб потоки справді перетнулись
        threading.Event().wait(0.02)
        with lock:
            busy[sid] = False
        return [_ad(1)]

    monkeypatch.setattr(olx, "fetch_watch_api", fake)
    monkeypatch.setattr(olx, "build_session", lambda *a, **k: object())
    app._prefetch_feeds(_jobs(12), object(), workers=4, pause=0,
                        search_api=True, api_fallback=[0])
    assert not clashes, "та сама сесія пішла у два потоки одночасно"


def test_відкат_на_html_рахується(monkeypatch):
    """Коли JSON-пошук не віддався, беремо HTML — і це має бути видно цифрою."""
    monkeypatch.setattr(olx, "fetch_watch_api",
                        lambda *a, **k: (_ for _ in ()).throw(olx.OlxError("не JSON")))
    monkeypatch.setattr(olx, "fetch_watch", lambda *a, **k: [_ad(1)])
    counter = [0]
    out = app._prefetch_feeds(_jobs(3), object(), workers=2, pause=0,
                              search_api=True, api_fallback=counter)
    assert counter[0] == 3
    assert all(isinstance(v, list) for v in out.values())


def test_прогін_бере_видачу_з_префетчу(monkeypatch):
    """`run()` має брати вже забрану видачу, а не ходити по неї ще раз.

    Самого числа запитів тут мало: послідовний цикл дав би те саме. Тому
    дивимось ще й на потоки — у префетчі вони робочі, у старому циклі все
    йшло з головного.
    """
    calls = []
    threads = set()

    def fake(s, url, pages=1, **k):
        calls.append(url)
        threads.add(threading.current_thread().name)
        return [_ad(1)]

    monkeypatch.setattr(olx, "fetch_watch_api", fake)
    cfg = {"defaults": {"currency": "UAH", "search_backend": "api",
                        "analytics_enabled": False, "seed_notify_max": 0,
                        "watch_workers": 2, "pause_between_watches": 0,
                        "needs_description": False},
           "watches": [{"id": f"w{i}", "name": f"Тест {i}",
                        "url": f"https://www.olx.ua/uk/q-{i}/", "max_price": 600}
                       for i in range(4)]}
    state = {"version": 1, "watches": {}}
    monkeypatch.setattr(olx, "build_session", lambda *a, **k: object())
    app.run(cfg, state, dry_run=True)
    assert len(calls) == 4, f"видачу забрали {len(calls)} разів замість 4"
    assert threads - {"MainThread"}, "жоден запит не пішов у робочий потік"


def test_пагінація_зупиняється_на_неповній_сторінці(monkeypatch):
    """Сторінка, коротша за ліміт, — це кінець видачі.

    Реальний випадок: «Клер: Кассандра Клер» з `pages: 3` брав другу сторінку
    (46 із 50) і йшов по третю, порожню, щоразу платячи за це запитом і
    паузою. Промо йдуть понад ліміт, тож повною вважається сторінка ≥ ліміту.
    """
    pages_asked = []

    class FakeSession:
        def get(self, url, **kw):
            import json as _json
            from urllib.parse import parse_qsl, urlparse
            q = dict(parse_qsl(urlparse(url).query))
            offset = int(q["offset"])
            pages_asked.append(offset)
            n = 50 if offset == 0 else 46
            return 200, _json.dumps({"data": [
                {"id": offset + i, "title": f"Книга {offset + i}", "url": "u",
                 "params": [], "photos": []} for i in range(n)]})

    ads = olx.fetch_watch_api(FakeSession(), "https://www.olx.ua/uk/hobbi-otdyh-i-sport/"
                              "knigi-zhurnaly/q-тест/", pages=3, pause=0)
    assert pages_asked == [0, 50], "третю сторінку просити не було за чим"
    assert len(ads) == 96


def test_повна_сторінка_з_промо_не_зупиняє(monkeypatch):
    """51 оголошення на ліміті 50 — це повна сторінка з промо, а не кінець."""
    pages_asked = []

    class FakeSession:
        def get(self, url, **kw):
            import json as _json
            from urllib.parse import parse_qsl, urlparse
            offset = int(dict(parse_qsl(urlparse(url).query))["offset"])
            pages_asked.append(offset)
            n = 51 if offset == 0 else 1
            return 200, _json.dumps({"data": [
                {"id": offset + i, "title": f"Книга {offset + i}", "url": "u",
                 "params": [], "photos": []} for i in range(n)]})

    olx.fetch_watch_api(FakeSession(), "https://www.olx.ua/uk/hobbi-otdyh-i-sport/"
                        "knigi-zhurnaly/q-тест/", pages=2, pause=0)
    assert pages_asked == [0, 50]
