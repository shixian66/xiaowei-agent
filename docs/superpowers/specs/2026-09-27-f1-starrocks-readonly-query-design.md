# F1 StarRocks 受治理只读查询与锁定预览设计

> 状态：Draft，等待负责人书面审查。
> 日期：2026-09-27。
> 设计基线：`c69a09bd8595df9808754b5e4272b7c95ee78a43`。
> 本文只记录 F1 设计，不授权源码、migration、真实 StarRocks/模型调用、部署、canary、UAT、F2、F3 或 E1。

相关真源：

- [项目架构](../../../ARCHITECTURE.md)
- [开发路线](../../../DEVELOPMENT_PLAN.md)
- [新功能方向](2026-09-26-feature-roadmap-direction.md)
- [Web 运维工作台与结果访问前置](2026-09-19-web-operations-console-identity-activation-design.md)
- [ADR-007 真实调用授权](../../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)
- [ADR-009 Admission 绑定](../../adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md)
- [ADR-012 StarRocks target-bound adapter](../../adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md)
- [ADR-013 渠道边界](../../adr/ADR-013-m7-channel-boundary.md)
- [ADR-015 模型边界](../../adr/ADR-015-real-model-provider-boundary.md)
- [ADR-017 智能交互边界](../../adr/ADR-017-intelligent-interaction-and-clarification.md)

## 1. 目标与成功口径

F1 面向已激活、已认证且具备 `submit_readonly_task` 权限的用户，在一个明确解析且已授权的
StarRocks target 上提供通用只读查询。用户可以：

1. 直接提交自己写的完整 SQL；系统保存并执行这份 SQL，不改写、不补写、不格式化；
2. 用自然语言描述查询；模型只生成候选 SQL，系统先完整展示，用户明确确认后才允许执行该份候选；
3. 使用 JOIN、CTE、子查询、UNION、窗口函数等实际大数据 SQL，不以单表或短 SQL 为前提；
4. 查询成功后获得受保护的 Web 链接和锁定的预览元数据；F1 不展示任何真实结果行。

F1 完成的判据是：正式 Runtime 主链可以在 fake/recording target 上证明上述两种输入都只执行
被用户提交或确认的原 SQL；查询经过同一个 ToolPolicy、同一个 SQLGuard 和同一个 ToolGateway；
结果预览有明确边界、ACL 和 24 小时保留；越权、SQL 漂移、目标漂移、资源超限和存储失败均
fail-closed。真实 StarRocks 验证仍需单独现场 GO。

## 2. 对既有方向的明确修订

`2026-09-26-feature-roadmap-direction.md` 与 `DEVELOPMENT_PLAN.md` 当前把 F1 描述为“确定性 SQL
模板，不接受用户或模型直接提供可执行 SQL”。该描述与本轮确认的产品需求冲突。

本设计采用以下新口径，并要求在任何源码实现前同步修订路线文档与相关 ADR：

- F1 接受用户原始 SQL；模型可以产生候选 SQL，但模型没有执行权；
- 用户 SQL 的提交动作即确认该原文；自然语言产生的 SQL 必须经过额外的完整展示和明确确认；
- 确认后以 SQL 原文的 SHA-256、target fingerprint 和预算快照锁定，实际执行字节必须一致；
- 现有 `starrocks.slow_query.diagnose` 继续使用模板锁定，不因 F1 放宽；
- F1 与慢查询复用共享 SQLGuard/ToolPolicy 内核，通过 capability policy profile 表达不同规则，
  不建设第二套守卫；
- DDL、DML 和其他可能改变被管目标状态的语句仍属于未来独立 E1 能力，本设计不授权。

本文获批后仍只是 F1 详细设计，不等于修订已经落地，也不等于 F1 开工授权。

## 3. 范围与非目标

### 3.1 F1 范围

