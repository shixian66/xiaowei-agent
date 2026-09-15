"""审批绑定与准入凭证。

``ApprovalRequest`` 保存审批**时刻**的 plan_hash、target_fingerprint 与
policy_revision；恢复时用重算值比对，不匹配即拒绝（ARCHITECTURE §5.7）。
``policy_revision`` 虽已进入 plan_hash，仍显式保留，使"policy 变化不能静默让旧
审批继续生效"有一个可独立断言、可独立给出拒绝原因的位置（ADR-009 D3）。

``AdmissionCertificate`` 由 StepAdmission（M3）产出、ToolGateway 消费。它必须
**同时绑定步骤身份与调用内容**：只绑定 step_id/operation 的话，持合法凭证替换
``typed_args`` 就能执行另一个调用。``tool_call_hash`` 覆盖 ``ToolCall`` 的全部
字段，Gateway 重算比对（ADR-009 D4）。

凭证的构造受**签发凭据**保护，与 ``ToolResult`` 同一模式。此前 Gateway 的
**输出**有 witness 保护、而它信任的**授权输入**却是普通 public DTO——任何调用
方都能构造一张 ``allow=True`` 的凭证直接调用 adapter，"未经准入即调用工具"因此
仍然可表达。授权凭据比结果更需要不可伪造：伪造结果只污染一次回答，伪造凭证
绕过的是整条策略链。

**诚实边界**：与 ``ToolResult`` 相同，Python 无法提供语言级私有性，
``object.__setattr__`` 与重定义模块两条路径无法封堵，属已知残余风险。
"""

from collections.abc import Mapping
from typing import Any, Final, Never

from pydantic import model_validator

from xiaowei_agent.contracts.base import AwareDatetime, Contract, Sha256Hex, StrictStr
from xiaowei_agent.contracts.enums import ApprovalState, EffectClass
from xiaowei_agent.contracts.ids import TaskId
from xiaowei_agent.contracts.policy import PolicyDecision


class ApprovalRequest(Contract):
    task_id: TaskId
    step_id: StrictStr
    plan_hash: Sha256Hex
    target_fingerprint: Sha256Hex
    policy_revision: StrictStr
    subject: StrictStr
    expires_at: AwareDatetime
    state: ApprovalState


_ADMISSION_WITNESS: Final[object] = object()
_ADMISSION_WITNESS_KEY: Final[str] = "__admission_witness__"


class AdmissionCertificate(Contract):
    step_id: StrictStr
    operation: StrictStr
    effect_class: EffectClass
    policy_decision: PolicyDecision
    approval_ref: StrictStr | None
    plan_hash: Sha256Hex
    target_fingerprint: Sha256Hex
    tool_call_hash: Sha256Hex

    @model_validator(mode="before")
    @classmethod
    def _require_admission_witness(cls, data: object) -> object:
        if not isinstance(data, dict):
            raise ValueError("AdmissionCertificate 只能由 StepAdmission 签发")
        if data.pop(_ADMISSION_WITNESS_KEY, None) is not _ADMISSION_WITNESS:
            raise ValueError("AdmissionCertificate 只能由 StepAdmission 签发")
        return data

    @classmethod
    def model_construct(cls, *args: object, **kwargs: object) -> Never:
        # 基类已统一封死；此处覆盖只为给出更准确的错误信息。
        raise NotImplementedError("AdmissionCertificate 只能由 StepAdmission 签发")

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Never:
        # 比基类更严：基类的 model_copy 会重新校验，但重新校验出的凭证不再由
        # StepAdmission 签发。允许复制等于允许"拿一张合法凭证改 operation"。
        raise NotImplementedError("AdmissionCertificate 不可复制；请重新签发")
