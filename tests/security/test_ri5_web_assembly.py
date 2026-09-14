"""RI5 Web 装配：飞书是插件，本地管理员是必备入口。

这里守的是**职责分界**与**降级方向**：装配函数只收端口不读配置；飞书不可用是
降级，本地管理员不可用是起不来。
"""

import functools
import json
from pathlib import Path
from typing import Any

import pytest

from xiaowei_agent.application.channel_access import TaskAccessService
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import WebMode
from xiaowei_agent.interfaces import local_stack as local_stack_module
from xiaowei_agent.interfaces import provider_consumption as provider_consumption_module
from xiaowei_agent.interfaces import web_app as web_app_module
from xiaowei_agent.interfaces.local_admin_auth import LocalAdminAuthService
from xiaowei_agent.interfaces.local_stack import build_postgres_web_stack
from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials
from xiaowei_agent.interfaces.web_app import (
    oauth_state_cookie_name,
    session_cookie_name,
)
from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity

pytestmark = pytest.mark.security

_FAKE_APP_SECRET = "unit-test-" + "app-secret"


def _fixed_credentials(
    credentials: ProviderCredentials, **_: object
) -> tuple[ProviderCredentials, dict[object, object]]:
    return credentials, {}


class _Connection:
    async def execute(self, *_: object, **__: object) -> object:
        return None

    async def scalar(self, *_: object, **__: object) -> object:
        return None


class _Engine:
    """支持 seed 写入的假 engine；本地管理员装配会真的写一次库。"""

    disposed = False

    def begin(self) -> Any:
        class _Transaction:
            async def __aenter__(self) -> _Connection:
                return _Connection()

            async def __aexit__(self, *_: object) -> bool:
                return False

        return _Transaction()

    connect = begin

    async def dispose(self) -> None:
        self.disposed = True


class _OAuth:
    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> FeishuOAuthIdentity:
        return FeishuOAuthIdentity(subject_ref="user-open-id")


class _Membership:
    async def is_current_group_member(
        self, *, tenant_id: str, conversation_ref: str, subject_ref: str
    ) -> bool:
        return True


def _identity_file(tmp_path: Path) -> Path:
    path = tmp_path / "identities.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": "dev-local",
                "environment_id": "dev",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _settings(tmp_path: Path, **updates: object) -> Settings:
    values: dict[str, object] = {
        "environment_id": "dev",
        "web_app_enabled": True,
        "feishu_oauth_enabled": True,
        "feishu_identity_file": str(_identity_file(tmp_path)),
        "web_public_origin": "https://ops.example.test",
    }
    values.update(updates)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _Engine:
    instance = _Engine()
    monkeypatch.setattr(
        local_stack_module, "create_database_engine", lambda _: instance
    )
    return instance


async def test_web_starts_without_any_feishu_configuration(
    tmp_path: Path, engine: _Engine
) -> None:
    stack = await build_postgres_web_stack(
        settings=_settings(tmp_path), oauth=None, membership=None
    )
    try:
        assert stack.oauth_available is False
        assert stack.auth is None
        assert stack.oauth_port is None
        assert stack.membership is None
        assert stack.identity_directory is None
        assert isinstance(stack.local_admin_auth, LocalAdminAuthService)
    finally:
        await stack.aclose()


