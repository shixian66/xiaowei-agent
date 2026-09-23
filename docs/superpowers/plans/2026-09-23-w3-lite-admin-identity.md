# W3-lite Admin 用户、激活与审计实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Every behavior change must also use `superpowers:test-driven-development`; before claiming completion use `superpowers:verification-before-completion`.

> 状态：Review Draft V0.1。产品负责人已批准本计划的 **W3-lite 范围**；本文仍须按精确 SHA 独立审查并合入 `main`，负责人随后明确下达开工口令，才授权实现。范围批准不等于计划获批、源码完成、部署、canary 或 UAT。

**Goal:** 在现有 `/admin` 管理壳中交付三个最小闭环：待激活申请的分页查询与批准/拒绝、用户分页查询与 `USER ↔ OPERATOR`/启用禁用管理、Admin 审计分页查询；本地 Admin 与飞书 Admin 均可使用这三类功能，所有授权改变继续只经 `UserDirectoryStore.apply()`，配置写入与连接测试继续只允许本地 Admin。

**Architecture:** 继续使用同一个 FastAPI Web 进程、现有 PostgreSQL 用户目录/激活/审计表、原生 HTML/CSS/ES module 和既有 Session/Origin/CSRF/CSP 边界。新增一个应用层 `AdminIdentityService` 作为管理业务边界；读路径扩展三个既有 Store 的显式分页方法，写路径只扩展 `DirectoryCommand` 闭集并复用唯一 `apply()` 事务。W3-lite 不创建第二个身份服务、第二条授权写路径、前端框架、通知队列或普通用户门户。

**Tech Stack:** Python 3.11、Pydantic strict contracts、FastAPI/Starlette、SQLAlchemy async、Alembic、PostgreSQL 16、原生 HTML/CSS/ES modules、pytest/Ruff/mypy。

**Spec:** [Web 运维工作台、身份激活与未来结果访问边界总体设计](../specs/2026-09-19-web-operations-console-identity-activation-design.md) §6.3–6.5、§7.2–7.4、§9.3、§12、§14；[ADR-013](../../adr/ADR-013-m7-channel-boundary.md) 管理面权限边界；[ADR-014](../../adr/ADR-014-real-feishu-oauth-and-web-activation.md) OAuth/激活边界。

**基线:** `main@eff7545a2ffd0d7ef0d4c30b81982ee54242759b`（PR #75 已收口 W2 离线实施）。该提交没有打开 W3 源码门；当前离线基线在显式 `PYTHONPATH=src` 下为 `4438 passed, 362 skipped`。

---

## 0. 进入门与证据边界

### 已满足

- W1a 已交付 `UserDirectoryStore.apply()` 唯一授权写入口、同事务命令派生审计和 append-only `AdminAuditStore`。
- W1b 已交付 `ActivationRequest`、容量/CAS、批准/拒绝命令、数据库身份解析，以及未知 OAuth 不签 Session 的边界。
- W2 已交付 `/login`、`/app`、`/admin`、只读详情四个壳，当前角色/能力每次请求重建，配置写接口仍只允许 `LOCAL_ADMIN`。
- `AdminCapability` 已有 `MANAGE_USERS` 与 `VIEW_ADMIN_AUDIT`；本阶段不新增权限枚举。
- 产品负责人已明确选择 W3-lite，不要求在本阶段完成全部 W3。

### 开工条件

1. 本计划以精确 SHA 独立审查，无未解决 P0/P1。
2. 审查修订后的计划作为纯计划 PR 合入最新 `main`，`AGENT_HANDOFF.md` 记录计划版本与批准来源。
3. 负责人在计划合入后明确说“开始 W3-lite 实现”。
4. 每个实现切片从当时最新 `main` 建独立分支；前一切片合入后下一切片才开工。

### 本计划不能产生的证据

- 计划、源码、fake、CI、一次性 PostgreSQL 与本地浏览器检查均不构成真实飞书、真实 Provider、部署、canary 或用户验收。
- 计划不开放 socket、真实凭据、真实应用注册、真实用户通知或运维目标连接。
- W3-lite 完成只能记为“W3 部分范围离线完成”，不能记为“W3 完成”。

---

## 1. 精确范围与非目标

### 1.1 本阶段交付

1. **待激活管理**
   - 当前作用域的有效 `PENDING` 申请按时间倒序分页。
   - 本地 Admin 与飞书 Admin 都可批准或拒绝。
   - 批准默认 `USER`；`OPERATOR` 必须显式选择；契约层写不出 `ADMIN`。
   - 批准时 Admin 显式填写内部 `actor` 与 `display_name`；浏览器不填写、读取或显示飞书 `open_id`。
2. **用户管理**
   - 当前作用域用户按 actor 升序分页，显示安全字段：内部 user ID、actor、display name、状态、角色、是否存在飞书绑定、更新时间。
   - 只允许 `USER ↔ OPERATOR`，不创建、提升、降级、禁用或解绑任何 `ADMIN`。
   - 允许启用/禁用普通账号；禁用是全局账号状态，跨作用域账号在 W3-lite 固定拒绝，避免一个作用域 Admin 影响其他作用域。
3. **Admin 审计读取**
   - 当前作用域按 `created_at DESC, event_id DESC` 分页。
   - 可按 action/outcome 过滤；从用户/申请页进入时可用目标引用做服务端摘要过滤。
   - Web 响应不含 `target_ref_digest`、subject/open_id、Secret、正文、结果行或异常正文。
4. **双 Admin 来源**
   - 当前作用域中仍为 ACTIVE ADMIN 的 `LOCAL_ADMIN` 与 `FEISHU` 会话均可使用上述三类能力。
   - 配置读取中的 raw DTO、保存、清除和连接测试继续只接受 `LOCAL_ADMIN`，本阶段不改这条边界。

### 1.2 明确非目标

