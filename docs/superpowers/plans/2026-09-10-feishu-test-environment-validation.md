# RI2 Feishu Test Environment Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 RI1 已审查的精确 SHA，在 Docker Compose 测试环境完成真实飞书 OAuth、长连接、消息发送/更新、群成员与身份权限验证，并只升级为 `test-env verified`。

**Architecture:** 不新增业务能力。真实飞书仍通过现有 OAuth、listener、channel worker 和 Web 薄入口进入同一个 Runtime；现场工作只配置、启动、观察、记录证据和回滚。

**Tech Stack:** Docker Compose、现有 HTTPS SSO 域名、飞书企业自建应用、PostgreSQL、现有 Python 服务。

**Spec:** [真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)

## Global Constraints

本阶段的授权唯一真源是既有 [M7 计划 §0.3.2](../../plans/M7-web-feishu-channels.md#032-真实渠道激活部署与-canary-门)。
本计划只说明如何执行和收集证据，不复制或放宽该清单。任何 secret 只由操作者在测试主机创建并
以只读文件挂载，不粘贴进聊天、命令参数、Git、日志或 evidence。旧小维与 2.0 绝不同时运行。

---

## 非目标

- 不新增业务能力、不修改安全主链、不接模型或 StarRocks。
- 不部署正式环境，不做 canary，不做全量产品验收。
- 不在测试主机热改源码或把现场配置提交进 Git。

## ADR

沿用 `ADR-013-m7-channel-boundary.md` 与 RI1 的 ADR-014。本阶段不新建 ADR；现场若要求改变身份、endpoint、传输或安全边界，停止验证并另开 ADR/PR。

## PR 边界

- 本阶段原则上不改业务代码；只允许修订测试环境 runbook、验收清单和 `AGENT_HANDOFF.md` 的真实证据。
- 现场发现代码缺陷时立即停止验证，回到新的修复 PR；不得在测试主机热改源码。
- 现场只验证已批准边界，不借验证 PR 改设计。

## 进入条件

- [ ] [M7 §0.3.2](../../plans/M7-web-feishu-channels.md#032-真实渠道激活部署与-canary-门)
  的全部勾选项已经由对应负责人逐项完成；不得用本文件中的近似文字代替。
- [ ] RI1 PR 已合并，精确 SHA 的四条基线命令与 Compose smoke 已通过 Codex 审查。
- [ ] 项目负责人在上述条件完成后明确给出 RI2 现场 GO；该 GO 同时满足 M7 真实渠道验证门。

测试主机 Docker/网络、Git 外 secret/身份文件、已有 SSO 入口的实际请求限流、旧小维停机与恢复
命令属于执行前预检；预检失败时停止，但它们不构成第二份授权真源。

### Task 1：准备可审计 runbook

**Files:**

- Create: `docs/runbooks/feishu-test-environment-validation.md`
- Create: `docs/checklists/feishu-test-environment-evidence.md`
- Create: `tests/contract/test_feishu_live_runbook.py`
- Modify: `README.md`

- [ ] **Step 1: 写静态文档契约测试**

测试 runbook 必须直接引用 M7 §0.3.2，并断言未全部勾选时现场步骤不可执行；还必须包含精确 SHA、
Compose project name、开关、旧服务停机门、secret 不入命令、场景清单、证据等级、回滚和清理
边界，以及 SSO 入口限流与 OAuth state 有界清理的 readback；禁止在本文件复制一套授权字段清单，
也禁止把 `test-env verified` 写成 canary/production/UAT。

Run: `python -m pytest tests/contract/test_feishu_live_runbook.py -q`

Expected: runbook 尚不存在，测试失败。

- [ ] **Step 2: 写 runbook 与清单**

runbook 只给命令形状，不给真实 secret。所有变量由测试主机的受限 env 文件或 Compose secret 解析；执行前用 `docker compose config` 检查配置结构，并确保输出不会包含 secret 内容。

- [ ] **Step 3: 提交 runbook PR**

```bash
git add docs/runbooks/feishu-test-environment-validation.md docs/checklists/feishu-test-environment-evidence.md tests/contract/test_feishu_live_runbook.py README.md
git commit -m "docs(feishu): add test environment validation runbook"
```

### Task 2：切换前检查与启动

**Files:**

- Runtime evidence only: Docker/Compose 状态、应用日志、PostgreSQL 审计
- Later modify: `AGENT_HANDOFF.md`

- [ ] **Step 1: 固定待验证制品**

执行并保存脱敏输出：

```bash
git rev-parse HEAD
git status --short --branch
docker compose config --services
docker compose images
```

记录镜像 digest，而不是只记录可漂移 tag。

- [ ] **Step 2: 停止旧小维**

由操作者执行既有停机命令，随后证明旧进程不再消费该飞书 app 的长连接。若无法证明已停，停止本阶段，不启动 2.0 listener。

- [ ] **Step 3: 启动 2.0 渠道 profile**

按 runbook 启动迁移、Web、listener、channel worker 和必要的 runtime worker。启动后先检查健康与装配日志；出现配置错误、身份文件错误或 provider 鉴权错误立即关闭渠道开关。

```bash
docker compose --profile m7-channels up -d --build
docker compose ps
docker compose logs --since 10m web-app feishu-listener channel-worker worker
```

日志证据必须经现有脱敏规则检查后再保存。

### Task 3：逐项真实验收

**Files:**

- Modify after execution: `AGENT_HANDOFF.md`
- Fill after execution: `docs/checklists/feishu-test-environment-evidence.md`

- [ ] **Step 1: 按固定顺序运行真实场景矩阵**

按以下顺序进行，每项失败都先停止，不继续堆叠变量：

1. **Web OAuth**：授权 URL、一次 callback、一次 session；重放 callback 被拒绝；未映射用户被拒绝。
2. **身份权限**：viewer 只能看自己的安全任务；operator/dba 可提交只读任务；admin 才可进入未来的配置管理边界；群成员身份不自动提权。
3. **长连接**：单聊和已批准群消息各创建一个任务；重复 event ID 不重复创建。
4. **消息发送**：初始卡片成功发送，正文不包含原始 Evidence 或 secret。
5. **消息更新**：同一 destination 在任务终态更新，不重复发送多个终态。
6. **群成员查询**：已批准用户匹配；非成员、查询超时和权限不足均拒绝访问。
7. **失败路径**：临时关闭 2.0 的 provider 网络或使用无权限测试主体，证明错误明确、重试有界、无假成功。
8. **隔离检查**：确认旧小维仍停止、E1 闸门仍关闭、没有新增 StarRocks 或模型真实调用。

每项记录 request/task/event 的脱敏 ID、时间、结果、相关日志位置和 evidence ID；不复制完整消息、token 或用户隐私数据。

### Task 4：回滚演练与证据收口

- [ ] **Step 1: 演练回滚**

停止 2.0 渠道服务，确认不再消费事件，再按既有方式恢复旧小维。回滚过程中不能出现两个 listener 同时在线。

```bash
docker compose --profile m7-channels stop web-app feishu-listener channel-worker
docker compose ps
```

- [ ] **Step 2: 更新 handoff**

只在所有必测项通过后，把相应能力标为 `test-env verified`。分开记录未覆盖项和残余风险，并明确：未部署正式环境、未 canary、未产品用户验收。

- [ ] **Step 3: 提交证据文档**

```bash
git add AGENT_HANDOFF.md docs/checklists/feishu-test-environment-evidence.md
git commit -m "docs(feishu): record test environment evidence"
```

## 验证命令

离线制品门：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
docker compose config
git diff origin/main...HEAD --check
```

现场命令只在负责人给出 GO 后按 runbook 执行；其输出必须脱敏并绑定精确 SHA/镜像 digest。

## 退出标准

- OAuth、长连接、消息发送/更新、群成员和身份权限的成功/失败路径均有真实测试环境证据。
- 旧小维停止后才启动 2.0；回滚演练证明可以恢复旧小维且没有双消费。
- `AGENT_HANDOFF.md` 最高只写 `test-env verified`。
- 没有把 secret、provider 正文或真实用户数据提交进 Git。
- RI3 未获单独开工口令前停止。

## 回滚

关闭 2.0 渠道开关并停止 `web-app`、`feishu-listener`、`channel-worker`，确认断连后恢复旧小维。PostgreSQL 中 append-only 审计和 evidence 保留，不把逻辑停止描述为物理删除。
