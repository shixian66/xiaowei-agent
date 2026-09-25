"""真实飞书 OAuth adapter 的离线线协议与失败关闭契约。"""

import asyncio
import json
import logging
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

from xiaowei_agent.interfaces import feishu_oauth
from xiaowei_agent.interfaces.feishu_oauth import FeishuOAuthAdapter
from xiaowei_agent.interfaces.web_auth import (
    FeishuOAuthCodeError,
    FeishuOAuthUnavailableError,
)
from xiaowei_agent.trace import bind_trace_id

_FAKE_SECRET = "unit-test-" + "credential"
_FAKE_APP_TOKEN = "app-unit-test-" + "token"
_FAKE_USER_TOKEN = "user-unit-test-" + "token"
_CALLBACK = "https://ops.example.test/oauth/feishu/callback"
_TRACE_ID = "a" * 32
_C1_CONTROLS = ("\u0080", "\u0085", "\u009f")


def _secret_file(tmp_path: Path, value: str = _FAKE_SECRET) -> Path:
    path = tmp_path / "feishu-credential"
    path.write_text(value, encoding="utf-8")
    return path


def _json_response(payload: object, *, status: int = 200):
    return feishu_oauth._HttpResponse(
        status=status,
        body=json.dumps(payload, separators=(",", ":")).encode(),
    )


def _success_responses() -> list[object]:
    return [
        _json_response(
            {
                "code": 0,
                "msg": "ok",
                "app_access_token": _FAKE_APP_TOKEN,
                "expire": 3600,
            }
        ),
        _json_response(
            {
                "code": 0,
                "msg": "ok",
                "data": {
                    "access_token": _FAKE_USER_TOKEN,
                    "token_type": "Bearer",
                    "expires_in": 7200,
                    "name": "Test User",
                    "en_name": "Test User",
                    "avatar_url": "https://example.test/avatar",
                    "avatar_thumb": "https://example.test/thumb",
                    "avatar_middle": "https://example.test/middle",
                    "avatar_big": "https://example.test/big",
                    "open_id": "ou_test_subject",
                    "union_id": "on_test_union",
                    "email": "test@example.test",
                    "enterprise_email": "work@example.test",
                    "user_id": "user-test",
                    "mobile": "10000000000",
                    "tenant_key": "tenant-test",
                    "refresh_expires_in": 86400,
                    "refresh_token": "refresh-test-value",
                    "sid": "sid-test",
                },
            }
        ),
    ]


def _adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[object] | None = None,
    *,
    timeout_seconds: float = 1.0,
) -> tuple[FeishuOAuthAdapter, list[dict[str, object]]]:
    calls: list[dict[str, object]] = []
    queued = list(_success_responses() if responses is None else responses)

    def post_json(**kwargs: object):
        calls.append(kwargs)
        response = queued.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(feishu_oauth, "_post_json", post_json)
    adapter = FeishuOAuthAdapter(
        app_id="cli_test_app",
        app_secret=_FAKE_SECRET,
        timeout_seconds=timeout_seconds,
    )
    return adapter, calls


def test_authorization_url_is_the_exact_official_v1_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _ = _adapter(tmp_path, monkeypatch)

    url = adapter.authorization_url(
        state="state_value_1234567890", redirect_uri=_CALLBACK
    )

    parsed = urlsplit(url)
    assert (
        parsed.scheme,
        parsed.netloc,
        parsed.hostname,
        parsed.port,
        parsed.username,
        parsed.password,
        parsed.path,
        parsed.fragment,
    ) == (
        "https",
        "open.feishu.cn",
        "open.feishu.cn",
        None,
        None,
        None,
        "/open-apis/authen/v1/index",
        "",
    )
    assert parse_qsl(parsed.query, keep_blank_values=True) == [
        ("app_id", "cli_test_app"),
        ("redirect_uri", _CALLBACK),
        ("state", "state_value_1234567890"),
    ]


