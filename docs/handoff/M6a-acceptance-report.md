# M6a Prometheus 与资产 fake 能力——里程碑验收报告

> 按 DEVELOPMENT_PLAN 的四段格式：已验证 / 只读推理 / 未覆盖 / 残余风险。
>
> **最终实现基线**：`e2032fdff958d2309973458d5c762a54b3a4f855`。
> [PR #14](https://github.com/shixian66/xiaowei-agent/pull/14) 的 `mergeCommit` 与该 SHA
> 相同，已以 fast-forward 合入 `main`。
>
> 历史资产代码与测试数据采集点为 `5a2bdec9902f5955b9e61a7fae3ffeb9daae8959`；线性
> 重放后的等价提交为 `9a2cadc`。归档文档会形成后续提交，所以该提交 SHA 由归档 PR 的
> `git rev-parse HEAD` 提供，不在文件内写一个必然落后的自引用值。
>
> **状态：项目负责人已于 2026-09-06 明确验收 M6a 并授权归档。** 这里的验收是开发
> 里程碑验收，不是部署、canary 或产品用户验收。

## 1. 已验证

### 1.0 里程碑合并链路

| PR | 作用 | 合并事实 |
| --- | --- | --- |
| [#11](https://github.com/shixian66/xiaowei-agent/pull/11) | binding seam + `prometheus.alert.evidence` | 代码修复复审对象 `cfd63923a35be1e5dcfe13b3dbd78c00e0bca520`；最终以 `ffd58ef09f13d983fb63fd3c8e106cabe60d4977` 合入 |
| [#12](https://github.com/shixian66/xiaowei-agent/pull/12) | 把已准入 `ResolvedTarget` 传入 Evidence builder | 候选 `b8a16705f16c7400a0accb8131a1d4a25f3a6b3d`，rebase 后 `main` 为 `8bac76aec5acf6eea82fcff2d3efaedd232c77ee` |
| [#13](https://github.com/shixian66/xiaowei-agent/pull/13) | target seam 合并事实收口 | 合入后 `main` 为 `32826af1ae70c7d88e3e7406d4cf2570f1157d2f` |
| [#15](https://github.com/shixian66/xiaowei-agent/pull/15) | CI secret-scan 确定性自检 | `9d9380c41af1f059b02025a008b5e44879657dcf` fast-forward 合入 |
| [#14](https://github.com/shixian66/xiaowei-agent/pull/14) | `asset.inventory.lookup` 与 M6a 扩展数据 | 复审与合入对象均为 `e2032fdff958d2309973458d5c762a54b3a4f855` |

三个能力均完成 fake/recording 垂直闭环；最终 Registry snapshot 为
`snapshot.m6a.starrocks-prometheus-asset.v1`，最强 evidence level 均为 `tests`。

### 1.1 功能与安全边界

- binding registry 取代 Runtime 的单能力硬编码；operation metadata 是 gateway 唯一真源，
  Worker/API/CLI 未获得 capability 领域分支。
- `prometheus.alert.evidence@1.0.0` 固定执行 Alertmanager `get_active_alerts` 与条件式
  Prometheus `query_metric_range`；只允许 capability 注册的两类 PromQL 模板，准入期从
  参数重编译后逐字比对，不接收任意 PromQL。
- `asset.inventory.lookup@1.0.0` 只接受一个精确 asset ID、hostname 或 IP；规范化后编译为
  一个 `lookup_asset` 只读步骤，gateway 从 Registry operation metadata 派生。
- fake adapter 的 recording key 包含 tenant、environment、selector kind/value；没有
  fallback。Evidence 只保留九字段，并核对已准入 target 的 environment 与精确身份。
- 0 行、2 行、timeout、malformed、scope/identity 冲突均降级为 `INDETERMINATE`；只有一条
  合法事实能渲染“唯一资产”，且不声称巡检、健康、拓扑或根因。
- 终态查询按持久化 plan key 选择资产 renderer；存储计划 selector/limit 漂移在 adapter
  前失败；敏感额外字段不会进入 Evidence、Render 或 trace。
- Registry snapshot 与 production policy revision 分别递增为
  `snapshot.m6a.starrocks-prometheus-asset.v1` 与 `policy-2026-09-05.2`，均与有序集合成对固定。

### 1.2 TDD 与局部门

真实红灯：

- A3 首次收集：缺 `evidence/reflection/rendering` 三个资产模块，3 个 collection error；
- A4 Runtime 首次收集：缺 `tests.fakes.asset_recordings`，1 个 collection error；
- Compose helper 首次执行：缺资产 persistence/render helper，4 failed；
- 首次全量：2 failed / 2166 passed / 154 skipped，根因是两个既有生命周期测试手工装配
  binding 时遗漏新能力；扫描所有同类构造点修正，没有放宽完整性闸门。

转绿证据：

```text
资产 Runtime + local stack                 → 28 passed
资产 L0/L1/L2 + Runtime 复核               → 77 passed
A4 组合门（矩阵校正前）                    → 125 passed, 2 skipped
能力地图/文档命令一致性                    → 5 passed
python -m pytest --collect-only -q          → 2322 tests collected
```

### 1.3 四条规范门

环境：本机 `.venv`，Python 3.11；未设置 `PYTEST_POSTGRES_DSN`，本机没有 Docker。

```text
python -m pytest -q
→ 2169 passed, 154 skipped, 5 warnings

python -m pytest -m security -q
→ 1012 passed, 79 skipped, 1232 deselected, 5 warnings

ruff check .
→ All checks passed!

mypy src
→ Success: no issues found in 133 source files
```

154 个 skip 保持既有受控语义；其中 M6a PostgreSQL 集成路径因本机无 DSN 跳过，不能当作
真实 PostgreSQL 运行证据。

### 1.4 远程 CI

[PR #14](https://github.com/shixian66/xiaowei-agent/pull/14) 的最终 run
[`33975532006`](https://github.com/shixian66/xiaowei-agent/actions/runs/33975532006) 精确绑定
`e2032fdff958d2309973458d5c762a54b3a4f855`，八个 job 全绿：tests、security-gate、
lint、types、secret-scan、deps-audit、integration、compose-smoke。合入后的 main push run
[`33976421909`](https://github.com/shixian66/xiaowei-agent/actions/runs/33976421909)
再次绑定同一 SHA，八个 job 全绿。integration 与 Compose smoke 补齐本机缺失的隔离
PostgreSQL 和三能力运行证据；仍不是任何真实运维目标验证。

事实收口提交曾触发既有随机 secret-scan 自检失败；独立 [PR #15](https://github.com/shixian66/xiaowei-agent/pull/15)
已将自检改成确定性的 40 位高熵 bait，并以 fast-forward 合入资产 PR 当时的 base。PR #15 run
[`33974959970`](https://github.com/shixian66/xiaowei-agent/actions/runs/33974959970) 与 main run
[`33975166668`](https://github.com/shixian66/xiaowei-agent/actions/runs/33975166668) 均八项全绿。
旧随机 bait 的具体未命中条件不可追溯；不把长度或熵写成已验证根因。

### 1.5 隔离变异反证

均在 `5a2bdec` 的 detached 临时 worktree、独立 pycache 与显式 `PYTHONPATH` 下执行；每项
撤掉保护后确认红灯，再还原，最终临时工作树无 diff 并删除。

| 撤掉的保护 | 转红结果 |
| --- | --- |
| recording key 不再消费 tenant/environment | scope 隔离 2 failed |
| Evidence 九字段白名单放行 `credential` | 白名单 1 failed |
| capability 集合增加但 snapshot ID 回退 | declaration golden 1 failed |
| 恢复期跳过 plan hash 比对 | selector 漂移 1 failed，结果不再是 Gateway 前 `FAILED` |
| profile 集合增加但 policy revision 回退 | policy golden 1 failed |

第一次 scope 变异由于 editable install 仍加载主 worktree 而得到全绿；随后先打印模块
`__file__`，用显式 `PYTHONPATH` 确认加载 detached 变体后得到预期 2 红。前一次结果只说明
测试环境指向错误，不被计作保护未承重。

最终 SHA 的独立复审还做了三组变异：移除 Evidence 的 environment/target 比对、把
recording scope key 改成常量、删除“恰一 selector”校验，合计 11 条用例转红；还原后
工作树保持干净。独立复审逐项核对最终 SHA、四条规范门和八项 CI 后给出无阻断结论。

### 1.6 扩展成本

- base → 数据 head：45 files，`+3002/-31`；27 个测试文件；资产 eval 53 个 corpus case、
  59 个实际 pytest case。
- 执行核心：0 文件、`+0/-0`。
- 五个注册/装配真源加生成能力地图：6 文件、`+183/-17`。
- 详细分类见 [M6a capability 扩展数据](M6a-capability-extension-data.md)。

## 2. 只读推理

- PR 2 没有修改 Runtime、Runner、Gateway、StepAdmission、持久化、跨边界 contracts 或
  canonical hash，且完整闭环通过，说明 PR 1 binding seam 与 PR #12 target seam 可以承载
  第三个异质 fake capability。
- 三个能力的执行骨架重复，但领域语义并不同：SQL AST、PromQL 时序、资产唯一性不能安全
  收敛成同一套开放 DSL。当前证据支持 ADR-004 继续延期。
- 五个注册/装配真源每次都要显式编辑是实际成本；若后续三个同类能力继续出现同样变更，
  才值得另立 ADR 评估窄的声明式注册。
- 资产返回两行时 `needs_user_input=False` 是有意设计：用户已经给出精确 selector，没有
  可补的槽位；Prometheus 多告警还可补 fingerprint，故为 `True`。两者不是同一交互条件，
  不应机械统一。

## 3. 未覆盖

- 本机未提供 PostgreSQL DSN 且没有 Docker；本机相关 integration 为受控 skip、Compose
  未实跑。最终 SHA 的 PostgreSQL/Compose 证据来自 GitHub 隔离 runner。
- 未连接真实资产系统、StarRocks、Prometheus、Alertmanager 或模型 API；没有任何 E1 调用。
- 未部署、未 canary、未做真实用户问法或产品用户验收；项目负责人完成的是开发里程碑验收，
  不改变 readiness level。
- 未实现列举、模糊/CIDR/range、跨环境选择、标签搜索、拓扑、巡检、写入、Grafana 或
  StarRocks 真实只读；M6b 尚未启动，其入口条件与授权不因 M6a 归档自动满足。

## 4. 残余风险

1. synthetic catalog 只有一个资产及三个精确别名，不能证明真实资产 API 的字段、分页、
   限流、权限、重复数据或错误语义兼容。
2. 规则式 intent 面刻意很窄；真实用户问法可能落到 `unknown` 并被拒绝。方向安全，但可用性
   未经用户语料校准。
3. asset ID、hostname 与 IP 在执行前各自形成 fingerprint；M6a 不证明不同 selector 一定是
   同一资产。若要跨别名同一指纹，必须引入权威目录参与目标解析并重新评审恢复语义。
4. Evidence 层为保持纯度而固定能力 key、步骤 ID、field set 与 selector 版本；这些是
   `1.0.0` 的隐式契约，未来改名/升版必须同步 planner、Evidence、Reflection、renderer 与测试。
5. 五个注册/装配点仍需重复编辑；当前显式性利于审查，但能力数量增加后会提高漏登记风险。
   完整性 golden 与 binding registry 能 fail-closed，不能消除维护成本。
6. `tests/security/test_asset_scope_isolation.py` 只有跨 scope 负向断言。独立复审变异证明，
   recording scope key 被常量化时该文件会因全部 miss 而空过；应补同一测试内的 in-scope
   成功对照。当前 unit 正向用例与该安全测试合起来仍能抓住变异。
7. `evidence/asset_inventory.py` 的 capability/version、selector version、field set、limit、
   environment allowlist 等六个副本没有单独 pairing 真源。漂移会 fail-closed，但会造成
   难定位的功能故障；后续应补 pairing 测试或改为装配层传参。
8. `asset_id` 当前接受 Unicode NFC。精确匹配不会放宽权限，但可能产生视觉同形却查询 miss；
   是否收紧为 ASCII 必须等未来真实资产 adapter 单独立项并取得权威契约后决定，不属于
   当前只负责 StarRocks 真实只读的 M6b。
9. private + GitHub Free 下仍没有分支保护，红灯合并和强推风险依赖人工纪律。
