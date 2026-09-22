"""`integrations.json` 的配置读写接口。

三条路由只服务**本地管理员**：这是这台机器的运维入口，不是任何外部身份的权限。
Secret 只进不出——保存请求可以携带，查询响应永远只回"已配置/未配置"。
"""

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.fakes.activation import RecordingActivationRequests

from xiaowei_agent.application.integration_state import SERVICE_WORKER
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AuthenticatedPrincipal,
    ChannelPermission,
    IdentitySource,
    LoadReceipt,
    WebMode,
)
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.local_admin_auth import (
    INITIAL_LOCAL_ADMIN_PASSWORD,
    LocalAdminAuthService,
    hash_password,
)
from xiaowei_agent.interfaces.web_app import create_app
from xiaowei_agent.interfaces.web_auth import (
    WebAuthService,
    web_origin_digest,
    web_session_digest,
)
from xiaowei_agent.persistence.fake import (
    InMemoryLocalAdminStore,
    InMemoryProviderStateStore,
    InMemoryWebSessionStore,
)
from xiaowei_agent.persistence.web_session import RotateWebSessionCommand

_ORIGIN = "https://ops.example.test"
_CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]*)">')
_NEW_PASSWORD = "rotated-local-" + "admin-secret"
_GEMINI_KEY = "gemini-unit-" + "test-key"
_FEISHU_SECRET = "feishu-unit-" + "test-secret"


class _Probe:
    async def check(self) -> Any:  # pragma: no cover - 这些用例不打 readyz
        raise AssertionError("not used")


class _Unused:
    def __getattr__(self, name: str) -> Any:  # pragma: no cover
        raise AssertionError(name)


class _OAuth:
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> Any:
        raise AssertionError("not used")


def _feishu_admin() -> AuthenticatedPrincipal:
    """一个权限最大的飞书主体——用来证明"权限大"不等于"能改这台机器的配置"。"""
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        source=IdentitySource.FEISHU,
        subject_ref="subject-alice",
        permissions=frozenset(
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
                ChannelPermission.ADMIN_ALL_SAFE_TASKS,
            }
        ),
    )


def _build(
    tmp_path: Path,
    clock: Any,
    memory_state: Any,
    *,
    with_feishu_auth: bool = False,
    **settings_updates: object,
) -> Any:
    directory = tmp_path / ".config"
    directory.mkdir(exist_ok=True)
    config_path = directory / "integrations.json"
    sessions = InMemoryWebSessionStore(clock=clock, state=memory_state)
    admins = InMemoryLocalAdminStore(clock=clock, state=memory_state)
    provider_state = InMemoryProviderStateStore(clock=clock, state=memory_state)
    values: dict[str, object] = {
        "environment_id": "dev",
        "web_app_enabled": True,
        "web_mode": WebMode.HTTPS,
        "web_public_origin": _ORIGIN,
    }
    values.update(settings_updates)
    if with_feishu_auth:
        values["feishu_oauth_enabled"] = True
        values["feishu_identity_file"] = "/run/config/feishu-identities.json"
    settings = Settings(**values)  # type: ignore[arg-type]
    auth = (
        WebAuthService(
            sessions=sessions,
            identities=StaticFeishuIdentityDirectory(
                principals={"subject-alice": _feishu_admin()}
            ),
            activations=RecordingActivationRequests().as_service(),
            oauth=_OAuth(),
            public_origin=_ORIGIN,
            mode=WebMode.HTTPS,
            oauth_state_ttl_seconds=300,
            session_ttl_seconds=3600,
        )
        if with_feishu_auth
        else None
    )
    app = create_app(
        auth=auth,
        local_admin_auth=LocalAdminAuthService(
            admins=admins,
            sessions=sessions,
            public_origin_digest=web_origin_digest(_ORIGIN),
            session_ttl_seconds=3600,
        ),
        oauth_available=auth is not None,
        settings=settings,
        readiness=_Probe(),
        task_access=_Unused(),
        submissions=_Unused(),
        clock=clock,
        policy_revision="policy-2026-09-01",
        provider_state=provider_state,
        integration_config_path=str(config_path),
    )
    return app, admins, sessions, provider_state, config_path


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_ORIGIN
    )


def _json_headers(csrf_token: str | None = None) -> dict[str, str]:
    headers = {"origin": _ORIGIN, "content-type": "application/json"}
    if csrf_token is not None:
        headers["x-csrf-token"] = csrf_token
    return headers


