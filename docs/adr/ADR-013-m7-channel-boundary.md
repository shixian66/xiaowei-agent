# ADR-013：M7 Web 与飞书薄渠道边界

- 状态：Accepted
- 日期：2026-09-08
- 决策人：项目负责人
- 相关：[ARCHITECTURE.md](../../ARCHITECTURE.md) §3/§4/§5.9、[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-010](ADR-010-m5-durable-attempt-and-compose-boundary.md)、[M7 实施计划](../plans/M7-web-feishu-channels.md)

## 背景

M5 已提供内部 API、CLI、Worker 与 Compose，M6a/M6b 已形成三个使用同一确定性主链的只读
能力，但还没有面向终端用户的 Web 或飞书入口。M7 必须证明多渠道能够复用同一任务事实和
`RenderPayload`，同时不能让入口进程顺带获得 Runner、ToolGateway 或运维目标 adapter。

飞书入站可选 webhook 或官方 SDK 长连接；出站消息、OAuth 和成员查询又属于渠道基础设施调用，
与通过 ToolGateway 访问被管运维目标的调用语义不同。若不先固定边界，入口容易形成第二套业务
路由、任务状态或授权链。

## 决策

### D1 渠道只做协议、身份、访问授权与投影

Web 与飞书入口只校验协议和大小、把服务端认证结果转换为 `AuthenticatedPrincipal`、调用共享的
任务提交/读取服务，并投影 `TaskView` 与 `RenderPayload`。它们不得产生 capability 候选、编译
计划、生成 SQL/PromQL、执行工具、判断审批或改写任务终态。

渠道核心权限只有 `view_safe_task`、`submit_readonly_task` 与 `admin_all_safe_tasks`。运维、DBA、
值班、查看人和审批人是身份目录标签，不进入核心权限枚举；数据库真实结果 artifact 的预览和
导出不属于 M7。

### D2 进程隔离同时隔离执行权

内部 API、任务 Worker、飞书 listener、渠道投影 Worker 与 Web app 使用同一代码仓库和镜像，
但由不同进程、命令、端口和窄装配类型隔离。只有任务 Worker 可装配完整 `XiaoweiRuntime`、
Runner、ToolGateway 和运维目标 adapter；其他进程只装配后续 M7 引入的四依赖
`TaskViewRuntime` 及各自需要的渠道端口。

Web app 不注册内部 `/v1/tasks/*`，内部 API 不注册 `/app` 或 OAuth 路由。飞书 listener 不监听
入站端口，只在真实激活门通过后建立出站长连接。进程隔离是最小权限边界，不是微服务拆分。

### D3 TaskStore 与 ChannelStore 不复制事实

TaskStore 继续是 task ID、作用域、actor、状态、版本、提交和终态投影引用的唯一真源。
ChannelStore 只保存来源绑定与投影订阅，包括渠道引用、claim、fencing、退避和投递状态；它不保存
task owner、task status、task version、request text 或 `RenderPayload` 副本。渠道恢复必须按
task ID 回读 TaskStore winner。

浏览器 session 只持久化随机 cookie 的 digest、飞书 subject 引用和时效事实；tenant、environment、
actor 与权限每次由当前身份目录重新解析，不把旧权限快照当作授权依据。

### D4 飞书长连接是唯一入站传输

M7 选择官方 `lark-oapi` 长连接客户端，并把它放入独立同步 listener 进程。SDK 只允许由一个动态
加载叶子适配器访问，立即转换为本地严格 DTO；应用层只依赖本地 Protocol。SDK 版本和 wheel 被
锁定，连接 URL、token、ticket、响应体与用户原文不得进入日志。

Webhook 被拒绝，因为它会额外引入公网回调、签名验证、重放窗口和第二种入站运行模式，却不能
为当前企业内场景提供必要收益。高层 channel/agent SDK 被拒绝，因为其自带路由或策略层会复制
本项目 Runtime 与安全链。

### D5 渠道基础设施调用不经 ToolGateway

飞书消息发送、卡片更新、OAuth 与当前群成员查询不会修改被管运维目标，不属于 E1，也不经
ToolGateway。它们仍必须使用固定超时、有限重试、tenant/目标限流、结构化闭集错误、脱敏日志与
可审计的 task/trace/subscription 关联。

渠道调用失败只能改变渠道投影或 session 事实，不能反向改变 capability、计划、准入、TaskStore
状态或业务结果。M0–M7 对被管运维目标的 E1 禁令保持不变。

### D6 离线实现不授权真实激活

M7 PR 1–8 可以使用 fake/recording 和本地隔离基础设施离线实现。注册真实飞书应用、读取真实
secret reference、发起任何飞书网络请求、部署或 canary 仍需 M7 计划的独立真实渠道门和项目
负责人明确口令。离线测试、Compose 或卡片截图不得表述为真实渠道可用。

## 后果

- Web、飞书、内部 API 与 CLI 可以共享同一安全任务投影，不复制业务判断。
- 渠道进程即使被攻破，也不应因装配关系直接取得运维目标执行对象。
- ChannelStore 不能单独回答任务状态或 owner；每次投影和授权读取会增加一次 TaskStore 回读。
- 群详情实时成员校验选择权限新鲜度而不是可用性；飞书失败时统一 fail-closed。
- 飞书外部投递只能提供带 fencing 的 at-least-once 语义，不能承诺 exactly-once。
- 静态身份映射的权限撤销需要受控重启；热更新留给未来配置治理里程碑。

## 备选方案与否决理由

- **让每个渠道直接装配完整 Runtime**：扩大 Runner、Gateway 和目标 adapter 的权限面。
- **渠道自行按 capability 或状态生成文案**：形成第二套业务与终态投影真源。
- **ChannelStore 复制 owner/status/submission**：任务事实可能与 TaskStore 漂移，并制造授权旁路。
- **飞书渠道消息经 ToolGateway**：混淆运维目标执行与产品渠道基础设施，扩大 capability 工具面。
- **同时支持 webhook 与长连接**：增加公网入口和双传输维护面，没有当前需求承重。
- **使用高层自带 Agent 的渠道 SDK**：会引入并列的候选、策略或工具路由。

## 回滚与变更门

关闭渠道 feature flag 并停止 Web、listener 和渠道 worker 即可回滚；既有内部 API、任务 Worker、
TaskStore 与 Runtime 不变。默认不删除渠道表或改写已完成任务。

下列变化必须先修订本 ADR：渠道进程取得完整 Runtime/Runner/Gateway、ChannelStore 新增任务事实、
新增 webhook/其他入站传输、渠道调用改走 ToolGateway、扩展核心权限枚举、开放数据库真实结果
artifact，或放宽真实应用、凭据、网络、部署与 canary 的独立授权门。
