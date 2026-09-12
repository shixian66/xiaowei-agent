# ADR-010：M5 持久执行尝试与本地 Compose 边界

- 状态：Accepted
- 日期：2026-09-05
- 决策人：项目负责人
- 相关：[ARCHITECTURE.md](../../ARCHITECTURE.md) §5.6/§7.3/§11、[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-009](ADR-009-plan-hash-approval-binding-and-tool-admission.md)、[ADR-015](ADR-015-real-model-provider-boundary.md)、[M5 实施计划](../plans/M5-api-worker-compose.md)

## 背景

M4 已把 TaskStore、PlanStore、EvidenceLedger、lease、heartbeat、fencing、stale
discovery 和 PostgreSQL migration 落成稳定契约。M5 要把这些能力组装成 API、CLI、
Worker 与 PostgreSQL 的本地可恢复闭环，但不能让入口层、Worker 或 Compose 另造一套
生命周期语义。

异步提交与恢复带来几组必须先定死的选择：任务提交事实在哪里持久化；谁有权领取和
推进任务；步骤调用结果如何在崩溃后重建；状态、Evidence 与审计怎样保持原子；重试
怎样立刻废止旧持有者；查询怎样保持纯读；以及本地进程如何共享一套装配与镜像。

## 决策

### D1 提交、领取与执行分离

API/CLI 只构造受信 `RequestContext`、解析协议并投影 `TaskView`。`submit_task()` 只把
不可变 `TaskSubmission` 与 TaskRecord 原子写入，不执行计划；Worker 先从只读 dispatch
列表发现候选，再通过唯一领取点 `begin_task_attempt()` 取得 `TaskAttemptGrant` 与同一
事务读出的 submission，最后调用 Runtime/Runner。

`TaskStore` 仍是一个 aggregate 事务端口；M5 将其扩展为十四个方法，不拆成消息总线、
队列服务或多个微服务。

### D2 Runner 只消费 grant

`WorkflowRunner.start()` 与 `resume()` 都显式接收 `TaskAttemptGrant`。Runner 不再自行
调用 `acquire_lease()`；进入执行后先用 grant 的 owner/token 续租验证，再启动 heartbeat。
任何续租失败都取消执行分支，loser 不得再提交步骤、Evidence 或终态。

> **RI3 已批准修订：** 上述文字记录的是 M5 已验收实现。ADR-015 已获批准；当 PR 3C 实施时，现有 heartbeat
> helper moves completely to application scope: Worker covers `execute_task()` plus
> retry scheduling, while `handle()` starts it only after its existing grant. Runner
> keeps `DEFAULT_LEASE_TTL_SECONDS`, `lease_ttl_seconds`, `_require_current_grant()` and
> all grant/fencing write checks. It drops only `heartbeat_interval_seconds`, the
> heartbeat-only sleep injection/state, `_heartbeat()` and `_run_with_heartbeat()`.
> The retained start/resume renewal is a one-shot grant validation, not a second periodic
> heartbeat owner. Exactly one periodic owner exists per attempt; no supervisor,
> task deadline or second state machine is added. 在 PR 3C 合入前，这仍是已批准的拟议行为，
> 不是当前源码事实。

领取意图分为 `DISPATCH` 与 `APPROVAL_RESUME`。前者只接受
`CREATED/PLANNING/RUNNING`，后者只接受 `AWAITING_APPROVAL`。M5 没有生产审批消费方，
但保留已验收的 `resume()` 契约及其测试入口，不能借重构静默废掉它。

### D3 三套计数互不替代

- `attempt_number`：每次成功取得任务执行权递增。
- `task_failure_count`：只有成功提交 `schedule_retry()` 才递增。
- infrastructure failure window：Worker 进程内用 monotonic clock 计量，不写入任务。

基础设施不可用不调用 `schedule_retry()`，不把系统性故障批量写成任务失败；让 lease
自然到期并在数据库恢复后重新领取。

