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
- 基线四门（计划分支实测）：`3977 passed, 269 skipped` / security `1447 passed, 83 skipped, 2716 deselected` / `ruff` 全绿 / `mypy` 189 个文件无问题；文档契约 `47 passed`。实现者必须从届时最新 `main` 重新跑全部四门取当期数字，**不得**把本行当作未来运行结果。
- 本计划为 V9。每一版都是对 PR #62 上一轮复审的根因修订，各版根因与修订位置见文末的 V2～V9 修订记录；**最新一版在最后**，前面各版只保留为返工来历，不再是当前规则。

## Global Constraints

- **W1a 是写内核阶段，不是产品阶段。** 允许新增/修改的路径见 Task 0 Step 5 的 allowlist。**禁止**新增任何 HTTP 路由、页面、模板、静态 JS、Compose 服务或运行配置文件。
- **W1b/W2/W3 未授权。** 不创建 `ActivationRequest`、`ActivationStore`、`IdentityActivationService`、`web_oauth_login_contexts`、通知 Port、登录页、Admin 页面或审计查询 API。W1a 只交付它们将来要复用的写底座。
- **`ChannelPermission` 精确三成员，不扩展。** `VIEW_SAFE_TASK`、`SUBMIT_READONLY_TASK`、`ADMIN_ALL_SAFE_TASKS`。管理面能力一律进独立的 `AdminCapability`（七成员闭集）。
- **`AdminCapability` 七成员，逐字取自 ADR-013 修订**：`MANAGE_USERS`、`MANAGE_DUTY_BINDINGS`、`VIEW_ADMIN_AUDIT`、`VIEW_PRIVATE_TASK_CONTENT`、`VIEW_INTEGRATION_STATUS`、`MANAGE_INTEGRATIONS`、`RUN_CONNECTION_TESTS`。其中 `MANAGE_INTEGRATIONS` 与 `RUN_CONNECTION_TESTS` **只允许 `LOCAL_ADMIN`**。
- **审计事实由 store 从命令派生，调用方不得组装。** 调用方只提供 `AdminOperationContext`（operation、操作者、认证来源）；`action`、`target_kind`、`target_ref_digest`、`outcome` 与 effect 全部由 `UserDirectoryStore` 根据 `DirectoryCommand` 计算。**不提供任何让调用方指定 action/target/outcome 的参数**——包括 override、hook 和可选覆盖字段。理由：能被调用方指定，就能被调用方写错；"校验它有没有写错"永远弱于"它根本没有机会写"。
- **授权改变只有一条写路径。** `UserDirectoryStore.apply()` 是 `user_accounts`、`user_role_assignments`、`external_identities` 与 `local_admins.user_id` 的**唯一**写入口。本地 Admin bootstrap 与旧身份迁移都必须是 `DirectoryCommand` 的成员，不得另开写方法、另开事务或另写一份 SQL。
- **授权改变与审计同事务，且审计必须能回答"改成了什么"。** 审计写入失败必须使整个授权改变回滚。`user_created`、`role_assigned`、`user_status_changed`、`local_admin_bootstrapped`、`legacy_identity_migrated` 事件必须携带**闭集** effect（新角色 / 新状态），由数据库 CHECK 强制；否则历史状态被后续改动覆盖后，就再也无法还原当时授予了什么。
- **审计 append-only，写契约在 W1a 就交付。** `DEVELOPMENT_PLAN.md:178` 与 `ADR-013:131` 都要求 W1a 先提供持久化与 append-only 写契约、W1b 作为消费者复用它，因此**不得**把写契约推迟到后续阶段。`AdminAuditStore` 的表面恰好是 `append_started` / `append_terminal` / `append_denied` / `load`；`persistence/` 中不得出现针对 `admin_audit_events` 的 `sa.update()` / `sa.delete()`；不提供清理、更新或删除 API。写方法必须**窄到写不出目录成功事实**：通用的 `append(candidate)` 不可接受，目录成功事实只能由 `UserDirectoryStore.apply()` 派生。
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
- **每个提交之后工作区必须干净，该提交必须自己能跑。** 每个任务的提交步骤末尾都带一句
  `test -z "$(git status --porcelain)"`。它挡的是 Task 0 第 7 条静态核对挡不住的那一半：
  某个文件计划从头到尾没提过，`Files:` 和 `git add` **两边一起漏**——静态检查看不见它，
  但只要真的改了它，工作区就不干净。不干净就停下来判断：是该把它加进本任务的 `Files:`
  与 `git add`，还是它本来就不该被改。**不要**用 `git add -A` 把它扫进去。

## 与规格的两处有意偏离（复审已确认可接受）

1. **`UserAccount` 不带 `tenant_id/environment_id`，`actor` 全局唯一。**
   规格 §6.1 的 `UserAccount` 字段表里没有作用域，作用域只出现在 `UserRoleAssignment`。§6.6 要求"在同一 `tenant_id/environment_id` 内拒绝 actor、subject 或本地账号冲突"——全局唯一的 `actor` 严格强于该要求。复审结论：可以接受，当前角色作用域已由 assignment 承载，没有现存需求要求两个租户复用同一 actor。
2. **`LocalCredential.username` 本阶段不落库。**
   规格 §6.1 列出 `username`（首版固定 `admin`），但 W1a 没有消费者：现有 `local_admin_auth.py` 的登录只校验口令，不读用户名，登录页属于 W2。本阶段只落 `local_admins.user_id`。复审结论：可以接受，**前提是 W2 继续把 `admin` 作为固定认证常量**，不得借机新增可变用户名或第二套账号真源。该前提写入本计划，W2 计划必须复述它。

## 三张规范矩阵（唯一真源）

下面三张表是本计划里**唯一**一处定义错误语义、写入口与冲突探测方式的地方。后面
每个 Task 只引用它们，不再各自给一份答案。

上一版的缺陷正是"同一条规则在多个章节各写一遍"：`apply()` 里同一个"复用
`operation_id`"场景，Task 5 的一条用例期望 `AdminAuditUnwritableError`，另一条
期望 `AdminAuditConflictError`——两条不可能同时通过。规则散在多处时，分叉不会
被任何人看见。

### 矩阵一：错误语义

归属规则一句话：**谁的公开方法失败，就用谁的错误族。** `apply()` 内部的审计冲突
不以 `AdminAuditConflictError` 的面目出现——它是一次授权改变的失败，叫
`AdminAuditUnwritableError`。

| 公开入口 | 事实 | 抛出 | 授权改变 |
| --- | --- | --- | --- |
| `UserDirectoryStore.apply` | `user_id`、`actor` 或 `subject` 摘要与既有目录事实冲突 | `UserDirectoryConflictError` | 回滚 |
| `UserDirectoryStore.apply` | 命令的目标账号或角色不存在 | `UserDirectoryNotFoundError` | 回滚 |
| `UserDirectoryStore.apply` | 该 `operation_id` 在该阶段已有审计事件，审计写不进去 | `AdminAuditUnwritableError` | 回滚 |
| `UserDirectoryStore.apply` | bootstrap 的凭据行指向不存在的账号，或该账号没有 ADMIN 角色 | `UserDirectoryConflictError`（fail-closed） | 回滚 |
| `AdminAuditStore.append_started` / `append_denied` / `append_terminal` | 该 `operation_id` 在该阶段已有事件 | `AdminAuditConflictError` | — |
| `AdminAuditStore.append_terminal` | 找不到对应的 `STARTED` | `AdminAuditMissingStartError` | — |
| 以上任意入口 | 未知唯一约束、CHECK、外键、NOT NULL、schema、解码故障 | `PersistenceIntegrityError`，`write_outcome` 由 `_write_transaction` 判定 | 回滚 |
| 以上任意入口 | 连接、超时、连接池故障 | `PersistenceUnavailableError`，`write_outcome` 同上 | 见 `write_outcome` |

内存实现与 PostgreSQL 实现在这张表上**逐格相同**，共享套件断言的就是这张表。内存
实现没有事务，用快照恢复表达"回滚"那一列——它表达的是同一个语义，不是另一个。

### 矩阵二：写入口

| 事实 | 唯一写入口 | 机械守卫 |
| --- | --- | --- |
| `user_accounts` | `apply()` → `_apply_locked` / `_apply_in_transaction` | `_AUTHZ_WRITE_SITES` |
| `user_role_assignments` | 同上 | `_AUTHZ_WRITE_SITES` |
| `external_identities` | 同上 | `_AUTHZ_WRITE_SITES` |
| `local_admins.user_id` | 同上 | `_CREDENTIAL_LINK_SITES` |
| `local_admins.password_hash` / `must_change_password` | `LocalAdminStore`（既有，本阶段不动） | 不禁止：改密是凭据，不是授权 |
| `admin_audit_events`（PostgreSQL） | 模块级 `_insert_audit_event`，**仅此一处**。目录路径与 `PostgresAdminAuditStore._write` 都调用它 | `_AUDIT_WRITE_SITES`（单元素）+ `_FULL_CANDIDATE_HELPERS` |
| `admin_audit_events`（内存） | `InMemoryAdminAuditStore._append_locked`，目录 store 在自己的临界区内复用它 | `_FULL_CANDIDATE_HELPERS` |

### 矩阵三：事务与冲突探测

**已知冲突不靠捕获 `IntegrityError` 判定，而是用 `ON CONFLICT DO NOTHING ... RETURNING`
在事务体内探测。** 这是仓库既有做法，不是本计划的发明：`PostgresTaskStore.create_task`
（`postgres.py:1927` 的 docstring 写明了理由）、`PostgresPlanStore.save`（`:2222`）、
`append_evidence`（`:416`）全是这个形状，而 `persistence/` 里**没有任何一处**捕获
`IntegrityError` 来分类冲突。

| 层 | 职责 | 不做什么 |
| --- | --- | --- |
| 事务体内（`_apply_in_transaction` / `_write`） | 已知唯一约束用 `ON CONFLICT DO NOTHING ... RETURNING` 探测；拿不到行就抛矩阵一的闭集领域错误 | 不捕获 `IntegrityError`，不读约束名，不碰 `exc.orig` |
| `_write_transaction`（`postgres.py:293`） | 没被探测吞掉的驱动异常在这里收敛，并按事务退出的事实判定 `ROLLED_BACK` / `NOT_CONFIRMED` | 不认识任何业务语义 |
| `@_persistence_boundary`（`postgres.py:244`） | 最终闭集与异常链切断 | 不区分冲突种类 |

闭集领域错误（`RuntimeError` 子类）穿过后两层时**原样通过**：
`classify_persistence_exception` 对它们返回 `None`，两层都走 `raise` 重抛。

`ON CONFLICT DO NOTHING` **不带**推断目标：一条 INSERT 可能撞上该表的任意一条唯一
约束，而带目标时只推断一条。它吞掉的**只有**唯一/主键/偏唯一索引冲突；CHECK、外键、
NOT NULL 一律照抛，落到 `_write_transaction`。

因此"未知约束被伪装成业务冲突"这条风险不是靠运行期判别堵的——运行期根本不去判别
——而是靠一条**静态**用例把这几张表的可推断冲突列组合逐格钉死（Task 6 Step 2 的离线用例）：新增一
条唯一约束就必须来那条用例里回答"它撞了意味着什么"。

### 这三张表依据的实测事实

以下结论来自在真实 PostgreSQL 16.15 + asyncpg 0.30.0 + SQLAlchemy 2.0.52 上跑的临时
探针（独立探针库，跑完即删），不是从文档或记忆推断的：

| 问题 | 实测结果 |
| --- | --- |
| `sa.exc.IntegrityError.orig` 是什么类型 | `sqlalchemy.dialects.postgresql.asyncpg.IntegrityError` |
| 它有没有 `.diag` | **没有**。`.diag.constraint_name` 是 psycopg 的接口，本仓库用 asyncpg |
| 它身上有什么 | 只有 `.sqlstate` / `.pgcode`（`23505`）；**没有** `constraint_name` |
| 约束名在哪 | 只在 `exc.orig.__cause__`（`asyncpg.exceptions.UniqueViolationError`）上——而那正是 `_persistence_boundary` 存在的目的所在：`str(exc)` 同时带着被拒绝的那一行 |
| 事务体内抛 `IntegrityError` 后，外层能不能 `except sa.exc.IntegrityError` | **不能**。`_write_transaction` 已经把它换成了 `PersistenceIntegrityError(constraint, rolled_back)`，且 `__cause__` 为 `None` |
| 不带推断目标的 `ON CONFLICT DO NOTHING` 吞掉什么 | 主键冲突、普通唯一约束冲突、**偏唯一索引**冲突三种：`RETURNING` 为空且不抛异常 |
| 它不吞什么 | CHECK、外键、NOT NULL 照抛，经两层收敛成 `PersistenceIntegrityError(constraint, rolled_back)` |
| 事务体内抛闭集领域错误（`RuntimeError` 子类）会怎样 | 穿过两层不变形；同事务里已发出的授权 INSERT 已回滚（实测该表行数为 0） |
| 事务体内直接抛 `PersistenceIntegrityError` 会怎样 | `category` 与 `write_outcome` 穿过两层后保持不变 |

探针本身不进仓库：它需要一个跑起来的库和一个能建库的账号，属于一次性事实核对，不是
可重复的回归。它证明的每一条，在 Task 6 的集成用例里都有对应断言。

---

## Review Focus

审核者应优先核对以下十七点。**第 0 点先看**：上面的三张规范矩阵是错误语义、写入口与冲突探测的唯一真源，下面每一点都只是它的核对方式；任何一处正文与矩阵不一致，以矩阵为准并当作缺陷报出来。

1. `apply(command, context)` 是否真的**不给调用方任何机会**指定 action、target 或 outcome：签名上有没有 override、可选覆盖字段或 hook。同时核对另一个方向：`AdminAuditStore` 的三个写方法是否**都写不出目录成功事实**（`AdminAuditStart` / `AdminAuditDenial` 没有 `outcome` 与 `effect` 字段；`AdminAuditTerminal` 的稳定字段从已存 `STARTED` 读回，而 CHECK 禁止目录动作写 `STARTED`）。V2 留了通用 `append(candidate)`（可伪造），V3 一度整个删掉写契约（违反 `DEVELOPMENT_PLAN.md:178` 与 `ADR-013:131`）——两个方向都错。
2. 四张授权表的写入是否**全部**发生在 `apply()` 的那一个 `begin()` 内。`grep` 一遍 `persistence/` 里对这四张表的 INSERT/UPDATE，看有没有第二处；特别核对 `seed_if_absent` 与旧身份迁移。
3. 审计失败回滚是否由**真实数据库约束**触发并在**真 PostgreSQL** 上验证；集成测试的 `user_directory` fixture 是否确实是 `PostgresUserDirectoryStore`（Task 6 有一条显式类型断言钉住它）。
4. `operation_id` 是否**不是**全局唯一（否则堵死规格 §14.2 的两阶段配置审计），两条 partial unique index 的 WHERE 是否正确，以及结构用例的 helper 有没有把 partial index 误算成全局唯一。
5. `role_assigned` / `user_status_changed` / `user_created` 事件能否回答"改成了什么"，且 effect 是闭集列而不是自由文本或 JSON。
6. 旧标签迁移是否是**一个命令、一个事务**：中途任何一步失败后，两张目录表与审计表是否零部分落库；测试制造的冲突是不是**数据库既有冲突**，而不是被 `_IdentityDocument` 解析期就挡掉的同文件重复 actor。
7. 由外部输入派生的 `user_id` / `operation_id` 是否有界，最大长度 actor 是否有用例。
8. bootstrap 的幂等判定读的是**目录链接是否完整**，还是『凭据行是否存在』：一个已经跑过 RI5、`local_admins` 早有行的库，升级到 `rev_0014` 后能否补齐账号、ADMIN 角色与链接，且**不覆盖**已改过的密码。Task 6 有一条真实降级—插入—升级的用例。
9. 承重守卫是否按**属性**判定而不是按写法枚举：完整候选 helper 由 AST 按**签名**发现（`AdminAuditCandidate` 参数）而非按方法名冻结；`local_admins.user_id` 的守卫是否提取 `values()` **实际写入的列**，四种等价写法（`values(user_id=v)`、`values({T.c.user_id: v})`、`values({"user_id": v})`、`values({T.c["user_id"]: v})`）全部命中，而**静态判不出写哪一列**的形式（变量键、`**` 展开）报警而不是放行；所有允许项 owner-qualified 到 `路径::类.方法`，**不跳过整模块**；只改 `password_hash` 的正常路径不误报。
10. **协议与实现是否一致**：`tests/contract/test_protocol_conformance.py` 里那条『公开表面精确等于 Protocol』的断言，对内存与 PostgreSQL 两个实现都成立。`isinstance` 式的 Protocol 检查只保证实现不少于协议，抓不住多出来的公开写方法。
11. 新 PostgreSQL 代码是否**复用了同文件里既有的机制**：公开方法都带 `@_persistence_boundary`，写路径走 `_write_transaction`，行与契约互转在 `rows.py` 且枚举显式构造，已知冲突用 `ON CONFLICT DO NOTHING ... RETURNING` 探测。直接 `engine.begin()`、`model_validate(dict(row))`、`except sa.exc.IntegrityError`、读 `exc.orig` 或约束名，都是错的。

12. **错误语义是否只有矩阵一那一份**：全文搜索 `AdminAuditConflictError`、`AdminAuditUnwritableError`、`UserDirectoryConflictError`、`PersistenceIntegrityError`，每一处出现是否都能在矩阵一里找到对应的那一格。特别核对同一个场景（复用 `operation_id` 调 `apply()`）在所有章节里是否只有一个答案。
13. diff 是否只含 Task 0 的 allowlist；有没有顺手加 HTTP 路由、激活表、DBA/值班表或 requester/approver 载体。
14. **迁移降级守卫是否真的会生效**：`rev_0014.downgrade()` 的调用形状是否与 `guards.py:48` 的真实签名一致（`connection` 位置参数 + `guarded=`），有没有留下手工的存在性预检把分类 `counts` 降级成布尔；以及真实 PostgreSQL 上是否有拒绝、数据保留、显式授权后删除、重新升级四段证据——离线用例不执行 `downgrade()`，抓不到这一类。
15. **给既有契约新增的字段是否贯穿了全部构造点**：`LocalAdminRecord.user_id` 在 `get()`、改密返回值与两个内存构造点上是否都被回填；测试是否同时断言数据库列、返回值与下一次读取三处，而不是只断言数据库列。
16. **每个任务的文件闭集是否三方闭合**：`Files:`、`git add` 与正文段落标记互相覆盖，且每个提交之后 `git status --porcelain` 为空。Task 0 的第 2、7 条查静态两方，工作区干净那一条兜「两边一起漏」。
17. **旧身份迁移的 skip 判据是否 fail-closed**：账号、actor、状态、角色与外部身份绑定五项全部精确匹配才跳过；部分存在是否整批冲突回滚。被剔除出批次的条目，后面的唯一约束再也看不见它——「靠后面兜底」在这里不成立。

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

