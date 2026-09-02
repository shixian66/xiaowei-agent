# 小维 Agent 2.0 当前交接

> 这是当前有效口径，不是按日期堆叠的变更流水。历史变更由 Git 提交承载；详细复盘放到 `docs/handoff/archive/`。完整的命令、exit code 和逐步输出放在里程碑验收报告中，不写入本文件。

## 1. 当前基线

| 项目 | 当前值 |
| --- | --- |
| 项目目录 | `/Users/kloenguyen/Desktop/agent` |
| 截止时间 | 2026-09-02（Asia/Shanghai） |
| 阶段 | **M0、M1、M2 已验收；M3 已实现完毕，待 Codex 深档验收** |
| 总体计划 | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) Approved V2，**已于 2026-09-01 获项目负责人批准** |
| M0 验收状态 | **已通过**，验收对象 `a1a8c888010abb8bbe1af28d792e760e3b229e5d` |
| 文档是否已入 `main` | **是**——上述验收 SHA 已以 `--ff-only` 快进合入，无合并提交，历史未改写 |
| M0 合入基线 SHA | `a1a8c888010abb8bbe1af28d792e760e3b229e5d`（与验收对象同一提交）。**`main` 的当前 HEAD 请用 `git rev-parse main` 查询——本文件不维护会随后续合并漂移的 HEAD** |
| 历史起点 SHA | `7ca391daffbfec65c8f0d1adbcd7e4180fa09178`（仅五份 Markdown 与 `.gitignore`） |
| 工作分支 | `claude/m0-plan-closure` 已合入 `main`，保留备查 |
| M1 状态 | **已验收通过**（技术审查通过 + 六个 CI gate 全绿 + 负责人批准退出标准修订） |
| 分支保护 | **未建立且当前不可建立**——private + GitHub Free，API 实证 `403`。项目负责人已于 2026-09-01 明确批准将其延后，M1 退出标准据此修订；见残余风险 |
| 远程 | `git@github.com:shixian66/xiaowei-agent.git`（**private**），默认分支 `main` |
| PR | [#1](https://github.com/shixian66/xiaowei-agent/pull/1)、[#3](https://github.com/shixian66/xiaowei-agent/pull/3)、[#4](https://github.com/shixian66/xiaowei-agent/pull/4) **均已合并** |
| M1 合入基线 SHA | `634aec016d422e7b0b474b9fb48bbd1966e5efd0`——以 `--ff-only` 快进合入，无合并提交，20 个受审 SHA 原样保留 |
| M1 合并后 CI | `main` 上 run [`33580222221`](https://github.com/shixian66/xiaowei-agent/actions/runs/33580222221)，六个 gate 全绿 |
| M1 工作分支 | `claude/m1-engineering-baseline` 已合入 `main`，保留备查 |
| M2 状态 | **已验收通过并归档**。PR #3 以 fast-forward 合入契约内核（合并对象 `527cd7fc0a85570104647d89da5694fef0bcbbca`）；PR #4 以 fast-forward 合入拒绝路径泄漏补修（最终验收对象 `319253aec7bbdda1bd4f7b661dc8938ae58ac18e`）。归档见 [docs/handoff/archive/2026-09-02-M2-contract-kernel.md](docs/handoff/archive/2026-09-02-M2-contract-kernel.md) |
| M2 详细计划 | [docs/plans/M2-contracts-kernel.md](docs/plans/M2-contracts-kernel.md) V2.3，经多轮 Codex 审核后获批开工（已带入 `main`） |
| M3 详细计划 | [docs/plans/M3-starrocks-slow-query.md](docs/plans/M3-starrocks-slow-query.md) V3，经两轮 Codex 审核批准 |
| M3 状态 | **实现完毕、未合并、未验收**。分支 `claude/m3-starrocks-slow-query`，T0-T14 共 15 个 TDD 提交。验收报告见 [docs/handoff/M3-acceptance-report.md](docs/handoff/M3-acceptance-report.md) |
| M3 能力状态 | `tests`——**非 `deployed SHA`、非 `canary`、非 `user-accepted`** |

| 本机工具链 | Python **3.11.16**（uv 独立分发）；项目依赖由 `uv.lock` 锁定，`uv sync --extra dev --frozen` 后在 `.venv` 中可原样执行 ADR-008 四条命令 |
| 运行状态 | 已有可安装、可测试、可静态检查的 Python 包；**尚未**声明 API、Worker、PostgreSQL、Docker Compose 或任何工具调用可运行 |
| 生产状态 | 未部署、未 canary、未用户验收 |
| 首个闭环 | `starrocks.slow_query.diagnose`——**已实现**，仅 fake/recording 数据，未连接真实 StarRocks |

旧项目 `ivor_aiops` 只提供历史边界和问题样本。本项目不把旧项目的分支、SHA、能力地图、线上状态或遗留待办当作自身事实。

## 2. 已确认的设计口径

已固化到 [ARCHITECTURE.md](ARCHITECTURE.md) 与 `docs/adr/`。

> **本节是快照摘要，不是规范真源。** 授权边界（真实调用、E1、写权限、租户上下文）的真源是 [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)；工程与测试工具链的真源是 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)；架构契约与安全语义的真源是 `ARCHITECTURE.md`；里程碑与验收门的真源是 `DEVELOPMENT_PLAN.md`。上述真源与本节冲突时，一律以真源为准，并回头修正本节。

- 采用模块化单体 + Docker Compose 的目标部署形态；控制面与数据面分离；PostgreSQL 作为 TaskStore、审批和审计事实真源。
- `XiaoweiRuntime` 是新的应用编排入口；不复用旧项目 `XiaoweiEngine` 的代码。
- LLM 只产结构化理解、解释和建议；不能直接选工具、目标、SQL、审批或执行。
- `CapabilityResolver` 是唯一候选生成真源；`route_shadow` 只消费 Resolver 输出，record-only。
- `ApprovalGate` 是共享组件，由 `WorkflowRunner` 在具体副作用步骤前调用；恢复时重解析并重算 `plan_hash` 与 `target_fingerprint`。
- SQL 由确定性 compiler 生成并经 `sqlglot` AST Guard；`ToolGateway` 是外部系统唯一入口。
- 外部日志、错误、知识、网页和用户粘贴文本全部按 `ExternalContent` 处理。
- **执行上下文统一命名为 `tenant_id`、`actor`、`environment_id`**；`RequestContext` 三项必填，`RequestEnvelope.environment_id` 可选；模块边界显式传递 `RequestContext`，其他 DTO 不机械复制这三项，精确字段归属由 M2 审定。
- **Phase 0 只做仓库、工具链、配置、日志/trace 和测试骨架**；业务契约（含 `ExternalContent`、`AdapterResponse`、error model、TaskStore CAS/lease/fencing 支持类型）全部属于 Phase 1。
- **验证命令单一真源**为 `python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`；禁止裸 `pytest` 调用形式。CI 通过 `setup-uv` 的 `activate-environment` 激活同一 `.venv` 后**原样执行**这四条命令。
- **CI workflow 的安全契约以整文件 SHA-256 固定**：任何 `ci.yml` 改动都会使 `tests/security/test_workflow_policy.py` 转红，必须显式更新摘要常量并接受人工审查。集合式白名单只能挡「多出的东西」，挡不住删除必需步骤、重复摘要顶替、配置移位或 `continue-on-error` 导致的 gate 失效。
- 允许受限 DSL 不等于 V1 必须实现；M0-M9 使用显式 `CapabilitySpec`，达门槛后再以 ADR-004 单独立项。
- 默认 Runner 是 `DeterministicStepRunner`；LangGraph 只能作为 adapter，先通过真实生命周期评测。
- **E1 默认关闭**：E1 指**任何可能修改被管运维目标状态的操作**，与是否经 `ToolGateway`、是否被标记 `side_effect=True` 无关；合法 E1 执行必须经 `ToolGateway` 且 `side_effect=True` 并通过 `StepAdmission`。M0-M7 全程禁止 E1（含非生产环境），M8 的受控 E1 需三项显式条件齐备，生产写另需独立授权。
- **E1 分类必须确定性派生**：`side_effect` 与 `effect_class` 只能由版本化 `CapabilitySpec` / operation metadata 派生；模型、用户输入和 adapter 都不得设置、覆盖或降级；未知分类、声明冲突、写操作误标只读一律 fail-closed。
- **Reflection 只消费结构化 Evidence，不拥有执行权**：只输出证据充分性、限制、缺失项，以及是否降级为 `indeterminate`、是否需用户补充信息的**建议**；是否真的进入 `indeterminate` 由 Runtime/Runner 依据该结构化结论确定性决定并由 TaskStore 保护，**Reflection 不设置终态、不写 TaskStore**；不得新增/修改计划步骤、选工具、扩大目标、提高权限、生成 SQL、触发 adapter 或改写 TaskStore 事实。缺槽与初始证据需求由 `CapabilityResolver` / `PlanCompiler` 处理。确需按条件追加取数时，只能是 `ExecutionPlan` 中预编译、预算内的可选只读分支，由 Runner 依确定性条件执行并照常经过 `StepAdmission`。
- **ADR-009 固化 hash 与准入形状**（M2）：`plan_hash` 规范输入集含 `effect_class`、`condition` 与 `budget`，`PLAN_SCHEMA_VERSION = 1`；`ExecutionPlan` 绑定**单一 capability**，步骤不携带 capability 标识；两个指纹**不作为 `ExecutionPlan` 字段**，绑定值存于 `ApprovalRequest`（含 `policy_revision`）；`AdmissionCertificate` 同时绑定步骤身份与 `tool_call_hash`。详见 [ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md)。
- **覆盖完备性由机制承重，不由人记得**（M2）：凡"某 DTO 全部字段必须进入某 hash"一律用显式「字段 → 指纹键」映射表实现，安全测试断言映射表键集等于 `model_fields`；含嵌套 DTO（`PlanBudget`、`StepCondition`）。给 DTO 加字段却不更新映射表会立即转红。
- **校验绕过面已封死**（M2）：`model_copy(update=...)` 与 `model_construct` 在 Pydantic v2 中完全不触发校验，均已在 `Contract` 基类封死/重新校验；未绑定的 `BaseModel.model_copy(obj, ...)` 由源码扫描禁止；不保留任何"未校验复制"的逃生口。需在校验期改写取值时一律用**字段级** `AfterValidator`——model 级 after-validator 返回非 `self` 的对象在 `__init__` 路径上会被 Pydantic 丢弃，规范化会静默失效。
- **决策权责矩阵已显式化**（`ARCHITECTURE.md` §4.3）：LLM 只在意图提取、证据解释和澄清措辞上可建议；capability/目标/参数/步骤/SQL/工具顺序由 Resolver+PlanCompiler 决定；Policy、effect 分类、审批有效性由确定性治理组件决定；工具执行由 Runner 经 StepAdmission+ToolGateway 驱动；测试环境连接授权与 E1 审批属人工授权；生产连接与生产写当前不授权；`route_shadow` record-only。该表**不授予任何新权限、不放宽 ADR-007，也不引入 `autonomy_level` 运行字段**；自治程度提升必须有 eval、失败样本、明确授权和 ADR，**不因模型或框架升级自动提高**。
- **错误分析闭环与 eval 边界**（`ARCHITECTURE.md` §13.1/§13.2）：闭环为「运行/eval → 阅读 trace → 错误归因 → 选择单一根因 → 修复 → 脱敏失败样本晋升为 regression/eval case → 复测」。职责时点：**M1 只建通用结构化日志、`trace_id` 传递与脱敏基础，不定义业务事件契约；M2 定义最小步骤级 trace/audit 事件契约；M3 建立首个完整闭环**。组件级与端到端 eval 分开记录；安全、权限、SQL、审批、终态由确定性断言验收，**LLM-as-judge 不得裁决安全正确性**；eval 与人工判断不一致时先校准 evaluator；离线 eval 不表述为部署、canary 或用户验收。
- **Multi-Agent 独立延期**（`ARCHITECTURE.md` §12.1）：M9 只评估 `WorkflowRunner` 实现，**不授予 Multi-Agent 权限**；采用 LangGraph ≠ 采用 Multi-Agent；Multi-Agent 须在 M9 之后另设独立里程碑与独立 ADR，且永远不得绕过既有安全链或产生第二个状态、计划、审批、工具路由真源。
- `test-env verified` 是非生产运行证据的旁注标签，不属于 readiness ladder，也不替代 `canary`。

## 3. 已生效的决策记录

| ADR | 主题 | 状态 |
| --- | --- | --- |
| [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) | 首批能力、初始执行上下文与真实调用许可 | Accepted 2026-09-01 |
| [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md) | 工程与测试基线：Python 3.11、pytest、security marker gate、Ruff、mypy | Accepted 2026-09-01 |
| [ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md) | `plan_hash` 规范形状、审批绑定与工具准入 | Accepted 2026-09-02 |

首批三个能力：`starrocks.slow_query.diagnose`（M3）、`prometheus.alert.evidence`（M6a）、`asset.inventory.lookup`（M6a），均先只读 fake/recording。

**外部调用许可**（ADR-007 D4）：真实运维目标系统与真实模型 API 的**网络调用**在 M0-M6a 全程禁止；本地隔离 PostgreSQL/Compose 经 M4/M5 各自里程碑批准后允许；StarRocks 非生产只读需 M6b 单独授权。CI 不持有任何测试、生产或运维目标系统凭证；仅允许 GitHub 自动签发、仓库范围、短生命周期的临时 `GITHUB_TOKEN`（`contents: read` + `persist-credentials: false`）。CI 不执行真实运维目标、真实模型、非生产只读或任何 E1 调用；对基础设施的写权限按 ADR-007 D8 的里程碑时点逐级开放。

**模型边界**：实现 provider adapter 与调用真实模型 API 是两件事。adapter 的实现和离线测试允许在其所属里程碑内进行，但**实现权不等于调用权**；M5 之后**不自动获得**真实调用权限。发起真实模型网络调用必须另设独立里程碑，并单独批准 ADR、凭证引用、数据范围和保留策略，且不得与 M0-M6a 的禁止期冲突。

**写权限**（ADR-007 D6/D7/D8）：**生产连接和生产写默认禁止**。

**E1 按后果定义，不按路径或标记定义**：E1 = **任何可能修改被管运维目标（StarRocks、Prometheus、资产系统、MySQL、Kafka、Kubernetes 等）状态的操作**。该判定与「是否经 `ToolGateway`」「是否被标记 `side_effect=True`」「是否发生磁盘写入」都无关。**绕过 `ToolGateway` 直接持有第三方客户端、或借内部持久化通道代理触达运维目标，都不因绕过而逃出 E1**，而是同时构成 E1 违规与架构违规。合法 E1 执行的唯一形式是：经 `ToolGateway`、`side_effect=True`、且已通过 `StepAdmission`。

**分类来源**：`side_effect` 与 `effect_class` 只能由版本化 `CapabilitySpec` / operation metadata 确定性派生；模型输出、用户输入、`IntentDraft`、外部文本和 adapter 都不得设置、覆盖或降级，adapter 尤其不得把已声明的写操作在运行时降级为只读。`effect_class` 未知/未声明、声明与步骤标记冲突、写操作被误标 `side_effect=False`、分类来源版本不可确定——四种情况一律 fail-closed。

**时点**：**M0-M7 全程禁止 E1，含非生产环境**；M8 才可开放一条低风险的测试环境受控 E1，且必须同时满足 ADR-005 已定稿、项目负责人已批准、ADR-007 已记录明确例外或完成修订三项，缺一即保持禁止。**生产写仍需新的独立授权和独立验收计划**，不能由 M8 的测试环境结论推导得出。

**本身不构成 E1**：TaskStore 的任务与状态迁移、approval 记录、audit 事件、evidence 索引与脱敏摘要等系统内部持久化；本地数据库 migration；测试 fixture、recording 和测试产物。但这三类**不因此自动获得基础设施许可**——使用本地隔离 PostgreSQL 或 Compose 仍须按 D4 的 C 层取得对应里程碑批准。

**CI 基础设施时点**（ADR-007 D8）：**M1-M3 的 CI 只允许写测试产物，不使用 PostgreSQL，也不使用 Compose**；M4 批准后才允许对应 CI job 使用本地隔离 PostgreSQL；M5 批准后才允许使用本地隔离 Compose。**所有阶段 CI 的 E1 调用次数恒为 0**，且 CI 不持有任何测试、生产或运维目标系统凭证（仅 GitHub 自动签发的仓库范围只读临时 token）。

**真实调用开放点按类别分别管理**，不存在「所有真实调用只能发生在 M6b」的说法：当前已批准路线中的首个真实运维目标调用是 M6b 的 StarRocks 非生产只读（仍需单独授权）；真实模型 API 调用遵守 B2 的独立里程碑；M8 的测试环境受控写遵守 E1 与 D6 三项门。

## 4. M0 收口证据

M0 的 18 个收口提交清单、五轮审查基点与差异统计已归档至
[docs/handoff/archive/2026-09-01-M0-closure.md](docs/handoff/archive/2026-09-01-M0-closure.md)。

**归档规则**：里程碑验收通过后，其逐条提交历史移入 `docs/handoff/archive/`，本文件只保留里程碑基线 SHA、当前阶段、已验证事实、阻塞项、下一步和禁止盲改点，**不记录随合并漂移的 HEAD**，也不随里程碑增长。

## 5. 下一步顺序

1. ~~M0 验收~~ **已完成**（验收对象 `a1a8c888`，已合入 `main`）。
2. ~~M1 实现与验收~~ **已完成**：技术审查通过、六个 CI gate 全绿、项目负责人批准退出标准修订，PR #1 已合入 `main`。
3. ~~M2 详细计划编写与审批~~ **已完成**：经多轮 Codex 审核（V1 → V2 → V2.1 → V2.2 → V2.3）后获批开工。
4. ~~M2 实现与验收~~ **已完成**：contracts、`ExternalContent`、`AdapterResponse`、error model、TaskStore CAS/lease/fencing 交互形状、fake ToolGateway 与 fake TaskStore 均已落地；合并后拒绝路径泄漏补修已在 PR #4 合入并通过复审。
5. ~~M3 详细计划编写与审批~~ **已完成**：V3 经两轮 Codex 审核批准。
6. ~~M3 实现~~ **已完成，待验收**：T0-T14 共 15 个 TDD 提交在 `claude/m3-starrocks-slow-query`。
7. **下一步**：Codex 按精确 SHA 做深档验收（真实 diff、调用链、安全绕过、测试充分性）。通过后由授权人员合并，并把逐条提交历史归档到 `docs/handoff/archive/`。
8. M4 实现 PostgreSQL TaskStore 的并发、恢复与终态保护；M5 完成 API/CLI/Worker/Compose。
9. M6a 完成两个 fake 能力；M6b 在单独授权下做 StarRocks 非生产真实只读验证。

## 6. 仍需拍板的事项

- 审批主体、审批渠道、审批有效期和拒绝/过期/冲突后的恢复语义（M8 前，ADR-005）。
- M8 受控 E1 的三项开放条件是否齐备：ADR-005 定稿、项目负责人批准、ADR-007 明确例外或修订。
- 生产写的独立授权与独立验收计划（不由 M8 推导）。
- 真实模型 API 网络调用所属的独立里程碑，及其 ADR、凭证引用、数据范围和保留策略授权。
- 证据、报告和大产物的存储位置及保留周期（M6b 前）。
- M4/M5 本地隔离 PostgreSQL 与 Compose 的里程碑批准。
- M6b 连接测试环境 StarRocks 真实只读的单独授权（环境、账号 secret reference、范围、时窗、脱敏、recording 删除方式）。
- 具备条件（升级套餐或改为 public）时补齐分支保护。
- 发布环境（M5 后）。

未拍板前的安全默认值：单租户开发、只读、fake adapter、无真实生产连接、无真实模型调用、无 LangGraph、无向量数据库、**任何环境均无 E1 操作**（系统内部持久化、本地 migration 和测试产物本身不构成 E1，但其基础设施许可仍受 ADR-007 D8 时点约束）。

## 7. 不要盲改

- 不要把 API、飞书或 CLI 变成第二个 Runtime。
- 不要新增关键词总表或独立候选生成器；先检查 Resolver 和 capability snapshot。
- 不要在不同 Runner、不同渠道或 shadow 中复制审批、Policy、SQLGuard、目标解析或终态语义。
- 不要为了“智能”开放模型 function calling、自由 ReAct 或模型直出 SQL/命令。
- 不要把 LangGraph checkpoint 当成 TaskStore 真源。
- 不要把外部文本中的指令、错误码或状态描述未经归类直接写进执行决策。
- 不要在文档或脚本中恢复缺少 `python -m` 前缀的测试命令形式。
- 不要把 provider adapter 的实现进度当作真实模型调用许可；不要在 M0-M7 以任何理由开启 E1；不要用 M8 的测试环境结论推导生产写许可。
- 不要把 TaskStore、approval、audit、evidence 或 migration 的内部持久化当作修改运维目标的旁路；也不要反过来把这些内部持久化本身误判为 E1 而阻塞 M4/M5。
- 不要让模型、用户输入或 adapter 参与决定 `side_effect` / `effect_class`；不要在分类未知或冲突时按只读放行。
- 不要在 M1-M3 的 CI 中引入 PostgreSQL 或 Compose；这两项要到 M4/M5 各自批准后才可用于对应 CI job。
- 不要恢复任何「取数之前先由 Reflection 决定补一个计划步骤」式的设计，也不要让 Reflection 追加或修改计划步骤；需要条件取数就在 `PlanCompiler` 里编译预算内的可选只读分支。
- 不要用 LLM-as-judge 裁决安全、权限、SQL、审批或终态的正确性；也不要为迎合指标去改业务逻辑而不校准 evaluator。
- 不要把采用 LangGraph 当作 Multi-Agent 许可；不要在 M9 内启动 Multi-Agent 工作。
- 不要宣称代码已部署、线上可用或能力已被用户接受，除非本文件有对应 SHA、命令、环境和验收证据。
- 不要给 `ExecutionPlan` / `PlanStep` / `PlanBudget` / `StepCondition` / `ResolvedTarget` / `ToolCall` 加字段却不更新 `planning` 的「字段 → 指纹键」映射表。
- 不要把 `plan_hash` / `target_fingerprint` 改成 `ExecutionPlan` 的字段；也不要给 `PlanStep` 加回 capability 标识。
- 不要在 `capabilities/effect.py` 之外直接构造 `PlanStep`；用 `build_plan_step()`。
- 不要为了让某个 adapter 跑通而放宽 `tools/gateway.py` 的 `_E1_EXECUTION_ENABLED`。
- 不要在 `model_validator(mode="after")` 里改写取值（返回值会被丢弃且只发警告）；用字段级 `AfterValidator`。也不要重新引入任何"未校验复制"的逃生口。
- 不要把 `test_domain_layer_has_no_third_party_client_import` 的扫描范围扩大到 `tools/` 或 `persistence/`，也不要为了让某模块通过而把它从领域层名单里删掉。
- 不要用**文本扫描**代替 AST 扫描来断言代码行为——docstring 里的说明文字会让断言失真。
- 不要让 count 模板与 list 模板各自演化：两者必须共用 `_scope_predicates()`。count 只回答"目标范围内有没有任何查询"，去掉目标过滤会把"拿不到该范围的审计数据"误报成"该范围没有慢查询"——一个自信但错误的结论。
- 不要把 SQLGuard 的重编译比对挪到 AST 规则之前：那会让每条攻击都得到最宽泛的 `RECOMPILE_MISMATCH`，并使规则 1-13 变成不可达的死代码。
- 不要让 Runner 写终态：终态由 Runtime 依 Answerability 的结论写且只写一次，Runner 先写会让终态保护堵死"降级为 indeterminate"这条路。
- 不要手工编辑 `docs/CAPABILITIES.md`；它由 `capabilities/doc.py` 生成并由测试检查。
- 不要在 `evidence/` 里加 `async` 或对 `contracts` 之外的内部依赖；不要让 `reflection/` / `rendering/` 够到 tools、存储或治理组件。
- 不要在被引用步骤执行失败时仍然执行可选分支：`EVIDENCE_ROW_COUNT_BELOW` 无法区分"取到零行"与"根本没取到"，而两者含义相反。
- 不要删除或覆盖用户未提交文件；不要运行破坏性命令。发现漂移或异常时停止并报告，不自动回滚。

## 8. 验证记录

### 已验证

- **M2 最终验收对象 `319253aec7bbdda1bd4f7b661dc8938ae58ac18e` 在 `main` 上四条命令全绿，GitHub CI 六项全绿（含 `secret-scan`）**：`python -m pytest -q` 640 passed / 3 warnings；`python -m pytest -m security -q` 499 passed / 141 deselected / 3 warnings；`ruff check .` 与 `mypy src` 均通过；`git diff --check d0971666..319253a` 无输出。PR #4 的 merge commit 与 head 均为 `319253a`，`main` 独立 CI run `33629416660` 为 success。更早的逐 SHA 结果见各提交信息与归档。
- **M2 的 TDD 反证逐条先转红后还原转绿**，条目见各任务提交信息；本文件不维护会随修订漂移的总数。
- **T1 是纯迁移**：M1 的 48 条脱敏/日志测试未改一行，输出与迁移前逐字相同。
- 变异测试暴露并修补了两个**测试覆盖缺口**：`verify_plan_effects` 的 `side_effect` 比对此前从未单独承重（既有伪造用例总是先被 `effect_class` 抓住）；`verify_approval_binding` 的过期/状态检查顺序此前是空断言（用例里 state 仍是 GRANTED，顺序对结果无影响）。两处均已补齐隔离用例。
- M0 验收对象 `a1a8c888…` 已以 `--ff-only` 合入 `main`，无合并提交，历史未改写。
- 远程 `git@github.com:shixian66/xiaowei-agent.git`（private），默认分支 `main`；播种前已核验远程无任何历史。
- M1 候选 SHA 见本节「M1 候选」条目；在该 SHA 上，激活 `.venv` 后原样执行 ADR-008 四条命令全部 exit 0。
- 依赖由 `uv.lock` 锁定，构建后端 `hatchling` 精确钉版并纳入锁定与 `pip-audit` 审计集。
- 日志脱敏的攻击矩阵（Basic 认证、带引号 JSON 键、mapping 作格式化参数、含空格未引号值、自定义对象 `__str__`、非 JSON 映射键、同名 logger 上的外部 handler、格式化占位符破坏）逐条复现后封堵，并固化为回归测试。
- 变异反证：移除 `redact()` 键分支、配置异常改回 `except` 块内 `from exc`、workflow 注入 `secrets[...]` 与 job 级 `write-all`，三类变异均使对应安全测试转红。
- **M3 的四条命令在分支上全绿**：`python -m pytest -q` 1213 passed；`python -m pytest -m security -q` 809 passed / 404 deselected；`ruff check .` 与 `mypy src`（74 个源文件）均通过。精确 SHA 与逐条 TDD 反证记录见 [docs/handoff/M3-acceptance-report.md](docs/handoff/M3-acceptance-report.md)。
- **M3 的每个任务都做了 TDD 反证**：撤掉承重保护确认转红、还原确认转绿，逐条写在各任务提交信息里。其中**四次反证首轮全绿，暴露了真实的覆盖缺口**并已各自补测试：`result.applied` 检查（原用例被"终态任务拿不到租约"先挡住）、`gateway.invoke` 的属性访问（原 AST 断言只扫 `ast.Call`）、sink 的常量消息（原用例绿的理由不对——detail 在契约层已被 scrub）、以及 L0 语料里 A26/A29 两条只在语料中、无驱动的纸面条目。
- **B1（count 模板必须复用目标范围）由三层共 5 条用例承重**：把 count 改成只带窗口后，T4 的集合等式 3 组、T6 的 A36、L0 的 A36 同时转红。

### 只读推理

- 五份文档的交叉冲突清单与定级（执行上下文字段名漂移、Phase 0/1 重叠、外部调用许可自相矛盾、命令口径分裂、`test-env verified` 术语缺口等）来自逐份阅读比对，无运行时证据。
- README 架构图与 `AGENTS.md`、`ARCHITECTURE.md` 的执行链顺序、`StepAdmission` 从属关系和 `ApprovalGate` 触发条件已复核一致，因此**未修改该图**，本轮未对其产生 diff。

### 未覆盖

- **分支保护未建立**，且 private + GitHub Free 下无法建立（API 实证 403）。
- 未部署、未 canary、未用户验收。
- 未连接任何外部系统：StarRocks、Prometheus、资产系统、PostgreSQL、Docker Compose、任何模型 API；**E1 调用恒为 0**。
- ~~M2 只有契约与 fake~~：M3 已落地 `CapabilityResolver`、`PlanCompiler`、`StepAdmission`、`ToolPolicy`、`SQLGuard`、`ApprovalGate`、`DeterministicStepRunner`、`EvidenceBuilder`、Reflection 与 `XiaoweiRuntime`，**全部只用 fake/recording 数据**。
- `tests/integration/`、`docker-compose.yml`、API、Worker、数据库迁移仍不存在。
- **`StepConditionKind` 四个成员 M3 只消费了两个**：`ALWAYS` 与 `EVIDENCE_ROW_COUNT_BELOW` 已被真实闭环消费；`EVIDENCE_FIELD_ABSENT` 与 `PRIOR_STEP_RESULT_IS` **未被消费、未被验证**，不要误以为四个都已验证。
- **M3 未验证真实恢复**：`resume()` 的漂移拒绝有测试，但"审批通过后恢复并真的执行副作用步骤"这条路径**永远不会在 M0-M7 走通**（E1 硬闸），因此只验证了控制流。
- 攻击矩阵中 A26/A29/A30 是链路层用例，A31-A36 是 SQL 层用例；**未覆盖**的是真实 StarRocks 的语法差异——全部 AST 结论都基于 sqlglot 30.17.0 的 starrocks 方言实现，不是真实服务端的解析结果。
- ADR-001 至 ADR-006 尚未编写。
- 错误分析闭环与 Multi-Agent 准入条件目前仍只是文档要求，其代码护栏要到 M3 之后才落地。

### 残余风险

- 本文件所在提交的 SHA 不写在文件内，由每轮验收报告提供。
- Git 初始化之前发生的所有文档修订永久没有 commit SHA 证据。
- **分支保护缺失（已知并被显式接受）**：private + GitHub Free 无法启用（API 实证 403）。项目负责人已批准延后并据此修订 M1 退出标准。后果是**红灯 PR 仍可被人工合并、可强推 `main`、可绕过 PR 流程**，合并纪律完全依赖人工。具备条件后应优先补齐。
- 日志脱敏是启发式规则：新出现的密钥形状或键名需要补规则；未加引号的敏感值脱敏到分隔符或行尾，属有意的过度脱敏。
- `pip-audit` 只能发现已收录漏洞，不能证明依赖无恶意代码。
- 部分架构约束（首条闭环是否正确消费 M2 契约、能力扩展成本）尚无运行验证，要到 M3/M6a 才可证。
- Reflection 的越权拒绝已有 M2 契约层字段闭集与安全测试；真正被首条闭环消费后的护栏要等 M3 落地。
- 「预编译的预算内可选只读分支」的形状已在 M2 给出（`StepCondition` 四成员闭集枚举，条件只能引用更早的步骤）。**残余部分**：该闭集是否覆盖 M3 实际需要的条件种类，要到 M3 才可证；不足时须改枚举并过评审，不得改成开放表达式。
- `ToolResult` 的私有性只封堵了直接构造、`model_validate`、`model_construct`、`model_copy` 四条实用路径；`object.__setattr__` 与重定义模块无法在语言层封堵，属已知残余风险，只能由评审与源码扫描覆盖。
- `InMemoryTaskStore` 的 CAS 语义只在单进程内成立（一把 `asyncio.Lock` 串行化写入）；跨进程原子性、崩溃恢复与隔离级别要到 M4 的 PostgreSQL 实现才可证。
- ~~M2 的全部安全保证尚未被真实闭环消费~~：M3 已消费它们；但下列风险是新增的。
- **审计表结构未在真实 StarRocks 核对**：表名 `starrocks_audit_db__.starrocks_audit_tbl__`、13 个列名、`state`/`errorCode` 的取值域，以及刻意排除的 `clientIp`/`digest`/`stmt` 是否存在，全部来自旧项目 `ivor_aiops` 的实证与推断。M6b 首次连接时必须用 `SHOW CREATE TABLE` 核对并回修。
- **`sqlglot` 的 AST 形状随版本可能变化**：节点闭集白名单是对**当前锁定版本 30.17.0** 的断言，升级必须重跑 `tests/unit/test_sqlglot_baseline.py` 基线。
- **`InMemoryTaskStore` / `InMemoryPlanStore` / `InMemoryEvidenceLedger` 只有单进程保证**；跨进程原子性与崩溃恢复要到 M4 才可证。
- **`WorkflowPaused` 用异常表达暂停**是对 M2 Protocol 的一种解释（已获复审裁定接受）：`TaskOutcome` 要求终态，而 `awaiting_approval` 是非终态，暂停在返回值里不可表达。若将来 Runner 需要表达更多非终态，应重新评估返回类型而不是继续加异常。
- **方向判断阈值来自旧项目，未在本项目的真实工作负载上校准**：因此结论一律带"疑似"，且只进渲染说明段、不进 `facts`。
- **`evidence/` 的纯度由一条 AST 测试承重**：删除 `tests/security/test_evidence_layer_purity.py` 即等于默默取消 `runners → evidence` 这条依赖边的正当性。
- **重编译比对在原理上无法捕获编译器自身的改动**（它用同一个编译器重算）：这正是 T4 的集合等式断言必须独立存在的理由，不要因为"已经有 A36 了"就删掉它。
- **`application` 是依赖面最宽的一层**（除 interfaces 外的全部业务包）：其正当性由 `tests/security/test_runtime_bypass.py` 的三条 AST 断言承重（不够到 Gateway、不自签凭证、只调 Runner 的 start/resume）。
