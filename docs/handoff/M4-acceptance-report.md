# M4 验收报告：PostgreSQL TaskStore 与恢复

> 按 DEVELOPMENT_PLAN §9 的四段格式：已验证 / 只读推理 / 未覆盖 / 残余风险。
>
> **本报告为第二版**：第一版的受审对象 `797a210` 已被 Codex 首轮验收**打回**（三条阻断项 + 一条非阻断）。三条阻断项中的两条已按根因修复（见 §2.4），**第三条「`integration` job 从未运行」在本报告出具时仍未解**——它的处置是开 PR，而 PR 尚未开。
>
> **这份报告最重要的一句话仍在「未覆盖」一节**：本机没有 PostgreSQL，也没有容器运行时，因此 **104 条 integration 用例一次都没跑过**。M4 的三条核心判定标准（并发裁决、并发幂等创建、崩溃无中间态）**目前没有任何运行时证据**。

## 1. 受审对象

| 项 | 值 |
| --- | --- |
| 分支 | `claude/m4-postgres-taskstore` |
| 基线 | `main` = `origin/main` = `12b5b584da031bff7aa26ab5544d2122736d8945` |
| 计划 | [docs/plans/M4-postgres-taskstore.md](../plans/M4-postgres-taskstore.md) V1.4 |
| 提交 | V1.2 计划修正 + T0–T9 各一个提交（HEAD = `797a210`） |
| **T10 + T11（本轮）** | **尚未提交**，改动在工作区。受审 SHA 待提交后回填 |
| 首轮验收 | Codex 于 `797a210` **打回**：三条阻断项 + 一条非阻断 |

每个任务一个提交，可独立拒绝。**T10 尚未落成提交**，因此本报告的「已验证」一节描述的是「`797a210` + 工作区未提交改动」这个组合，不是任何一个已存在的 SHA。

## 2. 已验证

**环境**：本机 `.venv`，Python 3.11.16（uv 独立分发）。T0–T9 于 2026-09-03 执行，T10 与本轮复跑于 2026-09-04。

**ADR-008 四条命令**（无 `PYTEST_POSTGRES_DSN`）：

| 命令 | `797a210`（首轮） | 含 T10 + T11（本轮） |
| --- | --- | --- |
| `python -m pytest -q` | `1298 passed, 86 skipped` | `1322 passed, 104 skipped` |
| `python -m pytest -m security -q` | `855 passed, 51 skipped, 478 deselected` | `861 passed, 60 skipped, 505 deselected` |
| `ruff check .` | `All checks passed!` | `All checks passed!` |
| `mypy src` | `Success: no issues found in 80 source files` | `Success: no issues found in 80 source files` |

基线是 `main` 上的 `1229 passed` / `823 passed, 406 deselected` / 74 源文件。

T10 与 T11 合计带来的 `+24 passed / +18 skipped`，逐项对得上（数字为 pytest 收集到的**用例项**，不是函数数）：

| 来源 | passed | skipped |
| --- | --- | --- |
| 9 条共享套件用例（内存绑定 / PostgreSQL 绑定各一份） | +9 | +9 |
| `tests/security/test_audit_ledger_guards.py`（2 个函数 × 2 个实现） | +4 | — |
| `tests/contract/test_config_encoding.py` | +4 | — |
| `tests/security/test_integration_network_boundary.py` 的两条散文禁令 | +2 | — |
| `test_row_mapping.py`：JSONB 覆盖完整性元测试 + `task_audit_events.event` 多出的一个参数 | +2 | — |
| `tests/integration/test_audit_and_approval_ledgers.py` | — | +9 |
| 删除恒绿的 `test_schema_module_declares_no_credentials` | −1 | — |
| **T11**：`tests/contract/test_integration_gate.py` 的四条 gate 触发条件用例 | +4 | — |
| **合计** | **+24** | **+18** |

**deps-audit job 的实际命令**是两步：`uv export --frozen --no-emit-project --extra dev -o requirements-audit.txt`，再 `pip-audit --strict -r requirements-audit.txt`。本轮复跑：exit 0，`No known vulnerabilities found`。

