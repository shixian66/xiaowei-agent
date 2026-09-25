"""默认关闭、无执行权的飞书认证 Web 工作台。"""

import asyncio
import datetime as dt
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.resources import files
from typing import Annotated, Final, TypeVar, cast
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xiaowei_agent.application.admin_identity import (
    AdminIdentityActor,
    AdminIdentityConflictError,
    AdminIdentityForbiddenError,
    AdminIdentityNotFoundError,
    AdminIdentityService,
    AdminIdentityUnavailableError,
)
from xiaowei_agent.application.channel_access import (
    FeishuMembershipPort,
    TaskAccessNotFoundError,
    TaskAccessQuery,
    TaskAccessService,
    TaskAccessSnapshotUnavailableError,
    TaskListQuery,
)
from xiaowei_agent.application.channel_submission import (
    ChannelParentNotFoundError,
    ChannelSubmissionForbiddenError,
    ChannelSubmissionService,
    ChannelSubmitCommand,
)
from xiaowei_agent.application.integration_config_service import (
    FeishuUpdate,
    GeminiUpdate,
    IntegrationConfigForbiddenError,
    IntegrationConfigService,
    IntegrationConfigUnavailableError,
    OAuthTestGenerationDriftError,
    PrometheusDraft,
    ResourceConflictError,
    ResourceNotFoundError,
    ResourceRejectedError,
    ResourceSaved,
    ResourceUpdate,
    StarRocksDraft,
)
from xiaowei_agent.application.integration_state import (
    SERVICE_WEB,
    ProviderDisplayState,
    compute_display_state,
    current_generation,
    web_holds_feishu_generation,
)
from xiaowei_agent.application.task_view_runtime import (
    ApplicationFailure,
    classify_application_exception,
)
from xiaowei_agent.config import (
    ConfigError,
    RuntimeProfile,
    Settings,
    canonical_non_ip_hostname,
    canonical_web_public_origin,
    load_settings,
)
from xiaowei_agent.contracts import (
    LOCAL_ADMIN_USER_ID,
    TASK_ID_PATTERN,
    AdminAuditAction,
    AdminAuditOutcome,
    AdminCapability,
    AiConfig,
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    ConfigDomain,
    FeishuConfig,
    IdentitySource,
    ProductRole,
    ReadinessProbe,
    TestResult,
    WebMode,
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.contracts.base import AwareDatetime
from xiaowei_agent.contracts.identity import BoundedActor, BoundedId
from xiaowei_agent.contracts.resource_config import (
    PrometheusResource,
    ResourcesConfig,
    StarRocksResource,
)
from xiaowei_agent.interfaces.auth import Clock, trusted_trace_id
from xiaowei_agent.interfaces.body_limit import JsonBodyLimitMiddleware
from xiaowei_agent.interfaces.http_models import error_body
from xiaowei_agent.interfaces.local_admin_auth import (
    LocalAdminAuthenticationError,
    LocalAdminAuthService,
)
from xiaowei_agent.interfaces.provider_consumption import (
    required_services,
    required_services_for_check,
)
from xiaowei_agent.interfaces.provider_probe import (
    PROVIDER_PROBE_TIMEOUT_SECONDS,
    ProbeErrorCode,
    ProbeOutcome,
    ProbeTransport,
    feishu_probe_transport,
    gemini_probe_transport,
    probe_feishu_credentials,
    probe_gemini_connection,
    refused_outcome,
)
from xiaowei_agent.interfaces.web_auth import (
    FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS,
    FeishuOAuthPort,
    OAuthStart,
    WebActivationPendingError,
    WebAuthenticationError,
    WebAuthService,
    WebCsrfError,
    WebDestinationNotAvailableError,
    WebOAuthCodeError,
    WebOAuthLoginContextError,
    WebOAuthStateError,
    WebOAuthUnavailableError,
    WebOriginError,
    validate_state_change,
    validate_unauthenticated_origin,
)
from xiaowei_agent.interfaces.web_models import (
    WebAiConfigChecks,
    WebAiConfigView,
    WebApproveActivationRequest,
    WebChangePasswordRequest,
    WebChangeUserRoleRequest,
    WebConfigClearRequest,
    WebConfigSaved,
    WebCurrentUser,
    WebFeishuConfigChecks,
    WebFeishuConfigUpdate,
    WebFeishuConfigView,
    WebFeishuDomainConfigView,
    WebGeminiConfigUpdate,
    WebGeminiConfigView,
    WebIntegrationDomainStatus,
    WebIntegrationLoadStatus,
    WebIntegrationStatusView,
    WebLoginRequest,
    WebOAuthTestStarted,
    WebPrometheusResourceCreate,
    WebPrometheusResourceView,
    WebRejectActivationRequest,
    WebResourceConfirm,
    WebResourceSaved,
    WebResourcesView,
    WebResourceUpdate,
    WebSetUserStatusRequest,
    WebStarRocksResourceCreate,
    WebStarRocksResourceView,
    WebTaskAccepted,
    WebTaskDetail,
    WebTaskPage,
    WebTaskSubmitRequest,
)
from xiaowei_agent.interfaces.web_navigation import (
    WebNavigationInputError,
    parse_web_return_intent,
    web_login_path,
    web_return_intent_allowed,
    web_return_path,
)
from xiaowei_agent.log import configure_logging
from xiaowei_agent.persistence.provider_state import (
    MAX_TEST_DURATION_MS,
    CheckName,
    ProviderStateSnapshot,
    ProviderStateStore,
)
from xiaowei_agent.trace import bind_trace_id, get_trace_id

_BodyT = TypeVar("_BodyT", bound=BaseModel)
_EnumT = TypeVar("_EnumT", bound=StrEnum)

_SESSION_COOKIE_BASE: Final[str] = "xiaowei-session"
_OAUTH_STATE_COOKIE_BASE: Final[str] = "xiaowei-oauth-state"
ALL_SESSION_COOKIE_NAMES: Final[tuple[str, ...]] = (
    f"__Host-{_SESSION_COOKIE_BASE}",
    _SESSION_COOKIE_BASE,
)
ALL_OAUTH_STATE_COOKIE_NAMES: Final[tuple[str, ...]] = (
    f"__Host-{_OAUTH_STATE_COOKIE_BASE}",
    _OAUTH_STATE_COOKIE_BASE,
)


def session_cookie_name(mode: WebMode) -> str:
    """session cookie 名。

    ``__Host-`` 前缀要求 ``Secure``，而 ``lan_http`` 下没有 TLS，带前缀的 cookie
    会被浏览器整个丢掉——不是"降级为不安全"，是登录直接不工作。因此按模式取名。
    """
    return f"__Host-{_SESSION_COOKIE_BASE}" if mode is WebMode.HTTPS else _SESSION_COOKIE_BASE


def oauth_state_cookie_name(mode: WebMode) -> str:
    """OAuth state cookie 名；同理。"""
    return (
        f"__Host-{_OAUTH_STATE_COOKIE_BASE}"
        if mode is WebMode.HTTPS
        else _OAUTH_STATE_COOKIE_BASE
    )
_CSRF_HEADER: Final[str] = "x-csrf-token"
_CSP: Final[str] = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "object-src 'none'"
)
_HSTS: Final[bytes] = b"max-age=31536000"
_TASK_ID_RE: Final[re.Pattern[str]] = re.compile(TASK_ID_PATTERN)
_POSITIVE_INT_RE: Final[re.Pattern[str]] = re.compile(r"[1-9][0-9]{0,18}")
_MAX_CREATED_SEQ: Final[int] = 9_223_372_036_854_775_807
_BOUNDED_ID_ADAPTER = TypeAdapter(BoundedId)
_BOUNDED_ACTOR_ADAPTER = TypeAdapter(BoundedActor)
_AWARE_DATETIME_ADAPTER = TypeAdapter(AwareDatetime)
_STATIC_MEDIA_TYPES: Final[dict[str, str]] = {
    "admin.js": "text/javascript",
    "app.css": "text/css",
    "app.js": "text/javascript",
    "detail.js": "text/javascript",
    "login.js": "text/javascript",
}
"""被服务的静态资源闭集。

这是**唯一真源**：路由由它循环注册（见 :func:`create_app`）。一个资源手写一条
路由时，壳里加一句 ``<script src>`` 不会带来路由，页面就静默 404。
"""
_CSRF_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_HEALTH_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/readyz"})
_LOGGED_ROUTE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "oauth_start",
        "oauth_callback",
        "web_shell",
        "task_shell",
        "account_api",
        "task_api",
        "static_asset",
    }
)
_LOGGER = logging.getLogger(__name__)
_UVICORN_LOGGER_NAMES: Final[tuple[str, ...]] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
)


class _WebConfigurationError(RuntimeError):
    """Web 真实装配所需的文件引用无效；不保留原始异常。"""


class _PasswordChangeRequiredError(RuntimeError):
    """初始口令尚未更换；除登录/改密/退出外一律拒绝。"""


class _WebInputError(ValueError):
    """Web 协议字段形状不合法；异常正文不返回浏览器。"""


class _SecurityHeadersMiddleware:
    """为正常、框架错误和 body 边界响应统一附加浏览器安全头。

    ``hsts`` 由 Web 模式决定：``lan_http`` 下发 HSTS 会让浏览器把整个
    局域网 IP 记成"只许 HTTPS"，之后连管理面自己都打不开，且该记录在
    max-age 内无法撤销。
    """

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self._app = app
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                security_header_names = {
                    b"content-security-policy",
                    b"cache-control",
                    b"x-content-type-options",
                    b"referrer-policy",
                    b"permissions-policy",
                    b"strict-transport-security",
                }
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in security_header_names
                ]
                headers.extend(
                    (
                        (b"content-security-policy", _CSP.encode("ascii")),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    )
                )
                if self._hsts:
                    headers.append((b"strict-transport-security", _HSTS))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)


def _canonical_host_header(value: str, *, mode: WebMode) -> str | None:
    """规范化 ``Host`` 头，按 Web 模式决定允许的主机形态。

    这里原本无条件走 :func:`canonical_non_ip_hostname`——即"主机名，绝不是 IP"。
    那是 HTTPS 模式的前提：证书签给域名。``lan_http`` 的 authority 本身就是
    IP 字面量，沿用同一条规则会让**每一个**请求都被判成不可信 Host 而 403，
    管理面根本打不开。与 ``public_origin_is_safe`` 是同一类问题、同一种修法：
    协议与主机形态的放宽只作用于本方，且必须由模式显式决定。
    """
    if mode is WebMode.LAN_HTTP:
        return _canonical_lan_authority(value)
    return _canonical_https_host_header(value)


