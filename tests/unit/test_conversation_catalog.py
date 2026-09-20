"""I2-B 普通对话投影：回答是能力快照的纯函数。

这条通道不调用工具也不带证据，所以它唯一的正确性来源就是"回答完全由声明决定"。
本文件把这一点当成契约来测：换快照必须换回答，换用户文本必须不换回答。
"""

import pytest

from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
from xiaowei_agent.contracts import (
    CapabilitySnapshot,
    CapabilitySpec,
    EffectClass,
    OperationSpec,
    ReadClass,
    TaskStatus,
)
from xiaowei_agent.rendering.generic import render_conversation_response


def _spec(capability_id: str, *, read_class: ReadClass) -> CapabilitySpec:
    return CapabilitySpec(
        capability_id=capability_id,
        version="9.9.9",
        domain="probe",
        input_schema_ref="schema.probe.v1",
        operations=(
            OperationSpec(
                operation="probe_read",
                gateway="probe_gateway",
                effect_class=EffectClass.READ,
                read_class=read_class,
                side_effect=False,
                argument_schema_ref="schema.probe.args.v1",
            ),
        ),
        policy_profile="readonly.probe.v1",
        evidence_contract="evidence.probe.v1",
        eval_ref="evals.probe.v1",
    )


def test_catalog_answer_lists_every_registered_capability_with_its_read_class() -> None:
    snapshot = StaticCapabilityRegistry().snapshot()

    payload = render_conversation_response(snapshot=snapshot)

    assert payload.status is TaskStatus.SUCCEEDED
    assert payload.refs == (f"capability-snapshot:{snapshot.snapshot_id}",)
    assert len(payload.sections) == len(snapshot.specs)
    for spec, section in zip(snapshot.specs, payload.sections, strict=True):
        assert section.title == spec.capability_id
        assert section.refs == (f"capability:{spec.capability_id}@{spec.version}",)
        assert spec.domain in section.body
        assert spec.version in section.body
        for operation in spec.operations:
            assert operation.operation in section.body
            assert operation.gateway in section.body
            assert operation.effect_class.value in section.body
            assert operation.read_class is not None
            assert operation.read_class.value in section.body


def test_catalog_answer_changes_with_the_snapshot() -> None:
    """快照是唯一输入：换一份声明必须换一份回答。

    没有这条，把投影写成常量同样能过上面那条——因为真实 registry 恰好一直是那三条。
    """
    one = CapabilitySnapshot(
        snapshot_id="snapshot.probe.one",
        specs=(_spec("probe.alpha", read_class=ReadClass.BOUNDED),),
    )
    two = CapabilitySnapshot(
        snapshot_id="snapshot.probe.two",
        specs=(
            _spec("probe.alpha", read_class=ReadClass.BOUNDED),
            _spec("probe.beta", read_class=ReadClass.RESTRICTED),
        ),
    )

    first = render_conversation_response(snapshot=one)
    second = render_conversation_response(snapshot=two)

    assert first != second
    assert [section.title for section in first.sections] == ["probe.alpha"]
    assert [section.title for section in second.sections] == [
        "probe.alpha",
        "probe.beta",
    ]
    assert first.refs == ("capability-snapshot:snapshot.probe.one",)
    assert second.refs == ("capability-snapshot:snapshot.probe.two",)
    assert ReadClass.RESTRICTED.value in second.sections[1].body


def test_empty_snapshot_says_so_instead_of_rendering_an_empty_list() -> None:
    """没有能力时必须**明说**没有能力。

    只把 sections 渲染成空列表，用户看到的是一句"我能做的事就是下面这份清单"后面
    什么都没有——那是一个看起来正常、实际在撒谎的回答。
    """
    empty = CapabilitySnapshot(snapshot_id="snapshot.probe.empty", specs=())

    payload = render_conversation_response(snapshot=empty)

    assert payload.sections == ()
    assert "没有任何已注册能力" in payload.answer
    assert payload.refs == ("capability-snapshot:snapshot.probe.empty",)


def test_projection_is_deterministic_for_one_snapshot() -> None:
    snapshot = StaticCapabilityRegistry().snapshot()

    assert render_conversation_response(
        snapshot=snapshot
    ) == render_conversation_response(snapshot=snapshot)


def test_projection_takes_no_user_text_at_all() -> None:
    """签名层面的反证：投影只接受快照。

    只要它多一个用户文本入参，这条就会红。这比逐条断言"输出里没有那段文本"更早、
    也更难绕过——后者只能证明某几个样本没泄漏。
    """
    import inspect

    signature = inspect.signature(render_conversation_response)
    assert list(signature.parameters) == ["snapshot"]


@pytest.mark.parametrize(
    "read_class", [ReadClass.BOUNDED, ReadClass.RESTRICTED]
)
def test_read_class_is_reported_verbatim(read_class: ReadClass) -> None:
    snapshot = CapabilitySnapshot(
        snapshot_id="snapshot.probe.readclass",
        specs=(_spec("probe.alpha", read_class=read_class),),
    )

    payload = render_conversation_response(snapshot=snapshot)

    assert read_class.value in payload.sections[0].body
