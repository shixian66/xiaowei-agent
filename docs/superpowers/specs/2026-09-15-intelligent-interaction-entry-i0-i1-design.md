# 小维 Agent 2.0 智能交互入口 I0/I1 总体设计

- 状态：Review Draft V1；输入契约已由项目负责人确认，待 Claude/Codex 按精确 SHA 复审后写入项目真相文档
- 规划基线：`main@174fa13c7d64f46cedb6b630209ffebe50cae3f5`
- 证据等级：当前源码事实 + 只读架构设计；没有实现、运行、部署、灰度或用户验收证据
- 日期：2026-09-15

## 1. 大白话结论

当前小维已经有一条受治理的确定性执行主干，但入口仍主要围绕“已有 capability 请求”工作。I0/I1
不是重写执行系统，而是在现有 `CapabilityResolver` 前增加一个统一的智能门厅：先判断用户是在普通
交流、查资料、分析用户提供的日志，还是要求执行某项能力；信息不足时先问清楚；只有明确且结构完整
的 capability 请求，才允许进入现有安全执行链。

模型只负责提出候选理解，确定性代码负责最终分流、槽位可信升级、环境一致性、读写风险和执行披露。
I1 完成后，小维仍不会实现普通对话、资料查询或日志分析本身；这些分别属于 I2、I3、I4。I1 只保证
它们不会被误送进能力执行，也不会因为“模型很自信”而获得工具权限。

完整路线固定为：

```text
interfaces
→ 创建 Task / 传递已认证 RequestContext
→ Worker attempt
→ load-or-create InteractionArtifact（每 attempt 最多一次应用层模型调用）
→ DeterministicInteractionRouter

  ├─ conversation / knowledge_lookup / log_analysis
  │  → I1: REJECTED(interaction.route_not_available)
  │  → I2/I3/I4: 各自独立通道
  │
  ├─ unknown / 信息含糊
  │  → ClarificationRecord
  │  → CLARIFICATION_REQUIRED（终态，不可恢复）
  │
  └─ capability_request + proceed
     → CapabilityResolver
     → select_entry()
     → SlotVerifier
        ├─ incomplete → ClarificationRecord → CLARIFICATION_REQUIRED
        ├─ invalid    → REJECTED
        └─ ready      → capability-specific Params
                       → Planner
                       → PlanStore(Plan, Target)
                       → ExecutionDisclosure 就绪审计
                       → StepAdmission
                       → ToolGateway
                       → Evidence / Outcome
```

## 2. I0 与 I1 的边界

### 2.1 I0：文档真相闭合

I0 是独立的纯文档里程碑，只做以下事情：

- 新增 ADR-017，冻结本设计中的交互路由、澄清链、可信槽位、`ReadClass` 和披露屏障；
- 更新 `ARCHITECTURE.md` 的稳定调用链、状态语义、模块边界和契约表；
- 更新 `DEVELOPMENT_PLAN.md`，在现有 M/RI 路线中加入 I0–I5，并明确 I1 与 M8 的依赖关系；
- 更新 `README.md` 的能力边界和开发导航；
- 更新 `AGENT_HANDOFF.md` 的当前事实、精确 SHA、下一步和未授权事项。

I0 不新增 Python 契约、migration、UI、模型调用或工具调用。I0 合入不等于 I1 已实现。

### 2.2 I1：安全分流、必要澄清和执行披露

I1 实现：

- 两维交互决策：`InteractionKind` 与 `RoutingDisposition`；
- 单次分类调用携带可选 `IntentDraft` 候选；
- application 层确定性 Router；
- `CLARIFICATION_REQUIRED` 终态与一对一、单次消费的澄清子任务；
- 独立、窄、grant-fenced、insert-once 的 `ClarificationRecordStore`；
- capability 级 `SlotVerifier`、类型化 `CapabilityParams` 和可信槽位升级；
- 静态 `ReadClass`、Plan schema V2 与 Policy 二次防线；
- 第一次 Gateway 调用前的“执行披露就绪屏障”；
- Web、飞书、CLI、API 与离线 `handle()` 共用同一 Runtime 路由。

I1 不实现 I2–I4 的业务响应通道，不开放新的真实模型、真实目标、部署或 E1 权限。

## 3. 不可破坏的总边界

1. LLM 只能产生 `InteractionDraft`/`IntentDraft` 候选，不能决定最终 capability、目标、参数、SQL、
   `read_class`、授权、审批或工具顺序。
2. `RoutingDisposition.PROCEED` 只表示信息足以进入下一层，不表示授权执行。
3. 只有 `interaction_kind == capability_request` 且 `disposition == proceed` 能进入
   `CapabilityResolver`。
4. capability 执行继续走现有确定性主干；I1 不建立旁路。
5. 普通对话不能调用工具；资料查询即使只读，未来也必须通过受治理工具并给来源；第一版日志分析只
   处理用户主动提供的日志。
6. 用户日志、附件、网页和知识文档都是 `ExternalContent`，不能改变目标、权限、策略、审批或计划。
7. 用户原请求可以表达“继续查询”的业务意图，但不构成权限授权。授权只来自已认证
   `RequestContext`、当前 capability 声明、编译计划与 ToolPolicy。
