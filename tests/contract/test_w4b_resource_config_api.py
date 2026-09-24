"""W4b 资源维护 API：Local Admin-only、服务端 ID、安全投影与失败语义。

真实 ``create_app`` + 内存存储 + 临时三域目录。每条拒绝都同时断言文件指纹不变。
"""

import json
from typing import Any, Final

import pytest
from tests.contract.test_ri5_config_api import (
    _build,
    _client,
    _fingerprint,
    _json_headers,
    _seed_feishu_session,
    _sign_in,
)

from xiaowei_agent.contracts import AdminAuditOutcome, AdminAuditReasonCode
from xiaowei_agent.interfaces.integration_config_file import read_resources_config

_PASSWORD: Final = "sr-api-" + "fake-password"
_TOKEN: Final = "prom-api-" + "fake-token"
_ID_1: Final = "1" * 32
_ID_2: Final = "2" * 32
_ID_3: Final = "3" * 32

_STARROCKS: Final[dict[str, Any]] = {
    "environment": "prod",
    "display_name": "核心 StarRocks",
    "host": "sr-fe.prod.example.internal",
    "port": 9030,
    "database": "ods_secretish_db",
    "username": "reader_account",
    "password": _PASSWORD,
    "tls_mode": "verify_identity",
    "enabled": True,
}
_PROMETHEUS: Final[dict[str, Any]] = {
    "environment": "staging",
    "display_name": "主 Prometheus",
    "base_url": "https://prom.example.internal/p",
    "auth_mode": "basic",
    "username": "grafana_reader",
    "secret": _TOKEN,
    "tls_mode": "verify_ca",
    "enabled": False,
}


class _Ids:
    def __init__(self, *values: str) -> None:
        self._values = list(values)

    def __call__(self) -> str:
        return self._values.pop(0)


def _built(tmp_path: Any, clock: Any, memory_state: Any, *ids: str, **kwargs: Any) -> Any:
    return _build(
        tmp_path,
        clock,
        memory_state,
        resource_id_factory=_Ids(*(ids or (_ID_1, _ID_2, _ID_3))),
        **kwargs,
    )


def _resource_audit(built: Any) -> list[tuple[str, AdminAuditOutcome, Any]]:
    return [
        (event.action.value, event.outcome, event.reason_code)
        for event in built.memory_state.admin_audit_events.values()
        if event.action.value in {"config_saved", "config_cleared"}
    ]


async def _post(client: Any, path: str, token: str, body: dict[str, Any]) -> Any:
    return await client.post(path, content=json.dumps(body), headers=_json_headers(token))


async def _create_both(client: Any, token: str) -> None:
    first = await _post(client, "/admin/api/resources/starrocks", token, _STARROCKS)
    second = await _post(client, "/admin/api/resources/prometheus", token, _PROMETHEUS)
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)


# --------------------------------------------------------------------------
# 正常闭环
# --------------------------------------------------------------------------


