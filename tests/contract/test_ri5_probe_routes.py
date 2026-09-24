"""三个测试项的路由行为。

探针是**控制面**：它证明凭据可用，不产生任何业务事实。这组用例的骨架就是把这句
话拆成可证伪的几条——不写任务、不写 Evidence、不碰 ToolGateway、不动 readiness、
不签发会话——再加上两个 state 域必须互不相认。W4a 起每次测试都经唯一配置写服务留下
两阶段审计，OAuth 测试的 operation id 随 state 跨回调。
"""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.fakes.activation import RecordingActivationRequests
from tests.fakes.admin_identity import UnusedAdminIdentity

from xiaowei_agent.application.integration_config_service import (
    IntegrationConfigService,
)
from xiaowei_agent.application.integration_state import SERVICE_WEB
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
    ReadinessReport,
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
from xiaowei_agent.interfaces.provider_probe import ProbeErrorCode
from xiaowei_agent.interfaces.web_app import create_app
from xiaowei_agent.interfaces.web_auth import (
    OAUTH_LOGIN_STATE_DOMAIN,
    OAUTH_TEST_STATE_DOMAIN,
    FeishuOAuthIdentity,
    WebAuthService,
    web_origin_digest,
)
from xiaowei_agent.persistence.fake import (
    InMemoryAdminAuditStore,
    InMemoryLocalAdminStore,
    InMemoryProviderStateStore,
    InMemoryWebSessionStore,
)

_ORIGIN = "https://ops.example.test"
_CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]*)">')
_NEW_PASSWORD = "rotated-local-" + "admin-secret"
_GEMINI_KEY = "gemini-probe-" + "test-key"
_FEISHU_SECRET = "feishu-probe-" + "test-secret"


class _Readiness:
    """始终就绪的探针，并记录被问了几次。"""

    def __init__(self) -> None:
        self.checks = 0

    async def check(self) -> ReadinessReport:
        self.checks += 1
        return ReadinessReport(
            database_ok=True, revision_matches_head=True, assembled=True
        )


class _CountingTaskAccess:
    """任何一次调用都会被计入；探针不该碰到它。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def record(*_: object, **__: object) -> Any:
            self.calls.append(name)
            raise AssertionError(name)

        return record


class _Spy:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result
        self._error = error

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


class _OAuth:
    """记录 code 交换次数的 OAuth 端口替身。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.exchanges: list[str] = []
        self._error = error

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        return f"https://feishu.example.test/authorize?state={state}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> Any:
        self.exchanges.append(code)
        if self._error is not None:
            raise self._error
        return FeishuOAuthIdentity(subject_ref="subject-alice")


def _admin_principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        tenant_id="dev-local",
        environment_id="dev",
        actor="alice",
        source=IdentitySource.FEISHU,
        subject_ref="subject-alice",
        permissions=frozenset({ChannelPermission.ADMIN_ALL_SAFE_TASKS}),
    )


def _build(
    tmp_path: Path,
    clock: Any,
    memory_state: Any,
    *,
    gemini_probe: Any = None,
    feishu_probe: Any = None,
    oauth: _OAuth | None = None,
    with_feishu_auth: bool = True,
    **settings_updates: object,
) -> Any:
    root = tmp_path / ".config"
    for domain in ("ai", "feishu", "resources"):
        (root / domain).mkdir(parents=True, exist_ok=True)
    ai_path = root / "ai" / "config.json"
    feishu_path = root / "feishu" / "config.json"
    sessions = InMemoryWebSessionStore(clock=clock, state=memory_state)
    admins = InMemoryLocalAdminStore(clock=clock, state=memory_state)
    provider_state = InMemoryProviderStateStore(clock=clock, state=memory_state)
    readiness = _Readiness()
    task_access = _CountingTaskAccess()
    submissions = _CountingTaskAccess()
    values: dict[str, object] = {
        "environment_id": "dev",
        "web_app_enabled": True,
        "web_mode": WebMode.HTTPS,
        "web_public_origin": _ORIGIN,
    }
    if with_feishu_auth:
        values["feishu_oauth_enabled"] = True
    values.update(settings_updates)
    settings = Settings(**values)  # type: ignore[arg-type]
    auth = (
        WebAuthService(
            sessions=sessions,
            identities=StaticFeishuIdentityDirectory(
                principals={"subject-alice": _admin_principal()},
                web_roles={"subject-alice": ProductRole.ADMIN},
                web_user_ids={"subject-alice": "user-alice"},
            ),
            activations=RecordingActivationRequests().as_service(),
            oauth=oauth if oauth is not None else _OAuth(),
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
        readiness=readiness,
        task_access=task_access,
        submissions=submissions,
        clock=clock,
        policy_revision="policy-2026-09-01",
        provider_state=provider_state,
        integration_config=IntegrationConfigService(
            repository=FileIntegrationConfigRepository(
                ai_path=str(ai_path), feishu_path=str(feishu_path)
            ),
            audit=InMemoryAdminAuditStore(clock=clock, state=memory_state),
            provider_state=provider_state,
        ),
        gemini_probe=gemini_probe if gemini_probe is not None else _Spy(),
        feishu_probe=feishu_probe if feishu_probe is not None else _Spy(),
        admin_identity=UnusedAdminIdentity(),
    )
    return {
        "app": app,
        "admins": admins,
        "sessions": sessions,
        "provider_state": provider_state,
        "ai_path": ai_path,
        "feishu_path": feishu_path,
        "readiness": readiness,
        "task_access": task_access,
        "submissions": submissions,
    }


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_ORIGIN)


