"""TraceSink 与 Runner 侧的阶段发射。

一次失败必须能被定位到**唯一一个** ``PipelineStage``——这是 ARCHITECTURE §13.1
要求的错误分析闭环的机器可验收部分。没有它，"阅读 trace → 归因到具体阶段"就只是
一句文档要求。

九个阶段中，Runner 覆盖 ADMISSION / GATEWAY / EVIDENCE / LIFECYCLE 四个；
INTENT / RESOLVER / PLANNER / REFLECTION / RENDERING 由 Runtime 发射，在 T12 与
本文件的用例合并成端到端断言。
"""

import logging

import pytest
from tests.fakes.recordings import EMPTY_WITH_TRAFFIC, GOLDEN, MALFORMED, TIMEOUT
from tests.fakes.runner import RunnerHarness

from xiaowei_agent.contracts import PipelineStage, StageOutcome
from xiaowei_agent.observability.log_sink import StructuredLogTraceSink
from xiaowei_agent.runners.runner import WorkflowPaused


async def test_a_successful_run_emits_the_runner_stages_in_order() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    stages = [event.stage for event in harness.sink.events]
    assert stages == [
        PipelineStage.LIFECYCLE,
        PipelineStage.ADMISSION,
        PipelineStage.GATEWAY,
        PipelineStage.EVIDENCE,
    ]


async def test_two_step_run_emits_a_stage_triple_per_step() -> None:
    harness = RunnerHarness(EMPTY_WITH_TRAFFIC)
    await harness.start()
    stages = [event.stage for event in harness.sink.events]
    assert stages.count(PipelineStage.ADMISSION) == 2
    assert stages.count(PipelineStage.GATEWAY) == 2
    assert stages.count(PipelineStage.EVIDENCE) == 2


async def test_every_event_carries_the_trace_id_and_task_id() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    assert harness.sink.events
    for event in harness.sink.events:
        assert event.trace_id == harness.context.trace_id
        assert event.task_id == harness.task_id


async def test_successful_stages_are_marked_ok() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    assert all(event.outcome is StageOutcome.OK for event in harness.sink.events)


@pytest.mark.parametrize(
    ("recording", "expected_stage"),
    [
        (TIMEOUT, PipelineStage.GATEWAY),
        (MALFORMED, PipelineStage.GATEWAY),
    ],
    ids=["adapter_timeout", "malformed_response"],
)
async def test_each_injected_failure_is_attributable_to_one_stage(
    recording: object, expected_stage: PipelineStage
) -> None:
    """注入一个故障，trace 必须指向唯一一个阶段。"""
    harness = RunnerHarness(recording)
    await harness.start()
    failed = [e for e in harness.sink.events if e.outcome is not StageOutcome.OK]
    assert [e.stage for e in failed] == [expected_stage]


async def test_a_tampered_plan_is_attributed_to_admission() -> None:
    harness = RunnerHarness(GOLDEN, tamper_sql=True)
    with pytest.raises(Exception):  # noqa: B017 —— 具体类型由 T7 承重，这里看归因
        await harness.start()
    failed = [e for e in harness.sink.events if e.outcome is not StageOutcome.OK]
    assert [e.stage for e in failed] == [PipelineStage.ADMISSION]


async def test_a_pause_is_attributed_to_admission_not_gateway() -> None:
    """暂停发生在准入边界；它不该被记成一次工具调用失败。"""
    harness = RunnerHarness(GOLDEN, synthetic_write=True)
    with pytest.raises(WorkflowPaused):
        await harness.start()
    stages = [e.stage for e in harness.sink.events if e.outcome is not StageOutcome.OK]
    assert PipelineStage.GATEWAY not in stages
    assert PipelineStage.ADMISSION in stages


async def test_trace_detail_never_carries_sql_or_external_text() -> None:
    harness = RunnerHarness(GOLDEN)
    await harness.start()
    for event in harness.sink.events:
        dumped = event.model_dump_json()
        assert "SELECT" not in dumped
        assert "starrocks_audit_tbl__" not in dumped


def test_structured_log_sink_writes_one_record_per_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.fakes.sinks import make_event

    sink = StructuredLogTraceSink()
    with caplog.at_level(logging.INFO):
        sink.emit(make_event(stage=PipelineStage.GATEWAY))
    assert len(caplog.records) == 1


def test_structured_log_sink_keeps_a_constant_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """消息体恒为常量，取值只经 ``extra`` 传出。

    这条断言看的是 **sink 自己的行为**。上一版这里写的是"canary 不出现在
    caplog.text"，但那是绿的**理由不对**：detail 在契约层就已经被 scrub 掉了，
    即使 sink 把取值拼进消息也检测不到。把取值格式化进消息会让脱敏与限长在格式化
    那一步失效，因此必须直接钉住"消息是常量"。
    """
    from tests.fakes.sinks import make_event

    sink = StructuredLogTraceSink()
    with caplog.at_level(logging.INFO):
        sink.emit(make_event(stage=PipelineStage.GATEWAY, detail={"note": "n"}))
    assert caplog.records[0].getMessage() == "trace event"


def test_contract_layer_scrubs_detail_before_it_reaches_the_sink(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """纵深防御的另一层：即便 sink 出错，detail 里也已经没有原文。"""
    from tests.fakes.sinks import make_event

    canary = "password=" + "hunter2"
    event = make_event(stage=PipelineStage.GATEWAY, detail={"note": canary})
    assert "hunter2" not in str(dict(event.detail))
    sink = StructuredLogTraceSink()
    with caplog.at_level(logging.INFO):
        sink.emit(event)
    assert "hunter2" not in caplog.text


def test_structured_log_sink_only_depends_on_contracts_and_stdlib() -> None:
    import ast
    from pathlib import Path

    from xiaowei_agent.observability import log_sink

    tree = ast.parse(Path(log_sink.__file__).read_text(encoding="utf-8"))
    internal = set()
    for node in ast.walk(tree):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if (module or "").startswith("xiaowei_agent"):
            internal.add(".".join((module or "").split(".")[:2]))
    assert internal <= {"xiaowei_agent.contracts", "xiaowei_agent.observability"}
