# M6a capability 扩展数据

> 本文件只记录可复核的扩展成本事实，不是验收报告。PR 1 已验收并合入；PR 2 已形成
> 本地数据采集提交，但尚未通过独立复审、远程 CI 或项目负责人验收。两个 PR 均未部署、
> 未 canary，也未连接真实 Alertmanager、Prometheus 或资产系统。

## PR 1：`prometheus.alert.evidence`

| 项目 | 事实 |
| --- | --- |
| base SHA | `a7dba18315b4213c8a61cdf492aca0cc951328bf` |
| 数据采集 head SHA | `c7eca0567d5e5ca18406f2fc2a79947db67f3439` |
| snapshot | `snapshot.m3.starrocks.slow_query.v1` → `snapshot.m6a.starrocks-prometheus.v1` |
| production policy | `policy-2026-09-01` → `policy-2026-09-05`；与两个有序 profile ID 成对固定 |
| 声明集合 | 新增 `prometheus.alert.evidence@1.0.0`；保留 `starrocks.slow_query.diagnose@1.0.0` |
| 总 diff | 87 files，`+7482/-267`；包含经批准的 1262 行实施计划，不把行数当成架构质量指标 |
| 测试文件 | 42 个新增或修改的 `tests/` 文件 |
| 测试收集 | 数据采集 head 共 2134 tests collected |
| M6a Prom eval | L0/L1/L2 共 51 个 corpus case、58 个实际 pytest case |
| 工时 | 未采集，无可靠来源 |

这里的 head 是完成代码、测试、能力地图与 ADR 补充后的数据采集点。之后仅有 README、
handoff 和本事实表的收口提交；最终送审 SHA 以 `git rev-parse HEAD` 与验收报告为准，
避免在提交自身内部伪造一个不可能自引用的 SHA。

### 执行核心改动

按计划口径单列 Runtime、Runner、Gateway、StepAdmission、跨边界 contracts 与哈希/
持久化面：8 个文件，`+340/-149`。

- `application/runtime.py` 的领域分支被 binding registry 取代；不是新增 Prom 分支。
- `runners/deterministic.py` 从 operation metadata 派生 gateway、消费持久化 step result，
  并把证据契约失败提交为 `MALFORMED_ADAPTER`。
- `governance/step_admission.py` 增加 SQL/PromQL 互斥信封准入，并独立核对声明 gateway。
- `tools/gateway.py` 对错误类型的 admission fail-closed；E1 硬闸未开启。
- contracts 只增加 `OperationSpec.gateway`、PromQL surface/rejection 与已有结果条件所需
  导出；`PlanStep`、plan schema、canonical hash 字段未改变。
- persistence 文件 0 个改动。

### 注册与装配面改动

按计划指定的 intent、Registry、profiles、default bindings、local stack、能力地图生成器
和生成文档计：7 个文件，`+495/-37`。

- intent 从全局槽位扫描改成每意图 allowlist，并登记 Prom 告警证据的窄问法。
- Registry、policy profile 和 default binding 各显式登记一次能力；没有动态发现或
  adapter fallback。
- local stack 显式装配 Alertmanager/Prometheus 两个 recording adapter；synthetic
  metric catalog 只覆盖默认 30 分钟窗、装配时刻前后 120 分钟的固定样例；非默认窗口
  或过期查询 fail-closed，不提供 fallback。
- 能力地图生成器新增 `gateway` 列；文档由 Registry 快照逐字生成。

### capability 专属规则

- 参数：只认 `HostHighCpu`、`InstanceDown`，精确 instance，可选 fingerprint，固定
  30 分钟默认窗、60 秒步长、5 series、361 points/series 上限。
- target：只由受信 context 和规范化告警/instance 构成；只允许 fake 目录中的
  `dev`/`test`。
- planner：固定 `Alertmanager get_active_alerts → Prometheus query_metric_range` 两步；
  第二步只在持久化的第一步结果为 `OK` 时执行。
- PromQL：只允许 CPU percent 与 instance up 两个代码内模板；准入期从参数重编译并
  逐字比对，不接受 raw expression。
- evidence：告警九字段和指标五字段白名单；指标点必须有限、窗口内、每 series 严格
  递增，最终只输出确定性摘要。
- answerability/render：唯一告警且有指标才充分；多告警要求 fingerprint；只陈述证据，
  不自动宣称根因。
