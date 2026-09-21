# W1a 用户、权限与 Admin 审计写内核实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用持久化的用户目录、作用域角色、外部身份与本地凭据取代当前的静态 JSON 身份标签，并交付独立 append-only 的 `AdminAuditStore`，使任何授权改变都**无法**在不写成功审计的情况下提交。

**Architecture:** 产品角色（`ADMIN/OPERATOR/USER`）与认证来源（`IdentitySource`）在 `governance/product_roles.py` 里做纯函数确定性映射，角色回答"能做什么"、来源回答"从哪来"，两者不互相推导。身份事实落在四张新表，`UserDirectoryStore` **只暴露一个写方法** `apply(command, audit)`——授权改变与审计事件是同一个参数对、同一个事务，没有第二条写路径可以绕开审计。`AdminAuditStore` 只有 `append` 与 `load` 两个窄方法，没有 update/delete，事件契约里**不存在任何自由文本字段**，因此"审计里混进 Secret 或聊天正文"在类型层就不可表达。旧静态 JSON 通过一次性、可重复、先全量预检再写入的迁移命令导入，冲突时整批拒绝而不是部分落库。

**Tech Stack:** Python 3.11、Pydantic v2 `Contract` 基类、SQLAlchemy Core + Alembic（`rev_0014`）、asyncpg/PostgreSQL、pytest（含 `security` marker gate）、Ruff、mypy。不新增运行依赖、不新增进程、不新增 Compose 服务。

**Spec:** [小维 Web 运维工作台、身份激活与未来结果访问边界总体设计](../specs/2026-09-19-web-operations-console-identity-activation-design.md) Approved V0.3，§6、§12、§14.2、§15、§16.1、§16.2、§17.1 第 2 项。
**ADR:** [ADR-013 Web 产品修订（2026-09-20）](../../adr/ADR-013-m7-channel-boundary.md)。

## Baseline and Evidence Boundary

- 本计划分支：`claude/w1a-identity-authz-audit-plan`。
- 本计划基线：`origin/main@a12578cd59cfaccf3fe6702e6437502b9462c28d`（PR #61 squash 合入 W0）。
- W0 只证明**文档与 ADR 真源已收口**。它不是本阶段任何源码、迁移、部署、真实调用或用户验收的证据。
- 编写本计划前已按 `AGENTS.md` 顺序读取 `ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`，并读取规格、ADR-013 修订、`contracts/enums.py`、`contracts/channel.py`、`persistence/{schema,store,local_admin,clarification_records,postgres}.py`、`interfaces/{feishu_identity,local_admin_auth,web_auth}.py` 与 `tests/suites/`、`tests/contract/test_schema_matches_migration.py` 的现有形状。
- 基线四门（计划分支 `a12578c` 实测）：文档契约 `46 passed`。实现者必须从届时最新 `main` 重新跑全部四门取当期数字，**不得**把本行当作未来运行结果。

## Global Constraints

- **W1a 是写内核阶段，不是产品阶段。** 允许新增/修改：`src/xiaowei_agent/contracts/`、`governance/`、`persistence/`、`interfaces/legacy_identity_migration.py`、`interfaces/local_admin.py` 相关装配、`tests/`、以及 Task 9 列出的四份文档。**禁止**新增任何 HTTP 路由、页面、模板、静态 JS、Compose 服务或运行配置文件。
- **W1b/W2/W3 未授权。** 不创建 `ActivationRequest`、`ActivationStore`、`IdentityActivationService`、`web_oauth_login_contexts`、通知 Port、登录页、Admin 页面或审计查询 API。W1a 只交付它们将来要复用的写底座。
- **`ChannelPermission` 精确三成员，不扩展。** `VIEW_SAFE_TASK`、`SUBMIT_READONLY_TASK`、`ADMIN_ALL_SAFE_TASKS`。管理面能力一律进独立的 `AdminCapability`（七成员闭集）。
- **`AdminCapability` 七成员，逐字取自 ADR-013 修订**：`MANAGE_USERS`、`MANAGE_DUTY_BINDINGS`、`VIEW_ADMIN_AUDIT`、`VIEW_PRIVATE_TASK_CONTENT`、`VIEW_INTEGRATION_STATUS`、`MANAGE_INTEGRATIONS`、`RUN_CONNECTION_TESTS`。其中 `MANAGE_INTEGRATIONS` 与 `RUN_CONNECTION_TESTS` **只允许 `LOCAL_ADMIN`**。
- **授权改变与审计同事务。** `UserDirectoryStore` 只有 `apply(command, audit)` 一个写方法；审计写入失败必须使整个授权改变回滚。不允许出现"先写用户、再尽力写审计"的两步路径，也不允许 `try/except` 吞掉审计失败。
- **审计 append-only。** `AdminAuditStore` 只有 `append` 与 `load`；`persistence/` 中不得出现针对 `admin_audit_events` 的 `sa.update()` / `sa.delete()`；不提供清理、更新或删除 API。
- **审计事件不含自由文本。** 字段闭集为 §14.2 逐条：`event_id / operation_id / tenant_id / environment_id / actor_user_id / actor / auth_source / action / target_kind / target_ref_digest / outcome / reason_code / created_at`。`reason_code` 是闭集枚举，不是异常正文。不新增 `message`、`detail`、`payload` 或任何 `dict` 字段。
- **`subject_ref` 是受控 PII。** 飞书 `open_id` 在新契约上必须 `exclude=True, repr=False`，不进普通日志、trace、异常和审计正文；审计里只保存 domain-separated 摘要。
- **认证生命周期事件不进 `AdminAuditStore`。** 登录成功/失败、改密、登出走结构化安全日志；本阶段不新建第二套认证审计表。
- **不提前建无消费者的名单。** 不创建 requester/approver 表、列或枚举成员；旧 `approver` 标签只进迁移报告，不生效任何授权。DBA/值班绑定属于 W1a 之后的 W3 交付面，本阶段不建表。
- **旧静态 JSON 不双写。** 迁移成功后静态文件保留只读一个发布周期；本阶段不删除它，也不让它与数据库目录同时成为写入目标。
- **不放宽既有硬门。** SQLGuard、ToolPolicy、ApprovalGate、`ToolGateway`、终态保护、RI2 飞书 / RI3 Gemini / RI4 StarRocks / RI6 部署真实调用门全部不变。W1a 网络调用次数恒为 0。
- **Secret 纪律。** 测试夹具里的类 secret 字面量一律拆开写（`"hunter" + "2-plain"`），口令哈希字段用 `SecretHash` 标注并 `exclude=True, repr=False`。
- **不改权威测试命令。** 仍是 `python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。触及 `governance/` 必须全量跑 security gate。
- 每个任务先写会失败的测试并确认**失败原因正确**（不是 import/collection 错误），再写最小实现。不得靠删断言、放宽集合、跳过用例或加特殊分支制造全绿。
- 每个提交只暂存本任务列出的文件；不使用 `git add -A`。

## 与规格的两处有意偏离（需复审确认或否决）

这两处不是疏漏，是我读规格后的判断；实现者不得自行改变，复审者可以否决。

1. **`UserAccount` 不带 `tenant_id/environment_id`，`actor` 全局唯一。**
   规格 §6.1 的 `UserAccount` 字段表里没有作用域，作用域只出现在 `UserRoleAssignment`。§6.6 要求"在同一 `tenant_id/environment_id` 内拒绝 actor、subject 或本地账号冲突"——全局唯一的 `actor` 严格强于该要求，因此满足它。若复审认为将来必须支持"同一 actor 字符串在两个租户下是两个人"，请在本计划批准前否决本条，那会改变 Task 2/4 的主键设计。
2. **`LocalCredential.username` 本阶段不落库。**
   规格 §6.1 列出 `username`（首版固定 `admin`），但 W1a 没有任何消费者：现有 `local_admin_auth.py` 的登录只校验口令，不读用户名，而登录页属于 W2。按 AGENTS.md "不为尚未验证的未来场景提前引入"，本阶段只落 `local_admins.user_id`（它有当前消费者：把凭据挂到目录账号上，这正是 `LocalCredential` 的存在理由），`username` 留给 W2 连同登录表单一起落地。

## Review Focus

审核者应优先核对以下七点：

1. `apply(command, audit)` 是否真的**是唯一写路径**：`UserDirectoryStore` 协议上有没有第二个写方法，PostgreSQL 实现里有没有独立于该事务的 INSERT/UPDATE。
2. 审计失败回滚是否由**真实数据库约束**触发并验证，而不是 mock 抛异常；移除同事务包裹后测试是否真的变红。
3. `AdminAuditEvent` 字段闭集是否与规格 §14.2 逐字相等，有没有混进自由文本、`dict` 或异常正文通道。
4. `admin_audit_events` 的 append-only 是否有机械守卫，而不只是"我们约定不改"。
5. 角色→`ChannelPermission`、角色+来源→`AdminCapability` 的映射是否与规格 §6.2/§6.3 逐格相等，特别是**飞书 `ADMIN` 恰好少两项**这个判别性用例。
6. 旧标签迁移是否先全量预检再写入、冲突时零部分落库、重跑结果一致，`approver` 是否确实不生效授权。
7. diff 是否只含本计划列出的文件；有没有顺手加 HTTP 路由、激活表、DBA/值班表或 requester/approver 载体。

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

依次读 `AGENTS.md`、`ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`。重点确认 handoff 第 0 节是否已记录 W0 合入与"下一步是 W1a"。若 handoff 仍写着 W0 进行中，说明基线不对，**停止**并报告。

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
src/xiaowei_agent/_conformance.py
tests/**
ARCHITECTURE.md
AGENT_HANDOFF.md
DEVELOPMENT_PLAN.md
docs/adr/ADR-013-m7-channel-boundary.md
```

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

