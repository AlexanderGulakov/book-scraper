"""Спільне для тестів.

Тут лише одна річ — латка сумісності mongomock з новим pymongo. Вона
стосується ТІЛЬКИ тестів: у бойовому коді ми говоримо зі справжнім Atlas,
де нічого латати не треба.

Суть розбіжності: pymongo ≥4.16 передає в bulk-операції аргумент `sort`,
якого mongomock 4.3 (остання версія) ще не знає, і будь-який `bulk_write`
з ReplaceOne падає з `TypeError: ... unexpected keyword argument 'sort'`.
Ми цим аргументом не користуємось, тож просто дозволяємо mongomock його
проковтнути. Коли mongomock оновиться — цей файл можна буде викинути.
"""

from __future__ import annotations

import pytest

try:  # mongomock може бути не встановлений — тоді тести storage просто skip
    from mongomock import collection as _mm_collection
except Exception:  # noqa: BLE001  pragma: no cover
    _mm_collection = None


def _tolerate_unknown_kwargs(cls, names: tuple[str, ...]) -> None:
    known = {"collation", "hint", "upsert", "array_filters"}
    for name in names:
        original = getattr(cls, name, None)
        if original is None:
            continue

        def wrapper(self, *args, __orig=original, **kwargs):
            return __orig(self, *args, **{k: v for k, v in kwargs.items() if k in known})

        setattr(cls, name, wrapper)


if _mm_collection is not None:
    _tolerate_unknown_kwargs(
        _mm_collection.BulkOperationBuilder,
        ("add_replace", "add_update", "add_delete", "add_insert"),
    )


@pytest.fixture
def mongo_client():
    """Порожній фейковий Mongo. Пропускає тест, якщо mongomock немає."""
    mongomock = pytest.importorskip("mongomock", reason="pip install mongomock")
    return mongomock.MongoClient()