- 新 capability：`starrocks.readonly_query@1.0.0`；单个执行计划仍只绑定这一个 capability；
- 单条只读语句；允许多行、数百至数千行 SQL；
- `SELECT`，包括 CTE、JOIN、子查询、集合运算、聚合、窗口函数和内建函数；
- 只读元数据语句闭集：`SHOW DATABASES`、`SHOW TABLES`、`SHOW COLUMNS`、`DESC`、
  `SHOW CREATE TABLE`；
- 普通 SQL 注释；安全的 StarRocks JOIN 分布提示 `BROADCAST`、`SHUFFLE`、`BUCKET`、
  `COLOCATE`，前提是解析器能把它识别为对应 AST 语义；
- target 可达的内部 catalog 数据库和表；
- SQL 原文最多 1 MiB UTF-8，足以承载常见千行 SQL，同时阻断无界请求体；
- 有界预览采集、锁定结果页、结果 ACL、24 小时保留与过期清理；
- Web Admin 针对每个 StarRocks target 调低预览行数、预览字节数和查询超时；
- 查询与结果审计、资源指标和结构化失败。

### 3.2 F1 明确不做

- DDL、DML、`CALL`、`SET`、`USE`、事务语句、临时表脚本、变量赋值或多语句脚本；
- `SELECT ... FOR UPDATE`、`INTO OUTFILE`、文件读取、导入导出或任何持久状态修改；
- 外部 Catalog、外部表、table function、UDF、用户变量和存储过程；未知或不能证明为内部只读的
  对象按拒绝处理；
- `/*+ SET_VAR(...) */`、`SET_USER_VARIABLE` 及其他能覆盖 session、资源、安全或执行参数的 hint；
- 自动重写 SQL、自动追加 `LIMIT`、删除注释后执行、拆分脚本或把失败 SQL 自动改成另一条 SQL；
- F2 的真实结果行查看、审批和分页 UI；
- F3 的 CSV 审批、重新查询、流式文件生成和下载；
- TiDB、跨 target 查询、定时查询、查询缓存、自动重试、自动 `KILL QUERY`；
- 让模型选择 capability、target、凭据、资源预算、审批结果、工具或调用顺序；
- 严格保证 F1 预览和未来 F3 导出来自同一数据快照。

## 4. 总体架构

继续使用现有模块化单体和唯一执行链：

```text
用户 SQL ────────────────────────────┐
                                    ├─→ SqlArtifactStore → Resolver / Planner
自然语言 → 受控 schema context → 模型候选 SQL ┘
                          ↓ 完整展示与确认
                         ExecutionPlan(sql_ref + sql_hash + budget snapshot)
                                   ↓
                         WorkflowRunner(step)
                                   ↓
              StepAdmission(ToolPolicy → SQLGuard → ApprovalGate*)
                                   ↓
                    target-bound ToolGateway adapter
                                   ↓
                         ResultArtifactStore
                                   ↓
                 Evidence / Outcome / 锁定结果链接
```

`ApprovalGate*` 对 F1 查询不触发，因为 F1 是 `READ + RESTRICTED`，且产品已经决定查询本身不审批。
自然语言 SQL 确认是“确认要执行哪一份输入”，不是 E1 审批，不复用 `ApprovalGate`。

新增的短期持久化责任只有三个：

1. `SqlArtifactStore`：保存短期 SQL 原文、来源、hash、确认和任务绑定；
2. `SchemaContextStore`：只为自然语言生成保存短期内部表结构投影及其 digest；
3. `ResultArtifactStore`：保存短期结果元数据和分块预览。

三者都不替代 TaskStore、PlanStore、EvidenceLedger 或 ChannelStore。任务状态仍由 TaskStore 决定；
结果访问关系只以 ResultArtifactStore 为真源。

## 5. 两种输入路径

### 5.1 用户直接提交 SQL

