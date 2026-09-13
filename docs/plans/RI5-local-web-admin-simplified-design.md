# RI5：本地 Web Admin 与第三方配置简化设计

## 状态与基线

这是对上一版 M7c 设计的修订，取代其实现口径；设计尚未批准进入实现计划。
本文同时明确取代当前 `main` 中的旧 RI5 实现计划
[`docs/superpowers/plans/2026-09-10-web-admin-config-center.md`](../superpowers/plans/2026-09-10-web-admin-config-center.md)。

设计基于当前 `main` 的里程碑状态：M7 离线范围已归档，RI1 Web/飞书 OAuth 代码已合入，RI3 PR 3A–3D 已合入，PR 3E 真实 Gemini 测试环境验证尚未开始，RI5 是后续的最小 Web Admin 里程碑。本文不把这些源码或离线测试事实升级为真实渠道、真实 Provider、部署或用户验收证据。

## 目标与边界

目标是完成本机 Docker Compose、局域网访问范围内的最小管理闭环：

`本地管理员登录 → Web 保存第三方配置 → 宿主机重启 Compose → 服务加载回执 → 管理员手动测试`

第一版只支持：

- 一个 Docker Compose 实例；
- 一个本地管理员；
- Gemini、飞书两个闭集集成；
- `lan_http` 和 HTTPS 两种显式 Web 模式。

第一版不做多用户、RBAC、找回密码、通用 Provider registry、配置历史、热加载、自动回滚、自动重启、Docker 控制、心跳、多实例和公网部署。

## 必须先修订的 ADR

这不是对现有 ADR 的静默例外。进入实现前必须修订并重新接受下列**四份**文档；
ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订必须成套复核并重新接受；任一份未被接受，RI5 都不得开工。

- **ADR-007**（真实调用许可真源）：修订 D4 表的 **B2** 与 **G** 两行。B2——任务模型调用仍
  worker-only，Web 不得取得模型端口；仅开窄例外允许 Web 为配置管理与固定最小 synthetic 输入的
  `gemini_connection` 探针读取 Key。G——取消独立 configuration-test worker、dispatch lane 与
  ADR-016，不纳入 StarRocks，M7「只有 task worker 装配完整执行 Runtime」口径不变。真实调用仍
  分别受 PR 3E / RI2 现场 GO 限制。
- **总体 spec**（[真实接入总体设计](../superpowers/specs/2026-09-10-real-integrations-design.md)）：
  §2 前提、§5 模块边界、§6.5 Admin 数据流、§7.2 配置、§8 权限、§11 依赖与硬门、§12 回滚、
  §13 索引与 §15 风险表的 RI5 段落，以及 §2/§6.1 中「只有 RI6 可发布宿主 IP、OAuth 只走 HTTPS」
  的旧口径。

- **ADR-014**：
  - 把 Web 定义为受信的本地配置管理进程；飞书 OAuth 变为可选登录插件，不再是 Web 启动条件；
  - 将现有 `web_detail_base_url`/`XIAOWEI_WEB_DETAIL_BASE_URL` **重命名并迁移**为唯一的 `web_public_origin`/`XIAOWEI_WEB_PUBLIC_ORIGIN`，不并存第二个 Origin 真源；Web Session、OAuth callback、Origin 校验和飞书卡片深链统一消费该值；
  - 仅在显式 `lan_http` 模式放开 canonical loopback 或 RFC1918 IPv4 字面量、HTTP 协议和显式端口；HTTPS 模式继续沿用现有受信 HTTPS hostname 约束；该放开只作用于本方 public origin，飞书授权/token URL 的 HTTPS 约束不变；
  - 解除 ADR-014 D3 的“无表、列、索引或 migration”限制，允许本地管理员、Provider 当前状态和 `web_sessions` 认证来源/public-origin 绑定所需的最小 schema 变更；该解除**不包含** `web_oauth_states`，OAuth 连通测试不得为此新增列或新表；
  - 允许 Web 读取本地第三方配置，并在管理员明确点击且对应真实测试开关已打开时执行连接测试。
