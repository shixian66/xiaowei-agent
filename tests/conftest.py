"""全局 fixture。

不定义自定义的 socket 放行 fixture；未来需要放行时使用 pytest-socket
官方提供的 ``socket_enabled``。
"""

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from xiaowei_agent.contracts import ExecutionPlan

_PREFIX = "XIAOWEI_"


@pytest.fixture(autouse=True)
def clean_xiaowei_env() -> Iterator[None]:
    """每个用例前清除全部 XIAOWEI_* 变量，结束后原样恢复。"""
    saved = {k: v for k, v in os.environ.items() if k.upper().startswith(_PREFIX)}
    for k in saved:
        del os.environ[k]
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.upper().startswith(_PREFIX)]:
            del os.environ[k]
        os.environ.update(saved)


@pytest.fixture
def two_step_plan() -> "ExecutionPlan":
    """两步计划；用于证明 ordered_steps 的顺序进入 plan_hash。"""
    from tests.fakes.fixtures import FIXTURE_PLAN

    first = FIXTURE_PLAN.steps[0]
    second = first.model_copy(update={"step_id": "s2", "depends_on": ("s1",)})
    return FIXTURE_PLAN.model_copy(update={"steps": (first, second)})