1. 入口只做协议、认证、请求体上限和 target 选择；Web/API 可显式选择 SQL 模式；飞书整条消息或
   fenced SQL block 能被确定性解析成单条 SQL 时进入本路径，混合且无法安全分离的输入要求用户改用
   SQL 模式，不把它猜成可执行 SQL；
2. `QuerySubmissionService` 在 TaskStore durable submit 前把 SQL 原文写入 `SqlArtifactStore`，计算
   UTF-8 原文字节的 SHA-256；超过 1 MiB 直接拒绝；
3. SQL artifact 记录 `source=user`、requester、tenant、environment、target fingerprint、创建时间、
   过期时间和 SQL hash；
4. 用户本次提交即视为对该原文的确认，不再弹第二次确认；
5. TaskSubmission、Planner 和渠道绑定只保存 `sql_ref`、`sql_hash`、安全摘要和预算快照，不把 SQL
   原文复制进 TaskStore、PlanStore、Evidence、日志或 ChannelStore；
6. Runner 在准入前从短期 store 读回 SQL，核对 requester、task、target、hash 和有效期；
7. SQLGuard 通过后，adapter 执行读回的同一份字符串。

系统不得给原 SQL追加 `LIMIT`。行数边界由 StarRocks session 的 `sql_select_limit` 和应用侧预览
边界共同保证，所以 `COUNT(...)`、上述 SHOW 闭集及没有文本 `LIMIT` 的 SELECT 都可以通过。

### 5.2 自然语言生成 SQL

自然语言路径分成“生成任务”和“执行任务”，不新增可恢复暂停状态，也不把确认伪装成审批：

1. 用户必须明确给出或通过澄清确认相关内部表引用；自然语言生成一次最多准备 64 个表，执行 SQL
   本身没有 64 表限制；超过时要求用户缩小生成范围或直接提交 SQL；
2. 生成任务的确定性计划先执行 `describe_query_tables`：只按已确认的 `database.table` 闭集读取表、
   列、类型和关键表属性，并通过同一 StepAdmission/ToolGateway；模型不能自行发元数据查询；
3. 原始 schema context 最大 2 MiB，写入 `SchemaContextStore`，24 小时过期；长期 Evidence 只保留
   表引用、context digest、字段数量和截断/拒绝事实，不保存完整 schema；
4. 模型只接收该受信投影和用户请求，不接收数据样本、凭据、host、原始错误或外部 catalog；模型
   只返回结构化候选 SQL 和解释，不产生执行查询的 ToolCall；
5. schema context 不能唯一解析、超过 64 表或 2 MiB 时，任务要求用户缩小数据库/表范围，不猜测；
6. 候选 SQL 以 `source=model` 写入 `SqlArtifactStore`，生成任务正常结束并完整展示 SQL；
7. 用户必须在受认证 Web 页面明确点击确认；飞书只发送受保护链接，不在卡片中嵌入 SQL 原文；
8. 确认服务重新校验当前 principal、tenant、environment、target、候选 hash 和有效期，并以单次消费
   方式创建新的执行任务；
9. 用户不能在“确认”动作里偷偷修改 SQL；若编辑内容，则按新的用户 SQL 创建新 artifact；
10. 执行任务重新走 Resolver、Planner、当前 ToolPolicy、当前 SQLGuard 和 ToolGateway，不继承模型
   对安全性、权限或正确性的任何判断。

候选确认只保证“执行的是用户看过的那份 SQL”，不保证业务语义一定正确。UI 必须明确展示 target、
完整 SQL、资源上限和“结果以执行时数据为准”。

真实模型生成 SQL 会把自然语言和受限 schema context 发给外部 provider，超出现有 ADR-015 的出站
闭集；因此它必须先修订 ADR-015，并单独取得真实模型调用与数据处置授权。未获授权时只允许 fake/
recording model，直接 SQL 路径不因此失效。

## 6. SQLGuard 与 ToolPolicy

### 6.1 一个共享 SQLGuard，两种 profile

