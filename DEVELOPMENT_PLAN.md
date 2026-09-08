# 小维 Agent 2.0 总体开发计划

> 状态：Approved V2.2。V2 于 2026-09-01 由项目负责人批准；V2.1 于 2026-09-07 为 M7 PR 1–3
> 增加离线窄例外；V2.2 于 2026-09-08 将该例外扩至 M7 PR 1–8，同时把真实应用、凭据、网络、
> 部署与 canary 保留为独立硬门。每个阶段仍需详细计划、文档基线与明确开工口令。

## 1. 文档定位

本计划把 [ARCHITECTURE.md](ARCHITECTURE.md) 的目标架构拆成可独立验收的里程碑。五份优先文档各自只承担一种责任（顺序与 `AGENTS.md`「优先阅读」一致）：

- `AGENTS.md`：稳定的协作、安全和验收规则。
- `ARCHITECTURE.md`：目标架构、模块边界和不可变量。
- `AGENT_HANDOFF.md`：当前已发生的事实、证据、风险和下一步。
- `README.md`：人类开发者的定位、启动、目录和快速路径。
- `DEVELOPMENT_PLAN.md`：实施顺序、决策门、交付物和退出标准。

若计划与 `AGENTS.md` 或 `ARCHITECTURE.md` 冲突，先停止实现并修改计划，不能用计划降低安全边界。每个里程碑开始前再编写该里程碑的函数级实施计划；不为尚未进入的阶段提前创建空目录、占位接口或框架代码。

## 2. 规划前提

本节只记录规划所依赖的稳定前提。**易漂移的 commit SHA、分支名、里程碑进度和验证证据一律只放 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)，本文不复制。**

- 项目从 0 开始建设，没有可复用的既有业务代码、测试、依赖或容器配置。
- 首批能力、初始执行上下文和真实调用许可由 [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) 固化；工程与测试基线由 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md) 固化。
- API、Worker、PostgreSQL、真实工具调用、部署、canary 和用户验收在本计划的规划范围内均不得被声称存在，除非 handoff 已记录对应证据。
- Git 远程与 CI 待 M1 单独拍板；在此之前不绑定远程、不创建 PR、不配置 CI。

因此 M0 验收通过后的第一个动作不是接入模型或基础设施，而是先编写并审批 M1 详细实施计划。

## 3. 路线选择

### 3.1 采用的路线

采用“薄 Foundation + 第一条只读垂直闭环 + 持久化生命周期”的顺序：

```text
设计收口与本地 Git 基线
  → 工程与 CI 基线
  → 稳定契约内核
  → StarRocks 只读闭环（fake）
  → PostgreSQL TaskStore
  → API / CLI / Worker / Compose
  → 第二、第三条能力（fake）
  → 测试环境真实只读验证
  → Web / 飞书渠道
  → 受控写闭环
  → LangGraph 准入评估
```

这条路线先写支撑首个闭环所需的最小契约，随后用真实垂直场景反证边界，再增加耐久性和部署形态。

**M7 窄例外（2026-09-08，经项目负责人明确批准）**：M6b 真实验证延期期间，M7 可离线实施
详细计划中标注为「离线实现门」的工作——当前 V0.6 对应 PR 1–8。允许锁定渠道 SDK、实现默认
关闭的 listener/worker/Web 进程、Web UI 与本地 Compose，并使用 fake/recording 完成测试；不允许
注册真实应用、读取真实 secret、发起真实飞书或运维目标网络调用、部署或 canary。真实渠道激活、
部署与 canary 仍须满足详细计划的独立硬门；该例外不改变 M6b 的证据等级，也不降低 M7 最终退出
标准。PR 编号仅是当前版本映射，阶段门标签才是本总体计划的规范锚点。

### 3.2 不采用的路线

- 不先横向实现所有抽象层：在没有调用者前，无法证明 DTO、Protocol 和目录边界正确。
- 不先搭完整基础设施：Compose、队列和 Worker 不能代替能力、策略和证据闭环。
- 不直接复制旧项目：旧项目仅用于样本和安全 oracle，新项目的契约和证据独立形成。
- 不先引入 LangGraph、ReAct、多 Agent、Redis、Kafka 或向量库：必须由可测瓶颈或生命周期样本触发。
- V1 不实现通用 capability DSL，也不做多证据源自适应诊断。M0-M9 使用显式 `CapabilitySpec` 和确定性 compiler；M6a 记录真实复用与维护数据。只有至少三个同类能力证明 DSL 能减少重复、且不削弱 policy/SQLGuard/evidence/eval 契约时，才先写 ADR-004 再立项。

## 4. 全程不可破坏的约束

所有里程碑共同遵守。**授权边界的真源是 [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)，工具链与验证命令的真源是 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)，架构契约的真源是 [ARCHITECTURE.md](ARCHITECTURE.md)；本文只承载里程碑顺序、决策门与验收标准。**

