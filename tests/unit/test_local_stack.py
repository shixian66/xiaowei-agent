"""LocalStack 装配根不依赖 tests/ 语料。"""

import asyncio
import datetime as dt
import json
from dataclasses import fields
from pathlib import Path
from types import MappingProxyType

import pytest
from tests.fakes.clock import ManualClock
from tests.fakes.feishu import RecordingFeishuInboundTransport

from xiaowei_agent.application.activation_notification import (
    ActivationNotificationService,
)
from xiaowei_agent.application.admin_identity import AdminIdentityService
from xiaowei_agent.application.capability_runtime import CapabilityBindingRegistry
from xiaowei_agent.application.channel_access import TaskAccessService
from xiaowei_agent.application.channel_projection import ChannelProjectionService
from xiaowei_agent.application.channel_submission import ChannelSubmissionService
from xiaowei_agent.application.identity_activation import IdentityActivationService
from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    Channel,
    ModelInvocationProfile,
    ReadinessReport,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskStatus,
    TaskSubmission,
)
from xiaowei_agent.interfaces import web_auth as web_auth_module
from xiaowei_agent.interfaces.directory_identity import (
    DirectoryFeishuIdentityDirectory,
)
from xiaowei_agent.interfaces.local_admin_auth import LocalAdminAuthService
from xiaowei_agent.interfaces.local_stack import (
    SMOKE_BARRIER_MARKER,
    ChannelWorkerStack,
    FeishuListenerStack,
    LocalStack,
    StarRocksLiveAssembly,
    TaskViewStack,
    WebStack,
    WebStackConfigurationError,
    build_in_memory_local_stack,
    build_postgres_channel_worker_stack,
    build_postgres_feishu_listener_stack,
    build_postgres_local_stack,
    build_postgres_task_view_stack,
    build_postgres_web_stack,
)
from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials
from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity, WebAuthService
from xiaowei_agent.persistence.database import DatabaseConfigurationError
from xiaowei_agent.persistence.postgres import (
    PostgresChannelStore,
    PostgresTaskStore,
    PostgresWebSessionStore,
)
from xiaowei_agent.rendering.feishu import RenderedFeishuCard
from xiaowei_agent.tools.gateway import DeterministicToolGateway
from xiaowei_agent.tools.starrocks import PhysicalIdentityProbe


@pytest.mark.asyncio
async def test_in_memory_local_stack_is_complete_and_ready() -> None:
    stack = build_in_memory_local_stack(settings=Settings(environment_id="dev"))
    assert isinstance(stack, LocalStack)
    assert isinstance(stack.runtime._runner._gateway, DeterministicToolGateway)
    assert set(stack.runtime._runner._gateway._adapters) == {
        "starrocks",
        "alertmanager",
        "prometheus",
        "asset_inventory",
    }
    assert isinstance(stack.runtime._bindings, CapabilityBindingRegistry)
    assert not hasattr(stack.runtime, "_context_assembler")
    assert stack.interaction_classifier is None
    assert stack.slow_query_advisory is None
    assert stack.model_profile is None
    assert stack.runtime._runner._bindings is stack.runtime._bindings
    assert await stack.readiness.check() == ReadinessReport(
        database_ok=True,
        revision_matches_head=True,
        assembled=True,
    )
    await stack.aclose()


def test_enabled_gemini_is_lazily_assembled_without_reading_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden_read(path: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("composition must not read the key")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.secret_file.read_secret_file",
        forbidden_read,
    )
    # RI5：Key 由调用方注入，内存栈自己既不读文件也不读 `integrations.json`。
    stack = build_in_memory_local_stack(
        settings=Settings(environment_id="dev", gemini_enabled=True),
        credentials=ProviderCredentials(gemini_api_key="AIza" + "x" * 35),
    )
    assert stack.interaction_classifier is not None
    assert stack.interaction_classifier is stack.slow_query_advisory
    assert isinstance(stack.model_profile, ModelInvocationProfile)
    assert stack.interaction_classifier.profile is stack.model_profile
    assert calls == 0


