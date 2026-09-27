# ADR-012：M6b target-bound StarRocks 测试环境只读 adapter

- 状态：Accepted
- F1 修订：**Proposed**（2026-09-27，待负责人接受），见文末“F1 修订”；不改变本 ADR 已接受条款的状态
- 日期：2026-09-07
- 决策人：项目负责人
- 相关：[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-009](ADR-009-plan-hash-approval-binding-and-tool-admission.md)、[M6b 计划](../plans/M6b-starrocks-test-readonly.md)

## 背景

现有 `DeterministicToolGateway` 只按 gateway 名称选择 adapter。该规则适用于本地
fake/recording，但不能证明真实 StarRocks connector 与已经准入的逻辑目标相同。现有 `test`
目录又含两个资源；即使 `AdmissionCertificate` 携带 `target_fingerprint`，真实 connector 仍
可能被错误地绑定到另一集群。

M6b 只验证既有 `starrocks.slow_query.diagnose@1.0.0` 的两个只读 operation。它不授予通用
SQL、流式大结果、E1、生产连接或其他真实 adapter 权限。

## 决策

### D1 两类 adapter 注册

Gateway 同时支持两种互斥注册：

- generic adapter：继续服务既有 fake/recording，按 gateway 名称选择；
- target-bound adapter：按 `(gateway, target_fingerprint)` 精确选择，并携带获批的
  tenant、environment、actor、激活窗口、配置 revision、证据来源和物理身份引用。

同一 gateway 不得同时存在于两类注册中。target-bound gateway 未精确命中时禁止 fallback；
Gateway 必须在调用 adapter 前复核 target fingerprint、tenant、environment、actor 和激活
窗口，任一不符时 adapter 与网络调用次数均为零。

真实 StarRocks 只允许由 composition root 注册为 target-bound；`ToolCall`、`IntentDraft` 和
adapter payload 都不能选择或覆盖 connector。Python Protocol 不是语言级隔离，因此同时用
类型检查、逐文件依赖测试和装配契约测试承重。

### D2 目标与兼容策略

测试环境必须解析成负责人批准的唯一 canonical resource ID，目录不保存 host、port、账号或
secret。selector version 从 `1` 升为 `2`；恢复中的旧 fingerprint 按目标漂移拒绝，不回退到
旧目标。现有 generic fake/recording 的 `ToolGateway` Protocol、`ToolAdapter.execute()`、
`AdapterResponse` 与 `ToolResult` 契约保持不变。

在 canonical resource ID 尚未由负责人明确给值前，测试环境保持不可唯一解析，真实装配必须
fail-closed；不得由实现者选择现有两个占位 ID 中的任意一个。

该歧义拒绝发生在目标解析层，因此会阻断 `test` 环境的 generic recording 与 target-bound
真实装配，不只是阻断网络连接。离线 recording 继续使用可唯一解析的 `dev` 环境；在负责人
批准唯一 canonical resource ID 前，不把 `test` 环境不可用误判为 recording adapter 回归。

### D3 默认关闭与配置绑定

StarRocks adapter 默认模式为 `recording`。`test_readonly` 需要完整的进程级受信配置，并且只
允许 `environment_id=test`、获批 actor、带时区且递增的激活窗口、TLS 验证、file-backed
credential reference、version/grants/DDL/identity 四个带外批准 digest 及来源引用。半配置、
明文 password 变量、未知字段、超过现有 30 秒 ToolPolicy 上限的 timeout，或不严格小于该
上限的任一 driver socket timeout 均启动失败；connect/write timeout 也不得超过 read timeout。

配置 revision 是对非 secret connector 配置、credential reference、目标、窗口、TLS、timeout、
driver version、normalizer version 和 SQL surface reference 的 canonical SHA-256；不读取或
包含 credential 文件字节，日志和错误中不展示 host、user、文件路径实值或 digest 输入。

### D4 固定握手与主查询闭集

真实 adapter 只允许两个既有 operation：`list_slow_queries` 与
`count_queries_in_window`。它只执行已经过 `StepAdmission` 的确定性主 SQL，并额外执行下列
固定闭集：

1. 固定 session `query_timeout` 设置与 readback；
2. `SELECT VERSION()`；
3. `SHOW GRANTS`；
4. 对批准 AuditLoader 表的 `SHOW CREATE TABLE`；
5. 批准的单行 identity 查询。

这些语句只读取服务端/会话事实或设置本连接的只读查询超时，不修改被管目标的持久状态，按
E0 处理；它们必须由受审代码固定生成并经闭集/AST 规则验证，不能来自用户、模型、ToolCall
附加字段或外部文本。除此之外 adapter 不得自行发 SQL。

每次调用使用 call-local connection，不自动重试，不启用 local infile 或 multi-statements，
并在 `finally` 关闭。list 最多接受计划 row limit 加一条探测，硬上限 200；count 只接受一行。
超量、列漂移、类型异常、权限/认证/连接/超时错误均结构化失败，不把部分结果当成功，也不
暴露上游错误正文。

`query_timeout` readback、version、`SHOW CREATE TABLE` 与 identity 必须逐字节匹配受审列名；
`SHOW GRANTS` 的列标签可能包含账号且随版本变化，因此只要求恰一列并对规范化值做批准
digest 比对，不把动态列标签当成身份真源。

### D5 preflight、证据与物理身份

每次调用在同一连接重新比对 version digest、grants digest、DDL digest/本地 surface、identity
digest 和 session timeout；任何漂移都在主查询前失败。expected digest 只能来自负责人/DBA
批准的带外材料，或另行批准的独立 discovery 阶段，禁止由同一次待验证连接自产自证。

