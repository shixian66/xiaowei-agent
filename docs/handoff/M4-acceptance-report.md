# M4 验收报告：PostgreSQL TaskStore 与恢复

> 按 DEVELOPMENT_PLAN §9 的四段格式：已验证 / 只读推理 / 未覆盖 / 残余风险。
>
> **这份报告最重要的一句话在「未覆盖」一节**：本机没有 PostgreSQL，也没有容器运行时，因此 **86 条 integration 用例一次都没跑过**。M4 的三条核心判定标准（并发裁决、并发幂等创建、崩溃无中间态）**目前没有任何运行时证据**。

## 1. 受审对象

| 项 | 值 |
| --- | --- |
| 分支 | `claude/m4-postgres-taskstore` |
| 基线 | `main` = `origin/main` = `12b5b584da031bff7aa26ab5544d2122736d8945` |
| 计划 | [docs/plans/M4-postgres-taskstore.md](../plans/M4-postgres-taskstore.md) V1.2 |
| 提交 | V1.2 计划修正 + T0–T9 各一个提交 |

每个任务一个提交，可独立拒绝。

## 2. 已验证

**环境**：本机 `.venv`，Python 3.11.16（uv 独立分发），2026-09-03。

**ADR-008 四条命令**（无 `PYTEST_POSTGRES_DSN`）：

| 命令 | 结果 |
| --- | --- |
| `python -m pytest -q` | `1298 passed, 86 skipped` |
| `python -m pytest -m security -q` | `855 passed, 51 skipped, 478 deselected` |
| `ruff check .` | `All checks passed!` |
| `mypy src` | `Success: no issues found in 80 source files` |

基线是 `main` 上的 `1229 passed` / `823 passed, 406 deselected` / 74 源文件。

**`pip-audit --strict`**（deps-audit job 的实际命令）：`No known vulnerabilities found`。

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

## 3. 只读推理

以下结论来自阅读源码与离线编译，**没有在真实 PostgreSQL 上执行过**：

1. 计划里的 SQL 形状（`SELECT ... FOR UPDATE` + `WHERE version = :expected_version` + `RETURNING`）能在 PostgreSQL 上产生预期的串行化与 CAS 语义。
2. `ON CONFLICT DO NOTHING` 能让并发幂等创建收敛到一行。
3. `pg_advisory_xact_lock(hashtext(task_id))` 能让两张 append-only 表的 `MAX(seq)+1` 分配免于竞争。
4. `pg_terminate_backend` 能模拟一次真实崩溃且不留中间态。
5. Alembic 迁移在空库与有数据的库上都能执行。
6. GitHub Actions 的 service container 从 job 容器经 `127.0.0.1:5432` 可达（计划 §5.3 第 3 项）。

其中 1–5 的 DDL 与语句形状经 SQLAlchemy 离线编译为 PostgreSQL 方言并与迁移逐表比对通过，但**编译通过不等于执行正确**。

## 4. 未覆盖

- **86 条 integration 用例一次都没跑过**：本机无 PostgreSQL、无 Docker/Podman、5432 未监听。其中包括：
  - 60 条 TaskStore / PlanStore / EvidenceLedger 的 PostgreSQL 绑定；
  - 6 条并发与崩溃注入（判定标准 2、3、4 的**唯一**证据来源）；
  - 4 条迁移路径；4 条 plan 漂移检测；2 条网络窄度中依赖真实连接的那条。
- **`integration` job 从未运行**。service 可达性、镜像可拉取、迁移在 CI 上的执行全部未验证。
- **service 镜像未钉 digest**（`postgres:16.10`）。digest 只能联网解析，本次改动在离线环境完成；编一个未经核对的 digest 比用版本 tag 更糟。
- **审批读回没有 Protocol 路径**（§14.4 拍板）：存储保真只有 integration 测试直接读表这一条证据，而它也没跑过。
- **`record_approval` 的并发行为**未验证。
- 未做性能、容量、连接池调优的任何验证。

## 5. 残余风险

| 风险 | 性质 | 处置 |
| --- | --- | --- |
| **M4 的核心判定标准没有运行时证据** | 阻断验收 | `integration` job 未绿即不通过（§14.2）。这是本次交付最重要的一条 |
| PostgreSQL 侧实现可能在真实库上失败 | 高 | 每个方法的判定逻辑与内存实现共用纯函数，失败面收窄到"SQL 写法"；但收窄不等于消除 |
| service 镜像用可变 tag | 中 | 已有断言挡住 `latest`；首次 CI 绿灯后钉 digest |
| 纯函数里的 bug 会让两个实现同时通过 | 中 | 共享判定消除的是分叉不是判错；由 §2.1 的变异反证承重 |
| `integration` 不是 GitHub 强制的 required check | 中 | 分支保护仍不可用（private + Free，API 实证 403）；口径已写死为"验收硬门槛"而非"已强制" |
| `hashtext` 是 PostgreSQL 未公开文档的内部函数 | 低 | 碰撞只让两个无关任务互相串行化，无害；函数消失会在 CI 立刻暴露 |

## 6. 能力状态

M4 完成后 TaskStore 的最强证据仍是 **`tests`**——而且是**不完整的 `tests`**：内存实现全绿，PostgreSQL 实现零运行。**非部署、非 canary、非用户验收。**
