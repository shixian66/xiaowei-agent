# M7 Web 与飞书薄渠道实施计划（V0.6）

> **For agentic workers:** implementation must use
> `superpowers:executing-plans` task by task. Do not dispatch subagents unless the
> project owner explicitly authorizes delegation. Every behavior change follows TDD,
> and every review conclusion is bound to an exact commit SHA.

**Goal:** 先完成飞书渠道的离线闭环和独立评审，再完成 Web 运维任务工作台，证明两个渠道
复用同一应用层 `TaskViewRuntime`、`TaskView`、`RenderPayload`、TaskStore 状态与证据语义；
完整 `XiaoweiRuntime` 也委托该组件，不能形成第二套投影。

**Architecture:** Web 与飞书只处理协议、可信身份、任务链接、访问授权和安全投影。
业务候选、计划、准入、SQL/PromQL、工具调用、证据判断与任务终态仍由现有确定性主链唯一负责。
TaskStore 是任务事实真源；ChannelStore 只保存渠道绑定与投影订阅，绝不复制任务事实。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、SQLAlchemy Core、PostgreSQL、原生
HTML/CSS/ES Modules、`lark-oapi==1.7.3`、pytest、Ruff、mypy。

**Normative inputs:** `AGENTS.md`、`ARCHITECTURE.md`、`AGENT_HANDOFF.md`、
`README.md`、`DEVELOPMENT_PLAN.md` M7、ADR-007/008/010/011/012，以及本文第 2 节的产品决定。

---

## 0. 文档状态与实现硬门

### 0.1 本次事实快照

- 计划修订日：2026-09-08。
- 当前文档分支：`claude/m7-plan-v0.4-gates`。
- 文档分支 fork point：`aa2af2edcd4c30c611e0ed51d10263b26acef598`。
- 当前 `origin/main`：`aa2af2edcd4c30c611e0ed51d10263b26acef598`；文档分支从该 SHA 创建。
- M6b 实现 squash merge：`a5b60baa25eda7ec964b2b48f13f051bf926e3c4`。
- M6b 当前最强证据是离线测试；没有真实测试环境验证、部署、canary 或用户验收。
- 当前源码的 `interfaces/api.py` 是固定配置、无终端用户鉴权的内部 API；它不能直接暴露到
  Web 公网入口。
- 当前 `TaskStore` 有按 tenant/environment/task 的单任务读取和不可变
  `task_submissions`，但没有面向 actor/admin 的分页读取。
- 当前 `RenderPayload` 只包含安全回答、分节、下一步、状态和引用；没有数据库原始结果行。
- 当前 `tests/security/test_module_layering.py` 对每个 `interfaces/*.py` 使用穷尽式白名单；
  新增任何入口文件都必须在同一提交登记。
- 当前 `tests/security/test_env_example_clean.py` 要求 `.env.example` 与 `_FIELD_TO_ENV` 的变量集合
  完全相等；新增设置必须三处同步。
- 当前 `interfaces/local_stack.py` 是 API/worker 唯一装配根，完整 `XiaoweiRuntime` 构造会同时装配
  Runner、ToolGateway 和各运维目标 recording adapter；渠道进程不能复用这条完整 builder。
- 当前 `_conformance.py` 是 `mypy src` 能看到的 Protocol 静态锚点；只在 tests 里放 fake 赋值不构成
  生产类型证据。
- 当前两份用户未跟踪路线稿不属于 M7，本计划不读取、不修改、不暂存：
  - `docs/plans/development-route-v3-proposal.md`
  - `docs/plans/legacy-capability-migration-matrix.md`

以上 SHA 与状态只是计划编写时快照，不能替代真正开工时的现场核对。

### 0.2 计划审核不等于实现授权

本文 **V0.6 已获项目负责人批准、通过技术复核并合入 `main`**。V0.5 已通过技术复核；
项目负责人于 2026-09-08 进一步明确：M6a/M6b 的代码完成状态不应成为 M7 离线开发的技术阻塞，
因此 PR 1–8 均可在离线门内实现；真实应用、凭据、网络连接、部署与 canary 继续使用独立硬门。
V0.6 已由提交 `289efe5` 合入；项目负责人随后明确发出“开始 M7 离线实现”。该口令只授权离线
源码、测试和本地隔离基础设施，不授权注册飞书应用、读取真实凭据、连接真实飞书或运维目标、
部署、canary 或用户验收。

### 0.3 分阶段且不可降低的实现入口门

#### 0.3.1 PR 1–8 离线实现门

PR 1–8 均可在 fake/recording 与本地隔离基础设施上离线实现和评审，包括锁定飞书 SDK、实现默认
关闭的 listener/worker/Web 进程、OAuth/Membership typed port、卡片投影、Web UI 与 Compose smoke。
离线实现不得注册真实应用、读取真实 secret、连接飞书或任何真实运维目标，也不得部署、canary
或声称渠道可用。开始源码工作前，以下条件必须同时成立：

- [x] 渠道优先级已确认：飞书第一，Web 第二。
- [x] V0.3 已通过 Claude 技术审核。
- [x] V0.5（包含 V0.4 阶段门与本轮产品范围收口）已通过技术复核。
- [x] 项目负责人已明确批准 V0.6：PR 1–8 均可离线实现，真实渠道激活仍保持硬门。
- [x] V0.6 已通过技术复核并作为纯文档基线合入 `main`。
- [x] 项目负责人在上述文档基线合入后明确说“开始 M7 离线实现”。
- [x] 届时重新按顺序完整读取最新 `AGENTS.md`、`ARCHITECTURE.md`、
  `AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`。
- [x] 届时重新核对 `git status --short --branch`、`HEAD`、`main`、`origin/main`、
  未提交文件和前一里程碑证据，不能依赖本文旧摘要。
- [x] 从当时最新 `main` 创建 `claude/<topic>` 分支，不直接在 `main` 开发。

该离线门是项目负责人对 `DEVELOPMENT_PLAN.md` 里程碑串行规则批准的窄例外，不改变 M6b 的
`tests` 证据等级，不把 PR 1–8 表述为渠道可用，也不授权真实渠道激活。任一项准备不足时，
仍只能继续评审，不能开始源码工作。

#### 0.3.2 真实渠道激活、部署与 canary 门

注册真实飞书应用、读取 secret reference、发起任何飞书网络请求、部署渠道进程或执行真实渠道
canary 前，除已满足 0.3.1 外，还必须同时成立：

- [ ] 被验证的 M7 代码已有精确受审 SHA，离线四门、Compose smoke 与安全 eval 全绿。
- [ ] 飞书测试企业自建应用、身份映射、所需权限、secret reference、数据范围、保留策略、调用窗口
  和回滚方式均已由项目负责人单独批准。
- [ ] `lark-oapi` 版本、wheel digest、类型声明和实际 async API 已在 PR 4 离线实现时核验；与本文
  不一致时已先修订计划，没有靠兼容猜测继续。
- [ ] canary 若展示真实运维结果，对应 capability 必须先取得其自身要求的真实运行证据；若只使用
  fake/recording，则必须显式标注，且不能提升 M6b 或该 capability 的证据等级。
- [ ] 项目负责人在上述条件满足后明确说“开始 M7 真实渠道验证”。

M6a/M6b 的代码完成与 M7 离线开发相互独立；M6b 延期的真实测试环境验证不阻塞 PR 1–8，
也不会因 M7 离线测试或真实渠道 fake/recording canary 自动完成。真实渠道门未齐时可以完成并
评审 PR 1–8，但所有渠道进程必须默认关闭，且不得注册应用、读取真实凭据、连接或部署真实服务。

---

## 1. 外部审核意见处理结论

### 1.1 V0.2 已处理项

| 审核点 | 处理 | V0.2 决定 |
| --- | --- | --- |
| Web 与内部 `/v1` 共用一个 ASGI app 会扩大攻击面 | 采纳 | 拆成独立进程、端口和路由闭集；公网 Web app 不注册内部 `/v1/tasks/*` |
| 新 `interfaces/*.py` 会触发穷尽白名单 | 采纳 | 每个新增入口文件必须在同一提交更新 `test_module_layering.py` |
| HTTP 错误闭集缺 401/403 | 采纳 | PR 1 预留 `unauthorized`、`forbidden` 最小错误体；PR 6/7 必须用真实 Web app 路由测试 401/403 与存在性保护 |
| 单次 delivery 无法表达等待终态与供应商失败 | 采纳 | 改成有 fencing 的持久化 ProjectionSubscription 状态机 |
| M6 canary 门可以降级 | V0.4 未采纳；V0.6 重新界定 | 取消 M6 canary 作为 M7 的全局前置；仅当渠道 canary 展示真实运维结果时，要求对应 capability 先取得自身真实运行证据，见 §0.3.2/§1.5 |
| 六个角色写进核心契约过早 | 采纳方向 | 核心只认识三个权限；运维/DBA/值班等标签只在身份映射配置出现 |
| ChannelStore 保存 owner/status/version/request_text 会成为第二事实源 | 采纳 | 任务字段、提交和列表全部从 TaskStore 读取；ChannelStore 仅保存绑定/订阅 |
| 群任务列表需要批量查群成员关系 | 收敛范围 | M7 不做“所有群任务”聚合列表；群卡片链接按单任务实时校验成员关系 |
| Renderer 输入缺 request preview 与版本 | 采纳 | 使用 `FeishuProjectionInput(task_view, request_preview, task_version, detail_url)` |
| 飞书 SDK 无类型声明会污染 strict mypy | 采纳 | 单一动态加载叶子适配器 + 本地 typed Protocol；禁止其他文件直接 import SDK |
| Web client idempotency key 可跨作用域碰撞 | 采纳 | 服务端对租户、环境、渠道、actor 与客户端键做 canonical digest |
| 飞书出站是否经过 ToolGateway 不清楚 | 澄清 | 它不是被管运维目标 E1，不进 ToolGateway；仍有独立超时、重试、限流、审计 |
| PR 粒度过大 | 采纳 | 拆为 7 个按依赖顺序评审的小 PR |

V0.2 还补充了一项 SDK 实测风险：官方长连接客户端会在 INFO 日志中记录完整连接 URL，URL
可能带不透明连接参数。M7 必须关闭/过滤 SDK 上游日志，并用安全测试证明连接 URL 与伪造凭据
不进入日志。

### 1.2 V0.3 新增修订