**不要裸跑 `pip-audit --strict`**：它会把本项目自身也算进审计对象，而 `xiaowei-agent` 不在 PyPI 上，`--strict` 下恒失败（本机实测 exit 1，`Dependency not found on PyPI`）。工作流的注释已写明这一点，本报告第一版把命令简写成裸调用，是一处不准确的表述。

### 2.1 TDD 反证

DEVELOPMENT_PLAN §7 M4 明文要求的三项，逐个撤掉承重保护，全部先红后绿：

| 撤掉的保护 | 转红 |
| --- | --- |
| 终态保护（`classify_transition` 第一段） | 7 |
| CAS（版本比较改成两侧同源） | 1 |
| fencing（`_check_fencing` 整段） | 7 |

另外执行的变异（含 T1/T4 的探针）：

| 变异 | 首轮 | 处置 |
| --- | --- | --- |
| 打乱拒绝顺序 | 1 红 | 已承重 |
| fencing 锚点改回「租约此刻是否 live」 | 1 红 | 已承重 |
| `may_acquire_lease` 去掉终态过滤 | 2 红 | 已承重 |
| `context_matches_envelope` 忽略 `environment_id` | 1 红 | 已承重 |
| `is_stale_lease` 去掉「未终态」过滤 | 1 红 | 已承重 |
| 从 `InMemoryTaskStore` 删掉 `list_stale_leases` | 1 红 | 已承重 |
| `apply_transition` 的 `terminal_reason` 改成「有值才写」 | **全绿** | 补测试后 1 红 |
| `may_renew_lease` 只比 `owner` 不比 `fencing_token` | **全绿** | 补测试后 1 红 |
| `stale_lease_sort_key` 去掉 `task_id` 决胜 | **全绿** | 补**纯函数层**测试后 1 红 |
| `lease_is_live` 的 `>` 改成 `>=` | **全绿** | 补测试后 1 红 |
| `PostgresTaskStore` 的 `WHERE version` 改成读到的值 | **全绿** | 见 §2.2 |
| 依赖集合偷加 `redis` | 1 红 | 已承重 |
| `src/` 直接 `import asyncpg` | 1 红 | 已承重 |
| 迁移删掉租约同置同清 CHECK | 1 红 | 已承重 |
| 迁移少建一张表 | 2 红 | 已承重 |
| `task_id` 改成 `Uuid` 列 | 2 红 | 已承重 |
| `alembic.ini` 填上连接串 | 3 红 | 已承重 |
| 套件用例漏进分组 | 1 红 | 已承重 |
| 绑定登记表删一项 | 2–3 红 | 已承重 |
| 绑定挂错分组 | 1 红 | 已承重 |
| 只给一组接 postgres 绑定 | 1 红 | 已承重 |

**四次首轮全绿，全部是真实覆盖缺口，全部已补测试并复跑转红。** 其中两项计划里预判过，两项没有。

### 2.2 一条与计划预期不同的结论

计划 §8.4 把 S2 禁令（`transition` 不得用事务内读到的值当 `expected_version`）的承重测试写成「并发用例转红」。**这个预期是错的**：`PostgresTaskStore.transition` 有两道独立的版本闸——`classify_transition`（Python 侧，两实现共用）和 SQL 侧的 `WHERE version`。在 `FOR UPDATE` 之下第一道先命中，第二道永远走不到被单独检验的路径上，**即使有真实数据库，改它也不会让任何用例转红**。它是纵深防御，而纵深防御按定义没有单独的行为反证。

处置是给它一个机械检查：`tests/security/test_vacuous_check_ban.py` 用 AST 断言 `transition` 里出现 `command.expected_version`、不出现 `current.version`，并配一条反例确认这两条有分辨力。反证：把 `WHERE version` 改成读到的值 → 2 红。

### 2.3 socket 放行的窄度（实证）

用一个指向不存在库的 DSN 跑真实 pytest：

