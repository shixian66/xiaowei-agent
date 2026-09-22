"""由持久化身份目录支撑的飞书入口身份适配器。"""

from xiaowei_agent.contracts import AuthenticatedPrincipal, IdentitySource
from xiaowei_agent.governance.product_roles import channel_permissions
from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityNotFoundError,
    FeishuIdentityUnavailableError,
)
from xiaowei_agent.persistence.identity import (
    UserDirectoryStore,
    UserDirectorySubjectUnavailableError,
)


class DirectoryFeishuIdentityDirectory:
    """每次请求都从目录重建主体，使禁用与降权立即生效。"""

    def __init__(
        self,
        *,
        directory: UserDirectoryStore,
        tenant_id: str,
        environment_id: str,
    ) -> None:
        self._directory = directory
        self._tenant_id = tenant_id
        self._environment_id = environment_id

    async def resolve(self, *, subject_ref: str) -> AuthenticatedPrincipal:
        """区分真正未登记与已绑定但不可用；两类错误均不回显主体。"""
        try:
            facts = await self._directory.resolve_by_subject(
                provider=IdentitySource.FEISHU,
                tenant_id=self._tenant_id,
                environment_id=self._environment_id,
                subject_ref=subject_ref,
            )
        except UserDirectorySubjectUnavailableError:
            raise FeishuIdentityUnavailableError(
                "feishu identity unavailable"
            ) from None
        if facts is None:
            raise FeishuIdentityNotFoundError("feishu identity not found")
        return AuthenticatedPrincipal(
            tenant_id=self._tenant_id,
            environment_id=self._environment_id,
            actor=facts.account.actor,
            source=IdentitySource.FEISHU,
            subject_ref=subject_ref,
            permissions=channel_permissions(role=facts.assignment.role),
        )


__all__ = ["DirectoryFeishuIdentityDirectory"]
