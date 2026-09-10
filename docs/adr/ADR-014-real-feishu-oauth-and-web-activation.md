# ADR-014：真实飞书 OAuth 与 Web 激活安全契约

- 状态：Accepted
- 日期：2026-09-10
- 决策人：项目负责人
- 相关：[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-013](ADR-013-m7-channel-boundary.md)、[真实接入总体设计](../superpowers/specs/2026-09-10-real-integrations-design.md)

## 背景

M7 已实现默认关闭的 fake OAuth、digest-only state/session 和 Web 认证边界，但真实 provider
adapter 与 composition root 尚未实现。RI1 只建立默认关闭、可离线验证的代码门；它不注册真实
应用、不读取真实 secret、不发起网络调用，也不取得部署、canary 或产品用户验收许可。

真实 OAuth 会新增三个必须分开治理的风险：provider endpoint 与 secret 的供应链边界、一次性
state 的并发容量边界，以及公网 SSO 入口的请求速率边界。state 表的容量上限不能冒充按 IP 的
HTTP 限流，provider 调用结果也不能把外部正文带回页面或日志。

## 决策

### D1 Provider 与身份契约保持闭集

飞书授权和 token endpoint 是代码内的官方固定闭集，不接受环境变量、请求、Host、forwarded
header、用户或模型提供任意 URL。App secret 只从受信 composition root 取得的绝对只读文件读取；
不接受明文环境变量，不进入数据库、日志、trace、异常或页面。

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
契约/集成测试中覆盖；默认值固定为 1024，不成为环境、请求、模型或用户字段，且 bool、零和负数
都必须拒绝。

每次签发必须在同一写事务中按固定顺序执行：取得不含用户输入的 transaction advisory lock；在锁
后读取时钟；物理删除已消费或已过期 state；统计全局未完成行；未达上限才插入新 digest。达到上限
时清理事务仍正常提交，事务退出后只抛出闭集 `OAuthStateCapacityError`。固定锁关闭多进程
`count → insert` 竞态；单进程 Python lock 不能替代它。fake 在共享 `asyncio.Lock` 下执行相同顺序。
清理后的 digest 不再碰撞；仍存活的 digest 碰撞保持既有冲突语义。

容量拒绝只使用不敏感常量文本，并由 `WebAuthService.start_login()` 映射为现有
`WebOAuthUnavailableError`，HTTP 路由继续返回 503。异常、响应和日志不得包含 state、cookie、
digest、数据库异常正文或异常链。

### D4 容量保护、provider 追踪与 HTTP 限流是三件事

state 行清理和 1024 容量上限只限制数据库中的全局未完成 state，解决无界增长和最小应用级滥用
保护；它不是按 IP、用户或租户的请求速率限制。

未来真实 provider 调用必须使用受信 trace 关联和闭集 outcome，原始 provider 正文不能成为控制
信号或用户可见错误。没有 task ID 的登录流程不伪造 task/audit 行；无任务 trace 仍走既有结构化
日志边界。

实际 HTTP 请求速率限制由已有 HTTPS SSO 边缘入口承担。RI2/RI6 激活门必须记录该设施的真实配置
和运行证据；没有证据不得宣称已具备按 IP 限流，也不得用 state 容量测试代替。

### D5 RI1 仍无真实调用许可

RI1 只允许实现默认关闭的 adapter/composition root 与离线 fake/recording、隔离 PostgreSQL 测试。
真实应用、secret、飞书网络调用、公开部署和 canary 仍须 ADR-007 F 层与 RI2 的独立现场 GO。
ADR-007 H 层生产只读授权尚未单独签认；生产连接与任何 E1/生产写均未授权。

## 后果

- 并发 Web 进程共享同一个数据库容量裁决，不会各自越过 state 上限。
- 已消费和已过期 state 会被物理清理，不长期占用额度或阻止同 digest 的安全再签发。
- provider、origin、身份、容量与限流边界各有单一责任，离线测试不会被误写成公网防护或真实兼容证据。
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
若未来要修改默认容量、开放容量配置、增加 provider endpoint、信任代理 header、改变身份字段、
迁移 state/session 真源、在登录期创建 task/audit 行，或由应用承担 HTTP 请求限流，必须先修订本 ADR。
