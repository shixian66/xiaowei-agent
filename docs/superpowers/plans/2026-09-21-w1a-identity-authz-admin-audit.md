# W1a 用户、权限与 Admin 审计写内核 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan slice-by-slice. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付身份目录与 Admin 审计的**写内核**：四类授权事实只有一条写路径，每一次授权改变都在同一个事务里带上一条由命令派生的 append-only 审计事件。

**Architecture:** `contracts/` 定义闭集与命令；`persistence/` 提供内存与 PostgreSQL 两个实现，共享同一套行为套件；`interfaces/` 只做旧身份的一次性迁移。不新增任何 HTTP 路由、页面或运行配置。

**Tech Stack:** Python 3.11、Pydantic v2（strict/frozen/extra=forbid）、SQLAlchemy 2.0 Core + asyncpg、Alembic、pytest。

## 这份计划写到哪一层

**它不是逐行代码稿。** 前八轮复审有五轮的缺陷出在计划里那些**从未运行过**的 Python/SQL/Shell
片段上——签名记错、导入漏写、反例没钉住目标、变异没真的撤掉保护。这些片段看着像证据，
其实一行都没跑过；围着它们逐轮打补丁，既不能提前发现真问题，又让计划膨胀到读不完。

所以这份计划只写四样东西：

1. **接口形状** —— 契约字段、Protocol 方法签名、表的列与约束名。它们是切片之间的契约，写错会让下一个切片停工。
2. **关键不变量** —— 必须成立的事实，以及**它为什么必须成立**。这是复审真正要核的东西。
3. **测试意图** —— 每条用例的名字与它钉住的那一件事。测试正文在实现阶段对着真实代码写。
4. **验收要求** —— 什么算做完，什么算假绿。

具体语句（SQL 文本、断言正文、变异脚本）留到 TDD 实现阶段，对着真实代码和真实数据库写。
实现阶段如果发现本计划的接口形状与真实代码不符，**以真实代码为准**，并把冲突报出来。

## Baseline and Evidence Boundary

- 计划分支：`claude/w1a-identity-authz-audit-plan`；基线 `origin/main@a12578cd59cfaccf3fe6702e6437502b9462c28d`（PR #61 squash 合入 W0）。
- W0 只证明**文档与 ADR 真源已收口**，不是本阶段任何源码、迁移、部署、真实调用或用户验收的证据。
- 编写本计划前已按 `AGENTS.md` 顺序读取 `ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`，并读取规格、ADR-013 修订，以及 `contracts/{enums,channel,base}.py`、`persistence/{schema,store,local_admin,rows,errors,fake,memory,postgres}.py`、`persistence/migrations/{guards.py,runner.py,versions/rev_0013_*.py}`、`interfaces/{feishu_identity,local_admin_auth,local_stack}.py`、`tests/suites/`、`tests/integration/conftest.py`、`tests/integration/test_migration_paths.py`、`tests/contract/test_schema_matches_migration.py` 的现有形状。
- 计划分支实测四门：`3977 passed, 269 skipped` / security `1447 passed, 83 skipped, 2716 deselected` / `ruff` 全绿 / `mypy` 189 个文件无问题；文档契约 `47 passed`。实现者必须从届时最新 `main` 重新跑取当期数字，**不得**把本行当作未来运行结果。
- 本计划为 V11。前十版的返工来历见文末「返工来历」，只作教训保留，不再是当前规则。

## Global Constraints

- **W1a 是写内核阶段，不是产品阶段。** 允许变更的路径见下面的 allowlist。**禁止**新增任何 HTTP 路由、页面、模板、静态 JS、Compose 服务或运行配置文件。
- **W1b/W2/W3 未授权。** 不创建 `ActivationRequest`、`ActivationStore`、`IdentityActivationService`、`web_oauth_login_contexts`、通知 Port、登录页、Admin 页面或审计查询 API。W1a 只交付它们将来要复用的写底座。
- **`ChannelPermission` 精确三成员，不扩展**：`VIEW_SAFE_TASK`、`SUBMIT_READONLY_TASK`、`ADMIN_ALL_SAFE_TASKS`。管理面能力一律进独立的 `AdminCapability`。
- **`AdminCapability` 七成员，逐字取自 ADR-013 修订**：`MANAGE_USERS`、`MANAGE_DUTY_BINDINGS`、`VIEW_ADMIN_AUDIT`、`VIEW_PRIVATE_TASK_CONTENT`、`VIEW_INTEGRATION_STATUS`、`MANAGE_INTEGRATIONS`、`RUN_CONNECTION_TESTS`。其中后两个**只允许 `LOCAL_ADMIN`**。
- **审计事实由 store 从命令派生，调用方不得组装。** 调用方只提供 `AdminOperationContext`（operation、操作者、认证来源）；`action`、`target_kind`、`target_ref_digest`、`outcome`、effect 全部由 store 根据命令计算。**不提供任何让调用方指定它们的参数**——包括 override、hook 和可选覆盖字段。理由：能被调用方指定，就能被调用方写错；"校验它有没有写错"永远弱于"它根本没有机会写"。
- **授权改变只有一条写路径。** `UserDirectoryStore.apply()` 是 `user_accounts`、`user_role_assignments`、`external_identities` 与 `local_admins.user_id` 的**唯一**写入口。本地 Admin bootstrap 与旧身份迁移都必须是 `DirectoryCommand` 的成员，不得另开写方法、另开事务或另写一份 SQL。
- **授权改变与审计同事务。** 审计写入失败必须使整个授权改变回滚。成功的目录动作必须携带**闭集** effect（新角色 / 新状态），由数据库 CHECK 强制；否则历史状态被后续改动覆盖后，就再也无法还原当时授予了什么。
- **审计 append-only，写契约在 W1a 就交付。** `DEVELOPMENT_PLAN.md:178` 与 `ADR-013:131` 都要求 W1a 先提供持久化与 append-only 写契约、W1b 作为消费者复用它。`AdminAuditStore` 的表面恰好是 `append_started` / `append_terminal` / `append_denied` / `load`；`persistence/` 中不得出现针对 `admin_audit_events` 的 `sa.update()` / `sa.delete()`；不提供清理、更新或删除 API。写方法必须**窄到写不出目录成功事实**：通用的 `append(candidate)` 不可接受。
- **审计事件不含自由文本。** 允许的列只有规格 §14.2 逐条列出的 13 个，加上闭集 effect 列 `effect_role` / `effect_status`。**不新增** `message`、`detail`、`payload`、JSON 或任何可容纳异常正文与用户输入的列。
- **一次操作最多一条 STARTED 和一条终态事件。** `operation_id` **不能**全局唯一（会堵死规格 §14.2 的两阶段配置审计）；用两条 partial unique index 表达。
- **`subject_ref` 是受控 PII。** 飞书 `open_id` 在新契约上必须 `exclude=True, repr=False`，数据库只存 domain-separated 摘要，不进普通日志、trace、异常和审计正文。
- **所有持久 ID 有界。** 由外部输入派生的 ID 必须经摘要截断得到有界值，**不得**直接拼接 actor 这类没有长度上限的外部字符串。
- **长度边界必须两端闭合。** 新契约的上限，和它要接收的旧数据的实际取值范围，必须对得上。契约收得比输入紧，就是把一批真实旧数据永久挡在门外；收得比输入松，超长值会一路写到库里。所以三个上限在本计划里是**确定值**：`_ID = 64`、`_NAME = 128`、`_ACTOR = 256`。超出上限的旧数据是**整批零写入**的错误，不是"截断后继续"；只有 `display_name` 这一个**纯显示**字段允许截断，因为它不参与任何身份或授权判定。凡是写下"超长就拒"的地方，都必须能在另一处指出这个阈值之下的旧数据是完整迁移的——两句话不能同时为真又互不相干。
- **认证生命周期事件不进 `AdminAuditStore`。** 登录成功/失败、改密、登出走结构化安全日志；不新建第二套认证审计表。改密因此仍留在 `LocalAdminStore`：它改的是凭据，不是授权。
- **不提前建无消费者的名单。** 不创建 requester/approver 表、列或枚举成员；旧 `approver` 标签只进迁移报告，不生效任何授权。DBA/值班绑定属于 W3。
- **旧静态 JSON 不双写。** 迁移成功后静态文件保留只读一个发布周期；本阶段不删除它，也不让它与数据库目录同时成为写入目标。
- **不放宽既有硬门。** SQLGuard、ToolPolicy、ApprovalGate、`ToolGateway`、终态保护、RI2/RI3/RI4/RI6 真实调用门全部不变。W1a 网络调用次数恒为 0。
- **Secret 纪律。** 测试夹具里的类 secret 字面量一律拆开写（`"hunter" + "2-plain"`），口令哈希字段用 `SecretHash` 标注并 `exclude=True, repr=False`。
- **不改权威测试命令**：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。触及 `governance/` 必须全量跑 security gate。
- **合格 RED 的判定。** 失败必须是**被测事实尚不存在**造成的（`ModuleNotFoundError` / `ImportError` / `AssertionError`）。不合格的是**测试自身写错**造成的失败：fixture 名拼错、参数名不符、语法错误。分辨方法：错误指向的是你要实现的东西，还是你刚写的测试？后者先修测试再重新取 RED。
- **反例只允许一项事实不成立。** 一条反例要证明"少了 X 就会被拒绝"，就必须把 X 以外的全部前置事实建好；否则它可能是被**别的**判据挡住的，删掉 X 的检查它照样绿。本轮复审抓到的正是这一条：角色不匹配的反例没有预建飞书绑定，于是它测的是"绑定缺失"，不是"角色不匹配"。
- **变异必须真的移除目标保护。** `assert after != before` 只证明脚本改到了字节，不证明改掉的是那道保护。每条变异要写清"它把代码恢复成了哪一个具体的旧缺陷"，并确认目标用例红在**预期的那条断言**上。把分支条件改成 `if False:` 而真实缺陷是"走另一个分支"，就不是有效变异。
- **主动降级的用例必须把 schema 还回去。** 集成测试的 `migrated_engine` 是 session 级、只升一次，`clean_database` 只 `TRUNCATE` 不重建 schema。任何 `run_downgrade` 的用例都必须 `try/finally`，`finally` 里无条件升回 `head`，并断言 revision、表与 `local_admins.user_id` 都已恢复；验证时必须**把该用例所在的整个文件**跑一遍，不能只跑单例。这条对**所有**主动降级的用例成立，不只是迁移测试文件里的那两条——切片 B 的 `test_an_already_seeded_database_gets_its_directory_backfilled_on_upgrade` 同样要降级到 `rev_0013` 去制造旧数据，它也必须还原。漏了这一步，后续用例会在 `rev_0013` 上运行，甚至在 `TRUNCATE` 阶段直接失败——那是测试顺序依赖，不是本用例的失败。
- 不得靠删断言、放宽集合、跳过用例、吞异常或加特殊分支制造全绿。
- 每个提交只暂存本切片列出的文件；不使用 `git add -A`。**每个提交之后 `git status --porcelain` 必须为空**，且该提交自己能跑——它兜的是静态核对看不见的那一半：某个文件计划从头到尾没提过，`Files:` 与 `git add` 两边一起漏。不干净就停下判断该不该把它加进本切片，不要扫进去。

## 与规格的两处有意偏离（复审已确认可接受）

1. **`UserAccount` 不带 `tenant_id/environment_id`，`actor` 全局唯一。** 规格 §6.1 的字段表里没有作用域，作用域只出现在 `UserRoleAssignment`。§6.6 要求"在同一作用域内拒绝 actor/subject/本地账号冲突"——全局唯一的 `actor` 严格强于该要求。
2. **`LocalCredential.username` 本阶段不落库。** 规格 §6.1 列出 `username`（首版固定 `admin`），但 W1a 没有消费者。本阶段只落 `local_admins.user_id`。**前提是 W2 继续把 `admin` 作为固定认证常量**，不得借机新增可变用户名或第二套账号真源；W2 计划必须复述这条前提。

## 三张规范矩阵（唯一真源）

这三张表是本计划里**唯一**一处定义错误语义、写入口与冲突探测方式的地方。每个切片只引用
它们，不再各自给一份答案。V6 的缺陷正是"同一条规则在多个章节各写一遍"：同一个"复用
`operation_id`"场景，一处期望 `AdminAuditUnwritableError`，另一处期望 `AdminAuditConflictError`。
规则散在多处时，分叉不会被任何人看见。

