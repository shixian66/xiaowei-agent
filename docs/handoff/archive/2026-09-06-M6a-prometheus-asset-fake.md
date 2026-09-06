# M6a Prometheus 与资产 fake 能力归档（2026-09-06）

> 本文件承载 M6a 验收通过后的历史归档。当前有效状态仍以项目根目录的
> `AGENT_HANDOFF.md` 为准；详细测试、变异与风险见
> [M6a 里程碑验收报告](../M6a-acceptance-report.md)。
>
> 文中的 SHA、run id 与测试数字是归档时已核对的历史事实，不随 `main` 后续前进而改变。

## 1. 归档结论

- M6a 最终实现基线：`e2032fdff958d2309973458d5c762a54b3a4f855`。
- [PR #14](https://github.com/shixian66/xiaowei-agent/pull/14) 已以 fast-forward 合入
  `main`；GitHub 的 `mergeCommit` 与最终实现基线相同，无合并提交或 SHA 改写。
- 项目负责人于 2026-09-06 明确验收 M6a 并授权归档。
- 合入后 main push CI run
  [`33976421909`](https://github.com/shixian66/xiaowei-agent/actions/runs/33976421909)
  精确绑定最终实现基线，八个 job 全部成功。
- M6a 的最强能力状态是 `tests`：三个能力完成 fake/recording 闭环，GitHub 隔离 runner
  的 PostgreSQL integration 与 Compose smoke 已通过；这不是部署、canary、真实运维
  目标验证或产品用户验收。

## 2. 范围

M6a 在 M5 可恢复 Runtime 上增加两个独立的只读 fake 垂直能力，并验证扩展 seam：

- `prometheus.alert.evidence@1.0.0`：从 Alertmanager 读取精确告警，再按已注册固定
  PromQL 模板向 Prometheus 取指标证据；条件步骤只消费持久化 step journal。
- `asset.inventory.lookup@1.0.0`：按单一精确 asset ID、hostname 或 IP 查询 synthetic
  资产目录；tenant、environment、selector、target 与 Evidence 逐层核对。
- binding registry 取代 Runtime 的单能力硬编码；operation metadata 是 gateway 唯一真源。
- StepAdmission 支持 SQL/PromQL 互斥信封；无查询信封的只读资产步骤仍依赖 plan effect、
  ToolPolicy、恢复期 plan hash 和 recording 闭集，不被包装成等价 SQLGuard。
- `ResolvedTarget` 进入 Evidence builder，避免领域 Evidence 依赖不可信 step 参数重建 scope。
- Registry snapshot 与 production policy revision 随有序集合递增；生成能力地图保持
  `tests` 证据等级。

M6a 没有连接真实 StarRocks、Alertmanager、Prometheus、资产系统或模型 API，没有加入
Grafana、巡检、StarRocks 慢查询增强、通用 DSL、写能力或任何 E1 调用。

## 3. 合并与审查链路

| PR | 作用 | 合并事实 |
| --- | --- | --- |
| [#11](https://github.com/shixian66/xiaowei-agent/pull/11) | binding seam + Prometheus fake | 代码修复复审对象 `cfd63923a35be1e5dcfe13b3dbd78c00e0bca520`；最终 `ffd58ef09f13d983fb63fd3c8e106cabe60d4977` 合入 |
| [#12](https://github.com/shixian66/xiaowei-agent/pull/12) | Evidence target seam | 候选 `b8a16705f16c7400a0accb8131a1d4a25f3a6b3d`；rebase 后 `8bac76aec5acf6eea82fcff2d3efaedd232c77ee` 合入 |
| [#13](https://github.com/shixian66/xiaowei-agent/pull/13) | seam 合入事实收口 | `32826af1ae70c7d88e3e7406d4cf2570f1157d2f` 合入 |
| [#15](https://github.com/shixian66/xiaowei-agent/pull/15) | CI secret-scan 确定性自检 | `9d9380c41af1f059b02025a008b5e44879657dcf` fast-forward 合入 |
| [#14](https://github.com/shixian66/xiaowei-agent/pull/14) | 资产 fake + M6a 数据收口 | 最终复审与合入对象均为 `e2032fdff958d2309973458d5c762a54b3a4f855` |

详细实施计划：[M6a-prometheus-asset-fake.md](../../plans/M6a-prometheus-asset-fake.md)
V1.1。扩展成本与 DSL 依据：[M6a capability 扩展数据](../M6a-capability-extension-data.md)。

## 4. 已验证

最终实现基线的本机四条规范门：

| 命令 | 结果 |
| --- | --- |
| `python -m pytest -q` | `2169 passed, 154 skipped, 5 warnings` |
| `python -m pytest -m security -q` | `1012 passed, 79 skipped, 1232 deselected, 5 warnings` |
| `ruff check .` | `All checks passed!` |
| `mypy src` | `Success: no issues found in 133 source files` |

PR #14 最终 run
[`33975532006`](https://github.com/shixian66/xiaowei-agent/actions/runs/33975532006)
与合入后 main run `33976421909` 均绑定最终实现基线，tests、integration、compose-smoke、
security-gate、lint、types、deps-audit、secret-scan 八个 job 全部成功。

独立复审在最终 SHA 上复跑四门并做三组变异：移除 Evidence environment/target 比对、
把 recording scope key 改成常量、删除“恰一 selector”校验，合计 11 条用例转红；还原后
工作树干净。此前 PR 1 与 PR 2 的隔离变异还覆盖 PromQL 重编译、gateway 派生、持久化
条件、snapshot/policy 联合 golden、恢复期 plan hash、Evidence 白名单与入口中立性。

## 5. 只读推理

- PR 2 对 Runtime、Runner、Gateway、StepAdmission、持久化、跨边界 contracts 与 canonical
  hash 的执行核心改动为 0 文件，说明 binding/target seam 可承载第三个异质能力。
- 注册与装配面仍有 6 文件、`+183/-17` 的真实成本；“执行核心 0 改动”不等于“零共享改动”。
- 三个能力分别依赖 SQL AST、固定 PromQL/时序语义和资产唯一性，没有三个同类能力证明
  通用 DSL 的收益。因此 ADR-004 继续延期，不在 M6a 引入动态 discovery 或任意 executor。
- 资产歧义的 `needs_user_input=False` 是有意语义：精确 selector 已给出且没有可补槽位；
  Prometheus 多告警还能补 fingerprint，因此两者不应机械统一。

## 6. 未覆盖

- 未连接真实 StarRocks、Alertmanager、Prometheus、资产系统或模型 API。
- 未部署、未 canary、未做产品用户验收；项目负责人验收只代表 M6a 开发里程碑退出。
- 当前开发机没有 Docker 与 PostgreSQL 测试 DSN；对应运行证据来自 GitHub 隔离 runner。
- 未覆盖任意 PromQL、真实 alert rules、长期时序窗口、资产分页/权限/重复数据、巡检、
  拓扑、模糊查询、写入、Grafana、StarRocks 真实只读或任何 E1 能力。

## 7. 残余风险与后续入口

- `tests/security/test_asset_scope_isolation.py` 应补同文件 in-scope 成功对照；当前仅负向
  用例会在 recording 全部 miss 时空过，unit 正向用例与它组合才抓住该类变异。
- `evidence/asset_inventory.py` 的六个版本化常量副本应增加 pairing 测试，或改为装配层
  传参；当前漂移会 fail-closed，但会造成难定位的功能故障。
- `asset_id` 是否收紧为 ASCII，等未来真实资产 adapter 单独立项并取得权威契约后决定；
  不属于只负责 StarRocks 真实只读的 M6b。Unicode NFC + 精确匹配当前不会放宽权限，
  但可能出现视觉同形却查询 miss。
- synthetic catalog 规模很小，不能证明真实服务兼容性；分支保护仍受 private + GitHub Free
  限制，合并纪律依赖人工。
- M6b 是独立里程碑，尚未启动。只有环境、只读账号/secret reference、查询范围、调用窗口、
  脱敏、recording 删除方式和证据保留周期均获明确批准后，才可建立真实 StarRocks 连接。
