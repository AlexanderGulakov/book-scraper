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
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

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
               "format_heartbeat", "format_watch_error", "format_cheapest",
               "format_loop_alarm", "format_loop_ok",
               "poll_marks", "confirm_mark", "Message"],
    "olx": ["build_session", "fetch_watch", "matches", "price_ok", "ad_state",
            "fetch_description"],
    "bookflea": ["collect"],
    "rules": ["decide", "Decision", "is_russian_text"],
    "analytics": ["profiles", "verdict_for_ad", "build_report", "report_due",
                  "mark_reported", "book_entry", "cheapest_now", "cheapest_ranked"],
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
        # Хвостовий коментар зрізаємо ДО лапок: `.env.example` сам показує
        # рядки виду `TELEGRAM_CHAT_ID=-100…  # OLX-books`, і без цього в
        # змінну їде «-100…  # OLX-books» — Telegram відповідає 400, а
        # виглядає це як хибний chat_id. Коментарем вважаємо лише `#` після
        # пробілу: у паролі MONGODB_URI решітка — звичайний символ.
        value = re.sub(r"\s+#.*$", "", value)
        # Лапки й пробіли зрізаємо тут-таки: саме через них Telegram місяцями
        # відповідав 400 chat not found на локальній машині.
        value = value.strip().strip('"').strip("'").strip()
        if not key:
            continue
        # Уже задана змінна головніша — але мовчати про це не можна: файл
        # виправили, а процес бере старе, і ніщо на це не вказує.
        current = os.environ.get(key)
        if current is not None and current != value:
            shown = "***" if any(s in key.upper() for s in ("TOKEN", "PASS", "URI", "SECRET")) else None
            log.warning(
                "%s у середовищі (%s) перебиває .env (%s). Щоб узяти значення з файлу: "
                "Remove-Item Env:%s",
                key, shown or current, shown or value, key)
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


def _check_alive(batch: list[tuple[str, str, dict[str, Any]]], session: Any, *,
                 workers: int, pause: float) -> list[str]:
    """['alive' | 'gone' | 'unknown', …] у тому ж порядку, що й `batch`.

    Стан тут НЕ змінюється: потоки лише ходять у мережу, а всі правки стану
    робить викликач на головному потоці. Інакше два потоки одночасно видаляли б
    з одного словника.

    ⚠ Сесію між потоками ділити НЕ можна: curl_cffi тримає всередині один
    curl-хендл, і паралельні запити з нього — гонка. Тому сесій рівно стільки,
    скільки потоків, і кожна дістається рівно одному завданню за раз. Перша —
    та, що прийшла ззовні: вона вже прогріта, і другий раз платити за це немає
    за що.
    """
    if workers <= 1 or len(batch) <= 1:
        out = []
        for i, (_k, _a, rec) in enumerate(batch):
            out.append(olx.ad_state(session, rec["url"]))
            if i + 1 < len(batch):
                time.sleep(pause + random.uniform(0, 1))
        return out

    import queue
    from concurrent.futures import ThreadPoolExecutor

    pool_size = min(workers, len(batch))
    sessions: queue.Queue = queue.Queue()
    sessions.put(session)
    for _ in range(pool_size - 1):
        s = olx.build_session()
        try:
            s.warmup()
        except Exception:  # noqa: BLE001 — прогрів не критичний
            pass
        sessions.put(s)

    def one(item: tuple[str, str, dict[str, Any]]) -> str:
        sess = sessions.get()
        try:
            verdict = olx.ad_state(sess, item[2]["url"])
        finally:
            # Пауза ДО повернення сесії в чергу: так сумарна частота виходить
            # workers/pause, а не «скільки встигне процесор».
            time.sleep(pause + random.uniform(0, 1))
            sessions.put(sess)
        return verdict

    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        return list(pool.map(one, batch))


