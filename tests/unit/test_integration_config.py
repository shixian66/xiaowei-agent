"""`integrations.json` 契约：代次为正整数，secret 只走显式访问器。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    FeishuIntegration,
    GeminiIntegration,
    IntegrationConfig,
)


def _config(**kwargs: object) -> IntegrationConfig:
    base: dict[str, object] = {
        "generation": 1,
        "gemini": GeminiIntegration(enabled=False),
        "feishu": FeishuIntegration(enabled=False),
    }
    base.update(kwargs)
    return IntegrationConfig(**base)  # type: ignore[arg-type]


def test_generation_must_be_a_positive_integer() -> None:
    with pytest.raises(ValidationError):
        _config(generation=0)
    with pytest.raises(ValidationError):
        _config(generation=True)


def test_model_dump_never_carries_secret_values() -> None:
    fake_key = "AIza" + "-not-a-real-key"
    config = _config(gemini=GeminiIntegration(enabled=True, api_key=fake_key))
    dumped = config.model_dump()
    assert fake_key not in repr(dumped)
    assert fake_key not in config.model_dump_json()
    assert dumped["gemini"]["enabled"] is True
    assert "api_key" not in dumped["gemini"]


def test_repr_never_carries_secret_values() -> None:
    """``exclude=True`` 只挡 ``model_dump()``；``repr()`` 是另一条通道。

    上一条断言的是 ``repr(dumped)``——已经 dump 过的字典。它看起来在测 repr，
    实际上什么也没测：只写 ``exclude=True`` 的实现同样能让它全绿，而
    ``repr(config)`` 会把完整 Key 原样打印出来。
    """
    fake_key = "AIza" + "-not-a-real-key"
    fake_secret = "app" + "-secret-placeholder"
    gemini = GeminiIntegration(enabled=True, api_key=fake_key)
    feishu = FeishuIntegration(enabled=True, app_id="cli_x", app_secret=fake_secret)
    config = _config(gemini=gemini, feishu=feishu)

    for rendered in (
        repr(gemini),
        str(gemini),
        repr(feishu),
        str(feishu),
        repr(config),
        str(config),
        f"{config}",
    ):
        assert fake_key not in rendered
        assert fake_secret not in rendered
    # 非 secret 字段仍要看得见，否则排障时 repr 变成一团空壳。
    assert "cli_x" in repr(feishu)
    assert "enabled=True" in repr(gemini)


def test_secret_values_are_reachable_only_through_the_explicit_accessor() -> None:
    fake_secret = "app" + "-secret-placeholder"
    config = _config(
        feishu=FeishuIntegration(enabled=True, app_id="cli_x", app_secret=fake_secret)
    )
    assert config.feishu.secret_value() == fake_secret


def test_secret_rejects_control_characters_and_oversize() -> None:
    with pytest.raises(ValidationError):
        GeminiIntegration(enabled=True, api_key="bad\nvalue")
    with pytest.raises(ValidationError):
        GeminiIntegration(enabled=True, api_key="x" * 4097)
