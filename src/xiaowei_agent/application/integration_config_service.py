"""唯一配置写服务：AI/飞书两域的保存、清除与连接测试（W4a），resources 域的资源维护（W4b）。

Web handler 只做认证、CSRF 与 body 解析，然后调用这里；它不读写文件、不写审计、不记
测试结果。文件访问经本模块定义的窄 :class:`IntegrationConfigRepository` port，由
``interfaces/integration_config_file.py`` 的文件 adapter 实现——application 不反向
import interfaces（``_conformance.py`` 静态锚定 adapter 与 port 的结构兼容）。

**文件与审计不在一个事务里，因此承诺的是顺序**：先 ``STARTED``，再执行文件/探针动作，
最后 ``SUCCEEDED``/``FAILED``。``STARTED`` 写不进去时文件与探针调用都为 0；终态写不进去
时统一 unavailable，绝不回报成功——文件可能已落盘的事实由遗留的 ``STARTED`` 表示。

同一服务实例用**一把**异步锁覆盖每次 read-modify-write：两个并发保存不会读到同一
generation 后互相覆盖。文件读写（含 fsync）经 ``asyncio.to_thread`` 离开事件循环，
因此读与写之间确实会交出控制权——锁是承重的，不是装饰。本阶段只有一个 Web 写进程，
不建设多进程 CAS。

operation id 只由服务端 trace id 派生并加 ``w4:`` 前缀；方法签名里没有接收客户端
operation id 的位置（唯一例外 :meth:`finish_oauth_test` 的 operation id 取自服务端
state 存储，而不是请求）。
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TypeVar

from pydantic import ValidationError

from xiaowei_agent.application.admin_identity import AdminIdentityActor
from xiaowei_agent.application.integration_state import web_holds_feishu_generation
from xiaowei_agent.contracts import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    AdminCapability,
    AiConfig,
    ConfigDomain,
    FeishuConfig,
    FeishuIntegration,
    GeminiIntegration,
    IdentitySource,
    ProductRole,
    TestStatus,
)
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditDenial,
    AdminAuditStart,
    AdminAuditTerminal,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.resource_config import (
    PrometheusAuthMode,
    PrometheusResource,
    ResourceEnvironment,
    ResourcesConfig,
    StarRocksResource,
    TlsMode,
    secret_required_on_create,
)
from xiaowei_agent.persistence.admin_audit import AdminAuditStore, stage_event_id
from xiaowei_agent.persistence.provider_state import (
    CheckName,
    ProviderStateStore,
    ProviderTestErrorCode,
    RecordTestCommand,
)

OPERATION_ID_PREFIX: Final[str] = "w4:"

_T = TypeVar("_T")
_ConfigT = TypeVar("_ConfigT", AiConfig, FeishuConfig)
_ReadT = TypeVar("_ReadT", AiConfig, FeishuConfig, ResourcesConfig)


class IntegrationConfigUnreadableError(RuntimeError):
    """已存在的域文件读不了或不满足 schema。由文件 adapter 抛出，文本固定。"""

    def __init__(self) -> None:
        super().__init__("integration config unreadable")


class IntegrationConfigWriteError(RuntimeError):
    """域文件原子写入失败（含写后持久性未知）。由文件 adapter 抛出，文本固定。"""

    def __init__(self) -> None:
        super().__init__("integration config write failed")


class IntegrationConfigForbiddenError(RuntimeError):
    """已认证主体不是本地管理员或缺对应能力；拒绝审计已落库。"""

    def __init__(self) -> None:
        super().__init__("integration config forbidden")


class OAuthTestGenerationDriftError(RuntimeError):
    """OAuth 测试回调时，文件或 Web 已加载的飞书代次已不是 state 绑定的那一代。

    不交换 code、不记任何测试结果；审计已写 ``FAILED``。不携带任何代次数值。
    """

    def __init__(self) -> None:
        super().__init__("oauth test generation drifted")


class ResourceNotFoundError(RuntimeError):
    """按 ID 找不到资源；审计已写 ``FAILED/target_not_found``。不携带 ID。"""

    def __init__(self) -> None:
        super().__init__("resource not found")


class ResourceConflictError(RuntimeError):
    """服务端生成的资源 ID 撞上了文档里已有的 ID；不覆盖、不重试。"""

    def __init__(self) -> None:
        super().__init__("resource conflict")


class ResourceRejectedError(RuntimeError):
    """合并后的资源或整份文档不满足契约（含 Secret 缺失、第 101 个资源、kind 不符）。"""

    def __init__(self) -> None:
        super().__init__("resource rejected")


class IntegrationConfigUnavailableError(RuntimeError):
    """审计、文件或测试结果存储不可用，结果不能被当成成功。"""

    def __init__(self) -> None:
        super().__init__("integration config unavailable")


class IntegrationConfigRepository(Protocol):
    """两个域的显式文件读写；没有动态 domain、schema、path 或 serializer 参数。

    ``read_*`` 在文件不存在时返回 ``None``（干净部署的起点），存在但坏时抛
    :class:`IntegrationConfigUnreadableError`；``write_*`` 失败抛
    :class:`IntegrationConfigWriteError`。三个域各一对显式方法，resources 是 W4b 加入的。
    """

    def read_ai(self) -> AiConfig | None: ...

    def write_ai(self, *, config: AiConfig) -> None: ...

    def read_feishu(self) -> FeishuConfig | None: ...

    def write_feishu(self, *, config: FeishuConfig) -> None: ...

    def read_resources(self) -> ResourcesConfig | None: ...

    def write_resources(self, *, config: ResourcesConfig) -> None: ...


class ProbeVerdict(Protocol):
    """一次探针的闭集结果；``interfaces.provider_probe.ProbeOutcome`` 结构兼容。"""

    @property
    def status(self) -> TestStatus: ...

    @property
    def duration_ms(self) -> int: ...

    @property
    def error_code(self) -> ProviderTestErrorCode | None: ...


@dataclass(frozen=True)
class GeminiUpdate:
    """AI 域的部分更新；``None`` 只表示"未携带、保留原值"。

    显式 ``null`` 与空串在 Web 请求模型里就被拒绝，清除 Secret 只能走独立的清除动作。
    """

    enabled: bool | None = None
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class FeishuUpdate:
    """飞书域的部分更新；语义同 :class:`GeminiUpdate`。"""

    enabled: bool | None = None
    app_id: str | None = None
    app_secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class StarRocksDraft:
    """新建一个 StarRocks 资源；ID 与 kind 由服务端决定，不在这里。"""

    environment: ResourceEnvironment
    display_name: str
    host: str
    port: int
    database: str
    username: str
    tls_mode: TlsMode
    enabled: bool
    password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PrometheusDraft:
    """新建一个 Prometheus 资源；ID 与 kind 由服务端决定，不在这里。"""

    environment: ResourceEnvironment
    display_name: str
    base_url: str
    auth_mode: PrometheusAuthMode
    tls_mode: TlsMode
    enabled: bool
    username: str | None = None
    secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ResourceUpdate:
    """按 ID 修改一个资源；``None`` 只表示"未携带、保留原值"。

    字段是两种 kind 的并集：携带了存量 kind 不认识的字段（如给 StarRocks 传 ``base_url``）
    即整体拒绝，而不是静默忽略。显式清除 Secret 走独立确认动作，不在这里。
    """

    environment: ResourceEnvironment | None = None
    display_name: str | None = None
    enabled: bool | None = None
    tls_mode: TlsMode | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    base_url: str | None = None
    auth_mode: PrometheusAuthMode | None = None
    secret: str | None = field(default=None, repr=False)


_STARROCKS_ONLY: Final = frozenset({"host", "port", "database", "password"})
_PROMETHEUS_ONLY: Final = frozenset({"base_url", "auth_mode", "secret"})


@dataclass(frozen=True)
class ResourceSaved:
    """一次成功资源写入后的资源 ID 与 resources 新 generation；不含任何参数值。"""

    resource_id: str
    generation: int


@dataclass(frozen=True)
class ConfigSaved:
    """一次成功写入后的域与它的新 generation；不含任何配置值。"""

    domain: ConfigDomain
    generation: int


def _operation_id(trace_id: str) -> str:
    return f"{OPERATION_ID_PREFIX}{trace_id}"


def _config_target(domain: ConfigDomain) -> str:
    return admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.CONFIG, target_ref=f"config_domain:{domain.value}"
    )


def _resource_target(resource_id: str) -> str:
    return admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.CONFIG, target_ref=f"resource:{resource_id}"
    )


def new_resource_id() -> str:
    """服务端资源 ID：32 位小写十六进制，来自 CSPRNG。"""
    return secrets.token_hex(16)


def _starrocks_from_draft(resource_id: str, draft: StarRocksDraft) -> StarRocksResource:
    return StarRocksResource(
        kind="starrocks",
        resource_id=resource_id,
        environment=draft.environment,
        display_name=draft.display_name,
        host=draft.host,
        port=draft.port,
        database=draft.database,
        username=draft.username,
        password=draft.password,
        tls_mode=draft.tls_mode,
        enabled=draft.enabled,
    )


def _prometheus_from_draft(resource_id: str, draft: PrometheusDraft) -> PrometheusResource:
    return PrometheusResource(
        kind="prometheus",
        resource_id=resource_id,
        environment=draft.environment,
        display_name=draft.display_name,
        base_url=draft.base_url,
        auth_mode=draft.auth_mode,
        username=draft.username,
        secret=draft.secret,
        tls_mode=draft.tls_mode,
        enabled=draft.enabled,
    )


def _with_secret(
    resource: StarRocksResource | PrometheusResource,
) -> dict[str, Any]:
    """资源的全部字段**含 Secret**：``model_dump()`` 会按 ``exclude=True`` 丢掉它。"""
    values = resource.model_dump()
    if isinstance(resource, StarRocksResource):
        values["password"] = resource.password
    else:
        values["secret"] = resource.secret
    return values


def _merged_resource(
    current: StarRocksResource | PrometheusResource, update: ResourceUpdate
) -> StarRocksResource | PrometheusResource:
    """把部分更新合进存量资源并**交回契约重新校验**；不适用于该 kind 的字段直接拒绝。"""
    carried = {
        name: value
        for name, value in vars(update).items()
        if value is not None
    }
    foreign = _PROMETHEUS_ONLY if isinstance(current, StarRocksResource) else _STARROCKS_ONLY
    if carried.keys() & foreign:
        raise ResourceRejectedError
    values = _with_secret(current) | carried
    if isinstance(current, StarRocksResource):
        return StarRocksResource.model_validate(values)
    # 认证方式决定哪些字段存在：切走 basic 时 username 随之消失，切到 none 时 secret 也消失。
    # 这不是第二套规则，只是把"新模式禁止的旧值"删掉，余下组合仍由契约校验。
    if values["auth_mode"] != "basic":
        values["username"] = None
    if values["auth_mode"] == "none":
        values["secret"] = None
    return PrometheusResource.model_validate(values)


def _without_secret(
    resource: StarRocksResource | PrometheusResource,
) -> StarRocksResource | PrometheusResource:
    values = _with_secret(resource)
    if isinstance(resource, StarRocksResource):
        values["password"] = None
        return StarRocksResource.model_validate(values)
    values["secret"] = None
    return PrometheusResource.model_validate(values)


def _probe_target(check: CheckName) -> str:
    return admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.CONFIG, target_ref=f"connection_test:{check.value}"
    )


def _denial_reason(
    actor: AdminIdentityActor, capability: AdminCapability
) -> AdminAuditReasonCode | None:
    """``None`` 表示放行：必须同时是 ADMIN、本地管理员来源且持有该能力。"""
    if actor.role is not ProductRole.ADMIN:
        return AdminAuditReasonCode.ACTOR_NOT_ADMIN
    if actor.auth_source is not IdentitySource.LOCAL_ADMIN:
        return AdminAuditReasonCode.AUTH_SOURCE_NOT_ALLOWED
    if capability not in actor.capabilities:
        return AdminAuditReasonCode.ACTOR_NOT_ADMIN
    return None


def _merged_gemini(current: GeminiIntegration, update: GeminiUpdate) -> GeminiIntegration:
    return GeminiIntegration(
        enabled=current.enabled if update.enabled is None else update.enabled,
        api_key=current.api_key if update.api_key is None else update.api_key,
    )


def _merged_feishu(current: FeishuIntegration, update: FeishuUpdate) -> FeishuIntegration:
    return FeishuIntegration(
        enabled=current.enabled if update.enabled is None else update.enabled,
        app_id=current.app_id if update.app_id is None else update.app_id,
        app_secret=current.app_secret if update.app_secret is None else update.app_secret,
    )


def _replace_one(
    existing: tuple[StarRocksResource | PrometheusResource, ...],
    resource_id: str,
    transform: Callable[
        [StarRocksResource | PrometheusResource], StarRocksResource | PrometheusResource
    ],
) -> tuple[StarRocksResource | PrometheusResource, ...]:
    """只替换目标资源、保持其余对象与顺序不变；找不到即 :class:`ResourceNotFoundError`。"""
    positions = [index for index, item in enumerate(existing) if item.resource_id == resource_id]
    if not positions:
        raise ResourceNotFoundError
    (position,) = positions
    return (*existing[:position], transform(existing[position]), *existing[position + 1 :])


class IntegrationConfigService:
    """两域配置的唯一写编排；读取视图也经它，但只读不审计。"""

    def __init__(
        self,
        *,
        repository: IntegrationConfigRepository,
        audit: AdminAuditStore,
        provider_state: ProviderStateStore,
        resource_id_factory: Callable[[], str] = new_resource_id,
    ) -> None:
        self._repository = repository
        self._audit = audit
        self._provider_state = provider_state
        self._new_resource_id = resource_id_factory
        # 必须随实例走，不能是模块级：每个 Web 进程只有一个服务实例。
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 读

    async def read_ai(self) -> AiConfig | None:
        """读 AI 域；不存在为 ``None``，存在但坏统一 unavailable（绝不降级成未配置）。"""
        return await self._read(self._repository.read_ai)

    async def read_feishu(self) -> FeishuConfig | None:
        return await self._read(self._repository.read_feishu)

    async def read_resources(self) -> ResourcesConfig | None:
        return await self._read(self._repository.read_resources)

    @staticmethod
    async def _read(reader: Callable[[], _ReadT | None]) -> _ReadT | None:
        try:
            return await asyncio.to_thread(reader)
        except IntegrationConfigUnreadableError:
            raise IntegrationConfigUnavailableError from None

    # ------------------------------------------------------------------ 审计原语

    async def _deny_unless_allowed(
        self,
        *,
        actor: AdminIdentityActor,
        capability: AdminCapability,
        operation_id: str,
        action: AdminAuditAction,
        target_ref_digest: str,
    ) -> None:
        reason = _denial_reason(actor, capability)
        if reason is None:
            return
        try:
            await self._audit.append_denied(
                denial=AdminAuditDenial(
                    operation_id=operation_id,
                    tenant_id=actor.tenant_id,
                    environment_id=actor.environment_id,
                    actor_user_id=actor.user_id,
                    actor=actor.actor,
                    auth_source=actor.auth_source,
                    action=action,
                    target_kind=AdminAuditTargetKind.CONFIG,
                    target_ref_digest=target_ref_digest,
                    reason_code=reason,
                )
            )
        except Exception:
            # 拒绝都记不下来时同样 fail-closed：文件、state 与网络调用保持为 0。
            raise IntegrationConfigUnavailableError from None
        raise IntegrationConfigForbiddenError

    async def _start(
        self,
        *,
        actor: AdminIdentityActor,
        operation_id: str,
        action: AdminAuditAction,
        target_ref_digest: str,
    ) -> None:
        try:
            await self._audit.append_started(
                start=AdminAuditStart(
                    operation_id=operation_id,
                    tenant_id=actor.tenant_id,
                    environment_id=actor.environment_id,
                    actor_user_id=actor.user_id,
                    actor=actor.actor,
                    auth_source=actor.auth_source,
                    action=action,
                    target_kind=AdminAuditTargetKind.CONFIG,
                    target_ref_digest=target_ref_digest,
                )
            )
        except Exception:
            raise IntegrationConfigUnavailableError from None

    async def _terminal(
        self, *, operation_id: str, reason: AdminAuditReasonCode | None
    ) -> None:
        try:
            await self._audit.append_terminal(
                terminal=AdminAuditTerminal(
                    operation_id=operation_id,
                    outcome=(
                        AdminAuditOutcome.SUCCEEDED
                        if reason is None
                        else AdminAuditOutcome.FAILED
                    ),
                    reason_code=reason,
                )
            )
        except Exception:
            raise IntegrationConfigUnavailableError from None

    async def _fail(self, *, operation_id: str, reason: AdminAuditReasonCode) -> None:
        """写 ``FAILED`` 终态；即使终态也写不进去，调用方仍然只会看到 unavailable。"""
        try:
            await self._terminal(operation_id=operation_id, reason=reason)
        except IntegrationConfigUnavailableError:
            pass

    # ------------------------------------------------------------------ 保存与清除

    async def _mutate(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        domain: ConfigDomain,
        action: AdminAuditAction,
        reader: Callable[[], _ConfigT | None],
        build: Callable[[_ConfigT | None], _ConfigT],
        writer: Callable[[_ConfigT], None],
    ) -> ConfigSaved:
        operation_id = _operation_id(trace_id)
        target = _config_target(domain)
        await self._deny_unless_allowed(
            actor=actor,
            capability=AdminCapability.MANAGE_INTEGRATIONS,
            operation_id=operation_id,
            action=action,
            target_ref_digest=target,
        )
        async with self._lock:
            await self._start(
                actor=actor, operation_id=operation_id, action=action, target_ref_digest=target
            )
            try:
                current = await asyncio.to_thread(reader)
            except IntegrationConfigUnreadableError:
                # 存在但坏：绝不按"未配置"从 generation 0 重写一份覆盖掉它。
                await self._fail(
                    operation_id=operation_id, reason=AdminAuditReasonCode.CONFIG_INVALID
                )
                raise IntegrationConfigUnavailableError from None
            updated = build(current)
            try:
                await asyncio.to_thread(writer, updated)
            except IntegrationConfigWriteError:
                await self._fail(
                    operation_id=operation_id, reason=AdminAuditReasonCode.FILE_IO_FAILED
                )
                raise IntegrationConfigUnavailableError from None
            await self._terminal(operation_id=operation_id, reason=None)
        return ConfigSaved(domain=domain, generation=updated.generation)

    async def save_ai(
        self, *, actor: AdminIdentityActor, trace_id: str, update: GeminiUpdate
    ) -> ConfigSaved:
        """合并 AI 域并推进一次 AI generation；不触碰飞书域。"""

        def build(current: AiConfig | None) -> AiConfig:
            return AiConfig(
                generation=(0 if current is None else current.generation) + 1,
                gemini=_merged_gemini(
                    GeminiIntegration() if current is None else current.gemini, update
                ),
            )

        return await self._mutate(
            actor=actor,
            trace_id=trace_id,
            domain=ConfigDomain.AI,
            action=AdminAuditAction.CONFIG_SAVED,
            reader=self._repository.read_ai,
            build=build,
            writer=lambda config: self._repository.write_ai(config=config),
        )

    async def save_feishu(
        self, *, actor: AdminIdentityActor, trace_id: str, update: FeishuUpdate
    ) -> ConfigSaved:
        """合并飞书域并推进一次飞书 generation；不触碰 AI 域。"""

        def build(current: FeishuConfig | None) -> FeishuConfig:
            return FeishuConfig(
                generation=(0 if current is None else current.generation) + 1,
                feishu=_merged_feishu(
                    FeishuIntegration() if current is None else current.feishu, update
                ),
            )

        return await self._mutate(
            actor=actor,
            trace_id=trace_id,
            domain=ConfigDomain.FEISHU,
            action=AdminAuditAction.CONFIG_SAVED,
            reader=self._repository.read_feishu,
            build=build,
            writer=lambda config: self._repository.write_feishu(config=config),
        )

    async def clear_ai(self, *, actor: AdminIdentityActor, trace_id: str) -> ConfigSaved:
        """把 AI 域整段重置为默认值：留一半配置会让页面显示"已配置"而实际不可用。"""

        def build(current: AiConfig | None) -> AiConfig:
            return AiConfig(
                generation=(0 if current is None else current.generation) + 1,
                gemini=GeminiIntegration(),
            )

        return await self._mutate(
            actor=actor,
            trace_id=trace_id,
            domain=ConfigDomain.AI,
            action=AdminAuditAction.CONFIG_CLEARED,
            reader=self._repository.read_ai,
            build=build,
            writer=lambda config: self._repository.write_ai(config=config),
        )

    async def clear_feishu(self, *, actor: AdminIdentityActor, trace_id: str) -> ConfigSaved:
        def build(current: FeishuConfig | None) -> FeishuConfig:
            return FeishuConfig(
                generation=(0 if current is None else current.generation) + 1,
                feishu=FeishuIntegration(),
            )

        return await self._mutate(
            actor=actor,
            trace_id=trace_id,
            domain=ConfigDomain.FEISHU,
            action=AdminAuditAction.CONFIG_CLEARED,
            reader=self._repository.read_feishu,
            build=build,
            writer=lambda config: self._repository.write_feishu(config=config),
        )

    # ------------------------------------------------------------------ 资源（W4b）

    async def _mutate_resources(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        action: AdminAuditAction,
        resource_id: str,
        change: Callable[
            [tuple[StarRocksResource | PrometheusResource, ...]],
            tuple[StarRocksResource | PrometheusResource, ...],
        ],
    ) -> ResourceSaved:
        """资源写入的共同骨架：拒绝审计 → 锁 → STARTED → 读 → 变换 → 整份写回 → 终态。

        ``change`` 只返回新的资源元组；它抛 :class:`ResourceNotFoundError` /
        :class:`ResourceConflictError` / :class:`ResourceRejectedError` 或契约
        ``ValidationError`` 时，文件与 generation 都不动，审计写对应的闭集 ``FAILED``。
        其余资源对象原样复用：不重排、不重写、不清空它们的 Secret。
        """
        operation_id = _operation_id(trace_id)
        target = _resource_target(resource_id)
        async with self._lock:
            await self._start(
                actor=actor, operation_id=operation_id, action=action, target_ref_digest=target
            )
            try:
                current = await asyncio.to_thread(self._repository.read_resources)
            except IntegrationConfigUnreadableError:
                await self._fail(
                    operation_id=operation_id, reason=AdminAuditReasonCode.CONFIG_INVALID
                )
                raise IntegrationConfigUnavailableError from None
            existing = () if current is None else current.resources
            try:
                updated = ResourcesConfig(
                    generation=(0 if current is None else current.generation) + 1,
                    resources=change(existing),
                )
            except ResourceNotFoundError:
                await self._fail(
                    operation_id=operation_id, reason=AdminAuditReasonCode.TARGET_NOT_FOUND
                )
                raise
            except ResourceConflictError:
                await self._fail(operation_id=operation_id, reason=AdminAuditReasonCode.CONFLICT)
                raise
            except (ResourceRejectedError, ValidationError):
                await self._fail(
                    operation_id=operation_id, reason=AdminAuditReasonCode.CONFIG_INVALID
                )
                raise ResourceRejectedError from None
            try:
                await asyncio.to_thread(
                    lambda: self._repository.write_resources(config=updated)
                )
            except IntegrationConfigWriteError:
                await self._fail(
                    operation_id=operation_id, reason=AdminAuditReasonCode.FILE_IO_FAILED
                )
                raise IntegrationConfigUnavailableError from None
            await self._terminal(operation_id=operation_id, reason=None)
        return ResourceSaved(resource_id=resource_id, generation=updated.generation)

    async def _deny_resource_change(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        action: AdminAuditAction,
        resource_id: str,
    ) -> None:
        await self._deny_unless_allowed(
            actor=actor,
            capability=AdminCapability.MANAGE_INTEGRATIONS,
            operation_id=_operation_id(trace_id),
            action=action,
            target_ref_digest=_resource_target(resource_id),
        )

    async def create_resource(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        draft: StarRocksDraft | PrometheusDraft,
    ) -> ResourceSaved:
        """新建一个资源：ID 由注入的 factory 在服务端生成，撞上已有 ID 即闭集冲突。

        被拒的主体连 ID 都不生成（拒绝审计的 target 是固定的 ``resource:new``）。
        """
        if _denial_reason(actor, AdminCapability.MANAGE_INTEGRATIONS) is not None:
            await self._deny_unless_allowed(
                actor=actor,
                capability=AdminCapability.MANAGE_INTEGRATIONS,
                operation_id=_operation_id(trace_id),
                action=AdminAuditAction.CONFIG_SAVED,
                target_ref_digest=_resource_target("new"),
            )
        resource_id = self._new_resource_id()

        def change(
            existing: tuple[StarRocksResource | PrometheusResource, ...],
        ) -> tuple[StarRocksResource | PrometheusResource, ...]:
            if any(item.resource_id == resource_id for item in existing):
                raise ResourceConflictError
            created = (
                _starrocks_from_draft(resource_id, draft)
                if isinstance(draft, StarRocksDraft)
                else _prometheus_from_draft(resource_id, draft)
            )
            if secret_required_on_create(created) and not created.secret_configured:
                raise ResourceRejectedError
            return (*existing, created)

        return await self._mutate_resources(
            actor=actor,
            trace_id=trace_id,
            action=AdminAuditAction.CONFIG_SAVED,
            resource_id=resource_id,
            change=change,
        )

    async def update_resource(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        resource_id: str,
        update: ResourceUpdate,
    ) -> ResourceSaved:
        """按 ID 合并修改；省略的字段（含 Secret）保留，kind 与 ID 不可改。"""
        await self._deny_resource_change(
            actor=actor,
            trace_id=trace_id,
            action=AdminAuditAction.CONFIG_SAVED,
            resource_id=resource_id,
        )

        def change(
            existing: tuple[StarRocksResource | PrometheusResource, ...],
        ) -> tuple[StarRocksResource | PrometheusResource, ...]:
            return _replace_one(
                existing, resource_id, lambda current: _merged_resource(current, update)
            )

        return await self._mutate_resources(
            actor=actor,
            trace_id=trace_id,
            action=AdminAuditAction.CONFIG_SAVED,
            resource_id=resource_id,
            change=change,
        )

    async def clear_resource_secret(
        self, *, actor: AdminIdentityActor, trace_id: str, resource_id: str
    ) -> ResourceSaved:
        """显式清除一个资源的 Secret；资源本身与其他资源的 Secret 都保留。"""
        await self._deny_resource_change(
            actor=actor,
            trace_id=trace_id,
            action=AdminAuditAction.CONFIG_CLEARED,
            resource_id=resource_id,
        )
        return await self._mutate_resources(
            actor=actor,
            trace_id=trace_id,
            action=AdminAuditAction.CONFIG_CLEARED,
            resource_id=resource_id,
            change=lambda existing: _replace_one(existing, resource_id, _without_secret),
        )

    async def delete_resource(
        self, *, actor: AdminIdentityActor, trace_id: str, resource_id: str
    ) -> ResourceSaved:
        """显式删除一个资源；其余资源原样保留。"""
        await self._deny_resource_change(
            actor=actor,
            trace_id=trace_id,
            action=AdminAuditAction.CONFIG_CLEARED,
            resource_id=resource_id,
        )

        def change(
            existing: tuple[StarRocksResource | PrometheusResource, ...],
        ) -> tuple[StarRocksResource | PrometheusResource, ...]:
            kept = tuple(item for item in existing if item.resource_id != resource_id)
            if len(kept) == len(existing):
                raise ResourceNotFoundError
            return kept

        return await self._mutate_resources(
            actor=actor,
            trace_id=trace_id,
            action=AdminAuditAction.CONFIG_CLEARED,
            resource_id=resource_id,
            change=change,
        )

    # ------------------------------------------------------------------ 同步探针

    async def _record(
        self, *, check: CheckName, generation: int | None, verdict: ProbeVerdict
    ) -> None:
        """有可信代次才落测试结果；没有文件时没有代次可归属，不伪造。"""
        if generation is None:
            return
        try:
            await self._provider_state.record_test(
                command=RecordTestCommand(
                    check_name=check,
                    generation=generation,
                    status=verdict.status,
                    duration_ms=verdict.duration_ms,
                    error_code=verdict.error_code,
                )
            )
        except Exception:
            # 结果存不下时只留 STARTED：它诚实地表示"做了，但结果未知"。
            raise IntegrationConfigUnavailableError from None

    async def _finish_probe(
        self,
        *,
        operation_id: str,
        check: CheckName,
        generation: int | None,
        verdict: ProbeVerdict,
    ) -> None:
        await self._record(check=check, generation=generation, verdict=verdict)
        await self._terminal(
            operation_id=operation_id,
            reason=None if verdict.status == "passed" else AdminAuditReasonCode.PROBE_FAILED,
        )

    async def _probe(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        check: CheckName,
        reader: Callable[[], _ConfigT | None],
        probe: Callable[[_ConfigT | None], Awaitable[ProbeVerdict]],
    ) -> ProbeVerdict:
        operation_id = _operation_id(trace_id)
        target = _probe_target(check)
        await self._deny_unless_allowed(
            actor=actor,
            capability=AdminCapability.RUN_CONNECTION_TESTS,
            operation_id=operation_id,
            action=AdminAuditAction.CONNECTION_TESTED,
            target_ref_digest=target,
        )
        await self._start(
            actor=actor,
            operation_id=operation_id,
            action=AdminAuditAction.CONNECTION_TESTED,
            target_ref_digest=target,
        )
        try:
            config = await asyncio.to_thread(reader)
        except IntegrationConfigUnreadableError:
            await self._fail(
                operation_id=operation_id, reason=AdminAuditReasonCode.CONFIG_INVALID
            )
            raise IntegrationConfigUnavailableError from None
        verdict = await probe(config)
        await self._finish_probe(
            operation_id=operation_id,
            check=check,
            generation=None if config is None else config.generation,
            verdict=verdict,
        )
        return verdict

    async def test_ai_connection(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        probe: Callable[[AiConfig | None], Awaitable[ProbeVerdict]],
    ) -> ProbeVerdict:
        """包住 Gemini 同步探针；测试代次取自 AI 域。"""
        return await self._probe(
            actor=actor,
            trace_id=trace_id,
            check=CheckName.GEMINI_CONNECTION,
            reader=self._repository.read_ai,
            probe=probe,
        )

    async def test_feishu_credentials(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        probe: Callable[[FeishuConfig | None], Awaitable[ProbeVerdict]],
    ) -> ProbeVerdict:
        """包住飞书凭据同步探针；测试代次取自飞书域。"""
        return await self._probe(
            actor=actor,
            trace_id=trace_id,
            check=CheckName.FEISHU_CREDENTIALS,
            reader=self._repository.read_feishu,
            probe=probe,
        )

    # ------------------------------------------------------------------ OAuth 跨回调

    async def start_oauth_test(
        self,
        *,
        actor: AdminIdentityActor,
        trace_id: str,
        refuse: Callable[[FeishuConfig | None], Awaitable[ProbeVerdict | None]],
        issue: Callable[[str, int], Awaitable[_T]],
    ) -> _T | ProbeVerdict:
        """写 ``STARTED`` 后把**同一个** operation id 与被测代次交给 state 签发。

        ``issue(operation_id, config_generation)`` 必须把两者与 state 同事务落库；回调只能
        从 state 取回它们。前置拒绝（开关关闭、未配置、凭据未在当前代次通过、Web 尚未加载
        当前代次）同样有终态 ``FAILED``。
        """
        operation_id = _operation_id(trace_id)
        target = _probe_target(CheckName.FEISHU_OAUTH)
        await self._deny_unless_allowed(
            actor=actor,
            capability=AdminCapability.RUN_CONNECTION_TESTS,
            operation_id=operation_id,
            action=AdminAuditAction.CONNECTION_TESTED,
            target_ref_digest=target,
        )
        await self._start(
            actor=actor,
            operation_id=operation_id,
            action=AdminAuditAction.CONNECTION_TESTED,
            target_ref_digest=target,
        )
        try:
            config = await asyncio.to_thread(self._repository.read_feishu)
        except IntegrationConfigUnreadableError:
            await self._fail(
                operation_id=operation_id, reason=AdminAuditReasonCode.CONFIG_INVALID
            )
            raise IntegrationConfigUnavailableError from None
        refusal = await refuse(config)
        if refusal is not None:
            await self._finish_probe(
                operation_id=operation_id,
                check=CheckName.FEISHU_OAUTH,
                generation=None if config is None else config.generation,
                verdict=refusal,
            )
            return refusal
        if config is None:
            # ``refuse`` 必然拒绝未配置；到这里说明调用方的前置写漏了——失败关闭。
            await self._fail(
                operation_id=operation_id, reason=AdminAuditReasonCode.CONFIG_INVALID
            )
            raise IntegrationConfigUnavailableError
        try:
            return await issue(operation_id, config.generation)
        except Exception:
            await self._fail(
                operation_id=operation_id, reason=AdminAuditReasonCode.PROBE_FAILED
            )
            raise

    async def finish_oauth_test(
        self,
        *,
        operation_id: str,
        config_generation: int,
        actor: AdminIdentityActor | None,
        exchange: Callable[[], Awaitable[ProbeVerdict]],
    ) -> ProbeVerdict | None:
        """回调已消费测试 state 并从中取回 ``operation_id`` 与绑定代次之后调用。

        先确认它是一条尚未结束的 OAuth 连接测试 ``STARTED``；再核对当前会话仍是本地
        管理员——不是就**不交换 code**，按取回的 operation id 写 ``FAILED`` 并返回
        ``None``。然后核对当前飞书文件代次与 Web 已加载代次都仍等于绑定代次：任一漂移
        都说明这次测试的结论不属于任何一份现行配置，同样**不交换 code**，写
        ``FAILED``（``config_invalid``）并抛 :class:`OAuthTestGenerationDriftError`。
        三者同代才交换一次 code，结果只记到绑定代次。
        """
        if not await self._is_open_oauth_start(operation_id):
            raise IntegrationConfigUnavailableError
        if actor is None or _denial_reason(actor, AdminCapability.RUN_CONNECTION_TESTS):
            await self._terminal(
                operation_id=operation_id, reason=AdminAuditReasonCode.SESSION_INVALID
            )
            return None
        try:
            config = await asyncio.to_thread(self._repository.read_feishu)
        except IntegrationConfigUnreadableError:
            await self._fail(
                operation_id=operation_id, reason=AdminAuditReasonCode.CONFIG_INVALID
            )
            raise IntegrationConfigUnavailableError from None
        try:
            snapshot = await self._provider_state.snapshot()
        except Exception:
            await self._fail(
                operation_id=operation_id, reason=AdminAuditReasonCode.PROBE_FAILED
            )
            raise IntegrationConfigUnavailableError from None
        if (
            config is None
            or config.generation != config_generation
            or not web_holds_feishu_generation(snapshot.receipts, config_generation)
        ):
            await self._fail(
                operation_id=operation_id, reason=AdminAuditReasonCode.CONFIG_INVALID
            )
            raise OAuthTestGenerationDriftError
        verdict = await exchange()
        await self._finish_probe(
            operation_id=operation_id,
            check=CheckName.FEISHU_OAUTH,
            generation=config_generation,
            verdict=verdict,
        )
        return verdict

    async def _is_open_oauth_start(self, operation_id: str) -> bool:
        """它是不是一条尚未结束、属于飞书 OAuth 测试的 ``STARTED``。"""
        if not operation_id.startswith(OPERATION_ID_PREFIX):
            return False
        try:
            started = await self._audit.load(
                event_id=stage_event_id(operation_id=operation_id, stage="started")
            )
            terminal = await self._audit.load(
                event_id=stage_event_id(operation_id=operation_id, stage="terminal")
            )
        except Exception:
            raise IntegrationConfigUnavailableError from None
        return (
            started is not None
            and terminal is None
            and started.action is AdminAuditAction.CONNECTION_TESTED
            and started.target_ref_digest == _probe_target(CheckName.FEISHU_OAUTH)
        )


__all__ = [
    "OPERATION_ID_PREFIX",
    "ConfigSaved",
    "FeishuUpdate",
    "GeminiUpdate",
    "IntegrationConfigForbiddenError",
    "IntegrationConfigRepository",
    "IntegrationConfigService",
    "IntegrationConfigUnavailableError",
    "IntegrationConfigUnreadableError",
    "IntegrationConfigWriteError",
    "OAuthTestGenerationDriftError",
    "ProbeVerdict",
    "PrometheusDraft",
    "ResourceConflictError",
    "ResourceNotFoundError",
    "ResourceRejectedError",
    "ResourceSaved",
    "ResourceUpdate",
    "StarRocksDraft",
    "new_resource_id",
]