def _json_headers(csrf_token: str | None = None) -> dict[str, str]:
    headers = {"origin": _ORIGIN, "content-type": "application/json"}
    if csrf_token is not None:
        headers["x-csrf-token"] = csrf_token
    return headers


async def _sign_in(client: httpx.AsyncClient, admins: Any) -> str:
    await admins.seed_if_absent(
        password_hash=hash_password(INITIAL_LOCAL_ADMIN_PASSWORD)
    )
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
    return str(me.json()["csrf_token"])


def _write_config(
    built: dict[str, Any], *, generation: int = 4, ai_generation: int | None = None
) -> None:
    """两个域各写一份；不指定时两域同代，便于与单文件时代的用例对照。"""
    built["ai_path"].write_text(
        json.dumps(
            {
                "generation": generation if ai_generation is None else ai_generation,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
            }
        ),
        encoding="utf-8",
    )
    built["feishu_path"].write_text(
        json.dumps(
            {
                "generation": generation,
                "feishu": {
                    "enabled": True,
                    "app_id": "cli_probe",
                    "app_secret": _FEISHU_SECRET,
                },
            }
        ),
        encoding="utf-8",
    )


_CONFIG_ACTIONS = frozenset(
    {
        AdminAuditAction.CONFIG_SAVED,
        AdminAuditAction.CONFIG_CLEARED,
        AdminAuditAction.CONNECTION_TESTED,
    }
)


def _audit(memory_state: Any) -> list[tuple[str, AdminAuditOutcome, Any]]:
    events = sorted(
        (
            event
            for event in memory_state.admin_audit_events.values()
            if event.action in _CONFIG_ACTIONS
        ),
        key=lambda event: (
            event.created_at,
            event.operation_id,
            event.outcome is not AdminAuditOutcome.STARTED,
        ),
    )
    return [(e.operation_id, e.outcome, e.reason_code) for e in events]


# --------------------------------------------------------------------------
# 探针不是能力
# --------------------------------------------------------------------------


async def test_probe_never_creates_a_task_submission_or_evidence(
    tmp_path, clock, memory_state
) -> None:
    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_probe=_Spy(result={"ok": True}),
        feishu_probe=_Spy(result={"ok": True}),
        gemini_real_test_enabled=True,
        feishu_real_test_enabled=True,
    )
    _write_config(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        for name in ("gemini_connection", "feishu_credentials", "feishu_oauth"):
            response = await client.post(
                f"/admin/api/config/test/{name}", headers=_json_headers(token)
            )
            assert response.status_code == 200

    assert built["task_access"].calls == []
    assert built["submissions"].calls == []
    # 任务与 Evidence 都写在同一个内存事实命名空间里；一条都不该多出来。
    assert memory_state.tasks == {}
    assert memory_state.submissions == {}
    assert memory_state.evidence == {}
    assert memory_state.audit_events == {}


def _import_closure(root: str) -> set[str]:
    """从源码静态展开 ``root`` 可达的本仓库模块名。

    **不看 ``sys.modules``**：同一个 pytest 进程里别的用例早就把半个包导进来了，
    那种断言只会随执行顺序随机变红或随机变绿——比没有断言更糟。
    """
    import ast
    import importlib.util
    import pathlib

    seen: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if name in seen or not name.startswith("xiaowei_agent"):
            continue
        seen.add(name)
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ModuleNotFoundError):
            # ``from x import Name`` 里的 Name 多半是个符号而不是子模块。
            continue
        if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
            continue
        tree = ast.parse(pathlib.Path(spec.origin).read_text(encoding="utf-8"))
        # 只看**模块级**导入：函数体里的延迟导入是"用到才加载"，正是 serve_web
        # 装配真实栈时才会走的那条路，不属于"import 这个模块就会被拉进来"。
        for node in tree.body:
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                pending.append(node.module)
                pending.extend(
                    f"{node.module}.{alias.name}" for alias in node.names
                )
    return seen


