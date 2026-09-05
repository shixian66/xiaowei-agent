"""wheel 在无仓库源码、无 tests/ 的干净环境中仍能完成 fake 闭环。"""

import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def test_wheel_contains_the_packaged_recording_and_runs_without_tests(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    dist = tmp_path / "dist"
    venv = tmp_path / "venv"
    cache = tmp_path / "uv-cache"
    subprocess.run(  # noqa: S603 -- uv 是 shutil.which 解析出的绝对路径
        [
            uv,
            "build",
            "--wheel",
            "--offline",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--cache-dir",
            str(cache),
            "--out-dir",
            str(dist),
        ],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = list(dist.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    assert "xiaowei_agent/tools/starrocks_recording.py" in names
    assert not any(name.startswith("tests/") for name in names)

    subprocess.run(  # noqa: S603 -- 当前 Python 与目标路径均由测试控制
        [
            sys.executable,
            "-m",
            "venv",
            str(venv),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    purelib_result = subprocess.run(  # noqa: S603 -- venv Python 位于测试临时目录
        [
            str(venv / "bin/python"),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    purelib = Path(purelib_result.stdout.strip())
    (purelib / "locked-runtime-dependencies.pth").write_text(
        sysconfig.get_paths()["purelib"] + "\n",
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603 -- uv 是 shutil.which 解析出的绝对路径
        [
            uv,
            "pip",
            "install",
            "--no-deps",
            "--offline",
            "--cache-dir",
            str(cache),
            "--python",
            str(venv / "bin/python"),
            str(wheels[0]),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    probe = """
import asyncio
import datetime as dt
import importlib.util
import pathlib
import xiaowei_agent
from xiaowei_agent.application.worker import WorkerLoop
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    Channel,
    RequestContext,
    RequestEnvelope,
    TaskLookup,
    TaskStatus,
    TaskSubmission,
)
from xiaowei_agent.interfaces.local_stack import build_in_memory_local_stack

async def run():
    now = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.UTC)
    settings = Settings(environment_id="dev")
    stack = build_in_memory_local_stack(settings=settings, clock=lambda: now)
    assert "venv" in pathlib.Path(xiaowei_agent.__file__).parts
    context = RequestContext(
        tenant_id=settings.tenant_id,
        actor=settings.actor,
        environment_id=settings.environment_id,
        trace_id="0" * 32,
        policy_revision=stack.policy_revision,
    )
    submission = TaskSubmission(
        envelope=RequestEnvelope(
            request_id="wheel-request",
            tenant_id=context.tenant_id,
            actor=context.actor,
            channel=Channel.CLI,
            text="检查最近三十分钟慢查询",
            idempotency_key="wheel-idem",
            environment_id=context.environment_id,
        ),
        context=context,
        as_of=now,
    )
    pending = await stack.runtime.submit_task(submission=submission)
    worker = WorkerLoop(
        runtime=stack.runtime,
        task_store=stack.task_store,
        clock=stack.clock,
        monotonic=stack.monotonic,
        settings=settings,
        sleep=asyncio.sleep,
    )
    assert await worker.poll_once() == 1
    view = await stack.runtime.query_task(
        lookup=TaskLookup(
            task_id=pending.task_id,
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
        )
    )
    assert view.status is TaskStatus.SUCCEEDED
    assert await stack.evidence_ledger.load(task_id=pending.task_id)
    try:
        tests_recording = importlib.util.find_spec("tests.fakes.recordings")
    except ModuleNotFoundError:
        tests_recording = None
    assert tests_recording is None
    await stack.aclose()

asyncio.run(run())
"""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    completed = subprocess.run(  # noqa: S603 -- venv Python 位于本测试创建的临时目录
        [str(venv / "bin/python"), "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