### 矩阵一：错误语义

归属规则一句话：**谁的公开方法失败，就用谁的错误族。** `apply()` 内部的审计冲突不以
`AdminAuditConflictError` 的面目出现——它是一次授权改变的失败，叫 `AdminAuditUnwritableError`。

| 公开入口 | 事实 | 抛出 | 授权改变 |
| --- | --- | --- | --- |
| `UserDirectoryStore.apply` | `user_id`、`actor` 或 subject 摘要与既有目录事实冲突 | `UserDirectoryConflictError` | 回滚 |
| `UserDirectoryStore.apply` | 命令的目标账号或角色不存在 | `UserDirectoryNotFoundError` | 回滚 |
| `UserDirectoryStore.apply` | 该 `operation_id` 在该阶段已有审计事件，审计写不进去 | `AdminAuditUnwritableError` | 回滚 |
| `UserDirectoryStore.apply` | bootstrap 的凭据行指向不存在的账号，或该账号没有 ADMIN 角色 | `UserDirectoryConflictError`（fail-closed） | 回滚 |
| `AdminAuditStore.append_started` / `append_denied` / `append_terminal` | 该 `operation_id` 在该阶段已有事件 | `AdminAuditConflictError` | — |
| `AdminAuditStore.append_terminal` | 找不到对应的 `STARTED` | `AdminAuditMissingStartError` | — |
| 以上任意入口 | 未知唯一约束、CHECK、外键、NOT NULL、schema、解码故障 | `PersistenceIntegrityError`，`write_outcome` 由 `_write_transaction` 判定 | 回滚 |
| 以上任意入口 | 连接、超时、连接池故障 | `PersistenceUnavailableError`，`write_outcome` 同上 | 见 `write_outcome` |

内存实现与 PostgreSQL 实现在这张表上**逐格相同**，共享套件断言的就是这张表。内存实现没有
事务，用快照恢复表达"回滚"那一列——它表达的是同一个语义，不是另一个。**恢复不挑异常类型**：
PostgreSQL 回滚的是整个事务，而事务不会问"这是哪一类异常"。

### 矩阵二：写入口

| 事实 | 唯一写入口 | 机械守卫 |
| --- | --- | --- |
| `user_accounts` | `apply()` → `_apply_locked` / `_apply_in_transaction` | `_AUTHZ_WRITE_SITES` |
| `user_role_assignments` | 同上 | `_AUTHZ_WRITE_SITES` |
| `external_identities` | 同上 | `_AUTHZ_WRITE_SITES` |
| `local_admins.user_id` | 同上 | `_CREDENTIAL_LINK_SITES` |
| `local_admins.password_hash` / `must_change_password` | `LocalAdminStore`（既有，本阶段不动语义） | 不禁止：改密是凭据，不是授权 |
| `admin_audit_events`（PostgreSQL） | 模块级 `_insert_audit_event`，**仅此一处**；目录路径与 `PostgresAdminAuditStore._write` 都调用它 | `_AUDIT_WRITE_SITES`（单元素）+ `_FULL_CANDIDATE_HELPERS` |
| `admin_audit_events`（内存） | `InMemoryAdminAuditStore._append_locked`，目录 store 在自己的临界区内复用它 | `_FULL_CANDIDATE_HELPERS` |

`_AUDIT_WRITE_SITES` **不写成 `_AUTHZ_WRITE_SITES | {...}`**：那等于顺带允许两个目录助手直接写
审计表，而它们现在都走 `_insert_audit_event`。把不需要的允许项留在名单里，守卫就比它能守的
范围松一圈，而松出来的那一圈正好是"绕过唯一落库口"。

### 矩阵三：事务与冲突探测

**已知冲突不靠捕获 `IntegrityError` 判定，而是用 `ON CONFLICT DO NOTHING ... RETURNING` 在事务体内
探测。** 这是仓库既有做法，不是本计划的发明：`PostgresTaskStore.create_task`（`postgres.py:1927`
的 docstring 写明了理由）、`PostgresPlanStore.save`（`:2222`）、`append_evidence`（`:416`）全是这个
形状，而 `persistence/` 里**没有任何一处**捕获 `IntegrityError` 来分类冲突。

| 层 | 职责 | 不做什么 |
| --- | --- | --- |
| 事务体内 | 已知唯一约束用 `ON CONFLICT DO NOTHING ... RETURNING` 探测；拿不到行就抛矩阵一的闭集领域错误 | 不捕获 `IntegrityError`，不读约束名，不碰 `exc.orig` |
| `_write_transaction`（`postgres.py:293`） | 没被探测吞掉的驱动异常在这里收敛，并按事务退出的事实判定 `ROLLED_BACK` / `NOT_CONFIRMED` | 不认识任何业务语义 |
| `@_persistence_boundary`（`postgres.py:244`） | 最终闭集与异常链切断 | 不区分冲突种类 |

闭集领域错误（`RuntimeError` 子类）穿过后两层时**原样通过**：`classify_persistence_exception`
对它们返回 `None`，两层都走 `raise` 重抛。

`ON CONFLICT DO NOTHING` **不带**推断目标：一条 INSERT 可能撞上该表的任意一条唯一约束，而带
目标时只推断一条。它吞掉的**只有**唯一/主键/偏唯一索引冲突；CHECK、外键、NOT NULL 一律照抛。
因此"未知约束被伪装成业务冲突"这条风险不靠运行期判别堵——运行期根本不去判别——而是靠一条
**静态**用例把这几张表的可推断冲突列组合逐格钉死：新增一条唯一约束就必须去那条用例里回答
"它撞了意味着什么"。

### 这三张表依据的实测事实

以下结论来自在真实 PostgreSQL 16.15 + asyncpg 0.30.0 + SQLAlchemy 2.0.52 上跑过的临时探针
（独立探针库，跑完即删），不是从文档或记忆推断的：

| 问题 | 实测结果 |
| --- | --- |
| `sa.exc.IntegrityError.orig` 是什么 | `sqlalchemy.dialects.postgresql.asyncpg.IntegrityError` |
| 它有没有 `.diag` | **没有**。`.diag.constraint_name` 是 psycopg 的接口，本仓库用 asyncpg |
| 它身上有什么 | 只有 `.sqlstate` / `.pgcode`（`23505`）；**没有** `constraint_name` |
| 约束名在哪 | 只在 `exc.orig.__cause__`（`asyncpg.exceptions.UniqueViolationError`）上——而 `str(exc)` 同时带着被拒绝的那一行，这正是 `_persistence_boundary` 存在的目的 |
| 事务体内抛 `IntegrityError` 后外层能否 `except sa.exc.IntegrityError` | **不能**。`_write_transaction` 已把它换成 `PersistenceIntegrityError`，且 `__cause__` 为 `None` |
| 不带推断目标的 `ON CONFLICT DO NOTHING` 吞掉什么 | 主键、普通唯一约束、**偏唯一索引**三种冲突：`RETURNING` 为空且不抛异常 |
| 它不吞什么 | CHECK、外键、NOT NULL 照抛，经两层收敛成 `PersistenceIntegrityError(constraint, rolled_back)` |
| 事务体内抛闭集领域错误会怎样 | 穿过两层不变形；同事务里已发出的授权 INSERT 已回滚（实测该表行数为 0） |

探针不进仓库：它需要一个跑起来的库和一个能建库的账号，属于一次性事实核对，不是可重复的
回归。它证明的每一条，在切片 B 的集成用例里都有对应断言。

## 切片划分

W1a 拆成三个**各自独立可验收、可复审、可合入**的切片。切片之间只通过下面列出的接口形状
耦合；前一个切片合入后，后一个才开工。

| 切片 | 交付 | 独立可验收的理由 |
| --- | --- | --- |
| **A. 契约与迁移** | 枚举、身份目录契约、审计契约、四张表、`rev_0014` | 只新增契约与表，没有消费者。合入后仓库四门全绿，运行时行为零变化 |
| **B. Store 与审计原子性** | 内存 + PostgreSQL 两个实现、共享行为套件、`seed_if_absent` 改道、三组机械守卫 | 依赖 A 的契约与表。合入后"授权改变必带同事务审计"这条不变量已经成立并被守卫钉住 |
| **C. 旧身份迁移** | 公开的旧文档解析契约、一次性原子迁移命令、文档真源同步 | 依赖 B 的 `apply()`。它是唯一对**既有部署数据**产生影响的一步，单独复审、单独合入 |

**依赖是单向的**：A 不知道 B，B 不知道 C。任何一个切片如果需要回头改前一个切片的接口形状，
先停下来把冲突报出来——那说明接口设计错了，不是补丁问题。

### 本阶段允许出现在 diff 里的路径

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
README.md
```

几条不显然的说明：

- `persistence/rows.py`：行与契约的互转按仓库既有约定就住在那里（`row_to_channel_binding` /
  `channel_binding_to_row` 等十余对）。审计事件的两个方向必须和它们并排，而不是在
  `postgres.py` 里另起一份——另起一份就是第二套列映射，两条写路径迟早分叉。
- `contracts/base.py`：只为把 `SecretHash` 这一行别名搬家。它现在定义在
  `persistence/local_admin.py:42`，而 `contracts/identity.py` 要按同一套约定标注 `password_hash`；
  契约模块不能反向依赖 persistence，在两处各写一份别名就会让"同一套约定"变成两套。
- `interfaces/local_admin_auth.py`：只为让它消费 `contracts/identity.py` 的五个本地管理员常量，
  并删掉自己那三个字面量。`persistence` 按 `test_module_layering.py:47` 不能反向导入 `interfaces`，
  所以真源必须下沉到契约层。
- `interfaces/feishu_identity.py`：切片 C 需要一份**保留原始 labels** 的公开只读解析契约，而
  当前的 `load_feishu_identity_directory()` 已经把 labels 压成了 `frozenset[ChannelPermission]`
  （`feishu_identity.py:125`），原始标签在返回值里不复存在。切片 C 只**抽出**已有的解析逻辑并
  把它公开，不改变校验规则、大小上限、权限位检查，也不改变任何现有调用方的行为。

任何超出该列表的文件出现在 `git status` 里，都必须先停下来说明理由。

### 开工前的机械核对

```bash
python - <<'CHECK'
import pathlib, re

text = pathlib.Path(
    "docs/superpowers/plans/2026-09-21-w1a-identity-authz-admin-audit.md"
).read_text(encoding="utf-8")

# 1. 每条 Files: 声明都必须落在 allowlist 里
allow = re.search(r"里的路径\n\n```\n(.*?)```", text, re.S).group(1).split()
declared = {
    m.group(1)
    for line in text.splitlines()
    if (m := re.match(r"^- (?:Create|Modify|Test): `([^`]+)`", line.strip()))
}
print("1) 超出 allowlist：", sorted(
    p for p in declared
    if not any(p == a or (a.endswith("**") and p.startswith(a[:-2])) for a in allow)
) or "（无）")

# 2. 每个切片的 git add 必须覆盖它自己的 Files:
slices = re.split(r"\n## 切片 ([A-C][^\n]*)\n", text)
unstaged = []
for name, body in zip(slices[1::2], slices[2::2]):
    files = {
        m.group(1)
        for line in body.splitlines()
        if (m := re.match(r"^- (?:Create|Modify|Test): `([^`]+)`", line.strip()))
    }
    adds: set[str] = set()
    for block in re.findall(r"```bash\n(.*?)```", body, re.S):
        if "git add" in block:
            segment = block[block.index("git add"):].split("git commit")[0]
            adds |= set(re.findall(r"[\w./\-]+\.(?:py|md)", segment))
    if adds and (missing := sorted({f for f in files if f.startswith(("src/", "tests/"))} - adds)):
        unstaged.append((name.split(".")[0], missing))
print("2) Files 声明了但 git add 漏掉：", unstaged or "（无）")

# 3. 计划里的 `路径:行号` 锚点必须指向真实存在、且非空的那一行
index: dict[str, list[pathlib.Path]] = {}
for base in ("src", "tests", "docs"):
    for f in pathlib.Path(base).rglob("*"):
        if f.is_file():
            index.setdefault(f.name, []).append(f)
for f in pathlib.Path(".").glob("*.md"):
    index.setdefault(f.name, []).append(f)