| V0.2 复审项 | 处理 | V0.3 决定 |
| --- | --- | --- |
| 新配置遗漏 `.env.example` | 采纳 | 每个增加设置的 PR 同时改 `config.py`、`.env.example`，安全测试钉集合相等 |
| 三个新进程缺少装配根 | 采纳 | 继续使用 `interfaces/local_stack.py` 唯一装配根，为各进程提供窄 stack builder |
| 渠道进程装配完整 Runtime 会携带 ToolGateway/目标 adapter | 采纳并扩大到同类入口 | 抽出四依赖 `TaskViewRuntime`；作为独立 PR 后总计 8 个 PR，只有 task-worker 装配完整执行 Runtime |
| 新 Protocol 没有 strict mypy 静态锚点 | 采纳 | 每个 Protocol 与生产实现同 PR 更新 `_conformance.py` |
| 新进程使用 `[project.scripts]` 与仓库惯例不一致 | 采纳 | 全部改为 `python -m xiaowei_agent.interfaces.<module>` |
| `get_submission` 调用闭集只有注释 | 采纳 | AST 安全测试把调用点锁为 `channel_access.py` 与 `channel_projection.py` |
| “同一 destination 严格串行”无法由 subscription claim 保证 | 采纳 | 改为同一 subscription 严格串行，不承诺跨任务 destination 全局串行 |
| 投影枚举和 `WAITING_FOR_TASK` 命名不自洽 | 采纳 | PR 1 明确定义三个枚举闭集；等待任务保持 `WAITING_TERMINAL`，不是错误码 |

`TaskViewRuntime` 不是新的业务脑：它只把现有 `XiaoweiRuntime.submit_task()`、
`query_task()` 和终态重投影所需逻辑抽成唯一共享组件。完整 Runtime 委托它；渠道和内部只读 API
直接使用它。这样“同一 Runtime 语义”仍由代码复用证明，同时入口进程没有 Runner、ToolGateway
或任何运维目标 adapter。

### 1.3 V0.4 入口门修订

V0.3 技术审核通过后，审核人指出原 §0.3 会因 M6b 真实验证延期而阻塞全部 M7。项目负责人
于 2026-09-07 明确批准 B，并接受 PR 1 与 PR 3 在 M6 路线最终被否定时可能产生返工的风险：

- PR 1–3 可在纯文档基线合入并再次获得明确开工口令后离线实现；其中 PR 2 的读取投影与执行权
  分离属于独立安全架构收益。
- PR 4–8 仍由 M6 真实只读 canary、真实飞书应用授权和新鲜 SDK 事实共同阻塞。
- 该拆分不改变 ADR-007，不提升 M6b 证据等级，不授权真实网络调用，也不降低 M7 最终退出标准。

以上是 V0.4 的历史决定；V0.6 以 §1.5 取代其“PR 4–8 不得离线实现”的部分，但不把离线开发
解释为真实渠道激活许可。

### 1.4 V0.5 产品范围收口

项目负责人于 2026-09-08 确认：未来产品应包含数据库、Prometheus、模型 API、飞书等
Admin 配置治理，以及后续审批和确定性运维能力；但这些目标不能借 UI 预览进入 M7。由此固定：

- M7 的主工作台只适配桌面端；不适配手机端 AI 工作台、任务发起、任务列表和后台配置。
- 仅保留从飞书卡片打开的轻量单任务详情在窄屏可读；它只展示状态与安全 `RenderPayload`，
  不提供发起、聚合列表或配置入口。
- 未来的 Admin 配置中心、真实模型 API、审批/重跑和 Jenkins、Dinky 等能力，
  必须分别经过后续里程碑、ADR、权限、secret、审计、测试与回滚设计后才可实现。
- 本轮交互示例中的连接、任务和状态只用于产品评审，不是已配置、已连接或已验证证据。

### 1.5 V0.6 离线开发与真实渠道激活解耦

项目负责人于 2026-09-08 明确批准修正 V0.4 的阶段门：M6a/M6b 的代码完成状态不再被误写成
M7 离线实现的技术依赖。V0.4 “只开放 PR 1–3”的结论由本节取代：

- PR 1–8 均可在 V0.6 合入并再次获得明确开工口令后离线实现；PR 顺序和逐 PR 审查不变。
- PR 4 可以核验并锁定 SDK，PR 4–8 可以实现默认关闭的渠道进程、Web UI 和本地 Compose；全部
  使用 fake/recording/typed port，不注册应用、不读取真实 secret、不发起真实网络调用。
- 真实应用注册、凭据、网络连接、部署、canary 与用户验收仍由 §0.3.2 单独阻塞。
- M6b 的真实测试环境验证继续独立延期；M7 的任何离线或 fake 渠道证据都不能提升其状态。

---

## 2. 已确认产品边界

### 2.1 M7 只交付什么

1. 飞书长连接事件接入、企业身份映射、群成员校验、事件幂等和任务提交。
2. 飞书受理/终态卡片，以及“还有更多证据 → 查看完整结果”的 Web 安全详情链接。
3. 普通 Web 用户任务完成后的飞书私聊通知；admin 不接收额外私聊通知。
4. 使用飞书企业身份登录的 Web“运维任务工作台”。
5. Web 的“我的任务”、admin 当前 scope 全部安全任务、单任务详情和有界轮询。
6. API、CLI、飞书、Web 对同一任务的状态、`RenderPayload`、证据引用、限制和下一步语义一致。
7. 渠道绑定、投影订阅、Web session 的可靠持久化及离线/Compose 证据。
8. 经单独授权后的真实飞书 canary 资产、回滚说明和验收记录模板。

### 2.2 明确不做什么

- 不增加 capability，不调整候选解析、planning、policy、SQLGuard、PromQLGuard、
  ApprovalGate、ToolGateway、Evidence、Reflection 或任务终态语义。
- 不实现数据库任意 SQL，不展示数据库真实结果行，不增加结果预览、文件下载或导出 API。
- 不提供 `EvidenceEnvelope.facts`、adapter payload、SQL/PromQL 或原始工具返回的通用详情 API。
- 不增加审批、拒绝、取消、重新执行、写操作或 admin 自批按钮；admin 自批属于后续里程碑。
- 不实现 Admin 配置中心。数据库、Prometheus、飞书、Jenkins、Dinky 等配置维护需要独立的
  配置版本、secret reference、审计、readback 和回滚设计。
- 不实现 Jenkins 发布、Dinky 启停或其他确定性运维能力；这些能力可复用本轮渠道，但不属于 M7。
- 不引入 React、Vue、Node 构建链、前端状态框架、微服务、消息总线或第二套 lint/type 工具。
- 不实现 Runtime 多轮会话、补槽恢复、模型记忆或 WebSocket。Web V1 每次发送都是独立任务。
- 不实现未来的真实模型 API 接入、动态资源参数发布、审批/重跑或运维能力控制面；这些目标不构成
  M7 实现授权。
- 不实现全局“我所在群的所有群任务”聚合列表；M7 只支持群卡片到单任务详情。
- 不实现飞书 webhook；长连接是 M7 唯一入站传输，webhook 只作为未来备选记录在 ADR。
- 不把渠道投递失败、OAuth 失败或成员校验失败转换成任务失败。

### 2.3 “查看完整结果”的唯一含义

“查看完整结果”表示 Web 展示该任务完整的安全 `RenderPayload`：

- `answer`
- 全部 `sections`
- `next_steps`
- `status`
- `refs`

它不表示完整 Evidence，不表示数据库真实结果行，也不表示导出。渠道不能通过 capability ID、
关键词、标题或字段名自行推断“这是数据库查询”，更不能重写数据库结果边界。数据库真实结果的
预览/导出继续遵循此前确定的“申请人和审批人”专门规则；M7 没有该 artifact 端点，所以不会触碰它。

### 2.4 飞书交互规则

- 私聊：拥有提交权限的用户可直接发送任务。
- 群聊：只有明确 `@小维` 的消息创建任务；普通群消息被忽略且不产生 TaskSubmission。
- 群任务绑定原 `chat_id` 和来源消息引用；初始受理后，终态更新同一张卡片。
- 群内所有成员可看到卡片里的完整**安全证据摘要**。
- 卡片空间不足时明确显示“还有更多证据”，提供 Web“查看完整结果”入口。
- 群成员打开详情时，Web 按当前登录人和当前群成员关系实时校验；旧链接不保留历史权限。
- 私聊任务安全详情只允许申请人和 admin；任务 ID、卡片链接和浏览器 URL 都不是授权凭证。
- 飞书私聊/群聊的中间运行状态不逐次推送；M7 只承诺受理态和最终态，减少乱序与刷屏。

### 2.5 Web 产品规则

- 产品名：**小维 · 运维任务工作台**。
- 默认入口是带“小维 AI”输入框的任务流，不是传统表单后台。
- 每次发送创建一个独立 `TaskSubmission`；页面按 task ID 展示状态和安全结果。
- 普通授权用户看到“我的任务”；admin 看到当前 tenant/environment 的全部安全任务。
- 群任务不进入普通用户聚合列表，只能由群卡片链接进入并做当前成员校验。
- admin 是本轮最高安全任务权限：可提交、看当前 scope 全部安全任务；这不授予数据库真实结果行权限。
- 普通 Web 用户任务终态后，发送一条飞书私聊完成通知；admin 自己发的 Web 任务不额外通知。
- 页面不展示尚未实现的配置中心、审批、发布、启停等可点击假入口。
- M7 主工作台只做桌面端；窄屏只保证由飞书链接进入的单任务详情可读，不提供 AI 工作台、任务
  发起、任务列表或后台配置。

---

## 3. 不可破坏的执行与数据边界

### 3.1 两个渠道复用的主链

```text
Feishu event / Web HTTP
  → protocol validation and size limit
  → authenticated identity adapter
  → AuthenticatedPrincipal (server constructed)
  → ChannelSubmissionService / TaskAccessService
  → TaskViewRuntime.submit_task() / query_task()
  → TaskView + RenderPayload
  → FeishuProjection / Web safe JSON and HTML
```

完整 `XiaoweiRuntime.submit_task()` / `query_task()` 也委托同一个 `TaskViewRuntime`，所以内部
API、CLI 兼容路径、Web 与飞书不会各写一份终态重投影。

任务执行链保持原样：

```text
IntentDraft
  → CapabilityResolver
  → PlanCompiler
  → WorkflowRunner
  → StepAdmission(ToolPolicy → SQLGuard/PromQLGuard → ApprovalGate*)
  → ToolGateway
  → Evidence
  → Reflection
  → RenderPayload
```

入口、访问服务和投影 worker 均不得 import capability、planning、governance、tools、evidence 或
reflection，也不得调用 `TaskStore.transition()`。

### 3.2 进程与网络拓扑

所有进程仍来自同一仓库、同一镜像和同一模块化单体；这是部署隔离，不是拆微服务。

