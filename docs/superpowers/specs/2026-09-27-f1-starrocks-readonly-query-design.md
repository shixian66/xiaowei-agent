# F1 StarRocks 受治理只读查询与锁定预览设计

> 状态：**Draft v8（精简版）**，待 exact-SHA 独立复审与负责人接受。
> v7 已由 PR #108 批准并合并（`91cadd43e13a4b1178fbddcbdfd4b41bf0decb36`，复审 SHA
> `e2727cf117b62f9c9a23ec1ce37c046312475f5a`）。负责人 2026-09-27 要求“最小化实现、不过度设计”，并明确决定：
> 取消 target 排队锁、一步提交、SQL 上限 64 KiB、结果一次读完再存、只读语句清单写在代码里、worker 最多同时
> 处理 4 个任务。v8 按这些决定收缩 v7；v7 全文与四轮评审记录保留在上述 SHA，本文只写现行决定。
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

F1 只交付 Web 显式 SQL 模式下的直接 SQL 查询。SQL 页面同时显示完整 SQL、目标 StarRocks 展示名和本次上限
（1000 行、20 MiB、180 秒），并写明“原 SQL 不会被改写”；用户点击“执行”即为确认，系统在一个事务里建任务并
保存 SQL，然后原样执行这份 UTF-8 bytes，不格式化、不改写、不自动追加 LIMIT。

SQLGuard 只放行能证明只读的语句：sqlglot 能完整解析的查询、SHOW、DESC、EXPLAIN 走 AST 证明；sqlglot
看不懂的 StarRocks 只读语句只认代码里的只读语句清单，清单外返回“暂未支持”。两条路径之后统一检查内部
Catalog、关系黑名单和副作用。StarRocks 专用只读账号的权限是真正的授权边界，黑名单只做额外拒绝。写入、
DDL、锁、导出、session 改写、外部 Catalog、table function、UNNEST、UDF 和 hint 一律在发送前拒绝。

结果最多读 1001 行（第 1001 行只用来判断“还有更多”），先在进程内存里读完，再与步骤结果在同一个数据库
事务里写入结果表。锁定结果页只给提交人看状态和本人的 SQL，不展示列和行；F2 获批后才能看行，F3 获批后
重新执行原 SQL 导出。

worker 改为最多同时处理 4 个任务，慢查询不再让其他任务排队等待。不设 target 排队锁：同一 StarRocks 最多
同时跑 4 条 F1 查询，由每条查询的超时、行数上限、只读账号和 DBA 配置的资源组约束。

自然语言生成 SQL 拆为 F1-NL，另行设计与批准。飞书只发送受保护的 Web 链接，不解析、不保存、不执行消息
里的 SQL。F1 查询不走 ApprovalGate，但完整经过：

    SQL 提交（Web）
    → TaskStore.submit_sql_query（一个事务：SQL、task、结果占位、requester grant）
    → Worker → XiaoweiRuntime 窄方法 → CapabilityResolver → PlanCompiler
    → WorkflowRunner（水合 HydratedQuery）
    → StepAdmission(ToolPolicy → SQLGuard confirmed_readonly)
    → ToolGateway → target-bound StarRocks adapter（结果写入进程内缓冲）
    → commit_step_result（同一事务写步骤结果与结果行）
    → Evidence / Outcome → 锁定结果页

## 2. v8 修订记录

| 项目 | v7 | v8（负责人 2026-09-27 决定） | 代价 |
| --- | --- | --- | --- |
| target 排队 | 跨 worker 排队锁、300 秒等待窗口、非阻塞退让、调度公平排序 | 全部取消；同一 StarRocks 最多同时跑 4 条（受 worker 并发上限约束） | 数据库侧压力由超时、行数、只读账号和资源组约束 |
| 提交方式 | 先存草稿、再确认，原子确认命令 | 一步提交：页面显示 SQL、目标与上限，点击执行即确认 | 少一个确认页 |
| SQL 上限 | 1 MiB，原始 `application/sql` 专用路由 | 64 KiB（65_536 bytes UTF-8），复用现有 JSON 写入口与 128 KiB body 上限 | 超长 SQL 被拒绝 |
| 结果写入 | 边查边存、线程桥、result fencing、staging/sealed/available | 一次读完（≤1001 行、≤20 MiB）后随步骤结果同事务写入 | 进程内存临时占用 ≤ 20 MiB |
| 只读语句登记 | 带小版本区间和 digest 的可配置 registry | 代码内只读语句清单，随代码版本走 | 升级 StarRocks 大版本需人工核对清单 |
| worker | 一次处理一个任务 | 最多同时处理 4 个任务 | 需要连接池与停机收尾的并发测试 |
| 连接前置校验 | version/grants/identity/DDL digest 与 resource group readback | F1 不做 digest 比对；只读权限由 DBA 配置的专用账号承重，F1-H 人工核验 | grants 漂移不能在调用前自动发现 |

