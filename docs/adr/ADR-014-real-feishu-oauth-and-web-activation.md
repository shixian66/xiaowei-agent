# ADR-014：真实飞书 OAuth 与 Web 激活安全契约

- 状态：Accepted（2026-09-10）+ **Accepted RI5 修订（2026-09-14；不授予 RI2 现场 GO）**
- 日期：2026-09-10；RI5 修订 2026-09-13 起草、2026-09-14 接受
- 决策人：项目负责人
- 相关：[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-013](ADR-013-m7-channel-boundary.md)、[ADR-015](ADR-015-real-model-provider-boundary.md)、[真实接入总体设计](../superpowers/specs/2026-09-10-real-integrations-design.md)、[RI5 简化设计](../plans/RI5-local-web-admin-simplified-design.md)

> **修订状态说明**：下文 §决策 D1–D5 是 2026-09-10 已接受的口径，保持原样。§RI5 修订
> 只在 RI5 本地 Web Admin 范围内增补或收窄，已于 2026-09-14 被接受。ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订已由项目负责人于 2026-09-14 成套接受；该接受不授予 RI3 PR 3E 与 RI2 的真实调用 GO。
> 接受本修订也**不**授予真实飞书调用许可——RI2 现场 GO 仍是独立硬门。

## 背景

M7 已实现默认关闭的 fake OAuth、digest-only state/session 和 Web 认证边界，但真实 provider
adapter 与 composition root 尚未实现。RI1 只建立默认关闭、可离线验证的代码门；它不注册真实
应用、不读取真实 secret、不发起网络调用，也不取得部署、canary 或产品用户验收许可。

真实 OAuth 会新增三个必须分开治理的风险：provider endpoint 与 secret 的供应链边界、一次性
state 的并发容量边界，以及公网 SSO 入口的请求速率边界。state 表的容量上限不能冒充按 IP 的
HTTP 限流，provider 调用结果也不能把外部正文带回页面或日志。

## 决策

### D1 Provider 与身份契约保持闭集

飞书 provider origin 是代码内唯一常量；OAuth 授权/token endpoint 必须由它派生，飞书 SDK 的
REST 与长连接 client 也必须显式收到同一个 origin，不依赖 SDK 默认值。Compose 离线 smoke 的
provider 黑洞 host 必须由契约测试机械核对为同一 endpoint host；锁定 SDK 的 REST/WS 路径仍为
相对路径也由安全测试承重。origin 与 endpoint 不接受环境变量、请求、Host、forwarded header、
用户或模型提供任意 URL。App secret 只从受信 composition root 取得的绝对只读文件读取；不接受
明文环境变量，不进入数据库、日志、trace、异常或页面。

`FeishuOAuthPort` 只接受由服务端生成的 `state` 和受信 public origin 推导的 `redirect_uri`。
OAuth 返回身份只包含飞书 `open_id`；它不是本地 actor、tenant、environment 或权限。Web 服务必须
在登录和每次受保护请求时用当前只读身份目录重新解析授权。

### D2 Origin、HTTP 与 Host 信任边界分离

基础 Compose 继续只把 HTTP Web 端口发布到 `127.0.0.1:8080`。浏览器 OAuth redirect 与 session
安全 origin 只使用已有、明确配置的 HTTPS SSO public origin；RI1 不新增 TLS、Ingress 或反向代理。
应用不得从任意 `Host`、`Forwarded`、`X-Forwarded-*` header 或请求 URL 推导 public origin、
callback、cookie domain 或安全决策。直接 HTTP IP 和伪造 Host 不能完成 OAuth 或取得受保护 session。

### D3 OAuth state 由现有 PostgreSQL 契约有界承重

state 与 session 继续由现有 `WebSessionStore` 和 PostgreSQL 表承重；本 ADR 不新增表、列、索引或
migration。state 只保存 SHA-256 digest、签发/过期/消费时效事实，不保存原文。