| 断言 | 实测 |
| --- | --- |
| DSN 生效、marker 按目录加上 | integration 用例报 `ConnectionRefusedError`（**不是** `SocketBlockedError`） |
| 目录外仍被拦 | 同一次运行里 `test_no_network.py` 与三条边界用例全绿 |
| integration 内连非 DSN host 被拦 | 连 `192.0.2.1`（RFC 5737）抛 `SocketConnectBlockedError` |
| DSN 已设置时 skip 数为 0 | 同一次运行 **0 skipped** |

这同时证实了计划 §5.2 那条被打回重写的设计现在是对的。

### 2.4 T10：首轮验收打回的四项

逐条根因与处置见计划 §10 T10 与 §14.5。摘要与**当前是否已解**：

| # | 打回项 | 根因 | 状态 |
| --- | --- | --- | --- |
| 1 | `alembic.ini` 依赖 locale（P1） | Alembic 用 `ConfigParser.read(..., encoding="locale")` 读它，`"locale"` 由**进程环境**解析且无选项可覆盖；ini 里的中文注释在 `LC_ALL=C` 下直接 `UnicodeDecodeError`。同一条读取路径也是 `alembic upgrade head` 的路径 | **已解**：ini 改 ASCII-only，散文移进 `env.py`；新增 `tests/contract/test_config_encoding.py` 按目录扫而非按文件名硬编码 |
| 2 | 审计事件只有表没有实现（P1） | 漏读交付物。`task_audit_events` 建了表，Protocol、两个实现、用例、写入方全都不存在 | **已解**：`record_audit_event` 进 Protocol，两个实现落地，9 条共享套件用例 + 1 个 AST 守卫模块 + 9 条 integration 用例；JSONB 覆盖完整性改为由 `ALL_TABLES` 决定 |
| 3 | `integration` job 从未运行（P1） | 工作流只在 `push: main` / `pull_request: main` 触发，而本分支没有 PR | **未解**。处置是开 PR 让 `pull_request` 在 exact HEAD 上跑满七个 job，**PR 尚未开** |
| 4 | `tests/conftest.py` 推荐被禁的 `socket_enabled`（P2） | 禁令此前只用 AST 扫代码，散文不受约束——docstring 里既无 `ast.Name` 也无 `ast.Attribute` | **已解**：禁令扩到原文扫描，只豁免解释禁令本身的两个文件，并加一条反空洞断言 |

顺带修掉的一条**恒绿**用例：`test_schema_module_declares_no_credentials` 遍历"以 `sqlalchemy.url` 开头的行"，而 ini 里根本没有那个键，循环体一次都没执行——把 DSN 写进**别的**键照样通过。替换成扫描全部选项值并断言确实扫过东西。

### 2.5 T10 的变异反证（2026-09-04 独立复跑）

选点覆盖 T10 新增的每一处承重。每项：改动 → 跑 `python -m pytest -q` → 从备份还原 → 复跑确认回到基线。

| 撤掉的保护 | 结果 |
| --- | --- |
| 台账分桶改成全局单桶（序号不按任务分） | 2 红 |
| `InMemoryTaskStore` 去掉 `task_id` 守卫 | 3 红 |
| `record_approval` 恒返回 1 | 3 红 |
| `PostgresTaskStore` 的守卫挪到 `async with` 之后 | 1 红（AST 守卫命中） |
| JSONB 登记表漏掉 `task_audit_events.event` | 1 红 |
| `alembic.ini` 加一行中文注释 | 3 红 |
| DSN 写进 `alembic.ini` 的**非** `sqlalchemy.url` 键 | 1 红 |
| `tests/conftest.py` 散文重新推荐 `socket_enabled` | 1 红 |
| `_next_seq` 去掉 `.where(task_id == ...)` 作用域 | **全绿** |

