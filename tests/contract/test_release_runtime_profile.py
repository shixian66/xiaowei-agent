"""W5 release 运行形态：provider-off 空准入快照与历史渲染 binding 分离。

release 不是"换个默认值"：它必须同时做到三件事——

1. Settings 只接受 ``release + dev-local/dev + starrocks disabled``，其余组合 fail-closed；
2. 普通对话只投影**当前**准入快照，release 下如实回答"当前无可执行能力"；
3. 已持久化的计划仍按精确 capability/version 从代码内完整渲染注册表找到投影器，
   而运维能力请求在 Resolver 阶段被确定性拒绝，工具调用次数为 0。
"""

import asyncio
import datetime as dt
from typing import Any

import pytest
from tests.fakes.clock import ManualClock
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.application.task_view_runtime import TaskViewRuntime
from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.contracts import (
    Channel,
    InteractionDraft,
    InteractionKind,
    InteractionModelResult,
    InteractionSource,
    ModelInvocationProfile,
    ModelUsage,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskStatus,
    TaskSubmission,
)
from xiaowei_agent.interfaces.local_stack import build_in_memory_local_stack
from xiaowei_agent.persistence.plans import PlanNotFoundError
from xiaowei_agent.rendering.generic import CONVERSATION_TERMINAL_REASON

_RELEASE_ENV = {
    "XIAOWEI_ENVIRONMENT_ID": "dev",
    "XIAOWEI_RUNTIME_PROFILE": "release",
    "XIAOWEI_STARROCKS_ADAPTER_MODE": "disabled",
}


def _release_settings(**updates: Any) -> Settings:
    return Settings.model_validate(
        {
            "environment_id": "dev",
            "runtime_profile": "release",
            "starrocks_adapter_mode": "disabled",
            **updates,
        }
    )


# --- Settings 闭集 -----------------------------------------------------------


def test_default_profile_stays_offline_recording() -> None:
    from xiaowei_agent.config import RuntimeProfile, StarRocksAdapterMode

    settings = load_settings({"XIAOWEI_ENVIRONMENT_ID": "dev"})

    assert settings.runtime_profile is RuntimeProfile.OFFLINE_RECORDING
    assert settings.starrocks_adapter_mode is StarRocksAdapterMode.RECORDING


def test_release_profile_loads_only_with_fixed_scope_and_disabled_starrocks() -> None:
    from xiaowei_agent.config import RuntimeProfile, StarRocksAdapterMode

    settings = load_settings(_RELEASE_ENV)

    assert settings.runtime_profile is RuntimeProfile.RELEASE
    assert settings.starrocks_adapter_mode is StarRocksAdapterMode.DISABLED
    assert (settings.tenant_id, settings.environment_id) == ("dev-local", "dev")


@pytest.mark.parametrize(
    "overrides",
    [
        # release 不能带合成执行。
        {"XIAOWEI_STARROCKS_ADAPTER_MODE": "recording"},
        {"XIAOWEI_STARROCKS_ADAPTER_MODE": None},
        # release 不能进入 RI4 的 test_readonly 形态。
        {"XIAOWEI_STARROCKS_ADAPTER_MODE": "test_readonly"},
        # 作用域固定 dev-local/dev，不接受任意环境。
        {"XIAOWEI_ENVIRONMENT_ID": "test"},
        {"XIAOWEI_ENVIRONMENT_ID": "prod"},
        # smoke 专用屏障不进入产品运行。
        {"XIAOWEI_SMOKE_STEP_BARRIER": "true"},
        # disabled 与 recording 一样不得携带真实 StarRocks 配置。
        {"XIAOWEI_STARROCKS_HOST": "starrocks.invalid"},
        # 闭集之外的运行形态名。
        {"XIAOWEI_RUNTIME_PROFILE": "production"},
    ],
)
def test_release_rejects_every_other_combination(overrides: dict[str, str | None]) -> None:
    env = dict(_RELEASE_ENV)
    for name, value in overrides.items():
        if value is None:
            env.pop(name)
        else:
            env[name] = value

    with pytest.raises(ConfigError):
        load_settings(env)


