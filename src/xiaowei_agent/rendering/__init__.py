"""回答投影。渠道只选择展示方式，不重算业务结果。"""

from xiaowei_agent.rendering.pending import render_pending
from xiaowei_agent.rendering.slow_query import render

__all__ = ["render", "render_pending"]
