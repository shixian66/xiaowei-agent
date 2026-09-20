# ADR-017: 智能交互入口、澄清链与执行披露边界

> 状态：Proposed for I0-DOC（2026-09-15）。本文合入后才成为 I1 实施门；当前不代表 I1
> 已实现、已运行或已连接任何真实服务。

## 背景

现有小维已经有受治理的 capability 执行主干，但入口仍围绕“用户已经在请求某个 capability”展开。
普通对话、资料查询、用户粘贴日志分析和不完整运维请求都可能在进入 Resolver 前缺少一个统一、
可审计、fail-closed 的交互门厅。

I0 只冻结文档真相；I1 才实现代码。I1 的目标不是扩大模型执行权，而是在
`CapabilityResolver` 前增加确定性分流、必要澄清、可信槽位升级、静态只读分类和执行披露屏障。

## 决策

稳定主链调整为：

```text
RequestEnvelope
  → TaskStore.submit
  → Worker attempt
  → load-or-create AcceptedInteractionArtifact
  → DeterministicInteractionRouter
       ├─ conversation / knowledge_lookup / log_analysis
       │    → I1 初始：REJECTED(interaction.route_not_available)
       │    → I2-A 修订：conversation → RESPOND / SUCCEEDED；knowledge_lookup、log_analysis 仍拒绝
       │    → I2-B 修订：该回复的内容是 CapabilitySnapshot 的确定性投影（能力目录），不是固定文案
       ├─ unknown / route unclear
       │    → ClarificationRecordStore.save
       │    → CLARIFICATION_REQUIRED
       └─ capability_request + proceed
            → CapabilityResolver
            → select_entry()
            → SlotVerifier
                 ├─ incomplete → ClarificationRecordStore.save → CLARIFICATION_REQUIRED
                 ├─ invalid    → REJECTED
                 └─ ready      → CapabilityInputBinding.params_type
                                → Planner
                                → PlanStore.save / load
                                → ExecutionDisclosure audit barrier
                                → WorkflowRunner / StepAdmission / ToolGateway
                                → Evidence / Outcome
```

`InteractionKind` 回答“用户在做什么”，闭集为 `conversation`、`knowledge_lookup`、
`log_analysis`、`capability_request`、`unknown`。`RoutingDisposition` 回答“本轮系统怎么办”，
I1 初始闭集为 `proceed`、`clarify`、`refuse`；I2-A 增加 `respond`，仅用于限定领域普通对话的
无工具固定回复。两个维度不能合并成一个宽枚举。

LLM 只能产生 `InteractionDraft` 和可选 `IntentDraft` 候选。Provider、model、origin、usage、
prompt/schema revision、input digest、result digest 与 fencing 信息均由 adapter/application 生成；
模型响应不能自报这些运行元数据。`confidence` 只用于观测和 eval，不能成为执行通行证。

## 状态语义

新增任务终态 `CLARIFICATION_REQUIRED`：

- terminal = true；
- resumable = false；
- plan_created = false；
- Gateway calls = 0；
- 只允许从 `CREATED` 或 `PLANNING` 进入；
- 进入后无出边，Runner 不领取该终态任务。

澄清不是暂停恢复，不新增 `AWAITING_CLARIFICATION`。只有副作用审批继续使用既有
`AWAITING_APPROVAL` 暂停/恢复机制。用户补充信息时创建新 Task，并通过
`clarification_parent_task_id` 显式关联父任务；每个子任务都重新鉴权、分类、路由、Resolver、
`SlotVerifier`、Policy 与计划编译。

## 澄清父链

I1 删除通用 `parent_task_id` 语义，不允许同名字段换义。新字段只叫
`clarification_parent_task_id`，表示“本任务消费一个 `CLARIFICATION_REQUIRED` 父任务”。普通
conversation history、任意终态继续、最近消息猜测、provider chat/session 和 TaskStore 中的通用会话
记忆都不属于该链。I2 若需要普通聊天记忆，必须用独立 `ConversationStore` 重新设计。