这三张表是规格 §6.2、§6.3 的逐格复制。用"整表相等"而不是逐条
`in` 断言：逐条断言只能发现少给，发现不了多给，而多给正是这里
唯一危险的方向。
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

- [ ] **Step 2: 运行，确认是"缺实现"而不是"写错 import"**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/unit/test_product_roles.py -q 2>&1 | tail -20
```

预期：`ModuleNotFoundError: No module named 'xiaowei_agent.governance.product_roles'`，以及 `ImportError: cannot import name 'AdminCapability'`。若报的是别的错（例如拼写错的 fixture），先修测试再继续——**collection error 不算 RED**。

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

预期：全部通过（含参数化共 15 个左右）。

- [ ] **Step 6: 隔离变异反证**

证明"来源约束"这条承重保护真的承重，而不是碰巧成立：

```bash
cp -r src /tmp/mutant-src && cp src/xiaowei_agent/governance/product_roles.py /tmp/keep.py
python - <<'PY'
from pathlib import Path
p = Path("src/xiaowei_agent/governance/product_roles.py")
t = p.read_text()
t = t.replace(
    "    if source is IdentitySource.LOCAL_ADMIN:\n        return frozenset(AdminCapability)\n    return frozenset(AdminCapability) - _LOCAL_ADMIN_ONLY",
    "    return frozenset(AdminCapability)",
)
p.write_text(t)
PY
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/unit/test_product_roles.py -q 2>&1 | tail -5
cp /tmp/keep.py src/xiaowei_agent/governance/product_roles.py
rm -rf /tmp/mutant-src /tmp/keep.py
```

预期：变异后 `test_feishu_administrator_is_short_exactly_the_two_config_plane_capabilities` 变红；还原后重新跑必须回到全绿。**先确认还原成功再进下一步。**

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
- Consumes: Task 1 的 `ProductRole`、`UserStatus`；既有 `Contract`、`StrictStr`、`AwareDatetime`、`IdentitySource`
- Produces: `ControlledPii`、`UserAccount`、`UserRoleAssignment`、`ExternalIdentity`、`DirectoryPrincipalFacts`、`CreateUserCommand`、`SetUserStatusCommand`、`AssignRoleCommand`、`RevokeRoleCommand`、`BindExternalIdentityCommand`、`UnbindExternalIdentityCommand`、`DirectoryCommand`

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
    CreateUserCommand,
    ExternalIdentity,
    UserAccount,
    UserRoleAssignment,
)

_NOW = _dt.datetime(2026, 9, 21, 3, 0, tzinfo=_dt.UTC)
_FAKE_OPEN_ID = "ou_" + "7f3c9a21b4e8"


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
    """时间和 user_id 由存储层盖，命令自带就等于可以伪造历史。"""
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


def test_create_user_command_carries_the_initial_scope_and_role() -> None:
    command = CreateUserCommand(
        user_id="usr-1",
        actor="alice",
        display_name="Alice",
        tenant_id="dev-local",
        environment_id="dev",
        role=ProductRole.OPERATOR,
    )
    assert command.role is ProductRole.OPERATOR


def test_bind_command_requires_a_non_empty_subject_ref() -> None:
    with pytest.raises(ValidationError):
        BindExternalIdentityCommand(
            user_id="usr-1",
            tenant_id="dev-local",
            environment_id="dev",
            subject_ref="",
        )
```

- [ ] **Step 2: 运行，确认失败原因是缺模块**

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

from xiaowei_agent.contracts.base import AwareDatetime, Contract, StrictStr
from xiaowei_agent.contracts.enums import IdentitySource, ProductRole, UserStatus

ControlledPii = StrictStr
"""受控 PII 的标记类型。

与 ``SecretHash`` 同一套约定：凡这样标注的字段必须同时写 ``exclude=True``
与 ``repr=False``，由 ``tests/security/test_controlled_pii_exposure.py`` 机械
检查。飞书 ``open_id`` 不是 secret，但它足以把一条运维记录钉到具体的人，
因此不进普通日志、trace、异常和审计正文。
"""


class UserAccount(Contract):
    """身份目录里的账号事实；作用域在角色上，不在这里。"""

    user_id: StrictStr = Field(min_length=1, max_length=64)
    actor: StrictStr = Field(min_length=1, max_length=64)
    display_name: StrictStr = Field(min_length=1, max_length=128)
    status: UserStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class UserRoleAssignment(Contract):
    """某个 tenant/environment 内的角色授予。"""

    user_id: StrictStr = Field(min_length=1, max_length=64)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    role: ProductRole
    created_by: StrictStr = Field(min_length=1, max_length=64)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ExternalIdentity(Contract):
    """外部 provider 主体到内部账号的绑定。"""

    user_id: StrictStr = Field(min_length=1, max_length=64)
    provider: Literal[IdentitySource.FEISHU]
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    subject_ref: ControlledPii = Field(
        min_length=1, max_length=128, exclude=True, repr=False
    )
    created_at: AwareDatetime
    last_seen_at: AwareDatetime


class DirectoryPrincipalFacts(Contract):
    """构造一个 principal 所需的全部目录事实。"""

    account: UserAccount
    assignment: UserRoleAssignment


class CreateUserCommand(Contract):
    """建号并在同一作用域授予初始角色。"""

    kind: Literal["create_user"] = "create_user"
    user_id: StrictStr = Field(min_length=1, max_length=64)
    actor: StrictStr = Field(min_length=1, max_length=64)
    display_name: StrictStr = Field(min_length=1, max_length=128)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    role: ProductRole


class SetUserStatusCommand(Contract):
    kind: Literal["set_user_status"] = "set_user_status"
    user_id: StrictStr = Field(min_length=1, max_length=64)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    status: UserStatus


class AssignRoleCommand(Contract):
    kind: Literal["assign_role"] = "assign_role"
    user_id: StrictStr = Field(min_length=1, max_length=64)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    role: ProductRole


class RevokeRoleCommand(Contract):
    kind: Literal["revoke_role"] = "revoke_role"
    user_id: StrictStr = Field(min_length=1, max_length=64)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)


class BindExternalIdentityCommand(Contract):
    kind: Literal["bind_external_identity"] = "bind_external_identity"
    user_id: StrictStr = Field(min_length=1, max_length=64)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    subject_ref: ControlledPii = Field(
        min_length=1, max_length=128, exclude=True, repr=False
    )


class UnbindExternalIdentityCommand(Contract):
    kind: Literal["unbind_external_identity"] = "unbind_external_identity"
    user_id: StrictStr = Field(min_length=1, max_length=64)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)


DirectoryCommand = Annotated[
    CreateUserCommand
    | SetUserStatusCommand
    | AssignRoleCommand
    | RevokeRoleCommand
    | BindExternalIdentityCommand
    | UnbindExternalIdentityCommand,
    Field(discriminator="kind"),
]
"""写命令闭集。新增成员必须同时在 ``_ACTION_FOR_COMMAND`` 里给出对应审计
动作，否则 ``tests/contract/test_identity_store.py`` 的完备性用例会变红。"""


__all__ = [
    "AssignRoleCommand",
    "BindExternalIdentityCommand",
    "ControlledPii",
    "CreateUserCommand",
    "DirectoryCommand",
    "DirectoryPrincipalFacts",
    "ExternalIdentity",
    "RevokeRoleCommand",
    "SetUserStatusCommand",
    "UnbindExternalIdentityCommand",
    "UserAccount",
    "UserRoleAssignment",
]
```

- [ ] **Step 4: 在 `contracts/__init__.py` 导出**

按该文件现有的字母序惯例，把 `UserAccount`、`UserRoleAssignment`、`ExternalIdentity`、`DirectoryPrincipalFacts`、`ControlledPii` 与六个命令加入 import 与 `__all__`。**不要**导出 `DirectoryCommand` 之外的内部别名。

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

### Task 3: Admin 审计事件契约与 target digest

**Files:**
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Create: `src/xiaowei_agent/contracts/admin_audit.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Test: `tests/contract/test_admin_audit_contracts.py`

**Interfaces:**
- Consumes: `IdentitySource`、`Contract`、`Sha256Hex`
- Produces: `AdminAuditAction`、`AdminAuditTargetKind`、`AdminAuditOutcome`、`AdminAuditReasonCode`、`admin_audit_target_digest()`、`AdminAuditCandidate`、`AdminAuditEvent`

- [ ] **Step 1: 写失败测试**

创建 `tests/contract/test_admin_audit_contracts.py`：