def test_real_transport_installs_no_redirect_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: list[object] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            assert size == 32769
            return b"{}"

    class Opener:
        def open(self, request: object, *, timeout: float) -> Response:
            assert request.full_url == (
                "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
            )
            assert request.method == "POST"
            assert json.loads(request.data.decode("utf-8")) == {
                "app_id": "app",
                "app_secret": "fake",
            }
            assert dict(request.header_items()) == {
                "Content-type": "application/json; charset=utf-8"
            }
            assert timeout == 0.5
            return Response()

    def build(*handlers: object) -> Opener:
        installed.extend(handlers)
        return Opener()

    monkeypatch.setattr(feishu_oauth, "build_opener", build)

    response = feishu_oauth._post_json(
        url="https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        headers={"Content-Type": "application/json; charset=utf-8"},
        body={"app_id": "app", "app_secret": "fake"},
        timeout_seconds=0.5,
    )

    assert response == feishu_oauth._HttpResponse(status=200, body=b"{}")
    assert len(installed) == 1
    assert isinstance(installed[0], feishu_oauth._NoRedirect)
    assert installed[0].redirect_request(None, None, 302, "", None, "https://evil") is None


@pytest.mark.parametrize(
    ("state", "callback"),
    [
        ("short", _CALLBACK),
        ("state_value_1234567890\n", _CALLBACK),
        ("state_value_1234567890", "http://ops.example.test/oauth/feishu/callback"),
        ("state_value_1234567890", "https://user@ops.example.test/oauth/feishu/callback"),
        ("state_value_1234567890", _CALLBACK + "?next=/"),
        ("state_value_1234567890", _CALLBACK + "#fragment"),
        ("state_value_1234567890", _CALLBACK + "/extra"),
    ],
)
def test_authorization_url_rejects_unbounded_state_or_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    callback: str,
) -> None:
    adapter, _ = _adapter(tmp_path, monkeypatch)

    with pytest.raises(FeishuOAuthCodeError):
        adapter.authorization_url(state=state, redirect_uri=callback)


