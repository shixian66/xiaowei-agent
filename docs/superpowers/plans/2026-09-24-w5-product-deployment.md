# W5 产品发布与分级验收实施计划

> 状态：Approved V0.2。规划基线：
> `origin/main@88a63a054062db2041e51f4105846920040f3037`（PR #82 已 squash 合入 W4b）。
> 计划经两轮 exact-SHA 独立复审后由 PR #83 合入；负责人随后在会话中下达“按照计划开始开发”，
> 授权 W5-A（Task 0–5）离线实现（无公开 GitHub 审批 permalink，合入事实本身不是批准来源）。
>
> 规格真源：[Web 运维工作台总体设计](../specs/2026-09-19-web-operations-console-identity-activation-design.md)，
> 真实调用与环境授权真源：[ADR-007](../../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)。
>
> **计划送审不授权 W5 源码、部署、真实调用、canary 或 UAT。** 计划获批后仍先做两个离线实现切片；
> 正式主机、窗口、边缘、Provider、目标、canary 与验收人分别取得明确 GO 后，才可执行对应运行步骤。
>
> 历史计划 [2026-09-10-compose-deployment-canary-uat.md](2026-09-10-compose-deployment-canary-uat.md)
> 仅作历史输入，不再作为可执行计划；其中单 Secret 文件、旧身份运行时挂载和旧 Compose 组合已被 W1–W4
> 改变。本计划获批后是 W5 唯一实施指南。
>
> V0.2 按首轮独立复审收紧三条共同根因：准入快照与历史渲染 binding 分离；首次 release 不继承
> recording 时代的计划/证据；W5 V1 固定为 provider-off 产品壳，不冒充 RI6 正式发布。
>
> **实施者必须使用 `superpowers:executing-plans` 按任务顺序执行；行为变更先走
> `superpowers:test-driven-development`，完成前使用 `superpowers:verification-before-completion`。**

**目标：** 让 provider-off 产品壳从不可变镜像安全启动，并把 W5 范围内的 `deployed SHA`、`canary`、
`user-accepted` 三种证据分开取得；未获真实调用授权的功能必须显示不可用，不能回落到 fake/recording，
也不能把这三层证据冒充 RI6 或只读 V1 的正式发布证据。

**架构：** W5 先补发布安全前置，再增加 release Compose 和证据手册，最后才在获准环境执行。离线/CI
继续使用现有合成 recording；W5 V1 release 对所有真实 Provider/目标固定关闭，使用空准入快照与空执行
binding，同时保留只按持久化计划精确版本查找的代码内 rendering binding。配置迁移、旧身份迁移、终态激活
清理都由显式一次性命令执行，长期进程不双读旧真源。

**技术栈：** Python 3.11、Pydantic、SQLAlchemy async、PostgreSQL 16、Alembic、Docker Compose
2.24.4+、Starlette、pytest/pytest-socket、Ruff、mypy、外部 HTTPS SSO edge/WAF。

---

## 1. 当前事实与进入条件

### 1.1 已满足

- W2、W3 V1 已离线收口；W4a/W4b 已分别由 PR #81/#82 合入。
- AI、飞书、resources 三个配置域、generation、加载回执、Admin 审计和显式旧配置迁移器已经存在。
- Web 登录、强制改密、OAuth/激活、角色路由、配置/资源管理、任务工作台与只读详情已有离线测试。
- 基础 Compose 仍保持开发默认关闭与 loopback；CI 的 socket 禁令、Secret 扫描和八项检查继续生效。

### 1.2 尚未满足，必须由本计划闭合

1. 当前 worker 默认装配 StarRocks、Prometheus、资产的 fake/recording，并用完整 capability snapshot
   对外描述能力；该形态只能用于 offline/CI，不能进入 release。
2. `feishu_identity_file` 仍属于长期进程 Settings，`docker-compose.feishu.yml` 仍把旧静态身份挂给
   listener/Web；W1a/W1b 已把数据库目录设为运行时真源，旧文件只能作一次性迁移输入。
3. `APPROVED / REJECTED / EXPIRED` 激活行含受控 PII，目前没有保留期和清理入口。
4. 尚无不可变镜像 release override、发布环境模板、正式 edge 限流证据、回滚演练或 UAT 记录。
5. ADR-007 的 H 层生产只读、RI2 飞书、RI3 Gemini、RI4 StarRocks 与 E1 均未因 W4 合入而自动获批。
6. W5 之前的计划任务与证据没有记录 runtime profile；同一 `dev-local/dev` 作用域中的历史计划/证据
   可能来自 recording，不能在首次 release 中被当成真实结果继续展示。

### 1.3 不作为进入条件

