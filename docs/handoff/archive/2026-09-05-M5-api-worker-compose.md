# M5 API / CLI / Worker / Compose 归档（2026-09-05）

> 本文件同时承载 M5 验收报告与验收通过后的历史归档。当前有效状态仍以项目根目录的
> `AGENT_HANDOFF.md` 为准。
>
> 文中的 SHA、run id 与测试数字是归档时已经核对的历史事实，不随
> `main` 后续前进而改变。

## 1. 归档结论

- M5 最终验收对象：`372c381f44ecfa1fa53961f137d0058033cbd805`。
- PR [#9](https://github.com/shixian66/xiaowei-agent/pull/9) 已以 fast-forward
  合入 `main`；GitHub 记录的 `mergeCommit` 与最终验收对象相同，无合并提交、
  无 SHA 改写，23 个受审提交原样保留。
- 项目负责人于 2026-09-05 明确确认“验收通过，可以归档”。
- 合并后 `main` 的 CI run
  [`33952529021`](https://github.com/shixian66/xiaowei-agent/actions/runs/33952529021)
  绑定最终验收 SHA，八个 job 全部成功。
- M5 的最强能力状态是 `tests`：真实 PostgreSQL integration 与隔离 Compose
  smoke 已验证；这不是部署、canary、真实运维目标验证或产品用户验收。

## 2. 范围

M5 将 M3 Runtime 与 M4 PostgreSQL TaskStore 组装成可恢复的本地 fake 闭环：

- 薄 FastAPI gateway 与标准库 CLI 只处理协议、可信身份上下文和
  `RenderPayload` 投影，不复制 Resolver、Planner、Policy、审批或状态判断。
- Worker 通过 TaskStore 的唯一领取事务取得 lease/fencing grant，驱动同一个
  Runtime/Runner，并从持久化 submission、plan 与 step journal 恢复。
- task submission、step journal、Evidence、audit、retry handoff 与事务结果未知
  使用 PostgreSQL 事实收敛；旧 fencing token 不能终态化新 winner。
- migration、health、readiness、配置校验、trace、结构化日志与脱敏进入可运行入口。
- API、Worker 与 migrate 共用同一应用镜像；Compose 使用 PostgreSQL、
  project-scoped named resources、file-backed secret reference 和宿主 loopback 发布。
- fake/recording ToolGateway 与确定性无模型 interpreter 支撑提交、查询、幂等、
  Worker 重启恢复和双 Worker 并发 smoke。

M5 没有连接真实模型、StarRocks 或其他运维系统，没有开放 E1，也没有拆微服务、
引入消息总线、实现 Web/飞书或审批消费端。

## 3. 合并与审查事实

- 切出基线：`fa6039bfbce680c0720606b2c34e556ce06c18f4`。
- 工作分支：`claude/m5-api-worker-compose`（已合入，保留备查）。
- 详细计划：[M5-api-worker-compose.md](../../plans/M5-api-worker-compose.md)
  V5.4.1。
- 最终 PR 验证 run
  [`33951654516`](https://github.com/shixian66/xiaowei-agent/actions/runs/33951654516)
  的 `headSha` 等于最终验收对象，八个 job 全部成功。
- 合并后 push 验证 run `33952529021` 再次在同一 SHA 上八绿，排除了验证只在
  pull-request 上成立的可能。

### 23 个受审提交

| # | SHA | 说明 |
| --- | --- | --- |
| 1 | `d4e2c71` | 固化 durable Worker 与 Compose 架构边界 |
| 2 | `502fc06` | 增加任务执行计数 schema |
| 3 | `895f09d` | 持久化不可变 task submission |
| 4 | `eacf053` | 增加 durable dispatch 与 retry |
| 5 | `3757e59` | 持久化可恢复 step journal |
| 6 | `b1a1fbc` | Runner 执行改为 lease-aware |
| 7 | `426d977` | trace delivery 显式化并接入 durable audit |
| 8 | `0e0a3de` | 拆分 Runtime 的提交、执行和查询 |
| 9 | `e82f9cd` | 增加 Worker loop 与 local stack |
| 10 | `ce749f4` | 增加薄 FastAPI gateway |
| 11 | `f5b2a88` | 增加标准库 CLI |
| 12 | `256c40b` | 增加 PostgreSQL Compose 基线 |
| 13 | `26c970c` | 增加 Compose 恢复 smoke gate |
| 14 | `28e03b0` | 收口恢复与失败语义；首轮复审对象 |
| 15 | `08d2e8a` | 补 Compose 资源归属守卫 |
| 16 | `ed32e7c` | 修复真实 PostgreSQL CI 回归 |
| 17 | `14151e0` | 分类 Compose migration 失败 |
| 18 | `b3f2625` | 修复容器 secret 文件可读性 |
| 19 | `3d38543` | 分类 smoke task outcome |
| 20 | `1d928b7` | 对齐 Compose 目标环境 |
| 21 | `7bbf389` | 归因 smoke action 失败 |
| 22 | `c9c0fcf` | 使 smoke 观测可由服务端解析 |
| 23 | `372c381` | 记录 PostgreSQL 与 Compose 运行证据；最终验收对象 |

### 复审后补齐的关键缺口

- Compose 资源归属测试补齐 named volume/network 的 `name`、`external` 与宿主
  bind mount 反例；三种变异均由同一契约测试捕获。
- 恢复测试明确证明 `failed_steps/degraded` 的两条等价防线同时退化时，任务会从
  `INDETERMINATE` 错写为 `SUCCEEDED`，避免用测试锁死某个私有实现位置。
- 真实 PostgreSQL 首次执行暴露 `sa.table()` 不能承载 `sa.Index` DDL；修为
  `sa.Table()`，降级 compatibility probe 才真正可执行。
- lease DTO 校验被移到 PostgreSQL 异常边界外；非法调用方输入不再被误归类为
  persistence integrity 故障或触发进程 fail-stop。

## 4. 已验证

### 本机

最终验收对象在本机 `.venv`、Python 3.11.16 下：

| 命令 | 结果 |
| --- | --- |
| `python -m pytest -q` | `1714 passed, 152 skipped, 5 warnings` |
| `python -m pytest -m security -q` | `923 passed, 79 skipped, 864 deselected, 5 warnings` |
| `ruff check .` | `All checks passed!` |
| `mypy src` | `Success: no issues found in 102 source files` |
| `git diff --check` | 无输出 |

本机 152 个 skip 全部属于未提供 `PYTEST_POSTGRES_DSN` 的 integration；本机没有
Docker，因此本机结果不冒充 PostgreSQL 或 Compose 运行证据。

### CI

最终 PR run `33951654516` 与合并后 `main` run `33952529021` 均绑定
`372c381f44ecfa1fa53961f137d0058033cbd805`，且下列八个 job 全部成功：

- `tests`：1714 passed，152 skipped；
- `security-gate`：923 passed，79 skipped，864 deselected；
- `integration`：1866 passed，**0 skipped**；
- `compose-smoke`：`compose-smoke: passed`；
- `lint`、`types`、`deps-audit`、`secret-scan`：success。

smoke 覆盖应用镜像构建、完整 migration chain、PostgreSQL health、API readiness、
容器内 CLI 提交/查询、Worker 消费、工具返回后 SIGKILL、租约到期接管、Evidence
等价、重复幂等键和双 Worker 并发，并在 `finally` 清理随机 Compose project。

终审还实际执行了八条变异检查；其中 SQLAlchemyError 兜底、sink guard、step
journal 关联不变量、接口分层、`_finish` fencing token 来源和预算终态六类保护均能
按预期转红。Compose 资源归属三种变异与恢复双防线同时退化的反例也分别转红。

## 5. 只读推理

- API/CLI/Worker 仍是模块化单体的三个进程入口，不是三个微服务；这一结论来自
  import 边界、同镜像配置与装配代码。
- migration/readiness、任务恢复与 audit 的责任边界已由契约和运行证据闭合，但
  生产容量、高可用和真实基础设施兼容性不能从隔离 CI 推导。

## 6. 未覆盖

- 未部署、未 canary，也没有产品用户验收证据。
- 未连接真实模型、真实 StarRocks、Prometheus、资产系统或其他运维目标；E1 调用
  恒为 0。
- 当前开发机没有 Docker 和 PostgreSQL 测试 DSN；运行证据来自 GitHub 隔离 runner。
- 未做长时间压力、chaos、跨 Docker/Compose 版本矩阵或生产数据库兼容验证。
- `AWAITING_APPROVAL` 的异步查询没有 pending approval 投影；M5 没有审批消费方。

## 7. 残余风险

| 风险 | 说明 |
| --- | --- |
| Compose 兼容性 | 单次 GitHub runner 不能证明跨 Docker/Compose 版本与长期稳定性。 |
| 崩溃点覆盖 | Compose 只覆盖“工具已返回、结果未提交”；“调用发出未返回”只有 journal 与保守预算保护。 |
| 真实副作用 | exactly-once 不能由 fake 证明；后续真实写必须依赖目标系统幂等与 readback。 |
| 数据库故障 | 提交确认边界精确断连、长锁竞争与资源耗尽仍主要由故障注入/契约测试承重。 |
| 分支保护 | private + GitHub Free 下仍不可用，合并纪律依赖人工。 |
| actor ACL | 同 tenant/environment 的不同 actor 当前可读同一任务；多 actor 部署前必须重审。 |

## 8. 后续入口

- M6a 只在详细计划获批并收到明确启动授权后，分别实现
  `prometheus.alert.evidence` 与 `asset.inventory.lookup` 的 fake 垂直闭环。
- M6b 的 StarRocks 非生产真实只读仍需单独批准环境、账号 secret reference、范围、
  时窗、脱敏、recording 删除和证据保留策略；M5 验收不授予该权限。
- M0–M7 的 E1 硬闸继续保持关闭；真实模型调用、Web/飞书与任何生产部署均需各自
  独立里程碑和证据。