async def _sign_in(client: httpx.AsyncClient, admins: Any) -> str:
    """走真实首启闭环拿到一个可用会话与 CSRF token。"""
    await admins.seed_if_absent(password_hash=hash_password(INITIAL_LOCAL_ADMIN_PASSWORD))
    await client.post(
        "/app/api/login",
        content=json.dumps({"password": INITIAL_LOCAL_ADMIN_PASSWORD}),
        headers=_json_headers(),
    )
    carried = _CSRF_META_RE.search((await client.get("/app")).text)
    assert carried is not None
    changed = await client.post(
        "/app/api/change-password",
        content=json.dumps(
            {
                "current_password": INITIAL_LOCAL_ADMIN_PASSWORD,
                "new_password": _NEW_PASSWORD,
            }
        ),
        headers=_json_headers(carried.group(1)),
    )
    assert changed.status_code == 200
    me = await client.get("/app/api/me")
    assert me.status_code == 200
    return me.json()["csrf_token"]


def _saved(config_path: Path) -> dict[str, Any]:
    return json.loads(config_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 读取：Secret 只进不出
# --------------------------------------------------------------------------


async def test_get_config_never_returns_secret_values(
    tmp_path, clock, memory_state
) -> None:
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    config_path.write_text(
        json.dumps(
            {
                "generation": 3,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
                "feishu": {
                    "enabled": True,
                    "app_id": "cli_visible",
                    "app_secret": _FEISHU_SECRET,
                },
            }
        ),
        encoding="utf-8",
    )
    async with _client(app) as client:
        await _sign_in(client, admins)
        response = await client.get("/app/api/config")

    assert response.status_code == 200
    body = response.json()
    assert _GEMINI_KEY not in response.text
    assert _FEISHU_SECRET not in response.text
    assert body["generation"] == 3
    assert body["gemini"] == {"enabled": True, "configured": True}
    # app_id 不是 secret：页面要显示它才能确认填的是哪个应用。
    assert body["feishu"] == {
        "enabled": True,
        "configured": True,
        "app_id": "cli_visible",
    }
    assert set(body["checks"]) == {
        "gemini_connection",
        "feishu_credentials",
        "feishu_oauth",
    }


async def test_get_config_reflects_the_load_receipts(
    tmp_path, clock, memory_state
) -> None:
    """状态不是凭空算的：回执齐了才从「待应用」走到「待测试」。"""
    app, admins, _, provider_state, config_path = _build(
        tmp_path, clock, memory_state, gemini_enabled=True
    )
    config_path.write_text(
        json.dumps(
            {
                "generation": 4,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
                "feishu": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    async with _client(app) as client:
        await _sign_in(client, admins)
        before = (await client.get("/app/api/config")).json()
        await provider_state.record_load(
            receipts={
                (SERVICE_WORKER, "gemini"): LoadReceipt(generation=4, status="loaded")
            }
        )
        after = (await client.get("/app/api/config")).json()
        await provider_state.record_load(
            receipts={
                (SERVICE_WORKER, "gemini"): LoadReceipt(generation=3, status="loaded")
            }
        )
        stale = (await client.get("/app/api/config")).json()

    assert before["checks"]["gemini_connection"] == "pending_restart"
    assert after["checks"]["gemini_connection"] == "pending_test"
    assert stale["checks"]["gemini_connection"] == "pending_restart"
    assert before["checks"]["feishu_credentials"] == "unconfigured"


# --------------------------------------------------------------------------
# 保存：未携带保留、携带替换、空串不算清除
# --------------------------------------------------------------------------


async def test_put_without_a_secret_field_keeps_the_existing_value(
    tmp_path, clock, memory_state
) -> None:
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    config_path.write_text(
        json.dumps(
            {
                "generation": 1,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
                "feishu": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        response = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"enabled": False}}),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 200
    document = _saved(config_path)
    assert document["gemini"]["enabled"] is False
    assert document["gemini"]["api_key"] == _GEMINI_KEY
    assert document["generation"] == 2


async def test_put_with_a_new_secret_replaces_it_and_bumps_generation(
    tmp_path, clock, memory_state
) -> None:
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    config_path.write_text(
        json.dumps(
            {
                "generation": 7,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
                "feishu": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    replacement = _GEMINI_KEY + "-rotated"
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        response = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"api_key": replacement}}),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 200
    document = _saved(config_path)
    assert document["gemini"]["api_key"] == replacement
    assert document["generation"] == 8


async def test_put_response_states_restart_required(
    tmp_path, clock, memory_state
) -> None:
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    config_path.write_text(
        json.dumps(
            {
                "generation": 1,
                "gemini": {"enabled": False},
                "feishu": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        response = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"enabled": True, "api_key": _GEMINI_KEY}}),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 200
    assert response.json() == {"generation": 2, "restart_required": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"gemini": {"api_key": ""}},
        {"feishu": {"app_secret": ""}},
        {"feishu": {"app_id": ""}},
    ],
    ids=["gemini-key", "feishu-secret", "feishu-app-id"],
)
async def test_empty_string_never_clears_a_secret(
    tmp_path, clock, memory_state, payload: dict[str, Any]
) -> None:
    """空串是**输入错误**，不是"清除"的暗示；清除必须走显式动作。"""
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    original = json.dumps(
        {
            "generation": 1,
            "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
            "feishu": {
                "enabled": True,
                "app_id": "cli_visible",
                "app_secret": _FEISHU_SECRET,
            },
        }
    )
    config_path.write_text(original, encoding="utf-8")
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        response = await client.put(
            "/app/api/config",
            content=json.dumps(payload),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 400
    assert config_path.read_text(encoding="utf-8") == original


async def test_clear_action_removes_the_secret_and_bumps_generation(
    tmp_path, clock, memory_state
) -> None:
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    config_path.write_text(
        json.dumps(
            {
                "generation": 2,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
                "feishu": {
                    "enabled": True,
                    "app_id": "cli_visible",
                    "app_secret": _FEISHU_SECRET,
                },
            }
        ),
        encoding="utf-8",
    )
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        response = await client.post(
            "/app/api/config/clear",
            content=json.dumps({"provider": "feishu"}),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 200
    assert response.json() == {"generation": 3, "restart_required": True}
    document = _saved(config_path)
    assert document["feishu"] == {"enabled": False}
    # 只清被点名的那一个：另一个 Provider 的凭据不受影响。
    assert document["gemini"]["api_key"] == _GEMINI_KEY


async def test_invalid_payload_does_not_replace_the_current_file(
    tmp_path, clock, memory_state
) -> None:
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    original = json.dumps(
        {
            "generation": 5,
            "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
            "feishu": {"enabled": False},
        }
    )
    config_path.write_text(original, encoding="utf-8")
    before = os.stat(config_path).st_mtime_ns
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        for payload in (
            {"gemini": {"enabled": "yes"}},
            {"gemini": {"api_key": "x" * 5000}},
            {"unknown_provider": {"enabled": True}},
            [],
        ):
            response = await client.put(
                "/app/api/config",
                content=json.dumps(payload),
                headers=_json_headers(csrf),
            )
            assert response.status_code == 400, payload

    assert config_path.read_text(encoding="utf-8") == original
    assert os.stat(config_path).st_mtime_ns == before


async def test_model_and_endpoint_are_read_only(
    tmp_path, clock, memory_state
) -> None:
    """固定常量不可编辑：模型、端点、API 版本由 ADR-015 钉死。"""
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    config_path.write_text(
        json.dumps(
            {
                "generation": 1,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
                "feishu": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        for field in ("model", "endpoint", "api_version"):
            response = await client.put(
                "/app/api/config",
                content=json.dumps({"gemini": {field: "anything"}}),
                headers=_json_headers(csrf),
            )
            assert response.status_code == 400, field


# --------------------------------------------------------------------------
# 干净部署与损坏文件：「不存在」是未配置，「存在但坏」是故障
# --------------------------------------------------------------------------


async def test_first_save_on_a_clean_deployment_creates_generation_1(
    tmp_path, clock, memory_state
) -> None:
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    assert not config_path.exists()
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        empty = await client.get("/app/api/config")
        assert empty.status_code == 200
        assert empty.json()["generation"] == 0
        assert empty.json()["checks"] == {
            "gemini_connection": "unconfigured",
            "feishu_credentials": "unconfigured",
            "feishu_oauth": "unconfigured",
        }
        saved = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"enabled": True, "api_key": _GEMINI_KEY}}),
            headers=_json_headers(csrf),
        )

    assert saved.status_code == 200
    assert saved.json() == {"generation": 1, "restart_required": True}
    assert _saved(config_path)["generation"] == 1


@pytest.mark.parametrize("shape", ["corrupt", "symlink"], ids=["corrupt", "symlink"])
async def test_a_corrupt_file_is_not_treated_as_absent(
    tmp_path, clock, memory_state, shape: str
) -> None:
    """存在但读不了必须是故障码。当成「未配置」会让下一次保存覆盖掉一份仍在用的配置。"""
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    if shape == "corrupt":
        config_path.write_text("{not json", encoding="utf-8")
        original = config_path.read_bytes()
    else:
        target = tmp_path / ".config" / "elsewhere.json"
        target.write_text(
            json.dumps(
                {
                    "generation": 1,
                    "gemini": {"enabled": False},
                    "feishu": {"enabled": False},
                }
            ),
            encoding="utf-8",
        )
        config_path.symlink_to(target)
        original = target.read_bytes()
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        read = await client.get("/app/api/config")
        written = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"enabled": True, "api_key": _GEMINI_KEY}}),
            headers=_json_headers(csrf),
        )

    assert read.status_code == 503
    assert read.json() == {"error": {"code": "unavailable"}}
    assert written.status_code == 503
    if shape == "corrupt":
        assert config_path.read_bytes() == original
    else:
        assert config_path.is_symlink()
        assert target.read_bytes() == original


