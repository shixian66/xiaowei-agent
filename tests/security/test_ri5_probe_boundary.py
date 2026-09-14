"""三个控制面探针的出站边界。

这组用例回答的全是同一个问题的不同侧面：**什么情况下允许有一次真实出站，以及
那次出站失败之后允许留下什么**。开关关闭时的"零调用"是其中最重的一条——它不是
"不发请求"，是连 client 都不构造。
"""

from typing import Any

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import ModelErrorCode
from xiaowei_agent.interfaces.provider_probe import (
    GEMINI_ERROR_CODES,
    GEMINI_PROBE_INPUT,
    ProbeErrorCode,
    ProbeOutcome,
    ProbeTransportError,
    probe_feishu_credentials,
    probe_gemini_connection,
)
from xiaowei_agent.persistence.provider_state import ProviderTestErrorCode

pytestmark = pytest.mark.security

_FAKE_KEY = "probe-unit-" + "test-key"
_FAKE_SECRET = "probe-unit-" + "test-secret"


class _Spy:
    """记录出站调用次数；任何一次调用都会被计入。"""

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result
        self._error = error

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


# --------------------------------------------------------------------------
# 开关与配置：任何一层不满足都不得产生出站
# --------------------------------------------------------------------------


async def test_disabled_switch_returns_the_closed_code_without_any_outbound_call() -> (
    None
):
    spy = _Spy()
    outcome = await probe_gemini_connection(
        enabled=False, api_key=_FAKE_KEY, transport=spy, timeout_seconds=5.0
    )
    assert outcome.status == "failed"
    assert outcome.error_code is ProbeErrorCode.REAL_TEST_DISABLED
    assert spy.calls == []
    # 没有出站就没有耗时可言；"很快"和"没做"必须能被区分开。
    assert outcome.duration_ms == 0


async def test_missing_configuration_is_refused_before_any_outbound_call() -> None:
    spy = _Spy()
    outcome = await probe_gemini_connection(
        enabled=True, api_key=None, transport=spy, timeout_seconds=5.0
    )
    assert outcome.error_code is ProbeErrorCode.NOT_CONFIGURED
    assert spy.calls == []


async def test_feishu_switch_is_independent_of_the_gemini_switch() -> None:
    spy = _Spy()
    outcome = await probe_feishu_credentials(
        enabled=False,
        app_id="cli_x",
        app_secret=_FAKE_SECRET,
        transport=spy,
        timeout_seconds=5.0,
    )
    assert outcome.error_code is ProbeErrorCode.REAL_TEST_DISABLED
    assert spy.calls == []


@pytest.mark.parametrize(
    ("app_id", "app_secret"),
    [(None, _FAKE_SECRET), ("cli_x", None), (None, None)],
    ids=["no-app-id", "no-secret", "neither"],
)
async def test_feishu_needs_both_fields_before_any_outbound_call(
    app_id: str | None, app_secret: str | None
) -> None:
    """反例：只填一半也是未配置。

    飞书有两个必填字段。只看 secret 会让"填了密钥没填 App ID"走到真实出站，
    然后带着一个 ``None`` 去问 Provider。
    """
    spy = _Spy()
    outcome = await probe_feishu_credentials(
        enabled=True,
        app_id=app_id,
        app_secret=app_secret,
        transport=spy,
        timeout_seconds=5.0,
    )
    assert outcome.error_code is ProbeErrorCode.NOT_CONFIGURED
    assert spy.calls == []


async def test_the_switch_is_checked_before_the_configuration() -> None:
    """反例：开关关闭时，连"有没有配 Key"都不该被回答。

    顺序反过来的实现会在未配置时返回 ``NOT_CONFIGURED``、已配置时返回
    ``REAL_TEST_DISABLED``——两个码的差别就把"这台机器上配没配过凭据"说了出去。
    """
    configured = await probe_gemini_connection(
        enabled=False, api_key=_FAKE_KEY, transport=_Spy(), timeout_seconds=5.0
    )
    absent = await probe_gemini_connection(
        enabled=False, api_key=None, transport=_Spy(), timeout_seconds=5.0
    )
    assert configured.error_code is absent.error_code is ProbeErrorCode.REAL_TEST_DISABLED