- 完整 W3 的 DBA/值班/激活通知人绑定、群 @ Admin、私聊通知、持久化通知状态、重试或 dead-letter。
- 查看或翻阅私聊/任务正文、敏感任务内容查看审计；`VIEW_PRIVATE_TASK_CONTENT` 本阶段没有消费者。
- 新建第二个 Admin、把 USER/OPERATOR 提升为 ADMIN、绑定已有 ADMIN 的飞书身份、删除用户或解绑身份。
- 普通用户后台、“我的结果”、任务列表、工作台、搜索、推荐或数据库结果 ACL。
- W4a/W4b 配置结构与数据库/Prometheus 参数，W5 release/限流/部署证据，W4c/R1 结果域。
- 真实飞书、真实 Gemini、真实运维目标、部署、canary、UAT。
- 任意 offset 分页、全文搜索、导出审计、清空审计或编辑策略表达式。

### 1.3 W3-lite 与完整 W3 的关系

W3-lite 是 W3 的可独立验收子集，不改变 `DEVELOPMENT_PLAN.md` 的 W3 退出含义。完成后：

- 可以说“用户/激活/审计三项管理闭环已离线实现”；
- 不能说“W3 已完成”；
- 完整 W3 剩余项必须另写增量计划，不得借本计划开工。

---

## 2. 当前接缝与根因判断

| 接缝 | 当前事实 | W3-lite 动作 |
| --- | --- | --- |
| `UserDirectoryStore` | 只有单项授权读取、主体解析与唯一 `apply()`；没有 Admin 列表 | 只增加批量只读 `list_admin_users`；新增两种受管命令仍只由 `apply()` 执行，不预留本阶段没有路由消费的单项 Admin 读方法 |
| `ActivationStore` | 只有 `create_or_reuse/load` | 增加有效 pending keyset 分页；批准/拒绝仍不放进该 Store |
| `AdminAuditStore` | 三个窄 append + 按 id 读取 | 增加只读分页；不增加 update/delete/clear |
| 激活事务授权 | Store 当前只允许 `IdentitySource.LOCAL_ADMIN` | 改为“来源是 LOCAL_ADMIN 或 FEISHU，且事务内账号仍 ACTIVE、当前作用域角色仍 ADMIN” |
| 普通状态/角色命令 | `SetUserStatusCommand` 与 `AssignRoleCommand` 没有 Web 所需的期望值 CAS；上一阶段已记录 status scope TOCTOU | 新增窄 `SetManagedUserStatusCommand` / `ChangeManagedUserRoleCommand`，事务内重验操作者和目标，不改变旧命令语义 |
| `WebIdentityResolution` | 有 principal/role，没有内部 `user_id` | 增 `user_id`，沿登录/再认证传到 Web session；绝不从 actor/subject 猜 user ID |
| `build_postgres_web_stack()` | 目录/激活/审计 Store 只在真实 OAuth 两端口齐全时装配 | 三个 Store 与 `AdminIdentityService` 无条件装配；OAuth adapter 仍可选 |
| `/admin` | 已有集成状态和本地配置 UI/API | 增用户、激活、审计三个区；配置区来源限制不变 |
| Schema | 没有三类分页需要的 scope+cursor 复合索引 | 新增 `rev_0017_w3_admin_query_indexes`，只加/删索引，不改数据或权限事实 |

### 2.1 为什么不让路由直接调用 Store

路由直接组装 `AdminOperationContext` 与宽命令，会把下面四件事散进每个 handler：能力判断、目标 Admin 保护、当前值 CAS、持久化错误收敛。新增 `AdminIdentityService` 是当前三个闭环共同需要的唯一应用边界，不是为未来预留的抽象。

### 2.2 为什么需要新的受管命令

`SetUserStatusCommand` 是通用内核命令，`AssignRoleCommand` 还是 upsert；它们无法表达“页面看到的旧状态仍然成立”或“只在 USER/OPERATOR 间变更”。只在 service 事务外先读再写会重开 TOCTOU。新的两个命令进入同一个 `DirectoryCommand` 联合、同一个 `apply()` 和同一个数据库事务，并携带期望状态/角色；它们不是第二条写路径。

---

## 3. 契约、分页与状态不变量

### 3.1 Store 分页契约

契约分别追加到既有领域模块，不新建同义 `admin_models.py`：

```python
# contracts/identity.py
class AdminUserListQuery(Contract):
    tenant_id: BoundedId
    environment_id: BoundedId
    after_actor: BoundedActor | None = None
    limit: StrictInt = Field(default=50, gt=0, le=100)


class AdminUserRecord(Contract):
    account: UserAccount
    assignment: UserRoleAssignment
    feishu_bound: StrictBool


class AdminUserPage(Contract):
    items: tuple[AdminUserRecord, ...] = Field(max_length=100)
    next_after_actor: BoundedActor | None = None
```

`AdminUserPage` 强制：items 的 actor 严格升序；空页不能有 cursor；有 cursor 时等于最后一项 actor。actor 全局唯一，所以不需要第二个 tie-breaker。

```python
# contracts/activation.py
class PendingActivationListQuery(Contract):
    tenant_id: BoundedId
    environment_id: BoundedId
    before_requested_at: AwareDatetime | None = None
    before_request_id: BoundedId | None = None
    limit: StrictInt = Field(default=50, gt=0, le=100)


class PendingActivationPage(Contract):
    items: tuple[ActivationRequest, ...] = Field(max_length=100)
    next_requested_at: AwareDatetime | None = None
    next_request_id: BoundedId | None = None
```

两个 cursor 字段必须同时出现或同时缺失；items 按 `(requested_at, request_id)` 严格降序；空页无 cursor；cursor 等于末项。查询只返回 `status=PENDING AND expires_at>now`，只读列表不偷偷更新状态。

