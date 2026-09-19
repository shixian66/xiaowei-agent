"""Typed capability planners must not consume untrusted model slots directly."""

import inspect

import pytest

from xiaowei_agent.application.capability_input import CapabilityPlanner

pytestmark = pytest.mark.security


def test_typed_capability_planner_receives_params_not_intentdraft_or_mapping() -> None:
    signature = inspect.signature(CapabilityPlanner.__call__)
    annotations = {
        name: str(parameter.annotation)
        for name, parameter in signature.parameters.items()
    }

    assert "params" in signature.parameters
    assert "draft" not in signature.parameters
    assert "IntentDraft" not in repr(annotations)
    assert "Mapping" not in repr(annotations)