def test_disabled_starrocks_is_release_only() -> None:
    with pytest.raises(ConfigError):
        load_settings(
            {
                "XIAOWEI_ENVIRONMENT_ID": "dev",
                "XIAOWEI_STARROCKS_ADAPTER_MODE": "disabled",
            }
        )


def test_runtime_profile_errors_do_not_echo_the_rejected_value() -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings({**_RELEASE_ENV, "XIAOWEI_RUNTIME_PROFILE": "hunter" + "2-profile"})

    assert "hunter" + "2-profile" not in str(caught.value)


# --- 当前准入快照 / 历史渲染 binding 分离 ---------------------------------------


def _provider_off_snapshot() -> Any:
    from xiaowei_agent.capabilities.registry import PROVIDER_OFF_SNAPSHOT

    return PROVIDER_OFF_SNAPSHOT


async def _conversation_record(harness: RuntimeHarness) -> Any:
    """造一条无 plan、无 evidence 的普通对话终态。"""
    from xiaowei_agent.persistence.store import TransitionCommand

    record = await harness.store.create_task(
        submission=harness.submission("你能做什么？", idempotency_key="idem-chat")
    )
    grant = await harness.store.acquire_lease(
        task_id=record.task_id, owner="probe", ttl_seconds=60
    )
    assert grant is not None
    current = record
    for status in (TaskStatus.PLANNING, TaskStatus.RUNNING, TaskStatus.SUCCEEDED):
        result = await harness.store.transition(
            command=TransitionCommand(
                task_id=record.task_id,
                expected_version=current.version,
                to_status=status,
                fencing_token=grant.fencing_token,
                terminal_reason=(
                    CONVERSATION_TERMINAL_REASON
                    if status is TaskStatus.SUCCEEDED
                    else None
                ),
            )
        )
        assert result.applied, result.rejection
        current = result.winner
    return current


def test_provider_off_snapshot_is_empty_with_a_fixed_identity() -> None:
    from xiaowei_agent.capabilities.registry import (
        PROVIDER_OFF_SNAPSHOT_ID,
        StaticCapabilityRegistry,
    )

    snapshot = _provider_off_snapshot()

    assert snapshot.specs == ()
    assert snapshot.snapshot_id == PROVIDER_OFF_SNAPSHOT_ID
    assert snapshot.snapshot_id != StaticCapabilityRegistry().snapshot().snapshot_id


async def test_conversation_projects_the_current_snapshot_not_the_rendering_registry() -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    recorded_task = harness.lookup
    recorded_render = (await harness.runtime.query_task(lookup=recorded_task)).render
    conversation = await _conversation_record(harness)
    views = TaskViewRuntime(
        task_store=harness.store,
        plan_store=harness.plan_store,
        ledger=harness.ledger,
        conversation_snapshot=_provider_off_snapshot(),
        rendering_bindings=harness.runtime._rendering_bindings,
        model_artifacts=harness.model_artifacts,
        model_profile=ModelInvocationProfile(),
    )

    answer = await views.project_recorded(record=conversation)
    history = await views.query_task(lookup=recorded_task)

    # 普通对话只看当前准入快照：release 下没有任何可执行能力。
    assert answer.sections == ()
    assert "没有任何已注册能力" in answer.answer
    assert answer.refs == (f"capability-snapshot:{_provider_off_snapshot().snapshot_id}",)
    # 已持久化计划仍能按精确版本找到代码内渲染器，投影与执行时一致。
    assert history.status is TaskStatus.SUCCEEDED
    assert history.render == recorded_render


class _ConversationPort:
    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, request: object) -> InteractionModelResult:
        self.calls += 1
        return InteractionModelResult(
            draft=InteractionDraft(
                proposed_kind=InteractionKind.CONVERSATION,
                capability_draft=None,
                confidence=0.8,
                source=InteractionSource.MODEL,
            ),
            usage=ModelUsage(),
        )