async def test_probe_never_reaches_the_tool_gateway(
    tmp_path, clock, memory_state
) -> None:
    """探针这条路径够不着 Gateway；"够不着"是结构事实，不是约定。

    断言落在 ``provider_probe`` 的导入闭包上而不是整个 ``web_app`` 上：Web 的任务
    投影本来就要读能力规格，把整条闭包一起禁掉会得到一条**假的**断言，红了也只能
    靠放宽白名单收场。真正要守住的是"测试连接"这条路——它一旦能拿到 Gateway，
    "验证凭据"就成了"以管理员身份跑一次真实调用"。
    """
    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_probe=_Spy(result={"ok": True}),
        gemini_real_test_enabled=True,
    )
    _write_config(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/gemini_connection", headers=_json_headers(token)
        )
    assert response.status_code == 200

    assert "xiaowei_agent.interfaces.provider_probe" in _import_closure(
        "xiaowei_agent.interfaces.web_app"
    )
    reachable = _import_closure("xiaowei_agent.interfaces.provider_probe")
    gateway_packages = {"runners", "tools", "capabilities"}
    assert not {
        name for name in reachable if name.split(".")[1] in gateway_packages
    }
    assert "xiaowei_agent.application.capability_runtime" not in reachable


async def test_probe_does_not_change_readiness(tmp_path, clock, memory_state) -> None:
    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_probe=_Spy(error=RuntimeError("provider down")),
        gemini_real_test_enabled=True,
    )
    _write_config(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        assert (await client.get("/readyz")).status_code == 200
        failed = await client.post(
            "/admin/api/config/test/gemini_connection", headers=_json_headers(token)
        )
        assert failed.json()["status"] == "failed"
        assert (await client.get("/readyz")).status_code == 200


# --------------------------------------------------------------------------
# 授权边界
# --------------------------------------------------------------------------


async def test_probe_routes_require_a_local_admin_and_a_csrf_token(
    tmp_path, clock, memory_state
) -> None:
    spy = _Spy(result={"ok": True})
    built = _build(
        tmp_path, clock, memory_state, gemini_probe=spy, gemini_real_test_enabled=True
    )
    _write_config(built)
    async with _client(built["app"]) as client:
        anonymous = await client.post(
            "/admin/api/config/test/gemini_connection", headers=_json_headers("x" * 64)
        )
        assert anonymous.status_code == 401
        token = await _sign_in(client, built["admins"])
        without_csrf = await client.post(
            "/admin/api/config/test/gemini_connection", headers=_json_headers()
        )
        assert without_csrf.status_code == 403
        foreign_origin = await client.post(
            "/admin/api/config/test/gemini_connection",
            headers={**_json_headers(token), "origin": "https://evil.example.test"},
        )
        assert foreign_origin.status_code == 403
    # 三次拒绝都发生在探针之前。
    assert spy.calls == []


async def test_an_unknown_check_name_is_refused(tmp_path, clock, memory_state) -> None:
    built = _build(tmp_path, clock, memory_state)
    _write_config(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/everything", headers=_json_headers(token)
        )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# 结果落库
# --------------------------------------------------------------------------


async def test_a_result_is_recorded_against_the_generation_that_was_tested(
    tmp_path, clock, memory_state
) -> None:
    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_probe=_Spy(result={"ok": True}),
        gemini_real_test_enabled=True,
    )
    _write_config(built, generation=9)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/gemini_connection", headers=_json_headers(token)
        )
    assert response.json()["status"] == "passed"
    snapshot = await built["provider_state"].snapshot()
    assert snapshot.tests["gemini_connection"].generation == 9
    assert snapshot.tests["gemini_connection"].status == "passed"


async def test_no_result_is_recorded_when_there_is_no_configuration_yet(
    tmp_path, clock, memory_state
) -> None:
    """反例：文件不存在时没有代次可归属，硬凑一个等于伪造证据。"""
    built = _build(
        tmp_path, clock, memory_state, gemini_real_test_enabled=True
    )
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/gemini_connection", headers=_json_headers(token)
        )
    assert response.json()["error_code"] == ProbeErrorCode.NOT_CONFIGURED.value
    snapshot = await built["provider_state"].snapshot()
    assert snapshot.tests == {}


