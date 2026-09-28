# F1 StarRocks 受治理只读查询与锁定预览设计

> 状态：**Draft v9（Agent 主链集成版）**，待 exact-SHA 独立复审与负责人接受。
> v7 已由 PR #108 批准并合并（`91cadd43e13a4b1178fbddcbdfd4b41bf0decb36`，复审 SHA
> `e2727cf117b62f9c9a23ec1ce37c046312475f5a`）；v8 精简版见 `21d4e64`。负责人 2026-09-27 决定：最小化实现、不过度设计；
> SQL 查询必须作为小维 agent 的一个能力接入统一对话主链，不做旁路；飞书与网页都可以直接发 SQL 执行；
> 不设默认目标，目标不确定时反问、不执行；取消配额。本文范围命名为 **F1-Core 查询执行内核**（完成定义见 §12.1）。
> 本文只写现行决定。
> 日期：2026-09-27。本文不授权源码、migration、真实 StarRocks 调用、部署、canary、UAT、F2、F3、F1-NL 或 E1。

相关真源：

- [项目协作与安全规则](../../../AGENTS.md)
- [项目架构](../../../ARCHITECTURE.md)
- [开发路线](../../../DEVELOPMENT_PLAN.md)
- [新功能方向](2026-09-26-feature-roadmap-direction.md)
- [Web 运维工作台与结果访问前置](2026-09-19-web-operations-console-identity-activation-design.md)
- [ADR-007 真实调用授权](../../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)
- [ADR-009 Admission 绑定](../../adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md)
- [ADR-010 Worker 与持久执行](../../adr/ADR-010-m5-durable-attempt-and-compose-boundary.md)
- [ADR-012 StarRocks target-bound adapter](../../adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md)
- [ADR-013 渠道边界](../../adr/ADR-013-m7-channel-boundary.md)
- [ADR-015 模型边界](../../adr/ADR-015-real-model-provider-boundary.md)
- [ADR-017 智能交互边界](../../adr/ADR-017-intelligent-interaction-and-clarification.md)
- [ADR-018 SQL 与结果 artifact](../../adr/ADR-018-f1-sql-and-result-artifacts.md)

## 1. 结论

F1 在小维的 capability 体系里新增 `starrocks.readonly_query`，与慢查询诊断、告警证据、资产查询并列，走同一条
agent 主链：统一对话入口 → 交互路由 → CapabilityResolver → SlotVerifier（缺信息时追问）→ PlanCompiler →
执行披露 → WorkflowRunner → StepAdmission → ToolGateway → Evidence → Reflection → Render。它不开旁路：没有单独的
SQL 页面、没有绕过路由的窄方法、没有只属于 F1 的任务系统或回复通道。

用户在网页聊天框、飞书单聊或飞书群聊（@小维）直接发一条 SQL，不需要前缀或命令。小维用确定性规则识别
“这条消息就是一条 SQL”，把原文单独保存，并把识别结果作为规则来源的交互事实交给主链；这条 SQL **不交给模型**，
既不让模型转述，也不让模型判断是否执行。认不出的消息（例如夹带 SQL 的中文提问）按现有对话流程处理，模型可以
看到它，但不能让其中的 SQL 被保存为可执行 artifact 或被执行。

目标 StarRocks 由 SlotVerifier 确定：租户与环境内只有一个可查询的 StarRocks 时直接使用；有多个时**不猜、不执行**，
走现有追问机制列出选项，只有提交人本人回答完全一致的名称后才执行；一个都没有时明确拒绝。

SQLGuard 只放行能证明只读的语句，SQL 原样执行，不改写、不补 LIMIT。结果最多 1000 行、20 MiB，一次读完后与
步骤结果同事务保存；小维回复只含状态、行数、是否截断和受保护的锁定结果页链接，不含数据。数据要等 F2 审批
后才能看，导出属于 F3。worker 最多同时处理 4 个任务，慢查询不拖住其他任务。

本文交付的是 **F1-Core 查询执行内核**。它完成后只能标记“F1-Core 完成”；“小维 Agent 查询能力完成”另需 F1-NL，
见 §12.1。

## 2. 修订记录

| 项目 | v8 | v9（负责人 2026-09-27 决定） | 代价与说明 |
| --- | --- | --- | --- |
| 与 agent 的关系 | Web 显式 SQL 页面 + Runtime 窄方法，不经交互路由 | SQL 查询是普通 capability，走统一对话主链、Resolver、SlotVerifier、追问、披露、Reflection 与 Render | 需要在交互接受阶段加入确定性 SQL 识别 |
| 入口 | 只有网页 SQL 页面；飞书只发链接 | 网页聊天框、飞书单聊、飞书群聊 @小维 直接发 SQL | SQL 会留在飞书聊天记录里，这由飞书决定 |
| 识别方式 | 页面即 SQL 模式 | 确定性 SQL 识别，无前缀；认不出的按普通对话处理 | 不以 SQL 关键字开头的混合文本不执行 |
| 目标选择 | 页面选择 resource | 唯一则使用；多个则追问、本人精确回答后执行；无默认目标 | 多集群时多一轮问答 |
| 配额 | 每人 5 / 每库 50 | 取消，只靠 24 小时过期清理 | 高频查询时存储短期上升 |
| 结果记录 | 提交时建 `pending` 占位 | 步骤成功提交时才建结果与访问授权 | 失败查询没有结果页，失败原因在回复与任务详情中 |
| 连接前置校验 | 不做 digest 比对 | 不变；负责人确认不做权限校验码与版本区间检查 | 权限漂移由 DBA 管控，F1-H 人工核验 |

v8 中保留的决定：不做 target 排队锁；SQL 上限 65_536 bytes；结果一次读完再存；代码内只读语句清单；worker 同时
处理 4 个任务；F1 查询人要等 F2 审批才能看数据。v7/v8 评审中确认且继续保留：SQL 不进 RequestEnvelope、ToolCall
或 Evidence；ToolCall 只放标量，SQL bytes 只在进程内 HydratedQuery；同名列按 ordinal 保存；SSCursor 截断时直接
关闭连接；hint 按 token 规则拒绝；RESTRICTED read 只在获批 profile 下免审批；Web §11.2 六项逐项对照。

## 3. 产品范围

### 3.1 F1 范围