v7 评审中确认且 v8 保留的结论：SQL 不进 RequestEnvelope、ToolCall 或 Evidence；ToolCall 只放标量，SQL bytes 只在
进程内 HydratedQuery；同名列按 ordinal 保存；SSCursor 截断时直接关闭连接；hint 按 token 规则拒绝；RESTRICTED
read 只在获批 profile 下免审批；Web §11.2 六项逐项对照。

## 3. 产品范围

### 3.1 F1 范围

- capability 为 `starrocks.readonly_query@1.0.0`，只有 `execute_readonly_query` 一个 operation；
- 入口为已认证 Web 的显式 SQL 页面；
- 单条 SQL，最长 65_536 bytes UTF-8；
- SELECT、CTE、JOIN、子查询、UNION、INTERSECT、EXCEPT、聚合、窗口函数和内建标量函数；
- sqlglot 可解析的 SHOW、DESC/DESCRIBE、EXPLAIN；代码清单登记的其他只读语句（§7.3）；
- target 内部 Catalog 的数据库、系统库、表、视图和物化视图；
- 普通行注释和普通块注释；
- 有界预览、锁定结果页、ACL、配额、24 小时保留和清理；
- 每个 target 可由 Web Admin 调低预览行数、字节数和查询超时，并维护关系黑名单（F1-2）；
- 查询、截断、超时、目标和数据处置审计。

### 3.2 明确不做

- 从飞书消息、普通 RequestEnvelope 或自然语言自动识别并执行 SQL；
- F1-NL 的 schema 采集、模型候选生成和候选确认；
- DDL、DML、CALL、SET、USE、事务、临时表脚本、变量赋值或多语句；
- SELECT FOR UPDATE、INTO OUTFILE、文件读取、导入、导出或任何持久状态修改；
- 外部 Catalog、外部表、table function、UNNEST、UDF 和存储过程；
- 任何 optimizer、resource、session 或 version comment hint；
- SQL 重写、自动 LIMIT、自动修复、自动重试或自动 KILL QUERY；
- target 排队锁、跨 worker 并发闸、调度退让；
- F2 的结果行展示与查看审批；F3 的导出审批与下载；
- TiDB、跨 target 查询、定时查询、查询缓存和快照一致性承诺；
- 让模型或入口 handler 决定 capability、target、预算、policy、SQL 或工具调用。

清单外的只读语句返回“暂未支持”，零 SQL 发送；新增支持必须改代码清单并补正反例，不能用泛化 SHOW 前缀放行。
普通注释保留并随原 SQL 执行，但始终是 ExternalContent。

## 4. 不可破坏的不变量

1. SQL 原文只在 SqlArtifactStore 保存一份；TaskStore、Plan、Evidence、RenderPayload、日志、trace 和 ChannelStore
   只保存 `sql_ref`、hash 或安全摘要；
2. 页面展示、SQLGuard 校验、ToolCall hash 和 adapter 执行绑定同一份原始 UTF-8 bytes；
3. 缺少、越权、hash 不符或 target 不符的 SQL 一律在 Gateway 前拒绝；
4. operation 声明需要 SQL 时，缺少进程内 HydratedQuery 必须拒绝，不能沿用“没有 SQL key 就跳过”；
5. 结果行不进入 AdapterResponse.payload、ToolResult.data_view、Evidence 或 TaskOutcome；
6. 列按 ordinal 保存，不用列名作 key，同名列原样保留；
7. F1 查询一旦开始尝试，不自动重放；
8. 结果行只在步骤结果提交的同一事务中写入并变为可读；步骤没提交就崩溃时结果永不可读；
9. Web 配置只在 task-worker 重启后加载，不热加载；
10. F1 任何真实 target 调用仍需独立现场 GO；
11. StarRocks credential 的对象权限是授权真源；关系黑名单只能缩小、不能扩大权限面；
12. target、黑名单或配置 revision 漂移必须在 Gateway 前拒绝旧计划；
13. AST 路径和清单路径只分析原文及其精确字节切片；adapter 永远收到用户提交的原始 SQL。

