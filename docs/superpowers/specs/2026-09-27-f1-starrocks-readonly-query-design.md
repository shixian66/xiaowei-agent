# F1 StarRocks 受治理只读查询与锁定预览设计

> 状态：Approved v7。独立复审 SHA `e2727cf117b62f9c9a23ec1ce37c046312475f5a` 通过，经 PR #108 合并（`91cadd43e13a4b1178fbddcbdfd4b41bf0decb36`）。
> F1-0 真源修订与实施计划见 [F1 实施计划](../plans/2026-09-27-f1-starrocks-readonly-query.md)。
> 日期：2026-09-27。
> 设计基线：c69a09bd8595df9808754b5e4272b7c95ee78a43。
> 审查修复基线：016eae3d4bb07c9cc592effd368929a112d83d3c。
> 第二轮复审基线：3a48cbe36d40a9d4fcece7e68ad25e718f6a929c。
> 本轮策略调整基线：8f721c1965a9b217cde1bb4787a12e7673ca4880。
> 第三轮复审基线：08e6aafa9c664bd15fd232be3edb2129d4552210。
> 增量登记决策基线：66bdd92eec082ac6b67f9a5a376317bdb7b6e8f1。
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

F1 在已验证 StarRocks 小版本区间和“内部数据源”范围内，支持 sqlglot 能完整证明的只读 AST，以及
`ReadonlyStatementRegistry` 首批登记的 §7.3 语句族和常用只读语句。Registry 不要求把目标版本全部只读
语句登记完整才开放 target；未登记或尚不能证明的语句在发送前返回结构化“暂未支持”，不影响同一 target
继续服务已支持语句。两条证明路径仍由同一个 SQLGuard 做单语句、语法形状、目标、Catalog、黑名单和
副作用校验。F1 不再维护业务数据库白名单；StarRocks 专用 credential 的对象权限是授权真源，
ResourceSnapshot 中默认空的关系黑名单只做额外拒绝。写入、DDL、锁、导出和 session 改写仍在发送前
拒绝；DDL 等写能力留给未来独立设计。外部 Catalog、table function、UNNEST、UDF 和 hint 仍是本阶段
明确不支持的边界。

自然语言生成 SQL 仍是已确认的产品方向，但拆成 F1-NL 单独设计、单独批准。“模型只产候选、完整
展示、用户确认后才可执行”只是 F1-NL 待批准的设计方向，不代表 AGENTS.md 已允许模型产生可执行
SQL；F1-NL 也不能借本设计进入实现。

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
| P1-7 AST、SHOW、DESC 和 hint 规则有缺口 | 成立 | 项目锁定 sqlglot 30.17.0 实测不能可靠区分注释 hint，SHOW catalog 不一定成为 Table，EXPLAIN 与 DESC 共用 Describe 形状；见 §7 |
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

### 2.3 第二轮复审意见

| 意见 | 核对结论 | 证据与处理 |
| --- | --- | --- |
| 新 P1：等待 target slot 会卡住唯一 worker | 成立 | 当前 worker 对 candidates 串行 await，且 begin_step_attempt 在 Gateway 前消耗预算；按 §8.4 和 §10 改成非阻塞资源退让 |
| P2-1：sqlglot 版本写错 | 成立 | uv.lock 与项目锁定环境都是 30.17.0；已修正文档和 probe 口径 |
| P2-2：1 MiB SQL 过不了请求体门 | 成立，原意见措辞需收窄 | JsonBodyLimitMiddleware 全局挂载但只对登记路径生效；现有唯一 limit 默认 128 KiB 且配置上界小于 1 MiB。F1 改用 §5.1 的原始 SQL body 和路由级策略 |
| P2-3：系统库未定义 | 成立，后续产品策略已变更 | 旧稿没有说明系统库的数据面；v5 不再按库拒绝，information_schema 等内部系统库由 credential 权限控制，并可按 §6、§7.3 的关系黑名单额外拒绝 |
| P2-4：同步线程如何写异步 sink 未定义 | 成立 | 当前 StarRocks adapter 使用 asyncio.to_thread，而 PostgreSQL store 为 async；§5.5 冻结线程桥、背压和迟到写 fencing |
| P2-5：契约测试绑定完整原句 | 成立但不阻断设计 | 测试改为按章节锚点检查关键语义；仍只是一层文档守卫，不冒充产品行为测试 |

没有把第二轮 P2-3/P2-4 留到实施时临场决定：两者分别关系数据访问边界和超时后的写入隔离，属于本设计
已有 SQLGuard 与 ResultChunkSink 主链，应在进入详细计划前闭合。v5 根据负责人后续决策把数据库级
allowlist 改为 credential 授权加关系级额外拒绝；这不是把应用黑名单冒充权限系统，真实权限仍由 F1-H
验证的 StarRocks grants 承重。

### 2.4 修复后独立复核

| 意见 | 核对结论 | 共同根因与处理 |
| --- | --- | --- |
| 恢复分类晚于资源检查，deadline 可能在首次 BUSY 崩溃后重置 | 成立 | 调度恢复与资源争抢次序没有形成一份状态机；§8.4 改为 inspect → 新执行水合/准入 → 持久化 deadline → deadline check → try_acquire → begin，并在 lease 写事务内重验 deadline |
| 原始 SQL bytes 放入 ToolCall.typed_args 不可实现 | 成立 | 把进程内敏感材料误塞进只接受 JSON 标量的公共契约；§5.4 拆成标量 ToolCall 与不可持久化 HydratedQuery，并用 sql_hash 绑定 |
| SHOW DATABASES 绕过业务库 allowlist | 在 v4 的 allowlist 模型下成立；v5 已被产品决策取代 | v5 删除数据库 allowlist，原样允许 SHOW DATABASES；结果以 StarRocks 对专用 credential 的实际返回为准，不做过滤或改写 |
| 文档测试只看词存在可误绿 | 成立 | 旧守卫无法证明顺序和否定语义；契约测试改为检查规范顺序、决策表单元格及允许/拒绝分区 |

### 2.5 负责人确认的策略调整

本轮不是给 SHOW DATABASES 增加一个名称特判，而是修正“发现能力、应用策略和数据库授权混在一起”的
共同根因：

1. 内部数据源只读语句按 AST 证明路径和增量 ReadonlyStatementRegistry 提交；parser 覆盖缺口不自动
   变成永久产品限制，但未登记语句在补充 descriptor 前明确返回“暂未支持”；
2. SHOW DATABASES、SHOW TABLES 和内部系统库不再因为数据库级 allowlist 被统一拒绝；
3. ResourceSnapshot 改用默认空、精确到 database.object 的关系黑名单，只额外拒绝指定表、视图和物化视图；
4. credential 的 SELECT grants 才是授权边界；黑名单不能授予权限，也不能替代 DBA 撤权；
5. DML、DDL、锁、文件、导出、session 改写和其他副作用仍拒绝；未来 DDL 必须使用新 capability、policy
   profile、审批和写操作 readback，不借 F1 放开。

此前已经确认暂不设计的外部 Catalog、table function、UNNEST 和 UDF 继续不在 F1 范围；hint 也继续按
§7.2 的 parser 证据拒绝。这些是明确的外部数据源/执行语义边界，不属于已删除的数据库白名单。

### 2.6 第三轮复审与负责人决定

