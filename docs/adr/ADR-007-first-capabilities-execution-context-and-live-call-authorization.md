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

非目标（三者共同）：kill query、修改参数、任意 E1 操作、跨 capability 自动扩张计划、伪确定性根因结论。

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
| B1 | 模型 provider adapter 的**实现与离线测试**（不发起任何网络调用） | 允许在其所属里程碑内实现，必须以 fake/recording 驱动测试；不得在 CI 或默认配置中启用真实调用。实现权**不等于**调用权 |
| B2 | 真实模型 API 的**网络调用**（任何外部 LLM 供应商） | **M0-M6a 全程禁止**，且 M6a 结束后**不自动开放**：必须另设独立里程碑，并单独批准 ADR、凭证引用、数据范围和保留策略后才允许。任何「M5 后单独决策」只指 B1 的实现决策，不授予 B2 的调用权限，也不得与本表 M0-M6a 的禁止期冲突 |
| C | 本地隔离基础设施（本地 PostgreSQL、本地 Docker Compose） | **M4 / M5 经各自里程碑批准后允许**；仅限本地隔离实例，不连共享或生产实例，不承载真实业务数据 |
| D | 非生产真实只读（StarRocks 测试环境） | **M6b，仍需单独授权**：环境、只读账号的 secret reference、查询范围、调用时窗、脱敏方式、recording 删除方式和证据保留周期均须逐项确认 |
| E1 | **任何可能修改被管运维目标状态的操作**（含非生产环境），与是否经 `ToolGateway`、是否被标记 `side_effect=True` 无关；完整定义见 D7 | **M0-M7 全程禁止**。M8 才可开放一条低风险的测试环境受控写，且必须同时满足 D6 的三项条件，缺一即保持禁止 |
| E2 | 生产连接与生产写 | **默认禁止**，不在本 ADR 授权范围内。生产写需要新的独立授权和独立验收计划，**不能由 M8 的测试环境结论推导得出** |

CI 全程不持有任何凭证，不执行 A、B2、D、E 类调用；**E1 调用次数恒为 0**。CI 对基础设施的写权限按 D8 的里程碑时点逐级开放：M1-M3 只允许写测试产物，M4/M5 各自批准后才允许对应 CI job 使用本地隔离 PostgreSQL / Compose。

### D5 变更门

更换首批能力或放宽任一层调用许可，必须先修订本 ADR，不得通过代码默认值、配置项或 feature flag 静默放宽。

### D6 写权限的开放条件

E1 默认关闭。开放 M8 的**唯一一条**低风险测试环境受控 E1 能力，必须同时满足下列三项，缺一则 M8 保持阻塞：

1. **ADR-005 已定稿**：审批主体、渠道、有效期、拒绝/过期/冲突语义、policy revision 绑定和应急关闭开关均已记录。
2. **项目负责人已批准**该次 E1 能力的范围、环境和回滚方式。
3. **本 ADR 已记录明确的 E1 例外或完成修订**，写明允许的 capability、环境、目标范围和失效条件。

生产连接与生产写不在上述例外范围内：即使 M8 全部通过，仍需新的独立授权和独立验收计划。**任何环境的 E1 操作都不得通过配置项、feature flag 或默认值静默开启。**

### D7 E1 的精确范围

**E1 按后果定义，不按路径或标记定义。**

**属于 E1**：**任何可能修改被管运维目标**（StarRocks、Prometheus、资产系统、MySQL、Kafka、Kubernetes 等）**状态的操作**。这一判定与下列因素**无关**：

- 该操作是否经过 `ToolGateway`；
- 该操作是否被标记为 `side_effect=True`；
- 是否发生了本地磁盘写入。

绕过 `ToolGateway` 直接持有并调用第三方客户端、或借内部持久化通道代理触达运维目标，**都不因绕过而逃出 E1**，而是同时构成 E1 违规与架构违规。

**合法 E1 执行的唯一形式**：经 `ToolGateway`，且对应计划步骤 `side_effect=True`，并已通过 `StepAdmission` 的 `ToolPolicy → SQLGuard → ApprovalGate`。任何其他形式的 E1 都是违规，不存在「合规的例外路径」。

**分类来源必须确定性**：`side_effect` 与 `effect_class` 只能由**版本化的 `CapabilitySpec` / operation metadata 确定性派生**。模型输出、用户输入、`IntentDraft`、外部文本和 adapter 自身**都不得设置、覆盖或降级**这两个字段；adapter 尤其不得把已声明的写操作在运行时降级为只读。

