"""终态激活保留的固定策略与窄表面（W5 §2.1）。

保留期是代码常量，不是配置：做成环境变量就会有人把它改成 0（静默删掉仍需核对的
决定）或无限大（受控 PII 永不清理）。清理入口也只有一个无参方法——调用方传 cutoff、
作用域或 request id 都等于把"删哪些"交给了调用方。
"""

import ast
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.suites.activation_store import ACTIVATION_RETENTION_CASES, bind

from xiaowei_agent.config import _FIELD_TO_ENV
from xiaowei_agent.contracts.activation import ActivationRetentionReport
from xiaowei_agent.persistence.activation import (
    ACTIVATION_TERMINAL_RETENTION_DAYS,
    ActivationRetentionStore,
)
from xiaowei_agent.persistence.fake import (
    InMemoryActivationRetentionStore,
    InMemoryActivationStore,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"


def test_retention_period_is_a_fixed_thirty_days() -> None:
    assert ACTIVATION_TERMINAL_RETENTION_DAYS == 30
    assert not any("RETENTION" in name for name in _FIELD_TO_ENV.values())


def test_retention_store_has_one_parameterless_method() -> None:
    surface = {
        name
        for name in dir(ActivationRetentionStore)
        if not name.startswith("_")
        and callable(getattr(ActivationRetentionStore, name, None))
    }

    assert surface == {"purge_expired_terminal"}
    assert set(
        inspect.signature(ActivationRetentionStore.purge_expired_terminal).parameters
    ) == {"self"}


def test_retention_report_is_exactly_four_non_negative_counts() -> None:
    report = ActivationRetentionReport(
        pending_expired=1, approved_deleted=2, rejected_deleted=3, expired_deleted=4
    )

    assert report.model_dump() == {
        "pending_expired": 1,
        "approved_deleted": 2,
        "rejected_deleted": 3,
        "expired_deleted": 4,
    }
    with pytest.raises(ValidationError):
        ActivationRetentionReport(
            pending_expired=-1, approved_deleted=0, rejected_deleted=0, expired_deleted=0
        )
    with pytest.raises(ValidationError):
        ActivationRetentionReport.model_validate(
            {
                "pending_expired": 0,
                "approved_deleted": 0,
                "rejected_deleted": 0,
                "expired_deleted": 0,
                "request_ids": ["r-1"],
            }
        )


def _callers_of(name: str) -> set[str]:
    callers: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == name:
                callers.add(path.relative_to(_SRC).as_posix())
    return callers


def test_only_the_one_shot_command_invokes_retention() -> None:
    """没有 Web 路由、listener 或 worker 能触发清理；只有显式一次性命令。"""
    assert _callers_of("purge_expired_terminal") == {
        "interfaces/activation_retention.py"
    }


def test_retention_store_is_not_assembled_into_any_long_running_process() -> None:
    for module in ("local_stack.py", "web_app.py", "api.py", "worker.py"):
        source = (_SRC / "interfaces" / module).read_text(encoding="utf-8")
        assert "ActivationRetentionStore" not in source, module


@pytest.fixture
def activation_store(clock, memory_state):
    return InMemoryActivationStore(clock=clock, state=memory_state)


@pytest.fixture
def retention_store(clock, memory_state):
    return InMemoryActivationRetentionStore(clock=clock, state=memory_state)


@pytest.fixture
def seed_activation(memory_state):
    async def seed(request):
        memory_state.activation_requests[request.request_id] = request

    return seed


bind(globals(), ACTIVATION_RETENTION_CASES)