| 意见 | 核对结论 | 共同根因与处理 |
| --- | --- | --- |
| P1：规格承诺只读 SHOW，但 sqlglot 30.17.0 对大量 StarRocks SHOW 降级为 Command 或 ParseError | 成立 | 产品能力错误地只绑定在第三方 parser 覆盖率上；按 §7.3 建立增量 ReadonlyStatementRegistry，首批覆盖已确认语句，不用泛化 SHOW 前缀放行 |
| P2：关系黑名单挡不住 information_schema 间接元数据 | 成立，不阻断 | 黑名单语义是阻止直接读取指定关系，不是元数据保密；§7.3 和 §16 明示列名、视图定义、查询文本和集群拓扑仍可能可见 |
| 同根因扩展：UNION/INTERSECT/EXCEPT 根节点不是 Select，EXPLAIN LOGICAL 也可能 ParseError | 成立 | AST 根类型与兼容登记表一起覆盖，不再用“根必须是 SELECT”或“parser 必须成功”代替只读证明 |
| 同根因扩展：ADMIN SHOW、SHOW PROFILELIST、ANALYZE PROFILE 等只读管理查询也有 parser 缺口 | 成立 | 纳入首批 registry 和现场权限验证；SQLGuard 支持语法不等于给 credential 增权，StarRocks 仍可按实际 grants 返回权限错误 |

负责人后续决定覆盖“完整登记才开放 target”的旧门：ReadonlyStatementRegistry **不要求**目标版本的
**全部只读语句**登记完整。首批登记 §7.3 表中各类和常用只读语句；未登记语句零发送并返回结构化
“暂未支持”，不影响 target 开放。Registry 与连续的已验证**小版本区间**绑定；就版本兼容判断而言，只有
实际版本超出区间才停用 target，既有的配置/digest 漂移保护不变。SQLGuard 支持语法仍不等于给
credential 增权；需要 OPERATE 或其他只读权限的已登记语句，
在专用 credential 没有该权限时，应像同一账号使用 SQL 客户端一样收到 StarRocks 权限错误。

### 2.7 六个共同根因

1. 输入类型没有与普通对话分离，导致 SQL 可能进入模型、TaskSubmission 和聊天入口；
2. Plan 中的引用没有一条受信的运行时水合链，导致 SQLGuard、hash 和 adapter 看见的内容可能不同；
3. 现有 ToolResult/Evidence 面向小型诊断行，不适合大结果、重复列名和短期敏感数据；
4. 当前把“等资源”和“已开始工具尝试”混在同一生命周期里，且 timeout/进程内并发语义默认工具可重试、
   可及时取消，不适合昂贵查询；
5. target、SQL 方言和 Web 前置条件分散在多个旧里程碑，未形成 F1 唯一真源。
6. 把 sqlglot 的方言覆盖率误当唯一能力边界，导致已决定支持的 StarRocks 只读语句被无意拒绝；修复后
   AST 与增量 Registry 共同定义当前已支持集合，未登记项可独立演进而不拖垮整个 target。

后续设计按这六个根因修正，不按评论位置或语句名称分别增加 operation 特判。

## 3. 产品范围

### 3.1 F1 范围

- capability 为 starrocks.readonly_query@1.0.0；
- capability 只有 execute_readonly_query 一个 operation；允许的查询、SHOW、DESC、EXPLAIN、只读 ADMIN
  SHOW 和 profile 查看都走这一条；
- 入口为已认证 Web 的显式 SQL 编辑和确认页面；
- 单条、多行、最长 1 MiB UTF-8 SQL；
- SELECT、CTE、JOIN、子查询、UNION、INTERSECT、EXCEPT、聚合、窗口函数和内建标量函数；
- Registry 首批覆盖 §7.3 表中各类和产品确认的常用只读语句，包括只读 SHOW、DESC/DESCRIBE、
  EXPLAIN、ADMIN SHOW 和 profile 查看；后续语句可增量登记，未登记项返回“暂未支持”而不关闭 target；
- target 内部 Catalog 的数据库、系统库、表、视图和物化视图；
- 普通行注释和普通块注释；
- 有界流式预览、锁定结果页、ACL、配额、24 小时保留和清理；
- 每个 target 可由 Web Admin 调低预览行数、字节数和查询超时，并维护关系查询黑名单；
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

未登记语法只表示当前 F1 暂未支持该请求：零 SQL 发送并返回结构化错误，同一 target 继续服务已支持
语句。新增支持必须先补 descriptor、正反例和 registry digest；不能用泛化 SHOW 前缀放行，也不能在
运行时猜测。

普通注释保留并随原 SQL 执行，但始终是 ExternalContent。由于当前 parser 证据不足，原稿允许的 JOIN hint
全部移出 F1；未来如恢复，必须固定 StarRocks/sqlglot 版本、定义可解析 AST 形状并补安全变异测试。

## 4. 不可破坏的不变量

1. SQL 原文只有 SqlArtifactStore 保存一份；TaskStore、PlanStore、Evidence、RenderPayload、日志和
   ChannelStore 只保存引用、hash 或安全摘要；
2. 确认页展示、SQLGuard 校验、ToolCall hash 和 adapter 执行绑定同一份原始 UTF-8 字节；
3. 缺少、过期、越权、hash 不符或 target 不符的 SQL artifact 一律在 Gateway 前拒绝；
4. operation 声明需要 SQL 时，缺少进程内 HydratedQuery 必须拒绝，不能沿用“没有 SQL key 就跳过”；
5. 结果行不进入 AdapterResponse.payload、ToolResult.data_view、Evidence 或 TaskOutcome；
6. 列按 ordinal 保存，不能用列名作 map key；同名列必须原样保留；
7. F1 查询一旦开始尝试，未知结果不能自动重放；
8. 单 target 并发闸跨 worker 生效，且只有确认连接关闭后才能主动释放；
9. Web 配置只有一个 task-worker 运行时真源，保存不等于加载，不热加载；
10. F1 任何真实 target 调用仍需独立现场 GO。
11. StarRocks credential 的对象权限是授权真源；应用关系黑名单只能缩小、不能扩大其权限面；
12. 黑名单、credential、target 或 config revision 任一漂移都必须在 Gateway 前拒绝旧计划。
13. ReadonlyStatementRegistry 与已验证小版本区间、policy snapshot 和 digest 绑定；未登记语句只拒绝
    当前请求，实际版本超出区间或登记漂移才在 Gateway 前使 target fail-closed。
14. AST 路径和兼容登记路径只分析原文及其精确字节切片；adapter 永远收到用户确认的原始 SQL，不能执行
    Guard 重建、格式化或改写的替代文本。

## 5. 输入、计划、准入和结果主链

### 5.1 Web SQL 草稿与确认

Web handler 只把认证上下文和以下 DirectSqlDraft 交给 application 层 QuerySubmissionService：

- resource_id；
- SQL 原始 UTF-8 bytes；
- draft idempotency key。

提交端点固定为 POST /app/api/sql-drafts。SQL 使用 Content-Type: application/sql; charset=utf-8 的原始
请求体传输；resource_id 和 idempotency key 分别使用单值、长度受限的 X-Xiaowei-Resource-Id 与
Idempotency-Key header，重复 header 一律拒绝，不把 SQL 放进 URL 或 JSON。
路由在读取和解码前按原始请求体执行 1_048_576 bytes 硬上限，拒绝 BOM、非法 UTF-8、空 body 和多余
content encoding。这样 SQL 上限就是用户确认并保存的 SQL bytes 上限，不会被 JSON 转义和 envelope
字段额外放大。