- capability `starrocks.readonly_query@1.0.0`，唯一 operation `execute_readonly_query`；
- 入口：网页聊天框、飞书单聊、飞书群聊（@小维）；
- 单条 SQL，最长 65_536 bytes UTF-8；
- SELECT、CTE、JOIN、子查询、UNION、INTERSECT、EXCEPT、聚合、窗口函数和内建标量函数；
- sqlglot 可解析的 SHOW、DESC/DESCRIBE、EXPLAIN；代码清单登记的其他只读语句（§7.3）；
- target 内部 Catalog 的数据库、系统库、表、视图和物化视图；
- 目标不唯一时追问，网页与飞书都支持作答；
- 有界预览、锁定结果页、ACL、24 小时保留和清理；
- 每个 target 可由 Web Admin 调低预览上限并维护关系黑名单（F1-2）；
- 查询、截断、超时、目标和数据处置审计。

### 3.2 明确不做

- 让模型识别、转述、改写、补全或选择要执行的 SQL；自然语言生成 SQL 属 F1-NL；
- 默认目标、名称模糊匹配或按上下文猜测目标；
- DDL、DML、CALL、SET、USE、事务、临时表脚本、变量赋值或多语句；
- SELECT FOR UPDATE、INTO OUTFILE、文件读取、导入、导出或任何持久状态修改；
- 外部 Catalog、外部表、table function、UNNEST、UDF 和存储过程；
- 任何 optimizer、resource、session 或 version comment hint；
- SQL 重写、自动 LIMIT、自动修复、自动重试或自动 KILL QUERY；
- target 排队锁、配额、单独的 SQL 页面；
- 在聊天回复或飞书卡片里展示结果数据；F2 结果查看审批；F3 导出；
- TiDB、跨 target 查询、定时查询、查询缓存和快照一致性承诺。

## 4. 不可破坏的不变量

1. 识别为纯 SQL 的原文只在 SqlArtifactStore 保存一份；TaskStore、Plan、Evidence、RenderPayload、日志、trace、
   ChannelStore 与交互事实只保存 `sql_ref`、hash 或安全摘要；
2. SQL 识别、SQLGuard、ToolCall hash 与 adapter 执行绑定同一份原始 UTF-8 bytes；
3. 纯 SQL 消息、SqlArtifact 和最终执行字节不进入模型；混合对话可以进入模型。模型候选不能直接执行，只有完整展示、用户确认并绑定 hash 后，才能生成新的 SqlArtifact。F1-Core 中模型来源不得产生 `starrocks_readonly_query` 意图、
   SqlArtifact 或执行，Gateway 调用为 0；检测到嵌入 SQL 的消息以固定文案拒绝执行（§5.1）；模型不能决定是否执行、在哪执行或执行什么；
4. 目标不唯一时不执行；只有提交人本人对追问给出与选项完全一致的回答才继续；
5. 缺少、越权、hash 不符或 target 不符的 SQL 一律在 Gateway 前拒绝；operation 需要 SQL 时缺少 HydratedQuery 必须拒绝；
6. 结果行不进入 AdapterResponse.payload、ToolResult.data_view、Evidence、RenderPayload 或 TaskOutcome；
7. 列按 ordinal 保存，同名列原样保留；
8. F1 查询一旦开始尝试，不自动重放；
9. 结果行只在步骤成功提交的同一事务中写入并可读；
10. Web 配置只在 task-worker 重启后加载，不热加载；
11. StarRocks credential 的对象权限是授权真源；关系黑名单只能缩小、不能扩大权限面；
12. target、黑名单或配置 revision 漂移必须在 Gateway 前拒绝旧计划；
13. adapter 永远收到用户提交的原始 SQL，不执行 Guard 重建、格式化或改写的文本；
14. F1 任何真实 target 调用仍需独立现场 GO。

## 5. Agent 主链

### 5.1 统一入口与确定性 SQL 识别

网页聊天框与飞书沿用现有渠道：Web handler、飞书 listener 只做协议解析、鉴权上下文与群聊 @ 校验，把文本交给
application 层 ChannelSubmissionService，不在入口判断业务。渠道文本上限从 8192 字符放宽到 65_536 bytes，
非 SQL 的普通消息仍受现有 8192 字符上限约束，超出时按“消息过长”拒绝。

ChannelSubmissionService 在创建任务前调用纯函数 `recognize_sql_message(text)`：

1. 去掉首尾空白；若整条消息恰好是一个 fenced code block，只取块内正文；
2. 首个 token 属于闭集 SQL 语句关键字（读：SELECT、WITH、SHOW、DESC、DESCRIBE、EXPLAIN、ADMIN、ANALYZE；写与会话：
   INSERT、UPDATE、DELETE、MERGE、REPLACE、CREATE、DROP、ALTER、TRUNCATE、GRANT、REVOKE、SET、USE、KILL、LOAD、
   EXPORT、SUBMIT、CANCEL、BEGIN、COMMIT、ROLLBACK 等，精确闭集写在代码中）；
3. 通过 §7.2 token 扫描后恰好是一条完整语句：sqlglot 30.17.0 完整解析，或命中 §7.3 只读语句清单；

三条同时满足才是 SQL 消息。写语句也会被识别，目的是明确回复“只允许只读查询”，而不是当成聊天。其他消息
（例如“show 一下昨天的慢查询”“帮我看看这条 SQL 为什么慢：SELECT …”）都不是 SQL 消息，不会被当作 SQL 执行。识别是
确定性规则，不调用模型，同一输入永远得到同一结论。

**嵌入 SQL 不执行。**不是 SQL 消息、但 `contains_embedded_sql(text)` 为真的消息，以固定文案拒绝，不执行任何
capability。候选片段是每个 fenced code block 的正文，以及正文外每个以闭集 SQL 语句关键字（词边界、不分大小写）
开头、到该段落末尾的文本；只有首个 token 属于闭集语句关键字且能被 sqlglot 完整解析为非 `Command` 语句的片段才算
嵌入 SQL。代码块只是载体，Python、日志、YAML 等代码块不满足该条件，不受影响。命中时 `route_interaction` 不看草案
来源——模型或现有关键词规则（例如含“慢SQL”被规则识别为慢查询诊断）——直接返回 `REFUSE` 与
`InteractionRejectionReasonCode.EMBEDDED_SQL_NOT_EXECUTED`，不进入 CapabilityResolver，Gateway 调用为 0；任务
`REJECTED`，`terminal_reason` 保存该码；`TaskViewRuntime.project_recorded` 把 `record.terminal_reason` 传给
`render_preplan_rejection`，回复为确定性文案“检测到消息中包含 SQL，本轮未执行；需要执行请单独发送这条 SQL。”。F1-Core 不做 SQL 解释，不新增模型端口；
SQL 解释与慢因诊断属 F1-NL。

