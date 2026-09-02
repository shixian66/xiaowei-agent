"""全局 fixture。

不定义自定义的 socket 放行 fixture；未来需要放行时使用 pytest-socket
官方提供的 ``socket_enabled``。
"""

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from xiaowei_agent.contracts import (
        AdmissionCertificate,
        ExecutionPlan,
        RequestContext,
        ToolCall,
    )
    from xiaowei_agent.tools.fake import RecordingToolAdapter
    from xiaowei_agent.tools.gateway import DeterministicToolGateway

_PREFIX = "XIAOWEI_"


@pytest.fixture(autouse=True)
def clean_xiaowei_env() -> Iterator[None]:
    """每个用例前清除全部 XIAOWEI_* 变量，结束后原样恢复。"""
    saved = {k: v for k, v in os.environ.items() if k.upper().startswith(_PREFIX)}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.upper().startswith(_PREFIX)]:
            del os.environ[k]
        os.environ.update(saved)


@pytest.fixture
def two_step_plan() -> "ExecutionPlan":
    """两步计划；用于证明 ordered_steps 的顺序进入 plan_hash。"""
    from tests.fakes.fixtures import FIXTURE_PLAN

    first = FIXTURE_PLAN.steps[0]
    second = first.model_copy(update={"step_id": "s2", "depends_on": ("s1",)})
    return FIXTURE_PLAN.model_copy(update={"steps": (first, second)})


@pytest.fixture
def context() -> "RequestContext":
    from xiaowei_agent.contracts import RequestContext

    return RequestContext(
        tenant_id="dev-local",
        actor="alice",
        environment_id="dev",
        trace_id="0" * 32,
        policy_revision="policy-2026-09-01",
    )


@pytest.fixture
def ok_call() -> "ToolCall":
    from tests.fakes.fixtures import FIXTURE_TOOL_CALL

    return FIXTURE_TOOL_CALL


@pytest.fixture
def recording_adapter() -> "RecordingToolAdapter":
    from xiaowei_agent.contracts import AdapterStatus
    from xiaowei_agent.tools.adapter import AdapterResponse
    from xiaowei_agent.tools.fake import RecordingToolAdapter

    return RecordingToolAdapter(
        responses=(
            AdapterResponse(
                status=AdapterStatus.OK,
                payload=({"query_id": "q1"},),
                source="starrocks-fake",
                error=None,
                elapsed_ms=3,
            ),
        )
    )


@pytest.fixture
def gateway(recording_adapter: "RecordingToolAdapter") -> "DeterministicToolGateway":
    from xiaowei_agent.tools.gateway import DeterministicToolGateway

    return DeterministicToolGateway(adapters={"starrocks": recording_adapter})


def make_certificate(call: "ToolCall", **overrides: object) -> "AdmissionCertificate":
    """构造与给定调用匹配的准入凭证。

    默认路径是"合法"的；测试通过 overrides 制造各种不匹配，因此这个工厂本身
    不能有任何"聪明"的补偿逻辑。
    """
    from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET

    from xiaowei_agent.contracts import (
        AdmissionCertificate,
        EffectClass,
        PolicyDecision,
        RiskLevel,
    )
    from xiaowei_agent.planning import (
        compute_plan_hash,
        compute_target_fingerprint,
        compute_tool_call_hash,
    )

    base: dict[str, object] = {
        "step_id": call.step_id,
        "operation": call.operation,
        "effect_class": EffectClass.READ,
        "policy_decision": PolicyDecision(
            allow=True,
            reason_code="readonly.allowed",
            risk=RiskLevel.LOW,
            policy_revision="policy-2026-09-01",
            obligations=(),
        ),
        "approval_ref": None,
        "plan_hash": compute_plan_hash(FIXTURE_PLAN),
        "target_fingerprint": compute_target_fingerprint(FIXTURE_TARGET),
        "tool_call_hash": compute_tool_call_hash(call),
    }
    return AdmissionCertificate(**(base | overrides))


@pytest.fixture
def admission(ok_call: "ToolCall") -> "AdmissionCertificate":
    return make_certificate(ok_call)
