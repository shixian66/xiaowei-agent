"""M3 的 policy profile 实例与当前生效的 policy 快照。

profile 是**声明**，判定在 ``governance.policy``。分开的理由与 capability 声明
相同：允许面必须可以被直接读出与断言，而不是散落在判定函数的 ``if`` 里。
"""

from typing import Final

from xiaowei_agent.capabilities.specs import OP_COUNT, OP_LIST, POLICY_PROFILE
from xiaowei_agent.capabilities.target import KNOWN_ENVIRONMENT_IDS
from xiaowei_agent.contracts import (
    EffectClass,
    PolicyProfile,
    PolicySnapshot,
    RiskLevel,
)

POLICY_REVISION: Final[str] = "policy-2026-09-01"
"""当前生效的 policy revision。

revision 变化必须让旧审批与旧凭证失效（ARCHITECTURE §15），因此它是一个显式常量，
改动即意味着一次评审。
"""

MAX_READONLY_TIMEOUT_SECONDS: Final[float] = 30.0

SLOW_QUERY_READONLY_PROFILE: Final[PolicyProfile] = PolicyProfile(
    profile_id=POLICY_PROFILE,
    allowed_operations=(OP_LIST, OP_COUNT),
    # 只读闭环的允许分类只有 READ 一项：即使某个 operation 在快照里被改成写，
    # 策略层也不放行——两层独立成立，不互相替代。
    allowed_effect_classes=(EffectClass.READ,),
    allowed_environment_ids=KNOWN_ENVIRONMENT_IDS,
    risk=RiskLevel.LOW,
    max_timeout_seconds=MAX_READONLY_TIMEOUT_SECONDS,
)

ACTIVE_POLICY_SNAPSHOT: Final[PolicySnapshot] = PolicySnapshot(
    policy_revision=POLICY_REVISION, profiles=(POLICY_PROFILE,)
)
"""当前注册的 profile 集合。

**合成写 profile 不在其中**：它只存在于测试夹具，因此任何试图以生产快照准入一个
写步骤的路径都会在 ``verify_policy_revision`` 处失败。
"""
