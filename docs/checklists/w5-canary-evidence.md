# W5 canary 证据清单

> 对应 [runbook](../runbooks/w5-product-deployment.md) §4–§5。前置：deployed SHA 已签署、
> [edge 清单](w5-edge-rate-limit-evidence.md) 五组齐全、回滚演练完成。本清单完成只能标记
> `W5 provider-off canary`，不是 user-accepted，也不是只读 V1。

## 回滚演练

| 项 | 记录 |
| --- | --- |
| 反向目标 drain/history 预检结果 | |
| 类型 | 有 W5-compatible 旧 digest：回退镜像/非秘密配置并核对 readback；首次部署：只演练停止应用服务，再恢复候选 |
| 结果与耗时 | |

任何演练都**绝不启动 recording 栈**，也不把运行形态改回 `offline_recording`。

## canary 范围

| 项 | 记录 |
| --- | --- |
| Local Admin 验收人（仅此范围） | |
| 时间窗 | |
| 停止条件 | 例：5xx、登录失败率、edge 告警、配置 readback 不一致 |

## 观察项

| 观察项 | 结果 |
| --- | --- |
| 本地登录 / 强制改密 | |
| Admin 页面 | |
| 空能力工作台（如实显示当前无可执行能力） | |
| 进程重启后恢复 | |
| DB 健康 | |
| edge 拒绝（429） | |
| 配置 readback | |

OAuth、群消息、模型与资源目标**不得**进入观测流量；命中停止条件立即按 runbook §4 回滚。
