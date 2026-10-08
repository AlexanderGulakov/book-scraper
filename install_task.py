#!/usr/bin/env python3
"""Реєструє завдання Планувальника «Bookflea watcher» і перевіряє його.

Навіщо Python, а не .bat. Уся ця логіка спершу жила в `bookflea-install.bat`,
і саме там 2026-09-21 знайшлась пастка, через яку стеження мовчало: cmd.exe
перечитує .bat з байтового зміщення після кожної команди, тож `chcp 65001`
посеред файлу з кирилицею зсуває ці зміщення, парсер ріже рядки `rem` навпіл
і намагається виконати їхні хвости. На одній машині збіглося, на іншій ні.

Висновок: .bat лишаються голим ASCII і роблять мінімум, а все, де потрібні
текст, логіка й умови, робить Python. Заодно це можна покрити тестами.

Запуск: подвійний клік по bookflea-install.bat (або `py -3 install_task.py`).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = "Bookflea watcher"
RUNNER = HERE / "run-bookflea.bat"
TEMPLATE = HERE / "bookflea-task.xml"
LOG = HERE / "logs" / "bookflea.log"

# Прогін Букфлі — 11 запитів із паузами, тобто близько хвилини.
WAIT_SECONDS = 150
POLL_SECONDS = 5


def say(text: str = "") -> None:
    print(text, flush=True)


def log_size() -> int:
    try:
        return LOG.stat().st_size
    except OSError:
        return -1


def tail(lines: int = 15) -> str:
    try:
        content = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(лог недоступний)"
    return "\n".join("    " + ln for ln in content[-lines:])


def run_directly() -> bool:
    """Крок 1: прогін без Планувальника — чи живий сам скрипт."""
    say("[1/3] Пробую прогін напряму, без Планувальника...")
    before = log_size()
    # .bat запускаємо явно через cmd /c: CreateProcess сам по собі .bat
    # не виконує, а покладатись на це в Python 3.13+ уже не можна.
    proc = subprocess.run(["cmd", "/c", str(RUNNER)], cwd=HERE)

    if log_size() < 0:
        say(f"    [X] Лог не з'явився: {LOG}")
        say("        Отже не запустився навіть Python. Перевірте: py -3 --version")
        return False
    if log_size() == before:
        say("    [X] Лог не виріс — скрипт нічого не написав.")
        return False
    if proc.returncode != 0:
        say(f"    [!] Скрипт завершився з кодом {proc.returncode}. Кінець логу:")
        say(tail())
        say("    Спершу це — Планувальник тут ні до чого.")
        return False

    say("    [OK] Скрипт відпрацював, лог пише.")
    return True


def create_task() -> bool:
    """Крок 2: реєстрація завдання з XML-шаблону.

    Саме з XML, а не `schtasks /Create /SC MINUTE`: та команда не дає задати
    Settings, а нам потрібні робоча тека, ліміт часу й поведінка на батареї.
    """
    say("[2/3] Створюю завдання з bookflea-task.xml...")
    if not TEMPLATE.exists():
        say(f"    [X] Немає шаблона {TEMPLATE.name} поруч.")
        return False

    xml = TEMPLATE.read_text(encoding="utf-8").replace("__DIR__", str(HERE))
    # Шаблон працює під S4U («незалежно від входу в систему»), а для цього
    # потрібен явний UserId — інакше schtasks бере принципала з XML як є і
    # відмовляє. Беремо поточного користувача: DOMAIN\user, а для машини без
    # домену USERDOMAIN — це просто ім'я комп'ютера, і це теж правильно.
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    xml = xml.replace("__USER__", f"{domain}\\{user}" if domain else user)
    # schtasks /XML найнадійніше читає UTF-16LE з BOM — саме так Планувальник
    # і сам експортує завдання. Пролог мусить збігатися з реальним кодуванням
    # файлу, інакше парсер спіткнеться об розбіжність.
    xml = xml.replace('encoding="UTF-8"', 'encoding="UTF-16"', 1)
    tmp = HERE / "logs" / "_task.xml"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_text(xml, encoding="utf-16")
    try:
        return try_register(tmp, xml)
    finally:
        tmp.unlink(missing_ok=True)


def try_register(tmp: Path, xml: str) -> bool:
    """Спершу без вікна (S4U), а якщо прав бракує — по-старому."""
    done = register(tmp)
    if done.returncode != 0:
        # Зареєструвати завдання, яке працює БЕЗ інтерактивного сеансу (S4U),
        # Windows дозволяє лише з підвищеними правами — звідси «Access is
        # denied». Сам прогін потім іде зі звичайними правами, адміністратор
        # потрібен рівно один раз, на момент реєстрації.
        say(f"    [!] Не вдалось зареєструвати без вікна: "
            f"{(done.stderr or done.stdout).strip()}")
        say("        Так буває без прав адміністратора.")
        say("        Варіант А: закрийте це вікно й запустіть"
            " bookflea-install.bat правою кнопкою -> 'Запуск від імені"
            " адміністратора'.")
        say("        Варіант Б (зараз): лишити як було, з видимим вікном.")
        say("        Роблю Б, щоб ви не лишились без завдання...")
        tmp.write_text(
            xml.replace("<LogonType>S4U</LogonType>",
                        "<LogonType>InteractiveToken</LogonType>"),
            encoding="utf-16",
        )
        done = register(tmp)
        if done.returncode != 0:
            say(f"    [X] schtasks відмовив і тут: "
                f"{(done.stderr or done.stdout).strip()}")
            return False
        say("    [OK] Створено у старому режимі — вікно буде видимим.")
        return True

    say("    [OK] Створено. Без вікна; на батареї не запускатиметься — економія.")
    return True


def register(tmp: Path) -> subprocess.CompletedProcess:
    """Віддає готовий XML у schtasks. Окремо — щоб можна було повторити."""
    try:
        return subprocess.run(
            ["schtasks", "/Create", "/TN", TASK, "/XML", str(tmp), "/F"],
            capture_output=True, text=True, errors="replace",
        )
    except OSError as err:  # schtasks немає в PATH — буває на урізаних збірках
        return subprocess.CompletedProcess([], 1, "", str(err))


def verify_via_scheduler() -> bool:
    """Крок 3: чи вміє Планувальник ДІЙСНО запустити скрипт.

    Дивимось не на код повернення schtasks — він локалізований і залежить від
    мови системи, — а на те, чи виріс лог. Розмір файлу однаковий будь-якою
    мовою.
    """
    say("[3/3] Прошу Планувальник запустити завдання...")
    before = log_size()
    subprocess.run(["schtasks", "/Run", "/TN", TASK], capture_output=True)

    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        if log_size() > before:
            say("    [OK] Лог виріс — завдання дійсно запускає скрипт.")
            return True
    say("    [X] Лог не виріс за %d с." % WAIT_SECONDS)
    return False


def schedule_minutes() -> str:
    """Інтервал беремо з XML, щоб напис на екрані не розходився з правдою."""
    try:
        found = re.search(r"<Interval>PT(\d+)M</Interval>",
                          TEMPLATE.read_text(encoding="utf-8"))
    except OSError:
        return "?"
    return found.group(1) if found else "?"


def main() -> int:
    say("=" * 60)
    say(f" Завдання : {TASK}")
    say(f" Запускає : {RUNNER}")
    say(f" Розклад  : кожні {schedule_minutes()} хв")
    say("=" * 60)
    say()

    if not RUNNER.exists():
        say(f"[X] Не знайшов {RUNNER.name}. Запускайте з теки проєкту.")
        return 1

    if not run_directly():
        return 1
    say()
    if not create_task():
        return 1
    say()
    if not verify_via_scheduler():
        say()
        say("    Скрипт при цьому працює — крок 1 це щойно довів.")
        say("    Значить, справа в самому завданні. Подивіться:")
        say(f'        schtasks /Query /TN "{TASK}" /XML')
        return 1

    say()
    say(f"[OK] Готово. Далі — кожні {schedule_minutes()} хв,"
        " поки ноутбук у розетці.")
    say(f"     Лог: {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
