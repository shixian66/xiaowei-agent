"""最小步骤级 trace / audit 事件契约。

**只定义事件形状与阶段枚举，不实现采集后端**（DEVELOPMENT_PLAN §7 M2）。九个阶段
与 ARCHITECTURE §13.1 的错误归因阶段逐一对应，使一次失败能被定位到具体阶段——这是
M3 建立首个错误分析闭环的前提。

``detail`` 的取值在**字段级** validator 中脱敏并限长 256 字符：它是标签，不是载荷。
脱敏复用下沉后的 ``xiaowei_agent.redaction``（而非 ``log``），因此 contracts 保持
只依赖 redaction 与标准库。
"""

from collections.abc import Mapping
from typing import Annotated, Final

from pydantic import AfterValidator, BeforeValidator, Field, PlainSerializer

from xiaowei_agent.contracts.base import (
    AwareDatetime,
    Contract,
    FreeText,
    StrictInt,
    StrictStr,
    TraceId,
    frozen_map,
)
from xiaowei_agent.contracts.enums import PipelineStage, StageOutcome
from xiaowei_agent.contracts.errors import AgentError
from xiaowei_agent.redaction import scrub_text


def _scrub_details(value: object) -> Mapping[str, str]:
    """**键与值都脱敏**后冻结。

    只脱敏值会把 secret 留在键里：``{"token=<值>": "safe"}`` 原样出现在
    审计事件中。键同样是调用方拼出来的自由文本。

    脱敏后两个键可能塌成同一个（``token=a`` 与 ``token=b`` 都变成 ``token=***``）。
    静默覆盖会丢失一条事件明细，因此**碰撞即拒绝**——与 ``canonical_json`` 的 NFC
    键碰撞是同一类缺陷。

    写成 ``BeforeValidator`` 而非 ``AfterValidator``，有两个各自独立的理由：

    1. **顺序**：写成后置校验时，内层 ``Mapping[StrictStr, ...]`` 的键类型与值的
       长度约束都在脱敏之前执行，未脱敏的原文会被拒绝路径带出去。
    2. **错误内容**：内层校验失败时 ``ValidationError`` 的 ``loc`` 会把出错的键
       原样嵌进去（``detail.  token=secret  .[key]``）。``hide_input_in_errors``
       只隐藏 ``input``，对 ``loc`` 无效。

    因此全部检查都必须在这里手写，且错误文本只用序号定位，不回显任何取值。
    模型级 after-validator 同样不可用：返回非 ``self`` 的对象在 ``__init__``
    路径上会被 Pydantic 丢弃（只发一条警告），脱敏会静默失效。
    """
    if not isinstance(value, Mapping):
        raise ValueError("detail must be a mapping")
    scrubbed: dict[str, str] = {}
    for index, (key, item) in enumerate(value.items()):
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"detail entry #{index} must map str to str")
        safe_key = scrub_text(key)
        if not safe_key or safe_key != safe_key.strip():
            raise ValueError(f"detail key #{index} is empty or padded with whitespace")
        if safe_key in scrubbed:
            raise ValueError(f"detail key #{index} collides with an earlier key after redaction")
        safe_value = scrub_text(item)
        if len(safe_value) > _DETAIL_VALUE_MAX:
            raise ValueError(
                f"detail value at key #{index} exceeds "
                f"{_DETAIL_VALUE_MAX} characters after redaction"
            )
        scrubbed[safe_key] = safe_value
    return frozen_map(scrubbed)


_DETAIL_VALUE_MAX: Final[int] = 256
TraceDetail = Annotated[
    Mapping[StrictStr, FreeText],
    BeforeValidator(_scrub_details),
    # 前置脱敏返回的 MappingProxyType 会被内层 Mapping 校验重建成普通 dict，
    # 必须在其后再冻结一次；前置管顺序与错误内容，后置管不可变性。
    AfterValidator(frozen_map),
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
    attempt_number: StrictInt | None = Field(default=None, ge=0)
    error: AgentError | None
    detail: TraceDetail