def sold_report(cfg: dict[str, Any], state: dict[str, Any], session: Any,
                *, max_checks: int = 60, pause: float = 1.5, workers: int = 4,
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

    # Кандидати: зниклі, наймолодші оголошення — першими.
    #
    # ⚠ Тут двічі міняли правило, і обидва рази через одні й ті самі граблі.
    # Перша версія сортувала «найдавніше зниклі першими» — і 60 перевірок за
    # прогін діставались двотижневим лежням, поки свіжі чекали годинами. Це
    # заткнули порогом `sold_check_max_age_days: 14`, тобто просто викинули
    # старих із черги.
    #
    # Але поріг «зникнення = продаж» тепер 29 днів, і викидання з черги на
    # 14-му дні означало б, що друга половина цього вікна ніколи не
    # перевіряється: оголошення, яке провисіло 20 днів і продалось, ніхто б не
    # спитав, і в `sold` воно б не потрапило взагалі. Тобто поріг мовчки
    # з'їдав би саме ті продажі, заради яких вікно й розширили.
    #
    # Тому черга сортується за ВІКОМ оголошення, а не за часом зникнення:
    # свіжі попереду (їхнє зникнення найбільше схоже на продаж), старі
    # добирають рештки бюджету. Старіючи, оголошення саме з'їжджає в хвіст —
    # і це правильно, бо після 29 днів воно однаково вже не продаж.
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
    # Молодші вперед; за однакового віку — той, хто зник раніше.
    candidates.sort(key=lambda c: (_age_days(c[2], now_dt) if _age_days(c[2], now_dt)
                                   is not None else 1e9, str(c[2].get("miss"))))
    if skipped_old:
        log.info("Звіт про зняті: пропустив %s оголошень старших за %s дн.",
                 skipped_old, max_age_days)

    if not candidates:
        log.info("Звіт про зняті: перевіряти нема чого")
        return []

    log.info("Звіт про зняті: кандидатів %s, перевіряю до %s", len(candidates), max_checks)

    sold: list[dict[str, Any]] = []
    checked = 0
    batch = candidates[:max_checks]

    # Перевірки незалежні одна від одної: кожна — це GET сторінки оголошення,
    # який нічого не знає про решту. Послідовно вони коштували ~2 с × 60 = дві
    # хвилини, і саме вони робили годинний прогін уп'ятеро довшим за звичайний.
    #
    # ⚠ Сесію між потоками ділити НЕ можна: curl_cffi тримає всередині
    # один curl-хендл, і паралельні запити з нього — гонка. Тому в кожного
    # потоку власна сесія, створена один раз на потік.
    verdicts = _check_alive(batch, session, workers=workers, pause=pause)

    for (key, ad_id, rec), verdict in zip(batch, verdicts):
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


def keyword_ok(ad: Any, rules: dict[str, Any]) -> bool:
    """Правило на ОКРЕМЕ ключове слово watch'а (поки що лише Букфлі).

    Навіщо. У Букфлі одне ключове слово = один запит, тому вигідно шукати за
    прізвищем автора — воно ловить усі його книжки одразу. Але з Кінга
    потрібні дев'ять конкретних назв, а не все підряд, і `include_keywords`
    watch'а тут не годиться: він застосувався б і до Кідрука, і до Ріггза,
    і до Гаррі Поттера.

    `ad.matched` каже, яке саме слово спрацювало, тож обмеження вішається
    точково на нього.
    """
    rule = rules.get(str(getattr(ad, "matched", "") or ""))
    if not rule:
        return True
    return olx.matches(ad, include=rule.get("include") or [],
                       exclude=rule.get("exclude") or [])


def exclude_ads(state: dict[str, Any], ids: Iterable[str], *, why: str = "bad",
                now: str | None = None) -> int:
    """Викреслює оголошення назавжди: ні в статистику, ні в «найдешевше», ні в ТГ.

    Одне місце на всі три входи — кнопка в Telegram, `--mark-ru`/`--exclude` і
    `exclude.py` зі списком, — щоб вони не розійшлися. Ідемпотентна.

    `why`: "ru" — російське видання, "bad" — недійсне (не відкривається, хибне
    спрацювання, дивне видання, що ламає статистику). Наслідок однаковий,
    причина зберігається, бо через місяць інакше не розібрати, чому саме цей
    запис викинуто.
    """
    now = now or datetime.now(timezone.utc).isoformat()
    flagged: dict[str, Any] = state.setdefault("manual_ru", {})
    n = 0
    for ad_id in ids:
        ad_id = str(ad_id or "").strip()
        if not ad_id:
            continue
        # Старі записи — просто рядок з датою, і це означало «російське».
        # Не переписуємо їх: причина, вказана раніше, точніша за здогад.
        if ad_id not in flagged:
            # `an0` — чи рахувалось оголошення ДО позначки. Потрібне рівно для
            # одного: щоб `restore_ads` умів відкотити випадкове натискання, не
            # втягнувши назад комплект, якому `an: false` поставили правила.
            was = [(ws.get("ads") or {}).get(ad_id) for ws in
                   (state.get("watches") or {}).values()]
            countable = any(r is not None and r.get("an") is not False for r in was)
            flagged[ad_id] = {"at": now, "why": why, "an0": countable}
            n += 1
        # Оголошення живе в кількох watch'ах одразу — гасимо в усіх.
        for ws in (state.get("watches") or {}).values():
            rec = (ws.get("ads") or {}).get(ad_id)
            if rec is not None:
                rec["an"] = False
            ws.setdefault("dropped", {})[ad_id] = now
        # І з історії проданих теж: одне хибне спрацювання в sold зсуває
        # медіану сильніше, ніж живе оголошення в «лежать».
        for s in state.get("sold") or []:
            if str(s.get("id")) == ad_id:
                s["an"] = False
    return n


def restore_ads(state: dict[str, Any], ids: Iterable[str]) -> int:
    """Скасовує ручну позначку — для випадкового натискання кнопки.

    Дзеркало `exclude_ads`. Прибирає id з `manual_ru` і з `dropped` в усіх
    watch'ах, і знімає `an: false` — але ЛИШЕ якщо його поставили саме ми.

    Різниця принципова: `an: false` ставить ще й шар правил (комплекти, лоти
    без ціни), і зняти його наосліп означало б тихо втягнути комплект у
    медіану окремих книжок. Тому `exclude_ads` запам'ятовує в позначці, чи
    оголошення рахувалось ДО неї (`an0`), а тут ми лише відкочуємо до того
    стану.
    """
    n = 0
    flagged: dict[str, Any] = state.setdefault("manual_ru", {})
    for ad_id in ids:
        ad_id = str(ad_id or "").strip()
        mark = flagged.pop(ad_id, None)
        if mark is None:
            continue
        n += 1
        # Старий формат — просто рядок з датою. Тоді ми ще не запам'ятовували
        # попередній стан; вважаємо, що оголошення рахувалось, бо саме такі
        # позначали руками. Наступний прогін однаково перерахує `an` за
        # правилами, щойно побачить оголошення у видачі.
        countable = mark.get("an0", True) if isinstance(mark, dict) else True
        for ws in (state.get("watches") or {}).values():
            (ws.get("dropped") or {}).pop(ad_id, None)
            rec = (ws.get("ads") or {}).get(ad_id)
            if rec is not None and countable:
                rec.pop("an", None)
        if countable:
            for s in state.get("sold") or []:
                if str(s.get("id")) == ad_id:
                    s.pop("an", None)
    return n


# --------------------------------------------- про що вже казали в Telegram

def told_before(state: dict[str, Any], ad: Any, kind: str) -> bool:
    """Чи про це оголошення вже надсилали повідомлення — у БУДЬ-ЯКОМУ прогоні.

    ⚠ Це не те саме, що набір `announced` у межах прогону, і саме на цьому
    користувач отримував дублі. Стан оголошення тримає КОЖЕН watch окремо, а
    пошуки навмисне перетинаються: «Гаррі Поттер» і «Поттер: Філософський
    камінь» бачать ту саму книжку. Поки обидва встигали в один прогін, дубль
    гасив `announced`. Але watch'і мають різні `interval_minutes`, і другий
    watch зустрічав оголошення вже наступним прогоном — для нього воно нове,
    бо `prev is None`. Реальний випадок 30.09: те саме оголошення за 500 грн
    приїхало о 16:23 («Поттер: Філософський камінь») і о 16:31 («Гаррі Поттер»).

    Здешевлення — окрема подія: про нього кажемо, навіть якщо про саме
    оголошення вже казали. Але про ОДНУ Й ТУ САМУ ціну — лише раз.
    """
    rec = (state.get("told") or {}).get(str(getattr(ad, "id", "")))
    if rec is None:
        return False
    if kind == "new":
        return True
    return rec.get("price") == getattr(ad, "price", None)


def remember_told(state: dict[str, Any], ad: Any, now: str) -> None:
    state.setdefault("told", {})[str(getattr(ad, "id", ""))] = {
        "at": now, "price": getattr(ad, "price", None)}


def prune_told(state: dict[str, Any], days: int) -> int:
    """Забути, про що казали, за тим самим правилом, що й самі оголошення."""
    told = state.get("told") or {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    stale = [k for k, v in told.items()
             if str((v or {}).get("at") or "") < cutoff]
    for k in stale:
        del told[k]
    return len(stale)


def apply_marks(state: dict[str, Any]) -> int:
    """Забирає з Telegram натискання кнопок під сповіщенням і застосовує їх.

    Навіщо взагалі ручна позначка. Мову видання інколи видно ЛИШЕ з обкладинки:
    опис український, у заголовку ні слова про мову, а книжка російська.
    Реальний випадок 2026-09-29 — «Книги Ренсома Ріґґз Дім дивних дітей» за
    50 грн стало «найдешевшим» під українським виданням за 350. Жоден текстовий
    фільтр такого не бачить, і читати обкладинки ми не вміємо.

    Позначка робить дві речі: оголошення більше не потрапляє в статистику й у
    «найдешевше зараз» (`an: false`), і наступні прогони його не розглядають
    взагалі (`manual_ru`). Ідемпотентна: повторне натискання нічого не зіпсує.
    """
    marks, new_offset = notify.poll_marks(state.get("tg_offset"))
    if new_offset is not None:
        state["tg_offset"] = new_offset
    if not marks:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    for mark in marks:
        ad_id = str(mark.get("ad_id") or "")
        if not ad_id:
            continue
        why = str(mark.get("why") or "ru")
        exclude_ads(state, [ad_id], why=why, now=now)
        try:
            notify.confirm_mark(mark)
        except Exception as exc:  # noqa: BLE001
            log.debug("Не вдалось підтвердити позначку %s: %s", ad_id, exc)
        log.info("Викреслено вручну (%s): %s", why, ad_id)
    return len(marks)


class CheapestNow:
    """Рядок «найдешевше зараз» — з перевіркою, що посилання ще живе.

    Навіщо взагалі перевірка. Індекс рахується зі стану, а стан відстає: його
    зібрано на початку прогону, тобто за підсумками ПОПЕРЕДНЬОГО. Плюс саме
    посилання продавець може зняти між нашим запитом і нашим повідомленням.
    Виходило, що під свіжим оголошенням за 175 грн стояло «найдешевше: 150»,
    яке на момент приходу повідомлення вже не відкривалось (реальний випадок
    2026-09-30). Мертве посилання гірше за відсутність рядка: воно не просто не
    допомагає, воно бреше про ціну на ринку.

    Скільки це коштує. Один GET на кандидата, і тільки тоді, коли рядок справді
    їде в повідомленні — тобто кілька запитів на прогін, а не на кожне
    оголошення в базі. Вердикти кешуються в межах прогону, бюджет обмежений
    (`cheapest_verify_max`), а `unknown` (мережа підвела) вважається живим:
    краще показати рядок під сумнівом, ніж мовчати через тайм-аут.

    Що робить зі знятим. Не просто пропускає, а ховає: прибирає зі стану й
    кладе в `sold`. Це той самий висновок, який зробив би звіт про зняті, тільки
    отриманий раніше й безкоштовно — а для оголошень, старших за
    `sold_check_max_age_days`, взагалі єдиний, бо туди звіт не заглядає.
    """

    def __init__(self, cfg: dict[str, Any], state: dict[str, Any], session: Any,
                 *, verify: bool = True, budget: int = 12, depth: int = 3,
                 timeout: int = 20) -> None:
        self.cfg = cfg
        self.state = state
        self.session = session
        self.verify = bool(verify) and session is not None
        self.left = int(budget)
        self.depth = max(1, int(depth))
        self.timeout = int(timeout)
        self.checked: dict[str, bool] = {}   # id → живе
        self.buried = 0
        try:
            # Зниклих із видачі беремо в чергу лише коли є чим їх перевірити:
            # `miss` означає «його не було на наших сторінках», а не «знято» —
            # старе оголошення просто виштовхують свіжі. Перевірка розрізняє ці
            # два випадки, а без неї лишається тільки не показувати.
            self.ranked = analytics.cheapest_ranked(cfg, state,
                                                    include_missing=self.verify)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не вдалось порахувати найдешевші ціни: %s", exc)
            self.ranked = {}

    # -- публічне ------------------------------------------------------------

    def for_ad(self, watch_name: str, ad: Any) -> dict[str, Any] | None:
        """Найдешевше живе оголошення на ту саму книжку, крім самого себе.

        Себе пропускаємо, і це не дрібниця. Раніше оголошення, яке саме було
        найдешевшим, поверталось сюди само, і рядок казав «дешевше немає» —
        порожня відповідь на найцікавіший випадок. Тепер замість себе береться
        наступне за ціною: під ним поїде «🔸 Найближче», яким і міряється,
        чого варта знахідка, і саме його позначать праві кнопки.

        Ціна рішення — одна перевірка посилання там, де раніше не було жодної.
        Бюджет прогону (`cheapest_verify_max`) від цього не змінюється.
        """
        if not self.ranked:
            return None
        try:
            rows = self.ranked.get(self._key(watch_name, ad)) or []
        except Exception as exc:  # noqa: BLE001
            log.debug("Мінімум для %s не склався: %s", getattr(ad, "id", "?"), exc)
            return None
        mine = str(getattr(ad, "id", ""))
        tried = 0
        for row in rows:
            if str(row.get("id")) == mine:
                continue        # пропуск себе перевірки не коштує
            if tried >= self.depth:
                break
            tried += 1
            if self._alive(row):
                return row
        return None

    # -- всередині -----------------------------------------------------------

    def _key(self, watch_name: str, ad: Any) -> str:
        w = next((x for x in self.cfg["watches"] if (x.get("name") or "") == watch_name), {})
        return analytics.book_key(
            getattr(ad, "title", "") or "", watch_name,
            catalog=self.cfg.get("books") or [],
            include=w.get("include_keywords") or [],
            bundle_keywords=w.get("bundle_keywords") or [])

    def _alive(self, row: dict[str, Any]) -> bool:
        ad_id = str(row.get("id"))
        if ad_id in self.checked:
            return self.checked[ad_id]
        url = str(row.get("url") or "")
        if not self.verify or self.left <= 0 or "olx.ua" not in url:
            # Нічим перевірити. Тому, хто був у видачі, віримо; тому, хто з неї
            # зник, — ні: саме на цій довірі й поїхало мертве посилання.
            return not row.get("miss")
        self.left -= 1
        try:
            verdict = olx.ad_state(self.session, url, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            log.debug("Не вдалось перевірити найдешевше %s: %s", url, exc)
            verdict = "unknown"
        alive = verdict != "gone"
        self.checked[ad_id] = alive
        if not alive:
            log.info("  найдешевше %s уже знято — беру наступне", url)
            self._bury(row)
        return alive

    def _bury(self, row: dict[str, Any]) -> None:
        """Знятого прибираємо зі стану й записуємо в історію проданих."""
        try:
            ws = (self.state.get("watches") or {}).get(str(row.get("watch"))) or {}
            rec = (ws.get("ads") or {}).pop(str(row.get("id")), None)
            if rec is None:
                return
            now_dt = datetime.now(timezone.utc)
            days = _age_days(rec, now_dt)
            self.state.setdefault("sold", []).append({
                "id": str(row.get("id")), "watch": str(row.get("watch")),
                "title": rec.get("title"), "price": rec.get("price"),
                "cur": rec.get("cur"), "url": rec.get("url"),
                "days": None if days is None else round(days, 1),
                "gone": now_dt.isoformat(),
                **({"an": False} if rec.get("an") is False else {}),
            })
            self.buried += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("Не вдалось прибрати зняте %s: %s", row.get("id"), exc)


def daily_book_report(cfg: dict[str, Any], state: dict[str, Any],
                      *, store: Any = None) -> list[str]:
    """Раз на добу — один звіт «за скільки що беруть». Без мережі, лише стан."""
    opt = {**analytics.DEFAULTS, **(cfg.get("defaults") or {})}
    if not opt.get("analytics_enabled", True):
        return []
    if not analytics.report_due(state, opt):
        return []
    # Та сама заявка, що й для звіту про зняті: два незалежні прогони над
    # однією базою інакше надішлють денний звіт двічі.
    today = analytics._to_local(datetime.now(timezone.utc),
                               str(opt.get("daily_report_tz", "Europe/Kyiv"))).date().isoformat()
    if store is not None and not store.claim("book_report_date", today):
        log.info("Денний звіт уже застовпив інший прогін — мовчу")
        return []
    parts = analytics.build_report(cfg, state, esc=notify.esc)
    # Позначаємо день як відзвітований у будь-якому разі: інакше порожній
    # звіт перевірявся б наново щопрогону до півночі.
    analytics.mark_reported(state, opt)
    if not parts:
        log.info("Денний звіт про ціни: висновків поки нема, мовчу")
        return []
    log.info("Денний звіт про ціни складено: %s повідомл., %s символів",
             len(parts), sum(len(p) for p in parts))
    return parts


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
        only: list[str] | None = None, skip: list[str] | None = None,
        store: Any = None, reports: bool = True,
        sender: Any = None) -> tuple[list[str], int]:
    """`store` потрібен лише для заявки на звіти (див. `Store.claim`).

    `reports=False` вимикає обидва глобальні звіти — про зняті з продажу і
    денний про ціни. Пульс watch'ів це НЕ зачіпає: він належить конкретному
    пошуку, а не прогону, і домашній Букфлі має про себе звітувати.

    Без нього — стара поведінка «перевірив мітку в стані й пішов»: цього
    достатньо для тестів і для одного процесу, але не там, де над однією базою
    працюють два незалежні прогони.
    """
    defaults = cfg.get("defaults", {}) or {}
    session = olx.build_session()
    messages: list[str] = []
    errors = 0
    now = datetime.now(timezone.utc).isoformat()
    # Одне оголошення — одне повідомлення НАЗАВЖДИ, а не за прогін: пошуки
    # перетинаються навмисне, і другий watch зустрічає ту саму книжку вже
    # наступним прогоном. Пам'ять про це живе в `state["told"]` — див.
    # `told_before()`. У стані кожного watch оголошення все одно записується
    # окремо, бо ціну вони відстежують незалежно.
    dropped_told = prune_told(state, int(defaults.get("prune_days", 30)))
    if dropped_told:
        log.debug("Забув про %s давніх сповіщень", dropped_told)

    # Профілі цін рахуємо один раз на прогін — це чиста арифметика над станом,
    # без жодного запиту в мережу. Далі кожне сповіщення про оголошення
    # отримує з них коментар («Брати не думаючи» / «Задорого» / …).
    # Спершу ручні позначки: інакше щойно позначене оголошення ще раз потрапить
    # і в профілі цін, і в «найдешевше зараз» цього ж прогону.
    if not dry_run:
        try:
            marked = apply_marks(state)
            if marked:
                log.info("Застосовано ручних позначок: %s", marked)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не вдалось прочитати позначки з Telegram: %s", exc)

    try:
        prof_map = analytics.profiles(cfg, state)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не вдалось порахувати профілі цін: %s", exc)
        prof_map = {}

    # Найдешевше оголошення на кожну книжку — арифметика над станом, але з
    # однією перевіркою наживо перед самою відправкою: стан відстає на прогін,
    # і посилання встигає померти. Див. CheapestNow.
    cheapest = CheapestNow(
        cfg, state, session,
        verify=bool(defaults.get("cheapest_verify", True)) and not dry_run,
        budget=int(defaults.get("cheapest_verify_max", 12)),
        depth=int(defaults.get("cheapest_verify_depth", 3)),
    )

    # Бюджет описів — НА ПРОГІН, а не на watch. Двадцять один watch по тридцять
    # описів кожен — це шістсот зайвих запитів у найгіршому випадку, тобто
    # надійний спосіб познайомитись з анти-ботом.
    desc_left = [int((defaults.get("description_max_fetch") or 0))]

    # Яким пошуком іти в OLX. Змінна середовища головніша за конфіг навмисно:
    # відкотитись на HTML має бути можливо змінною в налаштуваннях репозиторію,
    # без коміту й без чекання на прогін. `api_fallback` рахує watch'і, яким
    # довелось добирати HTML, — одна цифра в кінці прогону замість тиші.
    search_api = (os.getenv("OLX_SEARCH")
                  or str(defaults.get("search_backend") or "html")).lower() == "api"
    api_fallback = [0]
    log.info("Пошук OLX: %s", "JSON API" if search_api else "HTML-сторінки")

    # Надсилання по ходу прогону. Злив робимо на ПОЧАТКУ кожної ітерації, а не
    # в кінці: у циклі кілька `continue`, і будь-який із них обійшов би злив,
    # поставлений унизу. Так знахідка з watch'а N їде, щойно почався N+1.
    # Невдачі рахує сам `Sender` (.failed) — не тягнемо ще одне значення
    # крізь повернення `run()`, бо його читає main() і так.
    def flush() -> None:
        if sender is None or dry_run or not messages:
            return
        sender.send(list(messages))
        messages.clear()

    for idx, w in enumerate(cfg["watches"]):
        flush()
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
            elif search_api:
                # JSON-пошук: ушестеро легший за HTML і несе описи. Якщо він
                # чомусь не віддався — не падаємо, а доробляємо цей watch по
                # HTML і голосно пишемо в лог. Відкотити все одразу можна
                # змінною OLX_SEARCH=html, без коміту.
                try:
                    ads = olx.fetch_watch_api(session, w["url"],
                                              pages=int(opt.get("pages", 1)))
                except olx.OlxError as exc:
                    api_fallback[0] += 1
                    log.warning("  ⚠ JSON-пошук не вдався (%s) — беру HTML", exc)
                    ads = olx.fetch_watch(session, w["url"],
                                          pages=int(opt.get("pages", 1)))
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
                require=opt.get("require_any", []) or [],
                cities=opt.get("cities", []) or [],
                skip_promoted=bool(opt.get("skip_promoted", False)),
                allow_similar=bool(opt.get("allow_similar", False)),
            )
        ]
        krules = opt.get("keyword_rules") or {}
        if krules:
            narrowed = [a for a in kept if keyword_ok(a, krules)]
            if len(narrowed) != len(kept):
                log.info("  правила ключових слів відсіяли %s", len(kept) - len(narrowed))
            kept = narrowed
        log.info("  знайдено %s, після фільтрів %s", len(ads), len(kept))

        # Оголошення, які шар правил уже визнав чужими. Тримаємо самі id:
        # без цього кожен прогін заново тягнув би опис російського видання,
        # щоб удруге дійти того самого висновку.
        refused: dict[str, Any] = ws.setdefault("dropped", {})
        catalog = cfg.get("books") or []
        # Опис уміє діставати лише парсер OLX. Букфлі віддає свій опис у видачі
        # й іншою розміткою, тож зайвий запит туди — це витрачений час і нуль
        # користі: `fetch_description` не знайде там __PRERENDERED_STATE__.
        want_desc = bool(opt.get("needs_description")) and source == "olx"

        new_cnt = drop_cnt = skip_cnt = quiet_cnt = dup_cnt = 0
        seeded_msgs: list[tuple[Any, Any]] = []
        manual_ru = state.get("manual_ru") or {}
        for ad in kept:
            if ad.id in refused or ad.id in manual_ru:
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
            # JSON-пошук несе опис одразу — тоді окремий запит не потрібен.
            # Це не лише швидше: бюджет `description_max_fetch` обмежував нас
            # сорока описами за прогін, тож решта оголошень проходила мовні
            # фільтри наосліп. Тепер опис є в кожного.
            desc = (prev or {}).get("desc") or getattr(ad, "description", None)
            if want_desc and desc is None and prev is None and desc_left[0] > 0:
                desc = olx.fetch_description(session, ad.url)
                desc_left[0] -= 1
                # Сторінка оголошення легша за сторінку пошуку, тож і пауза
                # менша: при сорока описах за прогін кожна зайва секунда — це
                # сорок секунд життя воркфлоу.
                time.sleep(float(defaults.get("description_pause", 0.4))
                           + random.uniform(0, 0.6))

            d = rules.decide(title=ad.title, price=ad.price, watch=opt,
                             entry=entry, description=desc,
                             include=opt.get("include_keywords", []) or [],
                             watch_name=name, catalog=catalog)

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
                if d.notify and not told_before(state, ad, "new"):
                    cheap = cheapest.for_ad(name, ad)
                    msg = notify.Message(notify.format_event(
                        "new", name, ad, verdict=_verdict(cfg, prof_map, name, ad),
                        mark=d.tag, cheapest=cheap),
                        mark_id=ad.id, cheap_id=(cheap or {}).get("id"),
                        cheap_near=notify.cheap_is_near(cheap, ad))
                    if first_run:
                        # Перший прогін watch'а. Рішення, слати чи мовчати,
                        # ухвалюється ПІСЛЯ всього циклу — коли відомо, скільки
                        # їх набралось. Див. `seed_notify_max` нижче.
                        seeded_msgs.append((msg, ad))
                    else:
                        messages.append(msg)
                        remember_told(state, ad, now)
                        new_cnt += 1
                elif not d.notify:
                    quiet_cnt += 1
                elif d.notify and not first_run:
                    dup_cnt += 1
            else:
                old = prev.get("price")
                threshold = float(opt.get("min_drop_percent", 1)) / 100.0
                cheaper = (
                    d.notify
                    and old is not None
                    and ad.price is not None
                    and ad.price < old * (1 - threshold)
                )
                if cheaper and not told_before(state, ad, "drop"):
                    cheap = cheapest.for_ad(name, ad)
                    messages.append(notify.Message(notify.format_event(
                        "drop", name, ad, old_price=old,
                        verdict=_verdict(cfg, prof_map, name, ad), mark=d.tag,
                        cheapest=cheap),
                        mark_id=ad.id, cheap_id=(cheap or {}).get("id"),
                        cheap_near=notify.cheap_is_near(cheap, ad)))
                    remember_told(state, ad, now)
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
            # 🔑 Перший прогін нового watch'а мовчав ЗАВЖДИ — і цим ковтав те
            # саме оголошення, заради якого watch і додавали. Реальний випадок:
            # «Продам комплект книг Гаррі Поттер» за 1100 грн лежав у видачі
            # `q-комплект-гаррі-поттер`, збиральний watch його не бачить
            # взагалі, а новий «Поттер: комплекти» зустрів його першим прогоном
            # і проковтнув. Оголошення не надішлеться вже ніколи: наступні
            # прогони бачать його як відоме.
            #
            # Мовчання саме по собі правильне — інакше новий watch вивалив би
            # сотню повідомлень за весь ринок. Але правило має бути не «завжди
            # мовчи», а «мовчи, якщо їх багато». Заміряно на `Поттер:
            # комплекти`: зі 101 оголошення у видачі фільтри лишають 49, а до
            # Telegram дійшли б 11 — одна нормальна пачка, не сотня.
            cap = int(defaults.get("seed_notify_max", 15))
            if seeded_msgs and len(seeded_msgs) <= cap:
                for msg, ad in seeded_msgs:
                    messages.append(msg)
                    remember_told(state, ad, now)
                log.info("  перший запуск: запам'ятав %s, з них надсилаю %s "
                         "(стеля seed_notify_max=%s)", len(kept), len(seeded_msgs), cap)
            elif seeded_msgs:
                log.info("  перший запуск: запам'ятав %s; %s пройшли б у Telegram, "
                         "але це більше за seed_notify_max=%s — мовчу",
                         len(kept), len(seeded_msgs), cap)
            else:
                log.info("  перший запуск: запам'ятав %s оголошень, слати нема чого",
                         len(kept))
        else:
            if dup_cnt:
                log.info("  %s вже надсилали з іншого пошуку — мовчу", dup_cnt)
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

        # Пауза потрібна лише перед НАСТУПНИМ запитом. Раніше умова рахувала
        # всі watch'і конфігу, включно з вимкненими й відкинутими --skip, тож
        # прогін засинав і після останнього — просто щоб завершитись пізніше.
        if any(w2.get("enabled") is not False and wanted(w2, i2, only or [], skip or [])
               for i2, w2 in enumerate(cfg["watches"]) if i2 > idx):
            time.sleep(float(defaults.get("pause_between_watches", 3))
                       + random.uniform(0, float(defaults.get("pause_jitter", 2))))

    # Раз на годину — звіт про зняті оголошення. У dry-run не чіпаємо: він
    # видаляє записи зі стану, а «показати й нічого не змінити» тут не вийде.
    every = defaults.get("sold_report_minutes", 60)
    # Заявка, а не перевірка: між «час настав» і збереженням мітки минає
    # півтори хвилини, і за цей час устигає стартувати другий прогін.
    # `store.claim()` робить перевірку й запис однією операцією.
    if every and reports and not dry_run \
            and due({"last_run": state.get("sold_report_last")}, every) \
            and (store is None or store.claim("sold_report_last", now)):
        try:
            messages.extend(sold_report(
                cfg, state, session,
                max_checks=int(defaults.get("sold_report_max_checks", 60)),
                pause=float(defaults.get("sold_report_pause", 1.5)),
                workers=int(defaults.get("sold_report_workers", 4)),
                max_age_days=defaults.get("sold_check_max_age_days", 14),
            ))
            state["sold_report_last"] = now
        except Exception as exc:  # noqa: BLE001
            log.warning("Звіт про зняті не склався: %s", exc)

    # Раз на добу — аналітика цін. Вона рахується зі стану, тому нічого не
    # питає в OLX і не залежить від того, чи вдався цей конкретний прогін.
    if reports and not dry_run:
        try:
            messages.extend(daily_book_report(cfg, state, store=store))
        except Exception as exc:  # noqa: BLE001
            log.warning("Денний звіт про ціни не склався: %s", exc)

    if api_fallback[0]:
        log.warning("JSON-пошук не спрацював у %s watch'ах — добирав HTML. "
                    "Якщо це повторюється, перемкніть OLX_SEARCH=html.", api_fallback[0])

    if cheapest.buried:
        log.info("«Найдешевше зараз»: %s посилань виявились знятими — прибрав зі стану",
                 cheapest.buried)

    if not reports:
        log.info("Звіти вимкнені (--no-reports): їх складає інший прогін")

    if dry_run and messages:
        log.info("--dry-run: %s повідомлень НЕ надіслано:\n%s", len(messages), "\n---\n".join(messages))
        messages = []

    return messages, errors