```python
# contracts/admin_audit.py
class AdminAuditListQuery(Contract):
    tenant_id: BoundedId
    environment_id: BoundedId
    before_created_at: AwareDatetime | None = None
    before_event_id: BoundedId | None = None
    action: AdminAuditAction | None = None
    outcome: AdminAuditOutcome | None = None
    target_kind: AdminAuditTargetKind | None = None
    target_ref_digest: Sha256Hex | None = Field(default=None, exclude=True, repr=False)
    limit: StrictInt = Field(default=50, gt=0, le=100)


class AdminAuditPage(Contract):
    items: tuple[AdminAuditEvent, ...] = Field(max_length=100)
    next_created_at: AwareDatetime | None = None
    next_event_id: BoundedId | None = None
```

cursor 与顺序规则同 pending 页。`target_kind` 与 `target_ref_digest` 必须同时出现或同时缺失；`target_ref_digest` 只在应用到 Store 的内部查询存在，不能成为 Web 响应字段。

### 3.2 安全管理投影

新增 `contracts/admin_identity.py` 作为应用服务到 Web 的安全输出契约；它不复制 Store
查询契约，也不允许直接 `model_dump()` Store 页面：

```python
class AdminUserSummary(Contract):
    user_id: BoundedId
    actor: BoundedActor
    display_name: BoundedName
    status: UserStatus
    role: Literal[ProductRole.USER, ProductRole.OPERATOR, ProductRole.ADMIN]
    feishu_bound: StrictBool
    updated_at: AwareDatetime


class AdminPendingActivationSummary(Contract):
    request_id: BoundedId
    subject_hint: StrictStr = Field(pattern=r"^申请 · [0-9a-f]{8}$")
    source: ActivationSource
    requested_at: AwareDatetime
    expires_at: AwareDatetime


class AdminAuditSummary(Contract):
    event_id: BoundedId
    actor: BoundedActor
    auth_source: IdentitySource
    action: AdminAuditAction
    target_kind: AdminAuditTargetKind
    outcome: AdminAuditOutcome
    reason_code: AdminAuditReasonCode | None
    effect: AdminAuditEffect
    created_at: AwareDatetime


class AdminUserViewPage(Contract):
    items: tuple[AdminUserSummary, ...] = Field(max_length=100)
    next_after_actor: BoundedActor | None = None


class AdminPendingActivationPage(Contract):
    items: tuple[AdminPendingActivationSummary, ...] = Field(max_length=100)
    next_requested_at: AwareDatetime | None = None
    next_request_id: BoundedId | None = None


class AdminAuditViewPage(Contract):
    items: tuple[AdminAuditSummary, ...] = Field(max_length=100)
    next_created_at: AwareDatetime | None = None
    next_event_id: BoundedId | None = None
```

`subject_hint` 只取随机 `request_id` 的末 8 个十六进制字符，格式化为 `申请 · 12ab34cd`；它不是 subject/open_id 或 digest 的截断。安全管理 DTO 不包含 `subject_ref`、`subject_ref_digest`、事件/会话 digest、`target_ref_digest`、`decided_by` 或任意自由文本。

### 3.3 写命令与原子检查

```python
class SetManagedUserStatusCommand(Contract):
    kind: Literal["set_managed_user_status"] = "set_managed_user_status"
    user_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId
    expected_status: UserStatus
    expected_role: Literal[ProductRole.USER, ProductRole.OPERATOR]
    status: UserStatus


class ChangeManagedUserRoleCommand(Contract):
    kind: Literal["change_managed_user_role"] = "change_managed_user_role"
    user_id: BoundedId
    tenant_id: BoundedId
    environment_id: BoundedId
    expected_role: Literal[ProductRole.USER, ProductRole.OPERATOR]
    role: Literal[ProductRole.USER, ProductRole.OPERATOR]
```

契约拒绝 `expected == target` 的 no-op；Store 在同一事务内完成：

1. 锁定并确认操作者账号 ACTIVE、actor 精确相等、当前作用域角色 ADMIN，来源仅 LOCAL_ADMIN/FEISHU；
2. 锁定目标账号与当前作用域角色；目标不存在统一 not-found；
3. 期望状态/角色不匹配统一 conflict；
4. 任何作用域为 ADMIN 的目标都拒绝；
5. 状态是全局事实：目标若还有当前作用域之外的角色，W3-lite 拒绝状态变更；
6. 角色变更要求目标账号 ACTIVE；
7. 改变事实并写命令派生审计；审计失败整笔回滚。

内存实现用现有 snapshot/restore 表达相同回滚语义；PostgreSQL 用现有 `_write_transaction`。两边跑同一共享套件。
真库不能只锁当前 scope 的角色行：先用 `SELECT ... FOR UPDATE` 锁目标 `user_accounts` 行，再查全部 role 事实。同一 store 里既有 `SetUserStatus/AssignRole/RevokeRole/Bind/Unbind` 也必须复用这个每用户锁定 helper，否则另一个事务可在 W3-lite 进行跨 scope 检查时插入/撤销角色，重开幻读 TOCTOU。这是沿同一个 `UserDirectoryStore.apply()` 修根因，不是新写路径。
既有 `UserDirectoryDecisionDeniedError` 的类名已足够通用，但 docstring/消息中的“activation”必须改为“admin directory decision”。拒绝原因复用现有闭集：认证来源错、操作者不再是 Admin、目标不存在、scope 不匹配和 conflict；不增加自由文本理由。

### 3.4 激活审批授权

把 `_require_activation_admin*` 收敛为可复用的 `_require_admin*`，来源规则精确为：

```python
context.auth_source in {IdentitySource.LOCAL_ADMIN, IdentitySource.FEISHU}
```

来源允许不等于角色允许。Store 仍在决定事务内锁定 actor account/assignment 并要求 ACTIVE ADMIN。未知来源、actor 不匹配、角色已降级、作用域不匹配或目标已终态继续 fail-closed，并按既有闭集原因写拒绝审计。

认证来源与目录事实也要在同一事务里重验：`LOCAL_ADMIN` 必须是固定 `LOCAL_ADMIN_USER_ID`；`FEISHU` 必须在当前 scope 仍有该 `user_id` 的飞书外部身份绑定。这防止请求开头鉴权后、进入 `apply()` 前绑定被撤销仍完成一次管理动作，也防止一个内部调用者只改 `auth_source` 就伪造另一种认证来源。

