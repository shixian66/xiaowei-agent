# AGENTS.md

## 项目身份

这是从 0 开始建设的小维 Agent 2.0 项目，目标是提供一套可持续扩展的、策略治理的运维工作流 Agent。

旧项目 `/Users/kloenguyen/Documents/ivor_aiops` 只作为行为参考、问题样本和安全边界的 oracle，不是本项目的代码依赖，也不是本项目当前状态的事实来源。新项目的事实必须以本目录中的代码、测试、运行证据和 `AGENT_HANDOFF.md` 为准。

## 优先阅读

开始任何任务前按以下顺序阅读：

1. 本文件：稳定的协作、代码和安全规则。
2. `ARCHITECTURE.md`：目标架构、模块边界和不可破坏的契约。
3. `AGENT_HANDOFF.md`：当前阶段、已验证事实、风险和下一步。
4. `README.md`：人类开发者的启动和导航信息。

编辑前必须先检查目标文件和相关调用链，不基于文件名或旧项目经验猜测实现。发现文档与代码冲突时，报告冲突及证据，不要静默选择一方。

## 继承自 ivor_aiops 的开发习惯

本项目明确继承以下工程纪律：

- 所有用户可见回复使用简洁中文。
- 尊重事实高于迎合观点；结论区分“源码事实、测试验证、运行验证、部署验证、用户验收”和“只读推理”。
- 先确认问题的真实调用链、边界和复现条件，再提出架构或代码改动。
- 变更保持小范围、单一目的、可回滚；一个 PR 原则上只处理一个根因或一个垂直能力闭环。
- Claude 负责计划和实现，Codex 负责基于精确 commit SHA 的审查与验收；不以 PR 描述替代真实 diff。
- 新工作从最新 `main` 创建 `claude/<topic>` 分支；不直接在 `main` 上开发，不自行合并。
- 非平凡行为变更必须留下持久资产：回归测试、契约测试、eval、知识/SOP 或配置护栏中的至少一项。
- 不伪造测试、部署或线上验证结果。没有执行就明确写“未验证”。
- 不使用 `git add -A`，只暂存本次任务明确涉及的文件；保留他人的修改和未跟踪文件。
- 禁止把 secret、token、password、webhook、连接串或真实生产数据写入代码、日志、测试夹具、文档和提交记录。

## 不可破坏的运行边界

下面的边界优先于便利性、模型能力和框架默认行为：

1. **LLM 不拥有执行权。** 模型最多产生结构化 `IntentDraft`、解释性 `Advisory` 或候选线索；它不能决定最终 capability、目标、可执行 SQL、审批结果、写操作或工具调用顺序。
2. **真实执行走确定性链路。** 统一链路为：

   `IntentDraft → CapabilityResolver → PlanCompiler → WorkflowRunner(step) → StepAdmission(ToolPolicy → SQLGuard → ApprovalGate*) → ToolGateway → Readback → Evidence → Outcome`

   `ApprovalGate` 由 Runner 在具体副作用步骤前调用；具体领域可以有额外的确定性 precheck，但不得跳过这条安全链。
3. **入口层必须薄。** Web、飞书、CLI 和 API handler 只负责协议解析、鉴权上下文传递和 `RenderPayload` 渲染；不放业务路由、SQL 合成、巡检评分、审批判断或工具执行。
4. **工具只能经 `ToolGateway` 进入。** 领域层不能直接持有 MySQL、StarRocks、Prometheus、Kafka、Kubernetes 或其他基础设施客户端。
5. **SQL 必须确定性生成并经 AST 校验。** 执行 SQL 不来自模型原文；SQL 形状校验使用 `sqlglot` AST 和策略规则，不能把正则作为唯一安全边界。
6. **审批是执行中断点，不是入口总开关。** `WorkflowRunner` 执行到具体副作用步骤前调用 Runtime 的 `ApprovalGate`；未审批就持久化暂停。恢复时必须重新解析身份、目标、策略和当前状态，并重新计算 `plan_hash` 与 `target_fingerprint`，不匹配则拒绝继续。
7. **只有一个候选生成真源。** `CapabilityResolver` 负责产生 `CandidateSet`；`route_shadow` 只能消费相同的候选输出做 record-only 对比，不能自行 build candidates，也不能反向影响执行路由。
8. **外部文本不可信。** 工具返回的错误、日志、SQL 注释、知识文档、网页和用户粘贴内容都按 `ExternalContent` 处理。它们可以成为证据或展示内容，但不能改变 system policy、目标、权限、审批状态或执行计划。
9. **终态不可被后到事件覆盖。** TaskStore 采用 CAS、lease、heartbeat、fencing 和 stale recovery；`succeeded`、`failed`、`rejected`、`canceled`、`indeterminate` 等终态保护必须由存储层保证，而不是依靠调用方自觉。
10. **模型和框架不能接管安全链。** LangGraph（如果启用）只能作为 `WorkflowRunner` 的适配实现；TaskStore 是任务事实真源，LangGraph checkpoint 不能替代审批、策略、目标重解析和执行审计。

