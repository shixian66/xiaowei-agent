# ADR-011：M6a capability binding 与 PromQL 模板准入

- 状态：Accepted
- 日期：2026-09-05
- 决策人：项目负责人
- 相关：[ARCHITECTURE.md](../../ARCHITECTURE.md) §4/§5/§8/§9、[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-009](ADR-009-plan-hash-approval-binding-and-tool-admission.md)、[M6a 实施计划](../plans/M6a-prometheus-asset-fake.md)

## 背景

M3/M5 已证明一个 StarRocks fake capability 可以通过同一 Runtime、Runner、
StepAdmission 与 ToolGateway 运行，但 Runtime、Runner、准入和本地装配仍直接持有
StarRocks 领域对象。M6a 要加入 Prometheus 告警证据与资产精确查询；继续增加
`if capability_id == ...` 会形成第二套路由真源，并让每个新能力都修改公共执行核心。

Prometheus 能力还需要在同一个计划中先读取 Alertmanager、再按固定模板读取
Prometheus。小维不接受用户或模型提供的任意 PromQL，因此必须先冻结 operation 的
gateway 归属、能力运行 binding 和非 SQL 查询准入语义。

## 决策

### D1 Resolver 仍是唯一候选真源

`CapabilityResolver` 继续独占 `CandidateSet` 生成。application 层的
`CapabilityBindingRegistry` 只对 Resolver 已给出的精确
`(capability_id, capability_version)` 选择 binding；不读用户原文、不计算分数、不按
gateway 或 adapter 可用性回退，也不提供关键词、任意 executor 或动态插件发现。

binding 只组合该能力的确定性 planner、execution metadata、Evidence builder、
Answerability 与 renderer。Runner 只依赖窄的 `ExecutionBindingProvider`，不依赖
application registry 的候选选择职责。

### D2 gateway 是版本化 operation metadata

`OperationSpec.gateway` 为必填非空字段，是 `ToolCall.gateway` 的唯一声明来源。Runner
按计划的 capability key 与 step operation 从当前 snapshot 重新派生 gateway；
`IntentDraft`、step arguments、adapter 与单独 mapping 均不能设置或覆盖它。
StepAdmission 在签发凭证前再次从同一 snapshot 派生声明并逐字比对
`ToolCall.gateway`；Runner 派生正确不替代准入边界的独立 fail-closed 校验。

给 `starrocks.slow_query.diagnose@1.0.0` 的两个既有 operation 补
`gateway="starrocks"` 是本 ADR 唯一的一次性迁移例外：它只把 Runner 已使用的同一
常量移入声明，不改变 capability ID/version、PlanStep、SQL、typed arguments、
`plan_hash`、ToolCall 字节或 `tool_call_hash`，因此保持版本 `1.0.0`。

本 ADR 生效后例外关闭。既有 operation 的 gateway 变化必须递增 capability version
与 Registry snapshot ID；同版本声明由受审 declaration golden 固定。

### D3 不升级 Plan schema

`ExecutionPlan` 已包含 capability ID/version 和每个 step 的 operation，因此 gateway
可从版本化 snapshot 唯一派生。M6a 不向 `PlanStep` 增加 gateway 或 capability 字段，
不改变 `PLAN_SCHEMA_VERSION = 1`。准入证书继续通过 `tool_call_hash` 绑定最终完整
`ToolCall`。

### D4 查询信封按实际类型准入

StepAdmission 在 ToolPolicy 之后检查步骤实际携带的查询信封：

- `sql + sql_template_id`：必须有 `SqlSurface`，先执行既有 SQLGuard 规则，再重编译比对；
- `promql + promql_template_id`：必须有 `PromqlSurface`，从 typed arguments 重建
  参数并用注册模板逐字节重编译比对；
- 半个信封、同时出现两类信封、或 binding 缺少对应 surface：拒绝；
- 无查询信封的普通只读步骤：继续执行其余准入，不伪装成拥有 SQL/PromQL guard。

PromQL 只允许代码内注册的完整模板、闭集参数、统一 escape、窗口/series/point 预算。
M6a 不引入 PromQL parser，也不开放任意 expression、matcher、aggregation、range
selector、function 或 label map。

### D5 无查询信封不等于无保护

Alertmanager 与资产 fake 步骤依赖四层确定性约束：能力 params/compiler 产生不可变
计划；StepAdmission 重派生 effect/gateway 并执行 ToolPolicy；恢复前重编译并比较
`plan_hash`/`target_fingerprint`；fake adapter 只接受精确 operation 与 recording key
闭集且无 scope fallback。

这些约束只证明 fake 闭环。真实 Alertmanager/资产 adapter 入场前必须按其 API 另行
决定权威参数校验、分页、限流和 scope 绑定，不能把本决策外推成真实系统兼容证据。

### D6 capability snapshot 与声明集合共同版本化

PR 1 使用 `snapshot.m6a.starrocks-prometheus.v1`；PR 2 使用
`snapshot.m6a.starrocks-prometheus-asset.v1`。测试用字面量同时固定 snapshot ID 与
有序 `(capability_id, version)` 集合，能力地图从对应 snapshot 生成。

