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

import bookflea
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


def wanted(w: dict[str, Any], idx: int, only: list[str], skip: list[str]) -> bool:
    """Чи брати цей watch у цьому запуску (ключі --only / --skip).

    Потрібно, бо Букфлі доводиться питати з українського IP (сайт вибирає
    каталог за IP клієнта), тож він крутиться окремо на домашній машині, поки
    OLX лишається в GitHub Actions. Порівнюємо і з `id`, і з `name`, і з
    `source`, без урахування регістру — щоб `--skip bookflea` теж працювало.
    """
    tags = {str(w.get("id") or "").casefold(), str(w.get("name") or "").casefold(),
            str(w.get("source") or "olx").casefold(), watch_key(w, idx).casefold()}
    tags.discard("")
    if only and not (tags & {o.casefold() for o in only}):
        return False
    if skip and (tags & {s.casefold() for s in skip}):
        return False
    return True


def forget_orphans(cfg: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Прибирає зі стану watch'і, яких уже немає в конфізі (напр. перейменовані).

    Саме перейменування і є типовим випадком: ключ стану — це `name`, тож
    «Рональду» → «Роналду» лишає по собі мертвий запис на 40 оголошень.
    Вимкнені (`enabled: false`) watch'і в конфізі присутні, їхній стан цілий.
    """
    live = {watch_key(w, i) for i, w in enumerate(cfg["watches"])}
    gone = [k for k in state["watches"] if k not in live]
    for k in gone:
        del state["watches"][k]
    return gone


def due(watch_state: dict[str, Any], interval_minutes: Any) -> bool:
    """Чи час перевіряти цей watch. Без interval_minutes — щоразу."""
    if not interval_minutes:
        return True
    last = watch_state.get("last_run")
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(last)
    except ValueError:
        return True
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=timezone.utc)
    # Запас у хвилину: розклад GitHub «пливе», і рівно 60.0 хв майже не трапляється.
    return datetime.now(timezone.utc) - prev >= timedelta(minutes=float(interval_minutes) - 1)


def bookflea_notes(name: str, opt: dict[str, Any], ws: dict[str, Any]) -> list[str]:
    """Повідомлення про стан сесії та ринку Букфлі — не частіше разу на зміну.

    Букфлі розділений за країнами і вибирає її за IP клієнта, тому чужа валюта
    у видачі пояснює мовчазний нуль збігів краще за будь-який лог. Але писати
    про це щопівгодини — знущання, тож шлемо лише коли змінилась сигнатура.
    """
    expected = str(opt.get("currency", "UAH")).upper()
    seen_cur = {str(c).upper() for c in (bookflea.LAST_SCAN.get("currencies") or set())}
    foreign = seen_cur - {expected}
    login_state, login_note = bookflea.LOGIN_STATE

    # Токен у BOOKFLEA_COOKIE живе близько місяця. Нагадати за тиждень дешевше,
    # ніж дізнатись про його смерть із раптової тиші.
    expires = bookflea.COOKIE_EXPIRES_AT
    days_left = (expires - datetime.now(timezone.utc)).days if expires else None
    expiring = expires.date().isoformat() if days_left is not None and days_left <= 7 else ""

    if foreign:
        log.warning("  ⚠ валюти у видачі: %s (очікуємо %s)", ", ".join(sorted(seen_cur)), expected)

    signature = f"{login_state}|{','.join(sorted(foreign))}|{expiring}"
    if signature == ws.get("market_signature"):
        return []
    ws["market_signature"] = signature

    out: list[str] = []
    if login_state == "relogin":
        out.append(
            f"ℹ️ <b>{notify.esc(name)}</b>: BOOKFLEA_COOKIE протухла, зайшов паролем. "
            f"Все працює — але поки не оновите секрет, кожен прогін логінитиметься заново."
        )
    if expiring and login_state == "cookie":
        out.append(
            f"ℹ️ <b>{notify.esc(name)}</b>: токен у BOOKFLEA_COOKIE спливає "
            f"{notify.esc(expiring)} (лишилось {max(days_left or 0, 0)} дн.) — час оновити секрет."
        )
    if login_state == "fail":
        out.append(
            f"⚠️ <b>{notify.esc(name)}</b>: не вдалось авторизуватись на Букфлі "
            f"({notify.esc(login_note)})."
        )
    if foreign:
        out.append(
            f"⚠️ <b>{notify.esc(name)}</b>: у видачі валюта "
            f"{notify.esc(', '.join(sorted(foreign)))} замість {notify.esc(expected)} — "
            f"сайт показує каталог іншої країни. Потрібен запуск з українського IP."
        )
    return out


def run(cfg: dict[str, Any], state: dict[str, Any], *, dry_run: bool,
        only: list[str] | None = None, skip: list[str] | None = None) -> tuple[list[str], int]:
    defaults = cfg.get("defaults", {}) or {}
    session = olx.build_session()
    messages: list[str] = []
    errors = 0
    now = datetime.now(timezone.utc).isoformat()

    for idx, w in enumerate(cfg["watches"]):
        if w.get("enabled") is False:
            continue
        if not wanted(w, idx, only or [], skip or []):
            continue
        key = watch_key(w, idx)
        name = w.get("name") or key
        opt = {**defaults, **w}
        # exclude_keywords — єдиний список, який ДОДАЄТЬСЯ до спільного з defaults,
        # а не замінює його: спільний чорний список мерчу + власні слова пошуку.
        # Вимкнути спільний список для окремого watch: use_default_excludes: false
        shared = (defaults.get("exclude_keywords") or []) if opt.get("use_default_excludes", True) else []
        opt["exclude_keywords"] = list(dict.fromkeys(shared + (w.get("exclude_keywords") or [])))

        ws = state["watches"].setdefault(key, {"seeded": False, "ads": {}})

        if not due(ws, opt.get("interval_minutes")):
            log.info("▷ %s — пропускаю, ще не час (кожні %s хв)", name, opt.get("interval_minutes"))
            continue

        log.info("▶ %s", name)
        source = str(opt.get("source", "olx")).lower()
        window: list[str] = []
        try:
            if source == "bookflea":
                ads, window = bookflea.collect(
                    session,
                    list(w.get("keywords") or []),
                    mode=str(opt.get("mode", "latest")),
                    page_size=int(opt.get("page_size", 48)),
                )
            else:
                ads = olx.fetch_watch(session, w["url"], pages=int(opt.get("pages", 1)))
        except (olx.OlxError, bookflea.BookfleaError) as exc:
            log.error("  ✖ %s", exc)
            errors += 1
            continue

        if source == "bookflea":
            # Діагностика, і тільки. Вона не сміє валити прогін: якщо тут щось
            # піде не так, ми втратимо ще й результати OLX, зібрані вище.
            try:
                messages.extend(bookflea_notes(name, opt, ws))
            except Exception as exc:  # noqa: BLE001
                log.warning("  не вдалось зібрати діагностику Букфлі: %s", exc)

        # Сканування «найновіших N» бачить лише вікно. Якщо між запусками
        # з нього зникло геть усе, значить за цей час з'явилось понад N
        # оголошень — щось могло проскочити повз нас непоміченим.
        prev_window = ws.get("window") or []
        watching_window = source == "bookflea" and str(opt.get("mode", "latest")) == "latest"
        if watching_window and window and prev_window and not (set(window) & set(prev_window)):
            warn = (
                f"⚠️ <b>{notify.esc(name)}</b>: за час між перевірками змінилась уся стрічка "
                f"({len(window)} позицій). Можливо, щось пропущено — збільште page_size "
                f"або перевіряйте частіше."
            )
            log.warning("  ⚠ вікно провернулось повністю — можливий пропуск")
            messages.append(warn)
        if window:
            ws["window"] = window

        ws["last_run"] = now
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
    ap.add_argument("--only", action="append", default=[], metavar="WATCH",
                    help="перевіряти лише ці watch'і (ім'я, id або source; можна кілька разів)")
    ap.add_argument("--skip", action="append", default=[], metavar="WATCH",
                    help="пропустити ці watch'і — напр. --skip Букфлі у хмарі")
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

    gone = forget_orphans(cfg, state)
    if gone:
        log.info("Прибрав зі стану watch'і, яких уже немає в конфізі: %s", ", ".join(gone))

    selected = [w for i, w in enumerate(cfg["watches"])
                if w.get("enabled") is not False and wanted(w, i, args.only, args.skip)]
    if args.only or args.skip:
        log.info("Цього разу перевіряю %s з %s: %s", len(selected), len(cfg["watches"]),
                 ", ".join(str(w.get("name") or "?") for w in selected) or "нічого")

    messages, errors = run(cfg, state, dry_run=args.dry_run, only=args.only, skip=args.skip)

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
    if errors and errors >= max(len(selected), 1):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
