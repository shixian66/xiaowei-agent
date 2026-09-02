"""终态不可被后到事件覆盖，且必须由存储层承重（ARCHITECTURE §7.3）。"""

import pytest
from pydantic import ValidationError
from tests.conftest import drive_to_terminal

from xiaowei_agent.contracts import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    TaskOutcome,
    TaskStatus,
    TransitionRejection,
)

pytestmark = pytest.mark.security


def test_terminal_set_matches_architecture() -> None:
    assert {s.value for s in TERMINAL_STATUSES} == {
        "succeeded",
        "failed",
        "rejected",
        "canceled",
        "indeterminate",
    }


def test_terminal_statuses_have_no_outgoing_edges() -> None:
    """终态集合与迁移表必须一致，否则两处会各自漂移。"""
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_every_status_is_a_key_in_the_transition_table() -> None:
    """缺键会让 ALLOWED_TRANSITIONS[status] 抛 KeyError 而不是给出确定的拒绝。"""
    for status in TaskStatus:
        assert status in ALLOWED_TRANSITIONS


@pytest.mark.parametrize(
    "terminal", sorted(TERMINAL_STATUSES, key=lambda s: s.value), ids=lambda s: s.value
)
async def test_no_transition_out_of_any_terminal_status(store, task, terminal) -> None:
    await drive_to_terminal(store, task.task_id, terminal)
    current = await store.get(task.task_id)
    for target in TaskStatus:
        result = await store.transition(
            task_id=task.task_id, expected_version=current.version, to_status=target
        )
        assert result.applied is False
        assert result.rejection is TransitionRejection.TERMINAL_PROTECTED
        assert result.winner.status is terminal


async def test_terminal_protection_wins_over_version_mismatch(store, task) -> None:
    """拒绝原因必须指向最具体的违规，否则真实原因会被掩盖。"""
    await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.CANCELED
    )
    result = await store.transition(
        task_id=task.task_id, expected_version=999, to_status=TaskStatus.RUNNING
    )
    assert result.rejection is TransitionRejection.TERMINAL_PROTECTED


def test_task_outcome_rejects_non_terminal_status() -> None:
    with pytest.raises(ValidationError):
        TaskOutcome(
            task_id="t1",
            status=TaskStatus.RUNNING,
            terminal_reason=None,
            evidence_refs=(),
            render_ref=None,
        )


def test_copy_cannot_produce_a_non_terminal_task_outcome() -> None:
    outcome = TaskOutcome(
        task_id="t1",
        status=TaskStatus.SUCCEEDED,
        terminal_reason=None,
        evidence_refs=(),
        render_ref=None,
    )
    with pytest.raises(ValidationError):
        outcome.model_copy(update={"status": TaskStatus.RUNNING})
