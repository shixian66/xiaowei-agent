# M7 Web 与飞书薄渠道——离线范围验收报告

> 按 DEVELOPMENT_PLAN 的四段格式：已验证 / 只读推理 / 未覆盖 / 残余风险。
>
> **PR 8 主体实现对象**：`8a5d43f36803835ae94fabdcee0e1ee5e1bff865`；审查后补齐安全语料
> 驱动闭集的代码与测试对象为 `09b6babf8989279c99237f56a0d601ed320edc5b`，基于
> PR 7 合入提交 `0e7cb5c1e9db8ce34ed6f2a3e90f7374b16d2e6a`。
> 本报告与事实文档会形成后续提交，因此最终候选 SHA 必须由
> `git rev-parse HEAD` 提供，不在文件内写必然落后的自引用值。
>
> **状态：PR #27 的最终受审 head `c50d820…` 已以 squash commit `ba5ecfe5…` 合入 `main`，M7
> 离线实现范围 8/8 完成。项目负责人于 2026-09-09 明确批准按离线范围验收并归档；最强证据仍为
> `tests`，真实渠道验证尚未开始，`DEVELOPMENT_PLAN.md` 的 M7 完整退出标准仍未通过。**

## 1. 已验证

### 1.1 基线与范围

- 项目负责人于 2026-09-09 明确说“合并归档吧”，批准 M7 离线范围验收并授权归档；历史事实见
  [M7 离线范围归档](archive/2026-09-09-M7-web-feishu-offline.md)。该授权只关闭 PR 1–8 的离线
  实施任务，不授权真实渠道，也不解锁只读 V1 发布点或 M8。
- PR 7 已以 PR #26 合入 `main@0e7cb5c`；其最终受审 head 为 `11d90b8`，PR run
  `34329735688` 与合入后 main run `34329993048` 均八个 CI job 全绿，前者 integration
  `2976 passed`、0 skipped。
- PR 8 最终受审 head `c50d8201377588270e13fedc676b05f2a4433dd7` 已通过 PR #27 以 squash
  commit `ba5ecfe5edffb408e20c4bb9cbf494bfbb035b82` 合入；因此 M7 计划内 PR 1–8 的离线实现范围
  已全部进入 `main`。
- PR 8 只修改跨渠道离线证据、默认关闭的 Compose 拓扑、canonical smoke 与事实文档；没有
  注册真实飞书应用、读取真实 secret、打开外部网络、部署或执行 canary，也没有开启 E1。
- `docker-compose.smoke.yml` 经检查仍能复用既有短租约/轮询 override，因此保持零 diff；没有为
  计划中不存在的 `smoke` service 新建第二套入口。

### 1.2 同一 Runtime、投影与状态语义

- 同一终态 `TaskView`/`RenderPayload` 经 internal API、CLI、Web DTO 与飞书卡片投影保持
  status、安全字段和 refs 一致；飞书只因 provider 字节预算做显式截断，并显示“还有更多证据”及
  受信 HTTPS 详情入口。
- 新增真实 PostgreSQL 集成用例把 fake 飞书入站、同一 TaskStore、完整 task worker、projection
  worker、原卡片更新和 Web 详情串成一条链；另证明普通 Web 用户终态私聊一次，admin 不收到额外
  私聊。该用例本机因没有测试 DSN 受控跳过，尚不能计为本机 PostgreSQL 运行证据。
- 八条安全 Eval 都有真实执行驱动：伪造身份、跨 scope、旧群成员、任务 ID 猜测、外部文本、
  数据库原始行、provider 有界重试与陈旧 claim。驱动注册集合与语料 carrier 集合必须精确相等，
  `_by()` 对未知或拼错的 carrier 会在收集期显式失败，不会以空参数化静默跳过。

### 1.3 Compose 与默认关闭边界

