# 小维 Agent 2.0 目标架构

> 状态：Target V1 / 架构基线草案。本文描述从 0 开始建设的稳定目标，不声称当前代码、依赖、容器或线上环境已经存在。实施进度与证据只看 [当前状态](AGENT_HANDOFF.md#current-status)。

## 1. 定位与设计结论

小维 2.0 是一个 Policy-Governed Workflow Agent，而不是把聊天模型包在一层工具调用外的 ReAct Bot。它的价值是把自然语言入口、确定性计划、权限与审批、可恢复执行、外部证据和可审计回答组合成一条长期可维护的闭环。

核心结论：

1. 采用模块化单体作为第一种部署形态，控制面与数据面在代码和数据契约上分离。
2. 采用明确的 `Runtime`、`WorkflowRunner`、`ToolGateway`、`TaskStore` Protocol；实现可以替换，契约不随框架变化。
3. 默认先实现确定性的 `DeterministicStepRunner`，将 LangGraph 作为可选的 `LangGraphRunner` adapter。是否启用由多步、暂停、恢复、重试样本的评测结果决定，而不是由框架偏好决定。
4. 模型只负责结构化理解、解释和建议；最终 capability、目标、计划、策略、SQL、审批和执行全部由确定性组件控制。
5. `CapabilityResolver` 是唯一候选生成真源；`route_shadow` 只能记录同一份 `CandidateSet` 的对比信息。
6. DB/资产域允许受限 DSL 型 capability，DSL 只表达资源语义，不能表达任意代码、任意 SQL 或执行绕过。
7. PostgreSQL 是任务、审批和审计事实的第一存储；checkpoint、缓存和向量索引都不能替代它。
8. 智能交互入口的目标边界由 [ADR-017](docs/adr/ADR-017-intelligent-interaction-and-clarification.md) 冻结：
   I1 在 Resolver 前增加 `InteractionArtifact`、`DeterministicInteractionRouter`、终态澄清、
   `SlotVerifier`、`ReadClass` 与 `ExecutionDisclosure`，但不授予模型执行权。

## 2. 目标与非目标

### 2.1 目标

- 多入口共享一套业务语义和安全执行链。
- 新增资源域时主要新增 capability 声明、确定性 planner/compiler、adapter、证据 formatter 和 eval。
- 支持长任务、人工审批、暂停恢复、并发租约、崩溃恢复和不可确认结果。
- 每个回答都能追溯到请求、计划、工具调用、证据和最终 outcome。
- 对模型错误、提示注入、外部文本污染、目标漂移和重复执行采取 fail-closed 策略。
- 先通过离线和 fake adapter 验证，再逐步接入真实基础设施。

### 2.2 非目标

第一阶段不承诺：

- 任意自然语言生成任意生产 SQL 或任意运维命令。
- 模型自主选择工具、目标或审批人。
- 多 Agent 协作、自由 ReAct 循环或模型驱动的无限规划。Multi-Agent 的延期与准入条件见 §12.1。
- 一开始就拆成微服务、引入 Redis/Kafka/向量数据库或复杂工作流平台。
- 通过能力总表或关键词堆积来代替能力声明与确定性解析。
- 在没有真实生命周期证据时宣称“达到 Codex/Claude 的智能度”。

## 3. 分层架构

```text
                           Control Plane
┌─────────────────────────────────────────────────────────────┐
│ CapabilitySpec / DSL │ Policy │ Approval │ Config │ Eval    │
│ Registry              │ Rules  │ Subjects │ Versions│ Audit  │
└──────────────────────────────┬──────────────────────────────┘
                               │ declarations / decisions
用户 / API / Web / 飞书 / CLI   │
          │                    ▼
          ▼             ┌───────────────┐
   ┌──────────────┐     │ Agent Gateway │ auth / tenant / trace
   │ channel thin │────▶└──────┬────────┘
   └──────────────┘            ▼
                       ┌────────────────┐
                       │ XiaoweiRuntime │ application facade
                       │ Context        │
                       │ InteractionArtifact / IntentDraft
                       │ Resolver       │
                       │ SlotVerifier   │
                       │ PlanCompiler   │
                       └───────┬────────┘
                               ▼
                    PlanStore → ExecutionDisclosure
                               │
                               ▼
                       ┌────────────────┐
                       │ WorkflowRunner │ durable lifecycle host
                       │ step admission │
                       │ pause / resume │
                       └───────┬────────┘
                               ▼
                  Policy → SQLGuard → ApprovalGate*
                               │
                               ▼
                         ┌─────────────┐
                         │ ToolGateway │ only tool entry
                         └──────┬──────┘
                                ▼
                       MySQL / StarRocks /
                  Prometheus / Kafka / K8s / ...

                           Data Plane
┌─────────────────────────────────────────────────────────────┐
│ TaskStore │ Evidence │ Memory │ Readback │ Reflection │ Logs │
└─────────────────────────────────────────────────────────────┘

* ApprovalGate 是 Runtime 的单一组件，由 Runner 在具体副作用步骤边界调用。
```

控制面描述“允许什么、谁可以做、如何评估”；数据面承载请求、计划、工具调用、证据和任务状态。两者通过版本化的 DTO 和 policy revision 连接，不能通过共享全局变量或隐式字典连接。

## 4. 一次请求的真实调用链

```text
RequestEnvelope
  → AgentGateway
  → TaskStore.submit
  → Worker.begin_task_attempt
  → Worker heartbeat（覆盖本次 execute_task 与 retry scheduling）
       → XiaoweiRuntime
            → load-or-create AcceptedInteractionArtifact
            → DeterministicInteractionRouter
                 → conversation: deterministic capability-catalog response（I2，已归档）
                 → knowledge_lookup / log_analysis: REJECTED
                 → unclear route: ClarificationRecord → CLARIFICATION_REQUIRED（I1 后续切片）
                 → capability_request + proceed: continue
            → CapabilityResolver
            → SlotVerifier（I1 目标：可信槽位升级）
                 → incomplete: ClarificationRecord → CLARIFICATION_REQUIRED
                 → invalid: REJECTED
                 → ready: capability-specific Params
            → PlanCompiler
            → PlanStore.save / load
            → ExecutionDisclosure audit barrier（I1 目标）
            → WorkflowRunner
                 → StepAdmission
                      → ToolPolicy
                      → SQLGuard (需要 SQL 时)
                      → ApprovalGate (副作用步骤才调用)
                 → ToolGateway
                 → Readback (需要时)
            → EvidenceBuilder
            → Reflection / Answerability
            → optional SlowQueryAdvisoryPort（仅获批脱敏投影）
            → insert-once advisory（有合法结果时）
            → 现有 TaskStore 终态提交
  → TaskOutcome
  → TaskView/handle terminal read
  → RenderPayload (exactly once)
```

### 4.1 模型边界

当前 I1-A 源码事实是：只有获授权的 durable Runtime path 可以调用
`InteractionClassifierPort`；旧 `IntentInterpreter` 只作为规则 fallback，不拥有 provider access。
Provider output passes strict interaction schema and local semantic validation, then the port returns
an `InteractionModelResult` together with trusted nullable `ModelUsage`; usage is the locked SDK's
normalized metadata narrowed again to local bounds, not part of the model-generated schema.
The draft may then become an insert-once `AcceptedInteractionArtifact`; it means only that this task
accepted an untrusted interaction draft, never that the model gained execution authority. 任务重试必须
先读回并复用已接受 artifact。若 provider 已收到请求但本地还没保存就崩溃，恢复后允许重复一次模型调用；
模型无工具和执行副作用，因此当前仍接受这一 at-least-once 取舍，不为此建设调用 reservation 平台。
Router 只能从 artifact 产出 proceed/clarify/refuse 裁决；只有 proceed 的 capability draft 进入 Resolver。
Resolver 仍须重新从当前注册能力、租户上下文、环境目录和确定性规则得到最终候选。模型响应里的
confidence 复用现有有限 float `[0,1]` 语义（JSON 数字 `0/1` 可归一，bool/字符串/NaN/Inf 拒绝），
但不用于绕过 Router/Resolver、直接选 capability 或扩大参数。

证据解释只能通过独立的 `SlowQueryAdvisoryPort`。它只接收 capability 专属、字段闭集、限量且脱敏的
投影，输出只能进入无证据引用的建议展示槽。Runtime 可以用显式 capability binding 组合 planner、
执行元数据、Evidence builder、Answerability、advisory projector 与 renderer，但 binding 只能精确
消费 Resolver 的候选，不能再次生成、评分或回退候选。不建通用 `generate()` 或 provider registry，
首个真实模型的固定边界见 [ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md)。

模型允许：

- 提取用户意图和缺失槽位。
- 对已取得的脱敏证据做解释、摘要和建议。
- 生成面向人的澄清问题。

模型不允许：

- 直接调用工具或 SDK function calling。
- 直接决定 capability、资源 ID、环境、SQL、命令、审批结果或写入动作。
- 修改 policy、system prompt、TaskStore 状态或安全配置。
- 把外部文本中的指令当成系统指令。
- 使用 provider chat/session 作为任务事实或上下文真源。
- 生成或改写事实引用、任务状态、`next_steps` 或确定性结论。

### 4.2 Reflection 的位置

Reflection 不是第二条执行链，也不是模型拥有的“自我授权”。**它只消费已生成的结构化 Evidence，不拥有任何执行权。**

**Reflection 允许**产生一个结构化结论，内容限于：

- 现有证据是否足以回答；
- 存在哪些限制、样本性和时效性说明；
- 缺失了哪些信息项；
- **建议**是否应降级为 `indeterminate`；
- **建议**是否需要请求用户补充信息。

上述结论是**建议性输入**：是否真的进入 `indeterminate`、是否真的向用户提问，由 Runtime 与 Runner 依据该结构化结论确定性决定并写入 TaskStore。**Reflection 本身不设置终态，也不写 TaskStore。**

**Reflection 不允许**：

- 新增、删除或修改 `ExecutionPlan` 的任何步骤；
- 选择工具、切换 adapter 或决定调用顺序；
- 扩大目标、放宽选择器或提高权限；
- 生成或改写 SQL；
- 触发 `ToolGateway` 或任何 adapter；
- 修改 TaskStore 中已成为事实的状态、证据或审批记录。

缺槽识别与初始证据需求由 `CapabilityResolver` 和 `PlanCompiler` 处理，**不由 Reflection 事后补救**。

如果一个场景确实需要按条件追加取数，唯一合法形式是：`PlanCompiler` 在编译期就把该步骤作为**预编译、预算内的可选只读分支**写入 `ExecutionPlan`，由 `WorkflowRunner` 依据确定性条件决定是否执行，并照常经过 `StepAdmission`。**Reflection 不得动态扩计划**，也不得把可选分支的执行条件变成模型判断。

每次请求同时受三重预算限制：最大工作流步骤数、最大工具调用数、最大模型/token 预算。超出预算必须产生结构化结果，不得靠循环继续尝试。

### 4.3 决策权责矩阵（Degrees of autonomy）

本表只把已有边界显式化，便于速查；**它不授予任何新权限，也不放宽 [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)**。本项目**不引入泛化的 `autonomy_level` 运行字段**——自治程度由下列固定分工表达，不由一个可调参数表达。

| 决策对象 | 由谁决定 | LLM 的角色 |
| --- | --- | --- |
| 意图提取、证据解释、澄清问题措辞 | 确定性组件决定是否采纳；不采纳即丢弃 | **可建议** |
| capability、目标、参数、步骤、SQL、工具调用顺序 | `CapabilityResolver` + `PlanCompiler` 确定性决定 | 无 |
| Policy 判定、effect 分类、审批有效性 | `ToolPolicy`、`CapabilitySpec` 派生、`ApprovalGate` 等确定性治理组件 | 无 |
| 证据可答性结论（建议） | Reflection 基于结构化 Evidence 产出；**不改变计划、不设置终态** | 可参与结论措辞 |
| 终态与 `indeterminate` 判定 | Runtime / Runner 依据 Reflection 的结构化结论确定性决定，并由 TaskStore 保护 | 无 |
| 工具执行 | `WorkflowRunner` 经 `StepAdmission` 与 `ToolGateway` 驱动 | 无 |
| 测试环境真实连接授权、E1 审批 | **项目负责人 / 人工授权** | 无 |
| 生产连接与生产写 | **当前不授权** | 无 |
| `route_shadow` | **record-only**，只记录对比，不影响 active 路由 | 无 |

**自治程度的提升不是自动发生的**：任何一格从「无」变为「可建议」，或从「可建议」变为「可决定」，都必须同时具备离线 eval 证据、真实失败样本、明确的人工授权和一份 ADR。**模型能力升级、框架升级或供应商更换都不构成提升自治程度的理由。**

## 5. 稳定模块与职责

### 5.1 Agent Gateway

负责认证、租户/actor/channel/environment 上下文、请求大小限制、幂等键入口、trace_id 注入和错误映射。它不解析领域意图、不产生计划、不直接碰工具。

### 5.2 XiaoweiRuntime

应用层唯一编排入口，负责组装依赖、创建或恢复任务、接受或读回已持久化的 interaction artifact、调用 Runner、
汇总 evidence 和生成 `RenderPayload`。它通过显式、版本化 key 的 capability binding 选择领域 planner、
Answerability、可选 advisory projector 与 renderer；binding 不拥有候选生成权。模型建议先以 task 级
insert-once 事实保存，TaskView 只在任务终态后展示。Runtime 保持轻薄，不承载具体数据库、Prometheus
或 Jenkins 业务分支。

Only the granted durable `execute_task()` path loads/saves interaction artifacts and calls
the provider. Its production caller under `src/` remains `application/worker.py`.
The compatibility `XiaoweiRuntime.handle()` path first persists/submits the task, then
obtains a real attempt grant and calls the same durable execution path; duplicate terminal
submissions only re-project the recorded task. Heartbeat ownership stays in one small
application helper: Worker wraps execute/retry, while `handle()` wraps only its post-grant
attempt. Each attempt has exactly one owner; the public Runtime stays small.

### 5.3 Interaction Context / Clarification

I1/I2 不使用通用父任务历史或 `ContextAssembler`。TaskStore 不保存普通 conversation
history，provider chat/session 也不是任务事实源。I2 的普通对话只返回由当前 `CapabilitySnapshot`
投影出来的能力目录：回答与用户文本无关，不读取历史、不保存长期记忆、不调用工具、不进入
Resolver/Planner/Gateway。带 `clarification_parent_task_id` 的子任务不能走这条通道，否则一次性
澄清父链会被一句"成功"悄悄烧掉——它收成
`REJECTED / interaction.clarification_subject_incompatible`。连续补槽只通过
`clarification_parent_task_id` 显式消费一个 `CLARIFICATION_REQUIRED` 父任务；每个子任务都重新鉴权、
重新分类、重新 Resolver，并由 `SlotVerifier` 从本轮文本与父 `ClarificationRecord.confirmed_slots`
中确定性生成可信 Params。

`clarification_parent_task_id` 只表示澄清父链，不是任意终态“继续这个任务”。普通对话记忆、资料查询
历史、用户日志分析上下文和飞书 reply/thread 语义必须另立存储与 ADR，不能扩展澄清链或复用同名字段。

**当前准入快照与历史渲染 binding 分离（W5）。** `TaskViewRuntime` 不再用一对 snapshot/bindings
同时承担两种角色：`conversation_snapshot` 是**当前准入快照**，只回答普通对话"此刻能做什么"；
`rendering_bindings` 是代码内完整注册表，只按已持久化计划的精确 capability/version 查投影器。
`XiaoweiRuntime` 另持当前执行 bindings（键集合精确等于准入快照），其内嵌 view 显式收到这两份不同
权威的数据。进程运行形态是闭集 `XIAOWEI_RUNTIME_PROFILE`：`offline_recording`（默认，开发/测试/
smoke，保留三能力 recording）与 `release`（产品运行）。Settings 只接受 `release + dev-local/dev +
StarRocks disabled`；release 下 internal-api、web-app、feishu-listener、channel-worker 与 worker 五个
进程共用固定 ID 的空准入快照 `snapshot.w5.provider-off.empty.v1`，普通对话如实回答当前无可执行
能力；worker 另用空执行 binding 和不注册任何 adapter 的 Gateway，能力请求在 Resolver 阶段确定性
拒绝，工具调用为 0；装配完成后 `sys.modules` 不含任何 fake/recording 模块（fake import 只在 offline
分支与内存栈内部）。真实 adapter 进入 release 必须另立 RI2/RI4/H/RI6 方案，不是往空快照里加 binding。

Model context is bounded by field count, row count, Unicode character count and
serialized UTF-8 bytes. Fixed system/policy/capability prefixes are versioned local
constants; RI3 does not enable provider caching or sessions. Variable evidence is
projected separately. Typed builders apply raw per-field character/UTF-8-validity checks
and aggregate character/count limits before the total `redaction.scrub_text()` function;
there is no redaction-exception branch. Model ports never receive `RequestEnvelope`.
`InteractionClassifierRequest` 只接受 `user_text` 与可选 `clarification`，不接收通用 history
或已保存 advisory。`user_text` 在脱敏前检查 8,192 字符上限与 UTF-8 有效性，脱敏后的 typed
request 再复验字段约束；澄清内容来自 Runtime 读取的 `ClarificationRecord`。
8,192 字符本身隐含 32 KiB UTF-8 上限，不重复设置不可达的第二门。完整 typed request 的
序列化 JSON 另受 512 KiB 总量上限约束，超限拒绝；不存在旧通用历史的截断/保留预算。

The StarRocks advisory projector derives names and order directly from
`SLOW_QUERY_SURFACE.allowed_columns`, takes at most 20 rows, and applies closed type
rules to every derived field. Missing, extra or invalid columns reject the whole batch.
`stmt`, `clientIp`, `digest`, target/config internals, full rows, secrets, connections
and raw objects never enter model context. Advisory evidence is explanation input;
it is not a side channel into planner arguments or later classifier requests.
The projector runs only after successful deterministic execution and a sufficient
Answerability verdict. Rendering includes a stored advisory only for a `SUCCEEDED` task
whose rebuilt typed input digest still matches.

Accepted model intents bind provider/model/prompt/schema identity in their input digest
and recovery check. Rule-origin intents have no causal relationship to that model profile:
their digest binds only the scrubbed typed input plus `origin=rule`, provider identity stays
empty, and model prompt/schema upgrades must not invalidate an unfinished rule task.

RI3 places that typed projector in `application/model_advisory.py`: application is the
layer allowed to consume both capability surfaces and Evidence. `rendering/` receives
only already-validated display data, does not import `capabilities` or read
`SLOW_QUERY_SURFACE`, and retains its existing narrow module-layer allowlist.

### 5.4 CapabilityRegistry / CapabilityResolver

Registry 是声明和版本索引，不是“关键词总表”，也不是执行器。Resolver 接收
`IntentDraft + RequestContext + CapabilitySnapshot`，只从当前注册快照生成一个 `CandidateSet`，并解释
每个候选的必要上下文、拒绝原因和匹配证据。I1 后只有
`InteractionKind.CAPABILITY_REQUEST + RoutingDisposition.PROCEED` 才能进入 Resolver；非执行通道只能
稳定拒绝、进入澄清终态，或在 I2 的 conversation 情况下返回 `RoutingDisposition.RESPOND` 的能力
目录投影；它仍不能在入口层自造候选或调用工具。

所有 active 路由和 shadow 观测必须使用这份 `CandidateSet`。shadow 不得再次计算候选；漂移时以 Resolver 的输出为准，shadow 只记录 `observed_disagreement`。

### 5.5 PlanCompiler

将一个已解析候选编译成 `ExecutionPlan`。PlanCompiler 是确定性的：参数 schema、环境映射、目标解析、SQL 模板/AST、步骤依赖和输出投影都可测试、可复现。模型原文只能作为输入线索，不能作为计划原文写入执行。I1 后 Planner 只接收 capability 专属不可变 Params；原始 `IntentDraft`、普通 dict、日志和网页文本都不能直接成为 Planner 输入。

### 5.6 WorkflowRunner

Runner 是已编译工作流步骤的执行宿主，不拥有领域安全规则。它按计划步骤推进、持久化游标并处理
暂停/恢复。RI3 将现有 heartbeat 提成 application helper；Worker 用它覆盖完整 `execute_task()` 与
retry scheduling，兼容 `handle()` 用它覆盖自己的 attempt。Runner 只移除周期心跳所需的
`heartbeat_interval_seconds`、sleep 注入、`_heartbeat()` 与 `_run_with_heartbeat()`；保留
`DEFAULT_LEASE_TTL_SECONDS`、`lease_ttl_seconds` 与 `_require_current_grant()`，使 start/resume
仍在执行前做一次 grant 续租校验。该一次性校验不是第二个周期心跳 owner。所有持久写仍校验
grant/fencing。阶段 timeout 由各 service 管理，不新增 task deadline、
`TaskAttemptSupervisor` 或第二套生命周期。

稳定接口：

```python
class WorkflowRunner(Protocol):
    async def start(
        self,
        grant: TaskAttemptGrant,
        *,
        plan: ExecutionPlan,
        target: ResolvedTarget,
        context: RequestContext,
    ) -> TaskOutcome: ...
    async def resume(
        self,
        grant: TaskAttemptGrant,
        external_input: ExternalInput | None = None,
        *,
        plan: ExecutionPlan,
        context: RequestContext,
        target: ResolvedTarget,
        approval: ApprovalRequest | None = None,
    ) -> TaskOutcome: ...
```

`grant` 是 Worker 通过 TaskStore 唯一领取点取得的当前执行权；Worker 进入整个 attempt 前先用 grant 的
owner/token 续租验证，再启动唯一 heartbeat。Runner 仍在具体持久化写入前校验 grant/fencing，但不
自建 heartbeat。`plan`、`target` 与 `context` 出现在两个方法里，是因为**恢复时的漂移检测需要活的
对照物**：调用方必须以持久化 submission 重新解析并编译，再把当前计划、目标与 policy revision 交给
Runner，与 `PlanStore` 中最初保存的事实逐项比较。这里的“重新解析”只消费已持久化的
`AcceptedInteractionArtifact`，不重新调模型。若两边都从存储读最初计划，比较的是同一个值，检查恒真——
安全检查会静默退化成空操作。恢复通过后仍执行存储中的原计划；当前计划只用于验证。Runner 不拥有
领域安全规则，因此不能自行重算这些值。

M2 曾把接口写成 `start(task_id)` / `resume(task_id, external_input)`。那个形状隐含"只要 task_id 就能推进任务"，与上一段的职责不相容；M3 落地真实 Runner 时据此修正了契约。**不要把它改回窄签名**：唯一能让窄签名成立的写法，是调用方绕过 Protocol 直接调具体类，那会让 Runtime→Runner 这条边在类型层完全失去契约。

Runner 在每个步骤执行前调用统一的 `StepAdmission`：先做 `ToolPolicy`；步骤携带 SQL 信封时做 `SQLGuard`，携带已注册 PromQL 信封时做确定性模板重编译比对；副作用步骤再调用 `ApprovalGate`，通过后才把 `ToolCall` 交给 `ToolGateway`。operation 的 gateway 从版本化 `OperationSpec` 派生，不读用户、模型或 step arguments。因此审批既不是入口层总开关，也不是每个 runner 各自复制的一套安全逻辑。

### 5.7 ApprovalGate

ApprovalGate 是 application/governance 组件，负责创建审批请求、校验审批主体和渠道、绑定计划/目标/策略版本、处理过期/拒绝/冲突，并返回可审计的 gate decision。

受控写操作的唯一顺序：

```text
precheck(read)
  → ApprovalGate.pause
  → 人工决定
  → resume
  → 重新解析 actor / tenant_id / environment_id / target / current state
  → 重新计算 plan_hash / target_fingerprint
  → 不匹配则 stale_approval，匹配才允许 one write
  → readback
  → trusted outcome 或 indeterminate
```

批准不等于执行成功；批准后进程崩溃、网络超时或 readback 不可证时必须进入 `indeterminate` 或可恢复状态，不能伪造 `succeeded`。

### 5.8 ToolGateway

ToolGateway 是数据面唯一工具入口，负责超时、重试策略、凭证引用、审计、脱敏、adapter 选择和结果归一化。领域层只依赖 Gateway Protocol。

adapter 返回内部 `AdapterResponse`，由 Gateway 私有工厂创建公开的 `ToolResult`，这样调用方在类型边界上不能把原始 SDK 响应伪装成可信结果。Python 无法提供绝对的语言级私有性，因此同时使用模块可见性约定、静态检查和绕过契约测试；不把“私有”描述成超出语言能力的强保证。

ToolGateway 是**数据面**唯一工具入口。控制面的配置管理连通性探针不属于它：RI5 本地 Web Admin
中由管理员明确点击触发的 Gemini/飞书连接测试只验证凭据可用性，不创建 Task、TaskSubmission、
Evidence 或 capability，不产生 `ToolResult`，不进入 Policy/SQLGuard/ApprovalGate 链，也不改变
任何服务的 readiness；它们既不是 capability 执行，也不得成为绕过 Gateway 访问运维目标的通道。
该边界是显式决策而非实现推断，见
[ADR-014](docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md) §RI5 修订 R4 与
[ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md) §RI5 修订 R3（两者已于 2026-09-14
接受；设计授权与实现、真实调用证据分别记录于 handoff）。任何让探针顺手做真实工作的扩展——执行查询、
读取业务数据、写入运维目标——都必须回到完整安全链，或先修订上述 ADR。

真实 connector 必须注册为 target-bound adapter，由 Gateway 按
`(gateway, target_fingerprint)` 精确选择；同一 gateway 不得同时注册 generic 与
target-bound adapter，错目标不得回退 recording。Gateway 在连接前还会复核 tenant、environment、
actor 和激活窗口。M6b 的 StarRocks connector 进一步用配置 revision 与服务端 physical identity
preflight 闭合逻辑目标和物理集群；完整决策见
[ADR-012](docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md)。

### 5.9 Evidence、Memory 与 Rendering

- `EvidenceEnvelope` 记录来源、来源类型、capability、时间、是否样本、是否只读、限制和脱敏引用。
- **证据在步骤边界生成并写入 ledger**：Runner 在每个步骤的工具调用返回后构造 `EvidenceEnvelope`，并写入任务作用域的 append-only `EvidenceLedger`。`TaskOutcome.evidence_refs` 只携带引用，因此消费方（Runtime 渲染、Runner 求值可选分支的 `StepCondition`）一律按引用从 ledger 读回，**不读 Runner 的内部变量**。这使"证据是可寻址、可审计的事实"在执行期就成立，而不是事后归档；也使 M4 的跨进程恢复不必改变消费方。
- **Evidence builder 同时取得可信执行目标与不可信工具结果**：Runner 把本步骤刚通过 `StepAdmission` 的同一个 `ResolvedTarget` 作为必填参数传入 builder；恢复路径只在 `target_fingerprint` 复核通过后传入目标。`ToolResult`、adapter payload 与外部文本均不能提供或覆盖该目标。builder 据此执行 capability 专属的环境、资源身份与字段一致性校验；无法证明一致时 fail-closed。为保持边界最窄，builder 不接收完整 `RequestContext`。
- target-bound 真实结果还必须携带 Gateway 注入的 config revision、物理身份引用、driver version
  与 preflight verdict；Evidence builder 逐项比对获批策略。失败结果只允许记为
  `preflight=unverified`，不能借 adapter payload 或 limitations 把失败伪装成已验证。
- `ExternalContent` 统一包装日志、错误、知识、网页和用户粘贴文本，标记来源和不可信级别。
- 任务状态与证据分别由 TaskStore 与 EvidenceLedger 持久化，不由模型会话承载。普通显式父任务历史
  不进入模型；澄清补槽只走 `clarification_parent_task_id`，无父引用时不自动推断历史。
- `AcceptedInteractionArtifact` 是任务级 insert-once 的不可信输入事实；任务 retry 读回它，仍以当前 capability snapshot、target 和 policy 重跑确定性解析。provider 已收到请求但保存前崩溃时允许再次调用，这是 RI3 明确接受的无执行副作用 at-least-once 语义。
- Reflection 只读消费 `EvidenceEnvelope`，产出结构化的可答性结论（充分性、限制、缺失项、是否降级、是否需补充信息）；它不产生 `ToolCall`、不修改 `ExecutionPlan`、不写 TaskStore。边界见 §4.2。
- `ModelAdvisory` 是 task 级 insert-once 的可选展示事实；失败时不保存并保留确定性答案。TaskView 只在
  原任务已终态时附加它，它不能修改事实、限制、状态、证据引用或 `next_steps`，也不能触发新模型调用。
- 终态继续由现有 TaskStore transition 和 `TaskViewRuntime` 的 plan/evidence 重建语义负责；RI3 不为
  模型接入引入统一终态表或新的进度状态机。
- `RenderPayload` 是跨渠道的统一回答投影，Web、飞书和 CLI 只选择展示方式，不重算业务结果。

## 6. 核心契约

跨模块交互必须有版本化的 Pydantic model 或 Protocol。初始契约如下：

| 契约 | 关键字段 | 约束 |
| --- | --- | --- |
| `RequestEnvelope` | request_id、tenant_id、actor、channel、text、idempotency_key、environment_id（可选） | 入口统一上下文，禁止入口自造业务字段；Web 的 parent selector 不成为执行字段 |
| `TaskSubmission` | envelope、context、as_of、clarification_parent_task_id（可选） | 只消费 `CLARIFICATION_REQUIRED` 父任务；不表达普通历史、最近消息或任意终态继续 |
| `RequestContext` | tenant_id、actor、environment_id、trace_id、policy_revision | 三项执行上下文必填；模块边界显式传递，不从全局变量读取 |
| `InteractionDraft` | proposed_kind、capability_draft、confidence、source | 模型/规则的不可信交互候选；`InteractionKind` 与 `RoutingDisposition` 由确定性 Router 采纳或拒绝 |
| `AcceptedInteractionArtifact` | task_id、artifact_version、draft、origin、provider/model metadata、input/result digest、usage、fencing | I1 的 insert-once 交互事实；替代新运行路径的 accepted intent，artifact version 2 才会被 Runtime loader 解释，模型元数据不来自模型响应 |
| `IntentDraft` | intent、slots、missing、confidence、source | 模型可产生，但不具执行权 |
| `ModelAdvisory` | task_id、advisory_input_digest、advisory、safe metadata/result digest、fencing | input digest 绑定安全证据投影；insert-once；只在原任务终态后展示，不能改变原终态或动作 |
| `ModelInvocationProfile` | 固定 provider/model/API（RI3 为 Developer API `v1beta` + `https://generativelanguage.googleapis.com`）、prompt/schema revision、thinking/timeout/output 上限 | composition root 注入不可变非秘密 profile；没有任意 endpoint/proxy、tool 或 provider registry |
| `InteractionClassifierRequest` / `SlowQueryAdvisoryRequest` | 前者精确为 `user_text/clarification`；后者精确为 `rows/sampled` | 两个窄口专属 DTO；无原始 RequestEnvelope、任意 context/prompt/schema/tools/endpoint escape hatch；诊断 rows 为 0 时零调用 |
| `ProviderInteractionResponse` | `proposed_kind`、可选 `capability`、confidence | provider wire schema 只能表达 interaction 分类；`capability` 存在性必须与 `CAPABILITY_REQUEST` 匹配，本地再把 capability 字段复验成闭集 `IntentDraft` |
| `ProviderIntentSlots` | 现有 intent allowlist 并集的 11 个 omitted-or-string 字段 | Developer API schema 不宣告 null；显式 null/未知字段都整体拒绝，本地语义校验仍以 intent-specific allowlist 为单一真源 |
| `ModelUsage` | nullable input_tokens/output_tokens | 只由 adapter 从锁定 SDK 已归一化的 prompt/candidates token count 构造；SDK 会把可强制转换的 integer-like raw 值（bool、整数形 float、数字字符串）转为 int，本地再做 non-negative signed-64-bit 边界；不保留 total/raw metadata，不进入 provider response schema |
| `ModelPortError` | 闭集 `ModelErrorCode` | application 可消费的 provider-neutral 安全失败；intent 仅将 rate-limit、5xx server 与明确 transport error 列为可重试 |
| `InteractionModelResult` / `AdvisoryModelResult` | accepted draft/advisory + `ModelUsage` | 两个 port 的具体输出；无 tuple、全局 last_usage 或 callback 隐式侧道，支持并发调用安全传递 |
| `CapabilitySpec` | id、version、domain、operation、gateway、schemas、policy_profile、evidence_contract | 声明能力；operation gateway 是工具路由唯一真源，不直接执行 |
| `CapabilityInputBinding` | params_type、input_schema_ref、allowed_clarification_fields、slot_verifier、planner、confirmed_slot_projector | I1 的 capability 输入绑定；`CapabilitySpec.input_schema_ref`、binding 和 Params 必须一致 |
| `CandidateSet` | resolver_version、snapshot_id、items、rejections | Resolver 唯一真源，shadow 只消费 |
| `ClarificationRecord` | task_id、subject、reason_code、missing_fields、confirmed_slots、record_version、fencing | I1 的澄清事实；`ClarificationRecordStore` grant-fenced、insert-once，record 必须先于 `CLARIFICATION_REQUIRED` 终态 |
| `ReadClass` | BOUNDED、RESTRICTED | I1-C 的静态只读分类；由 `OperationSpec` 派生到 `PlanStep.read_class`，进入 Plan schema V2 / `plan_hash`，并由 StepAdmission 重新派生后交给 ToolPolicy 判定 |
| `ExecutionPlan` | plan_schema_version、capability_id、capability_version、steps、policy_profile、policy_revision、budget | 确定性、可重放、不可由模型直接覆盖；绑定单一 capability。**`plan_hash` 与 `target_fingerprint` 不是本契约的字段**，由 `planning` 按需计算，绑定值存于 `ApprovalRequest`（[ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md) D3）。I1-C 已把 `PLAN_SCHEMA_VERSION` 升到 2，并把 `PlanStep.read_class` 纳入计划规范形状 |
| `PolicyDecision` | allow、reason、risk、policy_revision、obligations | fail-closed，理由结构化 |
| `ApprovalRequest` | task_id、step_id、plan_hash、target_fingerprint、policy_revision、subject、expires_at、state | 审批与具体步骤绑定；`policy_revision` 使「policy 变化不能静默让旧审批继续生效」可独立断言（ADR-009 D3） |
| `AdmissionCertificate` | step_id、operation、effect_class、policy_decision、approval_ref、plan_hash、target_fingerprint、tool_call_hash | `StepAdmission` 产出、`ToolGateway` 消费；**同时绑定步骤身份与调用内容**，`tool_call_hash` 覆盖 `ToolCall` 全部字段，使「未经准入即调用工具」与「持合法凭证替换参数」都不可表达（ADR-009 D4） |
| `ToolCall` | gateway、operation、typed_args、timeout、idempotency_key | 不含任意代码/任意 SQL escape hatch |
| `ToolResult` | status、data_view、raw_ref、source、limitations、trace_id | 由 Gateway 构造，原始数据默认不进模型 |
| `ExternalContent` | source、trust、content、digest、captured_at | 所有外部文本的统一包装；恒为 untrusted，不能改变 policy、目标、权限、审批状态或执行计划 |
| `AdapterResponse` | status、payload、source、error、elapsed | adapter 的**内部**返回类型，只能由 `ToolGateway` 私有工厂转成 `ToolResult`；不得跨越 Gateway 边界外泄 |
| `AnswerabilityVerdict` | sufficient、limitations、missing、downgrade_suggestion、needs_user_input | Reflection 的唯一输出契约；**不含步骤、工具、目标、权限或 SQL 字段**（§4.2）；终态由 Runtime/Runner 依据它确定性判定 |
| `AgentError` | code、category、retryable、cause_ref、message_key | 结构化 error model；外部错误先包成 `ExternalContent` 再由确定性 mapper 归类，不把第三方错误文本当控制信号 |
| `EvidenceEnvelope` | facts、source、captured_at、readonly、limitations | 事实与解释分开 |
| `TaskOutcome` | status、terminal_reason、evidence_refs、render_ref | 终态语义封闭，indeterminate 一等公民 |
| `RenderPayload` | answer、sections、next_steps、status、refs | 只负责展示投影，不承载执行决策 |
| `ClarificationPayload` | reason_code、missing_fields、prompt | 只从 `ClarificationRecord` 投影，不是 `RenderPayload` 分支 |
| `ExecutionDisclosure` | capability、target summary、read disposition、projected slots、external_target_access | 首次 Gateway 前的可查询披露事实；表示计划语义，不表示渠道已送达或真实目标已联网 |

上表是按里程碑演进的稳定契约摘要：原始内核 DTO 的精确类型由 M2 审定，RI3 新增模型事实
契约的精确字段与类型以已接受的 [ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md) 为准。
契约的**名称与职责边界**在各自 ADR 获批后冻结，各模块不得另造同义 DTO。

执行上下文的字段名在全项目统一为 `tenant_id`、`actor` 和 `environment_id`，不使用 `tenant`、`env` 等别名。`RequestEnvelope.environment_id` 可选，表示渠道显式指定；`RequestContext` 的三项均必填，由 Gateway 解析后产生，环境无法解析出唯一值时 fail-closed。模块边界显式传递 `RequestContext`；其他 DTO 不机械复制这三项，各契约的精确字段归属由其自身语义决定。详见 [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)。

## 7. Hash、审批和任务一致性

### 7.1 `plan_hash`

`plan_hash` 是对规范化计划的 SHA-256：

```text
sha256(canonical_json({
  "plan_schema_version": ...,
  "capability_id": ...,
  "capability_version": ...,
  "ordered_steps": [
    {"step_id": ..., "operation": ..., "typed_arguments": ...,
     "depends_on": ..., "side_effect": ..., "effect_class": ...,
     "read_class": ...,
     "condition": {"kind": ..., "ref_step_id": ..., "field": ...,
                   "threshold": ..., "expected_result": ...}}
  ],
  "policy_profile": ...,
  "policy_revision": ...,
  "budget": {"max_steps": ..., "max_tool_calls": ..., "max_model_tokens": ...}
}))
```

`plan_schema_version` 当前取值为 `2`：V1 为 ADR-009 的首个规范形状，V2 由 I1-C 加入
`PlanStep.read_class`。**计划绑定单一 capability，步骤不携带 capability 标识**；跨 capability
计划须递增 schema version 并另立 ADR（ADR-009 D2）。

`effect_class`、`read_class`、`condition` 与 `budget` 都进入规范输入集：分类漂移、只读范围漂移、
可选分支条件变化和预算被放大，都必须被 `plan_hash` 检出，否则一份已批准的计划可在恢复时执行
不同的动作。

`PlanBudget.max_model_tokens` 表示 plan 形成后模型调用可请求的最大 provider-generated token 数，
provider 的 `max_output_tokens` 不得超过它；模型输入另受字段/字符/行数闭集限制。plan 形成前的意图
解释无法由尚不存在的 plan 管理，必须使用版本化的固定 intent call budget。RI3 首版 advisory 沿用
slow-query 现有 4000 token 上限，不为展示增强升级 capability 或 Registry snapshot。

`canonical_json` 使用固定字段顺序、UTF-8、无空白、稳定数字和字符串规范化；不包含 request_id、trace_id、时间戳、模型原文、日志、secret、token 或显示文案。计划字段新增或语义变化必须递增 schema version，并同步更新实现中的「字段 → 指纹键」映射表——该映射表由安全测试断言其键集等于对应 DTO 的字段集，因此漏加字段会立即失败。

### 7.2 `target_fingerprint`

`target_fingerprint` 是对已解析执行目标的 SHA-256：

```text
sha256(canonical_json({
  "tenant_id": ...,
  "environment_id": ...,
  "provider": ...,
  "resource_kind": ...,
  "resource_ids": [...],
  "selector_version": ...
}))
```

资源 ID 按 capability 规定的规范化规则排序；不把连接串、凭证、原始 SQL、用户 display text 或未确认的自然语言别名当作目标指纹。目标解析失败或不稳定时不能生成可审批指纹。

### 7.3 TaskStore 语义

TaskStore 是任务事实真源，至少提供：幂等创建、CAS 状态迁移、worker lease、heartbeat、fencing token、stale recovery、终态保护、审批记录和审计事件。所有写入都必须采纳存储层返回的 winner；调用方不能用本地旧对象覆盖 winner。

After real-model integration, TaskStore additionally carries only insert-once
`AcceptedInteractionArtifact` and `ModelAdvisory` artifacts. Both writes bind the current
grant/lease/fencing state. Exact digest replay returns the winner; different content,
an expired lease or stale fencing is rejected. Recovery reads accepted artifacts first.
If the provider received a call before local save, only that unsaved model call may
repeat under ADR-015's no-execution-side-effect at-least-once rule.
Model call count, latency, usage and fallback enter the existing safe trace/audit path.
Model ports return accepted DTO plus nullable bounded usage atomically. The composition
root exposes the same immutable invocation profile to the adapter and application service;
artifact persistence does not inspect the concrete adapter or duplicate its profile.
The trace contract adds `PipelineStage.MODEL` plus typed `ModelCallObservation` (call
kind, total elapsed milliseconds, request count, nullable input/output usage and a
closed fallback code). Model-stage free-form detail remains empty. Prompt, response,
provider error text and credentials are neither trace fields nor persisted artifacts.
Observation numbers are strict, non-negative and signed-64-bit bounded; total request
count is 0–2 and advisory is further limited to 0–1.
I1-A 新分类事件使用 `ModelCallKind.INTERACTION`，单 attempt 的
`ModelCallObservation.request_count` 为 `0..1`；历史 `ModelCallKind.INTENT`
只保留为 legacy trace read compatibility，仍按 `0..2` 只读兼容，`ADVISORY` 仍为 `0..1`，
不得把字段级上限全局降为 1。
I1-D 实现 `ExecutionDisclosure` 后新增 `PipelineStage.DISCLOSURE`，错误归因阶段从 RI3 后的十个
扩展为十一个阶段；当前源码和契约测试已包含该披露阶段。

以下 clarification-aware idempotency 是 I1-A 当前源码事实：非空
`clarification_parent_task_id` 进入 `request_dedup_digest` 和已存 `submission_digest`，但不进入
`idempotency_scope_digest`。前者判定同 key 语义冲突，完整 submission digest 只作为存储行一致性
checksum。空 parent 的 canonical bytes 保持不变。普通 `create_task()` 不接受澄清父任务；澄清
回复必须经 `TaskStore.create_clarification_child()` 一次性消费 `CLARIFICATION_REQUIRED` 父任务。
所有创建路径与共享 submission-integrity readback 都必须用持久化 clarification parent 重算该语义
digest；创建与后续完整性检查不能使用不同输入。

Worker 复用现有 heartbeat，覆盖整个 `execute_task()` 与 retry scheduling；Runner 不启动第二份。
RI3 does not add a whole-task deadline. It preserves the current 25-second StarRocks
query-timeout upper bound and 30-second read-only policy maximum. The proposed
180/190/195/200-second StarRocks-specific layers belong to RI4 and are not current
source facts. Model intent/advisory use independent 60/180-second stage budgets.
不能在 RI3 现场临时扩容后冒充已验证架构。

RI3 的 advisory 在原任务终态提交前保存，但 TaskView 只有看到终态才展示；这允许恢复重用，又不要求
把全项目终态统一迁入新表。保存失败按数据库故障处理，不能先把任务标成成功再补写展示。
A task can therefore remain RUNNING for up to 180 extra seconds. On recovery, committed
steps are adopted from the step journal without Gateway replay; only an advisory not
yet saved may be requested again.

渠道 ingress 仍是 task submission 后写 binding/projection 的既有路径；澄清子任务只消费
`CLARIFICATION_REQUIRED` 父任务，并在创建与 Worker 执行时重新核验。通用 Web parent 不再是
受支持路径。若后续要修渠道半聚合窗口，应单独立项并覆盖 Web/飞书全部失败和幂等路径。

`CANCELED` 只是终态闭集成员，不等于本阶段已有用户取消命令。RI3 不新增取消 API；进程级
`CancelledError`/SIGTERM 取消并等待正在运行的模型 coroutine，随后依赖现有 lease/stale recovery，
不能把基础设施取消伪装成用户取消。任何迟到 SDK 结果都必须丢弃且不得落库；本地取消不构成
provider 远端已经停止处理或停止计费的证据。

M5 在同一个 aggregate 事务端口内增加 submission、dispatch/attempt、step execution journal 与 retry handoff。发现候选仍是只读查询；唯一领取点 `begin_task_attempt()` 在行锁内返回当前 winner、grant 与本次提交的不可变 submission，拒绝分支不泄漏用户原文。步骤调用只有 `begin_step_attempt()` 返回 `PROCEED` 后才能进入 ToolGateway；步骤终局、Evidence 与 audit 原子提交，确认丢失后以持久化 digest 重放收敛。

fencing token 有两个推进点：成功取得 lease 时推进；`schedule_retry()` 提交成功时再次推进，使旧 grant 立即失效。heartbeat 不推进 token。任务 attempt、任务失败预算与 Worker 进程级基础设施故障窗口是三套独立计数；基础设施故障不写任务失败。

状态终态化、retry 调度与 step checkpoint 的对应审计和状态事实同事务提交。成功领取只改变可由 TTL 自愈的 lease/attempt，不强制事务审计。持久化写必须区分 confirmed rollback 与 not-confirmed，不能从异常类别猜测数据库是否已经提交。

**stale recovery 拆成「发现」与「接管」两半，只有前一半在 TaskStore 里**（M4）：`list_stale_leases(*, limit)` 是只读方法，返回「曾被租出、租约已过期、未终态」的任务，按 `(lease_expires_at, task_id)` 稳定排序。它不 claim、不调度、不判断审批是否应当恢复、不改变任何状态；接管仍走 `acquire_lease()`，并发 winner 仍由存储层裁决。把两半合成一个方法会让 TaskStore 长出调度能力，而调度属 Worker。

**审计事件由 TaskStore 承接写入，消费路径不在 M4**：`record_audit_event(*, event)` 是 append-only 写入，按 `(task_id, seq)` 编号并返回本次分配到的 `seq`。**证据表替代不了审计**——证据回答「看到了什么」，审计回答「系统做了什么、准入判成了什么」，一次被策略拒绝的调用不产生任何证据但必须留下审计。载荷是 M2 的 `TraceEvent`，其 `detail` 已在契约层做过键值双向脱敏并限长，持久化层不再脱敏第二次。

`TraceEvent.task_id` 可为 `None`（任务创建之前就失败的请求），而 `task_audit_events.task_id` 是 `NOT NULL`；这道落差在入口显式拒绝（`UnscopedAuditEventError`），不交给数据库约束——交给约束会让两个实现抛出不同的异常。这类无任务归属的事件仍走 `TraceSink`。

M5 已把 `TraceSink.emit` 收窄为必须等待的异步出口，并用显式 `Delivery` 参数区分独立 durable 写、已随命令提交、已确认回滚和结果未知；调用方不得从 `TraceEvent` 字段猜投递路径，也不得 fire-and-forget。无任务归属的事件只写结构化日志；带任务且未随命令提交的事件由 durable sink 经 `record_audit_event` 落库。审批记录的消费路径仍归 M8。

**判定规则与存储实现分离**（M4）：拒绝顺序、fencing 闭合真值表、租约与续租条件由 `persistence/decisions.py` 的纯函数持有，内存实现与 PostgreSQL 实现逐字共用。这样消除的是「两个实现各自跑偏」——那类分叉不会被任何单实现的用例发现，因为每个实现都通过自己那份断言。代价是纯函数里的 bug 会让两个实现同时通过，因此由变异反证承重。

建议的通用任务状态：

```text
created → planning
     ├──→ clarification_required
     └──→ running → awaiting_approval → running
               ├──→ succeeded
               ├──→ failed
               ├──→ rejected
               ├──→ canceled
               └──→ indeterminate
```

具体领域的运行状态不强行统一；统一的是 TaskStore 的生命周期、终态和并发语义。任何终态都不可被后到事件改写；恢复时如果状态、计划、目标或 policy revision 不一致，必须重新规划或安全终止。

## 8. Capability 与受限 DSL

### 8.1 通用 capability

一个 capability 的最小可执行闭环是：声明 → Resolver 候选 → 确定性 PlanCompiler → Policy profile → ToolGateway adapter → evidence contract → renderer → unit/contract/eval。只登记 YAML 或 Registry 不会自动产生可执行能力。

application 层可用显式 `CapabilityRuntimeBinding` 组装上述实现，Runner 只消费更窄的 execution binding。binding key 必须与 Registry snapshot 完全一致，且只能精确消费 Resolver 已生成的候选；缺失、重复、错版本或 profile 不一致都 fail-closed。M6a 仍使用显式 Python 注册，不实现通用 DSL 或动态插件发现。

### 8.2 DSL 的适用范围

DB/资产域的资源和操作组合数量大、结构相似，可以使用受限 DSL 减少枚举条目；DSL 是声明式语义层，不是执行语言。每个 DSL 实例必须可映射到一个确定性的 compiler 和一个 policy profile。

DSL capability 的最小八项契约为：

1. `capability_id` / `version`。
2. `domain` / `resource_kind` / `operation`。
3. `selector_schema`：允许的资源选择器和规范化规则。
4. `argument_schema`：类型、范围、必填槽和默认值。
5. `compiler_ref`：确定性计划/SQL AST 编译器引用。
6. `policy_profile`：只读/受控写风险级别及预算。
7. `evidence_contract`：来源、字段白名单、readback 要求和限制。
8. `eval_ref`：对应的安全、契约和行为评测集合。

DSL 可以复用域级默认 owner、renderer、adapter 和审计配置，因此不要求每个实例重复填写全部实现元数据；但上述八项不能省略。DSL 明确豁免的内容是任意 executor、任意 prompt、任意 SQL 模板和任意 formatter：这些只能来自受审查的代码/策略注册，不由 DSL 文本携带。

安全保证来自 compiler + Policy + SQLGuard + Gateway + evidence/readback 的共同约束，而不是来自 DSL 本身。推广 DSL 前先记录：枚举型条目总数、DSL 实例数、覆盖的操作/资源组合、误路由数、拒绝正确率和维护成本；不能只凭“文件数量变少”判定成功。

## 9. SQL 与工具安全

- 允许执行的 SQL 由确定性 compiler 生成；模型提供的 SQL 只能作为展示性建议或待解析输入，不能直接执行。
- SQLGuard 使用 `sqlglot` AST 解析，按方言和 policy profile 检查语句类型、表/列范围、子查询、锁、写入、注释和多语句边界。
- PromQL 只允许 capability 注册的完整模板：参数经闭集 schema 与统一 escape 后编译，准入期从 `typed_arguments` 重新编译并逐字节比对；M6a 不接受任意 PromQL，不引入 parser，也不把固定模板安全外推为任意表达式安全。
- AST 无法解析、方言不确定、目标不完整或权限无法确认时 fail-closed。
- 所有工具调用都通过 ToolPolicy 校验 target 与 context 的 `tenant_id`、`environment_id` 一致，再检查 environment allowlist、operation、effect class 与最大超时；步骤/调用预算继续由计划和 Runner 约束。target/context 漂移必须在 Gateway 与 Evidence 之前 fail-closed。
- adapter 不把第三方错误文本当作可信控制信号；原始错误先包成 `ExternalContent`，再由确定性 error mapper 归类。
- 每个真实写操作最多一次 write admission；审批、幂等键、fencing 和 readback 一起保障 at-most-once 尝试语义。不能无证据承诺 exactly-once。

## 10. 外部文本与上下文安全

所有外部文本都包装为：

```text
ExternalContent(
    source="tool|knowledge|web|user|log",
    trust="untrusted",
    content=...,
    digest=...,
    captured_at=...,
)
```

进入模型上下文时必须带清晰的来源和不可信标记，并放在隔离的 evidence 区域。模型输出不得引用其中的“忽略规则”“执行命令”“提升权限”等指令改变系统行为。安全测试必须包含工具错误注入、SQL 注释注入、知识文档注入和用户粘贴内容注入。

## 11. 部署形态：Docker Compose

第一阶段目标部署：

```text
docker compose
├── api               薄 internal API + TaskView/提交投影
├── worker            完整 Runtime + WorkflowRunner 异步执行进程
├── feishu-listener   飞书入站协议、身份上下文与任务提交
├── channel-worker    TaskView → 飞书卡片投影
├── web-app           OAuth/session、工作台与安全任务详情
├── migrate           一次性 schema upgrade，成功后应用进程才启动
└── postgres          TaskStore / ChannelStore / session / audit / evidence index
```

五个长驻应用角色与 migrate 共用一个应用镜像和 Python 包，通过进程命令区分。API、飞书 listener、
channel worker 与 Web app 只装配各自所需的窄端口，不复制业务路由、Policy、Guard、执行或终态语义；
只有 task worker 持有完整 Runtime、WorkflowRunner、ToolGateway 与目标 adapter。真实基础设施通过
网络配置和 adapter 接入，不能把凭证 bake 进镜像。

`ReadinessProbe` Protocol 与只含数据库、migration head 和装配状态的 `ReadinessReport` 位于
`contracts/`；具体检查实现位于 `persistence/` 并由 `interfaces/local_stack.py` 注入，入口不直接
依赖 Engine。Provider 凭据按 RI5 的受信装配从 `integration_config_file` JSON 边界读取；
单行 `interfaces.secret_file` 不是该 JSON 的第二条读取路径。owner/mode/中间目录等宿主权限边界
必须在对应真实接入阶段结合挂载与实际调用方验证，不能从静态配置推定完成。

Secrets are mounted only through fixed file references resolved by a trusted composition
root. API and Web publish only loopback ports in the base Compose file; PostgreSQL, task
worker, listener and channel worker publish no host ports.

**Provider 凭据与进程权限契约（ADR-015 RI5 修订，W4a 三域化）**：Provider 凭据不走 Docker secret。
配置分为 `ConfigDomain` 闭集 `ai` / `feishu` / `resources` 三个固定域，每域一个宿主 Git-ignored
目录、一份固定文件与一个独立 `generation`：Gemini 在 `/run/xiaowei-config/ai/config.json`，飞书在
`/run/xiaowei-config/feishu/config.json`，W4b 的运维资源登记在 `/run/xiaowei-config/resources/config.json`。
Compose 只挂域目录、从不挂父目录：`web-app` 三域读写；`worker` 只读 `ai` 与 `resources`；
`feishu-listener` / `channel-worker` 只读 `feishu`；`api` / `migrate` / `postgres` 不挂任何域。
每个进程只读取自己消费的域、只为 `(service, domain)` 签加载回执，不替兄弟进程作证。
**运维资源登记契约（W4b）**：`contracts/resource_config.py` 以 `kind` 判别 StarRocks 与 Prometheus 两种
资源，文档最多 100 个、`resource_id` 为服务端 CSPRNG 生成的 32 位小写十六进制且全局唯一；主机只接受
ASCII hostname / IPv4 / 不带方括号的 IPv6 字面量，Prometheus base URL 只接受 http/https 且禁止
userinfo/query/fragment，scheme 与 TLS、认证方式与 username/secret 的组合在契约层一次闭合。校验只用
`ipaddress` / `urlsplit` 这类纯函数：不解析 DNS、不探测端口、不发 HTTP、不登录。资源 Secret 与 Provider
Secret 同一规则；清除 Secret 后资源保留、`configured` 为假，新建时按 kind 要求必须带 Secret。写入只经唯一
配置写服务的 create/update/clear-secret/delete，复用两阶段审计（target 为 `resource:<id>`），每次整份
重写并推进一次 resources `generation`，其余资源对象与其 Secret 原样复用；ID 撞车是闭集 `conflict`，不覆盖、
不重试。Web 安全投影只返回 ID、kind、环境、显示名、启用、host/port 或 base URL 与 `configured`，页面标注
"已保存，尚未接入"且没有测试入口。task worker 只为签 `(worker, resources)` 加载回执读取该文件并随即丢弃，
资源不进入 Runtime、Resolver、Registry、ToolGateway 或任何 adapter；真实连接与目标注册属于 W4c。
旧单文件 `.config/integrations.json` 只是 `interfaces/integration_config_migrate.py` 的一次性迁移输入：
运行时不双读、不回落、不自动迁移；旧文件仍在时预检固定报 `migration_required`，迁移器遇到新旧内容
不一致同样返回 `migration_required` 且不覆盖任何一边。模型 override 只剩装配开关，在 `services.worker.environment` 下声明
`XIAOWEI_GEMINI_ENABLED=true`；共享的 `x-app-environment` anchor 与所有非 worker 服务都不带
该 flag。

Neither `GEMINI_API_KEY_FILE` nor `GEMINI_API_KEY` is a Settings/`_FIELD_TO_ENV` key,
container environment value or `.env.example` entry; the only
application setting is default-false
`XIAOWEI_GEMINI_ENABLED`. Provider/model/API/limits are versioned constants;
RI3 fixes Developer API `v1beta` and canonical origin
`https://generativelanguage.googleapis.com`.
W2 将任务工作台与配置面分开：`/app` 及其静态脚本不含 Provider 配置表单或配置请求；
本地 Admin 只经 `/admin/api/config/{ai,feishu}` 按域读取与保存、经 `…/clear` 显式确认清除、经
`/admin/api/config/test/*` 测试配置，并继续受强制改密、认证来源、Origin、CSRF 与 JSON body 上限约束。
**配置写入契约（W4a）**：`application/integration_config_service.py` 的 `IntegrationConfigService` 是
唯一配置写服务，只依赖 application 定义的窄 `IntegrationConfigRepository` port；文件 adapter
`interfaces/integration_config_repository.py` 由 web-app composition root 装配，`_conformance.py`
静态锚定其结构兼容，application 不反向 import interfaces，consumer 进程不装入写服务。同一实例用
`asyncio.Lock` 串行化 read-modify-write，同域并发保存的 `generation` 连续且不丢更新。每次保存、清除
与连接测试都是两阶段 Admin 审计：先写 `STARTED`（写不进即 503 且不动文件），再执行动作，最后写
`SUCCEEDED`/`FAILED`；operation id 只由服务端 `trusted_trace_id()` 派生并带 `w4:` 前缀，客户端不能
提供。Secret 缺省表示保留，`null`/空串拒绝，清除只走独立确认动作。OAuth 连接测试的 state 经
`web_oauth_test_contexts`（`rev_0018`）绑定到其 `STARTED` operation id 与被测飞书 `config_generation`，与登录
context 互不消费；开始测试前除凭据须在当前代次通过外，`(web, feishu)` 加载回执也必须是当前代次的
`loaded`（交换 code 的是 Web 启动期装配的 adapter）；回调交换 code 前再核对当前文件代次、Web 已加载代次与
绑定代次三者相等，任一漂移即写 `FAILED` 且不交换、不记结果，结果只记到绑定代次。state 无效或过期时保留
`STARTED`，state 已消费而 Admin session 失效时不调用 Provider 并写 `FAILED`。
`rev_0018` 同时把 `service_config_state.provider` 改名为 `config_domain`（`gemini`→`ai`），其 downgrade
遇到 W4a 审计事实、OAuth test context 或 `resources` 回执时在任何 DDL 之前拒绝。飞书 Admin 只能读取独立的
`/admin/api/integration-status` 脱敏投影，不能复用 raw config DTO；旧
`/app/api/config*` 路径不保留兼容入口。OAuth 连接测试完成后回到 `/admin`，仍不签发或轮换
Session，也不改变身份、激活与真实调用授权。
README/runbook alone explain host-side key setup. This path requires Docker
Compose 2.24.4+ (the project support floor shared with RI6's `!override` deployment
path) and Linux containers. The version floor and rendered-config check are
necessary but insufficient: a split-fake file secret must pass a functional mount
preflight without printing its value before model activation. This is not a
`docker stack deploy` contract. The three channel processes remain in the `m7-channels`
profile with their feature flags defaulting to false; no offline result proves activation.
Offline smoke proves only default-off behavior in the shared image. Until separate
real-application, credential, network, deployment and canary authorization exists,
this topology must not be described as an activated channel or model.

[ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md) 的 RI5 修订 R1
规定了当时的单文件 `.config/integrations.json` 与镜像内 UID/GID `10001:10001`；W4a 按已批准的
三域矩阵替换单文件形态，UID/GID 不变；实施与运行证据见 handoff。

基础 Compose 继续只发布 loopback 端口，局域网发布只能由独立 override 打开。
**2026-09-20 修订**：`ADR-014` Web 产品修订 R2 取消了"首次强制改密必须在 loopback 阶段完成"
的先后硬门；`WebMode.LAN_HTTP` 只接受 loopback/RFC1918 Host 的形态约束不在替代范围内，
继续生效。

**用户目录与 Admin 审计契约（W1a）**：迁移 `rev_0014` 定义 `user_accounts`、`user_role_assignments`、`external_identities` 与 `admin_audit_events` 四张表，
并给 `local_admins` 补上 `user_id` 外键。`UserDirectoryStore.apply()` 是前三张表与
`local_admins.user_id` 的**唯一写入口**：本地管理员 bootstrap 与旧身份迁移都是它的命令成员，
不另开写方法、不另开事务。每一次授权改变都在**同一个事务**里带上一条审计事件，而那条事件的
`action`、`target_kind`、`target_ref_digest`、`outcome` 与闭集 effect 全部由 store **从命令派生**，
调用方没有任何参数能指定它们。`AdminAuditStore` 只有 `append_started` / `append_terminal` /
`append_denied` / `load` 四个窄方法，`persistence/` 里不存在针对 `admin_audit_events` 的
`UPDATE` / `DELETE`。飞书 `open_id` 只以 domain-separated 摘要落库。旧静态身份文档由一次性、
整批原子的迁移命令搬进目录，迁移后该文件保留只读、不双写。W5 起它只经无参命令
`python -m xiaowei_agent.interfaces.legacy_identity_migration` 从固定只读挂载点
`/run/xiaowei-legacy/feishu-identities.json` 读取；长期 Settings 没有对应字段或 `XIAOWEI_*` 变量，
任何 Compose 服务都不挂载它。命令只输出 `created_count / skipped_count / deferred_count` 与闭集错误码，
无旧文档时输出 `not_applicable`。

**身份激活契约（W1b）**：`rev_0015` 建立
`activation_requests`，申请创建/过期由 `ActivationStore` 承载，批准或拒绝仍只经
`UserDirectoryStore.apply()`，使申请终态、目录授权与审计保持同事务。入口层通过
`IdentityActivationService` 复用该写路径，并以 `DirectoryFeishuIdentityDirectory` 每次从
**数据库目录**重建主体；静态身份文件只保留为一次性迁移输入，不是运行时 fallback。

**终态激活保留（W5）。** `APPROVED / REJECTED / EXPIRED` 行含受控 PII，固定保留 30 天
（`ACTIVATION_TERMINAL_RETENTION_DAYS`，不是配置项）：批准/拒绝以 `decided_at`、过期以 `expires_at`
为终态时间，`terminal_at <= now - 30 天` 即删除。窄 `ActivationRetentionStore` 只有无参的
`purge_expired_terminal()`，覆盖**全库所有作用域**，在与容量门相同的全局 activation advisory lock 下
先把过期 `PENDING` 转成 `EXPIRED` 再删除；仍有效的 `PENDING`、Admin 审计、目录、Session、任务与证据
都不在清理面。它只由一次性命令 `activation_retention` 调用，输出四个计数，没有 Web 路由。

Web OAuth 对真正未登记的身份创建或复用申请，返回闭集 `403` / `activation_pending`，不签发
Session；已经绑定但停用或失去当前作用域角色的身份仍按普通认证失败处理，不能借激活重新进入。
群入口只在未知群成员当次事件中创建申请，并由 `ActivationNotificationService` **单次**发送不含
正文、链接、按钮、Admin 名单或 @ 的通用卡片；私聊未知身份继续 fail-closed。批准/拒绝当前只有
模块级一次性入口，W1b 不建设管理页面、通知人绑定或可靠投递队列。

部署启用前，W5 必须先显式迁移并核验旧静态身份，不能依赖进程启动时自动迁移。

**W3 V1 Admin 身份管理面**：现有 `/admin` shell 通过
`/admin/api/users`、`/admin/api/activations` 与 `/admin/api/audit` 提供用户、待激活申请和
Admin 审计的分页投影；状态/角色变更及激活决定继续只经 `UserDirectoryStore.apply()`，审计仍与
授权事实同事务提交，不创建第二条授权写路径。本地 Admin 与当前作用域内仍为 ACTIVE ADMIN 的
飞书会话可以使用这三类身份管理能力；配置写入与连接测试继续只允许本地 Admin。

浏览器只接收安全投影，不接收 `subject_ref`、`open_id`、目标摘要、Secret、正文或原始异常；
普通用户仍只通过具体任务或结果链接进入，不提供“我的结果”门户。批准身份或授予产品角色不等于授予任务
结果访问权，Admin 不绕过结果 ACL。没有 OAuth adapter 时，本地 Admin 的三类管理能力仍可装配；
这组离线能力已经构成负责人确认的 W3 V1，但不表示真实飞书、部署、canary 或用户验收已经完成。

**W3 后续增强**包括 DBA/值班/激活通知人职责绑定、可靠/私聊通知及敏感内容查看审计。它们延期但
未取消，须另行计划与授权，且不作为 W4a/W4b 或 W5 的进入条件；W4b 只登记资源参数，不得在资源
文件中夹带职责或结果 ACL。

**产品边界与阶段归属**：W2 的登录改造与 `LocalCredential.username`，W3 V1 的 Admin 待办/
用户与审计 UI，W3 后续增强的职责绑定与可靠通知，W4a 的三域配置文件和进程挂载矩阵，W5 的 release override 与
边缘限流，按 [总体开发计划](DEVELOPMENT_PLAN.md) 分阶段交付。字段级定义见
[Web 运维工作台总体设计](docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md)
及 ADR-007/013/014/015。各阶段实施、测试、真实运行、部署与验收状态只由 handoff 记录。

`.gitignore` 与 `.dockerignore` 必须排除 `.secrets`、`.env`/`.env.*`；模型 runbook 禁止执行或留存会打印解析环境的
`docker compose config --environment`。普通 `docker compose config` 只可记录不含 secret 值的脱敏
结果。

`/healthz` 只回答对应 HTTP 进程是否存活，不触碰 TaskStore；`/readyz` 才检查数据库连接、
migration head 与装配状态。首次切换到 `release` 前运行只读的
`release_preflight`：组合三域目录预检、数据库可达与 schema head，并完整分页扫描固定 `dev-local/dev`，
任一非终态任务返回 `tasks_not_drained`，任一持久化 plan/evidence（含 `SUCCEEDED`）返回
`historical_execution_data_present`——既有 schema 不能证明它不是 recording 时代的合成结果；预检不删
历史、不接受覆盖参数。Worker 不引入第二个队列或状态真源，而是从 TaskStore 发现候选，再通过
带 lease/fencing 的唯一领取事务取得执行权。channel worker 只从 ChannelStore 领取投影订阅并回读
同一 TaskView；渠道投递状态不改写任务真相。migration 是一次性前置服务，失败时应用进程不得启动。
M5/M7 基础栈只装配确定性无模型 interpreter、fake/recording ToolGateway 与 fake 渠道 port；
真实模型要由独立 Compose override 显式开启，且只在 task worker 装配固定 provider/model 和窄口。
Compose 可运行不等于获得真实模型、真实渠道或真实运维目标的调用许可。

初始不强制 Redis。只有出现可测的队列吞吐、分布式租约或缓存需求时，才增加服务，并先更新契约、迁移和运维文档。PostgreSQL 的全文检索先满足知识/证据索引；只有 eval 和查询指标证明不足时才引入 pgvector。

## 12. LangGraph 的位置与准入

LangGraph 不是领域架构，也不是安全边界。它可以实现 `WorkflowRunner`，但必须遵守：

- 节点只调用 Runtime ports，不直接调用第三方工具客户端。
- 审批、重解析、hash 校验、Policy、SQLGuard、readback 和终态都由共享组件完成。
- checkpoint 仅保存 runner 恢复所需的游标/临时状态，TaskStore 仍保存任务事实和审计。
- 不允许 LangGraph 节点内部再实现一套候选解析、审批或工具路由。
- 通过至少两类真实生命周期样本：多步暂停恢复、崩溃重试/并发抢占，并与 DeterministicRunner 比较可恢复性、可观测性、延迟和维护成本后再转为默认。

这使“先不用”不会锁死未来；后续实现 LangGraph 只需新增 Runner adapter 和 checkpoint adapter，不改 capability、policy、TaskStore、ToolGateway 或 channel 契约。

### 12.1 Multi-Agent 与 Runner 准入是两件事

**采用 LangGraph 不等于采用 Multi-Agent。** Runner 准入评估（Phase 6 / M9）只评估 `WorkflowRunner` 的实现方式，**不授予任何 Multi-Agent 权限**。

Multi-Agent 必须在 Runner 准入结论之后**另设独立里程碑和独立 ADR**，进入条件至少包括：

1. 真实存在、可举证的职责拆分需求，而不是“看起来更 Agentic”；
2. 各 Agent 有清晰独立的上下文、工具和记忆边界；
3. 具备多步暂停恢复、崩溃重试、并发抢占等真实生命周期样本；
4. 相对单 Runner 有量化收益。

**不准入的判定**：没有量化收益，或任一 POC 需要复制 `CapabilityResolver`、Policy、Approval、TaskStore、`ToolGateway` 的真源。

**永久约束**：无论将来是否采用，Multi-Agent 都不能绕过本文第 4 节的安全链，也不能产生第二个状态真源、第二份计划、第二套审批或第二条工具路由。

## 13. Eval 与上线门槛

评测按能力层级分档，不能用一把准确率衡量所有能力：

| 层级 | 范围 | 必须回答的问题 |
| --- | --- | --- |
| L0 | 安全与不变量 | 是否拒绝越权、注入、危险 SQL、目标漂移和伪造成功 |
| L1 | 意图与补槽 | 是否识别领域、环境、资源、时间范围和缺失信息 |
| L2 | 只读闭环 | 是否拿到足够证据、正确解释限制、引用来源并可复现 |
| L3 | 生命周期 | 审批绑定、暂停恢复、并发 fencing、readback、indeterminate 是否正确 |

每个 capability 至少维护 golden cases、near-miss cases、missing-context cases、adversarial cases 和故障注入 cases。只有代码测试通过不代表可上线；需要依次记录 `declared → configured → deployed SHA → tests → canary → user-accepted` 的最强证据。

`test-env verified` 是非生产环境运行证据的旁注标签，不属于上述 readiness ladder，也不替代 `canary`；`canary` 只用于已确认部署 SHA 的受控生产灰度。

### 13.1 错误分析闭环

有 eval 不等于有改进能力。每一轮问题都必须走完这条闭环，否则视为未处理：

```text
运行 / eval
  → 阅读 trace
  → 错误归因（定位到具体阶段）
  → 选择单一根因
  → 修复
  → 脱敏失败样本晋升为 regression / eval case
  → 复测
```

支撑该闭环的 trace 必须能把一次失败定位到具体阶段。业务阶段包括 Intent、Resolver、Planner、
Admission、Gateway、Evidence、Reflection、Rendering、Lifecycle 与 Model。Model 只描述 provider/结构化复验；Intent 仍描述最终被
接受的 model/rule draft。模型失败并成功 fallback 时 Model 为失败、Intent 为成功，根因不会被重复
记到两个阶段。相应契约的建立时点见 `DEVELOPMENT_PLAN.md` 的 M1/M2/M3 与 RI3。

ADR-017/I1-D 定义 `PipelineStage.DISCLOSURE`，位于 PlanStore
读回/投影校验之后、StepAdmission/Gateway 之前；完整错误归因闭集为十一个阶段。Disclosure
只表示披露事实与审计持久化，不表示渠道送达、用户已读、审批通过或真实目标已联网。

### 13.2 eval 的边界

- **组件级 eval 与端到端 eval 分开记录**；端到端通过不能掩盖单个组件的退化，组件通过也不能替代端到端验收。
- **安全、权限、SQL 形状、审批绑定和终态语义一律由确定性断言验收。** LLM-as-judge 最多用于经过校准的主观回答质量评分，**不得用于裁决安全正确性**。
- eval 结论与人工判断不一致时，**先校准 evaluator**；不得只调整业务逻辑去迎合一个错误的指标。
- **离线 eval 结果不得表述为部署、canary 或用户验收。** 三者各自需要独立证据。

测试目录和 CI gate 固定如下：

```text
tests/
├── unit/         # 纯函数和状态迁移
├── contract/     # 模块间 DTO / Protocol 契约
├── security/     # 必须标记 pytest.mark.security
├── integration/  # Compose / PostgreSQL / fake adapter
└── evals/        # L0-L3 行为和安全评测
```

> 本节的工具链与命令为摘要，**真源是 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)**；冲突时以 ADR-008 为准。