这条检测是产品护栏，不是安全边界：漏判时消息按现有流程处理，现有 capability 只执行自己的模板 SQL。“用户粘贴的
SQL 只有作为纯 SQL 消息被识别后才可能执行”这一保证由 §5.1 识别与 `origin=rule` 约束承担，不依赖本检测。

识别为 SQL 后，服务调用 `TaskStore.submit_sql_query`，在**同一个 PostgreSQL 事务**内写 SqlArtifact（原始 bytes、
SHA-256、requester、tenant、environment、created_at）并创建 task 与 `ArtifactSubmission`；幂等键沿用渠道现有派生规则。
之后的渠道绑定、投影与回复与其他任务完全相同，ChannelStore 不保存 SQL。

### 5.2 TaskSubmission 不保存 SQL

`TaskSubmission` 升级为以 `input_kind` 判别的 union。现有类显式改名为 `ConversationSubmission`，全部构造点同步
更新；新增 `ArtifactSubmission`。除与 ConversationSubmission 共有的 context、as_of 外，ArtifactSubmission 只含
`input_kind=sql_artifact`、`sql_ref`、`sql_hash`，不含 SQL 原文、目标或结果引用。旧行由 migration 回填
`input_kind='conversation'`，对话提交的 digest 字节不变；精确 schema、digest 与 downgrade 规则以 ADR-018 D2 为准。

### 5.3 交互接受：规则来源，零模型调用

Runtime 仍由 `load_or_accept_interaction` 接受交互事实。对 `ArtifactSubmission`，它不构造模型请求，直接保存规则来源
（`origin=rule`）的 `AcceptedInteractionArtifact`：`proposed_kind=CAPABILITY_REQUEST`，capability draft 为
`starrocks_readonly_query` 意图、槽位为空。随后 `route_interaction`、CapabilityResolver 与其他能力完全相同。
`ModelCallObservation` 记录 `request_count=0`，trace 与审计照常。

### 5.4 SlotVerifier 确定目标，不确定就追问

F1 的 `CapabilityInputBinding` 提供 SlotVerifier，输入为当前 task 的 `sql_ref`/`sql_hash`、RequestContext、已加载的
F1 target 目录和可选的澄清上下文：

| 情况 | 结果 |
| --- | --- |
| 租户与环境内启用的 F1 target 恰好 1 个 | `SlotReady`，使用它 |
| 0 个 | `SlotInvalid`，回复“当前环境没有可查询的 StarRocks”，不执行 |
| 多个且没有澄清回答 | `SlotIncomplete`，reason `CAPABILITY_TARGET_SELECTION_REQUIRED`；ClarificationRecord 保存 `sql_ref`、`sql_hash` 与目标选项（resource_id + 展示名） |
| 多个且澄清回答与某个选项展示名完全一致 | `SlotReady`，使用该选项 |
| 回答不完全一致 | 再次 `SlotIncomplete`，重新列出选项，不猜 |

追问复用现有 `ClarificationRecord`、`CLARIFICATION_REQUIRED` 与 `create_clarification_child`，不新增状态或存储。
澄清子任务由规则解释回答文本，不调用模型；它从父记录取得 `sql_ref`/`sql_hash`，自身仍是普通
`ConversationSubmission`，回答文本只是目标名称。只有父任务的提交人本人可以作答（现有 owner 校验）。

作答方式：

- 网页：沿用现有澄清父任务提交；
- 飞书：**引用回复**小维发出的追问消息。listener 读取飞书事件的父消息 id，ChannelSubmissionService 通过
  ChannelStore 已记录的小维消息 id 找到父任务，再走同一 owner 与状态校验。没有引用回复的消息按新消息处理，
  不会被当作回答。

SQL 创建后 24 小时内未作答即过期（§9.4），回答时提示“已过期，请重新发送 SQL”。

### 5.5 Plan 只保存引用

PlanCompiler 接收不可变 ReadonlyQueryParams，把 `sql_ref`、`sql_hash`、`resource_id`、config revision、
`preview_max_rows`、`preview_max_bytes`、`query_timeout_seconds` 写入单一步骤 typed_arguments。ExecutionPlan 不含 SQL
原文；PlanBudget 固定 `max_steps=1`、`max_tool_calls=1`、`max_model_tokens=0`；以上进入 plan_hash。F1 为
READ + RESTRICTED，无副作用步骤，不调用 ApprovalGate。执行前照常写 ExecutionDisclosure（能力、目标展示名、只读）。

### 5.6 从引用到唯一 ToolCall

OperationSpec 增加受信 `query_requirement`，闭集为 `none`、`template_locked`、`confirmed_artifact`，由 CapabilitySnapshot
派生。`ToolCall.typed_args` 仍只接受 JSON 标量。`confirmed_artifact` 的 Runner 流程：

1. `begin_step_attempt` 返回 `PROCEED` 后，重新解析当前 target、policy 与 config revision；
2. 从 SqlArtifactStore 读取 `sql_ref` 一次，校验 actor、tenant、environment 与 SHA-256，构造不可变 HydratedQuery；
3. 构造只含标量的 ToolCall（引用、hash、target_fingerprint、config_revision 与三个预算值）；
4. StepAdmission 先 ToolPolicy，再按 requirement 执行 `confirmed_readonly` SQLGuard（在 `asyncio.to_thread` 中运行），
   并校验引用、预算与 bytes hash 一致；
5. `tool_call_hash` 覆盖完整标量 ToolCall；
6. Gateway 重算 hash，再次校验 HydratedQuery，把同一 bytes 交给 target-bound adapter；adapter 发送前最后一次计算 SHA-256。

| 载体 | 可含 SQL bytes | 约束 |
| --- | --- | --- |
| `ToolCall.typed_args` | 否 | 引用、hash、target_fingerprint、config_revision 与预算标量 |
| `HydratedQuery` | 是 | 仅进程内；SHA-256 必须等于 `sql_hash`；只作为 StepAdmission 与 Gateway 的受信 keyword-only 参数 |

required query 缺失、字段多余、hash 不符或 artifact 无法读取都闭集拒绝，Gateway 调用次数为 0。trace/audit 不序列化
HydratedQuery 或结果缓冲，也不回显 SQL 片段。`template_locked` 慢查询保持原行为。

### 5.7 结果一次读完，随步骤结果同事务提交

Runner 为 `confirmed_artifact` 步骤创建进程内 `QueryResultBuffer`，作为 Gateway 的受信 keyword-only 参数传给 adapter：

