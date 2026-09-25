"""飞书卡片投影、可靠订阅领取与有限 provider 重试。"""

import asyncio
import contextlib
import datetime as dt
import logging
import math
import uuid
from collections.abc import Callable, Coroutine
from typing import Any, Final, Literal, Protocol
from urllib.parse import quote

from xiaowei_agent.contracts import (
    TERMINAL_STATUSES,
    DestinationKind,
    FeishuProjectionInput,
    ProjectionErrorCode,
    ProjectionState,
    TaskLookup,
    TaskRecord,
    TaskView,
    content_digest,
)
from xiaowei_agent.persistence.channel import (
    ChannelBindingNotFoundError,
    ChannelStore,
    ClaimedTaskLookup,
    ClaimProjectionCommand,
    CompleteProjectionCommand,
    DeadLetterProjectionCommand,
    GroupBindingLookup,
    ProjectionClaimNotFoundError,
    ProjectionDueQuery,
    ProjectionSubscription,
    ProjectionUpdateResult,
    RecordInitialProjectionCommand,
    RenewProjectionClaimCommand,
    ScheduleProviderRetryCommand,
    ScheduleTaskRecheckCommand,
)
from xiaowei_agent.persistence.store import Clock, TaskStore
from xiaowei_agent.redaction import scrub_text
from xiaowei_agent.rendering.feishu import RenderedFeishuCard, render_feishu_card

AsyncSleep = Callable[[float], Coroutine[Any, Any, None]]
_LOGGER = logging.getLogger(__name__)
_PROJECTION_READ_ATTEMPTS = 3
_CLAIM_LOST_KIND: Final[str] = "claim_lost"
_RETRYABLE_PROVIDER_ERRORS = frozenset(
    {
        ProjectionErrorCode.PROVIDER_RATE_LIMITED,
        ProjectionErrorCode.PROVIDER_TIMEOUT,
        ProjectionErrorCode.PROVIDER_UNAVAILABLE,
        ProjectionErrorCode.PROVIDER_INTERNAL,
    }
)


class ChannelMessageError(RuntimeError):
    """出站 seam 的脱敏失败；只携带闭集错误码和可选退避提示。"""

    def __init__(
        self,
        *,
        error_code: ProjectionErrorCode,
        retry_after_seconds: float | None = None,
    ) -> None:
        if not isinstance(error_code, ProjectionErrorCode):
            raise TypeError("error_code must be a ProjectionErrorCode")
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, int | float)
            or not math.isfinite(retry_after_seconds)
            or retry_after_seconds < 0
        ):
            raise ValueError("retry_after_seconds must be a finite non-negative number")
        super().__init__("channel message delivery failed")
        self.error_code = error_code
        self.retry_after_seconds = (
            None if retry_after_seconds is None else float(retry_after_seconds)
        )