```python
"""Admin 审计事件的字段闭集、摘要域隔离与 reason_code 语义。"""

import datetime as _dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import IdentitySource
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditEvent,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
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
        "outcome": AdminAuditOutcome.SUCCEEDED,
    }
    return AdminAuditCandidate(**(values | updates))


def test_audit_candidate_fields_are_exactly_the_spec_14_2_set() -> None:
    """规格 §14.2 的字段表逐条。

    这条断言存在的唯一理由是挡住新增字段：任何 ``message``、``detail``、
    ``payload`` 或 ``dict`` 字段都会立刻把审计表变成正文/Secret 的旁路。
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
    }


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
        "legacy_identity_migrated",
    }


def test_authentication_lifecycle_never_became_an_audit_action() -> None:
    """登录成功/失败、改密、登出走结构化安全日志，不进这张表（规格 §14.2）。

    它们是认证生命周期事件，不是 Admin 对业务对象的操作。混进来会有两个
    后果：审计表变成高频写入的登录日志，而"这张表里的每一行都是一次管理
    动作"这个前提——W3 的审计界面正是按它设计的——就不再成立。
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


def test_audit_target_and_external_subject_digests_use_different_domains() -> None:
    """同一个 open_id 在两张表里必须算出不同摘要。

    同域的话，拿到 ``external_identities`` 的一行就能在审计表里反查出针对这个
    人的全部管理动作——两张表各自脱敏，合起来却不脱敏。
    """
    from xiaowei_agent.contracts import IdentitySource
    from xiaowei_agent.persistence.identity import external_subject_digest

    open_id = "ou_" + "7f3c9a21b4e8"
    assert admin_audit_target_digest(
        target_kind=AdminAuditTargetKind.USER, target_ref=open_id
    ) != external_subject_digest(
        provider=IdentitySource.FEISHU,
        tenant_id="dev-local",
        environment_id="dev",
        subject_ref=open_id,
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
    assert _candidate().reason_code is None
    with pytest.raises(ValidationError):
        _candidate(reason_code=AdminAuditReasonCode.ACTOR_NOT_ADMIN)
    denied = _candidate(
        outcome=AdminAuditOutcome.DENIED,
        reason_code=AdminAuditReasonCode.ACTOR_NOT_ADMIN,
    )
    assert denied.reason_code is AdminAuditReasonCode.ACTOR_NOT_ADMIN
    with pytest.raises(ValidationError):
        _candidate(outcome=AdminAuditOutcome.DENIED)


def test_candidate_cannot_stamp_event_id_or_time() -> None:
    with pytest.raises(ValidationError):
        _candidate(event_id="evt-1")
    with pytest.raises(ValidationError):
        _candidate(created_at=_NOW)
```

- [ ] **Step 2: 运行，确认失败原因是缺模块**

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
"""Admin 审计事件契约与 target 摘要。"""

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
)

_TARGET_DIGEST_DOMAIN: Final[str] = "admin-audit-target:v1"

_NEGATIVE_OUTCOMES: Final[frozenset[AdminAuditOutcome]] = frozenset(
    {AdminAuditOutcome.DENIED, AdminAuditOutcome.FAILED}
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


class AdminAuditCandidate(Contract):
    """调用方提供的审计事实；不含 event_id 与时间。

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

    @model_validator(mode="after")
    def _reason_code_matches_outcome(self) -> Self:
        if self.outcome in _NEGATIVE_OUTCOMES and self.reason_code is None:
            raise ValueError("denied and failed outcomes require a reason code")
        if self.outcome not in _NEGATIVE_OUTCOMES and self.reason_code is not None:
            raise ValueError("successful outcomes must not carry a reason code")
        return self


class AdminAuditEvent(AdminAuditCandidate):
    """已持久化的审计事件；append-only，永不更新或删除。"""

    event_id: StrictStr = Field(min_length=1, max_length=64)
    created_at: AwareDatetime


__all__ = [
    "AdminAuditCandidate",
    "AdminAuditEvent",
    "admin_audit_target_digest",
]
```

`test_audit_target_and_external_subject_digests_use_different_domains` 依赖 Task 5 才
创建的 `persistence/identity.py`。本任务先把它写下但标 `@pytest.mark.xfail(raises=
ImportError, strict=True)`，**Task 5 Step 6 收尾时删掉该标记并确认它真的变绿**——
`strict=True` 保证"提前实现了"也会失败，不会被漏掉。

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
git commit -m "feat(w1a): add append-only admin audit event contracts

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


def test_account_and_assignment_reference_the_account_by_foreign_key() -> None:
    for table in (USER_ROLE_ASSIGNMENTS, EXTERNAL_IDENTITIES):
        targets = {
            key.column.table.name for key in table.foreign_key_constraints
        }
        assert "user_accounts" in targets


def test_local_credential_is_linked_to_a_directory_account() -> None:
    assert "user_id" in {column.name for column in LOCAL_ADMINS.columns}
    assert "user_accounts" in {
        key.column.table.name for key in LOCAL_ADMINS.foreign_key_constraints
    }


def test_audit_operation_id_is_unique_so_one_operation_writes_one_event() -> None:
    assert frozenset({"operation_id"}) in _unique_column_sets(ADMIN_AUDIT_EVENTS)


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
        "created_at",
    }


def test_user_status_and_role_are_checked_at_the_database_level() -> None:
    assert "ck_user_accounts_status_closed" in _check_names(USER_ACCOUNTS)
    assert "ck_user_role_assignments_role_closed" in _check_names(
        USER_ROLE_ASSIGNMENTS
    )
```

- [ ] **Step 2: 运行，确认失败原因是缺表**

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
    sa.Column("operation_id", sa.Text, nullable=False, unique=True),
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
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint(
        "auth_source IN ('feishu', 'local_admin')",
        name="ck_admin_audit_events_auth_source_closed",
    ),
    sa.CheckConstraint(
        "action IN ('user_created', 'user_status_changed', 'role_assigned', "
        "'role_revoked', 'external_identity_bound', 'external_identity_unbound', "
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
    sa.Index("ix_admin_audit_events_created_at", "created_at"),
)
"""append-only 管理面审计。

``operation_id`` UNIQUE 是承重的：它既表达"一次操作只留一条事件"，也是
Task 6 证明"审计写失败则授权改变回滚"时用来制造真实冲突的那个约束。
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

并在该表 docstring 末尾补一句：`user_id` 可空是为了兼容 `rev_0014` 之前已 seed 的库；seed 路径在同一事务里建账号并回填它。

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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("operation_id", name="uq_admin_audit_events_operation_id"),
        sa.CheckConstraint(
            "auth_source IN ('feishu', 'local_admin')",
            name="ck_admin_audit_events_auth_source_closed",
        ),
        sa.CheckConstraint(
            "action IN ('user_created', 'user_status_changed', 'role_assigned', "
            "'role_revoked', 'external_identity_bound', "
            "'external_identity_unbound', 'legacy_identity_migrated')",
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
    )
    op.create_index(
        "ix_admin_audit_events_created_at", "admin_audit_events", ["created_at"]
    )
    op.add_column(
        "local_admins", sa.Column("user_id", sa.Text(), nullable=True)
    )
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
        bind.execute(sa.select(sa.literal(1)).select_from(_USER_ACCOUNTS).limit(1)).first()
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
    op.drop_table("admin_audit_events")
    op.drop_table("external_identities")
    op.drop_table("user_role_assignments")
    op.drop_table("user_accounts")
```

先读 `src/xiaowei_agent/persistence/migrations/guards.py` 确认 `require_destructive_authorization` 的确切签名，按它的既有形状调用，**不要**改它。

- [ ] **Step 5: 登记发布哈希并跑离线一致性**

```bash
python - <<'PY'
import hashlib, pathlib
p = pathlib.Path(
    "src/xiaowei_agent/persistence/migrations/versions/"
    "rev_0014_identity_directory_and_admin_audit.py"
)
print(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
PY
```

把结果加进 `tests/contract/test_schema_matches_migration.py` 的 `_PUBLISHED_REVISION_SOURCE_SHA256`。

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_schema.py \
  tests/contract/test_schema_matches_migration.py tests/contract/test_migrate_entry.py -q 2>&1 | tail -8
```

预期：全绿。若 `test_schema_matches_migration.py` 报 DDL 不一致，说明 `schema.py` 与迁移写的不是同一套表——**改到两边一致，不要改断言**。

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

### Task 5: 内存实现与"授权改变即审计"的单一写路径

这是整个 W1a 的承重任务。

**Files:**
- Create: `src/xiaowei_agent/persistence/identity.py`
- Create: `src/xiaowei_agent/persistence/admin_audit.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Create: `tests/suites/identity_directory.py`
- Create: `tests/contract/test_identity_store.py`

**Interfaces:**
- Consumes: Task 2/3 的契约；既有 `InMemoryPersistenceState`、`Clock`
- Produces: `UserDirectoryStore` Protocol（`load_account(user_id, tenant_id, environment_id)` / `resolve_by_subject` / `apply`）、`AdminAuditStore` Protocol（`append` / `load`）、`InMemoryUserDirectoryStore`、`InMemoryAdminAuditStore`、`ACTION_FOR_COMMAND`、`IDENTITY_DIRECTORY_CASES`

- [ ] **Step 1: 写共享行为套件**

创建 `tests/suites/identity_directory.py`。这些用例后面会被内存和 PostgreSQL 两个绑定各跑一遍：

```python
"""UserDirectoryStore 的内存/PostgreSQL 共享行为套件。"""

from __future__ import annotations

from typing import Any

import pytest
from tests.suites.task_store import bind

from xiaowei_agent.contracts import IdentitySource
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    admin_audit_target_digest,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditTargetKind,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    CreateUserCommand,
    SetUserStatusCommand,
)
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryConflictError,
    UserDirectoryScopeError,
)