`AGENT_HANDOFF.md` 第 0 节应当已经写明 **W0 已合入、下一步是 W1a**（该更新随本计划 PR 一起合入，见 Task 9）。若它仍写着"正在做 W0 文档收口"，说明本计划 PR 的 handoff 修订没有合进来，**停止并报告**，不要自行推断。

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
src/xiaowei_agent/contracts/base.py
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
src/xiaowei_agent/persistence/rows.py
src/xiaowei_agent/persistence/local_admin.py
src/xiaowei_agent/persistence/fake.py
src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_admin_audit.py
src/xiaowei_agent/interfaces/legacy_identity_migration.py
src/xiaowei_agent/interfaces/feishu_identity.py
src/xiaowei_agent/interfaces/local_admin_auth.py
src/xiaowei_agent/_conformance.py
tests/**
ARCHITECTURE.md
AGENT_HANDOFF.md
DEVELOPMENT_PLAN.md
```

`persistence/rows.py` 在列表里，因为行与契约的互转按仓库既有约定就住在那里（`row_to_channel_binding` / `channel_binding_to_row` 等十余对）。审计事件的两个方向必须和它们并排，而不是在 `postgres.py` 里另起一份——另起一份就是第二套列映射，两条写路径迟早分叉。

`interfaces/local_admin_auth.py` 在列表里，只为让它消费 `contracts/identity.py` 的五个本地管理员常量，并删掉自己那三个字面量。`persistence` 按 `test_module_layering.py:47` 不能反向导入 `interfaces`，所以真源必须下沉到契约层；下沉之后不改接口层，就会留下两份真源。

`contracts/base.py` 在列表里，只为把 `SecretHash` 这一行别名搬家。它现在定义在 `persistence/local_admin.py:42`（`SecretHash = StrictStr`），而 `contracts/identity.py` 要按同一套约定标注 `password_hash`。契约模块不能反向依赖 persistence；在两处各写一份别名，又会让『同一套约定』变成两套。全仓库只有 `local_admin.py:54` 与 `:65` 两个字段用它，搬家后由 `local_admin.py` 从 `contracts/base.py` 导入，不改变任何行为。

V3 的计划直接写了 `from xiaowei_agent.contracts.base import SecretHash`，而那里根本没有这个名字——按原文跑是 `ImportError`，又一条不合格 RED。

`persistence/fake.py` 在列表里，因为 Task 6 要让 `InMemoryLocalAdminStore.seed_if_absent`（`fake.py:514`）和 PostgreSQL 侧一样委托给 `UserDirectoryStore.apply()`。两个实现的 bootstrap 语义必须由同一段分流代码决定，否则共享套件失去意义。

`interfaces/feishu_identity.py` 在列表里是**有意的**：Task 8 需要一份保留原始 labels 的公开只读解析契约，而当前的 `load_feishu_identity_directory()` 已经把 labels 压成了 `frozenset[ChannelPermission]`，原始标签在返回值里不复存在（见 `src/xiaowei_agent/interfaces/feishu_identity.py:125`）。Task 8 只**抽出**已有的解析逻辑并把它公开，不改变它的校验规则、大小上限、权限位检查，也不改变任何现有调用方的行为。

任何超出该列表的文件出现在 `git status` 里，都必须先停下来说明理由。

**机械核对七件事，不靠眼睛。** 这七条各自对应一次真实返工，开工前一起跑：

```bash
python - <<'CHECK'
import re, pathlib

text = pathlib.Path(
    "docs/superpowers/plans/2026-09-21-w1a-identity-authz-admin-audit.md"
).read_text(encoding="utf-8")

# 1. 每条 Files: 声明都必须落在 allowlist 里
allow = re.search(r"只有：\n\n```\n(.*?)```", text, re.S).group(1).split()
declared = {
    m.group(1)
    for line in text.splitlines()
    if (m := re.match(r"^- (?:Create|Modify|Test): `([^`]+)`", line.strip()))
}
outside = sorted(
    p for p in declared
    if not any(p == a or (a.endswith("**") and p.startswith(a[:-2])) for a in allow)
)
print("1) 超出 allowlist：", outside or "（无）")

# 2. 每个任务的 git add 都必须覆盖它自己的 Files:
tasks = re.split(r"\n### (Task \d+[^\n]*)\n", text)
unstaged = []
for name, body in zip(tasks[1::2], tasks[2::2]):
    files = {
        m.group(1)
        for line in body.splitlines()
        if (m := re.match(r"^- (?:Create|Modify|Test): `([^`]+)`", line.strip()))
    }
    source = {f for f in files if f.startswith(("src/", "tests/"))}
    adds: set[str] = set()
    for block in re.findall(r"```bash\n(.*?)```", body, re.S):
        if "git add" in block:
            segment = block[block.index("git add"):].split("git commit")[0]
            adds |= set(re.findall(r"[\w./\-]+\.(?:py|md)", segment))
    if adds and (missing := sorted(source - adds)):
        unstaged.append((name.split(":")[0], missing))
print("2) Files 声明了但 git add 漏掉：", unstaged or "（无）")

# 3. 计划引用的既有符号必须真的存在
import ast, importlib
wanted: dict[str, set[str]] = {}
for block in re.findall(r"```python\n(.*?)```", text, re.S):
    try:
        tree = ast.parse(block)
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "xiaowei_agent"
        ):
            wanted.setdefault(node.module, set()).update(a.name for a in node.names)
absent = []
for module, names in sorted(wanted.items()):
    try:
        loaded = importlib.import_module(module)
    except ModuleNotFoundError:
        continue  # W1a 将要新建的模块
    for n in sorted(names):
        if hasattr(loaded, n):
            continue
        try:  # ``from pkg import submodule`` 不是缺符号
            importlib.import_module(f"{module}.{n}")
        except ModuleNotFoundError:
            absent.append(f"{module}.{n}")
print("3) 引用了不存在的符号（需逐条判定是否为 W1a 新建）：", absent or "（无）")

# 4. 按最终形状把文件拼回来，查未定义名与重复导入
import subprocess, tempfile
boundary = re.compile(
    r"\n(?:创建|追加到|修改) `[^`]+`(?:[（(][^）)]*[）)])?[：。]|\n- \[ \] \*\*Step |\n### Task "
)
merged: dict[str, str] = {}
for m in re.finditer(r"\n(?:创建|追加到) `([^`]+\.py)`(?:[（(][^）)]*[）)])?：", text):
    nxt = boundary.search(text, m.end())
    merged.setdefault(m.group(1), "")
    merged[m.group(1)] += text[m.end(): nxt.start() if nxt else len(text)]
broken = []
with tempfile.TemporaryDirectory() as tmp:
    for path, body in merged.items():
        blocks = re.findall(r"```python\n(.*?)```", body, re.S)
        if not blocks:
            continue
        # 追加到既有文件时先垫上真实文件内容：不垫，片段必然报一堆假的未定义名，
        # 真正的漏导入反而淹没在里面。
        real = pathlib.Path(path)
        head = real.read_text(encoding="utf-8").rstrip() + "\n\n" if real.exists() else ""
        f = pathlib.Path(tmp) / path.replace("/", "__")
        f.write_text(head + "\n\n".join(b.rstrip() + "\n" for b in blocks), encoding="utf-8")
        r = subprocess.run(
            ["ruff", "check", "--select", "F821,F811", "--no-cache",
             "--output-format", "concise", str(f)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            broken.append((path, r.stdout.strip().splitlines()[:6]))
print("4) 拼回文件后仍有未定义名/重复导入：", broken or "（无）",
      f"[共拼出 {len(merged)} 个文件]")

# 5. 调用既有 src/ 符号时，实参必须能绑上真实签名
import ast, inspect
_MARK = object()
unbindable = []
for block in re.findall(r"```python\n(.*?)```", text, re.S):
    try:
        tree = ast.parse(block)
    except SyntaxError:
        continue
    known: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "xiaowei_agent"
        ):
            try:
                loaded = importlib.import_module(node.module)
            except ModuleNotFoundError:
                continue
            for alias in node.names:
                obj = getattr(loaded, alias.name, _MARK)
                if obj is not _MARK and (inspect.isfunction(obj) or inspect.isclass(obj)):
                    known[alias.asname or alias.name] = obj
    shadowed = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = node.func.id
        if name in shadowed or name not in known:
            continue
        if any(isinstance(a, ast.Starred) for a in node.args) or any(
            k.arg is None for k in node.keywords
        ):
            continue
        try:
            sig = inspect.signature(known[name])
            sig.bind(*([_MARK] * len(node.args)), **{k.arg: _MARK for k in node.keywords})
        except (TypeError, ValueError) as exc:
            unbindable.append(f"{name}(...)：{exc}")
print("5) 调用既有符号时实参绑不上真实签名：", sorted(set(unbindable)) or "（无）")

# 6. 给既有契约加字段时，它的全部构造点必须逐个列出
table = re.search(
    r"^\| 构造点（`相对路径::owner`）[^\n]*\n\|[-| ]+\n((?:\|[^\n]*\n)+)", text, re.M
)
assert table, "计划没有那张构造点清单——加字段却不列构造点，这条核对就是白跑的"
listed = {m.group(1) for m in re.finditer(r"^\| `([^`]+)` \|", table.group(1), re.M)}
discovered = set()
for path in sorted(pathlib.Path("src").rglob("*.py")):
    stack: list[str] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                walk(child)
            stack.pop()
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LocalAdminRecord"
        ):
            discovered.add(f"{path.relative_to('src/xiaowei_agent')}::{'.'.join(stack)}")
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(ast.parse(path.read_text(encoding="utf-8")))
print("6) `LocalAdminRecord` 构造点与计划清单不一致：",
      sorted(listed ^ discovered) or "（无）")

# 7. 正文段落标记写到的文件，必须出现在该任务的 Files:
mismarked = []
for name, body in zip(tasks[1::2], tasks[2::2]):
    files = {
        m.group(1)
        for line in body.splitlines()
        if (m := re.match(r"^- (?:Create|Modify|Test): `([^`]+)`", line.strip()))
    }
    marked = {
        m.group(1) for m in re.finditer(r"^(?:创建|追加到|修改) `([^`]+\.py)`", body, re.M)
    }
    if missing := sorted(marked - files):
        mismarked.append((name.split(":")[0], missing))
print("7) 正文写到了但 Files: 没声明：", mismarked or "（无）")
CHECK
```

七条都必须输出 `（无）`，第 3 条允许出现 W1a 将要新建的符号——逐条确认，**不要**
整体跳过。

七条各自的来历，**全部是同一句话的不同层**："改了一处，没跟到下一处"。

| 条 | 来历 |
| --- | --- |
| 1 | V3 漏了 `persistence/fake.py`：Task 6 明确要改它，allowlist 却没有，按纪律执行会在 Task 6 停工 |
| 2 | V4 把 `fake.py` 加进了 allowlist 与 Files，却没加进 Task 6 的 `git add` |
| 3 | V3 写了 `from xiaowei_agent.contracts.base import SecretHash`，而那里没有这个名字 |
| 4 | V7 的 Task 7 文件里有一组**互斥断言**（同时要求某个站点"在"和"不在"同一个集合），而 `_helper_names()` 对模块级路径解析出的是 `py::_insert_audit_event` 不是函数名；集成测试用了从未导入过的 `AdminAuditOutcome`，旁边还写着一句"该文件已经导入" |
| 5 | V8 的 `rev_0014` 降级里写着 `require_destructive_authorization("rev_0014 downgrade …")`，而真实签名要 `connection` 与 `guarded=`。守卫看起来在，有数据时抛的却是 `TypeError` |
| 6 | V8 给 `LocalAdminRecord` 加了 `user_id`，却只跟到写入路径：`PostgresLocalAdminStore.get()` 与改密的返回值都不回填它，于是"库里已关联、Store 说没关联" |
| 7 | V8 的 Task 6 正文要改 `contracts/identity.py`、`interfaces/local_admin_auth.py` 与两个契约测试文件，Files 与 `git add` **两边都没有**——第 2 条只查得出"声明了却没暂存",查不出"两边一起漏" |

**第 4 条是前三条抓不到的那一类。** 前三条查的是"声明与声明之间对不对得上"，第 4 条
查的是"把一个文件的所有代码块按文档顺序拼起来之后，它还成不成立"。V7 之前每一轮我都
在验证**片段**：片段各自跑通过，拼起来却带着互斥断言和缺失导入。段落标题写的是哪个
路径，下面的代码块就属于哪个文件——这条规则现在是机械可查的，也因此**每个文件只能有
一份导入块**，不允许"再补两行导入"式的补充块。追加到既有文件时，第 4 条会先把**真实
文件内容**垫在前面再查，所以"这个名字在那个文件里本来就有"不用靠记忆判断。

**第 5、6 条查的是计划与既有代码的接缝，方向相反的两端。** 第 5 条是**调用方向**：计划
调用仓库里已有的函数时，实参能不能绑上它的真实签名。第 6 条是**被调用方向**：计划给
既有契约加了字段，它的全部构造点有没有被逐个处理——清单由 AST 发现，`==` 比对，将来
任何人新增一个构造点都会让它变红。这两条都只能靠"去读真实定义"发现，而"去读真实定义"
正是前八版反复失守的地方。

**第 7 条与"提交后工作区必须干净"配对使用。** 第 7 条是静态的：正文里凡用
以 `创建` / `追加到` / `修改` 加反引号路径加全角冒号标记写到的文件，必须出现在该任务的 `Files:` 里；因此
**每个要落盘的文件都必须有这样一个标记**，不能只在散文里提一句"放在 X 里"。但静态检查
永远抓不到"计划从头到尾就没提过这个文件"。那一半由每个任务提交步骤后的
`git status --porcelain` 兜底：改了没暂存，工作区就不干净。

七条都只做静态检查，它们**不**能验证：拼出来的文件能否 import（W1a 模块尚未存在）、
断言是否互相矛盾、代码是否真的正确。Task 7 的守卫文件另有一步：它不依赖任何 W1a 运行期
代码，因此可以拼出来直接 `pytest` 跑——见 Task 7 Step 1 的说明。

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
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
```

---

### Task 2: 身份目录契约与写命令闭集

**Files:**
- Modify: `src/xiaowei_agent/contracts/base.py`（把 `SecretHash` 别名从 `local_admin.py` 搬过来）
- Modify: `src/xiaowei_agent/interfaces/local_admin_auth.py`（改为消费契约层的五个本地管理员常量）
- Modify: `src/xiaowei_agent/persistence/local_admin.py`（改为从 `contracts/base.py` 导入 `SecretHash`）
- Create: `src/xiaowei_agent/contracts/identity.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Test: `tests/contract/test_identity_contracts.py`

**Interfaces:**
- Consumes: Task 1 的 `ProductRole`、`UserStatus`；既有 `Contract`、`StrictStr`、`AwareDatetime`、`IdentitySource`
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

from typing import Annotated, Final, Literal

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

    幂等判定读的是**目录链接是否完整**，不是"``local_admins`` 有没有行"。链接完整
    时整条命令是 no-op，返回零个审计事件——什么都没改，就不该有审计；有行但
    ``user_id IS NULL`` 时要补齐账号、角色与链接，并**保留原 ``password_hash``**。
    四种状态的完整规则见 Task 5 那张四态表，那里是唯一真源。

    上一版这里写的是"``local_admins`` 已有行时整条命令是 no-op"——那正是 V1 的缺陷
    原文，四态表改对了，这段 docstring 没跟着改。任何跑过 RI5 的库都早有这一行。
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
git add src/xiaowei_agent/contracts/base.py \
        src/xiaowei_agent/contracts/identity.py \
        src/xiaowei_agent/contracts/__init__.py \
        src/xiaowei_agent/persistence/local_admin.py \
        src/xiaowei_agent/interfaces/local_admin_auth.py \
        tests/contract/test_identity_contracts.py
git commit -m "feat(w1a): add identity directory contracts and the write command closed set

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
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
    DIRECTORY_ACTIONS,
    AdminAuditCandidate,
    AdminAuditDenial,
    AdminAuditEffect,
    AdminAuditEvent,
    AdminAuditStart,
    AdminAuditTerminal,
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


def test_two_phase_start_refuses_a_directory_action() -> None:
    """目录动作在**契约层**就被拒，不依赖任何实现或数据库。

    这条用例存在的理由是：上一版只有 PostgreSQL 的 CHECK 拦它，内存实现直接
    落库。同一个共享套件在两个绑定上会给出不同答案，而共享套件的全部价值正是
    "两边行为相同"。
    """
    with pytest.raises(ValidationError):
        AdminAuditStart(
            operation_id="op-two-phase",
            tenant_id="dev-local",
            environment_id="dev",
            actor_user_id="usr-admin",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
            action=AdminAuditAction.ROLE_ASSIGNED,
            target_kind=AdminAuditTargetKind.USER,
            target_ref_digest="a" * 64,
        )


def test_every_w1a_action_is_a_directory_action() -> None:
    """钉住一个**有意的**边界，而不是假装它不存在。

    `ADR-013:129` 把两阶段写留给非事务性的配置面动作，而 W1a 的 action 闭集
    全部是目录动作。结论：W1a 按 `DEVELOPMENT_PLAN.md:178` 交付了
    ``append_started`` / ``append_terminal`` 的**契约**，但本阶段没有任何合法
    action 能走通它们的成功路径。

    这条断言会在 W4a 加入配置动作时**转红**——那时才是放开的时机。在此之前它
    保证没有人用两阶段写绕过 ``apply()`` 的派生。
    """
    assert DIRECTORY_ACTIONS == frozenset(AdminAuditAction)


def test_neither_start_nor_denial_can_state_an_outcome_or_effect() -> None:
    """窄输入类型靠"字段不存在"提供保证，因此字段集合本身要被钉住。"""
    for contract in (AdminAuditStart, AdminAuditDenial):
        fields = set(contract.model_fields)
        assert "outcome" not in fields and "effect" not in fields
    assert set(AdminAuditTerminal.model_fields) == {
        "operation_id",
        "outcome",
        "reason_code",
        "effect",
    }


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
from typing import Final, Literal, Self

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

DIRECTORY_ACTIONS: Final[frozenset[AdminAuditAction]] = frozenset(
    {
        AdminAuditAction.USER_CREATED,
        AdminAuditAction.USER_STATUS_CHANGED,
        AdminAuditAction.ROLE_ASSIGNED,
        AdminAuditAction.ROLE_REVOKED,
        AdminAuditAction.EXTERNAL_IDENTITY_BOUND,
        AdminAuditAction.EXTERNAL_IDENTITY_UNBOUND,
        AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED,
        AdminAuditAction.LEGACY_IDENTITY_MIGRATED,
    }
)
"""由 ``UserDirectoryStore.apply()`` 派生、**不得**走两阶段写的动作。

数据库 CHECK ``ck_admin_audit_events_directory_actions_are_single_phase`` 用的是
同一份名单的 SQL 字面量；Task 4 有一条用例逐值比对两边，避免它们各自漂移。

W1a 的 ``AdminAuditAction`` 闭集恰好就是这八个，因此本阶段两阶段写没有合法
action——这是有意的，见 `ADR-013:129`（两阶段留给非事务性配置面动作）。
"""

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

    **只由 store 内部构造，不由业务调用方构造。** 构造它的只有 ``derive_audit()``
    与三个窄写方法的 ``as_candidate()``；``AdminAuditStore`` 的公开方法**一个都
    收不下它**（它们各自只收 ``AdminAuditStart`` / ``AdminAuditTerminal`` /
    ``AdminAuditDenial``）。

    能收下完整候选的只有两个私有助手，它们由 Task 7 的守卫**按签名发现**并冻结
    调用点——不是按名字冻结：上一版按名字冻结 ``_append_locked``，随后新增的
    ``_insert(candidate)`` 就自动逃出了名单。**私有命名不是安全边界。**

    ``DENIED`` 与 ``STARTED`` 两个 outcome 留在闭集里是给 W1b/W4a 用的；W1a
    没有产生它们的命令，数据库 ``CHECK`` 也不允许目录动作写 ``STARTED``。

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


class AdminAuditStart(Contract):
    """``AdminAuditStore.append_started()`` 的**唯一**输入。

    **没有 ``outcome``、没有 ``effect``、没有 ``reason_code``。** 少这三个字段
    不是省事，而是这个类型存在的全部理由：调用方拿着它写不出一条"成功"。
    """

    operation_id: StrictStr = Field(min_length=1, max_length=48)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    actor_user_id: StrictStr = Field(min_length=1, max_length=64)
    actor: StrictStr = Field(min_length=1, max_length=64)
    auth_source: IdentitySource
    action: AdminAuditAction
    target_kind: AdminAuditTargetKind
    target_ref_digest: Sha256Hex

    @model_validator(mode="after")
    def _two_phase_is_not_for_directory_actions(self) -> Self:
        """目录动作走不了两阶段写——**在契约层拒绝，不在某个实现里拒绝**。

        数据库有 ``ck_admin_audit_events_directory_actions_are_single_phase``，
        但内存实现没有数据库。把规则放在契约上，两个实现共用同一条；放在任一
        实现里，另一个就会在同一个共享套件里给出不同的答案，而共享套件的全部
        价值正是"两边行为相同"。
        """
        if self.action in DIRECTORY_ACTIONS:
            raise ValueError("directory actions are written in one phase by apply()")
        return self

    def as_candidate(self) -> AdminAuditCandidate:
        return AdminAuditCandidate(
            **self.model_dump(mode="python"), outcome=AdminAuditOutcome.STARTED
        )


class AdminAuditDenial(Contract):
    """``append_denied()`` 的唯一输入：什么都没改的拒绝。

    同样没有 ``outcome`` 与 ``effect``；``reason_code`` 是**必填**，因为一条
    说不出为什么被拒的拒绝审计，在事后争议里等于没有记录。
    """

    operation_id: StrictStr = Field(min_length=1, max_length=48)
    tenant_id: StrictStr = Field(min_length=1, max_length=64)
    environment_id: StrictStr = Field(min_length=1, max_length=64)
    actor_user_id: StrictStr = Field(min_length=1, max_length=64)
    actor: StrictStr = Field(min_length=1, max_length=64)
    auth_source: IdentitySource
    action: AdminAuditAction
    target_kind: AdminAuditTargetKind
    target_ref_digest: Sha256Hex
    reason_code: AdminAuditReasonCode

    def as_candidate(self) -> AdminAuditCandidate:
        return AdminAuditCandidate(
            **self.model_dump(mode="python"), outcome=AdminAuditOutcome.DENIED
        )


class AdminAuditTerminal(Contract):
    """``append_terminal()`` 的唯一输入：两阶段操作的终态。

    **只有 operation 与终态本身。** action、target、actor、作用域一律从已存的
    ``STARTED`` 事件读回——让调用方再传一遍，就等于允许 STARTED 与终态记着
    不同的动作或不同的对象，而那样的一对事件比没有事件更容易误导读者。
    """

    operation_id: StrictStr = Field(min_length=1, max_length=48)
    outcome: Literal[AdminAuditOutcome.SUCCEEDED, AdminAuditOutcome.FAILED]
    reason_code: AdminAuditReasonCode | None = None
    effect: AdminAuditEffect = AdminAuditEffect()

    def as_candidate(self, *, started: AdminAuditEvent) -> AdminAuditCandidate:
        """用 ``started`` 的稳定字段 + 自己的终态字段合成候选。"""
        stable = started.model_dump(
            mode="python",
            include={
                "tenant_id",
                "environment_id",
                "actor_user_id",
                "actor",
                "auth_source",
                "action",
                "target_kind",
                "target_ref_digest",
            },
        )
        return AdminAuditCandidate(
            operation_id=self.operation_id,
            outcome=self.outcome,
            reason_code=self.reason_code,
            effect=self.effect,
            **stable,
        )


__all__ = [
    "DIRECTORY_ACTIONS",
    "ROLE_EFFECT_ACTIONS",
    "STATUS_EFFECT_ACTIONS",
    "AdminAuditCandidate",
    "AdminAuditDenial",
    "AdminAuditEffect",
    "AdminAuditEvent",
    "AdminAuditStart",
    "AdminAuditTerminal",
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
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
```

---

### Task 4: schema 表与 rev_0014 迁移

**Files:**
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_admin_audit.py`
- Modify: `tests/contract/test_schema_matches_migration.py`（发布哈希登记 + `rev_0014` chain + head 锚点）
- Modify: `tests/unit/test_readiness.py`（head 锚点改为从 Alembic 取，不再写死 revision 字面量）
- Modify: `tests/integration/test_migration_paths.py`（真实 PostgreSQL 上的 `rev_0014` 降级守卫用例）
- Test: `tests/contract/test_identity_schema.py`

**Interfaces:**
- Consumes: Task 2/3 的枚举值（作为 CHECK 约束字面量）
- Produces: `USER_ACCOUNTS`、`USER_ROLE_ASSIGNMENTS`、`EXTERNAL_IDENTITIES`、`ADMIN_AUDIT_EVENTS` 四张表；`local_admins.user_id` 列

**`operation_id` 为什么不是全局 UNIQUE：** 规格 §14.2 要求非事务性配置操作写 `STARTED → SUCCEEDED/FAILED` 两条事件，它们**属于同一个 operation**。全局唯一会让第二条终态事件必然撞约束；换一个 operation id 又无法证明两条事件属于同一次操作。正确的表达是两条 partial unique index：同一 operation 下 `started` 至多一条、终态至多一条。目录回滚证明照样成立——重复终态事件仍然是真实冲突。

- [ ] **Step 1: 写失败测试**

创建 `tests/contract/test_identity_schema.py`：

```python
"""身份目录与审计表的结构不变量——离线，不需要数据库。"""

import re

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


def _partial_where(index: sa.Index) -> str | None:
    """partial unique index 的 WHERE；``None`` 表示这条 index 是全局唯一的。"""
    where = index.dialect_options["postgresql"].get("where")
    return None if where is None else str(where)


def _unique_column_sets(table: sa.Table) -> set[frozenset[str]]:
    """**全局**唯一的列组合。

    带 ``postgresql_where`` 的 index 只在满足条件的行之间唯一，不是全局唯一。
    把它算进来，下面"``operation_id`` 不全局唯一"那条断言就恒假——区分这两者
    正是本文件要钉的事实，所以这里必须按 WHERE 判断，不能只看 ``index.unique``。
    """
    sets: set[frozenset[str]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            sets.add(frozenset(column.name for column in constraint.columns))
    for index in table.indexes:
        if index.unique and _partial_where(index) is None:
            sets.add(frozenset(column.name for column in index.columns))
    return sets


def _partial_unique_indexes(table: sa.Table) -> dict[str, str]:
    return {
        index.name: where
        for index in table.indexes
        if index.unique and (where := _partial_where(index)) is not None
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


def test_the_unique_helper_tells_partial_indexes_apart_from_global_ones() -> None:
    """helper 自证。

    这条用例存在的理由是：上一版的 ``_unique_column_sets`` 只看 ``index.unique``，
    把两条 ``operation_id`` partial unique index 也算成全局唯一，于是
    ``test_operation_id_is_not_globally_unique`` 恒假——Task 4 按计划执行必然红灯。
    不区分这两者，上面那两条断言就一条恒假、一条恒真，两条都不再证明任何事。
    """
    probe = sa.Table(
        "probe",
        sa.MetaData(),
        sa.Column("scoped", sa.Text, primary_key=True),
        sa.Column("whole", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Index(
            "uq_probe_partial",
            "scoped",
            unique=True,
            postgresql_where=sa.text("outcome = 'started'"),
        ),
        sa.Index("uq_probe_global", "whole", unique=True),
    )
    assert _unique_column_sets(probe) == {frozenset({"whole"})}
    assert set(_partial_unique_indexes(probe)) == {"uq_probe_partial"}


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
        "ck_admin_audit_events_directory_actions_are_single_phase",
    } <= names


def test_the_single_phase_check_lists_exactly_the_directory_actions() -> None:
    """SQL 字面量与 Python 闭集必须逐值一致。

    CHECK 里写的是字符串，导不进 Python；两边各自维护迟早漂移，而漂移的方向
    一定是 CHECK 少列一个动作——于是那个动作就能走两阶段写绕过 ``apply()``。
    """
    from xiaowei_agent.contracts.admin_audit import DIRECTORY_ACTIONS

    (constraint,) = [
        item
        for item in ADMIN_AUDIT_EVENTS.constraints
        if getattr(item, "name", None)
        == "ck_admin_audit_events_directory_actions_are_single_phase"
    ]
    listed = set(re.findall(r"'([a-z_]+)'", str(constraint.sqltext)))
    assert listed - {"started"} == {action.value for action in DIRECTORY_ACTIONS}


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
    sa.CheckConstraint(
        "NOT (outcome = 'started' AND action IN ('user_created', "
        "'user_status_changed', 'role_assigned', 'role_revoked', "
        "'external_identity_bound', 'external_identity_unbound', "
        "'local_admin_bootstrapped', 'legacy_identity_migrated'))",
        name="ck_admin_audit_events_directory_actions_are_single_phase",
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

创建 `src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_admin_audit.py`：

```python
"""Add the identity directory, admin audit log and link local credentials.

Revision ID: 0014_identity_admin_audit
Revises: 0013_clarification_parent
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

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
        sa.CheckConstraint(
            "NOT (outcome = 'started' AND action IN ('user_created', "
            "'user_status_changed', 'role_assigned', 'role_revoked', "
            "'external_identity_bound', 'external_identity_unbound', "
            "'local_admin_bootstrapped', 'legacy_identity_migrated'))",
            name="ck_admin_audit_events_directory_actions_are_single_phase",
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
    if not context.is_offline_mode():
        require_destructive_authorization(
            op.get_bind(),
            guarded=(
                (_USER_ACCOUNTS, "user_accounts"),
                (_ADMIN_AUDIT_EVENTS, "admin_audit_events"),
            ),
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

**调用形状逐字取自既有迁移，不是凭印象写的。** `require_destructive_authorization`
的真实签名是 `(connection: Connection, *, guarded: Sequence[tuple[FromClause, str]])`
（`persistence/migrations/guards.py:48`），`rev_0013_clarification_parent.py:79` 是最近
一条用它的迁移，形状就是上面这一段。V8 的计划这里写的是
`require_destructive_authorization("rev_0014 downgrade ...")`——一个位置字符串，既没有
`connection` 也没有 `guarded`。**按原文跑，有数据时降级抛的是 `TypeError` 而不是
`MigrationSafetyError`**：守卫看起来在，实际上从未生效，而"显式授权后才允许删授权事实"
这条规则会连同它一起失效。

一并删掉的还有手工写的 `has_accounts` / `has_events` 预检：`_row_counts`
（`guards.py:26`）已经在数这两张表，守卫自己就有"没有受保护行就直接返回"的分支
（`guards.py:55`）。手工预检不但重复，还把 `counts` 这个**分类计数**降级成了一个布尔，
而 `counts` 正是既有用例断言的那个值（`test_migration_paths.py:414` 等处都断言
`exc_info.value.counts`）。

`if not context.is_offline_mode():` 是既有迁移的统一写法：离线模式（`--sql`）下没有可
执行的连接，读表会直接炸。

`guarded` 的两个类别名 `user_accounts` / `admin_audit_events` 就是断言里会出现的字符串，
顺序即 `counts` 的顺序。

- [ ] **Step 5: 同步随新 revision 断裂的既有锚点**

新增一条 revision 会让三处既有断言转红。它们**不是**被新功能破坏的，而是本来就按
"当前最新 revision"写的锚点；漏掉任何一处，Task 4 按计划执行就是红灯。先跑一次
确认它们确实红，再改：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/contract/test_schema_matches_migration.py tests/unit/test_readiness.py -q 2>&1 | tail -15
```

预期 RED 两条：`test_latest_declared_revision_is_the_alembic_head`（声明的是 `0013`，
真实 head 已是 `0014`）与 `test_readiness_requires_database_head_and_assembly`
（写死 `"0013_clarification_parent"`，与真实 head 不等，`revision_matches_head` 变成
`False`）。

**锚点一——新增 chain 测试**，与既有 `rev_0012` / `rev_0013` 同形，加在
`tests/contract/test_schema_matches_migration.py` 里 `rev_0013` 那条之后：

```python
def test_rev_0014_has_the_expected_revision_chain() -> None:
    from xiaowei_agent.persistence.migrations.versions import (
        rev_0014_identity_admin_audit as revision,
    )

    assert revision.revision == "0014_identity_admin_audit"
    assert revision.down_revision == "0013_clarification_parent"
```

**锚点二——head 锚点前移到 `rev_0014`**，同一文件：

```python
def test_latest_declared_revision_is_the_alembic_head() -> None:
    from xiaowei_agent.persistence.migrations.versions import (
        rev_0014_identity_admin_audit as revision,
    )

    assert ScriptDirectory.from_config(_alembic_config()).get_current_head() == (
        revision.revision
    )
```

这一处**保留**写死 revision：它要钉的正是"最新声明的 revision 就是 head"，把两边
都改成从 Alembic 取就成了 `head == head` 的同义反复。

**锚点三——`tests/unit/test_readiness.py` 改成从 Alembic 取 head。** 这一处与上一处
相反，必须做根因修复：它要钉的事实是"当前 revision 等于 head 时探针报 `True`"，与
具体是哪个 revision 无关。写死字面量把它变成了每加一次迁移就静默转红的锚点——而这种
锚点最终会被人随手改数字，改到某天数字对了、事实错了也看不出来。

```python
from alembic.script import ScriptDirectory

from xiaowei_agent.persistence.migrations.runner import alembic_config


@pytest.mark.asyncio
async def test_readiness_requires_database_head_and_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    monkeypatch.setattr(database, "_current_revision", lambda _: head)
    connection = _Connection(revision=head)
    probe = PostgresReadinessProbe(engine=_Engine(connection), assembled=True)
    report = await probe.check()
    assert report.database_ok is True
    assert report.revision_matches_head is True
    assert report.assembled is True
    assert connection.statements == ["SELECT 1"]
```

改完重跑上面那条命令，预期全绿。

**隔离变异（确认锚点三仍然有效）：** 把 `_Connection(revision=head)` 改成
`_Connection(revision=head + "-stale")`，该用例必须转红；确认后还原。不做这一步，
就分不清"从 Alembic 取 head"是修好了断言，还是把断言变成了恒真。

- [ ] **Step 6: 登记发布哈希并跑离线一致性**

```bash
python - <<'HASH'
import hashlib, pathlib
p = pathlib.Path(
    "src/xiaowei_agent/persistence/migrations/versions/"
    "rev_0014_identity_admin_audit.py"
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

- [ ] **Step 7: 真实 PostgreSQL 上验证降级守卫**

上一步全是离线的：它证明 DDL 两边一致，**不**证明守卫会在有数据时拦住降级。守卫的
调用形状一旦写错（V8 就写错了），离线用例一条都不会红——因为离线用例根本不执行
`downgrade()`。所以这条必须在真库上跑。

追加到 `tests/integration/test_migration_paths.py`（与既有的
`test_rev_0012_downgrade_requires_authorization_for_clarification_records` 并排，
同一套 `clean_database` / `alembic_runners` fixture）：

```python
async def _seed_identity_and_audit(engine: AsyncEngine) -> None:
    """一条账号事实加一条它的审计，两者都必须被守卫数到。"""
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO user_accounts "
                "(user_id, actor, display_name, status, created_at, updated_at) "
                "VALUES ('usr-guarded', 'guarded', 'Guarded', 'active', now(), now())"
            )
        )
        await connection.execute(
            sa.text(
                "INSERT INTO admin_audit_events "
                "(event_id, operation_id, tenant_id, environment_id, actor_user_id, "
                "actor, auth_source, action, target_kind, target_ref_digest, outcome, "
                "reason_code, effect_role, effect_status, created_at) VALUES "
                "('evt-guarded', 'op-guarded', 'dev-local', 'dev', 'usr-local-admin', "
                "'admin', 'local_admin', 'user_created', 'user', :digest, "
                "'succeeded', NULL, 'user', 'active', now())"
            ),
            {"digest": "e" * 64},
        )


async def test_rev_0014_downgrade_requires_authorization_for_identity_and_audit(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    """授权事实与"它为什么变成这样"的唯一记录，都不能被一次无声降级删掉。"""
    await _seed_identity_and_audit(clean_database)
    run_upgrade, run_downgrade = alembic_runners

    with pytest.raises(MigrationSafetyError) as exc_info:
        async with clean_database.begin() as connection:
            await connection.run_sync(run_downgrade, "0013_clarification_parent")
    assert exc_info.value.counts == (
        ("user_accounts", 1),
        ("admin_audit_events", 1),
    )

    # 拒绝之后：版本没动，两张表的行一行都没少。
    async with clean_database.connect() as connection:
        revision = await connection.scalar(
            sa.text("SELECT version_num FROM alembic_version")
        )
        accounts = await connection.scalar(
            sa.text("SELECT count(*) FROM user_accounts")
        )
        events = await connection.scalar(
            sa.text("SELECT count(*) FROM admin_audit_events")
        )
    assert revision == _head_revision()
    assert (accounts, events) == (1, 1)

    # 显式授权后才真的删，删完能重新升回来。
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0013_clarification_parent", True)
    dropped = await _table_names(clean_database)
    assert not {
        "user_accounts",
        "user_role_assignments",
        "external_identities",
        "admin_audit_events",
    } & dropped

    async with clean_database.begin() as connection:
        await connection.run_sync(run_upgrade, "head")
    restored = await _table_names(clean_database)
    assert {
        "user_accounts",
        "user_role_assignments",
        "external_identities",
        "admin_audit_events",
    } <= restored
    async with clean_database.connect() as connection:
        columns = await connection.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'local_admins'"
            )
        )
    assert "user_id" in {row[0] for row in columns}


async def test_rev_0014_downgrade_needs_no_authorization_on_an_empty_directory(
    clean_database: AsyncEngine,
    alembic_runners: tuple[Any, Any],
) -> None:
    """正常对照：没有受保护数据时，降级不该要授权。

    没有这一条，上一条用例无法区分"守卫按数据拦住了"和"守卫无条件拦住一切"。
    """
    _, run_downgrade = alembic_runners
    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0013_clarification_parent")
    assert "user_accounts" not in await _table_names(clean_database)
```

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/integration/test_migration_paths.py -q 2>&1 | tail -5
```

预期：全绿。若第一条报 `TypeError` 而不是 `MigrationSafetyError`，就是守卫调用形状又
写错了——回 Step 4 对着 `guards.py:48` 改实现，**不要**把 `pytest.raises` 改成
`TypeError`。

- [ ] **Step 8: 提交**

```bash
git add src/xiaowei_agent/persistence/schema.py \
        src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_admin_audit.py \
        tests/contract/test_schema_matches_migration.py \
        tests/contract/test_identity_schema.py \
        tests/integration/test_migration_paths.py \
        tests/unit/test_readiness.py
git commit -m "feat(w1a): add identity directory and admin audit tables in rev_0014

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
```

---

### Task 5: 内存实现与"审计由命令派生"的单一写路径

这是整个 W1a 的承重任务。

**Files:**
- Create: `src/xiaowei_agent/persistence/identity.py`
- Create: `src/xiaowei_agent/persistence/admin_audit.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/persistence/local_admin.py`（`LocalAdminRecord` 加 `user_id` 字段）
- Modify: `tests/conftest.py`
- Create: `tests/suites/identity_directory.py`
- Create: `tests/contract/test_identity_store.py`

**Interfaces:**
- Consumes: Task 2/3 的契约；既有 `InMemoryPersistenceState`、`Clock`
- Produces: `UserDirectoryStore` Protocol（`load_account` / `resolve_by_subject` / `apply`）、`AdminAuditStore` Protocol（`append_started` / `append_terminal` / `append_denied` / `load`）、`AdminAuditStart` / `AdminAuditTerminal` / `AdminAuditDenial`、`InMemoryUserDirectoryStore`、`InMemoryAdminAuditStore`、`ACTION_FOR_COMMAND`、`EFFECT_FOR_COMMAND`、`external_subject_digest`、`IDENTITY_DIRECTORY_CASES`

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

from typing import Any, Protocol

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

__all__ = ["IDENTITY_DIRECTORY_CASES", "LocalAdminProbe", "bind"]

_TENANT = "dev-local"
_ENV = "dev"
_OPEN_ID_A = "ou_" + "aaaa11112222"
_OPEN_ID_B = "ou_" + "bbbb33334444"
_FAKE_HASH = "argon2id$" + "v=19$m=65536,t=3,p=4$c29tZXNhbHQ"
_ROTATED_HASH = "argon2id$" + "v=19$m=65536,t=3,p=4$cm90YXRlZHNhbHQ"
"""代表『管理员已经改过密码』的那个哈希；backfill 不得把它覆盖掉。"""


class LocalAdminProbe(Protocol):
    """直接读写 ``local_admins`` 的**测试专用**探针。

    为什么需要它：bootstrap 的四态里有三态的前置条件是『数据库里已经有一行
    旧形状的凭据』。生产代码没有、也不该有制造这种状态的方法——那会是第二条
    写路径。所以它由 fixture 提供，两个绑定各给一份实现（内存版写
    ``state.local_admin``，PostgreSQL 版直接发 SQL），都住在测试代码里，不进
    ``src/``，因此不受 Task 7 写路径守卫的约束。
    """

    async def seed_legacy_credential(
        self, *, password_hash: str, user_id: str | None = None
    ) -> None:
        """写一行 ``rev_0014`` 之前形状的凭据（``user_id`` 默认为 ``NULL``）。"""

    async def linked_user_id(self) -> str | None: ...

    async def stored_password_hash(self) -> str: ...


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


def _bootstrap_command() -> BootstrapLocalAdminCommand:
    return BootstrapLocalAdminCommand(
        user_id="usr-local-admin",
        actor="admin",
        display_name="Local Admin",
        tenant_id=_TENANT,
        environment_id=_ENV,
        password_hash=_FAKE_HASH,
    )


async def test_bootstrap_is_idempotent_and_writes_no_audit_when_nothing_changed(
    user_directory: Any,
) -> None:
    """链接完整时什么都没改，就不该有审计。

    第二次 bootstrap 返回空元组而不是抛错，保留了 ``seed_if_absent`` 现有的
    『已 seed 返回 False』语义；也不会撞上终态唯一索引。
    """
    command = _bootstrap_command()
    assert len(await user_directory.apply(command=command, context=_context("b1"))) == 1
    assert await user_directory.apply(command=command, context=_context("b2")) == ()


async def test_bootstrap_backfills_a_credential_that_has_no_directory_account(
    user_directory: Any, local_admins: Any
) -> None:
    """已有凭据行但没有目录链接 → 补齐，**不动原密码**。

    这条用例存在的理由是：上一版的规则是『``local_admins`` 已有行就返回空元组』。
    任何跑过 RI5 的数据库都已经有这一行（``local_stack.py:1031`` 每次装配都调
    ``seed_if_absent``），于是升级到 ``rev_0014`` 之后 ``user_id`` 永远为空、
    账号与 ADMIN 角色永远不出现，而全新安装的用例照样全绿。
    """
    await local_admins.seed_legacy_credential(password_hash=_ROTATED_HASH)

    events = await user_directory.apply(
        command=_bootstrap_command(), context=_context("b-backfill")
    )

    assert len(events) == 1
    assert events[0].action is AdminAuditAction.LOCAL_ADMIN_BOOTSTRAPPED
    assert events[0].effect.role is ProductRole.ADMIN
    facts = await user_directory.load_account(
        user_id="usr-local-admin", tenant_id=_TENANT, environment_id=_ENV
    )
    assert facts is not None and facts.assignment.role is ProductRole.ADMIN
    assert await local_admins.linked_user_id() == "usr-local-admin"
    # 管理员可能早就改过密码；backfill 拿初始口令覆盖回去是一次静默凭据回滚。
    assert await local_admins.stored_password_hash() == _ROTATED_HASH


async def test_bootstrap_backfill_is_itself_idempotent(
    user_directory: Any, local_admins: Any
) -> None:
    """补齐之后再来一次就是第三行：no-op，不写第二条审计。"""
    await local_admins.seed_legacy_credential(password_hash=_ROTATED_HASH)
    await user_directory.apply(
        command=_bootstrap_command(), context=_context("b-first")
    )
    assert (
        await user_directory.apply(
            command=_bootstrap_command(), context=_context("b-second")
        )
        == ()
    )


async def test_bootstrap_fails_closed_when_the_link_points_at_nothing(
    user_directory: Any, local_admins: Any
) -> None:
    """第四行：链接存在但指向的账号不存在 → 不猜、不重建，直接拒绝。

    这里**不能**退回 backfill：``user_id`` 非空说明某一次 bootstrap 自认为
    成功过，账号却不在了。这种状态下重建一个 ADMIN 账号，等于用一次静默修复
    掩盖一次没人知道的数据丢失。
    """
    await local_admins.seed_legacy_credential(
        password_hash=_ROTATED_HASH, user_id="usr-vanished"
    )
    with pytest.raises(UserDirectoryConflictError):
        await user_directory.apply(
            command=_bootstrap_command(), context=_context("b-broken")
        )


async def test_bootstrap_fails_closed_when_the_linked_account_lost_admin(
    user_directory: Any, local_admins: Any
) -> None:
    """第四行的另一半：账号在，但在本作用域没有 ADMIN 角色。"""
    await _seed(user_directory, "usr-local-admin", actor="admin")
    await local_admins.seed_legacy_credential(
        password_hash=_ROTATED_HASH, user_id="usr-local-admin"
    )
    await user_directory.apply(
        command=RevokeRoleCommand(
            tenant_id=_TENANT, environment_id=_ENV, user_id="usr-local-admin"
        ),
        context=_context("b-revoke"),
    )
    with pytest.raises(UserDirectoryConflictError):
        await user_directory.apply(
            command=_bootstrap_command(), context=_context("b-noadmin")
        )


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


async def test_a_second_event_at_the_same_stage_is_rejected(admin_audit: Any) -> None:
    """直连 ``AdminAuditStore`` 时的阶段重复 → ``AdminAuditConflictError``。

    这是**矩阵一第五行**的正面用例，配对的是
    ``test_audit_write_failure_rolls_back_the_authorization_change``——同一条
    数据库约束，两个不同的公开入口，因此两个不同的错误族：谁的公开方法失败，
    就用谁的错误族。

    上一版这里放的是"复用 ``operation_id`` 调 ``apply()``"，与上面那条回滚用例
    **是同一个场景**，却期望另一个异常类型。两条不可能同时通过，而这正是"错误
    语义散在多个章节各写一遍"的代价。现在 ``apply()`` 侧只有那一条，本条改成
    覆盖直连路径——它原本是缺的。

    用 ``append_denied`` 而不是 ``append_started``：两者撞的是同一条终态 partial
    unique index，而 ``append_denied`` 不需要先有 ``STARTED``，用例里没有第二个
    前置条件。
    """
    from xiaowei_agent.contracts.admin_audit import (
        AdminAuditDenial,
        admin_audit_target_digest,
    )
    from xiaowei_agent.contracts.enums import AdminAuditReasonCode, AdminAuditTargetKind
    from xiaowei_agent.persistence.admin_audit import AdminAuditConflictError

    def _denial() -> Any:
        return AdminAuditDenial(
            operation_id="op-dup-stage",
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
            reason_code=AdminAuditReasonCode.ACTOR_NOT_ADMIN,
        )

    await admin_audit.append_denied(denial=_denial())
    with pytest.raises(AdminAuditConflictError):
        await admin_audit.append_denied(denial=_denial())


async def test_a_denied_event_records_the_refusal_without_claiming_an_effect(
    admin_audit: Any,
) -> None:
    """``append_denied`` 是 W1a 唯一可用的写方法，也写不出"成功"。

    W1b 的激活拒绝路径直接复用它——这正是 `ADR-013:131` 说的"W1b 作为消费者
    复用 W1a 的写契约"。
    """
    from xiaowei_agent.contracts.admin_audit import (
        AdminAuditDenial,
        admin_audit_target_digest,
    )
    from xiaowei_agent.contracts.enums import AdminAuditReasonCode, AdminAuditTargetKind

    event = await admin_audit.append_denied(
        denial=AdminAuditDenial(
            operation_id="op-denied",
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
            reason_code=AdminAuditReasonCode.ACTOR_NOT_ADMIN,
        )
    )
    assert event.outcome is AdminAuditOutcome.DENIED
    assert event.effect.role is None and event.effect.status is None


async def test_a_terminal_event_without_a_started_event_is_refused(
    admin_audit: Any,
) -> None:
    """终态的稳定字段从 ``STARTED`` 读回；没有 STARTED 就没有来源，fail-closed。

    这条是 ``append_terminal`` 不能变成通用 append 的那道结构保证：允许它凭空
    写一条终态，action、target、actor 就只能由调用方再传一遍，于是伪造回来了。
    """
    from xiaowei_agent.contracts.admin_audit import AdminAuditTerminal
    from xiaowei_agent.persistence.admin_audit import AdminAuditMissingStartError

    with pytest.raises(AdminAuditMissingStartError):
        await admin_audit.append_terminal(
            terminal=AdminAuditTerminal(
                operation_id="op-never-started",
                outcome=AdminAuditOutcome.SUCCEEDED,
                effect=_role_effect(),
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
    test_bootstrap_backfills_a_credential_that_has_no_directory_account,
    test_bootstrap_backfill_is_itself_idempotent,
    test_bootstrap_fails_closed_when_the_link_points_at_nothing,
    test_bootstrap_fails_closed_when_the_linked_account_lost_admin,
    test_legacy_migration_writes_the_whole_batch_or_nothing,
    test_legacy_migration_emits_one_audit_event_per_entry,
    test_a_second_event_at_the_same_stage_is_rejected,
    test_a_denied_event_records_the_refusal_without_claiming_an_effect,
    test_a_terminal_event_without_a_started_event_is_refused,
)

ALL_GROUPS = {"identity_directory": IDENTITY_DIRECTORY_CASES}
```

创建 `tests/contract/test_identity_store.py`：

```python
"""UserDirectoryStore 内存绑定、协议窄度、命令映射完备性与内存侧回滚。"""

import inspect
from typing import Any

import pytest
from tests.suites.identity_directory import (
    IDENTITY_DIRECTORY_CASES,
    _bootstrap_command,
    _context,
    _create,
    bind,
)

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

# ``_context`` / ``_create`` / ``_bootstrap_command`` 从套件模块导入，不在这里再造一
# 份：本文件最后那条回滚用例必须和套件用例落在**同一组数据**上，否则它证明的是另一
# 组数据，而不是套件里那条不变量。手搓一个命令还有个更直接的风险——V5 就是凭记忆给
# ``ChangePasswordCommand`` 传了两个不存在的字段。

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
    """表面恰好是三个窄写方法 + 一个读方法。

    ``==`` 而不是 ``<=``：多出来的任何一个方法都要重新论证它写不出目录成功
    事实，所以新增必须显式改这条断言。
    """
    assert {name for name in dir(AdminAuditStore) if not name.startswith("_")} == {
        "append_started",
        "append_terminal",
        "append_denied",
        "load",
    }


def test_every_command_has_a_declared_action_and_effect() -> None:
    """新增命令而忘了声明动作或 effect，必须炸在这里。"""
    assert set(ACTION_FOR_COMMAND) == _ALL_COMMANDS
    assert set(EFFECT_FOR_COMMAND) == _ALL_COMMANDS


async def test_any_failure_after_a_directory_write_leaves_nothing_behind(
    user_directory: Any, admin_audit: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """承重用例：内存实现的回滚**不挑异常类型**。

    上一版只在 ``UserDirectoryError`` 与 ``AdminAuditConflictError`` 时恢复快照。
    审计派生、契约校验或 helper 里抛出的任何其他异常，都会让内存实现停在"授权
    已改、审计没写"的半状态——而 PostgreSQL 那边整个事务会回滚。两个实现于是
    在共享套件之外悄悄分叉，分叉的那一格恰好是本计划最核心的不变量。

    这里注入的是 ``RuntimeError``：它既不是 ``UserDirectoryError``，也不是
    ``AdminAuditConflictError``，正好落在上一版那两个 except 之外。

    **为什么不在共享套件里**：故障注入点两边不同（内存是 ``_append_locked``，
    PostgreSQL 是 ``_insert_audit_event``），放进共享套件就要再造一套注入机制。
    PostgreSQL 侧的同一条语义由事务本身保证，已由
    ``test_rolled_back_change_leaves_no_row_in_either_table`` 在真库上证明。
    """

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("audit helper exploded")

    await user_directory.apply(
        command=_bootstrap_command(), context=_context("op-bootstrap")
    )
    before = _directory_snapshot(user_directory)

    monkeypatch.setattr(admin_audit, "_append_locked", _boom)
    with pytest.raises(RuntimeError, match="audit helper exploded"):
        await user_directory.apply(
            command=_create("usr-1", actor="alice"), context=_context("op-boom")
        )

    assert _directory_snapshot(user_directory) == before, (
        "四张授权事实必须逐张回到调用前的样子"
    )


def _directory_snapshot(store: Any) -> tuple[Any, ...]:
    """四张授权事实 + 审计事实的可比较快照。

    逐张取而不是只查 ``user_accounts``：上一版的半状态里，账号、角色、身份绑定与
    ``local_admins.user_id`` 各自可能留下残留，只查一张会漏掉另外三张。
    """
    state = store._state
    return (
        dict(state.user_accounts),
        dict(state.user_role_assignments),
        dict(state.external_identities),
        state.local_admin,
        dict(state.admin_audit_events),
        set(state.admin_audit_stage_keys),
    )
```

- [ ] **Step 2: 运行，确认 RED 指向缺模块**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_identity_store.py -q 2>&1 | tail -10
```

预期：`ModuleNotFoundError: No module named 'xiaowei_agent.persistence.identity'`。

- [ ] **Step 3: 写 `persistence/admin_audit.py`**

**为什么是三个窄方法，不是一个 `append(candidate)`，也不是只读。**

批准真源要求 W1a 就交付写契约，不能推迟：

- `DEVELOPMENT_PLAN.md:178`：W1a 交付「`AdminAuditStore` 的持久化与 **append-only 写契约**」；
- `ADR-013:131`：「交付顺序固定：**W1a** 先提供持久化与 append-only 写契约，**W1b** 才作为消费者复用它」；
- `ADR-013:129`：非事务性的配置面动作使用 `STARTED → SUCCEEDED/FAILED` 两阶段写。

同时，一个接受完整 `AdminAuditCandidate` 的通用 `append()` 是不能留的：它让任何
调用方都能写一条 `ROLE_ASSIGNED + SUCCEEDED` 而不曾改动任何角色。

两者并不矛盾——需要的是**窄**到写不出目录成功事实的写契约：

| 方法 | 为什么伪造不了目录成功事实 |
| --- | --- |
| `append_started` | `AdminAuditStart` 没有 `outcome` 字段，落库恒为 `STARTED`；而 `ck_admin_audit_events_directory_actions_are_single_phase` 禁止目录动作写 `STARTED`，直接被数据库拒绝 |
| `append_terminal` | 只收 `operation_id` 与终态字段；action、target、actor、scope 全部从**已存的 STARTED 行**读回。目录动作根本不可能有 STARTED 行，因此这条路对目录动作不可达 |
| `append_denied` | `AdminAuditDenial` 连 `effect` 字段都没有，`outcome` 恒为 `DENIED`，表达不出「成功」 |

于是「目录成功事实只能由 `apply()` 派生」这条保证依然成立，而 W1b 有契约可复用。
上面那条 CHECK 因此不只是纵深防御——**它正是让 `append_terminal` 的读回派生变安全的那块结构**。

```python
"""append-only 管理面审计的存储契约与内存实现。

写契约是三个**窄**方法，不是一个通用 ``append(candidate)``。理由见上表：
通用 append 会让调用方凭空写出一条「授权成功」，而窄方法在结构上写不出来。
"""

from __future__ import annotations

import uuid
from typing import Final, Protocol

from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditDenial,
    AdminAuditEvent,
    AdminAuditStart,
    AdminAuditTerminal,
)
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


class AdminAuditMissingStartError(AdminAuditError):
    """终态事件找不到对应的 ``STARTED``。

    fail-closed 而不是补写一条：能凭空写终态，``append_terminal`` 的稳定字段
    就没有可读回的来源，它也就退化成了通用 append。
    """


class AdminAuditStore(Protocol):
    """append-only：三个窄写方法 + 一个读方法。

    **没有 update、没有 delete、没有清理**——规格 §14.2 的明文要求：能删的
    审计在事后争议里等于没有审计。

    **没有通用 ``append(candidate)``**：它会让调用方凭空写出一条授权成功事实。
    三个窄方法各自在结构上写不出目录成功事实（见本步开头的表）。
    """

    async def append_started(self, *, start: AdminAuditStart) -> AdminAuditEvent: ...

    async def append_terminal(
        self, *, terminal: AdminAuditTerminal
    ) -> AdminAuditEvent: ...

    async def append_denied(self, *, denial: AdminAuditDenial) -> AdminAuditEvent: ...

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

    async def append_started(self, *, start: AdminAuditStart) -> AdminAuditEvent:
        async with self._state.lock:
            return self._append_locked(start.as_candidate())

    async def append_denied(self, *, denial: AdminAuditDenial) -> AdminAuditEvent:
        async with self._state.lock:
            return self._append_locked(denial.as_candidate())

    async def append_terminal(
        self, *, terminal: AdminAuditTerminal
    ) -> AdminAuditEvent:
        """稳定字段从已存的 ``STARTED`` 事件读回，不从调用方拿。"""
        async with self._state.lock:
            started = self._started_locked(terminal.operation_id)
            if started is None:
                raise AdminAuditMissingStartError("operation has no started event")
            return self._append_locked(terminal.as_candidate(started=started))

    def _started_locked(self, operation_id: str) -> AdminAuditEvent | None:
        for event in self._state.admin_audit_events.values():
            if (
                event.operation_id == operation_id
                and event.outcome is AdminAuditOutcome.STARTED
            ):
                return event
        return None

    def _append_locked(self, candidate: AdminAuditCandidate) -> AdminAuditEvent:
        """已持锁时的追加；``InMemoryUserDirectoryStore`` 在自己的临界区内复用它。

        复用而不是复制：两份追加逻辑会在"什么算冲突"上分叉，而这正是目录
        回滚证明所依赖的那条约束。

        **它接受完整候选，因此是本模块里唯一一处能写出任意审计事实的地方。**
        上一版把它叫 ``append_locked``（公开命名），并在这段注释里声称"Task 7
        的 AST 守卫要求它只在目录 store 里被调用"——**那个守卫当时并不存在**。
        一段声称保护存在的注释比没有注释更糟：它让下一个读代码的人停止追问。
        现在它是私有命名，并且真的有一条调用点守卫
        （``test_identity_write_path.py::test_the_full_candidate_helpers_have_only_declared_call_sites``）
        把调用点冻结在目录派生路径与三个窄方法上。
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
    "AdminAuditMissingStartError",
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
    事务里提交。失败与拒绝不经过它——它们没有授权改变可绑定，因此 W1a 根本
    不写这两类事件。要写它们需要一个能凭空构造审计事实的写方法，而那正是
    上一轮复审判定必须去掉的东西。
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
            except Exception as exc:
                # 内存实现没有事务，因此显式恢复快照。
                #
                # **先恢复，再决定抛什么。** 恢复不挑异常类型：PostgreSQL 那边
                # 回滚的是整个事务，事务不会问"这是哪一类异常"。上一版只在
                # ``UserDirectoryError`` 与 ``AdminAuditConflictError`` 时恢复，
                # 于是审计派生、契约校验或 helper 抛出的任何其他异常——
                # ``ValidationError``、``KeyError``、一个 helper 里的
                # ``RuntimeError``——都会让内存实现停在"授权已改、审计没写"的
                # 半状态上，而 PostgreSQL 会回滚。两个实现在共享套件上语义分叉，
                # 而分叉的那一格恰好是本计划最核心的那条不变量。
                self._restore(snapshot)
                if isinstance(exc, AdminAuditConflictError):
                    raise AdminAuditUnwritableError(
                        "admin audit event could not be written"
                    ) from None
                raise
```

`_apply_locked` 按命令类型分流，全部复用 `derive_audit()` 与 `self._audit._append_locked()`。

`_snapshot`/`_restore` 覆盖 **Step 6 新增的五个字段，外加既有的 `state.local_admin`**（`memory.py:46`）：四个 dict 与一个 set 做浅拷贝，`local_admin` 是单个记录引用、直接存取。**漏掉 `local_admin` 是一个真实缺陷**——bootstrap 在同一次调用里既写凭据又写目录，审计写失败时若只回滚目录、不回滚凭据，就会留下一行 `user_id IS NULL` 的凭据；而那正好落进上面第二行（backfill）而不是第四行（fail-closed），于是一次失败的 bootstrap 看起来会像一次『还没升级』的正常状态。

分流规则：

- `CreateUserCommand`：`user_id` 已存在或 `actor` 被占用 → `UserDirectoryConflictError`；否则写账号 + 角色 + 一条审计。
- `SetUserStatusCommand` / `AssignRoleCommand` / `RevokeRoleCommand` / `BindExternalIdentityCommand` / `UnbindExternalIdentityCommand`：目标账号或角色不存在 → `UserDirectoryNotFoundError`。
- `BindExternalIdentityCommand`：摘要已绑到别的账号、或该账号在本作用域已有绑定 → `UserDirectoryConflictError`。
- `BootstrapLocalAdminCommand`：按下面的四态规则分流。判定读的是 `state.local_admin` 与目录侧的账号/角色，不是『有没有凭据行』这一个布尔。
- `MigrateLegacyIdentitiesCommand`：逐条写账号 + 角色 + 绑定，每条用 `batch_operation_id(context, index)` 写一条审计；任一条冲突整批抛错，由上面的快照恢复保证零部分落库。

**bootstrap 的判定依据是目录链接是否完整，不是凭据行是否存在。**

| `local_admins` 行 | `user_id` | 目录侧 | 处置 |
| --- | --- | --- | --- |
| 无 | — | — | 建凭据 + 账号 + ADMIN 角色 + 链接 + 一条审计 |
| 有 | `NULL` | — | **保留原 `password_hash` 不动**，补账号 + ADMIN 角色 + 链接 + 一条审计 |
| 有 | 指向存在且在该作用域持 ADMIN 角色的账号 | 完整 | 幂等 no-op，返回 `()`，不写审计 |
| 有 | 指向不存在的账号，或该账号没有 ADMIN 角色 | 破损 | `UserDirectoryConflictError`（fail-closed） |

第二行是这版新增的，也是上一版真正的缺陷所在。上一版的规则是『`local_admins`
已有行 → 立即返回空元组』。而 `local_stack.py:1031` 在**每次**装配 Web 时都调用
`seed_if_absent`，任何跑过 RI5 的数据库早就有这一行了。于是升级到 `rev_0014`
之后：`local_admins.user_id` 永远是 `NULL`，`user_accounts` 不会出现，ADMIN
角色不会出现，bootstrap 审计不会出现——而全新安装的测试全绿，因为全新安装
走的是第一行。这是一条只在真实升级路径上才踩得到的裂缝。

保留原 `password_hash` 是硬要求：管理员可能早就改过密码，backfill 拿传进来的
初始口令覆盖回去，等于一次静默的凭据回滚。命令里的 `password_hash` **只在
第一行**（凭据行不存在）被使用。

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


class _InMemoryLocalAdminProbe:
    """``LocalAdminProbe`` 的内存实现。"""

    def __init__(self, state: Any) -> None:
        self._state = state

    async def seed_legacy_credential(
        self, *, password_hash: str, user_id: str | None = None
    ) -> None:
        self._state.local_admin = LocalAdminRecord(
            password_hash=password_hash, must_change_password=True, user_id=user_id
        )

    async def linked_user_id(self) -> str | None:
        return None if self._state.local_admin is None else self._state.local_admin.user_id

    async def stored_password_hash(self) -> str:
        assert self._state.local_admin is not None
        return self._state.local_admin.password_hash


@pytest.fixture
def local_admins(persistence_state: Any) -> _InMemoryLocalAdminProbe:
    return _InMemoryLocalAdminProbe(persistence_state)
```

`persistence_state` 与 `clock` 用该文件已有的同名 fixture；若名字不同，按实际名字改，**不要**新建一份 state。

修改 `src/xiaowei_agent/persistence/local_admin.py`：`LocalAdminRecord`
（`local_admin.py:51`，当前只有 `password_hash` 与 `must_change_password` 两个字段）
加一个字段，与 `local_admins.user_id` 列对应。

```python
class LocalAdminRecord(Contract):
    """本地管理员的持久化事实；只有哈希，没有明文。"""

    password_hash: SecretHash = Field(exclude=True, repr=False)
    must_change_password: bool
    user_id: StrictStr | None = None
```

`StrictStr | None`，不是 `str | None`：`Contract` 是 strict 模式，其余字段也都用契约
层的严格别名。

**默认 `None` 是为了不惊动 9 处 `seed_if_absent` 调用方，但它同时让每一个漏改的构造点
都安静地返回 `None`。** 这不是理论风险：`None` 在四态表里正好是第二行（backfill），
一个漏回填的读取路径会让"数据库里已经链接好了"看起来像"还没升级"。所以本任务不靠记忆，
把构造点**全部列出来**——下面这张表由 AST 发现，Task 0 机械核对第 6 条会把它和真实发现
集合做 `==` 比对，将来任何人新增一个构造点都会让那条核对变红。

| 构造点（`相对路径::owner`） | 当前传入 | 本轮必须变成 |
| --- | --- | --- |
| `persistence/fake.py::InMemoryLocalAdminStore.seed_if_absent` | `password_hash`, `must_change_password` | Task 6 Step 5 改为委托 `apply()`，不再自己构造 |
| `persistence/fake.py::InMemoryLocalAdminStore.change_password_and_rotate_session` | `password_hash`, `must_change_password` | 补 `user_id=self._state.local_admin.user_id` |
| `persistence/local_admin.py::PostgresLocalAdminStore.get` | `password_hash`, `must_change_password` | 补 `user_id=row["user_id"]` |
| `persistence/local_admin.py::PostgresLocalAdminStore.change_password_and_rotate_session` | `password_hash`, `must_change_password` | 补 `user_id`，取自 UPDATE 的 `RETURNING` |

后三行都在 Task 6 落地（那一步才改 PostgreSQL 实现），本任务只负责加字段本身；但清单
写在这里，因为**加字段的是这一步**，而"加了字段没跟到读取路径"正是这一类缺陷的发生
时刻。第四行尤其要注意：`change_password_and_rotate_session` 的返回值**不是**从库里读
出来的，而是用命令字段现搭的（`local_admin.py:168`），命令里根本没有 `user_id`——照原样
加字段，它每次改密都返回 `user_id=None`，而数据库里那一列好端端地还在。这就是"数据库
已关联、Store 返回未关联"的分叉，两个实现的语义会在这里各走各的。

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
        src/xiaowei_agent/persistence/local_admin.py \
        tests/suites/identity_directory.py \
        tests/contract/test_identity_store.py \
        tests/conftest.py
git commit -m "feat(w1a): derive admin audit from the command on a single write path

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
```

---

### Task 6: PostgreSQL 实现、真实 fixture 与同事务证明

**Files:**
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`（审计事件的双向列映射，与既有 `row_to_*` 并排）
- Modify: `src/xiaowei_agent/persistence/local_admin.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`（`InMemoryLocalAdminStore.seed_if_absent` 同样改为委托）
- Modify: `src/xiaowei_agent/contracts/identity.py`（五个 bootstrap 常量的唯一真源）
- Modify: `src/xiaowei_agent/interfaces/local_admin_auth.py`（改为导入那五个常量，删掉自己那三个字面量）
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `tests/integration/conftest.py`
- Create: `tests/integration/test_identity_directory_postgres.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/contract/test_identity_contracts.py`（常量只有一份）
- Modify: `tests/contract/test_identity_schema.py`（冲突目标钉死 + 行映射往返）

**Interfaces:**
- Consumes: Task 4 的表、Task 5 的协议、派生层与共享套件
- Produces: `PostgresUserDirectoryStore`、`PostgresAdminAuditStore`（四个方法与内存实现同 Protocol）、模块级 `_insert_audit_event`、`_conflict` / `_audit_unwritable`；`persistence/rows.py` 的 `admin_audit_event_to_row` / `row_to_admin_audit_event`；`seed_if_absent` 改为走 `apply()`

**fixture 必须显式定义。** 仓库的共享套件靠 `tests/integration/conftest.py` 用**同名** fixture 覆盖根 conftest 的内存实现（`store`、`plan_store`、`clarification_record_store` 都是这样）。只写 `bind(...)` 而不定义同名 fixture，集成文件会安静地继续跑内存实现，"PostgreSQL 同事务证据"就是假绿。另外该 conftest 里**没有** `engine` fixture，可用的是 `migrated_engine`（session 级）与 `clean_database`（函数级，每个用例清库）。

- [ ] **Step 1: 加 PostgreSQL fixture**

追加到 `tests/integration/conftest.py`（按 `clarification_record_store` 的既有写法）：

```python
# 下面四个名字并进该文件**既有的**导入块，不新开一段：
# PostgresAdminAuditStore / PostgresUserDirectoryStore 进已有的
# `from xiaowei_agent.persistence.postgres import (...)`；
# LOCAL_ADMINS 进已有的 `from xiaowei_agent.persistence.schema import (...)`；
# LOCAL_ADMIN_SINGLETON_ID 需要新增一行
# `from xiaowei_agent.persistence.local_admin import LOCAL_ADMIN_SINGLETON_ID`。
from xiaowei_agent.persistence.local_admin import LOCAL_ADMIN_SINGLETON_ID
from xiaowei_agent.persistence.postgres import (
    PostgresAdminAuditStore,
    PostgresUserDirectoryStore,
)
from xiaowei_agent.persistence.schema import LOCAL_ADMINS


@pytest.fixture
def admin_audit(clean_database: AsyncEngine, clock: Any) -> PostgresAdminAuditStore:
    return PostgresAdminAuditStore(engine=clean_database, clock=clock)


@pytest.fixture
def user_directory(
    clean_database: AsyncEngine, clock: Any
) -> PostgresUserDirectoryStore:
    return PostgresUserDirectoryStore(engine=clean_database, clock=clock)


class _PostgresLocalAdminProbe:
    """``LocalAdminProbe`` 的 PostgreSQL 实现；直接发 SQL，测试专用。"""

    def __init__(self, engine: AsyncEngine, clock: Any) -> None:
        self._engine = engine
        self._clock = clock

    async def seed_legacy_credential(
        self, *, password_hash: str, user_id: str | None = None
    ) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                sa.insert(LOCAL_ADMINS).values(
                    id=LOCAL_ADMIN_SINGLETON_ID,
                    password_hash=password_hash,
                    must_change_password=True,
                    updated_at=self._clock(),
                    user_id=user_id,
                )
            )

    async def _column(self, column: sa.Column[Any]) -> Any:
        async with self._engine.connect() as connection:
            return await connection.scalar(
                sa.select(column).where(LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID)
            )

    async def linked_user_id(self) -> str | None:
        return await self._column(LOCAL_ADMINS.c.user_id)

    async def stored_password_hash(self) -> str:
        return await self._column(LOCAL_ADMINS.c.password_hash)


@pytest.fixture
def local_admins(clean_database: AsyncEngine, clock: Any) -> _PostgresLocalAdminProbe:
    return _PostgresLocalAdminProbe(clean_database, clock)
```

三者都接 `clean_database`，因此它们操作的是**同一个** engine、同一个库——这是"同事务"用例有意义的前提。

`LOCAL_ADMIN_SINGLETON_ID` 是 `persistence/local_admin.py` 里现有的 `_SINGLETON_ID`；把它改成不带下划线的公开常量并从 `local_admin.py` 导出，**不要**在测试里重写一遍字面量。`updated_at` 取根 `conftest.py` 的 `clock` fixture（`tests/fakes/clock.py::ManualClock`，固定在 2026-09-02 UTC）——V8 这里写的是 `_FIXED_TIME`，而这个名字在两个 conftest 里都不存在，按原文跑是 `NameError`。

- [ ] **Step 2: 写 PostgreSQL 绑定测试**

创建 `tests/integration/test_identity_directory_postgres.py`：

```python
"""UserDirectoryStore PostgreSQL 共享绑定与真实事务证明。"""

import datetime as _dt
from typing import Any

import pytest
import sqlalchemy as sa
from tests.suites.identity_directory import IDENTITY_DIRECTORY_CASES, bind

from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
)
from xiaowei_agent.contracts.identity import CreateUserCommand
from xiaowei_agent.persistence.admin_audit import AdminAuditStore
from xiaowei_agent.persistence.errors import (
    PersistenceIntegrityCategory,
    PersistenceIntegrityError,
    PersistenceWriteOutcome,
)
from xiaowei_agent.persistence.identity import AdminAuditUnwritableError
from xiaowei_agent.persistence.postgres import (
    PostgresAdminAuditStore,
    PostgresUserDirectoryStore,
    _write_transaction,
)
from xiaowei_agent.persistence.schema import (
    ADMIN_AUDIT_EVENTS,
    LOCAL_ADMINS,
    USER_ACCOUNTS,
    USER_ROLE_ASSIGNMENTS,
)

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

再在同一个文件追加一条用例，钉死"未知约束不是业务冲突"。

它用到的 `_dt`、三个枚举、三个错误类型与 `_write_transaction` **已经在上面那个导入块里
了**——上一版把它们写成一个单独的补充块，还附了一句"`AdminAuditOutcome` 该文件已经
导入，不重复"，而那个名字其实从未导入过，用例会 `NameError`。补充块与"已经导入"这类
说明都是同一个毛病的两种形态：文件被当成片段来写，就没有人在看它拼起来之后的样子。
本计划现在每个文件只有一份导入块，Task 0 第 4 条机械核对按段落标记把它们拼回文件再查
一遍未定义名。

`_write_transaction` 是 `postgres.py` 的私有名；测试导入它在本仓库有先例，而这里需要的
恰恰是它那条"按事务退出的事实判定 `write_outcome`"的行为。

**这条用例为什么不走 store 的公开方法**：走不了。`AdminAuditStart` 的
`_two_phase_is_not_for_directory_actions` 校验器在契约层就把这个组合拒了，`_write`
根本收不到它。要证明"未被探测吞掉的约束落到哪一格"，只能从数据库那一侧造——而
这也正是这条 CHECK 存在的理由：它防的是绕过契约层直接写库的人。

```python
async def test_an_unknown_constraint_is_not_dressed_up_as_a_business_conflict(
    admin_audit: PostgresAdminAuditStore, clean_database: Any
) -> None:
    """矩阵一倒数第二行：未知约束 → ``PersistenceIntegrityError``。

    直连 ``connection`` 写一条违反
    ``ck_admin_audit_events_directory_actions_are_single_phase`` 的行——这是
    契约层拦不住、只有直连数据库才造得出的形态，也正是"未知约束"在真实运维里
    的样子。``ON CONFLICT DO NOTHING`` **不吞** CHECK 违反，因此它会穿过探测、
    落到 ``_write_transaction``，收敛成 ``PersistenceIntegrityError``。

    上一版把这一格映射成了 ``AdminAuditConflictError``，理由写的是"理论上不可达，
    留作兜底"。一条不可达的映射不是兜底，是一句不会被任何用例证伪的话。
    """
    with pytest.raises(PersistenceIntegrityError) as caught:
        # 用 _write_transaction 而不是 clean_database.begin()：这条用例要断言的
        # write_outcome 正是它按事务退出的事实判定出来的，换成裸 begin() 就只
        # 剩一个 write_outcome=None 的分类，证明不了『失败时调用方知不知道有
        # 没有落库』。
        async with _write_transaction(clean_database) as connection:
            await connection.execute(
                sa.insert(ADMIN_AUDIT_EVENTS).values(
                    event_id="evt-illegal",
                    operation_id="op-illegal",
                    tenant_id="dev-local",
                    environment_id="dev",
                    actor_user_id="usr-admin",
                    actor="admin",
                    auth_source=IdentitySource.LOCAL_ADMIN.value,
                    # 目录动作 + STARTED：CHECK 禁止的组合。
                    action=AdminAuditAction.USER_CREATED.value,
                    target_kind=AdminAuditTargetKind.USER.value,
                    target_ref_digest="0" * 64,
                    outcome=AdminAuditOutcome.STARTED.value,
                    reason_code=None,
                    effect_role=None,
                    effect_status=None,
                    created_at=_dt.datetime.now(tz=_dt.UTC),
                )
            )
    assert caught.value.category is PersistenceIntegrityCategory.CONSTRAINT
    assert caught.value.write_outcome is PersistenceWriteOutcome.ROLLED_BACK
    assert caught.value.__cause__ is None, "异常链必须切断：原文里带着被拒的那一行"
    assert "admin_audit_events" not in str(caught.value)
```

追加到 `tests/contract/test_identity_schema.py`（Task 4 建的那个文件）：

这一组是**离线**用例，是"未知约束不得被伪装成业务冲突"这条风险的真正防线——运行期
不做判别，防线就必须是静态的。

**注意它不属于上面那个集成测试文件。** 段落标题写的是哪个路径，下面的代码块就进哪个
文件；Task 0 第 4 条机械核对按这个标记拼文件，标记写错会被它抓出来。

该文件 Task 4 建立时只导入了 `re` 与 `sqlalchemy`，这一组要用 `pytest.mark.parametrize`
与 `Final`，因此**先补两行导入**：

```python
from typing import Final

import pytest
```

然后追加：

```python
_CONFLICT_TARGETS: Final[dict[str, set[frozenset[str]]]] = {
    "user_accounts": {
        frozenset({"user_id"}),  # 主键：账号已存在
        frozenset({"actor"}),  # uq_user_accounts_actor：actor 被别人占了
    },
    "user_role_assignments": {
        frozenset({"user_id", "tenant_id", "environment_id"}),  # 主键
    },
    "external_identities": {
        # 主键：这个 subject 在本作用域已绑到某个账号
        frozenset({"provider", "tenant_id", "environment_id", "subject_ref_digest"}),
        # uq_external_identities_one_subject_per_account：该账号在本作用域已有绑定
        frozenset({"provider", "tenant_id", "environment_id", "user_id"}),
    },
    "admin_audit_events": {
        frozenset({"event_id"}),  # 主键
        # 两条偏唯一索引都在 operation_id 上：同阶段已有事实
        frozenset({"operation_id"}),
    },
}
"""这四张表上，``ON CONFLICT DO NOTHING`` 会**静默吞掉**的全部列组合。

**每一格都必须在矩阵一里有一行。** 新增一条唯一约束或偏唯一索引，下面那条用例
立刻变红——因为它会被 ``ON CONFLICT`` 吞掉，而吞掉之后调用方会收到一个说"这是
业务冲突"的闭集错误。加约束的人必须先来这里回答"它撞了意味着什么"。

**按列组合而不是按约束名钉。** ``ON CONFLICT`` 推断的是列，不是名字；本仓库的
``METADATA`` 没有 naming convention，主键大多由 ``primary_key=True`` 声明、在
metadata 里 ``name is None``（真库上才由 PostgreSQL 命名为 ``<表>_pkey``）。按名
字钉会把一条能通过的断言写成恒假。两条 ``operation_id`` 偏唯一索引因此折叠成一
格——它们各自的名字与 WHERE 由
``test_one_started_and_one_terminal_event_per_operation`` 单独钉着，这里不重复。

CHECK、外键、NOT NULL **不在**这里：``ON CONFLICT DO NOTHING`` 不吞它们，它们照
抛并收敛成 ``PersistenceIntegrityError``（上面那条集成用例证明了这一点）。
"""


def _conflict_targets(table: sa.Table) -> set[frozenset[str]]:
    """``ON CONFLICT DO NOTHING``（不带推断目标）会吞掉的列组合。

    = 主键 ∪ 全局唯一 ∪ 偏唯一索引。复用同文件的 ``_unique_column_sets`` 与
    ``_partial_where``：那两个 helper 已经把"全局唯一 vs 偏唯一"分开了，这里要
    的是两者的并集，因为实测确认不带推断目标时两者都被吞。
    """
    targets = {frozenset(column.name for column in table.primary_key)}
    targets |= _unique_column_sets(table)
    targets |= {
        frozenset(column.name for column in index.columns)
        for index in table.indexes
        if index.unique and _partial_where(index) is not None
    }
    return targets


@pytest.mark.parametrize("table_name", sorted(_CONFLICT_TARGETS))
def test_the_conflict_probed_tables_have_exactly_the_declared_targets(
    table_name: str,
) -> None:
    """``==`` 而不是 ``<=``：多一格少一格都必须来这里显式回答。"""
    table = next(t for t in ALL_TABLES if t.name == table_name)
    assert _conflict_targets(table) == _CONFLICT_TARGETS[table_name]


def test_the_conflict_target_helper_sees_both_kinds_of_unique_index() -> None:
    """helper 自证：全局唯一与偏唯一都必须被算进来。

    ``_unique_column_sets`` **有意**把偏唯一索引排除在外（它服务的是"operation_id
    不全局唯一"那条断言）。直接拿它当冲突目标集，两条 ``operation_id`` 偏索引就
    会漏掉——而那正是审计表上最要紧的两条。这条用例钉住这次并集没有漏。
    """
    probe = sa.Table(
        "probe_conflict_targets",
        sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("whole", sa.Text),
        sa.Column("part", sa.Text),
        sa.UniqueConstraint("whole", name="uq_probe_whole"),
        sa.Index(
            "uq_probe_part", "part", unique=True, postgresql_where=sa.text("part <> ''")
        ),
    )
    assert _unique_column_sets(probe) == {frozenset({"whole"})}
    assert _conflict_targets(probe) == {
        frozenset({"id"}),
        frozenset({"whole"}),
        frozenset({"part"}),
    }
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

    @_persistence_boundary(write=True)
    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]:
        # 一个事务，目录写入与审计 INSERT 都在里面。**不要**在这里开第二个连接或
        # 第二个事务：那正是"权限改了、审计没记上"能够发生的唯一形态。
        #
        # 用 _write_transaction 而不是裸 engine.begin()：它按事务退出的事实区分
        # ROLLED_BACK 与 NOT_CONFIRMED。一次授权改变失败时，调用方必须能知道到底
        # 有没有落库。
        #
        # 这里**没有** except：已知冲突在事务体内就由 ON CONFLICT 探测出来并抛成
        # 闭集领域错误，剩下的由 _write_transaction 收敛。见矩阵三。
        async with _write_transaction(self._engine) as connection:
            return await self._apply_in_transaction(connection, command, context)
```

**这里不能写 `except sa.exc.IntegrityError`。** 上一版写了，而它**不可达**：
`_write_transaction` 的 `except Exception` 已经先把 `IntegrityError` 交给
`classify_persistence_exception`，换成了 `PersistenceIntegrityError`。实测确认
（见"这三张表依据的实测事实"）：该 `except` 分支永远不会命中，外层看到的是
`PersistenceIntegrityError(constraint, rolled_back)`，`__cause__` 为 `None`。
一段不可达的保护比没有保护更糟——它让读代码的人以为这条路径已经被处理了。

**也不能按约束名分流。** 上一版写的是
`getattr(getattr(exc.orig, "diag", None), "constraint_name", None)`，那是 psycopg
的接口；本仓库用的是 asyncpg，`exc.orig` 是
`sqlalchemy.dialects.postgresql.asyncpg.IntegrityError`，身上只有 `.sqlstate`
/ `.pgcode`，**没有 `.diag`**。那行代码在真库上恒为 `""`，于是每一次冲突——
包括审计冲突——都会落进 `UserDirectoryConflictError`，审计回滚用例会以错误的
异常类型"通过"。约束名只在 `exc.orig.__cause__` 上，而那条链正是
`_persistence_boundary` 要切断的东西。

**已知冲突改用 `ON CONFLICT DO NOTHING ... RETURNING` 在事务体内探测**，这是
矩阵三，也是仓库里既有的三处写法。分流函数因此整个删掉，取而代之的是一个把
"探测不到行"翻成闭集错误的小助手：

```python
def _conflict(command: DirectoryCommand) -> UserDirectoryError:
    """已知目录唯一约束冲突 → 矩阵一第一行。

    固定消息、不带命令内容：``command`` 里有 actor 与 ``subject_ref`` 摘要，
    进了消息就等于把受控 PII 写进日志。参数只用来在 mypy 上钉住调用点确实
    在处理一条命令，不进消息。
    """
    return UserDirectoryConflictError("directory command conflicts with stored facts")


def _audit_unwritable() -> AdminAuditUnwritableError:
    """审计事实写不进去 → 矩阵一第三行。授权改变由事务回滚。"""
    return AdminAuditUnwritableError("admin audit event could not be written")
```

`_apply_in_transaction` 的规则：

- 按命令类型发 INSERT/UPDATE/DELETE，全部用传入的 `connection` 与同一个 `now`。
- **每一条 INSERT 都是 `sa.dialects.postgresql.insert(...).on_conflict_do_nothing().returning(<主键列>)`，拿不到行就抛矩阵一对应的那个错。** 目录表拿不到行 → `_conflict(command)`；`ADMIN_AUDIT_EVENTS` 拿不到行 → `_audit_unwritable()`。这两个 `raise` 发生在事务体内，因此本事务里已经发出的授权写入随之回滚——这就是"审计写不进去必须回滚授权改变"的实现，不需要任何额外机制。
- **`on_conflict_do_nothing()` 不带推断目标**：一条 INSERT 可能撞上该表的任意一条唯一约束（`user_accounts` 上是主键与 `actor` 唯一索引两条），带目标时只推断一条，另一条会漏成 `PersistenceIntegrityError`。不带目标时三种唯一冲突全吞，而 CHECK、外键、NOT NULL 照抛——正是我们想要的分界。见矩阵三与实测表。
- 目标账号或角色不存在（UPDATE/DELETE 影响 0 行）→ 抛 `UserDirectoryNotFoundError`，**在同一事务内**，因此审计也不会留下。
- 每条成功改动用 Task 5 的 `derive_audit()` 生成候选，再交给模块级的 `_insert_audit_event(connection, candidate, now=now)`——**不要在这里另写一条 `sa.insert(ADMIN_AUDIT_EVENTS)`**。它返回 `None` 就抛 `_audit_unwritable()`（矩阵一第三行）；`PostgresAdminAuditStore._write` 调用同一个函数，返回 `None` 时抛 `AdminAuditConflictError`（第五行）。同一条约束、同一处落库、两个公开入口两个错误族。批量命令按 `batch_operation_id(context, index)` 逐条生成。审计 INSERT 放在各自改动之后、事务提交之前。
- `BootstrapLocalAdminCommand`：按 Task 5 那张四态表分流，与内存实现**同一套语义**。先 `SELECT id, password_hash, user_id FROM local_admins WHERE id = :singleton FOR UPDATE`——`FOR UPDATE` 是必需的：两个 Web 进程同时装配会同时调用 `seed_if_absent`，没有行锁时两边都读到 `user_id IS NULL`、都去建账号，第二个撞 `user_accounts` 主键，一次正常的并发启动变成一次启动失败。没有行时走 `INSERT ... ON CONFLICT DO NOTHING RETURNING id`；拿不到 id 说明另一个事务刚插进去，重读一次按同一张表继续分流。
- `external_identities` 存 `external_subject_digest(...)`，不存 `open_id`。
- `resolve_by_subject` 用一次 JOIN 取 account + assignment，并在 SQL 层就写 `WHERE user_accounts.status = 'active'`——把禁用判断放在 Python 里，就得依赖每个调用方都记得判一次。
- `PostgresAdminAuditStore` 实现与内存实现**同一个** Protocol：`append_started` / `append_terminal` / `append_denied` / `load`，公开表面精确等于这四个。它**没有** UPDATE/DELETE，Task 7 的 AST 守卫机械确认。

新代码用到的 `_persistence_boundary`（`postgres.py:244`）、`_write_transaction`（`:293`）、`AsyncConnection`（`:36` 已导入）、`_dt`（`:28`）、`uuid`（`:30`）、`sa.dialects.postgresql.insert`（`:2222` 等处已在用）全部是既有的，**不新增任何第三方依赖**。

需要新增的 import 只有 W1a 自己的符号，按 `postgres.py` 既有的分组追加：

```python
from xiaowei_agent.contracts.admin_audit import (
    AdminAuditCandidate,
    AdminAuditDenial,
    AdminAuditEvent,
    AdminAuditStart,
    AdminAuditTerminal,
    AdminOperationContext,
)
from xiaowei_agent.contracts.enums import AdminAuditOutcome
from xiaowei_agent.contracts.identity import DirectoryCommand
from xiaowei_agent.persistence.admin_audit import (
    AdminAuditConflictError,
    AdminAuditMissingStartError,
)
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryConflictError,
    UserDirectoryError,
    UserDirectoryNotFoundError,
    derive_audit,
)
from xiaowei_agent.persistence.rows import (
    admin_audit_event_to_row,
    row_to_admin_audit_event,
)
from xiaowei_agent.persistence.schema import (
    ADMIN_AUDIT_EVENTS,
    EXTERNAL_IDENTITIES,
    USER_ACCOUNTS,
    USER_ROLE_ASSIGNMENTS,
)
```

`AdminAuditCandidate` 必须**真的 import 进来**，不能写成字符串注解或 `Any`：Task 7 的
守卫按参数注解里出现的名字发现完整候选 helper，`_insert_audit_event` 要能被它发现。

**删掉的 import**：按约束名分流那套代码去掉之后，`sa.exc` 与 `NoReturn` 在本任务里
都不再需要，不要顺手留着。

**照同一个文件里既有的 Store 写，不要另起一套。** `persistence/postgres.py` 里每一个既有 Store 的公开方法都是这个形状：`@_persistence_boundary(write=...)` 装饰，写路径用 `_write_transaction(self._engine)`，行与契约的互转放在 `persistence/rows.py` 的 `X_to_row` / `row_to_X` 里。这不是风格问题，三件事各自承重：

| 既有机制 | 它挡住什么 |
| --- | --- |
| `_persistence_boundary`（`postgres.py:244`） | 把驱动异常收敛成闭集错误，并**切断异常链**。engine 没有开 `hide_parameters`，原始 SQLAlchemy 异常正文里带着 SQL 与绑定参数——其中有 actor 与 `target_ref_digest`。不套这层边界，一次约束冲突就能把受控 PII 写进日志 |
| `_write_transaction`（`postgres.py:293`） | 按事务退出的事实区分 `ROLLED_BACK` 与 `NOT_CONFIRMED`。直接 `engine.begin()` 的调用方无法知道一次失败到底有没有落库，而审计恰恰是最不能含糊的那张表 |
| `rows.py` 的双向映射 | 列与契约的形状差异集中在一处。数据库存的是字符串枚举与扁平的 `effect_role` / `effect_status`，契约是严格枚举与嵌套 `effect`——这个差异必须显式写出来 |

```python
class PostgresAdminAuditStore:
    """``AdminAuditStore`` 的 PostgreSQL 实现。"""

    def __init__(self, *, engine: AsyncEngine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    @_persistence_boundary(write=True)
    async def append_started(self, *, start: AdminAuditStart) -> AdminAuditEvent:
        async with _write_transaction(self._engine) as connection:
            return await self._insert_started(connection, start)

    @_persistence_boundary(write=True)
    async def append_denied(self, *, denial: AdminAuditDenial) -> AdminAuditEvent:
        async with _write_transaction(self._engine) as connection:
            return await self._insert_denial(connection, denial)

    @_persistence_boundary(write=True)
    async def append_terminal(
        self, *, terminal: AdminAuditTerminal
    ) -> AdminAuditEvent:
        """读回 ``STARTED`` 再写终态，**同一个事务**。

        读与写必须在一个事务里：跨事务时两者之间可以插进别的写，读回的稳定
        字段就不一定还是要落库那一行的来源。

        这里**不需要** ``SELECT ... FOR UPDATE``。审计表是 append-only，
        ``STARTED`` 行不会被改也不会被删，所以读到的值不会在事务内失效；
        真正的并发点是两个终态同时写，而那由终态 partial unique index 裁决——
        加行锁只会把一次干净的约束冲突换成一次锁等待，不增加任何保证。
        """
        async with _write_transaction(self._engine) as connection:
            row = (
                await connection.execute(
                    sa.select(ADMIN_AUDIT_EVENTS).where(
                        ADMIN_AUDIT_EVENTS.c.operation_id == terminal.operation_id,
                        ADMIN_AUDIT_EVENTS.c.outcome
                        == AdminAuditOutcome.STARTED.value,
                    )
                )
            ).mappings().first()
            if row is None:
                raise AdminAuditMissingStartError("operation has no started event")
            return await self._insert_terminal(
                connection, terminal, started=row_to_admin_audit_event(row)
            )

    @_persistence_boundary(write=False)
    async def load(self, *, event_id: str) -> AdminAuditEvent | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    sa.select(ADMIN_AUDIT_EVENTS).where(
                        ADMIN_AUDIT_EVENTS.c.event_id == event_id
                    )
                )
            ).mappings().first()
        return None if row is None else row_to_admin_audit_event(row)
```

**三个私有写助手各自只收自己那个窄类型，没有一个收 `AdminAuditCandidate`。**
这是上一版的缺陷：上一版写的是 `_insert(candidate)` / `_insert_on(connection, candidate)`，
两个都接受完整候选，于是 `audit._insert(forged)` 就是一条新的伪造路径——而
Task 7 的调用点守卫当时只冻结了 `_append_locked`。**私有命名不是安全边界。**
正确的做法不是把这两个名字也加进守卫名单，而是让它们**根本收不到**完整候选：

```python
    async def _insert_started(
        self, connection: AsyncConnection, start: AdminAuditStart
    ) -> AdminAuditEvent:
        return await self._write(connection, start.as_candidate())

    async def _insert_denial(
        self, connection: AsyncConnection, denial: AdminAuditDenial
    ) -> AdminAuditEvent:
        return await self._write(connection, denial.as_candidate())

    async def _insert_terminal(
        self,
        connection: AsyncConnection,
        terminal: AdminAuditTerminal,
        *,
        started: AdminAuditEvent,
    ) -> AdminAuditEvent:
        return await self._write(connection, terminal.as_candidate(started=started))

    async def _write(
        self, connection: AsyncConnection, candidate: AdminAuditCandidate
    ) -> AdminAuditEvent:
        """三个窄写方法共用的落库口，调用点由 Task 7 守卫冻结。

        它只做一件事：把 ``_insert_audit_event`` 的"没插进去"翻成
        ``AdminAuditStore`` 这个公开入口该抛的错（矩阵一第五行）。目录路径调用
        同一个 ``_insert_audit_event``，翻成的是 ``AdminAuditUnwritableError``。
        """
        event = await _insert_audit_event(connection, candidate, now=self._clock())
        if event is None:
            raise AdminAuditConflictError(
                "admin audit event conflicts with a stored event"
            )
        return event


async def _insert_audit_event(
    connection: AsyncConnection, candidate: AdminAuditCandidate, *, now: _dt.datetime
) -> AdminAuditEvent | None:
    """把候选变成事实并落库；同阶段事实已存在时返回 ``None``。

    **`persistence/` 里唯一一处 `INSERT INTO admin_audit_events`。** 目录路径与
    审计 store 都走它，因此"什么算阶段重复"只有一份定义。上一版是两处各写一条
    INSERT：两条 INSERT 迟早会在 ON CONFLICT 目标、列映射或冲突判定上分叉，而
    目录回滚证明依赖的正是这两条必须是同一条。

    **返回 ``None`` 而不是抛错**：调用它的公开入口不同，错误族就不同（矩阵一
    第三行与第五行）。这个函数不知道自己是被谁调的，所以它不做这个决定——
    做了就等于把两个入口的语义焊死成一个。
    """
    event = AdminAuditEvent(
        **candidate.model_dump(mode="python"),
        event_id=uuid.uuid4().hex,
        created_at=now,
    )
    inserted = (
        await connection.execute(
            sa.dialects.postgresql.insert(ADMIN_AUDIT_EVENTS)
            .values(admin_audit_event_to_row(event))
            .on_conflict_do_nothing()
            .returning(ADMIN_AUDIT_EVENTS.c.event_id)
        )
    ).first()
    return None if inserted is None else event
```

**`_write` 抛的是 `AdminAuditConflictError`，不是 `AdminAuditUnwritableError`。**
矩阵一的归属规则：`_write` 服务的公开入口是 `AdminAuditStore` 的三个窄写方法。
`PostgresUserDirectoryStore._apply_in_transaction` 不调用 `_write`——它走自己的
审计 INSERT，拿不到行时抛 `_audit_unwritable()`。两条路径撞的是同一条约束，抛
不同的错，因为公开入口不同；这是有意的，不是分叉。

**上一版的三处缺陷在这里一起消失：**

| 上一版 | 事实 |
| --- | --- |
| `except sa.exc.IntegrityError` | 在 `_write_transaction` 内部时它可达（`_write` 收的是 connection），但它要读的东西不存在 |
| `exc.orig.diag.constraint_name` | asyncpg 上恒为 `None`，于是 `name` 恒为 `""`，**每一次审计冲突都会走到 `raise exc`**，变成 `PersistenceIntegrityError`——那条"阶段重复 → `AdminAuditConflictError`"的断言永远不会绿 |
| `ck_..._directory_actions_are_single_phase` 映射成 `AdminAuditConflictError` | 这条 CHECK 的违反**不是**冲突，`ON CONFLICT` 也不吞它。它现在如实落进 `PersistenceIntegrityError`——一条直连 psql 写进来的非法行是数据完整性故障，不是"你重复提交了" |

`_AUDIT_CONSTRAINTS`、`_classify_directory_integrity_error`、
`_raise_audit_conflict_or_defer` 三个名字全部删除，不再有任何一份"约束名 → 错误"
的名单需要与数据库保持同步。约束名与语义的对应改由 Task 6 Step 2 的静态用例钉死。

**行与契约的互转放 `persistence/rows.py`**，与 `row_to_channel_binding` / `channel_binding_to_row` 同名同形。

两个函数的签名都用字符串前向引用 `"AdminAuditEvent"`，因此**必须**把它加进
`rows.py:46` 那个既有的 `if TYPE_CHECKING:` 块，否则 `mypy src` 报未定义名称：

```python
if TYPE_CHECKING:
    from xiaowei_agent.contracts.admin_audit import AdminAuditEvent
    from xiaowei_agent.persistence.channel import ChannelBinding, ProjectionSubscription
    ...
```

运行期的名字由 `row_to_admin_audit_event` 内部那个函数级 import 提供——与该文件既有
写法一致（`TYPE_CHECKING` 管注解，函数内 import 管运行期）。数据库与契约的形状差异只有两处，必须显式写出来——直接 `AdminAuditEvent.model_validate(dict(row))` 会**同时**撞上两条：`Contract` 是 `strict=True`（字符串喂不进 `StrEnum` 字段，报 `is_instance_of`）且 `extra="forbid"`（`effect_role` / `effect_status` 两列报 `extra_forbidden`）。

```python
def admin_audit_event_to_row(event: "AdminAuditEvent") -> dict[str, Any]:
    """审计契约到列；嵌套 ``effect`` 摊平成两列，可空字段显式保留。"""
    return {
        "event_id": event.event_id,
        "operation_id": event.operation_id,
        "tenant_id": event.tenant_id,
        "environment_id": event.environment_id,
        "actor_user_id": event.actor_user_id,
        "actor": event.actor,
        "auth_source": event.auth_source.value,
        "action": event.action.value,
        "target_kind": event.target_kind.value,
        "target_ref_digest": event.target_ref_digest,
        "outcome": event.outcome.value,
        "reason_code": None if event.reason_code is None else event.reason_code.value,
        "effect_role": None if event.effect.role is None else event.effect.role.value,
        "effect_status": (
            None if event.effect.status is None else event.effect.status.value
        ),
        "created_at": event.created_at,
    }


def row_to_admin_audit_event(row: Mapping[Any, Any]) -> "AdminAuditEvent":
    """列到审计契约；枚举显式构造，两列重新装回嵌套 ``effect``。"""
    from xiaowei_agent.contracts.admin_audit import AdminAuditEffect, AdminAuditEvent
    from xiaowei_agent.contracts.enums import (
        AdminAuditAction,
        AdminAuditOutcome,
        AdminAuditReasonCode,
        AdminAuditTargetKind,
        IdentitySource,
        ProductRole,
        UserStatus,
    )

    return AdminAuditEvent(
        event_id=row["event_id"],
        operation_id=row["operation_id"],
        tenant_id=row["tenant_id"],
        environment_id=row["environment_id"],
        actor_user_id=row["actor_user_id"],
        actor=row["actor"],
        auth_source=IdentitySource(row["auth_source"]),
        action=AdminAuditAction(row["action"]),
        target_kind=AdminAuditTargetKind(row["target_kind"]),
        target_ref_digest=row["target_ref_digest"],
        outcome=AdminAuditOutcome(row["outcome"]),
        reason_code=(
            None
            if row["reason_code"] is None
            else AdminAuditReasonCode(row["reason_code"])
        ),
        effect=AdminAuditEffect(
            role=None if row["effect_role"] is None else ProductRole(row["effect_role"]),
            status=(
                None
                if row["effect_status"] is None
                else UserStatus(row["effect_status"])
            ),
        ),
        created_at=row["created_at"],
    )
```

`PostgresUserDirectoryStore._apply_in_transaction` 不自己发审计 INSERT，它调用模块级的 `_insert_audit_event`——`admin_audit_event_to_row` 在那里面用。上一版让两条路径各写一条 INSERT、只共享列映射；只共享一半的东西迟早在另一半上分叉，而分叉点正好是『什么算阶段重复』。

**领域错误必须穿过这两层边界，不能被收敛掉。** `classify_persistence_exception`
对普通 `RuntimeError` 子类返回 `None`，两层都会原样重抛——`AdminAuditConflictError`、
`AdminAuditMissingStartError`、`UserDirectoryConflictError` 因此都能传到调用方。
这不是巧合，是这两层设计里的一条约定；补一条用例钉住它，否则某天有人给领域错误
换个基类，整组"冲突时抛什么"的断言会集体变成 `PersistenceIntegrityError`：

```python
def test_domain_errors_pass_through_the_persistence_boundary() -> None:
    """闭集领域错误不属于持久化边界，必须原样上抛。"""
    from xiaowei_agent.persistence.errors import classify_persistence_exception

    for error in (
        AdminAuditConflictError("x"),
        AdminAuditMissingStartError("x"),
        UserDirectoryConflictError("x"),
        UserDirectoryNotFoundError("x"),
    ):
        assert classify_persistence_exception(error, write_outcome=None) is None
```

补往返用例（放在 `tests/contract/test_identity_schema.py`，离线即可，不需要数据库）：

```python
def test_an_audit_event_survives_a_round_trip_through_its_row() -> None:
    """契约 → 列 → 契约 必须是恒等的。

    这条用例存在的理由是一次真实缺陷：上一版直接
    ``AdminAuditEvent.model_validate(dict(row))``，而 ``Contract`` 是
    ``strict=True`` + ``extra="forbid"``——字符串喂不进 ``StrEnum`` 字段，
    摊平的 ``effect_role`` / ``effect_status`` 又是"多余字段"。任何一次真实
    PostgreSQL ``load()`` 都会抛 ``ValidationError``。
    """
    from xiaowei_agent.persistence.rows import (
        admin_audit_event_to_row,
        row_to_admin_audit_event,
    )

    for event in (_succeeded_event_with_effect(), _denied_event_without_effect()):
        assert row_to_admin_audit_event(admin_audit_event_to_row(event)) == event


def test_the_row_uses_plain_strings_so_the_database_sees_its_own_types() -> None:
    """反面：列里不能是枚举对象，否则"数据库存字符串"这件事没有被表达。"""
    row = admin_audit_event_to_row(_succeeded_event_with_effect())
    for column in ("auth_source", "action", "target_kind", "outcome", "effect_role"):
        assert type(row[column]) is str
    assert "effect" not in row
```


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

`InMemoryLocalAdminStore.seed_if_absent`（`fake.py:515`）必须做**同样**的委托，改成调用 `InMemoryUserDirectoryStore.apply()` 而不是自己写 `state.local_admin`。两个实现的 bootstrap 语义必须由同一段分流代码决定——各写一份，四态里迟早有一态在两边不一致，而共享套件恰恰是靠"两边行为相同"才有意义的。

**新字段要贯穿读取路径，不只是写进去。** Task 5 给 `LocalAdminRecord` 加了
`user_id`，但 `PostgresLocalAdminStore` 的两个出口都不会自己长出这个值：

修改 `src/xiaowei_agent/persistence/local_admin.py`：

```python
    async def get(self) -> LocalAdminRecord:
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        sa.select(LOCAL_ADMINS).where(
                            LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise LocalAdminNotFoundError
        return LocalAdminRecord(
            password_hash=row["password_hash"],
            must_change_password=row["must_change_password"],
            user_id=row["user_id"],
        )
```

`get()` 的 `SELECT` 本来就是 `sa.select(LOCAL_ADMINS)`（整表），新列自动在 `row` 里，
只差把它装进契约——**正因为"自动在 row 里"，漏掉这一行不会有任何报错**。

改密路径要从 `RETURNING` 里把它取回来，不能凭命令现搭：

```python
    async def change_password_and_rotate_session(
        self, *, command: ChangePasswordCommand
    ) -> LocalAdminRecord:
        now = self._clock()
        async with self._engine.begin() as connection:
            updated = (
                await connection.execute(
                    sa.update(LOCAL_ADMINS)
                    .where(LOCAL_ADMINS.c.id == LOCAL_ADMIN_SINGLETON_ID)
                    .values(
                        password_hash=command.password_hash,
                        must_change_password=False,
                        updated_at=now,
                    )
                    .returning(LOCAL_ADMINS.c.id, LOCAL_ADMINS.c.user_id)
                )
            ).first()
            if updated is None:
                raise LocalAdminNotFoundError
            # ……撤销旧 session、签发新 session 两段保持不变……
        return LocalAdminRecord(
            password_hash=command.password_hash,
            must_change_password=False,
            user_id=updated.user_id,
        )
```

`UPDATE ... RETURNING user_id` 读的是**这次改密所在的那一行**，与那条 UPDATE 在同一个
语句里，所以不存在"读完又被别人改掉"的窗口。原本这里只 `.scalar(...)` 取 `id`，现在要
两列，所以改成 `.execute(...).first()`——`updated is None` 的判空语义不变。

`InMemoryLocalAdminStore.change_password_and_rotate_session`（`fake.py:538`）同样要带上
原记录的 `user_id`：

```python
            record = LocalAdminRecord(
                password_hash=command.password_hash,
                must_change_password=False,
                user_id=self._state.local_admin.user_id,
            )
```

`LOCAL_ADMIN_SINGLETON_ID` 就是本步把 `_SINGLETON_ID` 公开后的名字（见 Step 1 说明）；
`get()` 与改密两处的 `where` 都跟着改，不要留一个私有名、一个公开名。

**五个 bootstrap 常量的唯一真源是 `contracts/identity.py`，不是"二选一"。**

上一版写的是"从 `interfaces/local_admin_auth.py` 导入或与它并置定义"。两条路都不通：

- 反向导入不通——`tests/security/test_module_layering.py:47` 规定 `persistence`
  只能导入 `contracts` / `planning` / `persistence`，`interfaces` 不在其中。
  `persistence/local_admin.py` 去导 `interfaces/local_admin_auth.py` 会直接撞这道门。
- "并置定义"就是各写一遍字面量，正是这段话自己禁止的事。

放 `contracts/identity.py`：它是最底层，`persistence` 与 `interfaces` 都能导
（`test_module_layering.py:140` 允许 `interfaces/local_admin_auth.py` 导入
`xiaowei_agent.contracts`），身份常量本来也属于契约。

追加到 `src/xiaowei_agent/contracts/identity.py`：

```python
LOCAL_ADMIN_TENANT_ID: Final[str] = "dev-local"
LOCAL_ADMIN_ENVIRONMENT_ID: Final[str] = "dev"
LOCAL_ADMIN_ACTOR: Final[str] = "admin"
LOCAL_ADMIN_USER_ID: Final[str] = "usr-local-admin"
LOCAL_ADMIN_DISPLAY_NAME: Final[str] = "Local Admin"
"""本地管理员的身份常量；`interfaces/local_admin_auth.py` 与 `persistence` 共用这一份。

前三个的值来自既有的 `LOCAL_ADMIN_PRINCIPAL`（`interfaces/local_admin_auth.py:127`）。
搬到契约层之后，`local_admin_auth.py` 改为从这里导入，**删掉**它自己那三个字面量——
留着就是两份真源，而两份真源迟早会在某次改租户名时只改一边。
"""
```

修改 `src/xiaowei_agent/interfaces/local_admin_auth.py`：

```python
LOCAL_ADMIN_PRINCIPAL: Final = AuthenticatedPrincipal(
    tenant_id=LOCAL_ADMIN_TENANT_ID,
    environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
    actor=LOCAL_ADMIN_ACTOR,
    source=IdentitySource.LOCAL_ADMIN,
    subject_ref=LOCAL_ADMIN_SUBJECT_REF,
    permissions=frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
            ChannelPermission.ADMIN_ALL_SAFE_TASKS,
        }
    ),
)
```

追加到 `tests/contract/test_identity_contracts.py`（Task 2 建的那个文件）：

```python
def test_the_local_admin_principal_consumes_the_contract_constants() -> None:
    """两处必须是同一份值，而且是靠导入同一份、不是靠巧合相等。

    这条用例存在的理由：这五个常量一旦在 interfaces 与 persistence 各写一遍，
    改一次租户名就会只改一边，而 bootstrap 建出来的 ADMIN 角色会落在另一个
    作用域——登录成功但什么都看不见，且没有任何报错。
    """
    import inspect
    from pathlib import Path

    from xiaowei_agent.contracts.identity import (
        LOCAL_ADMIN_ACTOR,
        LOCAL_ADMIN_ENVIRONMENT_ID,
        LOCAL_ADMIN_TENANT_ID,
    )
    from xiaowei_agent.interfaces import local_admin_auth
    from xiaowei_agent.interfaces.local_admin_auth import LOCAL_ADMIN_PRINCIPAL

    assert LOCAL_ADMIN_PRINCIPAL.tenant_id == LOCAL_ADMIN_TENANT_ID
    assert LOCAL_ADMIN_PRINCIPAL.environment_id == LOCAL_ADMIN_ENVIRONMENT_ID
    assert LOCAL_ADMIN_PRINCIPAL.actor == LOCAL_ADMIN_ACTOR

    source = Path(inspect.getfile(local_admin_auth)).read_text(encoding="utf-8")
    for literal in ('"dev-local"', '"dev"'):
        assert literal not in source, "接口层不得保留自己那份字面量"
```

每次调用 `seed_if_absent` 用新的 `operation_id`，因为已 bootstrap 的情况返回空元组、不写审计，不会撞终态索引。

追加到 `tests/integration/test_identity_directory_postgres.py`：

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


async def test_an_already_seeded_database_gets_its_directory_backfilled_on_upgrade(
    clean_database: Any, clock: Any, alembic_runners: tuple[Any, Any]
) -> None:
    """**真实升级路径**：rev_0013 上已 seed 的库升到 rev_0014 之后必须补齐目录。

    这条用例存在的理由是上一版计划的一个真实缺陷：bootstrap 当时的规则是
    ``local_admins`` 已有行就返回空元组。任何跑过 RI5 的库都已经有那一行，
    于是升级后 ``user_id`` 永远为 ``NULL``、账号与 ADMIN 角色永远不出现，
    而全新安装的用例照样全绿。全新安装的证据证明不了升级路径。

    降级到 `rev_0013` 再写一行**旧形状**的凭据，是这里唯一能真实复现该状态
    的办法——在 `rev_0014` 的表上插一行 ``user_id IS NULL`` 只是模仿它的形状，
    证明不了迁移本身把旧行带过来时是什么样子。
    """
    from xiaowei_agent.interfaces.local_admin_auth import hash_password
    from xiaowei_agent.persistence.local_admin import (
        LOCAL_ADMIN_SINGLETON_ID,
        PostgresLocalAdminStore,
    )

    run_upgrade, run_downgrade = alembic_runners
    rotated = hash_password("rot" + "ated-secret")

    async with clean_database.begin() as connection:
        await connection.run_sync(run_downgrade, "0013_clarification_parent")
        await connection.execute(
            sa.text(
                "INSERT INTO local_admins (id, password_hash, must_change_password, "
                "updated_at) VALUES (:id, :hash, true, now())"
            ),
            {"id": LOCAL_ADMIN_SINGLETON_ID, "hash": rotated},
        )
        await connection.run_sync(run_upgrade, "head")

    async with clean_database.connect() as connection:
        assert (
            await connection.scalar(sa.select(LOCAL_ADMINS.c.user_id))
        ) is None, "迁移本身不该猜一个 user_id 出来"

    admins = PostgresLocalAdminStore(engine=clean_database, clock=clock)
    assert await admins.seed_if_absent(password_hash=hash_password("adm" + "in")) is True

    async with clean_database.connect() as connection:
        linked = await connection.scalar(sa.select(LOCAL_ADMINS.c.user_id))
        stored = await connection.scalar(sa.select(LOCAL_ADMINS.c.password_hash))
        roles = (
            await connection.execute(sa.select(USER_ROLE_ASSIGNMENTS.c.role))
        ).scalars().all()
        actions = (
            await connection.execute(sa.select(ADMIN_AUDIT_EVENTS.c.action))
        ).scalars().all()

    assert linked is not None
    assert roles == ["admin"]
    assert actions == ["local_admin_bootstrapped"]
    # 管理员早就改过密码；backfill 不得把初始口令写回去。
    assert stored == rotated

    # 第三行：链接已完整，再来一次是 no-op，不写第二条审计。
    assert await admins.seed_if_absent(password_hash=hash_password("adm" + "in")) is False
    async with clean_database.connect() as connection:
        assert (
            await connection.scalar(
                sa.select(sa.func.count()).select_from(ADMIN_AUDIT_EVENTS)
            )
        ) == 1


async def test_changing_the_password_keeps_the_directory_link(
    clean_database: Any, clock: Any
) -> None:
    """改密改的是凭据，不该顺手把目录链接清掉。

    这是加 ``user_id`` 列引入的一处真实回归风险：改密路径重建整条
    ``LocalAdminRecord``，只要漏带 ``user_id``，改一次密码就把目录链接弄丢，
    下一次装配就会落进 fail-closed 那一行，Web 直接起不来。
    """
    from xiaowei_agent.interfaces.local_admin_auth import hash_password
    from xiaowei_agent.persistence.local_admin import (
        ChangePasswordCommand,
        PostgresLocalAdminStore,
    )

    admins = PostgresLocalAdminStore(engine=clean_database, clock=clock)
    await admins.seed_if_absent(password_hash=hash_password("adm" + "in"))
    async with clean_database.connect() as connection:
        before = await connection.scalar(sa.select(LOCAL_ADMINS.c.user_id))
    assert before is not None
    # Store 看到的和库里的必须是同一件事——只断言列，等于只测了一半。
    assert (await admins.get()).user_id == before

    rotated = await admins.change_password_and_rotate_session(
        command=ChangePasswordCommand(
            password_hash=hash_password("new" + "-secret"),
            new_session_digest="0" * 64,
            public_origin_digest="1" * 64,
            session_ttl_seconds=3600,
        )
    )

    async with clean_database.connect() as connection:
        after = await connection.scalar(sa.select(LOCAL_ADMINS.c.user_id))
    assert after == before
    assert rotated.user_id == before
    assert (await admins.get()).user_id == before
```

三条断言钉的是三件不同的事，缺一条就漏一条路径：数据库列没被清空（`after`）、
改密的**返回值**带着链接（`rotated.user_id`）、下一次**读取**也带着链接
（`get().user_id`）。只断言第一条时，`get()` 与改密返回值可以双双返回 `None` 而全绿——
那正是 V8 计划的状态。

同一组断言在内存实现上由共享套件覆盖：`tests/suites/identity_directory.py` 的
`LocalAdminProbe` 已经有 `linked_user_id()`，两个绑定读到的必须是同一个值。

上面四个字段逐字取自 `persistence/local_admin.py:58` 的当前定义（`password_hash`、`new_session_digest`、`public_origin_digest`、`session_ttl_seconds`，后者 `gt=0, le=86_400`）。V3 这里写的是 `session_digest` 与 `issued_at`——两个都不存在，按原文跑会在到达 `user_id` 断言之前就因**测试自身写错**而失败，正是 Global Constraints 里那条不合格 RED。实现时若源码已变，以源码为准，**不要**改断言迁就。

- [ ] **Step 6: 登记 Protocol 一致性**

修改 `src/xiaowei_agent/_conformance.py`：按既有写法为 `UserDirectoryStore` 与 `AdminAuditStore` 各加一条 Protocol 断言，覆盖内存与 PostgreSQL 两个实现。

`isinstance` 式的 Protocol 断言只保证实现**不少于**协议；它挡不住"多出来一个公开写方法"。追加到 `tests/contract/test_protocol_conformance.py`（再加一条**精确相等**的表面断言）：

```python
@pytest.mark.parametrize(
    ("protocol", "implementation"),
    [
        (AdminAuditStore, InMemoryAdminAuditStore),
        (AdminAuditStore, PostgresAdminAuditStore),
        (UserDirectoryStore, InMemoryUserDirectoryStore),
        (UserDirectoryStore, PostgresUserDirectoryStore),
    ],
)
def test_the_implementation_surface_equals_the_protocol_surface(
    protocol: type, implementation: type
) -> None:
    """公开表面**精确等于**协议，多一个都不行。

    这条用例存在的理由是一次真实的返工：上一版把 `AdminAuditStore` 从只读改成
    三个窄写方法时，只改了 Protocol——内存实现留着一个公开的
    ``append_locked(candidate)``（接受完整候选，能直接写 `ROLE_ASSIGNED +
    SUCCEEDED`），PostgreSQL 实现则还是只有 `load()`。两处都与协议不一致，
    而当时仓库里没有任何一条断言会因此转红。

    `isinstance` 式的 Protocol 检查只保证实现**不少于**协议，方向正好相反：
    它对"多出来一个公开写方法"完全无感。所以这里必须是 `==`。
    """
    def surface(obj: type) -> set[str]:
        return {name for name in dir(obj) if not name.startswith("_")}

    assert surface(implementation) == surface(protocol)
```

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

两条变异，各自移除一条不同的保护。**每条脚本都先断言替换真的发生了**——上一版
那条脚本只是"示意形状"，`t.replace` 即使没匹配上也静默返回原文，于是"变异后没变红"
被当成了"保护有效"。一条无法确认自己生效的变异，证明不了任何事。

```bash
cp src/xiaowei_agent/persistence/postgres.py /tmp/keep-pg.py

mutate() {  # $1 = 内嵌 python 脚本
  cp /tmp/keep-pg.py src/xiaowei_agent/persistence/postgres.py
  python - <<MUT
from pathlib import Path
p = Path("src/xiaowei_agent/persistence/postgres.py")
before = p.read_text()
$1
assert after != before, "变异没有匹配到目标代码——先修脚本，不要把它当成『保护有效』"
p.write_text(after)
MUT
  PYTHONDONTWRITEBYTECODE=1 python -m pytest \
    tests/integration/test_identity_directory_postgres.py -q 2>&1 | tail -4
}
```

**变异 A：目录写与审计写不再同事务。**

```bash
mutate 'after = before.replace(
    "        async with _write_transaction(self._engine) as connection:\n"
    "            return await self._apply_in_transaction(connection, command, context)",
    "        async with self._engine.connect() as connection:\n"
    "            await connection.execution_options(isolation_level=\"AUTOCOMMIT\")\n"
    "            return await self._apply_in_transaction(connection, command, context)",
)'
```

`AUTOCOMMIT` 让每条语句各自提交，于是目录写在审计写失败之前就已经落库。预期
`test_rolled_back_change_leaves_no_row_in_either_table` 变红，且红在
`assert accounts == []`——`usr-2` 留在库里。这正是"权限改了、审计没记上"的形态。

**变异 B：冲突不再被探测出来。**

```bash
mutate 'after = before.replace(
    "    return None if inserted is None else event",
    "    return event",
)'
```

`ON CONFLICT DO NOTHING` 吞掉冲突之后返回空，改成无条件返回 `event` 就等于宣称
"写成功了"。预期同一条用例变红，且红在 `pytest.raises(AdminAuditUnwritableError)`
——根本没有异常抛出。两条变异红在**不同的断言上**，说明这条用例同时钉着两件事。

```bash
cp /tmp/keep-pg.py src/xiaowei_agent/persistence/postgres.py && rm /tmp/keep-pg.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/integration/test_identity_directory_postgres.py -q 2>&1 | tail -3
```

还原后必须回到全绿。若某条变异没红：先确认 `assert after != before` 通过了（脚本
确实改到了代码），再去看那条用例到底钉住了什么——**不要**改用例迁就。

- [ ] **Step 9: 提交**

```bash
git add src/xiaowei_agent/persistence/postgres.py \
        src/xiaowei_agent/persistence/rows.py \
        src/xiaowei_agent/persistence/local_admin.py \
        src/xiaowei_agent/persistence/fake.py \
        src/xiaowei_agent/contracts/identity.py \
        src/xiaowei_agent/interfaces/local_admin_auth.py \
        src/xiaowei_agent/_conformance.py \
        tests/integration/conftest.py \
        tests/integration/test_identity_directory_postgres.py \
        tests/contract/test_protocol_conformance.py \
        tests/contract/test_identity_contracts.py \
        tests/contract/test_identity_schema.py
git commit -m "feat(w1a): commit identity changes and their audit in one transaction

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
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

``AdminAuditStore`` 的三个写方法都窄到写不出目录成功事实，挡的是"通过 Store 伪造授权成功"。
这里挡的是另一条路：直接在某个 persistence 模块里对这张表发 UPDATE/DELETE。
谁可以 INSERT 由 ``test_identity_write_path.py`` 一并治理——审计行和另外四张
授权事实同属一条写路径，用同一套判定，不另写一份会分叉的逻辑。
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
    assert surface == {
        "append_started",
        "append_terminal",
        "append_denied",
        "load",
    }
```

创建 `tests/security/test_identity_write_path.py`：

```python
"""授权事实与审计事实的写入口是一份**冻结的名单**，而且按属性发现、不按名字枚举。

三条守卫，各自对应一次真实返工：

1. 表写入：某个模块绕过 Store 直接对这些表发 INSERT/UPDATE/DELETE；
2. 完整候选：某处拿到一个接受完整 ``AdminAuditCandidate`` 的 helper，凭空写一条
   ``ROLE_ASSIGNED + SUCCEEDED``；
3. 目录链接列：绕过目录 store 直接写 ``local_admins.user_id``。

**判定按属性，不按我记得的那个名字，也不按我记得的那个写法。**

第 2 条 V5 把 ``_append_locked`` 这个**名字**冻结起来，随后新增的 ``_insert`` /
``_insert_on`` 同样接受完整候选，却自动逃出了名单。名字会变，属性不会：改成
"凡是签名里有 ``AdminAuditCandidate`` 参数的函数"，由 AST 自己找出来。

第 3 条 V6 枚举的是两种**写法**（``values(user_id=...)`` 与
``values({LOCAL_ADMINS.c.user_id: ...})``），而 ``values({"user_id": ...})`` 和
``values({LOCAL_ADMINS.c["user_id"]: ...})`` 生成完全相同的 UPDATE，却整条漏过。
写法也会变：改成提取 ``values()`` **实际写入的列**，再问里面有没有那一列。静态
判不出写哪一列的形式（变量键、``**`` 展开）**报警而不是放行**。

**反例调用的是守卫本身，不是守卫的副本。** V6 的反例走一个叫
``_owners_credential`` 的函数，它把判定逻辑抄了一遍——于是反例证明的是那份副本
正确，守卫本体改没改它都不知道。这一版 ``_credential_link_writes`` 多收一个
``relative`` 参数，反例因此能直接调用它：反例与被守护的实现是同一段代码。

**允许项一律 owner-qualified（精确到 ``相对路径::类.方法``）。** 三版教训：V3 按
basename 放行整个文件；V4 用 ``startswith("_")`` 放行任意私有方法；V5 的第 3 条
守卫又整体跳过了 ``identity.py`` / ``postgres.py`` 两个模块。跳过模块等于放弃在
该模块内部发现新错误入口。
"""

import ast
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "xiaowei_agent"

_AUTHZ_TABLES: Final[frozenset[str]] = frozenset(
    {"USER_ACCOUNTS", "USER_ROLE_ASSIGNMENTS", "EXTERNAL_IDENTITIES"}
)
_AUDIT_TABLE: Final[frozenset[str]] = frozenset({"ADMIN_AUDIT_EVENTS"})
_WRITE_CALLS: Final[frozenset[str]] = frozenset({"insert", "update", "delete"})

_AUTHZ_WRITE_SITES: Final[frozenset[str]] = frozenset(
    {
        "persistence/identity.py::InMemoryUserDirectoryStore._apply_locked",
        "persistence/postgres.py::PostgresUserDirectoryStore._apply_in_transaction",
    }
)
"""四张授权事实的全部合法写入点。

``apply()`` 本身不发 SQL——它开事务并委托给这两个私有助手，所以名单里是助手。
"""

_AUDIT_WRITE_SITES: Final[frozenset[str]] = frozenset(
    {"persistence/postgres.py::_insert_audit_event"}
)
"""`INSERT INTO admin_audit_events` 的**唯一**合法位置。

**不写成 `_AUTHZ_WRITE_SITES | {...}`。** 上一版那样写，等于顺带允许
`_apply_in_transaction` 与 `_apply_locked` 直接写审计表——而它们现在都走
`_insert_audit_event`，不需要这个权限。把不需要的允许项留在名单里，守卫就比它
能守的范围松一圈，而松出来的那一圈正好是"绕过唯一落库口"。

内存实现不在名单里，因为它根本不碰 SQLAlchemy：`_table_write` 匹配的是
`sa.insert(ADMIN_AUDIT_EVENTS)` 这种语句，内存实现写的是 dict。"""

_CANDIDATE_TYPE: Final[str] = "AdminAuditCandidate"

_FULL_CANDIDATE_HELPERS: Final[frozenset[str]] = frozenset(
    {
        "persistence/admin_audit.py::InMemoryAdminAuditStore._append_locked",
        "persistence/postgres.py::PostgresAdminAuditStore._write",
        "persistence/postgres.py::_insert_audit_event",
    }
)
"""签名里能收下完整 ``AdminAuditCandidate`` 的全部函数。

``==`` 而不是 ``<=``：新增一个就必须显式加进来，加的时候必须回答"它的调用点
是谁"。

``admin_audit_event_to_row`` **不在**这里：它收的是 ``AdminAuditEvent``——一条
已经带了 ``event_id`` 与 ``created_at`` 的事实，不是候选。它确实能把任意事件
摊成列，但真正落库要经过 ``sa.insert``，而那已由 ``_AUDIT_WRITE_SITES`` 冻结。
在两处都列一遍就是重复校验。
"""

_HELPER_CALL_SITES: Final[frozenset[str]] = frozenset(
    {
        "persistence/admin_audit.py::InMemoryAdminAuditStore.append_started",
        "persistence/admin_audit.py::InMemoryAdminAuditStore.append_terminal",
        "persistence/admin_audit.py::InMemoryAdminAuditStore.append_denied",
        "persistence/identity.py::InMemoryUserDirectoryStore._apply_locked",
        "persistence/postgres.py::PostgresAdminAuditStore._insert_started",
        "persistence/postgres.py::PostgresAdminAuditStore._insert_denial",
        "persistence/postgres.py::PostgresAdminAuditStore._insert_terminal",
        "persistence/postgres.py::PostgresUserDirectoryStore._apply_in_transaction",
    }
)

_CREDENTIAL_LINK_SITES: Final[frozenset[str]] = frozenset(
    {
        "persistence/identity.py::InMemoryUserDirectoryStore._apply_locked",
        "persistence/postgres.py::PostgresUserDirectoryStore._apply_in_transaction",
    }
)
"""``local_admins.user_id`` 的合法写入点；与授权表同一份。"""


def _walk(module: Path):
    """逐个产出 ``(owner, node)``，owner 是 ``类.方法`` 或函数名。"""
    tree = ast.parse(module.read_text(encoding="utf-8"))

    def scan(node: ast.AST, owner: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from scan(child, child.name)
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{owner}.{child.name}" if owner else child.name
                yield (name, child)
                yield from scan(child, name)
            else:
                yield (owner or "<module>", child)

    yield from scan(tree, "")


def _calls_at(module: Path, matcher) -> list[str]:
    """命中 ``matcher`` 的调用，标注 ``相对路径::owner``。"""
    relative = module.relative_to(_SRC).as_posix()
    found: list[str] = []
    for owner, node in _walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue  # 函数体由它自己的条目覆盖，避免重复计数
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and matcher(call):
                found.append(f"{relative}::{owner}")
    return found


def _table_write(symbols: frozenset[str]):
    def matcher(call: ast.Call) -> bool:
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _WRITE_CALLS:
            return False
        names = [arg.id for arg in call.args if isinstance(arg, ast.Name)]
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            names.append(func.value.id)
        return any(n in symbols for n in names)

    return matcher


def _annotation_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _full_candidate_helpers() -> set[str]:
    """**按签名发现**：凡是有 ``AdminAuditCandidate`` 参数的函数都算。"""
    found: set[str] = set()
    for module in sorted(_SRC.rglob("*.py")):
        relative = module.relative_to(_SRC).as_posix()
        for owner, node in _walk(module):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            arguments = [*node.args.args, *node.args.posonlyargs, *node.args.kwonlyargs]
            for argument in arguments:
                if argument.annotation is None:
                    continue
                if _CANDIDATE_TYPE in _annotation_names(argument.annotation):
                    found.add(f"{relative}::{owner}")
                    break
    return found


def _helper_names() -> frozenset[str]:
    """名单项形如 ``相对路径::owner``，owner 可能是 ``类.方法``也可能是模块级函数名。

    **先按 ``::`` 拆掉路径，再从 owner 取最后一段。** 上一版直接
    ``site.rsplit(".", 1)[-1]``：路径里本来就有点号，对
    ``persistence/postgres.py::_insert_audit_event`` 得到的是
    ``py::_insert_audit_event``——那不是任何一个函数名，于是模块级 helper 的调用点
    守卫恒不命中。名单里全是 ``类.方法`` 时这个 bug 恰好看不出来，加进第一个模块级
    helper 的那一刻它就生效了。
    """
    return frozenset(
        site.split("::", 1)[1].rsplit(".", 1)[-1] for site in _FULL_CANDIDATE_HELPERS
    )


def _helper_call_sites(module: Path, relative: str | None = None) -> list[str]:
    """调用了完整候选 helper 的位置。

    独立成函数，是为了让反例调用**守卫本身**而不是它的副本——与
    ``_credential_link_writes`` 同一个理由。
    """
    names = _helper_names()
    relative = relative or module.relative_to(_SRC).as_posix()

    def matcher(call: ast.Call) -> bool:
        return _call_name(call) in names

    found: list[str] = []
    for owner, node in _walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and matcher(call):
                found.append(f"{relative}::{owner}")
    return sorted(set(found))


def test_the_helper_names_really_are_function_names() -> None:
    """调用点守卫的全部效力都建立在这一步产出真函数名上。

    上一版它产出的是 ``py::_insert_audit_event``，于是 ``_forge()`` 调用
    ``_insert_audit_event`` 时守卫**不会**命中——一条声称存在的保护实际不存在。
    这条断言是上一版缺的那一条。
    """
    assert _helper_names() == frozenset(
        {"_append_locked", "_write", "_insert_audit_event"}
    )


def _offenders(symbols: frozenset[str], allowed: frozenset[str]) -> list[str]:
    bad: list[str] = []
    for module in sorted(_SRC.rglob("*.py")):
        bad += [s for s in _calls_at(module, _table_write(symbols)) if s not in allowed]
    return bad


def test_authorization_tables_are_written_only_at_frozen_sites() -> None:
    assert _offenders(_AUTHZ_TABLES, _AUTHZ_WRITE_SITES) == []


def test_audit_table_is_written_only_at_frozen_sites() -> None:
    assert _offenders(_AUDIT_TABLE, _AUDIT_WRITE_SITES) == []


def test_the_full_candidate_helpers_are_exactly_the_declared_ones() -> None:
    """按签名发现的结果必须**精确等于**声明的名单。

    这条用例存在的理由是一次真实缺陷：上一版只冻结了 ``_append_locked`` 这个
    名字，随后新增的 ``_insert`` / ``_insert_on`` 同样接受完整候选，却自动逃出
    了名单——``audit._insert(forged)`` 既过表写入守卫、也过 helper 守卫。
    按签名发现之后，新增一个就必须来这里显式声明。
    """
    assert _full_candidate_helpers() == set(_FULL_CANDIDATE_HELPERS)


def test_the_full_candidate_helpers_have_only_declared_call_sites() -> None:
    """能写出任意审计事实的地方，调用点必须冻结。"""
    bad: list[str] = []
    for module in sorted(_SRC.rglob("*.py")):
        bad += [
            site
            for site in _helper_call_sites(module)
            if site not in _HELPER_CALL_SITES and site not in _FULL_CANDIDATE_HELPERS
        ]
    assert bad == [], f"完整候选 helper 出现了未授权调用点：{bad}"


_CREDENTIAL_TABLE: Final[str] = "LOCAL_ADMINS"
_CREDENTIAL_LINK_COLUMN: Final[str] = "user_id"
_COLUMN_CONTAINER: Final[str] = "c"


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _is_column_container(node: ast.expr) -> bool:
    """``<表>.c``——SQLAlchemy 取列的那个容器。"""
    return isinstance(node, ast.Attribute) and node.attr == _COLUMN_CONTAINER


def _column_name_of(key: ast.expr | None) -> str | None:
    """``values()`` 的字典键指向哪一列；判不出来返回 ``None``。

    三种等价键写法都认；第四种（关键字实参）在 ``_written_columns`` 里处理。
    """
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value  # values({"user_id": v})
    if isinstance(key, ast.Attribute) and _is_column_container(key.value):
        return key.attr  # values({LOCAL_ADMINS.c.user_id: v})
    if isinstance(key, ast.Subscript) and _is_column_container(key.value):
        index = key.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, str):
            return index.value  # values({LOCAL_ADMINS.c["user_id"]: v})
    return None


def _written_columns(call: ast.Call) -> tuple[frozenset[str], bool]:
    """``values()`` **实际写入**的列名，外加"有没有静态判不出的键"。

    第二个返回值是**故意**存在的。``values({column: v})`` 里 ``column`` 是变量、
    ``values(**patch)`` 里 ``patch`` 是字典时，AST 判不出写的是哪一列。判不出就
    报出来，不是放行——一条把自己判不出的情况当成合规的守卫，等于给绕过它的人
    附了一份说明书。
    """
    names: set[str] = set()
    unresolved = False
    for keyword in call.keywords:
        if keyword.arg is None:  # values(**mapping)
            unresolved = True
        else:
            names.add(keyword.arg)  # values(user_id=v)
    for argument in call.args:
        if not isinstance(argument, ast.Dict):
            unresolved = True
            continue
        for key in argument.keys:
            resolved = _column_name_of(key)
            if resolved is None:
                unresolved = True
            else:
                names.add(resolved)
    return frozenset(names), unresolved


def _targets_credential_table(call: ast.Call) -> bool:
    """这条 ``values()`` 挂在一个写 ``LOCAL_ADMINS`` 的语句上。"""
    return any(
        isinstance(node, ast.Call)
        and _call_name(node) in _WRITE_CALLS
        and any(
            isinstance(inner, ast.Name) and inner.id == _CREDENTIAL_TABLE
            for inner in ast.walk(node)
        )
        for node in ast.walk(call)
    )


def _credential_link_writes(module: Path, relative: str | None = None) -> list[str]:
    """改动 ``local_admins.user_id`` 的地方，**按写入的列判定，不按写法判定**。

    **不能**把整张 ``LOCAL_ADMINS`` 列进 ``_AUTHZ_TABLES``：改密码是合法的，按
    规格 §14.2 就该留在 ``LocalAdminStore``——凭据不是授权。要治理的只是它的
    授权侧那一列；把整张表禁掉会误伤改密，而会误伤的守卫最终会被放宽成不守。

    上一版枚举的是两种**写法**（``values(user_id=...)`` 与
    ``values({LOCAL_ADMINS.c.user_id: ...})``），于是另外两种等价写法
    ——``values({"user_id": ...})`` 和 ``values({LOCAL_ADMINS.c["user_id"]: ...})``
    ——生成完全相同的 UPDATE，却整条漏过。枚举写法的守卫永远差一种写法；这一版
    改成提取 ``values()`` 实际写入的列，再问"里面有没有那一列"。

    ``sa.delete(LOCAL_ADMINS)`` 一并纳入：删掉整行同样解除了目录链接，它和写
    ``user_id`` 是同一个属性上的同一件事，不是两件事。
    """
    relative = relative or module.relative_to(_SRC).as_posix()
    sites: list[str] = []
    for owner, node in _walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        hit = False
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = _call_name(call)
            if name == "values":
                if not _targets_credential_table(call):
                    continue
                columns, unresolved = _written_columns(call)
                if _CREDENTIAL_LINK_COLUMN in columns or unresolved:
                    hit = True
            elif name == "delete" and any(
                isinstance(inner, ast.Name) and inner.id == _CREDENTIAL_TABLE
                for inner in ast.walk(call)
            ):
                hit = True
        if hit:
            sites.append(f"{relative}::{owner}")
    return sorted(set(sites))


def test_the_credential_link_is_written_only_at_frozen_sites() -> None:
    """``local_admins.user_id`` 是第四张授权事实，和另外三张一个待遇。

    **不跳过任何模块。** V5 整体跳过 ``identity.py`` / ``postgres.py``，于是同一个
    模块里新增一个错误的写入口也不会报警。
    """
    bad: list[str] = []
    for module in sorted(_SRC.rglob("*.py")):
        bad += [
            s for s in _credential_link_writes(module) if s not in _CREDENTIAL_LINK_SITES
        ]
    assert bad == [], f"目录链接列出现了第二条写路径：{bad}"
```

**这个文件可以在写任何 W1a 源码之前就整体跑起来。** 它只用 `ast` 与 `pathlib`，不
import 任何 W1a 运行期符号；唯一的外部依赖是 `_SRC` 指向的那棵树。因此把本节的代码块
按顺序拼成文件，就能直接 `pytest` 它：

```bash
# 对真实 src/：此时 W1a 代码还不存在，
# test_the_full_candidate_helpers_are_exactly_the_declared_ones 必然红（发现集为空），
# 其余全部应当绿。任何**别的**用例变红都是这个文件自身的问题。
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/security/test_identity_write_path.py -q
```

**这一步是 V7 缺的那一步。** V7 的这个文件里有一组互斥断言（同时要求两个目录写入点
"在" `_AUDIT_WRITE_SITES` 和 `_AUTHZ_WRITE_SITES & _AUDIT_WRITE_SITES == ∅`），而
`_helper_names()` 对 `persistence/postgres.py::_insert_audit_event` 解析出的是
`py::_insert_audit_event`——不是函数名，于是模块级 helper 的调用点守卫恒不命中。两处
都只要把整个文件跑一次就会暴露；而当时验证的是一个个片段，片段各自都对。

**反例与正常对照。** 每条反例对应某一版守卫真实放过的一种情况。

```python
def _sample(tmp_path: Path, name: str, source: str) -> Path:
    module = tmp_path / name
    module.write_text(source, encoding="utf-8")
    return module


def _owners(module: Path, matcher) -> set[str]:
    return {site.split("::", 1)[1] for site in _calls_at_relative(module, matcher)}


def _calls_at_relative(module: Path, matcher) -> list[str]:
    """反例模块不在 ``_SRC`` 下，用文件名当相对路径。"""
    found: list[str] = []
    for owner, node in _walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and matcher(call):
                found.append(f"{module.name}::{owner}")
    return found


def test_the_guard_catches_a_module_level_write_function(tmp_path: Path) -> None:
    """V3 按 basename 放行整个 ``postgres.py``，这种函数会被原样放过。"""
    module = _sample(
        tmp_path,
        "postgres.py",
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import USER_ROLE_ASSIGNMENTS\n"
        "\n"
        "async def grant_admin(connection):\n"
        "    await connection.execute(sa.insert(USER_ROLE_ASSIGNMENTS))\n",
    )
    assert _owners(module, _table_write(_AUTHZ_TABLES)) == {"grant_admin"}
    assert "persistence/postgres.py::grant_admin" not in _AUTHZ_WRITE_SITES


def test_the_guard_catches_a_new_private_write_method(tmp_path: Path) -> None:
    """V4 用 ``startswith("_")`` 放行任意私有方法——这个就会被放过。"""
    module = _sample(
        tmp_path,
        "postgres.py",
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import USER_ACCOUNTS\n"
        "\n"
        "class PostgresUserDirectoryStore:\n"
        "    async def _force_create(self, connection):\n"
        "        await connection.execute(sa.insert(USER_ACCOUNTS))\n",
    )
    assert _owners(module, _table_write(_AUTHZ_TABLES)) == {
        "PostgresUserDirectoryStore._force_create"
    }
    assert (
        "persistence/postgres.py::PostgresUserDirectoryStore._force_create"
        not in _AUTHZ_WRITE_SITES
    )


def test_the_helper_discovery_finds_a_newly_added_candidate_taker(
    tmp_path: Path,
) -> None:
    """V5 新增的 ``_insert(candidate)`` 就是这样逃出名单的。

    按名字冻结时它不在名单里，也就不受任何约束；按签名发现时它会立刻出现在
    ``_full_candidate_helpers()`` 的结果里，迫使人来声明它的调用点。
    """
    module = _sample(
        tmp_path,
        "postgres.py",
        "class PostgresAdminAuditStore:\n"
        "    async def _insert(self, candidate: AdminAuditCandidate):\n"
        "        ...\n",
    )
    discovered = {
        owner
        for owner, node in _walk(module)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            argument.annotation is not None
            and _CANDIDATE_TYPE in _annotation_names(argument.annotation)
            for argument in node.args.args
        )
    }
    assert discovered == {"PostgresAdminAuditStore._insert"}
    assert (
        "persistence/postgres.py::PostgresAdminAuditStore._insert"
        not in _FULL_CANDIDATE_HELPERS
    )


_CREDENTIAL_LINK_FORMS: Final[tuple[tuple[str, str], ...]] = (
    ("关键字", "sa.update(LOCAL_ADMINS).values(user_id=value)"),
    ("列对象键", "sa.update(LOCAL_ADMINS).values({LOCAL_ADMINS.c.user_id: value})"),
    ("字符串键", 'sa.update(LOCAL_ADMINS).values({"user_id": value})'),
    ("下标列键", 'sa.update(LOCAL_ADMINS).values({LOCAL_ADMINS.c["user_id"]: value})'),
)
"""四种写法生成**完全相同**的 UPDATE。V6 的守卫只认前两种。"""

_CREDENTIAL_LINK_OPAQUE: Final[tuple[tuple[str, str], ...]] = (
    ("变量键", "sa.update(LOCAL_ADMINS).values({column: value})"),
    ("字典展开", "sa.update(LOCAL_ADMINS).values(**patch)"),
    ("整行删除", "sa.delete(LOCAL_ADMINS)"),
)
"""静态判不出写哪一列，或不经 ``values()`` 就解除链接。判不出必须报警。"""

_CREDENTIAL_LINK_CONTROLS: Final[tuple[tuple[str, str], ...]] = (
    ("改密关键字", "sa.update(LOCAL_ADMINS).values(password_hash=value)"),
    ("改密列对象键", "sa.update(LOCAL_ADMINS).values({LOCAL_ADMINS.c.password_hash: value})"),
    ("改密字符串键", 'sa.update(LOCAL_ADMINS).values({"password_hash": value})'),
    ("别的表写 user_id", "sa.insert(USER_ROLE_ASSIGNMENTS).values(user_id=value)"),
    ("只读凭据表", "sa.select(LOCAL_ADMINS.c.user_id)"),
)
"""正常对照。改密是凭据不是授权，误报会让这条守卫被放宽成不守。"""


def _credential_sample(tmp_path: Path, expression: str) -> Path:
    return _sample(
        tmp_path,
        "local_admin.py",
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import (\n"
        "    LOCAL_ADMINS,\n"
        "    USER_ROLE_ASSIGNMENTS,\n"
        ")\n"
        "\n"
        "def link(connection, value, column, patch):\n"
        f"    return {expression}\n",
    )


@pytest.mark.parametrize(("label", "expression"), _CREDENTIAL_LINK_FORMS)
def test_every_equivalent_credential_link_write_is_caught(
    tmp_path: Path, label: str, expression: str
) -> None:
    """四种等价写法必须全部命中。

    V6 枚举的是两种**写法**，于是另外两种生成相同 UPDATE 的写法整条漏过。守卫
    现在提取 ``values()`` 实际写入的列，写法就不再是判据。
    """
    module = _credential_sample(tmp_path, expression)
    assert _credential_link_writes(module, module.name) == ["local_admin.py::link"], label


@pytest.mark.parametrize(("label", "expression"), _CREDENTIAL_LINK_OPAQUE)
def test_an_unprovable_credential_write_is_reported_not_waved_through(
    tmp_path: Path, label: str, expression: str
) -> None:
    """判不出写哪一列，就报出来。

    一条把"我判不出来"当成"它合规"的守卫，等于给绕过它的人附了一份说明书。
    ``sa.delete(LOCAL_ADMINS)`` 一并纳入：删掉整行同样解除了目录链接。
    """
    module = _credential_sample(tmp_path, expression)
    assert _credential_link_writes(module, module.name) == ["local_admin.py::link"], label


@pytest.mark.parametrize(("label", "expression"), _CREDENTIAL_LINK_CONTROLS)
def test_credential_writes_that_are_not_the_link_column_are_not_flagged(
    tmp_path: Path, label: str, expression: str
) -> None:
    """正常对照：改密、别的表、只读，一律放行。"""
    module = _credential_sample(tmp_path, expression)
    assert _credential_link_writes(module, module.name) == [], label


def test_the_credential_guard_catches_a_wrong_owner_inside_an_allowed_module(
    tmp_path: Path,
) -> None:
    """V5 整体跳过 ``postgres.py``，同模块里的错误 owner 完全不报警。"""
    module = _sample(
        tmp_path,
        "postgres.py",
        "import sqlalchemy as sa\n"
        "from xiaowei_agent.persistence.schema import LOCAL_ADMINS\n"
        "\n"
        "class PostgresLocalAdminStore:\n"
        "    async def relink(self, connection, user_id):\n"
        "        await connection.execute(\n"
        '            sa.update(LOCAL_ADMINS).values({"user_id": user_id})\n'
        "        )\n",
    )
    found = _credential_link_writes(module, "persistence/postgres.py")
    assert found == ["persistence/postgres.py::PostgresLocalAdminStore.relink"]
    assert found[0] not in _CREDENTIAL_LINK_SITES


def test_an_unauthorised_call_site_of_the_write_sink_is_caught(
    tmp_path: Path,
) -> None:
    """反例：任何模块拿到唯一落库口就能凭空写一条授权成功事实。

    上一版有一条同形状的**变异**，但没有这条用例；而 ``_helper_names()`` 当时
    产出的是 ``py::_insert_audit_event``，模块级 helper 的调用点守卫其实恒不命中
    ——变异也照样不红。这条用例直接调用守卫本身，把那个洞钉住。
    """
    module = _sample(
        tmp_path,
        "reporting.py",
        "from xiaowei_agent.persistence.postgres import _insert_audit_event\n"
        "\n"
        "async def _forge(connection, candidate, now):\n"
        "    return await _insert_audit_event(connection, candidate, now=now)\n",
    )
    found = _helper_call_sites(module, "interfaces/reporting.py")
    assert found == ["interfaces/reporting.py::_forge"]
    assert found[0] not in _HELPER_CALL_SITES
    assert found[0] not in _FULL_CANDIDATE_HELPERS


def test_a_declared_call_site_of_the_write_sink_is_allowed(tmp_path: Path) -> None:
    """正常对照：合法调用点必须被发现**且**在名单里，否则守卫只是恒真。"""
    module = _sample(
        tmp_path,
        "postgres.py",
        "class PostgresAdminAuditStore:\n"
        "    async def _write(self, connection, candidate):\n"
        "        return await _insert_audit_event(\n"
        "            connection, candidate, now=self._clock()\n"
        "        )\n",
    )
    found = _helper_call_sites(module, "persistence/postgres.py")
    assert found == ["persistence/postgres.py::PostgresAdminAuditStore._write"]
    assert found[0] in _FULL_CANDIDATE_HELPERS


def test_the_frozen_sites_are_still_allowed() -> None:
    """正常对照：合法写入点必须在它**该在**的那份名单里。

    上一版这条用例同时要求两个目录写入点"在 ``_AUDIT_WRITE_SITES`` 里"和
    "``_AUTHZ_WRITE_SITES`` 与 ``_AUDIT_WRITE_SITES`` 不相交"——两条互斥，整条
    用例不可能通过。根因是 `_insert_audit_event` 收口之后，目录写入点不再直接写
    审计表，而这条正常对照没跟着改。
    """
    for site in (
        "persistence/postgres.py::PostgresUserDirectoryStore._apply_in_transaction",
        "persistence/identity.py::InMemoryUserDirectoryStore._apply_locked",
    ):
        assert site in _AUTHZ_WRITE_SITES
        assert site in _CREDENTIAL_LINK_SITES
        # 它们**不**直接写审计表，因此不在表写入名单里；它们是审计事实的合法
        # 来源，这件事由调用点名单表达。
        assert site not in _AUDIT_WRITE_SITES
        assert site in _HELPER_CALL_SITES
    assert _AUDIT_WRITE_SITES == frozenset(
        {"persistence/postgres.py::_insert_audit_event"}
    ), "审计表只能有一处 INSERT；多一处就等于多一份冲突判定"
    assert not (_AUTHZ_WRITE_SITES & _AUDIT_WRITE_SITES)
    # 那一处落库不许碰授权表，也不许碰凭据链接列。
    assert "persistence/postgres.py::_insert_audit_event" not in _AUTHZ_WRITE_SITES
    assert "persistence/postgres.py::_insert_audit_event" not in _CREDENTIAL_LINK_SITES
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

PII 守卫：

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
```

写入口守卫：**四条变异，每条对应某一版守卫真实放过的一种情况**，逐条确认只有它
该红的那条红。每条做完立刻还原，不要叠加——叠加之后分不清是哪一条触发的。

```bash
cp src/xiaowei_agent/persistence/postgres.py /tmp/keep-pg.py
mutate() {  # $1=追加的代码  $2=说明
  cp /tmp/keep-pg.py src/xiaowei_agent/persistence/postgres.py
  printf '\n\n%s\n' "$1" >> src/xiaowei_agent/persistence/postgres.py
  echo "--- 变异：$2"
  PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/security/test_identity_write_path.py -q 2>&1 | tail -3
}

# 1. 新增一个接受完整候选的私有 helper —— 按签名发现应把它揪出来
mutate 'class _Rogue:
    async def _stash(self, connection, candidate: AdminAuditCandidate) -> None:
        await _insert_audit_event(connection, candidate, now=self._clock())' \
  '新的完整候选 helper（V5 按名字冻结时会漏）'

# 2. 模块外调用唯一落库口 —— 调用点守卫应该报警
mutate 'async def _forge(connection, candidate, now):
    return await _insert_audit_event(connection, candidate, now=now)' \
  '未授权的落库口调用点'

# 3. 字符串键写 user_id，错误 owner，在允许模块内 —— V6 两处都漏
mutate 'class _RogueLink:
    async def relink(self, connection, user_id):
        await connection.execute(
            sa.update(LOCAL_ADMINS).values({"user_id": user_id})
        )' \
  '字符串键 + 错误 owner + 允许模块内（V6 漏）'

# 4. 第二条审计 INSERT —— _AUDIT_WRITE_SITES 现在是单元素集合
mutate 'async def _second_audit_insert(connection, row):
    await connection.execute(sa.insert(ADMIN_AUDIT_EVENTS).values(row))' \
  '第二条 INSERT INTO admin_audit_events'

cp /tmp/keep-pg.py src/xiaowei_agent/persistence/postgres.py && rm /tmp/keep-pg.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -m security -q 2>&1 | tail -3
```

预期：四条变异各自变红，且**红的是对应的那一条用例**（不是整文件语法错）；四次还原
后 security gate 全绿。若某条变异没红，说明那条守卫仍在识别写法而不是属性——先修
守卫，不要改变异。

- [ ] **Step 5: 提交**

```bash
git add tests/security/test_admin_audit_append_only.py \
        tests/security/test_identity_write_path.py \
        tests/security/test_controlled_pii_exposure.py
git commit -m "test(w1a): guard the single write path, append-only audit and PII exposure

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
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


async def _precreate(directory, *, actor, role, user_id=None, operation_id):
    """在库里先放一个账号，用来制造"半迁移"与"角色对不上"两种既有事实。"""
    from xiaowei_agent.contracts.admin_audit import AdminOperationContext
    from xiaowei_agent.contracts.enums import IdentitySource
    from xiaowei_agent.contracts.identity import CreateUserCommand

    await directory.apply(
        command=CreateUserCommand(
            user_id=legacy_user_id(actor=actor) if user_id is None else user_id,
            actor=actor,
            display_name=actor,
            tenant_id=_TENANT,
            environment_id=_ENV,
            role=role,
        ),
        context=AdminOperationContext(
            operation_id=operation_id,
            actor_user_id="usr-local-admin",
            actor="admin",
            auth_source=IdentitySource.LOCAL_ADMIN,
        ),
    )


async def test_an_account_without_its_binding_is_a_conflict_not_a_skip(
    tmp_path, user_directory, persistence_state
):
    """账号在、飞书绑定不在——这不是"迁移过了"，是半迁移。

    这条用例存在的理由是一次真实缺陷：上一版只看 ``load_account()`` 非空就记
    ``skipped`` 并把条目**从批次里剔除**，于是第 4 步的唯一约束根本不会碰到它。
    结果是报告写着成功、审计里什么都没有，而那个人仍然登不进来——当时没有任何
    一条断言会因此转红。
    """
    from xiaowei_agent.contracts.enums import IdentitySource

    await _precreate(
        user_directory,
        actor="alice",
        role=ProductRole.OPERATOR,
        operation_id="pre-half-migrated",
    )
    before = len(persistence_state.admin_audit_events)

    path = _write(
        tmp_path,
        [
            {"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]},
            {"subject_ref": _OPEN_ID_B, "actor": "bob", "labels": ["viewer"]},
        ],
    )
    with pytest.raises(LegacyMigrationConflictError):
        await _run(path, user_directory)

    # 整批没写：bob 不在、alice 仍然没有绑定、审计一条都没多。
    assert await _account(user_directory, "bob") is None
    assert (
        await user_directory.resolve_by_subject(
            provider=IdentitySource.FEISHU,
            tenant_id=_TENANT,
            environment_id=_ENV,
            subject_ref=_OPEN_ID_A,
        )
        is None
    )
    assert len(persistence_state.admin_audit_events) == before


async def test_a_role_that_no_longer_matches_is_a_conflict(tmp_path, user_directory):
    """角色对不上也不能跳过。

    跳过等于在"旧文档说了算"和"库里说了算"之间替人做了选择，而这两者哪个对，
    迁移程序不知道。
    """
    await _precreate(
        user_directory,
        actor="alice",
        role=ProductRole.USER,
        operation_id="pre-wrong-role",
    )
    path = _write(
        tmp_path,
        [{"subject_ref": _OPEN_ID_A, "actor": "alice", "labels": ["operator"]}],
    )
    with pytest.raises(LegacyMigrationConflictError):
        await _run(path, user_directory)
```

`test_rerunning_the_migration_changes_nothing`（本文件已有）就是这两条的正常对照：
完整迁移过的条目仍然要判 `skipped`，否则迁移变成不可重跑。两条反例里**必须**同时断言
"另一条没写进去"与"审计没多"——只断言抛了异常，无法区分"整批回滚"和"写了一半才炸"。
`persistence_state` 是根 `conftest.py` 已有的 fixture，与 `user_directory` 共享同一份
状态（Task 5 Step 6）。
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
3. 判定哪些条目**已经完整迁移过**。只有五项全部精确匹配才算，剔除出批次并记为
   `skipped`；**部分存在或任何一项不一致，立即抛 `LegacyMigrationConflictError`**，
   整批不写。剩余条目为空时直接返回 `created=0`。

   ```python
   for entry in entries:
       user_id = legacy_user_id(actor=entry.actor)
       facts = await directory.load_account(
           user_id=user_id, tenant_id=tenant_id, environment_id=environment_id
       )
       bound = await directory.resolve_by_subject(
           provider=IdentitySource.FEISHU,
           tenant_id=tenant_id,
           environment_id=environment_id,
           subject_ref=entry.subject_ref,
       )
       already = (
           facts is not None
           and bound is not None
           and bound.account.user_id == user_id
           and facts.account.actor == entry.actor
           and facts.account.status is UserStatus.ACTIVE
           and facts.assignment.role is role_for(entry)
       )
       if already:
           skipped += 1
           continue
       if facts is not None or bound is not None:
           # 账号在、绑定不在（或角色/状态对不上）：这条既不能跳过，也不能重写。
           raise LegacyMigrationConflictError(
               f"legacy identity for {user_id} is partially migrated"
           )
       pending.append(entry)
   ```

4. 把剩余条目组装成**一个** `MigrateLegacyIdentitiesCommand`，一次 `apply()` 写完。捕获 `UserDirectoryConflictError` / `AdminAuditUnwritableError` 并转成 `LegacyMigrationConflictError`——**不做事务外预检**：预检和写入之间有时间窗，而且预检通过不等于写入成功。原子性由那一个事务给。
5. `created` 取 `len(events)`，`audit_event_ids` 取事件 id。

**第 3 步的判定不是"报告用途"，它是闭集判据。** V8 的计划这里写的是"`load_account()`
非空即记为 `skipped`"，还附了一句自我安慰："即使它漏判，第 4 步的数据库唯一约束仍会
让整批回滚"。这句话不成立，而且**恰恰是因为第 3 步自己**：被判为 `skipped` 的条目已经
从批次里剔除了，第 4 步根本不会碰它，唯一约束当然不会执行。于是一个"账号和角色在、
飞书绑定不在"的半迁移用户会被静默跳过，报告写着成功，而那个人仍然登不进来——**没有
任何一条断言会因此转红**。

凡是"先剔除、再让后面的约束兜底"的结构，都要问一句：剔除之后，后面的约束还看得见它
吗？看不见，那这一步就是最终判据，必须自己 fail-closed。

`role_for(entry)` 就是第 2 步按 `_ROLE_RANK` 取出的最高角色；`tenant_id` /
`environment_id` 是迁移的目标作用域参数。`resolve_by_subject()` 拿的是**明文**
`subject_ref`，摘要化在 store 内部完成（见 Task 5 的 `external_subject_digest`）——
调用方不自己算摘要，算错了就会永远判成"未绑定"。

- [ ] **Step 6: 运行并跑全量**

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/contract/test_legacy_identity_migration.py \
  tests/contract/test_legacy_identity_document.py -q 2>&1 | tail -6
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q 2>&1 | tail -3
ruff check . && mypy src
```

- [ ] **Step 7: 隔离变异反证**

两条承重保护各变异一次。变异脚本自带"确实改到了代码"的自检——**改不到目标就是脚本错
了，不是保护有效**。

```bash
MIG=src/xiaowei_agent/interfaces/legacy_identity_migration.py
mutate() {  # $1=旧串 $2=新串
  cp "$MIG" /tmp/keep-mig.py
  OLD="$1" NEW="$2" python - <<'MUT'
import os, pathlib
p = pathlib.Path(os.environ["MIG"])
before = p.read_text(encoding="utf-8")
after = before.replace(os.environ["OLD"], os.environ["NEW"], 1)
assert after != before, "变异没有匹配到目标代码——先修脚本，不要把它当成『保护有效』"
p.write_text(after, encoding="utf-8")
MUT
  PYTHONDONTWRITEBYTECODE=1 python -m pytest \
    tests/contract/test_legacy_identity_migration.py -q 2>&1 | tail -6
  cp /tmp/keep-mig.py "$MIG" && rm /tmp/keep-mig.py
}
export MIG
```

变异一：把"半迁移必须冲突"改回"账号在就跳过"。

```bash
mutate '        if facts is not None or bound is not None:' \
       '        if False:'
```

预期：`test_an_account_without_its_binding_is_a_conflict_not_a_skip` 与
`test_a_role_that_no_longer_matches_is_a_conflict` 同时变红，且红在
`pytest.raises(LegacyMigrationConflictError)` 上——**根本没有异常抛出**，正是 V8 的行为。

变异二：把整批命令拆成逐条写。

```bash
mutate '    events = await directory.apply(' \
       '    events = ()
    for entry in pending:
        events += await directory.apply('
```

预期：`test_a_database_conflict_leaves_zero_rows_behind` 变红（`alice` 留在库里）。

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  tests/contract/test_legacy_identity_migration.py -q 2>&1 | tail -3
```

两次变异都必须确认还原后回到全绿再提交。变异二的替换串要按实际写出来的代码微调
（参数缩进），但 `assert after != before` 会先告诉你有没有改到。

- [ ] **Step 8: 提交**

```bash
git add src/xiaowei_agent/interfaces/feishu_identity.py \
        src/xiaowei_agent/interfaces/legacy_identity_migration.py \
        tests/contract/test_legacy_identity_document.py \
        tests/contract/test_legacy_identity_migration.py
git commit -m "feat(w1a): migrate legacy identity labels in one atomic command

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
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


_UNBUILT_AFTER_W1A: Final[tuple[str, ...]] = (
    "ActivationStore",
    "IdentityActivationService",
    "web_oauth_login_contexts",
    "激活审批",
    "Admin 管理中心",
)
"""W1a 交付后**仍然**不存在的东西：激活审批是 W1b，Admin 页面是 W3。"""

_EXISTENCE_CLAIMS: Final[tuple[str, ...]] = (
    "已实现",
    "已上线",
    "已交付",
    "已完成",
    "现已",
)
_PHASE_LABEL: Final[re.Pattern[str]] = re.compile(r"\bW\d[a-c]?\b")
"""交付阶段标签（`W1b`、`W2`、`W4a` …）。"""

_NON_EXISTENCE: Final[tuple[str, ...]] = (
    "尚未",
    "未实现",
    "还没有",
    "还不",
    "不存在",
    "将",
    "计划中",
)
"""明确否定其当前存在的说法。

这两组合起来就是"合格的提及"的完整定义：**要么**在同一行说清它属于哪个阶段，
**要么**在同一行否认它现在存在。不按这条原则、而是往列表里补字面量补到变绿，
补出来的就是一个不再表达任何不变量的守卫。
"""


def _false_existence_claims(text: str) -> list[str]:
    """把还不存在的东西说成当前事实的句子。

    **按行判定，不做整篇豁免。** 上一版写的是
    ``marker not in text or "W1b" in text or "未实现" in text``——只要文档里任意
    一处出现过 ``W1b`` 或"未实现"，整篇里所有错误声称都被放行。实测四份真源
    文档中有三份已经满足这个条件，也就是说那条守卫当时就已经抓不到东西了。
    这与凭据作用域守卫（``_credential_scope_conflations``）用的是同一套办法：
    按句子判定，豁免也必须写在同一句里。

    存在性声称不设豁免：一行说"激活审批已实现"，不论同一行还写了什么，都是错的。
    """
    claims: list[str] = []
    for line in text.splitlines():
        if not any(noun in line for noun in _UNBUILT_AFTER_W1A):
            continue
        if any(claim in line for claim in _EXISTENCE_CLAIMS):
            claims.append(line.strip())
    return claims


def _unqualified_mentions(text: str) -> list[str]:
    """提到未来组件却没在**同一行**标明它属于哪个未来阶段的句子。"""
    mentions: list[str] = []
    for line in text.splitlines():
        if not any(noun in line for noun in _UNBUILT_AFTER_W1A):
            continue
        qualified = _PHASE_LABEL.search(line) is not None or any(
            marker in line for marker in _NON_EXISTENCE
        )
        if not qualified:
            mentions.append(line.strip())
    return mentions


@pytest.mark.parametrize("name", _CURRENT_TRUTH_DOCS)
def test_truth_docs_do_not_claim_activation_or_admin_ui_exists(name: str) -> None:
    """W1a 只交付写底座。

    最容易出现的漂移正是"写内核做完了顺手把整条身份线说成做完了"。
    """
    text = _truth_doc_text(name)
    assert _false_existence_claims(text) == [], (
        f"{name} 把 W1b/W3 才有的东西写成了当前事实"
    )
    assert _unqualified_mentions(text) == [], (
        f"{name} 提到未来组件却没在同一行标明阶段"
    )


@pytest.mark.parametrize("name", _CURRENT_TRUTH_DOCS)
def test_the_activation_claim_guard_is_discriminating(name: str) -> None:
    """逐份注入反例。

    这条用例存在的理由是：上一版守卫用整篇文件级豁免，注入后四份里有三份仍然
    绿。逐份参数化是必须的——只要有一份文档的豁免条件被满足，整篇豁免就让那一份
    永久失去保护，而汇总成一条断言时这件事看不出来。
    """
    text = _truth_doc_text(name)
    assert _false_existence_claims(text + "\n激活审批已实现。\n")
    assert _false_existence_claims(text + "\nAdmin 管理中心已上线。\n")
    assert _unqualified_mentions(text + "\n身份绑定由 ActivationStore 负责。\n")


def test_naming_a_future_component_with_its_phase_is_allowed() -> None:
    """正常对照：写清楚阶段的句子必须放行，否则真源文档没法描述路线。"""
    assert _unqualified_mentions("W1b 将引入 `ActivationStore` 承载激活审批。") == []
    assert _false_existence_claims("激活审批尚未实现，属于 W1b。") == []


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
- `AGENT_HANDOFF.md`：第 0 节改为"W1a 写内核已离线实现"，记录实现基线 SHA、四门实测数字、变异反证结论；明确写出**未覆盖**：没有激活审批、没有 Admin 页面、没有登录改造、没有真实飞书调用、没有部署与用户验收；以及**残余风险**：既有 `AuthenticatedPrincipal.subject_ref` 仍未标 PII 遮蔽（本阶段未触碰渠道投影，留待 W2）；三条 AST 守卫的静态上限（别名/`Any`/未注解参数绕得过签名发现、运行时算出来的列名只能报警不能判定、`sa.text("insert into ...")` 不在视野内）按 Exit Criteria 那段**原样抄进 handoff**，不得写成『已封死』；engine 未开 `hide_parameters=True` 是全仓库既有状态，本阶段不改，仓库对该风险的既有答案是 `_persistence_boundary` 切断异常链。

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
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
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
- `operation_id` **不是**全局唯一，两条 partial unique index 的存在与 WHERE 由离线结构用例钉住，终态那条由套件用例在两个实现上行为验证；结构用例的 helper 自带『partial 不等于全局』的自证反例；
- `AdminAuditStore` 按 `DEVELOPMENT_PLAN.md:178` / `ADR-013:131` 交付 append-only 写契约，表面恰好是 `append_started` / `append_terminal` / `append_denied` / `load`；**内存与 PostgreSQL 两个实现的公开表面都精确等于它**，由 `test_protocol_conformance.py` 的 `==` 断言钉住；三个方法都写不出目录成功事实（目录动作在 `AdminAuditStart` 契约层即被拒，两个实现共用这条规则）；目录成功事实只由 `apply()` 在改动所在事务里派生并 INSERT；
- 接受完整 `AdminAuditCandidate` 的助手由守卫**按签名发现**（不是按名字冻结），发现结果与声明名单 `==`，调用点全部 owner-qualified 冻结，并带『新增一个收候选的 helper』与『外部模块调用它伪造成功』两条反例；
- `local_admins.user_id` 守卫提取 `values()` **实际写入的列**（关键字、字符串键、列对象键、下标列键四种等价写法逐一有反例），静态判不出写哪一列的形式（变量键、`**` 展开）与 `sa.delete(LOCAL_ADMINS)` 同样报警，且**不跳过任何模块**；带『允许模块内的错误 owner』反例与三条『改密不误伤』正常对照；反例调用守卫本身，不调用它的副本；
- 错误语义只有矩阵一那一份：全文任何一处 `AdminAuditConflictError` / `AdminAuditUnwritableError` / `UserDirectoryConflictError` / `PersistenceIntegrityError` 都能对上矩阵一的某一格，同一个场景在所有章节只有一个答案；
- `persistence/` 里**没有任何一处**捕获 `IntegrityError` 或读取约束名来判定冲突；已知冲突全部由 `ON CONFLICT DO NOTHING ... RETURNING` 探测，未知约束落到 `PersistenceIntegrityError` 并有真库用例钉住；`INSERT INTO admin_audit_events` 在 PostgreSQL 侧只有 `_insert_audit_event` 一处；四张被探测表的唯一约束集合由离线 `==` 用例钉死；
- 新 PostgreSQL 代码全部复用既有机制：公开方法 `@_persistence_boundary`、写路径 `_write_transaction`、行↔契约在 `rows.py` 且枚举显式构造；审计事件的契约→列→契约往返有离线恒等用例（succeeded 带 effect 与 denied 不带 effect 各一）；闭集领域错误穿过边界不被收敛，有用例钉住；
- 五个 `LOCAL_ADMIN_*` 常量在 `contracts/identity.py` 只有一份，`interfaces/local_admin_auth.py` 消费它并不再保留自己的字面量；
- `role_assigned` / `user_status_changed` / `user_created` / `local_admin_bootstrapped` / `legacy_identity_migrated` 事件携带闭集 effect，且由数据库 CHECK 强制；非成功 outcome 不得携带 effect；
- `admin_audit_events` 的 append-only 有源码级机械守卫，且守卫自身有反例证明它抓得到违规；
- `AdminAuditEvent` 列集合 = 规格 §14.2 的 13 个 + `effect_role` + `effect_status`，无任何自由文本或 JSON 通道；
- 角色→`ChannelPermission`、角色+来源→`AdminCapability` 与规格 §6.2/§6.3 逐格相等，飞书 `ADMIN` 恰好少两项；`ChannelPermission` 仍精确三成员；
- `subject_ref` 在契约与数据库两侧都不以明文出现，PII 守卫有"扫描数为 0 即失败"的自证；
- 旧标签迁移是一个命令一个事务，冲突用例制造在**数据库既有事实**上，冲突后零部分落库，重跑 `created=0`，`approver` 不生效授权，超长 actor 有用例；
- `legacy_user_id()` 对任意长度 actor 都产出 ≤64 字符的稳定 ID；
- `feishu_identity.py` 的抽取没有改变既有行为：`tests/contract/test_feishu_identity.py` 全绿；
- `seed_if_absent` 的签名不变，9 处既有调用方全绿；返回值含义仍是『这次调用是否做了 bootstrap 工作』，升级路径上的 backfill 返回 `True`（它确实补齐了目录），链接完整时返回 `False`；
- bootstrap 四态各有用例：全新安装、已有凭据未链接（backfill 且**不覆盖**已改密码）、链接完整幂等、链接破损 fail-closed；其中升级路径由一条**真实降级到 `rev_0013`、插入旧形状凭据、再升级到 head** 的 PostgreSQL 用例证明；
- 改密不清空 `local_admins.user_id`，有用例钉住；
- 两条承重守卫各自绑在它们声称的事实上并带自证反例：写路径守卫按精确相对路径 + 所在类/方法判定（四条反例：同文件模块级写函数、同类新增公开写方法、他目录同名文件、直接写 `user_id`；两条正常对照：私有助手放行、改密不误伤）；文档守卫按行判定，四份真源文档**逐份**注入反例都转红；
- `rev_0014` 与 `schema.py` 离线 DDL 一致（含两条 partial unique index），发布哈希已登记，downgrade 在有数据时要求显式破坏性授权；
- diff 只含 Task 0 的 allowlist，无 HTTP 路由、无激活载体、无 requester/approver 载体；
- ADR-008 四门全绿；
- W1a PR 经精确 SHA 复审并由**负责人合入** `main`；
- handoff 明确记录：没有激活审批、没有 Admin 页面、没有登录改造、没有真实飞书/Gemini/StarRocks 调用、没有部署、canary 或用户验收。

**这些守卫做不到的事，必须原样写进 handoff 的残余风险，不得说成『已封死』：**

- 完整候选 helper 按签名发现，只认参数注解里**字面出现** `AdminAuditCandidate`。用别名（`Candidate = AdminAuditCandidate`）、字符串注解里的别名、`Any`、`object` 或未注解参数，都绕得过去。这是 AST 静态判定的固有上限，不是实现没写好。
- `local_admins.user_id` 守卫同理：它认得出"这条 `values()` 写了哪些列"，认不出"这条 `values()` 写的列是运行时算出来的"——所以那种形式一律报警。报警不是证明，它只保证这种写法过不了审，不保证没有别的通路。
- 表写入守卫按 `sa.insert(TABLE)` 这种**语句形状**匹配。`sa.text("insert into ...")` 或把表对象存进变量再传递，都不在它的视野里。仓库当前没有这种写法，但守卫并不禁止它。
- 以上三条都不是"需要补的漏洞"，而是"这类守卫能做到哪一步"的边界。真正的兜底是 code review 与 `_AUDIT_WRITE_SITES` 这种**单元素**名单：名单越小，绕过它需要的改动就越显眼。

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

---

## V3 修订记录

V2（`abe8f46392f8d57e602c6430b9360f206be69a58`）经复审再次判定「需修改」。四项 P1 我逐条按真实源码与实跑核对，**全部成立**：

| 意见 | 核实方式与结论 |
| --- | --- |
| P1-1 `_unique_column_sets` 把 partial index 误算成全局唯一 | 按同样 SQLAlchemy 结构实跑，输出 `{frozenset({'operation_id'})}` / `False`，与复审报告一致。成立 |
| P1-2 已 seed 的库升级后目录永远补不上 | `local_admin.py:94` 无条件 `ON CONFLICT DO NOTHING`，`local_stack.py:1031` 每次装配都调用。成立 |
| P1-3 公开 `append()` 仍能伪造成功授权审计 | V2 自己的两阶段用例就在 append 一条 `ROLE_ASSIGNED + SUCCEEDED` 而不改任何角色。成立 |
| P1-4 两条守卫假绿 | 按 basename 放行；文档守卫整篇豁免，四份真源文档实测三份满足豁免条件。成立 |

三组共同根因与修订：

| 根因 | V2 的问题 | V3 的修订 |
| --- | --- | --- |
| **E. 派生保证只做了一半：`apply()` 收口了，`append()` 没有** | `AdminAuditStore.append(candidate)` 接受完整候选，任何调用方都能写一条『授权成功』而不曾改动任何授权 | 删掉通用 `append(candidate)`；新增 CHECK `ck_admin_audit_events_directory_actions_are_single_phase`；审计表并入 `test_identity_write_path.py` 的同一套写路径判定。**⚠ V3 在这一组上改过头**：连同写契约一起删成了只读协议，违反 `DEVELOPMENT_PLAN.md:178` 与 `ADR-013:131`，已由 V4 的 H 组纠正 |
| **F. bootstrap 的幂等键绑在凭据行上，不是目录链接上** | 已有 `local_admins` 行即返回空元组；任何跑过 RI5 的库升级后 `user_id` 永为 `NULL`、账号与 ADMIN 角色永不出现，而全新安装用例照样全绿 | bootstrap 改为四态分流（无凭据／有凭据但未链接／链接完整／链接破损），backfill **保留原 `password_hash`**，破损 fail-closed。`fake.py` 与 `postgres.py` 共用同一套语义。`_snapshot`/`_restore` 补上 `state.local_admin`。新增四条套件用例 + 一条**真实降级到 `rev_0013`、插入旧形状凭据、再升级**的 PostgreSQL 用例。顺带发现并处理同根问题：`LocalAdminRecord` 加 `user_id` 后，改密路径重建记录时漏带该字段会把链接清空，新增用例钉住 |
| **G. 守卫谓词绑错对象——三处，两个方向** | 写路径守卫按 basename 放行整个文件；文档守卫按整篇豁免；`_unique_column_sets` 只看 `index.unique`。前两处造成**假绿**，第三处造成**假红**（Task 4 按计划执行必然失败） | 写路径守卫改为精确相对路径 + 所在类/方法（沿用 `test_audit_ledger_guards.py` 的定位手法），并把 `local_admins.user_id` 做**列级**治理以免误伤改密；文档守卫改为按行判定（沿用 `_credential_scope_conflations` 的办法），豁免必须写在同一行；`_unique_column_sets` 按 `postgresql_where` 区分全局与 partial。三处各配自证反例 |

**同类问题扫描**（复审未点到、我顺着同一根因找到的）：新增 `rev_0014` 会让**三处**既有锚点转红，不是两处。除复审指出的 `test_latest_declared_revision_is_the_alembic_head` 外，`tests/unit/test_readiness.py:51` 把 `"0013_clarification_parent"` 写死并断言 `revision_matches_head is True`，而探针比对的是**真实** Alembic head。Task 4 新增 Step 5 一并处理，其中前者保留写死 revision（它要钉的就是"最新声明即 head"，两边都取 head 会变成同义反复），后者改为从 Alembic 取 head（它要钉的事实与具体 revision 无关，写死只会让它每加一次迁移就静默转红）。

另有一处笔误已改：开工门原写"见 Task 10"，实际只有 Task 9。

复审认可的两项有意偏离（`actor` 全局唯一比同作用域更严格；`admin` 作为固定用户名常量不重复落库，W2 须继续复述该边界）保持不变。

---

## V4 修订记录

V3（`af153a26d54f597eee162ae5acad525cd98efe65`）经复审判定「需修改」。四项 P1 逐条核实，**全部成立**：

| 意见 | 核实方式与结论 |
| --- | --- |
| P1-1 删除了 W1a 必须交付的写契约 | `DEVELOPMENT_PLAN.md:178`「W1a 交付 `AdminAuditStore` 的持久化与 append-only 写契约」、`ADR-013:131`「W1a 先提供…W1b 才作为消费者复用它」。原文核对，成立 |
| P1-2 改造未贯穿全文 | 第 32、2331、3033–3037、4232 行仍要求 `{"append", "load"}`，其中两处是可执行断言。成立 |
| P1-3 revision 自相矛盾 | 骨架声明 `0014_identity_admin_audit`（25 字符），chain 测试却断言 `0014_identity_directory_and_admin_audit`（39 字符），后者还会撞 `_ALEMBIC_VERSION_NUM_MAX_LENGTH = 32`。成立 |
| P1-4 Task 6 不能照计划执行 | `ChangePasswordCommand` 真实字段是 `new_session_digest` / `public_origin_digest` / `session_ttl_seconds`；`persistence/fake.py` 不在冻结 allowlist。两处均成立 |

三组共同根因与修订：

| 根因 | V3 的问题 | V4 的修订 |
| --- | --- | --- |
| **H. 删掉了批准真源要求的交付物，且没有报告冲突** | 复审要求"不能凭空写成功授权事实"，我把整个写契约删成只读，顺手把 W1b 的复用面一起删了。更严重的是：`AGENTS.md` 要求发现文档与设计冲突时**报告冲突与证据**，而 V3 的计划里 Global Constraint 第 8 条（`AdminAuditStore` 只有 `append` 与 `load`）原样留着——文件当场就在反驳我，我没有去看 | 恢复 append-only 写契约，做成三个**窄**方法：`append_started`（`AdminAuditStart` 无 `outcome`/`effect` 字段）、`append_terminal`（稳定字段从已存 `STARTED` 读回）、`append_denied`（`AdminAuditDenial` 无 `effect` 字段）。三者都写不出目录成功事实，各有一条用例证明；`apply()` 仍是目录成功事实的唯一来源。新增 `DIRECTORY_ACTIONS` 作为单一来源，并加一条用例逐值比对它与 CHECK 的 SQL 字面量 |
| **I. 同一轮改造没有贯穿计划全文** | 五处仍写着旧契约形状（两处是可执行断言）；allowlist 没跟着新增 `persistence/fake.py`；allowlist 里的迁移文件名也没跟着改 | 五处全部改为 `append_started` / `append_terminal` / `append_denied` / `load`。Task 0 新增一步**机械**核对：把计划里每条 `Files:` 声明与 allowlist 做集合差，开工前必须输出『（无）』。这一步跑出来的第一个结果就是 `persistence/fake.py` |
| **J. 计划示例凭记忆写，没有对着真实源码** | `ChangePasswordCommand` 传了两个不存在的字段；chain 测试的 revision 是按文件名编的，不是骨架里声明的那个 | 两处按源码改正。另做一次机械核对：把计划所有代码块里的 `from xiaowei_agent...import` 全部解析出来，逐个 `hasattr` 验证——**又查出第三例**（见下） |

**同类问题扫描（复审未点到）：** 机械核对导入时发现计划写了
`from xiaowei_agent.contracts.base import SecretHash`，而 `contracts/base.py` 里
没有这个名字——它定义在 `persistence/local_admin.py:42`（`SecretHash = StrictStr`）。
按原文跑是 `ImportError`，与 P1-4 的 `session_digest` 同一根因。修法是把这一行别名
搬到 `contracts/base.py`（契约模块不能反向依赖 persistence，两处各写一份又会让
「同一套约定」变成两套），`local_admin.py` 改为导入。全仓库只有它的两个字段用到，
搬家不改变行为。`contracts/base.py` 随之进入 allowlist 并写明理由。

**这一轮承认的边界：** `append_started` / `append_terminal` 的**成功路径**在 W1a
没有行为覆盖——`ADR-013:129` 把两阶段写留给非事务性配置面动作，而 W1a 的 action
闭集全部是目录动作，CHECK 禁止它们写 `STARTED`。这不是遗漏，是有意的：计划用一条
`test_every_w1a_action_is_a_directory_action`
把它钉成**显式事实**，该断言会在 W4a 加入配置动作时转红，那时才是放开的时机。
两个方法的拒绝路径在本阶段有用例。

---

## V5 修订记录

V4（`29d6be67dde23f2006b50b5600029d444d1b721d`）经复审判定「需修改」。三项 P1 逐条核实，**全部成立**：

| 意见 | 核实方式与结论 |
| --- | --- |
| P1-1 PostgreSQL 实现仍是旧的只读设计 | Task 6 原文仍写着「`PostgresAdminAuditStore` 只有 `load()`…这个类**没有**任何写方法」，而共享套件会在 PostgreSQL fixture 上调这三个窄方法。成立 |
| P1-2 内存实现过不了自己的测试，且重开通用旁路 | `append_started` 直接 `append_locked(start.as_candidate())`，没有任何 `DIRECTORY_ACTIONS` 检查；`append_locked` 是公开命名、接受完整候选。更严重的是它的注释声称「Task 7 的 AST 守卫要求它只在目录 store 里被调用」——`grep` 全文，**那个守卫不存在**。成立 |
| P1-3 bootstrap 常量没有合法落点 | `tests/security/test_module_layering.py:47` 规定 `persistence` 只能导 `contracts` / `planning` / `persistence`；计划允许的「从 `interfaces/local_admin_auth.py` 导入」会直接撞这道门。成立 |

**三项共一个根因：改造只推进到最外层。** V4 把写契约改到了 Protocol 与契约类型，就停下了；内存实现、PostgreSQL 实现、AST 守卫、提交清单全都停留在上一版设计。这与 V4 修订记录里的 **I 组（同一轮改造没有贯穿计划全文）是同一个根因，连续第二轮**。

V4 为此加了两条机械核对（allowlist 覆盖、import 符号存在），但两条都抓不住「Protocol 与实现不一致」——这正是本轮的失败形态。所以本轮的结构性修订不是再写一遍"记得贯穿"，而是补上能机械发现它的那条断言：

| 修订 | 内容 |
| --- | --- |
| **契约层统一规则** | 目录动作在 `AdminAuditStart` 的 `model_validator` 里被拒，内存与 PostgreSQL 共用同一条。放在任一实现里，共享套件就会在两个绑定上给出不同答案 |
| **PostgreSQL 三个窄方法** | 给出实现、错误分类表、事务与并发语义（含「为什么**不**需要 `SELECT FOR UPDATE`」：审计表 append-only，`STARTED` 行不会被改删，真正的并发点由终态 partial unique index 裁决） |
| **`append_locked` → `_append_locked`** | 私有命名，并真的写出那条调用点守卫，冻结在三个窄方法 + 目录派生路径共四处 |
| **AST 守卫改为冻结的精确方法集合** | 不再用 `startswith` 前缀放行（V4 的写法会让新加的任意私有写方法自动获得授权）；审计表与授权表各有一份名单，审计表多出审计 store 的那一处落库 |
| **公开表面精确等于 Protocol** | 对四个具体实现参数化断言。`isinstance` 式的 Protocol 检查只保证实现**不少于**协议，方向正好相反——对"多出来一个公开写方法"完全无感 |
| **`LOCAL_ADMIN_*` 下沉到 `contracts/identity.py`** | 明确唯一真源而不是"二选一"；`interfaces/local_admin_auth.py` 改为消费它并删掉自己的字面量，并补一条用例钉住接口层不得保留 `"dev-local"` / `"dev"` |
| **Task 0 机械核对扩到三条** | 新增第 2 条：每个任务的 `git add` 必须覆盖它自己的 `Files:`。V4 把 `fake.py` 加进了 allowlist 与 Files，却漏了 Task 6 的 `git add` |

**顺带修掉的重复校验：** Task 5 的 `test_no_write_method_can_state_a_successful_directory_change` 与 Task 3 新增的契约用例测同一件事；共享套件里的 `test_started_is_refused_for_a_directory_action` 与 `test_every_w1a_action_...` 是纯契约事实、不需要 store fixture。三条合并到 `tests/contract/test_admin_audit_contracts.py`。

**复审指出的两处报告不准，一并更正：** 上一轮我报告 allowlist 覆盖核对的声明文件数是 35，实际是 36（我是在补完 Task 2 的 Files 之前跑的那次计数）；计划文件末尾多了一个空行，`git diff --check` 会报 `new blank line at EOF`，已删除。

---

## V6 修订记录

V5（`2203252a4acf24dcce8262a61352fd5a7dedd99b`）经复审判定「需修改」。四项 P1 逐条核实，**全部成立**：

| 意见 | 核实方式与结论 |
| --- | --- |
| P1-1 数据库行还原不成契约 | 按 `Contract` 的真实 config（`strict=True` + `extra="forbid"`）实跑，同时报 `is_instance_of`（字符串喂不进 `StrEnum`）与两条 `extra_forbidden`（`effect_role` / `effect_status`）。成立 |
| P1-2 `_insert` / `_insert_on` 是新的伪造旁路 | 两者都收完整 `AdminAuditCandidate`，而调用点守卫只冻结了 `_append_locked` 这个**名字**。成立 |
| P1-3 绕过既有持久化边界 | `postgres.py:244` / `:293` 的 `_persistence_boundary` 与 `_write_transaction` 是仓库既有机制，每个既有 Store 的公开方法都用；我的新代码一处没用。成立 |
| P1-4 `user_id` 守卫假绿 | 只认 `values(user_id=...)` 关键字写法，且整体跳过 `identity.py` / `postgres.py` 两个模块。成立 |

两组共同根因：

### M. 新 PostgreSQL 代码没有对着同一个文件里的既有实现写（P1-1 + P1-3）

我凭印象写了 `engine.begin()` 和 `AdminAuditEvent.model_validate(dict(row))`，而同一个
`postgres.py` 里每个既有 Store 都是 `@_persistence_boundary` + `_write_transaction`，
行与契约的互转按既有约定住在 `persistence/rows.py`（十余对 `row_to_X` / `X_to_row`，
枚举一律显式构造）。

**最刺眼的证据**：`rows.py` 的模块 docstring 第 16 行写着「`Contract` 全局 `strict=True`：
python 校验模式下 `str` 不能转 `StrEnum`」——我本该复用的那个文件，开头就写着我犯的那个错。

修订：三个窄写方法各自只收自己的窄类型；落库集中到一处 `_write`；公开方法全部套
`@_persistence_boundary`，写路径走 `_write_transaction`；新增
`admin_audit_event_to_row` / `row_to_admin_audit_event` 放进 `rows.py`；未知约束交给边界
收敛而不是原样上抛。**同类问题一起处理**：目录 store 的 `apply()` 有同样的毛病
（裸 `engine.begin()` + 裸 `except IntegrityError`），复审没点到，一并改；异常类型统一走
`sa.exc.` 命名空间，与 `errors.py:87` 一致。

### N. 守卫枚举的是"我记得的那个写法/那个名字"，不是它要保护的属性（P1-2 + P1-4）

- 完整候选守卫冻结的是 `_append_locked` 这个**名字**。新增的 `_insert` / `_insert_on`
  同样收候选，却自动逃出名单。**私有命名不是安全边界。**
- `user_id` 守卫匹配的是 `values(user_id=...)` 这个**写法**。等价的
  `values({LOCAL_ADMINS.c.user_id: v})` 完全不被发现。
- 同一条守卫还整体跳过两个模块——而"按 owner 精确判定、不跳过模块"是我上一轮刚为
  另外两条守卫修过的东西，这一条漏掉了。

修订：完整候选守卫改为**按签名发现**（AST 找 `AdminAuditCandidate` 参数），发现结果与
声明名单 `==`，新增一个就必须来声明；`user_id` 守卫两种写法都认，允许项 owner-qualified
到 `路径::类.方法`，不跳过任何模块。

### 先红后绿

| 证据 | 先 | 后 |
| --- | --- | --- |
| 契约 ↔ 列往返 | `model_validate(dict(row))` → `is_instance_of` + `extra_forbidden` | succeeded 带 effect、denied 不带 effect 双向恒等 |
| 新增收候选的 helper | 按名字冻结时不受任何约束 | 按签名发现 → 守卫转红 |
| 外部模块调用 `_write(forged)` | V5 无此守卫 | 转红 |
| `postgres.py` 内错误 owner 用 mapping 写 `user_id` | V5 跳过该模块 | 转红 |
| 三条变异还原后 | — | 13 passed |

### 复审指出的过期说明

`AdminAuditCandidate` 的 docstring 还写着「`AdminAuditStore` 上没有任何写方法」——那是
V3 只读协议时期的话。已改为说明"公开方法一个都收不下完整候选，能收下的两个私有助手由
守卫按签名发现"。

### 本轮未做、且不打算在 W1a 做的一件事

engine 没有开 `hide_parameters=True`。这是全仓库既有状态，改它会影响每一个 Store 的
异常行为，超出 W1a 的 allowlist 与变更面。仓库对这个风险的既有答案就是
`_persistence_boundary` 切断异常链——本轮的修订正是回到这条答案上。已列入残余风险。

---

## V7 修订记录

针对 `1fb9e5ab8f0e995d58ceab2d095b6c8c03d07d4c` 的复审。四条 P1 逐条核实，**全部成立**，
归成两组根因。本轮的修订方式与前几轮不同：先建单一语义真源（新增的"三张规范矩阵"），
再沿全文统一修复，不按评论位置逐点打补丁。

### O. 错误语义没有唯一真源（P1-A1 ~ A5）

**根因** —— 同一条规则在四个章节各写了一遍，于是它们各自漂移。最刺眼的证据是
两条用例：同一个"复用 `operation_id` 调 `apply()`"场景，一条期望
`AdminAuditUnwritableError`，另一条期望 `AdminAuditConflictError`。两条不可能同时
通过，而规则散在多处时，没有任何人会看见它们已经分叉。

第二层根因是**凭记忆写驱动行为**。我写下
`getattr(getattr(exc.orig, "diag", None), "constraint_name", None)`，那是 psycopg
的接口；本仓库 `pyproject.toml:28` 钉的是 asyncpg。真库实测：`exc.orig` 是
`sqlalchemy.dialects.postgresql.asyncpg.IntegrityError`，**没有 `.diag`**，只有
`.sqlstate`。那行代码在真库上恒为 `""`，于是每一次审计冲突都会掉进 else 分支——
"阶段重复 → `AdminAuditConflictError`" 那条断言**永远不会绿**。同理，`apply()` 外层
的 `except sa.exc.IntegrityError` 不可达：`_write_transaction` 已经先把它换成了
`PersistenceIntegrityError`。

**修复位置** ——

| 位置 | 改动 |
| --- | --- |
| 新增「三张规范矩阵」 | 错误语义、写入口、事务与冲突探测各一份真源，附实测事实表；后续章节只引用 |
| 共享套件 | 删掉与回滚用例重复的那个 `apply()` 场景，改成复审点名但原本缺失的"直连 `append_denied` 重复 → `AdminAuditConflictError`" |
| `PostgresUserDirectoryStore.apply` | 删掉不可达的 `except`；已知冲突改为事务体内 `ON CONFLICT DO NOTHING ... RETURNING` 探测 |
| `PostgresAdminAuditStore._write` | 同上；不再捕获 `IntegrityError`、不读约束名 |
| 删除 | `_AUDIT_CONSTRAINTS`、`_classify_directory_integrity_error`、`_raise_audit_conflict_or_defer`、那张"约束名 → 错误"的表，以及其中那条自称"理论上不可达"的 `ck_...` 映射 |
| 新增静态防线 | `_CONFLICT_TARGETS`：四张表上 `ON CONFLICT` 能吞掉的列组合逐格钉死，`==` 断言 |
| 新增真库用例 | CHECK 违反（未知/未探测约束）→ `PersistenceIntegrityError(constraint, rolled_back)`，且 `__cause__ is None` |

**归属规则一句话**：谁的公开方法失败，就用谁的错误族。`apply()` 内部的审计冲突叫
`AdminAuditUnwritableError`，直连 store 叫 `AdminAuditConflictError`；同一条约束、
两个入口、两个错误族，这是有意的，不是分叉。

**为什么用 `ON CONFLICT` 而不是按复审建议"设计真实的约束名提取逻辑"** —— 复审对
症状的判断成立，但提议的修法不是这个仓库的做法。`persistence/` 里**没有任何一处**
捕获 `IntegrityError` 来分类冲突：`create_task`（`postgres.py:1927` 的 docstring 写明
了理由）、`PlanStore.save`、`append_evidence` 全部用 `ON CONFLICT DO NOTHING ...
RETURNING`。而约束名只挂在 `exc.orig.__cause__` 上——那条链正是 `_persistence_boundary`
存在的目的（`str(exc)` 带着被拒绝的整行）。从边界要切断的链里取值来做业务判断，是
在拆自己的防线。实测确认 `ON CONFLICT DO NOTHING` 恰好吞掉主键/唯一/偏唯一三种冲突，
而 CHECK、外键、NOT NULL 照抛——分界正好落在"我认识的"与"我不认识的"之间。

**同类问题一起处理（复审未点到）**

- `INSERT INTO admin_audit_events` 原本在两处：目录路径一条、审计 store 一条，只共享列映射。只共享一半的东西会在另一半上分叉，而分叉点正好是"什么算阶段重复"。合并成模块级 `_insert_audit_event`，PostgreSQL 侧**唯一**一处落库。
- `_AUDIT_WRITE_SITES` 原本写成 `_AUTHZ_WRITE_SITES | {...}`，顺带允许两个目录助手直接写审计表——而它们现在都不需要。收紧成单元素集合，并加一条 `_AUTHZ_WRITE_SITES & _AUDIT_WRITE_SITES == ∅` 的断言。
- 内存实现 docstring 引用的守卫用例名 `test_the_full_candidate_helper_has_only_derived_call_sites` 不存在（实际叫 `..._helpers_have_only_declared_call_sites`）。与 V4 那次"注释声称的守卫不存在"同一类，改正。
- 我自己新写的 `_CONFLICT_PROBED_TABLES` 第一版按**约束名**钉。实跑发现：本仓库 `METADATA` 没有 naming convention，主键由 `primary_key=True` 声明、在 metadata 里 `name is None`，那条用例会 `AssertionError` 恒红。改成按**列组合**钉——`ON CONFLICT` 推断的本来就是列不是名字。

### P. AST 守卫仍在识别写法（P1-B）

**根因** —— 与 V6 的 N 组是同一句话的下一层：V6 已经把"按名字"改成了"按签名"，
却把 `local_admins.user_id` 那条留在了"按写法"。守卫枚举
`values(user_id=...)` 与 `values({LOCAL_ADMINS.c.user_id: ...})` 两种写法，而
`values({"user_id": ...})` 和 `values({LOCAL_ADMINS.c["user_id"]: ...})` 生成**完全
相同**的 UPDATE，整条漏过。枚举写法的守卫永远差一种写法。

**修复位置** —— `_credential_link_writes` 重构为：提取 `values()` **实际写入的列**
（`_written_columns` + `_column_name_of`），判断里面有没有 `user_id`；允许项仍
owner-qualified，不跳过任何模块。

**静态判不出的一律报警，不放行。** 变量键 `values({column: v})` 与 `values(**patch)`
判不出写哪一列——判不出就报出来。一条把"我判不出来"当成"它合规"的守卫，等于给绕过
它的人附了一份说明书。

**同类问题一起处理（复审未点到）**

- 反例走的是一个叫 `_owners_credential` 的函数，它把判定逻辑**抄了一遍**。反例因此证明的是那份副本正确，守卫本体改没改它都不知道——这正是本轮 O 组要根治的"同一规则两份实现"。`_credential_link_writes` 多收一个 `relative` 参数后，反例直接调用它本身。
- `sa.delete(LOCAL_ADMINS)` 一并纳入：删掉整行同样解除目录链接，与写 `user_id` 是同一属性上的同一件事。
- 完整候选 helper 守卫的静态上限（别名、`Any`、未注解参数）如实写进 Exit Criteria 与 handoff 残余风险，**不写成"完全封死"**。

### 先红后绿

| 证据 | 先 | 后 |
| --- | --- | --- |
| 真库探针 E1：asyncpg 异常链 | `exc.orig` 无 `.diag`、无 `constraint_name` → 计划里的分流恒为 `""` | 分流整个删除 |
| 真库探针 E4：`_write_transaction` 之外 `except sa.exc.IntegrityError` | 不命中；外层拿到的是 `PersistenceIntegrityError(constraint, rolled_back)`、`__cause__ is None` | 该 `except` 删除 |
| 真库探针 E7：不带目标的 `ON CONFLICT DO NOTHING` | 主键/唯一/偏唯一三种冲突 `RETURNING` 为空且不抛；CHECK/外键/NOT NULL 照抛 | 成为矩阵三的机制 |
| 真库探针 E5：事务体内抛闭集领域错误 | 穿过两层不变形，同事务授权写入回滚（实测行数 0） | 成为"审计写不进去 → 回滚"的实现 |
| 守卫四种等价写法 | V6：字符串键、下标列键**放行** | 四种全部报警 |
| 守卫判不出的两种形式 + 整行删除 | V6：三种全部**放行** | 三种全部报警 |
| 正常对照四条（改密三种写法、别的表、只读） | 放行 | 仍放行 |
| 计划正文里的守卫（从 Markdown 提取后实跑） | — | 11 条用例全部符合预期；对真实 `src/` 零命中 |
| 计划正文里的反例用例（提取后 pytest 实跑） | — | `13 passed` |
| 隔离变异（错误 owner + 下标列键 / `**` 展开 / 整行删除） | 基线绿 | 三条各自转红；合法 owner 改密码仍绿；还原后绿 |
| `_conflict_targets` 对计划自己声明的四张表 | 按约束名的第一版：主键 `name is None` → 恒红 | 按列组合：四张表逐格相等，helper 自证通过 |

### 本轮删掉的重复与矛盾规则

1. `apply()` 复用 `operation_id` 的**两个**互斥期望 → 只剩一个（回滚用例），另一条改成覆盖原本缺失的直连路径。
2. `_AUDIT_CONSTRAINTS` + 两处按约束名分流的函数 + 一张"约束名 → 错误"表 → 全部删除。
3. 其中 `ck_..._directory_actions_are_single_phase → AdminAuditConflictError` 这条自称"理论上不可达"的映射 → 删除并改正：它如实落进 `PersistenceIntegrityError`。
4. 两处 `INSERT INTO admin_audit_events` → 一处。
5. `_AUDIT_WRITE_SITES` 的多余允许项 → 单元素集合。
6. 反例里那份 `_owners_credential` 判定逻辑副本 → 删除，反例直接调用守卫。
7. `apply()` 外层不可达的 `except sa.exc.IntegrityError` → 删除。

### 未覆盖

- 计划里的代码块**仍然绝大部分未执行**。本轮实跑的是：六项真库探针、守卫本体（从 Markdown 正文提取）、13 条反例用例、三条隔离变异、`_conflict_targets` 对四张表声明的一致性。PostgreSQL 实现本身要等 Task 6 才第一次运行。
- 真库探针用的是本机 Compose 里那个 PostgreSQL 16.15 容器，建了独立探针库、跑完即删，**没有触碰 `xiaowei` 库**。探针脚本不进仓库：它需要一个跑起来的库和一个能建库的账号，是一次性事实核对，不是可重复回归。它证明的每一条在 Task 6 的集成用例里都有对应断言。
- `append_started` / `append_terminal` 的成功路径在 W1a 仍无行为覆盖（有意的边界，按 `ADR-013:129` 归 W4a）。
- 无源码、无 migration、无部署、无用户验收证据。
- GitHub CI：8 个 job 全部 0 steps，**未执行**。不记作通过。

### 残余风险

- **连续第四轮出现"改了一处、没跟到下一处"这一族。** V4 是 allowlist，V5 是既有契约字段，V6 是持久化边界，V7 是错误语义。本轮的结构性回应是"单一真源 + 引用"，而不是再加一条检查：矩阵存在之后，分叉会变成两处文字不一致，而不是两条用例互斥。这仍然靠人眼核对，没有机械手段。
- 三张矩阵本身没有机械守卫。正文与矩阵不一致时不会有测试变红。复审项第 12 条就是为此设的核对方式。
- AST 守卫的静态上限：别名 / `Any` / 未注解参数绕得过签名发现；运行时算出来的列名只能报警不能判定；`sa.text("insert into ...")` 不在视野内。这些是这类守卫的能力边界，不是没写好，**不得说成"已封死"**。真正的兜底是 `_AUDIT_WRITE_SITES` 这种单元素名单——名单越小，绕过它需要的改动越显眼。
- engine 未开 `hide_parameters=True`，全仓库既有状态，本阶段不改（见上一节）。
- `AuthenticatedPrincipal.subject_ref` 仍未标 PII 遮蔽，留给 W2。

---

## V8 修订记录

针对 `1c6da6c6c46b8318fd3c2b9ab0976c858ad1f8ab` 的复审。三条 P1 逐条核实，**全部成立**。

复审对根因的判断比我自己的更准，直接引用：**"临时代码片段分别验证过，却没有按最终
计划形状组合验证。"** 上一轮我报告的"13 passed"证明的是我手边那个拼出来的片段集合，
不是 Task 7 那个文件——而那个文件按正文拼起来时带着一组互斥断言。

### Q. 守卫文件从未被当成一个文件跑过（P1-1）

**根因** —— 见上。两处缺陷都只要把整个文件跑一次就会暴露：

| 缺陷 | 事实 |
| --- | --- |
| `test_the_frozen_sites_are_still_allowed` 互斥 | 先断言两个目录写入点 ∈ `_AUDIT_WRITE_SITES`，紧接着断言 `_AUDIT_WRITE_SITES` 只含 `_insert_audit_event` 且与 `_AUTHZ_WRITE_SITES` 不相交。`_insert_audit_event` 收口是 V7 做的，这条正常对照没跟着改 |
| `_helper_names()` 解析错 | `site.rsplit(".", 1)[-1]` 对 `persistence/postgres.py::_insert_audit_event` 得到 `py::_insert_audit_event`——路径里本来就有点号。名单里全是 `类.方法` 时看不出来，加进第一个模块级 helper 的那一刻它就生效了。于是 `_forge()` 调用唯一落库口时，那条"声称存在"的调用点守卫根本不命中 |

**修复位置** —— `_helper_names()` 先按 `::` 拆路径再取 owner 末段；新增
`test_the_helper_names_really_are_function_names` 直接断言集合等于三个函数名；把调用点
扫描抽成 `_helper_call_sites(module, relative)`，让反例调用**守卫本身**；新增
`_forge()` 反例与"合法调用点确实被发现"的正常对照；重写那条互斥的正常对照。

**结构性修订（本轮的真正回应）** —— 不是再写一条"记得整体验证"，而是让它可机械执行：

1. **Task 0 新增第 4 条机械核对**：按段落标记把每个文件的全部代码块拼回去，跑
   `ruff --select F821,F811`。它查的不是"声明之间对不对得上"（前三条查这个），而是
   "拼起来之后还成不成立"。附带约束：**每个文件只能有一份导入块**，不允许"再补两行
   导入"式的补充块——那正是 P1-3 的形态。
2. **Task 7 Step 1 增加一步**：守卫文件不 import 任何 W1a 运行期符号，因此写任何源码
   之前就能整体 `pytest`。预期只有"发现集为空"那一条红，其余全绿；**别的**用例变红就
   是这个文件自身的问题。

### R. 内存实现的回滚挑异常类型（P1-2）

**根因** —— `apply()` 只在 `UserDirectoryError` / `AdminAuditConflictError` 时恢复快照。
PostgreSQL 那边回滚的是整个事务，而事务不会问"这是哪一类异常"。审计派生、契约校验或
helper 抛出的任何其他异常——`ValidationError`、`KeyError`、一个 `RuntimeError`——都会让
内存实现停在"授权已改、审计没写"的半状态。两个实现在共享套件**之外**悄悄分叉，而分叉
的那一格恰好是本计划最核心的不变量。

**修复位置** —— 改成 `except Exception`：**先恢复，再决定抛什么**；只有
`AdminAuditConflictError` 做错误映射，其余原样重抛。新增
`test_any_failure_after_a_directory_write_leaves_nothing_behind`：注入一个
`RuntimeError`（既不是 `UserDirectoryError` 也不是 `AdminAuditConflictError`，正好落在
上一版两个 except 之外），断言四张授权事实 + 审计 + stage keys 逐张回到调用前。

**为什么不放进共享套件**：故障注入点两边不同（内存是 `_append_locked`，PostgreSQL 是
`_insert_audit_event`），放进共享套件要再造一套注入机制。PostgreSQL 侧的同一条语义由
事务本身保证，已由 `test_rolled_back_change_leaves_no_row_in_either_table` 在真库上证明。

### S. Task 6 的导入与反证（P1-3）

| 缺陷 | 修复 |
| --- | --- |
| 集成测试用 `AdminAuditOutcome`，旁边写着"该文件已经导入" | 全部名字并进文件顶部**唯一**那份导入块，删掉补充块与那句错误说明 |
| PostgreSQL 实现导入清单缺 `AdminAuditOutcome` | 补上 |
| `rows.py` 的 `"AdminAuditEvent"` 前向引用没有 `TYPE_CHECKING` 导入 | 写明加进 `rows.py:46` 那个既有的 `if TYPE_CHECKING:` 块 |
| Step 8 的"事务变异"只把 `return` 挪出 `async with`，两个写仍在同一事务里，而正文自己写着"上面的变异只是示意形状" | 换成两条真变异：A 用 `AUTOCOMMIT` 让目录写与审计写不同事务（预期红在 `assert accounts == []`）；B 把冲突探测的返回值改成无条件成功（预期红在 `pytest.raises`）。两条红在**不同断言**上 |

**每条变异脚本现在都先 `assert after != before`。** 上一版那条脚本没有——`t.replace`
匹配不上时静默返回原文，于是"变异后没变红"被当成了"保护有效"。一条无法确认自己生效
的变异，证明不了任何事。

### 同类问题一起处理（复审未点到）

- `BootstrapLocalAdminCommand` 的 docstring 写着"`local_admins` 已有行时整条命令是
  no-op"——那是 **V1 的缺陷原文**。四态表在 V2 改对了，这段 docstring 没跟着改。任何跑
  过 RI5 的库都早有这一行，按 docstring 实现会重新引入那条只在升级路径上才踩得到的裂缝。
  改为指向四态表这个唯一真源。
- 新增的回滚用例原本手搓了一个 `BootstrapLocalAdminCommand`，**漏了 `display_name`**
  ——与 V5 给 `ChangePasswordCommand` 传不存在字段同一类。改成调用套件已有的
  `_bootstrap_command()`。
- Task 6 Step 2 那一段同时装着两个文件的代码块，只靠散文区分。改成每个文件一个
  `追加到 \`<path>\`：` 标记——这既是给读者的，也是第 4 条机械核对的输入。

### 先红后绿

| 证据 | 先 | 后 |
| --- | --- | --- |
| 拼出的 Task 7 文件对真实 `src/` | `test_the_frozen_sites_are_still_allowed` 红（互斥断言） | 只剩"发现集为空"一条红（W1a 源码尚不存在，预期内） |
| 拼出的 Task 7 文件对 stub 树 | 同上 | `25 passed` |
| `_helper_names()` | `{'py::_insert_audit_event', '_write', '_append_locked'}` | `{'_insert_audit_event', '_write', '_append_locked'}` |
| 变异：外部 `_forge()` 调用唯一落库口 | 守卫不命中（名字解析错） | `test_the_full_candidate_helpers_have_only_declared_call_sites` 红 |
| 变异：新增收完整候选 helper | — | `..._are_exactly_the_declared_ones` 红 |
| 变异：第二条审计 INSERT | — | `test_audit_table_is_written_only_at_frozen_sites` 红 |
| 变异：字符串键写 `user_id` | — | `test_the_credential_link_is_written_only_at_frozen_sites` 红 |
| 变异：模块级写授权表 | — | `test_authorization_tables_are_written_only_at_frozen_sites` 红 |
| 五条变异逐条还原后 | — | `25 passed` |
| 15 个文件拼回后的 `F821,F811` | 集成测试 5 处（未定义 `AdminAuditOutcome` 等 + 重复 `pytest`） | 15 / 15 干净 |

### 未覆盖

- 第 4 条机械核对只做**静态**检查（未定义名、重复导入）。它**不能**验证：拼出的文件能否
  import（W1a 模块尚不存在）、断言之间是否矛盾、代码是否正确。Task 7 的守卫文件是唯一
  能整体跑起来的那个，因为它不依赖任何 W1a 运行期符号。
- Task 6 的 PostgreSQL 实现不是通过"创建 `<path>`："标记给出的（它是往既有
  `postgres.py` 末尾追加），因此**不在**第 4 条的覆盖范围内。它的导入清单是人工核对的。
- 两条真变异（Step 8 A/B）本轮**没有执行**：它们需要 W1a 源码与真实 PostgreSQL，属于
  Task 6 实施时的动作。本轮只保证脚本形状正确且带 `assert after != before` 自检。
- 无源码、无 migration、无部署、无用户验收证据。
- GitHub CI：8 个 job 全部 0 steps、耗时 3 秒，注解为账号计费未启动。记作**未执行**。

### 残余风险

- **"片段验证 ≠ 整体验证"这条，第 4 条核对只挡住了静态的一半。** 断言互斥、语义矛盾这类
  问题，静态检查看不出来；本轮那条互斥断言是靠**跑**出来的，而能跑的只有 Task 7 一个文件。
  其余 14 个文件要等 Task 6/7 实施时才第一次运行。
- 连续第五轮出现"改了一处，没跟到下一处"（V4 allowlist、V5 契约字段、V6 持久化边界、
  V7 错误语义、V8 `_insert_audit_event` 收口后的正常对照与名字解析）。每一轮的结构性回应
  都只覆盖了当轮那一层。
- 三张矩阵仍无机械守卫；正文与矩阵不一致不会有测试变红。
- AST 守卫的静态上限（别名 / `Any` / 未注解参数 / 运行时列名 / `sa.text`）不变，不得说成
  "已封死"。
- engine 未开 `hide_parameters=True`；`AuthenticatedPrincipal.subject_ref` 仍未标 PII 遮蔽。

---

## V9 修订记录

针对 `5c03032264dfad2d42ef4b57f92c65fc46aff551` 的复审。四条 P1 逐条核实，**全部成立**，
另一条非阻断漂移一并修正。

四条**不是四个独立缺陷**，是三个根因：

| 复审条目 | 根因 |
| --- | --- |
| 1（降级守卫签名）、3（`user_id` 没贯穿读取路径） | **T. 与既有代码的接缝只写了自己那一端** |
| 2（Task 5/6 文件闭集假绿） | **U. 任务的文件闭集只在计划内部自洽** |
| 4（半迁移被误判为已迁移） | **V. 把"剔除后靠后面兜底"当成了闭集判据** |

### T. 与既有代码的接缝只写了自己那一端

**根因** —— 计划触碰仓库里已有的符号时，只写了计划这一侧，没有去读真实定义核对另一侧。
调用方向和被调用方向各失守一次：

| 方向 | 事实 |
| --- | --- |
| 调用既有函数 | `rev_0014.downgrade()` 写的是 `require_destructive_authorization("rev_0014 downgrade …")`，一个位置字符串。真实签名是 `(connection, *, guarded)`（`guards.py:48`）。**有数据时降级抛的是 `TypeError`，不是 `MigrationSafetyError`**——守卫在文档里在，在运行时从来没生效过。更刺眼的是紧挨着它的那句话："先读 `guards.py` 确认确切签名，按它的既有形状调用"：提醒写了，核对没做 |
| 给既有契约加字段 | `LocalAdminRecord` 加了 `user_id`，但它在 `src/` 有**四个**构造点，四个都不传这个字段。默认 `None` 让它们全部静默通过，于是 `get()` 与改密返回值都报告"未关联"，而数据库那一列好端端地存着 |

第二条的危害不止于"读不到"：`None` 在 bootstrap 四态表里正好是第二行（backfill），
一个漏回填的读取路径会让"已经链接好了"看起来像"还没升级"。

**修复位置**

- Task 4 Step 4：`downgrade()` 改成 `require_destructive_authorization(op.get_bind(),
  guarded=((_USER_ACCOUNTS, "user_accounts"), (_ADMIN_AUDIT_EVENTS,
  "admin_audit_events")))`，外面套 `if not context.is_offline_mode():`，形状逐字取自
  `rev_0013_clarification_parent.py:79`。**一并删掉**手工写的 `has_accounts` /
  `has_events` 预检：`_row_counts` 已经在数同样两张表，守卫自己就有"没有受保护行就返回"
  的分支，而手工预检把 `counts` 这个分类计数降级成了一个布尔——`counts` 正是既有用例
  断言的那个值。
- Task 4 **新增 Step 7**：在真实 PostgreSQL 上跑降级守卫——拒绝（`counts` 精确相等）、
  版本不动、数据一行不少、显式授权后真的删掉、再升回来且 `local_admins.user_id` 回来。
  外加一条正常对照"空目录降级不需要授权"，否则前一条无法区分"按数据拦住"和"无条件
  拦住一切"。离线用例一条都抓不到这个缺陷，**因为离线用例根本不执行 `downgrade()`**。
- Task 5 Step 6：把 `LocalAdminRecord` 的**四个构造点逐个列成表**（AST 发现），每个写清
  本轮要变成什么。
- Task 6 Step 5：`get()` 补 `user_id=row["user_id"]`；改密改用
  `.returning(LOCAL_ADMINS.c.id, LOCAL_ADMINS.c.user_id)` 并从中回填——**不能凭命令现搭**，
  命令里根本没有这个字段；`InMemoryLocalAdminStore` 改密同样带上原记录的值。
  `test_changing_the_password_keeps_the_directory_link` 从只断言数据库列，扩成三条：
  列没被清空、改密**返回值**带着链接、下一次**读取**也带着链接。

**同类问题（复审未点到，一并处理）**

- Task 6 的 PostgreSQL 探针写 `updated_at=_FIXED_TIME`，旁边注着"用该 conftest 已有的固定
  时间常量"——**两个 conftest 里都没有这个名字**，按原文跑是 `NameError`。改为接根
  `conftest.py` 的 `clock` fixture。
- `contracts/identity.py` 的导入块缺 `Final`，而 Task 6 要往里追加五个 `Final[str]` 常量。
- `tests/integration/conftest.py` 的新 fixture 用了四个从未说明要导入的名字。
- `test_identity_contracts.py` 的新用例用了 `Path` / `inspect` / `local_admin_auth` /
  三个常量，一个都没导入。
- 后三条都是**新增的机械核对自己查出来的**，不是我重读一遍发现的。

**结构性修订** —— Task 0 机械核对新增两条，方向正好相反：

- **第 5 条（调用方向）**：把计划所有 Python 代码块里对既有 `src/` 符号的调用抽出来，用
  `inspect.signature().bind()` 逐个绑实参。绑不上就报错。
- **第 6 条（被调用方向）**：给既有契约加字段时，该契约在 `src/` 的全部构造点由 AST 发现，
  与计划里那张清单做 `==`。将来任何人新增一个构造点，这条核对都会变红。

第 4 条也一并加强：**追加到既有文件时先把真实文件内容垫在前面再查**，于是"这个名字在
那个文件里本来就有"不再靠记忆判断；`修改` 开头的标记现在也会终止上一段，不会再把下一段
代码错误地并进上一个文件。

### U. 任务的文件闭集只在计划内部自洽

**根因** —— Task 5 正文要改 `persistence/local_admin.py`，Task 6 正文要改
`contracts/identity.py`、`interfaces/local_admin_auth.py` 与两个契约测试文件；这五处在
`Files:` 与 `git add` **两边都没有**。V4 加的第 2 条机械核对查的是"`Files:` 声明了但
`git add` 漏掉"——**两边一起漏**，它按定义查不出来。后果是本地全绿、提交里缺文件，而
每个任务的提交本应自己能跑。

**修复位置** —— Task 4 补 `tests/integration/test_migration_paths.py`；Task 5 补
`persistence/local_admin.py`；Task 6 补 `contracts/identity.py`、
`interfaces/local_admin_auth.py`、`tests/contract/test_identity_contracts.py`、
`tests/contract/test_identity_schema.py`。`Files:` 与 `git add` 同步改，第 2 条核对保持
`（无）`。

**结构性修订** —— 一静一动，配对使用：

- **Task 0 第 7 条（静态）**：正文里凡用 以 `创建` / `追加到` / `修改` 加反引号路径加全角冒号标记写到的文件，
  必须出现在该任务的 `Files:` 里。因此**每个要落盘的文件都必须有这样一个标记**——
  "放在 X 里"这种散文提法不再算数。本轮把 Task 6 里六处散文提法改成了标记，其中
  `tests/contract/test_identity_schema.py` 当场就是红的。
- **Global Constraints + 每个提交步骤（动态）**：提交之后 `git status --porcelain` 必须为空。
  这一条兜的正是静态检查兜不住的那一半——计划从头到尾没提过某个文件时，静态检查看不见
  它，但只要真的改了它，工作区就不干净。不干净就停下来判断该不该把它加进本任务，
  **不要**用 `git add -A` 扫进去。

### V. 把"剔除后靠后面兜底"当成了闭集判据

**根因** —— 旧身份迁移只要 `load_account()` 非空就记 `skipped`，并附了一句自我安慰：
"即使它漏判，第 4 步的数据库唯一约束仍会让整批回滚"。这句话不成立，**而且恰恰是因为
第 3 步自己**：被判为 `skipped` 的条目已经从批次里剔除，第 4 步根本不会碰它，唯一约束
当然不会执行。于是一个"账号和角色在、飞书绑定不在"的半迁移用户被静默跳过，报告写着
成功，而那个人仍然登不进来——没有任何一条断言会因此转红。

凡是"先剔除、再让后面的约束兜底"的结构，都要问一句：剔除之后，后面的约束还看得见它吗？
看不见，那这一步就是最终判据，必须自己 fail-closed。

**修复位置** —— Task 8 Step 5 第 3 步：账号、actor、状态、角色与 `resolve_by_subject()`
绑定**五项全部精确匹配**才跳过；部分存在或任何一项不一致立即抛
`LegacyMigrationConflictError`，整批不写。Step 4 新增两条反例
（`test_an_account_without_its_binding_is_a_conflict_not_a_skip`、
`test_a_role_that_no_longer_matches_is_a_conflict`），前者同时断言"另一条没写进去"
与"审计一条没多"——只断言抛了异常，无法区分"整批回滚"和"写了一半才炸"。本文件已有的
`test_rerunning_the_migration_changes_nothing` 就是它们的正常对照：完整迁移过的条目仍然
要判 `skipped`，否则迁移变成不可重跑。

**同类问题（复审未点到，一并处理）** —— Task 8 Step 7 的变异反证写的是
"`# 手工把第 4 步改成 …`"，正是 V8 刚在 Task 6 修掉的"示意形状"。改成两条可执行变异，
各自带 `assert after != before` 自检，且分别红在不同的断言上。

### 非阻断：README 阶段漂移

`README.md:14` 仍写着"当前阶段是 W0 文档与 ADR 真源收口……W0 合入后才编写 W1a 详细计划
并送审"，而 W0 已由 PR #61 合入 `main@a12578cd`，`AGENT_HANDOFF.md:23` 已写"当前阶段：
W1a 详细计划送审"。两份当前真源文档互相矛盾时不静默选一边——这里 handoff 有合入 SHA
作证据，README 落后，按 handoff 改。措辞保持"计划获批后才可开始 W1a 实现"，**不**写成
"W1a 已开始"。