8. 聊天中的“确认”“可以”“继续”永远不是 `ApprovalDecision`；副作用步骤仍使用正式
   `ApprovalGate`。
9. 不引入 ReAct、多 Agent、通用自主规划、复杂 Policy DSL、动态风险评分、事件总线或不必要的
   LangGraph。
10. 一个 PR 只交付一个垂直闭环；I0 文档 PR 与 I1 代码 PR 分开。

## 4. 两维交互契约

### 4.1 闭集枚举

```python
class InteractionKind(StrEnum):
    CONVERSATION = "conversation"
    KNOWLEDGE_LOOKUP = "knowledge_lookup"
    LOG_ANALYSIS = "log_analysis"
    CAPABILITY_REQUEST = "capability_request"
    UNKNOWN = "unknown"


class RoutingDisposition(StrEnum):
    PROCEED = "proceed"
    CLARIFY = "clarify"
    REFUSE = "refuse"
```

两个维度不能压成一个枚举。`kind` 回答“用户在做什么”，`disposition` 回答“系统本轮怎么办”。
例如 `capability_request + clarify` 与 `unknown + clarify` 都需要追问，但后续可继承的主题不同。

### 4.2 不可信候选与受信运行元数据分离

```text
InteractionDraft:
- proposed_kind: InteractionKind
- capability_draft: IntentDraft | None
- confidence: FiniteFloat[0, 1]
- source: model | rule

InteractionArtifactCandidate:
- draft: InteractionDraft
- origin: model | rule
- provider: str | None
- model: str | None
- provider_origin: str | None
- prompt_revision: str
- schema_revision: str
- input_digest: sha256
- result_digest: sha256
- usage: ModelUsage

AcceptedInteractionArtifact:
- InteractionArtifactCandidate 全部字段
- artifact_version = 2
- task_id
- created_at
- fencing_token
```

`provider`、模型名、origin、usage、prompt/schema revision 和 digest 由 adapter/application 生成；模型
响应不能自报这些字段。`confidence` 只用于 Eval、观测和校准，不能成为执行通行证，不设置未经真实
Eval 校准的阈值魔数。

### 4.3 分类请求

`InteractionClassifierRequest` 只包含：

- 当前任务经过既有大小、UTF-8 与脱敏边界校验的 `user_text`；
- 可选的类型化 `ClarificationContext`：父 `subject` 与父 `confirmed_slots`；
- 不含父任务原文、历史 RenderPayload、Evidence、模型推测、身份、权限或策略正文。

父 subject 与完整 confirmed snapshot 必须进入 artifact 的 canonical `input_digest`。相同回复挂在不同
澄清父任务下时，摘要必须不同。

### 4.4 单次调用与恢复语义

```text
load AcceptedInteractionArtifact
→ 已存在：模型调用 0，使用已存 artifact
→ 不存在：InteractionClassifierPort.classify() 最多调用 1 次
→ 可回退失败：生成同形状 rule fallback
→ grant-fenced、insert-once save
→ 必须使用 Store 返回或重新 load 的获胜 artifact
→ Router
```

- Provider 调用是 at-least-once；单个 attempt 最多一次 application Port 调用。
- 若 SDK 内部重试未关闭，只能声称“应用层不主动重试”，不能声称真实 HTTP 恰好一次。
- Provider 返回后、artifact 保存前崩溃，恢复 attempt 可以再次调用 Provider。
- artifact 保存后、Router 前崩溃，恢复 attempt 的模型调用为零。
- 可回退失败只包括：模型未配置、输入不适合发送、已识别 Provider 错误/超时、响应 schema 非法。
- 任务取消、进程关闭、grant/lease 丢失不得被 fallback 吞掉。
- save 冲突、grant 失效或持久化失败时，Router/Resolver/Planner/Gateway 全部为零。
- `ModelCallObservation.request_count` 对分类阶段按单个 attempt 收紧为 `0..1`。

旧 `IntentModelPort.generate_intent()`、第二次 intent 模型调用以及
`ModelArtifactStore.load_intent/save_intent` 全部淘汰，不保留双轨调用。

## 5. 确定性交互 Router

`DeterministicInteractionRouter` 位于 `application/interaction_router.py`，由
`XiaoweiRuntime` 在 `CapabilityResolver` 前调用。它是纯决策组件：

- 不访问数据库；
- 不调用模型；
- 不保存 artifact/record；
- 不转换任务状态；
- 不生成 Candidate、Plan、Target 或 ToolCall；
- 不调用工具；
- 不修改 `InteractionDraft` 或内嵌 `IntentDraft`。

Runtime 持有原始获胜 artifact，并在 Router 返回 `capability_request + proceed` 后，把其中
`capability_draft` 原样交给 Resolver。契约测试必须证明 intent、slots、missing、confidence、source
逐字段未被 Router 修改。

### 5.1 I1 决策矩阵

