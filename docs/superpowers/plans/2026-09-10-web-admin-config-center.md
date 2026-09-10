# Minimal Web Admin Configuration Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** 在飞书、模型和 StarRocks 配置契约稳定后，提供一个只有 admin 可用的最小 Web 配置中心，支持 draft、测试连接、显式发布、版本 readback、审计和回滚，永不保存或回显明文 secret。

**Architecture:** PostgreSQL 保存不可变配置版本、active pointer、连接测试摘要和审计；secret 仍是容器只读文件。Web 只是受 CSRF 保护的薄 API/静态页面，业务规则位于 `application/configuration.py`。发布后不做复杂热更新：目标进程在受控 Compose restart 时加载 active version，并写回自己实际加载的版本；Admin 页面据此显示 readback。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、SQLAlchemy Core、Alembic、PostgreSQL、现有 vanilla HTML/CSS/JS、Docker Compose、pytest。

**Spec:** [真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)

**Global Constraints:** 不引入前端框架、通用配置 DSL、secret vault 产品或动态插件系统。Admin 权限不授予 E1、任意 SQL或跨租户访问。StarRocks 测试连接仍必须经过 `StepAdmission -> ToolGateway`，不能由 Web/API 直接调用 adapter。

## 非目标

- 不做复杂 RBAC、审批流、无停机热更新、插件市场或通用配置 DSL。
- 不在 UI 输入、保存或显示 secret 明文和真实文件路径。
- 不用 Admin 权限解锁 E1、任意 SQL、跨租户查看或 provider 自动选择。

## ADR

新增 `docs/adr/ADR-016-versioned-admin-configuration.md`，固定配置与 secret 真源、不可变版本、测试绑定、CAS 发布/回滚、受控重启 readback 和独立 Admin 权限。

## PR 边界

- PR 5A：ADR、配置契约、权限、持久化和 migration。
- PR 5B：应用服务、测试连接、发布/readback/回滚。
- PR 5C：薄 Web Admin API/页面与 Compose 装配。
- ADR-016 在 PR 5A 中先于 migration 和应用实现。

## 进入条件

- 飞书、模型和 StarRocks 的字段、默认值、secret reference、超时和开关已分别由 ADR 固定；没有未决 provider 字段。
- 阶段 1、3、4 的离线安全测试均已合并；阶段 2/4 的真实证据是否完成必须在 handoff 中如实记录。
- 从当时最新 `main` 创建 `claude/web-admin-config` 并重新记录 SHA/status。
- 用户批准“发布后受控重启才生效”的最小方案；本阶段不做无停机热更新。

## Task 1：固定 Admin 权限和配置契约

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

**Step 1: 写 RED 权限测试**

新增独立权限：

```python
class ChannelPermission(StrEnum):
    VIEW_SAFE_TASK = "view_safe_task"
    SUBMIT_READONLY_TASK = "submit_readonly_task"
    ADMIN_ALL_SAFE_TASKS = "admin_all_safe_tasks"
    MANAGE_CONFIGURATION = "manage_configuration"
```

测试只有身份目录中的 `admin` label 获得 `MANAGE_CONFIGURATION`；viewer/operator/dba/oncall/approver、群成员、任务 admin 都不能推导该权限。已有三项权限语义保持不变。

**Step 2: 写 RED 配置 DTO 测试**

分别定义飞书、模型、StarRocks 的 Pydantic strict DTO；不用一个自由 `dict` 表达三种配置。每种 DTO 只允许已批准字段，并把 secret 表达为受限的绝对 `secret_ref`。

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

**Step 3: 写 ADR**

ADR 固定：PostgreSQL 是版本/active pointer/audit 真源；secret 文件是 secret 真源；发布后受控重启；配置类型闭集；测试结果绑定 draft digest；CAS 发布/回滚；admin 权限与执行权限分离；StarRocks probe 仍走 Gateway。

Run: `python -m pytest tests/unit/test_channel_contracts.py tests/contract/test_feishu_identity.py tests/security/test_admin_permission_boundary.py -q`

Expected: 最小实现后通过。

**Step 4: 提交契约与 ADR**

```bash
git add docs/adr/ADR-016-versioned-admin-configuration.md ARCHITECTURE.md src/xiaowei_agent/contracts/enums.py src/xiaowei_agent/contracts/configuration.py src/xiaowei_agent/contracts/__init__.py src/xiaowei_agent/interfaces/feishu_identity.py tests/unit/test_channel_contracts.py tests/contract/test_feishu_identity.py tests/security/test_admin_permission_boundary.py
git commit -m "docs(admin): define versioned configuration boundary"
```