**最后一项全绿是预期内的，不是新的覆盖缺口**：`_next_seq` 的 `task_id` 作用域由绑到 PostgreSQL 的那两条共享套件用例（`test_approval_seq_is_scoped_per_task`、`test_audit_seq_is_scoped_per_task`）承重，而 integration 从未运行。它落在 §5 已记的头号残余风险里，与 §2.2 的 S2 不同——S2 是**即使有数据库也没有任何用例能转红**，这一项是**有数据库就能转红，只是还没跑过**。

### 2.6 T11：`integration` gate 的触发条件本身没有承重（第二轮复审自查）

不由 Codex 打回，是沿「`integration` job 从未运行」这条路径往下查时发现的。

**根因**：job 能否证明任何东西，取决于 `PYTEST_POSTGRES_DSN` 这个**名字**在两处对得上——`tests/integration/conftest.py` 的 `DSN_ENV_VAR` 常量与 `ci.yml` 的 env 键。这是同一个字符串的两份独立拷贝，之前没有任何东西把它们钉在一起。重命名 conftest 那一侧，CI 就读不到 DSN，104 条用例全部跳过，而「DSN 已设置时不得有跳过」这条 gate 因 `dsn` 为 `None` 老老实实返回空——**七个 job 全绿，一条 integration 用例都没跑**。

既有护栏都挡不住这个方向：`ci.yml` 的整文件 SHA 门槛挡的是工作流被改，而这里改的是 conftest；env 白名单是集合包含判定，只挡「多出来的东西」；而那条 skip gate 的触发条件恰好就是被破坏的东西，**破坏它等于关掉它**。

**处置**：`tests/contract/test_integration_gate.py` 追加四条，把两侧钉死，全部在默认路径上跑，**不改 `ci.yml`**（因此 `_WORKFLOW_SHA256` 不变，不触发那条需人工审查的门槛）。变异反证四项，全部先红后绿：

| 变异 | 转红 |
| --- | --- |
| conftest 重命名 `DSN_ENV_VAR` | 2 |
| `ci.yml` 改掉 env 名 | 4（含整文件 SHA 门槛同时命中） |
| service 端口映射改成 `55432:5432` | 2 |
| 删掉 `integration` job 的 pytest 步骤 | 4 |

**这条不替代 `integration` 真的跑起来**：它保证的是「job 一旦跑，就不可能在零证据的情况下变绿」，不是「job 已经跑过」。阻断项 3 仍然只能靠 PR 上的真实 CI 解。

## 3. 只读推理

以下结论来自阅读源码与离线编译，**没有在真实 PostgreSQL 上执行过**：

1. 计划里的 SQL 形状（`SELECT ... FOR UPDATE` + `WHERE version = :expected_version` + `RETURNING`）能在 PostgreSQL 上产生预期的串行化与 CAS 语义。
2. `ON CONFLICT DO NOTHING` 能让并发幂等创建收敛到一行。
3. `pg_advisory_xact_lock(hashtext(task_id))` 能让两张 append-only 表的 `MAX(seq)+1` 分配免于竞争。
4. `pg_terminate_backend` 能模拟一次真实崩溃且不留中间态。
5. Alembic 迁移在空库与有数据的库上都能执行。
6. GitHub Actions 的 service container 从 job 容器经 `127.0.0.1:5432` 可达（计划 §5.3 第 3 项）。
7. **（T10 新增）** `INSERT ... VALUES(seq = (SELECT COALESCE(MAX(seq),0)+1 WHERE task_id = :id)) ... RETURNING seq` 能在 PostgreSQL 上返回数据库本次真正分配到的序号，且在 `pg_advisory_xact_lock` 之下不会有两个事务算出同一个 `seq`。
8. **（T10 新增）** `task_audit_events` 的提升列（`stage` / `outcome` / `occurred_at`）与整条 JSONB 载荷在真实写入后仍互相一致。

其中 1–5、7–8 的 DDL 与语句形状经 SQLAlchemy 离线编译为 PostgreSQL 方言并与迁移逐表比对通过，但**编译通过不等于执行正确**。

## 4. 未覆盖

