"""W4a 唯一配置写服务：两阶段审计、域隔离、Secret 语义、并发与探针编排。

文件配置与 PostgreSQL 审计不在一个事务里，因此服务的承诺是**顺序**：先 ``STARTED``，
再文件/探针动作，最后 ``SUCCEEDED``/``FAILED``。本文件逐条证明：

* ``STARTED`` 写不进去时文件与探针调用次数都为 0；
* 文件失败写 ``FAILED``；终态写不进去时绝不回报成功；
* 拒绝先落 ``DENIED`` 审计再 403，拒绝审计写不进去同样 fail-closed；
* operation id 只由服务端 trace id 加 ``w4:`` 前缀派生，服务没有接收它的参数；
* 同域并发保存得到两个连续 generation 且不丢更新，跨域保存不改写另一域。
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from xiaowei_agent.application.admin_identity import AdminIdentityActor
from xiaowei_agent.application.integration_config_service import (
    ConfigSaved,
    FeishuUpdate,
    GeminiUpdate,
    IntegrationConfigForbiddenError,
    IntegrationConfigRepository,
    IntegrationConfigService,
    IntegrationConfigUnavailableError,
    IntegrationConfigUnreadableError,
    IntegrationConfigWriteError,
)
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
)
from xiaowei_agent.contracts.admin_audit import admin_audit_target_digest
from xiaowei_agent.interfaces.integration_config_file import (
    FileIntegrationConfigRepository,
    read_ai_config,
    read_feishu_config,
    write_ai_config,
    write_feishu_config,
)
from xiaowei_agent.persistence.fake import (
    InMemoryAdminAuditStore,
    InMemoryProviderStateStore,
)
from xiaowei_agent.persistence.provider_state import CheckName, ProviderTestErrorCode

_KEY: Final = "gemini-svc-" + "test-key"
_SECRET: Final = "feishu-svc-" + "test-secret"
_TRACE: Final = "0123456789abcdef0123456789abcdef"


def _local_admin() -> AdminIdentityActor:
    return AdminIdentityActor(
        user_id="local-admin",
        tenant_id="dev-local",
        environment_id="dev",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
        role=ProductRole.ADMIN,
        capabilities=frozenset(AdminCapability),
    )


def _feishu_admin() -> AdminIdentityActor:
    return AdminIdentityActor(
        user_id="user-alice",
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        auth_source=IdentitySource.FEISHU,
        role=ProductRole.ADMIN,
        capabilities=frozenset(AdminCapability)
        - {AdminCapability.MANAGE_INTEGRATIONS, AdminCapability.RUN_CONNECTION_TESTS},
    )


def _operator() -> AdminIdentityActor:
    return AdminIdentityActor(
        user_id="user-bob",
        tenant_id="dev-local",
        environment_id="dev",
        actor="bob",
        auth_source=IdentitySource.LOCAL_ADMIN,
        role=ProductRole.OPERATOR,
        capabilities=frozenset(),
    )


@dataclass
class _Paths:
    ai: Path
    feishu: Path


class _FlakyAudit:
    """在指定阶段让审计写入失败；其余委托给真实内存实现。"""

    def __init__(self, inner: InMemoryAdminAuditStore) -> None:
        self.inner = inner
        self.fail_started = False
        self.fail_terminal = False
        self.fail_denied = False

    async def append_started(self, *, start: Any) -> Any:
        if self.fail_started:
            raise RuntimeError("audit down")
        return await self.inner.append_started(start=start)

    async def append_terminal(self, *, terminal: Any) -> Any:
        if self.fail_terminal:
            raise RuntimeError("audit down")
        return await self.inner.append_terminal(terminal=terminal)

    async def append_denied(self, *, denial: Any) -> Any:
        if self.fail_denied:
            raise RuntimeError("audit down")
        return await self.inner.append_denied(denial=denial)

    async def load(self, *, event_id: str) -> Any:
        return await self.inner.load(event_id=event_id)

    async def list_events(self, *, query: Any) -> Any:
        return await self.inner.list_events(query=query)


class _Harness:
    def __init__(self, tmp_path: Path, clock: Any, memory_state: Any) -> None:
        self.paths = _Paths(
            ai=tmp_path / "ai" / "config.json", feishu=tmp_path / "feishu" / "config.json"
        )
        self.paths.ai.parent.mkdir()
        self.paths.feishu.parent.mkdir()
        self.memory_state = memory_state
        self.audit = _FlakyAudit(InMemoryAdminAuditStore(clock=clock, state=memory_state))
        self.provider_state = InMemoryProviderStateStore(clock=clock, state=memory_state)
        self.repository = FileIntegrationConfigRepository(
            ai_path=str(self.paths.ai), feishu_path=str(self.paths.feishu)
        )
        self.service = IntegrationConfigService(
            repository=self.repository,
            audit=self.audit,
            provider_state=self.provider_state,
        )

    def events(self) -> list[Any]:
        # 手动时钟不前进时同一操作的两条事件时间相同：按阶段排，STARTED 在前。
        return sorted(
            self.memory_state.admin_audit_events.values(),
            key=lambda event: (
                event.created_at,
                event.operation_id,
                event.outcome is not AdminAuditOutcome.STARTED,
            ),
        )

    def outcomes(self) -> list[tuple[AdminAuditAction, AdminAuditOutcome, Any]]:
        return [(e.action, e.outcome, e.reason_code) for e in self.events()]


@pytest.fixture
def harness(tmp_path: Path, clock: Any, memory_state: Any) -> _Harness:
    return _Harness(tmp_path, clock, memory_state)


def _fingerprint(path: Path) -> tuple[int, int, bytes] | None:
    if not path.exists():
        return None
    info = path.stat()
    return info.st_ino, info.st_mtime_ns, path.read_bytes()


# --------------------------------------------------------------------------
# 保存：两阶段审计与 Secret 语义
# --------------------------------------------------------------------------


async def test_saving_ai_writes_started_then_succeeded_with_a_w4_operation(
    harness: _Harness,
) -> None:
    saved = await harness.service.save_ai(
        actor=_local_admin(),
        trace_id=_TRACE,
        update=GeminiUpdate(enabled=True, api_key=_KEY),
    )
    assert saved == ConfigSaved(domain=ConfigDomain.AI, generation=1)
    assert read_ai_config(str(harness.paths.ai)) == AiConfig(
        generation=1, gemini=GeminiIntegration(enabled=True, api_key=_KEY)
    )
    assert not harness.paths.feishu.exists()
    events = harness.events()
    assert [(e.outcome, e.operation_id) for e in events] == [
        (AdminAuditOutcome.STARTED, f"w4:{_TRACE}"),
        (AdminAuditOutcome.SUCCEEDED, f"w4:{_TRACE}"),
    ]
    assert {e.action for e in events} == {AdminAuditAction.CONFIG_SAVED}
    assert {e.target_kind for e in events} == {AdminAuditTargetKind.CONFIG}
    assert {e.target_ref_digest for e in events} == {
        admin_audit_target_digest(
            target_kind=AdminAuditTargetKind.CONFIG, target_ref="config_domain:ai"
        )
    }


async def test_omitted_secret_is_kept_and_only_the_named_field_changes(
    harness: _Harness,
) -> None:
    write_ai_config(
        str(harness.paths.ai),
        AiConfig(generation=4, gemini=GeminiIntegration(enabled=False, api_key=_KEY)),
    )
    await harness.service.save_ai(
        actor=_local_admin(), trace_id=_TRACE, update=GeminiUpdate(enabled=True)
    )
    assert read_ai_config(str(harness.paths.ai)) == AiConfig(
        generation=5, gemini=GeminiIntegration(enabled=True, api_key=_KEY)
    )


async def test_saving_feishu_keeps_the_ai_file_inode_generation_and_secret(
    harness: _Harness,
) -> None:
    write_ai_config(
        str(harness.paths.ai),
        AiConfig(generation=3, gemini=GeminiIntegration(enabled=True, api_key=_KEY)),
    )
    before = _fingerprint(harness.paths.ai)
    saved = await harness.service.save_feishu(
        actor=_local_admin(),
        trace_id=_TRACE,
        update=FeishuUpdate(enabled=True, app_id="cli_svc", app_secret=_SECRET),
    )
    assert saved == ConfigSaved(domain=ConfigDomain.FEISHU, generation=1)
    assert _fingerprint(harness.paths.ai) == before


async def test_clearing_feishu_resets_only_that_domain(harness: _Harness) -> None:
    write_ai_config(
        str(harness.paths.ai),
        AiConfig(generation=3, gemini=GeminiIntegration(enabled=True, api_key=_KEY)),
    )
    write_feishu_config(
        str(harness.paths.feishu),
        FeishuConfig(
            generation=6,
            feishu=FeishuIntegration(enabled=True, app_id="cli_svc", app_secret=_SECRET),
        ),
    )
    ai_before = _fingerprint(harness.paths.ai)
    saved = await harness.service.clear_feishu(actor=_local_admin(), trace_id=_TRACE)
    assert saved == ConfigSaved(domain=ConfigDomain.FEISHU, generation=7)
    assert read_feishu_config(str(harness.paths.feishu)) == FeishuConfig(
        generation=7, feishu=FeishuIntegration()
    )
    assert _fingerprint(harness.paths.ai) == ai_before
    assert harness.outcomes() == [
        (AdminAuditAction.CONFIG_CLEARED, AdminAuditOutcome.STARTED, None),
        (AdminAuditAction.CONFIG_CLEARED, AdminAuditOutcome.SUCCEEDED, None),
    ]


async def test_started_failure_never_touches_the_file(harness: _Harness) -> None:
    harness.audit.fail_started = True
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.save_ai(
            actor=_local_admin(), trace_id=_TRACE, update=GeminiUpdate(enabled=True)
        )
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.clear_feishu(actor=_local_admin(), trace_id=_TRACE)
    assert not harness.paths.ai.exists()
    assert not harness.paths.feishu.exists()
    assert harness.events() == []


async def test_file_write_failure_records_failed_and_keeps_the_file(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ai_config(str(harness.paths.ai), AiConfig(generation=2, gemini=GeminiIntegration()))
    before = _fingerprint(harness.paths.ai)

    def _refuse(*, config: AiConfig) -> None:
        raise IntegrationConfigWriteError

    monkeypatch.setattr(harness.repository, "write_ai", _refuse)
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.save_ai(
            actor=_local_admin(), trace_id=_TRACE, update=GeminiUpdate(enabled=True)
        )
    assert _fingerprint(harness.paths.ai) == before
    assert harness.outcomes() == [
        (AdminAuditAction.CONFIG_SAVED, AdminAuditOutcome.STARTED, None),
        (
            AdminAuditAction.CONFIG_SAVED,
            AdminAuditOutcome.FAILED,
            AdminAuditReasonCode.FILE_IO_FAILED,
        ),
    ]


async def test_unreadable_current_file_records_config_invalid_and_is_not_overwritten(
    harness: _Harness,
) -> None:
    harness.paths.ai.write_text("{not json", encoding="utf-8")
    before = _fingerprint(harness.paths.ai)
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.save_ai(
            actor=_local_admin(), trace_id=_TRACE, update=GeminiUpdate(enabled=True)
        )
    assert _fingerprint(harness.paths.ai) == before
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONFIG_SAVED,
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.CONFIG_INVALID,
    )


async def test_terminal_audit_failure_is_never_reported_as_success(
    harness: _Harness,
) -> None:
    harness.audit.fail_terminal = True
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.save_ai(
            actor=_local_admin(), trace_id=_TRACE, update=GeminiUpdate(enabled=True)
        )
    # 文件可能已经落盘：遗留的 STARTED 就是"结果未知"的诚实表达。
    assert read_ai_config(str(harness.paths.ai)).generation == 1
    assert harness.outcomes() == [
        (AdminAuditAction.CONFIG_SAVED, AdminAuditOutcome.STARTED, None)
    ]


# --------------------------------------------------------------------------
# 拒绝：先审计再 403；拒绝审计写不进去同样 fail-closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor", "reason"),
    [
        (_feishu_admin(), AdminAuditReasonCode.AUTH_SOURCE_NOT_ALLOWED),
        (_operator(), AdminAuditReasonCode.ACTOR_NOT_ADMIN),
    ],
)
async def test_non_local_admin_is_denied_with_audit_and_zero_side_effects(
    harness: _Harness, actor: AdminIdentityActor, reason: AdminAuditReasonCode
) -> None:
    calls: list[object] = []

    async def _probe(config: object) -> object:
        calls.append(config)
        raise AssertionError("probe must not run")

    attempts: list[Callable[[], Awaitable[object]]] = [
        lambda: harness.service.save_ai(
            actor=actor, trace_id="1" * 32, update=GeminiUpdate(enabled=True)
        ),
        lambda: harness.service.clear_feishu(actor=actor, trace_id="2" * 32),
        lambda: harness.service.test_ai_connection(
            actor=actor, trace_id="3" * 32, probe=_probe  # type: ignore[arg-type]
        ),
    ]
    for attempt in attempts:
        with pytest.raises(IntegrationConfigForbiddenError):
            await attempt()
    assert calls == []
    assert not harness.paths.ai.exists() and not harness.paths.feishu.exists()
    assert sorted((e.action.value, e.outcome, e.reason_code) for e in harness.events()) == [
        (AdminAuditAction.CONFIG_CLEARED.value, AdminAuditOutcome.DENIED, reason),
        (AdminAuditAction.CONFIG_SAVED.value, AdminAuditOutcome.DENIED, reason),
        (AdminAuditAction.CONNECTION_TESTED.value, AdminAuditOutcome.DENIED, reason),
    ]


async def test_unwritable_denial_audit_fails_closed(harness: _Harness) -> None:
    harness.audit.fail_denied = True
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.save_ai(
            actor=_feishu_admin(), trace_id=_TRACE, update=GeminiUpdate(enabled=True)
        )
    assert not harness.paths.ai.exists()
    assert harness.events() == []


def test_no_service_method_accepts_a_client_operation_id() -> None:
    """operation id 只能由服务端 trace id 派生；方法签名里根本没有它的位置。"""
    for name, method in inspect.getmembers(IntegrationConfigService, inspect.isfunction):
        if name.startswith("_"):
            continue
        parameters = set(inspect.signature(method).parameters)
        assert "operation_id" not in parameters or name == "finish_oauth_test", name


# --------------------------------------------------------------------------
# 并发：同一服务实例的 read-modify-write 串行化
# --------------------------------------------------------------------------


async def test_concurrent_same_domain_saves_get_consecutive_generations_without_loss(
    harness: _Harness,
) -> None:
    results = await asyncio.gather(
        harness.service.save_ai(
            actor=_local_admin(),
            trace_id="a" * 32,
            update=GeminiUpdate(enabled=True),
        ),
        harness.service.save_ai(
            actor=_local_admin(),
            trace_id="b" * 32,
            update=GeminiUpdate(api_key=_KEY),
        ),
    )
    assert sorted(result.generation for result in results) == [1, 2]
    assert read_ai_config(str(harness.paths.ai)) == AiConfig(
        generation=2, gemini=GeminiIntegration(enabled=True, api_key=_KEY)
    )


async def test_the_write_is_serialised_even_when_io_yields(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件读写在线程里完成、读与写之间会交出控制权：没有锁时五次保存会读到同一代。"""
    real_read = harness.repository.read_ai

    def _slow_read() -> AiConfig | None:
        current = real_read()
        # 让其他协程有机会在"读完、还没写"的窗口里插进来。
        import time

        time.sleep(0.02)
        return current

    monkeypatch.setattr(harness.repository, "read_ai", _slow_read)
    results = await asyncio.gather(
        *(
            harness.service.save_ai(
                actor=_local_admin(),
                trace_id=f"{index:032x}",
                update=GeminiUpdate(enabled=bool(index % 2)),
            )
            for index in range(1, 6)
        )
    )
    assert sorted(result.generation for result in results) == [1, 2, 3, 4, 5]
    assert read_ai_config(str(harness.paths.ai)).generation == 5


