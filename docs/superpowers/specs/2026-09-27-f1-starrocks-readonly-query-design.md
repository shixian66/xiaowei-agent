# F1 StarRocks 受治理只读查询与锁定预览设计

> 状态：Draft v2，等待负责人书面审查。
> 日期：2026-09-27。
> 设计基线：c69a09bd8595df9808754b5e4272b7c95ee78a43。
> 审查修复基线：016eae3d4bb07c9cc592effd368929a112d83d3c。
> 本文只修订 F1 设计，不授权源码、migration、真实 StarRocks 调用、部署、canary、UAT、F2、F3、F1-NL 或 E1。

相关真源：

- [项目协作与安全规则](../../../AGENTS.md)
- [项目架构](../../../ARCHITECTURE.md)
- [开发路线](../../../DEVELOPMENT_PLAN.md)
- [新功能方向](2026-09-26-feature-roadmap-direction.md)
- [Web 运维工作台与结果访问前置](2026-09-19-web-operations-console-identity-activation-design.md)
- 未来 ADR-005 审批语义（当前尚未建立文件）
- [ADR-007 真实调用授权](../../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)
- [ADR-009 Admission 绑定](../../adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md)
- [ADR-012 StarRocks target-bound adapter](../../adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md)
- [ADR-013 渠道边界](../../adr/ADR-013-m7-channel-boundary.md)
- [ADR-015 模型边界](../../adr/ADR-015-real-model-provider-boundary.md)
- [ADR-017 智能交互边界](../../adr/ADR-017-intelligent-interaction-and-clarification.md)

## 1. 修订后的结论

F1 第一阶段只交付 Web 显式 SQL 模式下的直接 SQL 查询。用户先提交 SQL 草稿，Web 再完整展示原 SQL、
target 和本次预算；用户明确确认后才创建执行任务。系统执行原始 UTF-8 字节对应的同一 SQL，不格式化、
不改写、不自动追加 LIMIT。

自然语言生成 SQL 仍是已确认的产品方向，但拆成 F1-NL 单独设计、单独批准。F1-NL 未完成不改变
“模型只产候选、完整展示、用户确认后才可执行”的既定边界；它也不能借本设计进入实现。

飞书和其他聊天入口在 F1 只返回可信 Web SQL 页面链接，不解析、不保存、不执行消息中的 SQL。F1 查询
本身不走 ApprovalGate，但仍完整经过：

    Artifact input
    → CapabilityResolver
    → PlanCompiler
    → WorkflowRunner
    → StepAdmission(ToolPolicy → SQLGuard)
    → ToolGateway
    → target-bound streaming adapter
    → ResultArtifact
    → Evidence / Outcome

F1 保存最多 1000 行、20 MiB 的预览 artifact，但页面不展示列名和真实结果行。F2 获得单独批准并完成
结果查看 ACL 后，才可分页读取这些已保存行。F3 获得独立导出审批后，重新执行同一原 SQL；F1 的
sql_select_limit 不影响 F3。

## 2. 审查意见核对与共同根因

### 2.1 十项阻断意见

| 意见 | 核对结论 | 当前证据与修复归组 |
| --- | --- | --- |
| P1-1 文档和 ADR 变更不全 | 成立 | 原稿漏了 AGENTS、ARCHITECTURE §6/§9、ADR-009、ADR-017 和 TaskStore 恢复语义；见 §13 |
| P1-2 SQL 从 store 到 adapter 的链路未定义 | 成立 | 当前 Runner 直接把 PlanStep.typed_arguments 复制成 ToolCall，StepAdmission 在没有 SQL key 时跳过 SQLGuard；见 §5.4 |
| P1-3 结果会进入 Evidence 且同名列丢失 | 成立 | 当前 AdapterResponse.payload 会变成 ToolResult.data_view，EvidenceBuilder 会逐行持久化；FrozenMap 无法表示同名列；见 §5.5 |
| P1-4 通用消息信封会永久保存 SQL | 成立 | RequestEnvelope.text 上限为 8192，TaskSubmission 又把整个 envelope 持久化；飞书猜 SQL 还会扩大入口权力；见 §5.1 至 §5.3 |
| P1-5 崩溃恢复可能重复执行查询 | 成立 | 当前未提交 step attempt 在新 fencing token 下可再次 PROCEED，max_tool_calls 耗尽又被映射为 FAILED；见 §10 |
| P1-6 排队、超时和连接关闭不闭合 | 成立 | TaskStatus 没有 queued；asyncio.wait_for 取消不了 asyncio.to_thread 中的 PyMySQL；提前释放进程内锁会放大并发；见 §8 |
| P1-7 AST、SHOW、DESC 和 hint 规则有缺口 | 成立 | sqlglot 30.8.0 实测不能可靠区分注释 hint，SHOW catalog 不一定成为 Table，EXPLAIN 与 DESC 共用 Describe 形状；见 §7 |
| P1-8 未逐项满足 Web §11.2 | 成立 | 原稿未处理 approver 真源、ADR-005 适用范围和独立数据处置授权；见 §11.3 |
| P1-9 target 有多个真源 | 成立 | 当前 Resolver 使用代码内目录，真实 adapter 使用环境变量，Web resources 文件只报告 generation；见 §6 |
| P1-10 自然语言 SQL 没有获批模型端口 | 成立 | 当前只有分类和终态 advisory 两个窄模型端口，且模型请求总上限为 512 KiB；见 §12.2 |

十项阻断没有发现不成立项。P1-5、P1-6 是源码路径推导，尚未用真实 PyMySQL/StarRocks 故障注入验证；
因此本文采用最保守的恢复和 lease 语义，不把推导写成已发生的线上事故。

### 2.2 非阻断意见的处理