1. 模型只产生 `IntentDraft`、解释或建议，不决定 capability、目标、SQL、审批或工具调用。
2. 执行链固定为 `IntentDraft → CapabilityResolver → PlanCompiler → WorkflowRunner(step) → StepAdmission → ToolGateway → Readback（需要时）→ Evidence → Outcome`。
3. `StepAdmission` 由 Runner 在每个具体步骤前调用；顺序是 `ToolPolicy → SQLGuard（需要时）→ ApprovalGate（副作用步骤）`。
4. `CapabilityResolver` 是唯一候选生成真源，shadow 只消费同一份 `CandidateSet` 并 record-only。
5. 领域代码不能直接持有外部客户端；adapter 只能经 `ToolGateway` 调用。
6. SQL 只能由确定性 compiler 产生，并经 `sqlglot` AST 规则验证；解析失败时 fail-closed。
7. 外部日志、错误、网页、知识和用户粘贴内容统一按 `ExternalContent` 处理。
8. TaskStore 是生命周期事实真源；终态保护、CAS、lease 和 fencing 由存储层保证。
9. 对被管运维目标的真实写操作（E1）必须经过 precheck、审批暂停、恢复重解析、hash/fingerprint 复核、单次写入、readback；不能承诺 exactly-once。E1 的定义与 fail-closed 规则见 [ADR-007 D7](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)。
10. 每个非平凡行为变更必须留下测试、契约、eval、SOP 或配置护栏中的至少一项。

## 5. 待拍板事项与建议默认值

| 决策 | 建议默认值 | 最迟拍板点 | 未拍板时行为 |
| --- | --- | --- | --- |
| 初始执行上下文 | 单租户开发；`RequestContext` 的 `tenant_id`、`actor`、`environment_id` 必填，`RequestEnvelope.environment_id` 可选；模块边界显式传递 `RequestContext` | M0 已拍板（ADR-007） | 使用固定开发租户；禁止生产连接 |
| 首个能力 | `starrocks.slow_query.diagnose`，只读、限定时间窗和字段白名单 | M0 已拍板（ADR-007） | 不进入 M3 |
| 第二个能力 | `prometheus.alert.evidence`，只允许注册模板生成 PromQL | M0 已拍板（ADR-007） | 不创建 capability |
| 第三个能力 | `asset.inventory.lookup`，仅精确资产标识查询 | M0 已拍板（ADR-007） | 不创建 capability |
| 外部调用许可 | 真实运维目标系统与真实模型 API 的**网络调用**在 M0-M6a 全程禁止，领域 adapter 只用 fake/recording；本地隔离 PostgreSQL/Compose 经 M4/M5 各自里程碑批准后允许，CI 对应 job 同样在该时点之后才可使用（ADR-007 D8）；StarRocks 非生产只读需 M6b 单独授权 | M0 已拍板（ADR-007） | 禁止任何真实连接 |
| 写操作许可（E1） | E1 = **任何可能修改被管运维目标状态的操作**，与是否经 `ToolGateway`、是否被标记 `side_effect=True` 无关（ADR-007 D7）；合法 E1 执行必须经 `ToolGateway` 且 `side_effect=True`，绕过 Gateway 的直接调用与内部持久化旁路同属违规。**M0-M7 全程禁止 E1，含非生产环境**；M8 才可开放一条低风险测试环境受控写，且须 ADR-005 定稿、项目负责人批准、ADR-007 记录明确例外或完成修订三者齐备；**生产连接与生产写默认禁止**，需新的独立授权和独立验收计划，不能由 M8 结论推导。TaskStore/approval/audit/evidence 的内部持久化、本地 migration 和测试 fixture/recording **不属于 E1** | M0 已拍板（ADR-007 D6/D7）／M8 逐条复核 | 不开放任何环境的 E1 操作 |
| Git 与 CI | M0 已初始化本地 `main` 基线；远程与 CI 由 M1 单独拍板后绑定 | M1 | 不伪造远程、PR 或 CI |
| Python 工具链 | Python 3.11（首个且唯一强制验证版本）；pytest；Ruff（唯一 linter）；mypy | M0 已拍板（ADR-008） | 不同时引入第二套 runner/linter/type checker |
| 证据保留 | M0-M6a 只保存脱敏 fixture/recording；真实保留周期和大对象后端在 M6b 前决定 | M6b | 不落真实原始 rows 或 secret |
| 审批语义 | 到 M8 前确定主体、渠道、有效期、拒绝/过期/冲突语义 | M8 | 不开放 E1 |
| 模型供应商 | 核心测试使用 fake interpreter；M5 后可单独决策是否**实现** provider adapter，但**实现权不等于调用权**；发起真实模型网络调用必须另设独立里程碑，并单独批准 ADR、凭证引用、数据范围和保留策略，且不得与 ADR-007 的 M0-M6a 禁止期冲突 | M5 仅限 adapter 实现决策；真实调用另设独立里程碑 | 不增加外部模型依赖，不发起任何真实模型调用 |
| 通用 capability DSL | V1 明确延期；M6a 只采集复用、改动文件、工时（如有可靠记录）和返工数据 | M9 后的新立项 | 继续使用显式 CapabilitySpec，不建 DSL 框架 |
| 多证据源自适应诊断 | V1 非目标；先验证三个有界、单能力闭环 | M9 后的新立项 | 不允许无界反思或跨能力自动扩张计划 |