async def test_exchange_uses_exact_v1_wire_order_bodies_and_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, calls = _adapter(tmp_path, monkeypatch)

    with bind_trace_id(_TRACE_ID):
        identity = await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)

    assert identity.model_dump() == {"subject_ref": "ou_test_subject"}
    assert [call["url"] for call in calls] == [
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        "https://open.feishu.cn/open-apis/authen/v1/access_token",
    ]
    assert calls[0]["body"] == {
        "app_id": "cli_test_app",
        "app_secret": _FAKE_SECRET,
    }
    assert calls[0]["headers"] == {
        "Content-Type": "application/json; charset=utf-8"
    }
    assert calls[1]["body"] == {
        "grant_type": "authorization_code",
        "code": "one-time-code",
    }
    assert calls[1]["headers"] == {
        "Authorization": f"Bearer {_FAKE_APP_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }
    assert len(calls) == 2
    assert all(0 < float(call["timeout_seconds"]) < 1.0 for call in calls)


async def test_each_exchange_fetches_a_fresh_app_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每次 exchange 都重新取 app_access_token，不复用上一次的。

    原用例还断言"每次 exchange 重读 secret 文件"。RI5 之后 Secret 由装配层注入，
    adapter 生命周期内不变——轮换凭据的方式是改 `integrations.json` 后重启，这正是
    generation/restart 模型本身。文件重读那一半因此不再存在，token 不复用这一半
    是真正的安全属性，保留。
    """
    responses = _success_responses() + _success_responses()
    responses[2] = _json_response(
        {
            "code": 0,
            "msg": "ok",
            "app_access_token": "second-app-value",
            "expire": 3600,
        }
    )
    calls: list[dict[str, object]] = []

    def post_json(**kwargs: object):
        calls.append(kwargs)
        return responses[len(calls) - 1]

    monkeypatch.setattr(feishu_oauth, "_post_json", post_json)
    adapter = FeishuOAuthAdapter(
        app_id="cli_test_app",
        app_secret=_FAKE_SECRET,
        timeout_seconds=1.0,
    )
    with bind_trace_id(_TRACE_ID):
        await adapter.exchange_code(code="first-code", redirect_uri=_CALLBACK)
        await adapter.exchange_code(code="second-code", redirect_uri=_CALLBACK)

    assert calls[0]["body"]["app_secret"] == _FAKE_SECRET
    assert calls[2]["body"]["app_secret"] == _FAKE_SECRET
    assert calls[1]["headers"]["Authorization"] == f"Bearer {_FAKE_APP_TOKEN}"
    assert calls[3]["headers"]["Authorization"] == "Bearer second-app-value"


@pytest.mark.parametrize(
    "code",
    ["", "bad\ncode", "x" * 2049, *(f"bad{value}code" for value in _C1_CONTROLS)],
)
async def test_exchange_rejects_invalid_code_without_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    adapter, calls = _adapter(tmp_path, monkeypatch)

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthCodeError):
        await adapter.exchange_code(code=code, redirect_uri=_CALLBACK)

    assert calls == []


@pytest.mark.parametrize("control", _C1_CONTROLS)
def test_authorization_url_rejects_c1_control_in_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    adapter, _ = _adapter(tmp_path, monkeypatch)
    callback = f"https://ops.example.test{control}/oauth/feishu/callback"

    with pytest.raises(FeishuOAuthCodeError):
        adapter.authorization_url(
            state="state_value_1234567890",
            redirect_uri=callback,
        )


@pytest.mark.parametrize("control", _C1_CONTROLS)
async def test_exchange_rejects_c1_control_in_callback_without_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: str
) -> None:
    adapter, calls = _adapter(tmp_path, monkeypatch)
    callback = f"https://ops.example.test{control}/oauth/feishu/callback"

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthCodeError):
        await adapter.exchange_code(code="one-time-code", redirect_uri=callback)

    assert calls == []


@pytest.mark.parametrize("kind", ["code", "callback"])
async def test_unpaired_surrogate_in_local_oauth_input_maps_to_closed_code_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    adapter, calls = _adapter(tmp_path, monkeypatch)
    code = "bad\ud800code" if kind == "code" else "one-time-code"
    callback = _CALLBACK + "\ud800" if kind == "callback" else _CALLBACK

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthCodeError) as caught:
        await adapter.exchange_code(code=code, redirect_uri=callback)

    assert str(caught.value) == ""
    assert caught.value.__context__ is None
    assert calls == []


async def test_exchange_requires_an_already_bound_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, calls = _adapter(tmp_path, monkeypatch)

    with pytest.raises(FeishuOAuthUnavailableError):
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)

    assert calls == []


@pytest.mark.parametrize(
    ("responses", "expected"),
    [
        ([_json_response({"code": 9, "msg": "denied"})], FeishuOAuthUnavailableError),
        (
            [
                _success_responses()[0],
                _json_response({"code": 20029, "msg": "invalid code"}),
            ],
            FeishuOAuthCodeError,
        ),
    ],
)
async def test_provider_failure_unions_map_to_closed_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[object],
    expected: type[Exception],
) -> None:
    adapter, _ = _adapter(tmp_path, monkeypatch, responses)

    with bind_trace_id(_TRACE_ID), pytest.raises(expected) as caught:
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)

    assert "denied" not in str(caught.value)
    assert "invalid code" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        feishu_oauth._HttpResponse(status=500, body=b"provider-private-body"),
        feishu_oauth._HttpResponse(status=200, body=b"not-json-provider-private-body"),
        feishu_oauth._HttpResponse(status=200, body=b"\xff"),
        feishu_oauth._HttpResponse(
            status=200,
            body=b'{"code":0,"code":0,"msg":"ok","app_access_token":"x","expire":1}',
        ),
        _json_response(
            {
                "code": 0,
                "msg": "ok",
                "app_access_token": _FAKE_APP_TOKEN,
                "expire": 3600,
                "unknown": "field",
            }
        ),
        _json_response(
            {
                "code": 0,
                "msg": "ok",
                "app_access_token": _FAKE_APP_TOKEN,
                "expire": True,
            }
        ),
        feishu_oauth._HttpResponse(status=200, body=b"x" * 32769),
    ],
)
async def test_app_token_malformed_responses_fail_closed_without_body_leakage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: object,
) -> None:
    adapter, calls = _adapter(tmp_path, monkeypatch, [response])

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthUnavailableError) as caught:
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)

    rendered = "".join(traceback.format_exception(caught.value))
    assert len(calls) == 1
    assert "provider-private-body" not in rendered
    assert _FAKE_APP_TOKEN not in rendered


@pytest.mark.parametrize(
    "data_update",
    [
        {"open_id": None},
        {"open_id": "bad\tidentity"},
        {"open_id": " padded-identity "},
        {"open_id": "x" * 257},
        {"access_token": ""},
        {"expires_in": True},
        {"unknown": "field"},
    ],
)
async def test_identity_response_rejects_missing_control_unknown_and_invalid_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data_update: dict[str, object],
) -> None:
    responses = _success_responses()
    payload = json.loads(responses[1].body)
    payload["data"].update(data_update)
    responses[1] = _json_response(payload)
    adapter, _ = _adapter(tmp_path, monkeypatch, responses)

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthUnavailableError):
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)


@pytest.mark.parametrize("control", _C1_CONTROLS)
@pytest.mark.parametrize(
    "location",
    ["app-message", "identity-message", "app-token", "user-token", "open-id"],
)
async def test_provider_c1_control_strings_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: str,
    location: str,
) -> None:
    responses = _success_responses()
    if location in {"app-message", "app-token"}:
        payload = json.loads(responses[0].body)
        field = "msg" if location == "app-message" else "app_access_token"
        payload[field] = f"bad{control}value"
        responses = [_json_response(payload)]
    else:
        payload = json.loads(responses[1].body)
        if location == "identity-message":
            payload["msg"] = f"bad{control}value"
        else:
            field = "access_token" if location == "user-token" else "open_id"
            payload["data"][field] = f"bad{control}value"
        responses[1] = _json_response(payload)
    adapter, calls = _adapter(tmp_path, monkeypatch, responses)

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthUnavailableError):
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)

    assert len(calls) == (1 if location in {"app-message", "app-token"} else 2)


async def test_printable_non_ascii_oauth_values_remain_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [
        _json_response(
            {
                "code": 0,
                "msg": "成功",
                "app_access_token": "应用令牌",
                "expire": 3600,
            }
        ),
        _json_response(
            {
                "code": 0,
                "msg": "成功",
                "data": {
                    "access_token": "用户令牌",
                    "name": "测试用户",
                    "open_id": "用户标识",
                },
            }
        ),
    ]
    calls: list[dict[str, object]] = []

    def post_json(**kwargs: object):
        calls.append(kwargs)
        return responses[len(calls) - 1]

    monkeypatch.setattr(feishu_oauth, "_post_json", post_json)
    adapter = FeishuOAuthAdapter(
        app_id="cli_测试应用",
        app_secret=_FAKE_SECRET,
        timeout_seconds=1.0,
    )

    with bind_trace_id(_TRACE_ID):
        identity = await adapter.exchange_code(code="一次性授权码", redirect_uri=_CALLBACK)

    assert identity.subject_ref == "用户标识"
    assert calls[0]["body"]["app_id"] == "cli_测试应用"
    assert calls[1]["body"]["code"] == "一次性授权码"


async def test_identity_top_level_rejects_duplicate_and_unknown_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = _success_responses()
    responses[1] = feishu_oauth._HttpResponse(
        status=200,
        body=b'{"code":0,"msg":"ok","data":{},"unknown":1}',
    )
    adapter, _ = _adapter(tmp_path, monkeypatch, responses)

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthUnavailableError):
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)


@pytest.mark.parametrize("location", ["app-message", "data-string", "app-token", "open-id"])
async def test_provider_surrogate_strings_map_to_closed_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    responses = _success_responses()
    if location == "app-message":
        responses = [
            _json_response(
                {
                    "code": 0,
                    "msg": "bad\ud800message",
                    "app_access_token": _FAKE_APP_TOKEN,
                    "expire": 3600,
                }
            )
        ]
    elif location == "app-token":
        responses = [
            _json_response(
                {
                    "code": 0,
                    "msg": "ok",
                    "app_access_token": "bad\ud800token",
                    "expire": 3600,
                }
            )
        ]
    else:
        payload = json.loads(responses[1].body)
        field = "name" if location == "data-string" else "open_id"
        payload["data"][field] = "bad\ud800value"
        responses[1] = _json_response(payload)
    adapter, _ = _adapter(tmp_path, monkeypatch, responses)

    with bind_trace_id(_TRACE_ID), pytest.raises(
        FeishuOAuthUnavailableError
    ) as caught:
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)

    assert str(caught.value) == ""
    assert caught.value.__context__ is None


async def test_deep_json_recursion_maps_to_closed_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deep_json = b"[" * 1200 + b"0" + b"]" * 1200
    adapter, _ = _adapter(
        tmp_path,
        monkeypatch,
        [feishu_oauth._HttpResponse(status=200, body=deep_json)],
    )

    with bind_trace_id(_TRACE_ID), pytest.raises(
        FeishuOAuthUnavailableError
    ) as caught:
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)

    assert str(caught.value) == ""
    assert caught.value.__context__ is None


@pytest.mark.parametrize("timeout_seconds", [0, -1, 5.01, float("inf"), float("nan"), True])
def test_constructor_rejects_invalid_deadlines(
    tmp_path: Path, timeout_seconds: float
) -> None:
    with pytest.raises(ValueError):
        FeishuOAuthAdapter(
            app_id="cli_test_app",
            app_secret=_FAKE_SECRET,
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.parametrize("app_id", ["bad\ud800app", " padded-app ", "é" * 129])
def test_constructor_maps_invalid_app_id_to_generic_value_error(
    tmp_path: Path, app_id: str
) -> None:
    with pytest.raises(ValueError) as caught:
        FeishuOAuthAdapter(
            app_id=app_id,
            app_secret=_FAKE_SECRET,
            timeout_seconds=1.0,
        )

    assert str(caught.value) == "feishu oauth configuration invalid"
    assert caught.value.__context__ is None


@pytest.mark.parametrize("control", _C1_CONTROLS)
def test_constructor_rejects_c1_control_in_app_id(
    tmp_path: Path, control: str
) -> None:
    with pytest.raises(ValueError, match="feishu oauth configuration invalid"):
        FeishuOAuthAdapter(
            app_id=f"cli_test{control}app",
            app_secret=_FAKE_SECRET,
            timeout_seconds=1.0,
        )


@pytest.mark.parametrize(
    "app_secret",
    ["", "bad\tvalue", "bad\nvalue", "x" * 4097, *(f"bad{c}value" for c in _C1_CONTROLS)],
)
def test_constructor_eagerly_rejects_an_unusable_secret(app_secret: str) -> None:
    """不可用的 Secret 在构造期就必须拒绝，且不得回填到异常文本里。

    原用例的后半段测的是"构造期通过、运行期重读时才发现坏掉"。Secret 改为注入后
    这个窗口不存在了：构造期看到的就是最终值，坏值根本进不来。
    """
    with pytest.raises(ValueError) as caught:
        FeishuOAuthAdapter(
            app_id="cli_test_app",
            app_secret=app_secret,
            timeout_seconds=1.0,
        )

    assert str(caught.value) == "feishu oauth configuration invalid"
    assert caught.value.__context__ is None
    if app_secret:
        assert app_secret not in str(caught.value)


@pytest.mark.parametrize("suffix", ["\ud800", "\x00"])
def test_constructor_maps_an_unrepresentable_secret_to_generic_value_error(
    suffix: str,
) -> None:
    """不可编码的取值（孤立代理、NUL）同样只能得到同一条常量错误。"""
    with pytest.raises(ValueError) as caught:
        FeishuOAuthAdapter(
            app_id="cli_test_app",
            app_secret=_FAKE_SECRET + suffix,
            timeout_seconds=1.0,
        )

    assert str(caught.value) == "feishu oauth configuration invalid"
    assert caught.value.__context__ is None


async def test_both_posts_share_one_total_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = _success_responses()
    timeouts: list[float] = []
    ticks = iter((100.0, 100.0, 100.12))

    def post_json(**kwargs: object):
        timeouts.append(float(kwargs["timeout_seconds"]))
        return responses[len(timeouts) - 1]

    monkeypatch.setattr(feishu_oauth, "_post_json", post_json)
    monkeypatch.setattr(
        feishu_oauth,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    adapter = FeishuOAuthAdapter(
        app_id="cli_test_app",
        app_secret=_FAKE_SECRET,
        timeout_seconds=0.2,
    )

    with bind_trace_id(_TRACE_ID):
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)

    assert len(timeouts) == 2
    assert timeouts == pytest.approx([0.1, 0.04])


# 原 `test_secret_reread_is_inside_total_deadline_and_cannot_start_http_late` 已删除：
# 它测的是"读 secret 文件消耗的时间也算进总预算"。Secret 改为注入后没有这次 I/O，
# 两次 POST 共享同一预算这条属性由 `test_both_posts_share_one_total_deadline` 覆盖。


async def test_late_first_response_cannot_trigger_a_second_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def post_json(**_kwargs: object):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        finished.set()
        return _success_responses()[0]

    monkeypatch.setattr(feishu_oauth, "_post_json", post_json)
    adapter = FeishuOAuthAdapter(
        app_id="cli_test_app",
        app_secret=_FAKE_SECRET,
        timeout_seconds=0.05,
    )
    try:
        with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthUnavailableError):
            await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)
        assert started.wait(1)
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert calls == 1


async def test_direct_cancellation_is_not_converted_and_late_result_is_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def post_json(**_kwargs: object):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(1)
        finished.set()
        return _success_responses()[0]

    monkeypatch.setattr(feishu_oauth, "_post_json", post_json)
    adapter = FeishuOAuthAdapter(
        app_id="cli_test_app",
        app_secret=_FAKE_SECRET,
        timeout_seconds=1.0,
    )
    try:
        with bind_trace_id(_TRACE_ID):
            task = asyncio.create_task(
                adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)
            )
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert calls == 1


async def test_late_second_response_cannot_construct_an_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def post_json(**_kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _success_responses()[0]
        second_started.set()
        release.wait(1)
        finished.set()
        return _success_responses()[1]

    monkeypatch.setattr(feishu_oauth, "_post_json", post_json)
    adapter = FeishuOAuthAdapter(
        app_id="cli_test_app",
        app_secret=_FAKE_SECRET,
        timeout_seconds=0.05,
    )
    constructed: list[str] = []

    def identity(**kwargs: str):
        constructed.append(kwargs["subject_ref"])
        raise AssertionError("late result must not construct identity")

    monkeypatch.setattr(feishu_oauth, "FeishuOAuthIdentity", identity)
    try:
        with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthUnavailableError):
            await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)
        assert second_started.wait(1)
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert calls == 2
    assert constructed == []


async def test_transport_error_and_adapter_repr_never_expose_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    code = "private-one-time-code"
    provider = "private-provider-body"
    app_id = "private-app-id"
    app_secret = "private-app-" + "secret-value"

    def fail(**_kwargs: object):
        raise OSError(provider)

    monkeypatch.setattr(feishu_oauth, "_post_json", fail)
    adapter = FeishuOAuthAdapter(
        app_id=app_id,
        app_secret=app_secret,
        timeout_seconds=1.0,
    )
    with caplog.at_level(logging.DEBUG), bind_trace_id(_TRACE_ID):
        with pytest.raises(FeishuOAuthUnavailableError) as caught:
            await adapter.exchange_code(code=code, redirect_uri=_CALLBACK)

    rendered = "\n".join((str(caught.value), repr(caught.value), repr(adapter), caplog.text))
    # Secret 现在常驻内存，repr 泄露的风险比持路径时更高，这条因此更承重。
    for private in (code, provider, app_id, app_secret):
        assert private not in rendered


def _real_shaped_app_token(**extra: object):
    """飞书 ``app_access_token/internal`` 的真实回包形状（本机实测字段名）。"""
    payload: dict[str, object] = {
        "code": 0,
        "msg": "ok",
        "app_access_token": _FAKE_APP_TOKEN,
        "expire": 3600,
        "tenant_access_token": "tenant-unit-test-" + "token",
    }
    payload.update(extra)
    return _json_response(payload)


async def test_exchange_accepts_the_real_app_token_response_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实飞书同时回 ``tenant_access_token``；闭集缺它时每次扫码都报 unavailable。"""
    responses = _success_responses()
    responses[0] = _real_shaped_app_token()
    adapter, calls = _adapter(tmp_path, monkeypatch, responses)

    with bind_trace_id(_TRACE_ID):
        identity = await adapter.exchange_code(
            code="one-time-code", redirect_uri=_CALLBACK
        )

    assert identity.subject_ref == "ou_test_subject"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "tenant_token",
    [None, "", 7, " padded ", "x" * 9000, "bad\u0085token", "bad\ttoken"],
)
async def test_a_malformed_tenant_token_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tenant_token: object
) -> None:
    responses = _success_responses()
    responses[0] = _real_shaped_app_token(tenant_access_token=tenant_token)
    adapter, calls = _adapter(tmp_path, monkeypatch, responses)

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthUnavailableError):
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)
    assert len(calls) == 1


async def test_fields_beyond_the_documented_app_token_set_still_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = _success_responses()
    responses[0] = _real_shaped_app_token(unknown="field")
    adapter, _ = _adapter(tmp_path, monkeypatch, responses)

    with bind_trace_id(_TRACE_ID), pytest.raises(FeishuOAuthUnavailableError):
        await adapter.exchange_code(code="one-time-code", redirect_uri=_CALLBACK)
