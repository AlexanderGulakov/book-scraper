"""Тести шару зберігання.

Перевіряють не CRUD, а рішення, заради яких Mongo взагалі з'явилась:
що формат стану в пам'яті не змінився, що пишеться лише різниця, що два
писачі не затирають одне одного, що історія цін накопичується, і що
`--reset` не зносить те, чого не можна перескрапити.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import storage  # noqa: E402

mongomock = pytest.importorskip("mongomock", reason="pip install mongomock")


def fresh_store() -> storage.MongoStore:
    return storage.MongoStore("mongodb://ignored", "test_db",
                              client=mongomock.MongoClient())


def sample_state() -> dict:
    return {
        "version": 1,
        "book_report_date": "2026-09-22",
        "sold_report_last": "2026-09-22T14:01:29+00:00",
        "watches": {
            "Стівен Кінг: Талісман": {
                "seeded": True,
                "last_run": "2026-09-22T14:00:00+00:00",
                "ads": {
                    "111": {"price": 250.0, "cur": "UAH", "title": "Талісман",
                            "seen": "2026-09-22T14:00:00+00:00",
                            "first": "2026-09-20T10:00:00+00:00",
                            "url": "https://www.olx.ua/d/uk/obyavlenie/talisman-ID1.html"},
                    "222": {"price": 400.0, "cur": "UAH", "title": "Талісман, тверда",
                            "seen": "2026-09-22T14:00:00+00:00",
                            "first": "2026-09-21T10:00:00+00:00",
                            "url": "https://www.olx.ua/d/uk/obyavlenie/talisman2-ID2.html"},
                },
            },
            "Букфлі": {
                "seeded": True,
                "last_run": "2026-09-22T13:30:00+00:00",
                "window": ["abc", "def"],
                "ads": {
                    "abc": {"price": 55.0, "cur": "UAH", "title": "Служниця",
                            "seen": "2026-09-22T13:30:00+00:00"},
                },
            },
        },
        "sold": [
            {"id": "933340741", "watch": "Роберт Ґалбрейт (Страйк)",
             "title": "Людина з клеймом", "price": 700.0, "cur": "UAH",
             "days": 19.7, "gone": "2026-09-19T10:02:56+00:00",
             "url": "https://www.olx.ua/d/uk/obyavlenie/x-ID3.html"},
        ],
    }


# ── формат ──────────────────────────────────────────────────────────────────

def test_mongo_returns_exactly_the_shape_the_file_had():
    """Увесь решта коду мутує `state` як раніше — форма мусить збігатись."""
    store = fresh_store()
    original = sample_state()
    store.save(original)

    reader = storage.MongoStore("mongodb://ignored", "test_db", client=store.client)
    assert reader.load() == original


def test_json_store_round_trip(tmp_path):
    path = tmp_path / "state.json"
    store = storage.JsonStore(path)
    assert store.load() == {"version": 1, "watches": {}}
    store.save(sample_state())
    assert storage.JsonStore(path).load() == sample_state()


def test_a_broken_file_does_not_kill_the_run(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ це не json", encoding="utf-8")
    assert storage.JsonStore(path).load() == {"version": 1, "watches": {}}


# ── діф ─────────────────────────────────────────────────────────────────────

def test_save_writes_only_what_changed():
    """Головна вимога: не «файл у хмарі», а точковий запис.

    Якби save() писав увесь стан, лічильник показав би всі три оголошення.
    """
    store = fresh_store()
    state = sample_state()
    store.save(state)

    state["watches"]["Стівен Кінг: Талісман"]["ads"]["111"]["price"] = 199.0
    state["watches"]["Стівен Кінг: Талісман"]["ads"]["111"]["seen"] = "2026-09-22T15:00:00+00:00"
    counts = store.save(state)

    assert counts["ads"] == 1
    assert counts["ads_deleted"] == 0
    assert counts["watches"] == 0          # службові поля watch'а не чіпали


def test_two_writers_do_not_clobber_each_other():
    """Заради цього все й робилось.

    Actions крутить OLX, домашня машина — Букфлі. Обидва читають увесь стан
    (аналітиці потрібні обидва джерела), але пишуть у різні документи.
    З одним JSON-файлом другий запис затирав би перший.
    """
    shared = mongomock.MongoClient()
    storage.MongoStore("mongodb://x", "test_db", client=shared).save(sample_state())

    actions = storage.MongoStore("mongodb://x", "test_db", client=shared)
    home = storage.MongoStore("mongodb://x", "test_db", client=shared)
    st_actions, st_home = actions.load(), home.load()

    # Обидва бачать увесь стан — саме те, чого не було з двома файлами.
    assert "Букфлі" in st_actions["watches"]
    assert "Стівен Кінг: Талісман" in st_home["watches"]

    st_actions["watches"]["Стівен Кінг: Талісман"]["ads"]["333"] = {
        "price": 180.0, "cur": "UAH", "title": "Новий лот",
        "seen": "2026-09-22T15:00:00+00:00", "first": "2026-09-22T15:00:00+00:00"}
    st_home["watches"]["Букфлі"]["ads"]["ghi"] = {
        "price": 95.0, "cur": "UAH", "title": "Кідрук",
        "seen": "2026-09-22T15:01:00+00:00"}

    actions.save(st_actions)
    home.save(st_home)

    final = storage.MongoStore("mongodb://x", "test_db", client=shared).load()
    assert "333" in final["watches"]["Стівен Кінг: Талісман"]["ads"]
    assert "ghi" in final["watches"]["Букфлі"]["ads"]


def test_a_pruned_ad_leaves_the_base():
    store = fresh_store()
    state = sample_state()
    store.save(state)

    del state["watches"]["Стівен Кінг: Талісман"]["ads"]["222"]
    counts = store.save(state)

    assert counts["ads_deleted"] == 1
    assert store.db[storage.COLL_ADS].count_documents({}) == 2


def test_an_orphan_watch_takes_its_ads_with_it():
    """`forget_orphans` прибирає перейменований watch — оголошення не мають
    лишатись сиротами в базі, інакше вони вічно там висітимуть."""
    store = fresh_store()
    state = sample_state()
    store.save(state)

    del state["watches"]["Букфлі"]
    store.save(state)

    assert store.db[storage.COLL_WATCHES].count_documents({}) == 1
    assert store.db[storage.COLL_ADS].count_documents({"watch": "Букфлі"}) == 0


def test_watch_names_with_punctuation_survive_as_ids():
    """_id складений (`{w, a}`), а не рядок із роздільником — саме тому, що
    назви watch'ів українські й довільні."""
    store = fresh_store()
    state = storage.empty_state()
    state["watches"] = {
        "Клер: Місто скла | том 3": {
            "seeded": True,
            "ads": {"a|b:c": {"price": 100.0, "cur": "UAH", "title": "Т",
                              "seen": "2026-09-22T10:00:00+00:00"}},
        }
    }
    store.save(state)
    assert store.load() == state