首批三个能力已由 ADR-007 固化。后续替换必须先修订 ADR-007，且替换项仍必须满足：只读、可 fake、可确定性规划、可形成证据契约、可构造 adversarial cases。

## 6. 里程碑总览

| 里程碑 | 对应架构阶段 | 可验收结果 |
| --- | --- | --- |
| M0 设计、计划与本地 Git 基线 | Phase 0 | 当前文档集形成可追踪基线，计划无关键歧义，首个能力和权限边界已拍板 |
| M1 工程与 CI 基线 | Phase 0 | 可安装、可测试、可静态检查、有 CI gate 的最小 Python 项目 |
| M2 契约内核 | Phase 1 | 核心 DTO、`ExternalContent`、TaskStore 并发交互形状、hash 规则和 fake Gateway 通过契约/安全测试 |
| M3 第一条只读闭环 | Phase 2 | StarRocks fake 闭环通过 L0-L2 eval，副作用测试步骤证明 ApprovalGate 不能被绕过 |
| M4 TaskStore 与恢复 | Phase 3 | PostgreSQL 上的幂等、CAS、lease、fencing、恢复和终态保护可证明 |
| M5 API / CLI / Worker / Compose | Phase 4 | 本地 Compose 中可通过薄入口运行 fake 闭环并恢复任务 |
| M6a 第二、第三条 fake 能力 | Phase 5 | Prometheus、资产查询两个独立闭环验证扩展性并产生 DSL 决策数据 |
| M6b StarRocks 测试环境真实只读 | Phase 5 | 在明确授权的非生产环境完成 StarRocks 真实 adapter 与只读运行验证 |
| M7 Web / 飞书渠道 | Phase 5 | 多渠道只做协议与渲染，复用同一 Runtime 结果 |
| M8 受控写闭环 | Phase 5 | 测试环境中一条低风险写能力完成审批、恢复、readback 和故障注入验收 |
| M9 Runner 准入评估 | Phase 6 | 用量化证据决定继续 DeterministicRunner 或新增 LangGraph adapter；**不授予 Multi-Agent 权限** |

只读 V1 的候选发布点要求 M6a、M6b 和 M7 各自通过退出门；受控写 V1 的候选发布点是 M8。代码测试通过不等于发布，仍需按 `declared → configured → deployed SHA → tests → canary → user-accepted` 记录最强证据。

## 7. 各里程碑实施与退出标准

### M0：设计、计划与本地 Git 基线

**目标**：让项目负责人能批准一个无隐含默认值、且后续变更可按 SHA 追踪的架构和实施顺序。

**工作项**：

- 开始 M0 时先检查 `.gitignore`、敏感信息和纳入范围；以当时的五份 Markdown 与 `.gitignore` 初始化本地 `main` 并记录基线 SHA。Git 初始化前发生的修订明确标记为“无 SHA 历史”，不伪造提交证据。
- 从该基线创建 `claude/m0-plan-closure` 分支；之后的文档收口只暂存明确文件，并用 commit SHA 审查。
- 逐条处理 Claude 已给出的 P1-P3 意见；新增意见继续按严重级别记录。
- 收敛 `ARCHITECTURE.md` 中 Phase 0/Phase 1 对契约和脚手架的重叠：Phase 0 只负责仓库/工具/测试骨架，Phase 1 才实现业务契约。
- 复核 README 架构图与稳定执行链一致，明确 `StepAdmission` 在 Runner 的步骤边界内执行。
- 由项目负责人拍板首批能力、初始执行上下文和真实调用许可；Git 远程与 CI 推迟到 M1 单独拍板。
- 形成 ADR-007（首批能力、初始执行上下文与真实调用许可）和 ADR-008（工程与测试基线）；其他 ADR 到其首次承重前再写。

**退出标准**：

- Claude 审查意见已逐条处理或明确拒绝并说明理由。
- 本地 `main` 基线和 M0 文档分支均有可核对 SHA；远程、PR 和 CI 若尚未配置则明确写“未验证”。
- 文档不存在相互冲突的调用链、目录落点、状态语义或测试命令。
- 首个能力有稳定 ID、输入边界、证据目标、非目标和真实调用级别。
- `AGENT_HANDOFF.md` 明确记录“计划已批准”，而不只是“计划已写”。

**本阶段不做**：依赖安装、业务代码、真实服务调用、未经确认的远程或 CI 绑定。

### M1：工程与 CI 基线

**目标**：建立最小、可重复、可审查的 Python 工程，不创建没有调用者的业务模块。

**交付物**：

