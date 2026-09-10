# RI4 StarRocks Readonly Live Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把已有 default-off、target-bound StarRocks readonly adapter 调整为可支持 1–2 分钟 SQL，并在负责人批准的唯一测试目标上取得真实只读证据。

**Architecture:** 不新增 capability 或 SQL 面。仍只允许 `list_slow_queries` 和 `count_queries_in_window`，经过 Resolver、Planner、StepAdmission、SQLGuard 和 target-bound Gateway。修改只聚焦分层超时、授权材料、Compose test profile 和现场验证。

**Tech Stack:** Python 3.11、sqlglot、PyMySQL、StarRocks、PostgreSQL、Docker Compose、pytest。

**Spec:** [真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)

## Global Constraints

SQL 180 秒、driver read 190 秒、正常 `ToolCall` 195 秒、Policy 硬上限 200 秒、connect/write
5 秒、最多 200 行。无自动 SQL 重试。不得加入任意 SQL、模型生成 SQL、导出、写操作或生产连接。

---

## 非目标

- 不新增 capability、operation、任意 SQL、流式结果、导出或写操作。
- 不连接生产 StarRocks，不放宽 E1，不顺带接其他数据库。
- 不把超时扩大到 Prometheus、资产查询或所有 Gateway。

## ADR

修订已接受的 `docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md`，把原 25/30 秒前提
替换为 StarRocks 专用 180/190/195/200 秒分层边界；决策提交先于实现。

## PR 边界

- PR 4A：修订 ADR-012、超时策略和离线测试；无真实调用。
- PR 4B：测试环境授权/runbook 与真实验证证据；原则上无业务代码。
- 不把 M6b 的历史 `tests` 证据改写成已现场验证。

## 进入条件

