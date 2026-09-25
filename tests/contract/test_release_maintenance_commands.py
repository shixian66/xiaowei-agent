"""W5 发布维护一次性命令：只输出闭集计数与闭集错误码。

部署证据会被复制进工单与聊天记录，因此命令的 stdout/stderr 就是一条对外泄露面：
request id、subject、actor、作用域与异常正文一律不得出现。
"""

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.clock import ManualClock

from xiaowei_agent.contracts.activation import ActivationRetentionReport
from xiaowei_agent.interfaces import activation_retention, legacy_identity_migration
from xiaowei_agent.persistence.errors import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)
from xiaowei_agent.persistence.fake import InMemoryUserDirectoryStore
from xiaowei_agent.persistence.identity import AdminAuditUnwritableError
from xiaowei_agent.persistence.memory import InMemoryPersistenceState

_SECRETISH = "ou_" + "leaky-subject"


class _Engine:
    def __init__(self) -> None:
        self.disposed = 0

    async def dispose(self) -> None:
        self.disposed += 1


class _Store:
    def __init__(self, outcome: ActivationRetentionReport | Exception) -> None:
        self._outcome = outcome
        self.calls = 0

    async def purge_expired_terminal(self) -> ActivationRetentionReport:
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    argv: list[str] | None = None,
    outcome: ActivationRetentionReport | Exception | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str, _Engine, _Store]:
    for name in list(os.environ):
        if name.startswith("XIAOWEI_"):
            monkeypatch.delenv(name)
    for name, value in ({"XIAOWEI_ENVIRONMENT_ID": "dev"} if env is None else env).items():
        monkeypatch.setenv(name, value)
    engine = _Engine()
    store = _Store(
        ActivationRetentionReport(
            pending_expired=1, approved_deleted=2, rejected_deleted=0, expired_deleted=3
        )
        if outcome is None
        else outcome
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    code = activation_retention.main(
        [] if argv is None else argv,
        stdout=stdout,
        stderr=stderr,
        engine_factory=lambda _settings: engine,  # type: ignore[arg-type,return-value]
        store_factory=lambda _engine, _clock: store,  # type: ignore[arg-type,return-value]
        clock=ManualClock(),
    )
    return code, stdout.getvalue(), stderr.getvalue(), engine, store


def test_success_prints_exactly_the_four_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, engine, store = _run(monkeypatch)

    assert code == 0
    assert err == ""
    assert json.loads(out) == {
        "pending_expired": 1,
        "approved_deleted": 2,
        "rejected_deleted": 0,
        "expired_deleted": 3,
    }
    assert out.count("\n") == 1
    assert store.calls == 1
    assert engine.disposed == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["--tenant", "dev-local"],
        ["--environment", "dev"],
        ["--cutoff", "2026-01-01"],
        ["--request-id", "r-1"],
        ["--force"],
        ["extra"],
    ],
)
def test_any_argument_is_rejected_before_touching_the_database(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    code, out, err, engine, store = _run(monkeypatch, argv=argv)

    assert code == 2
    assert out == ""
    assert err == "activation-retention: usage_invalid\n"
    assert store.calls == 0
    assert engine.disposed == 0


def test_configuration_error_is_a_closed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, _, store = _run(
        monkeypatch, env={"XIAOWEI_ENVIRONMENT_ID": "dev", "XIAOWEI_UNKNOWN": _SECRETISH}
    )

    assert code == 2
    assert out == ""
    assert err == "activation-retention: configuration_error\n"
    assert store.calls == 0


def test_database_unavailable_is_a_closed_code_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err, engine, _ = _run(
        monkeypatch,
        outcome=PersistenceUnavailableError(
            category=PersistenceUnavailableCategory.CONNECT
        ),
    )

    assert code == 1
    assert out == ""
    assert err == "activation-retention: database_unavailable\n"
    assert engine.disposed == 1


def test_unexpected_failure_never_echoes_the_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, out, err, engine, _ = _run(
        monkeypatch, outcome=RuntimeError(f"row {_SECRETISH} actor=admin-1")
    )

    assert code == 1
    assert out == ""
    assert err == "activation-retention: retention_failed\n"
    assert _SECRETISH not in out + err
    assert engine.disposed == 1


def test_command_has_no_scope_or_cutoff_parameter() -> None:
    import inspect

    assert set(inspect.signature(activation_retention.main).parameters) == {
        "argv",
        "stdout",
        "stderr",
        "engine_factory",
        "store_factory",
        "clock",
    }


# --- 旧身份一次性迁移 ----------------------------------------------------------

_ALICE_SUBJECT = "ou_" + "alice-subject"
_BOB_SUBJECT = "ou_" + "bob-subject"


def _identity_document(path: Path, *entries: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": "dev-local",
                "environment_id": "dev",
                "entries": list(entries),
            }
        ),
        encoding="utf-8",
    )
    return path


