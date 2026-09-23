"""身份激活入口测试替身；只记录调用，不复制存储状态机。"""

from types import SimpleNamespace
from typing import cast

from xiaowei_agent.application.activation_notification import (
    ActivationNotificationService,
)
from xiaowei_agent.application.identity_activation import (
    ActivationResumeUnavailableError,
    IdentityActivationService,
)
from xiaowei_agent.contracts.activation import ActivationRequest, ActivationStatus
from xiaowei_agent.contracts.web_navigation import WebReturnIntent


class RecordingActivationRequests:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.web_subjects: list[str] = []
        self.web_intents: list[WebReturnIntent] = []
        self.groups: list[tuple[str, str, str]] = []
        self.requests: dict[str, object] = {}

    async def request_web(
        self, *, subject_ref: str, return_intent: WebReturnIntent
    ) -> ActivationRequest:
        self.web_subjects.append(subject_ref)
        self.web_intents.append(return_intent)
        if self.failure is not None:
            raise self.failure
        request = SimpleNamespace(
            request_id=f"activation-{len(self.web_subjects)}",
            subject_ref=subject_ref,
            status=ActivationStatus.PENDING,
            return_intent=return_intent,
        )
        self.requests[request.request_id] = request
        return cast(ActivationRequest, request)

    async def resume_web(
        self, *, request_id: str, subject_ref: str
    ) -> ActivationRequest:
        del subject_ref
        request = self.requests.get(request_id)
        if request is None:
            raise ActivationResumeUnavailableError
        return cast(ActivationRequest, request)

    async def request_group(
        self,
        *,
        subject_ref: str,
        event_ref: str,
        chat_ref: str,
    ) -> ActivationRequest:
        self.groups.append((subject_ref, event_ref, chat_ref))
        if self.failure is not None:
            raise self.failure
        return cast(
            ActivationRequest,
            SimpleNamespace(
                request_id=f"group-activation-{len(self.groups)}",
                subject_ref=subject_ref,
                status=ActivationStatus.PENDING,
                return_intent=None,
            ),
        )

    def as_service(self) -> IdentityActivationService:
        return cast(IdentityActivationService, self)


class RecordingActivationNotifications:
    def __init__(self, *, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[str, str, bool]] = []

    async def notify(
        self,
        *,
        conversation_ref: str,
        event_id: str,
        submitted: bool,
    ) -> bool:
        self.calls.append((conversation_ref, event_id, submitted))
        return self.result

    def as_service(self) -> ActivationNotificationService:
        return cast(ActivationNotificationService, self)


__all__ = ["RecordingActivationNotifications", "RecordingActivationRequests"]