**104 条 integration 用例一次都没跑过**（T10 前是 86 条）：本机无 PostgreSQL、无 Docker/Podman、5432 未监听。按 `pytest tests/integration -q -rs` 的实测分布，104 条**穷尽**如下：

| 来源 | 条数 |
| --- | --- |
| `tests/suites/task_store.py` 的 PostgreSQL 绑定（含 T10 新增 9 条台账用例） | 63 |
| `tests/suites/evidence_ledger.py` 的 PostgreSQL 绑定 | 9 |
| `tests/suites/plan_store.py` 的 PostgreSQL 绑定 | 8 |
| `tests/integration/test_audit_and_approval_ledgers.py`（T10 新增） | 9 |
| `tests/integration/test_concurrency_and_recovery.py` | 6 |
| `tests/integration/test_migration_paths.py` | 4 |
| `tests/integration/test_plan_drift_detection.py` | 3 |
| `tests/integration/test_network_narrowness.py` | 2 |
| **合计** | **104** |

其中 6 条并发与崩溃注入是判定标准 2、3、4 的**唯一**证据来源；T10 新增的 9 条台账用例是「落盘保真、拒绝不留行、并发序号互不相撞」的唯一证据来源。

**本报告第一版把这三个套件的绑定数写成「60 条」，是错的**：当时的实际值是 71。分布表现在按实测出具，不再手数。

其余未覆盖项：

- **`integration` job 从未运行**——这是首轮验收的阻断项 3，**至今未解**。service 可达性、镜像可拉取、迁移在 CI 上的执行全部未验证。
- **service 镜像未钉 digest**（`postgres:16.10`）。digest 只能联网解析，本次改动在离线环境完成；编一个未经核对的 digest 比用版本 tag 更糟。
- **审批读回没有 Protocol 路径**（§14.4 拍板）：存储保真只有 integration 测试直接读表这一条证据，而它也没跑过。
- **`record_approval` 与 `record_audit_event` 的并发行为**未验证：`_next_seq` 的 `MAX(seq)+1` 靠 advisory lock 串行化，而 advisory lock 在真实并发下是否真的挡住了同号分配，只有 integration 的两条并发用例能回答（§2.5 最后一项）。
- 未做性能、容量、连接池调优的任何验证。

## 5. 残余风险

| 风险 | 性质 | 处置 |
| --- | --- | --- |
| **M4 的核心判定标准没有运行时证据** | 阻断验收 | `integration` job 未绿即不通过（§14.2）。这是本次交付最重要的一条 |
| **首轮阻断项 3 未解**：`integration` job 仍未运行 | 阻断验收 | 处置是开 PR 让 `pull_request` 事件在 exact HEAD 上跑满七个 job。**PR 尚未开**，第二轮验收若在此之前进行，会被同一条原样打回 |
| **T10 尚未落成提交** | 中 | 本报告「已验证」一节描述的是「`797a210` + 工作区未提交改动」，不是任何已存在的 SHA。提交后须回填受审 SHA 并复跑四条命令 |
| PostgreSQL 侧实现可能在真实库上失败 | 高 | 每个方法的判定逻辑与内存实现共用纯函数，失败面收窄到"SQL 写法"；但收窄不等于消除 |
| service 镜像用可变 tag | 中 | 已有断言挡住 `latest`；首次 CI 绿灯后钉 digest |
| 纯函数里的 bug 会让两个实现同时通过 | 中 | 共享判定消除的是分叉不是判错；由 §2.1 的变异反证承重 |
| `integration` 不是 GitHub 强制的 required check | 中 | 分支保护仍不可用（private + Free，API 实证 403）；口径已写死为"验收硬门槛"而非"已强制" |
| `hashtext` 是 PostgreSQL 未公开文档的内部函数 | 低 | 碰撞只让两个无关任务互相串行化，无害；函数消失会在 CI 立刻暴露 |

## 6. 能力状态

M4 完成后 TaskStore 的最强证据仍是 **`tests`**——而且是**不完整的 `tests`**：内存实现全绿，PostgreSQL 实现零运行。**非部署、非 canary、非用户验收。**
