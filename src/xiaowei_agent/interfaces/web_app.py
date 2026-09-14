"""默认关闭、无执行权的飞书认证 Web 工作台。"""

import asyncio
import json
import logging
import re
import sys
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
    ChannelSubmissionForbiddenError,
    ChannelSubmissionService,
    ChannelSubmitCommand,
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
    TASK_ID_PATTERN,
    AuthenticatedPrincipal,
    ChannelKind,
    ReadinessProbe,
    WebMode,
)
from xiaowei_agent.interfaces.auth import Clock
from xiaowei_agent.interfaces.body_limit import JsonBodyLimitMiddleware
from xiaowei_agent.interfaces.http_models import error_body
from xiaowei_agent.interfaces.local_admin_auth import (
    LocalAdminAuthenticationError,
    LocalAdminAuthService,
)
from xiaowei_agent.interfaces.web_auth import (
    FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS,
    FeishuOAuthPort,
    WebAuthenticationError,
    WebAuthService,
    WebCsrfError,
    WebOAuthCodeError,
    WebOAuthStateError,
    WebOAuthUnavailableError,
    WebOriginError,
    validate_state_change,
    validate_unauthenticated_origin,
)
from xiaowei_agent.interfaces.web_models import (
    WebChangePasswordRequest,
    WebCurrentUser,
    WebLoginRequest,
    WebTaskAccepted,
    WebTaskDetail,
    WebTaskPage,
    WebTaskSubmitRequest,
)
from xiaowei_agent.log import configure_logging
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


async def _application_error(_: Request, exc: Exception) -> Response:
    failure = classify_application_exception(exc)
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


def _login_shell(*, oauth_available: bool) -> str:
    """未登录时的最小登录壳。

    只含口令表单与同源提交脚本：**不加载**任务列表、配置面板或任何需要会话的
    资源，否则未登录页面自己就会触发一串 401。飞书入口仅在装配成功时渲染，
    避免出现一个点进去必然失败的假入口。
    """
    oauth_entry = (
        '<p><a id="feishu-oauth" href="/oauth/feishu/start">使用飞书登录</a></p>'
        if oauth_available
        else ""
    )
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<title>小维 Agent 登录</title>"
        '<link rel="stylesheet" href="/app/static/app.css"></head><body>'
        "<main><h1>登录</h1>"
        '<form id="login-form" method="post" action="/app/api/login">'
        '<label for="password">口令</label>'
        '<input id="password" name="password" type="password"'
        ' autocomplete="current-password" required>'
        '<button type="submit">登录</button></form>'
        '<p class="form-message is-hidden" id="form-message" role="alert"></p>'
        f"{oauth_entry}"
        '<script type="module" src="/app/static/login.js"></script>'
        "</main></body></html>"
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
        '<form id="change-password-form" method="post" action="/app/api/change-password">'
        '<label for="current-password">当前口令</label>'
        '<input id="current-password" name="current_password" type="password" required>'
        '<label for="new-password">新口令</label>'
        '<input id="new-password" name="new_password" type="password" required>'
        '<button type="submit">提交</button></form>'
        '<p class="form-message is-hidden" id="form-message" role="alert"></p>'
        '<script type="module" src="/app/static/login.js"></script>'
        "</main></body></html>"
    )


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