def _canonical_lan_authority(value: str) -> str | None:
    """只接受 canonical loopback/RFC1918 IPv4 + 显式端口。

    复用 :func:`canonical_web_public_origin` 的判定，而不是在这里重写一遍 IP
    规范化——两份实现会以第一次就出现过的方式漂移。
    """
    try:
        origin = canonical_web_public_origin(f"http://{value}", mode=WebMode.LAN_HTTP)
    except ValueError:
        return None
    return urlsplit(origin).netloc


def _canonical_https_host_header(value: str) -> str | None:
    if (
        not value
        or not value.isascii()
        or value != value.strip()
        or value.endswith(":")
        or any(delimiter in value for delimiter in "/?#")
        or any(
            ord(character) < 0x20
            or ord(character) == 0x7F
            or character.isspace()
            for character in value
        )
    ):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return None
    hostname = (
        None
        if parsed.hostname is None
        else canonical_non_ip_hostname(parsed.hostname)
    )
    if (
        hostname is None
        or (port is not None and not 1 <= port <= 65_535)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    if ":" in value and value.rsplit(":", maxsplit=1)[1] != str(port):
        return None
    return hostname if port in {None, 443} else f"{hostname}:{port}"


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", [])
        if key.lower() == name
    ]


def _route_class(_: str, path: str) -> str:
    if path == "/oauth/feishu/start":
        return "oauth_start"
    if path == "/oauth/feishu/callback":
        return "oauth_callback"
    if path == "/app":
        return "web_shell"
    if path.startswith("/app/tasks/"):
        return "task_shell"
    if path in {"/app/api/me", "/app/api/logout"}:
        return "account_api"
    if path == "/app/api/tasks" or path.startswith("/app/api/tasks/"):
        return "task_api"
    if path.startswith("/app/static/"):
        return "static_asset"
    if path == "/healthz":
        return "health"
    if path == "/readyz":
        return "readiness"
    return "other"


def _closed_outcome(status: int) -> str:
    if status < 400:
        return "ok"
    if status < 500:
        return "rejected"
    return "failed"


def _best_effort_log_request(
    *, route_class: str, outcome: str, trace_id: str
) -> None:
    """诊断日志不得改变请求响应或取消语义。"""
    try:
        _LOGGER.info(
            "web request completed",
            extra={
                "route_class": route_class,
                "outcome": outcome,
                "trace_id": trace_id,
            },
        )
    except BaseException:
        return


def _silence_uvicorn_loggers() -> None:
    """Uvicorn 只能由 ``main`` 的固定退出行报告启动失败。"""
    for name in _UVICORN_LOGGER_NAMES:
        logging.getLogger(name).disabled = True


class _WebRequestBoundaryMiddleware:
    """绑定服务端 trace，并只按固定 SSO authority/origin 接受 Web 请求。"""

    def __init__(self, app: ASGIApp, *, public_origin: str, mode: WebMode) -> None:
        self._app = app
        parsed = urlsplit(public_origin)
        self._public_origin = public_origin
        self._authority = parsed.netloc
        self._mode = mode

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        route_class = _route_class(method, path)
        status = 500
        response_started = False
        send_failed = False
        request_failed = False

        async def capture_status(message: Message) -> None:
            nonlocal response_started, send_failed, status
            if message["type"] == "http.response.start":
                response_started = True
                status = int(message["status"])
            try:
                await send(message)
            except BaseException:
                send_failed = True
                raise

        with bind_trace_id() as trace_id:
            try:
                trusted = path in _HEALTH_PATHS
                if not trusted:
                    hosts = _header_values(scope, b"host")
                    origins = _header_values(scope, b"origin")
                    trusted = (
                        len(hosts) == 1
                        and _canonical_host_header(hosts[0], mode=self._mode)
                        == self._authority
                        and (
                            not origins
                            or (
                                len(origins) == 1
                                and origins[0] == self._public_origin
                            )
                        )
                    )
                if trusted:
                    try:
                        await self._app(scope, receive, capture_status)
                    except Exception as exc:
                        request_failed = True
                        if send_failed:
                            raise
                        if not response_started:
                            try:
                                response = await _application_error(
                                    Request(scope), exc
                                )
                            except Exception:
                                response = _error(500, "internal_error")
                            await response(scope, receive, capture_status)
                else:
                    response = _error(403, "forbidden")
                    await response(scope, receive, capture_status)
            finally:
                if route_class in _LOGGED_ROUTE_CLASSES:
                    outcome = (
                        "failed"
                        if request_failed and status < 400
                        else _closed_outcome(status)
                    )
                    _best_effort_log_request(
                        route_class=route_class,
                        outcome=outcome,
                        trace_id=trace_id,
                    )


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code))


async def _validation_error(_: Request, __: Exception) -> Response:
    return _error(400, "invalid_request")


async def _http_error(_: Request, exc: Exception) -> Response:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    if status == 404:
        return _error(404, "not_found")
    if status == 405:
        return _error(405, "method_not_allowed")
    if status == 415:
        return _error(415, "unsupported_media_type")
    return _error(400, "invalid_request")


async def _authentication_error(_: Request, __: Exception) -> Response:
    return _error(401, "unauthorized")


async def _oauth_input_error(_: Request, __: Exception) -> Response:
    return _error(400, "invalid_request")


async def _oauth_unavailable(_: Request, __: Exception) -> Response:
    return _error(503, "unavailable")


async def _password_change_required(_: Request, __: Exception) -> Response:
    return _error(403, "password_change_required")


async def _forbidden(_: Request, __: Exception) -> Response:
    return _error(403, "forbidden")


async def _input_error(_: Request, __: Exception) -> Response:
    return _error(400, "invalid_request")


async def _task_not_found(_: Request, __: Exception) -> Response:
    return _error(404, "not_found")


async def _task_snapshot_unavailable(_: Request, __: Exception) -> Response:
    return _error(503, "unavailable")


async def _config_unavailable(_: Request, __: Exception) -> Response:
    return _error(503, "unavailable")


async def _admin_identity_not_found(_: Request, __: Exception) -> Response:
    return _error(404, "not_found")


async def _admin_identity_conflict(_: Request, __: Exception) -> Response:
    return _error(409, "idempotency_conflict")


async def _resource_not_found(_: Request, __: Exception) -> Response:
    return _error(404, "not_found")


async def _resource_conflict(_: Request, __: Exception) -> Response:
    return _error(409, "conflict")


async def _admin_identity_unavailable(_: Request, __: Exception) -> Response:
    return _error(503, "unavailable")


async def _application_error(_: Request, exc: Exception) -> Response:
    failure = classify_application_exception(exc)
    if failure is ApplicationFailure.CLARIFICATION_INTEGRITY:
        return _error(500, "clarification.integrity_error")
    if failure is ApplicationFailure.CONFLICT:
        return _error(409, "idempotency_conflict")
    if failure is ApplicationFailure.NOT_FOUND:
        return _error(404, "not_found")
    if failure is ApplicationFailure.UNAVAILABLE:
        return _error(503, "unavailable")
    return _error(500, "internal_error")


def _set_secret_cookie(
    response: Response,
    *,
    name: str,
    value: str,
    max_age: int,
    secure: bool,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _clear_secret_cookie(response: Response, *, name: str, secure: bool) -> None:
    response.delete_cookie(
        key=name,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _clear_all_known_cookies(
    response: Response, *, names: tuple[str, ...], secure: bool
) -> None:
    """两个已知 cookie 名都清一遍。

    切换 ``XIAOWEI_WEB_MODE`` 之后浏览器里可能同时留着 ``__Host-`` 版和普通版；
    只清当前模式那一个会留下一个永远读不到、也永远不过期的残留 cookie。
    """
    for name in names:
        _clear_secret_cookie(response, name=name, secure=secure)


def _is_json_request(request: Request) -> bool:
    values = request.headers.getlist("content-type")
    if len(values) != 1:
        return False
    return values[0].split(";", 1)[0].strip().lower() == "application/json"


def _single_cookie(request: Request, *, name: str) -> tuple[str | None, bool]:
    """返回唯一原始 cookie；重复同名值不交给框架静默择一。"""
    matches: list[str] = []
    for header in request.headers.getlist("cookie"):
        for item in header.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                matches.append(value)
    if len(matches) > 1:
        return None, False
    return (matches[0] if matches else None), True


_LOGIN_OAUTH_ENTRY: Final[str] = '<div class="oauth-entry" id="oauth-entry">'
_LOGIN_INTRO_RE: Final[re.Pattern[str]] = re.compile(
    r'(<p class="auth-intro">)[^<]*(</p>)'
)
_LOCAL_ONLY_INTRO: Final[str] = "请使用本地管理员账号登录。"


def _login_shell(*, shell: str, oauth_available: bool) -> str:
    """按装配事实裁掉不可用的 OAuth 入口与文案，不生成第二套登录页面。

    标记不是恰好一次时启动即失败，避免静默漏删飞书文案。
    """
    if oauth_available:
        return shell
    if shell.count(_LOGIN_OAUTH_ENTRY) != 1:
        raise RuntimeError("login shell markup drifted")
    hidden = shell.replace(
        _LOGIN_OAUTH_ENTRY,
        '<div class="oauth-entry is-hidden" id="oauth-entry">',
        1,
    )
    replaced, count = _LOGIN_INTRO_RE.subn(
        rf"\g<1>{_LOCAL_ONLY_INTRO}\g<2>", hidden
    )
    if count != 1:
        raise RuntimeError("login shell markup drifted")
    return replaced


_CAPABILITY_STRIP_RE: Final[re.Pattern[str]] = re.compile(
    r'<div class="capability-strip" aria-label="当前只读能力示例">.*?</div>', re.DOTALL
)
_TASK_INPUT_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    r'(<textarea id="task-input"[^>]*?) placeholder="[^"]*"'
)
_PROVIDER_OFF_CAPABILITY_STRIP: Final[str] = (
    '<div class="capability-strip" aria-label="当前可执行能力">'
    "<span>当前无可执行能力</span></div>"
)
_PROVIDER_OFF_PLACEHOLDER: Final[str] = "描述想检查的内容；当前部署尚未接入可执行能力"


def _provider_off_workbench_shell(shell: str) -> str:
    """W5 release：工作台壳不再展示三个 recording 能力的示例与占位提示。

    普通对话只投影空准入快照；页面上的静态示例若仍写着"慢查询证据"等能力，就是在
    替 Runtime 虚报当前能力。两处标记都必须恰好出现一次，壳的标记漂移时启动即失败，
    而不是静默放过一个仍在宣称能力的页面。
    """
    stripped, strips = _CAPABILITY_STRIP_RE.subn(_PROVIDER_OFF_CAPABILITY_STRIP, shell)
    replaced, placeholders = _TASK_INPUT_PLACEHOLDER_RE.subn(
        rf'\1 placeholder="{_PROVIDER_OFF_PLACEHOLDER}"', stripped
    )
    if strips != 1 or placeholders != 1:
        raise RuntimeError("workbench shell markup drifted")
    return replaced


def _password_change_shell(*, csrf_token: str) -> str:
    """强制改密壳；初始口令是源码常量，改完之前不放行任何业务接口。

    ``csrf_token`` 由服务端渲染进页面。session cookie 是 ``HttpOnly``，页面脚本
    读不到它，也就**派生不出** CSRF token；而 token 原本的唯一出口
    ``/app/api/me`` 恰好被改密闸门挡在门外。不在这里交付，改密就永远拿不到
    token，首启闭环卡死在第二步。

    交给页面是安全的：token 仍然绑定当前 session，跨站脚本读不到同源 HTML，
    伪造的 token 依旧会被 :func:`validate_state_change` 拒绝。
    """
    if _CSRF_TOKEN_RE.fullmatch(csrf_token) is None:
        # 拼进 HTML 的值必须先确认形态。当前 token 由 sha256 十六进制构成，
        # 这条断言是为了让"以后换了 token 形态"变成立刻可见的错误而不是注入点。
        raise RuntimeError("csrf token is not renderable")
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<title>小维 Agent 修改口令</title>"
        f'<meta name="csrf-token" content="{csrf_token}">'
        '<link rel="stylesheet" href="/app/static/app.css"></head><body>'
        "<main><h1>请先修改初始口令</h1>"
        '<form id="change-password-form" method="post" action="/login/api/change-password">'
        '<label for="current-password">当前口令</label>'
        '<input id="current-password" name="current_password" type="password" required>'
        '<label for="new-password">新口令</label>'
        '<input id="new-password" name="new_password" type="password" required>'
        '<button type="submit">提交</button></form>'
        '<p class="form-message is-hidden" id="form-message" role="alert"></p>'
        '<script type="module" src="/app/static/login.js"></script>'
        "</main></body></html>"
    )