```text
internal-api process / internal port
  existing POST /v1/tasks
  existing GET  /v1/tasks/{task_id}
  narrow TaskViewRuntime only
  healthz / readyz
  no public ingress

task-worker process
  full XiaoweiRuntime + deterministic execution worker
  the only process that assembles Runner / ToolGateway / managed-target adapters

feishu-listener process
  outbound long-lived WebSocket to Feishu
  no inbound listening port
  narrow TaskViewRuntime + ChannelSubmissionService
  validates events and calls ChannelSubmissionService

channel-worker process
  claims ProjectionSubscription
  reads TaskStore winner and safe TaskViewRuntime projection
  sends/updates Feishu cards through typed SDK seam
  no Runner / ToolGateway / managed-target adapters

web-app process / web ingress port
  Feishu OAuth + secure session
  narrow TaskViewRuntime + channel access/submission services
  /app, /app/api/* and its own health/readiness probes
  never registers /v1/tasks or the internal API app
```

Compose 契约必须检查：

1. `web-app` 与 `internal-api` 使用不同命令、端口和 app factory。
2. 只有 `web-app` 允许接 Web ingress；`internal-api` 仅内部网络可达。
3. 对 `web-app` 请求 `POST /v1/tasks` 与 `GET /v1/tasks/x` 必须得到统一 404。
4. `feishu-listener` 没有 `ports`，只建立出站长连接。
5. 渠道 feature flag 默认关闭；缺凭据时对应进程 fail-closed。
6. internal-api、feishu-listener、channel-worker、web-app 的 stack 类型都不含
   `XiaoweiRuntime`、Runner、ToolGateway 或运维目标 adapter。

### 3.3 唯一装配根与权限隔离

`interfaces/local_stack.py` 继续是项目唯一装配根，但不再让所有进程共用完整 `LocalStack`：

```text
build_postgres_local_stack()
  → existing full LocalStack
  → only task-worker / explicit execution tests

build_postgres_task_view_stack()
  → TaskStore + PlanStore + EvidenceLedger + CapabilityBindingRegistry
  → TaskViewRuntime + readiness + close
  → internal-api

build_postgres_feishu_listener_stack()
  → task-view stack + ChannelStore + IdentityDirectory
  → ChannelSubmissionService + FeishuInboundTransport

build_postgres_channel_worker_stack()
  → task-view stack + ChannelStore + ChannelProjectionService
  → ChannelMessagePort

build_postgres_web_stack()
  → task-view stack + ChannelStore + WebSessionStore + IdentityDirectory
  → TaskAccessService + ChannelSubmissionService + OAuth/Membership ports
```

现有 tool/runner/目标 adapter 的 import 移到完整执行装配函数内部，调用窄 builder 不触发这些模块
导入或对象构造。安全测试同时检查 stack 字段闭集、入口调用的 builder 闭集，以及
`application/task_view_runtime.py` 不依赖 governance、tools、runners、目标 adapter 或
observability。

### 3.4 TaskStore 与 ChannelStore 的真源划分

| 事实 | 唯一真源 |
| --- | --- |
| task_id、tenant、environment、actor、status、version、created_seq | TaskStore |
| 原始不可变 TaskSubmission | TaskStore 的 `task_submissions` |
| terminal RenderPayload 引用及任务终态 | TaskStore/现有 Runtime |
| 来源渠道、群引用、来源事件/消息引用 | ChannelStore 的 ChannelBinding |
| 卡片目标、投影状态、claim/fencing、退避 | ChannelStore 的 ProjectionSubscription |
| Web session digest、OAuth state digest、expiry | WebSessionStore |

ChannelStore 禁止保存 task owner、task status、task version、request text 或 RenderPayload 副本。
需要这些字段时必须按 task ID 回读 TaskStore winner。这样渠道恢复不会形成第二套生命周期事实。

### 3.5 飞书出站与 ToolGateway

飞书消息发送、卡片更新、OAuth 和群成员查询是**渠道基础设施调用**，不是对被管运维目标的
E1 执行，因此不经 ToolGateway。ADR-013 必须明确这一点，同时要求渠道调用具备：

- 固定超时和有限重试；
- tenant/目标维度限流；
- trace/task/subscription 关联；
- 脱敏错误码和可审计状态；
- 不记录响应体、用户消息、连接 URL、token、ticket 或 secret。

它们绝不能反向改变 capability、计划、准入、任务状态或结果语义。

---

## 4. 核心契约草案

契约字段在实现前由失败测试钉死；以下是评审基线，不允许用无结构 `dict` 穿越层边界。

### 4.1 权限与 principal

核心不固化 `viewer/operator/dba/oncall/approver/admin` 六角色，只固化 M7 真正使用的权限：

```python
class ChannelPermission(StrEnum):
    VIEW_SAFE_TASK = "view_safe_task"
    SUBMIT_READONLY_TASK = "submit_readonly_task"
    ADMIN_ALL_SAFE_TASKS = "admin_all_safe_tasks"


class IdentitySource(StrEnum):
    FEISHU = "feishu"


class AuthenticatedPrincipal(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    actor: StrictStr
    source: IdentitySource
    subject_ref: StrictStr
    permissions: frozenset[ChannelPermission]
```

身份适配配置负责把**显式 allowlist 中的账号**及其组织标签映射到权限；仅仅拥有同名组织标签
并不会自动获得访问权：

| 身份配置标签 | M7 权限 |
| --- | --- |
| 运维、DBA、值班人员 | `VIEW_SAFE_TASK` + `SUBMIT_READONLY_TASK` |
| 只读查看人、审批人 | `VIEW_SAFE_TASK` |
| admin | 三项全部 |

这张表描述默认产品映射，不进入核心 enum。审批人的数据库 artifact 权限不在 M7 建模。
admin 是被显式映射了 `ADMIN_ALL_SAFE_TASKS` 的飞书企业账号，不新增内置密码或后门账号。
配置文件在进程启动时严格读取，格式错误、未知标签或空 actor 映射都阻止启动；M7 不支持热更新，
权限撤销需更新配置并重启相关进程。该限制必须在 README 和风险中明示。

### 4.2 窄 TaskViewRuntime

新增 `application/task_view_runtime.py`，把现有 Runtime 中纯任务提交/读取/终态重投影逻辑移入
唯一共享实现：

```python
class TaskViewRuntime:
    def __init__(
        self,
        *,
        task_store: TaskStore,
        plan_store: PlanStore,
        ledger: EvidenceLedger,
        bindings: CapabilityBindingRegistry,
    ) -> None: ...

    async def submit_task(self, *, submission: TaskSubmission) -> TaskView: ...
    async def query_task(self, *, lookup: TaskLookup) -> TaskView: ...
    async def project_recorded(self, *, record: TaskRecord) -> RenderPayload: ...
```

- 四个构造依赖是闭集；没有 Clock、TraceSink、Interpreter、Resolver、Runner、ToolGateway 或目标 adapter。
- 从 `application/runtime.py` 移走 `_task_view`、`_project_recorded`、`project_terminal` 和只被投影使用的
  `_assess_evidence`，避免保留旧实现形成双真源。
- `XiaoweiRuntime` 在构造时用既有四个对象创建一个 `TaskViewRuntime`，`submit_task`、`query_task`
  和同步兼容路径的终态重投影全部委托它。
- internal-api、飞书和 Web 只装配 `TaskViewRuntime`；task-worker 才装配完整 `XiaoweiRuntime`。
- 契约测试对同一存储事实分别调用完整 Runtime 委托路径和窄 Runtime，断言 `TaskView` 完全相等。
- 安全 AST 测试封死 `task_view_runtime.py` 的 import 与方法调用面，证明它没有执行权。

### 4.3 任务读取、访问与提交

在 `contracts/task.py` 增加明确的读取 DTO，禁止 `actor: Optional[str]` 这种模糊 ACL：

```python
class ActorTaskPageQuery(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    actor: StrictStr
    before_created_seq: StrictInt | None = Field(default=None, gt=0)
    limit: StrictInt = Field(gt=0, le=100)


class ScopeTaskPageQuery(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    before_created_seq: StrictInt | None = Field(default=None, gt=0)
    limit: StrictInt = Field(gt=0, le=100)


class StoredTaskRead(Contract):
    record: TaskRecord
    submission: TaskSubmission


class StoredTaskPage(Contract):
    items: tuple[StoredTaskRead, ...]
    next_created_seq: StrictInt | None = Field(default=None, gt=0)
```

空页的 `next_created_seq` 必须为 `None`；非空页若存在下一页游标，它必须等于本页最后一条
`items[-1].record.created_seq`。非空页允许 `None` 表示没有下一页。这样服务端产出的游标才能
原样成为下一次查询的排他游标，且不会静默跳过或重复任务。`items` 的 `created_seq` 必须严格
降序；数据库已对该列施加唯一约束，因此契约不接受同值决胜这种数据库无法产出的页面。

`TaskStore` 增加三条纯读方法：

```python
async def get_submission(self, *, lookup: TaskLookup) -> TaskSubmission: ...
async def list_tasks_for_actor(self, *, query: ActorTaskPageQuery) -> StoredTaskPage: ...
async def list_tasks_for_scope(self, *, query: ScopeTaskPageQuery) -> StoredTaskPage: ...
```

- 排序固定为 `created_seq DESC, task_id DESC`；续页条件固定为
  `created_seq < before_created_seq`，游标只由服务端编码/校验。
- 两个列表读取都由 TaskStore 在同一次查询中关联不可变 `task_submissions`；TaskAccessService 只把
  `submission.as_of` 和脱敏限长的 request preview 投成安全 `TaskSummary`，不向 handler 返回
  TaskRecord、idempotency digest、lease 或原始 submission。
- `get_submission` 只能在 TaskAccessService 完成用户授权后，或 channel worker 持有同 scope 的有效
  projection claim 时调用，用于生成脱敏预览；外部 handler 不能直接调用。
- 任务详情由共享 `TaskViewRuntime.query_task(TaskLookup)` 产生 `TaskView`；完整
  `XiaoweiRuntime.query_task()` 也委托它，保证投影唯一。
- 普通用户列表只调 `list_tasks_for_actor`；admin 列表只在确认
  `ADMIN_ALL_SAFE_TASKS` 后调 `list_tasks_for_scope`。

访问与提交服务分别落在两个单一职责模块：`TaskAccessService` 与成员校验端口位于
`application/channel_access.py`；`ChannelSubmissionService` 位于
`application/channel_submission.py`。前者只做读取授权与安全投影，后者只做提交权限、服务端
幂等、任务提交和渠道绑定/订阅编排，不把命令与查询合并成一个宽泛的 `channel_tasks.py`。

接口形状：

```python
class TaskAccessService:
    async def get_task(self, *, query: TaskAccessQuery) -> AccessibleTask: ...
    async def list_tasks(self, *, query: TaskListQuery) -> TaskPage: ...


class ChannelSubmissionService:
    async def submit(self, *, command: ChannelSubmitCommand) -> SubmittedTask: ...
```

