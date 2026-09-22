"""W1b 身份激活申请的不可变契约。"""

from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import (
    CONTROLLED_PII_MAX_LENGTH,
    AwareDatetime,
    Contract,
    ControlledPii,
    Sha256Hex,
)
from xiaowei_agent.contracts.enums import (
    ActivationSource,
    ActivationStatus,
    IdentitySource,
    ProductRole,
)
from xiaowei_agent.contracts.identity import BoundedId, ControlledPiiField

ControlledPiiOptionalField: TypeAlias = Annotated[
    ControlledPii | None,
    Field(
        min_length=1,
        max_length=CONTROLLED_PII_MAX_LENGTH,
        exclude=True,
        repr=False,
    ),
]
"""可空受控 PII；把字段标志挂在整个联合上，避免 Pydantic 忽略元数据。"""


class CreateActivationCommand(Contract):
    """创建或复用待办；时间、ID 与摘要全部由 Store 派生。"""

    tenant_id: BoundedId
    environment_id: BoundedId
    provider: Literal[IdentitySource.FEISHU]
    subject_ref: ControlledPiiField
    source: ActivationSource
    source_event_ref: ControlledPiiOptionalField = None
    source_chat_ref: ControlledPiiOptionalField = None

    @model_validator(mode="after")
    def _source_references_match_source(self) -> Self:
        has_event = self.source_event_ref is not None
        has_chat = self.source_chat_ref is not None
        if self.source is ActivationSource.WEB_LOGIN and (has_event or has_chat):
            raise ValueError("web login activation must not carry event or chat refs")
        if self.source is ActivationSource.FEISHU_GROUP and not (has_event and has_chat):
            raise ValueError("group activation requires event and chat refs")
        return self


class ActivationLookup(Contract):
    """不可枚举 request id 之外仍必须显式携带作用域。"""

    request_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId


class ActivationRequest(Contract):
    """持久化激活事实；原始事件与会话引用从不进入本契约。"""

    request_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId
    provider: Literal[IdentitySource.FEISHU]
    subject_ref: ControlledPiiField
    subject_ref_digest: Sha256Hex
    source: ActivationSource
    source_event_digest: Sha256Hex | None = None
    source_chat_digest: Sha256Hex | None = None
    requested_at: AwareDatetime
    expires_at: AwareDatetime
    status: ActivationStatus
    decided_at: AwareDatetime | None = None
    decided_by: BoundedId | None = None
    approved_role: ProductRole | None = None

    @model_validator(mode="after")
    def _expiration_is_after_request(self) -> Self:
        if self.expires_at <= self.requested_at:
            raise ValueError("activation expiration must be after request time")
        return self

    @model_validator(mode="after")
    def _source_digests_match_source(self) -> Self:
        has_event = self.source_event_digest is not None
        has_chat = self.source_chat_digest is not None
        if self.source is ActivationSource.WEB_LOGIN and (has_event or has_chat):
            raise ValueError("web login activation must not carry event or chat digests")
        if self.source is ActivationSource.FEISHU_GROUP and not (has_event and has_chat):
            raise ValueError("group activation requires event and chat digests")
        return self

    @model_validator(mode="after")
    def _decision_fields_match_status(self) -> Self:
        has_decision = self.decided_at is not None and self.decided_by is not None
        has_partial_decision = (self.decided_at is None) != (self.decided_by is None)
        if has_partial_decision:
            raise ValueError("decision time and actor must be present together")
        if self.status in {ActivationStatus.PENDING, ActivationStatus.EXPIRED}:
            if has_decision or self.approved_role is not None:
                raise ValueError("pending or expired activation has no decision fields")
            return self
        if not has_decision:
            raise ValueError("a decided activation requires decision time and actor")
        if self.status is ActivationStatus.REJECTED:
            if self.approved_role is not None:
                raise ValueError("a rejected activation must not carry a role")
            return self
        if self.approved_role not in {ProductRole.USER, ProductRole.OPERATOR}:
            raise ValueError("activation may approve only user or operator")
        return self


__all__ = [
    "ActivationLookup",
    "ActivationRequest",
    "ActivationSource",
    "ActivationStatus",
    "CreateActivationCommand",
]
