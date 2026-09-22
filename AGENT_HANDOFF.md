# 小维 Agent 2.0 当前交接

> 这是当前有效口径，不是按日期堆叠的变更流水。历史变更由 Git 提交承载；详细复盘放到 `docs/handoff/archive/`。完整的命令、exit code 和逐步输出放在里程碑验收报告中，不写入本文件。

## 0. 当前事实：W1b 切片 1 激活写内核已离线实现，入口尚未接线

- **当前基线**：`main@8e6845da29c6d382dc53843c790e7c4fcaf5b8d7`（PR #66 squash 合入）。
  本行是当前基线的**唯一**规范出处；本文件其余位置出现的 `main@` SHA 都是历史记录，
  不得用来回答『现在从哪开工』。第 1 节基线表必须引用本行这一个 SHA。

- **I2 限定领域普通对话已完成离线实施并归档**，归档基线
  `main@cc617f5e2d18c53b4a92528663edb1bd16aab557`，详见
  [I2 归档](docs/handoff/archive/2026-09-20-I2-bounded-conversation.md)。
  PR #53/#54/#55/#56 的受审 SHA、squash 提交、CI run、四门数字与全部隔离变异反证
  都在该归档文里，不在本文件重复。
- **Web 运维工作台总体设计已经 PR #58 squash 合入**
  `main@4e5a844620b700e25d6a29e43687c1a4c876db16`；W0 实施计划已经 PR #59 合入
  `main@b0fcf5c154d7bfa1be2a20bd56e55e19eea28aed`。两者都只是**文档**，不是实现。
- **负责人批准来源**：决策人 shixian66，日期 2026-09-20，永久链接
  <https://github.com/shixian66/xiaowei-agent/pull/59#issuecomment-5750387268>。
  该记录逐条批准 `ADR-007 D5`、`ADR-014 RI5 R1/R2/R3` 四项修订、R2 的风险接受，
  以及优先级从 I3 切换为 `W0 → W1a`。**合入事实本身不是批准来源**，引用时用这条链接。
- **W0 文档与 ADR 真源收口已由 PR #61 squash 合入**
  `main@a12578cd59cfaccf3fe6702e6437502b9462c28d`；受审 head 为
  `1cb2b4b8e0f79916888f7e2fcb70d7efae414dd4`，合入树与受审 head 文件树完全一致。
  该轮只改 10 份 Markdown 与 `tests/contract/test_doc_fact_binding.py`，
  **没有产品源码**、没有 migration、没有运行配置、没有部署、没有真实网络调用、
  没有用户验收。
- **W1a 详细计划已由 PR #62 合入**，文档在
  `docs/superpowers/plans/2026-09-21-w1a-identity-authz-admin-audit.md`；它按三个切片交付，
  依赖单向 A → B → C，各自独立复审与合入。