def _two_entries(path: Path) -> Path:
    return _identity_document(
        path,
        {"subject_ref": _ALICE_SUBJECT, "actor": "alice-actor", "labels": ["operator"]},
        {"subject_ref": _BOB_SUBJECT, "actor": "bob-actor", "labels": ["approver"]},
    )


class _Directory:
    def __init__(self, state: InMemoryPersistenceState, clock: ManualClock) -> None:
        self.state = state
        self.store = InMemoryUserDirectoryStore(clock=clock, state=state)
        self.opened = 0


def _migrate(
    monkeypatch: pytest.MonkeyPatch,
    document: Path,
    *,
    directory: Any = None,
    argv: list[str] | None = None,
) -> tuple[int, str, str, _Engine, Any]:
    for name in list(os.environ):
        if name.startswith("XIAOWEI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XIAOWEI_ENVIRONMENT_ID", "dev")
    clock = ManualClock()
    target = _Directory(InMemoryPersistenceState(), clock) if directory is None else directory
    engine = _Engine()

    def factory(_engine: object, _clock: object) -> object:
        target.opened += 1
        return target.store

    stdout, stderr = io.StringIO(), io.StringIO()
    code = legacy_identity_migration.main(
        [] if argv is None else argv,
        stdout=stdout,
        stderr=stderr,
        engine_factory=lambda _settings: engine,  # type: ignore[arg-type,return-value]
        directory_factory=factory,  # type: ignore[arg-type]
        clock=clock,
        document_path=str(document),
    )
    return code, stdout.getvalue(), stderr.getvalue(), engine, target


def _facts(state: InMemoryPersistenceState) -> tuple[int, int, int, int]:
    return (
        len(state.user_accounts),
        len(state.user_role_assignments),
        len(state.external_identities),
        len(state.admin_audit_events),
    )


def _assert_no_identity_leak(text: str) -> None:
    for leaked in (_ALICE_SUBJECT, _BOB_SUBJECT, "alice-actor", "bob-actor", "approver"):
        assert leaked not in text


def test_identity_migration_prints_counts_only_and_reruns_as_zero_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    document = _two_entries(tmp_path / "identities.json")
    directory = _Directory(InMemoryPersistenceState(), ManualClock())

    first = _migrate(monkeypatch, document, directory=directory)
    second = _migrate(monkeypatch, document, directory=directory)

    for code, out, err, engine, _ in (first, second):
        assert code == 0, err
        assert err == ""
        assert engine.disposed == 1
        _assert_no_identity_leak(out)
    assert json.loads(first[1]) == {
        "status": "migrated",
        "created_count": 2,
        "skipped_count": 0,
        "deferred_count": 1,
    }
    assert json.loads(second[1]) == {
        "status": "migrated",
        "created_count": 0,
        "skipped_count": 2,
        "deferred_count": 1,
    }


def test_missing_document_is_not_applicable_without_opening_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, out, err, engine, directory = _migrate(monkeypatch, tmp_path / "absent.json")

    assert code == 0
    assert err == ""
    assert json.loads(out) == {"status": "not_applicable"}
    assert directory.opened == 0
    assert engine.disposed == 0


