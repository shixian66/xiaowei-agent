# RI5 Minimal Web Admin Configuration Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在飞书、模型和 StarRocks 配置契约稳定后，提供一个只有 admin 可用的最小 Web 配置中心，支持 draft、测试连接、显式发布、版本 readback、审计和回滚，永不保存或回显明文 secret。

**Architecture:** PostgreSQL 保存不可变配置版本、active pointer、测试请求/摘要和审计；secret 仍是
容器只读文件。Web 只是受 CSRF 保护的薄 API/静态页面，业务规则位于
`application/configuration.py`。该 application service 只写测试请求，永不 import/调用 Gateway。
默认关闭的 configuration-test worker 只调用 `interfaces/local_stack.py` 返回的窄
`ConfigurationTestStack`，自身不 import `tools/` 或组装 Gateway；唯一装配根仍是 `local_stack.py`。
飞书/模型走各自窄 port；StarRocks 在与现有 task-worker 隔离的候选配置栈中复用完整
Runtime/Runner/Admission/Gateway 任务链。发布后由目标进程在受控 Compose restart 时加载 active
version 并写回实际版本；Admin 页面据此显示 readback。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、SQLAlchemy Core、Alembic、PostgreSQL、现有 vanilla HTML/CSS/JS、Docker Compose、pytest。

**Spec:** [真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)

## Global Constraints

不引入前端框架、通用配置 DSL、secret vault 产品或动态插件系统。Admin 权限不授予 E1、任意
SQL、任意 endpoint、任意文件路径或跨租户访问。StarRocks 测试连接必须经过正常
`XiaoweiRuntime → DeterministicStepRunner → StepAdmission → ToolGateway`，不能由 Web/API 或
`application/` 直接调用 adapter/Gateway，也不能构造局部 Gateway 旁路。只有
`interfaces/local_stack.py` 可以 import `xiaowei_agent.tools`；新增 worker 不能扩大该白名单。

---

## 非目标

- 不做复杂 RBAC、审批流、无停机热更新、插件市场或通用配置 DSL。
- 不在 UI 输入、保存或显示 secret 明文和真实文件路径。
- 不用 Admin 权限解锁 E1、任意 SQL、跨租户查看或 provider 自动选择。

## ADR

新增 `docs/adr/ADR-016-versioned-admin-configuration.md`，固定逻辑 target/credential registry、配置与
secret 真源、不可变版本、测试请求绑定、持久限流、CAS 发布/回滚、受控重启 readback、独立 Admin
权限和 StarRocks 正常任务链。ADR 还必须显式修订 M7/`ARCHITECTURE.md`“只有 task-worker 装配完整
执行 Runtime”的口径：configuration-test worker 是唯一窄例外，原因是它必须验证尚未发布的候选
凭证/目标，不能热切换正在服务普通任务的 active Gateway，也不能把候选 credential 放进普通
`TaskSubmission`/`ToolCall`。候选任务使用 PostgreSQL 承重的闭集 `configuration_test` dispatch lane；
普通 task-worker 只能领取 `user` lane，不能靠进程约定隔离。

## PR 边界

- PR 5A：ADR、配置契约、权限。
- PR 5B：持久化、无网络应用服务、发布/readback/回滚状态机。
- PR 5C1：TaskStore 持久化 dispatch lane、migration、普通 worker 领取边界与并发反证。
- PR 5C2：默认关闭的 configuration-test worker、`local_stack.py` 唯一装配根和真实主链测试。
- PR 5D：薄 Web Admin API 与最小页面。
- PR 5E：启动 readback 与 Compose 装配。
- ADR-016 在 PR 5A 中先于 migration 和应用实现。

## 进入条件

- 飞书、模型和 StarRocks 的字段、默认值、逻辑 credential/target reference、超时和开关已分别由 ADR 固定；没有未决 provider 字段。
- RI1、RI3、RI4 的离线安全测试均已合并；RI2/RI4 的真实证据是否完成必须在 handoff 中如实记录。
- 从当时最新 `main` 创建 `claude/web-admin-config` 并重新记录 SHA/status。
- 用户批准“发布后受控重启才生效”的最小方案；本阶段不做无停机热更新。

