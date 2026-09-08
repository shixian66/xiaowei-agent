"""HTTP 请求的严格形状与闭集错误体。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xiaowei_agent.contracts import NonEmptyText, StrictStr


class SubmitTaskRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    text: NonEmptyText = Field(max_length=8192)
    idempotency_key: StrictStr = Field(max_length=200)


class ErrorItem(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    code: Literal[
        "invalid_request",
        "unauthorized",
        "forbidden",
        "not_found",
        "method_not_allowed",
        "idempotency_conflict",
        "payload_too_large",
        "unsupported_media_type",
        "internal_error",
        "unavailable",
    ]


class ErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    error: ErrorItem


def error_body(code: str) -> dict[str, dict[str, str]]:
    """构造不含 message、字段名或输入值的唯一错误体。"""
    item = ErrorItem.model_validate({"code": code})
    return ErrorResponse(error=item).model_dump()