生产默认最多存在 **1024** 个全局未完成 state。这个正整数只允许作为受信 store 构造参数在小型
契约/集成测试中覆盖；覆盖只能把容量缩小到 `1..1024`，不能放大安全硬上限。默认值固定为 1024，
不成为环境、请求、模型或用户字段，且 bool、零、负数和大于 1024 的值都必须拒绝。

每次签发必须在同一写事务中按固定顺序执行：取得不含用户输入的 transaction advisory lock；在锁
后读取时钟；物理删除已消费或已过期 state；统计全局未完成行；未达上限才插入新 digest。达到上限
时清理事务仍正常提交，事务退出后只抛出闭集 `OAuthStateCapacityError`。固定锁关闭多进程
`count → insert` 竞态；单进程 Python lock 不能替代它。fake 在共享 `asyncio.Lock` 下执行相同顺序。
清理后的 digest 不再碰撞；仍存活的 digest 碰撞保持既有冲突语义。

容量拒绝只使用不敏感常量文本，并由 `WebAuthService.start_login()` 映射为现有
`WebOAuthUnavailableError`，HTTP 路由继续返回 503。异常、响应和日志不得包含 state、cookie、
digest、数据库异常正文或异常链。

### D4 容量边界、provider 追踪与 HTTP 限流是三件事

state 行清理和 1024 容量上限只限制数据库中的全局未完成 state，解决数据库无界增长；它不是
应用级滥用保护，更不是按 IP、用户或租户的请求速率限制。OAuth start 在登录前可匿名访问，攻击者
可以在一个 state TTL 内填满这份全局额度，使所有正常登录持续收到 503；持续补位还能把拒绝维持
下去。容量上限在这里是 fail-closed 的资源边界，同时也是明确的全局可用性 DoS 风险。

未来真实 provider 调用必须使用受信 trace 关联和闭集 outcome，原始 provider 正文不能成为控制
信号或用户可见错误。没有 task ID 的登录流程不伪造 task/audit 行；无任务 trace 仍走既有结构化
日志边界。

实际 HTTP 请求速率限制由已有 HTTPS SSO 边缘入口承担。RI2/RI6 激活门必须记录该设施的真实限流
配置、监控/告警和运行反证；没有证据不得激活公网 OAuth、不得宣称已具备按 IP 限流，也不得用
state 容量测试代替。

### D5 RI1 仍无真实调用许可

RI1 只允许实现默认关闭的 adapter/composition root 与离线 fake/recording、隔离 PostgreSQL 测试。
真实应用、secret、飞书网络调用、公开部署和 canary 仍须 ADR-007 F 层与 RI2 的独立现场 GO。
ADR-007 H 层生产只读授权尚未单独签认；生产连接与任何 E1/生产写均未授权。

## RI5 修订（2026-09-13 起草，2026-09-14 Accepted）

RI5 交付本机 Compose、局域网范围内的最小 Web Admin：一个本地管理员、Gemini 与飞书两个闭集
集成、`lan_http` 与 HTTPS 两种显式 Web 模式。完整范围见
[RI5 简化设计](../plans/RI5-local-web-admin-simplified-design.md)。以下条目按 D 编号增补或
收窄，不改写已接受条文本身。

### R1 修订 D1：Web 成为受信的本地配置管理进程

飞书 App Secret 的来源由"受信 composition root 取得的绝对**只读**文件"扩展为本地私有配置文件
`.config/integrations.json`：Web 读写挂载其所在目录，实际需要飞书配置的进程只读挂载，API 不
挂载。其余 D1 约束不变——secret 仍不接受明文环境变量，不进入数据库、日志、trace、异常或页面。

飞书 OAuth 由 Web 的启动条件降级为**可选登录插件**：配置缺失、无效或装配失败只标记
`feishu_oauth` 不可用，不阻止 Web 启动、本地管理员登录或配置页访问。