默认测试框架为 pytest，统一以 `python -m pytest` 形式调用。触及 `governance/`、`planning/` 或 `tools/` 的变更必须运行 `python -m pytest -m security -q`；该 gate 与全量 `python -m pytest -q` 独立存在。全项目验证命令的单一真源见 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)。

## 14. 从 0 到可用的演进顺序

### Phase 0：仓库与工程基线

初始化 Git 基线、Python 包结构与工具链、配置规范、日志/trace 规范和测试骨架（测试目录与 `security` marker gate）。本阶段不实现任何业务契约类型，也不接真实写操作。

### Phase 1：Foundation

实现 `RequestContext`、`CapabilitySpec`、`CandidateSet`、`ExecutionPlan`、`ToolResult`、`EvidenceEnvelope`、`RenderPayload`，以及 `ExternalContent`、内部 `AdapterResponse`、结构化 error model 和支撑 TaskStore CAS/lease/fencing 的类型；建立 fake Gateway 和安全契约测试。

### Phase 2：第一条只读闭环

选择一个高价值、低副作用场景，例如 StarRocks 慢查询诊断。完成确定性 planner、只读 SQL AST guard、fake/recording adapter、证据和 L0-L2 eval。此时不接 LangGraph，不开放写入。

本阶段新增的包为 `evidence/`（纯证据构造器，无 async、无 I/O）、`reflection/`、`rendering/` 和 `application/`；`persistence/` 新增 `PlanStore` 与 `EvidenceLedger` 两个 port，`governance/` 新增 `ToolPolicy`、`SQLGuard`、`ApprovalGate` 与 `StepAdmission`。SQL AST 解析引入唯一的新运行依赖 `sqlglot`。

