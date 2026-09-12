# RI6 Compose Deployment, Canary, and UAT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Docker Compose 把已通过前序门的精确制品部署到正式主机，完成回滚演练、小范围 canary 和产品用户验收，并严格分开各级证据。

**Architecture:** 继续一个镜像、多进程 Compose：PostgreSQL、migrate、内部 API、task worker、Web、
飞书 listener、channel worker。若启用 Gemini，固定 provider/model/API 的 adapter 和
`GEMINI_API_KEY` secret 只存在于 task worker；其他服务不获得 key。基础 Compose 保持
`127.0.0.1:8080`；只有 production override 发布
`0.0.0.0:8080:8080`，使宿主 IP/端口网络可达。项目不部署 TLS/Ingress，OAuth/session 仍只允许
已有 HTTPS SSO Host/Origin。镜像 digest、配置版本和进程 readback 共同证明运行的是哪个版本。

**Tech Stack:** Docker Compose、OCI image、PostgreSQL、现有 HTTPS SSO 域名、现有日志/trace/evidence。

**Spec:** [真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)

## Global Constraints

本计划不授权部署。正式主机、变更窗口、canary 用户、验收人和回滚负责人都必须由项目负责人
明确。旧小维与 2.0 机器人不得同时运行；不新增 Kubernetes、Ingress、TLS 容器或云部署平台。
直接 HTTP IP 只提供网络到达能力，不能完成 OAuth 或使用受保护 session。
RI3 首版先按现有一个 Compose `worker` 容器验证。是否增加 worker 必须基于真实排队、provider 限流和
数据库领取证据另行决定；RI6 不在现场临时扩副本后把它写成已验证架构。

---

## 非目标

- 不引入 Kubernetes、Ingress、项目内 TLS、服务网格或新的部署平台。
- 不在部署阶段修改业务代码、补现场热修或放宽验收标准。
- 不验收 Dinky、E1、Alertmanager 写入、Jenkins、Kafka 或其他非目标。

## ADR

默认沿用前序 ADR 和现有 Compose 边界，不新建 ADR。若必须改变 HTTP/SSO、进程拓扑、migration 或回滚策略，部署立即停止，先单独评审新的 ADR。

## PR 边界

- PR 6A：Compose 制品固定、部署/回滚 runbook 和自动化静态契约。
- PR 6B：只在真实部署后提交脱敏的部署、canary、UAT evidence 与 handoff 更新。
- 若数据库 migration 不能前向兼容，必须回到对应功能 PR 修复；不得在部署 PR 中临时绕过。
- PR 6A 只做部署资产；PR 6B 只记录已经发生的真实证据。

## 进入条件

- 前序所有计划已由用户批准并按独立 PR 合并；四条项目基线命令在部署候选精确 SHA 上通过。
- 飞书和 StarRocks 至少具有 `test-env verified`；模型若启用也必须有独立 test-env 证据。该证据只
  允许进入 RI6，不自动授权生产网络调用；未验证或未逐项获批的 provider/目标必须保持关闭。
- Admin active config、允许版本化的 secret references 和目标进程 loaded readback 在测试环境一致；
  Gemini key 由宿主 `.env` 单独管理，只要求 task-worker 的 configured/loaded/test readback 一致，
  不要求也不允许 Admin 回显或回滚 key。
- 项目负责人指定正式主机、网络边界、Compose project name、数据备份责任人、部署窗口、canary 用户/群、观察窗口、UAT 验收人和旧小维恢复方式；并按 ADR-007 H 层逐项批准本次要启用的
  provider/只读目标、逻辑凭证、身份范围、数据处置和现场 GO。也允许先部署但保持全部 provider 关闭。
- 项目负责人已经单独勾选 ADR-007 的 H 层生产只读授权面变化；未勾选时不得建立新的生产只读
  连接，只能保持 provider 关闭或沿用已授权 test-env 目标且不宣称生产目标能力。
- 现有 HTTPS SSO 域名到正式主机 HTTP `IP:8080` 的转发已由环境 owner 验证；本项目不接管该设施。

### Task 1：固定可部署制品和 Compose 契约

**Files:**

- Inspect only: `docker-compose.yml`（不得修改其 Web loopback 发布）
- Inspect only: `docker-compose.model.yml`（RI3 已提供；Gemini 唯一启用 override）
- Create: `docker-compose.production.yml`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Create: `tests/contract/test_deployment_artifact_contract.py`
- Modify: `tests/security/test_web_auth_boundary.py`
- Modify: `README.md`

- [ ] **Step 1: 写 RED 制品契约**

生产 override 必须：

