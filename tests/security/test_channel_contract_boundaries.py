"""M7 渠道契约不得携带执行权或原始结果旁路。"""

import pytest

from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    FeishuProjectionInput,
    StoredTaskPage,
    StoredTaskRead,
)

pytestmark = pytest.mark.security


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
