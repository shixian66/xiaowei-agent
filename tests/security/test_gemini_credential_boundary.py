"""Gemini credential 只允许固定 worker secret 文件路径。"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.model import assert_safe_model_port_error

from xiaowei_agent.application.model_ports import ModelPortError
from xiaowei_agent.config import _FIELD_TO_ENV, Settings
from xiaowei_agent.contracts import ModelErrorCode, ModelIntentRequest
from xiaowei_agent.interfaces.gemini_model import GeminiModelAdapter

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]


class CountingFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, **kwargs: Any) -> object:
        self.calls += 1
        raise AssertionError("client construction must stay at zero")


@pytest.mark.asyncio
async def test_missing_or_invalid_key_never_constructs_a_client() -> None:
    """不可用的 Key 必须在构造 client **之前**就被闭集拒绝。

    RI5 之后 Key 由装配层注入，adapter 不再持有 reader，因此"reader 抛异常"这条
    路径不复存在——原本的三条用例（reader 抛 SecretFileError / 抛 ValueError /
    抛 BaseException）都测的是那个 seam。承重的属性没变，只是输入从"读出来的值"
    变成"注入的值"：任何不满足 ``_validate_credential`` 的取值都不得走到 client。
    """
    request = ModelIntentRequest(user_text="检查", history=(), context_truncated=False)
    for api_key in (
        "",
        "short",
        "AIza" + "x" * 35 + " whitespace",
        " " + "AIza" + "x" * 35,
        "AIza" + "x" * 35 + "\n",
        "AIza" + "x" * 35 + "\u200b",
        "A" * 257,
    ):
        factory = CountingFactory()
        adapter = GeminiModelAdapter(client_factory=factory, api_key=api_key)
        with pytest.raises(ModelPortError) as caught:
            await adapter.generate_intent(request)
        assert_safe_model_port_error(
            caught.value, ModelErrorCode.CREDENTIAL_UNAVAILABLE
        )
        assert factory.calls == 0


@pytest.mark.asyncio
async def test_the_rejected_key_never_appears_in_the_error() -> None:
    """拒绝路径本身不得成为明文 Key 的外泄通道。"""
    leaky = "AIza" + "leaked-sentinel-value" + " " * 3
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("client must not be constructed")
        ),
        api_key=leaky,
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.CREDENTIAL_UNAVAILABLE)
    assert leaky.strip() not in str(caught.value)
    assert leaky.strip() not in repr(caught.value)


@pytest.mark.asyncio
async def test_ambient_proxy_is_rejected_before_secret_or_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")

    # 环境代理必须在凭据处理**之前**判定。Key 现在是注入的，数不到"读了几次"，
    # 因此改测更强的顺序性质：合法 Key 也不得越过代理检查，非法 Key 也不得把
    # 结论改成 CREDENTIAL_UNAVAILABLE——代理才是这一轮唯一该报的事实。
    for api_key in ("AIza" + "x" * 35, "short"):
        factory = CountingFactory()
        adapter = GeminiModelAdapter(client_factory=factory, api_key=api_key)
        with pytest.raises(ModelPortError) as caught:
            await adapter.generate_intent(
                ModelIntentRequest(
                    user_text="检查", history=(), context_truncated=False
                )
            )
        assert_safe_model_port_error(caught.value, ModelErrorCode.AMBIENT_PROXY)
        assert factory.calls == 0


@pytest.mark.asyncio
async def test_redaction_changing_provider_output_is_rejected_without_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-" + "x" * 24
    response = json.dumps(
        {
            "intent": "starrocks.slow_query.diagnose",
            "slots": {"window_minutes": secret},
            "missing": [],
            "confidence": 0.5,
        }
    )

    class Models:
        async def generate_content(self, **kwargs: Any) -> object:
            return type(
                "Response", (), {"text": response, "usage_metadata": None}
            )()

    class Aio:
        models = Models()

        async def aclose(self) -> None:
            return None

    class Client:
        aio = Aio()

    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: Client(),
        api_key="AIza" + "x" * 35,
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="检查", history=(), context_truncated=False)
        )
    assert_safe_model_port_error(caught.value, ModelErrorCode.INVALID_RESPONSE)
    assert secret not in str(caught.value)
    assert secret not in caplog.text


def test_key_is_not_a_setting_and_ignore_files_cover_host_secret_sources() -> None:
    from xiaowei_agent.interfaces import gemini_model

    # RI5：Key 的唯一真源是 `integrations.json`，adapter 不再持有任何路径常量。
    assert not hasattr(gemini_model, "GEMINI_SECRET_FILE")
    assert "GEMINI_API_KEY" not in _FIELD_TO_ENV.values()
    assert "GEMINI_API_KEY_FILE" not in _FIELD_TO_ENV.values()
    assert not any(
        "gemini" in name.lower() and "key" in name.lower()
        for name in Settings.model_fields
    )
    for name in (".gitignore", ".dockerignore"):
        lines = {
            line.strip()
            for line in (_ROOT / name).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        assert ".env" in lines
        assert ".env.*" in lines
        assert any(line.rstrip("/") == ".secrets" for line in lines)