__all__ = ["IDENTITY_DIRECTORY_CASES", "bind"]

_TENANT = "dev-local"
_ENV = "dev"
_FAKE_OPEN_ID = "ou_" + "7f3c9a21b4e8"


def _audit(
    action: AdminAuditAction,
    *,
    operation_id: str,
    target_ref: str,
    outcome: AdminAuditOutcome = AdminAuditOutcome.SUCCEEDED,
    tenant_id: str = _TENANT,
    environment_id: str = _ENV,
) -> AdminAuditCandidate:
    return AdminAuditCandidate(
        operation_id=operation_id,
        tenant_id=tenant_id,
        environment_id=environment_id,
        actor_user_id="usr-admin",
        actor="admin",
        auth_source=IdentitySource.LOCAL_ADMIN,
        action=action,
        target_kind=AdminAuditTargetKind.USER,
        target_ref_digest=admin_audit_target_digest(
            target_kind=AdminAuditTargetKind.USER, target_ref=target_ref
        ),
        outcome=outcome,
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


async def test_creating_a_user_persists_the_account_and_its_audit_event(
    user_directory: Any, admin_audit: Any
) -> None:
    event = await user_directory.apply(
        command=_create(),
        audit=_audit(
            AdminAuditAction.USER_CREATED, operation_id="op-1", target_ref="usr-1"
        ),
    )
    facts = await user_directory.load_account(user_id="usr-1", tenant_id=_TENANT, environment_id=_ENV)
    assert facts is not None
    assert facts.account.actor == "alice"
    assert facts.assignment.role is ProductRole.OPERATOR
    stored = await admin_audit.load(event_id=event.event_id)
    assert stored == event


async def test_audit_write_failure_rolls_back_the_authorization_change(
    user_directory: Any, admin_audit: Any
) -> None:
    """承重用例。

    ``operation_id`` 唯一，因此复用同一个 operation_id 会让审计 INSERT 在
    数据库层失败。这是真实约束冲突，不是 mock 抛异常——后者只能证明我们
    的 except 分支写对了，证明不了两条写在同一个事务里。
    """
    await user_directory.apply(
        command=_create(),
        audit=_audit(
            AdminAuditAction.USER_CREATED, operation_id="op-1", target_ref="usr-1"
        ),
    )
    with pytest.raises(AdminAuditUnwritableError):
        await user_directory.apply(
            command=_create("usr-2", actor="bob"),
            audit=_audit(
                AdminAuditAction.USER_CREATED,
                operation_id="op-1",
                target_ref="usr-2",
            ),
        )
    assert await user_directory.load_account(user_id="usr-2", tenant_id=_TENANT, environment_id=_ENV) is None


async def test_audit_scope_must_match_the_command_scope(user_directory: Any) -> None:
    """审计写的是另一个租户，就等于这次改动在本租户没有记录。"""
    with pytest.raises(UserDirectoryScopeError):
        await user_directory.apply(
            command=_create(),
            audit=_audit(
                AdminAuditAction.USER_CREATED,
                operation_id="op-2",
                target_ref="usr-1",
                tenant_id="other-tenant",
            ),
        )
    assert await user_directory.load_account(user_id="usr-1", tenant_id=_TENANT, environment_id=_ENV) is None


async def test_audit_action_must_match_the_command_kind(user_directory: Any) -> None:
    """否则可以一边撤角色、一边记一条 ``user_created``。"""
    with pytest.raises(UserDirectoryScopeError):
        await user_directory.apply(
            command=_create(),
            audit=_audit(
                AdminAuditAction.ROLE_REVOKED, operation_id="op-3", target_ref="usr-1"
            ),
        )


async def test_two_accounts_cannot_claim_the_same_actor(user_directory: Any) -> None:
    await user_directory.apply(
        command=_create(),
        audit=_audit(
            AdminAuditAction.USER_CREATED, operation_id="op-4", target_ref="usr-1"
        ),
    )
    with pytest.raises(UserDirectoryConflictError):
        await user_directory.apply(
            command=_create("usr-9", actor="alice"),
            audit=_audit(
                AdminAuditAction.USER_CREATED, operation_id="op-5", target_ref="usr-9"
            ),
        )


async def test_one_subject_ref_cannot_bind_two_accounts(user_directory: Any) -> None:
    for index, (user_id, actor) in enumerate((("usr-1", "alice"), ("usr-2", "bob"))):
        await user_directory.apply(
            command=_create(user_id, actor=actor),
            audit=_audit(
                AdminAuditAction.USER_CREATED,
                operation_id=f"op-c{index}",
                target_ref=user_id,
            ),
        )
    await user_directory.apply(
        command=BindExternalIdentityCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_FAKE_OPEN_ID,
        ),
        audit=_audit(
            AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
            operation_id="op-b1",
            target_ref="usr-1",
        ),
    )
    with pytest.raises(UserDirectoryConflictError):
        await user_directory.apply(
            command=BindExternalIdentityCommand(
                user_id="usr-2",
                tenant_id=_TENANT,
                environment_id=_ENV,
                subject_ref=_FAKE_OPEN_ID,
            ),
            audit=_audit(
                AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
                operation_id="op-b2",
                target_ref="usr-2",
            ),
        )


async def test_disabled_account_stops_resolving_on_the_next_request(
    user_directory: Any,
) -> None:
    """规格 §15.12：禁用必须在后续请求实时生效。"""
    await user_directory.apply(
        command=_create(),
        audit=_audit(
            AdminAuditAction.USER_CREATED, operation_id="op-6", target_ref="usr-1"
        ),
    )
    await user_directory.apply(
        command=BindExternalIdentityCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_FAKE_OPEN_ID,
        ),
        audit=_audit(
            AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
            operation_id="op-7",
            target_ref="usr-1",
        ),
    )
    assert (
        await user_directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_FAKE_OPEN_ID,
        )
    ) is not None
    await user_directory.apply(
        command=SetUserStatusCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            status=UserStatus.DISABLED,
        ),
        audit=_audit(
            AdminAuditAction.USER_STATUS_CHANGED,
            operation_id="op-8",
            target_ref="usr-1",
        ),
    )
    assert (
        await user_directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_FAKE_OPEN_ID,
        )
    ) is None


async def test_role_change_takes_effect_and_is_audited(
    user_directory: Any, admin_audit: Any
) -> None:
    await user_directory.apply(
        command=_create(),
        audit=_audit(
            AdminAuditAction.USER_CREATED, operation_id="op-9", target_ref="usr-1"
        ),
    )
    event = await user_directory.apply(
        command=AssignRoleCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            role=ProductRole.ADMIN,
        ),
        audit=_audit(
            AdminAuditAction.ROLE_ASSIGNED, operation_id="op-10", target_ref="usr-1"
        ),
    )
    facts = await user_directory.load_account(user_id="usr-1", tenant_id=_TENANT, environment_id=_ENV)
    assert facts is not None
    assert facts.assignment.role is ProductRole.ADMIN
    assert (await admin_audit.load(event_id=event.event_id)) is not None


async def test_a_binding_in_one_scope_does_not_resolve_in_another(
    user_directory: Any,
) -> None:
    """绑定是按作用域的（规格 §16.1「跨租户/环境冲突」）。

    ``external_identities`` 的主键含 tenant/environment，因此同一个 open_id
    在另一个环境里查不到。这条用例钉住的是**默认拒绝**的方向：如果将来有人
    把主键收窄成只有 subject_ref，一个测试环境的绑定就会在生产环境直接生效。
    """
    await user_directory.apply(
        command=_create(),
        audit=_audit(
            AdminAuditAction.USER_CREATED, operation_id="op-x1", target_ref="usr-1"
        ),
    )
    await user_directory.apply(
        command=BindExternalIdentityCommand(
            user_id="usr-1",
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_FAKE_OPEN_ID,
        ),
        audit=_audit(
            AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
            operation_id="op-x2",
            target_ref="usr-1",
        ),
    )
    assert (
        await user_directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=_TENANT,
            environment_id="staging",
            subject_ref=_FAKE_OPEN_ID,
        )
    ) is None
    assert (
        await user_directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id="other-tenant",
            environment_id=_ENV,
            subject_ref=_FAKE_OPEN_ID,
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


IDENTITY_DIRECTORY_CASES = (
    test_creating_a_user_persists_the_account_and_its_audit_event,
    test_audit_write_failure_rolls_back_the_authorization_change,
    test_audit_scope_must_match_the_command_scope,
    test_audit_action_must_match_the_command_kind,
    test_two_accounts_cannot_claim_the_same_actor,
    test_one_subject_ref_cannot_bind_two_accounts,
    test_disabled_account_stops_resolving_on_the_next_request,
    test_role_change_takes_effect_and_is_audited,
    test_a_binding_in_one_scope_does_not_resolve_in_another,
    test_unknown_subject_resolves_to_nothing,
)

ALL_GROUPS = {"identity_directory": IDENTITY_DIRECTORY_CASES}
```

创建 `tests/contract/test_identity_store.py`：

```python
"""UserDirectoryStore 内存绑定、协议窄度与命令/动作完备性。"""

from tests.suites.identity_directory import IDENTITY_DIRECTORY_CASES, bind

from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    CreateUserCommand,
    RevokeRoleCommand,
    SetUserStatusCommand,
    UnbindExternalIdentityCommand,
)
from xiaowei_agent.persistence.admin_audit import AdminAuditStore
from xiaowei_agent.persistence.identity import ACTION_FOR_COMMAND, UserDirectoryStore