详情授权闭集：

```text
same tenant/environment
AND (
  ADMIN_ALL_SAFE_TASKS
  OR task.actor == principal.actor
  OR group binding exists AND current Feishu membership == true
)
ELSE not_found
```

- 未登录返回 401/`unauthorized`。
- 已登录但缺提交权限返回 403/`forbidden`。
- 单任务不存在、跨 scope、无查看权、成员查询失败统一返回 404/`not_found`，避免存在性泄露。
- `forbidden` 不用于任务详情，以免区分“存在但没权限”。

`unauthorized`、`forbidden` 在 PR 1 只进入共享错误体词汇表；现有 `internal-api` 是受信入口，
不新增不可达的鉴权分支，也不承诺产出这两个错误码。PR 6 的 Web app 真实路由必须产出并测试
401/`unauthorized`，PR 7 的提交路由必须产出并测试 403/`forbidden`；只直接调用
`error_body()` 不算路由行为证据。

### 4.4 渠道绑定与枚举闭集

```python
class ChannelKind(StrEnum):
    FEISHU_PRIVATE = "feishu_private"
    FEISHU_GROUP = "feishu_group"
    WEB = "web"


class DestinationKind(StrEnum):
    FEISHU_MESSAGE_CARD = "feishu_message_card"
    FEISHU_PRIVATE_NOTICE = "feishu_private_notice"


class ProjectionState(StrEnum):
    PENDING_INITIAL = "pending_initial"
    WAITING_TERMINAL = "waiting_terminal"
    DELIVERING_TERMINAL = "delivering_terminal"
    COMPLETED = "completed"
    DEAD_LETTER = "dead_letter"


class ProjectionErrorCode(StrEnum):
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_UNAUTHORIZED = "provider_unauthorized"
    PROVIDER_FORBIDDEN = "provider_forbidden"
    PROVIDER_INVALID_PAYLOAD = "provider_invalid_payload"
    PROVIDER_INTERNAL = "provider_internal"


class ChannelBinding(Contract):
    binding_id: StrictStr
    task_id: StrictStr
    tenant_id: StrictStr
    environment_id: StrictStr
    channel: ChannelKind
    initiator_subject_ref: StrictStr
    conversation_ref: StrictStr | None
    source_event_ref: StrictStr
    created_at: AwareDatetime
```

`source_event_ref` 保存服务端不可逆摘要或供应商稳定事件引用，不能保存整段事件正文。
ChannelStore 只提供：

```python
async def bind_task(self, *, command: BindTaskCommand) -> ChannelBinding: ...
async def get_group_binding(self, *, lookup: GroupBindingLookup) -> ChannelBinding: ...
```

同一 `(tenant, environment, channel, source_event_ref)` 幂等绑定同一 task；语义冲突 fail-closed。

### 4.5 服务端幂等键

飞书传 `event_id`，浏览器传稳定 `client_submission_id`；二者都不是 Runtime 最终幂等键。
服务端构造：

```text
channel:v1:sha256(canonical_json({
  tenant_id,
  environment_id,
  channel,
  actor,
  client_key_or_event_id
}))
```

请求正文继续由现有 `TaskStore.create_task()` 的 request digest 检查。同一作用域和客户端键但正文
不同必须返回 `idempotency_conflict`，不能静默复用旧任务。安全测试覆盖跨 actor、跨 tenant、
跨 environment、跨 channel 不碰撞，也覆盖伪造 Header/Cookie/事件字段不能替换作用域。

提交恢复顺序固定为：先授权并构造服务端信封，再用 scoped key 调 Runtime 幂等创建任务，随后在
ChannelStore 的一个事务中写 binding 与 subscription。若后一步失败，返回可重试的渠道错误；重试时
Runtime 找回同一任务，再补齐相同 binding/subscription。未绑定的孤儿任务不扩大任何查看权限，
也不能靠 task ID 被外部用户读取。

### 4.6 投影输入

```python
class FeishuProjectionInput(Contract):
    task_view: TaskView
    request_preview: NonEmptyText = Field(max_length=8192)
    task_version: StrictInt = Field(ge=0)
    detail_url: HttpsUrl
```

`HttpsUrl` 在 `AnyHttpUrl` 的既有主机与长度约束上进一步只允许 `https`。`request_preview` 是
服务端生成的展示文本，允许保留首尾空白，但不能为空；8192 与当前入口最大请求长度一致，为
投影输入建立独立安全上界。服务端先脱敏并限长，再由 Renderer 按更小的飞书卡片预算截断。
Renderer 不读取原始 submission，不读取 Evidence，不查数据库，不判断 capability。

飞书卡片投影只从该输入产生：标题、状态、请求摘要、安全答案/sections 摘要、限制、下一步、refs、
“还有更多证据”和详情 URL。Web 详情直接展示同一个 `TaskView.render` 的完整安全字段。

---

## 5. ProjectionSubscription 可靠投递设计

### 5.1 为什么不是一次性 outbox

任务创建时通常还不是终态。投递记录必须区分“任务尚未完成，等待即可”和“调用飞书失败，需要
退避”。若混为一次 delivery，会把正常等待累计成失败，也无法阻止陈旧 worker 覆盖终态卡片。

### 5.2 状态机

```text
PENDING_INITIAL --initial sent, nonterminal--> WAITING_TERMINAL
PENDING_INITIAL --terminal card sent---------> COMPLETED
WAITING_TERMINAL --task becomes terminal----> DELIVERING_TERMINAL
DELIVERING_TERMINAL --card sent--------------> COMPLETED
PENDING_INITIAL / DELIVERING_TERMINAL
  --nonretryable or retry budget exhausted---> DEAD_LETTER
```

- 飞书私聊/群聊任务从 `PENDING_INITIAL` 开始。
- 普通用户 Web 任务不发受理私聊，直接从 `WAITING_TERMINAL` 开始。
- admin Web 任务不创建通知订阅。
- `PENDING_INITIAL` claim 后若任务已终态，可直接发送终态卡片并进入 `COMPLETED`。
- `WAITING_TERMINAL` 只是轮询 TaskStore，不增加 provider failure count。
- `DELIVERING_TERMINAL` 只允许持有有效 claim/fencing 的 worker 完成。
- 可重试 provider failure 不创建额外业务状态：保持当前发送阶段并设置新的 `next_attempt_at`。
- `COMPLETED`、`DEAD_LETTER` 是渠道订阅终态，后到事件不能把它们重新打开。

### 5.3 持久化字段

```python
class ProjectionSubscription(Contract):
    subscription_id: StrictStr
    task_id: StrictStr
    destination_kind: DestinationKind
    destination_ref: StrictStr
    source_message_ref: StrictStr | None
    state: ProjectionState
    last_projected_task_version: StrictInt | None
    attempt_number: StrictInt = Field(ge=0)
    claim_owner: StrictStr | None
    claim_expires_at: AwareDatetime | None
    fencing_token: StrictInt | None
    next_attempt_at: AwareDatetime
    provider_failure_count: StrictInt = Field(ge=0)
    last_error_code: ProjectionErrorCode | None
    payload_digest: Sha256Hex | None
```

`destination_ref`、`source_message_ref` 是不透明引用；日志不可输出原值。存储命令包括：

- `create_projection_subscription`
- `list_due_projection_subscriptions`
- `claim_projection_subscription`
- `record_initial_projection`
- `schedule_task_recheck`
- `schedule_provider_retry`
- `complete_projection`
- `dead_letter_projection`

所有更新都带 `subscription_id + claim_owner + fencing_token + expected_state`。存储层返回 winner，
陈旧 claim 只能成为 loser，不能覆盖新状态。

### 5.4 发送前后的顺序

worker 每次执行：

1. claim 一条到期订阅，得到单调递增 fencing token。
2. 用 task ID 回读 TaskStore winner；不相信订阅里的旧版本。
3. 若任务未终态，调用 `schedule_task_recheck` 并保持 `WAITING_TERMINAL`，短延时后重新检查；
   不写 `last_error_code`，也不增加供应商失败计数。
4. 若任务终态，通过 Runtime 读取 `TaskView`，再读取已授权 submission 生成安全请求预览。
5. 紧邻发送前再次回读 `TaskRecord.version`；版本变化则放弃本次 payload 并重新投影。
6. 生成稳定 payload digest；同版本同目的地产生相同卡片内容。
7. 调用异步飞书端口；群/私聊来源任务更新同一 message，Web 来源任务发送一次私聊。
8. 以 claim/fencing 提交 `COMPLETED`；丢失 claim 的提交不生效。

TaskStore 终态本身不可回退；投影只发送“受理”和“终态”，因此旧 running 卡片不会在终态后再次
被计划发送。供应商调用成功但数据库提交前崩溃仍可能导致 Web 私聊重复，这是 at-least-once 的
残余风险；M7 不宣称 exactly-once。

### 5.5 超时、重试和限流基线

以下是 V0.2 待审核的保守默认值，实施时写入 typed settings 并可回滚：

| 参数 | 默认值 |
| --- | --- |
| 单次飞书 API timeout | 5 秒 |
| projection claim lease | 15 秒，必须大于调用超时加提交余量 |
| provider 最大尝试次数 | 3 次 |
| 默认 backoff | 1 秒、2 秒、4 秒 |
| `Retry-After` 上限 | 30 秒 |
| 每 tenant 并发出站调用 | 最多 2 |
| 同一 subscription | 依靠 claim/fencing 严格串行 |
| task 未终态轮询间隔 | 2 秒起，最高 10 秒，不计 provider failure |

- 429、网络错误、超时、5xx：可重试。
- 认证失败、权限不足、无效卡片 payload 等确定性 4xx：直接 `DEAD_LETTER`。
- 错误日志仅记录闭集错误码、HTTP 状态类别、trace/task/subscription 摘要，不记录响应正文。
- 不承诺同一 destination 下不同任务全局串行；它们是独立 subscription，可在 tenant 并发预算内并行。
- 任务结果不受渠道重试次数影响；投递失败只体现在渠道审计和运维告警。

---

## 6. 飞书 SDK、身份和长连接

### 6.1 已核验的依赖事实

实施前的只读依赖核验得到：

- PyPI `lark-oapi==1.7.3` wheel SHA256：
  `c91f00087b7977dc9059ab492e8fe435e1a873863dca1d4e660d2be5b801e4cd`。
- wheel 不包含 `py.typed`，不能让 strict mypy 直接遍布 SDK 调用。
- 官方 SDK 提供 `lark_oapi.ws.Client` 长连接客户端。
- 消息 create/patch/get 路径有 async 方法，可在 channel worker 使用异步调用，不需要
  `asyncio.to_thread` 包装出站 API。
