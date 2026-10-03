"""Надсилання по ходу прогону (2026-10-03).

Привід: оголошення знайдено о 19:49, надіслано о 19:51:40 — дві з половиною
хвилини чистої затримки, бо `run()` віддавав усі повідомлення одним списком
аж наприкінці.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import notify  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Пауза між надсиланнями — властивість Telegram, не логіки."""
    monkeypatch.setattr(notify, "SEND_PAUSE", 0)


class FakeTelegram(notify.Telegram):
    def __init__(self, fail_on=()):
        self.sent, self.fail_on = [], set(fail_on)

    def send(self, text, *, photo=None, keyboard=None):
        self.sent.append(str(text))
        return str(text) not in self.fail_on


def test_the_run_wide_limit_is_not_a_per_batch_limit():
    """🔑 Ліміт `MAX_MESSAGES_PER_RUN` — запобіжник від флуду. Якби кожна
    пачка рахувала його заново, тридцять один watch дав би 31 × 25."""
    ch = FakeTelegram()
    s = notify.Sender([ch], limit=3)
    s.send(["a", "b"])
    s.send(["c", "d", "e"])
    assert ch.sent == ["a", "b", "c"]
    assert s.sent == 3 and s.skipped == 2


def test_the_overflow_note_goes_out_once_at_the_end():
    ch = FakeTelegram()
    s = notify.Sender([ch], limit=2)
    s.send(["a", "b", "c", "d"])
    s.finish()
    assert ch.sent[:2] == ["a", "b"]
    assert "ще <b>2</b>" in ch.sent[2]
    assert len(ch.sent) == 3, "нотатка одна, а не на кожну пачку"


def test_failures_accumulate_across_batches():
    """Від цього залежить, чи збережеться стан: один недоставлений лист у
    першому watch'і мусить дожити до рішення в кінці прогону."""
    ch = FakeTelegram(fail_on={"b"})
    s = notify.Sender([ch])
    assert s.send(["a", "b"]) == 1
    assert s.send(["c"]) == 0
    assert s.failed == 1 and s.sent == 2


def test_a_plain_string_is_accepted_like_a_message():
    """Пульс і звіти — звичайні рядки, не Message. Якщо Sender на них
    спіткнеться, прогін впаде рівно там, де мав би поскаржитись у Telegram."""
    ch = FakeTelegram()
    notify.Sender([ch]).send(["звичайний рядок"])
    assert ch.sent == ["звичайний рядок"]


def test_no_channels_is_not_a_failure():
    s = notify.Sender([])
    assert s.send(["a"]) == 0 and s.failed == 0
