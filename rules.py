"""Що робити з оголошенням: викинути, зберегти мовчки, чи ще й написати в Telegram.

Навіщо окремий модуль. До 2026-09-22 рішення було одне: `max_price` вирішував
одночасно, чи слати повідомлення і чи взагалі пам'ятати оголошення. Через це
кожне послаблення порогу заради статистики оберталось спамом у Telegram, а
кожне звуження заради тиші вбивало вибірку, на якій тримається аналітика.

Тепер результатів три:

    skip    — не зберігати взагалі (сміття, чуже видання, не та книжка);
    store   — зберегти в базу для статистики, але промовчати;
    notify  — зберегти і написати.

І окремо від них — прапорець `analytics`: комплект варто показати очима, але
рахувати по ньому медіану немає сенсу, бо в кожному комплекті свій набір книг.

Порядок перевірок навмисний: спершу все, що взагалі не є потрібною книжкою
(інакше сміття потрапить у статистику й зсуне медіану), потім комплекти,
і лише наприкінці — ціна.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

# Літери, яких в українській абетці немає. Найдешевша ознака російськомовного
# оголошення, але НЕ достатня: «Гарри Поттер и узник Азкабана» не містить
# жодної з них. Тому нижче є ще два рівні.
RUSSIAN_LETTERS = set("ыъэё")

# Слова, які в українському тексті трапитись не можуть. «и» окремим словом —
# найсильніша з них: українською це «і» або «й», і саме вона ловить
# «Гарри Поттер И узник Азкабана», де жодної чужої літери немає.
RUSSIAN_WORDS = [
    "и", "или", "новая", "новый", "состояние", "страниц", "переплет",
    "издательство", "издание", "твердый", "мягкий", "отличном", "хорошем",
]

# Слова, які досить знайти підрядком (їх український відповідник пишеться
# інакше, тож хибного спрацювання бути не може).
RUSSIAN_SUBSTRINGS = ["издательств", "переплет", "состояние", "страниц"]

# Видавництва, які видають російською. Шукається і в заголовку, і в описі.
RUSSIAN_PUBLISHERS = ["росмен", "росмэн", "rosman", "махаон", "эксмо", "аст"]


@dataclass(frozen=True)
class Decision:
    action: str            # "skip" | "store" | "notify"
    analytics: bool = True  # чи брати оголошення в статистику цін
    reason: str = ""        # людською мовою, для логів
    tag: str | None = None  # "high" — сповіщення про дорогий лот («цінність»)

    @property
    def store(self) -> bool:
        return self.action in ("store", "notify")

    @property
    def notify(self) -> bool:
        return self.action == "notify"


def _opt(entry: dict[str, Any] | None, watch: dict[str, Any], key: str,
         book_key: str | None = None, default: Any = None) -> Any:
    """Значення з каталогу books: (якщо є), інакше з watch'а, інакше default."""
    if entry is not None:
        v = entry.get(book_key or key)
        if v is not None:
            return v
    v = watch.get(key)
    return default if v is None else v


def has_russian_letters(text: str) -> bool:
    return any(ch in RUSSIAN_LETTERS for ch in (text or "").casefold())


def is_russian_text(text: str, *, words: Iterable[str] = (),
                    substrings: Iterable[str] = ()) -> bool:
    """Три незалежні ознаки російського тексту, бо кожна окремо дірява.

    1. Чужі літери (ы ъ э ё) — точно, але їх може не бути зовсім.
    2. Ціле слово зі списку — ловить «Гарри Поттер И узник Азкабана».
    3. Підрядок («издательств») — ловить форми, які не збігаються зі словником.

    Свідомо агресивніше за фільтр заголовка: тут хибне спрацювання коштує
    одного пропущеного оголошення, а пропуск — засміченої статистики.
    """
    t = (text or "").casefold()
    if any(ch in RUSSIAN_LETTERS for ch in t):
        return True
    for w in (substrings or RUSSIAN_SUBSTRINGS):
        if w and w in t:
            return True
    tokens = {tok for tok in re.split(r"[^\w']+", t) if tok}
    return bool(tokens & {w.casefold() for w in (words or RUSSIAN_WORDS) if w})


def letter_mix(text: str) -> tuple[int, float]:
    """(скільки літер, яка частка з них латиниця).

    Рахуємо частку, а не наявність: у половині українських оголошень стоїть
    англійська назва поруч із українською («Black House Stephen King Чорний
    дім Стівен Кінг»), і будь-яка перевірка «є латиниця» викинула б їх усі.
    """
    lat = cyr = 0
    for ch in (text or ""):
        if "a" <= ch.lower() <= "z":
            lat += 1
        elif "Ѐ" <= ch <= "ӿ":
            cyr += 1
    total = lat + cyr
    return total, (lat / total if total else 0.0)