# --------------------------------------------------------------------------
# 授权：三条路由只服务本地管理员
# --------------------------------------------------------------------------


async def _seed_feishu_session(sessions: Any) -> str:
    cookie = "feishu_session_cookie_1234567890"
    await sessions.rotate_session(
        command=RotateWebSessionCommand(
            session_digest=web_session_digest(cookie),
            subject_ref="subject-alice",
            ttl_seconds=3600,
            auth_source=IdentitySource.FEISHU,
            public_origin_digest=web_origin_digest(_ORIGIN),
        )
    )
    return cookie


async def test_config_routes_reject_a_feishu_principal_with_admin_permission(
    tmp_path, clock, memory_state
) -> None:
    """ADMIN_ALL_SAFE_TASKS 是任务可见范围，不是改这台机器配置的权限。"""
    from xiaowei_agent.interfaces.web_auth import web_csrf_token

    app, _, sessions, _, config_path = _build(
        tmp_path, clock, memory_state, with_feishu_auth=True
    )
    original = json.dumps(
        {
            "generation": 1,
            "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
            "feishu": {"enabled": False},
        }
    )
    config_path.write_text(original, encoding="utf-8")
    cookie = await _seed_feishu_session(sessions)

    async with _client(app) as client:
        client.cookies.set("__Host-xiaowei-session", cookie)
        # 先确认这条 session 本身是有效的——否则 403 可能只是"没登录"。
        assert (await client.get("/app/api/me")).status_code == 200
        refused = [
            await client.get("/app/api/config"),
            await client.put(
                "/app/api/config",
                content=json.dumps({"gemini": {"enabled": False}}),
                headers=_json_headers(web_csrf_token(cookie)),
            ),
            await client.post(
                "/app/api/config/clear",
                content=json.dumps({"provider": "gemini"}),
                headers=_json_headers(web_csrf_token(cookie)),
            ),
        ]

    assert [response.status_code for response in refused] == [403, 403, 403]
    assert all(
        response.json() == {"error": {"code": "forbidden"}} for response in refused
    )
    assert config_path.read_text(encoding="utf-8") == original


