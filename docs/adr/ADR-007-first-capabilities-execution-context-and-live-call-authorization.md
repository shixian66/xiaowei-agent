# ADR-007：首批能力、初始执行上下文与真实调用许可

- 状态：Accepted Revision（2026-09-10）+ Accepted RI3 Amendment（2026-09-12；H 层仍未单独签认）
  + **RI5 Proposed Amendment（2026-09-13，待项目负责人重新接受）**
- 日期：2026-09-01；候选修订 2026-09-10、2026-09-12、2026-09-13
- 决策人：项目负责人
- 相关：[ARCHITECTURE.md](../../ARCHITECTURE.md)、[DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md)、[ADR-008](ADR-008-engineering-and-test-baseline.md)、[ADR-014](ADR-014-real-feishu-oauth-and-web-activation.md)、[ADR-015](ADR-015-real-model-provider-boundary.md)、[RI5 简化设计](../plans/RI5-local-web-admin-simplified-design.md)

> **修订状态说明**：§RI5 Proposed Amendment 尚未被接受。未经项目负责人重新接受前，D4 表中
> B2 与 G 的已接受口径继续有效，RI5 不得开工。ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订必须成套复核并重新接受；任一份未被接受，RI5 都不得开工。
> 接受该修订**不**授予任何真实网络调用许可——B2 的 RI3 PR 3E 现场 GO 与 F 的 RI2 现场 GO
> 仍是各自独立的硬门。

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

### 本次修订仍未完成的单独签认项

2026-09-10 已接受修订不是单纯编号整理：旧 E2 将“生产连接与生产写”一并默认禁止；本修订定义 H
层，允许 RI6 在逐项授权后启用生产只读 provider/目标，同时 E2 继续禁止生产写。这是授权面的实质
变化，必须由项目负责人明确知情并单独记录批准；本次 V2.4 修订与 RI1 离线开工批准都不能
代替该签认。未签认前，H 层仍是关闭的候选授权框架，不产生生产只读调用许可。

- [ ] 项目负责人明确批准：RI6 可按 H 层逐项目标开放生产只读网络调用；生产写仍由 E2 禁止。

未勾选时，RI6 只允许部署所有真实 provider 默认关闭的制品，或继续使用已授权 test-env 目标并把
证据严格保留为测试环境能力；不得建立新的生产只读连接。

### D4 真实调用许可分层

| 层 | 范围 | 许可 |
| --- | --- | --- |
| A | 真实运维目标系统（StarRocks、Prometheus、资产系统、MySQL、Kafka、Kubernetes 等） | **M0-M6a 全程禁止**；领域 adapter 只允许 fake/recording |
| B1 | 模型 provider adapter 的**实现与离线测试**（不发起任何网络调用） | 允许在其所属里程碑内实现，必须以 fake/recording 驱动测试；不得在 CI 或默认配置中启用真实调用。实现权**不等于**调用权 |
| B2 | 真实模型 API 的**网络调用**（任何外部 LLM 供应商） | **M0-M6a 全程禁止**，且 M6a 结束后不自动开放。RI3 候选仅限 Gemini Developer API `v1beta` + canonical origin `https://generativelanguage.googleapis.com` + `gemini-3-flash-preview` + 官方 `google-genai` structured-output API、worker-only secret、ADR-015 发送字段/历史闭集、意图 60 秒/最多两次 request、诊断 180 秒/一次 request、区域与保留/训练确认和现场 GO。用户选择不设本地费用硬上限，但必须保留调用次数闭集、usage 与供应商账户告警；该账户告警由 RI3 PR 3E 的现场进入条件和 runbook 核验，缺少非敏感 readback 证据时不得下达 GO。模型始终无执行权 |
| C | 本地隔离基础设施（本地 PostgreSQL、本地 Docker Compose） | **M4 / M5 经各自里程碑批准后允许**；仅限本地隔离实例，不连共享或生产实例，不承载真实业务数据 |
| D | 非生产真实只读（StarRocks 测试环境） | **M6b/RI4，仍需同一套单独授权**：环境、只读账号的逻辑 credential reference、查询范围、调用时窗、脱敏方式、recording 删除方式和证据保留周期均须逐项确认；RI4 是完成 M6b 延期现场门，不建立第二套授权清单 |
| F | 真实飞书 OAuth、长连接、消息与群成员网络调用 | RI1 只允许默认关闭的 adapter/装配与离线测试；RI2 必须逐项满足 M7 详细计划 §0.3.2 并取得现场 GO，最高证据为 `test-env verified` |
| G | Web Admin 配置治理与连接测试 | RI5 的 Admin 权限只管理不可变配置、逻辑 target/credential 名、测试请求、发布/readback/回滚；不授予任意 endpoint、文件路径、Gateway 调用或 E1。StarRocks 测试请求必须由独立 worker 复用正常 Runtime/Runner/Admission/Gateway 链；只有 `interfaces/local_stack.py` 可 import tools。该候选进程边界须由 ADR-016 显式修订 M7 的单执行进程口径，并由 TaskStore 持久化 `configuration_test` dispatch lane、普通/候选窄方法、列表过滤及领取事务二次核对承重后方可实施；lane 不进入用户或入口 DTO。独立 worker 不取得 Gemini key；固定无用户数据 Gemini probe 只由既有 task-worker 的窄 control port 执行 |
| H | 正式环境中的真实 provider 与生产只读目标网络调用 | RI6 可以先部署所有 provider 默认关闭的制品；启用任一 provider/只读目标还必须逐项批准精确 provider/目标、逻辑凭证名、tenant/environment/actor 范围、只读授权、数据处置/保留、变更窗口、canary 范围和现场 GO。test-env 证据只是进入条件，不自动授予生产网络调用权 |
| E1 | **任何可能修改被管运维目标状态的操作**（含非生产环境），与是否经 `ToolGateway`、是否被标记 `side_effect=True` 无关；完整定义见 D7 | **持续禁止，直到单独获批的受控写里程碑明确修订本 ADR。**RI1–RI6、只读接入、模型、渠道、Admin、部署或里程碑编号变化都不授予 E1；当前候选 M8 仍须同时满足 D6 三项条件 |
| E2 | 生产写 | **默认禁止**，不在本 ADR 授权范围内。生产写需要新的独立授权和独立验收计划，**不能由 M8 的测试环境结论或 RI6 的生产只读授权推导得出** |

