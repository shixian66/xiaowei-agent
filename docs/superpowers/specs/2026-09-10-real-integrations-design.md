# 小维 Agent 2.0 真实接入总体设计

- 状态：Proposed，等待用户与 Claude/Codex 审核
- 规划基线：`630c859e6985fe048011057989d5ea4d5ebfc367`
- 证据等级：源码事实 + 只读设计；没有真实调用、部署、canary 或用户验收
- 日期：2026-09-10

## 1. 大白话结论

这次不重做小维，也不把模型变成“万能执行器”。我们只把已经搭好的安全主干，按顺序接上四类真实东西：飞书登录与机器人、模型 API、StarRocks 只读查询、管理员配置页面；最后再用 Docker Compose 部署和验收。

推荐路线是串行六阶段，每阶段一个独立 PR、一个开关、一个回滚点：

1. 飞书 OAuth 与 Web 启动；
2. 测试环境真实飞书联调；
3. 真实模型 API；
4. StarRocks 真实只读查询；
5. 最小 Web Admin 配置中心；
6. 正式部署、canary 与产品用户验收。

前一阶段没拿到对应证据，后一阶段不借它的名义宣布成功。测试环境联调只能叫 `test-env verified`，不能叫 canary。

## 2. 已确认前提

- 全部服务使用 Docker Compose；继续使用同一个应用镜像、多进程服务，不拆微服务。
- 容器中的 Web 服务监听普通 HTTP，宿主通过 `IP:8080` 访问；项目不新增 TLS、Ingress 或反向代理。
- 浏览器 OAuth 的公网回调继续使用用户已有的 HTTPS SSO 域名。SSO 域名到 Compose Web 端口的既有转发属于环境前提，不在本项目中重建。
- 复用旧小维的飞书应用；切换时先停旧小维，再启动 2.0，绝不同时消费同一个机器人事件流。
- 飞书 `open_id` 只证明“是谁”；租户、环境和权限仍由 2.0 的服务端身份目录决定。
- 普通用户只能查看或提交只读任务；只有明确标记的 admin 可以管理配置。群成员身份不能自动推导 admin。
- StarRocks 单条 SQL 最长 180 秒，整个 `ToolCall` 最长 200 秒，连接超时 5 秒，结果最多 200 行。
- 模型供应商和具体模型尚未指定。阶段 3 在项目负责人明确供应商、模型标识、API 形态、单请求上限和供应商项目硬预算前保持阻塞，不能由实现者擅自选择。

### 2.1 当前 2.0 源码事实

- `web_app.main()` 目前明确返回 `oauth_adapter_not_activated`；真实 OAuth provider adapter 尚不存在。
- 飞书长连接、消息发送/更新和群成员 adapter 已有 default-off 离线实现，但没有真实飞书环境证据。
- M6b 的 StarRocks target-bound readonly adapter 已合并离线代码和测试；真实目标、授权和现场验证仍缺失。
- 当前只有规则式 `IntentInterpreter`，没有真实模型 provider。
- 当前没有 Admin 配置中心；配置来自进程环境和只读文件引用。
- M6b/M7 的现有最强证据是离线测试，不是部署、canary 或用户验收。

### 2.2 旧小维只读参考

旧项目只用于确认用户已经跑通的行为：标准 OAuth code 换身份、已有 HTTPS SSO callback、应用本身可运行在 HTTP 后端。2.0 复用这条用户流程，不复制旧代码，也不依赖旧仓库。

2.0 明确不照搬旧实现中较弱的部分：明文 secret 环境变量、把 actor 放进签名 cookie、state 在过期前可重复使用、由浏览器脚本写非 HttpOnly session，以及把 provider 原始错误正文返回给上层。2.0 继续使用已经落地的一次性 state 摘要、服务端 session、当前身份目录和统一安全错误。

## 3. 目标与非目标

### 3.1 目标

- 真实飞书 OAuth 登录、长连接收消息、消息发送/更新、群成员查询。
- 真实模型只做结构化理解与解释；模型失败时可回退确定性规则。
- 接入第一个真实系统 StarRocks，只复用已有两个只读 operation。
- 提供最小 Admin 页面管理飞书、模型和 StarRocks 的非秘密配置与 secret reference。
- 所有真实接入均具备超时、限流、审计、证据、开关和回滚。
- 用精确部署 SHA 分开记录测试环境、部署、canary 和用户验收证据。

