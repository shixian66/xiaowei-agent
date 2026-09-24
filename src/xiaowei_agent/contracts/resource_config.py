"""W4b resources 域契约：StarRocks 与 Prometheus 的闭集登记参数。

这里只回答"字符串与字段组合是否合法"。**纯语法**：不解析 DNS、不判断公网/私网、
不探测端口、不发 HTTP、不登录——``ipaddress`` 与 ``urllib.parse.urlsplit`` 都是纯函数。
W4c 才决定真实目标的网络策略与唯一调用路径；登记一个资源不等于把它注册成 capability
target。

组合规则（scheme 与 TLS、认证方式与 username/secret）只在本模块定义一次；Web 请求模型
不另抄一份较宽的规则，而是把合并后的结果交回这里重新校验。

Secret 字段与 Provider Secret 同一规则：``SecretRef`` + ``exclude=True`` + ``repr=False``。
清除 Secret 是独立确认动作，清除后资源仍在、``secret_configured`` 为假；新建时是否必须
带 Secret 由写服务按 kind 判定（见 :func:`secret_required_on_create`）。
"""

import ipaddress
import re
import unicodedata
from typing import Annotated, Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, Field, field_validator, model_validator

from xiaowei_agent.contracts.base import Contract, StrictInt, StrictStr
from xiaowei_agent.contracts.integration_config import SecretRef

MAX_RESOURCES: Final[int] = 100
_MAX_TEXT: Final[int] = 128
_MAX_HOSTNAME: Final[int] = 253
_MAX_BASE_URL: Final[int] = 2048

_RESOURCE_ID_RE: Final = re.compile(r"[0-9a-f]{32}")
_LABEL_RE: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_PATH_SEGMENT_RE: Final = re.compile(r"[A-Za-z0-9_~-][A-Za-z0-9._~-]*")

ResourceEnvironment = Literal["dev", "test", "staging", "prod"]
TlsMode = Literal["disabled", "verify_ca", "verify_identity"]
PrometheusAuthMode = Literal["none", "basic", "bearer"]


def _resource_id(value: str) -> str:
    if _RESOURCE_ID_RE.fullmatch(value) is None:
        raise ValueError("resource id must be 32 lowercase hex characters")
    return value


def _bounded_text(value: str) -> str:
    if not value.strip() or len(value) > _MAX_TEXT:
        raise ValueError("must be 1..128 visible characters")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("must not contain control characters")
    return value


def is_valid_host(value: str) -> bool:
    """ASCII hostname、IPv4 或不带方括号的 IPv6 字面量；只做语法判断。"""
    if not value or not value.isascii():
        return False
    if "%" in value:
        # IPv6 zone id 会把"哪块网卡"带进参数；登记层不接受。
        return False
    if ":" in value:
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            return False
        return True
    labels = value.split(".")
    if all(label.isdigit() for label in labels):
        # 全数字只可能是 IPv4；``999.1.1.1`` 或 ``10.0.0`` 不能被当成主机名放行。
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            return False
        return True
    if len(value) > _MAX_HOSTNAME:
        return False
    return all(_LABEL_RE.fullmatch(label) is not None for label in labels) and not any(
        label.lower().startswith("xn--") and len(label) == 4 for label in labels
    )


def _host(value: str) -> str:
    if not is_valid_host(value):
        raise ValueError("must be a bare hostname or IP literal")
    return value


