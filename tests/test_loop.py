"""`--loop`: один процес робить прохід за проходом замість нового раннера.

Чому це взагалі є: холодний старт (черга GitHub + checkout + `pip install`)
коштує 1-1.5 хв на кожен запуск, а сам прохід — ~40 с. Заміри в
`claude/notification-latency.md`.

Тут перевіряється не корисна робота проходу (її тестує `test_watcher.py`), а
рівно поведінка обгортки — бо ціна помилки саме в ній: цикл, який тихо помер
посеред години, означає годину без сповіщень, і нічого в логах про це не скаже.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import main


def _args(**over):
    base = dict(config=Path("watches.yaml"), reset=False, loop=True,
                loop_seconds=20, deadline_minutes=0, max_fails=5,
                alert_after=0, dry_run=False)
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def alarms(monkeypatch):
    """Перехоплює тривоги замість того, щоб слати їх у Telegram."""
    sent = []
    monkeypatch.setattr(main, "_alarm", lambda a, c, text: sent.append(text))
    return sent


@pytest.fixture
def no_sleep(monkeypatch):
    """Час у тестах не йде: `time.sleep` нічого не робить.

    `run_loop` рахує дедлайн по `time.monotonic()`, тож без фейкового годинника
    тест або висів би, або вимірював реальні хвилини.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(main.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(main.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return clock


def test_runs_many_passes_within_deadline(monkeypatch, no_sleep):
    calls = []
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: calls.append(1) or 0)

    # 5 хв дедлайну, прохід раз на 60 с, сам прохід «миттєвий».
    rc = main.run_loop(_args(loop_seconds=60, deadline_minutes=5), {}, object())

    assert rc == 0
    assert len(calls) == 5, "мало б вийти п'ять проходів, а не один"


def test_slow_pass_does_not_overrun_deadline(monkeypatch, no_sleep):
    """Прохід, що з'їдає пів періоду, не має стартувати за межею дедлайну.

    Саме це рятує від того, щоб GitHub убив джоб по `timeout-minutes` посеред
    запису стану: виходимо самі, заздалегідь.
    """
    def slow(a, c, s):
        no_sleep["t"] += 40        # прохід триває 40 с
        return 0

    monkeypatch.setattr(main, "one_pass", slow)
    rc = main.run_loop(_args(loop_seconds=60, deadline_minutes=3), {}, object())

    assert rc == 0
    assert no_sleep["t"] <= 3 * 60, "цикл виліз за дедлайн"


def test_exception_in_pass_does_not_kill_loop(monkeypatch, no_sleep):
    seen = []

    def flaky(a, c, s):
        seen.append(1)
        if len(seen) == 2:
            raise ConnectionError("OLX прилягла")
        return 0

    monkeypatch.setattr(main, "one_pass", flaky)
    rc = main.run_loop(_args(loop_seconds=60, deadline_minutes=5), {}, object())

    assert rc == 0
    assert len(seen) == 5, "після винятку цикл мусив іти далі"


def test_exits_after_max_fails_in_a_row(monkeypatch, no_sleep):
    """Три невдачі поспіль — виходимо з кодом 1, щоб піднявся свіжий раннер.

    Зламану сесію curl_cffi або мертвий конект до Mongo наступна спроба в тому
    САМОМУ процесі не лікує; новий процес — лікує.
    """
    calls = []
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: calls.append(1) or 1)

    rc = main.run_loop(_args(loop_seconds=60, deadline_minutes=60, max_fails=3),
                       {}, object())

    assert rc == 1
    assert len(calls) == 3, "вийшли не рівно на третій невдачі"


def test_fail_counter_resets_on_success(monkeypatch, no_sleep):
    """Невдачі впереміш з успіхами — не привід виходити."""
    seq = [1, 0, 1, 0, 1, 0]
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: seq.pop(0) if seq else 0)

    rc = main.run_loop(_args(loop_seconds=60, deadline_minutes=10, max_fails=2),
                       {}, object())

    assert rc == 0
    assert not seq, "цикл обірвався раніше, ніж скінчилась послідовність"


def test_reset_applies_only_to_first_pass(monkeypatch, no_sleep):
    """Інакше цикл щохвилини робив би seed — і мовчав би назавжди."""
    seen = []
    monkeypatch.setattr(main, "one_pass",
                        lambda a, c, s: seen.append(a.reset) or 0)

    main.run_loop(_args(loop_seconds=60, deadline_minutes=4, reset=True), {}, object())

    assert seen == [True, False, False, False]


def test_config_reloaded_when_file_changes(monkeypatch, no_sleep, tmp_path):
    """Правка `watches.yaml` має доїхати без перезапуску процесу."""
    cfg_path = tmp_path / "watches.yaml"
    cfg_path.write_text("defaults: {}\nwatches: []\n", encoding="utf-8")

    seen = []
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: seen.append(c) or 0)
    monkeypatch.setattr(main, "load_config", lambda p: {"tag": "новий"})

    def bump(path):
        # Другий прохід бачить інший mtime, решта — той самий.
        return 1.0 if len(seen) < 2 else 2.0

    monkeypatch.setattr(main, "_cfg_mtime", bump)
    main.run_loop(_args(loop_seconds=60, deadline_minutes=4, config=cfg_path),
                  {"tag": "старий"}, object())

    assert [c.get("tag") for c in seen] == ["старий", "старий", "новий", "новий"]