- **W4c**（资源测试连接）与 R1（数据库结果预览/ACL）是独立阶段，不阻塞 W5-A/W5-B 或
  provider-off 产品壳部署。
- W3 后续职责绑定、可靠私聊通知和敏感查看审计延期但未取消，不阻塞 W5。
- RI2、RI4 现场门与 H 层/RI6 未满足时，仍可完成 release 安全实现并部署 provider-off Web 管理面；
  对应真实功能必须保持关闭/不可用，所得证据只覆盖 W5 产品壳，不构成 RI6 或只读 V1 发布证据。

## 2. 本计划固定的产品与安全决定

计划批准即批准以下 W5 决定；若复审不同意，须先修订本节，不能在实现时自行换口径。

### 2.1 终态激活保留 30 天

- 固定 `ACTIVATION_TERMINAL_RETENTION_DAYS = 30`，不新增可被部署者随意改成 0 或无限大的环境变量。
- `APPROVED / REJECTED` 以 `decided_at` 为终态时间；`EXPIRED` 以 `expires_at` 为终态时间。
- 清理边界为 `terminal_at <= now - 30 天`；边界内保留，边界及更早删除。
- 这是**全库所有作用域**的数据保留任务，不接受 tenant/environment 参数，也不只清理
  `dev-local/dev`。每次在既有的全局 activation advisory lock 下，先把已过期 `PENDING`
  转成 `EXPIRED`，再按固定边界删除。
  仍有效的 `PENDING` 永不删除。
- 只删除 `activation_requests`；append-only `admin_audit_events`、用户目录、Session、任务和证据不在清理面。
- 清理命令只输出 `{pending_expired, approved_deleted, rejected_deleted, expired_deleted}`
  四个计数和闭集错误码，不输出 request id、subject、actor、作用域或任意受控 PII。
- 正式部署前执行一次，部署后由环境 owner 每日调度；没有可验证的调度与失败告警，不得晋升 canary。

### 2.2 release 不允许合成执行，也不虚报当前能力

新增闭集运行形态：

```text
offline_recording  仅开发、测试与 Compose smoke；保持现有三能力 recording
release            产品运行；禁止加载任何 fake/recording 模块或 adapter
```

- `release` 不是“换个默认值”：Settings 必须拒绝 `release + recording`，并新增
  `starrocks_adapter_mode=disabled`。W5 V1 release 固定为 `disabled`；`test_readonly` 仍只是 RI4/test
  形态，不能进入 W5 release。
- W5 V1 release 的作用域固定为 `dev-local/dev`（`tenant_id=dev-local` / `environment_id=dev`）。发布模板不得
  把 tenant/environment 变成可自由填写的开口；当前 Web 与 worker 必须使用同一作用域。
- 未取得 H 层/目标 GO 时，五个应用进程（internal-api、web-app、feishu-listener、
  channel-worker、worker）都必须显式收到 `XIAOWEI_RUNTIME_PROFILE=release`，并共用固定 ID
  的空 `conversation_snapshot`。普通对话只投影这份当前准入快照，因此必须如实回答
  “当前无可执行能力”，不能仍声称三个 recording 能力可用。
- worker 另外使用空执行 binding 和空 ToolGateway adapter 集；运维能力请求在 Resolver 阶段
  确定性返回 capability unavailable，绝不能给合成结果。
- `TaskViewRuntime` 不再用一对 snapshot/bindings 同时承担两种角色：`conversation_snapshot`
  仅服务当前普通对话；`rendering_bindings` 保留代码拥有的完整注册表，仅按已持久化
  plan 的精确 capability/version 查找渲染器。`XiaoweiRuntime` 的内嵌 view 也必须显式传入
  这两份不同权威的数据，不得用完整渲染注册表回答当前能力。
- release builder 构建完后，`tests/security/test_fake_isolation.py` 的精确 `_FAKE_MODULES`
  全部不得出现在 `sys.modules`；包括当前被 `local_stack.py` 顶层导入的 `persistence.fake`。
- 未来任一真实 adapter 进入 release，必须另立 RI2/RI4/H/RI6 方案并重做权限、来源与
  作用域设计；不是往 W5 的空快照中加一条 binding，也不能用环境变量传任意 capability 列表。

### 2.3 旧身份文件只允许一次性挂载

- 从长期进程 Settings 和 Compose 挂载中移除 `feishu_identity_file`；listener/Web 只查 PostgreSQL 目录。
- 旧 `.secrets/feishu-identities.json` 仅在一次性迁移命令中只读挂载到固定路径；迁移使用既有
  `migrate_static_identities()`，不复制映射规则或事务语义。