**fail-closed 规则**：出现下列任一情况一律拒绝执行，不得放行、不得降级、不得按只读处理：

1. operation 的 `effect_class` 未知或未在 CapabilitySpec 中声明；
2. CapabilitySpec 声明与计划步骤标记冲突；
3. 已注册为写的 operation 被标记为 `side_effect=False`（写操作误标只读）；
4. 分类信息来源不可信或版本无法确定。

**不属于 E1**：

1. 系统自身状态的内部持久化：TaskStore 的任务与状态迁移、approval 记录、audit 事件、evidence 索引与脱敏摘要。
2. 本地数据库 migration（Alembic upgrade/downgrade）。
3. 测试 fixture、recording 文件、覆盖率与报告等测试产物的读写。

这三类**本身不构成 E1**，但它们**不因此自动获得基础设施许可**：使用本地隔离 PostgreSQL 或 Compose 仍须按 D4 的 C 层取得对应里程碑批准。具体时点见 D8。

**反绕过约束**：内部持久化通道不得被用作绕过 `ToolGateway` 修改运维目标的代理通道。任何以写 TaskStore、audit、evidence 或 migration 为名、实际触达被管运维目标的路径，一律按 E1 认定并禁止。

**必须承重的测试断言**（M2 与 M3 测试门）：

1. 已注册为写的 operation 若被伪标为 `side_effect=False`，`StepAdmission` 必须拒绝，且 **`ToolGateway` 调用次数与 adapter 调用次数均为 0**。
2. 模型输出、用户输入或 adapter 尝试设置/覆盖/降级 `side_effect` 或 `effect_class` 时，必须拒绝并 fail-closed。
3. `effect_class` 未知、未声明或与 CapabilitySpec 冲突时，必须拒绝而非按只读放行。
4. 在 M0-M7 的任何路径上，对被管运维目标的 E1 调用次数为 0。

### D8 CI 基础设施授权时点

CI 使用基础设施的权限按里程碑逐级开放，不得提前：

| 阶段 | CI 允许写 | CI 不允许 |
| --- | --- | --- |
| M1-M3 | **仅测试产物**（fixture、recording、覆盖率、报告） | **不使用 PostgreSQL，不使用 Compose** |
| M4 起（经 M4 批准） | 测试产物 + **本地隔离 PostgreSQL**（仅对应 CI job） | 共享或生产实例、真实业务数据 |
| M5 起（经 M5 批准） | 上述 + **本地隔离 Compose**（仅对应 CI job） | 共享或生产实例、真实业务数据 |

**所有阶段共同约束**：CI 不持有任何凭证；CI 的 **E1 调用次数恒为 0**；CI 不执行 A、B2、D、E 类调用。

## 后果

- M3 可以立项，且其外部依赖为零，不被外部许可反向阻塞。
- M4/M5 的本地数据库与 Compose 集成测试有明确许可依据，消除了「禁止任何真实连接」与「真实 PostgreSQL 集成测试」之间的口径矛盾。
- M6b 与 M6a 解耦：外部许可未获批不阻塞内部能力扩展。
- 写权限在 M0-M7 全程关闭，M8 的开放条件是显式三项门而非默认演进；生产写与测试环境写是两次独立授权。
- E1 按「是否可能修改被管运维目标」定义，因此绕过 `ToolGateway` 的直接客户端调用和内部持久化代理旁路都无法逃出 E1；同时 TaskStore 持久化、本地 migration 和测试产物本身不构成 E1，但其基础设施许可仍受 D8 的里程碑时点约束。
- `side_effect` / `effect_class` 的确定性派生与 fail-closed 规则给了 M2/M3 四条可执行的承重断言，E1 边界不再只依赖文字约定。
- 模型 provider adapter 的实现进度不再隐含真实调用许可，避免 M5 之后被解读为自动获得网络调用权。
- `ARCHITECTURE.md` 核心契约表需同步收敛字段名，属本 ADR 的直接后果。

## 备选方案与否决理由

- **多能力横向铺开**：在没有一条完整闭环前无法证明契约正确，返工面积大。
- **M3 直接接入真实只读**：把内部进度绑定到外部授权流程上。
- **先实现通用 capability DSL**：V1 明确非目标；无三个同类能力的复用数据前，抽象缺乏依据。
- **新增独立的租户 ADR**：初始执行上下文正是这三个能力目标解析与 `target_fingerprint` 的输入，拆开会产生两处真源。

## 回滚

撤销本 ADR 即回到「首批能力与调用许可未拍板」状态，M3 不得立项。回滚必须以修订本 ADR 的方式显式记录，不通过修改代码默认值实现。
