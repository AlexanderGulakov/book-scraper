#!/usr/bin/env python3
"""OLX watcher — стежить за сторінками пошуку OLX.ua і пише в Telegram/e-mail
про нові оголошення та падіння цін.

Запуск:  python main.py [--config watches.yaml] [--state state.json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

import notify
import olx

log = logging.getLogger("main")
STATE_VERSION = 1


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("watches"):
        raise SystemExit(f"У {path} немає жодного watch. Додайте хоча б один.")
    return cfg


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "watches": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("state.json пошкоджений — починаю з чистого стану")
        return {"version": STATE_VERSION, "watches": {}}
    state.setdefault("version", STATE_VERSION)
    state.setdefault("watches", {})
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def prune(watch_state: dict[str, Any], days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    ads = watch_state.get("ads", {})
    stale = [k for k, v in ads.items() if v.get("seen", "") < cutoff]
    for k in stale:
        del ads[k]
    return len(stale)


def watch_key(w: dict[str, Any], idx: int) -> str:
    return str(w.get("id") or w.get("name") or f"watch-{idx}")


def run(cfg: dict[str, Any], state: dict[str, Any], *, dry_run: bool) -> tuple[list[str], int]:
    defaults = cfg.get("defaults", {}) or {}
    session = olx.build_session()
    messages: list[str] = []
    errors = 0
    now = datetime.now(timezone.utc).isoformat()

    for idx, w in enumerate(cfg["watches"]):
        if w.get("enabled") is False:
            continue
        key = watch_key(w, idx)
        name = w.get("name") or key
        opt = {**defaults, **w}

        log.info("▶ %s", name)
        try:
            ads = olx.fetch_watch(session, w["url"], pages=int(opt.get("pages", 1)))
        except olx.OlxError as exc:
            log.error("  ✖ %s", exc)
            errors += 1
            continue

        ws = state["watches"].setdefault(key, {"seeded": False, "ads": {}})
        known: dict[str, Any] = ws["ads"]
        first_run = not ws.get("seeded")

        kept = [
            a for a in ads
            if olx.matches(
                a,
                include=opt.get("include_keywords", []) or [],
                exclude=opt.get("exclude_keywords", []) or [],
                cities=opt.get("cities", []) or [],
                skip_promoted=bool(opt.get("skip_promoted", False)),
                allow_similar=bool(opt.get("allow_similar", False)),
            )
        ]
        log.info("  знайдено %s, після фільтрів %s", len(ads), len(kept))

        new_cnt = drop_cnt = 0
        for ad in kept:
            prev = known.get(ad.id)
            fits = olx.price_ok(
                ad,
                max_price=opt.get("max_price"),
                min_price=opt.get("min_price"),
                currency=opt.get("currency", "UAH"),
                allow_no_price=bool(opt.get("allow_no_price", False)),
            )

            if prev is None:
                if fits and not first_run:
                    messages.append(notify.format_event("new", name, ad))
                    new_cnt += 1
            else:
                old = prev.get("price")
                threshold = float(opt.get("min_drop_percent", 1)) / 100.0
                dropped = (
                    fits
                    and old is not None
                    and ad.price is not None
                    and ad.price < old * (1 - threshold)
                )
                if dropped:
                    messages.append(notify.format_event("drop", name, ad, old_price=old))
                    drop_cnt += 1

            known[ad.id] = {
                "price": ad.price,
                "cur": ad.currency,
                "title": ad.title[:120],
                "seen": now,
            }

        if first_run:
            ws["seeded"] = True
            log.info("  перший запуск: запам'ятав %s оголошень, сповіщення не слав", len(kept))
        else:
            log.info("  нових: %s, здешевлень: %s", new_cnt, drop_cnt)

        removed = prune(ws, int(opt.get("prune_days", 30)))
        if removed:
            log.info("  прибрано зі стану %s застарілих записів", removed)

        if idx + 1 < len(cfg["watches"]):
            time.sleep(float(defaults.get("pause_between_watches", 3)) + random.uniform(0, 2))

    if dry_run and messages:
        log.info("--dry-run: %s повідомлень НЕ надіслано:\n%s", len(messages), "\n---\n".join(messages))
        messages = []

    return messages, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="watches.yaml", type=Path)
    ap.add_argument("--state", default="state.json", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="нічого не надсилати, лише показати")
    ap.add_argument("--reset", action="store_true", help="забути стан (наступний запуск буде seed)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    state = {"version": STATE_VERSION, "watches": {}} if args.reset else load_state(args.state)

    messages, errors = run(cfg, state, dry_run=args.dry_run)

    channels = notify.build_channels(cfg)
    if messages and not channels:
        log.error("Є %s подій, але жодного налаштованого каналу сповіщень!", len(messages))
        errors += 1
    else:
        notify.dispatch(channels, messages)
        if messages:
            log.info("Надіслано %s сповіщень", len(messages))

    save_state(args.state, state)
    log.info("Стан збережено в %s", args.state)

    # Не валимо workflow через тимчасову помилку мережі, якщо хоч щось спрацювало.
    if errors and errors >= len(cfg["watches"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
