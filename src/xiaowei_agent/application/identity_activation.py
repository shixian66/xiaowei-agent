"""身份激活的窄应用编排；授权判断与原子写仍由目录 Store 承担。"""

from xiaowei_agent.contracts import (
    ActivationSource,
    AdminAuditAction,
    AdminAuditTargetKind,
    IdentitySource,
)
from xiaowei_agent.contracts.activation import (
    ActivationRequest,
    CreateActivationCommand,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditDenial,
    AdminAuditEvent,
    AdminOperationContext,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.identity import (
    ApproveActivationCommand,
    RejectActivationCommand,
)
from xiaowei_agent.persistence.activation import ActivationStore
from xiaowei_agent.persistence.admin_audit import AdminAuditStore
from xiaowei_agent.persistence.identity import (
    UserDirectoryDecisionDeniedError,
    UserDirectoryStore,
)

ActivationDecision = ApproveActivationCommand | RejectActivationCommand


class IdentityActivationService:
    """创建申请并把决定交给目录的唯一原子授权写路径。"""

    def __init__(
        self,
        *,
        activations: ActivationStore,
        directory: UserDirectoryStore,
        audit: AdminAuditStore,
        tenant_id: str,
        environment_id: str,
    ) -> None:
        self._activations = activations
        self._directory = directory
        self._audit = audit
        self._tenant_id = tenant_id
        self._environment_id = environment_id

    async def request_web(self, *, subject_ref: str) -> ActivationRequest:
        """为一次未知 OAuth 身份创建或复用待办。"""
        return await self._activations.create_or_reuse(
            command=CreateActivationCommand(
                tenant_id=self._tenant_id,
                environment_id=self._environment_id,
                provider=IdentitySource.FEISHU,
                subject_ref=subject_ref,
                source=ActivationSource.WEB_LOGIN,
            )
        )

    async def request_group(
        self,
        *,
        subject_ref: str,
        event_ref: str,
        chat_ref: str,
    ) -> ActivationRequest:
        """为一次未知群发送者创建或复用待办。"""
        return await self._activations.create_or_reuse(
            command=CreateActivationCommand(
                tenant_id=self._tenant_id,
                environment_id=self._environment_id,
                provider=IdentitySource.FEISHU,
                subject_ref=subject_ref,
                source=ActivationSource.FEISHU_GROUP,
                source_event_ref=event_ref,
                source_chat_ref=chat_ref,
            )
        )

    async def decide(
        self,
        *,
        command: ActivationDecision,
        context: AdminOperationContext,
    ) -> tuple[AdminAuditEvent, ...]:
        """执行一次决定；拒绝时只追加闭集审计并保留原错误分类。"""
        try:
            return await self._directory.apply(command=command, context=context)
        except UserDirectoryDecisionDeniedError as error:
            action = (
                AdminAuditAction.ACTIVATION_APPROVED
                if isinstance(command, ApproveActivationCommand)
                else AdminAuditAction.ACTIVATION_REJECTED
            )
            await self._audit.append_denied(
                denial=AdminAuditDenial(
                    operation_id=context.operation_id,
                    tenant_id=command.tenant_id,
                    environment_id=command.environment_id,
                    actor_user_id=context.actor_user_id,
                    actor=context.actor,
                    auth_source=context.auth_source,
                    action=action,
                    target_kind=AdminAuditTargetKind.ACTIVATION,
                    target_ref_digest=admin_audit_target_digest(
                        target_kind=AdminAuditTargetKind.ACTIVATION,
                        target_ref=command.request_id,
                    ),
                    reason_code=error.reason_code,
                )
            )
            raise


async def approve_activation(
    *,
    service: IdentityActivationService,
    command: ApproveActivationCommand,
    context: AdminOperationContext,
) -> tuple[AdminAuditEvent, ...]:
    """一次性批准入口；不注册 HTTP 路由或常驻管理服务。"""
    return await service.decide(command=command, context=context)


async def reject_activation(
    *,
    service: IdentityActivationService,
    command: RejectActivationCommand,
    context: AdminOperationContext,
) -> tuple[AdminAuditEvent, ...]:
    """一次性拒绝入口；不注册 HTTP 路由或常驻管理服务。"""
    return await service.decide(command=command, context=context)


__all__ = [
    "IdentityActivationService",
    "approve_activation",
    "reject_activation",
]