- 长连接客户端管理自己的事件循环，握手路径含同步调用；必须在独立 `feishu-listener` 进程运行，
  不能嵌入 FastAPI 主事件循环。
- SDK INFO 日志可能记录完整连接 URL；必须关闭/过滤该 logger。

实现 PR 必须在 `uv.lock` 中锁定完整传递依赖，并运行依赖审计；若下载产物的 digest、API 形状或
许可证与本节不一致，先停工更新计划，不用兼容胶水掩盖漂移。

### 6.2 单一 typed SDK seam

只有 `src/xiaowei_agent/interfaces/feishu_sdk.py` 可以动态加载 `lark_oapi`：

```python
class FeishuInboundTransport(Protocol):
    def run_forever(self, *, on_event: FeishuEventHandler) -> None: ...
```

- `FeishuMembershipPort` 定义在 `application/channel_access.py`，`ChannelMessagePort` 定义在
  `application/channel_projection.py`；应用层只依赖这两个本地 typed Protocol，绝不反向 import
  `interfaces`。SDK adapter 以结构化类型实现它们。
- `FeishuInboundTransport` 只被同层 listener 使用，可以留在 SDK seam 内。
- 该叶子适配器使用 `importlib.import_module()`，把 SDK 对象立即转换成本地严格 DTO。
- 不在全局 mypy 配置增加宽泛 `ignore_missing_imports`，不散布 `Any`，不在其他模块写
  `# type: ignore` 绕过。
- 安全 AST 测试禁止其他文件直接 import 或字符串加载 `lark_oapi`。
- 不采用高层 `lark-channel-sdk` 的自带 agent/policy 链，避免复制本项目 Runtime 与安全治理。

### 6.3 长连接事件处理

`feishu-listener` 只处理消息事件的严格闭集：

1. SDK 解析长连接帧；本地 DTO 再做 `extra="forbid"` 与大小限制。
2. 验证 app/tenant 归属、event type、event ID、chat type、sender 和 message type。
3. 私聊文本进入候选提交；群聊必须明确提及机器人并移除 mention 协议片段。
4. 通过身份目录把 Feishu subject 映射为 `AuthenticatedPrincipal`；未知或无提交权限 fail-closed。
5. 用服务端 scoped idempotency key 调 `ChannelSubmissionService`。
6. 原子写 ChannelBinding 与 ProjectionSubscription；事件重放得到同一 task/subscription。
7. 返回处理成功只代表“事件已持久化/已幂等确认”，不代表任务完成。

不记录原始事件正文。事件中的 tenant、actor、environment、role、policy revision 字段全部视为外部
不可信输入，不能覆盖服务端映射。

### 6.4 OAuth 与当前群成员校验

- Web 只接受飞书企业 OAuth；OAuth state 为单次、短时、服务端 digest 存储。
- callback 用 code 换身份后，必须再次经本地身份目录解析 tenant/environment/actor/permissions。
- 浏览器只得到随机 session cookie；服务端保存 cookie digest、Feishu subject ref、签发/过期/撤销时间，
  不保存明文 cookie，也不持久化 permission/tenant/environment 快照。每次请求都用 subject ref 从
  当前进程的身份目录重新构造 principal。
- 群任务详情的每一次 `GET /app/api/tasks/{task_id}`（首次读取和后续轮询都包括）都调用
  `is_current_group_member`；权限撤销必须在下一次读取生效，M7 不做跨请求成员缓存。
- 所需飞书应用权限在真实注册前按官方控制台现状逐项核对并记录，不在计划里猜 scope 名称。
- 注册应用、授权 scope、真实登录和真实群成员查询都需要项目负责人单独授权；fake 离线实现不受阻。

---

## 7. Web 工作台与接口规格

### 7.1 Web 路由闭集

`web-app` 只注册：

```text
GET  /app
GET  /app/tasks/{task_id}
GET  /oauth/feishu/start
GET  /oauth/feishu/callback
GET  /app/api/me
GET  /app/api/tasks
POST /app/api/tasks
GET  /app/api/tasks/{task_id}
POST /app/api/logout
GET  /healthz
GET  /readyz
```

- 未列出的方法和路由统一拒绝。
- `/app` 返回桌面工作台 shell；`/app/tasks/{task_id}` 返回独立只读详情 shell。详情 shell 只包含
  状态时间线与安全 `RenderPayload`，不含 textarea、提交表单、CSRF token、任务列表或配置导航，
  也不加载任务提交 JS；详情数据仍通过授权 API 拉取。
- Web JSON 复用 `TaskView.status` 与 `TaskView.render`，但不把现有内部
  `TaskView.query_path=/v1/tasks/...` 发给浏览器；Web transport 另给同源
  `/app/tasks/{task_id}`。这只是链接投影，不改变 Runtime 结果。
- `web-app` 对 `/v1/tasks`、`/v1/tasks/{id}` 和内部调试路径必须 404。
- `internal-api` 不注册 `/app`、OAuth 或 `/app/api/*`。
- 生产反向代理只暴露 `web-app`；内部 API 不依赖“路径没人知道”保护。

### 7.2 HTTP 安全契约

- Cookie：`Secure`、`HttpOnly`、`SameSite=Lax`、host-only、短时过期，登录后轮换。
- 所有状态变更 POST 检查 Origin、CSRF token 和 JSON Content-Type。
- CSP 至少为 `default-src 'self'`，CSS/JS 独立静态文件，不使用 inline script。
- JSON body 沿用全局大小限制；任务文本最大 8192 字符。
- 浏览器发送 `client_submission_id`，服务端生成最终 idempotency key。
- 所有用户文本通过 `textContent` 渲染；禁止 `innerHTML` 注入服务端/工具/用户文本。
- URL 中不放 session、subject、permission、tenant、environment 或任何 secret。
- 401 用 `unauthorized`，无提交权限用 `forbidden`，任务不可见统一 `not_found`。

### 7.3 信息架构

桌面布局：

```text
┌──────────────────────────────────────────────────────────────────┐
│ 小维 · 运维任务工作台     环境标识        当前用户       退出   │
├──────────────────┬───────────────────────────────────────────────┤
│ 我的任务         │ 小维 AI                                       │
│                  │ ┌───────────────────────────────────────────┐ │
│ 状态筛选         │ │ 你想让我检查什么？                        │ │
│ 最近任务列表     │ └───────────────────────────────────────────┘ │
│ admin: 全部任务  │ [发送任务]                                    │
│                  │                                               │
│                  │ 当前任务：状态时间线 / 安全结果 / 下一步      │
└──────────────────┴───────────────────────────────────────────────┘
```

M7 不适配手机端 AI 工作台、任务发起、任务列表和后台配置。仅从飞书卡片进入的单任务详情需要
在窄屏可读，内容限定为任务状态与安全 `RenderPayload`；关键状态和证据也不能依赖 hover 才显示。

`/app` 桌面工作台声明最小支持视口宽度为 1280px；低于该宽度不保证工作台布局，但服务端 HTML
必须提供静态可读的“请使用宽度至少 1280px 的桌面浏览器”提示，该提示不依赖 hover 或 JS。
`/app/tasks/{task_id}` 的独立只读详情 shell 不受这一下界限制，仍须在窄屏可读。

### 7.4 视觉与组件规范

- 视觉方向：沉稳的深蓝灰导航 + 明亮中性内容区 + 低饱和状态色，避免“监控大屏”式高噪音。
- 内容最大宽度 1440px；8px 间距系统；圆角 10/14px 两级；阴影只用于浮层和当前任务。
- 字体优先系统中文字体；正文 14-16px；结果标题 20px；最小可点击区域 44px。44px 即使在桌面端
  也保留，用于键盘/鼠标可访问性和更稳定的点击目标，不代表要适配完整手机端工作台。
- 状态颜色必须同时配图标和文字，不能只依赖颜色：
  - waiting/running：蓝色 + 旋转/进行中图标；
  - succeeded：绿色 + 勾；
  - failed/rejected/canceled/indeterminate：分别用闭集文案，不合并成“失败”。
- 安全结果按 `answer → sections → next_steps → refs` 排列，不自行改写内容。
- `refs` 是证据引用，不渲染成可猜测的内部对象下载 URL。
- loading 使用骨架屏；空态给出能力示例但不承诺未实现功能；网络错误提供“重试读取”，不提供
  “重新执行任务”。
- 输入框支持 Ctrl/Cmd+Enter 发送；提交期间禁止重复按钮，但网络重试复用同一
  `client_submission_id`。

### 7.5 前端状态机

```text
signed_out → oauth_redirect → authenticated

桌面工作台：
authenticated → empty | loading_list | list_ready | list_error
draft → submitting → accepted(task_id) | submit_error
accepted → polling → terminal | read_error

独立只读详情 shell：
detail_loading → detail_ready(running) | detail_ready(terminal) | detail_error
detail_ready(running) → detail_polling → detail_ready(running) | detail_ready(terminal) | detail_error
```

- 桌面工作台轮询从 2 秒开始退避，最高 10 秒；独立详情 shell 从 5 秒开始退避，最高 15 秒，
  以控制群成员实时校验的供应商调用成本。
- 页面隐藏时两种轮询都降至至少 30 秒，恢复可见后立即拉取一次；终态后停止轮询。
- 群任务详情的每次轮询请求都重新鉴权并实时校验当前群成员，不复用首次打开时的授权结果。
- `read_error` 不把任务标成 failed；任务真相仍来自下一次 TaskView。
- `detail_error` 不改写任务状态；权限被撤销时清除已渲染详情并显示统一不可访问状态。
- 页面刷新后从 URL 或列表恢复 task ID，不在 localStorage 保存 principal、permission 或结果正文。

---

## 8. 八个 PR 的 TDD 实施顺序

每个 PR 从最新 `main` 单独创建 `claude/<topic>` 分支，由 Claude 实现；Codex 按精确 SHA 审查；
项目负责人决定是否合并。后一个 PR 必须基于前一个已审通过的主线，不自行堆叠未审核分支。

PR 1–8 均受 §0.3.1 离线实现门约束，并继续按顺序逐 PR 实现、审查和合入；§0.3.2 只约束真实
应用注册、凭据读取、网络连接、部署和 canary。任何离线 PR 合入都不能被描述成飞书/Web 渠道
已经部署、真实可用或通过用户验收。

### PR 1：ADR、权限与读取契约

**建议分支：** `claude/m7-channel-contracts`

**Files:**

