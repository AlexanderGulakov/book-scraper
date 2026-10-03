"""Канали сповіщень: Telegram (основний) і, за бажанням, e-mail через SMTP."""

from __future__ import annotations

import html
import json
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
MAX_MESSAGES_PER_RUN = 25
# Telegram: ~30 повідомлень/сек загалом, але не більше одного на секунду в
# той самий чат. Константа, а не літерал у циклі, щоб тести не спали.
SEND_PAUSE = 1.2  # запобіжник від флуду, якщо пошук раптом віддав сотні збігів


class Message(str):
    """Текст повідомлення плюс те, чого в тексті не видно.

    Це підклас `str`, а не окремий тип, і навмисно: повідомлення ходять через
    `dispatch`, лог, e-mail і півдесятка тестів, які працюють з ними як з
    рядками. Перша спроба зробити з них NamedTuple миттєво поламала п'ять
    тестів на `"текст" in msg` — а зламати розсилку заради однієї кнопки було б
    погано.

    `mark_id` — id САМОГО оголошення, `cheap_id` — id того, що показане в
    рядку «найдешевше зараз». Немає id — немає відповідної кнопки: під пульсом
    і звітами їм нічого позначати.

    `cheap_near` — чи те друге оголошення не дешевше, а просто найближче за
    ціною. На дію кнопки це не впливає (позначається все одно воно), але
    впливає на підпис: сказати «найдешевше» там, де показано «найближче»,
    означало б збрехати про те, що саме зараз викреслять.
    """

    mark_id: str | None
    cheap_id: str | None
    cheap_near: bool

    def __new__(cls, text: str, mark_id: str | None = None,
                cheap_id: str | None = None, cheap_near: bool = False) -> "Message":
        obj = super().__new__(cls, text)
        obj.mark_id = mark_id
        obj.cheap_id = cheap_id
        obj.cheap_near = bool(cheap_near)
        return obj


MARK_RU_PREFIX = "ru:"
MARK_BAD_PREFIX = "bad:"

# Причина позначки → (підпис у клавіатурі після кліку, відповідь на натискання).
MARK_REASONS = {
    "ru": ("✓ позначено російським", "Позначено як російське"),
    "bad": ("✓ не враховується", "Більше не враховується"),
}


