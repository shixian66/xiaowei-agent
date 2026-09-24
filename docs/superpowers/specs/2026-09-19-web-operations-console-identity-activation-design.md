# 小维 Web 运维工作台、身份激活与未来结果访问边界总体设计

- 状态：Approved V0.4；V0.3 书面复核已通过，V0.4 按 2026-09-24 项目负责人决定收口 W3 V1 并进入 W4 总计划
- 规划基线：`origin/main@cc617f5e2d18c53b4a92528663edb1bd16aab557`
- 合入基线：PR #58 squash 合入 `origin/main@4e5a844620b700e25d6a29e43687c1a4c876db16`
- V0.3 批准来源：项目负责人 2026-09-20 决策记录
  <https://github.com/shixian66/xiaowei-agent/pull/59#issuecomment-5750387268>；
  该记录逐条批准第 19.2 节四项修订、R2 安全边界替换与 `I3 → W0/W1a` 优先级切换
- V0.4 决策来源：项目负责人于 2026-09-24 明确确认 W3 已按精简范围离线完成，并批准编写一份
  W4a/W4b 总实施计划；持久证据以本次计划 PR 的复审与合入记录为准
- 证据等级：当前源码事实 + 已确认产品决策 + 只读架构设计；没有本修订对应的源码、运行、部署、真实外部调用或用户验收证据
- 日期：2026-09-24

> 本文定义小维 Web 的产品目标、交付切片和安全边界，不表示目标功能已经实现，也不授权读取真实
> Secret、连接飞书/数据库/Prometheus/模型服务、部署或执行 canary。批准状态只覆盖文档口径：
> W3 V1 已按精简范围离线完成；当前只授权编写 W4a/W4b 总实施计划，该计划获批后才可按 TDD 写源码。

**V0.4 范围修订（2026-09-24）**：已经交付的 W3-lite 固化为 **W3 V1**。DBA/值班/激活通知人
职责绑定、可靠/私聊通知与敏感内容查看审计归入 **W3 后续增强**，延期但未取消，且**不作为
W4a/W4b 或 W5 的进入条件**。因此 W4b 只登记资源参数，不依赖或创建 `RoleAndDutyService`。

## 1. 大白话结论

小维 Web 的主要用户是 Admin 和运维人员，不建设面向所有人的通用门户：

1. **运维任务工作台**：给 Admin 和运维人员使用，用于和小维对话、发起已授权任务、查看任务状态与
   安全证据；普通对话继续走 I2 已有的确定性能力目录通道，不在前端另造一套 AI 路由。
2. **Admin 管理中心**：给 Admin 使用，用于用户激活、三角色、职责名单、审计，以及受控的 AI、飞书、
   数据库和 Prometheus 参数维护。
3. **单任务安全详情**：普通用户只通过具体链接进入当前已有的只读任务详情，不提供首页、任务台、
   “我的结果”、搜索或配置入口。
4. **数据库真实结果页是未来边界，不是本轮交付物**：当前仓库没有结果 artifact、申请人/审批人 ACL
   真源或导出域。本文保留“只允许该次申请人和审批人”的产品规则，但在前置域完成前不创建
   `/results/{result_ref}`、`ResultAccessService` 或假数据页面。

用户不再手工找飞书 `user_id/open_id`。第一次通过飞书登录时，系统自动取得飞书身份；若尚未绑定，
就创建待激活申请，由小维通知 Admin，Admin 明确批准后才建立内部账号、作用域角色和身份绑定。连接
测试与用户登录仍是两条独立流程：测试只验证应用凭据/OAuth 回调，不要求身份已登记，不签发 Session，
也不创建激活申请。

Admin 是最高产品角色，但**产品角色和认证来源不是一回事**：飞书登录的 Admin 可以管理用户、激活、
职责和审计；系统 Secret、资源配置变更及连接测试继续只接受本地管理员认证。数据库真实结果边界也
保持不变：链接、激活成功和 Admin 角色都不能代替某次查询的 requester/approver ACL。

部署目标是“已经完成且已获授权的功能可以正常使用”，不是永久藏在开发开关后面；这不放宽认证、
权限、`ToolGateway`、SQL/策略/审批链、Secret、进程挂载和内部端口等安全底线。

## 2. 已确认的产品决策

### 2.1 用户和入口

- 产品层只展示三个角色：`ADMIN`、`OPERATOR`（运维人员）、`USER`（普通用户）。
- Admin 是最高产品角色；系统初始化固定创建 `admin/admin`，第一次登录强制修改密码。
- 当前代码是“单一密码”本地登录；目标界面新增用户名字段，首版后端只接受固定用户名 `admin`。不能把
  目标用户名表单误写成当前已经存在的源码能力。
- 项目负责人明确接受受信局域网首次改密前默认口令可达的风险；不改成随机安装令牌，但正式公网发布前
  仍必须完成改密、HTTPS 和边缘防滥用。
- 这是对已接受 `ADR-014` §RI5 R2“首次强制改密必须在 loopback 阶段完成”的明示替代，
  不是对旧条款的解释。W0 必须把负责人批准、风险和补偿措施单独写入 ADR；W0 合入前，
  现行 Compose/运行手册的 loopback 要求仍是生效真源。
- Web 的主要用户是 Admin 和运维人员。
- 普通用户不提供工作台、任务列表、Admin 配置或“我的结果”；当前只允许经具体安全任务链接进入。
- 手机端只保证登录、激活状态和单任务只读详情可读；不建设手机版运维工作台、任务发起、任务列表或
  后台配置。未来数据库结果页若立项，才在其里程碑补手机结果详情。

### 2.2 身份激活

- 飞书 OAuth 自动取得飞书 `open_id`，不要求管理员或用户手工填写 `user_id/open_id`。
- 未登记身份必须进入待激活流程，不能自动成为 `USER`、`OPERATOR` 或 `ADMIN`。
- 小维负责通知，Admin 负责决定；模型和系统都不能自动批准。
- 群内触发时，W1b 只在当次事件里返回不含原始问题、结果信息、链接或 Admin 名单的通用卡片；不解析、
  不 @ 具体 Admin。按明确绑定 @/私聊收件人与可恢复投递属于 W3 后续增强，不由 W3 V1 或 W4 推定完成。
- 激活状态复用登录页壳，不建设独立 `/activation/pending` 前端应用。
- 激活完成后，群内用户重新提交原请求；系统不保存或自动执行激活前的原始任务。
- 未来数据库结果链接若成为首次入口，仍复用同一激活服务；在结果域落地前只保留这条规则，不注册结果路由。

### 2.3 权限和数据库结果

- DBA、值班人员不是第四、第五个全局角色，而是资源范围内的职责绑定。查询人/
  审批人同样不是全局角色，但它们只在未来 R1 结果域有消费者，因此不在 W0–W5 建表或开放管理入口。
- 既有 `ChannelPermission` 三成员闭集保持不变；Admin 管理能力使用独立 `AdminCapability`，不把配置、
  审计等权限硬塞进渠道权限。
- 群成员可查看该群任务的完整**安全证据摘要**和“还有更多证据”提示；数据库真实结果行不进入群卡片。
- 未来数据库结果预览与导出仍只允许该次查询申请人和审批人。
- Admin 查看任务与私聊内容的产品规则保留为 W3 后续增强；实现后每次敏感查看必须写独立 Admin 审计，
  且该能力不扩展到无关数据库结果行。W3 V1 不提供该入口。
- Admin 可以发起自己的任务。未来受控写能力若允许 Admin 自审，必须由明确的 `ApprovalGate` 策略实现；
  本文不开放 E1，也不授予任何真实写操作。

### 2.4 配置和部署

- Admin 页面维护人能理解的参数：环境、名称、地址、端口、账号、Secret、数据库名、TLS 等；
  `resource_id` 由系统生成，generation/digest/identity fingerprint 只放诊断区。
- Admin 配置中心允许维护数据库、Prometheus、AI、飞书和用户职责，但第一版只保存受控参数；数据库/
  Prometheus 不在 Web 进程发起真实连接测试。
- 配置中心不编辑 capability、Planner、SQL/PromQL 模板、Policy、审批算法或工具调用顺序。
- 第一版配置只有“保存 → 明确重启 → 加载回执”，不做热加载、版本中心或自动回滚。
- AI 首版沿用当前固定 Gemini provider、模型和官方 origin；页面只显示这些值，不开放模型/origin 编辑。
- 产品部署中，已实现且已获授权的服务应可配置并使用；缺配置时显示“待配置”，而不是让整个 Web 失败。
- “全部功能正常使用”不等于放开认证、权限、真实结果、Secret、网络和执行安全边界。

## 3. 当前基线事实与问题根因

以下是 `origin/main@cc617f5` 的源码/文档事实，不是对目标状态的声称：

1. 当前本地 Admin 登录是 password-only，页面不是成熟登录壳；源码已实现固定初始密码 `admin`、强制
   首次改密、Session 摘要存储、Origin/CSRF/CSP/HSTS 等安全边界。目标用户名字段是新产品能力。
2. 当前飞书 OAuth 连接测试已经不查询身份目录、不签 Session；登录与测试共用一个受信 callback URI，
   但使用不同 state digest domain。callback 先尝试登录域，仅在登录 state 不匹配时回落到测试域。
