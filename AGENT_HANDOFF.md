# 小维 Agent 2.0 当前交接

> 这是当前有效口径，不是按日期堆叠的变更流水。历史变更由 Git 提交承载；详细复盘放到 `docs/handoff/archive/`。完整的命令、exit code 和逐步输出放在里程碑验收报告中，不写入本文件。

## 1. 当前基线

| 项目 | 当前值 |
| --- | --- |
| 项目目录 | `/Users/kloenguyen/Desktop/agent`；当前 M6b 隔离 worktree 为 `/Users/kloenguyen/.codex/worktrees/5f7e/agent` |
| 截止时间 | 2026-09-08（Asia/Shanghai） |
| 阶段 | **M0–M6a 已通过项目里程碑验收并归档。M6b 默认关闭的真实只读 adapter 已完成离线实现、审查并合入 `main`；真实测试环境验证延期，证据等级仍为 `tests`。M7 V0.3 已通过技术审核，项目负责人已批准只对 PR 1–3 开放离线窄例外；V0.5 已收口桌面/窄屏与未来产品边界并通过技术复核，尚待合入 `main`，M7 源码尚未开始** |
| 总体计划 | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) Approved V2.1；V2 于 2026-09-01 获批，V2.1 于 2026-09-07 仅增加 M7 PR 1–3 离线窄例外 |
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
| M3 状态 | **已验收通过并归档**。首轮 Codex 深档验收打回一条阻断项（`WorkflowRunner` 契约未闭合），按根因修复后复审通过。以 `--ff-only` 合入 `main`，**无合并提交**，19 个受审提交原样保留。归档见 [docs/handoff/archive/2026-09-03-M3-starrocks-slow-query.md](docs/handoff/archive/2026-09-03-M3-starrocks-slow-query.md)，验收报告见 [docs/handoff/M3-acceptance-report.md](docs/handoff/M3-acceptance-report.md) |
| M3 合入基线 SHA | `64d295c8e4f38028527ec9a496262c7a660258b1`（最终验收对象，与合入对象同一提交）。**`main` 的当前 HEAD 请用 `git rev-parse main` 查询** |
| M3 合并后 CI | `main` 上 run [`33708913738`](https://github.com/shixian66/xiaowei-agent/actions/runs/33708913738)，六个 gate 全绿（lint、types、secret-scan、tests、deps-audit、security-gate） |
| M3 工作分支 | `claude/m3-starrocks-slow-query` 已合入 `main`，保留备查 |
| M3 能力状态 | `tests`——**非 `deployed SHA`、非 `canary`、非 `user-accepted`** |
| M4 详细计划 | [docs/plans/M4-postgres-taskstore.md](docs/plans/M4-postgres-taskstore.md) V1.9，经 Codex 审核批准开工（V1–V1.2），实施期间随打回追加至 V1.8，归档后事实漂移修正至 V1.9 |
| M4 状态 | **已验收通过并归档**。**首轮验收打回三条阻断 + 一条非阻断，此后又被打回三轮**，共四轮；其中三轮的阻断项是本机结构性看不见的东西（首轮时 `integration` job 未曾运行、伪造的枚举成员、跑不通的事务顺序、stale 查询先 `LIMIT` 后过滤）。以 `--ff-only` 合入 `main`，**无合并提交**，21 个受审提交原样保留。归档见 [docs/handoff/archive/2026-09-04-M4-postgres-taskstore.md](docs/handoff/archive/2026-09-04-M4-postgres-taskstore.md)，验收报告见 [docs/handoff/M4-acceptance-report.md](docs/handoff/M4-acceptance-report.md) |
| M4 合入基线 SHA | `714df07e8064154d6b576376fac23f8f569ae480`（最终验收对象，与合入对象同一提交）。**`main` 的当前 HEAD 请用 `git rev-parse main` 查询** |
| M4 合并后 CI | `main` 上 run [`33842205710`](https://github.com/shixian66/xiaowei-agent/actions/runs/33842205710)，**七个** job 全绿（既有六个 + `integration`）；integration `1442 passed`、**0 skipped** |
| M4 工作分支 | `claude/m4-postgres-taskstore` 已合入 `main`，保留备查 |
| M4 能力状态 | `tests`——**非 `deployed SHA`、非 `canary`、非 `user-accepted`**。跑绿的是 CI 里一次性的 PostgreSQL service container |
| CI 第七个 job | `integration`：PostgreSQL service（`POSTGRES_HOST_AUTH_METHOD=trust`，**CI 不持有任何凭证**）+ `PYTEST_POSTGRES_DSN`，仍执行 `python -m pytest -q`。**不新增第五条命令**，ADR-008 四条不变。镜像已钉 digest（`postgres:16.10@sha256:21f6013…c3c1`）。该 job **不是** GitHub 强制的 required status check——分支保护仍不可用 |
| M5 详细计划 | [docs/plans/M5-api-worker-compose.md](docs/plans/M5-api-worker-compose.md) V5.4.1，经 Claude 最终复审批准开工 |
| M5 状态 | **已验收通过并归档**。PR [#9](https://github.com/shixian66/xiaowei-agent/pull/9) 以 fast-forward 合入，23 个受审提交原样保留；归档与验收报告见 [docs/handoff/archive/2026-09-05-M5-api-worker-compose.md](docs/handoff/archive/2026-09-05-M5-api-worker-compose.md) |
| M5 合入基线 SHA | `372c381f44ecfa1fa53961f137d0058033cbd805`（最终验收对象，与 GitHub 记录的 `mergeCommit` 相同）。**`main` 的当前 HEAD 请用 `git rev-parse main` 查询** |
| M5 合并后 CI | `main` 上 run [`33952529021`](https://github.com/shixian66/xiaowei-agent/actions/runs/33952529021)，**八个** job 全绿；integration `1866 passed`、0 skipped，`compose-smoke: passed` |
| M5 工作分支 | `claude/m5-api-worker-compose` 已合入 `main`，保留备查 |
| M5 能力状态 | `tests`——**非 `deployed SHA`、非 `canary`、非产品 `user-accepted`**。运行证据来自 GitHub 隔离 runner 的 PostgreSQL service 与 Compose smoke |
| M6a 详细计划 | [docs/plans/M6a-prometheus-asset-fake.md](docs/plans/M6a-prometheus-asset-fake.md) V1.1；首审问题回写后复审通过并获明确开工授权 |
| M6a 状态 | **已验收通过并归档**。PR #11（Prometheus + binding）、#12/#13（Evidence target seam）、#15（CI 确定性）与 #14（资产闭环）均已合入；完整链路见 [M6a 归档](docs/handoff/archive/2026-09-06-M6a-prometheus-asset-fake.md) 与 [里程碑验收报告](docs/handoff/M6a-acceptance-report.md) |
| M6a 最终实现基线 | `e2032fdff958d2309973458d5c762a54b3a4f855`；PR #14 的 `mergeCommit` 与该 SHA 相同，无合并提交 |
| M6a 合并后 CI | main push run [`33976421909`](https://github.com/shixian66/xiaowei-agent/actions/runs/33976421909) 精确绑定最终实现基线，八个 job 全绿 |
| M6b 详细计划 | [docs/plans/M6b-starrocks-test-readonly.md](docs/plans/M6b-starrocks-test-readonly.md) V1.1；2026-09-07 经复审后获负责人批准离线开发 |
| M6b 实现与合入 | 开发基线 `e8128c8c364e1e5ba560c916044dfae0409dd490`；PR [#17](https://github.com/shixian66/xiaowei-agent/pull/17) 的受审 head 为 `0162888ebe4fe415aabfa3318a02a0b26459501c`，以 squash commit `a5b60baa25eda7ec964b2b48f13f051bf926e3c4` 合入 `main`；合入后 CI run [`34078690572`](https://github.com/shixian66/xiaowei-agent/actions/runs/34078690572) 八项全绿 |
| M7 计划与阶段门 | [docs/plans/M7-web-feishu-channels.md](docs/plans/M7-web-feishu-channels.md) V0.3 已通过 Claude 技术审核；项目负责人于 2026-09-07 明确批准 B：只开放 PR 1–3 离线实现，PR 4–8 继续由 M6 真实只读 canary 与真实飞书授权阻塞。V0.5 在 V0.4 阶段门之上追加已确认的产品范围收口，基于 `main@aa2af2edcd4c30c611e0ed51d10263b26acef598`，已通过技术复核，尚未合入 `main` |
| 下一步 | **先以纯文档 PR 合入 M7 V0.5 基线；未在合入后收到新的“开始 M7 离线实现”前不进入 PR 1–3。** M6b 继续暂停在真实验证前；PR 4–8 不得借离线窄例外提前启动。以后恢复 M6b 时仍须核对最新 `main` 与授权，补齐唯一 canonical target、物理 identity、带外 digest、secret reference、actor/窗口、证据处置，并取得新的明确“现场 GO” |
| 本机工具链 | Python **3.11.16**（uv 独立分发）；项目依赖由 `uv.lock` 锁定，`uv sync --extra dev --frozen` 后在 `.venv` 中可原样执行 ADR-008 四条命令 |
| 运行状态 | M5 的 API、CLI、Worker、migration、同镜像 Compose、三能力 fake local stack 与 M6b 默认关闭的 target-bound StarRocks adapter 均已合入 `main`；真实激活保持 fail-closed。仍未连接任何真实运维目标 |
| 生产状态 | 未部署、未 canary、未用户验收 |
| 能力闭环 | `starrocks.slow_query.diagnose`、`prometheus.alert.evidence`、`asset.inventory.lookup` 均已完成 fake/recording 闭环，证据等级均为 `tests`；三者均未连接对应真实运维系统 |

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
- **生产 policy snapshot 必须整体版本化**：M6a PR 1 新增 Prometheus 只读 profile 后，production revision 从 `policy-2026-09-01` 递增为 `policy-2026-09-05`；PR 2 新增资产只读 profile 后再次递增为 `policy-2026-09-05.2`。每次都与有序 profile ID 集合做成对 golden；测试 fake 的独立 revision 不随生产值机械迁移。
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
| [ADR-010](docs/adr/ADR-010-m5-durable-attempt-and-compose-boundary.md) | M5 持久执行尝试、事务审计、readiness 与本地 Compose 边界 | Accepted 2026-09-05 |
| [ADR-011](docs/adr/ADR-011-m6a-capability-binding-and-promql-template-admission.md) | 多能力 binding、operation gateway 与固定 PromQL 模板准入 | Accepted 2026-09-05 |
| [ADR-012](docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md) | M6b 精确目标绑定、固定 preflight 与测试环境只读激活边界 | Accepted 2026-09-07 |

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

## 4. 里程碑归档索引

M0 的 18 个收口提交清单、五轮审查基点与差异统计已归档至
[docs/handoff/archive/2026-09-01-M0-closure.md](docs/handoff/archive/2026-09-01-M0-closure.md)。

M2 的 26 个受审提交与合并事实已归档至
[docs/handoff/archive/2026-09-02-M2-contract-kernel.md](docs/handoff/archive/2026-09-02-M2-contract-kernel.md)。

M3 的 19 个受审提交、首轮打回的根因与修复、以及四段验收事实已归档至
[docs/handoff/archive/2026-09-03-M3-starrocks-slow-query.md](docs/handoff/archive/2026-09-03-M3-starrocks-slow-query.md)。

M4 的 21 个受审提交、四轮打回与 PostgreSQL 运行证据已归档至
[docs/handoff/archive/2026-09-04-M4-postgres-taskstore.md](docs/handoff/archive/2026-09-04-M4-postgres-taskstore.md)。

M5 的 23 个受审提交、复审补修、PostgreSQL integration 与 Compose smoke 证据已归档至
[docs/handoff/archive/2026-09-05-M5-api-worker-compose.md](docs/handoff/archive/2026-09-05-M5-api-worker-compose.md)。

M6a 的 Prometheus/资产两个 fake 闭环、两条上游修复链、扩展数据与四段验收事实已归档至
[docs/handoff/archive/2026-09-06-M6a-prometheus-asset-fake.md](docs/handoff/archive/2026-09-06-M6a-prometheus-asset-fake.md)。

**归档规则**：里程碑验收通过后，其逐条提交历史移入 `docs/handoff/archive/`，本文件只保留里程碑基线 SHA、当前阶段、已验证事实、阻塞项、下一步和禁止盲改点，**不记录随合并漂移的 HEAD**，也不随里程碑增长。

## 5. 下一步顺序

1. ~~M0 验收~~ **已完成**（验收对象 `a1a8c888`，已合入 `main`）。
2. ~~M1 实现与验收~~ **已完成**：技术审查通过、六个 CI gate 全绿、项目负责人批准退出标准修订，PR #1 已合入 `main`。
3. ~~M2 详细计划编写与审批~~ **已完成**：经多轮 Codex 审核（V1 → V2 → V2.1 → V2.2 → V2.3）后获批开工。
4. ~~M2 实现与验收~~ **已完成**：contracts、`ExternalContent`、`AdapterResponse`、error model、TaskStore CAS/lease/fencing 交互形状、fake ToolGateway 与 fake TaskStore 均已落地；合并后拒绝路径泄漏补修已在 PR #4 合入并通过复审。
5. ~~M3 详细计划编写与审批~~ **已完成**：V3 经两轮 Codex 审核批准。
6. ~~M3 实现与验收~~ **已完成**：首轮 Codex 深档验收打回一条阻断项（`WorkflowRunner` 契约未闭合），按根因修复后复审通过；以 `--ff-only` 合入 `main`（`64d295c`），CI run `33708913738` 六项全绿，逐条提交历史已归档。
7. ~~M4 详细计划编写与审批~~ **已完成**：V1–V1.2 经 Codex 审核批准开工。
8. ~~M4 实现与验收~~ **已完成**：四轮打回后以 `--ff-only` 合入 `main`（`714df07`），CI run `33842205710` 七项全绿，逐条提交历史已归档。
9. ~~M5 实现与验收~~ **已完成**：最终对象 `372c381` 经终审与项目负责人验收，以 fast-forward 合入 `main`；合并后 run `33952529021` 八项全绿，逐条提交历史已归档。
10. ~~M6a 实现与验收~~ **已完成**：PR #11、#12、#13、#15、#14 均已合入；最终实现基线 `e2032fd` 的 main push CI 八项全绿，里程碑已获负责人验收并归档。
11. **M6b 真实验证前暂停**：默认关闭的真实 adapter 已经 PR #17 审查并合入 `main`，合入后八项 CI 全绿；项目负责人明确将测试环境验证延期。恢复时必须从最新 `main` 重新核对授权和配置，唯一目标、物理身份、带外 version/grants/DDL/identity digest、secret reference、actor/窗口、证据处置和新的“现场 GO”未全部落定前不连接真实服务。M6b 仍未验收、未归档。
12. **M7 只开放 PR 1–3 离线窄例外**：V0.3 技术审核已通过，项目负责人于 2026-09-07
    明确批准把入口门拆为离线门和真实渠道门。V0.5 文档已通过技术复核；该文档合入并再次
    获得明确“开始 M7 离线实现”前，不创建 PR 1 实现分支。PR 4–8 仍等待 M6 真实只读 canary、
    真实飞书应用授权与新鲜 SDK 核验，不因 PR 1–3 完成自动解锁。

## 6. 仍需拍板的事项

M7 的阶段门拆分已拍板，不再是待决项：只允许 PR 1–3 离线实现，且须在 V0.5 文档合入后
再次取得明确开工口令；PR 4–8 的真实调用权限没有放宽。

M7 的产品范围也已拍板：主工作台只适配桌面端；窄屏仅保证飞书链接单任务安全详情可读。
未来的 Admin 配置治理、真实模型 API、审批/重跑和后续运维能力须另立里程碑与 ADR，不属于 M7，
这次范围确认也不能被推导为已实现或已连接。

- 审批主体、审批渠道、审批有效期和拒绝/过期/冲突后的恢复语义（M8 前，ADR-005）。
- M8 受控 E1 的三项开放条件是否齐备：ADR-005 定稿、项目负责人批准、ADR-007 明确例外或修订。
- 生产写的独立授权与独立验收计划（不由 M8 推导）。
- 真实模型 API 网络调用所属的独立里程碑，及其 ADR、凭证引用、数据范围和保留策略授权。
- M6b 现场验证已由项目负责人明确延期。以后恢复时仍需拍板唯一 canonical target、物理 identity 探针、带外 version/grants/DDL/identity digest、账号 secret reference、actor/时窗、允许字段、临时 Evidence/recording 销毁方式和脱敏证据保留周期。
- M6b 恢复真实验证前的单独“现场 GO”；既有离线开发批准与合入事实均不等于真实网络调用许可。
- 具备条件（升级套餐或改为 public）时补齐分支保护。
- 发布环境（M5 后）。

未拍板前的安全默认值：单租户开发、只读、fake adapter、无真实生产连接、无真实模型调用、无 LangGraph、无向量数据库、**任何环境均无 E1 操作**（系统内部持久化、本地 migration 和测试产物本身不构成 E1，但其基础设施许可仍受 ADR-007 D8 时点约束）。

## 7. 不要盲改

- **不要把「CI 上的 PostgreSQL service container 跑绿」当成生产就绪**：它不能推出任何关于生产数据库、真实负载、连接池或运维环境的结论。M4 的能力状态仍是 `tests`。
- **不要因为 integration 现在全绿就放松那两条 gate**：0 skip 由 `tests/integration/conftest.py::pytest_sessionfinish` 承重，触发条件（`DSN_ENV_VAR` 与 `ci.yml` env 键两侧同名）由 `tests/contract/test_integration_gate.py` 钉死。删掉任一条，下一次「全绿」就可能是零证据的全绿——run `33828598775` 之前正是这个状态。
- **不要把 `list_stale_leases` 的 SQL 谓词改宽**：有 `LIMIT` 时它必须与 `is_stale_lease` 逐条等价，否则终态任务会占满窗口、返回量随运行单调衰减到零。理由见 `postgres.stale_lease_statement` 的 docstring。
- **不要在终态迁移时清空租约字段**：`fencing_token IS NOT NULL` 是「曾被租出」的判据，清空它会重开 fencing 缺口。终态由 `is_stale_lease` 单独挡。

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
- **不要把 `WorkflowRunner` 改回 `start(task_id)` 的窄签名。** 窄签名与 Runner 自身的漂移检测职责不相容：`target` 与 `policy_revision` 的语义是"现在的值"，从存储读会让检查恒真。唯一能让窄签名成立的写法，是调用方绕过 Protocol 直接调具体类——那正是首轮深档验收打回的阻断项。理由见 ARCHITECTURE §5.6 与 `runners/runner.py` 的 docstring。
- **不要用 `type: ignore` 或 `getattr(obj, "attr", default)` 处理模块边界上的类型不匹配。** 两者都会把"契约与实现矛盾"静音成全绿，由 `tests/security/test_contract_edges.py` 承重。类型对不上时改契约或改实现，不要改静音手段。

## 8. 验证记录

### 已验证

- **M6b 离线实现已合入 `main`**：PR [#17](https://github.com/shixian66/xiaowei-agent/pull/17) 的 head 为 `0162888ebe4fe415aabfa3318a02a0b26459501c`，以 squash commit `a5b60baa25eda7ec964b2b48f13f051bf926e3c4` 合入；PR 与合入后 main push CI run [`34078690572`](https://github.com/shixian66/xiaowei-agent/actions/runs/34078690572) 的 tests、integration、compose-smoke、security-gate、lint、types、deps-audit、secret-scan 八项均成功。该证据只把状态推进到 `tests`，不构成 `test-env verified`。
- **M6b 独立审查修复后的离线工作树四门全绿**：`python -m pytest -q` 为 2330 passed / 154 skipped / 5 warnings；`python -m pytest -m security -q` 为 1085 passed / 79 skipped / 1320 deselected / 5 warnings；Ruff 通过；mypy 134 个源文件通过；`git diff --check` 无输出。154 个 skip 沿用无 `PYTEST_POSTGRES_DSN` 的本机口径，未提供新的 PostgreSQL/Compose 或真实 StarRocks 运行证据。修复覆盖 version digest、preflight 顺序/列契约/count 形状、driver timeout 内外层约束，以及 generic Gateway 失败与 target-bound metadata 的 Evidence 归因。
- **M6b 六组隔离变异反证均按预期转红**：分别拆掉 Gateway exact fingerprint、ToolCall 连接参数污染拒绝、list 行数上限、identity digest、DDL digest 与异常路径 connection close；对应测试均非零退出。另以先红后绿补严 physical identity AST：错列、无 database、`WHERE SLEEP(...)` 与 `ORDER BY` 不再能进入受审探针闭集。所有变体只位于 `/private/tmp/m6b-mutation.I2tKU9`，未进入工作树。
- **M6b 独立审查修复新增六组隔离变异反证**：分别移除 version digest 比对、恢复“任意 limitation 都是 Evidence 错位”、禁用跨能力保留 metadata 拒绝、恢复空 count/宽松列契约、放宽 driver timeout、把 `SET query_timeout` 移到 version 之后；对应测试分别以 1、2、2、3、3、1 条失败转红。变体确认从 `/private/tmp/m6b-review-mutation.Bujb3l/*/src` 加载，未进入工作树。
- **M6b 合并前加固新增两组隔离变异反证**：单独把 Runner 默认 ToolCall 预算从 30 秒降为 10 秒时，跨层 timeout pairing 安全测试 1 条转红；删除 slow-query generic Evidence 分支的 target metadata 拒绝调用时，对称用例 1 条转红。变体确认从 `/private/tmp/m6b-pairing-mutation.84tLwt/*/src` 加载，未进入工作树；生产代码零改动。
- **M6a 已验收、合入并归档**：最终实现基线 `e2032fdff958d2309973458d5c762a54b3a4f855` 的本机四门为 `python -m pytest -q` 2169 passed / 154 skipped / 5 warnings、`python -m pytest -m security -q` 1012 passed / 79 skipped / 1232 deselected / 5 warnings、Ruff 通过、mypy 133 个源文件通过。项目负责人于 2026-09-06 明确授权归档；这是项目里程碑验收，不是产品用户验收。
- **M6a 最终远程运行证据已闭合**：PR #14 run [`33975532006`](https://github.com/shixian66/xiaowei-agent/actions/runs/33975532006) 与合入后 main push run [`33976421909`](https://github.com/shixian66/xiaowei-agent/actions/runs/33976421909) 均精确绑定 `e2032fd` 且八个 job 全绿。integration 与三能力 Compose smoke 均通过；不包含真实资产系统/运维目标，也不是部署、canary 或产品用户验收。
- **M6a PR 2 未修改执行核心**：相对 PR 2 基线 `main@9d9380c`，Runtime、Runner、Gateway、StepAdmission、持久化、跨边界 contracts 与 canonical hash 共 0 文件改动；五个注册/装配真源加生成能力地图为 6 文件、`+183/-17`。这证明 seam 复用成功，但不隐藏注册成本；DSL 依据数据继续延期，详见 [扩展数据](docs/handoff/M6a-capability-extension-data.md)。
- **PR #15 已关闭 PR #14 的随机自检阻断**：复审建议的 40 位 Base64 高熵构造在最终候选 `9d9380c` 上通过本地四门、PR 八项 CI 与合入后 main 八项 CI；失败步骤实际使用 `generic-api-key` 的 10–150 位捕获规则，因此没有把“必须正好 40 位”误记为已证明根因。
- **M6a Evidence target seam 已独立合入并取得本地与远程验证**：PR #12 新增 start/resume 真实 Runner 用例证明 `StepEvidenceBuilder` 收到已准入/已复核 `ResolvedTarget`；准入与安全用例证明 target/context 环境不一致以 `policy.environment_mismatch` 在 Gateway/Evidence 前拒绝。两项保护均做隔离变异反证：撤掉 target 传递时 2 条 Runner 用例转红，撤掉环境一致性检查时契约与安全用例各 1 条转红；还原后关键 4 条全绿。本地全量为 1991 passed / 153 skipped / 5 warnings，security gate 为 980 passed / 79 skipped / 1085 deselected / 5 warnings，Ruff 与 mypy（124 个源文件）通过；PR run `33970097354` 与合入后 main run `33970187056` 均八个 job 全绿。以上仍是 fake/recording、隔离 PostgreSQL 与 Compose 证据，不是部署或真实运维系统验证。
- **M6a PR 1 审查修复后四条规范门全部 exit 0**：`python -m pytest -q` 为 1986 passed / 153 skipped / 5 warnings；`python -m pytest -m security -q` 为 979 passed / 79 skipped / 1081 deselected / 5 warnings；`ruff check .` 通过；`mypy src` 为 124 个源文件通过。skip 均保留原语义，其中 M6a PostgreSQL integration 因无 DSN skip。
- **首轮独立审查的必修项已做 TDD 根因修复**：production policy revision/profile 成对 golden 在旧 revision 上先红、递增后转绿；向 `application/worker.py` 临时注入 capability import 后，新逐文件依赖护栏真实转红，移除变体后恢复；60 分钟非默认窗口端到端到达 Prometheus adapter 后以 `INDETERMINATE` 收口，证明本地 recording 无 fallback。变体未保留。
- **M6a PR #11 首轮远程 CI 已取得非生产运行证据**：run [`33966437003`](https://github.com/shixian66/xiaowei-agent/actions/runs/33966437003) 的 head SHA 为 `cfd63923a35be1e5dcfe13b3dbd78c00e0bca520`，八个 job 全部 success；PostgreSQL integration 实跑 `2139 passed`、0 skipped，Compose 实跑 `python -m scripts.compose_smoke` 并输出 `compose-smoke: passed`。这不是部署、canary 或真实 Prometheus/Alertmanager 兼容证据。
- **M6a PR 1 做了 9 个隔离变异反证且对应用例均先转红**：分别拆掉 PromQL 重编译、operation gateway 派生、无记录结果 fail-closed、snapshot ID 联合 golden、恢复期 plan hash、Evidence 字段白名单、binding 精确查找、入口中立性，以及擅自递增 StarRocks version。变体位于临时 detached worktree、使用独立 pycache，已删除且未进入候选分支。
- **M5 最终对象 `372c381f44ecfa1fa53961f137d0058033cbd805` 已验收、快进合入并归档**：合并后 `main` run [`33952529021`](https://github.com/shixian66/xiaowei-agent/actions/runs/33952529021) 八个 job 全绿；integration `1866 passed`、0 skipped，`compose-smoke: passed`。本机四门为 1714 passed / 152 skipped、security 923 passed / 79 skipped / 864 deselected、Ruff 通过、mypy 102 个源文件通过。详细命令、变异反证和风险见 [M5 归档](docs/handoff/archive/2026-09-05-M5-api-worker-compose.md)。
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
- **M3 最终验收对象 `64d295c8e4f38028527ec9a496262c7a660258b1` 在 `main` 上四条命令全绿，GitHub CI 六项全绿**：`python -m pytest -q` 1229 passed / 3 warnings；`python -m pytest -m security -q` 823 passed / 406 deselected / 3 warnings；`ruff check .` 与 `mypy src`（74 个源文件）均通过；`git diff --check 4e3e309..64d295c` 无输出；`main` 独立 CI run `33708913738` 为 success。对照 M2 的 640 / 499：全量 +589，安全 gate +324。逐条 TDD 反证记录见各提交信息与 [docs/handoff/archive/2026-09-03-M3-starrocks-slow-query.md](docs/handoff/archive/2026-09-03-M3-starrocks-slow-query.md)。
- **M3 的每个任务都做了 TDD 反证**：撤掉承重保护确认转红、还原确认转绿，逐条写在各任务提交信息里。其中**四次反证首轮全绿，暴露了真实的覆盖缺口**并已各自补测试：`result.applied` 检查（原用例被"终态任务拿不到租约"先挡住）、`gateway.invoke` 的属性访问（原 AST 断言只扫 `ast.Call`）、sink 的常量消息（原用例绿的理由不对——detail 在契约层已被 scrub）、以及 L0 语料里 A26/A29 两条只在语料中、无驱动的纸面条目。
- **B1（count 模板必须复用目标范围）由三层共 5 条用例承重**：把 count 改成只带窗口后，T4 的集合等式 3 组、T6 的 A36、L0 的 A36 同时转红。

### 只读推理

- 五份文档的交叉冲突清单与定级（执行上下文字段名漂移、Phase 0/1 重叠、外部调用许可自相矛盾、命令口径分裂、`test-env verified` 术语缺口等）来自逐份阅读比对，无运行时证据。
- README 架构图保持既有执行顺序，只把 `SQLGuard` 标签扩为同时涵盖 SQL/PromQL 的 `QueryGuard`；没有改变 `StepAdmission` 从属关系或 `ApprovalGate` 触发条件。

### 未覆盖

- **M7 仍无源码、测试、Compose、部署、canary 或用户验收证据**：当前只有 V0.3 技术审核结论、
  负责人对阶段门拆分和产品范围的批准，以及已通过技术复核但尚未合入的 V0.5 文档修订。未来的 Admin 配置治理和
  真实模型 API 尚无源码或运行证据。PR 1–3 尚未获得开工口令；PR 4–8 仍被
  真实渠道门阻塞。
- **分支保护未建立**，且 private + GitHub Free 下无法建立（API 实证 403）。
- 未部署、未 canary、未取得产品用户验收；M5/M6a 的“验收通过”都是项目里程碑验收，不改变 readiness ladder。
- 未连接任何真实运维目标或模型服务：StarRocks、Prometheus、资产系统与任何模型 API 均未连接；M5 只使用 GitHub runner 上一次性的隔离 PostgreSQL/Compose，**E1 调用恒为 0**。
- ~~M2 只有契约与 fake~~：M3 已落地 `CapabilityResolver`、`PlanCompiler`、`StepAdmission`、`ToolPolicy`、`SQLGuard`、`ApprovalGate`、`DeterministicStepRunner`、`EvidenceBuilder`、Reflection 与 `XiaoweiRuntime`，**全部只用 fake/recording 数据**。
- 当前开发机未提供 `PYTEST_POSTGRES_DSN` 且没有 Docker，因此本机资产 PostgreSQL integration 是受控 skip、Compose 未实跑；最终 PR #14 run `33975532006` 与 main run `33976421909` 已在 GitHub 隔离 runner 补证。单次 CI 仍不能外推到其他 PostgreSQL/Docker/Compose 版本或长期运行。
- 主 `main` checkout 的旧 M6a 同路径计划稿已在 PR 1 合入前移到临时目录备份；当前仍保留两份与本任务无关的用户未跟踪文档 `docs/plans/development-route-v3-proposal.md` 与 `docs/plans/legacy-capability-migration-matrix.md`，本任务不读取、不修改、不暂存。
- **`StepConditionKind` 四个成员已有三个被消费**：`ALWAYS`、`EVIDENCE_ROW_COUNT_BELOW` 与 `PRIOR_STEP_RESULT_IS`。后者由 Prom 两步计划消费，并只读取持久化 step journal 的 committed `OK`；`FAILED`/`TIMEOUT`/无记录均不运行后续步骤。`EVIDENCE_FIELD_ABSENT` 仍未消费、未验证。
- **M3 未验证真实恢复**：`resume()` 的漂移拒绝有测试，但"审批通过后恢复并真的执行副作用步骤"这条路径**永远不会在 M0-M7 走通**（E1 硬闸），因此只验证了控制流。
- 攻击矩阵中 A26/A29/A30 是链路层用例，A31-A36 是 SQL 层用例；**未覆盖**的是真实 StarRocks 的语法差异——全部 AST 结论都基于 sqlglot 30.17.0 的 starrocks 方言实现，不是真实服务端的解析结果。
- ADR-001 至 ADR-006 尚未编写。
- Multi-Agent 准入条件仍只有文档约束；M9 也只评估 Runner，不授予 Multi-Agent 权限。
- M6b 已完成离线实现、审查、合入和合入后 CI，但项目负责人已将真实测试环境验证延期；尚无测试环境 StarRocks 的真实连接、凭证引用 readback、
  physical identity、DDL/grants 或数据处置证据，因此不能标记 `test-env verified`。

### 残余风险

- M5 的 `up --wait`、one-shot migration、healthcheck argv、`--force-recreate --no-deps` 重启语义及容器 secret 引用已在 GitHub 隔离 runner 的一次完整 smoke 中通过；单次 CI 不能证明跨 Docker/Compose 版本兼容、长期稳定性、高可用或生产安全性。
- M5 的事务结果未知分类与 fencing/接管已有真实 PostgreSQL integration，进程 SIGKILL 与恢复已有 Compose smoke；但数据库进程在提交确认边界精确断连、长时间锁竞争和资源耗尽仍主要由故障注入/契约测试承重，未做持续压力或 chaos 验证。
- 本文件所在提交的 SHA 不写在文件内，由每轮验收报告提供。
- Git 初始化之前发生的所有文档修订永久没有 commit SHA 证据。
- **分支保护缺失（已知并被显式接受）**：private + GitHub Free 无法启用（API 实证 403）。项目负责人已批准延后并据此修订 M1 退出标准。后果是**红灯 PR 仍可被人工合并、可强推 `main`、可绕过 PR 流程**，合并纪律完全依赖人工。具备条件后应优先补齐。
- 日志脱敏是启发式规则：新出现的密钥形状或键名需要补规则；未加引号的敏感值脱敏到分隔符或行尾，属有意的过度脱敏。
- `pip-audit` 只能发现已收录漏洞，不能证明依赖无恶意代码。
- PR 1/PR 2 的扩展成本已记录在 [M6a capability 扩展数据](docs/handoff/M6a-capability-extension-data.md)：执行 seam 可复用，但每个能力仍显式编辑五个注册/装配真源。三个能力语义异质，没有三个同类能力证明 DSL 收益，因此 ADR-004 继续延期；未来出现足够同类样本时再单独立项。
- Reflection 的越权拒绝已由 StarRocks 与 Prometheus 两条 fake 闭环消费；这仍不能推出未来真实 adapter 的外部文本都符合当前证据契约。
- 「预编译的预算内可选只读分支」已由 Prometheus 两步计划消费 `PRIOR_STEP_RESULT_IS`，并覆盖首次执行与重启后的 OK/FAILED/TIMEOUT/无记录矩阵。该闭集对未来能力是否足够仍须逐能力验证；不足时须改枚举并过评审，不得改成开放表达式。
- `ToolResult` 的私有性只封堵了直接构造、`model_validate`、`model_construct`、`model_copy` 四条实用路径；`object.__setattr__` 与重定义模块无法在语言层封堵，属已知残余风险，只能由评审与源码扫描覆盖。
- ~~M2 的全部安全保证尚未被真实闭环消费~~：M3 已消费它们；但下列风险是新增的。
- **审计表结构未在真实 StarRocks 核对**：表名 `starrocks_audit_db__.starrocks_audit_tbl__`、13 个列名、`state`/`errorCode` 的取值域，以及刻意排除的 `clientIp`/`digest`/`stmt` 是否存在，全部来自旧项目 `ivor_aiops` 的实证与推断。M6b 首次连接时必须用 `SHOW CREATE TABLE` 核对并回修。
- **M6b 真实激活仍有两道故意保留的硬闸**：`test` 目录仍含两个占位资源，API/Worker 也不提供获批的 `StarRocksLiveAssembly`。负责人给出唯一 target 与物理 identity 后必须用独立小补丁补齐并复审，不能由环境变量选择占位目标。
- **`test` 目录歧义会在 adapter 选择前拒绝整个目标解析**：因此唯一 target 未批准前，该环境的 generic recording 与 target-bound 真实装配都不可用；`dev` recording 仍可用。这是 selector v2 的 fail-closed 迁移语义，不是网络激活或 recording 回归。
- **真实结果会进入 append-only EvidenceLedger**：Render 只显示三列不等于持久化只保存三列。M6b 现场只能使用获批的独立临时 Compose project，并按精确 project/volume 销毁；未批准字段与处置前不得激活。
- **PyMySQL 的真实缓冲、超时后服务端残留与取消行为尚未实测**：M6b 小结果仍硬限制为 200 行且不自动重试、不发 KILL；这不能外推为后续通用 SQL 的分页、流式或取消能力。
- **M6b 仍有四项低风险 hardening 未实现**：`config_revision` 尚未覆盖 physical identity probe 的 SQL/列名；credential file 只校验非 symlink 普通文件、大小与单行格式，未校验 owner/mode；multi-statements 依赖 PyMySQL 默认关闭而没有独立参数/readback 断言；数值列若由 driver 返回 `Decimal` 会规范化成字符串，而 int/float 保持数值类型。它们不构成本轮合并阻断，但在真实激活或扩大结果契约前必须重新评估。
- **preflight 精确列标签仍是待现场核对的 fail-closed 假设**：`("VERSION()",)`、`("Table", "Create Table")`、`("Variable_name", "Value")` 尚未在批准的真实 StarRocks 版本实测；首次连接若拒绝，应按版本事实回修并重新审核，不能临时放宽列契约。
- **`sqlglot` 的 AST 形状随版本可能变化**：节点闭集白名单是对**当前锁定版本 30.17.0** 的断言，升级必须重跑 `tests/unit/test_sqlglot_baseline.py` 基线。
- **`WorkflowPaused` 用异常表达暂停**是对 M2 Protocol 的一种解释（已获复审裁定接受）：`TaskOutcome` 要求终态，而 `awaiting_approval` 是非终态，暂停在返回值里不可表达。若将来 Runner 需要表达更多非终态，应重新评估返回类型而不是继续加异常。
- **方向判断阈值来自旧项目，未在本项目的真实工作负载上校准**：因此结论一律带"疑似"，且只进渲染说明段、不进 `facts`。
- **`evidence/` 的纯度由一条 AST 测试承重**：删除 `tests/security/test_evidence_layer_purity.py` 即等于默默取消 `runners → evidence` 这条依赖边的正当性。
- **重编译比对在原理上无法捕获编译器自身的改动**（它用同一个编译器重算）：这正是 T4 的集合等式断言必须独立存在的理由，不要因为"已经有 A36 了"就删掉它。
- **`application` 是依赖面最宽的一层**（除 interfaces 外的全部业务包）：其正当性由 `tests/security/test_runtime_bypass.py` 的三条 AST 断言承重（不够到 Gateway、不自签凭证、只调 Runner 的 start/resume）。
- **PromQL 不是通用解析器验证**：M6a 只允许两个代码内固定模板并做参数重编译比对。未来新增模板、真实 Prometheus API 参数、代理路径、限流和响应形状都必须独立评审，不能把 fake 全绿当作服务端兼容。
- **本地 Prom metric recording 是有限 synthetic catalog**：装配时只生成默认 30 分钟窗口、中心时刻前后 120 分钟、两个固定告警样例的精确键。它服务短时本地/Compose smoke，不是长时间运行或非默认窗口的数据源；未命中会降级为 `INDETERMINATE`，不提供 fallback。
- **Prometheus 可答性与渲染把步骤 ID 当作版本化隐式契约**：当前通过 Evidence ID 的 `:s1`/`:s2` 后缀区分告警与指标。现有 plan/answerability/render 测试固定该形状；未来改步骤命名必须同步升级并复核四层，不能只改 compiler。
- **资产 fake 不是 CMDB 兼容证明**：本地 catalog 只有一个 synthetic 资产及三个精确别名；真实字段、分页、权限、重复数据和错误语义均未验证。不同 selector 目前各自形成预执行 fingerprint，不声称跨别名同一身份。
- **资产 Evidence 固定一组版本化隐式契约**：capability key、`s1`、field set、selector version 和九字段白名单必须与 planner/reflection/renderer 同步演化；完整回归能发现当前改名漂移，但不能替代未来升版评审。
- **资产 scope 安全测试缺同文件正向对照**：`tests/security/test_asset_scope_isolation.py` 当前只有跨 scope 负向断言；后续应补 in-scope 成功对照，避免 recording 全部 miss 时该文件自身空过。现有 unit 正向用例与安全负向用例组合仍能抓住已做的常量化变异。
- **资产 Evidence 六个常量副本缺 pairing 真源**：后续应增加 pairing 测试，或改为 Prometheus builder 的装配层传参模式；当前漂移方向是 fail-closed，但会造成难定位的功能故障。
- **`asset_id` 的 ASCII 边界待真实契约决定**：当前 Unicode NFC + 精确匹配不会造成越权，但可能出现视觉同形却查询 miss；未来真实资产 adapter 必须单独立项并取得权威契约后再决定，不属于只负责 StarRocks 真实只读的 M6b。
- **资产歧义不要求补槽是有意语义**：精确 selector 已由用户给出，两行结果没有可补槽位，因此 `needs_user_input=False`；Prometheus 多告警还能用 fingerprint 消歧，故为 `True`。不要把两者机械统一。
