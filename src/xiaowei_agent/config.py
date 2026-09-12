"""运行配置与 fail-fast 加载。

设计要点（均有承重测试）：

- 使用 ``BaseModel`` 而非 ``BaseSettings``：后者在只传部分构造参数时，
  仍会为未传字段回退读取宿主 ``os.environ``，无法做到来源隔离。
- ``extra="forbid"`` 对未知的进程环境变量无效，因此未知变量由
  :func:`load_settings` 显式扫描并拒绝。
- 校验失败时重建为 :class:`ConfigError`，并在 ``except`` 块**之外**抛出：
  隐式异常链与 ``from e`` 会把非法取值留在完整 traceback 中；而 ``from None``
  只设置 ``__suppress_context__``，``__context__`` 仍持有原始 ValidationError。
- 固定开发租户是常量，不接受任何环境入口（ADR-007 D2）。
"""

import json
import os
from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
from socket import inet_aton
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from xiaowei_agent.redaction import safe_error_details

ENV_PREFIX: Final[str] = "XIAOWEI_"
DEFAULT_TENANT_ID: Final[str] = "dev-local"
_DEFAULT_POSTGRES_SECRET_PATH: Final[str] = "/run/secrets/postgres_" + "password"
_STARROCKS_GATEWAY_TIMEOUT_SECONDS: Final[int] = 30