_LOGIN_NOTICE_VALUES: Final = frozenset(
    {"pending", "destination_not_available"}
)


def _login_query(request: Request) -> tuple[WebReturnIntent, str | None]:
    """严格解析登录 query；固定 notice 不进入 return intent 契约。"""
    intent_items: list[tuple[str, str]] = []
    notices: list[str] = []
    for name, value in request.query_params.multi_items():
        if name == "notice":
            notices.append(value)
        else:
            intent_items.append((name, value))
    if len(notices) > 1 or (notices and notices[0] not in _LOGIN_NOTICE_VALUES):
        raise _WebInputError
    try:
        intent = parse_web_return_intent(intent_items)
    except WebNavigationInputError:
        raise _WebInputError from None
    return intent, notices[0] if notices else None


def _login_redirect(intent: WebReturnIntent, *, notice: str | None = None) -> Response:
    path = web_login_path(intent)
    if notice is not None:
        path = f"{path}&notice={notice}"
    return RedirectResponse(path, status_code=302)


def _static_asset_route(
    body: str, media_type: str
) -> Callable[[], Awaitable[Response]]:
    """把一个已读入的静态资源包成路由处理函数。"""

    async def serve() -> Response:
        return Response(body, media_type=media_type)

    return serve


def _asset_text(name: str) -> str:
    """从源码 checkout 与已安装 wheel 的同一包资源位置读取静态文件。"""
    return files("xiaowei_agent.interfaces").joinpath("web_static", name).read_text(
        encoding="utf-8"
    )


def _task_id_or_not_found(task_id: str) -> str:
    if _TASK_ID_RE.fullmatch(task_id) is None:
        raise TaskAccessNotFoundError
    return task_id


def _list_query(request: Request) -> tuple[int | None, int]:
    allowed = {"before_created_seq", "limit"}
    if set(request.query_params) - allowed:
        raise _WebInputError

    cursor_values = request.query_params.getlist("before_created_seq")
    if len(cursor_values) > 1:
        raise TaskAccessNotFoundError
    before_created_seq: int | None = None
    if cursor_values:
        raw_cursor = cursor_values[0]
        if _POSITIVE_INT_RE.fullmatch(raw_cursor) is None:
            raise TaskAccessNotFoundError
        before_created_seq = int(raw_cursor)
        if before_created_seq > _MAX_CREATED_SEQ:
            raise TaskAccessNotFoundError

    limit_values = request.query_params.getlist("limit")
    if len(limit_values) > 1:
        raise _WebInputError
    if not limit_values:
        return before_created_seq, 20
    raw_limit = limit_values[0]
    if _POSITIVE_INT_RE.fullmatch(raw_limit) is None:
        raise _WebInputError
    limit = int(raw_limit)
    if limit > 100:
        raise _WebInputError
    return before_created_seq, limit


def _single_query_values(
    request: Request, *, allowed: frozenset[str]
) -> dict[str, str]:
    """严格 query 边界：未知字段与重复字段都不交给框架静默择一。"""
    if set(request.query_params) - allowed:
        raise _WebInputError
    values: dict[str, str] = {}
    for name in allowed:
        items = request.query_params.getlist(name)
        if len(items) > 1:
            raise _WebInputError
        if items:
            values[name] = items[0]
    return values


def _admin_limit(values: dict[str, str]) -> int:
    raw = values.get("limit")
    if raw is None:
        return 50
    if _POSITIVE_INT_RE.fullmatch(raw) is None:
        raise _WebInputError
    limit = int(raw)
    if limit > 100:
        raise _WebInputError
    return limit


def _bounded_id(value: str) -> str:
    try:
        return _BOUNDED_ID_ADAPTER.validate_python(value)
    except ValidationError:
        raise _WebInputError from None


def _bounded_actor(value: str) -> str:
    try:
        return _BOUNDED_ACTOR_ADAPTER.validate_python(value)
    except ValidationError:
        raise _WebInputError from None


def _aware_datetime(value: str) -> dt.datetime:
    try:
        return _AWARE_DATETIME_ADAPTER.validate_python(value)
    except ValidationError:
        raise _WebInputError from None


def _admin_users_query(request: Request) -> tuple[str | None, int]:
    values = _single_query_values(
        request, allowed=frozenset({"after_actor", "limit"})
    )
    after_actor = values.get("after_actor")
    return (
        None if after_actor is None else _bounded_actor(after_actor),
        _admin_limit(values),
    )


def _admin_activations_query(
    request: Request,
) -> tuple[dt.datetime | None, str | None, int]:
    values = _single_query_values(
        request,
        allowed=frozenset(
            {"before_requested_at", "before_request_id", "limit"}
        ),
    )
    raw_time = values.get("before_requested_at")
    raw_id = values.get("before_request_id")
    if (raw_time is None) != (raw_id is None):
        raise _WebInputError
    return (
        None if raw_time is None else _aware_datetime(raw_time),
        None if raw_id is None else _bounded_id(raw_id),
        _admin_limit(values),
    )


def _optional_enum(value: str | None, enum_type: type[_EnumT]) -> _EnumT | None:
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError:
        raise _WebInputError from None


def _admin_audit_query(
    request: Request,
) -> tuple[
    dt.datetime | None,
    str | None,
    AdminAuditAction | None,
    AdminAuditOutcome | None,
    str | None,
    str | None,
    int,
]:
    values = _single_query_values(
        request,
        allowed=frozenset(
            {
                "before_created_at",
                "before_event_id",
                "action",
                "outcome",
                "target_user_id",
                "target_request_id",
                "limit",
            }
        ),
    )
    raw_time = values.get("before_created_at")
    raw_id = values.get("before_event_id")
    if (raw_time is None) != (raw_id is None):
        raise _WebInputError
    target_user_id = values.get("target_user_id")
    target_request_id = values.get("target_request_id")
    if target_user_id is not None and target_request_id is not None:
        raise _WebInputError
    return (
        None if raw_time is None else _aware_datetime(raw_time),
        None if raw_id is None else _bounded_id(raw_id),
        cast(
            AdminAuditAction | None,
            _optional_enum(values.get("action"), AdminAuditAction),
        ),
        cast(
            AdminAuditOutcome | None,
            _optional_enum(values.get("outcome"), AdminAuditOutcome),
        ),
        None if target_user_id is None else _bounded_id(target_user_id),
        None if target_request_id is None else _bounded_id(target_request_id),
        _admin_limit(values),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _WebInputError
        result[key] = value
    return result


def _reject_non_json_constant(_: str) -> None:
    raise _WebInputError


async def _typed_body(request: Request, model: type[_BodyT]) -> _BodyT:
    """严格 JSON 解析：拒绝重复键、JSON 常量与任何 schema 偏差。"""
    try:
        value = json.loads(
            await request.body(),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_constant,
        )
        return model.model_validate(value)
    except (UnicodeError, ValueError, TypeError, ValidationError):
        raise _WebInputError from None


async def _login_body(request: Request) -> WebLoginRequest:
    return await _typed_body(request, WebLoginRequest)


async def _change_password_body(request: Request) -> WebChangePasswordRequest:
    return await _typed_body(request, WebChangePasswordRequest)


async def _task_submit_body(request: Request) -> WebTaskSubmitRequest:
    try:
        value = json.loads(
            await request.body(),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_json_constant,
        )
        return WebTaskSubmitRequest.model_validate(value)
    except (UnicodeError, ValueError, TypeError, ValidationError):
        raise _WebInputError from None


class _WebForbiddenError(RuntimeError):
    """已认证但不是本地管理员。

    与 401 严格分开：401 是"你是谁我不知道"，403 是"我知道你是谁，但这台机器的
    配置不归你管"。把两者合并会让飞书用户看到一个登录提示，然后无论怎么登都没用。
    """


def _gemini_enabled(config: AiConfig | None) -> bool:
    return config is not None and config.gemini.enabled


def _gemini_configured(config: AiConfig | None) -> bool:
    """AI 域必填字段是否齐备——页面"已配置"与状态机问的是同一个问题，只判一处。"""
    return config is not None and config.gemini.api_key is not None


def _feishu_enabled(config: FeishuConfig | None) -> bool:
    return config is not None and config.feishu.enabled


def _feishu_configured(config: FeishuConfig | None) -> bool:
    """飞书有两个必填字段：只看 secret 会把"填了 secret 没填 App ID"算成已配置。"""
    return (
        config is not None
        and config.feishu.app_id is not None
        and config.feishu.app_secret is not None
    )


def _pending_restart_services(
    *,
    domain: ConfigDomain,
    generation: int,
    required: frozenset[str],
    snapshot: ProviderStateSnapshot,
) -> tuple[str, ...]:
    """需要这个域、却还没报告当前代次 ``loaded`` 的服务名；只看**本域**回执。"""
    return tuple(
        sorted(
            service_name
            for service_name in required
            if (receipt := snapshot.receipts.get((service_name, domain))) is None
            or receipt.status != "loaded"
            or receipt.generation != generation
        )
    )


def _ai_display_state(
    *, settings: Settings, config: AiConfig | None, snapshot: ProviderStateSnapshot
) -> ProviderDisplayState:
    return compute_display_state(
        domain=ConfigDomain.AI,
        enabled=_gemini_enabled(config),
        required_fields_present=_gemini_configured(config),
        current_generation=current_generation(config),
        required_service_names=required_services_for_check(
            settings, CheckName.GEMINI_CONNECTION.value
        ),
        receipts=snapshot.receipts,
        test=snapshot.tests.get(CheckName.GEMINI_CONNECTION.value),
    )


def _feishu_display_state(
    *,
    check_name: CheckName,
    settings: Settings,
    config: FeishuConfig | None,
    snapshot: ProviderStateSnapshot,
) -> ProviderDisplayState:
    return compute_display_state(
        domain=ConfigDomain.FEISHU,
        enabled=_feishu_enabled(config),
        required_fields_present=_feishu_configured(config),
        current_generation=current_generation(config),
        required_service_names=required_services_for_check(settings, check_name.value),
        receipts=snapshot.receipts,
        test=snapshot.tests.get(check_name.value),
    )


def _ai_config_view(
    *, settings: Settings, config: AiConfig | None, snapshot: ProviderStateSnapshot
) -> WebAiConfigView:
    """AI 域查询投影：``configured`` 是布尔，Key 永不出现在响应里。"""
    generation = current_generation(config)
    return WebAiConfigView(
        generation=generation,
        gemini=WebGeminiConfigView(
            enabled=_gemini_enabled(config), configured=_gemini_configured(config)
        ),
        checks=WebAiConfigChecks(
            gemini_connection=_ai_display_state(
                settings=settings, config=config, snapshot=snapshot
            )
        ),
        pending_restart_services=(
            _pending_restart_services(
                domain=ConfigDomain.AI,
                generation=generation,
                required=required_services_for_check(
                    settings, CheckName.GEMINI_CONNECTION.value
                ),
                snapshot=snapshot,
            )
            if _gemini_enabled(config) and _gemini_configured(config)
            else ()
        ),
    )


def _feishu_config_view(
    *, settings: Settings, config: FeishuConfig | None, snapshot: ProviderStateSnapshot
) -> WebFeishuDomainConfigView:
    """飞书域查询投影；App Secret 永不出现，App ID 不是 secret。"""
    generation = current_generation(config)
    required = required_services_for_check(
        settings, CheckName.FEISHU_CREDENTIALS.value
    ) | required_services_for_check(settings, CheckName.FEISHU_OAUTH.value)
    return WebFeishuDomainConfigView(
        generation=generation,
        feishu=WebFeishuConfigView(
            enabled=_feishu_enabled(config),
            configured=_feishu_configured(config),
            app_id=None if config is None else config.feishu.app_id,
        ),
        checks=WebFeishuConfigChecks(
            feishu_credentials=_feishu_display_state(
                check_name=CheckName.FEISHU_CREDENTIALS,
                settings=settings,
                config=config,
                snapshot=snapshot,
            ),
            feishu_oauth=_feishu_display_state(
                check_name=CheckName.FEISHU_OAUTH,
                settings=settings,
                config=config,
                snapshot=snapshot,
            ),
        ),
        pending_restart_services=(
            _pending_restart_services(
                domain=ConfigDomain.FEISHU,
                generation=generation,
                required=required,
                snapshot=snapshot,
            )
            if _feishu_enabled(config) and _feishu_configured(config)
            else ()
        ),
    )


def _latest_test(
    snapshot: ProviderStateSnapshot, checks: tuple[CheckName, ...]
) -> TestResult | None:
    """取一个域最近的脱敏测试事实；没有时间的旧事实排在有时间事实之前。"""
    candidates = [
        snapshot.tests[check.value]
        for check in checks
        if check.value in snapshot.tests
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item.tested_at is not None,
            item.tested_at.isoformat() if item.tested_at is not None else "",
        ),
    )