stale = []
for m in re.finditer(r"`([\w./-]+\.(?:py|md)):(\d+)`", text):
    ref, line = m.group(1), int(m.group(2))
    direct = pathlib.Path(ref)
    found = [direct] if "/" in ref and direct.exists() else index.get(ref.split("/")[-1], [])
    if len(found) != 1:
        stale.append(f"{ref}:{line} → " + ("找不到该文件" if not found else f"同名文件 {len(found)} 个"))
        continue
    lines = found[0].read_text(encoding="utf-8").splitlines()
    if line > len(lines):
        stale.append(f"{ref}:{line} → 该文件只有 {len(lines)} 行")
    elif not lines[line - 1].strip():
        stale.append(f"{ref}:{line} → 该行是空行")
total = len(re.findall(r"`[\w./-]+\.(?:py|md):\d+`", text))
print("3) 失效的 `路径:行号` 锚点：", stale or "（无）", f"[共 {total} 个]")
CHECK
```

三条都必须输出 `（无）`。

**这里只剩三条，是有意的。** V9 有七条，其中三条（把计划里的代码块拼回文件查未定义名、按真实
签名绑实参、按 AST 比对契约构造点）存在的唯一理由是计划里带着大量从未运行过的代码。这份计划
不再带那些代码，而在**真实代码**上，`ruff` 与 `mypy` 覆盖的正是同一批问题且覆盖得更严；另有
一条（按导入语句核对既有符号是否存在）在新计划里已经无事可查——代码块里不再有 import。

剩下三条查的是 `ruff` 和 `mypy` 管不了的东西：

- 第 1、2 条查**声明与声明之间**对不对得上（allowlist ↔ `Files:` ↔ `git add`）。
- 第 3 条查计划引用的 `路径:行号` 锚点是否还指向真实存在、非空的那一行。这份计划靠这些锚点
  把读者带到真实代码前（`guards.py:48` 的签名、`postgres.py:293` 的事务助手、`local_stack.py:1031`
  每次装配都调 `seed_if_absent`），锚点烂掉等于把读者带到错误的位置。它**只能**验证位置存在，
  验证不了那一行的内容还是不是计划说的东西——这一点如实记在残余风险里。
- 第四道防线不在脚本里：每个提交后的 `git status --porcelain`，它兜的是静态核对看不见的
  "`Files:` 与 `git add` 两边一起漏"。

---

## 切片 A. 契约与迁移

**目标：** 把 W1a 的词汇表和存储结构落定——枚举闭集、身份目录契约、审计契约、四张表与
`rev_0014`。合入后仓库四门全绿，运行时行为零变化（没有任何代码消费它们）。

**Files:**
- Modify: `src/xiaowei_agent/contracts/base.py`
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Create: `src/xiaowei_agent/contracts/identity.py`
- Create: `src/xiaowei_agent/contracts/admin_audit.py`
- Create: `src/xiaowei_agent/governance/product_roles.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_admin_audit.py`
- Modify: `src/xiaowei_agent/interfaces/local_admin_auth.py`
- Test: `tests/unit/test_product_roles.py`
- Test: `tests/contract/test_identity_contracts.py`
- Test: `tests/contract/test_admin_audit_contracts.py`
- Test: `tests/contract/test_identity_schema.py`
- Test: `tests/security/test_controlled_pii_exposure.py`
- Modify: `tests/contract/test_schema_matches_migration.py`
- Modify: `tests/unit/test_readiness.py`
- Modify: `tests/integration/test_migration_paths.py`

### A.1 接口形状

**枚举**（`contracts/enums.py`，全部 `StrEnum`）：

```python
ProductRole            = {ADMIN: "admin", OPERATOR: "operator", USER: "user"}
UserStatus             = {ACTIVE: "active", DISABLED: "disabled"}
AdminCapability        = 七成员，见 Global Constraints
AdminAuditAction       = {USER_CREATED, USER_STATUS_CHANGED, ROLE_ASSIGNED, ROLE_REVOKED,
                          EXTERNAL_IDENTITY_BOUND, EXTERNAL_IDENTITY_UNBOUND,
                          LOCAL_ADMIN_BOOTSTRAPPED, LEGACY_IDENTITY_MIGRATED}
AdminAuditTargetKind   = {USER, ACTIVATION, DUTY_BINDING, CONFIG, TASK_CONTENT}
AdminAuditOutcome      = {STARTED, SUCCEEDED, DENIED, FAILED}
AdminAuditReasonCode   = {ACTOR_NOT_ADMIN, AUTH_SOURCE_NOT_ALLOWED, TARGET_NOT_FOUND,
                          SCOPE_MISMATCH, CONFLICT, AUDIT_UNWRITABLE}
```

`ACTIVATION` / `DUTY_BINDING` / `CONFIG` / `TASK_CONTENT` 四个 target kind 与 `STARTED` / `DENIED`
两个 outcome 留在闭集里是给 W1b/W3/W4a 用的；W1a 没有产生它们的命令。**留闭集成员**与
**建无消费者的表**是两回事：前者是规格 §14.2 逐字列出的值域，后者才是被禁止的提前建设。

**能力映射**（`governance/product_roles.py`）：

```python
def admin_capabilities(*, role: ProductRole, source: IdentitySource) -> frozenset[AdminCapability]: ...
def channel_permissions(*, role: ProductRole) -> frozenset[ChannelPermission]: ...
```

纯函数，无存储依赖。`MANAGE_INTEGRATIONS` 与 `RUN_CONNECTION_TESTS` 只在
`source is IdentitySource.LOCAL_ADMIN` 时出现，即使 role 是 ADMIN。

**身份目录契约**（`contracts/identity.py`）。`ControlledPii = StrictStr`，凡这样标注的字段必须同时
写 `exclude=True, repr=False`；`_ID = Field(min_length=1, max_length=64)`，
`_NAME = (1, 128)`，`_ACTOR = (1, 256)`，`_PII = (1, 128, exclude, repr=False)`。
对应关系是固定的：`user_id` / `tenant_id` / `environment_id` / `created_by` 用 `_ID`，`actor` 用
`_ACTOR`，`display_name` 用 `_NAME`，`subject_ref` 用 `_PII`。`_ACTOR` 比 `_NAME` 宽，是因为 `actor`
是**身份**、不可截断，而它要接收的旧静态文档对 actor 没有任何长度上限
（`src/xiaowei_agent/interfaces/feishu_identity.py:40`）；256 这个数怎么落到旧数据上，见切片 C.1 的转换策略。

```python
class UserAccount(Contract):
    user_id: StrictStr; actor: StrictStr; display_name: StrictStr
    status: UserStatus; created_at: AwareDatetime; updated_at: AwareDatetime

class UserRoleAssignment(Contract):
    user_id: StrictStr; tenant_id: StrictStr; environment_id: StrictStr
    role: ProductRole; created_by: StrictStr
    created_at: AwareDatetime; updated_at: AwareDatetime

class ExternalIdentity(Contract):
    user_id: StrictStr; provider: Literal[IdentitySource.FEISHU]
    tenant_id: StrictStr; environment_id: StrictStr
    subject_ref: ControlledPii
    created_at: AwareDatetime; last_seen_at: AwareDatetime

class DirectoryPrincipalFacts(Contract):
    account: UserAccount; assignment: UserRoleAssignment
```

写命令闭集，全部带 `kind: Literal[...]` 判别器，**全部不带时间、不带 `created_by`、不带任何
审计字段**——它们由 store 派生：

| 命令 | 字段（`kind` 之外） |
| --- | --- |
| `CreateUserCommand` | `user_id, actor, display_name, tenant_id, environment_id, role` |
| `SetUserStatusCommand` | `user_id, tenant_id, environment_id, status` |
| `AssignRoleCommand` | `user_id, tenant_id, environment_id, role` |
| `RevokeRoleCommand` | `user_id, tenant_id, environment_id` |
| `BindExternalIdentityCommand` | `user_id, tenant_id, environment_id, subject_ref: ControlledPii` |
| `UnbindExternalIdentityCommand` | `user_id, tenant_id, environment_id` |
| `BootstrapLocalAdminCommand` | `user_id, actor, display_name, tenant_id, environment_id, password_hash: SecretHash` |
| `MigrateLegacyIdentitiesCommand` | `tenant_id, environment_id, entries: tuple[LegacyIdentityMigrationEntry, ...]`（1–10000 条） |

`LegacyIdentityMigrationEntry`（`user_id, actor, display_name, role, subject_ref: ControlledPii`）
也住在这一层。**它和切片 C 的 `LegacyIdentityEntry` 是两个类型，不要合并：** 前者是命令载荷，
字段已经是派生完的结果（角色已按 §6.6 映射、`user_id` 已摘要截断）；后者是解析结果，带的是
**原始 labels**。合并成一个，要么让契约层去认识 `dba`/`oncall` 这些外部标签，要么让 `persistence`
反向依赖 `interfaces`——`test_module_layering.py:47` 直接禁止后者。

`DirectoryCommand` 是这八个的判别联合。五个本地管理员常量
（`LOCAL_ADMIN_TENANT_ID / _ENVIRONMENT_ID / _ACTOR / _USER_ID / _DISPLAY_NAME`）也住在这个模块，
**因为它是唯一一个 `persistence` 与 `interfaces` 都能导入的层**（`test_module_layering.py:47`
禁止 `persistence → interfaces`，`:140` 允许 `interfaces/local_admin_auth.py → contracts`）。
前三个的值取自既有的 `LOCAL_ADMIN_PRINCIPAL`（`interfaces/local_admin_auth.py:127`），搬过来
之后 `local_admin_auth.py` 改为导入，**删掉**自己那三个字面量。

**审计契约**（`contracts/admin_audit.py`）：

```python
def admin_audit_target_digest(*, target_kind: AdminAuditTargetKind, target_ref: str) -> str: ...

class AdminOperationContext(Contract):        # 调用方唯一能提供的东西
    operation_id: StrictStr                   # ≤ 48，给批量子 id 留后缀空间
    actor_user_id: StrictStr; actor: StrictStr; auth_source: IdentitySource

class AdminAuditEffect(Contract):
    role: ProductRole | None = None; status: UserStatus | None = None

class AdminAuditCandidate(Contract):          # 只由 store 内部构造
    operation_id, tenant_id, environment_id, actor_user_id, actor: StrictStr
    auth_source: IdentitySource; action: AdminAuditAction
    target_kind: AdminAuditTargetKind; target_ref_digest: Sha256Hex
    outcome: AdminAuditOutcome
    reason_code: AdminAuditReasonCode | None = None
    effect: AdminAuditEffect = AdminAuditEffect()

class AdminAuditEvent(AdminAuditCandidate):
    event_id: StrictStr; created_at: AwareDatetime

