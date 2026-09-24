"""W4b resources 域契约的完整正反表。

只回答"字符串与字段组合是否合法"：不解析 DNS、不判断公网/私网、不探测端口。
每条反例都是一次被拒绝的构造；正例证明边界值本身可用。
"""

from typing import Any, Final

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts.resource_config import (
    MAX_RESOURCES,
    PrometheusResource,
    ResourcesConfig,
    StarRocksResource,
)

_PASSWORD: Final = "sr-" + "fake-password"
_TOKEN: Final = "prom-" + "fake-token"
_ID_A: Final = "a" * 32
_ID_B: Final = "b" * 32


def _starrocks(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": "starrocks",
        "resource_id": _ID_A,
        "environment": "prod",
        "display_name": "核心 StarRocks",
        "host": "sr-fe.prod.example.internal",
        "port": 9030,
        "database": "ods",
        "username": "readonly_user",
        "password": _PASSWORD,
        "tls_mode": "verify_identity",
        "enabled": True,
    }
    value.update(updates)
    return value


def _prometheus(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": "prometheus",
        "resource_id": _ID_B,
        "environment": "prod",
        "display_name": "主 Prometheus",
        "base_url": "https://prom.example.internal/prometheus",
        "auth_mode": "bearer",
        "secret": _TOKEN,
        "tls_mode": "verify_ca",
        "enabled": True,
    }
    value.update(updates)
    return value


def _without(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in keys}


# --------------------------------------------------------------------------
# StarRocks
# --------------------------------------------------------------------------


def test_a_complete_starrocks_resource_is_accepted() -> None:
    resource = StarRocksResource.model_validate(_starrocks())
    assert resource.port == 9030
    assert resource.password == _PASSWORD


@pytest.mark.parametrize(
    "host",
    [
        "a",
        "sr-fe-01",
        "sr-fe.prod.example.internal",
        "x" * 63 + ".example",
        ".".join(["a" * 63] * 3) + "." + "b" * 61,  # 253 字符
        "10.0.0.1",
        "255.255.255.255",
        "::1",
        "fd00::10",
        "2001:db8::1",
    ],
)
def test_starrocks_host_accepts_hostnames_and_ip_literals(host: str) -> None:
    assert StarRocksResource.model_validate(_starrocks(host=host)).host == host


@pytest.mark.parametrize(
    "host",
    [
        "",
        "http://sr-fe",
        "sr-fe:9030",
        "sr-fe/path",
        "user@sr-fe",
        "[::1]",
        "fe80::1%eth0",
        "sr fe",
        "sr-fe\n",
        "sr-fe\x00",
        "-sr",
        "sr-",
        "sr_fe",
        "sr..fe",
        "sr-fe.",
        "x" * 64,
        ".".join(["a" * 63] * 4),
        "999.1.1.1",
        "10.0.0",
        "数据库.example",
        "xn--",
    ],
)
def test_starrocks_host_rejects_anything_but_a_bare_host(host: str) -> None:
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_starrocks(host=host))


@pytest.mark.parametrize("port", [1, 65535])
def test_starrocks_port_boundaries_are_accepted(port: int) -> None:
    assert StarRocksResource.model_validate(_starrocks(port=port)).port == port


@pytest.mark.parametrize("port", [0, 65536, -1, "9030", 9030.0, True])
def test_starrocks_port_outside_range_or_not_int_is_rejected(port: object) -> None:
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_starrocks(port=port))


@pytest.mark.parametrize("field", ["display_name", "database", "username"])
@pytest.mark.parametrize("length", [1, 128])
def test_bounded_text_fields_accept_their_boundaries(field: str, length: int) -> None:
    value = "d" * length
    assert getattr(StarRocksResource.model_validate(_starrocks(**{field: value})), field) == value


@pytest.mark.parametrize("field", ["display_name", "database", "username"])
@pytest.mark.parametrize("value", ["", " ", "d" * 129, "a\nb", "a\x7fb", "a\tb"])
def test_bounded_text_fields_reject_empty_oversize_and_control(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_starrocks(**{field: value}))


@pytest.mark.parametrize("tls_mode", ["disabled", "verify_ca", "verify_identity"])
def test_starrocks_accepts_every_closed_tls_mode(tls_mode: str) -> None:
    assert StarRocksResource.model_validate(_starrocks(tls_mode=tls_mode)).tls_mode == tls_mode


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tls_mode", "required"),
        ("environment", "production"),
        ("environment", "PROD"),
        ("kind", "mysql"),
        ("enabled", "true"),
        ("enabled", 1),
    ],
)
def test_closed_sets_and_strict_types_reject_other_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_starrocks(**{field: value}))