3. 当前正式飞书登录换得 `open_id` 后立即查询静态身份目录；未知身份返回通用认证失败。真正缺失的是
   持久化 `ActivationRequest`、Admin 审批和审批后身份绑定，不是手工取得 ID。
4. 当前静态身份目录使用 `operator/dba/oncall/viewer/approver/admin` 标签；面向用户的三角色、作用域角色
   和 Web 可管理身份目录尚不存在。
5. 当前存在两种认证来源：`LOCAL_ADMIN` 可读写配置并发起探针；飞书 `admin` 只拥有既有三项渠道权限。
   现行 ADR-007/014 明确禁止飞书 principal 访问配置，目标扩展必须先修 ADR，不能靠 UI 放开。
6. 当前 RI5 已实现 `.config/integrations.json`、Gemini/飞书配置、加载回执和受控探针；模型、API origin
   仍由 ADR-015 固定。数据库、Prometheus、用户职责和独立 Admin 审计尚未实现。
7. 单一 `integrations.json` 被需要 Provider 的多个进程只读挂载。把数据库/Prometheus Secret 继续塞入
   该文件，会让 listener/channel worker 看见不属于自己的凭据，因此必须按消费进程拆分文件与挂载。
8. 当前 `/app` 同时承载任务输入、列表、详情和集成配置；继续叠加用户、资源和审计会形成单页巨石。
9. 当前工作台在低于 1280px 时整体隐藏。目标仍不做手机运维台，但应支持 1024px 及以上常见笔记本/
   分屏；手机只保留登录、激活状态和只读深链。
10. I2 已完成限定领域普通对话：回答由当前 `CapabilitySnapshot` 确定性投影，不调用工具、不读取历史、
    不由用户文本改变能力目录。Web 必须复用这条 Runtime/`RenderPayload`，不能另造聊天后端。
11. 当前 `ReadClass.BOUNDED/RESTRICTED` 是执行前的静态读取范围分类，不是数据库结果 ACL。
12. 当前仓库没有数据库结果 artifact、requester/approver ACL 真源、结果分页或导出子系统。因此结果页只能
    保留未来边界，不能在本轮凭 TaskStore/Evidence 猜 ACL。
13. 当前没有应用层 HTTP rate limiter。OAuth state 有全局容量上限，但这不等于防滥用；正式发布必须由
    边缘/反向代理提供限流证据。
14. `task_audit_events` 是任务生命周期审计，不适合记录登录、用户管理、配置或敏感查看。当前没有
    `AdminAuditStore`。
15. I2 已在 `main` 完成收口，但 README/handoff 仍有“正在开发 I2-B/当前分支做 I2 收口”的旧口径；
    Gemini Secret 来源在五份真源间也存在漂移。W0 必须先按当前源码和精确 SHA 修正文档，
    不允许本规格静默选择其中一份。

## 4. 方案比较

### 方案 A：继续把所有内容塞进 `/app`

按角色用 CSS/JavaScript 隐藏模块，在现有 `index.html + app.js` 中加入激活、用户、资源和结果逻辑。

- 优点：短期文件少；
- 缺点：隐藏不是授权；登录、任务、配置和管理逻辑彼此耦合，路由/body limit/CSP 测试容易漏项。静态
  JavaScript 本来也不是 Secret，能否下载代码不作为安全判断；
- 结论：拒绝。

### 方案 B：同一模块化单体中的多壳层设计（推荐）

保留一个代码仓库和既有窄进程装配，把认证、运维工作台、Admin 管理中心和安全任务详情拆成独立 HTML/JS
壳层；未来结果域获批后再增加独立 result shell。后端通过共享 application 服务和类型化 DTO 协作。

- 优点：复用现有 Runtime、TaskStore、RenderPayload 和安全链；每个 shell 的职责与后端 API 边界一致；
  无需引入前端框架、第二套业务路由或新微服务；
- 缺点：需要明确路由、身份存储和迁移；
- 结论：采用。

### 方案 C：独立 SPA + 独立身份/配置服务

- 优点：长期可单独扩容和采用成熟前端工程体系；
- 缺点：当前规模下会提前引入构建链、API 网关、跨服务 Session、部署和观测复杂度，且容易复制权限逻辑；
- 结论：当前拒绝。出现多租户、多个独立 Web 团队或真实扩容证据后再评估。

## 5. 目标信息架构

### 5.1 页面壳层

当前 W0–W5 交付范围只包含下列页面：

| 页面 | 目标用户 | 主要能力 | 明确不包含 |
| --- | --- | --- | --- |
| `/login` | Admin、运维人员、待激活用户 | 本地 Admin 登录、飞书登录、首次改密、待激活状态 | 任务数据、Secret、角色选择 |
| `/app` | Admin、运维人员 | 小维对话、任务提交、任务列表、当前任务和安全证据 | 系统配置主表单、普通用户门户 |
| `/admin` | Admin | 概览、用户/激活、职责、资源参数、集成、审计 | 业务路由、任意 SQL/Policy 编辑器 |
| `/app/tasks/{task_id}` | 已授权主体 | 单任务只读状态和 `RenderPayload` | textarea、提交 JS、CSRF token、配置导航 |

未来数据库结果域满足第 11 节门槛后，才允许另立里程碑新增 `/results/{result_ref}`。当前不注册占位
路由，也不返回看似可用的空结果页。待激活状态是 `/login` 同一壳层的闭集视图，不新增
`/activation/pending` shell。

路由只是产品契约；实现计划可以在保持兼容的前提下调整前缀，但不能重新合并成一个条件渲染的万能壳。
每个新 JSON 写路由必须同时登记进请求体大小限制、Origin/CSRF、路由闭集和错误闭集测试，不能只添加
FastAPI handler。

### 5.2 前端代码边界

- `login`：只处理本地登录、飞书登录和首次改密；
- `workbench`：只处理任务提交、列表和任务轮询；
- `admin`：只处理用户/激活、配置和审计；
- `task-detail`：只读任务投影；
- 公共模块只放安全的 HTTP、文本渲染、日期和错误映射工具，不放角色或业务判断。

继续使用原生 HTML/CSS/ES module；没有真实复杂度证据前不引入 React/Vue、打包器或组件框架。所有外部文本
继续通过 `textContent` 渲染，禁止 `innerHTML`、内联脚本和 CSP 放宽。HTML shell 只加载自己的 JS，
用于降低耦合，不用于隐藏源码；静态 CSS/JS 可以公开读取，真正授权始终在后端 API。既有全局
`Cache-Control: no-store`、CSP、HSTS（HTTPS 模式）和安全响应头必须保留。

## 6. 统一身份模型

### 6.1 角色与认证来源分离

```text
UserAccount
- user_id                 内部稳定 ID
- actor                   既有任务 actor
- display_name            管理端展示名，不进入授权判断
- status                  ACTIVE | DISABLED
- created_at / updated_at

UserRoleAssignment
- user_id
- tenant_id / environment_id
- role                    ADMIN | OPERATOR | USER
- created_by / created_at / updated_at

ExternalIdentity
- user_id
- provider                FEISHU
- tenant_id / environment_id
- subject_ref             飞书 open_id；作为受控 PII，不进普通日志
- created_at / last_seen_at

LocalCredential
- user_id                 仅初始化 Admin 使用
- username                首版固定 admin
- password_hash
- must_change_password
```

角色属于明确的 tenant/environment 作用域，`LOCAL_ADMIN`、`FEISHU` 只是认证来源。初始化账号
`admin` 在当前固定开发作用域内是 `ADMIN`；它可以继续用本地密码登录，也可以由现有 Admin 通过独立受保护
流程绑定飞书身份。飞书身份不会因为首次 OAuth 成功而自动获得 Admin。

本地认证和飞书认证不互相升级：飞书登录的 `ADMIN` 可以管理用户、激活、职责和审计，也可以查看
独立的脱敏集成状态投影。该投影只含域名、`configured`、`restart_required`、加载闭集状态和最近
闭集测试结果；不含 Secret、host/endpoint、账号、资源列表、原始配置 DTO 或 Provider 正文。保存/清除
Secret、修改资源连接参数、运行任何连接测试的接口只接受 `IdentitySource.LOCAL_ADMIN`。需要这些
动作时，用户必须完成本地 Admin 登录；系统不签发“飞书会话临时变成本地会话”的混合凭证。

这是对 `ADR-014` §RI5 R1 和 `ADR-007` D5 配置读边界的窄例外，不是把飞书 Admin 变成配置读者。
W0 必须先同步两份 ADR；W0 合入前，现行“飞书 principal 不得读取配置状态”仍然生效。

### 6.2 现有渠道权限保持闭集

`ChannelPermission` 继续只有三个成员，角色每次请求确定性映射：

| 产品角色 | `VIEW_SAFE_TASK` | `SUBMIT_READONLY_TASK` | `ADMIN_ALL_SAFE_TASKS` |
| --- | :---: | :---: | :---: |
| `ADMIN` | 是 | 是 | 是 |
| `OPERATOR` | 是 | 是 | 否 |
| `USER` | 是 | 否 | 否 |

`USER` 拥有 `VIEW_SAFE_TASK` 不表示出现“我的任务”列表；它只让后端在具体深链请求上继续计算 owner、群成员
或其他既有 ACL。前端隐藏入口不替代后端判断。

### 6.3 Admin 管理能力单独建模

新增 `AdminCapability` 闭集，不修改 `ChannelPermission`：