def is_english_text(text: str, *, min_letters: int = 40, share: float = 0.75) -> bool:
    """Чи текст англомовний.

    Поріг високий і мінімальна довжина велика навмисне: короткий заголовок з
    двох англійських слів — це звичайна українська книгарня, а не англомовне
    видання. А от опис на сорок латинських літер поспіль — уже видання.
    """
    total, lat = letter_mix(text)
    return total >= min_letters and lat >= share


def needs_description(watch: dict[str, Any]) -> bool:
    """Чи має сенс витрачати зайвий запит на опис цього оголошення.

    Опис коштує +1 HTTP-запит, тож тягнемо його лише там, де від нього справді
    залежить рішення: комплекти в Кідрука і видавництво/мова в Гаррі Поттері.
    """
    return bool(watch.get("needs_description"))


def is_bundle_text(text: str, *, include: Iterable[str] = (),
                   bundle_keywords: Iterable[str] = (),
                   min_repeats: int = 2) -> bool:
    """Чи схоже, що в оголошенні кілька книжок.

    Та сама логіка, що в `olx.is_bundle`, але по будь-якому тексту, а не лише
    по заголовку: у Кідрука комплект часто видно тільки з опису («продам усі
    п'ять книг однією посилкою»), а назва каже просто «Макс Кідрук».
    """
    t = (text or "").casefold()
    words = list(bundle_keywords) or [
        "комплект", "набір", "набор", "збірник", "зібрання", "усі частини",
        "всі частини", "цикл", "серія книг", "семитомник", "трилогія", "томи",
        "одним лотом", "дві книги", "три книги", "чотири книги", "п'ять книг",
        "5 книг", "4 книги", "3 книги", "2 книги",
    ]
    if any(w.casefold() in t for w in words if w):
        return True
    hits = sum(t.count(w.casefold()) for w in include if w)
    return hits >= min_repeats


