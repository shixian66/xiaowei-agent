# M4 PostgreSQL TaskStore 与恢复详细实施计划（V1.9）

> 状态：**已验收通过并合入 `main`**（`714df07`，21 个受审提交以 `--ff-only` 原样保留）。归档见 [docs/handoff/archive/2026-09-04-M4-postgres-taskstore.md](../handoff/archive/2026-09-04-M4-postgres-taskstore.md)。V1–V1.2 经项目负责人与 Codex 审核批准开工（依据 [DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) §11）；T0–T13 已全部落地，经 PR [#6](https://github.com/shixian66/xiaowei-agent/pull/6) 合入。本文件自 V1.3 起同时充当**实施与复审记录**，§10 各任务段按完成时的事实书写。
>
> V1 按 Codex 审核的 7 项打回（B1–B7）与自查的 4 项同类问题（S1–S4）成稿，并记入项目负责人 2026-09-03 的三项拍板（§14.1–§14.3）。**V1.1 按提交后的计划复审自查，修正 6 项（§6.3）**——其中 P1、P3 是 V1 的过度断言，P2 是本计划内第三次漏读交付物。**V1.2 是 Codex 复审通过后的纯文档修正**：更新已过期的分支/SHA/工作区事实，并把 §8.4 对审批读回的处置从「一处判断」固化为已拍板结论（§14.4）。**V1.2 不改变任何设计决策、任务拆分或判定标准。****V1.3 是 Codex 首轮验收打回（受审 SHA `797a210`）后的修订**：新增 §10 T10（三条阻断项 + 一条非阻断的逐条根因与处置）与 §14.5（审计事件的拍板）。**V1.3 追加了一个任务和一条 Protocol 方法，因此不是纯文档修正。****V1.4 追加 §10 T11**：第二轮复审自查发现 `integration` gate 的触发条件本身没有承重，处置为四条新测试，不改 `ci.yml`。**V1.5 追加 §10 T12**：`integration` 首次真实运行（PR #6，run `33828598775`）暴露两类缺陷，共同前提是 integration 用例此前既无静态检查也无运行时检查。**V1.6 追加 §10 T13**：Codex 第二轮复审打回 `list_stale_leases` 先 `LIMIT` 后过滤（P1）与一轮文档事实漂移。**V1.7 是纯文档修正**：文首状态行由「待审核草案」改为实际状态；§10 T5 / T6 / T9 三段仍用现在时写着「一次都没跑过」「镜像用版本 tag」，改为标明那是**交付当时**的事实并注明此后已被哪一任务解除。**V1.7 不改变任何设计决策、任务拆分或判定标准。****V1.8 是合并后的交接收口**：状态行改为已合入并指向归档；三处「基线 = `main` = `origin/main`」在合并后已成假话，改为标明那是成稿时的值。**V1.9 是归档后事实漂移修正**：清掉顶部 handoff 与计划局部仍把 M4 写成待验收、待首次 CI 或仍未验证的现在时残留。**V1.8/V1.9 均不改变任何设计决策、任务拆分或判定标准。**
>
> 依据基线：本计划成稿时的 `main` = `12b5b584da031bff7aa26ab5544d2122736d8945`。**M4 已合入，该值不再等于 `main` 的当前 HEAD**（用 `git rev-parse main` 查询）。本计划位于分支 `claude/m4-postgres-taskstore`，Codex 复审对象为 `c235ee8f0153931214900c52d20fbdfd091e0c11`（V1.1）。真源为 [ARCHITECTURE.md](../../ARCHITECTURE.md)、[ADR-007](../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-008](../adr/ADR-008-engineering-and-test-baseline.md)、[ADR-009](../adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md)、[DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) §7 M4。与真源冲突一律以真源为准。

---

## 1. 目标与判定标准

把任务生命周期从进程内对象迁移为**存储层强制的不变量**：并发、租约、终态保护不再依赖调用方自觉，而由 PostgreSQL 承重。

判定标准八条：

1. **同一套行为用例在两个实现上逐条通过。** `InMemoryTaskStore` 与 `PostgresTaskStore` 绑定同一份契约套件；**元测试断言两个绑定覆盖的用例名集合相等**，使"新增用例只落到一个实现上"不可表达。
2. **并发结果由存储层裁决。** 真实多连接竞争同一次 CAS 时，恰有一个提交成功，其余全部拿到 `VERSION_MISMATCH` 与存储层 winner；这一条必须在**多个数据库连接**上复现，不能用 `asyncio.gather` 共用一个连接冒充并发。
3. **并发重复请求只产生一个任务事实。** 多个连接同时以同一 `(tenant_id, environment_id, idempotency_key)` 创建时，唯一索引使其中之一失败；实现必须**捕获唯一键冲突并返回既存记录**，而不是把数据库错误抛给调用方。这是 DEVELOPMENT_PLAN §7 M4 退出标准的第一句；既有用例只覆盖**串行**重复创建，并发路径此前无任何覆盖。
4. **崩溃恢复无中间态。** 事务执行中途断开连接后，任务状态要么是事务前的值，要么是事务后的值；不存在版本已增而状态未改、或租约字段部分置位的记录。
5. **三个承重保护各自可反证。** terminal protection、CAS、fencing 逐个撤掉后，对应用例必须转红（DEVELOPMENT_PLAN §7 M4 测试门明文要求）。
6. **跨进程恢复不使漂移检测退化。** plan 与 target 从 PostgreSQL 读回，`plan_hash` 与 `target_fingerprint` 由**调用方当下重算**，两个指纹在任何表里都不存在。清空/篡改存储中的 plan 必须使漂移检测转红，而不是静默通过。
7. **网络放行的窄度可被机器证明。** `tests/integration/` 之外的用例仍然连不出去；integration 用例连非 DSN host 仍被拦；DSN 已设置时 integration 的 skip 数为 0。
8. **stale recovery 可被发现，不只是可被接管。** 存在一条只读查询能列出"曾被租出、租约已过期、未终态"的任务；对它的接管仍走 `acquire_lease()`，并发 winner 仍由存储层裁决。**只验证 `acquire_lease` 能接管一个已知 `task_id`，不构成对 stale recovery 的验证**——那只证明了接管，没证明恢复。

---

## 2. 架构取向

七条贯穿全部任务的取向。其中第 2 条由本轮审核的 B3 与自查的 S2/S3 共同根因导出；第 3 条由 §5.2 的实证导出；其余为沿用 M2/M3 的既有取向。

1. **同一语义只允许有一个判定真源。** 拒绝顺序与 fencing 真值表抽成 `persistence/decisions.py` 的纯函数，两个实现共同消费。若各写一份，两份实现必然漂移，而漂移只在某条用例恰好覆盖时才被发现。

   > 反对意见先摆出来：共享代码意味着纯函数里的 bug 会让两个实现同时通过。这是真的。但替代方案（各写一份）**保证**漂移，且两种方案的测试集完全一样。取共享，并以「变异 `decisions.py` 后既有用例转红」作为该函数确实承重的证据。

2. **检查不得空洞。** 凡"当前值 vs 已存值"的比较，**当前值必须来自调用方**，不得在同一次调用里从存储读出来再和自己比。这是 M3 首轮深档验收打回的那条阻断项（`WorkflowRunner` 窄签名）的同一根因，在 M4 有三个新的发生点，见 §8.4。
3. **时钟只有一个来源。** `PostgresTaskStore` 与 `InMemoryTaskStore` 一样经参数注入 `Clock`，`expires_at` 在 Python 侧算好、比较时把 `now` 作为**参数**传进 SQL。用服务端 `now()` 会让注入时钟推不动租约过期，六条过期/抢占用例只能靠 `sleep` 复现——而 `sleep` 会让这些用例慢到必然被人关掉。
4. **不可表达优于运行时校验。** 沿用 M2/M3：能用类型、闭集枚举、DB 约束表达的，不写成 `if`。`TaskRecord` 的「三个租约字段同时置位或同时清空」在 Pydantic 与 CHECK 约束上各表达一次，两层独立承重。
5. **新增能力用新增方法/新表表达，不改 M2 已冻结的形状。** 沿用 M3 新增 `PlanStore` 的先例。若 PostgreSQL adapter 迫使既有形状改变，按 DEVELOPMENT_PLAN §7 M4 停止 M4，回到 M2/M3 修契约，不在 adapter 内加兼容补丁。
6. **DTO 以规范 JSON 落库，schema 不复制契约字段。** 计划、目标、证据、审批以 JSONB 存储，只把用于查询和唯一性的少数字段提取成列。理由：契约是真源，把它的字段抄成 DDL 会产生第二份需要同步的字段表。round-trip 保真由测试承重。
7. **依赖环境的 gate 必须无法静默跳过。** integration 用例在无 DSN 时跳过是合理的；但 DSN 已设置却跳过，等于 gate 悄悄死掉却全绿。以一条元测试钉死。

---

## 3. 技术栈与新增依赖

Python 3.11、Pydantic v2、标准库。**M4 新增且仅新增三个第三方依赖**：

| 依赖 | 落点 | 理由 |
| --- | --- | --- |
| `sqlalchemy[asyncio]` | `[project].dependencies` | Core 的 Table metadata 支撑 Alembic 自动比对；持久化是生产代码，不是 dev 工具 |
| `alembic` | `[project].dependencies` | 迁移入口随应用发布（M5 的 Compose 要调用它） |
| `asyncpg` | `[project].dependencies` | SQLAlchemy 2.0 的 async PostgreSQL 驱动 |

**明确不加**：FastAPI、uvicorn、Docker/Compose 相关、pgvector、Redis、任何队列、任何 Worker 框架、任何模型 SDK、LangGraph。这些分属 M5 及以后，或需独立准入。

**用 SQLAlchemy Core，不用 ORM。** 项目负责人已就此拍板。理由记录在案：ORM 的 session identity map 与 flush 时机会让「必须采纳存储层 winner」这条不变量更难断言——而并发语义正是 M4 的全部承重点。Core 保留显式语句，CAS 与租约的 SQL 逐字可读、可审计。

**版本区间**在 T0 确定并 `uv lock` 重生成；`deps-audit` job 用现有的 `uv export --frozen` 路径覆盖，无需改该 job 的命令。

---

## 4. 全局约束

- 验证命令恒为四条，逐字不变：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。**M4 不新增第五条命令口径**（见 §9.1）。
- `ruff` 选中 `E,F,W,I,N,UP,B,S,ANN,RUF`，行宽 100；`mypy` strict。
- 执行上下文字段名恒为 `tenant_id`、`actor`、`environment_id`，无别名。
- 安全测试置于 `tests/security/` 并 `pytestmark = pytest.mark.security`。
- **M0–M7 全程禁止 E1**；`tools/gateway.py::_E1_EXECUTION_ENABLED` 保持 `False`。TaskStore/approval/audit/evidence 的内部持久化与本地 migration **不构成 E1**（ADR-007 D7），但也不因此获得任何别的基础设施许可。
- **不连接任何真实运维目标系统或模型 API。** M4 唯一新增的网络连接是**本地隔离 PostgreSQL**，已获项目负责人在本轮明确批准（本地 + CI 均批准），符合 ADR-007 D4/D8 的 M4 时点。
- **CI 不持有任何凭证。** PostgreSQL service container 使用 `POSTGRES_HOST_AUTH_METHOD=trust`，因此 CI 里既没有密码，也没有密码形状的字面量——这既满足 ADR-007「CI 不持有任何测试、生产或运维目标系统凭证」，也不给 `secret-scan` 与 `test_secret_shaped_literals.py` 制造豁免需求。
- 不得出现 secret、token、密码、连接串或真实生产数据；伪造字面量一律拆开写。
- `src/` 内不使用 `assert` 表达运行时不变量；一律显式 `raise`。
- 新增 `raise` 里的插值必须先进 `test_reject_path_hygiene.py::SAFE_INTERPOLATIONS`。
- **不新增顶层包**：全部新代码落在既有 `persistence/` 内，因此 `test_module_layering.py::_ALLOWED_INTERNAL` 的 `persistence` 条目（`{contracts, planning, persistence}`）无需修改。若实现过程中发现需要新包，必须在**创建它的同一个任务**里登记白名单，不预登记、不 `xfail`。
- **不扩大 `test_domain_layer_has_no_third_party_client_import` 的扫描范围**：`persistence/` 持有数据库客户端是既定设计（AGENT_HANDOFF「不要盲改」明列），不得因新增 asyncpg 而把 `persistence` 加进领域层名单，也不得反向把该测试放宽。

---

## 5. 前置验收证据

### 5.1 基线

**已验证**（2026-09-03，本机 `.venv`，Python 3.11.16）：

| 项 | 值 |
| --- | --- |
| 分支 | `claude/m4-postgres-taskstore`，自 `main` 切出，只含本计划文件的提交；工作区有两个未跟踪文件：`development-route-v3-proposal.md`、`legacy-capability-migration-matrix.md` |
| 暂存范围 | **只允许本计划文件**。那两个未跟踪文件不属于 M4 范围，不得暂存、修改或删除（§12.3） |
| SHA | 成稿时的 `main` = `12b5b584da031bff7aa26ab5544d2122736d8945`（**已非当前 HEAD**）；本分支 V1 = `b546aca2c63c0a708dcdbbf53e2157a232df8a79`，V1.1 = `c235ee8f0153931214900c52d20fbdfd091e0c11`，V1.2 = `5305dc4`；Codex 首轮验收对象 = `797a210` |
| M3 验收对象 | `64d295c8e4f38028527ec9a496262c7a660258b1`，已在祖先链 |

### 5.2 `pytest-socket` 行为实证

本节结论**不要求复审者相信文字**：全部断言在 T5 落成可执行测试。

已安装版本 **0.8.1**。读 `pytest_socket/__init__.py::pytest_runtest_setup` 并在临时目录实跑四条用例，得到：

| 事实 | 证据 |
| --- | --- |
| `socket_enabled` fixture 命中后 `enable_socket()` 即 `return`，`_resolve_allow_hosts()` 永不执行 | 用例断言 `socket.socket.connect is _true_connect`（**未被保护**）通过 |
| `allow_hosts` marker 命中时 `socket_allow_hosts()` 打上受限 `connect` 补丁，且 `disable_socket()` 不执行 | 用例断言创建放行、`connect` 被接管，且连非白名单 host 抛 `SocketConnectBlockedError`，均通过 |
| **全局 `--allow-hosts`（CLI 或 `addopts`）会使 `hosts` 对每个用例为真，`disable_socket()` 对全套件永不执行** | 在传入全局 `--allow-hosts` 时，`test_no_network.py` 形态的「创建应被拦」用例**失败** |
| 只用 `pytest_collection_modifyitems` 给目标目录的 item 加 marker 时，目录外用例的创建与 DNS 仍被拦 | 8 passed，含目录外的创建拦截与 DNS 拦截两条 |

**结论**：放行机制必须是 **`allow_hosts` marker，按目录逐 item 添加**；**绝不使用 `socket_enabled` fixture 作为安全论据，也绝不在 `addopts` 或 CLI 使用全局 `--allow-hosts`**。

关于第三条事实的残余风险，须说清楚它**不是**静默失效：全局 `--allow-hosts` 会使 `test_no_network.py` 的创建拦截断言**转红**——实证中我自己的等价用例正是这样失败的。所以该文件本身就是这条边界的哨兵。真正的残余风险是**人**：有人看到红灯后，为了让 integration 跑起来而去放宽 `test_no_network.py`，而不是收窄放行范围。这条风险无法用测试消除，只能靠评审；因此 §9.2 的三条测试把「窄度」表述成独立的正面断言，使放宽哨兵的代价从"改一个文件"变成"同时改三处并解释理由"。

### 5.3 T0 实证事项

三项在 V1 定稿时**均未验证**，不得当作既成事实。前两项已由 T0 实测收口；第三项在 V1/T0 时仍待 T9，此后已由 CI 证实：

1. **SQLAlchemy 2.0 Core 在 `mypy strict` 下的类型完备性——已验证，通过。** `sqlalchemy` 2.0.52 自带 `py.typed`。探针覆盖 `Table` / `Column` / `update().where().values().returning()` / `select().order_by().limit()` / `AsyncEngine.begin()`，`mypy --strict --no-incremental` 零告警。**无需任何 override，全局 strict 完好。** `alembic` 1.19.1 同样自带 `py.typed`。
2. **`asyncpg` 的类型标注完备性——已验证，结论与计划的预设退路不同。** `asyncpg` 0.30.0 **不带 `py.typed`**；直接 `import asyncpg` 会在 strict 下报 `import-untyped`。

   V1 给的退路是"对单个模块加 `ignore_missing_imports`"。**这条退路不必走，因此不走**：驱动只经 `postgresql+asyncpg://` 的 DSN 方言字符串由 SQLAlchemy 内部加载，业务代码从不需要它的任何符号。**改为禁止 `src/` 直接 import `asyncpg`**——不 import 就不产生缺失存根，也就不需要放宽任何检查。

   代价是这条禁令必须被机制而非注释持有，且需要一个撤销条件。`tests/security/test_dependency_baseline.py` 同时承担两者：AST 扫描 `src/` 禁止该 import，另一条反向断言钉住"asyncpg 当前没有 `py.typed`"——上游一旦补上，该断言转红，正是重新评估禁令的时刻。
3. GitHub Actions service container 从 job 容器经 `127.0.0.1:5432` 可达。**V1 定稿时仍未验证**，不得当作既成事实；此后已由 PR #6 与 `main` push 的 `integration` job 证实。这是标准行为，但属于「未经本项目验证的第三方行为」，与 §5.2 打回的那条属同一类，因此在 T9 前只能列为显式验收项而非假设。

---

## 6. 审核处置

### 6.1 Codex 本轮打回项

| # | 意见 | 判定 | 根因与处置 |
| --- | --- | --- | --- |
| B1 | 不要写「`socket_enabled` + `--allow-hosts` 限死 host」 | **接受，且影响比原意见更大** | 根因：`pytest_runtest_setup` 在 `socket_enabled` 分支 `return`，后续 `allow_hosts` 解析不执行，等于完全放开网络。沿同路径自查另发现：**全局 `--allow-hosts` 会废掉全套件的创建拦截**（§5.2 第三条实证）。处置见 §9.2，并补三条安全测试 |
| B2 | T3 缺 `task_audit_events` | **接受** | 根因：我按「M4 = 换存储实现」理解交付物，漏读 DEVELOPMENT_PLAN §7 M4 交付物里并列的「审计事件」。`task_evidence` 是证据台账，回答"取到了什么"；audit 回答"谁在何时使系统发生了什么状态变化"，两者不可互相替代。处置见 §7.1 |
| B3 | `task_plans` 不要存 `plan_hash` / `target_fingerprint` | **接受** | 根因与 M3 首轮打回的阻断项完全相同：两侧都从存储读，比较恒真，漂移检测退化成空操作。且 `StoredPlan` 已有 `test_plan_store_holds_nothing_but_plan_and_target` 钉死字段集恰为两项，存指纹会直接违反既有安全测试。处置见 §7.2、§8.4 |
| B4 | T5 要处理 `clean_xiaowei_env` 会清掉 DSN | **接受，且根因更深** | 表层是 autouse fixture 抹掉变量；**更深的根因是这个变量根本不该带 `XIAOWEI_` 前缀**——`load_settings()` 对任何未知 `XIAOWEI_*` 变量 fail-fast，一旦清理 fixture 被收窄或移除，CI 里每个读 `os.environ` 的配置用例都会炸。处置见 §9.3 |
| B5 | T7 表述为「实现现有 port 的 PostgreSQL adapter」，不是新增 port | **接受** | 措辞修正。`PlanStore` / `EvidenceLedger` 的窄口径已由 `plans.py` 与 `evidence.py` 承重，M4 只加实现 |
| B6 | T0 只加 `sqlalchemy`、`alembic`、`asyncpg` | **接受** | 已写入 §3 的「明确不加」清单 |
| B7 | 不要 `git add -A`，只处理本计划文件 | **接受** | 见 §12.3 |

### 6.2 沿同一代码路径自查发现的同类问题

审核只指出了 B2/B3 两处，但它们各自代表一类缺陷。沿同类路径复查后另发现四项：

| # | 问题 | 与哪条同类 | 处置 |
| --- | --- | --- | --- |
| S1 | **`stale recovery` 同样是被漏掉的交付物。** DEVELOPMENT_PLAN §7 M4 交付物并列了「heartbeat、fencing token、stale recovery」；`renew_lease` 覆盖 heartbeat，但 TaskStore 现有五个方法中**没有任何一个能发现"哪些任务的租约已过期且未终态"** | 与 B2 同类（漏读交付物） | **已由项目负责人拍板：M4 内加，形状收窄**（§14.1）。追加只读方法 `list_stale_leases`；这是对 M2 已冻结 Protocol 的**追加**而非修改，循 M3 新增 `PlanStore` 的先例。语义边界与非目标见 §8.5 |
| S2 | **`expected_version` 不得在事务内重读。** PostgreSQL 实现会用 `SELECT ... FOR UPDATE` 取行以保证拒绝顺序；此时"顺手用刚读到的 `version` 去 UPDATE"是极自然的写法，而那会让 CAS 永远成功 | 与 B3 同类（空洞检查） | §8.4 列为禁令，并补一条承重测试 |
| S3 | **`policy_revision` 的审批有效性比较不得两侧都读库。** 「policy 变化不能静默让旧审批继续生效」要求把**存储中的**审批与**调用方当下的** `RequestContext.policy_revision` 比 | 与 B3 同类（空洞检查） | §8.4 列为禁令 |
| S4 | **新增一个 CI job 会同时打破五处闭集断言，不只是整文件 SHA。** `_EXPECTED_JOBS`、checkout 计数、setup-uv 计数、单行 run 命令的精确多重集、persist-credentials 计数 | 与 B1 同类（对既有机制的断言未经核对） | §11 给出逐项清单。另：Codex 提到的 env allowlist 与 service 约束**目前并不存在**，属于本里程碑要**新增**的断言，不是要更新的 |

### 6.3 计划复审自查（V1 定稿前）

对 V1 初稿逐条核对源码后发现六项，其中前两项是**初稿中的过度断言**——声称某机制存在或可复用，而实际不成立。它们与本计划批评 B1 时用的是同一把尺子。

| # | 问题 | 类型 | 处置 |
| --- | --- | --- | --- |
| P1 | 初稿称 S3 禁令「复用既有 `test_approval_binding.py` 扩到 PostgreSQL 绑定」。实际该文件测的是 `governance` 纯函数，与 TaskStore 无关；且 `record_approval` **只写不读**，全仓库无审批读回路径 | **过度断言**：声称的检查没有可施加的对象 | §8.4 改写：M4 不追加读回方法，存储保真由 integration 测试直接读表验证，S3 降级为记录在案的设计约束，M8 引入读路径时落成 |
| P2 | 并发幂等创建无覆盖，而它是 M4 退出标准的**第一句**；既有用例只测串行重复创建 | 漏读交付物（与 B2、S1 同类，本计划内第三次） | 新增 §1 判定标准 3；T6 增用例与 TDD 反证 |
| P3 | 初稿称两个安全测试文件「可以原样重绑」到 PostgreSQL。实际它们在 `tests/security/`，socket 被拦，原地绑定不可能；参数化 `store` fixture 会让全部安全用例尝试连库，与 §1 判定标准 7 冲突 | **过度断言**：结构上不成立 | §9.5 改为「抽取到共享套件模块再分别绑定」，并写明为何不能参数化 |
| P4 | `task_id` 列类型未指定，而直觉选择 `uuid` 是错的——读回是 `UUID` 对象，`StrictStr` 会拒 | 未指定 + 默认选择有陷阱 | T3 写死 `text`，并推广到所有映射 `StrictStr` 的列 |
| P5 | round-trip 测试只覆盖 JSONB 表；`tasks` 是列展开的，属另一组风险 | 覆盖缺口 | T3 拆成两组独立 round-trip |
| P6 | `terminal_reason` 的置空语义**当前无任何测试断言**；fake 在每次迁移无条件写入（传 `None` 即清空），PostgreSQL 若写成「有值才 SET」会静默发散 | 覆盖缺口（与 M3 那四次首轮全绿的反证同类） | §8.3 写死无条件写入；共享套件补一条置位→清空用例 |

P2 是本计划内第三次「交付物被漏读」（前两次是 B2 的审计事件、S1 的 stale recovery）。**这说明按印象复述交付物清单是不可靠的**：M4 交付物与测试门必须逐句对照 DEVELOPMENT_PLAN §7 原文核验，而不是凭对里程碑主题的理解概括。

---

## 7. 数据模型与迁移

### 7.1 表结构

五张表，全部在一个 Alembic revision 内建立。

```text
tasks              任务事实真源。CAS、租约、fencing、终态保护都作用在这张表
task_plans         StoredPlan（plan + target），供 resume 读回
task_evidence      append-only 证据台账
task_approvals     审批记录
task_audit_events  append-only 审计事件（B2 补齐）
```

**`tasks`** —— 唯一需要把契约字段展开成列的表，因为 CAS、租约判定和唯一性约束都要在 SQL 里作用于单个字段：

| 列 | 约束 | 理由 |
| --- | --- | --- |
| `task_id` | PK | 与 `TaskRecord.task_id` 同值 |
| `tenant_id` / `environment_id` / `idempotency_key` | 三者 UNIQUE | 幂等作用域。全局键表会让跨租户同键共用一个任务（`test_idempotency_key_is_scoped_per_tenant` 承重） |
| `request_digest` | NOT NULL | 同键不同语义的判据 |
| `actor` / `status` / `terminal_reason` | | |
| `version` | NOT NULL, `>= 0` | CAS 依据 |
| `lease_owner` / `lease_expires_at` / `fencing_token` | CHECK：三者同时为 NULL 或同时非 NULL；`fencing_token > 0` | 与 `TaskRecord._lease_fields_are_consistent` 同一不变量，两层独立表达 |

**`fencing_token` 的单调性**由一个数据库 SEQUENCE 提供，`acquire_lease` 时 `nextval()`。序列天然单调且不需要额外加锁；跳号无害（契约只要求"旧持有者的 token < 当前 token"）。

**注意**：租约过期后三个字段**不清空**——`fencing_token IS NOT NULL` 正是"任务曾被租出"的判据，而整条 fencing 闭合规则锚定在这一点上（见 `persistence/fake.py` 的真值表）。清空它会重新打开「过期后不带 token 即可写入」那个缺口。

**其余四张表以 JSONB 存 DTO**，只提取查询/唯一性所需的列：

| 表 | 提取列 | JSONB | append-only |
| --- | --- | --- | --- |
| `task_plans` | `task_id` PK | `plan`、`target` | 否（幂等 upsert，内容不同即拒） |
| `task_evidence` | `task_id`、`evidence_id`（联合 PK）、`seq` | `envelope` | 是 |
| `task_approvals` | `task_id`、`step_id`、`state` | `request` | 是 |
| `task_audit_events` | `task_id`、`stage`、`outcome`、`occurred_at`、`seq` | `event` | 是 |

`task_evidence.seq` 与 `task_audit_events.seq` 是**每任务内的序号**，因为两个台账的读回契约是「按写入顺序返回」（`EvidenceLedger.load` 的 docstring），而 JSONB 里的时间戳可能同值。

`task_audit_events` 的 JSONB 载荷是 M2 已定义的 `TraceEvent`（`contracts/trace_events.py`）。它的 `detail` 字段在契约层已做键与值双向脱敏并限长，因此**审计表不会成为新的脱敏缺口**——这一点由 round-trip 测试连带证明，不需要在持久化层再写一份脱敏。

### 7.2 明确**不**进入 schema 的东西

| 不存 | 根因 |
| --- | --- |
| `plan_hash`、`target_fingerprint` | 两侧都从存储读会让漂移检测恒真（B3）。ADR-009 已裁定两个指纹不是 `ExecutionPlan` 字段，绑定值只存于 `ApprovalRequest`；`StoredPlan` 的字段集由 `test_plan_store_holds_nothing_but_plan_and_target` 钉死为恰两项 |
| 任何凭证、连接串 | ADR-007 |
| 模型原文、prompt、未脱敏的外部文本 | `ExternalContent` 一律 untrusted；证据以 `EvidenceEnvelope` 形态入库，不落原始 rows |
| 完整 rows / 大对象 | ARCHITECTURE §5.9：result memory 只存脱敏、限长、可重建摘要。存储位置与保留周期属 M6b 前待拍板事项，M4 不预设 |

### 7.3 迁移

- 单个 Alembic revision，`upgrade()` 与 `downgrade()` 都实现。
- **两条 upgrade 路径都要验证**（DEVELOPMENT_PLAN §7 M4 测试门明文）：空库、以及已有测试数据的库。
- Alembic 的 `env.py` 从注入的 DSN 读连接，**不读 `.env`、不读 `XIAOWEI_*`**（与 `load_settings()` 的来源隔离设计一致）。
- 迁移不写任何默认业务数据。

---

## 8. PostgreSQL 实现的语义保持

### 8.1 共享判定纯函数

新增 `src/xiaowei_agent/persistence/decisions.py`。约束（由项目负责人在批准 T1 时明确设定）：

- **无 I/O、无 SQLAlchemy 与 asyncpg 依赖、无 `async`、无全局可变状态。**
- 只依赖 `contracts` 与标准库，因此不改动 `_ALLOWED_INTERNAL` 的 `persistence` 条目。
- 输入是 `TaskRecord`（当前值）、命令 DTO 与 `now`；输出是 `TransitionRejection | None`。
- 由 `InMemoryTaskStore` 与 `PostgresTaskStore` **共同消费**。

承载的两项语义，逐字来自现有 `persistence/fake.py`（它已把这两项写成语义而非散落的 `if`）：

1. **拒绝顺序**：终态保护 → 租约/fencing → 版本 → 迁移合法性。顺序有语义：终态放最前，使"违反终态保护"不被版本不匹配掩盖成一个看起来正常的并发结果；租约放在版本之前，因为"拿着陈旧 token 的 worker"比"版本落后"更具体，审计需要看到前者。既有 `test_terminal_protection_wins_over_version_mismatch` 与 `test_stale_token_is_reported_before_version_mismatch` 承重。
2. **fencing 闭合真值表**，锚定在"任务是否曾被租出"而非"租约此刻是否 live"。后者留有缺口：租约过期后 stale worker 只要不带 token 就能写入（`test_expired_lease_cannot_be_bypassed_by_omitting_the_token` 承重）。

**T1 是零行为重构**：既有用例一行行为断言都不改，仍须全绿。

### 8.2 时钟

`PostgresTaskStore.__init__` 接受与 `InMemoryTaskStore` 相同的 `Clock`。所有与时间相关的判定：

- `expires_at = clock() + timedelta(seconds=ttl)`，在 Python 侧算出后作为参数写入；
- 租约是否 live 的比较，把 `clock()` 作为**查询参数**传入，不用 `now()`。

**不得使用服务端 `now()` / `CURRENT_TIMESTAMP`。** 否则注入时钟推不动过期，六条过期/抢占用例只能靠 `sleep` 复现，而慢用例必然被人关掉。

### 8.3 CAS 与拒绝顺序的实现形状

```sql
BEGIN;
  SELECT ... FROM tasks WHERE task_id = :task_id FOR UPDATE;   -- 取当前行
  -- 用 decisions.py 的纯函数在 Python 侧判定，得到 rejection 或 None
  UPDATE tasks
     SET status = :to_status, version = version + 1, terminal_reason = :reason
   WHERE task_id = :task_id AND version = :expected_version    -- ← 调用方传入的值
  RETURNING ...;
COMMIT;
```

`FOR UPDATE` 负责让并发调用串行化，从而使拒绝顺序与单进程实现一致；`WHERE version = :expected_version` 是 CAS 本身，**且该值必须是调用方传入的**（见 §8.4 S2）。

命令 DTO（`TransitionCommand` / `LeaseCommand`）必须在**开事务之前**构造完成。既有 `test_invalid_lease_arguments_do_not_mutate_the_store` 承重：非法入参先污染存储再抛 `ValidationError`，会留下一条带无效租约字段的记录，之后所有 fencing 判定都建立在这条脏记录上。

**`terminal_reason` 必须无条件写入，包括写 NULL**（复审自查 P6）。`InMemoryTaskStore` 用 `model_copy(update={"terminal_reason": terminal_reason})`，因此一次非终态迁移传 `None` 会**清空**它。PostgreSQL 实现若写成「有值才 SET」，同一序列会得到不同结果：先失败置上原因、再重试转为非终态时，旧原因会残留，而调用方看到的是一个状态与原因矛盾的记录。

这条语义在 V1 定稿时**没有任何测试断言**，属真实覆盖缺口。**T1 已补上**：`test_terminal_reason_is_cleared_when_a_transition_omits_it`（置上原因 → 再做一次不带原因的合法迁移 → 断言已清空）。它是 T1 变异反证的产物——把 `apply_transition` 改成"有值才写"时全部 1236 条用例仍然全绿，证明该缺口真实存在。T2 把它并入共享套件后，两个实现被同一条断言约束。

### 8.4 空洞检查禁令

三条禁令同一根因：**"当前值"若从存储读出来再和存储比，检查恒真**。这正是 M3 首轮深档验收打回的那条阻断项的根因。

| 禁令 | 若违反 | 承重测试 |
| --- | --- | --- |
| **S2** `transition` 不得在事务内重读 `version` 充当 `expected_version` | CAS 永远成功，并发写全部提交 | `tests/security/test_vacuous_check_ban.py`（AST 机械检查）。**原先写的"并发用例转红"是错的**，理由见 §10 T8 |
| **S3** 审批有效性比较，`policy_revision` 的一侧必须来自调用方 `RequestContext` | policy 变化后旧审批静默继续生效 | **M4 无法施加此禁令**，见表下说明；作为设计约束记录，到 M8 引入读回路径时才可执行 |
| **B3** `plan_hash` / `target_fingerprint` 不得落库 | 恢复时漂移检测退化成空操作 | 新增：篡改存储中的 plan 后，调用方重算的指纹必须不匹配并拒绝继续 |

**关于 S3 的更正（复审自查）**：初稿写「复用既有 `test_approval_binding.py` 的断言形状扩到 PostgreSQL 绑定」是**错的**。该文件测的是 `governance/verify_approval_binding()` 纯函数，用内存 fixture，与 TaskStore 无关；且 `record_approval` 在整个仓库里**只写不读**——`persistence/fake.py` 的 `_approvals` 是一个从未被查询的私有 list，`TaskStore` Protocol 也没有任何读回方法。**没有读路径，S3 就没有可施加禁令的对象。**

因此 M4 的处置是：

- 审批表照建（M4 交付物明列「审批记录的数据契约和存储结构」），但**不向 Protocol 追加读回方法**——它在 M8 之前没有任何生产消费方，追加等于为不存在的调用者写接口。
- 存储保真由 integration 测试**直接用 SQL 读表**验证，不经 Protocol。测试可以够到存储细节，生产代码不行；这样既证明了数据真的落对，又不产生一个无人调用的公开 API。
- **S3 作为设计约束写在此处**，M8 引入读回路径时必须同时落成它的承重测试。届时若发现比较写成了两侧读库，属回归，不属新增缺陷。

**此项已于 2026-09-03 由项目负责人批准，不再是开放判断**——见 §14.4。

### 8.5 `list_stale_leases`：stale recovery 的**发现**

追加到 `TaskStore` Protocol 的唯一新方法（§14.1 拍板）：

```python
async def list_stale_leases(self, *, limit: int) -> tuple[TaskRecord, ...]:
    """列出曾被租出、租约已过期、且未处于终态的任务。

    只回答"哪些任务可能需要被接管"，不接管、不调度、不判断是否应当恢复。
    """
```

**语义恰好三条**，缺一或多一都改变它的性质：

1. **曾被租出**（`fencing_token IS NOT NULL`）——与 §8.1 fencing 真值表同一锚点，不是"租约此刻是否 live"。
2. **租约已过期**（`lease_expires_at <= :now`，`now` 由注入 `Clock` 作为参数传入，见 §8.2）。
3. **未终态**——终态任务不应再被任何 worker 领走，与 `acquire_lease` 对终态返回 `None` 是同一条不变量。

**排序恒为 `(lease_expires_at, task_id)`**，两个键都必要：只按到期时间排序时，同一毫秒到期的多条记录顺序不定，分页会重复或漏项；`task_id` 作为次键使排序全序且稳定。

**明确不是的东西**（这些构成"收窄"的实质，否则它会长成一个 Worker 队列）：

| 不做 | 理由 |
| --- | --- |
| 不 claim、不加锁、不改任何状态 | 它是只读查询。接管仍走 `acquire_lease()`，并发 winner 仍由存储层裁决——发现与接管分离，才使"两个 worker 同时发现同一任务"退化成一次普通的租约竞争 |
| 不判断审批是否应恢复、不看 plan/policy | 那是 Runtime 与 `ApprovalGate` 的职责。存储层不得获得任何领域判断权 |
| 不做优先级、不做退避、不做重试计数 | 调度属 M5 的 Worker。此处多一个字段，M5 就会绕开 Runner 直接消费存储 |
| 不做游标分页 | `limit` 已足够；游标是队列语义的开端 |

**`limit` 必填、无默认值。** 无界查询在任务表增长后会一次拉回整表；写成必填参数使调用方无法"忘记"它。

**它不构成 E1**：只读、只触碰内部持久化，符合 ADR-007 D7 对内部持久化的界定。

---

## 9. 测试基建

### 9.1 不新增第五条命令

integration 测试放在 `tests/integration/`，由既有 `testpaths = ["tests"]` 自动收集。**无 DSN 时跳过，有 DSN 时执行。** CI 的 integration job 与 tests job 执行的是**同一条** `python -m pytest -q`，差别只在于多一个 PostgreSQL service 和一个环境变量。

因此：**ADR-008 的四条命令一字不改**，`test_docs_command_consistency.py` 不受影响，不产生第五条命令口径。

代价是跳过是静默的，对价是 §9.4 的元测试。

### 9.2 socket 放行：`allow_hosts` marker，按目录逐 item

`tests/integration/conftest.py`：

```python
def pytest_collection_modifyitems(config, items):
    """只给本目录的 item 加 allow_hosts marker。

    不用 socket_enabled fixture：它在 pytest_runtest_setup 里命中后直接 return，
    _resolve_allow_hosts() 永不执行，connect 完全不受保护（0.8.1 实证）。
    不用全局 --allow-hosts：它会使 hosts 对每个用例为真，disable_socket()
    对全套件永不执行。
    """
```

marker 只带 DSN 解析出的那一个 host。DSN 未设置时**不加 marker**（此时用例本就跳过）。

**三条安全测试**（`tests/security/`，`pytestmark = pytest.mark.security`）：

1. **目录外仍被拦**：`tests/integration/` 之外的用例，`socket.socket(AF_INET)` 与 `getaddrinfo` 仍抛 `SocketBlockedError`。这条与既有 `test_no_network.py` 互补：那边断言"默认被拦"，这边断言"引入 integration 后默认仍被拦"。
2. **integration 连非 DSN host 被拦**：在 integration 目录内连一个不在白名单的地址，必须抛 `SocketConnectBlockedError`。
3. **放行配置的窄度**：断言 `pyproject.toml` 的 `addopts` 中**不含** `--allow-hosts`，且仓库内不存在裸用 `socket_enabled` 的测试。这条是对「有人图省事全局放行」的直接封堵，形态照抄 CI 里 gitleaks 的 allowlist narrowness self-test。

### 9.3 DSN 的命名与读取

**变量名 `PYTEST_POSTGRES_DSN`，不带 `XIAOWEI_` 前缀。** 两条独立理由：

1. `tests/conftest.py::clean_xiaowei_env` 是 autouse，每个用例前删除全部 `XIAOWEI_*`——带前缀的变量在用例里根本读不到。
2. 更深的根因：`load_settings()` 对**任何**未知 `XIAOWEI_*` 变量 fail-fast。今天靠清理 fixture 侥幸不炸，但那意味着一个 harness 变量的存活依赖于另一个 fixture 的实现细节。一旦清理范围被收窄，CI 里每个读 `os.environ` 的配置用例都会炸。不带前缀从根上消除这条耦合。

**读取时机**：在 `tests/integration/conftest.py` 的 `pytest_configure` 里读入并存进 `config.stash`，其余 fixture 一律从 stash 取，**不再读 `os.environ`**。理由：`pytest_configure` 在任何用例与任何 fixture 之前执行，取值不受任何清理逻辑影响。这也正是 `pytest-socket` 自身存配置的做法。

**不加入 `.env.example`**：它不是应用配置，是测试 harness 配置。加进去会同时误导使用者并打破 `test_env_example_clean.py`。

### 9.4 gate 不可静默死亡

一条元测试：**`PYTEST_POSTGRES_DSN` 已设置时，`tests/integration/` 的 skip 数必须为 0。** 实现为一个 `pytest_terminal_summary` 或收集期断言均可，由 T5 定形。

没有这条，CI 里 DSN 拼错、service 没起来、迁移失败等情况全都表现为"全绿"，而 M4 的全部退出标准都建立在这些用例真的跑过之上。

### 9.5 契约套件的双绑定

现有 `tests/contract/test_task_store_contract.py` 开头写着「M3/M4 共用的 TaskStore 交互形状。M4 的 PostgreSQL 实现必须原样通过本文件」。

**初稿说这两个安全测试文件「可以原样重绑」，这是错的**（复审自查 P3）。`test_lease_fencing.py` 与 `test_terminal_protection.py` 位于 `tests/security/`，而 §9.2 的放行只对 `tests/integration/` 生效——在原目录里它们连不上数据库。把 `store` fixture 参数化成"内存 + PostgreSQL"更糟：那会让 `tests/security/` 里的每条用例都尝试连库，与 §1 判定标准 7 直接冲突。

因此落实方式是**抽取**，不是就地参数化：

- 行为用例抽成一个不含 fixture 定义的共享套件模块（如 `tests/suites/task_store.py`），只声明用例函数，`store` 由绑定方提供；
- **内存绑定**：`tests/contract/`（契约用例）与 `tests/security/`（fencing、terminal 用例，保留 `pytestmark = pytest.mark.security`）——不需要数据库，仍在默认路径跑，socket 仍被拦；
- **PostgreSQL 绑定**：`tests/integration/`，同样保留 `security` marker，使 `python -m pytest -m security -q` 覆盖它们（无 DSN 时跳过，与 §9.4 的元测试相容）；
- **元测试断言两个绑定收集到的用例名集合相等**——否则新增一条用例只落到一个实现上，而没有任何东西会报错。

这些既有用例本身写得实现无关（只断言 `second.fencing_token > first.fencing_token`，不断言具体取值），因此抽取是**纯搬运**：一行行为断言都不改。这是 M4 最有价值的既有资产，但取用它需要先付这次搬运的代价。

---

## 10. 任务拆分（文件级 TDD）

每个任务一个提交，可独立拒绝。

### T0：依赖与实证基线 —— **已完成**

新增三个依赖并 `uv lock`；落成 §5.3 前两项实证的可执行断言；确认 `deps-audit` 的既有导出路径覆盖新依赖。**不写任何业务代码。**

实际落点是 `tests/security/test_dependency_baseline.py`（7 条，全部 `security` marker）。它比"跑一次 mypy 探针"覆盖更宽，因为探针只证明**当下**通过，挡不住**以后**的放宽：

- 生产依赖名集合**恰为五项**。用集合相等表达，而不是 §3「明确不加」那张禁用清单——禁用清单挡不住清单外的新依赖。
- M5+ 的基础设施依赖不得从 `dev` 这条侧门进来。
- `sqlalchemy` / `alembic` 必须自带 `py.typed`（撤回即转红）。
- `asyncpg` 当前**没有** `py.typed`（上游补上即转红，见 §5.3.2）。
- `src/` 不得直接 import `asyncpg`（AST 扫描）。
- `[tool.mypy]` 的 `strict` 恒为真，且不得出现全局 `ignore_missing_imports` / `follow_imports`。

**TDD 反证（已执行，先红后绿）**：向 `[project].dependencies` 偷加 `redis` → 集合相等用例转红；向 `persistence/store.py` 加一行 `import asyncpg` → import 禁令用例转红，且 `mypy src` 同时报 `import-untyped`（证明该禁令确实在挡真实的类型检查退化）。两次还原后全绿。

### T1：抽出共享判定纯函数（零行为重构）—— **已完成**

新增 `persistence/decisions.py`；`InMemoryTaskStore` 改为消费它。导出六个纯函数：`lease_is_live`、`classify_transition`、`apply_transition`、`may_acquire_lease`、`may_renew_lease`、`context_matches_envelope`。时间一律由调用方以 `now` 传入——读时钟就是 I/O，会让"同一输入必得同一输出"不成立，而那正是两个实现能被同一份断言约束的前提。

判定规则的真源随之从 `fake.py` 的模块 docstring 迁到 `decisions.py`（拒绝顺序、fencing 闭合真值表）。`fake.py` 只剩"怎么存"：一把锁、两个字典、一个自增 token，代码量 97 行 → 23 行。

- **验收**：既有 `test_task_store_contract.py`、`test_lease_fencing.py`、`test_terminal_protection.py` **一行行为断言未改**，全绿；重构前后 `1236 passed` / `830 passed` 逐字相同。
- **TDD 反证（计划要求的三项，全部转红）**：打乱拒绝顺序（版本检查提到 fencing 之前）→ 1 红；fencing 锚点改回"租约此刻是否 live"→ 1 红；去掉终态优先 → 7 红。

**另外四项额外探针发现两个真实覆盖缺口**（T1 明文规定"反证若首轮全绿必须先补测试再继续"，故在本任务内闭合，不推迟）：

| 探针 | 首轮 | 处置 |
| --- | --- | --- |
| `may_acquire_lease` 去掉终态过滤 | 2 红 | 已承重 |
| `context_matches_envelope` 忽略 `environment_id` | 1 红 | 已承重 |
| `apply_transition` 的 `terminal_reason` 改成"有值才写" | **全绿** | 补 `test_terminal_reason_is_cleared_when_a_transition_omits_it`（§8.3 P6 预判的缺口，此处被实测确认） |
| `may_renew_lease` 只比 `owner` 不比 `fencing_token` | **全绿** | 补 `test_renew_with_a_wrong_token_is_refused`。**这一项计划中没有预判**：既有用例只覆盖"换个 owner 续租被拒"，没覆盖"同 owner、错 token"。owner 名常是主机名或角色名，进程重启后重名是常态——而那正是 fencing token 存在的理由 |

补测试后复跑这两项变异，均转红。

### T2：契约套件双绑定化 —— **已完成**

按 §9.5 重构为共享套件 + 绑定；此时只绑定 InMemory（PostgreSQL 绑定在 T5 接入）。

**落点**：`tests/suites/task_store.py`（不匹配 `test_*.py`，因此自身不被收集）持有 34 条行为用例，分三组 `CONTRACT_CASES` / `LEASE_FENCING_CASES` / `TERMINAL_PROTECTION_CASES`；三个既有文件退化为绑定，各自调用 `bind(globals(), <组>)`。marker 在收集期按模块读取、不写回函数对象，因此同一个函数在 `tests/security/` 带 `security`、在 `tests/contract/` 不带，互不影响。

**纯搬运的证据**：重构前后 `pytest --collect-only` 对这三个文件给出的 **48 个用例 ID 逐条相同**。

`test_terminal_protection.py` 只搬走两条**存储层**用例；终态集合、迁移表、`TaskOutcome` 终态约束这几条与存储无关，留在原地——绑到 PostgreSQL 上只会重复跑一遍同样的纯函数断言。

**抽取换来三条新的静默失效路径**，形态都是"看起来写了测试、实际一次没跑"，且全部现有用例照常全绿。`tests/contract/test_task_store_bindings.py` 逐条承重：

| 失效路径 | 断言 | 反证 |
| --- | --- | --- |
| 用例漏进分组 | 分组并集 == 套件里定义的全部 `test_*`，且分组互不重叠 | 加一条不进组的用例 → 1 红 |
| 绑定漏挂分组 / 局部覆盖 | 每一组必须被**同一批实现**绑定 | 只把一组改成 `postgres` → 1 红 |
| 元测试不知道新绑定 | AST 扫描 `tests/` 下所有 `bind(...)` 调用，与登记表比对 | 从登记表删一个绑定 → 3 红 |
| 绑定挂错分组 | 每个绑定暴露的套件用例恰为它登记的那一组 | `CONTRACT_CASES[:-1]` → 1 红 |

第三条用 AST 扫描而不是靠人记得登记，理由与 `test_module_layering.py::test_every_existing_package_is_registered` 相同。

**「两个绑定用例名集合相等」目前是平凡真**（每组只有 memory 一个绑定）。写成"逐个绑定对齐分组"而不是"两两比较"，是为了让失败信息直接指出哪个绑定少了哪条。**T5 接入 postgres 绑定后必须复查这两条确实会因缺绑定而转红**。

### T3：schema 与 Alembic migration —— **已完成（真实库部分此后已由 CI 证实）**

五张表 + fencing 序列 + 唯一索引 + CHECK 约束；`upgrade()` / `downgrade()`；空库与有数据两条 upgrade 路径。

**落点**：`persistence/schema.py`（活的 Core `Table` 定义，供 T4 构造语句）、`persistence/rows.py`（DTO ↔ 行的纯映射）、`alembic.ini` + `persistence/migrations/`（`env.py` + `versions/rev_0001_initial_task_store_schema.py`）。

**迁移不 import `schema.py`**。迁移是冻结的历史快照——回放这条 revision 必须建出**当初**那套表，引用活 schema 会让历史随代码一起漂移。因此两处必然重复，重复的对价是 `tests/contract/test_schema_matches_migration.py`：用 Alembic 的**离线模式**（`upgrade --sql`，不连数据库）拿到迁移真正会发出的 DDL，再把 `schema.py` 的每张表编译成同一方言的 DDL 逐表比较。**这条检查在默认测试路径上就能跑**，不必等到有 PostgreSQL 才发现两边不一致。

反证：迁移里删掉租约同置同清 CHECK → 1 红；少建一张表 → 2 红；`task_id` 改 `Uuid` → 2 红；`alembic.ini` 填上连接串 → 3 红。

**`rows.py` 分出来的理由与 `decisions.py` 相同**：映射写在执行 SQL 的方法里就只能靠真实数据库才能测，而"往返是否无损"根本不需要数据库来回答。`test_row_mapping.py` 的两组断言因此跑在默认路径上；T5 的 integration 跑同样的断言，只是中间多一次真实写入与读回——**这一层证明映射无损，那一层才证明数据库无损**。

**JSON 路径是契约层要求的，不是实现偏好**：`Contract` 全局 `strict=True`，python 校验模式不接受 `str→StrEnum` 与 ISO 字符串→`datetime`，而 JSON 校验模式接受（`contracts/base.py` 已实证）。因此 JSONB 读回的 `dict` 必须经 `model_validate_json`，不能直接 `model_validate`。

**T3 交付时仍未验证**：两条 upgrade 路径（空库 / 有数据）在真实 PostgreSQL 上的执行、`timestamptz` 的实际精度、JSONB 的键序与数值表示。全部推到 T5，因为本机无 PostgreSQL、无容器运行时。此后这些路径已在 PR #6 的 `integration` job 中真实执行并通过。

**`task_id` 列必须是 `text`，不是 `uuid`**（复审自查 P4）。`TaskRecord.task_id` 是 `StrictStr`，而 `uuid` 列读回的是 `UUID` 对象，`StrictStr` 会直接拒绝——这条在写 DDL 时的直觉恰好是错的，因此写死在计划里。同理，所有映射到 `StrictStr` 的列一律 `text`。

**两组独立的 round-trip 测试**（复审自查 P5）：

1. **JSONB 表**：DTO → JSONB → DTO 与原对象相等（`task_plans`、`task_evidence`、`task_approvals`、`task_audit_events`）。
2. **`tasks` 表**：`TaskRecord` → 列展开 → `TaskRecord` 与原对象相等。这是**另一组风险**，不被第 1 组覆盖：`AwareDatetime` 经 `timestamptz` 往返的时区与微秒精度、`Sha256Hex` 的长度约束、`terminal_reason` 的 `None` 与 SQL `NULL` 的对应、以及三个租约字段的同置同清不变量。

`task_approvals` 目前没有 Protocol 读回路径（§8.4 S3），其 round-trip 由 integration 测试**直接用 SQL 读表**验证。

### T4：`PostgresTaskStore` —— **已完成（真实库行为此后已由 CI 证实）**

engine/session 工厂；注入 `Clock`；**六个方法**（既有五个 + §8.5 的 `list_stale_leases`）。此处的「五 / 六」只数任务生命周期方法，不含 `record_approval`；**Protocol 的方法总数在 T4 结束时是七个**（`tests/contract/test_protocol_conformance.py` 的哨兵即为 7），T10 追加 `record_audit_event` 后为八个（§14.5）。CAS 与租约按 §8.3 形状；入参 DTO 在开事务前构造。

**T4 当时写下的「SQL 谓词只被允许更宽」这条规则后来被证伪**（§10 T13）：有 `LIMIT` 时更宽的谓词会让名额被将要丢弃的行吃掉，谓词必须与 `is_stale_lease` 逐条等价。

`list_stale_leases` 同时要在 `InMemoryTaskStore` 上实现，并进入 §9.5 的共享套件——两个绑定的用例名集合相等这条元测试，正是用来保证它不会只落到一个实现上。追加 Protocol 方法还会触及 `persistence/__init__.py` 的导出与 `tests/contract/test_protocol_conformance.py` 的一致性锚点，两处都在本任务内更新。

**已执行的反证**：

| 变异 | 结果 |
| --- | --- |
| 去掉 `is_stale_lease` 的"未终态"过滤 | 1 红 |
| 把 `list_stale_leases` 的排序键第二项改成常量 | **首轮全绿 → 见下** |
| 把 `list_stale_leases` 从 `InMemoryTaskStore` 删掉 | 1 红（Protocol 方法清单派生生效） |
| `lease_is_live` 的 `>` 改成 `>=` | 1 红（补测试后） |

**排序决胜位的反证首轮全绿，是一个真实的覆盖缺口，且暴露了一条更一般的教训**：`task_id` 由 `uuid4` 生成，插入顺序有一半概率**恰好已是**字典序，因此存储层用例对这个变异只有 50% 的检出率。**一个一半概率转红的反证等于没有反证。** 处置是把这类性质下沉到纯函数层用构造好的输入确定性钉死（`tests/contract/test_lease_decisions.py`），存储层保留契约断言。这不是重复——两层的输入可控性不同。

顺带补上的 `lease_is_live` 边界方向（`>` 而非 `>=`，即"恰好等于过期时刻已算过期"）此前也无任何断言。

**`test_protocol_conformance.py` 的方法清单原本是写死的四项**，Protocol 长出 `list_stale_leases` 时它不会失败，只会静默少检一个。已改为从 Protocol 派生，并对**两个实现**都跑——只校 fake 会让 PostgreSQL 的签名漂移无人发现。

**T4 交付时仍未验证（需要真实 PostgreSQL）**：`ON CONFLICT DO NOTHING` 的并发幂等创建、`FOR UPDATE` 的实际串行化、序列的单调性，全部推到 T6/CI。S2 禁令的原定反证后来被证伪：把 `UPDATE` 的 `WHERE version` 改成事务内读到的值，即使有真实数据库也不会让并发用例转红；最终改由 T8 的 AST 机械检查承重。**T4 交付的是代码与签名一致性，不是运行时行为证据。**

### T5：integration 基建与 PostgreSQL 绑定 —— **已完成（绑定行为此后已由 CI 证实）**

`tests/integration/` + conftest（§9.2 marker、§9.3 stash、§9.4 元测试）；把 T2 的套件重绑到 PostgreSQL；补 §9.2 的三条安全测试。

**socket 放行的三条断言已实证，不只是写下来。** 用一个指向不存在库的 DSN 跑真实 pytest（`postgresql+asyncpg://postgres@127.0.0.1:5432/nonexistent`），得到：

| 断言 | 实测 |
| --- | --- |
| DSN 生效、marker 按目录加上 | integration 用例报 `ConnectionRefusedError`（**不是** `SocketBlockedError`）——放行确实生效，且连接被真的尝试了 |
| 目录外仍被拦 | 同一次运行里 `test_no_network.py` 与三条边界用例全绿 |
| integration 内连非 DSN host 被拦 | 连 `192.0.2.1`（RFC 5737 TEST-NET-1）抛 `SocketConnectBlockedError` |
| DSN 已设置时 integration skip 数为 0 | 同一次运行 **0 skipped** |

这同时证实了 §5.2 那条被打回的设计现在是对的：`pytest_configure` 在子目录 conftest 里确实会触发（historic hook），`allow_hosts` marker 只作用于本目录 item，`disable_socket()` 对其余用例照常执行。

**schema 由 Alembic 建，不由 `create_all` 建**：用 `create_all` 会让集成测试跑在一套没人迁移过的表上，迁移写错了照样全绿，而生产只会拿到迁移建出来的那套。

**隔离用 TRUNCATE，不用事务回滚**：M4 有需要**真实提交**的并发场景，把整条用例包进未提交事务会让"另一个连接看得到吗"这类断言全部失真。

**T2 要求的复查已执行**，三项均转红：删掉一个 postgres 绑定的登记 → 2 红；整个删掉一个 postgres 绑定模块 → 2 红；postgres 绑定挂错分组 → 1 红。「两个绑定用例名集合相等」自此不再是平凡真。

`unexpected_integration_skips` 写成纯函数并单独测试（`tests/contract/test_integration_gate.py`），因此这条 gate 不必靠"某次 CI 恰好出问题"来验证。

**T5 交付时未验证**：全部 PostgreSQL 绑定用例与迁移路径用例当时**一次都没跑过**——本机无 PostgreSQL、无容器运行时。T5 交付的是基建与放行窄度的证据，**不是** PostgreSQL 行为的证据。
**此状态已由 T10–T13 期间的 CI 解除**：`integration` 已在 PR #6 上真实运行并全绿，逐条分布与结果见[验收报告](../handoff/M4-acceptance-report.md) §4 与 §2.7。

### T6：并发与崩溃恢复故障注入 —— **已完成（本机无法运行，CI 已证实）**

**必须用多个真实数据库连接**，不得用共用连接的 `asyncio.gather` 冒充并发：

- N 个连接同时 CAS 同一版本 → 恰一个 `applied=True`，其余 `VERSION_MISMATCH` 且 winner 一致；
- **N 个连接同时用同一幂等键创建 → 恰产生一个任务事实**，全部调用返回同一 `task_id`，无一抛出数据库唯一键冲突（§1 判定标准 3；复审自查 P2）。这是 M4 退出标准的第一句，此前**无任何覆盖**——既有用例只测串行重复创建；
- 租约过期后被另一 worker 抢占，旧 token 写入被拒；
- 终态后到事件被拒，终态不变；
- 事务中途断连 → 无中间态（§1 判定标准 4）。

- **TDD 反证**：去掉唯一键冲突的捕获分支，并发创建用例必须转红（而不是变成偶发失败——因此该用例需要足够的并发度使冲突必然发生）。并发度取 8，理由同上。

**崩溃注入用 `pg_terminate_backend` 真的杀后端进程**，而不是客户端 rollback：后者证明的是"我们记得回滚"，前者证明的是"我们不回滚也不会留下中间态"。生产里进程是被 OOM killer 和 pod 驱逐杀掉的，没人记得回滚。

**T6 交付时**：本机无 PostgreSQL、无容器运行时，这 6 条用例一次都没跑过，判定标准 2 / 3 / 4 的证据要到 CI 的 integration job 才产生。
**此后已产生**：其中崩溃注入那条在 `integration` 首次运行时暴露出 `begin()` 顺序错误（§10 T12），修复后连同其余 5 条一并通过。

### T7：`PlanStore` / `EvidenceLedger` 的 PostgreSQL adapter —— **已完成（行为此后已由 CI 证实）**

**实现既有 port，不新增 port。** 冲突语义与内存实现一致：计划内容不同即 `PlanConflictError`；同 `evidence_id` 内容不同即 `EvidenceConflictError`；`load` 按写入顺序返回，无证据时返回空元组而非抛异常。同样走双绑定套件。

补 B3 承重测试：篡改存储中的 plan 后，调用方**当下重算**的 `plan_hash` 必须不匹配并拒绝继续。

### T8：TDD 反证收口 —— **已完成**

DEVELOPMENT_PLAN §7 M4 明文要求的三项，逐个撤掉承重保护，全部转红、还原全绿：

| 撤掉的保护 | 转红用例数 | 覆盖面 |
| --- | --- | --- |
| 终态保护（`classify_transition` 的第一段） | 7 | `test_terminal_protection.py` 全部 + eval `test_a29` |
| CAS（版本比较改成两侧同源，即空洞检查本身） | 1 | `test_cas_failure_returns_the_storage_winner` |
| fencing（`_check_fencing` 整段） | 7 | `test_lease_fencing.py` 六条 + `test_a_terminal_task_is_never_stale` |

**S2 禁令是一个例外，必须说清楚，不能当作"已验证"混过去。**

把 `PostgresTaskStore.transition` 的 `UPDATE ... WHERE version` 改成事务内读到的值，**没有任何用例转红**——而且**即使有真实数据库也不会**。根因是那里有两道独立的版本闸：`classify_transition`（Python 侧，两个实现共用）和 SQL 侧的 `WHERE version`。在 `FOR UPDATE` 之下第一道先命中，第二道永远走不到被单独检验的路径上。它是**纵深防御**，而纵深防御按定义没有单独的行为反证。

计划 §8.4 原本把 S2 的承重测试写成"并发用例转红"，**这个预期是错的**。正确的处置是给它一个机械检查：`tests/security/test_vacuous_check_ban.py` 用 AST 直接断言 `transition` 里出现 `command.expected_version`、不出现 `current.version`，并配一条反例确认这两条有分辨力。反证：把 `WHERE version` 改成读到的值 → 2 红。

没有这个文件，第二道闸可以被悄悄改成恒真而全部测试照常全绿；等到某天有人重构掉第一道闸，两道闸会一起消失。

### T9：CI job 与文档收口 —— **已完成（service 可达性此后已由 CI 证实）**

按 §11 清单改 `ci.yml` 与 `test_workflow_policy.py`；更新 `AGENT_HANDOFF.md`、`README.md`、`ARCHITECTURE.md`。

§11 的六项同步全部完成，另新增四条断言（`services` 闭集、镜像版本钉死、env 取值无凭证、integration 不引入第五条命令）。

**T9 交付时镜像用版本 tag 而不是 digest，这是一处对计划的偏离，理由是不编造未经核对的事实**：digest 只能联网解析，而当时的改动在离线环境完成。写一个没核对过的 digest 比用 `postgres:16.10` 更糟——前者看起来更严格，实际指向未知内容。当时记为需联网的待办，并配了一条断言先挡住 `latest`。
**此偏离已由 §10 T13 收口**：CI 绿灯后从 run `33829385450` 的 `Initialize containers` 输出取回 digest 钉死，并把那条断言从"带冒号或带 `@sha256`"（一个裸 tag 也能通过）收紧为完整的 `name:tag@sha256:<64>` 形状。

**§5.3 第 3 项（service container 从 job 容器经 `127.0.0.1:5432` 可达）在 T9 交付时仍未验证**——它只能由首次 CI 运行产生证据。此后已由 PR #6 的首次 `integration` run 与修复后的全绿 run 证实。

---

### T10：Codex 首轮验收打回的三项 —— **已完成**

受审 SHA `797a210` 被 Codex 打回，三条阻断项 + 一条非阻断。逐条的根因、影响与处置：

**1. `alembic.ini` 依赖 locale（P1）。** Alembic 用 `ConfigParser.read(..., encoding="locale")` 读它（`alembic/util/compat.py`），`"locale"` 由**进程环境**解析，Alembic 没有任何选项能覆盖它。于是 ini 里的中文注释在 `LC_ALL=C`（macOS 解析为 US-ASCII）的机器上直接 `UnicodeDecodeError`。

影响比"测试红"更大：**同一条读取路径也是 `alembic upgrade head` 的路径**，一个 locale 不是 UTF-8 的运维环境根本跑不了迁移。CI 在 UTF-8 下永远看不到这个问题。

处置：ini 改为 ASCII-only，散文移进 `env.py`（`.py` 按 PEP 263 默认 UTF-8，与 locale 无关）。同类排查覆盖了全部会被按 locale 读取的文件——仓库里只有 `alembic.ini` 一个；`script.py.mako` 由 Mako 读且其 lexer 默认 UTF-8（已实证 `LC_ALL=C` 下 `alembic revision` 正常），`pyproject.toml` / `.gitleaks.toml` / 工作流 YAML 的读取方都规定了 UTF-8，`src` 与 `tests` 里所有 `read_text` / `write_text` 均已显式带 `encoding=`。新增 `tests/contract/test_config_encoding.py` 按目录扫描而不是按文件名硬编码，将来新增 `setup.cfg` 之类同样受管。

顺带发现并删除了一条**恒绿**的用例：`test_schema_module_declares_no_credentials` 遍历"以 `sqlalchemy.url` 开头的行"，而 ini 里根本没有那个键，循环体一次都没执行——把 DSN 写进**别的**键照样通过。替换成扫描全部选项值，并断言确实扫过东西。

**2. 审计事件只有表，没有实现（P1）。** `task_audit_events` 建了表，但 Protocol、两个实现、任何用例、任何写入方全都不存在，而 DEVELOPMENT_PLAN §7 M4 的交付物与 ARCHITECTURE §7.3 都明文列了「审计事件」。**这是漏读交付物**，不是"integration 没跑"的副产品——是本计划第四次在同一处栽跟头（见 §6.3 P2）。

沿同一条代码路径排查发现同类问题两处：`record_approval` 虽有 Protocol 与两个实现，但**没有任何用例断言它真的落了盘**；且两个实现的编号模型已经分叉——PostgreSQL 按 `(task_id, seq)` 编号，内存实现只往一个扁平 `list` 里 append，连 seq 的概念都没有。一并修掉。

处置见 §14.5。新增 9 条共享套件用例（自动同时绑到内存与 PostgreSQL）、1 个 AST 守卫模块、9 条 integration 用例，并把 JSONB 载荷的覆盖完整性改为**由 schema 决定**：从 `ALL_TABLES` 扫出全部 JSONB 列与登记表比对，漏一列即红——这正是 `task_audit_events.event` 当初漏掉的地方。

**3. `integration` job 从未运行（P1）。** 根因不是代码：工作流只在 `push: main` / `pull_request: main` 触发，而该分支没有 PR。**不改触发条件**——给所有分支加 `push` 触发会让每个 PR 跑两遍 CI，且那不是这条门槛缺的东西。处置是开 PR，让 `pull_request` 事件在 exact HEAD 上跑满七个 job。

**4. `tests/conftest.py` 推荐了被禁的 `socket_enabled`（P2）。** 禁令此前只用 AST 扫代码，散文不受约束——docstring 里既没有 `ast.Name` 也没有 `ast.Attribute`，于是漂了。处置不是改掉那段话就完事，而是把禁令扩到原文扫描，只豁免解释禁令本身的两个文件，并加一条反空洞断言防止豁免名单里的文件被删后上一条平凡通过。

**变异反证**：十项，全部先红后绿。其中一项（"守卫挪到分配之后"）**首轮为绿**——没有 `task_id` 就没有桶可污染，任何"先分配再校验"的实现从 Protocol 上都观察不到，那条用例**不可能转红**。按 §10 T8 对 S2 的同一处置：删掉不可证伪的用例，改用 AST 检查（`tests/security/test_audit_ledger_guards.py`，两个实现各一条），落盘层面的证据放进 integration 直接读表。

### T11：`integration` gate 的**触发条件**本身没有承重 —— **已完成**

复审第二轮自查发现，不由 Codex 打回。

**根因**：`integration` job 能否证明任何东西，取决于 `PYTEST_POSTGRES_DSN` 这个**名字**在两处对得上——`tests/integration/conftest.py` 的 `DSN_ENV_VAR` 常量，和 `.github/workflows/ci.yml` 的 env 键。这是同一个字符串的两份独立拷贝，之前没有任何东西把它们钉在一起。

**失效路径**：重命名 conftest 里的常量 → CI 读不到 DSN → 104 条 integration 用例全部跳过 → `unexpected_integration_skips` 因 `dsn` 为 `None` 老老实实返回空 → **七个 job 全绿，而一条 integration 用例都没跑**。M4 的验收硬门槛就此变成一句空话。

**为什么既有护栏挡不住**：
- `ci.yml` 的整文件 SHA-256 门槛（`test_workflow_policy.py`）挡的是工作流被改，而这条路径改的是 conftest，工作流一个字节没动；
- env 白名单（`test_no_env_block_outside_declared_allowlist`）是集合包含判定，只挡「多出来的东西」，挡不住改名或删除——这正是 AGENT_HANDOFF 里已经写明的白名单局限；
- 「DSN 已设置时不得有跳过」这条 gate（`unexpected_integration_skips`）本身是对的，但它的触发条件恰好就是被破坏的那个东西，**破坏它等于关掉它**。

**处置**：在 `tests/contract/test_integration_gate.py` 追加四条，把两侧钉在一起，全部在默认路径上跑（不需要 PostgreSQL）：

| 用例 | 挡住的失效 |
| --- | --- |
| `test_the_workflow_declares_the_exact_env_var_the_gate_reads` | 两侧改名（任一侧） |
| `test_the_env_name_binding_is_discriminating` | 反空洞：确认上一条换成别的名字确实会失败 |
| `test_the_workflow_dsn_points_at_the_declared_service_port` | DSN 的 host/port 与 service 发布端口对不上；顺带钉死 host 是回环地址（`allow_hosts` 只放行 DSN 解析出的这一个 host） |
| `test_the_integration_job_actually_runs_the_suite` | env 与端口都对，但没有一步真的跑 pytest。与 `test_workflow_policy.py::test_the_integration_job_runs_no_extra_command` 互补：那条管「不多跑」，这条管「确实跑了」 |

**不改 `ci.yml`**：本任务只加测试，因此 `_WORKFLOW_SHA256` 不变，不触发那条需要人工审查的门槛。

**变异反证**：四项，全部先红后绿。

| 变异 | 转红 |
| --- | --- |
| conftest 重命名 `DSN_ENV_VAR` | 2 |
| `ci.yml` 改掉 env 名 | 4（含整文件 SHA 门槛） |
| service 端口映射改成 `55432:5432` | 2 |
| 删掉 `integration` job 的 pytest 步骤 | 4 |

### T12：`integration` 首次真实运行暴露的两类缺陷 —— **已完成**

PR #6 的 CI run [`33828598775`](https://github.com/shixian66/xiaowei-agent/actions/runs/33828598775) 是 `integration` job 的**第一次真实运行**：`4 failed, 1422 passed`，**0 skipped**。0 skip 说明 §10 T11 的 gate 生效——104 条用例确实全部跑了。四条失败分属两类根因，两类的**共同前提**是同一件事：integration 用例在本机永远是 skipped，而 ADR-008 的 `mypy src` 不覆盖 `tests`，因此这一整类代码此前**既没有静态检查也没有运行时检查**。

**A. 伪造的枚举成员（3 条）。** 用例写了 `StageOutcome.DENIED` 与 `StageOutcome.ERROR`，而该闭集只有 `OK / REJECTED / FAILED / SKIPPED`。修正为 `REJECTED` 与 `FAILED`（与 `runners/deterministic.py` 在 ADMISSION 拒绝、GATEWAY 出错时的用法一致），并同步 `row["outcome"] == "denied"` 这条字面量断言。

处置不止于改这三行：新增 `tests/contract/test_contract_enum_references.py`，按 AST 扫 `src` 与 `tests` 全部 `.py`，断言每一处 `<Enum>.<NAME>` 引用的成员真实存在。**枚举清单从 `xiaowei_agent.contracts` 派生而不是硬编码**，新增枚举自动纳入——理由与 `test_row_mapping.py` 从 `ALL_TABLES` 扫 JSONB 列相同。判定写成纯函数，因此能在不往仓库里真放一处错误引用的前提下测它自己。反空洞断言**按树分别计数**：只扫到 `src` 同样能让主断言全绿，而出问题的那一半全在 `tests` 里。

**B. `begin()` 排在第一个 `execute()` 之后（1 条）。** `test_a_killed_backend_leaves_no_intermediate_state` 先 `execute("SELECT pg_backend_pid()")` 再 `victim.begin()`。SQLAlchemy 2.0 的 connection 在首次 `execute` 时 **autobegin**，之后再调 `begin()` 必然抛 `InvalidRequestError`。**这条用例从写出来那天起就不可能跑通**，而它正是判定标准 4 的唯一证据来源。处置是把 `begin()` 移到取 pid 之前。

同类排查：全仓库 `tests/integration/` 里其余 `.begin()` 全部作用在 engine 上（`clean_database.begin()`），不存在同一形状。**B 类不加机械检查**——它现在有真实的行为反证（integration 每个 PR 都会跑），为一个已经被真实运行覆盖的问题再造一条脆弱的 AST 规则，是把护栏堆在已经承重的地方。

**变异反证**：两项，全部先红后绿。

| 变异 | 转红 |
| --- | --- |
| 放回真实出过事的 `StageOutcome.DENIED` | 1 |
| 枚举扫描范围缩回只有 `src`（即漏掉出问题的那一半） | 1 |

### T13：`list_stale_leases` 先 `LIMIT` 后过滤，与一轮文档事实漂移 —— **已完成**

Codex 第二轮复审打回一条 P1 + 一条文档项。

**1. 先 `LIMIT` 后过滤会漏报（P1）。** `PostgresTaskStore.list_stale_leases` 的 SQL 谓词只有「曾被租出 + 已过期」，把「未终态」留给 Python 侧的 `is_stale_lease`，依据是 §10 T4 写下的那条规则——「SQL 谓词只被允许**更宽**」。

**那条规则在有 `LIMIT` 的前提下不成立。** 更宽的谓词会让 `LIMIT` 被将要被丢弃的行吃掉。

**影响比「偶尔漏几条」严重得多**：`apply_transition` **有意不清租约字段**（清空会重开 fencing 的缺口，见 `is_stale_lease` 的 docstring），因此**每一个正常结束的任务都永久命中那个更宽的谓词**，并且按 `lease_expires_at` 升序**排在最前**。终态任务只增不减，于是 `list_stale_leases` 的返回量会随系统运行**单调衰减到零**——这是长期运行的稳态，不是边界情况。而内存实现是「先过滤、再排序、再截断」，**两个实现已经分叉**，正是本里程碑抽共享纯函数要消除的那一类。

处置是让谓词与 `is_stale_lease` **逐条等价**，不是「多取一些再截断」——多取多少才够没有答案，终态任务的数量没有上界。查询抽成模块级的 `stale_lease_statement(*, now, limit)`，因此能离线编译、离线检验。三处细节：

- **终态集合从 `TERMINAL_STATUSES` 派生**，不在查询里重列：新增终态时查询自动跟上；
- 租约条件写成 `NOT (owner IS NOT NULL AND expires IS NOT NULL AND expires > now)` 而不是简写的 `expires <= now`——后者在 `expires IS NULL` 时求值为 NULL 而非 TRUE，会**收窄**谓词，那正是被禁的方向；
- Python 侧的 `is_stale_lease` 过滤**保留**：它是两个实现共用的权威判定，等价之后在正常数据上是空操作，但 SQL 一旦漂移它仍是兜底。

**2. 文档事实漂移。** 验收报告写着「计划 V1.4」（实际 V1.5）、「HEAD = `797a210`」、「T10 尚未落成提交」、「PR 尚未开」；`AGENT_HANDOFF.md` 的「下一步：M4…开工前先写详细计划」与「`tests/integration/`、数据库迁移仍不存在」也是旧口径；README 同处两条同类。

根因不是笔误，是**只改了正在编辑的那一处，没有扫描同一事实的其余副本**。逐处修正之外，新增 `tests/contract/test_doc_fact_binding.py` 把「报告引用的计划版本」与「计划标题里的版本」钉在一起——这一类**能机械判定**的事实不该再靠人记得同步。散文是否过期没法自动判，那部分仍靠复审。

**3. 顺带钉死 service 镜像 digest。** 计划一直写着「首次 CI 绿灯后钉 digest」，绿灯已在 run `33829385450` 拿到，digest 取自该 run 的 `Initialize containers` 步骤输出（`sha256:21f6013…c3c1`），**不是编的**。同时把那条护栏收紧：`test_service_images_are_version_pinned` 原本只要求「带冒号或带 `@sha256`」，一个裸 tag 也能通过；改名为 `test_service_images_are_digest_pinned` 并按真正的不变量断言完整的 `name:tag@sha256:<64>` 形状。`_WORKFLOW_SHA256` 已同步更新——那条门槛的设计目的就是强制人工审查本次 `ci.yml` 改动。

**变异反证**：六项，全部先红后绿。

| 变异 | 转红 |
| --- | --- |
| 谓词退回原来的「更宽」写法（即原 bug） | 3 |
| 终态集合漏掉一个成员（`canceled`） | 2 |
| **内存实现**改成同样的「先截断再过滤」 | 1 |
| 报告引用的计划版本与计划头部不一致 | 1 |
| service 镜像退回裸版本 tag | 3 |
| digest 改一个字符 | 2 |

第三项只打红**一条**——就是新加的 `test_a_terminal_task_cannot_crowd_out_a_stale_one`。这同时说明：新用例精确命中这个 bug 形状，而**此前没有任何用例能抓到它**，这正是 PostgreSQL 实现能带着它通过前四轮审查的原因。

## 11. CI 变更清单

新增一个 `integration` job：PostgreSQL service container（`POSTGRES_HOST_AUTH_METHOD=trust`，无凭证）、`PYTEST_POSTGRES_DSN` 环境变量、执行 `python -m pytest -q`。

**该 job 是 M4 的必需 gate（§14.2 拍板），但必须准确表述其性质**：分支保护当前不可用（private + GitHub Free，API 实证 403），因此"必需"**不是 GitHub 强制的 required status check**，而是 M4 审查与验收的硬门槛——`integration` 未绿，M4 不通过。具备分支保护条件后再把它登记为 required status check。**不得把它写成"已强制"**：那会重复 M1 退出标准修订时已被显式记录的那类误述。

**`tests/security/test_workflow_policy.py` 需同步更新的项**（S4；漏任何一项都会红）：

| 项 | 当前 | 变更后 |
| --- | --- | --- |
| `_WORKFLOW_SHA256` | 固定值 | 重算 |
| `_EXPECTED_JOBS` | 六项 | 加 `integration`（**七项**） |
| `test_job_set_is_exactly_the_approved_six` | 函数名写死"six" | 改名 |
| `test_uses_occurrence_counts_are_exact` | checkout = 6，setup-uv = 5 | 7 / 6 |
| `test_single_line_run_commands_match_exactly` | `uv sync` ×5，`python -m pytest -q` ×1 | ×6 / ×2 |
| `test_each_checkout_step_binds_persist_credentials` | `steps == 6` | 7 |

**需要新增的断言**（当前不存在，Codex 提到的两项）：

1. **`services:` 闭集**：只允许 `integration` job 有 services，镜像必须钉到 digest，且只允许 `POSTGRES_HOST_AUTH_METHOD=trust` 这一项配置。没有它，日后加一个带凭证的 service 只有整文件 SHA 会拦——而合法改 workflow 时那个常量本来就要更新，等于没拦。
2. **`env:` 键闭集**：当前 `secret-scan` 有两个 env 键，加上 `integration` 的 DSN 共三个。断言键集恰为这三个，且**取值中不含任何密码字段**。

`permissions: contents: read`、`persist-credentials: false`、无 `secrets:`、无 `continue-on-error` / `if:` 全部保持不变。**CI 的 E1 调用次数仍恒为 0。**

---

## 12. 验证命令、回滚与 PR 边界

### 12.1 验证命令

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

四条，逐字不变。触及 `governance/`、`planning/`、`tools/` 的变更须全量跑安全 gate——M4 主要动 `persistence/`，但 T1 的重构与 T9 的 workflow 变更会触及安全测试，因此**每个任务都跑全部四条**。

本地跑 integration 需先起一个隔离 PostgreSQL 并设置 `PYTEST_POSTGRES_DSN`；不设置时 integration 跳过，四条命令仍应全绿。

### 12.2 回滚

M4 全部变更集中在 `persistence/`、`tests/` 与 CI 配置，不触及既有执行链。回滚方式是撤销对应提交；`InMemoryTaskStore` 全程保留且仍是默认实现，因此回滚不会让任何既有闭环失效。数据库迁移提供 `downgrade()`。

### 12.3 PR 边界

一个 PR 只承载一个可独立拒绝的交付物；一个 PR 不跨越两个里程碑。

**Git 卫生**：不使用 `git add -A`；只暂存本次任务明确涉及的文件。当前工作树另有两个未跟踪文件（`docs/plans/development-route-v3-proposal.md`、`docs/plans/legacy-capability-migration-matrix.md`），**不属于 M4 范围，不得暂存、不得修改、不得删除**。

---

## 13. 非目标

M4 明确不做：

- API、CLI、Worker、Docker Compose（M5）。**仓库不新增 `docker-compose.yml`，不新增任何 Compose 文档流程**（§14.3 拍板）。README 只写 `PYTEST_POSTGRES_DSN` 契约与"隔离 PostgreSQL"的要求，不绑定本地起法——单容器或本机已有实例均可。
- 任何调度能力。`list_stale_leases` 只做发现，不 claim、不排优先级、不重试计数（§8.5）。
- 任何 E1 操作。内部持久化不构成 E1，但也不因此获得任何别的基础设施许可。
- 真实 StarRocks、Prometheus、资产系统或任何模型 API 调用。
- ADR-005 的审批语义（主体、渠道、有效期、拒绝/过期/冲突）。M4 只按 M2 已冻结的 `ApprovalRequest` 字段建表，不预设主体模型、渠道枚举或过期策略——不把尚无决策的争议藏进 schema 默认值。
- Redis、pgvector、消息队列、LangGraph、Multi-Agent。
- 证据与大产物的保留周期决策（M6b 前）。

---

## 14. 已拍板事项

四项均由项目负责人于 **2026-09-03** 拍板，本节记录结论与理由。§14.1–§14.3 在 V1 定稿时拍板，§14.4 在 Codex 复审通过时拍板。**本节不再是开放决策**；计划中无未决项。

### 14.1 `stale recovery` 的发现方法：**M4 内实现，形状收窄**

**结论**：不推迟。在 `TaskStore` 追加只读方法 `list_stale_leases(*, limit: int) -> tuple[TaskRecord, ...]`。

**理由**（负责人原话要点）：`stale recovery` 已在 M4 交付物里明列；只验证 `acquire_lease` 能接管一个已知 `task_id`，不足以证明"恢复"——那只证明了接管。

**收窄边界**：只返回"曾被租出、租约已过期、未终态"的任务，按 `(lease_expires_at, task_id)` 稳定排序；不 claim、不调度、不判断审批是否应恢复。接管仍走现有 `acquire_lease()`，并发 winner 仍由存储层裁决。完整语义与非目标见 §8.5，判定标准见 §1 第 8 条。

**这是对 M2 已冻结 Protocol 的追加，不是修改**，循 M3 新增 `PlanStore` 的先例（§2 取向 5）。落点在 T4，两个实现同时提供并进入共享套件。

### 14.2 `integration` job：**M4 必需 gate**

**结论**：设为必需。**但口径必须准确**——分支保护不可用，所以它是 M4 审查与验收的硬门槛（未绿即不通过），**不是 GitHub 强制的 required status check**。具备分支保护条件后再登记为 required。CI 仍只执行 `python -m pytest -q`，不新增第五条命令。详见 §11。

### 14.3 本地 PostgreSQL 起法：**不把 Compose 带进 M4**

**结论**：README 只写 `PYTEST_POSTGRES_DSN` 契约和"隔离 PostgreSQL"要求；本地可用单容器或本机已有实例。**仓库不新增 `docker-compose.yml`，不新增 Compose 文档流程**——Compose 留给 M5，其基础设施许可按 ADR-007 D8 属 M5 时点。见 §13。

### 14.4 审批读回：**M4 不追加 Protocol 方法**

**结论**：批准 §8.4 的处置。M4 建审批表、落审批记录，但**不向 `TaskStore` Protocol 追加任何审批读回方法**；存储保真由 integration 测试直接用 SQL 读表验证；审批读回的消费路径与 S3 禁令的承重测试一并留到 M8。

**理由**：`record_approval` 在整个仓库里只写不读，M8 之前没有任何生产消费方。为不存在的调用者追加公开方法，等于把一个未经使用的接口形状提前冻结进 M2 已验收的契约。

**代价与到期条件**：M4 结束时，"审批记录能被正确读回"只有测试级证据，没有生产路径证据。**M8 引入读回路径时，必须同时落成 S3 的承重测试**（`policy_revision` 的一侧来自调用方 `RequestContext`）；届时若发现比较写成了两侧读库，属回归，不属新增缺陷。

**此项与 §14.1 的方向差异是刻意的**：`list_stale_leases` 进 Protocol，是因为 stale recovery 是 M4 自己的交付物且 M4 内就有消费方（T4 用例）；审批读回不进，是因为它的消费方在 M8。判据是"本里程碑内是否存在消费方"，不是"这个能力将来是否需要"。

---

### 14.5 审计事件：**M4 交付存储能力，生产者接线归 M5**

`record_audit_event(*, event) -> int` 进 Protocol，两个实现都落地。返回的是本次写入分配到的 `seq`——**这不是"读回"**（§14.4 拍板不加读回 Protocol 方法仍然有效），是本次写入自己的回执，不查询任何既存记录。加它的理由是：不返回序号，两个实现就可以对同一次写入给出完全不同的答案而没有任何断言能发现，而 M4 之前正是这个状态。`record_approval` 一并改成返回 `seq`，理由同上。

**不在 M4 接生产者**：`TraceSink.emit` 是同步的，`record_audit_event` 是异步的。把两者接起来要改 M2 已定的 `TraceSink` 形状，波及整条 Runner 调用链——那是一次架构改动，不是一次接线。M4 交付存储能力（Protocol + 两个实现 + 用例），接线归 M5：Worker 那一层本就在异步边界上。

**代价与到期条件**：M4 结束时审计表在生产路径上仍然是空的。**M5 落 Worker 时必须同时把 TraceSink 接到 TaskStore**，否则"审计事件"这条交付物在 M5 结束时仍只有测试级证据。

**边界**：`TraceEvent.task_id` 可为 `None` 而 `task_audit_events.task_id` 是 `NOT NULL`。落差在入口显式拒绝（`UnscopedAuditEventError`），不交给数据库约束——交给约束会让内存实现根本不报错、PostgreSQL 实现抛驱动层的 `IntegrityError`，调用方要按实现分别 except。台账**不校验任务存在性**（与 `record_approval` 一致），这是选择：校验会让写入依赖 `tasks` 行锁，而 advisory lock 的整个设计前提就是不依赖任何行。

---

## 15. 验收报告格式

按 DEVELOPMENT_PLAN §9 输出四段：**已验证**（实际命令、exit code、精确 SHA、环境、结果）、**只读推理**、**未覆盖**、**残余风险**。

能力状态只能使用已取得的最强证据。M4 完成后 TaskStore 的最强证据仍是 `tests`——**PostgreSQL 集成测试通过不等于部署，更不等于 canary 或用户验收**。

**M4 通过的硬门槛**：ADR-008 四条命令全绿、GitHub CI **七个** job 全绿（既有六个 + `integration`）、§1 的八条判定标准逐条有证据、§10 T8 的三项 TDD 反证逐条先红后绿。`integration` 未绿即不通过（§14.2）。

---

## 16. 计划自审

| 检查 | 结论 |
| --- | --- |
| 是否有占位符 / TBD | 无。§14 四项均已于 2026-09-03 拍板，含 §8.4 审批读回处置（§14.4）。**计划内无未决判断** |
| 复审自查 | 已执行，逐条核对源码，发现六项并全部修复（§6.3）。其中 P1、P3 是初稿的**过度断言**，P2 是第三次漏读交付物 |
| Codex 复审（V1.1） | 通过。两项文档修正已在 V1.2 落实：过期的分支/SHA/工作区事实（§5.1、文首基线），§8.4 的 B3 行脱表渲染与"未拍板"表述 |
| Codex 首轮验收（`797a210`） | **打回**，三条阻断项 + 一条非阻断。逐条根因与处置见 §10 T10；审计事件的拍板见 §14.5。修订记为 V1.3——它追加了一个任务和一条 Protocol 方法，**不是纯文档修正**，与 V1.2 的性质不同 |
| 内部一致性 | §3 依赖清单 / §10 T0 / §11 deps-audit 三处一致；§9.1「不加第五条命令」与 §12.1、§14.2 一致；`list_stale_leases` 在 §1 第 8 条、§6.2 S1、§8.5、§10 T4、§13、§14.1 六处口径一致（任务生命周期方法数已由"五个"改为"六个"）。**Protocol 方法总数**是另一套数：T4 结束时七个，T10 追加 `record_audit_event` 后八个，唯一真源是 `tests/contract/test_protocol_conformance.py` 的哨兵常量 |
| 与真源冲突 | 无已知冲突。§7.2 与 ADR-009 一致；§4 与 §8.5 的 E1 口径与 ADR-007 D7 一致；§12.1 与 ADR-008 一致；§13 的"不引入 Compose"与 ADR-007 D8 的 M5 时点一致 |
| 范围 | 单一里程碑，T0–T9 + T10（首轮验收打回）+ T11（复审自查）+ T12（首次真跑 integration 暴露）+ T13（第二轮复审打回），可拆为多个 PR。`list_stale_leases` 已用 §8.5 的四条「明确不是」封住向调度能力蔓延的路径 |
| 歧义 | 无。§14.2 的"必需 gate"已显式区分「验收硬门槛」与「GitHub required status check」，避免重复 M1 那类误述 |
| 是否降低了既有安全边界 | 否。新增的网络放行经三条测试证明窄度；CI 不引入任何凭证；既有六个 gate 与全部安全测试保持不变 |
