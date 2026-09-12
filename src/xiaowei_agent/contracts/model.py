"""RI3 的两个窄模型端口所使用的严格契约。"""

from collections.abc import Mapping
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from xiaowei_agent.contracts.base import (
    Contract,
    FiniteFloat,
    FreeText,
    FrozenMap,
    NonEmptyText,
    StrictInt,
    StrictStr,
)
from xiaowei_agent.contracts.intent import (
    INTENT_MISSING_ALLOWLISTS,
    INTENT_SLOT_ALLOWLISTS,
    IntentDraft,
)

MAX_MODEL_TEXT_CHARACTERS: Final[int] = 8_192
_MAX_UTF8_BYTES_PER_CODE_POINT: Final[int] = 4
MAX_MODEL_TEXT_BYTES: Final[int] = (
    MAX_MODEL_TEXT_CHARACTERS * _MAX_UTF8_BYTES_PER_CODE_POINT
)
MAX_MODEL_HISTORY_ITEMS: Final[int] = 20
MAX_MODEL_HISTORY_CHARACTERS: Final[int] = 64_000
MAX_MODEL_HISTORY_BYTES: Final[int] = (
    MAX_MODEL_HISTORY_CHARACTERS * _MAX_UTF8_BYTES_PER_CODE_POINT
)
MAX_MODEL_REQUEST_BYTES: Final[int] = 512 * 1_024
MAX_ADVISORY_ROWS: Final[int] = 20
MAX_INTENT_ITEMS: Final[int] = 20
MAX_ADVISORY_ITEMS: Final[int] = 20
MAX_ADVISORY_TEXT_CHARACTERS: Final[int] = 16_384
MAX_MODEL_USAGE_TOKENS: Final[int] = 2**63 - 1


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("model text is not valid UTF-8") from None


def _valid_utf8(value: str) -> str:
    _utf8_size(value)
    return value


def _require_request_size(value: Contract) -> None:
    if _utf8_size(value.model_dump_json()) > MAX_MODEL_REQUEST_BYTES:
        raise ValueError("model request exceeds the serialized byte limit")


ModelText = Annotated[
    FreeText,
    Field(max_length=MAX_MODEL_TEXT_CHARACTERS),
    AfterValidator(_valid_utf8),
]
AdvisoryText = Annotated[
    NonEmptyText,
    Field(max_length=MAX_ADVISORY_TEXT_CHARACTERS),
    AfterValidator(_valid_utf8),
]


class ModelIntentRequest(Contract):
    """仅含已净化文本的意图理解请求。"""

    user_text: ModelText
    history: tuple[ModelText, ...] = Field(max_length=MAX_MODEL_HISTORY_ITEMS)
    context_truncated: bool

    @model_validator(mode="after")
    def _history_and_request_are_bounded(self) -> "ModelIntentRequest":
        if sum(map(len, self.history)) > MAX_MODEL_HISTORY_CHARACTERS:
            raise ValueError("model history exceeds the character limit")
        _require_request_size(self)
        return self


class SlowQueryAdvisoryRequest(Contract):
    """仅含慢查询白名单行与抽样标志。"""

    rows: tuple[FrozenMap, ...] = Field(min_length=1, max_length=MAX_ADVISORY_ROWS)
    sampled: bool

    @model_validator(mode="after")
    def _rows_are_bounded(self) -> "SlowQueryAdvisoryRequest":
        for row in self.rows:
            if len(row) > 64:
                raise ValueError("model row has too many fields")
            for key, value in row.items():
                if len(key) > MAX_MODEL_TEXT_CHARACTERS:
                    raise ValueError("model row key exceeds its limit")
                _utf8_size(key)
                if isinstance(value, str):
                    if len(value) > MAX_MODEL_TEXT_CHARACTERS:
                        raise ValueError("model row text exceeds its limit")
                    _utf8_size(value)
        _require_request_size(self)
        return self


_ProviderSlotText = Annotated[StrictStr, Field(max_length=1_024)]
_ProviderSlot = _ProviderSlotText | SkipJsonSchema[None]


class ProviderIntentSlots(Contract):
    """Developer API schema 可表达的固定 provider wire 槽位。"""

    environment_id: _ProviderSlot = None
    database: _ProviderSlot = None
    user_name: _ProviderSlot = None
    query_id: _ProviderSlot = None
    window_minutes: _ProviderSlot = None
    alert_name: _ProviderSlot = None
    instance: _ProviderSlot = None
    fingerprint: _ProviderSlot = None
    asset_id: _ProviderSlot = None
    hostname: _ProviderSlot = None
    ip: _ProviderSlot = None


