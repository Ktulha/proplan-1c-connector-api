"""Иерархия пользовательских исключений приложения.

Все ошибки приложения наследуют :class:`AppError`, что позволяет единым
образом обрабатывать их во Flask-обработчиках и сериализовать в формат
``{"data": ..., "meta": ..., "errors": [...]}``.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AppError",
    "EncryptionError",
    "ExternalAPIError",
    "NotFoundError",
    "ValidationError",
]


class AppError(Exception):
    """Базовая ошибка приложения.

    Attributes:
        message: Человекочитаемое описание проблемы.
        code: Машинночитаемый код ошибки (SCREAMING_SNAKE_CASE).
        status_code: HTTP-статус, соответствующий ошибке.
        details: Дополнительные структурированные данные.
    """

    default_message: str = "Внутренняя ошибка приложения"
    default_code: str = "APP_ERROR"
    default_status_code: int = 500

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Инициализировать ошибку.

        Args:
            message: Описание ошибки; по умолчанию ``default_message``.
            code: Код ошибки; по умолчанию ``default_code``.
            status_code: HTTP-статус; по умолчанию ``default_status_code``.
            details: Дополнительные данные; сохраняется копия словаря.

        Raises:
            ValidationError: Если ``details`` не является словарем.
        """
        if details is not None and not isinstance(details, dict):
            raise ValidationError(
                "details должен быть словарем",
                details={"provided_type": type(details).__name__},
            )
        super().__init__(message or self.default_message)
        self.message: str = message or self.default_message
        self.code: str = code or self.default_code
        self.status_code: int = status_code or self.default_status_code
        # Копия защищает internals ошибки от внешней мутации.
        self.details: dict[str, Any] = dict(details) if details else {}

    def to_dict(self) -> dict[str, Any]:
        """Сериализовать ошибку в JSON-совместимый словарь.

        Returns:
            Словарь вида ``{"code": ..., "message": ..., "details": {...}}``.
        """
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:
        """Вернуть строку ``[CODE] message`` для логов."""
        return f"[{self.code}] {self.message}"


class ValidationError(AppError):
    """Ошибка валидации входных данных или конфигурации (HTTP 422)."""

    default_message: str = "Ошибка валидации данных"
    default_code: str = "VALIDATION_ERROR"
    default_status_code: int = 422


class NotFoundError(AppError):
    """Запрошенный ресурс не найден (HTTP 404)."""

    default_message: str = "Ресурс не найден"
    default_code: str = "NOT_FOUND"
    default_status_code: int = 404


class ExternalAPIError(AppError):
    """Ошибка внешнего сервиса (1С OData, Proplan GraphQL и т.п., HTTP 502)."""

    default_message: str = "Ошибка внешнего API"
    default_code: str = "EXTERNAL_API_ERROR"
    default_status_code: int = 502

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
        service: str | None = None,
        upstream_status: int | None = None,
    ) -> None:
        """Инициализировать ошибку внешнего API.

        Args:
            message: Описание ошибки.
            code: Код ошибки.
            status_code: HTTP-статус ответа клиенту.
            details: Дополнительные данные.
            service: Имя/идентификатор внешнего сервиса.
            upstream_status: HTTP-статус, полученный от внешнего сервиса.
        """
        merged: dict[str, Any] = dict(details) if details else {}
        if service is not None:
            merged["service"] = service
        if upstream_status is not None:
            merged["upstream_status"] = upstream_status
        super().__init__(message, code=code, status_code=status_code, details=merged)
        self.service: str | None = service
        self.upstream_status: int | None = upstream_status


class EncryptionError(AppError):
    """Ошибка шифрования/дешифрования credentials (AES-256)."""

    default_message: str = "Ошибка шифрования"
    default_code: str = "ENCRYPTION_ERROR"
    default_status_code: int = 500
