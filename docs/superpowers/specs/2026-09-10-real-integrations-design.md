# 小维 Agent 2.0 真实接入总体设计

- 状态：Proposed V2（已按独立复审修订），等待用户与 Claude/Codex 审核
- 规划基线：`630c859e6985fe048011057989d5ea4d5ebfc367`
- 证据等级：源码事实 + 只读设计；没有真实调用、部署、canary 或用户验收
- 日期：2026-09-10

## 1. 大白话结论

这次不重做小维，也不把模型变成“万能执行器”。我们只把已经搭好的安全主干，按顺序接上四类真实东西：飞书登录与机器人、模型 API、StarRocks 只读查询、管理员配置页面；最后再用 Docker Compose 部署和验收。

推荐路线是串行六阶段，每阶段一个独立里程碑、一个或多个单一目的 PR、一个开关和一个回滚点：

1. RI1：飞书 OAuth 与 Web 启动；
2. RI2：测试环境真实飞书联调；
3. RI3：真实模型 API；
4. RI4：StarRocks 真实只读查询；
5. RI5：最小 Web Admin 配置中心；
6. RI6：正式部署、canary 与产品用户验收。

前一阶段没拿到对应证据，后一阶段不借它的名义宣布成功。测试环境联调只能叫 `test-env verified`，不能叫 canary。

## 2. 已确认前提

- 全部服务使用 Docker Compose；继续使用同一个应用镜像、多进程服务，不拆微服务。
- 容器中的 Web 服务监听普通 HTTP；基础 `docker-compose.yml` 继续只发布
  `127.0.0.1:8080`。只有 RI6 的生产 override 才可发布宿主 `IP:8080`；项目不新增 TLS、Ingress
  或反向代理。
- 浏览器 OAuth 的公网回调继续使用用户已有的 HTTPS SSO 域名。SSO 域名到 Compose Web 端口的既有转发属于环境前提，不在本项目中重建。
- 复用旧小维的飞书应用；切换时先停旧小维，再启动 2.0，绝不同时消费同一个机器人事件流。
- 飞书 `open_id` 只证明“是谁”；租户、环境和权限仍由 2.0 的服务端身份目录决定。
- 普通用户只能查看或提交只读任务；只有明确标记的 admin 可以管理配置。群成员身份不能自动推导 admin。
- StarRocks 单条 SQL 最长 180 秒，driver read 190 秒，正常 `ToolCall` 预算 195 秒，Policy
  硬上限 200 秒，连接/写 socket 5 秒，结果最多 200 行。
- 模型供应商和具体模型尚未指定。RI3 在项目负责人明确供应商、模型标识、API 形态、单请求上限和供应商项目硬预算前保持阻塞，不能由实现者擅自选择。

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
- 应用消费的 `ModelProviderPort` 定义在内层 `capabilities/`，外层模型 adapter 在 `interfaces/` 实现；
  `application/` 不反向 import `interfaces/`。模型 adapter 不进入 `ToolGateway`，也不能持有 Runner、
  Policy、SQLGuard 或基础设施客户端。它的输出必须重新经过 `CapabilityResolver`。
- StarRocks 仍只由 `ToolGateway` 调用现有 target-bound readonly adapter；真实连接配置不能出现在 `ToolCall`、模型输出或用户请求中。
- Admin 发布配置不等于激活真实调用。composition root 只有在对应 feature flag、配置版本和健康检查全部通过时才注册 provider。
- Admin application service 不能 import/调用 `ToolGateway`，不能构造 `PlanStep`、`ToolCall` 或
  `AdmissionCertificate`。它只持久化配置与测试请求；StarRocks 测试由默认关闭的独立测试 worker
  复用完整 `XiaoweiRuntime → DeterministicStepRunner → StepAdmission → ToolGateway` 主链。只有
  `interfaces/local_stack.py` 能 import tools 并装配该候选栈；worker 入口只消费窄 stack。
- PostgreSQL 继续作为任务、session、证据和审计事实真源；secret 字节不进入 PostgreSQL。

## 6. 关键数据流

### 6.1 Web OAuth

1. 浏览器访问已有 HTTPS SSO 域名的登录入口。
2. 2.0 生成一次性 OAuth state，只在 PostgreSQL 保存摘要，并用临时 cookie 绑定当前浏览器。
3. 飞书回调后，adapter 用 code 换取 `open_id`；provider 原始响应不进入日志和页面。
4. 服务端按当前身份目录重新解析租户、环境、actor 和权限。
5. 成功后轮换随机 session；未知用户、重放 state、超时或身份已撤销均失败关闭。