bind(globals(), IDENTITY_DIRECTORY_CASES)


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


def test_admin_audit_store_is_append_only_by_shape() -> None:
    assert {name for name in dir(AdminAuditStore) if not name.startswith("_")} == {
        "append",
        "load",
    }


def test_every_command_has_a_declared_audit_action() -> None:
    """新增命令而忘了声明动作，必须炸在这里。"""
    assert set(ACTION_FOR_COMMAND) == {
        CreateUserCommand,
        SetUserStatusCommand,
        AssignRoleCommand,
        RevokeRoleCommand,
        BindExternalIdentityCommand,
        UnbindExternalIdentityCommand,
    }
```

- [ ] **Step 2: 运行，确认失败原因是缺模块**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py -q 2>&1 | tail -10
```

预期：`ModuleNotFoundError: No module named 'xiaowei_agent.persistence.identity'`。

- [ ] **Step 3: 写 `persistence/admin_audit.py`**

```python
"""append-only 管理面审计的存储契约与内存实现。"""

from __future__ import annotations

from typing import Protocol

from xiaowei_agent.contracts.admin_audit import AdminAuditCandidate, AdminAuditEvent


class AdminAuditError(RuntimeError):
    """审计存储错误；不把异常正文、Secret 或 PII 放进消息。"""


class AdminAuditConflictError(AdminAuditError):
    """同一个 ``operation_id`` 已经有事件。"""


class AdminAuditStore(Protocol):
    """只有追加与读取。

    **没有 update、没有 delete、没有清理。** 规格 §14.2 明确要求首版不提供
    更新/删除 API；能删的审计在事后争议里等于没有审计。
    """

    async def append(self, *, candidate: AdminAuditCandidate) -> AdminAuditEvent: ...

    async def load(self, *, event_id: str) -> AdminAuditEvent | None: ...


__all__ = [
    "AdminAuditConflictError",
    "AdminAuditError",
    "AdminAuditStore",
]
```

- [ ] **Step 4: 写 `persistence/identity.py`**

```python
"""身份目录的存储契约与内存实现。

**唯一的写方法是 ``apply(command, audit)``。** 授权改变和它的审计事件是同
一次调用的两个必填参数，落在同一个事务里；不存在"先改权限、再尽力写审计"
的路径，因此也不存在审计缺失的中间态。
"""

from __future__ import annotations

import uuid
from hashlib import sha256
from typing import Final, Protocol

from xiaowei_agent.contracts.admin_audit import AdminAuditCandidate, AdminAuditEvent
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    AssignRoleCommand,
    BindExternalIdentityCommand,
    CreateUserCommand,
    DirectoryCommand,
    DirectoryPrincipalFacts,
    RevokeRoleCommand,
    SetUserStatusCommand,
    UnbindExternalIdentityCommand,
    UserAccount,
    UserRoleAssignment,
)
from xiaowei_agent.persistence.memory import InMemoryPersistenceState
from xiaowei_agent.persistence.store import Clock


class UserDirectoryError(RuntimeError):
    """身份目录写入错误；消息里不含 ``subject_ref``、口令或异常正文。"""


class UserDirectoryConflictError(UserDirectoryError):
    """actor、subject 或账号绑定与既有事实冲突。"""


class UserDirectoryNotFoundError(UserDirectoryError):
    """命令的目标账号不存在。"""


class UserDirectoryScopeError(UserDirectoryError):
    """审计事件与命令的作用域或动作不一致。"""


class AdminAuditUnwritableError(UserDirectoryError):
    """审计写入失败；授权改变必须随之回滚。"""


ACTION_FOR_COMMAND: Final[dict[type, AdminAuditAction]] = {
    CreateUserCommand: AdminAuditAction.USER_CREATED,
    SetUserStatusCommand: AdminAuditAction.USER_STATUS_CHANGED,
    AssignRoleCommand: AdminAuditAction.ROLE_ASSIGNED,
    RevokeRoleCommand: AdminAuditAction.ROLE_REVOKED,
    BindExternalIdentityCommand: AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
    UnbindExternalIdentityCommand: AdminAuditAction.EXTERNAL_IDENTITY_UNBOUND,
}
"""命令类型到它**必须**记录的审计动作。

绑死而不是让调用方自己传：调用方自由选择动作，就可以一边撤销角色、一边
记一条 ``user_created``，审计从此只是一段与事实无关的文本。
``LEGACY_IDENTITY_MIGRATED`` 不在表里——它由 Task 8 的迁移命令显式覆盖，见
``apply`` 的 ``override_action`` 参数。
"""


def require_audit_matches_command(
    command: DirectoryCommand,
    audit: AdminAuditCandidate,
    *,
    override_action: AdminAuditAction | None = None,
) -> None:
    """共享内存/PostgreSQL 的审计一致性校验。"""
    expected = override_action or ACTION_FOR_COMMAND[type(command)]
    if audit.action is not expected:
        raise UserDirectoryScopeError("audit action does not match the command")
    if (
        audit.tenant_id != command.tenant_id
        or audit.environment_id != command.environment_id
    ):
        raise UserDirectoryScopeError("audit scope does not match the command")


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
        self,
        *,
        command: DirectoryCommand,
        audit: AdminAuditCandidate,
        override_action: AdminAuditAction | None = None,
    ) -> AdminAuditEvent: ...
```

同文件继续写摘要函数与内存实现：

```python
_EXTERNAL_SUBJECT_DOMAIN: Final[str] = "external-subject:v1"


def external_subject_digest(
    *, provider: IdentitySource, tenant_id: str, environment_id: str, subject_ref: str
) -> str:
    """外部主体的存储摘要；与审计 target 摘要**不同域**。

    同域会让"某个 open_id 的绑定记录"和"针对该用户的审计事件"算出相等的
    摘要，于是拿到一张表就能反查另一张表的关联。
    """
    payload = (
        f"{_EXTERNAL_SUBJECT_DOMAIN}:{provider.value}:"
        f"{tenant_id}:{environment_id}:{subject_ref}"
    )
    return sha256(payload.encode()).hexdigest()


class InMemoryUserDirectoryStore:
    """与 TaskStore 共用同一把锁和同一份 state 的单进程实现。

    共用是必须的：如果目录和审计各自持有 state，"同事务"在内存实现上就会
    无条件成立，共享套件也就证明不了任何东西。
    """

    def __init__(self, *, state: InMemoryPersistenceState, clock: Clock) -> None:
        self._state = state
        self._clock = clock

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
        self,
        *,
        command: DirectoryCommand,
        audit: AdminAuditCandidate,
        override_action: AdminAuditAction | None = None,
    ) -> AdminAuditEvent:
        require_audit_matches_command(command, audit, override_action=override_action)
        async with self._state.lock:
            # 全部校验先做完；任何一条不过，就在改动**任何** dict 之前抛出。
            self._reject_conflicts(command)
            if audit.operation_id in self._state.admin_audit_operation_ids:
                raise AdminAuditUnwritableError("audit operation id already used")
            now = self._clock()
            event = AdminAuditEvent(
                **audit.model_dump(mode="python"),
                event_id=uuid.uuid4().hex,
                created_at=now,
            )
            self._mutate(command, now=now, actor=audit.actor)
            self._state.admin_audit_operation_ids.add(audit.operation_id)
            self._state.admin_audit_events[event.event_id] = event
            return event
```

`_facts`、`_reject_conflicts` 与 `_mutate` 是三个同步私有方法，只在已持锁时调用：

- `_facts(user_id, tenant_id, environment_id)` 取 `user_accounts[user_id]` 与
  `user_role_assignments[(user_id, tenant_id, environment_id)]`，任一缺失返回 `None`。
- `_reject_conflicts(command)` 按命令类型分流：`CreateUserCommand` 时 `user_id` 已存在或
  `actor` 已被别的账号占用 → `UserDirectoryConflictError`；其余命令的目标账号不存在 →
  `UserDirectoryNotFoundError`；`BindExternalIdentityCommand` 时该摘要已绑到别的账号，或该
  账号在本作用域已有绑定 → `UserDirectoryConflictError`。
- `_mutate(command, *, now, actor)` 按命令类型改对应的 dict；时间一律用传入的 `now`，
  `created_by` 用传入的 `actor`，**不在方法内再读一次时钟**——两次读会让账号与审计的
  时间戳对不上。

