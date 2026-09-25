# W5 部署证据清单（deployed SHA）

> 对应 [runbook](../runbooks/w5-product-deployment.md) §1 与 §3。填写副本存放在部署证据位置；
> 不把 Secret、生产数据或 raw actor 复制进 Git。本清单完成只能标记
> `W5 provider-off product-shell deployed SHA`，不是 canary，更不是 RI6。

## 执行输入（负责人书面指定）

| 项 | 记录 |
| --- | --- |
| W5-C GO（签署人、时间） | |
| 目标主机与维护窗口 | |
| source SHA（40 位） | |
| image digest（`name@sha256:<64 hex>`） | |
| Compose 文件集合 | 固定：`docker-compose.yml` + `docker-compose.release.yml` |
| 部署检查结果 | `release-compose: ok`（只贴这一行） |
| HTTPS edge 与限流证据引用 | |
| 回滚 owner | |
| 回滚目标 | W5-compatible release digest：____；或首次部署：停止全部应用服务 |
| RI2 / RI3 / RI4 / H 层 / RI6 | 本次全部 NO-GO；最终 Compose 仍为 false/disabled |

## 首次 release 历史数据决定

| 项 | 记录 |
| --- | --- |
| owner 书面选择 | 新数据库 / 单独批准的数据处置（二选一） |
| release 预检结果 | `result` 与三个计数（不含 task id） |

runbook 不提供删除历史 plan/evidence 的便捷命令；命中 `historical_execution_data_present` 时停止。

## 执行记录

| 步骤 | 记录 |
| --- | --- |
| DB 备份位置与时间 | |
| 三域配置备份位置与时间 | |
| 旧 listener 已停的证明 | |
| migration revision（head） | |
| 三域配置 generation / readback | |
| `config_preflight` | `preflight: ok` |
| 身份迁移（count-only） | 首次：created / skipped / deferred；第二次：`created_count=0`；或 `not_applicable` |
| 全库保留清理（count-only） | pending_expired / approved_deleted / rejected_deleted / expired_deleted |
| 每日调度与失败告警 | 已配置 / 未配置（未配置不得晋升 canary） |
| 初始改密与旧 Session 失效 | |
| health / readiness | |
| 容器 image 与 source-sha 标签 readback | |
| 端口面 | 只有 web-app 在模板地址 8080；api/worker/postgres 无宿主端口 |
| Provider/target 开关 | 全部 false/disabled |

身份报告不得含 raw actor、subject 或 deferred label。