Compose 内部仍是 HTTP；浏览器安全 origin 与 OAuth redirect 使用现有 HTTPS SSO 域名。计划不新增证书或代理服务。
OAuth state 的过期清理、容量检查和插入必须由 PostgreSQL 同一事务内的固定锁串行化；普通
`count → insert` 事务不能抵抗并发穿透，应用进程锁也不能承载多进程正确性。

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
6. transport 的内层超时必须短于 15 秒总 deadline。若实现使用同步 SDK/标准库加
   `asyncio.to_thread`，外层取消只丢弃迟到结果，不能宣称线程或远端请求已停止；迟到结果不得
   进入 Intent、Advisory、Evidence 或成功成本统计。
7. 离线 provider 测试通过后仍需 RI3 独立现场 GO；只用固定脱敏样本验证结构化成功、越权拒绝、
   timeout/fallback、成本护栏和关闭回滚，最高标记 `test-env verified`。

### 6.4 StarRocks

1. 已认证用户提交只读诊断请求。
2. Resolver 和 Planner 只生成已有 `list_slow_queries` 或 `count_queries_in_window`。
3. `StepAdmission` 复核 policy、SQL AST、目标和调用超时。
4. Gateway 按 `(gateway, target_fingerprint)` 精确选择真实 adapter。
5. adapter 同连接做固定 preflight，再执行确定性 SQL；最多返回 200 行。
6. SQL 最长 180 秒，driver read timeout 190 秒，正常 Gateway 调用 195 秒，Policy 硬上限
   200 秒，连接和写 socket 超时 5 秒。
7. 超时、权限/DDL/身份漂移、结果超量或连接不确定均结构化失败，不返回部分成功。
8. Gateway 的 `asyncio.wait_for` 只能让调用方按时返回；它不能终止
   `asyncio.to_thread` 中已经开始的 PyMySQL 调用。超时后不得消费迟到结果，runbook 必须把
   “后台线程/服务端会话可能继续到 driver/server timeout”列为残余风险；本阶段不增加 `KILL`。

### 6.5 Admin 配置

1. admin 创建 draft，只能选择部署者预登记的 `target_ref` 与 `credential_ref` 等逻辑名；API
   不接受 host、port、任意 endpoint、绝对路径或相对路径。
2. “测试连接”只创建有频率限制、可审计且绑定 draft digest 的测试请求。飞书/模型使用各自窄
   provider port；StarRocks 请求由独立测试 worker 通过正常任务生命周期执行固定只读 count，
   Admin application/API 不直接探测网络。
3. admin 显式发布后生成不可变版本和审计记录。
4. composition root 读取 active version，校验 readback 后才激活对应 provider。
5. 回滚指向上一已发布版本；旧版本和审计保留，secret 始终由只读文件挂载提供。

## 7. 配置与 secret

### 7.1 RI1 到 RI4

- 普通配置使用 `XIAOWEI_*` 环境变量，并继续 `extra=forbid`、半配置拒绝和默认关闭。
- secret 只通过容器只读文件引用，例如 Docker Compose secret；只有受信 composition root 接收
  绝对挂载路径，不接收明文环境变量。
- 错误、日志、trace、readback 和测试快照只展示字段名、配置版本和脱敏状态，不展示 host、user、路径实值、token 或 provider 原始正文。

### 7.2 RI5 以后

- PostgreSQL 只保存非秘密配置、逻辑 `credential_ref`、版本、状态、操作者、时间和测试结果摘要。
- 逻辑凭证名和目标名都来自进程启动时加载的闭集 registry。路径解析、host/port/TLS/user 解析只在
  composition root 内发生；未知名、路径分隔符、点段或 registry revision 漂移全部 fail-closed。
- StarRocks 目标 registry 由部署者只读维护；Admin 只能选择其中已批准目标，不能新增或修改 endpoint。
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
| StarRocks ToolCall | 正常 195 秒；Policy 上限 200 秒 | Runner 按既有预算决定后续，不由 adapter 重试 | 任务失败或明确不确定 |
| StarRocks result | 200 行 | 不适用 | 超量整体失败 |
| 渠道消息 | 租户并发 2，最多 3 次 | 有界退避 | dead letter + 审计 |