| 输入 | disposition | 终态/下一层 | Resolver | Gateway |
| --- | --- | --- | ---: | ---: |
| `conversation` | refuse | `REJECTED / interaction.route_not_available` | 0 | 0 |
| `knowledge_lookup` | refuse | `REJECTED / interaction.route_not_available` | 0 | 0 |
| `log_analysis` | refuse | `REJECTED / interaction.route_not_available` | 0 | 0 |
| `unknown` 或路由信息含糊 | clarify | 保存 RouteSubject，`CLARIFICATION_REQUIRED` | 0 | 0 |
| 非 capability 却携带 `capability_draft` | refuse | `REJECTED / interaction.capability_draft_forbidden` | 0 | 0 |
| capability 却没有 `capability_draft` | refuse | `REJECTED / interaction.capability_draft_missing` | 0 | 0 |
| capability + draft + 环境一致 | proceed | 进入 Resolver | 1 | 尚未决定 |
| capability + 环境不一致 | refuse | `REJECTED / interaction.environment_context_mismatch` | 0 | 0 |

Resolver 没有候选时稳定拒绝，不能回流到 conversation、knowledge 或 log 通道。

### 5.2 环境断言

`RequestContext.environment_id` 是唯一授权环境和目标解析真源。用户文本里的环境只是一项待比较的
`requested_environment` 断言：

- 未提及环境：使用 `RequestContext.environment_id`，执行前披露；
- 明确提及且与上下文一致：继续，目标仍来自 Context；
- 明确提及且不一致：`REJECTED / interaction.environment_context_mismatch`；
- 出现多个环境或表达含糊：`CLARIFICATION_REQUIRED /
  interaction.environment_assertion_unclear`；
- 使用环境标记却无法在封闭别名表中归一化：通用拒绝，不查询环境注册表，也不透露目标是否存在。

环境别名解析是纯函数，只接受 `env=<id>`、`environment_id=<id>` 和 I0 明确列出的有限中文/英文
别名；不读取数据库或外部目录。`environment_id` 不进入 confirmed slots、不从父记录继承，也不能由
用户文本覆盖。切换环境必须先由入口重新认证/绑定 Context，再创建不带澄清父引用的新根任务。

现有 `slots.setdefault("environment_id", context.environment_id)` 和 capability required-slot 中的环境
语义必须删除。

## 6. 澄清是新任务，不是暂停恢复

### 6.1 状态语义

```text
CLARIFICATION_REQUIRED:
- terminal = true
- resumable = false
- plan_created = false（路由澄清）或 false（能力补槽澄清）
- gateway_calls = 0
- reason_code: closed enum
- missing_fields: typed field tuple
- confirmed_slots: canonical full snapshot
- user_prompt: RenderPayload projection
```

本轮保存澄清事实后以 `CLARIFICATION_REQUIRED` 终结。用户回复时创建新 Task，并用
`clarification_parent_task_id` 关联；每个子任务重新鉴权、分类、路由、Resolver、SlotVerifier 和 Policy。
只有副作用审批继续使用 `AWAITING_APPROVAL` 的暂停—恢复机制。

允许迁移为 `CLARIFICATION_REQUIRED` 的非终态是 `CREATED` 与 `PLANNING`；该状态加入
`TERMINAL_STATUSES`，出边为空。Runner 不领取终态任务。

### 6.2 淘汰 RI3 通用父任务历史

- 删除 `TaskSubmission.parent_task_id`，新增语义明确的
  `clarification_parent_task_id: TaskId | None`；
- 删除 `ContextAssembler` 和任意终态父任务最多 20 条历史的读取；
- 删除 `ModelIntentRequest.history/context_truncated` 通用历史契约；
- Web 删除所有终态的“继续这个任务”，只有 `CLARIFICATION_REQUIRED` 显示“补充信息”；
- 不保留开关、旧/新双轨上下文或同名字段换义；
- I2 若需要普通聊天记忆，使用独立 `ConversationStore` 重新设计，不能扩展澄清链。

数据库 migration 在改名/删列前必须检查现有非空 `parent_task_id`。只要存在一行就 fail-closed；不能
把旧关系静默解释为澄清关系。数据保留或清理由项目负责人另行授权，migration 不自行删除。

### 6.3 一对一、单次消费

```text
一个 CLARIFICATION_REQUIRED 父任务
→ 最多一个直接子任务
→ 子任务若再次 CLARIFICATION_REQUIRED，可成为下一轮父任务
```

非空 `clarification_parent_task_id` 的数据库唯一约束是唯一消费事实源。不增加 `consumed` 布尔、消费
表、状态机或额外锁表。

创建顺序固定：

1. 在同一事务中先按 `(tenant_id, environment_id, idempotency_key)` 查重；相同 key/相同语义返回原
   子任务，相同 key/不同语义返回既有幂等冲突。
2. 再校验父任务存在、状态为 `CLARIFICATION_REQUIRED`，且 tenant、environment、actor、channel
   与已认证 channel owner 一致。不存在或未授权统一返回 not-found，不得暴露“已消费”。
3. 原子写入子 Task、TaskSubmission、去重摘要和唯一父引用。
4. 不同幂等键并发消费同一父任务时，数据库唯一约束决定唯一赢家；失败方映射稳定错误
   `clarification.parent_already_consumed`，且不得调度 Worker 或调用 Gateway。

`TaskStore.create_clarification_child(...)` 是 lifecycle 创建的窄变体；它不保存 ClarificationRecord、
会话历史或消费状态。PostgreSQL 实现在任务事务中读取不可变渠道 owner 绑定；内存实现用同一共享锁
提供相同语义。渠道绑定自身失败仍沿用现有服务端幂等恢复，不扩大该事务边界。