def explain_ad(cfg: dict[str, Any], url: str) -> int:
    """Прогнати одне оголошення крізь усі watch'і й сказати, що з ним сталось.

    Навіщо. «Чомусь не прийшло» — найчастіше питання до цього скрапера, і
    відповідей на нього десяток: фільтр заголовка, мова, ціна, стоп-слово,
    оголошення взагалі не в тій видачі. Вгадувати щоразу дорожче, ніж один раз
    написати команду, яка показує рішення по кожному watch'у.

    Мережею тут ходимо лише по саму сторінку оголошення — стан не читаємо і не
    чіпаємо, тож запускати безпечно будь-коли.
    """
    session = olx.build_session()
    defaults = cfg.get("defaults") or {}
    catalog = cfg.get("books") or []

    try:
        status, body = session.get(url, timeout=30)
    except Exception as exc:  # noqa: BLE001
        log.error("Не вдалось відкрити сторінку: %s", exc)
        return 1
    if status != 200:
        log.error("OLX відповів %s — оголошення знято або посилання хибне.", status)
        return 1

    import json as _json
    m = olx._STATE_RE.search(body)
    if not m:
        log.error("На сторінці немає __PRERENDERED_STATE__ (капча?).")
        return 1
    data = _json.loads(_json.loads(m.group(1)))
    raw = (data.get("ad") or {}).get("ad") or data.get("ad") or {}
    price = ((raw.get("price") or {}).get("regularPrice") or {}).get("value")
    ad = olx.Ad(
        id=str(raw.get("id") or "?"), title=(raw.get("title") or "").strip(),
        url=url, price=float(price) if isinstance(price, (int, float)) else None,
        currency=((raw.get("price") or {}).get("regularPrice") or {}).get("currencyCode"),
        price_text=(raw.get("price") or {}).get("displayValue") or "—",
        city=(raw.get("location") or {}).get("cityName"),
        created_time=raw.get("createdTime"), source="olx",
    )
    desc = olx.fetch_description(session, url)

    log.info("Оголошення: %s", ad.title)
    log.info("Ціна: %s · подано: %s · статус: %s",
             ad.price_text, ad.created_time, raw.get("status"))
    log.info("Опис: %s", (desc or "(не вдалось дістати)")[:200])
    log.info("")

    hit = False
    for idx, w in enumerate(cfg["watches"]):
        if str(w.get("source", "olx")).lower() != "olx":
            continue
        name = w.get("name") or watch_key(w, idx)
        opt = {**defaults, **w}
        shared = (defaults.get("exclude_keywords") or []) if opt.get("use_default_excludes", True) else []
        opt["exclude_keywords"] = list(dict.fromkeys(shared + (w.get("exclude_keywords") or [])))
        opt["drop_keywords"] = list(dict.fromkeys(
            (defaults.get("drop_keywords") or []) + (w.get("drop_keywords") or [])))

        if not olx.matches(ad, include=opt.get("include_keywords", []) or [],
                           exclude=opt.get("exclude_keywords", []) or [],
                           require=opt.get("require_any", []) or [],
                           cities=opt.get("cities", []) or [],
                           skip_promoted=bool(opt.get("skip_promoted", False))):
            continue          # цей watch його просто не про це — мовчимо
        hit = True
        entry = analytics.book_entry(ad.title, name, catalog)
        d = rules.decide(title=ad.title, price=ad.price, watch=opt, entry=entry,
                         description=desc, include=opt.get("include_keywords", []) or [],
                         watch_name=name, catalog=catalog)
        icon = {"notify": "📨 Telegram", "store": "💾 лише база", "skip": "✕ відкинуто"}[d.action]
        log.info("%-34s %-13s %s", name, icon, d.reason)
        log.info("%-34s книжка: %s%s", "", entry["name"] if entry else "(за назвою watch\'а)",
                 "" if d.analytics else " · поза статистикою")

    log.info("")
    log.info("Тут показано лише рішення ПРАВИЛ. Watch може приймати оголошення "
             "за фільтрами й водночас ніколи його не бачити — якщо воно не "
             "потрапляє у видачу його запиту.")

    if not hit:
        log.info("Жоден watch не бере це оголошення: воно не проходить фільтр "
                 "заголовка (include/exclude) в усіх пошуках.")
        log.info("Але це ще не все: оголошення може не потрапляти й у саму "
                 "видачу пошуку — тоді скрапер його просто не бачить. "
                 "Перевірте, чи знаходить його ваш запит на сайті.")
    return 0


