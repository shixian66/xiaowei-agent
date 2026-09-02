# ADR-009：`plan_hash` 规范形状、审批绑定与工具准入

- 状态：Accepted
- 日期：2026-09-02
- 决策人：项目负责人
- 相关：[ARCHITECTURE.md](../../ARCHITECTURE.md) §6/§7/§15、[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[M2 实施计划](../plans/M2-contracts-kernel.md)

## 背景

M2 首次实现 `plan_hash`、`target_fingerprint` 与工具准入形状。`ARCHITECTURE.md` §7.1 给出的规范输入集写于「计划绑定单一 capability」的前提下，而 §6 契约表把两个指纹列为 `ExecutionPlan` 的字段。这两处在实现层面存在四个必须一次定死的问题：

1. 分类信息（`effect_class`）与可选分支条件（`condition`）是否进入 `plan_hash`。若不进入，一份批准时分类为只读的计划，在恢复时分类若发生变化，`plan_hash` 不变，漂移检查静默通过——这正是 [ADR-007 D7](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) 要求 fail-closed 的场景。
2. 步骤是否各自携带 capability 标识。若携带而 `ordered_steps` 的规范输入不含它，同一份计划可以改掉某一步的 capability 而 `plan_hash` 不变。
3. 两个指纹作为字段存储时，计划经 TaskStore 往返后，字段值与实际内容可能不一致；而恢复路径恰恰要靠重算发现不一致，存一份「自称的 hash」只会制造可被篡改的第二真源。
4. 准入凭证若只绑定步骤身份，持有合法凭证替换 `ToolCall` 的参数即可执行另一个调用。

`ARCHITECTURE.md` §15 要求「修改 DTO 的必填字段、hash canonicalization、审批绑定或 ToolGateway 语义，必须写 ADR」。以上四项全部落入该条，故先于任何代码固化。

## 决策

### D1 `plan_hash` 的规范输入集

规范输入集为：`plan_schema_version`、`capability_id`、`capability_version`、`ordered_steps`、`policy_profile`、`policy_revision`、`budget`。

`ordered_steps` 的每个元素为：`step_id`、`operation`、`typed_arguments`、`depends_on`、`side_effect`、`effect_class`、`condition`。

三点说明：

- **`effect_class` 进入**。有人会说它可由 `(capability_id, capability_version, operation)` 推出，而这三项已在输入集内，故属冗余。该推理只在 `CapabilitySpec` 版本不可原地变更时成立；显式写入使 hash 自描述，从而把「某个版本的 spec 被原地改写」这一治理失效也变成可检出的漂移。成本为零。
- **`condition` 进入**。可选只读分支的执行条件是计划的一部分；不进入则同一 `plan_hash` 可对应不同的实际执行路径。
- **`budget` 进入**。否则已批准的计划可被换上更大的 `max_tool_calls` 继续执行，审批所依据的预算约束失效。

`PLAN_SCHEMA_VERSION` 首个取值为 `1`。M2 是首次实现，此前不存在已发出的 hash 或已生效的审批，因此直接以含上述字段的形状定义版本 1，不存在需要迁移的存量。

**覆盖完备性由机制承重，不由人记得**：实现维护「模型字段名 → 指纹键名」的显式映射表，安全测试断言映射表的键集等于对应 DTO 的 `model_fields`。给 DTO 新增字段而未决定它是否进入指纹时，测试立即失败。该要求同样适用于嵌套 DTO（`PlanBudget`、`StepCondition`）。

### D2 `ExecutionPlan` 绑定单一 capability

步骤**不携带** `capability_id` / `capability_version`；二者只出现在计划级。

分类校验因此以整个计划为单位进行：`verify_plan_effects(snapshot, plan)` 用计划级的 capability 标识逐步骤重算并核对，而不是逐步骤各自解析。

将来若确需跨 capability 计划，必须递增 `PLAN_SCHEMA_VERSION` 并另立 ADR，同时给出「一份计划中不同 capability 的 policy profile 如何合并」的规则——本 ADR 不预留该扩展点。

### D3 两个指纹不作为 `ExecutionPlan` 的字段

`plan_hash` 与 `target_fingerprint` 由 `planning` 模块按需计算（`compute_plan_hash` / `compute_target_fingerprint`）。绑定值只存储在 `ApprovalRequest` 上，代表审批**时刻**的快照。

`ApprovalRequest` 因此必须同时携带 `plan_hash`、`target_fingerprint` 和 **`policy_revision`**。恢复时重算前两者并比对，第三项直接比对——`policy_revision` 虽已进入 `plan_hash`，仍显式保留，使「policy 变化不能静默让旧审批继续生效」有一个可独立断言、可独立给出拒绝原因的位置。

本决策修订 `ARCHITECTURE.md` §6 中 `ExecutionPlan` 一行的字段列。

### D4 工具准入形状

`AdmissionCertificate` 由 `StepAdmission` 产出、`ToolGateway` 消费，同时绑定：

- **步骤身份**：`step_id`、`operation`；
- **调用内容**：`tool_call_hash`，覆盖 `ToolCall` 的全部字段（`gateway`、`operation`、`step_id`、`typed_args`、`timeout_seconds`、`idempotency_key`）；
- **判定与绑定**：`effect_class`、`policy_decision`、`approval_ref`、`plan_hash`、`target_fingerprint`。

Gateway 在每次 `invoke` 时重算 `tool_call_hash` 并比对。凭证证明的是「**这一个 `ToolCall`** 已准入」，而不是「这个步骤已准入」。

`ToolGateway` 以 Protocol 形式定义，领域层只依赖该 Protocol。

**E1 硬闸**：`tools/gateway.py` 的 `_E1_EXECUTION_ENABLED` 常量在 M0–M7 恒为 `False`，凡 `effect_class` 非 `READ` 一律拒绝。这把 [ADR-007 D7](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) 的「M0–M7 全程禁止 E1，含非生产环境」从纪律变成代码事实。

## 后果

- `ARCHITECTURE.md` §7.1 的规范输入集示例、§6 的 `ExecutionPlan` 与 `ApprovalRequest` 行、§16 的 ADR 索引需同步修订，属本 ADR 的直接后果。
- M3 的 `PlanCompiler` 只能产生单 capability 计划；`StepAdmission` 必须在每个步骤准入前调用 `verify_plan_effects`，并在产出凭证时计算 `tool_call_hash`。
- M4 的 PostgreSQL `TaskStore` 持久化 `ExecutionPlan` 时不存储指纹；恢复后由调用方重算比对。
- M8 开放受控写时，解除 E1 硬闸只需改一个常量，但必须经独立评审并完成 ADR-007 的例外记录或修订。

## 备选方案与否决理由

- **`effect_class` 不进 hash，靠 `capability_version` 间接覆盖**：依赖「spec 版本不可原地变更」这一未被任何机制保证的前提；一旦该前提被破坏，漂移不可检出。
- **步骤携带 capability 标识以支持跨 capability 计划**：在没有任何调用者要求跨 capability 的阶段引入该能力，代价是 `plan_hash` 覆盖面立即出现缺口，且 policy profile 合并规则无处定义。
- **两个指纹作为字段存储并在校验时比对自身**：存储值与重算值不一致时以谁为准无法定义；且它给了篡改者一个可写入的目标。
- **准入凭证只绑定步骤身份**：无法区分「同一步骤的合法调用」与「同一步骤被替换了参数的调用」，审批与实际执行之间存在缺口。

## 回滚

| 决策 | 回滚方式 |
| --- | --- |
| D1 | 修改「字段 → 指纹键」映射表并递增 `PLAN_SCHEMA_VERSION`，同步更新固定向量测试与 `ARCHITECTURE.md` §7.1；已发出的审批因 `plan_hash` 变化而全部失效，属预期行为。 |
| D2 | 跨 capability 计划须递增 `PLAN_SCHEMA_VERSION` 并另立 ADR，同时定义 policy profile 合并规则。 |
| D3 | 恢复为字段存储，必须同时给出「存储值与重算值不一致时以谁为准」的规则，并说明该规则为何不构成第二真源。 |
| D4 | 解除 E1 硬闸只需把 `_E1_EXECUTION_ENABLED` 改为 `True`，但须经独立评审、ADR-007 完成例外记录或修订，并同步更新对应安全测试。 |