| 意见 | 决定 |
| --- | --- |
| 缓冲游标会吃满进程内存 | 接受；F1 必须使用 PyMySQL SSCursor，并限制单批读取 |
| 现有 driver timeout 小于 30 秒 | 接受；按 §8.3 重做 timeout 分层并修订 ADR-012 |
| 旧 SQLGuard 会误拒中文字符串/注释 | 接受；template_locked 保持原行为，confirmed_readonly 使用 token 位置规则 |
| RESTRICTED 读取无审批语义不清 | 接受；修订 ADR-017，只允许受信 profile 明确声明的 RESTRICTED read 无审批 |
| 24 小时 artifact 数量无上限 | 接受；增加 requester 与 target 两级存活配额 |
| “DBA 级只读”与专用账号冲突 | 接受；统一为“DBA 配置和批准的 F1 专用跨库只读账号” |
| UNNEST 会被 table function 规则拒绝 | 接受并明确；F1 初版不支持 UNNEST |
| Web 配置不支持热加载 | 接受；只允许保存后受控重启并写加载回执 |
| 原切片偏分层 | 接受；§15 改为用户可验收的业务闭环 |

审查建议把 SQL 上限从 1 MiB 调到 256 KiB，但没有给出本仓库解析性能或用户 SQL 分布证据。考虑用户存在
数百至数千行 SQL，本稿暂保留 1 MiB 系统硬上限，并把“接近上限的 sqlglot 耗时、峰值内存和递归深度”
列为 F1-0 必验项。若证据不达标，再以实测结果缩小上限，不凭经验数字直接改产品边界。

### 2.3 五个共同根因

1. 输入类型没有与普通对话分离，导致 SQL 可能进入模型、TaskSubmission 和聊天入口；
2. Plan 中的引用没有一条受信的运行时水合链，导致 SQLGuard、hash 和 adapter 看见的内容可能不同；
3. 现有 ToolResult/Evidence 面向小型诊断行，不适合大结果、重复列名和短期敏感数据；
4. 当前 attempt、timeout 和进程内并发语义默认工具可重试、可及时取消，不适合昂贵查询；
5. target、SQL 方言和 Web 前置条件分散在多个旧里程碑，未形成 F1 唯一真源。

后续设计按这五个根因修正，不按十条评论分别增加 operation 名称特判。

## 3. 产品范围

### 3.1 F1 范围

- capability 为 starrocks.readonly_query@1.0.0；
- capability 只有 execute_readonly_query 一个 operation；允许的 SELECT/SHOW/DESC 都走这一条；
- 入口为已认证 Web 的显式 SQL 编辑和确认页面；
- 单条、多行、最长 1 MiB UTF-8 SQL；
- SELECT、CTE、JOIN、子查询、UNION、聚合、窗口函数和内建标量函数；
- 闭集元数据查询：SHOW DATABASES、SHOW TABLES、SHOW COLUMNS、DESC、SHOW CREATE TABLE；
- target 内部 catalog 的数据库和表；
- 普通行注释和普通块注释；
- 有界流式预览、锁定结果页、ACL、配额、24 小时保留和清理；
- 每个 target 可由 Web Admin 调低预览行数、字节数和查询超时；
- 查询、截断、超时、目标、资源和数据处置审计。

### 3.2 明确不做

- 从飞书消息、普通 RequestEnvelope 或自然语言自动识别并执行 SQL；
- F1-NL 的 schema 采集、模型候选生成和候选确认；
- DDL、DML、CALL、SET、USE、事务、临时表脚本、变量赋值或多语句；
- SELECT FOR UPDATE、INTO OUTFILE、文件读取、导入、导出或任何持久状态修改；
- 外部 Catalog、外部表、table function、UNNEST、UDF 和存储过程；
- 任何 optimizer、resource、session 或 version comment hint；
- SQL 重写、自动 LIMIT、自动修复、自动重试或自动 KILL QUERY；
- F2 的结果行展示、查看审批和分页 UI；
- F3 的导出审批、文件生成和下载；
- TiDB、跨 target 查询、定时查询、查询缓存和快照一致性承诺；
- 让模型或入口 handler 决定最终 capability、target、预算、policy、SQL 或工具调用。

普通注释保留并随原 SQL 执行，但始终是 ExternalContent。由于当前 parser 证据不足，原稿允许的 JOIN hint
全部移出 F1；未来如恢复，必须固定 StarRocks/sqlglot 版本、定义可解析 AST 形状并补安全变异测试。

## 4. 不可破坏的不变量

1. SQL 原文只有 SqlArtifactStore 保存一份；TaskStore、PlanStore、Evidence、RenderPayload、日志和
   ChannelStore 只保存引用、hash 或安全摘要；
2. 确认页展示、SQLGuard 校验、ToolCall hash 和 adapter 执行绑定同一份原始 UTF-8 字节；
3. 缺少、过期、越权、hash 不符或 target 不符的 SQL artifact 一律在 Gateway 前拒绝；
4. operation 声明需要 SQL 时，缺少 QueryEnvelope 必须拒绝，不能沿用“没有 SQL key 就跳过”；
5. 结果行不进入 AdapterResponse.payload、ToolResult.data_view、Evidence 或 TaskOutcome；
6. 列按 ordinal 保存，不能用列名作 map key；同名列必须原样保留；
7. F1 查询一旦开始尝试，未知结果不能自动重放；
8. 单 target 并发闸跨 worker 生效，且只有确认连接关闭后才能主动释放；
9. Web 配置只有一个 task-worker 运行时真源，保存不等于加载，不热加载；
10. F1 任何真实 target 调用仍需独立现场 GO。

## 5. 输入、计划、准入和结果主链

### 5.1 Web SQL 草稿与确认

Web handler 只把认证上下文和以下 DirectSqlDraft 交给 application 层 QuerySubmissionService：

- resource_id；
- SQL 原始 UTF-8 bytes；
- draft idempotency key。

入口只传 `resource_id`，不构造 ResolvedTarget，不计算 target_fingerprint，也不选择 adapter。请求体使用
专用 SQL contract，受 1 MiB byte 上限约束，不复用 RequestEnvelope.text 的 8192 字符字段。