def _integration_domain_status(
    *,
    domain: ConfigDomain,
    configured: bool,
    generation: int,
    checks: tuple[CheckName, ...],
    settings: Settings,
    snapshot: ProviderStateSnapshot,
    required: frozenset[str] | None = None,
) -> WebIntegrationDomainStatus:
    """从本域文件/回执/测试事实派生一个不含原值的管理面域状态。

    ``required`` 缺省时由测试项推出等待的服务；resources 没有测试项，直接给出 worker。
    """
    required_services = (
        required
        if required is not None
        else frozenset().union(
            *(required_services_for_check(settings, check.value) for check in checks)
        )
    )
    restart_required = False
    load_status: WebIntegrationLoadStatus = "unconfigured"
    if configured and not required_services:
        # 已保存、但本次部署没有任何进程消费它（例如 W5 provider-off release）。
        # 对空集合取 any() 会落到"已加载"——那是虚报，凭据其实没被任何进程使用。
        load_status = "not_applicable"
    elif configured:
        receipts = [
            snapshot.receipts.get((service_name, domain))
            for service_name in required_services
        ]
        if any(
            receipt is not None
            and receipt.generation == generation
            and receipt.status == "invalid"
            for receipt in receipts
        ):
            load_status = "invalid"
        elif any(
            receipt is None
            or receipt.generation != generation
            or receipt.status != "loaded"
            for receipt in receipts
        ):
            load_status = "pending_restart"
            restart_required = True
        else:
            load_status = "loaded"
    latest = _latest_test(snapshot, checks)
    return WebIntegrationDomainStatus(
        domain=domain.value,
        configured=configured,
        restart_required=restart_required,
        load_status=load_status,
        last_test_status=None if latest is None else latest.status,
        last_tested_at=None if latest is None else latest.tested_at,
    )


def _integration_status_view(
    *,
    settings: Settings,
    ai: AiConfig | None,
    feishu: FeishuConfig | None,
    resources: ResourcesConfig | None,
    snapshot: ProviderStateSnapshot,
) -> WebIntegrationStatusView:
    """三域脱敏状态；resources 只有"已登记/待 worker 加载"，没有测试项，也不代表已接入。"""
    return WebIntegrationStatusView(
        domains=(
            _integration_domain_status(
                domain=ConfigDomain.AI,
                configured=_gemini_enabled(ai) and _gemini_configured(ai),
                generation=current_generation(ai),
                checks=(CheckName.GEMINI_CONNECTION,),
                settings=settings,
                snapshot=snapshot,
            ),
            _integration_domain_status(
                domain=ConfigDomain.FEISHU,
                configured=_feishu_enabled(feishu) and _feishu_configured(feishu),
                generation=current_generation(feishu),
                checks=(CheckName.FEISHU_CREDENTIALS, CheckName.FEISHU_OAUTH),
                settings=settings,
                snapshot=snapshot,
            ),
            _integration_domain_status(
                domain=ConfigDomain.RESOURCES,
                configured=resources is not None,
                generation=current_generation(resources),
                checks=(),
                settings=settings,
                snapshot=snapshot,
                required=frozenset(required_services(settings)[ConfigDomain.RESOURCES]),
            ),
        )
    )


def _resource_view(
    resource: StarRocksResource | PrometheusResource,
) -> WebStarRocksResourceView | WebPrometheusResourceView:
    """安全投影：只有定位与展示字段加一个 ``configured`` 布尔。"""
    if isinstance(resource, StarRocksResource):
        return WebStarRocksResourceView(
            resource_id=resource.resource_id,
            environment=resource.environment,
            display_name=resource.display_name,
            enabled=resource.enabled,
            host=resource.host,
            port=resource.port,
            configured=resource.secret_configured,
        )
    return WebPrometheusResourceView(
        resource_id=resource.resource_id,
        environment=resource.environment,
        display_name=resource.display_name,
        enabled=resource.enabled,
        base_url=resource.base_url,
        configured=resource.secret_configured,
    )


def _resources_view(
    *, settings: Settings, config: ResourcesConfig | None, snapshot: ProviderStateSnapshot
) -> WebResourcesView:
    generation = current_generation(config)
    return WebResourcesView(
        generation=generation,
        pending_restart_services=(
            _pending_restart_services(
                domain=ConfigDomain.RESOURCES,
                generation=generation,
                required=frozenset(required_services(settings)[ConfigDomain.RESOURCES]),
                snapshot=snapshot,
            )
            if config is not None
            else ()
        ),
        resources=tuple(
            _resource_view(resource) for resource in (() if config is None else config.resources)
        ),
    )


def _probe_duration_ms(started: float) -> int:
    """与 ``provider_probe`` 用同一个上界裁剪；两处不一致会让写库在边界上失败。"""
    elapsed = int((time.monotonic() - started) * 1000)
    return min(max(elapsed, 0), MAX_TEST_DURATION_MS)


def _probe_failure(code: ProbeErrorCode, *, started: float) -> ProbeOutcome:
    return ProbeOutcome(
        status="failed", duration_ms=_probe_duration_ms(started), error_code=code
    )


def _check_name_or_input_error(value: str) -> CheckName:
    """路径段必须落在三项闭集里。

    用 ``CheckName(value)`` 而不是自己列一遍字符串：页面、表上的 CHECK 与这里问的
    是同一个闭集，抄第二份就会出现"路由接受但写不进表"的那一半。
    """
    try:
        return CheckName(value)
    except ValueError:
        raise _WebInputError from None


def _usable_ai(config: AiConfig | None) -> AiConfig | None:
    """两层与关系都为真才交出配置，否则探针看到的就是"未配置"。

    `.env` 的真实测试开关与这里**不是一回事**：前者回答"这台机器允许发真实请求
    吗"，后者回答"有没有东西可以拿去发"。
    """
    return config if _gemini_enabled(config) and _gemini_configured(config) else None


def _usable_feishu(config: FeishuConfig | None) -> FeishuConfig | None:
    return config if _feishu_enabled(config) and _feishu_configured(config) else None


@dataclass(frozen=True)
class _WebPrincipalSession:
    """两条认证路径的统一结果。

    本地管理员与飞书 OAuth 写的是同一张 ``web_sessions`` 表、用的是同一个
    cookie 名和同一套 digest 域，路由层因此只需要一种会话形状。
    ``must_change_password`` 只有本地管理员可能为真。
    """

    cookie: str = field(repr=False)
    user_id: str
    principal: AuthenticatedPrincipal
    role: ProductRole
    admin_capabilities: frozenset[AdminCapability]
    csrf_token: str = field(repr=False)
    must_change_password: bool


