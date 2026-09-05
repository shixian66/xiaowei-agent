# 小维 Agent 2.0 目标架构

> 状态：Target V1 / 架构基线草案。本文描述从 0 开始建设的稳定目标，不声称当前代码、依赖、容器或线上环境已经存在。当前实际进度只看 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)。

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
                       │ IntentDraft    │
                       │ Resolver       │
                       │ PlanCompiler   │
                       └───────┬────────┘
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
  → XiaoweiRuntime
  → ContextAssembler
  → IntentInterpreter
  → CapabilityResolver
  → PlanCompiler
  → WorkflowRunner
       → StepAdmission
            → ToolPolicy
            → SQLGuard (需要 SQL 时)
            → ApprovalGate (副作用步骤才调用)
       → ToolGateway
       → Readback (需要时)
  → EvidenceBuilder
  → Reflection / Answerability
  → RenderPayload
  → TaskOutcome
```

### 4.1 模型边界

`IntentInterpreter` 可以使用模型，但模型输出必须解析为带 schema 的 `IntentDraft`。模型输出的意图、目标线索、自然语言参数和置信信息都属于不可信输入；Resolver 必须重新从已注册能力、租户上下文、环境目录和确定性规则得到最终候选。

模型允许：

- 提取用户意图和缺失槽位。
- 对已取得的脱敏证据做解释、摘要和建议。
- 生成面向人的澄清问题。

模型不允许：

- 直接调用工具或 SDK function calling。
- 直接决定 capability、资源 ID、环境、SQL、命令、审批结果或写入动作。
- 修改 policy、system prompt、TaskStore 状态或安全配置。
- 把外部文本中的指令当成系统指令。

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

应用层唯一编排入口，负责组装依赖、创建或恢复任务、调用 Runner、汇总 evidence 和生成 `RenderPayload`。它保持轻薄，不承载具体数据库、Prometheus 或 Jenkins 业务分支。

### 5.3 ContextAssembler

只向模型和 planner 提供本轮必要上下文：租户、actor、环境、已确认槽位、摘要化历史、能力摘要和脱敏证据。上下文按 token 预算截断，固定 system/policy/capability 前缀可缓存，变量证据单独拼接；完整 rows、secret、连接串和原始大对象不进入模型上下文。

### 5.4 CapabilityRegistry / CapabilityResolver

Registry 是声明和版本索引，不是“关键词总表”，也不是执行器。Resolver 接收 `IntentDraft + RequestContext + CapabilitySnapshot`，只从当前注册快照生成一个 `CandidateSet`，并解释每个候选的必要上下文、拒绝原因和匹配证据。

所有 active 路由和 shadow 观测必须使用这份 `CandidateSet`。shadow 不得再次计算候选；漂移时以 Resolver 的输出为准，shadow 只记录 `observed_disagreement`。

### 5.5 PlanCompiler

将一个已解析候选编译成 `ExecutionPlan`。PlanCompiler 是确定性的：参数 schema、环境映射、目标解析、SQL 模板/AST、步骤依赖和输出投影都可测试、可复现。模型原文只能作为输入线索，不能作为计划原文写入执行。

### 5.6 WorkflowRunner

Runner 是任务生命周期的宿主，不拥有领域安全规则。它按计划步骤推进、持久化游标、处理暂停/恢复、租约、重试和终态。

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

`grant` 是 Worker 通过 TaskStore 唯一领取点取得的当前执行权；Runner 不再自行领取 lease，进入后先用 grant 的 owner/token 续租验证，再启动 heartbeat。`plan`、`target` 与 `context` 出现在两个方法里，是因为**恢复时的漂移检测需要活的对照物**：调用方必须以持久化 submission 重新解释、解析并编译，再把当前计划、目标与 policy revision 交给 Runner，与 `PlanStore` 中最初保存的事实逐项比较。若两边都从存储读，比较的是同一个值，检查恒真——安全检查会静默退化成空操作。恢复通过后仍执行存储中的原计划；当前计划只用于验证。Runner 不拥有领域安全规则，因此不能自行重算这些值。

M2 曾把接口写成 `start(task_id)` / `resume(task_id, external_input)`。那个形状隐含"只要 task_id 就能推进任务"，与上一段的职责不相容；M3 落地真实 Runner 时据此修正了契约。**不要把它改回窄签名**：唯一能让窄签名成立的写法，是调用方绕过 Protocol 直接调具体类，那会让 Runtime→Runner 这条边在类型层完全失去契约。

Runner 在每个步骤执行前调用统一的 `StepAdmission`：先做 `ToolPolicy`，需要 SQL 时做 `SQLGuard`，副作用步骤再调用 `ApprovalGate`，通过后才把 `ToolCall` 交给 `ToolGateway`。因此审批既不是入口层总开关，也不是每个 runner 各自复制的一套安全逻辑。

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

### 5.9 Evidence、Memory 与 Rendering

- `EvidenceEnvelope` 记录来源、来源类型、capability、时间、是否样本、是否只读、限制和脱敏引用。
- **证据在步骤边界生成并写入 ledger**：Runner 在每个步骤的工具调用返回后构造 `EvidenceEnvelope`，并写入任务作用域的 append-only `EvidenceLedger`。`TaskOutcome.evidence_refs` 只携带引用，因此消费方（Runtime 渲染、Runner 求值可选分支的 `StepCondition`）一律按引用从 ledger 读回，**不读 Runner 的内部变量**。这使"证据是可寻址、可审计的事实"在执行期就成立，而不是事后归档；也使 M4 的跨进程恢复不必改变消费方。
- `ExternalContent` 统一包装日志、错误、知识、网页和用户粘贴文本，标记来源和不可信级别。
- working memory 存在 TaskStore；result memory 只存脱敏、限长、可重建摘要，不存完整 rows 或 secret。
- Reflection 只读消费 `EvidenceEnvelope`，产出结构化的可答性结论（充分性、限制、缺失项、是否降级、是否需补充信息）；它不产生 `ToolCall`、不修改 `ExecutionPlan`、不写 TaskStore。边界见 §4.2。
- `Advisory` 只能填展示槽，失败时回退确定性文案。
- `RenderPayload` 是跨渠道的统一回答投影，Web、飞书和 CLI 只选择展示方式，不重算业务结果。

## 6. 核心契约

跨模块交互必须有版本化的 Pydantic model 或 Protocol。初始契约如下：

| 契约 | 关键字段 | 约束 |
| --- | --- | --- |
| `RequestEnvelope` | request_id、tenant_id、actor、channel、text、idempotency_key、environment_id（可选） | 入口统一上下文，禁止入口自造业务字段 |
| `RequestContext` | tenant_id、actor、environment_id、trace_id、policy_revision | 三项执行上下文必填；模块边界显式传递，不从全局变量读取 |
| `IntentDraft` | intent、slots、missing、confidence、source | 模型可产生，但不具执行权 |
| `CapabilitySpec` | id、version、domain、operation、schemas、policy_profile、evidence_contract | 声明能力，不直接执行 |
| `CandidateSet` | resolver_version、snapshot_id、items、rejections | Resolver 唯一真源，shadow 只消费 |
| `ExecutionPlan` | plan_schema_version、capability_id、capability_version、steps、policy_profile、policy_revision、budget | 确定性、可重放、不可由模型直接覆盖；绑定单一 capability。**`plan_hash` 与 `target_fingerprint` 不是本契约的字段**，由 `planning` 按需计算，绑定值存于 `ApprovalRequest`（[ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md) D3） |
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

上表的字段是**初始集合**，精确类型与最终字段在 M2 详细计划中审定；但契约的**名称与职责边界**在此冻结，各模块不得另造同义 DTO。

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
     "condition": {"kind": ..., "ref_step_id": ..., "field": ...,
                   "threshold": ..., "expected_result": ...}}
  ],
  "policy_profile": ...,
  "policy_revision": ...,
  "budget": {"max_steps": ..., "max_tool_calls": ..., "max_model_tokens": ...}
}))
```

