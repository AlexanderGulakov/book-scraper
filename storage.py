#!/usr/bin/env python3
"""Шар зберігання стану: файл `state.json` або MongoDB.

Навіщо. Досі стан жив у JSON-файлі, який GitHub Actions комітив назад у репо,
а домашня машина тримала свій окремий `state.local.json`. Це давало три болі:
коміт стану в історію на кожен прогін, гонки `git pull --rebase`, і дві
половини стану, які ніколи не бачать одна одну — тому аналітика цін на
домашній машині не знала нічого про OLX, а в хмарі не знала про Букфлі.

Mongo прибирає всі три. Ключова умова — **писати документами, а не файлом**:
якщо класти весь стан одним документом, ми отримаємо той самий `state.json`,
тільки в хмарі, з тими самими гонками. Тому `MongoStore.save()` порівнює
поточний стан зі знімком, зробленим на `load()`, і пише лише те, що
змінилось. Два писачі (Actions з OLX, домашня машина з Букфлі) торкаються
різних документів і не конфліктують.

Формат стану в пам'яті НЕ змінився — `load()` повертає точно той самий dict,
що й раніше читався з файлу. Увесь інший код працює без правок.

Колекції
--------
| колекція            | _id                          | що всередині                          |
|---------------------|------------------------------|---------------------------------------|
| `meta`              | `"state"`                    | version, book_report_date, sold_report_last |
| `watches`           | ключ watch'а                 | seeded, last_run, heartbeat_last, window |
| `ads`               | `{w: watch, a: id}`          | ціна, назва, seen/first/miss, url     |
| `sold`              | `{w: watch, a: id, g: gone}` | зняті з продажу                       |
| `price_observations`| `{a: id, at: seen}`          | історія цін (нове — у файлі її не було)|

`price_observations` — єдине, чого у файловому стані не існувало взагалі.
Файл тримає лише останній зріз: побачили нову ціну — стару затерли. Тому
аналітика вміє сказати «за скільки зняли», але не «як ця книжка дешевшала».
Рядок на кожну помічену зміну ціни це виправляє, і коштує майже нічого:
кілька документів за прогін.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("storage")

STATE_VERSION = 1
DEFAULT_DB = "olx_watcher"

# Поля watch'а, які лежать у колекції `watches`. `ads` виноситься в окрему
# колекцію — саме заради цього все й затівалось.
_WATCH_FIELDS_SKIP = {"ads"}


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "watches": {}}


# ─────────────────────────────────────────────────────────────────────────────
#  Файл
# ─────────────────────────────────────────────────────────────────────────────

class JsonStore:
    """Стан у файлі — поведінка один-в-один як була до Mongo.

    Лишається не заради сумісності, а тому що це робочий режим: `--dry-run`
    на чужій машині, тести, і запасний варіант, якщо Atlas раптом недоступний.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def describe(self) -> str:
        return f"файл {self.path}"

    def load(self) -> dict[str, Any]:
        import json

        if not self.path.exists():
            return empty_state()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("%s пошкоджений — починаю з чистого стану", self.path)
            return empty_state()
        state.setdefault("version", STATE_VERSION)
        state.setdefault("watches", {})
        return state

    def save(self, state: dict[str, Any]) -> None:
        import json

        self.path.write_text(
            json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8",
        )

    def reset(self) -> dict[str, Any]:
        """`--reset`: забути все. Як і було — файл перезапишеться цілком."""
        return empty_state()

    def close(self) -> None:  # нічого закривати
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  Mongo
# ─────────────────────────────────────────────────────────────────────────────

def _watch_doc(key: str, ws: dict[str, Any]) -> dict[str, Any]:
    doc = {k: v for k, v in ws.items() if k not in _WATCH_FIELDS_SKIP}
    doc["_id"] = key
    return doc


def _ad_id(watch: str, ad: str) -> dict[str, str]:
    """Складений _id замість рядка з роздільником.

    Назви watch'ів українські й довільні («Клер: Місто скла»), тож будь-який
    роздільник рано чи пізно трапиться всередині назви. Mongo дозволяє
    документ як _id — беремо його і не вигадуємо екранування.
    """
    return {"w": watch, "a": ad}


