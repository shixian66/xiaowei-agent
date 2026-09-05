"""所有证据构造器共用的结构化失败。"""


class EvidenceBuildError(RuntimeError):
    """上游标量行不满足能力证据契约；消息不回显行内容。"""

    def __init__(self) -> None:
        super().__init__("tool rows do not satisfy the evidence contract")