@dataclass(frozen=True)
class _WebPrincipalSession:
    """两条认证路径的统一结果。

    本地管理员与飞书 OAuth 写的是同一张 ``web_sessions`` 表、用的是同一个
    cookie 名和同一套 digest 域，路由层因此只需要一种会话形状。
    ``must_change_password`` 只有本地管理员可能为真。
    """

    cookie: str = field(repr=False)
    principal: AuthenticatedPrincipal
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
            principal=local.principal,
            csrf_token=local.csrf_token,
            must_change_password=local.must_change_password,
        )
    if auth is None:
        raise WebAuthenticationError
    session = await auth.authenticate(session_cookie=cookie)
    return _WebPrincipalSession(
        cookie=cookie,
        principal=session.principal,
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
        # 每一个 JSON 写入口都必须在这里，否则它没有 body 大小上限。
        # 三个 config/test 子路径逐条列全：中间件是精确匹配，不做前缀。
        paths=frozenset(
            {
                "/app/api/logout",
                "/app/api/tasks",
                "/app/api/login",
                "/app/api/change-password",
                "/app/api/config",
                "/app/api/config/clear",
                "/app/api/config/test/gemini_connection",
                "/app/api/config/test/feishu_credentials",
                "/app/api/config/test/feishu_oauth",
            }
        ),
    )
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(WebAuthenticationError, _authentication_error)
    app.add_exception_handler(LocalAdminAuthenticationError, _authentication_error)
    app.add_exception_handler(WebOAuthStateError, _oauth_input_error)
    app.add_exception_handler(WebOAuthCodeError, _oauth_input_error)
    app.add_exception_handler(WebOAuthUnavailableError, _oauth_unavailable)
    app.add_exception_handler(
        _PasswordChangeRequiredError, _password_change_required
    )
    app.add_exception_handler(WebOriginError, _forbidden)
    app.add_exception_handler(WebCsrfError, _forbidden)
    app.add_exception_handler(ChannelSubmissionForbiddenError, _forbidden)
    app.add_exception_handler(TaskAccessNotFoundError, _task_not_found)
    app.add_exception_handler(
        TaskAccessSnapshotUnavailableError, _task_snapshot_unavailable
    )
    app.add_exception_handler(_WebInputError, _input_error)
    app.add_exception_handler(Exception, _application_error)

    if auth is not None:
        @app.get("/oauth/feishu/start")
        async def oauth_start() -> Response:
            started = await auth.start_login()
            response = RedirectResponse(started.authorization_url, status_code=302)
            _set_secret_cookie(
                response,
                name=oauth_state_cookie,
                value=started.state_cookie,
                max_age=started.max_age_seconds,
                secure=secure_cookies,
            )
            return response

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
            except WebAuthenticationError:
                response = _error(401, "unauthorized")
            except (WebOAuthStateError, WebOAuthCodeError):
                response = _error(400, "invalid_request")
            except WebOAuthUnavailableError:
                response = _error(503, "unavailable")
            else:
                response = RedirectResponse("/app", status_code=302)
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

    @app.get("/app")
    async def shell(request: Request) -> Response:
        try:
            session = await session_of(request)
        except (WebAuthenticationError, LocalAdminAuthenticationError):
            # 未登录必须拿到登录壳：否则本地管理员没有任何入口能取到表单。
            return HTMLResponse(_login_shell(oauth_available=oauth_available))
        if session.must_change_password:
            return HTMLResponse(
                _password_change_shell(csrf_token=session.csrf_token)
            )
        return HTMLResponse(index_shell)

    @app.get("/app/tasks/{task_id}")
    async def detail_shell_route(request: Request, task_id: str) -> Response:
        await session_allowed_to_work(request)
        _task_id_or_not_found(task_id)
        return HTMLResponse(detail_shell)

    @app.get("/app/api/me")
    async def current_user(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        return WebCurrentUser.from_principal(
            session.principal, csrf_token=session.csrf_token
        ).model_dump(mode="json")

    @app.get("/app/api/tasks")
    async def list_tasks(request: Request) -> dict[str, object]:
        session = await session_allowed_to_work(request)
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
        session = await session_allowed_to_work(request)
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
                parent_task_id=body.parent_task_id,
            )
        )
        accepted = WebTaskAccepted.from_submission(submitted)
        exclude = {"parent_task_id"} if accepted.parent_task_id is None else set()
        return accepted.model_dump(mode="json", exclude=exclude)

    @app.get("/app/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> dict[str, object]:
        session = await session_allowed_to_work(request)
        accessible = await task_access.get_task(
            query=TaskAccessQuery(
                principal=session.principal,
                task_id=_task_id_or_not_found(task_id),
            )
        )
        detail = WebTaskDetail.from_accessible(accessible)
        exclude = {"parent_task_id"} if detail.parent_task_id is None else set()
        return detail.model_dump(mode="json", exclude=exclude)

    @app.post("/app/api/login")
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
            password=body.password,
            previous_session_cookie=previous if unambiguous else None,
        )
        response = JSONResponse(content={"status": "ok"})
        _set_secret_cookie(
            response,
            name=session_cookie,
            value=issued.session_cookie,
            max_age=issued.max_age_seconds,
            secure=secure_cookies,
        )
        return response

    @app.post("/app/api/change-password")
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
        )
        response = JSONResponse(content={"status": "ok"})
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

    credentials, _ = load_provider_credentials(settings=settings)  # 回执见 Task 6
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
