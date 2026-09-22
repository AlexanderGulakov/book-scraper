#!/usr/bin/env python3
"""Одноразовий перенос стану з JSON-файлів у MongoDB.

Навіщо окремий скрипт, а не «перший прогін сам наповнить базу». Порожня база
для watcher'а означає «перший запуск» — він мовчки засідить усе, що бачить, і
історія знятих (79 записів, з яких рахується вся аналітика цін) зникне
назавжди. Тобто без міграції ми втратимо єдине, чого не можна перескрапити.

Станів у нас ДВА, і вони не перетинаються:
  • `state.json`       — OLX, комітиться з GitHub Actions;
  • `state.local.json` — Букфлі, лежить лише на домашній машині.
Скрипт приймає обидва й зводить їх в одну базу — власне заради цього все й
робиться: після міграції аналітика вперше побачить обидва джерела разом.

Запуск:
    py migrate_state.py --dry-run                       # порахувати, нічого не писати
    py migrate_state.py state.json state.local.json     # перенести
    py migrate_state.py --force ...                     # переписати непорожню базу
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import storage

log = logging.getLogger("migrate")


def _newer(a: str | None, b: str | None) -> bool:
    """Чи `a` пізніше за `b`. Дати скрізь ISO-рядки, тож порівняння лексичне."""
    return str(a or "") > str(b or "")


def merge_states(states: list[dict[str, Any]]) -> dict[str, Any]:
    """Зводить кілька файлів стану в один dict.

    Конфлікти вирішуються за свіжістю: у кожного оголошення є `seen`, у
    кожного watch'а — `last_run`. Перетин між OLX і Букфлі теоретично
    неможливий (різні watch'і), але покладатись на це не варто: один і той
    самий `state.json` легко передати двічі.
    """
    merged: dict[str, Any] = storage.empty_state()
    sold_seen: set[tuple[str, str, str]] = set()
    sold: list[dict[str, Any]] = []

    for st in states:
        for field in ("book_report_date", "sold_report_last"):
            if _newer(st.get(field), merged.get(field)):
                merged[field] = st[field]

        for key, ws in (st.get("watches") or {}).items():
            tgt = merged["watches"].setdefault(key, {"seeded": False, "ads": {}})
            for field, value in ws.items():
                if field == "ads":
                    continue
                if field == "seeded":
                    tgt["seeded"] = bool(tgt.get("seeded")) or bool(value)
                elif field in ("last_run", "heartbeat_last"):
                    if _newer(value, tgt.get(field)):
                        tgt[field] = value
                else:
                    tgt[field] = value
            for ad, rec in (ws.get("ads") or {}).items():
                prev = tgt["ads"].get(ad)
                if prev is None or _newer(rec.get("seen"), prev.get("seen")):
                    tgt["ads"][ad] = rec

        for rec in st.get("sold") or []:
            ident = (str(rec.get("watch") or ""), str(rec.get("id") or ""),
                     str(rec.get("gone") or ""))
            if ident in sold_seen:
                continue
            sold_seen.add(ident)
            sold.append(rec)

    if sold:
        merged["sold"] = sorted(sold, key=lambda r: str(r.get("gone") or ""))
    return merged


def describe(state: dict[str, Any]) -> str:
    ads = sum(len(ws.get("ads") or {}) for ws in (state.get("watches") or {}).values())
    return (f"{len(state.get('watches') or {})} watch'ів, {ads} оголошень, "
            f"{len(state.get('sold') or [])} знятих")


def db_counts(store: storage.MongoStore) -> dict[str, int]:
    return {
        name: store.db[name].count_documents({})
        for name in (storage.COLL_WATCHES, storage.COLL_ADS, storage.COLL_SOLD,
                     storage.COLL_PRICES, storage.COLL_STATE)
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path,
                    default=[Path("state.json"), Path("state.local.json")],
                    help="файли стану (за замовчуванням state.json і state.local.json)")
    ap.add_argument("--dry-run", action="store_true", help="лише показати, що буде перенесено")
    ap.add_argument("--force", action="store_true", help="писати навіть у непорожню базу")
    ap.add_argument("--mongo-uri", default=None, help="інакше береться з MONGODB_URI")
    ap.add_argument("--mongo-db", default=None, help="інакше з MONGODB_DB або olx_watcher")
    args = ap.parse_args()

    # .env поруч — щоб не доводилось експортувати URI вручну.
    try:
        import main as app

        app.load_env()
    except Exception:  # noqa: BLE001
        pass

    present = [p for p in args.files if p.exists()]
    missing = [p for p in args.files if not p.exists()]
    for p in missing:
        log.warning("немає файлу %s — пропускаю", p)
    if not present:
        log.error("Жодного файлу стану не знайдено. Вкажіть шляхи явно.")
        return 1

    states = []
    for p in present:
        try:
            states.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            log.error("%s не читається як JSON: %s", p, exc)
            return 1
        log.info("прочитано %s — %s", p, describe(states[-1]))

    merged = merge_states(states)
    log.info("разом після злиття: %s", describe(merged))

    if args.dry_run:
        log.info("--dry-run: у базу нічого не записано")
        return 0

    store = storage.open_store(state_path=Path("state.json"), mode="mongo",
                               uri=args.mongo_uri, db_name=args.mongo_db)
    before = db_counts(store)
    if any(before.values()) and not args.force:
        log.error("База вже не порожня: %s", before)
        log.error("Це майже напевно означає, що міграція вже була. "
                  "Якщо справді треба переписати — додайте --force.")
        return 1

    # Знімок лишається порожнім, тож save() запише все як нове.
    store._snapshot = storage.empty_state()
    counts = store.save(merged)
    after = db_counts(store)
    log.info("записано: %s", counts)
    log.info("у базі тепер: %s", after)

    # Перевірка на місці: читаємо назад і звіряємо з тим, що мали записати.
    # Дешево, а ловить саме той клас помилок, через який міграції й бояться.
    back = store.load()
    if describe(back) != describe(merged):
        log.error("⚠ прочитане з бази не збігається з переданим!")
        log.error("  передали:  %s", describe(merged))
        log.error("  прочитали: %s", describe(back))
        return 1
    log.info("✔ перевірка пройдена: з бази читається те саме — %s", describe(back))
    log.info("Тепер можна запускати: py main.py --storage mongo --dry-run")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