# --------------------------------------------------------------------------
# 同步探针：两阶段审计 + 按域代次记测试结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Verdict:
    status: str
    duration_ms: int = 5
    error_code: ProviderTestErrorCode | None = None


async def test_ai_probe_gets_the_ai_config_and_records_the_ai_generation(
    harness: _Harness,
) -> None:
    write_ai_config(
        str(harness.paths.ai),
        AiConfig(generation=4, gemini=GeminiIntegration(enabled=True, api_key=_KEY)),
    )
    seen: list[AiConfig | None] = []

    async def _probe(config: AiConfig | None) -> _Verdict:
        seen.append(config)
        return _Verdict(status="passed")

    verdict = await harness.service.test_ai_connection(
        actor=_local_admin(), trace_id=_TRACE, probe=_probe
    )
    assert verdict.status == "passed"
    assert seen and seen[0] is not None and seen[0].generation == 4
    snapshot = await harness.provider_state.snapshot()
    assert snapshot.tests[CheckName.GEMINI_CONNECTION.value].generation == 4
    assert harness.outcomes() == [
        (AdminAuditAction.CONNECTION_TESTED, AdminAuditOutcome.STARTED, None),
        (AdminAuditAction.CONNECTION_TESTED, AdminAuditOutcome.SUCCEEDED, None),
    ]


