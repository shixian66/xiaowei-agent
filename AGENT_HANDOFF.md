# 小维 Agent 2.0 当前交接

> 当前状态、有效授权、风险与下一步的唯一维护入口。历史流水见文末索引，规范见 AGENTS / ARCHITECTURE / ADR / DEVELOPMENT_PLAN。

<a id="current-status"></a>
<a id="current-baseline"></a>

## 0. 当前事实

- **当前基线**：`main@869be7ae20887fb3cda7c3de3d07a15b166c543c`（2026-09-23，PR #72 已合入 W2-A 原子登录导航 context）。
  开工时仍须核对实际最新 main；本文其余 SHA 都是证据对象，不是新的开工基线。
- **W1a 写内核与 W1b 两个切片已离线实现并合入**，最强证据为 `tests`；该历史前置没有被 W2 取代。
- **W2-A 已合入，W2-B1 已形成离线候选但尚未合入**。W2-A 由 PR #72 合入当前基线；W2-B1 最终代码与测试候选为 `eeb6613c6cfb372c2da263f9bc90de16921e9286`（PR #73，核心实现提交 `4c75ec028f8a1d6a4fa6dd7f79c99bc07ede99cc`）：独立登录、工作台、管理中心与结果详情壳、闭集角色导航、按请求重建角色、脱敏集成状态投影已经离线实现；本地 Admin 的激活状态目标固定返回 403 且不改投，激活恢复的 subject 绑定由真实服务测试承重，已有 Operator Session 仍不能进入 Admin 路由。等待独立精确 SHA 复审。W2-B2 尚未开始；I3 延期未取消。
- **通用开发流程 V1**：本任务用户在方案复审后于 2026-09-23 明确“那你实施吧”，授权 Codex 按
  [实施计划](docs/superpowers/plans/2026-09-23-unified-development-workflow.md) 完成本次治理调整。
  规则与测试候选 `2cd6eb61046c50a43fd80fa19610031a18d8d256` 已通过独立修复确认；两项文档迁移遗漏已闭合。
  PR #70 已合入当前 main，规则已成为后续开发的协作基线；已批准的在途计划保持有效，提效尚未测量。
  这次流程集成不是 W2 计划批准、源码开工、合并、部署或验收归档授权。
- **真实边界**：没有真实 Gemini、飞书、StarRocks、Prometheus 或资产系统调用证据；没有部署、canary 或产品用户验收。
  CI 与一次性 PostgreSQL/Compose 的运行只证明对应隔离 `tests`，不证明真实渠道激活。RI2、RI3 PR 3E、RI4、RI6/H 层与 E1 仍有独立门。

### 当前能力与证据入口

| 范围 | 已确认事实与证据边界 |
| --- | --- |
| 三个只读能力 | `starrocks.slow_query.diagnose`、`prometheus.alert.evidence`、`asset.inventory.lookup` 都只到 fake/recording `tests`；生成声明见 [能力地图](docs/CAPABILITIES.md) |
| RI1 / RI3 / RI5 | OAuth adapter、Gemini adapter/durable Runtime、Web 本地管理面已离线实现；RI5 真机浏览器首启与本机完整 Compose 未验收，真实 Provider 现场 GO 未下达 |
| I1 | `SlotVerifier`/可信槽位升级已完成离线实现；ReadClass/Plan schema V2 已完成离线实现；ExecutionDisclosure 执行披露屏障已完成离线实现；I1-D Eval/closure 及 Task 1.4/1.5 澄清存储与子任务唯一消费已落地 |
| I2 | I2 限定领域普通对话已完成离线实施并归档，详见 [I2 归档](docs/handoff/archive/2026-09-20-I2-bounded-conversation.md)。回答只投影当前 CapabilitySnapshot，无工具/外部系统/长期记忆；knowledge_lookup/log_analysis 仍拒绝 |
| W1a | `rev_0014`、用户目录与 Admin 审计写内核、旧身份原子迁移已离线实现。`UserDirectoryStore.apply()` 是唯一授权写入口，每次授权改变与命令派生审计同事务提交；审计不可写则回滚 |
| W1b | `rev_0015`、ActivationRequest/ActivationStore、身份激活流程与单次群通知已离线实现。未知 OAuth 身份返回 `403 activation_pending` 且无 Session；未知群成员当次事件只发通用卡片，私聊未知身份拒绝。运行时从数据库目录重建主体，停用/撤权不进入重新激活，静态文件仅作发布前一次性迁移输入 |
| W2 及以后 | W2 计划 Approved V0.2 与 A（契约/迁移）已合入；B1（登录/角色/壳）实现候选待独立复审与合入，B2（配置 UI/API 搬迁）尚未开始。W3/W4/W5 保持后续阶段；W4c 与 R1 独立，不由 W2 解锁 |

