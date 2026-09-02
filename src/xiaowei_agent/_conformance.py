"""Protocol 一致性锚点。

**为什么必须放在 ``src`` 而不是 ``tests``**：验收命令是 ``mypy src``，放在
``tests/`` 下的带类型标注赋值根本不会被检查；而 pytest 运行它只是在运行时断言
实例存在，证明不了 structural typing 没有漂移。Protocol 只在**有赋值发生时**才被
静态检查，因此需要一处显式赋值作为锚。

全部内容位于 ``TYPE_CHECKING`` 块内，运行时零开销、零导入副作用——特别是不会把
fake 实现拉进生产导入链（``tests/security/test_fake_isolation.py`` 断言这一点，
故此处的 fake 导入同样只在类型检查期存在）。

**冻结但无实现锚点的 Protocol**：``CapabilityRegistry``、``CapabilityResolver``、
``TraceSink`` 在 M2 没有任何实现（分别属 M3 与采集后端），因此只冻结形状、不设锚点。
M3 落地实现时必须在此补上对应赋值。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅供 mypy 检查结构兼容性
    from xiaowei_agent.contracts import TaskStatus
    from xiaowei_agent.persistence.fake import InMemoryTaskStore
    from xiaowei_agent.persistence.store import Clock, TaskStore
    from xiaowei_agent.runners.fake import ScriptedRunner
    from xiaowei_agent.runners.runner import WorkflowRunner
    from xiaowei_agent.tools.adapter import AdapterResponse, ToolAdapter
    from xiaowei_agent.tools.fake import RecordingToolAdapter
    from xiaowei_agent.tools.gateway import DeterministicToolGateway, ToolGateway

    def _anchor(
        clock: "Clock",
        responses: tuple["AdapterResponse", ...],
    ) -> None:
        adapter: ToolAdapter = RecordingToolAdapter(responses=responses)
        gateway: ToolGateway = DeterministicToolGateway(adapters={"starrocks": adapter})
        store: TaskStore = InMemoryTaskStore(clock=clock)
        runner: WorkflowRunner = ScriptedRunner(
            store, outcome_status=TaskStatus.SUCCEEDED
        )
        _ = (gateway, runner)