- 同一文档第一次执行创建缺失事实，第二次执行必须报告 `created=0` 且精确 skipped 数；部分匹配仍整批
  fail-closed。干净安装没有旧文档时明确记录 `not_applicable`，不造一份空文件冒充迁移证据。
- 迁移 CLI 不直接序列化既有 `deferred_labels`；它只允许输出 `created_count`、
  `skipped_count`、`deferred_count` 和闭集错误码。原始 actor、subject、label 与错误正文均不进部署证据。
- 成功后旧文件离线保留一个发布周期供回滚核对，但不再挂给任何长期进程；下一次正式发布前由 owner
  确认销毁或继续隔离保管。

### 2.4 provider-off release、真实调用和证据分级互不代替

- W5 V1 release 不留“默认可改”的开口：Gemini、Feishu listener、channel-worker、StarRocks
  与其他真实 Provider/target 开关在 release 模板中**固定关闭**。
- W5-C 只取得 `provider-off 产品壳` 的 deployed SHA / canary / user-accepted 证据。这些标签不构成 RI6，
  也不构成只读 V1 发布验收。
- `DEVELOPMENT_PLAN.md` 的 RI2 和 RI4 现场门、ADR-007 H 层以及 RI6 仍是后续真实发布的
  独立前置；任一真实调用都需新计划和新 GO，不在 W5 中开启。
- W5 不实现 **E1**，不修改被管目标，不开放 Dinky、Alertmanager 写入或任意工具逃生口。
- `deployed SHA` 只证明镜像与配置在目标主机运行；`canary` 还要求边缘限流、观测窗口与受控用户范围；
  `user-accepted` 还要求指定验收人按场景签字。三者不能合并成一个勾。
- 正式跨主机/公网只允许已有 HTTPS SSO Host/Origin。`lan_http` 只算受信局域网体验，不可晋升 canary。

### 2.5 首次 release 不继承 recording 时代的执行结果

- 切换 runtime profile 前，发布预检必须对固定 `dev-local/dev` 作用域完整分页扫描；
  终态判断直接复用 `TERMINAL_STATUSES`，不手写状态列表。
- 任一任务不在 `TERMINAL_STATUSES` 时返回 `tasks_not_drained`。其中 `PLANNING` 是非终态；
  `CLARIFICATION_REQUIRED` 是终态，不得反向分类。
- 任一历史任务已持久化 plan 或 evidence（包括 `SUCCEEDED` 等终态）时，返回
  `historical_execution_data_present`。既有 schema 无法证明它来自哪个 runtime profile，所以不可靠猜测放行。
- 命中历史执行数据后，owner 只能选择**新数据库**或另起方案做**单独批准的数据处置**。
  W5 不提供通用历史删除器，也不允许 runbook 用临时 SQL 绕过。
- 仅有无 plan/evidence 的对话或预计划终态可被保留；它们按当前空 `conversation_snapshot`
  投影，不冒充历史能力可用。
- 日后 RI6 若要在同一数据库升级真实能力，必须先设计可持久化的 runtime/evidence provenance；
  这不在 W5 V1 范围。

## 3. 交付切片与授权边界

### W5-A：发布安全前置（离线实现 PR）

运行形态闭集、当前准入快照/历史渲染 binding 分离、终态激活清理、旧身份一次性 CLI、
首次 release 历史数据预检。该 PR 不增加 Compose 外网端口，不部署，不访问真实 Provider/目标。

### W5-B：release 资产与证据合同（离线实现 PR）

不可变镜像 provider-off Compose override、发布环境模板、边缘/部署/canary/UAT 手册与契约测试。
依赖 W5-A 合入。
该 PR 只证明制品可渲染和隔离 smoke 可运行，不证明正式主机已部署。

### W5-C：目标环境执行（需新的明确 GO）

备份、迁移、预检、部署、回滚演练、canary、UAT 和证据回填，但范围仅限 provider-off 产品壳。
W5-A/B 合入不授权 W5-C；它也不授权 RI2、RI4、H 层或 RI6。任一真实 Provider/目标的发布是
另一阶段，须新计划与新 GO。

## 4. W5-A 任务：发布安全前置

### Task 0：锁定基线与 RED 证据

**Files:**

- Create: `tests/contract/test_release_runtime_profile.py`
- Create: `tests/contract/test_activation_retention.py`
- Create: `tests/contract/test_release_maintenance_commands.py`
- Modify: `tests/security/test_fake_isolation.py`
- Modify: `tests/security/test_identity_write_path.py`