### 3.5 当前操作者的内部 user ID

`WebIdentityResolution` 增加 `user_id: BoundedId`：数据库实现从 `facts.account.user_id` 返回；`StaticFeishuIdentityDirectory` 增加显式 `web_user_ids` 映射，与 `web_roles` 一样缺失即 unavailable，绝不从 actor/open_id 派生。

`AuthenticatedWebSession` 与 `_WebPrincipalSession` 携带 `user_id`；本地路径固定使用 `LOCAL_ADMIN_USER_ID`。`IssuedWebSession` 不需要缓存该值：每次业务请求仍经当前目录重新解析，撤权立即生效。

### 3.6 Admin API 请求契约

```python
class WebSetUserStatusRequest(Contract):
    user_id: BoundedId
    expected_status: UserStatus
    expected_role: Literal[ProductRole.USER, ProductRole.OPERATOR]
    status: UserStatus
    confirm: Literal[True]


class WebChangeUserRoleRequest(Contract):
    user_id: BoundedId
    expected_role: Literal[ProductRole.USER, ProductRole.OPERATOR]
    role: Literal[ProductRole.USER, ProductRole.OPERATOR]
    confirm: Literal[True]


class WebApproveActivationRequest(Contract):
    request_id: BoundedId
    actor: BoundedActor
    display_name: BoundedName
    approved_role: Literal[ProductRole.USER, ProductRole.OPERATOR] = ProductRole.USER
    confirm: Literal[True]


class WebRejectActivationRequest(Contract):
    request_id: BoundedId
    confirm: Literal[True]
```

浏览器不能提交 `actor_user_id`、auth source、tenant/environment、action、outcome、effect、target digest、operation ID 或 subject/open_id。它们分别来自当前 Session、受信 Settings、命令派生和路由层的 `trusted_trace_id()`。

---

## 4. 数据库查询与索引

新增 `rev_0017_w3_admin_query_indexes.py`，只创建下列索引：

```python
sa.Index(
    "ix_user_role_assignments_scope_user",
    USER_ROLE_ASSIGNMENTS.c.tenant_id,
    USER_ROLE_ASSIGNMENTS.c.environment_id,
    USER_ROLE_ASSIGNMENTS.c.user_id,
)

sa.Index(
    "ix_activation_requests_scope_status_requested",
    ACTIVATION_REQUESTS.c.tenant_id,
    ACTIVATION_REQUESTS.c.environment_id,
    ACTIVATION_REQUESTS.c.status,
    ACTIVATION_REQUESTS.c.requested_at.desc(),
    ACTIVATION_REQUESTS.c.request_id.desc(),
)

sa.Index(
    "ix_admin_audit_events_scope_created",
    ADMIN_AUDIT_EVENTS.c.tenant_id,
    ADMIN_AUDIT_EVENTS.c.environment_id,
    ADMIN_AUDIT_EVENTS.c.created_at.desc(),
    ADMIN_AUDIT_EVENTS.c.event_id.desc(),
)
```

- `down_revision = "0016_web_login_contexts"`。
- downgrade 只删除这三个索引，无数据丢失，不使用 destructive authorization。
- 不删除既有 `ix_admin_audit_events_created_at`；索引去重是另一个性能变更，不在本阶段顺手做。
- 迁移、`schema.py` 与离线 DDL 检查必须逐名一致；真库用例验证 upgrade/down/up 后 head 和三索引恢复。

PostgreSQL 查询使用 `limit + 1` 探测下一页，再截断到 limit。游标谓词必须与排序严格一致：

```sql
-- users
actor > :after_actor
ORDER BY actor ASC

-- pending / audit
(created_at, id) < (:before_time, :before_id)
ORDER BY created_at DESC, id DESC
```

pending 的 `created_at` 对应 `requested_at`。所有查询首先带 tenant/environment；pending 另带 `status='pending' AND expires_at>:now`。禁止 offset、无界 `IN` 或先全表读入再由 Python 分页。

---

## 5. 应用服务与错误闭集

新增 `src/xiaowei_agent/application/admin_identity.py`。公开方法表面固定如下，不允许再加
通用 `execute(command)`、任意 payload 或可选绕过参数：

| 方法 | 精确关键字参数 | 返回 |
| --- | --- | --- |
| `list_users` | `actor, after_actor, limit` | `AdminUserViewPage` |
| `list_pending_activations` | `actor, before_requested_at, before_request_id, limit` | `AdminPendingActivationPage` |
| `list_audit` | `actor, before_created_at, before_event_id, action, outcome, target_user_id, target_request_id, limit` | `AdminAuditViewPage` |
| `set_user_status` | `actor, user_id, expected_status, expected_role, status, trace_id` | `None` |
| `change_user_role` | `actor, user_id, expected_role, role, trace_id` | `None` |
| `approve_activation` | `actor, request_id, account_actor, display_name, approved_role, trace_id` | `None` |
| `reject_activation` | `actor, request_id, trace_id` | `None` |

`AdminIdentityActor` 是该模块内的 frozen dataclass，字段精确为 `user_id`、`tenant_id`、
`environment_id`、`actor`、`auth_source`、`role`、`capabilities`。它不持有整个
`AuthenticatedPrincipal`，因为后者还带飞书 `subject_ref`，管理服务根本不需要这个受控 PII。实现必须满足：

