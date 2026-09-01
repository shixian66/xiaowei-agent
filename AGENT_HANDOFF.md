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
- `test-env verified` 是非生产运行证据的旁注标签，不属于 readiness ladder，也不替代 `canary`。

## 3. 已生效的决策记录

| ADR | 主题 | 状态 |
| --- | --- | --- |
| [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) | 首批能力、初始执行上下文与真实调用许可 | Accepted 2026-09-01 |
| [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md) | 工程与测试基线：Python 3.11、pytest、security marker gate、Ruff、mypy | Accepted 2026-09-01 |

首批三个能力：`starrocks.slow_query.diagnose`（M3）、`prometheus.alert.evidence`（M6a）、`asset.inventory.lookup`（M6a），均先只读 fake/recording。

外部调用许可分五层：真实运维目标系统与真实模型 API 在 M0-M6a 全程禁止；本地隔离 PostgreSQL/Compose 经 M4/M5 各自里程碑批准后允许；StarRocks 非生产只读需 M6b 单独授权；生产连接与任何写操作全程禁止。CI 全程不持凭证。

## 4. M0 文档收口证据

在 `claude/m0-plan-closure` 分支上，基于基线 `7ca391da` 的收口提交：

| 主题 | commit SHA | 范围 |
| --- | --- | --- |
| 新增 ADR-007 与 ADR-008 | `7928d326b5d22fadc38f62ae4609a47fb3571027` | `docs/adr/` |
| 统一执行上下文字段名，收敛 Phase 0/1 边界，补 test-env 标签说明与 ADR 索引 | `0bb9ae2419465e333828d4b84010bedb220e8cde` | `ARCHITECTURE.md` |
| 统一验证命令为 `python -m` 形式并收敛 Python 版本口径 | `37c2b981fe02697b6ff1ededa4d903057a98c101` | `AGENTS.md`、`ARCHITECTURE.md`、`README.md`、`DEVELOPMENT_PLAN.md` |
| 纳入计划文档与 `docs/adr` 落点，更新计划批准状态与事实基线 | `7c07ff5fb97fbf297881182063c3d25305c2e274` | `AGENTS.md`、`README.md`、`DEVELOPMENT_PLAN.md` |
| 收敛正文中残留的 `tenant`/`env` 字段名别名 | `5a175d1974ec9d0627576e0dd756dee897978e9d` | `ARCHITECTURE.md`、`DEVELOPMENT_PLAN.md` |

**包含本文件在内的分支最终 HEAD SHA 不写在此处**，因为提交无法记录自身 SHA；该 SHA 由 M0 验收报告给出，供 Codex 按精确 SHA 审查。

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
- 证据、报告和大产物的存储位置及保留周期（M6b 前）。
- M4/M5 本地隔离 PostgreSQL 与 Compose 的里程碑批准。
- M6b 连接测试环境 StarRocks 真实只读的单独授权（环境、账号 secret reference、范围、时窗、脱敏、recording 删除方式）。
- Git 远程、默认分支保护、CI runner 和发布环境（M1）。
- 代码格式化方案（M1，ADR-008 已明确 Ruff 只作为 linter）。
- 固定开发租户 ID 的具体取值（M1）。

未拍板前的安全默认值：单租户开发、只读、fake adapter、无真实生产连接、无 LangGraph、无向量数据库、无写操作。

## 7. 不要盲改

- 不要把 API、飞书或 CLI 变成第二个 Runtime。
- 不要新增关键词总表或独立候选生成器；先检查 Resolver 和 capability snapshot。
- 不要在不同 Runner、不同渠道或 shadow 中复制审批、Policy、SQLGuard、目标解析或终态语义。
- 不要为了“智能”开放模型 function calling、自由 ReAct 或模型直出 SQL/命令。
- 不要把 LangGraph checkpoint 当成 TaskStore 真源。
- 不要把外部文本中的指令、错误码或状态描述未经归类直接写进执行决策。
- 不要在文档或脚本中恢复裸 `pytest` 调用形式。
- 不要宣称代码已部署、线上可用或能力已被用户接受，除非本文件有对应 SHA、命令、环境和验收证据。
- 不要删除或覆盖用户未提交文件；不要运行破坏性命令。发现漂移或异常时停止并报告，不自动回滚。

## 8. 验证记录

### 已验证

- 本地 Git 仓库已初始化，`main` 基线提交 `7ca391da` 的树内容为 6 个文件：`.gitignore`、`AGENTS.md`、`AGENT_HANDOFF.md`、`ARCHITECTURE.md`、`DEVELOPMENT_PLAN.md`、`README.md`；`.DS_Store` 未被纳入。
- 分支 `claude/m0-plan-closure` 已从该基线创建，四个文档收口提交 SHA 见第 4 节。
- 初始化前对六个基线文件做过 SHA-256 快照比对，全部一致，未发生计划外漂移。
- 对纳入 Git 的全部文件做过敏感信息扫描（私钥、云凭证、token、连接串、IP、邮箱），真实命中数为 0。
- 四份文档中原有的 7 处裸 `pytest` 调用已全部改为 `python -m pytest` 形式；`docs/adr/ADR-008` 背景段保留一处旧写法作为历史引用。
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