async def test_the_response_never_carries_the_secret(
    tmp_path, clock, memory_state
) -> None:
    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_probe=_Spy(error=RuntimeError(_GEMINI_KEY)),
        gemini_real_test_enabled=True,
    )
    _write_config(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/gemini_connection", headers=_json_headers(token)
        )
    assert _GEMINI_KEY not in response.text
    assert set(response.json()) == {"status", "duration_ms", "error_code"}


# --------------------------------------------------------------------------
# OAuth 测试：两个 state 域必须互不相认
# --------------------------------------------------------------------------


async def _passed_credentials(
    built: Any, *, generation: int = 4, web_loaded: int | None = None
) -> None:
    """OAuth 测试的两道前置：凭据在该代次通过，且 Web 进程已加载该代次。

    ``web_loaded`` 缺省与 ``generation`` 同代；传别的值模拟"文件已保存、Web 尚未重启"。
    """
    from xiaowei_agent.persistence.provider_state import CheckName, RecordTestCommand

    await built["provider_state"].record_test(
        command=RecordTestCommand(
            check_name=CheckName.FEISHU_CREDENTIALS,
            generation=generation,
            status="passed",
            duration_ms=5,
        )
    )
    await _web_loaded(built, generation if web_loaded is None else web_loaded)


async def _web_loaded(built: Any, generation: int, *, status: str = "loaded") -> None:
    """Web 进程为飞书域签下的加载回执——OAuth adapter 实际持有的就是这一代凭据。"""
    await built["provider_state"].record_load(
        receipts={
            (SERVICE_WEB, ConfigDomain.FEISHU): LoadReceipt(
                generation=generation, status=status
            )
        }
    )


async def test_oauth_test_requires_passing_feishu_credentials_first(
    tmp_path, clock, memory_state
) -> None:
    oauth = _OAuth()
    built = _build(
        tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True
    )
    _write_config(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )
    body = response.json()
    assert body["error_code"] == ProbeErrorCode.NOT_CONFIGURED.value
    assert "authorization_url" not in body
    # 未签发任何 state。
    assert memory_state.oauth_states == {}


async def test_oauth_test_at_a_stale_generation_is_refused(
    tmp_path, clock, memory_state
) -> None:
    """反例：凭据在**上一代**通过不算数。

    保存一次配置就可能换了 App Secret；拿旧代次的通过去放行 OAuth 测试，会让
    页面显示"可用"而实际用的是一把已经被换掉的密钥。
    """
    built = _build(
        tmp_path, clock, memory_state, feishu_real_test_enabled=True
    )
    _write_config(built, generation=5)
    await _passed_credentials(built, generation=4)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )
    assert response.json()["error_code"] == ProbeErrorCode.NOT_CONFIGURED.value
    assert memory_state.oauth_states == {}


@pytest.mark.parametrize(
    ("web_loaded", "status"),
    [(4, "loaded"), (None, "loaded"), (5, "invalid")],
    ids=["web-still-on-old-generation", "web-never-loaded", "web-loaded-invalid"],
)
async def test_oauth_test_is_refused_until_the_web_process_loads_the_current_generation(
    tmp_path, clock, memory_state, web_loaded: int | None, status: str
) -> None:
    """反例（P1）：文件已是第 5 代、凭据也在第 5 代通过，但 Web 仍持有第 4 代的 OAuth
    adapter。此时放行测试，跳出去的是旧凭据，结果却会记到第 5 代——重启后页面直接显示
    "OAuth 可用"，而新 App ID/Secret 从未走通过回调。重启前必须拒绝。"""
    oauth = _OAuth()
    built = _build(tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True)
    _write_config(built, generation=5)
    from xiaowei_agent.persistence.provider_state import CheckName, RecordTestCommand

    await built["provider_state"].record_test(
        command=RecordTestCommand(
            check_name=CheckName.FEISHU_CREDENTIALS,
            generation=5,
            status="passed",
            duration_ms=5,
        )
    )
    if web_loaded is not None:
        await _web_loaded(built, web_loaded, status=status)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )

    body = response.json()
    assert "authorization_url" not in body
    assert body["error_code"] == ProbeErrorCode.NOT_CONFIGURED.value
    assert memory_state.oauth_states == {}
    assert oauth.exchanges == []
    snapshot = await built["provider_state"].snapshot()
    assert snapshot.tests["feishu_oauth"].status != "passed"


async def test_oauth_test_state_is_bound_to_the_generation_it_started_on(
    tmp_path, clock, memory_state
) -> None:
    built = _build(tmp_path, clock, memory_state, feishu_real_test_enabled=True)
    _write_config(built, generation=5)
    await _passed_credentials(built, generation=5)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        await _start_oauth(client, token)

    (context,) = memory_state.oauth_test_contexts.values()
    assert context.config_generation == 5
    assert context.operation_id.startswith("w4:")


