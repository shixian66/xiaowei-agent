"""W4b 资源维护的唯一写服务：两阶段审计、服务端 ID、Secret 语义与失败路径。

每条失败路径都同时断言三件事：文件指纹不变、generation 不变、审计停在预期终态（或只剩
``STARTED``）。只断言返回值的用例无法区分"没写"与"写了但报错"。
"""

from pathlib import Path
from typing import Any, Final

import pytest
from tests.contract.test_w4a_integration_config_service import (
    _feishu_admin,
    _fingerprint,
    _FlakyAudit,
    _local_admin,
    _operator,
)

from xiaowei_agent.application.admin_identity import AdminIdentityActor
from xiaowei_agent.application.integration_config_service import (
    IntegrationConfigForbiddenError,
    IntegrationConfigService,
    IntegrationConfigUnavailableError,
    PrometheusDraft,
    ResourceConflictError,
    ResourceNotFoundError,
    ResourceRejectedError,
    ResourceUpdate,
    StarRocksDraft,
)
from xiaowei_agent.contracts import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    AiConfig,
    FeishuConfig,
    FeishuIntegration,
    GeminiIntegration,
)
from xiaowei_agent.contracts.admin_audit import admin_audit_target_digest
from xiaowei_agent.contracts.resource_config import (
    PrometheusResource,
    ResourcesConfig,
    StarRocksResource,
)
from xiaowei_agent.interfaces.integration_config_file import (
    read_resources_config,
    write_ai_config,
    write_feishu_config,
    write_resources_config,
)
from xiaowei_agent.interfaces.integration_config_repository import (
    FileIntegrationConfigRepository,
)
from xiaowei_agent.persistence.fake import (
    InMemoryAdminAuditStore,
    InMemoryProviderStateStore,
)

_PASSWORD: Final = "sr-svc-" + "fake-password"
_NEW_PASSWORD: Final = "sr-svc-" + "rotated-password"
_TOKEN: Final = "prom-svc-" + "fake-token"
_TRACE: Final = "0123456789abcdef0123456789abcdef"
_ID_1: Final = "1" * 32
_ID_2: Final = "2" * 32
_ID_3: Final = "3" * 32


class _Ids:
    """可控的服务端 ID factory：按顺序给出预设 ID，并记录被调用次数。"""

    def __init__(self, *values: str) -> None:
        self._values = list(values)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return self._values.pop(0)


class _Harness:
    def __init__(self, tmp_path: Path, clock: Any, memory_state: Any, ids: _Ids) -> None:
        self.root = tmp_path
        for domain in ("ai", "feishu", "resources"):
            (tmp_path / domain).mkdir()
        self.ai = tmp_path / "ai" / "config.json"
        self.feishu = tmp_path / "feishu" / "config.json"
        self.resources = tmp_path / "resources" / "config.json"
        self.memory_state = memory_state
        self.ids = ids
        self.audit = _FlakyAudit(InMemoryAdminAuditStore(clock=clock, state=memory_state))
        self.provider_state = InMemoryProviderStateStore(clock=clock, state=memory_state)
        self.service = IntegrationConfigService(
            repository=FileIntegrationConfigRepository(
                ai_path=str(self.ai),
                feishu_path=str(self.feishu),
                resources_path=str(self.resources),
            ),
            audit=self.audit,
            provider_state=self.provider_state,
            resource_id_factory=ids,
        )

    def outcomes(self) -> list[tuple[AdminAuditAction, AdminAuditOutcome, Any]]:
        # 手动时钟不前进，时间戳全同；内存审计表按追加顺序保存，就用它的写入顺序。
        events = list(self.memory_state.admin_audit_events.values())
        return [(e.action, e.outcome, e.reason_code) for e in events]

    def document(self) -> ResourcesConfig:
        return read_resources_config(str(self.resources))


@pytest.fixture
def ids() -> _Ids:
    return _Ids(_ID_1, _ID_2, _ID_3)


@pytest.fixture
def harness(tmp_path: Path, clock: Any, memory_state: Any, ids: _Ids) -> _Harness:
    return _Harness(tmp_path, clock, memory_state, ids)


def _starrocks_draft(**updates: Any) -> StarRocksDraft:
    values: dict[str, Any] = {
        "environment": "prod",
        "display_name": "核心 StarRocks",
        "host": "sr-fe.prod.example.internal",
        "port": 9030,
        "database": "ods",
        "username": "reader",
        "password": _PASSWORD,
        "tls_mode": "verify_identity",
        "enabled": True,
    }
    values.update(updates)
    return StarRocksDraft(**values)