- adapters：tenant、environment、operation 与全部参数共同组成精确 recording key；
  未命中即失败，无跨 scope 或默认回退。

### 重复形状与 DSL 判断

复用形状包括：`CapabilitySpec`/binding 注册、受信 context 目标解析、确定性 plan、
ToolPolicy、Gateway、EvidenceEnvelope、AnswerabilityVerdict、RenderPayload 和分层 eval。
Prometheus 独有的是双 gateway 条件计划、PromQL 模板 guard、时序点摘要与多告警消歧。

PR 1 只能证明最小 binding seam 可以承载第二个异质能力，尚不能证明三个同类能力有
稳定重复。因此 ADR-004/通用 DSL 继续延期；必须等 PR 2 在已验收 PR 1 基线上独立完成
并补齐第二组数据后再判断。

### 评审与返工

- 计划首审指出 snapshot、StarRocks 版本例外、注册面计数及四组安全/文件清单缺口；
  计划回写后，项目负责人转述复审通过并授权开工。
- 实现自审发现 StepAdmission 原先只信任 Runner 派生 gateway；已补独立声明比对。
  该测试首先暴露合成写夹具自身的 gateway 错配，最终改为从同一快照派生，未放宽闸门。
- 9 个隔离变异逐项拆掉 PromQL、gateway、条件、snapshot、恢复漂移、字段白名单、
  binding、入口中立性与 StarRocks version 承重点，对应用例均真实转红；变体已删除。
- 独立审查在 `300fa2e2` 上复跑四门并做 3 个变异反证，指出 production policy
  revision 未随 profile 集合递增这一合入前缺陷，以及 local catalog、Worker import
  护栏和未执行 PostgreSQL/Compose 路径；本轮按根因修复并补反例。修复提交形成时的
  外部复审与 CI 状态见下一条；部署、canary 与用户验收仍未发生。
- `cfd63923` 复审无剩余代码阻断；PR #11 run `33966437003` 八个 job 全绿，integration
  `2139 passed`、0 skipped，Compose 输出 `compose-smoke: passed`。这只补齐 GitHub
  隔离 runner 的非生产运行证据；部署、canary 与用户验收仍未发生。

## PR 2：`asset.inventory.lookup`

| 项目 | 事实 |
| --- | --- |
| base SHA | `32826af1ae70c7d88e3e7406d4cf2570f1157d2f`（含 PR #12 seam 修复与 PR #13 closure） |
| 数据采集 head SHA | `5a2bdec9902f5955b9e61a7fae3ffeb9daae8959` |
| snapshot | `snapshot.m6a.starrocks-prometheus.v1` → `snapshot.m6a.starrocks-prometheus-asset.v1` |
| production policy | `policy-2026-09-05` → `policy-2026-09-05.2`；与三个有序 profile ID 成对固定 |
| 声明集合 | 新增 `asset.inventory.lookup@1.0.0`；保留 StarRocks 与 Prometheus 既有版本 |
| 总 diff | 45 files，`+3002/-31`；测试与 eval 占主要行数，不把行数当成架构质量指标 |
| 测试文件 | 27 个新增或修改的 `tests/` 文件 |
| 测试收集 | 数据采集 head 共 2322 tests collected |
| M6a asset eval | L0/L1/L2 共 53 个 corpus case、59 个实际 pytest case |
| 工时 | 未采集，无可靠来源 |

数据采集 head 包含代码、测试、ADR、README 与生成能力地图。其后只增加本事实表、验收
报告和当前 handoff；最终送审 SHA 以 `git rev-parse HEAD` 为准，不在提交自身中写一个
必然落后的自引用 SHA。

### 执行核心改动

按计划口径检查 Runtime、Runner、Gateway、StepAdmission、生命周期/持久化、跨边界
contracts 与 canonical hash：**0 个文件，`+0/-0`**。

- PR 2 没有加入 capability 分支、第二套执行生命周期、adapter fallback 或新 hash 字段。
- `asset.inventory.lookup` 直接消费 PR 1 binding seam 与已独立合入的 Evidence target seam。
- 上游 target seam 修复属于 PR #12，已经在本 PR base 中；没有被藏进 PR 2 的成本统计。

这证明的是“执行 seam 对第三个异质能力可复用”，不是“新增能力没有共享改动成本”。

### 注册与装配面改动

计划指定的五个注册/装配真源加生成能力地图共 6 个文件，`+183/-17`：

