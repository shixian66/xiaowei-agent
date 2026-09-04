# M4 PostgreSQL TaskStore 与恢复归档（2026-09-04）

> 本文件是 M4 验收通过后的历史归档。当前有效状态仍以项目根目录的 `AGENT_HANDOFF.md` 为准。
>
> 文中的 SHA、run id 与数字都是**写下时刻的历史事实**，不随 `main` 漂移；不要把它们当成"现在是什么样"。

## 1. 归档结论

- M4 由项目负责人与 Codex 复审确认可以合并。
- 最终验收对象：`714df07e8064154d6b576376fac23f8f569ae480`。
- 以 fast-forward 合入 `main`，**无合并提交、无 SHA 改写**，21 个受审提交原样保留。
- **首轮验收被打回，此后又被打回三轮**，共四轮。其中三轮的阻断项是**本机结构性看不见的东西**。
- 能力状态最强为 `tests`——**非部署、非 canary、非用户验收**。一次 CI service container 跑绿推不出任何关于生产数据库、真实负载或运维环境的结论。

## 2. 范围

M4 把 M2 冻结的 `TaskStore` 交互形状落到真实 PostgreSQL 上，并补齐两个既有 port 的实现：

- **判定与实现分离**：`persistence/decisions.py` 持有拒绝顺序、fencing 闭合真值表、租约与续租条件、stale 判定与排序键；内存实现与 PostgreSQL 实现**逐字共用**。消除的是"两个实现各自跑偏"——那类分叉不会被任何单实现用例发现。代价是纯函数里的 bug 会让两个实现同时通过，因此由变异反证承重。
- **PostgreSQL 实现**：`create_task`（`ON CONFLICT DO NOTHING` 幂等）、`transition`（`FOR UPDATE` + `WHERE version` CAS）、`acquire_lease` / `renew_lease`（fencing token 单调）、`list_stale_leases`（只读发现，不 claim）、`record_approval` / `record_audit_event`（append-only，`(task_id, seq)` 编号，advisory lock 串行化）。
- **schema 与迁移**：五张表 + fencing 序列，schema 由 Alembic 建立而非 `create_all`——后者会让集成测试跑在一套没人迁移过的表上。
- **两个既有 port 的 PostgreSQL adapter**：`PlanStore`、`EvidenceLedger`。
- **integration 基建**：`tests/integration/`，socket 只按 DSN 解析出的单个 host 逐 item 放行；无 DSN 时整组跳过，**有 DSN 时不允许有任何跳过**。
- **CI 第七个 job `integration`**：PostgreSQL service container，无凭证（`POSTGRES_HOST_AUTH_METHOD=trust`），仍执行 `python -m pytest -q`，**不新增 ADR-008 之外的第五条命令**。

M4 **不做**：Worker、API、Compose、`TraceSink` 生产者接线（`emit` 同步而 `record_audit_event` 异步，接起来要改 M2 已定形状，归 M5）、审批读回的 Protocol 路径（归 M8）。

## 3. 合并与审查事实