async def test_failed_feishu_probe_records_failed_with_probe_reason(
    harness: _Harness,
) -> None:
    write_feishu_config(
        str(harness.paths.feishu),
        FeishuConfig(
            generation=2,
            feishu=FeishuIntegration(enabled=True, app_id="cli_svc", app_secret=_SECRET),
        ),
    )

    async def _probe(config: FeishuConfig | None) -> _Verdict:
        return _Verdict(status="failed", error_code=ProviderTestErrorCode.UNAUTHORIZED)

    verdict = await harness.service.test_feishu_credentials(
        actor=_local_admin(), trace_id=_TRACE, probe=_probe
    )
    assert verdict.status == "failed"
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONNECTION_TESTED,
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.PROBE_FAILED,
    )
    snapshot = await harness.provider_state.snapshot()
    assert snapshot.tests[CheckName.FEISHU_CREDENTIALS.value].generation == 2


async def test_probe_is_never_called_when_started_cannot_be_written(
    harness: _Harness,
) -> None:
    harness.audit.fail_started = True
    calls: list[object] = []

    async def _probe(config: AiConfig | None) -> _Verdict:
        calls.append(config)
        return _Verdict(status="passed")

    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.test_ai_connection(
            actor=_local_admin(), trace_id=_TRACE, probe=_probe
        )
    assert calls == []
    assert (await harness.provider_state.snapshot()).tests == {}