def _live_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "environment_id": "test",
        "actor": "m6b-operator",
        "starrocks_adapter_mode": "test_readonly",
        "starrocks_host": "starrocks.test.invalid",
        "starrocks_port": 9030,
        "starrocks_database": "audit_db",
        "starrocks_user": "audit_reader",
        "starrocks_password_file": "/approved/database-credential",
        "starrocks_tls_mode": "verify_identity",
        "starrocks_ca_file": "/approved/ca.pem",
        "starrocks_server_name": "starrocks.test.invalid",
        "starrocks_resource_id": "approved-test-cluster",
        "starrocks_expected_version_sha256": "d" * 64,
        "starrocks_expected_grants_sha256": "a" * 64,
        "starrocks_expected_ddl_sha256": "b" * 64,
        "starrocks_expected_identity_sha256": "c" * 64,
        "starrocks_expected_metadata_source_ref": "approval-ref:m6b-1",
        "starrocks_physical_identity_ref": "identity-ref:test-cluster",
        "starrocks_authorized_actor": "m6b-operator",
        "starrocks_active_from": dt.datetime(2026, 9, 7, 9, tzinfo=dt.UTC),
        "starrocks_active_until": dt.datetime(2026, 9, 7, 11, tzinfo=dt.UTC),
        "starrocks_connect_timeout_seconds": 5,
        "starrocks_read_timeout_seconds": 25,
        "starrocks_write_timeout_seconds": 5,
        "starrocks_query_timeout_seconds": 20,
    }
    return Settings(**(values | updates))


class _NeverConnectFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, password: str) -> object:
        del password
        self.calls += 1
        raise AssertionError("composition must not connect")


def _live_assembly(factory: _NeverConnectFactory) -> StarRocksLiveAssembly:
    return StarRocksLiveAssembly(
        identity_probe=PhysicalIdentityProbe(
            statement=(
                "SELECT `cluster_identity` FROM "
                "`xiaowei_meta`.`cluster_identity_v` LIMIT 1"
            ),
            result_column="cluster_identity",
        ),
        connection_factory=factory,
        driver_version="1.2.0-test-double",
        unredacted_rows_approved=True,
    )


def _install_approved_test_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "xiaowei_agent.capabilities.target._ENVIRONMENT_DIRECTORY",
        MappingProxyType(
            {
                "dev": ("starrocks-dev-1",),
                "test": ("approved-test-cluster",),
            }
        ),
    )


def test_test_readonly_stack_requires_code_owned_live_assembly() -> None:
    with pytest.raises(ValueError):
        build_in_memory_local_stack(settings=_live_settings())


def test_test_readonly_stack_rejects_current_ambiguous_target_before_connect() -> None:
    factory = _NeverConnectFactory()
    with pytest.raises(ValueError):
        build_in_memory_local_stack(
            settings=_live_settings(),
            starrocks_live_assembly=_live_assembly(factory),
        )
    assert factory.calls == 0


def test_test_readonly_stack_registers_only_exact_target_without_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_approved_test_directory(monkeypatch)
    factory = _NeverConnectFactory()
    now = dt.datetime(2026, 9, 7, 10, tzinfo=dt.UTC)

    stack = build_in_memory_local_stack(
        settings=_live_settings(),
        starrocks_live_assembly=_live_assembly(factory),
        clock=lambda: now,
    )

    gateway = stack.runtime._runner._gateway
    assert isinstance(gateway, DeterministicToolGateway)
    assert "starrocks" not in gateway._adapters
    assert set(gateway._adapters) == {"alertmanager", "prometheus", "asset_inventory"}
    assert len(gateway._target_adapters) == 1
    ((gateway_name, fingerprint), binding) = next(iter(gateway._target_adapters.items()))
    assert gateway_name == "starrocks"
    assert len(fingerprint) == 64
    assert binding.config_revision
    assert binding.physical_identity_ref == "identity-ref:test-cluster"
    assert factory.calls == 0


def test_test_readonly_stack_rejects_resource_config_directory_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_approved_test_directory(monkeypatch)
    factory = _NeverConnectFactory()
    with pytest.raises(ValueError):
        build_in_memory_local_stack(
            settings=_live_settings(starrocks_resource_id="other-cluster"),
            starrocks_live_assembly=_live_assembly(factory),
        )
    assert factory.calls == 0