- Create: `docs/adr/ADR-013-m7-channel-boundary.md`
- Modify: `README.md`
- Create: `src/xiaowei_agent/contracts/channel.py`
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/task.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/interfaces/http_models.py`
- Create: `tests/unit/test_channel_contracts.py`
- Modify: `tests/unit/test_render_payload.py`
- Modify: `tests/contract/test_api_contract.py`
- Create: `tests/security/test_channel_contract_boundaries.py`
- Modify: `tests/security/test_module_layering.py`

**Steps:**

1. 写失败测试钉死三个权限、principal 严格字段、分页 DTO、投影输入，以及
   `DestinationKind`、`ProjectionState`、`ProjectionErrorCode` 三个枚举闭集；未知字段和值一律拒绝。
   渠道枚举继续集中定义在 `contracts/enums.py`，不破坏既有枚举单一真源；契约包分层继续由中央
   allowlist 承重，并补齐其对 `importlib.import_module` / `__import__` 动态加载的盲区，不另写更弱的
   渠道专用 denylist。
2. 写失败测试证明 `unauthorized`/`forbidden` 作为后续 Web app 预留错误码加入闭集，且共享错误体
   不带 message/输入字段；不把直接调用 `error_body()` 描述成现有 internal-api 已具备鉴权行为。
3. 最小实现 contracts 和 error code；不创建身份 adapter。
4. ADR-013 记录进程隔离、长连接选择、渠道出站不进 ToolGateway、TaskStore/ChannelStore 真源边界、
   webhook 和高层 SDK 被拒绝的原因、未来变更门。
5. 运行：

```bash
python -m pytest tests/unit/test_channel_contracts.py tests/contract/test_api_contract.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

6. 只暂存上列文件，提交：`docs(m7): define thin channel contracts and boundary`。

### PR 2：抽取无执行权的 TaskViewRuntime

**建议分支：** `claude/m7-task-view-runtime`

**Files:**

- Create: `src/xiaowei_agent/application/task_view_runtime.py`
- Modify: `src/xiaowei_agent/application/__init__.py`
- Modify: `src/xiaowei_agent/application/runtime.py`
- Modify: `src/xiaowei_agent/interfaces/api.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `src/xiaowei_agent/runners/__init__.py`
- Create: `tests/contract/test_task_view_runtime.py`
- Modify: `tests/contract/test_runtime_async_lifecycle.py`
- Modify: `tests/unit/test_local_stack.py`
- Create: `tests/security/test_task_view_runtime_authority.py`
- Modify: `AGENT_HANDOFF.md`
- Modify: `README.md`
- Modify: `docs/plans/M7-web-feishu-channels.md`

**Interfaces:**

- Consumes: 现有 `TaskStore`、`PlanStore`、`EvidenceLedger`、`CapabilityBindingRegistry`。
- Produces: 第 4.2 节定义的 `TaskViewRuntime.submit_task()`、`query_task()`、
  `project_recorded()`；后续所有渠道 stack 只装配这一窄 Runtime。

**Steps:**

- [ ] **Step 1：先写 Runtime 委托失败测试。**
  对同一持久化终态分别调用 `XiaoweiRuntime.query_task()` 和 `TaskViewRuntime.query_task()`，断言
  完整 `TaskView` 相等；monkeypatch 唯一 `project_terminal` 后，两条路径都必须命中同一函数。
- [ ] **Step 2：运行红灯。**

```bash
python -m pytest tests/contract/test_task_view_runtime.py \
  tests/contract/test_runtime_async_lifecycle.py -q
```

预期：因 `task_view_runtime` 不存在或旧 Runtime 尚未委托而失败。

- [ ] **Step 3：抽取最小实现。**
  移动任务投影函数，删除 `runtime.py` 中旧副本；`XiaoweiRuntime` 用既有四个依赖创建并委托
  `TaskViewRuntime`，执行主链保持原样。
- [ ] **Step 4：把内部 API 改成窄装配。**
  在 `local_stack.py` 增加 `TaskViewStack` 和 `build_postgres_task_view_stack()`；把 tool/runner/目标
  adapter import 收进完整执行装配函数内部；`interfaces/api.py` 改用窄 builder，worker 仍使用
  `build_postgres_local_stack()`。同时移除 `application` 与 `runners` 包入口的 eager re-export；Python
  加载子模块前会先执行包入口，保留 eager re-export 会让窄 Runtime 间接加载完整 Runtime/Runner。
- [ ] **Step 5：写权限反证。**
  AST 测试断言 `task_view_runtime.py` 不 import/call governance、tools、runners、目标 adapter、
  `TaskStore.transition()`；stack 测试断言 `TaskViewStack` 不含完整 Runtime/Runner/Gateway 字段；独立
  进程测试断言导入 internal-api 并装配窄 stack 不加载完整 Runtime、Runner、Gateway 或目标 adapter。
- [ ] **Step 6：运行绿灯与安全门。**

```bash
python -m pytest tests/contract/test_task_view_runtime.py \
  tests/contract/test_runtime_async_lifecycle.py tests/unit/test_local_stack.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

- [ ] **Step 7：只暂存本 PR 文件并提交。**

```bash
git add src/xiaowei_agent/application/task_view_runtime.py \
  src/xiaowei_agent/application/__init__.py \
  src/xiaowei_agent/application/runtime.py \
  src/xiaowei_agent/interfaces/api.py \
  src/xiaowei_agent/interfaces/local_stack.py \
  src/xiaowei_agent/runners/__init__.py \
  tests/contract/test_task_view_runtime.py \
  tests/contract/test_runtime_async_lifecycle.py \
  tests/unit/test_local_stack.py \
  tests/security/test_task_view_runtime_authority.py \
  AGENT_HANDOFF.md README.md docs/plans/M7-web-feishu-channels.md
git commit -m "refactor(m7): isolate task view runtime from execution"
```

### PR 3：TaskStore 读取、ChannelStore、访问与提交服务

**建议分支：** `claude/m7-channel-access`

**Files:**

- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `src/xiaowei_agent/persistence/store.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`
- Create: `src/xiaowei_agent/persistence/channel.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0006_channels.py`
- Create: `src/xiaowei_agent/application/channel_access.py`
- Create: `src/xiaowei_agent/application/channel_submission.py`
- Create: `tests/suites/channel_store.py`
- Modify: `tests/suites/task_store.py`
- Create: `tests/contract/test_channel_store.py`
- Create: `tests/contract/test_task_read_store.py`
- Create: `tests/contract/test_channel_access.py`
- Create: `tests/contract/test_channel_submission.py`
- Create: `tests/contract/test_channel_migration.py`
- Create: `tests/integration/test_channel_store_postgres.py`
- Create: `tests/security/test_channel_access_control.py`
- Create: `tests/security/test_task_submission_read_boundary.py`

**Steps:**

1. 先在 shared suites 写失败用例：actor/admin 分页、scope 隔离、稳定倒序游标、submission 作用域读取。
2. 写 ChannelStore 失败用例：绑定幂等/冲突、订阅 claim/fencing、终态保护、task 等待不计 provider failure。
3. 创建 rev_0006；先跑 upgrade/downgrade/schema 对齐失败测试，再实现 fake/PostgreSQL 同语义。
4. 在 `test_channel_access.py` 写 TaskAccessService 失败用例：owner/admin/current-group-member 允许，
   其余统一 not_found；成员端口异常也 fail-closed。
5. 在 `test_channel_submission.py` 写 ChannelSubmissionService 失败用例：权限、服务端幂等 digest、
   `TaskViewRuntime` 重放、绑定恢复和 admin Web 任务不建通知订阅。
6. 最小实现，不在 ChannelStore 复制 task owner/status/version/request text；在 `_conformance.py`
   同时锚定内存/PostgreSQL ChannelStore 实现。
7. 新增 AST 调用点闭集：本 PR 暂时只允许 `application/channel_access.py` 调用
   `TaskStore.get_submission()`；PR 5 创建 projection 服务时把第二个允许点精确加入，最终闭集只能是
   `channel_access.py`、`channel_projection.py`。
8. 运行：

```bash
python -m pytest tests/contract/test_task_read_store.py tests/contract/test_channel_store.py \
  tests/contract/test_channel_access.py tests/contract/test_channel_submission.py \
  tests/contract/test_channel_migration.py -q
python -m pytest tests/integration/test_channel_store_postgres.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

9. 提交：`feat(m7): add scoped task access and channel subscriptions`。

### PR 4：飞书 SDK seam、身份和长连接 listener

**离线开工门：** 满足 §0.3.1 后可以创建本 PR 分支，核验并锁定 `lark-oapi`，使用 fake SDK seam
实现默认关闭的 listener。§0.3.2 未满足时不得注册应用、读取真实 secret、连接飞书或启动真实
listener；依赖安装和离线 contract 测试不构成真实渠道授权。

**建议分支：** `claude/m7-feishu-listener`

**Files:**

- Modify: `.env.example`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/security/test_dependency_baseline.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Create: `src/xiaowei_agent/interfaces/feishu_sdk.py`
- Create: `src/xiaowei_agent/interfaces/feishu_identity.py`
- Create: `src/xiaowei_agent/interfaces/feishu_listener.py`
- Modify: `tests/security/test_module_layering.py`
- Create: `tests/fakes/feishu.py`
- Create: `tests/unit/test_feishu_config.py`
- Modify: `tests/unit/test_local_stack.py`
- Create: `tests/contract/test_feishu_sdk_seam.py`
- Create: `tests/contract/test_feishu_identity.py`
- Create: `tests/contract/test_feishu_listener.py`
- Create: `tests/security/test_feishu_ingress.py`
- Create: `tests/security/test_feishu_sdk_boundary.py`
- Create: `tests/security/test_feishu_log_redaction.py`

**Steps:**

1. 写依赖锁与 seam 失败测试：只有 `feishu_sdk.py` 可加载 SDK，其他入口只能依赖本地 Protocol；
   同一提交把 `lark-oapi` 加入 `test_dependency_baseline.py` 的运行依赖精确集合，其他新增直接依赖
   仍须被集合相等守卫拒绝。
2. 写配置失败矩阵：feature 默认关、缺 app credential file reference、映射未知标签、重复 actor、
   跨 scope 配置；每个新增字段同时登记 `_FIELD_TO_ENV` 与 `.env.example`，并运行安全测试证明两边
   集合相等。示例文件只写空值或 secret 文件路径，不写真实凭据形状。
3. 写身份失败测试：事件字段不能覆盖 actor/tenant/environment/permissions；未知用户拒绝。
4. 写 listener 失败测试：群聊无 mention 忽略、非文本/超限拒绝、重复 event 幂等、冲突 fail-closed。
5. 写日志反证：伪造连接 URL、token/ticket 形状和 provider 响应体不进入捕获日志。
6. 最小实现动态 SDK seam 和独立同步 listener 进程；模块自身提供 `main()` 与
   `if __name__ == "__main__"`，运行方式固定为
   `python -m xiaowei_agent.interfaces.feishu_listener`，不增加 `[project.scripts]`。
7. 在 `local_stack.py` 增加 `build_postgres_feishu_listener_stack()`：只装配 TaskViewRuntime、
   ChannelStore、IdentityDirectory、ChannelSubmissionService 和 FeishuInboundTransport；不得调用完整
   `build_postgres_local_stack()`，返回类型不得含 Runner/Gateway/目标 adapter。
8. 同一提交更新 `test_module_layering.py`，为三个新入口分别给最小 import 白名单；在
   `_conformance.py` 锚定生产 FeishuMembershipPort 与 FeishuInboundTransport 实现。
9. 运行：

```bash
python -m pytest tests/contract/test_feishu_sdk_seam.py \
  tests/contract/test_feishu_identity.py tests/contract/test_feishu_listener.py -q
