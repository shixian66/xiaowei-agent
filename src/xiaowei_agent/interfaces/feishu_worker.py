"""默认关闭的飞书卡片 projection worker 进程入口。"""

import asyncio
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Final, Protocol

from xiaowei_agent.application.channel_projection import ChannelProjectionService
from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.log import configure_logging

_LOGGER = logging.getLogger(__name__)


class _FailureKind(StrEnum):
    CONFIGURATION_INVALID = "configuration_invalid"
    CHANNEL_WORKER_FAILURE = "channel_worker_failure"


_FAILURE_MESSAGES: Final[dict[_FailureKind, str]] = {
    _FailureKind.CONFIGURATION_INVALID: "channel projection worker configuration invalid",
    _FailureKind.CHANNEL_WORKER_FAILURE: "channel projection worker stopped",
}


class _WorkerProcessStack(Protocol):
    @property
    def service(self) -> ChannelProjectionService: ...

    @property
    def aclose(self) -> Callable[[], Awaitable[None]]: ...


def _record_process_failure(failure_kind: _FailureKind) -> None:
    """只记录进程级闭集原因，不保留配置或供应商异常正文。"""
    try:
        _LOGGER.error(
            _FAILURE_MESSAGES[failure_kind],
            extra={"failure_kind": failure_kind.value},
        )
    except Exception:
        return


async def serve_channel_worker(
    *, stack: _WorkerProcessStack, stop: asyncio.Event
) -> int:
    """运行投影循环，并在正常、取消和失败路径释放唯一装配资源。"""
    try:
        await stack.service.run(stop)
    except asyncio.CancelledError:
        return 0
    finally:
        await stack.aclose()
    return 0


async def _run(settings: Settings) -> int:
    from xiaowei_agent.interfaces.local_stack import (
        build_postgres_channel_worker_stack,
    )

    stack = await build_postgres_channel_worker_stack(settings=settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    return await serve_channel_worker(stack=stack, stop=stop)


def main() -> int:
    """加载默认关闭的 worker 配置；不提供 console-script 旁路。"""
    try:
        settings = load_settings()
    except ConfigError:
        _record_process_failure(_FailureKind.CONFIGURATION_INVALID)
        return 2
    if not settings.channel_worker_enabled:
        return 2
    configure_logging(settings)
    try:
        return asyncio.run(_run(settings))
    except Exception:
        _record_process_failure(_FailureKind.CHANNEL_WORKER_FAILURE)
        return 1


if __name__ == "__main__":  # pragma: no cover - 由进程/Compose 调用
    sys.exit(main())


__all__ = ["main", "serve_channel_worker"]