@pytest.mark.asyncio
async def test_packaged_prometheus_recordings_run_the_two_gateway_task() -> None:
    import datetime as dt

    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = Settings(environment_id="dev")
    stack = build_in_memory_local_stack(settings=settings, clock=clock)
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor=settings.actor,
        environment_id=settings.environment_id,
        trace_id="2" * 32,
        policy_revision=stack.policy_revision,
    )
    pending = await stack.runtime.submit_task(
        submission=TaskSubmission(
            envelope=RequestEnvelope(
                request_id="request-prometheus",
                tenant_id=context.tenant_id,
                actor=context.actor,
                channel=Channel.API,
                text="查告警 HostHighCpu 在 node-1.example.com:9100 的证据",
                idempotency_key="idem-local-prometheus",
                environment_id=context.environment_id,
            ),
            context=context,
            as_of=now,
        )
    )
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 1
    completed = await stack.runtime.query_task(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    evidences = await stack.evidence_ledger.load(task_id=pending.task_id)
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.render is not None
    assert [item.evidence_id.rsplit(":", 1)[-1] for item in evidences] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_packaged_prometheus_recording_has_no_nondefault_window_fallback(
) -> None:
    """本地 catalog 只承诺默认 30 分钟窗；60 分钟必须安全降级。"""
    import datetime as dt

    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = Settings(environment_id="dev")
    stack = build_in_memory_local_stack(settings=settings, clock=clock)
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor=settings.actor,
        environment_id=settings.environment_id,
        trace_id="3" * 32,
        policy_revision=stack.policy_revision,
    )
    pending = await stack.runtime.submit_task(
        submission=TaskSubmission(
            envelope=RequestEnvelope(
                request_id="request-prometheus-nondefault-window",
                tenant_id=context.tenant_id,
                actor=context.actor,
                channel=Channel.API,
                text=(
                    "查告警 HostHighCpu 在 node-1.example.com:9100 "
                    "最近60分钟的证据"
                ),
                idempotency_key="idem-local-prometheus-nondefault-window",
                environment_id=context.environment_id,
            ),
            context=context,
            as_of=now,
        )
    )
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 1
    completed = await stack.runtime.query_task(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    gateway = stack.runtime._runner._gateway
    prometheus = gateway._adapters["prometheus"]
    evidences = await stack.evidence_ledger.load(task_id=pending.task_id)
    assert completed.status is TaskStatus.INDETERMINATE
    assert prometheus.call_count == 1
    assert [item.evidence_id.rsplit(":", 1)[-1] for item in evidences] == ["s1", "s2"]
    assert evidences[0].facts
    assert evidences[1].facts == ()


@pytest.mark.asyncio
async def test_packaged_asset_recording_runs_one_exact_lookup() -> None:
    import datetime as dt

    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = Settings(environment_id="dev")
    stack = build_in_memory_local_stack(settings=settings, clock=clock)
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor=settings.actor,
        environment_id=settings.environment_id,
        trace_id="4" * 32,
        policy_revision=stack.policy_revision,
    )
    pending = await stack.runtime.submit_task(
        submission=TaskSubmission(
            envelope=RequestEnvelope(
                request_id="request-asset",
                tenant_id=context.tenant_id,
                actor=context.actor,
                channel=Channel.API,
                text="查资产 hostname=NODE-1.EXAMPLE.COM.",
                idempotency_key="idem-local-asset",
                environment_id=context.environment_id,
            ),
            context=context,
            as_of=now,
        )
    )
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 1
    completed = await stack.runtime.query_task(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    evidences = await stack.evidence_ledger.load(task_id=pending.task_id)
    adapter = stack.runtime._runner._gateway._adapters["asset_inventory"]
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.render is not None
    assert "node-1.example.com" in completed.render.model_dump_json()
    assert len(evidences) == 1
    assert adapter.call_count == 1


@pytest.mark.asyncio
async def test_worker_interface_closes_the_stack_when_cancelled() -> None:
    closed = asyncio.Event()
    stack = build_in_memory_local_stack(
        settings=Settings(environment_id="dev"),
        on_close=closed.set,
    )
    from xiaowei_agent.interfaces.worker import serve_worker

    stop = asyncio.Event()
    stop.set()
    assert await serve_worker(stack=stack, stop=stop) == 0
    assert closed.is_set()


def test_default_stack_does_not_install_a_smoke_barrier() -> None:
    stack = build_in_memory_local_stack(settings=Settings(environment_id="dev"))
    assert type(stack.runtime._runner._gateway) is DeterministicToolGateway


@pytest.mark.asyncio
async def test_packaged_recording_runs_one_complete_task_without_tests_data() -> None:
    import datetime as dt

    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = Settings(environment_id="dev")
    stack = build_in_memory_local_stack(settings=settings, clock=clock)
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor="local-developer",
        environment_id=settings.environment_id,
        trace_id="0" * 32,
        policy_revision=stack.policy_revision,
    )
    submission = TaskSubmission(
        envelope=RequestEnvelope(
            request_id="request-1",
            tenant_id=context.tenant_id,
            actor=context.actor,
            channel=Channel.API,
            text="检查最近三十分钟慢查询",
            idempotency_key="idem-local-stack-1",
            environment_id=context.environment_id,
        ),
        context=context,
        as_of=now,
    )
    pending = await stack.runtime.submit_task(submission=submission)
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 1
    completed = await stack.runtime.query_task(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    assert completed.status is TaskStatus.SUCCEEDED
    assert completed.render is not None
    assert await stack.evidence_ledger.load(task_id=pending.task_id)


@pytest.mark.asyncio
async def test_postgres_stack_uses_one_engine_and_disposes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )
    stack = await build_postgres_local_stack(settings=Settings(environment_id="dev"))
    assert isinstance(stack.task_store, PostgresTaskStore)
    assert not hasattr(stack.runtime, "_context_assembler")
    assert stack.task_store._engine is engine
    assert stack.plan_store._engine is engine
    assert stack.evidence_ledger._engine is engine
    await stack.aclose()
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_postgres_task_view_stack_has_only_projection_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    stack = await build_postgres_task_view_stack(
        settings=Settings(environment_id="dev")
    )

    assert isinstance(stack, TaskViewStack)
    assert isinstance(stack.runtime, TaskViewRuntime)
    assert {field.name for field in fields(TaskViewStack)} == {
        "runtime",
        "task_store",
        "plan_store",
        "evidence_ledger",
        "clock",
        "settings",
        "readiness",
        "aclose",
        "policy_revision",
    }
    assert set(vars(stack.runtime)) == {
        "_tasks",
        "_plans",
            "_ledger",
            "_bindings",
            "_snapshot",
            "_clarification_records",
            "_model_artifacts",
            "_model_profile",
    }
    assert stack.task_store._engine is engine
    assert stack.plan_store._engine is engine
    assert stack.evidence_ledger._engine is engine
    await stack.aclose()
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_postgres_task_view_stack_disposes_engine_when_assembly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    def fail_bindings() -> object:
        raise RuntimeError("constant task-view assembly failure")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack._build_capability_bindings",
        fail_bindings,
    )

    with pytest.raises(RuntimeError, match="constant task-view assembly failure"):
        await build_postgres_task_view_stack(settings=Settings(environment_id="dev"))
    assert engine.disposed is True