- **ADR-015**：
  - `.config/integrations.json` 取代现有 Gemini/飞书明文来源、Compose secret 类型和容器目标路径；旧 Gemini Key、飞书 App Secret 文件不与它并存；
  - Worker 只消费 Gemini 配置执行已批准的模型调用；实际需要飞书配置的进程只消费飞书配置；API 不挂载第三方配置；
  - Web 只为配置管理和管理员明确点击的连接测试读取 Secret，不把 Secret 回传给浏览器、API、任务、日志或测试结果；
  - 为保持单文件方案简单，挂载该文件的 Worker/飞书进程技术上可能看到另一 Provider 字段；该信任扩大只在本地 RI5 范围内接受；
  - Provider 连接测试是窄化的管理面连通性操作，不创建 Task、Evidence 或 capability，也不经过业务 `ToolGateway`。该边界还必须同步写入 `ARCHITECTURE.md`，不得由实现自行推断。

ADR-015 中的 Gemini provider、model、API version、endpoint 和 SDK 仍是固定常量。RI5 不把 `model`、endpoint 或 API version 作为可编辑配置；页面只读展示当前固定模型。

ADR 修订只改变本地 RI5 配置管理边界，不授予 PR 3E 真实 Gemini 网络调用许可，也不授予 RI2 真实飞书调用许可。真实调用仍分别需要：

- Gemini：RI3 PR 3E 的独立 test-env GO；
- 飞书凭据/OAuth：RI2 的现场 GO。

## Web 装配边界

Web 启动只依赖：

- PostgreSQL 可用；
- migration head 与当前代码匹配；
- 本地管理员认证存储可用；
- 明确配置且精确匹配的 `public origin`。

Gemini、飞书配置缺失、格式无效或 Provider 暂不可用，都不能阻止 Web 启动、管理员登录和配置页面访问。

装配规则必须改为：

- `web_app_enabled` 不再要求等于 `feishu_oauth_enabled`；
- 本地密码登录始终可用；
- 飞书 OAuth 只有配置完整且插件可装配时才显示/启用；
- 飞书 OAuth 装配失败只标记 `feishu_oauth` 不可用，不使 Web 进程退出；
- Gemini 只作为配置项和显式测试项，不参与 Web 的启动和 readiness。

## Web 模式、Origin 与 Cookie

部署启动参数为：

```text
XIAOWEI_WEB_MODE=lan_http
XIAOWEI_WEB_PUBLIC_ORIGIN=http://192.168.1.20:8080
XIAOWEI_GEMINI_REAL_TEST_ENABLED=false
XIAOWEI_FEISHU_REAL_TEST_ENABLED=false
```

