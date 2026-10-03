#!/usr/bin/env python3
"""Діагностика: чому OLX віддає 403.

Перебирає кілька стратегій і показує, яка спрацювала. Відповідь на головне
питання — блокують нас за TLS-відбиток (тоді рятує curl_cffi) чи за IP
дата-центру (тоді жоден код не допоможе, треба інший хост).

Запуск:  python probe.py [url]
         python probe.py --rate     — скільки запитів поспіль OLX терпить
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from urllib.parse import quote

DEFAULT_URL = (
    "https://www.olx.ua/uk/hobbi-otdyh-i-sport/knigi-zhurnaly/"
    "q-%D1%84%D1%83%D0%BD%D0%B4%D0%B0%D1%86%D1%96%D1%8F/"
    "?currency=UAH&search%5Border%5D=created_at%3Adesc"
)
MARK = "__PRERENDERED_STATE__"


def verdict(status, body) -> str:
    if status != 200:
        from fetcher import describe_block
        return f"HTTP {status} · блокує: {describe_block(body or '')}"
    if MARK in (body or ""):
        return f"OK ✅  {len(body):,} байт, дані на місці"
    return f"HTTP 200, але без {MARK} (капча чи заглушка)"


def where_am_i() -> None:
    """IP і ASN — щоб побачити, чи це дата-центр."""
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=15) as r:
            info = json.loads(r.read())
        print(f"IP: {info.get('ip')}  ·  {info.get('org')}  ·  {info.get('country')}")
        print(f"Хостинг-провайдер у полі org — ознака дата-центру.\n")
    except Exception as exc:  # noqa: BLE001
        print(f"IP визначити не вдалось: {exc}\n")


# Різні запити, щоб навантаження виглядало як людське гортання, а не як
# довбання в одну адресу.
RATE_QUERIES = [
    "%D1%84%D1%83%D0%BD%D0%B4%D0%B0%D1%86%D1%96%D1%8F",           # фундація
    "%D0%BA%D1%96%D0%B4%D1%80%D1%83%D0%BA",                       # кідрук
    "%D0%B1%D1%83%D0%BD%D0%BA%D0%B5%D1%80",                       # бункер
    "%D1%80%D0%BE%D0%BD%D0%B0%D0%BB%D0%B4%D1%83",                 # роналду
    "%D1%82%D0%B0%D0%BB%D1%96%D1%81%D0%BC%D0%B0%D0%BD-%D0%BA%D1%96%D0%BD%D0%B3",  # талісман кінг
    "%D0%B2%D1%96%D0%B4%D1%80%D0%BE%D0%B4%D0%B6%D0%B5%D0%BD%D0%BD%D1%8F-%D0%BA%D1%96%D0%BD%D0%B3",
]


def rate_probe() -> int:
    """Скільки насправді треба чекати між запитами.

    Навіщо. `pause_between_watches: 3` з'явилось із обережності, а не з
    вимірювання, і коштує ~84 секунди на прогін — більше, ніж уся корисна
    робота. Блок, який ми колись ловили, був за TLS-відбитком, а не за
    частотою (перевірено 2026-09-17: requests з повними заголовками діставав
    403 навіть з першого запиту, curl_cffi — 200 одразу). Тобто ліміту
    частоти ми ніколи не бачили; цей режим має або показати його, або дати
    підставу паузу зменшити.

    Тест малий навмисно: шість запитів на кожен інтервал, максимум 30 разом.
    """
    import fetcher

    base = ("https://www.olx.ua/uk/hobbi-otdyh-i-sport/knigi-zhurnaly/q-%s/"
            "?currency=UAH&search%%5Border%%5D=created_at%%3Adesc")
    f = fetcher.Fetcher()
    f.warmup()

    print(f"{'інтервал':>9}  {'запитів':>7}  {'200':>4}  {'403/429':>7}  "
          f"{'сер. час':>9}   вердикт")
    print("-" * 78)

    worst = None
    for gap in (3.0, 2.0, 1.0, 0.5, 0.25):
        ok = blocked = 0
        spent = 0.0
        for i, q in enumerate(RATE_QUERIES):
            t0 = time.time()
            try:
                status, body = f.get(base % q, timeout=30)
            except Exception:  # noqa: BLE001
                status, body = 0, ""
            spent += time.time() - t0
            if status == 200 and MARK in body:
                ok += 1
            elif status in (403, 429, 503):
                blocked += 1
            if i + 1 < len(RATE_QUERIES):
                time.sleep(gap)
        verdict_txt = "чисто" if blocked == 0 else f"⚠ блокує з {gap} с"
        print(f"{gap:>8.2f}с  {len(RATE_QUERIES):>7}  {ok:>4}  {blocked:>7}  "
              f"{spent / len(RATE_QUERIES):>8.2f}с   {verdict_txt}")
        if blocked:
            worst = gap
            break
        time.sleep(5)          # видих між серіями

    print()
    if worst is None:
        print("➜ На 0.25 с між запитами блокування не було. Пауза "
              "`pause_between_watches` може бути 1 с із запасом у чотири рази.")
        print("  Але це один замір з одного IP: зменшуйте поступово (3 → 2 → 1) "
              "і дивіться на помилки в логах.")
    else:
        print(f"➜ Ліміт частоти існує: блокувати почало на {worst} с між запитами. "
              f"Тримайте `pause_between_watches` щонайменше вдвічі більшим.")
    return 0


# ─────────────────────────────────────────────────── JSON-пошук замість HTML

API_URL = ("https://www.olx.ua/api/v1/offers/?query={q}&limit=50&offset=0"
           "&currency=UAH&sort_by=created_at%3Adesc")


def api_probe() -> int:
    """Чи віддає OLX свій JSON-пошук нашому клієнтові — і наскільки він дешевший.

    Навіщо. Зараз один watch коштує HTML-сторінки пошуку (заміряно з браузера
    2026-10-03: ~1.85 с, 3.1 МБ, 52 оголошення, описів немає), плюс окремий
    запит на ОПИС кожного нового оголошення. Той самий запит через
    `/api/v1/offers/` віддав 0.35-0.58 с, 449 КБ, 65 оголошень — **з описами**.
    Тобто перехід прибирає і половину ваги, і цілий клас запитів.

    ⚠ Алеміряли це з браузерної сесії того самого походження і з української
    адреси. Чи працює воно з раннера GitHub (США, curl_cffi, без кук) — питання
    без відповіді, і саме його вирішує цей режим. Поки відповіді немає,
    переписувати `olx.py` не можна: ціна помилки — скрапер, який мовчить.
    """
    import fetcher

    print(f"curl_cffi встановлено: {fetcher.HAS_CURL_CFFI}\n")
    f = fetcher.Fetcher()
    queries = ["гаррі поттер", "макс кідрук", "дім дивних дітей"]

    ok = 0
    print(f"{'запит':<22} {'статус':<8} {'час':>7} {'КБ':>7} {'оголошень':>10} {'з описом':>9}")
    print("-" * 70)
    for q in queries:
        url = API_URL.format(q=quote(q))
        t0 = time.time()
        try:
            status, body = f.get(url, timeout=30)
        except Exception as exc:  # noqa: BLE001
            print(f"{q:<22} {type(exc).__name__}: {str(exc)[:40]}")
            continue
        dt = time.time() - t0
        n = desc = 0
        if status == 200:
            try:
                data = (json.loads(body) or {}).get("data") or []
                n = len(data)
                desc = sum(1 for d in data if (d.get("description") or "").strip())
                ok += 1
            except ValueError:
                status = "не JSON"
        print(f"{q:<22} {str(status):<8} {dt:6.2f}с {len(body)/1024:6.0f} "
              f"{n:>10} {desc:>9}")
        time.sleep(1)

    print()
    if ok == len(queries):
        print("➜ JSON-пошук працює з цієї адреси. Описи приходять разом із видачею,")
        print("  отже окремі запити на опис стають непотрібні.")
        return 0
    if ok:
        print("➜ Працює НЕ завжди — на таке спиратись не можна.")
        return 1
    print("➜ JSON-пошук з цієї адреси не віддається. Лишаємось на HTML.")
    return 1


def main() -> int:
    if "--rate" in sys.argv[1:]:
        where_am_i()
        return rate_probe()
    if "--api" in sys.argv[1:]:
        where_am_i()
        return api_probe()
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    where_am_i()

    import fetcher

    print(f"curl_cffi встановлено: {fetcher.HAS_CURL_CFFI}\n")
    print(f"{'стратегія':<34} {'результат'}")
    print("-" * 78)

    # 1. Голий urllib — контрольна точка, майже напевно 403
    try:
        req = urllib.request.Request(url, headers={"User-Agent": fetcher.DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
        print(f"{'urllib (без нічого)':<34} {verdict(r.status, body)}")
    except Exception as exc:  # noqa: BLE001
        print(f"{'urllib (без нічого)':<34} {type(exc).__name__}: {str(exc)[:60]}")

    # 2. requests з повними браузерними заголовками
    try:
        import requests
        s = requests.Session()
        r = s.get(url, headers=fetcher.browser_headers(fetcher.DEFAULT_UA), timeout=30)
        print(f"{'requests + заголовки Chrome':<34} {verdict(r.status_code, r.text)}")
    except Exception as exc:  # noqa: BLE001
        print(f"{'requests + заголовки Chrome':<34} {type(exc).__name__}: {str(exc)[:60]}")

    # 3. curl_cffi з різними версіями Chrome, з прогрівом головної і без
    if fetcher.HAS_CURL_CFFI:
        for imp in fetcher.IMPERSONATE_CANDIDATES:
            for warm in (False, True):
                label = f"curl_cffi {imp}{' + прогрів' if warm else ''}"
                try:
                    f = fetcher.Fetcher(backend="curl_cffi", impersonate=imp)
                    if warm:
                        f.warmup()
                    status, body = f.get(url, referer=fetcher.HOME if warm else None)
                    print(f"{label:<34} {verdict(status, body)}")
                    if status == 200 and MARK in body:
                        print(f"\n➜ Працює: OLX_BACKEND=curl_cffi OLX_IMPERSONATE={imp}"
                              f"{' (з прогрівом)' if warm else ''}")
                        return 0
                except Exception as exc:  # noqa: BLE001
                    print(f"{label:<34} {type(exc).__name__}: {str(exc)[:60]}")
                time.sleep(2)
    else:
        print("curl_cffi не встановлено — `pip install curl_cffi`")

    print(
        "\n➜ Жодна стратегія не пройшла. Якщо org вище — це хостинг (Microsoft/Azure для "
        "GitHub Actions), значить блок за IP дата-центру, і код тут безсилий: переносьте "
        "джобу на домашній ПК або self-hosted runner."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