`governance/sqlguard.py` 继续是唯一 SQL AST 准入引擎，但从当前硬编码慢查询参数改成 profile 驱动：

- `template_locked`：现有慢查询能力使用；保留参数闭集、模板重编译逐字节比对、表列和时间窗规则；
- `confirmed_readonly`：F1 使用；验证单语句、只读 AST、内部对象、hint 闭集以及 SQL artifact hash，
  不做模板重编译。

`StepAdmission` 根据当前 CapabilitySnapshot 中受信的 query profile 选择规则，不能读取用户、模型或
PlanStep 自称的 profile。PromQL 路径不受影响。

### 6.2 F1 AST 规则

F1 SQL 必须同时满足：

1. 使用获批 StarRocks 方言成功解析，且恰好一条语句；尾部分号本身不产生第二条空语句；
2. 根语句属于 SELECT 或元数据 SHOW 闭集；
3. AST 中不存在写入、锁、文件、事务、session 改写、变量赋值、动态 SQL 或 procedure 节点；
4. 所有表引用属于当前 target 的内部 catalog；显式外部 catalog、未知 catalog 和 table function 拒绝；
5. UDF 不在 F1 支持面；target 启用前必须证明查询账号无 UDF 使用授权，并保存 DBA 批准的 UDF
   inventory digest；Guard 拒绝 inventory 中的函数、qualified function 和 table function；授权或
   inventory 无法读取、发生漂移时整个 F1 target fail-closed，而不是把所有未知函数误当 UDF；
6. 普通注释可以保留并随原 SQL 执行；注释内容始终按 ExternalContent 处理，不能改变策略；
7. 只接受 §3.1 的 JOIN 分布 hint；comment hint、未知 hint 和资源/session hint 拒绝；
8. SQL 原文 SHA-256 必须与提交或确认时的 artifact、计划和本次 ToolCall 一致；
9. SQL 中可以有自己的 LIMIT，也可以没有；Guard 不修改它，最终返回上限由 session 和应用共同限制。

AST 解析成功不代表语义一定可执行；不存在的列、类型不匹配和 StarRocks 运行错误按结构化查询失败
返回，不自动修改或重试。

### 6.3 ToolPolicy

F1 增加独立 policy profile，只允许：

- capability `starrocks.readonly_query@1.0.0` 的 `describe_query_tables` 与
  `execute_readonly_query` 两个 operation；
- metadata operation 为 `EffectClass.READ + ReadClass.BOUNDED`，执行 operation 为
  `EffectClass.READ + ReadClass.RESTRICTED`；
- 当前 context 和 target 的 tenant/environment 精确一致；
- 当前启用的 target、当前配置版本和预算快照；
- `timeout_seconds <= min(target 配置值, 系统硬上限 180)`。

查询无需 ApprovalGate，但 `RESTRICTED` 分类不可降级成 `BOUNDED`。慢查询继续使用原 profile；通用 SQL
不能借慢查询 operation 或 surface 执行。

## 7. 查询资源阀值与 StarRocks 会话

### 7.1 F1 系统硬上限

| 项目 | F1 硬上限 | 作用 |
| --- | ---: | --- |
| 保存的预览行 | 1000 行 | 结果 artifact 边界 |
| 保存的预览字节 | 20 MiB | 按规范序列化后的行数据累计，不含 SQL 原文 |
| 查询执行时间 | 180 秒 | session 与调用截止时间 |
| 单 target F1 并发 | 1 | 应用侧排队，避免普通用户并发压垮集群 |
| SQL 语句数 | 1 | 拒绝脚本与多语句 |
| SQL 原文字节 | 1 MiB | 支持千行 SQL，同时限制入口、解析器和短期存储负载 |

这些值是本版本的系统红线，Web 不能调高。提高红线需要修改版本化 policy、补性能证据、复审并发布，
不能通过数据库全局变量或普通 Admin 点击绕过。

