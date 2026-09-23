"""身份激活入口测试替身；只记录调用，不复制存储状态机。"""

from typing import cast

from xiaowei_agent.application.activation_notification import (
    ActivationNotificationService,
)
from xiaowei_agent.application.identity_activation import IdentityActivationService
from xiaowei_agent.contracts.activation import ActivationRequest
from xiaowei_agent.contracts.web_navigation import WebReturnIntent


class RecordingActivationRequests:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.web_subjects: list[str] = []
        self.web_intents: list[WebReturnIntent] = []
        self.groups: list[tuple[str, str, str]] = []

    async def request_web(
        self, *, subject_ref: str, return_intent: WebReturnIntent
    ) -> ActivationRequest:
        self.web_subjects.append(subject_ref)
        self.web_intents.append(return_intent)
        if self.failure is not None:
            raise self.failure
        return cast(ActivationRequest, object())

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
        return cast(ActivationRequest, object())

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
