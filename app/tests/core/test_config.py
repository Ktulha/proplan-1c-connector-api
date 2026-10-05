"""Тесты модуля core.config.

Проверяют загрузку конфигурации из переменных окружения (и ``.env``),
валидацию значений, секреты-файлы (*_FILE) и фабрика окружений.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from core.config import (
    Config,
    DevelopmentConfig,
    ProductionConfig,
    get_config,
    load_config,
)

VALID_KEY = base64.b64encode(b"0" * 32).decode()


def make_env(**overrides: str | None) -> dict[str, str]:
    """Собрать словарь окружения с обязательными значениями по умолчанию.

    Args:
        **overrides: Переопределяемые переменные; ``None`` удаляет переменную.

    Returns:
        Итоговый словарь окружения.
    """
    env = {
        "FLASK_ENV": "development",
        "DATABASE_URL": "postgresql+psycopg://user:pass@localhost:5432/connector1c",
        "ENCRYPTION_KEY": VALID_KEY,
    }
    for name, value in overrides.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


# ---------------------------------------------------------------------------
# Успешная загрузка
# ---------------------------------------------------------------------------


def test_load_config_reads_all_fields() -> None:
    """Все поля заполняются из явного окружения без .env."""
    cfg = load_config(
        env={
            "FLASK_ENV": "production",
            "DATABASE_URL": "postgresql+psycopg://u:p@h/db",
            "ENCRYPTION_KEY": VALID_KEY,
            "APP_SECRET": "s" * 32,
            "API_PREFIX": "/api/v2",
            "LOG_LEVEL": "warning",
            "ADMIN_URL": "/panel",
            "SYNC_TIMEZONE": "Europe/Moscow",
            "MAX_WORKERS": "8",
            "HTTP_TIMEOUT": "15",
            "ALLOWED_ORIGINS": "http://a.local, http://b.local",
        }
    )
    assert isinstance(cfg, Config)
    assert cfg.env == "production"
    assert cfg.database_url == "postgresql+psycopg://u:p@h/db"
    assert isinstance(cfg.encryption_key, bytes)
    assert len(cfg.encryption_key) == 32
    assert cfg.log_level == "WARNING"  # нормализация регистра
    assert cfg.api_prefix == "/api/v2"
    assert cfg.admin_url == "/panel"
    assert cfg.max_workers == 8
    assert cfg.http_timeout == 15.0
    assert cfg.allowed_origins == ["http://a.local", "http://b.local"]


def test_defaults_when_optional_missing() -> None:
    """Опциональные переменные получают безопасные значения по умолчанию."""
    cfg = load_config(env=make_env())
    assert cfg.app_secret == cfg.encryption_key.hex()  # fallback на ключ шифрования
    assert cfg.api_prefix == "/api/v1"
    assert cfg.log_level == "INFO"
    assert cfg.max_workers == 4
    assert cfg.http_timeout == 30.0
    assert cfg.allowed_origins == []


def test_dotenv_file_is_loaded(tmp_path: Path) -> None:
    """Значения из .env подхватываются и не перекрывают реальные env-переменные."""
    directory = tmp_path
    dotenv = directory / ".env"
    dotenv.write_text("APP_SECRET=from-dotenv\nMAX_WORKERS=9\n", encoding="utf-8")
    cfg = load_config(
        env={"APP_SECRET": "from-real-env", **make_env()},
        dotenv_path=directory,
    )
    # os.environ приоритетнее .env
    assert cfg.app_secret == "from-real-env"
    # значение из .env применено
    assert cfg.max_workers == 9


def test_secret_file_support(tmp_path: Path) -> None:
    """ENCRYPTION_KEY_FILE читает ключ из файла (Docker secrets)."""
    directory = tmp_path
    keyfile = directory / "enc.key"
    keyfile.write_bytes(base64.b64encode(b"k" * 32))
    cfg = load_config(
        env=make_env(ENCRYPTION_KEY=None, ENCRYPTION_KEY_FILE=str(keyfile)),
    )
    assert cfg.encryption_key == b"k" * 32


# ---------------------------------------------------------------------------
# Валидация и ошибки
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["DATABASE_URL", "ENCRYPTION_KEY"])
def test_missing_required_raises(missing: str) -> None:
    """Отсутствие обязательной переменной — ValidationError с именем переменной."""
    from core.exceptions import ValidationError

    env = make_env()
    env.pop(missing)
    env.pop("ENCRYPTION_KEY_FILE", None)
    with pytest.raises(ValidationError) as excinfo:
        load_config(env=env)
    assert missing in str(excinfo.value)


def test_short_encryption_key_rejected() -> None:
    """Ключ менее 32 байт отклоняется (требование AES-256)."""
    from core.exceptions import ValidationError

    short = base64.b64encode(b"x" * 16).decode()
    with pytest.raises(ValidationError):
        load_config(env=make_env(ENCRYPTION_KEY=short))


def test_non_base64_encryption_key_rejected() -> None:
    """Не-base64 строка в ENCRYPTION_KEY отклоняется."""
    from core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        load_config(env=make_env(ENCRYPTION_KEY="!!!not base64!!!"))


def test_invalid_number_rejected() -> None:
    """Некорректное числовое значение MAX_WORKERS отклоняется."""
    from core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        load_config(env=make_env(MAX_WORKERS="abc"))


def test_unknown_flask_env_rejected() -> None:
    """Неизвестное FLASK_ENV отклоняется фабрикой get_config."""
    from core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        get_config(env=make_env(FLASK_ENV="staging"))


# ---------------------------------------------------------------------------
# Фабрика окружений
# ---------------------------------------------------------------------------


def test_get_config_selects_class_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_config возвращает класс, соответствующий FLASK_ENV."""
    for name, value in make_env(FLASK_ENV="production").items():
        monkeypatch.setenv(name, value)
    cfg = get_config()
    assert isinstance(cfg, ProductionConfig)
    assert cfg.debug is False
    assert cfg.testing is False

    monkeypatch.setenv("FLASK_ENV", "development")
    assert isinstance(get_config(), DevelopmentConfig)


