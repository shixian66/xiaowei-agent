# W5 产品发布与分级验收实施计划

> 状态：Review Draft V0.1。规划基线：
> `origin/main@88a63a054062db2041e51f4105846920040f3037`（PR #82 已 squash 合入 W4b）。
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
> **实施者必须使用 `superpowers:executing-plans` 按任务顺序执行；行为变更先走
> `superpowers:test-driven-development`，完成前使用 `superpowers:verification-before-completion`。**

**目标：** 让已获授权的产品功能从不可变镜像安全启动，并把 `deployed SHA`、`canary`、
`user-accepted` 三种证据分开取得；未获真实调用授权的功能必须显示不可用，不能回落到 fake/recording。

**架构：** W5 先补发布安全前置，再增加 release Compose 和证据手册，最后才在获准环境执行。离线/CI
继续使用现有合成 recording；release 运行时只装配已获授权的真实 adapter，否则使用空 capability 快照。
配置迁移、旧身份迁移、终态激活清理都由显式一次性命令执行，长期进程不双读旧真源。

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

### 1.3 不作为进入条件

- **W4c**（资源测试连接）与 R1（数据库结果预览/ACL）是独立阶段，不阻塞 W5。
- W3 后续职责绑定、可靠私聊通知和敏感查看审计延期但未取消，不阻塞 W5。
- 没有真实 Provider GO 时仍可完成 release 安全实现和部署 Web 管理面；对应功能必须保持关闭/不可用。

## 2. 本计划固定的产品与安全决定

计划批准即批准以下 W5 决定；若复审不同意，须先修订本节，不能在实现时自行换口径。

### 2.1 终态激活保留 30 天

- 固定 `ACTIVATION_TERMINAL_RETENTION_DAYS = 30`，不新增可被部署者随意改成 0 或无限大的环境变量。
- `APPROVED / REJECTED` 以 `decided_at` 为终态时间；`EXPIRED` 以 `expires_at` 为终态时间。
- 清理边界为 `terminal_at <= now - 30 天`；边界内保留，边界及更早删除。
- 每次清理在既有 activation advisory lock 下先把已过期 `PENDING` 转成 `EXPIRED`，再按固定边界删除。
  仍有效的 `PENDING` 永不删除。
- 只删除 `activation_requests`；append-only `admin_audit_events`、用户目录、Session、任务和证据不在清理面。
- 清理命令只输出按状态计数的闭集报告，不输出 request id、subject、actor 或任意受控 PII。
- 正式部署前执行一次，部署后由环境 owner 每日调度；没有可验证的调度与失败告警，不得晋升 canary。

### 2.2 release 不允许合成执行

新增闭集运行形态：

```text
offline_recording  仅开发、测试与 Compose smoke；保持现有三能力 recording
release            产品运行；禁止加载任何 fake/recording 模块或 adapter
```

- `release` 不是“换个默认值”：Settings 必须拒绝 `release + recording` 的组合。
- 未取得 H 层/目标 GO 时，release worker 使用固定 ID 的**空 capability snapshot**、空执行 binding 和空
  ToolGateway adapter 集；运维问题确定性返回 capability unavailable，绝不能给合成结果。
- 后续某个只读真实 adapter 获独立 GO 时，只把该 capability 的 spec、binding 和 adapter 加入同一
  release 快照；不能用环境变量传任意 capability 列表。
- 现阶段 `test_readonly` StarRocks 仍只允许 `environment_id=test`，不得冒充生产 H 层授权。
- 终态历史任务的只读投影继续使用代码拥有的完整 rendering binding；**当前准入快照**与**历史渲染
  binding**分开，不能为了隐藏未授权能力导致历史终态打不开。
- 切换 runtime profile 前，发布预检必须证明目标作用域没有非终态任务；不能让 recording 下创建的任务
  由 release worker 接手。

### 2.3 旧身份文件只允许一次性挂载

- 从长期进程 Settings 和 Compose 挂载中移除 `feishu_identity_file`；listener/Web 只查 PostgreSQL 目录。
- 旧 `.secrets/feishu-identities.json` 仅在一次性迁移命令中只读挂载到固定路径；迁移使用既有
  `migrate_static_identities()`，不复制映射规则或事务语义。
- 同一文档第一次执行创建缺失事实，第二次执行必须报告 `created=0` 且精确 skipped 数；部分匹配仍整批
  fail-closed。干净安装没有旧文档时明确记录 `not_applicable`，不造一份空文件冒充迁移证据。
- 成功后旧文件离线保留一个发布周期供回滚核对，但不再挂给任何长期进程；下一次正式发布前由 owner
  确认销毁或继续隔离保管。

### 2.4 release、真实调用和证据分级互不代替