async def test_a_local_admin_can_create_list_update_clear_and_delete(
    tmp_path, clock, memory_state
) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        empty = await client.get("/admin/api/resources")
        created = await _post(client, "/admin/api/resources/starrocks", token, _STARROCKS)
        await _post(client, "/admin/api/resources/prometheus", token, _PROMETHEUS)
        listed = await client.get("/admin/api/resources")
        updated = await _post(
            client,
            "/admin/api/resources/update",
            token,
            {"resource_id": _ID_1, "port": 9031, "enabled": False},
        )
        cleared = await _post(
            client,
            "/admin/api/resources/clear-secret",
            token,
            {"resource_id": _ID_2, "confirm": True},
        )
        after_clear = await client.get("/admin/api/resources")
        deleted = await _post(
            client, "/admin/api/resources/delete", token, {"resource_id": _ID_1, "confirm": True}
        )
        final = await client.get("/admin/api/resources")

    assert empty.status_code == 200
    assert empty.json()["generation"] == 0 and empty.json()["resources"] == []
    assert created.json() == {"resource_id": _ID_1, "generation": 1, "restart_required": True}
    assert listed.json()["generation"] == 2
    assert listed.json()["resources"] == [
        {
            "kind": "starrocks",
            "resource_id": _ID_1,
            "environment": "prod",
            "display_name": "核心 StarRocks",
            "enabled": True,
            "host": "sr-fe.prod.example.internal",
            "port": 9030,
            "configured": True,
        },
        {
            "kind": "prometheus",
            "resource_id": _ID_2,
            "environment": "staging",
            "display_name": "主 Prometheus",
            "enabled": False,
            "base_url": "https://prom.example.internal/p",
            "configured": True,
        },
    ]
    assert updated.json()["generation"] == 3
    assert cleared.json()["generation"] == 4
    assert [item["configured"] for item in after_clear.json()["resources"]] == [True, False]
    assert deleted.json() == {"resource_id": _ID_1, "generation": 5, "restart_required": True}
    assert [item["resource_id"] for item in final.json()["resources"]] == [_ID_2]
    stored = read_resources_config(str(built.resources_path))
    assert stored.generation == 5
    assert [outcome for _, outcome, _ in _resource_audit(built)] == [
        AdminAuditOutcome.STARTED,
        AdminAuditOutcome.SUCCEEDED,
    ] * 5


async def test_the_projection_never_carries_secrets_usernames_or_connection_details(
    tmp_path, clock, memory_state
) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        responses = [
            await _post(client, "/admin/api/resources/starrocks", token, _STARROCKS),
            await _post(client, "/admin/api/resources/prometheus", token, _PROMETHEUS),
            await client.get("/admin/api/resources"),
            await client.get("/admin/api/integration-status"),
        ]
    text = "\n".join(response.text for response in responses)
    for forbidden in (
        _PASSWORD,
        _TOKEN,
        "reader_account",
        "grafana_reader",
        "ods_secretish_db",
        "password",
        "secret",
        "username",
        "tls_mode",
        "auth_mode",
    ):
        assert forbidden not in text, forbidden


async def test_the_page_state_says_saved_but_not_connected(tmp_path, clock, memory_state) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        await _post(client, "/admin/api/resources/starrocks", token, _STARROCKS)
        view = (await client.get("/admin/api/resources")).json()
    assert view["domain"] == "resources"
    assert view["connected"] is False
    # worker 还没加载这一代：待重启。
    assert view["pending_restart_services"] == ["worker"]


# --------------------------------------------------------------------------
# 客户端不能决定 ID、kind 或任意参数
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/admin/api/resources/starrocks", {**_STARROCKS, "resource_id": _ID_3}),
        ("/admin/api/resources/starrocks", {**_STARROCKS, "kind": "prometheus"}),
        ("/admin/api/resources/starrocks", {**_STARROCKS, "params": {"x": 1}}),
        ("/admin/api/resources/starrocks", {**_STARROCKS, "sql": "SELECT 1"}),
        ("/admin/api/resources/prometheus", {**_PROMETHEUS, "resource_id": _ID_3}),
        ("/admin/api/resources/prometheus", {**_PROMETHEUS, "headers": {"X": "y"}}),
        ("/admin/api/resources/starrocks", {**_STARROCKS, "password": ""}),
        ("/admin/api/resources/starrocks", {**_STARROCKS, "password": None}),
        ("/admin/api/resources/starrocks", {**_STARROCKS, "host": "http://sr-fe"}),
        ("/admin/api/resources/prometheus", {**_PROMETHEUS, "base_url": "https://u@p"}),
        ("/admin/api/resources/prometheus", {**_PROMETHEUS, "tls_mode": "disabled"}),
    ],
)
async def test_create_rejects_client_ids_kinds_arbitrary_params_and_bad_values(
    tmp_path, clock, memory_state, path: str, body: dict[str, Any]
) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        response = await _post(client, path, token, body)
    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_request"}}
    assert not built.resources_path.exists()


