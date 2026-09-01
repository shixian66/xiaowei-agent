"""测试期网络必须被阻断；不发起任何真实连接。"""

import socket

import pytest
from pytest_socket import SocketBlockedError

pytestmark = pytest.mark.security


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
