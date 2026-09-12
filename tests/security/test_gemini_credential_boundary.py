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
from xiaowei_agent.interfaces.gemini_model import (
    GEMINI_SECRET_FILE,
    GeminiModelAdapter,
)
from xiaowei_agent.interfaces.secret_file import SecretFileError

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
    request = ModelIntentRequest(user_text="检查", history=(), context_truncated=False)
    for reader in (
        lambda path: (_ for _ in ()).throw(SecretFileError("private")),
        lambda path: "short",
        lambda path: "AIza" + "x" * 35 + " whitespace",
    ):
        factory = CountingFactory()
        adapter = GeminiModelAdapter(client_factory=factory, secret_reader=reader)
        with pytest.raises(ModelPortError) as caught:
            await adapter.generate_intent(request)
        assert_safe_model_port_error(
            caught.value, ModelErrorCode.CREDENTIAL_UNAVAILABLE
        )
        assert factory.calls == 0


@pytest.mark.asyncio
async def test_ordinary_credential_reader_failure_is_safely_normalized() -> None:
    adapter = GeminiModelAdapter(
        client_factory=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("client must not be constructed")
        ),
        secret_reader=lambda path: (_ for _ in ()).throw(
            ValueError("private-credential-detail")
        ),
    )

    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert_safe_model_port_error(caught.value, ModelErrorCode.CREDENTIAL_UNAVAILABLE)


@pytest.mark.asyncio
async def test_credential_reader_base_exception_still_propagates() -> None:
    class StopCredentialRead(BaseException):
        pass

    sentinel = StopCredentialRead()
    factory = CountingFactory()
    adapter = GeminiModelAdapter(
        client_factory=factory,
        secret_reader=lambda path: (_ for _ in ()).throw(sentinel),
    )

    with pytest.raises(StopCredentialRead) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="x", history=(), context_truncated=False)
        )

    assert caught.value is sentinel
    assert factory.calls == 0


@pytest.mark.asyncio
async def test_ambient_proxy_is_rejected_before_secret_or_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def reader(path: str) -> str:
        nonlocal calls
        calls += 1
        return "AIza" + "x" * 35

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    factory = CountingFactory()
    adapter = GeminiModelAdapter(client_factory=factory, secret_reader=reader)
    with pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="检查", history=(), context_truncated=False)
        )
    assert_safe_model_port_error(caught.value, ModelErrorCode.AMBIENT_PROXY)
    assert calls == 0
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
        secret_reader=lambda path: "AIza" + "x" * 35,
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(ModelPortError) as caught:
        await adapter.generate_intent(
            ModelIntentRequest(user_text="检查", history=(), context_truncated=False)
        )
    assert_safe_model_port_error(caught.value, ModelErrorCode.INVALID_RESPONSE)
    assert secret not in str(caught.value)
    assert secret not in caplog.text


def test_key_is_not_a_setting_and_ignore_files_cover_dotenv_variants() -> None:
    assert GEMINI_SECRET_FILE == "/run/secrets/gemini_api_" + "key"
    assert "GEMINI_API_KEY" not in _FIELD_TO_ENV.values()
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