- 基于 M0 的本地 `main` 基线创建 M1 分支；绑定经确认的远程和 CI。
- `pyproject.toml`：Python 3.11、构建配置、运行依赖与 dev 依赖分组。
- 最小 `src/xiaowei_agent/` 包；只创建 M1 实际使用的配置、日志/trace 和版本模块。**M1 只建立通用的结构化日志、`trace_id` 传递和脱敏基础，不定义任何业务 trace/audit 事件契约，也不提前实现业务 DTO**——这些属于 M2。
- pytest 目录与 `security` marker；Ruff、mypy 和 CI 命令保持单一真源。
- 至少一条真实承重的 security 测试验证配置/日志脱敏，确保安全 gate 不是空集合。
- `.env.example` 只列非敏感变量名和安全默认值；启动时对缺失/非法配置 fail-fast。
- README 补充实际可运行的安装、检查和测试命令。
- CI 不注入测试环境或生产环境凭证；**M1 与 CI 不执行任何真实外部调用**，包括真实模型 API、StarRocks、Prometheus、资产系统和任何 E1 操作。**M1 阶段 CI 只允许写测试产物（fixture、recording、覆盖率、报告），不使用 PostgreSQL，也不使用 Compose**；本地隔离 PostgreSQL 与 Compose 要到 M4/M5 各自批准后，才允许对应 CI job 使用（ADR-007 D8）。所有阶段 CI 的 E1 调用次数恒为 0。
- 真实调用的开放点按类别分别管理，**不得写成「所有真实调用只能发生在 M6b」**：当前已批准路线中的**首个真实运维目标调用**是 M6b 的 StarRocks 非生产只读（ADR-007 D 层，仍需单独授权）；真实模型 API 调用遵守 ADR-007 B2，需另设独立里程碑与独立 ADR/凭证/数据/保留策略授权；M8 的测试环境受控写遵守 ADR-007 E1 与 D6 三项门。

**验证门**：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

CI 必须分别运行全量测试和 security marker，不用一次全量结果替代安全 gate。

**退出标准**：从干净 checkout 按 README 能安装并运行上述命令；CI 绑定真实 commit SHA，且扫描配置可证明没有测试/生产凭证和真实外部调用；无 secret 和空业务脚手架。

**退出标准修订（2026-09-01，经项目负责人明确批准）**：默认分支保护原为 M1 退出标准的一部分，但仓库为 private + GitHub Free，该能力不可用（API 实证 `403 Upgrade to GitHub Pro or make this repository public`）。退出标准调整为「**CI 已建立并六个 gate 全绿；分支保护延后至套餐具备或仓库改为 public 时**」。这是**显式接受的治理风险**，不是能力缺失被掩盖：在保护建立之前，红灯 PR 仍可被人工合并，合并纪律只能靠人。该风险记入 `AGENT_HANDOFF.md` 残余风险，并在具备条件时优先补齐。

### M2：契约内核

**目标**：只实现第一条闭环必需的稳定 DTO、Protocol 和纯函数边界。

**交付物**：

- `contracts/`：`RequestEnvelope`、`RequestContext`、`IntentDraft`、`CapabilitySpec`、`CandidateSet`、`ExecutionPlan`、`PolicyDecision`、`ApprovalRequest`、`ToolCall`、`ToolResult`、`ExternalContent`、`EvidenceEnvelope`、`TaskOutcome`、`RenderPayload` 和结构化 error model。
- `contracts/` 另含 **`AnswerabilityVerdict`**（Reflection 的唯一输出契约）：只承载证据充分性、限制、缺失项、是否降级为 `indeterminate`、是否需用户补充信息；**不含步骤、工具、目标、权限或 SQL 字段**（边界见 `ARCHITECTURE.md` §4.2 与 §6 契约表）。
- **最小步骤级 trace / audit 事件契约**：能把一次失败定位到 Intent、Resolver、Planner、Admission、Gateway、Evidence、Reflection、Rendering、Lifecycle 中的具体阶段；只定义事件形状与阶段枚举，不实现采集后端。
- `planning/`：canonical JSON、`plan_hash`、`target_fingerprint` 的确定性实现和固定测试向量。
- `tools/`：`ToolGateway` Protocol、内部 `AdapterResponse`、私有 `ToolResult` 工厂和 fake/recording adapter。
- `capabilities/`：最小 Registry snapshot 与 Resolver Protocol；`CapabilitySpec` 承载 `effect_class` 与 operation 级 `side_effect` 声明，作为 E1 分类的唯一确定性来源。`effect_class` 进入 `plan_hash` canonicalization——已由 [ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md) D1 裁定并在 M2 实现，`PLAN_SCHEMA_VERSION` 首个取值为 `1`。此时不建立关键词总表或 DSL 框架。
- `runners/`：只声明 `WorkflowRunner` 契约和最小同步 fake runner 测试替身，不提前实现 LangGraph。
- `persistence/`：冻结 M3/M4 共用的 `TaskStore` 交互形状：每次状态变更携带 `expected_version`，结果明确返回 `applied` 与存储层 `winner`；lease 获取/续租返回 owner、到期时间和单调 fencing token，所有 lease 内写入都携带该 token。精确 DTO 和方法签名在 M2 详细计划中审定，不留到 M4 临时改 Runner。
- 单进程 fake TaskStore 必须真实执行 expected-version 检查、stale winner 返回、lease/fencing token 传播和终态保护；只是不宣称跨进程原子性、故障恢复或 PostgreSQL 级保证。

**测试门**：

