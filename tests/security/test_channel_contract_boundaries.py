"""M7 渠道契约不得携带执行权或原始结果旁路。"""

import ast
from pathlib import Path

import pytest

from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    FeishuProjectionInput,
    StoredTaskPage,
    StoredTaskRead,
)

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_CHANNEL = _ROOT / "src" / "xiaowei_agent" / "contracts" / "channel.py"


def test_principal_surface_cannot_carry_role_policy_or_credentials() -> None:
    fields = set(AuthenticatedPrincipal.model_fields)

    assert fields == {
        "tenant_id",
        "environment_id",
        "actor",
        "source",
        "subject_ref",
        "permissions",
    }
    assert not ({"role", "policy_revision", "token", "secret", "password"} & fields)


def test_projection_input_cannot_carry_evidence_or_database_rows() -> None:
    fields = set(FeishuProjectionInput.model_fields)

    assert fields == {"task_view", "request_preview", "task_version", "detail_url"}
    assert not (
        {
            "evidence",
            "evidence_facts",
            "raw_rows",
            "sql",
            "promql",
            "adapter_payload",
            "capability_id",
        }
        & fields
    )


def test_stored_task_page_is_a_read_contract_not_an_acl_shortcut() -> None:
    assert set(StoredTaskRead.model_fields) == {"record", "submission"}
    assert set(StoredTaskPage.model_fields) == {"items", "next_created_seq"}


def test_channel_contract_module_has_no_execution_layer_imports() -> None:
    tree = ast.parse(_CHANNEL.read_text(encoding="utf-8"))
    forbidden = {
        "xiaowei_agent.application",
        "xiaowei_agent.capabilities",
        "xiaowei_agent.evidence",
        "xiaowei_agent.governance",
        "xiaowei_agent.persistence",
        "xiaowei_agent.planning",
        "xiaowei_agent.reflection",
        "xiaowei_agent.runners",
        "xiaowei_agent.tools",
    }
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not (imported & forbidden)
