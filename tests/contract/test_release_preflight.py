"""W5 首次 release 预检：不接管运行中任务，也不继承 recording 时代的执行结果。

既有 schema 无法证明某份 plan/evidence 出自哪个运行形态，所以任何历史 plan 或
evidence（包括 ``SUCCEEDED``）都让预检失败；只有无 plan、无 evidence 的对话或预计划
终态可以保留。预检只读：不删历史、不接受 ``--force``、重复运行不改任何事实。
"""

import ast
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.recordings import GOLDEN
from tests.fakes.runtime import RuntimeHarness

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    ReadinessReport,
    ScopeTaskPageQuery,
    TaskStatus,
)
from xiaowei_agent.interfaces import release_preflight
from xiaowei_agent.persistence.store import TransitionCommand
from xiaowei_agent.rendering.generic import CONVERSATION_TERMINAL_REASON

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"


class _Engine:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


class _Readiness:
    def __init__(self, *, database_ok: bool = True, head: bool = True) -> None:
        self._report = ReadinessReport(
            database_ok=database_ok,
            revision_matches_head=head and database_ok,
            assembled=True,
        )

    async def check(self) -> ReadinessReport:
        return self._report


def _config_root(tmp_path: Path) -> Path:
    root = tmp_path / "config"
    for domain in ("ai", "feishu", "resources"):
        (root / domain).mkdir(parents=True, exist_ok=True)
    return root


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness: RuntimeHarness,
    *,
    argv: list[str] | None = None,
    readiness: _Readiness | None = None,
    config_root: Path | None = None,
) -> tuple[int, dict[str, Any] | None, str, _Engine]:
    for name in list(os.environ):
        if name.startswith("XIAOWEI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XIAOWEI_ENVIRONMENT_ID", "dev")
    engine = _Engine()
    stdout, stderr = io.StringIO(), io.StringIO()
    code = release_preflight.main(
        [] if argv is None else argv,
        stdout=stdout,
        stderr=stderr,
        engine_factory=lambda _settings: engine,  # type: ignore[arg-type,return-value]
        stores_factory=lambda _engine, _clock: release_preflight.PreflightStores(
            task_store=harness.store,
            plan_store=harness.plan_store,
            ledger=harness.ledger,
            readiness=readiness or _Readiness(),
        ),
        config_root=str(config_root or _config_root(tmp_path)),
    )
    out = stdout.getvalue()
    return code, (json.loads(out) if out else None), stderr.getvalue(), engine


async def _terminal_without_plan(
    harness: RuntimeHarness,
    *,
    idempotency_key: str,
    status: TaskStatus,
    reason: str | None,
) -> str:
    record = await harness.store.create_task(
        submission=harness.submission("你能做什么？", idempotency_key=idempotency_key)
    )
    grant = await harness.store.acquire_lease(
        task_id=record.task_id, owner="probe", ttl_seconds=60
    )
    assert grant is not None
    current = record
    path: tuple[TaskStatus, ...] = {
        TaskStatus.PLANNING: (TaskStatus.PLANNING,),
        TaskStatus.SUCCEEDED: (
            TaskStatus.PLANNING,
            TaskStatus.RUNNING,
            TaskStatus.SUCCEEDED,
        ),
    }.get(status, (TaskStatus.PLANNING, status))
    for step in path:
        result = await harness.store.transition(
            command=TransitionCommand(
                task_id=record.task_id,
                expected_version=current.version,
                to_status=step,
                fencing_token=grant.fencing_token,
                terminal_reason=(
                    reason if step is status and step in TERMINAL_STATUSES else None
                ),
            )
        )
        assert result.applied, result.rejection
        current = result.winner
    return record.task_id


def test_empty_database_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    code, report, err, engine = _run(monkeypatch, tmp_path, RuntimeHarness(GOLDEN))

    assert (code, err) == (0, "")
    assert report == {
        "result": "ok",
        "tasks_total": 0,
        "non_terminal_tasks": 0,
        "tasks_with_execution_data": 0,
    }
    assert engine.disposed == 1


async def test_plan_free_terminals_are_kept(tmp_path: Path) -> None:
    harness = RuntimeHarness(GOLDEN)
    await _terminal_without_plan(
        harness,
        idempotency_key="chat",
        status=TaskStatus.SUCCEEDED,
        reason=CONVERSATION_TERMINAL_REASON,
    )
    await _terminal_without_plan(
        harness,
        idempotency_key="rejected",
        status=TaskStatus.REJECTED,
        reason="resolver.rejected",
    )
    await _terminal_without_plan(
        harness,
        idempotency_key="clarify",
        status=TaskStatus.CLARIFICATION_REQUIRED,
        reason=None,
    )

    code, report = await _scan(tmp_path, harness)

    assert code == 0
    assert report == {
        "result": "ok",
        "tasks_total": 3,
        "non_terminal_tasks": 0,
        "tasks_with_execution_data": 0,
    }


async def test_planning_is_not_drained(tmp_path: Path) -> None:
    harness = RuntimeHarness(GOLDEN)
    task_id = await _terminal_without_plan(
        harness, idempotency_key="planning", status=TaskStatus.PLANNING, reason=None
    )

    code, report = await _scan(tmp_path, harness)

    assert code == 1
    assert report == {
        "result": "tasks_not_drained",
        "tasks_total": 1,
        "non_terminal_tasks": 1,
        "tasks_with_execution_data": 0,
    }
    assert task_id not in json.dumps(report)