```text
MANAGE_USERS
MANAGE_DUTY_BINDINGS
VIEW_ADMIN_AUDIT
VIEW_PRIVATE_TASK_CONTENT
VIEW_INTEGRATION_STATUS
MANAGE_INTEGRATIONS
RUN_CONNECTION_TESTS
```

只有作用域角色 `ADMIN` 可以取得这些能力；`OPERATOR` 和 `USER` 全部为否。另有两条认证来源约束：

- `MANAGE_INTEGRATIONS` 只有 `LOCAL_ADMIN` principal 可实际使用；
- `RUN_CONNECTION_TESTS` 只有 `LOCAL_ADMIN` principal 可实际使用，且每类真实网络调用还要满足自己的
  ADR-007 现场 GO。
- `VIEW_INTEGRATION_STATUS` 只返回第 6.1 节的脱敏状态投影，不得复用配置读 DTO 后在前端遮字段。

“Admin 是最高权限”指产品管理面能力最完整，不代表能绕过认证来源、数据库结果 ACL、SQLGuard、
ToolPolicy、ApprovalGate 或 `ToolGateway`。

### 6.4 三角色产品权限

| 能力 | ADMIN | OPERATOR | USER |
| --- | :---: | :---: | :---: |
| 运维任务工作台 | 是 | 是 | 否 |
| 发起已获授权的任务 | 是 | 是 | 否 |
| 查看本人/当前授权范围的安全任务 | 是 | 是 | 仅具体链接且符合 ACL |
| 查看全部安全任务元数据 | 是 | 否 | 否 |
| 用户与激活管理 | 是 | 否 | 否 |
| 查看脱敏系统/集成状态 | 是 | 否 | 否 |
| 修改系统与集成配置 | 仅本地 Admin 认证 | 否 | 否 |
| 运行连接测试 | 仅本地 Admin 认证 + 对应现场 GO | 否 | 否 |
| 审计私聊/任务内容 | 是，且留审计 | 否 | 否 |
| 未来数据库真实结果预览/导出 | 仅申请人或审批人 | 仅申请人或审批人 | 仅申请人或审批人 |

前端是否显示按钮只改善体验，后端每个请求都重新执行同一权限判断。`ADMIN` 不是数据库结果万能通行证。

### 6.5 职责名单不是全局角色

W3 V1 不交付资源职责绑定。下列能力属于 W3 后续增强，只有形成实际消费者与独立计划后才可实现：

- DBA/值班名单：用于通知、运营职责和已定义的 capability policy；
- 群管理员/激活通知人：用于通知，不自动授予查询或结果权限。

数据库查询人和审批人必须等 R1 同结果 artifact、requester/approver ACL 真源一起设计和交付；
W0–W5 不先存一张没有消费者的“授权名单”，也不得把 DBA/值班绑定误当作结果 ACL。上述 W3 后续
增强不作为 W4a/W4b 或 W5 的进入条件。

这些绑定只能引用已激活的 `UserAccount` 和受信资源 ID；不能携带任意 SQL、工具名或自定义策略表达式。

### 6.6 旧身份标签迁移

| 旧标签 | 新产品角色 | 额外迁移 |
| --- | --- | --- |
| `admin` | `ADMIN` | 只迁移产品角色；资源职责留待 W3 后续增强 |
| `operator` / `dba` / `oncall` | `OPERATOR` | 只迁移产品角色；旧职责标签列入迁移报告，当前不生效资源授权 |
| `viewer` / `approver` | `USER` | 只迁移产品角色；旧 approver 标签列入迁移报告，等 R1 有 ACL 真源后再决定映射，当前不生效授权 |

迁移必须可重复、可审计，并在同一 `tenant_id/environment_id` 内拒绝 actor、subject 或本地账号冲突。
静态 JSON 在迁移成功后只读保留一个发布周期，不能和数据库身份目录长期双写。

## 7. 首次激活流程

### 7.1 已登记用户正常登录

```text
用户点击飞书登录
→ 服务端签发一次性 OAuth state
→ 飞书 OAuth 回调取得 open_id
→ UserDirectory 解析 ExternalIdentity、UserAccount 和当前作用域角色
→ 账号 ACTIVE 且目标页面允许该角色
→ 轮换并签发 digest-only Web Session
→ 回到服务端保存的闭集 return intent
```

当前 `return intent` 只能是 `WORKBENCH`、`SAFE_TASK_DETAIL(task_id)`、`ADMIN_CENTER` 或
`ACTIVATION_STATUS(request_id)`，由服务端保存并和一次性 state 绑定；不接受任意 URL，避免 open
redirect。未来结果域若获批，才通过版本化契约新增 `DATABASE_RESULT(result_ref)`，不能提前预留任意 path。

落地介质固定为登录域专用的 state-scoped 表 `web_oauth_login_contexts`，而不向登录/连接测试
共用的 `web_oauth_states` 塞可空业务列。新表以 `state_digest` 作为唯一主键和指向
`web_oauth_states` 的外键，只保存闭集 intent kind 与有上界的 `task_id/request_id`；不保存 URL/path。
登录 state 与 context 必须同事务签发，回调必须在同一次 state 消费中读取 context；缺失、多行或不匹配
一律按无效 state fail-closed，不提供独立更新/复用 API。连接测试 state 不建 context 行，现有回落顺序不变。
该新表命中 `ADR-014` §RI5 R3 的 schema 变更门；W0 必须先显式放开这一个精确表，实现不得自行加列/加表。

### 7.2 未登记用户从 Web 或任务深链进入

```text
OAuth 回调取得 open_id
→ 查不到 ExternalIdentity
→ 幂等创建或复用 PENDING ActivationRequest
→ 不签发业务 Session
→ W1b 返回闭集 activation_pending；W2 才用 login shell 显示“激活申请已提交”
→ W3 Admin 管理中心才显示待办并按已绑定通知人发送安全通知
→ Admin 明确批准/拒绝
→ 用户重新飞书登录；批准者得到正常 Session
```

`ActivationRequest` 是独立事实，至少包含：

```text
request_id                      不可枚举随机 ID
tenant_id / environment_id
provider                        FEISHU
subject_ref                     OAuth 或已验证事件所得 open_id；受控 PII
source_kind                     W1b: WEB_LOGIN | FEISHU_GROUP；SAFE_TASK_LINK 随 W2 return intent 加入
source_conversation_ref_digest  群入口才有
source_event_ref_digest         群入口才有
return_intent                   第 7.1 节闭集；**由 W2 随 `web_oauth_login_contexts` 一起添加**
status                          PENDING | APPROVED | REJECTED | EXPIRED
requested_at / expires_at
decided_at / decided_by         终态时成对出现
approved_role                   APPROVED 时为 USER 或 OPERATOR
```

它不保存原始请求正文、数据库 SQL、结果行、Secret、任意 URL 或群消息原文。`subject_ref` 为完成身份匹配
所必需，普通日志和 Admin 列表只显示脱敏标识；事件和会话引用只保存 domain-separated digest。

相同作用域和 subject 只能有一个有效的 `PENDING` 申请；重复 OAuth 不制造待办风暴。状态闭集为
`PENDING / APPROVED / REJECTED / EXPIRED`，所有状态迁移使用事务和 CAS，审批重放不能改变既有结果。

`ActivationStore` 必须同时执行容量边界：

- `PENDING` 默认 24 小时过期；创建/复用前在同一串行化临界区把已过期项转成 `EXPIRED`；
- 全库最多 1024 个有效 `PENDING`，不是“每个请求自己记得检查”；
- 到达容量时 fail-closed，并且在同一临界区内先收割、再判容量、容量未满才按 subject 复用/创建；
  满载时不得先查 subject，已有与不存在的 subject 都返回统一“暂时无法提交激活申请”；
- 容量只约束**有效待办积压**，不冒充 HTTP 防滥用，也不冒充终态数据保留策略。`APPROVED / REJECTED /
  EXPIRED` 行（含受控 PII）的保留期与清理机制必须在 W5 部署前明确并验证；未完成时不得部署启用。
  W5 计划（Approved V0.2）已固定为 **30 天**、全库所有作用域：批准/拒绝按 `decided_at`、过期按
  `expires_at`，`terminal_at <= now - 30 天` 删除；在全局 activation advisory lock 下先转过期再删除，
  仍有效的 `PENDING` 永不删除，只输出四个计数。正式环境每日调度与失败告警是晋升 canary 的前置。
  正式发布的边缘限流要求见第 13 节。

Admin 审批时默认角色为 `USER`；提升为 `OPERATOR` 必须显式选择并可审计。任何 Web/群激活路径都不能
授予 `ADMIN`；新增 Admin 只允许当前本地 Admin 认证在管理中心执行独立高风险操作，并写 Admin 审计。
若批准时复用已有账号，当前作用域无角色才新增所选角色；已有角色必须与所选角色精确一致，不能借激活
静默升降级。已有 `ADMIN` 账号的飞书身份绑定同样属于独立高风险操作，不走普通激活审批。

### 7.3 未登记用户在群里 @ 小维

```text
飞书 listener 验证事件与 sender open_id
→ 身份目录无记录
→ 创建/复用 ActivationRequest
→ 返回通用激活卡片，不解析或 @ Admin
→ W1b 卡片只写“激活申请已提交、等待管理员处理”，不含链接/按钮
→ 申请人后续 OAuth 所得 open_id 必须与申请中 subject_ref 一致
→ Admin 审批后，申请人重新提交原任务
```

