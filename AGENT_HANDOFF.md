# 小维 Agent 2.0 当前交接

> 这是当前有效口径，不是按日期堆叠的变更流水。历史变更由 Git 提交承载；详细复盘放到 `docs/handoff/archive/`。完整的命令、exit code 和逐步输出放在里程碑验收报告中，不写入本文件。

## 1. 当前基线

| 项目 | 当前值 |
| --- | --- |
| 项目目录 | `/Users/kloenguyen/Desktop/agent` |
| 截止时间 | 2026-09-01（Asia/Shanghai） |
| 阶段 | M0：设计、计划与本地 Git 基线（Phase 0） |
| 总体计划 | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) Approved V2，**已于 2026-09-01 获项目负责人批准** |
| Git 基线 | 本地 `main` 基线 SHA `7ca391daffbfec65c8f0d1adbcd7e4180fa09178`，仅含五份 Markdown 与 `.gitignore` |
| 工作分支 | `claude/m0-plan-closure`（从上述基线创建，未合并） |
| 远程 / PR / CI | **未配置，未验证**；由 M1 单独拍板后绑定 |
| 运行状态 | 尚未声明 API、Worker、PostgreSQL、Docker Compose 或任何工具调用可运行 |
| 生产状态 | 未部署、未 canary、未用户验收 |
| 首个闭环 | `starrocks.slow_query.diagnose`（已拍板，M3 实现，当前未实现） |

旧项目 `ivor_aiops` 只提供历史边界和问题样本。本项目不把旧项目的分支、SHA、能力地图、线上状态或遗留待办当作自身事实。

## 2. 已确认的设计口径

已固化到 [ARCHITECTURE.md](ARCHITECTURE.md) 与 `docs/adr/`：

- 采用模块化单体 + Docker Compose 的目标部署形态；控制面与数据面分离；PostgreSQL 作为 TaskStore、审批和审计事实真源。
- `XiaoweiRuntime` 是新的应用编排入口；不复用旧项目 `XiaoweiEngine` 的代码。
- LLM 只产结构化理解、解释和建议；不能直接选工具、目标、SQL、审批或执行。
- `CapabilityResolver` 是唯一候选生成真源；`route_shadow` 只消费 Resolver 输出，record-only。
- `ApprovalGate` 是共享组件，由 `WorkflowRunner` 在具体副作用步骤前调用；恢复时重解析并重算 `plan_hash` 与 `target_fingerprint`。
- SQL 由确定性 compiler 生成并经 `sqlglot` AST Guard；`ToolGateway` 是外部系统唯一入口。
- 外部日志、错误、知识、网页和用户粘贴文本全部按 `ExternalContent` 处理。
- **执行上下文统一命名为 `tenant_id`、`actor`、`environment_id`**；`RequestContext` 三项必填，`RequestEnvelope.environment_id` 可选；模块边界显式传递 `RequestContext`，其他 DTO 不机械复制这三项，精确字段归属由 M2 审定。
- **Phase 0 只做仓库、工具链、配置、日志/trace 和测试骨架**；业务契约（含 `ExternalContent`、`AdapterResponse`、error model、TaskStore CAS/lease/fencing 支持类型）全部属于 Phase 1。
- **验证命令单一真源**为 `python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`；禁止裸 `pytest` 调用形式。
- 允许受限 DSL 不等于 V1 必须实现；M0-M9 使用显式 `CapabilitySpec`，达门槛后再以 ADR-004 单独立项。
- 默认 Runner 是 `DeterministicStepRunner`；LangGraph 只能作为 adapter，先通过真实生命周期评测。
- **E1 默认关闭**：E1 指**经 `ToolGateway` 对被管运维目标执行的 `side_effect` 操作**；M0-M7 全程禁止 E1（含非生产环境），M8 的受控写需三项显式条件齐备，生产写另需独立授权。TaskStore/approval/audit/evidence 的内部持久化、本地 migration 和测试 fixture/recording **不属于 E1**，但内部持久化不得作为绕过 `ToolGateway` 修改运维目标的代理通道。
- `test-env verified` 是非生产运行证据的旁注标签，不属于 readiness ladder，也不替代 `canary`。

## 3. 已生效的决策记录

