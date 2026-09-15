"""终态不可被后到事件覆盖，且必须由存储层承重（ARCHITECTURE §7.3）。

本文件有两类用例，边界是"要不要 ``store``"：

- **契约层的终态定义**（终态集合、迁移表、``TaskOutcome`` 的终态约束）留在本文件。
  它们与存储实现无关，绑到 PostgreSQL 上只会重复跑一遍同样的纯函数断言。
- **存储层的终态保护**（后到事件被拒、终态保护优先于版本不匹配）定义在
  ``tests/suites/task_store.py``，由本文件绑到内存实现、由 ``tests/integration/``
  绑到 PostgreSQL。
"""

import pytest
from pydantic import ValidationError
from tests.suites.task_store import TERMINAL_PROTECTION_CASES, bind

from xiaowei_agent.contracts import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    TaskOutcome,
    TaskStatus,
)

pytestmark = pytest.mark.security

bind(globals(), TERMINAL_PROTECTION_CASES)


def test_terminal_set_matches_architecture() -> None:
    assert {s.value for s in TERMINAL_STATUSES} == {
        "succeeded",
        "failed",
        "rejected",
        "clarification_required",
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
