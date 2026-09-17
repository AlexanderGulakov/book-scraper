#!/usr/bin/env python3
"""Діагностика: чому OLX віддає 403.

Перебирає кілька стратегій і показує, яка спрацювала. Відповідь на головне
питання — блокують нас за TLS-відбиток (тоді рятує curl_cffi) чи за IP
дата-центру (тоді жоден код не допоможе, треба інший хост).

Запуск:  python probe.py [url]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

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


def main() -> int:
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