async def test_config_routes_refuse_an_anonymous_browser(
    tmp_path, clock, memory_state
) -> None:
    app, _, _, _, _ = _build(tmp_path, clock, memory_state)

    async with _client(app) as client:
        read = await client.get("/app/api/config")
        written = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"enabled": False}}),
            headers=_json_headers("0" * 64),
        )

    assert read.status_code == 401
    assert written.status_code == 401


async def test_config_writes_still_require_origin_and_csrf(
    tmp_path, clock, memory_state
) -> None:
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    config_path.write_text(
        json.dumps(
            {
                "generation": 1,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
                "feishu": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        without_csrf = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"enabled": False}}),
            headers=_json_headers(),
        )
        wrong_origin = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"enabled": False}}),
            headers={
                "origin": "https://evil.example",
                "content-type": "application/json",
                "x-csrf-token": csrf,
            },
        )

    assert without_csrf.status_code == 403
    assert wrong_origin.status_code == 403
    assert _saved(config_path)["gemini"]["enabled"] is True


async def test_configured_and_the_page_state_answer_the_same_question(
    tmp_path, clock, memory_state
) -> None:
    """反例：飞书有两个必填字段，只看 secret 会把半份配置显示成"已配置"。

    页面上的"已配置"与状态机的 ``required_fields_present`` 问的是同一件事，
    两处各写一份判定就会出现"显示已配置、状态却是未配置"的自相矛盾页面。
    """
    app, admins, _, _, config_path = _build(
        tmp_path, clock, memory_state, with_feishu_auth=True
    )
    config_path.write_text(
        json.dumps(
            {
                "generation": 1,
                "gemini": {"enabled": True},
                "feishu": {"enabled": True, "app_secret": _FEISHU_SECRET},
            }
        ),
        encoding="utf-8",
    )

    async with _client(app) as client:
        await _sign_in(client, admins)
        body = (await client.get("/app/api/config")).json()

    assert body["feishu"]["configured"] is False
    assert body["feishu"]["app_id"] is None
    assert body["checks"]["feishu_credentials"] == "unconfigured"
    assert body["checks"]["feishu_oauth"] == "unconfigured"
    # Gemini 侧同理：开了开关但没有 Key 不算已配置。
    assert body["gemini"] == {"enabled": True, "configured": False}
    assert body["checks"]["gemini_connection"] == "unconfigured"