class ChannelMessagePort(Protocol):
    """飞书出站窄端口；供应商失败必须归一为 ``ChannelMessageError``。"""

    async def send_to_chat(
        self,
        *,
        conversation_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        """向明确群会话发送新卡片并返回供应商消息引用。"""

    async def send_to_user(
        self,
        *,
        subject_ref: str,
        card: RenderedFeishuCard,
        idempotency_ref: str,
    ) -> str:
        """向明确企业主体发送新卡片并返回供应商消息引用。"""

    async def update_card(
        self, *, message_ref: str, card: RenderedFeishuCard
    ) -> None:
        """按已持久化消息引用更新原卡片。"""


class TaskProjectionPort(Protocol):
    """渠道只可消费既有任务记录的安全投影。"""

    async def project_task(self, *, record: TaskRecord) -> TaskView:
        """投影已读取记录；不得获得任务提交或执行入口。"""


class ChannelProjectionSettings(Protocol):
    """projection worker 实际消费的最小配置面。"""

    @property
    def tenant_id(self) -> str: ...

    environment_id: str
    web_public_origin: str | None
    projection_claim_ttl_seconds: int
    projection_batch_limit: int
    projection_provider_max_attempts: int
    projection_provider_backoff_base_seconds: float
    projection_retry_after_cap_seconds: float
    projection_tenant_concurrency: int
    projection_task_poll_base_seconds: float
    projection_task_poll_cap_seconds: float
    channel_worker_poll_interval_seconds: float


class ChannelProjectionInvariantError(RuntimeError):
    """存储 winner 或目的地语义互相矛盾，进程必须 fail-stop。"""


class ChannelProjectionService:
    """只读 TaskStore 投影并推进 ChannelStore；绝不改变任务状态。"""

    def __init__(
        self,
        *,
        runtime: TaskProjectionPort,
        task_store: TaskStore,
        channel_store: ChannelStore,
        message_port: ChannelMessagePort,
        clock: Clock,
        settings: ChannelProjectionSettings,
        sleep: AsyncSleep,
        owner: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._tasks = task_store
        self._channels = channel_store
        self._messages = message_port
        self._clock = clock
        self._settings = settings
        if settings.web_public_origin is None:
            raise ValueError("channel projection requires a trusted detail origin")
        self._web_public_origin = settings.web_public_origin
        self._sleep = sleep
        self.owner = owner or f"channel-worker-{uuid.uuid4().hex}"
        self._outbound = asyncio.Semaphore(settings.projection_tenant_concurrency)

    def _claim_lookup(self, subscription: ProjectionSubscription) -> ClaimedTaskLookup:
        if subscription.claim_owner is None or subscription.fencing_token is None:
            raise ChannelProjectionInvariantError("projection winner has no live claim")
        token = subscription.fencing_token
        return ClaimedTaskLookup(
            subscription_id=subscription.subscription_id,
            claim_owner=subscription.claim_owner,
            fencing_token=token,
        )

    def _detail_url(self, task_id: str) -> str:
        return (
            f"{self._web_public_origin}/app/tasks/"
            f"{quote(task_id, safe='')}"
        )

    async def _stable_card(
        self,
        *,
        lookup: TaskLookup,
        record: TaskRecord,
    ) -> tuple[RenderedFeishuCard, int, bool] | None:
        submission = await self._tasks.get_submission(lookup=lookup)
        request_preview = scrub_text(submission.envelope.text)[:8192]
        for _ in range(_PROJECTION_READ_ATTEMPTS):
            view = await self._runtime.project_task(record=record)
            after = await self._tasks.get(lookup=lookup)
            if after.version == record.version and after.status is record.status:
                card = render_feishu_card(
                    FeishuProjectionInput.model_validate(
                        {
                            "task_view": view,
                            "request_preview": request_preview,
                            "task_version": record.version,
                            "detail_url": self._detail_url(record.task_id),
                        }
                    )
                )
                return card, record.version, record.status in TERMINAL_STATUSES
            record = after
        return None

    def _idempotency_ref(
        self,
        *,
        subscription: ProjectionSubscription,
        task_version: int,
        card: RenderedFeishuCard,
    ) -> str:
        return content_digest(
            ":".join(
                (
                    "feishu-card-v1",
                    subscription.subscription_id,
                    str(task_version),
                    card.payload_digest,
                )
            )
        )

    async def _new_message_target(
        self,
        *,
        subscription: ProjectionSubscription,
        lookup: TaskLookup,
    ) -> tuple[Literal["chat", "user"], str]:
        """在 provider 异常域之外解析新消息目的地。"""
        if subscription.destination_kind is DestinationKind.FEISHU_PRIVATE_NOTICE:
            return "user", subscription.destination_ref
        if subscription.destination_kind is not DestinationKind.FEISHU_MESSAGE_CARD:
            raise ChannelProjectionInvariantError("unsupported projection destination")

        try:
            binding = await self._channels.get_group_binding(
                lookup=GroupBindingLookup(
                    task_id=lookup.task_id,
                    tenant_id=lookup.tenant_id,
                    environment_id=lookup.environment_id,
                )
            )
        except ChannelBindingNotFoundError:
            # 私聊卡片的目的地是提交时记录的 p2p 会话 id（见 ChannelSubmitCommand）。
            return "chat", subscription.destination_ref
        if binding.conversation_ref != subscription.destination_ref:
            raise ChannelProjectionInvariantError(
                "projection destination differs from its group binding"
            )
        return "chat", subscription.destination_ref

    async def _send_new_card(
        self,
        *,
        subscription: ProjectionSubscription,
        target: tuple[Literal["chat", "user"], str],
        task_version: int,
        card: RenderedFeishuCard,
    ) -> str:
        idempotency_ref = self._idempotency_ref(
            subscription=subscription,
            task_version=task_version,
            card=card,
        )
        target_kind, target_ref = target
        if target_kind == "chat":
            return await self._messages.send_to_chat(
                conversation_ref=target_ref,
                card=card,
                idempotency_ref=idempotency_ref,
            )
        return await self._messages.send_to_user(
            subject_ref=target_ref,
            card=card,
            idempotency_ref=idempotency_ref,
        )

    async def _deliver(
        self,
        *,
        subscription: ProjectionSubscription,
        new_message_target: tuple[Literal["chat", "user"], str] | None,
        task_version: int,
        card: RenderedFeishuCard,
    ) -> str:
        if subscription.source_message_ref is not None:
            await self._messages.update_card(
                message_ref=subscription.source_message_ref,
                card=card,
            )
            return subscription.source_message_ref
        if new_message_target is None:
            raise ChannelProjectionInvariantError("new projection has no destination")
        message_ref = await self._send_new_card(
            subscription=subscription,
            target=new_message_target,
            task_version=task_version,
            card=card,
        )
        if (
            not isinstance(message_ref, str)
            or not message_ref
            or message_ref != message_ref.strip()
        ):
            raise ChannelMessageError(
                error_code=ProjectionErrorCode.PROVIDER_INTERNAL
            )
        return message_ref

    def _retry_delay(self, error: ChannelMessageError, failure_count: int) -> float:
        exponential = self._bounded_exponential(
            base=self._settings.projection_provider_backoff_base_seconds,
            cap=self._settings.projection_retry_after_cap_seconds,
            exponent=failure_count,
        )
        requested = error.retry_after_seconds or 0.0
        return float(
            min(
                self._settings.projection_retry_after_cap_seconds,
                max(exponential, requested),
            )
        )

    @staticmethod
    def _bounded_exponential(*, base: float, cap: float, exponent: int) -> float:
        """在乘方前命中 cap，避免持久化大计数触发巨型整数或溢出。"""
        value = base
        remaining = exponent
        while remaining > 0 and value < cap:
            if value >= cap / 2:
                return cap
            value *= 2
            remaining -= 1
        return min(cap, value)

    def _task_poll_delay(self, subscription: ProjectionSubscription) -> float:
        task_poll_attempt = max(
            1,
            subscription.attempt_number - subscription.provider_failure_count,
        )
        exponent = task_poll_attempt - 1
        return self._bounded_exponential(
            base=self._settings.projection_task_poll_base_seconds,
            cap=self._settings.projection_task_poll_cap_seconds,
            exponent=exponent,
        )

    def _log_provider_failure(
        self, subscription: ProjectionSubscription, error: ChannelMessageError
    ) -> None:
        try:
            _LOGGER.warning(
                "channel projection delivery failed",
                extra={
                    "failure_kind": error.error_code.value,
                    "tenant_id": self._settings.tenant_id,
                    "environment_id": self._settings.environment_id,
                    "task_ref": content_digest(subscription.task_id)[:16],
                    "subscription_ref": content_digest(
                        subscription.subscription_id
                    )[:16],
                },
            )
        except Exception:
            return

    def _log_claim_lost(self, subscription: ProjectionSubscription) -> None:
        """记录不含原始标识的 fencing loser；日志故障不改变处理结果。"""
        try:
            _LOGGER.warning(
                "channel projection claim lost",
                extra={
                    "failure_kind": _CLAIM_LOST_KIND,
                    "tenant_id": self._settings.tenant_id,
                    "environment_id": self._settings.environment_id,
                    "task_ref": content_digest(subscription.task_id)[:16],
                    "subscription_ref": content_digest(
                        subscription.subscription_id
                    )[:16],
                },
            )
        except Exception:
            return

    def _claim_update_applied(
        self,
        *,
        subscription: ProjectionSubscription,
        result: ProjectionUpdateResult,
    ) -> bool:
        if result.applied:
            return True
        self._log_claim_lost(subscription)
        return False

    async def _record_provider_failure(
        self,
        *,
        subscription: ProjectionSubscription,
        error: ChannelMessageError,
    ) -> bool:
        self._log_provider_failure(subscription, error)
        lookup = self._claim_lookup(subscription)
        attempts_exhausted = (
            subscription.provider_failure_count + 1
            >= self._settings.projection_provider_max_attempts
        )
        if error.error_code not in _RETRYABLE_PROVIDER_ERRORS or attempts_exhausted:
            result = await self._channels.dead_letter_projection(
                command=DeadLetterProjectionCommand(
                    subscription_id=lookup.subscription_id,
                    claim_owner=lookup.claim_owner,
                    fencing_token=lookup.fencing_token,
                    expected_state=subscription.state,
                    error_code=error.error_code,
                )
            )
            return self._claim_update_applied(
                subscription=subscription,
                result=result,
            )
        delay = self._retry_delay(error, subscription.provider_failure_count)
        result = await self._channels.schedule_provider_retry(
            command=ScheduleProviderRetryCommand(
                subscription_id=lookup.subscription_id,
                claim_owner=lookup.claim_owner,
                fencing_token=lookup.fencing_token,
                expected_state=subscription.state,
                next_attempt_at=self._clock() + dt.timedelta(seconds=delay),
                error_code=error.error_code,
            )
        )
        return self._claim_update_applied(
            subscription=subscription,
            result=result,
        )

    async def _process_due(self, due: ProjectionSubscription) -> int:
        claimed = await self._channels.claim_projection_subscription(
            command=ClaimProjectionCommand(
                subscription_id=due.subscription_id,
                claim_owner=self.owner,
                ttl_seconds=self._settings.projection_claim_ttl_seconds,
                expected_state=due.state,
            )
        )
        if not claimed.applied:
            return 0
        subscription = claimed.winner
        try:
            lookup = await self._channels.resolve_claimed_task_lookup(
                lookup=self._claim_lookup(subscription)
            )
        except ProjectionClaimNotFoundError:
            self._log_claim_lost(subscription)
            return 0
        record = await self._tasks.get(lookup=lookup)
        if (
            subscription.state is ProjectionState.DELIVERING_TERMINAL
            and record.status not in TERMINAL_STATUSES
        ):
            claim = self._claim_lookup(subscription)
            result = await self._channels.schedule_task_recheck(
                command=ScheduleTaskRecheckCommand(
                    subscription_id=claim.subscription_id,
                    claim_owner=claim.claim_owner,
                    fencing_token=claim.fencing_token,
                    expected_state=ProjectionState.DELIVERING_TERMINAL,
                    next_attempt_at=self._clock()
                    + dt.timedelta(
                        seconds=self._task_poll_delay(subscription)
                    ),
                )
            )
            return int(
                self._claim_update_applied(
                    subscription=subscription,
                    result=result,
                )
            )
        if subscription.state not in {
            ProjectionState.PENDING_INITIAL,
            ProjectionState.DELIVERING_TERMINAL,
        }:
            raise ChannelProjectionInvariantError(
                "claimed projection has an unsupported state"
            )

        projected = await self._stable_card(lookup=lookup, record=record)
        if projected is None:
            # 任务连续变化时不发送混合版本；让短租约过期后再重新领取。
            return 1
        card, task_version, task_terminal = projected
        message_ref: str | None = None
        message_error: ChannelMessageError | None = None
        async with self._outbound:
            new_message_target = None
            if subscription.source_message_ref is None:
                new_message_target = await self._new_message_target(
                    subscription=subscription,
                    lookup=lookup,
                )
            claim = self._claim_lookup(subscription)
            renewed = await self._channels.renew_projection_claim(
                command=RenewProjectionClaimCommand(
                    subscription_id=claim.subscription_id,
                    claim_owner=claim.claim_owner,
                    fencing_token=claim.fencing_token,
                    expected_state=subscription.state,
                    ttl_seconds=self._settings.projection_claim_ttl_seconds,
                )
            )
            if not self._claim_update_applied(
                subscription=subscription,
                result=renewed,
            ):
                return 0
            subscription = renewed.winner
            if (
                subscription.claim_expires_at is None
                or subscription.claim_expires_at <= self._clock()
            ):
                self._log_claim_lost(subscription)
                return 0
            try:
                message_ref = await self._deliver(
                    subscription=subscription,
                    new_message_target=new_message_target,
                    task_version=task_version,
                    card=card,
                )
            except ChannelMessageError as caught:
                message_error = caught
            except ChannelProjectionInvariantError:
                raise
            except Exception:
                message_error = ChannelMessageError(
                    error_code=ProjectionErrorCode.PROVIDER_INTERNAL
                )

        if message_error is not None:
            return int(
                await self._record_provider_failure(
                    subscription=subscription,
                    error=message_error,
                )
            )
        if message_ref is None:
            raise ChannelProjectionInvariantError("provider returned no message result")

        claim = self._claim_lookup(subscription)
        if subscription.state is ProjectionState.PENDING_INITIAL:
            result = await self._channels.record_initial_projection(
                command=RecordInitialProjectionCommand(
                    subscription_id=claim.subscription_id,
                    claim_owner=claim.claim_owner,
                    fencing_token=claim.fencing_token,
                    expected_state=ProjectionState.PENDING_INITIAL,
                    source_message_ref=message_ref,
                    task_version=task_version,
                    payload_digest=card.payload_digest,
                    task_terminal=task_terminal,
                    next_attempt_at=self._clock()
                    + dt.timedelta(
                        seconds=self._settings.projection_task_poll_base_seconds
                    ),
                )
            )
            return int(
                self._claim_update_applied(
                    subscription=subscription,
                    result=result,
                )
            )
        result = await self._channels.complete_projection(
            command=CompleteProjectionCommand(
                subscription_id=claim.subscription_id,
                claim_owner=claim.claim_owner,
                fencing_token=claim.fencing_token,
                expected_state=ProjectionState.DELIVERING_TERMINAL,
                source_message_ref=message_ref,
                task_version=task_version,
                payload_digest=card.payload_digest,
            )
        )
        return int(
            self._claim_update_applied(
                subscription=subscription,
                result=result,
            )
        )

    async def poll_once(self) -> int:
        """按受信 scope 处理一批；同一订阅仍由 claim/fencing 串行。"""
        due = await self._channels.list_due_projection_subscriptions(
            query=ProjectionDueQuery(
                tenant_id=self._settings.tenant_id,
                environment_id=self._settings.environment_id,
                limit=self._settings.projection_batch_limit,
            )
        )
        if not due:
            return 0
        tasks = tuple(asyncio.create_task(self._process_due(item)) for item in due)
        try:
            results = await asyncio.gather(*tasks)
            return sum(results)
        finally:
            unfinished = tuple(task for task in tasks if not task.done())
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

    async def _wait_or_stop(self, seconds: float, stop: asyncio.Event) -> bool:
        sleeper = asyncio.create_task(self._sleep(seconds))
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                {sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            if stopper in done:
                sleeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sleeper
                return True
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
            await sleeper
            return False
        finally:
            unfinished = tuple(
                task for task in (sleeper, stopper) if not task.done()
            )
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

    async def run(self, stop: asyncio.Event) -> None:
        """轮询到协作式停止；持久化/不变量异常交给进程 fail-stop。"""
        while not stop.is_set():
            await self.poll_once()
            if await self._wait_or_stop(
                self._settings.channel_worker_poll_interval_seconds, stop
            ):
                return


__all__ = [
    "ChannelMessageError",
    "ChannelMessagePort",
    "ChannelProjectionInvariantError",
    "ChannelProjectionService",
    "ChannelProjectionSettings",
    "TaskProjectionPort",
]
