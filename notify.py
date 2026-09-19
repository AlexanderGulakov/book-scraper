"""Канали сповіщень: Telegram (основний) і, за бажанням, e-mail через SMTP."""

from __future__ import annotations

import html
import logging
import os
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from typing import Any

import requests

log = logging.getLogger("notify")

TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGES_PER_RUN = 25  # запобіжник від флуду, якщо пошук раптом віддав сотні збігів


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.session = requests.Session()

    def send(self, text: str, *, photo: str | None = None) -> bool:
        if photo:
            ok = self._call("sendPhoto", {"photo": photo, "caption": text[:1024], "parse_mode": "HTML"})
            if ok:
                return True
            log.warning("sendPhoto не вдався, надсилаю текстом")
        return self._call("sendMessage", {
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

    def _call(self, method: str, payload: dict[str, Any]) -> bool:
        payload = {"chat_id": self.chat_id, **payload}
        for attempt in range(3):
            try:
                r = self.session.post(TG_API.format(token=self.token, method=method), data=payload, timeout=30)
                if r.status_code == 429:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                    log.warning("Telegram rate limit, чекаю %sс", wait)
                    time.sleep(wait + 1)
                    continue
                if r.ok and r.json().get("ok"):
                    return True
                log.error("Telegram %s: %s %s", method, r.status_code, r.text[:300])
                hint = {
                    400: "хибний TELEGRAM_CHAT_ID, або бота немає в цьому чаті "
                         "(у приватний чат бот не напише першим — спершу треба /start)",
                    401: "хибний TELEGRAM_BOT_TOKEN",
                    403: "бота заблоковано або вигнано з чату",
                }.get(r.status_code)
                if hint:
                    log.error("  ↳ найімовірніше: %s", hint)
                return False
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram помилка мережі (%s/3): %s", attempt + 1, exc)
                time.sleep(3 * (attempt + 1))
        return False


class Email:
    def __init__(self, host: str, port: int, user: str, password: str, to: str, use_tls: bool = True):
        self.host, self.port, self.user, self.password, self.to, self.use_tls = (
            host, port, user, password, to, use_tls,
        )

    def send(self, subject: str, body_html: str) -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.user
        msg["To"] = self.to
        msg.set_content("Ваш поштовий клієнт не показує HTML.")
        msg.add_alternative(body_html, subtype="html")
        try:
            if self.use_tls:
                with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                    s.starttls()
                    s.login(self.user, self.password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as s:
                    s.login(self.user, self.password)
                    s.send_message(msg)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("SMTP помилка: %s", exc)
            return False


SOURCE_LABEL = {"olx": "OLX", "bookflea": "Букфлі"}


def format_event(kind: str, watch_name: str, ad, old_price: float | None = None) -> str:
    """kind: 'new' | 'drop'"""
    where = SOURCE_LABEL.get(getattr(ad, "source", "olx"), "")
    tag = f"{esc(watch_name)}"
    if getattr(ad, "matched", None):
        tag += f" → «{esc(ad.matched)}»"
    if where and where.lower() not in watch_name.lower():
        tag += f" · {where}"

    if kind == "drop" and old_price:
        delta = old_price - (ad.price or 0)
        pct = delta / old_price * 100 if old_price else 0
        head = (
            f"📉 <b>Ціна впала</b> — {tag}\n"
            f"<s>{esc(_fmt(old_price, ad.currency))}</s> → <b>{esc(ad.price_text)}</b>"
            f"  (−{esc(_fmt(delta, ad.currency))}, −{pct:.0f}%)"
        )
    else:
        head = f"🆕 <b>Нове оголошення</b> — {tag}\n💰 <b>{esc(ad.price_text)}</b>"

    caption = esc(ad.title)
    if getattr(ad, "author", None):
        caption = f"{esc(ad.author)} — {caption}"
    bits = [head, f"\n<a href=\"{esc(ad.url)}\">{caption}</a>"]

    meta = [x for x in (ad.city, ad.condition) if x]
    if ad.negotiable:
        meta.append("торг")
    if meta:
        bits.append("📍 " + esc(" · ".join(meta)))
    return "\n".join(bits)


def format_heartbeat(watch_name: str, *, keywords: int, seen: int, kept: int,
                     known: int, when: str | None = None) -> str:
    """«Живий, просто нічого нового» — пульс для watch'а з heartbeat_hours.

    Навіщо. Прогін, у якому нічого не знайшлось, і прогін, у якому скрапер
    тихо зламався, з боку Telegram виглядають однаково — ніяк. Пульс робить
    тишу помітною: поки повідомлення приходять, мовчання каналу означає
    поломку, а не відсутність книжок.
    """
    head = f"💤 <b>{esc(watch_name)}</b> — нічого нового"
    line = f"🔎 слів: {keywords} · у видачі: {seen} · підійшло: {kept} · у пам'яті: {known}"
    return "\n".join([head, line, f"🕒 {esc(when or _now_label())}"])


def format_watch_error(watch_name: str, exc: Any) -> str:
    """Скрапер упав. Для watch'а з пульсом про це треба сказати вголос."""
    return "\n".join([
        f"⚠️ <b>{esc(watch_name)}</b> — перевірка не вдалась",
        f"<code>{esc(exc)}</code>",
        f"🕒 {esc(_now_label())}",
    ])


def _now_label() -> str:
    return datetime.now().strftime("%d.%m, %H:%M")


def _fmt(value: float | None, currency: str | None) -> str:
    if value is None:
        return "—"
    sym = {"UAH": "грн.", "USD": "$", "EUR": "€"}.get((currency or "").upper(), currency or "")
    whole = f"{value:,.0f}".replace(",", " ")
    return f"{whole} {sym}".strip()


def _clean(value: str | None) -> str:
    return (value or "").strip().strip('"').strip("'").strip()


def format_test() -> str:
    """Повідомлення для --test-notify."""
    return "\n".join([
        "✅ <b>Перевірка зв'язку</b>",
        "Бачиш це — отже токен і chat_id правильні.",
        f"🕒 {_now_label()}",
    ])


def build_channels(cfg: dict[str, Any]) -> list[Any]:
    """Збирає канали з env-змінних. Секрети НІКОЛИ не лежать у конфігу."""
    channels: list[Any] = []

    # .strip() не косметика: у Windows `set TELEGRAM_CHAT_ID="123"` кладе лапки
    # ВСЕРЕДИНУ значення, а зайвий пробіл у кінці рядка .bat так само стає
    # частиною змінної. І те, й те Telegram повертає як 400 chat not found.
    token = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
    chat = _clean(os.getenv("TELEGRAM_CHAT_ID"))
    if token and chat:
        channels.append(Telegram(token, chat))
    elif cfg.get("notify", {}).get("telegram", True):
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані — Telegram вимкнено")

    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USER"):
        channels.append(
            Email(
                host=os.environ["SMTP_HOST"],
                port=int(os.getenv("SMTP_PORT", "587")),
                user=os.environ["SMTP_USER"],
                password=os.environ.get("SMTP_PASSWORD", ""),
                to=os.getenv("SMTP_TO") or os.environ["SMTP_USER"],
                use_tls=os.getenv("SMTP_TLS", "1") != "0",
            )
        )
    return channels


def dispatch(channels: list[Any], messages: list[str]) -> int:
    """Розсилає й повертає КІЛЬКІСТЬ невдалих надсилань.

    Раніше результат `send()` ігнорувався, і лог бадьоро писав «Надіслано N
    сповіщень» навіть тоді, коли Telegram на кожне відповідав 400. Саме так
    можна місяцями не помічати хибний chat_id.
    """
    if not messages:
        return 0
    overflow = len(messages) - MAX_MESSAGES_PER_RUN
    to_send = messages[:MAX_MESSAGES_PER_RUN]
    if overflow > 0:
        to_send.append(f"… і ще <b>{overflow}</b> збігів цього разу (звузьте фільтри або зменште інтервал).")

    failed = 0
    for ch in channels:
        if isinstance(ch, Telegram):
            for msg in to_send:
                if not ch.send(msg):
                    failed += 1
                time.sleep(1.2)  # Telegram: ~30 повідомлень/сек загалом, 1/сек у чат
        elif isinstance(ch, Email):
            body = "<hr>".join(m.replace("\n", "<br>") for m in to_send)
            if not ch.send(f"OLX: {len(messages)} нових подій", body):
                failed += 1
    return failed