### Task 1：固定 Admin 权限和配置契约

**Files:**

- Create: `docs/adr/ADR-016-versioned-admin-configuration.md`
- Modify: `ARCHITECTURE.md`
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Create: `src/xiaowei_agent/contracts/configuration.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/interfaces/feishu_identity.py`
- Modify: `tests/unit/test_channel_contracts.py`
- Modify: `tests/contract/test_feishu_identity.py`
- Create: `tests/security/test_admin_permission_boundary.py`
- Create: `tests/security/test_configuration_reference_boundary.py`

- [ ] **Step 1: 写 RED 权限测试**

新增独立权限：

```python
class ChannelPermission(StrEnum):
    VIEW_SAFE_TASK = "view_safe_task"
    SUBMIT_READONLY_TASK = "submit_readonly_task"
    ADMIN_ALL_SAFE_TASKS = "admin_all_safe_tasks"
    MANAGE_CONFIGURATION = "manage_configuration"
```

测试只有身份目录中的 `admin` label 获得 `MANAGE_CONFIGURATION`；viewer/operator/dba/oncall/approver、群成员、任务 admin 都不能推导该权限。已有三项权限语义保持不变。

- [ ] **Step 2: 写 RED 配置 DTO 测试**

分别定义飞书、模型、StarRocks 的 Pydantic strict DTO；不用一个自由 `dict` 表达三种配置。每种 DTO
只允许已批准字段。所有 secret 只表达为部署者预登记的逻辑 `credential_ref`；StarRocks 只表达
逻辑 `target_ref` + `credential_ref`，不能出现 host、port、user、TLS 文件、任意 endpoint、绝对/
相对路径或路径分隔符。目标 registry 和凭证 registry 的 revision 进入 draft digest。

```python
class ConfigurationVersionState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"

class SecretReferenceState(StrEnum):
    CONFIGURED = "configured"
    MISSING = "missing"
```

API read model 只有 secret 状态和引用版本摘要，不含路径实值或内容。

- [ ] **Step 3: 写 ADR**

ADR 固定：PostgreSQL 是版本/active pointer/audit 真源；secret 文件是 secret 真源；逻辑名只由受信
composition root 解析成预挂载路径/目标，Admin 无新增 registry 项权限；发布后受控重启；配置类型
闭集；测试结果绑定 draft digest + registry revisions；CAS 发布/回滚；admin 权限与执行权限分离；
StarRocks probe 走完整任务链。测试请求按 scope/actor/draft 做持久限流，同一 draft 只允许一个 in-flight。

Run: `python -m pytest tests/unit/test_channel_contracts.py tests/contract/test_feishu_identity.py tests/security/test_admin_permission_boundary.py tests/security/test_configuration_reference_boundary.py -q`

Expected: 最小实现后通过。

- [ ] **Step 4: 提交契约与 ADR**

```bash
git add docs/adr/ADR-016-versioned-admin-configuration.md ARCHITECTURE.md src/xiaowei_agent/contracts/enums.py src/xiaowei_agent/contracts/configuration.py src/xiaowei_agent/contracts/__init__.py src/xiaowei_agent/interfaces/feishu_identity.py tests/unit/test_channel_contracts.py tests/contract/test_feishu_identity.py tests/security/test_admin_permission_boundary.py tests/security/test_configuration_reference_boundary.py
git commit -m "docs(admin): define versioned configuration boundary"
```

### Task 2：实现不可变版本、审计和 migration

**Files:**

- Create: `src/xiaowei_agent/persistence/configuration.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0008_configuration_versions.py`
- Create: `tests/contract/test_configuration_store.py`
- Create: `tests/contract/test_configuration_migration.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/contract/test_schema_matches_migration.py`
- Modify: `tests/integration/test_migration_paths.py`
- Create: `tests/security/test_configuration_secret_storage.py`

