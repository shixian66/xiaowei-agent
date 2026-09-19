"""Prometheus 告警证据查询的闭集参数。"""

import datetime as dt
import ipaddress
import re
from collections.abc import Mapping
from typing import Annotated, Any, ClassVar, Final, Self

from pydantic import AfterValidator, Field, model_validator

from xiaowei_agent.contracts import (
    MAX_PROMQL_POINTS_PER_SERIES,
    MAX_PROMQL_SERIES,
    MAX_PROMQL_WINDOW_MINUTES,
    AwareDatetime,
    CapabilityParams,
    JsonScalar,
    StrictInt,
    StrictStr,
)

ALERT_NAMES: Final[frozenset[str]] = frozenset({"HostHighCpu", "InstanceDown"})
DEFAULT_WINDOW_MINUTES: Final[int] = 30
STEP_SECONDS: Final[int] = 60
MAX_SERIES: Final[int] = MAX_PROMQL_SERIES
MAX_POINTS_PER_SERIES: Final[int] = MAX_PROMQL_POINTS_PER_SERIES
PROMQL_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {"promql", "promql_template_id"}
)

_HOST_LABEL_RE: Final[re.Pattern[str]] = re.compile(
    r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_FINGERPRINT_RE: Final[re.Pattern[str]] = re.compile(
    r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z"
)


def _port(value: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ValueError("instance port must be decimal")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("instance port is outside the allowed range")
    return port


def _hostname(value: str) -> str:
    hostname = value[:-1] if value.endswith(".") else value
    if not hostname or len(hostname) > 253:
        raise ValueError("instance hostname is invalid")
    if re.fullmatch(r"[0-9.]+", hostname):
        try:
            return str(ipaddress.IPv4Address(hostname))
        except ValueError:
            raise ValueError("instance IPv4 address is invalid") from None
    if any(_HOST_LABEL_RE.fullmatch(label) is None for label in hostname.split(".")):
        raise ValueError("instance hostname is invalid")
    return hostname.lower()


def _instance(value: str) -> str:
    """规范化 hostname/IPv4/[IPv6]，并拒绝歧义或选择器语法。"""
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            raise ValueError("bracketed IPv6 instance is invalid")
        try:
            address = str(ipaddress.IPv6Address(value[1:closing]))
        except ValueError:
            raise ValueError("bracketed IPv6 instance is invalid") from None
        remainder = value[closing + 1 :]
        if not remainder:
            return f"[{address}]"
        if not remainder.startswith(":") or remainder.count(":") != 1:
            raise ValueError("bracketed IPv6 port is invalid")
        return f"[{address}]:{_port(remainder[1:])}"
    if value.count(":") > 1:
        raise ValueError("IPv6 instances must be bracketed")
    host, separator, raw_port = value.partition(":")
    canonical = _hostname(host)
    return f"{canonical}:{_port(raw_port)}" if separator else canonical


Instance = Annotated[StrictStr, AfterValidator(_instance)]


def _alert_name(value: str) -> str:
    if value not in ALERT_NAMES:
        raise ValueError("alert_name is not registered")
    return value


AlertName = Annotated[StrictStr, AfterValidator(_alert_name)]


def _fingerprint(value: str) -> str:
    if _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError("fingerprint is invalid")
    return value


Fingerprint = Annotated[StrictStr, AfterValidator(_fingerprint)]


class PrometheusAlertParams(CapabilityParams):
    """一次告警指标证据查询的全部参数与硬预算。"""

    INPUT_SCHEMA_REF: ClassVar[str] = "input.prometheus.alert.v1"

    alert_name: AlertName
    instance: Instance
    window_start: AwareDatetime
    window_end: AwareDatetime
    fingerprint: Fingerprint | None = None
    step_seconds: StrictInt = Field(default=STEP_SECONDS)
    max_series: StrictInt = Field(default=MAX_SERIES)
    max_points_per_series: StrictInt = Field(default=MAX_POINTS_PER_SERIES)

    @model_validator(mode="after")
    def _window_and_budgets_are_fixed(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be strictly after window_start")
        window = self.window_end - self.window_start
        if window > dt.timedelta(minutes=MAX_PROMQL_WINDOW_MINUTES):
            raise ValueError("window exceeds the maximum span")
        if self.step_seconds != STEP_SECONDS:
            raise ValueError("step_seconds is fixed")
        if self.max_series != MAX_SERIES:
            raise ValueError("max_series is fixed")
        if self.max_points_per_series != MAX_POINTS_PER_SERIES:
            raise ValueError("max_points_per_series is fixed")
        points = int(window.total_seconds()) // self.step_seconds + 1
        if points > self.max_points_per_series:
            raise ValueError("window exceeds the point budget")
        return self

    def to_typed_arguments(self) -> dict[str, JsonScalar]:
        return {
            "alert_name": self.alert_name,
            "instance": self.instance,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "fingerprint": self.fingerprint,
            "step_seconds": self.step_seconds,
            "max_series": self.max_series,
            "max_points_per_series": self.max_points_per_series,
        }

    @classmethod
    def from_typed_arguments(cls, arguments: Mapping[str, JsonScalar]) -> Self:
        payload: dict[str, Any] = {
            key: value
            for key, value in arguments.items()
            if key not in PROMQL_ENVELOPE_KEYS
        }
        for field in ("window_start", "window_end"):
            raw = payload.get(field)
            if isinstance(raw, str):
                try:
                    payload[field] = dt.datetime.fromisoformat(raw)
                except ValueError:
                    pass
        return cls.model_validate(payload)


def normalise_window(
    *, as_of: dt.datetime, window_minutes: int
) -> tuple[dt.datetime, dt.datetime]:
    """把查询窗口规范为 UTC 整分钟的 ``[start, end)``。"""
    if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("as_of must be timezone-aware")
    if not 0 < window_minutes <= MAX_PROMQL_WINDOW_MINUTES:
        raise ValueError("window span is outside the allowed range")
    end = as_of.astimezone(dt.UTC).replace(second=0, microsecond=0)
    return end - dt.timedelta(minutes=window_minutes), end