`InMemoryAdminAuditStore` 写在 `admin_audit.py`，同样持 `state.lock`：`append` 校验
`operation_id` 未用过后写入并返回事件，`load` 直接查 `admin_audit_events`。

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
    admin_audit_operation_ids: set[str] = field(default_factory=set)
```

`external_identities` 的键是 `(provider, tenant_id, environment_id, subject_ref_digest)`,
与 `external_identities` 表的主键**逐列相同**——键形状不一致，共享套件就会在两个实现上
测出不同的冲突语义。

- [ ] **Step 5: 补 conftest fixture**

在 `tests/conftest.py` 里按现有 store fixture 的写法加 `user_directory` 与 `admin_audit` 两个 fixture，两者共享同一个 `InMemoryPersistenceState`——**共享是必须的**，分开的 state 会让"同事务"用例在内存实现上无条件通过。

- [ ] **Step 6: 解除 Task 3 的 xfail 并确认全绿**

`persistence/identity.py` 现在存在了，删掉 Task 3 里那条 `@pytest.mark.xfail`：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_admin_audit_contracts.py \
  -q -k digests_use_different_domains 2>&1 | tail -5
```

先确认它以 `xpassed`/`strict` 失败的形式提醒你该删标记，删掉后再跑：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py \
  tests/contract/test_admin_audit_contracts.py -q 2>&1 | tail -6
ruff check . && mypy src
```

- [ ] **Step 7: 隔离变异反证**

把 `apply` 里的审计追加移到锁块**之外**（模拟"先改权限、再写审计"）：

```bash
cp src/xiaowei_agent/persistence/identity.py /tmp/keep-identity.py
python - <<'MUT'
from pathlib import Path
p = Path("src/xiaowei_agent/persistence/identity.py")
t = p.read_text()
# 删掉 operation_id 冲突检查：等价于"审计写不进去也照样改权限"
start = t.index("            if audit.operation_id in")
end = t.index("\n", t.index("raise AdminAuditUnwritableError", start)) + 1
p.write_text(t[:start] + t[end:])
MUT
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py -q 2>&1 | tail -6
cp /tmp/keep-identity.py src/xiaowei_agent/persistence/identity.py
rm /tmp/keep-identity.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py -q 2>&1 | tail -3
```

预期：变异后 `test_audit_write_failure_rolls_back_the_authorization_change` 变红；还原后回到全绿。**先确认还原成功再提交。**

- [ ] **Step 8: 提交**

```bash
git add src/xiaowei_agent/persistence/identity.py \
        src/xiaowei_agent/persistence/admin_audit.py \
        src/xiaowei_agent/persistence/memory.py \
        tests/suites/identity_directory.py \
        tests/contract/test_identity_store.py \
        tests/contract/test_admin_audit_contracts.py \
        tests/conftest.py
git commit -m "feat(w1a): couple every authorization change to its audit event

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: PostgreSQL 实现与真实同事务证明

**Files:**
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/local_admin.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Create: `tests/integration/test_identity_directory_postgres.py`
- Test: `tests/contract/test_protocol_conformance.py`（加两个 Protocol 登记）

**Interfaces:**
- Consumes: Task 4 的表、Task 5 的 Protocol 与共享套件
- Produces: `PostgresUserDirectoryStore`、`PostgresAdminAuditStore`；`LocalAdminStore.seed_if_absent` 增加建号与回填

- [ ] **Step 1: 写 PostgreSQL 绑定测试**

创建 `tests/integration/test_identity_directory_postgres.py`：

```python
"""UserDirectoryStore PostgreSQL 共享绑定。"""

from tests.suites.identity_directory import IDENTITY_DIRECTORY_CASES, bind

bind(globals(), IDENTITY_DIRECTORY_CASES)
```

并在同文件追加一条只有真库能证明的用例：

```python
import pytest
import sqlalchemy as sa

from xiaowei_agent.persistence.schema import ADMIN_AUDIT_EVENTS, USER_ACCOUNTS


async def test_rolled_back_change_leaves_no_row_in_either_table(
    user_directory, admin_audit, engine
) -> None:
    """回滚必须是数据库回滚，不是应用层把内存里的改动撤掉。

    直接查两张表：如果 ``apply`` 用了两个独立事务，用户行会留在库里，而
    审计行不在——这正是"授权改了但没人知道"的形态。
    """
    from tests.suites.identity_directory import _audit, _create
    from xiaowei_agent.contracts.enums import AdminAuditAction
    from xiaowei_agent.persistence.identity import AdminAuditUnwritableError

    await user_directory.apply(
        command=_create(),
        audit=_audit(
            AdminAuditAction.USER_CREATED, operation_id="dup", target_ref="usr-1"
        ),
    )
    with pytest.raises(AdminAuditUnwritableError):
        await user_directory.apply(
            command=_create("usr-2", actor="bob"),
            audit=_audit(
                AdminAuditAction.USER_CREATED, operation_id="dup", target_ref="usr-2"
            ),
        )
    async with engine.connect() as connection:
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

`engine` fixture 用 `tests/integration/conftest.py` 里现有的那个；先读它确认名字，不要新建。

- [ ] **Step 2: 运行，确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_identity_directory_postgres.py -q 2>&1 | tail -10
```

需要本机 PostgreSQL。若整组 skip，说明库没起——**先把库起起来**，本任务的全部价值就在真库上，skip 不算通过。

- [ ] **Step 3: 写 PostgreSQL 实现**

在 `postgres.py` 末尾按既有类的写法追加 `PostgresAdminAuditStore` 与 `PostgresUserDirectoryStore`。
`apply()` 的形状是本任务的全部要点，必须逐字照这个骨架写：

```python
class PostgresUserDirectoryStore:
    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    async def apply(
        self,
        *,
        command: DirectoryCommand,
        audit: AdminAuditCandidate,
        override_action: AdminAuditAction | None = None,
    ) -> AdminAuditEvent:
        require_audit_matches_command(command, audit, override_action=override_action)
        now = self._clock()
        event = AdminAuditEvent(
            **audit.model_dump(mode="python"),
            event_id=uuid.uuid4().hex,
            created_at=now,
        )
        # 一个 begin()，两类写入。**不要**在这里开第二个连接或第二个事务：
        # 那正是"权限改了、审计没记上"能够发生的唯一形态。
        try:
            async with self._engine.begin() as connection:
                await self._write_command(connection, command, now=now, actor=audit.actor)
                await connection.execute(
                    sa.insert(ADMIN_AUDIT_EVENTS).values(
                        **event.model_dump(mode="python")
                    )
                )
        except IntegrityError as exc:
            raise _classify_directory_integrity_error(exc) from None
        return event
```

分流函数按**约束名**判断，而不是按异常文本匹配：

```python
_AUDIT_CONSTRAINTS: Final[frozenset[str]] = frozenset(
    {"uq_admin_audit_events_operation_id", "admin_audit_events_pkey"}
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

其余要点：

- `_write_command` 按命令类型发 INSERT/UPDATE，全部用传入的 `connection` 与 `now`；
  目标账号不存在时（UPDATE 影响 0 行）抛 `UserDirectoryNotFoundError`，**在同一事务内**，
  于是审计也不会留下。
- `external_identities` 存的是摘要，不是 `open_id`。在 `persistence/identity.py` 里加
  `external_subject_digest(...)`（Task 5 已给出实现），PostgreSQL 与内存实现共用它——
  两边各算一次摘要，共享套件就会在两个实现上测出不同的冲突语义。
- 在 Task 3 的契约测试里补一条：`external_subject_digest` 与 `admin_audit_target_digest`
  对同一个 `subject_ref` 必须给出不同结果（域隔离的正面证明）。
- `resolve_by_subject` 用一次 JOIN 取 account + assignment，并在 SQL 层就写
  `WHERE user_accounts.status = 'active'`——把禁用判断放在 Python 里，就得依赖每个调用方
  都记得判一次。
- `PostgresAdminAuditStore.append()` 只做 INSERT + 返回；它**没有** UPDATE/DELETE 路径，
  Task 7 的 AST 守卫会机械确认这一点。

在 `local_admin.py` 的 `seed_if_absent` 里，用同一事务额外 INSERT 一条 `user_accounts`（`user_id='usr-local-admin'`、`actor='admin'`、`status='active'`）与一条 `user_role_assignments`（`dev-local/dev`、`role='admin'`），并回填 `local_admins.user_id`。已存在则不重复建（`ON CONFLICT DO NOTHING`），保持 `seed_if_absent` 现有的"已 seed 返回 False"语义不变。

- [ ] **Step 4: 登记 Protocol 一致性**

在 `_conformance.py` 里按既有写法为 `UserDirectoryStore` 与 `AdminAuditStore` 各加一条 `Protocol` 断言，覆盖内存与 PostgreSQL 两个实现。

- [ ] **Step 5: 运行三组测试**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_identity_directory_postgres.py \
  tests/contract/test_identity_store.py tests/contract/test_protocol_conformance.py -q 2>&1 | tail -8
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_migration_paths.py -q 2>&1 | tail -5
ruff check . && mypy src
```

- [ ] **Step 6: 隔离变异反证**

