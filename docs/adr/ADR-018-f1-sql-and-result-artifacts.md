# ADR-018：F1 SQL artifact、结果 artifact 与结果访问边界

- 状态：Proposed（F1-0，待项目负责人接受；接受前不得写 F1 行为源码）
- 日期：2026-09-27
- 决策人：项目负责人
- 设计真源：[F1 受治理只读查询设计](../superpowers/specs/2026-09-27-f1-starrocks-readonly-query-design.md)
  （PR #108 合并提交 `91cadd43e13a4b1178fbddcbdfd4b41bf0decb36`，设计复审 SHA
  `e2727cf117b62f9c9a23ec1ce37c046312475f5a`）
- 相关：[ARCHITECTURE.md](../../ARCHITECTURE.md) §4/§6/§7.3/§9、
  [ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、
  [ADR-009](ADR-009-plan-hash-approval-binding-and-tool-admission.md)、
  [ADR-012](ADR-012-m6b-target-bound-starrocks-readonly-adapter.md)、
  [ADR-013](ADR-013-m7-channel-boundary.md)、
  [ADR-017](ADR-017-intelligent-interaction-and-clarification.md)

## 背景

F1 让已认证用户在 Web 显式 SQL 模式提交一条原始 SQL，经确认后在单个 StarRocks target 上只读
执行，并保存有界预览。当前仓库只有小结果 `AdapterResponse.payload`，`TaskSubmission` 只能持久化
`RequestEnvelope`，也没有结果 artifact、结果 ACL 或保留策略。若把 SQL 原文或结果行塞进现有
TaskStore、Plan、Evidence 或 RenderPayload，会同时破坏“单一真源”“结果不进小 payload”和“渠道只投影
引用”三条边界。本 ADR 冻结 F1 引入的两类 artifact 及其消费契约。

## 决策

### D1 SqlArtifact 是 SQL 原文的唯一保存点

- `SqlArtifactStore` 保存用户提交的原始 UTF-8 bytes（≤ 1_048_576 bytes）、SHA-256、requester、
  tenant、environment、resource_id、target fingerprint、config revision、created_at 与 expires_at；
- `sql_ref` 由 CSPRNG 生成、不可枚举；编辑任何字节都产生新的 `sql_ref`，确认动作不能携带替换 SQL；
- TaskStore、PlanStore、Evidence、RenderPayload、ChannelStore、trace、audit 与日志只保存 `sql_ref`、
  `sql_hash` 或闭集决定，不保存 SQL 原文；
- 草稿创建 24 小时未确认即过期；一次确认只能消费一次，同一确认幂等键只能得到同一 task。

### D2 ArtifactSubmission 与现有 TaskSubmission 共用一个 TaskStore

`TaskSubmission` 升级为严格 discriminated union：`ConversationSubmission` 保持现有
`RequestEnvelope` 行为；`ArtifactSubmission` 只含 `input_kind=sql_artifact`、`sql_ref`、`sql_hash`、
`resource_id`、`result_ref` 与 `confirmation_ref`。旧记录按原 schema 读取，未知版本 fail-closed；
不建第二套任务系统或第二张提交表。精确 schema/digest 迁移策略写入 F1 实施计划，并由迁移测试证明
旧行读取不变。

### D3 ResultArtifact 采用 staging → sealed → available

- `result_ref` 由 CSPRNG 生成、不可枚举，确认时与 task 一起创建；
- `ResultArtifactStore` 是结果元数据、列、chunks 与 ACL 的唯一真源；
- 列按 ordinal 保存为 `ColumnSpec(ordinal, name, type)`，同名列不得合并；行按 ordinal 对齐，使用带类型
  标签的规范编码，Decimal/日期时间/大整数不经 float；
- `storage_state` 闭集为 `staging`、`sealed`、`available`、`failed`、`expired`；`completeness` 独立为
  `complete`、`truncated_rows`、`truncated_bytes`；
- 只有 `available` 可被读取；staging 由 Gateway 管理的 sink 写入，adapter 返回且连接关闭后才可
  seal，step journal 提交后才可幂等 activation；step 未提交的 artifact 永不 available；
- 每次写入携带 task、step、result_ref、task fencing 与 result fencing；abort 后轮换 result fencing，迟到
  chunk、seal 与 activation 一律被存储层拒绝。

### D4 配额与保留

- 预览硬上限 1000 行、20 MiB（按完整规范编码行计算，不存半行）；
- 单 requester 存活 query set ≤ 5，单 target ≤ 50；每组含一份 SQL 与至多一份 result；
- 未确认草稿创建后 24 小时过期；已确认 SQL 与结果在任务终态后 24 小时过期；无终态孤儿 artifact
  创建后最多 24 小时；过期即拒绝读取，retention 删除 bytes、列、chunks、ACL 与活动索引；
- 长期只保留 hash、actor、target、时间、预算、Guard/Policy 决定、query id 与资源指标；
- PostgreSQL DELETE 不证明物理擦除；上线前的备份/VACUUM/数据处置证据属于 F1-H，不由本 ADR 提供。

### D5 结果 ACL 与锁定页

- 新增 `result_access_grants`；F1 确认查询时只写 `requester_owner` grant；
- grant 绑定 result_ref、principal、grant_kind、approval_ref、created_at、expires_at；只有
  `requester_owner` 的 `approval_ref` 可为空；
- F1 不写 `view_approver` 或 `export_approver`；Admin 角色本身不产生 grant；
- F1 `/results/{result_ref}` 锁定页只显示状态、target 展示名、时间、预算、保存行/字节数、截断标记、
  过期时间、“详细结果尚未获 F2 查看审批”提示，以及 requester 自己确认的 SQL；页面和 API 不返回
  列名、单元格、chunks、数据摘要或导出链接；not-found、过期与越权使用同一隐藏式拒绝；
- `export_policy` 在 F1 固定为 `disabled`。

### D6 F2/F3 的消费契约

- F2 只读取 `available` artifact 的已保存 chunks 分页展示，翻页不访问 StarRocks；
- F3 在 TTL 内校验原 SQL hash、target fingerprint、requester 与有效 `sql_ref` 后，在新连接中重新执行
  原 SQL，不导出 F1 预览，也不继承 F1 的 `sql_select_limit`；
- 任何 approver grant、结果行展示或导出，都必须先满足未来 ADR-005 与各自获批的 F2/F3 设计；
  本 ADR 不定义审批语义，也不授权 F2/F3。

## 后果

- SQL 原文与结果行各只有一个受保护存储，渠道、任务与证据层只能持有引用；
- 结果写入需要 fencing、背压与 staging，换来“未提交的查询永不可见”和“迟到写不污染结果”；
- 新表、迁移、retention worker 与配额检查增加实现量；这些在 F1-1 以真实 PostgreSQL 集成测试验收；
- F1 锁定页不展示结果，因此可以先于 ADR-005 交付；展示与导出仍被 ADR-005 和 F2/F3 阻塞。

## 备选方案与否决理由

- **把 SQL 放进 `RequestEnvelope.text` 或 `ToolCall.typed_args`**：前者 8192 字符上限且进入对话链，
  后者只允许 JSON 标量且进入 hash/trace；两者都会复制原文。
- **结果行放进 `AdapterResponse.payload` / Evidence**：突破现有小结果边界，并让结果进入长期事实。
- **按列名保存行 map**：同名列会互相覆盖。
- **查询结束后直接 available**：step 未提交就崩溃时会暴露无任务事实支撑的结果。
- **F1 就写 approver grant**：等于在 ADR-005 前定义审批语义。

## 变更门

以下任一变化必须先修订本 ADR：SQL 或结果的保存位置、TaskSubmission union 形状、storage_state 或
completeness 闭集、配额/保留数值、ACL grant 种类、锁定页可见字段、export_policy，或 F2/F3 消费契约。

## 回滚

F1-1 之前回滚只需撤回本 ADR 与相关文档修订。F1-1 之后关闭 F1 capability 注册与路由即可停止新
artifact；既有 artifact 由 retention 在 24 小时内清理，迁移按 F1 实施计划的 downgrade 策略处理。