- 使用外部传入的不可变 image digest，不允许 `latest`；
- 不在正式主机 build 源码；
- base `docker-compose.yml` 的 Web 仍精确为 `127.0.0.1:8080:8080`；production override 才发布
  `0.0.0.0:8080:8080`，并通过 Compose 支持的显式列表替换语义覆盖 base `ports`，不得把两条发布
  规则合并保留；渲染结果必须只有一条 Web 宿主发布。内部 API 默认仍只绑定 loopback 或 Compose network；
- secret 只读挂载，容器 `read_only`、drop capabilities、no-new-privileges 保持；Gemini secret
  精确只挂载给 task worker，`XIAOWEI_GEMINI_ENABLED` 也只在 worker 自己的 environment 中出现；
  其他服务的渲染配置中不得出现该 secret 或开关；
- production override 本身不复制模型配置；只有本次 Gemini 生产 GO 已签认时，所有部署命令才额外
  叠加既有 `docker-compose.model.yml`。契约分别渲染“模型关闭”和“三文件模型开启”两种组合；
- provider feature flag 分别可关闭；
- migration 是独立一次性服务，其他服务等待其成功；
- PostgreSQL volume 名称固定在 Compose project scope，禁止广泛删除。
- 安全测试证明 `Host: <IP>:8080`、HTTP Origin 和伪造 forwarded host 不能完成 OAuth start/callback
  或访问受保护 session；只有配置的 HTTPS SSO Host/Origin 可通过。`/healthz` 是否允许直接 IP
  由独立路由闭集决定，不得借此放宽认证路由。

Run: `python -m pytest tests/contract/test_compose_contract.py tests/contract/test_deployment_artifact_contract.py -q`

Expected: production override 尚不存在，测试失败。

- [ ] **Step 2: 实现最小 production override**

只覆盖镜像引用、正式端口、资源/重启策略和只读 config/secret mounts；不修改 base Web 端口、不
复制整份 base Compose、不新增代理容器。端口列表使用部署主机 Compose 版本支持的显式替换标记
（例如已验证版本的 `!override`），部署前检查版本；不支持就停止，不退回到同时发布 loopback 与
全网卡两条端口。README 明确：可以用 IP 检查网络/健康，但浏览器登录入口必须使用已有 HTTPS
SSO 域名。

- [ ] **Step 3: 提交 Compose 制品**

```bash
git add docker-compose.production.yml tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py tests/contract/test_deployment_artifact_contract.py tests/security/test_web_auth_boundary.py README.md
git commit -m "chore(deploy): add immutable compose production profile"
```

### Task 2：部署、回滚、canary 和 UAT runbook

**Files:**

- Create: `docs/runbooks/compose-production-deployment.md`
- Create: `docs/checklists/deployment-evidence.md`
- Create: `docs/checklists/canary-evidence.md`
- Create: `docs/checklists/user-acceptance.md`
- Create: `tests/contract/test_production_runbooks.py`

- [ ] **Step 1: 写 RED runbook 契约**

检查四份文档包含：精确 SHA/image digest、配置版本/readback、数据库备份与 migration、旧小维停机、健康检查、回滚触发条件、canary 范围、观察指标、UAT 场景、证据等级和 owner。禁止把 `test-env verified`、`deployed SHA`、`canary`、`user-accepted` 合并成一个勾。

- [ ] **Step 2: 写部署 runbook**

顺序固定：只读检查 → 备份/恢复点 → 拉取不可变镜像 → `docker compose config` → 停旧小维 → migration → 启动 2.0 → 健康/readback → 烟测。runbook 必须把模型关闭的 base+production 与模型开启的 base+production+model 两套命令完整列开，不能靠操作者临时记得追加 override。任何一步失败都进入回滚，不边修边部署。

- [ ] **Step 3: 写 canary/UAT 清单**

- canary 只开放给负责人指定的小范围用户/群；
- 观察 OAuth 成功率、事件重复、任务失败、渠道 dead letter、模型 fallback/延迟/usage、StarRocks timeout/权限漂移、进程重启和配置 readback；
- UAT 由产品用户验证登录、提交、查看、飞书卡片、只读结果、失败提示、Admin 发布/readback/回滚；
- 不测试 Dinky、E1、Alertmanager 写入或其他非目标。

Run: `python -m pytest tests/contract/test_production_runbooks.py -q`

Expected: 全部通过。

- [ ] **Step 4: 提交 PR 6A**

```bash
git add docs/runbooks/compose-production-deployment.md docs/checklists/deployment-evidence.md docs/checklists/canary-evidence.md docs/checklists/user-acceptance.md tests/contract/test_production_runbooks.py
git commit -m "docs(deploy): add compose rollout and acceptance gates"
```

### Task 3：正式部署和回滚演练

**Files:**