### 3.2 明确不做

- 暂缓整个 M8；不实现 Dinky 查询、状态、start、stop 或 restart。
- 不实现 Alertmanager silence、Prometheus 规则修改或其他 E1 写操作。
- `_E1_EXECUTION_ENABLED` 继续关闭；接入真实渠道、模型或只读系统不改变它。
- 不引入 LangGraph、Multi-Agent、通用 capability DSL、向量数据库、消息总线或微服务拆分。
- 不顺带接 Jenkins、Kafka、Kubernetes 或独立 MySQL。
- 不让用户、模型或 Web 页面提交任意 SQL、任意 endpoint 或任意执行顺序。

## 4. 三种路线

| 路线 | 做法 | 优点 | 代价 | 结论 |
| --- | --- | --- | --- | --- |
| A 串行小闭环 | 六阶段逐个接通，每阶段独立开关、PR 和验收 | 风险最低，出问题容易定位和回滚 | 总日历时间略长 | **推荐** |
| B 飞书与模型并行 | 两个团队并行做渠道和模型，再汇合 | 开发看似更快 | 配置、身份、故障语义未稳定时容易互相返工 | 不推荐当前采用 |
| C 先做 Admin | 先造通用配置中心，再接供应商 | UI 早出现 | 会为尚未稳定的配置契约造过度抽象 | 否决 |

## 5. 模块边界

```text
浏览器/飞书事件
      |
      v
interfaces/  协议解析、OAuth、身份上下文、RenderPayload 投影
      |
      v
application/ Runtime 与任务生命周期
      |
      +--> capabilities/ + planning/ + governance/  确定性解析、计划、Policy、SQLGuard
      |
      +--> runners/ -> ToolGateway -> tools/starrocks.py  唯一真实系统执行路径
      |
      +--> persistence/ + evidence/ + observability/  状态、证据、审计

模型 API --> 只产出受 schema 约束的 IntentDraft/Advisory --> 回到确定性链路
Admin   --> 只管理版本化配置和 secret reference --> 显式发布后 composition root readback
```

具体约束：

- `interfaces/` 只做渠道协议、OAuth、浏览器 session、身份传递和安全投影，不写 capability 路由或 SQL。
- 真实 OAuth adapter 实现现有 `FeishuOAuthPort`；只返回 `subject_ref`，不返回本地角色或执行权限。
- 模型 adapter 不进入 `ToolGateway`，也不能持有 Runner、Policy、SQLGuard 或基础设施客户端。它的输出必须重新经过 `CapabilityResolver`。
- StarRocks 仍只由 `ToolGateway` 调用现有 target-bound readonly adapter；真实连接配置不能出现在 `ToolCall`、模型输出或用户请求中。
- Admin 发布配置不等于激活真实调用。composition root 只有在对应 feature flag、配置版本和健康检查全部通过时才注册 provider。
- PostgreSQL 继续作为任务、session、证据和审计事实真源；secret 字节不进入 PostgreSQL。

## 6. 关键数据流

### 6.1 Web OAuth

1. 浏览器访问已有 HTTPS SSO 域名的登录入口。
2. 2.0 生成一次性 OAuth state，只在 PostgreSQL 保存摘要，并用临时 cookie 绑定当前浏览器。
3. 飞书回调后，adapter 用 code 换取 `open_id`；provider 原始响应不进入日志和页面。
4. 服务端按当前身份目录重新解析租户、环境、actor 和权限。
5. 成功后轮换随机 session；未知用户、重放 state、超时或身份已撤销均失败关闭。

Compose 内部仍是 HTTP；浏览器安全 origin 与 OAuth redirect 使用现有 HTTPS SSO 域名。计划不新增证书或代理服务。

### 6.2 飞书机器人

1. 长连接收到事件，先校验 app、tenant、sender、message 和事件唯一键。
2. 用事件 ID 去重；重复投递不会重复创建任务。
3. 入口只提交 `RequestEnvelope`，Runtime 决定 capability 和计划。
4. 渠道 worker 只消费 `RenderPayload`，发送初始卡片并在终态更新；失败进入有界重试或 dead letter。

### 6.3 模型

