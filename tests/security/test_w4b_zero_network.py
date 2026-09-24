"""W4b 资源登记的零网络证明。

资源的新建、修改、清 Secret、删除与读取都只做本地语法与字段组合校验。这里在 pytest-socket
之外再装一层计数反证：DNS、TCP 连接、HTTP 传输、数据库驱动与 ToolGateway 任何一个被调用都
会被记下来。整条正式路由（真实 ``create_app`` + 文件 adapter）跑完后计数必须全为 0。

另用 AST 禁止 W4b 的契约、服务、文件边界与 Web 资源路由 import 或引用 ``tools.*``、数据库驱动、
HTTP client、``socket`` 或 Gateway——"没调用"之外还要"够不着"。
"""

import ast
import inspect
import json
import socket
import textwrap
from collections import Counter
from pathlib import Path
from typing import Any, Final

import pytest
from tests.contract.test_ri5_config_api import (
    _build,
    _client,
    _json_headers,
    _sign_in,
)

pytestmark = pytest.mark.security

_SRC: Final = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_PASSWORD: Final = "sr-net-" + "fake-password"
_TOKEN: Final = "prom-net-" + "fake-token"

_W4B_MODULES: Final = (
    "contracts/resource_config.py",
    "application/integration_config_service.py",
    "interfaces/integration_config_file.py",
    "interfaces/integration_config_repository.py",
)
_FORBIDDEN_IMPORT_ROOTS: Final = frozenset(
    {
        "socket",
        "ssl",
        "http",
        "urllib.request",
        "httpx",
        "requests",
        "aiohttp",
        "asyncpg",
        "psycopg",
        "psycopg2",
        "pymysql",
        "MySQLdb",
        "sqlalchemy",
        "xiaowei_agent.tools",
        "xiaowei_agent.runners",
        "xiaowei_agent.capabilities",
    }
)
_FORBIDDEN_NAMES: Final = frozenset(
    {
        "getaddrinfo",
        "gethostbyname",
        "create_connection",
        "urlopen",
        "ToolGateway",
        "DeterministicToolGateway",
        "invoke",
        "connect",
    }
)


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _forbidden(module: str) -> bool:
    return any(
        module == root or module.startswith(f"{root}.") for root in _FORBIDDEN_IMPORT_ROOTS
    )


@pytest.mark.parametrize("relative", _W4B_MODULES)
def test_w4b_modules_cannot_reach_network_drivers_or_the_gateway(relative: str) -> None:
    tree = ast.parse((_SRC / relative).read_text(encoding="utf-8"))
    offenders = sorted(module for module in _imported_modules(tree) if _forbidden(module))
    assert offenders == [], f"{relative} 引入了网络/驱动/Gateway：{offenders}"


def _resource_route_sources() -> dict[str, str]:
    """``create_app`` 里的资源路由函数源码（它们是闭包，只能从源码里取）。"""
    from xiaowei_agent.interfaces import web_app

    tree = ast.parse(textwrap.dedent(inspect.getsource(web_app.create_app)))
    sources: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        paths = [
            decorator.args[0].value
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[0].value, str)
        ]
        if any(str(path).startswith("/admin/api/resources") for path in paths):
            sources[node.name] = ast.unparse(node)
    return sources


def test_the_web_resource_routes_reference_no_network_name() -> None:
    sources = _resource_route_sources()
    assert set(sources) == {
        "read_resources",
        "create_starrocks_resource",
        "create_prometheus_resource",
        "update_resource",
        "clear_resource_secret",
        "delete_resource",
    }
    for name, source in sources.items():
        referenced = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        assert not referenced & _FORBIDDEN_NAMES, (name, referenced & _FORBIDDEN_NAMES)