- 用户/激活读写要求 `MANAGE_USERS`；审计读取要求 `VIEW_ADMIN_AUDIT`。
- 路由层用既有 `trusted_trace_id()` 生成 trace 并传给 service；service 只从受信 `actor + trace_id` 构造 `AdminOperationContext`，operation ID 为 `w3:<32-char trace id>`。应用层不导入 `interfaces.auth`，也不读客户端 header/body。
- 用户状态/角色调用 `UserDirectoryStore.apply()` 的受管命令。
- 激活批准/拒绝复用 `IdentityActivationService.decide()`；不得直接改 `ActivationStore`。
- 受管用户写若收到 `UserDirectoryDecisionDeniedError`，复用激活链路的闭集方式追加 `AdminAuditDenial` 后原样拒绝；路由不得自行组装审计候选。
- 四个写方法对 Web 返回 `None`，路由固定返回 `204 No Content`；底层返回的审计事件只用共享套件验证“恰好一条且 operation 正确”，不在事务提交后再因 service 断言报失败，也禁止把含 target digest 的 `AdminAuditEvent` 直接序列化。
- 目标审计过滤只在 service 内用 `admin_audit_target_digest()` 派生；Web 不收/返 digest。
- `target_user_id` 与 `target_request_id` 最多出现一个；service 分别派生 USER/ACTIVATION target kind 与 digest，两个同时出现按输入错误拒绝。
- `subject_hint` 只由 request ID 生成；任何 `ActivationRequest` 不直接序列化到响应。

应用错误闭集：

| 错误 | HTTP | 语义 |
| --- | ---: | --- |
| `AdminIdentityForbiddenError` | 403 | 当前会话没有所需 capability |
| `AdminIdentityNotFoundError` | 404 | 目标不存在或不在当前 scope；不区分 |
| `AdminIdentityConflictError` | 409 | 页面已过期、目标状态漂移、终态重放、Admin/跨 scope 目标不可改 |
| `AdminIdentityUnavailableError` | 503 | Store/数据库不可用；任何写均未成功 |

不得让 Pydantic/SQLAlchemy/Store 原始异常正文进入 HTTP、DOM、日志或审计；也不得用 `except Exception: return success` 吞掉失败。

---

## 6. Web 路由与 UI

### 6.1 路由闭集

```text
GET  /admin/api/users
POST /admin/api/users/status
POST /admin/api/users/role
GET  /admin/api/activations
POST /admin/api/activations/approve
POST /admin/api/activations/reject
GET  /admin/api/audit
```

- 所有路由先走 `session_allowed_to_work()`。
- 用户与激活路由再要求 `MANAGE_USERS`；审计路由要求 `VIEW_ADMIN_AUDIT`。
- 四条 POST 使用固定路径并精确加入 `JsonBodyLimitMiddleware.paths`，要求单个 `application/json`、Origin 与 CSRF。不把 ID 放进动态路径：现有中间件只做精确路径匹配，动态路由会静默绕过 body limit。
- 写目标的 `user_id/request_id` 进严格请求体并使用既有 `BoundedId` 上限；重复 query 参数、未知 query 参数、半个 cursor、limit 0/101/非整数统一 400。
- `internal-api` 不注册任何 `/admin` 路由；Web 不注册 `/v1/tasks/*`。
- 配置相关路由和 `_config_session()` 不复用新的双来源 helper，确保飞书 Admin 仍为 403。

### 6.2 管理壳

保留一个 `admin.html` 和 `admin.js`，继续复用全站 `app.css`；不拆 SPA、不加 bundler。一级区域为：

1. 概览/集成状态（既有）；
2. 用户与权限（新增用户 + 待激活）；
3. 审计（新增）；
4. AI 与飞书配置（既有，本地 Admin 可编辑，飞书 Admin 只看脱敏状态）。

交互要求：

- 用户表默认 50 行，继续加载使用返回 cursor；切换区域不预加载其它全部数据。
- 待激活表显示固定申请标识、来源、申请/过期时间；批准表单要求 actor、display name，角色默认 USER，OPERATOR 为显式选择。
- 禁用、角色变更、批准、拒绝均使用单独确认对话区；`confirm=true` 由用户明确点击后才提交。
- 写请求收到 `204` 后重新读取当前列表与审计，不在浏览器里猜测最终状态；`409` 也先刷新再提示“数据已变化”。
- 审计表显示时间、操作者、来源、动作、目标类型、结果、闭集原因/effect；不显示 digest 或正文。
- 空状态给出下一步；403/409/503 使用固定中文文案，不渲染后端正文。
- 不使用 `innerHTML`、`insertAdjacentHTML`、`document.write`、inline script/style、eval 或 `new Function`；所有外部文本走 `textContent`。
- Admin 仍是桌面页面：1024/1280/1440 验收；低于 1024 显示静态提示，不做手机后台。

---

## 7. 实施切片

### 切片 A：查询/授权内核与原子写命令

**可独立验收结果：** memory/PostgreSQL 两套实现均能分页读取用户、有效 pending 和审计；两个来源的当前 Admin 可通过应用服务执行受管用户写和激活决定；没有 HTTP/UI。

#### Task A0：冻结基线与先红清单

**Files:**

- Create: `tests/contract/test_admin_identity_contracts.py`
- Modify: `tests/contract/test_identity_store.py`
- Modify: `tests/contract/test_activation_store.py`

- [ ] 从最新 main 建 `claude/w3-lite-admin-kernel`，记录 HEAD、merge-base、status；不得触碰主工作区既有改动。
- [ ] 先写协议表面测试，期望最初因缺方法/类型而红：

```python
assert _protocol_surface(UserDirectoryStore) == {
    "apply", "list_admin_users", "load_account", "resolve_by_subject"
}
assert _protocol_surface(ActivationStore) == {"create_or_reuse", "list_pending", "load"}
assert _protocol_surface(AdminAuditStore) == {
    "append_denied", "append_started", "append_terminal", "list_events", "load"
}
```

- [ ] 写 page 正常/空页/乱序/等值/半 cursor/错误末项 cursor/limit 0 和 101 的反例，确认红在缺契约而非导入错误。
- [ ] Commit: `test(w3): specify admin identity query contracts`

#### Task A1：实现契约与 session actor ID 贯通

**Files:**