def _prometheus_draft(**updates: Any) -> PrometheusDraft:
    values: dict[str, Any] = {
        "environment": "prod",
        "display_name": "主 Prometheus",
        "base_url": "https://prom.example.internal/p",
        "auth_mode": "bearer",
        "secret": _TOKEN,
        "tls_mode": "verify_ca",
        "enabled": True,
    }
    values.update(updates)
    return PrometheusDraft(**values)


def _target(resource_id: str) -> str:
    return admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.CONFIG, target_ref=f"resource:{resource_id}"
    )


async def _create_two(harness: _Harness) -> None:
    await harness.service.create_resource(
        actor=_local_admin(), trace_id="a" * 32, draft=_starrocks_draft()
    )
    await harness.service.create_resource(
        actor=_local_admin(), trace_id="b" * 32, draft=_prometheus_draft()
    )


# --------------------------------------------------------------------------
# 新建
# --------------------------------------------------------------------------


async def test_create_uses_a_server_generated_id_and_starts_generation_one(
    harness: _Harness,
) -> None:
    saved = await harness.service.create_resource(
        actor=_local_admin(), trace_id=_TRACE, draft=_starrocks_draft()
    )
    assert (saved.resource_id, saved.generation) == (_ID_1, 1)
    document = harness.document()
    assert document.generation == 1
    (resource,) = document.resources
    assert isinstance(resource, StarRocksResource)
    assert resource.resource_id == _ID_1
    assert resource.password == _PASSWORD
    assert harness.outcomes() == [
        (AdminAuditAction.CONFIG_SAVED, AdminAuditOutcome.STARTED, None),
        (AdminAuditAction.CONFIG_SAVED, AdminAuditOutcome.SUCCEEDED, None),
    ]
    started = next(iter(harness.memory_state.admin_audit_events.values()))
    assert started.operation_id == f"w4:{_TRACE}"
    assert started.target_ref_digest == _target(_ID_1)


async def test_every_write_advances_exactly_one_resources_generation(
    harness: _Harness,
) -> None:
    await _create_two(harness)
    assert harness.document().generation == 2
    await harness.service.update_resource(
        actor=_local_admin(),
        trace_id="c" * 32,
        resource_id=_ID_1,
        update=ResourceUpdate(display_name="改名"),
    )
    await harness.service.clear_resource_secret(
        actor=_local_admin(), trace_id="d" * 32, resource_id=_ID_2
    )
    await harness.service.delete_resource(
        actor=_local_admin(), trace_id="e" * 32, resource_id=_ID_1
    )
    document = harness.document()
    assert document.generation == 5
    assert [r.resource_id for r in document.resources] == [_ID_2]


async def test_resource_writes_never_touch_the_ai_or_feishu_domain(harness: _Harness) -> None:
    write_ai_config(
        str(harness.ai),
        AiConfig(generation=7, gemini=GeminiIntegration(enabled=True, api_key="k" + "ey")),
    )
    write_feishu_config(
        str(harness.feishu),
        FeishuConfig(generation=9, feishu=FeishuIntegration(enabled=False)),
    )
    before = (_fingerprint(harness.ai), _fingerprint(harness.feishu))
    await _create_two(harness)
    await harness.service.delete_resource(
        actor=_local_admin(), trace_id="c" * 32, resource_id=_ID_2
    )
    assert (_fingerprint(harness.ai), _fingerprint(harness.feishu)) == before


@pytest.mark.parametrize(
    "draft",
    [
        _starrocks_draft(password=None),
        _prometheus_draft(auth_mode="bearer", secret=None),
        _prometheus_draft(auth_mode="basic", username="grafana", secret=None),
    ],
    ids=["starrocks-without-password", "bearer-without-secret", "basic-without-secret"],
)
async def test_create_requires_the_secret_its_kind_needs(harness: _Harness, draft: Any) -> None:
    with pytest.raises(ResourceRejectedError):
        await harness.service.create_resource(
            actor=_local_admin(), trace_id=_TRACE, draft=draft
        )
    assert not harness.resources.exists()
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONFIG_SAVED,
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.CONFIG_INVALID,
    )


