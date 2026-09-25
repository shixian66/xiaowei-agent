from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def test_local_real_test_override_enables_only_provider_probes() -> None:
    compose = yaml.safe_load(
        (_ROOT / "docker-compose.local-test.yml").read_text(encoding="utf-8")
    )

    assert set(compose) == {"services"}
    assert set(compose["services"]) == {"web-app"}
    assert compose["services"]["web-app"]["environment"] == {
        "XIAOWEI_GEMINI_REAL_TEST_ENABLED": "true",
        "XIAOWEI_FEISHU_REAL_TEST_ENABLED": "true",
    }


def test_local_real_test_override_does_not_enable_real_channels_or_model_execution() -> None:
    compose = yaml.safe_load(
        (_ROOT / "docker-compose.local-test.yml").read_text(encoding="utf-8")
    )
    environment = compose["services"]["web-app"]["environment"]

    assert "XIAOWEI_GEMINI_ENABLED" not in environment
    assert "XIAOWEI_FEISHU_OAUTH_ENABLED" not in environment
    assert "XIAOWEI_FEISHU_LISTENER_ENABLED" not in environment
    assert "XIAOWEI_CHANNEL_WORKER_ENABLED" not in environment
