"""最小步骤级 trace / audit 事件契约。

**只定义事件形状与阶段枚举，不实现采集后端**（DEVELOPMENT_PLAN §7 M2）。九个阶段
与 ARCHITECTURE §13.1 的错误归因阶段逐一对应，使一次失败能被定位到具体阶段——这是
M3 建立首个错误分析闭环的前提。

``detail`` 的取值在**字段级** validator 中脱敏并限长 256 字符：它是标签，不是载荷。
脱敏复用下沉后的 ``xiaowei_agent.redaction``（而非 ``log``），因此 contracts 保持
只依赖 redaction 与标准库。
"""

from collections.abc import Mapping
from typing import Annotated

from pydantic import AfterValidator, Field, PlainSerializer

from xiaowei_agent.contracts.base import (
    AwareDatetime,
    Contract,
    StrictStr,
    TraceId,
    frozen_map,
)
from xiaowei_agent.contracts.enums import PipelineStage, StageOutcome
from xiaowei_agent.contracts.errors import AgentError
from xiaowei_agent.redaction import scrub_text


def _scrub_details(value: Mapping[str, str]) -> Mapping[str, str]:
    """**键与值都脱敏**后冻结。

    只脱敏值会把 secret 留在键里：``{"token=abc123def456": "safe"}`` 原样出现在
    审计事件中。键同样是调用方拼出来的自由文本。

    脱敏后两个键可能塌成同一个（``token=a`` 与 ``token=b`` 都变成 ``token=***``）。
    静默覆盖会丢失一条事件明细，因此**碰撞即拒绝**——与 ``canonical_json`` 的 NFC
    键碰撞是同一类缺陷。

    写成**字段级** validator 而非 model 级 after-validator：后者返回非 ``self`` 的
    对象在 ``__init__`` 路径上会被 Pydantic 丢弃（只发一条警告），脱敏会静默失效。
    """
    scrubbed: dict[str, str] = {}
    for key, item in value.items():
        safe_key = scrub_text(key)
        if safe_key in scrubbed:
            raise ValueError(f"detail keys collide after redaction: {safe_key!r}")
        scrubbed[safe_key] = scrub_text(item)
    return frozen_map(scrubbed)


DetailValue = Annotated[str, Field(max_length=256)]
TraceDetail = Annotated[
    Mapping[StrictStr, DetailValue],
    AfterValidator(_scrub_details),
    # 与 FrozenMap 同理：MappingProxyType 不是 pydantic 认识的序列化目标，
    # 不显式转换时 model_dump() 会发 PydanticSerializationUnexpectedValue 警告。
    PlainSerializer(dict, return_type=dict, when_used="always"),
]


class TraceEvent(Contract):
    event_id: StrictStr
    trace_id: TraceId
    task_id: StrictStr | None
    stage: PipelineStage
    outcome: StageOutcome
    occurred_at: AwareDatetime
    capability_id: StrictStr | None
    step_id: StrictStr | None
    policy_revision: StrictStr | None
    error: AgentError | None
    detail: TraceDetail