群卡片不得包含原始提问、SQL、数据库名、结果是否存在、Admin 私人联系方式或尚未由 W2 交付的状态
链接。W1b 不查询群内 Admin，因此有没有 Admin 都不影响申请，只提示“已提交管理员处理”。Admin 中心
待办已由 W3 V1 交付；明确 @/私聊激活通知人属于 W3 后续增强。群事件创建申请时，
必须把经过 SDK 验证的 sender `open_id` 作为受控 `subject_ref`，并保存 event/conversation digest；不能只靠
卡片 URL 里的 request ID 认人。

### 7.4 审批后的会话和撤权

- 审批只建立账号/外部身份映射，不代替目标资源权限或结果 ACL。
- 禁用账号或撤销身份后，后续受保护请求立即失败；已有 Session 失效或在下一次请求中被拒绝。
- 被拒绝/过期后可以由用户重新申请；旧 request ID 不复活。
- 审批失败不得留下“账号已建、身份未绑”或“身份已绑、角色未建”的半完成状态。
- 为避免激活前请求在事后意外执行，群任务不排队、不自动重放；安全任务深链由用户主动重新打开。
- 用户状态、作用域角色或外部身份发生改变时，旧 Session 在下一次请求重新解析后 fail-closed；不能把登录时
  的角色快照当长期授权。
- “从未绑定”与“已绑定但账号停用/当前作用域角色已撤销”必须是两个内部闭集结果：只有前者创建激活申请；
  后者继续按普通认证/提交失败处理，零申请且不向用户暴露账号状态。不能把撤权后的 `None` 当成新用户重新激活。

## 8. 飞书连接测试与用户登录彻底分开

三条流程使用不同发起入口和结果语义；需要 OAuth 的两条流程共用**同一个已备案 callback URI**，但 state
digest domain 互不相认：

| 流程 | 发起者 | 是否要求身份已登记 | 是否签发 Session | 结果 |
| --- | --- | :---: | :---: | --- |
| 飞书凭据测试 | Admin | 否 | 否 | App ID/Secret 是否可用 |
| 飞书 OAuth 回调测试 | Admin | 否 | 否 | 授权跳转与 code exchange 是否成功 |
| 用户登录/激活 | 任意访问者 | 登录要求是；未知则申请激活 | 已登记且 ACTIVE 才是 | 登录或待激活 |

固定流程如下：

- 登录从 `/oauth/feishu/start` 发起，使用 `oauth-state:v1`；
- OAuth 测试从 Admin 配置接口发起，使用 `oauth-conn-test:v1`；
- 两者 redirect URI 都是 `{public_origin}/oauth/feishu/callback`，不能为了代码方便测试另一个未备案 URI；
- callback 保持当前顺序：先消费登录域；只有登录 state 不匹配时才尝试测试域。登录域 state 已消费且身份
  未登记时，创建激活申请，绝不回落成测试；
- 测试域命中后，先再次确认当前 `LOCAL_ADMIN` Session，再交换 code；测试成功也不解析身份目录、不轮换
  Session、不绑定用户、不创建激活申请；
- 激活不是第三种 OAuth domain，而是正常登录完成 state 消费和 code exchange 后的 `identity_missing`
  结果。

配置页文案必须写“测试 OAuth 回调（不会登录或绑定用户）”，不能再出现“请先配置 user ID 才能测试”的
产品暗示。当前连接测试的安全语义保持不变；本轮只补 UI 说明和身份激活链，不重写正确的测试路径。

## 9. 页面与交互设计

### 9.1 登录页

登录页使用独立视觉壳，不再复用工作台的零散表单：

- 左侧（或宽屏背景区）：小维品牌、产品说明、“策略治理的运维任务工作台”；
- 右侧登录卡：`用户名`、`密码`、主按钮“登录”，以及可用时的“使用飞书登录”；
- 首版用户名固定为 `admin`，但仍使用正常标签/自动填充语义，不把密码框孤零零放在页面上；
- 首次密码为默认值时，登录后只进入强制改密页；改密完成前不能访问工作台和管理中心；
- 飞书身份未知时仍使用同一个登录壳显示“激活申请已提交、等待管理员处理”，不显示 Admin 名单、用户角色、
  原目标详情或结果是否存在；W1b 在成熟登录壳交付前只返回闭集 `activation_pending` 语义；
- 已登记身份通过认证、但当前角色不能进入请求页面时，不签 Session、不自动回落其他页面；登录壳显示
  “当前账号不能进入此页面”。普通用户直接登录时进一步提示“请从小维发送的具体结果链接进入”，不能
  归类成密码错误、身份待激活或安全任务不存在；
- 本地账号不存在、密码错误等未认证失败继续使用同一个闭集错误；只有在 OAuth state/code 已验证、用户已
  证明自己持有该飞书主体后，才可返回“身份待激活”，它不能作为枚举其他用户的接口；
- 连接状态只显示“系统可用/暂不可用”，不暴露数据库、主机、版本或异常正文；
- 窄屏仍可完成登录和改密。

### 9.2 运维任务工作台

- 入口首页以“小维对话/任务输入”为第一视觉焦点；任务列表和当前任务为辅助区；
- 系统配置从工作台移出，Admin 通过顶部“管理中心”进入；
- `ADMIN` 显示角色标识和管理入口，`OPERATOR` 不看到配置入口；
- 普通对话、澄清和 capability 请求都提交到既有 Web 任务入口，由统一 Runtime 决定 I2 回答或执行路径；
  前端不按关键词决定“聊天还是任务”，也不自己拼能力目录；
- 支持 1024px 及以上常见桌面/笔记本宽度，1280/1440 做主要视觉验收；
- 低于桌面下界时给出静态可读提示，不加载手机版任务提交布局；
- 任务结果继续复用 `TaskViewRuntime + RenderPayload`，入口不复制业务路由或证据语义。

### 9.3 Admin 管理中心

左侧一级导航固定为：

1. **概览与系统状态**：服务可用性、待激活数、已保存/待重启配置、加载回执和脱敏诊断；
2. **用户与权限**：W3 V1 提供用户、角色、状态、飞书绑定和待激活申请；DBA/值班范围与激活通知人
   属于 W3 后续增强，查询人/审批人等 R1 有真实消费者后再进入管理中心；
3. **资源配置**：数据库与 Prometheus；
4. **AI 与飞书**：AI API、飞书应用、OAuth 回调、连接测试；
5. **审计**：配置变化、激活审批、权限变化和敏感内容查看记录。

飞书认证的 Admin 可以进入管理中心处理用户、职责、审计并查看脱敏系统状态；资源/Secret 的保存、清除、
连接测试区域显示“需使用本地管理员账号继续”，后端也必须拒绝非 `LOCAL_ADMIN` 请求。

“清除凭据”“禁用用户”“新增 Admin”等破坏性动作与普通保存分开，要求明确二次确认，不与“测试连接”
并排做成容易误点的同级主按钮。

### 9.4 未来数据库结果页（不在当前 W0–W5 交付）

本节只固定未来 UX，不授权创建路由或数据模型：

- 未认证时只显示“使用飞书登录查看此结果”；
- 未激活时复用第 7 节激活服务，不展示结果元数据；
- 已激活但无 ACL 时返回统一不可用页，不区分结果不存在、过期或无权限；
- 授权后只展示本次结果概要、分页预览、审批与有效期信息，以及符合结果域规则的导出入口；
- 不提供“我的结果”、搜索、推荐、任务列表、AI 输入框、侧边栏或 Admin 导航；
- 页面以手机链接打开为主要场景，支持窄屏、键盘与屏幕阅读器；
- 浏览器不得缓存真实结果，退出/撤权后清除已渲染数据。

只有第 11 节前置条件全部满足并另行批准时，这些要求才进入实现计划。

### 9.5 视觉与可用性基线

- 视觉方向采用“冷静、可信、面向运维”的浅色界面：深墨绿作为主色，暖灰作为页面底色，状态色只用于
  成功、等待、警告和失败，不用大面积高饱和渐变；
- 统一 8px 间距体系、12–16px 圆角、清晰的标题层级和 44px 最小点击区；
- 表单使用“标签 + 示例/说明 + 输入 + 就地校验”，不把内部字段名直接暴露给管理员；
- 重要状态同时使用文字、图标和颜色，不能只靠颜色区分；
- 表格在窄桌面改为横向滚动或摘要卡，不通过缩小字号塞满；
- 空状态必须告诉用户下一步，例如“尚无待激活用户”“保存配置后测试连接”，不只显示空白；
- 危险操作使用独立危险区；主按钮每个页面原则上只有一个，避免“保存/测试/清除/发布”同级竞争；
- 当前静态资源可公开获取；验收检查的是每个 HTML shell 只加载所需模块，以及 API 权限不依赖前端隐藏。

## 10. Admin 参数维护模型

### 10.1 总体规则

- 脱敏状态可由飞书认证的 `ADMIN` 查看；任何配置保存、Secret 替换/清除和连接测试只接受
  `IdentitySource.LOCAL_ADMIN`。
- 配置保存只表示 `declared/configured`，不表示进程已加载、网络可达、adapter 已启用或能力已验收。
- 环境字段只是一项受控参数；把资源标成 `prod` 不授予生产网络调用权。
- `resource_id` 由系统生成且不可编辑；显示名称不是授权键。
- 每个 DTO `extra="forbid"`，不接受任意 JSON、驱动参数、代理、脚本、SQL 或 Python 类名。

