"""每个 Protocol 都必须有实现满足它，且签名不漂移。

``Protocol`` 只在有赋值/传参发生时才被静态检查；一个从未被赋值的 Protocol 可能与
实现悄悄漂移而 mypy 毫无反应。赋值给带类型标注的变量让 mypy 真正检查结构兼容性；
再用 inspect 比对关键字参数，覆盖只跑 pytest 的场景。
"""

import inspect

from tests.fakes.clock import ManualClock  # noqa: F401  (M2 T10 起可用)

from xiaowei_agent.tools.adapter import ToolAdapter
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway, ToolGateway


def test_deterministic_gateway_satisfies_the_tool_gateway_protocol(
    recording_adapter: RecordingToolAdapter,
) -> None:
    instance: ToolGateway = DeterministicToolGateway(adapters={"starrocks": recording_adapter})
    assert instance is not None


def test_recording_adapter_satisfies_the_tool_adapter_protocol(
    recording_adapter: RecordingToolAdapter,
) -> None:
    adapter: ToolAdapter = recording_adapter
    assert adapter is not None


def _keyword_params(func: object) -> set[str]:
    return {
        name
        for name, param in inspect.signature(func).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }


def test_gateway_implementation_keeps_the_protocol_keyword_arguments() -> None:
    """关键字参数漂移不会被结构兼容性检查发现，但会在调用点炸掉。"""
    assert _keyword_params(DeterministicToolGateway.invoke) == _keyword_params(
        ToolGateway.invoke
    )
