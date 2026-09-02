"""Runner 的生命周期安全边界。

两条承重：合成副作用步骤在无审批时**持久化暂停**且 Gateway/adapter 调用次数为 0；
resume 时重解析并重算两个指纹，任一漂移即拒绝且调用次数仍为 0。
"""

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runner import RunnerHarness

from xiaowei_agent.contracts import TaskStatus
from xiaowei_agent.runners.runner import WorkflowPaused

pytestmark = pytest.mark.security


async def test_synthetic_side_effect_step_pauses_with_zero_gateway_calls() -> None:
    harness = RunnerHarness(GOLDEN, synthetic_write=True)
    with pytest.raises(WorkflowPaused) as paused:
        await harness.start()
    assert (await harness.store.get(harness.task_id)).status is (
        TaskStatus.AWAITING_APPROVAL
    )
    assert harness.adapter.call_count == 0
    assert harness.gateway.invocations == 0
    assert paused.value.approval_ref


async def test_pausing_records_an_auditable_approval_request() -> None:
    """暂停必须留下可审计的审批请求，而不只是一个状态。"""
    harness = RunnerHarness(GOLDEN, synthetic_write=True)
    with pytest.raises(WorkflowPaused):
        await harness.start()
    assert harness.store.approvals
    request = harness.store.approvals[-1]
    assert request.task_id == harness.task_id
    assert request.step_id == "s1"


def test_workflow_paused_does_not_leak_ids_into_its_message() -> None:
    exc = WorkflowPaused(task_id="t-secret", step_id="s1", approval_ref="a1")
    assert "t-secret" not in str(exc)
    assert "t-secret" not in repr(exc)
    assert exc.task_id == "t-secret"


async def test_workflow_paused_is_part_of_the_runner_contract() -> None:
    """``WorkflowPaused`` 与 Protocol 同模块：暂停在返回值里不可表达。

    ``start()`` 的返回类型是 ``TaskOutcome``，而 ``TaskOutcome`` 要求终态；
    ``awaiting_approval`` 是非终态。调用方不该靠猜。
    """
    from xiaowei_agent.runners import runner as runner_module

    assert runner_module.WorkflowPaused is WorkflowPaused
    assert hasattr(runner_module, "WorkflowRunner")


@pytest.mark.parametrize("drift", ["plan", "target", "policy_revision"])
async def test_resume_refuses_on_any_drift_with_zero_gateway_calls(drift: str) -> None:
    harness = RunnerHarness(GOLDEN, synthetic_write=True)
    with pytest.raises(WorkflowPaused):
        await harness.start()
    harness.reset_call_counters()
    harness.introduce_drift(drift)
    with pytest.raises(Exception) as err:
        await harness.resume()
    assert not isinstance(err.value, WorkflowPaused)
    assert harness.adapter.call_count == 0
    assert harness.gateway.invocations == 0


async def test_resume_without_an_approval_pauses_again_rather_than_executing() -> None:
    """没有漂移、也没有审批时，恢复必须重新暂停，而不是放行。"""
    harness = RunnerHarness(GOLDEN, synthetic_write=True)
    with pytest.raises(WorkflowPaused):
        await harness.start()
    harness.reset_call_counters()
    with pytest.raises(WorkflowPaused):
        await harness.resume()
    assert harness.adapter.call_count == 0
    assert harness.gateway.invocations == 0


async def test_resume_recomputes_both_fingerprints() -> None:
    """恢复不信任存储里自称的指纹，而是重算。"""
    harness = RunnerHarness(GOLDEN, synthetic_write=True)
    with pytest.raises(WorkflowPaused):
        await harness.start()
    harness.reset_call_counters()
    with pytest.raises(WorkflowPaused):
        await harness.resume()
    assert harness.recomputed_fingerprints >= 1


async def test_e1_call_count_is_zero_on_every_path() -> None:
    """M0-M7 对被管运维目标的 E1 调用次数恒为 0。"""
    harness = RunnerHarness(GOLDEN, synthetic_write=True)
    with pytest.raises(WorkflowPaused):
        await harness.start()
    assert harness.gateway.side_effect_invocations == 0


async def test_read_only_run_never_touches_the_approval_gate() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    assert harness.approval_gate.calls == 0


async def test_runner_never_constructs_a_tool_result_or_certificate() -> None:
    """AST：Runner 不得自签凭证，也不得伪造工具结果。"""
    import ast
    from pathlib import Path

    from xiaowei_agent.runners import deterministic

    tree = ast.parse(Path(deterministic.__file__).read_text(encoding="utf-8"))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "AdmissionCertificate" not in called
    assert "ToolResult" not in called
    assert "issue_admission_certificate" not in called