1. 只把最小必要文本和白名单上下文发给已选供应商；不发送 secret、连接串、完整身份目录、未经批准的历史证据或原始数据库结果。
2. provider 必须返回严格 schema；未知字段、超长内容和无效枚举直接拒绝。
3. 模型请求 15 秒超时，不自动改变执行计划；失败、限流或 schema 错误时回退现有规则解释器。
4. `IntentDraft` 仍由 Resolver、Planner、Policy、SQLGuard 全链复核。模型无法选择 endpoint、SQL、adapter、权限或审批结果。
5. 解释结果只基于已有安全投影；外部文本仍按 `ExternalContent` 处理。

### 6.4 StarRocks

1. 已认证用户提交只读诊断请求。
2. Resolver 和 Planner 只生成已有 `list_slow_queries` 或 `count_queries_in_window`。
3. `StepAdmission` 复核 policy、SQL AST、目标和调用超时。
4. Gateway 按 `(gateway, target_fingerprint)` 精确选择真实 adapter。
5. adapter 同连接做固定 preflight，再执行确定性 SQL；最多返回 200 行。
6. SQL 最长 180 秒，driver read timeout 190 秒，整个 Gateway 调用 200 秒，连接和写 socket 超时 5 秒。
7. 超时、权限/DDL/身份漂移、结果超量或连接不确定均结构化失败，不返回部分成功。

### 6.5 Admin 配置

1. admin 创建 draft，只能填写受 schema 限制的非秘密值和 secret reference。
2. “测试连接”使用 draft 做一次受限探测，既不发布也不保存 secret 内容。
3. admin 显式发布后生成不可变版本和审计记录。
4. composition root 读取 active version，校验 readback 后才激活对应 provider。
5. 回滚指向上一已发布版本；旧版本和审计保留，secret 始终由只读文件挂载提供。

## 7. 配置与 secret

### 7.1 阶段 1 到阶段 4

- 普通配置使用 `XIAOWEI_*` 环境变量，并继续 `extra=forbid`、半配置拒绝和默认关闭。
- secret 只通过容器只读文件引用，例如 Docker Compose secret；代码只接收绝对路径，不接收明文环境变量。
- 错误、日志、trace、readback 和测试快照只展示字段名、配置版本和脱敏状态，不展示 host、user、路径实值、token 或 provider 原始正文。

### 7.2 阶段 5 以后

- PostgreSQL 只保存非秘密配置、secret reference、版本、状态、操作者、时间和测试结果摘要。
- API 和 UI 对 secret 只有“已配置/未配置/引用已变化”三种可见状态，不提供明文写入框或回显接口。
- 发布和回滚使用 CAS，避免两个 admin 互相覆盖；每次操作产生 append-only 审计事件。
- 环境变量只保留 bootstrap 能力；发布配置的优先级和可覆盖字段由 ADR 固定，禁止同一字段有两个隐式真源。

## 8. 身份与权限

- OAuth `open_id` 是外部身份键，不是 actor、tenant、environment 或 role。
- 当前身份目录每次登录和每次受保护请求都重新解析；删除映射即撤权。
- 新增独立的 `manage_configuration` 权限，不复用 `admin_all_safe_tasks` 作为隐式 Admin 开关。
- admin 只能管理配置，不能因此获得 E1、任意 SQL、越租户查看或跳过 Policy 的权限。
- 飞书群只用于上下文和成员确认；群主、群管理员或“在群里”都不自动成为系统 admin。

## 9. 超时、重试和限流

| 边界 | 超时/限制 | 重试 | 失败语义 |
| --- | --- | --- | --- |
| 飞书 OAuth/API | 5 秒 | OAuth code 不重试；消息投递最多 3 次 | 安全错误，不回显 provider 正文 |
| 模型 API | 15 秒 | 默认不做隐式重试；回退规则解释器 | 标记 fallback，不伪装模型成功 |
| StarRocks connect | 5 秒 | 不自动重试 | 调用失败 |
| StarRocks SQL | 180 秒 | 不自动重试 | TIMEOUT，不返回部分结果 |
| StarRocks driver read | 190 秒 | 不自动重试 | UPSTREAM/TIMEOUT |
| StarRocks ToolCall | 200 秒 | Runner 按既有预算决定后续，不由 adapter 重试 | 任务失败或明确不确定 |
| StarRocks result | 200 行 | 不适用 | 超量整体失败 |
| 渠道消息 | 租户并发 2，最多 3 次 | 有界退避 | dead letter + 审计 |

