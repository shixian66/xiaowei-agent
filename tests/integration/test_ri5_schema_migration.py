"""RI5 三张新表在真实 PostgreSQL 上的 CHECK 行为。

这些不变量只有数据库能真正执行：内存 fake 里"最多一行"和"通过即无错误码"都是
代码自觉，只有 CHECK 约束能让写入本身失败。
"""

import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_NOW = dt.datetime(2026, 9, 14, 9, 0, tzinfo=dt.UTC)


async def test_the_three_new_tables_exist_at_head(clean_database: AsyncEngine) -> None:
    async with clean_database.connect() as connection:
        names = set(
            (
                await connection.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = 'public'"
                    )
                )
            ).scalars()
        )
    assert {"local_admins", "service_config_state", "provider_test_state"} <= names


async def test_local_admins_rejects_a_second_row(clean_database: AsyncEngine) -> None:
    async with clean_database.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO local_admins (id, password_hash, must_change_password,"
                " updated_at) VALUES (1, 'scrypt$1$x', true, :now)"
            ),
            {"now": _NOW},
        )
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO local_admins (id, password_hash,"
                    " must_change_password, updated_at)"
                    " VALUES (2, 'scrypt$1$y', true, :now)"
                ),
                {"now": _NOW},
            )


async def test_provider_test_state_rejects_an_unknown_check_name(
    clean_database: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO provider_test_state (check_name, tested_generation,"
                    " test_status, tested_at, duration_ms)"
                    " VALUES ('made_up_check', 1, 'passed', :now, 5)"
                ),
                {"now": _NOW},
            )


async def test_a_passing_test_cannot_carry_an_error_code(
    clean_database: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO provider_test_state (check_name, tested_generation,"
                    " test_status, tested_at, duration_ms, error_code)"
                    " VALUES ('gemini_connection', 1, 'passed', :now, 5, 'boom')"
                ),
                {"now": _NOW},
            )


async def test_a_failing_test_must_carry_an_error_code(
    clean_database: AsyncEngine,
) -> None:
    """反例方向：反过来漏掉错误码同样必须被拒，否则等值约束只挡住一半。"""
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO provider_test_state (check_name, tested_generation,"
                    " test_status, tested_at, duration_ms)"
                    " VALUES ('gemini_connection', 1, 'failed', :now, 5)"
                ),
                {"now": _NOW},
            )


async def test_service_config_state_rejects_a_zero_generation(
    clean_database: AsyncEngine,
) -> None:
    """``loaded_generation > 0`` 是"不写伪造代次"这条规则的数据库侧执行点。"""
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO service_config_state (service_name, provider,"
                    " loaded_generation, load_status, loaded_at)"
                    " VALUES ('worker', 'gemini', 0, 'loaded', :now)"
                ),
                {"now": _NOW},
            )


async def test_service_config_state_rejects_an_unknown_load_status(
    clean_database: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO service_config_state (service_name, provider,"
                    " loaded_generation, load_status, loaded_at)"
                    " VALUES ('worker', 'gemini', 1, 'load_failed', :now)"
                ),
                {"now": _NOW},
            )


async def test_web_sessions_rejects_an_unknown_auth_source(
    clean_database: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        async with clean_database.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO web_sessions (session_digest, subject_ref, issued_at,"
                    " expires_at, auth_source, public_origin_digest)"
                    " VALUES (:digest, 'subject-alice', :now, :later, 'saml', :origin)"
                ),
                {
                    "digest": "a" * 64,
                    "now": _NOW,
                    "later": _NOW + dt.timedelta(hours=1),
                    "origin": "b" * 64,
                },
            )