### 10.2 数据库资源

每个资源使用封闭 DTO，不接受任意 JSON：

```text
环境                  test / prod 等受信枚举
显示名称
数据库类型            代码支持的闭集；首版只登记 STARROCKS
主机 / 端口
数据库或默认 schema
用户名
密码或 Secret         只写，永不回显
TLS 模式               关闭 / 校验证书等受支持闭集
启用状态
```

页面不能编辑 SQL 模板、SQLGuard、capability、工具类、任意驱动模块、任意连接参数字典或网络代理。
保存时只做类型、长度、host/port 和 TLS 组合的本地校验，不做 DNS、socket、登录或 SQL。W4b 不创建
DBA/值班职责绑定，也不把职责写入含 Secret 的资源文件；该能力留给 W3 后续增强。查询人/审批人
不在 W4b 预存，随 R1 的结果 ACL 真源一起实现。

### 10.3 Prometheus 资源

```text
环境 / 显示名称
服务地址
认证方式              none / basic / bearer 等受支持闭集
用户名                仅 basic
密码或 token          只写
TLS 模式
启用状态
```

URL 必须经过目标类型、协议、主机和网络策略的**纯本地形状校验**；不能借配置页把任意 URL 变成
服务器端请求代理。W4b 只保存参数，不发 HTTP 请求，也不创建资源职责绑定。真实 adapter 尚未获准
消费前，页面明确显示“已保存，尚未接入”。

### 10.4 AI API

第一版只开放当前 adapter 真正支持的字段：

```text
Provider              固定 Gemini，只读
启用状态
API Key               只写
模型 ID               当前 ADR-015 固定值，只读
API Origin            当前 ADR-015 官方值，只读
```

配置 AI 只改变模型 adapter 的连接参数，不改变模型是否拥有工具权、任务目标、SQL、审批或执行顺序。
新增 Provider、可编辑 model/origin 都需代码、测试和 ADR-015 修订，不允许在页面填写 Python 类名或动态插件。

### 10.5 飞书

```text
App ID
App Secret            只写
OAuth callback URL    服务端生成、只读、一键复制
消息/登录启用状态
凭据测试
OAuth 回调测试        明确标注“不登录、不绑定用户”
```

真实凭据测试与真实 OAuth 网络调用仍要满足部署环境授权和网络边界；测试结果不保存 Token 或 Provider 原文。

### 10.6 Secret 文件与进程可见性

数据库/Prometheus Secret 不能加入现有单一 `integrations.json`。W4a 将配置按消费域拆成三个宿主目录，
每个目录只有一个固定 `config.json`；Web 可写管理，消费者只读挂载所需目录：

| 宿主真源 | Web | task-worker | feishu-listener | channel-worker | API/migrate/postgres |
| --- | :---: | :---: | :---: | :---: | :---: |
| `.config/ai/config.json` | 读写 | 只读 | 不可见 | 不可见 | 不可见 |
| `.config/feishu/config.json` | 读写 | 不可见 | 只读 | 只读 | 不可见 |
| `.config/resources/config.json` | 读写 | 只读 | 不可见 | 不可见 | 不可见 |

规则如下：

- 三个宿主目录均 Git-ignored、Docker-build-ignored、`0700`，文件由非 root Web 进程以固定绝对路径原子写；
- consumer 只得到只读 bind mount，不得通过共享父目录看见其他域；
- Secret 不进入数据库、环境变量、Compose 渲染、日志、trace、异常、DOM、审计或加载回执；
- Web 在解析和原子写入时只短暂持有资源 Secret；写入完成后不驻留、不缓存、不放入任何回执或后续响应，
  也不得为了页面“已填写”状态保留明文副本；
- getter、`repr=False`、序列化排除和 secret-shaped literal 扫描沿用当前双重防线；
- 每个域独立 generation/digest，加载回执只记录 service/domain/generation/status，不记录值；
- 当前 `.config/integrations.json` 只作为 W4a 一次性迁移输入。迁移期间停止消费者；转换、校验和 fsync
  全部成功后删除旧文件。运行进程不长期双读；新旧真源同时存在时 fail-closed 为 `migration_required`。

Web 能看见三类 Secret 是其受信配置管理职责；这不赋予 Web `ToolGateway`、目标 adapter 或模型任务端口。
只有 task-worker 装配完整 Runtime/Gateway 和数据库/Prometheus consumer。

### 10.7 保存、测试和生效

不恢复已被否决的通用配置发布平台。第一版只提供最小且诚实的三步：

1. **保存**：服务端校验闭集字段，Secret 缺省表示保留，清除走独立动作；同目录临时文件 + 原子替换；
2. **本地校验**：数据库/Prometheus 只校验 DTO、host/port/TLS 和引用闭集，网络调用次数必须为 0；
3. **重启生效**：任何配置变化都显示明确的 `restart_required` 服务列表。管理员在宿主执行受控重启，进程
   启动后写加载回执；Web 不挂载 `docker.sock`，不热加载、不在页面造进程管理器。

AI/飞书沿用当前已有的显式探针，但必须同时满足 `LOCAL_ADMIN`、对应真实调用开关和 RI2/RI3 现场 GO；
未获 GO 时按钮禁用，后端仍拒绝且外部调用为 0。数据库/Prometheus 真实探针不属于 W4b：未来 W4c 必须先
修订 ADR-007，并另写详细设计决定唯一合法的 task-worker 调用路径。路径未批准前网络调用恒为 0；无论最终
选择为何，Web 都不得直接持有目标客户端，也不得建立绕过现有安全链的第二条工具入口。

目标是让管理员不再手工编辑 `.env`/JSON，同时诚实显示“已保存”“待重启”“已加载”“尚未接入”和
“测试未获授权”。配置格式不合法时保留上一份有效配置；不建设热加载、历史版本库、任意回滚树或多写者 CAS。

## 11. 未来数据库结果访问边界

本节是未来产品不变量，不是 W0–W5 实现范围。当前安全任务详情、TaskStore、Evidence 和
`ReadClass.BOUNDED/RESTRICTED` 都不能充当数据库结果 ACL 真源。

### 11.1 两层证据必须分开

| 内容 | 群成员/安全任务详情 | 申请人/审批人结果页 |
| --- | :---: | :---: |
| 状态、耗时、目标别名、安全摘要 | 是 | 是 |
| “还有更多证据”提示 | 是 | 是 |
| 真实结果行 | 否 | 是 |
| 结果文件/导出 | 否 | 是 |
| SQL 中可能含敏感值的原文 | 否 | 仅既有规则允许时 |

飞书卡片和普通 `RenderPayload` 永不嵌入真实结果行。未来结果里程碑只能投影受保护的
`/results/{result_ref}` 链接；当前 M7 卡片只能链接既有 `/app/tasks/{task_id}` 安全详情，不能生成不存在
的 `result_ref`。

### 11.2 实现前置条件

只有同时满足以下条件，才能另写数据库结果详细规格和实施计划：

1. 数据库查询域已经产生稳定、不可枚举的 `result_ref` 和有界结果 artifact；
2. 结果域持久保存 requester、该次 approver、状态、有效期和导出规则，且有明确唯一真源；
3. ADR-005 已冻结审批主体、状态、有效期、拒绝/过期/冲突和审计语义；
4. ADR-013 已定义结果深链只投影引用、不把真实行带入 ChannelStore/RenderPayload；
5. 数据保留、脱敏、预览分页、导出授权和下载失效策略已获负责人批准；
6. 独立里程碑、详细计划和真实调用/数据处置授权已经审核。

前置条件不齐时：不创建 `/results/{result_ref}`、`ResultAccessService`、空表、fake 结果页或导出接口。

### 11.3 未来访问路径

未来实现可新增窄结果访问服务，但服务必须依赖结果域 Port，而不是从任务事实猜权限：

```text
result_ref
→ 当前已认证 principal
→ ResultArtifactPort 读取结果元数据与 requester/approver ACL
→ 确认 principal 是 requester 或该次 approver
→ bounded preview / export ticket
→ result-detail shell 安全渲染
```

- `result_ref` 使用不可枚举的随机引用，但随机性不代替认证；
- Web 不能从 TaskStore 或 Evidence 猜测谁是审批人；ACL 的真源必须来自数据库查询结果域；
- 激活只把飞书身份绑定到内部 user/actor，不添加 requester/approver 关系；
- Admin 若不是该次申请人或审批人，结果接口仍返回同样的隐藏式拒绝；
- 导出规则与飞书已确定规则一致，并由服务端生成短时、单用途或受 Session 绑定的下载授权；
- 未来结果入口首次遇到未知身份时复用第 7 节激活服务；批准身份不自动批准结果访问。

## 12. 后端组件与责任

继续使用模块化单体和窄进程，不新增身份微服务或配置微服务：