### D4 fencing token 有两个推进点

fencing token 在成功取得 lease 时推进；`schedule_retry()` 成功时再次推进，使刚结束的
grant 立即失效，而不是等到未来的 `next_attempt_at`。heartbeat 不推进 token。
`schedule_retry()` 同时把 `lease_expires_at` 移到 `next_attempt_at`，不新增语义模糊的
`release_lease()`。

### D5 步骤 journal 与原子 checkpoint

`begin_step_attempt()` 在调用 Gateway 前持久化 in-flight 行并保守扣减计划级工具预算；
同一 fencing token 重入不重复计数，新 token 接管才计一次。`commit_step_result()` 把步骤
终局、可选 Evidence 与非空 audit event 集合放在同一事务内提交，并用持久化 digest 使
确认丢失后的重放可收敛。

Gateway 返回 `ToolResult` 时必须有 Evidence；malformed adapter 没有 `ToolResult`，以
独立 kind 记录 `FAILED + audit` 且不伪造 Evidence。`TIMEOUT` 保持独立终局事实，不压成
`FAILED`。`SKIPPED` 是由既存步骤结果重新求值的派生量，M5 不持久化它。

### D6 状态改动与审计的事务边界

终态 transition、retry 调度、step commit，以及 `begin_task_attempt()` 在领取前发现的
不可恢复损坏，都必须把状态事实与对应审计放在同一事务。成功领取只改变可由 TTL 自愈
的 lease/attempt，不强制事务审计；调用方可补常规观察事件。

`begin_task_attempt()` 内部提交的终态事件随结果返回，Worker 只补一条已提交日志，不再
重复 durable 写。audit 不做 fencing：它记录一次 attempt 的观察，而不是任务 winner；
代价是 grant 后的事件必须带 `attempt_number`，loser 在续租失败后立即停止执行分支。

### D7 持久化写显式区分未知结果

写失败分为 confirmed rollback 与 not-confirmed。前者可记录 rolled-back，后者只表示
客户端不知道数据库是否提交，不能谎报回滚或成功。最终事实以 TaskStore winner、
Evidence 与 audit 为准；异常类别、日志和 API 错误不得包含 SQL、参数、DSN 或外部原文。

持久化故障只分「暂时不可用」与「系统性」两类，两者都不改任务。前者进 Worker 有界
退避；后者进程 fail-stop。

### D8 查询是 scope 约束的纯读

`TaskLookup` 只含 task id、tenant 和 environment。跨 tenant/environment 统一表现为
not found；同一 tenant/environment 内暂不按 actor 隔离。这是单租户本地闭环的刻意
收窄，不构成生产身份系统，未来多 actor 部署前必须另立授权与 ACL 契约。

异步查询只有终态任务携带 `RenderPayload`；`AWAITING_APPROVAL` 返回 `render=null`。
M5 没有审批消费端和可恢复 pending approval 引用，入口层不得猜测审批状态。

### D9 同步 facade 保留既有提交顺序

`handle()` preserves M3's eval-gated deterministic rejection before `create_task`.
It shares terminal projection and Runner call shapes with async execution, but never
loads/saves model artifacts or calls the real provider. Durable `execute_task()` is the
only model path. Changing this order remains a separate behavior change requiring eval
re-recording.

### D10 幂等索引使用定长摘要

`idempotency_key` 保留输入长度上限，但数据库唯一约束建立在定长 scope digest 上；命中
后必须回查 tenant/environment/key 明文，摘要冲突 fail-closed。submission、retry、step
commit 与 idempotency digest 都只是与数据同库的无密钥一致性 checksum，不是防数据库
写入攻击者的完整性证明。

降回 M4 的 `0001_initial` 前必须先用 SAVEPOINT 证明旧三列唯一约束可重建；不兼容数据
即使带破坏性开关也拒绝。迁移期对无法恢复的在途任务做确定性终态化是显式审计例外，
由 revision 与闭集 terminal reason 留痕。