## 7. ClarificationRecord 真相源

### 7.1 Subject

```text
ClarificationSubject =
  RouteSubject(kind = "route")
  | CapabilitySubject(
      kind = "capability",
      capability_id,
      capability_version,
      operation,
      input_schema_ref,
    )
```

`RouteSubject` 不携带 capability 字段，其 confirmed slots 恒为空。

`CapabilitySubject` 只能由获胜 `CandidateSet` 中经 `select_entry()` 选定的 Candidate 与当前 Binding
构造。四个身份字段用于解释下一轮补槽上下文，不是授权、候选真源或直达 Planner 的捷径。

子任务必须重新检查：父子授权作用域；当前 Registry 中完全相同的 capability/version/operation；
父 subject、CapabilitySpec、CapabilityInputBinding 与 Params 的 `input_schema_ref` 一致；重新运行 Router
和 Resolver；新 Resolver 的选定 Candidate 与父 subject 完全相同。删除、升版、operation 改变、模型
换能力或 schema 漂移均返回 `interaction.clarification_subject_incompatible`，Planner/Gateway 为零，
并提示创建新根任务。

### 7.2 显式判别 value 联合

```text
ConfirmedTextValue:
- kind = "text"
- text: StrictStr，最多 512 个 Unicode code point、最多 2048 UTF-8 bytes

ConfirmedTimeRangeValue:
- kind = "time_range"
- start_utc: AwareDatetime
- end_utc: AwareDatetime
- timezone_id: "UTC" | "Asia/Shanghai"（I1 受控 IANA 闭集）

ConfirmedSlot:
- field: ClarificationField
- value: ConfirmedTextValue | ConfirmedTimeRangeValue（按 kind 判别）
```

I1 的 `ClarificationField → value kind` 闭集：

| ClarificationField | value kind |
| --- | --- |
| `time_range` | `time_range` |
| `database` | `text` |
| `user_name` | `text` |
| `query_id` | `text` |
| `alert_name` | `text` |
| `instance` | `text` |
| `fingerprint` | `text` |
| `asset_id` | `text` |
| `hostname` | `text` |
| `ip` | `text` |

`environment_id`、tenant、actor、permissions、policy revision、SQL、prompt、log 和任意自由文本不属于
`ClarificationField`。

资产能力缺少精确 selector 时，`reason_code=capability.asset_selector_required`，`missing_fields` 按固定
顺序给出 `asset_id, hostname, ip`，语义是“三选一”，不是三项全部必填。这个语义由封闭 reason code
固定，不新增通用 one-of 表达式或动态字段 DSL。

### 7.3 文本与 secret-shaped 规则

Confirmed text 只保存经过字段专属 parser/normalizer 验证的短业务标量。进入 confirmed snapshot、
ClarificationRecord 或 Planner 前必须完成：

- 长度与 UTF-8 大小校验；
- 字段专属字符形状、别名与 NFC/IP/hostname 等规范化；
- 调用既有 `scrub_text(value)`；若净化前后不相等，直接拒绝持久化；
- 禁止把脱敏后的字符串冒充 confirmed value；
- 禁止日志、prompt、SQL、网页段落、附件正文或任意自由文本。

原始 `IntentDraft.slots` 始终是不可信候选。Resolver 可以读取它生成 CandidateSet，但这不会把任何值
升级为 confirmed。

### 7.4 时间范围

- 语义固定为半开区间 `[start_utc, end_utc)`；
- `start_utc < end_utc`；
- 两个时间点都必须显式规范化为 `tzinfo=UTC`；仅“带时区”但 offset 非零不算 canonical；
- `timezone_id` 只记录用户表达的解释/展示时区，不改变保存的绝对时刻；
- 相对时间按表达实际出现的那个 Task 的 `submission.as_of` 归一化；
- 一旦写入父 ClarificationRecord，子任务继承绝对 UTC 范围，绝不按子任务 `as_of` 重算；
- 最终 Params 和 Plan 继续使用相同绝对区间。

现有 `AwareDatetime` 只能证明有 offset，不能证明 UTC，必须新增专用 validator；同时把 Prometheus
参数 docstring 的 `[start, end]` 修正为 `[start, end)`。

### 7.5 完整累计快照

`ClarificationRecord.confirmed_slots` 是本轮所有可继承、已确定性确认槽位的完整累计快照，不是增量：

- 按 `field.value` 字典序排列；
- 字段不得重复；
- 非规范顺序或重复直接拒绝，不能静默排序/覆盖；
- 父子变化按两个完整快照计算，不新增 `SlotChange` 持久化类型；
- 只继承父 snapshot 中当前 binding 仍允许且重新校验通过的值；
- 模型推测、父原文、外部内容、环境和权限不继承。

### 7.6 Store

```text
ClarificationCandidate:
- subject
- reason_code
- missing_fields
- confirmed_slots

ClarificationRecord:
- record_version = 1
- task_id                 # Store 生成
- subject
- reason_code
- missing_fields
- confirmed_slots
- created_at              # Store 时钟生成
- fencing_token           # 当前 grant 生成
```