QuerySubmissionService：

1. 用当前 ResourceSnapshot 和 RequestContext 确定性解析 target；
2. 生成不可枚举 sql_ref，保存原始 bytes、SHA-256、requester、tenant、environment、resource_id、
   target fingerprint、config revision、创建时间和 expires_at；
3. 返回受保护确认页；此时不创建 task、不连接 StarRocks、不调用模型；
4. 确认页完整展示原 SQL、target 展示名、1000 行/20 MiB/180 秒等有效预算及“原 SQL 不会被改写”；
5. 用户确认时重新校验 principal、target、config revision、hash、过期和一次性确认状态；
6. 编辑任何字节都创建新 sql_ref；确认动作不能携带替换 SQL；
7. 只有成功确认才创建执行 task 和 result_ref。

SQL 草稿创建后 24 小时未确认即过期。确认请求幂等：同一确认 key 只能得到同一 task；不同 key 对同一
已消费草稿不能再创建第二个 task。

### 5.2 TaskSubmission 不保存 SQL

现有 TaskSubmission 只能持久化完整 RequestEnvelope，因此不能承载 F1。F1-0 必须把提交输入升级为严格
discriminated union：

- ConversationSubmission：保留现有 RequestEnvelope 行为；
- ArtifactSubmission：只含 input_kind=sql_artifact、sql_ref、sql_hash、resource_id、result_ref 和
  confirmation_ref，不含 SQL 原文或用户自然语言。

存储仍使用同一个 TaskStore 和 task_submissions 表，不建第二套任务系统。旧记录按原 schema 读取；
ArtifactSubmission 使用新版本并进入 request digest。未知版本 fail-closed。ARCHITECTURE §6 和迁移策略
必须先修订。

F1 的显式 Web 路由由 XiaoweiRuntime 的窄方法接收 ArtifactSubmission，生成确定性的 capability draft，
再进入唯一 CapabilityResolver。它不调用 InteractionClassifierPort，也不让 Web handler 直接选择
operation 或编译计划。

### 5.3 Plan 只保存不可变引用

PlanCompiler 接收不可变 ReadonlyQueryParams，并把以下字段写入单一步骤 typed_arguments：

- sql_ref、sql_hash、result_ref；
- resource_id、target config revision；
- preview_max_rows、preview_max_bytes、query_timeout_seconds；
- confirmation_ref。

ExecutionPlan 不含 SQL 原文，PlanBudget 固定 max_steps=1、max_tool_calls=1、max_model_tokens=0。上述
typed arguments、effect/read class 和预算继续进入 plan_hash。F1 为 READ + RESTRICTED；无副作用步骤，
不调用 ApprovalGate。

### 5.4 从引用到唯一 ToolCall

OperationSpec 增加受信 query requirement，闭集至少区分：

- none；
- template_locked；
- confirmed_artifact。

该 requirement 由 CapabilitySnapshot 派生，不能由 step 或用户自报。confirmed_artifact 的通用 Runner
水合流程是：

1. 重新解析当前 target、policy 和 config revision；
2. 从 SqlArtifactStore 读取 sql_ref 一次；
3. 校验 task、actor、tenant、environment、target、confirmation_ref、expiry 和 SHA-256；
4. 生成运行时 QueryEnvelope，包含原始 SQL、hash、引用、target/config 和有效预算；
5. 把 QueryEnvelope 放入本次 ToolCall.typed_args；
6. StepAdmission 先执行 ToolPolicy，再按 operation requirement 强制执行 confirmed_readonly SQLGuard；
7. AdmissionCertificate 的 tool_call_hash 覆盖含原始 SQL 的完整 ToolCall；
8. ToolGateway 重算 tool_call_hash 后，把同一个 ToolCall 交给 target-bound adapter；
9. adapter 在发送前再次计算 SQL bytes 的 SHA-256；不重新读 artifact，也不接受另一份 SQL。

StepAdmission 接口必须校验已经水合的 ToolCall/QueryEnvelope，而不是只看 PlanStep。任何 required query
缺失、字段多余、hash 不符或 artifact 无法读取都返回闭集拒绝，Gateway 调用次数为 0。现有
template_locked 慢查询仍按原参数重编译和逐字节比对，不经 confirmed_artifact 分支。

ToolCall.timeout_seconds 由有效 query timeout 和 §8.3 的 gateway grace 确定，不再固定为 30 秒。该
水合规则属于 query requirement 的共享机制，不在 Runner 里判断 F1 operation 名称。

含 QueryEnvelope 的 ToolCall 只在当前进程内存中存活；trace/audit 不得序列化 typed_args，只记录 sql_ref、
SQL hash、tool_call_hash 和闭集决定。解析、driver 和 server error 也不能回显 SQL 片段。

### 5.5 结果只走 Gateway 管理的流式 sink

F1 不把结果行塞进现有小型 payload。ToolGateway 在证书校验后，按 ToolCall 中的 result_ref 创建一个
带 task attempt 和 fencing token 的 ResultChunkSink，再调用实现 StreamingToolAdapter 的
target-bound adapter。

adapter 使用位置数据写入 sink：

- columns 为有序 ColumnSpec(ordinal, name, type)；
- rows 为按 ordinal 对齐的 tuple；
- 同名列由不同 ordinal 区分；
- chunk 最多 100 行；
- sink 自己执行行数、规范编码字节数、配额和 fencing 校验。

adapter 返回的 AdapterResponse 只含有界执行元数据，例如 query id、耗时、扫描量、峰值内存和截断类型；
payload 必须为空。Gateway 产生的 ToolResult.data_view 也必须为空，raw_ref 只引用 result_ref。
EvidenceBuilder 只持久化 result_ref、hash、行/字节计数、完整性状态和资源指标，不持久化列或行。

结果提交采用 staging → sealed → available：