D1 的"每次受保护请求用当前只读身份目录重新解析授权"继续适用于 `FEISHU` principal。新增的
`LOCAL_ADMIN` principal 不查身份目录，使用固定映射 `tenant_id=dev-local`、
`environment_id=dev`、`actor=admin`，权限取现有闭集中的管理员三项。配置读取、保存与 Provider
测试接口**只接受 `LOCAL_ADMIN`**；飞书 principal 即使持有 `ADMIN_ALL_SAFE_TASKS` 也不得读取
配置状态、修改凭据或发起连接测试。本修订不新增配置 RBAC 权限。

### R2 修订 D2：单一 public origin 与外科手术式的协议放开

现有 `web_detail_base_url` / `XIAOWEI_WEB_DETAIL_BASE_URL` **重命名并迁移**为唯一的
`web_public_origin` / `XIAOWEI_WEB_PUBLIC_ORIGIN`，不并存第二个 Origin 真源；Web Session、
OAuth callback、Origin 校验与飞书卡片深链统一消费该值。

新增显式 `lan_http` 模式，且只在该模式下放开：`http` 协议、canonical loopback 或 RFC1918
IPv4 字面量、显式端口。HTTPS 模式继续沿用现有受信 HTTPS hostname 约束。

**该放开只作用于本方 public origin。** 实现必须把 `interfaces/web_auth.py` 中同时服务于本方
public origin 校验与**飞书授权 URL 校验**的共用 HTTPS helper 拆成两个：provider 授权/token URL
的 HTTPS 约束不变，且须有承重测试证明 `lan_http` 模式下 `http` 授权 URL 仍被拒绝。直接放宽
共用 helper 属于违反本修订。

D2 的"不从 `Host`、`Forwarded`、`X-Forwarded-*` 或请求 URL 推导 origin/callback/cookie/安全
决策"不变。Cookie 在 HTTPS 模式继续使用 `Secure` 与 `__Host-`；`lan_http` 模式使用普通 Cookie
名且不设 `Secure`，仅 HTTPS 模式设置 HSTS。Session 在创建时绑定 canonical public origin digest，
认证时必须匹配，因此切换模式或 origin 等同于全体登出，不做跨模式 Session 迁移。

`lan_http` 的 Cookie 以明文 HTTP 传输，只用于受信局域网体验；正式部署仍走 RI6 的 HTTPS 边界。
D2"基础 Compose 继续只把 Web 端口发布到 `127.0.0.1:8080`"保持不变——局域网发布只能由独立
override 打开，且首次强制改密必须在 loopback 阶段完成。

### R3 修订 D3：有限解除 schema 冻结，但 `web_oauth_states` 不在其内

解除 D3 的"本 ADR 不新增表、列、索引或 migration"，允许且仅允许 RI5 所需的最小 schema 变更：

- 本地管理员表（最多一行）；
- Provider 当前状态（服务加载回执与连接测试结果）；
- `web_sessions` 的认证来源与 canonical public origin digest 绑定。

**该解除不包含 `web_oauth_states`。** OAuth 连通测试不得为此新增列或新表；测试 state 与登录
state 通过**不同的 digest domain 常量**落在互不相交的摘要命名空间（登录沿用 `oauth-state:v1`），
一类 state 不可能被另一条路径消费。

**digest domain 只提供路径隔离，不提供上下文绑定。** 现有 state 行只有 digest 与三个时间字段，
无法承载发起者身份或配置代次；因此本修订**不承诺**把测试 state 绑定到发起 Session 或某个
`generation`。所需的上下文改由请求侧与写入侧承担，见 R4。若将来确需在 state 行内做精确绑定，
必须先修订本 ADR 放开 state schema，不得由实现自行加列。

D3 的其余约束不变：state 与 session 仍只保存 SHA-256 digest 与时效事实，不保存原文；1024 全局
未完成 state 上限、固定 advisory lock 与清理顺序不变。

