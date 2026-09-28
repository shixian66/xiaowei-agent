# F1 StarRocks 受治理只读查询 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> 状态：V2.0（Agent 主链集成版，2026-09-27），待 exact-SHA 独立复审与负责人接受。V2.0 随设计 v9 重写：SQL 查询作为
> 小维普通 capability 接入统一对话主链；网页与飞书直接发 SQL、确定性识别、SQL 消息不进模型（夹带 SQL 的对话可经模型但不能触发执行）；目标不唯一时追问；取消配额。
> 本计划范围为 **F1-Core 查询执行内核**；完成后只标记“F1-Core 完成”，“小维 Agent 查询能力完成”另需 F1-NL（设计 §12.1）。
> 历史版本见 git（V0.3 `73a61c5`，V1.0 `21d4e64`）。
> 本计划获批**不**等于任何切片开工：F1-0b 与 F1-1 起每个切片都需负责人明确开工口令；真实 StarRocks 调用只属 F1-H，
> 真实飞书调用受 ADR-007 F 层现场 GO 约束。当前进度只看 [当前状态](../../../AGENT_HANDOFF.md#current-status)。

**Goal:** 让已认证用户在网页聊天框或飞书直接发送一条 SQL，由小维作为普通 capability 在唯一确定的 StarRocks 上只读执行，保存最多 1000 行预览，并回复状态与锁定结果页链接。

**Architecture:** 复用现有 ChannelSubmissionService、交互接受与路由、CapabilityResolver、SlotVerifier 与澄清链、PlanCompiler、ExecutionDisclosure、WorkflowRunner、StepAdmission、ToolGateway、Reflection 与 Render。新增：确定性 SQL 识别、SQL artifact 与提交事务、规则来源交互事实、F1 SlotVerifier 与目标选择追问、`confirmed_readonly` SQLGuard 与代码内只读语句清单、进程内结果 buffer 与同事务结果写入、worker 有界并发、飞书引用回复作答。SQL 原文只在 SqlArtifactStore 与进程内 HydratedQuery 中出现；识别为 SQL 消息的原文、SqlArtifact 与最终执行字节永不进入模型端口，夹带 SQL 的普通对话照常经模型但不能触发执行。

**Tech Stack:** Python 3.11、Pydantic 2.13.5、SQLAlchemy async + PostgreSQL、migration（`persistence/migrations/versions/rev_00NN_*.py`）、sqlglot 30.17.0、PyMySQL 1.2.0（SSCursor）、Starlette/ASGI、飞书 SDK 长连接、pytest（`security` marker）、Ruff、mypy。

## Global Constraints

- 设计真源：[F1 受治理只读查询设计](../specs/2026-09-27-f1-starrocks-readonly-query-design.md) v9；本计划只写实施与验证，冲突时以设计与 ADR 为准并先报告。
- 真源 ADR：[ADR-018](../../adr/ADR-018-f1-sql-and-result-artifacts.md)，以及 ADR-007/009/010/012/013/017 的“F1 修订”。
- 保存预览行 1000（第 1001 行只作 has_more 哨兵）；保存预览字节 20 MiB；server query timeout 180 秒；SQL 语句数 1；SQL 原文 65_536 bytes UTF-8；普通消息仍 8192 字符。
- Admin 可调低：`preview_max_rows` 1..1000（默认 1000）；`preview_max_bytes` 1 MiB..20 MiB（默认 20 MiB）；`query_timeout_seconds` 1..180（默认 180）。
- timeout：connect/write ≤ 10 秒且 ≤ read；session `query_timeout=Q`；PyMySQL `read_timeout=Q+10`；Gateway `Q+20`；ToolCall ≤ 300 秒。
- worker：`worker_max_concurrent_tasks` 默认 4，范围 1..16；启动校验 `db_pool_size + db_pool_max_overflow >= 2 × 并发数 + 1`。
- `ToolCall.typed_args` 只允许 JSON 标量；SQL 原文不进入交互事实、trace 或审计。纯 SQL 消息、SqlArtifact 和最终执行字节不进入模型；混合对话可以进入模型。模型候选不能直接执行，只有完整展示、用户确认并绑定 hash 后，才能生成新的 SqlArtifact。
- 目标：唯一则用，0 个拒绝，多个追问；只接受提交人本人与展示名完全一致的回答；无默认目标、无模糊匹配。
- 清单外只读语句：`error_code=READONLY_STATEMENT_NOT_SUPPORTED`、用户文案“暂未支持”、`retryable=false`、零 SQL 发送。
- 不支持：外部 Catalog、table function、UNNEST、UDF/qualified function、任何 hint、多语句、INTO OUTFILE、写/DDL/锁/事务/session 改写。
- 不做：target 排队锁、配额、单独的 SQL 页面、草稿确认、流式写入、结果占位或封存状态、StarRocks 版本区间与 digest 比对。
- 触及 `governance/`、`planning/`、`tools/` 的每个 PR 收尾必须跑四门：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。
- 行为变更 TDD：先看到目标原因失败（不是导入/夹具失败），再最小实现；关键保护做隔离变异，用 `PYTHONDONTWRITEBYTECODE=1`。

---

## 0. 阶段门与切片

| 切片 | PR | 交付 | 进入条件 | 退出证据 |
| --- | --- | --- | --- | --- |
| F1-0a | 本 PR | 设计 v9、§13 真源/ADR 修订、本计划 | 设计复审通过 | 文档契约测试、四门、exact-SHA 复审；负责人接受 |
| F1-0b | PR-A | Task 1：worker 有界并发 | F1-0a 获批 + 开工口令 | 长任务不阻塞其他任务、并发上限、停机收尾、连接池校验 |
| F1-0b | PR-B | Task 2–3：契约 DTO、migration、SQL 存储与提交事务 | PR-A 合入 | 共享 suite 与 PostgreSQL 集成、故障注入、digest 固定向量不变 |
| F1-0b | PR-C | Task 4–7：token scan、AST 证明、只读语句清单、StepAdmission requirement | PR-B 合入 | 安全矩阵与变异；慢查询原回归 |
| F1-1 | PR-D | Task 8–9：capability、target 目录、SlotVerifier 与目标追问；SQL 识别、渠道提交与规则交互事实 | PR-C 合入 + 开工口令 | SQL 识别矩阵；模型调用 0 次；追问正反例 |
| F1-1 | PR-E | Task 10–11：Runner 水合、结果 buffer 与同事务写入、fake adapter；回复渲染与锁定结果页 | PR-D 合入 | 正式网页聊天路径成功对照与关键拒绝；§3 对照满足后才注册 `/results` |
| F1-1 | PR-F | Task 12–13：24 小时清理、PyMySQL adapter（离线） | PR-E 合入 | retention 集成测试；SSCursor 截断测试 |
| F1-2 | — | Admin 调低上限与维护黑名单 | F1-1 合入 + 开工口令 | 保存、diff、删除二次确认、restart_required、漂移拒绝 |
| F1-3 | PR-G | Task 14：飞书引用回复作答与长 SQL | F1-1 合入 + 开工口令 | 引用回复正反例、他人作答拒绝、长文本 |
| F1-G | — | 离线总验收 | F1-1–F1-3 合入 | 正式网页与飞书路径、全量/安全/eval、exact-SHA 深审 |
| F1-H | — | 真实 target 现场验证 | 现场计划 + 现场 GO | 设计 §14.2 十一项；最高 `tests + test-env verified` |

F1-0 拆为 F1-0a（文档）与 F1-0b（契约与内核源码）已由负责人同意；F1-0b 仍需负责人明确开工口令。F1-1 的 SQL 识别
对所有渠道生效，因此飞书 ≤ 8192 字符的 SQL 在 F1-1 后即可离线走通；多目标时的飞书作答要到 F1-3。

## 1. 首批只读语句清单（代码内）

sqlglot 30.17.0 看不懂的 StarRocks 只读语句只认本清单。清单写在 `governance/readonly_statements.py`，随代码版本走；
升级 StarRocks 大版本时人工核对。清单外返回“暂未支持”。

**分类证据（只读推理 + 本地 sqlglot 30.17.0 离线 probe，未连接 StarRocks）：**

- 走 AST 路径、无需入清单（sqlglot 解析为 `Show`/`Describe`/查询根）：SHOW DATABASES、SHOW TABLES / FULL TABLES、SHOW COLUMNS / FULL COLUMNS、SHOW INDEX、SHOW TABLE STATUS、SHOW CREATE TABLE / VIEW / DATABASE、SHOW PROCESSLIST / FULL PROCESSLIST、SHOW VARIABLES、SHOW STATUS、SHOW GRANTS、SHOW ENGINES、SHOW CHARSET、SHOW COLLATION、SHOW PLUGINS、SHOW WARNINGS、DESC/DESCRIBE、EXPLAIN / EXPLAIN ANALYZE 查询。
- 反例：`HELP 'show'` 被 sqlglot 解析为 `Alias`，不是允许的根节点，必须拒绝。

**A. 必选（sqlglot 降级为 Command 或 ParseError）：**

| # | 语句族 | 直接对象提取 |
| --- | --- | --- |
| A1 | SHOW CREATE MATERIALIZED VIEW | relation，必有 |
| A2 | SHOW MATERIALIZED VIEWS [FROM db] [LIKE/WHERE] | database；显式 relation 变体检查黑名单 |
| A3 | SHOW PARTITIONS FROM tbl | relation，必有 |
| A4 | SHOW TABLET FROM tbl / SHOW TABLET id | relation 或 tablet id |
| A5 | SHOW DATA [FROM db[.tbl]] | 无 / database / relation |
| A6 | SHOW LOAD、SHOW ROUTINE LOAD、SHOW FUNCTIONS | database/listing |
| A7 | SHOW CATALOGS | 无（仅列举） |
| A8 | SHOW FRONTENDS、SHOW BACKENDS、SHOW RESOURCE GROUPS、SHOW PROC '<path>'、SHOW PROFILELIST | 无（系统 listing） |
| A9 | SHOW ALTER TABLE {COLUMN\|ROLLUP\|MATERIALIZED VIEW} [FROM db] | database；有 relation 时检查黑名单 |
| A10 | ADMIN SHOW REPLICA STATUS / REPLICA DISTRIBUTION FROM tbl | relation，必有 |
| A11 | ANALYZE PROFILE FROM '<query_id>' | 无 |
| A12 | EXPLAIN LOGICAL / VERBOSE / COSTS <query> | 内层 query 精确 byte slice 交 AST 证明 |

**B. 常用只读语句（负责人已确认全部纳入）：**

| # | 语句 | 用途 | 直接对象提取 |
| --- | --- | --- | --- |
| B1 | SHOW RUNNING QUERIES | 看当前运行查询 | 无 |
| B2 | SHOW ANALYZE STATUS、SHOW STATS META | 统计信息排查 | 无直接目标；可选 WHERE 只接受登记列与字面量的比较及 AND 组合，数据库条件属于谓词过滤，不作为对象目标，也不按任意表达式执行 |
| B3 | SHOW DYNAMIC PARTITION TABLES [FROM db] | 分区排查 | database |
| B4 | SHOW DELETE [FROM db]、SHOW EXPORT [FROM db] | 任务 listing（只读） | database |
| B5 | SHOW TRANSACTION [FROM db] WHERE id = <int> | 导入事务排查 | database；WHERE 只接受 `id = 整数` |
| B6 | SHOW COMPUTE NODES | 存算分离节点 | 无 |
| B7 | SHOW ROLES | 权限排查 | 无 |
| B8 | SHOW CREATE ROUTINE LOAD [db.]job | 导入作业定义 | 作业名（非 relation） |
| B9 | DESC db.tbl ALL | 多 index 结构（ParseError） | relation，必有 |

**登记支持不等于给账号增权。** SHOW COMPUTE NODES 需要 OPERATE / cluster_admin，SHOW ROLES 需要 user_admin，
SHOW ROUTINE LOAD 通常需要目标表 INSERT 权限，ADMIN SHOW REPLICA 需要额外权限。F1 可以识别并发送这些语法，
专用只读账号收到 StarRocks 权限错误是预期行为，按结构化执行错误返回；不得为了跑通而给 F1 账号增权。

**暂不入清单（上线后“暂未支持”，按需再加）：**SHOW USERS、SHOW RESOURCES、SHOW STORAGE VOLUMES、SHOW WAREHOUSES、
SHOW PIPES、SHOW STREAM LOAD、SHOW BROKER、SHOW REPOSITORIES、SHOW BACKUP、SHOW RESTORE、SHOW PROPERTY、
SHOW HISTOGRAM META、SHOW VIEWS。

## 2. v7 遗留 P2 的去向

| P2 | 处理 |
| --- | --- |
| 已退让的 F1 任务占满 dispatch 批次 | 已消失：不做调度退让，dispatch 排序与 `dispatch_sort_key` 不变，`tasks` 不新增 `created_at` |
| 每次退让后在 event loop 上重解析最大 1 MiB SQL | 已消失：没有退让；SQL 上限 64 KiB；SQLGuard 在 `asyncio.to_thread` 中执行（Task 10 测试 heartbeat 不被阻塞） |

## 3. Web 规格 §11.2 六项对照

| 前置 | 负责任务 | 验收证据 |
| --- | --- | --- |
| 1. 稳定 result_ref 与有界 artifact | Task 10、Task 11 | 成功时 CSPRNG 引用；1000/1001 行、20 MiB 边界；失败不产生结果 |
| 2. requester/approver/状态/有效期/导出规则唯一真源 | Task 10、Task 11 | `result_access_grants` 只写 `requester_owner`；approver grant 写路径不存在；`export_policy=disabled` |
| 3. ADR-005 | 本 PR 按设计 §13 第 4 项窄化，设计 §11.3 第 3 行、Web 规格 §11.2 第 3 项与 ADR-018 同一结论：F1 锁定页**不依赖 ADR-005**；approver grant、结果行展示与导出仍必须先满足 ADR-005 | 锁定页不写 approver grant、不返回列/行/导出；安全测试断言页面与 API 字段闭集 |
| 4. ADR-013 深链只投影引用 | 本 PR（ADR-013 F1 修订）、Task 11、Task 14 | 网页与飞书回复、ChannelStore 无 SQL、列、行；深链鉴权先于读取 |
| 5. 保留、脱敏、分页、导出、失效 | Task 12；分页归 F2，导出归 F3 | 24 小时过期、隐藏式拒绝、retention 删除范围；数据处置现场证据归 F1-H |
| 6. 独立里程碑、计划与真实调用/数据处置授权 | 本计划 §0 | 各切片开工口令与 F1-H 现场 GO 分离记录在 handoff |

`/results/{result_ref}` 路由只在 Task 11 验收通过后注册；空页或 fake rows 不算交付。

## 4. 文件结构

| 文件 | 责任 | 任务 |
| --- | --- | --- |
| `src/xiaowei_agent/application/worker.py`、`config.py` | 有界并发、`worker_max_concurrent_tasks`、连接池校验、停机收尾 | 1 |
| `src/xiaowei_agent/contracts/task.py` | `ConversationSubmission`（由 `TaskSubmission` 改名）、`ArtifactSubmission`、`TaskSubmission` union | 2 |
| `src/xiaowei_agent/contracts/sql_query.py`（新） | `ReadonlyQueryBudget`、`HydratedQuery`、`ColumnSpec`、`Completeness`、`ResultGrantKind` | 2 |
| `src/xiaowei_agent/contracts/capability.py` | `QueryRequirement` 与 `OperationSpec.query_requirement` | 2 |
| `src/xiaowei_agent/persistence/migrations/versions/rev_0019_f1_sql_results.py`（新） | `task_submissions.input_kind` 与形状约束；`sql_artifacts`、`query_results`、`result_access_grants` | 3 |
| `src/xiaowei_agent/persistence/store.py`、`postgres.py`、`fake.py`、`schema.py`、`rows.py` | submission 按 `input_kind` 分派与 digest；`submit_sql_query`；SQL 读取；`commit_step_result` 写结果 | 3、10 |
| `src/xiaowei_agent/governance/sql_tokens.py`（新） | quote-aware token scan | 4 |
| `src/xiaowei_agent/governance/sqlguard.py` | `confirmed_readonly` 入口、AST 证明、统一后置检查（`template_locked` 不变） | 5 |
| `src/xiaowei_agent/governance/readonly_statements.py`（新） | 代码内只读语句清单 | 6 |
| `src/xiaowei_agent/governance/step_admission.py` | 按 `query_requirement` 强制 profile | 7 |
| `src/xiaowei_agent/capabilities/readonly_query.py`（新）、`planning/starrocks/readonly_query.py`（新） | capability、Params、SlotVerifier、planner | 8 |
| `src/xiaowei_agent/contracts/resource_config.py` | `StarRocksResource` 新增黑名单、F1 上限与启用开关；展示名唯一校验 | 8 |
| `src/xiaowei_agent/contracts/clarification.py`（或现有澄清契约所在文件） | `CAPABILITY_TARGET_SELECTION_REQUIRED` 与目标选项 | 8 |
| `src/xiaowei_agent/capabilities/sql_message.py`（新） | `recognize_sql_message` 纯函数 | 9 |
| `src/xiaowei_agent/application/channel_submission.py`、`application/model_interaction.py` | SQL 消息分流提交；ArtifactSubmission 的规则来源交互事实 | 9 |
| `src/xiaowei_agent/tools/query_result_buffer.py`（新）、`tools/gateway.py`、`tools/starrocks_query_fake.py`（新） | 结果 buffer、Gateway keyword-only 参数、fake adapter | 10 |
| `src/xiaowei_agent/runners/deterministic.py`、`application/runtime.py` | 水合、buffer 交接、结果同事务提交 | 10 |
| `src/xiaowei_agent/rendering/`、`application/result_access.py`（新）、`interfaces/web_app.py`、`web_static` | 回复文案闭集、锁定结果页 | 11 |
| `src/xiaowei_agent/application/query_result_retention.py`（新） | 24 小时清理 | 12 |
| `src/xiaowei_agent/tools/starrocks_query.py`（新） | PyMySQL SSCursor adapter（离线） | 13 |
| `src/xiaowei_agent/interfaces/feishu_sdk.py`、`interfaces/feishu_listener.py`、`application/channel_submission.py` | 飞书父消息 id、引用回复作答、长文本 | 14 |

---

## F1-0b / PR-A：worker 有界并发

### Task 1: worker 最多同时处理 N 个任务

**Files:**
- Modify: `src/xiaowei_agent/application/worker.py`、`src/xiaowei_agent/config.py`、`tests/unit/test_config_happy.py`
- Test: `tests/unit/test_worker_concurrency.py`、`tests/integration/test_worker_concurrency_postgres.py`

**Interfaces:**
- Produces: `Settings.worker_max_concurrent_tasks: int = Field(default=4, ge=1, le=16)`；`Settings.db_pool_size` 默认值由 5 改为 9（同步更新 `tests/unit/test_config_happy.py` 的默认断言）；Settings 校验 `db_pool_size + db_pool_max_overflow >= 2 * worker_max_concurrent_tasks + 1`；worker 在 `run(stop)` 中维护在途 asyncio task 集合，只在有空位时调用 `begin_task_attempt`；每个在途任务仍经 `run_with_task_heartbeat`。

- [ ] **Step 1: 写失败测试**：一个任务在 fake gateway 中阻塞时，另一个任务在同一 worker 内完成；在途数永不超过上限；两个 worker 实例并发领取时同一任务只执行一次；停机时停止领取并等待在途任务结束，超出宽限后取消且任务由 lease 过期恢复；连接池容量不足时 Settings 构造失败；不带任何环境变量的默认 `Settings()` 构造成功且满足校验；上限为 1 时行为与当前逐个执行一致。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/unit/test_worker_concurrency.py tests/integration/test_worker_concurrency_postgres.py -q`
- [ ] **Step 3: 最小实现**：不改 TaskStore、dispatch 规则、失败计数或基础设施故障窗口语义。
- [ ] **Step 4: 运行通过 + 现有 worker/heartbeat 回归**：`python -m pytest tests -k "worker or heartbeat" -q`
- [ ] **Step 5: 四门后提交** `feat(worker): run up to N tasks concurrently`

## F1-0b / PR-B：契约、存储与提交事务

### Task 2: F1 契约 DTO

**Files:**
- Modify: `src/xiaowei_agent/contracts/task.py`、`src/xiaowei_agent/contracts/capability.py`，以及全部 `TaskSubmission(` 构造点
- Create: `src/xiaowei_agent/contracts/sql_query.py`
- Test: `tests/unit/test_f1_contracts.py`、`tests/security/test_f1_contract_boundaries.py`

**Interfaces:**
- Produces（字段名以 ADR-018 D2 为唯一真源）：
  - 现有 `TaskSubmission` 类**显式改名**为 `ConversationSubmission`，机械更新全部构造点（当前 25 处 `TaskSubmission(`），以及 `TaskSubmission.model_fields`、`load_contract(TaskSubmission, ...)` 等类接口用法；不保留同名别名或无标签兼容层
  - `class ConversationSubmission(Contract)`：`input_kind: Literal["conversation"] = "conversation"` + 现有 `envelope`、`context`、`as_of`、`clarification_parent_task_id`
  - `class ArtifactSubmission(Contract)`：`input_kind: Literal["sql_artifact"]`、`context: RequestContext`、`as_of: AwareDatetime`、`sql_ref: StrictStr`、`sql_hash: Sha256Hex`
  - `TaskSubmission = Annotated[ConversationSubmission | ArtifactSubmission, Field(discriminator="input_kind")]`
  - `class QueryRequirement(StrEnum): NONE; TEMPLATE_LOCKED; CONFIRMED_ARTIFACT`；`OperationSpec.query_requirement: QueryRequirement = QueryRequirement.NONE`
  - `ReadonlyQueryBudget(preview_max_rows 1..1000, preview_max_bytes 1_048_576..20_971_520, query_timeout_seconds 1..180)`
  - `@dataclass(frozen=True, slots=True) class HydratedQuery`：`sql_ref`、`sql_hash`、`sql_bytes: bytes`、`resource_id`、`target_fingerprint`、`config_revision`、`budget`；构造时校验 SHA-256；`repr` 不含 bytes；不可 pickle
  - `ColumnSpec(ordinal, name, type)`：`ordinal: int ≥ 0`、`name: str`、`type: str`；`Completeness`（`complete`/`truncated_rows`/`truncated_bytes`）；`ResultGrantKind`（F1 只用 `requester_owner`）

- [ ] **Step 1: 写失败测试**：`TaskSubmission` union 对缺少 `input_kind` 的输入按 Pydantic 2.13.5 行为拒绝（`union_tag_not_found`）；直接构造 `ConversationSubmission(...)` 得到 `input_kind="conversation"`；conversation 的三个 digest 固定向量逐字节不变；`ArtifactSubmission` 无 SQL 原文、目标或结果字段且 `extra="forbid"`；`HydratedQuery` hash 不符构造失败，`repr`/`pickle` 不泄漏 bytes；`ToolCall` 仍拒绝非标量；`ReadonlyQueryBudget` 越界被拒。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/unit/test_f1_contracts.py tests/security/test_f1_contract_boundaries.py -q`
- [ ] **Step 3: 最小实现**；同一提交内完成改名与全部构造点更新。
- [ ] **Step 4: 运行相关测试与 `tests/unit/test_hash_vectors.py`**。
- [ ] **Step 5: 提交** `feat(contracts): add F1 submission and query DTOs`

### Task 3: migration、SQL 存储与提交事务

**Files:**
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0019_f1_sql_results.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`、`store.py`、`postgres.py`、`fake.py`、`rows.py`、`decisions.py`
- Test: `tests/suites/task_store.py`（新增用例）、`tests/integration/test_f1_submit_postgres.py`、`tests/integration/test_migration_paths.py`

**Interfaces:**
- Produces:
  - `TaskStore.submit_sql_query(*, command: SqlQuerySubmitCommand) -> SqlQuerySubmitResult`——SQL 消息的**唯一提交入口**；SqlArtifact 写入只作为共享同一连接/事务的私有 helper，不新增公开的 Store 提交方法、UnitOfWork 或通用事务框架
  - `SqlQuerySubmitCommand(context: RequestContext, sql_bytes: bytes, idempotency_key: str, as_of, trace_id)`
  - `SqlQuerySubmitResult(outcome: SqlSubmitOutcome, task: TaskRecord | None)`；`SqlSubmitOutcome` 闭集：`CREATED`、`REPLAYED`、`IDEMPOTENCY_CONFLICT`
  - `decisions.classify_sql_submit(...)`：内存与 PostgreSQL 共用的纯判定
  - `TaskStore.load_sql_for_execution(*, grant, sql_ref, sql_hash) -> SqlArtifactRecord`（校验 actor、tenant、environment、hash、未过期）
  - 表：`sql_artifacts`、`query_results`（列与行 JSONB、completeness、计数、`result_ref` 唯一）、`result_access_grants`（CHECK：只有 `requester_owner` 可空 `approval_ref`）

- [ ] **Step 1: 写失败测试**：按 ADR-018 D2 逐条断言——旧行回填 `input_kind='conversation'` 后按原 schema 读回且三个 digest 不变；加列后默认值已移除；`ck_task_submissions_shape` 拒绝 conversation 行带 artifact 列、sql_artifact 行带 envelope 或 clarification parent、缺任一引用列；未知 `input_kind` fail-closed；存在 sql_artifact 行时 downgrade 报错、无此类行时 downgrade 恢复 `envelope NOT NULL`。提交事务按 ADR-018 D2a：同幂等键同内容返回 `REPLAYED` 且同一 task；同键不同内容 `IDEMPOTENCY_CONFLICT`；SQL 幂等键与相同字面值的对话幂等键不冲突；在写 SqlArtifact、建 task 之后分别注入异常，全部回滚且重试可成功；提交结果未知时回读 winner 不重复创建；`load_sql_for_execution` 对他人、跨环境、hash 不符与过期拒绝。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/integration/test_f1_submit_postgres.py tests/integration/test_migration_paths.py -q`
- [ ] **Step 3: 实现**：只在现有 PostgreSQL TaskStore 事务内完成。
- [ ] **Step 4: 运行共享 suite 的内存与 PostgreSQL 实现**，再跑 `tests/integration` 全部。
- [ ] **Step 5: 变异**：把 SqlArtifact 写入挪到独立事务，故障注入用例必须转红；还原。
- [ ] **Step 6: 四门后提交** `feat(persistence): add F1 SQL storage and submit transaction`

## F1-0b / PR-C：SQLGuard

### Task 4: quote-aware token scan

**Files:**
- Create: `src/xiaowei_agent/governance/sql_tokens.py`
- Test: `tests/unit/test_sql_tokens.py`、`tests/security/test_f1_token_scan.py`

**Interfaces:**
- Produces: `scan_sql(raw: bytes) -> TokenScan`，`TokenScan.tokens: tuple[SqlToken, ...]`，`SqlToken(kind, start_byte, end_byte)`；`TokenScanError(reason)`，闭集含 `MULTI_STATEMENT`、`HINT_COMMENT`、`CONTROL_CHARACTER`、`AMBIGUOUS_PUNCTUATION`、`INTO_OUTFILE`、`INVALID_UTF8`、`BOM`、`EMPTY`、`TOO_LONG`。

- [ ] **Step 1: 写失败测试**：字符串/quoted identifier/普通注释中的 `;` 不算多语句；尾随单个 `;` 后只有空白或注释时视为同一语句；`/*+`、`/*!` 在字符串外拒绝、在字符串内允许；`--`、`#`、普通块注释允许；控制字符、Unicode format 字符与 BOM 拒绝；中文在字符串/注释内允许，全角标点和 smart quote 在关键字/运算符/identifier 位置拒绝；字符串外 `INTO OUTFILE` 拒绝；65_537 bytes 拒绝；每个 token 的字节区间可精确切回原 bytes。
- [ ] **Step 2–4: 失败 → 单遍状态机实现（不用正则做安全判断）→ 通过**：`python -m pytest tests/unit/test_sql_tokens.py tests/security/test_f1_token_scan.py -q`
- [ ] **Step 5: 变异**：移除 hint 检测，comment hint 恶意矩阵必须转红。
- [ ] **Step 6: 提交** `feat(governance): add quote-aware SQL token scan`

### Task 5: confirmed_readonly AST 证明与黑名单

**Files:**
- Modify: `src/xiaowei_agent/governance/sqlguard.py`
- Test: `tests/unit/test_sqlguard_confirmed_readonly.py`、`tests/security/test_f1_sql_ast_matrix.py`

**Interfaces:**
- Consumes: `scan_sql`（Task 4）
- Produces:
  - `verify_confirmed_readonly(*, raw: bytes, expected_sha256: str, policy: ReadonlyPolicy) -> ReadonlyProof`
  - `ReadonlyProof(path: Literal["ast", "statement_list"], statement_family: str, relations: tuple[QualifiedRelation, ...])`
  - `ReadonlyPolicy(default_database: str, blocked_relation_names: frozenset[QualifiedRelation])`
  - 拒绝码：`SqlGuardRejection` 增加 `READONLY_STATEMENT_NOT_SUPPORTED`、`BLOCKED_RELATION`、`EXTERNAL_CATALOG`、`TABLE_FUNCTION`、`HASH_MISMATCH`、`NOT_READONLY`

- [ ] **Step 1: 写失败测试**：`Select`/`Union`/`Intersect`/`Except` 根节点成功与写形状反例；SHOW DATABASES、SHOW DATABASES FROM default_catalog 成功，FROM 外部 Catalog 拒绝；SHOW TABLES 成功；DESC、SHOW CREATE、SHOW COLUMNS 精确命中黑名单拒绝；一段名按 default database、两段、三段 `default_catalog` 成功，其他 catalog 拒绝；CTE alias 与 derived table 不当基础关系；跨内部库 JOIN 成功；information_schema/sys 读取成功；EXPLAIN / EXPLAIN ANALYZE SELECT 递归检查，EXPLAIN ANALYZE INSERT 拒绝；table function、UNNEST、qualified function 拒绝；`HELP 'x'`（`Alias` 根）拒绝；hash 不符拒绝；Guard 从不返回改写文本。
- [ ] **Step 2–4: 失败 → 实现 → 通过 + 慢查询原回归**：`python -m pytest tests/unit/test_sqlguard_confirmed_readonly.py tests/security/test_f1_sql_ast_matrix.py -q && python -m pytest tests -k "sqlguard or slow_query" -q`
- [ ] **Step 5: 变异**：移除黑名单检查与 hash 校验，对应反例分别转红。
- [ ] **Step 6: 提交** `feat(governance): add confirmed_readonly AST proof`

### Task 6: 代码内只读语句清单

**Files:**
- Create: `src/xiaowei_agent/governance/readonly_statements.py`
- Modify: `src/xiaowei_agent/governance/sqlguard.py`（Command/ParseError 时查清单）
- Test: `tests/unit/test_readonly_statements.py`、`tests/security/test_f1_statement_list_matrix.py`

**Interfaces:**
- Produces: `READONLY_STATEMENTS: tuple[ReadonlyStatement, ...]`；`ReadonlyStatement(family: str, pattern: tuple[TokenPattern, ...], target: TargetRule, forbidden_clauses: frozenset[str])`；`match_statement(scan: TokenScan, raw: bytes) -> StatementMatch | None`

- [ ] **Step 1: 写失败测试**：§1 清单 A1–A12、B1–B9 每项有正常对照、大小写/空白/quoted identifier 边界、缺失或重复 clause、多语句、hint、外部 Catalog、黑名单目标与歧义目标反例；清单外前缀返回 `READONLY_STATEMENT_NOT_SUPPORTED`、`retryable=false`；EXPLAIN LOGICAL/VERBOSE/COSTS 的内层精确 byte slice 交 AST 证明，INSERT、INTO OUTFILE、外部 Catalog、table function、黑名单反例；没有“以 SHOW 开头即放行”的泛化规则。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_readonly_statements.py tests/security/test_f1_statement_list_matrix.py -q`
- [ ] **Step 5: 变异**：对带对象的清单项移除目标提取，至少一个黑名单测试转红，且确认未被更早规则遮蔽。
- [ ] **Step 6: 提交** `feat(governance): add in-code readonly statement list`

### Task 7: StepAdmission 按 query requirement 强制 profile

**Files:**
- Modify: `src/xiaowei_agent/governance/step_admission.py`、`governance/admission.py`
- Test: `tests/security/test_f1_step_admission.py`

**Interfaces:**
- Produces: `admit_step(..., hydrated_query: HydratedQuery | None = None)`；requirement 为 `CONFIRMED_ARTIFACT` 时 HydratedQuery 必须存在且与 ToolCall 标量逐项一致；为其他值时传入 HydratedQuery 同样拒绝。

- [ ] **Step 1: 写失败测试**：required 缺失 → 拒绝；多余 → 拒绝；`sql_hash`/`target_fingerprint`/`config_revision`/预算任一不一致 → 拒绝；慢查询计划伪装成 `confirmed_readonly` → profile 漂移拒绝；`tool_call_hash` 覆盖全部标量。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/security/test_f1_step_admission.py tests/security -q`
- [ ] **Step 5: 四门后提交** `feat(governance): enforce confirmed_artifact requirement in admission`

## F1-1 / PR-D：接入 agent 主链

### Task 8: capability、target 目录、SlotVerifier 与目标追问

**Files:**
- Create: `src/xiaowei_agent/capabilities/readonly_query.py`、`src/xiaowei_agent/planning/starrocks/readonly_query.py`
- Modify: `application/default_capabilities.py`、`capabilities/specs.py`、`contracts/resource_config.py`、澄清契约与 `rendering/generic.py`（追问文案）
- Test: `tests/unit/test_readonly_query_slots.py`、`tests/unit/test_readonly_query_planner.py`、`tests/unit/test_f1_resource_config.py`、`tests/security/test_plan_determinism.py`（新增用例）

**Interfaces:**
- Produces:
  - `starrocks.readonly_query@1.0.0`，唯一 operation `execute_readonly_query`（READ + RESTRICTED、`query_requirement=CONFIRMED_ARTIFACT`、profile `confirmed_readonly`）；意图名 `starrocks_readonly_query`，只由规则来源交互事实产生，不进入模型可选意图集合
  - `CapabilityInputBinding` 与 `ReadonlyQuerySlotVerifier`：输入 `sql_ref`/`sql_hash`、RequestContext、F1 target 目录、可选澄清上下文；输出按设计 §5.4 决策表的 `SlotReady[ReadonlyQueryParams]` / `SlotIncomplete` / `SlotInvalid`
  - `ClarificationReasonCode.CAPABILITY_TARGET_SELECTION_REQUIRED`；ClarificationRecord 为该 reason 保存 `sql_ref`、`sql_hash` 与 `target_options: tuple[TargetOption, ...]`（resource_id + 展示名）；提示从记录渲染选项
  - `ReadonlyQueryParams(sql_ref, sql_hash, resource_id, config_revision, budget)`；`compile_readonly_query_plan(params) -> ExecutionPlan`（`max_steps=1`、`max_tool_calls=1`、`max_model_tokens=0`）
  - `StarRocksResource` 新增 `blocked_relation_names: tuple[str, ...] = ()`（规范化、去重，冲突/单段/通配启动失败）、`f1_enabled: bool = False`、`f1_budget: ReadonlyQueryBudget`；同一租户与环境内 F1 启用资源的 `display_name` 唯一，否则启动失败

- [ ] **Step 1: 失败测试**：1 个 target → `SlotReady`；0 个 → `SlotInvalid`；多个 → `SlotIncomplete` 且记录选项；澄清回答与展示名完全一致 → `SlotReady`；大小写不同、前缀、序号、空白差异 → 再次追问；plan 无 SQL 原文；typed_arguments 与 budget 进入 `plan_hash`；预算放大被检出；该意图不出现在模型可选意图中；黑名单、展示名或上限变化改变 config revision。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_readonly_query_slots.py tests/unit/test_readonly_query_planner.py tests/unit/test_f1_resource_config.py tests/security/test_plan_determinism.py -q`
- [ ] **Step 5: 提交** `feat(capabilities): register starrocks.readonly_query with target clarification`

### Task 9: SQL 识别、渠道提交与规则来源交互事实

**Files:**
- Create: `src/xiaowei_agent/capabilities/sql_message.py`
- Modify: `application/channel_submission.py`、`application/model_interaction.py`、`application/runtime.py`、`interfaces/web_models.py`（聊天文本上限）
- Test: `tests/unit/test_sql_message_recognition.py`、`tests/security/test_f1_sql_never_reaches_model.py`、`tests/integration/test_f1_channel_submit_postgres.py`

**Interfaces:**
- Produces:
  - `recognize_sql_message(text: str) -> SqlMessage | None`：纯函数；按设计 §5.1 三条规则（闭集语句关键字、token 扫描、恰好一条完整语句）；整条 fenced code block 解包；复用 Task 4–6 的扫描与清单
  - `contains_embedded_sql(text: str) -> bool`：纯函数；文本中任一 fenced code block，或以闭集语句关键字开头、可被 sqlglot 完整解析为非 `Command` 语句的片段
  - `route_interaction(*, draft, context, embedded_sql: bool = False)`：`embedded_sql=True` 时任何 `CAPABILITY_REQUEST` 返回 RESPOND（advisory-only），不进入 Resolver；Runtime 由确定性检测传入，不取自模型
  - `ChannelSubmissionService.submit`：识别为 SQL → `TaskStore.submit_sql_query`；否则走现有 `ConversationSubmission`，普通消息超过 8192 字符拒绝；`ChannelSubmitCommand.text` 上限放宽到 65_536 bytes
  - `load_or_accept_interaction`：`ArtifactSubmission` 直接保存 `origin=rule` 的 `AcceptedInteractionArtifact`（`CAPABILITY_REQUEST` + `starrocks_readonly_query`、空槽位），不构造模型请求，`ModelCallObservation.request_count=0`；目标选择追问的澄清子任务同样由规则解释

- [ ] **Step 1: 失败测试**：读/写关键字开头的完整语句识别为 SQL；“show 一下昨天的慢查询”“帮我看看这条 SQL 为什么慢：SELECT …”“explain why …”识别为对话；整条代码块解包；多语句、hint 仍识别为 SQL 以便明确拒绝；同一输入永远同一结论；SQL 消息与目标选择回答的模型调用次数为 0，且模型请求构造对它们不可达；SQL 原文不出现在 envelope、交互事实、trace、审计与 ChannelStore；非 SQL 超 8192 字符拒绝、SQL 超 65_536 bytes 拒绝；现有对话、澄清与慢查询路由回归不变；夹带 SQL 的对话中 fake 模型返回 `starrocks_readonly_query` 意图时被拒绝为不可执行，不建 SqlArtifact、Gateway 调用为 0；“帮我分析这条慢SQL：SELECT …”在现有关键词规则会识别为慢查询诊断的情况下仍为 advisory-only、Gateway 调用为 0，模型路由同理；不含嵌入 SQL 的“最近有哪些慢查询”仍正常走慢查询诊断。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_sql_message_recognition.py tests/security/test_f1_sql_never_reaches_model.py tests/integration/test_f1_channel_submit_postgres.py -q && python -m pytest tests -k "interaction or channel or clarification" -q`
- [ ] **Step 5: 变异**：让 ArtifactSubmission 走模型分类，`test_f1_sql_never_reaches_model` 必须转红；放开模型来源的 `starrocks_readonly_query` 意图，`test_f1_model_origin_query_intent_never_executes` 必须转红；去掉 `embedded_sql` 分支，`test_f1_embedded_sql_is_advisory_only` 必须转红；还原。
- [ ] **Step 6: 四门后提交** `feat(application): route SQL messages into the capability pipeline without the model`

## F1-1 / PR-E：执行、结果与回复

### Task 10: Runner 水合、结果 buffer 与同事务写入

**Files:**
- Create: `src/xiaowei_agent/tools/query_result_buffer.py`、`src/xiaowei_agent/tools/starrocks_query_fake.py`
- Modify: `runners/deterministic.py`、`tools/gateway.py`、`tools/adapter.py`、`persistence/store.py`、`postgres.py`、`fake.py`
- Test: `tests/unit/test_query_result_buffer.py`、`tests/security/test_f1_gateway_query.py`、`tests/integration/test_f1_runner_flow_postgres.py`

**Interfaces:**
- Consumes: Task 2–9
- Produces:
  - `QueryResultBuffer(max_rows, max_bytes)`：`set_columns(tuple[ColumnSpec, ...])`、`append_row(tuple[object, ...]) -> BufferState`、`close()`、`freeze() -> BufferedQueryResult`；关闭后写入被丢弃
  - `ToolGateway.invoke(call, *, context, admission, hydrated_query: HydratedQuery | None = None, result_buffer: QueryResultBuffer | None = None)`；`AdapterResponse.payload` 与 `ToolResult.data_view` 必须为空
  - `StepCommitCommand` 增加可选 `query_result: BufferedQueryResult | None` 与 `result_ref: str | None`（二者同有同无）；Runner 在 `_build_evidence` 前用 `secrets.token_urlsafe(32)` 生成 `result_ref`，同一个值写入 Evidence 与命令；`commit_step_result` 在同一事务以它写结果行与 `requester_owner` grant；失败或超时不写结果
  - 水合只按 `query_requirement` 分支，不按 operation 名称判断

- [ ] **Step 1: 失败测试**：HydratedQuery 缺失或 bytes hash 不符时 adapter 调用 0；payload/data_view 非空时 Gateway 拒绝；1000/1001 行得到 `truncated_rows`、超字节得到 `truncated_bytes`、不存半行；Gateway 超时后迟到写入被丢弃且不产生结果；Evidence 中的 `result_ref` 与结果表、grant 中的完全相同；结果、grant 与步骤提交同事务，在两者之间注入故障时都不生效；提交前崩溃不产生结果；已开始未提交的步骤再次领取得到 `BUDGET_EXHAUSTED`、Gateway 调用 0；SQLGuard 在线程中执行时 heartbeat 按时续租；结果行不进入 Evidence、trace、audit；ExecutionDisclosure 在首次 Gateway 前写入。
- [ ] **Step 2–4: 失败 → 实现 → 通过 + Runner/worker 回归**：`python -m pytest tests/unit/test_query_result_buffer.py tests/security/test_f1_gateway_query.py tests/integration/test_f1_runner_flow_postgres.py -q && python -m pytest tests -k "runner or worker" -q`
- [ ] **Step 5: 变异**：把结果写入挪出 `commit_step_result` 事务，同事务用例转红；还原。
- [ ] **Step 6: 提交** `feat(runners): execute SQL artifacts with an in-memory result buffer`

### Task 11: 回复渲染与锁定结果页

**Files:**
- Create: `src/xiaowei_agent/application/result_access.py`、锁定结果页静态资源
- Modify: `rendering/generic.py`、`rendering/feishu.py`、`interfaces/web_app.py`、`reflection/`（截断限制）
- Test: `tests/contract/test_f1_render.py`、`tests/security/test_f1_locked_result_page.py`、`tests/evals/`（F1 回复用例）

**Interfaces:**
- Produces: RenderPayload 文案闭集（成功：目标展示名、行数、是否截断、耗时、锁定结果页链接与“数据需 F2 审批后查看”；失败：不是只读语句、暂未支持、命中黑名单、目标不唯一或未配置、超时、StarRocks 权限不足、StarRocks 语法或执行错误（只给类别与错误码）、执行中断结果未知）；`GET /results/{result_ref}` 与对应 API，字段闭集：target 展示名、时间、上限、保存行/字节、截断、过期时间、F2 提示、requester 本人的 SQL；not-found/过期/越权同一隐藏式拒绝

- [ ] **Step 1: 失败测试**：每类结局的回复文案精确匹配闭集；回复、飞书卡片与 RenderPayload 不含 SQL、列与行；requester 可见锁定页，Admin、群内其他用户、过期同一拒绝；响应字段集合精确等于闭集；上游错误正文不回显。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/contract/test_f1_render.py tests/security/test_f1_locked_result_page.py -q`
- [ ] **Step 5: 正式进程 + 浏览器**：在网页聊天框发送 SQL、查看回复与锁定结果页（fake adapter，注明替身边界）；多目标时完成一次网页追问往返。
- [ ] **Step 6: 四门后提交** `feat(rendering): add F1 replies and locked result page`

## F1-1 / PR-F：清理与真实驱动 adapter（离线）

### Task 12: 24 小时清理

**Files:**
- Create: `src/xiaowei_agent/application/query_result_retention.py`
- Modify: worker 装配
- Test: `tests/integration/test_f1_retention_postgres.py`

- [ ] **Step 1: 失败测试**：结果在任务终态后 24 小时过期；SQL 创建即写 `created_at + 24h`，新引用或任务终态时延到该时刻 + 24h、只延不缩；未作答、长期排队或卡住的任务不阻止 SQL 过期；执行前 SQL 已过期时步骤以 `sql_artifact.expired` 失败、Gateway 调用为 0、任务 FAILED；过期后作答提示重新发送；过期后立即拒绝读取；删除 SQL bytes、列、行、grant 与活动索引，只保留 hash、actor、target、时间、上限、决定、query id 与指标；日志、trace、Evidence 无 SQL/结果副本。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/integration/test_f1_retention_postgres.py -q`
- [ ] **Step 5: 提交** `feat(persistence): add 24h F1 retention`

### Task 13: PyMySQL SSCursor adapter（离线）

**Files:**
- Create: `src/xiaowei_agent/tools/starrocks_query.py`
- Test: `tests/unit/test_starrocks_query_adapter.py`

**Interfaces:**
- Produces: target-bound `StarRocksReadonlyQueryAdapter`：独占 connection；设置 `query_timeout=Q`、`sql_select_limit=rows+1`；执行提交的原始 bytes（发送前再算一次 SHA-256）；`fetchmany(≤100)` 写入 `QueryResultBuffer`；提前截断/超时/取消时直接 `connection.close()`，不走会耗尽结果的 `SSCursor.close()`；错误映射为闭集类别与 StarRocks 错误码，不回显上游正文

- [ ] **Step 1: 失败测试**（假 connection/cursor 替身，锁定 PyMySQL 1.2.0 行为）：第 1001 行后不再 fetch；截断路径不调用 `cursor.close()`；bytes hash 不符零发送；buffer 关闭后停止读取；权限错误与语法错误映射到对应类别。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_starrocks_query_adapter.py -q`
- [ ] **Step 5: 真实驱动集成**（本地隔离 MySQL 协议容器验证 SSCursor 提前关闭）需负责人确认可用镜像；未确认前标“未验证”。
- [ ] **Step 6: F1-1 收尾**：四门、正式网页聊天路径成功对照与关键拒绝、设计 §14.1 变异清单；提交 `feat(tools): add offline StarRocks query adapter`

## F1-3 / PR-G：飞书引用回复作答与长 SQL

### Task 14: 飞书引用回复与长文本

**Files:**
- Modify: `src/xiaowei_agent/interfaces/feishu_sdk.py`（事件 DTO 新增父消息 id、文本上限）、`interfaces/feishu_listener.py`、`application/channel_submission.py`（放开飞书 clarification parent）、`persistence/channel.py`（按小维消息 id 查父任务）
- Test: `tests/unit/test_feishu_reply_parent.py`、`tests/integration/test_f1_feishu_clarification_postgres.py`

**Interfaces:**
- Produces: `FeishuMessageEvent.parent_message_ref: StrictStr | None`；ChannelStore 按已记录的小维消息 id 查询所属任务；`ChannelSubmitCommand.clarification_parent_task_id` 允许飞书渠道，但只能由引用回复的父消息推导，不接受客户端自报

- [ ] **Step 1: 失败测试**：引用回复小维追问且作答人为提交人 → 子任务并继续执行；他人引用回复 → 拒绝；未引用回复的同名消息 → 当作新消息；引用回复非小维消息或非追问消息 → 当作新消息；父任务已完成或过期 → 提示重新发送；飞书 SQL 超过 8192 字符（≤ 65_536 bytes）可以提交；群聊未 @小维 的消息仍收不到。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_feishu_reply_parent.py tests/integration/test_f1_feishu_clarification_postgres.py -q && python -m pytest tests -k "feishu" -q`
- [ ] **Step 5: 四门后提交** `feat(feishu): answer F1 target clarification by quoted reply`

---

## F1-2 / F1-G / F1-H（开工前再展开）

- **F1-2**：扩展 W4 资源页；只能调低上限；黑名单精确 `database.object`、规范化、去重、diff、删除二次确认；展示名唯一；保存后 `restart_required`，task-worker 重启后生效；旧任务因 config revision 漂移在 Gateway 前拒绝。
- **F1-G**：正式网页与飞书路径、全量/安全/eval、exact-SHA 深审。
- **F1-H**：按设计 §14.2 另写现场计划并取得现场 GO。

## 自检记录

- 设计 v9 §13 十一项：本 PR 全部覆盖；ADR-005 按设计 §11.3 窄化，不新建 ADR-005 文件。
- 设计 v9 §14.1 行为测试：分布在 Task 1–14 的 Step 1；§14.2 属 F1-H。
- 复用而非新建：渠道提交、交互接受与路由、Resolver、SlotVerifier、ClarificationRecord 与澄清子任务、ExecutionDisclosure、Runner、Reflection、Render、TaskStore 事务；没有单独 SQL 页面、旁路入口或第二套路由。
- 字段名：判别字段只用 `input_kind`；列定义统一为 `ColumnSpec(ordinal, name, type)`；SQL 提交唯一入口为 `TaskStore.submit_sql_query`；排序沿用现有 `dispatch_sort_key`。
