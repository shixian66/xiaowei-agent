"""M6b L0：真实 adapter 新增边界必须 100% fail-closed。"""

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from xiaowei_agent.capabilities.specs import OP_COUNT, OP_LIST, SLOW_QUERY_SURFACE
from xiaowei_agent.capabilities.target import TargetResolutionError, resolve_target
from xiaowei_agent.contracts import AdapterStatus, RequestContext, ToolCall
from xiaowei_agent.planning.starrocks.compiler import COUNT_V1, LIST_V1, compile_sql
from xiaowei_agent.planning.starrocks.params import SlowQueryParams
from xiaowei_agent.tools.starrocks import (
    PhysicalIdentityProbe,
    StarRocksConnection,
    StarRocksReadonlyAdapter,
    StarRocksReadonlyAdapterConfig,
)

pytestmark = pytest.mark.security

_CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "corpus" / "m6b_starrocks_l0.json").read_text(
        encoding="utf-8"
    )
)["cases"]
_CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="m6b-operator",
    environment_id="test",
    trace_id="6" * 32,
    policy_revision="policy-2026-09-05.2",
)
_PARAMS = SlowQueryParams(
    window_start=dt.datetime(2026, 9, 7, 9, tzinfo=dt.UTC),
    window_end=dt.datetime(2026, 9, 7, 10, tzinfo=dt.UTC),
    min_query_time_ms=10_000,
    row_limit=20,
)


class _NoConnectionFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, password: str) -> StarRocksConnection:
        del password
        self.calls += 1
        raise AssertionError("L0 rejection reached the connection factory")


def _adapter(factory: _NoConnectionFactory) -> StarRocksReadonlyAdapter:
    return StarRocksReadonlyAdapter(
        config=StarRocksReadonlyAdapterConfig(
            password_file=str(Path("/not/read/on/l0/rejection")),
            query_timeout_seconds=20,
            expected_version_sha256="d" * 64,
            expected_grants_sha256="a" * 64,
            expected_ddl_sha256="b" * 64,
            expected_identity_sha256="c" * 64,
            audit_table=SLOW_QUERY_SURFACE.allowed_tables[0],
            list_columns=SLOW_QUERY_SURFACE.allowed_columns,
            identity_probe=PhysicalIdentityProbe(
                statement="SELECT `id` FROM `meta`.`identity` LIMIT 1",
                result_column="id",
            ),
            list_operation=OP_LIST,
            count_operation=OP_COUNT,
            list_template_id=LIST_V1,
            count_template_id=COUNT_V1,
        ),
        connection_factory=factory,
    )


def _call() -> ToolCall:
    return ToolCall(
        gateway="starrocks",
        operation=OP_LIST,
        step_id="s1",
        typed_args={
            "sql": compile_sql(
                template_id=LIST_V1,
                params=_PARAMS,
                surface=SLOW_QUERY_SURFACE,
            ),
            "sql_template_id": LIST_V1,
            **_PARAMS.to_typed_arguments(),
        },
        timeout_seconds=30.0,
        idempotency_key="m6b-l0",
    )


def _by(carrier: str) -> list[dict[str, Any]]:
    return [case for case in _CASES if case["carrier"] == carrier]


def test_every_m6b_l0_case_has_an_executing_driver() -> None:
    assert {case["carrier"] for case in _CASES} == {
        "operation",
        "argument",
        "identity",
        "target",
    }
    assert len({case["id"] for case in _CASES}) == len(_CASES) == 8


@pytest.mark.parametrize("case", _by("operation"), ids=lambda case: case["id"])
async def test_operation_expansion_stops_before_connection(case: dict[str, Any]) -> None:
    factory = _NoConnectionFactory()
    response = await _adapter(factory).execute(
        _call().model_copy(update={"operation": case["value"]}),
        context=_CONTEXT,
    )
    assert response.status is AdapterStatus.ERROR
    assert factory.calls == 0


@pytest.mark.parametrize("case", _by("argument"), ids=lambda case: case["id"])
async def test_connection_profile_pollution_stops_before_connection(
    case: dict[str, Any],
) -> None:
    factory = _NoConnectionFactory()
    call = _call()
    response = await _adapter(factory).execute(
        call.model_copy(
            update={"typed_args": dict(call.typed_args) | {case["value"]: "polluted"}}
        ),
        context=_CONTEXT,
    )
    assert response.status is AdapterStatus.ERROR
    assert factory.calls == 0


@pytest.mark.parametrize("case", _by("identity"), ids=lambda case: case["id"])
def test_identity_sql_surface_cannot_expand(case: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PhysicalIdentityProbe(statement=case["value"], result_column="id")


def test_unapproved_test_target_remains_ambiguous() -> None:
    with pytest.raises(TargetResolutionError):
        resolve_target(context=_CONTEXT, params=_PARAMS)