本地管理员的初始化只由 Web 在 migration-head 检查通过后幂等 seed（行不存在才插入），
**migration 不写入口令或口令哈希**。口令使用标准库 `hashlib.scrypt`（随机 16-byte salt，
`n=2**14`、`r=8`、`p=1`、`dklen=32`，保存带版本与参数的封装，校验用 `hmac.compare_digest`），
不新增认证依赖。

### R4 补充 D4：连接测试是控制面探针，不是数据面工具调用

Web 在管理员明确点击、且对应真实测试开关已打开时，可执行 `gemini_connection`、
`feishu_credentials`、`feishu_oauth` 三个连通性探针。它们属于控制面：不创建 Task、
TaskSubmission、Evidence 或 capability，不经过数据面 `ToolGateway`，也不改变任何服务的
readiness。该边界必须同步写入 `ARCHITECTURE.md`，不得由实现自行推断。

`feishu_oauth` 是 callback 连通性测试，**不等同于飞书登录验收**。它采用不新增 state schema 的
最小方案：

- **启动**：需 OAuth 插件已装配、且当前 `generation` 的 `feishu_credentials` 已通过；启动接口
  是状态变更 POST，必须同时通过 `LOCAL_ADMIN` 认证与 D2/§7.2 既有的 Origin + CSRF token 检查；
- **state**：只写入测试 digest domain，不携带发起者身份或代次；
- **callback**：命中测试 domain 时走测试分支，且必须验证请求仍持有**当前有效的 `LOCAL_ADMIN`
  Session**，否则拒绝并不写入任何结果；
- **结果**：code exchange 时读取**当时的当前配置**，并把**当时的当前 `generation`** 写入测试
  结果行。因此结果永远标注它实际验证过的那一代配置；若期间配置已被改写，新代次自然回到
  “待测试”，不会把旧结果冒充为新配置的证据。

测试分支**绝不签发、替换或延长任何 Session**，也不设置任何 Cookie，不要求
`feishu_identity_file` 映射。正式身份映射与飞书登录验收仍属于 RI2。

探针开关默认关闭且彼此独立。关闭时页面禁用按钮，接口在本地返回闭集 `REAL_TEST_DISABLED`，
外部调用数必须为零。D4 关于 provider 正文不得成为控制信号或用户可见错误的约束继续适用：失败
只持久化闭集 `error_code`、本地安全提示与耗时，不保存原始响应、Secret 或 Token。

### R5 修订 D5：RI5 同样不携带任何真实调用许可

接受本修订只解锁 RI5 的离线实现与本地 Compose 验证。真实飞书应用注册、凭据、网络调用、公开
部署与 canary 仍须 ADR-007 F 层与 **RI2 的独立现场 GO**；真实 Gemini 网络调用仍须
**RI3 PR 3E 的独立 test-env GO**。ADR-007 H 层生产只读仍未单独签认，E1/生产写仍未授权。

### R6 已处理的跨 ADR 冲突：ADR-007 D4 的 B2 与 G

[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)
D4 权限表 **G 行**记录的是**旧** RI5 设计（独立 `configuration_test` worker、复用完整
Runtime/Runner/Admission/Gateway 链、须经 ADR-016 修订、"独立 worker 不取得 Gemini key；固定
无用户数据 Gemini probe 只由既有 task-worker 的窄 control port 执行"）。新 RI5 设计取消了该
独立 worker，由 Web 自身读取配置并执行控制面探针，与 G 行直接冲突。

复核另指出：**冲突不止 G 行**。D4 的 **B2 行**写有 worker-only secret，同样与 Web 读取 Gemini
key 执行探针冲突。

ADR-007 D5 规定放宽任一层调用许可必须先修订 ADR-007，因此两行必须一并处理。相应的
**RI5 Amendment 已在 ADR-007 中完成并接受**（R1 修订 B2、R2 修订 G、R3 声明未放宽项）；
本行不再有待决项。

ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订已由项目负责人于 2026-09-14 成套接受；该接受不授予 RI3 PR 3E 与 RI2 的真实调用 GO。

## Web 产品修订（2026-09-20）