## 5. 输入、计划、准入和结果主链

### 5.1 Web 一步提交

SQL 页面在用户点击“执行”前完整展示原 SQL、target 展示名和有效上限。提交端点为
`POST /app/api/sql-queries`，沿用现有 JSON 写入口、session、scope、Origin/CSRF、激活校验和 128 KiB body 上限：

- body：`{"resource_id": "...", "sql": "..."}`；`sql` 按 UTF-8 编码后必须为 1..65_536 bytes，字符规则见 §7.2；
- `Idempotency-Key` header：单值、长度受限；重复 header 拒绝。

Web handler 只把认证上下文、`resource_id`、SQL bytes 与幂等键交给 application 层 QuerySubmissionService；
不构造 ResolvedTarget、不选择 adapter、不编译计划。服务确定性解析 target 后调用
`TaskStore.submit_sql_query`，在**同一个 PostgreSQL 事务**内完成：

1. 以事务级 advisory lock 串行化同一 requester 与同一 target 的配额计数，重验 5 / 50 存活上限；
2. 同一幂等键已提交过相同内容时直接返回已有 winner；内容不同则拒绝；
3. 写 SqlArtifact（原始 bytes、SHA-256、requester、tenant、environment、resource_id、target fingerprint、config revision）；
4. 建 task 与 `sql_artifact` 形状的 task_submissions 行；
5. 建 `pending` 状态的结果行（`result_ref`）；
6. 写 `requester_owner` grant。

任一步失败整体回滚；提交结果未知时按现有 not-confirmed 语义回读 winner。修改 SQL 后再次执行就是一次新
提交，产生新的 task、`sql_ref` 与 `result_ref`。

### 5.2 TaskSubmission 不保存 SQL

`TaskSubmission` 升级为以 `input_kind` 判别的 union。现有类显式改名为 `ConversationSubmission`，全部构造点
同步更新；新增 `ArtifactSubmission`。除与 ConversationSubmission 共有的 context、as_of 外，ArtifactSubmission 只含
`input_kind=sql_artifact`、`sql_ref`、`sql_hash`、`resource_id`、`result_ref`，不含 SQL 原文或用户自然语言。
同一个 TaskStore 和 task_submissions 表；旧行由 migration 回填 `input_kind='conversation'`，对话提交的 digest
字节不变。精确 schema、digest 与 downgrade 规则以 ADR-018 D2 为准。

F1 任务由 XiaoweiRuntime 的窄方法接收 ArtifactSubmission，生成确定性 capability draft，再进入唯一
CapabilityResolver。它不调用 InteractionClassifierPort，也不让 Web handler 选择 operation 或编译计划。

### 5.3 Plan 只保存引用

PlanCompiler 接收不可变 ReadonlyQueryParams，把 `sql_ref`、`sql_hash`、`result_ref`、`resource_id`、config revision、
`preview_max_rows`、`preview_max_bytes`、`query_timeout_seconds` 写入单一步骤 typed_arguments。ExecutionPlan 不含
SQL 原文；PlanBudget 固定 `max_steps=1`、`max_tool_calls=1`、`max_model_tokens=0`；以上进入 plan_hash。F1 为
READ + RESTRICTED，无副作用步骤，不调用 ApprovalGate。

### 5.4 从引用到唯一 ToolCall

OperationSpec 增加受信 `query_requirement`，闭集为 `none`、`template_locked`、`confirmed_artifact`，由
CapabilitySnapshot 派生，不能由 step 或用户自报。`ToolCall.typed_args` 仍只接受 JSON 标量。`confirmed_artifact`
的 Runner 流程：

1. 重新解析当前 target、policy 与 config revision；
2. 从 SqlArtifactStore 读取 `sql_ref` 一次，校验 task、actor、tenant、environment、target 与 SHA-256，构造不可变
   HydratedQuery；
3. 构造只含标量的 ToolCall（引用、hash、target_fingerprint、config_revision 与三个预算值）；
4. StepAdmission 分别接收 ToolCall 与 HydratedQuery，先 ToolPolicy，再按 requirement 执行 `confirmed_readonly`
   SQLGuard，并校验引用、预算与 bytes hash 一致；SQLGuard 在 `asyncio.to_thread` 中执行，不阻塞 event loop；
