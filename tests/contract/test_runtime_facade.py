"""XiaoweiRuntime：应用层唯一编排入口。

Runtime 组装依赖、驱动 Runner、按引用读回证据、依 Answerability 确定性判定终态、
写 TaskStore，最后投影成 ``RenderPayload``。它**不**重算业务结果，也不绕过链上的
任何一环。
"""

import pytest
from tests.fakes.recordings import (
    EMPTY_IN_SCOPE_BUT_TRAFFIC_ELSEWHERE,
    EMPTY_WITH_TRAFFIC,
    EMPTY_WITHOUT_TRAFFIC,
    GOLDEN,
    TIMEOUT,
)
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.contracts import PipelineStage, TaskStatus


async def test_golden_request_yields_a_successful_answer() -> None:
    harness = RuntimeHarness(GOLDEN)
    payload = await harness.handle("最近30分钟有哪些慢查询")
    assert payload.status is TaskStatus.SUCCEEDED
    assert payload.refs


async def test_empty_with_traffic_is_a_confident_negative_answer() -> None:
    harness = RuntimeHarness(EMPTY_WITH_TRAFFIC)
    payload = await harness.handle("最近30分钟有哪些慢查询")
    assert payload.status is TaskStatus.SUCCEEDED


async def test_empty_without_traffic_is_indeterminate() -> None:
    harness = RuntimeHarness(EMPTY_WITHOUT_TRAFFIC)
    payload = await harness.handle("最近30分钟有哪些慢查询")
    assert payload.status is TaskStatus.INDETERMINATE


async def test_traffic_elsewhere_does_not_make_the_answer_confident() -> None:
    """B1 的端到端回归：sales 无审计数据、别的库有流量 → 必须降级。"""
    harness = RuntimeHarness(EMPTY_IN_SCOPE_BUT_TRAFFIC_ELSEWHERE)
    payload = await harness.handle("查一下 sales 库最近30分钟的慢查询")
    assert payload.status is TaskStatus.INDETERMINATE


async def test_adapter_timeout_is_indeterminate_not_success() -> None:
    harness = RuntimeHarness(TIMEOUT)
    payload = await harness.handle("最近30分钟有哪些慢查询")
    assert payload.status is TaskStatus.INDETERMINATE


async def test_runtime_writes_the_terminal_state_to_the_task_store() -> None:
    """终态由 Runtime 写，且只写一次。"""
    harness = RuntimeHarness(GOLDEN)
    payload = await harness.handle("最近30分钟有哪些慢查询")
    record = await harness.store.get(lookup=harness.lookup)
    assert record.status is payload.status
    assert record.status in {TaskStatus.SUCCEEDED, TaskStatus.INDETERMINATE}


async def test_runtime_renders_only_from_the_evidence_ledger() -> None:
    """正例 + 反例。

    正例：渲染出的证据引用数等于 ledger 里的条数。
    反例：在 Runner 返回后、渲染前清空 ledger，同一次 outcome 必须渲染成"无证据"
    ——若 Runtime 读的是 Runner 的内部变量，这条就转红。
    """
    harness = RuntimeHarness(GOLDEN)
    payload = await harness.handle("最近30分钟有哪些慢查询")
    stored = await harness.ledger.load(task_id=harness.task_id)
    assert len(payload.refs) == len(stored)

    blind = RuntimeHarness(GOLDEN, clear_ledger_before_render=True)
    blinded = await blind.handle("最近30分钟有哪些慢查询")
    assert blinded.refs == ()
    assert blinded.status is TaskStatus.INDETERMINATE


async def test_runtime_returns_a_pending_payload_when_the_runner_pauses() -> None:
    harness = RuntimeHarness(GOLDEN, synthetic_write=True)
    payload = await harness.handle("最近30分钟有哪些慢查询")
    assert payload.status is TaskStatus.AWAITING_APPROVAL
    assert payload.refs
    assert harness.adapter.call_count == 0


async def test_identical_request_twice_yields_one_task_and_identical_payload() -> None:
    """幂等：同一 idempotency_key 只产生一个任务事实，渲染结果逐字相同。"""
    harness = RuntimeHarness(GOLDEN)
    first = await harness.handle("最近30分钟有哪些慢查询")
    second = await harness.handle("最近30分钟有哪些慢查询")
    assert first.model_dump_json() == second.model_dump_json()
    assert len(harness.store.created_task_ids) == 1


async def test_compatibility_handle_emits_every_non_model_stage_in_order() -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    stages = [event.stage for event in harness.sink.events]
    assert set(stages) == set(PipelineStage) - {PipelineStage.MODEL}

    # 八个单次阶段按链路顺序首次出现。
    ordered = [
        PipelineStage.INTENT,
        PipelineStage.RESOLVER,
        PipelineStage.PLANNER,
        PipelineStage.ADMISSION,
        PipelineStage.GATEWAY,
        PipelineStage.EVIDENCE,
        PipelineStage.REFLECTION,
        PipelineStage.RENDERING,
    ]
    first_seen = [stages.index(stage) for stage in ordered]
    assert first_seen == sorted(first_seen)

    # LIFECYCLE **两端各出现一次**，这不是缺陷：任务状态迁移确实发生在执行前
    # （→RUNNING，由 Runner 发）与执行后（→终态，由 Runtime 发）。
    lifecycle = [i for i, stage in enumerate(stages) if stage is PipelineStage.LIFECYCLE]
    assert len(lifecycle) >= 2
    assert lifecycle[0] < stages.index(PipelineStage.ADMISSION)
    assert lifecycle[-1] > stages.index(PipelineStage.REFLECTION)


@pytest.mark.parametrize(
    ("injected", "expected_stage"),
    [
        ("unknown_capability", PipelineStage.RESOLVER),
        ("window_too_wide", PipelineStage.PLANNER),
        ("adapter_timeout", PipelineStage.GATEWAY),
        ("empty_audit_window", PipelineStage.REFLECTION),
    ],
)
async def test_each_injected_failure_is_attributable_to_one_stage(
    injected: str, expected_stage: PipelineStage
) -> None:
    """错误归因闭环的机器可验收部分：注入一个故障，trace 指向唯一一个阶段。"""
    harness = RuntimeHarness.for_injection(injected)
    await harness.run_injected()
    failed = [e for e in harness.sink.events if e.outcome.value != "ok"]
    assert [e.stage for e in failed] == [expected_stage]


def test_render_ref_is_none_in_m3() -> None:
    """M3 不持久化 RenderPayload：它由 Runtime 同步返回，没有任何读回方。

    与其造一个没人读的 store，不如把 render_ref 显式置 None 并用测试把这个决定
    钉住——M4/M5 拆出 worker 与 API 后再补，那时才有真实的读回需求。
    """
    from xiaowei_agent.application.runtime import RENDER_REF

    assert RENDER_REF is None