async def test_probe_terminal_failure_is_not_reported_as_a_result(
    harness: _Harness,
) -> None:
    harness.audit.fail_terminal = True

    async def _probe(config: AiConfig | None) -> _Verdict:
        return _Verdict(status="passed")

    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.test_ai_connection(
            actor=_local_admin(), trace_id=_TRACE, probe=_probe
        )


# --------------------------------------------------------------------------
# OAuth 跨回调：STARTED 的 operation id 随 state 走，回调写终态
# --------------------------------------------------------------------------


def _usable_feishu(harness: _Harness) -> None:
    write_feishu_config(
        str(harness.paths.feishu),
        FeishuConfig(
            generation=3,
            feishu=FeishuIntegration(enabled=True, app_id="cli_svc", app_secret=_SECRET),
        ),
    )


async def test_oauth_start_binds_the_state_to_the_started_operation(
    harness: _Harness,
) -> None:
    _usable_feishu(harness)
    issued: list[str] = []

    async def _refuse(config: FeishuConfig | None) -> _Verdict | None:
        return None

    async def _issue(operation_id: str) -> str:
        issued.append(operation_id)
        return "authorization-url"

    result = await harness.service.start_oauth_test(
        actor=_local_admin(), trace_id=_TRACE, refuse=_refuse, issue=_issue
    )
    assert result == "authorization-url"
    assert issued == [f"w4:{_TRACE}"]
    # 终态等回调：此刻只有 STARTED。
    assert harness.outcomes() == [
        (AdminAuditAction.CONNECTION_TESTED, AdminAuditOutcome.STARTED, None)
    ]