- 单元测试覆盖不可变 DTO、枚举、规范化、hash 稳定性和敏感字段排除。
- 契约测试证明 Gateway 外部不能用原始 SDK 响应伪造 `ToolResult`。
- 安全测试覆盖模型字段污染、目标不稳定、未知 policy revision 和 `ExternalContent` 注入。
- fake TaskStore 契约测试覆盖 CAS 成功、CAS 失败后采用 winner、过期 lease/旧 fencing token 拒绝和终态后到事件拒绝。
- 对 hash/目标指纹承重规则做 TDD 反证，确认撤掉规范化或敏感字段排除时测试变红。
- **E1 分类承重测试**：`side_effect` 与 `effect_class` 只能由版本化 `CapabilitySpec` / operation metadata 确定性派生；模型输出、用户输入或 adapter 尝试设置、覆盖或降级这两个字段时必须拒绝；`effect_class` 未知、未声明或与 CapabilitySpec 冲突时 fail-closed，不得按只读放行。
- **Reflection 越权拒绝契约测试**：Reflection 结论中出现步骤、工具、目标、权限或 SQL 字段时必须被拒绝；契约层面不存在让 Reflection 修改 `ExecutionPlan` 或写 TaskStore 的入口。

**退出标准**：核心契约可被首个闭环消费；类型名和字段在所有边界一致；没有真实客户端、数据库或模型 SDK。

### M3：第一条 StarRocks 只读垂直闭环

**目标**：用 fake/recording 数据完成第一条从结构化意图到证据回答的真实调用链。

**推荐能力边界**：

- capability：`starrocks.slow_query.diagnose`，版本从 `1.0.0` 开始。
- 输入：已确认的环境、时间范围和可选的数据库/用户/query_id；所有范围有上限。
- 计划：由确定性 compiler 生成白名单 SQL AST；禁止接收用户或模型原始 SQL。
- 输出：慢查询事实、采样时间、数据来源、限制和可复现参数；不自动 kill query、不改参数、不给出伪确定性根因。

**交付物**：CapabilitySpec、Resolver item、PlanCompiler、只读 ToolPolicy、SQLGuard、最小 ApprovalGate Protocol/fake、DeterministicStepRunner、fake TaskStore、fake Gateway、EvidenceBuilder、只读消费 Evidence 的 Answerability（不改计划）、RenderPayload、消费 `RequestContext + IntentDraft` 的 `XiaoweiRuntime` application facade 和 L0-L2 eval corpus。另提供一个只存在于 contract/security test 的 `side_effect=True` 合成步骤；它不注册为真实 capability，也不能调用任何可触达被管运维目标的真实 adapter。

**另需交付首个完整的错误分析闭环**：运行/eval → 阅读 trace → 错误归因到具体阶段 → 选择单一根因 → 修复 → 脱敏失败样本晋升为 regression/eval case → 复测。组件级 eval 与端到端 eval 分开记录；安全、权限、SQL、审批和终态由确定性断言验收，不使用 LLM-as-judge；离线 eval 结果不表述为部署、canary 或用户验收（见 `ARCHITECTURE.md` §13.1、§13.2）。

**测试门**：

- golden、near-miss、missing-context、adversarial、timeout、malformed adapter response 和 empty evidence cases。
- SQL 多语句、注释注入、写语句、未知方言、超范围时间窗和非白名单列必须 fail-closed。
- Runtime 契约测试证明 `XiaoweiRuntime` 的调用不能跳过 Resolver、Planner、Admission 或 Gateway。
- 合成副作用步骤证明：缺少有效审批时 Runner 持久化暂停/待审批状态，ToolGateway 调用次数为 0；恢复时重新解析 actor/tenant_id/environment_id/target/current state，重算 `plan_hash` 与 `target_fingerprint`，任一不匹配都拒绝且 Gateway 调用次数仍为 0。该测试只验证控制流，不代表 M8 的 E1 能力已实现。
- **伪标拒绝测试**：把一个已注册为写的 operation 伪标为 `side_effect=False` 时，`StepAdmission` 必须拒绝，且 **`ToolGateway` 调用次数与 adapter 调用次数均为 0**；同时断言 M0-M7 的任何路径上对被管运维目标的 E1 调用次数为 0。
- **Reflection 越权拒绝测试**：Reflection 提出新增步骤、更换工具、扩大目标或把只读升级为写的建议时，该建议必须被丢弃，计划与终态不变，且 **`ToolGateway` 与 adapter 调用次数均为 0**；仅当 `ExecutionPlan` 中存在预编译的预算内只读分支时，才由 Runner 按确定性条件执行并照常经过 `StepAdmission`。
- Runner/TaskStore 契约测试证明每次 CAS 状态变更携带 `expected_version`，每次 lease 内变更携带有效 fencing token，并始终采用存储层返回的 winner。
- `python -m pytest -q` 与 `python -m pytest -m security -q` 全部通过；触及 planning/governance/tools 的 PR 必须附两条命令尾部输出。