- release override 默认所有真实调用关闭。RI2、RI3、RI4、H 层分别取得 GO 才能打开对应开关。
- W5 不实现 **E1**，不修改被管目标，不开放 Dinky、Alertmanager 写入或任意工具逃生口。
- `deployed SHA` 只证明镜像与配置在目标主机运行；`canary` 还要求边缘限流、观测窗口与受控用户范围；
  `user-accepted` 还要求指定验收人按场景签字。三者不能合并成一个勾。
- 正式跨主机/公网只允许已有 HTTPS SSO Host/Origin。`lan_http` 只算受信局域网体验，不可晋升 canary。

## 3. 交付切片与授权边界

### W5-A：发布安全前置（离线实现 PR）

运行形态闭集、终态激活清理、旧身份一次性 CLI、发布预检。该 PR 不增加 Compose 外网端口，不部署，
不访问真实 Provider/目标。

### W5-B：release 资产与证据合同（离线实现 PR）

不可变镜像 Compose override、发布环境模板、边缘/部署/canary/UAT 手册与契约测试。依赖 W5-A 合入。
该 PR 只证明制品可渲染和隔离 smoke 可运行，不证明正式主机已部署。

### W5-C：目标环境执行（需新的明确 GO）

备份、迁移、预检、部署、回滚演练、canary、UAT 和证据回填。W5-A/B 合入不授权 W5-C；每个真实
Provider/目标仍使用自己的 GO，未获批项保持关闭。

## 4. W5-A 任务：发布安全前置

### Task 0：锁定基线与 RED 证据

**Files:**

- Create: `tests/contract/test_release_runtime_profile.py`
- Create: `tests/contract/test_activation_retention.py`
- Create: `tests/contract/test_release_maintenance_commands.py`
- Modify: `tests/security/test_fake_isolation.py`
- Modify: `tests/security/test_identity_write_path.py`

- [ ] 从最新 `main` 创建 `claude/w5a-release-safety`，核对本计划 Approved 状态和明确开工口令。
- [ ] 写 RED：release 仍加载 recording、旧身份仍是长期进程必填项、终态 PII 永不清理、维护命令不存在。
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

- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `src/xiaowei_agent/application/default_capabilities.py`
- Modify: `src/xiaowei_agent/capabilities/registry.py`
- Modify: `tests/unit/test_config_happy.py`
- Modify: `tests/unit/test_local_stack.py`
- Modify: `tests/unit/test_capability_runtime_registry.py`
- Modify: `tests/contract/test_release_runtime_profile.py`
- Modify: `tests/security/test_fake_isolation.py`
- Modify: `.env.example`

- [ ] 新增 `RuntimeProfile.OFFLINE_RECORDING / RELEASE`；base 默认保持 offline，release 组合禁止 recording。
- [ ] 为 worker 建立独立 admission snapshot/bindings builder；空快照有固定 ID，binding 集必须精确等于快照。
- [ ] 把 fake/recording import 放进 offline 分支内部；仅 import/build release worker 时相关模块不得进入
  `sys.modules`。
- [ ] release 空快照下，普通对话只诚实投影“当前无可执行能力”，能力请求在 Resolver 阶段拒绝，
  ToolGateway 调用计数保持 0。
- [ ] 保留 TaskViewRuntime 的历史 rendering binding；补终态历史任务可读的正常对照。
- [ ] 做隔离变异：撤掉 release/profile 校验或把 recording adapter 加回 release，目标测试必须转红。

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
- [ ] CLI 固定使用部署作用域和固定保留期，只输出 `{pending_expired, approved_deleted, rejected_deleted,
  expired_deleted}` 计数与闭集错误码。
- [ ] 真库测试直接确认被删行、保留行和 Admin 审计行；日志/异常/报告不含受控 PII。
- [ ] 变异分别撤掉 `PENDING` 保护、边界比较与 advisory lock，测试须因对应不变量失败。

### Task 3：旧身份迁移 CLI 与运行时去旧真源

**Files:**

- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/legacy_identity_migration.py`
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
- Modify: `tests/unit/test_feishu_config.py`
- Modify: `tests/unit/test_local_stack.py`
- Modify: `tests/unit/test_provider_consumption.py`
- Modify: `tests/unit/test_web_config.py`
- Modify: `tests/security/test_ri5_web_assembly.py`
- Modify: `tests/security/test_w4_config_domain_boundary.py`
- Modify: `tests/security/test_web_auth_boundary.py`

- [ ] 给既有迁移模块增加固定路径的一次性 `main()`；路径不进入长期 Settings，不接受任意映射策略。
- [ ] CLI 构造 PostgreSQL `UserDirectoryStore`，使用固定 local admin actor/scope，确保 engine 所有退出路径释放。
- [ ] 从 listener/Web 装配和配置闭集移除 `feishu_identity_file`；全仓生产代码仅维护 CLI 可读旧文档。
- [ ] 覆盖首次迁移、完整重跑、部分冲突、损坏/符号链接文件、超长旧值和审计失败；失败整批零写入。
- [ ] AST 守卫证明长期入口不 import/读取旧文档；变异把文件加回 Web 装配时安全测试转红。

### Task 4：增加发布预检，不把运行中任务跨 profile 接管

**Files:**

- Create: `src/xiaowei_agent/interfaces/release_preflight.py`
- Create: `tests/contract/test_release_preflight.py`
- Create: `tests/integration/test_release_preflight_postgres.py`
- Modify: `src/xiaowei_agent/interfaces/config_preflight.py`
- Modify: `tests/security/test_ri5_compose_boundary.py`
- Modify: `tests/security/test_w4_config_domain_boundary.py`

- [ ] 复用 `TaskStore.list_tasks_for_scope()` 分页读取目标作用域；任何非终态任务都只返回闭集
  `tasks_not_drained` 与总数，不打印 task id、正文或 actor。
- [ ] 组合既有三域目录预检；旧 `integrations.json` 存在、目录不可写、schema 未到 head、数据库不可达均
  fail-closed。
- [ ] 身份迁移与终态清理由 runbook 独立执行并保存各自报告；预检不暗中执行写操作。
- [ ] 分页超过 100、空库、只有终态、含 awaiting-approval/clarification/running/created 的边界全部覆盖。
- [ ] 预检重复运行不改数据库/配置文件；网络只允许目标 PostgreSQL，不解析或连接任何已登记资源。

### Task 5：W5-A 集成、自审与合并门

**Files:**

- Modify: `ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`
- Modify: `docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md`
- Modify: `tests/contract/test_doc_fact_binding.py`

- [ ] 更新稳定架构与已批准规格：release admission snapshot 与历史 rendering binding 分离、30 天保留、
  旧身份仅一次性输入；只把本计划获批的确定值写回规格，不改真实调用门。
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
  runtime profile=release、长期服务无旧身份文件、真实调用默认关闭、配置域挂载矩阵不变。
- [ ] 合成 base+release 和可选 provider override 的最终 Compose 再断言；不能只读单个 YAML。
- [ ] 证明 release 缺边缘证据/必要变量时预检失败；不把 OAuth/Activation 容量冒充 rate limit。

### Task 7：实现不可变镜像 release override

**Files:**

- Create: `docker-compose.release.yml`
- Delete: `docker-compose.feishu.yml`
- Modify: `docker-compose.lan.yml`
- Modify: `docker-compose.model.yml`
- Create: `docs/examples/w5-release.env.example`
- Modify: `scripts/compose_smoke.py`
- Modify: `src/xiaowei_agent/interfaces/feishu_listener.py`
- Modify: `src/xiaowei_agent/interfaces/feishu_worker.py`
- Modify: `README.md`
- Modify: `tests/contract/test_feishu_listener.py`
- Modify: `tests/contract/test_feishu_worker.py`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_release_compose.py`
- Modify: `tests/security/test_ri5_compose_boundary.py`
- Modify: `tests/security/test_release_boundary.py`

- [ ] `XIAOWEI_RELEASE_IMAGE` 必须是 `name@sha256:<64 hex>`；release 对 migrate/api/worker/web/channel
  services 全部清除 `build`，禁止现场构建与 mutable tag。
- [ ] release 固定 `XIAOWEI_RUNTIME_PROFILE=release`、Web enabled、HTTPS mode；public origin、bind IP、
  environment/actor 由脱敏模板显式提供，缺失即 Compose 解析失败。
- [ ] listener/channel-worker 使用同一个审计开关，显式覆盖 base 的字面 `false`；默认 false。OAuth、Gemini
  和所有真实测试开关各自默认 false，不能互相联动。
- [ ] release 只发布 Web；API 的 base loopback 端口在 release 最终模型中也被清除。PostgreSQL、worker、
  listener、channel-worker 不发布端口。
- [ ] 删除长期旧身份 override。一次性身份迁移在 runbook 用只读临时 mount 调 CLI，命令退出后没有容器
  持有该 mount。
- [ ] provider 未配置时 Web 仍 ready；被明确启用但配置缺失的 listener/channel-worker 进入可观测
  `waiting_for_config`，不联网、不忙循环，配置完成后由受控重启加载，不承诺热加载。
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
  迁移/身份/保留清理报告、初始改密、健康检查和回滚 owner。
- [ ] 回滚演练只回滚镜像/非秘密配置；数据库 schema 默认 forward-only。紧急关闭某 Provider 必须重建对应
  进程并证明 mount/adapter/readback 已消失，不能只改 UI 状态。
- [ ] canary 固定用户/群、时间窗、观测项和停止条件；UAT 分角色覆盖 Local Admin、Operator 和结果深链普通
  用户。未获真实调用 GO 的功能只验“待配置/未授权”，不伪造成功。