- RI3 的代码与本阶段无直接依赖；如果模型尚未获批，可关闭模型并继续 StarRocks，但必须由负责人明确调整阶段顺序。
- 从当时最新 `main` 创建 `claude/starrocks-readonly-live` 并重新记录 SHA/status。
- [ ] [M6b 计划 §3.3](../../plans/M6b-starrocks-test-readonly.md#33-真实连接必备授权清单)
  的全部真实连接项已经逐项获批。
- [ ] ADR-012 的 target-bound、preflight、证据处置和回滚决定仍与现场一致。
- [ ] 当时最新 [AGENT_HANDOFF.md](../../../AGENT_HANDOFF.md) §5 第 11 项及 §6 记录的 M6b
  未完成条件已全部闭合，离线候选精确 SHA 已通过审查。
- [ ] 项目负责人在以上条件完成后再次明确“现场 GO”。

以上三份材料是唯一授权真源；本计划不复制第二份近似清单。未全部勾选时只能做离线代码/文档工作。
- credential 仍只通过只读文件引用，CI 不持有真实配置或 secret。

### Task 1：修订 ADR-012 的分层超时

**Files:**

- Modify: `docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md`
- Modify: `docs/plans/M6b-starrocks-test-readonly.md`
- Create: `tests/contract/test_starrocks_timeout_contract.py`

- [ ] **Step 1: 写 RED 文档/常量契约**

测试要求 ADR、Settings、Policy、Runner call 和 adapter 同时满足：

```text
connect = 5
write = 5
server query = 180
driver read = 190
binding ToolCall = 195
policy maximum = 200
result rows <= 200
```

并断言 Prometheus 和资产查询的 30 秒上限不被一起放大。

Run: `python -m pytest tests/contract/test_starrocks_timeout_contract.py -q`

Expected: 当前 25/30 秒限制使测试失败。

- [ ] **Step 2: 修订 ADR 与旧计划事实**

说明这不是放宽 SQL 面，只是为已存在的合法慢查询提供真实超时预算；200 秒仍低于 `ToolCall`
契约的 300 秒硬上限。记录正常 call 195 秒与 policy maximum 200 秒分离的原因、无自动重试、无
部分成功和 heartbeat/lease 继续维持的原因；明确递增 `POLICY_REVISION`，让旧 admission/approval
失效。

- [ ] **Step 3: 提交决策变更**

```bash
git add docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md docs/plans/M6b-starrocks-test-readonly.md tests/contract/test_starrocks_timeout_contract.py
git commit -m "docs(starrocks): approve bounded long query timeout"
```

### Task 2：让配置、Policy、Runner 与 adapter 一致

**Files:**

- Modify: `src/xiaowei_agent/config.py`
- Modify: `.env.example`
- Modify: `src/xiaowei_agent/governance/profiles.py`
- Modify: `src/xiaowei_agent/runners/binding.py`
- Modify: `src/xiaowei_agent/runners/deterministic.py`
- Modify: `src/xiaowei_agent/application/default_capabilities.py`
- Modify: `src/xiaowei_agent/tools/starrocks.py`
- Modify: `tests/unit/test_config_happy.py`
- Modify: `tests/unit/test_capability_runtime_registry.py`
- Modify: `tests/unit/test_governance_profiles.py`
- Modify: `tests/unit/test_starrocks_adapter.py`
- Modify: `tests/unit/test_asset_plan.py`
- Modify: `tests/security/test_approval_binding.py`
- Modify: `tests/contract/test_step_admission.py`
- Modify: `tests/contract/test_deterministic_runner.py`
- Modify: `tests/contract/test_starrocks_adapter_runtime.py`
- Modify: `tests/security/test_starrocks_live_boundary.py`
- Modify: `tests/evals/test_m6b_starrocks_l0.py`
- Modify: `tests/evals/test_m6b_starrocks_l2.py`
- Modify: `tests/fakes/capability_bindings.py`
- Modify: `tests/contract/test_starrocks_timeout_contract.py`

- [ ] **Step 1: 写 RED 行为矩阵**

覆盖 180/190/195/200/5 的合法组合，以及 query 181、read 191、binding call 200/201、policy
201、connect 6、read 不大于 query、call 不大于 read、正常 call 不严格小于 policy maximum 等
非法组合。另断言 Prometheus/asset 的正常 binding 25 秒、profile 上限 30 秒，避免相同死分支。

- [ ] **Step 2: 分离“正常调用预算”与“策略上限”**

不要把全局 `MAX_READONLY_TIMEOUT_SECONDS` 改为 200。新增 StarRocks 专用 200 秒 Policy 上限，
只赋给 `SLOW_QUERY_READONLY_PROFILE`；Prometheus/asset 保持 30。给
`CapabilityExecutionBinding` 增加受审 `tool_timeout_seconds`：StarRocks 为 195，现有 Prometheus/
asset 为 25；构造时必须满足 `0 < tool_timeout_seconds < policy_profile.max_timeout_seconds`。

- [ ] **Step 3: Runner 只消费 binding 的调用预算**

Runner 在构造 `ToolCall` 前取得当前 capability execution binding，只使用其受审调用预算。禁止从
profile maximum、用户参数、模型 draft 或 adapter payload 读取正常 timeout。

```python
timeout_seconds = execution.tool_timeout_seconds
```

- [ ] **Step 4: 收紧 adapter 层级关系并更新 policy revision**

Settings 和 `StarRocksReadonlyAdapterConfig` 接受且只接受批准范围；仍由 adapter 固定执行
`SET query_timeout = 180` 并 readback。driver read 190 给服务端超时留出 10 秒，正常 Gateway
195 再留 5 秒给调用方收口；Policy 200 是拒绝漂移/伪造调用的硬上限，不是正常值。递增
`POLICY_REVISION`，更新所有精确 revision fixture/golden，并证明旧 certificate/approval 被拒绝。
`.env.example` 同步全部新增/变化键，继续通过精确键集合安全测试。

Run: `python -m pytest tests/contract/test_starrocks_timeout_contract.py tests/contract/test_step_admission.py tests/contract/test_deterministic_runner.py tests/contract/test_starrocks_adapter_runtime.py tests/unit/test_governance_profiles.py tests/security/test_env_example_clean.py tests/security/test_approval_binding.py tests/security/test_starrocks_live_boundary.py -q`

Expected: 全部通过。

- [ ] **Step 5: 做 TDD 反证并提交**

在独立临时副本分别让 Runner 重新取 profile maximum、让 StarRocks 正常 call 等于 200、漏增
`POLICY_REVISION`，确认 timeout/policy/approval 测试逐一变红；恢复后提交。

```bash
git add .env.example src/xiaowei_agent/config.py src/xiaowei_agent/governance/profiles.py src/xiaowei_agent/runners/binding.py src/xiaowei_agent/runners/deterministic.py src/xiaowei_agent/application/default_capabilities.py src/xiaowei_agent/tools/starrocks.py tests/unit/test_config_happy.py tests/unit/test_capability_runtime_registry.py tests/unit/test_governance_profiles.py tests/unit/test_starrocks_adapter.py tests/unit/test_asset_plan.py tests/security/test_approval_binding.py tests/contract/test_step_admission.py tests/contract/test_deterministic_runner.py tests/contract/test_starrocks_adapter_runtime.py tests/security/test_starrocks_live_boundary.py tests/evals/test_m6b_starrocks_l0.py tests/evals/test_m6b_starrocks_l2.py tests/fakes/capability_bindings.py tests/contract/test_starrocks_timeout_contract.py
git commit -m "feat(starrocks): support bounded 180 second queries"
```

### Task 3：准备唯一目标和 Compose test profile

**Files:**

- Modify: `docker-compose.m6b-test.yml`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Create: `docs/runbooks/starrocks-readonly-live-validation.md`
- Create: `docs/checklists/starrocks-readonly-evidence.md`
- Modify: `README.md`

- [ ] **Step 1: 写 RED Compose/runbook 契约**

断言 profile 默认不启用；完整目标、窗口、TLS、digest、actor、timeout 和 secret reference 缺一即失败；无明文 password 环境变量；CI/普通 Compose 不连接真实 StarRocks。

- [ ] **Step 2: 写 runbook**

固定准备、预检、两种 operation、长查询、超时、权限漂移、DDL/identity 漂移、200 行边界和销毁
精确 Compose project 的步骤。runbook 不含真实值，不用广泛 prune/glob。它必须明确：Gateway
`wait_for` 超时不会取消已在 `to_thread` 中运行的 PyMySQL 调用，后台线程/服务端会话可能继续到
driver/server timeout；调用方丢弃迟到结果并记录风险，本里程碑不以 `KILL` 修补。

Run: `python -m pytest tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py tests/contract/test_starrocks_timeout_contract.py -q`

Expected: 全部通过。

- [ ] **Step 3: 提交 PR 4A**

```bash
git add docker-compose.m6b-test.yml tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py docs/runbooks/starrocks-readonly-live-validation.md docs/checklists/starrocks-readonly-evidence.md README.md
git commit -m "docs(starrocks): add readonly live validation gate"
```

### Task 4：现场验证与证据升级

**Files:**

- Fill after execution: `docs/checklists/starrocks-readonly-evidence.md`
- Modify after execution: `AGENT_HANDOFF.md`

- [ ] **Step 1: 取得现场 GO**

只检查 M6b §3.3、ADR-012 和最新 handoff 的既有现场门是否已全部闭合；项目负责人再次确认精确
SHA、唯一目标、actor、时间窗口和证据处置。没有 GO 就停止，不试连，不在此创建替代清单。

- [ ] **Step 2: 启动隔离 Compose project**

只启动精确命名的测试 project 和临时 PostgreSQL volume；保存 `git rev-parse HEAD`、镜像 digest、配置 revision 与 profile 状态的脱敏 readback。

- [ ] **Step 3: 按顺序验证**

1. 握手和四类 preflight 全部匹配；
2. count operation 成功且只有一行；
3. list operation 成功且不超过计划 limit/200 行；
4. 一条 1–2 分钟的已批准测试查询在 180 秒内完成；
5. 一条超过 180 秒的受控测试触发 TIMEOUT；通常由服务端在约 180 秒返回，最迟必须在正常
   `ToolCall` 的 195 秒 deadline 内向调用方收口，绝不能等到 200 秒 Policy 上限或返回部分结果；
6. 错 target、错 actor、过期窗口、权限/DDL/identity 漂移均在主查询前失败；
7. 日志/evidence 不含 host、user、路径、SQL 原始注释、secret 或 provider 正文；
8. 若触发 Gateway 超时，记录调用方已结束、迟到 payload 未被消费，以及后台线程/服务端会话是否
   仍持续到 driver/server timeout；不能把 future 取消写成服务端查询已取消。

- [ ] **Step 4: 清理与提交 evidence**

销毁精确测试 project/volume，不能使用 Docker 广泛 prune。更新 handoff 时最高只写 `tests + test-env verified`。

```bash
git add docs/checklists/starrocks-readonly-evidence.md AGENT_HANDOFF.md
git commit -m "docs(starrocks): record readonly test evidence"
```

## 验证命令

```bash
python -m pytest tests/contract/test_starrocks_timeout_contract.py tests/contract/test_starrocks_adapter_runtime.py tests/evals/test_m6b_starrocks_l0.py tests/evals/test_m6b_starrocks_l2.py -q
python -m pytest -m security -q
python -m pytest -q
ruff check .
mypy src
docker compose -f docker-compose.yml -f docker-compose.m6b-test.yml config
git diff origin/main...HEAD --check
```

## 退出标准

- 离线测试证明 180/190/195/200 分层超时，其他能力正常值 25、Policy 上限 30 秒。
- 真实测试环境证明两个既有 operation、1–2 分钟查询、180 秒超时和所有漂移拒绝。
- 结果始终不超过 200 行，无任意 SQL、无自动重试、无 E1。
- 证据只标记 `test-env verified`，未冒充部署或 canary。
- RI5 未获开工口令前停止。

## 回滚

关闭 `test_readonly`，恢复 `recording`；移除真实 target-bound 注册并重建 task worker。超时 policy revision 变化会让旧 admission/approval 失效，这是预期 fail-closed 行为。