@pytest.mark.parametrize("drift", ["file-saved", "web-reloaded", "file-and-web"])
async def test_a_generation_drift_between_start_and_callback_fails_closed(
    tmp_path, clock, memory_state, drift: str
) -> None:
    """反例（P1）：第 5 代开始测试后、回调前配置升到第 6 代。

    不得用旧 adapter 交换 code，也不得给任何代次写 ``passed``——绑定代次已经不是当前
    文件或 Web 加载的代次，这次测试的结论不属于任何一份现行配置。"""
    oauth = _OAuth()
    built = _build(tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True)
    _write_config(built, generation=5)
    await _passed_credentials(built, generation=5)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        state = await _start_oauth(client, token)
        if drift in {"file-saved", "file-and-web"}:
            _write_config(built, generation=6)
        if drift in {"web-reloaded", "file-and-web"}:
            await _web_loaded(built, 6)
        response = await client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_request"}}
    assert oauth.exchanges == []
    snapshot = await built["provider_state"].snapshot()
    assert "feishu_oauth" not in snapshot.tests
    assert [(o, r) for _, o, r in _audit(memory_state)] == [
        (AdminAuditOutcome.STARTED, None),
        (AdminAuditOutcome.FAILED, AdminAuditReasonCode.CONFIG_INVALID),
    ]


async def test_a_same_generation_oauth_test_is_recorded_against_the_bound_generation(
    tmp_path, clock, memory_state
) -> None:
    """对照：文件、Web 回执、state 三者同为第 5 代时正常完成，结果只记第 5 代。"""
    oauth = _OAuth()
    built = _build(tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True)
    _write_config(built, generation=5)
    await _passed_credentials(built, generation=5)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        state = await _start_oauth(client, token)
        response = await client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert oauth.exchanges == ["code-1"]
    snapshot = await built["provider_state"].snapshot()
    assert snapshot.tests["feishu_oauth"].status == "passed"
    assert snapshot.tests["feishu_oauth"].generation == 5


async def test_oauth_test_issues_a_state_that_the_login_path_cannot_consume(
    tmp_path, clock, memory_state
) -> None:
    """两个域互不相认的**正方向**：测试 state 送进登录 callback 必须被拒。

    共用一个域的实现会让"点一下测试按钮"变成"签发一个会话"——登录分支在消费掉
    state 之后会 rotate_session 并下发 cookie。
    """
    oauth = _OAuth()
    built = _build(
        tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True
    )
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        started = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )
        assert started.status_code == 200
        state = started.json()["authorization_url"].split("state=")[1]
        # 清掉本地管理员 session，逼回调只能走登录分支。
        signed_out = await client.post(
            "/app/api/logout", content="{}", headers=_json_headers(token)
        )
        assert signed_out.status_code == 204
        response = await client.get(
            "/oauth/feishu/callback", params={"code": "code-1", "state": state}
        )

    assert response.status_code in {400, 401}
    assert "set-cookie" not in {
        key.lower() for key in response.headers if "session" in response.headers[key]
    }
    # 登录分支没有把它当成自己的 state，因此也没有发起 code 交换。
    assert oauth.exchanges == []


async def test_login_state_cannot_be_consumed_by_the_test_path(
    tmp_path, clock, memory_state
) -> None:
    """反方向：登录 state 送进测试分支同样必须被拒，且不写任何测试结果。"""
    oauth = _OAuth()
    built = _build(
        tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True
    )
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        await _sign_in(client, built["admins"])
        started = await client.get("/oauth/feishu/start", follow_redirects=False)
        state = started.headers["location"].split("state=")[1]
        # 登录 state 存在于登录域；测试分支查的是测试域，查不到。
        response = await client.get(
            "/oauth/feishu/callback", params={"code": "code-1", "state": state}
        )

    # 登录分支自己消费掉了它——这是登录，不是测试。
    snapshot = await built["provider_state"].snapshot()
    assert "feishu_oauth" not in snapshot.tests
    assert response.status_code in {302, 400, 401}


async def test_the_two_state_domains_are_not_the_same_constant() -> None:
    """域隔离的根：两个常量不同，且都不是空串。"""
    assert OAUTH_LOGIN_STATE_DOMAIN != OAUTH_TEST_STATE_DOMAIN
    assert OAUTH_LOGIN_STATE_DOMAIN and OAUTH_TEST_STATE_DOMAIN


