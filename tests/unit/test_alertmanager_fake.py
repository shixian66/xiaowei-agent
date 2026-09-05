"""Alertmanager fake 只按 scope 与完整闭集参数精确回放。"""

import pytest

from xiaowei_agent.contracts import AdapterStatus, RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.alertmanager_fake import (
    AlertmanagerRecordingAdapter,
    AlertmanagerRecordingNotFoundError,
)
from xiaowei_agent.tools.alertmanager_recording import (
    default_alertmanager_recording,
)

CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)
OPERATION = "get_active_alerts"
ARGS = {
    "alert_name": "HostHighCpu",
    "instance": "node-1.example.com:9100",
    "limit": 5,
}
RESPONSE = AdapterResponse(
    status=AdapterStatus.OK,
    payload=({"fingerprint": "fp-001", "state": "firing"},),
    source="alertmanager-recording",
    error=None,
    elapsed_ms=1,
)
KEY = (
    "dev-local",
    "dev",
    OPERATION,
    "HostHighCpu",
    "node-1.example.com:9100",
    None,
    5,
)


def _call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "gateway": "alertmanager",
        "operation": OPERATION,
        "step_id": "s1",
        "typed_args": ARGS,
        "timeout_seconds": 30.0,
        "idempotency_key": "fixed:s1",
    }
    return ToolCall(**(values | overrides))


@pytest.mark.asyncio
async def test_exact_alert_call_returns_the_recording_and_tracks_the_call() -> None:
    adapter = AlertmanagerRecordingAdapter({KEY: RESPONSE})
    assert await adapter.execute(_call(), context=CONTEXT) == RESPONSE
    assert adapter.call_count == 1
    assert adapter.calls == [_call()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_update", "context"),
    [
        ({"operation": "unknown"}, CONTEXT),
        ({"typed_args": ARGS | {"limit": 6}}, CONTEXT),
        ({"typed_args": ARGS | {"raw_labels": "x"}}, CONTEXT),
        ({"typed_args": ARGS | {"fingerprint": "fp-999"}}, CONTEXT),
        ({}, CONTEXT.model_copy(update={"tenant_id": "other"})),
        ({}, CONTEXT.model_copy(update={"environment_id": "test"})),
    ],
)
async def test_unknown_operation_arguments_or_scope_never_fall_back(
    call_update: dict[str, object], context: RequestContext
) -> None:
    adapter = AlertmanagerRecordingAdapter({KEY: RESPONSE})
    with pytest.raises(AlertmanagerRecordingNotFoundError):
        await adapter.execute(_call(**call_update), context=context)


def test_default_alert_rows_are_flat_and_field_limited() -> None:
    recording = default_alertmanager_recording(operation=OPERATION)
    assert recording
    allowed = {
        "fingerprint",
        "state",
        "alert_name",
        "instance",
        "severity",
        "starts_at",
        "ends_at",
        "silenced",
        "inhibited",
    }
    for response in recording.values():
        assert response.status is AdapterStatus.OK
        for row in response.payload:
            assert set(row) <= allowed
            assert all(not isinstance(value, dict | list) for value in row.values())


def test_empty_alert_recording_is_rejected() -> None:
    with pytest.raises(ValueError):
        AlertmanagerRecordingAdapter({})