- [ ] **Step 1: 写 RED store 状态机测试**

覆盖：创建 draft、不可变版本、同 kind 单一 active pointer、测试请求/摘要绑定 draft digest 与 registry
revision、未测试不能发布、CAS 冲突、回滚到已发布版本、跨 tenant/environment 拒绝、审计
append-only、终态版本不可修改、同 draft 并发测试拒绝、actor/scope 频率上限和过期 claim 恢复。

- [ ] **Step 2: 设计最小五表结构**

- `configuration_versions`：scope、kind、version、state、canonical public payload、secret reference digest、created actor/time；
- `active_configurations`：scope + kind 唯一 active version 和 CAS revision；
- `configuration_test_requests`：version/digest、registry revisions、claim/fencing、请求 actor/time、状态和限流事实；
- `configuration_test_results`：request/version、结果闭集、耗时、测试时间、配置 digest、safe error code、可选正常 task/evidence refs；
- `runtime_configuration_readbacks`：component、kind、loaded version/digest、loaded time、safe status；
- 审计复用现有 durable trace/audit 机制；若现有结构不能表达 admin action，再新增专用 append-only 表并在 ADR 中说明。

数据库不得出现 secret 内容或可直接定位 secret 文件的 API readback 值。

- [ ] **Step 3: 实现 store 与 migration**

使用 SQLAlchemy Core、唯一约束和 compare-and-swap；rollback 只切 active pointer，不删除历史。
migration upgrade 是追加式；downgrade 作为显式 destructive 操作，不纳入普通部署回滚。所有新增
store Protocol 的 memory/PostgreSQL 实现都在 `_conformance.py` 锚定，并由 Protocol 方法集/关键字
参数测试承重。

Run: `python -m pytest tests/contract/test_configuration_store.py tests/contract/test_configuration_migration.py tests/contract/test_protocol_conformance.py tests/contract/test_schema_matches_migration.py tests/integration/test_migration_paths.py tests/security/test_configuration_secret_storage.py -q`

Expected: 全部通过。

- [ ] **Step 4: 安全反证并提交 PR 5B**

在独立临时副本让 store 接受 `secret_value` 或覆盖已发布版本，确认安全测试变红；恢复后提交。

```bash
git add src/xiaowei_agent/persistence/configuration.py src/xiaowei_agent/persistence/schema.py src/xiaowei_agent/persistence/rows.py src/xiaowei_agent/persistence/postgres.py src/xiaowei_agent/persistence/fake.py src/xiaowei_agent/persistence/memory.py src/xiaowei_agent/_conformance.py src/xiaowei_agent/persistence/migrations/versions/rev_0008_configuration_versions.py tests/contract/test_configuration_store.py tests/contract/test_configuration_migration.py tests/contract/test_protocol_conformance.py tests/contract/test_schema_matches_migration.py tests/integration/test_migration_paths.py tests/security/test_configuration_secret_storage.py
git commit -m "feat(admin): persist immutable configuration versions"
```

### Task 3：无网络应用服务与测试请求状态机

**Files:**

- Create: `src/xiaowei_agent/application/configuration.py`
- Create: `tests/contract/test_configuration_service.py`
- Modify: `tests/security/test_admin_permission_boundary.py`
- Modify: `tests/security/test_runtime_bypass.py`

- [ ] **Step 1: 写 RED 应用权限和状态测试**

每个 command 都携带服务端 `AuthenticatedPrincipal`，先要求同 scope 的 `MANAGE_CONFIGURATION`，
再执行业务。测试未知 kind、未知字段、过期 test result、draft/registry revision 已变化、并发
publish/rollback、重复请求、频率超限和非 admin 全部 fail-closed。

- [ ] **Step 2: 实现 draft/request-test/publish/rollback/readback**

