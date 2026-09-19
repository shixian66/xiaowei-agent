"""M7 渠道访问不能泄露任务存在性、原始提交或内部任务字段。"""

import ast
from pathlib import Path

import pytest
from tests.suites import channel_store as channel_store_suite

from xiaowei_agent.application.channel_access import (
    AccessibleTask,
    TaskAccessNotFoundError,
    TaskAccessQuery,
    TaskListQuery,
    TaskPage,
    TaskSummary,
)
from xiaowei_agent.application.channel_submission import ChannelSubmitCommand
from xiaowei_agent.persistence.channel import (
    ClaimedTaskLookup,
    ProjectionClaimNotFoundError,
    RenewProjectionClaimCommand,
)
from xiaowei_agent.persistence.fake import InMemoryChannelStore

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"


@pytest.fixture
def channel_store(clock, memory_state):
    return InMemoryChannelStore(clock=clock, state=memory_state)


async def test_projection_due_discovery_is_bound_to_the_worker_scope(
    channel_store, store, context, clock
) -> None:
    await channel_store_suite.test_due_scan_returns_only_the_requested_tenant_and_environment(
        channel_store, store, context, clock
    )


def test_safe_access_models_cannot_return_submission_or_internal_task_facts() -> None:
    accessible = set(AccessibleTask.model_fields)
    summary = set(TaskSummary.model_fields)
    page = set(TaskPage.model_fields)

    assert accessible == {
        "task_view",
        "request_preview",
        "submitted_at",
        "task_version",
        "clarification_parent_task_id",
    }
    assert summary == {"task_id", "status", "request_preview", "submitted_at"}
    assert page == {"items", "next_created_seq"}
    forbidden = {
        "submission",
        "request_envelope",
        "request_context",
        "idempotency_key",
        "request_digest",
        "lease_owner",
        "fencing_token",
        "raw_rows",
        "evidence",
    }
    assert not forbidden & (accessible | summary | page)
    assert set(TaskListQuery.model_fields) == {
        "principal",
        "before_created_seq",
        "limit",
    }
    assert set(TaskAccessQuery.model_fields) == {"principal", "task_id"}


def test_submit_command_cannot_override_authenticated_scope_or_server_key() -> None:
    fields = set(ChannelSubmitCommand.model_fields)

    assert fields == {
        "principal",
        "channel",
        "request_id",
        "trace_id",
        "policy_revision",
        "text",
        "client_submission_ref",
        "conversation_ref",
        "submitted_at",
        "clarification_parent_task_id",
    }
    assert not {
        "tenant_id",
        "environment_id",
        "actor",
        "permissions",
        "idempotency_key",
        "source_event_ref",
        "destination_ref",
    } & fields


def test_projection_worker_cannot_supply_the_task_scope_it_resolves() -> None:
    fields = set(ClaimedTaskLookup.model_fields)

    assert fields == {"subscription_id", "claim_owner", "fencing_token"}
    assert not {"task_id", "tenant_id", "environment_id", "actor"} & fields

    renewal_fields = set(RenewProjectionClaimCommand.model_fields)
    assert renewal_fields == {
        "subscription_id",
        "claim_owner",
        "fencing_token",
        "expected_state",
        "ttl_seconds",
    }
    assert not {"task_id", "tenant_id", "environment_id", "actor"} & renewal_fields


def test_not_found_error_has_one_constant_message_and_no_identifier_slot() -> None:
    first = TaskAccessNotFoundError()
    second = TaskAccessNotFoundError()

    assert str(first) == str(second) == "task not found"
    assert vars(first) == vars(second) == {}

    claim = ProjectionClaimNotFoundError()
    assert str(claim) == "projection claim not found"
    assert vars(claim) == {}


def test_channel_application_modules_do_not_import_execution_authority() -> None:
    forbidden_roots = {
        "xiaowei_agent.governance",
        "xiaowei_agent.runners",
        "xiaowei_agent.tools",
        "xiaowei_agent.planning.compiler",
    }
    for name in ("channel_access.py", "channel_submission.py"):
        tree = ast.parse((_SRC / "application" / name).read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not {
            imported
            for imported in imports
            if any(
                imported == root or imported.startswith(f"{root}.")
                for root in forbidden_roots
            )
        }, name