@pytest.mark.parametrize(
    "draft",
    [
        _starrocks_draft(host="http://sr"),
        _starrocks_draft(port=0),
        _prometheus_draft(base_url="https://u@prom"),
        _prometheus_draft(base_url="http://prom", tls_mode="verify_ca"),
        _prometheus_draft(auth_mode="none"),
    ],
)
async def test_invalid_parameters_fail_closed_after_started(
    harness: _Harness, draft: Any
) -> None:
    await harness.service.create_resource(
        actor=_local_admin(), trace_id="0" * 32, draft=_starrocks_draft()
    )
    before = _fingerprint(harness.resources)
    with pytest.raises(ResourceRejectedError):
        await harness.service.create_resource(
            actor=_local_admin(), trace_id=_TRACE, draft=draft
        )
    assert _fingerprint(harness.resources) == before
    assert harness.outcomes()[-1][1:] == (
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.CONFIG_INVALID,
    )


async def test_an_id_collision_is_a_closed_conflict_not_an_overwrite(
    tmp_path: Path, clock: Any, memory_state: Any
) -> None:
    """factory 撞上文档里已有的 ID：不覆盖旧资源、不无界重试、generation 不动。"""
    ids = _Ids(_ID_1, _ID_1, _ID_2)
    harness = _Harness(tmp_path, clock, memory_state, ids)
    await harness.service.create_resource(
        actor=_local_admin(), trace_id="a" * 32, draft=_starrocks_draft()
    )
    before = _fingerprint(harness.resources)
    with pytest.raises(ResourceConflictError):
        await harness.service.create_resource(
            actor=_local_admin(), trace_id="b" * 32, draft=_prometheus_draft()
        )
    assert ids.calls == 2
    assert _fingerprint(harness.resources) == before
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONFIG_SAVED,
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.CONFLICT,
    )
    (only,) = harness.document().resources
    assert isinstance(only, StarRocksResource) and only.password == _PASSWORD


async def test_the_one_hundred_and_first_resource_is_rejected(
    tmp_path: Path, clock: Any, memory_state: Any
) -> None:
    ids = _Ids(*(f"{index:032x}" for index in range(1, 102)))
    harness = _Harness(tmp_path, clock, memory_state, ids)
    write_resources_config(
        str(harness.resources),
        ResourcesConfig(
            generation=3,
            resources=tuple(
                StarRocksResource(
                    kind="starrocks",
                    resource_id=f"{index:032x}",
                    environment="dev",
                    display_name="d",
                    host="h",
                    port=1,
                    database="b",
                    username="u",
                    password=_PASSWORD,
                    tls_mode="disabled",
                    enabled=False,
                )
                for index in range(1000, 1100)
            ),
        ),
    )
    before = _fingerprint(harness.resources)
    with pytest.raises(ResourceRejectedError):
        await harness.service.create_resource(
            actor=_local_admin(), trace_id=_TRACE, draft=_starrocks_draft()
        )
    assert _fingerprint(harness.resources) == before


# --------------------------------------------------------------------------
# 修改、清 Secret、删除
# --------------------------------------------------------------------------


async def test_update_merges_fields_and_omitted_secret_is_kept(harness: _Harness) -> None:
    await _create_two(harness)
    await harness.service.update_resource(
        actor=_local_admin(),
        trace_id=_TRACE,
        resource_id=_ID_1,
        update=ResourceUpdate(host="10.0.0.9", port=9031, enabled=False),
    )
    starrocks, prometheus = harness.document().resources
    assert isinstance(starrocks, StarRocksResource)
    assert (starrocks.host, starrocks.port, starrocks.enabled) == ("10.0.0.9", 9031, False)
    assert starrocks.password == _PASSWORD
    # 另一种资源的 Secret、位置与内容都不被重写或重排。
    assert isinstance(prometheus, PrometheusResource) and prometheus.secret == _TOKEN


async def test_update_can_rotate_a_secret(harness: _Harness) -> None:
    await _create_two(harness)
    await harness.service.update_resource(
        actor=_local_admin(),
        trace_id=_TRACE,
        resource_id=_ID_1,
        update=ResourceUpdate(password=_NEW_PASSWORD),
    )
    starrocks, prometheus = harness.document().resources
    assert isinstance(starrocks, StarRocksResource) and starrocks.password == _NEW_PASSWORD
    assert isinstance(prometheus, PrometheusResource) and prometheus.secret == _TOKEN


