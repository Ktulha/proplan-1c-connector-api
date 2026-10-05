"""Тесты модуля core.extensions (Flask-Admin, APScheduler, Flask-Migrate).

Используют in-memory SQLite и минимальное Flask-приложение; внешние
сервисы не требуются. Планировщик запускается только явно
(``start_scheduler=True``), чтобы тесты не зависели от фоновых потоков.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

import core.extensions as ext
from core.config import Config
from core.database import Base


@pytest.fixture(autouse=True)
def _reset_registry() -> Iterator[None]:
    """Остановить живые планировщики до и после каждого теста."""
    ext.shutdown_scheduler()
    yield
    ext.shutdown_scheduler()


def make_config(tmp_path: Path) -> Config:
    """Собрать конфигурацию для тестов (SQLite в файле tmp_path).

    Args:
        tmp_path: Директория pytest для артефактов теста.

    Returns:
        Экземпляр ``Config`` с валидным ключом шифрования.
    """
    db_file = tmp_path / "test.db"
    return Config(
        env="testing",
        database_url=f"sqlite:///{db_file}",
        encryption_key=base64.b64decode(base64.b64encode(b"0" * 32)),
        app_secret="test-secret",
        sync_timezone="Europe/Moscow",
    )


class DummyModel(Base):
    """Модель-заглушка для проверки регистрации Flask-Admin."""

    __tablename__ = "dummy_model"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(default="")


# ---------------------------------------------------------------------------
# init_extensions — Flask-Migrate + SQLAlchemy session
# ---------------------------------------------------------------------------


def test_init_extensions_sets_config_and_session(tmp_path: Path) -> None:
    """Экземпляр Config кладется в app.config, фабрика сессий доступна."""
    cfg = make_config(tmp_path)
    app = Flask(__name__)
    ext.init_extensions(app, cfg)

    assert app.config["API_PREFIX"] == "/api/v1"
    assert app.config["ENCRYPTION_KEY"] == cfg.encryption_key
    factory = ext.get_session_factory(app)
    assert isinstance(factory, sessionmaker)
    with ext.get_session(app) as session:
        assert isinstance(session, Session)


def test_get_session_without_init_raises(tmp_path: Path) -> None:
    """Без инициализации расширение недоступно — понятная ошибка."""
    app = Flask(__name__)
    with pytest.raises(RuntimeError, match="init_extensions"):
        with ext.get_session(app):
            pass


def test_migrations_registered_on_app(tmp_path: Path) -> None:
    """Flask-Migrate инициализирован: команда `db` доступна в CLI."""
    cfg = make_config(tmp_path)
    app = Flask(__name__)
    ext.init_extensions(app, cfg)

    command_names = set(app.cli.commands)  # type: ignore[attr-defined]
    assert "db" in command_names


# ---------------------------------------------------------------------------
# Flask-Admin
# ---------------------------------------------------------------------------


def test_admin_not_created_by_default(tmp_path: Path) -> None:
    """По умолчанию админка не поднимается (нужен явный флаг)."""
    cfg = make_config(tmp_path)
    app = Flask(__name__)
    assert ext.init_extensions(app, cfg) is None


def test_admin_mounts_models(tmp_path: Path) -> None:
    """При include_admin=True модели Base.metadata регистрируются во Flask-Admin."""
    cfg = make_config(tmp_path)
    app = Flask(__name__)
    admin = ext.init_extensions(app, cfg, include_admin=True)

    assert admin is not None
    assert app.config["ADMIN_URL"] in ("/admin", cfg.admin_url)
    view_names = {view.name for view in admin._views}  # noqa: SLF001
    assert "DummyModel" in view_names


# ---------------------------------------------------------------------------
# APScheduler
# ---------------------------------------------------------------------------


def test_scheduler_created_stopped_with_timezone(tmp_path: Path) -> None:
    """Планировщик создается остановленным и с таймзоной из конфига."""
    cfg = make_config(tmp_path)
    app = Flask(__name__)
    ext.init_extensions(app, cfg, start_scheduler=False)

    scheduler = ext.get_scheduler(app)
    assert isinstance(scheduler, BackgroundScheduler)
    assert not scheduler.running
    assert str(scheduler.timezone) == "Europe/Moscow"


def test_scheduler_is_singleton_per_app(tmp_path: Path) -> None:
    """Повторный init_extensions переиспользует тот же планировщик."""
    cfg = make_config(tmp_path)
    app = Flask(__name__)
    ext.init_extensions(app, cfg, start_scheduler=False)
    first = ext.get_scheduler(app)
    ext.init_extensions(app, cfg, start_scheduler=False)
    assert ext.get_scheduler(app) is first


def test_start_stop_scheduler_lifecycle(tmp_path: Path) -> None:
    """Явный запуск и остановка планировщика переводят состояние корректно."""
    cfg = make_config(tmp_path)
    app = Flask(__name__)
    ext.init_extensions(app, cfg, start_scheduler=True)

    scheduler = ext.get_scheduler(app)
    assert scheduler.running

    ext.shutdown_scheduler()
    assert not scheduler.running


def test_add_job_wraps_in_sqlalchemy_job(tmp_path: Path) -> None:
    """add_job передаёт SQLAlchemyJobStore-compatible callable и id."""
    cfg = make_config(tmp_path)
    app = Flask(__name__)
    ext.init_extensions(app, cfg)
    scheduler = ext.get_scheduler(app)

    def heartbeat() -> None:
        """Ничего не делает — цель теста только регистрация задачи."""

    job = ext.add_job(app, heartbeat, "interval", seconds=5, job_id="heartbeat")
    assert job.id == "heartbeat"
    assert len(scheduler.get_jobs()) == 1