- adapter 写入有序 `ColumnSpec(ordinal, name, type)` 与按 ordinal 对齐的行；
- buffer 执行 1000 行 / 20 MiB 上限：第 1001 行只置 `has_more`，下一完整行会超字节时置 `truncated_bytes`，不存半行；
- Gateway 超时或取消时关闭 buffer，之后迟到写入被丢弃；
- AdapterResponse.payload 与 ToolResult.data_view 必须为空。

步骤成功时 Runner 先生成 CSPRNG `result_ref`，把同一个值写入 Evidence 与 `StepCommitCommand`，再把冻结后的 buffer
交给 `TaskStore.commit_step_result`；Store 在提交步骤结果、Evidence 与审计的**同一事务**里以该 `result_ref` 写入结果行与
`requester_owner` 授权（`result_ref` 唯一约束冲突则整个事务失败）。步骤失败、超时或提交前崩溃时不产生结果记录。
Evidence 只记录 `result_ref`、hash、行/字节计数、完整性与资源指标。

### 5.8 Reflection 与回复

Reflection 用现有 `AnswerabilityVerdict` 标出限制（例如“结果已截断”“仅返回前 1000 行”）。Render 生成的回复只含：

- 成功：目标展示名、返回行数、是否截断、耗时，以及锁定结果页链接和“数据需 F2 审批后查看”；
- 失败：闭集原因与建议——不是只读语句、暂未支持的语句、命中黑名单、目标不唯一或未配置、超时、StarRocks 权限
  不足、StarRocks 语法或执行错误（只给错误类别与 StarRocks 错误码，不回显上游错误正文）、SQL 已过期、SQL 无法读取；
- 嵌入 SQL：“检测到消息中包含 SQL，本轮未执行；需要执行请单独发送这条 SQL。”

回复不含 SQL 原文、列名或数据。模型 advisory 不参与 F1，因为它需要看到 SQL 或结果。

## 6. target 配置

F1 复用 W4 的 `StarRocksResource`，task-worker 启动时加载一次，新增：

- `blocked_relation_names`：默认空；每项是内部 Catalog 中精确的 `database.object`（表、视图或物化视图），不接受
  catalog 前缀、通配符、正则或单段名；规范化后冲突、非法或重复则 target 启动失败；
- F1 查询上限（§8.2）；
- F1 是否启用；
- 展示名沿用资源已有 `display_name`，同一租户与环境内 F1 启用资源的展示名必须唯一，否则启动失败（追问依赖它精确匹配）。

账号口径：DBA 配置和批准的 F1 专用内部 Catalog 跨库只读账号，只授予预期对象 SELECT，无写、管理、UDF、外部 Catalog
和文件权限。黑名单只在权限面内额外拒绝；强隔离必须由 DBA 撤销对应表和视图权限。

配置保存后显示 `restart_required`，task-worker 重启后生效；host、账号、黑名单、上限、展示名或启用状态变化都改变
config revision，旧计划在 Admission 前因漂移拒绝。F1 不做 version/grants/DDL/identity digest 比对，也不绑定 StarRocks
版本区间（负责人已确认）。

## 7. SQLGuard 与 ToolPolicy

### 7.1 一个内核，两个 profile

`governance/sqlguard.py` 仍是唯一 SQL 准入内核：`template_locked` 服务现有慢查询，行为不变；`confirmed_readonly`
服务 F1，校验原文 hash、单语句、只读证明、内部对象与黑名单，不重编译、不改写。StepAdmission 只能从
CapabilitySnapshot 取得 profile；慢查询计划伪装成 `confirmed_readonly` 必须拒绝。§5.1 的 SQL 识别复用同一 token
扫描与解析，但识别只决定“是不是 SQL 消息”，是否放行只由 StepAdmission 中的 SQLGuard 决定。

### 7.2 原文 token 扫描

AST 前先做一遍 quote-aware 扫描，识别语句边界、注释类别与每个 token 在原始 bytes 中的位置，不用正则做安全判断：

- 字符串、quoted identifier 和普通注释内的分号不算多语句；
- 普通 `--`、`#` 和不以 `+`/`!` 开头的块注释允许；字符串外出现 `/*+` 或 `/*!` 拒绝；
- 控制字符、Unicode format 字符与 BOM 拒绝；
- 中文和全角字符可出现在字符串与普通注释内；smart quote、全角标点出现在关键字、运算符或 identifier 位置拒绝；
- 字符串、quoted identifier 和注释外的 `INTO OUTFILE` 拒绝。

### 7.3 只读证明

两条互斥路径都产生同一种 `ReadonlyProof`，再统一执行内部 Catalog、黑名单、副作用与原文 hash 校验。

**AST 路径（sqlglot 30.17.0 完整解析时）：**恰好一条语句，且只允许：

1. 查询根 `Select`、`Union`、`Intersect`、`Except`（含 CTE、JOIN、子查询、聚合、窗口函数）；
2. parser 完整识别且无写入/会话副作用的 SHOW（如 SHOW DATABASES、SHOW TABLES、SHOW COLUMNS、SHOW CREATE）；
3. DESC/DESCRIBE 内部对象；
4. EXPLAIN 与 EXPLAIN ANALYZE 包装的内层查询递归通过同一规则；写形状（如 EXPLAIN ANALYZE INSERT）拒绝；
5. 一段、两段 `db.object`，以及 catalog 为 `default_catalog` 的三段名；
6. information_schema、`sys`、`_statistics_` 等内部系统库读取，只要 credential 有权限且未命中黑名单。

其他根节点（例如 `HELP` 被解析成的 `Alias`）拒绝。

**清单路径（sqlglot 降级为 `Command` 或 ParseError 时）：**只匹配代码内只读语句清单。每条清单项写明 token 形状、
允许与禁止的 clause、直接对象的唯一提取规则和 Catalog 规则，不用“以 SHOW 开头”放行。清单随代码版本走；升级
StarRocks 大版本时人工核对清单。首批清单与实施计划 §1 一致（A1–A12、B1–B9）。清单外语句返回
`error_code=READONLY_STATEMENT_NOT_SUPPORTED`、用户文案“暂未支持”、`retryable=false`，零 SQL 发送。EXPLAIN
LOGICAL/VERBOSE/COSTS 只用 token 位置切出内层查询字节交 AST 路径证明，执行的仍是原始 SQL。

登记支持不等于给账号增权：SHOW COMPUTE NODES、SHOW ROLES、SHOW ROUTINE LOAD、ADMIN SHOW REPLICA 等需要额外权限
的语句，专用只读账号收到 StarRocks 权限错误是预期行为，按结构化执行错误返回，不为跑通而增权。