1. 查询期间 chunk 只处于 staging，对读取 API 不可见；
2. adapter 成功且连接关闭后，Gateway 封存为 sealed 并返回 ToolResult；
3. Runner 提交 step journal；
4. 已提交 step 的恢复路径可幂等地把相同 result_ref 激活为 available，不重新查询；
5. step 未提交就崩溃时，artifact 永不 available，任务按 §10 进入 INDETERMINATE，staging/sealed 数据
   由 retention 清理。

这套 sink 是现有 ToolGateway 的窄扩展，不是第二个执行入口。领域层仍不能直接持有 StarRocks 客户端。

## 6. F1 target 的唯一真源

### 6.1 ResourceSnapshot

F1 把 task-worker 在启动时成功加载并校验的 ResourcesConfig generation 冻结为 ResourceSnapshot，
作为本进程唯一 target/connector 真源。StarRocksResource 在 F1-0 增加：

- tenant_id；
- F1 专用 credential reference；
- DBA 带外批准的 version、grants、identity 和必要 DDL digest 及 source_ref；
- resource group 标识和审批引用；
- F1 查询预算；
- enabled 与 generation。

host、port、database、username、TLS、secret ref、预算和 preflight digest 全部来自同一 snapshot。
现有 XIAOWEI_STARROCKS_* 测试装配只保留给 M6b test_readonly profile，不能与 F1 ResourceSnapshot
同时启用；F1 release 装配发现双真源直接启动失败。

账号口径统一为：DBA 配置和批准的 F1 专用跨库只读账号，无写、管理、UDF、外部 Catalog 和文件权限。
Web 一个 StarRocks resource 只引用这一套 F1 credential，不再同时假设另一套“DBA 账号”。

### 6.2 确定性解析和漂移

task-worker 启动时由有效 snapshot 构造 F1TargetDirectory。CapabilityResolver 只根据：

- RequestContext 的 tenant/environment；
- 用户在 Web 选择的 opaque resource_id；
- 当前 snapshot 的 enabled resource；

解析唯一 ResolvedTarget。没有默认 target、名称猜测或跨 tenant fallback。target_fingerprint 继续由
canonical target 计算；adapter binding 还必须精确匹配 config revision。

配置保存后 Web 只显示 saved generation 和 restart_required。管理员在宿主受控重启 task-worker；
新进程校验成功后写 loaded receipt。旧进程不热加载。新 generation、host、credential、预算、enabled
或 digest 任一变化都会使旧计划在 Admission 前因 config revision/target 漂移拒绝，Gateway 调用为 0。

preflight expected digest 只能来自负责人/DBA 的带外批准输入，不能用首次连接结果自我签名。

## 7. SQLGuard 与 ToolPolicy

### 7.1 一个内核，两个 profile

governance/sqlguard.py 仍是唯一 SQL 准入内核：

- template_locked：现有 starrocks.slow_query.diagnose 使用，保留模板重编译、参数闭集、LIMIT、
  表列和时间窗规则；
- confirmed_readonly：F1 使用，校验原文 hash、单语句、只读 AST、内部对象和 F1 token 规则，
  不重编译、不改写 SQL。

StepAdmission 只能从 CapabilitySnapshot 取得 profile。慢查询计划如果被伪装成 confirmed_readonly，
必须因 operation/profile 漂移拒绝。

### 7.2 原文 token 扫描

AST 前增加一个 quote-aware lexer pass，仅识别语句边界和注释类别，不用正则替代 AST：

