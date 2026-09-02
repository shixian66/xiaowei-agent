"""策略判定与策略快照。

``PolicySnapshot`` 是"当前生效的 policy revision 与允许的 profile 集合"，
使"未知 policy revision"成为可用纯函数验收的检查。
"""

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import RiskLevel


class PolicyDecision(Contract):
    allow: bool
    reason_code: StrictStr
    risk: RiskLevel
    policy_revision: StrictStr
    obligations: tuple[StrictStr, ...]


class PolicySnapshot(Contract):
    policy_revision: StrictStr
    profiles: tuple[StrictStr, ...]
