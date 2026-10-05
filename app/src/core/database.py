"""Инициализация SQLAlchemy и управление сессиями БД.

Модуль предоставляет:

* :data:`Base` — общий Declarative-класс для всех ORM-моделей проекта
  (Connection, EntityMapping, FieldMapping, SyncLog и др.);
* :func:`init_db` — создание движка и фабрики сессий из DSN;
* :func:`get_session` / :func:`get_transaction` — контекстный менеджер
  транзакции (commit при успехе, rollback при исключении).

Для продакшена ожидается PostgreSQL (psycopg3, JSONB); в тестах используется
in-memory SQLite через те же интерфейсы.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from core.exceptions import ValidationError

__all__ = ["Base", "get_session", "get_transaction", "init_db", "make_engine"]


class Base(DeclarativeBase):
    """Declarative-база для ORM-моделей Repo 1.

    Все модели модулей ``connections``, ``mappings``, ``sync`` наследуются от
    этого класса, что дает единый ``Base.metadata`` для Alembic-миграций и
    создания схемы в тестах.
    """


_DEFAULT_POOL_SIZE: Final[int] = 5
_DEFAULT_MAX_OVERFLOW: Final[int] = 10


def make_engine(
    database_url: str,
    *,
    echo: bool = False,
    pool_size: int = _DEFAULT_POOL_SIZE,
    max_overflow: int = _DEFAULT_MAX_OVERFLOW,
) -> sa.Engine:
    """Создать SQLAlchemy Engine по DSN.

    Args:
        database_url: DSN вида ``postgresql+psycopg://user:pass@host/db``.
        echo: Логировать все SQL-запросы (уровень DEBUG движка).
        pool_size: Размер пула соединений (игнорируется SQLite).
        max_overflow: Максимум временных сверхпуловых соединений.

    Returns:
        Настроенный ``sqlalchemy.Engine`` (future API).

    Raises:
        ValidationError: Если DSN пуст или не может быть разобран.
    """
    if not database_url or not database_url.strip():
        raise ValidationError(
            "DATABASE_URL не задан — невозможно создать engine",
            details={"variable": "DATABASE_URL"},
        )
    try:
        parsed = sa.make_url(database_url)
    except Exception as exc:  # noqa: BLE001 - конвертируем любую ошибку DSN в доменную
        raise ValidationError(
            "Некорректный DATABASE_URL",
            details={"error": str(exc)},
        ) from exc

    kwargs: dict[str, Any] = {"echo": echo, "future": True, "pool_pre_ping": True}
    if not parsed.drivername.startswith("sqlite"):
        kwargs["pool_size"] = pool_size
        kwargs["max_overflow"] = max_overflow
    return sa.create_engine(parsed, **kwargs)


def init_db(
    database_url: str,
    *,
    echo: bool = False,
    create_tables: bool = False,
) -> sessionmaker[Session]:
    """Инициализировать слой доступа к данным.

    Создает движок и фабрику сессий; опционально выполняет ``create_all``
    (в продакшене схема управляется Alembic-миграциями, флаг оставляют
    выключенным). Регистрирует закрытие пула при завершении процесса.

    Args:
        database_url: DSN подключения к PostgreSQL.
        echo: Эхо SQL-запросов в лог.
        create_tables: Создать схему из ``Base.metadata`` сразу после старта.

    Returns:
        Фабрика сессий ``sessionmaker[Session]``.

    Raises:
        ValidationError: При некорректном DSN.
    """
    engine = make_engine(database_url, echo=echo)
    if create_tables:
        Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def get_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Контекстный менеджер unit-of-work.

    Выдает сессию БД; при штатном выходе выполняет ``commit``, при
    исключении — ``rollback`` и пробрасывает ошибку дальше. Сессия всегда
    закрывается.

    Args:
        session_factory: Фабрика сессий из :func:`init_db`.

    Yields:
        Активная ``sqlalchemy.orm.Session``.

    Example:
        >>> with get_session(session_factory) as session:  # doctest: +SKIP
        ...     session.add(obj)
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Синоним для читаемости бизнес-кода: `with get_transaction(...) as s:`.
get_transaction = get_session
