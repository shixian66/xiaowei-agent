"""所有证据构造器共用的结构化失败与 metadata 边界。"""

from typing import Final

from xiaowei_agent.contracts import ToolResult

_TARGET_METADATA_PREFIXES: Final[tuple[str, ...]] = (
    "target_fingerprint=",
    "config_revision=",
    "physical_identity_ref=",
    "driver_version=",
    "preflight=",
)


class EvidenceBuildError(RuntimeError):
    """上游标量行不满足能力证据契约；消息不回显行内容。"""

    def __init__(self) -> None:
        super().__init__("tool rows do not satisfy the evidence contract")


def reject_unapproved_target_metadata(result: ToolResult) -> None:
    """拒绝只有 target-bound Evidence policy 才能消费的保留 metadata。"""
    if any(
        limitation.startswith(_TARGET_METADATA_PREFIXES)
        for limitation in result.limitations
    ):
        raise EvidenceBuildError
