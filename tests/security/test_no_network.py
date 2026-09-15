"""测试期网络必须被阻断；不发起任何真实连接。"""

import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]


def test_inet_socket_creation_is_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_inet6_socket_creation_is_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM)


def test_dns_resolution_is_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.getaddrinfo("example.invalid", 443)


def test_unix_socket_is_allowed_for_asyncio() -> None:
    """asyncio 依赖 AF_UNIX；阻断它会让异步用例直接 ERROR。"""
    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()


def test_import_config_and_default_local_stack_are_zero_network() -> None:
    code = """
import sys
def fail_network(event, args):
    if event.startswith('socket.'):
        raise AssertionError('network attempted')
sys.addaudithook(fail_network)
from xiaowei_agent.config import load_settings
from xiaowei_agent.interfaces.local_stack import build_in_memory_local_stack
settings = load_settings({'XIAOWEI_ENVIRONMENT_ID': 'dev'})
stack = build_in_memory_local_stack(settings=settings)
assert stack.interaction_classifier is None
assert stack.slow_query_advisory is None
"""
    result = subprocess.run(  # noqa: S603 -- 当前解释器固定执行内联审计脚本
        [sys.executable, "-c", code],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_import_gemini_sdk_seam_is_zero_network() -> None:
    code = """
import sys
def fail_network(event, args):
    if event.startswith('socket.'):
        raise AssertionError('network attempted')
sys.addaudithook(fail_network)
import xiaowei_agent.interfaces.gemini_model
"""
    result = subprocess.run(  # noqa: S603 -- 当前解释器固定执行内联审计脚本
        [sys.executable, "-c", code],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
