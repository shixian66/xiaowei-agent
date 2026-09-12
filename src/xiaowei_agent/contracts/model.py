"""RI3 的两个窄模型端口所使用的严格契约。"""

from collections.abc import Mapping
from typing import Annotated, Final

from pydantic import AfterValidator, Field, model_validator

from xiaowei_agent.contracts.base import (
    Contract,
    FiniteFloat,
    FreeText,
    FrozenMap,
    FrozenStrMap,
    NonEmptyText,
    StrictStr,
)

MAX_MODEL_TEXT_CHARACTERS: Final[int] = 8_192
MAX_MODEL_TEXT_BYTES: Final[int] = 32 * 1_024
MAX_MODEL_HISTORY_ITEMS: Final[int] = 20
MAX_ADVISORY_ROWS: Final[int] = 20
MAX_INTENT_ITEMS: Final[int] = 20
MAX_ADVISORY_ITEMS: Final[int] = 20
MAX_ADVISORY_TEXT_CHARACTERS: Final[int] = 16_384


def _bounded_utf8(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_MODEL_TEXT_BYTES:
        raise ValueError("model text exceeds the UTF-8 byte limit")
    return value


def _bounded_advisory_text(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_ADVISORY_TEXT_CHARACTERS * 4:
        raise ValueError("model advisory exceeds the UTF-8 byte limit")
    return value


ModelText = Annotated[
    FreeText,
    Field(max_length=MAX_MODEL_TEXT_CHARACTERS),
    AfterValidator(_bounded_utf8),
]
AdvisoryText = Annotated[
    NonEmptyText,
    Field(max_length=MAX_ADVISORY_TEXT_CHARACTERS),
    AfterValidator(_bounded_advisory_text),
]


class ModelIntentRequest(Contract):
    """仅含已净化文本的意图理解请求。"""

    user_text: ModelText
    history: tuple[ModelText, ...] = Field(max_length=MAX_MODEL_HISTORY_ITEMS)
    context_truncated: bool


class SlowQueryAdvisoryRequest(ModelIntentRequest):
    """只比意图请求增加慢查询白名单行与抽样标志。"""

    rows: tuple[FrozenMap, ...] = Field(min_length=1, max_length=MAX_ADVISORY_ROWS)
    sampled: bool

    @model_validator(mode="after")
    def _rows_are_bounded(self) -> "SlowQueryAdvisoryRequest":
        for row in self.rows:
            if len(row) > 64:
                raise ValueError("model row has too many fields")
            for value in row.values():
                if isinstance(value, str) and (
                    len(value) > MAX_MODEL_TEXT_CHARACTERS
                    or len(value.encode("utf-8")) > MAX_MODEL_TEXT_BYTES
                ):
                    raise ValueError("model row text exceeds its limit")
        return self


class ProviderIntentResponse(Contract):
    """供应商可返回的意图字段闭集；adapter 本地补 ``source``。"""

    intent: StrictStr = Field(max_length=256)
    slots: FrozenStrMap
    missing: tuple[StrictStr, ...] = Field(max_length=MAX_INTENT_ITEMS)
    confidence: FiniteFloat = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _intent_collections_are_bounded(self) -> "ProviderIntentResponse":
        if len(self.slots) > MAX_INTENT_ITEMS:
            raise ValueError("too many intent slots")
        if any(len(key) > 128 or len(value) > 1_024 for key, value in self.slots.items()):
            raise ValueError("intent slot exceeds its text limit")
        if any(len(item) > 128 for item in self.missing):
            raise ValueError("missing item exceeds its text limit")
        return self


class ModelAdvisory(Contract):
    """只读分析展示字段；不含事实、引用、状态或可执行动作。"""

    analysis: AdvisoryText
    suggestions: tuple[AdvisoryText, ...] = Field(max_length=MAX_ADVISORY_ITEMS)
    uncertainties: tuple[AdvisoryText, ...] = Field(max_length=MAX_ADVISORY_ITEMS)


def model_text_values(value: Contract) -> tuple[str, ...]:
    """返回供应商响应中的全部文本，供 total redaction 复验。"""
    dumped = value.model_dump(mode="python")
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, Mapping):
            for key, nested in item.items():
                visit(key)
                visit(nested)
        elif isinstance(item, tuple | list):
            for nested in item:
                visit(nested)

    visit(dumped)
    return tuple(found)


__all__ = [
    "MAX_ADVISORY_ROWS",
    "MAX_MODEL_HISTORY_ITEMS",
    "MAX_MODEL_TEXT_BYTES",
    "MAX_MODEL_TEXT_CHARACTERS",
    "ModelAdvisory",
    "ModelIntentRequest",
    "ProviderIntentResponse",
    "SlowQueryAdvisoryRequest",
    "model_text_values",
]