`ClarificationRecordStore` 是独立、极窄端口：`load(task_id)` 与 `save(grant, candidate)`。`save()` 是
grant-fenced、insert-once：无有效 grant 拒绝；相同内容重放返回原记录；同一 task 不同内容返回稳定冲突。
`load(task_id)` 返回 `ClarificationRecord | None`；“没有记录”本身不是 Store 异常，但 TaskView 在
`CLARIFICATION_REQUIRED` 终态下读到 `None` 必须判定为完整性错误。

持久化顺序只能是：

```text
load 既有 record
→ 无则在有效 grant 下 save
→ TaskStore transition(CLARIFICATION_REQUIRED)
```

保存后、终态前崩溃，恢复 attempt 读同一 record 后完成终态；禁止先终态后保存。数据库用 `task_id`
作主键/外键，`record_version=1` check，不增加 consumed 字段、触发器或跨 Store 通用事务框架。

`RenderPayload` 只能从 record 投影。任务状态是 `CLARIFICATION_REQUIRED` 但 record 缺失/损坏时，返回
确定性的完整性错误，不从 `terminal_reason` 或原始用户文本临时拼问题。

## 8. SlotVerifier 与 capability 输入

### 8.1 capability input 与 operation arguments 分层

```python
class CapabilitySpec(Contract):
    capability_id: StrictStr
    version: StrictStr
    input_schema_ref: StrictStr
    operations: tuple[OperationSpec, ...]


class CapabilityParams(Contract):
    INPUT_SCHEMA_REF: ClassVar[str]
```

`input_schema_ref` 描述 Resolver 后、Planner 前的整个 capability 业务输入；
`OperationSpec.argument_schema_ref` 描述 Planner 后某个 PlanStep 的工具参数。二者互不比较、复制或替代。

### 8.2 类型化 Binding

```python
@dataclass(frozen=True)
class CapabilityInputBinding(Generic[ParamsT]):
    params_type: type[ParamsT]
    input_schema_ref: str
    allowed_clarification_fields: frozenset[ClarificationField]
    slot_verifier: SlotVerifier[ParamsT]
    planner: CapabilityPlanner[ParamsT]
    confirmed_slot_projector: ConfirmedSlotProjector | None
```

Registry 启动时验证：

```text
CapabilitySpec.input_schema_ref
== CapabilityInputBinding.input_schema_ref
== params_type.INPUT_SCHEMA_REF
```

同时验证 verifier/planner 泛型在构造处配对、entry operation 正确、允许字段闭集合法；允许产生澄清的
binding 若缺 `confirmed_slot_projector`，启动装配失败。`input_schema_ref` 只是身份标识，绝不靠字符串
解析允许字段；唯一允许字段声明就是 binding 的 `allowed_clarification_fields`。

Python 泛型只提供静态配对，Runtime 仍必须精确检查：

```python
if type(result.params) is not binding.params_type:
    raise CapabilityBindingError("slot verifier returned an unexpected params type")
```

不使用 `isinstance()`，不反射函数注解。类型不匹配是服务端装配故障，不伪装成用户拒绝；Planner 和
Gateway 均为零，错误文本不携槽位值。

### 8.3 Verifier 结果

```text
SlotVerificationResult[ParamsT]
  = SlotReady(params: ParamsT, confirmed_slots: tuple[ConfirmedSlot, ...])
  | SlotIncomplete(reason_code, missing_fields, confirmed_slots)
  | SlotInvalid(reason_code)
```

全部使用 frozen dataclass。Planner 只能接收专属不可变 Params，不再接收 `IntentDraft` 或普通 dict。
Target resolver 只接收 `RequestContext` 与可信 Params。

Verifier 是确定性可信升级的唯一入口：

1. 从本轮用户明确文本做字段专属解析；
2. 父 confirmed snapshot 逐项按当前 schema 重验；
3. 模型 slot 只用于提出候选，必须有本轮文本或父 snapshot 的确定性依据；
4. 应用固定默认值并按本轮 `submission.as_of` 归一化相对时间；
5. 生成专属 Params 与 canonical confirmed snapshot。

优先级固定：

| 来源 | 能否成为 confirmed |
| --- | --- |
| 本轮明确文本，经确定性解析 | 可以，最高优先级 |
| 父 `confirmed_slots`，重验通过 | 可以继承 |
| 模型提取值 | 不可以单独确认，只是候选 |
| 日志、附件、网页、引用 | 不可以 |
| 身份、环境、权限、策略 | 不属于槽位 |

父值处理：本轮未提及则重验继承；明确同值则保持；明确唯一新值则采用新值并在最终披露中显示从旧值
改为新值；含糊或多值冲突则 clarify。变化由父 snapshot 与最终 Plan/Target 投影即时计算，不持久化
`SlotChange`。

### 8.4 三个现有 binding

| capability | Params | `input_schema_ref` | allowed clarification fields |
| --- | --- | --- | --- |
| `starrocks.slow_query.diagnose@1.0.0` | `SlowQueryParams` | `input.starrocks.slow_query.v1` | time_range, database, user_name, query_id |
| `prometheus.alert.evidence@1.0.0` | `PrometheusAlertParams` | `input.prometheus.alert.v1` | time_range, alert_name, instance, fingerprint |
| `asset.inventory.lookup@1.0.0` | `AssetLookupParams` | `input.asset.lookup.v1` | asset_id, hostname, ip |