CI 不持有任何测试环境、生产环境或运维目标系统凭证；仅允许 GitHub 自动签发、作用域限于本仓库、短生命周期的临时 `GITHUB_TOKEN`，且 workflow 权限固定为 `contents: read`、checkout 设 `persist-credentials: false`。CI 的 **E1 调用次数恒为 0**，不执行 A、B2、D、F、H、E1、E2 类真实调用，也不执行 G 的真实 provider probe。CI 对基础设施的写权限按 D8 的里程碑时点逐级开放：M1-M3 只允许写测试产物，M4/M5 各自批准后才允许对应 CI job 使用本地隔离 PostgreSQL / Compose。

B2 的 RI3 边界不因写进本表或取得离线开工许可而获得真实调用许可。项目负责人已批准
ADR-015/详细计划并于 2026-09-12 下达“开始 RI3”，当前只开放 default-off adapter 与
fake/recording 离线实现；首次 Gemini 网络调用仍须另给现场 GO。
Gemini 只能接收统一脱敏后的当前用户文本、explicit parent 中再次脱敏的用户文本/最终安全展示文本，
以及 `starrocks.slow_query.diagnose` 最多 20 行的独立字段投影；它不能接收 SQL、secret、连接/目标配置、
完整 Evidence/RenderPayload 对象或 provider 原始错误。模型或规则输出经本地复验后，作为 task 级
insert-once accepted intent 保存并重走 Resolver/Planner；
合法 advisory 另作 task 级 insert-once 展示事实，只有原任务终态后可见。已保存事实在恢复时复用；
provider 已收到但保存前崩溃时允许再次调用，这是模型无执行副作用下明确接受的 at-least-once 语义。
模型始终不能决定执行。

RI6 的生产部署不会自动开放表中任一网络能力：即使已有 test-env 证据，也只有逐项满足 H 层并由
负责人明确纳入本次部署范围的 provider/只读目标才能启用。生产 `IP:8080` 仅用于宿主端口发布；
OAuth/session 仍必须通过已批准 HTTPS SSO Host/Origin，直接 HTTP IP 不具备登录或受保护 session 权限。

### D5 变更门

更换首批能力或放宽任一层调用许可，必须先修订本 ADR，不得通过代码默认值、配置项或 feature flag 静默放宽。

RI5 Proposed Amendment 接受后，以下任一变化同样必须先修订本 ADR：让 Web 取得模型端口或参与
任务模型调用、扩大探针输入超出固定最小 synthetic 文本、让探针创建 Task/Evidence 或进入
`ToolGateway`、把 StarRocks 或任何运维目标纳入 Web Admin 配置或测试范围、让 `LOCAL_ADMIN`
之外的 principal 读写配置或发起探针，以及为 RI5 新增任何执行进程或 dispatch lane。

