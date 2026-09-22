#!/usr/bin/env python3
"""OLX watcher — стежить за сторінками пошуку OLX.ua і пише в Telegram/e-mail
про нові оголошення та падіння цін.

Запуск:  python main.py [--config watches.yaml] [--state state.json] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

import analytics
import bookflea
import notify
import olx
import rules
import storage

log = logging.getLogger("main")
STATE_VERSION = storage.STATE_VERSION


# Що main.py очікує від сусідніх модулів. Перевіряється на старті, бо файли
# копіюються на домашню машину руками й легко оновити не всі.
REQUIRED_API = {
    "notify": ["build_channels", "dispatch", "format_event", "format_test",
               "format_heartbeat", "format_watch_error"],
    "olx": ["build_session", "fetch_watch", "matches", "price_ok", "ad_state",
            "fetch_description"],
    "bookflea": ["collect"],
    "rules": ["decide", "Decision", "is_russian_text"],
    "analytics": ["profiles", "verdict_for_ad", "build_report", "report_due",
                  "mark_reported", "book_entry"],
    "storage": ["open_store", "empty_state", "JsonStore", "MongoStore"],
}


def check_modules() -> list[str]:
    """Ловить різнобій версій файлів ДО того, як він стане трейсбеком.

    Реальний випадок 2026-09-19: на домашню машину скопіювали новий main.py,
    але старий notify.py — і `--test-notify` упав з
    `AttributeError: module 'notify' has no attribute 'format_test'`.
    Раніше так само помер цілий прогін через розсинхрон main.py/bookflea.py.
    Діагноз має бути людським реченням, а не стеком.
    """
    return [
        f"{mod}.{attr}"
        for mod, attrs in REQUIRED_API.items()
        for attr in attrs
        if not hasattr(globals()[mod], attr)
    ]


def load_env(path: Path | None = None) -> list[str]:
    """Підтягує секрети з `.env` поруч зі скриптом. Повертає імена ключів.

    Навіщо окремий файл. Секрети не можна класти ні у `watches.yaml`, ні в
    `run-bookflea.bat` — репозиторій публічний. Змінні середовища Windows
    працюють, але Планувальник підхоплює нові лише після перелогіну, а
    `py main.py --test-notify` хочеться запустити одразу. `.env` читається
    обома способами запуску й уже стоїть у `.gitignore`.

    Уже задані змінні НЕ перезаписуються: у GitHub Actions секрети приходять
    із середовища й мають лишатись головнішими за випадковий файл у клоні.
    """
    path = path or Path(__file__).with_name(".env")
    if not path.exists():
        return []
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("set ").strip()
        # Лапки й пробіли зрізаємо тут-таки: саме через них Telegram місяцями
        # відповідав 400 chat not found на локальній машині.
        value = value.strip().strip('"').strip("'").strip()
        if not key:
            continue
        os.environ.setdefault(key, value)
        loaded.append(key)
    return loaded


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("watches"):
        raise SystemExit(f"У {path} немає жодного watch. Додайте хоча б один.")
    return cfg


# Файлові load/save лишились як тонкі обгортки над storage.JsonStore: на них
# спираються тести й звичка запускати `--storage json` локально.
def load_state(path: Path) -> dict[str, Any]:
    return storage.JsonStore(path).load()


def save_state(path: Path, state: dict[str, Any]) -> None:
    storage.JsonStore(path).save(state)


def prune(watch_state: dict[str, Any], days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    ads = watch_state.get("ads", {})
    stale = [k for k, v in ads.items() if v.get("seen", "") < cutoff]
    for k in stale:
        del ads[k]
    # Список відкинутих росте так само, як і самі оголошення, тож і забувається
    # за тим самим правилом. Знята з продажу «Гарри Поттер» не має вічно
    # займати місце у стані заради того, щоб ми її вдруге не перевірили.
    refused = watch_state.get("dropped") or {}
    for k in [k for k, v in refused.items() if str(v or "") < cutoff]:
        del refused[k]
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


def _age_days(rec: dict[str, Any], gone_at: datetime) -> float | None:
    """Скільки днів оголошення провисіло. None, якщо дату створення не знаємо."""
    raw = rec.get("created") or rec.get("first")
    if not raw:
        return None
    try:
        born = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if born.tzinfo is None:
        born = born.replace(tzinfo=timezone.utc)
    return max((gone_at - born).total_seconds() / 86400, 0)


def _days_text(days: float | None) -> str:
    if days is None:
        return "невідомо скільки"
    if days < 1:
        return f"{round(days * 24)} год"
    return f"{days:.0f} дн."


def sold_report(cfg: dict[str, Any], state: dict[str, Any], session: Any,
                *, max_checks: int = 60, pause: float = 1.5,
                max_age_days: float | None = 14, keep_days: float = 60) -> list[str]:
    """Перевіряє зниклі оголошення і звітує про ті, яких уже немає на сайті.

    Навіщо. Зняте оголошення — найкращий доступний сигнал «за цю ціну беруть».
    OLX не каже «продано», тому працюємо від протилежного: оголошення, яке
    зникло з видачі, перевіряємо за його власним посиланням (див.
    `olx.ad_state`). HTTP 410 означає, що його зняли.

    ⚠ Зняли ≠ продали: продавець міг і передумати, а через ~30 днів OLX сам
    архівує неоновлені оголошення. Тому в звіті довгожителі позначені окремо.

    Повертає повідомлення для Telegram; історію додає в state["sold"], щоб
    згодом було на чому рахувати статистику.
    """
    now_dt = datetime.now(timezone.utc)
    names = {watch_key(w, i): (w.get("name") or watch_key(w, i))
             for i, w in enumerate(cfg["watches"])}

    # Кандидати: зниклі, найдавніше зниклі — першими.
    #
    # ⚠ Довгожителів у чергу не беремо. Оголошення, яке провисіло два тижні,
    # уже відповіло на своє питання: за ці гроші його не беруть, і чи зняли
    # його на п'ятнадцятий день — нецікаво. Але сортування «найдавніше зниклі
    # першими» ставить саме їх на початок черги, і 60 перевірок за прогін
    # витрачались би на них, поки свіжі оголошення чекають. Для статистики
    # вони не пропадають: analytics бере їх з іншого боку — як тих, що лежать.
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    skipped_old = 0
    for key, ws in state["watches"].items():
        for ad_id, rec in (ws.get("ads") or {}).items():
            if not rec.get("miss") or "olx.ua" not in str(rec.get("url") or ""):
                continue
            if max_age_days is not None:
                age = _age_days(rec, now_dt)
                if age is not None and age > float(max_age_days):
                    skipped_old += 1
                    continue
            candidates.append((key, ad_id, rec))
    candidates.sort(key=lambda c: str(c[2].get("miss")))
    if skipped_old:
        log.info("Звіт про зняті: пропустив %s оголошень старших за %s дн.",
                 skipped_old, max_age_days)

    if not candidates:
        log.info("Звіт про зняті: перевіряти нема чого")
        return []

    log.info("Звіт про зняті: кандидатів %s, перевіряю до %s", len(candidates), max_checks)

    sold: list[dict[str, Any]] = []
    checked = 0
    for key, ad_id, rec in candidates[:max_checks]:
        verdict = olx.ad_state(session, rec["url"])
        checked += 1
        if verdict == "alive":
            rec.pop("miss", None)          # просто злетіло з першої сторінки
        elif verdict == "gone":
            days = _age_days(rec, now_dt)
            sold.append({
                "id": ad_id, "watch": names.get(key, key), "title": rec.get("title"),
                "price": rec.get("price"), "cur": rec.get("cur"),
                "url": rec.get("url"), "days": None if days is None else round(days, 1),
                "gone": now_dt.isoformat(),
                # Прапорець їде далі разом з оголошенням: комплект, проданий
                # за 900 грн, так само не описує ціну жодної окремої книжки.
                **({"an": False} if rec.get("an") is False else {}),
            })
            state["watches"][key]["ads"].pop(ad_id, None)
        time.sleep(pause + random.uniform(0, 1))

    log.info("Звіт про зняті: перевірено %s, знято %s", checked, len(sold))
    if not sold:
        return []

    # Історія — це і є паливо для аналітики, тож тримаємо її за датою, а не
    # за кількістю: обрізання «останні 1000» в активний тиждень з'їдало б
    # саме те вікно, по якому analytics рахує медіани.
    history = state.setdefault("sold", [])
    history.extend(sold)
    cutoff = (now_dt - timedelta(days=float(keep_days))).isoformat()
    state["sold"] = [s for s in history if str(s.get("gone") or "") >= cutoff][-5000:]

    by_watch: dict[str, list[dict[str, Any]]] = {}
    for s in sold:
        by_watch.setdefault(s["watch"], []).append(s)

    lines = [f"🧾 <b>Зняті з продажу за годину</b> — {len(sold)} шт."]
    for watch, items in by_watch.items():
        lines.append(f"\n<b>{notify.esc(watch)}</b>")
        for s in sorted(items, key=lambda x: x["price"] if x["price"] is not None else 1e9):
            price = f"{s['price']:.0f} {s['cur'] or ''}".strip() if s["price"] is not None else "без ціни"
            title = notify.esc(str(s["title"] or "")[:70])
            link = f"<a href=\"{notify.esc(s['url'])}\">{title}</a>" if s.get("url") else title
            mark = " ⏳" if (s["days"] or 0) >= 30 else ""
            lines.append(f"• {notify.esc(price)} · {_days_text(s['days'])}{mark} — {link}")

    priced = sorted(s["price"] for s in sold if s["price"] is not None)
    if priced:
        mid = priced[len(priced) // 2]
        lines.append(f"\nМедіана: <b>{mid:.0f} грн</b> (від {priced[0]:.0f} до {priced[-1]:.0f})")
    aged = sorted(s["days"] for s in sold if s["days"] is not None)
    if aged:
        lines.append(f"Провисіли: медіана {_days_text(aged[len(aged) // 2])}, "
                     f"від {_days_text(aged[0])} до {_days_text(aged[-1])}")
    if any((s["days"] or 0) >= 30 for s in sold):
        lines.append("⏳ — висіло понад 30 днів: можливо, не продаж, а закінчився термін.")

    return ["\n".join(lines)]


def _verdict(cfg: dict[str, Any], prof_map: dict[str, Any], watch_name: str,
             ad: Any) -> str | None:
    """Коментар до ціни для одного оголошення. Ніколи не валить прогін.

    Аналітика — приємний додаток, а не умова роботи: якщо вона з якоїсь
    причини впаде, сповіщення все одно має долетіти, просто без коментаря.
    """
    try:
        return analytics.verdict_for_ad(ad, watch_name, cfg, prof_map)
    except Exception as exc:  # noqa: BLE001
        log.debug("Вердикт для %s не склався: %s", getattr(ad, "id", "?"), exc)
        return None


def daily_book_report(cfg: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """Раз на добу — один звіт «за скільки що беруть». Без мережі, лише стан."""
    opt = {**analytics.DEFAULTS, **(cfg.get("defaults") or {})}
    if not opt.get("analytics_enabled", True):
        return []
    if not analytics.report_due(state, opt):
        return []
    text = analytics.build_report(cfg, state, esc=notify.esc)
    # Позначаємо день як відзвітований у будь-якому разі: інакше порожній
    # звіт перевірявся б наново щопрогону до півночі.
    analytics.mark_reported(state, opt)
    if not text:
        log.info("Денний звіт про ціни: висновків поки нема, мовчу")
        return []
    log.info("Денний звіт про ціни складено (%s символів)", len(text))
    return [text]


def heartbeat_due(opt: dict[str, Any], ws: dict[str, Any]) -> bool:
    """Чи час слати «живий» для цього watch'а.

    `heartbeat_hours` у watch: 0 — щоразу, N — не частіше ніж раз на N годин.
    Ключа немає (або false) — пульсу немає, поведінка як була.
    """
    hours = opt.get("heartbeat_hours")
    if hours is None or hours is False:
        return False
    return due({"last_run": ws.get("heartbeat_last")}, float(hours) * 60)


def run(cfg: dict[str, Any], state: dict[str, Any], *, dry_run: bool,
        only: list[str] | None = None, skip: list[str] | None = None) -> tuple[list[str], int]:
    defaults = cfg.get("defaults", {}) or {}
    session = olx.build_session()
    messages: list[str] = []
    errors = 0
    now = datetime.now(timezone.utc).isoformat()
    # Одне оголошення — одне повідомлення за прогін, навіть якщо його бачать
    # два пошуки (напр. «кассандра клер» і «знаряддя смерті» перетинаються).
    # У стані кожного watch воно все одно записується окремо.
    announced: set[str] = set()

    # Профілі цін рахуємо один раз на прогін — це чиста арифметика над станом,
    # без жодного запиту в мережу. Далі кожне сповіщення про оголошення
    # отримує з них коментар («Брати не думаючи» / «Задорого» / …).
    try:
        prof_map = analytics.profiles(cfg, state)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не вдалось порахувати профілі цін: %s", exc)
        prof_map = {}

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
        # Те саме для стоп-слів шару правил: вони дивляться ще й в опис, тож
        # спільний список («росмен») має додаватись, а не витіснятись власним.
        opt["drop_keywords"] = list(dict.fromkeys(
            (defaults.get("drop_keywords") or []) + (w.get("drop_keywords") or [])))

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
            # Watch із пульсом мовчати не має права навіть коли впав: інакше
            # зламаний скрапер виглядає точнісінько як «нічого не знайшлось».
            if heartbeat_due(opt, ws):
                messages.append(notify.format_watch_error(name, exc))
                ws["heartbeat_last"] = now
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

        # Оголошення, які шар правил уже визнав чужими. Тримаємо самі id:
        # без цього кожен прогін заново тягнув би опис російського видання,
        # щоб удруге дійти того самого висновку.
        refused: dict[str, Any] = ws.setdefault("dropped", {})
        catalog = cfg.get("books") or []
        desc_budget = int(opt.get("description_max_fetch", 30) or 0)
        want_desc = bool(opt.get("needs_description"))

        new_cnt = drop_cnt = skip_cnt = quiet_cnt = 0
        for ad in kept:
            if ad.id in refused:
                continue
            prev = known.get(ad.id)

            # Валюта — окремо від решти правил: 220 zł не сміє пройти як 220 грн.
            cur_want = opt.get("currency", "UAH")
            if ad.price is not None and cur_want and (
                    not ad.currency or ad.currency.upper() != str(cur_want).upper()):
                refused[ad.id] = now
                skip_cnt += 1
                continue

            entry = analytics.book_entry(ad.title, name, catalog)

            # Опис дістаємо один раз за життя оголошення і кладемо в стан:
            # він не змінюється, а коштує окремий запит.
            desc = (prev or {}).get("desc")
            if want_desc and desc is None and prev is None and desc_budget > 0:
                desc = olx.fetch_description(session, ad.url)
                desc_budget -= 1
                time.sleep(1 + random.uniform(0, 1))

            d = rules.decide(title=ad.title, price=ad.price, watch=opt,
                             entry=entry, description=desc,
                             include=opt.get("include_keywords", []) or [])

            if d.action == "skip":
                # Якщо оголошення колись було в базі, а тепер правила його
                # відкинули (змінився конфіг) — прибираємо, щоб не тягнути
                # сміття в статистику.
                known.pop(ad.id, None)
                refused[ad.id] = now
                skip_cnt += 1
                log.debug("  ✕ %s — %s", ad.title[:60], d.reason)
                continue

            if prev is None:
                if d.notify and not first_run and ad.id not in announced:
                    messages.append(notify.format_event(
                        "new", name, ad, verdict=_verdict(cfg, prof_map, name, ad),
                        mark=d.tag))
                    announced.add(ad.id)
                    new_cnt += 1
                elif not d.notify:
                    quiet_cnt += 1
            else:
                old = prev.get("price")
                threshold = float(opt.get("min_drop_percent", 1)) / 100.0
                cheaper = (
                    d.notify
                    and old is not None
                    and ad.price is not None
                    and ad.price < old * (1 - threshold)
                )
                if cheaper and ad.id not in announced:
                    messages.append(notify.format_event(
                        "drop", name, ad, old_price=old,
                        verdict=_verdict(cfg, prof_map, name, ad), mark=d.tag))
                    announced.add(ad.id)
                    drop_cnt += 1

            prev = prev or {}
            rec = {
                "price": ad.price,
                "cur": ad.currency,
                "title": ad.title[:120],
                "seen": now,
                # Для звіту про зняті оголошення: за посиланням ми потім
                # питаємо, чи воно ще живе, а дати дають «скільки пролежало».
                "url": ad.url or prev.get("url"),
                "created": ad.created_time or prev.get("created"),
                "first": prev.get("first") or now,
            }
            if desc:
                rec["desc"] = desc
            if not d.analytics:
                # Комплекти й оголошення без ціни: показати варто, рахувати
                # по них медіану — ні.
                rec["an"] = False
            known[ad.id] = rec

        # Позначаємо, чого цього разу в видачі не було. Зникнення з першої
        # сторінки ще нічого не означає — свіжі оголошення виштовхують старі, —
        # тому це лише кандидати на перевірку, яку робить sold_report().
        present = {a.id for a in ads} | set(window)
        for ad_id, rec in known.items():
            if ad_id in present:
                rec.pop("miss", None)
            elif "miss" not in rec:
                rec["miss"] = now

        if first_run:
            ws["seeded"] = True
            log.info("  перший запуск: запам'ятав %s оголошень, сповіщення не слав", len(kept))
        else:
            log.info("  нових: %s, здешевлень: %s, мовчки в базу: %s, відкинуто: %s",
                     new_cnt, drop_cnt, quiet_cnt, skip_cnt)
            # Знахідка сама по собі доводить, що скрапер живий, тож пульс
            # потрібен лише в порожній прогін.
            if not (new_cnt or drop_cnt) and heartbeat_due(opt, ws):
                messages.append(notify.format_heartbeat(
                    name,
                    keywords=len(w.get("keywords") or []),
                    seen=len(ads),
                    kept=len(kept),
                    known=len(known),
                ))
                ws["heartbeat_last"] = now
                log.info("  пульс надіслано")

        removed = prune(ws, int(opt.get("prune_days", 30)))
        if removed:
            log.info("  прибрано зі стану %s застарілих записів", removed)

        if idx + 1 < len(cfg["watches"]):
            time.sleep(float(defaults.get("pause_between_watches", 3)) + random.uniform(0, 2))

    # Раз на годину — звіт про зняті оголошення. У dry-run не чіпаємо: він
    # видаляє записи зі стану, а «показати й нічого не змінити» тут не вийде.
    every = defaults.get("sold_report_minutes", 60)
    if every and not dry_run and due({"last_run": state.get("sold_report_last")}, every):
        try:
            messages.extend(sold_report(
                cfg, state, session,
                max_checks=int(defaults.get("sold_report_max_checks", 60)),
                max_age_days=defaults.get("sold_check_max_age_days", 14),
            ))
            state["sold_report_last"] = now
        except Exception as exc:  # noqa: BLE001
            log.warning("Звіт про зняті не склався: %s", exc)

    # Раз на добу — аналітика цін. Вона рахується зі стану, тому нічого не
    # питає в OLX і не залежить від того, чи вдався цей конкретний прогін.
    if not dry_run:
        try:
            messages.extend(daily_book_report(cfg, state))
        except Exception as exc:  # noqa: BLE001
            log.warning("Денний звіт про ціни не склався: %s", exc)

    if dry_run and messages:
        log.info("--dry-run: %s повідомлень НЕ надіслано:\n%s", len(messages), "\n---\n".join(messages))
        messages = []

    return messages, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="watches.yaml", type=Path)
    ap.add_argument("--state", default="state.json", type=Path,
                    help="файл стану; використовується лише при --storage json")
    ap.add_argument("--storage", default="auto", choices=("auto", "json", "mongo"),
                    help="де тримати стан: auto (Mongo, якщо є MONGODB_URI), json, mongo")
    ap.add_argument("--mongo-uri", default=None, help="інакше береться з MONGODB_URI")
    ap.add_argument("--mongo-db", default=None, help="інакше з MONGODB_DB або olx_watcher")
    ap.add_argument("--only", action="append", default=[], metavar="WATCH",
                    help="перевіряти лише ці watch'і (ім'я, id або source; можна кілька разів)")
    ap.add_argument("--skip", action="append", default=[], metavar="WATCH",
                    help="пропустити ці watch'і — напр. --skip Букфлі у хмарі")
    ap.add_argument("--dry-run", action="store_true", help="нічого не надсилати, лише показати")
    ap.add_argument("--reset", action="store_true", help="забути стан (наступний запуск буде seed)")
    ap.add_argument("--test-notify", action="store_true",
                    help="надіслати тестове повідомлення в канали й вийти (нічого не сканує)")
    ap.add_argument("--find-chat", action="store_true",
                    help="показати chat_id чатів, де бот нещодавно бачив повідомлення")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s │ %(message)s",
        datefmt="%H:%M:%S",
    )

    stale = check_modules()
    if stale:
        log.error("Файли проєкту з різних версій — бракує: %s", ", ".join(stale))
        log.error("Скопіюйте ВЕСЬ набір з одного коміту: main.py, notify.py, "
                  "olx.py, bookflea.py, models.py, watches.yaml.")
        return 1

    from_env_file = load_env()
    if from_env_file:
        # Лише імена — значення в лог не потрапляють ніколи.
        log.info(".env: підхопив %s", ", ".join(from_env_file))

    cfg = load_config(args.config)

    if args.find_chat:
        try:
            chats = notify.recent_chats()
        except Exception as exc:  # noqa: BLE001
            log.error("Не вдалось спитати Telegram: %s", exc)
            log.error("409 → на бота навішано webhook (зніміть deleteWebhook); "
                      "401 → хибний TELEGRAM_BOT_TOKEN.")
            return 1
        if not chats:
            log.error("Telegram не показав жодного чату.")
            log.error("Напишіть боту в потрібний чат (у приват — /start, у групі — "
                      "будь-яке повідомлення) і запустіть ще раз: getUpdates "
                      "пам'ятає лише останню добу.")
            return 1
        log.info("Знайдено чатів: %s", len(chats))
        for c in chats:
            log.info("  TELEGRAM_CHAT_ID=%-16s %-10s %s", c["id"], c["type"], c["title"])
        log.info("Потрібний рядок скопіюйте в .env, тоді: py main.py --test-notify")
        return 0

    if args.test_notify:
        channels = notify.build_channels(cfg)
        if not channels:
            log.error("Жодного каналу. Перевірте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID "
                      "у змінних середовища ЦЬОГО користувача.")
            return 1
        if notify.dispatch(channels, [notify.format_test()]):
            log.error("Тест не дійшов. Як читати відповідь Telegram вище: "
                      "400 chat not found → хибний TELEGRAM_CHAT_ID (або бот не в тому чаті); "
                      "401 Unauthorized → хибний TELEGRAM_BOT_TOKEN; "
                      "403 bot was blocked → бота заблоковано в чаті.")
            return 1
        log.info("✔ Тестове повідомлення надіслано — канал працює")
        return 0

    store = storage.open_store(state_path=args.state, mode=args.storage,
                               uri=args.mongo_uri, db_name=args.mongo_db)
    log.info("Стан: %s", store.describe())
    state = store.reset() if args.reset else store.load()

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
    undelivered = 0
    if messages and not channels:
        log.error("Є %s подій, але жодного налаштованого каналу сповіщень!", len(messages))
        errors += 1
        undelivered = len(messages)
    else:
        undelivered = notify.dispatch(channels, messages)
        if messages and undelivered:
            log.error("НЕ доставлено %s повідомлень — див. відповідь Telegram/SMTP вище", undelivered)
            errors += 1
        elif messages:
            log.info("Надіслано %s сповіщень", len(messages))

    # Стан зберігаємо, ЛИШЕ якщо все доїхало. Інакше оголошення осіло б у
    # state як «вже бачене», і після полагодження каналу про нього б ніхто
    # не дізнався — саме так хибний chat_id тихо з'їдав знахідки. Ціна —
    # можливий дубль тих повідомлень, що встигли пройти до збою.
    if undelivered:
        log.error("Стан НЕ збережено: наступний прогін спробує надіслати ці ж події ще раз")
    else:
        store.save(state)
        log.info("Стан збережено: %s", store.describe())
    store.close()

    # Не валимо workflow через тимчасову помилку мережі, якщо хоч щось спрацювало.
    if errors and errors >= max(len(selected), 1):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