- Modify: `src/xiaowei_agent/contracts/identity.py`
- Modify: `src/xiaowei_agent/contracts/activation.py`
- Modify: `src/xiaowei_agent/contracts/admin_audit.py`
- Create: `src/xiaowei_agent/contracts/admin_identity.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/interfaces/feishu_identity.py`
- Modify: `src/xiaowei_agent/interfaces/directory_identity.py`
- Modify: `src/xiaowei_agent/interfaces/web_auth.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `tests/contract/test_feishu_identity.py`
- Modify: `tests/contract/test_directory_identity.py`
- Modify: `tests/contract/test_web_auth.py`
- Modify: `tests/contract/test_web_app_routes.py`

- [ ] 最小实现第 3 节 page/受管命令与 validator；更新 `DirectoryCommand`、命令→action/effect 总映射及全集测试。
- [ ] 先补 `WebIdentityResolution.user_id` 构造点清单测试，再逐个更新数据库/静态实现、测试替身、认证结果和 `_WebPrincipalSession`。
- [ ] 正常对照：本地 Admin 得到 `LOCAL_ADMIN_USER_ID`；飞书 Admin 得到数据库 account user_id；actor/open_id 与 user_id 不同时仍返回精确 user_id。
- [ ] 反例：Static 目录只有 role 没 user ID 时 unavailable；禁用/撤权仍 401，不回落激活。
- [ ] Run:

```bash
python -m pytest tests/contract/test_admin_identity_contracts.py tests/contract/test_feishu_identity.py tests/contract/test_directory_identity.py tests/contract/test_web_auth.py tests/contract/test_web_app_routes.py -q
```

- [ ] Commit: `feat(w3): define bounded admin identity contracts`

#### Task A2：新增查询索引迁移

**Files:**

- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0017_w3_admin_query_indexes.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `tests/contract/test_schema_matches_migration.py`
- Modify: `tests/integration/test_migration_paths.py`

- [ ] 先写离线 DDL 索引集合相等测试与真库 upgrade/down/up 恢复测试，确认缺 `rev_0017` 时红。
- [ ] 按第 4 节精确创建三个索引；不改表、列、CHECK 或既有 revision。
- [ ] 真库用例必须 `try/finally` 升回 head，并断言三张表与三索引恢复，避免 session 级 fixture 污染后续用例。
- [ ] Run:

```bash
python -m pytest tests/contract/test_schema_matches_migration.py -q
python -m pytest tests/integration/test_migration_paths.py -q
```

第二条无 DSN 时允许只按统一理由 skip；切片完成前 CI integration 必须 0 skip。

- [ ] Commit: `feat(w3): index bounded admin queries`

#### Task A3：实现三类 Store 分页

**Files:**

- Modify: `src/xiaowei_agent/persistence/identity.py`
- Modify: `src/xiaowei_agent/persistence/activation.py`
- Modify: `src/xiaowei_agent/persistence/admin_audit.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Create: `tests/suites/admin_identity_queries.py`
- Create: `tests/contract/test_admin_identity_queries.py`
- Create: `tests/integration/test_admin_identity_queries_postgres.py`
- Modify: `tests/contract/test_identity_store.py`
- Modify: `tests/contract/test_activation_store.py`
- Modify: `tests/contract/test_protocol_conformance.py`

- [ ] 先把同一共享套件绑定到 memory/PostgreSQL，覆盖：scope 隔离、排序、两页往返、恰好一页、末页、同时间 tie-break、过期待办排除、过滤、无绑定用户、disabled 用户仍可被 Admin 列出。
- [ ] 加计数反例证明列表是固定次数批量查询，不出现每用户一次绑定查询的 N+1。
- [ ] 实现 `limit+1`、严格游标谓词和 scope-first SQL；用户绑定用 `EXISTS`/外连接一次算出。PostgreSQL 公开读方法沿用既有 `@_persistence_boundary(write=False)`，未知 SQL/驱动异常不得直接到应用层。
- [ ] 协议、两个实现与 `_conformance.py` 表面必须精确相等。
- [ ] Run:

```bash
python -m pytest tests/contract/test_admin_identity_queries.py tests/integration/test_admin_identity_queries_postgres.py -q
```

- [ ] Commit: `feat(w3): add scoped admin identity queries`

#### Task A4：实现受管用户写与双来源 Admin 决定

**Files:**

- Modify: `src/xiaowei_agent/persistence/identity.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `tests/suites/identity_directory.py`
- Modify: `tests/security/test_identity_write_path.py`
- Modify: `tests/integration/test_identity_directory_postgres.py`

- [ ] 先写共享套件红灯：LOCAL_ADMIN/FEISHU 当前 Admin 正常；Operator、User、disabled Admin、actor/user ID 不匹配、跨 scope、飞书绑定已撤销、伪造 LOCAL_ADMIN 来源、目标 Admin、跨 scope 账号、陈旧 expected 值全部拒绝且事实/审计零变化。
- [ ] 正常对照覆盖 USER→OPERATOR、OPERATOR→USER、ACTIVE→DISABLED→ACTIVE，以及 local/Feishu 各批准和拒绝一条 pending。
- [ ] 并发真库用例让两个状态/角色请求共享同一期望值，只允许一个提交；loser 为 conflict，授权事实和审计各一条。
- [ ] 用可观测的行锁等待证明受管状态修改与既有 `AssignRole/RevokeRole` 按同一 user 串行；不用固定 sleep。先提交跨 scope 角色时状态写必须拒绝，先完成状态写时后续命令只能在其后线性化。
- [ ] 复用并泛化现有 `_require_activation_admin*`，不要复制第二份 Admin 检查。
- [ ] AST 守卫证明新增命令只由 `UserDirectoryStore.apply()` 分派；授权表写点和完整 audit candidate helper 精确名单同步更新，不用 startswith/整模块跳过。
- [ ] 隔离变异：去掉 expected CAS、目标 Admin 检查、来源与目录身份重验、审计写四项分别必须使对应测试因预期原因转红。
- [ ] Commit: `feat(w3): enforce atomic managed user changes`

#### Task A5：实现应用服务并装配无 OAuth 的本地 Admin 路径

**Files:**

- Create: `src/xiaowei_agent/application/admin_identity.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Create: `tests/contract/test_admin_identity_service.py`
- Modify: `tests/unit/test_local_stack.py`
- Modify: `tests/security/test_module_layering.py`
- Modify: `tests/security/test_task_view_runtime_authority.py`