**退出标准**：对固定 `IntentDraft` 可稳定产生同一计划、同一 fake 工具调用、可追溯证据和确定性降级结果；ApprovalGate 分支已有合成步骤反证；尚不宣称自然语言、真实 E1 能力或真实 StarRocks 已验证。

**本阶段不做**：多证据源自适应诊断、跨 capability 自动扩张计划、通用 DSL 或无界 reflection。

### M4：PostgreSQL TaskStore 与恢复

**目标**：把任务生命周期从进程内对象迁移为存储层强制的不变量。

**交付物**：

- SQLAlchemy model、Alembic migration 和 PostgreSQL `TaskStore` adapter。
- 幂等创建、合法状态迁移、CAS winner、worker lease、heartbeat、fencing token、stale recovery、终态保护和审计事件。
- 审批记录的数据契约和存储结构；本阶段不开放任何 E1 步骤。
- Runner 只使用 TaskStore 返回的 winner 和 fencing token，不用本地旧对象覆盖状态。
- M4 只替换存储实现并补强并发/恢复保证；如果 PostgreSQL adapter 迫使 Runner 改变 M2 已批准的交互形状，先回到 M2/M3 修正契约和测试，不在 adapter 内加兼容补丁。

**测试门**：

- 纯状态机单元测试、真实 PostgreSQL 集成测试、并发 worker 竞争、lease 过期、旧 fencing token、终态后到事件和崩溃恢复故障注入。
- TDD 反证至少覆盖 terminal protection、CAS 和 fencing 三个承重保护。
- migration upgrade 在空库和已有测试数据两种路径通过。

**退出标准**：重复请求只产生一个任务事实；并发执行只有存储层 winner 能提交；任何终态不可被迟到事件覆盖。

### M5：API、CLI、Worker 与 Compose

**目标**：在本地可重复环境中暴露薄入口，并将同步接入与可恢复执行分离。

**交付物**：

- FastAPI Agent Gateway：鉴权上下文 adapter、请求限制、idempotency key、trace_id 和错误映射。
- 标准库优先的最小 CLI；CLI 不复制 resolver、planner 或 renderer 逻辑。
- Worker 从 TaskStore 取得 lease 并驱动同一个 Runner。
- `docker-compose.yml`：API、Worker、PostgreSQL；API 与 Worker 共用一个应用镜像。
- health/readiness、迁移入口、结构化脱敏日志和配置校验。
- fake IntentInterpreter 与 fake/recording ToolGateway 支撑端到端测试；真实模型仍不作为核心测试依赖。

**测试门**：

- API/CLI 到 Runtime、Runtime 到 Runner、Gateway 到 adapter 的契约测试。
- Compose 启动、迁移、健康检查、任务提交、Worker 执行、进程重启恢复和重复 idempotency key 集成测试。
- 入口绕过、超大请求、伪造 tenant_id/actor、外部错误注入和日志脱敏安全测试。

**退出标准**：从干净 checkout 能按 README 启动 Compose，通过 API/CLI 完成 fake 只读闭环并在 Worker 重启后恢复；没有真实生产连接。

### M6a：第二、第三条 fake 能力

**目标**：不依赖外部许可，以两个独立垂直 PR 验证架构扩展性，并为是否需要 DSL 收集数据。

**顺序**：

1. `prometheus.alert.evidence`：只允许 capability 注册的 PromQL 模板，不接收任意 PromQL。
2. `asset.inventory.lookup`：精确 ID/hostname/IP 解析、租户/环境约束和字段白名单；不做写入或模糊跨环境选择。该能力必须专门验证 canonical target resolution、`target_fingerprint` 稳定性和环境/租户漂移拒绝。

**测试与观测门**：

- 每个能力独立包含 unit、contract、security、integration 和 L0-L2 eval。
- 每个能力记录新增/修改文件、capability 专属规则、共享核心改动、重复的 schema/planner/policy/evidence 形状、测试/eval 数量、评审返工次数，以及有可靠记录时的工时；不以单一 LOC 决定是否抽象。
- 本阶段全部使用 fake/recording；不因 DSL 已在目标架构中被允许就提前实现。是否进入 ADR-004 只依据上述数据和同类能力样本。
- `docs/CAPABILITIES.md` 由 Registry snapshot 生成并由 CI 检查漂移。

**退出标准**：三个能力的 fake 闭环各自有明确测试/eval 证据；Prometheus 和资产能力不复制入口、Runner、Gateway 或渲染真源；形成一份 DSL “继续延期/建议立项”的数据结论，但不在本里程碑实现 DSL。

### M6b：StarRocks 测试环境真实只读

**目标**：把外部依赖与内部能力扩展拆开，在明确授权的非生产环境验证第一条 StarRocks 只读 adapter。

**进入条件**：M5 已通过；项目负责人已确认环境、只读账号/secret reference、允许的查询范围、调用窗口、数据脱敏、recording 删除方式和证据保留周期。许可申请可在 M5 期间开始，但未满足条件时 M6b 保持阻塞，不反向阻塞 M6a。

**交付物**：StarRocks 测试环境 adapter，固定超时、查询预算、字段白名单、脱敏、结构化错误和 recording 回放；领域层仍不能持有真实客户端。

