"""F1 契约的安全边界：SQL bytes 只在进程内 HydratedQuery，且不外泄。"""

import hashlib
import pickle

import pytest
from pydantic import BaseModel, ValidationError
from tests.fakes.fixtures import FIXTURE_TOOL_CALL

from xiaowei_agent.contracts import (
    HydratedQuery,
    ReadonlyQueryBudget,
    ToolCall,
)

pytestmark = pytest.mark.security

_SQL = b"SELECT secret_column FROM t"
_SQL_HASH = hashlib.sha256(_SQL).hexdigest()


def _budget() -> ReadonlyQueryBudget:
    return ReadonlyQueryBudget(
        preview_max_rows=1000, preview_max_bytes=20_971_520, query_timeout_seconds=180
    )


def _hydrated(**updates: object) -> HydratedQuery:
    values: dict[str, object] = {
        "sql_ref": "ref-1",
        "sql_hash": _SQL_HASH,
        "sql_bytes": _SQL,
        "resource_id": "starrocks-test",
        "target_fingerprint": "f" * 64,
        "config_revision": "c" * 64,
        "budget": _budget(),
    }
    return HydratedQuery(**(values | updates))  # type: ignore[arg-type]


def test_hydrated_query_requires_bytes_matching_the_bound_hash() -> None:
    assert _hydrated().sql_bytes == _SQL
    with pytest.raises(ValueError):
        _hydrated(sql_bytes=b"SELECT 2")
    with pytest.raises(ValueError):
        _hydrated(sql_hash="0" * 63)
    with pytest.raises(TypeError):
        _hydrated(sql_bytes="SELECT secret_column FROM t")


def test_hydrated_query_never_prints_or_serialises_sql_bytes() -> None:
    hydrated = _hydrated()
    assert b"secret_column" not in repr(hydrated).encode()
    assert "secret_column" not in str(hydrated)
    with pytest.raises(TypeError):
        pickle.dumps(hydrated)
    # 不是跨边界契约：不能被 dump 进 Plan、TaskStore、trace 或 audit。
    assert not isinstance(hydrated, BaseModel)
    with pytest.raises(AttributeError):
        hydrated.sql_bytes = b"SELECT 3"  # type: ignore[misc]


@pytest.mark.parametrize(
    "value", [{"sql": "SELECT 1"}, ["SELECT 1"], b"SELECT 1", _SQL]
)
def test_tool_call_still_rejects_non_scalar_arguments(value: object) -> None:
    fields = FIXTURE_TOOL_CALL.model_dump()
    with pytest.raises(ValidationError):
        ToolCall(**(fields | {"typed_args": {"sql": value}}))
