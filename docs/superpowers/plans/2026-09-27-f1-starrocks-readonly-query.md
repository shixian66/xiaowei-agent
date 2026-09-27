# F1 StarRocks 受治理只读查询 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> 状态：Draft V0.2（F1-0，2026-09-27）。V0.2 按 PR #109 复审（SHA `f5e4a99`）修订：ADR-005 口径统一、
> TaskSubmission/迁移/原子确认写死、调度公平排序改用真实可迁移字段、B2 目标形状与权限说明。
> V0.3 按复审（SHA `970e6d6`）收敛：确认只有 `TaskStore.confirm_sql_artifact` 一个入口；`created_at` 接入
> `TaskRecord` 与现有 `dispatch_sort_key`；旧提交兼容只靠显式改名与 migration 回填。待 exact-SHA 复审与负责人接受。
> 本计划获批**不**等于任何切片开工：F1-0b 与 F1-1 起每个切片都需负责人明确开工口令；真实 StarRocks
> 调用只属 F1-H，另需现场计划与现场 GO。当前进度只看 [当前状态](../../../AGENT_HANDOFF.md#current-status)。

**Goal:** 让已认证用户在 Web 显式 SQL 模式提交并确认一条原始 SQL，经唯一安全链在单个 StarRocks target 上只读执行，保存有界预览并返回锁定结果页。

**Architecture:** 复用现有 TaskStore、CapabilityResolver、PlanCompiler、WorkflowRunner、StepAdmission、ToolGateway 主链；新增 SQL/结果两类 artifact（ADR-018）、`confirmed_readonly` SQLGuard profile 与版本化 `ReadonlyStatementRegistry`、target slot 调度租约和 Gateway 管理的流式结果 sink。SQL 原文只在 SqlArtifactStore 与进程内 HydratedQuery 中出现。

**Tech Stack:** Python 3.11、Pydantic、SQLAlchemy async + PostgreSQL、Alembic 风格 migration（`persistence/migrations/versions/rev_00NN_*.py`）、sqlglot 30.17.0、PyMySQL 1.2.0（SSCursor）、Starlette/ASGI、pytest（`security` marker）、Ruff、mypy。

## Global Constraints

- 设计真源：[F1 受治理只读查询设计](../specs/2026-09-27-f1-starrocks-readonly-query-design.md) Approved v7；本计划不复述其决定，只写实施与验证。冲突时以设计与 ADR 为准，并先报告冲突。
- 真源 ADR：[ADR-018](../../adr/ADR-018-f1-sql-and-result-artifacts.md)，以及 ADR-007/009/012/013/017 的“F1 修订”。
- 保存预览行 1000；保存预览字节 20 MiB；server query timeout 180 秒；target slot 累计等待 300 秒；单 target 活跃查询 1；SQL 语句数 1；SQL 原文 1 MiB UTF-8（1_048_576 bytes）；单 requester 存活 query set 5；单 target 存活 query set 50。
- Admin 可调低：`preview_max_rows` 1..1000（默认 1000）；`preview_max_bytes` 1 MiB..20 MiB（默认 20 MiB）；`query_timeout_seconds` 1..180（默认 180）。
- timeout 层次：connect/write ≤ 10 秒且 ≤ read；session `query_timeout=Q`；PyMySQL `read_timeout=Q+10`；Gateway `Q+20`；slot lease TTL `Q+30`；ToolCall ≤ 300 秒。
- slot BUSY 退让固定：`next_attempt_at = min(now + 5 秒, 当前 slot expires_at, wait_deadline)`。
- chunk 最多 100 行；`sql_select_limit = effective_preview_max_rows + 1`。
- `ToolCall.typed_args` 只允许 JSON 标量；`api_request_body_limit_bytes`（默认 131_072，约束 < 1_048_576）不改。
- 锁定依赖：`sqlglot==30.17.0`、`PyMySQL==1.2.0`（以 `uv.lock` 为准，升级需重跑 §14.3 解析门与 registry 测试）。
- 未登记只读语句：`error_code=READONLY_STATEMENT_NOT_SUPPORTED`、用户文案“暂未支持”、`retryable=false`、零 SQL 发送、target 保持 enabled。
- 不支持：外部 Catalog、table function、UNNEST、UDF/qualified function、任何 hint、多语句、INTO OUTFILE、写/DDL/锁/事务/session 改写。
- 触及 `governance/`、`planning/`、`tools/` 的每个 PR 收尾必须跑四门：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。
- 行为变更 TDD：先看到目标原因失败（不是导入/夹具失败），再最小实现；关键保护做隔离变异，用 `PYTHONDONTWRITEBYTECODE=1` 或独立 pycache。

---

## 0. 阶段门与切片

| 切片 | 交付 | 进入条件 | 退出证据 |
| --- | --- | --- | --- |
| F1-0a（本 PR） | 设计 §13 的真源/ADR 修订、本计划、首批 registry 候选清单 | 设计已批准（PR #108） | 文档契约测试、四门、exact-SHA 复审；负责人接受 ADR-018 与各 F1 修订 |
| F1-0b | 契约与内核前置（Task 1–10，含 Task 2A）：DTO、migration、store、原子确认、调度租约、SQLGuard profile、registry、body policy、流式 sink、ResourceSnapshot | F1-0a 获批 + 负责人开工口令 | 各任务测试、四门、exact-SHA 复审；慢查询全部原回归通过 |
| F1-1 | Web 直接 SQL 闭环（Task 11–16）：capability、Runner 流程、提交/确认、锁定页、retention、PyMySQL streaming adapter（离线） | F1-0b 合入 + 开工口令；`/results` 开放前满足 §3 对照 | 正式 Web 路径成功对照与关键拒绝、PostgreSQL 集成、故障注入、安全矩阵与变异 |
| F1-2 | Admin 预算与关系黑名单 | F1-1 合入 + 开工口令 | Admin 保存/diff/二次确认、restart_required、loaded receipt、漂移拒绝 |
| F1-3 | 飞书只投影可信链接 | F1-1 合入 + 开工口令 | 飞书消息中的 SQL 零进入、深链鉴权先于读取 |
| F1-G | 离线总验收 | F1-1–F1-3 合入；负责人确定跨任务延迟 SLO | 正式 Web 路径、全量/安全/eval、exact-SHA 深审 |
| F1-H | 真实 target 现场验证 | 单独现场计划 + 现场 GO | 设计 §14.4 十七项；最高 `tests + test-env verified` |

**关于设计 §15 的 F1-0 范围：** 设计 §13 要求“F1-0 先形成一个文档/ADR PR，该 PR 未批准前不得写 F1 行为源码”，§15 又把契约前置列在 F1-0。本计划据此把 F1-0 拆为 F1-0a（文档，本 PR）与 F1-0b（契约与内核源码）。F1-0b 仍需负责人在接受本 PR 时或之后明确给出开工口令，不由本计划自我授权。

## 1. 首批 ReadonlyStatementRegistry 清单（请负责人确认）

设计 §7.3 表是**必选**最低范围。“常用只读语句”由本计划提出候选，负责人在复审本 PR 时确认或删减；未入选语句上线后返回“暂未支持”，以后可增量登记。

**分类证据（只读推理 + 本地 sqlglot 30.17.0 离线 probe，未连接 StarRocks）：**

- 走 AST 路径、无需登记（sqlglot 解析为 `Show`/`Describe`/查询根）：SHOW DATABASES、SHOW TABLES / FULL TABLES、SHOW COLUMNS / FULL COLUMNS、SHOW INDEX、SHOW TABLE STATUS、SHOW CREATE TABLE / VIEW / DATABASE、SHOW PROCESSLIST / FULL PROCESSLIST、SHOW VARIABLES、SHOW STATUS、SHOW GRANTS、SHOW ENGINES、SHOW CHARSET、SHOW COLLATION、SHOW PLUGINS、SHOW WARNINGS、DESC/DESCRIBE、EXPLAIN / EXPLAIN ANALYZE 查询。
- 反例提醒：`HELP 'show'` 被 sqlglot 解析为 `Alias`，不是允许的根节点，必须拒绝。

**A. 必选（设计 §7.3 表，sqlglot 降级为 Command 或 ParseError）：**

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

**B. 建议入选的常用只读语句（sqlglot 30.17.0 均降级为 Command，需 descriptor）：**

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

独立复审建议 B1–B9 全部纳入首批；负责人在接受本 PR 时确认。

**登记支持不等于给账号增权。** 部分语句需要只读 SELECT 之外的权限：SHOW COMPUTE NODES 需要 OPERATE / cluster_admin，
SHOW ROLES 需要 user_admin，SHOW ROUTINE LOAD 通常需要目标表 INSERT 权限，ADMIN SHOW REPLICA 需要额外权限。
F1 可以识别并发送这些语法，但专用只读账号收到 StarRocks 权限错误是预期行为，按结构化执行错误返回；不得为了
跑通而给 F1 账号增权。F1-H 第 10 项记录每条语句在专用账号下的实际结果。

**建议暂不入选（上线后“暂未支持”，按需再登记）：** SHOW USERS、SHOW RESOURCES、SHOW STORAGE VOLUMES、SHOW WAREHOUSES、SHOW PIPES、SHOW STREAM LOAD、SHOW BROKER、SHOW REPOSITORIES、SHOW BACKUP、SHOW RESTORE、SHOW PROPERTY、SHOW HISTOGRAM META、SHOW VIEWS（目标版本是否支持待 F1-H 确认）。

每个 descriptor 的具体 token grammar 与各版本输出形状，在 Task 7 以离线正反例冻结，在 F1-H 第 10 项以真实版本区间下界、上界和一个代表版本验证。

## 2. 已知 P2 与处理位置

| P2 | 处理 |
| --- | --- |
| 已退让的 F1 任务按 `created_seq` 占满 `dispatch_batch_limit=10`，更新的其他能力任务可能长期拿不到批次 | Task 12 先写红测：10 个以上到期的 F1 退让任务 + 1 个更新的普通任务，普通任务必须在有限轮内被执行；修复写死为：Task 3 的 migration 给 `tasks` 新增 `created_at TIMESTAMPTZ NOT NULL`（加列时 `server_default=now()` 把全部旧行回填为同一迁移时刻，旧行之间仍按 `created_seq` 保持现有先后；保留该数据库时钟默认值给新行）；`list_dispatchable_tasks` 改为按 `COALESCE(next_attempt_at, created_at), created_seq, task_id` 排序；字段同时接入 `TaskRecord`、`schema.py`、`rows.py` 的 row mapper、PostgreSQL 与内存 store；直接扩展现有 `persistence/decisions.py` 的 `dispatch_sort_key(record)` 为 `(COALESCE(next_attempt_at, created_at), created_seq, task_id)`，不新增第二个排序函数；PostgreSQL 写等价 SQL 排序表达式，共享 suite 断言两边顺序一致。每次退让把 `next_attempt_at` 推后，因此任一到期的新任务最多等待有界轮数；downgrade 删除该列与排序表达式 |
| 每次退让后重新水合与准入会在 event loop 上重新解析最大 1 MiB SQL | Task 7 的解析门同时测 event-loop 阻塞时长；Task 12 让 SQLGuard 在 `asyncio.to_thread` 中执行，并加“解析期间 heartbeat 仍按时续租”的测试。顺序仍保持设计 §8.4：inspect → 水合/准入 → 窗口 → 争抢，不为省解析调换顺序 |

## 3. Web 规格 §11.2 六项对照

| 前置 | 负责任务 | 验收证据 |
| --- | --- | --- |
| 1. 稳定 result_ref 与有界 artifact | Task 2、Task 9、Task 14 | CSPRNG 引用；staging/sealed/available 迁移与故障集成测试；1000/1001 行、20 MiB 边界 |
| 2. requester/approver/状态/有效期/导出规则唯一真源 | Task 2、Task 14 | `result_access_grants` 只写 `requester_owner`；approver grant 写路径不存在；`export_policy=disabled` |
| 3. ADR-005 | 本 PR 按设计 §13 第 7 项窄化，设计 §11.3 第 3 行、Web 规格 §11.2 第 3 项与 ADR-018 同一结论：F1 锁定页**不依赖 ADR-005**；approver grant、结果行展示与导出仍必须先满足 ADR-005 | 锁定页不写 approver grant、不返回列/行/导出；安全测试断言页面与 API 字段闭集 |
| 4. ADR-013 深链只投影引用 | 本 PR（ADR-013 F1 修订）、Task 14、F1-3 | RenderPayload/ChannelStore 无 SQL、列、行；深链鉴权先于读取 |
| 5. 保留、脱敏、分页、导出、失效 | Task 15；分页归 F2，导出归 F3 | 24 小时三类过期、隐藏式拒绝、retention 删除范围；数据处置现场证据归 F1-H |
| 6. 独立里程碑、计划与真实调用/数据处置授权 | 本计划 §0 | 各切片开工口令与 F1-H 现场 GO 分离记录在 handoff |

`/results/{result_ref}` 路由只在 Task 14 验收通过后注册；空页或 fake rows 不算交付。

## 4. 文件结构

| 文件 | 责任 | 任务 |
| --- | --- | --- |
| `src/xiaowei_agent/contracts/task.py` | `TaskSubmission` union：`ConversationSubmission` / `ArtifactSubmission` | 1 |
| `src/xiaowei_agent/contracts/sql_artifact.py`（新） | `SqlArtifact`、`DirectSqlDraft`、`HydratedQuery`、`ReadonlyQueryBudget` | 1 |
| `src/xiaowei_agent/contracts/result_artifact.py`（新） | `ColumnSpec`、`ResultChunk`、`ResultArtifactMeta`、`StorageState`、`Completeness`、`ResultGrant` | 1 |
| `src/xiaowei_agent/contracts/capability.py` | `QueryRequirement` 闭集与 `OperationSpec.query_requirement` | 1 |
| `src/xiaowei_agent/persistence/migrations/versions/rev_0019_f1_artifacts.py`（新） | `task_submissions.input_kind` 与形状约束、`tasks.created_at`、SQL/结果/ACL/slot lease/等待窗口列与约束（一个 migration，按任务分步补全，合入前只存在一个 head） | 2、2A、3 |
| `src/xiaowei_agent/persistence/sql_artifacts.py`（新） | `SqlArtifactStore` Protocol + PostgreSQL + 内存实现 | 2 |
| `src/xiaowei_agent/persistence/result_artifacts.py`（新） | `ResultArtifactStore` Protocol + 实现（chunks、fencing、seal、activation、grants） | 2 |
| `src/xiaowei_agent/persistence/target_slots.py`（新） | `TargetQueryLeaseStore` + `TargetSlotGrant` | 3 |
| `src/xiaowei_agent/persistence/decisions.py` | 共用 step 执行分类函数、deferral 判定 | 3 |
| `src/xiaowei_agent/persistence/store.py`、`postgres.py`、`fake.py` | submission 按 `input_kind` 分派与 digest；`confirm_sql_artifact`；`inspect_step_execution`、`ensure_slot_wait_window`、`check_slot_wait_deadline`、`schedule_deferral`；dispatch 排序 | 2、2A、3 |
| `src/xiaowei_agent/governance/sql_tokens.py`（新） | quote-aware lexer：语句边界、注释类别、token 字节区间 | 4 |
| `src/xiaowei_agent/governance/sqlguard.py` | `confirmed_readonly` profile 入口与 `ReadonlyProof` 统一后置检查（`template_locked` 不变） | 5、6 |
| `src/xiaowei_agent/governance/readonly_registry.py`（新） | `ReadonlyStatementRegistry`、descriptor、版本区间、digest | 7 |
| `src/xiaowei_agent/interfaces/body_limit.py` | 路由 body policy 表（JSON 原上限 + `application/sql` 1 MiB） | 8 |
| `src/xiaowei_agent/tools/result_sink.py`（新） | `ResultChunkSink`、`ThreadsafeResultChunkWriter`、`StreamingToolAdapter` Protocol | 9 |
| `src/xiaowei_agent/tools/gateway.py` | `invoke(..., hydrated_query=, slot_grant=)` 窄扩展与 streaming 分支 | 9 |
| `src/xiaowei_agent/tools/starrocks_query_fake.py`（新） | fake streaming adapter（行数/字节/慢写/超时注入） | 9 |
| `src/xiaowei_agent/contracts/resource_config.py` | `StarRocksResource` F1 字段；`ResourceSnapshot`；`F1TargetDirectory` | 10 |
| `src/xiaowei_agent/capabilities/readonly_query.py`（新）、`planning/starrocks/readonly_query.py`（新） | capability spec、`ReadonlyQueryParams`、planner | 11 |
| `src/xiaowei_agent/runners/deterministic.py`、`runners/runner.py` | `confirmed_artifact` 水合、slot 顺序、`WorkflowDeferred` | 12 |
| `src/xiaowei_agent/application/runtime.py`、`worker.py`、`task_heartbeat.py` | ArtifactSubmission 窄方法；透传与捕获 `WorkflowDeferred` | 12 |
| `src/xiaowei_agent/application/query_submission.py`（新） | `QuerySubmissionService`（草稿、确认） | 13 |
| `src/xiaowei_agent/interfaces/web_app.py` + `web_static` | `POST /app/api/sql-drafts`、确认、SQL 页、锁定结果页 | 13、14 |
| `src/xiaowei_agent/application/result_access.py`（新） | 锁定页只读投影与隐藏式拒绝 | 14 |
| `src/xiaowei_agent/application/artifact_retention.py`（新） | 24 小时 retention worker | 15 |
| `src/xiaowei_agent/tools/starrocks_query.py`（新） | PyMySQL SSCursor streaming adapter（离线实现） | 16 |
| `tests/unit/`、`tests/contract/`、`tests/security/`、`tests/integration/`、`tests/evals/` | 各任务测试 | 全部 |

---

## F1-0b：契约与内核前置

### Task 1: F1 契约 DTO

**Files:**
- Modify: `src/xiaowei_agent/contracts/task.py`、`src/xiaowei_agent/contracts/capability.py`
- Create: `src/xiaowei_agent/contracts/sql_artifact.py`、`src/xiaowei_agent/contracts/result_artifact.py`
- Test: `tests/unit/test_f1_contracts.py`、`tests/security/test_f1_contract_boundaries.py`

**Interfaces:**
- Produces:
  - `class QueryRequirement(StrEnum): NONE="none"; TEMPLATE_LOCKED="template_locked"; CONFIRMED_ARTIFACT="confirmed_artifact"`；`OperationSpec.query_requirement: QueryRequirement = QueryRequirement.NONE`
  - 字段名以 ADR-018 D2 为唯一真源，判别字段只叫 `input_kind`：
  - 现有 `TaskSubmission` 类**显式改名**为 `ConversationSubmission`，机械更新全部构造点（当前 25 处 `TaskSubmission(`），不保留同名别名或无标签兼容层
  - `class ConversationSubmission(Contract)`：`input_kind: Literal["conversation"] = "conversation"` + 现有 `envelope`、`context`、`as_of`、`clarification_parent_task_id`
  - `class ArtifactSubmission(Contract)`：`input_kind: Literal["sql_artifact"]`、`context: RequestContext`、`as_of: AwareDatetime`、`sql_ref: StrictStr`、`sql_hash: Sha256Hex`、`resource_id: StrictStr`、`result_ref: StrictStr`、`confirmation_ref: StrictStr`
  - `TaskSubmission = Annotated[ConversationSubmission | ArtifactSubmission, Field(discriminator="input_kind")]`
  - `class ReadonlyQueryBudget(Contract)`：`preview_max_rows: int (1..1000)`、`preview_max_bytes: int (1_048_576..20_971_520)`、`query_timeout_seconds: int (1..180)`
  - `@dataclass(frozen=True, slots=True) class HydratedQuery`：`sql_ref`、`sql_hash`、`sql_bytes: bytes`、`result_ref`、`resource_id`、`target_fingerprint`、`config_revision`、`confirmation_ref`、`budget`；`__post_init__` 校验 `sha256(sql_bytes).hexdigest() == sql_hash`；`__repr__` 不含 bytes；`__reduce__` 抛错（不可 pickle）
  - `ColumnSpec(ordinal, name, type)`：`ordinal: int ≥ 0`、`name: str`、`type: str`（ADR-018 D3 同名）；`StorageState`、`Completeness`、`ResultGrantKind` 闭集（值与 ADR-018 D3/D5 一致）

- [ ] **Step 1: 写失败测试**：`TaskSubmission` union 对缺少 `input_kind` 的输入按 Pydantic 2.13.5 行为拒绝（`union_tag_not_found`），旧数据只经 Task 2 的 migration 回填；直接构造 `ConversationSubmission(...)` 得到 `input_kind="conversation"`；conversation 的三个 digest 固定向量逐字节不变；`ArtifactSubmission` 无 SQL 原文字段且 `extra="forbid"`；`HydratedQuery` hash 不符构造失败、`repr` 与 `pickle.dumps` 不泄漏 bytes；`ToolCall(typed_args={"sql": b"..."})` 仍被拒绝；`OperationSpec` 缺省 `query_requirement` 为 `NONE`；`ReadonlyQueryBudget` 1001 行、180+1 秒、20 MiB+1 被拒。
- [ ] **Step 2: 运行确认因缺失符号/断言失败**：`python -m pytest tests/unit/test_f1_contracts.py tests/security/test_f1_contract_boundaries.py -q`
- [ ] **Step 3: 最小实现**上述 DTO；同一提交内把全部旧 `TaskSubmission(` 构造点改为 `ConversationSubmission(`，类型注解改用 `TaskSubmission` union。
- [ ] **Step 4: 运行相关测试与 `tests/unit/test_hash_vectors.py`**，确认 request/plan 固定向量未变。
- [ ] **Step 5: 提交** `feat(contracts): add F1 submission, query and result DTOs`

### Task 2: SQL/结果 artifact 存储与迁移

**Files:**
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0019_f1_artifacts.py`、`src/xiaowei_agent/persistence/sql_artifacts.py`、`src/xiaowei_agent/persistence/result_artifacts.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`、`store.py`（digest 按 `input_kind` 分派）、`postgres.py`、`fake.py`（submission 读写按 `input_kind` 分派）
- Test: `tests/suites/sql_artifact_store.py`、`tests/suites/result_artifact_store.py`（内存与 PostgreSQL 共用）、`tests/integration/test_f1_artifacts_postgres.py`、`tests/integration/test_migration_paths.py`

**Interfaces:**
- Produces:
  - `SqlArtifactStore.create_draft(*, draft: DirectSqlDraft, target: ResolvedTarget, config_revision: str, now) -> SqlArtifactRef`
  - `SqlArtifactStore.load_for_execution(*, sql_ref, task_id, principal, tenant_id, environment_id, now) -> SqlArtifactRecord`
  - `ResultArtifactStore.write_chunk(*, result_ref, task_id, step_id, task_fencing, result_fencing, seq, rows) -> ChunkWriteResult`
  - `ResultArtifactStore.abort(...)`、`seal(...)`、`activate(*, result_ref, committed_step)`、`read_locked_view(*, result_ref, principal, now)`
  - 草稿创建时的配额检查：requester ≤ 5、target ≤ 50 存活 query set
  - 确认**没有**公开的 Store 方法：唯一入口是 Task 2A 的 `TaskStore.confirm_sql_artifact`；SQL、结果与 grant 表在确认中的写入只作为共享同一连接/事务的私有数据库 helper，不新增 UnitOfWork 或通用事务框架

- [ ] **Step 1: 写失败测试**：按 ADR-018 D2 的 migration 规则逐条断言——旧行回填 `input_kind='conversation'` 后按原 schema 读回且三个 digest 不变；加列后默认值已移除；`ck_task_submissions_shape` 拒绝 conversation 行带 artifact 列、sql_artifact 行带 envelope 或 clarification parent、缺任一引用列；未知 `input_kind` fail-closed；存在 sql_artifact 行时 downgrade 报错、无此类行时 downgrade 恢复 `envelope NOT NULL`；草稿 24 小时过期；草稿创建时 requester 第 6 个、target 第 51 个存活 set 被拒；chunk 在 abort 后以旧 result fencing 写入被拒；未 seal 不能 activate；未提交 step 不能 activate；同名列按 ordinal 往返；只有 `available` 可读。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/integration/test_f1_artifacts_postgres.py tests/integration/test_migration_paths.py -q`
- [ ] **Step 3: 实现 migration 与两个 store**；表名 `sql_artifacts`、`result_artifacts`、`result_columns`、`result_chunks`、`result_access_grants`；`result_access_grants` 用 CHECK 约束保证只有 `requester_owner` 可空 `approval_ref`。
- [ ] **Step 4: 运行共用 suite 的内存与 PostgreSQL 两个实现**，再跑 `tests/integration` 全部。
- [ ] **Step 5: 提交** `feat(persistence): add F1 sql and result artifact stores`

### Task 2A: 原子确认命令

**Files:**
- Modify: `src/xiaowei_agent/persistence/store.py`（Protocol）、`postgres.py`、`fake.py`、`decisions.py`
- Test: `tests/suites/task_store.py`（新增用例）、`tests/integration/test_f1_confirmation_postgres.py`

**Interfaces:**
- Consumes: Task 1 DTO、Task 2 表
- Produces:
  - `TaskStore.confirm_sql_artifact(*, command: SqlConfirmationCommand) -> SqlConfirmationResult`
  - `SqlConfirmationCommand(sql_ref, principal: RequestContext, confirmation_key, expected_sql_hash, expected_target_fingerprint, expected_config_revision, budget: ReadonlyQueryBudget, trace_id)`
  - `SqlConfirmationResult(outcome: SqlConfirmationOutcome, task_id: str | None, result_ref: str | None)`；`SqlConfirmationOutcome` 闭集：`CREATED`、`REPLAYED`、`ALREADY_CONFIRMED_OTHER_KEY`、`EXPIRED`、`NOT_FOUND_OR_FORBIDDEN`、`DRIFTED`、`QUOTA_EXCEEDED`
  - `decisions.classify_sql_confirmation(...)`：内存与 PostgreSQL 共用的纯判定
  - 事务范围与步骤以 ADR-018 D2a 为准：锁草稿 → advisory lock 配额 → 幂等判定 → 建 task 与 submission → 建 `staging` 零 chunk 结果行 → 写 `requester_owner` grant → 标记草稿已消费并绑定确认键；任一步失败整体回滚

- [ ] **Step 1: 写失败测试**：同一确认键重复调用得到同一 `task_id`/`result_ref`（`REPLAYED`）；两个不同确认键并发确认同一草稿只有一个 `CREATED`，另一个 `ALREADY_CONFIRMED_OTHER_KEY`；确认幂等键与相同字面值的对话幂等键不冲突；在第 4、5、6、7 步之后分别注入异常，事务回滚后草稿仍未消费、task/submission/结果行/grant 都不存在，重试可成功；提交结果未知时回读 winner 且不重复创建；requester 第 6 个、target 第 51 个存活 set 在并发下也只放行到上限；过期、hash/config/target 漂移、跨 principal 分别得到对应闭集结果且零写入。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/integration/test_f1_confirmation_postgres.py -q`
- [ ] **Step 3: 实现**：只在现有 PostgreSQL TaskStore 事务内完成，不新增第二个任务系统或跨 Store 分步提交。
- [ ] **Step 4: 运行共享 suite 的内存与 PostgreSQL 实现**。
- [ ] **Step 5: 变异**：把第 7 步挪到独立事务，故障注入用例必须转红；还原。
- [ ] **Step 6: 提交** `feat(persistence): confirm SQL artifacts in one transaction`

### Task 3: 恢复分类、等待窗口、非阻塞退让与 target slot 租约

**Files:**
- Create: `src/xiaowei_agent/persistence/target_slots.py`
- Modify: `persistence/decisions.py`、`store.py`、`postgres.py`、`fake.py`、`rows.py`、`schema.py`、`contracts/task.py`（`TaskRecord.created_at`）、`contracts/enums.py`、migration `rev_0019`
- Test: `tests/suites/task_store.py`（新增用例）、`tests/integration/test_f1_slot_scheduling_postgres.py`、`tests/security/test_f1_no_replay.py`

**Interfaces:**
- Produces:
  - `class StepExecutionClass(StrEnum): ELIGIBLE_TO_START; ADOPT_COMMITTED_RESULT; PREVIOUS_ATTEMPT_UNCERTAIN; BUDGET_EXHAUSTED; NOT_RUNNABLE; STALE_FENCING`
  - `decisions.classify_step_execution(...) -> StepExecutionClass`（inspect 与 begin 共用唯一判定）
  - `TaskStore.inspect_step_execution(*, grant, step_id, never_replay: bool) -> StepExecutionClass`
  - `TaskStore.ensure_slot_wait_window(*, grant) -> SlotWaitWindow(wait_started_at, wait_deadline)`（数据库时钟，单次 CAS，存在即返回原值）
  - `TaskStore.check_slot_wait_deadline(*, grant) -> bool`
  - `TaskStore.schedule_deferral(*, command: DeferralCommand) -> DeferralResult`（`next_attempt_at=min(now+5s, slot_expires_at, wait_deadline)`；轮换 fencing；结束 lease；不改 `task_failure_count`、不建 StepExecutionRecord）
  - `TargetQueryLeaseStore.try_acquire(*, grant, target_fingerprint, step_id, tool_call_hash, ttl_seconds, not_after) -> TargetSlotGrant | SlotBusy`；`release(*, slot_grant, connection_closed: Literal[True])`
  - `begin_step_attempt` 对 `never_replay=True` 的未提交 started 永不返回 `PROCEED`
  - `tasks.created_at` → `TaskRecord.created_at: AwareDatetime`（`schema.py`、`rows.py`、PostgreSQL、内存 store 同步）；现有 `decisions.dispatch_sort_key(record)` 扩展为 `-> tuple[datetime, int, str]`，`list_dispatchable_tasks` 两个实现都按它排序（§2）

- [ ] **Step 1: 写失败测试**（设计 §14.2 对应条目）：两个 store 实例同 target 只有一个取得 slot；committed / 未提交 started 两种恢复在 slot 前分类且 slot 获取数为 0；wait_deadline 首次争抢前持久化，重领取与 BUSY 不重置；到期后 slot 空闲也不能获取（`not_after` 在锁事务内用数据库时钟重验）；deferral 不增加失败计数/Step attempt/预算；deferral 与 heartbeat 续租同刻完成时 winner 为退让；持久化“有 Step attempt 无 started”被 CHECK 约束拒绝。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/integration/test_f1_slot_scheduling_postgres.py tests/security/test_f1_no_replay.py -q`
- [ ] **Step 3: 实现**；把 `begin_step_attempt` 现有判定抽到 `classify_step_execution`，行为对现有能力保持逐字不变。
- [ ] **Step 3a: 调度公平**：先写 §2 的饥饿红测（12 个到期的退让 F1 任务 + 1 个更新的普通任务，普通任务在 2 轮 poll 内被领取），再按 §2 实现 `created_at` 并扩展 `dispatch_sort_key`；现有 dispatch 顺序测试保持通过。
- [ ] **Step 4: 运行** `tests/suites` 两个实现、`tests/integration/test_dispatch_and_attempts_postgres.py`、`test_concurrency_and_recovery.py`。
- [ ] **Step 5: 变异**：临时让 `begin_step_attempt` 忽略 `never_replay`，`test_f1_no_replay` 必须转红；还原。
- [ ] **Step 6: 提交** `feat(persistence): add slot wait window, deferral and target query lease`

### Task 4: quote-aware token scan

**Files:**
- Create: `src/xiaowei_agent/governance/sql_tokens.py`
- Test: `tests/unit/test_sql_tokens.py`、`tests/security/test_f1_token_scan.py`

**Interfaces:**
- Produces: `scan_sql(raw: bytes) -> TokenScan`，`TokenScan.tokens: tuple[SqlToken, ...]`，`SqlToken(kind, start_byte, end_byte)`；`TokenScanError(reason: TokenScanRejection)`，闭集含 `MULTI_STATEMENT`、`HINT_COMMENT`、`CONTROL_CHARACTER`、`AMBIGUOUS_PUNCTUATION`、`INTO_OUTFILE`、`INVALID_UTF8`、`BOM`、`EMPTY`。

- [ ] **Step 1: 写失败测试**：字符串/quoted identifier/普通注释中的 `;` 不算多语句；尾随单个 `;` 规则按设计（仅空白/注释后跟随时视为同一语句）；`/*+`、`/*!` 在字符串外拒绝、在字符串内允许；`--`、`#`、普通块注释允许；控制字符和 Unicode format 字符拒绝；中文在字符串/注释内允许，全角标点和 smart quote 在关键字/运算符/identifier 位置拒绝；字符串外 `INTO OUTFILE` 拒绝；每个 token 的字节区间可精确切回原 bytes。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/unit/test_sql_tokens.py tests/security/test_f1_token_scan.py -q`
- [ ] **Step 3: 最小实现**（单遍状态机，不用正则做安全判断）。
- [ ] **Step 4: 运行通过**；**变异**：移除 hint 检测，comment hint 恶意矩阵必须转红。
- [ ] **Step 5: 提交** `feat(governance): add quote-aware SQL token scan`

### Task 5: confirmed_readonly AST 证明路径

**Files:**
- Modify: `src/xiaowei_agent/governance/sqlguard.py`
- Test: `tests/unit/test_sqlguard_confirmed_readonly.py`、`tests/security/test_f1_sql_ast_matrix.py`

**Interfaces:**
- Consumes: `scan_sql`（Task 4）
- Produces:
  - `verify_confirmed_readonly(*, raw: bytes, expected_sha256: str, policy: ReadonlyPolicySnapshot) -> ReadonlyProof`
  - `ReadonlyProof(path: Literal["ast", "registry"], statement_family: str, relations: tuple[QualifiedRelation, ...], databases: tuple[str, ...])`
  - `ReadonlyPolicySnapshot(default_database, blocked_relation_names: frozenset[QualifiedRelation], registry: ReadonlyStatementRegistry, actual_version)`
  - 统一后置检查 `_check_proof(proof, policy)`：内部 `default_catalog`、黑名单精确命中、副作用
  - 拒绝码：`SqlGuardRejection` 增加 `READONLY_STATEMENT_NOT_SUPPORTED`、`BLOCKED_RELATION`、`EXTERNAL_CATALOG`、`TABLE_FUNCTION`、`HASH_MISMATCH`、`NOT_READONLY`

- [ ] **Step 1: 写失败测试**：`Select`/`Union`/`Intersect`/`Except` 根节点成功与写形状反例；SHOW DATABASES、SHOW DATABASES FROM default_catalog 成功，FROM 外部 Catalog 拒绝；SHOW TABLES 成功；DESC、SHOW CREATE、SHOW COLUMNS 精确命中黑名单拒绝；一段名按 default database 解析、两段 db.obj、三段 default_catalog.db.tbl 成功，其他 catalog 拒绝；CTE alias 与 derived table 不当基础关系；跨内部数据库 JOIN 成功；information_schema/sys/_statistics_ 读取成功；EXPLAIN / EXPLAIN ANALYZE SELECT 递归检查，EXPLAIN ANALYZE INSERT 拒绝；table function、UNNEST、qualified function 拒绝；`HELP 'x'`（`Alias` 根）拒绝；原文 hash 不符拒绝；Guard 从不返回改写文本。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/unit/test_sqlguard_confirmed_readonly.py tests/security/test_f1_sql_ast_matrix.py -q`
- [ ] **Step 3: 实现**：`template_locked` 路径和 `verify_sql` 签名不变。
- [ ] **Step 4: 运行通过 + 慢查询原回归** `python -m pytest tests -k "sqlguard or slow_query" -q`。
- [ ] **Step 5: 变异**：移除黑名单检查，黑名单矩阵转红；移除 hash 校验，hash 反例转红。
- [ ] **Step 6: 提交** `feat(governance): add confirmed_readonly AST proof path`

### Task 6: StepAdmission 按 query requirement 强制 profile

**Files:**
- Modify: `src/xiaowei_agent/governance/step_admission.py`、`governance/admission.py`
- Test: `tests/security/test_f1_step_admission.py`

**Interfaces:**
- Consumes: `OperationSpec.query_requirement`（Task 1）、`verify_confirmed_readonly`（Task 5）
- Produces: `admit_step(..., hydrated_query: HydratedQuery | None = None)`；requirement 为 `CONFIRMED_ARTIFACT` 时 HydratedQuery 必须存在且与 ToolCall 标量逐项一致，否则闭集拒绝；为其他值时传入 HydratedQuery 同样拒绝。

- [ ] **Step 1: 写失败测试**：required 缺失 → 拒绝；多余 → 拒绝；`sql_hash`/`result_ref`/`target_fingerprint`/`config_revision`/预算任一不一致 → 拒绝；慢查询计划伪装成 `confirmed_readonly` → profile 漂移拒绝；`tool_call_hash` 覆盖全部标量。
- [ ] **Step 2–4: 运行失败 → 实现 → 运行通过**：`python -m pytest tests/security/test_f1_step_admission.py tests/security -q`
- [ ] **Step 5: 提交** `feat(governance): enforce confirmed_artifact query requirement in admission`

### Task 7: ReadonlyStatementRegistry 与首批 descriptor；1 MiB 解析门

**Files:**
- Create: `src/xiaowei_agent/governance/readonly_registry.py`、`tests/unit/test_readonly_registry.py`、`tests/security/test_f1_registry_matrix.py`、`tests/evals/f1_parse_gate.py`
- Modify: `governance/sqlguard.py`（Command/ParseError 时查询 registry）

**Interfaces:**
- Produces:
  - `ReadonlyStatementDescriptor(family: str, grammar: tuple[GrammarPart, ...], target_kind: TargetKind, forbidden_clauses: frozenset[str])`
  - `ReadonlyStatementRegistry(profile: str, version: str, verified_min_version: StarRocksVersion, verified_max_version: StarRocksVersion, descriptors: tuple[...])`；`digest: str`（canonical JSON SHA-256）；`match(scan: TokenScan, raw: bytes) -> RegistryMatch | NotRegistered`
  - `version_in_range(actual) -> bool`（闭区间）；区间非法构造失败

- [ ] **Step 1: 写失败测试**：§1 清单 A 全部与负责人确认的 B 项，每个 descriptor 各有正常对照、大小写/空白/quoted identifier 边界、缺失或重复 clause、多语句、hint、外部 Catalog、黑名单目标、歧义目标反例；未知只读前缀与区间内未登记语法返回 `READONLY_STATEMENT_NOT_SUPPORTED`、`retryable=false`；EXPLAIN LOGICAL/VERBOSE/COSTS 的内层精确 byte slice 交 AST 证明，INSERT、INTO OUTFILE、外部 Catalog、table function、黑名单反例；版本位于下界、中部、上界可加载，越界 fail-closed；descriptor 改动导致 digest 变化。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/unit/test_readonly_registry.py tests/security/test_f1_registry_matrix.py -q`
- [ ] **Step 3: 实现 registry 与首批 descriptor**；不得出现“以 SHOW 开头即放行”的泛化规则。
- [ ] **Step 4: 变异**：对带对象的 descriptor 移除目标提取，至少一个黑名单测试转红，且确认未被更早规则遮蔽。
- [ ] **Step 5: 解析门**：`python tests/evals/f1_parse_gate.py` 用锁定 sqlglot 测 256 KiB / 512 KiB / 1 MiB 的 parse 时间、峰值内存、递归深度、恶意嵌套，以及同步解析阻塞 event loop 的时长；记录机器、版本、样本 hash 与阈值。1 MiB 不能在资源门内稳定完成时停止并请负责人重新确认上限。
- [ ] **Step 6: 提交** `feat(governance): add versioned readonly statement registry`

### Task 8: 路由 body policy

**Files:**
- Modify: `src/xiaowei_agent/interfaces/body_limit.py`、`interfaces/web_app.py`、`interfaces/api.py`（装配）
- Test: `tests/security/test_body_limit_routes.py`

**Interfaces:**
- Produces: `BodyRoutePolicy(path: str, method: str, media_type: str, limit: int)`；`JsonBodyLimitMiddleware` 改为按 policy 表精确匹配（保留现有类名或以 `RouteBodyLimitMiddleware` 替换并删旧名，二选一，不并存）。`POST /app/api/sql-drafts` 使用 `application/sql; charset=utf-8`、上限 1_048_576，不读 `api_request_body_limit_bytes`。

- [ ] **Step 1: 写失败测试**：SQL 路由 1 MiB 成功、1 MiB+1 拒绝；BOM、非法 UTF-8、空 body、`Content-Encoding` 拒绝；重复 `X-Xiaowei-Resource-Id` / `Idempotency-Key` 拒绝；原 JSON 路由仍在 128 KiB 边界拒绝；未知写路由仍被现有安全测试拒绝。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/security/test_body_limit_routes.py tests/security -q`
- [ ] **Step 5: 提交** `feat(interfaces): route-scoped body policy for raw SQL drafts`

### Task 9: Gateway 流式 sink 与线程桥

**Files:**
- Create: `src/xiaowei_agent/tools/result_sink.py`、`src/xiaowei_agent/tools/starrocks_query_fake.py`
- Modify: `src/xiaowei_agent/tools/gateway.py`、`tools/adapter.py`
- Test: `tests/unit/test_result_sink_bridge.py`、`tests/security/test_f1_gateway_streaming.py`

**Interfaces:**
- Consumes: `ResultArtifactStore`（Task 2）、`TargetSlotGrant`（Task 3）、`HydratedQuery`（Task 1）
- Produces:
  - `class StreamingToolAdapter(Protocol): def execute_streaming(self, request: StreamingAdapterRequest, writer: ThreadsafeResultChunkWriter) -> StreamingAdapterReceipt`（同步，在 `asyncio.to_thread` 内运行）
  - `ThreadsafeResultChunkWriter.write(columns | rows) -> WriteAck`：`asyncio.run_coroutine_threadsafe` 提交到主 loop，`Future.result(timeout=min(sink_write_limit, remaining_deadline))`，同时最多一个 chunk 在途
  - `StreamingAdapterReceipt(connection_closed: bool, rows, bytes, completeness, query_id, metrics)`
  - `ToolGateway.invoke(call, *, context, admission, hydrated_query: HydratedQuery | None = None, slot_grant: TargetSlotGrant | None = None)`：streaming 分支重算 hash、复核 grant，创建 sink；timeout/取消先 abort 并轮换 result fencing；只有 `connection_closed=True` 后才 seal 并释放 slot；`AdapterResponse.payload` 与 `ToolResult.data_view` 必须为空，`raw_ref=result_ref`

- [ ] **Step 1: 写失败测试**：慢 PostgreSQL 写反压 fetch（fake adapter 记录 fetch 次数）；Gateway 超时后迟到 chunk/seal 被拒；移除 result fencing 后该反例转红；abort 持久化失败时不返回成功、不 seal、不释放 slot；未确认连接关闭不释放 slot；HydratedQuery 缺失或 bytes hash 不符时 adapter 调用 0；payload/data_view 非空时 Gateway 拒绝；1000/1001 行得到 `truncated_rows`，超字节得到 `truncated_bytes`，不存半行。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_result_sink_bridge.py tests/security/test_f1_gateway_streaming.py tests/security -q`
- [ ] **Step 5: 提交** `feat(tools): add gateway-managed streaming result sink`

### Task 10: ResourceSnapshot 与 F1TargetDirectory

**Files:**
- Modify: `src/xiaowei_agent/contracts/resource_config.py`、`interfaces/local_stack.py`（装配）、`interfaces/release_preflight.py`
- Create: `src/xiaowei_agent/capabilities/f1_target.py`
- Test: `tests/unit/test_f1_resource_snapshot.py`、`tests/security/test_f1_target_resolution.py`

**Interfaces:**
- Produces:
  - `StarRocksResource` 新增可选 `f1: StarRocksF1Profile | None`，含 `tenant_id`、`credential_ref`、`default_database`、`blocked_relation_names: tuple[str, ...]`（规范化、去重，冲突/非法/单段/通配启动失败）、`verified_min_version`、`verified_max_version`、`grants_digest`、`identity_digest`、`ddl_digest`、`source_ref`、`registry_profile`、`registry_digest`、`resource_group_ref`、`budget: ReadonlyQueryBudget`
  - `ResourceSnapshot(generation, config_revision, resources)`；`F1TargetDirectory.resolve(*, context: RequestContext, resource_id: str) -> ResolvedTarget`（无默认、无猜测、无跨 tenant fallback）
  - release 装配同时启用 `XIAOWEI_STARROCKS_*` 与 F1 snapshot 时启动失败

- [ ] **Step 1: 写失败测试**：解析唯一 target；跨 tenant、disabled、未知 resource_id 拒绝；任一 F1 字段变化改变 config revision；双真源启动失败；黑名单规范化冲突启动失败。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_f1_resource_snapshot.py tests/security/test_f1_target_resolution.py -q`
- [ ] **Step 5: F1-0b 收尾四门**，并跑慢查询全部原回归；提交 `feat(resources): add F1 resource snapshot and target directory`

---

## F1-1：Web 直接 SQL 闭环

### Task 11: capability、Params 与 planner

**Files:**
- Create: `src/xiaowei_agent/capabilities/readonly_query.py`、`src/xiaowei_agent/planning/starrocks/readonly_query.py`
- Modify: `application/default_capabilities.py`、`capabilities/specs.py`
- Test: `tests/unit/test_readonly_query_planner.py`、`tests/security/test_plan_determinism.py`（新增用例）

**Interfaces:**
- Produces: `starrocks.readonly_query@1.0.0`，唯一 operation `execute_readonly_query`（READ + RESTRICTED、`query_requirement=CONFIRMED_ARTIFACT`、profile `confirmed_readonly`）；`ReadonlyQueryParams(sql_ref, sql_hash, result_ref, resource_id, config_revision, budget, confirmation_ref)`；`compile_readonly_query_plan(params) -> ExecutionPlan`（`max_steps=1`、`max_tool_calls=1`、`max_model_tokens=0`；typed_arguments 无 SQL 原文）

- [ ] **Step 1: 失败测试**：plan 无 SQL 原文；typed_arguments 与 budget 进入 `plan_hash`；预算放大被检出；该能力不出现在对话 Resolver 的模型候选中。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_readonly_query_planner.py tests/security/test_plan_determinism.py -q`
- [ ] **Step 5: 提交** `feat(capabilities): register starrocks.readonly_query`

### Task 12: Runner confirmed_artifact 流程、WorkflowDeferred 与 worker

**Files:**
- Modify: `runners/runner.py`（`WorkflowDeferred`）、`runners/deterministic.py`、`application/runtime.py`、`application/worker.py`、`application/task_heartbeat.py`
- Test: `tests/integration/test_f1_runner_flow_postgres.py`、`tests/security/test_f1_runner_order.py`、`tests/unit/test_worker_deferral.py`

**Interfaces:**
- Consumes: Task 1–11 全部
- Produces: `class WorkflowDeferred(Exception)`（控制信号，携带 `task_id`、`next_attempt_at`）；`XiaoweiRuntime.execute_artifact_submission(*, grant, submission: ArtifactSubmission, trace_id)`；worker 在 `RetryableTaskError` 之前捕获 `WorkflowDeferred` 并返回“未执行”

- [ ] **Step 1: 失败测试**（顺序与计数）：inspect → 水合/准入 → 窗口 → 争抢 → begin → Gateway；committed/未提交 started 在 slot 前分类；BUSY 时同一轮其他 capability 仍执行；deferral 不进入 Evidence/Reflection/finalize/backoff；已提交 deferral 与 heartbeat 续租失败并发时仍视为正常退让；取得 slot 后 begin 前崩溃不误判且不调用 Gateway；SQLGuard 在线程中执行时 heartbeat 按时续租；worker 端到端验证 §2 的有界等待（排序本身由 Task 3 实现）。
- [ ] **Step 2: 运行确认失败**：`python -m pytest tests/integration/test_f1_runner_flow_postgres.py tests/security/test_f1_runner_order.py tests/unit/test_worker_deferral.py -q`
- [ ] **Step 3: 实现**：水合只按 `query_requirement` 分支，不按 operation 名称判断。
- [ ] **Step 4: 通过 + 现有 Runner/worker 全部回归**：`python -m pytest tests -k "runner or worker or heartbeat" -q`
- [ ] **Step 5: 提交** `feat(runners): execute confirmed SQL artifacts with non-blocking slot deferral`

### Task 13: QuerySubmissionService 与 Web 草稿/确认

**Files:**
- Create: `src/xiaowei_agent/application/query_submission.py`、`interfaces/web_static/sql.html`（或现有壳内页面）
- Modify: `interfaces/web_app.py`、`interfaces/web_models.py`
- Test: `tests/contract/test_web_sql_draft.py`、`tests/security/test_f1_web_entry.py`

**Interfaces:**
- Produces: `POST /app/api/sql-drafts`（原始 SQL body + `X-Xiaowei-Resource-Id` + `Idempotency-Key`）→ 确认页数据（完整 SQL、target 展示名、有效预算、“原 SQL 不会被改写”）；`POST /app/api/sql-drafts/{sql_ref}/confirm`（`Idempotency-Key`，无 body SQL）→ `task_id`、`result_ref`
- 草稿阶段零 task、零 StarRocks 连接、零模型调用；handler 不构造 ResolvedTarget

- [ ] **Step 1: 失败测试**：草稿不建 task；确认重验 principal/target/config/hash/过期；同确认 key 同 task、换 key 拒绝；编辑字节产生新 sql_ref；未登录/未激活/scope 不匹配/Origin 不符拒绝；SQL 不出现在 URL、日志与 trace。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/contract/test_web_sql_draft.py tests/security/test_f1_web_entry.py -q`
- [ ] **Step 5: 正式进程 + 浏览器**检查 SQL 页与确认页（fake streaming adapter，注明替身边界）。
- [ ] **Step 6: 提交** `feat(web): add raw SQL draft and confirmation flow`

### Task 14: 锁定结果页与 ACL

**Files:**
- Create: `src/xiaowei_agent/application/result_access.py`
- Modify: `interfaces/web_app.py`、`rendering/`（深链投影）
- Test: `tests/security/test_f1_locked_result_page.py`、`tests/contract/test_f1_result_render.py`

**Interfaces:**
- Produces: `GET /results/{result_ref}` 与对应 API；字段闭集：状态、target 展示名、时间、预算、保存行/字节、截断、过期时间、F2 提示、requester 的完整 SQL；不返回列名、单元格、chunks、摘要、导出链接；not-found/过期/越权同一隐藏式拒绝

- [ ] **Step 1: 失败测试**：requester 可见；Admin、其他用户、过期 ACL 同一拒绝；响应字段集合精确等于闭集；RenderPayload 只含受保护链接。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/security/test_f1_locked_result_page.py tests/contract/test_f1_result_render.py -q`
- [ ] **Step 5: 提交** `feat(web): add locked F1 result page`

### Task 15: 24 小时 retention

**Files:**
- Create: `src/xiaowei_agent/application/artifact_retention.py`
- Modify: worker 装配
- Test: `tests/integration/test_f1_retention_postgres.py`

- [ ] **Step 1: 失败测试**：三类过期时点；过期后立即拒绝读取；删除 bytes/列/chunks/ACL/活动索引，只保留 hash、actor、target、时间、预算、决定、query id、指标；staging/sealed 孤儿被清理；日志、trace、Evidence 无 SQL/结果副本。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/integration/test_f1_retention_postgres.py -q`
- [ ] **Step 5: 提交** `feat(persistence): add 24h F1 artifact retention`

### Task 16: PyMySQL SSCursor streaming adapter（离线）

**Files:**
- Create: `src/xiaowei_agent/tools/starrocks_query.py`
- Test: `tests/unit/test_starrocks_query_adapter.py`

**Interfaces:**
- Consumes: `StreamingToolAdapter`、`ThreadsafeResultChunkWriter`（Task 9）；`StarRocksF1Profile`（Task 10）
- Produces: target-bound `StarRocksReadonlyQueryAdapter.execute_streaming`：独占 connection；设置并回读 `query_timeout=Q`、`sql_select_limit=rows+1`；版本区间与 digest preflight；执行确认的原始 bytes（发送前再算一次 SHA-256）；`fetchmany(≤100)`；提前截断/超时/取消时直接 `connection.close()`，不走会耗尽结果的 `SSCursor.close()`；`finally` 返回 `connection_closed` 回执；错误不回显 SQL 片段

- [ ] **Step 1: 失败测试**（假 connection/cursor 替身，锁定 PyMySQL 1.2.0 行为）：第 1001 行后不再 fetch；截断路径不调用 `cursor.close()`；readback 不符拒绝；版本越界 fail-closed；bytes hash 不符零发送。
- [ ] **Step 2–4: 失败 → 实现 → 通过**：`python -m pytest tests/unit/test_starrocks_query_adapter.py -q`
- [ ] **Step 5: 真实驱动集成**（C 层本地隔离 MySQL 协议容器验证 SSCursor 提前关闭行为）需负责人确认可用镜像；未确认前标“未验证”，不以单元测试替代。
- [ ] **Step 6: F1-1 收尾**：四门、正式 Web 路径成功对照与关键拒绝、设计 §14.2 变异清单（hash、required query、token scan、黑名单、registry 版本绑定、ACL、fencing、no-replay 各撤一次转红）；提交 `feat(tools): add offline StarRocks streaming query adapter`

---

## F1-2 / F1-3 / F1-G / F1-H（开工前再展开）

- **F1-2 Admin 预算与黑名单**：扩展 W4 资源页；只能调低预算；黑名单精确 `database.object`、规范化、去重、diff、删除二次确认；保存产生 generation 与 `restart_required`；task-worker 重启写 loaded receipt；旧任务因 config revision 漂移在 Gateway 前拒绝。开工前另写任务级步骤。
- **F1-3 飞书**：只投影可信 Web SQL 页面和受保护 result 链接；消息中的 SQL（含 fenced code block）零进入 QuerySubmissionService。
- **F1-G**：负责人先确定“一条运行中 F1 查询对同一 worker 其他任务的延迟 SLO”，再实测；不满足则另行设计有界并发并复审。
- **F1-H**：按设计 §14.4 另写现场计划并取得现场 GO；包括首批 registry 在版本区间下界/上界/代表版本的输出形状与权限验证。

## 自检记录

- 设计 §13 十三项：本 PR 覆盖 1–13（ADR-005 按第 7 项窄化，不新建 ADR-005 文件）。
- 设计 §14.2 行为测试：分布在 Task 1–16 的 Step 1；§14.3 在 Task 7 Step 5；§14.4 属 F1-H。
- 设计 §15 F1-0 契约前置：Task 1–10（含 2A）；F1-1：Task 11–16。
- V0.3：确认唯一入口为 `TaskStore.confirm_sql_artifact`；排序复用 `dispatch_sort_key`；无无标签兼容层。V0.2 复审修订：ADR-005 结论四处一致（设计 §11.3、Web 规格 §11.2、ADR-018、本计划 §3）；判别字段统一为 `input_kind`；`ColumnSpec(ordinal, name, type)` 统一；确认原子性见 Task 2A；公平排序字段见 §2 与 Task 3。