父任务消费是一对一、单次消费：

```text
一个 CLARIFICATION_REQUIRED 父任务
  → 最多一个直接子任务
  → 子任务若再次 CLARIFICATION_REQUIRED，可成为下一轮父任务
```

唯一消费事实源是数据库对非空 `clarification_parent_task_id` 的唯一约束，不增加 `consumed` 布尔、
消费表、触发器或额外状态机。并发消费同一父任务时，数据库约束决定赢家；失败方稳定映射为
`clarification.parent_already_consumed`，且不得调度 Worker 或调用 Gateway。

父子创建必须在同一任务创建事务中校验：幂等键、父任务存在且为 `CLARIFICATION_REQUIRED`、tenant、
environment、actor、channel 与 channel owner 一致。不存在、未授权与不应暴露的消费状态统一返回
not-found 形状，不能泄漏父任务是否存在或已被消费。

## ClarificationRecord

`ClarificationRecordStore` 是独立窄端口，只提供 `load(task_id)` 与 `save(grant, candidate)`。
`save()` 必须 grant-fenced、insert-once：无有效 grant 拒绝；同内容重放返回 winner；同 task 不同内容
返回 `clarification.record_conflict`。

持久化顺序固定为：

```text
load existing record
  → save missing record under valid grant
  → TaskStore transition(CLARIFICATION_REQUIRED)
```

禁止先终态后保存。保存后、终态前崩溃时，恢复 attempt 读回同一 record 并完成终态。
`CLARIFICATION_REQUIRED` 终态下 `load()` 返回空或损坏记录时，TaskView 返回
`clarification.integrity_error`，不得从 `terminal_reason`、原始用户文本或模型输出临时拼 prompt。

`ClarificationSubject` 是显式判别联合：`RouteSubject` 不带 capability 字段，confirmed slots 恒为空；
`CapabilitySubject` 只能由获胜 `CandidateSet` 中经 `select_entry()` 选定的 candidate 与当前
`CapabilityInputBinding` 构造。子任务必须重新验证当前 Registry 中 capability/version/operation 和
`input_schema_ref` 仍一致；删除、升版、operation 改变、schema 漂移或模型换能力都返回
`interaction.clarification_subject_incompatible`，Planner/Gateway 为零。

## 可信槽位

`confirmed_slots` 是完整累计快照，不是增量。它按 `field.value` 字典序排序、字段不得重复，并只保存
确定性确认过的短业务标量。

槽位值使用判别联合：

- `ConfirmedTextValue(kind="text", text=...)`：最多 512 个 Unicode code point、最多 2048 UTF-8 bytes；
- `ConfirmedTimeRangeValue(kind="time_range", start_utc, end_utc, timezone_id)`：半开区间
  `[start_utc, end_utc)`，`start_utc < end_utc`，两个端点必须 canonical UTC。

I1 的澄清字段闭集只包含业务 selector，例如 `time_range`、`database`、`user_name`、`query_id`、
`alert_name`、`instance`、`fingerprint`、`asset_id`、`hostname`、`ip`。`environment_id`、tenant、
actor、permissions、policy revision、SQL、prompt、log、附件正文和任意自由文本都不是
`ClarificationField`。

所有 text confirmed value 在进入 snapshot、record 或 Planner 前必须完成字段专属 parse/normalize、
长度/UTF-8 校验、NFC/IP/hostname 等规范化，并调用既有 `scrub_text(value)`。若净化前后不相等，
直接拒绝；禁止把脱敏后的字符串冒充 confirmed value。模型 slot 始终是不可信候选，不能单独升级为
confirmed。

相对时间按表达实际出现的 Task 的 `submission.as_of` 归一化；一旦写入父
`ClarificationRecord`，子任务继承绝对 UTC 范围，不能按子任务 `as_of` 重算。

## Capability 输入分层

I1 明确区分 capability input 与 operation arguments：