def _sold_id(rec: dict[str, Any]) -> dict[str, Any]:
    # `gone` у ключі навмисно: те саме оголошення може бути зняте, виставлене
    # знову і зняте вдруге — це два різні спостереження, а не одне.
    return {"w": str(rec.get("watch") or ""), "a": str(rec.get("id") or ""),
            "g": str(rec.get("gone") or "")}


def _flatten_ads(state: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for key, ws in (state.get("watches") or {}).items():
        for ad, rec in (ws.get("ads") or {}).items():
            out[(str(key), str(ad))] = rec
    return out


class MongoStore:
    """Стан у MongoDB, з подокументним записом.

    ⚠ `save()` не атомарний: кілька `bulk_write` підряд, без транзакції.
    Транзакції на Atlas M0 технічно є, але покладатись на них тут ні до чого —
    прогін і так ідемпотентний. Натомість важливий ПОРЯДОК запису: спершу
    оголошення, потім службові поля watch'ів (`last_run`, `seeded`), потім
    історія, потім meta.

    Якщо збій станеться посередині, гіршим наслідком буде повторна перевірка
    тих самих оголошень наступного прогону. Зворотний порядок був би гіршим:
    записаний `last_run` без записаних оголошень = тихо пропущені книжки.
    """

    def __init__(self, uri: str, db_name: str = DEFAULT_DB, *,
                 client: Any = None, timeout_ms: int = 15000) -> None:
        if client is not None:
            self.client = client
        else:
            try:
                from pymongo import MongoClient
            except ImportError as exc:  # pragma: no cover - залежить від оточення
                raise SystemExit(
                    "Для роботи з Mongo потрібен пакет pymongo: pip install -r requirements.txt"
                ) from exc
            self.client = MongoClient(
                uri,
                appname="olx-watcher",
                serverSelectionTimeoutMS=timeout_ms,
                connectTimeoutMS=timeout_ms,
                retryWrites=True,
            )
        self.db = self.client[db_name]
        self.db_name = db_name
        self._snapshot: dict[str, Any] = empty_state()
        self._indexed = False

    # ── службове ────────────────────────────────────────────────────────────

    def describe(self) -> str:
        return f"MongoDB, база {self.db_name}"

    def ping(self) -> None:
        """Перевірка зв'язку з людською помилкою замість трейсбека pymongo."""
        try:
            self.client.admin.command("ping")
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(
                f"Не вдалось під'єднатись до MongoDB: {exc}\n"
                "Найчастіші причини:\n"
                "  • IP не в Atlas Network Access (раннер GitHub щоразу бере новий) —\n"
                "    додайте 0.0.0.0/0 або вайтлистіть IP кроком воркфлоу;\n"
                "  • хибний пароль у MONGODB_URI (спецсимволи треба percent-encode'ити);\n"
                "  • кластер на паузі — Atlas паузить M0 після ~60 днів без звернень."
            ) from exc

    def ensure_indexes(self) -> None:
        """Індекси створюються один раз і не сміють валити прогін.

        Це рівно той самий урок, що й з діагностикою Букфлі: допоміжна дія,
        яка кладе весь запуск, гірша за відсутню допоміжну дію.
        """
        if self._indexed:
            return
        try:
            self.db.ads.create_index([("w", 1)])
            self.db.ads.create_index([("seen", 1)])
            self.db.ads.create_index([("miss", 1)])
            self.db.sold.create_index([("gone", -1)])
            self.db.sold.create_index([("watch", 1)])
            self.db.price_observations.create_index([("a", 1), ("at", 1)])
            self.db.price_observations.create_index([("at", -1)])
            self.db.price_observations.create_index([("w", 1)])
        except Exception as exc:  # noqa: BLE001
            log.warning("Не вдалось створити індекси (працюємо далі): %s", exc)
        self._indexed = True

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001  pragma: no cover
            pass

    # ── читання ─────────────────────────────────────────────────────────────

    def load(self) -> dict[str, Any]:
        """Збирає з колекцій рівно той dict, що раніше лежав у файлі."""
        state = empty_state()

        meta = self.db.meta.find_one({"_id": "state"}) or {}
        for field in ("version", "book_report_date", "sold_report_last"):
            if meta.get(field) is not None:
                state[field] = meta[field]
        state.setdefault("version", STATE_VERSION)

        watches: dict[str, Any] = {}
        for doc in self.db.watches.find({}):
            key = str(doc.pop("_id"))
            doc.pop("ads", None)
            doc["ads"] = {}
            watches[key] = doc

        for doc in self.db.ads.find({}):
            ident = doc.get("_id") or {}
            key, ad = str(ident.get("w", "")), str(ident.get("a", ""))
            if not key or not ad:
                continue
            rec = {k: v for k, v in doc.items() if k not in ("_id", "w", "a")}
            watches.setdefault(key, {"seeded": False, "ads": {}}).setdefault("ads", {})[ad] = rec

        state["watches"] = watches

        sold = [
            {k: v for k, v in doc.items() if k != "_id"}
            for doc in self.db.sold.find({}).sort("gone", 1)
        ]
        if sold:
            state["sold"] = sold

        # Знімок для діфа. Глибока копія обов'язкова: далі код мутує state
        # на місці, і поверхнева копія показала б «змін немає».
        self._snapshot = copy.deepcopy(state)
        return state

    def reset(self) -> dict[str, Any]:
        """`--reset`: забути побачені оголошення, але НЕ історію.

        У файловому стані `--reset` затирав усе разом із `sold` — просто тому,
        що файл перезаписувався цілком. У базі так робити не можна: `sold` і
        `price_observations` це єдине, чого не можна перескрапити, і на них
        тримається вся аналітика цін. Тому чистимо лише `watches` і `ads` —
        рівно те, що означає «наступний запуск буде seed».

        Щоб стерти й історію, є `migrate_state.py --force` або рука в Atlas.
        """
        self.db.watches.delete_many({})
        self.db.ads.delete_many({})
        state = self.load()          # підтягне sold і meta, які лишились
        state["watches"] = {}
        self._snapshot = copy.deepcopy(state)
        log.warning("--reset: watches і ads очищено; sold і price_observations збережено")
        return state

    # ── запис ───────────────────────────────────────────────────────────────

    def save(self, state: dict[str, Any]) -> dict[str, int]:
        """Пише в Mongo лише різницю між `state` і знімком з `load()`.

        Повертає лічильники — вони йдуть у лог і в тести.
        """
        self.ensure_indexes()
        from pymongo import DeleteOne, ReplaceOne, UpdateOne

        snap = self._snapshot
        counts = {"ads": 0, "ads_deleted": 0, "watches": 0, "watches_deleted": 0,
                  "sold": 0, "sold_deleted": 0, "prices": 0, "meta": 0}

        # 1. Оголошення — найперше (див. докстрінг класу про порядок).
        cur_ads = _flatten_ads(state)
        old_ads = _flatten_ads(snap)
        ad_ops: list[Any] = []
        price_ops: list[Any] = []
        for (key, ad), rec in cur_ads.items():
            prev = old_ads.get((key, ad))
            if prev == rec:
                continue
            doc = dict(rec)
            doc["_id"] = _ad_id(key, ad)
            doc["w"], doc["a"] = key, ad
            ad_ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
            # Історія цін: перша поява або зміна ціни.
            price = rec.get("price")
            if price is not None and (prev is None or prev.get("price") != price):
                at = str(rec.get("seen") or "")
                price_ops.append(UpdateOne(
                    {"_id": {"a": ad, "at": at}},
                    {"$setOnInsert": {
                        "w": key, "a": ad, "at": at, "price": price,
                        "cur": rec.get("cur"), "title": rec.get("title"),
                        "url": rec.get("url"), "first": rec.get("first"),
                    }},
                    upsert=True,
                ))
        for (key, ad) in old_ads.keys() - cur_ads.keys():
            ad_ops.append(DeleteOne({"_id": _ad_id(key, ad)}))
            counts["ads_deleted"] += 1
        counts["ads"] = len(ad_ops) - counts["ads_deleted"]
        counts["prices"] = len(price_ops)
        self._bulk(self.db.ads, ad_ops)
        self._bulk(self.db.price_observations, price_ops)

        # 2. Службові поля watch'ів.
        cur_w = state.get("watches") or {}
        old_w = snap.get("watches") or {}
        w_ops: list[Any] = []
        for key, ws in cur_w.items():
            doc = _watch_doc(str(key), ws)
            if old_w.get(key) is not None and _watch_doc(str(key), old_w[key]) == doc:
                continue
            w_ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
            counts["watches"] += 1
        orphans = [k for k in old_w.keys() - cur_w.keys()]
        for key in orphans:
            w_ops.append(DeleteOne({"_id": key}))
            counts["watches_deleted"] += 1
        self._bulk(self.db.watches, w_ops)
        if orphans:
            # Watch зник із конфігу (перейменували) — його оголошення теж
            # більше нікому не належать.
            try:
                self.db.ads.delete_many({"w": {"$in": orphans}})
            except Exception as exc:  # noqa: BLE001
                log.warning("Не вдалось прибрати оголошення зниклих watch'ів: %s", exc)

        # 3. Історія знятих.
        cur_sold = {_key(_sold_id(r)): r for r in (state.get("sold") or [])}
        old_sold = {_key(_sold_id(r)): r for r in (snap.get("sold") or [])}
        s_ops: list[Any] = []
        for ident, rec in cur_sold.items():
            if old_sold.get(ident) == rec:
                continue
            doc = dict(rec)
            doc["_id"] = dict(zip(("w", "a", "g"), ident))
            s_ops.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
            counts["sold"] += 1
        for ident in old_sold.keys() - cur_sold.keys():
            s_ops.append(DeleteOne({"_id": dict(zip(("w", "a", "g"), ident))}))
            counts["sold_deleted"] += 1
        self._bulk(self.db.sold, s_ops)

        # 4. meta — останньою: це «прогін дорахував до кінця».
        meta = {"_id": "state", "version": state.get("version", STATE_VERSION)}
        for field in ("book_report_date", "sold_report_last"):
            if state.get(field) is not None:
                meta[field] = state[field]
        old_meta = {"_id": "state", "version": snap.get("version", STATE_VERSION)}
        for field in ("book_report_date", "sold_report_last"):
            if snap.get(field) is not None:
                old_meta[field] = snap[field]
        if meta != old_meta:
            self.db.meta.replace_one({"_id": "state"}, meta, upsert=True)
            counts["meta"] = 1

        # Новий знімок — щоб повторний save() у тому ж процесі не переписував
        # усе вдруге.
        self._snapshot = copy.deepcopy(state)
        log.info(
            "Mongo: оголошень %s (видалено %s), watch'ів %s (видалено %s), "
            "знятих %s, цін %s",
            counts["ads"], counts["ads_deleted"], counts["watches"],
            counts["watches_deleted"], counts["sold"], counts["prices"],
        )
        return counts

    @staticmethod
    def _bulk(collection: Any, ops: list[Any]) -> None:
        if not ops:
            return
        # ordered=False: одна невдала операція не має зупиняти решту.
        collection.bulk_write(ops, ordered=False)


def _key(ident: dict[str, Any]) -> tuple[str, str, str]:
    return (str(ident.get("w", "")), str(ident.get("a", "")), str(ident.get("g", "")))


# ─────────────────────────────────────────────────────────────────────────────
#  Вибір сховища
# ─────────────────────────────────────────────────────────────────────────────

def open_store(*, state_path: Path, mode: str = "auto", uri: str | None = None,
               db_name: str | None = None) -> Any:
    """Повертає сховище за режимом `auto` / `json` / `mongo`.

    `auto` — Mongo, якщо є `MONGODB_URI`, інакше файл. Зручно локально.

    `mongo` — жорстко Mongo: якщо URI немає або кластер недоступний, падаємо
    з поясненням. Саме цей режим стоїть у воркфлоу, і це навмисно. Тихий
    відкат на файл у GitHub Actions означав би, що стан нікуди не зберігся,
    а наступний прогін вважав би всі оголошення новими — тобто сорок
    повідомлень у Telegram замість помилки в логах.
    """
    mode = (mode or "auto").lower()
    uri = uri or os.environ.get("MONGODB_URI") or ""
    db_name = db_name or os.environ.get("MONGODB_DB") or DEFAULT_DB

    if mode == "json":
        return JsonStore(state_path)
    if mode == "mongo" and not uri:
        raise SystemExit(
            "--storage mongo, але MONGODB_URI порожній.\n"
            "У GitHub Actions це секрет MONGODB_URI, локально — рядок у .env."
        )
    if mode == "auto" and not uri:
        return JsonStore(state_path)

    store = MongoStore(uri, db_name)
    store.ping()
    return store