5. `tool_call_hash` 覆盖完整标量 ToolCall；
6. Gateway 重算 hash，再次校验 HydratedQuery，把同一 bytes 交给 target-bound adapter；adapter 发送前最后一次
   计算 SHA-256。

| 载体 | 可含 SQL bytes | 约束 |
| --- | --- | --- |
| `ToolCall.typed_args` | 否 | 引用、hash、target_fingerprint、config_revision 与预算标量 |
| `HydratedQuery` | 是 | 仅进程内；SHA-256 必须等于 `sql_hash`；只作为 StepAdmission 与 Gateway 的受信 keyword-only 参数 |

required query 缺失、字段多余、hash 不符或 artifact 无法读取都返回闭集拒绝，Gateway 调用次数为 0。trace/audit
不序列化 HydratedQuery 或结果缓冲，也不回显 SQL 片段。`template_locked` 慢查询保持原行为。ToolCall.timeout_seconds
由有效 query timeout 与 §8.3 的 Gateway 余量确定。

### 5.5 结果一次读完，随步骤结果同事务提交

Runner 为 `confirmed_artifact` 步骤创建进程内 `QueryResultBuffer`，作为 Gateway 的受信 keyword-only 参数传入，
Gateway 再交给 adapter：

- adapter 写入有序 `ColumnSpec(ordinal, name, type)` 与按 ordinal 对齐的行；
- buffer 自己执行 1000 行 / 20 MiB 上限：第 1001 行只置 `has_more`，下一完整行会超字节时置 `truncated_bytes`，
  不存半行；
- Gateway 超时或取消时关闭 buffer，之后迟到写入被丢弃；Gateway 返回 TIMEOUT，buffer 不再交给 Runner；
- AdapterResponse.payload 与 ToolResult.data_view 必须为空，`raw_ref` 只引用 `result_ref`。

步骤成功时 Runner 把冻结后的 buffer 交给 `TaskStore.commit_step_result`，在提交步骤结果、Evidence 与审计的
**同一事务**里写入列与行，并把结果行从 `pending` 改为 `available`；步骤失败或超时时同一事务把结果改为
`failed`。Evidence 只记录 `result_ref`、hash、行/字节计数、完整性与资源指标，不记录列或行。进程在提交前崩溃
时内存结果随之丢失，结果行保持 `pending`，由 §9.4 清理。领域层仍不直接持有 StarRocks 客户端。

## 6. target 配置

F1 复用 W4 的 `StarRocksResource` 与 ResourcesConfig，task-worker 启动时加载一次。F1 在该资源上新增：

- `blocked_relation_names`：默认空；每项是内部 Catalog 中精确的 `database.object`（表、视图或物化视图），不接受
  catalog 前缀、通配符、正则或单段名；规范化后冲突、非法或重复则 target 启动失败；
- F1 查询上限（§8.2）；
- F1 是否启用。

账号口径：DBA 配置和批准的 F1 专用内部 Catalog 跨库只读账号，只授予预期对象 SELECT，无写、管理、UDF、
外部 Catalog 和文件权限。黑名单只在权限面内额外拒绝；强隔离必须由 DBA 撤销对应表和视图权限。

CapabilityResolver 只根据 RequestContext 的 tenant/environment、用户选择的 `resource_id` 和已加载的 enabled 资源
解析唯一 ResolvedTarget；没有默认 target、名称猜测或跨 tenant fallback。配置保存后显示 `restart_required`，
task-worker 重启后生效；host、账号、黑名单、上限或 enabled 任一变化都改变 config revision，旧计划在 Admission
前因漂移拒绝，Gateway 调用为 0。

## 7. SQLGuard 与 ToolPolicy

### 7.1 一个内核，两个 profile

`governance/sqlguard.py` 仍是唯一 SQL 准入内核：`template_locked` 服务现有慢查询，行为不变；
`confirmed_readonly` 服务 F1，校验原文 hash、单语句、只读证明、内部对象与黑名单，不重编译、不改写。
StepAdmission 只能从 CapabilitySnapshot 取得 profile；慢查询计划伪装成 `confirmed_readonly` 必须拒绝。

### 7.2 原文 token 扫描

