# M6a capability 扩展数据

> 本文件只记录可复核的扩展成本事实，不是验收报告。PR 1 尚未部署、未 canary、
> 未连接真实 Alertmanager/Prometheus，也未取得项目负责人验收；PR 2 资产能力尚未开始。

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
  护栏和未执行 PostgreSQL/Compose 路径；本轮按根因修复并补反例。修复后的外部复审、
  CI、部署、canary 与用户验收仍未发生。