**拒绝：**DML、DDL、写入、锁、文件、导出、事务、session/变量改写、动态 SQL、procedure 等副作用；清单外语句；
清单语法不完整、歧义或无法唯一提取直接对象；多语句、hint、INTO OUTFILE；任何位置引用外部 Catalog；SELECT、
EXPLAIN 查询、DESC、SHOW CREATE、SHOW COLUMNS 等直接对象读取精确命中黑名单；table function、UNNEST、
qualified function。以上拒绝都在 adapter 之前，零 SQL 发送。

一段名按 target 的 default database 解析；CTE alias 和 derived table 不当基础关系。规范化后的 `database.object`
与黑名单精确命中即拒绝，不做前缀、通配或正则匹配。SHOW DATABASES 与 SHOW TABLES 可以列出黑名单对象的名称。
黑名单**不提供元数据保密**：有权限时仍可能经 `information_schema.columns`、`information_schema.views`、SHOW PROFILELIST
或 SHOW PROC 看到列名、视图定义、查询文本或集群信息；SQLGuard 也不展开视图定义。需要隐藏或强隔离时由
StarRocks 权限与安全视图承重。未限定名称的内建函数可以使用；UDF 边界由拒绝 qualified/table function 加专用
账号无 UDF 权限共同承重。

### 7.4 LIMIT 语义

SQLGuard 不要求也不追加 LIMIT。返回上限由本次连接的 `sql_select_limit` 与结果 buffer 双重限制，只约束返回与保存，
不约束扫描、JOIN、排序、服务端内存或 CPU。

## 8. 资源上限、worker 并发和连接

### 8.1 系统硬上限

| 项目 | F1 硬上限 | 说明 |
| --- | ---: | --- |
| 保存预览行 | 1000 | 第 1001 行仅作 has_more 哨兵 |
| 保存预览字节 | 20 MiB | 按规范编码后的完整行累计 |
| server query timeout | 180 秒 | |
| SQL 语句数 | 1 | 不接受脚本 |
| SQL 原文 | 65_536 bytes UTF-8 | 普通消息仍为 8192 字符 |
| worker 同时处理任务数 | 4 | 所有 capability 共用；同一 StarRocks 最多同时 4 条 F1 查询 |

Web 不能调高硬上限。

### 8.2 Admin 可调值

每个 target 可调低：`preview_max_rows` 1..1000（默认 1000）；`preview_max_bytes` 1 MiB..20 MiB（默认 20 MiB）；
`query_timeout_seconds` 1..180（默认 180）。黑名单维护见 §6；删除条目需二次确认，因为它扩大可查询面。保存后
`restart_required`，重启 task-worker 生效。

### 8.3 timeout 层次

设有效 query timeout 为 Q：connect/write ≤ 10 秒且不大于 read；session `query_timeout=Q`（Q ≤ 180）；PyMySQL
`read_timeout=Q+10`；Gateway timeout `Q+20`；ToolCall ≤ 300 秒。慢查询旧 profile 保持现有上限。客户端超时不能证明
服务端已停止，服务端残留窗口由 F1-H 实测。

### 8.4 worker 有界并发

现有 worker 对一轮取到的任务逐个 await，一条长查询会让同一 worker 的其他任务等待。改为有界并发：

- 新配置 `worker_max_concurrent_tasks`，默认 4，范围 1..16；
- worker 只在有空位时才调用 `begin_task_attempt` 领取任务，领取后立即作为独立 asyncio task 运行，不为等空位持有 lease；
- 每个在途任务沿用现有 `run_with_task_heartbeat`，各自续租；lease、fencing 与终态保护不变；
- 停机时停止领取，等待在途任务在收尾宽限内结束，超时后取消，由现有 lease 过期恢复接管；
- 启动校验数据库连接池容量足够（`db_pool_size + db_pool_max_overflow >= 2 × 并发数 + 1`），不足则启动失败；
  `db_pool_size` 默认值同步由 5 调为 9，`.env.example` 同步为 9，默认配置与示例配置都直接满足并发 4；
- 同步 StarRocks 读取仍在 `asyncio.to_thread` 中运行，默认线程池容量大于并发上限。

这是 worker 级改动，所有 capability 受益；它不改变 dispatch 候选规则、`dispatch_sort_key`、失败计数或基础设施
故障窗口的语义。

### 8.5 会话与读取

每次查询使用独占 connection，不自动重试、不设全局变量：

1. 设置 `query_timeout=Q` 与 `sql_select_limit=preview_max_rows+1`；
2. 执行经 Guard 的原 SQL；
3. 用 PyMySQL SSCursor 和 `fetchmany`（每批最多 100 行）读到 EOF、第 1001 行或字节上限；
4. 正常读到 EOF 时关闭 cursor 和 connection；提前截断、超时或取消时**直接关闭 connection**。PyMySQL 1.2.0 的
   `SSCursor.close()` 会耗尽未读结果，因此截断路径不能使用会隐式调用它的上下文管理器。

SSCursor 只避免在 Python 进程缓存全部结果，不限制服务端内存；单行过大的风险与资源组约束在 F1-H 验证。

## 9. 结果、ACL 和保留

### 9.1 数据形状

结果只在步骤成功时产生。`result_ref` 由 CSPRNG 生成，不可枚举。结果记录保存 task、requester、tenant、environment、
target fingerprint、config revision、`sql_ref`、SQL hash、query id、开始/结束时间、有序
`ColumnSpec(ordinal, name, type)`、行、保存行/字节数、`has_more`、`completeness`（`complete`/`truncated_rows`/
`truncated_bytes`）、created_at、expires_at 与 `export_policy`。行使用带类型标签的规范编码；Decimal、日期时间和大整数
不经 float，非法 UTF-8 与未支持的 binary 类型结构化失败。

### 9.2 ACL

新增 `result_access_grants`：结果写入的同一事务写 `requester_owner` grant；grant 绑定 result_ref、principal、grant_kind、
approval_ref、created_at、expires_at，只有 `requester_owner` 的 approval_ref 可为空。F1 不写 approver grant；Admin 身份
不产生 grant。`export_policy` 在 F1 固定为 `disabled`。

### 9.3 锁定页面

`/results/{result_ref}` 只显示：target 展示名、时间和上限；保存行/字节数、是否截断、过期时间；“详细结果尚未获 F2
查看审批”提示；requester 本人提交的完整 SQL。页面和 API 不返回列名、单元格、数据摘要或导出链接；not-found、过期
和越权使用同一隐藏式拒绝。群聊里其他人点开链接同样被拒绝。

