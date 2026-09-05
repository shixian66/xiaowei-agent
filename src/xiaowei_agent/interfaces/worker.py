"""Worker 进程入口的信号/退出码边界。"""

import asyncio
import signal
import sys

from xiaowei_agent.application.worker import (
    WorkerInfrastructureExhaustedError,
    WorkerInvariantError,
    WorkerLoop,
    WorkerSystemFailureError,
)
from xiaowei_agent.config import ConfigError, Settings, load_settings
from xiaowei_agent.interfaces.local_stack import LocalStack, build_postgres_local_stack
from xiaowei_agent.log import configure_logging


async def serve_worker(*, stack: LocalStack, stop: asyncio.Event) -> int:
    """运行 Worker 并确保唯一装配资源在所有退出路径释放。"""
    loop = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=stack.settings,
        sleep=asyncio.sleep,
    )
    try:
        await loop.run(stop)
    except asyncio.CancelledError:
        return 0
    except (
        WorkerInfrastructureExhaustedError,
        WorkerInvariantError,
        WorkerSystemFailureError,
        LookupError,
    ):
        return 1
    finally:
        await stack.aclose()
    return 0


async def run_worker(settings: Settings) -> int:
    """装配 Worker，并把 SIGTERM/SIGINT 转换成协作式停止事件。"""
    configure_logging(settings)
    stack = await build_postgres_local_stack(settings=settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    return await serve_worker(stack=stack, stop=stop)


def main() -> int:
    try:
        return asyncio.run(run_worker(load_settings()))
    except ConfigError:
        sys.stderr.write("xiaowei-worker: configuration_error\n")
        return 2
    except Exception:
        sys.stderr.write("xiaowei-worker: startup_failed\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