单 target 并发闸必须跨 task-worker 进程生效，并在取得 StarRocks 连接前获得；不能只用进程内
`Semaphore`。等待中的任务保留 queued 状态，不计入 180 秒执行时间，并继续服从既有取消、lease 和
任务重试边界。

### 7.2 每 target 的 Web 可调值

`StarRocksResource` 增加 F1 查询策略，`LOCAL_ADMIN` 可在现有 Web 资源管理页调低：

- `preview_max_rows`：`1..1000`，默认 1000；
- `preview_max_bytes`：`1 MiB..20 MiB`，默认 20 MiB；
- `query_timeout_seconds`：`1..180`，默认 180。

单 target 并发在 F1 固定为 1，只读展示，不提供编辑框。原始 `sql_select_limit`、resource group、
`query_mem_limit`、host、凭据和任意 session 变量不作为 F1 查询策略表单字段。

每次保存都使用现有 resources generation、Admin 审计和原子文件写路径。修改后的 target 默认值只用于
新建查询；执行计划保存实际预算快照。若当前系统 policy 被紧急收紧或 target 被禁用，即使旧计划使用
旧快照，Admission 仍按当前 policy fail-closed。

### 7.3 每次调用的会话设置

每次查询使用 call-local connection，不自动重试，不使用 `SET GLOBAL`：

1. 设置并回读 `query_timeout = effective_timeout_seconds`；
2. 设置并回读 `sql_select_limit = effective_preview_max_rows + 1`；
3. 确认当前 catalog/target、只读身份、resource group 和配置 revision；
4. 执行用户确认的原 SQL；
5. 在 `finally` 关闭连接，不把本次 session 设置泄漏给下一查询。

例如预览配置为 1000 行时，session 使用 `sql_select_limit=1001`：保存前 1000 行，第 1001 行只用于
判定 `has_more=true`。配置为 500 行时自动使用 501。管理员和用户都不直接填写这个变量。

`sql_select_limit` 只限制返回行，不限制扫描、JOIN、排序或聚合开销。真实 target 启用前还必须由 DBA
提供专用只读账号、专用 resource group、查询内存/CPU/扫描限制和 query queue 口径；这些集群级值
依赖实际 StarRocks 版本和资源，F1 不在 Web 中臆造通用数字。缺少任一项时真实装配保持关闭。