### Phase 3：TaskStore 与恢复

实现 PostgreSQL migration、任务状态、幂等、CAS、lease、heartbeat、fencing、stale recovery、terminal protection 和 approval record。

### Phase 4：API/Worker/Compose

将同步回答与异步任务分开，加入健康检查、结构化日志、配置校验、迁移和 Compose 集成验证。

### Phase 5：渠道与能力扩展

API/CLI 稳定后接 Web/飞书；随后按垂直闭环添加 Prometheus、MySQL、Kafka、Jenkins、Kubernetes 等 capability，不在入口层新增业务分支。

### Phase 6：Runner 评估

用真实的多步计划、审批中断、恢复、超时、并发和 indeterminate 样本评估 LangGraph adapter。达不到收益门槛就继续使用 DeterministicRunner；达到门槛也只替换 Runner 实现。**本阶段不授予 Multi-Agent 权限**，其独立准入条件见 §12.1。

## 15. 变更与版本规则

- 修改 DTO 的必填字段、枚举、状态迁移、hash canonicalization、审批绑定或 ToolGateway 语义，必须写 ADR、迁移方案、兼容策略和回归/eval。
- capability version 与 plan schema version 分离；能力语义变更不可只改显示名称。
- policy revision 必须进入审批绑定和审计；policy 变化不能静默让旧审批继续生效。
- 配置变更必须说明来源优先级、默认值、灰度方式和回滚方式。
- 每个变更只维护一份业务真源：不要在 Web、飞书、CLI、shadow 或 LangGraph 中复制判断逻辑。

