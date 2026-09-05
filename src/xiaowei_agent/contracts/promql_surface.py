"""一个 capability 允许使用的 PromQL 模板与查询预算。"""

from typing import Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import Contract, StrictInt, StrictStr


class PromqlSurface(Contract):
    """固定模板 PromQL 的闭集声明；不携带任意表达式。"""

    surface_id: StrictStr
    allowed_template_ids: tuple[StrictStr, ...]
    max_window_minutes: StrictInt = Field(gt=0)
    max_series: StrictInt = Field(gt=0)
    max_points_per_series: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _templates_are_nonempty_and_unique(self) -> Self:
        if not self.allowed_template_ids:
            raise ValueError("allowed_template_ids must not be empty")
        if len(set(self.allowed_template_ids)) != len(self.allowed_template_ids):
            raise ValueError("allowed_template_ids must be unique")
        return self
