"""可答性判断。**只消费已生成的结构化证据，不拥有任何执行权**（ARCHITECTURE §4.2）。"""

from xiaowei_agent.reflection.answerability import assess
from xiaowei_agent.reflection.status import terminal_status_for

__all__ = ["assess", "terminal_status_for"]