```python
class ConfigurationService:
    async def create_draft(self, command: CreateConfigurationDraft) -> ConfigurationView: ...
    async def request_connection_test(self, command: TestConfigurationDraft) -> TestRequestView: ...
    async def publish(self, command: PublishConfigurationDraft) -> ConfigurationView: ...
    async def rollback(self, command: RollbackConfiguration) -> ConfigurationView: ...
    async def readback(self, query: ConfigurationReadbackQuery) -> ConfigurationReadbackView: ...
```

该 service 只校验权限/状态并持久化测试请求，不持有 provider port。发布要求最近一次成功测试与
当前 draft digest、target registry revision、credential registry revision 精确相同；返回
`restart_required=true`。readback 分开显示“数据库 active version”和“每个进程 loaded version”，
不把发布成功误报成运行已生效。

- [ ] **Step 3: 保持 application 绕过门原样承重**

`tests/security/test_runtime_bypass.py` 继续扫描全部 `application/**/*.py`；不得排除 configuration
模块，也不得改弱 AST 规则。新增反例证明 application service 无法 import Gateway、访问 `invoke`
或构造 `PlanStep`/`ToolCall`/`AdmissionCertificate`。这不是为测试让路，而是架构硬门。

Run: `python -m pytest tests/contract/test_configuration_service.py tests/security/test_admin_permission_boundary.py tests/security/test_runtime_bypass.py -q`

Expected: 全部通过。

- [ ] **Step 4: 提交无网络应用服务**

```bash
git add src/xiaowei_agent/application/configuration.py tests/contract/test_configuration_service.py tests/security/test_admin_permission_boundary.py tests/security/test_runtime_bypass.py
git commit -m "feat(admin): add tested publish and rollback service"
```

### Task 4：用持久化 dispatch lane 隔离候选探测任务

**Files:**

- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/task.py`
- Modify: `src/xiaowei_agent/persistence/store.py`
- Modify: `src/xiaowei_agent/persistence/decisions.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/application/task_view_runtime.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0009_configuration_test_dispatch_lane.py`
- Modify: `tests/suites/task_store.py`
- Modify: `tests/contract/test_task_submission.py`
- Modify: `tests/contract/test_dispatch_and_attempts.py`
- Modify: `tests/contract/test_lease_decisions.py`
- Modify: `tests/contract/test_worker_loop.py`
- Modify: `tests/contract/test_task_view_runtime.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/contract/test_schema_matches_migration.py`
- Modify: `tests/integration/test_dispatch_and_attempts_postgres.py`
- Modify: `tests/integration/test_task_submission_postgres.py`
- Modify: `tests/integration/test_migration_paths.py`
- Create: `tests/security/test_configuration_dispatch_lane.py`

- [ ] **Step 1: 写 RED lane 契约和迁移测试**

新增闭集 `TaskDispatchLane.USER` 与 `TaskDispatchLane.CONFIGURATION_TEST`，并把 lane 放进不可变
`TaskRecord`，而不是 `TaskSubmission`。普通内存/测试构造可使用兼容默认值 `USER`，但两个持久化
创建入口必须显式写入并断言自己的固定 lane。lane 是服务端持久化事实，不能来自
`RequestEnvelope`、HTTP body、用户文本、模型输出或 adapter payload。已有任务在 migration 中回填
`USER`。

现有 `create_task`、`list_dispatchable_tasks`、`list_stale_leases`、`begin_task_attempt` 与
`acquire_lease` 的公开签名不增加 lane 参数，而是在存储实现内部固定使用 `USER`；另建只供候选栈
持有的窄 `ConfigurationTestTaskStore`，其 create/list-dispatchable/list-stale/begin 方法固定使用
`CONFIGURATION_TEST`。两组公开方法调用同一私有实现，私有领取方法在加锁/同一数据库事务内核对
期望 lane。这样正常调用点无需批量传一个形式化默认值，也不存在用户或普通 worker 伪造 lane 的
入口。候选列表过滤和领取检查缺一不可：只过滤列表会被已知 task_id 绕过，只检查领取会造成跨
lane 扫描和饥饿。

Run: `python -m pytest tests/contract/test_task_submission.py tests/contract/test_dispatch_and_attempts.py tests/contract/test_lease_decisions.py tests/contract/test_task_view_runtime.py tests/integration/test_task_submission_postgres.py tests/integration/test_dispatch_and_attempts_postgres.py tests/security/test_configuration_dispatch_lane.py -q`

Expected: 当前 TaskStore 没有 lane，跨 worker 领取反例失败。

- [ ] **Step 2: 实现 schema、migration 与 store 原子约束**

任务行持久化不可变 `dispatch_lane` 并进入 dispatch index；配置测试结果/evidence 绑定 task lane。
用户 task page 和 `TaskViewRuntime` 只投影 `USER`，候选结果只从配置测试 readback 读取，不能混入
普通任务列表。fake/PostgreSQL 共享同一 contract suite。retry、stale recovery、lease/fencing 和
终态保护必须保留原 lane，禁止通过重试把 configuration-test 任务改回 user lane。rev_0009 调整
幂等唯一约束时必须把 lane 作为独立维度并安全回填既有行为，不能让内部配置请求被同字符串的用户
idempotency key 抢占；不修改已合并的 rev_0008。`renew_lease`、retry 和后续 step 方法可保持
lane 无关，因为它们必须携带已签发 grant 的 owner/fencing 事实，且不得修改 lane。

- [ ] **Step 3: 把普通 task-worker 固定在 user lane**

现有 `WorkerLoop` 不改调用形状：它拿到的 `TaskStore` 普通方法在 store 内部固定为 `USER`；API/Web/
飞书提交仍只调用固定创建 USER 的 `create_task`，请求 DTO 中根本不存在 lane 字段。候选 worker 只
拿到窄 `ConfigurationTestTaskStore` port。configuration-test lane 尚未启用时，现有任务行为保持
不变。

- [ ] **Step 4: 做并发与 TDD 反证**

用 barrier 同时启动 user worker 和 configuration-test claimant：user worker 看到/领取候选任务次数
必须为 0；configuration claimant 只能领取 configuration-test lane；普通 `begin_task_attempt`、
`acquire_lease`、过期 lease 恢复、retry 和直接 task_id 领取都不能跨 lane，用户 task page 也不返回
候选任务。临时移除列表过滤或领取二次核对，分别确认安全测试变红，恢复后全绿。

- [ ] **Step 5: 提交 PR 5C1**

```bash
git add src/xiaowei_agent/contracts/enums.py src/xiaowei_agent/contracts/task.py src/xiaowei_agent/persistence/store.py src/xiaowei_agent/persistence/decisions.py src/xiaowei_agent/persistence/rows.py src/xiaowei_agent/persistence/schema.py src/xiaowei_agent/persistence/postgres.py src/xiaowei_agent/persistence/fake.py src/xiaowei_agent/persistence/memory.py src/xiaowei_agent/application/task_view_runtime.py src/xiaowei_agent/_conformance.py src/xiaowei_agent/persistence/migrations/versions/rev_0009_configuration_test_dispatch_lane.py tests/suites/task_store.py tests/contract/test_task_submission.py tests/contract/test_dispatch_and_attempts.py tests/contract/test_lease_decisions.py tests/contract/test_worker_loop.py tests/contract/test_task_view_runtime.py tests/contract/test_protocol_conformance.py tests/contract/test_schema_matches_migration.py tests/integration/test_task_submission_postgres.py tests/integration/test_dispatch_and_attempts_postgres.py tests/integration/test_migration_paths.py tests/security/test_configuration_dispatch_lane.py
git commit -m "feat(worker): isolate configuration test dispatch lane"
```

### Task 5：默认关闭的候选配置测试 worker

**Files:**

- Create: `src/xiaowei_agent/application/configuration_probe.py`
- Create: `src/xiaowei_agent/interfaces/configuration_test_worker.py`
- Create: `src/xiaowei_agent/interfaces/configuration_registry.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Create: `tests/contract/test_configuration_test_worker.py`
- Create: `tests/contract/test_configuration_registry.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Create: `tests/security/test_configuration_probe_boundary.py`
- Modify: `tests/security/test_gateway_boundary.py`
- Modify: `tests/security/test_module_layering.py`
- Modify: `tests/security/test_runtime_bypass.py`

- [ ] **Step 1: 写 RED worker/registry/限流测试**

测试 worker 只能 claim 已持久化请求；draft digest 或任一 registry revision 漂移时拒绝且所有 provider/
adapter 调用为 0。`target_ref`/`credential_ref` 必须精确命中启动时的闭集 registry；未知名、大小写
别名、路径分隔符、点段、symlink、非普通文件、超出批准 mount root、非法 owner/mode 均在联网前
失败。重复/并发请求由 PostgreSQL claim/fencing 和频率窗口拒绝并留下审计。另做静态等式反例：
允许 import `xiaowei_agent.tools` 的 interface 文件集合仍必须精确等于
`{"interfaces/local_stack.py"}`；`configuration_test_worker.py` 不得加入该集合。

- [ ] **Step 2: 固定独立进程的必要性和边界**

现有 task-worker 只使用已发布、已 readback 的 active 配置；候选 draft 可能选择同一目标的另一条
预登记 credential。复用现有进程将迫使它热切换 Gateway、把候选 credential 放入普通任务协议，或
让候选探测影响正在执行的用户任务，三者均否决。ADR-016 与 `ARCHITECTURE.md` 必须把本 worker 写
成 M7 单执行进程口径的唯一例外：默认关闭、不发布端口、每次只 claim 一个绑定 digest/revision 的
请求、只处理 `CONFIGURATION_TEST` lane、完成后关闭全部连接。未来若有证据证明 active worker 可
安全复用，只能先修订 ADR，不能静默合并进程。

- [ ] **Step 3: 飞书与模型使用既有窄 port**

飞书执行固定最小身份/API 探测，5 秒且不发送消息；模型执行固定 structured-output 探测，15 秒，
计入预算且不带用户数据。worker 只保存闭集结果、耗时、draft digest/registry revisions 和 safe error
code；provider 正文、endpoint、路径和 secret 不保存。

- [ ] **Step 4: StarRocks 复用隔离 lane 内的正常任务生命周期**

`interfaces/local_stack.py` 是唯一 composition root：它把 candidate 的逻辑 target/credential 名解析
为预登记 target-bound binding，返回已装配的 `ConfigurationTestStack`。worker 入口只调用该 builder
和 stack 的窄运行方法，不 import `tools/`，不自行构造 Runtime/Runner/Gateway。栈内只创建
`CONFIGURATION_TEST` lane 的固定现有 `count_queries_in_window` 只读任务，并只用窄 store port 的
对应领取方法，复用 `XiaoweiRuntime`、`DeterministicStepRunner`、`StepAdmission` 和
`DeterministicToolGateway`。必须持久化正常 task/evidence refs；adapter 调用次数必须等于 Gateway
invocation 且每次都有 admission。禁止局部简化 plan、手签 certificate、直接 `adapter.execute()`
或在 application service/worker 入口中构造 Gateway。

- [ ] **Step 5: 证明 active worker 永远抢不到候选任务**

PostgreSQL 集成测试同时运行 active task-worker 与 configuration-test worker；前者的 candidate、
attempt、Gateway 和 adapter 次数都为 0，后者恰好执行一次。config worker 崩溃/lease 过期后，只能
由同 lane 的 config worker 恢复；普通 worker 即使知道 task_id 且伪造领取也被 store 拒绝。测试结果
必须绑定 config request、task_id、draft digest、registry revisions 和 lane。

- [ ] **Step 6: Protocol 锚与安全反证**

测试请求 store/claim port、飞书/模型 probe port 的每个新生产实现都在
`src/xiaowei_agent/_conformance.py` 有类型赋值锚；`tests/contract/test_protocol_conformance.py`
检查方法关键字。反例分别让 target/credential 跳出 registry、application 触碰 Gateway、worker
直调 adapter、worker import `xiaowei_agent.tools`，确认 security gate 变红；不得修改
`test_only_local_stack_can_import_the_tools_layer` 的期望集合来放行新 worker。

Run: `python -m pytest tests/contract/test_configuration_test_worker.py tests/contract/test_configuration_registry.py tests/contract/test_protocol_conformance.py tests/integration/test_dispatch_and_attempts_postgres.py tests/security/test_configuration_dispatch_lane.py tests/security/test_configuration_probe_boundary.py tests/security/test_gateway_boundary.py tests/security/test_module_layering.py tests/security/test_runtime_bypass.py -q`

Expected: 全部通过。

- [ ] **Step 7: 提交 PR 5C2**

```bash
git add src/xiaowei_agent/application/configuration_probe.py src/xiaowei_agent/interfaces/configuration_test_worker.py src/xiaowei_agent/interfaces/configuration_registry.py src/xiaowei_agent/interfaces/local_stack.py src/xiaowei_agent/_conformance.py tests/contract/test_configuration_test_worker.py tests/contract/test_configuration_registry.py tests/contract/test_protocol_conformance.py tests/integration/test_dispatch_and_attempts_postgres.py tests/security/test_configuration_dispatch_lane.py tests/security/test_configuration_probe_boundary.py tests/security/test_gateway_boundary.py tests/security/test_module_layering.py tests/security/test_runtime_bypass.py
git commit -m "feat(admin): run isolated configuration tests"
```

### Task 6：薄 Admin API 和最小页面

**Files:**

- Modify: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Create: `src/xiaowei_agent/interfaces/web_static/admin.html`
- Create: `src/xiaowei_agent/interfaces/web_static/admin.js`
- Modify: `src/xiaowei_agent/interfaces/web_static/app.css`
- Create: `tests/contract/test_web_admin_routes.py`
- Create: `tests/security/test_web_admin_boundary.py`

- [ ] **Step 1: 写 RED 路由测试**

路由闭集：

```text
GET  /app/admin
GET  /app/api/admin/configurations
POST /app/api/admin/configurations/drafts
POST /app/api/admin/configurations/{version}/test
POST /app/api/admin/configurations/{version}/publish
POST /app/api/admin/configurations/{version}/rollback
GET  /app/api/admin/configurations/readback
```

全部要求已认证 session；变更请求要求同源 Origin、CSRF 和 `MANAGE_CONFIGURATION`。响应不含 secret/path/provider body；无权限统一 404 或 403 的既定安全语义，不泄露配置存在性。

- [ ] **Step 2: 实现薄 handler**

handler 只做 body limit、Pydantic 解析、认证上下文和 service 调用；`/test` 只返回持久化 request ID/
状态，不在路由中判断配置状态、联网或测试 provider。

- [ ] **Step 3: 实现最小页面**

只用现有 vanilla HTML/JS：三类配置表单、secret 状态、保存 draft、提交测试请求、轮询结果、发布、
回滚和 readback。没有 secret 明文输入框；页面只能选择服务端返回的逻辑 target/credential 名称，
响应不包含其 host、port 或路径实值。

Run: `python -m pytest tests/contract/test_web_admin_routes.py tests/security/test_web_admin_boundary.py -q`

Expected: 全部通过。

- [ ] **Step 4: 提交 PR 5D**

```bash
git add src/xiaowei_agent/interfaces/web_models.py src/xiaowei_agent/interfaces/web_app.py src/xiaowei_agent/interfaces/local_stack.py src/xiaowei_agent/interfaces/web_static/admin.html src/xiaowei_agent/interfaces/web_static/admin.js src/xiaowei_agent/interfaces/web_static/app.css tests/contract/test_web_admin_routes.py tests/security/test_web_admin_boundary.py
git commit -m "feat(web): add minimal configuration admin"
```

### Task 7：启动 readback 与 Compose 装配

**Files:**

- Modify: `src/xiaowei_agent/config.py`
- Modify: `.env.example`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `docker-compose.yml`
- Modify: `tests/contract/test_compose_contract.py`
- Create: `tests/integration/test_configuration_readback.py`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`