### D6 写权限的开放条件

E1 默认关闭。任何候选受控写里程碑（当前名称为 M8）要开放**唯一一条**低风险测试环境受控 E1
能力，必须同时满足下列三项，缺一则保持阻塞：

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
4. 在本 ADR 尚未记录获批受控写例外时，任何里程碑和任何路径上对被管运维目标的 E1 调用次数为 0。

### D8 CI 基础设施授权时点

CI 使用基础设施的权限按里程碑逐级开放，不得提前：

| 阶段 | CI 允许写 | CI 不允许 |
| --- | --- | --- |
| M1-M3 | **仅测试产物**（fixture、recording、覆盖率、报告） | **不使用 PostgreSQL，不使用 Compose** |
| M4 起（经 M4 批准） | 测试产物 + **本地隔离 PostgreSQL**（仅对应 CI job） | 共享或生产实例、真实业务数据 |
| M5 起（经 M5 批准） | 上述 + **本地隔离 Compose**（仅对应 CI job） | 共享或生产实例、真实业务数据 |

**所有阶段共同约束**：CI 不持有任何测试环境、生产环境或运维目标系统凭证；仅允许 GitHub 自动签发、作用域限于本仓库、短生命周期的临时 `GITHUB_TOKEN`，且 workflow 权限固定为 `contents: read`、checkout 设 `persist-credentials: false`。CI 的 **E1 调用次数恒为 0**，不执行 A、B2、D、F、H、E1、E2 类真实调用，也不执行 G 的真实 provider probe。

## RI5 Proposed Amendment（2026-09-13，待重新接受）

RI5 的实现方案已由[RI5 简化设计](../plans/RI5-local-web-admin-simplified-design.md)取代原方案：
取消独立配置测试 worker，改由 Web 自身读取本地配置并执行控制面连通性探针。该变化触及 D4 表中
**两行**，必须一并修订，不能只改 G 行。

本修订只改变 D4 表内 B2 与 G 的相关口径，不改变 A、B1、C、D、F、H、E1、E2 任何一层，也不改变
D5–D8。

### R1 修订 B2：任务模型调用仍是 worker-only，Web 只获得固定 synthetic 探针

B2 现行口径中的 **worker-only secret** 表述在"任务模型调用"这一语义上**保持不变**：
`IntentModelPort` 与 `SlowQueryAdvisoryPort` 仍只由 task worker 装配，Web、API、飞书 listener
和 channel worker 都不得取得模型端口，也不得参与任何任务的模型调用。

在此前提下，B2 增加一个窄例外：RI5 的 Web 进程可以读取 Gemini API key，**且仅用于**

- 配置管理本身（保存、校验、显示"已配置/未配置"）；
- 管理员明确点击触发的 `gemini_connection` 探针：使用 ADR-015 D1 固定的 provider、model、
  API version 与 canonical origin，输入为固定的最小 synthetic 文本。

该探针不创建 Task、TaskSubmission、Evidence 或 capability，不产生 `ToolResult`，不进入
Policy / SQLGuard / ApprovalGate / `ToolGateway`，不消费 ADR-015 D3 的出站白名单，不产生 D5 的
模型事实行，也不改变任何服务的 readiness。它不授予 Web 任意 prompt、任意 endpoint、任意 API
版本、任意文件路径或任何模型选择能力。

**调用许可不变**：探针仍属 B2 层真实网络调用，默认关闭，首次真实调用仍须 ADR-015 D8 的
**RI3 PR 3E 现场 GO**。未获 GO 时页面与接口只返回本地闭集禁用状态，外部调用数必须为零。
模型始终无执行权这一条不变。

### R2 修订 G：取消独立 worker 与 ADR-016，收敛为控制面探针

G 行现行口径中的以下内容由本修订**取代**：

- "StarRocks 测试请求必须由独立 worker 复用正常 Runtime/Runner/Admission/Gateway 链"；
- "该候选进程边界须由 ADR-016 显式修订 M7 的单执行进程口径"，以及随附的 TaskStore
  `configuration_test` dispatch lane、普通/候选窄方法、列表过滤与领取事务二次核对要求；
- "独立 worker 不取得 Gemini key；固定无用户数据 Gemini probe 只由既有 task-worker 的窄
  control port 执行"。

