# 小维 Agent 2.0 当前交接

> 这是当前有效口径，不是按日期堆叠的变更流水。历史变更由 Git 提交承载；当仓库初始化后，详细复盘放到 `docs/handoff/archive/`。

## 1. 当前基线

| 项目 | 当前值 |
| --- | --- |
| 项目目录 | `/Users/kloenguyen/Desktop/agent` |
| 截止时间 | 2026-09-01（Asia/Shanghai） |
| 阶段 | Phase 0：文档与契约初始化 |
| Git | 当前目录尚未初始化为 Git 仓库；暂无分支、commit SHA、PR 或 CI 证据 |
| 运行状态 | 尚未声明 API、Worker、PostgreSQL、Docker Compose 或真实工具调用可运行 |
| 生产状态 | 未部署、未 canary、未用户验收 |
| 首个闭环 | 尚未拍板；建议从一个只读 StarRocks 诊断场景开始 |

旧项目 `ivor_aiops` 只提供历史边界和问题样本。本项目不把旧项目当前分支、SHA、能力地图、线上状态或遗留待办当作自身事实。

## 2. 已确认的设计口径

本轮已固化到 `ARCHITECTURE.md`：

- 采用模块化单体 + Docker Compose 的目标部署形态。
- 控制面与数据面分离；PostgreSQL 作为 TaskStore、审批和审计事实真源。
- `XiaoweiRuntime` 是新的应用编排入口；不直接复用旧项目 `XiaoweiEngine` 的代码。
- LLM 只产结构化理解、解释和建议；不能直接选工具、目标、SQL、审批或执行。
- `CapabilityResolver` 是唯一候选生成真源；`route_shadow` 只消费 Resolver 输出，record-only，不参与 active 路由。
- `ApprovalGate` 是 Runtime/governance 的共享组件，由 `WorkflowRunner` 在具体副作用步骤前调用；多步计划在执行中暂停，恢复时重解析并重算 hash。
- `plan_hash` 与 `target_fingerprint` 的 canonicalization、审批绑定和 stale approval 语义已经明确。
- DB/资产域允许受限 DSL；每个 DSL 实例必须满足八项最小契约，不携带任意代码、SQL、prompt、executor 或 formatter。
- 允许 DSL 不等于 V1 必须实现；M0-M9 使用显式 `CapabilitySpec`，M6a 先采集复用和维护数据，达到门槛后再以 ADR-004 单独立项。
- 默认 Runner 是 `DeterministicStepRunner`；LangGraph 只能作为 `WorkflowRunner` adapter，先通过真实生命周期评测再决定是否启用。
- SQL 由确定性 compiler 生成并经 `sqlglot` AST Guard；ToolGateway 是外部系统唯一入口。
- 外部日志、错误、知识、网页和用户粘贴文本全部视为 `ExternalContent`，不能改变安全策略或执行目标。

## 3. 本轮已完成

- [x] 创建项目规则：[AGENTS.md](AGENTS.md)
- [x] 创建人类开发入口：[README.md](README.md)
- [x] 创建目标架构：[ARCHITECTURE.md](ARCHITECTURE.md)
- [x] 创建当前交接：[AGENT_HANDOFF.md](AGENT_HANDOFF.md)
- [x] 创建 Git 初始化前的忽略规则：[.gitignore](.gitignore)
- [x] 根据 Claude 复审统一 `runners/`、`reflection/`、`tools/`、`tests/security/`、`tests/evals/` 和 pytest 约定。
- [x] 将 `StepAdmission` 写入第一层安全链，并把安全测试设为治理/规划/工具变更的独立 CI gate。
- [x] 明确新项目继承 `ivor_aiops` 的开发纪律，但不复制旧项目实现和历史状态。
- [x] 创建总体开发计划草案：[DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)。
- [x] 逐条核对 Claude 第一轮计划审查：纳入 TaskStore CAS/fencing 交互形状、`ExternalContent`、ApprovalGate 合成分支、M6a/M6b 拆分、CI 无真实凭证、M0 Git 基线和证据术语修正；DSL 改为 V1 明确延期而非在 M6 强制试点。
- [x] 总体计划已更新为 Review Draft V2；Claude 意见已处理，但尚未获得项目负责人批准。

## 4. 下一步顺序

在写业务代码前，按以下顺序推进：