AST 前先做一遍 quote-aware 扫描，识别语句边界、注释类别与每个 token 在原始 bytes 中的位置，不用正则做安全
判断：

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

**清单路径（sqlglot 降级为 `Command` 或 ParseError 时）：**只匹配代码内只读语句清单。每条清单项写明 token
形状、允许与禁止的 clause、直接对象的唯一提取规则和 Catalog 规则，不用“以 SHOW 开头”放行。清单随代码版本
走；升级 StarRocks 大版本时由 DBA/实施者按 F1-H 方式人工核对清单。首批清单与实施计划 §1 一致（v7 表中各类
加 B1–B9）。清单外语句返回 `error_code=READONLY_STATEMENT_NOT_SUPPORTED`、用户文案“暂未支持”、
`retryable=false`，零 SQL 发送。EXPLAIN LOGICAL/VERBOSE/COSTS 只用 token 位置切出内层查询字节交 AST 路径证明，
执行的仍是原始 SQL。

登记支持不等于给账号增权：SHOW COMPUTE NODES、SHOW ROLES、SHOW ROUTINE LOAD、ADMIN SHOW REPLICA 等需要额外
权限的语句，专用只读账号收到 StarRocks 权限错误是预期行为，按结构化执行错误返回，不为跑通而增权。

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

SQLGuard 不要求也不追加 LIMIT。返回上限由本次连接的 `sql_select_limit` 与结果 buffer 双重限制，只约束返回与
保存，不约束扫描、JOIN、排序、服务端内存或 CPU。

## 8. 资源上限、worker 并发和连接

### 8.1 系统硬上限

| 项目 | F1 硬上限 | 说明 |
| --- | ---: | --- |
| 保存预览行 | 1000 | 第 1001 行仅作 has_more 哨兵 |
| 保存预览字节 | 20 MiB | 按规范编码后的完整行累计 |
| server query timeout | 180 秒 | |
| SQL 语句数 | 1 | 不接受脚本 |
| SQL 原文 | 65_536 bytes UTF-8 | 在现有 128 KiB JSON body 上限内 |
| worker 同时处理任务数 | 4 | 所有 capability 共用；同一 StarRocks 最多同时 4 条 F1 查询 |
| 单 requester 存活 query set | 5 | 每组含一份 SQL 与一份结果 |
| 单 target 存活 query set | 50 | 结果数据最大约 1 GiB |

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

现有 worker 对一轮取到的任务逐个 await，一条长查询会让同一 worker 的其他任务等待。v8 改为有界并发：

- 新配置 `worker_max_concurrent_tasks`，默认 4，范围 1..16；
- worker 只在有空位时才调用 `begin_task_attempt` 领取任务，领取后立即作为独立 asyncio task 运行，不为等空位
  持有 lease；
- 每个在途任务沿用现有 `run_with_task_heartbeat`，各自续租；lease、fencing 与终态保护不变；
- 停机时停止领取，等待在途任务在收尾宽限内结束，超时后取消，由现有 lease 过期恢复接管；
- 启动校验数据库连接池容量足够（`db_pool_size + db_pool_max_overflow >= 2 × 并发数 + 1`），不足则启动失败；
- 同步 StarRocks 读取仍在 `asyncio.to_thread` 中运行，默认线程池容量大于并发上限。

这是 worker 级改动，所有 capability 受益；它不改变 TaskStore 的 dispatch 候选规则、失败计数或基础设施故障窗口
的语义。

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

`result_ref` 由 CSPRNG 生成，不可枚举。结果行保存 task、requester、tenant、environment、target fingerprint、config
revision、`sql_ref`、SQL hash、query id、开始/结束时间、有序 `ColumnSpec(ordinal, name, type)`、行、保存行/字节数、
`has_more`、`completeness`、`storage_state`、created_at、expires_at 与 `export_policy`。

`storage_state` 闭集为 `pending`、`available`、`failed`、`expired`；`completeness` 为 `complete`、`truncated_rows`、
`truncated_bytes`。只有 `available` 可被结果服务读取。行使用带类型标签的规范编码；Decimal、日期时间和大整数
不经 float，非法 UTF-8 与未支持的 binary 类型结构化失败。

### 9.2 ACL

新增 `result_access_grants`：F1 提交时写 `requester_owner` grant；grant 绑定 result_ref、principal、grant_kind、
approval_ref、created_at、expires_at，只有 `requester_owner` 的 approval_ref 可为空。F1 不写 approver grant；Admin
身份不产生 grant。`export_policy` 在 F1 固定为 `disabled`。

