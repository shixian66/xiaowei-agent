# W0 Web 产品 ADR 与真源收口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已复核并通过 PR #58 合入的 Web 运维工作台、身份激活与未来结果访问设计，收口为当前可执行的 ADR、总体架构、路线、README 和 handoff 真源；只修改文档与文档契约测试，不实现任何 W1a 或后续产品代码。

**Architecture:** 采用“历史 ADR 原文保持可追溯 + 统一追加 2026-09-20 Web 产品修订”的方式，不静默改写既有 RI5 决策。稳定文档明确区分“当前已经实现的 RI5 单文件/loopback 运行事实”和“W4a/W5 才会实现的三域配置与 release 目标”。`tests/contract/test_doc_fact_binding.py` 用闭集、顺序与反例把四条已接受条款、W0–W5 顺序和当前/未来边界机械绑定；`AGENT_HANDOFF.md` 只记录实际基线、候选证据和未覆盖项。

**Tech Stack:** Markdown ADR/架构/计划/运行手册、Python 3.11、pytest 文档契约测试、Git/GitHub CI；不新增运行依赖、迁移、Compose 服务或应用代码。

**Spec:** [小维 Web 运维工作台、身份激活与未来结果访问边界总体设计](../specs/2026-09-19-web-operations-console-identity-activation-design.md) V0.3；设计通过 PR [#58](https://github.com/shixian66/xiaowei-agent/pull/58) 以 `4e5a844620b700e25d6a29e43687c1a4c876db16` 合入。W0 范围取自该规格 §17.1、§19 和 §20。

## Baseline and Evidence Boundary

- 本计划分支：`claude/w0-web-product-adr-plan`。
- 本计划基线：`origin/main@4e5a844620b700e25d6a29e43687c1a4c876db16`。
- PR #58 只证明总体设计已完成书面复核并进入 `main`；它不是 W0 文档收口的实现证据，更不是 W1a 源码、部署、真实调用或用户验收证据。
- 编写本计划前已按 `AGENTS.md` 顺序完整读取 `ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`，并读取规格、ADR-007/013/014/015 与现有文档事实测试。
- 计划分支基线四门：`3949 passed, 269 skipped`；security gate `1447 passed, 83 skipped, 2688 deselected`；Ruff 通过；mypy 对 189 个源文件通过。实现者必须从届时最新 `main` 重新核对，不能把这些数字当作未来运行结果。

## Global Constraints

- **W0 是文档与 ADR 阶段。** 允许修改 Markdown 与 `tests/contract/test_doc_fact_binding.py`；禁止修改 `src/`、migration、`pyproject.toml`、`uv.lock`、Compose、脚本、运行配置样例或静态页面。
- **W1a 未授权。** 不创建 `UserAccount`、`UserRoleAssignment`、`ExternalIdentity`、`LocalCredential`、`AdminCapability`、`AdminAuditStore`、`ActivationRequest` 或 `web_oauth_login_contexts` 的 Python/SQL 载体；不创建空目录、占位接口或 migration。
- **四条规范变更必须精确闭合。** 只修订 `ADR-007 D5`、`ADR-014 RI5 R1/R2/R3`；不得借 W0 放开原始配置 DTO、Secret、配置 mutation、连接测试、真实网络、E1、生产写或结果访问。
- **历史 ADR 不重写。** 原 RI5 条款保留为当时接受事实；在四份 ADR 中使用同名的“Web 产品修订（2026-09-20 Accepted）”段落说明哪些旧条款被窄替代、哪些边界继续有效。
- **当前事实与未来目标分开。** 当前运行代码仍使用 `.config/integrations.json`，当前基础 Compose 仍只发布 loopback，当前没有持久用户目录、Admin 审计、激活审批、三域配置或 release override。W4a/W5 目标不得写成当前启动步骤。
- **身份边界不混用。** 产品角色固定 `ADMIN / OPERATOR / USER`；认证来源独立；既有 `ChannelPermission` 三成员不扩展；管理面能力由未来独立 `AdminCapability` 承担。
- **结果访问不提前。** W0–W5 不创建 `/results/{result_ref}`、结果 artifact、`ResultAccessService`、requester/approver 名单或假结果页。未来 R1 仍要求申请人/该次审批人 ACL 真源，Admin 不自动越权。
- **配置不侵入能力。** W4b 只登记数据库/Prometheus 参数并做本地校验，网络调用为 0；W4c 是独立 ADR-007 修订、唯一 task-worker 路径与现场 GO。Web 永不直接持有运维目标客户端。
- **真实调用门不变。** RI2 飞书、RI3 PR 3E Gemini、RI4 StarRocks、RI6 部署/canary/UAT、H 层生产只读和 E1 各自保持独立硬门。
- **审计顺序不倒置。** `AdminAuditStore` 持久化与 append-only 写契约必须在 W1a 落地，W1b 激活审批复用它并在审计不可写时 fail-closed；W3 只增加查询 UI 与敏感查看审计。
- **Secret 不扩散。** Secret 不进入数据库、日志、trace、异常、DOM、审计、加载回执或 Git；未来 Web 仅在解析和原子写入期间短暂持有明文。
- **不改权威测试命令。** 验收仍使用 `python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。
- 每个任务先写可复现的文档契约测试并确认因目标事实尚未落地而失败，再在规范真源处最小修复；不得通过删除断言、扩大允许集合或把未来目标写成当前事实制造全绿。
- 每个提交只暂存本任务列出的文件；不使用 `git add -A`，不触碰无关未跟踪文件。

## Review Focus

审核者应优先核对以下六点：

1. 四条已接受规范变更是否逐条落在正确 ADR，且没有顺手放宽真实调用或结果 ACL。
2. `ADR-014` 是否把登录 state context 精确限制为 `web_oauth_login_contexts`，并保留连接测试 state 无 context、无 Session、无身份绑定的现行语义。
3. `ARCHITECTURE.md` 与 `README.md` 是否诚实地区分当前 `.config/integrations.json`/loopback 运行事实和 W4a/W5 未来目标。
4. `DEVELOPMENT_PLAN.md` 是否固定 `W0 → W1a → W1b → W2 → W3 → W4a → W4b → W5`，并把 W4c/R1 留在独立阻塞门；I3 只是延期的独立路线，不被伪装为已取消。
5. W1a 的持久审计写底座是否明确早于 W1b 激活审批，requester/approver 是否仍只属于未来 R1。
6. `AGENT_HANDOFF.md` 是否只记录 PR #58/W0 的真实文档与测试证据，没有声称源码、部署、真实调用或用户验收已经发生。

---

### Task 0: 从最新 main 重新入职并冻结允许变更面

**Files:** 无

**Interfaces:**
- Consumes: 五份优先文档、Web 产品规格、ADR-007/013/014/015、当前 Git/PR/CI 事实
- Produces: W0 实现基线记录与精确文件 allowlist

- [ ] **Step 1: 创建独立实现分支**

计划 PR 合入后，从届时最新 `origin/main` 创建新的 `claude/w0-web-product-adr` 分支或隔离 worktree。不得直接在本计划分支继续实现。

```bash
git fetch origin main
git status --short --branch
git rev-parse HEAD
git rev-parse origin/main
git log -1 --format='%H%n%s' origin/main
```

Expected: 新实现分支基于相同的最新 `origin/main`；工作树没有未知修改。若 `main` 在计划审核期间前进，先重新阅读真实 diff 和受影响文档，再决定计划是否需要小幅修订。

- [ ] **Step 2: 按项目顺序重新读取真源**

依次完整读取：

1. `AGENTS.md`
2. `ARCHITECTURE.md`
3. `AGENT_HANDOFF.md`
4. `README.md`
5. `DEVELOPMENT_PLAN.md`

随后读取规格、ADR-007/013/014/015 和 `tests/contract/test_doc_fact_binding.py`。发现当前文档或源码事实与本计划不一致时，先记录冲突并停下复核，不静默选择旧摘要。

- [ ] **Step 3: 跑开工基线**

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

Expected: 四门全绿。记录实际数字、Python 版本、精确基线 SHA；失败先归因，不在 W0 中顺手修无关代码。

- [ ] **Step 4: 冻结 W0 文件 allowlist**

W0 只允许修改：

```text
docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md
docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md
docs/adr/ADR-013-m7-channel-boundary.md
docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md
docs/adr/ADR-015-real-model-provider-boundary.md
ARCHITECTURE.md
DEVELOPMENT_PLAN.md
README.md
AGENT_HANDOFF.md
tests/contract/test_doc_fact_binding.py
```

如需第 11 个文件，必须先说明为什么上述真源无法承载，并重新审核计划；不能边做边扩项。

---

### Task 1: 把已复核规格从 Review Draft 收成 W0 唯一输入

**Files:**
- Modify: `tests/contract/test_doc_fact_binding.py`（现有 Web 产品规格门约在 452–546 行）
- Modify: `docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md`（头部 1–10 行、§17/§19/§20）

**Interfaces:**
- Consumes: PR #58 合入事实、规格现有 `_WEB_PRODUCT_ADR_CHANGES` 四项闭集
- Produces: 一个明确为 Approved、但只授权 W0 文档收口的规格真源

- [ ] **Step 1: 先补规格状态契约并确认 RED**

在 `test_doc_fact_binding.py` 复用现有 `_WEB_PRODUCT_SPEC`、`_accepted_adr_change_clauses()` 和 `_WEB_PRODUCT_ADR_CHANGES`，增加
`test_web_product_spec_records_approval_without_claiming_implementation()` 与
`test_web_product_spec_keeps_w1a_and_live_use_outside_w0_authority()` 两条测试。

断言必须覆盖：

- 头部状态为 `Approved V0.3`，并明确“只授权 W0 文档/ADR 收口”；
- 规划基线绑定 PR #58 的合入 SHA `4e5a844620b700e25d6a29e43687c1a4c876db16`；
- 四条 ADR 变更集合仍精确等于既有四成员；
- 规格仍明确没有本轮对应源码、运行、部署、真实调用或用户验收证据；
- W0 合入前不得写 W1a，W1a 仍需独立详细计划、审核与明确口令。

运行：

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
```

Expected RED: 现有头部仍是 `Review Draft V0.3`、基线仍是 `cc617f5…`，新增状态契约失败；既有 Web 规格闭集测试保持通过。

- [ ] **Step 2: 只修规格状态和证据口径**

把规格头部更新为已批准设计，记录 PR #58 合入基线；把 8–10 行的流程说明改为“当前只授权 W0 文档/ADR；W0 合入后才可写 W1a 计划，计划获批后才可 TDD 写源码”。同步 §20 的已满足设计退出条件，但不修改产品决策、不新增第 5 条 ADR 变更，也不写任何 W0 已完成或 W1a 已开始的声称。

- [ ] **Step 3: GREEN 与反例**

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
```

Expected: 全绿。再在测试内用现有 `_replace_once()` 构造内存反例：把 `Approved V0.3` 换回 `Review Draft V0.3`、删除“只授权 W0”任一保护，确认对应 predicate 为 false；不修改工作树做假变异。

- [ ] **Step 4: 提交规格状态收口**

```bash
git add tests/contract/test_doc_fact_binding.py docs/superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md
git commit -m "docs(w0): accept Web product specification for ADR closure"
```

---

### Task 2: 根因修订授权与 OAuth/激活 ADR

**Files:**
- Modify: `tests/contract/test_doc_fact_binding.py`
- Modify: `docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md`（D5 与 RI5 Amendment 后）
- Modify: `docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md`（RI5 R1/R2/R3 后、`## 后果` 前）

**Interfaces:**
- Consumes: 规格 §6.1–§8、§13、§19.2 的 `ADR-007 D5` 与 `ADR-014 RI5 R1/R2/R3`
- Produces: 飞书 Admin 脱敏状态窄读例外、受信 LAN 首启替换、唯一 login-context schema 例外

- [ ] **Step 1: 建立统一 ADR amendment 抽取器和精确条款门**

在测试文件增加 `_web_product_amendment(path)`：只读取统一标题 `## Web 产品修订（2026-09-20 Accepted）` 到后续 `## 后果` 的文本，不把历史 RI5 原文误当现行增补。

增加 `test_w0_adr_007_records_only_the_redacted_status_read_exception()`、
`test_w0_adr_014_replaces_r1_r2_r3_at_their_actual_security_boundaries()` 与
`test_w0_auth_adr_bindings_are_discriminating()` 三条测试。

`ADR-007` amendment 的必备事实：

- `VIEW_INTEGRATION_STATUS` 只给飞书认证的 `ADMIN` 返回独立脱敏状态投影；
- 投影只含域名、`configured`、`restart_required`、加载闭集状态和最近闭集测试结果；
- 原始配置 DTO、Secret、配置保存/清除、资源参数 mutation 与 probe 仍只接受 `LOCAL_ADMIN`；
- W4b 数据库/Prometheus 只做本地校验、网络调用为 0，资源 Secret 只挂 task-worker；
- W4c 必须另修 ADR-007、指定唯一 task-worker 路径并取得现场 GO；不授予 Web 目标客户端、E1 或真实调用。

`ADR-014` amendment 的必备事实：

- **R1 窄替代**：只放开上述脱敏状态投影，配置读写与 probe 保持 `LOCAL_ADMIN`；
- **R2 明示替代**：项目负责人接受受信 LAN 在首次改密前可达；补偿控制固定为醒目默认凭据文档、立即改密、改密前只允许改密/退出、release/canary 的边缘限流证据；回滚为 loopback 并撤销本地 Session；不把 LAN HTTP 写成公网安全；
- **R3 精确解冻**：只允许新增 `web_oauth_login_contexts`，`state_digest` 为唯一 PK/FK，return intent 闭集仅为 `WORKBENCH`、`SAFE_TASK_DETAIL(task_id)`、`ADMIN_CENTER`、`ACTIVATION_STATUS(request_id)`；不保存任意 URL/path；登录 state/context 同事务签发、同一次消费，缺失/多行/不匹配 fail-closed；连接测试 state 不产生 context；
- 登录与 OAuth 测试继续使用不同 digest domain、同一备案 callback；测试不签 Session、不解析身份目录、不绑定用户、不创建激活申请；
- 激活申请 24 小时过期、全局 1024 `PENDING` 上限、同 scope/subject 单一有效申请、CAS 状态机与不自动授予 Admin 只作为后续 W1b 契约，不在 W0 落表。

运行并确认 RED：

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
```

Expected RED: 两份 ADR 还没有统一 Web 产品修订段，抽取器明确报缺段；失败原因不是旧 RI5 文字不同。

- [ ] **Step 2: 在 ADR-007 追加窄修订**

在现有 RI5 Amendment 之后、`## 后果` 之前追加统一 amendment。明确它只取代 D5 中“任何 `LOCAL_ADMIN` 之外 principal 读配置”的绝对表述；其余 D5、D6–D8、B2/F/H/E1/E2 与现场 GO 保持原样。不要删除或改写 2026-09-14 的历史接受文本。

- [ ] **Step 3: 在 ADR-014 追加 R1/R2/R3 修订**

用三个子节逐条写 R1/R2/R3 的新口径与回滚/变更门。保留既有 RI5 段作为历史，显式使用“本修订取代 RI5 R2 的 loopback 先后硬门”等措辞，避免两套现行规则并存。

- [ ] **Step 4: GREEN、同根扫描与提交**

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
rg -n "LOCAL_ADMIN|VIEW_INTEGRATION_STATUS|loopback|web_oauth_login_contexts|web_oauth_states|return intent" docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md
git diff --check
git add tests/contract/test_doc_fact_binding.py docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md
git commit -m "docs(w0): amend Web authorization and activation boundaries"
```

Expected: 测试全绿；扫描结果同时看得到历史 RI5 与 2026-09-20 替代段，但新段只有一个现行口径。反例测试删除 `LOCAL_ADMIN` 限制、加入任意 URL return intent 或让测试 state 建 context 时必须转为 false。

---

### Task 3: 收口渠道身份/审计与三域配置 ADR

**Files:**
- Modify: `tests/contract/test_doc_fact_binding.py`
- Modify: `docs/adr/ADR-013-m7-channel-boundary.md`（D6 后、`## 后果` 前）
- Modify: `docs/adr/ADR-015-real-model-provider-boundary.md`（RI5 R4 后、`## 后果` 前）

**Interfaces:**
- Consumes: 规格 §6、§10.6–§10.7、§11–§15、§19.1
- Produces: 身份/管理/审计责任边界与未来三域配置/进程挂载矩阵

- [ ] **Step 1: 先写闭集与矩阵测试并确认 RED**

增加 `test_w0_adr_013_keeps_channel_permissions_closed_and_adds_admin_boundaries()`、
`test_w0_adr_015_freezes_the_future_three_domain_mount_matrix()` 与
`test_w0_channel_and_config_bindings_are_discriminating()` 三条测试。

ADR-013 的断言必须覆盖：

- 产品角色 `ADMIN / OPERATOR / USER` 与认证来源分离；
- `ChannelPermission` 仍精确三成员，不加入配置/审计权限；
- 未来独立 `AdminCapability` 含 `MANAGE_USERS`、`MANAGE_DUTY_BINDINGS`、`VIEW_ADMIN_AUDIT`、`VIEW_PRIVATE_TASK_CONTENT`、`VIEW_INTEGRATION_STATUS`、`MANAGE_INTEGRATIONS`、`RUN_CONNECTION_TESTS`；后两项只允许 `LOCAL_ADMIN`，状态查看仍受 Task 2 的窄 DTO 约束；
- `AdminAuditStore` 独立于 `task_audit_events`，append-only，不含 Secret、正文、真实结果行；授权改变/敏感查看审计不可写时 fail-closed；W1a 先提供持久化/写契约，W1b 才消费，W3 才加查询 UI；
- W0–W5 只交付 DBA、值班、激活通知职责；requester/approver 与结果 ACL 只属于 R1；
- `TaskStore`、`ChannelStore`、薄渠道与唯一 task-worker 执行权保持不变；Admin 不绕过结果 ACL。

ADR-015 的测试必须从 amendment 的 Markdown 表解析出精确三行与可见性，不只做散文包含：

```text
.config/ai/config.json        Web=读写, task-worker=只读
.config/feishu/config.json    Web=读写, feishu-listener/channel-worker=只读
.config/resources/config.json Web=读写, task-worker=只读
```

API/migrate/postgres 对三者均不可见；未列出的消费者均不可见。另断言：

- 这是 W4a **未来迁移目标**，当前 `.config/integrations.json` 在 W4a 前仍是当前实现；
- 旧文件只作为一次性迁移输入，不长期双读；新旧同时存在 fail-closed 为 `migration_required`；
- Gemini provider/model/API version/origin 继续固定，Web 不取得任务模型 port；
- Secret 不进数据库/环境变量/日志/trace/异常/DOM/审计/回执，Web 只短暂持有；
- 本 amendment 不授权真实 Gemini/飞书/数据库/Prometheus 网络调用。

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
```

Expected RED: 两份 ADR 还没有 Web 产品 amendment 和三域表。

- [ ] **Step 2: 修订 ADR-013**

追加身份目录、三角色、`AdminCapability`、当前职责、Admin audit 与未来 R1 变更门。不要把规格中的未来 DTO/表误写成已存在；不要修改 D1–D6 的薄渠道与进程隔离结论。

- [ ] **Step 3: 修订 ADR-015**

追加三域配置与进程挂载表，明确它取代 RI5 单文件的**未来目标**而不是当前运行事实；冻结迁移、权限和回滚边界。保留固定 Gemini 与 PR 3E 现场 GO，不建立 provider registry 或 Web 模型调用路径。

- [ ] **Step 4: GREEN、矩阵反例与提交**

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
git diff --check
git add tests/contract/test_doc_fact_binding.py docs/adr/ADR-013-m7-channel-boundary.md docs/adr/ADR-015-real-model-provider-boundary.md
git commit -m "docs(w0): freeze identity audit and config domain boundaries"
```

反例测试必须证明：给 API 增加任一配置可见性、把 resources 文件挂给 listener、删掉 `AdminAuditStore` fail-closed、把 requester/approver 放进 W0–W5，都会让对应 predicate 失败。

---

### Task 4: 同步稳定架构、路线与人类运行手册

**Files:**
- Modify: `tests/contract/test_doc_fact_binding.py`
- Modify: `ARCHITECTURE.md`（§5.8、§11、§14/§16）
- Modify: `DEVELOPMENT_PLAN.md`（头部版本、§3、§6/§7、§8/§11）
- Modify: `README.md`（当前状态、配置/首启 runbook、产品演进导航）

**Interfaces:**
- Consumes: 四份已修订 ADR、规格 §9–§17
- Produces: 当前架构与 W0–W5 路线的稳定真源；不改变当前可执行命令

- [ ] **Step 1: 先写当前/未来与阶段顺序测试**

增加 `test_w0_stable_docs_distinguish_current_runtime_from_future_targets()`、
`test_development_plan_orders_every_web_product_stage_and_keeps_independent_gates_outside()`、
`test_readme_does_not_present_w4a_or_w5_as_current_runbook()` 与
`test_w0_stable_doc_bindings_are_discriminating()` 四条测试。

机械断言：

- `ARCHITECTURE.md` 不再包含“RI5 修订已接受但尚未实现”的过期句；它把当前已实现单文件/loopback（证据等级 `tests`、未部署）和未来三域/W5 release 分段说明；
- `DEVELOPMENT_PLAN.md` 的 Web 产品序列精确且有序：`W0 < W1a < W1b < W2 < W3 < W4a < W4b < W5`；`W4c` 与 `R1` 只出现在独立阻塞门，不被插进必经序列；
- 计划明确 W0 仅文档，W1a 必须等 W0 合入后另写详细计划并获批；每阶段均需独立计划、TDD、复审；
- I3 仍是独立延期路线，不被 W0 取消，也不与 W1a 并行改同一真源；
- README 当前启动说明继续使用 `.config/integrations.json` 与现有 loopback 首启流程；未来三文件和受信 LAN 首改密前可达只出现在“已批准但尚未实现”的产品路线区，不成为当前命令；
- README 明确普通用户无首页/“我的结果”，数据库结果访问仍是 R1，Admin 不绕过 ACL。

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
```

Expected RED: Architecture 仍声称 RI5 未实现，Development Plan 尚无 W0–W5，README 尚未标出已批准产品路线。

- [ ] **Step 2: 修正 ARCHITECTURE 当前事实与目标架构**

把 §11 中 RI5 “尚未实现”改为当前已离线实现的单文件/loopback 事实，证据等级保持 `tests`，不得写部署或真机验收。随后单列 W0 接受的未来产品目标：持久用户目录、独立 Admin 审计、三域配置与 W5 release；引用修订后的 ADR，不复制字段级规格。保持任务执行链、ToolGateway、薄渠道和唯一 task-worker 不变。

- [ ] **Step 3: 把 DEVELOPMENT_PLAN 升级为包含 W0–W5 的路线真源**

版本升级为 `Approved V2.5`，在头部记录 2026-09-20 只批准 Web 产品文档/ADR 路线，不授权 W1a 或真实调用。更新路线图、里程碑表和第 7 节：

1. W0 文档与 ADR；
2. W1a 用户/角色/外部身份/本地凭据/Admin 能力/Admin 审计写内核；
3. W1b 激活内核；
4. W2 登录和多 shell；
5. W3 用户/职责/审计 UI；
6. W4a AI/飞书配置迁移；
7. W4b 数据库/Prometheus 参数登记，网络 0 次；
8. W5 release、边缘限流、视觉与分级运行证据。

单列 W4c 与 R1；写明 W5 不依赖二者。不要把易漂移 SHA/PR/进度写入总体计划。

- [ ] **Step 4: 更新 README，但保留当前真实运行命令**

顶部状态修正为 I2 已归档、Web 产品设计已合入、当前进入 W0 文档收口；I3 暂缓但未取消。新增“已批准的 Web 产品演进”短节并链接规格/本计划/ADR。现有 RI5 配置与首启命令保持当前代码可执行，不把 `.config/ai|feishu|resources/config.json` 或 LAN 首次改密前开放写成现在可照做的步骤；仅在未来目标段说明 W4a/W5 会替代现行方式。

- [ ] **Step 5: GREEN、交叉扫描与提交**

```bash
python -m pytest tests/contract/test_doc_fact_binding.py tests/security/test_docs_command_consistency.py tests/security/test_capabilities_doc.py -q
rg -n "W0|W1a|W1b|W4a|W4b|W4c|R1|integrations.json|config.json|loopback|受信局域网" ARCHITECTURE.md DEVELOPMENT_PLAN.md README.md
git diff --check
git add tests/contract/test_doc_fact_binding.py ARCHITECTURE.md DEVELOPMENT_PLAN.md README.md
git commit -m "docs(w0): align architecture roadmap and current runbook"
```

反例测试需在内存中交换 W1a/W1b 顺序、把 README 未来三文件替换进当前 runbook、或把 W4c 插入必经序列，确认门真实转红。

---

### Task 5: 用 handoff 收口当前事实、证据与下一步

**Files:**
- Modify: `tests/contract/test_doc_fact_binding.py`
- Modify: `AGENT_HANDOFF.md`（§0、§1 当前基线、§5 下一步、§6/§8 未覆盖与验证）

**Interfaces:**
- Consumes: PR #58 合入事实、W0 分支实际测试与审查事实
- Produces: 下一位实现者可直接判断 W0/W1a 门状态的单一交接真源

- [ ] **Step 1: 先写 handoff 证据等级测试并确认 RED**

增加 `test_w0_handoff_records_design_merge_and_keeps_implementation_claims_closed()`、
`test_w0_handoff_names_w1a_plan_as_the_only_post_merge_next_step()` 与
`test_w0_handoff_binding_is_discriminating()` 三条测试。

断言：

- PR #58 与 merge SHA 精确记录；
- 当前优先级不再声称“下一步是 I3”；I3 标为独立延期路线；
- W0 只具有文档/ADR 与测试证据，未产生产品源码、migration、运行配置、部署、真实网络或 UAT；
- W1a 源码未开始，只有 W0 合入后才允许另写 W1a 详细计划并送审；
- RI2/RI3 PR 3E/RI4/RI6/H/E1 门保持关闭；
- 当前 RI5 已离线实现，不再保留“未来 Admin 配置治理无源码”的过期声称；但真机浏览器、部署、canary、用户验收仍未覆盖。

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
```

Expected RED: 当前 handoff 仍把 I3 写为唯一下一步、没有 PR #58/W0 记录，并包含 RI5/Admin 的过期表述。

- [ ] **Step 2: 精简而非堆叠地更新 handoff**

按“当前有效口径”改写 §0/§1/§5；不要把旧 I2/W0 流程继续追加成长流水。把历史细节留给 Git/I2 归档，只保留：

- I2 已归档事实；
- PR #58 设计合入事实；
- W0 文档/ADR 候选的精确基线、分支、测试证据与待合入状态；
- W0 合入后唯一获准动作是 W1a **计划**，不是 W1a 实现；
- 独立真实调用门和未覆盖项。

如果 W0 PR 已创建，可记录 PR 编号、受审 head 与 CI run；不能预写未知 merge SHA，也不能把 PR 绿灯写成合入。W0 合入后由下一轮 handoff 以真实 GitHub 事实关闭候选状态，再开始 W1a 计划。

- [ ] **Step 3: GREEN 与过度声称反例**

```bash
python -m pytest tests/contract/test_doc_fact_binding.py -q
git diff --check
git add tests/contract/test_doc_fact_binding.py AGENT_HANDOFF.md
git commit -m "docs(w0): hand off Web product ADR closure"
```

在内存反例中分别加入“W1a 已实现”“真实飞书已可用”“已部署”“已用户验收”，确认过度声称门会失败；不能只检查一个禁用词。

---

### Task 6: 深档自审、全量验证与计划内送审

**Files:**
- Review only: Task 0 allowlist 中全部文件
- Modify only if verification exposes a W0 root-cause defect; new scope requires re-review

**Interfaces:**
- Consumes: 完整 W0 diff
- Produces: 精确 SHA、本地四门、文档契约反例与 PR/CI 证据

- [ ] **Step 1: 检查真实 diff 和禁止面**

```bash
git status --short --branch
git diff --name-status origin/main...HEAD
git diff --stat origin/main...HEAD
git diff --check origin/main...HEAD
git log --oneline --decorate origin/main..HEAD
```

Expected: 只有 Task 0 allowlist 的 10 个文件；`src/`、migration、依赖、锁文件、Compose、脚本、静态页面均为零 diff。

- [ ] **Step 2: 沿同事实扫描冲突与过度声称**

逐项核对：

- 四条被替代条款在 ADR amendment 内只有一个现行答案，历史原文有明确“被取代”关系；
- `ChannelPermission`、`AdminCapability`、产品角色、认证来源、当前职责和 R1 结果 ACL 没有互相代替；
- `.config/integrations.json` 是当前运行事实，三域文件是 W4a 未来目标；
- 当前 loopback runbook 仍可执行，受信 LAN 首改密前可达只属于 W5 future release；
- W1a 在 W1b 前，W4c/R1 不在 W0–W5 必经序列；
- W0 没有把计划/文档/测试写成源码、运行、部署或用户验收。

- [ ] **Step 3: 运行聚焦回归**

```bash
python -m pytest tests/contract/test_doc_fact_binding.py tests/contract/test_ri5_schema.py tests/security/test_ri5_compose_boundary.py tests/security/test_ri5_probe_boundary.py tests/security/test_docs_command_consistency.py tests/security/test_capabilities_doc.py -q
```

Expected: 全绿。RI5 schema/Compose/probe 测试仍断言**当前代码**，W0 文档不能通过修改这些运行边界测试来迁就未来目标。

- [ ] **Step 4: 运行 ADR-008 四门**

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

Expected: 四门全绿；记录真实数字和尾部输出。测试通过只计为 `tests`，不是运行、部署或真实渠道证据。

- [ ] **Step 5: 最终 SHA 自审**

```bash
git rev-parse HEAD
git show --stat --oneline HEAD
git diff --check origin/main...HEAD
```

按 Review Focus 逐条自审。若任何文档把未来目标写成当前事实、让飞书 Admin 读到原始配置、让 LAN 风险替代应用安全或让 W0 越到 W1a，修根因后重新跑 Step 3/4。

- [ ] **Step 6: 推送并开纯计划批准后的 W0 文档 PR**

```bash
git push -u origin claude/w0-web-product-adr
gh pr create --base main --head claude/w0-web-product-adr --title "docs(w0): align Web product architecture and governance" --body "W0 文档与 ADR 真源收口；不包含产品源码、部署或真实调用。验证结果、未覆盖项和残余风险见 AGENT_HANDOFF.md。"
```

PR 描述必须分成：已验证、只读推理、未覆盖、残余风险；明确这是文档/ADR 收口，不是 W1a 实现。等待精确 SHA 复审和负责人合入，不自行合并。

## W0 Exit Criteria

W0 只有同时满足以下条件才可判定完成：

- 规格状态、四份 ADR、Architecture、Development Plan、README 与 handoff 对同一边界无冲突；
- §19.2 四条变更有机械闭集测试，W0–W5 顺序和当前/未来运行方式有反例测试；
- diff 只含 allowlist 文档与文档契约测试；
- 聚焦回归和 ADR-008 四门全绿；
- W0 PR 经精确 SHA 复审并由负责人合入 `main`；
- handoff 仍明确：没有 W1a 源码、真实飞书/Gemini/StarRocks 调用、部署、canary 或用户验收。

W0 合入后允许的下一步只有：从最新 `main` 新建分支，重新入职，编写 **W1a 用户、权限与 Admin 审计写内核详细计划**并送审。W1a 计划未获批前不得写其测试、migration 或源码。