| ADR | 主题 | 状态 |
| --- | --- | --- |
| [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) | 首批能力、初始执行上下文与真实调用许可 | Accepted 2026-09-01 |
| [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md) | 工程与测试基线：Python 3.11、pytest、security marker gate、Ruff、mypy | Accepted 2026-09-01 |

首批三个能力：`starrocks.slow_query.diagnose`（M3）、`prometheus.alert.evidence`（M6a）、`asset.inventory.lookup`（M6a），均先只读 fake/recording。

**外部调用许可**（ADR-007 D4）：真实运维目标系统与真实模型 API 的**网络调用**在 M0-M6a 全程禁止；本地隔离 PostgreSQL/Compose 经 M4/M5 各自里程碑批准后允许；StarRocks 非生产只读需 M6b 单独授权。CI 全程不持凭证，不执行真实运维目标、真实模型、非生产只读或任何写调用。

**模型边界**：实现 provider adapter 与调用真实模型 API 是两件事。adapter 的实现和离线测试允许在其所属里程碑内进行，但**实现权不等于调用权**；M5 之后**不自动获得**真实调用权限。发起真实模型网络调用必须另设独立里程碑，并单独批准 ADR、凭证引用、数据范围和保留策略，且不得与 M0-M6a 的禁止期冲突。

**写权限**（ADR-007 D6/D7）：**生产连接和生产写默认禁止**。E1 的精确范围是**经 `ToolGateway` 对被管运维目标（StarRocks、Prometheus、资产系统、MySQL、Kafka、Kubernetes 等）执行的 `side_effect` 操作**，判定依据是步骤的 `side_effect` 标记与 `ToolGateway` 调用边界，不依据是否发生磁盘写入。**M0-M7 全程禁止 E1，含非生产环境**；M8 才可开放一条低风险的测试环境受控写，且必须同时满足 ADR-005 已定稿、项目负责人已批准、ADR-007 已记录明确例外或完成修订三项，缺一即保持禁止。**生产写仍需新的独立授权和独立验收计划**，不能由 M8 的测试环境结论推导得出。

**不属于 E1**：TaskStore 的任务与状态迁移、approval 记录、audit 事件、evidence 索引与脱敏摘要等系统内部持久化；本地数据库 migration；测试 fixture、recording 和测试产物。这三类的许可由 D4 的 C 层与各里程碑批准范围决定，因此 M4 的 PostgreSQL TaskStore、M5 的 Compose 集成测试和 CI 的本地隔离数据库写入均属合规。**但内部持久化不得作为绕过 `ToolGateway` 修改运维目标的代理通道**；任何以写 TaskStore、audit、evidence 或 migration 为名而实际触达运维目标的路径一律按 E1 认定并禁止。CI 可以写本地隔离 PostgreSQL 和测试产物，**但 E1 调用次数必须为 0**。

**真实调用开放点按类别分别管理**，不存在「所有真实调用只能发生在 M6b」的说法：当前已批准路线中的首个真实运维目标调用是 M6b 的 StarRocks 非生产只读（仍需单独授权）；真实模型 API 调用遵守 B2 的独立里程碑；M8 的测试环境受控写遵守 E1 与 D6 三项门。

## 4. M0 文档收口证据

在 `claude/m0-plan-closure` 分支上，基于基线 `7ca391da` 的收口提交：

