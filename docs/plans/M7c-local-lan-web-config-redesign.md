# M7c：局域网 Web 登录与第三方配置闭环设计（已取代）

> 本文已被 [RI5 本地 Web Admin 简化设计](RI5-local-web-admin-simplified-design.md) 取代，不再作为实现依据。保留本文仅用于记录上一版设计审查结果。

## 状态

设计已确认，尚未实现。

本文是当前项目 `docs/plans/` 下的设计基线，不代表 Web、用户名密码登录、配置保存或真实飞书/Gemini 测试已经存在。实现前仍需满足当前 M7 的实现门，并取得明确的实现开始指令。

## 目标

在本机 Docker Compose、局域网访问范围内，形成最小可用闭环：

`管理员登录 → Web 配置 Gemini/飞书 → 宿主机重启 Compose → 检查配置加载 → 管理员手动测试 Provider`

第一版只面向单管理员和单机 Compose，不建设通用配置中心。

## 登录设计

- 默认单管理员账号为 `admin/admin`。
- 首次登录必须修改密码；修改前不能进入配置和业务页面。
- 密码修改后，初始密码立即失效，数据库只保存密码哈希。
- 保留飞书 OAuth 作为可选登录方式，不再要求它作为唯一入口。
- 支持两种 Web 模式：
  - HTTPS：使用 `__Host-` Cookie。
  - `lan_http`：使用普通 Cookie 名称，供局域网 HTTP 体验。
- Cookie 的名称和 `Secure` 属性由配置的 `public origin` 自动决定。
- OAuth 回调地址由固定配置生成，不信任动态 `Host`。

## Compose 与访问边界

默认配置：

```text
XIAOWEI_WEB_MODE=lan_http
XIAOWEI_WEB_PUBLIC_ORIGIN=http://192.168.1.20:8080
```

- 容器内部监听 `0.0.0.0:8080`。
- Compose 将端口发布到宿主机局域网。
- `.env` 只保存部署启动参数，不保存 Gemini Key 或飞书 App Secret。
- Web 不挂载 `docker.sock`，不直接控制 Docker。
- 配置页面只读展示当前 origin 和完整 OAuth 回调地址。
- 局域网 IP 通过静态地址或 DHCP 保留保持稳定。

`lan_http` 只承诺应用端兼容；飞书平台是否接受具体 HTTP 回调地址，以飞书后台配置和真实 OAuth 回调测试为准。

## 配置文件

- 第三方集成配置保存于宿主机私有配置文件，建议固定为被 Git 忽略的 `.config/integrations.json`。
- Web 容器对该目录读写挂载。
- API、Worker 等配置消费者只读挂载。
- 文件只承载 Gemini、飞书等第三方集成配置，不承载管理员账号、Session、测试结果或配置历史。
- 保存使用临时文件写入后原子替换正式文件。
- 配置保存前校验结构和 Provider 配置；无效 Provider 不阻塞管理员登录和配置页面，只标记该 Provider 不可用。
- 保存后不自动重启。宿主机统一执行 Compose 重启脚本，服务重新读取配置后生效。

## 状态与数据库边界

### `service_config_state`

只保存各服务当前已加载配置的状态，不是服务注册中心，也不是通用运行状态表：

```text
service_name       primary key
loaded_generation
load_status
loaded_at
```

每个服务启动并成功读取配置后 UPSERT 当前值。第一版不做心跳、多实例和历史状态。

### `provider_test_state`

只保存每个测试项的最新结果，不保存测试历史：

```text
check_name         primary key
tested_generation
test_status        passed / failed
tested_at
error_code         nullable
error_message      nullable, redacted
```

测试项为：

- `gemini_connection`
- `feishu_credentials`
- `feishu_oauth`

不保存原始异常、Provider 响应或任何凭据。飞书 OAuth 只有成功完成回调后才能写入 `passed`。

## 页面状态规则

Provider 状态按以下顺序计算：

1. 缺少配置：`未配置`
2. 服务尚未加载当前 generation：`待应用`
3. 没有当前 generation 的测试结果：`待测试`
4. 当前 generation 测试通过：`可用`
5. 当前 generation 测试失败：`测试失败`

页面刷新不自动调用外部 Provider。只有管理员明确点击测试时，才执行真实连接测试。

## 健康检查

- `/healthz` 只表示进程存活。
- `/readyz` 只检查 PostgreSQL 等核心依赖。
- Gemini、飞书等外部 Provider 不纳入整体 readiness。
- Provider 状态只在配置页展示，不改变服务健康检查语义。

## 第一版明确不做

- 多用户、角色和权限管理。
- 通用配置中心、配置历史和发布审批。
- 热加载、自动回滚和自动重启。
- Web 控制 Docker 或挂载 `docker.sock`。
- 多实例、心跳、服务注册中心。
- 生产公网部署和公网安全边界。