def _feishu_settings(identity_file: Path, secret_file: Path) -> Settings:
    return Settings(
        environment_id="dev",
        feishu_listener_enabled=True,
        feishu_tenant_key="tenant-test",
        feishu_bot_open_id="bot-open-id",
        feishu_identity_file=str(identity_file),
    )


def _channel_worker_settings(secret_file: Path) -> Settings:
    return Settings(
        environment_id="dev",
        channel_worker_enabled=True,
        web_public_origin="https://ops.example.test",
    )


class _RecordingMessages:
    async def send_to_chat(
        self,
        *,
        conversation_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        del conversation_ref, card, idempotency_ref
        return "message-1"

    async def send_to_user(
        self,
        *,
        subject_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        del subject_ref, card, idempotency_ref
        return "message-1"

    async def update_card(
        self, *, message_ref: str, card: RenderedFeishuCard
    ) -> None:
        del message_ref, card


class _OfflineOAuth:
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        del redirect_uri
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        del code, redirect_uri
        return FeishuOAuthIdentity(subject_ref="user-open-id")


class _OfflineMembership:
    async def is_current_group_member(
        self, *, tenant_id: str, conversation_ref: str, subject_ref: str
    ) -> bool:
        del tenant_id, conversation_ref, subject_ref
        return True


def _write_identity(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": "dev-local",
                "environment_id": "dev",
                "entries": [
                    {
                        "subject_ref": "user-open-id",
                        "actor": "alice",
                        "labels": ["operator"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _web_settings(identity_file: Path) -> Settings:
    return Settings(
        environment_id="dev",
        web_app_enabled=True,
        feishu_oauth_enabled=True,
        feishu_identity_file=str(identity_file),
        web_public_origin="https://ops.example.test",
    )


class _FakeWebResult:
    """``execute()`` 的结果替身：读什么都读不到行。"""

    def mappings(self) -> "_FakeWebResult":
        return self

    def first(self) -> object:
        return None

    def all(self) -> list[object]:
        return []


class _FakeWebConnection:
    """连接替身。

    Web 装配会在这里 seed 本地管理员，而 W1a 之后那是一次**完整的 bootstrap**：
    凭据行、目录账号、ADMIN 角色、凭据链接与一条审计事件在同一个事务里落库。
    因此替身要分别回答两类调用——``execute()`` 的读返回空（目录里还没有这个
    账号），``scalar()`` 返回一个非空值（``ON CONFLICT ... RETURNING`` 拿到了行，
    即"确实插进去了"）。两者返回同一个值时，装配会被判成一次主键冲突。
    """

    async def execute(self, *_: object, **__: object) -> object:
        return _FakeWebResult()

    async def scalar(self, *_: object, **__: object) -> object:
        return "inserted"


class _FakeWebEngine:
    disposed = False

    async def dispose(self) -> None:
        self.disposed = True

    def begin(self) -> object:
        connection = _FakeWebConnection()

        class _Transaction:
            async def __aenter__(self) -> _FakeWebConnection:
                return connection

            async def __aexit__(self, *_: object) -> bool:
                return False

        return _Transaction()

    connect = begin


@pytest.mark.asyncio
async def test_postgres_web_stack_has_only_auth_and_task_view_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _FakeWebEngine()
    identity_file = tmp_path / "identities.json"
    _write_identity(identity_file)
    oauth = _OfflineOAuth()
    membership = _OfflineMembership()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    stack = await build_postgres_web_stack(
        settings=_web_settings(identity_file),
        oauth=oauth,
        membership=membership,
    )

    assert isinstance(stack, WebStack)
    assert isinstance(stack.auth, WebAuthService)
    assert stack.oauth_available is True
    assert isinstance(stack.local_admin_auth, LocalAdminAuthService)
    assert isinstance(stack.runtime, TaskViewRuntime)
    assert isinstance(stack.task_access_service, TaskAccessService)
    assert isinstance(stack.submission_service, ChannelSubmissionService)
    assert isinstance(stack.channel_store, PostgresChannelStore)
    assert isinstance(stack.web_session_store, PostgresWebSessionStore)
    assert {field.name for field in fields(WebStack)} == {
        "local_admin_auth",
        "oauth_available",
        "auth",
        "oauth_port",
        "membership",
        "runtime",
        "task_store",
        "channel_store",
        "web_session_store",
        "provider_state",
        "identity_directory",
        "activation_service",
        "admin_identity_service",
        # W4a：唯一配置写服务；只写本地配置文件与管理审计，不持有执行面。
        "integration_config_service",
        "task_access_service",
        "submission_service",
        "clock",
        "settings",
        "readiness",
        "aclose",
        "policy_revision",
    }
    assert set(vars(stack.runtime)) == {
        "_tasks",
        "_plans",
            "_ledger",
            "_bindings",
            "_snapshot",
            "_clarification_records",
            "_model_artifacts",
            "_model_profile",
    }
    assert stack.oauth_port is oauth
    assert stack.membership is membership
    assert isinstance(stack.identity_directory, DirectoryFeishuIdentityDirectory)
    assert isinstance(stack.activation_service, IdentityActivationService)
    assert isinstance(stack.admin_identity_service, AdminIdentityService)
    assert stack.admin_identity_service._directory is stack.identity_directory._directory
    assert (
        stack.admin_identity_service._directory
        is stack.activation_service._directory
    )
    assert (
        stack.admin_identity_service._activations
        is stack.activation_service._activations
    )
    assert stack.admin_identity_service._audit is stack.activation_service._audit
    assert (
        stack.admin_identity_service._activation_decisions
        is stack.activation_service
    )
    assert stack.task_store._engine is engine
    assert stack.channel_store._engine is engine
    assert stack.submission_service._web_parent_access is stack.task_access_service
    assert stack.web_session_store._engine is engine
    assert stack.task_access_service._runtime is stack.runtime
    assert stack.submission_service._runtime is stack.runtime
    assert stack.auth._oauth_timeout_seconds == (
        web_auth_module.FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS
    )
    await stack.aclose()
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_web_stack_assembles_admin_identity_without_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _FakeWebEngine()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    stack = await build_postgres_web_stack(
        settings=_web_settings(tmp_path / "unused-identities.json"),
        oauth=None,
        membership=None,
    )
    try:
        assert stack.auth is None
        assert stack.identity_directory is None
        assert stack.oauth_available is False
        assert isinstance(stack.activation_service, IdentityActivationService)
        assert isinstance(stack.admin_identity_service, AdminIdentityService)
        assert (
            stack.admin_identity_service._activation_decisions
            is stack.activation_service
        )
        assert (
            stack.admin_identity_service._directory
            is stack.activation_service._directory
        )
        assert (
            stack.admin_identity_service._activations
            is stack.activation_service._activations
        )
        assert stack.admin_identity_service._audit is stack.activation_service._audit
    finally:
        await stack.aclose()


@pytest.mark.asyncio
async def test_web_stack_rejects_local_admin_scope_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_engine(_: object) -> object:
        raise AssertionError("scope mismatch must stop before database assembly")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        fail_engine,
    )
    settings = Settings(
        **(
            _web_settings(tmp_path / "unused-identities.json").model_dump()
            | {"environment_id": "staging"}
        )
    )

    with pytest.raises(WebStackConfigurationError):
        await build_postgres_web_stack(
            settings=settings,
            oauth=None,
            membership=None,
        )


@pytest.mark.asyncio
async def test_web_stack_uses_fixed_oauth_deadline_not_channel_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity_file = tmp_path / "identities.json"
    _write_identity(identity_file)
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: _FakeWebEngine(),
    )
    settings = Settings(
        **(
            _web_settings(identity_file).model_dump()
            | {"feishu_api_timeout_seconds": 10.0}
        )
    )

    stack = await build_postgres_web_stack(
        settings=settings,
        oauth=_OfflineOAuth(),
        membership=_OfflineMembership(),
    )
    try:
        assert stack.auth._oauth_timeout_seconds == (
            web_auth_module.FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS
        )
    finally:
        await stack.aclose()


@pytest.mark.asyncio
async def test_disabled_web_stack_does_not_create_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_engine(_: object) -> object:
        raise AssertionError("disabled Web app must stop before database assembly")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine", fail_engine
    )

    with pytest.raises(ValueError, match="Web app is disabled"):
        await build_postgres_web_stack(
            settings=Settings(environment_id="dev"),
            oauth=_OfflineOAuth(),
            membership=_OfflineMembership(),
        )


@pytest.mark.asyncio
async def test_web_stack_maps_database_credential_failure_to_its_narrow_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_engine(_: object) -> object:
        raise DatabaseConfigurationError("database-sensitive-reference")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        fail_engine,
    )

    with pytest.raises(WebStackConfigurationError) as caught:
        await build_postgres_web_stack(
            settings=_web_settings(tmp_path / "unused-identities.json"),
            oauth=_OfflineOAuth(),
            membership=_OfflineMembership(),
        )

    assert str(caught.value) == "web stack configuration invalid"
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_static_identity_file_is_not_a_web_runtime_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """静态身份文件只作迁移输入；OAuth 运行时只读数据库目录。"""
    engine = _FakeWebEngine()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    stack = await build_postgres_web_stack(
        settings=_web_settings(tmp_path / "missing-identities.json"),
        oauth=_OfflineOAuth(),
        membership=_OfflineMembership(),
    )
    try:
        assert stack.oauth_available is True
        assert isinstance(stack.auth, WebAuthService)
        assert isinstance(stack.identity_directory, DirectoryFeishuIdentityDirectory)
        assert isinstance(stack.activation_service, IdentityActivationService)
        assert stack.oauth_port is not None
        assert stack.membership is not None
        assert isinstance(stack.local_admin_auth, LocalAdminAuthService)
        assert engine.disposed is False
    finally:
        await stack.aclose()


@pytest.mark.asyncio
async def test_local_admin_seed_failure_disposes_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地管理员是必填：seed 失败必须让 composition 不成立。

    与上一条互为对照——飞书失败是"降级"，本地管理员失败是"起不来"。
    """
    engine = _FakeWebEngine()
    identity_file = tmp_path / "identities.json"
    _write_identity(identity_file)
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    async def fail_seed(self: object, *, password_hash: str) -> bool:
        raise RuntimeError("constant seed failure")

    monkeypatch.setattr(
        "xiaowei_agent.persistence.local_admin.PostgresLocalAdminStore.seed_if_absent",
        fail_seed,
    )

    with pytest.raises(RuntimeError, match="constant seed failure"):
        await build_postgres_web_stack(
            settings=_web_settings(identity_file),
            oauth=_OfflineOAuth(),
            membership=_OfflineMembership(),
        )
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_postgres_feishu_listener_stack_has_only_ingress_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    identity_file = tmp_path / "identities.json"
    _write_identity(identity_file)
    fake_transport = RecordingFeishuInboundTransport()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    stack = await build_postgres_feishu_listener_stack(
        settings=_feishu_settings(identity_file, tmp_path / "missing-secret"),
        transport=fake_transport,
        credentials=ProviderCredentials(
            feishu_app_id="cli_listener",
            feishu_app_secret="listener-" + "fixture-secret",
        ),
    )

    assert isinstance(stack, FeishuListenerStack)
    assert isinstance(stack.runtime, TaskViewRuntime)
    assert isinstance(stack.channel_store, PostgresChannelStore)
    assert {field.name for field in fields(FeishuListenerStack)} == {
        "listener",
        "transport",
        "provider_state",
        "load_receipts",
        "runtime",
        "task_store",
        "channel_store",
        "identity_directory",
        "activation_service",
        "activation_notifications",
        "message_port",
        "submission_service",
        "clock",
        "settings",
        "readiness",
        "aclose",
        "policy_revision",
    }
    assert set(vars(stack.runtime)) == {
        "_tasks",
        "_plans",
            "_ledger",
            "_bindings",
            "_snapshot",
            "_clarification_records",
            "_model_artifacts",
            "_model_profile",
    }
    assert stack.transport is fake_transport
    assert isinstance(stack.identity_directory, DirectoryFeishuIdentityDirectory)
    assert isinstance(stack.activation_service, IdentityActivationService)
    assert isinstance(stack.activation_notifications, ActivationNotificationService)
    assert stack.task_store._engine is engine
    assert stack.channel_store._engine is engine
    assert stack.submission_service._web_parent_access is None
    await stack.aclose()
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_feishu_listener_stack_requires_credentials_even_with_fake_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件 scope 校验仍要用 app_id；fake transport 不能让 listener 带 None 启动。"""
    identity_file = tmp_path / "identities.json"
    _write_identity(identity_file)

    def fail_engine(_: object) -> object:
        raise AssertionError("missing credentials must stop before database assembly")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine", fail_engine
    )

    with pytest.raises(ValueError, match="feishu credentials are not configured"):
        await build_postgres_feishu_listener_stack(
            settings=_feishu_settings(identity_file, tmp_path / "missing-secret"),
            transport=RecordingFeishuInboundTransport(),
            credentials=ProviderCredentials(),
        )


@pytest.mark.asyncio
async def test_disabled_feishu_stack_does_not_create_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_engine(_: object) -> object:
        raise AssertionError("disabled listener must stop before database assembly")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine", fail_engine
    )

    with pytest.raises(ValueError, match="Feishu listener is disabled"):
        await build_postgres_feishu_listener_stack(
            settings=Settings(environment_id="dev")
        )