| 组件 | 责任 | 禁止 |
| --- | --- | --- |
| `UserDirectory` | 用户、角色、状态与外部身份读取 | 任务执行、结果 ACL 推断 |
| `ActivationStore` | `PENDING` 创建/复用、scope 读取与过期收割 | 写批准/拒绝终态、自动决定角色、保存原始请求 |
| `UserDirectoryStore.apply()` | 批准/拒绝 CAS；批准时目录事实、申请终态与审计同事务 | 第二条授权写路径、事务外终态更新 |
| `IdentityActivationService` | 创建申请、编排批准/拒绝、闭集拒绝审计 | 飞书网络、Task/Tool 调用、只在事务外预读 Admin 后授权 |
| `WebAuthService` | OAuth state、Session、认证来源 | 自动注册或提权 |
| `RoleAndDutyService`（W3 后续增强） | 未来承载 DBA/值班与激活通知绑定；R1 才扩展 requester/approver | W3 V1/W4b 提前创建、自定义策略表达式、结果 ACL 猜测 |
| `IntegrationConfigService` | 三域类型化配置、Secret 保留/清除、加载状态 | capability/Policy/SQL 编辑、目标网络调用 |
| `AdminAuditStore` | append-only Admin 操作事件与查询 | 任务生命周期事件、Secret、正文、结果行 |
| Web/飞书 interfaces | 协议解析、认证上下文、页面/卡片投影 | 业务路由、审批和工具执行 |

`TaskStore` 仍是任务真源，`ChannelStore` 仍只保存渠道绑定/投影，结果域仍是数据库结果真源。身份、渠道、任务、
Admin 审计和未来结果五类事实不互相复制。未来 `ResultAccessService` 只有第 11 节门满足后才进入组件表。

## 13. 部署形态

### 13.1 产品部署与离线开发分开

- `dev/offline/CI` 继续默认禁止真实 socket 和外部调用，确保测试不会意外访问飞书、数据库或模型；
- 文档化的 `release` override 是用户安装路径：包含所有已完成的应用进程；缺少某项配置时，该功能显示
  “待配置/待授权”，不能把整个 Web 拉垮；
- 当前基础 Compose 把 listener/channel-worker 开关写成字面 `false`。W5 必须由可审计 release override
  显式覆盖并用 Compose 契约测试证明；不能宣称“改 `.env` 就能打开”；
- W5 V1（计划 Approved V0.2）固定为 **provider-off 产品壳**：作用域固定 `dev-local/dev`，五个应用进程
  以 `XIAOWEI_RUNTIME_PROFILE=release` 运行，StarRocks 为 `disabled`，Gemini、OAuth、listener、
  channel-worker 与真实测试开关在 release 模板中固定关闭；普通对话只投影空准入快照，工作台如实显示
  当前无可执行能力。首次 release 不继承 recording 时代的 plan/evidence，命中历史执行数据时只能换新
  数据库或另行批准数据处置。这些证据不构成 RI6 或只读 V1 发布；真实 Provider/目标进入 release
  须另立计划与 GO；
- 配置齐全、对应真实调用 GO 已取得并完成重启后，已实现功能可用，不再要求用户寻找历史里程碑开关；
- 真实目标、凭据、网络、部署和 canary 的具体执行仍需对应环境的明确授权与证据；本规格不代替该授权。

### 13.2 首次启动

- migration 完成后幂等创建唯一 `admin/admin`，`must_change_password=true`；
- 第一次登录只能改密、退出，改密撤销旧 Session；
- 项目负责人接受受信局域网地址在改密前即可访问的风险，不强制 loopback 首启；部署文档必须醒目标注
  默认凭据和“部署后立即改密”；
- 上一条明示替代 `ADR-014` §RI5 R2 的 loopback 首次改密硬门；只有 W0 在 ADR 中单独记录负责人批准后，
  release override 才可在改密前暴露于受信 LAN。这不改变应用内“改密前只放行改密/退出”的路由闭集；
- 当前应用没有 HTTP rate limiter，本文不能写“速率限制继续生效”。OAuth state/ActivationStore 容量只是
  存储边界，不是防滥用；
- 正式 release/canary 必须在受信反向代理或 WAF 上对本地登录、OAuth start/callback 和激活入口配置限流，
  并留下配置、压测/反例和告警证据。没有边缘限流的 LAN 模式只能算本地体验，不能晋升 canary；
- 安全底线不因首次启动取舍放宽：密码哈希、Session、Origin/CSRF/CSP、Cookie、Admin 审计和容量边界
  继续生效；
- 正式跨主机/公网部署使用 HTTPS。局域网 HTTP 是受信网络模式，不开放内部 API、PostgreSQL 或 worker 端口。

### 13.3 服务可用性

- Web 核心 readiness 只依赖数据库、migration、认证存储和自身装配；第三方 Provider 故障不让管理页不可用；
- 数据库/AdminAuditStore 不可用时，只读页面可显示统一的“服务暂不可用”，但用户/职责/配置/
  Secret 的任何保存和激活审批都必须阻断；不能为了“配置文件仍能写”而绕过 fail-closed 审计。
- listener/worker 在功能已启用但配置缺失时进入可观测的 `waiting_for_config`，不忙循环、不反复刷外部请求；
- 任务 Worker 是唯一拥有完整 Runtime/Runner/Gateway/目标 adapter 的进程；Web、飞书 listener、渠道 worker
  继续使用窄装配；
- 配置变化一律经受控重启加载；不承诺热加载。页面通过加载回执区分 saved generation 与 active generation；
- release Compose 只发布 Web 所需端口；内部 API、数据库、worker 管理口不暴露到宿主外网；
- Compose smoke 中“文件可读/代次已加载”只证明离线装配，不得写成真实 Provider 或运维目标可达。

## 14. 错误、隐私与审计

### 14.1 用户可见错误

闭集区分：

- 登录失败；
- 身份待激活；
- 账号已停用；
- 当前账号不能进入此页面；
- 安全任务不可用；
- 激活容量暂时不可用；
- Provider 未配置/待应用/测试失败；
- 资源已保存但尚未接入；
- 服务暂不可用。

普通用户不能通过错误差异判断某个任务、未来结果、用户、群或资源是否存在。Provider 原始正文、数据库
异常、Token、Secret、digest 和内部地址不进入页面。

### 14.2 Admin 审计

下列动作写入结构化审计：

- 激活批准/拒绝、角色改变、账号禁用/启用；
- W3 后续增强中的 DBA/值班/激活通知范围改变；未来 R1 再纳入 requester/approver 范围改变；
- 配置保存、Secret 替换/清除、连接测试开始与闭集结果；
- 查看私人聊天或敏感任务正文；
- 新增 Admin、飞书身份绑定/解绑；
- 未来结果导出仍由结果域审计，不复制进 Admin 审计。

新增独立 append-only `AdminAuditStore`，不能复用 `task_audit_events`。事件至少包含：

```text
event_id / operation_id
tenant_id / environment_id
actor_user_id / actor / auth_source
action                 闭集枚举
target_kind            USER | ACTIVATION | DUTY_BINDING | CONFIG | TASK_CONTENT
target_ref_digest      domain-separated digest，不保存敏感正文
outcome                STARTED | SUCCEEDED | DENIED | FAILED
reason_code            可空闭集，不存异常正文
created_at
```

审计只保存必要引用、操作者、时间、动作和闭集结果；不复制 Secret、聊天正文、真实结果行或 Provider 原始
响应。首版不提供更新/删除 API，也不自动清理；如未来需要保留周期或合规清理，必须另修 ADR 并提供独立
运维证据，不能在 Admin 页面加“清空审计”。

登录成功/失败、改密和登出是认证生命周期事件，不是 Admin 对业务对象的操作。它们走闭集、脱敏的
结构化安全日志，不进 `AdminAuditStore`；日志不记密码、Cookie/state、Secret、异常正文或未验证的用户输入。
本规格不顺带新增第二套认证审计表；如未来合规要求持久化，另立边界。

授权改变、配置改变和敏感内容查看都以 `AdminAuditStore` 可用为前置：审计不可写时操作 fail-closed。
用户/角色/职责/激活都是同库事实，它们的状态迁移与成功审计必须在同一数据库事务提交；审计写入失败
就回滚授权改变。文件配置与数据库审计无法处于同一事务，因此配置操作先追加 `STARTED`，完成后追加
`SUCCEEDED/FAILED`；只看到 `STARTED` 就表示结果未知，不能伪装成功。

## 15. 安全不变量

1. LLM 不参与用户激活、角色分配、结果 ACL、配置发布、Secret 选择或审批裁决。
2. Web/飞书仍是薄入口；任务继续走统一 Runtime、安全链和 TaskStore。
3. `ChannelPermission` 与 `AdminCapability` 是两个闭集；产品角色、认证来源、资源职责和结果 ACL 不互相替代。
4. 链接、OAuth 成功、群成员身份、Admin 角色和激活批准都不能单独授予未来数据库结果权限。
5. OAuth 登录/激活与连接测试使用隔离 state domain，但共用备案 callback；测试永不签发用户 Session。
6. 激活持久化层承重单 subject 去重、24 小时过期、CAS 与 1024 全局 pending 上限：
   `ActivationStore` 只写 `PENDING/EXPIRED`，`UserDirectoryStore.apply()` 只写
   `APPROVED/REJECTED`，后者与目录/审计共用一个事务。
7. 所有 Session/state/一次性票据只在浏览器保存原文，服务端保存 domain-separated digest 与必要时效事实；
   激活所需 `subject_ref` 是受控 PII，不进普通日志。
8. Secret 只写不回显，不进数据库、环境明文、日志、trace、异常、DOM、测试夹具和 Git；消费者只能看到
   自己域的只读挂载。