- `XIAOWEI_WEB_PUBLIC_ORIGIN` 取代现有 `XIAOWEI_WEB_DETAIL_BASE_URL`；迁移完成后旧字段不再读取，避免 Session、OAuth callback 与飞书卡片深链出现两个 Origin 真源。
- 容器始终监听 `0.0.0.0:8080`；是否仅发布到 loopback 或发布到局域网，由 Compose 的宿主端口绑定决定。
- `lan_http` 只接受明确配置的 `http` Origin：canonical loopback 或 RFC1918 IPv4 字面量，并且必须带显式端口；HTTPS 模式沿用现有受信 HTTPS hostname 约束。协议和模式不能错配。
- OAuth callback、Origin 校验和 Cookie 决策都使用该固定 origin，不动态信任 `Host`、`Forwarded` 或请求 URL。
- 配置页面只读展示当前 origin 和完整 callback URL。
- 两种 Cookie 均使用 `HttpOnly`、`SameSite=Lax`、`Path=/`。
- HTTPS 使用 `Secure` 和 `__Host-` Cookie；HTTP 使用普通 Cookie 名称，不设置 `Secure`。
- 只有 HTTPS 模式设置 HSTS。
- RI5 只新增 `lan_http` 支持，不建设 TLS、Ingress 或证书管理；现有 HTTPS 行为保留并做回归验证。
- 协议放开必须是外科手术式的。当前代码有两道彼此独立的 HTTPS 闸门：`config.py` 的 `HttpsOrigin`/非 IP 主机名校验，以及 `interfaces/web_auth.py` 的 `_split_https_url()`。后者同时被本方 public origin 校验（`_public_origin_is_safe()`）和**飞书授权 URL 校验**（`_authorization_url_is_safe()`）复用，因此实现必须把它拆成两个 helper：只有 public origin 那一侧按模式放开 `http`，provider 授权/token URL 永远保持 HTTPS-only。禁止直接放宽共用 helper。
- 该拆分必须有承重测试：`lan_http` 模式下，一个 `http` 的飞书授权 URL 仍须被拒绝；撤掉拆分后该测试必须变红。
- 修改 Web 模式或 public origin 后必须重启 Web；Session 在创建时保存 canonical public origin digest，认证时必须与当前值匹配，因此旧 Session 不再被接受，用户需要重新登录。登录与退出路径清理两种已知 Cookie 名称，不做跨模式 Session 迁移。
- 飞书平台是否接受具体局域网 HTTP callback，只能由飞书后台配置和真实 OAuth 回调测试确认；应用端只承诺兼容该模式。
- 两个真实测试开关彼此独立且默认关闭：Gemini 开关只控制 `gemini_connection`，飞书开关控制 `feishu_credentials` 与 `feishu_oauth`。开关关闭时页面禁用按钮，接口也必须在本地返回闭集 `REAL_TEST_DISABLED`，不得发起外部网络请求。

`.env` 只保存部署启动参数，不保存 Gemini Key 或飞书 App Secret；局域网地址通过静态地址或 DHCP 保留保持稳定。

启用规则只有两层，不再增加优先级系统：

- `.env`/Compose 的现有进程开关决定对应功能是否装配，默认均为关闭；
- JSON 中 `gemini.enabled`、`feishu.enabled` 决定已装配功能是否消费对应配置；
- 最终启用条件是两者同时为真，JSON 不能反向启动 Compose 中未装配的 Worker、飞书监听器、渠道 Worker 或 OAuth；
- Web 真实测试还必须同时满足对应 `*_REAL_TEST_ENABLED=true`；
- 回滚时关闭相应 Compose 开关并重建相关服务；`lan_http` 回滚为 loopback 发布或现有 HTTPS 配置。

## 首次启动保护

保留已确认的初始账号 `admin/admin`，但它不能直接暴露到局域网。首启流程固定为：

1. 基础 Compose 只发布 `127.0.0.1:8080`，public origin 使用 `http://127.0.0.1:8080`；
2. 管理员在宿主机浏览器登录并完成强制改密；
3. 修改 `.env` 中的 public origin，启用单独的 LAN 端口发布 override，然后由宿主机脚本重建 Web；
4. 旧 Cookie 失效，管理员使用新密码从局域网地址重新登录。

首启保护只由 Compose 端口绑定承重，不在应用内增加来源 IP 判断、安装令牌或随机口令机制。`lan_http` 的 Cookie 会以明文 HTTP 传输，只用于受信局域网体验；正式部署仍走 RI6 的 HTTPS 边界。

**残余风险**：上述顺序由 runbook 承重，应用内没有强制。若部署者在第一次改密前就套用 LAN 端口发布 override，`admin/admin` 会在该窗口内暴露给同网段。README/runbook 必须把“先 loopback 改密、后开放局域网”写成不可跳过的步骤，并说明跳步后的补救是改密后重建 Web 并撤销全部 `local_admin` Session。

## 本地管理员契约

