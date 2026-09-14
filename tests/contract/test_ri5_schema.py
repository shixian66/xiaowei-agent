"""RI5 三张新表与 `web_sessions` 两个新列的形状契约。"""

import sqlalchemy as sa

from xiaowei_agent.persistence import schema


def test_local_admins_allows_at_most_one_row() -> None:
    table = schema.LOCAL_ADMINS
    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
    assert any("id" in str(c.sqltext) for c in checks)
    assert [c.name for c in table.primary_key.columns] == ["id"]


def test_service_config_state_is_keyed_by_service_and_provider() -> None:
    assert [c.name for c in schema.SERVICE_CONFIG_STATE.primary_key.columns] == [
        "service_name",
        "provider",
    ]


def test_provider_test_state_is_keyed_by_check_name() -> None:
    assert [c.name for c in schema.PROVIDER_TEST_STATE.primary_key.columns] == ["check_name"]
    assert "duration_ms" in schema.PROVIDER_TEST_STATE.c


def test_web_sessions_carries_auth_source_and_origin_digest() -> None:
    columns = schema.WEB_SESSIONS.c
    assert "auth_source" in columns and not columns["auth_source"].nullable
    assert "public_origin_digest" in columns and not columns["public_origin_digest"].nullable


def test_no_new_column_was_added_to_web_oauth_states() -> None:
    """ADR-014 R3：解除 schema 冻结不包含 web_oauth_states。"""
    assert set(schema.WEB_OAUTH_STATES.c.keys()) == {
        "state_digest",
        "issued_at",
        "expires_at",
        "consumed_at",
    }


def test_new_tables_are_registered() -> None:
    names = {t.name for t in schema.ALL_TABLES}
    assert {"local_admins", "service_config_state", "provider_test_state"} <= names
