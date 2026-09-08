"""M7 渠道表的迁移链、约束与活 schema 必须对齐。"""

import contextlib
import io
import re
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tests.contract.test_schema_matches_migration import _offline_upgrade_sql

from xiaowei_agent.persistence.schema import (
    CHANNEL_BINDINGS,
    PROJECTION_FENCING_SEQUENCE_NAME,
    PROJECTION_SUBSCRIPTIONS,
)


def test_rev_0006_has_the_expected_revision_chain() -> None:
    from xiaowei_agent.persistence.migrations.versions import rev_0006_channels

    assert rev_0006_channels.revision == "0006_channels"
    assert rev_0006_channels.down_revision == "0005_step_journal"


def test_channel_bindings_hold_only_source_identity_not_task_facts() -> None:
    assert set(CHANNEL_BINDINGS.columns.keys()) == {
        "binding_id",
        "task_id",
        "tenant_id",
        "environment_id",
        "channel",
        "initiator_subject_ref",
        "conversation_ref",
        "source_event_ref",
        "created_at",
    }
    assert not {
        "actor",
        "status",
        "version",
        "request_text",
        "request_digest",
        "render_payload",
    } & set(CHANNEL_BINDINGS.columns.keys())


def test_projection_subscriptions_hold_delivery_state_not_task_truth() -> None:
    assert set(PROJECTION_SUBSCRIPTIONS.columns.keys()) == {
        "subscription_id",
        "task_id",
        "destination_kind",
        "destination_ref",
        "source_message_ref",
        "state",
        "last_projected_task_version",
        "attempt_number",
        "claim_owner",
        "claim_expires_at",
        "fencing_token",
        "next_attempt_at",
        "provider_failure_count",
        "last_error_code",
        "payload_digest",
    }
    assert not {"task_status", "task_actor", "request_text", "render_payload"} & set(
        PROJECTION_SUBSCRIPTIONS.columns.keys()
    )


def test_channel_tables_have_closed_state_and_consistency_constraints() -> None:
    binding_constraints = {
        item.name for item in CHANNEL_BINDINGS.constraints if item.name is not None
    }
    subscription_constraints = {
        item.name
        for item in PROJECTION_SUBSCRIPTIONS.constraints
        if item.name is not None
    }
    assert binding_constraints >= {
        "uq_channel_bindings_task",
        "uq_channel_bindings_source",
        "ck_channel_bindings_channel",
        "ck_channel_bindings_group_conversation",
    }
    assert subscription_constraints >= {
        "uq_projection_subscriptions_destination",
        "ck_projection_subscriptions_destination_kind",
        "ck_projection_subscriptions_state",
        "ck_projection_subscriptions_claim_consistent",
        "ck_projection_subscriptions_terminal_unclaimed",
        "ck_projection_subscriptions_projection_consistent",
        "ck_projection_subscriptions_failure_consistent",
    }
    assert isinstance(PROJECTION_SUBSCRIPTIONS.c.fencing_token.type, sa.BigInteger)


def test_channel_migration_creates_sequence_and_due_scan_index() -> None:
    sql = _offline_upgrade_sql()
    assert f"CREATE SEQUENCE {PROJECTION_FENCING_SEQUENCE_NAME}" in sql
    assert re.search(
        r"CREATE INDEX ix_projection_subscriptions_due ON "
        r"projection_subscriptions \(state, next_attempt_at, subscription_id\)",
        sql,
    )


def test_channel_downgrade_drops_subscription_before_binding_and_sequence() -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(root / config.get_main_option("script_location", ""))
    )
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline/offline")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.downgrade(config, "0006_channels:0005_step_journal", sql=True)
    sql = buffer.getvalue()

    assert sql.index("DROP TABLE projection_subscriptions") < sql.index(
        "DROP TABLE channel_bindings"
    )
    assert f"DROP SEQUENCE {PROJECTION_FENCING_SEQUENCE_NAME}" in sql