async def test_recording_era_success_with_plan_and_evidence_is_never_inherited(
    tmp_path: Path,
) -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    view = await harness.runtime.query_task(lookup=harness.lookup)
    assert view.status is TaskStatus.SUCCEEDED

    code, report = await _scan(tmp_path, harness)

    assert code == 1
    assert report == {
        "result": "historical_execution_data_present",
        "tasks_total": 1,
        "non_terminal_tasks": 0,
        "tasks_with_execution_data": 1,
    }
    assert harness.task_id not in json.dumps(report)


async def test_the_scan_crosses_the_hundred_task_page_limit(tmp_path: Path) -> None:
    harness = RuntimeHarness(GOLDEN)
    # 最早的一条带执行数据；它落在最后一页，只看首页的实现会放行。
    await harness.handle("最近30分钟有哪些慢查询", idempotency_key="oldest")
    for index in range(150):
        await _terminal_without_plan(
            harness,
            idempotency_key=f"chat-{index}",
            status=TaskStatus.SUCCEEDED,
            reason=CONVERSATION_TERMINAL_REASON,
        )

    code, report = await _scan(tmp_path, harness)

    assert code == 1
    assert report is not None
    assert report["result"] == "historical_execution_data_present"
    assert report["tasks_total"] == 151
    assert report["tasks_with_execution_data"] == 1


async def _facts(harness: RuntimeHarness) -> tuple[object, ...]:
    page = await harness.store.list_tasks_for_scope(
        query=ScopeTaskPageQuery(tenant_id="dev-local", environment_id="dev", limit=100)
    )
    facts: list[object] = []
    for item in page.items:
        facts.append(
            (
                item.record,
                await harness.plan_store.load(task_id=item.record.task_id),
                await harness.ledger.load(task_id=item.record.task_id),
            )
        )
    return tuple(facts)


async def test_preflight_is_read_only_and_repeatable(tmp_path: Path) -> None:
    harness = RuntimeHarness(GOLDEN)
    await harness.handle("最近30分钟有哪些慢查询")
    before = await _facts(harness)

    first = await _scan(tmp_path, harness)
    second = await _scan(tmp_path, harness)

    assert first == second
    assert await _facts(harness) == before
    assert await harness.plan_store.load(task_id=harness.task_id)
    assert await harness.ledger.load(task_id=harness.task_id)


def test_config_preflight_failure_stops_before_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _config_root(tmp_path)
    (root / "integrations.json").write_text("{}", encoding="utf-8")

    code, report, _, engine = _run(
        monkeypatch, tmp_path, RuntimeHarness(GOLDEN), config_root=root
    )

    assert code == 1
    assert report == {"result": "config_migration_required"}
    assert engine.disposed == 0


@pytest.mark.parametrize(
    ("readiness", "expected"),
    [
        (_Readiness(database_ok=False), "database_unavailable"),
        (_Readiness(head=False), "schema_not_at_head"),
    ],
)
def test_database_and_schema_are_checked_before_the_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    readiness: _Readiness,
    expected: str,
) -> None:
    code, report, _, engine = _run(
        monkeypatch, tmp_path, RuntimeHarness(GOLDEN), readiness=readiness
    )

    assert code == 1
    assert report == {"result": expected}
    assert engine.disposed == 1


@pytest.mark.parametrize(
    "argv", [["--force"], ["--ignore-history"], ["--tenant", "t"], ["--sql", "x"]]
)
def test_preflight_accepts_no_override_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> None:
    code, report, err, engine = _run(
        monkeypatch, tmp_path, RuntimeHarness(GOLDEN), argv=argv
    )

    assert (code, report) == (2, None)
    assert err == "release-preflight: usage_invalid\n"
    assert engine.disposed == 0


def test_terminal_classification_reuses_the_single_status_source() -> None:
    """终态判断直接复用 ``TERMINAL_STATUSES``，不手写第二份状态列表。"""
    tree = ast.parse(
        (_SRC / "interfaces" / "release_preflight.py").read_text(encoding="utf-8")
    )
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "TaskStatus"
    }
    assert "TERMINAL_STATUSES" in names
    assert attributes == set()


def test_preflight_never_reaches_registered_resources_or_providers() -> None:
    """网络面只有目标 PostgreSQL：不 import 工具、Provider 消费或资源配置读取。"""
    tree = ast.parse(
        (_SRC / "interfaces" / "release_preflight.py").read_text(encoding="utf-8")
    )
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = (
        "xiaowei_agent.tools",
        "xiaowei_agent.interfaces.provider_consumption",
        "xiaowei_agent.interfaces.provider_probe",
        "xiaowei_agent.interfaces.integration_config_repository",
        "xiaowei_agent.application",
    )
    assert not {module for module in imported if module.startswith(forbidden)}


async def _scan(tmp_path: Path, harness: RuntimeHarness) -> tuple[int, dict[str, Any]]:
    """异步核心：与 ``main()`` 同一条路径，只是不经 ``asyncio.run``。"""
    engine = _Engine()
    report = await release_preflight.run_release_preflight(
        settings=Settings(environment_id="dev"),
        engine_factory=lambda _settings: engine,  # type: ignore[arg-type,return-value]
        stores_factory=lambda _engine, _clock: release_preflight.PreflightStores(
            task_store=harness.store,
            plan_store=harness.plan_store,
            ledger=harness.ledger,
            readiness=_Readiness(),
        ),
        clock=harness.clock,
        config_root=str(_config_root(tmp_path)),
    )
    assert engine.disposed == 1
    return (0 if report["result"] == "ok" else 1), report