9. 飞书 Admin 不得修改配置或发起测试；这些接口只接受 `LOCAL_ADMIN`，真实探针还需对应现场 GO。
10. W4b 保存数据库/Prometheus 参数时网络调用次数恒为 0；未来 W4c 不得让 Web 直接持有目标客户端。
11. 自定义 endpoint 只有 adapter 和 ADR 明确支持时才可开放；不能提供任意 URL/驱动/代理探测器。
12. 禁用/撤权在后续请求实时生效；页面必须删除已渲染的受保护数据。
13. 用户/权限/配置/敏感查看的 Admin 审计不可写时 fail-closed；不能拿任务审计表代替。
14. Admin 管理权不能绕过 SQLGuard、ToolPolicy、ApprovalGate、ToolGateway 或终态保护。
15. 静态 JS 是否可下载不是授权边界；所有 API 均按服务端 principal、作用域和能力重新判断。
16. release 可用性不反向削弱 offline/CI 的 `--disable-socket`、真实调用门或证据分级；存储容量也不能
    冒充边缘限流。

## 16. 验证与验收设计

### 16.1 单元/契约测试

- 三角色到现有三项 `ChannelPermission`、新增 `AdminCapability` 和认证来源约束的正反例；
- 旧标签迁移、重复 actor/open_id、跨租户/环境冲突；
- 激活申请去重、24 小时过期、1024 容量、过期清理、容量失败、拒绝、并发审批、重放和半事务失败；
- 群事件 sender 与 OAuth 返回 open_id 不一致时拒绝；
- （**W2**）`web_oauth_login_contexts` 与登录 state 同事务签发/消费、`return intent` 闭集、外部 URL/路径
  穿越反例，以及连接测试 state 无 context 行的对照；
- callback 登录域优先、测试域安全回落，以及身份未知不会误入测试域；
- 飞书连接测试不访问身份目录、不签 Session、不创建激活申请；
- 本地 Admin 首次改密前的路由闭集；
- 所有新增 JSON 写路由已进入 body limit、Origin/CSRF 和错误闭集注册表；
- 三域配置字段、Secret 保留/替换/清除、跨环境、原子写失败、旧文件迁移冲突，以及写入后不驻留/
  不缓存/不进回执的反证；
- 数据库/Prometheus 保存路径在 DNS/socket/adapter 全部设反证时仍为 0 调用；
- saved generation、restart_required、loaded receipt 的确定性状态；
- Admin/运维/普通用户得到正确 HTML shell，每个 shell 只加载自己的 JS；静态资源公开不改变 API 授权；
- `AdminAuditStore` append-only、闭集 action/outcome、敏感字段拒绝，以及审计失败时激活/角色/配置/
  敏感查看 fail-closed；登录/改密/登出只进结构化安全日志的正反例；
- 禁用/撤权后清除页面内容，错误不泄露任务、用户或资源存在性。

### 16.2 安全测试

- CSRF、Origin、Cookie、Session rotation、OAuth login CSRF/replay/expiry；
- 激活链接猜测、重复审批、跨群/跨 tenant/跨 environment 绑定；
- 角色前端篡改、隐藏按钮直调 API、飞书 Admin 直调配置/测试接口；
- 外部文本 XSS、CSP、无 `innerHTML`/内联脚本；
- Secret-shaped literal、日志/异常/审计脱敏；
- 渲染后的 Compose 挂载单元格与 §10.6 矩阵逐格相等，API/listener/channel-worker 看不见数据库/
  Prometheus Secret；
- 数据库/Prometheus 参数中的控制字符、任意 scheme、代理、路径和驱动扩展 fail-closed；
- 正式 release 缺边缘限流配置时部署检查失败；ActivationStore 达容量时无存在性泄露；
- Web/飞书进程继续不加载完整 Runtime/Gateway/目标 adapter。

### 16.3 集成与浏览器验收

- PostgreSQL 上身份/作用域角色/激活容量与 CAS、Admin audit append、Session 撤销和 migration 一致性；
- fake 飞书 OAuth 的已登记登录、未知身份激活、Admin 审批、重登闭环；
- 1024/1280/1440 工作台与管理中心视觉检查；
- 手机宽度的登录、激活状态和任务详情视觉/键盘检查；
- wheel/容器内静态资源完整性；
- release Compose 中所有已实现进程启动、缺配置等待、配置文件按角色挂载、重启后加载回执和内部端口不暴露；
- Compose 的“配置已加载”只按文件/generation/receipt 断言，不发真实 Provider/目标请求。

未来结果里程碑必须另加 requester/approver 正例，以及 Admin、群成员和其他用户反例；当前测试集不得用
fake 结果页提前勾掉该验收项。

### 16.4 证据分级

- 代码/测试通过只证明离线实现；
- 浏览器截图只证明指定尺寸的 UI 检查；
- 配置保存/加载回执只证明 `configured`，不证明真实服务可达；
- 真实飞书、数据库、Prometheus、AI API 必须分别留下授权、部署 SHA、配置版本、脱敏 trace 与用户验收；
- 任一真实集成未验证时，必须写“未覆盖”，不能用 fake 或 CI 代替。

## 17. 实施拆分边界

本规格是一个产品目标，但实现必须拆成可独立评审、可回滚的阶段；不能以一个巨型 PR 落地：

### 17.1 当前可交付序列

1. **W0 文档与 ADR**：修订 ADR-007/013/014/015、ARCHITECTURE、DEVELOPMENT_PLAN、README 和
   handoff；消除当前 I2/RI5/Secret 真源漂移；把 §19.2 的四条已接受条款变更逐条写入 ADR；不写源码。
2. **W1a 用户、权限与审计写内核**：`UserAccount`、作用域 `UserRoleAssignment`、
   `ExternalIdentity`、`LocalCredential`、`AdminCapability`、旧标签迁移，以及 `AdminAuditStore` 持久化与
   append-only 写入契约；用 PostgreSQL/fake 共享套件证明角色/账号改变与审计同事务、审计失败时回滚。
3. **W1b 激活内核**：`ActivationRequest/ActivationStore/IdentityActivationService`、24 小时过期、
   全局容量、CAS 审批、群事件绑定，以及**群事件当次响应内**的通用激活卡片；审批必须复用 W1a 的
   审计写契约并 fail-closed；不改页面。
4. **W2 登录与页面壳**：成熟登录/改密/激活状态，`web_oauth_login_contexts` 与闭集 return intent，
   拆分工作台和 Admin shell，并把当前 I2 统一 Runtime/RenderPayload 接进新工作台；不新增聊天路由。
5. **W3 V1 用户、激活与审计界面**：Admin 用户/激活/角色管理、受管授权变更与 `AdminAuditStore` 查询 API/UI；
   不在此阶段才补审计写底座，也不提前建 requester/approver 名单。DBA/值班/激活通知管理、私聊/
   可靠通知与敏感查看审计归入 W3 后续增强，延期但未取消，且不作为 W4a/W4b 的进入条件。
6. **W4a AI/飞书配置迁移**：把 W2 已迁入 Admin shell 的当前 RI5 Gemini/飞书配置从单文件迁到
   按消费域拆分的 AI/飞书文件与挂载；固定 model/origin 只读，保留现有探针语义和现场 GO。
7. **W4b 数据库/Prometheus 参数登记**：新增资源 DTO、resources 文件与 task-worker-only 挂载；
   只做本地校验，真实目标网络调用次数为 0，不建职责绑定或 requester/approver 数据。
8. **W5 产品部署**：release override、listener/channel-worker 可审计开关、边缘限流、配置目录预检、
   明确重启、加载回执、浏览器视觉和分级运行证据。W5 只依赖 W2–W4b，不以 W4c 或 R1 为必经前置。

**W1b 计划复审修订（2026-09-22）。** 本节此前把登录 context 表放在 W1b，而
`DEVELOPMENT_PLAN.md` 的 Web 序列把它放在 W2；两份都是已批准真源，实现者按哪一份做都能自称
合规。现按 `DEVELOPMENT_PLAN.md` 收敛：该表与闭集 return intent 属于 W2，第 7.2 节
`ActivationRequest` 的对应字段和 `SAFE_TASK_LINK` 来源成员也随 W2 添加。同一轮把私聊 Admin 激活通知与可恢复投递重试移到
W3 后续增强：它们的收件人真源是未来的激活通知人绑定，而当前 `external_identities` 只保存不可逆的
`subject_ref_digest`，`UserDirectoryStore` 也没有列出 Admin 的读路径——W1b 没有能力把通知投到
具体的人。W1b 保留群事件当次响应内、不带链接且不 @ Admin 的通用激活卡片，这条不依赖任何持久化
收件人。W1b 的终态写也收敛到 `UserDirectoryStore.apply()`：`ActivationStore` 只写
`PENDING/EXPIRED`，避免批准时在目录事务之外再开第二条 CAS 写路径。负责人已在
W1b 计划收口轮确认该阶段归属；这不等于 W1b 实施计划已经通过最终复审或获得开工授权。

**W2 计划复审修订（2026-09-23，随 W2 详细计划送审）。** §14.1 增加“当前账号不能进入此页面”这一
用户可见终态，专门承接“认证成功但角色不允许 return intent”的组合；它不签 Session，也不把普通用户
误报成登录失败或待激活。W2 把现有 RI5 配置 UI/API 移出工作台，W4a 只负责随后迁移其单文件存储与挂载，
两阶段以 `DEVELOPMENT_PLAN.md` 的归属为准。本段随 W2 计划合入才生效，不提前授权 W2 源码、W3/W4、
真实调用、部署或 UAT。