async def test_the_stack_builder_never_reads_the_integration_config(
    tmp_path: Path, engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """职责分界的承重断言：装配函数只收端口，不读文件。

    把读配置挪回装配函数的实现会在这里变红。
    """
    calls: list[str] = []
    monkeypatch.setattr(
        provider_consumption_module,
        "load_provider_credentials",
        lambda **_: calls.append("load") or (ProviderCredentials(), {}),
    )
    monkeypatch.setattr(
        provider_consumption_module,
        "read_or_absent",
        lambda _: calls.append("read") or None,
    )

    stack = await build_postgres_web_stack(
        settings=_settings(tmp_path), oauth=None, membership=None
    )
    await stack.aclose()

    assert calls == []


@pytest.mark.parametrize(
    ("oauth", "membership"),
    [(_OAuth(), None), (None, _Membership())],
    ids=["oauth-without-membership", "membership-without-oauth"],
)
async def test_half_a_port_pair_does_not_assemble_feishu(
    tmp_path: Path, engine: _Engine, oauth: object, membership: object
) -> None:
    """半套端口不得走进「已装配」分支——否则会在第一次真实调用时才炸。"""
    stack = await build_postgres_web_stack(
        settings=_settings(tmp_path),
        oauth=oauth,  # type: ignore[arg-type]
        membership=membership,  # type: ignore[arg-type]
    )
    try:
        assert stack.oauth_available is False
        assert stack.auth is None
    finally:
        await stack.aclose()


async def test_feishu_oauth_assembly_failure_does_not_kill_the_process(
    tmp_path: Path, engine: _Engine
) -> None:
    stack = await build_postgres_web_stack(
        settings=_settings(
            tmp_path, feishu_identity_file=str(tmp_path / "missing.json")
        ),
        oauth=_OAuth(),
        membership=_Membership(),
    )
    try:
        assert stack.oauth_available is False
        assert isinstance(stack.local_admin_auth, LocalAdminAuthService)
        assert engine.disposed is False
    finally:
        await stack.aclose()


async def test_bad_origin_does_not_take_down_the_whole_web(
    tmp_path: Path, engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个端口都在，但 WebAuthService 构造抛 ValueError。

    捕获集合漏掉 ValueError 的实现会在这里变红：一个 TTL/origin 的问题不该
    把整个 Web 打挂。
    """

    def explode(**_: object) -> object:
        raise ValueError("constant web auth construction failure")

    monkeypatch.setattr(local_stack_module, "create_database_engine", lambda _: engine)
    import xiaowei_agent.interfaces.web_auth as web_auth_module

    monkeypatch.setattr(web_auth_module, "WebAuthService", explode)

    stack = await build_postgres_web_stack(
        settings=_settings(tmp_path), oauth=_OAuth(), membership=_Membership()
    )
    try:
        assert stack.oauth_available is False
        assert stack.auth is None
        assert engine.disposed is False
    finally:
        await stack.aclose()


async def test_gemini_never_participates_in_web_readiness(
    tmp_path: Path, engine: _Engine
) -> None:
    """readyz 只看 database / migration head / assembled。"""
    stack = await build_postgres_web_stack(
        settings=_settings(tmp_path, gemini_enabled=True), oauth=None, membership=None
    )
    try:
        assert set(type(stack.readiness).__mro__[0].__init__.__code__.co_varnames) >= {
            "engine",
            "assembled",
        }
        assert not hasattr(stack.readiness, "_gemini")
    finally:
        await stack.aclose()


async def test_membership_is_required_by_the_access_service_signature() -> None:
    """`membership=None` 必须是**类型上**可表达的，否则装配层只能塞一个假对象。"""
    import inspect

    annotation = inspect.signature(TaskAccessService.__init__).parameters["membership"]
    assert "None" in str(annotation.annotation)


def test_cookie_names_and_flags_follow_the_web_mode() -> None:
    assert session_cookie_name(WebMode.HTTPS) == "__Host-xiaowei-session"
    assert session_cookie_name(WebMode.LAN_HTTP) == "xiaowei-session"
    assert oauth_state_cookie_name(WebMode.HTTPS) == "__Host-xiaowei-oauth-state"
    assert oauth_state_cookie_name(WebMode.LAN_HTTP) == "xiaowei-oauth-state"
    # `__Host-` 前缀要求 Secure；lan_http 下带前缀的 cookie 会被浏览器整个丢掉，
    # 那不是"降级为不安全"，是登录直接不工作。
    assert not session_cookie_name(WebMode.LAN_HTTP).startswith("__Host-")


def test_serve_web_builds_no_feishu_adapter_when_the_switch_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关关闭时，即使 JSON 里飞书字段齐备也不得构造 adapter。"""
    constructed: list[str] = []

    import xiaowei_agent.interfaces.feishu_oauth as feishu_oauth_module
    import xiaowei_agent.interfaces.feishu_sdk as feishu_sdk_module

    monkeypatch.setattr(
        feishu_oauth_module,
        "FeishuOAuthAdapter",
        lambda **_: constructed.append("oauth"),
    )
    monkeypatch.setattr(
        feishu_sdk_module,
        "FeishuSdkMembershipAdapter",
        lambda **_: constructed.append("membership"),
    )
    monkeypatch.setattr(
        provider_consumption_module,
        "load_provider_credentials",
        lambda **_: (
            ProviderCredentials(
                feishu_app_id="cli_x", feishu_app_secret=_FAKE_APP_SECRET
            ),
            {},
        ),
    )
    received: dict[str, object] = {}

    async def build(**kwargs: object) -> object:
        received.update(kwargs)
        raise local_stack_module.WebStackConfigurationError("stop after assembly")

    monkeypatch.setattr(local_stack_module, "build_postgres_web_stack", build)
    monkeypatch.setattr(web_app_module, "configure_logging", lambda _: None)

    settings = _settings(tmp_path, feishu_oauth_enabled=False)
    with pytest.raises(web_app_module._WebConfigurationError):
        import asyncio

        asyncio.run(web_app_module.serve_web(settings))

    assert constructed == []
    assert received["oauth"] is None
    assert received["membership"] is None


def test_serve_web_survives_missing_feishu_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """开关开着但凭据缺失：不抛配置错误，只是不装配飞书。"""
    received: dict[str, object] = {}

    async def build(**kwargs: object) -> object:
        received.update(kwargs)
        raise local_stack_module.WebStackConfigurationError("stop after assembly")

    monkeypatch.setattr(local_stack_module, "build_postgres_web_stack", build)
    monkeypatch.setattr(web_app_module, "configure_logging", lambda _: None)
    for credentials in (
        ProviderCredentials(feishu_app_id="cli_x", feishu_app_secret=None),
        ProviderCredentials(feishu_app_id=None, feishu_app_secret=_FAKE_APP_SECRET),
        ProviderCredentials(),
    ):
        received.clear()
        monkeypatch.setattr(
            provider_consumption_module,
            "load_provider_credentials",
            functools.partial(_fixed_credentials, credentials),
        )
        with pytest.raises(web_app_module._WebConfigurationError):
            import asyncio

            asyncio.run(web_app_module.serve_web(_settings(tmp_path)))
        assert received["oauth"] is None
        assert received["membership"] is None


# --------------------------------------------------------------------------
# 路由层：首次登录必须能在没有任何 cookie 的情况下完成
# --------------------------------------------------------------------------


def _app(clock: Any, memory_state: Any, *, mode: WebMode = WebMode.HTTPS) -> Any:
    from xiaowei_agent.interfaces.local_admin_auth import (
        INITIAL_LOCAL_ADMIN_PASSWORD,
        LocalAdminAuthService,
        hash_password,
    )
    from xiaowei_agent.interfaces.web_app import create_app
    from xiaowei_agent.interfaces.web_auth import web_origin_digest
    from xiaowei_agent.persistence.fake import (
        InMemoryLocalAdminStore,
        InMemoryWebSessionStore,
    )

    origin = (
        "https://ops.example.test"
        if mode is WebMode.HTTPS
        else "http://127.0.0.1:8080"
    )
    admins = InMemoryLocalAdminStore(clock=clock, state=memory_state)
    sessions = InMemoryWebSessionStore(clock=clock, state=memory_state)
    local_admin_auth = LocalAdminAuthService(
        admins=admins,
        sessions=sessions,
        public_origin_digest=web_origin_digest(origin),
        session_ttl_seconds=3600,
    )

    class _Probe:
        async def check(self) -> Any:  # pragma: no cover - 这些用例不打 readyz
            raise AssertionError("not used")

    class _Unused:
        def __getattr__(self, name: str) -> Any:  # pragma: no cover
            raise AssertionError(name)

    app = create_app(
        auth=None,
        local_admin_auth=local_admin_auth,
        oauth_available=False,
        settings=Settings(
            environment_id="dev",
            web_app_enabled=True,
            web_mode=mode,
            web_public_origin=origin,
        ),
        readiness=_Probe(),
        task_access=_Unused(),
        submissions=_Unused(),
        clock=clock,
        policy_revision="policy-2026-09-01",
    )
    return app, admins, origin, INITIAL_LOCAL_ADMIN_PASSWORD, hash_password


def _client(app: Any) -> Any:
    import httpx

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ops.example.test"
    )


async def _seed(admins: Any, hash_password: Any, password: str) -> None:
    await admins.seed_if_absent(password_hash=hash_password(password))


async def test_first_login_succeeds_without_any_csrf_token(clock, memory_state) -> None:
    """首启闭环的入口：没有 session 就没有 CSRF token，登录不能要求它。"""
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)

    async with _client(app) as client:
        response = await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial}),
            headers={"origin": origin, "content-type": "application/json"},
        )

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any(header.startswith("__Host-xiaowei-session=") for header in cookies)