- Fill after execution: `docs/checklists/deployment-evidence.md`
- Modify after execution: `AGENT_HANDOFF.md`

- [ ] **Step 1: 取得部署 GO**

项目负责人确认精确 SHA、image digest、active config version、secret reference 状态、部署窗口和
回滚负责人，并把“仅部署”和“允许启用的精确 provider/只读目标”分开签认。没有部署 GO 不连接
正式主机；没有某项 H 层 GO 就保持该项关闭。

- [ ] **Step 2: 只读预检**

在正式主机记录 Compose/Docker 版本、磁盘、端口、当前旧服务、数据库恢复点和 SSO 转发健康。命令输出脱敏后进入 evidence。

- [ ] **Step 3: 停旧小维并部署 2.0**

按 runbook 先证明旧 listener 已停，再启动 2.0。使用 production override 和不可变镜像 digest；migration 成功后才启动长期服务。下面第一组保持 Gemini 关闭：

```bash
docker compose -f docker-compose.yml -f docker-compose.production.yml config
docker compose -f docker-compose.yml -f docker-compose.production.yml pull
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d migrate
docker compose -f docker-compose.yml -f docker-compose.production.yml --profile m7-channels up -d
docker compose -f docker-compose.yml -f docker-compose.production.yml ps
```

只有本次 Gemini 生产网络调用已单独 GO 且 `.env`/readback 前置齐备时，必须从 `config` 到 `ps` 全程
显式使用 `--env-file .env` 和三文件组合，不能依赖工作目录隐式发现 `.env`，也不能只在 `up` 时
临时追加。禁止运行或留存会打印解析环境（可能包含 key）的 `config --environment`：

```bash
docker compose --env-file .env -f docker-compose.yml -f docker-compose.production.yml -f docker-compose.model.yml config
docker compose --env-file .env -f docker-compose.yml -f docker-compose.production.yml -f docker-compose.model.yml pull
docker compose --env-file .env -f docker-compose.yml -f docker-compose.production.yml -f docker-compose.model.yml up -d migrate
docker compose --env-file .env -f docker-compose.yml -f docker-compose.production.yml -f docker-compose.model.yml --profile m7-channels up -d
docker compose --env-file .env -f docker-compose.yml -f docker-compose.production.yml -f docker-compose.model.yml ps
```

- [ ] **Step 4: 健康与 readback**

核对所有进程健康、实际 image digest、policy revision、active config version 与 loaded readback。模型
启用时还要保存最终 Compose 文件集合包含 `docker-compose.model.yml` 的非秘密证据，并证明只有
task-worker 有 mount。同时证明只有一个 `worker` 容器，并且 readback 对应该启动代。此时最多标记
`deployed SHA`，还不是 `canary`/`user-accepted`。

- [ ] **Step 5: 回滚演练**

在 canary 前执行一次受控回滚：停止 2.0 渠道进程、切回上一配置/镜像、确认 readback，并在 2.0 完全
断连后恢复旧小维。必须把两种动作分开演练：

1. **回滚镜像/非秘密配置但继续启用 Gemini**：从 `config` 到 `ps/readback` 仍使用
   base+production+model 三文件组合，不能意外卸掉已获批能力；
2. **紧急关闭 Gemini**：只使用 base+production，并 `--force-recreate worker`，证明新容器已无
   Gemini mount、readback 为 disabled；不能只改环境变量或仍叠 model override。

数据库不做普通 downgrade；应用版本必须兼容已升级 schema。演练后按目标状态对应的完整文件组合
恢复候选版本。

### Task 4：canary

**Files:**

- Fill after execution: `docs/checklists/canary-evidence.md`
- Modify after execution: `AGENT_HANDOFF.md`

- [ ] **Step 1: 先固定 StarRocks canary 目标类型**

在 evidence 中二选一并绑定精确 target fingerprint：

1. **沿用 RI4 test-env 目标**：只验证正式部署的 Web/飞书/任务体验；StarRocks 证据仍是
   `test-env verified`，不得表述为生产 StarRocks 能力。
2. **接入新的生产只读目标**：必须重新完整进入 M6b §3.3 与 ADR-012 目标准入流程，不能只打开
   H 层开关。重新取得唯一 canonical target、只读授权/credential reference、version/grants/DDL/
   physical identity 四类带外 digest、physical identity preflight、首次连接 `SHOW CREATE TABLE`
   核对、actor/window、证据处置/保留和现场 GO；任一项缺失就停止。

- [ ] **Step 2: 开放最小范围**

只允许已指定 canary 用户/群；provider 按已经 test-env 验证的配置启用。其他用户由 2.0 身份目录拒绝或不开放；旧小维继续停止，不能让两个机器人 listener 同时在线。

