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
    plain = notify.Message("Пульс: живий")
    assert plain.mark_id is None and plain.cheap_id is None
    assert notify.mark_keyboard(plain.mark_id, plain.cheap_id) is None


def test_both_ads_in_a_notification_can_be_marked():
    """Головне тут: російським буває САМЕ «найдешевше зараз», а не оголошення,
    про яке прийшло сповіщення. Перша версія вішала одну кнопку на оголошення
    й позначила б українське видання за 350 замість російського за 50."""
    kb = notify.mark_keyboard("88", "77")
    data = [b["callback_data"] for b in kb["inline_keyboard"][0]]
    assert data == ["ru:88", "ru:77"]


def test_no_second_button_when_the_cheapest_is_the_ad_itself():
    kb = notify.mark_keyboard("88", "88")
    assert len(kb["inline_keyboard"][0]) == 1


def test_clicking_one_button_leaves_the_other():
    """Натиснути можуть обидві кнопки поспіль. Якщо після першого натискання
    підмінити всю клавіатуру підписом, друга зникне назавжди."""
    kb = notify.mark_keyboard("88", "77")
    after = notify._keyboard_after_click({"data": "ru:77", "markup": kb})
    left = [b["callback_data"] for row in after["inline_keyboard"] for b in row]
    assert "ru:88" in left, "друга кнопка має лишитись"
    assert "ru:77" not in left, "натиснута — зникнути"

    again = notify._keyboard_after_click({"data": "ru:88", "markup": after})
    left = [b["callback_data"] for row in again["inline_keyboard"] for b in row]
    ticks = [c for c in left if c == "noop"]
    assert len(ticks) == 1, "галочка має бути одна, а не дві"
    # Кнопки другої причини натискання першої не чіпає: це різні твердження.
    assert {"bad:88", "bad:77"} <= set(left)


def test_the_two_reasons_are_separate_buttons():
    """«Російське видання» і «не враховувати» дають однаковий наслідок, але це
    різні причини, і в базі вони мають лягти по-різному."""
    kb = notify.mark_keyboard("88", "77")
    rows = kb["inline_keyboard"]
    assert len(rows) == 2, "по рядку на причину"
    assert [b["callback_data"] for b in rows[0]] == ["ru:88", "ru:77"]
    assert [b["callback_data"] for b in rows[1]] == ["bad:88", "bad:77"]
    # Без «найдешевшого» — по одній кнопці в рядку, а не порожній рядок.
    solo = notify.mark_keyboard("88")
    assert [len(r) for r in solo["inline_keyboard"]] == [1, 1]


def test_a_click_says_what_it_actually_did():
    """Підпис галочки бере причину з натискання: сказати «позначено російським»
    там, де людина натиснула «не враховувати», — це збрехати про зроблене."""
    kb = notify.mark_keyboard("88")
    after = notify._keyboard_after_click({"data": "bad:88", "why": "bad", "markup": kb})
    tick = [b for row in after["inline_keyboard"] for b in row
            if b["callback_data"] == "noop"][0]
    assert "не враховується" in tick["text"]


# ─────────────────────────────────────────── id з посилання

def test_the_id_in_the_url_is_not_the_id_in_the_data():
    """Справжній баг: `--mark-ru <посилання>` клав у базу «11lZx2», а стан
    ключується числом 936150588 — тобто позначка нікуди не потрапляла.
    У посиланні OLX пише base62 з алфавітом 0-9a-zA-Z."""
    import olx
    url = ("https://www.olx.ua/d/uk/obyavlenie/"
           "prodam-komplekt-knig-garr-potter-ID11lZx2.html")
    assert olx.ad_id_from(url) == "936150588"
    assert olx.ad_id_from("11lZx2") == "936150588", "голий код теж має працювати"
    assert olx.ad_id_from("936150588") == "936150588", "готовий id не чіпаємо"
    # Не розпізнав — так і кажемо. Мовчки покласти в базу сміття гірше:
    # виглядатиме, ніби справу зроблено.
    assert olx.ad_id_from("сміття") is None
    assert olx.ad_id_from("https://olx.ua/") is None
    assert olx.ad_id_from("") is None


# ─────────────────────────────────────────── викреслити назавжди

