"""资产目录结果的纯证据构造器。"""

import datetime as dt
import ipaddress
from typing import Final

from xiaowei_agent.contracts import (
    EvidenceEnvelope,
    ExecutionPlan,
    ExternalSource,
    FrozenMap,
    JsonScalar,
    PlanStep,
    ResolvedTarget,
    ToolResult,
)
from xiaowei_agent.contracts.evidence import evidence_id
from xiaowei_agent.evidence.errors import EvidenceBuildError

_CAPABILITY_ID: Final[str] = "asset.inventory.lookup"
_CAPABILITY_VERSION: Final[str] = "1.0.0"
_OPERATION: Final[str] = "lookup_asset"
_FIELD_SET_ID: Final[str] = "asset.summary.v1"
_LOOKUP_LIMIT: Final[int] = 2
_TARGET_PROVIDER: Final[str] = "asset_inventory"
_TARGET_KIND: Final[str] = "asset"
_SELECTOR_VERSION: Final[str] = "asset.exact.v1"
_ALLOWED_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "test"})
_SELECTOR_TO_FACT: Final[dict[str, str]] = {
    "asset_id": "asset_id",
    "hostname": "hostname",
    "ip": "primary_ip",
}
_FACT_FIELDS: Final[tuple[str, ...]] = (
    "asset_id",
    "hostname",
    "primary_ip",
    "asset_type",
    "status",
    "owner_team",
    "service",
    "os_family",
    "environment_id",
)
_ARGUMENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"selector_kind", "selector_value", "field_set_id", "limit"}
)


def _nonempty_string(row: FrozenMap, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise EvidenceBuildError
    return value


def _validate_target_and_step(
    *, step: PlanStep, plan: ExecutionPlan, target: ResolvedTarget
) -> tuple[str, str, int]:
    if (
        plan.capability_id != _CAPABILITY_ID
        or plan.capability_version != _CAPABILITY_VERSION
        or step not in plan.steps
        or step.operation != _OPERATION
        or step.step_id != "s1"
        or set(step.typed_arguments) != _ARGUMENT_FIELDS
    ):
        raise EvidenceBuildError
    arguments = step.typed_arguments
    selector_kind = arguments.get("selector_kind")
    selector_value = arguments.get("selector_value")
    field_set_id = arguments.get("field_set_id")
    limit = arguments.get("limit")
    if (
        selector_kind not in _SELECTOR_TO_FACT
        or not isinstance(selector_value, str)
        or not selector_value
        or field_set_id != _FIELD_SET_ID
        or type(limit) is not int
        or limit != _LOOKUP_LIMIT
    ):
        raise EvidenceBuildError
    if (
        target.provider != _TARGET_PROVIDER
        or target.resource_kind != _TARGET_KIND
        or target.selector_version != _SELECTOR_VERSION
        or target.environment_id not in _ALLOWED_ENVIRONMENTS
        or len(target.resource_ids) != 1
    ):
        raise EvidenceBuildError
    target_kind, separator, target_value = target.resource_ids[0].partition(":")
    if (
        separator != ":"
        or target_kind != selector_kind
        or target_value != selector_value
    ):
        raise EvidenceBuildError
    return selector_kind, selector_value, limit


def _asset_fact(
    row: FrozenMap,
    *,
    target: ResolvedTarget,
    selector_kind: str,
    selector_value: str,
) -> dict[str, JsonScalar]:
    if not set(_FACT_FIELDS) <= set(row):
        raise EvidenceBuildError
    strings = {
        key: _nonempty_string(row, key) for key in _FACT_FIELDS
    }
    if strings["environment_id"] != target.environment_id:
        raise EvidenceBuildError
    selected_field = _SELECTOR_TO_FACT[selector_kind]
    if strings[selected_field] != selector_value:
        raise EvidenceBuildError
    hostname = strings["hostname"]
    if not hostname.isascii() or hostname != hostname.lower() or hostname.endswith("."):
        raise EvidenceBuildError
    try:
        canonical_ip = ipaddress.ip_address(strings["primary_ip"]).compressed
    except ValueError:
        raise EvidenceBuildError from None
    if strings["primary_ip"] != canonical_ip:
        raise EvidenceBuildError
    fact: dict[str, JsonScalar] = dict(strings)
    return fact


def build_asset_evidence(
    *,
    task_id: str,
    step: PlanStep,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    result: ToolResult,
    captured_at: dt.datetime,
) -> EvidenceEnvelope:
    """验证 scope/精确身份并只保留九个资产字段。"""
    selector_kind, selector_value, limit = _validate_target_and_step(
        step=step, plan=plan, target=target
    )
    if len(result.data_view) > limit:
        raise EvidenceBuildError
    facts = tuple(
        _asset_fact(
            row,
            target=target,
            selector_kind=selector_kind,
            selector_value=selector_value,
        )
        for row in result.data_view
    )
    return EvidenceEnvelope(
        evidence_id=evidence_id(task_id=task_id, step_id=step.step_id),
        capability_id=plan.capability_id,
        capability_version=plan.capability_version,
        facts=facts,
        source=result.source,
        source_kind=ExternalSource.TOOL,
        captured_at=captured_at,
        sampled=len(facts) == limit,
        limitations=(
            f"exact selector: {selector_kind}",
            f"environment scope: {target.environment_id}",
            f"asset row limit: {limit}",
            f"returned assets: {len(facts)}",
            "fields limited to evidence.asset.inventory.v1",
        ),
        redaction_ref=None,
    )
