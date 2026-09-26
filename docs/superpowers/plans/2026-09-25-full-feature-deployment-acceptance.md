# 完整功能部署验收 实施计划（Draft V0.1，待批准）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **状态**：草案，待负责人审批。本计划**不授权**任何源码、Compose、数据库或真实环境改动，也不授权任何真实调用、
> 部署、canary 或 UAT。批准本计划只确认顺序与门槛；每个切片仍按 AGENTS 要求各自取得开工口令，
> 每个真实调用各自取得现场 GO。
>
> **2026-09-26 变更**：用户决定取消 W5-C，本计划 S1 / G1 作废，不再执行；其目标环境证明事项转入两容器正式部署 /
> S7 RI6。依据见 [当前状态](../../../AGENT_HANDOFF.md#current-status) 第 6 节“W5-C 取消”一条。

**Goal:** 在不删除、不绕过既有安全链的前提下，把 Gemini、飞书、StarRocks、Prometheus、Web、权限、任务与结果
预览依次推进到可真实使用，并分层取得 `test-env verified → deployed SHA → canary → user-accepted` 证据。

**Architecture:** 不新建执行路径。所有真实能力仍走
`IntentDraft → CapabilityResolver → PlanCompiler → WorkflowRunner → StepAdmission(ToolPolicy → SQLGuard → ApprovalGate*) → ToolGateway → Evidence → Outcome`；
模型只产草案与解释。每个 Provider/目标先在测试环境单独验证，再经一个新的“release 真实能力准入”设计按
Provider 逐个进入 release；W5 的 provider-off release 是起点，不是被放宽的对象。

**Tech Stack:** Python 3.11、FastAPI/Starlette、PostgreSQL 16、Docker Compose ≥ 2.24.4、google-genai、lark-oapi、
PyMySQL、sqlglot；Prometheus/Alertmanager HTTP 客户端待本计划切片 P 的 ADR 决定。

**核对基线**：`main@d4e619a1b25553c64f39f93df81fef50e63dc001`（PR #87，本机 Provider 测试探针覆盖）。
W5-A/W5-B 已由 PR #84/#85 合入；W5-C 未获 GO；RI2、RI3 PR 3E、RI4、RI6、H 层、W4c、R1、M8 均未开放。

## Global Constraints

- LLM 不拥有执行权；最终 capability、目标、SQL、审批、写操作与工具顺序只由确定性链路决定（AGENTS 边界 1–2）。
- 工具只经 `ToolGateway`；SQL 确定性生成并经 sqlglot AST 校验（AGENTS 边界 4–5）。
- **E1（任何修改被管运维目标的操作）持续关闭**；只有 M8 的三项进入条件齐备才可开放，本计划不开放（DEVELOPMENT_PLAN M8）。
- 生产只读连接属于 **H 层**，尚未签认；未签认前所有真实目标只能是负责人批准的 **test** 环境（ADR-007、M6b §3.3 第 1 项）。
- 一个 GO 不外推给另一个：RI2、RI3、RI4、Prometheus、H 层、RI6、R1、M8 各自独立（DEVELOPMENT_PLAN §6、W5 §7）。
- Secret 只以文件形式由操作者创建并只读挂载给需要它的唯一进程；不进 Git、命令参数、日志、证据或聊天。
- 证据分级不可合并：`tests`、`test-env verified`、`deployed SHA`、`canary`、`user-accepted` 分栏记录。
- 本地 Docker、CI、截图、`readyz` 不是部署/canary/UAT 证据（W5 §7）。
- 每个切片：独立详细计划 → TDD → 独立 exact-SHA 复审 → 负责人合入；本计划只是总控，不替代切片详细计划。

---

## 1. 已具备的功能（源码事实，最强证据 `tests`，除非另注）

| 领域 | 已有 | 证据上限 / 限制 |
| --- | --- | --- |
| Web 登录与会话 | Local Admin 登录、首次强制改密、Session/CSRF/Origin/Host 校验、`__Host-` cookie、lan_http/https 两种模式 | tests；本机 2026-09-25 release 形态本地体验（非证据） |
| 权限 | admin/operator/user 三角色；`AdminCapability` 闭集；`MANAGE_INTEGRATIONS` 只给 Local Admin；飞书身份需激活审批 | tests |
| Admin | 用户目录、角色/状态变更、待激活申请审批、Admin 审计（两阶段、审计不可写即回滚） | tests |
| 配置 | `.config/{ai,feishu,resources}` 三域、唯一写服务、按进程挂载矩阵、加载回执、迁移器与预检 | tests；无真实环境迁移 |
| 资源登记 | StarRocks/Prometheus 参数登记，本地校验、零网络 | tests；**只登记不接入** |
| 任务 | 提交、TaskStore（CAS/lease/fencing/终态保护）、worker、澄清、任务列表与安全详情页 | tests；PostgreSQL integration |
| Gemini | `gemini_model.py` 固定 provider/model 的 adapter；`IntentModelPort`、`SlowQueryAdvisoryPort`；失败回落规则解释；Key 只给 worker | RI3 PR 3A–3D tests；PR 3E 未执行 |
| Provider 探针 | Web 管理面 Gemini/飞书“测试连接”；本机覆盖 `docker-compose.local-test.yml` 可打开（PR #87） | tests；release 固定关闭 |
| 飞书 | OAuth adapter（RI1）、listener、channel-worker、渲染与投影订阅、群成员校验 | M7 PR 1–8 离线验收；真实调用 0 |
| StarRocks | 两个 operation 的慢查询 capability；`StarRocksReadonlyAdapter` + PyMySQL、target-bound Gateway、version/grants/DDL/identity digest 预检 | M6b 离线；**产品入口未装配 live assembly**；目标目录是占位 `dev → starrocks-dev-1` |
| Prometheus / Alertmanager / 资产 | `prometheus.alert.evidence`、`asset.inventory.lookup` capability 与 PromQL 模板准入 | **只有 fake/recording adapter**，没有真实 adapter |
| 审批链 | `ApprovalGate` 在 StepAdmission 中；plan_hash / target_fingerprint 绑定 | tests；**当前没有任何需要审批的 capability**（全部只读） |
| 写入的边界 | 系统内部写已有：Admin 配置、用户目录与角色、激活审批、Admin 审计、TaskStore/evidence 持久化 | **被管运维目标写操作（E1）尚未实现**，也不在本计划开放 |
| 发布 | W5 provider-off release：不可变 digest、字面关闭开关、部署前检查、发布预检、runbook 与四份清单 | W5-B 离线；W5-C 未执行 |

## 2. 仍缺的配置与代码

### 2.1 代码缺口

| # | 缺口 | 为什么必须补 | 落点 |
| --- | --- | --- | --- |
| C1 | 首登与入口体验 4 项：登录后 `password_change_required` JSON、`/` 404、登录页常显飞书文案、工作台无管理中心入口 | 真实用户无法自行完成首登 | `interfaces/web_app.py`、`web_static/*` |
| C2 | **release 真实能力准入**：release 当前只有空快照；需要按“已获 GO 且已加载配置的 Provider”逐个准入 capability，不能用环境变量传能力列表（W5 §2.2 末条） | 没有它，任何真实能力都进不了 release | 新 ADR + `interfaces/local_stack.py`、`config.py`、`docker-compose.release.yml` |
| C3 | **runtime/evidence provenance**：在同一数据库从 provider-off 升级到真实能力前，必须持久化每个 plan/evidence 来自哪个运行形态与 Provider（W5 §2.5 末条） | 否则无法区分历史与真实结果、无法做发布预检 | `persistence/` 迁移 + `evidence/` |
| C4 | Gemini 在 release 的装配：release 目前把 `XIAOWEI_GEMINI_ENABLED` 固定为 `false`；RI3 PR 3E runbook 仍按旧 `.env`/`docker-compose.model.yml` 写，需按 W4a 的 `.config/ai` 刷新 | C2 的第一个 Provider | `docs/runbooks/`、`scripts/ri3_gemini_probe.py` |
| C5 | 飞书 release 装配：listener/channel-worker/OAuth 在 release 固定关闭；RI2 计划早于 W4a/W5，需按三域配置与 release 文件集刷新 | C2 的第二个 Provider | RI2 计划修订、Compose |
| C6 | StarRocks：产品 worker 入口未传 `StarRocksLiveAssembly`；目标目录是代码占位；超时仍为 25/30 秒（RI4 计划要改为 180/190/195/200）；W4b 登记的资源**不参与**目标解析 | RI4 的前置代码 | `capabilities/target.py`、`interfaces/worker.py`、`tools/starrocks.py`、ADR-012 修订 |
| C7 | **Prometheus/Alertmanager 真实只读 adapter**：完全没有；没有 ADR、计划或现场门 | “Prometheus 可用”的前提 | 新 ADR（承接 ADR-011）+ `tools/prometheus.py`、`tools/alertmanager.py` |
| C8 | 资产查询真实数据源：未指定来源系统 | 若不指定，release 中该 capability 保持不可用 | 待负责人决定来源；否则明确“不适用” |
| C9 | **结果预览（R1）**：`/results/{result_ref}`、预览、ACL（仅申请人与审批人）均未实现；W0–W5 明确不建 | “结果预览”验收项 | 新 ADR + 计划；独立门 |
| C10 | W4c：Admin 登记资源 → 测试连接/纳入目标，需另修 ADR-007 并指定唯一 task-worker 路径 | 若要“在 Admin 登记即可用” | 独立门；本计划推荐**不走 W4c**，目标仍由代码拥有 |

### 2.2 配置与外部材料缺口（全部由负责人/运维提供，Claude 不代填）

- Gemini：项目与 API Key（文件）、数据保留/训练/区域/日志政策确认、账户用量与费用告警 readback、synthetic corpus。
- 飞书：测试企业自建应用、App ID/Secret（文件）、权限范围、回调域名（已有 HTTPS SSO）、身份映射、数据范围与保留、调用窗口。
- StarRocks：M6b §3.3 全部 16 项（唯一 test 目标、只读账号与 password file、带外 version/grants/DDL/identity 原始材料、字段脱敏、recording 与证据处置、网络 allowlist、窗口与操作者）。
- Prometheus/Alertmanager：唯一 test 目标 URL、认证方式与凭据文件、允许的 PromQL 模板与时间窗上限、网络 allowlist、脱敏字段。
- 部署：目标主机、维护窗口、HTTPS SSO 域名与 edge（含限流证据）、不可变镜像仓库、DB 与配置备份位置、回滚 owner、canary 验收人、UAT 签字人。
- H 层：若要连接**生产**只读目标，需先签认 H 层授权面；否则全程仅 test 目标。

## 3. 需要负责人明确 GO 的事项

每一项都是**独立口令**，缺一项只阻塞对应 Provider，不阻塞其他离线切片。

| GO | 触发的真实动作 | 前置 |
| --- | --- | --- |
| G0 本计划批准 | 无（只确认顺序） | — |
| G1 W5-C GO | 在指定主机部署 provider-off 产品壳、canary、UAT | W5 Task 10 冻结输入 |
| G2 “RI3 Gemini test-env GO” | worker 首次读取真实 Key 并调用 Gemini | RI3 PR 3E 现场条件 7 项 |
| G3 “开始 M7 真实渠道验证”（RI2） | 注册/启用飞书应用、OAuth、长连接、发消息 | M7 §0.3.2 全部勾选 |
| G4 RI4 现场 GO | 连接唯一 test StarRocks、执行两条只读 SQL | M6b §3.3 16 项 + 带外 digest 已批准 |
| G5 Prometheus test-env GO | 连接唯一 test Prometheus/Alertmanager | 切片 P 的 ADR 与计划获批 |
| G6 H 层签认（可选） | 允许任何生产只读连接 | ADR-007 修订；未签认则全程 test |
| G7 RI6 部署 GO | 正式主机部署带真实能力的 release | 已通过的 Provider 各自达到 `test-env verified` |
| G8 RI6 canary GO | 对指定用户开放真实能力 | G7 部署证据 + edge 限流证据 + 回滚演练 |
| G9 UAT 签字 | 验收人按功能矩阵签字 | G8 canary 结束 |
| G10 R1 / M8（独立） | 结果预览服务 / 第一条受控写 | 各自 ADR 与计划；本计划不开放 |

## 4. 切片与执行顺序

```text
S0 首登体验修复(离线) ─┐
S1 W5-C 产品壳部署(G1) ─┤
S2 RI3 Gemini(G2)  ─────┤
S3 RI2 飞书(G3) ────────┼─→ S6 release 真实能力准入 + provenance(离线, ADR) ─→ S7 RI6 部署/canary/UAT(G7–G9)
S4 RI4 StarRocks(代码→G4)┤
S5 Prometheus(ADR→代码→G5)┘
S8 R1 结果预览、M8 受控写：独立门，不在本计划执行
```

S2–S5 互不依赖，可按外部材料就绪顺序推进；S6 可在 S2 完成后先以 Gemini 为第一个 Provider 落地，其他 Provider
达到 `test-env verified` 后逐个追加。

### Task S0：首登与入口体验修复（离线，需开工口令）

**Files:**
- Modify: `src/xiaowei_agent/interfaces/web_app.py`（`login`、`shell`、`admin_shell_route`、根路由）
- Modify: `src/xiaowei_agent/interfaces/web_static/login.html`、`index.html`、`app.js`
- Test: `tests/contract/test_web_app_routes.py`、`tests/security/test_ri5_web_assembly.py`

**Interfaces:**
- Produces: 未改密会话访问 `/app`、`/admin` 时 `302 → /login`；`POST /login/api/login` 在 `must_change_password` 时返回 `{"status":"ok","destination":"/login"}`；`GET /` → `302 /login`；登录页飞书入口仅在 `feishu_oauth_enabled` 时渲染；工作台对 `role == "admin"` 显示“管理中心”链接。

- [ ] **Step 1: 写失败测试**

```python
async def test_first_login_is_sent_to_the_password_change_shell(client) -> None:
    response = await client.post("/login/api/login", json=_admin_login("admin", "admin"))
    assert response.json()["destination"] == "/login"
    page = await client.get("/app", follow_redirects=False)
    assert (page.status_code, page.headers["location"]) == (302, "/login")


async def test_root_redirects_to_login(client) -> None:
    response = await client.get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (302, "/login")


async def test_login_shell_hides_feishu_when_oauth_is_off(client_oauth_off) -> None:
    body = (await client_oauth_off.get("/login")).text
    assert "feishu-oauth" not in body and "飞书身份登录" not in body
```

- [ ] **Step 2:** 运行 `python -m pytest tests/contract/test_web_app_routes.py -q`，确认 3 条失败且失败原因为断言不符。
- [ ] **Step 3:** 最小实现：`login` 按 `issued` 的 `must_change_password` 选 destination；`shell`/`admin_shell_route` 捕获 `_PasswordChangeRequiredError` 返回 `RedirectResponse("/login", 302)`；新增 `GET /`；登录壳按设置渲染飞书区块；`app.js` 对 admin 渲染 `<a href="/admin">管理中心</a>`。API 路由（`/app/api/*`、`/admin/api/*`）继续返回 JSON 403，不改。
- [ ] **Step 4:** 运行四门：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`，全绿。
- [ ] **Step 5:** 反证：撤掉 `/app` 重定向，首条测试转红；恢复。
- [ ] **Step 6:** 提交 `fix(web): route first login to password change and add admin entry`，开 PR，等待复审。

### Task S1：W5-C provider-off 产品壳部署（G1）

完全按已合入的 [W5 runbook](../../runbooks/w5-product-deployment.md) 与 W5 计划 Task 10–12 执行，本计划不改写其步骤。
建议在 S0 合入后以新 digest 执行，避免首登问题进入 canary。

- [ ] 负责人书面冻结 Task 10 输入（主机、窗口、digest、edge 与限流证据、验收人、备份位置、回滚 owner、首次数据处置）。
- [ ] 按 runbook §2–§3 部署并回填 [部署证据清单](../../checklists/w5-deployment-evidence.md)。
- [ ] 按 §4–§5 回滚演练与 canary，回填 [canary 清单](../../checklists/w5-canary-evidence.md)。
- [ ] 按 [UAT 清单](../../checklists/w5-user-acceptance.md) 签字。

### Task S2：RI3 Gemini test-env 验证（G2）

- [ ] **Step 1: 刷新 PR 3E 执行资产（离线 PR）**：把 [RI3 计划 Task 5](2026-09-10-model-provider-adapter.md) 的 `.env` / `docker-compose.model.yml` 步骤改为 W4a 的 `.config/ai/config.json` + worker 只读挂载；补 `docs/runbooks/ri3-gemini-test-env.md` 与 `scripts/ri3_gemini_probe.py`（只提交固定 case ID，不读 Key、不直连 SDK）。测试：runbook 契约测试断言文件集合与“Key 只挂 worker”。
- [ ] **Step 2: 现场前置**：负责人确认数据政策、费用告警 readback、synthetic corpus，下达 G2。
- [ ] **Step 3: 现场执行**：按 PR 3E Step 1–8（默认关闭反证 → 启用 → smoke → 10×3 意图与 5 个 advisory → 受控取消 → 日志/secret 扫描 0 命中 → 回滚 → 证据）。
- [ ] **退出**：证据只写 `test-env verified`；生产调用仍关闭。

### Task S3：RI2 飞书 test-env 验证（G3）

- [ ] **Step 1: 刷新 RI2 计划（离线 PR）**：[RI2 计划](2026-09-10-feishu-test-environment-validation.md) 改用 `.config/feishu` 三域配置、W5 身份迁移命令与当前 Compose 文件集；不复制或放宽 M7 §0.3.2。
- [ ] **Step 2: 现场前置**：M7 §0.3.2 每项勾选；旧小维停机；负责人说“开始 M7 真实渠道验证”。
- [ ] **Step 3: 现场执行**：OAuth 登录与激活审批、长连接收消息、发送与更新卡片、群成员校验、停用/撤权即时生效、回滚（关闭 listener/channel-worker 与 OAuth，确认 0 出站）。
- [ ] **退出**：只写 `test-env verified`。

### Task S4：RI4 StarRocks（代码 → G4）

- [ ] **Step 1: ADR-012 修订 + RI4 代码（离线 PR）**：按 [RI4 计划](2026-09-10-starrocks-readonly-live-validation.md) 改分层超时 180/190/195/200 秒；新增代码拥有的 test 目标条目（canonical resource ID 由负责人给出，禁止 host/账号进入 ID）；worker 入口在 `starrocks_adapter_mode=test_readonly` 时装配 `StarRocksLiveAssembly`，其余形态保持现状。W4b 资源登记**不**参与目标解析（W4c 未开放）。必测：目标唯一性、`prod` 不在目录、非 test 形态不构造 PyMySQL、digest 不匹配 fail-closed、超时层级严格递增。
- [ ] **Step 2: 带外材料**：DBA 在窗口外提供 version/grants/DDL/identity 原始值，操作者用运行时同一 normalizer 离线算 digest，负责人批准。
- [ ] **Step 3: 现场执行（G4）**：preflight 四项 digest 匹配 → 两个 operation 各跑批准场景 → 超时/权限不足场景 → 证据脱敏核对 → 回滚到 `disabled`。
- [ ] **退出**：只写 `test-env verified`；生产目标需 G6 并重走 M6b §3.3。

### Task S5：Prometheus/Alertmanager 真实只读（ADR → 代码 → G5）

- [ ] **Step 1: ADR-018 与详细计划（纯文档 PR）**：承接 ADR-011 的 PromQL 模板准入，决定 HTTP 客户端、target-bound binding、认证（token/basic 文件）、连接/读/总超时、响应大小上限、时间窗与步长上限、标签/注解脱敏、错误闭集、recording 策略；明确只调用 `GET /api/v1/query_range`、`GET /api/v2/alerts`（只读），禁止任何 admin/silence/写 API。
- [ ] **Step 2: 实现（离线 PR，TDD）**：`tools/prometheus.py`、`tools/alertmanager.py` 返回 `AdapterResponse`；只能经 Gateway；安全测试覆盖模板外 PromQL、URL 覆盖、重定向、超大响应、SSRF 目标漂移、注解注入文本作为 `ExternalContent`。
- [ ] **Step 3: 现场执行（G5）**：唯一 test 目标、固定场景、脱敏证据、回滚到不装配。
- [ ] 资产查询（C8）：负责人指定来源系统前，release 中 `asset.inventory.lookup` 保持不可用并在 UAT 中标“不适用”。

### Task S6：release 真实能力准入 + provenance（离线，ADR 先行）

- [ ] **Step 1: ADR（纯文档 PR）**：定义 `release` 下的 capability 准入真源 = 代码拥有的 “Provider → capability” 映射 × 该 Provider 的 release 开关（Compose 字面量，逐 Provider 单独 override 文件）× 进程加载回执为当前代次 `loaded`；三者缺一即不准入。禁止环境变量列能力、禁止 recording 进入 release。定义 provenance 字段（runtime profile、provider 集合摘要、image digest）写入 plan/evidence。
- [ ] **Step 2: 实现（离线 PR，TDD）**：迁移新增 provenance 列（forward-only）；`_conversation_snapshot` 与执行 binding 按准入结果构造；部署前检查扩展为“每个启用的 Provider override 都在批准集合内”；发布预检区分 provider-off 与真实能力数据。必测：未加载回执不准入、开关与回执代次不一致不准入、fake 模块仍不出现在 release `sys.modules`、W5 provider-off 行为不变。
- [ ] **Step 3: 逐 Provider 追加**：每个达到 `test-env verified` 的 Provider 一个小 PR：新增其 release override 文件与部署前检查规则、release smoke 场景（离线 fake Provider 在 CI 中只验证装配，不联网）。

### Task S7：RI6 带真实能力的部署、canary、UAT（G7–G9）

- [ ] 刷新 [RI6 计划](2026-09-10-compose-deployment-canary-uat.md)：以 W5 release 文件集为基础，删去 `0.0.0.0:8080` 与 `.env` Key 路径等过时内容，改为 S6 的逐 Provider override。
- [ ] 按第 5 节部署前/中/后步骤执行并分层记录证据。

## 5. 验收步骤

### 5.1 部署前（每次部署都做）

- [ ] 被部署 SHA 已经 exact-SHA 复审合入，CI 8/8 绿；镜像以 `name@sha256` 推送，source-sha 标签一致。
- [ ] 干净 shell；`python -m scripts.release_compose --env-file …` 为 `release-compose: ok`；启用的 Provider override 全在已批准集合。
- [ ] 备份：DB 逻辑导出 + `.config/{ai,feishu,resources}`；记录位置与校验。
- [ ] `migrate` 退出 0；`config_preflight` 为 `preflight: ok`；`release_preflight` 为 `result=ok`。
- [ ] 每个启用 Provider 的 Secret 文件只挂给其唯一消费进程，其他容器反证 absent。
- [ ] 回滚目标 digest 与回滚命令已写入证据清单并经 owner 确认。

### 5.2 部署中

- [ ] 按 runbook 顺序启动；任一步失败即停并回滚（第 6 节）。
- [ ] readback：容器 image digest、source-sha 标签、端口面（只有 Web 发布在模板地址）、`XIAOWEI_RELEASE_*` 不入容器、每个 Provider 开关与加载回执代次。
- [ ] 首登经 HTTPS edge 完成强制改密。

### 5.3 部署后（功能矩阵，逐项记“通过 / 失败 / 不适用”）

| 领域 | 场景 | 通过判据 |
| --- | --- | --- |
| Web | 登录、强制改密、登出、Session 过期、直接 IP/8080 访问 | 改密前业务页重定向；直接访问 403 |
| 权限 | admin/operator/user 三角色访问矩阵；飞书用户未激活 → `activation_pending`；停用/撤权即时生效 | 越权全部 403；审计有记录 |
| Admin | 配置保存与回执、资源登记、用户/激活/审计 | 审计两阶段完整；Secret 不回显 |
| Gemini | 意图草案、advisory、模型失败回落 | 最终 capability 与模型无关；回落时任务仍成功 |
| 飞书 | OAuth 登录、群内提问、卡片更新、任务链接跳转 | 渠道与 Web 语义一致 |
| StarRocks | 两个 operation 的批准场景；超时；权限不足 | 只执行批准 SQL；失败 fail-closed |
| Prometheus | 告警与指标范围查询批准场景 | 只调用两个只读 API；模板外被拒 |
| 任务 | 提交、排队、澄清、终态、重启后恢复 | 终态不被覆盖；重启不重复执行 |
| 结果预览 | 任务详情中的证据摘要 | 仅安全字段；**R1 未开放则“不适用”** |
| 写操作 | 任意写意图（重启、kill、改配置） | 确定性拒绝为 capability unavailable；E1 调用为 0 |
| 安全 | 日志/trace/DB 中 Secret canary 扫描；出站目的地仅 allowlist | 0 命中；无额外目的地 |

canary：只对指定验收人、固定时间窗；观测错误率、延迟、Provider 用量与费用告警、edge 429、审计事件。UAT：验收人按上表签字，未开放项如实标“不适用”。

## 6. 回滚方案与失败停止条件

### 6.1 回滚（由轻到重）

1. **单 Provider 回滚**：移除该 Provider 的 release override，`up -d --force-recreate` 相关进程；readback 证明 Secret 挂载消失、准入快照不再含其 capability、后续出站为 0。已完成任务的安全证据保留可读。
2. **镜像回滚**：回到上一个已知良好的 W5-compatible release digest（先跑 §5.1 预检）；数据库 forward-only，不做普通 downgrade。
3. **首次部署回滚**：停止全部应用服务，保留数据库与证据。
4. **禁止**：回到 recording 镜像、把 `XIAOWEI_RUNTIME_PROFILE` 改回 `offline_recording`、在线改源码、用临时 SQL 删数据。

### 6.2 失败停止条件（命中任一项立即停止并按 6.1 回滚）

- 任一 Secret canary 在日志、trace、数据库安全字段、证据或页面中命中。
- 出现 allowlist 之外的出站目的地，或任何写类 API / 非批准 SQL / 模板外 PromQL 被执行。
- release 进程中出现 fake/recording 模块，或未获 GO 的 Provider 被装配。
- 最终 capability、目标或 SQL 随模型输出变化（与规则解释对照样本不一致）。
- 跨租户/跨环境/越权读取成功，或停用/撤权后仍可访问。
- 终态被覆盖、任务重复执行、`indeterminate` 被显示为成功。
- 预检 digest 不匹配却继续执行；超时层级被突破。
- Provider 费用/用量告警触发，或 edge 限流证据缺失。
- canary 窗口内错误率或延迟超过负责人事先书面给定的阈值（阈值在 G8 时冻结）。

## 7. 明确不做

- 不开放 E1/M8、R1、W4c、H 层；不连接生产目标（除非 G6 另行签认）。
- 不删除或绕过 Resolver/Planner/StepAdmission/SQLGuard/ApprovalGate/Gateway 任一环。
- 不引入任意 SQL、模型生成 SQL、任意 PromQL、导出、通用 Provider 插件或动态能力列表。
- 不在本计划批准前修改源码、Compose、数据库或真实环境。