python -m pytest -m security -q
ruff check .
mypy src
python -m pip_audit
```

10. 提交：`feat(m7): add typed Feishu long-connection ingress`。

### PR 5：飞书卡片与 projection worker

**建议分支：** `claude/m7-feishu-projection`

**Files:**

- Modify: `.env.example`
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Create: `src/xiaowei_agent/rendering/feishu.py`
- Create: `src/xiaowei_agent/application/channel_projection.py`
- Create: `src/xiaowei_agent/interfaces/feishu_worker.py`
- Modify: `tests/security/test_module_layering.py`
- Create: `tests/unit/test_feishu_rendering.py`
- Modify: `tests/unit/test_local_stack.py`
- Create: `tests/contract/test_channel_projection_worker.py`
- Create: `tests/security/test_feishu_projection_safety.py`
- Create: `tests/security/test_projection_fencing.py`
- Modify: `tests/security/test_task_submission_read_boundary.py`

**Steps:**

1. 写 card snapshot/结构失败测试：受理、各终态、截断、“还有更多证据”、详情 URL、无 raw rows。
2. 写 worker 状态机失败测试：等待任务与 provider failure 分开、版本变化重投影、陈旧 fencing 失败、
   同一 subscription 串行、terminal subscription 不重开；不写无法兑现的 destination 全局串行断言。
3. 写 provider 失败矩阵和 5s/15s/3 次/退避/Retry-After 上限测试，使用 fake clock/sleep/client。
4. 写 TDD 反证：临时移除发送前 version re-read 或 fencing 条件时，对应用例必须变红。
5. 每个新增投递设置（含安全 Web 详情 base URL）同时登记 `_FIELD_TO_ENV` 与
   `.env.example`，安全门证明集合相等；base URL 必须是配置的 HTTPS origin，不能由事件、Host
   Header 或 Forwarded Header 拼出。
6. 最小实现；投递 worker 只读 TaskViewRuntime/TaskStore，不触发任务状态迁移；模块入口固定为
   `python -m xiaowei_agent.interfaces.feishu_worker`，不增加 `[project.scripts]`。
7. 在 `local_stack.py` 增加 `build_postgres_channel_worker_stack()`，只装配窄 task-view stack、
   ChannelStore、ChannelProjectionService 和 ChannelMessagePort；stack 类型不得出现执行权对象。
8. 同一提交登记 `interfaces/feishu_worker.py` 最小 import 白名单，在 `_conformance.py` 锚定生产
   ChannelMessagePort 实现，并把 `get_submission` AST 调用点闭集更新为且仅为
   `channel_access.py`、`channel_projection.py`。
9. 运行：

```bash
python -m pytest tests/unit/test_feishu_rendering.py \
  tests/contract/test_channel_projection_worker.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

10. 飞书离线闭环到此做一次独立精确 SHA 评审；通过后才开始 Web PR。
11. 提交：`feat(m7): project task outcomes to Feishu cards`。

### PR 6：Web OAuth 与 session

**建议分支：** `claude/m7-web-auth`

**Files:**

- Modify: `.env.example`
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Create: `src/xiaowei_agent/persistence/web_session.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0007_web_sessions.py`
- Create: `src/xiaowei_agent/interfaces/web_auth.py`
- Create: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `tests/security/test_module_layering.py`
- Create: `tests/contract/test_web_session_store.py`
- Create: `tests/contract/test_web_auth.py`
- Create: `tests/contract/test_web_app_routes.py`
- Modify: `tests/unit/test_local_stack.py`
- Create: `tests/integration/test_web_session_postgres.py`
- Create: `tests/security/test_web_auth_boundary.py`

**Steps:**

1. 写 rev_0007 upgrade/downgrade 与 digest-only session 失败测试。
2. 写 OAuth state 单次、过期、重放、code 错误、未知身份、session rotation 与注销测试。
3. 写 Cookie/Origin/CSRF/CSP 失败测试和 route 闭集；必须通过 Web app 的受保护真实路由断言
   未登录返回 401/`unauthorized`，不能只直接调用 `error_body()`；每个 Web/OAuth/session 设置
   同时登记 `_FIELD_TO_ENV` 与 `.env.example`，安全门证明集合相等。
4. 写关键隔离测试：`web-app` 不注册 `/v1/tasks/*`；`internal-api` 不注册 `/app` 和 OAuth。
5. 最小实现 Web app factory，只完成 auth 和空壳 `/app`，不实现任务业务路由；模块入口固定为
   `python -m xiaowei_agent.interfaces.web_app`，不增加 `[project.scripts]`。
6. 在 `local_stack.py` 增加 `build_postgres_web_stack()`：只装配窄 task-view stack、ChannelStore、
   WebSessionStore、IdentityDirectory、TaskAccessService、ChannelSubmissionService 和 OAuth/Membership
   ports；stack 类型不得出现完整 Runtime/Runner/Gateway/目标 adapter。
7. 同一提交登记 `web_auth.py`、`web_app.py` 的最小 import 白名单，并在 `_conformance.py` 锚定
   内存/PostgreSQL WebSessionStore 实现。
8. 运行：

```bash
python -m pytest tests/contract/test_web_session_store.py tests/contract/test_web_auth.py \
  tests/contract/test_web_app_routes.py -q
python -m pytest tests/integration/test_web_session_postgres.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

9. 提交：`feat(m7): add isolated Feishu-authenticated web app`。

### PR 7：Web 运维任务工作台

**建议分支：** `claude/m7-web-workbench`

**Files:**

- Create: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Create: `src/xiaowei_agent/interfaces/web_static/index.html`
- Create: `src/xiaowei_agent/interfaces/web_static/detail.html`
- Create: `src/xiaowei_agent/interfaces/web_static/app.css`
- Create: `src/xiaowei_agent/interfaces/web_static/app.js`
- Create: `src/xiaowei_agent/interfaces/web_static/detail.js`
- Modify if required: `pyproject.toml`（先实测 wheel 内容；只有 hatchling 默认未包含静态资源时才补
  最小打包声明，否则该文件保持零 diff；不重复定义进程入口）
- Modify: `tests/security/test_module_layering.py`
- Create: `tests/contract/test_web_task_api.py`
- Create: `tests/contract/test_web_static_assets.py`
- Create: `tests/contract/test_web_detail_shell_scope.py`
- Create: `tests/security/test_web_task_access.py`
- Create: `tests/security/test_web_xss.py`

**Steps:**

1. 写 Web API 失败测试：普通用户只列自己、admin 列 scope、群详情首次读取与每次轮询都校验当前
   成员、成员撤销后下一次读取统一 404、非法游标和其他不可见任务统一 404。
2. 写提交失败测试：通过 Web app 的真实提交路由断言缺提交权限返回 403/`forbidden`，并覆盖
   CSRF、server-scoped idempotency、网络重试复用任务、正文冲突 409。
3. 写静态资源安全失败测试：两个 shell 都无 inline script、无 `innerHTML`、满足 CSP、静态路径
   闭集且 `web_static/` 无 `__init__.py`；先检查实际 wheel 是否已包含 HTML/CSS/JS，只有失败证据
   证明 hatchling 默认遗漏时才修改 `pyproject.toml`。
4. 写只读详情 shell 范围失败测试：`/app/tasks/{task_id}` 不含 textarea、提交表单、CSRF token、
   列表/配置导航或 `app.js`，只加载 `detail.js`；`detail.js` 不包含任务提交路径或 POST 请求。
5. 先实现 JSON API，再分别实现桌面工作台 shell 与独立只读详情 shell；两者共享 `app.css`，但
   使用不同 JS，不把权限判断放入前端。
6. 实现桌面端空/加载/提交/运行/终态/读取错误状态；终态严格展示 `RenderPayload`。窄屏仅实现
   飞书链接进入的单任务详情可读态，不实现手机端工作台、发起、列表或配置。
7. `web_static/` 不创建 `__init__.py`；同一提交只为 `web_models.py` 增加接口白名单。
8. 运行：

```bash
python -m pytest tests/contract/test_web_task_api.py \
  tests/contract/test_web_static_assets.py \
  tests/contract/test_web_detail_shell_scope.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

9. 使用 fake 数据在 1280/1440 桌面宽度检查工作台，并在窄屏检查飞书链接单任务详情可读性；
   截图只算 UI 检查，不算部署或用户验收。
10. 提交：`feat(m7): add Xiaowei operations task workbench`。

### PR 8：跨渠道一致性、Compose 与 canary 资产

**建议分支：** `claude/m7-channel-integration`

**Files:**

- Modify: `docker-compose.yml`
- Modify: `docker-compose.smoke.yml`
- Modify: `scripts/compose_smoke.py`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Create: `tests/contract/test_channel_render_parity.py`
- Create: `tests/integration/test_m7_channel_flow.py`
- Create: `tests/evals/test_m7_channel_safety.py`
- Create: `tests/evals/corpus/m7_channel_safety.json`
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `DEVELOPMENT_PLAN.md`（只在稳定计划口径确有变化时）
- Modify: `AGENT_HANDOFF.md`
- Create: `docs/handoff/M7-acceptance-report.md`

**Steps:**

1. 写 parity 失败测试：同一 task 经 API、CLI、飞书、Web 得到相同 status、完整安全字段与 refs；
   卡片仅因 provider 预算做显式截断。
2. 写 Compose 失败测试：五个进程命令/端口、web/internal route 隔离、listener 无入站端口、flag 默认关。
3. 写集成闭环：fake Feishu event → 同一 TaskViewRuntime 提交 → 完整 Runtime/worker 执行 →
   TaskStore terminal → 原卡片更新 → Web 详情；
   Web submit → terminal → 普通用户私聊一次；admin 无额外私聊。
4. 写安全 eval：伪造身份、跨 scope、旧群成员、任务 ID 猜测、外部文本注入、数据库行诱导、
   provider 乱序/重试/陈旧 claim。