def test_broken_config_keeps_the_old_one(monkeypatch, no_sleep, tmp_path):
    """Зламаний YAML не має зупиняти сповіщення — доживаємо на попередньому."""
    seen = []
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: seen.append(c) or 0)
    monkeypatch.setattr(main, "load_config",
                        lambda p: (_ for _ in ()).throw(ValueError("битий yaml")))
    flips = iter([1.0, 2.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(main, "_cfg_mtime", lambda p: next(flips, 2.0))

    rc = main.run_loop(_args(loop_seconds=60, deadline_minutes=3), {"tag": "старий"},
                       object())

    assert rc == 0
    assert all(c.get("tag") == "старий" for c in seen)


def test_period_has_a_floor(monkeypatch, no_sleep):
    """`--loop-seconds 1` не має перетворити скрапер на ddos OLX."""
    calls = []
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: calls.append(1) or 0)

    main.run_loop(_args(loop_seconds=1, deadline_minutes=2), {}, object())

    # 2 хв при підлозі 20 с — шість проходів, а не сто двадцять.
    assert len(calls) == 6


# --- Тривога в Telegram ----------------------------------------------------
#
# Сенс цих тестів один: поломка циклу має бути ЧУТНОЮ, але рівно один раз.
# Прохід іде щохвилини, тож «писати на кожну невдачу» означало б тридцять
# однакових повідомлень за годину аварії OLX — після такого сповіщення
# перестають читати, і наступну справжню знахідку теж проґавлять.

def test_alarm_sent_once_per_outage(monkeypatch, no_sleep, alarms):
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: 1)

    main.run_loop(_args(loop_seconds=60, deadline_minutes=10, max_fails=0,
                        alert_after=2), {}, object())

    assert len(alarms) == 1, "на кожну невдачу слати не можна"
    assert "не працює" in alarms[0]


def test_no_alarm_on_a_single_blip(monkeypatch, no_sleep, alarms):
    """Одна невдача поспіль — ще не аварія, а мережа моргнула."""
    seq = [1, 0, 1, 0, 1, 0, 0, 0, 0, 0]
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: seq.pop(0) if seq else 0)

    main.run_loop(_args(loop_seconds=60, deadline_minutes=10, max_fails=0,
                        alert_after=2), {}, object())

    assert alarms == []


def test_recovery_message_after_alarm(monkeypatch, no_sleep, alarms):
    """Відбій обов'язковий: інакше перше повідомлення лишається без відповіді."""
    seq = [1, 1, 1, 0]
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: seq.pop(0) if seq else 0)

    main.run_loop(_args(loop_seconds=60, deadline_minutes=8, max_fails=0,
                        alert_after=2), {}, object())

    assert len(alarms) == 2
    assert "не працює" in alarms[0]
    assert "знову працює" in alarms[1]
    assert "Пропущено проходів: 3" in alarms[1]


def test_second_outage_alarms_again(monkeypatch, no_sleep, alarms):
    """Після відбою прапорець скидається — друга аварія теж має прозвучати."""
    seq = [1, 1, 0, 1, 1, 0]
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: seq.pop(0) if seq else 0)

    main.run_loop(_args(loop_seconds=60, deadline_minutes=10, max_fails=0,
                        alert_after=2), {}, object())

    assert [("не працює" in m, "знову працює" in m) for m in alarms] == \
        [(True, False), (False, True), (True, False), (False, True)]


def test_fatal_alarm_on_exit(monkeypatch, no_sleep, alarms):
    """Вихід по max-fails — окреме повідомлення, бо це вже не «пробую далі»."""
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: 1)

    rc = main.run_loop(_args(loop_seconds=60, deadline_minutes=60, max_fails=3,
                             alert_after=2), {}, object())

    assert rc == 1
    assert len(alarms) == 2
    assert "зупинився" in alarms[-1]


def test_exception_text_reaches_telegram(monkeypatch, no_sleep, alarms):
    def boom(a, c, s):
        raise ConnectionError("DataDome на сторінці пошуку")

    monkeypatch.setattr(main, "one_pass", boom)
    main.run_loop(_args(loop_seconds=60, deadline_minutes=10, max_fails=0,
                        alert_after=2), {}, object())

    assert "DataDome" in alarms[0], "без причини повідомлення марне"


def test_alerts_can_be_switched_off(monkeypatch, no_sleep, alarms):
    monkeypatch.setattr(main, "one_pass", lambda a, c, s: 1)
    main.run_loop(_args(loop_seconds=60, deadline_minutes=5, max_fails=0,
                        alert_after=0), {}, object())
    assert alarms == []


def test_alarm_never_raises(monkeypatch, caplog):
    """Telegram лежить — цикл це переживає.

    Найгірший можливий баг тут: канал сповіщень падає, виняток летить із
    `_alarm`, і скрапер зупиняється САМЕ ЧЕРЕЗ механізм, який мав повідомити
    про зупинку.
    """
    monkeypatch.setattr(main.notify, "build_channels",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("401")))
    main._alarm(_args(), {}, "текст")   # не має кинути нічого


def test_dry_run_does_not_send(monkeypatch):
    calls = []
    monkeypatch.setattr(main.notify, "build_channels", lambda cfg: calls.append(1) or [])
    main._alarm(_args(dry_run=True), {}, "текст")
    assert calls == []
