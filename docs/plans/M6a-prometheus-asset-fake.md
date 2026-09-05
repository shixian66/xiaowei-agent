# M6a Prometheus 告警证据与资产查询 fake 能力实施计划（V1.1）

> 状态：Approved V1.1；2026-09-05 经独立 Claude 复审通过，项目负责人已明确批准开始 M6a。
>
> 计划基线：小维 `main` / `origin/main` = `a7dba18315b4213c8a61cdf492aca0cc951328bf`。
>
> 外部调研基线：OpenSRE `main` = `b343b6e03c3aac1351afd661b142035b568e8570`
>（2026-09-05，tag `v0.1.2026.9.5`）。
>
> 本计划只覆盖 M6a。2026-09-05 已获明确“开始 M6a”授权；本轮仍不调用 Prometheus、
> Alertmanager、资产系统或任何真实模型 API。

## 0. 一句话结论

M6a 用两个顺序独立、可以分别拒绝的垂直 PR，新增：

1. `prometheus.alert.evidence@1.0.0`：先查 fake Alertmanager 的当前告警事实，再用能力内注册的固定 PromQL 模板查询 fake Prometheus 指标，最后只做证据合并，不做自动根因判断。
2. `asset.inventory.lookup@1.0.0`：按一个精确 `asset_id`、`hostname` 或 `ip` 查询 fake 资产目录，严格绑定 tenant/environment，只输出字段白名单。

第一个 PR 同时把当前 StarRocks 单能力硬编码收敛为一个窄的显式 capability binding seam；第二个 PR 必须复用这条 seam。M6a 不实现通用 DSL，不连接真实系统，不做 Grafana、告警规则管理、巡检、StarRocks 慢查询增强或任何写操作。

## 1. 目标、交付物与证据上限

### 1.1 目标

- 证明第三个领域能力可以继续消费同一条确定性执行链，而不是继续扩张 `XiaoweiRuntime`、`DeterministicStepRunner` 或入口 handler 的领域分支。
- 证明一个 capability 可以在同一计划内按已声明 operation 分别路由到两个 adapter，同时仍逐步经过 `ToolPolicy → 查询守卫 → ApprovalGate* → ToolGateway`。
- 证明非 SQL 能力也能形成稳定 `plan_hash`、`tool_call_hash`、canonical target、Evidence、Answerability、Render 和 L0-L2 eval。
- 采集三个能力的真实扩展数据，形成 DSL “继续延期 / 建议另立 ADR-004”结论；本里程碑不实现 DSL。

### 1.2 交付物

- PR 1：共享 binding seam + `prometheus.alert.evidence` 完整 fake 闭环。
- PR 2：`asset.inventory.lookup` 完整 fake 闭环 + M6a 扩展性数据结论。
- 两个能力各自的 unit、contract、security、integration、L0、L1、L2 证据。
- 更新后由 Registry 生成的 `docs/CAPABILITIES.md`，状态最强只写 `tests`。
- `docs/handoff/M6a-capability-extension-data.md`：扩展成本与重复形状的事实表。
- `docs/handoff/M6a-acceptance-report.md`：只在完成实现和验证后填写真实 SHA、命令与结果。
- `AGENT_HANDOFF.md`：只在行为、证据或当前阶段真正变化时更新。

### 1.3 本轮能证明和不能证明什么

本轮最多证明：源码声明、离线 fake/recording 测试、本地 PostgreSQL/Compose 集成路径。它不能证明真实 Alertmanager/Prometheus/资产系统兼容性、真实权限、真实数据质量、部署、canary 或用户验收。

## 2. 入口门与开工前复核

### 2.1 本计划编写时已核对的事实

- M0-M5 已验收并合入 `main`；M5 验收 SHA 为 `372c381f44ecfa1fa53961f137d0058033cbd805`，归档后当前 `main` 为 `a7dba18315b4213c8a61cdf492aca0cc951328bf`。
- 当前 Runtime、Runner、StepAdmission、LocalStack、Evidence/Reflection/Rendering 都含 StarRocks 单能力硬编码。
- 当前工作树存在两份用户的未跟踪文件，M6a 不编辑、不暂存：
  - `docs/plans/development-route-v3-proposal.md`
  - `docs/plans/legacy-capability-migration-matrix.md`
- M6a 的真实运维目标调用和真实模型调用均未授权；只能使用 fake/recording。

### 2.2 真正开工时必须再次执行

顺序不可变：

```bash
sed -n '1,9999p' AGENTS.md
sed -n '1,9999p' ARCHITECTURE.md
sed -n '1,9999p' AGENT_HANDOFF.md
sed -n '1,9999p' README.md
sed -n '1,9999p' DEVELOPMENT_PLAN.md
git status --short --branch
git rev-parse HEAD
git rev-parse main
git rev-parse origin/main
git log -1 --format='%H%n%P%n%ad%n%s' --date=iso-strict
git merge-base --is-ancestor 372c381f44ecfa1fa53961f137d0058033cbd805 HEAD
```

随后核对 M5 归档证据、CI 结论、未提交文件和本计划是否被审核修订。只有以下条件同时成立才创建 `claude/m6a-prometheus-alert-evidence`：

1. M5 祖先关系仍成立；
2. 工作树中的用户文件已识别且不会被本轮覆盖；
3. 本计划已由项目负责人明确批准；
4. 项目负责人明确说“开始 M6a”；
5. 无任何真实运维目标或真实模型网络调用被加入范围。

PR 2 必须等 PR 1 被用户验收并合入最新 `main`，再从最新 `main` 创建 `claude/m6a-asset-inventory-lookup`，不得在未合并的 PR 1 上继续堆叠第二个能力。

## 3. OpenSRE 精确 SHA 调研结论

### 3.1 调研边界

