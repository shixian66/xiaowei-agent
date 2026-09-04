# M4 验收报告：PostgreSQL TaskStore 与恢复

> 按 DEVELOPMENT_PLAN §9 的四段格式：已验证 / 只读推理 / 未覆盖 / 残余风险。
>
> **本报告为第四版**。第一版的受审对象 `797a210` 被 Codex 首轮验收**打回**（三条阻断项 + 一条非阻断）；第二版补上 T10 与 T11；第三版记录了 `integration` 的首次真实运行与它当场抓出的四条缺陷。
>
> **第四版最重要的事实**：`integration` job 已在 `d26d3d3` 上**复跑通过**——run [`33829057416`](https://github.com/shixian66/xiaowei-agent/actions/runs/33829057416)，**七个 job 全绿**，integration 步骤输出 `1430 passed`，**0 failed、0 skipped**。`1430 = 本机 1326 passed + 104 skipped`：每一条在本机被跳过的用例都真的跑了，并且全过。
>
> M4 的三条核心判定标准（并发裁决、并发幂等创建、崩溃无中间态）**至此首次拥有运行时证据**。仍未做的事见 §4 与 §5——尤其 service 镜像仍钉在版本 tag 而非 digest。

## 1. 受审对象

| 项 | 值 |
| --- | --- |
| 分支 | `claude/m4-postgres-taskstore` |
| 基线 | `main` = `origin/main` = `12b5b584da031bff7aa26ab5544d2122736d8945` |
| 计划 | [docs/plans/M4-postgres-taskstore.md](../plans/M4-postgres-taskstore.md) V1.4 |
| 提交 | V1.2 计划修正 + T0–T9 各一个提交（HEAD = `797a210`） |
| T10 + T11 | `4ef3701`，已推送 |
| T12 | `d26d3d3` |
| PR | [#6](https://github.com/shixian66/xiaowei-agent/pull/6) |
| CI run #1（首次含 `integration`） | [`33828598775`](https://github.com/shixian66/xiaowei-agent/actions/runs/33828598775) @ `4ef3701`：六个 job success，`integration` **failure**（`4 failed, 1422 passed, 0 skipped`） |
| CI run #2（复跑） | [`33829057416`](https://github.com/shixian66/xiaowei-agent/actions/runs/33829057416) @ `d26d3d3`：**七个 job 全绿**，integration `1430 passed`（0 failed、0 skipped） |
| CI run #3（文档回填后） | [`33829259436`](https://github.com/shixian66/xiaowei-agent/actions/runs/33829259436) @ `4792fee`：**七个 job 全绿**，integration `1430 passed` |
| 受审对象 | **PR #6 的 HEAD**。`pull_request` 事件在每次推送后都会在 exact HEAD 上跑满七个 job，因此受审 SHA 永远有它自己的运行结果；最近一次见 `gh pr checks 6` |
| 首轮验收 | Codex 于 `797a210` **打回**：三条阻断项 + 一条非阻断 |

每个任务一个提交，可独立拒绝。**T10 尚未落成提交**，因此本报告的「已验证」一节描述的是「`797a210` + 工作区未提交改动」这个组合，不是任何一个已存在的 SHA。

## 2. 已验证

**环境**：本机 `.venv`，Python 3.11.16（uv 独立分发）。T0–T9 于 2026-09-03 执行，T10 与本轮复跑于 2026-09-04。

**ADR-008 四条命令**（无 `PYTEST_POSTGRES_DSN`）：

| 命令 | `797a210`（首轮） | 含 T10–T12（本轮） |
| --- | --- | --- |
| `python -m pytest -q` | `1298 passed, 86 skipped` | `1326 passed, 104 skipped` |
| `python -m pytest -m security -q` | `855 passed, 51 skipped, 478 deselected` | `861 passed, 60 skipped, 509 deselected` |
| `ruff check .` | `All checks passed!` | `All checks passed!` |
| `mypy src` | `Success: no issues found in 80 source files` | `Success: no issues found in 80 source files` |

基线是 `main` 上的 `1229 passed` / `823 passed, 406 deselected` / 74 源文件。

T10–T12 合计带来的 `+28 passed / +18 skipped`，逐项对得上（数字为 pytest 收集到的**用例项**，不是函数数）：

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
| **T12**：`tests/contract/test_contract_enum_references.py` | +4 | — |
| **合计** | **+28** | **+18** |

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

### 2.7 T12：`integration` 首次真实运行抓到的四条

run [`33828598775`](https://github.com/shixian66/xiaowei-agent/actions/runs/33828598775)：`4 failed, 1422 passed`，**0 skipped**。**0 skip 本身就是 §2.6 那条 gate 的第一份运行时证据**——它证明的不是"没出错"，而是"104 条确实全跑了"。

四条失败分属两类根因，共同前提是同一件事：integration 用例在本机永远 skipped，而 ADR-008 的 `mypy src` **不覆盖 `tests`**，因此这一整类代码此前既没有静态检查也没有运行时检查。

| 类 | 失败用例 | 根因 |
| --- | --- | --- |
| A | `test_promoted_columns_agree_with_the_stored_payload`、`test_audit_payload_survives_a_real_round_trip`、`test_a_denied_step_leaves_an_audit_row_without_any_evidence` | `StageOutcome.DENIED` / `StageOutcome.ERROR` **是伪造的成员**，闭集只有 `OK / REJECTED / FAILED / SKIPPED` |
| B | `test_a_killed_backend_leaves_no_intermediate_state` | `victim.begin()` 排在 `execute("SELECT pg_backend_pid()")` **之后**，而 SQLAlchemy 2.0 在首次 `execute` 时 autobegin，再调 `begin()` 必抛 `InvalidRequestError`。**这条用例从写出来那天起就不可能跑通**，而它是判定标准 4 的唯一证据来源 |

**A 的处置不止于改那三行**：新增 `tests/contract/test_contract_enum_references.py`，按 AST 扫 `src` 与 `tests` 的全部 `.py`，断言每一处 `<Enum>.<NAME>` 引用真实存在；枚举清单**从 `xiaowei_agent.contracts` 派生**而非硬编码。反空洞断言按树分别计数——只扫到 `src` 同样能让主断言全绿，而出问题的那一半全在 `tests` 里。

**B 不加机械检查**：全仓库同类形状只此一处（其余 `.begin()` 都作用在 engine 上），且它现在有真实的行为反证——`integration` 每个 PR 都会跑。为一个已被真实运行覆盖的问题再造一条脆弱的 AST 规则，是把护栏堆在已经承重的地方。

**变异反证**：两项，全部先红后绿。

| 变异 | 转红 |
| --- | --- |
| 放回真实出过事的 `StageOutcome.DENIED` | 1 |
| 枚举扫描范围缩回只有 `src` | 1 |

**复跑已完成**：run [`33829057416`](https://github.com/shixian66/xiaowei-agent/actions/runs/33829057416) @ `d26d3d3`，integration `1430 passed`、0 failed、0 skipped。四条全部转绿，判定标准 2、3、4 至此有运行时证据。

## 3. 只读推理

第一版这一节列了六条「读源码 + 离线编译推出来、没在真实 PostgreSQL 上跑过」的结论。run `33828598775` 之后其中大部分**不再是推理**——它们被 1422 条通过的用例真的执行了。逐条改判：

| # | 结论 | 现状 |
| --- | --- | --- |
| 1 | `SELECT ... FOR UPDATE` + `WHERE version` + `RETURNING` 产生预期的串行化与 CAS 语义 | **已由首次运行证实**（`test_exactly_one_writer_wins_a_concurrent_cas` 等通过） |
| 2 | `ON CONFLICT DO NOTHING` 让并发幂等创建收敛到一行 | **已由首次运行证实** |
| 3 | `pg_advisory_xact_lock(hashtext(task_id))` 让 `MAX(seq)+1` 分配免于竞争 | **已由首次运行证实**（两条并发台账写入用例通过） |
| 4 | `pg_terminate_backend` 能模拟一次真实崩溃且不留中间态 | **已证实**（run `33829057416`）。它是全部八条里**最后**拿到证据的一条——修复前它从未执行过任何断言 |
| 5 | Alembic 迁移在空库与有数据的库上都能执行 | **已由首次运行证实**（4 条迁移路径用例通过） |
| 6 | GitHub Actions 的 service container 经 `127.0.0.1:5432` 可达 | **已由首次运行证实** |
| 7 | `INSERT ... RETURNING seq` 返回数据库真正分配到的序号 | **已由首次运行证实** |
| 8 | 提升列与整条 JSONB 载荷在真实写入后仍互相一致 | **已证实**（run `33829057416`） |

**八条现在全部由真实执行证实。** 但「编译通过不等于执行正确」这句话在这次拿到了代价：4 与 8 恰好是两条**从未真正执行过断言**的用例，而 4 是判定标准 4 的唯一证据来源——它们在两次运行之间才被发现并修好。

## 4. 未覆盖

**104 条 integration 用例在本机一次都没跑过**（T10 前是 86 条）：本机无 PostgreSQL、无 Docker/Podman、5432 未监听。**它们已在 CI 上全部跑过并通过**（run `33829057416`，`1430 passed`、0 skipped）——本节保留这张分布表，是为了记录「本机看不到什么」这个长期事实，不再是未覆盖项。按 `pytest tests/integration -q -rs` 的实测分布，104 条**穷尽**如下：

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

- ~~`integration` job 从未运行~~ **已解**：run `33828598775` 证实 service 可达、镜像可拉取、迁移能在 CI 上执行；run `33829057416` 全绿。
- **service 镜像未钉 digest**（`postgres:16.10`）。digest 只能联网解析，本次改动在离线环境完成；编一个未经核对的 digest 比用版本 tag 更糟。
- **审批读回没有 Protocol 路径**（§14.4 拍板）：存储保真只有 integration 测试直接读表这一条证据，而它也没跑过。
- ~~`record_approval` 与 `record_audit_event` 的并发行为未验证~~ **已验证**：两条并发用例在 run `33828598775` 上通过，advisory lock 确实挡住了同号分配。
- 未做性能、容量、连接池调优的任何验证。

## 5. 残余风险

| 风险 | 性质 | 处置 |
| --- | --- | --- |
| ~~M4 的核心判定标准没有运行时证据~~ | **已解除** | run `33829057416` 上 `1430 passed`、0 skipped。这曾是本次交付最重要的一条 |
| **报告无法在自身提交内写下自己的 SHA** | 低 | 这是自指，不是覆盖缺口：每次推送都会在 exact HEAD 上跑满七个 job，所以受审 SHA 总有对应的 run。核验方式是 `gh pr checks 6`，不是相信本表里的某个固定 SHA |
| **integration 用例本身此前无任何验证** | 已收窄 | `mypy src` 不覆盖 `tests`，本机又全部 skipped——伪造枚举成员和跑不通的事务顺序因此各躺了一轮。枚举那一类已由 §2.7 的 AST 扫描覆盖；其余类别现在靠「每个 PR 真跑 integration」承重 |
| PostgreSQL 侧实现可能在真实库上失败 | 高 | 每个方法的判定逻辑与内存实现共用纯函数，失败面收窄到"SQL 写法"；但收窄不等于消除 |
| service 镜像用可变 tag | 中 | 已有断言挡住 `latest`；首次 CI 绿灯后钉 digest |
| 纯函数里的 bug 会让两个实现同时通过 | 中 | 共享判定消除的是分叉不是判错；由 §2.1 的变异反证承重 |
| `integration` 不是 GitHub 强制的 required check | 中 | 分支保护仍不可用（private + Free，API 实证 403）；口径已写死为"验收硬门槛"而非"已强制" |
| `hashtext` 是 PostgreSQL 未公开文档的内部函数 | 低 | 碰撞只让两个无关任务互相串行化，无害；函数消失会在 CI 立刻暴露 |

## 6. 能力状态

M4 完成后 TaskStore 的最强证据仍是 **`tests`**——现在是**在真实 PostgreSQL 上全绿的 `tests`**（run `33829057416`，`1430 passed`、0 skipped），不再是「PostgreSQL 实现零运行」。

**这依然只是 `tests`。非部署、非 canary、非用户验收。** 一次 CI 上的 PostgreSQL service container 跑绿，不能推出任何关于生产数据库、真实负载、连接池或运维环境的结论。