def one_pass(args: Any, cfg: dict[str, Any], store: Any) -> int:
    """Один повний прохід: стан → watch'і → сповіщення → збереження.

    Винесено з `main()` заради `--loop`: у циклі той самий процес робить
    прохід за проходом, не платячи щоразу за старт раннера, `pip install`,
    прогрів curl_cffi і конект до Mongo. Стан читається й пишеться КОЖНОГО
    проходу, бо в ту саму базу паралельно пише домашній Букфлі.
    """
    state = store.reset() if args.reset else store.load()

    gone = forget_orphans(cfg, state)
    if gone:
        log.info("Прибрав зі стану watch'і, яких уже немає в конфізі: %s", ", ".join(gone))

    selected = [w for i, w in enumerate(cfg["watches"])
                if w.get("enabled") is not False and wanted(w, i, args.only, args.skip)]
    if args.only or args.skip:
        log.info("Цього разу перевіряю %s з %s: %s", len(selected), len(cfg["watches"]),
                 ", ".join(str(w.get("name") or "?") for w in selected) or "нічого")

    channels = notify.build_channels(cfg)
    # Надсилаємо по ходу прогону, а не купою в кінці: знахідка з першого
    # watch'а інакше чекає, доки відпрацюють решта тридцять і звіт про зняті.
    sender = notify.Sender(channels) if channels and not args.dry_run else None

    messages, errors = run(cfg, state, dry_run=args.dry_run, only=args.only,
                           skip=args.skip, store=store,
                           reports=not args.no_reports, sender=sender)

    if messages and not channels:
        log.error("Є %s подій, але жодного налаштованого каналу сповіщень!", len(messages))
        errors += 1
        undelivered = len(messages)
    elif sender is not None:
        # Хвіст: останній watch і звіти. Через той самий Sender, щоб ліміт на
        # прогін лишився спільним, а не подвоївся.
        undelivered = sender.failed + sender.send(messages)
        sender.finish()
        if undelivered:
            log.error("НЕ доставлено %s повідомлень — див. відповідь Telegram/SMTP вище",
                      undelivered)
            errors += 1
        elif sender.sent:
            log.info("Надіслано %s сповіщень (по ходу прогону)", sender.sent)
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

    # Не валимо workflow через тимчасову помилку мережі, якщо хоч щось спрацювало.
    if errors and errors >= max(len(selected), 1):
        return 1
    return 0