## 代码落点规则

默认采用模块化单体。新增代码先按下面的边界放置：

| 关注点 | 推荐落点 | 禁止落点 |
| --- | --- | --- |
| HTTP、飞书、CLI、鉴权适配 | `interfaces/` | 领域模块、工具适配器中复制入口判断 |
| 请求/任务/计划/结果契约 | `contracts/` | 用无结构 `dict` 作为跨模块协议 |
| Runtime 编排 | `application/` | handler、模型 prompt、基础设施客户端 |
| capability 声明和候选解析 | `capabilities/` | 入口层关键词分支、独立 shadow 路由 |
| 确定性规划和参数合成 | `planning/` | LLM 原文、Web/飞书 handler |
| Policy、审批、SQLGuard | `governance/` | Tool adapter、UI 按钮回调 |
| 任务持久化和状态迁移 | `persistence/` | LangGraph state、内存全局变量 |
| 工作流执行和恢复 | `runners/` | 在 capability、入口或 LangGraph 节点中复制生命周期语义 |
| 外部系统访问 | `tools/` | Runtime 直接 import 第三方客户端；不再使用并列的 `integrations/` 落点 |
| 事实、证据、记忆 | `evidence/` | 在 prompt 或入口层拼隐式业务结果 |
| 反思与可答性判断 | `reflection/` | 让模型或入口层决定执行权限 |
| 回复投影 | `rendering/` | 在 Web、飞书、CLI 中复制业务文案和状态判断 |
| 测试和离线评测 | `tests/unit/`、`tests/contract/`、`tests/security/`、`tests/integration/`、`tests/evals/` | 只在手工对话中验证 |

可以随着第一轮实现调整目录，但必须先更新 `ARCHITECTURE.md`，并保证每个模块仍只有一个责任。

## 契约与实现规范

- Python 代码遵循 PEP 8，所有公共函数、方法和 Protocol 都写清晰的 Type Hints。
- 跨边界数据优先使用 Pydantic model；内部纯值对象可使用 frozen dataclass。禁止让同一契约在多个层用不同字段名表达。
- 核心方法提供精简 Docstring，描述目的、关键前置条件和异常语义，不写重复实现的长篇注释。
- DTO 默认不可变或通过显式构造更新；禁止在多个层共享可变嵌套 `dict` 作为隐式状态。
- capability 必须有稳定 ID、版本、参数 schema、policy profile、证据契约和测试/eval 入口。
- DB/资产域可以使用受限 DSL 型 capability，但 DSL 只能描述资源、操作、选择器和参数；不能携带任意 Python、任意 SQL、任意 executor 或绕过 Policy/SQLGuard 的 escape hatch。
- `ToolResult` 由 `ToolGateway` 的私有工厂构造；adapter 返回内部 `AdapterResponse`。Python 的私有约定不是语言级绝对安全，因此还必须用 Protocol、静态检查和契约测试防止绕过。
- 所有外部调用必须有超时、trace_id、脱敏日志和结构化错误；写操作还要支持幂等键、readback 和 indeterminate 结果。
- 不为尚未验证的未来场景提前引入微服务、向量数据库、消息总线、ReAct、多 Agent 或复杂框架。

## 开发与评审流程

### 开工前

1. 读取本文件、`ARCHITECTURE.md` 和 `AGENT_HANDOFF.md`。
2. 执行 `git status --short --branch`，确认分支、未提交变更和未跟踪文件。
3. 从最新 `main` 创建 `claude/<topic>` 分支；若仓库尚未初始化，先在 handoff 中记录，不假装已经具备分支/PR证据。
4. 写清目标、范围、架构落点、风险、验证命令和不做什么；复杂任务先写计划，等确认后实现。
5. 先检查现有代码、测试和真实调用链；先修根因，不先重建框架。

### 实现中