@pytest.mark.parametrize(
    "body",
    [
        {"resource_id": _ID_1, "kind": "prometheus"},
        {"resource_id": _ID_1, "new_resource_id": _ID_3},
        {"resource_id": _ID_1, "params": {"a": 1}},
        {"resource_id": _ID_1, "password": None},
        {"resource_id": _ID_1, "password": ""},
        {"resource_id": _ID_1, "display_name": None},
        {"resource_id": _ID_1, "base_url": "https://prom"},
        {"resource_id": "not-a-resource-id", "enabled": False},
        {"enabled": False},
    ],
)
async def test_update_rejects_kind_id_changes_nulls_and_foreign_fields(
    tmp_path, clock, memory_state, body: dict[str, Any]
) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        await _create_both(client, token)
        before = _fingerprint(built.resources_path)
        response = await _post(client, "/admin/api/resources/update", token, body)
    assert response.status_code == 400
    assert _fingerprint(built.resources_path) == before


async def test_an_unknown_resource_is_404_and_audited(tmp_path, clock, memory_state) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        await _create_both(client, token)
        before = _fingerprint(built.resources_path)
        responses = [
            await _post(
                client, "/admin/api/resources/update", token, {"resource_id": _ID_3, "port": 1}
            ),
            await _post(
                client,
                "/admin/api/resources/clear-secret",
                token,
                {"resource_id": _ID_3, "confirm": True},
            ),
            await _post(
                client,
                "/admin/api/resources/delete",
                token,
                {"resource_id": _ID_3, "confirm": True},
            ),
        ]
    assert [response.status_code for response in responses] == [404] * 3
    assert all(r.json() == {"error": {"code": "not_found"}} for r in responses)
    assert _fingerprint(built.resources_path) == before
    assert _resource_audit(built)[-1][1:] == (
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.TARGET_NOT_FOUND,
    )


async def test_an_id_collision_is_a_409_conflict(tmp_path, clock, memory_state) -> None:
    built = _built(tmp_path, clock, memory_state, _ID_1, _ID_1)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        await _post(client, "/admin/api/resources/starrocks", token, _STARROCKS)
        before = _fingerprint(built.resources_path)
        response = await _post(client, "/admin/api/resources/prometheus", token, _PROMETHEUS)
    assert response.status_code == 409
    assert response.json() == {"error": {"code": "conflict"}}
    assert _fingerprint(built.resources_path) == before


@pytest.mark.parametrize(
    "path", ["/admin/api/resources/clear-secret", "/admin/api/resources/delete"]
)
@pytest.mark.parametrize(
    "body", [{"resource_id": _ID_1}, {"resource_id": _ID_1, "confirm": False}]
)
async def test_clear_and_delete_require_an_explicit_confirmation(
    tmp_path, clock, memory_state, path: str, body: dict[str, Any]
) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        await _create_both(client, token)
        before = _fingerprint(built.resources_path)
        response = await _post(client, path, token, body)
    assert response.status_code == 400
    assert _fingerprint(built.resources_path) == before


# --------------------------------------------------------------------------
# 授权
# --------------------------------------------------------------------------


async def test_a_feishu_admin_is_refused_everywhere_with_denial_audit(
    tmp_path, clock, memory_state
) -> None:
    from xiaowei_agent.interfaces.web_auth import web_csrf_token

    built = _built(tmp_path, clock, memory_state, with_feishu_auth=True)
    cookie = await _seed_feishu_session(built.sessions)
    async with _client(built.app) as client:
        client.cookies.set("__Host-xiaowei-session", cookie)
        assert (await client.get("/app/api/me")).status_code == 200
        token = web_csrf_token(cookie)
        responses = [
            await client.get("/admin/api/resources"),
            await _post(client, "/admin/api/resources/starrocks", token, _STARROCKS),
            await _post(client, "/admin/api/resources/prometheus", token, _PROMETHEUS),
            await _post(
                client, "/admin/api/resources/update", token, {"resource_id": _ID_1, "port": 1}
            ),
            await _post(
                client,
                "/admin/api/resources/clear-secret",
                token,
                {"resource_id": _ID_1, "confirm": True},
            ),
            await _post(
                client,
                "/admin/api/resources/delete",
                token,
                {"resource_id": _ID_1, "confirm": True},
            ),
        ]
    assert [response.status_code for response in responses] == [403] * 6
    assert not built.resources_path.exists()
    assert [(o, r) for _, o, r in _resource_audit(built)] == [
        (AdminAuditOutcome.DENIED, AdminAuditReasonCode.AUTH_SOURCE_NOT_ALLOWED)
    ] * 5