- **W1a 用户、权限与 Admin 审计写内核已离线实现。** 切片 A（契约、四张表与 `rev_0014`）以
  PR #63 squash 合入 `main@dd18def`；切片 B（内存 + PostgreSQL 两个 Store、共享行为套件、
  `seed_if_absent` 改道、三组机械守卫）以 PR #64 squash 合入
  `main@564707d13a8a8d148d1a2402786313bd31d561f9`；切片 C（公开的旧文档解析契约、一次性
  原子迁移命令、真源文档同步）以 PR #65 squash 合入 `main@f92aa88b9016b52c35ebce4052a8609fd574d8fb`，受审 head
  `ed8c9e82db46d75e47e4761d8897d0d2d23f9831`。切片 C 复审把 `subject_ref` 的三份长度上限收成
  `CONTROLLED_PII_MAX_LENGTH` 一处，并给 entry/command/context 的契约拒绝建了统一收敛点。
  该 head 的证据：四门在本机与 CI 同时全绿（离线 `4216 passed, 312 skipped`；
  security `1488 passed, 83 skipped`；`ruff check .`；`mypy src` 196 个源文件），
  CI run [`35681783752`](https://github.com/shixian66/xiaowei-agent/actions/runs/35681783752)
  八项全绿且每个 job 都有实际 step，其中真实 PostgreSQL 全量 `4528 passed`、零 skip。
- **写内核成立的两条不变量**：`UserDirectoryStore.apply()` 是 `user_accounts`、
  `user_role_assignments`、`external_identities` 与 `local_admins.user_id` 的唯一写入口；
  每一次授权改变都与一条由命令派生的审计事件同事务提交，审计写不进去则整个授权改变回滚。
- **证据等级到 `tests` 为止。** W1a 是离线写内核：没有激活流程、登录改造、Admin 页面、
  审计查询 API、真实飞书调用、部署或用户验收；W1a 的网络调用次数为 0。
- **W1b 详细计划已由 PR #66 squash 合入** `main@8e6845da29c6d382dc53843c790e7c4fcaf5b8d7`；
  负责人随后明确授权按计划开发。详细计划见
  `docs/superpowers/plans/2026-09-22-w1b-identity-activation-and-notification.md`，
  其两个切片依赖固定为“切片 1 写内核 → 切片 2 入口接线与通知”。该轮复审同时暴露一处真源冲突：
  登录 context 表 `web_oauth_login_contexts` 在详细规格 §17.1 属于
  W1b，在 `DEVELOPMENT_PLAN.md:182` 属于 W2。已按 `DEVELOPMENT_PLAN.md` 收敛，并在规格 §17.1
  留下署名修订（同一处把私聊 Admin 通知与可恢复投递重试移到 W3）。负责人已在 W1b 计划收口轮
  确认该阶段归属；`tests/contract/test_doc_fact_binding.py` 新增守卫使这处归属不能再静默分叉。
- **W1b 切片 1 已在分支 `claude/w1b-activation-core` 离线实现，尚待复审与合入。** 实现提交
  `d6c53d8` 新增 `ActivationRequest` / `ActivationStore`、`activation_requests` / `rev_0015`，
  并把批准/拒绝接入 `UserDirectoryStore.apply()` 的同事务目录与审计写路径。当前仍**没有**把
  Web/OAuth 或飞书 listener 接到激活服务，也没有群通知；这些只属于切片 2。
- **切片 1 当前证据等级为 `tests`。** 本机离线全量 `4289 passed, 346 skipped`；security
  `1497 passed, 83 skipped`；`ruff check .` 与 `mypy src`（199 个源文件）通过；一次性 PostgreSQL
  16.15 容器内 `tests/integration` 为 `348 passed`、零 skip。容量 advisory lock、fake 激活快照恢复、
  激活动作单阶段审计、待办 partial unique index 四项保护均做了隔离变异并按预期转红后还原。
  PR #67 首轮 CI run `35705507266` 精确绑定 `f88933dd9be084056a24a9f71c91bf0501e1f774`，
  八项全绿且均有真实 steps（tests/integration/compose-smoke/security-gate/types/lint/secret-scan 为
  8/10/8/8/8/8/8 steps，deps-audit 为 9 steps）。这些仍不是部署、真实飞书、canary 或用户验收证据。
- 计划中的 1024 上限只约束活跃待办；终态申请的受控 PII 保留/清理是 W5 部署硬门，未完成前
  不得部署启用。
- **I3 受治理资料查询为延期路线，未取消。** 它仍卡在同一个未拍板前提上：受治理
  资料的源是什么形态（仓库内文档 / 独立 store / 外部系统）。恢复 I3 时另起计划，
  不与 Web 产品线并行修改同一真源。
- I0-DOC 目标已完成并绑定 [ADR-017](docs/adr/ADR-017-intelligent-interaction-and-clarification.md)：
  智能交互入口、终态澄清、`ReadClass` 与执行披露边界已写入项目真相文档。
- `main` 已包含 I1-A Task 1.1–1.3 的 interaction classifier、insert-once
  interaction artifact、确定性 Router 与 Runtime 接入切片：
  `InteractionClassifierPort.classify()` 替代旧 `IntentModelPort.generate_intent()`；`application/model_interaction.py`
  负责单次分类预算、规则 fallback、input/result digest 与 artifact identity 复验；`ModelArtifactStore`
  改为 `load_interaction/save_interaction` 与 `load_advisory/save_advisory` 四个窄方法；Gemini adapter
  只返回 `InteractionModelResult`；Runtime 在 Resolver 前读写 `AcceptedInteractionArtifact` 并经
  `route_interaction()` 分流。
- `main` 已包含 I1-A Task 1.4：新增 grant-fenced、insert-once `ClarificationRecordStore`
  与 `task_clarification_records` 表；Runtime 在模型 `UNKNOWN` route clarify 时先保存
  `ClarificationRecord`，再收成 `CLARIFICATION_REQUIRED`；TaskView/Web/飞书从持久澄清事实投影，
  缺记录 fail-closed 为闭集 `clarification.integrity_error`。规则 fallback 的 `UNKNOWN` 仍保持旧
  preplan `REJECTED` 边界，避免把明确不支持的写/未知能力改成澄清。
- `main` 已包含 I1-A Task 1.5（PR #48）：`TaskSubmission` 使用
  `clarification_parent_task_id` 替代通用 `parent_task_id`；普通 `create_task()` 拒绝携带澄清父任务；
  `TaskStore.create_clarification_child()` 在同一 store/事务内校验 `CLARIFICATION_REQUIRED` 父任务、
  scope/actor 与 authenticated owner，并用非空 `clarification_parent_task_id` 唯一约束表达一次性消费。
  Web 提交与详情 JSON 暴露新字段名；新增 `rev_0013_clarification_parent` 从旧列 rename 到新列并加
  nullable unique 约束。同 key 重试返回同一个 child，不同 key 重复消费按 not-found 语义拒绝。
- Task 1.5 复审补修：`rev_0013` downgrade 现在复用既有 destructive authorization，显式授权时才清除
  I1-only 父引用并允许降级；TaskStore 共享套件覆盖父 tenant/environment/actor 与 authenticated owner
  漂移；Web 只在 `CLARIFICATION_REQUIRED` 详情/列表上下文展示“继续这个任务”。
- `rev_0011_interaction_clarification` 已把 `task_accepted_intents` 物理表重命名为
  `task_interaction_artifacts`，新写入 artifact version 2；旧 V1 行保留但 Postgres loader 只解释 V2，
  防止旧 accepted intent 被新运行路径误执行。复审打回后已补：0011 downgrade 对 V2 interaction rows
  复用 `require_destructive_authorization`，显式授权时先删除 V2 再恢复旧 CHECK；Postgres
  `save_interaction()` 只在当前 grant/状态复验通过后把同 task 的 legacy V1 行替换为 V2。
- PR #47 复审指出 0011 已随 PR #46 发布，不能再原地追加澄清表；`main` 已把 0011 恢复为
  `origin/main@befaa6b` 的历史快照，并新增 `rev_0012_clarification_records` 承载
  `task_clarification_records` 建表与自身 downgrade 守卫，避免已升级到 0011 的库在 upgrade head 时
  静默 no-op。
- 复审打回的 CI head 漂移与 Gemini seam 测试缺口已按根因处理：migration path 测试的当前 head
  断言改由 Alembic `ScriptDirectory` 提供，并新增契约测试防止最新 revision 与 Alembic head 再分叉；
  Gemini SDK seam 恢复真实 SDK schema/no-AFC/no-network、close timeout、close error 不遮蔽主错误、
  外层 cancellation 传播、usage metadata 与 API error 安全归一保护。隔离变异已证明移除 V2 loader 过滤、
  调大 Gemini close timeout 会使新增测试变红。当前证据仍为本机 `tests` 与本机 PostgreSQL integration，
  不是 CI、部署、canary 或用户验收。
- 本分支已补同根护栏：ClarificationRecordStore 内存/PostgreSQL 共享套件、schema/migration
  离线一致性、已发布 migration 源码哈希、0012 downgrade 授权守卫、崩溃恢复不重放模型/网关、
  row mapping JSONB 往返、Protocol conformance、只读进程 module surface、通道 parity 与安全 marker gate。
- `main` 已包含 I1-B（PR #49）：新增类型化 `CapabilityInputBinding`，三个现有 capability 均通过
  `SlotVerifier`/可信槽位升级已完成离线实现；Planner 只接收专属 Params，不再接收原始模型 slots；
  capability 级澄清父记录会在 Resolver/SlotVerifier 前绑定并校验 capability/version/operation/input schema。
- `main` 已包含 I1-C（PR #50）：`ReadClass` 闭集为 `BOUNDED` / `RESTRICTED`；`OperationSpec.read_class` 是唯一真源，
  `build_plan_step()` 派生 `PlanStep.read_class`，`PLAN_SCHEMA_VERSION = 2` 且 `plan_hash` 覆盖
  `read_class`；StepAdmission 在 Gateway 前重新从 `CapabilitySnapshot` 派生并交给 ToolPolicy 判定。
  当前三个生产只读 operation 均声明为 `BOUNDED`，I1-C 不新增受限读取额外确认。
- PR #50 复审补修：Postgres 读取持久化 plan 时会在构造 `ExecutionPlan` 前拒绝 V1/raw 旧 schema，
  返回闭集 `plan.schema_version_unsupported`，Runtime 将其收成任务级 `REJECTED` 而不是 worker 系统失败；
  Runtime 在已编译 plan 聚合出 `RESTRICTED` read 时于 Runner/Admission/Gateway 前拒绝，原因保持
  `policy.read_class_not_allowed`。本机最终证据：`python -m pytest -q` 3862 passed / 265 skipped；
  `python -m pytest -m security -q` 1411 passed / 83 skipped；`ruff check .` 与 `mypy src` 通过。
- `main` 已包含 I1-D（PR #51）：ExecutionDisclosure 执行披露屏障已完成离线实现。该切片新增
  `ExecutionDisclosure` 合约、纯投影、Runner `PipelineStage.DISCLOSURE` 持久审计屏障、澄清父
  `confirmed_slots` 快照传递、TaskView/Web/API/飞书离线投影。Disclosure 只表示 Gateway 前计划语义披露事实与
  审计持久化，不表示渠道已送达、用户已读、审批通过、真实目标已联网、部署、canary 或用户验收。
- 当前分支补齐 I1-D Task 4.4/4.5 Eval/closure：新增版本化
  `tests/evals/fixtures/i1_interaction_cases.json`、I1 L0/L1 eval、PostgreSQL 生命周期 integration 用例、
  M7 跨渠道 rejected parity、compose smoke 与 I1 executable fixture 绑定，以及 external content /
  intent pollution / no-network 安全门。当前 shell 默认没有 `PYTEST_POSTGRES_DSN`，不带 DSN 的普通
  pytest 运行会 skip PostgreSQL integration；真实 PostgreSQL 证据仅来自下条记录的隔离临时容器。
- PR #52 复审核出 `l0_unsupported_runtime` 四条安全 eval 只被规则 fallback 拦截、与乱码控制串
  不可区分；本分支已把这四条改为文本承重的 runtime 用例：伪只读写走合成 E1 审批暂停、
  restricted SELECT 走 `policy.read_class_not_allowed`、日志/secret 输入走模型前置分类请求与 Router
  拒绝，并新增控制串差异断言。隔离变异已证明把 restricted SELECT 文本替成控制串会转红；恢复后回绿。
- PR #53 合入前本机证据：`python -m pytest -q` 为 3913 passed / 267 skipped；
  `python -m pytest -m security -q` 为 1439 passed / 83 skipped / 2658 deselected；
  `ruff check .` 通过；`mypy src` 对 189 个 source files 通过。另用隔离临时 PostgreSQL
  容器跑通新增 I1/M7 integration 2 passed，以及 migration/clarification/I1 深档 26 passed。
  本机 Docker CLI 没有 `docker compose` 子命令，standalone `docker-compose config` 通过。隔离变异已证明
  slow-query SlotVerifier 若改为信任模型 `draft.slots`，新增 slot-pollution 测试会红；恢复后回绿。
- `main` 已包含 I2-A（PR #53）：限定领域普通对话通道只把 `InteractionKind.CONVERSATION` 路由为
  `RoutingDisposition.RESPOND`，Runtime 在无 Plan、无 Evidence、无 Gateway、无 Approval、无
  ExecutionDisclosure 的情况下按既有 TaskStore 状态机完成 `SUCCEEDED`。`knowledge_lookup` 与
  `log_analysis` 仍保持 `interaction.route_not_available`，分别留给 I3/I4。
- `main` 已包含 PR #54：带 `clarification_parent_task_id` 的子任务被模型分类为 `CONVERSATION` 时
  收成 `REJECTED / interaction.clarification_subject_incompatible`，且该拒绝在 trace 上归因到
  `INTENT/REJECTED`（不是记成 OK 的静默拒绝）。父链一次性消费仍是 TaskStore 既有语义。
- `main` 已包含 I2-B（PR #55）：普通对话的回答不再是一句固定文案，而是当前
  `CapabilitySnapshot` 的确定性投影——逐条列出已注册能力、操作、`read_class`、`effect_class` 与
  所经 gateway，`refs` 带 `capability-snapshot:<id>` 与 `capability:<id>@<version>`。回答只由快照
  决定，投影函数签名里没有用户文本入参；投影的是**当前**快照而不是任务创建时的快照，`snapshot_id`
  落在 refs 里所以快照演进可见。同时给 `docs/CAPABILITIES.md` 的生成器补上此前缺的 `read_class`
  列，并去掉 TaskView 里那次读了却不参与投影的 `get_submission`。仍不调用工具、不访问外部系统、
  不读取历史、不保存长期记忆。
- I2 收口卫生（PR #56）已合入：Router 的闭集拒绝码落到任务 `terminal_reason`（澄清仍保持
  `None`，其原因只属于 `ClarificationRecord`）；普通对话终态补发 `REFLECTION/OK` 并与终态迁移
  同一条命令提交，至此**每条终态任务恰好一条 REFLECTION**；补齐真实 PostgreSQL 对话集成
  （含崩溃恢复）、四渠道 parity（含飞书 `truncated` 断言）与 TaskView 投影守卫负向用例。
- 已知未纳入 I2 收口的存量：迁移不可变哈希登记 1/13 属 M/RI 系列；`_resolve`/`_prepare`/
  "clarification parent is missing" 三处拒绝仍无闭集码——给它们发码属于新增设计，不是卫生。
- I3-I5、RI2/RI3 真实现场 GO、RI4/RI5/RI6、M8 与 M9 仍未实现。
- I2 不读取真实 Gemini key，不调用真实飞书、Gemini、StarRocks 或任何运维目标，不部署、不 canary，
  不改变 RI2/RI3/RI4/H 层生产只读、RI6 或 E1 的独立 GO 门。当前证据等级仍为 `tests`。
- I2-B 的"能力目录"只是把 Registry 声明重排给用户看，**不表示这些能力已经连接真实系统**：三条
  能力的证据等级都仍是 `tests`，均未连接对应真实运维系统。

## 1. 当前基线

| 项目 | 当前值 |
| --- | --- |
| 项目目录 | 当前开发分支 `claude/w1b-activation-core`（切片 1 实现待复审），基线 `main@8e6845da29c6d382dc53843c790e7c4fcaf5b8d7`。不记录机器专属 worktree 路径——它对下一位实现者没有意义且必然过期 |
| 截止时间 | 2026-09-22（Asia/Shanghai） |
| 阶段 | **W1a 三个切片已全部合入；W1b 计划（PR #66）已批准并合入。W1b 切片 1 激活存储/审批写内核已离线实现，正在等待复审与合入；Web/OAuth、飞书入口和通知尚未接线，必须留给依赖它的切片 2。I3 延期未取消。真实模型/飞书/StarRocks、部署、canary 与用户验收仍未开放。最强证据仍为 `tests`。** |
| 总体计划 | [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md) Approved V2.5（2026-09-20 增加 `W0 → W5` 序列与优先级切换）；RI3 V7.1/ADR-015 已批准按 5 个 PR 顺序离线实现，真实调用现场 GO 仍未下达 |
| M0 验收状态 | **已通过**，验收对象 `a1a8c888010abb8bbe1af28d792e760e3b229e5d` |
| 文档是否已入 `main` | **是**——I0-DOC 已以 squash commit `033f3c60be273e5e99d0f371020f123b85692e06` 合入 `main` |
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
| M7 状态 | **离线范围已验收并归档**。PR #20–#27 已合入最终代码/测试基线 `ba5ecfe5edffb408e20c4bb9cbf494bfbb035b82`；PR #28 以 `a5c88cbc285658f7f38355a9470bfdd725858ba5` 合入离线完成交接。归档见 [docs/handoff/archive/2026-09-09-M7-web-feishu-offline.md](docs/handoff/archive/2026-09-09-M7-web-feishu-offline.md)，验收报告见 [docs/handoff/M7-acceptance-report.md](docs/handoff/M7-acceptance-report.md)。证据等级仍为 `tests`，真实渠道退出门未通过 |
| RI1 离线交付 | 起点 `66864253e0c9c9a0c6ebda1eec3a25b8e06a8df2`；Task 1：`040daf7c7e22e278eef0496f3e69ee380077245c`、`b117493000895661f949ba14edf159512b4c9454`、`4db90a5982549837630c90388fe4fc89a7046cd4`；Task 2：`390720dfc6d9c16b6d49f9fa61288a4f2d592eca`、`36f010d0d9db40713952f50cbcb9662151a91833`；Task 3：`2fa0177ee33b6135015303344f791287f485280d`、`33fcbbc16d5a329877a6481d89d2e3cb64d6fa32`、`f1b03557dd62b4fbe070385529062330a39affb1`；跨任务审查补修：`5428cea403599e41414f7445644ffd0afcfb1013`；Task 4：`a85e9b9eae7f3f6ed3ac7d08e1f3595348235a50`、`3c53b4aa7254adc00c3473ee41354f67152bd4be`、`0f095774f32bd31cb0e78f0532b39abd08301a27`、`18a0c78a72696982be9d298b7c01e7ae1b46d4cc`、`e678c673cb1594fc97f9a14ce197b4b11c47163f`、`e1f1b45def5537d94d3503ef4d60b450af5266db`、`167af47352e42e025fce84753bc34160c1064944`；首个全分支受审与 CI head：`d4f48e8800a033bf01560d006c984cfbde514b5f`；提交前终审 head：`eb26975a1e07424d69b978f532da3c304d5f260d`；根因补修实现基线：`2d67b59e128c1c226fb062708edf10c9251a4b3e`。交付 PR：[#31](https://github.com/shixian66/xiaowei-agent/pull/31) |
| RI3 规划 | 基于 `origin/main@ab35b756b07aa1baa2f0eb3ac98dba24999c40b1`；[ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md) 与 [RI3 详细计划](docs/superpowers/plans/2026-09-10-model-provider-adapter.md) V7.1 已获批准。该行只记录规划批准，不是源码或运行证据 |
| RI3 PR 3B 合入 | 实现基线 `b31f7c464c75f0c3191060fceb040e61cb54131c`，最终受审 head `79675aeed895899282e6f4f697463f8b6b27e29b`；PR [#34](https://github.com/shixian66/xiaowei-agent/pull/34) 的 run [`34703681866`](https://github.com/shixian66/xiaowei-agent/actions/runs/34703681866) 精确绑定该 head，八项全绿；项目负责人批准后以 squash commit `8374dc4255e88949cf5e638371b393bb01f6ed1c` 合入 `main` |
| RI3 PR 3C 合入 | 基于 `main@2d60dd9ed3d9e01a0089613c9a69384d5c8f1acc`；最终受审 head `bc1812e72dada63a83776bfb159a04c970153c66`。PR [#36](https://github.com/shixian66/xiaowei-agent/pull/36) 的 run [`34730018472`](https://github.com/shixian66/xiaowei-agent/actions/runs/34730018472) 精确绑定该 head，八项全绿；项目负责人批准后以 squash commit `2d40f7040dae6488ef983589c5655b6c330c529d` 合入 `main`，且合入树与受审 head 无差异。交付 grant-fenced insert-once intent/advisory、`rev_0008_model_artifacts`、entry-scoped heartbeat、MODEL trace、surface-derived 慢查询投影与终态 readback，证据等级仍为 `tests` |
| RI3 PR 3D 合入 | 基于 `main@c06ea393c814f461ed76ccd070a0804a9ab3cc8c`；最终受审 head `c2d82ede1035b5ca6f8c175c618d4d6837196ce3`。PR [#38](https://github.com/shixian66/xiaowei-agent/pull/38) 的 run [`34736983148`](https://github.com/shixian66/xiaowei-agent/actions/runs/34736983148) 精确绑定该 head，八项全绿；项目负责人批准后以 squash commit `fb6718cc6f295206c7c125a887b34d2ee762e71f` 合入 `main`，且合入树与受审 head 无差异。交付统一 `TaskId` 域、直接 parent 的 Web 预检、worker 全链复核、脱敏后历史预算复核、确定性投影损坏拒绝、`rev_0009_task_parent_context` 与显式 Web 继续交互；旧的无父任务 hash 字节保持不变，父任务不改变幂等 scope。证据等级仍为 `tests` |
| RI5 设计与 ADR 接受 | 受审对象 `abb00a1bc13552e1dae5a0dfc9e8b5b4b9a6ae7f`，状态翻转 `28f193200651ea4df4925b1481b971687f1003a5`；PR [#40](https://github.com/shixian66/xiaowei-agent/pull/40) 以 fast-forward 合入 `main`（`mergeCommit` 与 `28f1932` 同一提交，无合并提交，11 个受审提交原样保留）。项目负责人于 2026-09-14 成套接受 ADR-007 §RI5 Amendment、ADR-014/ADR-015 §RI5 修订与总体 spec 的 RI5 修订，以及 RI5 简化设计、ARCHITECTURE 与 DEVELOPMENT_PLAN 的同步。**该接受不授予 RI3 PR 3E 与 RI2 的真实调用 GO** |
| RI5 实现计划 | [docs/superpowers/plans/2026-09-14-ri5-local-web-admin.md](docs/superpowers/plans/2026-09-14-ri5-local-web-admin.md)，Task 0–9 共 10 个。计划文档内含逐 Task 执行记录：每一处偏离计划的自主判断、反证清单（逐条改坏源码验证测试变红后恢复），以及三条**没有变红**的反证与原因 |
| RI5 实现基线 | 分支 `claude/ri5-implementation`，PR [#42](https://github.com/shixian66/xiaowei-agent/pull/42)，基于 `origin/main@c9b3cae898f7090d3f29c9fc64b7a9b551a77c55`，包含 Task 0–9 与 PR CI 暴露的 Alembic revision 长度、smoke 飞书 app_id/enablement 夹具漂移、listener fake transport 凭据旁路、smoke 配置目录容器可遍历性补修。证据等级 **`tests`**：`python -m pytest -q` 3787 passed / 237 skipped；`-m security` 1402 passed / 80 skipped；`ruff check .` 通过；`mypy src` 175 个源文件通过。PR CI 与合入状态请以 GitHub 实时状态为准 |
| 下一步 | 先按精确 SHA 复审并合入 **W1b 切片 1**；只有切片 1 合入最新 `main` 后，才从该基线实现切片 2 的 Web/OAuth、飞书入口与单次群通知。I3 受治理资料查询延期但未取消，恢复时仍需先拍板资料源形态（仓库内文档 / 独立 store / 外部系统）。真实 Gemini/飞书/StarRocks、部署、canary、UAT、RI3 test-env GO 与 E1 仍保持各自独立硬门 |
| 本机工具链 | Python **3.11.16**（uv 独立分发）；项目依赖由 `uv.lock` 锁定，`uv sync --extra dev --frozen` 后在 `.venv` 中可原样执行 ADR-008 四条命令 |
| 运行状态 | `main` 已包含 I0-DOC、I1-A Task 1.1–1.5、I1-B typed SlotVerifier/可信槽位升级、I1-C ReadClass/Plan schema V2、I1-D ExecutionDisclosure、I1-D Eval/closure（PR #52）与完整 I2（PR #53/#54/#55/#56，已归档）。仍未连接任何真实运维目标或模型服务 |
| 生产状态 | 未部署、未 canary、未用户验收 |
| 能力闭环 | `starrocks.slow_query.diagnose`、`prometheus.alert.evidence`、`asset.inventory.lookup` 均已完成 fake/recording 闭环，证据等级均为 `tests`；三者均未连接对应真实运维系统 |

旧项目 `ivor_aiops` 只提供历史边界和问题样本。本项目不把旧项目的分支、SHA、能力地图、线上状态或遗留待办当作自身事实。

## 2. 已确认的设计口径

已固化到 [ARCHITECTURE.md](ARCHITECTURE.md) 与 `docs/adr/`。

> **本节是快照摘要，不是规范真源。** 授权边界（真实调用、E1、写权限、租户上下文）的真源是 [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)；工程与测试工具链的真源是 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)；架构契约与安全语义的真源是 `ARCHITECTURE.md`；里程碑与验收门的真源是 `DEVELOPMENT_PLAN.md`。上述真源与本节冲突时，一律以真源为准，并回头修正本节。

- 采用模块化单体 + Docker Compose 的目标部署形态；控制面与数据面分离；PostgreSQL 作为 TaskStore、审批和审计事实真源。
- `XiaoweiRuntime` 是新的应用编排入口；不复用旧项目 `XiaoweiEngine` 的代码。
- LLM 只产结构化理解、解释和建议；不能直接选工具、目标、SQL、审批或执行。
- **RI3 已批准设计**：只接固定的 Google Gemini Developer API `v1beta`、canonical origin
  `https://generativelanguage.googleapis.com` 和 `gemini-3-flash-preview`，使用官方
  `google-genai==2.23.0` 的异步 `models.generate_content` structured-output 接口；实现 PR 必须独立
  审计精确 wheel，身份或元数据不匹配就停下复审。
  不建通用 provider registry，不启用 provider chat/session、tools、function calling、搜索、代码执行、
  文件或 MCP。意图与慢查询解释走两个窄 port。
- **RI3 已批准的精简生命周期设计**：Gemini key 的唯一明文真源是宿主 Git-ignored 的
  `.config/integrations.json`（容器内 `/run/xiaowei-config/integrations.json`），由 Web 管理面
  写入、task worker 只读挂载；Provider 凭据不再走 Compose secret，`.env` 不保存 key 或路径。
  意图 low/60 秒/最多两次 request，解释 high/180 秒/一次 request，分别限制 2048/4000 output tokens；
  RI3 adds no whole-task deadline and preserves the current StarRocks 25-second
  query-timeout upper bound/30-second read-only policy cap; future 180/190/195/200-second layers belong
  to RI4. Heartbeat moves completely from Runner to one application helper: Worker
  covers `execute_task()`/retry scheduling and `handle()` only its post-grant
  compatibility attempt. Runner removes only the periodic interval/sleep state and
  `_heartbeat()`/`_run_with_heartbeat()`; it retains lease TTL and
  `_require_current_grant()` for start/resume's one-shot grant renewal.
  一次 migration 建 task 级 insert-once accepted intent/advisory；保存成功后 retry 复用。provider 收到
  请求但保存前崩溃时允许重复一次，这是模型无执行副作用、体验优先下的明确 at-least-once 取舍，
  不建 reservation/transport-attempt/统一终态平台。现有 `XiaoweiRuntime.handle()` 保留为离线测试入口。
  Only durable `execute_task()` calls the model/artifact store. Advisory is pre-terminal,
  so RUNNING may last 180 seconds longer; committed StarRocks steps recover from the
  journal without Gateway replay. Trace gains a typed MODEL stage.
  Model text is raw-size-bounded before total `scrub_text()`, then scrubbed history rounds
  and their aggregate are rechecked against the same character budgets before the final
  request cap; there is no redaction-failure branch. Parent history is explicit Web-only, ownership-checked, limited to 20 complete
  tasks/64,000 characters, which itself guarantees at most 256,000 UTF-8 bytes
  (less than 256 KiB). Null-parent digest bytes stay frozen; non-null
  parent enters semantic request and stored submission digests, not the scope digest.
  Slow-query projection derives exact names/order from
  `SLOW_QUERY_SURFACE.allowed_columns`, takes at most 20 rows and rejects a bad batch;
  the projector belongs to `application/model_advisory.py`, while `rendering/` never
  imports the capability surface. The enable flag and secret are both worker-only.
  Feishu reply/thread context waits for RI2 evidence.
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
- **ADR-009/I1-C 固化 hash 与准入形状**：`plan_hash` 规范输入集含 `effect_class`、`read_class`、`condition` 与 `budget`，`PLAN_SCHEMA_VERSION = 2`；`ExecutionPlan` 绑定**单一 capability**，步骤不携带 capability 标识；两个指纹**不作为 `ExecutionPlan` 字段**，绑定值存于 `ApprovalRequest`（含 `policy_revision`）；`AdmissionCertificate` 同时绑定步骤身份与 `tool_call_hash`。详见 [ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md) 与 [ADR-017](docs/adr/ADR-017-intelligent-interaction-and-clarification.md)。
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
| [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) | 首批能力、初始执行上下文与真实调用许可 | Accepted Revision 2026-09-10；RI3 amendment Accepted 2026-09-12；H 层未签认 |
| [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md) | 工程与测试基线：Python 3.11、pytest、security marker gate、Ruff、mypy | Accepted 2026-09-01 |
| [ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md) | `plan_hash` 规范形状、审批绑定与工具准入 | Accepted 2026-09-02 |
| [ADR-010](docs/adr/ADR-010-m5-durable-attempt-and-compose-boundary.md) | M5 持久执行尝试、事务审计、readiness 与本地 Compose 边界 | Accepted 2026-09-05 |
| [ADR-011](docs/adr/ADR-011-m6a-capability-binding-and-promql-template-admission.md) | 多能力 binding、operation gateway 与固定 PromQL 模板准入 | Accepted 2026-09-05 |
| [ADR-012](docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md) | M6b 精确目标绑定、固定 preflight 与测试环境只读激活边界 | Accepted 2026-09-07 |
| [ADR-013](docs/adr/ADR-013-m7-channel-boundary.md) | M7 Web/飞书薄渠道、身份与安全投影边界 | Accepted 2026-09-08 |
| [ADR-014](docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md) | 真实飞书 OAuth 与 Web 激活安全契约 | Accepted 2026-09-10 |

**已接受决策**：[ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md) 固定 Gemini 窄端口、数据、
记忆、阶段 timeout、持久化与无执行权边界。接受 ADR 只使设计生效，不能当作源码已经实现。
首批三个能力：`starrocks.slow_query.diagnose`（M3）、`prometheus.alert.evidence`（M6a）、`asset.inventory.lookup`（M6a），均先只读 fake/recording。

**外部调用许可**（ADR-007 D4）：真实运维目标系统与真实模型 API 的**网络调用**在 M0-M6a 全程禁止；本地隔离 PostgreSQL/Compose 经 M4/M5 各自里程碑批准后允许；StarRocks 非生产只读需 M6b 单独授权。CI 不持有任何测试、生产或运维目标系统凭证；仅允许 GitHub 自动签发、仓库范围、短生命周期的临时 `GITHUB_TOKEN`（`contents: read` + `persist-credentials: false`）。CI 不执行真实运维目标、真实模型、非生产只读或任何 E1 调用；对基础设施的写权限按 ADR-007 D8 的里程碑时点逐级开放。

**模型边界**：实现 provider adapter 与调用真实模型 API 是两件事。RI3 的固定 Gemini 方向、数据
白名单、凭证来源和时限已经 ADR-015 固化，并于 2026-09-12 获离线开工授权。即使离线实现完成，
**实现权仍不等于调用权**；首次真实 Gemini 网络调用必须另获现场 GO，
且模型永远只产生不可信结构化草案或无执行权建议。

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

M7 的 Web/飞书薄渠道 PR 1–8、跨渠道一致性、离线验证证据与真实渠道重入硬门已归档至
[docs/handoff/archive/2026-09-09-M7-web-feishu-offline.md](docs/handoff/archive/2026-09-09-M7-web-feishu-offline.md)。

**归档规则**：里程碑验收通过后，其逐条提交历史移入 `docs/handoff/archive/`，本文件只保留里程碑基线 SHA、当前阶段、已验证事实、阻塞项、下一步和禁止盲改点，**不记录随合并漂移的 HEAD**，也不随里程碑增长。

## 5. 下一步顺序

> **当前唯一在途项**：W1b 切片 1 激活存储/审批写内核的精确 SHA 复审与合入。
> 切片 2 不得绕过该依赖先接 Web/OAuth 或飞书入口。
> 下面的历史清单只作为已完成里程碑索引，不代表当前待办。


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
12. ~~M7 PR 1–8 离线实现与范围验收归档~~ **已完成**：最终代码/测试基线为 `ba5ecfe5`，
    项目负责人于 2026-09-09 明确按离线范围验收并授权归档。该结论只关闭离线实施任务；真实
    OAuth/飞书真实使用、应用注册、凭据、网络、部署、canary 与产品用户验收仍保持独立硬门，M7 完整退出
    标准未通过。
13. **RI1 默认关闭的离线实现已通过 PR #31 交付，合并前审查问题已按根因补修**：Task 1–4 已实现
    真实 OAuth adapter、Web composition root、失败边界和同一 Compose profile 装配；补修把 OAuth、
    SDK 与 smoke 黑洞机械绑定到同一 provider origin，让 Compose 真实进程走完不联网的 OAuth start
    正反例，并闭合超时层级、credential reader 公共 API 和容量风险口径。没有读取或保存真实 secret，
    也没有发起飞书网络调用。精确实现 SHA 见 §1；交付不解锁 RI2、H 层生产只读、部署、canary、用户
    验收或 M8。
14. **RI3 Gemini 的离线 PR 3A–3D 已顺序合入，PR 3E 等待独立现场 GO**：项目负责人已批准 ADR-015/V7.1 计划并于
    2026-09-12 下达“开始 RI3”。已批准边界固定
    Google Gemini Developer API、`gemini-3-flash-preview`、官方 SDK、两个窄模型 port、凭据真源为
    `.config/integrations.json`（Web 为配置管理与管理员显式连接测试读取 Secret；两个窄模型调用 port
    仍只由 task worker 装配，Web 不获得）、意图 60 秒/解释 180 秒阶段 timeout、insert-once accepted intent/
    advisory、Web 显式父任务和逐列定型的 20 行 StarRocks 投影。计划由 9 PR/4 migration 收缩为
    5 PR/2 migration；不新增任务总 deadline、调用预约平台、统一终态表、进度状态机、全项目凭证
    重构或渠道聚合事务。Web parent 的离线契约不依赖尚未完成的 RI2 OAuth；飞书 reply/thread 上下文
    等 RI2 真实事件语义明确后再复用同一 assembler。PR 3B 已离线实现固定字段 provider slots、
    provider-neutral 错误/重试闭集、严格 usage 收窄、显式禁用 AFC 的真实 SDK seam、
    默认关闭 LocalStack 与 worker-only Compose override/smoke；依赖锁定精确 wheel。PR #34 已以
    `8374dc4255e88949cf5e638371b393bb01f6ed1c` 合入 `main`。PR 3C 已补齐 durable-only
    model/rule intent、grant-fenced insert-once artifact、单一 application heartbeat、MODEL trace、
    surface-derived 慢查询 advisory 及 TaskView readback；独立审查问题按根因补修后，PR #36 已以
    `2d40f7040dae6488ef983589c5655b6c330c529d` 合入 `main`。PR 3D 从
    `0c17e9defe305a7ae871376d04762d541f19d147` 起实现：父任务只可由 Web 用户显式选择，提交前只检查
    直接 parent 的终态、租户/环境/actor 与绑定所有权，worker 再基于持久记录逐跳复核并组装最多 20 轮、64,000 字符/
    256 KiB 的清洗历史；缺失 child binding 可重试，其余范围漂移 fail-closed。`parent_task_id=None`
    保持既有 canonical/hash 字节，父任务不进入 idempotency scope；`rev_0009` 只新增可空外键和索引。
    独立审查确认无阻断后提出的五项同路径问题已在 `c61f90d5bfd7f2615078de6c2915175c38d4ca64`
    根因修复；最终受审 head `c2d82ede1035b5ca6f8c175c618d4d6837196ce3` 的八项 CI 全绿，PR #38
    已以 `fb6718cc6f295206c7c125a887b34d2ee762e71f` 合入且树一致。
    全程没有读取真实 key、真实调用、部署或归档；真实 Gemini 调用仍需 PR 3E 的独立现场 GO。
    Independent review V7 fixes the current-timeout fact, parent idempotency predicate,
    Settings versus host Compose env boundary, complete storage/protocol/suite/SDK/trace
    test surfaces, durable-only intent path, single-render advisory flow, bounded
    redaction input, one-shot grant renewal and projector package ownership. PR 3A remains
    documentation, not implementation evidence.

## 6. 仍需拍板的事项

M7 的阶段门拆分已拍板，PR 1–8 离线范围已经完成、验收并归档；真实渠道激活权限没有放宽，
也未因离线归档自动进入下一阶段。

M7 的产品范围也已拍板：主工作台只适配桌面端；窄屏仅保证飞书链接单任务安全详情可读。
未来的 Admin 配置治理、审批/重跑和后续运维能力须另立里程碑与 ADR；真实模型 API 已单列 RI3，
其 ADR-015/详细计划已经批准。它们都不属于 M7，设计获批也不能被推导为已实现或已连接。

- 审批主体、审批渠道、审批有效期和拒绝/过期/冲突后的恢复语义（M8 前，ADR-005）。
- M8 受控 E1 的三项开放条件是否齐备：ADR-005 定稿、项目负责人批准、ADR-007 明确例外或修订。
- 生产写的独立授权与独立验收计划（不由 M8 推导）。
- RI3 首次 Gemini 网络调用的现场 GO、供应商数据保留/训练/区域政策和固定 synthetic corpus 仍须
  独立确认；离线开工口令不替代这些现场条件。
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

### W0 文档收口（本轮）

- 分支 `claude/w0-web-product-adr-truth-sync`，基线
  `origin/main@b0fcf5c154d7bfa1be2a20bd56e55e19eea28aed`（PR #59 的 squash 合并提交）。
- 开工基线四门（本地 Python 3.11.16 / pytest 9.1.1）：`3950 passed, 269 skipped`；
  security gate `1447 passed, 83 skipped, 2689 deselected`；Ruff 通过；mypy 189 个源文件通过。
- 改动范围：规格 1 份、ADR 4 份、`ARCHITECTURE.md`、`DEVELOPMENT_PLAN.md`、`README.md`、
  `AGENT_HANDOFF.md`、`tests/contract/test_doc_fact_binding.py`，共 10 个文件。
  **`src/`、migration、`pyproject.toml`、`uv.lock`、Compose 与运行配置零改动。**
- 证据等级：文档 + 离线契约测试。**没有**运行、部署、canary、真实外部调用或用户验收证据。

### 已验证

- **I1-A Task 1.2/1.3 interaction artifact/router 切片已在当前 worktree 通过本地四门，证据等级仍为
  `tests`**：本轮基线为 `origin/main@5532ba2be3ae58cb92ee32c6f9bcda7a5ff70116`，变更把旧
  model intent path 收束为 interaction classifier + artifact v2 + deterministic router。RED 证据：
  新增 `tests/unit/test_interaction_router.py` 初跑因缺模块失败；`tests/contract/test_model_artifact_store.py::test_model_artifact_store_has_only_four_narrow_methods`
  在旧 `load_intent/save_intent` 接口上失败。修复后 targeted tests 全绿。隔离变异证据：临时把
  `route_interaction()` 的环境不一致保护改坏后，`test_capability_route_refuses_environment_context_mismatch`
  按预期失败；恢复保护后同用例通过。最终本地四门：
  `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 /Users/kloenguyen/Desktop/agent/.venv/bin/python -m pytest -q`
  为 `3724 passed, 238 skipped, 5 warnings`；
  `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 /Users/kloenguyen/Desktop/agent/.venv/bin/python -m pytest -m security -q`
  为 `1399 passed, 81 skipped, 2482 deselected, 5 warnings`；
  `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 /Users/kloenguyen/Desktop/agent/.venv/bin/ruff check .`
  为 `All checks passed!`；
  `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 /Users/kloenguyen/Desktop/agent/.venv/bin/mypy src`
  为 `Success: no issues found in 179 source files`。

- **RI5 本地管理面已离线实现完毕，证据等级 `tests`**：`claude/ri5-implementation` 上 17 个实现提交
  交付 `.config/integrations.json` 单一明文真源、本地管理员登录与强制改密、`lan_http` / `https`
  两种 Web 模式、配置读写 API、加载回执与五态页面模型、三个控制面探针，以及 Compose/Dockerfile/
  前端面板/首启 runbook。四条基线全绿（数字见上表）。

  **每个 Task 都做了反证**：逐条把源码改坏、确认对应测试变红、再恢复复跑。累计 40 余条，全部记在
  实现计划文档的「执行记录」里。其中 **三条第一次没有变红**，都已如实记录并补测试或更正判断：
  空串拒绝实际由 `StrictStr` 在更低一层持有（行为契约成立，但持有层与原判断不同）；"配置面不再
  限定本地管理员"最初只测了匿名请求，补上一个持 `ADMIN_ALL_SAFE_TASKS` 的**已登录**飞书主体后
  才承重；`_ComposeLoader` 一度是谁都不需要的死代码，补 `test_every_compose_file_is_registered_and_parses`
  之后才真正承重。

  **两轮外部复审驳回，都按共同根因修复而非按评论位置打补丁**：`9c51a7c` 的三个 P1（真实进程入口
  仍强制飞书 OAuth、真实浏览器首启闭环拿不到 CSRF、`WebAuthService` 缺 `auth_source` 校验）；
  `069c937` 的一个 P1（加载回执用全局服务集合产出，任何一个进程启动都会替所有兄弟进程签字，页面
  会从"待应用"假跳到"待测试"）。后者的根因在**产出点**而不是写入点，修法是给
  `load_provider_credentials()` 加一个无默认值的 `service_name`，并由装配函数而非调用方填写。

### RI5 未验证项（本轮**没有**做）

- **真机本地闭环未实跑。** 实现计划 Task 9 Step 2 要求按 README 首启 runbook 跑一遍
  `compose up` 并在浏览器完成登录/改密/配置保存。本轮**没有执行**：它需要 build 镜像（拉依赖源）
  并启动一整套容器，属于联网与部署，超出本轮授权。等价断言已在 ASGI 层由契约测试覆盖——包括
  首启强制改密之前配置面与三个测试项全部 403 且不落任何一行——但**这不是真机证据**。
- **`python -m scripts.compose_smoke` 未执行**，同一原因。它自身的 136 条脚本测试与全部 Compose
  契约测试是绿的，`docker-compose config` 的静态渲染也已核对（LAN override 渲染出且仅出一条端口
  映射），但真机 smoke 未取得。
- **真实 Gemini 调用、真实飞书调用全部未做。** 两个真实测试开关全程 `false`，三个探针只被注入的
  spy 驱动过；两个真实 transport 只在被 monkeypatch 的情况下走过错误映射分支，没有构造过任何
  SDK client。RI3 PR 3E 与 RI2 的现场 GO 仍未下达。
- **未部署、未 canary、未用户验收。** TLS/Ingress/证书属 RI6，本轮不涉及。
- **PR #42 已打开并跑过 CI。** CI 暴露过五项仅在真机 CI 路径出现的阻断：Alembic revision ID
  超过默认 `alembic_version.version_num varchar(32)`；smoke 合成 `integrations.json` 的飞书
  `app_id` 与 OAuth URL 校验常量漂移；smoke 只启用 `XIAOWEI_FEISHU_OAUTH_ENABLED` 却未启用
  JSON Provider 消费层；以及 listener 在注入 fake transport 时未先取得 `app_id`，导致事件 scope
  校验用 `None` 拒绝合法事件；smoke 配置目录以 `0700` 绑定给非 root 容器，导致 Web 读不到
  `integrations.json`。已分别补 `test_revision_ids_fit_the_default_alembic_version_column`、
  `test_synthetic_feishu_config_uses_the_app_id_expected_by_oauth_smoke`、
  `test_synthetic_feishu_config_enables_the_oauth_smoke_provider` 与
  `test_feishu_listener_stack_requires_credentials_even_with_fake_transport`；配置目录可遍历性由
  `test_smoke_owns_and_cleans_all_generated_input_files` 承重。最终 CI 与合入状态以 GitHub
  实时状态为准。

- **RI3 PR 3D 已完成本地根因/边界验证、独立审查、CI 与合入**：从
  `0c17e9defe305a7ae871376d04762d541f19d147` 起新增显式 Web 父任务、持久化外键、两阶段访问复核、
  安全历史投影和 Web “继续这个任务”交互；没有从最近任务猜父任务，飞书没有获得隐式上下文。
  审查补修后的本地全量为 3531 passed / 227 skipped；security gate 为 1290 passed / 80 skipped /
  2388 deselected；Ruff 通过，mypy 164 个源文件通过；初始候选的 Web JavaScript 两个入口 `node --check`
  均通过。另以一次性 PostgreSQL 16 容器在 `127.0.0.1:55439` 实跑任务存储、渠道存储和迁移路径，
  66 passed，随后容器已停止并自动移除。分别删除 null-parent hash 兼容、内存/PostgreSQL parent digest、
  共享 readback 和 worker actor 复核后，对应隔离变异用例均按预期转红，恢复后通过。以上仅证明
  本地 `tests`。最终受审 head `c2d82ede1035b5ca6f8c175c618d4d6837196ce3` 的 run
  `34736983148` 八项全绿，已 squash 为 `fb6718cc6f295206c7c125a887b34d2ee762e71f`，树一致。

- **RI3 PR 3C 已按独立审查沿同一 durable 调用链完成根因补修、自审、CI 与合入**：原候选已用
  失败测试覆盖 application 外层 timeout、SDK close、model/rule accepted intent、grant/fencing
  insert-once、恢复漂移、单一 heartbeat、MODEL trace、慢查询 advisory 和 TaskView readback。本轮
  进一步把 provider 错误归一改为显式完整映射与未知值总降级；把慢查询文本/数值列改为两个显式闭集
  并以精确并集承重；把 rule-origin intent 的恢复身份从 Gemini prompt/schema revision 中解耦。
  沿同路径一并修复 TaskView 共享 `SUCCEEDED` 展示闸、task 状态错误与真实 lease/fencing 丢失分类、
  advisory conflict 后 Reflection 使用旧 outcome，以及第四段模型 advisory 的跨渠道回归。两项承重
  保护分别做了定向变异，均得到预期失败。最终 head
  `bc1812e72dada63a83776bfb159a04c970153c66` 上，本机临时 PostgreSQL 全量
  `python -m pytest -q` 为 3689 passed / 6 个预期 socket-block warnings；
  `python -m pytest -m security -q` 为 1270 passed / 79 skipped / 2340 deselected / 5 个预期 warning；
  Ruff 通过，mypy 162 个源文件通过，`git diff --check` 无输出。临时数据库仅绑定
  `127.0.0.1:55436`、使用无密码 trust 测试配置，验证后已停止并自动移除。PR #36 run
  `34730018472` 精确绑定该 head，八项全绿；squash 后 `main@2d40f7040dae6488ef983589c5655b6c330c529d`
  与受审文件树无差异。以上证据等级仍为 `tests`，未读取真实 Gemini key、未访问 Gemini 或任何真实
  运维目标。

- **RI3 PR 3B 独立复审问题已在实现基线 `b31f7c464c75f0c3191060fceb040e61cb54131c`
  沿完整调用链修复**：单字段 32 KiB 判断不仅四处数学不可达，沿同一路径还确认历史已由
  64,000 字符限制在最多 256,000 UTF-8 字节（小于 256 KiB），所以删除重复假保护，保留字符边界、
  UTF-8 合法性与真正独立可达的 512 KiB 完整序列化上限；usage 两个 SDK 字段分别允许省略或 null，
  只有实际非 null 的负数/溢出拒绝。Python 3.11 实测表明单次取消并不会必然跳过 finally 中的 close；
  真正缺口是重复取消会中断清理、close 自身 `CancelledError` 会遮蔽主异常，现以单一 tracked task、
  共同 deadline、shield、取消后收口和主异常优先修复；自审又发现任意 close `BaseException` 原文可越过
  拒绝边界，已归一为固定本地错误码。Gemini 宿主 key 现固定在 `.config/integrations.json`，ambient
  `GEMINI_API_KEY_FILE` 不能改变渲染结果；smoke 用最后一层私有 JSON override 注入自己的 fake 文件，
  不读取宿主环境。与模型无关的 shared secret reader/StarRocks 行为及 245 行测试已从 PR diff 完整移除，
  相对 `origin/main` 四个路径零差异。
  新反例在旧实现得到 5 failed/1 passed；修复后相关 286 passed，另补 profile mutation、httpx 层级、
  close-side cancellation/BaseException 与 hostile Compose env 边界。两个隔离源码变体均确认从临时路径
  加载：删 UTF-8 合法性校验后 surrogate 用例转红，删 512 KiB 校验后 `+1` 用例转红。最终本地四门为
  3268 passed / 195 skipped / 5 个预期 socket-block warnings、security 1255 passed / 79 skipped /
  2129 deselected / 同 5 warnings、Ruff 通过、mypy 156 个源码文件通过。最终受审 head
  `79675aeed895899282e6f4f697463f8b6b27e29b` 的 PR run
  [`34703681866`](https://github.com/shixian66/xiaowei-agent/actions/runs/34703681866) 已由 GitHub 回读确认
  tests、integration、compose-smoke、security-gate、lint、types、deps-audit、secret-scan 八项 success。
  项目负责人批准后，PR [#34](https://github.com/shixian66/xiaowei-agent/pull/34) 已以 squash commit
  `8374dc4255e88949cf5e638371b393bb01f6ed1c` 合入 `main`。以上仍只到 `tests`，没有读取真实 key、
  调用 Gemini、部署、canary 或用户验收。

- **RI3 PR 3B 的 Compose secret 首轮失败此前已沿真实容器路径定位并按根因修复**：首轮 PR #34 CI 的
  `tests`/`integration` 失败来自契约测试只查找旧版独立 `docker-compose`，没有复用 smoke 已有的
  Compose v2/legacy 解析；`compose-smoke` 失败则不是测试环境偶发问题。实际 Compose create/start
  证明 environment-backed secret 虽出现在渲染配置中，却不能为现有 `read_only` worker 建立 mount，
  启动会拒绝非 file 来源。修复复用既有 Compose command resolver，并把 Gemini 改为宿主
  Git-ignored key 文件到 worker-only file-backed secret（该拓扑**已由 RI5 取代**，当前真源是
  `.config/integrations.json`）；没有取消 worker 只读根文件系统，也没有把
  key 放进容器环境。新增反例覆盖错误宿主 source、两类宿主输入名进入容器环境、非 worker 泄漏、
  全部五个临时输入创建点的 `RuntimeError`/`KeyboardInterrupt` 清理；受控本机 model-only
  create/inspect 已证明 worker mount 的
  `Source` 精确等于本轮 fake key 文件、目标为固定只读路径，其他服务没有该 mount，且全程不读取
  secret 内容。聚焦 Compose/Gemini credential 契约为 159 passed；本地四门为 3268 passed /
  195 skipped / 5 个预期 socket-block warnings、security 1254 passed / 79 skipped / 2128 deselected /
  同 5 warnings、Ruff 通过、mypy 156 个源码文件通过；冻结依赖导出 80 个包，严格 `pip-audit` 无已知
  漏洞。根因修复实现提交为 `e7a529db927fd70be901e8b88df520e90d8c9b39`；PR #34 run
  [`34698212114`](https://github.com/shixian66/xiaowei-agent/actions/runs/34698212114) 精确绑定该提交，
  tests、integration、compose-smoke、security-gate、lint、types、deps-audit、secret-scan 八项均
  success。以上仍只是离线 `tests`/本机容器结构证据，不是 Gemini 网络、测试环境、部署或用户验收。

- **RI3 PR 3A 文档候选基于最新 `origin/main@ab35b756b07aa1baa2f0eb3ac98dba24999c40b1`**：
  PR #30/#31 的 GitHub 状态均为 merged，当前分支只修改 11 份规划/ADR 文档，`src/`、`tests/`、
  dependency、Compose 和脚本均未改动。聚焦文档/分层/Runner/Compose 契约为 211 passed；本地四门为
  3101 passed / 195 skipped / 5 个预期 socket-block warnings、security 1240 passed / 79 skipped /
  1977 deselected / 同 5 warnings、Ruff 通过、mypy 153 个源码文件通过。该证据等级仍是 `tests`，
  不包含 SDK wheel 审计、真实 key、Gemini 网络、部署、canary 或用户验收。

- **RI3 规划基于 `origin/main@ab35b756b07aa1baa2f0eb3ac98dba24999c40b1` 重新入职**：按
  `AGENTS.md` 顺序读取五份真源，并直接检查 Runtime、Worker、Runner、TaskStore、TaskView、
  Compose 与 secret reader 的当前调用链。该 PR 3A 审计时的源码事实仍是只有规则解释器、无 Gemini
  dependency/adapter/真实调用；PR 3A 只产生已批准的文档基线。已合入的 PR 3B 离线补齐 dependency/
  adapter，但 Runtime 与真实调用仍为零。Google 官方模型、structured-output 和 Python SDK 文档
  支持使用成熟的异步 `models.generate_content`，因此精简稿不再使用 Interactions API；实际 wheel
  仍要求实现开始时再次审计并精确锁定。

- **RI1 合并前审查补修实现基线 `2d67b59e128c1c226fb062708edf10c9251a4b3e` 已通过本地深档验证，
  证据等级仍为 `tests`**：审查提出的 I1/I2/M1–M3 均沿真实调用链修复；N1 经源码与既有对抗测试
  确认是绕过 Settings 校验时可达的纵深防御；经正常 `load_settings()` 路径仍不可达，因此保留但
  不表述成真实半开启动分支。最终受影响模块 413 passed；四条规范门分别为 3101 passed /
  195 skipped / 5 warnings、security 1240 passed / 79 skipped / 1977 deselected / 5 warnings、Ruff
  通过、mypy 153 个源文件通过。新增反例先在旧实现得到 OAuth deadline 1 failed，以及 OAuth start
  资源/cookie 边界 12 failed / 4 passed，修复后分别 3 passed 与 16 passed；全局 origin、OAuth start、
  timeout pairing 与 SDK domain 四个隔离变体也分别按预期转红。两轮独立复审确认 deadline 起点、
  response/connection 关闭、重复 cookie、无代理/重定向和 callback 排除均已闭环。以上没有运行本机
  Compose 或真实飞书。PR run [`34555013826`](https://github.com/shixian66/xiaowei-agent/actions/runs/34555013826)
  精确绑定该实现基线，tests、integration、compose-smoke、security-gate、lint、types、deps-audit、
  secret-scan 八项均 success；其中 Compose smoke 在 GitHub 隔离 runner 的真实 Web/PostgreSQL
  容器上走完上述离线 OAuth start 正反例。
- **RI1 首个全分支候选 `d4f48e8800a033bf01560d006c984cfbde514b5f` 已通过独立终审与 PR CI，
  证据等级仍为 `tests`**：相对 `main@66864253e0c9c9a0c6ebda1eec3a25b8e06a8df2` 的 42 个文件
  由独立终审确认 0 Critical / 0 Important / 0 Suggestion；本机四门为 3081 passed / 195 skipped、
  security 1239 passed / 79 skipped、Ruff 通过、mypy 153 个源文件通过。PR [#31](https://github.com/shixian66/xiaowei-agent/pull/31)
  run [`34501610890`](https://github.com/shixian66/xiaowei-agent/actions/runs/34501610890) 精确绑定该
  head，tests、integration、compose-smoke、security-gate、lint、types、deps-audit、secret-scan
  八项均 success；integration 使用隔离 PostgreSQL，compose-smoke 在 GitHub 隔离 runner 实际执行
  `python -m scripts.compose_smoke`。这不是飞书测试企业、部署、canary 或用户验收证据。
- **RI1 Task 4 初版实现为 `a85e9b9eae7f3f6ed3ac7d08e1f3595348235a50`，最终精确审查对象为
  `167af47352e42e025fce84753bc34160c1064944`，证据上限仍为 `tests`**：初版首轮
  Compose/Web smoke RED 为 21 failed / 47 passed；精确审查首批反例为 14 failed / 66 passed，
  后续七条组合/边界反例为 6 failed / 1 passed——其中 `main` 传递已解析 Compose command 的实现
  原本正确，其余六条先红。补修覆盖实际进程 EUID、任意异常后的输入清理、host readiness 禁代理/禁
  重定向、容器 healthcheck 直连、main/barrier Compose command 贯穿，以及 UUID 私有输入目录和动态
  override；后续继续封住首个失败被清理异常覆盖、note 写入反向遮蔽首错、`BaseException` 清理遗漏，
  并固定 cleanup 阶段码和异常链不泄漏二次动态详情。独立复审在精确 head 上取得 focused 113 passed、
  `.venv/bin/python -m pytest -q` 3081 passed / 195 skipped、
  `.venv/bin/python -m pytest -m security -q` 1239 passed / 79 skipped；`.venv/bin/ruff check .` 与
  `.venv/bin/mypy src`（153 个源文件）均通过，`git diff --check` 无输出。四个最终独立变体分别绕过
  两处 safe-note helper、删除固定 cleanup 阶段码、注入二次 cleanup 异常链，对应用例分别以
  1、1、1、3 条失败转红；Task 4 最终审查结论为 0 Critical / 0 Important / 0 Suggestion。
  `/opt/homebrew/bin/docker-compose -f docker-compose.yml config`、追加静态 smoke override 和追加本次
  动态 JSON override 的 `--profile m7-channels config` 均 exit 0；动态结果确认两个 secret source 与
  identity bind 只指向本次私有目录。静态 config 不证明源文件对容器真实可读，也不证明容器健康、
  OAuth/provider 或飞书能力。
- **M7 PR 1–8 离线实现范围已验收并归档**：PR #20–#27 已合入最终代码/测试基线
  `ba5ecfe5edffb408e20c4bb9cbf494bfbb035b82`；PR #28 以
  `a5c88cbc285658f7f38355a9470bfdd725858ba5` 合入离线完成交接。项目负责人于
  2026-09-09 明确批准按离线范围验收并授权归档。PR #27 run `34341819892` 与合入后
  main run `34343986339` 均八项全绿；后者 integration `2995 passed`、0 skipped，
  Compose 输出 `compose-smoke: passed`。最强证据仍为 `tests`，完整 PR、TDD 与变异链见
  [M7 离线范围归档](docs/handoff/archive/2026-09-09-M7-web-feishu-offline.md) 和
  [M7 离线范围验收报告](docs/handoff/M7-acceptance-report.md)。该结论不是部署、真实渠道、
  canary、产品用户验收或 M7 完整退出。
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

- **W0 只改文档与文档契约测试**：Web 产品的用户目录、`AdminCapability`、`AdminAuditStore`、
  激活审批、三域配置与结果访问**都还没有任何实现载体**；W0 全绿不代表这些功能存在。
- **真机浏览器、部署、canary 与用户验收仍未覆盖**：RI5 本地 Web Admin 已离线实现，
  但从未在真实浏览器、真实部署或真实用户手上验证过。
- **ADR-014 R2 的补偿控制只有文档口径**：边缘限流是 release/canary 的证据要求，
  应用层至今没有 HTTP rate limiter，本轮没有改变这一点。

- **I1-D 仍只是本地离线 `tests` 证据**：没有读取真实 Gemini key，没有 Gemini/飞书/
  StarRocks 网络调用，没有部署、canary、测试环境验证或用户验收。`CLARIFICATION_REQUIRED` 终态澄清、
  clarification child 消费、typed SlotVerifier/可信槽位升级与 ReadClass/Plan schema V2 已完成离线实现；
  ExecutionDisclosure 执行披露屏障正在本分支离线实现。本机未提供 `PYTEST_POSTGRES_DSN`，因此需要真实
  PostgreSQL 的 integration 用例仍需以 CI 或本机 DSN 实跑补足真实库证据。

- **RI3 PR 3C/3D 已合入，但证据仍只到离线 `tests`**：durable Runtime、
  `rev_0008`、artifact、MODEL trace、fallback/retry service 和慢查询 advisory 已在 fake/临时
  PostgreSQL 与隔离 CI 路径验证；PR 3D 的 Web 显式父任务、`rev_0009` 和安全历史组装已在本地
  fake、一次性 PostgreSQL 与隔离 CI 路径验证；这仍不等于真实模型或人工 Web 验收。
  未读取 `GEMINI_API_KEY`，Gemini 网络调用为 0；固定 10 个 intent/5 个 advisory corpus 只证明离线
  安全矩阵，不证明真实模型质量、配额、延迟、usage 字段或 preview 模型稳定性，也没有 test-env、
  部署、canary 或用户验收证据。Web 交互目前只由自动化测试验证，未做真实浏览器人工验收。

- **PR 3B 的完整 base+model workflow 尚未在本机实跑**：受控 model-only create/inspect 已取得
  worker-only mount 实证，但用户已有容器占用 `127.0.0.1:8000`；本任务未停止或修改该容器，因此没有
  在本机启动整套 base+model 服务。PR #34 的 GitHub 隔离 runner 已执行完整 Compose smoke 和
  PostgreSQL integration，但这仍只是一次性的 `tests` 证据，不能声称本机或测试企业的
  Web/OAuth/Gemini 已运行，更不能外推为部署、canary 或用户验收。加固后的 smoke 请求隔离 runner
  Web 进程的 OAuth start 正反例，但不跟随 Location、不请求 callback、不调用 provider 或其他真实
  外部业务接口。
- **M7 PR 1–8 离线范围已验收并归档，但证据上限仍是 `tests`**：当时本机未能使用 Docker
  daemon，也未提供 PostgreSQL DSN，因此新增 M7 同库 integration 在本机受控 skip、Compose
  未在本机实跑。PR 与
  main CI 已在 GitHub 隔离 runner 补得 PostgreSQL 0 skipped 与 Compose 容器运行证据。M7 仍无真实飞书/Web
  渠道进程、部署、canary 或用户验收证据。未来的
  Admin 配置治理和真实模型 API 也尚无源码或运行证据；真实应用、凭据、网络连接、部署与 canary
  仍被独立硬门阻塞。
- **PR 7 的真实浏览器视觉截图尚未取得**：当前浏览器安全策略阻止访问本机预览地址及离线页面；
  1280/1440 桌面工作台与窄屏详情目前只有 HTML/CSS 契约、断点和静态范围测试证据，仍需在可访问
  本地服务的浏览器中人工核对。该缺口不应被表述成 UI、部署或产品用户验收。
- **分支保护未建立**，且 private + GitHub Free 下无法建立（API 实证 403）。
- 未部署、未 canary、未取得产品用户验收；M5/M6a 与 M7 离线范围的“验收通过”都是项目里程碑
  范围验收，不改变 readiness ladder。
- 未连接任何真实运维目标或模型服务：StarRocks、Prometheus、资产系统与任何模型 API 均未连接；M5 只使用 GitHub runner 上一次性的隔离 PostgreSQL/Compose，**E1 调用恒为 0**。
- ~~M2 只有契约与 fake~~：M3 已落地 `CapabilityResolver`、`PlanCompiler`、`StepAdmission`、`ToolPolicy`、`SQLGuard`、`ApprovalGate`、`DeterministicStepRunner`、`EvidenceBuilder`、Reflection 与 `XiaoweiRuntime`，**全部只用 fake/recording 数据**。
- 当前开发机未提供 `PYTEST_POSTGRES_DSN`；Colima daemon 当前可用，但上述 credential helper 与
  `127.0.0.1:8000` 冲突仍阻止本轮完整 PR 3B mount audit。本机资产 PostgreSQL integration 仍是
  受控 skip；最终 PR #14 run
  `33975532006` 与 main run `33976421909` 只补得当时版本的 GitHub 隔离 runner 证据。单次 CI
  仍不能外推到 RI1 新版、其他 PostgreSQL/Docker/Compose 版本或长期运行。
- 主 `main` checkout 的旧 M6a 同路径计划稿已在 PR 1 合入前移到临时目录备份；当前仍保留三份与
  本任务无关的用户未跟踪文档 `docs/plans/M8-dinky-controlled-stop.md`、
  `docs/plans/development-route-v3-proposal.md` 与
  `docs/plans/legacy-capability-migration-matrix.md`，本任务不读取、不修改、不暂存。
- **`StepConditionKind` 四个成员已有三个被消费**：`ALWAYS`、`EVIDENCE_ROW_COUNT_BELOW` 与 `PRIOR_STEP_RESULT_IS`。后者由 Prom 两步计划消费，并只读取持久化 step journal 的 committed `OK`；`FAILED`/`TIMEOUT`/无记录均不运行后续步骤。`EVIDENCE_FIELD_ABSENT` 仍未消费、未验证。
- **M3 未验证真实恢复**：`resume()` 的漂移拒绝有测试，但"审批通过后恢复并真的执行副作用步骤"这条路径**永远不会在 M0-M7 走通**（E1 硬闸），因此只验证了控制流。
- 攻击矩阵中 A26/A29/A30 是链路层用例，A31-A36 是 SQL 层用例；**未覆盖**的是真实 StarRocks 的语法差异——全部 AST 结论都基于 sqlglot 30.17.0 的 starrocks 方言实现，不是真实服务端的解析结果。
- ADR-001 至 ADR-006 尚未编写。
- Multi-Agent 准入条件仍只有文档约束；M9 也只评估 Runner，不授予 Multi-Agent 权限。
- M6b 已完成离线实现、审查、合入和合入后 CI，但项目负责人已将真实测试环境验证延期；尚无测试环境 StarRocks 的真实连接、凭证引用 readback、
  physical identity、DDL/grants 或数据处置证据，因此不能标记 `test-env verified`。

### 残余风险

- **Interaction artifact 迁移保留 V1 历史行但新路径只解释 V2**：这避免旧 accepted intent 被当作
  新 interaction 执行，但也意味着升级后的旧任务不会自动补成完整 I1 interaction 语义。未来若要展示或
  迁移旧 V1 artifact，必须单独设计只读兼容/回填路径，不能让 Runtime loader 放宽到 V1。
- **Router 的 `CLARIFY` 纯裁决早于澄清存储闭环落地**：当前能测试分流根因，但还不能证明
  `ClarificationRecord`、子任务唯一消费、confirmed slots 与 TaskView 四象限在真实 Runtime 中闭环。
  后续 Task 1.4 必须用同一条 Runtime 调用链承重，而不是在 handler 或渠道层补特例。

- **RI3 精简设计已获批准但仍依赖 preview 模型**：模型或 SDK 可能在首次真实调用前漂移；公司项目的数据
  保留/训练/区域条款尚未现场确认；协程取消不能证明远端停止；共享脱敏规则不能识别所有业务敏感
  信息；显式历史会把既存文本再次发送给 provider。provider 收到请求但本地保存前崩溃时可能重复
  调用，且该次 usage 可能无法完整落库，这是当前明确接受的体验优先取舍。`WorkerLoop` 单进程逐任务
  执行，真实吞吐和排队尚无证据；需要扩 worker 时必须单独设计 provider 限流与运行验证，不能现场
  workaround 后冒充已验收。上述任一现场门未闭合时 model override 必须保持关闭。

- **RI3 的已保存 advisory 可作为后续显式父链的模型输入**：它经过严格 schema、再次脱敏和总量限制，
  且模型仍无执行权，但这是 RI3 唯一一条模型文本进入后续模型请求的路径。PR 3E 必须用含指令文本的
  已保存 advisory 做 20 轮恶意输入矩阵，并证明相对控制样本不能改变 capability、slots 或 missing；
  在该现场反证前，这项只具备源码边界与离线推理，不是 test-env 证据。

- **M7 PR 4 虽已合入，但只验证了锁定 wheel 的源码形状与 fake SDK 对象**：尚未验证测试企业的真实长连接握手、
  回调对象、应用权限、成员可见性与分页返回形状；这些只能在 RI2 现场门齐备后先取得
  `test-env verified`，部署后再独立 canary。身份 allowlist
  在 listener 和 Web app 装配时读取一次，撤权或改权限必须受控重启；同步 SDK 线程的优雅停止与进程崩溃恢复
  已有 PR 8 的静态进程契约、默认关闭入口检查及一次 GitHub CI Compose 运行证据，但尚无 RI1
  本机容器运行或真实 provider 证据。
- **RI1 已实现默认关闭的真实 OAuth adapter 与 Web composition root，但只经过离线 transport/路由
  契约**：授权 URL、code exchange、错误与超时分支没有与真实飞书交互；provider 总预算固定 5 秒，
  WebAuth 外层 watchdog 固定 6 秒，第二次 credential 文件读取也计入 provider 总预算。同步文件读取
  本身不能被 asyncio 强制中断，但它晚返回时不会再启动 HTTP。默认双开关、配置校验、state/session
  摘要与 1024 个全局 pending state 上限继续 fail-closed；该上限只防数据库无界增长，匿名方可在一个
  TTL 内填满额度并持续补位，使所有正常登录返回 503。RI2/RI6 在真实激活前必须取得 SSO 边缘限流、
  监控/告警和运行反证，不能把容量测试当作滥用防护。过期 state/session 的后台清理仍未实现。
- **RI1 共用的 credential reader 仍只验证最终路径分量和文件内容形状**：`O_NOFOLLOW` 拒绝最终
  symlink，并检查普通文件、大小、UTF-8、单行与控制字符；它不验证中间目录是否含 symlink，也不验证
  文件 owner/mode。该限制没有因公共 API 重命名而消失，RI2 真实 secret 引用准入前必须结合容器挂载
  与宿主权限重新核对。RI3 只让 Gemini 复用现有 `interfaces.secret_file`，不把四类调用方重构绑进
  模型接入；共同 owner/mode/中间目录 hardening 仍由对应真实接入阶段单独负责，不能提前消除此风险。
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