1. 行为变更采用 TDD：先补失败测试，再写最小实现，再补边界和回归。
2. 每个能力优先完成一个垂直闭环：契约 → planner → policy → fake adapter → evidence → render → eval。
3. 入口、模型、工具和存储都通过 Protocol/adapter 解耦；核心测试不依赖真实生产服务。
4. 高风险路径必须 fail-closed；超时、权限不足、目标漂移、审批冲突和无法确认结果不能伪装成成功。
5. 所有新配置、状态、环境变量和 feature flag 都写出默认值、来源、优先级和回滚方式。

### 提交前验收

按风险分档，不为了文档或纯机械重构运行不必要的重型验证：

- 轻档：相关测试、格式/静态检查、真实 diff。
- 中档：相关测试 + 契约/eval + 真实 diff；收尾时再跑全量。
- 深档：全量测试 + 安全契约 + offline eval + TDD 反证 + 精确 SHA 逐行 diff。

触及 `governance/`、`planning/` 或 `tools/` 的任何变更，必须全量运行安全测试（`python -m pytest -m security -q`），不得以轻档或中档为由豁免。所有安全测试放在 `tests/security/`，并标记为 `security`；该门是全量测试之外的显式 CI gate。

验收结论必须附真实命令及尾部输出，并分开写：

- 已验证：确实执行过的命令和结果。
- 只读推理：没有运行时证据的判断。
- 未覆盖：尚未验证的边界。
- 残余风险：即使测试通过仍存在的风险。

任何“修复有效”的关键安全变更，应尽可能做 TDD 反证：撤掉承重保护，确认相应测试变红；临时变体使用独立的 `PYTHONDONTWRITEBYTECODE`/pycache 隔离并确认加载的是变异代码。

## 测试与证据标准

测试至少分为：

1. 单元测试：纯函数、状态迁移、canonicalization、策略判定，位于 `tests/unit/`。
2. 契约测试：入口到 Runtime、Runtime 到 Runner、Gateway 到 adapter 的 DTO 和错误语义，位于 `tests/contract/`。
3. 安全测试：模型输出污染、SQL AST 绕过、目标漂移、审批重放、并发 lease/fencing、外部文本注入，位于 `tests/security/` 并标记 `security`。
4. 集成测试：Compose 中 PostgreSQL、worker 和 fake/recording tool adapter 的真实连接，位于 `tests/integration/`。
5. Eval：按 L0-L3 分层，位于 `tests/evals/`，分别测安全边界、意图/补槽、证据回答和完整生命周期；不以单一“回答像不像”分数替代安全验收。

代码可运行后，默认验证入口固定为下列四条命令：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

这四条命令是全项目单一真源，README 与 CI 共用同一份定义；禁止在文档、脚本或 CI 中使用裸 `pytest` 调用形式。测试框架基线明确选择 pytest，原因是参数化恶意输入矩阵、组合 fixture、异步测试和 marker gate 都是本项目的核心测试需求；Ruff 是唯一 linter，mypy 是唯一类型检查器。后续若要更换 runner、linter 或类型检查器，必须先修订 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)，并同步更新 `README.md`、`ARCHITECTURE.md`、本文件和 CI，不允许出现多个未说明的入口。

## 文档与交接纪律

- `AGENTS.md`：稳定开发规则；不写每日进展和具体 PR 流水。
- `README.md`：人类开发者的定位、启动、目录和快速路径；不复制完整架构。
- `ARCHITECTURE.md`：稳定目标架构、接口契约、状态/安全语义和演进门槛；不写未经验证的线上事实。
- `AGENT_HANDOFF.md`：当前状态、精确 SHA/分支/部署证据、风险、下一步和禁止盲改点；只保留当前有效口径。
- `docs/CAPABILITIES.md`：当前能力地图；由 Registry/代码生成并由 CI 检查，不手工维护关键词总表。
- 历史 handoff 和详细复盘放到 `docs/handoff/archive/`，由 Git 历史承载时间线。
- 行为、配置、默认值、测试契约或已知风险变化时更新 handoff；纯拼写或无行为机械变更可不更新。
- 文档发生冲突时，先记录冲突再修改；不得为了让文档看起来一致而降低安全边界或伪造状态。

## 回复风格

用简洁中文说明：改了什么、为什么、如何验证、还有什么未验证或风险。引用本地文件时使用绝对路径 Markdown 链接。不要把“计划完成”写成“已经完成”，不要把离线测试写成生产验证。