@pytest.mark.parametrize(
    ("resource_id", "update"),
    [
        (_ID_1, ResourceUpdate(base_url="https://prom")),
        (_ID_1, ResourceUpdate(secret=_TOKEN)),
        (_ID_1, ResourceUpdate(auth_mode="none")),
        (_ID_2, ResourceUpdate(host="10.0.0.1")),
        (_ID_2, ResourceUpdate(password=_PASSWORD)),
        (_ID_2, ResourceUpdate(tls_mode="disabled")),  # https + disabled
        (_ID_1, ResourceUpdate(port=70000)),
    ],
)
async def test_an_update_that_does_not_fit_the_stored_kind_is_rejected(
    harness: _Harness, resource_id: str, update: ResourceUpdate
) -> None:
    await _create_two(harness)
    before = _fingerprint(harness.resources)
    with pytest.raises(ResourceRejectedError):
        await harness.service.update_resource(
            actor=_local_admin(), trace_id=_TRACE, resource_id=resource_id, update=update
        )
    assert _fingerprint(harness.resources) == before
    assert harness.outcomes()[-1][1:] == (
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.CONFIG_INVALID,
    )


async def test_switching_between_secret_bearing_auth_modes_keeps_the_secret(
    harness: _Harness,
) -> None:
    """bearer → basic 是普通修改：存量 Secret 保留，只有 username 随认证方式出现/消失。"""
    await _create_two(harness)
    await harness.service.update_resource(
        actor=_local_admin(),
        trace_id=_TRACE,
        resource_id=_ID_2,
        update=ResourceUpdate(auth_mode="basic", username="grafana"),
    )
    prometheus = harness.document().resources[1]
    assert isinstance(prometheus, PrometheusResource)
    assert (prometheus.auth_mode, prometheus.username, prometheus.secret) == (
        "basic",
        "grafana",
        _TOKEN,
    )


@pytest.mark.parametrize(
    "update",
    [
        ResourceUpdate(auth_mode="none"),
        ResourceUpdate(auth_mode="none", display_name="改名"),
        ResourceUpdate(auth_mode="none", secret="prom-svc-" + "replacement"),
    ],
    ids=["switch-only", "switch-with-other-field", "switch-with-new-secret"],
)
async def test_an_ordinary_update_can_never_drop_a_stored_secret(
    harness: _Harness, update: ResourceUpdate
) -> None:
    """复审 P1：普通修改切到 ``none`` 会绕过确认框永久丢掉只进不出的凭据。

    存量 Secret 非空时必须拒绝，文件、generation 与 Secret 都不动；``none`` 同时带新
    Secret 也拒绝，不能把输入静默丢掉。清除只能走独立确认动作。
    """
    await _create_two(harness)
    before = _fingerprint(harness.resources)
    with pytest.raises(ResourceRejectedError):
        await harness.service.update_resource(
            actor=_local_admin(), trace_id=_TRACE, resource_id=_ID_2, update=update
        )
    assert _fingerprint(harness.resources) == before
    prometheus = harness.document().resources[1]
    assert isinstance(prometheus, PrometheusResource) and prometheus.secret == _TOKEN
    assert harness.document().generation == 2
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONFIG_SAVED,
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.CONFIG_INVALID,
    )


async def test_explicitly_clearing_first_then_switching_to_none_succeeds(
    harness: _Harness,
) -> None:
    await _create_two(harness)
    await harness.service.clear_resource_secret(
        actor=_local_admin(), trace_id="c" * 32, resource_id=_ID_2
    )
    saved = await harness.service.update_resource(
        actor=_local_admin(),
        trace_id="d" * 32,
        resource_id=_ID_2,
        update=ResourceUpdate(auth_mode="none"),
    )
    assert saved.generation == 4
    prometheus = harness.document().resources[1]
    assert isinstance(prometheus, PrometheusResource)
    assert (prometheus.auth_mode, prometheus.username, prometheus.secret) == ("none", None, None)


async def test_an_ordinary_update_keeps_every_stored_secret(harness: _Harness) -> None:
    await _create_two(harness)
    for resource_id in (_ID_1, _ID_2):
        await harness.service.update_resource(
            actor=_local_admin(),
            trace_id=f"{resource_id[:1]}" * 32,
            resource_id=resource_id,
            update=ResourceUpdate(display_name="普通修改", enabled=False),
        )
    starrocks, prometheus = harness.document().resources
    assert isinstance(starrocks, StarRocksResource) and starrocks.password == _PASSWORD
    assert isinstance(prometheus, PrometheusResource) and prometheus.secret == _TOKEN