### D7 policy revision 与允许 profile 集合共同版本化

PR 1 把生产允许面从仅有 `readonly.starrocks.slow_query.v1` 扩展为同时包含
`readonly.prometheus.alert.evidence.v1`，因此生产 `POLICY_REVISION` 从
`policy-2026-09-01` 递增为 `policy-2026-09-05`。测试用字面量同时固定 revision 与
有序 profile ID 集合。PR 2 再加入 `readonly.asset.inventory.lookup.v1`，因此 revision
再次递增为 `policy-2026-09-05.2`；PR 1 的 revision 不得在扩大的允许面下继续生效。
新增、删除或重排生产 profile 时必须显式评审并更新 revision，不能让旧任务、旧审批
或审计记录静默指向新的允许面。测试 fake 的独立 revision 不代表生产快照，不随本次
变更机械迁移。

### D8 M6a 不实现通用 DSL

PR 1 建立最小显式 binding seam 并交付 Prometheus 能力；PR 2 只有在 PR 1 验收合入
后才交付资产能力。扩展数据把执行核心改动与 intent/Registry/profile/default
bindings/local stack 等注册装配改动分开计量。M6a 只形成 ADR-004 是否应立项的数据
结论，不实现 DSL、动态 discovery 或新框架。

### D9 Evidence builder 必须接收已准入目标

`StepEvidenceBuilder` 的必填输入包含 `ResolvedTarget`。Runner 在首次执行时传入本步骤
刚通过 `StepAdmission` 的同一个目标；恢复时先完成 `target_fingerprint` 漂移复核，再
把复核后的目标传入 builder。adapter payload、`ToolResult` 与外部文本都不能提供或
覆盖该参数。

ToolPolicy 必须分别拒绝 target/context 的租户不一致与环境不一致，再判断环境
allowlist。否则一个错误环境目标可能通过准入，并被下游误当成可信目标。Evidence
builder 不接收完整 `RequestContext`：`ResolvedTarget` 已包含证据一致性校验所需的租户、
环境、provider、资源类型、资源 ID 和 selector version，继续扩大输入只会增加耦合。

该变更不修改 `ExecutionPlan`、`PLAN_SCHEMA_VERSION`、`plan_hash`、
`target_fingerprint` 或 ToolCall 字节。StarRocks 与 Prometheus builder 先保持行为不变；
资产 builder 将消费该目标校验 environment 与精确资产身份。

## 后果

- 新能力仍需显式注册 planner、policy、adapter、Evidence、renderer 和 eval；这是可审
  查成本，不被隐藏成自动发现。
- Runner 可按 operation 派生多个 gateway，但仍只有一条 ToolGateway 执行入口。
- SQL 现有规则与 hash 向量保持不变；PromQL 的安全上限是固定模板，不是任意 PromQL。
- 新增生产 policy profile 会使旧 revision 在当前准入边界失效，审计可区分 M5 与
  M6a 的允许面。
- binding 错配、缺失或 evidence/plan capability key 冲突均 fail-closed，不回退到
  StarRocks renderer。
- Python Protocol 和 frozen dataclass 不是语言级不可绕过边界，仍需静态检查、契约
  测试和精确 composition root 审查。
- Prometheus 本地 recording 仍是装配时刻附近的有限闭集：只含默认 30 分钟窗口，
  非默认窗口或超出中心时刻前后 120 分钟的查询会 fail-closed，不提供 fallback。
- capability Evidence builder 可以依赖 Runner 已准入或恢复期已复核的 `ResolvedTarget`，
  但不能从 adapter 返回值反推执行目标。
- Reflection/renderer 通过 Evidence ID 的 `:s1`/`:s2` 后缀识别两个固定步骤；步骤名
  是本能力版本的隐式契约，改名必须同步更新 planner、可答性、渲染和回归测试。

## 备选方案与否决理由

- **在 Runtime/Runner 继续加 capability 分支**：持续修改公共核心并形成并列路由真源。
- **把 gateway 放进 step arguments**：让用户/模型可污染执行路由，且 gateway 与
  版本化声明脱钩。
- **给 PlanStep 增加 gateway**：同一事实出现两份，还会无必要升级 plan schema。
- **允许任意 PromQL 后做字符串过滤**：无法可靠约束语法形状、标签范围和预算。
- **直接引入通用 DSL/插件系统**：只有三个异质能力，没有重复数据证明收益。
- **把 Alertmanager 与 Prometheus 合成一个 adapter**：隐藏真实故障归因和两个独立
  工具边界，且不利于逐步准入与调用计数。

## 回滚与变更门

- Prometheus capability 可从 Registry、bindings、profiles 与 local stack 移除并重新
  生成能力地图；StarRocks 继续消费同一 binding seam。
- 共享 seam 只有在 StarRocks 全部 contract/security/eval 与 hash vector 仍绿时才能
  独立回滚。
- 放宽 PromQL 形状、改变 gateway/version 规则、跨 capability 计划或动态 binding
  discovery 均需新的 ADR，不得以 adapter 兼容分支绕过。
- 本 ADR 不授予真实 Prometheus、Alertmanager、资产系统、模型或任何 E1 调用权限。