## Task 2：实现不可变版本、审计和 migration

**Files:**

- Create: `src/xiaowei_agent/persistence/configuration.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0008_configuration_versions.py`
- Create: `tests/contract/test_configuration_store.py`
- Create: `tests/contract/test_configuration_migration.py`
- Modify: `tests/contract/test_schema_matches_migration.py`
- Modify: `tests/integration/test_migration_paths.py`
- Create: `tests/security/test_configuration_secret_storage.py`

**Step 1: 写 RED store 状态机测试**

覆盖：创建 draft、不可变版本、同 kind 单一 active pointer、测试摘要绑定 draft digest、未测试不能发布、CAS 冲突、回滚到已发布版本、跨 tenant/environment 拒绝、审计 append-only、终态版本不可修改。

**Step 2: 设计最小四表结构**

- `configuration_versions`：scope、kind、version、state、canonical public payload、secret reference digest、created actor/time；
- `active_configurations`：scope + kind 唯一 active version 和 CAS revision；
- `configuration_test_results`：version、结果闭集、耗时、测试时间、配置 digest、safe error code；
- `runtime_configuration_readbacks`：component、kind、loaded version/digest、loaded time、safe status；
- 审计复用现有 durable trace/audit 机制；若现有结构不能表达 admin action，再新增专用 append-only 表并在 ADR 中说明。

数据库不得出现 secret 内容或可直接定位 secret 文件的 API readback 值。

**Step 3: 实现 store 与 migration**

使用 SQLAlchemy Core、唯一约束和 compare-and-swap；rollback 只切 active pointer，不删除历史。migration upgrade 是追加式；downgrade 作为显式 destructive 操作，不纳入普通部署回滚。

Run: `python -m pytest tests/contract/test_configuration_store.py tests/contract/test_configuration_migration.py tests/contract/test_schema_matches_migration.py tests/integration/test_migration_paths.py -q`

Expected: 全部通过。

**Step 4: 安全反证并提交 PR 5A**

在独立临时副本让 store 接受 `secret_value` 或覆盖已发布版本，确认安全测试变红；恢复后提交。

```bash
git add src/xiaowei_agent/persistence/configuration.py src/xiaowei_agent/persistence/schema.py src/xiaowei_agent/persistence/rows.py src/xiaowei_agent/persistence/postgres.py src/xiaowei_agent/persistence/fake.py src/xiaowei_agent/persistence/memory.py src/xiaowei_agent/persistence/migrations/versions/rev_0008_configuration_versions.py tests/contract/test_configuration_store.py tests/contract/test_configuration_migration.py tests/contract/test_schema_matches_migration.py tests/integration/test_migration_paths.py tests/security/test_configuration_secret_storage.py
git commit -m "feat(admin): persist immutable configuration versions"
```

## Task 3：应用服务和三类测试连接

**Files:**

- Create: `src/xiaowei_agent/application/configuration.py`
- Create: `src/xiaowei_agent/application/configuration_testing.py`
- Create: `tests/contract/test_configuration_service.py`
- Create: `tests/contract/test_configuration_testing.py`
- Modify: `tests/security/test_admin_permission_boundary.py`
- Modify: `tests/security/test_gateway_boundary.py`

**Step 1: 写 RED 应用权限和状态测试**

每个 command 都携带服务端 `AuthenticatedPrincipal`，先要求同 scope 的 `MANAGE_CONFIGURATION`，再执行业务。测试未知 kind、未知字段、过期 test result、draft 已变化、并发 publish/rollback、重复请求和非 admin 全部 fail-closed。

**Step 2: 实现 draft/test/publish/rollback/readback**

```python
class ConfigurationService:
    async def create_draft(self, command: CreateConfigurationDraft) -> ConfigurationView: ...
    async def test_connection(self, command: TestConfigurationDraft) -> TestResultView: ...
    async def publish(self, command: PublishConfigurationDraft) -> ConfigurationView: ...
    async def rollback(self, command: RollbackConfiguration) -> ConfigurationView: ...
    async def readback(self, query: ConfigurationReadbackQuery) -> ConfigurationReadbackView: ...
```

发布要求最近一次成功测试与当前 draft digest 精确相同；返回 `restart_required=true`。readback 分开显示“数据库 active version”和“每个进程 loaded version”，不把发布成功误报成运行已生效。

**Step 3: 实现 provider probe**

- 飞书：固定最小身份/API 探测，5 秒，无消息发送；
- 模型：固定最小 structured-output 探测，15 秒，计入预算且不带用户数据；
- StarRocks：用 draft 构造隔离的 target-bound adapter，但仍通过既有 deterministic plan、`StepAdmission` 和一个局部 `ToolGateway` 执行固定 count operation；不得从 Web 直接调用 adapter，也不得增加任意 SQL。