- `capabilities/intent.py`：登记资产窄意图、三类 selector 槽位闭集与 near-miss 排除面；
- `capabilities/registry.py`：登记 capability 并递增 snapshot ID；
- `governance/profiles.py`：登记只读 operation/environment 允许面并递增 policy revision；
- `application/default_capabilities.py`：组合 planner、Evidence、Answerability 与 renderer；
- `interfaces/local_stack.py`：显式装配唯一资产 recording adapter；
- `docs/CAPABILITIES.md`：从最终 Registry snapshot 生成第三个能力条目。

五个注册点的重复编辑是真实维护成本。它不否定 seam 复用，也不能从统计中删掉后宣称
“零共享改动”。

### capability 专属规则

- 参数：恰好一个 `asset_id`、`hostname` 或 `ip`；NFC、hostname lowercase/末尾点、
  `ipaddress` compressed 规范化；拒绝 wildcard、CIDR、range、zone id、端口与多 selector。
- target：tenant/environment 只来自 `RequestContext`；资源键是 selector kind + canonical
  value；只允许 fake 目录的 `dev`/`test`。
- planner：固定一个 `lookup_asset` 只读步骤，`field_set_id=asset.summary.v1`、`limit=2`，
  用 0/1/2 行区分未命中、唯一与歧义。
- evidence：只保留九个目录字段；校验已准入 target、environment、精确身份与 canonical
  hostname/IP；额外 credential、secret、tags、notes 与自由文本全部丢弃。
- answerability/render：只有一条合法事实才成功；0/2 行、timeout、malformed、scope 或
  identity 冲突均 `INDETERMINATE`；不生成巡检、健康分、拓扑或根因结论。
- adapter：recording key 精确包含 tenant、environment、selector kind/value；未命中即失败，
  不跨 scope、不模糊匹配、不列举。

### 重复形状与 DSL 判断

PR 2 复用了 `CapabilitySpec`、binding registry、受信 target、单一 StepAdmission/Gateway、
EvidenceEnvelope、Answerability、RenderPayload 与 L0-L2 驱动形状。资产独有的是精确 selector
规范化、单行唯一性、九字段目录白名单；Prometheus 独有的是双 gateway 条件计划、固定
PromQL 与时序摘要；StarRocks 独有的是 SQL AST 与慢查询诊断。

因此，M6a 的事实支持两个结论：

1. 显式 binding seam 已能承载三个异质能力，暂时没有必要以 DSL 替换执行核心；
2. 每加一个能力仍重复编辑五个注册/装配真源，存在维护成本，但目前没有“三个同类能力”
   证明一套声明式 DSL 能同时覆盖其语义、失败方式、policy、Evidence 与 eval。

**ADR-004 / 通用 capability DSL 继续延期。** 若未来至少三个同类能力再次稳定重复这些
注册动作，应另立 ADR 评估窄的声明式注册；不得在 M6a 内实现动态 discovery、任意 executor
或削弱现有显式审查面。

### 评审、返工与反证

- A1/A2 后发现 Evidence builder 取不到已准入 target，按计划暂停；PR #12 独立修复，
  PR #13 记录合入事实，PR 2 再快进到 `main@32826af`，没有在资产层复制 scope 参数绕过。
- 首次全量门出现 2 条生命周期用例失败：两处手工 `CapabilityBindingRegistry` 仍只装配
  StarRocks + Prometheus。扫描全部构造点后补齐资产 binding，保持“snapshot 与 bindings
  精确相等”的闸门不变；复跑为 2168 passed / 154 skipped。
- A4 自审发现初版 L1 没真正驱动多 selector/非法 hostname/非法 IP，L2 前三条都走 hostname；
  已按计划矩阵改成 asset ID/hostname/IP 三条真实闭环与对应失败输入。
- 本轮 TDD 红灯包括：A3 三个纯模块缺失导致 3 个 collection error；A4 recording 模块缺失
  导致 1 个 collection error；Compose 新 helper 缺失导致 4 failed；均在最小实现后转绿。
- 五个 detached-worktree 变异分别撤掉 scope key、放行 `credential`、回退 snapshot ID、跳过
  恢复期 plan hash、回退 policy revision，依次得到 2/1/1/1/1 条失败；还原后临时工作树
  干净并已删除。第一次 scope 变异误加载主 worktree 的 editable package 而全绿，随后用
  显式 `PYTHONPATH` 核对模块路径并得到预期 2 红；该次全绿不是覆盖结论。
