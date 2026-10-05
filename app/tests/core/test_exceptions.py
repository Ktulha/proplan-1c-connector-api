"""Тесты модуля core.exceptions.

Проверяют иерархию ошибок, сериализацию в API-формат
(``{"data": ..., "meta": ..., "errors": [...]}``) и неизменяемость payload.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.exceptions import (
    AppError,
    EncryptionError,
    ExternalAPIError,
    NotFoundError,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Стратегии hypothesis
# ---------------------------------------------------------------------------

message_st = st.text(min_size=1, max_size=200)
status_st = st.integers(min_value=400, max_value=599)
code_st = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ_", min_size=1, max_size=30)
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-10**6, max_value=10**6)
    | st.text(max_size=50),
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=10), children, max_size=4),
    max_leaves=10,
)
details_st = st.dictionaries(st.text(min_size=1, max_size=20), json_values, max_size=5)


# ---------------------------------------------------------------------------
# Табличные проверки иерархии
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_class", "expected_status", "default_code"),
    [
        (AppError, 500, "APP_ERROR"),
        (ValidationError, 422, "VALIDATION_ERROR"),
        (NotFoundError, 404, "NOT_FOUND"),
        (ExternalAPIError, 502, "EXTERNAL_API_ERROR"),
        (EncryptionError, 500, "ENCRYPTION_ERROR"),
    ],
)
def test_defaults_and_hierarchy(
    exc_class: type[AppError], expected_status: int, default_code: str
) -> None:
    """Каждая ошибка имеет корректный статус по умолчанию и наследует AppError."""
    error = exc_class("boom")
    assert isinstance(error, AppError)
    assert isinstance(error, Exception)
    assert error.status_code == expected_status
    assert error.code == default_code
    assert error.message == "boom"


def test_external_api_error_carries_status_from_upstream() -> None:
    """ExternalAPIError сохраняет HTTP-статус внешнего сервиса."""
    error = ExternalAPIError("1C unavailable", status_code=504, service="1C OData")
    assert error.status_code == 504
    assert error.service == "1C OData"
    assert error.details["service"] == "1C OData"


def test_custom_code_and_details_override_defaults() -> None:
    """Конструктор позволяет переопределить код и детали."""
    error = AppError("x", code="CUSTOM", status_code=418, details={"a": 1})
    assert error.code == "CUSTOM"
    assert error.status_code == 418
    assert error.details == {"a": 1}


def test_to_dict_shape() -> None:
    """Словарь ошибки содержит стабильный набор ключей."""
    payload = ValidationError("bad field", details={"field": "name"}).to_dict()
    assert set(payload) == {"code", "message", "details"}
    assert payload["code"] == "VALIDATION_ERROR"
    assert payload["message"] == "bad field"
    assert payload["details"] == {"field": "name"}


def test_str_representation() -> None:
    """str(error) включает код и сообщение для логов."""
    text = str(AppError("oops"))
    assert "APP_ERROR" in text
    assert "oops" in text


# ---------------------------------------------------------------------------
# Property-based тесты
# ---------------------------------------------------------------------------


@settings(max_examples=50)
@given(message=message_st, status=status_st, code=code_st, details=details_st)
def test_to_dict_is_json_serializable_and_stable(
    message: str, status: int, code: str, details: dict[str, object]
) -> None:
    """to_dict() всегда дает JSON-совместимый словарь независимо от входа."""
    import json

    error = AppError(message, code=code, status_code=status, details=dict(details))
    payload = error.to_dict()
    restored = json.loads(json.dumps(payload))
    assert restored["code"] == code
    assert restored["message"] == message
    assert error.status_code == status


@settings(max_examples=50)
@given(message=message_st, details=details_st)
def test_payload_is_immutable_copy(message: str, details: dict[str, object]) -> None:
    """Мутация исходного словаря не влияет на internals ошибки (защита данных)."""
    source = dict(details)
    error = AppError(message, details=source)
    snapshot = dict(error.details)
    source["__injected__"] = True
    assert error.details == snapshot
    assert "__injected__" not in error.details
