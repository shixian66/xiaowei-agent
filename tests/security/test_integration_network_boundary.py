"""引入 integration 之后，默认路径仍然**没有**网络。

M4 给 ``tests/integration/`` 开了一个针对单个 host 的 socket 放行。放行本身是必要
的，危险的是它的**范围**——放宽一格就等于给全套件解除封锁，而且不会有任何用例失败。
这三条钉住范围：

1. 目录之外仍被拦（与 ``test_no_network.py`` 互补：那边断言"默认被拦"，这边断言
   "引入 integration 之后默认仍被拦"）。
2. 放行配置必须是**逐 item 的 marker**，不能是全局开关。
3. 仓库里不得出现裸用 ``socket_enabled``。

第 3 条针对的是 pytest-socket 0.8.1 的一个实测行为：``pytest_runtest_setup`` 命中
``socket_enabled`` 分支后**直接 return**，``allow_hosts`` 再也不会被解析，``connect``
完全不受保护。它看起来像"放行"，实际是全开。
"""

import ast
import socket
import tomllib
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]


def test_sockets_outside_the_integration_directory_are_still_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_name_resolution_outside_the_integration_directory_is_still_blocked() -> None:
    """``getaddrinfo`` 也必须被拦：只拦 ``socket()`` 挡不住 DNS 侧信道。"""
    with pytest.raises(SocketBlockedError):
        socket.getaddrinfo("example.invalid", 80)


def test_addopts_never_carries_a_global_allow_hosts() -> None:
    """全局 ``--allow-hosts`` 会让 ``disable_socket()`` 对**整个套件**永不执行。

    pytest-socket 只在 ``_resolve_allow_hosts`` 返回空时才 ``disable_socket``；命令行
    或 addopts 里的 ``--allow-hosts`` 对每个 item 都返回非空。于是一个"只是为了让
    integration 能连库"的开关，会静默解除全部用例的网络封锁。
    """
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]
    assert "--allow-hosts" not in addopts
    assert "--disable-socket" in addopts


def test_no_test_uses_the_bare_socket_enabled_fixture() -> None:
    """``socket_enabled`` 会让 ``allow_hosts`` 完全失效——它不是"放行"，是全开。"""
    offenders: list[str] = []
    for path in (_ROOT / "tests").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names = {argument.arg for argument in node.args.args}
                if "socket_enabled" in names:
                    offenders.append(f"{path.relative_to(_ROOT)}::{node.name}")
            if isinstance(node, ast.Attribute) and node.attr in {
                "enable_socket",
                "socket_enabled",
            }:
                offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"裸用 socket_enabled 会让 allow_hosts 失效：{offenders}"


def test_the_allowlist_is_built_from_the_dsn_host_only() -> None:
    """放行清单只能来自 DSN 解析出的 host，不能是通配或网段。

    形态照抄 CI 里 gitleaks allowlist 的窄度自检：允许清单本身也要被检查，否则
    "加一条就好了"会一点点把它变成 ``0.0.0.0/0``。
    """
    source = (_ROOT / "tests/integration/conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    marker_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "allow_hosts"
    ]
    assert len(marker_calls) == 1, "allow_hosts 只应在一处构造"
    (argument,) = marker_calls[0].args
    assert isinstance(argument, ast.List), "放行清单必须是显式列表"
    assert len(argument.elts) == 1, "只放行 DSN 的那一个 host"
    assert isinstance(argument.elts[0], ast.Name) and argument.elts[0].id == "host"