- [ ] **Step 3: 运行验收场景并观察**

Run login, Feishu messaging, read-only tasks, Gemini advisory/fallback and Admin safe
readback. Run a 1-2 minute StarRocks case only when RI4's exact deployed SHA has already
delivered and verified its proposed 180/190/195/200-second layers; those values are not
facts of the current pre-RI4 source. Observe model 60/180-second stages and the exact
RI4 contract actually deployed, plus error rate, duplicates, dead letters,
queue/end-to-end latency, usage and process stability. Capacity shortfall stops
expansion; it does not authorize live timeout edits or an unreviewed worker scale-out.

- [ ] **Step 4: go/no-go**

只有所有硬指标满足且没有未解释的权限/目标/数据泄露事件，负责人才能把证据升级为 `canary`。否则关闭相应 provider 或整体回滚，不能用“基本可用”绕过。

### Task 5：产品用户验收和证据 PR

**Files:**

- Fill after execution: `docs/checklists/user-acceptance.md`
- Modify after execution: `AGENT_HANDOFF.md`

- [ ] **Step 1: UAT**

产品验收人逐项确认：

- Web OAuth 登录和退出；
- 飞书单聊/批准群提交任务；
- 初始卡片和终态更新；
- 查看本人任务与 admin 范围；
- 模型解释清楚但不能替代事实；
- StarRocks 只读结果与长查询等待体验；
- provider 失败、无权限、超时和回滚提示；
- Admin draft、测试、发布、restart/readback 和回滚。

- [ ] **Step 2: 更新 handoff**

按“已验证、只读推理、未覆盖、残余风险”写清证据。只有验收人明确签字/确认的场景才标记 `user-accepted`。

- [ ] **Step 3: 提交 PR 6B**

```bash
git add docs/checklists/deployment-evidence.md docs/checklists/canary-evidence.md docs/checklists/user-acceptance.md AGENT_HANDOFF.md
git commit -m "docs(release): record deployment canary and acceptance"
```

## 验证命令

```bash
python -m pytest tests/contract/test_compose_contract.py tests/contract/test_deployment_artifact_contract.py tests/contract/test_production_runbooks.py -q
python -m pytest -m security -q
python -m pytest -q
ruff check .
mypy src
docker compose -f docker-compose.yml -f docker-compose.production.yml config
git diff origin/main...HEAD --check
```

上面的命令验证模型关闭组合；Gemini 获生产 GO 时，还必须在现场 `.env` 已安全就绪后对同一条命令
显式追加 `--env-file .env -f docker-compose.model.yml` 并记录脱敏结果。继续启用模型的版本回滚也
使用该三文件组合；
只有紧急关闭模型才切回两文件，并强制重建 worker 后验证 mount/readback。PR 6A 的离线契约测试用
拆分构造的 fake key 渲染三文件组合，不读取真实 key。
Before any model-enabled render/start, verify Docker Compose is at least 2.24.4 (the
project support floor required by this plan's `!override` path) and use
Linux containers. The version floor and merged-config check are necessary but
insufficient: a split-fake environment secret must pass a functional mount preflight
without printing its value. The merged config must show worker with both
`postgres_password` and `gemini_api_key`, every other service with its prior secret list,
`XIAOWEI_GEMINI_ENABLED=true` only under worker (never the shared application anchor),
no `GEMINI_API_KEY` container environment, and no secret value in rendered output.
`docker stack deploy` is not an accepted substitute.

真实部署命令只在负责人现场 GO 后执行，不能由计划批准自动触发。

## 退出标准

- 正式主机运行不可变 image digest，配置 active/loaded readback 一致，健康检查通过。
- production override 提供 IP:8080 网络可达；直接 HTTP IP 不能完成 OAuth/session，真实浏览器认证
  只通过已批准 HTTPS SSO 域名。
- 已证明旧小维与 2.0 不双跑，并完成可复现的回滚演练。
- canary 有明确用户范围、观察窗口和无阻断问题的证据。
- UAT 只覆盖本轮目标并由指定产品用户明确确认。
- `AGENT_HANDOFF.md` 分开记录源码审查事实，以及 `declared`、`configured`、`tests`、
  `test-env verified`、`deployed SHA`、`canary`、`user-accepted` 中实际取得的最强状态。
- 不自行合并、归档或宣布全量上线。

## 回滚

停止 2.0 渠道服务，切回上一 active config 和上一不可变镜像 digest，受控重建并核对 readback；确认
2.0 listener 断连后才恢复旧小维。模型继续启用的版本回滚使用三文件组合；模型紧急关闭使用两文件
组合强制重建 worker，并确认 mount 消失/readback disabled。若只是一类 provider 故障，优先关闭该类
开关，保留其他已验证功能。