class AdminAuditStart(Contract):    # 无 outcome / effect / reason_code
class AdminAuditTerminal(Contract): # 稳定字段从已存 STARTED 读回，只带 outcome/reason/effect
class AdminAuditDenial(Contract):   # 无 outcome / effect，reason_code 必填
```

三个常量名单，供契约校验与数据库 CHECK 共用同一份真源：`DIRECTORY_ACTIONS`（八个，等于 W1a
全部 action）、`ROLE_EFFECT_ACTIONS`（`USER_CREATED / ROLE_ASSIGNED / LOCAL_ADMIN_BOOTSTRAPPED /
LEGACY_IDENTITY_MIGRATED`）、`STATUS_EFFECT_ACTIONS`（`USER_CREATED / USER_STATUS_CHANGED /
LOCAL_ADMIN_BOOTSTRAPPED / LEGACY_IDENTITY_MIGRATED`）。`ROLE_REVOKED` 不在 role effect 里：撤销后
该作用域没有角色，记任何角色都是错的。

**表结构**（`persistence/schema.py`，四张新表 + `local_admins.user_id`）：

| 表 | 主键 | 其他列 | 约束 |
| --- | --- | --- | --- |
| `user_accounts` | `user_id` | `actor`(UNIQUE), `display_name`, `status`, `created_at`, `updated_at` | `ck_user_accounts_status_closed` |
| `user_role_assignments` | `(user_id, tenant_id, environment_id)` | `role`, `created_by`, `created_at`, `updated_at` | `role` 闭集 CHECK；`user_id` FK → `user_accounts` `ondelete=RESTRICT` |
| `external_identities` | `(provider, tenant_id, environment_id, subject_ref_digest)` | `user_id`, `created_at`, `last_seen_at` | `provider IN ('feishu')`；`uq_external_identities_one_subject_per_account(provider, tenant_id, environment_id, user_id)`；`user_id` FK `RESTRICT` |
| `admin_audit_events` | `event_id` | `operation_id`, `tenant_id`, `environment_id`, `actor_user_id`, `actor`, `auth_source`, `action`, `target_kind`, `target_ref_digest`, `outcome`, `reason_code`, `effect_role`, `effect_status`, `created_at` | 见下 |
| `local_admins` | 既有 | **新增** `user_id`（nullable） | `fk_local_admins_user_id` → `user_accounts` `ondelete=RESTRICT` |

`external_identities` **只存摘要**：`open_id` 落库就等于给它开了一条同时进入备份、慢查询日志和
运维截图的通道。主键保证一个主体最多绑一个账号，UNIQUE 保证一个账号在同作用域最多绑一个主体。

`admin_audit_events` 的约束（名字是接口的一部分，迁移与 `schema.py` 必须逐字一致）：

- 闭集 CHECK：`auth_source` / `action` / `target_kind` / `outcome` / `reason_code` / `effect_role` / `effect_status` 各一条。
- `ck_admin_audit_events_reason_code_matches_outcome`：`(outcome IN ('denied','failed')) = (reason_code IS NOT NULL)`。
- `ck_admin_audit_events_effect_only_on_success`：非 `succeeded` 不得带任何 effect。
- `ck_admin_audit_events_role_effect_required` / `..._status_effect_required`：成功且 action 属于对应名单时 effect 必须在，否则必须不在（`=` 而不是 `→`，两个方向都钉）。
- `ck_admin_audit_events_directory_actions_are_single_phase`：目录动作不得写 `STARTED`。
- 两条**偏唯一索引**：`uq_admin_audit_one_started_per_operation`（`WHERE outcome = 'started'`）、`uq_admin_audit_one_terminal_per_operation`（`WHERE outcome IN ('succeeded','denied','failed')`）。
- 一条普通索引 `ix_admin_audit_events_created_at`。

**`operation_id` 为什么不是全局 UNIQUE**：规格 §14.2 要求非事务性配置操作写
`STARTED → SUCCEEDED/FAILED` 两条事件，它们**属于同一个 operation**。全局唯一会让第二条终态
事件必然撞约束；换一个 operation id 又无法证明两条事件属于同一次操作。

**`rev_0014` 的形状**（`down_revision = "0013_clarification_parent"`）：
`upgrade()` 建四张表、两条偏唯一索引、一条普通索引，给 `local_admins` 加 `user_id` 与外键。
`downgrade()` 逐字按既有形状调用守卫——**先读 `persistence/migrations/guards.py` 确认签名，
参照 `rev_0013_clarification_parent.py:79`**：

```python
if not context.is_offline_mode():
    require_destructive_authorization(op.get_bind(), guarded=(...))
```

`guarded` 是 `((table, 类别名), ...)`，类别名就是 `MigrationSafetyError.counts` 里出现的字符串。
**不要**在它前面再写一遍"有没有行"的手工预检：`_row_counts` 已经在数同样的表，守卫自己就有
"没有受保护行就返回"的分支，而手工预检会把分类 `counts` 降级成一个布尔——`counts` 正是既有
用例断言的那个值。

### A.2 关键不变量

1. `AdminAuditCandidate` **没有任何自由文本字段**。审计表是唯一一处既要长期保留、又天然贴着
   secret、聊天正文和异常堆栈的存储；只要留一个 `str` 通道，第一个赶工的调用方就会把
   `str(exc)` 塞进去。
2. `reason_code` 与 outcome **双向绑定**：负面结果必须有原因码，非负面结果必须没有。
3. effect 与 action **双向绑定**，且只有 `SUCCEEDED` 能带 effect。没成功却声称 effect，会让审计
   读者以为权限已经给出去了。
4. `AdminAuditStart` 在**契约层**拒绝目录动作——不是只靠数据库 CHECK。内存实现没有数据库；
   规则放在任一实现里，另一个就会在同一个共享套件上给出不同的答案。
5. `admin_audit_target_digest` 把 `target_kind` 放进**摘要域**：两类 target 共用同一个 ref 字符串
   （用户 id 与任务 id 撞号完全可能）时，不隔离就会算出相等的摘要，两条不同性质的事件从此
   互相冒充。
6. 命令**不能自带时间或身份**：`created_at` / `created_by` / `event_id` 由 store 盖章。
7. 所有 ID 字段有界；`operation_id ≤ 48` 是为批量子 id 后缀（`{op}:{index}`）留的余量。
8. `schema.py` 的 `METADATA` **没有命名约定**（`schema.py:58`），因此 `primary_key=True` 声明出来的
   主键在 metadata 里 `name is None`。任何"按约束名去查"的结构用例都会永久变红——结构用例必须
   按**列集合**判定。

### A.3 测试意图

`tests/unit/test_product_roles.py` —— 能力映射是纯函数，也是最容易被"Admin 就是全权"这种直觉
写错的地方：

| 用例 | 钉住什么 |
| --- | --- |
| `test_channel_permission_enum_is_still_exactly_three_members` | 渠道权限没有被顺手扩展 |
| `test_admin_capability_enum_is_exactly_the_seven_adr_013_members` | 能力闭集逐字等于 ADR-013 |
| `test_product_role_and_user_status_are_closed` | 两个新枚举是闭集 |
| `test_channel_permissions_match_the_spec_table_exactly` | 角色 → 渠道权限逐行等于规格表 |
| `test_local_admin_administrator_gets_every_admin_capability` | 本地 Admin 拿满七项 |
| `test_feishu_administrator_is_short_exactly_the_two_config_plane_capabilities` | 飞书 Admin 恰好少 `MANAGE_INTEGRATIONS` 与 `RUN_CONNECTION_TESTS`，不多不少 |
| `test_non_administrators_get_no_admin_capability_from_any_source` | 非 Admin 在任何来源下都拿不到管理能力 |
| `test_mapping_is_total_over_the_role_enum` | 映射对角色枚举是全函数，新增角色会立刻变红 |

`tests/contract/test_identity_contracts.py`：

| 用例 | 钉住什么 |
| --- | --- |
| `test_user_account_fields_are_exactly_the_spec_set` | 字段集合精确等于规格 §6.1 |
| `test_user_account_is_frozen_and_rejects_unknown_fields` | frozen + `extra="forbid"` |
| `test_role_assignment_carries_the_scope_not_the_account` | 作用域在角色上，不在账号上 |
| `test_external_identity_never_leaks_the_subject_ref` | `repr` 与 `model_dump` 都不含 `open_id` |
| `test_external_identity_provider_is_restricted_to_feishu` | provider 是 `Literal`，不是开放枚举 |
| `test_commands_cannot_stamp_their_own_time_or_identity` | 命令没有时间/身份字段 |
| `test_no_command_can_carry_audit_fields` | 八个命令都没有 action/target/outcome/effect |
| `test_every_command_carries_the_scope_the_store_derives_audit_from` | 每个命令都带 store 派生审计所需的作用域 |
| `test_command_kind_discriminators_are_unique` | 判别器互不相同 |
| `test_bootstrap_command_never_exposes_the_password_hash` | 口令哈希 `exclude` + `repr=False` |
| `test_migration_command_carries_the_whole_batch_and_hides_open_ids` | 批量是一个命令，且不泄 `open_id` |
| `test_migration_command_rejects_an_empty_batch` | 空批次是错误，不是 no-op |
| `test_identifier_fields_are_bounded` | 三个上限的两侧都写死：65 字符 `user_id`、257 字符 `actor`、129 字符 `display_name` 被拒；**正对照**：64 / 256 / 128 全部被接受——只断"拒"不断"收"的界，把上限改成 1 也照样绿 |
| `test_bind_command_requires_a_non_empty_subject_ref` | 空 subject 不是合法绑定 |
| `test_the_local_admin_principal_consumes_the_contract_constants` | 五个常量只有一份，且 `local_admin_auth.py` 里不再有那几个字面量 |

`tests/contract/test_admin_audit_contracts.py`：

| 用例 | 钉住什么 |
| --- | --- |
| `test_operation_context_carries_no_audit_decision` | 上下文里没有 action/target/outcome |
| `test_operation_id_leaves_room_for_batch_suffixes` | `≤ 48` 与批量后缀相容 |
| `test_audit_candidate_fields_are_the_spec_set_plus_the_closed_effect` | 字段 = §14.2 十三项 + 两个 effect 列 |
| `test_effect_has_only_two_closed_enum_slots` | effect 不是 JSON，不能塞正文 |
| `test_stored_event_adds_only_identity_and_time` | 事件比候选只多 `event_id` 与 `created_at` |
| `test_target_kind_is_the_spec_five_member_closed_set` / `test_outcome_is_the_spec_four_member_closed_set` | 两个闭集逐值等于规格 |
| `test_w1a_actions_cover_only_what_this_stage_actually_writes` | action 闭集不含本阶段写不出的动作 |
| `test_authentication_lifecycle_never_became_an_audit_action` | 登录/改密/登出没有混进来 |
| `test_target_digest_is_domain_separated_across_kinds` | 同 ref 不同 kind 摘要不等 |
| `test_target_digest_never_contains_the_plaintext_ref` / `test_target_ref_digest_field_rejects_a_plaintext_reference` | 明文进不了摘要列 |
| `test_reason_code_is_absent_on_success_and_required_on_denial` | 不变量 2 |
| `test_a_negative_outcome_cannot_claim_an_effect` / `test_started_outcome_cannot_claim_an_effect_either` | 不变量 3 |
| `test_role_changing_actions_require_a_role_effect` / `test_status_changing_actions_require_a_status_effect` / `test_binding_actions_carry_no_effect` | effect 与 action 双向绑定 |
| `test_two_phase_start_refuses_a_directory_action` / `test_every_w1a_action_is_a_directory_action` | 不变量 4 |
| `test_neither_start_nor_denial_can_state_an_outcome_or_effect` | 三个窄写类型在结构上写不出成功 |
| `test_candidate_cannot_stamp_event_id_or_time` | 不变量 6 |

`tests/contract/test_identity_schema.py`（离线，不需要数据库）：

| 用例 | 钉住什么 |
| --- | --- |
| `test_new_tables_are_registered_in_all_tables` | 四张表进了 `ALL_TABLES`，否则 `clean_database` 清不到它们 |
| `test_actor_is_unique_so_two_accounts_cannot_claim_the_same_identity` | `actor` 全局唯一 |
| `test_role_assignment_is_keyed_by_user_and_scope` | 角色主键是三元组 |
| `test_one_subject_ref_binds_to_at_most_one_account_per_scope` | 主键 + UNIQUE 两个方向都在 |
| `test_external_identity_stores_a_digest_not_the_open_id` | 列名与长度表明它存的是摘要 |
| `test_assignment_and_identity_reference_the_account_by_foreign_key` | 两条 FK 都是 `RESTRICT` |
| `test_local_credential_is_linked_to_a_directory_account` | `local_admins.user_id` 与它的 FK |
| `test_operation_id_is_not_globally_unique` | 没有全局 UNIQUE |
| `test_one_started_and_one_terminal_event_per_operation` | 两条偏唯一索引的谓词正确 |
| `test_the_unique_helper_tells_partial_indexes_apart_from_global_ones` | **测试自己的 helper**：别把偏唯一误算成全局唯一 |
| `test_audit_closed_sets_are_expressed_as_database_checks` | 闭集在数据库层也成立，不只在契约层 |
| `test_the_single_phase_check_lists_exactly_the_directory_actions` | CHECK 的 SQL 字面量逐值等于 `DIRECTORY_ACTIONS` |
| `test_audit_table_has_no_free_text_column` | 不变量 1 在表层面也成立 |
| `test_effect_columns_are_closed_enums_not_json` | effect 是两列枚举，不是 JSON |
| `test_user_status_and_role_are_checked_at_the_database_level` | 枚举值在数据库也闭 |

`tests/security/test_controlled_pii_exposure.py`：`test_every_pii_field_is_excluded_and_unrepresented`
（凡标 `ControlledPii` 的字段必须同时 `exclude=True` 与 `repr=False`，按注解发现而不是按名字
枚举）、`test_pii_never_reaches_repr_or_dump`。

既有的 `tests/security/test_secret_field_exposure.py` 按 AST 找**注解名以 `Secret` 开头**的字段，
所以 `BootstrapLocalAdminCommand.password_hash: SecretHash` 会被它自动收进参数化里，必须同时
写 `exclude=True, repr=False`。把 `SecretHash` 别名从 `persistence/local_admin.py` 搬到
`contracts/base.py` 不影响这条发现——它看的是字段处的注解名，不是别名定义在哪。

迁移锚点（既有文件）：新增一条 revision 会让两条既有断言转红，它们**不是**被新功能破坏的，
而是本来就按"当前最新 revision"写的锚点：`test_latest_declared_revision_is_the_alembic_head`
（声明的是 `0013`）与 `test_readiness_requires_database_head_and_assembly`（写死了
`"0013_clarification_parent"`）。先跑一次确认它们确实红，再改；另加
`test_rev_0014_has_the_expected_revision_chain` 与发布哈希登记。

真实 PostgreSQL 的降级守卫（追加到 `tests/integration/test_migration_paths.py`）：

| 用例 | 钉住什么 |
| --- | --- |
| `test_rev_0014_downgrade_requires_authorization_for_identity_and_audit` | 有数据时降级被拒、`counts` 分类精确、版本不动、数据一行不少；显式授权后真的删；再升回来且 `local_admins.user_id` 回来 |
| `test_rev_0014_downgrade_needs_no_authorization_on_an_empty_directory` | 正常对照：没有受保护数据时不该要授权 |

**这两条都必须 `try/finally` 升回 `head`**，见 Global Constraints 那条"主动降级的用例必须把
schema 还回去"。离线用例一条都抓不到降级守卫的缺陷，**因为离线用例根本不执行 `downgrade()`**；
而这两条一旦漏了还原，它们自己会变成后续所有集成用例的污染源。

### A.4 验收要求

- 四门全绿；与开工前记录的基线数字对比，说明新增用例数。
- `test_schema_matches_migration.py` 的 DDL 一致性通过。若它报 partial index 的 `WHERE` 对不上，
  说明 `schema.py` 与迁移写的不是同一套 DDL——**改到两边一致，不要改断言**。
- `tests/integration/test_migration_paths.py` **整文件**跑过，不是只跑新增的两条。
- 反证：把 `ck_admin_audit_events_effect_only_on_success` 从迁移和 `schema.py` 里同时删掉，
  `test_a_negative_outcome_cannot_claim_an_effect` 之外的**数据库层**用例必须变红；还原后全绿。
  变异要写清它恢复的是哪个旧缺陷。
- 提交后 `git status --porcelain` 为空。

```bash
git add src/xiaowei_agent/contracts/base.py \
        src/xiaowei_agent/contracts/enums.py \
        src/xiaowei_agent/contracts/__init__.py \
        src/xiaowei_agent/contracts/identity.py \
        src/xiaowei_agent/contracts/admin_audit.py \
        src/xiaowei_agent/governance/product_roles.py \
        src/xiaowei_agent/persistence/schema.py \
        src/xiaowei_agent/persistence/migrations/versions/rev_0014_identity_admin_audit.py \
        src/xiaowei_agent/interfaces/local_admin_auth.py \
        tests/unit/test_product_roles.py \
        tests/unit/test_readiness.py \
        tests/contract/test_identity_contracts.py \
        tests/contract/test_admin_audit_contracts.py \
        tests/contract/test_identity_schema.py \
        tests/contract/test_schema_matches_migration.py \
        tests/security/test_controlled_pii_exposure.py \
        tests/integration/test_migration_paths.py