- [ ] 从最新 `main` 创建 `claude/w5a-release-safety`，核对本计划 Approved 状态和明确开工口令。
- [ ] 写 RED：release 仍加载 recording；五个 task-view 进程的普通对话仍投影完整快照；旧身份仍是长期进程必填项；
  终态 PII 永不清理；有 recording 时代的终态 plan/evidence 却被首次 release 预检放行。
- [ ] 反例只改变一个事实；确认失败落在目标断言，而非 ConfigError、缺 fixture 或 socket 禁令。
- [ ] 保存 RED 命令和末尾输出，不修改断言迁就当前实现。

Run:

```bash
python -m pytest tests/contract/test_release_runtime_profile.py \
  tests/contract/test_activation_retention.py \
  tests/contract/test_release_maintenance_commands.py \
  tests/security/test_fake_isolation.py -q
```

Expected: 新增目标用例因缺少实现而失败；既有安全用例继续通过。

### Task 1：实现 release runtime profile

**Files:**

- Modify: `src/xiaowei_agent/application/task_view_runtime.py`
- Modify: `src/xiaowei_agent/application/runtime.py`
- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `tests/contract/test_task_view_runtime.py`
- Modify: `tests/contract/test_runtime_facade.py`
- Modify: `tests/unit/test_config_happy.py`
- Modify: `tests/unit/test_local_stack.py`
- Modify: `tests/contract/test_release_runtime_profile.py`
- Modify: `tests/security/test_fake_isolation.py`
- Modify: `tests/security/test_task_view_runtime_authority.py`
- Modify: `.env.example`

- [ ] 新增 `RuntimeProfile.OFFLINE_RECORDING / RELEASE` 与 `StarRocksAdapterMode.DISABLED`；base 默认保持
  offline/recording，release 必须固定 `dev-local/dev + disabled`，其他组合 fail-closed。
- [ ] 建立空准入/执行 registry 和另一份完整的代码内 rendering registry；两者不共用一个含混快照的
  `CapabilityBindingRegistry`。
- [ ] 把 `TaskViewRuntime` 参数与内部字段改为 `conversation_snapshot` / `rendering_bindings`；
  `XiaoweiRuntime` 也分开当前准入快照、执行 bindings 和历史 rendering bindings。
- [ ] internal-api、web-app、feishu-listener、channel-worker 与 worker 五个 builder 都根据同一
  `XIAOWEI_RUNTIME_PROFILE` 装配普通对话快照；release 下回答空快照 ID/“当前无可执行能力”。
- [ ] worker 的能力请求在 Resolver 阶段拒绝，ToolGateway 调用计数保持 0；已持久化 plan 仍能按
  capability/version 从 `rendering_bindings` 查到投影器。
- [ ] 把 fake/recording import 放进 offline 分支内部；五个 release builder 各自在干净子进程中装配后，
  `tests/security/test_fake_isolation.py::_FAKE_MODULES` 精确全集与 `sys.modules` 交集必须为空。
- [ ] 做隔离变异：任一 view 进程改用完整快照回答普通对话、把 recording adapter 加回 release，
  或恢复 `persistence.fake` 顶层导入时，相应测试必须因目标不变量转红。

### Task 2：实现固定终态保留与维护 CLI

**Files:**

