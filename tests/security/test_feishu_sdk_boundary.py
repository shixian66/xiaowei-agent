"""第三方飞书 SDK 只能存在于一个延迟加载的 typed seam。"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from xiaowei_agent.interfaces import feishu_sdk

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"
_SDK_SEAM = "interfaces/feishu_sdk.py"


def _loads_lark_sdk(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".", 1)[0] == "lark_oapi" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(
            ".", 1
        )[0] == "lark_oapi":
            return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("lark_oapi")
        ):
            return True
    return False


def test_only_the_sdk_seam_can_name_or_load_lark_oapi() -> None:
    loaders = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if _loads_lark_sdk(path)
    }
    assert loaders == {_SDK_SEAM}


def test_sdk_seam_contains_no_type_ignore_escape_hatch() -> None:
    source = (_SRC / _SDK_SEAM).read_text(encoding="utf-8")
    assert "type: ignore" not in source


def test_importing_listener_and_constructing_adapters_does_not_load_sdk() -> None:
    script = """
import sys
from xiaowei_agent.interfaces.feishu_listener import FeishuListener
from xiaowei_agent.interfaces.feishu_sdk import (
    FeishuSdkInboundTransport,
    FeishuSdkMessageAdapter,
    FeishuSdkMembershipAdapter,
)
FeishuSdkInboundTransport(app_id='app', app_secret_file='/missing')
FeishuSdkMembershipAdapter(
    tenant_id='dev-local', app_id='app', app_secret_file='/missing'
)
FeishuSdkMessageAdapter(app_id='app', app_secret_file='/missing')
assert 'lark_oapi' not in sys.modules
assert FeishuListener is not None
"""
    completed = subprocess.run(  # noqa: S603 -- 当前解释器与脚本均由测试控制
        [sys.executable, "-c", script],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_request_builder_fixes_member_identity_to_open_id() -> None:
    source = Path(feishu_sdk.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    builders = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_build_chat_members_request"
    ]
    assert len(builders) == 1
    literals = {
        node.value
        for node in ast.walk(builders[0])
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "open_id" in literals