- [ ] **Step 1: 写 RED 启动/readback 测试**

每个需要 provider 的进程启动时读取 active version、校验 secret reference 和全部字段，装配成功后写 loaded readback；失败时进程不启动且不覆盖上一条成功 readback。无 active version 时保持对应 provider 关闭。
同时断言 `.env.example` 与 `_FIELD_TO_ENV` 精确同键，逻辑 registry 配置只给安全空值/默认值。

- [ ] **Step 2: 实现 bootstrap 优先级**

固定为：最小数据库/bootstrap 环境变量 → PostgreSQL active config 中的逻辑名 → 启动时只读
target/credential registries → 预挂载 secret file。Admin 数据永远不能解析为任意 host/port/path。
已有环境变量形式在迁移期只能创建初始 registry/version，不能与 active config 悄悄竞争；具体
优先级由 ADR-016 固定并由测试断言。

- [ ] **Step 3: 文档和证据**

README 说明“发布不等于生效，受控 restart + readback 才算生效”。handoff 只记录离线证据，不宣称真实 provider 已配置。

Run: `python -m pytest tests/contract/test_compose_contract.py tests/security/test_env_example_clean.py tests/integration/test_configuration_readback.py -q`

Expected: 全部通过。

- [ ] **Step 4: 提交 PR 5E**