async def test_oauth_test_callback_requires_a_live_local_admin_session(
    tmp_path, clock, memory_state
) -> None:
    """命中测试域之后，session 必须仍然有效，否则拒绝且不写结果。"""
    oauth = _OAuth()
    built = _build(
        tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True
    )
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        started = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )
        state = started.json()["authorization_url"].split("state=")[1]
        signed_out = await client.post(
            "/app/api/logout", content="{}", headers=_json_headers(token)
        )
        assert signed_out.status_code == 204
        response = await client.get(
            "/oauth/feishu/callback", params={"code": "code-1", "state": state}
        )

    assert response.status_code in {400, 401}
    assert oauth.exchanges == []
    snapshot = await built["provider_state"].snapshot()
    assert "feishu_oauth" not in snapshot.tests


async def test_oauth_test_branch_issues_no_cookie_and_no_session(
    tmp_path, clock, memory_state
) -> None:
    """成功完成一次 OAuth 测试之后，浏览器手里不该多出任何东西。"""
    oauth = _OAuth()
    built = _build(
        tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True
    )
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        started = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )
        state = started.json()["authorization_url"].split("state=")[1]
        sessions_before = dict(memory_state.web_sessions)
        response = await client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/admin"
    assert oauth.exchanges == ["code-1"]
    # session 表**逐字节不变**：没有轮换，也没有新签发。
    assert dict(memory_state.web_sessions) == sessions_before
    for name, value in response.headers.multi_items():
        if name.lower() == "set-cookie":
            # 只允许清除一次性 state cookie，不允许下发会话 cookie。
            assert "oauth-state" in value
    snapshot = await built["provider_state"].snapshot()
    assert snapshot.tests["feishu_oauth"].status == "passed"
    assert snapshot.tests["feishu_oauth"].generation == 4


async def test_a_failed_oauth_exchange_is_recorded_as_a_closed_code(
    tmp_path, clock, memory_state
) -> None:
    from xiaowei_agent.interfaces.web_auth import FeishuOAuthCodeError

    oauth = _OAuth(error=FeishuOAuthCodeError("rejected"))
    built = _build(
        tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True
    )
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        started = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )
        state = started.json()["authorization_url"].split("state=")[1]
        response = await client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 302
    snapshot = await built["provider_state"].snapshot()
    assert snapshot.tests["feishu_oauth"].status == "failed"


async def test_oauth_test_is_refused_when_feishu_is_not_assembled(
    tmp_path, clock, memory_state
) -> None:
    """没装配飞书时这条路由**仍然在**，但回的是闭集拒绝码。

    这与两条 OAuth 入口不同：那两条在未装配时不注册，因为"路由在但永远失败"是
    假入口。这一条给的是一个可操作的答案，不是一次永远失败的跳转。
    """
    built = _build(
        tmp_path,
        clock,
        memory_state,
        with_feishu_auth=False,
        feishu_real_test_enabled=True,
    )
    _write_config(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )
    assert response.json()["error_code"] == ProbeErrorCode.NOT_CONFIGURED.value


async def test_the_literal_oauth_route_wins_over_the_parameterised_one(
    tmp_path, clock, memory_state
) -> None:
    """注册顺序是承重的：反过来会用凭据探针去回答一个 OAuth 问题。"""
    feishu = _Spy(result={"ok": True})
    built = _build(
        tmp_path,
        clock,
        memory_state,
        feishu_probe=feishu,
        feishu_real_test_enabled=True,
    )
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        response = await client.post(
            "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
        )
    assert "authorization_url" in response.json()
    # 凭据探针一次都没被调用——说明请求没有落进那条参数化路由。
    assert feishu.calls == []


# --------------------------------------------------------------------------
# 授权：权限最大的飞书主体也不能测这台机器的凭据
# --------------------------------------------------------------------------


