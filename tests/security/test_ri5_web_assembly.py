"""RI5 Web 装配：飞书是插件，本地管理员是必备入口。

这里守的是**职责分界**与**降级方向**：装配函数只收端口不读配置；飞书不可用是
降级，本地管理员不可用是起不来。
"""

import functools
import json
import re
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


class _Result:
    """``execute()`` 的结果替身：读什么都读不到行。"""

    def mappings(self) -> "_Result":
        return self

    def first(self) -> object:
        return None

    def all(self) -> list[object]:
        return []


class _Connection:
    """连接替身。

    W1a 之后，本地管理员装配是一次**完整的 bootstrap**：凭据行、目录账号、
    ADMIN 角色、凭据链接与一条审计事件在同一个事务里落库。因此读（``execute``）
    要返回空——目录里还没有这个账号；写（``scalar``）要返回非空——
    ``ON CONFLICT ... RETURNING`` 拿到了行，即"确实插进去了"。两者返回同一个值
    时，装配会被判成一次主键冲突。
    """

    async def execute(self, *_: object, **__: object) -> object:
        return _Result()

    async def scalar(self, *_: object, **__: object) -> object:
        return "inserted"


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
        InMemoryProviderStateStore,
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
        provider_state=InMemoryProviderStateStore(clock=clock, state=memory_state),
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
        for method, path in (
            ("POST", "/app/api/login"),
            ("POST", "/app/api/change-password"),
            # 配置保存是 PUT。中间件只认 POST 时这一条会整条绕过 body 上限，
            # 而它恰好是唯一一个会被原样写进磁盘文件的入口。
            ("PUT", "/app/api/config"),
            ("POST", "/app/api/config/clear"),
            ("POST", "/app/api/config/test/gemini_connection"),
            ("POST", "/app/api/config/test/feishu_credentials"),
            ("POST", "/app/api/config/test/feishu_oauth"),
        ):
            response = await client.request(
                method,
                path,
                content=oversize,
                headers={"origin": origin, "content-type": "application/json"},
            )
            # 中间件必须在路由分发之前挡下：尚不存在的探针路由也不能返回 404。
            assert response.status_code == 413, path


# --------------------------------------------------------------------------
# 真实浏览器首启闭环：读不到 HttpOnly cookie，也没有第二台设备
# --------------------------------------------------------------------------

_ASSET_RE = re.compile(r'(?:src|href)="(/app/static/[^"]+)"')
_CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]*)">')


async def test_every_asset_the_server_rendered_shells_reference_is_served(
    clock, memory_state
) -> None:
    """壳引用的每个静态资源都必须真的能取到。

    登录壳与改密壳是服务端拼出来的字符串，不在 ``web_static`` 的既有守卫覆盖内；
    静态路由又是一条一条手写的，因此"加一句 ``<script src>``"不会自动带来一条路由。
    这里把"壳里引用的 URL"和"实际能取到的 URL"直接绑在一起。
    """
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)

    async with _client(app) as client:
        pages = [(await client.get("/app")).text]
        await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial}),
            headers={"origin": origin, "content-type": "application/json"},
        )
        pages.append((await client.get("/app")).text)
        referenced = sorted({url for page in pages for url in _ASSET_RE.findall(page)})
        # 反向断言：守卫本身不能因为"壳其实什么都没引用"而空转。
        assert "/app/static/login.js" in referenced
        assert "/app/static/app.css" in referenced
        for url in referenced:
            asset = await client.get(url)
            assert asset.status_code == 200, url
            assert asset.content, url


async def test_a_browser_completes_the_first_run_without_reading_the_cookie(
    clock, memory_state
) -> None:
    """整条首启闭环，全程只用浏览器拿得到的东西。

    session cookie 是 ``HttpOnly``，页面脚本读不到它，因此也**派生不出** CSRF
    token。强制改密又要求 CSRF token——token 的唯一出口 ``/app/api/me`` 却正好被
    改密闸门挡在门外。这里不调用 ``web_csrf_token()``：一旦用它算 token，测试就
    绕过了真实浏览器唯一的取值渠道，绿灯是假的。
    """
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)
    new_password = "rotated-local-" + "admin-secret"

    async with _client(app) as client:
        login_page = await client.get("/app")
        assert "login-form" in login_page.text
        # 未登录时没有 session，就没有可绑定的 token；此刻交出任何 token 都是错的。
        assert _CSRF_META_RE.search(login_page.text) is None
        assert (await client.get("/app/static/login.js")).status_code == 200

        signed_in = await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial}),
            headers={"origin": origin, "content-type": "application/json"},
        )
        assert signed_in.status_code == 200

        change_page = await client.get("/app")
        assert "change-password-form" in change_page.text
        carried = _CSRF_META_RE.search(change_page.text)
        assert carried is not None, "改密页必须自带 CSRF token，否则闭环走不下去"

        changed = await client.post(
            "/app/api/change-password",
            content=json.dumps(
                {"current_password": initial, "new_password": new_password}
            ),
            headers={
                "origin": origin,
                "content-type": "application/json",
                "x-csrf-token": carried.group(1),
            },
        )
        assert changed.status_code == 200
        assert (await admins.get()).must_change_password is False
        assert "workbench-shell" in (await client.get("/app")).text


async def test_a_token_from_someone_elses_page_is_still_refused(
    clock, memory_state
) -> None:
    """反例：页面交付 token 不等于 token 不再绑定 session。"""
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)
    new_password = "rotated-local-" + "admin-secret"

    async with _client(app) as client:
        await client.post(
            "/app/api/login",
            content=json.dumps({"password": initial}),
            headers={"origin": origin, "content-type": "application/json"},
        )
        stolen = _CSRF_META_RE.search((await client.get("/app")).text)
        assert stolen is not None
        forged = "f" * len(stolen.group(1))
        assert forged != stolen.group(1)
        refused = await client.post(
            "/app/api/change-password",
            content=json.dumps(
                {"current_password": initial, "new_password": new_password}
            ),
            headers={
                "origin": origin,
                "content-type": "application/json",
                "x-csrf-token": forged,
            },
        )

    assert refused.status_code == 403
    assert (await admins.get()).must_change_password is True


def test_an_unrenderable_csrf_token_is_refused_instead_of_interpolated() -> None:
    """反例：交给页面的值必须先确认形态，不能直接拼进 HTML。

    现在的 token 是 sha256 十六进制，天然安全；这条守卫是为了让"以后换了 token
    形态"变成一个立刻可见的错误，而不是一个安静的注入点。
    """
    from xiaowei_agent.interfaces.web_app import _password_change_shell

    for forged in (
        '"><script>alert(1)</script>',
        "not-hex-" + "0" * 56,
        "0" * 63,
        "0" * 65,
        "",
    ):
        with pytest.raises(RuntimeError):
            _password_change_shell(csrf_token=forged)

    rendered = _password_change_shell(csrf_token="a" * 64)
    assert '<meta name="csrf-token" content="' + "a" * 64 + '">' in rendered
