"""所有 capability 共用的 Answerability 到终态映射。"""

from xiaowei_agent.contracts import AnswerabilityVerdict, TaskStatus


def terminal_status_for(verdict: AnswerabilityVerdict) -> TaskStatus:
    """把建议性结论确定性映射成终态。"""
    if verdict.sufficient and not verdict.downgrade_suggestion:
        return TaskStatus.SUCCEEDED
    return TaskStatus.INDETERMINATE
