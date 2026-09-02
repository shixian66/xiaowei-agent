"""证据信封。事实与解释分开：``facts`` 只承载结构化事实，解释归 Advisory。

``readonly`` 钉为 ``Literal[True]``——M2-M7 不存在可写证据。``sampled`` 与
``limitations`` 是必填而非可选：证据的样本性和时效性不能靠调用方记得填。
"""

import datetime as _dt
from typing import Literal

from xiaowei_agent.contracts.base import Contract, FrozenMap, StrictStr
from xiaowei_agent.contracts.enums import ExternalSource


class EvidenceEnvelope(Contract):
    evidence_id: StrictStr
    capability_id: StrictStr
    capability_version: StrictStr
    facts: tuple[FrozenMap, ...]
    source: StrictStr
    source_kind: ExternalSource
    captured_at: _dt.datetime
    readonly: Literal[True] = True
    sampled: bool
    limitations: tuple[str, ...]
    redaction_ref: str | None = None