现有 api_request_body_limit_bytes 默认 128 KiB，且配置约束本身小于 1 MiB；它继续保护原有 JSON
写入口，不能被整体调高来迁就 F1。F1-0 复用现有 body-limit 实现，把它收成一份按精确 path、method、
media type 和 limit 匹配的路由策略表：原 JSON 路由保持原上限，SQL 草稿路由单独使用不可由 Web 调高的
1 MiB 上限。该策略不替代现有 session、scope、Origin/CSRF 和激活状态校验。未知写路由仍由现有安全测试
拒绝，不能再挂一个旁路中间件。

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

该 requirement 由 CapabilitySnapshot 派生，不能由 step 或用户自报。现有 ToolCall.typed_args 使用
FrozenMap，只接受 JSON 标量；原始 bytes 或嵌套 DTO 不能塞进去，也不能为了 F1 放宽这条公共契约。
confirmed_artifact 的通用 Runner 水合流程是：

1. 重新解析当前 target、policy 和 config revision；
2. 从 SqlArtifactStore 读取 sql_ref 一次；
3. 校验 task、actor、tenant、environment、target、confirmation_ref、expiry 和 SHA-256，创建不可变
   HydratedQuery；
4. 构造只含标量的 ToolCall，typed_args 保存 sql_ref、sql_hash、result_ref、resource_id、
   target_fingerprint、config_revision、confirmation_ref 和三个有效预算值；
5. StepAdmission 分别接收 ToolCall 与 HydratedQuery，先执行 ToolPolicy，再按 operation requirement 强制
   执行 confirmed_readonly SQLGuard，并校验所有引用、预算及 bytes hash 一致；
6. AdmissionCertificate 的 tool_call_hash 覆盖完整标量 ToolCall，其中 sql_hash 以 SHA-256 绑定原始 bytes；
7. ToolGateway 重算 tool_call_hash，并再次校验 HydratedQuery 的引用、target/config、预算及 bytes hash；
8. Gateway 创建私有、不可持久化的 StreamingAdapterRequest，把同一 SQL bytes 和 ResultChunkSink 交给
   target-bound adapter；adapter 在发送前最后一次计算 SHA-256，不重新读 artifact，也不接受另一份 SQL。

| 载体 | 可含 SQL bytes | 内容与约束 |
| --- | --- | --- |
| `ToolCall.typed_args` | 否，只允许 JSON 标量 | 引用、hash、target_fingerprint、config_revision 和预算标量 |
| `HydratedQuery` | 是 | 仅进程内；bytes 的 SHA-256 必须等于 ToolCall.sql_hash |

HydratedQuery 是 Runner 从受保护 artifact 水合出的运行时值，不是 Plan、ToolCall、TaskStore 或用户可构造的
输入。它只能作为 StepAdmission 和 Gateway 的受信 keyword-only 参数传递；StreamingAdapterRequest 只在
Gateway 内创建。任何 required query 缺失、字段多余、hash 不符或 artifact 无法读取都返回闭集拒绝，
Gateway 调用次数为 0。现有 template_locked 慢查询仍按原参数重编译和逐字节比对，不经
confirmed_artifact 分支。

ToolCall.timeout_seconds 由有效 query timeout 和 §8.3 的 gateway grace 确定，不再固定为 30 秒。该
水合规则属于 query requirement 的共享机制，不在 Runner 里判断 F1 operation 名称。

HydratedQuery 和 StreamingAdapterRequest 只在当前进程内存中存活；trace/audit 不得序列化它们，也不得
展开 ToolCall.typed_args，只记录 sql_ref、SQL hash、tool_call_hash 和闭集决定。解析、driver 和 server
error 也不能回显 SQL 片段。

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

ResultChunkSink 的持久化接口保持 async；运行在 asyncio.to_thread 中的同步 PyMySQL 读取循环只拿到
Gateway 创建的 ThreadsafeResultChunkWriter，不直接持有 PostgreSQL store，也不在工作线程另起 event
loop。writer 在创建它的主 event loop 上用 asyncio.run_coroutine_threadsafe 提交一个 chunk，并同步等到
该 chunk 的持久化回执后才继续 fetchmany；Future.result 的等待上限取 sink write 上限与剩余 Gateway
deadline 的较小值。任一时刻最多一个 chunk 在途，形成明确背压，不建无界队列。

每次 write 都携带 task、step、result_ref、task fencing 和 result fencing。Gateway timeout 或取消时，
async 侧必须先把 sink 原子标为 abort 并轮换/撤销 result fencing，再向调用方返回；此后仍在运行的同步
线程产生的迟到写入、seal 或 activation 全部被 store 拒绝。writer 收到 closed/fenced 结果后令 adapter
退出读取并在 finally 关闭独占 connection。无法确认线程和连接已经退出时不主动释放 target slot，只等
§8.4 的 lease TTL；staging chunks 最终由 retention 清理。

abort 持久化失败时，Gateway 不能返回成功、不能 seal/activate，也不能释放 slot；旧 fencing 下可能迟到的
chunk 最多仍是不可见 staging 数据。恢复路径先幂等补写 abort/过期状态，再由 retention 清理，不能把存储
故障吞成正常 timeout。

正常路径只有在 to_thread 已返回“连接已关闭”的结构化回执后，Gateway 的 async 侧才可 seal；同步线程
不能直接 seal 或 activate。相关测试必须证明慢 PostgreSQL 写会反压 fetch、Gateway 超时后迟到 chunk
写不进去，以及移除 result fencing 后该反例会转红。

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
- default database 和规范化后的 blocked_relation_names 查询黑名单；
- DBA 带外批准的 StarRocks `verified_min_version`、`verified_max_version` 小版本区间、grants、identity、
  必要 DDL digest 及 source_ref；
- 与该小版本区间对应的 readonly registry profile/version/digest；
- resource group 标识和审批引用；
- F1 查询预算；
- enabled 与 generation。

blocked_relation_names 默认空，每项都是内部 Catalog 中精确的 database.object，不接受 catalog 前缀、
通配符、正则或只写 object 的歧义形式；object 可以是表、视图和物化视图。名称按 F1-H 验证后的 StarRocks
identifier 规则规范化，规范化冲突、非法名称或重复项使 target 启动失败。系统库对象与业务库对象使用同一
规则，没有隐式系统库全拒绝。

host、port、database、blocked_relation_names、username、TLS、secret ref、预算、readonly registry digest
和 preflight digest 全部来自同一 snapshot。
现有 XIAOWEI_STARROCKS_* 测试装配只保留给 M6b test_readonly profile，不能与 F1 ResourceSnapshot
同时启用；F1 release 装配发现双真源直接启动失败。

账号口径统一为：DBA 配置和批准的 F1 专用内部 Catalog 跨库只读账号，只向预期对象授予 SELECT 权限，
无写、管理、UDF、外部 Catalog 和文件权限。数据库 credential 的 SELECT 权限是实际安全授权边界；
blocked_relation_names 只是在权限面内额外拒绝，不能让无权限对象变得可读。如果某对象必须作为强安全边界
禁止访问，DBA 还必须撤销该表、视图和可能暴露它的其他视图权限，不能只依赖应用黑名单。Web 一个
StarRocks resource 只引用这一套 F1 credential，不再同时假设另一套“DBA 账号”。

### 6.2 确定性解析和漂移

task-worker 启动时由有效 snapshot 构造 F1TargetDirectory。CapabilityResolver 只根据：

- RequestContext 的 tenant/environment；
- 用户在 Web 选择的 opaque resource_id；
- 当前 snapshot 的 enabled resource；

解析唯一 ResolvedTarget。没有默认 target、名称猜测或跨 tenant fallback。target_fingerprint 继续由
canonical target 计算；adapter binding 还必须精确匹配 config revision。

