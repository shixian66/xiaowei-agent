# W5 edge 限流证据清单

> 用途：canary 前置。缺任何一组、任何一项，不得开始 canary。本清单是模板，填写副本存放在
> 部署证据位置，不提交真实值。

应用本身**不做** HTTP rate limit：OAuth state 容量与 Activation 容量是防滥用的容量上限，**不是**
限流，不能拿来冒充 edge rate limit。edge 存在也不删除应用的 Origin/CSRF/Session/CSP/权限检查。

没有本清单运行证据的 LAN 访问只能算本地体验，**不是 canary**。

## 通用信息

| 项 | 记录 |
| --- | --- |
| edge 产品与配置版本 | |
| 可信 client-IP 链（哪一跳写入、哪一跳信任） | |
| 告警通道与 owner | |
| 证据引用（写入部署模板 `XIAOWEI_RELEASE_EDGE_EVIDENCE_REF`） | |

## 五组路由

每组都要有：正常请求、429 反例、窗口恢复、告警触发与 owner。

| 路由组 | 限额与窗口 | 正常请求 | 429 反例 | 窗口恢复 | 告警触发 | owner |
| --- | --- | --- | --- | --- | --- | --- |
| `/login/api/login` | | | | | | |
| `/oauth/feishu/start` | | | | | | |
| `/oauth/feishu/callback` | | | | | | |
| `/admin/api/activations/approve` | | | | | | |
| `/admin/api/activations/reject` | | | | | | |

provider-off release 下 OAuth 两条路由在应用内不存在（返回 404）；edge 仍要为它们配好限流，
以便日后 RI2 另行取得 GO 时不留空窗。

## 日志禁止项

edge 与应用日志、截图与证据文件都**不得**包含：query、`code`、`state`、Cookie、密码、Secret、
subject 或请求正文。