新增本地管理员持久化契约，但不做用户管理：

- 管理员表最多一行；数据库为空时初始化 `admin/admin`，并设置 `must_change_password=true`。
- migration 只创建 schema；Web 在完成 migration-head 检查后启动时幂等 seed，只有管理员行不存在时才 `INSERT ... ON CONFLICT DO NOTHING`，不由 migration 写入口令或口令哈希。
- 首次改密前，只允许登录、改密和退出接口。
- 改密使用事务更新密码哈希与 `must_change_password=false`，同时轮换当前 Session，并撤销所有旧的 `local_admin` Session。
- 口令使用标准库 `hashlib.scrypt`：随机 16-byte salt，固定 `n=2**14`、`r=8`、`p=1`、`dklen=32`，保存带版本和参数的哈希封装；校验使用 `hmac.compare_digest`。不新增认证依赖。
- `web_sessions` 增加/明确认证来源和 canonical public origin digest；认证来源至少区分 `local_admin` 和 `feishu`，Session 只在创建它的 public origin 下有效。
- 本地管理员身份固定映射为 `tenant_id=dev-local`、`environment_id=dev`、`actor=admin`，权限使用当前闭集中的管理员权限：`VIEW_SAFE_TASK`、`SUBMIT_READONLY_TASK`、`ADMIN_ALL_SAFE_TASKS`。
- 身份来源闭集增加 `LOCAL_ADMIN`；飞书身份继续使用 `FEISHU`。
- 配置读取、保存和 Provider 测试接口只接受 `IdentitySource.LOCAL_ADMIN`；飞书 principal 即使拥有 `ADMIN_ALL_SAFE_TASKS` 也不能读取配置状态、修改凭据或启动连接测试。本版不新增配置 RBAC 权限。
- 不实现找回密码、第二个账号、角色编辑或租户编辑。

## Secret 挂载与配置文件

第一版保留一个宿主机 Git-ignored 配置文件以保持实现简单，但明确接受其信任边界：

- `.config/integrations.json` 是 Gemini Key、飞书 App ID/Secret 和当前 `generation` 的唯一明文真源。
- 它**取代** `.secrets/gemini_api_key`、`.secrets/feishu_app_secret` 及对应 Compose secrets/container paths；实现迁移时删除这两条 Provider 旧读取路径，不提供双读、回退或优先级兼容。
- 新增一个面向严格 JSON schema 的 integration config loader；现有只支持单行 Secret 的 `read_secret_file()` 不读取该 JSON，并继续只服务 PostgreSQL、StarRocks 等其他既有 file secret。
- 容器内目标路径固定为 `/run/xiaowei-config/integrations.json`。Web 读写挂载整个宿主机 `.config/` 目录；Worker 和实际需要飞书配置的 Feishu 进程只读挂载该目录；API 不挂载第三方配置。
- 临时文件与正式文件必须位于同一个 `.config/` 目录中，再通过原子替换更新正式文件；不能把单个文件直接作为 bind-mount 目标后调用 `os.replace()`。
- Dockerfile 将非 root `xiaowei` 用户固定为数值 UID/GID `10001:10001`，避免镜像重建后漂移。
- 宿主机启动脚本先创建目录，再以镜像内 `xiaowei` 用户执行“创建临时文件 → 权限设置 → 原子替换 → 重新读取”的 Compose 预检；预检不输出文件内容，失败时不启动 Web。
- 首版支持 Linux Docker Engine 与 macOS Docker Desktop：Linux 目录属主设为 `10001:10001`；macOS 目录由宿主部署用户持有，由 Docker Desktop 映射，是否可用以同一容器内预检为准。不假设两种平台的 UID 语义相同。
- 该选择意味着挂载该文件的 Worker/Feishu 进程技术上可以读取另一 Provider 的 Secret，这是 RI5 单机本地信任边界内的明确取舍，不向公网或多租户场景推广。
- 宿主目录权限为 `0700`，配置文件权限为 `0600`。
- Secret 永不通过查询接口回显；页面只显示“已配置/未配置”。
- 保存请求未携带某个 Secret 字段时保留原值，携带新值时替换；清除凭据使用显式“清除配置”动作，不能用空字符串暗示。这样页面无需把现有 Secret 放入 HTML、DOM 或 API 响应。
- Web 保存使用临时文件、权限设置和原子替换；一个 Web 进程内串行保存，不支持多 Web 写实例，也不保存历史版本。
- 如果后续需要缩小进程间信任范围，再单独拆分 Gemini/飞书 Secret 文件，不在本版扩大范围。

