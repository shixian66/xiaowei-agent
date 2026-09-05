"""Worker 进程入口的信号/退出码边界。"""

import asyncio

from xiaowei_agent.application.worker import (
    WorkerInfrastructureExhaustedError,
    WorkerLoop,
    WorkerSystemFailureError,
)
from xiaowei_agent.interfaces.local_stack import LocalStack


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
        WorkerSystemFailureError,
        LookupError,
    ):
        return 1
    finally:
        await stack.aclose()
    return 0