def test_production_requires_strong_values() -> None:
    """Production-конфигг наследует общую валидацию."""
    cfg = load_config(env=make_env(FLASK_ENV="production"))
    assert isinstance(cfg, ProductionConfig)
    assert cfg.debug is False


# ---------------------------------------------------------------------------
# Property-based: устойчивость к произвольным значениям
# ---------------------------------------------------------------------------


@settings(max_examples=40)
@given(
    log_level=st.sampled_from(["debug", "INFO", "Warning", "ERROR", "CRITICAL"]),
    workers=st.integers(min_value=1, max_value=64),
    timeout=st.floats(min_value=0.1, max_value=600.0, allow_nan=False),
    origins=st.lists(st.text(min_size=1, max_size=40).filter(lambda s: "," not in s), max_size=5),
)
@example(log_level="info", workers=1, timeout=0.5, origins=["http://x"])
def test_arbitrary_valid_values_parse(log_level: str, workers: int, timeout: float, origins: list[str]) -> None:
    """Любые допустимые значения парсятся без исключений и нормализуются."""
    cfg = load_config(
        env=make_env(
            LOG_LEVEL=log_level,
            MAX_WORKERS=str(workers),
            HTTP_TIMEOUT=str(timeout),
            ALLOWED_ORIGINS=",".join(origins),
        )
    )
    assert cfg.log_level == log_level.upper()
    assert cfg.max_workers == workers
    assert cfg.http_timeout == pytest.approx(timeout)
    assert len(cfg.allowed_origins) == len([o for o in origins if o.strip()])


def test_os_environ_used_when_env_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без явного env используется реальный процесс-окружение."""
    for name, value in make_env().items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TZ", raising=False)
    cfg = load_config()
    assert cfg.env == os.environ["FLASK_ENV"]
