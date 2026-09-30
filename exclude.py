#!/usr/bin/env python3
"""Викреслює пачку оголошень зі статистики — за списком у файлі.

Навіщо. Кнопка в Telegram добра, коли оголошення щойно приїхало. Але накопичені
випадки («відкрив через тиждень — не працює», «це взагалі не та книжка»,
«видання якесь дивне і ламає медіану») зручніше зібрати в текстовий файл і
застосувати одним рухом. Файл лишається в репозиторії як історія: через місяць
видно, що саме викинуто й коли.

Формат файлу — по одному на рядок, у будь-якому вигляді:

    https://www.olx.ua/d/uk/obyavlenie/...-ID11lZx2.html   # не відкривається
    11lZx2
    936150588
    # рядки з решіткою і порожні пропускаються

⚠ У посиланні OLX показує НЕ той id, що в даних: `ID11lZx2` — це base62-запис
числа 936150588, а стан ключується саме числом. Через це старий `--mark-ru`
з посиланням мовчки не спрацьовував. Тут розкодовує `olx.ad_id_from`.

    py exclude.py excluded.txt                 # показати, що буде зроблено
    py exclude.py excluded.txt --apply         # застосувати
    py exclude.py excluded.txt --apply --why ru   # …як російські видання
    py exclude.py --list                       # що вже викреслено
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import olx
import storage

log = logging.getLogger("exclude")


def read_list(path: Path) -> list[tuple[str, str | None]]:
    """[(рядок як написано, розпізнаний id або None)] — порядок збережено."""
    out: list[tuple[str, str | None]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        out.append((line, olx.ad_id_from(line)))
    return out


def check_one(state: dict, raw: str) -> int:
    """Усе, що база знає про одне оголошення, одним екраном.

    Навіщо окрема команда, коли є Studio 3T. Стан розкиданий по чотирьох
    місцях, і щоб відповісти на просте «чи враховується це оголошення», треба
    зазирнути в кожне: `meta.manual_ru` (ручна позначка), `ads` (прапорець
    `an` у КОЖНОМУ watch'і, бо оголошення бачать кілька пошуків), `dropped`
    на watch'і і `sold`. Запит руками в чотири колекції — це чотири нагоди
    помилитись, і саме на такій помилці легко вирішити, що позначка не
    спрацювала, хоча вона спрацювала в іншому watch'і.
    """
    ad_id = olx.ad_id_from(raw)
    if ad_id is None:
        print(f"Не розпізнав оголошення: {raw}")
        return 2
    print(f"Оголошення {ad_id}" + (f"  ← {raw}" if raw != ad_id else ""))

    mark = (state.get("manual_ru") or {}).get(ad_id)
    if mark is None:
        print("  ручна позначка: немає")
    else:
        why = mark.get("why", "ru") if isinstance(mark, dict) else "ru"
        when = (mark.get("at") if isinstance(mark, dict) else str(mark)) or ""
        label = {"ru": "російське видання", "bad": "недійсне"}.get(why, why)
        print(f"  ручна позначка: {label}  ({when[:19]})")

    found = False
    for wname, ws in (state.get("watches") or {}).items():
        rec = (ws.get("ads") or {}).get(ad_id)
        dropped = ad_id in (ws.get("dropped") or {})
        if rec is None and not dropped:
            continue
        found = True
        print(f"\n  watch «{wname}»")
        if rec is None:
            print("    у списку оголошень: немає (лише у відкинутих)")
        else:
            an = rec.get("an")
            print(f"    ціна: {rec.get('price')} {rec.get('cur') or ''}")
            print(f"    назва: {str(rec.get('title') or '')[:70]}")
            print(f"    в аналітиці: {'НІ (an: false)' if an is False else 'так'}")
            print(f"    останній раз у видачі: {str(rec.get('seen') or '—')[:19]}")
            if rec.get("miss"):
                print(f"    зникло з видачі: {str(rec['miss'])[:19]}")
            if rec.get("created"):
                print(f"    створене на OLX: {str(rec['created'])[:19]}")
        print(f"    у відкинутих (dropped): {'так' if dropped else 'ні'}")

    hits = [s for s in (state.get("sold") or []) if str(s.get("id")) == ad_id]
    for s in hits:
        found = True
        print(f"\n  історія зняття: {str(s.get('gone') or '')[:19]} · "
              f"{s.get('price')} {s.get('cur') or ''} · "
              f"провисіло {s.get('days')} дн. · watch «{s.get('watch')}»"
              + ("  [поза статистикою]" if s.get("an") is False else ""))

    if not found:
        print("\n  У стані його немає взагалі: або не потрапляло в жоден пошук, "
              "або вже прибране (prune_days).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", type=Path,
                    help="текстовий файл зі списком посилань або id")
    ap.add_argument("--apply", action="store_true",
                    help="справді записати в базу (без цього — лише показати)")
    ap.add_argument("--why", choices=["bad", "ru"], default="bad",
                    help="причина: bad — недійсне (типово), ru — російське видання")
    ap.add_argument("--list", action="store_true",
                    help="показати, що вже викреслено, і вийти")
    ap.add_argument("--check", metavar="ID|URL",
                    help="показати все, що база знає про одне оголошення")
    ap.add_argument("--undo", metavar="ID|URL", action="append", default=[],
                    help="скасувати позначку (випадково натиснув кнопку в ТГ)")
    ap.add_argument("--storage", choices=["auto", "json", "mongo"], default="auto")
    ap.add_argument("--state", type=Path, default=Path("state.json"))
    ap.add_argument("--mongo-uri", default=None)
    ap.add_argument("--mongo-db", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # .env читає main — без нього MONGODB_URI не підхопиться при запуску руками.
    import main as watcher
    watcher.load_env()

    store = storage.open_store(state_path=args.state, mode=args.storage,
                               uri=args.mongo_uri, db_name=args.mongo_db)
    state = store.load()
    flagged = state.get("manual_ru") or {}

    if args.check:
        return check_one(state, args.check)

    if args.undo:
        ids, bad = [], []
        for raw in args.undo:
            ad_id = olx.ad_id_from(raw)
            (ids if ad_id else bad).append(ad_id or raw)
        for raw in bad:
            print(f"  ✖ не розпізнав: {raw}")
        n = watcher.restore_ads(state, ids)
        store.save(state)
        print(f"Знято позначок: {n}. Лишилось у базі: "
              f"{len(state.get('manual_ru') or {})}.")
        if n:
            print("Оголошення повернеться в статистику наступним прогоном, "
                  "щойно його знову побачать у видачі.")
        return 1 if bad else 0

    if args.list:
        print(f"Викреслено оголошень: {len(flagged)}")
        for ad_id, meta in sorted(flagged.items()):
            why = meta.get("why", "ru") if isinstance(meta, dict) else "ru"
            when = meta.get("at", "") if isinstance(meta, dict) else str(meta)
            print(f"  {ad_id:<14} {why:<4} {when[:19]}")
        return 0

    if not args.file:
        ap.error("вкажіть файл зі списком або --list")
    if not args.file.exists():
        print(f"Немає файлу {args.file}", file=sys.stderr)
        return 2

    rows = read_list(args.file)
    good = [(line, ad_id) for line, ad_id in rows if ad_id]
    bad = [line for line, ad_id in rows if not ad_id]

    for line in bad:
        print(f"  ✖ не розпізнав: {line}")
    # Один файл легко містить те саме оголошення двічі — посиланням і кодом.
    seen: set[str] = set()
    uniq = [(l, i) for l, i in good if not (i in seen or seen.add(i))]
    known = [(l, i) for l, i in uniq if i in flagged]
    fresh = [(l, i) for l, i in uniq if i not in flagged]

    for line, ad_id in fresh:
        print(f"  + {ad_id:<14} ← {line[:70]}")
    if known:
        print(f"  = вже було: {len(known)}")

    if not args.apply:
        print(f"\nРозпізнано {len(good)} з {len(rows)}, нових {len(fresh)}. "
              f"Це був підрахунок — щоб записати, запустіть з --apply.")
        return 1 if bad else 0

    added = watcher.exclude_ads(state, [i for _, i in good], why=args.why)
    store.save(state)
    print(f"\nЗаписано. Нових позначок: {added}. "
          f"Усього в базі: {len(state.get('manual_ru') or {})}.")
    if bad:
        print(f"⚠ Не розпізнано рядків: {len(bad)} — їх не записано.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
