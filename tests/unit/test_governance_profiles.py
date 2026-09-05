"""生产 policy 快照的版本与允许面必须共同变化。"""

from xiaowei_agent.governance.profiles import (
    ACTIVE_POLICY_SNAPSHOT,
    POLICY_REVISION,
)


def test_policy_revision_and_ordered_profile_set_are_one_golden() -> None:
    """新增允许 profile 不能静默复用旧审批绑定的 revision。"""
    assert (
        ACTIVE_POLICY_SNAPSHOT.policy_revision,
        ACTIVE_POLICY_SNAPSHOT.profiles,
    ) == (
        "policy-2026-09-05.2",
        (
            "readonly.starrocks.slow_query.v1",
            "readonly.prometheus.alert.evidence.v1",
            "readonly.asset.inventory.lookup.v1",
        ),
    )
    assert POLICY_REVISION == ACTIVE_POLICY_SNAPSHOT.policy_revision