5. 最小 Compose 和文档更新；不要把离线结果写成真实飞书验证。
6. 运行最终四门和 Compose smoke：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
docker compose -f docker-compose.yml -f docker-compose.smoke.yml up \
  --build --abort-on-container-exit --exit-code-from smoke
```

7. 运行 TDD 反证：至少撤掉 actor/scope 授权、成员 fail-closed、projection fencing、Web route 隔离中
   的承重保护，分别确认对应测试变红；变异文件不得提交。
8. 生成精确 SHA 的验收报告，明确“离线/Compose 已验证”和“真实飞书未验证”。
9. 提交：`test(m7): prove cross-channel runtime parity`。

---

## 9. 真实飞书 canary 计划

PR 1–8 离线实现和评审完成后，才进入本节。真实飞书 app 注册只有在项目负责人单独授权后才可
准备；离线实现不要求也不得读取真实凭据。§0.3.2 未满足前，不得注册应用、连接飞书、部署渠道
进程或执行 canary。

### 9.1 前置核对

- [ ] 被 canary 的代码 SHA 与已审 SHA 完全一致。
- [ ] 依赖锁、镜像 digest、数据库 migration 版本有记录。
- [ ] 飞书 app、租户、测试群、测试用户和权限 scope 由项目负责人明确授权。
- [ ] 只使用测试 tenant/environment，不连接生产运维目标。
- [ ] feature flag、回滚命令、日志脱敏检查和告警可用。
- [ ] `web-app` ingress 与 `internal-api` 网络隔离已现场验证。

### 9.2 场景顺序

1. 指定运维用户在私聊发送一条已 canary 的真实只读能力请求。
2. 确认飞书只返回受理卡片和终态更新，task ID/trace/SHA 可关联。
3. 在测试群 `@小维` 发相同请求，确认原群卡片更新且所有群成员可见安全摘要。
4. 群成员经 Web 登录打开详情，确认看到完整安全 RenderPayload。
5. 将该成员移出群，再访问旧链接，确认统一 404。
6. 普通用户从 Web 提交，确认终态后一条私聊通知；admin 提交确认没有额外私聊。
7. 重放同一飞书 event 与 Web client key，确认不创建第二个任务。
8. 注入 provider 429/超时与 worker 重启，确认任务终态不变、投影按预算恢复、无 secret 日志。
9. 尝试 `/v1/tasks` 访问 Web ingress，确认不可达/404；从外部网络验证内部 API 不暴露。

每个场景记录：部署 SHA、镜像 digest、时间、操作者、task ID、trace ID、订阅 ID 摘要、脱敏截图、
预期与观察结果。测试通过不等于用户验收；最后仍需项目负责人明确验收。

### 9.3 回滚

1. 关闭 Web 与飞书 feature flag，停止 `web-app`、`feishu-listener`、`channel-worker`。
2. 保留 TaskStore 与 ChannelStore 数据用于审计，不回退或改写已完成任务。
3. internal API、task worker 和既有 Runtime 继续运行；渠道失败不应影响任务执行。
4. 数据库 schema 只有在确认新表无必须保留数据并经项目负责人授权时才 downgrade；默认不删表。
5. 若凭据疑似泄露，先在飞书侧吊销/轮换，再排查日志；不把 secret 写入 handoff。

---

## 10. M7 退出标准

### 10.1 必须全部满足

- [ ] PR 1–8 的离线入口门证据齐全，且各 PR 从当时最新主线按顺序合规启动。
- [ ] 真实渠道激活、部署与 canary 门证据齐全；离线阶段没有注册应用、读取真实 secret 或调用
  真实服务。安装锁定的飞书 SDK 和运行 fake contract 不构成真实激活。
- [ ] 飞书长连接入站与 Web 独立 app 均为薄入口，没有业务路由和安全链副本。
- [ ] Web ingress 与内部 `/v1/tasks/*` 进程/端口隔离，并有负向测试和现场证据。
- [ ] internal-api、feishu-listener、channel-worker、web-app 只装配四依赖 TaskViewRuntime；只有
  task-worker 装配完整 XiaoweiRuntime、Runner、ToolGateway 和运维目标 adapter。
- [ ] 完整 Runtime 与窄 Runtime 委托同一投影实现，同一任务的 `TaskView` 完全一致。
- [ ] 三个核心权限、服务端身份映射、session、当前群成员校验均 fail-closed。
- [ ] 普通用户只列自己任务，admin 只列当前 scope 全部安全任务；群详情按单任务校验。
- [ ] 群任务详情首次读取和每次轮询都重新校验当前群成员，成员撤销在下一次读取生效；详情轮询按
  5 秒起、15 秒封顶和页面隐藏至少 30 秒的纪律控频，终态停止。
- [ ] TaskStore 是 owner/status/version/submission/list 的唯一真源。
- [ ] ChannelStore 只保存 ChannelBinding 和 ProjectionSubscription。
- [ ] 投影状态机区分等待任务与 provider failure，并有 CAS/lease/fencing/终态保护。
- [ ] 飞书卡片与 Web 详情复用同一 `TaskView`/`RenderPayload`，无数据库原始结果行。
- [ ] 普通 Web 用户收到终态私聊，admin 不收到额外私聊；群任务更新原卡片。
- [ ] 桌面端工作台和飞书链接单任务详情使用不同 shell 与 JS，并有契约测试保护；详情 shell 不含
  任务提交、聚合列表、CSRF token 或 Admin 配置入口。
- [ ] 每个新增 `interfaces/*.py` 在同一提交进入穷尽白名单；`web_static` 无 Python 包文件。
- [ ] 每个新增配置在 `config.py`、`_FIELD_TO_ENV`、`.env.example` 三处同步并通过集合相等安全门。
- [ ] 所有新 Protocol 在 `_conformance.py` 有生产实现静态锚点；`get_submission` 调用点只有两个。
- [ ] 新进程通过 `python -m ...` 启动，没有把进程入口塞进面向人的 `[project.scripts]`。
- [ ] strict mypy 未全局放宽，`lark_oapi` 只存在于单一 typed seam。
- [ ] SDK 连接 URL、凭据、响应正文和用户原文不进入日志。
- [ ] 四条项目标准门、Compose smoke、安全 eval 和 TDD 反证全部通过。
- [ ] 真实飞书 canary 有精确部署 SHA、trace、脱敏证据、回滚记录和用户验收。

### 10.2 收口格式

完成时必须按四段报告：

1. **已验证**：真实执行的命令、尾部输出、精确 commit/deploy SHA、Compose/真实 canary 证据。
2. **只读推理**：根据代码与契约能判断、但没有现场运行证据的结论。
3. **未覆盖**：未测试环境、未注册 scope、未验证浏览器/飞书客户端等。
4. **残余风险**：即使测试通过仍存在的 at-least-once、静态权限更新、供应商行为漂移等。

不得把计划审核、单元测试、Compose、飞书 fake 或卡片截图写成真实部署/真实用户验收。完成后等待
项目负责人验收；只有明确说“验收通过，归档”才归档任务，不自行合并。

---

## 11. 审核清单

请 Claude 重点逐项回答“通过/不通过 + file:line/理由”，不要只给总体评价：

1. 进程隔离是否真正保证 `web-app` 不注册/暴露内部 `/v1/tasks/*`？
2. TaskStore/ChannelStore 是否仍有重复 owner/status/version/submission/list 的字段或查询真源？
3. 三权限模型是否足够表达既定产品规则，而未把未来审批/artifact 能力偷渡进 M7？
4. 普通用户、admin、群成员的详情/列表授权是否闭集、fail-closed 且不泄露任务存在性？
5. ProjectionSubscription 是否能抵抗并发 claim、陈旧 worker、终态后到事件和供应商重试？
6. 飞书长连接进程、异步出站端口和 SDK 日志处理是否符合官方 SDK 的真实行为？
7. `lark_oapi` 类型隔离是否在 strict mypy 下可维护，是否仍有旁路 import？
8. Web OAuth/session/CSRF/CSP/route 闭集是否完整，权限撤销需重启是否写清楚？
9. “完整结果”是否始终仅指完整安全 RenderPayload，没有数据库行或 Evidence facts 旁路？
10. 八个 PR 是否各自可独立 TDD、审查和回滚，文件归属是否与模块分层一致？
11. 入口门是否允许 PR 1–8 离线实现，同时仍完整阻塞真实应用注册、凭据、网络连接、部署、
    canary 与用户验收？是否明确 M7 证据不能提升 M6b 状态？
12. 退出标准是否区分源码、测试、Compose、部署、canary 与用户验收？
13. 所有配置项是否同步进入 `_FIELD_TO_ENV` 与 `.env.example`，且示例没有 secret 形状？
14. `local_stack.py` 的窄 builder、TaskViewRuntime 依赖闭集和进程调用闭集是否从结构上排除了
    Runner、ToolGateway 与运维目标 adapter？
15. 五个新 Protocol 是否都有 `_conformance.py` 锚点，`get_submission` 是否只有两个允许调用点？
16. 投影状态/目的地/错误码是否是闭集，等待任务是否保持 `WAITING_TERMINAL`，并且只承诺同一
    subscription 串行？

## 12. 已知残余风险

- 飞书/浏览器无法提供真正 exactly-once 的外部可见投递；Web 私聊在“发送成功、提交完成前”崩溃
  时可能重复。M7 通过稳定目的地、串行发送、payload digest 和 at-least-once 记录降低风险。
- 供应商调用在网络超时后可能已成功，应用无法仅凭响应确定结果；渠道订阅可重试但不能伪造成功。
- 静态身份映射不支持热更新，权限撤销依赖受控重启；若产品要求实时撤权，必须另开配置治理里程碑。
- 每次打开群详情都查当前成员会受飞书可用性和限流影响；M7 选择 fail-closed，不以缓存换权限陈旧。
- 官方 SDK 和飞书卡片协议可能漂移；锁版本、单一 seam、contract fake 不能替代真实 canary。
- 原生前端降低构建复杂度，但浏览器兼容性与视觉一致性仍需在目标企业浏览器人工检查。
- 本计划没有解决数据库真实结果 artifact 的专门授权、预览和导出；这是刻意非目标，不是通用
  RenderPayload 的缺陷。
- 未来的 Admin 配置治理、真实模型 API、审批/重跑和后续运维能力尚无独立里程碑、ADR、源码或
  运行证据；这些未来目标不能被解释为 M7 交付物。
- M6b 真实测试环境验证仍延期，但它不阻塞 M7 PR 1–8 的离线实现。若真实渠道 canary 只使用
  fake/recording，它只能证明渠道链路；若要展示真实运维结果，对应 capability 仍必须先取得自身
  要求的真实运行证据，二者不能互相冒充。