async def test_an_anonymous_browser_gets_401_without_audit(tmp_path, clock, memory_state) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        responses = [
            await client.get("/admin/api/resources"),
            await _post(client, "/admin/api/resources/starrocks", "x", _STARROCKS),
        ]
    assert [response.status_code for response in responses] == [401, 401]
    assert _resource_audit(built) == []


async def test_a_write_without_the_csrf_token_is_refused(tmp_path, clock, memory_state) -> None:
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        await _sign_in(client, built.admins)
        response = await client.post(
            "/admin/api/resources/starrocks",
            content=json.dumps(_STARROCKS),
            headers=_json_headers(),
        )
    assert response.status_code == 403
    assert not built.resources_path.exists()


async def test_an_ordinary_update_to_auth_none_is_refused_and_keeps_the_secret(
    tmp_path, clock, memory_state
) -> None:
    """复审 P1（正式路由）：切到 none 不能绕过"清除凭据…"确认框。"""
    built = _built(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        await _create_both(client, token)
        before = _fingerprint(built.resources_path)
        switch_to_none = {"resource_id": _ID_2, "auth_mode": "none"}
        refused = await _post(client, "/admin/api/resources/update", token, switch_to_none)
        after_refusal = _fingerprint(built.resources_path)
        cleared = await _post(
            client,
            "/admin/api/resources/clear-secret",
            token,
            {"resource_id": _ID_2, "confirm": True},
        )
        switched = await _post(client, "/admin/api/resources/update", token, switch_to_none)
    assert refused.status_code == 400
    assert after_refusal == before
    assert (cleared.status_code, switched.status_code) == (200, 200)
    assert switched.json()["generation"] == 4
    stored = read_resources_config(str(built.resources_path))
    assert stored.resources[1].secret_configured is False


async def test_an_unwritable_failure_terminal_is_503_not_a_business_error(
    tmp_path, clock, memory_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复审 P1（正式路由）：FAILED 终态写不进去时统一 503，而不是 404/409/400。"""
    from xiaowei_agent.contracts import AdminAuditOutcome as Outcome
    from xiaowei_agent.persistence.fake import InMemoryAdminAuditStore

    built = _built(tmp_path, clock, memory_state)
    real_terminal = InMemoryAdminAuditStore.append_terminal

    async def refuse_failed_terminal(self: Any, *, terminal: Any) -> Any:
        if terminal.outcome is Outcome.FAILED:
            raise RuntimeError("audit down")
        return await real_terminal(self, terminal=terminal)

    async with _client(built.app) as client:
        token = await _sign_in(client, built.admins)
        await _create_both(client, token)
        before = _fingerprint(built.resources_path)
        monkeypatch.setattr(InMemoryAdminAuditStore, "append_terminal", refuse_failed_terminal)
        responses = [
            await _post(
                client,
                "/admin/api/resources/delete",
                token,
                {"resource_id": _ID_3, "confirm": True},
            ),
            await _post(
                client, "/admin/api/resources/update", token, {"resource_id": _ID_1, "port": 0}
            ),
        ]
    assert [response.status_code for response in responses] == [503, 503]
    assert all(r.json() == {"error": {"code": "unavailable"}} for r in responses)
    assert _fingerprint(built.resources_path) == before