配置文件的最小形状：

```json
{
  "generation": 1,
  "gemini": {
    "enabled": true,
    "api_key": "..."
  },
  "feishu": {
    "enabled": true,
    "app_id": "...",
    "app_secret": "..."
  }
}
```

示例只说明结构；真实 Secret 不进入代码、文档、测试夹具或提交记录。

## generation 与服务加载回执

- `generation` 是配置文件顶层的正整数。
- 读取当前值后，每次成功的配置原子替换都写入 `previous + 1`；首次有效配置从 `1` 开始。
- 单 Web 写进程串行执行“读取当前 generation → 校验 → 原子替换”，本版不支持多写实例。
- 本版有意保留**全局 generation**，不保存 Secret 子树的指纹：一次保存、一次 Compose 重启和一次状态收敛是同一个本地操作单元。修改任一 Provider 会使两个 Provider 的旧加载/测试结果一起过期，这是单管理员、双 Provider 范围内接受的简单取舍。
- 配置文件结构校验和本地字段校验不等于凭据有效；凭据真伪只由管理员点击测试确认。
- `service_config_state` 只表示服务启动时读取配置的回执，不表示服务当前存活，也不是服务注册中心：

```text
service_name
provider
loaded_generation
load_status          loaded / invalid
loaded_at

primary key (service_name, provider)
```

- 保留该表是为了满足“配置已保存”和“服务已加载”必须可区分的产品要求；`healthz`/`readyz` 无法回答服务加载的是哪个 generation，因此不能用健康检查替代它。
- 服务只为自己实际启用且需要的 Provider 写入加载回执；未启用服务不会造成永久“待应用”。
- `service_name` 与 `provider` 联合标识一条加载回执；一个 Web 服务可以分别报告 Gemini 和飞书的加载状态。
- 配置缺失或某个 Provider 无效时，相关服务记录该 Provider 的闭集加载失败状态，其他服务和 Web 仍可运行。
- `healthz` 只表示进程存活。
- `readyz` 保留当前数据库可用、migration head 匹配和 composition assembled 等核心检查；不把 Gemini、飞书外部 Provider 纳入整体 readiness。
- 本地管理员表与 Session 使用同一个 PostgreSQL 和 migration head，不增加第二个 readiness 探针；Web 启动 seed 失败时 composition 不成立，Web 不进入 ready。

## Provider 测试结果

单独使用 `provider_test_state`，只保存每个测试项当前 generation 的最新结果：

```text
check_name         primary key
tested_generation
test_status        passed / failed
tested_at
duration_ms        non-negative, bounded integer
error_code         nullable, closed set
error_message      nullable, local safe message
```

测试项固定为：

- `gemini_connection`
- `feishu_credentials`
- `feishu_oauth`

页面刷新不调用外部服务。只有管理员点击测试才发起真实请求。测试失败只持久化闭集 `error_code` 和本地安全提示，不保存 Provider 原始错误正文、原始响应、Secret 或 Token。