@pytest.mark.parametrize("shape", ["corrupt", "symlink", "wrong_scope", "empty"])
def test_invalid_document_fails_closed_with_zero_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shape: str
) -> None:
    real = _two_entries(tmp_path / "real.json")
    document = tmp_path / "identities.json"
    if shape == "corrupt":
        document.write_text("{not json " + _ALICE_SUBJECT, encoding="utf-8")
    elif shape == "symlink":
        document.symlink_to(real)
    elif shape == "wrong_scope":
        document.write_text(
            real.read_text(encoding="utf-8").replace('"dev"', '"prod"'), encoding="utf-8"
        )
    else:
        document.write_text("", encoding="utf-8")

    code, out, err, engine, directory = _migrate(monkeypatch, document)

    assert code == 1
    assert out == ""
    assert err == "legacy-identity-migration: document_invalid\n"
    assert _facts(directory.state) == (0, 0, 0, 0)
    assert engine.disposed == 1


def test_partial_conflict_and_oversized_values_reject_the_whole_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = _Directory(InMemoryPersistenceState(), ManualClock())
    _migrate(
        monkeypatch,
        _identity_document(
            tmp_path / "seed.json",
            {"subject_ref": _ALICE_SUBJECT, "actor": "alice-actor", "labels": ["viewer"]},
        ),
        directory=directory,
    )
    seeded = _facts(directory.state)
    conflicting = _two_entries(tmp_path / "identities.json")

    code, out, err, _, _ = _migrate(monkeypatch, conflicting, directory=directory)

    assert (code, out) == (1, "")
    assert err == "legacy-identity-migration: migration_conflict\n"
    assert _facts(directory.state) == seeded

    oversized = _identity_document(
        tmp_path / "oversized.json",
        {"subject_ref": _BOB_SUBJECT, "actor": "x" * 5000, "labels": ["viewer"]},
    )
    code, out, err, _, _ = _migrate(monkeypatch, oversized, directory=directory)

    assert (code, out) == (1, "")
    assert err in {
        "legacy-identity-migration: migration_conflict\n",
        "legacy-identity-migration: document_invalid\n",
    }
    assert _facts(directory.state) == seeded


def test_audit_failure_is_a_closed_conflict_with_zero_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = _Directory(InMemoryPersistenceState(), ManualClock())

    async def unwritable(**_: object) -> object:
        raise AdminAuditUnwritableError

    monkeypatch.setattr(directory.store, "apply", unwritable)

    code, out, err, engine, _ = _migrate(
        monkeypatch, _two_entries(tmp_path / "identities.json"), directory=directory
    )

    assert (code, out) == (1, "")
    assert err == "legacy-identity-migration: migration_conflict\n"
    assert _facts(directory.state) == (0, 0, 0, 0)
    assert engine.disposed == 1


def test_database_failure_never_echoes_exception_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    directory = _Directory(InMemoryPersistenceState(), ManualClock())

    async def broken(**_: object) -> object:
        raise RuntimeError(f"subject {_ALICE_SUBJECT} actor alice-actor")

    monkeypatch.setattr(directory.store, "load_account", broken)

    code, out, err, engine, _ = _migrate(
        monkeypatch, _two_entries(tmp_path / "identities.json"), directory=directory
    )

    assert (code, out) == (1, "")
    assert err == "legacy-identity-migration: migration_failed\n"
    _assert_no_identity_leak(err)
    assert engine.disposed == 1


@pytest.mark.parametrize(
    "argv",
    [["other.json"], ["--path", "other.json"], ["--tenant", "t"], ["--map", "x=y"]],
)
def test_identity_migration_accepts_no_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> None:
    code, out, err, engine, directory = _migrate(
        monkeypatch, _two_entries(tmp_path / "identities.json"), argv=argv
    )

    assert (code, out) == (2, "")
    assert err == "legacy-identity-migration: usage_invalid\n"
    assert directory.opened == 0
    assert engine.disposed == 0


def test_identity_document_path_is_fixed_and_not_a_setting() -> None:
    from xiaowei_agent.config import Settings

    assert legacy_identity_migration.LEGACY_IDENTITY_DOCUMENT_PATH.startswith("/run/")
    assert "feishu_identity_file" not in Settings.model_fields
