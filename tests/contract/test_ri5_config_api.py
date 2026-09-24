"""W4a 按域拆分的 Admin 配置读写接口。

``/admin/api/config/{ai,feishu}`` 的读、写与显式清除只服务**本地管理员**：这是这台机器的
运维入口，不是任何外部身份的权限。Secret 只进不出——保存请求可以携带，查询响应永远只回
"已配置/未配置"。每个域各有自己的文件与 generation，一个域的写入绝不改写另一个域。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.fakes.activation import RecordingActivationRequests
from tests.fakes.admin_identity import UnusedAdminIdentity

from xiaowei_agent.application.integration_config_service import (
    IntegrationConfigService,
)
from xiaowei_agent.application.integration_state import SERVICE_WORKER
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AuthenticatedPrincipal,
    ChannelPermission,
    ConfigDomain,
    IdentitySource,
    LoadReceipt,
    ProductRole,
    WebMode,
)
from xiaowei_agent.interfaces.feishu_identity import StaticFeishuIdentityDirectory
from xiaowei_agent.interfaces.integration_config_repository import (
    FileIntegrationConfigRepository,
)
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
    InMemoryAdminAuditStore,
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


_CONFIG_ACTIONS = frozenset(
    {
        AdminAuditAction.CONFIG_SAVED,
        AdminAuditAction.CONFIG_CLEARED,
        AdminAuditAction.CONNECTION_TESTED,
    }
)


@dataclass
class _Built:
    app: Any
    admins: Any
    sessions: Any
    provider_state: Any
    memory_state: Any
    ai_path: Path
    feishu_path: Path
    resources_path: Path

    def audit(self) -> list[tuple[AdminAuditAction, AdminAuditOutcome, Any]]:
        """只看 W4a 的三个配置动作；首启 bootstrap 等目录审计不在考察范围。"""
        events = sorted(
            (
                event
                for event in self.memory_state.admin_audit_events.values()
                if event.action in _CONFIG_ACTIONS
            ),
            key=lambda event: (
                event.created_at,
                event.operation_id,
                event.outcome is not AdminAuditOutcome.STARTED,
            ),
        )
        return [(e.action, e.outcome, e.reason_code) for e in events]


def _build(
    tmp_path: Path,
    clock: Any,
    memory_state: Any,
    *,
    with_feishu_auth: bool = False,
    resource_id_factory: Any = None,
    **settings_updates: object,
) -> _Built:
    root = tmp_path / ".config"
    for domain in ("ai", "feishu", "resources"):
        (root / domain).mkdir(parents=True, exist_ok=True)
    ai_path = root / "ai" / "config.json"
    feishu_path = root / "feishu" / "config.json"
    resources_path = root / "resources" / "config.json"
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
    settings = Settings(**values)  # type: ignore[arg-type]
    auth = (
        WebAuthService(
            sessions=sessions,
            identities=StaticFeishuIdentityDirectory(
                principals={"subject-alice": _feishu_admin()},
                web_roles={"subject-alice": ProductRole.ADMIN},
                web_user_ids={"subject-alice": "user-alice"},
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
    integration_config = IntegrationConfigService(
        repository=FileIntegrationConfigRepository(
            ai_path=str(ai_path),
            feishu_path=str(feishu_path),
            resources_path=str(resources_path),
        ),
        audit=InMemoryAdminAuditStore(clock=clock, state=memory_state),
        provider_state=provider_state,
        **({} if resource_id_factory is None else {"resource_id_factory": resource_id_factory}),
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
        integration_config=integration_config,
        admin_identity=UnusedAdminIdentity(),
    )
    return _Built(
        app=app,
        admins=admins,
        sessions=sessions,
        provider_state=provider_state,
        memory_state=memory_state,
        ai_path=ai_path,
        feishu_path=feishu_path,
        resources_path=resources_path,
    )


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
        "/login/api/login",
        content=json.dumps(
            {
                "username": "admin",
                "password": INITIAL_LOCAL_ADMIN_PASSWORD,
                "return_intent": {"kind": "workbench"},
            }
        ),
        headers=_json_headers(),
    )
    carried = _CSRF_META_RE.search(
        (await client.get("/login?intent=workbench")).text
    )
    assert carried is not None
    changed = await client.post(
        "/login/api/change-password",
        content=json.dumps(
            {
                "current_password": INITIAL_LOCAL_ADMIN_PASSWORD,
                "new_password": _NEW_PASSWORD,
                "return_intent": {"kind": "workbench"},
            }
        ),
        headers=_json_headers(carried.group(1)),
    )
    assert changed.status_code == 200
    me = await client.get("/app/api/me")
    assert me.status_code == 200
    return me.json()["csrf_token"]


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _saved(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _fingerprint(path: Path) -> tuple[int, int, bytes] | None:
    if not path.exists():
        return None
    info = path.stat()
    return info.st_ino, info.st_mtime_ns, path.read_bytes()


def _seed_both(built: _Built, *, ai_generation: int = 3, feishu_generation: int = 5) -> None:
    _write(
        built.ai_path,
        {"generation": ai_generation, "gemini": {"enabled": True, "api_key": _GEMINI_KEY}},
    )
    _write(
        built.feishu_path,
        {
            "generation": feishu_generation,
            "feishu": {
                "enabled": True,
                "app_id": "cli_visible",
                "app_secret": _FEISHU_SECRET,
            },
        },
    )


# --------------------------------------------------------------------------
# 读取：Secret 只进不出，两个域各自投影
# --------------------------------------------------------------------------


async def test_get_domain_config_never_returns_secret_values(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    _seed_both(built)
    async with _client(built.app) as client:
        await _sign_in(client, built.admins)
        ai = await client.get("/admin/api/config/ai")
        feishu = await client.get("/admin/api/config/feishu")

    assert ai.status_code == 200 and feishu.status_code == 200
    for response in (ai, feishu):
        assert _GEMINI_KEY not in response.text
        assert _FEISHU_SECRET not in response.text
    assert ai.json() == {
        "domain": "ai",
        "generation": 3,
        "gemini": {"enabled": True, "configured": True},
        "checks": {"gemini_connection": "pending_test"},
        "pending_restart_services": [],
    }
    body = feishu.json()
    assert body["domain"] == "feishu"
    assert body["generation"] == 5
    # app_id 不是 secret：页面要显示它才能确认填的是哪个应用。
    assert body["feishu"] == {
        "enabled": True,
        "configured": True,
        "app_id": "cli_visible",
    }
    assert set(body["checks"]) == {"feishu_credentials", "feishu_oauth"}


async def test_domain_views_reflect_their_own_load_receipts(
    tmp_path, clock, memory_state
) -> None:
    """状态不是凭空算的：回执齐了才从「待应用」走到「待测试」，且只看本域回执。"""
    built = _build(tmp_path, clock, memory_state, gemini_enabled=True)
    _write(
        built.ai_path,
        {"generation": 4, "gemini": {"enabled": True, "api_key": _GEMINI_KEY}},
    )
    async with _client(built.app) as client:
        await _sign_in(client, built.admins)
        before = (await client.get("/admin/api/config/ai")).json()
        await built.provider_state.record_load(
            receipts={
                (SERVICE_WORKER, ConfigDomain.FEISHU): LoadReceipt(
                    generation=4, status="loaded"
                )
            }
        )
        wrong_domain = (await client.get("/admin/api/config/ai")).json()
        await built.provider_state.record_load(
            receipts={
                (SERVICE_WORKER, ConfigDomain.AI): LoadReceipt(generation=4, status="loaded")
            }
        )
        after = (await client.get("/admin/api/config/ai")).json()

    assert before["checks"]["gemini_connection"] == "pending_restart"
    assert before["pending_restart_services"] == ["worker"]
    assert wrong_domain["checks"]["gemini_connection"] == "pending_restart"
    assert after["checks"]["gemini_connection"] == "pending_test"
    assert after["pending_restart_services"] == []


# --------------------------------------------------------------------------
# 保存：未携带保留、携带替换、空串与 null 不算清除，另一域绝不被改写
# --------------------------------------------------------------------------


async def test_put_ai_without_a_secret_keeps_it_and_leaves_feishu_untouched(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    _seed_both(built, ai_generation=1)
    feishu_before = _fingerprint(built.feishu_path)
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        response = await client.put(
            "/admin/api/config/ai",
            content=json.dumps({"enabled": False}),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 200
    assert response.json() == {"domain": "ai", "generation": 2, "restart_required": True}
    assert _saved(built.ai_path) == {
        "generation": 2,
        "gemini": {"enabled": False, "api_key": _GEMINI_KEY},
    }
    assert _fingerprint(built.feishu_path) == feishu_before
    assert built.audit() == [
        (AdminAuditAction.CONFIG_SAVED, AdminAuditOutcome.STARTED, None),
        (AdminAuditAction.CONFIG_SAVED, AdminAuditOutcome.SUCCEEDED, None),
    ]


async def test_put_feishu_with_a_new_secret_replaces_it_and_leaves_ai_untouched(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    _seed_both(built, feishu_generation=7)
    ai_before = _fingerprint(built.ai_path)
    replacement = _FEISHU_SECRET + "-rotated"
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        response = await client.put(
            "/admin/api/config/feishu",
            content=json.dumps({"app_secret": replacement}),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 200
    assert response.json() == {
        "domain": "feishu",
        "generation": 8,
        "restart_required": True,
    }
    assert _saved(built.feishu_path)["feishu"]["app_secret"] == replacement
    assert _saved(built.feishu_path)["feishu"]["app_id"] == "cli_visible"
    assert _fingerprint(built.ai_path) == ai_before


@pytest.mark.parametrize(
    ("domain", "payload"),
    [
        ("ai", {"api_key": ""}),
        ("ai", {"api_key": None}),
        ("ai", {"enabled": None}),
        ("feishu", {"app_secret": ""}),
        ("feishu", {"app_secret": None}),
        ("feishu", {"app_id": ""}),
        ("feishu", {"app_id": None}),
    ],
    ids=[
        "ai-empty-key",
        "ai-null-key",
        "ai-null-enabled",
        "feishu-empty-secret",
        "feishu-null-secret",
        "feishu-empty-app-id",
        "feishu-null-app-id",
    ],
)
async def test_empty_string_or_null_never_clears_anything(
    tmp_path, clock, memory_state, domain: str, payload: dict[str, Any]
) -> None:
    """空串与 ``null`` 都是**输入错误**，不是"清除"的暗示；清除必须走显式确认动作。"""
    built = _build(tmp_path, clock, memory_state)
    _seed_both(built)
    before = (_fingerprint(built.ai_path), _fingerprint(built.feishu_path))
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        response = await client.put(
            f"/admin/api/config/{domain}",
            content=json.dumps(payload),
            headers=_json_headers(csrf),
        )

    assert response.status_code == 400
    assert (_fingerprint(built.ai_path), _fingerprint(built.feishu_path)) == before
    assert built.audit() == []


async def test_invalid_payloads_do_not_touch_any_file(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    _seed_both(built)
    before = (_fingerprint(built.ai_path), _fingerprint(built.feishu_path))
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        attempts = [
            ("ai", {"enabled": "yes"}),
            ("ai", {"api_key": "x" * 5000}),
            ("ai", {"app_secret": _FEISHU_SECRET}),  # 另一个域的字段
            ("ai", {"gemini": {"enabled": True}}),  # 旧组合形状
            ("feishu", {"api_key": _GEMINI_KEY}),
            ("feishu", {"feishu": {"enabled": True}}),
            ("ai", []),
            ("ai", {"model": "anything"}),
            ("ai", {"endpoint": "anything"}),
            ("ai", {"api_version": "anything"}),
            ("ai", {"operation_id": "w4:client-chosen"}),
            ("feishu", {"operation_id": "w4:client-chosen"}),
        ]
        for domain, payload in attempts:
            response = await client.put(
                f"/admin/api/config/{domain}",
                content=json.dumps(payload),
                headers=_json_headers(csrf),
            )
            assert response.status_code == 400, (domain, payload)

    assert (_fingerprint(built.ai_path), _fingerprint(built.feishu_path)) == before
    assert built.audit() == []


async def test_the_audit_operation_id_is_server_derived(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        response = await client.put(
            "/admin/api/config/ai",
            content=json.dumps({"enabled": True}),
            headers={**_json_headers(csrf), "x-operation-id": "w4:client-chosen"},
        )

    assert response.status_code == 200
    operations = {
        e.operation_id
        for e in memory_state.admin_audit_events.values()
        if e.action in _CONFIG_ACTIONS
    }
    assert len(operations) == 1
    (operation,) = operations
    assert re.fullmatch(r"w4:[0-9a-f]{32}", operation)


# --------------------------------------------------------------------------
# 清除：独立确认动作，只清被点名的域
# --------------------------------------------------------------------------


async def test_clear_requires_confirmation_and_only_clears_that_domain(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    _seed_both(built, ai_generation=2, feishu_generation=6)
    ai_before = _fingerprint(built.ai_path)
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        unconfirmed = [
            await client.post(
                "/admin/api/config/feishu/clear",
                content=body,
                headers=_json_headers(csrf),
            )
            for body in ("{}", json.dumps({"confirm": False}), json.dumps({"provider": "feishu"}))
        ]
        confirmed = await client.post(
            "/admin/api/config/feishu/clear",
            content=json.dumps({"confirm": True}),
            headers=_json_headers(csrf),
        )

    assert [response.status_code for response in unconfirmed] == [400, 400, 400]
    assert confirmed.status_code == 200
    assert confirmed.json() == {
        "domain": "feishu",
        "generation": 7,
        "restart_required": True,
    }
    assert _saved(built.feishu_path) == {"generation": 7, "feishu": {"enabled": False}}
    assert _fingerprint(built.ai_path) == ai_before
    assert built.audit() == [
        (AdminAuditAction.CONFIG_CLEARED, AdminAuditOutcome.STARTED, None),
        (AdminAuditAction.CONFIG_CLEARED, AdminAuditOutcome.SUCCEEDED, None),
    ]


# --------------------------------------------------------------------------
# 干净部署与损坏文件：「不存在」是未配置，「存在但坏」是故障
# --------------------------------------------------------------------------


async def test_first_save_on_a_clean_deployment_creates_generation_1_per_domain(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        empty = await client.get("/admin/api/config/ai")
        assert empty.status_code == 200
        assert empty.json()["generation"] == 0
        assert empty.json()["checks"] == {"gemini_connection": "unconfigured"}
        saved = await client.put(
            "/admin/api/config/ai",
            content=json.dumps({"enabled": True, "api_key": _GEMINI_KEY}),
            headers=_json_headers(csrf),
        )
        feishu = await client.get("/admin/api/config/feishu")

    assert saved.json() == {"domain": "ai", "generation": 1, "restart_required": True}
    assert _saved(built.ai_path)["generation"] == 1
    assert feishu.json()["generation"] == 0
    assert not built.feishu_path.exists()


@pytest.mark.parametrize("shape", ["corrupt", "symlink"], ids=["corrupt", "symlink"])
async def test_a_corrupt_domain_file_is_not_treated_as_absent(
    tmp_path, clock, memory_state, shape: str
) -> None:
    """存在但读不了必须是故障码。当成「未配置」会让下一次保存覆盖掉一份仍在用的配置。"""
    built = _build(tmp_path, clock, memory_state)
    if shape == "corrupt":
        built.ai_path.write_text("{not json", encoding="utf-8")
        original = built.ai_path.read_bytes()
    else:
        target = tmp_path / "elsewhere.json"
        _write(target, {"generation": 1, "gemini": {"enabled": False}})
        built.ai_path.symlink_to(target)
        original = target.read_bytes()
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        read = await client.get("/admin/api/config/ai")
        written = await client.put(
            "/admin/api/config/ai",
            content=json.dumps({"enabled": True, "api_key": _GEMINI_KEY}),
            headers=_json_headers(csrf),
        )
        other = await client.get("/admin/api/config/feishu")

    assert read.status_code == 503
    assert read.json() == {"error": {"code": "unavailable"}}
    assert written.status_code == 503
    # 另一个域不受这一域损坏的影响。
    assert other.status_code == 200
    if shape == "corrupt":
        assert built.ai_path.read_bytes() == original
    else:
        assert built.ai_path.is_symlink()
        assert target.read_bytes() == original
    assert built.audit()[-1] == (
        AdminAuditAction.CONFIG_SAVED,
        AdminAuditOutcome.FAILED,
        AdminAuditReasonCode.CONFIG_INVALID,
    )


# --------------------------------------------------------------------------
# 授权：本地管理员之外一律 403；写与清除的拒绝先落审计
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


async def test_domain_routes_refuse_a_feishu_admin_and_audit_the_mutations(
    tmp_path, clock, memory_state
) -> None:
    """飞书 ADMIN 只能看脱敏状态：读 raw config、写与清除都是 403，文件一字节不动。"""
    from xiaowei_agent.interfaces.web_auth import web_csrf_token

    built = _build(tmp_path, clock, memory_state, with_feishu_auth=True)
    _seed_both(built)
    before = (_fingerprint(built.ai_path), _fingerprint(built.feishu_path))
    cookie = await _seed_feishu_session(built.sessions)

    async with _client(built.app) as client:
        client.cookies.set("__Host-xiaowei-session", cookie)
        # 先确认这条 session 本身是有效的——否则 403 可能只是"没登录"。
        assert (await client.get("/app/api/me")).status_code == 200
        headers = _json_headers(web_csrf_token(cookie))
        refused = [
            await client.get("/admin/api/config/ai"),
            await client.get("/admin/api/config/feishu"),
            await client.put(
                "/admin/api/config/ai", content=json.dumps({"enabled": False}), headers=headers
            ),
            await client.put(
                "/admin/api/config/feishu",
                content=json.dumps({"enabled": False}),
                headers=headers,
            ),
            await client.post(
                "/admin/api/config/ai/clear",
                content=json.dumps({"confirm": True}),
                headers=headers,
            ),
            await client.post(
                "/admin/api/config/feishu/clear",
                content=json.dumps({"confirm": True}),
                headers=headers,
            ),
        ]

    assert [response.status_code for response in refused] == [403] * 6
    assert all(response.json() == {"error": {"code": "forbidden"}} for response in refused)
    assert (_fingerprint(built.ai_path), _fingerprint(built.feishu_path)) == before
    denied = (AdminAuditOutcome.DENIED, AdminAuditReasonCode.AUTH_SOURCE_NOT_ALLOWED)
    assert sorted((a.value, o, r) for a, o, r in built.audit()) == [
        ("config_cleared", *denied),
        ("config_cleared", *denied),
        ("config_saved", *denied),
        ("config_saved", *denied),
    ]


async def test_domain_routes_refuse_an_anonymous_browser_without_audit(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    async with _client(built.app) as client:
        responses = [
            await client.get("/admin/api/config/ai"),
            await client.put(
                "/admin/api/config/feishu",
                content=json.dumps({"enabled": False}),
                headers=_json_headers("0" * 64),
            ),
            await client.post(
                "/admin/api/config/ai/clear",
                content=json.dumps({"confirm": True}),
                headers=_json_headers("0" * 64),
            ),
        ]

    assert [response.status_code for response in responses] == [401, 401, 401]
    # 未认证请求没有可信 actor：不为未知主体伪造审计。
    assert built.audit() == []


async def test_legacy_combined_and_workbench_config_routes_are_closed(
    tmp_path, clock, memory_state
) -> None:
    """W4a 不保留组合 API 的兼容双写，W2 迁走的 /app 路径也不回来。"""
    built = _build(tmp_path, clock, memory_state)
    _seed_both(built)
    before = (_fingerprint(built.ai_path), _fingerprint(built.feishu_path))

    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        headers = _json_headers(csrf)
        responses = [
            await client.get("/admin/api/config"),
            await client.put(
                "/admin/api/config",
                content=json.dumps({"gemini": {"enabled": False}}),
                headers=headers,
            ),
            await client.post(
                "/admin/api/config/clear",
                content=json.dumps({"provider": "gemini"}),
                headers=headers,
            ),
            await client.get("/admin/api/config/resources"),
            await client.put(
                "/admin/api/config/resources", content="{}", headers=headers
            ),
            await client.get("/app/api/config"),
            await client.put(
                "/app/api/config",
                content=json.dumps({"gemini": {"enabled": False}}),
                headers=headers,
            ),
            await client.post(
                "/app/api/config/clear",
                content=json.dumps({"provider": "gemini"}),
                headers=headers,
            ),
            await client.post(
                "/app/api/config/test/gemini_connection", content="{}", headers=headers
            ),
        ]

    assert all(response.status_code in {404, 405} for response in responses), [
        response.status_code for response in responses
    ]
    assert (_fingerprint(built.ai_path), _fingerprint(built.feishu_path)) == before
    assert built.audit() == []


async def test_config_writes_still_require_origin_and_csrf(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state)
    _seed_both(built)
    before = _fingerprint(built.ai_path)
    async with _client(built.app) as client:
        csrf = await _sign_in(client, built.admins)
        without_csrf = await client.put(
            "/admin/api/config/ai",
            content=json.dumps({"enabled": False}),
            headers=_json_headers(),
        )
        wrong_origin = await client.put(
            "/admin/api/config/ai",
            content=json.dumps({"enabled": False}),
            headers={
                "origin": "https://evil.example",
                "content-type": "application/json",
                "x-csrf-token": csrf,
            },
        )
        clear_without_csrf = await client.post(
            "/admin/api/config/ai/clear",
            content=json.dumps({"confirm": True}),
            headers=_json_headers(),
        )

    assert without_csrf.status_code == 403
    assert wrong_origin.status_code == 403
    assert clear_without_csrf.status_code == 403
    assert _fingerprint(built.ai_path) == before
    assert built.audit() == []


async def test_configured_and_the_page_state_answer_the_same_question(
    tmp_path, clock, memory_state
) -> None:
    """反例：飞书有两个必填字段，只看 secret 会把半份配置显示成"已配置"。"""
    built = _build(tmp_path, clock, memory_state, with_feishu_auth=True)
    _write(built.ai_path, {"generation": 1, "gemini": {"enabled": True}})
    _write(
        built.feishu_path,
        {"generation": 1, "feishu": {"enabled": True, "app_secret": _FEISHU_SECRET}},
    )

    async with _client(built.app) as client:
        await _sign_in(client, built.admins)
        feishu = (await client.get("/admin/api/config/feishu")).json()
        ai = (await client.get("/admin/api/config/ai")).json()

    assert feishu["feishu"]["configured"] is False
    assert feishu["feishu"]["app_id"] is None
    assert feishu["checks"] == {
        "feishu_credentials": "unconfigured",
        "feishu_oauth": "unconfigured",
    }
    # Gemini 侧同理：开了开关但没有 Key 不算已配置。
    assert ai["gemini"] == {"enabled": True, "configured": False}
    assert ai["checks"] == {"gemini_connection": "unconfigured"}


async def test_config_routes_refuse_before_the_forced_password_change(
    tmp_path, clock, memory_state
) -> None:
    """反例：``admin/admin`` 还没改掉时，配置面一个字节都不该动。"""
    built = _build(tmp_path, clock, memory_state)
    await built.admins.seed_if_absent(
        password_hash=hash_password(INITIAL_LOCAL_ADMIN_PASSWORD)
    )
    async with _client(built.app) as client:
        await client.post(
            "/login/api/login",
            content=json.dumps(
                {
                    "username": "admin",
                    "password": INITIAL_LOCAL_ADMIN_PASSWORD,
                    "return_intent": {"kind": "workbench"},
                }
            ),
            headers=_json_headers(),
        )
        carried = _CSRF_META_RE.search(
            (await client.get("/login?intent=workbench")).text
        )
        assert carried is not None
        token = carried.group(1)

        responses = [
            await client.get("/admin/api/config/ai"),
            await client.put(
                "/admin/api/config/ai",
                content=json.dumps({"enabled": True, "api_key": _GEMINI_KEY}),
                headers=_json_headers(token),
            ),
            await client.post(
                "/admin/api/config/feishu/clear",
                content=json.dumps({"confirm": True}),
                headers=_json_headers(token),
            ),
        ]

    for response in responses:
        assert response.status_code == 403
        assert response.json() == {"error": {"code": "password_change_required"}}
    assert not built.ai_path.exists() and not built.feishu_path.exists()
    snapshot = await built.provider_state.snapshot()
    assert snapshot.receipts == {}
    assert snapshot.tests == {}
    assert built.audit() == []