### 9.3 锁定页面

`/results/{result_ref}` 只显示：任务状态、target 展示名、时间和上限；保存行/字节数、是否截断、过期时间；
“详细结果尚未获 F2 查看审批”提示；requester 本人提交的完整 SQL。页面和 API 不返回列名、单元格、数据摘要或
导出链接；not-found、过期和越权使用同一隐藏式拒绝。

### 9.4 24 小时保留

- SQL 与结果：任务终态后 24 小时过期；无终态的 `pending` 孤儿：创建后最多 24 小时；
- 过期后读取立即拒绝；retention 删除 SQL bytes、列、行、grant 与活动索引；
- 长期只保留 hash、actor、target、时间、上限、Guard/Policy 决定、query id 与资源指标；
- 日志、trace、TaskStore、Evidence、ChannelStore 不新增 SQL 或结果副本。

PostgreSQL DELETE 不证明物理擦除；StarRocks 自身的 audit log、query history 与 Profile 保留口径由 F1-H 向 DBA
确认，没有证据时不得宣称“SQL 24 小时后从所有系统删除”。

## 10. 不重放与崩溃恢复

F1 不新增恢复状态机，沿用现有 step journal：`begin_step_attempt` 在同一事务里创建 started 并消耗工具预算；
`max_tool_calls=1`，因此已开始但未提交的步骤再次领取时得到 `BUDGET_EXHAUSTED`，任务以 FAILED
（`budget.tool_calls_exhausted`）结束，Gateway 调用次数为 0，结果行保持 `pending` 并按 §9.4 清理。锁定页把这种
结局显示为“执行中断，结果未知，请重新提交”，不宣称查询已安全停止。已提交的步骤按现有规则 adopt，结果已在
同一事务内 `available`，不重新查询。

用户“重新查询”就是一次新提交（新 task、新 `sql_ref`、新 `result_ref`），不是自动 retry。取消只关闭客户端连接；
服务端终止无法证明时不得伪装为已安全停止。

## 11. 渠道、结果页和 Web 规格 §11.2 六项

### 11.1 渠道边界

- Web 显式 SQL 页面是 F1 唯一提交入口；
- 飞书只发送服务端生成的受保护 Web 深链；普通聊天文本即使含 SQL 代码块也不进入 QuerySubmissionService；
- ChannelStore、卡片和普通任务详情不保存 SQL、列或行；
- 未登录、未激活或 scope 不匹配时，深链在读取 artifact 前拒绝。

### 11.2 RESTRICTED read

F1 operation 是 READ + RESTRICTED。查询不经 ApprovalGate 的条件必须同时满足：CapabilitySnapshot 明确绑定
`confirmed_readonly`；requester 有 `submit_readonly_task`；target、配置、上限和 SQL 全部通过 Admission；无副作用且
`export_policy=disabled`；结果页仍受 requester ACL 锁定。写操作与 F2/F3 审批不受此条影响。

### 11.3 六项逐条对照

| Web §11.2 前置 | F1 设计 | 负责阶段与开放门 |
| --- | --- | --- |
| 1. 稳定 result_ref 和有界 artifact | CSPRNG result_ref、pending/available、1000 行/20 MiB | F1-1 契约、migration、故障集成测试通过前不注册结果路由 |
| 2. requester、approver、状态、有效期、导出规则唯一真源 | 结果表 + result_access_grants；F1 只写 requester_owner；export disabled | F1-1 验收 store/ACL；F2/F3 分别验收 approver 写路径 |
| 3. ADR-005 审批语义 | F1 锁定页只向 requester 展示状态与其本人的 SQL，不写 approver grant、不展示列与行、不提供导出，因此不依赖 ADR-005；本条窄化为“任何 approver grant、结果行展示或导出前必须满足 ADR-005” | F1-0 修订 Web 规格 §11.2 第 3 项；F2/F3 开工前必须先建立并批准 ADR-005 |
| 4. ADR-013 深链只投影引用 | 飞书/RenderPayload 只含 result_ref 深链，无 SQL/列/行 | F1-0 修订 ADR-013，安全测试通过后开放 |
| 5. 保留、脱敏、分页、导出和失效 | 24h、隐藏式拒绝、锁定页；分页归 F2，导出归 F3 | F2/F3 未批准前保持锁定/disabled |
| 6. 独立里程碑、计划及真实调用/数据处置授权 | F1 实施计划、F1-H 现场计划、数据处置 GO 分离 | 各自书面 GO 后才能进入对应阶段 |

