# M6a PR 2 资产精确查询 fake 闭环——验收报告

> 按 DEVELOPMENT_PLAN 的四段格式：已验证 / 只读推理 / 未覆盖 / 残余风险。
>
> **受审对象**：分支 `claude/m6a-asset-inventory-lookup` 的 HEAD，相对
> `main = 32826af1ae70c7d88e3e7406d4cf2570f1157d2f`。
>
> 代码与测试数据采集点为 `5a2bdec9902f5955b9e61a7fae3ffeb9daae8959`。本报告自身
> 还会形成后续提交，所以最终候选 SHA 由交接时的 `git rev-parse HEAD` 提供，不在文件内
> 写一个必然落后的自引用值。
>
> **状态：本地候选，等待独立复审、GitHub CI 与项目负责人验收。未合入、未归档。**

## 1. 已验证

### 1.1 功能与安全边界

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
→ 2168 passed, 154 skipped, 5 warnings

python -m pytest -m security -q
→ 1011 passed, 79 skipped, 1232 deselected, 5 warnings

ruff check .
→ All checks passed!

mypy src
→ Success: no issues found in 133 source files
```

154 个 skip 保持既有受控语义；其中 M6a PostgreSQL 集成路径因本机无 DSN 跳过，不能当作
真实 PostgreSQL 运行证据。

### 1.4 隔离变异反证

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

### 1.5 扩展成本

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

## 3. 未覆盖

- 本机未提供 PostgreSQL DSN，新增资产 persistence/reprojection integration 实际为 skip；
  本报告没有声称它在真实 PostgreSQL 上运行。
- 本机没有 Docker，新增资产 Compose task、持久化计数与 API 重启投影没有容器实跑。
- PR 尚未推送，GitHub CI、0-skip integration 与 Compose smoke 尚无本 PR exact-SHA 证据。
- 未连接真实资产系统、StarRocks、Prometheus、Alertmanager 或模型 API；没有任何 E1 调用。
- 未部署、未 canary、未做真实用户问法验收，也未获得项目负责人 M6a 退出验收。
- 未实现列举、模糊/CIDR/range、跨环境选择、标签搜索、拓扑、巡检、写入、Grafana 或
  StarRocks 真实只读；M6b 入口条件与授权不因本 PR 自动满足。

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