@pytest.mark.parametrize("environment", ["dev", "test", "staging", "prod"])
def test_every_closed_environment_is_accepted(environment: str) -> None:
    resource = StarRocksResource.model_validate(_starrocks(environment=environment))
    assert resource.environment == environment


@pytest.mark.parametrize(
    "resource_id",
    ["", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 31 + "-", " " + "a" * 31],
)
def test_resource_id_is_exactly_32_lowercase_hex(resource_id: str) -> None:
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_starrocks(resource_id=resource_id))


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_starrocks(sql="SELECT 1"))
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_starrocks(driver="mysql"))


@pytest.mark.parametrize("field", ["host", "port", "database", "username", "tls_mode"])
def test_starrocks_required_fields_are_required(field: str) -> None:
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_without(_starrocks(), field))


@pytest.mark.parametrize("password", ["", "a\nb", "x" * 4097])
def test_starrocks_password_must_be_a_bounded_non_control_secret(password: str) -> None:
    with pytest.raises(ValidationError):
        StarRocksResource.model_validate(_starrocks(password=password))


def test_a_cleared_starrocks_password_is_representable_but_not_configured() -> None:
    """清除 Secret 是独立确认动作：清除后资源仍在，只是 ``configured`` 为假。"""
    resource = StarRocksResource.model_validate(_without(_starrocks(), "password"))
    assert resource.password is None
    assert resource.secret_configured is False
    assert StarRocksResource.model_validate(_starrocks()).secret_configured is True


# --------------------------------------------------------------------------
# Prometheus
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "tls_mode"),
    [
        ("http://prom.example.internal", "disabled"),
        ("http://prom.example.internal:9090", "disabled"),
        ("http://10.0.0.8:9090/", "disabled"),
        ("https://prom.example.internal", "verify_ca"),
        ("https://prom.example.internal/prometheus", "verify_identity"),
        ("https://prom.example.internal/a/b-c/d_e.f~g/", "verify_identity"),
        ("https://[2001:db8::1]:9443/prom", "verify_ca"),
    ],
)
def test_prometheus_accepts_closed_scheme_and_tls_combinations(
    base_url: str, tls_mode: str
) -> None:
    resource = PrometheusResource.model_validate(
        _prometheus(base_url=base_url, tls_mode=tls_mode)
    )
    assert resource.base_url == base_url


@pytest.mark.parametrize(
    ("base_url", "tls_mode"),
    [
        ("http://prom.example.internal", "verify_ca"),
        ("http://prom.example.internal", "verify_identity"),
        ("https://prom.example.internal", "disabled"),
    ],
)
def test_prometheus_rejects_mismatched_scheme_and_tls(base_url: str, tls_mode: str) -> None:
    with pytest.raises(ValidationError):
        PrometheusResource.model_validate(_prometheus(base_url=base_url, tls_mode=tls_mode))


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "prom.example.internal",
        "ftp://prom.example.internal",
        "HTTPS://prom.example.internal",
        "file:///etc/passwd",
        "https://user:pw@prom.example.internal",
        "https://user@prom.example.internal",
        "https://prom.example.internal/?q=1",
        "https://prom.example.internal/?",
        "https://prom.example.internal/#frag",
        "https://prom.example.internal/#",
        "https://prom.example.internal/a b",
        "https://prom.example.internal/\n",
        "https://prom.example.internal\x00",
        "https://prom.example.internal/%2e%2e/",
        "https://prom.example.internal/../admin",
        "https://prom.example.internal/./x",
        "https://prom.example.internal//x",
        "https://prom.example.internal/a;b",
        "https://",
        "https://:9090",
        "https://prom.example.internal:0",
        "https://prom.example.internal:65536",
        "https://prom.example.internal:abc",
        "https://prom_example",
        "https://2001:db8::1/",
        "https://[fe80::1%25eth0]/",
        "https://" + "a" * 64 + ".example",
        "https://prom.example.internal/" + "p" * 2048,
    ],
)
def test_prometheus_base_url_rejects_everything_but_a_plain_http_origin_and_path(
    base_url: str,
) -> None:
    tls_mode = "disabled" if base_url.startswith("http://") else "verify_ca"
    with pytest.raises(ValidationError):
        PrometheusResource.model_validate(_prometheus(base_url=base_url, tls_mode=tls_mode))


