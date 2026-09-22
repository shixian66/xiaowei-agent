"""数据库身份目录到飞书入口主体的窄适配契约。"""

import datetime as dt
import inspect

import pytest

from xiaowei_agent.contracts import (
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    DirectoryPrincipalFacts,
    UserAccount,
    UserRoleAssignment,
)
from xiaowei_agent.governance.product_roles import channel_permissions
from xiaowei_agent.interfaces.directory_identity import DirectoryFeishuIdentityDirectory
from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityDirectory,
    FeishuIdentityNotFoundError,
    FeishuIdentityUnavailableError,
    StaticFeishuIdentityDirectory,
)
from xiaowei_agent.persistence.identity import UserDirectorySubjectUnavailableError

_NOW = dt.datetime(2026, 9, 22, tzinfo=dt.UTC)


def _facts(*, role: ProductRole = ProductRole.USER) -> DirectoryPrincipalFacts:
    return DirectoryPrincipalFacts(
        account=UserAccount(
            user_id="user-alice",
            actor="alice",
            display_name="Alice",
            status=UserStatus.ACTIVE,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        assignment=UserRoleAssignment(
            user_id="user-alice",
            tenant_id="dev-local",
            environment_id="dev",
            role=role,
            created_by="local-admin",
            created_at=_NOW,
            updated_at=_NOW,
        ),
    )


class _Directory:
    def __init__(self, result: DirectoryPrincipalFacts | Exception | None) -> None:
        self.result = result
        self.calls: list[tuple[IdentitySource, str, str, str]] = []

    async def resolve_by_subject(
        self,
        *,
        provider: IdentitySource,
        tenant_id: str,
        environment_id: str,
        subject_ref: str,
    ) -> DirectoryPrincipalFacts | None:
        self.calls.append((provider, tenant_id, environment_id, subject_ref))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_feishu_identity_protocol_and_implementations_are_async() -> None:
    assert inspect.iscoroutinefunction(FeishuIdentityDirectory.resolve)
    assert inspect.iscoroutinefunction(StaticFeishuIdentityDirectory.resolve)
    assert inspect.iscoroutinefunction(DirectoryFeishuIdentityDirectory.resolve)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [ProductRole.USER, ProductRole.OPERATOR, ProductRole.ADMIN])
async def test_directory_adapter_uses_the_shared_role_permission_mapping(
    role: ProductRole,
) -> None:
    store = _Directory(_facts(role=role))
    adapter = DirectoryFeishuIdentityDirectory(
        directory=store,
        tenant_id="dev-local",
        environment_id="dev",
    )

    principal = await adapter.resolve(subject_ref="ou_alice")

    assert store.calls == [(IdentitySource.FEISHU, "dev-local", "dev", "ou_alice")]
    assert principal.tenant_id == "dev-local"
    assert principal.environment_id == "dev"
    assert principal.actor == "alice"
    assert principal.source is IdentitySource.FEISHU
    assert principal.subject_ref == "ou_alice"
    assert principal.permissions == channel_permissions(role=role)


@pytest.mark.asyncio
async def test_missing_subject_is_the_only_state_that_can_enter_activation() -> None:
    adapter = DirectoryFeishuIdentityDirectory(
        directory=_Directory(None),
        tenant_id="dev-local",
        environment_id="dev",
    )

    with pytest.raises(
        FeishuIdentityNotFoundError, match=r"^feishu identity not found$"
    ):
        await adapter.resolve(subject_ref="ou_missing")


@pytest.mark.asyncio
async def test_bound_but_unavailable_subject_is_not_reclassified_as_missing() -> None:
    adapter = DirectoryFeishuIdentityDirectory(
        directory=_Directory(
            UserDirectorySubjectUnavailableError("the bound subject is unavailable")
        ),
        tenant_id="dev-local",
        environment_id="dev",
    )

    with pytest.raises(
        FeishuIdentityUnavailableError, match=r"^feishu identity unavailable$"
    ):
        await adapter.resolve(subject_ref="ou_disabled")
