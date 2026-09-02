"""DeterministicStepRunner：生命周期宿主。

Runner 拥有租约、CAS 推进与暂停，但**不拥有领域安全规则**：分类、策略、SQL 与
审批一律经 ``admit_step``；证据一律经 ledger。
"""

import pytest
from tests.fakes.recordings import (
    EMPTY_IN_SCOPE_BUT_TRAFFIC_ELSEWHERE,
    EMPTY_WITH_TRAFFIC,
    GOLDEN,
    MALFORMED,
    TIMEOUT,
)
from tests.fakes.runner import RunnerHarness

from xiaowei_agent.capabilities.specs import OP_COUNT, OP_LIST
from xiaowei_agent.contracts import TaskStatus, ToolCallStatus


async def test_golden_run_executes_only_the_first_step() -> None:
    """s1 有命中时，可选分支不执行——预算内的分支不是"总是跑"。"""
    harness = RunnerHarness(GOLDEN)
    outcome = await harness.start()
    assert [call.operation for call in harness.adapter.calls] == [OP_LIST]
    assert outcome.status is TaskStatus.SUCCEEDED
    assert len(outcome.evidence_refs) == 1


@pytest.mark.parametrize(
    ("recording", "s2_runs"),
    [(GOLDEN, False), (EMPTY_WITH_TRAFFIC, True)],
    ids=["s1_has_rows", "s1_empty"],
)
async def test_optional_branch_runs_only_when_s1_returns_no_rows(
    recording: object, s2_runs: bool
) -> None:
    harness = RunnerHarness(recording)
    await harness.start()
    operations = [call.operation for call in harness.adapter.calls]
    assert (OP_COUNT in operations) is s2_runs


async def test_condition_is_evaluated_from_the_ledger_not_local_state() -> None:
    """条件求值必须读 ledger。

    反例更强：把 ledger 换成永远返回空的实现，s2 必须**被执行**（因为 s1 的行数
    读回来是 0）。若 Runner 用的是本地变量，s2 会被跳过，这条就转红。
    """
    harness = RunnerHarness(GOLDEN, blind_ledger=True)
    await harness.start()
    assert OP_COUNT in [call.operation for call in harness.adapter.calls]


async def test_every_evidence_ref_in_the_outcome_resolves_in_the_ledger() -> None:
    harness = RunnerHarness(EMPTY_WITH_TRAFFIC)
    outcome = await harness.start()
    assert outcome.evidence_refs
    for ref in outcome.evidence_refs:
        await harness.ledger.get(task_id=harness.task_id, evidence_id=ref)


async def test_evidence_is_written_for_every_executed_step() -> None:
    harness = RunnerHarness(EMPTY_WITH_TRAFFIC)
    await harness.start()
    stored = await harness.ledger.load(task_id=harness.task_id)
    assert [e.evidence_id for e in stored] == [
        f"{harness.task_id}:s1",
        f"{harness.task_id}:s2",
    ]


async def test_runner_leaves_the_terminal_decision_to_the_runtime() -> None:
    """Runner 不写终态。

    终态由 Runtime 依 Answerability 的结构化结论确定性判定（ARCHITECTURE §4.3）。
    Runner 若先写 SUCCEEDED，终态保护会让 Runtime 再也无法降级为 indeterminate——
    "空证据不得渲染成成功"就在存储层被堵死了。
    """
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    assert (await harness.store.get(harness.task_id)).status is TaskStatus.RUNNING


async def test_plan_and_target_are_saved_before_execution() -> None:
    """resume 要重算指纹，就必须先拿得回当初那份计划。"""
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    stored = await harness.plan_store.load(task_id=harness.task_id)
    assert stored.plan == harness.plan
    assert stored.target == harness.target


async def test_budget_exhaustion_produces_a_structured_failure() -> None:
    harness = RunnerHarness(EMPTY_WITH_TRAFFIC, max_tool_calls=1)
    outcome = await harness.start()
    assert outcome.status is TaskStatus.FAILED
    assert outcome.terminal_reason == "budget.tool_calls_exhausted"
    assert len(harness.adapter.calls) == 1


