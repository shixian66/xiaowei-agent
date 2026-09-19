"""破坏性降级授权默认 fail-closed。"""

from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from xiaowei_agent.persistence.migrations import guards


class _ScalarResult:
    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _Connection:
    def __init__(self, *counts: int) -> None:
        self._counts = iter(counts)

    def execute(self, statement: object) -> _ScalarResult:
        assert statement is not None
        return _ScalarResult(next(self._counts))


def _set_attributes(monkeypatch: pytest.MonkeyPatch, attributes: dict[str, Any]) -> None:
    monkeypatch.setattr(
        guards.context,
        "config",
        SimpleNamespace(attributes=attributes),
        raising=False,
    )


def test_guard_allows_an_empty_milestone(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_attributes(monkeypatch, {})
    guards.require_destructive_authorization(
        _Connection(0),
        guarded=((sa.table("task_submissions"), "task_submissions"),),
    )


def test_guard_rejects_data_when_the_attribute_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_attributes(monkeypatch, {})
    with pytest.raises(guards.MigrationSafetyError) as caught:
        guards.require_destructive_authorization(
            _Connection(2),
            guarded=((sa.table("task_submissions"), "task_submissions"),),
        )
    assert str(caught.value) == "DESTRUCTIVE_DOWNGRADE_REJECTED"
    assert caught.value.counts == (("task_submissions", 2),)


def test_upgrade_precondition_rejects_protected_rows() -> None:
    with pytest.raises(guards.MigrationPreconditionError) as caught:
        guards.require_no_rows(
            _Connection(1),
            guarded=((sa.table("task_submissions"), "legacy_parent_context"),),
        )
    assert str(caught.value) == "MIGRATION_PRECONDITION_FAILED"
    assert caught.value.counts == (("legacy_parent_context", 1),)


def test_upgrade_precondition_allows_empty_sets() -> None:
    guards.require_no_rows(
        _Connection(0),
        guarded=((sa.table("task_submissions"), "legacy_parent_context"),),
    )


def test_guard_requires_the_literal_true_value(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_attributes(monkeypatch, {"allow_destructive": 1})
    with pytest.raises(guards.MigrationSafetyError):
        guards.require_destructive_authorization(
            _Connection(1),
            guarded=((sa.table("task_submissions"), "task_submissions"),),
        )


def test_guard_accepts_explicit_destructive_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_attributes(monkeypatch, {"allow_destructive": True})
    guards.require_destructive_authorization(
        _Connection(1),
        guarded=((sa.table("task_submissions"), "task_submissions"),),
    )


def test_rev_0013_downgrade_requires_authorization_for_parent_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xiaowei_agent.persistence.migrations.versions import (
        rev_0013_clarification_parent as revision,
    )

    _set_attributes(monkeypatch, {})
    monkeypatch.setattr(revision.context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(revision.op, "get_bind", lambda: _Connection(1))
    monkeypatch.setattr(revision.op, "drop_constraint", lambda *_, **__: None)
    monkeypatch.setattr(revision.op, "alter_column", lambda *_, **__: None)
    monkeypatch.setattr(revision.op, "create_foreign_key", lambda *_, **__: None)
    monkeypatch.setattr(revision.op, "create_index", lambda *_, **__: None)

    with pytest.raises(guards.MigrationSafetyError) as caught:
        revision.downgrade()
    assert caught.value.counts == (("task_parent_context", 1),)