@pytest.mark.parametrize(
    "payload",
    [
        {"gemini": {"api_key": None}},
        {"gemini": {"enabled": None}},
        {"feishu": {"app_secret": None}},
        {"feishu": {"app_id": None}},
        {"gemini": None},
    ],
    ids=["gemini-key", "gemini-enabled", "feishu-secret", "feishu-app-id", "provider"],
)
async def test_an_explicit_null_is_refused_like_an_empty_string(
    tmp_path, clock, memory_state, payload: dict[str, Any]
) -> None:
    """``null`` 与空串是同一类错误：用一个取值暗示"清除"。

    两者的差别只在于 ``null`` 更隐蔽——它会被"未携带 = 保留原值"那条规则悄悄
    吸收，请求返回 200 而什么都没发生，页面上看不出是哪一步没生效。
    """
    app, admins, _, _, config_path = _build(tmp_path, clock, memory_state)
    original = json.dumps(
        {
            "generation": 1,
            "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
            "feishu": {
                "enabled": True,
                "app_id": "cli_visible",
                "app_secret": _FEISHU_SECRET,
            },
        }
    )
    config_path.write_text(original, encoding="utf-8")

    async with _client(app) as client:
        csrf = await _sign_in(client, admins)
        response = await client.put(
            "/app/api/config",
            content=json.dumps(payload),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 400
    assert config_path.read_text(encoding="utf-8") == original


async def test_config_routes_refuse_before_the_forced_password_change(
    tmp_path, clock, memory_state
) -> None:
    """反例：``admin/admin`` 还没改掉时，配置面一个字节都不该动。

    首启那几分钟是整条链路最脆弱的窗口——初始口令是公开常量，而这几条路由写的是
    这台机器的明文 Provider 凭据。顺序必须是"先改密、再配置"，不能是"先配置、
    顺便提醒你改密"。
    """
    app, admins, _, provider_state, config_path = _build(tmp_path, clock, memory_state)
    await admins.seed_if_absent(
        password_hash=hash_password(INITIAL_LOCAL_ADMIN_PASSWORD)
    )
    async with _client(app) as client:
        await client.post(
            "/app/api/login",
            content=json.dumps({"password": INITIAL_LOCAL_ADMIN_PASSWORD}),
            headers=_json_headers(),
        )
        # 页面里渲染的 token 是真实浏览器唯一拿得到的那一个；用它才说明这条拒绝
        # 不是"缺 CSRF"而是"必须先改密"。
        carried = _CSRF_META_RE.search((await client.get("/app")).text)
        assert carried is not None
        token = carried.group(1)

        read = await client.get("/app/api/config")
        written = await client.put(
            "/app/api/config",
            content=json.dumps({"gemini": {"enabled": True, "api_key": _GEMINI_KEY}}),
            headers=_json_headers(token),
        )
        cleared = await client.post(
            "/app/api/config/clear",
            content=json.dumps({"provider": "gemini"}),
            headers=_json_headers(token),
        )

    for response in (read, written, cleared):
        assert response.status_code == 403
        assert response.json() == {"error": {"code": "password_change_required"}}
    # 文件没被创建，状态表里也没有任何行。
    assert not config_path.exists()
    snapshot = await provider_state.snapshot()
    assert snapshot.receipts == {}
    assert snapshot.tests == {}
