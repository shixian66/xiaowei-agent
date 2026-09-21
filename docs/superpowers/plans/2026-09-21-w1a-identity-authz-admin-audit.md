# W1a 用户、权限与 Admin 审计写内核实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用持久化的用户目录、作用域角色、外部身份与本地凭据取代当前的静态 JSON 身份标签，并交付独立 append-only 的 `AdminAuditStore`，使任何授权改变都**无法**在不写成功审计的情况下提交。

**Architecture:** 产品角色（`ADMIN/OPERATOR/USER`）与认证来源（`IdentitySource`）在 `governance/product_roles.py` 里做纯函数确定性映射，角色回答"能做什么"、来源回答"从哪来"，两者不互相推导。身份事实落在四张新表。`UserDirectoryStore` **只暴露一个写方法** `apply(command, context)`：调用方只提供操作者上下文，审计事件的 action、target、outcome 与 effect 全部由 store 从命令**派生**——调用方没有机会写错，因此不需要校验它有没有写错。本地 Admin bootstrap 与旧身份批量迁移都是 `DirectoryCommand` 的成员，因此授权表在整个代码库里只有这一个写入口。旧静态 JSON 通过**一个命令、一个事务**导入，任何一步失败整批回滚。

**Tech Stack:** Python 3.11、Pydantic v2 `Contract` 基类、SQLAlchemy Core + Alembic（`rev_0014`）、asyncpg/PostgreSQL、pytest（含 `security` marker gate）、Ruff、mypy。不新增运行依赖、不新增进程、不新增 Compose 服务。

**Spec:** [小维 Web 运维工作台、身份激活与未来结果访问边界总体设计](../specs/2026-09-19-web-operations-console-identity-activation-design.md) Approved V0.3，§6、§12、§14.2、§15、§16.1、§16.2、§17.1 第 2 项。
**ADR:** [ADR-013 Web 产品修订（2026-09-20）](../../adr/ADR-013-m7-channel-boundary.md)。

## Baseline and Evidence Boundary

- 本计划分支：`claude/w1a-identity-authz-audit-plan`。
- 本计划基线：`origin/main@a12578cd59cfaccf3fe6702e6437502b9462c28d`（PR #61 squash 合入 W0）。
- W0 只证明**文档与 ADR 真源已收口**。它不是本阶段任何源码、迁移、部署、真实调用或用户验收的证据。
- 编写本计划前已按 `AGENTS.md` 顺序读取 `ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`，并读取规格、ADR-013 修订、`contracts/enums.py`、`contracts/channel.py`、`persistence/{schema,store,local_admin,clarification_records,postgres}.py`、`interfaces/{feishu_identity,local_admin_auth,web_auth,local_stack}.py` 与 `tests/suites/`、`tests/integration/conftest.py`、`tests/contract/test_schema_matches_migration.py` 的现有形状。
- 基线四门（计划分支实测）：文档契约 `46 passed`。实现者必须从届时最新 `main` 重新跑全部四门取当期数字，**不得**把本行当作未来运行结果。
- 本计划为 V2，依据 PR #62 对 `e7ff83fe21b3bc697209519e56fb0dec592e4fa1` 的复审意见做根因修订。四组根因与修订位置见文末「V2 修订记录」。

## Global Constraints

