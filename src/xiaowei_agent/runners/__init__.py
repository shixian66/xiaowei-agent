"""工作流执行。M2 只有 Protocol 与测试替身；真实 Runner 属 M3。

**不从本入口导出 fake 实现**：``runners.fake`` 必须显式 import。
"""

from xiaowei_agent.runners.runner import WorkflowRunner

__all__ = ["WorkflowRunner"]
