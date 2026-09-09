# M7 Web 与飞书薄渠道离线范围归档（2026-09-09）

> 本文件承载 M7 PR 1–8 离线实现范围验收后的历史归档。当前有效状态仍以项目根目录的
> `AGENT_HANDOFF.md` 为准；详细测试、变异与风险见
> [M7 离线范围验收报告](../M7-acceptance-report.md)。
>
> 文中的 SHA、run id 与测试数字是归档时已核对的历史事实，不随 `main` 后续前进而改变。

## 1. 归档结论

- 项目负责人于 2026-09-09 明确说“合并归档吧”，批准 M7 **离线实现范围**验收并授权归档。
- M7 PR 1–8 已全部审查并合入；最终代码/测试基线是
  `ba5ecfe5edffb408e20c4bb9cbf494bfbb035b82`。
- PR #28 的离线完成交接已以 squash commit
  `a5c88cbc285658f7f38355a9470bfdd725858ba5` 合入 `main`。
- M7 当前最强证据是 `tests`：GitHub 隔离 runner 的 PostgreSQL integration 与 Compose smoke
  已通过，Web/飞书共享 Runtime、TaskView、RenderPayload、状态与证据语义有离线闭环证据。
- 本次归档只关闭 PR 1–8 的离线实施任务，**不表示** `DEVELOPMENT_PLAN.md` 的 M7 完整退出标准
  已通过，也不解锁真实渠道、只读 V1 发布点或 M8。
- 没有注册真实飞书应用、读取真实凭据、连接真实飞书或运维目标、部署、canary 或取得产品用户
  验收；真实渠道激活继续受详细计划 §0.3.2 的独立硬门约束。

## 2. 归档范围

M7 离线范围完成了以下薄渠道能力：

- 版本化渠道、权限、TaskView、投影订阅与 Web session 契约，以及
  [ADR-013](../../adr/ADR-013-m7-channel-boundary.md)。
- 抽取无执行权的 `TaskViewRuntime`，供完整 Runtime、internal API、Web 与飞书投影复用。
- scoped TaskStore 读取、ChannelStore、渠道访问与提交服务；任务事实仍只属于 TaskStore。
- 默认关闭的飞书长连接 listener、卡片 projection worker 与单一 typed SDK seam。
- digest-only OAuth state/session、严格 Origin/CSRF/CSP/HSTS 与独立 Web app。
- 桌面运维任务工作台和窄屏只读详情 shell；外部文本只经安全文本节点渲染。
- 同一终态在 API、CLI、Web 与飞书中的状态、证据引用和限制一致性测试。
- 同镜像 Compose 进程闭集、默认关闭入口 smoke、安全 Eval 与变异反证。

M7 没有实现 Admin 配置中心、真实模型 API、审批/重跑、Jenkins、Dinky 或其他后续运维能力，
也没有开放任何 E1 操作。

## 3. 合并与审查链路