# --------------------------------------------------------------------------
# 出站形状与失败投影
# --------------------------------------------------------------------------


async def test_gemini_probe_input_is_the_fixed_minimal_synthetic_text() -> None:
    spy = _Spy(result={"ok": True})
    await probe_gemini_connection(
        enabled=True, api_key=_FAKE_KEY, transport=spy, timeout_seconds=5.0
    )
    assert len(spy.calls) == 1
    assert spy.calls[0]["contents"] == GEMINI_PROBE_INPUT
    assert isinstance(GEMINI_PROBE_INPUT, str) and GEMINI_PROBE_INPUT


async def test_failure_never_returns_provider_body_or_secret() -> None:
    sentinel = "sentinel" + "-provider-body"
    spy = _Spy(error=RuntimeError(sentinel))
    outcome = await probe_gemini_connection(
        enabled=True, api_key=_FAKE_KEY, transport=spy, timeout_seconds=5.0
    )
    assert outcome.status == "failed"
    assert outcome.error_code in set(ProbeErrorCode)
    rendered = outcome.model_dump_json()
    assert sentinel not in rendered
    assert _FAKE_KEY not in rendered
    # repr 与 exclude 是两条通道；异常链里也不能把正文带出来。
    assert sentinel not in repr(outcome)


async def test_duration_is_recorded_and_bounded() -> None:
    spy = _Spy(result={"ok": True})
    outcome = await probe_gemini_connection(
        enabled=True, api_key=_FAKE_KEY, transport=spy, timeout_seconds=5.0
    )
    assert outcome.status == "passed"
    assert 0 <= outcome.duration_ms <= 600_000


async def test_a_transport_may_only_speak_in_closed_codes() -> None:
    """``ProbeTransportError`` 是 SDK 唯一能把细节带过来的通道，而它只带闭集码。"""
    spy = _Spy(error=ProbeTransportError(ProbeErrorCode.UNAUTHORIZED))
    outcome = await probe_feishu_credentials(
        enabled=True,
        app_id="cli_x",
        app_secret=_FAKE_SECRET,
        transport=spy,
        timeout_seconds=5.0,
    )
    assert outcome.error_code is ProbeErrorCode.UNAUTHORIZED


async def test_a_timeout_is_its_own_code_not_a_generic_failure() -> None:
    """超时与不可用必须分开：一个让管理员去查网络，另一个去查凭据。"""

    async def never_returns(**_: object) -> object:
        import asyncio

        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    outcome = await probe_gemini_connection(
        enabled=True,
        api_key=_FAKE_KEY,
        transport=never_returns,
        timeout_seconds=0.01,
    )
    assert outcome.error_code is ProbeErrorCode.TIMEOUT


async def test_an_empty_response_is_invalid_not_a_pass() -> None:
    """反例：transport 什么都没返回不算成功。"""
    outcome = await probe_gemini_connection(
        enabled=True, api_key=_FAKE_KEY, transport=_Spy(result=None), timeout_seconds=5.0
    )
    assert outcome.error_code is ProbeErrorCode.INVALID_RESPONSE


# --------------------------------------------------------------------------
# 闭集本身
# --------------------------------------------------------------------------


def test_the_probe_codes_are_the_codes_that_get_written() -> None:
    """探针闭集与写库闭集是**同一个对象**，不是两份形状相同的枚举。

    抄第二份的后果不是重复代码：多出来的成员写不进表（CHECK 拒绝），少掉的成员
    让探针无法表达一种真实失败，而两者都只在真机上才暴露。
    """
    assert ProbeErrorCode is ProviderTestErrorCode