### 9.4 24 小时保留

- 结果：`expires_at = 结果提交时刻 + 24h`，grant 同值；
- SQL 的 `expires_at` 只在下列四个事务里写，一律 `GREATEST(expires_at, 新值)`，只延不缩：
  1. `submit_sql_query` 创建：`now + 24h`；
  2. `create_clarification_child` 消费目标选择澄清：同事务读取并分类父澄清记录中的 `sql_ref`，可用时延到 `now + 24h`；
     失败时不消费父任务、不建子任务，按下文分类回复；
  3. `load_sql_for_execution` 水合：同一事务内 `SELECT … FOR UPDATE` 锁住该 `sql_ref` 行，按下文分类，可用时
     `UPDATE … SET expires_at = GREATEST(expires_at, now + 24h)` 并返回 bytes，覆盖本次有界执行（≤ 300 秒）与提交；
     失败时步骤失败、Gateway 调用为 0、任务 FAILED；
- 取 SQL 的失败由内存与 PostgreSQL 共用的纯函数 `classify_sql_artifact_read` 判定，顺序固定：行不存在 →
  `SqlArtifactUnavailableError`，原因 `NOT_FOUND`；requester、tenant 或 environment 不符 → `SCOPE_MISMATCH`；hash 不符 →
  `HASH_MISMATCH`（闭集 `NOT_FOUND`、`SCOPE_MISMATCH`、`HASH_MISMATCH`，终态码 `sql_artifact.unavailable`，原因只进审计，回复统一为
  “SQL 无法读取，本次未执行”，不泄漏对象是否存在）；以上都通过后，已清除或 `expires_at <= now` → `SqlArtifactExpiredError`
  （终态码 `sql_artifact.expired`，回复“SQL 已过期，请重新发送”）。先判归属再判过期，他人或错绑定的 SQL 不会得到
  “已过期”；
  4. `commit_step_result` 成功提交结果：同事务把 SQL 延到与结果相同的 `expires_at`；
- 任务终态迁移（`TaskStore.transition`）不改 SQL 过期时间。失败任务的 SQL 按水合时写入的时间过期；从未水合的任务
  （未作答、长期排队、卡住或恢复失败）按创建或澄清时写入的时间过期，所以没有 SQL 会永久保存；
- SQL 清除是 tombstone：`UPDATE sql_artifacts SET sql_bytes = NULL, purged_at = now WHERE expires_at <= now AND
  purged_at IS NULL`；约束 `ck_sql_artifacts_purge_shape` 要求 `(purged_at IS NULL) = (sql_bytes IS NOT NULL)`；行保留
  sql_ref、SHA-256、requester、tenant、environment、created_at、expires_at、purged_at，`task_submissions.sql_ref` 不悬空；
- 竞态：清除的 UPDATE 与水合的 `SELECT … FOR UPDATE` 争同一行锁而串行——水合先提交时清除在 READ COMMITTED 下重新
  评估 `expires_at <= now` 并跳过该行；清除先提交时水合读到已清除行，判为 `SqlArtifactExpiredError`；水合后 SQL 至少再保留 24 小时，远大于执行上限，执行中
  不会被清除；
- 过期后读取立即拒绝；retention 删除 SQL bytes、列、行、grant 与活动索引；
- 长期只保留 hash、actor、target、时间、上限、Guard/Policy 决定、query id 与资源指标；
- 日志、trace、TaskStore、Evidence、ChannelStore 不新增 SQL 或结果副本。

PostgreSQL DELETE 不证明物理擦除；飞书聊天记录与 StarRocks 自身的 audit log、query history、Profile 保留口径不归
本系统控制，由 F1-H 向 DBA 与负责人确认，没有证据时不得宣称“SQL 24 小时后从所有系统删除”。

## 10. 不重放与崩溃恢复

F1 不新增恢复状态机，沿用现有 step journal：`begin_step_attempt` 在同一事务里创建 started 并消耗工具预算；
`max_tool_calls=1`，因此已开始但未提交的步骤再次领取时得到 `BUDGET_EXHAUSTED`，任务以 FAILED
（`budget.tool_calls_exhausted`）结束，Gateway 调用次数为 0，不产生结果。回复把这种结局显示为“执行中断，结果未知，
请重新发送 SQL”，不宣称查询已安全停止。已提交的步骤按现有规则 adopt，不重新查询。

用户再发一次同一条 SQL 就是一次新任务，不是自动 retry。取消只关闭客户端连接；服务端终止无法证明时不得伪装为
已安全停止。

## 11. 渠道、结果页和 Web 规格 §11.2 六项

### 11.1 渠道边界

- 网页聊天框、飞书单聊、飞书群聊（@小维）都可以提交 SQL；入口只做协议解析、鉴权与 @ 校验，SQL 识别在 application 层；
- 飞书追问以“引用回复”作答；listener 新增读取父消息 id，ChannelSubmissionService 用 ChannelStore 已有的小维消息记录定位父任务；
- 回复与卡片不含 SQL、列或行；ChannelStore 不保存 SQL；
- 结果链接鉴权先于读取：未登录、未激活、scope 不匹配或不是提交人都拒绝。

### 11.2 RESTRICTED read

F1 operation 是 READ + RESTRICTED。查询不经 ApprovalGate 的条件必须同时满足：CapabilitySnapshot 明确绑定
`confirmed_readonly`；requester 有 `submit_readonly_task`；target、配置、上限和 SQL 全部通过 Admission；无副作用且
`export_policy=disabled`；结果页仍受 requester ACL 锁定。写操作与 F2/F3 审批不受此条影响。

### 11.3 六项逐条对照

