"""Мережевий шар.

OLX стоїть за анти-бот захистом, який дивиться не лише на заголовки, а й на
TLS-відбиток (JA3) та порядок HTTP/2-фреймів. Звичайний `requests` має
характерний «пітонівський» відбиток і ловить 403 з дата-центрів.

`curl_cffi` вміє прикидатися справжнім Chrome на рівні TLS — це знімає більшість
таких блоків. Якщо його немає, тихо відкочуємось на `requests`.
"""

from __future__ import annotations

import logging
import os
import random
import time

log = logging.getLogger("fetch")

try:
    from curl_cffi import requests as curl_requests  # type: ignore
    HAS_CURL_CFFI = True
except Exception:  # noqa: BLE001
    curl_requests = None  # type: ignore
    HAS_CURL_CFFI = False

import requests as plain_requests

# Версії, які підтримує curl_cffi. Перша, що спрацює, і буде використана.
IMPERSONATE_CANDIDATES = ["chrome131", "chrome124", "chrome120", "chrome110", "chrome"]

UA_BY_IMPERSONATE = {
    "chrome131": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "chrome124": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "chrome120": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "chrome110": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
}
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"

HOME = "https://www.olx.ua/uk/"


def browser_headers(ua: str, *, referer: str | None = None) -> dict[str, str]:
    """Повний набір заголовків, який шле справжній Chrome при переході по сайту."""
    major = "131"
    for part in ua.split("Chrome/")[-1:]:
        major = part.split(".")[0] or major
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not?A_Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }
    if referer:
        h["Referer"] = referer
    return h


class Fetcher:
    """Уніфікований фасад над curl_cffi / requests."""

    def __init__(self, backend: str | None = None, impersonate: str | None = None):
        self.backend = backend or os.getenv("OLX_BACKEND") or ("curl_cffi" if HAS_CURL_CFFI else "requests")
        if self.backend == "curl_cffi" and not HAS_CURL_CFFI:
            log.warning("curl_cffi не встановлено — відкочуюсь на requests (ймовірний 403)")
            self.backend = "requests"

        self.impersonate = impersonate or os.getenv("OLX_IMPERSONATE") or IMPERSONATE_CANDIDATES[0]
        self.ua = UA_BY_IMPERSONATE.get(self.impersonate, DEFAULT_UA)
        self._session = None
        self._warmed = False
        log.info("Мережевий бекенд: %s%s", self.backend,
                 f" (impersonate={self.impersonate})" if self.backend == "curl_cffi" else "")

    # ---------------------------------------------------------------- session

    def _new_session(self):
        if self.backend == "curl_cffi":
            return curl_requests.Session(impersonate=self.impersonate)  # type: ignore[union-attr]
        s = plain_requests.Session()
        return s

    @property
    def session(self):
        if self._session is None:
            self._session = self._new_session()
        return self._session

    def reset(self) -> None:
        try:
            if self._session is not None:
                self._session.close()
        except Exception:  # noqa: BLE001
            pass
        self._session = None
        self._warmed = False

    # ------------------------------------------------------------------- get

    def get(self, url: str, *, timeout: int = 30, referer: str | None = None):
        """Повертає (status_code, text). Винятків мережі не ковтає."""
        headers = browser_headers(self.ua, referer=referer)
        r = self.session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        return r.status_code, r.text

    def warmup(self) -> bool:
        """Зайти на головну, щоб отримати кукі сесії — як робить живий браузер."""
        if self._warmed:
            return True
        try:
            status, _ = self.get(HOME)
            self._warmed = status == 200
            log.debug("Прогрів головної: HTTP %s", status)
            time.sleep(1.0 + random.uniform(0, 1.5))
        except Exception as exc:  # noqa: BLE001
            log.debug("Прогрів не вдався: %s", exc)
        return self._warmed


def describe_block(body: str) -> str:
    """Намагається сказати, ХТО саме заблокував, — це вирішує, що робити далі."""
    low = body[:4000].lower()
    if "datadome" in low:
        return "DataDome"
    if "cloudflare" in low or "cf-ray" in low or "attention required" in low:
        return "Cloudflare"
    if "captcha" in low or "px-captcha" in low:
        return "капча"
    if "access denied" in low or "forbidden" in low:
        return "загальний 403"
    return "невідомо"