## 16. ADR 索引

后续需要单独记录的关键决策：

- ADR-001：模块化单体与 Compose 部署边界。
- ADR-002：TaskStore 事实真源与 Runner checkpoint 的关系。
- ADR-003：DeterministicRunner / LangGraphRunner 准入和切换条件。
- ADR-004：Capability DSL 的最小契约与量化推广标准。
- ADR-005：审批绑定、hash canonicalization 和目标漂移处理。
- ADR-006：PostgreSQL 全文检索到 pgvector 的升级门槛。
- ADR-007：首批能力、初始执行上下文与真实调用许可（已记录：[docs/adr/ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)）。
- ADR-008：工程与测试基线，含 Python 3.11、pytest、security marker gate、Ruff 和 mypy（已记录：[docs/adr/ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)）。
- ADR-009：`plan_hash` 规范形状、审批绑定与工具准入（已记录：[docs/adr/ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md)）。
- ADR-010：M5 持久执行尝试与本地 Compose 边界（已记录：[docs/adr/ADR-010](docs/adr/ADR-010-m5-durable-attempt-and-compose-boundary.md)）。
- ADR-011：M6a capability binding、operation gateway 与 PromQL 固定模板准入（已记录：[docs/adr/ADR-011](docs/adr/ADR-011-m6a-capability-binding-and-promql-template-admission.md)）。
- ADR-012：M6b StarRocks 真实只读 adapter 的精确目标绑定（已记录：[docs/adr/ADR-012](docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md)）。
- ADR-013：M7 Web/飞书薄渠道与身份投影边界（已记录：[docs/adr/ADR-013](docs/adr/ADR-013-m7-channel-boundary.md)）。
- ADR-014：真实飞书 OAuth 与 Web 激活边界（已记录：[docs/adr/ADR-014](docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md)；**RI5 修订 2026-09-14 Accepted**）。
- ADR-015：Gemini 真实模型的窄口、数据、时限、记忆和执行权边界（已接受：[docs/adr/ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md)；**RI5 修订 2026-09-14 Accepted**）。
- ADR-016：曾计划用于 RI5 独立配置测试 worker 的进程边界例外。新 RI5 设计取消该 worker，改为
  控制面探针，因此 **ADR-016 不再立项**；相应结论写入
  [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)
  §RI5 Amendment R2，同时 R1 修订 D4 的 B2 行（任务模型调用仍 worker-only，Web 只获得
  固定 synthetic 探针）。ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订已由项目负责人于 2026-09-14 成套接受；该接受不授予 RI3 PR 3E 与 RI2 的真实调用 GO。

ADR 未形成前，不把对应争议藏在代码默认值里。
