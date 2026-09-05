"""Prometheus fake 只按完整 query_range 调用精确回放扁平点。"""

import pytest

from xiaowei_agent.contracts import AdapterStatus, RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.prometheus_fake import (
    PrometheusRecordingAdapter,
    PrometheusRecordingNotFoundError,
)
from xiaowei_agent.tools.prometheus_recording import default_prometheus_recording

CONTEXT = RequestContext(
    tenant_id="dev-local",
    actor="alice",
    environment_id="dev",
    trace_id="0" * 32,
    policy_revision="policy-2026-09-01",
)
OPERATION = "query_metric_range"
PROMQL = (
    '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",'
    'instance="node-1.example.com:9100"}[5m])))'
)
ARGS = {
    "promql": PROMQL,
    "promql_template_id": "prometheus.alert.host_cpu_percent.v1",
    "alert_name": "HostHighCpu",
    "instance": "node-1.example.com:9100",
    "window_start": "2026-09-05T11:30:00+00:00",
    "window_end": "2026-09-05T12:00:00+00:00",
    "step_seconds": 60,
    "max_series": 5,
    "max_points_per_series": 361,
}
RESPONSE = AdapterResponse(
    status=AdapterStatus.OK,
    payload=(
        {
            "series_key": "instance=node-1.example.com:9100",
            "metric_name": "node_cpu_percent",
            "instance": "node-1.example.com:9100",
            "timestamp_ms": 1_788_607_800_000,
            "value": 82.0,
        },
    ),
    source="prometheus-recording",
    error=None,
    elapsed_ms=1,
)
KEY = (
    "dev-local",
    "dev",
    OPERATION,
    PROMQL,
    "prometheus.alert.host_cpu_percent.v1",
    "HostHighCpu",
    "node-1.example.com:9100",
    "2026-09-05T11:30:00+00:00",
    "2026-09-05T12:00:00+00:00",
    60,
    5,
    361,
)


def _call(**overrides: object) -> ToolCall:
    values: dict[str, object] = {
        "gateway": "prometheus",
        "operation": OPERATION,
        "step_id": "s2",
        "typed_args": ARGS,
        "timeout_seconds": 30.0,
        "idempotency_key": "fixed:s2",
    }
    return ToolCall(**(values | overrides))


@pytest.mark.asyncio
async def test_exact_metric_call_returns_the_recording_and_tracks_the_call() -> None:
    adapter = PrometheusRecordingAdapter({KEY: RESPONSE})
    assert await adapter.execute(_call(), context=CONTEXT) == RESPONSE
    assert adapter.call_count == 1
    assert adapter.calls == [_call()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_update", "context"),
    [
        ({"operation": "unknown"}, CONTEXT),
        ({"typed_args": ARGS | {"promql": PROMQL + " or vector(1)"}}, CONTEXT),
        ({"typed_args": ARGS | {"promql_template_id": "unknown"}}, CONTEXT),
        ({"typed_args": ARGS | {"max_series": 6}}, CONTEXT),
        ({"typed_args": ARGS | {"raw_labels": "x"}}, CONTEXT),
        ({}, CONTEXT.model_copy(update={"tenant_id": "other"})),
        ({}, CONTEXT.model_copy(update={"environment_id": "test"})),
    ],
)
async def test_tampered_query_budget_or_scope_never_falls_back(
    call_update: dict[str, object], context: RequestContext
) -> None:
    adapter = PrometheusRecordingAdapter({KEY: RESPONSE})
    with pytest.raises(PrometheusRecordingNotFoundError):
        await adapter.execute(_call(**call_update), context=context)


def test_default_metric_rows_are_flat_and_field_limited() -> None:
    recording = default_prometheus_recording(
        operation=OPERATION,
        promql=PROMQL,
        template_id="prometheus.alert.host_cpu_percent.v1",
    )
    assert recording
    allowed = {"series_key", "metric_name", "instance", "timestamp_ms", "value"}
    for response in recording.values():
        assert response.status is AdapterStatus.OK
        for row in response.payload:
            assert set(row) == allowed
            assert all(not isinstance(value, dict | list) for value in row.values())


def test_default_metric_recording_preserves_the_injected_identity_and_values() -> None:
    recording = default_prometheus_recording(
        operation=OPERATION,
        promql="up{instance=\"node-2.example.com:9100\"}",
        template_id="prometheus.alert.instance_up.v1",
        alert_name="InstanceDown",
        instance="node-2.example.com:9100",
        metric_name="up",
        values=(0.0, 1.0),
    )
    key, response = next(iter(recording.items()))
    assert key[5:7] == ("InstanceDown", "node-2.example.com:9100")
    assert [(row["metric_name"], row["value"]) for row in response.payload] == [
        ("up", 0.0),
        ("up", 1.0),
    ]


@pytest.mark.parametrize(
    ("window_start", "window_end", "values"),
    [
        ("2026-09-05T11:30:00", "2026-09-05T12:00:00+00:00", (0.0, 1.0)),
        (
            "2026-09-05T11:30:00+00:00",
            "2026-09-05T11:29:00+00:00",
            (0.0, 1.0),
        ),
        (
            "2026-09-05T11:30:00+00:00",
            "2026-09-05T12:01:00+00:00",
            (0.0, 1.0),
        ),
        (
            "2026-09-05T11:30:00+00:00",
            "2026-09-05T12:00:00+00:00",
            (0.0,),
        ),
    ],
)
def test_default_metric_recording_rejects_ambiguous_or_invalid_ranges(
    window_start: str,
    window_end: str,
    values: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError):
        default_prometheus_recording(
            operation=OPERATION,
            promql=PROMQL,
            template_id="prometheus.alert.host_cpu_percent.v1",
            window_start=window_start,
            window_end=window_end,
            values=values,  # type: ignore[arg-type]
        )


def test_empty_prometheus_recording_is_rejected() -> None:
    with pytest.raises(ValueError):
        PrometheusRecordingAdapter({})