def decide(*, title: str, price: float | None, watch: dict[str, Any],
           entry: dict[str, Any] | None = None, description: str | None = None,
           include: Iterable[str] = ()) -> Decision:
    """Головна функція модуля. `watch` — опції watch'а (вже злиті з defaults)."""

    hay = f"{title or ''}\n{description or ''}"

    # 1. Позиція каталогу може бути помічена як «нам це взагалі не потрібно»
    #    (сувеніри, альманахи, ілюстровані видання Гаррі Поттера).
    if entry is not None and entry.get("skip"):
        return Decision("skip", reason=f"каталог: {entry.get('name')} позначено skip")

    # 2. Слова-вбивці. На відміну від exclude_keywords вони дивляться і в опис,
    #    бо видавництво в заголовку не пишуть майже ніколи.
    for w in list(_opt(entry, watch, "drop_keywords", default=[]) or []):
        if w and w.casefold() in hay.casefold():
            return Decision("skip", reason=f"стоп-слово «{w}»")

    # 3. Російськомовне оголошення. Дві незалежні ознаки, бо кожна окремо
    #    дірява: заголовок «Гаррі Поттер, вид. Росмен» українських літер не
    #    порушує, а «Гарри Поттер и узник Азкабана» не згадує видавництва.
    if _opt(entry, watch, "drop_russian", default=False):
        if is_russian_text(title):
            return Decision("skip", reason="російськомовний заголовок")
        if description and is_russian_text(description):
            return Decision("skip", reason="російськомовний опис")
        for pub in list(_opt(entry, watch, "russian_publishers",
                             default=RUSSIAN_PUBLISHERS) or []):
            if pub and pub.casefold() in hay.casefold():
                return Decision("skip", reason=f"російське видавництво «{pub}»")

    # 3б. Англомовне видання. На відміну від російського воно не потрібне
    #     навіть для історії: ціни на англійські видання живуть своїм життям
    #     і українську медіану лише зсувають.
    if _opt(entry, watch, "drop_english", default=False):
        if description and is_english_text(description):
            return Decision("skip", reason="англомовний опис")
        # Без опису дивимось на заголовок, але значно суворіше: двомовний
        # заголовок («Black House Stephen King Чорний дім») — це норма.
        if not description and is_english_text(title, min_letters=20, share=0.95):
            return Decision("skip", reason="англомовний заголовок")

    # 3в. Російське — «мовчки»: зберігаємо заради історії, але не пишемо.
    #     Окремо від drop_russian, бо для Кінга й Ріггза російське видання —
    #     це факт ринку, а для Гаррі Поттера просто сміття.
    silent = False
    if _opt(entry, watch, "silent_russian", default=False):
        if is_russian_text(title) or (description and is_russian_text(description)):
            silent = True
        elif any(p.casefold() in hay.casefold()
                 for p in (_opt(entry, watch, "russian_publishers",
                                default=RUSSIAN_PUBLISHERS) or []) if p):
            silent = True

    # 4. Комплект. Дивимось і заголовок, і опис.
    bundle = is_bundle_text(hay, include=include,
                            bundle_keywords=watch.get("bundle_keywords") or ())
    bundle_analytics = bool(_opt(entry, watch, "bundle_analytics", default=True))
    analytics = bundle_analytics if bundle else True

    def out(d: Decision) -> Decision:
        """Останній фільтр: російське оголошення нікуди не пише, лише лягає в базу."""
        if silent and d.action == "notify":
            return Decision("store", analytics=d.analytics,
                            reason=f"{d.reason}; але російською — мовчимо")
        return d

    if bundle and _opt(entry, watch, "bundle_notify", default=False):
        # Комплект шлемо попри будь-які пороги: за набір книг просять зовсім
        # інші гроші, і жодна межа для одиночного тому тут не працює.
        return out(Decision("notify", analytics=bundle_analytics,
                            reason="комплект", tag="bundle"))

    # 5. Ціна. Пороги беруться з каталогу, якщо він їх задає, інакше з watch'а.
    #    Комплект може мати власні межі: за два томи «Протистояння» просять
    #    удвічі більше, ніж за один, і одна межа на обидва випадки не працює.
    if bundle:
        b_notify = _opt(entry, watch, "notify_max_price_bundle",
                        book_key="notify_max_bundle")
        b_store = _opt(entry, watch, "max_price_bundle", book_key="store_max_bundle")
        if b_notify is not None or b_store is not None:
            watch = {**watch}
            entry = {**(entry or {})}
            if b_store is not None:
                watch["max_price"] = b_store
                entry.pop("store_max", None)
            if b_notify is not None:
                watch["notify_max_price"] = b_notify
                entry["notify_max"] = b_notify
            elif b_store is not None:
                watch["notify_max_price"] = b_store
                entry["notify_max"] = b_store

    notify_max = _opt(entry, watch, "notify_max_price", book_key="notify_max")
    notify_high = _opt(entry, watch, "notify_min_price_high", book_key="notify_min_high")
    store_max = _opt(entry, watch, "max_price", book_key="store_max")

    # Сумісність зі старими watch'ами: поки жодного порогу Telegram не задано,
    # поводимось як до 2026-09-22 — «зберегли, значить і написали». Без цього
    # рядка десяток налаштованих watch'ів (Кінг, Ріггз, Ґалбрейт…) замовк би
    # цілком, і виглядало б це як зламаний скрапер, а не як зміна конфігу.
    if notify_max is None and notify_high is None:
        notify_max = store_max

    if price is None:
        # Обмін / «договірна». Статистиці така ціна нічого не дає, але
        # оголошення варто пам'ятати, щоб не слати його повторно.
        if watch.get("allow_no_price"):
            return out(Decision("notify", analytics=False, reason="без ціни"))
        return Decision("store", analytics=False, reason="без ціни")

    if notify_max is not None and price <= float(notify_max):
        return out(Decision("notify", analytics=analytics,
                            reason=f"ціна ≤ {float(notify_max):g}"))

    if notify_high is not None and price >= float(notify_high):
        # Не «купувати», а «подивитись, скільки за це просять». У Telegram
        # такі йдуть з іншим значком, щоб не плутати з вигідною знахідкою.
        return out(Decision("notify", analytics=analytics,
                            reason=f"ціна ≥ {float(notify_high):g} (маркер цінності)",
                            tag="high"))

    if store_max is None or price <= float(store_max):
        return Decision("store", analytics=analytics,
                        reason="поза межею Telegram, лишаємо для статистики")

    # Дорожче за межу статистики — але НЕ забуваємо. Падіння ціни відстежується
    # для всіх оголошень пошуку, і саме заради випадку «висіло за 1990, впало
    # до 500» воно й існує: якби ми таке оголошення не пам'ятали, порівнювати
    # згодом не було б із чим. У медіану воно не йде — `analytics=False`.
    return Decision("store", analytics=False,
                    reason=f"ціна > {float(store_max):g}: пам'ятаємо лише заради падіння ціни")