async def _authenticated(
    request: Request,
    *,
    local_admin_auth: LocalAdminAuthService,
    auth: WebAuthService | None,
    cookie_name: str,
) -> _WebPrincipalSession:
    """先按本地管理员解析，再回落到飞书 OAuth。

    顺序不能反：``LocalAdminAuthService.authenticate`` 会核对 ``auth_source``，
    飞书签发的 session 在那一步就被拒，因此回落是安全的；反过来
    ``WebAuthService`` 会拿本地管理员的 ``subject_ref`` 去查身份目录，得到的是
    一个语义错误的"身份不存在"。
    """
    cookie, unambiguous = _single_cookie(request, name=cookie_name)
    if not unambiguous or cookie is None:
        raise WebAuthenticationError
    try:
        local = await local_admin_auth.authenticate(session_cookie=cookie)
    except LocalAdminAuthenticationError:
        pass
    else:
        return _WebPrincipalSession(
            cookie=cookie,
            user_id=LOCAL_ADMIN_USER_ID,
            principal=local.principal,
            role=local.role,
            admin_capabilities=local.admin_capabilities,
            csrf_token=local.csrf_token,
            must_change_password=local.must_change_password,
        )
    if auth is None:
        raise WebAuthenticationError
    session = await auth.authenticate(session_cookie=cookie)
    return _WebPrincipalSession(
        cookie=cookie,
        user_id=session.user_id,
        principal=session.principal,
        role=session.role,
        admin_capabilities=session.admin_capabilities,
        csrf_token=session.csrf_token,
        must_change_password=False,
    )


def _state_change_headers(request: Request) -> tuple[str | None, str | None]:
    origins = request.headers.getlist("origin")
    csrf_tokens = request.headers.getlist(_CSRF_HEADER)
    return (
        origins[0] if len(origins) == 1 else None,
        csrf_tokens[0] if len(csrf_tokens) == 1 else None,
    )


def _validate_state_change(
    request: Request, *, public_origin: str, session_cookie: str
) -> None:
    origin, csrf_token = _state_change_headers(request)
    validate_state_change(
        public_origin=public_origin,
        session_cookie=session_cookie,
        origin=origin,
        csrf_token=csrf_token,
    )