def test_the_default_test_path_keeps_external_sockets_disabled() -> None:
    with pytest.raises(Exception, match="socket"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


class _Tripwires:
    """把每一个可能出网的入口换成计数器；被调用即记账并拒绝。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: Counter[str] = Counter()
        import http.client
        import urllib.request

        import asyncpg
        import httpx
        import pymysql

        from xiaowei_agent.tools.gateway import DeterministicToolGateway

        def trip(name: str) -> Any:
            def _tripped(*_: object, **__: object) -> Any:
                self.calls[name] += 1
                raise AssertionError(f"network entry {name} was called")

            return _tripped

        monkeypatch.setattr(socket, "getaddrinfo", trip("socket.getaddrinfo"))
        monkeypatch.setattr(socket, "gethostbyname", trip("socket.gethostbyname"))
        monkeypatch.setattr(socket, "create_connection", trip("socket.create_connection"))
        monkeypatch.setattr(http.client.HTTPConnection, "connect", trip("http.client.connect"))
        monkeypatch.setattr(urllib.request, "urlopen", trip("urllib.urlopen"))
        monkeypatch.setattr(
            httpx.AsyncHTTPTransport, "handle_async_request", trip("httpx.async_transport")
        )
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", trip("httpx.transport"))
        monkeypatch.setattr(asyncpg, "connect", trip("asyncpg.connect"))
        monkeypatch.setattr(pymysql, "connect", trip("pymysql.connect"))
        monkeypatch.setattr(DeterministicToolGateway, "invoke", trip("gateway.invoke"))


async def test_every_resource_operation_makes_zero_network_calls(
    tmp_path: Path, clock: Any, memory_state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _build(tmp_path, clock, memory_state)
    tripwires = _Tripwires(monkeypatch)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        headers = _json_headers(token)

        async def post(path: str, body: dict[str, Any]) -> Any:
            response = await client.post(path, content=json.dumps(body), headers=headers)
            assert response.status_code == 200, (path, response.text)
            return response.json()

        created = await post(
            "/admin/api/resources/starrocks",
            {
                "environment": "prod",
                "display_name": "sr",
                "host": "sr-fe.prod.example.internal",
                "port": 9030,
                "database": "ods",
                "username": "reader",
                "password": _PASSWORD,
                "tls_mode": "verify_identity",
                "enabled": True,
            },
        )
        prometheus = await post(
            "/admin/api/resources/prometheus",
            {
                "environment": "prod",
                "display_name": "prom",
                "base_url": "https://prom.example.internal/p",
                "auth_mode": "bearer",
                "secret": _TOKEN,
                "tls_mode": "verify_identity",
                "enabled": True,
            },
        )
        await post(
            "/admin/api/resources/update",
            {"resource_id": created["resource_id"], "host": "db.other.example.internal"},
        )
        await post(
            "/admin/api/resources/clear-secret",
            {"resource_id": prometheus["resource_id"], "confirm": True},
        )
        listed = await client.get("/admin/api/resources")
        status = await client.get("/admin/api/integration-status")
        await post(
            "/admin/api/resources/delete",
            {"resource_id": created["resource_id"], "confirm": True},
        )
    assert listed.status_code == status.status_code == 200
    assert sum(tripwires.calls.values()) == 0, dict(tripwires.calls)


def test_the_worker_side_resources_read_makes_zero_network_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from xiaowei_agent.config import Settings
    from xiaowei_agent.contracts import ConfigDomain
    from xiaowei_agent.contracts.resource_config import ResourcesConfig, StarRocksResource
    from xiaowei_agent.interfaces.integration_config_file import write_resources_config
    from xiaowei_agent.interfaces.provider_consumption import load_provider_credentials

    target = tmp_path / "config.json"
    write_resources_config(
        str(target),
        ResourcesConfig(
            generation=2,
            resources=(
                StarRocksResource(
                    kind="starrocks",
                    resource_id="7" * 32,
                    environment="prod",
                    display_name="sr",
                    host="sr-fe.prod.example.internal",
                    port=9030,
                    database="ods",
                    username="reader",
                    password=_PASSWORD,
                    tls_mode="verify_identity",
                    enabled=True,
                ),
            ),
        ),
    )
    tripwires = _Tripwires(monkeypatch)
    _, receipts = load_provider_credentials(
        settings=Settings(environment_id="dev"),
        service_name="worker",
        ai_path=str(tmp_path / "missing-ai.json"),
        resources_path=str(target),
    )
    assert receipts[("worker", ConfigDomain.RESOURCES)].generation == 2
    assert sum(tripwires.calls.values()) == 0, dict(tripwires.calls)