async def test_probe_routes_reject_a_feishu_principal_with_admin_permission(
    tmp_path, clock, memory_state
) -> None:
    """反例：``ADMIN_ALL_SAFE_TASKS`` 是任务可见范围，不是运维这台机器的权限。

    只测匿名请求不够——匿名在任何一种实现里都会被拒。真正要钉住的是"已登录但
    来源不对"：把判定从**签发来源**换成权限集合，这条就会变红，而飞书那侧的
    权限集合随身份目录而变，等于把配置面的门交给了目录维护者。
    """
    from xiaowei_agent.contracts import IdentitySource
    from xiaowei_agent.interfaces.web_auth import web_csrf_token, web_session_digest
    from xiaowei_agent.persistence.web_session import RotateWebSessionCommand

    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_real_test_enabled=True,
        feishu_real_test_enabled=True,
    )
    _write_config(built)
    await _passed_credentials(built)
    cookie = "feishu_session_cookie_1234567890"
    await built["sessions"].rotate_session(
        command=RotateWebSessionCommand(
            session_digest=web_session_digest(cookie),
            subject_ref="subject-alice",
            ttl_seconds=3600,
            auth_source=IdentitySource.FEISHU,
            public_origin_digest=web_origin_digest(_ORIGIN),
        )
    )
    async with _client(built["app"]) as client:
        client.cookies.set("__Host-xiaowei-session", cookie)
        for name in ("gemini_connection", "feishu_credentials", "feishu_oauth"):
            response = await client.post(
                f"/admin/api/config/test/{name}",
                headers=_json_headers(web_csrf_token(cookie)),
            )
            assert response.status_code == 403, name

    # 一条测试结果都不该留下，state 也不该被签发。
    snapshot = await built["provider_state"].snapshot()
    assert set(snapshot.tests) == {"feishu_credentials"}
    assert memory_state.oauth_states == {}


async def test_probe_routes_refuse_before_the_forced_password_change(
    tmp_path, clock, memory_state
) -> None:
    """反例：改密之前，三个测试项一个都不能触发。

    与配置面同一条理由：初始口令是公开常量。这里更进一步——探针会发起**真实
    出站**，用一个还没被真正接管的账号去点它，等于让任何拿到首启窗口的人替这台
    机器发一次带凭据的请求。
    """
    gemini = _Spy(result={"ok": True})
    feishu = _Spy(result={"ok": True})
    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_probe=gemini,
        feishu_probe=feishu,
        gemini_real_test_enabled=True,
        feishu_real_test_enabled=True,
    )
    _write_config(built)
    await _passed_credentials(built)
    await built["admins"].seed_if_absent(
        password_hash=hash_password(INITIAL_LOCAL_ADMIN_PASSWORD)
    )
    async with _client(built["app"]) as client:
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
        for name in ("gemini_connection", "feishu_credentials", "feishu_oauth"):
            response = await client.post(
                f"/admin/api/config/test/{name}", headers=_json_headers(token)
            )
            assert response.status_code == 403, name
            assert response.json() == {
                "error": {"code": "password_change_required"}
            }

    # 一次出站都没有，也没有签发 OAuth state、没有写下任何新结果。
    assert gemini.calls == []
    assert feishu.calls == []
    assert memory_state.oauth_states == {}
    snapshot = await built["provider_state"].snapshot()
    assert set(snapshot.tests) == {"feishu_credentials"}


# --------------------------------------------------------------------------
# W4a：测试代次取自对应域；每次测试都是两阶段审计
# --------------------------------------------------------------------------


async def test_each_probe_records_the_generation_of_its_own_domain(
    tmp_path, clock, memory_state
) -> None:
    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_probe=_Spy(result={"ok": True}),
        feishu_probe=_Spy(result={"ok": True}),
        gemini_real_test_enabled=True,
        feishu_real_test_enabled=True,
    )
    _write_config(built, generation=6, ai_generation=11)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        for name in ("gemini_connection", "feishu_credentials"):
            response = await client.post(
                f"/admin/api/config/test/{name}", headers=_json_headers(token)
            )
            assert response.json()["status"] == "passed"
    snapshot = await built["provider_state"].snapshot()
    assert snapshot.tests["gemini_connection"].generation == 11
    assert snapshot.tests["feishu_credentials"].generation == 6
    outcomes = [(outcome, reason) for _, outcome, reason in _audit(memory_state)]
    assert outcomes == [
        (AdminAuditOutcome.STARTED, None),
        (AdminAuditOutcome.SUCCEEDED, None),
        (AdminAuditOutcome.STARTED, None),
        (AdminAuditOutcome.SUCCEEDED, None),
    ]


