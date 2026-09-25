"""本机完整体验 override：打开的开关是闭集，入口只绑 loopback，不碰真人消息通道。"""

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _override() -> dict[str, Any]:
    loaded = yaml.safe_load(
        (_ROOT / "docker-compose.local-full.yml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_local_full_override_opens_exactly_the_provider_switches() -> None:
    compose = _override()

    assert set(compose) == {"services", "volumes"}
    assert set(compose["services"]) == {"worker", "web-app", "edge"}
    assert compose["services"]["worker"] == {
        "environment": {"XIAOWEI_GEMINI_ENABLED": "true"}
    }
    assert compose["services"]["web-app"] == {
        "environment": {
            "XIAOWEI_WEB_APP_ENABLED": "true",
            "XIAOWEI_WEB_MODE": "https",
            "XIAOWEI_WEB_PUBLIC_ORIGIN": (
                "${XIAOWEI_WEB_PUBLIC_ORIGIN:-https://xiaowei.localhost:8443}"
            ),
            "XIAOWEI_FEISHU_OAUTH_ENABLED": "true",
            "XIAOWEI_GEMINI_REAL_TEST_ENABLED": "true",
            "XIAOWEI_FEISHU_REAL_TEST_ENABLED": "true",
        }
    }


def test_local_full_override_never_starts_the_feishu_message_channels() -> None:
    compose = _override()
    text = (_ROOT / "docker-compose.local-full.yml").read_text(encoding="utf-8")

    assert "feishu-listener" not in compose["services"]
    assert "channel-worker" not in compose["services"]
    assert "XIAOWEI_FEISHU_LISTENER_ENABLED" not in text
    assert "XIAOWEI_CHANNEL_WORKER_ENABLED" not in text
    assert "profiles" not in text


def test_local_full_override_keeps_the_web_port_and_carries_no_credentials() -> None:
    compose = _override()
    text = (_ROOT / "docker-compose.local-full.yml").read_text(encoding="utf-8")

    # Web 的 127.0.0.1:8080 仍由基础 Compose 决定，这里不改端口面。
    assert "ports" not in compose["services"]["web-app"]
    for marker in ("api_key", "app_secret", "app_id", "secrets", ".config"):
        assert marker not in compose["services"]["worker"]
        assert marker not in compose["services"]["web-app"]
    assert "GEMINI_API_KEY" not in text


def test_local_edge_is_pinned_loopback_only_and_hardened() -> None:
    edge = _override()["services"]["edge"]

    assert edge["image"].startswith("caddy@sha256:")
    assert len(edge["image"].split("@sha256:")[1]) == 64
    assert edge["ports"] == ["127.0.0.1:8443:8443"]
    assert edge["command"][:2] == ["caddy", "reverse-proxy"]
    assert "--internal-certs" in edge["command"]
    assert "--disable-redirects" in edge["command"]
    assert edge["command"][edge["command"].index("--to") + 1] == "web-app:8080"
    assert edge["read_only"] is True
    assert edge["cap_drop"] == ["ALL"]
    assert edge["cap_add"] == ["NET_BIND_SERVICE"]
    assert edge["security_opt"] == ["no-new-privileges:true"]
    assert edge["depends_on"] == {"web-app": {"condition": "service_healthy"}}
