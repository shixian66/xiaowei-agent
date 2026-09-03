"""Protocol 一致性锚点。

**为什么必须放在 ``src`` 而不是 ``tests``**：验收命令是 ``mypy src``，放在
``tests/`` 下的带类型标注赋值根本不会被检查；而 pytest 运行它只是在运行时断言
实例存在，证明不了 structural typing 没有漂移。Protocol 只在**有赋值发生时**才被
静态检查，因此需要一处显式赋值作为锚。

全部内容位于 ``TYPE_CHECKING`` 块内，运行时零开销、零导入副作用——特别是不会把
fake 实现拉进生产导入链（``tests/security/test_fake_isolation.py`` 断言这一点，
故此处的 fake 导入同样只在类型检查期存在）。

M3 补齐了五个此前只有形状、没有实现的 Protocol 锚点：``CapabilityRegistry``、
``CapabilityResolver``、``TraceSink``、``PlanStore``、``EvidenceLedger``。

``WorkflowRunner`` 锚定**两个**实现：fake 的 ``ScriptedRunner`` 与真实的
``DeterministicStepRunner``。只锚 fake 是不够的——真实 Runner 才是生产调用路径，
它一旦偏离契约，调用方要么改签名要么绕过 Protocol，而后者不会有任何静态检查失败。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅供 mypy 检查结构兼容性
    from xiaowei_agent.capabilities.registry import StaticCapabilityRegistry
    from xiaowei_agent.capabilities.resolver import (
        CapabilityRegistry,
        CapabilityResolver,
    )
    from xiaowei_agent.capabilities.resolver_impl import DeterministicCapabilityResolver
    from xiaowei_agent.contracts import TaskStatus
    from xiaowei_agent.observability.log_sink import StructuredLogTraceSink
    from xiaowei_agent.observability.sink import TraceSink
    from xiaowei_agent.persistence.evidence import EvidenceLedger, InMemoryEvidenceLedger
    from xiaowei_agent.persistence.fake import InMemoryTaskStore
    from xiaowei_agent.persistence.plans import InMemoryPlanStore, PlanStore
    from xiaowei_agent.persistence.store import Clock, TaskStore
    from xiaowei_agent.runners.deterministic import DeterministicStepRunner
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
        registry: CapabilityRegistry = StaticCapabilityRegistry()
        resolver: CapabilityResolver = DeterministicCapabilityResolver()
        sink: TraceSink = StructuredLogTraceSink()
        plans: PlanStore = InMemoryPlanStore()
        ledger: EvidenceLedger = InMemoryEvidenceLedger()
        _ = (gateway, runner, registry, resolver, sink, plans, ledger)

    def _real_runner_anchor(step_runner: "DeterministicStepRunner") -> None:
        """真实 Runner 必须**就是**一个 ``WorkflowRunner``。

        取参数而不在此构造：``DeterministicStepRunner`` 需要八个协作者，构造它会把
        一堆无关装配拖进锚点文件；而结构兼容性只需要一次赋值就能被 mypy 检查。
        """
        anchored: WorkflowRunner = step_runner
        _ = anchored