git commit -m "feat(w1a): add the identity directory and admin audit contracts and tables

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
```

`interfaces/local_admin_auth.py` 在这条 `git add` 里，因为五个常量下沉到契约层的**同时**必须
删掉它那三个字面量——留着就是两份真源，而两份真源迟早会在某次改租户名时只改一边。

---

## 切片 B. Store 与审计原子性

**目标：** 两个实现（内存 + PostgreSQL）、一套共享行为套件、三组机械守卫。合入后"每一次
授权改变都在同一个事务里带上一条由命令派生的审计"这条不变量已经成立，并且被守卫钉住。

**这是整个 W1a 的承重切片。**

**Files:**
- Create: `src/xiaowei_agent/persistence/identity.py`
- Create: `src/xiaowei_agent/persistence/admin_audit.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`
- Modify: `src/xiaowei_agent/persistence/local_admin.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `tests/conftest.py`
- Modify: `tests/integration/conftest.py`
- Create: `tests/suites/identity_directory.py`
- Create: `tests/contract/test_identity_store.py`
- Create: `tests/integration/test_identity_directory_postgres.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/contract/test_identity_schema.py`
- Create: `tests/security/test_admin_audit_append_only.py`
- Create: `tests/security/test_identity_write_path.py`

### B.1 接口形状

```python
class UserDirectoryStore(Protocol):
    async def load_account(
        self, *, user_id: str, tenant_id: str, environment_id: str
    ) -> DirectoryPrincipalFacts | None: ...
    async def resolve_by_subject(
        self, *, provider: IdentitySource, tenant_id: str, environment_id: str,
        subject_ref: str,
    ) -> DirectoryPrincipalFacts | None: ...
    async def apply(
        self, *, command: DirectoryCommand, context: AdminOperationContext
    ) -> tuple[AdminAuditEvent, ...]: ...

class AdminAuditStore(Protocol):
    async def append_started(self, *, start: AdminAuditStart) -> AdminAuditEvent: ...
    async def append_terminal(self, *, terminal: AdminAuditTerminal) -> AdminAuditEvent: ...
    async def append_denied(self, *, denial: AdminAuditDenial) -> AdminAuditEvent: ...
    async def load(self, *, event_id: str) -> AdminAuditEvent | None: ...
```

`apply()` 返回**元组**：批量迁移命令一次产生 N 条事件，单命令返回 1 元组，幂等 no-op 返回空
元组。调用方用同一个形状处理全部情况——再开一个"批量专用"方法就等于再开一条写路径。
`resolve_by_subject()` 拿的是**明文** `subject_ref`，摘要化在 store 内部完成；调用方不自己算
摘要，算错了就会永远判成"未绑定"。

派生层（`persistence/identity.py`，模块级）：

```python
BOOTSTRAP_ACTOR_USER_ID: Final[str] = "system-bootstrap"
ACTION_FOR_COMMAND: Final[dict[type, AdminAuditAction]]   # 八个命令类型 → 它必然记录的动作
EFFECT_FOR_COMMAND: Final[dict[type, Callable[..., AdminAuditEffect]]]
def external_subject_digest(*, provider, tenant_id, environment_id, subject_ref) -> str: ...
def batch_operation_id(context: AdminOperationContext, index: int) -> str: ...
def derive_audit(command, context, *, target_ref) -> AdminAuditCandidate: ...

class UserDirectoryError(RuntimeError): ...
class UserDirectoryConflictError(UserDirectoryError): ...
class UserDirectoryNotFoundError(UserDirectoryError): ...
class AdminAuditUnwritableError(UserDirectoryError): ...
```

审计侧的错误族住在 `persistence/admin_audit.py`，与目录侧**分开**（矩阵一的归属规则）：

```python
class AdminAuditError(RuntimeError): ...
class AdminAuditConflictError(AdminAuditError): ...
class AdminAuditMissingStartError(AdminAuditError): ...

def audit_stage_key(candidate: AdminAuditCandidate) -> tuple[str, str]: ...
```

**`append_started` 在 W1a 没有任何可用的 action，这是设计结果，不是缺陷。** `DIRECTORY_ACTIONS`
恰好等于 W1a 的全部八个 action，而 `AdminAuditStart` 在契约层拒绝目录动作（`ADR-013:129` 把两阶段
留给非事务性配置面动作，那些 action 属于 W4a）。因此 W1a 能真正调用的写方法只有 `append_denied`，
W1b 的激活拒绝路径会直接复用它。**不要**为了让某条用例跑通而放宽那个校验——放宽它等于允许
目录动作走两阶段，而两阶段的终态字段是调用方再传一遍的，伪造就此回来。据此：
`test_a_second_event_at_the_same_stage_is_rejected` 用**两次 `append_denied`**（撞同一条终态偏唯一
索引，且不需要先有 `STARTED` 这个第二前置条件）；`test_a_terminal_event_without_a_started_event_is_refused`
在查不到 `STARTED` 时就已经失败，到不了 action 校验。

`audit_stage_key` 返回 `(operation_id, "started" | "terminal")`，是内存实现表达那两条偏唯一索引
的方式——**两个实现共用同一个"什么算阶段重复"的定义**，各写一份就会在共享套件上给出不同答案。
四个错误类都是 `RuntimeError` 子类，因此 `classify_persistence_exception` 对它们返回 `None`，
能原样穿过 `_write_transaction` 与 `_persistence_boundary` 两层（矩阵三）。

`BOOTSTRAP_ACTOR_USER_ID` 是保留操作者，不是"不写审计的例外"：bootstrap 发生时还没有任何
Admin 账号，操作者不可能是某个人；用例外会让"每一次授权改变都有审计"这句话需要附加条件，
而附加条件是审计体系瓦解的起点。

`external_subject_digest` 与 `admin_audit_target_digest` **不同域**：同域会让"某个 open_id 的绑定
记录"和"针对该用户的审计事件"算出相等的摘要，于是拿到一张表就能反查另一张表的关联——两张表
各自脱敏，合起来却不脱敏。

`ACTION_FOR_COMMAND` 把命令类型绑死到动作，不让调用方传：调用方能选就能选错，一边撤销角色、
一边记一条 `user_created`，审计从此只是一段与事实无关的文本。

PostgreSQL 侧的模块级落库口：

```python
async def _insert_audit_event(
    connection: AsyncConnection, candidate: AdminAuditCandidate, *, now: datetime
) -> AdminAuditEvent | None: ...   # 同阶段事实已存在时返回 None
```

`PostgresAdminAuditStore._write` 拿到 `None` 抛 `AdminAuditConflictError`；
`PostgresUserDirectoryStore._apply_in_transaction` 拿到 `None` 抛 `AdminAuditUnwritableError`。
这是矩阵一那条归属规则的落点：同一个事实，两个公开入口，两个错误族。

`persistence/rows.py` 按既有 `row_to_X` / `X_to_row` 约定加一对
`admin_audit_event_to_row` / `row_to_admin_audit_event`，枚举**显式构造**（`Contract` 是 strict 模式，
不接受 `str` 当 `StrEnum`），行里是平铺的 `effect_role` / `effect_status` 两列而不是嵌套对象。

`InMemoryPersistenceState`（`persistence/memory.py`）加五个字段：`user_accounts`、
`user_role_assignments`、`external_identities`、`admin_audit_events`、`admin_audit_stage_keys`。
`external_identities` 的键必须是 `(provider, tenant_id, environment_id, subject_ref_digest)`，与表
主键**逐列相同**——键形状不一致，共享套件就会在两个实现上测出不同的冲突语义。

`LocalAdminRecord` 加 `user_id: StrictStr | None = None`。**默认 `None` 不惊动既有调用方，但也让
每一个漏改的构造点安静地返回 `None`**，而 `None` 在下面那张 bootstrap 四态表里正好是第二行
（backfill）——漏回填会让"数据库里已经链接好了"看起来像"还没升级"。它在 `src/` 有四个构造点，
四个都要处理：

| 构造点 | 本切片要做的事 |
| --- | --- |
| `persistence/fake.py::InMemoryLocalAdminStore.seed_if_absent` | 改为委托 `apply()`，不再自己构造 |
| `persistence/fake.py::InMemoryLocalAdminStore.change_password_and_rotate_session` | 带上原记录的 `user_id` |
| `persistence/local_admin.py::PostgresLocalAdminStore.get` | 从 `row` 回填（`SELECT` 本来就是整表，新列自动在 `row` 里——正因为"自动在里面"，漏掉不会有任何报错） |
| `persistence/local_admin.py::PostgresLocalAdminStore.change_password_and_rotate_session` | 从 `UPDATE ... RETURNING` 回填。**不能凭命令现搭**：`ChangePasswordCommand` 里根本没有这个字段 |