### D11 组装根、readiness 与单镜像

组装根位于 `interfaces/local_stack.py`，用 frozen dataclass 持有进程内对象引用；它不是
跨进程 DTO。fake/recording Gateway 只能在该文件组装，Runtime/application 不 import
具体 Gateway 实现。

`ReadinessProbe` Protocol 与 `ReadinessReport` 放在 `contracts/`，具体检查实现放在
`persistence/` 并由 LocalStack 注入 API。这是对 TaskStore/PlanStore「Protocol 与实现
同包」惯例的刻意分叉：薄 API 必须能给 `check()` 标注类型，但不能因此获得直接依赖
persistence/Engine 的权限。

API、Worker 与 migrate 使用同一个应用镜像，仅由命令区分进程角色。Compose 包含
postgres、一次性 migrate、api、worker；API 只向宿主 `127.0.0.1` 发布，PostgreSQL 和
Worker 不发布宿主端口。secret 只经文件引用注入，不写入镜像、仓库、argv、环境或日志。

### D12 文件级入口依赖与通用叶子

`interfaces/` 使用按文件穷尽的内部依赖白名单，只有 LocalStack 获得宽依赖与 fake 装配
权。每个文件的允许集都与 `_UNIVERSAL_LEAF` 取并集；`redaction.py` 继续只在唯一的通用
叶子集合中登记，不在每个文件条目复制。这样 CLI 可复用单一脱敏真源，又不会制造第二
份规则。

## 后果

- M5 新增四个顺序 migration；每个纵向切片的 schema 与 metadata 必须同步。
- Worker 可从持久化 submission、plan 与 step journal 恢复，但 fake 闭环不能证明未来
  真实副作用工具 exactly-once；真实写仍需幂等与 readback。
- `APPROVAL_RESUME` 在 M5 只有契约测试调用方；`AWAITING_APPROVAL` 暂无异步 pending
  投影；同 scope 跨 actor 可读。这三项在后续真实审批/身份接入时必须重审。
- migration 升级/降级会确定性判死一部分无法恢复的在途任务，且数据语义不可逆；执行
  前必须停 API/Worker 并备份。
- Compose 只证明单机本地 fake 闭环，不代表部署、高可用、canary 或用户验收。

## 备选方案与否决理由

- **API/Worker 各自实现生命周期判断**：会复制 Resolver、Planner、Policy、fencing 与
  终态语义，入口不再薄。
- **增加消息总线或拆微服务**：M5 没有吞吐或隔离证据支撑，引入第二个任务事实来源。
- **Worker 继续调用 `acquire_lease()`，Runner 再领取一次**：产生双重 owner/token，
  旧调用方无法证明自己仍是 winner。
- **只靠任务终态、不持久化 step journal**：工具返回后进程崩溃时无法区分未调用、已
  调用未提交和已提交确认丢失。
- **用 exception 类型猜提交结果**：同一个断链异常既可能发生在 commit 前，也可能发生
  在 commit 后，无法区分 rollback 与 not-confirmed。
- **把 readiness Protocol 放在 persistence/**：会迫使薄 API 为类型注解直接 import
  持久化层，破坏文件级入口依赖边界。
- **每个 interfaces 文件单独列 redaction**：通用叶子产生多份易漂移的白名单表述。

## 回滚

- 代码回滚使用 feature branch 上的逐提交 revert，不改写共享历史。
- schema 回滚必须先停 API/Worker、备份，并经 M5 migrate 入口执行到 `0001_initial`；
  默认拒绝任何 M5 submission/journal，显式破坏性开关也不能绕过旧索引兼容 probe。
- 回退应用镜像与 schema revision 必须匹配；不得只回退其中一侧。
- 本 ADR 若要改变 TaskStore 方法集、fencing 推进点、query scope、审计事务边界或真实调用
  权限，须另立 ADR，不以入口层兼容分支绕过。
