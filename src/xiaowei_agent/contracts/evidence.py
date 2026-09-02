"""证据信封。事实与解释分开：``facts`` 只承载结构化事实，解释归 Advisory。

``readonly`` 钉为 :data:`AlwaysTrue`——M2-M7 不存在可写证据。**不用
``Literal[True]``**：它按 ``==`` 比较，而 ``1 == True``，一个整数就能冒充只读标志。

``sampled`` 与 ``limitations`` 是必填而非可选：证据的样本性和时效性不能靠调用方
记得填。
"""

from xiaowei_agent.contracts.base import (
    AlwaysTrue,
    AwareDatetime,
    Contract,
    FrozenMap,
    StrictStr,
)
from xiaowei_agent.contracts.enums import ExternalSource


class EvidenceEnvelope(Contract):
    evidence_id: StrictStr
    capability_id: StrictStr
    capability_version: StrictStr
    facts: tuple[FrozenMap, ...]
    source: StrictStr
    source_kind: ExternalSource
    captured_at: AwareDatetime
    readonly: AlwaysTrue = True
    sampled: bool
    limitations: tuple[str, ...]
    redaction_ref: str | None = None