- Compose 闭集包含 `api`、`worker`、`feishu-listener`、`channel-worker`、`web-app`、`migrate` 与
  `postgres`。五个长驻应用角色使用同一镜像；三个渠道角色属于 `m7-channels` profile，flag 默认
  `false`。
- listener 与两个 worker 不发布宿主端口；API 与 Web 仅发布 loopback 端口。Web/internal 路由
  闭集继续由真实 app factory 契约保护。
- canonical `python -m scripts.compose_smoke` 会在同一已运行镜像内执行三个渠道模块入口，要求
  全部以 code 2 且 stdout/stderr 为空退出；测试覆盖任一入口返回 0、其他非 2 或产生输出时 smoke
  必须失败且不转抄敏感输出。

### 1.4 TDD 与变异反证

真实红灯：

- Compose 三个渠道服务尚未声明时，新增闭集测试为 5 failed；最小拓扑落地后转绿。
- canonical smoke 尚无默认关闭入口检查时，新增脚本测试为 5 failed；helper 与调用链落地后转绿。
- 安全 Eval 首轮外部文本断言暴露 JSON 表示层与卡片纯文本层混淆；纠正错误测试观察面后 9 条
  Eval 转绿，没有为绿灯改变生产渲染语义。
- 首轮“每个安全语料都有执行驱动”守卫只检查语料 taxonomy，未检查测试驱动登记；新增驱动闭集
  等式后先因登记集合为空准确转红，补齐 `_by()` 的非空校验与 import 期登记后 10 条 Eval 转绿。

隔离变异均使用独立 worktree、独立 pycache 与显式 `PYTHONPATH`，恢复后相关 10 条测试通过，
临时 worktree 零 diff 后删除：

| 临时撤掉的保护 | 准确转红结果 |
| --- | --- |
| task lookup 把 environment 固定为 `dev` | prod principal 读到 dev task，scope Eval 未抛 404 |
| 群成员结果为 false 仍返回记录 | stale-membership Eval 未抛 404 |
| 同一 worker 重领后忽略 fencing token | 旧 token 写回 `applied=True`，覆盖新 claim |
| Web app 临时注册 `GET /v1/tasks` | 路由精确闭集出现额外 internal 路由 |
| 删除 `database_rows` 的执行驱动 | 驱动登记集合缺项，闭集守卫准确转红 |
| 把驱动 carrier 拼成 `database_row` | 收集期显式 `AssertionError`，没有退化为 skip |

fencing 用例在变异前进一步收紧为同一个 `claim_owner` 重领两次，并显式断言 token 不同；因此
失败只能由 fencing token 保护被拆掉触发，不会被 owner 不同提前挡住。

### 1.5 本机工程门

本报告提交前在最终工作树重跑的结果：

```text
python -m pytest -q
→ 2808 passed, 187 skipped, 5 warnings

python -m pytest -m security -q
→ 1193 passed, 79 skipped, 1723 deselected, 5 warnings

ruff check .
→ All checks passed!

mypy src
→ Success: no issues found in 151 source files
```

另运行文档/Compose 聚焦门，`59 passed`。全局 pytest 继续以 `--disable-socket` 运行；5 条 warning
来自故意触发网络禁令的反例，不是网络放行。187 个 skip 保持本机无 PostgreSQL DSN 的受控语义。

### 1.6 PR 远程 CI