W1a 最终受审 head `ed8c9e82db46d75e47e4761d8897d0d2d23f9831` 已由 PR #65 合入
`f92aa88b9016b52c35ebce4052a8609fd574d8fb`，历史 CI [35681783752](https://github.com/shixian66/xiaowei-agent/actions/runs/35681783752)
八项通过，其中 PostgreSQL 全量 `4528 passed`、零 skip。
W1b 切片 1 已由 PR #67 合入 `c05068f8021b167d32273e569c9a27ea68f9982b`；切片 2 最终受审 head
`e6fee708068bd46671d473f0a9fd7f933ed4ed37` 已由 PR #68 合入 `dfa7a5c9d507d7793b876d39b530519615f73f12`。
后者历史 CI [35728218343](https://github.com/shixian66/xiaowei-agent/actions/runs/35728218343) 八项通过，
PostgreSQL 全量 `4674 passed`、零 skip；本机离线为 `4325 passed, 349 skipped`，security 为
`1505 passed, 83 skipped`，Ruff/mypy 通过，一次性 PostgreSQL integration `351 passed`。
这些是历史运行记录，本轮没有重跑该 CI 或真实库；完整命令与变异链见[原文快照](docs/handoff/archive/2026-09-23-pre-workflow-v1.md)。

W2-A 实现与测试证据 head `33866a4e7d6fd8b6d2930f195e11c35f5ee7de95` 本机离线全量为
`4395 passed, 362 skipped`，security 为 `1505 passed, 83 skipped`，Ruff/mypy 通过；
专用一次性 PostgreSQL 16.15 容器全量 `4752 passed, 0 skipped`，容器已删除且未触碰既有实例。
四项隔离反证分别证明 context 必填、缺 context 回滚、收割级联与数据库 source/intent CHECK 承重。
GitHub CI [35827515879](https://github.com/shixian66/xiaowei-agent/actions/runs/35827515879)
在同一 head 上 8/8 通过且每个 job 都有真实 steps；该候选已由 PR #72 squash 合入
`869be7ae20887fb3cda7c3de3d07a15b166c543c`。这不构成部署、真实调用或用户验收证据。

W2-B1 核心实现提交 `4c75ec028f8a1d6a4fa6dd7f79c99bc07ede99cc`、最终代码与测试候选
`eeb6613c6cfb372c2da263f9bc90de16921e9286`（PR #73）的本机离线全量为
`4435 passed, 362 skipped`，security 为 `1512 passed, 83 skipped`，Ruff/mypy 通过；聚焦受影响回归为
`243 passed`。锁定 wheel 实测包含 `login/admin/index/detail` 的 HTML、CSS 与 JS；本机 headless Chrome
只完成登录壳 1440/1280/1024/390、管理壳 1440/1024、工作台 1440 与低于 1024 静态提示的视觉核对。
GitHub CI [35849155681](https://github.com/shixian66/xiaowei-agent/actions/runs/35849155681)
在最终代码与测试候选上 8/8 通过且每个 job 都有真实 steps；普通测试为
`4435 passed, 362 skipped`，security 为 `1512 passed, 83 skipped`，PostgreSQL integration 为
`4797 passed, 0 skipped`。这批证据不包含正式浏览器交互、部署、真实飞书或用户验收。

## 1. 当前基线

| 项目 | 当前值 |
| --- | --- |
| 项目目录 | 当前开发分支 `claude/w2-b1-login-shells`；基线只引用[第 0 节](#current-baseline)，不复制机器路径 |
| 截止时间 | 2026-09-23（Asia/Shanghai） |
| 阶段 | W2-A 已合入；W2-B1 源码候选已完成本地深档验证，尚待独立复审与合入 |
| 下一步 | 按最终 PR head 独立复审 W2-B1；合入后从最新 main 开始 W2-B2，不提前实施 W3/W4 或真实调用 |
| 总体计划 | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) Approved V2.5，Web 序列与各阶段硬门保持原定义 |
| 本机工具链 | Python 3.11.16；依赖由 uv.lock 锁定，标准安装与四门见 README / ADR-008；实际环境在每轮验收时记录 |
| 分支保护 | 2026-09-01 的记录为 private + GitHub Free 不支持、API 403，负责人批准延后；本轮未重查套餐或保护设置，不能假定已受保护 |
| 运行状态 | 三能力与模型/渠道离线闭环为 tests；真实运行、部署、canary、产品用户验收未取得 |

## 2. 已确认的设计口径

| 真源 | 管理的事实 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 开工、分档、授权复用、验证、精确 SHA 独立审查与交付 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Runtime / Resolver / Planner / Admission / Gateway / TaskStore 单一链路及契约 |
| [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) | 真实调用、租户/环境、E1 与基础设施授权 |
| [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md) | 工具链与四条权威验证命令 |
| [ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md) / [ADR-017](docs/adr/ADR-017-intelligent-interaction-and-clarification.md) | Plan schema V2、read_class、审批绑定、智能分流、澄清与执行披露 |
| [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) | 里程碑顺序、进入/退出门与已批准窄例外 |

当前 Provider 明文配置为 `.config/integrations.json`，Web 读写、所需进程只读、API 不挂载；模型调用端口只在 task worker 装配。
将来三域配置的迁移归 W4a，不能提前改写当前 README 启动步骤。历史设计摘要在快照内不再是并行真源。

## 3. 有效授权与独立门

- **Web 产品批准来源**：决策人 shixian66，日期 2026-09-20，永久记录
  <https://github.com/shixian66/xiaowei-agent/pull/59#issuecomment-5750387268>。
  批准 ADR-007 D5、ADR-014 RI5 R1/R2/R3、R2 风险接受与 `W0 → W1a` 优先级；该评论最初只授权 W0，后续源码授权不能由这条记录外推。
  总体设计 PR #58 合入 `4e5a844620b700e25d6a29e43687c1a4c876db16`；设计合入不是授权来源。
- W1a/W1b 各自批准计划与交付记录见 [W1a 计划](docs/superpowers/plans/2026-09-21-w1a-identity-authz-admin-audit.md)、
  [W1b 计划](docs/superpowers/plans/2026-09-22-w1b-identity-activation-and-notification.md) 和原文快照。
  登录 context 表 `web_oauth_login_contexts` 归 W2，私聊 Admin 通知与可靠重试归 W3。
- W2 [详细计划 Approved V0.2](docs/superpowers/plans/2026-09-23-w2-login-and-multi-shell.md) 已由 PR #71 合入并取得负责人开工口令；授权范围是 W2-A（闭集 intent/context/迁移）、W2-B1（登录、角色路由与四个 shell）和 W2-B2（配置 UI/API 搬迁），不包含 W3/W4/真实调用。W2-A 已由 PR #72 合入，W2-B1 仍须独立代码复审与合入。
- RI5 成套设计于 2026-09-14 接受；它不授权 RI2 或 RI3 PR 3E。ADR-014 R2 取消首次强制改密必须先在 loopback 完成的顺序门，
  但 `WebMode.LAN_HTTP` 的 loopback/RFC1918 Host 约束继续生效，release/canary 仍需边缘限流证据。
- M6b 仅离线实现并合入，真实验证被负责人延期，未验收归档。重新进入需唯一 canonical target、物理身份、带外 version/grants/DDL/identity digest、
  secret reference、actor/时窗、字段与证据处置以及新的现场 GO；真实装配仍需独立小补丁与复审，不能用环境变量选占位目标。
- M7 仅 PR 1–8 离线范围于 2026-09-09 经负责人验收归档；真实应用、凭据、网络、部署、canary、产品用户验收和完整退出门仍未通过。
- RI3 的 ADR-015/V7.1 与“开始 RI3”（2026-09-12）仅授权五个 PR 顺序离线实施。
  PR 3A–3D 已合入，首次真实 Gemini 读取/网络仍需 PR 3E 现场 GO 与供应商数据政策、synthetic corpus 确认；实现权不等于调用权。
- RI2 飞书、RI4 StarRocks、RI6 部署及 H 层生产只读均需各自独立批准；H 层授权面变化尚未签认。
- E1 按修改被管运维目标的后果判定，M0–M7 全环境禁止。M8 需 ADR-005 定稿、负责人批准、ADR-007 例外/修订三项齐备；生产写另需独立授权与验收。
  系统内部持久化、migration、测试产物本身不是 E1，但本地 PostgreSQL/Compose 的许可仍按 ADR-007 D4/D8 管理，不能作为触达运维目标的旁路。
- CI 不持有真实系统凭据，不调用真实模型/运维目标或 E1；只允许仓库范围的临时只读 GITHUB_TOKEN。

## 4. 历史证据索引

- [流程治理前的原文快照](docs/handoff/archive/2026-09-23-pre-workflow-v1.md) 原样保留基线 handoff 的全部内容，含旧 SHA、CI、命令、批准来源和风险。
  **保存快照不是新里程碑验收或归档签认**；其中相互冲突的旧状态不再用于开工，当前风险仍维护于下文。
- [I2 离线范围归档](docs/handoff/archive/2026-09-20-I2-bounded-conversation.md)。


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

M7 的 Web/飞书薄渠道 PR 1–8、跨渠道一致性、离线验证证据与真实渠道重入硬门已归档至
[docs/handoff/archive/2026-09-09-M7-web-feishu-offline.md](docs/handoff/archive/2026-09-09-M7-web-feishu-offline.md)。

## 5. 下一步顺序

1. 通用流程治理已由 PR #70 合入当前 main；后续任务按 AGENTS 的分档、授权复用与证据规则执行，不再把它写成待集成候选。
2. 产品下一步是完成 W2-B1 的独立代码复审与合入；之后从最新 main 开始 W2-B2。W2 不提前建设 W3 或后续范围。
3. I3 延期但未取消，恢复前仍需明确资料源形态（仓库内文档 / 独立 store / 外部系统），与 Web 产品线不并行修改同一真源。
4. RI2/RI3/RI4/RI6、W4c、R1、M8/M9 等分别满足自己的进入门；流程实施不自动开放它们。

## 6. 仍需拍板或补证的事项

- M8 审批主体/渠道/有效期、拒绝/过期/冲突恢复语义与 ADR-005；生产写独立授权与验收计划。
- RI3 首次 GO、供应商保留/训练/区域条款与 synthetic corpus；M6b 现场输入与证据处置、现场 GO。
- W5 发布环境、边缘限流、旧静态身份显式迁移与核验、终态激活申请受控 PII 保留/清理。
  1024 上限只限制活跃申请，不能据此取消终态清理门。
- 分支保护的可用条件与实施；当前不能将 CI 状态视为 GitHub 强制合并限制。
- ADR-001 至 ADR-006 的待决项目按各自首次承重阶段处理；I3 资料源形态尚未决。

安全默认值仍为单租户开发、只读、fake、无真实生产连接或模型调用、无 LangGraph/向量数据库与全环境无 E1；明确批准的离线基础设施例外按 ADR-007 执行。

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

## 8. 本次流程治理证据

本轮执行记录与验证结果放在[实施计划](docs/superpowers/plans/2026-09-23-unified-development-workflow.md#执行记录)；
只证明文档/文档契约调整，不作为产品运行、真实接入或部署证据。生效提交由实际合入记录提供，文件不自写自己的 SHA。

## 9. 未覆盖的边界

- W2-B1 只完成本机 headless Chrome 静态视觉核对；真机浏览器交互、RI5 首启/改密/配置保存、正式部署、canary 与产品用户验收仍未取得。ASGI、HTML/CSS、截图或 CI smoke 不替代这些证据。
- RI3 离线 corpus 不证明真实模型质量、配额、延迟、usage、远端取消或 preview 稳定性；没有真实 Gemini key/网络证据。
- 飞书真实 OAuth、长连接、消息、成员分页、身份和测试企业权限仍待 RI2；窄屏详情与完整正式用户路径尚无真实浏览器人工验收。
- M6b 无真实 StarRocks 连接、credential reference readback、physical identity、DDL/grants、字段与销毁证据，不能标记 test-env verified。
- 本机各轮 DSN、Docker/Compose 能力、端口占用与 credential helper 状态只描述当时环境，本轮未重新运行容器；恢复验证时重新预检。历史 CI 不替代当前版本或长期运行。
- `EVIDENCE_FIELD_ABSENT` 尚无实际能力消费；审批恢复后真正执行 E1 未开放；StarRocks 方言只经锁定 sqlglot 验证，未与真实服务端核对。
- I2 保留的存量：迁移不可变哈希覆盖尚非全量；`_resolve` / `_prepare` / “clarification parent is missing” 三处拒绝码属于后续独立设计。
- Multi-Agent 仍只有独立准入文档，M9 只评估 Runner；本次流程实现不改变此产品权限。

## 10. 残余风险

- **Interaction artifact 迁移保留 V1 历史行但新路径只解释 V2**：这避免旧 accepted intent 被当作
  新 interaction 执行，但也意味着升级后的旧任务不会自动补成完整 I1 interaction 语义。未来若要展示或
  迁移旧 V1 artifact，必须单独设计只读兼容/回填路径，不能让 Runtime loader 放宽到 V1。
- **RI3 精简设计已获批准但仍依赖 preview 模型**：模型或 SDK 可能在首次真实调用前漂移；公司项目的数据
  保留/训练/区域条款尚未现场确认；协程取消不能证明远端停止；共享脱敏规则不能识别所有业务敏感
  信息；本轮文本与澄清槽位仍须符合获批的数据范围。provider 收到请求但本地保存前崩溃时可能重复
  调用，且该次 usage 可能无法完整落库，这是当前明确接受的体验优先取舍。`WorkerLoop` 单进程逐任务
  执行，真实吞吐和排队尚无证据；需要扩 worker 时必须单独设计 provider 限流与运行验证，不能现场
  workaround 后冒充已验收。上述任一现场门未闭合时 model override 必须保持关闭。

- **I1 已替代 RI3 通用父链**：按 ADR-017，分类请求只含 `user_text` 与可选 `clarification`；
  Runtime 从 `ClarificationRecord` 构造澄清上下文，不把已保存 advisory 作为历史重新送入模型。
  旧父链专属的 20 轮矩阵要求仅留在历史快照，不再作为当前 PR 3E 的验收路径。首次真实调用仍须
  核对当前契约的恶意输入/澄清边界，并满足现场 GO、供应商条款与 synthetic corpus 门；本次无真实调用证据。

- **M7 PR 4 虽已合入，但只验证了锁定 wheel 的源码形状与 fake SDK 对象**：尚未验证测试企业的真实长连接握手、
  回调对象、应用权限、成员可见性与分页返回形状；这些只能在 RI2 现场门齐备后先取得
  `test-env verified`，部署后再独立 canary。W1b 已改为数据库目录按请求解析身份，撤权不依赖重启；这一源码事实不替代真实飞书证据。同步 SDK 线程的优雅停止与进程崩溃恢复
  已有 PR 8 的静态进程契约、默认关闭入口检查及一次 GitHub CI Compose 运行证据，但尚无 RI1
  本机容器运行或真实 provider 证据。
- **RI1 已实现默认关闭的真实 OAuth adapter 与 Web composition root，但只经过离线 transport/路由
  契约**：授权 URL、code exchange、错误与超时分支没有与真实飞书交互；provider 总预算固定 5 秒，
  WebAuth 外层 watchdog 固定 6 秒，第二次 credential 文件读取也计入 provider 总预算。同步文件读取
  本身不能被 asyncio 强制中断，但它晚返回时不会再启动 HTTP。默认双开关、配置校验、state/session
  摘要与 1024 个全局 pending state 上限继续 fail-closed；该上限只防数据库无界增长，匿名方可在一个
  TTL 内填满额度并持续补位，使所有正常登录返回 503。RI2/RI6 在真实激活前必须取得 SSO 边缘限流、
  监控/告警和运行反证，不能把容量测试当作滥用防护。过期 state/session 的后台清理仍未实现。
- **凭据文件的宿主权限边界仍需现场核对**：旧 RI1 单行 `secret_file` reader 已不用于 Provider JSON；
  当前 `integration_config_file.read_integration_config()` 检查绝对路径、最终分量 `O_NOFOLLOW`、
  普通文件、大小和 schema，写入为 `0600` 原子替换，但读取不核验 owner/mode 或中间目录 symlink。
  RI2/RI3 真实接入前仍需结合实际容器挂载与宿主权限验收，W4a/W5 的迁移与目录预检不能由本次文档修改推定完成。
- **M7 PR 5 的消息发送只经过 fake SDK 对象**：锁定 SDK 的 async 方法在首次 token cache miss 时仍会
  进入其内部同步 token 获取路径；当前把 SDK timeout 配置为 5 秒，但离线测试不能证明测试企业的真实
  延迟、限流、消息幂等或事件循环影响，须在 RI2 先做测试环境验证，正式部署后再由 canary 测量。
- **飞书外部可见投递只有 at-least-once**：发送成功但订阅提交前崩溃时仍可能重试；稳定目的地、
  payload digest、创建消息 UUID 与 fencing 降低重复风险，但不能宣称 exactly-once。
- **RI1 Task 4 的 Compose 变更尚未在本机执行**：YAML 闭集、canonical smoke 脚本单测、静态
  Compose 5.5.1 merge 与默认关闭入口契约继续承重；补修实现基线的 PR #31 run `34555013826`
  已在 GitHub 隔离 runner 实跑容器 smoke（含本地 OAuth start 正反例），但单次 CI 不能外推到本机、
  其他 Docker/Compose 版本或长期运行。
  测试企业运行、正式部署与 canary 仍是后续独立证据门。
- **RI1 smoke 的输入清理不对抗主动恶意的同 UID 本机进程**：正常并发运行各自使用不可预测的
  `0700` 私有目录，清理先原子隔离并核对目录与四个已知文件的 inode，且不会递归删除未知内容；
  这能防止人工固定文件、正常并发 smoke 与其他 UID 进程被误删。同 UID 恶意进程本来就能观察并
  修改同一用户路径，仍可能制造清理失败或遗留；目录身份漂移和未知内容一律固定失败并保留现场。
- **M7 PR 3 的普通用户列表为避免建立“所在群任务”聚合权限，最多按 actor 扫描 100 条，再以一次
  scoped ChannelStore batch 排除群绑定**：群任务密集时一页可以为空但携带继续游标，Web 客户端
  必须按游标继续而不能把空页误判为已到底；N+1 已消除，但这条查询尚无真实负载基准。
- **projection worker 已在读阶段之后、取得并发槽且解析目的地之后，以同 owner/token/fence 紧邻
  出站前续期 live claim，并检查全部 fencing 写回结果**；但 provider 成功后到数据库提交前，claim
  仍可能过期或进程仍可能崩溃，因此外部投递依然只有 at-least-once。当前只用 fake clock/SDK 与
  存储契约证明 loser 不覆盖 winner 且会记录 `claim_lost`，尚无真实飞书/数据库延迟、长期运行或
  进程崩溃压测。
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
- **`LocalStack` 的完整 Runtime 注解只在静态检查期可解析**：为防止仅导入 `local_stack` 就加载
  完整 Runtime/Runner/工具链，`XiaoweiRuntime` 必须留在 `TYPE_CHECKING` 下；因此无显式命名空间的
  `get_type_hints(LocalStack)` 会得到 `NameError`。仓库当前无该调用者，`TaskViewStack` 可正常内省；
  若未来需要内省完整栈，应设计显式类型命名空间或窄 Protocol，不得把执行导入搬回顶层。
- **PromQL 不是通用解析器验证**：M6a 只允许两个代码内固定模板并做参数重编译比对。未来新增模板、真实 Prometheus API 参数、代理路径、限流和响应形状都必须独立评审，不能把 fake 全绿当作服务端兼容。
- **本地 Prom metric recording 是有限 synthetic catalog**：装配时只生成默认 30 分钟窗口、中心时刻前后 120 分钟、两个固定告警样例的精确键。它服务短时本地/Compose smoke，不是长时间运行或非默认窗口的数据源；未命中会降级为 `INDETERMINATE`，不提供 fallback。
- **Prometheus 可答性与渲染把步骤 ID 当作版本化隐式契约**：当前通过 Evidence ID 的 `:s1`/`:s2` 后缀区分告警与指标。现有 plan/answerability/render 测试固定该形状；未来改步骤命名必须同步升级并复核四层，不能只改 compiler。
- **资产 fake 不是 CMDB 兼容证明**：本地 catalog 只有一个 synthetic 资产及三个精确别名；真实字段、分页、权限、重复数据和错误语义均未验证。不同 selector 目前各自形成预执行 fingerprint，不声称跨别名同一身份。
- **资产 Evidence 固定一组版本化隐式契约**：capability key、`s1`、field set、selector version 和九字段白名单必须与 planner/reflection/renderer 同步演化；完整回归能发现当前改名漂移，但不能替代未来升版评审。
- **资产 scope 安全测试缺同文件正向对照**：`tests/security/test_asset_scope_isolation.py` 当前只有跨 scope 负向断言；后续应补 in-scope 成功对照，避免 recording 全部 miss 时该文件自身空过。现有 unit 正向用例与安全负向用例组合仍能抓住已做的常量化变异。
- **资产 Evidence 六个常量副本缺 pairing 真源**：后续应增加 pairing 测试，或改为 Prometheus builder 的装配层传参模式；当前漂移方向是 fail-closed，但会造成难定位的功能故障。
- **`asset_id` 的 ASCII 边界待真实契约决定**：当前 Unicode NFC + 精确匹配不会造成越权，但可能出现视觉同形却查询 miss；未来真实资产 adapter 必须单独立项并取得权威契约后再决定，不属于只负责 StarRocks 真实只读的 M6b。
- **资产歧义不要求补槽是有意语义**：精确 selector 已由用户给出，两行结果没有可补槽位，因此 `needs_user_input=False`；Prometheus 多告警还能用 fingerprint 消歧，故为 `True`。不要把两者机械统一。