### 17.2 独立阻塞门

- **W4c 数据库/Prometheus 真实探针**：不在上述交付序列内。先修 `ADR-007`、指定环境/目标/
  Secret/窗口/数据处置、审核唯一 task-worker 调用路径并取得现场 GO；Web 永不直接连接目标。
- **R1 未来数据库结果访问**：只有第 11 节六项前置全部满足后另立规格；不属于 W0–W5，也不由本规格
  审核自动授权。requester/approver 职责与结果 ACL 真源必须在 R1 同步交付。

每个阶段都从最新 `main` 开独立分支，先写详细计划并审核，再按 TDD 实现。W0 合入前不得启动 W1；
W4c、W5 的真实部署/验证和 R1 各自需要新的明确口令。任何阶段都不得为了 UI 完整度伪造数据库结果页面。

## 18. 明确非目标

- 普通用户首页、“我的结果”、普通用户任务列表或 AI 工作台；
- 手机端运维工作台、任务发起、任务列表和后台配置；
- 任意 RBAC/Policy DSL、用户自定义角色、组织同步或多租户管理平台；
- 让 Admin 页面编辑 capability、Planner、SQL/PromQL 模板、ToolPolicy、ApprovalGate 或工具顺序；
- 引入 SPA 框架、微前端、身份微服务、配置微服务、消息总线或 Docker Socket 管理；
- 通用动态 Provider 插件、可编辑 Gemini model/origin、任意 Python/URL/驱动配置；
- 配置热加载、历史版本平台、自动回滚树、多写者配置协同；
- 在 W4b 从 Web、worker 或测试连接数据库/Prometheus；真实探针只可能进入另行授权的 W4c；
- 在当前 W0–W5 创建数据库结果 artifact、`/results/{result_ref}`、预览、导出或 `ResultAccessService`；
- 自动执行激活前请求、自动批准身份、首个 OAuth 用户自动成为 Admin；
- 以 Admin 角色绕过数据库结果 ACL；
- 把 OAuth/ActivationStore 容量上限宣传成 HTTP rate limiting；
- 在本设计中开放新的 E1、生产写、真实调用、部署或用户验收权限。

## 19. 对既有规范的影响

规格获批后、写实施计划前，W0 必须同时处理“新增能力”和“取消/收窄已接受条款”。后者不能再混在
功能列表里靠评审者自己推导。

### 19.1 新增能力同步

- `ADR-007`：明确本地 Admin 可登记数据库/Prometheus 参数但 W4b 不能联网；资源 Secret 只挂 task-worker；
  飞书 Admin 只能读第 6.1 节的脱敏状态投影，原始配置读取、mutation 和 probe 仍只接受 `LOCAL_ADMIN`；
  W4c 真实探针是独立授权面，不能由 W4b 或配置开关解锁。
- `ADR-013`：增加持久身份目录、独立 `AdminCapability` 和 Admin audit 边界；保持现有三项
  `ChannelPermission`、薄渠道、TaskStore/ChannelStore 真源和进程隔离不变。数据库结果深链及
  requester/approver 职责只记录为 R1 未来变更门，不提前创建契约或名单。
- `ADR-014`：增加 `ActivationRequest` state/context、24 小时 TTL、1024 pending 容量、Admin 审批、
  群 sender 绑定；明确登录/测试不同 domain、同一 callback 及当前 fallback 顺序，并显式修订第 19.2 节的
  R1/R2/R3 条款，不得一边保留旧文一边实现新口径。
- `ADR-015`：把现有 AI/飞书单文件配置迁成按消费域拆分的挂载；Gemini provider/model/origin 继续固定，
  不借本规格开放动态 provider 或 Web 模型调用权。

### 19.2 本规格取消或收窄的既有条款

| 规范条款 | 已接受口径 | V0.3 新口径 | W0 必做动作 |
| --- | --- | --- | --- |
| `ADR-014 RI5 R1` | 配置读取/保存/测试只接受 `LOCAL_ADMIN`，飞书 principal 不得读配置状态 | 只为飞书认证 `ADMIN` 增加第 6.1 节的脱敏状态投影；原始配置读取、保存和测试仍禁止 | 修订 R1，冻结状态 DTO 闭集和反例 |
| `ADR-014 RI5 R2` | LAN override 只能在 loopback 首次强制改密完成后启用 | 项目负责人批准受信 LAN 在改密前可达；用“部署文档醒目警示 + 边缘限流 + 立即改密 + 改密前路由闭集”替代 loopback 先后硬门 | 在 R2 单独记录负责人批准、风险、补偿措施与回滚方式 |
| `ADR-014 RI5 R3` | 本轮 schema 解冻不包含 `web_oauth_states`；新增 state 列或表必须先修 ADR | 允许且仅允许新增登录域 `web_oauth_login_contexts`，用 state digest 做唯一 PK/FK；连接测试 state 不产生 context | 在 R3 精确放开该表、事务/清理不变量和禁止任意 URL 字段 |
| `ADR-007 D5` | 任何非 `LOCAL_ADMIN` 读写配置或发起探针都命中变更门 | 只放开飞书 `ADMIN` 读脱敏状态投影；配置读 DTO、写入、Secret 和 probe 不放开 | 修订 D5 的“读”半句并保留其余现场 GO/证据门 |

项目负责人在 2026-09-20 本轮指令中批准按上表修订，包括 R2 的安全边界替换、R1/D5 的窄读例外，
以及 R3 选择独立 state-scoped 表。该批准只允许 W0 修订文档，不授权源码、部署或真实调用；W0 合入前，
现行 ADR/运行手册仍是生效真源。

### 19.3 W0 同步文档

- `ARCHITECTURE.md`：统一用户/作用域角色/当前职责、Admin audit、配置文件可见性、未来结果门和 release 边界；
- `DEVELOPMENT_PLAN.md`：把 §17.1 的 W0–W5 建成可独立验收的交付序列，把 W4c/R1 单列为阻塞门；
- `README.md`：更新登录、初始化、Admin 配置和产品部署路径；
- `AGENT_HANDOFF.md`：修正 I2 已合入、RI5 配置真源等漂移，只记录实际合入、测试、部署与未覆盖证据。

ADR-005 不在 W0 被臆造完成；它是 R1 requester/approver 审批语义的未来前置。上述文档修订必须先于
源码。保留的是本地 Admin 原始配置读取/mutation/probe、固定 Gemini 和真实调用 GO；窄扩展只有第 19.2 节显式列出的
脱敏状态读取、首启顺序替换和登录 context 表。不允许一边保留旧限制，一边在源码里偷偷扩大权限。

## 20. 规格退出条件

下列条件是进入实施计划的前提。截至 2026-09-20，前两项已满足（见头部「批准来源」与 PR #59）；
其余条款是本规格对后续阶段的持续约束，不因批准而解除。

- **已满足**：项目负责人已批准第 19.2 节四条修订；技术复核对本 V0.3 书面规格明确接受；
- 三角色、现有三项 `ChannelPermission`、`AdminCapability`、认证来源和职责名单没有歧义；
- 接受“飞书 Admin 管用户/当前职责/审计并只读脱敏集成状态；原始配置读取、mutation/probe 只接受
  本地 Admin”的边界，并在 `ADR-014` R1 / `ADR-007` D5 可追溯；
- 接受 `admin/admin` 在受信 LAN 首次改密前可达，以“部署文档 + 边缘限流 + 立即改密 + 改密前路由闭集”
  替代 loopback 首启硬门，且该安全边界替换在 `ADR-014` R2 单独可追溯；
- 接受“登录/激活与连接测试不同 state domain、同一 callback，测试不登录/不绑定用户”的分离；
- 接受登录 `return intent` 使用 `web_oauth_login_contexts` 与一次性 state 精确绑定，并先修 `ADR-014` R3
  精确放开该表；
- 接受三域 Secret 文件/进程挂载矩阵，以及旧 `integrations.json` 不长期双读；
- 接受 W4b 数据库/Prometheus 只保存参数、网络调用为 0，W4c 另修 ADR-007 并单独授权；
- 接受 `AdminAuditStore` 独立于任务审计、append-only 且敏感操作在审计不可写时 fail-closed；
- 接受 `AdminAuditStore` 持久化/写契约在 W1a 先于 W1b 激活审批落地；W3 V1 增加查询 UI 与受管
  用户/角色/激活变更，仍复用唯一授权写入口，不新增第二条授权写路径；
- 接受 DBA/值班/激活通知职责与敏感查看审计作为 W3 后续增强延期，不作为 W4a/W4b 或 W5 的
  进入条件；requester/approver 名单随 R1 一起交付；
- 接受应用当前没有 HTTP rate limiter，正式 release 必须由边缘限流提供运行证据；
- 接受数据库结果 ACL 规则保留但 R1 延后，当前不实现 `/results`、预览、导出或结果服务；
- 接受 §17.1 的 W0–W5 是当前交付序列，§17.2 的 W4c/R1 是独立阻塞门，不把后者排成产品部署必经步骤，
  也不把设计稿记为已实现；
- **已满足**：W0 文档/ADR 计划已单独审核并经 PR #59 合入；其收口动作本身尚未实施；
- W0 收口合入后，才调用 `superpowers:writing-plans` 生成 W1a 实施计划；该计划再次审核通过后
  才能 TDD 写源码。