def _base_url(value: str) -> str:
    if not value or len(value) > _MAX_BASE_URL or not value.isascii():
        raise ValueError("invalid base url")
    if any(character in value for character in "?#@;%\\") or any(
        not character.isprintable() or character.isspace() for character in value
    ):
        raise ValueError("invalid base url")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not value.startswith(f"{parts.scheme}://"):
        raise ValueError("invalid base url")
    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError("invalid base url") from None
    if not hostname or not is_valid_host(hostname):
        raise ValueError("invalid base url")
    if ":" in hostname and f"[{hostname}]" not in parts.netloc:
        raise ValueError("invalid base url")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid base url")
    if parts.netloc.endswith(":"):
        raise ValueError("invalid base url")
    path = parts.path
    if path:
        segments = path[1:].split("/")
        if segments and segments[-1] == "":
            segments = segments[:-1]
        if any(
            _PATH_SEGMENT_RE.fullmatch(segment) is None or segment in {".", ".."}
            for segment in segments
        ):
            raise ValueError("invalid base url")
    return value


ResourceId = Annotated[StrictStr, AfterValidator(_resource_id)]
BoundedText = Annotated[StrictStr, AfterValidator(_bounded_text)]
Host = Annotated[StrictStr, AfterValidator(_host)]
BaseUrl = Annotated[StrictStr, AfterValidator(_base_url)]


class StarRocksResource(Contract):
    """一个 StarRocks FE 的登记参数。"""

    kind: Literal["starrocks"]
    resource_id: ResourceId
    environment: ResourceEnvironment
    display_name: BoundedText
    host: Host
    port: StrictInt = Field(ge=1, le=65535)
    database: BoundedText
    username: BoundedText
    password: SecretRef | None = Field(default=None, exclude=True, repr=False)
    tls_mode: TlsMode
    enabled: bool

    @property
    def secret_configured(self) -> bool:
        return self.password is not None


class PrometheusResource(Contract):
    """一个 Prometheus 查询端点的登记参数。"""

    kind: Literal["prometheus"]
    resource_id: ResourceId
    environment: ResourceEnvironment
    display_name: BoundedText
    base_url: BaseUrl
    auth_mode: PrometheusAuthMode
    username: BoundedText | None = None
    secret: SecretRef | None = Field(default=None, exclude=True, repr=False)
    tls_mode: TlsMode
    enabled: bool

    @property
    def secret_configured(self) -> bool:
        return self.secret is not None

    @model_validator(mode="after")
    def _combinations_are_closed(self) -> Self:
        https = self.base_url.startswith("https://")
        if https == (self.tls_mode == "disabled"):
            raise ValueError("http requires disabled tls; https requires verification")
        if (self.auth_mode == "basic") != (self.username is not None):
            raise ValueError("username is required for basic auth and forbidden otherwise")
        if self.auth_mode == "none" and self.secret is not None:
            raise ValueError("auth mode none forbids a secret")
        return self


Resource = Annotated[StarRocksResource | PrometheusResource, Field(discriminator="kind")]


def secret_required_on_create(resource: StarRocksResource | PrometheusResource) -> bool:
    """新建时是否必须带 Secret：StarRocks 恒需要；Prometheus 只有 basic/bearer 需要。

    清除 Secret 之后资源允许处于"未配置 Secret"的状态，所以这条要求不能放进契约本身。
    """
    if isinstance(resource, StarRocksResource):
        return True
    return resource.auth_mode != "none"


class ResourcesConfig(Contract):
    """resources 域的完整文档；与 AI/飞书代次互不相干。"""

    generation: StrictInt = Field(gt=0)
    resources: tuple[Resource, ...] = Field(max_length=MAX_RESOURCES)

    @field_validator("resources", mode="before")
    @classmethod
    def _json_array_is_a_tuple(cls, value: object) -> object:
        """文件里的 JSON 数组是 ``list``；只在这一处把它转成不可变 tuple，元素仍严格校验。"""
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _resource_ids_are_unique(self) -> Self:
        identifiers = [resource.resource_id for resource in self.resources]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("resource ids must be unique")
        return self


__all__ = [
    "MAX_RESOURCES",
    "PrometheusAuthMode",
    "PrometheusResource",
    "Resource",
    "ResourceEnvironment",
    "ResourcesConfig",
    "StarRocksResource",
    "TlsMode",
    "is_valid_host",
    "secret_required_on_create",
]