@pytest.mark.parametrize("outcome", ["not_found", "conflict", "rejected"])
async def test_a_failed_terminal_audit_is_unavailable_not_a_business_error(
    tmp_path: Path, clock: Any, memory_state: Any, outcome: str
) -> None:
    """复审 P1：业务失败的 FAILED 终态写不进去时，结果只能是 unavailable（503）。

    审计只剩 STARTED 表示"结果未知"；此时回 404/409/400 就是把未闭合的审计当成一次
    正常业务失败。文件与 generation 同样不能动。
    """
    ids = _Ids(_ID_1, _ID_1 if outcome == "conflict" else _ID_2)
    harness = _Harness(tmp_path, clock, memory_state, ids)
    await harness.service.create_resource(
        actor=_local_admin(), trace_id="a" * 32, draft=_starrocks_draft()
    )
    before = _fingerprint(harness.resources)
    events_before = len(harness.outcomes())
    harness.audit.fail_terminal = True
    with pytest.raises(IntegrationConfigUnavailableError):
        if outcome == "not_found":
            await harness.service.delete_resource(
                actor=_local_admin(), trace_id=_TRACE, resource_id=_ID_3
            )
        elif outcome == "conflict":
            await harness.service.create_resource(
                actor=_local_admin(), trace_id=_TRACE, draft=_prometheus_draft()
            )
        else:
            await harness.service.update_resource(
                actor=_local_admin(),
                trace_id=_TRACE,
                resource_id=_ID_1,
                update=ResourceUpdate(port=0),
            )
    assert _fingerprint(harness.resources) == before
    assert harness.document().generation == 1
    assert [o for _, o, _ in harness.outcomes()[events_before:]] == [AdminAuditOutcome.STARTED]


async def test_clearing_a_secret_keeps_the_resource_and_every_other_secret(
    harness: _Harness,
) -> None:
    await _create_two(harness)
    await harness.service.clear_resource_secret(
        actor=_local_admin(), trace_id=_TRACE, resource_id=_ID_1
    )
    starrocks, prometheus = harness.document().resources
    assert isinstance(starrocks, StarRocksResource)
    assert starrocks.password is None and starrocks.host == "sr-fe.prod.example.internal"
    assert isinstance(prometheus, PrometheusResource) and prometheus.secret == _TOKEN
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONFIG_CLEARED,
        AdminAuditOutcome.SUCCEEDED,
        None,
    )


async def test_delete_removes_only_that_resource(harness: _Harness) -> None:
    await _create_two(harness)
    await harness.service.delete_resource(
        actor=_local_admin(), trace_id=_TRACE, resource_id=_ID_1
    )
    (remaining,) = harness.document().resources
    assert isinstance(remaining, PrometheusResource) and remaining.secret == _TOKEN
    assert harness.outcomes()[-1] == (
        AdminAuditAction.CONFIG_CLEARED,
        AdminAuditOutcome.SUCCEEDED,
        None,
    )


@pytest.mark.parametrize("operation", ["update", "clear", "delete"])
async def test_an_unknown_resource_is_not_found_and_changes_nothing(
    harness: _Harness, operation: str
) -> None:
    await _create_two(harness)
    before = _fingerprint(harness.resources)
    with pytest.raises(ResourceNotFoundError):
        if operation == "update":
            await harness.service.update_resource(
                actor=_local_admin(),
                trace_id=_TRACE,
                resource_id=_ID_3,
                update=ResourceUpdate(enabled=False),
            )
        elif operation == "clear":
            await harness.service.clear_resource_secret(
                actor=_local_admin(), trace_id=_TRACE, resource_id=_ID_3
            )
        else:
            await harness.service.delete_resource(
                actor=_local_admin(), trace_id=_TRACE, resource_id=_ID_3
            )
    assert _fingerprint(harness.resources) == before
    assert harness.outcomes()[-1][1:] == (
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.TARGET_NOT_FOUND,
    )


async def test_an_update_on_a_missing_document_is_not_found(harness: _Harness) -> None:
    with pytest.raises(ResourceNotFoundError):
        await harness.service.delete_resource(
            actor=_local_admin(), trace_id=_TRACE, resource_id=_ID_1
        )
    assert not harness.resources.exists()


