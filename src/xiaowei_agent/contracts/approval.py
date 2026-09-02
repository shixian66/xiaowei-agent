"""审批绑定与准入凭证。

``ApprovalRequest`` 保存审批**时刻**的 plan_hash、target_fingerprint 与
policy_revision；恢复时用重算值比对，不匹配即拒绝（ARCHITECTURE §5.7）。
``policy_revision`` 虽已进入 plan_hash，仍显式保留，使"policy 变化不能静默让旧
审批继续生效"有一个可独立断言、可独立给出拒绝原因的位置（ADR-009 D3）。

``AdmissionCertificate`` 由 StepAdmission（M3）产出、ToolGateway 消费。它必须
**同时绑定步骤身份与调用内容**：只绑定 step_id/operation 的话，持合法凭证替换
``typed_args`` 就能执行另一个调用。``tool_call_hash`` 覆盖 ``ToolCall`` 的全部
字段，Gateway 重算比对（ADR-009 D4）。
"""

import datetime as _dt

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import ApprovalState, EffectClass
from xiaowei_agent.contracts.policy import PolicyDecision


class ApprovalRequest(Contract):
    task_id: StrictStr
    step_id: StrictStr
    plan_hash: StrictStr
    target_fingerprint: StrictStr
    policy_revision: StrictStr
    subject: StrictStr
    expires_at: _dt.datetime
    state: ApprovalState


class AdmissionCertificate(Contract):
    step_id: StrictStr
    operation: StrictStr
    effect_class: EffectClass
    policy_decision: PolicyDecision
    approval_ref: str | None
    plan_hash: StrictStr
    target_fingerprint: StrictStr
    tool_call_hash: StrictStr