def _state_with_ad(ad_id="42"):
    return {
        "watches": {
            "Кідрук": {"ads": {ad_id: {"title": "Колонія", "price": 300.0}}},
            "Букфлі": {"ads": {ad_id: {"title": "Колонія", "price": 300.0}}},
        },
        "sold": [{"id": ad_id, "watch": "Кідрук", "price": 300.0}],
    }


def test_excluding_kills_the_ad_everywhere_at_once():
    """Оголошення живе в кількох watch'ах одразу — гасити треба в усіх, інакше
    воно повернеться в статистику з того watch'а, якого не зачепили."""
    import main
    state = _state_with_ad()
    assert main.exclude_ads(state, ["42"], why="bad") == 1

    for ws in state["watches"].values():
        assert ws["ads"]["42"]["an"] is False
        assert "42" in ws["dropped"], "і в майбутніх прогонах теж не розглядати"
    assert state["sold"][0]["an"] is False, "і з історії продажів"
    assert state["manual_ru"]["42"]["why"] == "bad"


def test_excluding_twice_changes_nothing():
    import main
    state = _state_with_ad()
    main.exclude_ads(state, ["42"], why="bad")
    assert main.exclude_ads(state, ["42"], why="bad") == 0, "нових позначок нема"
    assert len(state["manual_ru"]) == 1


def test_an_old_string_mark_keeps_its_meaning():
    """Раніше в `manual_ru` лежав просто рядок з датою, і означав «російське».
    Перезаписати його як «bad» означало б задним числом переписати причину."""
    import main
    state = _state_with_ad()
    state["manual_ru"] = {"42": "2026-09-01T00:00:00+00:00"}
    main.exclude_ads(state, ["42"], why="bad")
    assert state["manual_ru"]["42"] == "2026-09-01T00:00:00+00:00"


def test_the_file_tool_reads_links_ids_and_comments(tmp_path):
    """Файл зі списком — це те, що людина збирає тижнями, тож він мусить
    терпіти коментарі, порожні рядки й будь-який із трьох записів id."""
    import exclude
    f = tmp_path / "excluded.txt"
    f.write_text(
        "https://www.olx.ua/d/uk/obyavlenie/x-ID11lZx2.html  # не відкривається\n"
        "\n"
        "# цілий рядок-коментар\n"
        "936373155\n"
        "сміття\n", encoding="utf-8")
    rows = exclude.read_list(f)
    assert [r[1] for r in rows] == ["936150588", "936373155", None]


def test_check_shows_every_place_the_ad_lives(capsys):
    """Стан розкиданий по чотирьох місцях, і «чи враховується це оголошення»
    не читається з жодного окремо. Команда має показати всі одразу — зокрема
    розбіжність, коли позначка лягла в один watch і не лягла в інший."""
    import exclude
    state = {
        "manual_ru": {"42": {"at": "2026-09-30T06:00:00+00:00", "why": "bad"}},
        "watches": {
            "А": {"ads": {"42": {"price": 100.0, "cur": "UAH", "an": False,
                                 "title": "Книжка"}},
                  "dropped": {"42": "2026-09-30T06:00:00+00:00"}},
            "Б": {"ads": {"42": {"price": 100.0, "cur": "UAH", "title": "Книжка"}}},
        },
        "sold": [{"id": "42", "watch": "А", "price": 100.0, "days": 3.0,
                  "gone": "2026-09-30T07:00:00+00:00"}],
    }
    assert exclude.check_one(state, "42") == 0
    out = capsys.readouterr().out
    assert "недійсне" in out
    assert "watch «А»" in out and "watch «Б»" in out
    assert "НІ (an: false)" in out, "де позначка лягла"
    assert "історія зняття" in out


def test_check_says_plainly_when_there_is_nothing():
    """«Нічого не знайшов» — теж відповідь, і краща за порожній екран, з якого
    незрозуміло, чи то оголошення чисте, чи то команда не спрацювала."""
    import exclude
    assert exclude.check_one({"watches": {}}, "42") == 0
    assert exclude.check_one({"watches": {}}, "сміття") == 2


# ─────────────────────────────────────────── скасувати випадкове натискання

def test_undo_brings_the_ad_back():
    """Кнопка велика, палець один — натиснути не те легко. Скасування має
    повернути оголошення рівно туди, де воно було."""
    import main
    state = _state_with_ad()
    main.exclude_ads(state, ["42"], why="bad")
    assert main.restore_ads(state, ["42"]) == 1

    assert "42" not in state["manual_ru"]
    for ws in state["watches"].values():
        assert "an" not in ws["ads"]["42"], "знову рахується"
        assert "42" not in (ws.get("dropped") or {})
    assert "an" not in state["sold"][0]


