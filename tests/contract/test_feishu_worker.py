"""M7 projection worker 的进程入口、停止与资源释放契约。"""

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable

import pytest

from xiaowei_agent.config import Settings
from xiaowei_agent.interfaces import feishu_worker
from xiaowei_agent.interfaces.feishu_worker import main, serve_channel_worker


class _Service:
    def __init__(self, action: Callable[[asyncio.Event], Awaitable[None]]) -> None:
        self._action = action

    async def run(self, stop: asyncio.Event) -> None:
        await self._action(stop)


class _Stack:
    def __init__(self, service: _Service) -> None:
        self.service = service
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_serve_channel_worker_releases_stack_after_clean_stop() -> None:
    async def finish(stop: asyncio.Event) -> None:
        assert stop.is_set()

    stack = _Stack(_Service(finish))
    stop = asyncio.Event()
    stop.set()

    assert await serve_channel_worker(stack=stack, stop=stop) == 0
    assert stack.closed is True


@pytest.mark.asyncio
async def test_serve_channel_worker_treats_task_cancellation_as_clean_shutdown() -> None:
    async def cancel(_: asyncio.Event) -> None:
        raise asyncio.CancelledError

    stack = _Stack(_Service(cancel))

    assert await serve_channel_worker(stack=stack, stop=asyncio.Event()) == 0
    assert stack.closed is True


@pytest.mark.asyncio
async def test_serve_channel_worker_releases_stack_before_failure_escapes() -> None:
    async def fail(_: asyncio.Event) -> None:
        raise RuntimeError("provider response body must not escape")

    stack = _Stack(_Service(fail))

    with pytest.raises(RuntimeError, match="provider response body must not escape"):
        await serve_channel_worker(stack=stack, stop=asyncio.Event())
    assert stack.closed is True


def test_process_main_is_default_closed_before_starting_async_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        feishu_worker,
        "load_settings",
        lambda: Settings(environment_id="dev"),
    )

    def fail_run(_: object) -> int:
        raise AssertionError("disabled worker must not enter asyncio runtime")

    monkeypatch.setattr(feishu_worker.asyncio, "run", fail_run)

    assert main() == 2


def test_real_module_entry_is_silent_and_closed_with_default_profile() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("XIAOWEI_")
    }
    env["XIAOWEI_ENVIRONMENT_ID"] = "dev"

    completed = subprocess.run(
        [sys.executable, "-m", "xiaowei_agent.interfaces.feishu_worker"],
        cwd=os.fspath(os.getcwd()),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_real_module_entry_distinguishes_invalid_configuration() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("XIAOWEI_")
    }
    env.update(
        {
            "XIAOWEI_ENVIRONMENT_ID": "dev",
            "XIAOWEI_CHANNEL_WORKER_ENABLED": "true",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-m", "xiaowei_agent.interfaces.feishu_worker"],
        cwd=os.fspath(os.getcwd()),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "channel projection worker configuration invalid\n"


def test_process_main_reports_only_a_closed_failure_kind(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        environment_id="dev",
        channel_worker_enabled=True,
        feishu_app_id="cli_test_app",
        feishu_app_secret_file="/run/secrets/feishu_app_" + "secret",
        web_public_origin="https://ops.example.test",
    )
    monkeypatch.setattr(feishu_worker, "load_settings", lambda: settings)
    monkeypatch.setattr(feishu_worker, "configure_logging", lambda _: None)

    async def fail(_: Settings) -> int:
        raise RuntimeError("provider response body must not escape")

    monkeypatch.setattr(feishu_worker, "_run", fail)

    with caplog.at_level(logging.ERROR):
        assert main() == 1

    assert "provider response body must not escape" not in caplog.text
    assert [record.failure_kind for record in caplog.records] == [
        "channel_worker_failure"
    ]
    assert [record.getMessage() for record in caplog.records] == [
        "channel projection worker stopped"
    ]


def test_process_failure_messages_cover_the_closed_taxonomy() -> None:
    assert set(feishu_worker._FAILURE_MESSAGES) == set(feishu_worker._FailureKind)
