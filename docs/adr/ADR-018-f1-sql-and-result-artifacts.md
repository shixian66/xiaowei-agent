# ADR-018：F1 SQL artifact、结果 artifact 与结果访问边界

- 状态：Proposed（F1-0，待项目负责人接受；接受前不得写 F1 行为源码）
- 日期：2026-09-27
- 决策人：项目负责人
- 设计真源：[F1 受治理只读查询设计](../superpowers/specs/2026-09-27-f1-starrocks-readonly-query-design.md) v8（精简版）
- 相关：[ARCHITECTURE.md](../../ARCHITECTURE.md) §4/§6/§7.3/§9、
  [ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、
  [ADR-009](ADR-009-plan-hash-approval-binding-and-tool-admission.md)、
  [ADR-010](ADR-010-m5-durable-attempt-and-compose-boundary.md)、
  [ADR-012](ADR-012-m6b-target-bound-starrocks-readonly-adapter.md)、
  [ADR-013](ADR-013-m7-channel-boundary.md)、
  [ADR-017](ADR-017-intelligent-interaction-and-clarification.md)

## 背景

F1 让已认证用户在 Web 显式 SQL 页面提交一条原始 SQL，在单个 StarRocks target 上只读执行，并保存有界预览。
当前 `TaskSubmission` 只能持久化 `RequestEnvelope`，也没有结果表、结果 ACL 或保留策略。若把 SQL 原文或结果行
塞进 TaskStore、Plan、Evidence 或 RenderPayload，会破坏“单一真源”“结果不进小 payload”和“渠道只投影引用”三条
边界。本 ADR 冻结 F1 新增的两类 artifact。负责人要求最小化实现，因此采用一步提交、一次读完再存，不引入
草稿确认、流式写入或结果封存状态机。

## 决策

### D1 SqlArtifact 是 SQL 原文的唯一保存点

- `SqlArtifactStore` 保存原始 UTF-8 bytes（1..65_536 bytes）、SHA-256、requester、tenant、environment、resource_id、
  target fingerprint、config revision、created_at 与 expires_at；
- `sql_ref` 由 CSPRNG 生成、不可枚举；
- TaskStore、PlanStore、Evidence、RenderPayload、ChannelStore、trace、audit 与日志只保存 `sql_ref`、`sql_hash` 或
  闭集决定，不保存 SQL 原文。

### D2 TaskSubmission 以 `input_kind` 判别

| 类型 | 字段 |
| --- | --- |
| `ConversationSubmission` | `input_kind: Literal["conversation"] = "conversation"`、`envelope`、`context`、`as_of`、`clarification_parent_task_id` |
| `ArtifactSubmission` | `input_kind: Literal["sql_artifact"]`、`context`、`as_of`、`sql_ref`、`sql_hash`、`resource_id`、`result_ref` |

现有 `TaskSubmission` 类显式改名为 `ConversationSubmission` 并更新全部构造点；`TaskSubmission` 只作为两者的
union 类型名。缺少 `input_kind` 的输入被判别 union 拒绝，旧数据兼容**只**靠下述 migration 回填，不设无标签
兼容层。`context` 与 `as_of` 是两类提交共有的执行上下文与提交时间；除此之外 `ArtifactSubmission` 不含 SQL
原文、用户自然语言或 envelope。不建第二套任务系统或第二张提交表。

**存储与迁移（一个 migration，同一 `task_submissions` 表）：**

1. 新增 `input_kind TEXT NOT NULL`：先以 `server_default='conversation'` 加列回填全部旧行，再移除默认值；
   `CHECK (input_kind IN ('conversation','sql_artifact'))`；
2. `envelope` 改为可空；新增可空列 `sql_ref`、`sql_hash CHAR(64)`、`resource_id`、`result_ref`；
3. 形状约束 `ck_task_submissions_shape`：`conversation` 行 `envelope` 非空且四个 artifact 列全空；`sql_artifact` 行
   `envelope` 与 `clarification_parent_task_id` 为空且四个 artifact 列全非空；`result_ref` 唯一；
4. 读取按 `input_kind` 分派；其他值或违反形状的行 fail-closed；
5. downgrade：存在任一 `sql_artifact` 行时拒绝执行并报错；否则删除新增列与约束，恢复 `envelope NOT NULL`。

**digest 兼容：**`conversation` 的 `submission_digest`、`request_dedup_digest` 与 `idempotency_scope_digest` 的规范
输入**逐字节不变**，旧行无需重算，固定向量测试承重。`sql_artifact` 的 `submission_digest` 覆盖 `input_kind`、
`context`、`as_of` 与四个引用；`request_dedup_digest` 覆盖 tenant、environment、actor、`input_kind`、`resource_id`、
SQL SHA-256 与幂等键；`idempotency_scope_digest` 在原三项外加入 `input_kind`，使 SQL 提交与对话提交的幂等键
分属不同作用域。

### D2a 一步提交是一个事务

`TaskStore.submit_sql_query` 是 F1 唯一的提交入口，在**同一个 PostgreSQL 事务**内完成；SqlArtifactStore 与结果
存储不提供公开的提交方法，事务内的表写入只是共享同一连接的私有 helper，不引入通用事务框架：

1. 以事务级 advisory lock 串行化同一 requester 与同一 target 的配额计数，重验 5 / 50 存活上限；
2. 同一幂等键已提交相同内容时返回已有 winner（`task_id`、`result_ref`）；内容不同则拒绝；
3. 写 SqlArtifact；
4. 建 `tasks` 行与 `sql_artifact` 形状的 `task_submissions` 行；
5. 建 `pending` 结果行；
6. 写 `requester_owner` grant。

任一步失败整体回滚；提交结果未知时按现有 not-confirmed 语义回读 winner，不重复创建。

### D3 结果一次读完，随步骤结果同事务可读

- `result_ref` 由 CSPRNG 生成、不可枚举，提交时创建；
- 列按 ordinal 保存为 `ColumnSpec(ordinal, name, type)`，同名列不得合并；行按 ordinal 对齐，使用带类型标签的
  规范编码，Decimal/日期时间/大整数不经 float；
- `storage_state` 闭集为 `pending`、`available`、`failed`、`expired`；`completeness` 为 `complete`、`truncated_rows`、
  `truncated_bytes`；只有 `available` 可读；
- adapter 把结果写入 Runner 创建的进程内 `QueryResultBuffer`（≤ 1000 行、≤ 20 MiB，不存半行）；Gateway 超时或
  取消时关闭 buffer，迟到写入被丢弃；
- 步骤成功时 `commit_step_result` 在提交步骤结果、Evidence 与审计的同一事务里写入列与行并置为 `available`；
  失败或超时时同一事务置为 `failed`；提交前崩溃时结果保持 `pending`，永不可读。

### D4 配额与保留

- 单 requester 存活 query set ≤ 5，单 target ≤ 50；每组一份 SQL 与一份结果；
- SQL 与结果在任务终态后 24 小时过期；无终态的 `pending` 孤儿创建后最多 24 小时；过期即拒绝读取，retention
  删除 bytes、列、行、grant 与活动索引；
- 长期只保留 hash、actor、target、时间、上限、Guard/Policy 决定、query id 与资源指标；
- PostgreSQL DELETE 不证明物理擦除；数据处置证据属于 F1-H。

### D5 结果 ACL 与锁定页

- 新增 `result_access_grants`；F1 提交时只写 `requester_owner` grant；
- grant 绑定 result_ref、principal、grant_kind、approval_ref、created_at、expires_at；只有 `requester_owner` 的
  `approval_ref` 可为空；
- F1 不写 `view_approver` 或 `export_approver`；Admin 角色本身不产生 grant；
- `/results/{result_ref}` 锁定页只显示状态、target 展示名、时间、上限、保存行/字节数、截断标记、过期时间、
  “详细结果尚未获 F2 查看审批”提示，以及 requester 本人提交的 SQL；不返回列名、单元格、数据摘要或导出链接；
  not-found、过期与越权使用同一隐藏式拒绝；
- `export_policy` 在 F1 固定为 `disabled`。

### D6 F2/F3 的消费契约

- F2 只读取 `available` 结果分页展示，翻页不访问 StarRocks；
- F3 在 TTL 内校验原 SQL hash、target fingerprint、requester 与有效 `sql_ref` 后，在新连接中重新执行原 SQL，
  不导出 F1 预览，也不继承 F1 的 `sql_select_limit`；F3 所需的流式写入由 F3 单独设计；
- 任何 approver grant、结果行展示或导出，都必须先满足未来 ADR-005 与各自获批的 F2/F3 设计。

## 后果

- SQL 原文与结果行各只有一个受保护存储，渠道、任务与证据层只持有引用；
- 结果在步骤提交的同一事务里可读，不需要 staging、封存或写入 fencing；代价是进程内存临时持有 ≤ 20 MiB 结果；
- 一步提交少一次确认，执行前页面仍展示完整 SQL、目标与上限；
- F1 锁定页不写 approver grant、不展示列与行、不提供导出，因此**不依赖 ADR-005**，可以先于 ADR-005 交付；
  结果行展示、approver grant 与导出仍被 ADR-005 和 F2/F3 阻塞。

## 备选方案与否决理由

- **把 SQL 放进 `RequestEnvelope.text` 或 `ToolCall.typed_args`**：前者进入对话链并被长期保存，后者只允许 JSON
  标量且进入 hash/trace；
- **结果行放进 `AdapterResponse.payload` / Evidence**：突破小结果边界，并让结果进入长期事实；
- **按列名保存行 map**：同名列会互相覆盖；
- **边查边存（v7）**：需要线程桥、写入 fencing 与三态封存；F1 预览只有 1000 行，收益不抵复杂度，留给 F3；
- **先草稿再确认（v7）**：多一张表、一次请求和一个原子确认命令；页面在执行前已展示完整信息；
- **F1 就写 approver grant**：等于在 ADR-005 前定义审批语义。

## 变更门

以下任一变化必须先修订本 ADR：SQL 或结果的保存位置、`input_kind` 字段名与存储形状、一步提交的事务范围、
storage_state 或 completeness 闭集、配额/保留数值、ACL grant 种类、锁定页可见字段、export_policy，或 F2/F3 消费契约。

## 回滚

F1-1 之前回滚只需撤回本 ADR 与相关文档修订。F1-1 之后关闭 F1 capability 注册与路由即可停止新 artifact；既有
artifact 由 retention 在 24 小时内清理；migration 按 D2 的 downgrade 规则处理。