def test_undo_does_not_resurrect_what_the_rules_killed():
    """🔑 `an: false` ставить ще й шар правил — комплектам і лотам без ціни.
    Якщо скасування зніме його наосліп, комплект за 1100 грн потрапить у
    медіану окремих книжок і зіпсує її. Тому позначка пам'ятає, чи оголошення
    рахувалось ДО неї."""
    import main
    state = _state_with_ad()
    for ws in state["watches"].values():
        ws["ads"]["42"]["an"] = False          # так вирішили правила, не людина
    main.exclude_ads(state, ["42"], why="ru")
    assert state["manual_ru"]["42"]["an0"] is False

    main.restore_ads(state, ["42"])
    for ws in state["watches"].values():
        assert ws["ads"]["42"]["an"] is False, "правила лишаються в силі"


def test_undo_of_something_unmarked_changes_nothing():
    import main
    state = _state_with_ad()
    assert main.restore_ads(state, ["42"]) == 0


# ─────────────────────────────────────────── дубль із двох пошуків

class _Ad:
    def __init__(self, ad_id, price):
        self.id, self.price = ad_id, price


def test_the_same_ad_is_announced_once_across_runs():
    """Реальні дублі 30.09: одне оголошення за 500 грн приїхало о 16:23 від
    «Поттер: Філософський камінь» і о 16:31 від «Гаррі Поттер». Набір
    `announced` цього не ловив — він жив лише в межах прогону, а watch'і мають
    різні інтервали й зустрічають оголошення в різних прогонах."""
    import main
    state: dict = {}
    ad = _Ad("500", 500.0)
    assert main.told_before(state, ad, "new") is False
    main.remember_told(state, ad, "2026-09-30T16:23:00+00:00")
    assert main.told_before(state, ad, "new") is True


def test_a_further_price_drop_is_still_worth_a_message():
    """Але здешевлення — окрема подія: мовчати про неї тільки тому, що про
    оголошення вже казали, означало б проґавити саме те, заради чого стежимо."""
    import main
    state: dict = {}
    main.remember_told(state, _Ad("500", 500.0), "2026-09-30T16:23:00+00:00")
    assert main.told_before(state, _Ad("500", 500.0), "drop") is True, "та сама ціна"
    assert main.told_before(state, _Ad("500", 400.0), "drop") is False, "подешевшало"


def test_memory_of_messages_expires_like_the_ads_themselves():
    """Інакше мапа росла б вічно: за рік це десятки тисяч записів у одному
    документі meta."""
    import main
    state = {"told": {
        "old": {"at": "2020-01-01T00:00:00+00:00", "price": 1.0},
        "new": {"at": "2026-09-30T00:00:00+00:00", "price": 2.0},
    }}
    assert main.prune_told(state, 30) == 1
    assert list(state["told"]) == ["new"]


def test_the_olx_alphabet_swaps_v_and_w():
    """🔑 Код у посиланні — НЕ звичайний base62: в алфавіті OLX `v` і `w`
    переставлені місцями, і в нижньому регістрі, і у верхньому.

    Схоже на описку в їхній константі (…stu**wv**xyz, …STU**WV**XYZ), але
    стабільну: перевірено 2026-10-02 на 2889 парах (id ↔ код) з живої видачі —
    звичайний base62 дав 734 помилки, переставлені лише великі — 278,
    обидва — жодної.

    Чому це взагалі помітили: `--exclude <посилання>` на коді з `v` або `w`
    клав у базу ключ неіснуючого оголошення. Без помилки, без ефекту — рівно
    та тиша, яку неможливо помітити, поки не звіриш із живими даними.
    """
    import olx
    # Усі п'ять — живі пари, зняті з API OLX 2026-10-02.
    assert olx.ad_id_from("-IDV5hGi.html") == "858287098"    # велика V
    assert olx.ad_id_from("-ID11nWTh.html") == "936613247"   # велика W
    assert olx.ad_id_from("-IDvdI1a.html") == "476110224"    # мала v
    assert olx.ad_id_from("-IDCwjiN.html") == "568963137"    # мала w
    assert olx.ad_id_from("-ID11lZx2.html") == "936150588"   # без v/w — не зачеплено