**测试与观测门**：

- unit、contract、security、integration 和 L0-L2 eval 先在 fake/recording 通过。
- 真实调用只允许受控人工触发，不进入 CI；记录精确 SHA、环境标识、授权范围、命令/入口、时间、脱敏结果和限制，凭证不进入任何记录。
- 超时、权限不足、响应不完整和结果不确定必须 fail-closed 或进入明确降级结果，不能伪装成成功。

**退出标准**：StarRocks 达到 `test-env verified`，并有可复核的非生产运行证据。该标签不等于生产部署、canary 或用户验收。

### M7：Web 与飞书渠道

**目标**：验证统一 `RenderPayload` 和审批/任务链接可以跨渠道投影，而不复制业务判断。

**分阶段进入条件**：详细计划中标注为「离线实现门」的工作（当前 V0.6 对应 PR 1–8）可按
第 3.1 节已批准的窄例外离线实施，但仍须先把门槛修订作为纯文档基线合入、重新读取最新文档与
Git/前序证据，并取得项目负责人明确的离线开工口令。真实应用注册、凭据读取、网络连接、部署与
canary 不适用该例外；详细计划中的真实渠道激活门未全部满足前保持阻塞。

**交付物**：先接一个实际优先级最高的渠道，稳定后再接第二个；渠道只处理协议解析、鉴权上下文、任务链接和展示适配。

**产品目标与本里程碑分离**：未来 Web 产品应具备仅 admin 可维护的数据库、Prometheus、
模型 API、飞书等配置治理，以及相应的版本、secret reference、权限、审计、测试连接、发布、
readback 与回滚。但该控制面、真实模型调用、审批/重跑和 Jenkins/Dinky 等运维能力必须另立
里程碑与 ADR；这些未来目标不是 M7 实现授权。M7 主工作台只适配桌面端，窄屏仅保证从飞书
卡片进入的单任务安全详情可读。

**测试门**：同一 Runtime outcome 在 API、CLI、Web/飞书产生语义一致的状态、证据引用和限制；渠道不能改变 capability、计划、policy 或终态。

**退出标准**：目标用户可在至少一个真实渠道完成只读提问、查看任务状态和引用证据；用户验收记录与代码/部署证据分开。

### M8：第一条受控写闭环

**目标**：只在 TaskStore、身份、渠道和 readback 已稳定后，开放一条低风险的测试环境受控 E1 能力。

**进入条件**（三项须同时满足，缺一则 M8 保持阻塞）：① 审批主体、渠道、有效期、拒绝/过期/冲突语义、policy revision 和应急关闭开关均已拍板并写 ADR-005；② 项目负责人已批准该次写能力的范围、环境和回滚方式；③ ADR-007 已记录明确的写操作例外或完成修订。M0-M7 期间 E1 全程关闭，不得通过配置项、feature flag 或默认值静默开启；内部持久化不得作为绕过 `ToolGateway` 修改运维目标的代理通道。

**交付物**：precheck、ApprovalGate pause、持久化审批、恢复重解析、`plan_hash`/`target_fingerprint` 复核、one write admission、幂等键、fencing、readback 和 `indeterminate`。

**测试门**：审批重放、actor/tenant_id/environment_id/target 漂移、policy 变化、过期/冲突审批、写超时、进程崩溃、readback 不一致和迟到响应全部做安全与故障注入测试；关键保护做 TDD 反证。

**退出标准**：测试环境验证能区分 approved、executed、readback-confirmed 和 indeterminate；没有证据时绝不返回 succeeded。生产写入需要新的单独授权和验收计划，且只有受控生产灰度才能称为 canary。

### M9：Runner 框架准入评估

**目标**：先评估，再决定是否写 `LangGraphRunner`。

**样本与指标**：至少包含多步暂停/恢复、崩溃重试/并发抢占两类生命周期样本；比较恢复正确率、重复副作用、状态漂移、代码/测试维护量、延迟和可观测性。

**决策规则**：

- 没有量化收益：继续使用 `DeterministicStepRunner`，不新增依赖。
- 有量化收益：只新增 Runner/checkpoint adapter；复用 TaskStore、StepAdmission、ApprovalGate、ToolGateway、Evidence 和 channel 契约。
- 任一 POC 需要复制候选解析、policy、审批、目标解析或终态语义：判定不准入。

**M9 不授予 Multi-Agent 权限**：本里程碑只评估 `WorkflowRunner` 的实现方式，**采用 LangGraph 不等于采用 Multi-Agent**。Multi-Agent 必须在 M9 之后另设独立里程碑和独立 ADR，进入条件至少包括真实可举证的职责拆分需求、独立的上下文/工具/记忆边界、真实生命周期样本和相对单 Runner 的量化收益；没有量化收益，或任一 POC 需要复制 `CapabilityResolver`、Policy、Approval、TaskStore、`ToolGateway` 真源时判定不准入。无论将来是否采用，Multi-Agent 都不得绕过既有安全链，也不得产生第二个状态、计划、审批或工具路由真源（见 `ARCHITECTURE.md` §12.1）。