@pytest.mark.asyncio
async def test_static_identity_file_is_not_a_listener_runtime_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    stack = await build_postgres_feishu_listener_stack(
        settings=_feishu_settings(
            tmp_path / "missing-identities.json", tmp_path / "missing-secret"
        ),
        transport=RecordingFeishuInboundTransport(),
        credentials=ProviderCredentials(
            feishu_app_id="cli_listener",
            feishu_app_secret="listener-" + "fixture-secret",
        ),
    )
    try:
        assert isinstance(stack.identity_directory, DirectoryFeishuIdentityDirectory)
    finally:
        await stack.aclose()
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_postgres_channel_worker_stack_has_only_projection_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    messages = _RecordingMessages()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    stack = await build_postgres_channel_worker_stack(
        settings=_channel_worker_settings(tmp_path / "missing-secret"),
        message_port=messages,
    )

    assert isinstance(stack, ChannelWorkerStack)
    assert isinstance(stack.service, ChannelProjectionService)
    assert isinstance(stack.runtime, TaskViewRuntime)
    assert isinstance(stack.task_store, PostgresTaskStore)
    assert isinstance(stack.channel_store, PostgresChannelStore)
    assert stack.message_port is messages
    assert {field.name for field in fields(ChannelWorkerStack)} == {
        "service",
        "message_port",
        "provider_state",
        "load_receipts",
        "runtime",
        "task_store",
        "channel_store",
        "clock",
        "settings",
        "readiness",
        "aclose",
        "policy_revision",
    }
    assert set(vars(stack.runtime)) == {
        "_tasks",
        "_plans",
            "_ledger",
            "_bindings",
            "_snapshot",
            "_clarification_records",
            "_model_artifacts",
            "_model_profile",
    }
    assert stack.task_store._engine is engine
    assert stack.channel_store._engine is engine
    await stack.aclose()
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_disabled_channel_worker_does_not_create_an_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_engine(_: object) -> object:
        raise AssertionError("disabled worker must stop before database assembly")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine", fail_engine
    )

    with pytest.raises(ValueError, match="channel worker is disabled"):
        await build_postgres_channel_worker_stack(
            settings=Settings(environment_id="dev")
        )


