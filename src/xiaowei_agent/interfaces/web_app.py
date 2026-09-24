"""默认关闭、无执行权的飞书认证 Web 工作台。"""

import asyncio
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Annotated, Final, TypeVar, cast
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
from xiaowei_agent.application.integration_state import (
    SERVICE_WEB,
    ProviderDisplayState,
    compute_display_state,
    current_generation,
)
from xiaowei_agent.application.task_view_runtime import (
    ApplicationFailure,
    classify_application_exception,
)
from xiaowei_agent.config import (
    ConfigError,
    Settings,
    canonical_non_ip_hostname,
    canonical_web_public_origin,
    load_settings,
)
from xiaowei_agent.contracts import (
    LOCAL_ADMIN_USER_ID,
    TASK_ID_PATTERN,
    AdminCapability,
    AuthenticatedPrincipal,
    ChannelKind,
    ChannelPermission,
    FeishuIntegration,
    GeminiIntegration,
    IdentitySource,
    IntegrationConfig,
    ProductRole,
    ProviderName,
    ReadinessProbe,
    TestResult,
    WebMode,
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.interfaces.auth import Clock
from xiaowei_agent.interfaces.body_limit import JsonBodyLimitMiddleware
from xiaowei_agent.interfaces.http_models import error_body
from xiaowei_agent.interfaces.integration_config_file import (
    DEFAULT_INTEGRATION_CONFIG_PATH,
    IntegrationConfigError,
    write_integration_config,
)
from xiaowei_agent.interfaces.local_admin_auth import (
    LocalAdminAuthenticationError,
    LocalAdminAuthService,
)
from xiaowei_agent.interfaces.provider_consumption import (
    read_or_absent,
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
    WebChangePasswordRequest,
    WebConfigChecks,
    WebConfigClearRequest,
    WebConfigSaved,
    WebConfigUpdateRequest,
    WebConfigView,
    WebCurrentUser,
    WebFeishuConfigUpdate,
    WebFeishuConfigView,
    WebGeminiConfigUpdate,
    WebGeminiConfigView,
    WebIntegrationDomain,
    WebIntegrationDomainStatus,
    WebIntegrationLoadStatus,
    WebIntegrationStatusView,
    WebLoginRequest,
    WebOAuthTestStarted,
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
    RecordTestCommand,
)
from xiaowei_agent.trace import bind_trace_id, get_trace_id

_BodyT = TypeVar("_BodyT", bound=BaseModel)

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


def _login_shell(*, shell: str, oauth_available: bool) -> str:
    """按装配事实裁掉不可用的 OAuth 入口，不生成第二套登录页面。"""
    if oauth_available:
        return shell
    return shell.replace(
        '<div class="oauth-entry" id="oauth-entry">',
        '<div class="oauth-entry is-hidden" id="oauth-entry">',
        1,
    )


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


class _ConfigUnavailableError(RuntimeError):
    """`integrations.json` 存在但读不了。

    **绝不能**降级成"未配置"：那样下一次保存会拿 ``generation=0`` 起步，
    把一份仍在被各进程使用的配置整个覆盖掉。"不存在"是未配置，"存在但坏"是故障。
    """


def _integration_config_or_unavailable(path: str) -> IntegrationConfig | None:
    """读当前配置；``None`` 表示文件尚不存在（干净部署的正常起点）。"""
    try:
        return read_or_absent(path)
    except IntegrationConfigError:
        raise _ConfigUnavailableError from None


def _write_config_or_unavailable(path: str, config: IntegrationConfig) -> None:
    try:
        write_integration_config(path, config)
    except IntegrationConfigError:
        raise _ConfigUnavailableError from None


def _merged_gemini(
    current: GeminiIntegration, update: WebGeminiConfigUpdate | None
) -> GeminiIntegration:
    """未携带的字段保留原值。

    ``None`` 在这里只可能是"未携带"：显式 ``null`` 与空串都已在请求模型里被拒。
    """
    if update is None:
        return current
    return GeminiIntegration(
        enabled=current.enabled if update.enabled is None else update.enabled,
        api_key=current.api_key if update.api_key is None else update.api_key,
    )


def _merged_feishu(
    current: FeishuIntegration, update: WebFeishuConfigUpdate | None
) -> FeishuIntegration:
    if update is None:
        return current
    return FeishuIntegration(
        enabled=current.enabled if update.enabled is None else update.enabled,
        app_id=current.app_id if update.app_id is None else update.app_id,
        app_secret=(
            current.app_secret if update.app_secret is None else update.app_secret
        ),
    )


def _next_config(
    current: IntegrationConfig | None,
    *,
    gemini: GeminiIntegration,
    feishu: FeishuIntegration,
) -> IntegrationConfig:
    """代次自增。文件不存在时从逻辑代次 ``0`` 起步，第一次保存写 ``1``。"""
    return IntegrationConfig(
        generation=current_generation(current) + 1, gemini=gemini, feishu=feishu
    )


def _required_fields_present(
    provider: ProviderName, config: IntegrationConfig | None
) -> bool:
    """该 Provider 的必填字段是否齐备。

    **只有这一处判定**：页面上的"已配置"与状态机的 ``required_fields_present``
    问的是同一个问题。各写一份会出现"显示已配置、状态却是未配置"这种自相矛盾的页面——
    飞书尤其容易，它有两个必填字段，只看 secret 就会把"填了 secret 没填 App ID"
    算成已配置。
    """
    if config is None:
        return False
    if provider is ProviderName.GEMINI:
        return config.gemini.api_key is not None
    return config.feishu.app_id is not None and config.feishu.app_secret is not None


def _provider_of(check_name: CheckName) -> ProviderName:
    """测试项归属的 Provider；两个飞书测试项共用同一份凭据与加载回执。"""
    if check_name is CheckName.GEMINI_CONNECTION:
        return ProviderName.GEMINI
    return ProviderName.FEISHU


def _provider_enabled(
    provider: ProviderName, config: IntegrationConfig | None
) -> bool:
    if config is None:
        return False
    if provider is ProviderName.GEMINI:
        return config.gemini.enabled
    return config.feishu.enabled


def _display_state(
    *,
    check_name: CheckName,
    settings: Settings,
    config: IntegrationConfig | None,
    snapshot: ProviderStateSnapshot,
) -> ProviderDisplayState:
    """把一份配置与一份持久化事实折成某个测试项的页面状态。"""
    provider = _provider_of(check_name)
    return compute_display_state(
        provider=provider,
        enabled=_provider_enabled(provider, config),
        required_fields_present=_required_fields_present(provider, config),
        current_generation=current_generation(config),
        required_service_names=required_services_for_check(
            settings, check_name.value
        ),
        receipts=snapshot.receipts,
        test=snapshot.tests.get(check_name.value),
    )


def _config_view(
    *,
    settings: Settings,
    config: IntegrationConfig | None,
    snapshot: ProviderStateSnapshot,
) -> WebConfigView:
    """查询投影：``configured`` 是布尔，两个 secret 永不出现在响应里。"""
    return WebConfigView(
        generation=current_generation(config),
        gemini=WebGeminiConfigView(
            enabled=_provider_enabled(ProviderName.GEMINI, config),
            configured=_required_fields_present(ProviderName.GEMINI, config),
        ),
        feishu=WebFeishuConfigView(
            enabled=_provider_enabled(ProviderName.FEISHU, config),
            configured=_required_fields_present(ProviderName.FEISHU, config),
            app_id=None if config is None else config.feishu.app_id,
        ),
        checks=WebConfigChecks(
            **{
                check.value: _display_state(
                    check_name=check,
                    settings=settings,
                    config=config,
                    snapshot=snapshot,
                )
                for check in CheckName
            }
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
    domain: WebIntegrationDomain,
    provider: ProviderName,
    checks: tuple[CheckName, ...],
    settings: Settings,
    config: IntegrationConfig | None,
    snapshot: ProviderStateSnapshot,
) -> WebIntegrationDomainStatus:
    """从现有配置/回执/测试事实派生一个不含原值的管理面域状态。"""
    configured = _provider_enabled(provider, config) and _required_fields_present(
        provider, config
    )
    generation = current_generation(config)
    required_services = frozenset().union(
        *(required_services_for_check(settings, check.value) for check in checks)
    )
    restart_required = False
    load_status: WebIntegrationLoadStatus = "unconfigured"
    if configured:
        receipts = [
            snapshot.receipts.get((service_name, provider.value))
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
        domain=domain,
        configured=configured,
        restart_required=restart_required,
        load_status=load_status,
        last_test_status=None if latest is None else latest.status,
        last_tested_at=None if latest is None else latest.tested_at,
    )


def _integration_status_view(
    *,
    settings: Settings,
    config: IntegrationConfig | None,
    snapshot: ProviderStateSnapshot,
) -> WebIntegrationStatusView:
    """三域脱敏状态；resources 未交付，永远明确标为 not_applicable。"""
    return WebIntegrationStatusView(
        domains=(
            _integration_domain_status(
                domain="ai",
                provider=ProviderName.GEMINI,
                checks=(CheckName.GEMINI_CONNECTION,),
                settings=settings,
                config=config,
                snapshot=snapshot,
            ),
            _integration_domain_status(
                domain="feishu",
                provider=ProviderName.FEISHU,
                checks=(CheckName.FEISHU_CREDENTIALS, CheckName.FEISHU_OAUTH),
                settings=settings,
                config=config,
                snapshot=snapshot,
            ),
            WebIntegrationDomainStatus(
                domain="resources",
                configured=False,
                restart_required=False,
                load_status="not_applicable",
            ),
        )
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


def _usable_config(
    provider: ProviderName, config: IntegrationConfig | None
) -> IntegrationConfig | None:
    """两层与关系都为真才交出配置，否则探针看到的就是"未配置"。

    `.env` 的真实测试开关与这里**不是一回事**：前者回答"这台机器允许发真实请求
    吗"，后者回答"有没有东西可以拿去发"。合并会让"关着开关"与"没填 Key"变成
    同一个错误码，管理员看不出该去开开关还是该去填配置。
    """
    if not (
        _provider_enabled(provider, config)
        and _required_fields_present(provider, config)
    ):
        return None
    return config


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
    integration_config_path: str = DEFAULT_INTEGRATION_CONFIG_PATH,
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
                "/admin/api/config",
                "/admin/api/config/clear",
                "/admin/api/config/test/gemini_connection",
                "/admin/api/config/test/feishu_credentials",
                "/admin/api/config/test/feishu_oauth",
            }
        ),
    )
    # 一个 Web 进程内串行执行"读当前代次 → 合并 → 原子替换"。本版不支持多写实例，
    # 因此这把锁就是全部并发控制；它必须随 app 走，不能是模块级的。
    config_lock = asyncio.Lock()
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
    app.add_exception_handler(_ConfigUnavailableError, _config_unavailable)
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
            """OAuth 连接测试的回调分支：只记一条结果，**不签发任何会话**。

            顺序是承重的：先消费测试域 state，再核对本地管理员 session，最后才换
            code。反过来先查 session，会把"登录 state 过期"这种常见情况误报成
            403——那时用户会去找权限问题，而真正的原因是重新点一次登录就好。
            """
            try:
                await auth.consume_connection_test_state(
                    state=state, state_cookie=state_cookie
                )
            except WebOAuthStateError:
                return _error(400, "invalid_request")
            try:
                await local_admin_session(request)
            except (WebAuthenticationError, LocalAdminAuthenticationError):
                # session 已过期或被撤销：测试不再有主体可归属，什么都不写。
                return _error(401, "unauthorized")
            except (_WebForbiddenError, _PasswordChangeRequiredError):
                return _error(403, "forbidden")
            config = _integration_config_or_unavailable(integration_config_path)
            started = time.monotonic()
            outcome: ProbeOutcome
            try:
                await auth.complete_connection_test(code=code)
            except (WebOAuthCodeError, WebAuthenticationError):
                outcome = _probe_failure(
                    ProbeErrorCode.UNAUTHORIZED, started=started
                )
            except WebOAuthUnavailableError:
                outcome = _probe_failure(
                    ProbeErrorCode.UNAVAILABLE, started=started
                )
            else:
                outcome = ProbeOutcome(
                    status="passed", duration_ms=_probe_duration_ms(started)
                )
            await record_probe(
                outcome, check_name=CheckName.FEISHU_OAUTH, config=config
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

    async def safe_task_session(request: Request) -> _WebPrincipalSession:
        session = await session_allowed_to_work(request)
        if ChannelPermission.VIEW_SAFE_TASK not in session.principal.permissions:
            raise _WebForbiddenError
        return session

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
        try:
            await workbench_session(request)
        except (WebAuthenticationError, LocalAdminAuthenticationError):
            return _login_redirect(
                WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH)
            )
        return HTMLResponse(index_shell)

    @app.get("/admin")
    async def admin_shell_route(request: Request) -> Response:
        try:
            await admin_session(request)
        except (WebAuthenticationError, LocalAdminAuthenticationError):
            return _login_redirect(
                WebReturnIntent(kind=WebReturnIntentKind.ADMIN_CENTER)
            )
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

    @app.get("/admin/api/integration-status")
    async def read_integration_status(request: Request) -> dict[str, object]:
        await admin_session(request)
        config = _integration_config_or_unavailable(integration_config_path)
        snapshot = await provider_state.snapshot()
        return _integration_status_view(
            settings=settings,
            config=config,
            snapshot=snapshot,
        ).model_dump(mode="json")

    @app.get("/admin/api/config")
    async def read_config(request: Request) -> dict[str, object]:
        await local_admin_session(request)
        config = _integration_config_or_unavailable(integration_config_path)
        snapshot = await provider_state.snapshot()
        return _config_view(
            settings=settings, config=config, snapshot=snapshot
        ).model_dump(mode="json")

    @app.put("/admin/api/config")
    async def save_config(request: Request) -> dict[str, object]:
        session = await local_admin_session(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebConfigUpdateRequest)
        async with config_lock:
            current = _integration_config_or_unavailable(integration_config_path)
            updated = _next_config(
                current,
                gemini=_merged_gemini(
                    GeminiIntegration() if current is None else current.gemini,
                    body.gemini,
                ),
                feishu=_merged_feishu(
                    FeishuIntegration() if current is None else current.feishu,
                    body.feishu,
                ),
            )
            _write_config_or_unavailable(integration_config_path, updated)
        return WebConfigSaved(
            generation=updated.generation, restart_required=True
        ).model_dump(mode="json")

    @app.post("/admin/api/config/clear")
    async def clear_config(request: Request) -> dict[str, object]:
        session = await local_admin_session(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        body = await _typed_body(request, WebConfigClearRequest)
        async with config_lock:
            current = _integration_config_or_unavailable(integration_config_path)
            gemini = GeminiIntegration() if current is None else current.gemini
            feishu = FeishuIntegration() if current is None else current.feishu
            # 清除是整段重置为默认值，不是把某个字段置空：留一半配置在文件里，
            # 页面会显示"已配置"而实际不可用。
            if body.provider is ProviderName.GEMINI:
                gemini = GeminiIntegration()
            else:
                feishu = FeishuIntegration()
            updated = _next_config(current, gemini=gemini, feishu=feishu)
            _write_config_or_unavailable(integration_config_path, updated)
        return WebConfigSaved(
            generation=updated.generation, restart_required=True
        ).model_dump(mode="json")

    async def record_probe(
        outcome: ProbeOutcome,
        *,
        check_name: CheckName,
        config: IntegrationConfig | None,
    ) -> None:
        """有可信代次才落库。

        文件不存在时没有代次可归属，而 ``RecordTestCommand.generation`` 恒 ``> 0``；
        硬凑一个值等于伪造证据。不落库的后果只是页面继续显示"未配置"——它本来就是。
        """
        if config is None:
            return
        await provider_state.record_test(
            command=RecordTestCommand(
                check_name=check_name,
                generation=config.generation,
                status=outcome.status,
                duration_ms=outcome.duration_ms,
                error_code=outcome.error_code,
            )
        )

    def oauth_test_refusal(
        config: IntegrationConfig | None, *, snapshot: ProviderStateSnapshot
    ) -> ProbeOutcome | None:
        """OAuth 测试的三道前置；``None`` 表示可以签发 state。

        最后一道——凭据测试必须在**当前代次**通过——不是礼貌，是必需：OAuth 回调
        链路建立在应用凭据之上，凭据本身没验过时跳出去的失败无法区分"回调地址配
        错了"与"密钥根本不对"，管理员会照着错误的方向改半天。
        """
        if not settings.feishu_real_test_enabled:
            return refused_outcome(ProbeErrorCode.REAL_TEST_DISABLED)
        if auth is None or _usable_config(ProviderName.FEISHU, config) is None:
            return refused_outcome(ProbeErrorCode.NOT_CONFIGURED)
        credentials = snapshot.tests.get(CheckName.FEISHU_CREDENTIALS.value)
        if (
            credentials is None
            or credentials.status != "passed"
            or credentials.generation != current_generation(config)
        ):
            return refused_outcome(ProbeErrorCode.NOT_CONFIGURED)
        return None

    # 字面量路由必须注册在带路径参数的那条**之前**：Starlette 按注册顺序匹配。
    # 顺序之外还有一道兜底——下面那条路由显式拒绝 ``feishu_oauth``，因此即使有人
    # 调换了顺序，也不会静默落进"用凭据探针去测 OAuth"的错误分支。
    @app.post("/admin/api/config/test/feishu_oauth", response_model=None)
    async def start_oauth_test(request: Request) -> Response:
        """签发一次只能被测试分支消费的 state，并返回授权 URL。

        ``auth is None`` 时这条路由仍然注册——它与两条 OAuth 入口不同，返回的是
        一个闭集拒绝码而不是一次永远失败的跳转。管理员需要知道"没装配"，而不是
        点下去毫无反应。
        """
        session = await local_admin_session(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        config = _integration_config_or_unavailable(integration_config_path)
        refusal = oauth_test_refusal(config, snapshot=await provider_state.snapshot())
        if refusal is not None:
            await record_probe(
                refusal, check_name=CheckName.FEISHU_OAUTH, config=config
            )
            return JSONResponse(content=refusal.model_dump(mode="json"))
        started = await cast(WebAuthService, auth).start_connection_test()
        response = JSONResponse(
            content=WebOAuthTestStarted(
                authorization_url=started.authorization_url
            ).model_dump(mode="json")
        )
        _set_secret_cookie(
            response,
            name=oauth_state_cookie,
            value=started.state_cookie,
            max_age=started.max_age_seconds,
            secure=secure_cookies,
        )
        return response

    @app.post("/admin/api/config/test/{check_name}")
    async def run_provider_test(
        request: Request, check_name: str
    ) -> dict[str, object]:
        session = await local_admin_session(request)
        _validate_state_change(
            request, public_origin=public_origin, session_cookie=session.cookie
        )
        check = _check_name_or_input_error(check_name)
        if check is CheckName.FEISHU_OAUTH:
            # 走到这里说明上面那条字面量路由被移到了后面。宁可 400，也不能用
            # 凭据探针去回答一个 OAuth 问题——那会写下一条名不副实的测试结果。
            raise _WebInputError
        config = _integration_config_or_unavailable(integration_config_path)
        if check is CheckName.GEMINI_CONNECTION:
            usable = _usable_config(ProviderName.GEMINI, config)
            outcome = await probe_gemini_connection(
                enabled=settings.gemini_real_test_enabled,
                api_key=None if usable is None else usable.gemini.api_key,
                transport=gemini_probe,
                timeout_seconds=PROVIDER_PROBE_TIMEOUT_SECONDS,
            )
        else:
            usable = _usable_config(ProviderName.FEISHU, config)
            outcome = await probe_feishu_credentials(
                enabled=settings.feishu_real_test_enabled,
                app_id=None if usable is None else usable.feishu.app_id,
                app_secret=None if usable is None else usable.feishu.app_secret,
                transport=feishu_probe,
                timeout_seconds=PROVIDER_PROBE_TIMEOUT_SECONDS,
            )
        await record_probe(outcome, check_name=check, config=config)
        return outcome.model_dump(mode="json")

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

    这里是 composition root：读 `integrations.json`、判双层开关、构造或**不构造**
    真实飞书 adapter。装配函数只收端口，不读配置。
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
    # ``serve_web()`` 按 `integrations.json` 决定，不可用只是降级，不是配置错误。
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
