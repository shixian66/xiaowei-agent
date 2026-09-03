"""Runner 契约：采纳存储层 winner、不越过终态、不触碰 Gateway。"""

import inspect
from pathlib import Path

import pytest
from tests.fakes.admission import CONTEXT
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET

from xiaowei_agent.contracts import TaskStatus
from xiaowei_agent.runners.fake import ScriptedRunner, TerminalOrLeasedTaskError
from xiaowei_agent.runners.runner import WorkflowRunner

# ``WorkflowRunner`` 的活输入（见 ARCHITECTURE §5.6）。ScriptedRunner 不消费它们，
# 但契约要求它们被传入——测试按契约调用，不按实现走捷径。
_LIVE = {"plan": FIXTURE_PLAN, "target": FIXTURE_TARGET, "context": CONTEXT}


def test_scripted_runner_satisfies_the_workflow_runner_protocol(store) -> None:
    runner: WorkflowRunner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    assert runner is not None


async def test_runner_adopts_the_storage_winner_version(store, task) -> None:
    runner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    outcome = await runner.start(task.task_id, **_LIVE)
    assert outcome.status is TaskStatus.SUCCEEDED
    final = await store.get(task.task_id)
    assert final.version == 3  # created→planning→running→succeeded


async def test_resume_on_a_terminal_task_does_not_overwrite_it(store, task) -> None:
    """终态保护由存储层承重；Runner 只是拿不到租约而已。"""
    runner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED)
    await runner.start(task.task_id, **_LIVE)
    before = await store.get(task.task_id)
    with pytest.raises(TerminalOrLeasedTaskError):
        await runner.resume(task.task_id, context=CONTEXT, target=FIXTURE_TARGET)
    after = await store.get(task.task_id)
    assert after.version == before.version
    assert after.status is TaskStatus.SUCCEEDED


def test_runner_rejects_a_terminal_status_unreachable_from_running(store) -> None:
    with pytest.raises(ValueError, match="not reachable from RUNNING"):
        ScriptedRunner(store, outcome_status=TaskStatus.REJECTED)


def test_runner_rejects_a_non_terminal_outcome(store) -> None:
    with pytest.raises(ValueError, match="terminal outcome status"):
        ScriptedRunner(store, outcome_status=TaskStatus.RUNNING)


@pytest.mark.parametrize("bad_owner", ["", "  ", " w1 "])
def test_runner_rejects_a_blank_or_padded_owner(store, bad_owner: str) -> None:
    with pytest.raises(ValueError, match="owner"):
        ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED, owner=bad_owner)


async def test_runner_holds_the_lease_under_its_own_owner(store, task) -> None:
    """owner 必须真的被用于取租约。"""
    runner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED, owner="w-alpha")
    await runner.start(task.task_id, **_LIVE)
    final = await store.get(task.task_id)
    assert final.lease_owner == "w-alpha"


async def test_two_runners_cannot_drive_the_same_task_concurrently(store, task) -> None:
    """第二个 owner 拿不到租约，必须失败而不是并行推进同一任务。"""
    assert await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    runner = ScriptedRunner(store, outcome_status=TaskStatus.SUCCEEDED, owner="w2")
    with pytest.raises(TerminalOrLeasedTaskError):
        await runner.start(task.task_id, **_LIVE)


def test_runner_never_imports_a_gateway_or_adapter() -> None:
    """M2 的 fake runner 不执行步骤，因此 adapter 调用次数恒为 0。

    **扫 AST 的 import 而不是原始文本**：文本扫描会被 docstring 里"不接触
    ToolGateway"这句话本身触发，断言的就不再是代码行为。
    """
    import ast

    from xiaowei_agent.runners import fake

    source = Path(inspect.getfile(fake)).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if module:
            imported.add(module)
        if isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{a.name}" for a in node.names}
    assert not any("tools" in name for name in imported), imported
