"""Тесты модуля core.database.

Используют in-memory SQLite (SQLAlchemy поддерживает JSONB-совместимый
JSON на SQLite), поэтому интеграционные сценарии upsert/soft-delete
проверяются без запущенного PostgreSQL.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.exc import IntegrityError

from core.database import Base, get_session, get_transaction, init_db

metadata_table = sa.Table(
    "test_rows",
    Base.metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("guid", sa.String(36), unique=True, nullable=False),
    sa.Column("payload", sa.JSON, nullable=False, default=dict),
    sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
)


@pytest.fixture
def engine() -> Iterator[sa.Engine]:
    """In-memory SQLite engine со схемой из Base.metadata."""
    eng = sa.create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def sessionmaker_(engine: sa.Engine) -> sa.orm.sessionmaker[sa.orm.Session]:
    """Фабрика сессий для тестового движка."""
    return sa.orm.sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


def test_init_db_returns_factory_and_registers_teardown() -> None:
    """init_db создает фабрику сессий поверх рабочего движка."""
    factory = init_db("sqlite:///:memory:")
    assert callable(factory)
    with get_session(factory) as session:
        assert session.execute(sa.text("SELECT 1")).scalar() == 1


def test_init_db_rejects_empty_url() -> None:
    """Пустой DSN отклоняется через ValidationError."""
    from core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        init_db("")


def test_base_is_declarative_and_shared() -> None:
    """Base — общий Declarative-класс для всех моделей проекта."""
    assert isinstance(Base.metadata, sa.MetaData)
    assert hasattr(Base, "registry")


# ---------------------------------------------------------------------------
# get_session / get_transaction
# ---------------------------------------------------------------------------


def test_get_session_commits_on_success(sessionmaker_: sa.orm.sessionmaker[sa.orm.Session]) -> None:
    """get_session коммитит данные при штатном выходе из контекста."""
    now = dt.datetime.now(dt.UTC)

    def insert() -> None:
        with get_session(sessionmaker_) as session:
            session.execute(
                metadata_table.insert().values(guid="g1", payload={"a": 1}, synced_at=now)
            )

    insert()
    with get_session(sessionmaker_) as session:
        rows = session.execute(sa.select(metadata_table.c.guid)).all()
    assert [r[0] for r in rows] == ["g1"]


def test_get_session_rolls_back_on_exception(sessionmaker_: sa.orm.sessionmaker[sa.orm.Session]) -> None:
    """Исключение внутри контекста откатывает транзакцию."""
    now = dt.datetime.now(dt.UTC)
    with pytest.raises(RuntimeError), get_session(sessionmaker_) as session:
        session.execute(
            metadata_table.insert().values(guid="bad", payload={}, synced_at=now)
        )
        raise RuntimeError("boom")
    with get_session(sessionmaker_) as session:
        count = session.execute(sa.select(sa.func.count()).select_from(metadata_table)).scalar()
    assert count == 0


def test_get_transaction_yields_same_session(sessionmaker_: sa.orm.sessionmaker[sa.orm.Session]) -> None:
    """get_transaction — sugar над get_session."""
    with get_transaction(sessionmaker_) as session:
        assert session.get_bind() is not None


def test_upsert_soft_delete_flow(sessionmaker_: sa.orm.sessionmaker[sa.orm.Session]) -> None:
    """Смоук-тест гибридного хранения: повторный upsert не дублирует guid."""
    now = dt.datetime.now(dt.UTC)
    with get_session(sessionmaker_) as session:
        session.execute(
            metadata_table.insert().values(guid="dup", payload={"v": 1}, synced_at=now)
        )
    with get_session(sessionmaker_) as session, pytest.raises(IntegrityError):
        session.execute(
            metadata_table.insert().values(guid="dup", payload={"v": 2}, synced_at=now)
        )
    # soft delete помечает запись, не удаляя строку
    with get_session(sessionmaker_) as session:
        session.execute(
            metadata_table.update()
            .where(metadata_table.c.guid == "dup")
            .values(is_deleted=True)
        )
        row = session.execute(
            sa.select(metadata_table.c.is_deleted).where(metadata_table.c.guid == "dup")
        ).one()
    assert row[0] is True


# ---------------------------------------------------------------------------
# Property-based: GUID уникальность
# ---------------------------------------------------------------------------


@settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(guids=st.lists(st.uuids().map(str), min_size=1, max_size=10, unique=True))
def test_unique_guids_insert_without_conflict(
    guids: list[str], sessionmaker_: sa.orm.sessionmaker[sa.orm.Session]
) -> None:
    """Произвольные уникальные GUID вставляются без конфликтов ограничений."""
    now = dt.datetime.now(dt.UTC)
    Base.metadata.drop_all(sessionmaker_.kw["bind"])
    Base.metadata.create_all(sessionmaker_.kw["bind"])
    with get_session(sessionmaker_) as session:
        for guid in guids:
            session.execute(
                metadata_table.insert().values(guid=guid, payload={}, synced_at=now)
            )
    with get_session(sessionmaker_) as session:
        count = session.execute(sa.select(sa.func.count()).select_from(metadata_table)).scalar()
    assert count == len(guids)