新口径：RI5 不新增执行进程，不新增 dispatch lane，**不纳入 StarRocks 配置或 StarRocks 测试
请求**。Web Admin 只管理 Gemini 与飞书两个闭集集成的配置，并只执行三个控制面探针
（`gemini_connection`、`feishu_credentials`、`feishu_oauth`）。因此 **ADR-016 不再需要**，
M7 "只有 task worker 装配完整执行 Runtime" 的口径**不被修改**——控制面探针不是执行 Runtime。

G 行以下内容**保持不变**：Admin 不授予任意 endpoint、任意文件路径、`ToolGateway` 调用或 E1；
只有 `interfaces/local_stack.py` 可 import tools；CI 不执行 G 的真实 provider probe。

新增约束：配置读取、保存与探针接口只接受 `IdentitySource.LOCAL_ADMIN` principal；飞书
principal 即使持有 `ADMIN_ALL_SAFE_TASKS` 也不得访问。Web 不得由此取得任何运维目标
（A/D/H 层）的访问权——RI5 范围内不存在通往运维目标的 Web 路径。

`feishu_credentials` 与 `feishu_oauth` 探针属于 F 层真实飞书网络调用，默认关闭，仍须
**RI2 现场 GO**；F 行本身不因本修订放宽。

### R3 未放宽的部分

本修订不授予 E1、不授予 E2、不改变 H 层生产只读的单独签认要求、不改变 D6 的写权限三项条件，
也不改变 D8 的 CI 时点。RI5 的所有探针在 CI 中调用次数恒为 0。

## 后果

- **凭证措辞修订（2026-09-01，经项目负责人批准）**：修订前的绝对化表述（宣称 CI 完全不接触任何凭证）在 GitHub Actions 下不成立——`GITHUB_TOKEN` 由平台必然签发，`actions/checkout` 与 `astral-sh/setup-uv` 都会使用。现收敛为「不持有测试、生产或运维目标系统凭证；仅允许 GitHub 自动签发、仓库范围、短生命周期的只读临时 token」。这是**收紧表述精度**，不放宽任何真实调用许可：A、B2、D、E 类调用与 E1 调用次数为 0 的约束不变。
- M3 可以立项，且其外部依赖为零，不被外部许可反向阻塞。
- M4/M5 的本地数据库与 Compose 集成测试有明确许可依据，消除了「禁止任何真实连接」与「真实 PostgreSQL 集成测试」之间的口径矛盾。
- M6b 与 M6a 解耦：外部许可未获批不阻塞内部能力扩展。
- 写权限不再依赖“M0-M7”这种会被插入里程碑破坏的编号区间：在本 ADR 明确批准受控写例外前
  持续关闭。RI1–RI6 不授予 E1；生产写与测试环境写仍是两次独立授权。
- E1 按「是否可能修改被管运维目标」定义，因此绕过 `ToolGateway` 的直接客户端调用和内部持久化代理旁路都无法逃出 E1；同时 TaskStore 持久化、本地 migration 和测试产物本身不构成 E1，但其基础设施许可仍受 D8 的里程碑时点约束。
- `side_effect` / `effect_class` 的确定性派生与 fail-closed 规则给了 M2/M3 四条可执行的承重断言，E1 边界不再只依赖文字约定。
- 模型 provider adapter 的实现进度不再隐含真实调用许可，避免 M5 之后被解读为自动获得网络调用权。
- 飞书、模型、StarRocks、Admin 和生产部署各自有独立许可层；任一 test-env 或部署证据都不能
  替另一层授权。Admin 的 StarRocks 测试也不能产生第二条工具执行真源。
- 正式制品可以在所有 provider 默认关闭时先部署；生产网络启用权由 H 层逐项授予，不从
  `test-env verified`、配置发布、进程 readback 或 canary 计划自动推导。生产写仍由 E2 单独禁止。
- `ARCHITECTURE.md` 核心契约表需同步收敛字段名，属本 ADR 的直接后果。

## 备选方案与否决理由

- **多能力横向铺开**：在没有一条完整闭环前无法证明契约正确，返工面积大。
- **M3 直接接入真实只读**：把内部进度绑定到外部授权流程上。
- **先实现通用 capability DSL**：V1 明确非目标；无三个同类能力的复用数据前，抽象缺乏依据。
- **新增独立的租户 ADR**：初始执行上下文正是这三个能力目标解析与 `target_fingerprint` 的输入，拆开会产生两处真源。

## 回滚

撤销本 ADR 即回到「首批能力与调用许可未拍板」状态，M3 不得立项。回滚必须以修订本 ADR 的方式显式记录，不通过修改代码默认值实现。