- [ ] 先写 service 红灯：`AdminIdentityActor` 精确字段集不含 principal/subject、能力拒绝、分页投影不泄露、目标 digest 只进查询、closed error mapping、四类写只走既有 service/store。
- [ ] `WebStack` 增加必填 `admin_identity_service`；`build_postgres_web_stack()` 无条件装配唯一一组 directory/activation/audit store 与 admin service；OAuth 可用时 `DirectoryFeishuIdentityDirectory`、`IdentityActivationService`、`WebAuthService` 复用这组实例，不重复构造第二组。只有真实 OAuth adapter 仍受 oauth+membership 控制。
- [ ] 构建 `oauth=None, membership=None` 的真实窄栈，断言本地 Admin service 可用且没有加载 Runner/Gateway/tool adapter。
- [ ] 更新 process allowlist 为实际加载集合相等；不得扩大为顶层包子集。
- [ ] Run:

```bash
python -m pytest tests/contract/test_admin_identity_service.py tests/unit/test_local_stack.py tests/security/test_task_view_runtime_authority.py -q
python -m pytest -m security -q
```

- [ ] Commit: `feat(w3): assemble the admin identity service`

#### Task A6：切片 A 收尾

- [ ] 逐文件自审契约→memory/PostgreSQL→service→装配，确认读写 scope、异常、并发、回滚与审计一致。
- [ ] 运行 ADR-008 四门；有 DSN 的 CI integration 必须 0 skip：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

- [ ] 构建 wheel，确认没有意外依赖或静态资源变化。
- [ ] 推分支、开 PR；PR 描述分开写已验证/只读推理/未覆盖/残余风险。等待独立 exact-SHA 审查与合入，不自行进入切片 B。

### 切片 B：Admin API、页面与离线收口

**前置：** 切片 A 已审查并合入最新 main。

**可独立验收结果：** 两类 Admin 可在现有 `/admin` 页面完成 W3-lite 三个闭环；后端权限、CSRF、错误、XSS 和隐私不依赖前端隐藏。

#### Task B0：先写 Web 路由红灯

**Files:**

- Create: `tests/contract/test_web_admin_identity_api.py`
- Modify: `tests/contract/test_web_app_routes.py`
- Modify: `tests/security/test_web_xss.py`

- [ ] 先断言 7 条路由闭集、未登录 401、非 Admin/缺 capability 403、Local/Feishu Admin 正常、raw config 对 Feishu Admin 仍 403。
- [ ] 四条 POST 逐条覆盖缺 Origin、恶意/重复 Origin、缺/错/重复 CSRF、错误 Content-Type、超 body 上限、额外字段、`confirm=false`。
- [ ] 隐私反例把看似真实的 subject/Secret 拆开构造，断言 JSON/HTML/日志不含；不得提交连续 secret-shaped literal。
- [ ] 运行聚焦测试，确认红在路由/模型尚不存在。
- [ ] Commit: `test(w3): specify the admin identity web boundary`

#### Task B1：实现 Web 写请求模型与 API

**Files:**

- Modify: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `tests/contract/test_web_admin_identity_api.py`
- Modify: `tests/contract/test_web_app_routes.py`
- Modify: `tests/security/test_web_task_access.py`

- [ ] 直接复用第 3.2 节安全响应契约，实现第 3.6 节写请求模型、query parser 和 capability-specific session helpers；不要复制一套响应 DTO，也不要扩宽既有 `admin_session()`/`local_admin_session()`。
- [ ] 注册 7 条路由，把 4 条写路径加入 body-limit；服务端生成 context，不采纳用户 scope/identity/operation；写成功统一返回 `204`，不序列化审计事件。
- [ ] 404/409/503 只返回既有闭集 error body；禁止返回 Store/数据库正文。
- [ ] 用真实 ASGI 路由证明：禁用/降级后旧 Session 下一次请求立即 401/403；批准不签发 Session，申请人必须重新 OAuth 登录。
- [ ] 隔离变异：删 capability 检查、CSRF、body-limit，或误把原始审计事件序列化的任一变体，对应用例必须转红。目标 Admin 保护的反证只在切片 A 共享 Store 套件承重，不在路由层复制。
- [ ] Commit: `feat(w3): expose governed admin identity APIs`

#### Task B2：实现 Admin UI

**Files:**

- Modify: `src/xiaowei_agent/interfaces/web_static/admin.html`
- Modify: `src/xiaowei_agent/interfaces/web_static/admin.js`
- Modify: `src/xiaowei_agent/interfaces/web_static/app.css`
- Modify: `tests/contract/test_web_static_assets.py`
- Modify: `tests/security/test_web_xss.py`

- [ ] 先写静态契约：三个新增区域、每页单一主操作、无 inline/innerHTML/eval、只调用第 6.1 节 API、配置 API 不出现在工作台脚本。
- [ ] 用 `createElement/textContent` 实现分页、空态、确认态、固定错误文案；不把响应原文插入 DOM。
- [ ] UI 不渲染 subject/digest/open_id/Secret/正文；审批表单没有 open_id 输入。
- [ ] `node --check src/xiaowei_agent/interfaces/web_static/admin.js`。
- [ ] 在本地假数据与无网络条件下用真实浏览器检查 1024/1280/1440：用户、待激活、审计、空态、403、409、503、确认取消、加载下一页；低于 1024 只有静态提示。
- [ ] Commit: `feat(w3): add the lite admin identity console`

#### Task B3：接入 composition root 与完整路径

**Files:**

- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Create: `tests/integration/test_w3_admin_identity_flow.py`
- Modify: `tests/contract/test_web_app_routes.py`

