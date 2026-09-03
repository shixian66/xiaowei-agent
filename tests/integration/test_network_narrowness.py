"""integration 目录内的放行必须**只**覆盖 DSN 的 host。

无 DSN 时跳过：此时目录里根本没有加 marker，也就没有"放行范围"可谈。
"""

import socket

import pytest
from pytest_socket import SocketConnectBlockedError
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.security


def test_connecting_to_a_host_outside_the_allowlist_is_blocked(postgres_dsn: str) -> None:
    """连一个不在放行清单里的地址必须被拦。

    用 ``192.0.2.1``（RFC 5737 TEST-NET-1，保证不可路由）：即便放行被误开，这个
    地址也不会真的连上任何东西，因此这条用例本身不产生任何外部流量。
    """
    with pytest.raises(SocketConnectBlockedError):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            probe.connect(("192.0.2.1", 5432))


def test_connecting_to_the_dsn_host_is_allowed(postgres_dsn: str) -> None:
    """反面：DSN 的 host 必须真的能连上，否则放行等于没开。"""
    url = make_url(postgres_dsn)
    assert url.host is not None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(5)
        probe.connect((url.host, url.port or 5432))
