# W5 用户验收清单（provider-off 功能矩阵）

> 对应 [runbook](../runbooks/w5-product-deployment.md) §6。前置：canary 已签署。本清单完成只能标记
> `W5 provider-off user-accepted`；三层齐备也只允许归档 W5 产品壳，不得写 RI6 或只读 V1 完成。

| 验收人 | 时间 | 部署 digest |
| --- | --- | --- |
| | | |

## 本轮验收项

| 场景 | 结果 | 签字 |
| --- | --- | --- |
| Local Admin 登录 / 强制改密 | | |
| Admin 用户管理 | | |
| Admin 审计 | | |
| Admin 配置（保存后显示“已保存，尚未接入/未授权”） | | |
| Admin 资源登记（只登记，不连接） | | |
| 工作台如实显示当前无可执行能力 | | |
| 真实功能的未授权/待接入提示 | | |
| 统一失败分类 | | |

## 本轮不适用

以下路径本轮记为**不适用**，不能用假 Session 或合成结果补齐角色矩阵：

- Operator 与普通用户的真实 OAuth 登录；
- 飞书私聊/群消息与卡片回执；
- 真实 Provider（Gemini）与真实目标（StarRocks、Prometheus）；
- 真实结果深链。
