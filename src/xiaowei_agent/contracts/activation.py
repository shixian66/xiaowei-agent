"""W1b 身份激活申请的不可变契约。"""

from itertools import pairwise
from typing import Annotated, Final, Literal, Self, TypeAlias

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import (
    CONTROLLED_PII_MAX_LENGTH,
    AwareDatetime,
    Contract,
    ControlledPii,
    Sha256Hex,
    StrictInt,
)
from xiaowei_agent.contracts.enums import (
    ActivationSource,
    ActivationStatus,
    IdentitySource,
    ProductRole,
    WebReturnIntentKind,
)
from xiaowei_agent.contracts.identity import BoundedId, ControlledPiiField
from xiaowei_agent.contracts.web_navigation import WebReturnIntent

ACTIVATION_SOURCE_REFERENCE_REQUIREMENTS: Final = {
    ActivationSource.WEB_LOGIN: False,
    ActivationSource.SAFE_TASK_LINK: False,
    ActivationSource.FEISHU_GROUP: True,
}
"""每个来源是否必须携带 event/chat 摘要；键集合必须等于枚举全集。"""

ACTIVATION_SOURCE_INTENT_KINDS: Final = {
    ActivationSource.WEB_LOGIN: frozenset(
        {
            WebReturnIntentKind.WORKBENCH,
            WebReturnIntentKind.ADMIN_CENTER,
            WebReturnIntentKind.ACTIVATION_STATUS,
        }
    ),
    ActivationSource.SAFE_TASK_LINK: frozenset(
        {WebReturnIntentKind.SAFE_TASK_DETAIL}
    ),
    ActivationSource.FEISHU_GROUP: frozenset(),
}
"""每个来源允许的 return intent；空集合表示必须为 ``None``。"""

ACTIVATION_STATUS_DECISION_RULES: Final = {
    ActivationStatus.PENDING: (False, frozenset()),
    ActivationStatus.EXPIRED: (False, frozenset()),
    ActivationStatus.REJECTED: (True, frozenset()),
    ActivationStatus.APPROVED: (
        True,
        frozenset({ProductRole.USER, ProductRole.OPERATOR}),
    ),
}
"""状态到（是否需要决定字段、允许角色）的总映射。"""


def _validate_source_shape(
    *,
    source: ActivationSource,
    has_event: bool,
    has_chat: bool,
    return_intent: WebReturnIntent | None,
) -> None:
    requires_references = ACTIVATION_SOURCE_REFERENCE_REQUIREMENTS[source]
    if has_event is not requires_references or has_chat is not requires_references:
        raise ValueError("activation source references do not match source")
    allowed_kinds = ACTIVATION_SOURCE_INTENT_KINDS[source]
    if not allowed_kinds:
        if return_intent is not None:
            raise ValueError("activation source must not carry a return intent")
        return
    if return_intent is None or return_intent.kind not in allowed_kinds:
        raise ValueError("activation return intent does not match source")

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
    return_intent: WebReturnIntent | None
    source_event_ref: ControlledPiiOptionalField = None
    source_chat_ref: ControlledPiiOptionalField = None

    @model_validator(mode="after")
    def _source_references_match_source(self) -> Self:
        has_event = self.source_event_ref is not None
        has_chat = self.source_chat_ref is not None
        _validate_source_shape(
            source=self.source,
            has_event=has_event,
            has_chat=has_chat,
            return_intent=self.return_intent,
        )
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
    return_intent: WebReturnIntent | None
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
        _validate_source_shape(
            source=self.source,
            has_event=has_event,
            has_chat=has_chat,
            return_intent=self.return_intent,
        )
        return self

    @model_validator(mode="after")
    def _decision_fields_match_status(self) -> Self:
        has_decision = self.decided_at is not None and self.decided_by is not None
        has_partial_decision = (self.decided_at is None) != (self.decided_by is None)
        if has_partial_decision:
            raise ValueError("decision time and actor must be present together")
        decision_required, allowed_roles = ACTIVATION_STATUS_DECISION_RULES[self.status]
        if has_decision is not decision_required:
            raise ValueError("activation decision fields do not match status")
        if self.approved_role not in allowed_roles:
            if self.approved_role is not None or allowed_roles:
                raise ValueError("activation approved role does not match status")
        return self


class PendingActivationListQuery(Contract):
    """有效 pending 申请的显式 scope 与复合 keyset 游标。"""

    tenant_id: BoundedId
    environment_id: BoundedId
    before_requested_at: AwareDatetime | None = None
    before_request_id: BoundedId | None = None
    limit: StrictInt = Field(default=50, gt=0, le=100)

    @model_validator(mode="after")
    def _cursor_is_complete(self) -> Self:
        if (self.before_requested_at is None) != (self.before_request_id is None):
            raise ValueError("pending activation cursor must be complete")
        return self


class PendingActivationPage(Contract):
    """按 ``(requested_at, request_id)`` 严格降序的 pending 页。"""

    items: tuple[ActivationRequest, ...] = Field(max_length=100)
    next_requested_at: AwareDatetime | None = None
    next_request_id: BoundedId | None = None

    @model_validator(mode="after")
    def _ordering_and_cursor_are_consistent(self) -> Self:
        keys = tuple((item.requested_at, item.request_id) for item in self.items)
        if any(left <= right for left, right in pairwise(keys)):
            raise ValueError("pending activations must be strictly descending")
        if (self.next_requested_at is None) != (self.next_request_id is None):
            raise ValueError("pending activation page cursor must be complete")
        if not self.items and self.next_requested_at is not None:
            raise ValueError("an empty pending activation page cannot have a cursor")
        if self.next_requested_at is not None and (
            self.next_requested_at,
            self.next_request_id,
        ) != keys[-1]:
            raise ValueError("pending activation cursor must match the last item")
        return self

class ActivationRetentionReport(Contract):
    """一次终态保留运行的闭集计数；不含任何 request id、主体、决定人或作用域。"""

    pending_expired: StrictInt = Field(ge=0)
    approved_deleted: StrictInt = Field(ge=0)
    rejected_deleted: StrictInt = Field(ge=0)
    expired_deleted: StrictInt = Field(ge=0)


__all__ = [
    "ACTIVATION_SOURCE_INTENT_KINDS",
    "ACTIVATION_SOURCE_REFERENCE_REQUIREMENTS",
    "ACTIVATION_STATUS_DECISION_RULES",
    "ActivationLookup",
    "ActivationRequest",
    "ActivationRetentionReport",
    "ActivationSource",
    "ActivationStatus",
    "CreateActivationCommand",
    "PendingActivationListQuery",
    "PendingActivationPage",
]
