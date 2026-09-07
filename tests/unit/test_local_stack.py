"""LocalStack 装配根不依赖 tests/ 语料。"""

import asyncio
import datetime as dt
from types import MappingProxyType

import pytest
from tests.fakes.clock import ManualClock

from xiaowei_agent.application.capability_runtime import CapabilityBindingRegistry
from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    Channel,
    ReadinessReport,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskStatus,
    TaskSubmission,
)
from xiaowei_agent.interfaces.local_stack import (
    SMOKE_BARRIER_MARKER,
    LocalStack,
    StarRocksLiveAssembly,
    build_in_memory_local_stack,
    build_postgres_local_stack,
)
from xiaowei_agent.persistence.postgres import PostgresTaskStore
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
    assert stack.runtime._runner._bindings is stack.runtime._bindings
    assert await stack.readiness.check() == ReadinessReport(
        database_ok=True,
        revision_matches_head=True,
        assembled=True,
    )
    await stack.aclose()


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
    assert stack.task_store._engine is engine
    assert stack.plan_store._engine is engine
    assert stack.evidence_ledger._engine is engine
    await stack.aclose()
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
        "xiaowei_agent.interfaces.local_stack.StarRocksRecordingAdapter",
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
