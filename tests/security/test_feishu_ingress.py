"""飞书入口不能携带业务路由、安全链或执行权。"""

import ast
from dataclasses import fields
from pathlib import Path

import pytest
from tests.security.test_task_view_runtime_authority import (
    _TASK_VIEW_PROCESS_ALLOWED_MODULES,
    _loaded_xiaowei_modules_after,
)

from xiaowei_agent.interfaces.feishu_sdk import FeishuMessageEvent
from xiaowei_agent.interfaces.local_stack import FeishuListenerStack

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"


def _internal_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if (module or "").startswith("xiaowei_agent"):
            found.add(".".join((module or "").split(".")[:2]))
    return found


def test_listener_does_not_import_execution_or_governance_layers() -> None:
    imports = _internal_imports(_SRC / "interfaces/feishu_listener.py")
    assert not imports & {
        "xiaowei_agent.capabilities",
        "xiaowei_agent.governance",
        "xiaowei_agent.planning",
        "xiaowei_agent.runners",
        "xiaowei_agent.tools",
    }


def test_inbound_event_has_no_authority_fields() -> None:
    assert set(FeishuMessageEvent.model_fields) == {
        "event_schema",
        "event_id",
        "event_type",
        "app_id",
        "tenant_key",
        "sender_type",
        "sender_subject_ref",
        "message_id",
        "chat_id",
        "chat_type",
        "message_type",
        "text",
        "mentions",
    }
    assert not set(FeishuMessageEvent.model_fields) & {
        "actor",
        "environment_id",
        "permissions",
        "policy_revision",
        "tenant_id",
    }


def test_listener_stack_field_surface_has_no_execution_authority() -> None:
    names = {field.name for field in fields(FeishuListenerStack)}
    assert not names & {
        "gateway",
        "runner",
        "tool_adapter",
        "xiaowei_runtime",
    }
    assert names == {
        "listener",
        "transport",
        "provider_state",
        "load_receipts",
        "runtime",
        "task_store",
        "channel_store",
        "identity_directory",
        "submission_service",
        "clock",
        "settings",
        "readiness",
        "aclose",
        "policy_revision",
    }


def test_built_listener_process_has_exact_nonexecuting_module_surface() -> None:
    script = """
import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.fakes.feishu import RecordingFeishuInboundTransport
from xiaowei_agent.config import Settings
from xiaowei_agent.interfaces.local_stack import build_postgres_feishu_listener_stack
from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

async def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        postgres = root / "postgres-credential"
        postgres.write_text("local-" + "fixture", encoding="utf-8")
        identity = root / "identities.json"
        identity.write_text(json.dumps({
            "version": 1,
            "tenant_id": "dev-local",
            "environment_id": "dev",
            "entries": [],
        }), encoding="utf-8")
        stack = await build_postgres_feishu_listener_stack(
            settings=Settings(
                environment_id="dev",
                postgres_password_file=str(postgres),
                feishu_listener_enabled=True,
                feishu_tenant_key="tenant",
                feishu_bot_open_id="bot",
                feishu_identity_file=str(identity),
            ),
            transport=RecordingFeishuInboundTransport(),
            credentials=ProviderCredentials(
                feishu_app_id="cli_listener",
                feishu_app_secret="listener-" + "fixture-secret",
            ),
        )
        await stack.aclose()

asyncio.run(main())
assert "lark_oapi" not in sys.modules
"""
    loaded = _loaded_xiaowei_modules_after(script)
    internal_api_only = {
        "xiaowei_agent.interfaces.api",
        "xiaowei_agent.interfaces.auth",
        "xiaowei_agent.interfaces.body_limit",
        "xiaowei_agent.interfaces.http_models",
    }
    listener_only = {
        "xiaowei_agent.application.channel_submission",
        "xiaowei_agent.interfaces.feishu_identity",
        "xiaowei_agent.interfaces.feishu_listener",
        "xiaowei_agent.interfaces.feishu_sdk",
    }
    assert loaded == (
        set(_TASK_VIEW_PROCESS_ALLOWED_MODULES) - internal_api_only
    ) | listener_only