这些 Params 改为继承 `CapabilityParams`。原 `_prepare_*` 中的提取、默认、规范化、目标和编译职责拆成
Verifier、target resolver、Planner 三段；不能保留旧 Planner 兜底。

### 8.5 最终投影一致性

`confirmed_slot_projector(plan, target)` 是纯函数，从已保存 Plan/Target 重建 canonical 最终槽位投影，
用于执行披露与父子变化展示。它不读取 IntentDraft、父原文、模型 artifact 或数据库。

- projector 输出字段必须是 binding allowlist 子集，并遵循相同 value union、UTC 与排序规则；
- 父/本轮 confirmed snapshot 中的每一项必须在最终投影中存在且同值；
- projector 可以额外包含确定性默认值（例如默认 30 分钟窗口），但这些值必须标为系统采用的范围，
  不能伪称为用户确认；
- 不一致、字段无法安全反投影或 projector 抛错时，披露就绪失败，Admission/Gateway 为零。

## 9. 只读分类与 Policy

### 9.1 静态 ReadClass

```python
class ReadClass(StrEnum):
    BOUNDED = "bounded"
    RESTRICTED = "restricted"
```

唯一真源链：

```text
OperationSpec.read_class
→ build_plan_step() 自动派生
→ PlanStep.read_class
→ plan canonicalization / plan_hash
→ StepAdmission 从当前 CapabilitySnapshot 重新派生核对
→ ToolPolicy 使用重新派生值判定
```

交叉约束：`effect_class == READ` 时 read_class 必填；非 READ 时必须为 `None`。调用者不能给
`build_plan_step()` 传 read_class。Admission 不信 PlanStep 自带值。

`PolicyProfile.allowed_read_classes` 同样为闭集：profile 允许 READ 时不能为空；不允许 READ 时必须空。
I1 三个现有只读 profile 只允许 `(ReadClass.BOUNDED,)`，新增稳定原因
`policy.read_class_not_allowed`。

### 9.2 I1 restricted 语义

- 可以通过缩小时间、字段、数据量或目标范围改用一个**另行声明为 BOUNDED 的 operation**：使用
  RouteSubject `clarify`，要求用户提交完整的缩小请求并重新 Resolver；
- 权限不足、策略禁止，或澄清后仍 restricted：`refuse`；
- 降级并通过 ToolPolicy 前 Gateway 为零；
- 不复用写操作 ApprovalGate；I1 不实现“受限读取额外确认”。

`ReadClass` 是 operation 静态声明，不能因同一 operation 的参数变小而动态从 RESTRICTED 改成 BOUNDED，
也不能让 CapabilitySubject 子任务切换 operation。I1 当前三个真实注册能力全部是 BOUNDED，且不注册可
转换的 RESTRICTED operation；因此当前生产 Registry 遇到 RESTRICTED 时只会 refuse。上述 clarify 规则
只冻结未来“已有独立 bounded operation”时的安全语义，不为它提前建设映射框架。

大批量导出、敏感字段/凭据/个人信息、跨租户/环境、无边界全表扫描、明显负载/费用、受合规限制的
审计数据，即使 SQL 是 SELECT 也不能进入 bounded 快速通道。I1 只留下 fail-closed 契约；真正查询
导出能力另立 operation、ADR 和授权流程。

计划级只读结论即时从步骤计算：全是 READ+BOUNDED 才是 bounded；任一 READ+RESTRICTED 为
restricted；存在非 READ 则按副作用工作流。`ExecutionPlan` 不新增可漂移的汇总 read_class。

### 9.3 bounded read 与副作用确认

明确、范围受限、低风险的 `READ + BOUNDED` 请求在首次 Gateway 调用前完成披露后直接继续，不要求
用户再次点击确认。用户原请求表达继续意图，但不构成权限授权；是否允许仍由已认证 Context、Target、
Plan 与 ToolPolicy 决定，“只读”只来自 Capability/Plan 声明与 Admission 重派生。

任何副作用步骤继续进入正式 ApprovalGate。审批必须绑定已认证审批人、tenant、environment、
capability id/version、plan hash、target fingerprint、具体步骤、有效期和防重放标识；恢复时重新解析身份、
目标与策略并重算 hash/fingerprint，任一不一致都拒绝。聊天文本“确认/可以/继续”不能构造审批事实。

### 9.4 Plan schema V2

- `PlanStep.read_class` 纳入 `STEP_FIELD_TO_HASH_KEY`；
- canonical payload 显式序列化 enum value 或 `None`；
- `PLAN_SCHEMA_VERSION` 从 1 升为 2；
- 更新 golden vectors、hash coverage、审批绑定、恢复和篡改测试；
- 已持久化 V1 Plan 不得补默认值、重算 hash 或复用旧审批；Runtime/Runner 读取后 fail-closed，并要求
  新建任务。

## 10. 执行披露就绪屏障

这不是渠道送达屏障、用户确认或审批，也不是新任务状态。顺序固定：

```text
PlanStore 成功保存不可变 Plan/Target
→ 纯函数生成并校验 ExecutionDisclosure
→ DISCLOSURE / OK 审计事件成功持久化
→ StepAdmission
→ 第一次 ToolGateway 调用
```

