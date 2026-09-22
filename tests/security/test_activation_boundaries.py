"""W1b 入口只创建激活事实或发送通用卡片，不获得执行权。"""

import ast
import inspect
from pathlib import Path

import pytest

from xiaowei_agent.application.activation_notification import (
    ActivationNotificationService,
)
from xiaowei_agent.application.identity_activation import IdentityActivationService
from xiaowei_agent.interfaces.feishu_listener import FeishuListener
from xiaowei_agent.interfaces.web_auth import WebAuthService

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"


def _tree(relative: str) -> ast.Module:
    return ast.parse((_SRC / relative).read_text(encoding="utf-8"))


def _internal_packages(relative: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_tree(relative)):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if (module or "").startswith("xiaowei_agent"):
            found.add(".".join((module or "").split(".")[:2]))
    return found


def _method(relative: str, *, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in _tree(relative).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == method_name:
                    return member
                if isinstance(member, ast.AsyncFunctionDef) and member.name == method_name:
                    return member  # type: ignore[return-value]
    raise AssertionError(f"missing {class_name}.{method_name}")


def test_activation_application_modules_have_no_execution_authority() -> None:
    forbidden = {
        "xiaowei_agent.capabilities",
        "xiaowei_agent.evidence",
        "xiaowei_agent.planning",
        "xiaowei_agent.runners",
        "xiaowei_agent.tools",
    }
    for relative in (
        "application/identity_activation.py",
        "application/activation_notification.py",
    ):
        assert not (_internal_packages(relative) & forbidden)


def test_notification_is_one_attempt_without_retry_or_persistence() -> None:
    relative = "application/activation_notification.py"
    method = _method(
        relative,
        class_name="ActivationNotificationService",
        method_name="notify",
    )
    sends = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_to_chat"
    ]

    assert len(sends) == 1
    assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(method))
    assert "xiaowei_agent.persistence" not in _internal_packages(relative)
    assert not {
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } & {"sleep", "retry"}


@pytest.mark.parametrize(
    ("constructor", "required"),
    [
        (WebAuthService.__init__, {"activations"}),
        (
            FeishuListener.__init__,
            {"activation_service", "activation_notifications"},
        ),
        (IdentityActivationService.__init__, {"activations", "directory", "audit"}),
        (ActivationNotificationService.__init__, {"messages"}),
    ],
)
def test_activation_dependencies_are_required_not_silent_defaults(
    constructor: object,
    required: set[str],
) -> None:
    parameters = inspect.signature(constructor).parameters
    assert {
        name
        for name in required
        if parameters[name].default is inspect.Parameter.empty
    } == required


def test_runtime_stacks_do_not_load_the_legacy_identity_file() -> None:
    tree = _tree("interfaces/local_stack.py")
    called = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert "load_feishu_identity_directory" not in called
    assert called.count("DirectoryFeishuIdentityDirectory") == 2