- 状态: Accepted
- 决策人: shixian66（项目负责人）
- 决策日期: 2026-09-20
- 批准出处: https://github.com/shixian66/xiaowei-agent/pull/59#issuecomment-5750387268

本修订按 Web 产品规格 §19.2 逐条收窄 RI5 的 R1/R2/R3。2026-09-14 已接受的 RI5 原文**保留为
当时事实**，不删除、不改写；下面三节是此后的现行口径。规格真源见
[Web 运维工作台总体设计](../superpowers/specs/2026-09-19-web-operations-console-identity-activation-design.md)。

### R1 窄替代：飞书 Admin 只读脱敏状态投影

本修订**取代 RI5 R1** 中"飞书 principal 即使持有 `ADMIN_ALL_SAFE_TASKS` 也不得读取配置状态"
这一句的绝对表述，且只取代这一句。

飞书认证的 `ADMIN` 获得 `VIEW_INTEGRATION_STATUS`，只能读取 ADR-007 同日修订所定义的脱敏
状态投影闭集。原始配置读取、配置保存/清除与 Provider 连接测试**仍只接受 `LOCAL_ADMIN`**；
本修订不新增配置 RBAC，配置面准入继续按 `IdentitySource` 判定。

### R2 明示替代：取消 loopback 先后硬门

本修订**取代 RI5 R2** 结尾"局域网发布只能由独立 override 打开，且首次强制改密必须在
loopback 阶段完成"中的先后硬门。项目负责人接受：受信局域网在首次强制改密完成前即可达。

**登录准入不以网络位置为条件。** 应用层从未实现登录 IP 白名单，本修订也不新增。
`WebMode.LAN_HTTP` 仍按 D2 与 RI5 R2 只接受 canonical loopback/RFC1918 IPv4 字面量 Host——
那是**明文 HTTP 模式的形态约束**，用于阻止明文暴露到公网，不是登录准入条件，本修订不放开它。
非本机对外部署继续使用 HTTPS 模式；公网明文 HTTP 不得被描述为安全部署。

固定的补偿控制：

- 部署文档对默认管理员凭据做醒目警示；
- 首次登录后立即强制改密；
- **改密前只允许改密和退出**（既有 `must_change_password` 路由闭集，已在 RI5 实现）；
- 正式 release / canary 必须提供**边缘限流**的配置与运行证据。

**风险与事实边界**：从服务启动到完成改密期间，同网段可用文档所载默认凭据登录，负责人已
接受该风险。此处不得写成"速率限制继续生效"——**应用层没有 HTTP rate limiter**，OAuth state
与 ActivationStore 的容量上限只是数据库无界增长保护，不冒充 HTTP 防滥用。CSRF、Session 绑定
与改密后 Session 轮换继续有效。

回滚方式：移除局域网 override、回到基础 Compose 的 loopback 发布，并撤销全部本地管理员
Session。回滚不需要回退数据库。

### R3 精确解冻：只放开 `web_oauth_login_contexts`

本修订**取代 RI5 R3** 中"该解除不包含 `web_oauth_states`"对登录域的绝对封锁，且只放开一张
新表：`web_oauth_login_contexts`。

- `state_digest` 是唯一 PK，并作为指向登录 state 的唯一 FK；
- return intent 是闭集，只允许 `WORKBENCH`、`SAFE_TASK_DETAIL(task_id)`、`ADMIN_CENTER`、
  `ACTIVATION_STATUS(request_id)` 四种；
- **不保存任意 URL 或 path**；深链由闭集 intent 在服务端重新构造；
- 登录 state 与其 context 同事务签发、同一次消费；缺失、多行或不匹配一律 fail-closed；
- **连接测试 state 不产生 context**，且继续不签发 Session、不解析身份目录、不绑定用户、
  不创建激活申请。

`web_oauth_states` 自身的列不变。登录与连接测试继续使用不同 digest domain
（`oauth-state:v1` 与 `oauth-conn-test:v1`）、同一备案 callback。除本表外，为 OAuth state
新增任何列或表仍须先修订本 ADR。

