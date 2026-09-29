"""HTTP 请求的严格形状与闭集错误体。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xiaowei_agent.contracts import NonEmptyText, StrictStr


class SubmitTaskRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    text: NonEmptyText = Field(max_length=8192)
    idempotency_key: StrictStr = Field(max_length=200)


class ErrorItem(BaseModel):
    """共享 HTTP 错误码；401/403 由后续独立 Web app 产出。

    当前受信的 internal-api 不实现终端用户鉴权，也不因词汇表预留而增加不可达分支。
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    code: Literal[
        "invalid_request",
        "unauthorized",
        "forbidden",
        "not_found",
        "method_not_allowed",
        "clarification.integrity_error",
        "idempotency_conflict",
        # F1：目标选择作答时 SQL 已过期或不可读；后者不区分内部原因。
        "sql_artifact.expired",
        "sql_artifact.unavailable",
        # F1：纯 SQL 不能作为普通对话或澄清回答提交（API/CLI 本阶段不支持 SQL 提交）。
        "sql_message.not_accepted",
        # F1：像 SQL 却无法确认是完整语句，写任何事实前拒绝（设计 §5.1）。
        "sql_message.incomplete",
        # W4b：服务端生成的资源 ID 撞上已有 ID；不是幂等重放，单独一个闭集码。
        "conflict",
        "payload_too_large",
        "unsupported_media_type",
        "internal_error",
        "unavailable",
        # RI5：初始口令未更换时，除登录/改密/退出外一律用这一条拒绝。
        # 它必须是闭集成员而不是自由文本——错误码是浏览器唯一能据以分支的事实。
        "password_change_required",
        "activation_pending",
    ]


class ErrorResponse(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    error: ErrorItem


def error_body(code: str) -> dict[str, dict[str, str]]:
    """构造不含 message、字段名或输入值的唯一错误体。"""
    item = ErrorItem.model_validate({"code": code})
    return ErrorResponse(error=item).model_dump()
