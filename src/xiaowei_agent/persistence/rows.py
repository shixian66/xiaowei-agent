"""DTO ↔ 数据库行的映射：**纯函数，不碰连接**。

与 ``decisions.py`` 同一个理由分出来：映射写在执行 SQL 的方法里，就只能靠一个真实
数据库才能测——而"往返是否无损"根本不需要数据库来回答。分出来之后，无损性在默认
测试路径上就能证明，集成测试只需要再证"数据库也没弄丢东西"。

**两组独立的往返风险，不能用一组覆盖另一组**：

1. **JSONB 表**：DTO → JSON → DTO。风险在嵌套契约、``FrozenMap``、``tuple`` 与
   ``AlwaysTrue`` 这类特殊字段。
2. **``tasks`` 表**：``TaskRecord`` → 列展开 → ``TaskRecord``。风险完全不同——
   ``AwareDatetime`` 经 ``timestamptz`` 往返的时区与微秒精度、``Sha256Hex`` 的长度
   约束、``terminal_reason`` 的 ``None`` 与 SQL ``NULL`` 的对应，以及三个租约字段的
   同置同清不变量。

**JSON 路径是被契约层要求的，不是实现偏好。** ``Contract`` 全局 ``strict=True``：
python 校验模式下 ``str`` 不能转 ``StrEnum``、ISO 字符串不能转 ``datetime``，而 JSON
校验模式下两者都允许（见 ``contracts/base.py``）。因此从 JSONB 读回的 ``dict`` 必须
经 ``model_validate_json`` 回到契约，不能直接 ``model_validate``。
"""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import TypeAdapter

from xiaowei_agent.contracts import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    ChannelKind,
    ClarificationRecord,
    ClarificationSubject,
    Contract,
    DestinationKind,
    IdentitySource,
    InteractionDraft,
    ModelAdvisory,
    ModelUsage,
    ProductRole,
    ProjectionErrorCode,
    ProjectionState,
    StepOutcomeKind,
    StepResultStatus,
    TaskRecord,
    TaskStatus,
    UserStatus,
)
from xiaowei_agent.contracts.activation import (
    ActivationRequest,
    ActivationSource,
    ActivationStatus,
)
from xiaowei_agent.contracts.web_navigation import (
    WebReturnIntent,
    WebReturnIntentKind,
)

if TYPE_CHECKING:
    from xiaowei_agent.contracts.admin_audit import AdminAuditEvent
    from xiaowei_agent.persistence.channel import ChannelBinding, ProjectionSubscription
    from xiaowei_agent.persistence.model_artifacts import (
        AcceptedInteractionArtifact,
        StoredModelAdvisory,
    )
    from xiaowei_agent.persistence.store import StepExecutionRecord
    from xiaowei_agent.persistence.web_session import OAuthState, WebSession

_C = TypeVar("_C", bound=Contract)


def dump_contract(model: Contract) -> dict[str, Any]:
    """契约 → 可写入 JSONB 的普通 ``dict``。

    走 ``model_dump_json`` 再 ``json.loads``，而不是 ``model_dump(mode="json")``：
    前者是 Pydantic 真正的 JSON 序列化路径，与 ``load_contract`` 的 ``validate_json``
    严格互逆。后者在若干边界类型上与 JSON 路径有细微差异，用它会让往返测试证明的
    是另一条路径。
    """
    loaded: dict[str, Any] = json.loads(model.model_dump_json())
    return loaded


def load_contract(model_type: type[_C], payload: Mapping[str, Any]) -> _C:
    """JSONB 读回的 ``dict`` → 契约。

    必须经 JSON 校验模式：strict 下 ``model_validate`` 会拒绝 ISO 字符串形态的
    ``datetime`` 与字符串形态的枚举，而 JSONB 里存的正是这两种形态。
    """
    return model_type.model_validate_json(json.dumps(payload))


def record_to_row(record: TaskRecord) -> dict[str, Any]:
    """``TaskRecord`` → ``tasks`` 表的一行。

    ``terminal_reason`` 与三个租约字段一律**无条件**出现在结果里，取值可以是
    ``None``。省略"当前为 None"的键会让调用方写出"有值才进 SET 子句"的 UPDATE，
    于是清空语义丢失——见 ``decisions.apply_transition`` 的同一条约束。
    """
    return {
        "task_id": record.task_id,
        "tenant_id": record.tenant_id,
        "environment_id": record.environment_id,
        "actor": record.actor,
        "idempotency_key": record.idempotency_key,
        "request_digest": record.request_digest,
        "status": record.status.value,
        "version": record.version,
        "created_seq": record.created_seq,
        "attempt_number": record.attempt_number,
        "task_failure_count": record.task_failure_count,
        "next_attempt_at": record.next_attempt_at,
        "retry_scheduled_by_attempt": record.retry_scheduled_by_attempt,
        "retry_command_digest": record.retry_command_digest,
        "terminal_reason": record.terminal_reason,
        "lease_owner": record.lease_owner,
        "lease_expires_at": record.lease_expires_at,
        "fencing_token": record.fencing_token,
    }