配置保存后 Web 只显示 saved generation 和 restart_required。管理员在宿主受控重启 task-worker；
新进程校验成功后写 loaded receipt。旧进程不热加载。新 generation、host、credential、
blocked_relation_names、预算、enabled、已验证版本区间、preflight 实际版本、readonly registry
profile/digest 或其他 digest
任一变化都会使旧计划在 Admission 前因 config revision 或 target 漂移拒绝，Gateway 调用为 0。

preflight expected digest 只能来自负责人/DBA 的带外批准输入，不能用首次连接结果自我签名。task-worker
必须回读实际 StarRocks version；版本落在闭区间
`verified_min_version <= actual_version <= verified_max_version` 内即可加载当前 registry。只有实际版本
超出已验证小版本区间、区间本身非法或 registry digest 不匹配时，整个 target fail-closed。区间内遇到未登记
语句只拒绝该请求，不改变 target enabled 状态。

## 7. SQLGuard 与 ToolPolicy

### 7.1 一个内核，两个 profile

governance/sqlguard.py 仍是唯一 SQL 准入内核：

- template_locked：现有 starrocks.slow_query.diagnose 使用，保留模板重编译、参数闭集、LIMIT、
  表列和时间窗规则；
- confirmed_readonly：F1 使用，校验原文 hash、单语句、AST 或版本化 registry 只读证明、内部对象和 F1
  token 规则，不重编译、不改写 SQL。

StepAdmission 只能从 CapabilitySnapshot 取得 profile。慢查询计划如果被伪装成 confirmed_readonly，
必须因 operation/profile 漂移拒绝。

### 7.2 原文 token 扫描

AST 前增加一个 quote-aware lexer pass，识别语句边界、注释类别、token 类别及每个 token 在原始 UTF-8
bytes 中的起止位置。它为 §7.3 的兼容语法匹配和内层 query 精确切片提供证据，但不用正则或前缀判断
替代 AST/登记语法：

