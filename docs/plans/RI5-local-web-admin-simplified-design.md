# RI5：本地 Web Admin 与第三方配置简化设计

## 状态与基线

这是对上一版 [M7c 设计](M7c-local-lan-web-config-redesign.md) 的修订，取代其实现口径；设计尚未批准进入实现计划。

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

这不是对现有 ADR 的静默例外。进入实现前必须修订并重新接受：

- **ADR-014**：把 Web 定义为受信的本地配置管理进程；允许显式 `lan_http` origin；HTTPS 与 HTTP 的 Cookie/安全头差异由配置决定；飞书 Secret 可由 Web 从本地私有配置文件读取并执行凭据测试；飞书 OAuth 变为可选 Web 登录插件，不再是 Web 启动条件。
- **ADR-015**：允许 Web 只为管理员明确点击的 Gemini 连接测试读取 Gemini Key；Worker 只消费 Gemini 配置执行已批准的模型调用；API 不接触 Gemini Key；Web 不把 Key 回传给浏览器、API、任务、日志或 Provider 测试结果。为保持单文件方案简单，Worker 的只读挂载可能技术上看到其他 Provider 字段，该信任扩大在本地 RI5 范围内明确接受。

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
```

- 容器监听 `0.0.0.0:8080`，Compose 发布到宿主机局域网。
- `lan_http` 只接受明确配置的局域网 HTTP origin；HTTPS 模式沿用现有受信 HTTPS origin 约束；协议和模式不能错配。
- OAuth callback、Origin 校验和 Cookie 决策都使用该固定 origin，不动态信任 `Host`、`Forwarded` 或请求 URL。
- 配置页面只读展示当前 origin 和完整 callback URL。
- 两种 Cookie 均使用 `HttpOnly`、`SameSite=Lax`、`Path=/`。
- HTTPS 使用 `Secure` 和 `__Host-` Cookie；HTTP 使用普通 Cookie 名称，不设置 `Secure`。
- 只有 HTTPS 模式设置 HSTS。
- 飞书平台是否接受具体局域网 HTTP callback，只能由飞书后台配置和真实 OAuth 回调测试确认；应用端只承诺兼容该模式。

`.env` 只保存部署启动参数，不保存 Gemini Key 或飞书 App Secret；局域网地址通过静态地址或 DHCP 保留保持稳定。

## 本地管理员契约

新增本地管理员持久化契约，但不做用户管理：

- 管理员表最多一行；数据库为空时初始化 `admin/admin`，并设置 `must_change_password=true`。
- 首次改密前，只允许登录、改密和退出接口。
- 改密使用事务更新密码哈希与 `must_change_password=false`，同时轮换当前 Session，并撤销所有旧的 `local_admin` Session。
- `web_sessions` 增加/明确认证来源，至少区分 `local_admin` 和 `feishu`。
- 本地管理员身份固定映射为 `tenant_id=dev-local`、`environment_id=dev`、`actor=admin`，权限使用当前闭集中的管理员权限：`VIEW_SAFE_TASK`、`SUBMIT_READONLY_TASK`、`ADMIN_ALL_SAFE_TASKS`。
- 身份来源闭集增加 `LOCAL_ADMIN`；飞书身份继续使用 `FEISHU`。
- 不实现找回密码、第二个账号、角色编辑或租户编辑。

## Secret 挂载与配置文件

第一版保留一个宿主机 Git-ignored 配置文件以保持实现简单，但明确接受其信任边界：

- `.config/integrations.json` 由 Web 读写，保存第三方配置和当前 `generation`。
- Web 对该文件读写挂载；Worker 和实际需要飞书配置的 Feishu 进程只读挂载；API 不挂载第三方配置。
- 该选择意味着挂载该文件的 Worker/Feishu 进程技术上可以读取另一 Provider 的 Secret，这是 RI5 单机本地信任边界内的明确取舍，不向公网或多租户场景推广。
- 宿主目录权限为 `0700`，配置文件权限为 `0600`。
- Secret 永不通过查询接口回显；页面只显示“已配置/未配置”。
- Web 保存使用临时文件、权限设置和原子替换；不保存历史版本。
- 如果后续需要缩小进程间信任范围，再单独拆分 Gemini/飞书 Secret 文件，不在本版扩大范围。

配置文件的最小形状：

```json
{
  "generation": 1,
  "gemini": {
    "enabled": true,
    "api_key": "...",
    "model": "..."
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
- 配置文件结构校验和本地字段校验不等于凭据有效；凭据真伪只由管理员点击测试确认。
- `service_config_state` 只表示服务启动时读取配置的回执，不表示服务当前存活，也不是服务注册中心：

```text
service_name       primary key
loaded_generation
load_status
loaded_at
```

- 服务只为自己实际启用且需要的 Provider 写入加载回执；未启用服务不会造成永久“待应用”。
- 配置缺失或某个 Provider 无效时，相关服务记录该 Provider 的闭集加载失败状态，其他服务和 Web 仍可运行。
- `healthz` 只表示进程存活。
- `readyz` 保留当前数据库可用、migration head 匹配和 composition assembled 等核心检查；不把 Gemini、飞书外部 Provider 纳入整体 readiness。

## Provider 测试结果

单独使用 `provider_test_state`，只保存每个测试项当前 generation 的最新结果：

```text
check_name         primary key
tested_generation
test_status        passed / failed
tested_at
error_code         nullable, closed set
error_message      nullable, local safe message
```

测试项固定为：

- `gemini_connection`
- `feishu_credentials`
- `feishu_oauth`

页面刷新不调用外部服务。只有管理员点击测试才发起真实请求。测试失败只持久化闭集 `error_code` 和本地安全提示，不保存 Provider 原始错误正文、原始响应、Secret 或 Token。

飞书 OAuth 只有成功完成 callback、消费有效 state 并建立对应 Session 后，才能写入 `passed`。

## 页面状态

按以下顺序计算 Provider 状态：

1. 缺少配置：`未配置`；
2. 实际需要该配置的服务尚未加载当前 generation：`待应用`；
3. 没有当前 generation 的测试结果：`待测试`；
4. 当前 generation 测试通过：`可用`；
5. 当前 generation 测试失败：`测试失败`。

## 宿主机重启与回滚边界

- Web 只保存配置，不自动重启，不挂载 `docker.sock`。
- 宿主机统一执行 Compose 重启/重建脚本；页面等待 `/healthz`、`/readyz` 恢复后重新读取服务加载回执。
- 第一版不做自动回滚；配置校验失败时不替换当前有效文件。
- Provider 连接测试不改变服务 readiness，也不自动改写配置。

## 进入实现计划前的门

必须先完成：

1. 本设计复审通过；
2. ADR-014、ADR-015 修订并重新接受；
3. 明确 RI3 PR 3E 与 RI2 真实连接 GO 仍是独立现场门；
4. 从最新 `main` 创建实现分支；
5. 再单独编写实现计划。
