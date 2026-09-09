"""默认关闭、无执行权的飞书认证 Web 工作台。"""

import json
import re
import sys
from importlib.resources import files
from typing import Annotated, Final

from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xiaowei_agent.application.channel_access import (
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
from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.contracts import ChannelKind, ReadinessProbe
from xiaowei_agent.interfaces.auth import Clock, trusted_trace_id
from xiaowei_agent.interfaces.body_limit import JsonBodyLimitMiddleware
from xiaowei_agent.interfaces.http_models import error_body
from xiaowei_agent.interfaces.web_auth import (
    AuthenticatedWebSession,
    WebAuthenticationError,
    WebAuthService,
    WebCsrfError,
    WebOAuthCodeError,
    WebOAuthStateError,
    WebOAuthUnavailableError,
    WebOriginError,
)
from xiaowei_agent.interfaces.web_models import (
    WebCurrentUser,
    WebTaskAccepted,
    WebTaskDetail,
    WebTaskPage,
    WebTaskSubmitRequest,
)
from xiaowei_agent.log import configure_logging
from xiaowei_agent.trace import bind_trace_id

SESSION_COOKIE_NAME: Final[str] = "__Host-xiaowei-session"
OAUTH_STATE_COOKIE_NAME: Final[str] = "__Host-xiaowei-oauth-state"
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
_TASK_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_POSITIVE_INT_RE: Final[re.Pattern[str]] = re.compile(r"[1-9][0-9]{0,18}")
_MAX_CREATED_SEQ: Final[int] = 9_223_372_036_854_775_807
_STATIC_MEDIA_TYPES: Final[dict[str, str]] = {
    "app.css": "text/css",
    "app.js": "text/javascript",
    "detail.js": "text/javascript",
}


class _WebInputError(ValueError):
    """Web 协议字段形状不合法；异常正文不返回浏览器。"""


class _SecurityHeadersMiddleware:
    """为正常、框架错误和 body 边界响应统一附加浏览器安全头。"""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    (
                        (b"content-security-policy", _CSP.encode("ascii")),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (b"strict-transport-security", _HSTS),
                    )
                )
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_headers)


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
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_secret_cookie(response: Response, *, name: str) -> None:
    response.delete_cookie(
        key=name,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


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


async def _authenticated(
    request: Request, *, auth: WebAuthService
) -> tuple[str, AuthenticatedWebSession]:
    cookie, unambiguous = _single_cookie(request, name=SESSION_COOKIE_NAME)
    if not unambiguous:
        raise WebAuthenticationError
    session = await auth.authenticate(session_cookie=cookie)
    if cookie is None:  # authenticate() 已 fail-closed；保留窄化供类型检查。
        raise WebAuthenticationError
    return cookie, session


def _validate_state_change(
    request: Request, *, auth: WebAuthService, session_cookie: str
) -> None:
    origins = request.headers.getlist("origin")
    csrf_tokens = request.headers.getlist(_CSRF_HEADER)
    auth.validate_state_change(
        session_cookie=session_cookie,
        origin=origins[0] if len(origins) == 1 else None,
        csrf_token=csrf_tokens[0] if len(csrf_tokens) == 1 else None,
    )


def create_app(
    *,
    auth: WebAuthService,
    settings: Settings,
    readiness: ReadinessProbe,
    task_access: TaskAccessService,
    submissions: ChannelSubmissionService,
    clock: Clock,
    policy_revision: str,
) -> FastAPI:
    """注册认证与薄任务投影路由，不装配执行 Runtime。"""
    if not settings.web_app_enabled:
        raise ValueError("Web app is disabled")
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
        paths=frozenset({"/app/api/logout", "/app/api/tasks"}),
    )
    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(WebAuthenticationError, _authentication_error)
    app.add_exception_handler(WebOAuthStateError, _oauth_input_error)
    app.add_exception_handler(WebOAuthCodeError, _oauth_input_error)
    app.add_exception_handler(WebOAuthUnavailableError, _oauth_unavailable)
    app.add_exception_handler(WebOriginError, _forbidden)
    app.add_exception_handler(WebCsrfError, _forbidden)
    app.add_exception_handler(ChannelSubmissionForbiddenError, _forbidden)
    app.add_exception_handler(TaskAccessNotFoundError, _task_not_found)
    app.add_exception_handler(
        TaskAccessSnapshotUnavailableError, _task_snapshot_unavailable
    )
    app.add_exception_handler(_WebInputError, _input_error)
    app.add_exception_handler(Exception, _application_error)

    @app.get("/oauth/feishu/start")
    async def oauth_start() -> Response:
        started = await auth.start_login()
        response = RedirectResponse(started.authorization_url, status_code=302)
        _set_secret_cookie(
            response,
            name=OAUTH_STATE_COOKIE_NAME,
            value=started.state_cookie,
            max_age=started.max_age_seconds,
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
            request, name=OAUTH_STATE_COOKIE_NAME
        )
        previous_session, session_cookie_is_unambiguous = _single_cookie(
            request, name=SESSION_COOKIE_NAME
        )
        if request.query_params.getlist("code") != [code] or request.query_params.getlist(
            "state"
        ) != [state] or not (
            state_cookie_is_unambiguous and session_cookie_is_unambiguous
        ):
            response = _error(400, "invalid_request")
            _clear_secret_cookie(response, name=OAUTH_STATE_COOKIE_NAME)
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
                name=SESSION_COOKIE_NAME,
                value=issued.session_cookie,
                max_age=issued.max_age_seconds,
            )
        _clear_secret_cookie(response, name=OAUTH_STATE_COOKIE_NAME)
        return response

    @app.get("/app")
    async def shell(request: Request) -> Response:
        await _authenticated(request, auth=auth)
        return HTMLResponse(index_shell)

    @app.get("/app/tasks/{task_id}")
    async def detail_shell_route(request: Request, task_id: str) -> Response:
        await _authenticated(request, auth=auth)
        _task_id_or_not_found(task_id)
        return HTMLResponse(detail_shell)

    @app.get("/app/api/me")
    async def current_user(request: Request) -> dict[str, object]:
        _, session = await _authenticated(request, auth=auth)
        return WebCurrentUser.from_session(session).model_dump(mode="json")

    @app.get("/app/api/tasks")
    async def list_tasks(request: Request) -> dict[str, object]:
        _, session = await _authenticated(request, auth=auth)
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
        session_cookie, session = await _authenticated(request, auth=auth)
        _validate_state_change(request, auth=auth, session_cookie=session_cookie)
        body = await _task_submit_body(request)
        trace_id = trusted_trace_id()
        with bind_trace_id(trace_id):
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
                )
            )
        return WebTaskAccepted.from_submission(submitted).model_dump(mode="json")

    @app.get("/app/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> dict[str, object]:
        _, session = await _authenticated(request, auth=auth)
        accessible = await task_access.get_task(
            query=TaskAccessQuery(
                principal=session.principal,
                task_id=_task_id_or_not_found(task_id),
            )
        )
        return WebTaskDetail.from_accessible(accessible).model_dump(mode="json")

    @app.post("/app/api/logout", status_code=204)
    async def logout(request: Request) -> Response:
        cookie, _ = await _authenticated(request, auth=auth)
        if not _is_json_request(request):
            return _error(415, "unsupported_media_type")
        try:
            payload = json.loads(await request.body())
        except (ValueError, UnicodeError):
            return _error(400, "invalid_request")
        if payload != {}:
            return _error(400, "invalid_request")
        _validate_state_change(request, auth=auth, session_cookie=cookie)
        await auth.logout(session_cookie=cookie)
        response = Response(status_code=204)
        _clear_secret_cookie(response, name=SESSION_COOKIE_NAME)
        return response

    @app.get("/app/static/app.css")
    async def app_css() -> Response:
        return Response(static_assets["app.css"], media_type="text/css")

    @app.get("/app/static/app.js")
    async def app_javascript() -> Response:
        return Response(static_assets["app.js"], media_type="text/javascript")

    @app.get("/app/static/detail.js")
    async def detail_javascript() -> Response:
        return Response(static_assets["detail.js"], media_type="text/javascript")

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

    return app


def main() -> int:
    """默认关闭；真实 OAuth adapter 装配由后续激活门承重。"""
    try:
        settings = load_settings()
    except ConfigError:
        sys.stderr.write("xiaowei-web: configuration_error\n")
        return 2
    if not settings.web_app_enabled:
        return 2
    configure_logging(settings)
    # PR 6 只授权 fake OAuth port；不得用占位实现伪装成可运行的真实登录。
    sys.stderr.write("xiaowei-web: oauth_adapter_not_activated\n")
    return 2


if __name__ == "__main__":  # pragma: no cover - 由进程/Compose 调用
    sys.exit(main())


__all__ = [
    "OAUTH_STATE_COOKIE_NAME",
    "SESSION_COOKIE_NAME",
    "create_app",
    "main",
]
