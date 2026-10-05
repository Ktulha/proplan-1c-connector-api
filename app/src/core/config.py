"""Конфигурация приложения Repo 1 (1C OData API).

Загружает настройки из переменных окружения и файла ``.env``
(реальные переменные окружения имеют приоритет), валидирует их и
предоставляет классы ``Config`` / ``DevelopmentConfig`` / ``ProductionConfig``
в стиле Flask-конфига.

Обязательные переменные:
    DATABASE_URL: SQLAlchemy URL PostgreSQL (``postgresql+psycopg://...``).
    ENCRYPTION_KEY: base64-кодированный 32-байтный ключ AES-256
        (или ENCRYPTION_KEY_FILE — путь к файлу с ключом, Docker secrets).

Опциональные переменные:
    APP_SECRET, FLASK_ENV, API_PREFIX, ADMIN_URL, LOG_LEVEL, SYNC_TIMEZONE,
    MAX_WORKERS, HTTP_TIMEOUT, ALLOWED_ORIGINS, SQL_ECHO.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from dotenv import dotenv_values, load_dotenv

from core.exceptions import ValidationError

__all__ = [
    "Config",
    "DevelopmentConfig",
    "ProductionConfig",
    "get_config",
    "load_config",
]

_VALID_LOG_LEVELS: Final[frozenset[str]] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
_AES_256_KEY_BYTES: Final[int] = 32


@dataclass(frozen=True, slots=True)
class Config:
    """Иммутабельная конфигурация приложения.

    Attributes:
        env: Имя окружения (``development`` / ``production`` / ``testing``).
        database_url: SQLAlchemy DSN подключения к PostgreSQL.
        encryption_key: 32-байтный ключ AES-256 для шифрования credentials.
        app_secret: Секрет Flask для подписи сессий/flash-сообщений.
        api_prefix: Префикс REST API (``/api/v1`` по ТЗ).
        admin_url: Путь встроенного Flask-Admin UI.
        log_level: Уровень логирования (заглавные).
        sync_timezone: IANA-имя часового пояса планировщика APScheduler.
        max_workers: Максимум параллельных задач синхронизации.
        http_timeout: Таймаут HTTP-запросов к 1С OData, секунды.
        allowed_origins: Список разрешенных CORS-источников.
        debug: Флаг Flask debug-режима.
        testing: Флаг тестового режима.
        sql_echo: Логировать SQL-запросы SQLAlchemy.
    """

    env: str
    database_url: str
    encryption_key: bytes
    app_secret: str
    api_prefix: str = "/api/v1"
    admin_url: str = "/admin"
    log_level: str = "INFO"
    sync_timezone: str = "UTC"
    max_workers: int = 4
    http_timeout: float = 30.0
    allowed_origins: list[str] = field(default_factory=list)
    debug: bool = False
    testing: bool = False
    sql_echo: bool = False

    def as_flask_mapping(self) -> dict[str, object]:
        """Преобразовать конфиг в словарь настроек для ``Flask.config.from_mapping``.

        Returns:
            Словарь стандартных и прикладных ключей конфигурации Flask.
        """
        return {
            "ENV": self.env,
            "DEBUG": self.debug,
            "TESTING": self.testing,
            "SECRET_KEY": self.app_secret,
            "SQLALCHEMY_DATABASE_URI": self.database_url,
            "SQLALCHEMY_ECHO": self.sql_echo,
            "ENCRYPTION_KEY": self.encryption_key,
            "API_PREFIX": self.api_prefix,
            "ADMIN_URL": self.admin_url,
            "LOG_LEVEL": self.log_level,
            "SYNC_TIMEZONE": self.sync_timezone,
            "MAX_WORKERS": self.max_workers,
            "HTTP_TIMEOUT": self.http_timeout,
            "ALLOWED_ORIGINS": list(self.allowed_origins),
        }


class DevelopmentConfig(Config):
    """Конфигурация окружения разработки (debug включен)."""

    debug: bool = True


class ProductionConfig(Config):
    """Конфигурация боевого окружения (debug строго выключен)."""

    debug: bool = False


class TestingConfig(Config):
    """Конфигурация для автоматических тестов."""

    testing: bool = True
    debug: bool = False


_CONFIG_CLASSES: Final[dict[str, type[Config]]] = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def _require(env: dict[str, str], name: str) -> str:
    """Вернуть непустое значение обязательной переменной.

    Args:
        env: Словарь окружения.
        name: Имя переменной.

    Returns:
        Значение переменной.

    Raises:
        ValidationError: Если переменная отсутствует или пуста.
    """
    value = env.get(name, "").strip()
    if not value:
        raise ValidationError(
            f"Обязательная переменная окружения {name} не задана",
            details={"variable": name},
        )
    return value


def _parse_key(raw: str, *, variable: str) -> bytes:
    """Раскодировать и проверить base64-ключ AES-256.

    Args:
        raw: Base64-строка ключа.
        variable: Имя источника (для сообщения об ошибке).

    Returns:
        Декодированные байты ключа длиной 32.

    Raises:
        ValidationError: Если строка не является корректным base64
            или декодированный ключ длиннее/короче 32 байт.
    """
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(
            f"Переменная {variable} должна содержать base64-кодированный ключ",
            details={"variable": variable, "reason": "not_base64"},
        ) from exc
    if len(key) != _AES_256_KEY_BYTES:
        raise ValidationError(
            f"Ключ в {variable} должен быть ровно {_AES_256_KEY_BYTES} байта (AES-256)",
            details={"variable": variable, "actual_bytes": len(key)},
        )
    return key


def _parse_int(env: dict[str, str], name: str, default: int) -> int:
    """Прочитать целочисленную переменную окружения.

    Args:
        env: Словарь окружения.
        name: Имя переменной.
        default: Значение по умолчанию.

    Returns:
        Целое число >= 1.

    Raises:
        ValidationError: Если значение не число или меньше 1.
    """
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValidationError(
            f"Переменная {name} должна быть целым числом",
            details={"variable": name, "value": raw},
        ) from exc
    if value < 1:
        raise ValidationError(
            f"Переменная {name} должна быть >= 1",
            details={"variable": name, "value": value},
        )
    return value


def _parse_float(env: dict[str, str], name: str, default: float) -> float:
    """Прочитать вещественную положительную переменную окружения.

    Args:
        env: Словарь окружения.
        name: Имя переменной.
        default: Значение по умолчанию.

    Returns:
        Положительное число.

    Raises:
        ValidationError: Если значение не число или <= 0.
    """
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValidationError(
            f"Переменная {name} должна быть числом",
            details={"variable": name, "value": raw},
        ) from exc
    if value <= 0:
        raise ValidationError(
            f"Переменная {name} должна быть > 0",
            details={"variable": name, "value": value},
        )
    return value


def _parse_log_level(env: dict[str, str]) -> str:
    """Прочитать и нормализовать уровень логирования.

    Args:
        env: Словарь окружения.

    Returns:
        Уровень заглавными буквами; ``INFO`` по умолчанию.

    Raises:
        ValidationError: Если уровень недопустим.
    """
    raw = env.get("LOG_LEVEL", "").strip().upper()
    if not raw:
        return "INFO"
    if raw not in _VALID_LOG_LEVELS:
        raise ValidationError(
            "LOG_LEVEL должен быть одним из: " + ", ".join(sorted(_VALID_LOG_LEVELS)),
            details={"variable": "LOG_LEVEL", "value": raw},
        )
    return raw


def _parse_origins(env: dict[str, str]) -> list[str]:
    """Разобрать список CORS-источников из переменной ALLOWED_ORIGINS.

    Args:
        env: Словарь окружения.

    Returns:
        Список непустых trimmed-значений (пустой список, если не задано).
    """
    raw = env.get("ALLOWED_ORIGINS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _resolve_encryption_key(env: dict[str, str]) -> bytes:
    """Получить ключ AES-256 из ENCRYPTION_KEY или ENCRYPTION_KEY_FILE.

    Args:
        env: Словарь окружения.

    Returns:
        32-байтный ключ.

    Raises:
        ValidationError: Если ни один источник не задан либо файл недоступен.
    """
    file_path = env.get("ENCRYPTION_KEY_FILE", "").strip()
    if file_path:
        try:
            raw = Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValidationError(
                "Не удалось прочитать ENCRYPTION_KEY_FILE",
                details={"path": file_path, "error": str(exc)},
            ) from exc
        return _parse_key(raw, variable="ENCRYPTION_KEY_FILE")
    return _parse_key(_require(env, "ENCRYPTION_KEY"), variable="ENCRYPTION_KEY")


def load_config(
    *,
    env: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Config:
    """Загрузить и валидировать конфигурацию приложения.

    Порядок источников: значения из ``dotenv_path/.env`` дополняют окружение,
    реальные переменные окружения всегда имеют приоритет (``override=False``).

    Args:
        env: Явный словарь окружения (для тестов). По умолчанию —
            ``os.environ`` поверх значений из ``.env``.
        dotenv_path: Каталог, в котором искать ``.env``. По умолчанию — текущий.

    Returns:
        Экземпляр ``Config`` подкласса, соответствующего ``FLASK_ENV``.

    Raises:
        ValidationError: Если обязательные переменные отсутствуют или
            содержат некорректные значения.
    """
    if env is None:
        directory = dotenv_path if dotenv_path is not None else Path.cwd()
        load_dotenv(directory / ".env", override=False)
        source: dict[str, str] = dict(os.environ)
    else:
        source = dict(env)
        if dotenv_path is not None:
            loaded = {k: v for k, v in dotenv_values(dotenv_path / ".env").items() if v is not None}
            # .env НЕ перекрывает явно переданное окружение.
            source = {**loaded, **source}

    env_name = source.get("FLASK_ENV", "development").strip().lower() or "development"
    config_cls = _CONFIG_CLASSES.get(env_name)
    if config_cls is None:
        raise ValidationError(
            "FLASK_ENV должен быть одним из: " + ", ".join(sorted(_CONFIG_CLASSES)),
            details={"variable": "FLASK_ENV", "value": env_name},
        )

    encryption_key = _resolve_encryption_key(source)
    database_url = _require(source, "DATABASE_URL")
    app_secret = source.get("APP_SECRET", "").strip() or encryption_key.hex()

    kwargs: dict[str, object] = {
        "env": env_name,
        "database_url": database_url,
        "encryption_key": encryption_key,
        "app_secret": app_secret,
        "api_prefix": source.get("API_PREFIX", "").strip() or "/api/v1",
        "admin_url": source.get("ADMIN_URL", "").strip() or "/admin",
        "log_level": _parse_log_level(source),
        "sync_timezone": source.get("SYNC_TIMEZONE", "UTC").strip() or "UTC",
        "max_workers": _parse_int(source, "MAX_WORKERS", 4),
        "http_timeout": _parse_float(source, "HTTP_TIMEOUT", 30.0),
        "allowed_origins": _parse_origins(source),
        "sql_echo": source.get("SQL_ECHO", "").strip().lower() in {"1", "true", "yes"},
    }
    return config_cls(**kwargs)  # type: ignore[arg-type]


def get_config(
    *,
    env: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Config:
    """Фабрика конфигурации: псевдоним :func:`load_config`.

    Args:
        env: Явное окружение (для тестов).
        dotenv_path: Каталог с ``.env``.

    Returns:
        Готовый проверенный объект конфигурации.

    Raises:
        ValidationError: При некорректных или отсутствующих переменных.
    """
    return load_config(env=env, dotenv_path=dotenv_path)