def create_app(
    *,
    auth: WebAuthService | None,
    local_admin_auth: LocalAdminAuthService,
    oauth_available: bool,
    settings: Settings,
    readiness: ReadinessProbe,
    task_access: TaskAccessService,
    submissions: ChannelSubmissionService,
    clock: Clock,
    policy_revision: str,
    provider_state: ProviderStateStore,
    admin_identity: AdminIdentityService,
    integration_config: IntegrationConfigService,
    gemini_probe: ProbeTransport = gemini_probe_transport,
    feishu_probe: ProbeTransport = feishu_probe_transport,
) -> FastAPI:
    """注册认证与薄任务投影路由，不装配执行 Runtime。

    ``auth is None`` 时**不注册**两条 OAuth 路由——不是注册后返回 503。
    「路由在但永远失败」是假入口，M7 §2.5 明文禁止。
    """
    if not settings.web_app_enabled:
        raise ValueError("Web app is disabled")
    if oauth_available != (auth is not None):
        raise ValueError("oauth availability does not match the assembled service")
    mode = settings.web_mode
    secure_cookies = mode is WebMode.HTTPS
    session_cookie = session_cookie_name(mode)
    oauth_state_cookie = oauth_state_cookie_name(mode)
    public_origin = cast(str, settings.web_public_origin).rstrip("/")
    index_shell = _asset_text("index.html")
    if settings.runtime_profile is RuntimeProfile.RELEASE:
        index_shell = _provider_off_workbench_shell(index_shell)
    detail_shell = _asset_text("detail.html")
    login_shell = _login_shell(
        shell=_asset_text("login.html"), oauth_available=oauth_available
    )

    def login_shell_response(*, notice: str | None) -> HTMLResponse:
        """目标无权限仍渲染固定登录壳，但保留闭集 403 语义。"""
        status_code = 403 if notice == "destination_not_available" else 200
        return HTMLResponse(login_shell, status_code=status_code)

    admin_shell = _asset_text("admin.html")
    static_assets = {name: _asset_text(name) for name in _STATIC_MEDIA_TYPES}
    app = FastAPI(
        redirect_slashes=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        JsonBodyLimitMiddleware,
        limit=settings.api_request_body_limit_bytes,
        # 配置保存是 PUT；只认 POST 会让它整条绕过 body 上限。
        methods=frozenset({"POST", "PUT"}),
        # 每一个 JSON 写入口都必须在这里，否则它没有 body 大小上限。
        # 三个 config/test 子路径逐条列全：中间件是精确匹配，不做前缀。
        paths=frozenset(
            {
                "/app/api/logout",
                "/app/api/tasks",
                "/login/api/login",
                "/login/api/change-password",
                "/admin/api/config/ai",
                "/admin/api/config/feishu",
                "/admin/api/config/ai/clear",
                "/admin/api/config/feishu/clear",
                "/admin/api/config/test/gemini_connection",
                "/admin/api/config/test/feishu_credentials",
                "/admin/api/config/test/feishu_oauth",
                "/admin/api/resources/starrocks",
                "/admin/api/resources/prometheus",
                "/admin/api/resources/update",
                "/admin/api/resources/clear-secret",
                "/admin/api/resources/delete",
                "/admin/api/users/status",
                "/admin/api/users/role",
                "/admin/api/activations/approve",
                "/admin/api/activations/reject",
            }
        ),
    )
    # "读当前代次 → 合并 → 原子替换"的串行化由唯一配置写服务那把锁承担；
    # Web handler 不再持有第二把锁，也不直接碰文件。
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(WebAuthenticationError, _authentication_error)
    app.add_exception_handler(LocalAdminAuthenticationError, _authentication_error)
    app.add_exception_handler(WebOAuthStateError, _oauth_input_error)
    app.add_exception_handler(WebOAuthLoginContextError, _oauth_input_error)
    app.add_exception_handler(WebOAuthCodeError, _oauth_input_error)
    app.add_exception_handler(WebOAuthUnavailableError, _oauth_unavailable)
    app.add_exception_handler(WebDestinationNotAvailableError, _forbidden)
    app.add_exception_handler(
        _PasswordChangeRequiredError, _password_change_required
    )
    app.add_exception_handler(_WebForbiddenError, _forbidden)
    app.add_exception_handler(AdminIdentityForbiddenError, _forbidden)
    app.add_exception_handler(AdminIdentityNotFoundError, _admin_identity_not_found)
    app.add_exception_handler(AdminIdentityConflictError, _admin_identity_conflict)
    app.add_exception_handler(
        AdminIdentityUnavailableError, _admin_identity_unavailable
    )
    app.add_exception_handler(IntegrationConfigUnavailableError, _config_unavailable)
    app.add_exception_handler(ResourceNotFoundError, _resource_not_found)
    app.add_exception_handler(ResourceConflictError, _resource_conflict)
    app.add_exception_handler(ResourceRejectedError, _input_error)
    app.add_exception_handler(IntegrationConfigForbiddenError, _forbidden)
    app.add_exception_handler(WebOriginError, _forbidden)
    app.add_exception_handler(WebCsrfError, _forbidden)
    app.add_exception_handler(ChannelParentNotFoundError, _task_not_found)
    app.add_exception_handler(ChannelSubmissionForbiddenError, _forbidden)
    app.add_exception_handler(TaskAccessNotFoundError, _task_not_found)
    app.add_exception_handler(
        TaskAccessSnapshotUnavailableError, _task_snapshot_unavailable
    )
    app.add_exception_handler(_WebInputError, _input_error)
    app.add_exception_handler(Exception, _application_error)

    if auth is not None:
        @app.get("/oauth/feishu/start")
        async def oauth_start(request: Request) -> Response:
            return_intent, notice = _login_query(request)
            if notice is not None:
                raise _WebInputError
            started = await auth.start_login(return_intent=return_intent)
            response = RedirectResponse(started.authorization_url, status_code=302)
            _set_secret_cookie(
                response,
                name=oauth_state_cookie,
                value=started.state_cookie,
                max_age=started.max_age_seconds,
                secure=secure_cookies,
            )
            return response

        async def oauth_connection_test_callback(
            request: Request, *, code: str, state: str, state_cookie: str | None
        ) -> Response:
            """OAuth 连接测试的回调分支：只记一条结果与审计终态，**不签发任何会话**。

            顺序是承重的：先消费测试域 state 并取回服务端 operation id，再核对本地管理员
            session，最后才换 code。state 无效或过期时拿不到可信 operation id，只能保留
            ``STARTED``；state 已消费但会话失效时**不交换 code**，按取回的 operation id
            写 ``FAILED``。
            """
            try:
                binding = await auth.consume_connection_test_state(
                    state=state, state_cookie=state_cookie
                )
            except WebOAuthStateError:
                return _error(400, "invalid_request")
            actor: AdminIdentityActor | None = None
            try:
                actor = identity_actor(await session_allowed_to_work(request))
            except (
                WebAuthenticationError,
                LocalAdminAuthenticationError,
                _PasswordChangeRequiredError,
            ):
                actor = None

            async def exchange() -> ProbeOutcome:
                started = time.monotonic()
                try:
                    await auth.complete_connection_test(code=code)
                except (WebOAuthCodeError, WebAuthenticationError):
                    return _probe_failure(ProbeErrorCode.UNAUTHORIZED, started=started)
                except WebOAuthUnavailableError:
                    return _probe_failure(ProbeErrorCode.UNAVAILABLE, started=started)
                return ProbeOutcome(status="passed", duration_ms=_probe_duration_ms(started))

            try:
                verdict = await integration_config.finish_oauth_test(
                    operation_id=binding.operation_id,
                    config_generation=binding.config_generation,
                    actor=actor,
                    exchange=exchange,
                )
            except OAuthTestGenerationDriftError:
                # 绑定代次已不是当前文件或 Web 已加载的代次：没有交换 code，也没有记结果；
                # 这张 state 对现行配置已无效，与过期 state 同一个闭集回答。
                return _error(400, "invalid_request")
            if verdict is None:
                # 会话已过期/撤销，或已不是本地管理员：测试结束为 FAILED，什么都不交换。
                return _error(401, "unauthorized") if actor is None else _error(
                    403, "forbidden"
                )
            # 不 ``_set_secret_cookie``、不 ``rotate_session``：一次连接测试结束时
            # 浏览器手里不该多出任何东西。
            return RedirectResponse("/admin", status_code=302)

        @app.get("/oauth/feishu/callback")
        async def oauth_callback(
            request: Request,
            code: Annotated[str, Query(min_length=1, max_length=2048)],
            state: Annotated[str, Query(min_length=1, max_length=512)],
        ) -> Response:
            response: Response
            state_cookie, state_cookie_is_unambiguous = _single_cookie(
                request, name=oauth_state_cookie
            )
            previous_session, session_cookie_is_unambiguous = _single_cookie(
                request, name=session_cookie
            )
            if request.query_params.getlist("code") != [code] or request.query_params.getlist(
                "state"
            ) != [state] or not (
                state_cookie_is_unambiguous and session_cookie_is_unambiguous
            ):
                response = _error(400, "invalid_request")
                _clear_secret_cookie(response, name=oauth_state_cookie, secure=secure_cookies)
                return response
            try:
                issued = await auth.complete_login(
                    code=code,
                    state=state,
                    state_cookie=state_cookie,
                    previous_session_cookie=previous_session,
                )
            except WebOAuthStateError:
                # 登录域里没有这个 state。可能是一次连接测试的回调——两个域互不
                # 相认，所以回落是安全的：测试域同样不认识它时仍然是 400，与没有
                # 这条分支时完全一致。
                response = await oauth_connection_test_callback(
                    request, code=code, state=state, state_cookie=state_cookie
                )
            except WebActivationPendingError as pending:
                response = _login_redirect(
                    WebReturnIntent(
                        kind=WebReturnIntentKind.ACTIVATION_STATUS,
                        request_id=pending.request_id,
                    ),
                    notice="pending",
                )
            except WebDestinationNotAvailableError as unavailable:
                response = _login_redirect(
                    unavailable.return_intent,
                    notice="destination_not_available",
                )
            except WebAuthenticationError:
                response = _error(401, "unauthorized")
            except WebOAuthCodeError:
                response = _error(400, "invalid_request")
            except WebOAuthUnavailableError:
                response = _error(503, "unavailable")
            else:
                response = RedirectResponse(
                    web_return_path(issued.return_intent), status_code=302
                )
                _set_secret_cookie(
                    response,
                    name=session_cookie,
                    value=issued.session_cookie,
                    max_age=issued.max_age_seconds,
                    secure=secure_cookies,
                )
            _clear_secret_cookie(response, name=oauth_state_cookie, secure=secure_cookies)
            return response

    async def session_of(request: Request) -> _WebPrincipalSession:
        return await _authenticated(
            request,
            local_admin_auth=local_admin_auth,
            auth=auth,
            cookie_name=session_cookie,
        )

    async def session_allowed_to_work(request: Request) -> _WebPrincipalSession:
        """改密之前只放行 login / change-password / logout。

        初始口令是常量，因此"已登录"在改密前不等于"可以做事"。这个闸门放在
        每条业务路由的入口，而不是某个中间件按路径名单放行——名单会随新增路由漂移。
        """
        session = await session_of(request)
        if session.must_change_password:
            raise _PasswordChangeRequiredError
        return session

    async def workbench_session(request: Request) -> _WebPrincipalSession:
        session = await session_allowed_to_work(request)
        if session.role not in {ProductRole.ADMIN, ProductRole.OPERATOR}:
            raise _WebForbiddenError
        return session

    async def admin_session(request: Request) -> _WebPrincipalSession:
        session = await session_allowed_to_work(request)
        if (
            session.role is not ProductRole.ADMIN
            or AdminCapability.VIEW_INTEGRATION_STATUS
            not in session.admin_capabilities
        ):
            raise _WebForbiddenError
        return session

    async def identity_admin_session(
        request: Request, *, capability: AdminCapability
    ) -> _WebPrincipalSession:
        """W3-lite 双来源 Admin 闸门；配置面的 local-only helper 保持独立。"""
        session = await session_allowed_to_work(request)
        if (
            session.role is not ProductRole.ADMIN
            or capability not in session.admin_capabilities
        ):
            raise _WebForbiddenError
        return session

    def identity_actor(session: _WebPrincipalSession) -> AdminIdentityActor:
        return AdminIdentityActor(
            user_id=session.user_id,
            tenant_id=session.principal.tenant_id,
            environment_id=session.principal.environment_id,
            actor=session.principal.actor,
            auth_source=session.principal.source,
            role=session.role,
            capabilities=session.admin_capabilities,
        )

    async def safe_task_session(request: Request) -> _WebPrincipalSession:
        session = await session_allowed_to_work(request)
        if ChannelPermission.VIEW_SAFE_TASK not in session.principal.permissions:
            raise _WebForbiddenError
        return session

    @app.get("/")
    async def root_entry() -> Response:
        # 不查 Session：登录入口自己决定渲染登录壳、改密壳还是跳到工作台。
        return _login_redirect(WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH))

    @app.get("/login")
    async def login_shell_route(request: Request) -> Response:
        return_intent, notice = _login_query(request)
        try:
            session = await session_of(request)
        except (WebAuthenticationError, LocalAdminAuthenticationError):
            return login_shell_response(notice=notice)
        if session.must_change_password:
            return HTMLResponse(_password_change_shell(csrf_token=session.csrf_token))
        if return_intent.kind is WebReturnIntentKind.ACTIVATION_STATUS:
            return login_shell_response(notice=notice)
        if web_return_intent_allowed(
            source=session.principal.source,
            role=session.role,
            intent=return_intent,
        ):
            return RedirectResponse(web_return_path(return_intent), status_code=302)
        if notice == "destination_not_available":
            return login_shell_response(notice=notice)
        return _login_redirect(
            return_intent, notice="destination_not_available"
        )

    @app.get("/app")
    async def shell(request: Request) -> Response:
        intent = WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH)
        try:
            await workbench_session(request)
        except (WebAuthenticationError, LocalAdminAuthenticationError):
            return _login_redirect(intent)
        except _PasswordChangeRequiredError:
            # 登录入口已会为未改密会话渲染改密壳；壳路由只负责把浏览器带过去。
            return _login_redirect(intent)
        return HTMLResponse(index_shell)

    @app.get("/admin")
    async def admin_shell_route(request: Request) -> Response:
        intent = WebReturnIntent(kind=WebReturnIntentKind.ADMIN_CENTER)
        try:
            await admin_session(request)
        except (WebAuthenticationError, LocalAdminAuthenticationError):
            return _login_redirect(intent)
        except _PasswordChangeRequiredError:
            return _login_redirect(intent)
        return HTMLResponse(admin_shell)

    @app.get("/app/tasks/{task_id}")
    async def detail_shell_route(request: Request, task_id: str) -> Response:
        try:
            await safe_task_session(request)
        except (WebAuthenticationError, LocalAdminAuthenticationError):
            try:
                task_id = _task_id_or_not_found(task_id)
            except TaskAccessNotFoundError:
                raise WebAuthenticationError from None
            return _login_redirect(
                WebReturnIntent(
                    kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
                    task_id=task_id,
                )
            )
        except _PasswordChangeRequiredError:
            # 非法 task_id 不进 Location：保留基线的改密拒绝。
            try:
                task_id = _task_id_or_not_found(task_id)
            except TaskAccessNotFoundError:
                raise _PasswordChangeRequiredError from None
            return _login_redirect(
                WebReturnIntent(
                    kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
                    task_id=task_id,
                )
            )
        task_id = _task_id_or_not_found(task_id)
        return HTMLResponse(detail_shell)

    @app.get("/app/api/me")
    async def current_user(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        return WebCurrentUser.from_principal(
            session.principal,
            role=session.role,
            admin_capabilities=session.admin_capabilities,
            csrf_token=session.csrf_token,
        ).model_dump(mode="json")

    @app.get("/app/api/tasks")
    async def list_tasks(request: Request) -> dict[str, object]:
        session = await workbench_session(request)
        before_created_seq, limit = _list_query(request)
        page = await task_access.list_tasks(
            query=TaskListQuery(
                principal=session.principal,
                before_created_seq=before_created_seq,
                limit=limit,
            )
        )
        return WebTaskPage.from_page(page).model_dump(mode="json")

    @app.post("/app/api/tasks", status_code=202)
    async def submit_task(request: Request) -> dict[str, object]:
        session = await workbench_session(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _task_submit_body(request)
        trace_id = get_trace_id()
        if trace_id is None:  # create_app 的外层 middleware 是唯一可信绑定入口。
            raise RuntimeError("web request trace is unavailable")
        submitted = await submissions.submit(
            command=ChannelSubmitCommand(
                principal=session.principal,
                channel=ChannelKind.WEB,
                request_id=f"web:{trace_id}",
                trace_id=trace_id,
                policy_revision=policy_revision,
                text=body.text,
                client_submission_ref=body.client_submission_id,
                conversation_ref=None,
                submitted_at=clock(),
                clarification_parent_task_id=body.clarification_parent_task_id,
            )
        )
        accepted = WebTaskAccepted.from_submission(submitted)
        exclude = (
            {"clarification_parent_task_id"}
            if accepted.clarification_parent_task_id is None
            else set()
        )
        return accepted.model_dump(mode="json", exclude=exclude)

    @app.get("/app/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> dict[str, object]:
        session = await safe_task_session(request)
        accessible = await task_access.get_task(
            query=TaskAccessQuery(
                principal=session.principal,
                task_id=_task_id_or_not_found(task_id),
            )
        )
        detail = WebTaskDetail.from_accessible(accessible)
        exclude = (
            {"clarification_parent_task_id"}
            if detail.clarification_parent_task_id is None
            else set()
        )
        if detail.clarification is None:
            exclude.add("clarification")
        if detail.disclosure is None:
            exclude.add("disclosure")
        return detail.model_dump(mode="json", exclude=exclude)

    @app.post("/login/api/login")
    async def login(request: Request) -> Response:
        """首次登录必须能在没有任何 cookie 的情况下完成。"""
        origin, _ = _state_change_headers(request)
        content_types = request.headers.getlist("content-type")
        validate_unauthenticated_origin(
            public_origin=public_origin,
            origin=origin,
            content_type=content_types[0] if len(content_types) == 1 else None,
        )
        body = await _login_body(request)
        previous, unambiguous = _single_cookie(request, name=session_cookie)
        issued = await local_admin_auth.login(
            username=body.username,
            password=body.password,
            return_intent=body.return_intent,
            previous_session_cookie=previous if unambiguous else None,
        )
        response = JSONResponse(
            content={
                "status": "ok",
                "destination": web_return_path(issued.return_intent),
            }
        )
        _set_secret_cookie(
            response,
            name=session_cookie,
            value=issued.session_cookie,
            max_age=issued.max_age_seconds,
            secure=secure_cookies,
        )
        return response

    @app.post("/login/api/change-password")
    async def change_password(request: Request) -> Response:
        session = await session_of(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _change_password_body(request)
        issued = await local_admin_auth.change_password(
            session_cookie=session.cookie,
            current=body.current_password,
            new=body.new_password,
            return_intent=body.return_intent,
        )
        response = JSONResponse(
            content={
                "status": "ok",
                "destination": web_return_path(issued.return_intent),
            }
        )
        _set_secret_cookie(
            response,
            name=session_cookie,
            value=issued.session_cookie,
            max_age=issued.max_age_seconds,
            secure=secure_cookies,
        )
        return response

    @app.post("/app/api/logout", status_code=204)
    async def logout(request: Request) -> Response:
        session = await session_of(request)
        if not _is_json_request(request):
            return _error(415, "unsupported_media_type")
        try:
            payload = json.loads(await request.body())
        except (ValueError, UnicodeError):
            return _error(400, "invalid_request")
        if payload != {}:
            return _error(400, "invalid_request")
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        if auth is not None:
            await auth.logout(session_cookie=session.cookie)
        await local_admin_auth.logout(session_cookie=session.cookie)
        response = Response(status_code=204)
        # 两个已知 cookie 名都清：切过模式的浏览器可能同时留着两份。
        _clear_all_known_cookies(
            response, names=ALL_SESSION_COOKIE_NAMES, secure=secure_cookies
        )
        return response

    async def local_admin_session(request: Request) -> _WebPrincipalSession:
        """配置面只服务本地管理员。

        ``ADMIN_ALL_SAFE_TASKS`` 是任务可见范围，不是"能改这台机器的配置"。
        判定按**签发来源**而不是权限集合：权限集合会随身份目录变，来源不会。
        """
        session = await session_allowed_to_work(request)
        if session.principal.source is not IdentitySource.LOCAL_ADMIN:
            raise _WebForbiddenError
        return session

    @app.get("/admin/api/users")
    async def list_admin_users(request: Request) -> dict[str, object]:
        session = await identity_admin_session(
            request, capability=AdminCapability.MANAGE_USERS
        )
        after_actor, limit = _admin_users_query(request)
        page = await admin_identity.list_users(
            actor=identity_actor(session),
            after_actor=after_actor,
            limit=limit,
        )
        return page.model_dump(mode="json")

    @app.post("/admin/api/users/status", status_code=204)
    async def set_admin_user_status(request: Request) -> Response:
        session = await identity_admin_session(
            request, capability=AdminCapability.MANAGE_USERS
        )
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebSetUserStatusRequest)
        await admin_identity.set_user_status(
            actor=identity_actor(session),
            user_id=body.user_id,
            expected_status=body.expected_status,
            expected_role=body.expected_role,
            status=body.status,
            trace_id=trusted_trace_id(),
        )
        return Response(status_code=204)

    @app.post("/admin/api/users/role", status_code=204)
    async def change_admin_user_role(request: Request) -> Response:
        session = await identity_admin_session(
            request, capability=AdminCapability.MANAGE_USERS
        )
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebChangeUserRoleRequest)
        await admin_identity.change_user_role(
            actor=identity_actor(session),
            user_id=body.user_id,
            expected_role=body.expected_role,
            role=body.role,
            trace_id=trusted_trace_id(),
        )
        return Response(status_code=204)

    @app.get("/admin/api/activations")
    async def list_admin_activations(request: Request) -> dict[str, object]:
        session = await identity_admin_session(
            request, capability=AdminCapability.MANAGE_USERS
        )
        before_requested_at, before_request_id, limit = _admin_activations_query(
            request
        )
        page = await admin_identity.list_pending_activations(
            actor=identity_actor(session),
            before_requested_at=before_requested_at,
            before_request_id=before_request_id,
            limit=limit,
        )
        return page.model_dump(mode="json")

    @app.post("/admin/api/activations/approve", status_code=204)
    async def approve_admin_activation(request: Request) -> Response:
        session = await identity_admin_session(
            request, capability=AdminCapability.MANAGE_USERS
        )
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebApproveActivationRequest)
        await admin_identity.approve_activation(
            actor=identity_actor(session),
            request_id=body.request_id,
            account_actor=body.actor,
            display_name=body.display_name,
            approved_role=body.approved_role,
            trace_id=trusted_trace_id(),
        )
        return Response(status_code=204)

    @app.post("/admin/api/activations/reject", status_code=204)
    async def reject_admin_activation(request: Request) -> Response:
        session = await identity_admin_session(
            request, capability=AdminCapability.MANAGE_USERS
        )
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebRejectActivationRequest)
        await admin_identity.reject_activation(
            actor=identity_actor(session),
            request_id=body.request_id,
            trace_id=trusted_trace_id(),
        )
        return Response(status_code=204)

    @app.get("/admin/api/audit")
    async def list_admin_audit(request: Request) -> dict[str, object]:
        session = await identity_admin_session(
            request, capability=AdminCapability.VIEW_ADMIN_AUDIT
        )
        (
            before_created_at,
            before_event_id,
            action,
            outcome,
            target_user_id,
            target_request_id,
            limit,
        ) = _admin_audit_query(request)
        page = await admin_identity.list_audit(
            actor=identity_actor(session),
            before_created_at=before_created_at,
            before_event_id=before_event_id,
            action=action,
            outcome=outcome,
            target_user_id=target_user_id,
            target_request_id=target_request_id,
            limit=limit,
        )
        return page.model_dump(mode="json")

    @app.get("/admin/api/integration-status")
    async def read_integration_status(request: Request) -> dict[str, object]:
        await admin_session(request)
        ai = await integration_config.read_ai()
        feishu = await integration_config.read_feishu()
        resources = await integration_config.read_resources()
        snapshot = await provider_state.snapshot()
        return _integration_status_view(
            settings=settings, ai=ai, feishu=feishu, resources=resources, snapshot=snapshot
        ).model_dump(mode="json")

    # ------------------------------------------------------------------ 按域配置
    #
    # 读取只服务本地管理员且不审计；写与清除经唯一配置写服务，由服务同时校验来源与能力、
    # 先写 STARTED 再动文件。operation id 只来自服务端 ``trusted_trace_id()``。

    @app.get("/admin/api/config/ai")
    async def read_ai_config(request: Request) -> dict[str, object]:
        await local_admin_session(request)
        config = await integration_config.read_ai()
        snapshot = await provider_state.snapshot()
        return _ai_config_view(
            settings=settings, config=config, snapshot=snapshot
        ).model_dump(mode="json")

    @app.get("/admin/api/config/feishu")
    async def read_feishu_config(request: Request) -> dict[str, object]:
        await local_admin_session(request)
        config = await integration_config.read_feishu()
        snapshot = await provider_state.snapshot()
        return _feishu_config_view(
            settings=settings, config=config, snapshot=snapshot
        ).model_dump(mode="json")

    @app.put("/admin/api/config/ai")
    async def save_ai_config(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebGeminiConfigUpdate)
        saved = await integration_config.save_ai(
            actor=identity_actor(session),
            trace_id=trusted_trace_id(),
            update=GeminiUpdate(enabled=body.enabled, api_key=body.api_key),
        )
        return WebConfigSaved(
            domain="ai", generation=saved.generation, restart_required=True
        ).model_dump(mode="json")

    @app.put("/admin/api/config/feishu")
    async def save_feishu_config(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebFeishuConfigUpdate)
        saved = await integration_config.save_feishu(
            actor=identity_actor(session),
            trace_id=trusted_trace_id(),
            update=FeishuUpdate(
                enabled=body.enabled, app_id=body.app_id, app_secret=body.app_secret
            ),
        )
        return WebConfigSaved(
            domain="feishu", generation=saved.generation, restart_required=True
        ).model_dump(mode="json")

    @app.post("/admin/api/config/ai/clear")
    async def clear_ai_config(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        await _typed_body(request, WebConfigClearRequest)
        saved = await integration_config.clear_ai(
            actor=identity_actor(session), trace_id=trusted_trace_id()
        )
        return WebConfigSaved(
            domain="ai", generation=saved.generation, restart_required=True
        ).model_dump(mode="json")

    @app.post("/admin/api/config/feishu/clear")
    async def clear_feishu_config(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        await _typed_body(request, WebConfigClearRequest)
        saved = await integration_config.clear_feishu(
            actor=identity_actor(session), trace_id=trusted_trace_id()
        )
        return WebConfigSaved(
            domain="feishu", generation=saved.generation, restart_required=True
        ).model_dump(mode="json")

    # ------------------------------------------------------------------ 资源（W4b）
    #
    # 只登记参数：这些路由不解析 DNS、不连接目标、没有"测试连接"。读取只服务本地管理员
    # 且不审计；写入经唯一配置写服务（服务同时校验来源与能力、先写 STARTED 再动文件）。
    # 路径都是固定字面量、资源 ID 走 body——body 上限中间件是精确匹配。

    @app.get("/admin/api/resources")
    async def read_resources(request: Request) -> dict[str, object]:
        await local_admin_session(request)
        config = await integration_config.read_resources()
        snapshot = await provider_state.snapshot()
        return _resources_view(settings=settings, config=config, snapshot=snapshot).model_dump(
            mode="json"
        )

    def _resource_saved(saved: ResourceSaved) -> dict[str, object]:
        return WebResourceSaved(
            resource_id=saved.resource_id, generation=saved.generation, restart_required=True
        ).model_dump(mode="json")

    @app.post("/admin/api/resources/starrocks")
    async def create_starrocks_resource(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebStarRocksResourceCreate)
        saved = await integration_config.create_resource(
            actor=identity_actor(session),
            trace_id=trusted_trace_id(),
            draft=StarRocksDraft(
                environment=body.environment,
                display_name=body.display_name,
                host=body.host,
                port=body.port,
                database=body.database,
                username=body.username,
                password=body.password,
                tls_mode=body.tls_mode,
                enabled=body.enabled,
            ),
        )
        return _resource_saved(saved)

    @app.post("/admin/api/resources/prometheus")
    async def create_prometheus_resource(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebPrometheusResourceCreate)
        saved = await integration_config.create_resource(
            actor=identity_actor(session),
            trace_id=trusted_trace_id(),
            draft=PrometheusDraft(
                environment=body.environment,
                display_name=body.display_name,
                base_url=body.base_url,
                auth_mode=body.auth_mode,
                username=body.username,
                secret=body.secret,
                tls_mode=body.tls_mode,
                enabled=body.enabled,
            ),
        )
        return _resource_saved(saved)

    @app.post("/admin/api/resources/update")
    async def update_resource(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebResourceUpdate)
        saved = await integration_config.update_resource(
            actor=identity_actor(session),
            trace_id=trusted_trace_id(),
            resource_id=body.resource_id,
            update=ResourceUpdate(
                environment=body.environment,
                display_name=body.display_name,
                enabled=body.enabled,
                tls_mode=body.tls_mode,
                host=body.host,
                port=body.port,
                database=body.database,
                username=body.username,
                password=body.password,
                base_url=body.base_url,
                auth_mode=body.auth_mode,
                secret=body.secret,
            ),
        )
        return _resource_saved(saved)

    @app.post("/admin/api/resources/clear-secret")
    async def clear_resource_secret(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebResourceConfirm)
        saved = await integration_config.clear_resource_secret(
            actor=identity_actor(session),
            trace_id=trusted_trace_id(),
            resource_id=body.resource_id,
        )
        return _resource_saved(saved)

    @app.post("/admin/api/resources/delete")
    async def delete_resource(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebResourceConfirm)
        saved = await integration_config.delete_resource(
            actor=identity_actor(session),
            trace_id=trusted_trace_id(),
            resource_id=body.resource_id,
        )
        return _resource_saved(saved)

    # ------------------------------------------------------------------ 探针

    async def oauth_test_refusal(config: FeishuConfig | None) -> ProbeOutcome | None:
        """OAuth 测试的四道前置；``None`` 表示可以签发 state。

        第三道——凭据测试必须在**飞书域当前代次**通过——不是礼貌，是必需：OAuth 回调
        链路建立在应用凭据之上，凭据本身没验过时跳出去的失败无法区分"回调地址配错了"
        与"密钥根本不对"。

        第四道——Web 必须已加载当前代次：交换 code 的是本进程**启动期**装配的 OAuth
        adapter。文件已保存、Web 尚未重启时放行，测的是旧凭据，结果却会记到新代次。
        这由服务端回执判定，不靠前端隐藏按钮。
        """
        if not settings.feishu_real_test_enabled:
            return refused_outcome(ProbeErrorCode.REAL_TEST_DISABLED)
        if auth is None or _usable_feishu(config) is None:
            return refused_outcome(ProbeErrorCode.NOT_CONFIGURED)
        snapshot = await provider_state.snapshot()
        credentials = snapshot.tests.get(CheckName.FEISHU_CREDENTIALS.value)
        if (
            credentials is None
            or credentials.status != "passed"
            or credentials.generation != current_generation(config)
        ):
            return refused_outcome(ProbeErrorCode.NOT_CONFIGURED)
        if not web_holds_feishu_generation(snapshot.receipts, current_generation(config)):
            return refused_outcome(ProbeErrorCode.NOT_CONFIGURED)
        return None

    # 字面量路由必须注册在带路径参数的那条**之前**：Starlette 按注册顺序匹配。
    # 顺序之外还有一道兜底——下面那条路由显式拒绝 ``feishu_oauth``，因此即使有人
    # 调换了顺序，也不会静默落进"用凭据探针去测 OAuth"的错误分支。
    @app.post("/admin/api/config/test/feishu_oauth", response_model=None)
    async def start_oauth_test(request: Request) -> Response:
        """写 ``STARTED`` 后签发一次只能被测试分支消费、且绑定该 operation 的 state。

        ``auth is None`` 时这条路由仍然注册——它返回的是一个闭集拒绝码而不是一次
        永远失败的跳转。管理员需要知道"没装配"，而不是点下去毫无反应。
        """
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )

        async def issue(operation_id: str, config_generation: int) -> OAuthStart:
            return await cast(WebAuthService, auth).start_connection_test(
                operation_id=operation_id, config_generation=config_generation
            )

        result = await integration_config.start_oauth_test(
            actor=identity_actor(session),
            trace_id=trusted_trace_id(),
            refuse=oauth_test_refusal,
            issue=issue,
        )
        if not isinstance(result, OAuthStart):
            return JSONResponse(
                content=ProbeOutcome(
                    status=result.status,
                    duration_ms=result.duration_ms,
                    error_code=result.error_code,
                ).model_dump(mode="json")
            )
        response = JSONResponse(
            content=WebOAuthTestStarted(
                authorization_url=result.authorization_url
            ).model_dump(mode="json")
        )
        _set_secret_cookie(
            response,
            name=oauth_state_cookie,
            value=result.state_cookie,
            max_age=result.max_age_seconds,
            secure=secure_cookies,
        )
        return response

    @app.post("/admin/api/config/test/{check_name}")
    async def run_provider_test(
        request: Request, check_name: str
    ) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        check = _check_name_or_input_error(check_name)
        if check is CheckName.FEISHU_OAUTH:
            # 走到这里说明上面那条字面量路由被移到了后面。宁可 400，也不能用
            # 凭据探针去回答一个 OAuth 问题——那会写下一条名不副实的测试结果。
            raise _WebInputError
        if check is CheckName.GEMINI_CONNECTION:

            async def gemini(config: AiConfig | None) -> ProbeOutcome:
                usable = _usable_ai(config)
                return await probe_gemini_connection(
                    enabled=settings.gemini_real_test_enabled,
                    api_key=None if usable is None else usable.gemini.api_key,
                    transport=gemini_probe,
                    timeout_seconds=PROVIDER_PROBE_TIMEOUT_SECONDS,
                )

            outcome = await integration_config.test_ai_connection(
                actor=identity_actor(session), trace_id=trusted_trace_id(), probe=gemini
            )
        else:

            async def feishu(config: FeishuConfig | None) -> ProbeOutcome:
                usable = _usable_feishu(config)
                return await probe_feishu_credentials(
                    enabled=settings.feishu_real_test_enabled,
                    app_id=None if usable is None else usable.feishu.app_id,
                    app_secret=None if usable is None else usable.feishu.app_secret,
                    transport=feishu_probe,
                    timeout_seconds=PROVIDER_PROBE_TIMEOUT_SECONDS,
                )

            outcome = await integration_config.test_feishu_credentials(
                actor=identity_actor(session), trace_id=trusted_trace_id(), probe=feishu
            )
        return ProbeOutcome(
            status=outcome.status,
            duration_ms=outcome.duration_ms,
            error_code=outcome.error_code,
        ).model_dump(mode="json")

    # 路由从闭集注册表循环注册，而不是一条一条手写：漏一条就是页面静默 404，
    # 而那种 404 只有真正用浏览器打开才会发现。
    for asset_name, media_type in _STATIC_MEDIA_TYPES.items():
        app.add_api_route(
            f"/app/static/{asset_name}",
            _static_asset_route(static_assets[asset_name], media_type),
            methods=["GET"],
            response_model=None,
        )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=None)
    async def ready() -> Response:
        report = await readiness.check()
        if not (
            report.database_ok and report.revision_matches_head and report.assembled
        ):
            return _error(503, "unavailable")
        return JSONResponse(content=report.model_dump(mode="json"))

    app.middleware_stack = _SecurityHeadersMiddleware(
        _WebRequestBoundaryMiddleware(
            app.build_middleware_stack(),
            public_origin=public_origin,
            mode=mode,
        ),
        hsts=secure_cookies,
    )
    return app