调研对象固定为 [OpenSRE `b343b6e0`](https://github.com/Tracer-Cloud/opensre/tree/b343b6e03c3aac1351afd661b142035b568e8570)，不以 GitHub 搜索缓存、旧摘要或 README 宣传语替代当前源码。

当前 SHA 中不存在此前搜索结果提到的 `docs/investigation-pipeline-architecture.md`、`docs/investigation-tool-calling.md` 和 `tools/investigation/`。这些旧路径不作为 M6a 当前事实或设计依据。

OpenSRE 使用 Apache-2.0。M6a 只借鉴设计模式和测试清单，不复制源码；将来若复制任何实现，必须单独做 license/NOTICE 和出处审查。

### 3.2 借鉴清单

| 当前源码 | 可借鉴内容 | 小维落法 |
| --- | --- | --- |
| [`docs/ARCHITECTURE.md`](https://github.com/Tracer-Cloud/opensre/blob/b343b6e03c3aac1351afd661b142035b568e8570/docs/ARCHITECTURE.md) | integration/config/client/tool 与 composition root 分离 | adapter 仍只在 `interfaces/local_stack.py` 装配；领域层不持有客户端 |
| [`docs/adding-tools-and-integrations.md`](https://github.com/Tracer-Cloud/opensre/blob/b343b6e03c3aac1351afd661b142035b568e8570/docs/adding-tools-and-integrations.md) | 配置归一、verifier、结构化错误、分页/截断、真实 fixture、registry/discovery 测试 | 变成 M6a adapter 与 capability 的 Definition of Done；不照搬其 agent tool loop |
| [`integrations/alertmanager/client.py`](https://github.com/Tracer-Cloud/opensre/blob/b343b6e03c3aac1351afd661b142035b568e8570/integrations/alertmanager/client.py) | Alertmanager v2 alerts 的有界读取、状态和标签归一 | fake 响应只保留当前告警事实白名单；错误原文仍按 `ExternalContent` 处理 |
| [`integrations/alertmanager/incident_anchor.py`](https://github.com/Tracer-Cloud/opensre/blob/b343b6e03c3aac1351afd661b142035b568e8570/integrations/alertmanager/incident_anchor.py) | 告警开始时间可作为事故时间线锚点 | M6a 只展示 `starts_at`；不让第一步输出动态改写第二步查询窗口 |
| [`integrations/alertmanager/tools/__init__.py`](https://github.com/Tracer-Cloud/opensre/blob/b343b6e03c3aac1351afd661b142035b568e8570/integrations/alertmanager/tools/__init__.py) | 明确区分“找到 N 条但 0 firing”与“没有计数” | 小维 Answerability 使用完整真值表，不把空证据静默省略 |
| [`infrastructure/evidence/metric_summary.py`](https://github.com/Tracer-Cloud/opensre/blob/b343b6e03c3aac1351afd661b142035b568e8570/infrastructure/evidence/metric_summary.py) | 对 Prometheus matrix 做确定性首值/末值/min/max/delta/trend 摘要 | 小维只实现本轮需要的有限摘要，不引入通用 metric 分析框架 |
| [`integrations/ec2/tools/ec2_instances_by_tag_tool/__init__.py`](https://github.com/Tracer-Cloud/opensre/blob/b343b6e03c3aac1351afd661b142035b568e8570/integrations/ec2/tools/ec2_instances_by_tag_tool/__init__.py) | 无过滤条件不调用、结果有界、字段归一、`truncated` 显式 | 资产查询必须有且只有一个精确 selector；输出字段白名单和限制 |
| [`core/tool/contracts.py`](https://github.com/Tracer-Cloud/opensre/blob/b343b6e03c3aac1351afd661b142035b568e8570/core/tool/contracts.py) | tool 元数据、use/anti-use case 和 discovery 完整性 | 小维把 gateway 归属纳入版本化 `OperationSpec`，并验证 Registry/binding 完整性 |

### 3.3 明确不移植的内容

| OpenSRE 形状 | 不移植原因 |
| --- | --- |
| LLM/agent 自主选择工具和组织调查步骤 | 小维的模型没有 capability、目标、查询或执行顺序决定权 |
| Grafana metrics tool 接收任意 metric query / PromQL 表达式 | 违反 ADR-007“只允许 capability 注册模板”的硬边界 |
| `integrations/grafana/mimir.py` 直接拼接 metric/service 字符串 | 没有小维所需的模板闭集、参数 schema 和准入期重编译比对 |
| Grafana alert-rule 查询 | 用户已确认告警事实源是 Prometheus + Alertmanager；M6a 不引入 Grafana |
| silence 创建、remediation 或其他写工具 | M0-M7 的 E1 调用次数必须为 0 |
| EC2 按 tag/VPC/tier 广泛列举资源 | M6a 资产能力只接受一个精确 ID/hostname/IP，不做全局浏览或模糊搜索 |
| OpenSRE 工具错误直接携带上游可读文本 | 小维外部文本不能进入可信错误、计划、证据判断或控制流 |
| MySQL/PostgreSQL/ClickHouse 诊断工具 | 可作为后续数据库能力样本，但不属于 M6a |

## 4. 当前架构缺口与 M6a 的最小修正

### 4.1 当前硬编码事实

- `application/runtime.py` 直接 import StarRocks 的 intent、target、params、compiler、answerability 和 renderer，并固定选择 `OP_LIST`。
- `runners/deterministic.py` 固定 `GATEWAY_NAME="starrocks"`，构造证据时固定 `SlowQueryParams` 和 StarRocks builder。
- `governance/step_admission.py` 只接受一个 `SqlSurface`，直接按 StarRocks 参数重建 SQL。
- `interfaces/local_stack.py` 只装配一个 StarRocks adapter/profile/surface。
- `query_task()` 在终态投影时默认使用慢查询 renderer；多 capability 后会发生错误投影。

直接给这些文件继续加 `if capability_id == ...` 会形成第二套路由真源，也会让第二个能力持续修改公共核心。因此 PR 1 先做一次窄的结构修正。

### 4.2 修正后的链路

```text
RequestEnvelope
  → IntentInterpreter（只产出闭集 IntentDraft）
  → CapabilityResolver（唯一 CandidateSet 真源）
  → CapabilityBindingRegistry.select_entry(CandidateSet)
       仅按精确 (capability_id, version) 找 binding，并选择其声明的 entry_operation
       不读用户原文、不建候选、不模糊匹配、不回退
  → binding.prepare(...) → ResolvedTarget + ExecutionPlan
  → WorkflowRunner
       从同一个 binding registry 精确取 ExecutionBinding
       OperationSpec.gateway → ToolCall.gateway
       ToolPolicy → SQL/PromQL 查询信封校验 → ApprovalGate*
       → ToolGateway → adapter
       → binding.build_evidence(...)
  → EvidenceLedger
  → binding.assess(...)
  → Runtime 确定性终态映射
  → binding.render(...)
```

`CapabilityResolver` 仍是唯一候选生成真源。Binding registry 只消费 Resolver 已生成的候选并做精确绑定；禁止按关键词、分数、gateway 或 adapter 可用性重新选择 capability。

### 4.3 显式 binding，不做 DSL

新增 `src/xiaowei_agent/application/capability_runtime.py`，定义：

```python
@dataclass(frozen=True)
class PreparedCapability:
    target: ResolvedTarget
    plan: ExecutionPlan

class CapabilityPlanner(Protocol):
    def __call__(
        self,
        *,
        candidate: Candidate,
        draft: IntentDraft,
        context: RequestContext,
        as_of: datetime,
        snapshot: CapabilitySnapshot,
    ) -> PreparedCapability: ...

class CapabilityAssessor(Protocol):
    def __call__(
        self, *, evidences: tuple[EvidenceEnvelope, ...]
    ) -> AnswerabilityVerdict: ...

class CapabilityRenderer(Protocol):
    def __call__(
        self,
        *,
        evidences: tuple[EvidenceEnvelope, ...],
        verdict: AnswerabilityVerdict,
        status: TaskStatus,
    ) -> RenderPayload: ...

@dataclass(frozen=True)
class CapabilityRuntimeBinding:
    capability_id: str
    capability_version: str
    entry_operation: str
    planner: CapabilityPlanner
    assessor: CapabilityAssessor
    renderer: CapabilityRenderer
    execution: CapabilityExecutionBinding
```

`CapabilityBindingRegistry` 构造时必须 fail-closed 验证：

- binding key 无重复；
- binding key 集合与传入 `CapabilitySnapshot` 的 key 集合完全相等；
- `entry_operation` 恰为该版本已声明 operation；
- `execution.policy_profile.profile_id == CapabilitySpec.policy_profile`；
- policy profile 出现在当前 `PolicySnapshot`；
- binding 不提供 candidate builder、关键词、任意 executor、任意 SQL/PromQL 或 adapter 对象。

新增 `src/xiaowei_agent/runners/binding.py`，只暴露 Runner 所需窄协议：

```python
class StepEvidenceBuilder(Protocol):
    def __call__(
        self,
        *,
        task_id: str,
        step: PlanStep,
        plan: ExecutionPlan,
        result: ToolResult,
        captured_at: datetime,
    ) -> EvidenceEnvelope: ...

@dataclass(frozen=True)
class CapabilityExecutionBinding:
    capability_id: str
    capability_version: str
    policy_profile: PolicyProfile
    sql_surface: SqlSurface | None
    promql_surface: PromqlSurface | None
    evidence_builder: StepEvidenceBuilder

class ExecutionBindingProvider(Protocol):
    def execution_for(self, *, plan: ExecutionPlan) -> CapabilityExecutionBinding: ...
```

具体 registry 由 application 提供，但 Runner 只依赖 `ExecutionBindingProvider` Protocol。`interfaces/local_stack.py` 是 fake adapter 唯一运行时装配点。

### 4.4 operation 到 gateway 的唯一真源

`OperationSpec` 新增必填 `gateway`。三个 capability 的 gateway 声明为：

| capability | operation | gateway |
| --- | --- | --- |
| `starrocks.slow_query.diagnose` | `list_slow_queries` | `starrocks` |
| `starrocks.slow_query.diagnose` | `count_queries_in_window` | `starrocks` |
| `prometheus.alert.evidence` | `get_active_alerts` | `alertmanager` |
| `prometheus.alert.evidence` | `query_metric_range` | `prometheus` |
| `asset.inventory.lookup` | `lookup_asset` | `asset_inventory` |

Runner 用 `derive_effect(...)` 返回的版本化 `OperationSpec.gateway` 构造 `ToolCall`，不从 `IntentDraft`、step arguments、adapter 或单独 mapping 读取。

本次给 `starrocks.slow_query.diagnose@1.0.0` 补 `gateway="starrocks"` 是 ADR-011 中唯一的一次性迁移例外：它只是把当前 `capabilities/specs.py::GATEWAY_NAME` 和 Runner 已实际使用的同一取值移入 `OperationSpec`，不改变 capability ID/version、`PlanStep`、SQL、typed arguments、`plan_hash`、`ToolCall` 字节或 `tool_call_hash`。因此 StarRocks 保持 `1.0.0`，不做无行为变化的版本 bump，也不迁移已存储计划。

例外从 ADR-011 生效后关闭：以后新增或修改任何已存在 operation 的 gateway，都必须递增 capability version 和 snapshot ID；同版本静默改 gateway 由 declaration golden/registry 测试拒绝。PR 1 必须用现有 StarRocks hash vector 和 L2 重复运行用例证明本次显式化没有改变计划/调用字节，而不能只凭代码阅读声称兼容。

这里不修改 `PlanStep` 和 `PLAN_SCHEMA_VERSION`：gateway 可由计划已有的 `(capability_id, capability_version, operation)` 唯一派生，避免为未部署的本地计划引入无必要的数据迁移。`ToolCall.gateway` 已进入 `tool_call_hash`，准入凭证仍与实际 gateway 绑定。

### 4.5 Capability snapshot ID 与 specs 集合绑定

`capabilities/registry.py::SNAPSHOT_ID` 是候选和能力地图可见事实，不能继续保留带 `m3` 的单能力旧名称。两个 PR 分别写死：

| 阶段 | `SNAPSHOT_ID` | 有序 specs key |
| --- | --- | --- |
| PR 1 | `snapshot.m6a.starrocks-prometheus.v1` | `starrocks.slow_query.diagnose@1.0.0`、`prometheus.alert.evidence@1.0.0` |
| PR 2 | `snapshot.m6a.starrocks-prometheus-asset.v1` | 上述两项、`asset.inventory.lookup@1.0.0` |

`tests/unit/test_capability_registry.py` 为每个 PR 固定 `(SNAPSHOT_ID, ordered (capability_id, version) keys)` 的成对 golden，不只分别断言“ID 稳定”和“集合正确”。只改 specs 元组而未同步改 snapshot ID 时，该成对 golden 必须变红；更新 golden 本身属于受审变更，不能由实现自动计算或按时钟生成 ID。

`docs/CAPABILITIES.md` 随每个 PR 从对应 snapshot 重新生成。PR 1 和 PR 2 的扩展数据都记录 old/new snapshot ID，避免能力集合变化只体现在文档行数里。

### 4.6 Policy revision 与 profiles 集合绑定

`governance/profiles.py::POLICY_REVISION` 是审批绑定和审计可见事实。PR 1 把生产
profiles 从仅有 `readonly.starrocks.slow_query.v1` 扩为同时包含
`readonly.prometheus.alert.evidence.v1`，必须把 revision 从 `policy-2026-09-01`
递增为 `policy-2026-09-05`。`tests/unit/test_governance_profiles.py` 用
`(POLICY_REVISION, ordered profile IDs)` 成对 golden 承重；测试 fake 自有 revision，
不与生产 revision 机械耦合。PR 2 若再增加资产生产 profile，必须再次显式递增并评审。

### 4.7 终态投影的 capability 选择

`XiaoweiRuntime` 新增 `PlanStore` 和 binding registry 依赖：

1. 同步 `handle()` 已有当前 binding，直接用它 assess/render。
2. Worker 完成后，`execute_task()` 用当前 binding finalize；返回仍只有 `TaskOutcome`。
3. `query_task()` 先从 PlanStore 读取计划并按精确 key 选择 binding；若已有 Evidence，则全部 Evidence 的 capability key 必须与计划一致。
4. 计划尚未生成就被拒绝的任务使用一个通用、无领域断言的 rejected/indeterminate 投影，不猜 capability。
5. 计划缺失、Evidence key 混杂或 binding 缺失一律 fail-closed，不回退到 StarRocks renderer。

## 5. 查询准入：SQL 保持不变，PromQL 只允许注册模板

### 5.1 查询信封规则

StepAdmission 的第 4 段从“只检查 SQL”收敛为“检查已出现的查询信封”：

```text
sql + sql_template_id         → 必须有 SqlSurface，重建参数并经现有 SQLGuard
promql + promql_template_id   → 必须有 PromqlSurface，重建参数并逐字节重编译比对
没有查询信封                 → 资产/Alertmanager 等普通只读步骤可继续
半个信封                     → 拒绝
SQL 与 PromQL 同时出现        → 拒绝
有查询信封但 binding 无对应 surface → 拒绝
```

执行顺序保持：`verify policy revision → verify plan effects → ToolPolicy → query envelope guard → ApprovalGate → issue certificate`。不允许 adapter 内部补做一套“安全检查”来替代准入。

### 5.2 无查询信封步骤的安全边界

`lookup_asset` 和 `get_active_alerts` 没有查询信封，因此 M6a 不声称它们拥有与 SQLGuard 或 PromQL 重编译等价的语义闸门。fake 路径的安全性来自四层互相独立的确定性约束：

1. 首次执行的 step arguments 只能由能力自己的 params schema、canonicalization 和 compiler 生成，模型及入口不能直接构造可执行参数；编译后的 `Plan` 不在执行期原地修改。
2. StepAdmission 从当前 plan key 和 operation 重新派生 effect/gateway metadata，再执行 `verify_plan_effects` 与 ToolPolicy；准入证书通过 `tool_call_hash` 绑定最终精确 `ToolCall`，adapter 不能替换参数。
3. 恢复执行前 Runtime 以当前身份、target、policy 和 capability binding 重新编译计划，Runner 在任何 adapter 调用前比较当前/已存储的 `plan_hash` 与 `target_fingerprint`；不一致抛出 `DriftError`。
4. fake adapter 只接受显式 operation 和精确 recording key 闭集，没有模糊匹配、scope fallback 或默认结果；未命中返回结构化无事实/错误，而不是扩大查询。

这四层只证明 M6a 的 fake 闭环，不自动证明未来真实 Alertmanager 或资产 adapter 的参数安全。真实 adapter 入场前必须基于其 API 形状另行决定并验收权威的非查询参数校验器、分页/限流和 scope 绑定；不得把“没有查询信封可继续”解释为可以跳过 planner、ToolPolicy、恢复期漂移校验或 recording 闭集。

安全测试必须同时包含正反例：参数化篡改已存储资产计划的 `selector_type`/`selector_value` 后恢复，应在 adapter 前得到 `DriftError`，Gateway/asset adapter 调用均为 0；参数化篡改已存储告警第一步的 `alert_name`/`instance`/`fingerprint`/固定 `limit` 后恢复，应得到相同拒绝且 Alertmanager/Prometheus adapter 调用均为 0；未篡改的等价恢复必须成功，防止测试退化成无条件拒绝。

### 5.3 `PromqlSurface`

新增 `src/xiaowei_agent/contracts/promql_surface.py`：

```python
class PromqlSurface(Contract):
    surface_id: StrictStr
    allowed_template_ids: tuple[StrictStr, ...]
    max_window_minutes: StrictInt
    max_series: StrictInt
    max_points_per_series: StrictInt
```

字段均为正数、模板 ID 非空且唯一。M6a 固定：

- `surface_id = "promql.prometheus.alert.evidence.v1"`
- `max_window_minutes = 360`
- `max_series = 5`
- `max_points_per_series = 361`

### 5.4 模板与重编译

`planning/prometheus/templates.py` 只注册两条 synthetic 模板：

| 告警名 | template id | 指标意义 |
| --- | --- | --- |
| `HostHighCpu` | `prometheus.alert.host_cpu_percent.v1` | instance 的 CPU 使用率时间序列 |
| `InstanceDown` | `prometheus.alert.instance_up.v1` | instance 的 `up` 时间序列 |

这些名称是 fake catalog，不声称等于用户真实 Prometheus rules。

`compile_promql(template_id, params, surface)` 只接受经过 schema 校验的具名参数；不接受 PromQL 片段、matcher、aggregation、range selector、function 名或任意 label map。实例标识只允许规范化后的 hostname/IPv4/`[IPv6]` 加可选端口，端口范围 `1..65535`。字符串字面量使用单一 escape 函数，测试覆盖引号、反斜杠、换行、花括号和 selector 注入。

`governance/promqlguard.py::verify_promql(...)` 必须：

1. 确认 template ID 在 surface 闭集；
2. 从 `typed_arguments` 重新构造 `PrometheusAlertParams`；
3. 重新调用同一 compiler；
4. 与计划中的 `promql` 做逐字节比较；
5. 任一不符在 Gateway 前拒绝，adapter 调用次数为 0。

M6a 不新增 PromQL parser 依赖，也不声称支持任意 PromQL AST 安全分析。安全性来自“模板全文在代码中固定 + 参数 schema 闭集 + 统一 escape + 准入期重编译比对”。如果 M6b 之后要接受更开放的 PromQL 形状，必须另立决策，不可放宽本实现。

## 6. `prometheus.alert.evidence` 产品与技术设计

### 6.1 用户能力表现

用户给出明确告警名和 instance 后，小维回答：

- Alertmanager 当前是否返回精确匹配的活动告警；
- 告警 fingerprint、状态、severity、开始/结束时间、是否 silenced/inhibited；
- 对应注册指标模板在请求窗口内的 first/latest/min/max/delta/trend/point_count；
- 数据来源、采样时间、查询窗口、模板 ID、系列和点数上限；
- 明确说明“这是告警与指标证据，不是自动根因结论”。

可选 `fingerprint` 用于同一 `alert_name + instance` 返回多条时精确消歧。没有 `fingerprint` 且返回多条时不得自动挑一条。

### 6.2 输入闭集

`planning/prometheus/params.py::PrometheusAlertParams`：

- 必填：`alert_name`、`instance`、`window_start`、`window_end`；
- 可选：`fingerprint`；
- 固定：`step_seconds=60`；
- 窗口：默认 30 分钟，最大 360 分钟；
- template ID 由 `alert_name → template_id` 注册映射确定，不接受用户/模型输入；
- `environment_id`、`tenant_id` 只来自 `RequestContext`。

`RuleBasedIntentInterpreter` 先判定唯一 intent，再仅执行该 intent 的槽位提取器。Prometheus 槽位闭集只有 `alert_name`、`instance`、`fingerprint`、`window_minutes`；禁止跨域提取 `sql`、`promql`、`template_id`、`gateway`、`operation`、`capability_id`、`policy_profile`、`approval_ref`、资产 selector 或 StarRocks filter。

支持的最小离线问法示例：

```text
查告警 HostHighCpu 在 node-1.example.com:9100 最近30分钟的证据
告警名=InstanceDown instance=10.0.0.8:9100 最近1小时证据
告警名=HostHighCpu instance=node-1:9100 fingerprint=fp-001 查证据
```

自然语言解析仅服务 fake eval，不代表真实模型接入；未匹配或缺少必填槽位时在 Planner 前拒绝/追问，不猜告警或目标。

### 6.3 target

`ResolvedTarget`：

- `provider="prometheus"`
- `resource_kind="alert_instance"`
- `resource_ids=("alert:<alert_name>", "instance:<canonical_instance>")`
- `selector_version="prometheus.alert.instance.v1"`
- tenant/environment 来自 context

fingerprint 是告警记录过滤条件，不改变被监控 instance 目标，因此不进入 target；它进入 step arguments 和 `plan_hash`。同一语义 instance 的大小写、hostname 末尾点、IPv6 压缩形式经规范化后必须得到相同 target fingerprint。

### 6.4 固定计划

```text
s1 get_active_alerts
   gateway: alertmanager（由 OperationSpec 派生）
   args: alert_name, instance, optional fingerprint, limit=5

s2 query_metric_range
   gateway: prometheus（由 OperationSpec 派生）
   depends_on: s1
   condition: PRIOR_STEP_RESULT_IS(s1, OK)
   args: promql, promql_template_id, instance,
         window_start, window_end, step_seconds,
         max_series=5, max_points_per_series=361
```

Runner 在 M6a 实现契约中已存在但当前未消费的 `PRIOR_STEP_RESULT_IS`。它的唯一真源是 `TaskStore.load_step_executions(task_id)` 返回的已提交 step journal，而不是当前进程的 `failed_steps` 内存集合：

1. `_run_steps` 在进入循环前从全部已提交 journal 行构造 `result_by_step: dict[str, StepResultStatus]`；
2. 遇到 `ALREADY_COMMITTED` 并采纳持久化结果后，以及每次新结果成功提交后，同步更新该 map；
3. `_condition_holds` 接收该 map。`PRIOR_STEP_RESULT_IS(ref, expected)` 在 ref 不存在时返回 false，存在时只做持久化状态与 expected 的精确比较；
4. `EVIDENCE_ROW_COUNT_BELOW` 先要求被引用 step 的持久化状态为 `OK`，再读取 ledger 行数；失败、超时或无 journal 记录不能被误判成“0 行”；
5. `failed_steps` 最多由持久化结果派生，用于聚合 degraded 状态，不能参与条件真值判定。

因此只有 s1 产生已提交的 `OK` step result 才运行 s2。s1 超时、错误、malformed 或没有已提交记录时 s2 必须跳过；进程重启后语义不变。s1 成功但返回 0 条仍运行 s2，因为“Alertmanager 当前未返回该告警”和“指标窗口事实”是两个可以并列保留的只读事实。测试覆盖首次执行和恢复/重启两条路径下的 OK、FAILED、TIMEOUT、无记录，并证明不需要修改 persistence 层。

计划不支持 step output substitution。`starts_at` 不动态扩大指标窗口，fingerprint 也不从 s1 反馈给 s2；这样恢复时计划仍逐字节稳定。若真实产品需要按告警开始时间动态派生窗口，应另立计划数据流契约，而不是在 adapter 或 Runner 中暗改下一步参数。

### 6.5 adapter 与 recording

新增但不从 `tools/__init__.py` 导出：

- `tools/alertmanager_fake.py::AlertmanagerRecordingAdapter`
- `tools/alertmanager_recording.py::default_alertmanager_recording`
- `tools/prometheus_fake.py::PrometheusRecordingAdapter`
- `tools/prometheus_recording.py::default_prometheus_recording`

共同规则：

- `IS_FAKE=True`；
- 按 canonical `ToolCall` 内容精确索引，无默认回退；
- 记录 `call_count` 与 `calls`；
- unknown operation、unknown template、超预算或未命中 recording 结构化失败；
- 不 import capabilities/planning，不连接网络，不读取环境变量或 secret；
- 只在 `interfaces/local_stack.py` 被运行时 import/装配。

Alertmanager fake 输出扁平标量行，字段白名单前的最大内部形状：

```text
fingerprint, state, alert_name, instance, severity,
starts_at, ends_at, silenced, inhibited
```

`annotations`、`generator_url`、原始 labels、receiver 和上游错误原文不进入 Evidence。

Prometheus fake 把 matrix 预归一为“一点一行”的扁平标量：

```text
series_key, metric_name, instance, timestamp_ms, value
```

不把任意 labels map、原始响应或 nested objects 塞进 `FrozenMap`。

### 6.6 Evidence、Answerability 与 Render

新增：

- `evidence/prometheus_alert.py`
- `reflection/prometheus_alert.py`
- `rendering/prometheus_alert.py`

s1 Evidence 只保留告警白名单；s2 builder 按 `series_key` 确定性分组并输出：

```text
metric_name, instance, point_count,
first_value, latest_value, min_value, max_value,
delta, trend
```

`trend` 闭集为 `rising / falling / flat`；非有限值、乱序时间、重复时间点、series/point 超限或字段类型错误不得被包装成成功证据。计算结果固定 round 到 6 位，不加入未提出产品需求的 p95、预测、异常分数或根因评分。

Answerability 真值表：

| Alertmanager | Prometheus | 结论 |
| --- | --- | --- |
| 精确 1 条 | 至少 1 个有效 series | `SUCCEEDED`，展示两类事实，不判断因果 |
| 精确 1 条 | 0 点 / 无有效 series | `INDETERMINATE`，缺指标相关证据 |
| 0 条 | 有指标 | `INDETERMINATE`，不能把指标事实绑定成当前告警事实 |
| 多条且无 fingerprint 消歧 | 任意 | `INDETERMINATE`，`needs_user_input=True`，请求 fingerprint |
| s1 失败/超时/malformed | s2 跳过 | `INDETERMINATE` |
| s1 成功 | s2 失败/超时/malformed | `INDETERMINATE` |
| Evidence 缺失或 capability key 不一致 | 任意 | fail-closed，不渲染成功 |

状态不是“firing”的活动记录仍可成功展示其事实，但文案必须原样说明 `state/silenced/inhibited`，不得改写成“告警仍在触发”。

## 7. `asset.inventory.lookup` 产品与技术设计

### 7.1 用户能力表现

用户给出一个精确资产标识后，小维返回唯一资产的受限事实：资产 ID、hostname、primary IP、资产类型、状态、owner team、service、OS family、environment ID，以及来源、采样时间和限制。

本轮不提供：列出全部资产、通配符、子串/拼音/模糊匹配、CIDR/range、跨环境选择、标签搜索、拓扑、巡检、变更或凭证信息。

### 7.2 selector 与 canonicalization

`planning/assets/params.py::AssetLookupParams` 要求下列三项恰好出现一项：

- `asset_id`：NFC 规范化；区分大小写；长度 `1..128`；只允许明确标识符字符；
- `hostname`：ASCII lowercase、去一个末尾点、逐 label 校验；禁止 wildcard；
- `ip`：用标准库 `ipaddress.ip_address()` 解析并输出 compressed 形式；拒绝 CIDR、range、zone id 和端口。

目标：

- `provider="asset_inventory"`
- `resource_kind="asset"`
- `resource_ids=("<selector_kind>:<canonical_value>",)`
- `selector_version="asset.exact.v1"`
- tenant/environment 只来自 context

这里的 canonical target 是“本次精确 selector 所指的查询目标”。M6a 不声称不同 selector 类型一定是同一资产别名，因此 `asset_id=A1` 与 `hostname=a1.example.com` 可以有不同 fingerprint；最终命中的 canonical `asset_id` 记录在 Evidence。若未来要求跨别名生成同一预执行 fingerprint，需要权威资产目录参与目标解析和新的两阶段计划契约，不能靠 fake 映射伪装已解决。

必须验证：

- hostname 大小写/末尾点等价输入得到同一 fingerprint；
- IPv6 等价文本得到同一 fingerprint；
- NFC 等价 asset ID 得到同一 fingerprint；
- selector kind 或 canonical value 变化会改变 fingerprint；
- context tenant/environment 变化会改变 fingerprint，并在恢复时触发 drift 拒绝；
- 用户文本里的 `tenant_id` / `environment_id` 不覆盖 RequestContext。

### 7.3 固定计划

```text
s1 lookup_asset
   gateway: asset_inventory（由 OperationSpec 派生）
   args: selector_kind, selector_value, field_set_id="asset.summary.v1", limit=2
```

`limit=2` 是为了区分 0、1、多个精确匹配；不是向用户开放的分页参数。无 selector、多个 selector 或 selector 不合法时不得生成计划，Gateway/adapter 调用次数均为 0。

### 7.4 adapter、Evidence 和渲染

新增但不导出：

- `tools/asset_inventory_fake.py::AssetInventoryRecordingAdapter`
- `tools/asset_inventory_recording.py::default_asset_inventory_recording`
- `evidence/asset_inventory.py`
- `reflection/asset_inventory.py`
- `rendering/asset_inventory.py`

recording key 必须包含：

```text
(tenant_id, environment_id, selector_kind, canonical_selector_value)
```

未命中不回退到其他 tenant/environment；跨 scope 查询表现为“该 scope 未命中”，不得泄漏另一个 scope 是否存在同名资产。

Evidence 字段白名单固定为：

```text
asset_id, hostname, primary_ip, asset_type, status,
owner_team, service, os_family, environment_id
```

adapter 即使返回 `credential`、`token`、`secret`、`password`、完整 tags、备注或其他自由文本，Evidence builder 也必须丢弃。`environment_id` 必须与 context/target 一致；0 条、2 条、字段冲突或 malformed 都不可成功。

Answerability 真值表：

| 精确匹配行数 | scope/字段一致性 | 结论 |
| --- | --- | --- |
| 1 | 一致且关键字段合法 | `SUCCEEDED` |
| 0 | 无事实 | `INDETERMINATE`，提示核对 selector 和 scope |
| 2 | 歧义 | `INDETERMINATE`，不得自动挑选 |
| 1 | environment/asset identity 冲突 | `INDETERMINATE` |
| timeout/error/malformed | 不适用 | `INDETERMINATE` |

## 8. 文件级 TDD 实施顺序

每个 task 都执行：先写失败测试并保留 red 证据，再写最小实现，再做 refactor 和局部门。不得先批量写实现后补测试。

### PR 1：共享 seam + Prometheus 告警证据

#### Task P0：ADR 与架构契约前置

**修改/新增**：

- `docs/adr/ADR-011-m6a-capability-binding-and-promql-template-admission.md`
- `ARCHITECTURE.md`
- `src/xiaowei_agent/contracts/capability.py`
- `src/xiaowei_agent/capabilities/specs.py`
- `tests/fakes/fixtures.py`
- `tests/security/test_scalar_strictness.py`
- `tests/unit/test_capability_spec.py`
- `tests/unit/test_plan_compiler.py`

**受影响回归承重点（不得为迁移改写既有行为期望）**：

- `tests/security/test_effect_classification.py`
- `tests/security/test_effect_single_source.py`
- `tests/security/test_hash_coverage.py`（仅确认本轮不改变 PlanStep/plan hash 字段）
- `tests/unit/test_hash_vectors.py`
- `tests/evals/test_l2_readonly.py`

**RED**：先让测试要求 `OperationSpec.gateway` 必填、非空，且同版本声明 golden 包含 gateway；确认旧实现失败。现有 StarRocks plan/tool hash vector 与 L2 输出必须作为兼容基线，任何无意的 version、PlanStep、typed arguments 或调用字节变化都先红。

**GREEN**：补字段和 ADR，明确 gateway 是版本化 operation metadata、Binding 不生成候选、PromQL 守卫在 StepAdmission 第 4 段；同时在 ADR-011 固化 §4.4 的一次性 StarRocks `1.0.0` 迁移例外，以及例外关闭后的版本/snapshot 递增规则。

**局部门**：

```bash
python -m pytest tests/unit/test_capability_spec.py tests/unit/test_hash_vectors.py tests/security/test_hash_coverage.py tests/security/test_effect_classification.py tests/security/test_effect_single_source.py -q
python -m pytest tests/evals/test_l2_readonly.py -q
```

#### Task P1：binding registry 与 Runtime 去 StarRocks 硬编码

**新增/修改**：

- `src/xiaowei_agent/application/capability_runtime.py`
- `src/xiaowei_agent/application/default_capabilities.py`
- `src/xiaowei_agent/application/runtime.py`
- `src/xiaowei_agent/_conformance.py`
- `src/xiaowei_agent/runners/binding.py`
- `src/xiaowei_agent/runners/runner.py`
- `src/xiaowei_agent/runners/deterministic.py`
- `src/xiaowei_agent/planning/starrocks/compiler.py`
- `src/xiaowei_agent/evidence/builder.py`
- `src/xiaowei_agent/rendering/pending.py`
- `src/xiaowei_agent/rendering/generic.py`
- `src/xiaowei_agent/interfaces/local_stack.py`
- `tests/unit/test_capability_runtime_registry.py`
- `tests/contract/test_runtime_facade.py`
- `tests/contract/test_runtime_async_lifecycle.py`
- `tests/contract/test_deterministic_runner.py`
- `tests/contract/test_protocol_conformance.py`
- `tests/fakes/capability_bindings.py`
- `tests/fakes/runtime.py`
- `tests/unit/test_local_stack.py`
- `tests/security/test_runtime_bypass.py`
- `tests/security/test_module_layering.py`

**RED**：

- registry 缺少/多出/重复 binding、entry operation 未声明、profile 不匹配均拒绝；
- CandidateSet 横跨两个 capability、无 entry candidate 或无 exact binding 均拒绝；
- Runtime 源码不再直接 import `planning.starrocks`、`SlowQueryParams`、慢查询 assess/render；
- Runner 源码不再固定 `GATEWAY_NAME`、`SlowQueryParams` 或 StarRocks evidence builder；
- terminal query 按 stored plan 选择 renderer；plan/evidence key 冲突拒绝；无 plan 的 pre-plan rejection 用通用投影；
- pending 文案移到 `rendering/pending.py`，不再借用慢查询 renderer；`rendering/generic.py` 只处理尚无 plan 的确定性拒绝，不猜领域事实；
- 合成写 capability 只通过 `tests/fakes/capability_bindings.py` 提供 test-only binding，永不进入生产 Registry/default bindings；
- 新增 Protocol 必须在 `_conformance.py` 被静态锚定，并通过 protocol signature/implementation 契约测试；
- 在现有 `test_module_layering.py` 新增精确文件级反例：`application/worker.py`、`interfaces/api.py`、`interfaces/cli.py`、`interfaces/worker.py` 不出现 capability ID/gateway 字符串，也不因 binding 引入而 import capabilities/planning/evidence/reflection/rendering/governance/tools；它们继续只做通用生命周期或入口职责；
- 现有 StarRocks 所有 contract/eval 结果保持不变。

**GREEN**：实现最小 binding seam，把 StarRocks 包装为第一个 binding；不改入口/API DTO，不加动态插件发现，不加 DSL。

**局部门**：

```bash
python -m pytest tests/unit/test_capability_runtime_registry.py -q
python -m pytest tests/unit/test_local_stack.py -q
python -m pytest tests/contract/test_runtime_facade.py tests/contract/test_runtime_async_lifecycle.py tests/contract/test_deterministic_runner.py tests/contract/test_protocol_conformance.py -q
python -m pytest tests/security/test_runtime_bypass.py tests/security/test_module_layering.py -q
python -m pytest tests/evals/test_l1_intent.py tests/evals/test_l2_readonly.py -q
```

#### Task P2：多 gateway 派生与已有条件语义

**修改/受影响回归**：

- `src/xiaowei_agent/capabilities/effect.py`
- `src/xiaowei_agent/runners/deterministic.py`
- `tests/security/test_effect_classification.py`
- `tests/security/test_effect_single_source.py`
- `tests/unit/test_plan_contracts.py`
- `tests/contract/test_deterministic_runner.py`
- `tests/security/test_runtime_bypass.py`
- `tests/security/test_step_condition_closed_set.py`

**RED**：

- ToolCall gateway 必须来自当前 plan key + operation 对应的 `OperationSpec.gateway`；
- step args 中伪造 `gateway` 不改变 ToolCall；
- unknown capability/version/operation 在 Gateway 前拒绝；
- `PRIOR_STEP_RESULT_IS` 在首次执行和恢复/重启中都对 OK/FAILED/TIMEOUT/无记录逐项 fail-closed；条件真值只读取由 `load_step_executions` 构造并随 committed/adopted result 更新的 `result_by_step`，不读取上一次进程的内存变量；
- `EVIDENCE_ROW_COUNT_BELOW` 对已提交 OK + 0 行成立，但对 FAILED/TIMEOUT/无记录均不成立，不能把失败伪装成空结果。

**GREEN**：Runner 只增加 metadata 派生和已存在条件枚举的执行语义，不增加 capability 分支。

**局部门**：

```bash
python -m pytest tests/unit/test_plan_contracts.py tests/contract/test_deterministic_runner.py tests/security/test_runtime_bypass.py tests/security/test_effect_classification.py tests/security/test_effect_single_source.py tests/security/test_step_condition_closed_set.py -q
```

#### Task P3：PromQL contract、params、compiler 与 guard

**新增/修改**：

- `src/xiaowei_agent/contracts/promql_surface.py`
- `src/xiaowei_agent/contracts/__init__.py`
- `src/xiaowei_agent/planning/prometheus/__init__.py`
- `src/xiaowei_agent/planning/prometheus/params.py`
- `src/xiaowei_agent/planning/prometheus/templates.py`
- `src/xiaowei_agent/planning/prometheus/compiler.py`
- `src/xiaowei_agent/governance/promqlguard.py`
- `src/xiaowei_agent/governance/step_admission.py`
- `tests/unit/test_prometheus_alert_params.py`
- `tests/unit/test_promql_compiler.py`
- `tests/security/test_promql_guard.py`
- `tests/contract/test_step_admission.py`

**RED**：覆盖两个模板 golden、重复运行逐字节相同、unknown template、raw PromQL、模板 ID/operation/gateway 污染、恶意 instance、超窗、超点、半信封、双信封、无 surface、篡改 query/args。

**GREEN**：实现两个固定模板和准入期重编译；不安装 PromQL parser，不增加任意 expression 入口。

**局部门**：

```bash
python -m pytest tests/unit/test_prometheus_alert_params.py tests/unit/test_promql_compiler.py -q
python -m pytest tests/security/test_promql_guard.py tests/contract/test_step_admission.py -q
```

#### Task P4：Prometheus capability、intent、target 与 plan

**新增/修改**：

- `src/xiaowei_agent/capabilities/prometheus_alert.py`
- `src/xiaowei_agent/capabilities/registry.py`
- `src/xiaowei_agent/capabilities/intent.py`
- `src/xiaowei_agent/planning/prometheus/compiler.py`
- `src/xiaowei_agent/governance/profiles.py`
- `src/xiaowei_agent/application/default_capabilities.py`
- `tests/unit/test_capability_registry.py`
- `tests/unit/test_governance_profiles.py`
- `tests/unit/test_intent_interpreter.py`
- `tests/unit/test_prometheus_alert_plan.py`
- `tests/security/test_intent_interpreter_boundary.py`
- `tests/security/test_intent_pollution.py`

**RED**：PR 1 的 capability 成对 golden 必须精确等于 `("snapshot.m6a.starrocks-prometheus.v1", (("starrocks.slow_query.diagnose", "1.0.0"), ("prometheus.alert.evidence", "1.0.0")))`；policy 成对 golden 必须精确等于 `("policy-2026-09-05", ("readonly.starrocks.slow_query.v1", "readonly.prometheus.alert.evidence.v1"))`；只增 specs/profile 而不改对应 snapshot/revision 必须红；intent 精确命中和 near-miss；缺 alert/instance；用户 env/tenant 不能改 context；gateway/profile/template 污染不能进入 draft；target 和 plan/tool hash 稳定。

**GREEN**：实现 Prom binding 和固定两步计划，把 capability snapshot 精确更新为 `snapshot.m6a.starrocks-prometheus.v1`、production policy revision 精确更新为 `policy-2026-09-05`；不修改 Resolver 算法，不增加关键词路由到入口。

#### Task P5：两个 fake adapter

**新增/修改**：

- `src/xiaowei_agent/tools/alertmanager_fake.py`
- `src/xiaowei_agent/tools/alertmanager_recording.py`
- `src/xiaowei_agent/tools/prometheus_fake.py`
- `src/xiaowei_agent/tools/prometheus_recording.py`
- `src/xiaowei_agent/interfaces/local_stack.py`
- `tests/unit/test_alertmanager_fake.py`
- `tests/unit/test_prometheus_fake.py`
- `tests/security/test_fake_isolation.py`
- `tests/security/test_gateway_boundary.py`
- `tests/unit/test_local_stack.py`

**RED**：精确 recording、无回退、调用计数、unknown op/template、预算、异常结构化、fake 不导出、除 local stack 外无生产 runtime import、Gateway 无 adapter fallback。

**GREEN**：只实现 synthetic recordings，不引入 requests/httpx/vendor SDK 或配置凭证。

#### Task P6：Prom Evidence / Reflection / Render

**新增/修改**：

- `src/xiaowei_agent/evidence/prometheus_alert.py`
- `src/xiaowei_agent/reflection/prometheus_alert.py`
- `src/xiaowei_agent/rendering/prometheus_alert.py`
- `src/xiaowei_agent/application/default_capabilities.py`
- `tests/unit/test_prometheus_alert_evidence.py`
- `tests/unit/test_prometheus_alert_answerability.py`
- `tests/unit/test_prometheus_alert_render.py`
- `tests/security/test_evidence_layer_purity.py`
- `tests/security/test_reflection_has_no_authority.py`

**RED**：字段白名单、系列摘要、非有限/乱序/重复点、状态真值表、无因果措辞、external annotations/labels/error 不进入事实与文案、ledger 清空后不能成功。

**GREEN**：实现纯 builder、纯 assessor、纯 renderer；不访问 Gateway、TaskStore、plan compiler 或模型。

#### Task P7：Prom 端到端、integration 与 L0-L2

**新增/修改**：

- `tests/fakes/prometheus_recordings.py`
- `tests/fakes/runtime.py`
- `tests/contract/test_prometheus_alert_runtime.py`
- `tests/integration/test_m6a_fake_capabilities_postgres.py`
- `tests/evals/corpus/m6a_prometheus_l0.json`
- `tests/evals/corpus/m6a_prometheus_l1.json`
- `tests/evals/corpus/m6a_prometheus_l2.json`
- `tests/evals/test_m6a_prometheus_l0.py`
- `tests/evals/test_m6a_prometheus_l1.py`
- `tests/evals/test_m6a_prometheus_l2.py`
- `scripts/compose_smoke.py`
- `tests/contract/test_compose_smoke_script.py`

**RED**：先落 §9.1 的完整矩阵，并证明每个 corpus case 都有执行 driver。

**GREEN**：扩展通用 RuntimeHarness，让 adapter/binding 通过显式参数装配，不复制一份 Prom Runtime；Compose smoke 增加一条 Prom 任务并验证 worker、持久化证据、终态查询和进程重启后的同一 renderer。

#### Task P8：PR 1 文档与收口

**修改/新增**：

- `src/xiaowei_agent/capabilities/doc.py`
- 由 generator 输出 `docs/CAPABILITIES.md`
- `README.md`
- `AGENT_HANDOFF.md`
- `docs/handoff/M6a-capability-extension-data.md`

**受影响回归承重点**：

- `tests/security/test_capabilities_doc.py`
- `tests/security/test_docs_command_consistency.py`

只记录 PR 1 的真实文件、测试、评审轮次和可靠工时；能力地图写 `tests`，明确 fake、未部署、未 canary、未用户验收。PR 1 验收并合入前不得开始 PR 2。

**局部门**：

```bash
python -m pytest tests/security/test_capabilities_doc.py tests/security/test_docs_command_consistency.py -q
```

### PR 2：资产精确查询

#### Task A1：selector schema、target 与 plan

**新增/修改**：

- `src/xiaowei_agent/capabilities/asset_inventory.py`
- `src/xiaowei_agent/capabilities/registry.py`
- `src/xiaowei_agent/capabilities/intent.py`
- `src/xiaowei_agent/planning/assets/__init__.py`
- `src/xiaowei_agent/planning/assets/params.py`
- `src/xiaowei_agent/planning/assets/compiler.py`
- `src/xiaowei_agent/governance/profiles.py`
- `src/xiaowei_agent/application/default_capabilities.py`
- `tests/unit/test_asset_lookup_params.py`
- `tests/unit/test_asset_target.py`
- `tests/unit/test_asset_plan.py`
- `tests/unit/test_capability_registry.py`
- `tests/security/test_intent_interpreter_boundary.py`
- `tests/security/test_intent_pollution.py`

**RED**：PR 2 的成对 golden 必须精确等于 `("snapshot.m6a.starrocks-prometheus-asset.v1", (("starrocks.slow_query.diagnose", "1.0.0"), ("prometheus.alert.evidence", "1.0.0"), ("asset.inventory.lookup", "1.0.0")))`；只增 specs 不改 snapshot ID 必须红；同时覆盖恰一 selector、canonicalization、fingerprint 等价/差异、context scope、无 wildcard/CIDR/range/port、多 selector、unknown selector、raw gateway/operation/profile 污染、固定一步计划。

**GREEN**：只增加资产领域 binding，把 snapshot 精确更新为 `snapshot.m6a.starrocks-prometheus-asset.v1`。预期不修改 Runtime、Runner、Gateway、StepAdmission 或公共生命周期契约；若必须修改，先停止并把它作为 PR 1 seam 缺陷回到独立上游修复，不在 PR 2 打兼容补丁。

#### Task A2：fake adapter 与 scope 隔离

**新增/修改**：

- `src/xiaowei_agent/tools/asset_inventory_fake.py`
- `src/xiaowei_agent/tools/asset_inventory_recording.py`
- `src/xiaowei_agent/interfaces/local_stack.py`
- `tests/unit/test_asset_inventory_fake.py`
- `tests/security/test_fake_isolation.py`
- `tests/security/test_asset_scope_isolation.py`
- `tests/unit/test_local_stack.py`

**RED**：recording key 四元组、无 scope fallback、0/1/2 行、unknown operation、无 selector 时 0 调用、跨 tenant/environment 不泄漏、fake 运行时 import 唯一落在 local stack。

**GREEN**：实现一个只读 fake adapter 和最小 synthetic catalog。

#### Task A3：Evidence / Reflection / Render

**新增/修改**：

- `src/xiaowei_agent/evidence/asset_inventory.py`
- `src/xiaowei_agent/reflection/asset_inventory.py`
- `src/xiaowei_agent/rendering/asset_inventory.py`
- `src/xiaowei_agent/application/default_capabilities.py`
- `tests/unit/test_asset_inventory_evidence.py`
- `tests/unit/test_asset_inventory_answerability.py`
- `tests/unit/test_asset_inventory_render.py`

**RED**：九字段白名单、secret-shaped extra fields 丢弃、scope/identity 冲突、0/2 行、timeout/malformed、Evidence key/plan key 冲突、无巡检/健康分/拓扑推断。

**GREEN**：实现三个纯函数模块，不加入通用 asset DSL。

#### Task A4：资产端到端、integration 与 L0-L2

**新增/修改**：

- `tests/fakes/asset_recordings.py`
- `tests/contract/test_asset_inventory_runtime.py`
- `tests/integration/test_m6a_fake_capabilities_postgres.py`
- `tests/evals/corpus/m6a_asset_l0.json`
- `tests/evals/corpus/m6a_asset_l1.json`
- `tests/evals/corpus/m6a_asset_l2.json`
- `tests/evals/test_m6a_asset_l0.py`
- `tests/evals/test_m6a_asset_l1.py`
- `tests/evals/test_m6a_asset_l2.py`
- `scripts/compose_smoke.py`
- `tests/contract/test_compose_smoke_script.py`

**RED/GREEN**：落 §9.2 全矩阵；Compose smoke 最终顺序跑 StarRocks、Prom、asset 三个独立任务，每个使用不同 idempotency key，并通过 API 查询持久化终态和 refs。任一能力失败都要能定位到自己的 task/capability/stage。

#### Task A5：能力地图、扩展数据与 M6a 收口

**修改/新增**：

- 由 generator 输出 `docs/CAPABILITIES.md`
- `docs/handoff/M6a-capability-extension-data.md`
- `docs/handoff/M6a-acceptance-report.md`
- `AGENT_HANDOFF.md`
- `README.md`

**受影响回归承重点**：

- `tests/security/test_capabilities_doc.py`
- `tests/security/test_docs_command_consistency.py`

能力地图最终恰含三个已声明 capability；扩展数据按真实两个 PR SHA 填写；给出 DSL 结论但不实现 DSL。完成后等待用户验收，不自行合并、不自行归档。

**局部门**：

```bash
python -m pytest tests/security/test_capabilities_doc.py tests/security/test_docs_command_consistency.py -q
```

## 9. 验收矩阵

### 9.1 Prometheus 独立矩阵

#### Unit / contract / security

- 两条模板逐字节 golden、参数 round-trip、plan/tool hash 稳定。
- Alertmanager 和 Prometheus 两个 gateway 都由 operation metadata 派生。
- raw PromQL、template ID、gateway、operation、policy、effect、approval slot 污染不生效。
- query/args/surface 任一篡改在准入阶段拒绝，两个 adapter 调用次数均为 0。
- 参数化篡改已存储计划第一步的 `alert_name`、`instance`、`fingerprint` 或固定 `limit` 后恢复，在首个 adapter 调用前以 `DriftError` 拒绝，Gateway/Alertmanager/Prometheus 调用均为 0；未篡改的等价恢复成功。
- Alertmanager 失败时 Prometheus 不调用；s1 空成功时 s2 可调用。
- `PRIOR_STEP_RESULT_IS` 在首次执行与进程重启后都只认 step journal：s1 为 OK 才运行 s2，FAILED/TIMEOUT/无记录均跳过；`EVIDENCE_ROW_COUNT_BELOW` 不把失败或无记录当作 0 行。
- 两 adapter 的异常、timeout、malformed 均结构化，不泄漏外部文本。
- annotation/URL/任意 labels 和多余字段不进入 Evidence/Render/trace detail。
- task query 和重启恢复按 plan key 选择 Prom renderer，不回退 StarRocks。

#### L0 corpus（安全，100% fail-closed）

至少覆盖：raw `promql`、未知/篡改 template、label matcher 注入、引号/反斜杠/换行、超窗、超点、双查询信封、半信封、跨 scope、已存储 `alert_name`/`instance` 漂移、gateway/operation/effect/approval 污染、external instruction、secret-shaped error、无证书调用。

每个拒绝 case 断言 Gateway 与两个 adapter 的实际调用次数；不能只断言抛异常。

#### L1 corpus（意图）

- golden 6 条；
- near-miss 6 条：Prometheus 巡检、Grafana dashboard、告警静默/创建、泛指标查询、StarRocks 慢查询、资产查询；
- missing 4 条：缺 alert、缺 instance、未知 alert template、非法 window；
- slot pollution 4 条。

#### L2 corpus（闭环）

1. 精确活动告警 + 有效指标；
2. suppressed/silenced 告警 + 有效指标；
3. 精确告警 + 无指标点；
4. 无告警 + 有指标；
5. 多告警无 fingerprint；
6. Alertmanager timeout，Prometheus 0 调用；
7. Prometheus timeout；
8. malformed adapter response；
9. 外部字段污染被丢弃；
10. 重复运行 plan/tool hash 与 RenderPayload 逐字节稳定（时间由 ManualClock 固定）。

### 9.2 资产独立矩阵

#### Unit / contract / security

- asset ID、hostname、IPv4、IPv6 canonicalization 与反例。
- exactly-one selector；missing/multiple/fuzzy/CIDR/range/hostname wildcard 均 0 调用。
- target fingerprint 稳定、scope 变化触发 drift。
- 参数化篡改已存储计划的 `selector_type` 或 `selector_value` 后恢复，在 adapter 前以 `DriftError` 拒绝，Gateway/asset adapter 调用均为 0；未篡改的等价恢复成功。
- adapter recording 不跨 tenant/environment 回退。
- 0/1/2 行和字段冲突的 Answerability 真值表。
- 九字段白名单；secret-shaped extra fields 不进入 Evidence/Render/trace。
- task query 和恢复选择 asset renderer。

#### L0 corpus（安全，100% fail-closed）

至少覆盖：多 selector、wildcard、CIDR、range、zone ID、带端口 IP、超长 selector、已存储 `selector_value` 漂移、tenant/env/gateway/operation/effect/approval 污染、跨 scope recording、重复精确行、secret-shaped adapter 字段、external instruction、无证书调用。

#### L1 corpus（意图）

- golden 6 条：asset ID/hostname/IPv4/IPv6 的中英文确定性问法；
- near-miss 6 条：列出全部、模糊找机器、网段查询、资产巡检、修改资产、Prom 告警证据；
- missing 4 条：无 selector、多 selector、非法 hostname、非法 IP；
- slot pollution 4 条。

#### L2 corpus（闭环）

1. asset ID 唯一命中；
2. hostname 唯一命中；
3. IP 唯一命中；
4. 0 行；
5. 2 行歧义；
6. timeout；
7. malformed；
8. extra sensitive fields 被丢弃；
9. 跨 tenant/environment 不泄漏；
10. 重复运行指纹、plan/tool hash 与 RenderPayload 稳定。

### 9.3 三能力共同回归

- Registry、binding key、profile 与 generated capabilities doc 集合完全一致。
- Resolver 对每次输入只命中一个 capability；near-miss 不串域。
- API、CLI、Worker、PostgreSQL、Compose 共用同一个 Runtime 和 Runner。
- StarRocks 现有全部 unit/contract/security/integration/eval 不退化。
- E1 调用次数恒为 0；无真实外部网络依赖或凭证。
- wheel 在不含 `tests/` 时仍可运行三个 fake 闭环。

## 10. 扩展性与 DSL 决策数据

`docs/handoff/M6a-capability-extension-data.md` 每个 PR 按精确 base/head SHA 记录：

| 维度 | 记录方法 |
| --- | --- |
| 新增/修改文件 | `git diff --name-status <base>..<head>` |
| 行数 | `git diff --numstat <base>..<head>`，只作辅助，不作单一结论 |
| capability 专属规则 | 人工列出 intent/params/target/compiler/evidence/answerability/render/adapter 规则 |
| 执行核心改动 | 单列 Runtime、Runner、Gateway、StepAdmission、生命周期/持久化、跨边界 contracts/canonical hash 的文件数、diff 行数和根因 |
| 注册/装配面改动 | 单列 `capabilities/intent.py`、`capabilities/registry.py`、`governance/profiles.py`、`application/default_capabilities.py`、`interfaces/local_stack.py` 以及能力地图生成的文件数、diff 行数和改动原因 |
| 重复形状 | schema/planner/policy/evidence/render/test fixture 逐类比较 |
| 测试/eval 数 | `python -m pytest --collect-only -q` 和 corpus case 计数 |
| 评审返工 | 按 review revision/commit 记录，不能凭印象 |
| 工时 | 只有可靠起止记录才填写；否则写“未采集，无可靠来源” |

判断规则：

- PR 2 若无需再改执行核心且没有复制执行生命周期，记为 seam 复用成功；不得写成“共享核心 0 改动”，因为注册/装配面本来就必须变化。
- PR 2 预期修改五个注册/装配真源；这些改动不否定 seam 复用，但其文件数、diff 行数、重复编辑形状必须单独记录。若每个新 capability 都要重复碰多个注册点，这正是评估声明式注册或 ADR-004 的决策数据，不能从统计中排除。
- 相同字段名不自动等于可抽象；只有语义、失败方式、policy、Evidence 和 eval 形状都相同才计为重复。
- 三个首批能力横跨 SQL 诊断、双源告警证据、精确资产查询，不预设它们是“同类能力”。
- 默认结论是“DSL 继续延期”。只有数据证明至少三个同类能力存在稳定重复、抽象能减少真实返工且不削弱 policy/query guard/evidence/eval，才建议另立 ADR-004；仍不在 M6a 写 DSL。

## 11. 文档与能力地图

`docs/CAPABILITIES.md` 不手工编辑。实现时调用 `render_capabilities_doc(StaticCapabilityRegistry().snapshot())` 生成，并由 `tests/security/test_capabilities_doc.py` 做逐字节检查。

能力地图表新增 `gateway` 列，最终列出五个 operation；页首继续明确：

- 最强证据为 `tests`；
- 只使用 fake/recording；
- 未连接真实系统；
- 未部署、未 canary、未用户验收。

README 只增加三条本地 fake 示例和边界导航，不复制本计划；ARCHITECTURE 只记录稳定 binding/query-guard 契约；当前 SHA、测试证据和风险只放 handoff/acceptance report。

## 12. 精确验证命令

### 12.1 每个 TDD task 的局部门

使用 §8 对应命令；所有触及 `planning/`、`governance/` 或 `tools/` 的 task 同时运行：

```bash
python -m pytest -m security -q
```

### 12.2 PR 1 独立门

```bash
python -m pytest tests/unit -q
python -m pytest tests/contract -q
python -m pytest tests/security -q
python -m pytest tests/evals/test_m6a_prometheus_l0.py tests/evals/test_m6a_prometheus_l1.py tests/evals/test_m6a_prometheus_l2.py -q
python -m pytest tests/evals/test_l0_security.py tests/evals/test_l1_intent.py tests/evals/test_l2_readonly.py -q
```

PostgreSQL integration 按现有受控测试入口执行，不新建真实外部系统连接。Compose smoke 必须从干净 checkout/build 运行，并保存实际命令、退出码和尾部输出。

```bash
PYTEST_POSTGRES_DSN='postgresql+asyncpg://postgres@127.0.0.1:5432/postgres' python -m pytest -q
python -m scripts.compose_smoke
```

第一条只允许指向专用、可清空的本地隔离 PostgreSQL；不得指向共享或业务数据库。设置 DSN 后 integration 必须 0 skip。

### 12.3 PR 2 独立门

```bash
python -m pytest tests/unit/test_asset_lookup_params.py tests/unit/test_asset_target.py tests/unit/test_asset_plan.py -q
python -m pytest tests/unit/test_asset_inventory_fake.py tests/unit/test_asset_inventory_evidence.py tests/unit/test_asset_inventory_answerability.py tests/unit/test_asset_inventory_render.py -q
python -m pytest tests/contract/test_asset_inventory_runtime.py -q
python -m pytest tests/security/test_asset_scope_isolation.py tests/security/test_fake_isolation.py -q
python -m pytest tests/evals/test_m6a_asset_l0.py tests/evals/test_m6a_asset_l1.py tests/evals/test_m6a_asset_l2.py -q
```

### 12.4 每个 PR 收尾四条规范门

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

再执行：

```bash
git diff --check
git status --short --branch
git diff --stat <base>...HEAD
git diff <base>...HEAD -- . ':(exclude)docs/CAPABILITIES.md'
git diff <base>...HEAD -- docs/CAPABILITIES.md
```

只暂存本 PR 文件，禁止 `git add -A`。提交前运行现有 secret-shaped literal 安全门；任何伪 secret 必须按项目规则拆开写。

### 12.5 TDD 反证

至少做下列隔离变体并保存 red 证据：

1. PromQL guard 暂时跳过重编译，篡改 query 测试必须红；
2. operation gateway 暂时改为从 step args 读取，gateway 污染测试必须红；
3. asset recording 暂时去掉 tenant/environment key，scope 隔离测试必须红；
4. Evidence builder 暂时放行一个敏感额外字段，白名单测试必须红；
5. binding registry 暂时允许 fallback，缺 binding/错误 renderer 测试必须红。
6. Registry 临时新增/删除一个 spec 但保持 snapshot ID 不变，成对 declaration golden 必须红；
7. 条件判断临时改回只读进程内 `failed_steps`，恢复/重启的 OK/FAILED/TIMEOUT/无记录矩阵必须红；
8. 恢复期临时跳过 plan hash/target fingerprint 比对，篡改 `selector_value`、`alert_name` 或 `instance` 的 0 调用测试必须红；
9. 临时在 API/Worker/CLI 任一入口加入 capability ID、gateway 分支或领域 import，入口中立性测试必须红；
10. 临时 bump StarRocks version 或改变 PlanStep/typed arguments，既有 hash vector 或 L2 兼容测试必须红。

变体使用独立 pycache，不把临时代码提交或混入用户工作树。

## 13. PR、回滚与合并边界

### 13.1 PR 1

- 单一目的：建立多能力/多 gateway 的最小 seam，并交付 Prom 告警证据 fake 闭环。
- 必须包含 StarRocks 完整回归证据。
- 不包含 asset 实现。
- 项目负责人验收并合入后才关闭分支；Codex 不自行合并。

### 13.2 PR 2

- 单一目的：消费已验收 seam，交付资产精确查询 fake 闭环和 DSL 数据结论。
- 不修改 Runtime/Runner/Gateway/StepAdmission/持久化或公共生命周期/哈希契约；发现需要时停止，另做上游根因修复并重新过 PR 1 gate。
- 允许且预期修改 intent、Registry、profile、default bindings、local stack 等注册/装配面；必须按 §10 独立计数，不能把这些必要改动隐去后宣称“零共享改动”。
- 不包含巡检、报告、Grafana、真实资产系统或后续里程碑。

### 13.3 回滚

- PR 2 可独立回滚：从 Registry/binding/profile/local stack 移除 asset capability 和 adapter，重新生成能力地图；Prom/StarRocks 不受影响。
- PR 1 的 Prom capability 可从 Registry/binding/profile/local stack 移除并重新生成能力地图；共享 seam 只有在 StarRocks 全门仍绿且无持久化事实依赖时才可另行回滚。
- 不删除用户未跟踪文件，不清理用户 Compose volume；如需清理测试数据，使用 README 已批准的显式命令并先说明可恢复性。

## 14. 明确非目标

- 不连接真实 Prometheus、Alertmanager、Grafana、资产系统或 StarRocks。
- 不实现真实凭证、URL、TLS、auth、secret reference 或数据保留策略。
- 不引入 Grafana；不查询 dashboard、datasource 或 alert rules。
- 不创建/静默/确认/关闭告警，不做 Alertmanager 写操作。
- 不接受任意 PromQL、PromQL 片段、任意 label matcher 或模型生成查询。
- 不做自动 RCA、因果链、跨 capability 自动扩张计划或多 Agent 调查。
- 不做 Prometheus 单机/组件/全平台巡检、健康分、HTML 报告。
- 不增强或真实验证 `starrocks.slow_query.diagnose`。
- 不做资产模糊搜索、列表、标签浏览、拓扑、巡检或修改。
- 不实现通用 capability DSL、通用 query DSL、动态插件发现或新框架。
- 不改变 API/CLI 对外协议，不实现 Web/飞书。
- 不开放 E1，不调用真实模型。

## 15. 审核裁定与待拍板提案

本 V1.1 已吸收独立 Claude 静态审核；以下具体提案仍由项目负责人最终拍板，任何修改应先回写本计划：

1. PR 拆分固定为“共享 seam + Prom”与“asset 消费 seam”两个顺序独立 PR。
2. Prom fake catalog 固定支持 `HostHighCpu`、`InstanceDown` 两条模板。
3. Prom 默认窗口 30 分钟、上限 360 分钟、step 60 秒、最多 5 series、每 series 最多 361 点。
4. Alertmanager 查询要求 `alert_name + instance`，fingerprint 只作可选消歧。
5. Alertmanager s1 失败时 s2 不执行；s1 成功但 0 行时 s2 执行。
6. asset selector 三选一；不同 selector 类型不承诺预执行 fingerprint 等价。
7. 资产 Evidence 使用 §7.4 的九字段白名单。
8. `OperationSpec.gateway` 是多 adapter 路由唯一真源；不升级 Plan schema。首次把既有 StarRocks 硬编码显式化时保持 `starrocks.slow_query.diagnose@1.0.0`，作为 ADR-011 唯一迁移例外；以后同一 operation 改 gateway 必须递增 capability version 与 snapshot ID。
9. PromQL 采用固定模板 + schema + escape + 重编译比对，不新增 parser 依赖。
10. DSL 默认继续延期，M6a 只提交数据结论；执行核心复用和注册/装配成本必须分开计量。
11. PR 1/PR 2 snapshot ID 分别固定为 `snapshot.m6a.starrocks-prometheus.v1` 与 `snapshot.m6a.starrocks-prometheus-asset.v1`，并与 ordered specs key 做成对 golden。
12. PR 1 的 production policy revision 递增为 `policy-2026-09-05`，并与 ordered profile ID 做成对 golden；PR 2 新增生产 profile 时再次递增。

### 15.1 独立 Claude 审核问题的根因与实际影响

| 问题 | 根因判断 | 不修的实际影响 | 本计划的根因修正 |
| --- | --- | --- | --- |
| snapshot ID 未进入任务 | 把能力声明集合当作普通注册数据，遗漏了候选、审计和能力地图共同使用的版本身份 | specs 已变而 snapshot 仍伪装成 M3 单能力，审计/文档无法可靠复现声明集合 | §4.5 固定每个 PR 的 exact ID，并用 `(snapshot ID, ordered keys)` 成对 golden 承重 |
| StarRocks gateway 与 version 规则冲突 | 把首次显式化既有事实和未来行为变更混为一类 | 实现阶段要么违反计划，要么无意义 bump version，连带打穿 golden/持久化兼容 | §4.4 与 ADR-011 固化一次性保持 `1.0.0` 的迁移例外，并用 hash/L2 证明无行为变化 |
| “共享核心 0 改动”统计失真 | 未区分执行核心与注册/装配真源 | 会把每加能力重复编辑五个注册点的成本藏掉，污染 DSL 决策 | §10、§13.2、§17 分层计数和裁定 |
| 无查询信封步骤缺少安全论证 | 计划只描述“可继续”，没有列出其真正依赖的确定性保护，也没有标注它不等价于 SQLGuard | 容易误认为 asset/Alertmanager 可跳过参数/恢复校验，未来真实 adapter 可能沿用错误边界 | §5.2 明确四层保护、证据上限、恢复期 drift 正反例 |
| `PRIOR_STEP_RESULT_IS` 真源不具体 | 只写“step journal”，没有定义 Runner 如何装载、采纳和更新结果状态 | 首次执行看似正确，进程重启后可能因内存集合丢失而错误运行/跳过后续步骤 | §6.4 固定 `result_by_step` 构造与更新语义，并补 OK/FAILED/TIMEOUT/无记录恢复矩阵 |
| 文件清单与当前源码不一致 | 计划按概念猜测试名，未沿 Protocol、composition root、intent pollution、文档一致性调用链穷举承重点 | 实现会漏改静态锚点或漏跑已有安全回归，也可能引用不存在的测试 | §8 改为当前真实文件并补齐 conformance/local stack/intent/docs/effect/hash 测试 |
| 入口中立性未承重 | “入口保持薄”仅是设计假设，没有针对 binding 重构的静态反例 | capability 分支或领域 import 可能悄悄进入 API/Worker/CLI | P1 在现有 `test_module_layering.py` 增加精确文件级测试，对四个入口/worker 模块做禁止字符串与禁止依赖断言 |

上述提案已获项目负责人批准；实现仍按两个顺序独立 PR 和各自验收门执行。

## 16. 完成时的四类收口

### 已验证

- 精确 base/head SHA、PR、真实 diff。
- 实际执行的 unit/contract/security/integration/eval/Compose/四条规范门及退出码、尾部输出。
- TDD red→green 和隔离反证。
- generated capability map 与 Registry 一致。

### 只读推理

- binding seam 是否让第二个能力保持执行核心零改动；注册/装配面的重复成本另行量化，不混为“零共享改动”。
- OpenSRE 模式哪些适合小维、哪些因执行权模型不同而不适合。
- DSL 是否应继续延期的架构判断；必须紧邻真实扩展数据。

### 未覆盖

- 真实 Alertmanager/Prometheus/资产 API、auth、TLS、限流、分页和版本差异。
- 真实 alert rules 与模板 catalog 的对应关系。
- 真实标签基数、时间序列规模和资产字段质量。
- 部署、canary 和用户验收。

### 残余风险

- 两个 synthetic Prom 模板不能证明真实规则兼容。
- 不同资产 selector 可能指向同一资产，但 M6a 只保证各 selector 自身 canonical。
- 固定模板安全不等于任意 PromQL 安全。
- binding 是显式 Python 配置；类型和测试降低误配风险，但 Python 不能提供语言级不可绕过性。
- fake adapter 的成功不代表真实外部系统错误语义、时钟或数据完整性已验证。

## 17. 退出标准

M6a 只有在以下条件全部满足时才可提交用户验收：

1. 两个独立 PR 均有精确 SHA 和各自完整测试证据；PR 2 基于已验收合入的 PR 1。
2. StarRocks、Prom、asset 三个 fake 闭环都通过独立 unit/contract/security/integration/L0-L2。
3. PromQL 任意输入、目标/scope 漂移、外部文本污染和 fake 旁路均 fail-closed。
4. Prom 与 asset 未复制入口、Runtime、Runner、Gateway 或终态渲染真源。
5. PR 2 无执行核心或公共生命周期/哈希契约改动；注册/装配面已单独计数并纳入 DSL 判断。若出现执行核心改动，已回到独立上游根因修复并重新验收。
6. `docs/CAPABILITIES.md` 由最终 Registry 生成，且只声称 `tests`。
7. 扩展数据文档给出有证据的 DSL 结论，没有实现 DSL。
8. 无真实运维目标/模型调用、无真实凭证、无 E1、无后续里程碑实现。
9. 完成报告按“已验证、只读推理、未覆盖、残余风险”收口。
10. Codex 等待用户验收，不自行合并；只有用户明确说“验收通过，归档”后才归档任务。

## 18. 计划自审

- [x] 只覆盖 M6a 两个 fake 能力。
- [x] 以 OpenSRE 当前精确 SHA 为依据，并剔除旧搜索缓存路径。
- [x] 保留 Resolver 唯一候选真源和确定性执行链。
- [x] 没有任意 PromQL、任意 executor、DSL 或动态插件机制。
- [x] Prometheus + Alertmanager 的真实拓扑在两个 gateway 中表达，Grafana 不入场。
- [x] StarRocks 只作回归基线，慢查询增强和真实连接不入场。
- [x] 巡检与报告不入场。
- [x] 两个 PR 能分别审核和回滚。
- [x] 每个能力有独立 unit/contract/security/integration/L0-L2 门。
- [x] 每个 PR 的 snapshot ID 与 ordered specs key 成对固定，能力集合变化不能沿用旧 snapshot。
- [x] StarRocks `1.0.0` 仅有一次性 gateway 显式化例外，未来 gateway 变化必须 bump version/snapshot。
- [x] 无查询信封步骤没有被包装成等价 SQLGuard；首次执行、恢复漂移和 fake recording 三层参数边界均有正反例。
- [x] `PRIOR_STEP_RESULT_IS` 与行数条件以持久化 step journal 为真源，并覆盖进程重启。
- [x] Protocol conformance、local stack、intent pollution、能力文档/README 一致性和入口中立性承重点已纳入文件清单。
- [x] 执行核心与注册/装配面分开计量，DSL 结论不会隐藏五个注册点的重复成本。
- [x] 能力地图由 Registry 生成，不手工维护。
- [x] 所有真实验证与用户验收状态均未被提前声称。
- [x] 计划结束后等待审核，不把写计划当成开始实现。