- Modify: `src/xiaowei_agent/contracts/activation.py`
- Modify: `src/xiaowei_agent/persistence/activation.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Create: `src/xiaowei_agent/interfaces/activation_retention.py`
- Modify: `tests/suites/activation_store.py`
- Modify: `tests/contract/test_activation_store.py`
- Modify: `tests/contract/test_activation_retention.py`
- Modify: `tests/contract/test_release_maintenance_commands.py`
- Modify: `tests/integration/test_activation_store_postgres.py`
- Modify: `tests/security/test_identity_write_path.py`

- [ ] 新增窄 `ActivationRetentionStore`；没有通用 delete、调用方 cutoff、request-id 删除或 Web 路由。
- [ ] 内存/PostgreSQL 共享套件覆盖 30 天两侧、恰好边界、四状态、先过期再删除、重复执行零删除。
- [ ] PostgreSQL 在 activation advisory lock 与一个事务里完成转换/删除；并发创建不能绕过容量/清理语义。
- [ ] CLI 使用全局 activation advisory lock 扫描全库所有作用域，不接受 scope 参数；只输出
  `{pending_expired, approved_deleted, rejected_deleted, expired_deleted}` 计数与闭集错误码。
- [ ] 共享/真库套件必须在至少两个 tenant/environment 中同时造数，证明一次运行处理全部到期行且不误删
  任一作用域内未到期行。
- [ ] 真库测试直接确认被删行、保留行和 Admin 审计行；日志/异常/报告不含受控 PII。
- [ ] 变异分别撤掉 `PENDING` 保护、边界比较与 advisory lock，测试须因对应不变量失败。

### Task 3：旧身份迁移 CLI 与运行时去旧真源

**Files:**

- Modify: `.env.example`
- Modify: `README.md`
- Delete: `docker-compose.feishu.yml`
- Modify: `docker-compose.smoke.yml`
- Modify: `scripts/compose_smoke.py`
- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/legacy_identity_migration.py`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `tests/contract/test_legacy_identity_migration.py`
- Modify: `tests/contract/test_release_maintenance_commands.py`
- Modify: `tests/contract/test_ri5_config_api.py`
- Modify: `tests/contract/test_ri5_load_receipts.py`
- Modify: `tests/contract/test_ri5_probe_routes.py`
- Modify: `tests/contract/test_web_admin_identity_api.py`
- Modify: `tests/contract/test_web_app_routes.py`
- Modify: `tests/contract/test_web_task_api.py`
- Modify: `tests/integration/test_m7_channel_flow.py`
- Modify: `tests/integration/test_w3_admin_identity_flow.py`
- Modify: `tests/security/test_feishu_ingress.py`
- Modify: `tests/security/test_feishu_log_redaction.py`
- Modify: `tests/security/test_ri5_compose_boundary.py`
- Modify: `tests/unit/test_feishu_config.py`
- Modify: `tests/unit/test_local_stack.py`
- Modify: `tests/unit/test_provider_consumption.py`
- Modify: `tests/unit/test_web_config.py`
- Modify: `tests/security/test_ri5_web_assembly.py`
- Modify: `tests/security/test_w4_config_domain_boundary.py`
- Modify: `tests/security/test_web_auth_boundary.py`

- [ ] 给既有迁移模块增加固定路径的一次性 `main()`；路径不进入长期 Settings，不接受任意映射策略。
- [ ] CLI 构造 PostgreSQL `UserDirectoryStore`，使用固定 local admin actor/scope，确保 engine 所有退出路径释放。
- [ ] CLI 将既有迁移结果投影为 `created_count / skipped_count / deferred_count` 和闭集错误码；
  不把 raw `deferred_labels`、actor、subject 或 `str(exc)` 序列化到 stdout/stderr。
- [ ] 在同一 W5-A PR 中从 Settings、`.env.example`、README、listener/Web 装配、base/smoke Compose
  与 smoke 脚本中移除长期 `feishu_identity_file`；删除 `docker-compose.feishu.yml`，不把失效 override
  留到 W5-B。全仓生产代码仅一次性 CLI 可读旧文档。
- [ ] 覆盖首次迁移、完整重跑、部分冲突、损坏/符号链接文件、超长旧值和审计失败；失败整批零写入。
- [ ] Compose/smoke 契约证明任一长期服务都没有旧文件变量或 mount，且移除该 Settings 字段后
  compose-smoke 仍能启动。
- [ ] AST 守卫证明长期入口不 import/读取旧文档；变异把文件加回 Web 装配时安全测试转红。

### Task 4：增加发布预检，不把运行中任务跨 profile 接管

**Files:**

- Create: `src/xiaowei_agent/interfaces/release_preflight.py`
- Create: `tests/contract/test_release_preflight.py`
- Create: `tests/integration/test_release_preflight_postgres.py`
- Modify: `src/xiaowei_agent/interfaces/config_preflight.py`
- Modify: `tests/security/test_ri5_compose_boundary.py`
- Modify: `tests/security/test_w4_config_domain_boundary.py`

- [ ] 复用 `TaskStore.list_tasks_for_scope()` 分页读取固定 `dev-local/dev`；终态判断直接导入
  `TERMINAL_STATUSES`。任何非终态任务返回 `tasks_not_drained` 与总数，不打印 task id、正文或 actor。
- [ ] 同一分页扫描还要查 plan/evidence 是否存在；任一历史任务（含终态）已有 plan 或 evidence
  就返回 `historical_execution_data_present`，不接受“已成功所以可继承”的特例。
- [ ] 组合既有三域目录预检；旧 `integrations.json` 存在、目录不可写、schema 未到 head、数据库不可达均
  fail-closed。
- [ ] 身份迁移与终态清理由 runbook 独立执行并保存各自报告；预检不暗中执行写操作。
- [ ] 分页超过 100、空库、无 plan/evidence 的终态对话、`PLANNING`、`CLARIFICATION_REQUIRED`
  的边界全部覆盖；真 PostgreSQL 反例必须包含一条 recording 时代 `SUCCEEDED` + plan/evidence。
- [ ] runbook 契约明确要求新数据库或单独批准的数据处置；本预检不删历史数据，不接受
  `--force`、忽略标志或临时 SQL。
