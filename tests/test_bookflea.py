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


def test_survives_broken_tree_and_renamed_classes():
    """Картка має читатись навіть якщо дерево DOM зібралось криво."""
    broken = HTML.replace("</a>", "")          # парсер не закрив посилання
    renamed = HTML.replace("text-sm", "zz").replace("text-base", "yy")  # інші класи

    for label, variant in (("зламане дерево", broken), ("інші класи", renamed)):
        ads = bookflea.parse_listings(variant)
        titles = {a.title for a in ads}
        assert "(без назви)" not in titles, f"{label}: картка лишилась порожньою"
        assert "Відьмак. Останнє бажання. Книга 1" in titles, label
        brown = next(a for a in ads if a.id == "i7z3xyj5k6kxyyjkuzhi17a4")
        assert bookflea.matches_keyword(brown, KEYWORDS) == "Ден Браун", label


def test_polish_market_prices_are_not_mistaken_for_hryvnia():
    """Букфлі має ще й польський ринок: 220 zł не сміє пройти як 220 грн."""
    import olx
    from models import Ad

    assert bookflea._parse_price("220 zł")[:2] == (220.0, "PLN")
    assert bookflea._parse_price("33 zł")[:2] == (33.0, "PLN")
    assert bookflea._parse_price("99 PLN")[:2] == (99.0, "PLN")
    assert bookflea._parse_price("1 250 грн")[:2] == (1250.0, "UAH")
    # без позначки валюту НЕ вгадуємо
    assert bookflea._parse_price("77")[1] is None

    def ad(cur):
        return Ad(id="x", title="t", url="u", price=220, currency=cur, price_text="220")

    assert olx.price_ok(ad("UAH"), max_price=2000, min_price=None, currency="UAH") is True
    assert olx.price_ok(ad("PLN"), max_price=2000, min_price=None, currency="UAH") is False
    assert olx.price_ok(ad(None), max_price=2000, min_price=None, currency="UAH") is False
    # без фільтра валюти все одно проходить
    assert olx.price_ok(ad("PLN"), max_price=2000, min_price=None, currency=None) is True


def test_parse_from_raw_is_tree_independent():
    data = bookflea.parse_from_raw(HTML)
    assert data["u6x6e2e9y23tc3o84ln658dy"]["title"] == "Відьмак. Останнє бажання. Книга 1"
    assert data["u6x6e2e9y23tc3o84ln658dy"]["author"] == "Анджей Сапковський"
    assert data["t0g4szaa5e8ynkktc804kxam"]["price"] == "1 250 грн"
    # картка, де лишився лише alt
    assert data["onlyaltfallback00000001"]["title"] == "Фундація"
    assert data["onlyaltfallback00000001"]["author"] == "Айзек Азімов"


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


AUTHED = "Мій профіль | Букфлі"
ANON = "Вхід і реєстрація | Букфлі"


class FakeSession:
    """Мінімальний двійник Fetcher: пам'ятає виклики, нічого нікуди не шле.

    `me_text` може бути списком — тоді кожен наступний GET /me віддає наступне
    значення (щоб зіграти «кукі мертва → після логіну живий профіль»).
    """

    def __init__(self, login_status=200, me_text=AUTHED):
        self.login_status = login_status
        self.me_texts = list(me_text) if isinstance(me_text, list) else [me_text]
        self.posts: list[tuple[str, dict]] = []
        self.cookies: list[tuple[str, str | None]] = []

    def post_json(self, url, payload, **kw):
        self.posts.append((url, payload))
        body = '{"ok":true}' if self.login_status == 200 else '{"error":"Invalid credentials"}'
        return self.login_status, body

    def get(self, url, **kw):
        text = self.me_texts[0] if len(self.me_texts) == 1 else self.me_texts.pop(0)
        return 200, text

    def set_cookie_header(self, host, value):
        self.cookies.append((host, value))