# ── історія цін ─────────────────────────────────────────────────────────────

def test_price_history_records_a_change_and_ignores_a_repeat():
    """Те, чого у файловому стані не існувало: рядок на кожну зміну ціни."""
    store = fresh_store()
    state = sample_state()
    store.save(state)
    assert store.db[storage.COLL_PRICES].count_documents({"adId": "111"}) == 1

    # Та сама ціна, новий прогін — нового спостереження бути не має.
    state["watches"]["Стівен Кінг: Талісман"]["ads"]["111"]["seen"] = "2026-09-22T15:00:00+00:00"
    store.save(state)
    assert store.db[storage.COLL_PRICES].count_documents({"adId": "111"}) == 1

    # Подешевшало — з'являється друге.
    state["watches"]["Стівен Кінг: Талісман"]["ads"]["111"]["price"] = 150.0
    state["watches"]["Стівен Кінг: Талісман"]["ads"]["111"]["seen"] = "2026-09-22T16:00:00+00:00"
    store.save(state)

    prices = sorted(d["price"] for d in store.db[storage.COLL_PRICES].find({"adId": "111"}))
    assert prices == [150.0, 250.0]


def test_price_history_outlives_the_ad():
    """Оголошення зникає — історія його цін лишається. Інакше вся затія
    з базою не дала б аналітиці нічого нового."""
    store = fresh_store()
    state = sample_state()
    store.save(state)
    del state["watches"]["Стівен Кінг: Талісман"]["ads"]["111"]
    store.save(state)

    assert store.db[storage.COLL_ADS].count_documents({"adId": "111"}) == 0
    assert store.db[storage.COLL_PRICES].count_documents({"adId": "111"}) == 1


