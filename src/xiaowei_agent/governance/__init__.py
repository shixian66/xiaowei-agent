"""确定性治理校验。M2 只有纯函数，没有 ApprovalGate（属 M3）。"""

from xiaowei_agent.governance.binding import (
    BindingError,
    verify_approval_binding,
    verify_policy_revision,
)

__all__ = ["BindingError", "verify_approval_binding", "verify_policy_revision"]