`_SINGLETON_ID`（`local_admin.py:25`）改成公开的 `LOCAL_ADMIN_SINGLETON_ID` 并导出，测试不重写
一遍字面量；`get()` 与改密两处的 `where` 跟着改，不留一个私有名一个公开名。

### B.2 关键不变量

1. **`apply()` 是四类授权事实的唯一写入口**（矩阵二）。`apply()` 本身不发 SQL——它开事务并
   委托给一个私有助手，所以守卫名单里是助手。
2. **审计与授权同事务。** PostgreSQL 靠一个 `_write_transaction`；内存靠"进临界区先拍快照，
   **任何**异常先恢复再决定抛什么"。内存实现**不能挑异常类型**恢复：只在两三种异常上恢复，
   审计派生、契约校验或 helper 抛出的别的异常就会留下"授权已改、审计没写"的半状态，两个实现
   在共享套件**之外**分叉，而分叉的那一格恰好是本计划最核心的不变量。
3. **快照必须覆盖 `state.local_admin`**，不只是四张目录表。bootstrap 在同一次调用里既写凭据又
   写目录；审计失败时只回滚目录不回滚凭据，会留下一行 `user_id IS NULL` 的凭据——而那正好落进
   四态表第二行（backfill）而不是第四行（fail-closed），于是一次失败的 bootstrap 看起来像一次
   "还没升级"的正常状态。
4. **bootstrap 的幂等判定读的是目录链接是否完整，不是凭据行是否存在：**

   | `local_admins` 行 | `user_id` | 目录侧 | 处置 |
   | --- | --- | --- | --- |
   | 无 | — | — | 建凭据 + 账号 + ADMIN 角色 + 链接 + 一条审计 |
   | 有 | `NULL` | — | **保留原 `password_hash` 不动**，补账号 + ADMIN 角色 + 链接 + 一条审计 |
   | 有 | 指向存在且在该作用域持 ADMIN 角色的账号 | 完整 | 幂等 no-op，返回 `()`，不写审计 |
   | 有 | 指向不存在的账号，或该账号没有 ADMIN 角色 | 破损 | `UserDirectoryConflictError`（fail-closed） |

   第二行是承重的：`local_stack.py:1031` 在**每次**装配 Web 时都调用 `seed_if_absent`，任何跑过
   RI5 的数据库早就有这一行。按"已有行即 no-op"实现，升级到 `rev_0014` 之后 `user_id` 永远是
   `NULL`、`user_accounts` 不会出现、ADMIN 角色不会出现、bootstrap 审计不会出现——而全新安装的
   测试全绿，因为全新安装走的是第一行。这是一条只在真实升级路径上才踩得到的裂缝。
   保留原 `password_hash` 同样是硬要求：管理员可能早就改过密码，backfill 拿传进来的初始口令
   覆盖回去，等于一次静默的凭据回滚。命令里的 `password_hash` **只在第一行**被使用。
5. **PostgreSQL 的 bootstrap 分流必须先 `SELECT ... FOR UPDATE`。** 两个 Web 进程同时装配会同时
   调用 `seed_if_absent`，没有行锁时两边都读到 `user_id IS NULL`、都去建账号，第二个撞
   `user_accounts` 主键——一次正常的并发启动变成一次启动失败。
6. **两个实现的公开表面精确等于 Protocol。** `isinstance` 式的 Protocol 检查只保证实现**不少于**
   协议，方向正好相反：它对"多出来一个公开写方法"完全无感。所以要一条 `==` 的表面断言。
7. **`seed_if_absent` 的签名与返回语义不变**（9 处调用方，其中 6 处在既有 RI5 测试里）。那 6 处
   必须全部保持绿；有用例变红说明语义被改了，回头修实现，**不要**改那些测试。
8. 新 PostgreSQL 代码必须复用同文件既有机制：公开方法带 `@_persistence_boundary`，写路径走
   `_write_transaction`，行与契约互转在 `rows.py`，已知冲突用 `ON CONFLICT DO NOTHING ... RETURNING`
   探测。直接 `engine.begin()`、`model_validate(dict(row))`、`except sa.exc.IntegrityError`、
   读 `exc.orig` 或约束名，都是错的（矩阵三）。

### B.3 机械守卫

三组守卫都**按属性判定，不按写法枚举**——按名字冻结的名单，下一个新写法自动逃出去。
允许项一律 owner-qualified 到 `相对路径::类.方法`，**不跳过整模块**。

| 守卫 | 判定方式 | 允许项 |
| --- | --- | --- |
| 授权表写入 | AST 找 `sa.insert/update/delete` 的目标表名 | `_AUTHZ_WRITE_SITES`（两个私有助手） |
| 审计表写入 | 同上 | `_AUDIT_WRITE_SITES`，**单元素** `postgres.py::_insert_audit_event` |
| 完整候选 helper | AST 按**签名**发现能收下 `AdminAuditCandidate` 的函数，与声明名单 `==` | `_FULL_CANDIDATE_HELPERS`（三个） |
| helper 调用点 | 上述 helper 的全部调用位置 | `_HELPER_CALL_SITES`（八个） |
| `local_admins.user_id` | AST 提取 `values()` **实际写入的列** | `_CREDENTIAL_LINK_SITES`（与授权表同一份） |
| append-only | `persistence/` 里对 `admin_audit_events` 的 `update` / `delete` 一律禁止 | 无 |

`_FULL_CANDIDATE_HELPERS` 用 `==` 而不是 `<=`：新增一个就必须显式加进来，加的时候必须回答
"它的调用点是谁"。`admin_audit_event_to_row` **不在**这里——它收的是 `AdminAuditEvent`（一条已带
`event_id` 与 `created_at` 的事实，不是候选）；它确实能把任意事件摊成列，但真正落库要经过
`sa.insert`，那已由 `_AUDIT_WRITE_SITES` 冻结，两处都列一遍就是重复校验。

`local_admins.user_id` 的守卫必须认出**四种等价写法**：`values(user_id=v)`、
`values({T.c.user_id: v})`、`values({"user_id": v})`、`values({T.c["user_id"]: v})`，并覆盖
`sa.delete(LOCAL_ADMINS)`；同时**只改 `password_hash` 的正常路径不得误报**。

**静态判不出写哪一列的形式**（变量键、`**` 展开、`Any` 注解、别名导入、`sa.text` 拼的 SQL）
**报警而不是放行**，并且在计划与代码注释里都如实写成"残余风险"，**不得**写成"完全封死"。

守卫文件不 import 任何 W1a 运行期符号（它读的是 AST），因此写任何实现之前就能整体跑一次：
预期只有"发现集为空"那一条红，**别的**用例变红就是守卫文件自身的问题。

### B.4 测试意图

共享行为套件 `tests/suites/identity_directory.py` 被内存与 PostgreSQL 两个绑定各跑一遍。
集成侧必须用**同名 fixture** 覆盖根 conftest 的内存实现（`store` / `plan_store` /
`clarification_record_store` 都是这样）——只写 `bind(...)` 而不定义同名 fixture，集成文件会安静地
继续跑内存实现，"PostgreSQL 同事务证据"就是假绿。集成 conftest 里**没有** `engine` fixture，
可用的是 `migrated_engine`（session 级）与 `clean_database`（函数级）。套件需要一个
`LocalAdminProbe`（`seed_legacy_credential` / `linked_user_id` / `stored_password_hash`），两个绑定
各给一份实现。

| 用例 | 钉住什么 |
| --- | --- |
| `test_creating_a_user_persists_the_account_and_a_derived_audit_event` | 账号与审计一起出现 |
| `test_the_audit_event_points_at_the_user_the_command_changed` | target 摘要指向命令改的那个人 |
| `test_role_assignment_records_which_role_was_granted` | 审计能回答"改成了什么" |
| `test_status_change_records_the_new_status` | 同上，状态方向 |
| `test_revoking_a_role_claims_no_effect` | 撤销不声称任何角色 |
| `test_audit_write_failure_rolls_back_the_authorization_change` | **承重**：审计写不进去，授权改动一点都不留 |
| `test_any_failure_after_a_directory_write_leaves_nothing_behind` | **承重**：注入一个与领域无关的异常（如 `RuntimeError`），四张表 + 凭据链接 + 审计全部回到调用前 |
| `test_two_accounts_cannot_claim_the_same_actor` | `actor` 全局唯一 |
| `test_commands_against_a_missing_account_are_not_found` | `UserDirectoryNotFoundError` |
| `test_one_subject_ref_cannot_bind_two_accounts` | 主体绑定唯一 |
| `test_disabled_account_stops_resolving_on_the_next_request` | 停用即刻生效 |
| `test_a_binding_in_one_scope_does_not_resolve_in_another` | 作用域隔离 |
| `test_unknown_subject_resolves_to_nothing` | 未知主体不报错、不返回 |
| `test_bootstrap_creates_account_role_and_credential_together` | 四态表第一行 |
| `test_bootstrap_is_idempotent_and_writes_no_audit_when_nothing_changed` | 第三行，且**不写审计** |
| `test_bootstrap_backfills_a_credential_that_has_no_directory_account` | 第二行；同时断言 `password_hash` 没被覆盖 |
| `test_bootstrap_backfill_is_itself_idempotent` | 第二行跑两次仍然只有一条审计 |
| `test_bootstrap_fails_closed_when_the_link_points_at_nothing` / `..._when_the_linked_account_lost_admin` | 第四行两种破损 |
| `test_legacy_migration_writes_the_whole_batch_or_nothing` | 批量原子性 |
| `test_legacy_migration_emits_one_audit_event_per_entry` | 每条一个审计，子 operation id 有界 |
| `test_a_second_event_at_the_same_stage_is_rejected` | 直接调 `AdminAuditStore` 时是 `AdminAuditConflictError`（矩阵一的另一格） |
| `test_a_denied_event_records_the_refusal_without_claiming_an_effect` | 拒绝有原因码、无 effect |
| `test_a_terminal_event_without_a_started_event_is_refused` | `AdminAuditMissingStartError` |

结构断言（`tests/contract/test_identity_store.py`，不需要数据库）：
`test_directory_store_exposes_exactly_one_write_method`、
`test_apply_gives_the_caller_no_way_to_steer_the_audit`、
`test_admin_audit_store_is_append_only_by_shape`、
`test_every_command_has_a_declared_action_and_effect`。

PostgreSQL 专有（`tests/integration/test_identity_directory_postgres.py`）：

| 用例 | 钉住什么 |
| --- | --- |
| `test_the_bound_fixtures_are_the_postgres_implementations` | 防止整份"同事务证据"假绿 |
| `test_rolled_back_change_leaves_no_row_in_either_table` | 真库上的同事务回滚 |
| `test_an_unknown_constraint_is_not_dressed_up_as_a_business_conflict` | CHECK/FK 不被伪装成业务冲突 |
| `test_seeding_the_local_admin_writes_its_audit_event` | `seed_if_absent` 真的走了 `apply()` |
| `test_an_already_seeded_database_gets_its_directory_backfilled_on_upgrade` | 四态表第二行的真库版：降级—插入旧行—升级—装配 |
| `test_changing_the_password_keeps_the_directory_link` | 数据库列、改密**返回值**、下一次 `get()` 三处都带着链接。只断言数据库列时，`get()` 与返回值可以双双返回 `None` 而全绿 |

离线冲突目标钉死（追加到 `tests/contract/test_identity_schema.py`）：
`test_the_conflict_probed_tables_have_exactly_the_declared_targets` 按**列集合**（主键 ∪ 唯一约束 ∪
偏唯一索引）逐格 `==`，外加 `test_the_conflict_target_helper_sees_both_kinds_of_unique_index`
自证 helper 两种唯一索引都认得。行映射与边界：
`test_domain_errors_pass_through_the_persistence_boundary`、
`test_an_audit_event_survives_a_round_trip_through_its_row`、
`test_the_row_uses_plain_strings_so_the_database_sees_its_own_types`。
Protocol 一致性：`test_the_implementation_surface_equals_the_protocol_surface`（`==`，覆盖两个实现）。

守卫用例（`tests/security/`）。每条守卫都配一条**反证**：

