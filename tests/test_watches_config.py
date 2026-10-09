"""Перевірки самого `watches.yaml` — того, що ламається тихо.

Конфіг тут великий (47 watch'ів, каталог книг, якорі), і помилка в ньому не
падає: скрапер просто мовчить або рахує не ту книжку. Саме цей клас і коштував
найдорожче — див. claude/silent-misses.md і claude/strike-coverage.md.
"""

from pathlib import Path

import pytest
import yaml

import olx


@pytest.fixture(scope="module")
def cfg():
    root = Path(__file__).resolve().parent.parent
    return yaml.safe_load((root / "watches.yaml").read_text(encoding="utf-8"))


def _olx_watches(cfg):
    return [w for w in cfg["watches"]
            if str(w.get("source", "olx")).lower() == "olx"]


def test_назви_watchів_унікальні(cfg):
    """Ключ стану — це `name`. Два однакові означають злиту історію цін."""
    names = [w["name"] for w in cfg["watches"]]
    assert len(names) == len(set(names)), \
        [n for n in names if names.count(n) > 1]


def test_кожен_запит_розкладається_в_api(cfg):
    """URL без `q-` або з незнайомою категорією тихо відкочується на HTML.

    Це не помилка сама по собі, але якщо так сталося ВИПАДКОВО (одрук у
    шляху категорії), watch стає втричі повільнішим і втрачає описи, нічого
    про це не сказавши.
    """
    broken = [w["name"] for w in _olx_watches(cfg)
              if olx.api_params(w["url"]) is None]
    assert not broken, f"не розкладаються в параметри API: {broken}"


def test_watchі_з_каталогу_існують(cfg):
    """`books: → watch:` звужує пошук книжки до перелічених watch'ів.

    Якщо там стоїть ім'я, якого серед watch'ів немає (перейменували,
    видалили, зробили одрук), книжка просто не збігається — і оголошення
    рахується не тією книжкою, мовчки.
    """
    names = {w["name"] for w in cfg["watches"]}
    unknown = sorted({w for b in cfg.get("books") or []
                      for w in (b.get("watch") or []) if w not in names})
    assert not unknown, f"у каталозі books: згадані неіснуючі watch'і: {unknown}"


def test_ніколи_не_здригайся_шле_все(cfg):
    """Нова книжка (2026-10-09): поки ціни невідомі — слати кожне оголошення.

    Коли з'явиться статистика, `notify_always` треба зняти й поставити
    `notify_max`. Тест на те й стоїть, щоб ця зміна була свідомою.
    """
    book = next(b for b in cfg["books"] if b["name"] == "Кінг: Ніколи не здригайся")
    assert book.get("notify_always") is True
    assert "notify_max" not in book, \
        "поріг і `notify_always` разом — суперечливо: вирішіть, що з них правда"

    # Запит за назвою має існувати: без нього оголошення, підписане самою
    # назвою без автора, у видачу не потрапляє.
    byname = {w["name"]: w for w in cfg["watches"]}
    params = olx.api_params(byname["Кінг: Ніколи не здригайся"]["url"])
    assert params["query"] == "ніколи не здригайся"

    # Корінь назви має бути в спільному списку книжок Кінга — тоді книжку
    # бачить не лише власний запит, а й усі десять точкових.
    # (Збиральний watch «стівен кінг» тут ні до чого: з 2026-10-09 він
    # шукає ознаки комплекту, а не назви книжок.)
    assert any("здригайся" in k
               for k in byname["Кінг: Талісман"]["include_keywords"])