- 字符串、quoted identifier 和普通注释内部的分号不计为多语句；
- 普通 --、# 和不以 +/! 开头的块注释允许；
- 字符串/quoted identifier 外出现 /*+ 或 /*! 一律拒绝；
- 控制字符和 Unicode format 字符拒绝；
- 中文及全角字符可以出现在合法字符串和普通注释内；
- smart quote、全角标点若位于关键字、运算符或 identifier token 位置则拒绝。
- 字符串、quoted identifier 和普通注释外的 INTO OUTFILE 在所有证明路径中拒绝。

移除 token scan 后，comment hint 恶意矩阵必须转红。confirmed_readonly 不复用旧 SQLGuard 对整段文本的
全角字符禁令。

### 7.3 只读证明闭集

confirmed_readonly 只有一个 SQLGuard 和一个执行入口，但有两条互斥的只读证明路径。两条路径都先通过
§7.2，最终都产生同一种规范化 `ReadonlyProof`，再统一执行 Catalog、目标、blocked_relation_names、
副作用和原文 hash 校验。`sqlglot 30.17.0` 的覆盖率是实现事实，不是产品能力边界。

**AST 证明路径：** sqlglot 能完整解析时，必须恰好得到一条语句，且只允许：

1. 查询根节点 `Select`、`Union`、`Intersect` 或 `Except`，包括 CTE、JOIN、子查询、聚合和窗口函数；
2. parser 完整识别且没有写入或 session 副作用的 SHOW，包括 SHOW DATABASES、SHOW DATABASES FROM
   default_catalog、SHOW TABLES、SHOW COLUMNS 和 SHOW CREATE；
3. DESC/DESCRIBE 内部对象；
4. EXPLAIN 包装的内层 query 递归通过本节同一查询根节点、Catalog 和黑名单检查；EXPLAIN ANALYZE SELECT
   虽会实际执行查询，也使用同一查询预算、slot、流式结果和 timeout。EXPLAIN ANALYZE INSERT 等写形状
   即使数据库声称事务最终回滚，也不属于 F1；
5. 一段 object、两段 db.object，以及 catalog 明确为内部 default_catalog 的三段
   default_catalog.db.table；
6. information_schema、`sys`、`_statistics_`、`statistics` 等内部系统库读取，只要 credential 有权限且
   直接目标没有命中 blocked_relation_names。

**版本兼容证明路径：** sqlglot 30.17.0 把 StarRocks 只读语句降级为不透明 `Command` 或直接抛出
ParseError 时，SQLGuard 查询版本化 `ReadonlyStatementRegistry`。Registry 是 confirmed_readonly policy
snapshot 内的只读语法描述数据，不是第二套 parser、第二个 Guard 或第二条执行链。每个 descriptor 必须
继承 registry profile 的已验证小版本区间，并声明 quote-aware token grammar、允许/禁止的 clause、目标
类型、所有直接对象的唯一提取规则、Catalog 规则和副作用排除规则；不得用“以 SHOW 开头”这类泛化正则
放行。首批登记下面 sqlglot probe 已确认的语句族，并在 F1-0 补入产品确认的常用只读语句。Registry
**不要求**把目标版本的只读语句**完整**登记才开放 target；下表是首批最低范围，不是最终清单上限：

| 只读语句族 | descriptor 必须证明的附加约束 |
| --- | --- |
| SHOW CREATE MATERIALIZED VIEW | 唯一提取 relation 目标并检查内部 Catalog 与黑名单 |
| SHOW MATERIALIZED VIEWS | 提取 database 范围；显式 relation 变体仍检查黑名单 |
| SHOW PARTITIONS、SHOW TABLET | 唯一提取 relation 目标并检查黑名单 |
| SHOW DATA | 区分无目标、database 和 relation 形状；有 relation 时检查黑名单 |
| SHOW LOAD、SHOW ROUTINE LOAD、SHOW FUNCTIONS | 校验 database/listing 形状，不把过滤表达式当任意子语句执行 |
| SHOW CATALOGS | 只允许列举 Catalog 元数据；不得借此读取外部 Catalog 数据 |
| SHOW FRONTENDS、SHOW BACKENDS、SHOW RESOURCE GROUPS、SHOW PROC、SHOW PROFILELIST | 仅允许登记的只读系统/listing 形状，保留其元数据暴露审计 |
| SHOW ALTER TABLE | 校验登记的 ALTER 任务 listing 形状；语法中存在直接 relation 目标时提取并检查黑名单 |
| ADMIN SHOW REPLICA STATUS、ADMIN SHOW REPLICA DISTRIBUTION 及目标版本登记的其他 ADMIN SHOW | 只允许只读形状，唯一提取每个直接 relation 目标并检查黑名单；实际 OPERATE 等权限仍由 credential 决定 |
| ANALYZE PROFILE | 只允许读取既有 profile 的登记形状，不允许拼接第二条语句或执行 SQL 文本 |
| EXPLAIN LOGICAL、EXPLAIN VERBOSE、EXPLAIN COSTS | lexer 只定位 mode 与内层 query 的精确 byte slice；内层必须由 AST 证明为上述只读查询并通过 Catalog/黑名单检查 |

兼容路径只用切出的内层 query 做校验；确认页、hash、审计引用和 adapter 执行的始终是同一份**原始 SQL**
bytes，不能执行 lexer/sqlglot 重建、格式化或改写后的文本。新增语句支持时先增加 descriptor、正反例和
registry digest；不要求补齐同版本的其他只读语句，也不需要关闭区间内的 target。

拒绝：

- 所有 DML、DDL、写入、锁、文件、导出、事务、session/变量改写、动态 SQL、procedure，以及其他有
  副作用的 AST；
- parser 降级为 Command/ParseError 后没有匹配当前 registry descriptor 的语句，即未登记或未知只读
  前缀；这类请求返回 `error_code=READONLY_STATEMENT_NOT_SUPPORTED`、用户文案“暂未支持”、
  `retryable=false`，Gateway/adapter 零 SQL 发送，但 target 保持 enabled；
- 已登记语法不完整、歧义匹配、目标缺失或无法唯一提取直接对象目标的语句；
- 任何多语句、hint、INTO OUTFILE，或 descriptor 明确排除的可执行 clause；
- 任意 AST 位置引用外部 Catalog，或 SHOW DATABASES FROM 等 SHOW 明确指向外部 Catalog；
- SELECT、EXPLAIN 查询、DESC、SHOW CREATE、SHOW COLUMNS 或其他对象级元数据读取精确命中
  blocked_relation_names；
- table function、UNNEST、qualified function；
- optimizer/resource/session/version comment hint。

以上拒绝都发生在 adapter 前，保证 Gateway/adapter **零 SQL 发送**；不能把“交给数据库试一下”当作
无法证明只读或无法唯一提取目标时的退路。未登记是请求级不支持，不得写成 target 不可用；只有实际
StarRocks 版本超出已验证小版本区间等 §6.2 漂移才关闭 target。

一段对象名按 target 配置的 default database 确定性解析；两段 db.object 直接使用指定库；三段名只允许
catalog 规范化后等于 default_catalog。CTE alias 和 derived table 不被误当基础关系。Guard 遍历 SELECT
及 EXPLAIN 内层 query 的每一个直接基础关系，并解析 DESC、SHOW CREATE、SHOW COLUMNS 及 registry
descriptor 声明的每个直接对象目标；规范化后的 database.object 与 blocked_relation_names 精确命中即在
发送 SQL 前拒绝，不做前缀、通配符或正则匹配。

SHOW DATABASES 没有关系目标，直接放行；SHOW TABLES 只列名称，也允许显示黑名单对象的名称。黑名单的
语义是“不准直接查询指定关系或对它执行直接对象级元数据命令”，**不提供元数据保密**，也不隐藏数据库或
对象存在性。只要 credential 有权限，用户仍可能通过 `information_schema.columns` 看到列名，通过
`information_schema.views` 的 `view_definition` 看到视图定义，也可能从 SHOW PROFILELIST、SHOW PROC
或其他只读系统查询看到查询文本、任务名称和集群拓扑。SQLGuard 不尝试解析 information_schema 的
WHERE 谓词来推断其描述了哪个黑名单对象，因为这既不能形成可靠保密边界，也会误伤普通元数据查询。
如元数据也必须隐藏，应由 StarRocks 权限/安全视图承重，或未来另行设计独立隐藏策略。

SQLGuard 不递归展开视图定义，也不声称能从查询文本发现视图背后的基础表。直接查询一个未列入黑名单、
但底层引用黑名单表的视图仍可能被 StarRocks 执行。因此数据库 credential 的对象级 SELECT grants 才是
强安全边界：敏感表及可能暴露它的视图必须由 DBA 撤权，或把表、视图和物化视图分别加入黑名单。重命名、
新增视图或权限变化必须生成新 config/grants digest；未通过 preflight 的 target 不开放。

[SHOW DATABASES](https://docs.starrocks.io/docs/sql-reference/sql-statements/Database/SHOW_DATABASES/) 明确支持
当前内部 Catalog 和指定 Catalog；F1 只允许内部 default_catalog。
[StarRocks Information Schema](https://docs.starrocks.io/docs/sql-reference/information_schema/) 将其定义为
只读系统视图集合，因此它适用同一 credential 与关系黑名单规则，不再整库排除。
[SHOW MATERIALIZED VIEWS](https://docs.starrocks.io/docs/sql-reference/sql-statements/materialized_view/SHOW_MATERIALIZED_VIEW/)、
[SHOW PARTITIONS](https://docs.starrocks.io/docs/sql-reference/sql-statements/table_bucket_part_index/SHOW_PARTITIONS/)、
[EXPLAIN](https://docs.starrocks.io/docs/sql-reference/sql-statements/cluster-management/plan_profile/EXPLAIN/)、
[SHOW PROFILELIST](https://docs.starrocks.io/docs/sql-reference/sql-statements/cluster-management/plan_profile/SHOW_PROFILELIST/)
和 [ANALYZE PROFILE](https://docs.starrocks.io/docs/sql-reference/sql-statements/cluster-management/plan_profile/ANALYZE_PROFILE/)
证明这些是 StarRocks 的只读查询面，但它们的元数据可见性和所需权限不同。
[ADMIN SHOW REPLICA STATUS](https://docs.starrocks.io/docs/sql-reference/sql-statements/cluster-management/tablet_replica/ADMIN_SHOW_REPLICA_STATUS/)
与 [ADMIN SHOW REPLICA DISTRIBUTION](https://docs.starrocks.io/docs/sql-reference/sql-statements/cluster-management/tablet_replica/ADMIN_SHOW_REPLICA_DISTRIBUTION/)
需要额外权限；F1 支持语法但不提升专用 credential，数据库权限拒绝按结构化执行错误返回。
[StarRocks 权限概览](https://docs.starrocks.io/docs/administration/user_privs/authorization/user_privs/) 将 TABLE、
VIEW 等对象的 SELECT 权限作为授权项。不存在、无权限、类型错误或其他 StarRocks 执行错误结构化返回，
不自动修改或重试。

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
| target slot 累计等待 | 300 秒 | 多次非阻塞退让共用同一 deadline；超时后不建立连接 |
| 单 target 活跃查询 | 1 | 跨 task-worker |
| SQL 语句数 | 1 | 不接受脚本 |
| SQL 原文 | 1 MiB UTF-8 | 需通过近上限解析资源测试 |
| 单 requester 存活 query set | 5 | 每组含一份 SQL 与至多一份 result |
| 单 target 存活 query set | 50 | 结果行数据最大约 1 GiB |

Web 不能调高硬上限。提高任何红线都要修改版本化 policy、补性能证据并复审。

### 8.2 Admin 可调值

每个 target 可调低预算：

- preview_max_rows：1..1000，默认 1000；
- preview_max_bytes：1 MiB..20 MiB，默认 20 MiB；
- query_timeout_seconds：1..180，默认 180。

Web Admin 还可维护 blocked_relation_names：默认空；每项必须是精确 database.object，可指向表、视图或
物化视图；不接受通配符、正则、catalog 前缀和单段名称。保存时规范化、去重并展示 diff，删除条目要明确
二次确认，因为它会扩大应用层可查询面。保存产生新 generation 和 restart_required；task-worker 重启并写
loaded receipt 后才生效，旧任务因 config revision 漂移拒绝。

并发、SQL 大小和 artifact 配额初版只读。Admin 不能输入任意 session variable、AST 规则、函数规则、
resource group 表达式或 connector fallback。保存后必须重启 task-worker，见 §6。

### 8.3 timeout 层次

设有效 server query timeout 为 Q：

- connect/write timeout：最多 10 秒，且不大于 read timeout；
- StarRocks session query_timeout：Q，Q ≤ 180；
- PyMySQL read_timeout：Q + 10 秒；
- ToolGateway timeout：Q + 20 秒，从取得 slot 并写入 step started 后开始，不包含调度退让；
- target slot lease TTL：Q + 30 秒；
- ToolCall 仍满足现有 300 秒硬上限。

ADR-012 和 Settings 的当前 25/30 秒限制必须随 F1 profile 一起修订；慢查询旧 profile 保持自己的
现有上限。Gateway timeout 只能给连接关闭和结果封存留余量，不能把 server query_timeout 扩大。

### 8.4 跨 worker slot

TaskStatus 不新增 QUEUED。slot 等待使用非阻塞争抢，不在 task-worker coroutine 里 sleep 或等待数据库资源。
任务保持 RUNNING，并由 TaskStore 保存 defer_reason=target_slot_busy、substage=waiting_target_slot、首次
wait_started_at、固定 wait_deadline 和 next_attempt_at；等待时间和查询执行时间分别观测。

等待窗口必须在第一次资源争抢前持久化，不能等看到 BUSY 后才开始计时。ensure_slot_wait_window 使用数据库
时钟、当前 task fencing 和单次 CAS：没有窗口时原子写 wait_started_at=now、wait_deadline=now+300 秒；
已有窗口时只返回原值。事务在提交前崩溃等于从未争抢 slot，恢复后重新建立一次；提交后崩溃则 deadline
已经存在，redispatch、进程重启或 task lease 换 owner 都不能重置。

TargetQueryLeaseStore 属于 persistence 的执行调度租约，不持有 StarRocks client、SQL 或 result rows，也
不是第二个 ToolGateway。它以 target_fingerprint 为 key，使用数据库唯一约束、lease、fencing 和 expiry。
WorkflowRunner 是唯一协调者，顺序固定为：

1. 调用 TaskStore.inspect_step_execution，以当前 task grant 做只读、fenced 的恢复分类。已 committed 直接
   adopt；已有未提交 started 直接 INDETERMINATE；drift、预算耗尽或不可运行按闭集处理。只有
   ELIGIBLE_TO_START 才能继续，以上分支都不争抢 target slot；
2. 仅在 ELIGIBLE_TO_START 分支水合 ToolCall/HydratedQuery 并完成 StepAdmission；artifact 过期、target/
   policy/config 漂移只影响尚未 started 的新执行，不能遮蔽已 committed 或未提交 started 的恢复决定；
3. 调用 TaskStore.ensure_slot_wait_window，原子建立或读取固定等待窗口；
4. 调用 TaskStore.check_slot_wait_deadline。已到期直接
   FAILED(target_slot_wait_timeout)，StarRocks 连接数和 target slot 获取数都为 0；
5. 调用 TargetQueryLeaseStore.try_acquire(grant, not_after=wait_deadline) 做一次非阻塞争抢；该事务也用数据库
   时钟重验 not_after，禁止在 deadline 到期后取得 slot；
6. 返回 BUSY 时调用 TaskStore.schedule_deferral：把 next_attempt_at 设为
   min(now + 5 秒, 当前 slot expires_at, wait_deadline)，轮换 task fencing 并结束当前 task lease；
7. Runner 抛出内部控制信号 WorkflowDeferred，worker 收到后把本 candidate 视为已退让，并在同一轮继续
   处理其他任务；
8. 取得 slot 后，Runner 调用 TaskStore.begin_step_attempt。它在同一锁内重新执行与
   inspect_step_execution 相同的恢复/预算判定，消除 inspect 与资源争抢之间的竞态；只有原子新建 started
   的 PROCEED 才能调用 Gateway.invoke；
9. 把不透明 TargetSlotGrant 作为 Gateway.invoke 的受信 keyword-only 参数传入，不放进 ToolCall.typed_args、
   Plan 或用户输入；grant 自身绑定 tool_call_hash，Runner 不持有 StarRocks connection；
10. Gateway 在建连前重新校验 TargetSlotGrant 的 target、task、step、tool_call_hash 和 fencing。任何漂移都
    零连接拒绝；begin_step_attempt 未返回 PROCEED 时，只能在证明尚未建连后释放这个未使用 slot。

| 路径 | task_failure_count | StepExecutionRecord | max_tool_calls | 当前 task lease |
| --- | --- | --- | --- | --- |
| BUSY schedule_deferral | 不变 | 不创建 | 不消耗 | 结束并轮换 fencing |

inspect_step_execution 只分类、不写 started、不消费预算；begin_step_attempt 才是创建 started 与消费一次工具
预算的唯一事务。两者复用 TaskStore 内同一个判定函数，不能各复制一套恢复语义。

schedule_deferral 是通用的“资源忙退让”命令，但 F1-0 只为受信 operation requirement 明确声明的 target
slot 启用，不按 operation 字符串特判。它必须像 schedule_retry 一样幂等和 fenced，却没有“执行失败”语义。
Task attempt 可以因 worker 再领取而递增；它只记录调度领取，不等于 Step attempt，也不影响工具预算。
现有 run_with_task_heartbeat 必须把已提交的 WorkflowDeferred 当作正常退让：即使旧 heartbeat 同时因 fencing
轮换而续租失败，也不能覆盖存储 winner、改判基础设施失败或再安排 retry；收尾后 worker 继续下一 candidate。
XiaoweiRuntime 必须像透传 WorkflowPaused 一样透传 WorkflowDeferred，worker 在 RetryableTaskError 之前
单独捕获并返回“未执行”；不能进入 Evidence、Reflection、终态 finalize 或 task failure backoff。

取得 slot 后才可创建 StarRocks 连接。正常路径必须等同步 worker thread 的 finally 已确认连接关闭，再由
Gateway 主动释放 slot。

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

先严格区分两个事实：Task attempt 是 worker 对任务的一次 fenced 领取；Step attempt 才表示一次工具尝试。
等 slot 时可以存在 Task attempt，但没有 started 的 StepExecutionRecord，因此不消耗 max_tool_calls，也不
构成“SQL 可能已发送”。

Runner 在重新读取 SQL artifact、水合或 Admission 前，先用 inspect_step_execution 分类已有执行事实；只有
无 StepExecutionRecord 且预算仍可用的 ELIGIBLE_TO_START 才读取当前 artifact 并进入等待窗口。这样 artifact
过期或 target/policy 漂移不会把既有 committed 错判为拒绝，也不会遮蔽未提交 started 的
INDETERMINATE。取得 target slot 后、创建连接前，Runner 再调用 begin_step_attempt 原子重验并开始。该
存储事务同时创建 StepExecutionRecord、写非空 started_at 并把工具预算加 1；三件事不可拆分。因此“有
Step attempt 但没有 started”不是可持久化状态。migration/check constraint 发现这种旧数据或损坏数据必须
fail-closed，不能靠恢复代码猜测。对于 confirmed_artifact，重复 begin 同一个未提交 started 也不能再次
得到 PROCEED；never-replay 语义从受信 OperationSpec/query requirement 派生，不按 operation 名称特判。

恢复决策表固定为：

| 持久事实 | 能证明什么 | 决定 |
| --- | --- | --- |
| 有 Task attempt，但没有 StepExecutionRecord | 工具未 started；可能正在退让或 worker 在写 started 前崩溃 | inspect 返回 ELIGIBLE_TO_START；建立/沿用 deadline 后才可非阻塞争抢 slot，不消耗 max_tool_calls |
| 有 TargetSlotGrant，但没有 StepExecutionRecord | 仍能证明未建连；只是 slot 可能等 TTL 回收 | 不调用 Gateway；slot 可用后允许第一次 begin_step_attempt |
| 本次 begin_step_attempt 原子新建 started | 当前 live caller 是唯一获准的首次执行者 | 仅这一条控制流可调用 Gateway 一次 |
| 已有未提交 started，无论 task fencing 是否变化 | 无法证明 SQL 未发送或结果未产生 | inspect 在 slot 前返回 PREVIOUS_ATTEMPT_UNCERTAIN → TaskStatus.INDETERMINATE；Gateway 调用次数为 0 |
| 已有 committed Step attempt | 结果已成为任务事实 | inspect 在 slot 前返回 ADOPT_COMMITTED_RESULT；sealed result 只做幂等 activation，不重新查询 |
| 没有不确定 started，但预算因其他已提交步骤耗尽 | 确定性预算不足 | BUDGET_EXHAUSTED → FAILED，不能伪装成不确定 |

等待窗口事务提交前崩溃时尚未争抢 slot；恢复后重新建立窗口是第一次有效计时，不会漏掉真实等待。窗口提交
后崩溃、schedule_deferral 崩溃或 task 重新领取都沿用原 wait_deadline。每次 try_acquire 前和 lease 写事务内
都重验 deadline，所以到期后即使 slot 已空闲也不能执行。取得 slot 后、begin_step_attempt 前崩溃仍没有
started；恢复先由 inspect 判定 ELIGIBLE_TO_START，再等旧 slot lease 失效，但不得越过原 deadline。
begin_step_attempt 一旦提交，哪怕真正发送 SQL 前就崩溃，也按表中不确定路径保守地不重放。

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
3. ARCHITECTURE.md §6：加入 versioned TaskSubmission、原始 application/sql 路由策略、标量 ToolCall 与
   进程内 HydratedQuery、ResultArtifact、ResultChunkSink/线程桥和 target lease 契约；
4. ARCHITECTURE.md §7/§7.3：加入 artifact/ToolCall hash、inspect → 新执行水合/准入 → 持久化 deadline →
   try_acquire → begin 顺序、非阻塞 schedule_deferral、Task/Step attempt 区分、sealed result adoption 和
   PREVIOUS_ATTEMPT_UNCERTAIN；
5. ARCHITECTURE.md §9：加入 confirmed_readonly profile、token scan、AST/ReadonlyStatementRegistry 双证明路径、
   已验证小版本区间、增量 registry 与请求级暂未支持错误、blocked_relation_names 精确拒绝、元数据非保密
   边界、credential 授权边界和无 hint 决策；
6. DEVELOPMENT_PLAN.md、路线规格和 AGENT_HANDOFF.md：移除“只接受模板 SQL”的过期口径，同时保持
   “无源码授权、无真实调用授权”的当前状态；
7. 未来 ADR-005/Web §11.2：批准锁定页不依赖 approver，但任何 view/export approver grant 仍受
   ADR-005；
8. ADR-007：登记第四 capability、F1 test/release 授权矩阵和真实现场门；
9. ADR-009：冻结 HydratedQuery 水合、标量 ToolCall hash、inspect → 新执行水合/准入 → deadline → slot →
   begin_step_attempt → Gateway 顺序、result_ref 和 attempt unknown 语义；
10. ADR-012：扩展独立 F1 adapter/profile、ResourceSnapshot、SSCursor、同步线程/异步 sink 桥和 timeout
    层次，不改变慢查询；
11. ADR-013：允许只投影受保护 result deep link；
12. ADR-017：冻结 RESTRICTED read 无审批的窄条件；
13. 新 artifact ADR：冻结 SqlArtifact、ResultArtifact、ACL、配额、24 小时保留、result fencing、
    staging/sealed/available 和 F2/F3 消费契约。

ADR-015 不因直接 SQL而放宽；只有 F1-NL 设计才可申请新增模型端口和出站数据边界。

## 14. 验证与验收

### 14.1 本次设计修复证据

- 本轮设计契约测试先在 v4 旧稿上因 SHOW DATABASES 仍被拒绝、旧数据库白名单字段仍存在且没有
  blocked_relation_names 而转红；修订后必须转绿；
- 第三轮回归测试先在 v5 旧稿上得到 `2 failed, 9 passed`：一项因 §7.3 缺少 `Union` 和版本化兼容证明
  路径转红，另一项因没有明示黑名单“不提供元数据保密”而转红；不是导入、夹具或标题定位失败；
- 增量登记决策契约先在 v6 旧稿上因缺少“首批登记”而 `1 failed`，证明旧稿仍把 registry 完整性与 target
  开放错误绑定；
- sqlglot probe 使用 uv.lock 的 30.17.0，覆盖 SHOW DATABASES 与 FROM default_catalog 的允许、外部
  Catalog 的拒绝、内部三段名、comment hint、version comment、JOIN comment hint、DESC、
  EXPLAIN SELECT、EXPLAIN ANALYZE SELECT 和 EXPLAIN ANALYZE INSERT；另确认 Union/Intersect/Except 的
  根节点不是 Select，EXPLAIN LOGICAL/VERBOSE/COSTS 会 ParseError，复审列举的多项 StarRocks SHOW 会
  降级为 Command；
- 源码检查确认当前 TaskSubmission、ToolCall、AdapterResponse/Evidence、attempt recovery、PyMySQL Cursor
  和 config 真源的实际缺口；另确认 worker 串行 await、schedule_retry 会增加 task_failure_count、
  api_request_body_limit_bytes 小于 1 MiB，以及 StarRocks adapter 在 asyncio.to_thread 中执行；
- 本轮不新增产品实现测试，不把规格测试冒充 F1 功能已实现。

### 14.2 实施时先红后绿

F1-0/F1-1 开工后，先补并确认以下行为测试在旧实现上因目标原因失败：

- required HydratedQuery 缺失时 Gateway 调用为 0；
- SQL artifact 被替换、过期、跨 actor/target/config 时 Gateway 调用为 0；
- adapter 收到的 bytes 与确认 hash 不同则零 SQL 发送；
- ToolCall.typed_args 仍只接受 JSON 标量；HydratedQuery 不能进入 Plan、TaskStore、trace 或 audit；
- duplicate column names 按 ordinal 往返；
- 结果 rows 不进入 ToolResult.data_view、Evidence 和 TaskSubmission；
- crash after started/before commit 恢复为 INDETERMINATE，Gateway 不重放；
- committed sealed result 只 activation，不重放；
- 两个 worker 同 target 只有一个取得 slot；
- committed/未提交 started 的恢复分类发生在 slot 获取前，二者 target slot 获取数均为 0；
- wait_deadline 在第一次 try_acquire 前持久化，崩溃、重新领取和 BUSY deferral 都不能重置；到期后即使
  slot 空闲也不能获取；
- 单 worker 中 F1 因 slot BUSY 退让时，同一轮的其他 capability 仍被执行；
- 等 slot 或取得 slot 后、begin_step_attempt 前崩溃，恢复不误判 FAILED/INDETERMINATE，也不调用 Gateway；
- schedule_deferral 不增加 task_failure_count、Step attempt 或 max_tool_calls，300 秒 deadline 跨领取保持；
- schedule_deferral 与 heartbeat 续租同刻完成时，存储 winner 仍是正常退让且 worker 继续下一任务；
- cancellation 时 slot 在连接关闭前不释放；
- application/sql 的 1 MiB 原始 body 成功、1 MiB + 1 byte 拒绝；原 JSON 路由仍保持原上限；
- 同步 reader 对 async sink 写入有背压，abort 后的迟到 chunk/seal 被 result fencing 拒绝；
- abort 持久化失败时结果保持不可见、slot 不释放，恢复只补 abort 而不激活；
- SHOW DATABASES、SHOW TABLES、内部系统库、跨内部数据库 JOIN 和 default_catalog.db.table 成功对照；
- 黑名单表、视图和物化视图通过 SELECT、CTE、JOIN、子查询、EXPLAIN、DESC、SHOW CREATE、
  SHOW COLUMNS 访问时均在发送前拒绝，大小写/quoted identifier 按现场确认规则规范化；
- SHOW TABLES 仍可只列黑名单对象名称；未列入黑名单但 credential 无 SELECT 权限的对象仍由 StarRocks
  拒绝，黑名单不能扩大授权；
- 查询根节点 Select、Union、Intersect、Except 都有正常对照和写形状反例；
- ReadonlyStatementRegistry 的每一个 registry descriptor 都有正常对照、大小写/空白/quoted identifier
  边界、缺失或重复 clause、多语句、hint、外部 Catalog、黑名单目标和歧义目标反例；未知只读前缀和
  当前版本未登记语法必须返回 READONLY_STATEMENT_NOT_SUPPORTED、零 SQL 发送且 target 继续可用；
- 实际版本位于 verified_min_version、区间中部、verified_max_version 时 target 可加载；低于下界或高于
  上界时 target fail-closed；
- EXPLAIN LOGICAL/VERBOSE/COSTS 对 SELECT/Union 有正常对照，对 INSERT、INTO OUTFILE、外部 Catalog、
  table function 和黑名单关系有反例，并证明 adapter 仍收到完整原始 SQL；
- SHOW CATALOGS、SHOW PROFILELIST、SHOW PROC、information_schema.columns 和
  information_schema.views 的元数据可见性按已接受语义测试，不误写成黑名单保密保证；
- DML、DDL、锁、文件、导出、session 改写、外部 Catalog、table function、UNNEST、UDF 和 hint 的恶意
  矩阵；
- 慢查询 template_locked 全部原回归继续通过；
- requester、Admin、其他用户、过期 ACL 正反例；
- 去掉 hash、required query、token scan、blocked_relation_names、registry 版本绑定、ACL、fencing 或
  no-replay 中任一保护时，对应测试转红；对带对象的 descriptor 隔离变异“移除目标提取”时，至少一个
  黑名单目标测试必须转红，且确认不是被更早规则遮蔽。

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
6. 同 target 第二个任务非阻塞退让 slot，期间同一 worker 的其他能力仍可执行；
7. 一条实际运行的 180 秒 F1 查询对同一串行 worker 上其他能力造成的端到端调度延迟；
8. StarRocks 内部 queue 等待也受 180 秒总发送后预算约束；
9. 取消/driver timeout 后服务端残留查询的最长窗口；
10. 在已验证小版本区间的区间下界、区间上界和至少一个代表版本上，验证首批 registry（§7.3 表中各类及
    常用只读语句）的输出形状、所需权限和权限不足错误；另用未登记语句确认返回
    READONLY_STATEMENT_NOT_SUPPORTED、零 SQL 发送且 target 继续可用，区间外版本才 fail-closed；
11. 黑名单表/视图的直接查询和对象级元数据读取在 SQL 发送前拒绝，SHOW TABLES 仍只列名称；
12. 外部 Catalog、UDF、UNNEST、hint、写语句、session 改写和 target drift 在 SQL 发送前拒绝；
13. 专用 credential 对未授权对象的读取确实失败，且不存在写、管理、UDF、外部 Catalog 和文件权限；
14. 最大列宽/单行结果不会把应用保存上限误报成 driver 或服务端内存上限；
15. query id 与 Profile 指标可关联；
16. 24 小时应用层删除，以及 StarRocks audit/query history 与部署环境的数据处置；
17. information_schema、SHOW PROFILELIST、SHOW PROC 等元数据面实际可见内容，并确认负责人接受列名、
    视图定义、查询文本和集群拓扑暴露；如环境要求隐藏，不得以当前黑名单冒充控制。

最高证据只能标记 tests + test-env verified；它不等于生产部署、canary 或 UAT。

## 15. 按业务闭环实施

1. F1-0 规则与契约前置：完成 §13 文档/ADR、已验证小版本区间、§7.3 各类加常用只读语句的首批
   ReadonlyStatementRegistry、请求级暂未支持错误、TaskSubmission/query/result/target lease、
   schedule_deferral、路由 body policy、线程 sink bridge 契约和 SQLGuard profile；慢查询回归保持通过；
2. F1-1 Web 直接 SQL 闭环：固定默认预算，完成 SQL 草稿/确认、Plan/Admission、fake streaming
   adapter、ResultArtifact、锁定页、no-replay、并发 slot 和 24 小时清理；
3. F1-2 Admin 预算与黑名单：扩展 StarRocksResource，支持下调预算和维护精确关系黑名单，保存后
   restart_required，task-worker 启动加载回执；
4. F1-3 飞书入口：只投影可信 Web SQL 页面和受保护 result 链接，不接受消息 SQL；
5. F1-NL：单独设计、单独批准；
6. F1-G 离线总验收：正式 Web 路径、全量/安全/eval、exact-SHA 独立审查；
7. F1-H 真实 target：另写现场计划并单独 GO。

每个切片都以前一闭环的真实接口为基础；不并行建设第二套 SQLGuard、TaskStore、结果服务或 connector。
F2/F3 只能在 F1 离线验收及各自设计获批后启动。

## 16. 已接受的权衡和残余风险

- 保留原 SQL避免改写语义；代价是依赖 session limit、resource group、确认和严格 Guard；
- Web 两步确认比“编辑器直接执行”多一步，但能把 SQL、target 和预算绑定为可审计事实；
- Registry 采用增量覆盖：首批之外的合法只读语句可能暂未支持并返回请求级结构化错误，但不会关闭
  target；代价是 SQL 客户端兼容度逐批提升，而不是首版承诺同版本全部语法；
- Registry 绑定连续的已验证小版本区间；区间内未登记语句不关闭 target，只有实际版本越界、区间非法或
  digest 漂移才 fail-closed；
- 初版仍不支持任何 hint、UNNEST、UDF 和外部 Catalog；这是已确认的数据源/执行语义范围，不是 parser
  覆盖率造成的偶然限制；
- 关系黑名单是 fail-open 的额外策略，而且不递归展开视图 lineage；重命名或新增视图可能产生新入口。
  强隔离必须由 StarRocks credential 撤销 SELECT，并在 config/grants 漂移后重新 preflight；
- 关系黑名单不提供元数据保密；`information_schema`、profile 和系统 listing 可能暴露列名、视图定义、
  查询文本或集群信息。需要隐藏时依赖数据库权限/安全视图，或未来另行批准独立隐藏策略；
- 预览有界、导出未来重跑，F1/F3 数据可能不同；
- 同 target 并发固定 1，峰值排队更长；
- 当前 worker 对已领取任务串行 await；非阻塞 slot 退让解决了“排队占住 worker”，但真正执行中的 F1
  查询仍可能让同一 worker 上其他任务延迟最多约 Q+20 秒。当前没有获批的跨任务延迟 SLO，本设计不顺带
  引入 worker 并发；F1-G 前必须由负责人确定阈值并实测，若不满足则另行设计有界并发并复审，未闭合前
  不进入真实 target 发布；
- slot BUSY 会产生短周期 PostgreSQL 调度写入；初版固定 5 秒退让，只有实测出现调度压力才引入更复杂
  backoff，不提前建设独立队列；
- PostgreSQL 保存短期预览，物理擦除受 MVCC/备份约束；
- 客户端超时不能证明服务端立即停止，最坏残留窗口必须现场测量；
- F1-NL 拆出后，F1 第一阶段只满足直接 SQL，不宣称自然语言查询已经交付；
- 当前仓库仍没有本文描述的源码、migration 或真实调用证据；本文批准只代表设计可进入详细实施计划。