| PR | 作用 | 合并时 head（GitHub） | squash merge |
| --- | --- | --- | --- |
| [#20](https://github.com/shixian66/xiaowei-agent/pull/20) | 渠道契约与 ADR-013 | `76b2243cd8f0275167b643742e9f553519c32891` | `04c2fcda2ef39e247bdf336d6dcf16ec1bdb7d59` |
| [#21](https://github.com/shixian66/xiaowei-agent/pull/21) | 无执行权 TaskViewRuntime | `28130662e0812b9feebe594b230b232a4a67939e` | `1ec2b5c5edc7fb100e69649bb1463de19d768736` |
| [#22](https://github.com/shixian66/xiaowei-agent/pull/22) | scoped access 与 ChannelStore | `3e61ae5e8fe2b56e6712a1fc3ce5c9eba02abe5a` | `dd7d18f030fbe490d504483d5df30928f9b34758` |
| [#23](https://github.com/shixian66/xiaowei-agent/pull/23) | 飞书 listener 与 SDK seam | `7e109d166158dd1935a7f1a1355a31447e77d9ec` | `3487808edc3a98203dcd354a796f298ff6fa57cc` |
| [#24](https://github.com/shixian66/xiaowei-agent/pull/24) | 飞书卡片与 projection worker | `2f3d83e451ac980d5e9a44720a8787b970da30a3` | `2b66cf9d595ba0b36876636e602b1ecb3442b110` |
| [#25](https://github.com/shixian66/xiaowei-agent/pull/25) | Web OAuth 与 session | `ed6f4acf40edadfd3f503b4754a2c2d72d882fc6` | `6599bada8846c5338b89ade6fd16deeca581af47` |
| [#26](https://github.com/shixian66/xiaowei-agent/pull/26) | Web 运维任务工作台 | `11d90b81025c4804d7aba75aa03bf39bf3470661` | `0e7cb5c1e9db8ce34ed6f2a3e90f7374b16d2e6a` |
| [#27](https://github.com/shixian66/xiaowei-agent/pull/27) | 跨渠道一致性、Compose 与离线证据 | `c50d8201377588270e13fedc676b05f2a4433dd7` | `ba5ecfe5edffb408e20c4bb9cbf494bfbb035b82` |
| [#28](https://github.com/shixian66/xiaowei-agent/pull/28) | 离线范围完成交接 | `35b0d0f4da459700ed0f847083477bac3fe7a40c` | `a5c88cbc285658f7f38355a9470bfdd725858ba5` |

详细实施计划：[M7-web-feishu-channels.md](../../plans/M7-web-feishu-channels.md) V0.6。

## 4. 已验证

最终代码/测试工作树的四条规范门：

| 命令 | 结果 |
| --- | --- |
| `python -m pytest -q` | `2808 passed, 187 skipped, 5 warnings` |
| `python -m pytest -m security -q` | `1193 passed, 79 skipped, 1723 deselected, 5 warnings` |
| `ruff check .` | `All checks passed!` |
| `mypy src` | `Success: no issues found in 151 source files` |

PR #27 最终 run
[`34341819892`](https://github.com/shixian66/xiaowei-agent/actions/runs/34341819892)
与合入后 main run
[`34343986339`](https://github.com/shixian66/xiaowei-agent/actions/runs/34343986339)
均有 tests、integration、compose-smoke、security-gate、lint、types、deps-audit、secret-scan
八个成功 job。后者 integration 在隔离 PostgreSQL service 上实跑 `2995 passed`、0 skipped，
Compose 输出 `compose-smoke: passed`。

PR #28 run
[`34344702378`](https://github.com/shixian66/xiaowei-agent/actions/runs/34344702378)
同样八项全绿；integration 为 `2995 passed`、0 skipped，Compose 输出 `compose-smoke: passed`。

关键红灯与隔离变异覆盖：

- 渠道契约字段边界、HTTPS 详情 URL、倒序游标和动态导入闭集。
- internal API 进程加载模块精确闭集，以及仅导入装配根不加载完整执行 Runtime。
- worker scope、批量群绑定过滤、成员检查异常诊断和来源引用派生。
- 飞书 mention 一致性、身份/secret 文件、成员分页、callback 超时取消与单在途约束。
- projection fencing、live claim 续期、全部写回 loser、provider/数据库异常分域和字节预算。
- OAuth state 单次消费、session 轮换、重复 Cookie、Origin/CSRF、非 ASCII 头与路由隔离。
- Web shell XSS sink、只读详情范围、预览宽度配对与成功/错误轮询退避。
- 跨 scope、旧群成员、数据库原始行、provider 重试、陈旧 claim、驱动语料闭集与 Compose
  默认关闭入口。

## 5. 只读推理

- API、CLI、Web 与飞书均消费同一 TaskStore、TaskViewRuntime 和 RenderPayload；入口没有形成第二套
  Resolver、Planner、Policy、Guard、审批、执行或终态真源。
- 渠道绑定和投影订阅只记录投递事实；provider 重试、失败或乱序不能改写 TaskStore 中的任务终态。
- 三个渠道进程复用同一镜像且默认关闭，证明装配可共享，但不能推出真实 provider、网络或部署兼容。

## 6. 未覆盖

- 没有真实飞书 app、OAuth code exchange、成员接口、消息发送、长连接、真实凭据或网络调用。
- 没有部署、TLS/Ingress 现场证据、canary、回滚演练或产品用户验收。
- 1280/1440 桌面工作台和窄屏只读详情尚无真实浏览器视觉截图；HTML/CSS/JS 契约不等于视觉验收。
- 当前开发机没有 Docker 或 PostgreSQL 测试 DSN；对应运行证据来自 GitHub 隔离 runner。
- Admin 配置中心、真实模型 API、审批/重跑、Jenkins、Dinky 与其他运维能力不属于本次归档。

## 7. 残余风险与后续入口

- 飞书外部投递只有 at-least-once；provider 成功而数据库提交前崩溃仍可能产生重复消息。
- 锁定 SDK 形状与 fake port 不能证明真实租户的权限、限流、分页、延迟、token cache 或事件循环行为。
- 身份文件在装配时一次读取，权限变更依赖受控重启；真实撤权时延和配置审计尚未现场验证。
- 过期 OAuth state/session 的后台清理、公网限流、代理/Ingress 与 TLS 运维尚未实现或核对。
- 群详情每次轮询实时查成员，真实群规模与供应商限流下的成本尚未测量。
- Compose 只在一次 GitHub runner 版本上运行，不能外推到其他版本、长期运行或生产兼容。
- 真实渠道阶段只能在项目负责人另行明确发出“开始 M7 真实渠道验证”后进入；届时必须重新读取
  最新文档与 Git 状态，并逐项满足详细计划 §0.3.2。离线归档不自动开放该阶段。
- M6b 真实验证仍独立延期；M7 离线证据不能提升 M6b 或任何真实 capability 的证据等级。