| 用例 | 钉住什么 |
| --- | --- |
| `test_no_persistence_module_updates_or_deletes_admin_audit_events` | append-only |
| `test_the_guard_actually_detects_a_mutating_call` | 上一条的反证：塞一个 `sa.update(ADMIN_AUDIT_EVENTS)` 必须被抓 |
| `test_audit_store_offers_no_cleanup_or_retention_api` | 没有清理接口 |
| `test_authorization_tables_are_written_only_at_frozen_sites` / `test_audit_table_is_written_only_at_frozen_sites` | 矩阵二 |
| `test_the_full_candidate_helpers_are_exactly_the_declared_ones` | 签名发现集 `==` 声明名单 |
| `test_the_helper_names_really_are_function_names` | 名单项的解析本身正确（模块级路径里有点号，`rsplit(".")` 会得到 `py::xxx`） |
| `test_the_full_candidate_helpers_have_only_declared_call_sites` | 调用点冻结 |
| `test_the_credential_link_is_written_only_at_frozen_sites` | `local_admins.user_id` |
| `test_the_frozen_sites_are_still_allowed` | **正常对照**：合法站点确实被判为合法。写这条时要小心别和上面几条互斥——V8 的版本先断言两个目录站点 ∈ `_AUDIT_WRITE_SITES`，又断言该集合只含 `_insert_audit_event`，两条不可能同时成立 |
| `test_the_guard_catches_a_module_level_write_function` / `..._a_new_private_write_method` / `test_the_helper_discovery_finds_a_newly_added_candidate_taker` / `test_an_unauthorised_call_site_of_the_write_sink_is_caught` | 四条反证，各自制造一种绕过 |
| `test_a_declared_call_site_of_the_write_sink_is_allowed` | 反证的正常对照 |
| `test_every_equivalent_credential_link_write_is_caught` | 四种等价写法全部命中 |
| `test_an_unprovable_credential_write_is_reported_not_waved_through` | 静态判不出的形式报警而不是放行 |
| `test_credential_writes_that_are_not_the_link_column_are_not_flagged` | 只改 `password_hash` 不误报 |
| `test_the_credential_guard_catches_a_wrong_owner_inside_an_allowed_module` | owner 粒度而不是模块粒度 |

### B.5 验收要求

- 四门全绿；`seed_if_absent` 的 6 处既有 RI5 测试调用方全部保持绿。
- `test_an_already_seeded_database_gets_its_directory_backfilled_on_upgrade` 主动降级制造旧数据，
  因此必须 `try/finally` 升回 `head` 并断言恢复；`tests/integration/` **整目录**跑一遍验证它没有
  污染后续用例的 schema。
- 共享套件在**两个绑定**上都跑过，且集成侧确实绑到了 PostgreSQL 实现（有显式类型断言）。
- 守卫文件能整体跑，且反证用例都调用**守卫本身**，不是复制一份判定逻辑。
- 反证（每条都要写清它恢复的是哪个旧缺陷，并确认目标用例红在**预期的那条断言**上）：
  1. 删掉内存实现的快照恢复 → 审计失败回滚与批量原子性两条同时变红。
  2. 让 `_role_effect` 恒返回空 effect → "记录授予了哪个角色"变红（契约校验先拒绝它）。
  3. 把 PostgreSQL 的 `_write_transaction` 换成两个独立连接/自动提交 → 同事务回滚用例变红。
  4. 让 `_insert_audit_event` 无条件返回事件（忽略 `ON CONFLICT` 吞掉冲突后的空结果）→ 同一条
     用例变红，但红在**另一条断言**上（根本没有异常抛出）。第 3、4 条红在不同断言，说明那条
     用例同时钉着两件事。
- 全部变异还原后必须回到全绿，提交前确认。
- 提交后 `git status --porcelain` 为空。

```bash
git add src/xiaowei_agent/persistence/identity.py \
        src/xiaowei_agent/persistence/admin_audit.py \
        src/xiaowei_agent/persistence/memory.py \
        src/xiaowei_agent/persistence/postgres.py \
        src/xiaowei_agent/persistence/rows.py \
        src/xiaowei_agent/persistence/local_admin.py \
        src/xiaowei_agent/persistence/fake.py \
        src/xiaowei_agent/_conformance.py \
        tests/conftest.py \
        tests/integration/conftest.py \
        tests/suites/identity_directory.py \
        tests/contract/test_identity_store.py \
        tests/contract/test_identity_schema.py \
        tests/contract/test_protocol_conformance.py \
        tests/integration/test_identity_directory_postgres.py \
        tests/security/test_admin_audit_append_only.py \
        tests/security/test_identity_write_path.py
git commit -m "feat(w1a): commit identity changes and their audit in one transaction

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
```

---

## 切片 C. 旧身份迁移

**目标：** 把旧静态 JSON 里的身份标签一次性、原子地迁进数据库目录，并把当前真源文档改成
"W1a 写内核已实现"。这是唯一对**既有部署数据**产生影响的一步。

**改哪几份文档不是自选题。** `tests/contract/test_doc_fact_binding.py:987` 的 `_CURRENT_TRUTH_DOCS`
把 `ARCHITECTURE.md`、`DEVELOPMENT_PLAN.md`、`AGENT_HANDOFF.md`、`README.md` 四份同时列为当前事实
文档。而 `README.md:16` 到 `README.md:19` 现在逐字写着本阶段是 W1a 计划送审、没有 `UserAccount`、`AdminAuditStore`、`rev_0014`
或任何源码，计划获批后才可开始实现。三个切片全部完成、所有用例全绿之后，这句话就是假的，
而四门依旧全绿——这正是验收假绿。所以 `README.md` 必须进本切片的 Files、allowlist 和 `git add`。

**反过来，`DEVELOPMENT_PLAN.md` 本切片不改。** `AGENTS.md:155` 规定它只写里程碑顺序、决策门、
交付物、退出标准，**不写里程碑进度和验证证据**，那些只放 `AGENT_HANDOFF.md`。W1a 没有改变任何
交付物或退出标准，所以它一行不动。上一版把它列进 `git add` 而把 `README.md` 漏掉，两头都错。

**Files:**
- Modify: `src/xiaowei_agent/interfaces/feishu_identity.py`
- Create: `src/xiaowei_agent/interfaces/legacy_identity_migration.py`
- Test: `tests/contract/test_legacy_identity_document.py`
- Test: `tests/contract/test_legacy_identity_migration.py`
- Modify: `ARCHITECTURE.md`
- Modify: `AGENT_HANDOFF.md`
- Modify: `README.md`
- Modify: `tests/contract/test_doc_fact_binding.py`

### C.1 接口形状

**为什么要动 `feishu_identity.py`：** 当前唯一的公开读取入口 `load_feishu_identity_directory()`
返回 `StaticFeishuIdentityDirectory`，其中的 `AuthenticatedPrincipal` 已经把 labels 压成了
`frozenset[ChannelPermission]`（`feishu_identity.py:125`）。`viewer` 与 `approver`、`operator` 与
`dba`/`oncall` 压完之后完全一样，而规格 §6.6 的迁移表恰恰要按**原始 label** 分流。
`_IdentityDocument` 与 `_read_identity_file` 都是私有的。因此本切片**抽出**一份公开的、保留原始
labels 的只读解析契约，让两条路径共用同一个解析器——不是第二次实现解析。抽出时不改变它的校验
规则、大小上限、权限位检查，也不改变任何现有调用方的行为。

```python
# interfaces/feishu_identity.py —— 把私有的 `_IdentityEntry` 改名公开。值域、数量边界、校验规则
# 一字不改（下面这行就是 feishu_identity.py:41-43 的原样），只给 subject_ref 补
# exclude=True, repr=False（它是受控 PII）
class LegacyIdentityEntry(Contract):
    subject_ref: ControlledPii
    actor: StrictStr                                   # 旧文档对 actor 无长度上限，保持无上限
    labels: tuple[
        Literal["operator", "dba", "oncall", "viewer", "approver", "admin"], ...
    ] = Field(min_length=1, max_length=6)

def read_legacy_identity_document(
    *, path: str, tenant_id: str, environment_id: str
) -> tuple[LegacyIdentityEntry, ...]: ...

# interfaces/legacy_identity_migration.py
def legacy_user_id(*, actor: str) -> str: ...          # 摘要截断，总长 ≤ 64
class LegacyMigrationConflictError(RuntimeError): ...   # 没有任何一行被写入
class LegacyMigrationReport(Contract):
    created: StrictInt; skipped: StrictInt
    deferred_labels: tuple[tuple[StrictStr, StrictStr], ...]
    audit_event_ids: tuple[StrictStr, ...]
async def migrate_static_identities(
    *, document_path: str, directory: UserDirectoryStore,
    tenant_id: str, environment_id: str, actor_user_id: str, actor: str,
) -> LegacyMigrationReport: ...
```

标签映射逐行取自规格 §6.6：`admin → ADMIN`；`operator` / `dba` / `oncall` → `OPERATOR`；
`viewer` / `approver` → `USER`。一条记录带多个标签时取**最高**角色。

`approver` 映射到 `USER` 而不是某个审批角色：旧 approver 标签当前没有 ACL 真源，R1 交付结果
artifact 时才决定它的映射。现在给它任何高于 `USER` 的东西，都是在凭空生效一份没有消费者的授权。
它只进 `deferred_labels` 报告。

**`labels` 保持六成员闭集，数量边界保持 1–6。** 这一条单独写出来，是因为上一版把它写成了
`tuple[StrictStr, ...]`——那会让今天被拒的空标签、未知标签、7 项标签全部变成合法输入，**改变既有
飞书身份解析行为**，而未知标签一路走到 §6.6 映射表时会变成一个非结构化的 `KeyError`，不是
fail-closed 的拒绝。公开化只改可见性和 `subject_ref` 的 `exclude` / `repr`，不改值域。

**长度转换策略（三个字段各走各的）。** 旧文档对 actor 没有上限，而新契约有，两端必须在这里接上：

| 目标字段 | 上限 | 旧 actor 怎么变成它 | 超限时 |
| --- | --- | --- | --- |
| `user_id` | 64 | `legacy_user_id(actor=...)`，domain-separated 摘要截断；**不拼 actor** | 不可能超限，摘要定长 |
| `actor` | 256 | **原样搬过去，不截断**——它是身份，截断会把两个人合成一个 | 整批零写入 |
| `display_name` | 128 | 由 actor 截断到 128 | 不适用，按定义总能装下 |

于是 256 这个阈值两侧的行为都是确定的：**≤ 256 字符的 actor 完整迁移**，`actor` 列里存的就是原值；
**> 256 字符的 actor 让整批零写入**，在组装 `MigrateLegacyIdentitiesCommand` 时就被契约拒绝，
发生在任何 `apply()` 之前，因此不存在"写了一半"的状态。实现时把那一次 `ValidationError` 转成
`LegacyMigrationConflictError`，不要让 pydantic 的异常泄到调用方。

这也是上一版自相矛盾的地方：切片 A 写"超长 actor 在契约层就被拒"，切片 C 写"200 字符的 actor
不会炸"，却从没给出那个上限是多少——两句话可以同时是对的，也可以同时是错的，无法验证。

### C.2 关键不变量

1. **整批原子。** 剩余条目组装成**一个** `MigrateLegacyIdentitiesCommand`，一次 `apply()` 写完。
   捕获 `UserDirectoryConflictError` / `AdminAuditUnwritableError` 转成 `LegacyMigrationConflictError`。
   **不做事务外预检**：预检和写入之间有时间窗，而且预检通过不等于写入成功。原子性由那一个
   事务给。
2. **skip 判据是闭集 fail-closed，不是"报告用途"。** 只有五项**全部精确匹配**才记 `skipped` 并从
   批次剔除：账号存在、`actor` 相同、状态是 `ACTIVE`、作用域角色等于本次派生出的角色、
   `resolve_by_subject()` 能用该条目的 `subject_ref` 解析到**同一个** `user_id`。任何一项不符（包括
   "账号在、绑定不在"和"账号在、角色不同"）一律抛 `LegacyMigrationConflictError`，整批不写。

   **理由，也是上一版的缺陷：** 被判 `skipped` 的条目已经**从批次里剔除**，第 1 条的唯一约束
   根本不会碰它。"即使漏判，后面的约束仍会让整批回滚"这句话在这里不成立。凡是"先剔除、再让
   后面的约束兜底"的结构，都要问一句：剔除之后，后面的约束还看得见它吗？看不见，那这一步就是
   最终判据，必须自己 fail-closed。