def test_ready_cookie_skips_login(monkeypatch):
    """Жива BOOKFLEA_COOKIE — і жодного запиту на логін."""
    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("skip", ""))
    monkeypatch.setenv("BOOKFLEA_COOKIE", "session=abc123; theme=dark")
    monkeypatch.setenv("BOOKFLEA_EMAIL", "a@b.c")
    monkeypatch.setenv("BOOKFLEA_PASSWORD", "secret")
    s = FakeSession()

    assert bookflea.ensure_login(s) is True
    assert s.posts == [], "живу кукі не міняємо на новий логін"
    assert s.cookies == [("bookflea.co", "session=abc123; theme=dark")]
    assert bookflea.LOGIN_STATE[0] == "cookie"


def test_stale_cookie_falls_back_to_login(monkeypatch):
    """Протухла кукі: прибрати її й залогінитись паролем."""
    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("skip", ""))
    monkeypatch.setenv("BOOKFLEA_COOKIE", "session=old")
    monkeypatch.setenv("BOOKFLEA_EMAIL", "a@b.c")
    monkeypatch.setenv("BOOKFLEA_PASSWORD", "secret")
    s = FakeSession(me_text=[ANON, AUTHED])

    assert bookflea.ensure_login(s) is True
    assert s.posts == [(bookflea.LOGIN, {"email": "a@b.c", "password": "secret"})]
    # мертву кукі прибрали, інакше вона перебивала б свіжу з логіну
    assert s.cookies == [("bookflea.co", "session=old"), ("bookflea.co", None)]
    assert bookflea.LOGIN_STATE[0] == "relogin", "привід нагадати про оновлення секрету"


def test_bare_token_is_accepted_as_cookie():
    """Сесія Букфлі — одна кукі accessToken, тож голе значення теж має працювати."""
    assert bookflea.normalize_cookie("  eyJhbGciOi.abc.def ") == "accessToken=eyJhbGciOi.abc.def"
    assert bookflea.normalize_cookie("accessToken=abc; _gcl_au=1") == "accessToken=abc; _gcl_au=1"
    assert bookflea.normalize_cookie("accessToken=abc;") == "accessToken=abc"
    assert bookflea.normalize_cookie("") == ""


def test_cookie_expiry_read_from_jwt():
    import base64 as b64

    def jwt(exp):
        part = b64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": "u1"}).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{part}.c2lnbmF0dXJlXzEyMzQ1Ng"

    when = bookflea.cookie_expiry("accessToken=" + jwt(1800000000))
    assert when is not None and when.year == 2027 and when.tzinfo is not None

    # не JWT або без exp — просто нічого не знаємо, і це не помилка
    assert bookflea.cookie_expiry("accessToken=plain-opaque-value") is None
    assert bookflea.cookie_expiry("accessToken=aaaa.bbbb.cccc") is None
    assert bookflea.cookie_expiry("") is None


def test_stale_cookie_without_password_is_reported(monkeypatch):
    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("skip", ""))
    monkeypatch.setenv("BOOKFLEA_COOKIE", "session=old")
    monkeypatch.delenv("BOOKFLEA_EMAIL", raising=False)
    monkeypatch.delenv("BOOKFLEA_PASSWORD", raising=False)

    assert bookflea.ensure_login(FakeSession(me_text=ANON)) is False
    assert bookflea.LOGIN_STATE[0] == "fail", "це вже привід написати в Telegram"


def test_cookie_header_stays_on_its_own_domain():
    """Сесія спільна з OLX — кукі Букфлі не сміє поїхати на olx.ua."""
    from fetcher import Fetcher

    f = Fetcher(backend="requests")
    f.set_cookie_header("bookflea.co", "session=abc")
    assert f._cookie_for("https://www.bookflea.co/search?q=x") == "session=abc"
    assert f._cookie_for("https://bookflea.co/me") == "session=abc"
    assert f._cookie_for("https://www.olx.ua/uk/") is None
    f.set_cookie_header("bookflea.co", None)
    assert f._cookie_for("https://www.bookflea.co/") is None


def test_login_posts_credentials_and_verifies(monkeypatch):
    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("skip", ""))
    monkeypatch.delenv("BOOKFLEA_COOKIE", raising=False)
    monkeypatch.setenv("BOOKFLEA_EMAIL", "a@b.c")
    monkeypatch.setenv("BOOKFLEA_PASSWORD", "secret")
    s = FakeSession()

    assert bookflea.ensure_login(s) is True
    assert s.posts == [(bookflea.LOGIN, {"email": "a@b.c", "password": "secret"})]
    assert bookflea.LOGIN_STATE[0] == "ok"

    bookflea.ensure_login(s)
    assert len(s.posts) == 1, "вдруге за прогін мережу не чіпаємо"