async def test_oauth_refusal_records_the_refusal_and_fails_the_operation(
    harness: _Harness,
) -> None:
    _usable_feishu(harness)

    async def _refuse(config: FeishuConfig | None) -> _Verdict | None:
        return _Verdict(
            status="failed", duration_ms=0, error_code=ProviderTestErrorCode.REAL_TEST_DISABLED
        )

    async def _issue(operation_id: str) -> str:
        raise AssertionError("must not issue")

    result = await harness.service.start_oauth_test(
        actor=_local_admin(), trace_id=_TRACE, refuse=_refuse, issue=_issue
    )
    assert isinstance(result, _Verdict)
    assert harness.outcomes()[-1][1:] == (
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.PROBE_FAILED,
    )


async def test_oauth_issue_failure_fails_the_operation(harness: _Harness) -> None:
    _usable_feishu(harness)

    async def _refuse(config: FeishuConfig | None) -> None:
        return None

    async def _issue(operation_id: str) -> str:
        raise RuntimeError("capacity")

    with pytest.raises(RuntimeError):
        await harness.service.start_oauth_test(
            actor=_local_admin(), trace_id=_TRACE, refuse=_refuse, issue=_issue
        )
    assert harness.outcomes()[-1][1:] == (
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.PROBE_FAILED,
    )