async def test_embedded_worker_view_answers_from_the_admission_snapshot() -> None:
    port = _ConversationPort()
    harness = RuntimeHarness(GOLDEN, interaction_classifier=port, provider_off=True)

    payload = await harness.handle("你能做什么？")

    assert port.calls == 1
    assert payload.sections == ()
    assert payload.refs == (f"capability-snapshot:{_provider_off_snapshot().snapshot_id}",)
    assert harness.gateway.invocations == 0


async def test_provider_off_runtime_rejects_capability_requests_at_the_resolver() -> None:
    harness = RuntimeHarness(GOLDEN, provider_off=True)

    await harness.handle("最近30分钟有哪些慢查询")
    view = await harness.runtime.query_task(lookup=harness.lookup)

    assert view.status is TaskStatus.REJECTED
    assert harness.gateway.invocations == 0
    with pytest.raises(PlanNotFoundError):
        await harness.plan_store.load(task_id=harness.task_id)


# --- 装配根 -----------------------------------------------------------------


def _full_keys() -> set[tuple[str, str]]:
    from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry

    return {
        (spec.capability_id, spec.version)
        for spec in StaticCapabilityRegistry().snapshot().specs
    }


def _registry_keys(registry: Any) -> set[tuple[str, str]]:
    return set(registry._bindings)


async def test_release_in_memory_worker_has_no_adapters_and_rejects_before_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.UTC)
    clock = ManualClock(start=now)
    settings = _release_settings()
    stack = build_in_memory_local_stack(settings=settings, clock=clock)
    runtime = stack.runtime
    gateway = runtime._runner._gateway
    invocations = 0
    original = gateway.invoke

    async def counting(*args: object, **kwargs: object) -> object:
        nonlocal invocations
        invocations += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(gateway, "invoke", counting)

    assert runtime._snapshot.specs == ()
    assert _registry_keys(runtime._bindings) == set()
    assert _registry_keys(runtime._rendering_bindings) == _full_keys()
    assert runtime._task_views._conversation_snapshot is runtime._snapshot
    assert runtime._task_views._rendering_bindings is runtime._rendering_bindings

    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor="local-developer",
        environment_id=settings.environment_id,
        trace_id="0" * 32,
        policy_revision=stack.policy_revision,
    )
    pending = await runtime.submit_task(
        submission=TaskSubmission(
            envelope=RequestEnvelope(
                request_id="request-release-1",
                tenant_id=context.tenant_id,
                actor=context.actor,
                channel=Channel.API,
                text="检查最近三十分钟慢查询",
                idempotency_key="idem-release-1",
                environment_id=context.environment_id,
            ),
            context=context,
            as_of=now,
        )
    )
    worker = WorkerLoop(
        runtime=runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )

    assert await worker.poll_once() == 1
    done = await runtime.query_task(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    assert done.status is TaskStatus.REJECTED
    assert invocations == 0
    assert await stack.evidence_ledger.load(task_id=pending.task_id) == ()


def test_offline_in_memory_worker_keeps_the_full_recording_authority() -> None:
    stack = build_in_memory_local_stack(settings=Settings(environment_id="dev"))
    runtime = stack.runtime

    assert _registry_keys(runtime._bindings) == _full_keys()
    assert runtime._snapshot.specs != ()
    assert runtime._task_views._conversation_snapshot is runtime._snapshot


def test_release_rejects_a_live_starrocks_assembly() -> None:
    from xiaowei_agent.interfaces.local_stack import StarRocksLiveAssembly

    class _Probe:
        def probe(self, *_: object, **__: object) -> str:
            raise AssertionError("must not connect")

    with pytest.raises(ValueError, match="disabled"):
        build_in_memory_local_stack(
            settings=_release_settings(),
            starrocks_live_assembly=StarRocksLiveAssembly(
                identity_probe=_Probe(),  # type: ignore[arg-type]
                unredacted_rows_approved=True,
            ),
        )