- `CapabilitySpec.input_schema_ref` 描述 Resolver 后、Planner 前的业务输入；
- `OperationSpec.argument_schema_ref` 描述 Planner 后某个 step 的工具参数；
- 二者互不比较、复制或替代。

`CapabilityInputBinding` 是唯一把 capability 声明、`SlotVerifier`、专属 params、planner 和
confirmed slot projector 绑定起来的运行配置。启动时必须验证：

```text
CapabilitySpec.input_schema_ref
== CapabilityInputBinding.input_schema_ref
== params_type.INPUT_SCHEMA_REF
```

`SlotVerifier` 是不可信 slot 升级为可信 `CapabilityParams` 的唯一入口。Planner 只能接收专属不可变
Params，不能继续接 `IntentDraft`、普通 dict 或模型 slots。Runtime 必须精确检查
`type(result.params) is binding.params_type`；类型不匹配是服务端装配故障，不伪装成用户输入错误。

## ReadClass 与 Policy

新增静态只读分类 `ReadClass`，闭集为 `BOUNDED` 与 `RESTRICTED`。唯一真源链为：

```text
OperationSpec.read_class
  → build_plan_step() 派生
  → PlanStep.read_class
  → plan canonicalization / plan_hash
  → StepAdmission 从当前 CapabilitySnapshot 重新派生核对
  → ToolPolicy 判定
```

`effect_class == READ` 时 `read_class` 必填；非 READ 时必须为 `None`。调用者不能给
`build_plan_step()` 传 `read_class`，Admission 不能信任 PlanStep 自带值。`PLAN_SCHEMA_VERSION` 已在
I1-C 升为 2，`PlanStep.read_class` 进入 hash coverage；已持久化 V1 Plan 不补默认值、不重算 hash、
不复用旧审批，读取后 fail-closed 并要求新建任务。

I1 不实现“受限读取额外确认”。`RESTRICTED` 读取不能复用 `ApprovalGate`；可缩小为有界读取的场景，
必须依赖另行声明为 `BOUNDED` 的 operation，并要求用户提交完整新请求重新 Resolver。当前三个既有
只读能力均应保持 `BOUNDED`；大批量导出、敏感字段、个人信息、跨租户/环境、无界扫描或明显负载费用
风险即使是 SELECT，也不能进入 bounded 快速通道。

## 执行披露屏障

`ExecutionDisclosure` 是首次 ToolGateway 调用前的就绪屏障，不是渠道送达、用户确认、审批或新任务
状态。start 与 resume 走同一屏障：

```text
start:  PlanStore.save → PlanStore.load
resume: PlanStore.load → drift verification
  → 得到本 attempt 唯一 StoredPlan
  → 纯函数生成并校验 ExecutionDisclosure
  → DISCLOSURE / OK 审计事件成功持久化
  → StepAdmission
  → 第一次 ToolGateway 调用
```

I1-D 实现时必须同步 trace contract：新增 `PipelineStage.DISCLOSURE`，错误归因阶段从
当前 RI3 后的十个扩展为十一个阶段。该阶段只表示披露事实与 `DISCLOSURE / OK` 审计事件已
成功持久化，不表示渠道送达、用户已读、审批通过或真实目标已经联网。

屏障位于 Runner 的 `_start`/`_resume` 汇聚后、`_run_steps` 入口，不能只放在 `_start`。
投影只读取 PlanStore 重新读回的 `StoredPlan`，不得读取调用方传入的 plan/target。Plan 保存/读取、
投影、校验或审计写入失败/结果未知时，Admission/Gateway 均为零，当前 attempt 按既有 Worker 恢复。

Runtime 每次调用 Runner `start/resume` 前加载并验证可选父 `ClarificationRecord`，把不可变父 snapshot
作为本 attempt 的披露上下文传给 Runner。Runner 的依赖中不得出现 `ClarificationRecordStore`；崩溃恢复
由 Runtime 重新加载父记录。