def _strict_str(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("must not be empty or padded with whitespace")
    return value


StrictStr = Annotated[str, AfterValidator(_strict_str)]


def _sha256_hex(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


Sha256Hex = Annotated[StrictStr, AfterValidator(_sha256_hex)]


def _ip_literal(value: str) -> str:
    try:
        ip_address(value)
    except ValueError:
        raise ValueError("must be an IP address literal") from None
    return value


IpLiteral = Annotated[StrictStr, AfterValidator(_ip_literal)]


def _absolute_path(value: str) -> str:
    if not Path(value).is_absolute():
        raise ValueError("must be an absolute path")
    return value


AbsolutePath = Annotated[StrictStr, AfterValidator(_absolute_path)]


def _canonical_ip_literal(value: str) -> str | None:
    try:
        address = ip_address(value)
    except ValueError:
        try:
            address = ip_address(inet_aton(value))
        except (OSError, UnicodeError, ValueError):
            return None
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def _canonical_dns_hostname(value: str) -> str | None:
    try:
        hostname = value.encode("idna").decode("ascii").lower().removesuffix(".")
    except UnicodeError:
        return None
    if not hostname or len(hostname) > 253:
        return None
    labels = hostname.split(".")
    if not all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        return None
    return hostname


def canonical_non_ip_hostname(value: str) -> str | None:
    """规范化 DNS 主机名；标准及 legacy IP 文本一律返回 ``None``。"""
    if _canonical_ip_literal(value) is not None:
        return None
    return _canonical_dns_hostname(value)


def _canonical_origin_hostname(value: str) -> str | None:
    address = _canonical_ip_literal(value)
    return address if address is not None else _canonical_dns_hostname(value)


def _https_origin(value: str) -> str:
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or character.isspace()
        for character in value
    ):
        raise ValueError("must be an HTTPS origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("must be an HTTPS origin") from None
    hostname = (
        None
        if parsed.hostname is None
        else _canonical_origin_hostname(parsed.hostname)
    )
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or hostname is None
        or (port is not None and not 1 <= port <= 65_535)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        raise ValueError("must be an HTTPS origin")
    authority = hostname if port in {None, 443} else f"{hostname}:{port}"
    return f"https://{authority}"


HttpsOrigin = Annotated[StrictStr, AfterValidator(_https_origin)]


class ConfigError(RuntimeError):
    """配置缺失或非法。

    消息只含变量名、字段名与错误类型，绝不含取值。
    """


class Settings(BaseModel):
    """本轮请求之外的进程级配置。不可变。"""

    # 与 Contract 基类同理：环境变量取值可能是凭证一类的敏感串，被拒绝时不得
    # 回填进异常文本。Settings 不是 Contract 子类，所以必须在这里单独声明——
    # 这正是"逐个模型配置会漏"的又一个实例。
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    environment_id: StrictStr
    actor: StrictStr = "local-developer"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    lease_ttl_seconds: int = Field(default=60, gt=0)
    heartbeat_interval_seconds: float = Field(default=10.0, gt=0)
    task_failure_limit: int = Field(default=3, gt=0)
    infrastructure_backoff_base_seconds: float = Field(default=1.0, gt=0)
    infrastructure_backoff_cap_seconds: float = Field(default=30.0, gt=0)
    continuous_infrastructure_failure_window_seconds: float = Field(default=900.0, gt=0)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    dispatch_batch_limit: int = Field(default=10, gt=0, le=100)
    api_request_body_limit_bytes: int = Field(default=131_072, gt=0, lt=1_048_576)
    postgres_host: StrictStr = "postgres"
    postgres_port: int = Field(default=5432, gt=0, le=65_535)
    postgres_database: StrictStr = "xiaowei"
    postgres_user: StrictStr = "xiaowei"
    postgres_password_file: StrictStr = _DEFAULT_POSTGRES_SECRET_PATH
    db_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    db_command_timeout_seconds: float = Field(default=15.0, gt=0)
    db_pool_size: int = Field(default=5, gt=0)
    db_pool_max_overflow: int = Field(default=0, ge=0)
    api_bind_host: IpLiteral = "127.0.0.1"
    api_bind_port: int = Field(default=8000, gt=0, le=65_535)
    smoke_step_barrier: bool = False
    starrocks_adapter_mode: Literal["recording", "test_readonly"] = "recording"
    starrocks_host: StrictStr | None = None
    starrocks_port: int | None = Field(default=None, gt=0, le=65_535)
    starrocks_database: StrictStr | None = None
    starrocks_user: StrictStr | None = None
    starrocks_password_file: StrictStr | None = None
    starrocks_tls_mode: Literal["verify_ca", "verify_identity"] | None = None
    starrocks_ca_file: StrictStr | None = None
    starrocks_server_name: StrictStr | None = None
    starrocks_resource_id: StrictStr | None = None
    starrocks_expected_version_sha256: Sha256Hex | None = None
    starrocks_expected_grants_sha256: Sha256Hex | None = None
    starrocks_expected_ddl_sha256: Sha256Hex | None = None
    starrocks_expected_identity_sha256: Sha256Hex | None = None
    starrocks_expected_metadata_source_ref: StrictStr | None = None
    starrocks_physical_identity_ref: StrictStr | None = None
    starrocks_authorized_actor: StrictStr | None = None
    starrocks_active_from: datetime | None = None
    starrocks_active_until: datetime | None = None
    starrocks_connect_timeout_seconds: int | None = Field(default=None, gt=0, le=30)
    starrocks_read_timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        lt=_STARROCKS_GATEWAY_TIMEOUT_SECONDS,
    )
    starrocks_write_timeout_seconds: int | None = Field(default=None, gt=0, le=30)
    starrocks_query_timeout_seconds: int | None = Field(default=None, gt=0, le=25)
    feishu_listener_enabled: bool = False
    channel_worker_enabled: bool = False
    web_app_enabled: bool = False
    feishu_oauth_enabled: bool = False
    feishu_app_id: StrictStr | None = None
    feishu_app_secret_file: AbsolutePath | None = None
    feishu_tenant_key: StrictStr | None = None
    feishu_bot_open_id: StrictStr | None = None
    feishu_identity_file: AbsolutePath | None = None
    web_detail_base_url: HttpsOrigin | None = None
    feishu_api_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    projection_claim_ttl_seconds: int = Field(default=15, gt=0, le=300)
    projection_batch_limit: int = Field(default=10, gt=0, le=100)
    projection_provider_max_attempts: int = Field(default=3, gt=0, le=10)
    projection_provider_backoff_base_seconds: float = Field(
        default=1.0, gt=0, le=30
    )
    projection_retry_after_cap_seconds: float = Field(default=30.0, gt=0, le=300)
    projection_tenant_concurrency: int = Field(default=2, gt=0, le=2)
    projection_task_poll_base_seconds: float = Field(default=2.0, gt=0, le=60)
    projection_task_poll_cap_seconds: float = Field(default=10.0, gt=0, le=300)
    channel_worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    web_bind_host: IpLiteral = "127.0.0.1"
    web_bind_port: int = Field(default=8080, gt=0, le=65_535)
    web_oauth_state_ttl_seconds: int = Field(default=300, gt=0, le=600)
    web_session_ttl_seconds: int = Field(default=3600, gt=0, le=86_400)
    gemini_enabled: bool = False

    @model_validator(mode="after")
    def _worker_timings_are_consistent(self) -> "Settings":
        if self.heartbeat_interval_seconds >= self.lease_ttl_seconds / 2:
            raise ValueError("heartbeat interval must be less than half the lease ttl")
        if self.infrastructure_backoff_cap_seconds < self.infrastructure_backoff_base_seconds:
            raise ValueError("infrastructure backoff cap must not be below its base")
        if (
            self.continuous_infrastructure_failure_window_seconds
            <= self.infrastructure_backoff_cap_seconds
        ):
            raise ValueError("infrastructure failure window must exceed the backoff cap")
        if self.worker_poll_interval_seconds > self.lease_ttl_seconds / 4:
            raise ValueError("worker poll interval must not exceed one quarter of the lease ttl")
        if self.db_connect_timeout_seconds >= self.db_command_timeout_seconds:
            raise ValueError("database connect timeout must be below command timeout")
        if self.db_command_timeout_seconds >= self.lease_ttl_seconds:
            raise ValueError("database command timeout must be below lease ttl")
        return self

    @model_validator(mode="after")
    def _projection_timings_are_consistent(self) -> "Settings":
        if (
            self.projection_claim_ttl_seconds
            <= self.feishu_api_timeout_seconds + 2
        ):
            raise ValueError(
                "projection claim ttl must exceed the API timeout and commit margin"
            )
        if (
            self.projection_retry_after_cap_seconds
            < self.projection_provider_backoff_base_seconds
        ):
            raise ValueError("projection provider backoff cap must not be below its base")
        if (
            self.projection_task_poll_cap_seconds
            < self.projection_task_poll_base_seconds
        ):
            raise ValueError("projection task poll cap must not be below its base")
        if (
            self.channel_worker_poll_interval_seconds
            > self.projection_claim_ttl_seconds / 4
        ):
            raise ValueError(
                "projection worker poll interval must not exceed one quarter of claim ttl"
            )
        return self

    @model_validator(mode="after")
    def _starrocks_profile_is_closed(self) -> "Settings":
        live_fields = {
            "host": self.starrocks_host,
            "port": self.starrocks_port,
            "database": self.starrocks_database,
            "user": self.starrocks_user,
            "password_file": self.starrocks_password_file,
            "tls_mode": self.starrocks_tls_mode,
            "ca_file": self.starrocks_ca_file,
            "server_name": self.starrocks_server_name,
            "resource_id": self.starrocks_resource_id,
            "expected_version_sha256": self.starrocks_expected_version_sha256,
            "expected_grants_sha256": self.starrocks_expected_grants_sha256,
            "expected_ddl_sha256": self.starrocks_expected_ddl_sha256,
            "expected_identity_sha256": self.starrocks_expected_identity_sha256,
            "expected_metadata_source_ref": self.starrocks_expected_metadata_source_ref,
            "physical_identity_ref": self.starrocks_physical_identity_ref,
            "authorized_actor": self.starrocks_authorized_actor,
            "active_from": self.starrocks_active_from,
            "active_until": self.starrocks_active_until,
            "connect_timeout_seconds": self.starrocks_connect_timeout_seconds,
            "read_timeout_seconds": self.starrocks_read_timeout_seconds,
            "write_timeout_seconds": self.starrocks_write_timeout_seconds,
            "query_timeout_seconds": self.starrocks_query_timeout_seconds,
        }
        if self.starrocks_adapter_mode == "recording":
            if any(value is not None for value in live_fields.values()):
                raise ValueError("recording mode must not carry live StarRocks configuration")
            return self

        if any(value is None for value in live_fields.values()):
            raise ValueError("test_readonly mode requires the complete StarRocks profile")
        if self.environment_id != "test":
            raise ValueError("test_readonly mode requires the test environment")
        if self.actor != self.starrocks_authorized_actor:
            raise ValueError("test_readonly mode requires the authorized actor")
        if (
            self.starrocks_tls_mode == "verify_identity"
            and self.starrocks_host != self.starrocks_server_name
        ):
            raise ValueError("StarRocks verify_identity requires host to match server name")
        if self.starrocks_active_from is None or self.starrocks_active_until is None:
            raise ValueError("test_readonly mode requires an activation window")
        if (
            self.starrocks_active_from.utcoffset() is None
            or self.starrocks_active_until.utcoffset() is None
        ):
            raise ValueError("StarRocks activation window must include a timezone")
        if self.starrocks_active_from >= self.starrocks_active_until:
            raise ValueError("StarRocks activation window must increase")
        if (
            self.starrocks_read_timeout_seconds is None
            or self.starrocks_connect_timeout_seconds is None
            or self.starrocks_write_timeout_seconds is None
            or self.starrocks_query_timeout_seconds is None
        ):
            raise ValueError("test_readonly mode requires complete timeout settings")
        if self.starrocks_connect_timeout_seconds > self.starrocks_read_timeout_seconds:
            raise ValueError("StarRocks connect timeout must not exceed read timeout")
        if self.starrocks_write_timeout_seconds > self.starrocks_read_timeout_seconds:
            raise ValueError("StarRocks write timeout must not exceed read timeout")
        if self.starrocks_query_timeout_seconds > self.starrocks_read_timeout_seconds:
            raise ValueError("StarRocks server timeout must not exceed read timeout")
        return self

    @model_validator(mode="after")
    def _feishu_profiles_are_closed(self) -> "Settings":
        if self.web_app_enabled != self.feishu_oauth_enabled:
            raise ValueError("Web app and Feishu OAuth must be enabled together")
        shared = (self.feishu_app_id, self.feishu_app_secret_file)
        listener_only = (self.feishu_tenant_key, self.feishu_bot_open_id)
        identity = (self.feishu_identity_file,)
        web_origin = (self.web_detail_base_url,)
        live_profile = shared + listener_only + identity + web_origin
        if not (
            self.feishu_listener_enabled
            or self.channel_worker_enabled
            or self.web_app_enabled
        ):
            if any(value is not None for value in live_profile):
                raise ValueError(
                    "disabled Feishu listener, channel worker, and Web app must not "
                    "carry live configuration"
                )
            return self
        if self.feishu_listener_enabled:
            if any(value is None for value in shared + listener_only + identity):
                raise ValueError(
                    "enabled listener requires the complete Feishu listener profile"
                )
        elif any(value is not None for value in listener_only):
            raise ValueError(
                "disabled Feishu listener must not carry listener configuration"
            )
        if self.channel_worker_enabled:
            if any(value is None for value in shared + web_origin):
                raise ValueError(
                    "enabled worker requires the complete channel worker profile"
                )
        if self.web_app_enabled:
            if any(value is None for value in shared + identity + web_origin):
                raise ValueError(
                    "enabled Web app requires the complete Web authentication profile"
                )
            origin_hostname = urlsplit(str(self.web_detail_base_url)).hostname
            if canonical_non_ip_hostname(origin_hostname or "") is None:
                raise ValueError("OAuth Web origin requires a hostname")
        if not self.feishu_listener_enabled and not self.web_app_enabled and any(
            value is not None for value in identity
        ):
            raise ValueError(
                "disabled Web app and Feishu listener must not carry identity configuration"
            )
        if not self.channel_worker_enabled and not self.web_app_enabled and any(
            value is not None for value in web_origin
        ):
            raise ValueError(
                "disabled Web app and channel worker must not carry Web origin"
            )
        return self

    @property
    def tenant_id(self) -> str:
        """固定开发租户；常量，不可由环境覆盖。"""
        return DEFAULT_TENANT_ID

    def starrocks_config_revision(
        self,
        *,
        driver_version: str,
        normalizer_version: str,
        sql_surface_ref: str,
    ) -> str:
        """计算真实 StarRocks profile 的稳定摘要，不读取 credential 文件内容。"""
        if self.starrocks_adapter_mode != "test_readonly":
            raise ValueError("StarRocks config revision requires test_readonly mode")
        runtime_refs = {
            "driver_version": _strict_str(driver_version),
            "normalizer_version": _strict_str(normalizer_version),
            "sql_surface_ref": _strict_str(sql_surface_ref),
        }
        profile = {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in self.model_dump(mode="python").items()
            if name.startswith("starrocks_")
        }
        canonical = json.dumps(
            {
                "actor": self.actor,
                "environment_id": self.environment_id,
                "profile": profile,
                "runtime": runtime_refs,
                "tenant_id": self.tenant_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(canonical).hexdigest()


_FIELD_TO_ENV: Final[Mapping[str, str]] = {
    "environment_id": "XIAOWEI_ENVIRONMENT_ID",
    "actor": "XIAOWEI_ACTOR",
    "log_level": "XIAOWEI_LOG_LEVEL",
    "lease_ttl_seconds": "XIAOWEI_LEASE_TTL_SECONDS",
    "heartbeat_interval_seconds": "XIAOWEI_HEARTBEAT_INTERVAL_SECONDS",
    "task_failure_limit": "XIAOWEI_TASK_FAILURE_LIMIT",
    "infrastructure_backoff_base_seconds": "XIAOWEI_INFRASTRUCTURE_BACKOFF_BASE_SECONDS",
    "infrastructure_backoff_cap_seconds": "XIAOWEI_INFRASTRUCTURE_BACKOFF_CAP_SECONDS",
    "continuous_infrastructure_failure_window_seconds": (
        "XIAOWEI_CONTINUOUS_INFRASTRUCTURE_FAILURE_WINDOW_SECONDS"
    ),
    "worker_poll_interval_seconds": "XIAOWEI_WORKER_POLL_INTERVAL_SECONDS",
    "dispatch_batch_limit": "XIAOWEI_DISPATCH_BATCH_LIMIT",
    "api_request_body_limit_bytes": "XIAOWEI_API_REQUEST_BODY_LIMIT_BYTES",
    "postgres_host": "XIAOWEI_POSTGRES_HOST",
    "postgres_port": "XIAOWEI_POSTGRES_PORT",
    "postgres_database": "XIAOWEI_POSTGRES_DATABASE",
    "postgres_user": "XIAOWEI_POSTGRES_USER",
    "postgres_password_file": "XIAOWEI_POSTGRES_PASSWORD_FILE",
    "db_connect_timeout_seconds": "XIAOWEI_DB_CONNECT_TIMEOUT_SECONDS",
    "db_command_timeout_seconds": "XIAOWEI_DB_COMMAND_TIMEOUT_SECONDS",
    "db_pool_size": "XIAOWEI_DB_POOL_SIZE",
    "db_pool_max_overflow": "XIAOWEI_DB_POOL_MAX_OVERFLOW",
    "api_bind_host": "XIAOWEI_API_BIND_HOST",
    "api_bind_port": "XIAOWEI_API_BIND_PORT",
    "smoke_step_barrier": "XIAOWEI_SMOKE_STEP_BARRIER",
    "starrocks_adapter_mode": "XIAOWEI_STARROCKS_ADAPTER_MODE",
    "starrocks_host": "XIAOWEI_STARROCKS_HOST",
    "starrocks_port": "XIAOWEI_STARROCKS_PORT",
    "starrocks_database": "XIAOWEI_STARROCKS_DATABASE",
    "starrocks_user": "XIAOWEI_STARROCKS_USER",
    "starrocks_password_file": "XIAOWEI_STARROCKS_PASSWORD_FILE",
    "starrocks_tls_mode": "XIAOWEI_STARROCKS_TLS_MODE",
    "starrocks_ca_file": "XIAOWEI_STARROCKS_CA_FILE",
    "starrocks_server_name": "XIAOWEI_STARROCKS_SERVER_NAME",
    "starrocks_resource_id": "XIAOWEI_STARROCKS_RESOURCE_ID",
    "starrocks_expected_version_sha256": "XIAOWEI_STARROCKS_EXPECTED_VERSION_SHA256",
    "starrocks_expected_grants_sha256": "XIAOWEI_STARROCKS_EXPECTED_GRANTS_SHA256",
    "starrocks_expected_ddl_sha256": "XIAOWEI_STARROCKS_EXPECTED_DDL_SHA256",
    "starrocks_expected_identity_sha256": "XIAOWEI_STARROCKS_EXPECTED_IDENTITY_SHA256",
    "starrocks_expected_metadata_source_ref": (
        "XIAOWEI_STARROCKS_EXPECTED_METADATA_SOURCE_REF"
    ),
    "starrocks_physical_identity_ref": "XIAOWEI_STARROCKS_PHYSICAL_IDENTITY_REF",
    "starrocks_authorized_actor": "XIAOWEI_STARROCKS_AUTHORIZED_ACTOR",
    "starrocks_active_from": "XIAOWEI_STARROCKS_ACTIVE_FROM",
    "starrocks_active_until": "XIAOWEI_STARROCKS_ACTIVE_UNTIL",
    "starrocks_connect_timeout_seconds": "XIAOWEI_STARROCKS_CONNECT_TIMEOUT_SECONDS",
    "starrocks_read_timeout_seconds": "XIAOWEI_STARROCKS_READ_TIMEOUT_SECONDS",
    "starrocks_write_timeout_seconds": "XIAOWEI_STARROCKS_WRITE_TIMEOUT_SECONDS",
    "starrocks_query_timeout_seconds": "XIAOWEI_STARROCKS_QUERY_TIMEOUT_SECONDS",
    "feishu_listener_enabled": "XIAOWEI_FEISHU_LISTENER_ENABLED",
    "channel_worker_enabled": "XIAOWEI_CHANNEL_WORKER_ENABLED",
    "web_app_enabled": "XIAOWEI_WEB_APP_ENABLED",
    "feishu_oauth_enabled": "XIAOWEI_FEISHU_OAUTH_ENABLED",
    "feishu_app_id": "XIAOWEI_FEISHU_APP_ID",
    "feishu_app_secret_file": "XIAOWEI_FEISHU_APP_SECRET_FILE",
    "feishu_tenant_key": "XIAOWEI_FEISHU_TENANT_KEY",
    "feishu_bot_open_id": "XIAOWEI_FEISHU_BOT_OPEN_ID",
    "feishu_identity_file": "XIAOWEI_FEISHU_IDENTITY_FILE",
    "web_detail_base_url": "XIAOWEI_WEB_DETAIL_BASE_URL",
    "feishu_api_timeout_seconds": "XIAOWEI_FEISHU_API_TIMEOUT_SECONDS",
    "projection_claim_ttl_seconds": "XIAOWEI_PROJECTION_CLAIM_TTL_SECONDS",
    "projection_batch_limit": "XIAOWEI_PROJECTION_BATCH_LIMIT",
    "projection_provider_max_attempts": (
        "XIAOWEI_PROJECTION_PROVIDER_MAX_ATTEMPTS"
    ),
    "projection_provider_backoff_base_seconds": (
        "XIAOWEI_PROJECTION_PROVIDER_BACKOFF_BASE_SECONDS"
    ),
    "projection_retry_after_cap_seconds": (
        "XIAOWEI_PROJECTION_RETRY_AFTER_CAP_SECONDS"
    ),
    "projection_tenant_concurrency": "XIAOWEI_PROJECTION_TENANT_CONCURRENCY",
    "projection_task_poll_base_seconds": (
        "XIAOWEI_PROJECTION_TASK_POLL_BASE_SECONDS"
    ),
    "projection_task_poll_cap_seconds": (
        "XIAOWEI_PROJECTION_TASK_POLL_CAP_SECONDS"
    ),
    "channel_worker_poll_interval_seconds": (
        "XIAOWEI_CHANNEL_WORKER_POLL_INTERVAL_SECONDS"
    ),
    "web_bind_host": "XIAOWEI_WEB_BIND_HOST",
    "web_bind_port": "XIAOWEI_WEB_BIND_PORT",
    "web_oauth_state_ttl_seconds": "XIAOWEI_WEB_OAUTH_STATE_TTL_SECONDS",
    "web_session_ttl_seconds": "XIAOWEI_WEB_SESSION_TTL_SECONDS",
    "gemini_enabled": "XIAOWEI_GEMINI_ENABLED",
}


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """从环境映射构建 :class:`Settings`，缺失或非法即抛 :class:`ConfigError`。

    :param env: 显式环境映射；``None`` 时取 ``os.environ``。传入时绝不回退宿主环境。
    :raises ConfigError: 存在未知 ``XIAOWEI_*`` 变量，或字段缺失/非法。
    """
    source: Mapping[str, str] = os.environ if env is None else env
    prefixed = {k: v for k, v in source.items() if k.upper().startswith(ENV_PREFIX)}

    known = set(_FIELD_TO_ENV.values())
    unknown = sorted(k for k in prefixed if k not in known)
    if unknown:
        raise ConfigError(f"未知配置变量: {unknown}") from None

    kwargs = {
        field: prefixed[name]
        for field, name in _FIELD_TO_ENV.items()
        if name in prefixed
        and not (
            (
                field.startswith("starrocks_")
                or field.startswith("feishu_")
                or field == "web_detail_base_url"
            )
            and not prefixed[name]
        )
    }
    detail: str | None = None
    try:
        return Settings.model_validate(kwargs)
    except ValidationError as exc:
        # 经 safe_error_details 投影：绝不能把 exc.errors() 的 input 带出去。
        detail = "; ".join(safe_error_details(exc))
    # 在 except 块之外抛出：`from None` 只会设置 __suppress_context__，
    # 不会清空 __context__，原始 ValidationError（含非法取值）仍会挂在异常对象上。
    raise ConfigError(f"配置非法: {detail}")
