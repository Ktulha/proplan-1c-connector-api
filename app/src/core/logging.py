"""Структурированное логирование (structlog) с JSON-выводом для Loki.

Конфигурация единая для всего приложения:

* JSON-рендерер в продакшене — Promtail/Alloy забирает stdout и отправляет
  в Loki; каждая строка — самодостаточный JSON-event;
* Console-рендерер в разработке — человекочитаемый вывод ``ключ=значение``;
* процессоры времени, уровня, имени логгера и contextvars (request_id и т.п.);
* redaction-процессор вырезает секретные поля (password, token, secret...),
  что критично для коннектора, работающего с credentials 1С.

Дизайн: structlog работает поверх stdlib ``logging`` (``ProcessorFormatter``),
поэтому логи сторонних библиотек (Flask, SQLAlchemy) проходят тот же
redaction/рендер pipeline и попадают в Loki в едином формате.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Final

import structlog

__all__ = [
    "REDACT_PLACEHOLDER",
    "SENSITIVE_FIELDS",
    "get_logger",
    "make_log_capture_handler",
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

#: Ключ event_dict, по которому тестовый handler узнает готовые события.
CAPTURE_KEY: Final[str] = "_captured_event"


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
    """Собрать общую цепочку процессоров для structlog и stdlib-записей.

    Args:
        json_output: ``True`` — финальный рендер в JSON (Loki).

    Returns:
        Список процессоров без финального рендерера.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _redact_sensitive,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(colors=False)
        if not json_output
        else structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]


def setup_logging(
    level: str = "INFO",
    *,
    json_output: bool = True,
    stream: Any | None = None,
) -> None:
    """Настроить structlog + stdlib logging для всего приложения.

    Функция идемпотентна: повторные вызовы переконфигурируют pipeline
    целиком; собственные handler'ы модуля не дублируются.

    Args:
        level: Минимальный уровень (``DEBUG``/``INFO``/``WARNING``/...).
        json_output: ``True`` — компактный JSON (для Loki), ``False`` — консоль.
        stream: Целевой поток вывода; по умолчанию ``sys.stderr``.
    """
    global _configured  # noqa: PLW0603 - модульный флаг идемпотентности

    log_level = getattr(logging, level.upper(), logging.INFO)
    target = stream if stream is not None else sys.stderr
    pre_chain = _shared_processors(json_output)
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*pre_chain, CAPTURE_MARKER_PROCESSOR, renderer],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_core_logging_handler", False):
            root.removeHandler(handler)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(target)
    handler.setFormatter(formatter)
    handler.setLevel(log_level)
    handler._core_logging_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(log_level)

    _configured = True


def capture_to_dict(
    _logger: object, _method_name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Пометить событие как прошедшее structlog-pipeline (для ProcessorFormatter).

    Args:
        _logger: Логгер (не используется).
        _method_name: Метод логирования (не используется).
        event_dict: Данные события.

    Returns:
        Event с добавленным маркером ``CAPTURE_KEY``.
    """
    event_dict[CAPTURE_KEY] = True
    return event_dict


# Маркер используется ProcessorFormatter для различения structlog-событий
# и «чужих» stdlib-записей (Flask, SQLAlchemy).
CAPTURE_MARKER_PROCESSOR = capture_to_dict


class LogCaptureHandler(logging.Handler):
    """Handler для тестов: сохраняет итоговые отрендеренные строки логов.

    Заменяет собой потоковый handler root-логгера в pytest, позволяя
    проверять фактический JSON/консольный вывод без парсинга caplog.
    """

    def __init__(self) -> None:
        """Инициализировать пустое хранилище захваченных строк."""
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Сохранить отформатированную запись.

        Args:
            record: Запись stdlib logging.
        """
        self.messages.append(self.format(record))


def make_log_capture_handler(level: int = logging.INFO) -> LogCaptureHandler:
    """Подготовить приложение к тестовому перехвату логов.

    Вызывает :func:`setup_logging` и возвращает handler, который можно
    добавить к root-логгеру для чтения готовых строк вывода.

    Args:
        level: Уровень фильтрации захватчика.

    Returns:
        Экземпляр :class:`LogCaptureHandler` со списком ``messages``.
    """
    setup_logging(level=logging.getLevelName(level))
    handler = LogCaptureHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.getLogger().handlers[-1].formatter)
    return handler


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Получить структурированный логгер по имени модуля.

    При первом обращении автоматически инициализирует логирование
    базовой конфигурацией, если :func:`setup_logging` еще не вызывался.

    Args:
        name: Имя логгера, обычно ``__name__`` модуля вызова.

    Returns:
        Связанный логгер structlog (``BoundLogger``).
    """
    if not _configured:
        setup_logging()
    return structlog.get_logger(name)
