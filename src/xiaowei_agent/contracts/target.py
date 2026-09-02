"""已解析的执行目标。

**规范化必须在字段级 validator 完成，不能在 model 级 after-validator 里改写**：
Pydantic v2 的 ``model_validator(mode="after")`` 通过 ``__init__`` 构造时，返回
非 ``self`` 的对象会被丢弃（只发警告），规范化于是静默失效。字段级
``AfterValidator`` 才能真正改写取值。

**顺序**：先 NFC 规范化，**再**检测碰撞。反过来会让规范化后才相同的两个 ID 作为
两项存活，使同一目标产生不同指纹。碰撞**拒绝**而非合并：规范化后相同意味着调用方
传入了未确认别名，目标解析不稳定，不应生成可审批指纹（ARCHITECTURE §7.2）。

不含连接串、凭证、原始 SQL 或 display text——它们会让指纹随展示措辞漂移。
"""

import unicodedata
from typing import Annotated

from pydantic import AfterValidator, Field

from xiaowei_agent.contracts.base import Contract, StrictStr


def _normalise_resource_ids(value: tuple[str, ...]) -> tuple[str, ...]:
    normalised = tuple(unicodedata.normalize("NFC", rid) for rid in value)
    if len(set(normalised)) != len(normalised):
        raise ValueError(
            "resource_ids collide after NFC normalisation; "
            "target contains unconfirmed aliases"
        )
    return normalised


ResourceIds = Annotated[
    tuple[StrictStr, ...],
    Field(min_length=1),
    AfterValidator(_normalise_resource_ids),
]


class ResolvedTarget(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    provider: StrictStr
    resource_kind: StrictStr
    resource_ids: ResourceIds
    selector_version: StrictStr
