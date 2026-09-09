"""默认关闭、无执行权的飞书认证 Web app。"""

import json
import sys
from typing import Annotated, Final

from fastapi import FastAPI, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.contracts import ReadinessProbe
from xiaowei_agent.interfaces.body_limit import JsonBodyLimitMiddleware
from xiaowei_agent.interfaces.http_models import error_body
from xiaowei_agent.interfaces.web_auth import (
    WebAuthenticationError,
    WebAuthService,
    WebCsrfError,
    WebOAuthCodeError,
    WebOAuthStateError,
    WebOAuthUnavailableError,
    WebOriginError,
)
from xiaowei_agent.log import configure_logging

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
_SHELL: Final[str] = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>小维 · 运维任务工作台</title></head>
<body><main><h1>小维 · 运维任务工作台</h1><p>认证已完成。</p></main></body>
</html>"""


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


async def _internal_error(_: Request, __: Exception) -> Response:
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


def create_app(
    *,
    auth: WebAuthService,
    settings: Settings,
    readiness: ReadinessProbe,
) -> FastAPI:
    """只注册 PR 6 认证路由；任务业务路由留给 PR 7。"""
    if not settings.web_app_enabled:
        raise ValueError("Web app is disabled")
    app = FastAPI(
        redirect_slashes=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        JsonBodyLimitMiddleware,
        limit=settings.api_request_body_limit_bytes,
        paths=frozenset({"/app/api/logout"}),
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
    app.add_exception_handler(Exception, _internal_error)

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
        session_cookie, _ = _single_cookie(request, name=SESSION_COOKIE_NAME)
        await auth.authenticate(session_cookie=session_cookie)
        return HTMLResponse(_SHELL)

    @app.get("/app/api/me")
    async def current_user(request: Request) -> dict[str, object]:
        session_cookie, _ = _single_cookie(request, name=SESSION_COOKIE_NAME)
        session = await auth.authenticate(session_cookie=session_cookie)
        return {
            "actor": session.principal.actor,
            "environment_id": session.principal.environment_id,
            "permissions": sorted(
                permission.value for permission in session.principal.permissions
            ),
            "csrf_token": session.csrf_token,
        }

    @app.post("/app/api/logout", status_code=204)
    async def logout(request: Request) -> Response:
        cookie, _ = _single_cookie(request, name=SESSION_COOKIE_NAME)
        await auth.authenticate(session_cookie=cookie)
        if not _is_json_request(request):
            return _error(415, "unsupported_media_type")
        try:
            payload = json.loads(await request.body())
        except (ValueError, UnicodeError):
            return _error(400, "invalid_request")
        if payload != {}:
            return _error(400, "invalid_request")
        origins = request.headers.getlist("origin")
        csrf_tokens = request.headers.getlist(_CSRF_HEADER)
        auth.validate_state_change(
            session_cookie=cookie or "",
            origin=origins[0] if len(origins) == 1 else None,
            csrf_token=csrf_tokens[0] if len(csrf_tokens) == 1 else None,
        )
        await auth.logout(session_cookie=cookie)
        response = Response(status_code=204)
        _clear_secret_cookie(response, name=SESSION_COOKIE_NAME)
        return response

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
