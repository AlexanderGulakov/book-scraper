#!/usr/bin/env python3
"""Показує ДІЮЧІ цінові пороги по кожній книжці — так, як їх бачить `rules.py`.

Навіщо окремий скрипт, коли є `watches.yaml`. Поріг для однієї книжки
складається з трьох шарів, і жоден з них не видно цілком, читаючи конфіг очима:

    позиція `books:`  →  watch  →  defaults

Плюс два неочевидні правила, на яких уже двічі спіткнулись:

* якщо порогу Telegram немає, він **дорівнює** `max_price` — тобто watch з
  самим лише `max_price: 2000` шле все до 2000, а не мовчить;
* якщо немає взагалі нічого, це «без обмежень», а не «нічого не слати».

Тому таблицю краще щоразу рахувати, ніж тримати списком, який розійдеться з
конфігом наступного ж дня.

    py thresholds.py              # усі книжки
    py thresholds.py --watches    # ще й watch'і без каталогу
    py thresholds.py кідрук       # лише те, де трапляється слово
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


def effective(book: dict[str, Any], watch: dict[str, Any],
              defaults: dict[str, Any]) -> dict[str, Any]:
    """Те саме зіставлення трьох шарів, що робить `rules._opt`."""
    def pick(book_key: str, watch_key: str) -> Any:
        if book.get(book_key) is not None:
            return book[book_key]
        if watch.get(watch_key) is not None:
            return watch[watch_key]
        return defaults.get(watch_key)

    notify = pick("notify_max", "notify_max_price")
    high = pick("notify_min_high", "notify_min_price_high")
    store = pick("store_max", "max_price")
    if notify is None and high is None:
        notify = store                      # сумісність: «зберегли = написали»
    return {
        "notify": notify, "high": high, "store": store,
        "bundle_notify": pick("bundle_notify", "bundle_notify"),
        "bundle_max": pick("notify_max_bundle", "notify_max_price_bundle"),
        "always": book.get("notify_always") or watch.get("notify_always"),
        "skip": book.get("skip"),
    }


def _short(names: list[str]) -> str:
    """Один пошук — назвою, кілька — «N пошуків»; інакше рядок не влазить."""
    return names[0] if len(names) == 1 else f"{len(names)} пошуків"


def describe(e: dict[str, Any]) -> str:
    if e["skip"]:
        return "— знято з нагляду —"
    if e["always"]:
        return "ВСЕ, за будь-яку ціну"
    if e["notify"] is None and e["high"] is None and e["store"] is None:
        return "ВСЕ (жодної межі не задано)"
    out = f"≤ {e['notify']:g}" if e["notify"] is not None else "—"
    if e["high"] is not None:
        out += f" або ≥ {e['high']:g}"
    if e["store"] is not None and e["notify"] is not None and e["store"] != e["notify"]:
        out += f"   (в базу ≤ {e['store']:g})"
    if e["bundle_notify"]:
        out += "   · комплект: завжди"
    elif e["bundle_max"] is not None:
        out += f"   · комплект ≤ {e['bundle_max']:g}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("filter", nargs="?", default="",
                    help="показати лише рядки, де трапляється це слово")
    ap.add_argument("--watches", action="store_true",
                    help="додати watch'і, яким каталог нічого не зіставив")
    ap.add_argument("--config", default="watches.yaml")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    defaults = cfg.get("defaults") or {}
    by_name = {w.get("name"): w for w in cfg["watches"]}
    needle = args.filter.casefold()

    rows, used = [], set()
    for book in cfg.get("books") or []:
        # `watch:` у позиції каталогу може й не стояти — тоді книжка діє в
        # будь-якому пошуку, і поріг залежить від того, ЯКИЙ пошук її знайшов.
        # Показувати в такому разі «меж не задано» — брехня: саме так таблиця
        # першого разу приписала Клер і «Безсонню» відсутність порогів, яких
        # насправді повно. Тому: якщо прив'язки немає, беремо всі пошуки, чия
        # назва починається так само («Клер: …» → «Клер: Кассандра Клер»), а
        # коли й це не спрацювало — усі взагалі.
        names = book.get("watch") or []
        cands = [by_name[n] for n in names if n in by_name]
        if not cands:
            prefix = (book.get("name") or "").split(":")[0].strip()
            cands = [w for w in cfg["watches"]
                     if prefix and (w.get("name") or "").startswith(prefix)]
        if not cands:
            cands = list(cfg["watches"])

        # Варіанти групуємо за текстом і підписуємо пошуком: у Букфлі свої
        # межі, і «≤ 1600 / ≤ 2000» без підпису нічого не пояснює.
        variants: dict[str, list[str]] = {}
        for w in cands:
            variants.setdefault(describe(effective(book, w, defaults)), []).append(
                str(w.get("name") or "?"))
        used.update(w.get("name") for w in cands)
        if len(variants) == 1:
            text = next(iter(variants))
        else:
            text = " │ ".join(f"{k} ← {_short(v)}" for k, v in variants.items())
        rows.append((book.get("name") or "?", text, cands[0].get("name") or "—"))

    if args.watches:
        for w in cfg["watches"]:
            if w.get("name") in used:
                continue
            rows.append((f"[watch] {w.get('name')}",
                         describe(effective({}, w, defaults)), w.get("name")))

    rows = [r for r in rows if not needle or needle in " ".join(r).casefold()]
    if not rows:
        print("Нічого не знайшлось.")
        return 1

    wide = max(len(r[0]) for r in rows)
    print(f"{'КНИЖКА':<{wide}}  ПОРІГ TELEGRAM")
    print("─" * (wide + 2 + max(len(r[1]) for r in rows)))
    for name, text, _ in rows:
        print(f"{name:<{wide}}  {text}")
    print(f"\n{len(rows)} позицій. Пороги правляться у `books:` (на книжку) "
          f"або в `watches:` (на весь пошук).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