**退出标准**：ADR-003 记录数据、结论和回滚方式；框架选择不依赖“更 Agentic”的主观判断。

## 8. 每个里程碑的执行协议

每个里程碑严格按下列顺序进行：

1. **计划**：Claude 基于最新 `main`、`ARCHITECTURE.md`、本计划和 handoff 编写该里程碑的详细实施计划，列出精确文件、接口、TDD 步骤、验证命令和非目标。
2. **审核**：项目负责人和 Codex 审核计划；涉及新契约或不可逆选择时先写 ADR。未批准不实现。
3. **分支**：从最新 `main` 创建一个或多个 `claude/<topic>` 分支；每个 PR 只承载一个可独立拒绝的交付物。
4. **TDD 实现**：先失败测试，确认失败原因，再写最小实现和边界测试。
5. **本地验收**：执行相关测试、全量测试、安全 gate、静态检查和真实 diff；记录命令、exit code 和尾部输出。
6. **Codex 审查**：按精确 commit SHA 审查真实 diff、调用链、安全绕过和测试充分性；PR 描述不能代替证据。
7. **合并与运行验证**：由授权人员合并；部署、canary 和用户验收分别记录，不能由 CI 自动推定。
8. **交接更新**：更新 `AGENT_HANDOFF.md`；能力变化时生成 `docs/CAPABILITIES.md`；只保留当前有效口径。

一个里程碑可以拆成多个 PR，但一个 PR 不跨越两个里程碑；M6a 与 M6b 也分别按独立里程碑和独立 gate 处理。发现上游契约错误时，先修上游并重新通过其 gate，不在下游增加兼容性补丁掩盖问题。

## 9. 统一验收报告格式

每次计划或代码验收均输出四段：

- **已验证**：实际执行的命令、exit code、精确 SHA、环境和结果。
- **只读推理**：基于源码/文档但没有运行证据的判断。
- **未覆盖**：未执行的测试、未连接的环境、未验证的故障路径。
- **残余风险**：即使当前 gate 通过仍存在的限制及触发升级条件。

能力状态只能使用已取得的最强证据：`declared`、`configured`、`deployed SHA`、`tests`、`canary`、`user-accepted`。禁止把计划、声明、离线测试或 PR 合并描述成线上可用。

`test-env verified` 是非生产环境运行证据标签，不等于也不替代 `canary`。`canary` 仅用于已经确认部署 SHA 的受控生产灰度；如果项目后续要把 `test-env verified` 纳入正式 readiness ladder，必须另行批准一次架构/ADR 变更，并同步更新 `ARCHITECTURE.md` 与 handoff，不能由单个能力自行改名。

## 10. 复核清单（Review Draft V2 审核时使用）

以下清单用于 V2 审核，结论已并入本文第 2、5 节与 ADR-007、ADR-008；保留原文以便追溯审核范围。

1. M0-M9（含 M6a/M6b）是否存在错误依赖顺序或无法独立验收的里程碑。
2. `StepAdmission`、ApprovalGate、ToolGateway、TaskStore 是否仍只有一套真源。
3. M2 的 `ExternalContent`、CAS winner、lease/fencing 交互形状是否足以让 M3/M4 共用，且没有写多余框架。
4. M3 是否真的形成 `IntentDraft → CapabilityResolver → PlanCompiler → WorkflowRunner → StepAdmission → ToolGateway → Readback（需要时）→ Evidence → Outcome` 的垂直闭环。
5. M4 的 CAS、lease、heartbeat、fencing、stale recovery 和 terminal protection 是否均由存储层承重。
6. M3 的合成副作用步骤是否证明 ApprovalGate 不能被绕过，同时没有提前开放真实写能力。
7. M6a 的三个候选能力能否共同验证扩展性、产出 DSL 决策数据，而不是复制三个 handler；M6b 是否与外部许可独立设 gate。
8. M8 是否完整覆盖审批绑定、恢复重解析、one write、readback 和 indeterminate。
9. M9 的 LangGraph 门槛是否可量化，且失败时可以保持“不引入”。
10. 是否存在把目标技术基线误写成当前可运行事实，或扩大未授权真实调用范围的表述。

## 11. 里程碑启动规则

**总体计划初次获批时**，只进入 M0：先检查忽略规则和敏感信息，初始化本地 Git 基线并记录 SHA，再在 `claude/m0-plan-closure` 分支完成文档/ADR 收口。

**此后每个里程碑同理**：必须先有获批的详细实施计划，才创建 `claude/<topic>` 分支并实现；
上一里程碑未验收不得进入下一里程碑。唯一已批准的例外是第 3.1 节所述、在 M7 详细计划中标注
为「离线实现门」的工作（当前 V0.6 对应 PR 1–8）；它不改变 M6b 状态，不授权真实应用注册、
凭据读取、网络调用、部署或 canary，也不降低 M7 退出标准。M2 及以后只保留路线级定义，不提前
创建超出已批准阶段门的代码或依赖。

当前处于哪个里程碑、上一里程碑是否已验收，见 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)；本文不记录进度。