| # | commit SHA | 主题 | 范围 |
| --- | --- | --- | --- |
| 1 | `7928d326b5d22fadc38f62ae4609a47fb3571027` | 新增 ADR-007 与 ADR-008 | `docs/adr/` |
| 2 | `0bb9ae2419465e333828d4b84010bedb220e8cde` | 统一执行上下文字段名，收敛 Phase 0/1 边界，补 test-env 标签说明与 ADR 索引 | `ARCHITECTURE.md` |
| 3 | `37c2b981fe02697b6ff1ededa4d903057a98c101` | 统一验证命令为 `python -m` 形式并收敛 Python 版本口径 | `AGENTS.md`、`ARCHITECTURE.md`、`README.md`、`DEVELOPMENT_PLAN.md` |
| 4 | `7c07ff5fb97fbf297881182063c3d25305c2e274` | 纳入计划文档与 `docs/adr` 落点，更新计划批准状态与事实基线 | `AGENTS.md`、`README.md`、`DEVELOPMENT_PLAN.md` |
| 5 | `f9e5ba9c7c08ec10c5cfb4f30913195bc6115c01` | handoff 首次记录计划已批准、基线 SHA 与收口 SHA | `AGENT_HANDOFF.md` |
| 6 | `5a175d1974ec9d0627576e0dd756dee897978e9d` | 收敛正文中残留的 `tenant`/`env` 字段名别名 | `ARCHITECTURE.md`、`DEVELOPMENT_PLAN.md` |
| 7 | `b8ab1fd9ab609c1890cf4c4e43f49bbef853db30` | handoff 补记提交 6 与字段命名复核结论 | `AGENT_HANDOFF.md` |
| 8 | `31ad9b24d4ba3f96dc7b1c52fce12a1c17971817` | 收紧写权限与真实模型调用边界，分离计划职责与漂移事实，清除历史命令字面量 | `AGENTS.md`、`DEVELOPMENT_PLAN.md`、`docs/adr/` |
| 9 | `873cae94ed276250c8427251e02815e241713180` | handoff 同步边界并补全收口提交清单 | `AGENT_HANDOFF.md` |
| 10 | `7b75a126f0249f8a3164ad20a05d31ce9e2f487f` | 定义 E1 为经 `ToolGateway` 对运维目标的 `side_effect` 操作，修正 M1/CI 真实调用口径 | `DEVELOPMENT_PLAN.md`、`docs/adr/ADR-007` |

提交 1-7 为 Codex 首轮审查范围，原始差异为 `7 files changed, 306 insertions(+), 114 deletions(-)`。提交 8-9 为第二轮修订（Codex 复审 SHA `873cae94`），提交 10 起为第三轮 E1 权限分类修订。全部为前向追加，未 rebase、未 amend、未 reset，历史未被改写。

**本文件所在提交的 SHA 不写在此处**，因为提交无法记录自身 SHA；分支最终 HEAD SHA 由 M0 验收报告给出，供 Codex 按精确 SHA 审查。

## 5. 下一步顺序

1. Codex 按上述精确 commit SHA 审查 M0 的真实 diff、文档一致性和安全边界是否被削弱。
2. M0 验收通过后，才编写 M1 详细实施计划；**M0 未验收前不进入 M1**。
3. M1 建立 Python 工程与 CI 基线，并单独拍板 Git 远程与 CI runner；CI 不持凭证、不执行真实外部调用。
4. M2 实现 contracts、`ExternalContent`、`AdapterResponse`、error model、TaskStore CAS/lease/fencing 交互形状、fake ToolGateway 和 fake TaskStore。
5. 以 TDD 落地 M3 第一条只读垂直闭环，并用仅测试的合成副作用步骤反证 ApprovalGate 不能被绕过。
6. M4 实现 PostgreSQL TaskStore 的并发、恢复与终态保护；M5 完成 API/CLI/Worker/Compose。
7. M6a 完成两个 fake 能力；M6b 在单独授权下做 StarRocks 非生产真实只读验证。

## 6. 仍需拍板的事项

- 审批主体、审批渠道、审批有效期和拒绝/过期/冲突后的恢复语义（M8 前，ADR-005）。
- M8 受控写的三项开放条件是否齐备：ADR-005 定稿、项目负责人批准、ADR-007 明确例外或修订。
- 生产写的独立授权与独立验收计划（不由 M8 推导）。
- 真实模型 API 网络调用所属的独立里程碑，及其 ADR、凭证引用、数据范围和保留策略授权。
- 证据、报告和大产物的存储位置及保留周期（M6b 前）。
- M4/M5 本地隔离 PostgreSQL 与 Compose 的里程碑批准。
- M6b 连接测试环境 StarRocks 真实只读的单独授权（环境、账号 secret reference、范围、时窗、脱敏、recording 删除方式）。
- Git 远程、默认分支保护、CI runner 和发布环境（M1）。
- 代码格式化方案（M1，ADR-008 已明确 Ruff 只作为 linter）。
- 固定开发租户 ID 的具体取值（M1）。

未拍板前的安全默认值：单租户开发、只读、fake adapter、无真实生产连接、无真实模型调用、无 LangGraph、无向量数据库、**任何环境均无 E1 操作**（系统内部持久化、本地 migration 和测试产物不在此列）。