# --------------------------------------------------------------------------
# 授权与审计失败
# --------------------------------------------------------------------------


@pytest.mark.parametrize("actor", [_feishu_admin(), _operator()])
async def test_only_a_local_admin_may_change_resources(
    harness: _Harness, actor: AdminIdentityActor
) -> None:
    with pytest.raises(IntegrationConfigForbiddenError):
        await harness.service.create_resource(
            actor=actor, trace_id=_TRACE, draft=_starrocks_draft()
        )
    assert not harness.resources.exists()
    assert [outcome for _, outcome, _ in harness.outcomes()] == [AdminAuditOutcome.DENIED]
    # 被拒的请求连 ID 都不生成。
    assert harness.ids.calls == 0


async def test_started_failure_never_touches_the_file(harness: _Harness) -> None:
    await _create_two(harness)
    before = _fingerprint(harness.resources)
    harness.audit.fail_started = True
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.update_resource(
            actor=_local_admin(),
            trace_id=_TRACE,
            resource_id=_ID_1,
            update=ResourceUpdate(enabled=False),
        )
    assert _fingerprint(harness.resources) == before


async def test_terminal_failure_is_unavailable_and_leaves_only_started(
    harness: _Harness,
) -> None:
    harness.audit.fail_terminal = True
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.create_resource(
            actor=_local_admin(), trace_id=_TRACE, draft=_starrocks_draft()
        )
    # 文件可能已落盘；遗留的 STARTED 诚实表示"结果未知"，不能被当成成功。
    assert [outcome for _, outcome, _ in harness.outcomes()] == [AdminAuditOutcome.STARTED]


async def test_an_unreadable_document_is_never_rewritten_from_scratch(
    harness: _Harness,
) -> None:
    harness.resources.write_text("{broken", encoding="utf-8")
    before = _fingerprint(harness.resources)
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.create_resource(
            actor=_local_admin(), trace_id=_TRACE, draft=_starrocks_draft()
        )
    assert _fingerprint(harness.resources) == before
    assert harness.outcomes()[-1][1:] == (
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.CONFIG_INVALID,
    )


async def test_a_write_failure_is_audited_as_file_io(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xiaowei_agent.interfaces import integration_config_repository
    from xiaowei_agent.interfaces.integration_config_file import IntegrationConfigError

    await _create_two(harness)

    def _refuse(path: str, config: Any) -> None:
        raise IntegrationConfigError("integration config unavailable")

    monkeypatch.setattr(integration_config_repository, "write_resources_config", _refuse)
    with pytest.raises(IntegrationConfigUnavailableError):
        await harness.service.delete_resource(
            actor=_local_admin(), trace_id=_TRACE, resource_id=_ID_1
        )
    assert harness.document().generation == 2
    assert harness.outcomes()[-1][1:] == (
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.FILE_IO_FAILED,
    )


async def test_secrets_never_reach_the_audit_log(harness: _Harness) -> None:
    await _create_two(harness)
    await harness.service.update_resource(
        actor=_local_admin(),
        trace_id=_TRACE,
        resource_id=_ID_1,
        update=ResourceUpdate(password=_NEW_PASSWORD),
    )
    dumped = repr(list(harness.memory_state.admin_audit_events.values()))
    for secret in (_PASSWORD, _NEW_PASSWORD, _TOKEN):
        assert secret not in dumped


def test_drafts_and_updates_never_repr_a_secret() -> None:
    rendered = repr(
        (
            _starrocks_draft(),
            _prometheus_draft(),
            ResourceUpdate(password=_PASSWORD, secret=_TOKEN),
        )
    )
    assert _PASSWORD not in rendered and _TOKEN not in rendered


async def test_concurrent_creates_do_not_lose_an_update(
    tmp_path: Path, clock: Any, memory_state: Any
) -> None:
    import asyncio

    ids = _Ids(*(f"{index:032x}" for index in range(1, 9)))
    harness = _Harness(tmp_path, clock, memory_state, ids)
    await asyncio.gather(
        *(
            harness.service.create_resource(
                actor=_local_admin(), trace_id=f"{index:032x}", draft=_starrocks_draft()
            )
            for index in range(8)
        )
    )
    document = harness.document()
    assert document.generation == 8
    assert len(document.resources) == 8