把 `apply()` 的审计 INSERT 挪到 `begin()` 块之外、用新连接执行，然后重跑：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/integration/test_identity_directory_postgres.py -q 2>&1 | tail -6
```

预期：`test_rolled_back_change_leaves_no_row_in_either_table` 变红（`usr-2` 留在库里）。还原后必须回到全绿。

- [ ] **Step 7: 提交**

```bash
git add src/xiaowei_agent/persistence/postgres.py \
        src/xiaowei_agent/persistence/local_admin.py \
        src/xiaowei_agent/persistence/identity.py \
        src/xiaowei_agent/_conformance.py \
        tests/integration/test_identity_directory_postgres.py \
        tests/contract/test_protocol_conformance.py \
        tests/contract/test_identity_contracts.py
git commit -m "feat(w1a): commit identity changes and their audit in one transaction

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: append-only 守卫与 PII/Secret 不外泄安全测试

**Files:**
- Create: `tests/security/test_admin_audit_append_only.py`
- Create: `tests/security/test_controlled_pii_exposure.py`
- Test: 两者都必须带 `security` marker

**Interfaces:**
- Consumes: Task 3–6 的全部产物
- Produces: 机械 append-only 守卫、`ControlledPii` 字段暴露守卫

- [ ] **Step 1: 写 append-only 守卫**

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


def _mutating_calls_on(module: Path, table_symbol: str) -> list[str]:
    """找出形如 ``sa.update(TABLE)`` / ``TABLE.delete()`` 的调用。"""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"update", "delete"}:
            # TABLE.update() / TABLE.delete()
            if isinstance(func.value, ast.Name) and func.value.id == table_symbol:
                found.append(f"{module.name}:{node.lineno} {table_symbol}.{func.attr}()")
            # sa.update(TABLE) / sa.delete(TABLE)
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
    offenders = _mutating_calls_on(_PERSISTENCE / module, "ADMIN_AUDIT_EVENTS")
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
    offenders = _mutating_calls_on(sample, "ADMIN_AUDIT_EVENTS")
    assert len(offenders) == 2


def test_audit_store_offers_no_cleanup_or_retention_api() -> None:
    from xiaowei_agent.persistence.admin_audit import AdminAuditStore

    surface = {name for name in dir(AdminAuditStore) if not name.startswith("_")}
    assert surface == {"append", "load"}
    assert not any(
        word in name
        for name in surface
        for word in ("purge", "clear", "prune", "delete", "update", "retain")
    )
```

- [ ] **Step 2: 写 PII 暴露守卫**

创建 `tests/security/test_controlled_pii_exposure.py`：

```python
"""``ControlledPii`` 标注的字段必须同时 ``exclude`` 与 ``repr=False``。

与 ``SecretHash`` 同一套约定、同一个理由：``repr`` 进 traceback，
``model_dump`` 进日志和响应体，只堵一条等于没堵。
"""

import pytest
from pydantic import BaseModel

from xiaowei_agent.contracts import identity as identity_module
from xiaowei_agent.contracts.identity import BindExternalIdentityCommand, ExternalIdentity

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
    assert checked >= 2, "PII guard scanned nothing — the marker set is out of date"


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
        import datetime as _dt

        from xiaowei_agent.contracts import IdentitySource

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

注意 `assert checked >= 2`：这条防的是"标记集合写错导致扫描 0 个字段却全绿"——没有它，守卫失效时是静默的。

- [ ] **Step 3: 运行 security gate**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -m security -q 2>&1 | tail -5
```

必须全绿，且新增用例数大于 0。

- [ ] **Step 4: 隔离变异反证**

```bash
cp src/xiaowei_agent/contracts/identity.py /tmp/keep-pii.py
python - <<'PY'
from pathlib import Path
p = Path("src/xiaowei_agent/contracts/identity.py")
p.write_text(p.read_text().replace(", exclude=True, repr=False", "", 1))
PY
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/security/test_controlled_pii_exposure.py -q 2>&1 | tail -5
cp /tmp/keep-pii.py src/xiaowei_agent/contracts/identity.py && rm /tmp/keep-pii.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -m security -q 2>&1 | tail -3
```

预期：变异后变红；还原后全绿。

- [ ] **Step 5: 提交**

```bash
git add tests/security/test_admin_audit_append_only.py \
        tests/security/test_controlled_pii_exposure.py
git commit -m "test(w1a): guard admin audit append-only and controlled PII exposure

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: 旧静态身份标签迁移

**Files:**
- Create: `src/xiaowei_agent/interfaces/legacy_identity_migration.py`
- Test: `tests/contract/test_legacy_identity_migration.py`

**Interfaces:**
- Consumes: 既有 `interfaces/feishu_identity.py` 的文档解析；Task 5 的 `UserDirectoryStore.apply`
- Produces: `LegacyMigrationReport`、`migrate_static_identities()`

- [ ] **Step 1: 写失败测试**

创建 `tests/contract/test_legacy_identity_migration.py`，覆盖：

```python
"""旧静态身份标签到数据库目录的一次性迁移。"""

import json

import pytest

from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    ProductRole,
)
from xiaowei_agent.interfaces.legacy_identity_migration import (
    LegacyMigrationConflictError,
    migrate_static_identities,
)

_OPEN_ID_A = "ou_" + "aaaa11112222"
_OPEN_ID_B = "ou_" + "bbbb33334444"
_TENANT = "dev-local"
_ENV = "dev"


def _document(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "version": 1,
        "tenant_id": "dev-local",
        "environment_id": "dev",
        "entries": entries,
    }


def _write(tmp_path, document):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


async def test_labels_map_to_product_roles_per_spec_6_6(
    tmp_path, user_directory
) -> None:
    path = _write(
        tmp_path,
        _document(
            [
                {"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["admin"]},
                {"subject_ref": _OPEN_ID_B, "actor": "bob", "labels": ["dba"]},
            ]
        ),
    )
    report = await migrate_static_identities(
        document_path=path, directory=user_directory, actor_user_id="usr-local-admin"
    )
    assert report.created == 2
    alice = await user_directory.load_account(
        user_id=report.user_id_for("alice"), tenant_id=_TENANT, environment_id=_ENV
    )
    bob = await user_directory.load_account(
        user_id=report.user_id_for("bob"), tenant_id=_TENANT, environment_id=_ENV
    )
    assert alice is not None and alice.assignment.role is ProductRole.ADMIN
    assert bob is not None and bob.assignment.role is ProductRole.OPERATOR


async def test_viewer_and_approver_both_become_plain_users(
    tmp_path, user_directory
) -> None:
    """``approver`` 只进报告，不生效任何授权——R1 才有它的 ACL 真源。"""
    path = _write(
        tmp_path,
        _document(
            [
                {"subject_ref": _OPEN_ID_A, "actor": "carol", "labels": ["approver"]},
            ]
        ),
    )
    report = await migrate_static_identities(
        document_path=path, directory=user_directory, actor_user_id="usr-local-admin"
    )
    facts = await user_directory.load_account(
        user_id=report.user_id_for("carol"), tenant_id=_TENANT, environment_id=_ENV
    )
    assert facts is not None
    assert facts.assignment.role is ProductRole.USER
    assert report.deferred_labels == (("carol", "approver"),)


async def test_multiple_labels_take_the_highest_role(tmp_path, user_directory) -> None:
    """同时带 ``admin`` 和 ``viewer`` 时取 ADMIN。

    取最高而不是报错，是因为旧文件里这种组合是既有事实；取最低会静默
    降权，让迁移当天有人突然进不去。该选择记在迁移报告里。
    """
    path = _write(
        tmp_path,
        _document(
            [
                {
                    "subject_ref": _OPEN_ID_A,
                    "actor": "dave",
                    "labels": ["viewer", "admin"],
                }
            ]
        ),
    )
    report = await migrate_static_identities(
        document_path=path, directory=user_directory, actor_user_id="usr-local-admin"
    )
    facts = await user_directory.load_account(
        user_id=report.user_id_for("dave"), tenant_id=_TENANT, environment_id=_ENV
    )
    assert facts is not None
    assert facts.assignment.role is ProductRole.ADMIN


async def test_rerunning_the_migration_changes_nothing(tmp_path, user_directory) -> None:
    path = _write(
        tmp_path,
        _document(
            [{"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]}]
        ),
    )
    first = await migrate_static_identities(
        document_path=path, directory=user_directory, actor_user_id="usr-local-admin"
    )
    second = await migrate_static_identities(
        document_path=path, directory=user_directory, actor_user_id="usr-local-admin"
    )
    assert first.created == 1 and first.skipped == 0
    assert second.created == 0 and second.skipped == 1


async def test_a_conflict_aborts_the_whole_run_before_any_write(
    tmp_path, user_directory
) -> None:
    """先全量预检再写入。

    部分落库最坏：一半人有账号、一半人没有，而运维看到的是一条失败日志，
    于是很可能重跑——重跑又撞上刚写进去的一半。
    """
    path = _write(
        tmp_path,
        _document(
            [
                {"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]},
                {"subject_ref": _OPEN_ID_B, "actor": "alice", "labels": ["viewer"]},
            ]
        ),
    )
    with pytest.raises(LegacyMigrationConflictError):
        await migrate_static_identities(
            document_path=path,
            directory=user_directory,
            actor_user_id="usr-local-admin",
        )
    assert await user_directory.load_account(
        user_id="usr-legacy-alice", tenant_id=_TENANT, environment_id=_ENV
    ) is None


async def test_every_migrated_entry_writes_its_own_audit_event(
    tmp_path, user_directory, admin_audit
) -> None:
    path = _write(
        tmp_path,
        _document(
            [
                {"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]},
                {"subject_ref": _OPEN_ID_B, "actor": "bob", "labels": ["viewer"]},
            ]
        ),
    )
    report = await migrate_static_identities(
        document_path=path, directory=user_directory, actor_user_id="usr-local-admin"
    )
    assert len(report.audit_event_ids) == 2
    for event_id in report.audit_event_ids:
        event = await admin_audit.load(event_id=event_id)
        assert event is not None
        assert event.action is AdminAuditAction.LEGACY_IDENTITY_MIGRATED
        assert event.outcome is AdminAuditOutcome.SUCCEEDED


async def test_report_never_contains_a_plaintext_open_id(
    tmp_path, user_directory
) -> None:
    path = _write(
        tmp_path,
        _document(
            [{"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]}]
        ),
    )
    report = await migrate_static_identities(
        document_path=path, directory=user_directory, actor_user_id="usr-local-admin"
    )
    assert _OPEN_ID_A not in repr(report)
    assert _OPEN_ID_A not in report.model_dump_json()
```