- [ ] 预检重复运行不改数据库/配置文件；网络只允许目标 PostgreSQL，不解析或连接任何已登记资源。

### Task 5：W5-A 集成、自审与合并门

**Files:**

- Modify: `ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`
- Modify: `docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md`
- Modify: `tests/contract/test_doc_fact_binding.py`

- [ ] 更新稳定架构与已批准规格：`conversation_snapshot` 与 `rendering_bindings` 分离、provider-off release、
  首次发布历史数据门、全库 30 天激活保留、旧身份仅一次性输入；只把本计划获批的确定值写回规格，
  不改真实调用门。
- [ ] 运行受影响聚焦测试、四门、安全反证、一次性 PostgreSQL 全量和 wheel 内容检查。
- [ ] 自审入口→Settings→装配→Resolver/Gateway、维护 CLI→store→事务、错误/并发/重跑/退出路径。
- [ ] 提交、推送、开 PR；按 exact SHA 独立审查和 CI。未合入前不得开始 W5-B。

Run:

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

Expected: 四门通过；有 DSN 的 PostgreSQL 全量 0 skip；release 反例证明 fake/recording 调用为 0。

## 5. W5-B 任务：release 资产与证据合同

### Task 6：先写 release Compose RED 契约

**Files:**

- Create: `tests/contract/test_release_compose.py`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `tests/security/test_ri5_compose_boundary.py`
- Create: `tests/security/test_release_boundary.py`

- [ ] 从 W5-A 合入后的最新 main 创建 `claude/w5b-release-assets`。
- [ ] RED 必须覆盖：镜像 digest 必填、所有 app service 无 build、只发布 Web、API/DB/worker 无宿主端口、
  五个 app service 的 runtime profile=release、固定 `dev-local/dev`、长期服务无旧身份文件、全部真实调用
  固定关闭、配置域挂载矩阵不变。
- [ ] 合成 base+release 的最终 Compose 再断言；不能只读单个 YAML。任何额外 override 试图打开 Gemini、OAuth、
  listener、channel-worker、StarRocks 或真实测试开关时，契约必须拒绝，而不是把它当作本阶段合法组合。
- [ ] 证明 release 缺边缘证据/必要变量时预检失败；不把 OAuth/Activation 容量冒充 rate limit。

### Task 7：实现不可变镜像 release override

**Files:**

- Create: `docker-compose.release.yml`
- Create: `docs/examples/w5-release.env.example`
- Modify: `scripts/compose_smoke.py`
- Modify: `README.md`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `tests/contract/test_release_compose.py`
- Modify: `tests/security/test_ri5_compose_boundary.py`
- Modify: `tests/security/test_release_boundary.py`

- [ ] `XIAOWEI_RELEASE_IMAGE` 必须是 `name@sha256:<64 hex>`；release 对 migrate/api/worker/web/channel
  services 全部清除 `build`，禁止现场构建与 mutable tag。
- [ ] release 对 migrate/api/worker/web-app 以及未启动的 listener/channel-worker 定义统一
  `XIAOWEI_RUNTIME_PROFILE=release`，并固定 `tenant_id=dev-local`、`XIAOWEI_ENVIRONMENT_ID=dev`、
  `XIAOWEI_STARROCKS_ADAPTER_MODE=disabled`；Web enabled、HTTPS mode，部署 actor、public origin 与 bind IP
  由脱敏模板显式提供。
- [ ] release 将 `XIAOWEI_GEMINI_ENABLED`、`XIAOWEI_FEISHU_OAUTH_ENABLED`、
  `XIAOWEI_FEISHU_LISTENER_ENABLED`、`XIAOWEI_CHANNEL_WORKER_ENABLED` 和两个 real-test flag 写成不可由
  `.env` 覆盖的字面 `false`；本 Compose 集合不启动渠道 profile，也没有“获准 Provider”组合。
- [ ] release 只发布 Web；API 的 base loopback 端口在 release 最终模型中也被清除。PostgreSQL、worker、
  listener、channel-worker 不发布端口。
- [ ] 删除长期旧身份 override。一次性身份迁移在 runbook 用只读临时 mount 调 CLI，命令退出后没有容器
  持有该 mount。
- [ ] Provider 配置为空或已登记但未接入时 Web 仍 ready；管理面只展示“已保存，尚未接入/未授权”。
  W5 不新增 `waiting_for_config` 状态机，也不启动 listener/channel-worker 来等待未来配置。
- [ ] Compose smoke 使用 `.invalid` 与假配置，只证明装配/回执；任何真实 DNS/socket/Provider 调用仍为 0。