参考 StarRocks 官方说明：[System variables](https://docs.starrocks.io/docs/sql-reference/System_variable/)、
[Resource groups](https://docs.starrocks.io/docs/administration/management/resource_management/resource_group/)、
[Query queues](https://docs.starrocks.io/docs/administration/management/resource_management/query_queues/)。

## 8. 预览采集、正确性和结果状态

adapter 只执行一次 SQL，并使用无缓冲/流式游标分批读取；批大小是实现细节，不能成为用户可见的
查询语义。F1 不使用 `LIMIT/OFFSET` 循环。

结果按列元数据加行数组保存为有序 chunk；单个 chunk 固定最多 100 行，未来 F2 可直接按 50 或
100 行投影页面。任意数据库
类型必须先转成确定性的展示标量，Decimal、日期时间和大整数不得经浮点转换丢精度；二进制值不在
F1 展示面，遇到时按结构化不支持失败处理。

终态语义：

| 情况 | artifact 状态 | 行为 |
| --- | --- | --- |
| 实际结果不超过行/字节上限 | `complete` | 保存全部返回行，`has_more=false` |
| 读到第 `max_rows + 1` 行 | `truncated_rows` | 只保存前 `max_rows` 行，`has_more=true` |
| 下一完整行会超过字节上限 | `truncated_bytes` | 不保存半行，`has_more=true`，记录已保存字节数 |
| 超时、取消、连接错误、server error | `failed` | 丢弃未完成 chunks，不把部分结果标成可用预览 |
| 结果存储或 finalize 失败 | `failed` 或任务 `indeterminate` | 不返回成功链接；由既有 durable-attempt 规则判定 |

查询执行成功但预览被截断仍是可解释的成功：页面必须明确显示截断原因。命中预览上限不代表数据库
只扫描了这些行，也不代表未来导出只有这些行。

查询超时依靠 session `query_timeout` 与客户端截止时间共同关闭连接；F1 不自动发 `KILL QUERY`，因为
它是单独的目标控制能力。网络中断后无法证明服务端已停止时，审计保留 query id 和未知终止事实，
不得伪造“已取消成功”。

## 9. Result Artifact、ACL 与 24 小时保留

### 9.1 数据形状

每个执行任务生成一个 CSPRNG 不可枚举的 `result_ref`。结果域至少保存：

- task id、requester、tenant、environment、target fingerprint；
- `sql_ref`、SQL hash、SQL source；
- query policy/config revision 与预算快照；
- query id、开始/结束时间、queue/execute duration；
- 列元数据、保存行数/字节数、`has_more`、截断原因；
- artifact 状态、创建时间、`expires_at`；
- 有序结果 chunks。

SQL 原文只在 `SqlArtifactStore` 保存；ResultArtifact 只引用 `sql_ref` 和 hash，避免多份原文产生不同
清理真源。Evidence 和长期审计不得复制 SQL 原文或结果行。

### 9.2 ACL 与页面

F1 的结果 ACL 只有 requester。当前认证 principal 必须与 artifact 的 requester、tenant 和 environment
全部一致；Admin 身份本身不越权。not-found、已过期和无权访问返回同样的隐藏式拒绝。

F1 可以注册 `/results/{result_ref}`，但页面只能显示：

- 查询状态、target 展示名、开始/结束时间；
- 实际预算、保存行数/字节数、是否截断和过期时间；
- “详细结果尚未获 F2 查看审批”的锁定提示；
- requester 自己提交或确认过的完整 SQL；SQL 不进入飞书卡片、普通 RenderPayload 或群消息。

页面和 API 均不得返回列名、真实单元格、结果 chunk 或可推断其内容的摘要。F2 获批并实现独立 ACL
后，才允许从 ResultArtifactStore 分页读取保存的预览；翻页不重新查询 StarRocks。

### 9.3 保留与删除

- SQL candidate 未确认：从创建起 24 小时过期；
- schema context：从创建起 24 小时过期，候选生成失败也按同一规则清理；
- 已执行 SQL 和预览：从查询终态起 24 小时过期；
- 过期后应用读取立即拒绝，retention worker 删除 SQL 原文和所有结果 chunks；
- 长期只保留 SQL hash、actor、target、时间、预算、Guard/Policy 决定、query id 和资源指标；
- 日志、trace、Admin 审计、TaskStore、Evidence 和备份不得新增 SQL/结果副本。

PostgreSQL `DELETE` 不能证明磁盘页或既有备份立即物理擦除。正式部署前必须把 artifact 表的备份策略、
VACUUM/存储回收和实际数据处置能力列入现场验收；未验证时只能声明“应用层 24 小时不可访问并已删除
活动记录”，不能宣称物理介质已清除。

## 10. 与 F2、F3 的边界

F1 只提供后续能力所需的稳定输入，不实现它们：

- F2 读取 F1 保存的最多 1000 行预览，以每页 50 或 100 行分页；翻页不访问 StarRocks；
- F2 查看审批与 F3 导出审批互不授权；
- F3 审批绑定原 SQL hash、target fingerprint 和 requester，批准后在新连接、新 session 中重新执行
  同一份原 SQL；
- F3 不导出 F1 的 1000 行预览，也不使用 `LIMIT/OFFSET` 循环；它执行一次 SQL，用流式游标
  `fetchmany()` 分批写 CSV；
- 若 F3 最大允许 200000 行，则该导出 session 使用 `sql_select_limit=200001`，第 200001 行只作
  超限哨兵；F1 的 1001 不会影响 F3；
- F1 时间 `T1` 的预览与 F3 时间 `T2` 的导出允许因数据变化而不同；两次时间都必须展示，产品不承诺
  快照一致性；
- SQL artifact 已过期或 target/policy 漂移时不能导出，要求用户重新提交查询。

F3 的行数、文件大小、超时、CSV 注入防护、下载票据和完整性判定仍需单独设计与批准，不能从本文的
示例值外推授权。

## 11. 失败、取消与观测

- SQL parse/Guard 拒绝：不连接 StarRocks，不记录原始解析异常正文；Web 对 requester 展示闭集原因；
- policy、target、配置或 hash 漂移：Gateway 调用次数为 0；
- target 排队：展示 queued；等待时间与 180 秒执行时间分开统计；
- 用户在排队阶段取消：不取得连接；执行阶段取消只关闭客户端连接，不宣称 server 已确认取消；
- timeout、连接、认证、权限、资源组、队列、server 和结果存储错误映射为结构化错误；不回传上游原文；
- F1 不自动重试查询，避免昂贵 SQL 被重复执行；用户明确重试会创建新任务和新 result_ref；
- 必须记录 query id、queue time、execution time、returned rows/bytes、scan rows/bytes、CPU、peak memory、
  resource group、截断原因和关闭结果；目标版本取不到某指标时显式标记 unavailable，不填 0。

监控指标只用于观测和后续调参，不能让模型据此扩大预算或自动执行下一条 SQL。

## 12. Web 配置与权限边界

当前 Web 已有 StarRocks resource 登记和 resources generation，但现行 ADR-007 RI5 Amendment 明确
排除了 StarRocks 连接测试和真实目标路径。F1 需要窄修订：

- 复用现有 resource 页面、`IntegrationConfigService`、resources generation、原子写和 AdminAudit；
- `LOCAL_ADMIN` 才能修改 F1 查询策略；飞书 Admin 只可按既有能力查看脱敏生效状态；
- Web app 只写配置，不能装配 Runner、ToolGateway 或 StarRocks client；
- target-worker 在启动/重载时读取并验证策略，产出带 generation 的加载回执；
- “已保存”不等于“worker 已加载”，页面分别显示 saved generation 与 loaded generation；
- 页面不提供任意 policy 表达式、SQL 规则、hint 白名单、resource group、session 变量或连接测试按钮；
- 真实查询仍只由 task worker 经 Runtime 主链执行。

## 13. 文档和 ADR 变更门

源码实现前至少需要一组文档修订：

1. `2026-09-26-feature-roadmap-direction.md` 与 `DEVELOPMENT_PLAN.md`：替换“仅确定性模板”旧口径；
2. `ARCHITECTURE.md`：增加短期 SQL/result artifact、确认链和 query profile；
3. ADR-007：登记第四个 capability、F1 真实调用类别和生产/测试授权；
4. ADR-012：把现有两个慢查询 operation 的窄 adapter 边界扩展为单独 F1 adapter/profile，仍禁止
   fallback 和通用连接选择；
5. ADR-013：允许受保护的 result deep link，同时保持飞书/RenderPayload 无结果行；
6. ADR-015：若启用真实自然语言 SQL，批准模型接收的 schema context 与候选 SQL 出站/入站边界；
7. ADR-017：登记 SQL candidate/confirmation 与 interaction 路由，不把 SQL 塞进现有 confirmed slots；
8. 新 ADR：冻结 SqlArtifact、SchemaContext、ResultArtifact、24 小时保留、ACL、plan/call hash 绑定
   和 F2/F3 消费契约。

以上修订必须先获评审；不能只改代码或 feature flag 静默放宽。

## 14. 验证与验收矩阵

### 14.1 离线必验

- 单元：SQL artifact hash、预算求交、字节累计、chunk 分页、过期、hint/AST 分类；
- 契约：直接 SQL、模型候选确认、Plan/Admission/Gateway/adapter/result store、Web 配置和锁定结果页；
- 安全：多语句、DDL/DML、SET_VAR、UDF/table function、外部 catalog、SQL/target/hash 漂移、结果 ACL、
  Admin 越权、日志泄漏、过期竞态和 Gateway 零调用；
- 集成：PostgreSQL migration、insert-once/单次确认、并发 finalize、retention、正式 Web 路由；
- eval：自然语言候选只生成不执行、完整 SQL 确认、复杂 JOIN/CTE/UNION、歧义时澄清；
- 变异反证：移除 hash、ACL、单语句、只读节点或当前 policy 校验中的任一保护时，对应测试必须转红；
- 项目最终门：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。

### 14.2 真实 target 现场门

离线验收不能证明 StarRocks 性能或生产安全。真实调用前必须固定 exact SHA、StarRocks 版本、target、
只读 grants、无 UDF/外部 catalog 权限、resource group、query queue、内存/扫描限制、测试数据处置、
时间窗和回滚方式，并取得现场 GO。

现场至少验证：

1. session 变量设置/readback 与连接关闭；
2. 1000/1001 行哨兵、20 MiB 字节截断和 180 秒 server timeout；
3. 一个成功的复杂 JOIN/CTE/window SQL；
4. 一个高扫描低返回 SQL 在 resource group 内受控；
5. 同 target 第二条请求排队而不是并发进入；
6. 外部 catalog、UDF、SET_VAR、写语句和 target 漂移均在执行前拒绝；
7. query id 与 Profile 指标可以关联，取消/断连不被伪报；
8. 24 小时应用层清理和部署环境数据处置证据。

最高证据先标记为 `tests + test-env verified`；它不等于生产部署、canary 或 UAT。

## 15. 推荐实施切片

F1 是深档变更，按可独立复审的结果拆分，不按文件数量拆：

1. **F1-A 文档与契约**：修订路线/ADR，新增 SQL/result 契约和 migration；无查询执行；
2. **F1-B 共享治理内核**：profile 化 SQLGuard/StepAdmission，保持慢查询反例全绿；
3. **F1-C 直接 SQL 闭环**：用户原 SQL → plan/hash → fake adapter → 锁定 artifact；
4. **F1-D 预览持久化与 Web 壳**：chunks、ACL、retention、锁定 `/results/{result_ref}`；仍不展示行；
5. **F1-E Web target 阀值配置**：复用 resources generation/AdminAudit，worker 加载回执；
6. **F1-F 自然语言候选确认**：受限 schema context、候选 artifact、完整展示、确认后新任务；
7. **F1-G 离线总验收**：正式入口、全量/安全/eval、exact-SHA 独立审查；
8. **F1-H 真实目标验证**：另写现场计划并单独 GO，不与离线实现授权捆绑。

每个切片必须在前一切片的接口上增量完成；不得并行建设另一套 SQLGuard、结果服务或 StarRocks
connector。F2/F3 只能在 F1 离线验收及各自设计获批后启动。

## 16. 已接受的主要权衡

- **保留原 SQL而不重写**：避免系统改写改变语义；代价是依赖 session limit、资源隔离和用户确认；
- **预览有界，导出未来重跑**：F1 对集群和 Web 可控；代价是 T1/T2 数据可能不同；
- **同 target 并发先固定为 1**：安全优先；代价是高峰时排队，待真实指标证明后再调；
- **PostgreSQL 保存短期预览**：复用现有基础设施；代价是物理擦除受 MVCC/备份约束，必须诚实验收；
- **模型只产候选**：保留自然语言体验，同时不把执行权交给模型；代价是多一次用户确认；
- **F1 不做 CSV**：避免把预览边界误当完整导出；F3 使用独立审批、预算和流式执行。