def mark_keyboard(ad_id: str | None, cheap_id: str | None = None, *,
                  near: bool = False) -> dict[str, Any] | None:
    """Кнопки під сповіщенням. `callback_data` ≤ 64 байти — id влазить.

    Дві кнопки, бо російським може виявитись будь-яке з двох оголошень, і це
    РІЗНІ оголошення. Перша версія вішала одну кнопку на саме оголошення — і
    це було просто неправильно: у випадку, заради якого все й робилось,
    російським було «найдешевше зараз», а кнопка позначила б українське
    видання, про яке прийшло сповіщення.

    Друга кнопка з'являється лише коли рядок про сусіднє оголошення є і вказує
    на ІНШЕ оголошення.

    `near=True` — коли дешевшого немає і показане «найближче за ціною». Кнопка
    та сама й позначає те саме оголошення, змінюється лише підпис: він має
    називати те, що людина бачить у повідомленні.
    """
    rows = []
    other = "Найближче" if near else "Найдешевше"
    for prefix, mine, theirs in (
        (MARK_RU_PREFIX, "🚫 Це оголошення рос.", f"🚫 {other} рос."),
        # Друга причина: «не відкривається / хибне спрацювання / дивне
        # видання, що ламає статистику». Наслідок той самий — оголошення
        # більше не враховується ніде, — але причина інша, і в базі вона
        # зберігається окремо: інакше через місяць не розібрати, чому саме
        # цей запис викинуто.
        (MARK_BAD_PREFIX, "🗑 Не враховувати це", f"🗑 Не враховувати {other.lower()}"),
    ):
        row = []
        if ad_id:
            row.append({"text": mine, "callback_data": f"{prefix}{ad_id}"[:64]})
        if cheap_id and cheap_id != ad_id:
            row.append({"text": theirs, "callback_data": f"{prefix}{cheap_id}"[:64]})
        if row:
            rows.append(row)
    return {"inline_keyboard": rows} if rows else None


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.session = requests.Session()

    def send(self, text: str, *, photo: str | None = None,
             keyboard: dict[str, Any] | None = None) -> bool:
        extra = {"reply_markup": json.dumps(keyboard)} if keyboard else {}
        if photo:
            ok = self._call("sendPhoto", {"photo": photo, "caption": text[:1024],
                                          "parse_mode": "HTML", **extra})
            if ok:
                return True
            log.warning("sendPhoto не вдався, надсилаю текстом")
        return self._call("sendMessage", {
            "text": text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            **extra,
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


HEADLINE = {
    # Чому не однаковий заголовок. «≥1000 грн» шлеться НЕ для того, щоб
    # купити, а щоб бачити, скільки за цю книжку взагалі просять. Якщо таке
    # повідомлення виглядає як знахідка, воно вчить гортати не читаючи — і
    # справжня знахідка поїде туди ж.
    "high": "💸 <b>Дорогий лот</b> (для оцінки ринку)",
    "bundle": "📚 <b>Комплект</b>",
}


def format_event(kind: str, watch_name: str, ad, old_price: float | None = None,
                 verdict: str | None = None, mark: str | None = None,
                 cheapest: dict | None = None) -> str:
    """kind: 'new' | 'drop'

    `verdict` — коментар від analytics («🟢 Брати не думаючи», «🔴 Задорого»…).
    Ставимо його одразу під ціною: саме там на нього дивляться, коли треба
    вирішити за півсекунди, відкривати посилання чи гортати далі.

    `mark` — чому оголошення взагалі приїхало, якщо це не звичайна знахідка
    («high» — дорогий лот для оцінки ринку, «bundle» — комплект).

    `cheapest` — найдешевше живе оголошення на цю ж книжку (з
    `analytics.cheapest_now`), щоб ціну було з чим порівняти одразу.
    """
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
        title_line = HEADLINE.get(mark or "", "🆕 <b>Нове оголошення</b>")
        head = f"{title_line} — {tag}\n💰 <b>{esc(ad.price_text)}</b>"

    if verdict:
        head += f"\n<b>{esc(verdict)}</b>"

    caption = esc(ad.title)
    if getattr(ad, "author", None):
        caption = f"{esc(ad.author)} — {caption}"
    bits = [head, f"\n<a href=\"{esc(ad.url)}\">{caption}</a>"]

    meta = [x for x in (ad.city, ad.condition) if x]
    if ad.negotiable:
        meta.append("торг")
    if meta:
        bits.append("📍 " + esc(" · ".join(meta)))

    line = format_cheapest(cheapest, ad)
    if line:
        bits.append(line)
    return "\n".join(bits)


def cheap_is_near(cheapest: dict | None, ad) -> bool:
    """Сусід не дешевший за наше оголошення, а просто найближчий за ціною.

    Одне місце на всіх, бо відповідь потрібна двічі й мусить збігтися: тут
    вирішується і текст рядка, і підпис кнопки, яка позначає те саме
    оголошення.
    """
    if not cheapest or cheapest.get("price") is None:
        return False
    if str(cheapest.get("id") or "") == str(getattr(ad, "id", "")):
        return False
    mine = getattr(ad, "price", None)
    return mine is not None and float(cheapest["price"]) >= float(mine)


def format_cheapest(cheapest: dict | None, ad) -> str:
    """Рядок про сусіднє оголошення на ту саму книжку.

    Сенс у порівнянні: «300 грн» саме по собі не каже нічого, а «300 грн, а
    поруч лежить за 180» — каже все.

    Коли дешевшого немає, це теж відповідь — і саме та, заради якої варто
    відкривати посилання. Але самого «дешевше немає» замало: без другого числа
    не видно, це перевага в десять гривень чи вдвічі. Тому поруч їде найближче
    за ціною — наступне оголошення на ту саму книжку, яким і міряється, чого
    варта знахідка. Воно ж стоїть під правими кнопками: сусіда теж буває
    потрібно викреслити (російське видання, хибне спрацювання), і доти воно
    псуватиме порівняння в кожному наступному сповіщенні.
    """
    if not cheapest or cheapest.get("price") is None:
        return ""
    price = float(cheapest["price"])
    label = esc(_fmt(price, ad.currency))
    url = cheapest.get("url")
    link = f"<a href=\"{esc(url)}\">{label}</a>" if url else label

    if str(cheapest.get("id") or "") == str(getattr(ad, "id", "")):
        return "🔻 <i>Дешевше на зараз немає</i>"
    if cheap_is_near(cheapest, ad):
        return f"🔻 <i>Дешевше на зараз немає</i>\n🔸 Найближче: {link}"
    return f"🔻 Найдешевше зараз: {link}"


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


def recent_chats() -> list[dict[str, Any]]:
    """Чати, де бот нещодавно бачив повідомлення — щоб знайти свій chat_id.

    Два обмеження самого Telegram, про які варто знати:
    getUpdates пам'ятає лише останню добу, і він мовчить (409), якщо на бота
    навішано webhook. Тому спершу треба щось боту написати.
    """
    token = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не заданий")
    r = requests.get(TG_API.format(token=token, method="getUpdates"),
                     params={"limit": 100}, timeout=30)
    data = r.json() if r.content else {}
    if not data.get("ok"):
        raise RuntimeError(f"{r.status_code} {data.get('description') or r.text[:200]}")

    found: dict[Any, dict[str, Any]] = {}
    for upd in data.get("result", []):
        for key in ("message", "edited_message", "channel_post",
                    "my_chat_member", "callback_query"):
            node = upd.get(key) or {}
            chat = node.get("chat") or (node.get("message") or {}).get("chat") or {}
            cid = chat.get("id")
            if cid is None:
                continue
            name = (chat.get("title") or chat.get("username")
                    or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                    or "—")
            # Статус бота в чаті Telegram дає лише в my_chat_member. Де не дав —
            # пишемо None, а не вигадуємо: «member» там, де ми не знаємо, —
            # це готова відповідь «усе гаразд» на питання, яке ми не ставили.
            status = ((node.get("new_chat_member") or {}).get("status")
                      if key == "my_chat_member" else None)
            found.setdefault(cid, {"id": cid, "type": chat.get("type", "?"),
                                   "title": name, "status": status, "via": key})
    return list(found.values())


def poll_marks(offset: int | None = None) -> tuple[list[dict[str, Any]], int | None]:
    """Зчитує натискання кнопок і повертає (позначки, новий offset).

    Чому це працює без вебхука й без постійного процесу. Скрапер — це разова
    джоба, але Telegram тримає непідтверджені апдейти близько доби, тож
    наступний прогін (їх чотири на годину) спокійно їх забирає. Той самий
    `getUpdates`, яким уже користується `--find-chat`.

    `offset` — до якого update_id уже прочитано. Підтвердження відбувається
    наступним викликом з offset = last_id + 1, тому апдейт, який ми прочитали,
    але не встигли зберегти, Telegram віддасть ще раз. Це навмисно: краще
    обробити двічі (позначка ідемпотентна), ніж загубити.

    ⚠ 409 від Telegram означає, що на бота навішано вебхук — тоді getUpdates
    мовчить. Лікується `deleteWebhook`.
    """
    token = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
    if not token:
        return [], offset
    params: dict[str, Any] = {"timeout": 0, "allowed_updates": '["callback_query"]'}
    if offset is not None:
        params["offset"] = int(offset)
    try:
        r = requests.get(TG_API.format(token=token, method="getUpdates"),
                         params=params, timeout=30)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Не вдалось прочитати натискання: %s", exc)
        return [], offset
    if not data.get("ok"):
        log.warning("getUpdates: %s", str(data)[:200])
        return [], offset

    marks: list[dict[str, Any]] = []
    last: int | None = offset
    for upd in data.get("result") or []:
        last = int(upd.get("update_id", 0)) + 1
        cq = upd.get("callback_query") or {}
        payload = str(cq.get("data") or "")
        prefix = next((p for p in (MARK_RU_PREFIX, MARK_BAD_PREFIX)
                       if payload.startswith(p)), None)
        if prefix is None:
            continue
        msg = cq.get("message") or {}
        marks.append({
            "ad_id": payload[len(prefix):],
            "why": prefix.rstrip(":"),
            "callback_id": cq.get("id"),
            "chat_id": (msg.get("chat") or {}).get("id"),
            "message_id": msg.get("message_id"),
            # Поточна клавіатура приїжджає разом з натисканням — тільки з неї
            # можна дізнатись, які ще кнопки були під повідомленням.
            "markup": msg.get("reply_markup"),
            "data": payload,
        })
    return marks, last


def _keyboard_after_click(mark: dict[str, Any]) -> dict[str, Any]:
    """Та сама клавіатура без натиснутої кнопки, плюс галочка.

    Кнопок під сповіщенням до чотирьох (дві причини × основне і найдешевше), і
    натиснути можуть кілька. Якщо після першого натискання підмінити всю
    клавіатуру підписом, решта кнопок зникне назавжди.

    Галочка бере підпис з причини, яку щойно натиснули: «позначено російським»
    і «не враховується» — різні твердження, і плутати їх у підписі означало б
    брехати про те, що саме зроблено.
    """
    clicked = str(mark.get("data") or "")
    rows = ((mark.get("markup") or {}).get("inline_keyboard") or [])
    # Викидаємо і натиснуту кнопку, і галочку з попереднього натискання —
    # інакше після другого кліку їх стане дві.
    kept = [[b for b in row
             if b.get("callback_data") not in (clicked, "noop")] for row in rows]
    kept = [row for row in kept if row]
    label = MARK_REASONS.get(str(mark.get("why") or "ru"), MARK_REASONS["ru"])[0]
    kept.append([{"text": label, "callback_data": "noop"}])
    return {"inline_keyboard": kept}


def confirm_mark(mark: dict[str, Any], text: str | None = None) -> None:
    """Прибирає «годинник» на кнопці й лишає решту кнопок на місці.

    Без `answerCallbackQuery` Telegram крутить спінер на кнопці до хвилини, і
    виглядає це як зависла кнопка. Ні один зі щаблів не критичний, тож усе в
    try/except: позначку вже збережено, а косметика не сміє валити прогін.
    """
    token = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
    if not token:
        return
    if text is None:
        text = MARK_REASONS.get(str(mark.get("why") or "ru"), MARK_REASONS["ru"])[1]
    try:
        requests.post(TG_API.format(token=token, method="answerCallbackQuery"),
                      data={"callback_query_id": mark.get("callback_id"), "text": text},
                      timeout=15)
    except Exception as exc:  # noqa: BLE001
        log.debug("answerCallbackQuery: %s", exc)
    if not (mark.get("chat_id") and mark.get("message_id")):
        return
    try:
        requests.post(
            TG_API.format(token=token, method="editMessageReplyMarkup"),
            data={"chat_id": mark["chat_id"], "message_id": mark["message_id"],
                  "reply_markup": json.dumps(_keyboard_after_click(mark))},
            timeout=15)
    except Exception as exc:  # noqa: BLE001
        log.debug("editMessageReplyMarkup: %s", exc)


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
    # Повідомлення буває рядком (пульс, звіти) або Message з id оголошення.
    items = [m if isinstance(m, Message) else Message(m) for m in messages]
    overflow = len(items) - MAX_MESSAGES_PER_RUN
    to_send = items[:MAX_MESSAGES_PER_RUN]
    if overflow > 0:
        to_send.append(Message(
            f"… і ще <b>{overflow}</b> збігів цього разу (звузьте фільтри або зменште інтервал)."))

    return _deliver(channels, to_send)


def _deliver(channels: list[Any], to_send: list[Any]) -> int:
    """Саме надсилання. Спільне для `dispatch` і `Sender`, щоб вони не
    розійшлися: двічі виписаний цикл по каналах — це два місця, де можна
    забути перевірити результат `send()`."""
    # Повідомлення буває звичайним рядком (пульс, звіти) — зводимо до Message,
    # щоб нижче можна було читати mark_id/cheap_id без перевірок на кожному кроці.
    to_send = [m if isinstance(m, Message) else Message(m) for m in to_send]
    failed = 0
    for ch in channels:
        if isinstance(ch, Telegram):
            for msg in to_send:
                # `keyboard` передаємо лише коли кнопка справді є: інакше
                # будь-який інший канал із сигнатурою send(text, *, photo)
                # зламався б на незнайомому аргументі.
                kb = mark_keyboard(msg.mark_id, msg.cheap_id, near=msg.cheap_near)
                extra = {"keyboard": kb} if kb else {}
                if not ch.send(str(msg), **extra):
                    failed += 1
                time.sleep(SEND_PAUSE)
        elif isinstance(ch, Email):
            body = "<hr>".join(m.replace("\n", "<br>") for m in to_send)
            if not ch.send(f"OLX: {len(messages)} нових подій", body):
                failed += 1
    return failed

class Sender:
    """Надсилання ПО ХОДУ прогону, зі спільним лімітом на весь прогін.

    Навіщо. `run()` збирав усі повідомлення в список і віддавав їх одним
    махом у самому кінці, тож знахідка в першому watch'і лежала, доки
    відпрацюють решта тридцять і звіт про зняті. Заміряно на живому випадку
    2026-10-03: оголошення знайдено о 19:49, надіслано о 19:51:40. Дві з
    половиною хвилини чистої затримки.

    ⚠ Тривалість прогону від цього НЕ росте: надсилань стільки ж, і пауза
    1.2 с між ними та сама — вони просто відбуваються раніше, впереміж із
    запитами, а не купою в кінці.

    Ліміт `MAX_MESSAGES_PER_RUN` мусить лишитись НА ПРОГІН, а не на watch:
    інакше запобіжник від флуду перетворився б на 25 × 31 повідомлення.
    """

    def __init__(self, channels: list[Any], limit: int = MAX_MESSAGES_PER_RUN) -> None:
        self.channels = channels
        self.left = int(limit)
        self.sent = 0
        self.failed = 0
        self.skipped = 0

    def send(self, messages: list[str]) -> int:
        """Шле пачку, повертає кількість недоставлених."""
        if not messages or not self.channels:
            return 0
        allowed = messages[:max(0, self.left)]
        self.skipped += len(messages) - len(allowed)
        self.left -= len(allowed)
        if not allowed:
            return 0
        failed = _deliver(self.channels, allowed)
        self.sent += len(allowed) - failed
        self.failed += failed
        return failed

    def finish(self) -> None:
        if self.skipped:
            self.send_overflow_note()

    def send_overflow_note(self) -> None:
        note = Message(f"… і ще <b>{self.skipped}</b> збігів цього разу "
                       f"(звузьте фільтри або зменште інтервал).")
        self.left = 1
        self.skipped = 0
        self.send([note])