async def test_login_still_requires_the_exact_origin_and_json_content_type(
    clock, memory_state
) -> None:
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)

    async with _client(app) as client:
        for headers, expected in (
            # Origin 不符 / 缺失 —— 由登录自己的窄校验拒绝。
            (
                {"origin": "http://evil.example", "content-type": "application/json"},
                403,
            ),
            ({"content-type": "application/json"}, 403),
            # 非 JSON —— body limit 中间件在路由分发之前就挡下，415 比 403 更早也更准。
            ({"origin": origin, "content-type": "text/plain"}, 415),
        ):
            response = await client.post(
                "/app/api/login",
                content=json.dumps({"password": initial}),
                headers=headers,
            )
            assert response.status_code == expected, headers
            assert response.headers.get_list("set-cookie") == []


async def test_a_wrong_password_never_sets_a_cookie(clock, memory_state) -> None:
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)

    async with _client(app) as client:
        response = await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial + "x"}),
            headers={"origin": origin, "content-type": "application/json"},
        )

    assert response.status_code == 401
    assert response.headers.get_list("set-cookie") == []


async def test_before_first_change_password_other_routes_are_refused(
    clock, memory_state
) -> None:
    """初始口令是源码常量，因此"已登录"在改密前不等于"可以做事"。"""
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)

    async with _client(app) as client:
        await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial}),
            headers={"origin": origin, "content-type": "application/json"},
        )
        blocked = await client.get("/app/api/me")
        shell = await client.get("/app")

    assert blocked.status_code == 403
    assert blocked.json() == {"error": {"code": "password_change_required"}}
    assert shell.status_code == 200
    assert "change-password-form" in shell.text