- `gemini_connection` 使用固定模型和固定的最小 synthetic 输入，只验证一次 Developer API 请求能否成功；不创建任务、不读取或保存对话、不保存模型响应正文，只记录状态、脱敏原因和耗时。
- `feishu_credentials` 只验证 App ID/Secret 能否取得应用凭证；不发送消息、不读取或操作群聊，不保存 Token。
- `feishu_oauth` 是独立的 callback 连通性测试，不等同于飞书登录验收：
  - 只有当 OAuth 插件已在 Compose 中装配（`feishu_oauth_enabled=true`，callback 路由已注册）、且当前 generation 的 `feishu_credentials=passed` 之后，才能开始；
  - 启动接口是状态变更 POST，必须同时通过 `LOCAL_ADMIN` 认证与既有的 Origin + CSRF token 检查；
  - 测试 state 与登录 state 使用**不同的 digest domain 常量**（登录沿用 `oauth-state:v1`，测试另取一个），落在互不相交的摘要命名空间，因此一类 state 不可能被另一条路径消费。该隔离不新增 `web_oauth_states` 的列，也不新增表；
  - **digest domain 只做路径隔离，不做上下文绑定**。现有 state 行只有 digest 与三个时间字段，承载不了发起者身份或配置代次，因此本版**不承诺**把测试 state 绑定到发起 Session 或某个 generation；所需上下文由下面两条承担。若将来确需在 state 行内精确绑定，必须先修订 ADR-014 放开 state schema，不得由实现自行加列；
  - callback 按 state 的 digest domain 分流；命中测试域时走测试分支，并且必须验证请求仍持有**当前有效的 `LOCAL_ADMIN` Session**，否则拒绝且不写入任何结果。测试分支**绝不签发、替换或延长任何 Session**，也不设置任何 Cookie；
  - code exchange 时读取**当时的当前配置**，并把**当时的当前 generation** 写入测试结果行，因此结果永远标注它实际验证过的那一代；期间若配置已改写，新代次自然回到“待测试”，旧结果不会被冒充为新配置的证据；
  - 测试不创建或替换本地管理员 Session，不要求 `feishu_identity_file` 映射，也不证明该账号已经获准使用正式飞书登录；正式身份映射与飞书登录验收仍属于 RI2。

以上三个测试都是管理面连接探针，不进入 Task、TaskSubmission、Evidence、capability 或业务 `ToolGateway`。Gemini 与飞书测试开关仍独立、默认关闭；没有对应现场 GO 时，页面和接口都只能返回本地禁用状态，外部调用数必须为零。

## 页面状态

配置页分别展示 Gemini、飞书凭据和飞书 OAuth；Gemini 使用 `gemini_connection`，飞书凭据使用 `feishu_credentials`，OAuth 使用 `feishu_oauth`，不把三项折叠成一个模糊总状态。每一项按以下顺序计算：

1. Provider 已停用或缺少必填配置：`未配置`；
2. 实际需要该配置的服务尚未加载当前 generation：`待应用`；
3. 没有当前 generation 的测试结果：`待测试`；
4. 当前 generation 测试通过：`可用`；
5. 当前 generation 测试失败：`测试失败`。

保存产生新 generation 后，旧 generation 的加载回执和测试结果只视为过期，不删除、不复制为当前状态；页面因此先显示“待应用”，服务重启加载后再显示“待测试”。

## 宿主机重启与回滚边界

- Web 只保存配置，不自动重启，不挂载 `docker.sock`。
- 宿主机统一执行 Compose 重启/重建脚本；页面等待 `/healthz`、`/readyz` 恢复后重新读取服务加载回执。
- 第一版不做自动回滚；配置校验失败时不替换当前有效文件。
- Provider 连接测试不改变服务 readiness，也不自动改写配置。

## 进入实现计划前的门

必须先完成：

1. 本设计复审通过；
2. ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订必须成套复核并重新接受；任一份未被接受，RI5 都不得开工。
3. 明确 RI3 PR 3E 与 RI2 真实连接 GO 仍是独立现场门；
4. 从最新 `main` 创建实现分支；
5. 再单独编写实现计划。