def run_loop(args: Any, cfg: dict[str, Any], store: Any) -> int:
    """`--loop`: проходи кожні `--loop-seconds`, доки не вичерпано `--deadline-minutes`.

    Чотири правила, кожне заради того, щоб цикл не став новою точкою відмови:

    1. Виняток в одному проході НЕ валить цикл — наступний піде за розкладом.
       Інакше одна мережева помилка коштувала б тиші до наступного крона, а в
       циклі «наступний крон» — це аж новий раннер.
    2. `--max-fails` невдач поспіль — виходимо з кодом 1. Процес, у якому
       зламалась сесія чи конект до Mongo, лікується новим раннером, а не
       наступною спробою в тому самому процесі; watchdog-dispatch його підніме.
    3. Дедлайн перевіряється ДО сну: якщо наступний прохід не встигне
       вкластися, виходимо самі. Інакше GitHub уб'є джоб по `timeout-minutes`
       посеред запису стану.
    4. Про поломку пишемо в Telegram — див. `_alarm()`. Без цього цикл, що
       впав, виглядає з боку Telegram точно як «нічого не знайшлось».

    Конфіг перечитується, якщо файл змінився на диску — вдома це рятує від
    перезапуску заради однієї правки в `watches.yaml`; в Actions чекаут
    статичний, тож там це просто нічого не робить.
    """
    period = max(20.0, float(args.loop_seconds))
    deadline = (time.monotonic() + float(args.deadline_minutes) * 60
                if args.deadline_minutes else None)
    cfg_mtime = _cfg_mtime(args.config)
    passes = ok = fails = 0
    alarmed = False          # чи вже писали в Telegram про ЦЮ серію невдач
    last_exc: Any = None
    log.info("Цикл: прохід кожні %.0f с, %s", period,
             f"дедлайн {args.deadline_minutes:.0f} хв" if deadline else "без дедлайну")
    while True:
        started = time.monotonic()
        try:
            rc = one_pass(args, cfg, store)
            ok += rc == 0
            if rc:
                fails += 1
                last_exc = "прохід завершився з помилками (деталі в логах)"
            else:
                if alarmed:
                    _alarm(args, cfg, notify.format_loop_ok(
                        fails, minutes=fails * period / 60))
                    alarmed = False
                fails = 0
        except Exception as exc:  # noqa: BLE001
            fails += 1
            last_exc = exc
            log.exception("Прохід впав (%s поспіль): %s", fails, exc)
        passes += 1
        # --reset має сенс лише на першому проході, інакше цикл щохвилини
        # робив би seed і мовчав назавжди.
        args.reset = False

        if args.max_fails and fails >= int(args.max_fails):
            log.error("%s невдалих проходів поспіль — виходжу, хай підніметься "
                      "свіжий раннер", fails)
            _alarm(args, cfg, notify.format_loop_alarm(fails, last_exc, fatal=True))
            return 1

        # Тривога — рівно раз на серію, а не на кожен прохід: при аварії OLX
        # це була б тридцятка однакових повідомлень за годину.
        if not alarmed and args.alert_after and fails >= int(args.alert_after):
            _alarm(args, cfg, notify.format_loop_alarm(fails, last_exc))
            alarmed = True

        mtime = _cfg_mtime(args.config)
        if mtime != cfg_mtime:
            cfg_mtime = mtime
            try:
                cfg = load_config(args.config)
                log.info("Конфіг перечитано: %s", args.config)
            except Exception as exc:  # noqa: BLE001
                log.error("Новий конфіг не читається (%s) — лишаю попередній", exc)

        took = time.monotonic() - started
        nxt = started + period
        if deadline is not None and nxt + took >= deadline:
            log.info("Дедлайн: %s проходів, з них вдалих %s. Виходжу штатно.",
                     passes, ok)
            return 0 if ok else 1
        sleep_for = nxt - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            log.warning("Прохід (%.0f с) довший за період (%.0f с) — наступний "
                        "починаю одразу", took, period)