`PipelineStage` 新增 `DISCLOSURE`。Plan 保存、投影、校验或审计写入失败/结果未知时，当前 attempt 直接
失败并走既有 Worker 恢复；Admission/Gateway 为零，不新增暂停状态机。

`TaskView` 新增独立 `disclosure` 字段，不复用终态 `RenderPayload`。只要已保存 Plan/Target，非终态和
终态都能在鉴权通过时查询披露。PlanStore 仍只保存 Plan/Target，不新增 DisclosureStore 或摘要表。

`ExecutionDisclosure` 至少包含：

- capability id/version；
- environment id、provider、resource kind 与有界 resource ids；
- 是否纯只读、计划级 bounded/restricted/side-effect disposition；
- 规范化查询/时间范围和其他 projector 输出字段；
- 若父任务存在，确定性计算的旧值→新值展示；
- `external_target_access=true`：表示计划包含经 Gateway 访问受管外部目标的步骤。

最后一项是“计划语义”，不是当前 adapter 已真实联网的证据。fake/recording 测试只能证明屏障和投影
逻辑，不能把该字段升级为真实连接、送达或用户已看到。对外准确措辞是：

> 执行披露所依赖的事实已经持久化；在服务可用且鉴权通过时，可由 TaskView 查询。

不能声称 Web/飞书已送达、用户已阅读或理解。披露事件按有效 attempt 记录 `attempt_number`；崩溃恢复
可能留下多条，属于合法审计历史，不为 exactly-once 新增锁或唯一约束。副作用步骤仍必须正式审批。

Runtime 在调用 Runner 前加载并重新验证可选父 ClarificationRecord，把不可变父 snapshot 作为本 attempt
的披露上下文传给 Runner；Runner 不自行查询 ClarificationRecordStore。崩溃恢复会重新加载父记录。核心
执行事实仍只从已保存 Plan/Target 投影，父 record 只用于核对继承值和计算用户可见的旧值→新值。

## 11. 渠道与投影

所有入口只解析协议、认证与显式父引用，然后调用统一 Runtime：

- API：请求体可带 `clarification_parent_task_id`；
- CLI：显式 `--clarification-parent-task-id`；
- Web：仅 `CLARIFICATION_REQUIRED` 详情页显示“补充信息”，提交隐藏字段携父 ID；
- 飞书：只接受由澄清卡片动作或显式任务引用产生的父 ID；不得按“最近一条消息”隐式猜父任务；
- 离线 `handle()`：改为复用 create/attempt/execute/project 主路径，不在创建 Task 前单独解释/规划。

渠道可以决定如何携带显式引用，但不能自行做关键词分类、补槽、环境判断、候选选择、风险判断或执行。
不支持交互卡片动作的飞书部署必须提示用户用显式任务引用或创建包含完整参数的新根任务，不能隐式绑定。

`TaskView` 的澄清文案从 ClarificationRecord 投影，披露从 Plan/Target 投影，终态能力结果从
Plan/Evidence 投影；三者不相互冒充真相源。

## 12. 持久化迁移

I1 预计新增/修改：

1. `task_submissions.parent_task_id` → 删除；新增 nullable
   `clarification_parent_task_id`、同表 FK、普通索引与非空唯一约束。
2. 新表 `task_clarification_records`，`task_id` PK/FK，record JSON/列、version/fencing check。
3. `task_accepted_intents` 物理表重命名为 `task_interaction_artifacts`，新写入 artifact version 2；旧 V1
   行保留为不可执行历史，不被新 Runtime 解释或升级。活动任务命中 V1 时 fail-closed。
4. 旧 parent 非空数据检测失败时整次 migration 回滚；清理/保留策略必须另行确认。

不用数据库 trigger 跨表强制任务状态，也不建设跨 Store 通用事务框架。唯一消费依靠现有关系的唯一
约束；record 一致性由 Store、grant 和恢复测试承重。

## 13. 稳定错误与计数口径

I1 至少冻结以下公开 reason code：

```text
interaction.route_not_available
interaction.kind_ambiguous
interaction.environment_assertion_unclear
interaction.environment_context_mismatch
interaction.capability_draft_forbidden
interaction.capability_draft_missing
interaction.clarification_subject_incompatible
capability.fields_missing
capability.fields_ambiguous
capability.fields_invalid
capability.asset_selector_required
clarification.parent_already_consumed
clarification.record_conflict
clarification.integrity_error
policy.read_class_not_allowed
plan.schema_version_unsupported
disclosure.projection_failed
```

错误信息不得包含用户槽位值、父 task 是否存在、环境是否存在、Provider 原文或 secret-shaped 输入。

环境不一致的无故障验收路径：Task=1、初始 Worker attempt=1、classifier=0/1、获胜 artifact 行=1、
Router=1、最终 `REJECTED`；Resolver/Planner/Admission/Gateway/ClarificationRecord=0，业务重试=0。

跨故障不声称 Worker 或 Router exactly-once。每个 attempt 最多一次 Router；崩溃/消息重投/lease 恢复
可产生新 attempt，但无论多少次，环境不一致的 Resolver/Planner/Admission/Gateway/ClarificationRecord
总调用都为零，最终只能提交 REJECTED。