@pytest.mark.asyncio
async def test_channel_worker_stack_disposes_engine_when_port_assembly_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    def fail_port(**_: object) -> object:
        raise RuntimeError("constant message port assembly failure")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.feishu_sdk.FeishuSdkMessageAdapter",
        fail_port,
    )

    with pytest.raises(RuntimeError, match="constant message port assembly failure"):
        await build_postgres_channel_worker_stack(
            settings=_channel_worker_settings(tmp_path / "missing-secret"),
            # 凭据必须显式注入：没有它们，装配在**构造真实 adapter 之前**就被
            # ``_feishu_credentials_or_fail`` 拒绝，这条用例要验的 dispose 分支
            # 反而走不到。
            credentials=ProviderCredentials(
                feishu_app_id="cli_dispose",
                feishu_app_secret="dispose-" + "fixture-secret",
            ),
        )
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_postgres_stack_disposes_engine_when_assembly_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        disposed = False

        async def dispose(self) -> None:
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.local_stack.create_database_engine",
        lambda _: engine,
    )

    def fail_adapter(*_: object, **__: object) -> object:
        raise RuntimeError("constant assembly failure")

    monkeypatch.setattr(
        "xiaowei_agent.tools.starrocks_fake.StarRocksRecordingAdapter",
        fail_adapter,
    )
    with pytest.raises(RuntimeError, match="constant assembly failure"):
        await build_postgres_local_stack(settings=Settings(environment_id="dev"))
    assert engine.disposed is True