## 7. 不要盲改

- 不要把 API、飞书或 CLI 变成第二个 Runtime。
- 不要新增关键词总表或独立候选生成器；先检查 Resolver 和 capability snapshot。
- 不要在不同 Runner、不同渠道或 shadow 中复制审批、Policy、SQLGuard、目标解析或终态语义。
- 不要为了“智能”开放模型 function calling、自由 ReAct 或模型直出 SQL/命令。
- 不要把 LangGraph checkpoint 当成 TaskStore 真源。
- 不要把外部文本中的指令、错误码或状态描述未经归类直接写进执行决策。
- 不要在文档或脚本中恢复缺少 `python -m` 前缀的测试命令形式。
- 不要把 provider adapter 的实现进度当作真实模型调用许可；不要在 M0-M7 以任何理由开启 E1；不要用 M8 的测试环境结论推导生产写许可。
- 不要把 TaskStore、approval、audit、evidence 或 migration 的内部持久化当作修改运维目标的旁路；也不要反过来把这些内部持久化误判为 E1 而阻塞 M4/M5。
- 不要宣称代码已部署、线上可用或能力已被用户接受，除非本文件有对应 SHA、命令、环境和验收证据。
- 不要删除或覆盖用户未提交文件；不要运行破坏性命令。发现漂移或异常时停止并报告，不自动回滚。

## 8. 验证记录

### 已验证

- 本地 Git 仓库已初始化，`main` 基线提交 `7ca391da` 的树内容为 6 个文件：`.gitignore`、`AGENTS.md`、`AGENT_HANDOFF.md`、`ARCHITECTURE.md`、`DEVELOPMENT_PLAN.md`、`README.md`；`.DS_Store` 未被纳入。
- 分支 `claude/m0-plan-closure` 已从该基线创建；截至本文件所在提交之前，分支上共有 10 个收口提交，SHA 与范围逐条列于第 4 节。
- 初始化前对六个基线文件做过 SHA-256 快照比对，全部一致，未发生计划外漂移。
- 对纳入 Git 的全部文件做过敏感信息扫描（私钥、云凭证、token、连接串、IP、邮箱），真实命中数为 0。
- 四份文档中原有的 7 处缺少 `python -m` 前缀的测试命令已全部改为规范形式；全部 Markdown 的非规范命令扫描结果为 0，包括 ADR 与本文件在内均不再保留旧写法的字面量。
- 执行上下文字段名已全项目统一为 `tenant_id`、`actor`、`environment_id`，正文中的字段名式枚举无旧别名残留；`ARCHITECTURE.md` 与 `README.md` 架构图内的 `tenant`/`env` 是概念轴标签，不是 DTO 字段，按既定范围未修改。

### 只读推理

- 五份文档的交叉冲突清单与定级（执行上下文字段名漂移、Phase 0/1 重叠、外部调用许可自相矛盾、命令口径分裂、`test-env verified` 术语缺口等）来自逐份阅读比对，无运行时证据。
- README 架构图与 `AGENTS.md`、`ARCHITECTURE.md` 的执行链顺序、`StepAdmission` 从属关系和 `ApprovalGate` 触发条件已复核一致，因此**未修改该图**，本轮未对其产生 diff。

### 未覆盖

- 无远程、无 PR、无 CI；未绑定任何远程仓库。
- 无 Python 代码、无 `pyproject.toml`、无依赖安装、无虚拟环境。
- `python -m pytest`、`ruff`、`mypy` 从未在本项目执行过；四条验证命令目前是目标口径，不是可执行事实。
- 未连接任何外部系统，包括 StarRocks、Prometheus、资产系统、PostgreSQL、Docker Compose 和任何模型 API。
- ADR-001 至 ADR-006 尚未编写。

### 残余风险

- 包含本文件的分支最终 HEAD SHA 不在文件内，只能由验收报告提供，Codex 需据此核对。
- Git 初始化之前发生的所有文档修订永久没有 commit SHA 证据。
- 工具链版本号（pytest、Ruff、mypy）尚未锁定，M1 之前无法复现完全一致的检查结果。
- 固定开发租户 ID 与代码格式化方案未定，M1 之前相关配置仍是空缺。
- M0 只做了文档与决策收口，没有任何可执行代码验证架构约束是否真的可实现。