class ProviderIntentResponse(Contract):
    """供应商可返回的意图字段闭集；adapter 本地补 ``source``。"""

    intent: StrictStr = Field(max_length=256)
    slots: ProviderIntentSlots
    missing: tuple[StrictStr, ...] = Field(max_length=MAX_INTENT_ITEMS)
    confidence: FiniteFloat = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _intent_collections_are_bounded(self) -> "ProviderIntentResponse":
        slots = self.slots.model_dump(exclude_none=True)
        if self.slots.model_fields_set - slots.keys():
            raise ValueError("explicit null intent slot is not allowed")
        allowed_slots = INTENT_SLOT_ALLOWLISTS.get(self.intent)
        if allowed_slots is None or not slots.keys() <= allowed_slots:
            raise ValueError("intent slot key is not allowed")
        if not set(self.missing) <= INTENT_MISSING_ALLOWLISTS[self.intent]:
            raise ValueError("missing intent field is not allowed")
        if any(len(item) > 128 for item in self.missing):
            raise ValueError("missing item exceeds its text limit")
        return self


class ModelAdvisory(Contract):
    """只读分析展示字段；不含事实、引用、状态或可执行动作。"""

    analysis: AdvisoryText
    suggestions: tuple[AdvisoryText, ...] = Field(max_length=MAX_ADVISORY_ITEMS)
    uncertainties: tuple[AdvisoryText, ...] = Field(max_length=MAX_ADVISORY_ITEMS)


class ModelUsage(Contract):
    """SDK 元数据收窄后的可信、可持久化 token 计数。"""

    input_tokens: StrictInt | None = Field(
        default=None, ge=0, le=MAX_MODEL_USAGE_TOKENS
    )
    output_tokens: StrictInt | None = Field(
        default=None, ge=0, le=MAX_MODEL_USAGE_TOKENS
    )


class IntentModelResult(Contract):
    """一次 intent 调用接受的 DTO 与同次可信 usage。"""

    draft: IntentDraft
    usage: ModelUsage


class AdvisoryModelResult(Contract):
    """一次 advisory 调用接受的 DTO 与同次可信 usage。"""

    advisory: ModelAdvisory
    usage: ModelUsage


class ModelInvocationProfile(Contract):
    """composition root 注入的固定、非秘密 RI3 身份与预算。"""

    provider: Literal["google-gemini-developer-api"] = (
        "google-gemini-developer-api"
    )
    model: Literal["gemini-3-flash-preview"] = "gemini-3-flash-preview"
    api_version: Literal["v1beta"] = "v1beta"
    origin: Literal["https://generativelanguage.googleapis.com"] = (
        "https://generativelanguage.googleapis.com"
    )
    intent_prompt_revision: Literal["ri3-intent-prompt-v1"] = (
        "ri3-intent-prompt-v1"
    )
    intent_schema_revision: Literal["ri3-intent-schema-v1"] = (
        "ri3-intent-schema-v1"
    )
    advisory_prompt_revision: Literal["ri3-advisory-prompt-v1"] = (
        "ri3-advisory-prompt-v1"
    )
    advisory_schema_revision: Literal["ri3-advisory-schema-v1"] = (
        "ri3-advisory-schema-v1"
    )
    intent_thinking_level: Literal["LOW"] = "LOW"
    advisory_thinking_level: Literal["HIGH"] = "HIGH"
    intent_timeout_seconds: StrictInt = Field(default=60, ge=60, le=60)
    advisory_timeout_seconds: StrictInt = Field(default=180, ge=180, le=180)
    intent_output_tokens: StrictInt = Field(default=2_048, ge=2_048, le=2_048)
    advisory_output_tokens: StrictInt = Field(default=4_000, ge=4_000, le=4_000)


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
    "MAX_MODEL_HISTORY_BYTES",
    "MAX_MODEL_HISTORY_CHARACTERS",
    "MAX_MODEL_HISTORY_ITEMS",
    "MAX_MODEL_REQUEST_BYTES",
    "MAX_MODEL_TEXT_BYTES",
    "MAX_MODEL_TEXT_CHARACTERS",
    "MAX_MODEL_USAGE_TOKENS",
    "AdvisoryModelResult",
    "IntentModelResult",
    "ModelAdvisory",
    "ModelIntentRequest",
    "ModelInvocationProfile",
    "ModelUsage",
    "ProviderIntentResponse",
    "ProviderIntentSlots",
    "SlowQueryAdvisoryRequest",
    "model_text_values",
]