@pytest.mark.asyncio
async def test_smoke_barrier_stops_after_tool_result_before_commit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import datetime as dt

    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = Settings(environment_id="dev", smoke_step_barrier=True)
    stack = build_in_memory_local_stack(settings=settings, clock=clock)
    submission = TaskSubmission(
        envelope=RequestEnvelope(
            request_id="request-barrier",
            tenant_id=settings.tenant_id,
            actor=settings.actor,
            channel=Channel.API,
            text="检查最近三十分钟慢查询",
            idempotency_key="idem-barrier",
            environment_id=settings.environment_id,
        ),
        context=RequestContext(
            tenant_id=settings.tenant_id,
            actor=settings.actor,
            environment_id=settings.environment_id,
            trace_id="1" * 32,
            policy_revision=stack.policy_revision,
        ),
        as_of=now,
    )
    pending = await stack.runtime.submit_task(submission=submission)
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=clock,
        monotonic=stack.monotonic,
        settings=settings,
        sleep=asyncio.sleep,
    )
    running = asyncio.create_task(worker.poll_once())
    gateway = stack.runtime._runner._gateway
    await asyncio.wait_for(gateway.entered.wait(), timeout=1)
    assert SMOKE_BARRIER_MARKER in capsys.readouterr().out
    records = await stack.task_store.load_step_executions(task_id=pending.task_id)
    assert len(records) == 1
    assert records[0].result_status is None
    assert await stack.evidence_ledger.load(task_id=pending.task_id) == ()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