@pytest.mark.parametrize(
    "recording", [TIMEOUT, MALFORMED], ids=["timeout", "malformed"]
)
async def test_tool_failures_never_look_like_empty_success(recording: object) -> None:
    """失败不得表现为"取到零行"，也不得触发可选分支。

    EVIDENCE_ROW_COUNT_BELOW 在数值上无法区分"取到零行"与"根本没取到"，而这两者
    含义相反。s1 失败后仍去跑 s2，会让一次取数故障看起来像是在"确认范围内有没有
    流量"——正是本闭环要避免的、自信但错误的结论。
    """
    harness = RunnerHarness(recording)
    outcome = await harness.start()
    assert outcome.status is TaskStatus.INDETERMINATE
    assert [call.operation for call in harness.adapter.calls] == [OP_LIST]
    for envelope in await harness.ledger.load(task_id=harness.task_id):
        assert envelope.facts == ()


async def test_empty_scope_recording_still_produces_evidence() -> None:
    """B1 语料：范围内无审计数据时，两步都执行且都留下证据。"""
    harness = RunnerHarness(EMPTY_IN_SCOPE_BUT_TRAFFIC_ELSEWHERE, database="sales")
    outcome = await harness.start()
    assert [call.operation for call in harness.adapter.calls] == [OP_LIST, OP_COUNT]
    assert len(outcome.evidence_refs) == 2


async def test_repeated_runs_issue_identical_tool_calls() -> None:
    """固定输入 → 逐字节相同的 ToolCall 与 tool_call_hash。"""
    from xiaowei_agent.planning import compute_tool_call_hash

    first = RunnerHarness(GOLDEN)
    second = RunnerHarness(GOLDEN)
    await first.start()
    await second.start()
    assert [compute_tool_call_hash(c) for c in first.adapter.calls] == [
        compute_tool_call_hash(c) for c in second.adapter.calls
    ]


async def test_every_transition_carries_expected_version_and_token() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    assert harness.store.transitions
    for command in harness.store.transitions:
        assert command["expected_version"] is not None
        assert command["fencing_token"] is not None


async def test_runner_adopts_the_store_winner_on_cas_failure() -> None:
    """CAS 失败是正常并发结果：必须采纳 winner，不得用本地旧对象继续。"""
    harness = RunnerHarness(GOLDEN)
    await harness.ensure_task()
    await harness.store.transition(
        task_id=harness.task_id,
        expected_version=(await harness.store.get(harness.task_id)).version,
        to_status=TaskStatus.CANCELED,
    )
    with pytest.raises(RuntimeError):
        await harness.start()
    assert harness.adapter.call_count == 0


async def test_rejected_transition_stops_the_run_with_zero_calls() -> None:
    """CAS 被拒时必须停下，不得用本地旧对象继续推进。

    这条与"终态任务拿不到租约"是**两回事**：这里租约拿得到（任务并非终态），
    被拒的是状态迁移本身。少了这条用例，撤掉 ``result.applied`` 检查的变异会
    全绿——租约那条先挡住了，``applied`` 从未单独承重。
    """
    harness = RunnerHarness(GOLDEN)
    await harness.ensure_task()
    # 推到 AWAITING_APPROVAL：非终态（租约可取），但 → PLANNING 不是合法迁移。
    record = await harness.store.get(harness.task_id)
    for status in (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.AWAITING_APPROVAL):
        result = await harness.store.transition(
            task_id=harness.task_id,
            expected_version=record.version,
            to_status=status,
        )
        assert result.applied
        record = result.winner

    with pytest.raises(RuntimeError):
        await harness.start()
    assert harness.adapter.call_count == 0
    assert harness.gateway.invocations == 0


async def test_terminal_task_cannot_be_advanced() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.drive_to(TaskStatus.CANCELED)
    with pytest.raises(RuntimeError):
        await harness.start()
    assert harness.adapter.call_count == 0


async def test_a_task_held_by_another_worker_is_not_started() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.ensure_task()
    grant = await harness.store.acquire_lease(
        task_id=harness.task_id, owner="other-worker", ttl_seconds=60
    )
    assert grant is not None
    with pytest.raises(RuntimeError):
        await harness.start()
    assert harness.adapter.call_count == 0


async def test_gateway_results_reach_evidence_unchanged_in_shape() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    stored = await harness.ledger.load(task_id=harness.task_id)
    assert len(stored[0].facts) == 3
    assert harness.gateway.results[0].status is ToolCallStatus.OK