所有 probe 都只保存闭集结果、耗时和配置 digest；provider 正文不保存。

Run: `python -m pytest tests/contract/test_configuration_service.py tests/contract/test_configuration_testing.py tests/security/test_admin_permission_boundary.py tests/security/test_gateway_boundary.py -q`

Expected: 全部通过。

**Step 4: 提交 PR 5B**

```bash
git add src/xiaowei_agent/application/configuration.py src/xiaowei_agent/application/configuration_testing.py tests/contract/test_configuration_service.py tests/contract/test_configuration_testing.py tests/security/test_admin_permission_boundary.py tests/security/test_gateway_boundary.py
git commit -m "feat(admin): add tested publish and rollback service"
```

## Task 4：薄 Admin API 和最小页面

**Files:**

- Modify: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Create: `src/xiaowei_agent/interfaces/web_static/admin.html`
- Create: `src/xiaowei_agent/interfaces/web_static/admin.js`
- Modify: `src/xiaowei_agent/interfaces/web_static/app.css`
- Create: `tests/contract/test_web_admin_routes.py`
- Create: `tests/security/test_web_admin_boundary.py`

**Step 1: 写 RED 路由测试**

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

**Step 2: 实现薄 handler**

handler 只做 body limit、Pydantic 解析、认证上下文和 service 调用，不在路由中判断配置状态或测试 provider。

**Step 3: 实现最小页面**

只用现有 vanilla HTML/JS：三类配置表单、secret 状态、保存 draft、测试、发布、回滚和 readback。没有 secret 明文输入框；页面只选择由部署者预先挂载并登记的 secret reference 名称。

Run: `python -m pytest tests/contract/test_web_admin_routes.py tests/security/test_web_admin_boundary.py -q`

Expected: 全部通过。

**Step 4: 提交 PR 5C**

```bash
git add src/xiaowei_agent/interfaces/web_models.py src/xiaowei_agent/interfaces/web_app.py src/xiaowei_agent/interfaces/local_stack.py src/xiaowei_agent/interfaces/web_static/admin.html src/xiaowei_agent/interfaces/web_static/admin.js src/xiaowei_agent/interfaces/web_static/app.css tests/contract/test_web_admin_routes.py tests/security/test_web_admin_boundary.py
git commit -m "feat(web): add minimal configuration admin"
```

## Task 5：启动 readback 与 Compose 装配

**Files:**

- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `docker-compose.yml`
- Modify: `tests/contract/test_compose_contract.py`
- Create: `tests/integration/test_configuration_readback.py`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`

**Step 1: 写 RED 启动/readback 测试**

每个需要 provider 的进程启动时读取 active version、校验 secret reference 和全部字段，装配成功后写 loaded readback；失败时进程不启动且不覆盖上一条成功 readback。无 active version 时保持对应 provider 关闭。

**Step 2: 实现 bootstrap 优先级**

固定为：最小数据库/bootstrap 环境变量 → PostgreSQL active config → secret file。已有环境变量形式在迁移期只能创建初始版本或显式覆盖，不能与 active config 悄悄竞争；具体优先级由 ADR-016 固定并由测试断言。

**Step 3: 文档和证据**

README 说明“发布不等于生效，受控 restart + readback 才算生效”。handoff 只记录离线证据，不宣称真实 provider 已配置。

**Step 4: 提交装配**

```bash
git add src/xiaowei_agent/config.py src/xiaowei_agent/interfaces/local_stack.py docker-compose.yml tests/contract/test_compose_contract.py tests/integration/test_configuration_readback.py README.md AGENT_HANDOFF.md
git commit -m "chore(admin): load published config on restart"
```

## 验证命令

```bash
python -m pytest tests/contract/test_configuration_store.py tests/contract/test_configuration_service.py tests/contract/test_configuration_testing.py tests/contract/test_web_admin_routes.py tests/integration/test_configuration_readback.py -q
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
- StarRocks 测试连接仍经过 `StepAdmission -> ToolGateway`。
- 页面只使用现有 Web 技术栈；未做热更新、插件系统或通用 DSL。
- 阶段 6 未获开工口令前停止。

## 回滚

admin 用 CAS 把 active pointer 切到上一已发布版本，操作者受控重启对应 Compose 服务并确认 loaded readback。若 Admin 本身异常，关闭 Admin feature flag；已有 task/channel/runtime 继续使用上次成功加载的版本。