`ExecutionDisclosure` 至少包含 capability id/version、environment id、provider、resource kind、有界
resource ids、是否纯只读、计划级 bounded/restricted/side-effect disposition、规范化查询/时间范围、
可投影 confirmed slots、父子旧值到新值变化，以及 `external_target_access=true`。最后一项表示计划
语义上会经 Gateway 访问受管外部目标，不是 fake/recording 已真实联网的证据。

## TaskView 与渠道

`TaskView` 使用三个独立字段：`render: RenderPayload | None`、
`clarification: ClarificationPayload | None`、`disclosure: ExecutionDisclosure | None`。presence
invariant 固定为：

- 非终态：`render` 与 `clarification` 均为空；
- `CLARIFICATION_REQUIRED`：`clarification` 必须非空，`render` 必须为空；
- 其余终态：`render` 必须非空，`clarification` 必须为空；
- `disclosure` 与终态正交，只取决于 PlanStore 是否已有可读取且可安全投影的 Plan/Target；无 Plan 的
  路由澄清、能力补槽澄清和 pre-plan rejection 必须为空。

入口只解析协议、认证和显式父引用，然后调用统一 Runtime。Web 只在
`CLARIFICATION_REQUIRED` 详情页展示补充表单；飞书不得按“最近一条消息”隐式猜父任务；API/CLI 必须显式
携带 `clarification_parent_task_id`。渠道不得自行关键词分类、补槽、环境判断、候选选择、风险判断或
执行。

## 模型调用与 trace

新分类事件使用 `ModelCallKind.INTERACTION`，`ModelCallObservation.request_count` 对单个 attempt 严格为
`0..1`。历史 `ModelCallKind.INTENT` 只读兼容旧事件 `0..2`，新 Runtime 不得再发出 `INTENT`。
禁止把 observation 字段级上限全局降为 1，因为会破坏已持久化旧事件读取。

旧 `IntentModelPort.generate_intent()`、第二次 intent 模型调用和
`ModelArtifactStore.load_intent/save_intent` 均淘汰，不保留双轨运行真源。Provider 调用是
at-least-once：同一 application attempt 最多一次 port 调用；provider 返回后、artifact 保存前崩溃时，
恢复 attempt 可再次调用；artifact 保存后，恢复 attempt 必须零模型调用并使用已存 winner。

任务取消、进程关闭、grant/lease 丢失、save 冲突或持久化失败不得被 rule fallback 吞掉；此时 Router、
Resolver、Planner、Admission 和 Gateway 均为零。

## Reason Code 归属

clarify 只能写入 `ClarificationRecord.reason_code`：

- `interaction.kind_ambiguous`
- `interaction.environment_assertion_unclear`
- `capability.fields_missing`
- `capability.fields_ambiguous`
- `capability.asset_selector_required`

pre-plan rejection 使用独立拒绝域：

- `interaction.route_not_available`
- `interaction.environment_context_mismatch`
- `interaction.capability_draft_forbidden`
- `interaction.capability_draft_missing`
- `interaction.clarification_subject_incompatible`
- `capability.fields_invalid`

I2-A 的普通对话不是 pre-plan rejection：它使用 `RoutingDisposition.RESPOND`，任务以
`SUCCEEDED / interaction.conversation_responded` 终结，但 Plan、Disclosure、Admission、Gateway 与
Evidence 均为零。该结果只说明系统返回了限定领域普通回复，不表示资料查询、日志分析、
真实模型、真实渠道、真实目标、部署、canary 或用户验收已经开放。

I2-B 把这条回复的**内容**定死为 `CapabilitySnapshot` 的确定性投影：逐条列出已注册能力、
它们的操作、`read_class`、`effect_class` 与所经 gateway，`refs` 带 `capability-snapshot:<id>` 与
`capability:<id>@<version>`。三条约束不可放宽：

1. **回答只由快照决定，不接受用户文本。** 投影函数的签名里没有用户文本这个入参——这是签名
   层面的保证，不是"我们检查过输出里没有"。一个不调用工具但让用户文本参与生成的通道，就是
   一个可被注入的自由问答口。