async def _start(harness: _Harness) -> str:
    _usable_feishu(harness)
    issued: list[str] = []

    async def _refuse(config: FeishuConfig | None) -> None:
        return None

    async def _issue(operation_id: str) -> str:
        issued.append(operation_id)
        return "url"

    await harness.service.start_oauth_test(
        actor=_local_admin(), trace_id=_TRACE, refuse=_refuse, issue=_issue
    )
    return issued[0]


@pytest.mark.parametrize("actor", [None, _feishu_admin(), _operator()])
async def test_oauth_finish_with_an_invalid_session_never_calls_the_provider(
    harness: _Harness, actor: AdminIdentityActor | None
) -> None:
    operation_id = await _start(harness)
    calls: list[object] = []

    async def _exchange() -> _Verdict:
        calls.append(1)
        return _Verdict(status="passed")

    result = await harness.service.finish_oauth_test(
        operation_id=operation_id, actor=actor, exchange=_exchange
    )
    assert result is None
    assert calls == []
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONNECTION_TESTED,
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.SESSION_INVALID,
    )
    assert (await harness.provider_state.snapshot()).tests == {}


async def test_oauth_finish_exchanges_once_and_records_the_result(
    harness: _Harness,
) -> None:
    operation_id = await _start(harness)

    async def _exchange() -> _Verdict:
        return _Verdict(status="passed")

    result = await harness.service.finish_oauth_test(
        operation_id=operation_id, actor=_local_admin(), exchange=_exchange
    )
    assert result is not None and result.status == "passed"
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONNECTION_TESTED,
        AdminAuditOutcome.SUCCEEDED,
        None,
    )
    snapshot = await harness.provider_state.snapshot()
    assert snapshot.tests[CheckName.FEISHU_OAUTH.value].generation == 3


async def test_oauth_finish_refuses_an_operation_that_is_not_an_open_oauth_start(
    harness: _Harness,
) -> None:
    async def _exchange() -> _Verdict:
        raise AssertionError("must not exchange")

    # 不存在的 operation、一次保存的 operation，以及已经结束的测试都不能再交换 code。
    await harness.service.save_ai(
        actor=_local_admin(), trace_id="c" * 32, update=GeminiUpdate(enabled=True)
    )
    finished = await _start(harness)
    await harness.service.finish_oauth_test(
        operation_id=finished, actor=None, exchange=_exchange
    )
    for operation_id in ("w4:" + "f" * 32, "w4:" + "c" * 32, finished, "trace-only"):
        with pytest.raises(IntegrationConfigUnavailableError):
            await harness.service.finish_oauth_test(
                operation_id=operation_id, actor=_local_admin(), exchange=_exchange
            )


def test_the_repository_protocol_lists_only_explicit_ai_and_feishu_methods() -> None:
    members = {
        name
        for name, _ in inspect.getmembers(IntegrationConfigRepository)
        if not name.startswith("_")
    }
    assert members == {"read_ai", "write_ai", "read_feishu", "write_feishu"}
    for name in members:
        parameters = set(inspect.signature(getattr(IntegrationConfigRepository, name)).parameters)
        assert not parameters & {"schema", "domain", "path", "serializer"}, name
    assert issubclass(IntegrationConfigUnreadableError, RuntimeError)