- **W1a 是写内核阶段，不是产品阶段。** 允许新增/修改的路径见 Task 0 Step 5 的 allowlist。**禁止**新增任何 HTTP 路由、页面、模板、静态 JS、Compose 服务或运行配置文件。
- **W1b/W2/W3 未授权。** 不创建 `ActivationRequest`、`ActivationStore`、`IdentityActivationService`、`web_oauth_login_contexts`、通知 Port、登录页、Admin 页面或审计查询 API。W1a 只交付它们将来要复用的写底座。
- **`ChannelPermission` 精确三成员，不扩展。** `VIEW_SAFE_TASK`、`SUBMIT_READONLY_TASK`、`ADMIN_ALL_SAFE_TASKS`。管理面能力一律进独立的 `AdminCapability`（七成员闭集）。
- **`AdminCapability` 七成员，逐字取自 ADR-013 修订**：`MANAGE_USERS`、`MANAGE_DUTY_BINDINGS`、`VIEW_ADMIN_AUDIT`、`VIEW_PRIVATE_TASK_CONTENT`、`VIEW_INTEGRATION_STATUS`、`MANAGE_INTEGRATIONS`、`RUN_CONNECTION_TESTS`。其中 `MANAGE_INTEGRATIONS` 与 `RUN_CONNECTION_TESTS` **只允许 `LOCAL_ADMIN`**。
- **审计事实由 store 从命令派生，调用方不得组装。** 调用方只提供 `AdminOperationContext`（operation、操作者、认证来源）；`action`、`target_kind`、`target_ref_digest`、`outcome` 与 effect 全部由 `UserDirectoryStore` 根据 `DirectoryCommand` 计算。**不提供任何让调用方指定 action/target/outcome 的参数**——包括 override、hook 和可选覆盖字段。理由：能被调用方指定，就能被调用方写错；"校验它有没有写错"永远弱于"它根本没有机会写"。
- **授权改变只有一条写路径。** `UserDirectoryStore.apply()` 是 `user_accounts`、`user_role_assignments`、`external_identities` 与 `local_admins.user_id` 的**唯一**写入口。本地 Admin bootstrap 与旧身份迁移都必须是 `DirectoryCommand` 的成员，不得另开写方法、另开事务或另写一份 SQL。
- **授权改变与审计同事务，且审计必须能回答"改成了什么"。** 审计写入失败必须使整个授权改变回滚。`user_created`、`role_assigned`、`user_status_changed`、`local_admin_bootstrapped`、`legacy_identity_migrated` 事件必须携带**闭集** effect（新角色 / 新状态），由数据库 CHECK 强制；否则历史状态被后续改动覆盖后，就再也无法还原当时授予了什么。
- **审计 append-only。** `AdminAuditStore` 只有 `append` 与 `load`；`persistence/` 中不得出现针对 `admin_audit_events` 的 `sa.update()` / `sa.delete()`；不提供清理、更新或删除 API。
- **审计事件不含自由文本。** 允许的列只有规格 §14.2 逐条列出的 13 个，加上闭集 effect 列 `effect_role` / `effect_status`。**不新增** `message`、`detail`、`payload`、`dict`、JSON 或任何可容纳异常正文与用户输入的列。规格 §14.2 写的是"事件**至少**包含"，因此闭集 effect 是它允许的扩展；自由文本不是。
- **一次操作最多一条 STARTED 和一条终态事件。** 规格 §14.2 要求非事务性配置操作写 `STARTED → SUCCEEDED/FAILED`，因此 `operation_id` **不能**全局唯一。用两条 partial unique index 表达：同一 `operation_id` 下 `started` 至多一条、终态（`succeeded`/`denied`/`failed`）至多一条。
- **`subject_ref` 是受控 PII。** 飞书 `open_id` 在新契约上必须 `exclude=True, repr=False`，数据库只存 domain-separated 摘要，不进普通日志、trace、异常和审计正文。
- **所有持久 ID 有界。** `user_id`、`operation_id` 等契约字段有明确上限。由外部输入派生的 ID 必须经摘要截断得到有界值，**不得**直接拼接 actor 这类没有长度上限的外部字符串。
- **认证生命周期事件不进 `AdminAuditStore`。** 登录成功/失败、改密、登出走结构化安全日志；本阶段不新建第二套认证审计表。改密因此仍留在 `LocalAdminStore`：它改的是凭据，不是授权。
- **不提前建无消费者的名单。** 不创建 requester/approver 表、列或枚举成员；旧 `approver` 标签只进迁移报告，不生效任何授权。DBA/值班绑定属于 W3 交付面，本阶段不建表。
- **旧静态 JSON 不双写。** 迁移成功后静态文件保留只读一个发布周期；本阶段不删除它，也不让它与数据库目录同时成为写入目标。
- **不放宽既有硬门。** SQLGuard、ToolPolicy、ApprovalGate、`ToolGateway`、终态保护、RI2 飞书 / RI3 Gemini / RI4 StarRocks / RI6 部署真实调用门全部不变。W1a 网络调用次数恒为 0。
- **Secret 纪律。** 测试夹具里的类 secret 字面量一律拆开写（`"hunter" + "2-plain"`），口令哈希字段用 `SecretHash` 标注并 `exclude=True, repr=False`。
- **不改权威测试命令。** 仍是 `python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。触及 `governance/` 必须全量跑 security gate。
- **RED 的判定标准。** 每个任务先写会失败的测试，并确认失败是**被测事实尚不存在**造成的——新模块尚未创建时的 `ModuleNotFoundError`、新符号尚未定义时的 `ImportError`、断言不成立时的 `AssertionError` 都是合格的 RED。不合格的是**测试自身写错**造成的失败：fixture 名拼错、参数名不符、语法错误。分辨方法是问一句：错误指向的是你要实现的东西，还是你刚写的测试？后者先修测试，再重新取 RED。
- 不得靠删断言、放宽集合、跳过用例、吞异常或加特殊分支制造全绿。
- 每个提交只暂存本任务列出的文件；不使用 `git add -A`。

## 与规格的两处有意偏离（复审已确认可接受）

1. **`UserAccount` 不带 `tenant_id/environment_id`，`actor` 全局唯一。**
   规格 §6.1 的 `UserAccount` 字段表里没有作用域，作用域只出现在 `UserRoleAssignment`。§6.6 要求"在同一 `tenant_id/environment_id` 内拒绝 actor、subject 或本地账号冲突"——全局唯一的 `actor` 严格强于该要求。复审结论：可以接受，当前角色作用域已由 assignment 承载，没有现存需求要求两个租户复用同一 actor。
2. **`LocalCredential.username` 本阶段不落库。**
   规格 §6.1 列出 `username`（首版固定 `admin`），但 W1a 没有消费者：现有 `local_admin_auth.py` 的登录只校验口令，不读用户名，登录页属于 W2。本阶段只落 `local_admins.user_id`。复审结论：可以接受，**前提是 W2 继续把 `admin` 作为固定认证常量**，不得借机新增可变用户名或第二套账号真源。该前提写入本计划，W2 计划必须复述它。

## Review Focus

审核者应优先核对以下八点：

1. `apply(command, context)` 是否真的**不给调用方任何机会**指定 action、target 或 outcome：签名上有没有 override、可选覆盖字段或 hook；`AdminAuditCandidate` 是否只出现在"什么都没改"的独立 `append` 路径上。
2. 四张授权表的写入是否**全部**发生在 `apply()` 的那一个 `begin()` 内。`grep` 一遍 `persistence/` 里对这四张表的 INSERT/UPDATE，看有没有第二处；特别核对 `seed_if_absent` 与旧身份迁移。
3. 审计失败回滚是否由**真实数据库约束**触发并在**真 PostgreSQL** 上验证；集成测试的 `user_directory` fixture 是否确实是 `PostgresUserDirectoryStore`（Task 6 有一条显式类型断言钉住它）。
4. `operation_id` 是否允许同一操作写 `STARTED` + 一条终态，且第二条 STARTED 与第二条终态都被拒绝。
5. `role_assigned` / `user_status_changed` / `user_created` 事件能否回答"改成了什么"，且 effect 是闭集列而不是自由文本或 JSON。
6. 旧标签迁移是否是**一个命令、一个事务**：中途任何一步失败后，两张目录表与审计表是否零部分落库；测试制造的冲突是不是**数据库既有冲突**，而不是被 `_IdentityDocument` 解析期就挡掉的同文件重复 actor。
7. 由外部输入派生的 `user_id` / `operation_id` 是否有界，最大长度 actor 是否有用例。
8. diff 是否只含 Task 0 的 allowlist；有没有顺手加 HTTP 路由、激活表、DBA/值班表或 requester/approver 载体。

---

### Task 0: 从最新 main 重新入职并冻结允许变更面

**Files:** 无

**Interfaces:**
- Consumes: 五份优先文档、规格 §6/§12/§14.2/§15/§16、ADR-013 修订、当前 Git/CI 事实
- Produces: W1a 实现基线记录与精确文件 allowlist

- [ ] **Step 1: 从最新 main 新建实现分支**

```bash
cd /path/to/agent
git fetch origin main
git worktree add -b claude/w1a-identity-authz-audit /tmp/xiaowei-w1a-impl origin/main
cd /tmp/xiaowei-w1a-impl
git log -1 --format='%H %s'
git status --short --branch
```

预期：HEAD 是当时的 `origin/main`，工作区干净。把该 SHA 记为本轮实现基线。

- [ ] **Step 2: 按 AGENTS.md 顺序读完五份文档**

依次读 `AGENTS.md`、`ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`。

`AGENT_HANDOFF.md` 第 0 节应当已经写明 **W0 已合入、下一步是 W1a**（该更新随本计划 PR 一起合入，见 Task 10）。若它仍写着"正在做 W0 文档收口"，说明本计划 PR 的 handoff 修订没有合进来，**停止并报告**，不要自行推断。

- [ ] **Step 3: 确认 W1a 计划已获批**

```bash
gh pr list --state merged --limit 5 --json number,title,mergeCommit
```

确认本计划所在 PR 已由负责人合入。**未合入不得开始 Task 1。**

- [ ] **Step 4: 记录基线四门数字**

```bash
python -m pytest -q 2>&1 | tail -3
python -m pytest -m security -q 2>&1 | tail -3
ruff check .
mypy src
```

四条都必须在改动前通过。把数字写进本轮工作笔记，作为后续"新增用例数"的对照基准。

- [ ] **Step 5: 冻结 allowlist**

本轮允许出现在 diff 里的路径，只有：

```
src/xiaowei_agent/contracts/enums.py
src/xiaowei_agent/contracts/identity.py
src/xiaowei_agent/contracts/admin_audit.py
src/xiaowei_agent/contracts/__init__.py
src/xiaowei_agent/governance/product_roles.py
src/xiaowei_agent/persistence/identity.py
src/xiaowei_agent/persistence/admin_audit.py
src/xiaowei_agent/persistence/schema.py
src/xiaowei_agent/persistence/memory.py
src/xiaowei_agent/persistence/postgres.py
src/xiaowei_agent/persistence/local_admin.py
src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_directory_and_admin_audit.py
src/xiaowei_agent/interfaces/legacy_identity_migration.py
src/xiaowei_agent/interfaces/feishu_identity.py
src/xiaowei_agent/_conformance.py
tests/**
ARCHITECTURE.md
AGENT_HANDOFF.md
DEVELOPMENT_PLAN.md
```

`interfaces/feishu_identity.py` 在列表里是**有意的**：Task 8 需要一份保留原始 labels 的公开只读解析契约，而当前的 `load_feishu_identity_directory()` 已经把 labels 压成了 `frozenset[ChannelPermission]`，原始标签在返回值里不复存在（见 `src/xiaowei_agent/interfaces/feishu_identity.py:125`）。Task 8 只**抽出**已有的解析逻辑并把它公开，不改变它的校验规则、大小上限、权限位检查，也不改变任何现有调用方的行为。

任何超出该列表的文件出现在 `git status` 里，都必须先停下来说明理由。

---

### Task 1: 产品角色、管理能力与认证来源的确定性映射

先做纯函数层：它没有存储依赖，是后面所有任务的词汇表，也是最容易被"Admin 就是全权"这种直觉写错的地方。

**Files:**
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Create: `src/xiaowei_agent/governance/product_roles.py`
- Test: `tests/unit/test_product_roles.py`

**Interfaces:**
- Consumes: 既有 `ChannelPermission`、`IdentitySource`
- Produces: `ProductRole`、`UserStatus`、`AdminCapability` 枚举；`channel_permissions_for(role) -> frozenset[ChannelPermission]`；`admin_capabilities_for(role, *, source) -> frozenset[AdminCapability]`

- [ ] **Step 1: 写失败测试**

创建 `tests/unit/test_product_roles.py`：

```python
"""产品角色 → 渠道权限 / 管理能力的确定性映射。

这三张表是规格 §6.2、§6.3 的逐格复制。用"整表相等"而不是逐条 `in` 断言：
逐条断言只能发现少给，发现不了多给，而多给正是这里唯一危险的方向。
"""

import pytest

from xiaowei_agent.contracts import ChannelPermission, IdentitySource
from xiaowei_agent.contracts.enums import AdminCapability, ProductRole, UserStatus
from xiaowei_agent.governance.product_roles import (
    admin_capabilities_for,
    channel_permissions_for,
)

_VIEW = ChannelPermission.VIEW_SAFE_TASK
_SUBMIT = ChannelPermission.SUBMIT_READONLY_TASK
_ADMIN_ALL = ChannelPermission.ADMIN_ALL_SAFE_TASKS


def test_channel_permission_enum_is_still_exactly_three_members() -> None:
    assert frozenset(ChannelPermission) == frozenset({_VIEW, _SUBMIT, _ADMIN_ALL})


def test_admin_capability_enum_is_exactly_the_seven_adr_013_members() -> None:
    assert {member.value for member in AdminCapability} == {
        "manage_users",
        "manage_duty_bindings",
        "view_admin_audit",
        "view_private_task_content",
        "view_integration_status",
        "manage_integrations",
        "run_connection_tests",
    }


def test_product_role_and_user_status_are_closed() -> None:
    assert {member.value for member in ProductRole} == {"admin", "operator", "user"}
    assert {member.value for member in UserStatus} == {"active", "disabled"}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (ProductRole.ADMIN, frozenset({_VIEW, _SUBMIT, _ADMIN_ALL})),
        (ProductRole.OPERATOR, frozenset({_VIEW, _SUBMIT})),
        (ProductRole.USER, frozenset({_VIEW})),
    ],
)
def test_channel_permissions_match_the_spec_table_exactly(
    role: ProductRole, expected: frozenset[ChannelPermission]
) -> None:
    assert channel_permissions_for(role) == expected


def test_local_admin_administrator_gets_every_admin_capability() -> None:
    assert admin_capabilities_for(
        ProductRole.ADMIN, source=IdentitySource.LOCAL_ADMIN
    ) == frozenset(AdminCapability)


def test_feishu_administrator_is_short_exactly_the_two_config_plane_capabilities() -> None:
    """判别性用例：只有它能区分"按角色发"和"按角色+来源发"。

    飞书 ADMIN 可以管用户、职责、审计、私聊正文和脱敏状态；不能改配置、
    不能跑连接测试。少一项、多一项都是错的，所以这里断言差集本身。
    """
    granted = admin_capabilities_for(ProductRole.ADMIN, source=IdentitySource.FEISHU)
    assert frozenset(AdminCapability) - granted == frozenset(
        {AdminCapability.MANAGE_INTEGRATIONS, AdminCapability.RUN_CONNECTION_TESTS}
    )


@pytest.mark.parametrize("role", [ProductRole.OPERATOR, ProductRole.USER])
@pytest.mark.parametrize("source", list(IdentitySource))
def test_non_administrators_get_no_admin_capability_from_any_source(
    role: ProductRole, source: IdentitySource
) -> None:
    assert admin_capabilities_for(role, source=source) == frozenset()


def test_mapping_is_total_over_the_role_enum() -> None:
    """新增角色而忘了给映射，必须炸在这里而不是运行期 KeyError。"""
    for role in ProductRole:
        assert isinstance(channel_permissions_for(role), frozenset)
```

- [ ] **Step 2: 运行，确认 RED 指向"尚未实现"**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/unit/test_product_roles.py -q 2>&1 | tail -20
```

预期：`ModuleNotFoundError: No module named 'xiaowei_agent.governance.product_roles'`，以及 `ImportError: cannot import name 'AdminCapability'`。这两个都是合格 RED（被测事实尚不存在）。若报的是 fixture 名拼错、参数不符这类**测试自身的错**，先修测试再重新取 RED。

- [ ] **Step 3: 加三个枚举**

在 `src/xiaowei_agent/contracts/enums.py` 中 `IdentitySource` 之后插入：

```python
class ProductRole(StrEnum):
    """Web 产品线的三角色闭集；回答"能做什么"，不回答"从哪来"。"""

    ADMIN = "admin"
    OPERATOR = "operator"
    USER = "user"


class UserStatus(StrEnum):
    """用户账号状态；``DISABLED`` 在后续请求上实时 fail-closed。"""

    ACTIVE = "active"
    DISABLED = "disabled"


class AdminCapability(StrEnum):
    """管理面能力闭集（ADR-013 2026-09-20 修订）。

    **与 ``ChannelPermission`` 是两个枚举，不是一个。** 任务面权限闭集一旦
    被管理面需求撑大，渠道投影现有的全部断言就同时失去意义——那些断言的
    前提正是"这个集合只有三个成员"。
    """

    MANAGE_USERS = "manage_users"
    MANAGE_DUTY_BINDINGS = "manage_duty_bindings"
    VIEW_ADMIN_AUDIT = "view_admin_audit"
    VIEW_PRIVATE_TASK_CONTENT = "view_private_task_content"
    VIEW_INTEGRATION_STATUS = "view_integration_status"
    MANAGE_INTEGRATIONS = "manage_integrations"
    RUN_CONNECTION_TESTS = "run_connection_tests"
```

- [ ] **Step 4: 写映射实现**

创建 `src/xiaowei_agent/governance/product_roles.py`：

```python
"""产品角色、认证来源到既有权限与管理能力的确定性映射。

放在 ``governance/`` 而不是 ``interfaces/``：这是授权判定，每个请求都要
重新算一次。放进入口层就会出现第二份判定，而两份判定迟早分叉。
"""

from typing import Final

from xiaowei_agent.contracts import ChannelPermission, IdentitySource
from xiaowei_agent.contracts.enums import AdminCapability, ProductRole

_ROLE_CHANNEL_PERMISSIONS: Final[dict[ProductRole, frozenset[ChannelPermission]]] = {
    ProductRole.ADMIN: frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
            ChannelPermission.ADMIN_ALL_SAFE_TASKS,
        }
    ),
    ProductRole.OPERATOR: frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
        }
    ),
    ProductRole.USER: frozenset({ChannelPermission.VIEW_SAFE_TASK}),
}

_LOCAL_ADMIN_ONLY: Final[frozenset[AdminCapability]] = frozenset(
    {
        AdminCapability.MANAGE_INTEGRATIONS,
        AdminCapability.RUN_CONNECTION_TESTS,
    }
)
"""只有 ``LOCAL_ADMIN`` principal 能实际使用的两项。

飞书会话即使属于 ADMIN 也拿不到它们，且系统不签发"飞书会话临时变成本地
会话"的混合凭证——要改配置或跑测试，必须真的完成本地 Admin 登录。
"""


def channel_permissions_for(role: ProductRole) -> frozenset[ChannelPermission]:
    """返回该角色在任务面的渠道权限闭集。"""
    return _ROLE_CHANNEL_PERMISSIONS[role]


def admin_capabilities_for(
    role: ProductRole, *, source: IdentitySource
) -> frozenset[AdminCapability]:
    """返回该角色在该认证来源下实际可用的管理能力。

    非 ADMIN 一律空集：``OPERATOR``/``USER`` 没有任何管理面能力，这一条
    先于来源判断，避免将来加来源时漏掉它们。
    """
    if role is not ProductRole.ADMIN:
        return frozenset()
    if source is IdentitySource.LOCAL_ADMIN:
        return frozenset(AdminCapability)
    return frozenset(AdminCapability) - _LOCAL_ADMIN_ONLY


__all__ = ["admin_capabilities_for", "channel_permissions_for"]
```

- [ ] **Step 5: 运行，确认全绿**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/unit/test_product_roles.py -q 2>&1 | tail -5
```

- [ ] **Step 6: 隔离变异反证**

证明"来源约束"这条承重保护真的承重：

```bash
cp src/xiaowei_agent/governance/product_roles.py /tmp/keep-roles.py
python - <<'MUT'
from pathlib import Path
p = Path("src/xiaowei_agent/governance/product_roles.py")
t = p.read_text()
t = t.replace(
    "    if source is IdentitySource.LOCAL_ADMIN:\n"
    "        return frozenset(AdminCapability)\n"
    "    return frozenset(AdminCapability) - _LOCAL_ADMIN_ONLY",
    "    return frozenset(AdminCapability)",
)
p.write_text(t)
MUT
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/unit/test_product_roles.py -q 2>&1 | tail -5
cp /tmp/keep-roles.py src/xiaowei_agent/governance/product_roles.py && rm /tmp/keep-roles.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/unit/test_product_roles.py -q 2>&1 | tail -3
```

预期：变异后 `test_feishu_administrator_is_short_exactly_the_two_config_plane_capabilities` 变红；还原后回到全绿。**先确认还原成功再进下一步。**

- [ ] **Step 7: 提交**

```bash
git add src/xiaowei_agent/contracts/enums.py \
        src/xiaowei_agent/governance/product_roles.py \
        tests/unit/test_product_roles.py
git commit -m "feat(w1a): map product roles and auth sources to closed permission sets

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: 身份目录契约与写命令闭集

**Files:**
- Create: `src/xiaowei_agent/contracts/identity.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Test: `tests/contract/test_identity_contracts.py`

**Interfaces:**
- Consumes: Task 1 的 `ProductRole`、`UserStatus`；既有 `Contract`、`StrictStr`、`SecretHash`、`AwareDatetime`、`IdentitySource`
- Produces: `ControlledPii`、`UserAccount`、`UserRoleAssignment`、`ExternalIdentity`、`DirectoryPrincipalFacts`、八个命令类型与 `DirectoryCommand` 联合

**为什么命令闭集包含 bootstrap 与批量迁移：** 授权表只有一条写路径这件事，只有在"所有授权写入都是一个 `DirectoryCommand`"时才成立。本地 Admin bootstrap 建的是第一个 ADMIN 角色，旧身份迁移建的是一整批账号与角色——它们都是授权改变。把它们留在 `LocalAdminStore` 或一个循环里，就等于开了第二、第三个写入口。

- [ ] **Step 1: 写失败测试**

创建 `tests/contract/test_identity_contracts.py`：

```python
"""身份目录 DTO 的不可变性、字段闭集与 PII 遮蔽。"""

import datetime as _dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import IdentitySource
from xiaowei_agent.contracts.enums import ProductRole, UserStatus
from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    CreateUserCommand,
    ExternalIdentity,
    LegacyIdentityMigrationEntry,
    MigrateLegacyIdentitiesCommand,
    RevokeRoleCommand,
    SetUserStatusCommand,
    UnbindExternalIdentityCommand,
    UserAccount,
    UserRoleAssignment,
)

_NOW = _dt.datetime(2026, 9, 21, 3, 0, tzinfo=_dt.UTC)
_FAKE_OPEN_ID = "ou_" + "7f3c9a21b4e8"
_FAKE_HASH = "argon2id$" + "v=19$m=65536,t=3,p=4$c29tZXNhbHQ"

_ALL_COMMANDS = (
    CreateUserCommand,
    SetUserStatusCommand,
    AssignRoleCommand,
    RevokeRoleCommand,
    BindExternalIdentityCommand,
    UnbindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    MigrateLegacyIdentitiesCommand,
)


def _account() -> UserAccount:
    return UserAccount(
        user_id="usr-1",
        actor="alice",
        display_name="Alice",
        status=UserStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_user_account_fields_are_exactly_the_spec_set() -> None:
    assert set(UserAccount.model_fields) == {
        "user_id",
        "actor",
        "display_name",
        "status",
        "created_at",
        "updated_at",
    }


def test_user_account_is_frozen_and_rejects_unknown_fields() -> None:
    account = _account()
    with pytest.raises(ValidationError):
        account.user_id = "usr-2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        UserAccount(**account.model_dump(mode="python"), tenant_id="dev-local")


def test_role_assignment_carries_the_scope_not_the_account() -> None:
    """作用域在角色上，不在账号上——见计划「与规格的两处有意偏离」第 1 条。"""
    assert set(UserRoleAssignment.model_fields) == {
        "user_id",
        "tenant_id",
        "environment_id",
        "role",
        "created_by",
        "created_at",
        "updated_at",
    }


def test_external_identity_never_leaks_the_subject_ref() -> None:
    """``open_id`` 是受控 PII。

    ``repr`` 会进 traceback，``model_dump`` 会进日志和响应体。两条都必须
    堵死，只堵一条等于没堵。
    """
    identity = ExternalIdentity(
        user_id="usr-1",
        provider=IdentitySource.FEISHU,
        tenant_id="dev-local",
        environment_id="dev",
        subject_ref=_FAKE_OPEN_ID,
        created_at=_NOW,
        last_seen_at=_NOW,
    )
    assert identity.subject_ref == _FAKE_OPEN_ID
    assert _FAKE_OPEN_ID not in repr(identity)
    assert "subject_ref" not in identity.model_dump(mode="python")
    assert _FAKE_OPEN_ID not in identity.model_dump_json()


def test_external_identity_provider_is_restricted_to_feishu() -> None:
    with pytest.raises(ValidationError):
        ExternalIdentity(
            user_id="usr-1",
            provider=IdentitySource.LOCAL_ADMIN,
            tenant_id="dev-local",
            environment_id="dev",
            subject_ref=_FAKE_OPEN_ID,
            created_at=_NOW,
            last_seen_at=_NOW,
        )


def test_commands_cannot_stamp_their_own_time_or_identity() -> None:
    """时间由存储层盖，命令自带就等于可以伪造历史。"""
    assert "created_at" not in CreateUserCommand.model_fields
    assert "updated_at" not in AssignRoleCommand.model_fields
    with pytest.raises(ValidationError):
        AssignRoleCommand(
            user_id="usr-1",
            tenant_id="dev-local",
            environment_id="dev",
            role=ProductRole.ADMIN,
            created_at=_NOW,
        )


def test_no_command_can_carry_audit_fields() -> None:
    """审计由 store 从命令派生。

    命令上只要出现 ``action``/``outcome``/``target_kind``，调用方就又拿回了
    "这次改动记成什么"的话语权——那正是本设计要消除的东西。
    """
    audit_fields = {
        "action",
        "outcome",
        "target_kind",
        "target_ref_digest",
        "reason_code",
        "event_id",
    }
    for command in _ALL_COMMANDS:
        assert audit_fields.isdisjoint(set(command.model_fields)), command.__name__


def test_every_command_carries_the_scope_the_store_derives_audit_from() -> None:
    """派生审计需要作用域，因此每个命令都必须自带 tenant/environment。

    漏一个，store 就只能从别处取作用域——而"别处"最可能是调用方传进来的
    对象，那正是本轮要根除的路径。
    """
    for command in _ALL_COMMANDS:
        assert {"tenant_id", "environment_id", "kind"} <= set(command.model_fields)


def test_command_kind_discriminators_are_unique() -> None:
    kinds = [command.model_fields["kind"].default for command in _ALL_COMMANDS]
    assert len(kinds) == len(set(kinds)) == 8


def test_bootstrap_command_never_exposes_the_password_hash() -> None:
    command = BootstrapLocalAdminCommand(
        user_id="usr-local-admin",
        actor="admin",
        display_name="Local Admin",
        tenant_id="dev-local",
        environment_id="dev",
        password_hash=_FAKE_HASH,
    )
    assert _FAKE_HASH not in repr(command)
    assert "password_hash" not in command.model_dump(mode="python")
    assert _FAKE_HASH not in command.model_dump_json()


def test_migration_command_carries_the_whole_batch_and_hides_open_ids() -> None:
    """整批是一个命令——原子性只能由事务给。"""
    command = MigrateLegacyIdentitiesCommand(
        tenant_id="dev-local",
        environment_id="dev",
        entries=(
            LegacyIdentityMigrationEntry(
                user_id="usr-legacy-" + "a" * 32,
                actor="alice",
                display_name="alice",
                role=ProductRole.OPERATOR,
                subject_ref=_FAKE_OPEN_ID,
            ),
        ),
    )
    assert len(command.entries) == 1
    assert _FAKE_OPEN_ID not in command.model_dump_json()


def test_migration_command_rejects_an_empty_batch() -> None:
    with pytest.raises(ValidationError):
        MigrateLegacyIdentitiesCommand(
            tenant_id="dev-local", environment_id="dev", entries=()
        )


def test_identifier_fields_are_bounded() -> None:
    """外部输入派生的 ID 必须有界，否则超长 actor 会在写库时才炸。"""
    with pytest.raises(ValidationError):
        CreateUserCommand(
            user_id="u" * 65,
            actor="alice",
            display_name="Alice",
            tenant_id="dev-local",
            environment_id="dev",
            role=ProductRole.USER,
        )
    with pytest.raises(ValidationError):
        CreateUserCommand(
            user_id="usr-1",
            actor="a" * 65,
            display_name="Alice",
            tenant_id="dev-local",
            environment_id="dev",
            role=ProductRole.USER,
        )


def test_bind_command_requires_a_non_empty_subject_ref() -> None:
    with pytest.raises(ValidationError):
        BindExternalIdentityCommand(
            user_id="usr-1",
            tenant_id="dev-local",
            environment_id="dev",
            subject_ref="",
        )
```

- [ ] **Step 2: 运行，确认 RED 指向缺模块**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_contracts.py -q 2>&1 | tail -10
```

预期：`ModuleNotFoundError: No module named 'xiaowei_agent.contracts.identity'`。

- [ ] **Step 3: 写契约模块**

创建 `src/xiaowei_agent/contracts/identity.py`：

```python
"""持久身份目录的契约：账号、作用域角色、外部身份与写命令闭集。"""

from typing import Annotated, Literal

from pydantic import Field

from xiaowei_agent.contracts.base import (
    AwareDatetime,
    Contract,
    SecretHash,
    StrictStr,
)
from xiaowei_agent.contracts.enums import IdentitySource, ProductRole, UserStatus

ControlledPii = StrictStr
"""受控 PII 的标记类型。

与 ``SecretHash`` 同一套约定：凡这样标注的字段必须同时写 ``exclude=True``
与 ``repr=False``，由 ``tests/security/test_controlled_pii_exposure.py`` 机械
检查。飞书 ``open_id`` 不是 secret，但它足以把一条运维记录钉到具体的人，
因此不进普通日志、trace、异常和审计正文。
"""

_ID = Field(min_length=1, max_length=64)
_NAME = Field(min_length=1, max_length=128)
_PII = Field(min_length=1, max_length=128, exclude=True, repr=False)


class UserAccount(Contract):
    """身份目录里的账号事实；作用域在角色上，不在这里。"""

    user_id: StrictStr = _ID
    actor: StrictStr = _ID
    display_name: StrictStr = _NAME
    status: UserStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class UserRoleAssignment(Contract):
    """某个 tenant/environment 内的角色授予。"""

    user_id: StrictStr = _ID
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID
    role: ProductRole
    created_by: StrictStr = _ID
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ExternalIdentity(Contract):
    """外部 provider 主体到内部账号的绑定。"""

    user_id: StrictStr = _ID
    provider: Literal[IdentitySource.FEISHU]
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID
    subject_ref: ControlledPii = _PII
    created_at: AwareDatetime
    last_seen_at: AwareDatetime


class DirectoryPrincipalFacts(Contract):
    """构造一个 principal 所需的全部目录事实。"""

    account: UserAccount
    assignment: UserRoleAssignment


class CreateUserCommand(Contract):
    """建号并在同一作用域授予初始角色。"""

    kind: Literal["create_user"] = "create_user"
    user_id: StrictStr = _ID
    actor: StrictStr = _ID
    display_name: StrictStr = _NAME
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID
    role: ProductRole


class SetUserStatusCommand(Contract):
    kind: Literal["set_user_status"] = "set_user_status"
    user_id: StrictStr = _ID
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID
    status: UserStatus


class AssignRoleCommand(Contract):
    kind: Literal["assign_role"] = "assign_role"
    user_id: StrictStr = _ID
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID
    role: ProductRole


class RevokeRoleCommand(Contract):
    kind: Literal["revoke_role"] = "revoke_role"
    user_id: StrictStr = _ID
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID


class BindExternalIdentityCommand(Contract):
    kind: Literal["bind_external_identity"] = "bind_external_identity"
    user_id: StrictStr = _ID
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID
    subject_ref: ControlledPii = _PII


class UnbindExternalIdentityCommand(Contract):
    kind: Literal["unbind_external_identity"] = "unbind_external_identity"
    user_id: StrictStr = _ID
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID


class BootstrapLocalAdminCommand(Contract):
    """首次启动时建立本地 Admin 账号、ADMIN 角色与本地凭据。

    **它必须是一个 DirectoryCommand，不能留在 ``LocalAdminStore`` 里。**
    它写的是授权事实——第一个 ADMIN 角色——而授权事实只有一条写路径；
    留在凭据存储里就等于开了第二个写入口，"任何授权改变都带审计"随即失效。

    幂等：``local_admins`` 已有行时整条命令是 no-op，返回零个审计事件。
    什么都没改，就不该有审计。
    """

    kind: Literal["bootstrap_local_admin"] = "bootstrap_local_admin"
    user_id: StrictStr = _ID
    actor: StrictStr = _ID
    display_name: StrictStr = _NAME
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID
    password_hash: SecretHash = Field(exclude=True, repr=False)


class LegacyIdentityMigrationEntry(Contract):
    """批量迁移里的一个条目；本身不是命令。"""

    user_id: StrictStr = _ID
    actor: StrictStr = _ID
    display_name: StrictStr = _NAME
    role: ProductRole
    subject_ref: ControlledPii = _PII


class MigrateLegacyIdentitiesCommand(Contract):
    """把整份旧静态身份文件一次性迁入目录。

    **整批是一个命令、一个事务。** 逐条 apply 的写法有一个无法回避的后果：
    第 3 条失败时前 2 条已经提交，库里留下半批身份；重跑会把残缺账号当成
    已迁移跳过，那半批就永远补不上。原子性只能由事务给——事务外预检替代
    不了它，而且预检和写入之间还有一个时间窗。
    """

    kind: Literal["migrate_legacy_identities"] = "migrate_legacy_identities"
    tenant_id: StrictStr = _ID
    environment_id: StrictStr = _ID
    entries: tuple[LegacyIdentityMigrationEntry, ...] = Field(
        min_length=1, max_length=10_000
    )


DirectoryCommand = Annotated[
    CreateUserCommand
    | SetUserStatusCommand
    | AssignRoleCommand
    | RevokeRoleCommand
    | BindExternalIdentityCommand
    | UnbindExternalIdentityCommand
    | BootstrapLocalAdminCommand
    | MigrateLegacyIdentitiesCommand,
    Field(discriminator="kind"),
]
"""写命令闭集 —— 也是四张授权表的**全部**写入形态。

新增成员必须同时在 ``ACTION_FOR_COMMAND`` 与 ``EFFECT_FOR_COMMAND`` 里给出
条目，否则 ``tests/contract/test_identity_store.py`` 的两条完备性用例会变红。
"""


__all__ = [
    "AssignRoleCommand",
    "BindExternalIdentityCommand",
    "BootstrapLocalAdminCommand",
    "ControlledPii",
    "CreateUserCommand",
    "DirectoryCommand",
    "DirectoryPrincipalFacts",
    "ExternalIdentity",
    "LegacyIdentityMigrationEntry",
    "MigrateLegacyIdentitiesCommand",
    "RevokeRoleCommand",
    "SetUserStatusCommand",
    "UnbindExternalIdentityCommand",
    "UserAccount",
    "UserRoleAssignment",
]
```

- [ ] **Step 4: 在 `contracts/__init__.py` 导出**

按该文件现有的字母序惯例，把上面 `__all__` 里的名字加入 import 与 `__all__`。

- [ ] **Step 5: 运行，确认全绿**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_contracts.py -q 2>&1 | tail -5
ruff check src/xiaowei_agent/contracts/identity.py
mypy src
```

- [ ] **Step 6: 提交**

```bash
git add src/xiaowei_agent/contracts/identity.py \
        src/xiaowei_agent/contracts/__init__.py \
        tests/contract/test_identity_contracts.py
git commit -m "feat(w1a): add identity directory contracts and the write command closed set

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Admin 审计事件契约、操作上下文与闭集 effect

**Files:**
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Create: `src/xiaowei_agent/contracts/admin_audit.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Test: `tests/contract/test_admin_audit_contracts.py`

**Interfaces:**
- Consumes: `IdentitySource`、`ProductRole`、`UserStatus`、`Contract`、`Sha256Hex`
- Produces: `AdminAuditAction`、`AdminAuditTargetKind`、`AdminAuditOutcome`、`AdminAuditReasonCode`、`admin_audit_target_digest()`、`AdminOperationContext`、`AdminAuditEffect`、`AdminAuditCandidate`、`AdminAuditEvent`

**这个任务的两个设计要点：**

1. **`AdminOperationContext` 是调用方唯一能提供的东西。** 它只含 operation 与操作者身份，没有 action、target、outcome。`UserDirectoryStore` 拿它加上命令去派生完整事件。`AdminAuditCandidate` 保留，但只服务于**什么都没改**的路径（拒绝、配置面 `STARTED`），那里没有命令可派生。
2. **effect 是闭集两列，不是自由文本。** 没有它，`role_assigned` 事件只能回答"谁在什么时候改了谁"，回答不了"改成了什么"——而后者正是授权审计存在的理由。

- [ ] **Step 1: 写失败测试**

创建 `tests/contract/test_admin_audit_contracts.py`：

```python
"""Admin 审计事件的字段闭集、摘要域隔离、effect 与 outcome 语义。"""

import datetime as _dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import IdentitySource
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditEffect,
    AdminAuditEvent,
    AdminOperationContext,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    ProductRole,
    UserStatus,
)

_NOW = _dt.datetime(2026, 9, 21, 3, 0, tzinfo=_dt.UTC)


def _candidate(**updates: object) -> AdminAuditCandidate:
    values: dict[str, object] = {
        "operation_id": "op-1",
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "actor_user_id": "usr-admin",
        "actor": "admin",
        "auth_source": IdentitySource.LOCAL_ADMIN,
        "action": AdminAuditAction.ROLE_ASSIGNED,
        "target_kind": AdminAuditTargetKind.USER,
        "target_ref_digest": admin_audit_target_digest(
            target_kind=AdminAuditTargetKind.USER, target_ref="usr-1"
        ),
        "outcome": AdminAuditOutcome.DENIED,
        "reason_code": AdminAuditReasonCode.ACTOR_NOT_ADMIN,
        "effect": AdminAuditEffect(),
    }
    return AdminAuditCandidate(**(values | updates))


def test_operation_context_carries_no_audit_decision() -> None:
    """调用方能提供的全部东西。

    这条断言是整个设计的锚点：只要 ``action``/``outcome``/``target`` 之一
    出现在这里，调用方就又拿回了"这次改动记成什么"的话语权。
    """
    assert set(AdminOperationContext.model_fields) == {
        "operation_id",
        "actor_user_id",
        "actor",
        "auth_source",
    }


def test_operation_id_leaves_room_for_batch_suffixes() -> None:
    """批量命令按 ``{operation_id}:{index}`` 派生子 operation id。

    上限留到 48 而不是 64，是为了给 ``:9999`` 这样的后缀留位置；否则
    一万条的迁移会在最后几条上突然超长。
    """
    AdminOperationContext(
        operation_id="o" * 48,
        actor_user_id="usr-admin",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
    )
    with pytest.raises(ValidationError):
        AdminOperationContext(
            operation_id="o" * 49,
            actor_user_id="usr-admin",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
        )


def test_audit_candidate_fields_are_the_spec_set_plus_the_closed_effect() -> None:
    """规格 §14.2 的字段表逐条，外加闭集 effect。

    §14.2 写的是"事件**至少**包含"，因此 effect 是它允许的扩展。这条断言
    存在的理由是挡住**别的**新增字段：任何 ``message``、``detail``、
    ``payload`` 或 ``dict`` 都会立刻把审计表变成正文/Secret 的旁路。
    """
    assert set(AdminAuditCandidate.model_fields) == {
        "operation_id",
        "tenant_id",
        "environment_id",
        "actor_user_id",
        "actor",
        "auth_source",
        "action",
        "target_kind",
        "target_ref_digest",
        "outcome",
        "reason_code",
        "effect",
    }


def test_effect_has_only_two_closed_enum_slots() -> None:
    assert set(AdminAuditEffect.model_fields) == {"role", "status"}
    annotations = {
        name: field.annotation for name, field in AdminAuditEffect.model_fields.items()
    }
    assert annotations["role"] == ProductRole | None
    assert annotations["status"] == UserStatus | None


def test_stored_event_adds_only_identity_and_time() -> None:
    assert set(AdminAuditEvent.model_fields) - set(AdminAuditCandidate.model_fields) == {
        "event_id",
        "created_at",
    }


def test_target_kind_is_the_spec_five_member_closed_set() -> None:
    assert {member.value for member in AdminAuditTargetKind} == {
        "user",
        "activation",
        "duty_binding",
        "config",
        "task_content",
    }


def test_outcome_is_the_spec_four_member_closed_set() -> None:
    assert {member.value for member in AdminAuditOutcome} == {
        "started",
        "succeeded",
        "denied",
        "failed",
    }


def test_w1a_actions_cover_only_what_this_stage_actually_writes() -> None:
    """W1b/W3 会追加成员；本阶段不预留没有消费者的动作。"""
    assert {member.value for member in AdminAuditAction} == {
        "user_created",
        "user_status_changed",
        "role_assigned",
        "role_revoked",
        "external_identity_bound",
        "external_identity_unbound",
        "local_admin_bootstrapped",
        "legacy_identity_migrated",
    }


def test_authentication_lifecycle_never_became_an_audit_action() -> None:
    """登录成功/失败、改密、登出走结构化安全日志，不进这张表（规格 §14.2）。

    它们是认证生命周期事件，不是 Admin 对业务对象的操作。混进来有两个后果：
    审计表变成高频写入的登录日志，而"这张表每一行都是一次管理动作"这个
    前提——W3 的审计界面正按它设计——就不再成立。
    """
    forbidden = ("login", "logout", "password", "session", "signin", "sign_in")
    offenders = [
        member.value
        for member in AdminAuditAction
        if any(word in member.value for word in forbidden)
    ]
    assert offenders == [], f"authentication events must not be admin audit: {offenders}"


def test_target_digest_is_domain_separated_across_kinds() -> None:
    """同一个 ref 在两类 target 下必须得到不同摘要。

    不隔离的话，一次"查看任务正文"的审计摘要会与一次"改用户"的摘要相等，
    审计查询里两条事件就会互相冒充。
    """
    ref = "shared-ref"
    assert admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.USER, target_ref=ref
    ) != admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.TASK_CONTENT, target_ref=ref
    )


def test_target_digest_never_contains_the_plaintext_ref() -> None:
    ref = "ou_" + "7f3c9a21b4e8"
    digest = admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.USER, target_ref=ref
    )
    assert ref not in digest
    assert len(digest) == 64


def test_target_ref_digest_field_rejects_a_plaintext_reference() -> None:
    with pytest.raises(ValidationError):
        _candidate(target_ref_digest="usr-1")


def test_reason_code_is_absent_on_success_and_required_on_denial() -> None:
    assert _candidate().reason_code is AdminAuditReasonCode.ACTOR_NOT_ADMIN
    with pytest.raises(ValidationError):
        _candidate(outcome=AdminAuditOutcome.DENIED, reason_code=None)
    with pytest.raises(ValidationError):
        _candidate(
            outcome=AdminAuditOutcome.SUCCEEDED,
            reason_code=AdminAuditReasonCode.ACTOR_NOT_ADMIN,
        )


def test_a_negative_outcome_cannot_claim_an_effect() -> None:
    """拒绝或失败的操作没有改变任何东西，因此不能声称授予了角色。

    没有这条，一次 DENIED 事件可以写着 ``effect.role = ADMIN``，审计读者
    会以为权限真的给出去了。
    """
    with pytest.raises(ValidationError):
        _candidate(effect=AdminAuditEffect(role=ProductRole.ADMIN))
    with pytest.raises(ValidationError):
        _candidate(
            outcome=AdminAuditOutcome.FAILED,
            reason_code=AdminAuditReasonCode.CONFLICT,
            effect=AdminAuditEffect(status=UserStatus.DISABLED),
        )


def test_started_outcome_cannot_claim_an_effect_either() -> None:
    """只看到 STARTED 表示结果未知，不能伪装成功（规格 §14.2）。"""
    with pytest.raises(ValidationError):
        _candidate(
            outcome=AdminAuditOutcome.STARTED,
            reason_code=None,
            effect=AdminAuditEffect(role=ProductRole.ADMIN),
        )


def test_role_changing_actions_require_a_role_effect() -> None:
    """``role_assigned`` 必须回答"改成了什么角色"。"""
    with pytest.raises(ValidationError):
        _candidate(
            action=AdminAuditAction.ROLE_ASSIGNED,
            outcome=AdminAuditOutcome.SUCCEEDED,
            reason_code=None,
            effect=AdminAuditEffect(),
        )
    ok = _candidate(
        action=AdminAuditAction.ROLE_ASSIGNED,
        outcome=AdminAuditOutcome.SUCCEEDED,
        reason_code=None,
        effect=AdminAuditEffect(role=ProductRole.ADMIN),
    )
    assert ok.effect.role is ProductRole.ADMIN


def test_status_changing_actions_require_a_status_effect() -> None:
    with pytest.raises(ValidationError):
        _candidate(
            action=AdminAuditAction.USER_STATUS_CHANGED,
            outcome=AdminAuditOutcome.SUCCEEDED,
            reason_code=None,
            effect=AdminAuditEffect(),
        )


def test_binding_actions_carry_no_effect() -> None:
    """绑定/解绑不改角色也不改状态，声称 effect 就是在编造事实。"""
    with pytest.raises(ValidationError):
        _candidate(
            action=AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
            outcome=AdminAuditOutcome.SUCCEEDED,
            reason_code=None,
            effect=AdminAuditEffect(role=ProductRole.USER),
        )


def test_candidate_cannot_stamp_event_id_or_time() -> None:
    with pytest.raises(ValidationError):
        _candidate(event_id="evt-1")
    with pytest.raises(ValidationError):
        _candidate(created_at=_NOW)
```

- [ ] **Step 2: 运行，确认 RED 指向缺模块**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_admin_audit_contracts.py -q 2>&1 | tail -10
```

- [ ] **Step 3: 加四个审计枚举**

在 `src/xiaowei_agent/contracts/enums.py` 中 `AdminCapability` 之后追加：

```python
class AdminAuditAction(StrEnum):
    """W1a 实际写入的管理面动作闭集。

    W1b 追加激活审批动作、W3 追加敏感查看与配置动作；**追加而不是重定义**，
    历史事件的 action 值永远不改写。
    """

    USER_CREATED = "user_created"
    USER_STATUS_CHANGED = "user_status_changed"
    ROLE_ASSIGNED = "role_assigned"
    ROLE_REVOKED = "role_revoked"
    EXTERNAL_IDENTITY_BOUND = "external_identity_bound"
    EXTERNAL_IDENTITY_UNBOUND = "external_identity_unbound"
    LOCAL_ADMIN_BOOTSTRAPPED = "local_admin_bootstrapped"
    LEGACY_IDENTITY_MIGRATED = "legacy_identity_migrated"


class AdminAuditTargetKind(StrEnum):
    """规格 §14.2 的 target 闭集；五个成员在 W1a 一次冻结。"""

    USER = "user"
    ACTIVATION = "activation"
    DUTY_BINDING = "duty_binding"
    CONFIG = "config"
    TASK_CONTENT = "task_content"


class AdminAuditOutcome(StrEnum):
    """``STARTED`` 单独存在是为了非事务性动作。

    文件配置与数据库审计无法同事务，因此配置面先写 ``STARTED``、完成后再
    写 ``SUCCEEDED``/``FAILED``；只看到 ``STARTED`` 表示结果未知，不能当成功读。
    """

    STARTED = "started"
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"


class AdminAuditReasonCode(StrEnum):
    """拒绝/失败原因的闭集；**不是异常正文的容器**。"""

    ACTOR_NOT_ADMIN = "actor_not_admin"
    AUTH_SOURCE_NOT_ALLOWED = "auth_source_not_allowed"
    TARGET_NOT_FOUND = "target_not_found"
    SCOPE_MISMATCH = "scope_mismatch"
    CONFLICT = "conflict"
    AUDIT_UNWRITABLE = "audit_unwritable"
```

- [ ] **Step 4: 写审计契约模块**

创建 `src/xiaowei_agent/contracts/admin_audit.py`：

```python
"""Admin 审计事件契约、操作上下文与 target 摘要。"""

from hashlib import sha256
from typing import Final, Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import AwareDatetime, Contract, Sha256Hex, StrictStr
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
    UserStatus,
)

_TARGET_DIGEST_DOMAIN: Final[str] = "admin-audit-target:v1"

_NEGATIVE_OUTCOMES: Final[frozenset[AdminAuditOutcome]] = frozenset(
    {AdminAuditOutcome.DENIED, AdminAuditOutcome.FAILED}
)

ROLE_EFFECT_ACTIONS: Final[frozenset[AdminAuditAction]] = frozenset(
    {
        AdminAuditAction.USER_CREATED,
        AdminAuditAction.ROLE_ASSIGNED,
        AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED,
        AdminAuditAction.LEGACY_IDENTITY_MIGRATED,
    }
)
"""成功时必须记录"改成了哪个角色"的动作。

``role_revoked`` 不在其中：撤销后该作用域没有角色，记任何角色都是错的。
"""

STATUS_EFFECT_ACTIONS: Final[frozenset[AdminAuditAction]] = frozenset(
    {
        AdminAuditAction.USER_CREATED,
        AdminAuditAction.USER_STATUS_CHANGED,
        AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED,
        AdminAuditAction.LEGACY_IDENTITY_MIGRATED,
    }
)


def admin_audit_target_digest(
    *, target_kind: AdminAuditTargetKind, target_ref: str
) -> str:
    """target 引用的 domain-separated 摘要。

    ``target_kind`` 进域而不是进正文：两类 target 共用同一个 ref 字符串时
    （用户 id 与任务 id 撞号完全可能），不隔离就会算出相等的摘要，审计查询
    里两条不同性质的事件从此互相冒充。
    """
    payload = f"{_TARGET_DIGEST_DOMAIN}:{target_kind.value}:{target_ref}"
    return sha256(payload.encode()).hexdigest()


class AdminOperationContext(Contract):
    """调用方**唯一**能提供的审计输入：一次操作的身份与编号。

    没有 action、没有 target、没有 outcome。这些由
    ``UserDirectoryStore.apply()`` 从命令派生——调用方没有机会写错，
    因此也不需要一层"校验它有没有写错"的防线。
    """

    operation_id: StrictStr = Field(min_length=1, max_length=48)
    actor_user_id: StrictStr = Field(min_length=1, max_length=64)
    actor: StrictStr = Field(min_length=1, max_length=64)
    auth_source: IdentitySource


class AdminAuditEffect(Contract):
    """一次成功授权改变的结果；两个闭集槽位，没有第三个。

    存在的理由：``role_assigned`` 如果不记新角色，历史被后续改动覆盖后就
    再也无法还原当时授予了什么——而那正是授权审计存在的全部意义。
    用两个枚举列而不是一个 JSON：JSON 字段迟早会被塞进异常正文。
    """

    role: ProductRole | None = None
    status: UserStatus | None = None


class AdminAuditCandidate(Contract):
    """一条待持久化的审计事实；不含 event_id 与时间。

    **由 store 构造，不由业务调用方构造。** 唯一的例外是"什么都没改"的
    路径——拒绝（``DENIED``）与配置面的 ``STARTED``——那里没有命令可派生，
    由 ``AdminAuditStore.append()`` 直接写。

    这个契约**故意没有任何自由文本字段**。审计表是唯一一处既要长期保留、
    又天然贴着 Secret、聊天正文和异常堆栈的存储；只要留一个 ``str`` 通道，
    第一个赶工的调用方就会把 ``str(exc)`` 塞进去。
    """

    operation_id: StrictStr = Field(min_length=1, max_length=64)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    actor_user_id: StrictStr = Field(min_length=1, max_length=64)
    actor: StrictStr = Field(min_length=1, max_length=64)
    auth_source: IdentitySource
    action: AdminAuditAction
    target_kind: AdminAuditTargetKind
    target_ref_digest: Sha256Hex
    outcome: AdminAuditOutcome
    reason_code: AdminAuditReasonCode | None = None
    effect: AdminAuditEffect = AdminAuditEffect()

    @model_validator(mode="after")
    def _reason_code_matches_outcome(self) -> Self:
        if self.outcome in _NEGATIVE_OUTCOMES and self.reason_code is None:
            raise ValueError("denied and failed outcomes require a reason code")
        if self.outcome not in _NEGATIVE_OUTCOMES and self.reason_code is not None:
            raise ValueError("non-negative outcomes must not carry a reason code")
        return self

    @model_validator(mode="after")
    def _effect_matches_action_and_outcome(self) -> Self:
        has_role = self.effect.role is not None
        has_status = self.effect.status is not None
        if self.outcome is not AdminAuditOutcome.SUCCEEDED:
            # 没成功就什么都没改；声称 effect 会让审计读者以为权限已经给出去了。
            if has_role or has_status:
                raise ValueError("only a succeeded outcome may carry an effect")
            return self
        if has_role != (self.action in ROLE_EFFECT_ACTIONS):
            raise ValueError("role effect does not match the action")
        if has_status != (self.action in STATUS_EFFECT_ACTIONS):
            raise ValueError("status effect does not match the action")
        return self


class AdminAuditEvent(AdminAuditCandidate):
    """已持久化的审计事件；append-only，永不更新或删除。"""

    event_id: StrictStr = Field(min_length=1, max_length=64)
    created_at: AwareDatetime


__all__ = [
    "ROLE_EFFECT_ACTIONS",
    "STATUS_EFFECT_ACTIONS",
    "AdminAuditCandidate",
    "AdminAuditEffect",
    "AdminAuditEvent",
    "AdminOperationContext",
    "admin_audit_target_digest",
]
```

- [ ] **Step 5: 在 `contracts/__init__.py` 导出并运行**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_admin_audit_contracts.py -q 2>&1 | tail -5
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_contract_enum_references.py -q 2>&1 | tail -5
ruff check . && mypy src
```

两个文件都必须绿。若 `test_contract_enum_references.py` 因新增枚举而变红，按它的既有规则补登记，**不要**放宽它的断言。

- [ ] **Step 6: 提交**

```bash
git add src/xiaowei_agent/contracts/enums.py \
        src/xiaowei_agent/contracts/admin_audit.py \
        src/xiaowei_agent/contracts/__init__.py \
        tests/contract/test_admin_audit_contracts.py
git commit -m "feat(w1a): derive-only admin audit contracts with closed effects

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: schema 表与 rev_0014 迁移

**Files:**
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_directory_and_admin_audit.py`
- Modify: `tests/contract/test_schema_matches_migration.py`（只加新 revision 的发布哈希登记）
- Test: `tests/contract/test_identity_schema.py`

**Interfaces:**
- Consumes: Task 2/3 的枚举值（作为 CHECK 约束字面量）
- Produces: `USER_ACCOUNTS`、`USER_ROLE_ASSIGNMENTS`、`EXTERNAL_IDENTITIES`、`ADMIN_AUDIT_EVENTS` 四张表；`local_admins.user_id` 列

**`operation_id` 为什么不是全局 UNIQUE：** 规格 §14.2 要求非事务性配置操作写 `STARTED → SUCCEEDED/FAILED` 两条事件，它们**属于同一个 operation**。全局唯一会让第二条终态事件必然撞约束；换一个 operation id 又无法证明两条事件属于同一次操作。正确的表达是两条 partial unique index：同一 operation 下 `started` 至多一条、终态至多一条。目录回滚证明照样成立——重复终态事件仍然是真实冲突。

- [ ] **Step 1: 写失败测试**

创建 `tests/contract/test_identity_schema.py`：

```python
"""身份目录与审计表的结构不变量——离线，不需要数据库。"""

import sqlalchemy as sa

from xiaowei_agent.persistence.schema import (
    ADMIN_AUDIT_EVENTS,
    ALL_TABLES,
    EXTERNAL_IDENTITIES,
    LOCAL_ADMINS,
    USER_ACCOUNTS,
    USER_ROLE_ASSIGNMENTS,
)


def _check_names(table: sa.Table) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name is not None
    }


def _unique_column_sets(table: sa.Table) -> set[frozenset[str]]:
    sets: set[frozenset[str]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            sets.add(frozenset(column.name for column in constraint.columns))
    for index in table.indexes:
        if index.unique:
            sets.add(frozenset(column.name for column in index.columns))
    return sets


def _partial_unique_indexes(table: sa.Table) -> dict[str, str]:
    return {
        index.name: str(index.dialect_options["postgresql"]["where"])
        for index in table.indexes
        if index.unique and index.dialect_options["postgresql"].get("where") is not None
    }


def test_new_tables_are_registered_in_all_tables() -> None:
    names = {table.name for table in ALL_TABLES}
    assert {
        "user_accounts",
        "user_role_assignments",
        "external_identities",
        "admin_audit_events",
    } <= names


def test_actor_is_unique_so_two_accounts_cannot_claim_the_same_identity() -> None:
    assert frozenset({"actor"}) in _unique_column_sets(USER_ACCOUNTS)


def test_role_assignment_is_keyed_by_user_and_scope() -> None:
    assert {column.name for column in USER_ROLE_ASSIGNMENTS.primary_key} == {
        "user_id",
        "tenant_id",
        "environment_id",
    }


def test_one_subject_ref_binds_to_at_most_one_account_per_scope() -> None:
    assert {column.name for column in EXTERNAL_IDENTITIES.primary_key} == {
        "provider",
        "tenant_id",
        "environment_id",
        "subject_ref_digest",
    }


def test_external_identity_stores_a_digest_not_the_open_id() -> None:
    """``open_id`` 是受控 PII，数据库里只留摘要。"""
    columns = {column.name for column in EXTERNAL_IDENTITIES.columns}
    assert "subject_ref_digest" in columns
    assert "subject_ref" not in columns


def test_assignment_and_identity_reference_the_account_by_foreign_key() -> None:
    for table in (USER_ROLE_ASSIGNMENTS, EXTERNAL_IDENTITIES):
        targets = {key.column.table.name for key in table.foreign_key_constraints}
        assert "user_accounts" in targets


def test_local_credential_is_linked_to_a_directory_account() -> None:
    assert "user_id" in {column.name for column in LOCAL_ADMINS.columns}
    assert "user_accounts" in {
        key.column.table.name for key in LOCAL_ADMINS.foreign_key_constraints
    }


def test_operation_id_is_not_globally_unique() -> None:
    """全局唯一会堵死规格 §14.2 要求的 ``STARTED → 终态`` 两阶段审计。"""
    assert frozenset({"operation_id"}) not in _unique_column_sets(ADMIN_AUDIT_EVENTS)


def test_one_started_and_one_terminal_event_per_operation() -> None:
    partials = _partial_unique_indexes(ADMIN_AUDIT_EVENTS)
    assert set(partials) == {
        "uq_admin_audit_one_started_per_operation",
        "uq_admin_audit_one_terminal_per_operation",
    }
    assert "started" in partials["uq_admin_audit_one_started_per_operation"]
    terminal = partials["uq_admin_audit_one_terminal_per_operation"]
    assert all(word in terminal for word in ("succeeded", "denied", "failed"))


def test_audit_closed_sets_are_expressed_as_database_checks() -> None:
    """枚举闭集在数据库层独立再表达一次。

    应用层的 Pydantic 校验只保护走应用层的写入；直接 psql 进去插一条
    ``outcome='whatever'`` 的行，审计查询就会遇到它不认识的值。
    """
    names = _check_names(ADMIN_AUDIT_EVENTS)
    assert {
        "ck_admin_audit_events_action_closed",
        "ck_admin_audit_events_target_kind_closed",
        "ck_admin_audit_events_outcome_closed",
        "ck_admin_audit_events_reason_code_matches_outcome",
        "ck_admin_audit_events_effect_only_on_success",
        "ck_admin_audit_events_role_effect_required",
        "ck_admin_audit_events_status_effect_required",
    } <= names


def test_audit_table_has_no_free_text_column() -> None:
    assert {column.name for column in ADMIN_AUDIT_EVENTS.columns} == {
        "event_id",
        "operation_id",
        "tenant_id",
        "environment_id",
        "actor_user_id",
        "actor",
        "auth_source",
        "action",
        "target_kind",
        "target_ref_digest",
        "outcome",
        "reason_code",
        "effect_role",
        "effect_status",
        "created_at",
    }


def test_effect_columns_are_closed_enums_not_json() -> None:
    for name in ("effect_role", "effect_status"):
        column = ADMIN_AUDIT_EVENTS.c[name]
        assert isinstance(column.type, sa.Text)
        assert column.nullable is True


def test_user_status_and_role_are_checked_at_the_database_level() -> None:
    assert "ck_user_accounts_status_closed" in _check_names(USER_ACCOUNTS)
    assert "ck_user_role_assignments_role_closed" in _check_names(
        USER_ROLE_ASSIGNMENTS
    )
```

- [ ] **Step 2: 运行，确认 RED 指向缺表**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_schema.py -q 2>&1 | tail -10
```

预期：`ImportError: cannot import name 'USER_ACCOUNTS'`。

- [ ] **Step 3: 在 `schema.py` 加四张表**

在 `LOCAL_ADMINS` 定义之前插入：

```python
USER_ACCOUNTS: Final = sa.Table(
    "user_accounts",
    METADATA,
    sa.Column("user_id", sa.Text, primary_key=True),
    sa.Column("actor", sa.Text, nullable=False, unique=True),
    sa.Column("display_name", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "status IN ('active', 'disabled')",
        name="ck_user_accounts_status_closed",
    ),
)
"""身份目录账号。``actor`` 全局唯一——两个账号声称同一个 actor，任务归属就
不再可判定，而任务归属是既有 ACL 的输入。"""

USER_ROLE_ASSIGNMENTS: Final = sa.Table(
    "user_role_assignments",
    METADATA,
    sa.Column(
        "user_id",
        sa.Text,
        sa.ForeignKey("user_accounts.user_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column("tenant_id", sa.Text, primary_key=True),
    sa.Column("environment_id", sa.Text, primary_key=True),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "role IN ('admin', 'operator', 'user')",
        name="ck_user_role_assignments_role_closed",
    ),
)
"""作用域角色。``ondelete='RESTRICT'``：删账号不能把授权记录静默带走。"""

EXTERNAL_IDENTITIES: Final = sa.Table(
    "external_identities",
    METADATA,
    sa.Column("provider", sa.Text, primary_key=True),
    sa.Column("tenant_id", sa.Text, primary_key=True),
    sa.Column("environment_id", sa.Text, primary_key=True),
    sa.Column("subject_ref_digest", sa.Text, primary_key=True),
    sa.Column(
        "user_id",
        sa.Text,
        sa.ForeignKey("user_accounts.user_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "provider IN ('feishu')", name="ck_external_identities_provider_closed"
    ),
    sa.UniqueConstraint(
        "provider",
        "tenant_id",
        "environment_id",
        "user_id",
        name="uq_external_identities_one_subject_per_account",
    ),
)
"""外部主体绑定。**只存摘要**：``open_id`` 是受控 PII，落库就等于给它开了
一条同时进入备份、慢查询日志和运维截图的通道。主键保证一个主体最多绑一个
账号，UNIQUE 保证一个账号在同作用域最多绑一个主体。"""

ADMIN_AUDIT_EVENTS: Final = sa.Table(
    "admin_audit_events",
    METADATA,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("operation_id", sa.Text, nullable=False),
    sa.Column("tenant_id", sa.Text, nullable=False),
    sa.Column("environment_id", sa.Text, nullable=False),
    sa.Column("actor_user_id", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("auth_source", sa.Text, nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target_kind", sa.Text, nullable=False),
    sa.Column("target_ref_digest", sa.Text, nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("reason_code", sa.Text, nullable=True),
    sa.Column("effect_role", sa.Text, nullable=True),
    sa.Column("effect_status", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "auth_source IN ('feishu', 'local_admin')",
        name="ck_admin_audit_events_auth_source_closed",
    ),
    sa.CheckConstraint(
        "action IN ('user_created', 'user_status_changed', 'role_assigned', "
        "'role_revoked', 'external_identity_bound', 'external_identity_unbound', "
        "'local_admin_bootstrapped', 'legacy_identity_migrated')",
        name="ck_admin_audit_events_action_closed",
    ),
    sa.CheckConstraint(
        "target_kind IN ('user', 'activation', 'duty_binding', 'config', "
        "'task_content')",
        name="ck_admin_audit_events_target_kind_closed",
    ),
    sa.CheckConstraint(
        "outcome IN ('started', 'succeeded', 'denied', 'failed')",
        name="ck_admin_audit_events_outcome_closed",
    ),
    sa.CheckConstraint(
        "(outcome IN ('denied', 'failed')) = (reason_code IS NOT NULL)",
        name="ck_admin_audit_events_reason_code_matches_outcome",
    ),
    sa.CheckConstraint(
        "reason_code IS NULL OR reason_code IN ('actor_not_admin', "
        "'auth_source_not_allowed', 'target_not_found', 'scope_mismatch', "
        "'conflict', 'audit_unwritable')",
        name="ck_admin_audit_events_reason_code_closed",
    ),
    sa.CheckConstraint(
        "effect_role IS NULL OR effect_role IN ('admin', 'operator', 'user')",
        name="ck_admin_audit_events_effect_role_closed",
    ),
    sa.CheckConstraint(
        "effect_status IS NULL OR effect_status IN ('active', 'disabled')",
        name="ck_admin_audit_events_effect_status_closed",
    ),
    sa.CheckConstraint(
        "outcome = 'succeeded' OR (effect_role IS NULL AND effect_status IS NULL)",
        name="ck_admin_audit_events_effect_only_on_success",
    ),
    sa.CheckConstraint(
        "(outcome = 'succeeded' AND action IN ('user_created', 'role_assigned', "
        "'local_admin_bootstrapped', 'legacy_identity_migrated')) "
        "= (effect_role IS NOT NULL)",
        name="ck_admin_audit_events_role_effect_required",
    ),
    sa.CheckConstraint(
        "(outcome = 'succeeded' AND action IN ('user_created', "
        "'user_status_changed', 'local_admin_bootstrapped', "
        "'legacy_identity_migrated')) = (effect_status IS NOT NULL)",
        name="ck_admin_audit_events_status_effect_required",
    ),
    sa.Index(
        "uq_admin_audit_one_started_per_operation",
        "operation_id",
        unique=True,
        postgresql_where=sa.text("outcome = 'started'"),
    ),
    sa.Index(
        "uq_admin_audit_one_terminal_per_operation",
        "operation_id",
        unique=True,
        postgresql_where=sa.text(
            "outcome IN ('succeeded', 'denied', 'failed')"
        ),
    ),
    sa.Index("ix_admin_audit_events_created_at", "created_at"),
)
"""append-only 管理面审计。

``operation_id`` **不是**全局唯一：规格 §14.2 要求非事务性配置操作写
``STARTED → SUCCEEDED/FAILED``，两条事件属于同一个 operation。两条 partial
unique index 精确表达"每个 operation 至多一条 STARTED、至多一条终态"。

终态索引同时承担 Task 6 的回滚证明：对同一 operation 重复写终态事件是真实
的数据库冲突，因此能证明目录改动确实与审计在同一个事务里。
"""
```

在 `LOCAL_ADMINS` 里追加一列：

```python
    sa.Column(
        "user_id",
        sa.Text,
        sa.ForeignKey("user_accounts.user_id", ondelete="RESTRICT"),
        nullable=True,
    ),
```

并在该表 docstring 末尾补一句：`user_id` 可空是为了兼容 `rev_0014` 之前已 seed 的库；bootstrap 路径在同一事务里建账号并回填它。

最后把四张新表加入 `ALL_TABLES`（放在 `LOCAL_ADMINS` 之前，保持与定义顺序一致）。

- [ ] **Step 4: 写 `rev_0014` 迁移**

创建 `src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_directory_and_admin_audit.py`：

```python
"""Add the identity directory, admin audit log and link local credentials.

Revision ID: 0014_identity_admin_audit
Revises: 0013_clarification_parent
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from xiaowei_agent.persistence.migrations.guards import (
    require_destructive_authorization,
)

revision: str = "0014_identity_admin_audit"
down_revision: str | None = "0013_clarification_parent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USER_ACCOUNTS = sa.table("user_accounts", sa.column("user_id", sa.Text))
_ADMIN_AUDIT_EVENTS = sa.table("admin_audit_events", sa.column("event_id", sa.Text))


def upgrade() -> None:
    op.create_table(
        "user_accounts",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("actor", name="uq_user_accounts_actor"),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_user_accounts_status_closed",
        ),
    )
    op.create_table(
        "user_role_assignments",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "tenant_id", "environment_id"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_accounts.user_id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "role IN ('admin', 'operator', 'user')",
            name="ck_user_role_assignments_role_closed",
        ),
    )
    op.create_table(
        "external_identities",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("subject_ref_digest", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "provider", "tenant_id", "environment_id", "subject_ref_digest"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_accounts.user_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "provider",
            "tenant_id",
            "environment_id",
            "user_id",
            name="uq_external_identities_one_subject_per_account",
        ),
        sa.CheckConstraint(
            "provider IN ('feishu')", name="ck_external_identities_provider_closed"
        ),
    )
    op.create_table(
        "admin_audit_events",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("environment_id", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("auth_source", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_ref_digest", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("effect_role", sa.Text(), nullable=True),
        sa.Column("effect_status", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        sa.CheckConstraint(
            "auth_source IN ('feishu', 'local_admin')",
            name="ck_admin_audit_events_auth_source_closed",
        ),
        sa.CheckConstraint(
            "action IN ('user_created', 'user_status_changed', 'role_assigned', "
            "'role_revoked', 'external_identity_bound', "
            "'external_identity_unbound', 'local_admin_bootstrapped', "
            "'legacy_identity_migrated')",
            name="ck_admin_audit_events_action_closed",
        ),
        sa.CheckConstraint(
            "target_kind IN ('user', 'activation', 'duty_binding', 'config', "
            "'task_content')",
            name="ck_admin_audit_events_target_kind_closed",
        ),
        sa.CheckConstraint(
            "outcome IN ('started', 'succeeded', 'denied', 'failed')",
            name="ck_admin_audit_events_outcome_closed",
        ),
        sa.CheckConstraint(
            "(outcome IN ('denied', 'failed')) = (reason_code IS NOT NULL)",
            name="ck_admin_audit_events_reason_code_matches_outcome",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN ('actor_not_admin', "
            "'auth_source_not_allowed', 'target_not_found', 'scope_mismatch', "
            "'conflict', 'audit_unwritable')",
            name="ck_admin_audit_events_reason_code_closed",
        ),
        sa.CheckConstraint(
            "effect_role IS NULL OR effect_role IN ('admin', 'operator', 'user')",
            name="ck_admin_audit_events_effect_role_closed",
        ),
        sa.CheckConstraint(
            "effect_status IS NULL OR effect_status IN ('active', 'disabled')",
            name="ck_admin_audit_events_effect_status_closed",
        ),
        sa.CheckConstraint(
            "outcome = 'succeeded' OR (effect_role IS NULL AND effect_status IS NULL)",
            name="ck_admin_audit_events_effect_only_on_success",
        ),
        sa.CheckConstraint(
            "(outcome = 'succeeded' AND action IN ('user_created', 'role_assigned', "
            "'local_admin_bootstrapped', 'legacy_identity_migrated')) "
            "= (effect_role IS NOT NULL)",
            name="ck_admin_audit_events_role_effect_required",
        ),
        sa.CheckConstraint(
            "(outcome = 'succeeded' AND action IN ('user_created', "
            "'user_status_changed', 'local_admin_bootstrapped', "
            "'legacy_identity_migrated')) = (effect_status IS NOT NULL)",
            name="ck_admin_audit_events_status_effect_required",
        ),
    )
    op.create_index(
        "uq_admin_audit_one_started_per_operation",
        "admin_audit_events",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("outcome = 'started'"),
    )
    op.create_index(
        "uq_admin_audit_one_terminal_per_operation",
        "admin_audit_events",
        ["operation_id"],
        unique=True,
        postgresql_where=sa.text("outcome IN ('succeeded', 'denied', 'failed')"),
    )
    op.create_index(
        "ix_admin_audit_events_created_at", "admin_audit_events", ["created_at"]
    )
    op.add_column("local_admins", sa.Column("user_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_local_admins_user_id",
        "local_admins",
        "user_accounts",
        ["user_id"],
        ["user_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """删除身份目录与审计事件是破坏性的，必须显式授权。

    这三类行都不是可重建的派生数据：账号与角色是**授权事实**，审计是
    **它们为什么变成这样的唯一记录**。默认降级就静默删掉它们，等于把
    一次回滚变成一次无痕的提权窗口。
    """
    bind = op.get_bind()
    has_accounts = bool(
        bind.execute(
            sa.select(sa.literal(1)).select_from(_USER_ACCOUNTS).limit(1)
        ).first()
    )
    has_events = bool(
        bind.execute(
            sa.select(sa.literal(1)).select_from(_ADMIN_AUDIT_EVENTS).limit(1)
        ).first()
    )
    if has_accounts or has_events:
        require_destructive_authorization(
            "rev_0014 downgrade drops identity directory and admin audit rows"
        )
    op.drop_constraint("fk_local_admins_user_id", "local_admins", type_="foreignkey")
    op.drop_column("local_admins", "user_id")
    op.drop_index("ix_admin_audit_events_created_at", table_name="admin_audit_events")
    op.drop_index(
        "uq_admin_audit_one_terminal_per_operation", table_name="admin_audit_events"
    )
    op.drop_index(
        "uq_admin_audit_one_started_per_operation", table_name="admin_audit_events"
    )
    op.drop_table("admin_audit_events")
    op.drop_table("external_identities")
    op.drop_table("user_role_assignments")
    op.drop_table("user_accounts")
```

先读 `src/xiaowei_agent/persistence/migrations/guards.py` 确认 `require_destructive_authorization` 的确切签名，按它的既有形状调用，**不要**改它。

- [ ] **Step 5: 登记发布哈希并跑离线一致性**

```bash
python - <<'HASH'
import hashlib, pathlib
p = pathlib.Path(
    "src/xiaowei_agent/persistence/migrations/versions/"
    "rev_0014_identity_directory_and_admin_audit.py"
)
print(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
HASH
```

把结果加进 `tests/contract/test_schema_matches_migration.py` 的 `_PUBLISHED_REVISION_SOURCE_SHA256`。

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_schema.py \
  tests/contract/test_schema_matches_migration.py tests/contract/test_migrate_entry.py -q 2>&1 | tail -8
```

预期：全绿。若 `test_schema_matches_migration.py` 报 DDL 不一致（partial index 的 `WHERE` 子句最容易在这里对不上），说明 `schema.py` 与迁移写的不是同一套 DDL——**改到两边一致，不要改断言**。

- [ ] **Step 6: 提交**

```bash
git add src/xiaowei_agent/persistence/schema.py \
        src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_directory_and_admin_audit.py \
        tests/contract/test_schema_matches_migration.py \
        tests/contract/test_identity_schema.py
git commit -m "feat(w1a): add identity directory and admin audit tables in rev_0014

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: 内存实现与"审计由命令派生"的单一写路径

这是整个 W1a 的承重任务。

**Files:**
- Create: `src/xiaowei_agent/persistence/identity.py`
- Create: `src/xiaowei_agent/persistence/admin_audit.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `tests/conftest.py`
- Create: `tests/suites/identity_directory.py`
- Create: `tests/contract/test_identity_store.py`

**Interfaces:**
- Consumes: Task 2/3 的契约；既有 `InMemoryPersistenceState`、`Clock`
- Produces: `UserDirectoryStore` Protocol（`load_account` / `resolve_by_subject` / `apply`）、`AdminAuditStore` Protocol（`append` / `load`）、`InMemoryUserDirectoryStore`、`InMemoryAdminAuditStore`、`ACTION_FOR_COMMAND`、`EFFECT_FOR_COMMAND`、`external_subject_digest`、`IDENTITY_DIRECTORY_CASES`

**`apply` 的签名与返回：**

```python
async def apply(
    self, *, command: DirectoryCommand, context: AdminOperationContext
) -> tuple[AdminAuditEvent, ...]
```

返回元组而不是单个事件，是因为批量迁移命令一次产生 N 条事件。**单命令返回 1 元组、幂等 no-op 返回空元组**，调用方用同一个形状处理全部情况——再开一个"批量专用"方法就等于再开一条写路径。

- [ ] **Step 1: 写共享行为套件**

创建 `tests/suites/identity_directory.py`。这些用例后面会被内存与 PostgreSQL 两个绑定各跑一遍：

```python
"""UserDirectoryStore 的内存/PostgreSQL 共享行为套件。"""

from __future__ import annotations

from typing import Any

import pytest
from tests.suites.task_store import bind

from xiaowei_agent.contracts import IdentitySource
from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    CreateUserCommand,
    LegacyIdentityMigrationEntry,
    MigrateLegacyIdentitiesCommand,
    RevokeRoleCommand,
    SetUserStatusCommand,
)
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryConflictError,
    UserDirectoryNotFoundError,
)

__all__ = ["IDENTITY_DIRECTORY_CASES", "bind"]

_TENANT = "dev-local"
_ENV = "dev"
_OPEN_ID_A = "ou_" + "aaaa11112222"
_OPEN_ID_B = "ou_" + "bbbb33334444"
_FAKE_HASH = "argon2id$" + "v=19$m=65536,t=3,p=4$c29tZXNhbHQ"


def _context(operation_id: str) -> AdminOperationContext:
    """调用方能提供的全部东西——注意这里**没有** action/target/outcome。"""
    return AdminOperationContext(
        operation_id=operation_id,
        actor_user_id="usr-admin",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
    )


def _create(user_id: str = "usr-1", *, actor: str = "alice") -> CreateUserCommand:
    return CreateUserCommand(
        user_id=user_id,
        actor=actor,
        display_name=actor.title(),
        tenant_id=_TENANT,
        environment_id=_ENV,
        role=ProductRole.OPERATOR,
    )


async def _seed(store: Any, user_id: str = "usr-1", *, actor: str = "alice") -> None:
    await store.apply(
        command=_create(user_id, actor=actor), context=_context(f"op-seed-{user_id}")
    )


async def test_creating_a_user_persists_the_account_and_a_derived_audit_event(
    user_directory: Any, admin_audit: Any
) -> None:
    events = await user_directory.apply(
        command=_create(), context=_context("op-1")
    )
    assert len(events) == 1
    event = events[0]
    # 审计的这四项都不是调用方给的——它们由命令派生。
    assert event.action is AdminAuditAction.USER_CREATED
    assert event.outcome is AdminAuditOutcome.SUCCEEDED
    assert event.tenant_id == _TENANT and event.environment_id == _ENV
    assert event.effect.role is ProductRole.OPERATOR
    assert event.effect.status is UserStatus.ACTIVE

    facts = await user_directory.load_account(
        user_id="usr-1", tenant_id=_TENANT, environment_id=_ENV
    )
    assert facts is not None
    assert facts.account.actor == "alice"
    assert facts.assignment.role is ProductRole.OPERATOR
    assert await admin_audit.load(event_id=event.event_id) == event


async def test_the_audit_event_points_at_the_user_the_command_changed(
    user_directory: Any
) -> None:
    """target 摘要由命令的 user_id 派生，不可能指向别人。

    旧设计里 target 是调用方传进来的，于是"改了 A、审计写着 B"是一次
    拼写错误的距离。现在它是计算出来的。
    """
    from xiaowei_agent.contracts.admin_audit import admin_audit_target_digest
    from xiaowei_agent.contracts.enums import AdminAuditTargetKind

    events = await user_directory.apply(
        command=_create("usr-7", actor="grace"), context=_context("op-t1")
    )
    assert events[0].target_ref_digest == admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.USER, target_ref="usr-7"
    )


async def test_role_assignment_records_which_role_was_granted(
    user_directory: Any
) -> None:
    """没有 effect，审计就回答不了"改成了什么"。

    历史状态被下一次改动覆盖之后，``role_assigned`` 事件如果不带新角色，
    就再也无法还原当时授予的是 OPERATOR 还是 ADMIN。
    """
    await _seed(user_directory)
    events = await user_directory.apply(
        command=AssignRoleCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            role=ProductRole.ADMIN,
        ),
        context=_context("op-2"),
    )
    assert events[0].action is AdminAuditAction.ROLE_ASSIGNED
    assert events[0].effect.role is ProductRole.ADMIN
    assert events[0].effect.status is None
    facts = await user_directory.load_account(
        user_id="usr-1", tenant_id=_TENANT, environment_id=_ENV
    )
    assert facts is not None and facts.assignment.role is ProductRole.ADMIN


async def test_status_change_records_the_new_status(user_directory: Any) -> None:
    await _seed(user_directory)
    events = await user_directory.apply(
        command=SetUserStatusCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            status=UserStatus.DISABLED,
        ),
        context=_context("op-3"),
    )
    assert events[0].effect.status is UserStatus.DISABLED
    assert events[0].effect.role is None


async def test_revoking_a_role_claims_no_effect(user_directory: Any) -> None:
    """撤销后该作用域没有角色，记任何角色都是错的。"""
    await _seed(user_directory)
    events = await user_directory.apply(
        command=RevokeRoleCommand(
            user_id="usr-1", tenant_id=_TENANT, environment_id=_ENV
        ),
        context=_context("op-4"),
    )
    assert events[0].action is AdminAuditAction.ROLE_REVOKED
    assert events[0].effect.role is None and events[0].effect.status is None
    assert (
        await user_directory.load_account(
            user_id="usr-1", tenant_id=_TENANT, environment_id=_ENV
        )
    ) is None


async def test_audit_write_failure_rolls_back_the_authorization_change(
    user_directory: Any, admin_audit: Any
) -> None:
    """承重用例。

    同一 operation 的终态事件至多一条，因此复用 operation_id 会让审计 INSERT
    在**数据库层**失败。这是真实约束冲突，不是 mock 抛异常——后者只能证明
    我们的 except 分支写对了，证明不了两条写在同一个事务里。
    """
    await user_directory.apply(command=_create(), context=_context("op-dup"))
    with pytest.raises(AdminAuditUnwritableError):
        await user_directory.apply(
            command=_create("usr-2", actor="bob"), context=_context("op-dup")
        )
    assert (
        await user_directory.load_account(
            user_id="usr-2", tenant_id=_TENANT, environment_id=_ENV
        )
    ) is None


async def test_two_accounts_cannot_claim_the_same_actor(user_directory: Any) -> None:
    await _seed(user_directory)
    with pytest.raises(UserDirectoryConflictError):
        await user_directory.apply(
            command=_create("usr-9", actor="alice"), context=_context("op-5")
        )


async def test_commands_against_a_missing_account_are_not_found(
    user_directory: Any, admin_audit: Any
) -> None:
    """目标不存在时既不改任何东西，也不留成功审计。"""
    with pytest.raises(UserDirectoryNotFoundError):
        await user_directory.apply(
            command=AssignRoleCommand(
                user_id="ghost",
                tenant_id=_TENANT,
                environment_id=_ENV,
                role=ProductRole.ADMIN,
            ),
            context=_context("op-6"),
        )


async def test_one_subject_ref_cannot_bind_two_accounts(user_directory: Any) -> None:
    await _seed(user_directory, "usr-1", actor="alice")
    await _seed(user_directory, "usr-2", actor="bob")
    await user_directory.apply(
        command=BindExternalIdentityCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_OPEN_ID_A,
        ),
        context=_context("op-b1"),
    )
    with pytest.raises(UserDirectoryConflictError):
        await user_directory.apply(
            command=BindExternalIdentityCommand(
                user_id="usr-2",
                tenant_id=_TENANT,
                environment_id=_ENV,
                subject_ref=_OPEN_ID_A,
            ),
            context=_context("op-b2"),
        )


async def test_disabled_account_stops_resolving_on_the_next_request(
    user_directory: Any,
) -> None:
    """规格 §15.12：禁用必须在后续请求实时生效。"""
    await _seed(user_directory)
    await user_directory.apply(
        command=BindExternalIdentityCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_OPEN_ID_A,
        ),
        context=_context("op-7"),
    )
    assert (
        await user_directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_OPEN_ID_A,
        )
    ) is not None
    await user_directory.apply(
        command=SetUserStatusCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            status=UserStatus.DISABLED,
        ),
        context=_context("op-8"),
    )
    assert (
        await user_directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_OPEN_ID_A,
        )
    ) is None


async def test_a_binding_in_one_scope_does_not_resolve_in_another(
    user_directory: Any,
) -> None:
    """绑定是按作用域的（规格 §16.1「跨租户/环境冲突」）。

    如果将来有人把主键收窄成只有 subject_ref，一个测试环境的绑定就会在
    生产环境直接生效。这条用例钉住的是默认拒绝的方向。
    """
    await _seed(user_directory)
    await user_directory.apply(
        command=BindExternalIdentityCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_OPEN_ID_A,
        ),
        context=_context("op-9"),
    )
    for tenant, environment in ((_TENANT, "staging"), ("other-tenant", _ENV)):
        assert (
            await user_directory.resolve_by_subject(
                provider=IdentitySource.FEISHU,
                tenant_id=tenant,
                environment_id=environment,
                subject_ref=_OPEN_ID_A,
            )
        ) is None


async def test_unknown_subject_resolves_to_nothing(user_directory: Any) -> None:
    assert (
        await user_directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref="ou_" + "unknown0000",
        )
    ) is None


async def test_bootstrap_creates_account_role_and_credential_together(
    user_directory: Any, admin_audit: Any
) -> None:
    """本地 Admin bootstrap 走**同一条**写路径。

    它建的是第一个 ADMIN 角色，也就是一次授权改变；留在凭据存储里单独写
    就等于开第二个写入口，"任何授权改变都带审计"随即失效。
    """
    events = await user_directory.apply(
        command=BootstrapLocalAdminCommand(
            user_id="usr-local-admin",
            actor="admin",
            display_name="Local Admin",
            tenant_id=_TENANT,
            environment_id=_ENV,
            password_hash=_FAKE_HASH,
        ),
        context=_context("op-boot"),
    )
    assert len(events) == 1
    assert events[0].action is AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED
    assert events[0].effect.role is ProductRole.ADMIN
    facts = await user_directory.load_account(
        user_id="usr-local-admin", tenant_id=_TENANT, environment_id=_ENV
    )
    assert facts is not None and facts.assignment.role is ProductRole.ADMIN


async def test_bootstrap_is_idempotent_and_writes_no_audit_when_nothing_changed(
    user_directory: Any,
) -> None:
    """什么都没改，就不该有审计。

    第二次 bootstrap 返回空元组而不是抛错，保留了 ``seed_if_absent`` 现有的
    "已 seed 返回 False"语义；也不会撞上终态唯一索引。
    """
    command = BootstrapLocalAdminCommand(
        user_id="usr-local-admin",
        actor="admin",
        display_name="Local Admin",
        tenant_id=_TENANT,
        environment_id=_ENV,
        password_hash=_FAKE_HASH,
    )
    assert len(await user_directory.apply(command=command, context=_context("b1"))) == 1
    assert await user_directory.apply(command=command, context=_context("b2")) == ()


async def test_legacy_migration_writes_the_whole_batch_or_nothing(
    user_directory: Any, admin_audit: Any
) -> None:
    """整批原子性：第二条与既有账号冲突，第一条也不得留下。

    冲突制造在**数据库既有事实**上（先建一个占用 actor 的账号），而不是在
    输入文件里放两个相同 actor——后者会被 ``_IdentityDocument`` 在解析期就
    挡掉，测到的是解析器，不是迁移。
    """
    await _seed(user_directory, "usr-existing", actor="bob")
    command = MigrateLegacyIdentitiesCommand(
        tenant_id=_TENANT,
        environment_id=_ENV,
        entries=(
            LegacyIdentityMigrationEntry(
                user_id="usr-legacy-a",
                actor="alice",
                display_name="alice",
                role=ProductRole.OPERATOR,
                subject_ref=_OPEN_ID_A,
            ),
            LegacyIdentityMigrationEntry(
                user_id="usr-legacy-b",
                actor="bob",
                display_name="bob",
                role=ProductRole.USER,
                subject_ref=_OPEN_ID_B,
            ),
        ),
    )
    with pytest.raises(UserDirectoryConflictError):
        await user_directory.apply(command=command, context=_context("op-mig"))
    assert (
        await user_directory.load_account(
            user_id="usr-legacy-a", tenant_id=_TENANT, environment_id=_ENV
        )
    ) is None


async def test_legacy_migration_emits_one_audit_event_per_entry(
    user_directory: Any, admin_audit: Any
) -> None:
    command = MigrateLegacyIdentitiesCommand(
        tenant_id=_TENANT,
        environment_id=_ENV,
        entries=(
            LegacyIdentityMigrationEntry(
                user_id="usr-legacy-a",
                actor="alice",
                display_name="alice",
                role=ProductRole.OPERATOR,
                subject_ref=_OPEN_ID_A,
            ),
            LegacyIdentityMigrationEntry(
                user_id="usr-legacy-b",
                actor="bob",
                display_name="bob",
                role=ProductRole.USER,
                subject_ref=_OPEN_ID_B,
            ),
        ),
    )
    events = await user_directory.apply(command=command, context=_context("op-mig2"))
    assert len(events) == 2
    assert {event.effect.role for event in events} == {
        ProductRole.OPERATOR,
        ProductRole.USER,
    }
    assert len({event.operation_id for event in events}) == 2
    for event in events:
        assert event.action is AdminAuditAction.LEGACY_IDENTITY_MIGRATED
        assert await admin_audit.load(event_id=event.event_id) is not None


async def test_a_started_event_and_its_terminal_event_share_one_operation(
    admin_audit: Any,
) -> None:
    """规格 §14.2 的两阶段配置审计必须写得出来。

    这条用例存在的理由是：``operation_id`` 曾经被设计成全局唯一，那会让
    第二条终态事件必然撞约束，W4a 的配置审计从第一天起就无法落地。
    """
    from xiaowei_agent.contracts.admin_audit import (
        AdminAuditCandidate,
        admin_audit_target_digest,
    )
    from xiaowei_agent.contracts.enums import AdminAuditTargetKind

    def _candidate(outcome: AdminAuditOutcome, **extra: Any) -> AdminAuditCandidate:
        return AdminAuditCandidate(
            operation_id="cfg-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            actor_user_id="usr-admin",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
            action=AdminAuditAction.ROLE_ASSIGNED,
            target_kind=AdminAuditTargetKind.USER,
            target_ref_digest=admin_audit_target_digest(
                target_kind=AdminAuditTargetKind.USER, target_ref="usr-1"
            ),
            outcome=outcome,
            **extra,
        )

    started = await admin_audit.append(
        candidate=_candidate(AdminAuditOutcome.STARTED)
    )
    terminal = await admin_audit.append(
        candidate=_candidate(
            AdminAuditOutcome.SUCCEEDED, effect=_role_effect()
        )
    )
    assert started.operation_id == terminal.operation_id
    assert started.event_id != terminal.event_id


async def test_a_second_started_or_second_terminal_event_is_rejected(
    admin_audit: Any,
) -> None:
    """两阶段允许两条，不允许三条。"""
    from xiaowei_agent.contracts.admin_audit import (
        AdminAuditCandidate,
        admin_audit_target_digest,
    )
    from xiaowei_agent.contracts.enums import AdminAuditReasonCode, AdminAuditTargetKind
    from xiaowei_agent.persistence.admin_audit import AdminAuditConflictError

    def _candidate(outcome: AdminAuditOutcome, **extra: Any) -> AdminAuditCandidate:
        return AdminAuditCandidate(
            operation_id="cfg-2",
            tenant_id=_TENANT,
            environment_id=_ENV,
            actor_user_id="usr-admin",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
            action=AdminAuditAction.ROLE_REVOKED,
            target_kind=AdminAuditTargetKind.USER,
            target_ref_digest=admin_audit_target_digest(
                target_kind=AdminAuditTargetKind.USER, target_ref="usr-1"
            ),
            outcome=outcome,
            **extra,
        )

    await admin_audit.append(candidate=_candidate(AdminAuditOutcome.STARTED))
    with pytest.raises(AdminAuditConflictError):
        await admin_audit.append(candidate=_candidate(AdminAuditOutcome.STARTED))
    await admin_audit.append(candidate=_candidate(AdminAuditOutcome.SUCCEEDED))
    with pytest.raises(AdminAuditConflictError):
        await admin_audit.append(
            candidate=_candidate(
                AdminAuditOutcome.FAILED,
                reason_code=AdminAuditReasonCode.CONFLICT,
            )
        )


def _role_effect() -> Any:
    from xiaowei_agent.contracts.admin_audit import AdminAuditEffect

    return AdminAuditEffect(role=ProductRole.ADMIN)


IDENTITY_DIRECTORY_CASES = (
    test_creating_a_user_persists_the_account_and_a_derived_audit_event,
    test_the_audit_event_points_at_the_user_the_command_changed,
    test_role_assignment_records_which_role_was_granted,
    test_status_change_records_the_new_status,
    test_revoking_a_role_claims_no_effect,
    test_audit_write_failure_rolls_back_the_authorization_change,
    test_two_accounts_cannot_claim_the_same_actor,
    test_commands_against_a_missing_account_are_not_found,
    test_one_subject_ref_cannot_bind_two_accounts,
    test_disabled_account_stops_resolving_on_the_next_request,
    test_a_binding_in_one_scope_does_not_resolve_in_another,
    test_unknown_subject_resolves_to_nothing,
    test_bootstrap_creates_account_role_and_credential_together,
    test_bootstrap_is_idempotent_and_writes_no_audit_when_nothing_changed,
    test_legacy_migration_writes_the_whole_batch_or_nothing,
    test_legacy_migration_emits_one_audit_event_per_entry,
    test_a_started_event_and_its_terminal_event_share_one_operation,
    test_a_second_started_or_second_terminal_event_is_rejected,
)

ALL_GROUPS = {"identity_directory": IDENTITY_DIRECTORY_CASES}
```

创建 `tests/contract/test_identity_store.py`：

```python
"""UserDirectoryStore 内存绑定、协议窄度与命令映射完备性。"""

import inspect

from tests.suites.identity_directory import IDENTITY_DIRECTORY_CASES, bind

from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    CreateUserCommand,
    MigrateLegacyIdentitiesCommand,
    RevokeRoleCommand,
    SetUserStatusCommand,
    UnbindExternalIdentityCommand,
)
from xiaowei_agent.persistence.admin_audit import AdminAuditStore
from xiaowei_agent.persistence.identity import (
    ACTION_FOR_COMMAND,
    EFFECT_FOR_COMMAND,
    UserDirectoryStore,
)

bind(globals(), IDENTITY_DIRECTORY_CASES)

_ALL_COMMANDS = {
    CreateUserCommand,
    SetUserStatusCommand,
    AssignRoleCommand,
    RevokeRoleCommand,
    BindExternalIdentityCommand,
    UnbindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    MigrateLegacyIdentitiesCommand,
}


def test_directory_store_exposes_exactly_one_write_method() -> None:
    """``apply`` 是唯一写路径。

    多一个写方法，"授权改变必然带审计"就从类型保证退化成使用约定，而
    使用约定挡不住下一个赶工的调用方。
    """
    assert {name for name in dir(UserDirectoryStore) if not name.startswith("_")} == {
        "load_account",
        "resolve_by_subject",
        "apply",
    }


def test_apply_gives_the_caller_no_way_to_steer_the_audit() -> None:
    """签名级守卫。

    只要出现 ``audit``、``action``、``override_action`` 这类参数，调用方就又
    拿回了"这次改动记成什么"的话语权——上一版计划正是栽在这个 override 上。
    """
    parameters = set(inspect.signature(UserDirectoryStore.apply).parameters)
    assert parameters == {"self", "command", "context"}


def test_admin_audit_store_is_append_only_by_shape() -> None:
    assert {name for name in dir(AdminAuditStore) if not name.startswith("_")} == {
        "append",
        "load",
    }


def test_every_command_has_a_declared_action_and_effect() -> None:
    """新增命令而忘了声明动作或 effect，必须炸在这里。"""
    assert set(ACTION_FOR_COMMAND) == _ALL_COMMANDS
    assert set(EFFECT_FOR_COMMAND) == _ALL_COMMANDS
```

- [ ] **Step 2: 运行，确认 RED 指向缺模块**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py -q 2>&1 | tail -10
```

预期：`ModuleNotFoundError: No module named 'xiaowei_agent.persistence.identity'`。

- [ ] **Step 3: 写 `persistence/admin_audit.py`**

```python
"""append-only 管理面审计的存储契约与内存实现。"""

from __future__ import annotations

import uuid
from typing import Final, Protocol

from xiaowei_agent.contracts.admin_audit import AdminAuditCandidate, AdminAuditEvent
from xiaowei_agent.contracts.enums import AdminAuditOutcome
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.store import Clock

TERMINAL_OUTCOMES: Final[frozenset[AdminAuditOutcome]] = frozenset(
    {
        AdminAuditOutcome.SUCCEEDED,
        AdminAuditOutcome.DENIED,
        AdminAuditOutcome.FAILED,
    }
)


class AdminAuditError(RuntimeError):
    """审计存储错误；不把异常正文、Secret 或 PII 放进消息。"""


class AdminAuditConflictError(AdminAuditError):
    """同一个 ``operation_id`` 已经有同阶段事件。

    每个 operation 至多一条 ``STARTED`` 和一条终态；这不是"一次操作一条
    事件"，因为规格 §14.2 的非事务配置动作必须写两条。
    """


class AdminAuditStore(Protocol):
    """只有追加与读取。

    **没有 update、没有 delete、没有清理。** 规格 §14.2 明确要求首版不提供
    更新/删除 API；能删的审计在事后争议里等于没有审计。
    """

    async def append(self, *, candidate: AdminAuditCandidate) -> AdminAuditEvent: ...

    async def load(self, *, event_id: str) -> AdminAuditEvent | None: ...


def audit_stage_key(candidate: AdminAuditCandidate) -> tuple[str, str]:
    """(operation_id, 阶段)；内存实现用它表达两条 partial unique index。"""
    stage = "terminal" if candidate.outcome in TERMINAL_OUTCOMES else "started"
    return (candidate.operation_id, stage)


class InMemoryAdminAuditStore:
    """与 TaskStore 共用同一把锁和同一份 state 的单进程实现。"""

    def __init__(self, *, state: InMemoryPersistenceState, clock: Clock) -> None:
        self._state = state
        self._clock = clock

    async def append(self, *, candidate: AdminAuditCandidate) -> AdminAuditEvent:
        async with self._state.lock:
            return self.append_locked(candidate)

    def append_locked(self, candidate: AdminAuditCandidate) -> AdminAuditEvent:
        """已持锁时的追加；``InMemoryUserDirectoryStore`` 在自己的临界区内复用它。

        复用而不是复制：两份追加逻辑会在"什么算冲突"上分叉，而这正是
        目录回滚证明所依赖的那条约束。
        """
        key = audit_stage_key(candidate)
        if key in self._state.admin_audit_stage_keys:
            raise AdminAuditConflictError("operation already has an event at this stage")
        event = AdminAuditEvent(
            **candidate.model_dump(mode="python"),
            event_id=uuid.uuid4().hex,
            created_at=self._clock(),
        )
        self._state.admin_audit_stage_keys.add(key)
        self._state.admin_audit_events[event.event_id] = event
        return event

    async def load(self, *, event_id: str) -> AdminAuditEvent | None:
        async with self._state.lock:
            return self._state.admin_audit_events.get(event_id)


__all__ = [
    "TERMINAL_OUTCOMES",
    "AdminAuditConflictError",
    "AdminAuditError",
    "AdminAuditStore",
    "InMemoryAdminAuditStore",
    "audit_stage_key",
]
```

- [ ] **Step 4: 写 `persistence/identity.py` 的派生层与协议**

```python
"""身份目录的存储契约与内存实现。

**唯一的写方法是 ``apply(command, context)``。** 调用方只提供操作者上下文；
审计事件的 action、target、outcome 与 effect 由 store 从命令**派生**。
调用方没有机会把它们写错，因此这里不需要（也不应该有）一层"校验调用方
有没有写对"的防线——那层防线的存在本身就意味着写错是可能的。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Final, Protocol

from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditEffect,
    AdminAuditEvent,
    AdminOperationContext,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    CreateUserCommand,
    DirectoryCommand,
    DirectoryPrincipalFacts,
    MigrateLegacyIdentitiesCommand,
    RevokeRoleCommand,
    SetUserStatusCommand,
    UnbindExternalIdentityCommand,
)

_EXTERNAL_SUBJECT_DOMAIN: Final[str] = "external-subject:v1"

BOOTSTRAP_ACTOR_USER_ID: Final[str] = "system-bootstrap"
"""首次 bootstrap 的保留操作者。

bootstrap 发生时还没有任何 Admin 账号，因此操作者不可能是某个人。用一个
保留常量而不是把 bootstrap 列为"不写审计的例外"：例外会让"每一次授权改变
都有审计"这句话需要附加条件，而附加条件是审计体系瓦解的起点。
"""


class UserDirectoryError(RuntimeError):
    """身份目录写入错误；消息里不含 ``subject_ref``、口令或异常正文。"""


class UserDirectoryConflictError(UserDirectoryError):
    """actor、subject 或账号绑定与既有事实冲突。"""


class UserDirectoryNotFoundError(UserDirectoryError):
    """命令的目标账号不存在。"""


class AdminAuditUnwritableError(UserDirectoryError):
    """审计写入失败；授权改变必须随之回滚。"""


def external_subject_digest(
    *, provider: IdentitySource, tenant_id: str, environment_id: str, subject_ref: str
) -> str:
    """外部主体的存储摘要；与审计 target 摘要**不同域**。

    同域会让"某个 open_id 的绑定记录"和"针对该用户的审计事件"算出相等的
    摘要，于是拿到一张表就能反查另一张表的关联——两张表各自脱敏，合起来
    却不脱敏。
    """
    payload = (
        f"{_EXTERNAL_SUBJECT_DOMAIN}:{provider.value}:"
        f"{tenant_id}:{environment_id}:{subject_ref}"
    )
    return sha256(payload.encode()).hexdigest()


ACTION_FOR_COMMAND: Final[dict[type, AdminAuditAction]] = {
    CreateUserCommand: AdminAuditAction.USER_CREATED,
    SetUserStatusCommand: AdminAuditAction.USER_STATUS_CHANGED,
    AssignRoleCommand: AdminAuditAction.ROLE_ASSIGNED,
    RevokeRoleCommand: AdminAuditAction.ROLE_REVOKED,
    BindExternalIdentityCommand: AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
    UnbindExternalIdentityCommand: AdminAuditAction.EXTERNAL_IDENTITY_UNBOUND,
    BootstrapLocalAdminCommand: AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED,
    MigrateLegacyIdentitiesCommand: AdminAuditAction.LEGACY_IDENTITY_MIGRATED,
}
"""命令类型到它**必然**记录的审计动作。

绑死而不是让调用方传：调用方能选，就能选错——一边撤销角色、一边记一条
``user_created``，审计从此只是一段与事实无关的文本。
"""


def _create_effect(command: CreateUserCommand) -> AdminAuditEffect:
    return AdminAuditEffect(role=command.role, status=UserStatus.ACTIVE)


def _status_effect(command: SetUserStatusCommand) -> AdminAuditEffect:
    return AdminAuditEffect(status=command.status)


def _role_effect(command: AssignRoleCommand) -> AdminAuditEffect:
    return AdminAuditEffect(role=command.role)


def _no_effect(command: object) -> AdminAuditEffect:
    return AdminAuditEffect()


def _bootstrap_effect(command: BootstrapLocalAdminCommand) -> AdminAuditEffect:
    return AdminAuditEffect(role=ProductRole.ADMIN, status=UserStatus.ACTIVE)


EFFECT_FOR_COMMAND: Final[dict[type, object]] = {
    CreateUserCommand: _create_effect,
    SetUserStatusCommand: _status_effect,
    AssignRoleCommand: _role_effect,
    RevokeRoleCommand: _no_effect,
    BindExternalIdentityCommand: _no_effect,
    UnbindExternalIdentityCommand: _no_effect,
    BootstrapLocalAdminCommand: _bootstrap_effect,
    MigrateLegacyIdentitiesCommand: _no_effect,  # 批量按条目单独派生，见 derive_audit
}
"""命令类型到"这次改成了什么"的派生函数。

``MigrateLegacyIdentitiesCommand`` 的条目各有自己的角色，因此它在
``derive_audit()`` 里按条目派生，表里的占位只用于完备性检查。
"""


def derive_audit(
    command: DirectoryCommand,
    context: AdminOperationContext,
    *,
    target_user_id: str,
    operation_id: str,
    effect: AdminAuditEffect,
) -> AdminAuditCandidate:
    """把命令 + 操作者上下文变成一条成功审计事实。

    注意这里没有 ``outcome`` 参数：能走到这个函数，就意味着改动即将在同一
    事务里提交。失败与拒绝不经过它——它们没有授权改变可绑定，走
    ``AdminAuditStore.append()``。
    """
    return AdminAuditCandidate(
        operation_id=operation_id,
        tenant_id=command.tenant_id,
        environment_id=command.environment_id,
        actor_user_id=context.actor_user_id,
        actor=context.actor,
        auth_source=context.auth_source,
        action=ACTION_FOR_COMMAND[type(command)],
        target_kind=AdminAuditTargetKind.USER,
        target_ref_digest=admin_audit_target_digest(
            target_kind=AdminAuditTargetKind.USER, target_ref=target_user_id
        ),
        outcome=AdminAuditOutcome.SUCCEEDED,
        effect=effect,
    )


def batch_operation_id(context: AdminOperationContext, index: int) -> str:
    """批量条目的子 operation id；有界，因为 context.operation_id ≤ 48。"""
    return f"{context.operation_id}:{index}"


class UserDirectoryStore(Protocol):
    async def load_account(
        self, *, user_id: str, tenant_id: str, environment_id: str
    ) -> DirectoryPrincipalFacts | None: ...

    async def resolve_by_subject(
        self,
        *,
        provider: IdentitySource,
        tenant_id: str,
        environment_id: str,
        subject_ref: str,
    ) -> DirectoryPrincipalFacts | None: ...

    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]: ...
```

- [ ] **Step 5: 写内存实现**

在同文件追加 `InMemoryUserDirectoryStore`。它需要 Step 4 的导入块之外再补五项：

```python
from xiaowei_agent.contracts.identity import UserAccount, UserRoleAssignment
from xiaowei_agent.persistence.admin_audit import (
    AdminAuditConflictError,
    InMemoryAdminAuditStore,
)
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.store import Clock
```

`identity.py` 依赖 `admin_audit.py` 而不是反过来——追加审计的逻辑只有一份，
由目录 store 复用，不在两个模块各写一遍："什么算冲突"分叉之后，回滚证明
所依赖的那条约束就不再是同一条了。



```python
class InMemoryUserDirectoryStore:
    """与 TaskStore 共用同一把锁和同一份 state 的单进程实现。

    共用是必须的：如果目录和审计各自持有 state，"同事务"在内存实现上就会
    无条件成立，共享套件也就证明不了任何东西。
    """

    def __init__(
        self,
        *,
        state: InMemoryPersistenceState,
        clock: Clock,
        audit: InMemoryAdminAuditStore,
    ) -> None:
        self._state = state
        self._clock = clock
        self._audit = audit

    async def load_account(
        self, *, user_id: str, tenant_id: str, environment_id: str
    ) -> DirectoryPrincipalFacts | None:
        async with self._state.lock:
            return self._facts(user_id, tenant_id, environment_id)

    async def resolve_by_subject(
        self,
        *,
        provider: IdentitySource,
        tenant_id: str,
        environment_id: str,
        subject_ref: str,
    ) -> DirectoryPrincipalFacts | None:
        digest = external_subject_digest(
            provider=provider,
            tenant_id=tenant_id,
            environment_id=environment_id,
            subject_ref=subject_ref,
        )
        async with self._state.lock:
            user_id = self._state.external_identities.get(
                (provider.value, tenant_id, environment_id, digest)
            )
            if user_id is None:
                return None
            facts = self._facts(user_id, tenant_id, environment_id)
            if facts is None or facts.account.status is not UserStatus.ACTIVE:
                # 禁用必须在**下一个请求**上生效，而不是等 session 过期。
                return None
            return facts

    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]:
        async with self._state.lock:
            snapshot = self._snapshot()
            try:
                return self._apply_locked(command, context)
            except (UserDirectoryError, AdminAuditConflictError) as exc:
                # 内存实现没有事务，因此显式恢复快照。这不是"多此一举的
                # 特殊分支"：不恢复的话，共享套件里的回滚用例会在内存实现上
                # 无条件失败，而它必须在两个实现上表达同一个语义。
                self._restore(snapshot)
                if isinstance(exc, AdminAuditConflictError):
                    raise AdminAuditUnwritableError(
                        "admin audit event could not be written"
                    ) from None
                raise
```

`_apply_locked` 按命令类型分流，全部复用 `derive_audit()` 与 `self._audit.append_locked()`；`_snapshot`/`_restore` 对五个 dict 与两个集合做浅拷贝与整体替换。分流规则：

- `CreateUserCommand`：`user_id` 已存在或 `actor` 被占用 → `UserDirectoryConflictError`；否则写账号 + 角色 + 一条审计。
- `SetUserStatusCommand` / `AssignRoleCommand` / `RevokeRoleCommand` / `BindExternalIdentityCommand` / `UnbindExternalIdentityCommand`：目标账号或角色不存在 → `UserDirectoryNotFoundError`。
- `BindExternalIdentityCommand`：摘要已绑到别的账号、或该账号在本作用域已有绑定 → `UserDirectoryConflictError`。
- `BootstrapLocalAdminCommand`：`local_admins` 已有行 → **返回空元组**（幂等 no-op，不是错误）；否则写账号 + ADMIN 角色 + 凭据行 + 一条审计。
- `MigrateLegacyIdentitiesCommand`：逐条写账号 + 角色 + 绑定，每条用 `batch_operation_id(context, index)` 写一条审计；任一条冲突整批抛错，由上面的快照恢复保证零部分落库。

- [ ] **Step 6: 扩 `InMemoryPersistenceState` 与 conftest**

在 `persistence/memory.py` 的 `InMemoryPersistenceState` 上加五个字段：

```python
    user_accounts: dict[str, UserAccount] = field(default_factory=dict)
    user_role_assignments: dict[tuple[str, str, str], UserRoleAssignment] = field(
        default_factory=dict
    )
    external_identities: dict[tuple[str, str, str, str], str] = field(
        default_factory=dict
    )
    admin_audit_events: dict[str, AdminAuditEvent] = field(default_factory=dict)
    admin_audit_stage_keys: set[tuple[str, str]] = field(default_factory=set)
```

`external_identities` 的键是 `(provider, tenant_id, environment_id, subject_ref_digest)`，与 `external_identities` 表的主键**逐列相同**——键形状不一致，共享套件就会在两个实现上测出不同的冲突语义。

在 `tests/conftest.py` 里按现有 store fixture 的写法加 `admin_audit` 与 `user_directory`，两者共享同一个 `InMemoryPersistenceState`：

```python
@pytest.fixture
def admin_audit(persistence_state: Any, clock: Any) -> InMemoryAdminAuditStore:
    return InMemoryAdminAuditStore(state=persistence_state, clock=clock)


@pytest.fixture
def user_directory(
    persistence_state: Any, clock: Any, admin_audit: InMemoryAdminAuditStore
) -> InMemoryUserDirectoryStore:
    return InMemoryUserDirectoryStore(
        state=persistence_state, clock=clock, audit=admin_audit
    )
```

`persistence_state` 与 `clock` 用该文件已有的同名 fixture；若名字不同，按实际名字改，**不要**新建一份 state。

- [ ] **Step 7: 运行，确认全绿**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py \
  tests/contract/test_admin_audit_contracts.py -q 2>&1 | tail -6
ruff check . && mypy src
```

- [ ] **Step 8: 隔离变异反证（两处承重保护）**

变异一：让审计追加脱离目录改动的原子性。

```bash
cp src/xiaowei_agent/persistence/identity.py /tmp/keep-identity.py
python - <<'MUT'
from pathlib import Path
p = Path("src/xiaowei_agent/persistence/identity.py")
t = p.read_text()
# 去掉失败恢复：等价于"审计写不进去，但权限改动留下了"
t = t.replace("                self._restore(snapshot)\n", "")
p.write_text(t)
MUT
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py -q 2>&1 | tail -6
cp /tmp/keep-identity.py src/xiaowei_agent/persistence/identity.py && rm /tmp/keep-identity.py
```

预期：`test_audit_write_failure_rolls_back_the_authorization_change` 与 `test_legacy_migration_writes_the_whole_batch_or_nothing` 同时变红。

变异二：让 effect 恒为空。

```bash
cp src/xiaowei_agent/persistence/identity.py /tmp/keep-identity.py
python - <<'MUT'
from pathlib import Path
p = Path("src/xiaowei_agent/persistence/identity.py")
t = p.read_text()
t = t.replace(
    "def _role_effect(command: AssignRoleCommand) -> AdminAuditEffect:\n"
    "    return AdminAuditEffect(role=command.role)",
    "def _role_effect(command: AssignRoleCommand) -> AdminAuditEffect:\n"
    "    return AdminAuditEffect()",
)
p.write_text(t)
MUT
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py -q 2>&1 | tail -6
cp /tmp/keep-identity.py src/xiaowei_agent/persistence/identity.py && rm /tmp/keep-identity.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py -q 2>&1 | tail -3
```

预期：`test_role_assignment_records_which_role_was_granted` 变红（契约层的 `_effect_matches_action_and_outcome` 会先拒绝它）。**两次变异都必须确认还原成功再提交。**

- [ ] **Step 9: 提交**

```bash
git add src/xiaowei_agent/persistence/identity.py \
        src/xiaowei_agent/persistence/admin_audit.py \
        src/xiaowei_agent/persistence/memory.py \
        tests/suites/identity_directory.py \
        tests/contract/test_identity_store.py \
        tests/conftest.py
git commit -m "feat(w1a): derive admin audit from the command on a single write path

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: PostgreSQL 实现、真实 fixture 与同事务证明

**Files:**
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/local_admin.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `tests/integration/conftest.py`
- Create: `tests/integration/test_identity_directory_postgres.py`
- Modify: `tests/contract/test_protocol_conformance.py`

**Interfaces:**
- Consumes: Task 4 的表、Task 5 的协议、派生层与共享套件
- Produces: `PostgresUserDirectoryStore`、`PostgresAdminAuditStore`；`seed_if_absent` 改为走 `apply()`

**fixture 必须显式定义。** 仓库的共享套件靠 `tests/integration/conftest.py` 用**同名** fixture 覆盖根 conftest 的内存实现（`store`、`plan_store`、`clarification_record_store` 都是这样）。只写 `bind(...)` 而不定义同名 fixture，集成文件会安静地继续跑内存实现，"PostgreSQL 同事务证据"就是假绿。另外该 conftest 里**没有** `engine` fixture，可用的是 `migrated_engine`（session 级）与 `clean_database`（函数级，每个用例清库）。

- [ ] **Step 1: 加 PostgreSQL fixture**

在 `tests/integration/conftest.py` 里，按 `clarification_record_store` 的既有写法追加：

```python
@pytest.fixture
def admin_audit(clean_database: AsyncEngine, clock: Any) -> PostgresAdminAuditStore:
    return PostgresAdminAuditStore(engine=clean_database, clock=clock)


@pytest.fixture
def user_directory(
    clean_database: AsyncEngine, clock: Any
) -> PostgresUserDirectoryStore:
    return PostgresUserDirectoryStore(engine=clean_database, clock=clock)
```

两者都接 `clean_database`，因此它们操作的是**同一个** engine、同一个库——这是"同事务"用例有意义的前提。

- [ ] **Step 2: 写 PostgreSQL 绑定测试**

创建 `tests/integration/test_identity_directory_postgres.py`：

```python
"""UserDirectoryStore PostgreSQL 共享绑定与真实事务证明。"""

from typing import Any

import pytest
import sqlalchemy as sa
from tests.suites.identity_directory import IDENTITY_DIRECTORY_CASES, bind

from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.enums import IdentitySource, ProductRole
from xiaowei_agent.contracts.identity import CreateUserCommand
from xiaowei_agent.persistence.admin_audit import AdminAuditStore
from xiaowei_agent.persistence.identity import AdminAuditUnwritableError
from xiaowei_agent.persistence.postgres import (
    PostgresAdminAuditStore,
    PostgresUserDirectoryStore,
)
from xiaowei_agent.persistence.schema import ADMIN_AUDIT_EVENTS, USER_ACCOUNTS

bind(globals(), IDENTITY_DIRECTORY_CASES)


def test_the_bound_fixtures_are_the_postgres_implementations(
    user_directory: Any, admin_audit: AdminAuditStore
) -> None:
    """守住"这组用例真的跑在 PostgreSQL 上"。

    共享套件靠同名 fixture 覆盖根 conftest 的内存实现。漏定义一个，整组
    用例会安静地跑内存版并全绿——而这里唯一有价值的证据恰恰是"真库上
    也成立"。这条断言让那种情况变成失败而不是假绿。
    """
    assert type(user_directory) is PostgresUserDirectoryStore
    assert type(admin_audit) is PostgresAdminAuditStore


async def test_rolled_back_change_leaves_no_row_in_either_table(
    user_directory: PostgresUserDirectoryStore, clean_database: Any
) -> None:
    """回滚必须是数据库回滚，不是应用层把内存里的改动撤掉。

    直接查两张表：如果 ``apply`` 用了两个独立事务，用户行会留在库里而审计
    行不在——这正是"授权改了但没人知道"的形态。
    """

    def _context(operation_id: str) -> AdminOperationContext:
        return AdminOperationContext(
            operation_id=operation_id,
            actor_user_id="usr-admin",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
        )

    def _create(user_id: str, actor: str) -> CreateUserCommand:
        return CreateUserCommand(
            user_id=user_id,
            actor=actor,
            display_name=actor,
            tenant_id="dev-local",
            environment_id="dev",
            role=ProductRole.OPERATOR,
        )

    await user_directory.apply(
        command=_create("usr-1", "alice"), context=_context("dup")
    )
    with pytest.raises(AdminAuditUnwritableError):
        await user_directory.apply(
            command=_create("usr-2", "bob"), context=_context("dup")
        )

    async with clean_database.connect() as connection:
        accounts = (
            await connection.execute(
                sa.select(USER_ACCOUNTS.c.user_id).where(
                    USER_ACCOUNTS.c.user_id == "usr-2"
                )
            )
        ).all()
        events = (
            await connection.execute(
                sa.select(sa.func.count()).select_from(ADMIN_AUDIT_EVENTS)
            )
        ).scalar_one()
    assert accounts == []
    assert events == 1
```

- [ ] **Step 3: 运行，确认 RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_identity_directory_postgres.py -q 2>&1 | tail -10
```

需要本机 PostgreSQL。若整组 skip，说明库没起——**先把库起起来**，本任务的全部价值就在真库上，skip 不算通过。仓库的 `unexpected_integration_skips` 钩子也会把静默跳过报出来。

- [ ] **Step 4: 写 PostgreSQL 实现**

在 `postgres.py` 末尾按既有类的写法追加两个类。`apply()` 的形状是本任务的全部要点，必须逐字照这个骨架：

```python
class PostgresUserDirectoryStore:
    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]:
        try:
            # 一个 begin()，目录写入与审计 INSERT 都在里面。**不要**在这里开
            # 第二个连接或第二个事务：那正是"权限改了、审计没记上"能够发生的
            # 唯一形态。
            async with self._engine.begin() as connection:
                return await self._apply_in_transaction(connection, command, context)
        except IntegrityError as exc:
            raise _classify_directory_integrity_error(exc) from None
```

分流函数按**约束名**判断，而不是按异常文本匹配：

```python
_AUDIT_CONSTRAINTS: Final[frozenset[str]] = frozenset(
    {
        "uq_admin_audit_one_started_per_operation",
        "uq_admin_audit_one_terminal_per_operation",
        "admin_audit_events_pkey",
    }
)


def _classify_directory_integrity_error(exc: IntegrityError) -> UserDirectoryError:
    """把数据库约束冲突翻译成闭集错误。

    ``from None`` 与固定消息是一组的：``IntegrityError`` 的文本里带着被拒绝
    的那一行，其中包含 ``subject_ref_digest``、actor 和租户。让它进 traceback
    就等于把受控 PII 写进日志。
    """
    name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None) or ""
    if name in _AUDIT_CONSTRAINTS:
        return AdminAuditUnwritableError("admin audit event could not be written")
    return UserDirectoryConflictError("directory command conflicts with stored facts")
```

`_apply_in_transaction` 的规则：

- 按命令类型发 INSERT/UPDATE/DELETE，全部用传入的 `connection` 与同一个 `now`。
- 目标账号或角色不存在（UPDATE/DELETE 影响 0 行）→ 抛 `UserDirectoryNotFoundError`，**在同一事务内**，因此审计也不会留下。
- 每条成功改动用 Task 5 的 `derive_audit()` 生成候选，再 INSERT 进 `ADMIN_AUDIT_EVENTS`；批量命令按 `batch_operation_id(context, index)` 逐条生成。审计 INSERT 放在各自改动之后、事务提交之前。
- `BootstrapLocalAdminCommand`：先用 `INSERT ... ON CONFLICT DO NOTHING ... RETURNING id` 试写 `local_admins`；没拿到 id 说明已 bootstrap，**直接返回空元组**（整个事务不写任何行），保持 `seed_if_absent` 现有的"已 seed 返回 False"语义。
- `external_identities` 存 `external_subject_digest(...)`，不存 `open_id`。
- `resolve_by_subject` 用一次 JOIN 取 account + assignment，并在 SQL 层就写 `WHERE user_accounts.status = 'active'`——把禁用判断放在 Python 里，就得依赖每个调用方都记得判一次。
- `PostgresAdminAuditStore.append()` 只做 INSERT + 返回，捕获 `IntegrityError` 转 `AdminAuditConflictError`；它**没有** UPDATE/DELETE 路径，Task 7 的 AST 守卫会机械确认这一点。

- [ ] **Step 5: 让 `seed_if_absent` 走同一条写路径**

改写 `PostgresLocalAdminStore.seed_if_absent`，**不改签名**（它有 9 处调用方，其中 6 处在既有 RI5 测试里）：

```python
    async def seed_if_absent(self, *, password_hash: str) -> bool:
        """首次 seed 本地管理员。

        **不再自己写库。** 它建的是第一个 ADMIN 角色，也就是一次授权改变，
        因此必须走 ``UserDirectoryStore.apply()`` 那条唯一写路径，和账号、
        角色、审计一起在同一个事务里完成。留在这里单独 INSERT 就等于开了
        第二个写入口，"任何授权改变都带审计"随即失效。

        改密仍留在本类：它改的是凭据，不是授权（规格 §14.2）。
        """
        directory = PostgresUserDirectoryStore(engine=self._engine, clock=self._clock)
        events = await directory.apply(
            command=BootstrapLocalAdminCommand(
                user_id=LOCAL_ADMIN_USER_ID,
                actor=LOCAL_ADMIN_ACTOR,
                display_name=LOCAL_ADMIN_DISPLAY_NAME,
                tenant_id=LOCAL_ADMIN_TENANT_ID,
                environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
                password_hash=password_hash,
            ),
            context=AdminOperationContext(
                operation_id=f"bootstrap-{uuid.uuid4().hex[:24]}",
                actor_user_id=BOOTSTRAP_ACTOR_USER_ID,
                actor=LOCAL_ADMIN_ACTOR,
                auth_source=IdentitySource.LOCAL_ADMIN,
            ),
        )
        return bool(events)
```

`LOCAL_ADMIN_TENANT_ID` / `LOCAL_ADMIN_ENVIRONMENT_ID` / `LOCAL_ADMIN_ACTOR` 取自既有 `LOCAL_ADMIN_PRINCIPAL`（`interfaces/local_admin_auth.py:127`，值为 `dev-local` / `dev` / `admin`），**从那里导入或与它并置定义，不要各写一遍字面量**。每次调用用新的 `operation_id`，因为已 bootstrap 的情况返回空元组、不写审计，不会撞终态索引。

补一条测试钉住这个行为（放在 `tests/integration/test_identity_directory_postgres.py`）：

```python
async def test_seeding_the_local_admin_writes_its_audit_event(
    clean_database: Any, clock: Any
) -> None:
    """seed 是一次授权改变，必须留下审计。

    这条用例存在的理由：上一版计划里 ``seed_if_absent`` 直接写账号和 ADMIN
    角色却不写审计，第一次部署就违反了"任何授权改变都带审计"。
    """
    from xiaowei_agent.interfaces.local_admin_auth import hash_password
    from xiaowei_agent.persistence.local_admin import PostgresLocalAdminStore

    admins = PostgresLocalAdminStore(engine=clean_database, clock=clock)
    assert await admins.seed_if_absent(password_hash=hash_password("adm" + "in")) is True
    assert await admins.seed_if_absent(password_hash=hash_password("adm" + "in")) is False

    async with clean_database.connect() as connection:
        rows = (
            await connection.execute(
                sa.select(
                    ADMIN_AUDIT_EVENTS.c.action, ADMIN_AUDIT_EVENTS.c.effect_role
                )
            )
        ).all()
    assert rows == [("local_admin_bootstrapped", "admin")]
```

- [ ] **Step 6: 登记 Protocol 一致性**

在 `_conformance.py` 里按既有写法为 `UserDirectoryStore` 与 `AdminAuditStore` 各加一条 Protocol 断言，覆盖内存与 PostgreSQL 两个实现。

- [ ] **Step 7: 运行四组测试**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_identity_directory_postgres.py -q 2>&1 | tail -8
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py \
  tests/contract/test_protocol_conformance.py -q 2>&1 | tail -5
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_migration_paths.py -q 2>&1 | tail -5
PYTHONDONTWRITEBYTECODE=1 python -m pytest -m security -q 2>&1 | tail -3
ruff check . && mypy src
```

`seed_if_absent` 有 6 处既有测试调用方（`tests/contract/test_ri5_config_api.py`、`tests/contract/test_ri5_probe_routes.py`、`tests/security/test_ri5_web_assembly.py`、`tests/security/test_local_admin_boundary.py`），它们必须**全部保持绿**——签名与返回语义没变是本步的验收条件。若有用例变红，说明语义被改动了，回头修实现，**不要**改那些测试。

- [ ] **Step 8: 隔离变异反证**

```bash
cp src/xiaowei_agent/persistence/postgres.py /tmp/keep-pg.py
python - <<'MUT'
from pathlib import Path
p = Path("src/xiaowei_agent/persistence/postgres.py")
t = p.read_text()
# 把审计 INSERT 挪出目录事务：用一个独立连接写它
t = t.replace(
    "                return await self._apply_in_transaction(connection, command, context)",
    "                events = await self._apply_in_transaction(\n"
    "                    connection, command, context\n"
    "                )\n"
    "            return events",
)
p.write_text(t)
MUT
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_identity_directory_postgres.py -q 2>&1 | tail -6
cp /tmp/keep-pg.py src/xiaowei_agent/persistence/postgres.py && rm /tmp/keep-pg.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_identity_directory_postgres.py -q 2>&1 | tail -3
```

上面的变异只是示意形状；实际做法是把审计 INSERT 改用 `self._engine.begin()` 另开一个事务。预期：`test_rolled_back_change_leaves_no_row_in_either_table` 变红（`usr-2` 留在库里）。还原后必须回到全绿。

- [ ] **Step 9: 提交**

```bash
git add src/xiaowei_agent/persistence/postgres.py \
        src/xiaowei_agent/persistence/local_admin.py \
        src/xiaowei_agent/_conformance.py \
        tests/integration/conftest.py \
        tests/integration/test_identity_directory_postgres.py \
        tests/contract/test_protocol_conformance.py
git commit -m "feat(w1a): commit identity changes and their audit in one transaction

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: append-only、单一写路径与 PII 暴露的机械守卫

**Files:**
- Create: `tests/security/test_admin_audit_append_only.py`
- Create: `tests/security/test_identity_write_path.py`
- Create: `tests/security/test_controlled_pii_exposure.py`

**Interfaces:**
- Consumes: Task 3–6 的全部产物
- Produces: append-only AST 守卫、单一写路径 AST 守卫、`ControlledPii` 字段暴露守卫

- [ ] **Step 1: 写 append-only 与单一写路径守卫**

创建 `tests/security/test_admin_audit_append_only.py`：

```python
"""``admin_audit_events`` 的 append-only 由源码扫描机械保证。

协议窄度（``append``/``load``）挡住的是"通过 Store 改审计"。这里挡的是另一
条路：直接在 ``postgres.py`` 里对这张表发 UPDATE/DELETE。两条都堵上，
append-only 才是结构性质而不是口头约定。
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PERSISTENCE = _ROOT / "src" / "xiaowei_agent" / "persistence"

pytestmark = pytest.mark.security


def mutating_calls_on(module: Path, table_symbol: str) -> list[str]:
    """找出形如 ``sa.update(TABLE)`` / ``TABLE.delete()`` 的调用。"""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"update", "delete"}:
            if isinstance(func.value, ast.Name) and func.value.id == table_symbol:
                found.append(f"{module.name}:{node.lineno} {table_symbol}.{func.attr}()")
            for arg in node.args:
                if isinstance(arg, ast.Name) and arg.id == table_symbol:
                    found.append(
                        f"{module.name}:{node.lineno} sa.{func.attr}({table_symbol})"
                    )
    return found


@pytest.mark.parametrize(
    "module", sorted(path.name for path in _PERSISTENCE.glob("*.py"))
)
def test_no_persistence_module_updates_or_deletes_admin_audit_events(
    module: str,
) -> None:
    offenders = mutating_calls_on(_PERSISTENCE / module, "ADMIN_AUDIT_EVENTS")
    assert offenders == [], f"admin audit must be append-only: {offenders}"


def test_the_guard_actually_detects_a_mutating_call(tmp_path: Path) -> None:
    """反例：守卫自己必须能抓到它要抓的东西。

    没有这条，上面那组用例在 AST 解析写错时会静默全绿——扫不到任何调用
    和不存在任何调用，返回的都是空列表。
    """
    sample = tmp_path / "offender.py"
    sample.write_text(
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import ADMIN_AUDIT_EVENTS\n"
        "stmt = sa.update(ADMIN_AUDIT_EVENTS)\n"
        "other = ADMIN_AUDIT_EVENTS.delete()\n",
        encoding="utf-8",
    )
    assert len(mutating_calls_on(sample, "ADMIN_AUDIT_EVENTS")) == 2


def test_audit_store_offers_no_cleanup_or_retention_api() -> None:
    from xiaowei_agent.persistence.admin_audit import AdminAuditStore

    surface = {name for name in dir(AdminAuditStore) if not name.startswith("_")}
    assert surface == {"append", "load"}
```

创建 `tests/security/test_identity_write_path.py`：

```python
"""四张授权表只有 ``UserDirectoryStore.apply()`` 一个写入口。

协议上只有一个写方法，挡住的是"通过 Store 绕开审计"。这里挡的是另一条：
某个模块直接对授权表发 INSERT/UPDATE。上一版计划正是栽在这条路上——
``seed_if_absent`` 自己写了账号和 ADMIN 角色，第一次部署就违反了不变量。
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"
_AUTHZ_TABLES = frozenset(
    {"USER_ACCOUNTS", "USER_ROLE_ASSIGNMENTS", "EXTERNAL_IDENTITIES"}
)
_ALLOWED_MODULES = frozenset({"identity.py", "postgres.py"})
"""只有这两个模块可以出现授权表的写语句。

``postgres.py`` 承载 ``PostgresUserDirectoryStore``，``identity.py`` 承载内存
实现。任何第三个模块出现在结果里，都意味着又开了一个写入口。
"""

pytestmark = pytest.mark.security


def writes_to(module: Path, symbols: frozenset[str]) -> list[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"insert", "update", "delete"}:
            continue
        targets = [arg for arg in node.args if isinstance(arg, ast.Name)]
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            targets.append(func.value)
        for target in targets:
            if target.id in symbols:
                found.append(f"{module.name}:{node.lineno} {name}({target.id})")
    return found


def test_only_the_directory_store_modules_write_authorization_tables() -> None:
    offenders: list[str] = []
    for module in sorted(_SRC.rglob("*.py")):
        if module.name in _ALLOWED_MODULES:
            continue
        offenders.extend(writes_to(module, _AUTHZ_TABLES))
    assert offenders == [], f"authorization tables have a second write path: {offenders}"


def test_local_admin_module_no_longer_writes_authorization_rows() -> None:
    """``seed_if_absent`` 必须委托给 apply()，不能自己 INSERT。"""
    module = _SRC / "persistence" / "local_admin.py"
    assert writes_to(module, _AUTHZ_TABLES) == []


def test_the_guard_actually_detects_a_second_write_path(tmp_path: Path) -> None:
    sample = tmp_path / "offender.py"
    sample.write_text(
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import USER_ROLE_ASSIGNMENTS\n"
        "stmt = sa.insert(USER_ROLE_ASSIGNMENTS)\n",
        encoding="utf-8",
    )
    assert len(writes_to(sample, _AUTHZ_TABLES)) == 1
```

- [ ] **Step 2: 写 PII 暴露守卫**

创建 `tests/security/test_controlled_pii_exposure.py`：

```python
"""``ControlledPii`` 标注的字段必须同时 ``exclude`` 与 ``repr=False``。

与 ``SecretHash`` 同一套约定、同一个理由：``repr`` 进 traceback，
``model_dump`` 进日志和响应体，只堵一条等于没堵。
"""

import datetime as _dt

import pytest
from pydantic import BaseModel

from xiaowei_agent.contracts import IdentitySource
from xiaowei_agent.contracts import identity as identity_module
from xiaowei_agent.contracts.identity import (
    BindExternalIdentityCommand,
    ExternalIdentity,
)

pytestmark = pytest.mark.security

_PII_FIELD_NAMES = frozenset({"subject_ref"})


def _contract_models() -> list[type[BaseModel]]:
    return [
        value
        for value in vars(identity_module).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__module__ == identity_module.__name__
    ]


def test_every_pii_field_is_excluded_and_unrepresented() -> None:
    checked = 0
    for model in _contract_models():
        for name, field in model.model_fields.items():
            if name not in _PII_FIELD_NAMES:
                continue
            checked += 1
            assert field.exclude is True, f"{model.__name__}.{name} must be excluded"
            assert field.repr is False, f"{model.__name__}.{name} must not be repr'd"
    assert checked >= 3, "PII guard scanned nothing — the marker set is out of date"


@pytest.mark.parametrize("model", [ExternalIdentity, BindExternalIdentityCommand])
def test_pii_never_reaches_repr_or_dump(model: type[BaseModel]) -> None:
    open_id = "ou_" + "7f3c9a21b4e8"
    values: dict[str, object] = {
        "user_id": "usr-1",
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "subject_ref": open_id,
    }
    if model is ExternalIdentity:
        now = _dt.datetime(2026, 9, 21, tzinfo=_dt.UTC)
        values |= {
            "provider": IdentitySource.FEISHU,
            "created_at": now,
            "last_seen_at": now,
        }
    instance = model(**values)
    assert open_id not in repr(instance)
    assert open_id not in instance.model_dump_json()
```

`assert checked >= 3` 防的是"标记集合写错导致扫描 0 个字段却全绿"——没有它，守卫失效时是静默的。三个是 `ExternalIdentity`、`BindExternalIdentityCommand`、`LegacyIdentityMigrationEntry`。

- [ ] **Step 3: 运行 security gate**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -m security -q 2>&1 | tail -5
```

必须全绿，且新增用例数大于 0。

- [ ] **Step 4: 隔离变异反证**

```bash
cp src/xiaowei_agent/contracts/identity.py /tmp/keep-pii.py
python - <<'MUT'
from pathlib import Path
p = Path("src/xiaowei_agent/contracts/identity.py")
t = p.read_text()
t = t.replace(
    '_PII = Field(min_length=1, max_length=128, exclude=True, repr=False)',
    '_PII = Field(min_length=1, max_length=128)',
)
p.write_text(t)
MUT
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/security/test_controlled_pii_exposure.py -q 2>&1 | tail -5
cp /tmp/keep-pii.py src/xiaowei_agent/contracts/identity.py && rm /tmp/keep-pii.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -m security -q 2>&1 | tail -3
```

预期：变异后变红；还原后全绿。

- [ ] **Step 5: 提交**

```bash
git add tests/security/test_admin_audit_append_only.py \
        tests/security/test_identity_write_path.py \
        tests/security/test_controlled_pii_exposure.py
git commit -m "test(w1a): guard the single write path, append-only audit and PII exposure

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: 旧静态身份标签迁移

**Files:**
- Modify: `src/xiaowei_agent/interfaces/feishu_identity.py`
- Create: `src/xiaowei_agent/interfaces/legacy_identity_migration.py`
- Test: `tests/contract/test_legacy_identity_document.py`
- Test: `tests/contract/test_legacy_identity_migration.py`

**Interfaces:**
- Consumes: 既有 `feishu_identity.py` 的解析逻辑；Task 5 的 `apply()` 与 `MigrateLegacyIdentitiesCommand`
- Produces: `LegacyIdentityEntry`、`read_legacy_identity_document()`、`legacy_user_id()`、`LegacyMigrationReport`、`migrate_static_identities()`

**为什么要动 `feishu_identity.py`：** 当前唯一的公开读取入口 `load_feishu_identity_directory()` 返回 `StaticFeishuIdentityDirectory`，其中的 `AuthenticatedPrincipal` 已经把 labels 压成了 `frozenset[ChannelPermission]`（见 `src/xiaowei_agent/interfaces/feishu_identity.py:125`）。`viewer` 与 `approver`、`operator` 与 `dba`/`oncall` 压完之后完全一样，而规格 §6.6 的迁移表恰恰要按原始 label 分流。`_IdentityDocument` 与 `_read_identity_file` 都是私有的。因此本任务**抽出**一份公开的、保留原始 labels 的只读解析契约，让两条路径共用同一个解析器——不是第二次实现解析。

- [ ] **Step 1: 写解析契约的失败测试**

创建 `tests/contract/test_legacy_identity_document.py`：

```python
"""公开的旧身份文档解析契约；两条路径共用同一个解析器。"""

import json

import pytest

from xiaowei_agent.interfaces.feishu_identity import (
    FeishuIdentityConfigurationError,
    LegacyIdentityEntry,
    load_feishu_identity_directory,
    read_legacy_identity_document,
)

_OPEN_ID_A = "ou_" + "aaaa11112222"


def _write(tmp_path, entries):
    path = tmp_path / "identity.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": "dev-local",
                "environment_id": "dev",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_reading_preserves_the_original_labels(tmp_path) -> None:
    """规格 §6.6 按原始 label 分流。

    现有的 ``load_feishu_identity_directory()`` 把 labels 压成权限集合，
    ``viewer`` 与 ``approver``、``operator`` 与 ``dba`` 压完之后完全一样，
    迁移无法再区分它们。
    """
    path = _write(
        tmp_path, [{"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["dba"]}]
    )
    entries = read_legacy_identity_document(
        path=str(path), tenant_id="dev-local", environment_id="dev"
    )
    assert entries == (
        LegacyIdentityEntry(subject_ref=_OPEN_ID_A, actor="alice", labels=("dba",)),
    )


def test_reading_enforces_the_same_scope_check_as_before(tmp_path) -> None:
    path = _write(
        tmp_path, [{"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["viewer"]}]
    )
    with pytest.raises(FeishuIdentityConfigurationError):
        read_legacy_identity_document(
            path=str(path), tenant_id="other", environment_id="dev"
        )


def test_reading_never_echoes_the_document_on_failure(tmp_path) -> None:
    path = tmp_path / "identity.json"
    path.write_text('{"version": 1, "entries": "' + _OPEN_ID_A + '"}', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(FeishuIdentityConfigurationError) as excinfo:
        read_legacy_identity_document(
            path=str(path), tenant_id="dev-local", environment_id="dev"
        )
    assert _OPEN_ID_A not in str(excinfo.value)


def test_the_existing_directory_loader_still_behaves_identically(tmp_path) -> None:
    """抽取解析器不得改变既有调用方的行为。"""
    from xiaowei_agent.contracts import ChannelPermission

    path = _write(
        tmp_path, [{"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["dba"]}]
    )
    directory = load_feishu_identity_directory(
        path=str(path), tenant_id="dev-local", environment_id="dev"
    )
    principal = directory.resolve(subject_ref=_OPEN_ID_A)
    assert principal.actor == "alice"
    assert principal.permissions == frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
        }
    )


def test_the_public_entry_hides_the_open_id() -> None:
    entry = LegacyIdentityEntry(
        subject_ref=_OPEN_ID_A, actor="alice", labels=("viewer",)
    )
    assert _OPEN_ID_A not in repr(entry)
```

- [ ] **Step 2: 抽出公开解析器**

在 `feishu_identity.py` 里：

1. 把 `_IdentityEntry` 改名为公开的 `LegacyIdentityEntry`，字段与校验**一字不改**，只给 `subject_ref` 补 `exclude=True, repr=False`（它是受控 PII，与 Task 2 的约定一致）。
2. 新增公开函数：

```python
def read_legacy_identity_document(
    *, path: str, tenant_id: str, environment_id: str
) -> tuple[LegacyIdentityEntry, ...]:
    """读取并校验静态身份文件，返回**保留原始 labels** 的条目。

    ``load_feishu_identity_directory()`` 现在建立在它之上——两条路径共用同
    一个解析器。第二份解析迟早会在"什么算合法条目"上与这一份分叉，而这
    个文件恰好同时喂着运行期鉴权和一次性迁移。
    """
    document: _IdentityDocument | None = None
    invalid = False
    try:
        document = _IdentityDocument.model_validate_json(_read_identity_file(path))
        if document.tenant_id != tenant_id or document.environment_id != environment_id:
            invalid = True
    except (OSError, ValueError, UnicodeError, ValidationError):
        invalid = True
    if invalid or document is None:
        raise FeishuIdentityConfigurationError(
            "feishu identity configuration invalid"
        ) from None
    return document.entries
```

3. 把 `load_feishu_identity_directory()` 的函数体改为调用它，再做原来的 principal 构造。大小上限、`O_NOFOLLOW`、权限位、唯一性校验、失败不回显——一条都不改。
4. 在 `__all__` 里加 `LegacyIdentityEntry` 与 `read_legacy_identity_document`。

- [ ] **Step 3: 运行，确认解析层全绿且既有用例不回归**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_legacy_identity_document.py \
  tests/contract/test_feishu_identity.py -q 2>&1 | tail -6
```

`test_feishu_identity.py` 是既有用例，**必须全绿**——抽取不得改变行为。

- [ ] **Step 4: 写迁移的失败测试**

创建 `tests/contract/test_legacy_identity_migration.py`：

```python
"""旧静态身份标签到数据库目录的一次性、原子迁移。"""

import json

import pytest

from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    ProductRole,
)
from xiaowei_agent.interfaces.legacy_identity_migration import (
    LegacyMigrationConflictError,
    legacy_user_id,
    migrate_static_identities,
)

_OPEN_ID_A = "ou_" + "aaaa11112222"
_OPEN_ID_B = "ou_" + "bbbb33334444"
_TENANT = "dev-local"
_ENV = "dev"


def _write(tmp_path, entries):
    path = tmp_path / "identity.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": _TENANT,
                "environment_id": _ENV,
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


async def _run(path, directory):
    return await migrate_static_identities(
        document_path=str(path),
        directory=directory,
        tenant_id=_TENANT,
        environment_id=_ENV,
        actor_user_id="usr-local-admin",
        actor="admin",
    )


async def _account(directory, actor):
    return await directory.load_account(
        user_id=legacy_user_id(actor=actor), tenant_id=_TENANT, environment_id=_ENV
    )


async def test_labels_map_to_product_roles_per_spec_6_6(tmp_path, user_directory):
    path = _write(
        tmp_path,
        [
            {"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["admin"]},
            {"subject_ref": _OPEN_ID_B, "actor": "bob", "labels": ["dba"]},
        ],
    )
    report = await _run(path, user_directory)
    assert report.created == 2
    alice = await _account(user_directory, "alice")
    bob = await _account(user_directory, "bob")
    assert alice is not None and alice.assignment.role is ProductRole.ADMIN
    assert bob is not None and bob.assignment.role is ProductRole.OPERATOR


async def test_viewer_and_approver_both_become_plain_users(tmp_path, user_directory):
    """``approver`` 只进报告，不生效任何授权——R1 才有它的 ACL 真源。"""
    path = _write(
        tmp_path,
        [{"subject_ref": _OPEN_ID_A, "actor": "carol", "labels": ["approver"]}],
    )
    report = await _run(path, user_directory)
    carol = await _account(user_directory, "carol")
    assert carol is not None and carol.assignment.role is ProductRole.USER
    assert report.deferred_labels == (("carol", "approver"),)


async def test_multiple_labels_take_the_highest_role(tmp_path, user_directory):
    """同时带 ``admin`` 和 ``viewer`` 时取 ADMIN。

    取最高而不是取最低、也不是报错：旧文件里这种组合是既有事实；取最低会
    在迁移当天静默降权，让本来能进的人进不去；报错会让一条历史数据卡住整批。
    该选择记在迁移报告里，迁移后可人工复核。
    """
    path = _write(
        tmp_path,
        [{"subject_ref": _OPEN_ID_A, "actor": "dave", "labels": ["viewer", "admin"]}],
    )
    await _run(path, user_directory)
    dave = await _account(user_directory, "dave")
    assert dave is not None and dave.assignment.role is ProductRole.ADMIN


async def test_rerunning_the_migration_changes_nothing(tmp_path, user_directory):
    path = _write(
        tmp_path,
        [{"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]}],
    )
    first = await _run(path, user_directory)
    second = await _run(path, user_directory)
    assert (first.created, first.skipped) == (1, 0)
    assert (second.created, second.skipped) == (0, 1)


async def test_a_database_conflict_leaves_zero_rows_behind(tmp_path, user_directory):
    """整批原子性。

    冲突必须制造在**数据库既有事实**上。在输入文件里放两个相同 actor 是
    测不到迁移的——``_IdentityDocument`` 在解析期就会拒绝它，测到的是
    解析器。这里先单独建一个占用 ``bob`` 的账号，再让第二条撞上它。
    """
    from xiaowei_agent.contracts.admin_audit import AdminOperationContext
    from xiaowei_agent.contracts.enums import IdentitySource
    from xiaowei_agent.contracts.identity import CreateUserCommand

    await user_directory.apply(
        command=CreateUserCommand(
            user_id="usr-existing",
            actor="bob",
            display_name="Bob",
            tenant_id=_TENANT,
            environment_id=_ENV,
            role=ProductRole.USER,
        ),
        context=AdminOperationContext(
            operation_id="pre-existing",
            actor_user_id="usr-local-admin",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
        ),
    )
    path = _write(
        tmp_path,
        [
            {"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]},
            {"subject_ref": _OPEN_ID_B, "actor": "bob", "labels": ["viewer"]},
        ],
    )
    with pytest.raises(LegacyMigrationConflictError):
        await _run(path, user_directory)
    assert await _account(user_directory, "alice") is None


async def test_every_migrated_entry_writes_its_own_audit_event(
    tmp_path, user_directory, admin_audit
):
    path = _write(
        tmp_path,
        [
            {"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]},
            {"subject_ref": _OPEN_ID_B, "actor": "bob", "labels": ["viewer"]},
        ],
    )
    report = await _run(path, user_directory)
    assert len(report.audit_event_ids) == 2
    for event_id in report.audit_event_ids:
        event = await admin_audit.load(event_id=event_id)
        assert event is not None
        assert event.action is AdminAuditAction.LEGACY_IDENTITY_MIGRATED
        assert event.outcome is AdminAuditOutcome.SUCCEEDED
        assert event.effect.role is not None


async def test_derived_ids_stay_within_the_contract_bounds(tmp_path, user_directory):
    """``usr-legacy-`` + actor 会随 actor 长度无界增长。

    当前静态解析器对 actor 没有长度上限，而 ``user_id`` 契约上限是 64。
    直接拼接的话，一个长 actor 会在写库时才炸，而且是在批量中途。
    """
    long_actor = "a" * 200
    assert len(legacy_user_id(actor=long_actor)) <= 64
    path = _write(
        tmp_path,
        [{"subject_ref": _OPEN_ID_A, "actor": long_actor, "labels": ["viewer"]}],
    )
    with pytest.raises(LegacyMigrationConflictError):
        await _run(path, user_directory)


async def test_report_never_contains_a_plaintext_open_id(tmp_path, user_directory):
    path = _write(
        tmp_path,
        [{"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]}],
    )
    report = await _run(path, user_directory)
    assert _OPEN_ID_A not in repr(report)
    assert _OPEN_ID_A not in report.model_dump_json()
```

- [ ] **Step 5: 写迁移命令**

创建 `src/xiaowei_agent/interfaces/legacy_identity_migration.py`。标签映射与 ID 派生照抄：

```python
_ROLE_FOR_LABEL: Final[dict[str, ProductRole]] = {
    "admin": ProductRole.ADMIN,
    "operator": ProductRole.OPERATOR,
    "dba": ProductRole.OPERATOR,
    "oncall": ProductRole.OPERATOR,
    "viewer": ProductRole.USER,
    "approver": ProductRole.USER,
}
"""规格 §6.6 的迁移表逐行。

``approver`` 映射到 ``USER`` 而不是某个审批角色：旧 approver 标签当前没有
ACL 真源，R1 交付结果 artifact 时才决定它的映射。现在给它任何高于 USER 的
东西，都是在凭空生效一份没有消费者的授权。
"""

_ROLE_RANK: Final[dict[ProductRole, int]] = {
    ProductRole.USER: 0,
    ProductRole.OPERATOR: 1,
    ProductRole.ADMIN: 2,
}

_LEGACY_USER_ID_DOMAIN: Final[str] = "legacy-actor:v1"
_MAX_ACTOR_LENGTH: Final[int] = 64


def legacy_user_id(*, actor: str) -> str:
    """由 actor 派生的**有界**稳定 user_id。

    不直接拼 actor：静态解析器对 actor 没有长度上限，而 ``user_id`` 契约
    上限是 64。拼接的话，一个长 actor 会在写库时才炸，而且是在批量中途。
    摘要截断到 32 位，总长 11 + 32 = 43。
    """
    digest = sha256(f"{_LEGACY_USER_ID_DOMAIN}:{actor}".encode()).hexdigest()[:32]
    return f"usr-legacy-{digest}"


class LegacyMigrationConflictError(RuntimeError):
    """迁移无法整批完成；**没有任何一行被写入**。"""


class LegacyMigrationReport(Contract):
    """迁移结果；不含 ``subject_ref``。"""

    created: StrictInt = Field(ge=0)
    skipped: StrictInt = Field(ge=0)
    deferred_labels: tuple[tuple[StrictStr, StrictStr], ...]
    audit_event_ids: tuple[StrictStr, ...]
```

`migrate_static_identities()` 的结构：

1. 用 Task 8 Step 2 的 `read_legacy_identity_document()` 读入条目，拿到**原始 labels**。
2. 校验与派生：actor 超过 `_MAX_ACTOR_LENGTH` → `LegacyMigrationConflictError`；每条按 `_ROLE_RANK` 取最高角色；带 `approver` 的记进 `deferred_labels`（不改变它拿到的 `USER`）。
3. 用 `directory.load_account()` 查出**已经迁移过**的条目，记为 `skipped` 并从批次中剔除。剩余条目为空时直接返回 `created=0`。
4. 把剩余条目组装成**一个** `MigrateLegacyIdentitiesCommand`，一次 `apply()` 写完。捕获 `UserDirectoryConflictError` / `AdminAuditUnwritableError` 并转成 `LegacyMigrationConflictError`——**不做事务外预检**：预检和写入之间有时间窗，而且预检通过不等于写入成功。原子性由那一个事务给。
5. `created` 取 `len(events)`，`audit_event_ids` 取事件 id。

第 3 步的 `skipped` 查询是**报告用途**，不是正确性前提：即使它漏判，第 4 步的数据库唯一约束仍会让整批回滚。

- [ ] **Step 6: 运行并跑全量**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_legacy_identity_migration.py \
  tests/contract/test_legacy_identity_document.py -q 2>&1 | tail -6
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q 2>&1 | tail -3
ruff check . && mypy src
```

- [ ] **Step 7: 隔离变异反证**

把整批命令拆回逐条 `apply`：

```bash
cp src/xiaowei_agent/interfaces/legacy_identity_migration.py /tmp/keep-mig.py
# 手工把第 4 步改成 for entry in entries: await directory.apply(CreateUserCommand(...))
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_legacy_identity_migration.py -q 2>&1 | tail -6
cp /tmp/keep-mig.py src/xiaowei_agent/interfaces/legacy_identity_migration.py && rm /tmp/keep-mig.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_legacy_identity_migration.py -q 2>&1 | tail -3
```

预期：`test_a_database_conflict_leaves_zero_rows_behind` 变红（`alice` 留在库里）。还原后回到全绿。

- [ ] **Step 8: 提交**

```bash
git add src/xiaowei_agent/interfaces/feishu_identity.py \
        src/xiaowei_agent/interfaces/legacy_identity_migration.py \
        tests/contract/test_legacy_identity_document.py \
        tests/contract/test_legacy_identity_migration.py
git commit -m "feat(w1a): migrate legacy identity labels in one atomic command

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: 文档同步、深档自审与送审

**Files:**
- Modify: `ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`DEVELOPMENT_PLAN.md`
- Modify: `tests/contract/test_doc_fact_binding.py`

**Interfaces:**
- Consumes: Task 1–8 的全部产物与实测证据
- Produces: 同步后的真源文档、W1a 文档事实绑定、PR

- [ ] **Step 1: 写文档事实绑定测试**

在 `tests/contract/test_doc_fact_binding.py` 追加（沿用文件既有的路径常量与写法；若常量名不同，按实际名字改）：

```python
_W1A_COMPONENT_ROWS: Final[tuple[tuple[str, str], ...]] = (
    ("`UserDirectory`", "用户、角色、状态与外部身份读取"),
    ("`AdminAuditStore`", "append-only Admin 操作事件与查询"),
)


def test_architecture_records_the_w1a_write_kernel_as_implemented() -> None:
    """W1a 落地后，架构文档必须把这两个组件写成当前事实而不是未来目标。"""
    text = _ARCHITECTURE.read_text("utf-8")
    for name, duty in _W1A_COMPONENT_ROWS:
        assert name in text and duty in text


def test_architecture_states_the_single_write_path_invariant() -> None:
    """这是 W1a 的核心承诺，必须出现在稳定架构文档里。"""
    text = _ARCHITECTURE.read_text("utf-8")
    assert "唯一写入口" in text or "单一写路径" in text
    assert "审计不可写" in text


def test_truth_docs_do_not_claim_activation_or_admin_ui_exists() -> None:
    """W1a 只交付写底座。

    最容易出现的漂移正是"写内核做完了顺手把整条身份线说成做完了"——
    激活审批是 W1b、Admin 页面是 W3，两者都还不存在。
    """
    forbidden = (
        "ActivationStore",
        "IdentityActivationService",
        "web_oauth_login_contexts",
        "激活审批已实现",
        "Admin 管理中心已上线",
    )
    for name in _CURRENT_TRUTH_DOCS:
        text = (_ROOT / name).read_text("utf-8")
        for marker in forbidden:
            assert marker not in text or "W1b" in text or "未实现" in text


def test_handoff_names_w1b_as_the_only_post_merge_next_step() -> None:
    handoff = _AGENT_HANDOFF.read_text("utf-8")
    statements = _next_step_statements(handoff)
    assert statements, "handoff 必须有明确的下一步声明"
    for statement in statements:
        assert "W1b" in statement
```

- [ ] **Step 2: 运行，确认因文档未同步而失败**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_doc_fact_binding.py -q 2>&1 | tail -10
```

- [ ] **Step 3: 同步三份文档**

- `ARCHITECTURE.md`：在组件表加入 `UserDirectory` 与 `AdminAuditStore` 两行（职责/禁止逐字取自规格 §12），并写明两条不变量：「四张授权表只有 `UserDirectoryStore.apply()` 一个**唯一写入口**」与「授权改变与审计同事务，**审计不可写**即 fail-closed」。
- `DEVELOPMENT_PLAN.md`：把 W1a 标为已交付，下一项为 W1b；**不改** W4c/R1 的独立阻塞门位置。
- `AGENT_HANDOFF.md`：第 0 节改为"W1a 写内核已离线实现"，记录实现基线 SHA、四门实测数字、变异反证结论；明确写出**未覆盖**：没有激活审批、没有 Admin 页面、没有登录改造、没有真实飞书调用、没有部署与用户验收；以及**残余风险**：既有 `AuthenticatedPrincipal.subject_ref` 仍未标 PII 遮蔽（本阶段未触碰渠道投影，留待 W2）。

- [ ] **Step 4: 深档自审**

```bash
git diff --stat origin/main...HEAD
```

逐项核对：

1. diff 是否只含 Task 0 的 allowlist；
2. `grep -rn "override_action\|def apply(.*audit" src/` 必须无结果——调用方不得有任何机会指定审计；
3. `grep -rn "APIRouter\|@app\." src/xiaowei_agent/interfaces/legacy_identity_migration.py` 必须无结果；
4. `grep -rn "requester\|approver" src/` 只应命中迁移的 `_ROLE_FOR_LABEL` 与 `deferred_labels`；
5. `grep -rn "ActivationRequest\|ActivationStore\|web_oauth_login_contexts" src/` 必须无结果；
6. `python -m pytest tests/security/test_identity_write_path.py -q` 单独跑一次，确认没有第二个写入口。

- [ ] **Step 5: 跑 ADR-008 四门**

```bash
python -m pytest -q 2>&1 | tail -3
python -m pytest -m security -q 2>&1 | tail -3
ruff check .
mypy src
```

四条全绿，并与 Task 0 记录的基线数字对比，说明新增用例数。**任一条不绿就停下修，不得跳过或加 xfail。**

- [ ] **Step 6: 提交并开 PR**

```bash
git add ARCHITECTURE.md AGENT_HANDOFF.md DEVELOPMENT_PLAN.md \
        tests/contract/test_doc_fact_binding.py
git commit -m "docs(w1a): record the identity and audit write kernel as current truth

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push -u origin claude/w1a-identity-authz-audit
```

PR 描述必须分成四段：**已验证**（真实跑过的命令与尾部输出）、**只读推理**、**未覆盖**、**残余风险**。明确写出：这是离线写内核实现，**不是**激活流程、登录改造、Admin 页面、真实飞书调用、部署或用户验收。等待精确 SHA 复审与负责人合入，**不自行合并**。

---

## W1a Exit Criteria

W1a 只有同时满足以下条件才可判定完成：

- `UserDirectoryStore` 只有 `apply(command, context)` 一个写方法，签名上**没有**任何让调用方指定 action/target/outcome 的参数；内存与 PostgreSQL 两个实现都通过同一份共享套件；
- `tests/security/test_identity_write_path.py` 证明四张授权表在整个 `src/` 里只有目录 store 两个模块会写，`local_admin.py` 不在其中；
- "审计写失败 → 授权改变回滚"由**真实数据库约束**触发并在真库上验证，隔离变异证明移除同事务包裹后该用例变红；
- 集成测试有显式类型断言证明 `user_directory` / `admin_audit` 是 PostgreSQL 实现，不是根 conftest 的内存 fixture；
- 同一 `operation_id` 可写 `STARTED` + 一条终态，第二条 STARTED 与第二条终态都被拒绝；
- `role_assigned` / `user_status_changed` / `user_created` / `local_admin_bootstrapped` / `legacy_identity_migrated` 事件携带闭集 effect，且由数据库 CHECK 强制；非成功 outcome 不得携带 effect；
- `admin_audit_events` 的 append-only 有源码级机械守卫，且守卫自身有反例证明它抓得到违规；
- `AdminAuditEvent` 列集合 = 规格 §14.2 的 13 个 + `effect_role` + `effect_status`，无任何自由文本或 JSON 通道；
- 角色→`ChannelPermission`、角色+来源→`AdminCapability` 与规格 §6.2/§6.3 逐格相等，飞书 `ADMIN` 恰好少两项；`ChannelPermission` 仍精确三成员；
- `subject_ref` 在契约与数据库两侧都不以明文出现，PII 守卫有"扫描数为 0 即失败"的自证；
- 旧标签迁移是一个命令一个事务，冲突用例制造在**数据库既有事实**上，冲突后零部分落库，重跑 `created=0`，`approver` 不生效授权，超长 actor 有用例；
- `legacy_user_id()` 对任意长度 actor 都产出 ≤64 字符的稳定 ID；
- `feishu_identity.py` 的抽取没有改变既有行为：`tests/contract/test_feishu_identity.py` 全绿；
- `seed_if_absent` 的签名与返回语义不变，6 处既有 RI5 测试调用方全绿；
- `rev_0014` 与 `schema.py` 离线 DDL 一致（含两条 partial unique index），发布哈希已登记，downgrade 在有数据时要求显式破坏性授权；
- diff 只含 Task 0 的 allowlist，无 HTTP 路由、无激活载体、无 requester/approver 载体；
- ADR-008 四门全绿；
- W1a PR 经精确 SHA 复审并由**负责人合入** `main`；
- handoff 明确记录：没有激活审批、没有 Admin 页面、没有登录改造、没有真实飞书/Gemini/StarRocks 调用、没有部署、canary 或用户验收。

W1a 合入后允许的下一步只有：从最新 `main` 新建分支，重新入职，编写 **W1b 激活内核详细计划**并送审。W1b 计划未获批前不得写它的测试、migration 或源码。

---

## V2 修订记录

本计划 V1（`e7ff83fe21b3bc697209519e56fb0dec592e4fa1`）经复审判定「需修改」。四组根因与修订：

| 根因 | V1 的问题 | V2 的修订 |
| --- | --- | --- |
| **A. 审计由调用方组装，store 只做匹配校验** | `require_audit_matches_command()` 只校验 action 与 scope；不校验 target、不校验 outcome（传 `DENIED` 照样提交改动）；公开的 `override_action` 可绕过映射；`role_assigned` 不记新角色；`seed_if_absent` 是第二个写入口且完全不写审计 | 改为**派生**：调用方只给 `AdminOperationContext`，store 从命令算出 action/target/outcome/effect。删除 `require_audit_matches_command` 与 `override_action`。bootstrap 与批量迁移变成 `DirectoryCommand` 成员。新增闭集 `AdminAuditEffect` 与两条数据库 CHECK。新增 `tests/security/test_identity_write_path.py` 机械证明没有第二个写入口 |
| **B. `operation_id` 全局 UNIQUE 堵死两阶段审计** | 规格 §14.2 要求同一 operation 写 `STARTED → 终态`，全局唯一使第二条必然撞约束 | 改为两条 partial unique index（每 operation 至多一条 STARTED、至多一条终态）。回滚证明改用终态索引，仍是真实冲突。新增两条套件用例正反覆盖 |
| **C. 计划引用了不存在的既有设施** | 集成测试请求 `engine` fixture（实际只有 `migrated_engine`/`clean_database`）；只写 `bind()` 不定义同名 PostgreSQL fixture，会静默跑内存实现；公开 loader 已把 labels 压成权限集合，无法按 §6.6 分流 | Task 6 显式定义基于 `clean_database` 的两个 fixture，并加类型断言钉住实现；Task 8 从 `feishu_identity.py` 抽出公开的、保留原始 labels 的 `read_legacy_identity_document()`，两条路径共用同一解析器，`feishu_identity.py` 进 allowlist 并说明理由 |
| **D. 由外部输入派生的 ID 无界** | `usr-legacy-{actor}` 直接拼接，而静态解析器对 actor 没有长度上限，`user_id` 契约上限 64 | `legacy_user_id()` 改用域分隔摘要截断（总长 43）；`AdminOperationContext.operation_id` 上限收到 48，给批量后缀留位；新增最大长度 actor 用例 |

另外两项非阻断意见：

- **handoff 状态与 Task 0 停工条件冲突**：本计划 PR 一并更新 `AGENT_HANDOFF.md`，使 W0 已合入成为已记录事实；Task 0 Step 2 相应改写为"若仍写着 W0 进行中则说明 handoff 修订没合进来，停止报告"。
- **RED 定义与各任务期待的 `ModuleNotFoundError` 矛盾**：Global Constraints 里改为按**失败原因**区分——"被测事实尚不存在"（含新模块的 `ModuleNotFoundError`）是合格 RED，"测试自身写错"不是。