2. **投影的是当前快照，不是任务创建时的快照。** "你能做什么"问的是此刻的事实；这与证据投影
   必须钉死在已读到的东西上正好相反。`refs` 里的 `snapshot_id` 让读者永远能分辨这份回答出自
   哪一份声明，所以快照演进不会产生"看不出来变过"的旧回答。
3. **只复述声明，不加解释性断言。** 目录不得声称能力已部署、已验收或可写；能力状态仍最强为
   `tests`。空快照必须明说"没有任何已注册能力"，不得只渲染一个空列表。

能力目录与 `docs/CAPABILITIES.md` 同源于 Registry 快照，因此两者不会互相漂移。

Store/Policy/Plan/Disclosure 继续使用各自封闭错误域，例如 `clarification.parent_already_consumed`、
`clarification.record_conflict`、`clarification.integrity_error`、
`policy.read_class_not_allowed`、`plan.schema_version_unsupported`、`disclosure.projection_failed`。
这些错误不得混入澄清或 pre-plan rejection 枚举，也不得包含槽位值、父 task 是否存在、环境是否存在、
provider 原文或 secret-shaped 输入。

## 迁移

I1-A 的持久化演进已拆成顺序 revision，而不是回改已发布 revision：

1. `0011_interaction_clarification` 将 `task_accepted_intents` 物理表重命名为
   `task_interaction_artifacts`；新写入 artifact version 2；旧 V1 行只作为不可执行历史，活动任务命中
   V1 时 fail-closed。
2. `0012_clarification_records` 新增 `task_clarification_records`，以 `task_id` 为 PK/FK，包含 record
   version、record payload/列和 fencing check。
3. `0013_clarification_parent` 将 `task_submissions.parent_task_id` rename 为 nullable
   `clarification_parent_task_id`，新增同表 FK 与非空唯一约束；唯一约束本身就是消费查找索引，不再额外建
   一条重复普通索引。
4. `0013` upgrade 在 rename 前必须检查旧 `parent_task_id IS NOT NULL`。命中即整次 migration 回滚，不能
   静默解释为澄清关系。
5. downgrade 的 I1 数据丢失检查复用既有 `require_destructive_authorization`。默认返回
   `DESTRUCTIVE_DOWNGRADE_REJECTED`；只有显式 destructive authorization 才能删除 I1-only 澄清记录、
   父引用和 V2 artifact。upgrade 前置检查与 destructive downgrade guard 语义不同，不能新造第二套
   授权开关。

## 明确不采用

- 不把 `parent_task_id` 原地改成澄清父任务。
- 不新增 `AWAITING_CLARIFICATION`。
- 不把 conversation history、父原文、历史 `RenderPayload` 或 Evidence 偷渡进 TaskStore。
- 不让 Planner 继续消费 `IntentDraft` 或普通 dict。
- 不用 `ApprovalGate` 给 `RESTRICTED` read 做“额外确认”。
- 不新增 `DisclosureStore`、渠道送达 ACK 或用户已读语义。
- 不做动态字段注册、动态风险 DSL、Policy DSL、ReAct、多 Agent、自主跨能力规划、事件总线或
  LangGraph 接管安全链。
- 不把 fake/recording 披露写成真实模型、真实渠道、真实目标、部署、canary 或用户验收证据。

## 后果

I1 会打断当前“直接从意图草稿进入 Resolver/Planner”的入口形状，换来更窄的执行前门和更清楚的
证据等级。代价是需要 migration、TaskView presence invariant、Plan schema V2、额外契约测试与
PostgreSQL 唯一约束测试。

I0 合入后，I1 仍必须按独立 PR 顺序实现。任何想改变本 ADR 中的状态、字段名、reason code 归属、
model trace、migration revision、`ReadClass` 语义、披露屏障位置或 TaskView invariant 的变更，都必须先
修订本文，并重新跑相应契约/安全测试。