阶段 4 必须同时修改 StarRocks profile、Settings、adapter 校验、计划编译的 `ToolCall.timeout_seconds`、ADR 和测试。只改某一个数字会导致配置启动失败或 Policy 拒绝。

## 10. 证据升级路径

| 等级 | 必须具备 | 不能宣称 |
| --- | --- | --- |
| source reviewed | 精确 SHA 的真实 diff 审查 | 测试通过、可运行 |
| tests | 四条基线命令和相关契约/eval 通过 | 真实服务可用 |
| test-env verified | 获批测试环境、精确 SHA、真实调用清单与脱敏 evidence | 已部署、canary、生产可用 |
| deployed | 指定环境运行指定镜像/SHA，健康检查与回滚材料齐全 | 产品行为已验收 |
| canary | 生产环境小范围真实用户/流量，明确窗口和观测结果 | 全量用户验收 |
| user accepted | 产品负责人按验收清单确认 | 未列出的能力也可用 |

每阶段证据必须记录：代码 SHA、镜像 digest、配置版本、启用开关、目标环境、执行时间、操作者、验证命令/场景、脱敏结果和回滚结果。PR 描述不能代替这些事实。

## 11. 阶段依赖与硬门

```text
阶段 1 OAuth/Web
   |
   v
阶段 2 飞书 test-env verified
   |
   v
阶段 3 模型 provider ---- 供应商/模型/成本上限明确门
   |
   v
阶段 4 StarRocks readonly ---- DBA 授权/精确目标/digest/窗口门
   |
   v
阶段 5 Admin ---- 前三类配置契约稳定门
   |
   v
阶段 6 部署/canary/UAT ---- 前序证据 + 回滚演练门
```

共同硬门：

- 每个阶段从当时最新 `main` 重新入职和取精确 SHA，不沿用本文 SHA 作为未来事实。
- 每个阶段默认关闭；缺配置、未知配置、权限不足或 readback 不一致均启动失败或调用失败。
- 真实调用前必须由项目负责人给出该阶段的明确 GO；计划批准不等于现场调用授权。
- 用户未提交文件、真实 secret、生产数据和旧小维代码均不得被修改或复制进本仓库。
- 任一阶段失败均可只关闭本阶段开关，不要求回退整个系统。

## 12. 回滚

- 飞书：停止 2.0 的三个渠道进程；确认不再消费事件后恢复旧小维。禁止双跑。
- 模型：关闭模型开关，立即回到规则解释器。
- StarRocks：移除 target-bound 注册并恢复 recording；capability 和 E1 闸门不变。
- Admin：CAS 切回上一发布版本并 readback；必要时关闭 Admin 写入口。
- 部署：Compose 使用上一镜像 digest 和上一配置版本重建；数据库迁移必须在各阶段计划中注明向前兼容或独立回退办法。

## 13. 分阶段计划索引

1. [飞书 OAuth 与 Web 启动](../plans/2026-09-10-feishu-oauth-web-activation.md)
2. [飞书测试环境真实验证](../plans/2026-09-10-feishu-test-environment-validation.md)
3. [真实模型供应商接入](../plans/2026-09-10-model-provider-adapter.md)
4. [StarRocks 只读真实接入](../plans/2026-09-10-starrocks-readonly-live-validation.md)
5. [最小 Web Admin 配置中心](../plans/2026-09-10-web-admin-config-center.md)
6. [Compose 正式部署、canary 与用户验收](../plans/2026-09-10-compose-deployment-canary-uat.md)

## 14. 当前未决项

- 阶段 3 的模型供应商、模型标识、API 协议、数据保留条款、单请求 token 上限和供应商项目硬预算尚未确定；这是实施前硬门。
- 阶段 4 的 StarRocks canonical resource ID、只读账号授权、TLS 材料、四类带外 digest、现场 actor 和激活窗口尚未提供；计划不读取这些值。
- 阶段 6 的正式主机、canary 用户名单、观察窗口和验收人尚未指定；部署前再由负责人明确。

这些未决项不会阻止评审总体设计，但会阻止对应真实调用或部署。