async def test_change_password_still_requires_session_csrf(
    clock, memory_state
) -> None:
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)
    new_password = "rotated-local-" + "admin-secret"

    async with _client(app) as client:
        await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial}),
            headers={"origin": origin, "content-type": "application/json"},
        )
        response = await client.post(
            "/app/api/change-password",
            content=json.dumps(
                {"current_password": initial, "new_password": new_password}
            ),
            headers={"origin": origin, "content-type": "application/json"},
        )

    assert response.status_code == 403
    assert (await admins.get()).must_change_password is True


async def test_the_full_first_run_loop_works(clock, memory_state) -> None:
    """登录 -> 改密 -> 业务接口放行：整条首启闭环。"""
    from xiaowei_agent.interfaces.web_auth import web_csrf_token

    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)
    new_password = "rotated-local-" + "admin-secret"

    async with _client(app) as client:
        await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial}),
            headers={"origin": origin, "content-type": "application/json"},
        )
        cookie = client.cookies.get("__Host-xiaowei-session")
        assert cookie is not None
        changed = await client.post(
            "/app/api/change-password",
            content=json.dumps(
                {"current_password": initial, "new_password": new_password}
            ),
            headers={
                "origin": origin,
                "content-type": "application/json",
                "x-csrf-token": web_csrf_token(cookie),
            },
        )
        assert changed.status_code == 200
        assert (await admins.get()).must_change_password is False
        # 改密后旧 cookie 已被撤销，新 cookie 由响应写回；业务接口不再被闸门拦下。
        me = await client.get("/app/api/me")

    assert me.status_code != 403


async def test_lan_http_mode_drops_the_host_prefix_and_hsts(
    clock, memory_state
) -> None:
    app, admins, origin, initial, hash_password = _app(
        clock, memory_state, mode=WebMode.LAN_HTTP
    )
    await _seed(admins, hash_password, initial)

    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=origin
    ) as client:
        response = await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial}),
            headers={"origin": origin, "content-type": "application/json"},
        )

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any(header.startswith("xiaowei-session=") for header in cookies)
    assert not any("__Host-" in header for header in cookies)
    assert not any("Secure" in header for header in cookies)
    # lan_http 下发 HSTS 会让浏览器把这个局域网地址记成"只许 HTTPS"，
    # 之后连管理面自己都打不开，且 max-age 内无法撤销。
    assert "strict-transport-security" not in response.headers


async def test_every_new_json_write_route_enforces_the_body_limit(
    clock, memory_state
) -> None:
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)
    oversize = json.dumps({"password": "x" * 200_000})

    async with _client(app) as client:
        for path in (
            "/app/api/login",
            "/app/api/change-password",
            "/app/api/config",
            "/app/api/config/clear",
            "/app/api/config/test/gemini_connection",
            "/app/api/config/test/feishu_credentials",
            "/app/api/config/test/feishu_oauth",
        ):
            response = await client.post(
                path,
                content=oversize,
                headers={"origin": origin, "content-type": "application/json"},
            )
            # 中间件必须在路由分发之前挡下：尚不存在的 config 路由也不能返回 404。
            assert response.status_code == 413, path
