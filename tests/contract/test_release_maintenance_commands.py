"""W5 发布维护一次性命令：只输出闭集计数与闭集错误码。

部署证据会被复制进工单与聊天记录，因此命令的 stdout/stderr 就是一条对外泄露面：
request id、subject、actor、作用域与异常正文一律不得出现。
"""

import io
import json
import os

import pytest
from tests.fakes.clock import ManualClock

from xiaowei_agent.contracts.activation import ActivationRetentionReport
from xiaowei_agent.interfaces import activation_retention
from xiaowei_agent.persistence.errors import (
    PersistenceUnavailableCategory,
    PersistenceUnavailableError,
)

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
