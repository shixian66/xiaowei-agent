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

import os
from collections.abc import Mapping
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from xiaowei_agent.redaction import safe_error_details

ENV_PREFIX: Final[str] = "XIAOWEI_"
DEFAULT_TENANT_ID: Final[str] = "dev-local"


def _strict_str(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("must not be empty or padded with whitespace")
    return value


StrictStr = Annotated[str, AfterValidator(_strict_str)]


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
    smoke_step_barrier: bool = False

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
        return self

    @property
    def tenant_id(self) -> str:
        """固定开发租户；常量，不可由环境覆盖。"""
        return DEFAULT_TENANT_ID


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
    "smoke_step_barrier": "XIAOWEI_SMOKE_STEP_BARRIER",
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

    kwargs = {field: prefixed[name] for field, name in _FIELD_TO_ENV.items() if name in prefixed}
    detail: str | None = None
    try:
        return Settings.model_validate(kwargs)
    except ValidationError as exc:
        # 经 safe_error_details 投影：绝不能把 exc.errors() 的 input 带出去。
        detail = "; ".join(safe_error_details(exc))
    # 在 except 块之外抛出：`from None` 只会设置 __suppress_context__，
    # 不会清空 __context__，原始 ValidationError（含非法取值）仍会挂在异常对象上。
    raise ConfigError(f"配置非法: {detail}")