RI4 必须同时修改 StarRocks profile、`POLICY_REVISION`、Settings、adapter 校验、execution
binding 的独立 `tool_timeout_seconds`、ADR 和测试。Runner 不得再从 profile 上限反推正常调用值；
否则“超过 Policy 上限”的拒绝分支对所有正常 binding 都失去区分力。

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
RI1 OAuth/Web（完成 M7 真实 OAuth 激活代码门）
   |
   v
RI2 飞书 test-env verified（满足 M7 真实渠道退出证据）
   |
   v
RI3 模型 provider ---- 供应商/模型/成本上限明确门
   |
   v
RI4 StarRocks readonly（完成 M6b 延期现场门）---- DBA 授权/精确目标/digest/窗口门
   |
   v
RI5 Admin ---- 前三类配置契约稳定门
   |
   v
RI6 部署/canary/UAT ---- 前序证据 + 回滚演练门
```

共同硬门：

- 每个阶段从当时最新 `main` 重新入职和取精确 SHA，不沿用本文 SHA 作为未来事实。
- 每个阶段默认关闭；缺配置、未知配置、权限不足或 readback 不一致均启动失败或调用失败。
- RI2 直接以既有 [M7 计划 §0.3.2](../../plans/M7-web-feishu-channels.md#032-真实渠道激活部署与-canary-门)
  为真实渠道授权真源；RI4 直接以既有 M6b 计划与 handoff 的延期现场条件为真源。分阶段计划只记录
  如何取得证据，不复制第二份授权清单。
- 真实调用前必须由项目负责人给出该阶段的明确 GO；计划批准不等于现场调用授权。
- RI6 部署制品不等于允许生产联网。每个要在正式环境启用的 provider/只读目标还必须按 ADR-007
  H 层逐项批准；未批准项保持关闭，生产写仍不在范围内。
- 用户未提交文件、真实 secret、生产数据和旧小维代码均不得被修改或复制进本仓库。
- 任一阶段失败均可只关闭本阶段开关，不要求回退整个系统。

## 12. 回滚

- 飞书：停止 2.0 的三个渠道进程；确认不再消费事件后恢复旧小维。禁止双跑。
- 模型：关闭模型开关，立即回到规则解释器。
- StarRocks：移除 target-bound 注册并恢复 recording；capability 和 E1 闸门不变。
- Admin：CAS 切回上一发布版本并 readback；必要时关闭 Admin 写入口。
- 部署：Compose 使用上一镜像 digest 和上一配置版本重建；数据库迁移必须在各阶段计划中注明向前兼容或独立回退办法。

## 13. 分阶段计划索引

1. RI1：[飞书 OAuth 与 Web 启动](../plans/2026-09-10-feishu-oauth-web-activation.md)
2. RI2：[飞书测试环境真实验证](../plans/2026-09-10-feishu-test-environment-validation.md)
3. RI3：[真实模型供应商接入](../plans/2026-09-10-model-provider-adapter.md)
4. RI4：[StarRocks 只读真实接入](../plans/2026-09-10-starrocks-readonly-live-validation.md)
5. RI5：[最小 Web Admin 配置中心](../plans/2026-09-10-web-admin-config-center.md)
6. RI6：[Compose 正式部署、canary 与用户验收](../plans/2026-09-10-compose-deployment-canary-uat.md)

## 14. 当前未决项

- RI3 的模型供应商、模型标识、API 协议、数据保留条款、单请求 token 上限和供应商项目硬预算尚未确定；这是实施前硬门。
- RI4 的 StarRocks canonical resource ID、只读账号授权、TLS 材料、四类带外 digest、现场 actor 和激活窗口尚未提供；计划不读取这些值。
- RI6 的正式主机、canary 用户名单、观察窗口和验收人尚未指定；部署前再由负责人明确。
- ADR-007 从“生产连接与生产写均默认禁止”改为“生产只读可在 RI6 按 H 层逐项批准、生产写继续
  禁止”属于实质授权面变化，尚未得到项目负责人单独签认。未签认时只能部署 provider 全关闭的
  制品，或继续使用已授权 test-env 目标且不得宣称生产 StarRocks 能力。

这些未决项不会阻止评审总体设计，但会阻止对应真实调用或部署。

## 15. 本轮复审问题的根因、影响与系统性修复

| 问题根因 | 实际影响 | 系统性修复与证明 |
| --- | --- | --- |
| Admin application 拟自行构造 plan/admission/Gateway | 产生第二条执行真源，并直接违反 application AST 硬门 | application 只持久化测试请求；默认关闭的接口层 worker 复用完整任务链；保留并扩充 `test_runtime_bypass.py` 反例 |
| draft 可提交 host/port/任意 secret path | 配置管理员可借测试连接做 SSRF、端口探测或读取挂载文件 | DTO 只接收闭集逻辑 `target_ref`/`credential_ref`；路径与 endpoint 只在 composition root 从只读 registry 解析；未知名/路径形状/registry 漂移零网络调用 |
| 六阶段只存在于临时计划 | 总体里程碑、ADR 授权和实现计划会各说各话 | 先做纯文档 V2.4，把路线映射为 RI1–RI6，并同步修订 ADR-007；未批准前不实施 |
| RI2/RI4 复制已有真实调用清单 | 两份硬门会随时间漂移，执行者可能选择较弱版本 | RI2 直接引用 M7 §0.3.2；RI4 直接引用 M6b §3.3、ADR-012 与 handoff 未完成项，分计划只记录执行证据 |
| 新环境变量未同步 `.env.example` | `test_env_example_clean.py` 必然失败，也会形成配置文档漂移 | RI1/RI3/RI4/RI5 都修改并精确暂存 `.env.example`，同时运行原样承重的动态键集合测试；没有新增断言时不得修改或暂存该安全测试 |
| 把 `IntentInterpreter` 改 async 只列局部调用方 | 其余 runtime/eval/security 测试会收到 coroutine，迁移不完整 | 以 `rg -l '\.interpret\(' src tests` 为一次性迁移清单，列出当前全部调用文件并跑完整回归 |
| 把应用消费的模型 Port 定义在 `interfaces/` | `application/` 被迫反向依赖外层，破坏现有单向分层 | `ModelProviderPort` 定义在 `capabilities/intent.py`，外层 adapter 实现；分层反例禁止 application import interfaces |
| 正常 ToolCall 直接取 Policy maximum | 所有合法 binding 都等于上限，超限拒绝分支无法证明正常值/上限独立 | execution binding 持有独立调用预算：StarRocks 195/Policy 200，其他只读 25/Policy 30；非法相等和超限均有反例 |
| Policy 语义变化但 revision 不变 | 旧 admission/approval 可能继续被视为有效 | RI4 强制递增 `POLICY_REVISION`，更新精确 fixture，并验证旧 certificate/approval 漂移拒绝 |
| 在 RI1 修改基础 Compose 为全网卡发布 | 提前扩大本地/离线攻击面，并混淆 HTTP 可达与 OAuth 安全 origin | base 保持 `127.0.0.1:8080`；RI6 override 才发布 `0.0.0.0:8080`；直接 IP/伪 Host 不能完成 OAuth/session |
| 新 Protocol 实现只靠运行时 duck typing | `mypy src` 看不到实现与 Protocol 漂移 | 每个新生产实现同步加入 `src/xiaowei_agent/_conformance.py` 类型锚和 Protocol 方法/关键字参数测试 |
| 把 `wait_for` 取消当成 PyMySQL/服务端已停止 | 超时后线程或服务端查询可能继续，占用连接与资源 | 迟到结果一律丢弃；现场记录线程/会话持续时间，依赖 driver/server timeout 收口；本轮不越权增加 `KILL` |
| 其他同步 provider 也可能使用 `to_thread` | OAuth/模型外层已超时后，线程或远端仍可能继续并产生迟到响应 | OAuth 与模型同样要求内层 deadline、取消/迟到反例和结果丢弃；不能把 coroutine 取消写成远端取消 |
| 新 configuration-test worker 自行 import tools | 破坏“只有 local_stack 是工具装配根”的静态等式，最容易诱发修改安全测试白名单的 workaround | worker 只消费 `local_stack.py` 返回的窄 `ConfigurationTestStack`；`test_only_local_stack_can_import_the_tools_layer` 期望集合保持原样并增加反例 |
| 候选配置测试悄悄增加第二个完整执行进程 | 与 M7“只有 task-worker 装配完整 Runtime”冲突，且没有解释为何不能复用 active worker | ADR-016 显式记录唯一例外：候选凭证/目标不能热切换 active Gateway、进入普通任务协议或影响用户任务；默认关闭、单请求 claim、无端口、完成即关闭候选连接 |
| 把生产连接禁令拆成 H 生产只读授权与 E2 生产写禁令 | 如果作为编号整理合并，可能在负责人未意识到时实质扩大生产网络权限 | ADR-007 增加单独签认框；未签认时 RI6 只能 provider 全关闭或沿用 test-env 目标，不能建立/宣称生产只读能力 |