| Web §11.2 前置 | F1 设计 | 负责阶段与开放门 |
| --- | --- | --- |
| 1. 稳定 result_ref 和有界 artifact | 成功时生成 CSPRNG result_ref、1000 行/20 MiB | F1-1 契约、migration、故障集成测试通过前不注册结果路由 |
| 2. requester、approver、状态、有效期、导出规则唯一真源 | 结果表 + result_access_grants；F1 只写 requester_owner；export disabled | F1-1 验收 store/ACL；F2/F3 分别验收 approver 写路径 |
| 3. ADR-005 审批语义 | F1 锁定页只向 requester 展示状态与其本人的 SQL，不写 approver grant、不展示列与行、不提供导出，因此不依赖 ADR-005；本条窄化为“任何 approver grant、结果行展示或导出前必须满足 ADR-005” | F1-0 修订 Web 规格 §11.2 第 3 项；F2/F3 开工前必须先建立并批准 ADR-005 |
| 4. ADR-013 深链只投影引用 | 飞书与网页回复只含 result_ref 深链，无 SQL/列/行 | F1-0 修订 ADR-013，安全测试通过后开放 |
| 5. 保留、脱敏、分页、导出和失效 | 24h、隐藏式拒绝、锁定页；分页归 F2，导出归 F3 | F2/F3 未批准前保持锁定/disabled |
| 6. 独立里程碑、计划及真实调用/数据处置授权 | F1 实施计划、F1-H 现场计划、数据处置 GO 分离 | 各自书面 GO 后才能进入对应阶段 |

空页面或 fake rows 不算完成；真实数据页面还必须取得现场 GO。

## 12. 与 F2、F3、F1-NL 的边界

- F2 只读取已保存结果分页展示，翻页不访问 StarRocks；F2 查看审批与 F3 导出审批互不授权；
- F3 审批绑定原 SQL hash、target fingerprint、requester 与有效 `sql_ref`，在新连接中重新执行原 SQL 流式写 CSV；
  不导出 F1 预览，也不继承 F1 的 `sql_select_limit`；F1 与 F3 的数据时间不同，UI 显示两次时间；
- F1-NL 另行设计模型端口、元数据模板、COMMENT 注入防护与候选确认；候选 SQL 经用户确认后，作为一条新的 SQL 消息
  进入本文同一能力与同一安全链，模型仍不决定执行。F1-NL 方向（负责人 2026-09-28 采纳，届时单独设计）：
  - 纯 SQL 仍按本文确定性识别、零模型调用，直接进入 F1-Core；
  - 夹带 SQL 的混合文本可进入 InteractionClassifier；模型可提出候选意图与候选 SQL，但不得创建可执行 SqlArtifact；
    SQL 优先由确定性规则提取（如代码块），规则取不出时才用模型候选；
  - F1-NL 区分“自然语言数据查询”与“混合文本中的 SQL 执行请求”，两者的候选 SQL 都必须完整展示给提交人，确认内容
    按 hash 绑定展示的 SQL，确认后作为新的 SQL 消息进入 F1-Core；未经完整确认 Gateway 调用为 0；
  - “解释这条 SQL”只做解释、不执行；“为什么慢”预留给 SQL 性能诊断，由 F1-NL 单独决定升级现有慢查询能力还是新建
    能力；两者默认都不重新执行用户粘贴的 SQL，也不自动 EXPLAIN；
  - 混合文本中的 SQL 字面值随对话进入模型，属于现有对话出站边界；F1-NL 修订 ADR-015 时一并处理脱敏与提示。

### 12.1 完成定义

“小维 Agent 查询能力完成”必须同时满足：

1. **F1-Core 完成**：本文范围（直接 SQL、目标追问、SQLGuard、执行、锁定结果与报错说明）通过 F1-G 离线验收；
2. **F1-NL 完成**：用户用中文提问，小维生成候选 SQL，用户确认后进入 F1-Core 执行，单独设计、批准与验收。

结果说明按负责人 2026-09-27 决定**只在报错时做**：成功只回复行数、截断与链接，不解释数据；失败按 §5.8 闭集原因
给出原因与下一步建议，由确定性模板生成，属于 F1-Core，不需要模型、不需要 F2，也不修订 ADR-015。若将来要让模型
解读 StarRocks 错误正文或结果数据，须先修订 ADR-015 的出站数据边界并单独批准，不属于上述完成定义。

## 13. 源码前必须修订的真源

F1-0 文档 PR 获批前不写 F1 行为源码。须同步：

1. AGENTS.md 第 5 条：模板 SQL 确定性生成；用户直接 SQL 只来自受保护 SQL artifact，原样执行；
2. ARCHITECTURE.md §4/§6/§7.3/§9/§16：SQL 能力接入统一主链、契约、worker 有界并发、confirmed_readonly；
3. DEVELOPMENT_PLAN.md、路线规格与 AGENT_HANDOFF.md：移除“只接受模板 SQL”“飞书只发链接不执行”的过期口径；
4. Web 规格 §11.2 第 3 项：F1 锁定页不依赖 ADR-005；
5. ADR-007：第四能力与 F1 授权矩阵；
6. ADR-009：query requirement、HydratedQuery、结果 buffer 与 max_tool_calls=1 不重放；
7. ADR-010：worker 有界并发；
8. ADR-012：F1 adapter profile、会话设置、SSCursor 截断与 timeout 层次；
9. ADR-013：网页与飞书可提交 SQL、飞书引用回复作答、渠道文本上限、只投影受保护 result 深链；
10. ADR-017：确定性 SQL 识别与规则来源交互事实、目标选择追问、飞书澄清父链、RESTRICTED read 无审批的窄条件；
11. ADR-018：SQL 与结果 artifact、提交事务、ACL 与保留。

ADR-015 不因直接 SQL 放宽；纯 SQL 消息、SqlArtifact 和最终执行字节不进入模型；混合对话可以进入模型。模型候选不能直接执行，只有完整展示、用户确认并绑定 hash 后，才能生成新的 SqlArtifact。

## 14. 验证与验收

### 14.1 离线行为测试（先红后绿）

- SQL 识别：读/写关键字开头的完整语句识别为 SQL；“show 一下…”“帮我看看这条 SQL …”等识别为对话；整条代码块被解包；
  识别不调用模型；非 SQL 超过 8192 字符拒绝，SQL 超过 65_536 bytes 拒绝；
- SQL 消息的交互事实为规则来源、模型调用 0 次；模型请求构造对 SQL 消息不可达；
- 夹带 SQL 的对话即使模型返回 `starrocks_readonly_query` 意图，也被拒绝为不可执行：不建 SqlArtifact、Gateway 调用为 0；
- 目标：1 个直接执行；0 个拒绝；多个进入 `CLARIFICATION_REQUIRED` 且记录选项；本人精确回答后执行；非精确、他人回答、
  过期回答均不执行；飞书未引用回复的消息不被当作回答；