# ── reset і історія знятих ──────────────────────────────────────────────────

def test_reset_forgets_ads_but_keeps_sold_history():
    """У файлі `--reset` зносив і `sold` разом із рештою. У базі це було б
    втратою єдиного, чого не перескрапиш."""
    store = fresh_store()
    store.save(sample_state())

    state = store.reset()

    assert state["watches"] == {}
    assert len(state["sold"]) == 1
    assert store.db[storage.COLL_ADS].count_documents({}) == 0
    assert store.db[storage.COLL_PRICES].count_documents({}) > 0


def test_sold_survives_a_re_listing_of_the_same_ad():
    """Те саме оголошення можуть зняти, виставити знову і зняти вдруге —
    це два спостереження, а не одне. Тому `gone` входить у ключ."""
    store = fresh_store()
    state = sample_state()
    store.save(state)

    again = dict(state["sold"][0])
    again["gone"] = "2026-09-25T10:00:00+00:00"
    again["price"] = 650.0
    state["sold"].append(again)
    store.save(state)

    assert store.db[storage.COLL_SOLD].count_documents({"adId": "933340741"}) == 2


def test_sold_pruned_by_cutoff_disappears():
    store = fresh_store()
    state = sample_state()
    store.save(state)
    state["sold"] = []
    counts = store.save(state)
    assert counts["sold_deleted"] == 1
    assert store.db[storage.COLL_SOLD].count_documents({}) == 0


# ── імена в базі ────────────────────────────────────────────────────────────

def test_documents_use_readable_camelcase_names():
    """На ці документи дивиться людина — у Compass, Studio 3T чи Atlas.

    У пам'яті поля лишились історичними (`cur`, `seen`, `miss`), бо на них
    спирається півпроєкту; у базі вони мають бути повними й camelCase.
    Тест сторожить саме межу між цими двома світами.
    """
    store = fresh_store()
    store.save(sample_state())

    ad = store.db[storage.COLL_ADS].find_one({"adId": "111"})
    assert set(ad["_id"]) == {"watch", "adId"}
    assert ad["currency"] == "UAH"
    assert ad["lastSeenAt"] and ad["firstSeenAt"]
    for short in ("cur", "seen", "first", "miss", "created", "w", "a"):
        assert short not in ad, f"куце ім'я {short!r} протекло в базу"

    watch = store.db[storage.COLL_WATCHES].find_one({"_id": "Букфлі"})
    assert watch["isSeeded"] is True
    assert watch["lastRunAt"]
    assert watch["recentAdIds"] == ["abc", "def"]
    for short in ("seeded", "last_run", "window", "heartbeat_last"):
        assert short not in watch

    sold = store.db[storage.COLL_SOLD].find_one({})
    assert set(sold["_id"]) == {"watch", "adId", "goneAt"}
    assert sold["goneAt"] and sold["daysListed"] == 19.7
    for short in ("id", "cur", "days", "gone"):
        assert short not in sold

    price = store.db[storage.COLL_PRICES].find_one({"adId": "111"})
    assert set(price["_id"]) == {"adId", "seenAt"}
    assert price["seenAt"] and price["currency"] == "UAH"

    meta = store.db[storage.COLL_STATE].find_one({"_id": "watcher"})
    assert meta["lastBookReportDate"] == "2026-09-22"
    assert meta["lastSoldReportAt"]
    for short in ("book_report_date", "sold_report_last"):
        assert short not in meta


