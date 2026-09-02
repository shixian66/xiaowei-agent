"""外部系统访问。领域层只依赖 ToolGateway Protocol，不持有任何第三方客户端。

**不从本入口导出 fake 实现**：``tools.fake`` 必须显式 import，且由
``tests/security/test_fake_isolation.py`` 断言没有生产模块导入它。
"""

from xiaowei_agent.tools.adapter import AdapterResponse, ToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway, ToolGateway

__all__ = ["AdapterResponse", "DeterministicToolGateway", "ToolAdapter", "ToolGateway"]
