"""Структурированное логирование (structlog) с JSON-выводом для Loki.

Конфигурация единая для всего приложения:

* JSON-рендерер в продакшене — Promtail/Alloy забирает stdout и отправляет
  в Loki; каждая строка — самодостаточный JSON-event;
* Console-рендерер в разработке — человекочитаемый вывод ``ключ=значение``;
* процессоры времени, уровня, имени логгера и contextvars (request_id и т.п.);
* redaction-процессор вырезает секретные поля (password, token, secret...),
  что критично для коннектора, работающего с credentials 1С.

Дизайн: structlog конфигурируется на "make_logger" pipeline (структурные
события пишутся напрямую в настроенный вывод, минуя двойной рендер через
stdlib ProcessorFormatter). Логи сторонних библиотек (Flask, SQLAlchemy)
перехватываются stdlib-логгером root с обычным форматом и тоже попадают
в целевой поток.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import IO, TYPE_CHECKING, Final, TextIO

import structlog

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

__all__ = [
    "REDACT_PLACEHOLDER",
    "SENSITIVE_FIELDS",
    "get_logger",
    "setup_logging",
]

#: Поля, значения которых никогда не попадают в логи целиком.
SENSITIVE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "app_secret",
        "authorization",
        "api_key",
        "encryption_key",
        "password_encrypted",
    }
)

#: Значение-заглушка, заменяющее секреты в логах.
REDACT_PLACEHOLDER: Final[str] = "***"

#: Флаг идемпотентности: setup_logging выполнялся хотя бы раз.
_configured: bool = False


def _redact_sensitive(
    _logger: object, _method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Заменить значения чувствительных ключей на ``***``.

    Args:
        _logger: Логгер structlog (не используется).
        _method_name: Имя метода логирования (не используется).
        event_dict: Данные события.

    Returns:
        Тот же ``event_dict`` с вырезанными секретами.
    """
    for key in event_dict:
        if key.lower() in SENSITIVE_FIELDS:
            event_dict[key] = REDACT_PLACEHOLDER
    return event_dict


def _shared_processors(json_output: bool) -> list[structlog.types.Processor]:
    """Собрать общую цепочку процессоров до финального рендера.

    Args:
        json_output: ``True`` — финальный рендер в JSON (Loki).

    Returns:
        Список процессоров без финального рендерера.
    """
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.stdlib.ExtraAdder(),
        _redact_sensitive,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    if json_output:
        processors.append(structlog.processors.format_exc_info)
    return processors


def _build_renderer(json_output: bool) -> structlog.types.Processor:
    """Выбрать финальный рендерер: JSON (Loki) или консоль (разработка).

    Args:
        json_output: Флаг режима JSON.

    Returns:
        Процессор-рендерер.
    """
    if json_output:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=False)


def setup_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
    stream: IO[str] | None = None,
) -> None:
    """Настроить structlog + stdlib logging для всего приложения.

    Функция идемпотентна: повторные вызовы переконфигурируют pipeline
    целиком; собственные handler'ы модуля не дублируются.

    Args:
        level: Минимальный уровень (``DEBUG``/``INFO``/``WARNING``/...).
        json_output: ``True`` — компактный JSON (для Loki), ``False`` — консоль.
        stream: Целевой поток вывода; по умолчанию ``sys.stdout``.
    """
    global _configured  # noqa: PLW0603 - модульный флаг идемпотентности

    log_level = getattr(logging, level.upper(), logging.INFO)
    target: TextIO = (
        stream if stream is not None and hasattr(stream, "write") else sys.stdout
    )
    pre_chain = _shared_processors(json_output)
    renderer = _build_renderer(json_output)

    structlog.configure(
        processors=[*pre_chain, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.WriteLoggerFactory(file=target),
        cache_logger_on_first_use=False,
    )

    # stdlib logging: перехват логов сторонних библиотек тем же потоком.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_core_logging_handler", False):
            root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    handler = logging.StreamHandler(target)
    handler.setFormatter(formatter)
    handler.setLevel(log_level)
    setattr(handler, "_core_logging_handler", True)  # noqa: B010
    root.addHandler(handler)
    root.setLevel(log_level)

    _configured = True


def get_logger(name: str | None = None) -> BoundLogger:
    """Получить структурированный логгер по имени модуля.

    При первом обращении автоматически инициализирует логирование
    базовой конфигурацией, если :func:`setup_logging` еще не вызывался.

    Args:
        name: Имя логгера, обычно ``__name__`` модуля вызова.

    Returns:
        Связанный логгер structlog (``BoundLogger``).
    """
    if not _configured:
        setup_logging(level=os.environ.get("LOG_LEVEL", "INFO"))
    logger: BoundLogger = structlog.stdlib.get_logger(name)
    return logger