1. 由项目负责人审核 Review Draft V2，并拍板首批三类闭环能力、单租户默认值、真实调用许可、Git 远程和 CI 环境；计划建议的三个能力均先从只读 fake/recording 开始。
2. 计划批准后进入 M0：检查 `.gitignore`、敏感信息和纳入范围，以当时的五份 Markdown 与 `.gitignore` 初始化本地 `main`，记录基线 SHA。
3. 从本地基线创建 `claude/m0-plan-closure`，收敛 Phase 0/Phase 1 边界和必要 ADR；所有后续文档变更按精确 SHA 审查。
4. M0 通过后单独编写 M1 详细计划，建立 Python 工程与 CI 基线；CI 不持有测试/生产凭证，不执行真实外部调用。
5. M1 通过后为 M2 单独规划并实现 contracts、`ExternalContent`、TaskStore CAS/lease/fencing 交互形状、fake ToolGateway 和 fake TaskStore。
6. 以 TDD 落地 M3 第一条只读垂直闭环，并用仅测试的副作用步骤反证 ApprovalGate 不能被绕过。
7. 再实现 M4 PostgreSQL TaskStore 的真实并发、恢复和终态保护，随后进入 M5 Compose。
8. M5 后将内部扩展与外部依赖分开：M6a 完成两个 fake 能力，M6b 在单独授权下做 StarRocks 测试环境真实只读验证。

## 5. 仍需拍板的事项

这些决定会影响第一阶段实现，不能通过隐含默认值绕过：

- 首批三个闭环能力及每个能力允许的真实调用范围。
- 初始是否单租户；如果多租户，租户/actor/环境的隔离边界。
- 审批主体、审批渠道、审批有效期和拒绝后的恢复语义。
- 证据、报告和大产物的存储位置及保留周期。
- 是否授权 M6b 连接测试环境的 StarRocks 真实只读服务，以及允许的环境、账号、范围、时窗和证据保留方式。
- Git 远程、默认分支、CI runner 和发布环境。

未拍板前的安全默认值：单租户开发、只读、fake adapter、无真实生产连接、无 LangGraph、无向量数据库、无写操作。

## 6. 不要盲改

- 不要把 API、飞书或 CLI 变成第二个 Runtime。
- 不要新增关键词总表或独立候选生成器；先检查 Resolver 和 capability snapshot。
- 不要在不同 Runner、不同渠道或 shadow 中复制审批、Policy、SQLGuard、目标解析或终态语义。
- 不要为了“智能”开放模型 function calling、自由 ReAct 或模型直出 SQL/命令。
- 不要把 LangGraph checkpoint 当成 TaskStore 真源。
- 不要把外部文本中的指令、错误码或状态描述未经归类直接写进执行决策。
- 不要宣称代码已部署、线上可用或能力已被用户接受，除非 handoff 中有对应 SHA、命令、环境和验收证据。
- 不要删除或覆盖用户未提交文件；不要运行破坏性命令。

## 7. 验证记录

已验证：

- 读取了旧项目 `ivor_aiops/AGENTS.md`，确认本项目需要继承其中文沟通、证据优先、确定性执行链、TDD/回归、精确 SHA 评审、handoff 和 secret 管理纪律。
- 检查了新目录；当前原有内容仅发现 `.DS_Store`，没有覆盖已有的四份目标文档。
- 已补充 `.gitignore`，覆盖 `.DS_Store`、Python 缓存、虚拟环境、环境变量、reports 和 logs；尚未执行 Git 初始化。
- 已完成四份文档的交叉引用检查：未发现旧 `unittest` 命令、并列 `tools/`/`integrations/` 落点或合并的 Reflection 目录约定。
- 当前目录执行 `git status --short --branch` 返回 exit 128，原因是尚未初始化 Git；因此本轮没有伪造分支或 SHA 证据。
- 2026-09-01 已逐份读取四份既有 Markdown，并基于当前文档事实编写总体开发计划草案；尚未执行代码、依赖、测试或外部服务验证。
- 2026-09-01 已读取并逐项核对 Claude 计划审查；Review Draft V2 已吸收确认成立的意见，并对 DSL 试点建议作“V1 延期、以数据触发 ADR”的修正处理。此次发生在 Git 初始化前，因此没有 commit SHA 证据。

未验证：

- 尚无业务代码、依赖、测试、数据库迁移、Compose 启动或真实工具调用。
- Review Draft V2 尚未获得项目负责人批准；首批能力、真实调用许可、Git 远程和 CI 仍未拍板。
