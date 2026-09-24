"""内存 ``ActivationStore`` 绑定与协议表面。"""

import inspect

import pytest
from tests.suites.activation_store import ACTIVATION_STORE_CASES, bind

from xiaowei_agent.persistence.activation import ActivationStore
from xiaowei_agent.persistence.fake import InMemoryActivationStore


def _protocol_surface(protocol: type) -> frozenset[str]:
    return frozenset(
        name
        for name in dir(protocol)
        if not name.startswith("_") and callable(getattr(protocol, name, None))
    )


def test_activation_store_has_only_three_narrow_methods() -> None:
    assert _protocol_surface(ActivationStore) == {
        "create_or_reuse",
        "list_pending",
        "load",
    }
    assert set(inspect.signature(ActivationStore.create_or_reuse).parameters) == {
        "self",
        "command",
    }
    assert set(inspect.signature(ActivationStore.load).parameters) == {"self", "query"}
    assert set(inspect.signature(ActivationStore.list_pending).parameters) == {
        "self",
        "query",
    }


@pytest.fixture
def activation_store(clock, memory_state):
    return InMemoryActivationStore(clock=clock, state=memory_state)


bind(globals(), ACTIVATION_STORE_CASES)