async def test_a_failed_probe_is_audited_as_failed(tmp_path, clock, memory_state) -> None:
    built = _build(
        tmp_path,
        clock,
        memory_state,
        gemini_probe=_Spy(error=RuntimeError("provider down")),
        gemini_real_test_enabled=True,
    )
    _write_config(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        await client.post(
            "/admin/api/config/test/gemini_connection", headers=_json_headers(token)
        )
    assert [(o, r) for _, o, r in _audit(memory_state)] == [
        (AdminAuditOutcome.STARTED, None),
        (AdminAuditOutcome.FAILED, AdminAuditReasonCode.PROBE_FAILED),
    ]


async def _start_oauth(client: httpx.AsyncClient, token: str) -> str:
    started = await client.post(
        "/admin/api/config/test/feishu_oauth", headers=_json_headers(token)
    )
    assert started.status_code == 200
    return str(started.json()["authorization_url"].split("state=")[1])


async def test_oauth_test_state_carries_the_started_operation_to_the_callback(
    tmp_path, clock, memory_state
) -> None:
    oauth = _OAuth()
    built = _build(tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True)
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        state = await _start_oauth(client, token)
        after_start = _audit(memory_state)
        response = await client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert [outcome for _, outcome, _ in after_start] == [AdminAuditOutcome.STARTED]
    (operation_id, _, _) = after_start[0]
    assert re.fullmatch(r"w4:[0-9a-f]{32}", operation_id)
    # 签发时写进 context 的就是这条 STARTED 的 operation id。
    assert {c.operation_id for c in memory_state.oauth_test_contexts.values()} <= {
        operation_id
    }
    assert _audit(memory_state) == [
        (operation_id, AdminAuditOutcome.STARTED, None),
        (operation_id, AdminAuditOutcome.SUCCEEDED, None),
    ]


@pytest.mark.parametrize("shape", ["unknown", "expired", "cookie_mismatch"])
async def test_an_invalid_or_expired_test_state_leaves_only_started(
    tmp_path, clock, memory_state, shape: str
) -> None:
    """拿不到可信 operation id 时绝不补写终态：只剩 STARTED 就是"结果未知"。"""
    oauth = _OAuth()
    built = _build(tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True)
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        state = await _start_oauth(client, token)
        if shape == "unknown":
            state = "u" * len(state)
            client.cookies.set("__Host-xiaowei-oauth-state", state)
        elif shape == "expired":
            clock.advance(seconds=301)
        else:
            client.cookies.set("__Host-xiaowei-oauth-state", "m" * len(state))
        response = await client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert oauth.exchanges == []
    assert [outcome for _, outcome, _ in _audit(memory_state)] == [
        AdminAuditOutcome.STARTED
    ]
    snapshot = await built["provider_state"].snapshot()
    assert "feishu_oauth" not in snapshot.tests


async def test_a_consumed_state_with_a_revoked_session_fails_without_calling_the_provider(
    tmp_path, clock, memory_state
) -> None:
    oauth = _OAuth()
    built = _build(tmp_path, clock, memory_state, oauth=oauth, feishu_real_test_enabled=True)
    _write_config(built)
    await _passed_credentials(built)
    async with _client(built["app"]) as client:
        token = await _sign_in(client, built["admins"])
        state = await _start_oauth(client, token)
        signed_out = await client.post(
            "/app/api/logout", content="{}", headers=_json_headers(token)
        )
        assert signed_out.status_code == 204
        response = await client.get(
            "/oauth/feishu/callback",
            params={"code": "code-1", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 401
    assert oauth.exchanges == []
    assert [(o, r) for _, o, r in _audit(memory_state)] == [
        (AdminAuditOutcome.STARTED, None),
        (AdminAuditOutcome.FAILED, AdminAuditReasonCode.SESSION_INVALID),
    ]
    snapshot = await built["provider_state"].snapshot()
    assert "feishu_oauth" not in snapshot.tests


async def test_a_feishu_admin_probe_attempt_is_denied_with_audit(
    tmp_path, clock, memory_state
) -> None:
    from xiaowei_agent.interfaces.web_auth import web_csrf_token, web_session_digest
    from xiaowei_agent.persistence.web_session import RotateWebSessionCommand

    spy = _Spy(result={"ok": True})
    built = _build(
        tmp_path, clock, memory_state, gemini_probe=spy, gemini_real_test_enabled=True
    )
    _write_config(built)
    cookie = "feishu_session_cookie_1234567890"
    await built["sessions"].rotate_session(
        command=RotateWebSessionCommand(
            session_digest=web_session_digest(cookie),
            subject_ref="subject-alice",
            ttl_seconds=3600,
            auth_source=IdentitySource.FEISHU,
            public_origin_digest=web_origin_digest(_ORIGIN),
        )
    )
    async with _client(built["app"]) as client:
        client.cookies.set("__Host-xiaowei-session", cookie)
        response = await client.post(
            "/admin/api/config/test/gemini_connection",
            headers=_json_headers(web_csrf_token(cookie)),
        )
    assert response.status_code == 403
    assert spy.calls == []
    assert [(o, r) for _, o, r in _audit(memory_state)] == [
        (AdminAuditOutcome.DENIED, AdminAuditReasonCode.AUTH_SOURCE_NOT_ALLOWED)
    ]