- PR [#27](https://github.com/shixian66/xiaowei-agent/pull/27) 的首次完整 run
  [`34340238099`](https://github.com/shixian66/xiaowei-agent/actions/runs/34340238099) 精确绑定代码与测试
  head `09b6babf8989279c99237f56a0d601ed320edc5b`；tests、security-gate、lint、types、deps-audit、
  secret-scan、integration、compose-smoke 八个 job 全绿。
- integration job 在 GitHub 隔离 PostgreSQL service 上实跑 `2995 passed`、**0 skipped**；仓库的
  integration gate 会把 DSN 已设置时的任何 skip 改为整轮失败。
- compose-smoke job 在真实 CI 容器里输出 `compose-smoke: passed`，包含三个默认关闭渠道入口的
  code 2、无输出检查。以上是隔离 PostgreSQL/Compose 与 fake/recording 渠道证据，不是飞书真实
  provider、部署、canary 或产品用户验收。
- 本报告收口形成晚于上述代码/测试 head 的纯文档提交；该最终 SHA 及其 PR CI 由 Git/PR 外部
  记录提供，不在报告内循环自引用。
- 最终 PR run [`34341819892`](https://github.com/shixian66/xiaowei-agent/actions/runs/34341819892)
  与合入后 main run [`34343986339`](https://github.com/shixian66/xiaowei-agent/actions/runs/34343986339)
  均八项全绿；main run 的 integration 为 `2995 passed`、0 skipped，compose-smoke 输出
  `compose-smoke: passed`。
- 离线完成交接 PR [#28](https://github.com/shixian66/xiaowei-agent/pull/28) 的 head
  `35b0d0f4da459700ed0f847083477bac3fe7a40c` 已以 squash commit
  `a5c88cbc285658f7f38355a9470bfdd725858ba5` 合入；run
  [`34344702378`](https://github.com/shixian66/xiaowei-agent/actions/runs/34344702378) 八项全绿，
  integration 为 `2995 passed`、0 skipped，compose-smoke 输出 `compose-smoke: passed`。

## 2. 只读推理

- parity、端到端 fake 流与模块依赖闭集共同表明，飞书和 Web 是同一 TaskStore、TaskViewRuntime、
  RenderPayload 与终态的薄投影，不存在第二套业务路由、Policy、Guard、审批或执行链。
- projection 订阅与渠道绑定只保存投递事实，任务真相仍由 TaskStore 提供；provider 重试和乱序失败
  不应改变任务终态。
- 三个渠道进程复用同一镜像且默认关闭，降低了部署分叉；这只是架构与离线测试结论，不能推出
  容器、网络或真实 provider 已可用。

## 3. 未覆盖

- 当前开发机没有 Docker/Podman，未在本机执行 PR 8 更新后的 Compose smoke；当前开发机也未提供
  `PYTEST_POSTGRES_DSN`，新增 M7 同库 integration 在本机受控跳过。PR run `34340238099` 已在
  GitHub 隔离 runner 补得 PostgreSQL 0 skipped 与 Compose 容器运行证据，但不能改写本机未验证
  的事实。
- 没有真实飞书 app、OAuth code exchange、成员接口、消息发送、长连接、真实凭据或网络调用；没有
  部署、canary、回滚演练或产品用户验收。
- PR 7 的 1280/1440 桌面工作台及窄屏只读详情尚无真实浏览器截图；HTML/CSS/JS 契约不等于视觉
  验收。
- Admin 配置中心、真实模型 API、审批/重跑、Jenkins、Dinky 与其他运维能力不属于 M7，未实现。

## 4. 残余风险

1. 飞书投递是 at-least-once；provider 已成功而数据库提交前崩溃仍可能产生重复消息，不能宣称
   exactly-once。
2. 锁定 SDK 的静态 async/sync 形状与 fake port 不能证明真实租户的权限、限流、分页、延迟、错误
   正文、token cache 或事件循环行为。
3. 身份文件在 listener/Web app 装配时一次读取，权限变更依赖受控重启；真实激活前还需确定变更
   发布、撤权时延和审计流程。
4. 过期 OAuth state/session 的后台清理、公网限流、代理/ingress 拓扑与 TLS 运维尚未实现或现场
   核对。
5. Compose profile、`depends_on`、loopback 端口和容器退出语义已由 PR run `34340238099` 在一个
   隔离 runner 版本实跑通过；该单次证据不能证明其他 Docker/Compose 版本、长期运行或生产兼容。
6. 群详情每次轮询实时查询成员，以权限即时性换取 provider 调用成本；真实群规模与限流下的负载
   尚未测量。