def test_basic_auth_requires_a_username_and_accepts_a_secret() -> None:
    resource = PrometheusResource.model_validate(
        _prometheus(auth_mode="basic", username="grafana", secret=_TOKEN)
    )
    assert resource.username == "grafana"
    assert resource.secret_configured is True


@pytest.mark.parametrize(
    "updates",
    [
        {"auth_mode": "basic"},  # 缺 username
        {"auth_mode": "bearer", "username": "grafana"},  # bearer 不接受 username
        {"auth_mode": "none"},  # none 不接受 secret
        {"auth_mode": "none", "username": "grafana", "secret": None},
        {"auth_mode": "digest"},
        {"auth_mode": "basic", "username": ""},
    ],
)
def test_prometheus_auth_combinations_are_closed(updates: dict[str, Any]) -> None:
    value = _prometheus(**updates)
    if value.get("secret") is None:
        value = _without(value, "secret")
    with pytest.raises(ValidationError):
        PrometheusResource.model_validate(value)


def test_auth_none_without_secret_or_username_is_accepted() -> None:
    resource = PrometheusResource.model_validate(
        _without(_prometheus(auth_mode="none"), "secret")
    )
    assert resource.secret is None
    assert resource.secret_configured is False


def test_a_cleared_bearer_secret_is_representable_but_not_configured() -> None:
    resource = PrometheusResource.model_validate(_without(_prometheus(), "secret"))
    assert resource.secret_configured is False


# --------------------------------------------------------------------------
# 文档
# --------------------------------------------------------------------------


def test_an_empty_resource_list_is_a_valid_document() -> None:
    config = ResourcesConfig.model_validate({"generation": 1, "resources": []})
    assert config.resources == ()


@pytest.mark.parametrize("generation", [0, -1, "1", True])
def test_generation_is_a_positive_int(generation: object) -> None:
    with pytest.raises(ValidationError):
        ResourcesConfig.model_validate({"generation": generation, "resources": []})


def test_a_mixed_document_keeps_the_discriminated_kinds() -> None:
    config = ResourcesConfig.model_validate(
        {"generation": 3, "resources": [_starrocks(), _prometheus()]}
    )
    assert [type(item) for item in config.resources] == [StarRocksResource, PrometheusResource]


def test_resource_ids_are_unique_across_kinds() -> None:
    with pytest.raises(ValidationError):
        ResourcesConfig.model_validate(
            {"generation": 1, "resources": [_starrocks(), _prometheus(resource_id=_ID_A)]}
        )


def _many(count: int) -> list[dict[str, Any]]:
    return [_starrocks(resource_id=f"{index:032x}") for index in range(count)]


def test_one_hundred_resources_are_accepted() -> None:
    assert MAX_RESOURCES == 100
    config = ResourcesConfig.model_validate({"generation": 1, "resources": _many(100)})
    assert len(config.resources) == 100


def test_one_hundred_and_one_resources_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ResourcesConfig.model_validate({"generation": 1, "resources": _many(101)})


def test_unknown_document_fields_and_unknown_kinds_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ResourcesConfig.model_validate({"generation": 1, "resources": [], "extra": 1})
    with pytest.raises(ValidationError):
        ResourcesConfig.model_validate(
            {"generation": 1, "resources": [_starrocks(kind="kafka")]}
        )


# --------------------------------------------------------------------------
# Secret 可见性
# --------------------------------------------------------------------------


def test_secrets_never_appear_in_repr_str_or_dump() -> None:
    config = ResourcesConfig.model_validate(
        {
            "generation": 2,
            "resources": [
                _starrocks(),
                _prometheus(auth_mode="basic", username="grafana"),
            ],
        }
    )
    rendered = (
        repr(config),
        str(config),
        repr(config.resources[0]),
        str(config.resources[1]),
        config.model_dump_json(),
        str(config.model_dump()),
    )
    for text in rendered:
        assert _PASSWORD not in text
        assert _TOKEN not in text


def test_validation_errors_never_echo_a_rejected_secret() -> None:
    bad = "tok\nen-" + "leak"
    with pytest.raises(ValidationError) as caught:
        PrometheusResource.model_validate(_prometheus(secret=bad))
    # 与全项目同一口径：``str(exc)`` 不回显输入；``errors()`` 从不越过文件边界。
    assert "leak" not in str(caught.value)
