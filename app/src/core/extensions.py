"""Инициализация сторонних расширений Flask: Flask-Admin, APScheduler, Migrate.

Модуль собирает «каркас» приложения Repo 1 в единой точке
:func:`init_extensions`:

* **SQLAlchemy session factory** — создается через :func:`core.database.init_db`
  и хранится в ``app.extensions``; бизнес-код получает сессию через
  :func:`get_session` (unit-of-work: commit при успехе, rollback при ошибке);
* **Flask-Migrate** — Alembic-миграции схемы (CLI-команда ``flask db upgrade``);
  привязывается к ``Base.metadata`` напрямую (без flask-sqlalchemy);
* **APScheduler** — ``BackgroundScheduler`` с таймзоной из конфигурации;
  задачи регистрируются через :func:`add_job`, останавливаются через
  :func:`shutdown_scheduler` (graceful shutdown по сигналу/atexit);
* **Flask-Admin** — служебный UI (``/admin``) поверх ORM-моделей ``Base``;
  включается явно (``include_admin=True``), чтобы не поднимать лишний UI
  в тестах и чистых API-инстансах.

Планировщик — синглтон на приложение: повторные вызовы ``init_extensions``
(Flask reloader, тесты) переиспользуют существующий экземпляр.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_migrate import Migrate
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import Config
from core.database import Base, get_session as db_get_session
from core.database import init_db

if TYPE_CHECKING:
    from apscheduler.job import Job

__all__ = [
    "add_job",
    "get_scheduler",
    "get_session",
    "get_session_factory",
    "init_extensions",
    "shutdown_scheduler",
]

#: Ключи реестра ``app.extensions`` (соглашение Flask-расширений).
EXT_SESSION_FACTORY: Final[str] = "sqlalchemy_session_factory"
EXT_SCHEDULER: Final[str] = "apscheduler"
EXT_MIGRATE: Final[str] = "migrate"

# Слабые ссылки на созданные планировщики: shutdown работает без app
# (сигнальные обработчики, atexit в точке входа приложения).
_LIVE_SCHEDULERS: Final[weakref.WeakSet[BackgroundScheduler]] = weakref.WeakSet()


def _make_scheduler(timezone_name: str) -> BackgroundScheduler:
    """Создать и зарегистрировать в реестре остановленный планировщик.

    Args:
        timezone_name: IANA-имя часового пояса (например, ``Europe/Moscow``).

    Returns:
        Новый ``BackgroundScheduler`` (ещё не запущен).
    """
    scheduler = BackgroundScheduler(timezone=timezone_name)
    _LIVE_SCHEDULERS.add(scheduler)
    return scheduler


def init_extensions(
    app: Flask,
    config: Config,
    *,
    include_admin: bool = False,
    start_scheduler: bool = True,
) -> Admin | None:
    """Инициализировать все расширения приложения.

    Args:
        app: Экземпляр Flask-приложения.
        config: Загруженная и валидированная конфигурация.
        include_admin: Поднять ли Flask-Admin поверх моделей ``Base``.
        start_scheduler: Запустить ли APScheduler сразу (в воркере gunicorn —
            ``True``; при reload/тестах — ``False``).

    Returns:
        Экземпляр ``Admin``, если ``include_admin=True``, иначе ``None``.

    Raises:
        core.exceptions.ValidationError: При некорректном DSN из конфигурации.
    """
    app.config.from_mapping(config.as_flask_mapping())

    # --- SQLAlchemy session factory -------------------------------------
    session_factory = init_db(config.database_url, echo=config.sql_echo)
    engine: Engine = session_factory.kw["bind"]
    app.extensions[EXT_SESSION_FACTORY] = session_factory

    # --- Flask-Migrate (Alembic поверх Base.metadata) --------------------
    migrate: Migrate | None = app.extensions.get(EXT_MIGRATE, None)  # type: ignore[assignment]
    if not isinstance(migrate, Migrate):
        migrate = Migrate()
        migrate.init_app(app, db=Base.metadata, directory="migrations")
    app.extensions[EXT_MIGRATE] = migrate

    # --- APScheduler ------------------------------------------------------
    scheduler: BackgroundScheduler | None = app.extensions.get(EXT_SCHEDULER, None)  # type: ignore[assignment]
    if not isinstance(scheduler, BackgroundScheduler):
        scheduler = _make_scheduler(config.sync_timezone)
        app.extensions[EXT_SCHEDULER] = scheduler
    if start_scheduler and not scheduler.running:
        scheduler.start()

    # --- Flask-Admin --------------------------------------------------------
    admin: Admin | None = None
    if include_admin:
        admin = Admin(
            app,
            name="1C Connector Admin",
            url=config.admin_url,
        )
        for mapper in Base.registry.mappers:
            admin.add_view(ModelView(mapper.class_, session_factory))
    return admin


def get_session_factory(app: Flask) -> sessionmaker[Session]:
    """Вернуть фабрику сессий, зарегистрированную в приложении.

    Args:
        app: Инициализированное Flask-приложение.

    Returns:
        ``sessionmaker[Session]`` из ``app.extensions``.

    Raises:
        RuntimeError: Если :func:`init_extensions` еще не вызывался.
    """
    factory = app.extensions.get(EXT_SESSION_FACTORY, None)
    if not isinstance(factory, sessionmaker):
        raise RuntimeError("Расширения не инициализированы — вызовите init_extensions()")
    return factory


@contextmanager
def get_session(app: Flask) -> Iterator[Session]:
    """Unit-of-work контекст: сессия с commit при успехе / rollback при ошибке.

    Args:
        app: Инициализированное Flask-приложение.

    Yields:
        Активная ``sqlalchemy.orm.Session``.

    Example:
        >>> with get_session(app) as session:  # doctest: +SKIP
        ...     rows = session.execute(select(Connection)).scalars().all()
    """
    with db_get_session(get_session_factory(app)) as session:
        yield session


def get_scheduler(app: Flask) -> BackgroundScheduler:
    """Вернуть планировщик приложения (создать остановленный, если отсутствует).

    Args:
        app: Flask-приложение.

    Returns:
        Синглтон ``BackgroundScheduler`` данного приложения.
    """
    scheduler: BackgroundScheduler | None = app.extensions.get(EXT_SCHEDULER, None)  # type: ignore[assignment]
    if not isinstance(scheduler, BackgroundScheduler):
        scheduler = _make_scheduler("UTC")
        app.extensions[EXT_SCHEDULER] = scheduler
    return scheduler


def add_job(
    app: Flask,
    func: Callable[..., Any],
    trigger: str,
    *,
    job_id: str,
    replace_existing: bool = True,
    **trigger_kwargs: Any,
) -> Job:
    """Зарегистрировать периодическую задачу синхронизации в планировщике.

    Args:
        app: Flask-приложение.
        func: Вызываемая задача (например, ``run_sync_cycle``).
        trigger: Тип триггера APScheduler (``interval`` / ``cron``).
        job_id: Уникальный идентификатор задачи.
        replace_existing: Перезаписать задачу с тем же id (идемпотентный старт).
        **trigger_kwargs: Параметры триггера (``minutes=5``, ``hour=3`` и т.п.).

    Returns:
        Созданный объект ``apscheduler.job.Job``.
    """
    return get_scheduler(app).add_job(
        func,
        trigger,
        id=job_id,
        replace_existing=replace_existing,
        **trigger_kwargs,
    )


def shutdown_scheduler(wait: bool = True) -> None:
    """Остановить все живые планировщики (graceful shutdown).

    Безопасен при отсутствии запущенных планировщиков; идемпотентен.

    Args:
        wait: Ждать ли окончания выполняющихся задач.
    """
    for scheduler in list(_LIVE_SCHEDULERS):
        if scheduler.running:
            scheduler.shutdown(wait=wait)