async def serve_web(settings: Settings) -> int:
    """装配真实、默认关闭的 Web 进程，并释放唯一数据库 Engine。

    这里是 composition root：读飞书域文件、判双层开关、构造或**不构造**真实飞书
    adapter。装配函数只收端口，不读配置。
    """
    if not settings.web_app_enabled:
        raise _WebConfigurationError
    configure_logging(settings)
    _silence_uvicorn_loggers()

    from xiaowei_agent.interfaces.feishu_oauth import FeishuOAuthAdapter
    from xiaowei_agent.interfaces.feishu_sdk import (
        FeishuSdkError,
        FeishuSdkMembershipAdapter,
    )
    from xiaowei_agent.interfaces.local_stack import (
        WebStackConfigurationError,
        build_postgres_web_stack,
    )
    from xiaowei_agent.interfaces.provider_consumption import (
        load_provider_credentials,
    )

    # 回执与凭据出自**同一次读取**：分两次读会让"页面上说加载了第几代"与
    # "进程实际用的是哪一代"在两次读取之间的保存里错开一代。
    # ``service_name`` 只能是 ``web``：Web 进程唯一能作证的是它自己加载了第几代，
    # worker / listener / channel worker 那几条由它们各自启动时写。
    credentials, receipts = load_provider_credentials(
        settings=settings, service_name=SERVICE_WEB
    )
    oauth: FeishuOAuthPort | None = None
    membership: FeishuMembershipPort | None = None
    if (
        settings.feishu_oauth_enabled
        and credentials.feishu_app_id is not None
        and credentials.feishu_app_secret is not None
    ):
        try:
            oauth = FeishuOAuthAdapter(
                app_id=credentials.feishu_app_id,
                app_secret=credentials.feishu_app_secret,
                timeout_seconds=FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS,
            )
            membership = FeishuSdkMembershipAdapter(
                tenant_id=settings.tenant_id,
                app_id=credentials.feishu_app_id,
                app_secret=credentials.feishu_app_secret,
            )
        except (ValueError, FeishuSdkError):
            # 两个必须同进同退：装配函数收到半套端口会走进"已装配"分支，
            # 然后在第一次真实调用时才炸。
            oauth = None
            membership = None
    # 飞书凭据不可用是**正常状态**，不是配置错误：本地管理员登录始终可用，
    # Web 进程照常起来，页面只是不渲染飞书入口。
    stack = None
    stack_configuration_invalid = False
    try:
        stack = await build_postgres_web_stack(
            settings=settings,
            oauth=oauth,
            membership=membership,
        )
    except WebStackConfigurationError:
        stack_configuration_invalid = True
    if stack_configuration_invalid or stack is None:
        raise _WebConfigurationError

    try:
        # 回执在**开始服务之前**落库：页面第一次打开就必须看到这个进程加载的代次，
        # 而不是"还没人来问过所以显示待应用"。
        await stack.provider_state.record_load(receipts=receipts)
        app = create_app(
            auth=stack.auth,
            local_admin_auth=stack.local_admin_auth,
            oauth_available=stack.oauth_available,
            settings=settings,
            readiness=stack.readiness,
            task_access=stack.task_access_service,
            submissions=stack.submission_service,
            clock=stack.clock,
            policy_revision=stack.policy_revision,
            provider_state=stack.provider_state,
            integration_config=stack.integration_config_service,
            admin_identity=stack.admin_identity_service,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.web_bind_host,
                port=settings.web_bind_port,
                proxy_headers=False,
                access_log=False,
                log_config=None,
            )
        )
        await server.serve()
        return 0
    finally:
        await stack.aclose()


def main() -> int:
    """加载默认关闭的 Web 配置，并用固定退出语义运行唯一装配根。"""
    try:
        settings = load_settings()
    except ConfigError:
        sys.stderr.write("xiaowei-web: configuration_error\n")
        return 2
    # Web 进程的启动门只有一条：``web_app_enabled``。飞书 OAuth 是不是可用由
    # ``serve_web()`` 按飞书域文件决定，不可用只是降级，不是配置错误。
    # 这里多写一个条件，等于把"飞书必须装配"重新钉回真实进程入口。
    if not settings.web_app_enabled:
        return 2
    try:
        return asyncio.run(serve_web(settings))
    except _WebConfigurationError:
        sys.stderr.write("xiaowei-web: configuration_error\n")
        return 2
    except (Exception, SystemExit):
        sys.stderr.write("xiaowei-web: startup_failed\n")
        return 1


if __name__ == "__main__":  # pragma: no cover - 由进程/Compose 调用
    sys.exit(main())


__all__ = [
    "ALL_OAUTH_STATE_COOKIE_NAMES",
    "ALL_SESSION_COOKIE_NAMES",
    "create_app",
    "main",
    "oauth_state_cookie_name",
    "serve_web",
    "session_cookie_name",
]