def row_to_record(row: Mapping[str, Any]) -> TaskRecord:
    """``tasks`` 表的一行 → ``TaskRecord``。

    ``status`` 显式转成 ``TaskStatus``：strict 的 python 校验模式不接受裸字符串，
    而这一行的其余字段（``datetime`` / ``int`` / ``str``）本来就是原生类型，走 JSON
    往返反而会引入一次不必要的时间戳字符串化。因此这里**不**复用
    ``load_contract``——两条路径的输入形态不同。
    """
    return TaskRecord(
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        environment_id=row["environment_id"],
        actor=row["actor"],
        idempotency_key=row["idempotency_key"],
        request_digest=row["request_digest"],
        status=TaskStatus(row["status"]),
        version=row["version"],
        created_seq=row["created_seq"],
        attempt_number=row["attempt_number"],
        task_failure_count=row["task_failure_count"],
        next_attempt_at=row["next_attempt_at"],
        retry_scheduled_by_attempt=row["retry_scheduled_by_attempt"],
        retry_command_digest=row["retry_command_digest"],
        terminal_reason=row["terminal_reason"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        fencing_token=row["fencing_token"],
    )


def interaction_artifact_to_row(
    artifact: "AcceptedInteractionArtifact",
) -> dict[str, Any]:
    """AcceptedInteractionArtifact → 展开列与严格 JSONB。"""
    return {
        "task_id": artifact.task_id,
        "artifact_version": artifact.artifact_version,
        "draft": dump_contract(artifact.draft),
        "origin": artifact.origin,
        "provider": artifact.provider,
        "model": artifact.model,
        "provider_origin": artifact.provider_origin,
        "prompt_revision": artifact.prompt_revision,
        "schema_revision": artifact.schema_revision,
        "input_digest": artifact.input_digest,
        "result_digest": artifact.result_digest,
        "usage": dump_contract(artifact.usage),
        "created_at": artifact.created_at,
        "fencing_token": artifact.fencing_token,
    }


def row_to_interaction_artifact(
    row: Mapping[Any, Any]
) -> "AcceptedInteractionArtifact":
    """数据库行 → 自校验 AcceptedInteractionArtifact。"""
    from xiaowei_agent.persistence.model_artifacts import AcceptedInteractionArtifact

    return AcceptedInteractionArtifact(
        task_id=row["task_id"],
        artifact_version=row["artifact_version"],
        draft=load_contract(InteractionDraft, row["draft"]),
        origin=row["origin"],
        provider=row["provider"],
        model=row["model"],
        provider_origin=row["provider_origin"],
        prompt_revision=row["prompt_revision"],
        schema_revision=row["schema_revision"],
        input_digest=row["input_digest"],
        result_digest=row["result_digest"],
        usage=load_contract(ModelUsage, row["usage"]),
        created_at=row["created_at"],
        fencing_token=row["fencing_token"],
    )


def model_advisory_to_row(artifact: "StoredModelAdvisory") -> dict[str, Any]:
    """StoredModelAdvisory → 展开列与严格 JSONB。"""
    return {
        "task_id": artifact.task_id,
        "artifact_version": artifact.artifact_version,
        "advisory": dump_contract(artifact.advisory),
        "origin": artifact.origin,
        "provider": artifact.provider,
        "model": artifact.model,
        "provider_origin": artifact.provider_origin,
        "prompt_revision": artifact.prompt_revision,
        "schema_revision": artifact.schema_revision,
        "input_digest": artifact.input_digest,
        "result_digest": artifact.result_digest,
        "usage": dump_contract(artifact.usage),
        "created_at": artifact.created_at,
        "fencing_token": artifact.fencing_token,
    }


def row_to_model_advisory(row: Mapping[Any, Any]) -> "StoredModelAdvisory":
    """数据库行 → 自校验 StoredModelAdvisory。"""
    from xiaowei_agent.persistence.model_artifacts import StoredModelAdvisory

    return StoredModelAdvisory(
        task_id=row["task_id"],
        artifact_version=row["artifact_version"],
        advisory=load_contract(ModelAdvisory, row["advisory"]),
        origin=row["origin"],
        provider=row["provider"],
        model=row["model"],
        provider_origin=row["provider_origin"],
        prompt_revision=row["prompt_revision"],
        schema_revision=row["schema_revision"],
        input_digest=row["input_digest"],
        result_digest=row["result_digest"],
        usage=load_contract(ModelUsage, row["usage"]),
        created_at=row["created_at"],
        fencing_token=row["fencing_token"],
    )


def clarification_record_to_row(record: ClarificationRecord) -> dict[str, Any]:
    """ClarificationRecord → 展开列与严格 JSONB。"""
    return {
        "task_id": record.task_id,
        "record_version": record.record_version,
        "subject": dump_contract(record.subject),
        "reason_code": record.reason_code.value,
        "missing_fields": [field.value for field in record.missing_fields],
        "confirmed_slots": [dump_contract(slot) for slot in record.confirmed_slots],
        "created_at": record.created_at,
        "fencing_token": record.fencing_token,
    }


def row_to_clarification_record(row: Mapping[Any, Any]) -> ClarificationRecord:
    """数据库行 → 自校验 ClarificationRecord。"""
    from xiaowei_agent.contracts import (
        ClarificationField,
        ClarificationReasonCode,
        ConfirmedSlot,
    )

    return ClarificationRecord(
        task_id=row["task_id"],
        record_version=row["record_version"],
        subject=TypeAdapter(ClarificationSubject).validate_json(
            json.dumps(row["subject"])
        ),
        reason_code=ClarificationReasonCode(row["reason_code"]),
        missing_fields=tuple(
            ClarificationField(value) for value in row["missing_fields"]
        ),
        confirmed_slots=tuple(
            load_contract(ConfirmedSlot, slot) for slot in row["confirmed_slots"]
        ),
        created_at=row["created_at"],
        fencing_token=row["fencing_token"],
    )


def channel_binding_to_row(binding: "ChannelBinding") -> dict[str, Any]:
    """渠道绑定契约到列；只展开来源授权所需字段。"""
    return {
        "binding_id": binding.binding_id,
        "task_id": binding.task_id,
        "tenant_id": binding.tenant_id,
        "environment_id": binding.environment_id,
        "channel": binding.channel.value,
        "initiator_subject_ref": binding.initiator_subject_ref,
        "conversation_ref": binding.conversation_ref,
        "source_event_ref": binding.source_event_ref,
        "created_at": binding.created_at,
    }


def row_to_channel_binding(row: Mapping[Any, Any]) -> "ChannelBinding":
    """数据库列到渠道绑定契约。"""
    from xiaowei_agent.persistence.channel import ChannelBinding

    return ChannelBinding(
        binding_id=row["binding_id"],
        task_id=row["task_id"],
        tenant_id=row["tenant_id"],
        environment_id=row["environment_id"],
        channel=ChannelKind(row["channel"]),
        initiator_subject_ref=row["initiator_subject_ref"],
        conversation_ref=row["conversation_ref"],
        source_event_ref=row["source_event_ref"],
        created_at=row["created_at"],
    )


def projection_subscription_to_row(
    subscription: "ProjectionSubscription",
) -> dict[str, Any]:
    """投影订阅契约到列；可空 claim 与投影字段一律显式保留。"""
    return {
        "subscription_id": subscription.subscription_id,
        "task_id": subscription.task_id,
        "destination_kind": subscription.destination_kind.value,
        "destination_ref": subscription.destination_ref,
        "source_message_ref": subscription.source_message_ref,
        "state": subscription.state.value,
        "last_projected_task_version": subscription.last_projected_task_version,
        "attempt_number": subscription.attempt_number,
        "claim_owner": subscription.claim_owner,
        "claim_expires_at": subscription.claim_expires_at,
        "fencing_token": subscription.fencing_token,
        "next_attempt_at": subscription.next_attempt_at,
        "provider_failure_count": subscription.provider_failure_count,
        "last_error_code": None
        if subscription.last_error_code is None
        else subscription.last_error_code.value,
        "payload_digest": subscription.payload_digest,
    }


def row_to_projection_subscription(row: Mapping[Any, Any]) -> "ProjectionSubscription":
    """数据库列到自校验的投影订阅契约。"""
    from xiaowei_agent.persistence.channel import ProjectionSubscription

    error_code = row["last_error_code"]
    return ProjectionSubscription(
        subscription_id=row["subscription_id"],
        task_id=row["task_id"],
        destination_kind=DestinationKind(row["destination_kind"]),
        destination_ref=row["destination_ref"],
        source_message_ref=row["source_message_ref"],
        state=ProjectionState(row["state"]),
        last_projected_task_version=row["last_projected_task_version"],
        attempt_number=row["attempt_number"],
        claim_owner=row["claim_owner"],
        claim_expires_at=row["claim_expires_at"],
        fencing_token=row["fencing_token"],
        next_attempt_at=row["next_attempt_at"],
        provider_failure_count=row["provider_failure_count"],
        last_error_code=None
        if error_code is None
        else ProjectionErrorCode(error_code),
        payload_digest=row["payload_digest"],
    )


def oauth_state_to_row(state: "OAuthState") -> dict[str, Any]:
    """OAuth state 契约到列；明文 state 永不进入本映射。"""
    return {
        "state_digest": state.state_digest,
        "issued_at": state.issued_at,
        "expires_at": state.expires_at,
        "consumed_at": state.consumed_at,
    }


def row_to_oauth_state(row: Mapping[Any, Any]) -> "OAuthState":
    """数据库列到一次性 OAuth state 契约。"""
    from xiaowei_agent.persistence.web_session import OAuthState

    return OAuthState(
        state_digest=row["state_digest"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
    )


def web_session_to_row(session: "WebSession") -> dict[str, Any]:
    """浏览器 session 契约到列；只展开 cookie 摘要与时效事实。"""
    return {
        "session_digest": session.session_digest,
        "subject_ref": session.subject_ref,
        "issued_at": session.issued_at,
        "expires_at": session.expires_at,
        "revoked_at": session.revoked_at,
        "auth_source": session.auth_source.value,
        "public_origin_digest": session.public_origin_digest,
    }


def row_to_web_session(row: Mapping[Any, Any]) -> "WebSession":
    """数据库列到浏览器 session 契约。"""
    from xiaowei_agent.persistence.web_session import WebSession

    return WebSession(
        session_digest=row["session_digest"],
        subject_ref=row["subject_ref"],
        issued_at=row["issued_at"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        auth_source=IdentitySource(row["auth_source"]),
        public_origin_digest=row["public_origin_digest"],
    )


def web_return_intent_to_row(intent: WebReturnIntent) -> dict[str, Any]:
    """闭集返回意图到三个标量列；绝不产生 path/URL。"""
    return {
        "return_intent_kind": intent.kind.value,
        "return_intent_task_id": intent.task_id,
        "return_intent_request_id": intent.request_id,
    }


def row_to_web_return_intent(row: Mapping[Any, Any]) -> WebReturnIntent:
    """三个标量列到闭集返回意图；组合错误由契约拒绝。"""
    return WebReturnIntent(
        kind=WebReturnIntentKind(row["return_intent_kind"]),
        task_id=row["return_intent_task_id"],
        request_id=row["return_intent_request_id"],
    )


def step_execution_to_row(record: "StepExecutionRecord") -> dict[str, Any]:
    """步骤 journal 契约到列；可空终局字段一律显式保留。"""
    return {
        "task_id": record.task_id,
        "step_id": record.step_id,
        "attempt_count": record.attempt_count,
        "last_fencing_token": record.last_fencing_token,
        "result_status": None
        if record.result_status is None
        else record.result_status.value,
        "kind": None if record.kind is None else record.kind.value,
        "evidence_id": record.evidence_id,
        "commit_digest": record.commit_digest,
        "started_at": record.started_at,
        "committed_at": record.committed_at,
    }


def row_to_step_execution(row: Mapping[Any, Any]) -> "StepExecutionRecord":
    """数据库列到自描述步骤 journal 契约。"""
    from xiaowei_agent.persistence.store import StepExecutionRecord

    status = row["result_status"]
    kind = row["kind"]
    return StepExecutionRecord(
        task_id=row["task_id"],
        step_id=row["step_id"],
        attempt_count=row["attempt_count"],
        last_fencing_token=row["last_fencing_token"],
        result_status=None if status is None else StepResultStatus(status),
        kind=None if kind is None else StepOutcomeKind(kind),
        evidence_id=row["evidence_id"],
        commit_digest=row["commit_digest"],
        started_at=row["started_at"],
        committed_at=row["committed_at"],
    )


def admin_audit_event_to_row(event: "AdminAuditEvent") -> dict[str, Any]:
    """审计事件契约到列。

    effect 在行里是**平铺的两列**而不是嵌套对象：嵌套要么变成 JSON 列，要么变成
    一份只有这里知道的编码，而审计表明确不留任何自由文本通道。
    """
    return {
        "event_id": event.event_id,
        "operation_id": event.operation_id,
        "tenant_id": event.tenant_id,
        "environment_id": event.environment_id,
        "actor_user_id": event.actor_user_id,
        "actor": event.actor,
        "auth_source": event.auth_source.value,
        "action": event.action.value,
        "target_kind": event.target_kind.value,
        "target_ref_digest": event.target_ref_digest,
        "outcome": event.outcome.value,
        "reason_code": None if event.reason_code is None else event.reason_code.value,
        "effect_role": None if event.effect.role is None else event.effect.role.value,
        "effect_status": (
            None if event.effect.status is None else event.effect.status.value
        ),
        "created_at": event.created_at,
    }


def row_to_admin_audit_event(row: Mapping[Any, Any]) -> "AdminAuditEvent":
    """数据库列到审计事件契约；枚举**显式构造**。

    ``Contract`` 是 strict 模式，``str`` 不会自动变成 ``StrEnum``；漏一个显式构造
    就会在读回时炸，而读回发生在事后查证的那一刻。
    """
    from xiaowei_agent.contracts.admin_audit import AdminAuditEffect, AdminAuditEvent

    reason_code = row["reason_code"]
    effect_role = row["effect_role"]
    effect_status = row["effect_status"]
    return AdminAuditEvent(
        event_id=row["event_id"],
        operation_id=row["operation_id"],
        tenant_id=row["tenant_id"],
        environment_id=row["environment_id"],
        actor_user_id=row["actor_user_id"],
        actor=row["actor"],
        auth_source=IdentitySource(row["auth_source"]),
        action=AdminAuditAction(row["action"]),
        target_kind=AdminAuditTargetKind(row["target_kind"]),
        target_ref_digest=row["target_ref_digest"],
        outcome=AdminAuditOutcome(row["outcome"]),
        reason_code=None if reason_code is None else AdminAuditReasonCode(reason_code),
        effect=AdminAuditEffect(
            role=None if effect_role is None else ProductRole(effect_role),
            status=None if effect_status is None else UserStatus(effect_status),
        ),
        created_at=row["created_at"],
    )


def activation_request_to_row(request: ActivationRequest) -> dict[str, Any]:
    """激活申请契约到列；受控主体是唯一允许落库的明文 PII。"""
    intent_row = (
        {
            "return_intent_kind": None,
            "return_intent_task_id": None,
            "return_intent_request_id": None,
        }
        if request.return_intent is None
        else web_return_intent_to_row(request.return_intent)
    )
    return {
        "request_id": request.request_id,
        "tenant_id": request.tenant_id,
        "environment_id": request.environment_id,
        "provider": request.provider.value,
        "subject_ref": request.subject_ref,
        "subject_ref_digest": request.subject_ref_digest,
        "source": request.source.value,
        **intent_row,
        "source_event_digest": request.source_event_digest,
        "source_chat_digest": request.source_chat_digest,
        "requested_at": request.requested_at,
        "expires_at": request.expires_at,
        "status": request.status.value,
        "decided_at": request.decided_at,
        "decided_by": request.decided_by,
        "approved_role": (
            None if request.approved_role is None else request.approved_role.value
        ),
    }


def row_to_activation_request(row: Mapping[Any, Any]) -> ActivationRequest:
    """数据库列到激活申请；strict 契约要求显式恢复每个闭集枚举。"""
    provider = IdentitySource(row["provider"])
    if provider is not IdentitySource.FEISHU:
        raise ValueError("activation provider must be feishu")
    approved_role = row["approved_role"]
    intent_kind = row["return_intent_kind"]
    return_intent = (
        None
        if intent_kind is None
        else row_to_web_return_intent(row)
    )
    return ActivationRequest(
        request_id=row["request_id"],
        tenant_id=row["tenant_id"],
        environment_id=row["environment_id"],
        provider=IdentitySource.FEISHU,
        subject_ref=row["subject_ref"],
        subject_ref_digest=row["subject_ref_digest"],
        source=ActivationSource(row["source"]),
        return_intent=return_intent,
        source_event_digest=row["source_event_digest"],
        source_chat_digest=row["source_chat_digest"],
        requested_at=row["requested_at"],
        expires_at=row["expires_at"],
        status=ActivationStatus(row["status"]),
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        approved_role=(
            None if approved_role is None else ProductRole(approved_role)
        ),
    )