### 仅为后续阶段的契约，W0 不落表

激活申请 24 小时过期、全局 1024 `PENDING` 上限、同 scope/subject 单一有效申请、CAS 状态机、
不自动授予 Admin——这些是 W1b 的实现契约，**本修订不创建任何表、列或 migration**。

### 本修订不提供的证据

本修订是文档口径变更，没有对应的源码、迁移、运行、部署、canary 或用户验收证据。

## 后果

- 并发 Web 进程共享同一个数据库容量裁决，不会各自越过 state 上限。
- 已消费和已过期 state 会被物理清理，不长期占用额度或阻止同 digest 的安全再签发。
- provider、origin、身份、容量与限流边界各有单一责任，离线测试不会被误写成公网防护或真实兼容证据。
- 1024 全局额度只保证存储有界；边缘限流与监控证据缺失时，真实 OAuth 激活必须保持关闭。
- 登录在 task 创建前失败时没有 task/audit 行；排障只依赖脱敏、无外部正文的结构化日志。

## 备选方案与否决理由

- **只在应用进程加锁**：不能覆盖多进程或多实例，拒绝。
- **普通事务内 count 后 insert**：并发事务可同时看到旧计数并穿透上限，拒绝。
- **新增配额表或 migration**：固定 transaction advisory lock 已能串行化现有表操作，没有失败测试证明需要新 schema，拒绝。
- **让容量上限可由环境或请求配置**：扩大配置面并可被误调成无界，拒绝；只保留受信构造参数供小测试使用。
- **把 state 容量称为 IP 限流**：保护对象与证据都不相同，拒绝。
- **信任 forwarded header 自动发现 callback**：代理链未在应用内建立可信边界，存在 origin 注入风险，拒绝。

## 回滚与变更门

关闭 Web feature flag 并停止 Web 进程即可回滚；既有 session/state schema 不变，不删除已持久化事实。
若未来要修改默认容量、开放容量配置、改变 provider origin 或增加 endpoint、信任代理 header、改变身份字段、
迁移 state/session 真源、在登录期创建 task/audit 行，或由应用承担 HTTP 请求限流，必须先修订本 ADR。

RI5 修订接受后，以下任一变化同样必须先修订本 ADR：放开 `lan_http` 之外的协议/地址形态、把协议
放开扩散到 provider 授权或 token URL、为 OAuth state 新增列或表、让 `LOCAL_ADMIN` 之外的
principal 读写配置或发起连接测试、让连接测试创建 Task/Evidence 或进入数据面 `ToolGateway`、
让测试分支签发 Session，或新增第三个 Provider、第二个本地账号与任何配置 RBAC。

**2026-09-20 窄替代（见本文 Web 产品修订（2026-09-20））**：上列三项已被逐项窄替代，其替代
范围就是该修订写明的范围，不得外推：

- "让 `LOCAL_ADMIN` 之外的 principal 读配置状态"——在"飞书认证 `ADMIN` 读脱敏状态投影闭集"
  这一点上，已**由本修订 R1** 取代；同项中的写配置与发起连接测试继续完整命中本变更门。
- "为 OAuth state 新增列或表"——仅"新增 `web_oauth_login_contexts` 一张表"已**由本修订 R3**
  取代；其余新增列或表继续完整命中本变更门。
- "局域网发布必须在 loopback 首次强制改密之后"——已**由本修订 R2** 取代；`lan_http` 的
  loopback/RFC1918 Host 形态约束不在替代范围内，继续生效。

本节其余各项，以及"再次变更仍须先修订本 ADR"的要求，全部原样继续有效。

RI5 的功能回滚不需要回退数据库：关闭 Compose 中的 LAN 端口发布与相关 feature flag 并重建服务，
即回到 loopback + 默认关闭状态；本地管理员表与 Provider 状态表保留只读兼容。