## 14. 验收矩阵

### 14.1 交互与模型

- 单个 attempt 最多一次 classifier Port 调用；rule fallback 调用为零。
- Provider 返回后在 artifact 保存前/后分别注入崩溃，验证 at-least-once 与持久化 winner。
- grant 丢失、save 冲突/失败时 Router 和下游全零。
- 非 capability 携 draft、capability 缺 draft均 fail-closed。
- confidence 从 0 到 1 都不能突破结构、环境或授权边界。
- 模型伪造 provider/model/usage 不进入持久化元数据。
- 所有真实/离线入口共用 Router；架构测试禁止 handler 关键词路由。

### 14.2 澄清链

- route clarify：Resolver/Plan/Gateway=0，record 先于终态。
- capability incomplete：Resolver 可运行，Planner/Admission/Gateway=0。
- 普通 SUCCEEDED/FAILED/REJECTED/CANCELED/INDETERMINATE 不能作为澄清父任务。
- 同 key 并发两次返回同子任务；不同 key 并发同父一胜一
  `clarification.parent_already_consumed`。
- 失败事务回滚后父任务仍可消费；失败方 Worker/Gateway=0。
- 子任务再次 clarify 可成为下一轮父任务。
- PostgreSQL 真实唯一约束测试，不只靠 FakeStore。
- 跨租户、环境、actor、channel owner 全部统一 not-found；环境切换只能新根任务。
- 旧 parent 非空 migration fail-closed；不存在静默改义。

### 14.3 confirmed slots / Params

- value union 判别严格；time field+text value、text field+time value均拒绝。
- UTC canonical、半开区间、start<end、受控 timezone、父绝对时间不重算。
- 非规范排序、重复 field、任意 dict、自由文本、SQL/log/prompt、secret-shaped 值全部拒绝。
- `scrub_text` 前后不同即拒绝，不能保存 scrub 后值。
- 模型自补 slot 不能确认；父 slot 与当前 schema 重验；本轮明确修正优先并披露。
- incomplete/invalid 时 Planner/Gateway=0；ready 时 Planner 只收到精确 Params 类型。
- Registry 缺 verifier/projector、schema ref 漂移、类型不匹配均在 Planner 前失败。
- capability input schema 可编译到多个不同 operation argument schema，二者不相等仍正常。

### 14.4 ReadClass / disclosure

- OperationSpec/PlanStep/read profile 交叉约束与重新派生核对。
- `RESTRICTED + SELECT` 在上层不能走 bounded，在强行绕过时仍由 Admission 拒绝，Gateway=0。
- read_class 纳入 plan hash；V1 Plan/旧审批不能复用。
- bounded read 的披露 OK 事件严格先于首次 Admission/Gateway。
- 缺环境或时间范围时 Gateway=0；用户把写能力称为只读仍进 ApprovalGate。
- 聊天“确认”不能恢复写任务；审批身份、target、plan hash/fingerprint 任一变化恢复失败。
- projector 缺失/不一致、披露 audit 写失败时 Admission/Gateway=0。
- TaskView 可查询披露但不宣称渠道送达。

## 15. Eval 与证据等级

I1 新增离线 L0/L1 测试集：

- L0：提示注入、环境欺骗、伪只读、restricted SELECT、澄清父链越权、secret-shaped slots、模型元数据
  伪造、外部日志指令。
- L1：同义表达、错别字、四类/unknown 分流、必要澄清、显式补槽、多轮一对一链、父值修改与歧义。

固定安全不变量（Gateway=0、Resolver=0 等）使用绝对断言；分类质量阈值必须先收集真实模型样本再在 I5
设定，I1 不凭空设置准确率魔数。

证据必须分开：源码、单元/契约/安全、PostgreSQL integration、Compose、真实模型、真实渠道、部署、
灰度、用户验收。I1 离线全绿不能写成真实模型或真实用户体验已通过。

## 16. 明确非目标

- I2 的限定领域普通对话；
- I3 的受治理资料查询与来源引用；
- I4 的用户提供日志分析；
- 主动连接 Loki、ELK、Kubernetes、服务器或数据库获取日志；
- 敏感读取/批量导出的额外授权流程；
- M8 受控写能力本身；
- 普通聊天长期记忆或 ConversationStore；
- 动态字段注册、capability 专属持久化联合表、SlotChange 表；
- 多人会签、通用审批平台、动态风险引擎、Policy DSL、事件总线；
- ReAct、多 Agent、自主跨能力规划、微服务或 LangGraph 接管 TaskStore。

## 17. 完成定义

I0 完成必须有：ADR-017 与四份真相文档一致、链接/契约表/命令检查通过、Claude/Codex 基于精确 SHA
复审、项目负责人批准；没有这些只能叫“设计稿完成”。

I1 完成必须有：四个代码 PR 逐个合入；每个 PR 的相关 TDD、契约、安全测试通过；最终候选 SHA 上
完整执行项目四条基线；PostgreSQL 真实验证唯一消费；Compose 离线闭环通过；handoff 分开记录已验证、
未验证和残余风险。真实模型、真实飞书、真实目标、部署、灰度与用户验收仍按各自现有 GO 门管理。
