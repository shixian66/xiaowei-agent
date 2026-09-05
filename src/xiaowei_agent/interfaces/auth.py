"""M5 固定本地身份适配；不接受任何请求侧身份覆盖。"""

import datetime as dt
import uuid
from collections.abc import Callable
from typing import TypeAlias

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    Channel,
    RequestContext,
    RequestEnvelope,
    TaskSubmission,
)
from xiaowei_agent.interfaces.http_models import SubmitTaskRequest
from xiaowei_agent.trace import new_trace_id

Clock: TypeAlias = Callable[[], dt.datetime]


def trusted_submission(
    *,
    request: SubmitTaskRequest,
    settings: Settings,
    clock: Clock,
    policy_revision: str,
    trace_id: str,
) -> TaskSubmission:
    """只从受信配置和服务端生成值构造执行上下文。"""
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor=settings.actor,
        environment_id=settings.environment_id,
        trace_id=trace_id,
        policy_revision=policy_revision,
    )
    envelope = RequestEnvelope(
        request_id=uuid.uuid4().hex,
        tenant_id=context.tenant_id,
        actor=context.actor,
        channel=Channel.API,
        text=request.text,
        idempotency_key=request.idempotency_key,
        environment_id=context.environment_id,
    )
    return TaskSubmission(envelope=envelope, context=context, as_of=clock())


def trusted_trace_id() -> str:
    return new_trace_id()
