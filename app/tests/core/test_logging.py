"""Тесты модуля core.logging (structlog + JSON-вывод для Loki).

Вывод перехватывается через явный ``stream`` (io.StringIO), который
передается в ``setup_logging`` — это независимо от internals structlog.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from structlog.contextvars import bind_contextvars, clear_contextvars

import core.logging as core_logging


@pytest.fixture(autouse=True)
def _reset_state() -> object:
    """Сбрасывать конфигурацию structlog и контекст между тестами."""
    clear_contextvars()
    core_logging._configured = False
    yield
    clear_contextvars()
    core_logging._configured = False


def make_stream(json_output: bool = True, level: str = "INFO") -> io.StringIO:
    """Настроить логирование с перехватом в StringIO и вернуть поток.

    Args:
        json_output: Режим JSON (Loki) либо человекочитаемой консоли.
        level: Минимальный уровень логирования.

    Returns:
        Поток, в который пишутся отрендеренные строки логов.
    """
    stream = io.StringIO()
    core_logging.setup_logging(level=level, json_output=json_output, stream=stream)
    return stream


def last_event(stream: io.StringIO) -> dict[str, object]:
    """Разобрать JSON последней записанной строки.

    Args:
        stream: Поток с выводом логов.

    Returns:
        Разобранное событие.
    """
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert lines, "лог-вывод пуст"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_outputs_json() -> None:
    """После инициализации логи пишутся одной JSON-строкой."""
    stream = make_stream(json_output=True)
    core_logging.get_logger("core.test").info("sync_started", connection_id=7)

    payload = last_event(stream)
    assert payload["event"] == "sync_started"
    assert payload["level"] == "info"
    assert payload["connection_id"] == 7
    assert "timestamp" in payload


def test_setup_logging_is_idempotent() -> None:
    """Повторный вызов setup_logging не ломает логирование и не дублирует handler."""
    stream = make_stream()
    core_logging.setup_logging(json_output=True, stream=stream)
    core_logging.get_logger("x").info("second_call_ok")
    assert last_event(stream)["event"] == "second_call_ok"
    own = [h for h in logging.getLogger().handlers if getattr(h, "_core_logging_handler", False)]
    assert len(own) == 1


def test_level_filters_debug() -> None:
    """Уровень WARNING глушит debug/info сообщения."""
    stream = make_stream(level="WARNING")
    log = core_logging.get_logger("filtered")
    log.debug("hidden_debug")
    log.warning("visible_warning")
    raw = stream.getvalue()
    assert "hidden_debug" not in raw
    assert "visible_warning" in raw


def test_console_mode_for_development() -> None:
    """json_output=False дает человекочитаемый ключ=value вывод."""
    stream = make_stream(json_output=False)
    core_logging.get_logger("dev").info("hello_dev", foo="bar")
    message = stream.getvalue()
    assert "hello_dev" in message
    assert "foo=bar" in message
    # при этом это НЕ валидный JSON — консольный рендерер
    with pytest.raises(json.JSONDecodeError):
        json.loads(message.strip())


# ---------------------------------------------------------------------------
# Контекстные поля (request id, connection id)
# ---------------------------------------------------------------------------


def test_contextvars_enrich_all_events() -> None:
    """bind_contextvars добавляет поля ко всем последующим событиям."""
    stream = make_stream()
    bind_contextvars(request_id="req-42")
    core_logging.get_logger("ctx").info("first")
    core_logging.get_logger("ctx").info("second")
    lines = [ln for ln in stream.getvalue().splitlines() if ln.strip()]
    payloads = [json.loads(ln) for ln in lines]
    assert payloads, "нет захваченных событий"
    assert all(p["request_id"] == "req-42" for p in payloads)


# ---------------------------------------------------------------------------
# Property-based: секретные поля вырезаются
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = ["password", "token", "secret", "authorization", "api_key"]


@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    key=st.sampled_from(SENSITIVE_KEYS),
    value=st.text(min_size=8, max_size=50).filter(lambda s: "\n" not in s and '"' not in s),
)
def test_secrets_never_leak_to_logs(key: str, value: str) -> None:
    """Любое чувствительное поле заменяется на *** независимо от значения."""
    stream = make_stream(json_output=True)
    core_logging.get_logger("sec").info("evt", **{key: value})
    raw = stream.getvalue()
    payload = last_event(stream)
    assert payload[key] == "***"
    assert value not in raw


def test_get_logger_returns_bound_instance() -> None:
    """get_logger возвращает связанный логгер с именем модуля."""
    make_stream()
    log = core_logging.get_logger("my.module")
    assert getattr(log, "name", None) == "my.module" or "my.module" in repr(log)


def test_get_logger_autosetup() -> None:
    """get_logger без предварительного setup_logging инициализует pipeline сам."""
    assert core_logging._configured is False
    log = core_logging.get_logger("auto")
    assert log is not None
    assert core_logging._configured is True
