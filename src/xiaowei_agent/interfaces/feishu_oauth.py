"""飞书 authen-v1 OAuth 的标准库、失败关闭 adapter。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from xiaowei_agent.interfaces.secret_file import _read_secret_file, _SecretFileError
from xiaowei_agent.interfaces.web_auth import (
    FeishuOAuthCodeError,
    FeishuOAuthIdentity,
    FeishuOAuthPort,
    FeishuOAuthUnavailableError,
)
from xiaowei_agent.trace import get_trace_id

__all__ = ["FeishuOAuthAdapter"]

_AUTHORIZATION_URL: Final[str] = "https://open.feishu.cn/open-apis/authen/v1/index"
_APP_TOKEN_URL: Final[str] = (
    "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"  # noqa: S105
)
_USER_TOKEN_URL: Final[str] = (
    "https://open.feishu.cn/open-apis/authen/v1/access_token"  # noqa: S105
)
_MAX_RESPONSE_BYTES: Final[int] = 32_768
_MAX_PROVIDER_STRING_BYTES: Final[int] = 8192
_MAX_CALLBACK_BYTES: Final[int] = 2048
_JSON_CONTENT_TYPE: Final[str] = "application/json; charset=utf-8"
_STATE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{16,512}")

_USER_STRING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "token_type",
        "name",
        "en_name",
        "avatar_url",
        "avatar_thumb",
        "avatar_middle",
        "avatar_big",
        "open_id",
        "union_id",
        "email",
        "enterprise_email",
        "user_id",
        "mobile",
        "tenant_key",
        "refresh_token",
        "sid",
    }
)
_USER_INT_FIELDS: Final[frozenset[str]] = frozenset(
    {"expires_in", "refresh_expires_in"}
)
_USER_FIELDS: Final[frozenset[str]] = _USER_STRING_FIELDS | _USER_INT_FIELDS


@dataclass(frozen=True)
class _HttpResponse:
    status: int
    body: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _post_json(
    *,
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, str],
    timeout_seconds: float,
) -> _HttpResponse:
    request = Request(  # noqa: S310 -- callers pass only fixed official HTTPS URLs
        url,
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers=dict(headers),
        method="POST",
    )
    with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
        return _HttpResponse(
            status=int(response.status),
            body=response.read(_MAX_RESPONSE_BYTES + 1),
        )


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _bounded_string(value: object, *, max_bytes: int = _MAX_PROVIDER_STRING_BYTES) -> bool:
    if not isinstance(value, str) or _has_control(value):
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeEncodeError:
        return False


def _callback_is_valid(value: str) -> bool:
    if not _bounded_string(value, max_bytes=_MAX_CALLBACK_BYTES):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/oauth/feishu/callback"
        and not parsed.query
        and not parsed.fragment
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate json key")
        result[key] = value
    return result


def _decode_object(response: _HttpResponse) -> dict[str, object] | None:
    if (
        type(response.status) is not int
        or response.status != 200
        or not isinstance(response.body, bytes)
        or not response.body
        or len(response.body) > _MAX_RESPONSE_BYTES
    ):
        return None
    try:
        decoded = response.body.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _valid_code_and_message(payload: dict[str, object]) -> bool:
    return type(payload.get("code")) is int and _bounded_string(payload.get("msg"))


def _parse_app_token(response: _HttpResponse) -> str | None:
    payload = _decode_object(response)
    if payload is None or not _valid_code_and_message(payload):
        return None
    if payload["code"] != 0:
        return "" if set(payload) == {"code", "msg"} else None
    if set(payload) != {"code", "msg", "app_access_token", "expire"}:
        return None
    token = payload["app_access_token"]
    expire = payload["expire"]
    if (
        not isinstance(token, str)
        or not _bounded_string(token)
        or not token
        or token != token.strip()
        or type(expire) is not int
        or expire <= 0
    ):
        return None
    return token


def _parse_identity(response: _HttpResponse) -> tuple[bool, str | None]:
    payload = _decode_object(response)
    if payload is None or not _valid_code_and_message(payload):
        return False, None
    if payload["code"] != 0:
        return (True, None) if set(payload) == {"code", "msg"} else (False, None)
    if set(payload) != {"code", "msg", "data"} or not isinstance(
        payload["data"], dict
    ):
        return False, None
    data = payload["data"]
    if not {"access_token", "open_id"} <= set(data) or not set(data) <= _USER_FIELDS:
        return False, None
    for name, value in data.items():
        if name in _USER_STRING_FIELDS:
            if not _bounded_string(value):
                return False, None
        elif type(value) is not int:
            return False, None
    access_token = data["access_token"]
    open_id = data["open_id"]
    if (
        not isinstance(access_token, str)
        or not access_token
        or access_token != access_token.strip()
        or not isinstance(open_id, str)
        or not open_id
        or open_id != open_id.strip()
        or len(open_id) > 256
    ):
        return False, None
    return True, open_id


class FeishuOAuthAdapter(FeishuOAuthPort):
    """把两个固定飞书端点收窄为一次 ``open_id`` 身份事实。"""

    def __init__(
        self, *, app_id: str, app_secret_file: str, timeout_seconds: float
    ) -> None:
        if (
            not _bounded_string(app_id, max_bytes=256)
            or not app_id
            or app_id != app_id.strip()
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 5.0
        ):
            raise ValueError("feishu oauth configuration invalid")
        secret_failed = False
        try:
            _read_secret_file(app_secret_file)
        except _SecretFileError:
            secret_failed = True
        if secret_failed:
            raise ValueError("feishu oauth configuration invalid")
        self._app_id = app_id
        self._app_secret_file = app_secret_file
        self._timeout_seconds = float(timeout_seconds)

    def authorization_url(self, *, state: str, redirect_uri: str) -> str:
        if (
            not isinstance(state, str)
            or _STATE_RE.fullmatch(state) is None
            or not _callback_is_valid(redirect_uri)
        ):
            raise FeishuOAuthCodeError
        query = urlencode(
            (
                ("app_id", self._app_id),
                ("redirect_uri", redirect_uri),
                ("state", state),
            )
        )
        return f"{_AUTHORIZATION_URL}?{query}"

    async def _request(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, str],
        deadline: float,
    ) -> _HttpResponse:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FeishuOAuthUnavailableError
        failed = False
        response: _HttpResponse | None = None
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    _post_json,
                    url=url,
                    headers=headers,
                    body=body,
                    timeout_seconds=remaining / 2,
                ),
                timeout=remaining,
            )
        except Exception:
            failed = True
        if failed or not isinstance(response, _HttpResponse):
            raise FeishuOAuthUnavailableError
        return response

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity:
        if get_trace_id() is None:
            raise FeishuOAuthUnavailableError
        if (
            not _bounded_string(code, max_bytes=2048)
            or not code
            or not _callback_is_valid(redirect_uri)
        ):
            raise FeishuOAuthCodeError
        secret_failed = False
        app_secret = ""
        try:
            app_secret = _read_secret_file(self._app_secret_file)
        except _SecretFileError:
            secret_failed = True
        if secret_failed:
            raise FeishuOAuthUnavailableError

        deadline = time.monotonic() + self._timeout_seconds
        app_response = await self._request(
            url=_APP_TOKEN_URL,
            headers={"Content-Type": _JSON_CONTENT_TYPE},
            body={"app_id": self._app_id, "app_secret": app_secret},
            deadline=deadline,
        )
        app_token = _parse_app_token(app_response)
        if app_token is None:
            raise FeishuOAuthUnavailableError
        if not app_token:
            raise FeishuOAuthUnavailableError

        identity_response = await self._request(
            url=_USER_TOKEN_URL,
            headers={
                "Authorization": f"Bearer {app_token}",
                "Content-Type": _JSON_CONTENT_TYPE,
            },
            body={"grant_type": "authorization_code", "code": code},
            deadline=deadline,
        )
        valid, open_id = _parse_identity(identity_response)
        if not valid:
            raise FeishuOAuthUnavailableError
        if open_id is None:
            raise FeishuOAuthCodeError
        return FeishuOAuthIdentity(subject_ref=open_id)