### Task 8：固化 edge、部署、回滚、canary 与 UAT 合同

**Files:**

- Create: `docs/runbooks/w5-product-deployment.md`
- Create: `docs/checklists/w5-edge-rate-limit-evidence.md`
- Create: `docs/checklists/w5-deployment-evidence.md`
- Create: `docs/checklists/w5-canary-evidence.md`
- Create: `docs/checklists/w5-user-acceptance.md`
- Create: `tests/contract/test_w5_release_runbooks.py`

- [ ] edge 清单按实际路由分组，至少覆盖 `/login/api/login`、`/oauth/feishu/start`、
  `/oauth/feishu/callback`、`/admin/api/activations/approve`、`/admin/api/activations/reject`。
- [ ] 每组记录边缘配置版本、可信 client-IP 链、正常请求、429 反例、窗口恢复、告警触发与 owner；日志禁止
  query/code/state、Cookie、密码、Secret、subject 或请求正文。
- [ ] 部署清单分开记录 source SHA、image digest、Compose 文件集合、配置 generation/readback、DB 备份、
  migration revision、身份迁移闭集计数、全库保留清理闭集计数、初始改密、健康检查和回滚 owner；
  身份报告不得含 raw actor/deferred label。
- [ ] 首次 release 清单要求 owner 在“新数据库”与“单独批准的数据处置”之间书面选择并保存预检结果；
  runbook 不提供删除历史 plan/evidence 的便捷命令。
- [ ] 回滚演练只回滚镜像/非秘密配置；数据库 schema 默认 forward-only。回滚目标必须是**更早的、已经支持
  W5 release 契约的不可变 digest**，且反向切换前同样运行 drain/history 预检；不得回到 recording 镜像。
- [ ] 首次部署不存在安全的旧 release digest，回滚定义为停止全部应用服务、保留数据库与证据等待修复；
  不允许为了“恢复服务”启动 W5 前的 recording 栈。
- [ ] canary 固定 Local Admin 验收人、时间窗、观测项和停止条件；UAT 只覆盖 provider-off 产品壳可达路径。
  Operator、普通用户、群与真实 OAuth/Provider 路径明确记为本轮不适用，不能靠假 Session 或合成结果补齐角色矩阵。
- [ ] 文档显式说明：没有 edge 运行证据的 LAN 只能算本地体验，不是 canary。

### Task 9：W5-B 验证与合并门

**Files:**

- Modify: `ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`
- Modify: `tests/contract/test_doc_fact_binding.py`

- [ ] `docker compose ... config --quiet` 覆盖唯一合法的 provider-off release；另用负例逐项证明任何真实
  Provider/target 开关被打开都会失败。禁止 `config --environment` 或输出完整解析配置。
- [ ] 构建一次不可变候选镜像并记录 digest；本地/CI smoke 不接真实 Provider/目标。
- [ ] 重跑四门、Compose smoke、wheel 检查、1280/1440 桌面与窄屏只读详情视觉检查。
- [ ] exact-SHA 独立复审与 CI 通过后由负责人合入。此时最多写“W5 provider-off 离线发布资产完成”，
  不得写 deployed、RI6 或只读 V1 已发布。

## 6. W5-C 任务：获准环境执行

### Task 10：取得部署 GO 并冻结执行输入

- [ ] 负责人书面指定：目标主机、维护窗口、release SHA/image digest、HTTPS edge、provider-off canary 范围、
  验收人、DB/配置备份位置和回滚 owner；作用域固定为 `dev-local/dev`。
- [ ] RI2、RI3、RI4、H 层与 RI6 在本次执行中全部保持独立门/NO-GO，最终 Compose 必须仍为
  false/disabled。即使某项另行取得 GO，也必须退出本计划、先形成对应的真实 release 设计与复审；
  **W5 GO 不代替任何真实调用 GO。**
- [ ] 明确回滚目标：已有 W5-compatible release 时写下其不可变 digest；首次部署则写明“停止全部应用服务”，
  不得把 W5 前 recording 镜像列作恢复目标。
- [ ] 核对目标主机 Docker/Compose 版本、磁盘、端口、DNS/NTP、证书链和 Secret/config 文件权限；输出脱敏。
- [ ] 证据文件在执行前固定模板与存放位置；不把 Secret 或生产数据复制进 Git。

### Task 11：备份、迁移、预检和首次启动

执行顺序固定，任一步失败即停止并按 runbook 回滚，禁止在线改源码：