`plan_schema_version` 的首个取值为 `1`（[ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md) D1）。**计划绑定单一 capability，步骤不携带 capability 标识**；跨 capability 计划须递增 schema version 并另立 ADR（ADR-009 D2）。

`effect_class`、`condition` 与 `budget` 进入规范输入集的理由见 ADR-009 D1：分类漂移、可选分支条件变化和预算被放大，都必须被 `plan_hash` 检出，否则一份已批准的计划可在恢复时执行不同的动作。

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
created → planning → running → awaiting_approval → running
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
- AST 无法解析、方言不确定、目标不完整或权限无法确认时 fail-closed。
- 所有工具调用都通过 ToolPolicy 做 tenant_id、actor、environment_id、resource、operation、risk 和预算校验。
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
├── api        Agent Gateway + 同步 Runtime
├── worker     WorkflowRunner 异步执行进程
├── migrate    一次性 schema upgrade，成功后 API/Worker 才启动
└── postgres   TaskStore / approval / audit / evidence index
```

API 与 worker 可以共用一个应用镜像和 Python 包，通过进程角色区分；镜像内部不直接绑定某个渠道。真实基础设施通过网络配置和 adapter 接入，不能把凭证 bake 进镜像。

API、worker 与 migrate 使用同一个应用镜像。`ReadinessProbe` Protocol 与只含数据库、migration head 和装配状态的 `ReadinessReport` 位于 `contracts/`；具体检查实现位于 `persistence/` 并由 `interfaces/local_stack.py` 注入，API 不直接依赖 Engine 或 persistence。secret 只通过文件引用挂载；API 只向宿主 loopback 发布端口，PostgreSQL 与 Worker 不发布宿主端口。

`/healthz` 只回答 API 进程是否存活，不触碰 TaskStore；`/readyz` 才检查数据库连接、migration head 与装配状态。Worker 不引入第二个队列或状态真源，而是从 TaskStore 发现候选，再通过带 lease/fencing 的唯一领取事务取得执行权。migration 是一次性前置服务，失败时 API 与 Worker 不得启动。M5 本地栈只装配确定性无模型 interpreter 与 fake/recording ToolGateway；Compose 可运行不等于获得真实模型或真实运维目标的调用许可。

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

支撑该闭环的 trace 必须能把一次失败定位到具体阶段：Intent、Resolver、Planner、Admission、Gateway、Evidence、Reflection、Rendering、Lifecycle。相应契约的建立时点见 `DEVELOPMENT_PLAN.md` 的 M1/M2/M3。

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

ADR 未形成前，不把对应争议藏在代码默认值里。
