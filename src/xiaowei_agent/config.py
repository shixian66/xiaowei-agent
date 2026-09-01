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

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError

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

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment_id: StrictStr
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @property
    def tenant_id(self) -> str:
        """固定开发租户；常量，不可由环境覆盖。"""
        return DEFAULT_TENANT_ID


_FIELD_TO_ENV: Final[Mapping[str, str]] = {
    "environment_id": "XIAOWEI_ENVIRONMENT_ID",
    "log_level": "XIAOWEI_LOG_LEVEL",
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
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()
        )
    # 在 except 块之外抛出：`from None` 只会设置 __suppress_context__，
    # 不会清空 __context__，原始 ValidationError（含非法取值）仍会挂在异常对象上。
    raise ConfigError(f"配置非法: {detail}")