- required HydratedQuery 缺失、多余或 bytes hash 不符时 Gateway/adapter 调用为 0；
- ToolCall.typed_args 只接受标量；HydratedQuery 与结果 buffer 不进入 Plan、TaskStore、trace、audit 或交互事实；
- 提交事务：同幂等键同内容返回同一 task；事务中任一步注入故障后全部回滚；
- 同名列按 ordinal 往返；结果行不进入 ToolResult.data_view、Evidence、RenderPayload 与 TaskSubmission；
- 1000/1001 行得到 `truncated_rows`，超字节得到 `truncated_bytes`，不存半行；
- 结果与步骤提交同事务；失败、超时或提交前崩溃不产生结果；已开始未提交的步骤再次领取得到 `BUDGET_EXHAUSTED`，
  Gateway 调用为 0；Gateway 超时后迟到的 buffer 写入被丢弃；截断路径不调用 `SSCursor.close()`；
- worker：一个长任务运行时其他任务照常执行；在途数不超过上限；同一任务不被重复领取；停机等待与取消；连接池不足时启动失败；
- SQLGuard：AST 路径与清单路径每项有正常对照、大小写/空白/quoted identifier 边界、缺失或重复 clause、多语句、hint、
  外部 Catalog、黑名单与歧义目标反例；清单外语句返回 `READONLY_STATEMENT_NOT_SUPPORTED` 且零发送；`HELP` 等非允许根拒绝；
- 黑名单表、视图和物化视图经 SELECT、CTE、JOIN、子查询、EXPLAIN、DESC、SHOW CREATE、SHOW COLUMNS 访问均拒绝；
  SHOW TABLES 仍可列名；黑名单不扩大授权；
- DML、DDL、锁、文件、导出、session 改写、外部 Catalog、table function、UNNEST、UDF 与 hint 恶意矩阵；
- 回复：成功与各类失败的文案闭集；回复与卡片不含 SQL、列与行；
- 慢查询 `template_locked` 与现有对话、澄清、披露全部原回归通过；
- requester、Admin、群内其他用户与过期 ACL 正反例；锁定页响应字段集合精确等于 §9.3 闭集；
- 变异：分别去掉 hash 校验、required query、token scan、黑名单、清单目标提取、ACL、同事务结果写入、SQL 消息不进模型、
  模型来源意图不执行的保护，对应测试转红，且确认不是被更早规则遮蔽。

触及 governance、planning、tools 后执行四门：

    python -m pytest -q
    python -m pytest -m security -q
    ruff check .
    mypy src

### 14.2 真实 target 现场门（F1-H）

离线通过不等于真实安全。F1-H 另写现场计划并取得现场 GO，固定 exact SHA、StarRocks/PyMySQL 版本、target、专用账号
grants、资源组、数据处置、时间窗与回退，至少验证：

1. `query_timeout`、`sql_select_limit` 生效与连接关闭；SQL 自带 LIMIT 时的实际优先级；
2. 1000/1001 行、20 MiB 与 180 秒边界；一个复杂 JOIN/CTE/window 成功对照；
3. 高扫描低返回查询受资源组约束；4 条并发 F1 查询对集群的实际影响；
4. 一条 180 秒查询运行时其他任务的端到端延迟（验证 worker 并发）；
5. 取消/driver timeout 后服务端残留查询的最长窗口；
6. 首批清单每条语句在专用账号下的输出形状、所需权限与权限不足错误；清单外语句零发送；
7. 黑名单表/视图直接读取在发送前拒绝；外部 Catalog、UDF、UNNEST、hint、写语句与 session 改写在发送前拒绝；
8. 专用账号对未授权对象读取失败，且无写、管理、UDF、外部 Catalog 与文件权限；
9. 飞书单聊、群聊与引用回复追问的真实往返；
10. query id 与 Profile 可关联；24 小时应用层删除；StarRocks audit/query history 与飞书聊天记录的保留口径；
11. information_schema、SHOW PROFILELIST、SHOW PROC 等元数据实际可见内容，由负责人确认接受。

最高证据只能标记 tests + test-env verified，不等于生产部署、canary 或 UAT。

## 15. 实施切片

以下 F1-0a 至 F1-G 统称 F1-Core；F1-NL 完成后才满足 §12.1 的完整完成定义。

1. F1-0a：本设计与 §13 真源修订、实施计划（文档，无行为源码）；
2. F1-0b：worker 有界并发；SQL 契约、migration 与提交事务；SQLGuard `confirmed_readonly` 与只读清单；
3. F1-1：SQL 识别与规则交互事实、capability 与 SlotVerifier（含网页追问）、Runner 水合与结果同事务提交、Reflection/Render
   回复、网页锁定结果页、PyMySQL adapter（离线）、24 小时清理；
4. F1-2：Admin 调低上限与维护黑名单；
5. F1-3：飞书 SQL 提交与引用回复追问；
6. F1-NL：单独设计、单独批准；
7. F1-G：离线总验收；
8. F1-H：真实 target 现场计划与现场 GO。

每个切片都以前一切片的真实接口为基础；不建第二套 SQLGuard、TaskStore、结果服务、交互路由或 connector。F2/F3
只能在 F1 离线验收及各自设计获批后启动。

## 16. 已接受的权衡和残余风险

- 保留原 SQL 避免改写语义；代价是依赖 session limit、资源组与严格 Guard；
- 发送即执行，没有单独确认步骤；执行前写 ExecutionDisclosure，且只能执行只读语句；
- 不以 SQL 关键字开头或不是一条完整语句的消息按对话处理，混合文本里的 SQL 不执行；
- SQL 会出现在飞书聊天记录中，群聊成员可见，其保留不归本系统控制；
- 同一 StarRocks 最多同时 4 条 F1 查询，没有应用层排队；数据库侧由超时、行数、只读账号与资源组约束；
- 取消配额后，高频查询会让 24 小时内的结果存储上升；单份结果仍 ≤ 20 MiB；
- 结果一次读完，进程内存临时占用最多约 20 MiB × 并发数；
- 只读语句清单写在代码里，未登记语句“暂未支持”；升级 StarRocks 大版本需人工核对清单；
- F1 不在调用前比对 grants/identity digest，也不检查 StarRocks 版本；只读权限依赖 DBA 配置的专用账号，F1-H 人工核验；
- 关系黑名单是额外策略，不展开视图 lineage，不提供元数据保密；
- 已开始未提交的查询以 FAILED（`budget.tool_calls_exhausted`）结束并提示结果未知；
- F1 查询人在 F2 前看不到结果数据；
- 预览有界、导出未来重跑，F1/F3 数据可能不同；PostgreSQL 物理擦除受 MVCC/备份约束；客户端超时不能证明服务端立即停止；
- 当前仓库仍没有本文描述的源码、migration 或真实调用证据；本文批准只代表可进入实施。
