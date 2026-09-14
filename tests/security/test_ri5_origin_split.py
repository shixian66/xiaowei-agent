"""协议放开只作用于本方 origin；Provider URL 在任何模式下仍必须是 HTTPS。"""

import pytest

from xiaowei_agent.contracts import WebMode
from xiaowei_agent.interfaces import web_auth

pytestmark = pytest.mark.security


@pytest.mark.parametrize("mode", [WebMode.LAN_HTTP, WebMode.HTTPS])
def test_provider_authorization_url_is_always_https_only(mode: WebMode) -> None:
    """协议放开只作用于本方 origin；飞书授权 URL 在任何模式下都必须是 HTTPS。"""
    http_url = "http://open.feishu.cn/open-apis/authen/v1/index?state=s"
    assert web_auth._authorization_url_is_safe(http_url, expected_state="s") is False


def test_provider_token_url_helper_rejects_http() -> None:
    assert web_auth._provider_https_url("http://open.feishu.cn/x") is None
    assert web_auth._provider_https_url("https://open.feishu.cn/x") is not None


def test_public_origin_helper_is_mode_aware() -> None:
    assert web_auth.public_origin_is_safe("http://192.168.1.20:8080", mode=WebMode.LAN_HTTP)
    assert not web_auth.public_origin_is_safe("http://192.168.1.20:8080", mode=WebMode.HTTPS)
    assert web_auth.public_origin_is_safe("https://sso.example.com", mode=WebMode.HTTPS)
    assert not web_auth.public_origin_is_safe("https://sso.example.com", mode=WebMode.LAN_HTTP)


def test_the_two_helpers_are_not_the_same_object() -> None:
    """反例：如果实现只是把共用 helper 放宽并起了个别名，这条会红。"""
    assert web_auth._provider_https_url is not web_auth.public_origin_is_safe
    assert "mode" not in web_auth._provider_https_url.__code__.co_varnames