- [ ] 文档显式说明：没有 edge 运行证据的 LAN 只能算本地体验，不是 canary。

### Task 9：W5-B 验证与合并门

**Files:**

- Modify: `ARCHITECTURE.md`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`
- Modify: `tests/contract/test_doc_fact_binding.py`

- [ ] `docker compose ... config --quiet` 覆盖最小 release、渠道关闭、渠道启用、Gemini 获准四种闭集组合；
  禁止 `config --environment` 或输出完整解析配置。
- [ ] 构建一次不可变候选镜像并记录 digest；本地/CI smoke 不接真实 Provider/目标。
- [ ] 重跑四门、Compose smoke、wheel 检查、1280/1440 桌面与窄屏只读详情视觉检查。
- [ ] exact-SHA 独立复审与 CI 通过后由负责人合入。此时最多写“W5 离线发布资产完成”，不得写 deployed。

## 6. W5-C 任务：获准环境执行

### Task 10：取得部署 GO 并冻结执行输入

- [ ] 负责人书面指定：目标主机、维护窗口、release SHA/image digest、HTTPS edge、canary 范围、验收人、
  旧版本恢复方式、DB/配置备份位置和回滚 owner。
- [ ] 对 RI2、RI3、RI4、H 层逐项记录 GO/NO-GO。NO-GO 项在最终 Compose 中仍为 false/disabled；
  **W5 GO 不代替任何真实调用 GO。**
- [ ] 核对目标主机 Docker/Compose 版本、磁盘、端口、DNS/NTP、证书链和 Secret/config 文件权限；输出脱敏。
- [ ] 证据文件在执行前固定模板与存放位置；不把 Secret 或生产数据复制进 Git。

### Task 11：备份、迁移、预检和首次启动

执行顺序固定，任一步失败即停止并按 runbook 回滚，禁止在线改源码：

1. 记录旧服务/进程并取得数据库与三域配置恢复点；证明旧 listener 已停，避免双消费。
2. 拉取并核对不可变 image digest；用 release 文件集合运行 `config --quiet`。
3. 启动 PostgreSQL（若由本 Compose 管理）并运行 schema migration；不做普通 downgrade。
4. 停止长期进程后显式迁移旧 `integrations.json`；重读三域并执行 `config_preflight`。
5. 有旧身份文件时只读临时挂载并执行迁移两次，保存“首次结果 + 第二次零创建”；无文件记录
   `not_applicable`。
6. 执行终态激活 30 天清理并确认每日调度/失败告警；执行 release task drain preflight。
7. 启动 Web/worker/API；只有获准渠道才启动 listener/channel-worker。核对 health/readiness、容器 image digest、
   端口面和 generation/load receipt。
8. 使用 `admin/admin` 首登并立即改密；旧 Session 必须失效。受信 LAN 改密前可达是已接受风险，不表示
   可以跳过改密。

完成本 Task 只能标记 `deployed SHA`。

### Task 12：回滚演练、canary 与 user-accepted

- [ ] 在真实用户进入前执行受控回滚演练：停止渠道、回退镜像/非秘密配置、核对 readback，再恢复候选。
- [ ] edge 对五组路由完成正常、429、恢复和告警证据；缺一项不得开始 canary。
- [ ] canary 只开放给已指定用户/群，观察登录/OAuth、激活积压、任务错误、投影重试、进程重启、DB、edge
  拒绝和配置 readback。命中停止条件立即回滚，不在现场改代码。
- [ ] UAT 按获准功能矩阵验收：登录/强制改密、Operator 工作台、Admin 用户/激活/审计/配置/资源登记、
  普通用户只读具体结果链接（若 R1 尚未实现则明确不验数据库结果门户）、统一失败提示。
- [ ] 分别签署 `deployed SHA`、`canary`、`user-accepted`；更新 handoff。W5 只有三层证据都取得后才可归档。

## 7. 独立门与明确非目标

- **W4c**：StarRocks/Prometheus 测试连接、DNS、登录、SQL/HTTP 探针；不在 W5 偷做。
- **R1**：数据库结果 artifact、ACL、预览、导出与“我的结果”；不在 W5。
- **H 层**：生产只读授权尚未签认；W5 只提供关闭态与证据槽位。
- **RI2 / RI3 / RI4**：真实飞书、Gemini、StarRocks 各自 GO；一个 GO 不外推给另一个。
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
| deployed SHA | 主机、SHA/digest、备份、迁移、预检、health/readback、端口面 | canary |
| canary | edge 429/恢复/告警、受控范围、观察窗、停止条件、回滚演练 | user-accepted |
| user-accepted | 指定验收人按功能矩阵签字，未授权功能如实标注 | 全量上线或未获批 Provider 可用 |

计划、源码、离线测试、真实环境、部署、canary 与用户验收必须在交付报告中分栏表述。