def test_every_model_error_code_has_a_probe_code() -> None:
    """反例：模型侧新增一个错误码而没有在这里给出映射，必须立刻变红。

    少一条映射的实现会在运行时 ``KeyError``——发生在真实 Provider 返回一个新
    错误码的时候，也就是最不希望多一个未处理异常的时候。
    """
    assert set(GEMINI_ERROR_CODES) == set(ModelErrorCode)
    assert set(GEMINI_ERROR_CODES.values()) <= set(ProbeErrorCode)


def test_a_passed_outcome_cannot_carry_an_error_code() -> None:
    """通过与失败原因互斥，和写库那条 CHECK 是同一条不变量。"""
    with pytest.raises(ValidationError):
        ProbeOutcome(
            status="passed", duration_ms=1, error_code=ProbeErrorCode.TIMEOUT
        )
    with pytest.raises(ValidationError):
        ProbeOutcome(status="failed", duration_ms=1, error_code=None)


def test_the_probe_module_does_not_import_any_sdk_at_module_scope() -> None:
    """Web 进程只有管理员真的点了测试才需要 SDK。

    模块导入期就把 ``lark_oapi`` / ``google.genai`` 拉进来，会让每个引用探针的
    进程都背上这份依赖，也让"没装配飞书的部署不加载飞书 SDK"这条既有边界失效。
    """
    import ast
    import pathlib

    import xiaowei_agent.interfaces.provider_probe as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            top_level.append(node.module)
    forbidden = {"lark_oapi", "google", "google.genai", "httpx"}
    assert not [name for name in top_level if name.split(".")[0] in forbidden]
    assert "xiaowei_agent.interfaces.gemini_model" not in top_level
    assert "xiaowei_agent.interfaces.feishu_sdk" not in top_level


def test_the_transport_payloads_are_exactly_what_each_provider_needs() -> None:
    """出站参数是闭集：探针不得把调用方能影响的任意键透传给 SDK。"""
    import ast
    import pathlib

    import xiaowei_agent.interfaces.provider_probe as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    payloads: list[set[str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "payload":
            assert isinstance(node.value, ast.Dict)
            payloads.append(
                {
                    key.value
                    for key in node.value.keys
                    if isinstance(key, ast.Constant)
                }
            )
    assert payloads == [{"api_key", "contents"}, {"app_id", "app_secret"}]


def test_feishu_rejected_credentials_are_unauthorized_not_invalid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """飞书在凭据错误时返回 HTTP 200 加业务错误码。

    把 "200 而未 success" 读成 ``INVALID_RESPONSE`` 会让管理员去查网络，而真正
    要改的是 App ID / Secret。
    """
    from xiaowei_agent.interfaces.provider_probe import _feishu_error_code

    assert _feishu_error_code(200) is ProbeErrorCode.UNAUTHORIZED
    assert _feishu_error_code(401) is ProbeErrorCode.UNAUTHORIZED
    assert _feishu_error_code(403) is ProbeErrorCode.UNAUTHORIZED
    assert _feishu_error_code(408) is ProbeErrorCode.TIMEOUT
    assert _feishu_error_code(503) is ProbeErrorCode.UNAVAILABLE
    assert _feishu_error_code(418) is ProbeErrorCode.INVALID_RESPONSE


async def test_a_real_transport_maps_model_errors_without_leaking_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Gemini transport 只把闭集码带出来，原始异常不进异常链。"""
    from xiaowei_agent.application.model_ports import ModelPortError
    from xiaowei_agent.interfaces import gemini_model, provider_probe

    sentinel = "sentinel" + "-model-detail"

    async def refuse(**_: Any) -> None:
        raise ModelPortError(ModelErrorCode.UNAUTHORIZED)

    monkeypatch.setattr(gemini_model, "probe_connection", refuse)
    with pytest.raises(ProbeTransportError) as caught:
        await provider_probe.gemini_probe_transport(
            api_key=_FAKE_KEY, contents=GEMINI_PROBE_INPUT
        )
    assert caught.value.code is ProbeErrorCode.UNAUTHORIZED
    assert caught.value.__context__ is None
    assert sentinel not in str(caught.value)
