"""PostgreSQL 连接参数只有一个构造入口，且密码只从文件读取。"""

import traceback
from pathlib import Path

import pytest

from xiaowei_agent.config import Settings
from xiaowei_agent.persistence import database
from xiaowei_agent.persistence.database import (
    DatabaseConfigurationError,
    create_database_engine,
    database_url,
    read_postgres_password,
)


def test_engine_factory_uses_structured_url_and_all_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "postgres-password"
    fake_db_secret = "fake-database-" + "password"
    secret.write_text(fake_db_secret + "\n", encoding="utf-8")
    settings = Settings(
        environment_id="dev",
        postgres_host="db.internal",
        postgres_port=5544,
        postgres_database="agentdb",
        postgres_user="agentuser",
        postgres_password_file=str(secret),
        db_connect_timeout_seconds=3,
        db_command_timeout_seconds=12,
        db_pool_size=7,
        db_pool_max_overflow=0,
    )
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create(url: object, **kwargs: object) -> object:
        captured.update(url=url, **kwargs)
        return sentinel

    monkeypatch.setattr(database, "create_async_engine", fake_create)
    assert create_database_engine(settings) is sentinel
    url = database_url(settings)
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "db.internal"
    assert url.port == 5544
    assert url.database == "agentdb"
    assert url.username == "agentuser"
    assert url.password == fake_db_secret
    assert fake_db_secret not in str(url)
    assert captured == {
        "url": url,
        "pool_size": 7,
        "max_overflow": 0,
        "pool_pre_ping": True,
        "connect_args": {"timeout": 3.0, "command_timeout": 12.0},
    }


@pytest.mark.parametrize("payload", ["", "line-one\nline-two", "x" * 4097])
def test_password_file_shape_is_bounded(payload: str, tmp_path: Path) -> None:
    secret = tmp_path / "postgres-password"
    secret.write_text(payload, encoding="utf-8")
    settings = Settings(environment_id="dev", postgres_password_file=str(secret))
    with pytest.raises(DatabaseConfigurationError):
        read_postgres_password(settings)


def test_password_file_failure_never_exposes_path_or_secret(tmp_path: Path) -> None:
    private_path = tmp_path / ("private-" + "password-file")
    settings = Settings(environment_id="dev", postgres_password_file=str(private_path))
    try:
        read_postgres_password(settings)
    except DatabaseConfigurationError as exc:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        assert str(private_path) not in rendered
        assert exc.__cause__ is None
        assert exc.__context__ is None
    else:
        pytest.fail("expected ConfigError")