Gateway 为成功结果提供脱敏的 target fingerprint、config revision、evidence source reference、
physical identity reference、driver version 和 preflight verdict。adapter rows 和上游文本不能
覆盖这些值；Evidence builder 仍以 Runner 传入的同一个 `ResolvedTarget` 判断目标。

真实验证只使用隔离的临时 PostgreSQL/Compose project。当前 EvidenceLedger 是 append-only，
所以批准周期结束后只能销毁精确 project/volume；不得把逻辑删除表述为物理擦除，也不得使用
广泛 prune 或未解析的 glob。CI 不持有 StarRocks 配置或 secret，不执行真实调用。

### D6 激活、失效与回滚

只有离线候选 SHA 通过审查、真实授权清单全部有值且负责人再次给出“现场 GO”后，才允许在
批准窗口人工激活。窗口、actor、目标、配置 revision、digest 来源或 secret reference 任一
漂移都会立即失效。

回滚方式是移除真实 target-bound 注册并恢复 `recording`；不改变 capability、plan schema、
hash canonicalization、Runner、Policy profile 或 `_E1_EXECUTION_ENABLED=False`。M6b 结论最多
为 `tests + test-env verified`，不等于部署、canary、生产就绪或用户验收。

## 后果

- 真实 connector 无法再仅凭相同 gateway 名称消费另一目标的准入凭证。
- selector v2 会让旧任务在恢复时明确产生目标漂移，不能静默兼容。
- 唯一 test 目标未批准前，该环境的 recording 与真实装配都会在目标解析阶段拒绝；`dev`
  recording 不受影响。
- 每次调用都重新 preflight，增加固定小开销，但避免在短窗口内复用已漂移的权限或身份。
- 小结果 adapter 保持现有 DTO；后续通用 SQL、流式结果和导出仍需独立 ADR 与里程碑。
- 物理 identity、带外 digest、唯一 resource ID 和真实 Evidence 处置未获批时，离线代码可以完成，
  但真实激活保持关闭。

## 备选方案与否决理由

- **只按 gateway 注册真实 adapter**：不能证明 connector 属于准入目标。
- **把 host/账号/secret 放进 ToolCall**：给用户或模型连接选择权，破坏进程级受信配置边界。
- **错 fingerprint 回退 generic**：会把目标错误伪装成成功或在环境间切换数据源。
- **启动时缓存 preflight**：调用窗口内权限、DDL 或目标漂移不能被发现。
- **M6b 顺手实现 Evidence TTL/通用 Artifact**：扩大持久化契约，且不服务 200 行以内的当前能力。
- **首次连接自产 digest 再比对**：属于 trust-on-first-use，不能证明目标、权限或 DDL。

## 变更门

下列任一变化必须先修订本 ADR：target-bound 注册键或 fallback 语义、受信元数据来源、固定 SQL
闭集、selector 迁移策略、真实 Evidence 处置，或 M6b 对 operation/环境/调用类别的授权范围。

## F1 修订（2026-09-27，Proposed）

- 状态：Proposed（F1-0，待项目负责人接受；接受前不得写 F1 行为源码）
- 设计真源：[F1 受治理只读查询设计](../superpowers/specs/2026-09-27-f1-starrocks-readonly-query-design.md) v8 §5.5、§6、§8

### F1-D1 独立 profile，不改变慢查询

F1 新增独立的 target-bound 查询 adapter 与 `confirmed_readonly` profile。M6b 的
`list_slow_queries` / `count_queries_in_window`、D4 固定握手闭集、D5 preflight、`template_locked` SQLGuard、
30 秒 ToolPolicy 上限与 recording 默认模式全部保持不变。D1 的 target-bound 注册、禁止 fallback 与
Gateway 前复核同样适用于 F1 adapter。

### F1-D2 target 配置

F1 复用 W4 的 `StarRocksResource`，task-worker 启动时加载，新增 `blocked_relation_names`、F1 查询上限与启用
开关；保存后 `restart_required`，不热加载。host、账号、黑名单、上限或启用状态变化都改变 config revision，
旧计划在 Admission 前拒绝。F1 **不做** D5 的 version/grants/DDL/identity digest 比对，也不绑定 StarRocks
版本区间；只读权限由 DBA 配置的专用账号承重，并在 F1-H 人工核验。

### F1-D3 会话、读取与 timeout

设有效 query timeout 为 Q（≤ 180 秒）：connect/write ≤ 10 秒且不大于 read；session `query_timeout=Q`；
PyMySQL `read_timeout=Q+10`；Gateway timeout `Q+20`；ToolCall ≤ 300 秒。每次调用使用独占 connection，
设置 `query_timeout` 与 `sql_select_limit=rows+1`，以 `SSCursor.fetchmany(≤100)` 读取到 EOF、第 1001 行或
字节上限。提前截断、超时或取消时直接关闭 connection，不调用会耗尽未读结果的 `SSCursor.close()`。

同步读取在 `asyncio.to_thread` 中运行，结果写入 Runner 创建的进程内 `QueryResultBuffer`；Gateway 超时或取消时
关闭 buffer，迟到写入被丢弃。AdapterResponse.payload 与 ToolResult.data_view 必须为空。

### F1-D4 授权

本修订只定义离线契约。F1 真实 target 属 ADR-007 D 层，另需 F1-H 现场计划与现场 GO；M6b 的
`test_readonly` 授权不外推到 F1。