- [ ] **Step 2: 运行，确认失败原因是缺模块**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_legacy_identity_migration.py -q 2>&1 | tail -10
```

- [ ] **Step 3: 写迁移命令**

创建 `src/xiaowei_agent/interfaces/legacy_identity_migration.py`。标签映射与报告契约照抄：

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
"""多标签取最高，不是取最低、也不是报错。

旧文件里 ``["viewer", "admin"]`` 这种组合是既有事实。取最低会在迁移当天
静默降权，让本来能进的人进不去；报错会让一条历史数据卡住整批迁移。
该选择写进报告，迁移后可人工复核。
"""


class LegacyMigrationConflictError(RuntimeError):
    """预检发现冲突；**此时尚未写入任何一行**。"""


class LegacyMigrationReport(Contract):
    """迁移结果；不含 ``subject_ref``。"""

    created: StrictInt = Field(ge=0)
    skipped: StrictInt = Field(ge=0)
    deferred_labels: tuple[tuple[StrictStr, StrictStr], ...]
    audit_event_ids: tuple[StrictStr, ...]
    user_ids: tuple[tuple[StrictStr, StrictStr], ...]

    def user_id_for(self, actor: str) -> str:
        for stored_actor, user_id in self.user_ids:
            if stored_actor == actor:
                return user_id
        raise KeyError(actor)


def legacy_user_id(actor: str) -> str:
    """确定性 user_id，使重跑能把同一个人识别成 ``skipped``。"""
    return f"usr-legacy-{actor}"
```

`migrate_static_identities()` 的结构：

1. 复用 `feishu_identity.py` 已有的文档解析（大小上限、权限位、唯一性校验）读入条目。
   **不要**第二次实现解析——两份解析迟早在"什么算合法条目"上分叉。
2. **预检阶段**：对每个条目算出 `legacy_user_id(actor)` 与目标角色，并查目录：
   已存在同 `user_id` 且角色一致 → 记为 `skipped`；`actor` 被别的 `user_id` 占用、或该
   `subject_ref` 已绑到别的账号 → 立刻 `raise LegacyMigrationConflictError`。
   **这一阶段一次 `apply` 都不调用。**
3. **写入阶段**：预检全过才逐条 `apply(CreateUserCommand(...), audit=...,
   override_action=AdminAuditAction.LEGACY_IDENTITY_MIGRATED)`，再 `apply(
   BindExternalIdentityCommand(...), ...)`；`operation_id` 用
   `f"legacy-migrate-{user_id}-{step}"`，因此重跑会撞上 `operation_id` 唯一约束而不是
   写出第二套事实。
4. 带 `approver` 标签的条目额外进 `deferred_labels`，不改变它拿到的 `USER` 角色。

- [ ] **Step 4: 运行并跑全量**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_legacy_identity_migration.py -q 2>&1 | tail -6
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q 2>&1 | tail -3
ruff check . && mypy src
```

- [ ] **Step 5: 隔离变异反证**

把两阶段改成边预检边写（去掉预检循环），重跑：预期 `test_a_conflict_aborts_the_whole_run_before_any_write` 变红。还原后回到全绿。

- [ ] **Step 6: 提交**

```bash
git add src/xiaowei_agent/interfaces/legacy_identity_migration.py \
        tests/contract/test_legacy_identity_migration.py
git commit -m "feat(w1a): migrate static identity labels into the directory atomically

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

在 `tests/contract/test_doc_fact_binding.py` 追加（沿用文件既有的 `_ADR_013`、`_CURRENT_TRUTH_DOCS` 等常量与写法）：

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

`_ARCHITECTURE` 沿用该文件既有的路径常量；若名字不同，按文件里实际的常量名改，不要新建。

- [ ] **Step 2: 运行，确认因文档未同步而失败**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_doc_fact_binding.py -q 2>&1 | tail -10
```

- [ ] **Step 3: 同步三份文档**

- `ARCHITECTURE.md`：在组件表加入 `UserDirectory` 与 `AdminAuditStore` 两行（职责/禁止逐字取自规格 §12），并写明"授权改变与审计同事务，审计不可写即 fail-closed"这条不变量。
- `DEVELOPMENT_PLAN.md`：把 W1a 标为已交付，下一项为 W1b；**不改** W4c/R1 的独立阻塞门位置。
- `AGENT_HANDOFF.md`：第 0 节改为"W1a 写内核已离线实现"，记录实现基线 SHA、四门实测数字、变异反证结论；明确写出**未覆盖**：没有激活审批、没有 Admin 页面、没有登录改造、没有真实飞书调用、没有部署与用户验收；以及**残余风险**：既有 `AuthenticatedPrincipal.subject_ref` 仍未标 PII 遮蔽（本阶段未触碰渠道投影，留待 W2）。

- [ ] **Step 4: 深档自审**

```bash
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- src/ | head -200
```

逐项核对：
1. diff 是否只含 Task 0 的 allowlist；
2. `grep -rn "def .*route\|@app\.\|APIRouter" src/xiaowei_agent/interfaces/legacy_identity_migration.py` 必须无结果——W1a 不加路由；
3. `grep -rn "requester\|approver" src/ --include="*.py"` 只应命中迁移报告里的 `deferred_labels`；
4. `grep -rn "ActivationRequest\|ActivationStore\|web_oauth_login_contexts" src/` 必须无结果。

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
gh pr create --base main --title "W1a: identity directory, scoped roles and admin audit write kernel" --body "..."
```

PR 描述必须分成四段：**已验证**（真实跑过的命令与尾部输出）、**只读推理**、**未覆盖**、**残余风险**。明确写出：这是离线写内核实现，**不是**激活流程、登录改造、Admin 页面、真实飞书调用、部署或用户验收。等待精确 SHA 复审与负责人合入，**不自行合并**。

---

## W1a Exit Criteria

W1a 只有同时满足以下条件才可判定完成：

- `UserDirectoryStore` 只有 `apply` 一个写方法，且内存与 PostgreSQL 两个实现都通过同一份共享套件；
- "审计写失败 → 授权改变回滚"由**真实数据库唯一约束**触发并在真库上验证，隔离变异证明移除同事务包裹后该用例变红；
- `admin_audit_events` 的 append-only 有源码级机械守卫，且守卫自身有反例证明它抓得到违规；
- `AdminAuditEvent` 字段集合与规格 §14.2 逐字相等，无任何自由文本或 `dict` 通道；
- 角色→`ChannelPermission`、角色+来源→`AdminCapability` 与规格 §6.2/§6.3 逐格相等，飞书 `ADMIN` 恰好少 `MANAGE_INTEGRATIONS` 与 `RUN_CONNECTION_TESTS` 两项；
- `ChannelPermission` 仍精确三成员；
- `subject_ref` 在契约与数据库两侧都不以明文出现，PII 守卫有"扫描数为 0 即失败"的自证；
- 旧标签迁移先全量预检再写入，冲突时零部分落库，重跑 `created=0`，`approver` 不生效授权；
- `rev_0014` 与 `schema.py` 离线 DDL 一致，发布哈希已登记，downgrade 在有数据时要求显式破坏性授权；
- diff 只含 Task 0 的 allowlist，无 HTTP 路由、无激活载体、无 requester/approver 载体；
- ADR-008 四门全绿；
- W1a PR 经精确 SHA 复审并由**负责人合入** `main`；
- handoff 明确记录：没有激活审批、没有 Admin 页面、没有登录改造、没有真实飞书/Gemini/StarRocks 调用、没有部署、canary 或用户验收。

W1a 合入后允许的下一步只有：从最新 `main` 新建分支，重新入职，编写 **W1b 激活内核详细计划**并送审。W1b 计划未获批前不得写它的测试、migration 或源码。
