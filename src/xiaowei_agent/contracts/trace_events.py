"""最小步骤级 trace / audit 事件契约。

**只定义事件形状与阶段枚举，不实现采集后端**（DEVELOPMENT_PLAN §7 M2）。九个阶段
与 ARCHITECTURE §13.1 的错误归因阶段逐一对应，使一次失败能被定位到具体阶段——这是
M3 建立首个错误分析闭环的前提。

``detail`` 的取值在**字段级** validator 中脱敏并限长 256 字符：它是标签，不是载荷。
脱敏复用下沉后的 ``xiaowei_agent.redaction``（而非 ``log``），因此 contracts 保持
只依赖 redaction 与标准库。
"""

import datetime as _dt
from collections.abc import Mapping
from typing import Annotated

from pydantic import AfterValidator, Field

from xiaowei_agent.contracts.base import Contract, StrictStr, frozen_map
from xiaowei_agent.contracts.enums import PipelineStage, StageOutcome
from xiaowei_agent.contracts.errors import AgentError
from xiaowei_agent.redaction import scrub_text


def _scrub_details(value: Mapping[str, str]) -> Mapping[str, str]:
    """逐值脱敏后冻结。

    写成**字段级** validator 而非 model 级 after-validator：后者返回非 ``self`` 的
    对象在 ``__init__`` 路径上会被 Pydantic 丢弃（只发一条警告），脱敏会静默失效。
    """
    return frozen_map({key: scrub_text(item) for key, item in value.items()})


DetailValue = Annotated[str, Field(max_length=256)]
TraceDetail = Annotated[Mapping[StrictStr, DetailValue], AfterValidator(_scrub_details)]


class TraceEvent(Contract):
    event_id: StrictStr
    trace_id: StrictStr
    task_id: str | None
    stage: PipelineStage
    outcome: StageOutcome
    occurred_at: _dt.datetime
    capability_id: str | None
    step_id: str | None
    policy_revision: str | None
    error: AgentError | None
    detail: TraceDetail