空页面或 fake rows 不算完成；真实数据页面还必须取得现场 GO。

## 12. 与 F2、F3、F1-NL 的边界

- F2 只读取 `available` 结果分页展示，翻页不访问 StarRocks；F2 查看审批与 F3 导出审批互不授权；
- F3 审批绑定原 SQL hash、target fingerprint、requester 与有效 `sql_ref`，在新连接中重新执行原 SQL 流式写 CSV；
  不导出 F1 预览，也不继承 F1 的 `sql_select_limit`；F1 与 F3 的数据时间不同，UI 显示两次时间；
- F3 需要的边查边存、文件上限、CSV 注入与下载票据在 F3 单独设计；
- F1-NL 另行设计新模型端口、元数据模板、COMMENT 注入防护与候选确认，候选确认后仍走本文的 SQL 提交路径；
  在其批准前不保留任何 schema 采集默认值。

## 13. 源码前必须修订的真源

F1-0 文档 PR 获批前不写 F1 行为源码。须同步：

1. AGENTS.md 第 5 条：模板 SQL 确定性生成；用户直接 SQL 只来自受保护 SQL artifact，原样执行；
2. ARCHITECTURE.md §4/§6/§7.3/§9/§16：F1 目标链路、契约、worker 有界并发、confirmed_readonly；
3. DEVELOPMENT_PLAN.md、路线规格与 AGENT_HANDOFF.md：移除“只接受模板 SQL”的过期口径；
4. Web 规格 §11.2 第 3 项：F1 锁定页不依赖 ADR-005；
5. ADR-007：第四能力与 F1 授权矩阵；
6. ADR-009：query requirement、HydratedQuery、结果 buffer 与 max_tool_calls=1 不重放；
7. ADR-010：worker 有界并发；
8. ADR-012：F1 adapter profile、会话设置、SSCursor 截断与 timeout 层次；
9. ADR-013：只投影受保护 result 深链；
10. ADR-017：RESTRICTED read 无审批的窄条件；
11. ADR-018：SQL 与结果 artifact、一步提交事务、ACL、配额与保留。

ADR-015 不因直接 SQL 放宽。

## 14. 验证与验收

### 14.1 离线行为测试（先红后绿）

- required HydratedQuery 缺失、多余或 bytes hash 不符时 Gateway/adapter 调用为 0；
- ToolCall.typed_args 只接受标量；HydratedQuery 与结果 buffer 不进入 Plan、TaskStore、trace 或 audit；
- 一步提交：同幂等键同内容返回同一 task，同键不同内容拒绝；事务中任一步注入故障后全部回滚；配额在并发下只放行到上限；
- 同名列按 ordinal 往返；结果行不进入 ToolResult.data_view、Evidence 与 TaskSubmission；
- 1000/1001 行得到 `truncated_rows`，超字节得到 `truncated_bytes`，不存半行；
- 步骤提交与结果 `available` 同事务；提交前崩溃结果保持 `pending`；已开始未提交的步骤再次领取得到
  `BUDGET_EXHAUSTED`，Gateway 调用为 0；
- Gateway 超时后迟到的 buffer 写入被丢弃；截断路径不调用 `SSCursor.close()`；
- worker：一个长任务运行时其他任务照常执行；在途数不超过上限；同一任务不被重复领取；停机等待与取消；
  连接池不足时启动失败；
- SQLGuard：AST 路径与清单路径每项有正常对照、大小写/空白/quoted identifier 边界、缺失或重复 clause、多语句、
  hint、外部 Catalog、黑名单与歧义目标反例；清单外语句返回 `READONLY_STATEMENT_NOT_SUPPORTED` 且零发送；
  `HELP` 等非允许根拒绝；
- 黑名单表、视图和物化视图经 SELECT、CTE、JOIN、子查询、EXPLAIN、DESC、SHOW CREATE、SHOW COLUMNS 访问均拒绝；
  SHOW TABLES 仍可列名；黑名单不扩大授权；
