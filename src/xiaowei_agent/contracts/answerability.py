"""Reflection 的唯一输出契约。

**这五个字段就是 Reflection 的全部权限**：证据是否充分、有哪些限制、缺了什么、
建议是否降级、建议是否需要用户补充。是否真的进入 ``indeterminate``、是否真的向
用户提问，由 Runtime/Runner 依据本结论确定性决定并写 TaskStore；Reflection 不设
终态、不写 TaskStore、不改计划（ARCHITECTURE §4.2）。

字段集之外的任何键都会被 ``extra="forbid"`` 拒绝，越权建议在契约层**无法被表达**
——这比"表达后再拒绝"更强，也更容易长期维持。
"""

from xiaowei_agent.contracts.base import Contract, StrictStr


class MissingItem(Contract):
    key: StrictStr
    reason_key: StrictStr


class AnswerabilityVerdict(Contract):
    sufficient: bool
    limitations: tuple[str, ...]
    missing: tuple[MissingItem, ...]
    downgrade_suggestion: bool
    needs_user_input: bool
