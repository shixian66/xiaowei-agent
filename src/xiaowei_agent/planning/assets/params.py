"""资产查询 selector 的闭集参数与规范化。"""

import ipaddress
import re
import unicodedata
from typing import Annotated, Self

from pydantic import AfterValidator, model_validator

from xiaowei_agent.contracts import Contract, StrictStr

_ASSET_ID_PUNCTUATION = frozenset("._:-")
_HOST_LABEL_RE = re.compile(
    r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


def _asset_id(value: str) -> str:
    canonical = unicodedata.normalize("NFC", value)
    if not 1 <= len(canonical) <= 128 or any(
        not (character.isalnum() or character in _ASSET_ID_PUNCTUATION)
        for character in canonical
    ):
        raise ValueError("asset_id is invalid")
    return canonical


def _hostname(value: str) -> str:
    canonical = value[:-1] if value.endswith(".") else value
    if (
        not canonical.isascii()
        or not 1 <= len(canonical) <= 253
        or any(
            _HOST_LABEL_RE.fullmatch(label) is None
            for label in canonical.split(".")
        )
    ):
        raise ValueError("hostname is invalid")
    return canonical.lower()


def _ip(value: str) -> str:
    if any(marker in value for marker in ("%", "/", "-", "[", "]")):
        raise ValueError("ip selector must be one address without scope or port")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        raise ValueError("ip selector is invalid") from None


AssetId = Annotated[StrictStr, AfterValidator(_asset_id)]
Hostname = Annotated[StrictStr, AfterValidator(_hostname)]
IpAddress = Annotated[StrictStr, AfterValidator(_ip)]


class AssetLookupParams(Contract):
    """恰含一个精确 selector 的资产查询参数。"""

    asset_id: AssetId | None = None
    hostname: Hostname | None = None
    ip: IpAddress | None = None

    @model_validator(mode="after")
    def _has_exactly_one_selector(self) -> Self:
        if sum(value is not None for value in (self.asset_id, self.hostname, self.ip)) != 1:
            raise ValueError("exactly one asset selector is required")
        return self

    def selector(self) -> tuple[str, str]:
        """返回 selector 的稳定字段名与规范值。"""
        for name in ("asset_id", "hostname", "ip"):
            value = getattr(self, name)
            if value is not None:
                return name, value
        raise AssertionError("validated params must contain one selector")