- DML、DDL、锁、文件、导出、session 改写、外部 Catalog、table function、UNNEST、UDF 与 hint 恶意矩阵；
- 慢查询 `template_locked` 全部原回归通过；
- requester、Admin、其他用户与过期 ACL 正反例；锁定页响应字段集合精确等于 §9.3 闭集；
- 变异：分别去掉 hash 校验、required query、token scan、黑名单、清单目标提取、ACL、同事务结果写入，对应测试转红，
  且确认不是被更早规则遮蔽。

触及 governance、planning、tools 后执行四门：

    python -m pytest -q
    python -m pytest -m security -q
    ruff check .
    mypy src

### 14.2 真实 target 现场门（F1-H）

离线通过不等于真实安全。F1-H 另写现场计划并取得现场 GO，固定 exact SHA、StarRocks/PyMySQL 版本、target、
专用账号 grants、资源组、数据处置、时间窗与回退，至少验证：

1. `query_timeout`、`sql_select_limit` 生效与连接关闭；SQL 自带 LIMIT 时的实际优先级；
2. 1000/1001 行、20 MiB 与 180 秒边界；一个复杂 JOIN/CTE/window 成功对照；
3. 高扫描低返回查询受资源组约束；4 条并发 F1 查询对集群的实际影响；
4. 一条 180 秒查询运行时其他任务的端到端延迟（验证 worker 并发）；
5. 取消/driver timeout 后服务端残留查询的最长窗口；
6. 首批清单每条语句在专用账号下的输出形状、所需权限与权限不足错误；清单外语句零发送；
7. 黑名单表/视图直接读取在发送前拒绝；外部 Catalog、UDF、UNNEST、hint、写语句与 session 改写在发送前拒绝；
8. 专用账号对未授权对象读取失败，且无写、管理、UDF、外部 Catalog 与文件权限；
9. query id 与 Profile 可关联；24 小时应用层删除；StarRocks audit/query history 的保留口径；
10. information_schema、SHOW PROFILELIST、SHOW PROC 等元数据实际可见内容，由负责人确认接受。

最高证据只能标记 tests + test-env verified，不等于生产部署、canary 或 UAT。

## 15. 实施切片

1. F1-0a：本设计与 §13 真源修订、实施计划（文档，无行为源码）；
2. F1-0b：worker 有界并发；SQL/结果契约、migration 与一步提交事务；SQLGuard `confirmed_readonly` 与只读清单；
3. F1-1：capability、Runner 水合、结果 buffer 同事务提交、Web SQL 页与锁定结果页、PyMySQL adapter（离线）、24 小时清理；
4. F1-2：Admin 调低上限与维护黑名单；
5. F1-3：飞书只投影可信链接；
6. F1-NL：单独设计、单独批准；
7. F1-G：离线总验收；
8. F1-H：真实 target 现场计划与现场 GO。

每个切片都以前一切片的真实接口为基础；不建第二套 SQLGuard、TaskStore、结果服务或 connector。F2/F3 只能在
F1 离线验收及各自设计获批后启动。

## 16. 已接受的权衡和残余风险

- 保留原 SQL 避免改写语义；代价是依赖 session limit、资源组、页面展示与严格 Guard；
- 一步提交少一次确认；执行前页面仍展示完整 SQL、目标与上限，且只能执行只读语句；
- 同一 StarRocks 最多同时 4 条 F1 查询，没有应用层排队；数据库侧由超时、行数、只读账号与资源组约束；
- SQL 上限 64 KiB，超长 SQL 不支持；
- 结果一次读完，进程内存临时占用最多约 20 MiB × 并发数；
- 只读语句清单写在代码里，未登记语句“暂未支持”；升级 StarRocks 大版本需人工核对清单；
- F1 不在调用前比对 grants/identity digest；只读权限依赖 DBA 配置的专用账号，F1-H 人工核验，权限漂移需 DBA 管控；
- 关系黑名单是额外策略，不展开视图 lineage，不提供元数据保密；
- 已开始未提交的查询以 FAILED（`budget.tool_calls_exhausted`）结束并提示结果未知，而不是新增 INDETERMINATE 分支；
- 预览有界、导出未来重跑，F1/F3 数据可能不同；
- PostgreSQL 保存短期预览，物理擦除受 MVCC/备份约束；客户端超时不能证明服务端立即停止；
- F1-NL 拆出后，F1 只满足直接 SQL，不宣称自然语言查询已交付；
- 当前仓库仍没有本文描述的源码、migration 或真实调用证据；本文批准只代表可进入实施。