def test_login_failures_are_survivable(monkeypatch):
    # немає облікових даних — просто йдемо анонімно
    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("skip", ""))
    monkeypatch.delenv("BOOKFLEA_COOKIE", raising=False)
    monkeypatch.delenv("BOOKFLEA_EMAIL", raising=False)
    monkeypatch.delenv("BOOKFLEA_PASSWORD", raising=False)
    s = FakeSession()
    assert bookflea.ensure_login(s) is False
    assert s.posts == []
    assert bookflea.LOGIN_STATE[0] == "skip"

    # невірний пароль
    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("skip", ""))
    monkeypatch.delenv("BOOKFLEA_COOKIE", raising=False)
    monkeypatch.setenv("BOOKFLEA_EMAIL", "a@b.c")
    monkeypatch.setenv("BOOKFLEA_PASSWORD", "wrong")
    assert bookflea.ensure_login(FakeSession(login_status=401)) is False
    assert bookflea.LOGIN_STATE[0] == "fail"

    # сервер сказав 200, але /me все одно віддає сторінку входу
    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("skip", ""))
    assert bookflea.ensure_login(FakeSession(me_text=ANON)) is False
    assert bookflea.LOGIN_STATE[0] == "fail"


def test_expiring_token_reminder_fires_once(monkeypatch):
    """Про токен, що спливає, нагадати один раз, а не щопрогону."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("cookie", "жива"))
    monkeypatch.setattr(bookflea, "COOKIE_EXPIRES_AT",
                        datetime.now(timezone.utc) + timedelta(days=3, hours=1))
    monkeypatch.setattr(bookflea, "_get", lambda *a, **k: HTML)
    monkeypatch.setattr(app.olx, "build_session", lambda: None)

    cfg = {"defaults": {}, "watches": [{
        "id": "bf", "name": "Букфлі", "source": "bookflea",
        "mode": "latest", "keywords": KEYWORDS,
    }]}
    state = {"version": 1, "watches": {}}

    msgs, _ = app.run(cfg, state, dry_run=False)
    assert any("спливає" in m and "3 дн" in m for m in msgs)

    msgs, _ = app.run(cfg, state, dry_run=False)
    assert not any("спливає" in m for m in msgs)

    # свіжий токен — і нагадування замовкає
    monkeypatch.setattr(bookflea, "COOKIE_EXPIRES_AT",
                        datetime.now(timezone.utc) + timedelta(days=30))
    msgs, _ = app.run(cfg, state, dry_run=False)
    assert not any("спливає" in m for m in msgs)


def test_foreign_market_warns_once_and_sends_nothing(monkeypatch):
    """Каталог іншої країни: попередити один раз і НЕ слати «55 грн» за 55 zł."""
    monkeypatch.setattr(bookflea, "LOGIN_STATE", ("skip", ""))
    monkeypatch.delenv("BOOKFLEA_COOKIE", raising=False)
    monkeypatch.delenv("BOOKFLEA_EMAIL", raising=False)
    monkeypatch.delenv("BOOKFLEA_PASSWORD", raising=False)
    polish = HTML.replace("грн", "zł")
    monkeypatch.setattr(bookflea, "_get", lambda *a, **k: polish)

    cfg = {"defaults": {}, "watches": [{
        "id": "bf", "name": "Букфлі", "source": "bookflea",
        "mode": "latest", "keywords": KEYWORDS, "max_price": 2000,
    }]}
    state = {"version": 1, "watches": {}}

    msgs, _ = app.run(cfg, state, dry_run=False)          # перший прогін (seed)
    assert any("каталог іншої країни" in m for m in msgs)
    assert bookflea.LAST_SCAN["currencies"] == {"PLN"}

    msgs, _ = app.run(cfg, state, dry_run=False)          # стан не змінився
    assert not any("каталог іншої країни" in m for m in msgs), "не спамимо щопівгодини"
    assert not any("грн" in m for m in msgs), "жодне польське оголошення не пішло як гривневе"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
