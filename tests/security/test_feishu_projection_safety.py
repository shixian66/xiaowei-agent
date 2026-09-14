"""飞书投影不得获得执行权、原始证据或不可信 URL/日志出口。"""

import ast
import asyncio
import datetime as dt
import logging
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.conftest import make_envelope, make_submission
from tests.security.test_task_view_runtime_authority import (
    _TASK_VIEW_PROCESS_ALLOWED_MODULES,
    _loaded_xiaowei_modules_after,
)

from xiaowei_agent.application import channel_projection as projection_module
from xiaowei_agent.application.channel_projection import (
    ChannelMessageError,
    ChannelProjectionService,
)
from xiaowei_agent.contracts import (
    DestinationKind,
    ProjectionErrorCode,
    ProjectionState,
    TaskLookup,
    TaskRecord,
    TaskView,
    task_query_path,
)
from xiaowei_agent.interfaces.local_stack import ChannelWorkerStack
from xiaowei_agent.persistence.channel import (
    ProjectionSubscription,
    ProjectionUpdateResult,
)
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from xiaowei_agent.rendering.feishu import RenderedFeishuCard

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"


def _internal_imports(relative: str) -> set[str]:
    tree = ast.parse((_SRC / relative).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if (module or "").startswith("xiaowei_agent"):
            found.add(".".join((module or "").split(".")[:2]))
    return found


def _called_attributes(relative: str) -> set[str]:
    tree = ast.parse((_SRC / relative).read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_renderer_and_projection_import_surfaces_are_closed() -> None:
    assert _internal_imports("rendering/feishu.py") == {
        "xiaowei_agent.contracts",
    }
    assert _internal_imports("application/channel_projection.py") == {
        "xiaowei_agent.contracts",
        "xiaowei_agent.persistence",
        "xiaowei_agent.redaction",
        "xiaowei_agent.rendering",
    }


def test_projection_has_no_task_execution_or_business_routing_call() -> None:
    called = _called_attributes("application/channel_projection.py")
    assert not (
        called
        & {
            "execute_task",
            "invoke",
            "resume",
            "submit",
            "transition",
        }
    )


def test_importing_worker_does_not_load_execution_or_sdk_modules() -> None:
    script = """
import sys
from xiaowei_agent.interfaces.feishu_worker import main
for forbidden in (
    'xiaowei_agent.application.runtime',
    'xiaowei_agent.runners.deterministic',
    'xiaowei_agent.runners.runner',
    'xiaowei_agent.tools.gateway',
    'lark_oapi',
):
    assert forbidden not in sys.modules, forbidden
assert main is not None
"""
    completed = subprocess.run(  # noqa: S603 -- 当前解释器与脚本均由测试控制
        [sys.executable, "-c", script],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_channel_worker_stack_field_surface_has_no_execution_authority() -> None:
    names = {field.name for field in fields(ChannelWorkerStack)}
    assert names == {
        "service",
        "message_port",
        "runtime",
        "task_store",
        "channel_store",
        "clock",
        "settings",
        "readiness",
        "aclose",
        "policy_revision",
    }
    assert not names & {"gateway", "runner", "tool_adapter", "xiaowei_runtime"}


def test_built_channel_worker_has_exact_nonexecuting_module_surface() -> None:
    script = """
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from xiaowei_agent.config import Settings
from xiaowei_agent.interfaces.feishu_worker import main as worker_main
from xiaowei_agent.interfaces.local_stack import build_postgres_channel_worker_stack

class Messages:
    async def send_to_chat(self, **kwargs):
        return 'message'
    async def send_to_user(self, **kwargs):
        return 'message'
    async def update_card(self, **kwargs):
        return None

async def probe():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        postgres = root / 'postgres-credential'
        postgres.write_text('local-' + 'fixture', encoding='utf-8')
        stack = await build_postgres_channel_worker_stack(
            settings=Settings(
                environment_id='dev',
                postgres_password_file=str(postgres),
                channel_worker_enabled=True,
                web_public_origin='https://ops.example.test',
            ),
            message_port=Messages(),
        )
        await stack.aclose()

asyncio.run(probe())
assert worker_main is not None
"""
    loaded = _loaded_xiaowei_modules_after(script)
    internal_api_only = {
        "xiaowei_agent.interfaces.api",
        "xiaowei_agent.interfaces.auth",
        "xiaowei_agent.interfaces.body_limit",
        "xiaowei_agent.interfaces.http_models",
    }
    projection_only = {
        "xiaowei_agent.application.channel_projection",
        "xiaowei_agent.interfaces.feishu_sdk",
        "xiaowei_agent.interfaces.feishu_worker",
        "xiaowei_agent.rendering.feishu",
    }
    assert loaded == (
        set(_TASK_VIEW_PROCESS_ALLOWED_MODULES) - internal_api_only
    ) | projection_only


@pytest.mark.asyncio
async def test_request_preview_is_scrubbed_before_it_enters_the_card(
    clock, context
) -> None:
    shaped = "token=" + "unit-test-sensitive-value"
    store = InMemoryTaskStore(clock=clock)
    record = await store.create_task(
        submission=make_submission(
            context,
            envelope=make_envelope(text=f"检查任务 {shaped}"),
            as_of=clock(),
        )
    )

    class Runtime:
        async def project_task(self, *, record: TaskRecord) -> TaskView:
            return TaskView(
                task_id=record.task_id,
                status=record.status,
                query_path=task_query_path(record.task_id),
            )

    service = ChannelProjectionService(
        runtime=Runtime(),
        task_store=store,
        channel_store=SimpleNamespace(),
        message_port=SimpleNamespace(),
        clock=clock,
        settings=SimpleNamespace(
            tenant_id="dev-local",
            environment_id="dev",
            web_public_origin="https://ops.example.test",
            projection_claim_ttl_seconds=15,
            projection_batch_limit=10,
            projection_provider_max_attempts=3,
            projection_provider_backoff_base_seconds=1.0,
            projection_retry_after_cap_seconds=30.0,
            projection_tenant_concurrency=2,
            projection_task_poll_base_seconds=2.0,
            projection_task_poll_cap_seconds=10.0,
            channel_worker_poll_interval_seconds=1.0,
        ),
        sleep=asyncio.sleep,
    )

    projected = await service._stable_card(
        lookup=TaskLookup(
            task_id=record.task_id,
            tenant_id=record.tenant_id,
            environment_id=record.environment_id,
        ),
        record=record,
    )

    assert projected is not None
    card, _, _ = projected
    assert shaped not in card.content_json
    assert "token=***" in card.content_json


def test_provider_failure_log_contains_only_closed_code_and_hashed_refs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    task_id = "task-sensitive-reference"
    subscription_id = "subscription-sensitive-reference"
    destination = "chat-sensitive-reference"
    subscription = ProjectionSubscription(
        subscription_id=subscription_id,
        task_id=task_id,
        destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
        destination_ref=destination,
        state=ProjectionState.PENDING_INITIAL,
        next_attempt_at=now,
        attempt_number=0,
        provider_failure_count=0,
    )
    service = object.__new__(ChannelProjectionService)
    service._settings = SimpleNamespace(
        tenant_id="dev-local",
        environment_id="dev",
    )

    with caplog.at_level(logging.WARNING):
        service._log_provider_failure(
            subscription,
            ChannelMessageError(
                error_code=ProjectionErrorCode.PROVIDER_UNAVAILABLE
            ),
        )

    assert "provider_unavailable" in caplog.records[0].failure_kind
    assert caplog.records[0].tenant_id == "dev-local"
    assert caplog.records[0].environment_id == "dev"
    assert task_id not in caplog.text
    assert subscription_id not in caplog.text
    assert destination not in caplog.text


def test_claim_lost_log_contains_scope_but_no_raw_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    task_id = "task-sensitive-claim-reference"
    subscription_id = "subscription-sensitive-claim-reference"
    destination = "chat-sensitive-claim-reference"
    subscription = ProjectionSubscription(
        subscription_id=subscription_id,
        task_id=task_id,
        destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
        destination_ref=destination,
        state=ProjectionState.PENDING_INITIAL,
        next_attempt_at=now,
        attempt_number=1,
        provider_failure_count=0,
    )
    service = object.__new__(ChannelProjectionService)
    service._settings = SimpleNamespace(
        tenant_id="dev-local",
        environment_id="dev",
    )

    with caplog.at_level(logging.WARNING):
        service._log_claim_lost(subscription)

    record = caplog.records[0]
    assert record.failure_kind == "claim_lost"
    assert record.tenant_id == "dev-local"
    assert record.environment_id == "dev"
    assert task_id not in caplog.text
    assert subscription_id not in caplog.text
    assert destination not in caplog.text


def test_claim_lost_logging_failure_does_not_turn_a_loser_into_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 9, 8, tzinfo=dt.UTC)
    subscription = ProjectionSubscription(
        subscription_id="subscription-logging-failure",
        task_id="task-logging-failure",
        destination_kind=DestinationKind.FEISHU_MESSAGE_CARD,
        destination_ref="chat-logging-failure",
        state=ProjectionState.PENDING_INITIAL,
        next_attempt_at=now,
        attempt_number=1,
        provider_failure_count=0,
    )
    service = object.__new__(ChannelProjectionService)
    service._settings = SimpleNamespace(
        tenant_id="dev-local",
        environment_id="dev",
    )

    def fail_logging(*_: object, **__: object) -> None:
        raise RuntimeError("logging backend unavailable")

    monkeypatch.setattr(projection_module._LOGGER, "warning", fail_logging)

    assert not service._claim_update_applied(
        subscription=subscription,
        result=ProjectionUpdateResult(applied=False, winner=subscription),
    )


def test_rendered_card_contract_cannot_carry_raw_fact_or_row_fields() -> None:
    assert set(RenderedFeishuCard.model_fields) == {
        "content_json",
        "payload_digest",
        "truncated",
    }