1. 记录旧服务/进程并取得数据库与三域配置恢复点；证明旧 listener 已停，避免双消费。
2. 拉取并核对不可变 image digest；用 release 文件集合运行 `config --quiet`。
3. 启动 PostgreSQL（若由本 Compose 管理）并运行 schema migration；不做普通 downgrade。
4. 停止长期进程后显式迁移旧 `integrations.json`；重读三域并执行 `config_preflight`。
5. 有旧身份文件时只读临时挂载并执行迁移两次，只保存 count-only 的“首次结果 + 第二次零创建”；无文件记录
   `not_applicable`，不保存 raw actor/deferred labels。
6. 执行全库终态激活 30 天清理并确认每日调度/失败告警；对固定 `dev-local/dev` 执行 release drain/history
   preflight。命中 `historical_execution_data_present` 时停止，按已批准的新数据库/数据处置决定执行。
7. 只启动 Web/worker/API，不启动 listener/channel-worker；核对 health/readiness、容器 image digest、端口面和
   generation/load receipt，并证明所有 Provider/target 开关仍为 false/disabled。
8. 使用 `admin/admin` 首登并立即改密；旧 Session 必须失效。受信 LAN 改密前可达是已接受风险，不表示
   可以跳过改密。

完成本 Task 只能标记 `deployed SHA`。

### Task 12：回滚演练、canary 与 user-accepted

- [ ] 在真实用户进入前执行受控回滚演练：先对反向目标运行 drain/history preflight；有 W5-compatible 旧 digest
  才回退镜像/非秘密配置并核对 readback。首次部署只演练停止应用服务，再恢复候选；绝不启动 recording 栈。
- [ ] edge 对五组路由完成正常、429、恢复和告警证据；缺一项不得开始 canary。
- [ ] canary 只开放给指定的 Local Admin 验收人，观察本地登录/强制改密、Admin 页面、空能力工作台、进程重启、
  DB、edge 拒绝和配置 readback。OAuth、群消息、模型与资源目标不得进入观测流量；命中停止条件立即回滚。
- [ ] UAT 只验 provider-off 功能矩阵：Local Admin 登录/强制改密、Admin 用户/审计/配置/资源登记、工作台诚实
  显示当前无可执行能力、真实功能的未授权/待接入提示与统一失败分类。Operator/普通用户的真实 OAuth、飞书、
  真实结果深链不在本轮验收，不能用假 Session 或合成结果代替。
- [ ] 分别签署 `W5 provider-off product-shell deployed SHA`、`W5 provider-off canary`、
  `W5 provider-off user-accepted`；更新 handoff。三层齐备只允许归档 W5 产品壳，不得写 RI6 或只读 V1 完成。

## 7. 独立门与明确非目标

- **W4c**：StarRocks/Prometheus 测试连接、DNS、登录、SQL/HTTP 探针；不在 W5 偷做。
- **R1**：数据库结果 artifact、ACL、预览、导出与“我的结果”；不在 W5。
- **H 层**：生产只读授权尚未签认；W5 只提供关闭态与证据槽位。
- **RI2 / RI3 / RI4 / RI6**：真实飞书、Gemini、StarRocks 与正式只读部署验收各有独立门；一个 GO 不外推给
  另一个。W5 provider-off 证据不得登记为 RI6 或只读 V1 证据。
- **E1**：任何被管目标写操作持续禁止；W5 不更改 ADR-007 的写边界。
- 不在应用里临时造 HTTP rate limiter 来代替正式 edge；也不因 edge 存在而删除应用的 Origin/CSRF/
  Session/CSP/权限检查。
- 不引入 Kubernetes、服务网格、动态配置中心、热加载、任意 Provider 插件、消息总线或自动全量发布。
- 不把本地 Docker、CI Compose、一次性 PostgreSQL、浏览器截图或 `readyz` 冒充正式部署/canary/UAT。

## 8. 验收矩阵

| 结论 | 最低证据 | 不能声称 |
| --- | --- | --- |
| W5-A 离线完成 | 四门、真库 0 skip、runtime/retention/迁移变异、exact SHA CI | release 可部署 |
| W5-B 离线完成 | Compose 合成契约、原始 Dockerfile smoke、runbook 契约、视觉检查 | 已部署 |
| W5 provider-off product-shell deployed SHA | 主机、SHA/digest、备份、迁移、历史门、health/readback、端口面 | canary、RI6 |
| W5 provider-off canary | edge 429/恢复/告警、受控 Local Admin、观察窗、停止条件、回滚演练 | user-accepted、只读 V1 |
| W5 provider-off user-accepted | 指定验收人按 provider-off 功能矩阵签字，未授权功能如实标注 | 全量上线、RI6 或未获批 Provider 可用 |

计划、源码、离线测试、真实环境、部署、canary 与用户验收必须在交付报告中分栏表述。
