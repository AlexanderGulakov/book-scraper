#!/usr/bin/env python3
"""Прибирає з Mongo дані тем, знятих з нагляду 2026-09-22.

Зняті: Ден Браун, «Служниця» Мак-Фадден, «Лоліта» Набокова і весь «Відьмак»,
крім «Вежі Ластівки».

Навіщо окремий скрипт. `storage.py` сам прибирає оголошення watch'а, який зник
із конфігу, але `sold` і `price_observations` він НЕ чіпає — і правильно
робить: історію не перескрапиш, тож видаляти її на кожну зміну назви watch'а
було б небезпечно. Тут же видалення свідоме й одноразове.

Другий випадок, який автоматика не бачить: Букфлі. Його watch лишається, а от
книжки Брауна й Сапковського всередині нього — ні. Тому другий прохід по
заголовках.

    py purge_dropped.py                # порахувати, нічого не чіпати
    py purge_dropped.py --apply        # видалити
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# Watch'і, знятих цілком. Їхні оголошення storage прибере й сам, коли їх не
# стане в конфізі, але sold і price_observations лишаються за ним.
DROPPED_WATCHES = [
    "Ден Браун",
    "Відьмак",
    "Служниця (Мак-Фадден)",
    "Лоліта (Набоков)",
]

# Заголовки знятих книжок — для Букфлі та для решток у старих watch'ах.
# «Вежа Ластівки» лишається, тож перевірка на неї стоїть окремо і головніша.
DROPPED_TITLE_WORDS = [
    "відьмак", "ведьмак", "сапковськ", "сапковски", "witcher",
    "ден браун", "дэн браун", "dan brown", "код да вінчі", "код да винчи",
    "янголи і демони", "ангелы и демоны", "інферно", "инферно",
    "втрачений символ", "утраченный символ", "цифрова фортеця",
    "точка обману", "походження", "таємниця таємниць",
    "служниц", "housemaid",
    "лоліта", "лолита", "lolita",
]
KEEP_TITLE_WORDS = ["вежа ластівки", "вежа ласт", "tower of the swallow"]


def title_is_dropped(title: str) -> bool:
    t = (title or "").casefold()
    if any(w in t for w in KEEP_TITLE_WORDS):
        return False
    return any(w in t for w in DROPPED_TITLE_WORDS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="справді видалити (без цього — лише порахувати)")
    ap.add_argument("--uri", default=os.getenv("MONGODB_URI"))
    ap.add_argument("--db", default=os.getenv("MONGODB_DB", "olx_watcher"))
    args = ap.parse_args()

    if not args.uri:
        print("Немає MONGODB_URI (ні в середовищі, ні в --uri).", file=sys.stderr)
        return 2

    from pymongo import MongoClient

    db = MongoClient(args.uri, serverSelectionTimeoutMS=15000)[args.db]

    watch_filters = [
        ("ads", {"w": {"$in": DROPPED_WATCHES}}),
        ("price_observations", {"w": {"$in": DROPPED_WATCHES}}),
        ("sold", {"watch": {"$in": DROPPED_WATCHES}}),
    ]

    total = 0
    for coll, flt in watch_filters:
        n = db[coll].count_documents(flt)
        total += n
        print(f"{coll:20} за watch'ем: {n}")
        if args.apply and n:
            db[coll].delete_many(flt)

    # Другий прохід — по заголовках, для тих, хто пережив перший (Букфлі).
    for coll in ("ads", "price_observations", "sold"):
        ids = [d["_id"] for d in db[coll].find({}, {"title": 1})
               if title_is_dropped(str(d.get("title") or ""))]
        total += len(ids)
        print(f"{coll:20} за заголовком: {len(ids)}")
        if args.apply and ids:
            # Пачками: _id складений, і один величезний $in Atlas M0 не любить.
            for i in range(0, len(ids), 500):
                db[coll].delete_many({"_id": {"$in": ids[i:i + 500]}})

    if args.apply:
        print(f"\nВидалено документів: {total}")
    else:
        print(f"\nЗнайдено до видалення: {total}. Це був підрахунок — "
              f"щоб видалити, запустіть з --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