- 基线：`main` = `12b5b584da031bff7aa26ab5544d2122736d8945`。
- 工作分支：`claude/m4-postgres-taskstore`（已合入，保留备查）。
- PR [#6](https://github.com/shixian66/xiaowei-agent/pull/6)，`MERGED`，快进合并故 `mergeCommit` 即 `714df07`。
- 合并后 `main` 上独立 run [`33842205710`](https://github.com/shixian66/xiaowei-agent/actions/runs/33842205710)（push 事件）：七个 job 全绿，integration `1442 passed`、0 skipped。
- 同一 SHA 在 `pull_request` 事件下的 run [`33841978802`](https://github.com/shixian66/xiaowei-agent/actions/runs/33841978802) 同样七绿——两个事件各绿一次，排除了"绿依赖 PR 上下文"。

### 21 个受审提交

| # | SHA | 说明 |
| --- | --- | --- |
| 1–3 | `b546aca` `c235ee8` `5305dc4` | 计划 V1 → V1.1（自查修正 6 项）→ V1.2（Codex 复审后的文档修正） |
| 4 | `086f26f` | T0 依赖与实证基线；`asyncpg` 的类型结论与计划预设不同 |
| 5 | `63da870` | T1 抽出共享判定纯函数；变异反证补回两个覆盖缺口 |
| 6 | `c8a683e` | T2 契约套件双绑定化；抽取换来的三条静默失效路径逐条承重 |
| 7 | `891bca9` | T3 schema、迁移与行映射；两处 schema 表述用离线 DDL 比对锁死 |
| 8 | `9fd4a91` | T4 `PostgresTaskStore` 与 `list_stale_leases` |
| 9 | `a2621f3` | T5 integration 基建；socket 放行窄度用真实运行实证 |
| 10 | `000325c` | T6+T7 并发/崩溃注入用例；两个既有 port 的 adapter |
| 11 | `2132823` | T8 三项反证收口；S2 的承重方式与计划预期不同 |
| 12 | `6d723eb` | T9 integration job 与文档收口；镜像用版本 tag 而非编造的 digest |
| 13 | `797a210` | M4 验收报告（**首轮受审对象**） |
| 14 | `4ef3701` | T10 首轮打回四项 + T11 integration gate 触发条件补洞 |
| 15 | `d26d3d3` | T12 integration 首次真跑抓到的四条 |
| 16–17 | `4792fee` `71dbc49` | 回填运行事实；修掉一句自指的残余风险 |
| 18 | `f59dc71` | T13 stale 查询先 LIMIT 后过滤 + 文档漂移 + 钉 digest |
| 19–21 | `734c0fc` `8c8344a` `714df07` | 三枚纯文档修正（**最后一枚为最终验收对象**） |

### 四轮打回

**第一轮（受审 `797a210`）**，三条阻断 + 一条非阻断：

1. **`alembic.ini` 依赖 locale**。Alembic 用 `ConfigParser.read(..., encoding="locale")` 读它，`"locale"` 由**进程环境**解析且无选项可覆盖，于是 ini 里的中文注释在 `LC_ALL=C` 的机器上直接 `UnicodeDecodeError`。影响面不是"测试红"——**同一条读取路径也是 `alembic upgrade head` 的路径**，一个 locale 不是 UTF-8 的运维环境根本跑不了迁移，而 CI 在 UTF-8 下永远看不到。
2. **审计事件只有表没有实现**。`task_audit_events` 建了表，Protocol、两个实现、任何用例、任何写入方全都不存在。**这是漏读交付物**，是本计划内第四次栽在同一处。
3. **`integration` job 从未运行**。根因不是代码：工作流只在 `push: main` / `pull_request: main` 触发，而分支没有 PR。
4. （非阻断）`tests/conftest.py` 的 docstring 推荐了仓库明令禁止的 `socket_enabled`——禁令此前只用 AST 扫代码，散文不受约束。

**第二轮（复审自查，非打回）**：`integration` gate 的**触发条件**本身没有承重。`PYTEST_POSTGRES_DSN` 这个名字在 `tests/integration/conftest.py` 与 `ci.yml` 里是两份独立拷贝，改任一侧就会"七个 job 全绿而零 integration 证据"。既有三道护栏都挡不住这个方向。

**第三轮（`integration` 首次真实运行）**：run [`33828598775`](https://github.com/shixian66/xiaowei-agent/actions/runs/33828598775) 结果是 `4 failed, 1422 passed, 0 skipped`。四条失败分两类，**共同前提是同一件事**：integration 用例在本机永远 skipped，而 ADR-008 的 `mypy src` 不覆盖 `tests`，因此这一整类代码此前既无静态检查也无运行时检查。

- 三条用了**伪造的枚举成员**（`StageOutcome.DENIED` / `.ERROR`，闭集只有 `OK / REJECTED / FAILED / SKIPPED`）；
- 一条 `begin()` 排在首个 `execute()` 之后，SQLAlchemy 2.0 首次 execute 即 autobegin，**该用例从写出来那天起就不可能跑通**——而它是判定标准 4 的唯一证据来源。

**第四轮（Codex 复审）**：`PostgresTaskStore.list_stale_leases` 先在 SQL 侧 `LIMIT`、再在 Python 侧排除终态。根因在 T4 立下的规则本身——「SQL 谓词只被允许更宽」**在有 `LIMIT` 的前提下不成立**。影响不是"偶尔漏几条"：`apply_transition` 有意不清租约字段，因此每个正常结束的任务都永久命中更宽的谓词并按过期时间排在最前，**返回量随系统运行单调衰减到零**；而内存实现是"先过滤、再排序、再截断"，两个实现已分叉。

此外该轮及后续还打回了**两批文档事实漂移**（共九处），根因是文档把"某次运行时为真"与"现在是什么样"用同一时态混写。

## 4. 已验证

**本机**（`.venv`，Python 3.11.16）在最终验收对象上：

| 命令 | 结果 |
| --- | --- |
| `python -m pytest -q` | `1337 passed, 105 skipped` |
| `python -m pytest -m security -q` | `862 passed, 61 skipped, 519 deselected` |
| `ruff check .` | `All checks passed!` |
| `mypy src` | `Success: no issues found in 80 source files` |
| `git diff --check` | 无输出 |

对照 M3 基线的 `1229 passed` / `823 passed, 406 deselected` / 74 源文件。

**CI**：七个 job 全绿；integration `1442 passed`、**0 skipped**（`1442 = 1337 + 105`，即每一条在本机被跳过的用例都真的跑了）。`Initialize containers` 确认拉取的 digest 正是 `ci.yml` 钉死的那个。

**判定标准 2 / 3 / 4**（并发裁决、并发幂等创建、崩溃无中间态）至此拥有运行时证据——它们的**唯一**来源是 `tests/integration/test_concurrency_and_recovery.py` 的 6 条用例。

**变异反证**：M4 全程逐任务执行，全部先红后绿；其中数次首轮全绿，均为真实覆盖缺口并已补测试。详见验收报告 §2.1、§2.5、§2.7、§2.8。

## 5. 只读推理

验收报告 §3 原列八条"读源码 + 离线编译推出、未在真实 PostgreSQL 上执行"的结论。`integration` 跑起来之后**八条全部由真实执行证实**。

值得留档的是其中两条的经过：第 4 条（`pg_terminate_backend` 不留中间态）与第 8 条（提升列与载荷一致）是**最后**拿到证据的，因为承载它们的两条用例**从未真正执行过任何断言**——一条卡在 `begin()` 顺序，一条卡在伪造的枚举成员。「编译通过不等于执行正确」在这次是拿代价换回来的。

## 6. 未覆盖

- **`docker-compose.yml`、API、Worker 不存在**，属 M5。
- **`TraceSink` 生产者未接线**：审计表在生产路径上仍然是空的。**M5 落 Worker 时必须同时接上**，否则"审计事件"这条交付物在 M5 结束时仍只有测试级证据。
- **审批读回没有 Protocol 路径**：存储保真已由 integration 直接读表验证，但仓库里没有任何消费路径——按计划 §14.4 接受，读路径归 M8。
- 未做性能、容量、连接池调优的任何验证。
- 未部署、未 canary、未用户验收；未连接任何真实外部系统，E1 调用恒为 0。

## 7. 残余风险

| 风险 | 说明 |
| --- | --- |
| **CI 绿 ≠ 生产就绪** | 跑绿的是 CI 里的一次性 service container。推不出关于生产数据库、真实负载、连接池或运维环境的任何结论。 |
| **纯函数里的 bug 会让两个实现同时通过** | 共享判定消除的是"分叉"，不是"判错"。只由变异反证承重。 |
| **`integration` 不是 GitHub 强制的 required check** | 分支保护在 private + Free 下不可用（API 实证 403），合并纪律仍依赖人工。 |
| **`hashtext` 是 PostgreSQL 未公开文档的内部函数** | 碰撞只让两个无关任务互相串行化，无害；函数消失会在 CI 立刻暴露。 |
| **本机永远看不见 105 条 integration 用例** | 它们只在 CI 上跑。任何只在本机验证过的改动，对这一半代码没有任何证据。 |

## 8. 后续入口

- **M5 起必须保持的两条 gate**：`integration` 的 0 skip 由 `tests/integration/conftest.py::pytest_sessionfinish` 承重；其**触发条件**由 `tests/contract/test_integration_gate.py` 把 `DSN_ENV_VAR` 与 `ci.yml` 的 env 键钉在一起。删掉任一条，下一次"全绿"就可能是零证据的全绿——`33828598775` 之前正是这个状态。
- **不要把 `list_stale_leases` 的 SQL 谓词改宽**。有 `LIMIT` 时它必须与 `is_stale_lease` 逐条等价，理由见 `postgres.stale_lease_statement` 的 docstring 与本文件第 4 轮打回。
- **不要在终态迁移时清空租约字段**：`fencing_token IS NOT NULL` 是"曾被租出"的判据，清空它会重开 fencing 缺口。终态由 `is_stale_lease` 单独挡。
- M5 落 Worker 时接 `TraceSink` → `record_audit_event`。
