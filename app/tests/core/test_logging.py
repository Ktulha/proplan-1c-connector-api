"""Тесты модуля core.logging (structlog + JSON для Loki).

Логи перехватываются фикстурой ``caplog`` через стандартный logging,
так как процессор ``RendererJSON`` пишет итог в stdlib-логгер.
"""

from __future__ import annotations

import json
import logging

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.contextvars import bind_contextvars, clear_contextvars

import core.logging as core_logging


@pytest.fixture(autouse=True)
def _reset_state() -> object:
    """Сбрасывать конфигурацию structlog и контекст между тестами."""
    clear_contextvars()
    core_logging._configured = False  # noqa: SLF001
    yield
    clear_contextvars()
    core_logging._configured = False  # noqa: SLF001


def parse_last_record(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    """Разобрать JSON из последней записи лога.

    Args:
        caplog: Фикстура перехвата stdlib-логов.

    Returns:
        Разобранная JSON-строка события.
    """
    record = caplog.records[-1]
    return json.loads(record.getMessage())


def log_event(payload: dict[str, object]) -> str:
    """Сформировать structlog-событие так, чтобы ProcessorFormatter не давал рекурсии.

    ``ProcessorFormatter`` применяет внешние процессоры к записям, которые уже
    прошли structlog-pipeline (dict-сообщения), поэтому в тестах передаем
    готовый JSON через специальный ключ ``msg_json``.

    Args:
        payload: Данные события (event, level и т.д.).

    Returns:
        JSON-строка для записи в лог.
    """
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_outputs_json(caplog: pytest.LogCaptureFixture) -> None:
    """После инициализации логи пишутся одной JSON-строкой."""
    caplog.set_level(logging.INFO)
    core_logging.setup_logging(level="INFO", json_output=True)
    log = core_logging.get_logger("core.test")
    log.info("sync_started", connection_id=7)

    payload = parse_last_record(caplog)
    assert payload["event"] == "sync_started"
    assert payload["level"] == "info"
    assert payload["connection_id"] == 7
    assert "timestamp" in payload


def test_setup_logging_is_idempotent(caplog: pytest.LogCaptureFixture) -> None:
    """Повторный вызов setup_logging не ломает логирование."""
    caplog.set_level(logging.INFO)
    core_logging.setup_logging(level="INFO")
    core_logging.setup_logging(level="INFO")
    core_logging.get_logger("x").info("second_call_ok")
    assert parse_last_record(caplog)["event"] == "second_call_ok"


def test_level_filters_debug(caplog: pytest.LogCaptureFixture) -> None:
    """Уровень WARNING глушит debug/info сообщения."""
    core_logging.setup_logging(level="WARNING")
    log = core_logging.get_logger("filtered")
    log.debug("hidden_debug")
    log.warning("visible_warning")
    events = [json.loads(r.getMessage())["event"] for r in caplog.records]
    assert "hidden_debug" not in events
    assert "visible_warning" in events


def test_console_mode_for_development(caplog: pytest.LogCaptureFixture) -> None:
    """json_output=False дает человекочитаемый ключ=value вывод."""
    caplog.set_level(logging.INFO)
    core_logging.setup_logging(level="INFO", json_output=False)
    core_logging.get_logger("dev").info("hello_dev", foo="bar")
    message = caplog.records[-1].getMessage()
    assert "hello_dev" in message
    assert "foo=bar" in message
    # при этом это НЕ валидный JSON — консольный рендерер
    with pytest.raises(json.JSONDecodeError):
        json.loads(message)


# ---------------------------------------------------------------------------
# Контекстные поля (request id, connection id)
# ---------------------------------------------------------------------------


def test_contextvars_enrich_all_events(caplog: pytest.LogCaptureFixture) -> None:
    """bind_contextvars добавляет поля ко всем последующим событиям."""
    caplog.set_level(logging.INFO)
    core_logging.setup_logging()
    bind_contextvars(request_id="req-42")
    core_logging.get_logger("ctx").info("first")
    core_logging.get_logger("ctx").info("second")
    payloads = [json.loads(r.getMessage()) for r in caplog.records]
    assert all(p["request_id"] == "req-42" for p in payloads)


# ---------------------------------------------------------------------------
# Property-based: секретные поля вырезаются
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = ["password", "token", "secret", "authorization", "api_key"]


@settings(max_examples=30)
@given(
    key=st.sampled_from(SENSITIVE_KEYS),
    value=st.text(min_size=1, max_size=50),
    event=st.text(min_size=1, max_size=40).filter(lambda s: "\n" not in s and '"' not in s),
)
def test_secrets_never_leak_to_logs(
    key: str, value: str, event: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Любое чувствительное поле заменяется на *** независимо от значения."""
    caplog.set_level(logging.INFO)
    core_logging.setup_logging(json_output=True)
    core_logging.get_logger("sec").info(event, **{key: value})
    raw = caplog.records[-1].getMessage()
    payload = json.loads(raw)
    assert payload[key] == "***"
    assert value not in raw


def test_get_logger_returns_bound_instance() -> None:
    """get_logger возвращает связанный логгер с именем модуля."""
    core_logging.setup_logging()
    log = core_logging.get_logger("my.module")
    assert getattr(log, "name", None) == "my.module" or "my.module" in repr(log)
