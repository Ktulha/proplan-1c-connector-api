"""Общие фикстуры pytest для тестов модуля core."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Гарантируем детерминизм: переменные окружения процесса не влияют на юнит-тесты,
# которые явно передают env в load_config().
for _name in (
    "DATABASE_URL",
    "ENCRYPTION_KEY",
    "ENCRYPTION_KEY_FILE",
    "APP_SECRET",
    "FLASK_ENV",
):
    os.environ.pop(_name, None)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Удалить все переменные конфигурации из реального окружения процесса."""
    for name in (
        "DATABASE_URL",
        "ENCRYPTION_KEY",
        "ENCRYPTION_KEY_FILE",
        "APP_SECRET",
        "FLASK_ENV",
        "LOG_LEVEL",
        "MAX_WORKERS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