3. **报告不含明文 `open_id`**，`repr` 与 `model_dump_json` 都不含。
4. **旧静态文件不删、不双写**：迁移成功后保留只读一个发布周期。

### C.3 测试意图

`tests/contract/test_legacy_identity_document.py`：
`test_reading_preserves_the_original_labels`（抽出解析器的全部理由）、
`test_reading_enforces_the_same_scope_check_as_before`、
`test_reading_never_echoes_the_document_on_failure`、
`test_the_existing_directory_loader_still_behaves_identically`（既有调用方零行为变化）、
`test_the_public_entry_hides_the_open_id`、
`test_empty_labels_are_still_rejected`、
`test_an_unknown_label_is_still_rejected`、
`test_more_than_six_labels_are_still_rejected`。

**后三条是"公开化没有放宽值域"的证据**，不是凑数：它们逐条对应上一版会误放进来的三类输入。
把 `labels` 写回 `tuple[StrictStr, ...]` 时，这三条必须同时变红。

`tests/contract/test_legacy_identity_migration.py`：

| 用例 | 钉住什么 |
| --- | --- |
| `test_labels_map_to_product_roles_per_spec_6_6` | 映射表逐行 |
| `test_viewer_and_approver_both_become_plain_users` | `approver` 只进报告，不生效授权 |
| `test_multiple_labels_take_the_highest_role` | 取最高 |
| `test_rerunning_the_migration_changes_nothing` | **正常对照**：完整迁移过的条目判 `skipped`，迁移可重跑 |
| `test_a_database_conflict_leaves_zero_rows_behind` | 整批原子性；冲突必须制造在**数据库既有事实**上——在输入文件里放两个相同 actor 测到的是解析器，解析期就被拒了 |
| `test_every_migrated_entry_writes_its_own_audit_event` | 每条一个审计，动作与结果正确 |
| `test_a_256_character_actor_migrates_whole` | 上限之内**完整迁移**：`actor` 列逐字等于原值（没被截断），`user_id` 是摘要且 ≤ 64，`display_name` 截到 128 |
| `test_an_actor_beyond_the_contract_bound_writes_zero_rows` | 上限之外**整批零写入**：257 字符 actor 抛 `LegacyMigrationConflictError`，同批次里合法的那一条也没进库，审计一条没多 |
| `test_the_bound_failure_happens_before_any_apply` | 上一条红在"命令组装期"而不是"写到一半"：断言 `UserDirectoryStore.apply()` 从未被调用 |
| `test_report_never_contains_a_plaintext_open_id` | 不变量 3 |
| `test_an_account_without_its_binding_is_a_conflict_not_a_skip` | 不变量 2 的"绑定缺失"一格；同时断言批次里**另一条**没写进去、审计一条没多 |
| `test_a_role_that_no_longer_matches_is_a_conflict` | 不变量 2 的"角色不符"一格 |

**最后两条是反例，必须遵守"只允许一项事实不成立"。** 尤其是角色那条：它要先把账号、actor、
状态**和飞书绑定**全部按正确值建好，只让角色一项不同。V9 的版本只预建了账号和角色、没建绑定，
于是即使删掉"角色必须一致"这一项检查，它仍会因为"绑定缺失"而抛冲突，测试继续绿——它测的
根本不是自己名字说的那件事。

### C.4 验收要求

- 四门全绿。
- 反证两条，各自写清恢复的是哪个具体旧缺陷，并确认目标用例红在**预期断言**上：
  1. **把 skip 判据退回"账号存在就 `skipped`"**（不是把冲突分支改成 `if False:`——那样条目会落进
     批次、撞唯一约束、照样抛错，缺陷没被恢复）。预期：上面最后两条反例同时变红，且红在
     `pytest.raises(LegacyMigrationConflictError)` 上——**根本没有异常抛出**。
  2. **把整批命令拆成逐条**：对每个条目各构造一个只含它自己的 `MigrateLegacyIdentitiesCommand`
     并逐个 `apply()`（不是循环调用同一个完整批次——那样每次仍是原子的，原子性没被破坏）。
     预期：`test_a_database_conflict_leaves_zero_rows_behind` 变红，第一条留在库里。
- 文档真源同步，并由 `tests/contract/test_doc_fact_binding.py` 钉住：
  `test_architecture_records_the_w1a_write_kernel_as_implemented`、
  `test_architecture_states_the_single_write_path_invariant`、
  `test_truth_docs_do_not_claim_activation_or_admin_ui_exists`、
  `test_the_activation_claim_guard_is_discriminating`（反证：那条守卫不能宽到什么都抓不到）、
  `test_naming_a_future_component_with_its_phase_is_allowed`（正常对照：写"W1b 将提供 X"合法）、
  `test_handoff_names_w1b_as_the_only_post_merge_next_step`、
  `test_readme_no_longer_says_w1a_has_no_source`（README 顶部不再出现"没有 W1a 源码"
  "计划送审""计划获批后才可开始"这类字面量；它现在说的是"W1a 离线写内核已实现，下一步 W1b"）、
  `test_the_readme_guard_is_discriminating`（反证：把 README 那段改回旧措辞，上一条必须变红——
  否则它就是一条抓不到东西的守卫）、
  `test_development_plan_carries_no_implementation_progress`（`AGENTS.md:155`：进度与证据只放
  handoff。这一条同时说明本切片为什么**不**改 `DEVELOPMENT_PLAN.md`）。
- 四份当前真源文档**都不得**声称激活流程、登录改造、Admin 页面、真实飞书调用、部署或用户验收
  已经存在——包括本切片新纳入的 `README.md`。
- 提交后 `git status --porcelain` 为空。

```bash
git add src/xiaowei_agent/interfaces/feishu_identity.py \
        src/xiaowei_agent/interfaces/legacy_identity_migration.py \
        tests/contract/test_legacy_identity_document.py \
        tests/contract/test_legacy_identity_migration.py \
        tests/contract/test_doc_fact_binding.py \
        ARCHITECTURE.md AGENT_HANDOFF.md README.md
git commit -m "feat(w1a): migrate legacy identity labels in one atomic command

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
test -z "$(git status --porcelain)" || {
  echo "提交后工作区不干净：下面这些文件改了却没进本次提交"; git status --porcelain; false
}
```

每个切片各自开 PR，描述分四段：**已验证**（真实跑过的命令与尾部输出）、**只读推理**、
**未覆盖**、**残余风险**。明确写出：这是离线写内核实现，**不是**激活流程、登录改造、Admin 页面、
真实飞书调用、部署或用户验收。等待精确 SHA 复审与负责人合入，**不自行合并**。

---

## Review Focus

按承重程度排序。前六条任何一条不成立，W1a 就没有达成它的目的。

1. **`apply()` 是否真的不给调用方任何机会指定 action / target / outcome**：签名上有没有
   override、可选覆盖字段或 hook。同时核对另一个方向：`AdminAuditStore` 的三个写方法是否
   **都写不出目录成功事实**。
2. **四张授权表的写入是否全部发生在 `apply()` 的那一个事务内**：`grep` 一遍 `persistence/` 里
   对这四张表的 INSERT/UPDATE，看有没有第二处；特别核对 `seed_if_absent` 与旧身份迁移。
3. **审计失败回滚是否在真 PostgreSQL 上验证过**，且集成 fixture 确实绑到了 PostgreSQL 实现；
   内存实现的恢复是否**不挑异常类型**。
4. **effect 能否回答"改成了什么"**，且是闭集列而不是自由文本或 JSON；`role_revoked` 是否确实
   不带角色。
5. **bootstrap 的幂等判定读的是目录链接是否完整，还是"凭据行是否存在"**：一个跑过 RI5、
   `local_admins` 早有行的库，升级后能否补齐账号、ADMIN 角色与链接，且**不覆盖**已改过的密码。
6. **旧身份迁移的 skip 判据是否五项全等才跳过**，部分存在是否整批回滚。被剔除出批次的条目，
   后面的唯一约束再也看不见它。
7. **承重守卫是否按属性判定而不是按写法枚举**；静态判不出的形式是报警还是放行；限制有没有被
   如实写成残余风险而不是"完全封死"。
8. **协议与实现是否精确相等**（`==` 而不是 `isinstance`）。
9. **新 PostgreSQL 代码是否复用了同文件既有机制**（矩阵三）；有没有出现 `except IntegrityError`、
   读 `exc.orig` 或约束名。
10. **`operation_id` 是否不是全局唯一**，两条偏唯一索引的谓词是否正确，结构用例的 helper 有没有
    把偏唯一误算成全局唯一。
11. **迁移降级守卫的调用形状是否与 `guards.py` 的真实签名一致**，真库上是否有拒绝 / 数据保留 /
    授权后删除 / 重新升级四段证据，且每条主动降级的用例都 `try/finally` 升回了 `head`。
12. **每条反例是否只让一项事实不成立**；每条变异是否真的恢复了某个具体旧缺陷，而不只是让
    `assert after != before` 通过。
13. **diff 是否只含 allowlist**；有没有顺手加 HTTP 路由、激活表、DBA/值班表或 requester/approver
    载体。

## W1a Exit Criteria

- 三个切片各自独立合入，每个合入时四门全绿、CI 真实执行通过。
- `UserDirectoryStore.apply()` 是四类授权事实的唯一写入口，并由机械守卫钉住。
- 每一次成功的授权改变都有且只有一条同事务的审计事件，且该事件能回答"谁、对谁、改成了什么"。
- 审计表 append-only：`persistence/` 里没有任何 `update` / `delete`，也没有清理 API。
- 两个实现在共享行为套件上逐格相同，PostgreSQL 侧的原子性在真实数据库上验证过。
- 旧静态身份可以一次性、原子地迁入，且半迁移状态 fail-closed。
- 三份当前真源文档如实记录"W1a 写内核已实现"，且不声称激活流程、Admin 页面或任何部署/验收事实。
- W1a 的网络调用次数恒为 0。

## 返工来历

前九版都被复审打回。记在这里只为一件事：**每一次的根因都是"改了一处，没跟到下一处"，
而不是某个具体写错的字符。**

| 版本 | 根因 |
| --- | --- |
| V2 | 审计可被调用方伪造（通用 `append(candidate)`） |
| V3 | 为了堵伪造，把写契约整个删掉——违反 `DEVELOPMENT_PLAN.md:178` 与 `ADR-013:131` |
| V4 | allowlist 漏了明确要改的文件；`git add` 与 `Files:` 不同步 |
| V5 | 引用了既有契约上并不存在的字段 |
| V6 | 同一条规则在多个章节各写一遍，两处答案互相矛盾 |
| V7 | 错误语义没有唯一真源；按 psycopg 的 `exc.orig.diag` 取约束名，而本仓库用 asyncpg |
| V8 | 片段各自验证过，但从没按最终形状拼起来跑过——拼出来的守卫文件带着一组互斥断言 |
| V9 | 与既有代码的接缝只写了自己那一端：调用既有函数没核对签名，给既有契约加字段没跟到读取路径 |
| V10 | 主动降级的用例不还原 schema，会污染后续集成测试；反例与变异没有真正撤掉目标保护 |
| V11（本版） | 收缩时把旧契约的值域和长度上限重写成了更宽的形状，且两个切片对同一个上限给出互相矛盾的要求；文档同步漏掉 `README.md`，而 `README.md` 正是四份当前真源之一 |

**V11 的两处都属于同一类：收缩时只跟到了自己写的那一端。** `labels` 写成 `tuple[StrictStr, ...]`
是没回头看既有实现；一个上限在切片 A 说"拒"、在切片 C 说"过"，是没把两个切片放在一起读；
`README.md` 漏掉，是没回头看 `_CURRENT_TRUTH_DOCS` 到底列了几份。收缩本身是对的，但收缩过的
每一处，都要拿既有代码和既有契约再对一遍——这正是 V9 那条根因的复发形式。

**V10 同时做了一件结构性的事：把计划从 8100 行收缩到现在这个规模。** 前九轮里有五轮的缺陷
出在计划里那些从未运行过的代码片段上。它们看着像证据，其实一行都没跑过；而在真实代码上，
`ruff`、`mypy` 与真实测试覆盖同样的问题且覆盖得更严。继续围着伪代码打补丁，只会用更多轮次
换来同一类缺陷。