def test_collections_are_named_for_humans():
    store = fresh_store()
    store.save(sample_state())
    assert set(store.db.list_collection_names()) == {
        "watcherState", "watches", "ads", "soldListings", "priceHistory"}


def test_an_unmapped_field_reaches_the_base_under_its_own_name():
    """Якщо в main.py колись з'явиться нове поле, воно має потрапити в базу,
    а не зникнути в перекладі."""
    store = fresh_store()
    state = sample_state()
    state["watches"]["Букфлі"]["ads"]["abc"]["sellerRating"] = 4.8
    store.save(state)

    assert store.db[storage.COLL_ADS].find_one({"adId": "abc"})["sellerRating"] == 4.8
    assert store.load()["watches"]["Букфлі"]["ads"]["abc"]["sellerRating"] == 4.8


# ── вибір сховища ───────────────────────────────────────────────────────────

def test_mongo_mode_without_uri_fails_loudly(monkeypatch, tmp_path):
    """Тихий відкат на файл у GitHub Actions = стан нікуди не зберігся, і
    наступний прогін вважає всі оголошення новими. Краще впасти."""
    monkeypatch.delenv("MONGODB_URI", raising=False)
    with pytest.raises(SystemExit):
        storage.open_store(state_path=tmp_path / "s.json", mode="mongo")


def test_auto_uses_the_file_when_there_is_no_uri(monkeypatch, tmp_path):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    store = storage.open_store(state_path=tmp_path / "s.json", mode="auto")
    assert isinstance(store, storage.JsonStore)


def test_json_mode_ignores_a_present_uri(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://nope/")
    store = storage.open_store(state_path=tmp_path / "s.json", mode="json")
    assert isinstance(store, storage.JsonStore)


# ── міграція ────────────────────────────────────────────────────────────────

def test_migration_merges_the_two_state_files():
    """Стан живе у двох файлах: state.json (OLX, у репо) і state.local.json
    (Букфлі, лише вдома). Міграція має звести їх в одну базу."""
    import migrate_state

    olx_state = {"version": 1, "watches": {"Кінг": {"seeded": True, "ads": {"1": {"price": 100}}}},
                 "sold": [{"id": "1", "watch": "Кінг", "gone": "2026-09-01T00:00:00+00:00"}],
                 "book_report_date": "2026-09-20"}
    local_state = {"version": 1, "watches": {"Букфлі": {"seeded": True, "ads": {"a": {"price": 55}}}},
                   "book_report_date": "2026-09-22"}

    merged = migrate_state.merge_states([olx_state, local_state])

    assert set(merged["watches"]) == {"Кінг", "Букфлі"}
    assert merged["book_report_date"] == "2026-09-22"      # свіжіша дата виграє
    assert len(merged["sold"]) == 1


def test_migration_keeps_the_fresher_record_of_the_same_ad():
    import migrate_state

    old = {"watches": {"Кінг": {"ads": {"1": {"price": 300, "seen": "2026-09-01T00:00:00+00:00"}}}}}
    new = {"watches": {"Кінг": {"ads": {"1": {"price": 200, "seen": "2026-09-20T00:00:00+00:00"}}}}}

    merged = migrate_state.merge_states([old, new])
    assert merged["watches"]["Кінг"]["ads"]["1"]["price"] == 200


def test_migration_does_not_duplicate_the_same_sale_twice():
    """Передати той самий state.json двічі — найімовірніша помилка оператора."""
    import migrate_state

    st = {"watches": {}, "sold": [{"id": "1", "watch": "Кінг", "gone": "2026-09-01T00:00:00+00:00"}]}
    merged = migrate_state.merge_states([st, dict(st)])
    assert len(merged["sold"]) == 1