```bash
git add .env.example src/xiaowei_agent/config.py src/xiaowei_agent/interfaces/local_stack.py docker-compose.yml tests/contract/test_compose_contract.py tests/integration/test_configuration_readback.py README.md AGENT_HANDOFF.md
git commit -m "chore(admin): load published config on restart"
```

## 验证命令

```bash
python -m pytest tests/contract/test_configuration_store.py tests/contract/test_configuration_service.py tests/contract/test_configuration_test_worker.py tests/contract/test_web_admin_routes.py tests/integration/test_configuration_readback.py -q
python -m pytest -m security -q
python -m pytest -q
ruff check .
mypy src
docker compose config
git diff origin/main...HEAD --check
```

## 退出标准

- 只有独立 `manage_configuration` 权限可访问 Admin；该权限不扩大运维执行权。
- 三类配置均有严格 DTO、不可变版本、测试绑定、显式发布、restart-required、进程 readback、审计和 CAS 回滚。
- PostgreSQL、API、UI、日志和测试均不保存或回显 secret 明文和真实路径。
- StarRocks DTO 不含 host/port/path；只选择部署者闭集 registry 中的逻辑 target/credential 名。
- application/API 只持久化测试请求；StarRocks 测试经完整 Runtime/Runner/`StepAdmission -> ToolGateway`
  主链；只有 `local_stack.py` import tools，独立测试进程的架构例外已由 ADR-016 明示。普通 worker
  只能领取 `user` lane，候选 worker 只能领取 `configuration_test` lane，TaskStore 的列表过滤、领取
  二次核对、retry 和崩溃恢复共同保持边界；新 Protocol 实现有 conformance 锚，测试请求有持久限流
  与审计。
- 页面只使用现有 Web 技术栈；未做热更新、插件系统或通用 DSL。
- RI6 未获开工口令前停止。

## 回滚

admin 用 CAS 把 active pointer 切到上一已发布版本，操作者受控重启对应 Compose 服务并确认 loaded readback。若 Admin 本身异常，关闭 Admin feature flag；已有 task/channel/runtime 继续使用上次成功加载的版本。