- 字符串、quoted identifier 和普通注释内部的分号不计为多语句；
- 普通 --、# 和不以 +/! 开头的块注释允许；
- 字符串/quoted identifier 外出现 /*+ 或 /*! 一律拒绝；
- 控制字符和 Unicode format 字符拒绝；
- 中文及全角字符可以出现在合法字符串和普通注释内；
- smart quote、全角标点若位于关键字、运算符或 identifier token 位置则拒绝。

移除 token scan 后，comment hint 恶意矩阵必须转红。confirmed_readonly 不复用旧 SQLGuard 对整段文本的
全角字符禁令。

### 7.3 AST 闭集

SQL 必须用项目锁定的 StarRocks 方言成功解析为恰好一条语句。允许：

1. 根语句为 SELECT；
2. SHOW DATABASES 且没有 catalog、db、LIKE、WHERE 或其他参数；
3. SHOW TABLES、SHOW COLUMNS、SHOW CREATE TABLE 的数据库位置最多是一个单段内部 database
   identifier，不接受 catalog；
4. DESC 的 this 必须是 Table，且没有 style、kind、partition、format、as_json 或嵌套 query。

拒绝：

- 所有写入、锁、文件、事务、session、变量、动态 SQL、procedure 和 explain 节点；
- Describe 内含 Select、Insert 或其他语句的 EXPLAIN/EXPLAIN ANALYZE 形状；
- 任意 AST 位置出现三段及以上表名，包括 default_catalog.db.table；
- 任意 AST 位置出现非当前 target 内部 catalog；
- table function、UNNEST、qualified function；
- optimizer/resource/session/version comment hint。

一段基础表名按 target 配置的 default database 确定性解析；两段 db.table 允许；CTE alias 和 derived
table 不被误当基础表。所有数据库和表引用在当前 credential 权限面内再次 fail-closed。不存在的对象、
类型错误或 StarRocks 执行错误结构化返回，不自动修改或重试。

未限定名称的 scalar、aggregate 和 window function 可以使用；F1 不维护容易漂移且会误伤大数据 SQL 的
内建函数白名单。UDF 边界由“拒绝 qualified/table function + F1 credential 无 UDF 权限”共同承重；
preflight grants digest 无法证明或发生漂移时关闭整个 target，而不是尝试执行后再猜函数来源。
[StarRocks 权限文档](https://docs.starrocks.io/docs/administration/user_privs/authorization/User_privilege/)
明确把数据库级和全局 UDF 的 USAGE 与普通表 SELECT 分开授权。

### 7.4 LIMIT 语义

SQLGuard 不要求用户文本自带 LIMIT，也不追加 LIMIT。COUNT 和允许的 SHOW 同样通过。返回上限由本次
session 的 sql_select_limit 和应用 sink 双重限制；这只约束返回/保存，不约束扫描、JOIN、排序、
聚合、服务端内存或 CPU。
[StarRocks system variables](https://docs.starrocks.io/docs/sql-reference/System_variable/) 只把
sql_select_limit 定义为最大返回行数，并把 query_timeout 定义为当前连接查询的秒级超时。

## 8. 资源预算、排队和连接生命周期

### 8.1 系统硬上限

| 项目 | F1 硬上限 | 说明 |
| --- | ---: | --- |
| 保存预览行 | 1000 | 第 1001 行仅作 has_more 哨兵 |
| 保存预览字节 | 20 MiB | 按规范编码后的完整行累计 |
| server query timeout | 180 秒 | 不含等待 target slot |
| target slot 等待 | 300 秒 | 超时后不建立连接 |
| 单 target 活跃查询 | 1 | 跨 task-worker |
| SQL 语句数 | 1 | 不接受脚本 |
| SQL 原文 | 1 MiB UTF-8 | 需通过近上限解析资源测试 |
| 单 requester 存活 query set | 5 | 每组含一份 SQL 与至多一份 result |
| 单 target 存活 query set | 50 | 结果行数据最大约 1 GiB |

Web 不能调高硬上限。提高任何红线都要修改版本化 policy、补性能证据并复审。

### 8.2 Admin 可调值

每个 target 可调低：

- preview_max_rows：1..1000，默认 1000；
- preview_max_bytes：1 MiB..20 MiB，默认 20 MiB；
- query_timeout_seconds：1..180，默认 180。

并发、SQL 大小和 artifact 配额初版只读。Admin 不能输入任意 session variable、SQL 规则、resource
group 表达式或 connector fallback。保存后必须重启 task-worker，见 §6。

### 8.3 timeout 层次

设有效 server query timeout 为 Q：

- connect/write timeout：最多 10 秒，且不大于 read timeout；
- StarRocks session query_timeout：Q，Q ≤ 180；
- PyMySQL read_timeout：Q + 10 秒；
- ToolGateway timeout：Q + 20 秒；
- target slot lease TTL：Q + 30 秒；
- ToolCall 仍满足现有 300 秒硬上限。

ADR-012 和 Settings 的当前 25/30 秒限制必须随 F1 profile 一起修订；慢查询旧 profile 保持自己的
现有上限。Gateway timeout 只能给连接关闭和结果封存留余量，不能把 server query_timeout 扩大。

### 8.4 跨 worker slot

TaskStatus 不新增 QUEUED。等待 slot 时任务保持 RUNNING，并写结构化 substage=waiting_target_slot；
等待时间和查询执行时间分别观测。等待超过 300 秒结构化失败，且 StarRocks 连接数仍为 0。

TargetQueryLeaseStore 以 target_fingerprint 为 key，使用数据库唯一约束、lease、fencing 和 expiry。
取得 slot 后才创建 StarRocks 连接。正常路径必须等同步 worker thread 的 finally 已确认连接关闭，再
主动释放 slot。

asyncio timeout、取消、worker 崩溃或无法确认连接关闭时不得提前释放；停止续租并等待 TTL 到期。即使
客户端已经返回超时，服务端查询仍可能残留到 Q，产品必须显示“终止未确认”，不能宣称取消成功。
现场门必须测量这个窗口。

该 lease 只能约束应用新建连接，不能证明进程崩溃后服务端没有残留查询。TTL 到期后新任务可能与残留
查询短暂重叠；StarRocks resource group/query queue 才是数据库侧最终保护。若现场证据不能把残留窗口
控制在批准范围，F1 真实 target 不开放，而不是无限延长应用 lease。

### 8.5 会话与流式读取

每次查询使用 call-local connection，不自动重试，不设置全局变量：

1. 设置并回读 query_timeout=Q；
2. 设置并回读 sql_select_limit=effective_preview_max_rows+1；
3. target 启用 StarRocks query queue 时，证明其 pending timeout 不大于 Q，或设置并回读获批的等价
   session 值；无法证明就拒绝真实装配；
4. 核对 identity、grants/config revision 和批准的 resource group 事实；
5. 执行经 Guard 的原 SQL；
6. 用 PyMySQL SSCursor 和 fetchmany(最多 100 行) 顺序读取；
7. 到 max_rows+1 或下一完整行超过 byte 上限即停止读取；
8. 正常读到 EOF 时关闭 cursor 和 connection；提前截断、超时或取消时直接关闭本次独占 connection；
9. 连接关闭后才能 seal artifact 和释放 slot。

PyMySQL 当前实现中，SSCursor.close 会耗尽未读结果，所以提前截断路径不能使用会隐式调用
cursor.close 的上下文管理器；否则 1001 行哨兵之后仍可能继续接收完整结果。该路径必须直接关闭本次独占
connection，并以项目锁定的 PyMySQL 版本做单元和真实驱动集成测试。连接关闭只能中断客户端读取，仍不能
证明 StarRocks 服务端已经立即停止。
[PyMySQL SSCursor 源码](https://github.com/PyMySQL/PyMySQL/blob/main/pymysql/cursors.py) 同时说明其为
unbuffered cursor，并显示 close 会调用 finish_unbuffered_query。

SSCursor 只避免把全部结果行缓存在 Python 进程，不限制服务端算子内存，也不能阻止单个超大 row/packet
先进入 driver。20 MiB 是保存上限，不是端到端内存承诺；最大列宽/单行压力、resource group 内存限制和
网络行为必须进入现场门。

resource group 是否能以 session 变量回读不是已验证事实。实现只能核对当前 StarRocks 版本确实支持的
事实；其余依赖 DBA 带外配置、grants digest 和现场 Profile 证据，不伪造 readback。

## 9. Result Artifact、ACL 和保留

### 9.1 数据形状

result_ref 使用 CSPRNG，不能枚举。ResultArtifactStore 是结果和 ACL 的唯一真源，至少保存：

- task、attempt、requester、tenant、environment、target fingerprint、config revision；
- sql_ref、SQL hash、confirmation_ref；
- query id、开始/结束、slot wait/query/close duration；
- 有序 ColumnSpec 和有序 chunks；
- saved rows/bytes、has_more、completeness；
- storage_state、created_at、expires_at、export_policy；
- 当前 fencing token 和 seal digest。

storage_state 为 staging、sealed、available、failed、expired。completeness 独立为 complete、
truncated_rows 或 truncated_bytes；只有 available 才能被结果服务读取。查询/存储失败的部分 chunks
不成为预览。

列名不是 key。SELECT a.id, b.id 之类同名列由 ordinal 保持顺序，F2 展示时也不得覆盖。

行使用带类型标签的规范编码；Decimal、日期时间和大整数不能经 float 转换，非法 UTF-8 与未批准 binary
类型结构化失败。byte 上限按完整规范编码后的 row 计算，不保存半行。

### 9.2 ACL

新增规范化 result_access_grants：

- F1 确认查询时写 requester_owner grant；
- grant 绑定 result_ref、principal、grant_kind、approval_ref、created_at、expires_at；approval_ref 仅
  requester_owner 可为空，所有 approver grant 必须非空；
- F1 不写 view_approver 或 export_approver；
- F2/F3 只能通过各自批准的审批流程增加对应 grant，不能改 requester；
- Admin 身份本身不产生 grant。

export_policy 在 F1 固定为 disabled。SqlArtifact 记录将来可被哪一个 result/export request 引用，但
F3 仍须另行审批并在 TTL 内校验 hash、target 和 requester。

### 9.3 锁定页面

F1 可以开放 /results/{result_ref} 锁定页的前提见 §11.3。页面只显示：

- query/task 状态、target 展示名、时间和预算；
- 保存行数/字节数、是否截断、过期时间；
- “详细结果尚未获 F2 查看审批”的提示；
- requester 自己已确认的完整 SQL。

页面和 API 不返回列名、单元格、chunks、数据摘要或导出链接。not-found、过期和越权使用同一隐藏式
拒绝。飞书/RenderPayload 只投影受保护链接，不嵌入 SQL 原文或结果行。

### 9.4 24 小时和清理

- 未确认 SQL 草稿：创建后 24 小时；
- 已确认 SQL 与结果：任务终态后 24 小时；
- 无终态的孤儿 artifact：创建后最多 24 小时；
- 过期后应用读取立即拒绝；
- retention worker 删除 SQL bytes、columns、chunks、ACL 和活动索引；
- 长期只保留 hash、actor、target、时间、预算、Guard/Policy 决定、query id 和资源指标；
- 日志、trace、TaskStore、Evidence、ChannelStore 和备份不得新增 SQL/结果副本。

PostgreSQL DELETE 不能证明磁盘页和旧备份立即物理擦除。上线前必须验证备份排除/过期、VACUUM 和
现场数据处置；否则只能声明“应用层已不可访问且活动记录已删除”。

24 小时只约束小维应用 artifact。StarRocks FE audit log、query history、Profile 或数据库侧备份可能按
集群策略保留原 SQL 和 literal 更久；F1-H 必须由 DBA 给出保留、访问和删除口径并展示给负责人。没有这份
证据时不得宣称“SQL 24 小时后从所有系统删除”。

## 10. 一次尝试、崩溃恢复和取消

F1 依靠现有 PlanBudget.max_tool_calls=1 表达“不自动重放”，不新增 operation 名称特判。TaskStore 的
step attempt 决策增加通用结果 PREVIOUS_ATTEMPT_UNCERTAIN：

- 没有旧 attempt：允许第一次 PROCEED；
- 已有 committed attempt：ADOPT_COMMITTED_RESULT，不调用 Gateway；
- 已有未提交 attempt，且新的 fencing owner 无法证明工具未开始：返回
  PREVIOUS_ATTEMPT_UNCERTAIN；
- Runner 把该决定映射为 TaskStatus.INDETERMINATE，Gateway 调用次数为 0；
- 普通 budget exhaustion 仍是确定性 FAILED，不能与“上次可能已经执行”混为一类。

attempt 在取得 target slot 后、连接前写入 started 事实；因此即使崩溃发生在真正发送 SQL 前，恢复也
保守地不重放。若 ToolResult 已提交但 result 仍 sealed，恢复只执行幂等 activation，不重新查询。

用户点击“重新查询”会创建新的 sql_ref 确认或显式复用仍有效 SQL artifact，并始终创建新 task/new
result_ref；它不是自动 retry。取消只关闭客户端连接并记录 close outcome；服务端终止无法证明时任务
不得伪装为已安全停止。

## 11. 渠道、结果页和 Web 规格 §11.2 六项

### 11.1 渠道边界

- Web 显式 SQL 模式是 F1 唯一提交和确认入口；
- 飞书只发送由服务端生成的受保护 Web 深链；
- 普通聊天文本即使包含 fenced code block 也不进入 QuerySubmissionService；
- ChannelStore、卡片和普通任务详情不保存 SQL 原文、列或行；
- 未登录、未激活或 scope 不匹配时，深链在读取 artifact 前拒绝。

### 11.2 RESTRICTED read

F1 operation 是 READ + RESTRICTED。查询不需要 ApprovalGate 的条件必须同时满足：

- CapabilitySnapshot 明确绑定 confirmed_readonly；
- 当前 requester 有 submit_readonly_task；
- target、配置、预算和 SQL artifact 全部通过 Admission；
- operation 无 side effect，export_policy=disabled；
- result page 仍受 requester ACL 锁定。

ADR-017 必须明确：RESTRICTED 不等于必须审批；只有获批 policy profile 显式声明的 RESTRICTED read
可以无 ApprovalGate。写操作和 F2/F3 审批不受此条影响。

### 11.3 六项逐条对照

| Web §11.2 前置 | F1 设计 | 负责阶段与开放门 |
| --- | --- | --- |
| 1. 稳定 result_ref 和有界 artifact | CSPRNG result_ref、staging/sealed/available、1000 行/20 MiB | F1-1 契约、migration、故障集成测试通过前不注册结果路由 |
| 2. requester、approver、状态、有效期、导出规则唯一真源 | ResultArtifactStore + result_access_grants；F1 只写 requester_owner，approver grant 留给 F2/F3；export disabled | F1-1 验收 store/ACL；F2/F3 分别验收 approver 写路径 |
| 3. ADR-005 审批语义 | F1 锁定页不展示结果、无审批，申请把本条窄化为“任何 approver grant 或导出前必须满足 ADR-005” | 必须先修订 Web 规格、建立并批准 ADR-005；未批准时 F1 不开放 /results |
| 4. ADR-013 深链只投影引用 | 飞书/RenderPayload 只含 result_ref 深链，无 SQL/列/行 | F1-0 修订 ADR-013，安全测试通过后开放 |
| 5. 保留、脱敏、分页、导出和失效 | F1 定义 24h、隐藏式拒绝、锁定页；分页归 F2，导出/下载票据归 F3 | 本文批准 F1 部分；F2/F3 未批准前保持锁定/disabled |
| 6. 独立里程碑、计划及真实调用/数据处置授权 | F1 实施计划、F1-H 现场计划、数据处置 GO 分离 | 本文不授予其中任何一项；各自书面 GO 后才能进入对应阶段 |

因此，本文即使获批，也不能直接开放 /results。先完成表中适用的文档修订、源码验收和离线数据处置
验证；真实数据页面还必须取得现场 GO。空页面或 fake rows 不算完成。

## 12. 与 F2、F3、F1-NL 的边界

### 12.1 F2 与 F3

- F2 只读取 F1 available artifact，按保存 chunks 投影每页 50 或 100 行；翻页不访问 StarRocks；
- F2 查看审批与 F3 导出审批互不授权；
- F3 审批绑定原 SQL hash、target fingerprint、requester 和有效 sql_ref；
- F3 在新连接、新 session 中一次重新执行原 SQL，使用 SSCursor/fetchmany 流式写 CSV；
- F3 不导出 F1 的 1000 行预览，也不用 LIMIT/OFFSET 循环；
- 若 F3 上限为 200000 行，其 session 可使用 sql_select_limit=200001；F1 的 1001 不会传入 F3；
- F1 时间 T1 和 F3 时间 T2 的数据可以不同，UI 必须显示两次时间，不承诺快照一致；
- SQL artifact 过期或 target/policy/config 漂移时不能导出。

F3 的文件大小、超时、CSV 注入、下载票据、完整性和中断恢复必须单独设计。

### 12.2 F1-NL 单独设计

自然语言查询不属于本稿实施范围。后续 F1-NL 至少要单独解决：

- 新模型 Port、DTO、artifact 和它在 Runtime 主链中的准确位置；
- 元数据查询使用 template_locked 系统模板；
- 表/列 COMMENT 一律按 ExternalContent 处理；
- 完整 provider request 不超过 ADR-015 的 512 KiB；
- 模型只产生候选，不产生 ToolCall；
- Web 完整展示候选 SQL、target 和预算，确认后创建新的 ArtifactSubmission；
- 候选生成、确认过期和执行任务分别可恢复；
- 注入 COMMENT eval、候选零执行和 Gateway 调用次数为 0 的反例。

在该设计批准前，不保留 64 表、2 MiB context 或任何 schema 采集默认值。

## 13. 源码前必须修订的真源

本稿获批后，F1-0 必须先形成一个文档/ADR PR；该 PR 未批准前不得写 F1 行为源码：

1. AGENTS.md：把“执行 SQL 必须确定性生成”修订为“模板 SQL 确定性生成；用户 SQL 必须来自受保护
   artifact、完整确认、原文 hash 和 AST Guard”，不放宽 LLM 无执行权；
2. ARCHITECTURE.md §4：加入显式 artifact input，不把它伪装成普通对话；
3. ARCHITECTURE.md §6：加入 versioned TaskSubmission、QueryEnvelope、ResultArtifact、
   ResultChunkSink 和 target lease 契约；
4. ARCHITECTURE.md §7/§7.3：加入 artifact/ToolCall hash、sealed result adoption 和
   PREVIOUS_ATTEMPT_UNCERTAIN；
5. ARCHITECTURE.md §9：加入 confirmed_readonly profile、token scan、元数据闭集和无 hint 决策；
6. DEVELOPMENT_PLAN.md、路线规格和 AGENT_HANDOFF.md：移除“只接受模板 SQL”的过期口径，同时保持
   “无源码授权、无真实调用授权”的当前状态；
7. 未来 ADR-005/Web §11.2：批准锁定页不依赖 approver，但任何 view/export approver grant 仍受
   ADR-005；
8. ADR-007：登记第四 capability、F1 test/release 授权矩阵和真实现场门；
9. ADR-009：冻结 QueryEnvelope 水合、完整 ToolCall hash、result_ref 和 attempt unknown 语义；
10. ADR-012：扩展独立 F1 adapter/profile、ResourceSnapshot、SSCursor 和 timeout 层次，不改变慢查询；
11. ADR-013：允许只投影受保护 result deep link；
12. ADR-017：冻结 RESTRICTED read 无审批的窄条件；
13. 新 artifact ADR：冻结 SqlArtifact、ResultArtifact、ACL、配额、24 小时保留、staging/sealed/
    available 和 F2/F3 消费契约。

ADR-015 不因直接 SQL而放宽；只有 F1-NL 设计才可申请新增模型端口和出站数据边界。

## 14. 验证与验收

### 14.1 本次设计修复证据

- 设计契约测试必须先在旧稿上因缺少 QueryEnvelope、ResultChunkSink、恢复、target 和 Web 前置约束转红，
  修订后转绿；
- sqlglot probe 固定项目允许版本，覆盖 SHOW DATABASES FROM catalog、comment hint、version comment、
  JOIN comment hint、DESC、EXPLAIN SELECT 和 EXPLAIN ANALYZE INSERT；
- 源码检查确认当前 TaskSubmission、ToolCall、AdapterResponse/Evidence、attempt recovery、PyMySQL Cursor
  和 config 真源的实际缺口；
- 本轮不新增产品实现测试，不把规格测试冒充 F1 功能已实现。

### 14.2 实施时先红后绿

F1-0/F1-1 开工后，先补并确认以下行为测试在旧实现上因目标原因失败：

- required query envelope 缺失时 Gateway 调用为 0；
- SQL artifact 被替换、过期、跨 actor/target/config 时 Gateway 调用为 0；
- adapter 收到的 bytes 与确认 hash 不同则零 SQL 发送；
- duplicate column names 按 ordinal 往返；
- 结果 rows 不进入 ToolResult.data_view、Evidence 和 TaskSubmission；
- crash after started/before commit 恢复为 INDETERMINATE，Gateway 不重放；
- committed sealed result 只 activation，不重放；
- 两个 worker 同 target 只有一个取得 slot；
- cancellation 时 slot 在连接关闭前不释放；
- token scan、SHOW/DESC/catalog 恶意矩阵；
- 慢查询 template_locked 全部原回归继续通过；
- requester、Admin、其他用户、过期 ACL 正反例；
- 去掉 hash、required query、token scan、ACL、fencing 或 no-replay 中任一保护时，对应测试转红。

触及 governance、planning、tools 后执行：

    python -m pytest -q
    python -m pytest -m security -q
    ruff check .
    mypy src

### 14.3 1 MiB 解析门

使用项目锁定 sqlglot 和代表性复杂 SQL，测量 256 KiB、512 KiB、1 MiB 附近的 parse 时间、峰值内存、
递归深度和恶意嵌套。测试机、版本、样本 hash 和阈值必须记录。1 MiB 不能在资源门内稳定完成时，
在实现计划中缩小硬上限并重新请负责人确认。

### 14.4 真实 target 现场门

离线通过不等于真实安全。F1-H 另行固定 exact SHA、StarRocks/PyMySQL 版本、target、专用 grants、
resource group、query queue、数据处置、时间窗和回退方式，至少验证：

1. query_timeout、sql_select_limit 设置/readback 与连接关闭；
2. SQL 自带 LIMIT 时 sql_select_limit 的实际优先级；
3. 1000/1001 行、20 MiB 和 180 秒边界；
4. 一个复杂 JOIN/CTE/window 成功对照；
5. 一个高扫描低返回查询仍受 resource group 约束；
6. 同 target 第二个 worker 等待 slot；
7. StarRocks 内部 queue 等待也受 180 秒总发送后预算约束；
8. 取消/driver timeout 后服务端残留查询的最长窗口；
9. 外部 catalog、UDF、UNNEST、hint、写语句和 target drift 在 SQL 发送前拒绝；
10. 最大列宽/单行结果不会把应用保存上限误报成 driver 或服务端内存上限；
11. query id 与 Profile 指标可关联；
12. 24 小时应用层删除，以及 StarRocks audit/query history 与部署环境的数据处置。

最高证据只能标记 tests + test-env verified；它不等于生产部署、canary 或 UAT。

## 15. 按业务闭环实施

1. F1-0 规则与契约前置：完成 §13 文档/ADR、TaskSubmission/query/result/target lease 契约和
   SQLGuard profile；慢查询回归保持通过；
2. F1-1 Web 直接 SQL 闭环：固定默认预算，完成 SQL 草稿/确认、Plan/Admission、fake streaming
   adapter、ResultArtifact、锁定页、no-replay、并发 slot 和 24 小时清理；
3. F1-2 Admin 下调预算：扩展 StarRocksResource，保存后 restart_required，task-worker 启动加载回执；
4. F1-3 飞书入口：只投影可信 Web SQL 页面和受保护 result 链接，不接受消息 SQL；
5. F1-NL：单独设计、单独批准；
6. F1-G 离线总验收：正式 Web 路径、全量/安全/eval、exact-SHA 独立审查；
7. F1-H 真实 target：另写现场计划并单独 GO。

每个切片都以前一闭环的真实接口为基础；不并行建设第二套 SQLGuard、TaskStore、结果服务或 connector。
F2/F3 只能在 F1 离线验收及各自设计获批后启动。

## 16. 已接受的权衡和残余风险

- 保留原 SQL避免改写语义；代价是依赖 session limit、resource group、确认和严格 Guard；
- Web 两步确认比“编辑器直接执行”多一步，但能把 SQL、target 和预算绑定为可审计事实；
- 初版不支持任何 hint、UNNEST、UDF 和三段表名，实用性受限；这是 parser/权限证据不足下的明确收窄；
- 预览有界、导出未来重跑，F1/F3 数据可能不同；
- 同 target 并发固定 1，峰值排队更长；
- PostgreSQL 保存短期预览，物理擦除受 MVCC/备份约束；
- 客户端超时不能证明服务端立即停止，最坏残留窗口必须现场测量；
- F1-NL 拆出后，F1 第一阶段只满足直接 SQL，不宣称自然语言查询已经交付；
- 当前仓库仍没有本文描述的源码、migration 或真实调用证据；本文批准只代表设计可进入详细实施计划。