def _alarm(args: Any, cfg: dict[str, Any], text: str) -> None:
    """Сповіщення про стан самого циклу — повз `Sender` і повз ліміт на прогін.

    Саме повз: ліміт 25 повідомлень існує, щоб знахідки не залили чат, а
    «скрапер лежить» не має в нього впиратись. І повз стан теж — тривога
    нікуди не зберігається, тож її не «з'їсть» невдалий запис у Mongo.

    Жодна помилка тут не має права підняти виняток: якщо Telegram недоступний,
    це ще не привід зупиняти цикл, який саме намагається вижити.
    """
    if getattr(args, "dry_run", False):
        log.info("[dry-run] тривога не надсилається: %s", text.splitlines()[0])
        return
    try:
        channels = notify.build_channels(cfg)
        if not channels:
            log.error("Нікуди надіслати тривогу: жодного каналу сповіщень")
            return
        if notify.dispatch(channels, [text]):
            log.error("Тривога НЕ доставлена — див. відповідь Telegram вище")
    except Exception as exc:  # noqa: BLE001
        log.error("Не вдалось надіслати тривогу (%s) — цикл продовжую", exc)


def _cfg_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None



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
    ap.add_argument("--no-reports", action="store_true",
                    help="не складати звіт про зняті й денний звіт цін — "
                         "їх бере на себе інший прогін")
    ap.add_argument("--mark-ru", metavar="ID|URL", action="append", default=[],
                    help="позначити оголошення російським вручну (без Telegram)")
    ap.add_argument("--exclude", metavar="ID|URL", action="append", default=[],
                    help="викреслити оголошення назавжди (не відкривається, "
                         "хибне спрацювання, дивне видання)")
    ap.add_argument("--unexclude", metavar="ID|URL", action="append", default=[],
                    help="скасувати ручну позначку (випадково натиснув кнопку)")
    ap.add_argument("--poll-marks", action="store_true",
                    help="лише забрати натискання кнопок з Telegram і вийти — "
                         "без жодного запиту в OLX, секунда роботи")
    ap.add_argument("--explain", metavar="URL",
                    help="чому конкретне оголошення прийшло або не прийшло")
    ap.add_argument("--find-chat", action="store_true",
                    help="показати chat_id чатів, де бот нещодавно бачив повідомлення")
    # --- Цикл у межах одного процесу ----------------------------------------
    # Холодний старт (черга раннера + checkout + pip install) коштує 1-1.5 хв на
    # КОЖЕН запуск, а сам прохід — ~40 с. Тому замість тридцяти запусків за
    # годину вигідніший один, який усередині робить прохід щохвилини.
    ap.add_argument("--loop", action="store_true",
                    help="не виходити після проходу, а повторювати його")
    ap.add_argument("--loop-seconds", type=float, default=60,
                    help="період проходу в циклі (за замовч. 60 с)")
    ap.add_argument("--deadline-minutes", type=float, default=0,
                    help="скільки хвилин крутитись; 0 — без обмеження. Має бути "
                         "помітно меншим за timeout-minutes воркфлоу, інакше "
                         "GitHub уб'є джоб посеред запису стану")
    ap.add_argument("--max-fails", type=int, default=5,
                    help="скільки невдалих проходів поспіль терпіти, перш ніж "
                         "вийти з кодом 1 (хай підніметься свіжий раннер)")
    ap.add_argument("--alert-after", type=int, default=2,
                    help="після скількох невдалих проходів поспіль писати "
                         "тривогу в Telegram; 0 — не писати. Повідомлення одне "
                         "на серію, плюс «знову працює» після відновлення")
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

    if args.poll_marks:
        # Окремий режим, бо кнопка в Telegram крутить «годинник», доки хтось не
        # відповість на натискання, а повний прогін буває раз на 15 хвилин.
        # Тут немає жодного запиту в OLX, тож це можна ганяти хоч щохвилини з
        # планувальника — і кнопка почне відповідати одразу.
        store = storage.open_store(state_path=args.state, mode=args.storage,
                                   uri=args.mongo_uri, db_name=args.mongo_db)
        state = store.load()
        n = apply_marks(state)
        if n:
            store.save(state)
        log.info("Оброблено натискань: %s", n)
        store.close()
        return 0

    if args.unexclude:
        store = storage.open_store(state_path=args.state, mode=args.storage,
                                   uri=args.mongo_uri, db_name=args.mongo_db)
        state = store.load()
        ids = []
        for raw in args.unexclude:
            ad_id = olx.ad_id_from(raw)
            if ad_id is None:
                log.error("Не розпізнав оголошення: %s", raw)
                continue
            ids.append(ad_id)
        n = restore_ads(state, ids)
        store.save(state)
        log.info("Знято позначок: %s. Лишилось у базі: %s",
                 n, len(state.get("manual_ru") or {}))
        if n:
            log.info("Оголошення повернеться в статистику й у «найдешевше зараз» "
                     "наступним прогоном, щойно його знову побачать у видачі.")
        return 0

    if args.mark_ru or args.exclude:
        # ⚠ Тут стояло `open_store(args, cfg)`, а він приймає лише іменовані
        # аргументи — тобто гілка падала з TypeError ще до того, як дійти до
        # неправильного id. Перевірено на живій базі 2026-09-30.
        store = storage.open_store(state_path=args.state, mode=args.storage,
                                   uri=args.mongo_uri, db_name=args.mongo_db)
        state = store.load()
        added = 0
        for raw_list, why in ((args.mark_ru, "ru"), (args.exclude, "bad")):
            for raw in raw_list:
                # ⚠ У посиланні OLX показує id, закодований base62, а не той,
                # яким ключується стан. Раніше тут стояв regex, що брав код
                # як є, — і позначка за посиланням мовчки нікуди не потрапляла.
                ad_id = olx.ad_id_from(raw)
                if ad_id is None:
                    log.error("Не розпізнав оголошення: %s", raw)
                    continue
                added += exclude_ads(state, [ad_id], why=why)
                log.info("Викреслено (%s): %s ← %s", why, ad_id, raw)
        store.save(state)
        log.info("Додано нових: %s. Усього ручних позначок у базі: %s",
                 added, len(state.get("manual_ru") or {}))
        return 0

    if args.explain:
        return explain_ad(cfg, args.explain)

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
    try:
        if args.loop:
            return run_loop(args, cfg, store)
        return one_pass(args, cfg, store)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