- [ ] `serve_web()` 把切片 A 已装配的 `stack.admin_identity_service` 传给 `create_app()`，不得在 route 里现场构造 Store。
- [ ] 纵向 fake 测试：未知 OAuth 从 `SAFE_TASK_DETAIL` 创建申请 → Admin 页面列出 → Feishu Admin 批准 USER → 用户重新 OAuth 登录 → 只恢复服务端保存的那一个具体详情意图。目标任务不存在独立 ACL 时仍隐藏式 404；另用既有合法 ACL 做正对照。批准不得自动执行原任务、授予结果 ACL 或开放“我的结果”。
- [ ] 本地 Admin 无 OAuth adapter 时仍可列用户/审计并处理已有申请；飞书登录按钮继续按 adapter availability 显示。
- [ ] Compose/进程边界保持无真实 socket；测试不得新增 `allow_hosts`。
- [ ] Commit: `test(w3): prove the lite admin identity flow`

#### Task B4：文档、证据与收口

**Files:**

- Modify: `ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`
- Modify: `tests/contract/test_doc_fact_binding.py`

- [ ] 只在实现与 CI 事实成立后更新真源：W3-lite 两切片状态、exact SHA/PR/CI、测试数字、浏览器检查、未覆盖与完整 W3 剩余项。
- [ ] `DEVELOPMENT_PLAN.md` 不记录进度；除非实现证明阶段定义有冲突，否则保持零 diff。
- [ ] 文档明确：W3-lite ≠ 完整 W3；配置写仍 local-only；普通用户仍只有具体结果/任务链接且受 ACL；Admin 不绕过结果 ACL。
- [ ] 运行 ADR-008 四门、真实 PostgreSQL integration 0 skip、wheel 内容核对和 `git diff --check`。
- [ ] 推分支、开 PR，等 independent exact-SHA review；不自行合并、部署或归档完整 W3。

---

## 8. 必须证明的反例矩阵

| 保护 | 正常对照 | 撤掉保护后的预期红灯 |
| --- | --- | --- |
| Admin 来源与当前角色双重校验 | Local/Feishu ACTIVE ADMIN 各成功 | 飞书 Operator 或已降级 Session 写成功即红 |
| 目标 Admin 不可改 | USER/OPERATOR 可变更 | 任意作用域 ADMIN 被禁用/改角色即红 |
| 状态/角色 expected CAS | 单请求成功 | 两并发请求都成功或产生两条成功审计即红 |
| 授权+审计同事务 | 正常写各一条成功审计 | 制造审计冲突后授权事实仍变化即红 |
| 激活批准角色闭集 | USER 默认、OPERATOR 显式 | ADMIN 能通过模型/HTTP/Store 任一层即红 |
| 激活决定 CAS | pending 一次终态化 | 重放改变终态或增加成功审计即红 |
| scope 隔离 | 当前 scope 页返回 | other tenant/environment 任一行出现即红 |
| keyset 完整性 | 两页无重无漏 | `>`/`<` 方向反转、同时间漏项、空页带 cursor 即红 |
| PII/摘要隔离 | 安全 DTO 正常 | subject/open_id/任一 digest 出现在 JSON/DOM/repr 即红 |
| 配置来源边界 | Local Admin 配置 API 成功 | Feishu Admin 能 GET raw/PUT/clear/test 任一项即红 |
| 无 OAuth 本地管理 | oauth=None 时本地 Admin 可用 | service 被绑在 OAuth 条件分支导致 unavailable 即红 |
| 前端不承担授权 | 合法按钮可用 | 直接 HTTP 绕过 UI 后成功即红 |

---

## 9. Review Focus

审查者请优先按以下问题找 P0/P1，不按文件逐行凑问题：

1. **唯一写路径是否仍为真：** 新的状态/角色/激活决定有没有绕过 `UserDirectoryStore.apply()` 或把审计放到第二个事务。
2. **授权是否事务内仍成立：** 路由检查之后，Store 是否重新锁定并确认 actor 仍是当前 scope ACTIVE ADMIN。
3. **目标 Admin 与跨 scope 是否能被误改：** 尤其账号状态是全局事实，不可只凭当前 scope 的事务外读取决定。
4. **列表是否泄露或越权：** scope 谓词、有效 pending、subject/digest、审计目标摘要、错误差异和 N+1。
5. **分页是否真的稳定：** 排序、cursor 比较方向、tie-break、limit+1、空页和并发插入边界。
6. **双来源边界是否只扩到 W3-lite：** 飞书 Admin 能管理用户/激活/审计，但 raw config/Secret/连接测试仍 local-only。
7. **本地 Admin 是否依赖 OAuth：** OAuth 未配置时三个 W3-lite 功能仍应装配；真实网络仍不可调用。
8. **测试是否命中承重点：** 反例不能因缺绑定、缺 session 或别的前置条件碰巧失败；变异必须红在目标断言。
9. **阶段诚实度：** W3-lite 不能被文档写成完整 W3、部署、真实飞书或用户验收。

---

## 10. 退出标准

W3-lite 只有同时满足以下条件才可离线收口：

- [ ] 切片 A/B 均从各自最新 main 实现、独立 exact-SHA 审查、CI 八项真实执行并合入。
- [ ] 内存与 PostgreSQL 共享套件证明查询、CAS、scope、事务回滚和审计一致；CI integration 0 skip。
- [ ] 本地 Admin 与飞书 Admin 的用户/激活/审计后端权限均由真实路由证明；配置来源边界无回退。
- [ ] 管理壳 1024/1280/1440 本地浏览器检查完成，wheel 包含静态资源；该证据明确不是 UAT。
- [ ] 安全反例矩阵与至少四项隔离变异在交付记录中给出真实失败原因。
- [ ] `AGENT_HANDOFF.md` 只写“W3-lite 部分范围离线完成”，列出完整 W3 剩余项与真实环境硬门。
- [ ] 未注册真实应用、未读取真实凭据、未发外网请求、未部署、未做 canary/UAT。
