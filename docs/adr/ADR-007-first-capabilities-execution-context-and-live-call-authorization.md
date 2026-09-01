# ADR-007：首批能力、初始执行上下文与真实调用许可

- 状态：Accepted
- 日期：2026-09-01
- 决策人：项目负责人
- 相关：[ARCHITECTURE.md](../../ARCHITECTURE.md)、[DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md)、[ADR-008](ADR-008-engineering-and-test-baseline.md)

## 背景

`DEVELOPMENT_PLAN.md` 把首批能力、租户模式和真实调用边界列为必须在 M0 拍板的事项。若不拍板，这些争议会以隐含默认值的形式沉到代码、配置或 feature flag 中，与 `AGENTS.md` 的「不把争议藏在代码默认值里」冲突。

同时 `ARCHITECTURE.md` 的核心契约表对同一执行上下文使用了 `tenant_id`、`tenant`、`env`、`environment_id` 四种写法，违反 `AGENTS.md`「禁止让同一契约在多个层用不同字段名表达」。

## 决策

### D1 首批三个能力

三个能力均先以只读 fake/recording 形式实现。

| capability_id | 版本 | 里程碑 | 边界 |
| --- | --- | --- | --- |
| `starrocks.slow_query.diagnose` | 1.0.0 | M3 | 输入为 `environment_id` + 有上限的时间窗 + 可选 `database`/`user`/`query_id`；SQL 由确定性 compiler 生成并经 `sqlglot` AST 白名单校验，不接收用户或模型原始 SQL；输出慢查询事实、采样时间、来源、限制和可复现参数 |
| `prometheus.alert.evidence` | 1.0.0 | M6a | 只允许 capability 注册的 PromQL 模板加具名参数，拒绝任意 PromQL |
| `asset.inventory.lookup` | 1.0.0 | M6a | 只做精确 ID / hostname / IP 解析，带租户与环境约束和字段白名单；必须专项验证 canonical target resolution、`target_fingerprint` 稳定性和环境/租户漂移拒绝 |

非目标（三者共同）：kill query、修改参数、任意写操作、跨 capability 自动扩张计划、伪确定性根因结论。

### D2 初始执行上下文

初始采用单租户开发模式；固定开发租户的具体取值在 M1 以非敏感配置常量给出。

- `RequestEnvelope.environment_id`：**可选**，表示渠道显式指定的环境。
- `RequestContext` 的 `tenant_id`、`actor`、`environment_id`：**三项均必填**，由 Gateway 解析后产生。
- 模块边界**显式传递 `RequestContext`**，不从全局变量或隐式默认读取。
- 其他 DTO **不机械复制**这三项上下文；各契约的精确字段归属由 M2 详细计划审定，本 ADR 不预先规定。
- 环境无法解析出唯一值时 fail-closed，不取默认环境。

### D3 字段命名统一

全项目统一使用 `tenant_id`、`actor`、`environment_id`，消除 `tenant`、`env` 等别名。`target_fingerprint` 的既有 canonicalization 已使用该组名称，收敛后自动一致。

### D4 真实调用许可分层

| 层 | 范围 | 许可 |
| --- | --- | --- |
| A | 真实运维目标系统（StarRocks、Prometheus、资产系统、MySQL、Kafka、Kubernetes 等） | **M0-M6a 全程禁止**；领域 adapter 只允许 fake/recording |
| B | 真实模型 API（任何外部 LLM 供应商） | **M0-M6a 全程禁止**；核心测试使用 fake interpreter |
| C | 本地隔离基础设施（本地 PostgreSQL、本地 Docker Compose） | **M4 / M5 经各自里程碑批准后允许**；仅限本地隔离实例，不连共享或生产实例，不承载真实业务数据 |
| D | 非生产真实只读（StarRocks 测试环境） | **M6b，仍需单独授权**：环境、只读账号的 secret reference、查询范围、调用时窗、脱敏方式、recording 删除方式和证据保留周期均须逐项确认 |
| E | 生产连接与任何写操作 | **全程禁止**，不在本 ADR 授权范围内 |

CI 全程不持有任何凭证，不执行 A、B、D 类调用。

### D5 变更门

更换首批能力或放宽任一层调用许可，必须先修订本 ADR，不得通过代码默认值、配置项或 feature flag 静默放宽。

## 后果

- M3 可以立项，且其外部依赖为零，不被外部许可反向阻塞。
- M4/M5 的本地数据库与 Compose 集成测试有明确许可依据，消除了「禁止任何真实连接」与「真实 PostgreSQL 集成测试」之间的口径矛盾。
- M6b 与 M6a 解耦：外部许可未获批不阻塞内部能力扩展。
- `ARCHITECTURE.md` 核心契约表需同步收敛字段名，属本 ADR 的直接后果。

## 备选方案与否决理由

- **多能力横向铺开**：在没有一条完整闭环前无法证明契约正确，返工面积大。
- **M3 直接接入真实只读**：把内部进度绑定到外部授权流程上。
- **先实现通用 capability DSL**：V1 明确非目标；无三个同类能力的复用数据前，抽象缺乏依据。
- **新增独立的租户 ADR**：初始执行上下文正是这三个能力目标解析与 `target_fingerprint` 的输入，拆开会产生两处真源。

## 回滚

撤销本 ADR 即回到「首批能力与调用许可未拍板」状态，M3 不得立项。回滚必须以修订本 ADR 的方式显式记录，不通过修改代码默认值实现。
