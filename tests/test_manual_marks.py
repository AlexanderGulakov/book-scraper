"""Ручна позначка «це російське видання».

Мову видання інколи видно лише з обкладинки: опис український, у заголовку ні
слова про мову. Тому позначку ставить людина кнопкою в Telegram, а скрапер
мусить її запам'ятати назавжди й більше до цього оголошення не повертатись.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as app  # noqa: E402
import notify  # noqa: E402


def _state():
    return {"version": 1, "watches": {
        "Ренсом Ріггз (автор)": {"seeded": True, "ads": {
            "77": {"title": "Книги Ренсома Ріґґз Дім дивних дітей", "price": 50,
                   "cur": "UAH", "seen": "2026-09-29T10:00:00+00:00",
                   "first": "2026-09-01T10:00:00+00:00", "url": "https://olx.ua/77"},
        }},
        "Ріггз: Дім дивних дітей (за серією)": {"seeded": True, "ads": {
            "77": {"title": "Книги Ренсома Ріґґз Дім дивних дітей", "price": 50,
                   "cur": "UAH", "seen": "2026-09-29T10:00:00+00:00",
                   "first": "2026-09-01T10:00:00+00:00", "url": "https://olx.ua/77"},
        }},
    }}


def _mark(monkeypatch, marks, offset_out=101):
    monkeypatch.setattr(notify, "poll_marks", lambda offset=None: (marks, offset_out))
    monkeypatch.setattr(notify, "confirm_mark", lambda *a, **k: None)


def test_mark_kills_the_ad_in_every_watch(monkeypatch):
    """Одне оголошення бачать два watch'і — гасити треба в обох, інакше воно
    лишиться «найдешевшим» через другий."""
    _mark(monkeypatch, [{"ad_id": "77", "callback_id": "c1",
                         "chat_id": 1, "message_id": 2}])
    state = _state()
    assert app.apply_marks(state) == 1
    for ws in state["watches"].values():
        assert ws["ads"]["77"]["an"] is False, "має випасти зі статистики"
        assert "77" in ws["dropped"], "має випасти з розгляду"
    assert "77" in state["manual_ru"]


def test_mark_is_idempotent(monkeypatch):
    """Кнопку можна натиснути двічі — і Telegram сам передоставляє апдейти,
    які ми прочитали, але не встигли зберегти."""
    _mark(monkeypatch, [{"ad_id": "77", "callback_id": "c1"}])
    state = _state()
    app.apply_marks(state)
    first = dict(state["manual_ru"])
    app.apply_marks(state)
    assert state["manual_ru"] == first


def test_offset_is_remembered_even_without_marks(monkeypatch):
    """Інакше кожен прогін перечитував би ті самі апдейти по колу."""
    monkeypatch.setattr(notify, "poll_marks", lambda offset=None: ([], 500))
    state = _state()
    assert app.apply_marks(state) == 0
    assert state["tg_offset"] == 500


def test_marked_ad_is_never_considered_again():
    """Позначка переживає prune: `dropped` чиститься за давністю, а
    `manual_ru` — ні. Інакше через місяць те саме оголошення повернулось би."""
    state = _state()
    state["manual_ru"] = {"77": "2026-01-01T00:00:00+00:00"}
    ws = state["watches"]["Ренсом Ріггз (автор)"]
    ws["dropped"] = {"77": "2026-01-01T00:00:00+00:00"}
    app.prune(ws, days=1)
    assert "77" not in ws["dropped"], "старий запис у dropped має піти"
    assert "77" in state["manual_ru"], "а ручна позначка — лишитись"


def test_button_is_attached_only_to_ad_messages():
    ad_msg = notify.Message("🆕 Нове оголошення", mark_id="77")
    plain = notify.Message("Пульс: живий")
    assert notify.mark_keyboard(ad_msg.mark_id)["inline_keyboard"][0][0]["callback_data"] == "ru:77"
    assert plain.mark_id is None
