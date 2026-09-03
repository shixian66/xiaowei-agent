# M3 首条 StarRocks 只读垂直闭环归档（2026-09-03）

> 本文件是 M3 验收通过后的历史归档。当前有效状态仍以项目根目录的 `AGENT_HANDOFF.md` 为准。

## 1. 归档结论

- M3 已由项目负责人明确声明「验收通过，可以合并」。
- 最终验收对象：`64d295c8e4f38028527ec9a496262c7a660258b1`。
- 以 fast-forward 合入 `main`，**无合并提交、无 SHA 改写**，19 个受审提交原样保留。
- M3 交付一条**只用 fake/recording 数据**的只读垂直闭环；不声明真实 StarRocks 连接、
  部署、canary 或用户验收。能力状态最强为 `tests`。
- 首轮 Codex 深档验收**打回**一条阻断项（`WorkflowRunner` 契约未闭合），按根因修复后复审通过。

## 2. 范围

M3 落地了从入口到渲染的完整确定性链路，能力为 `starrocks.slow_query.diagnose` v1.0.0：

```text
RequestEnvelope → IntentDraft → CapabilityResolver → PlanCompiler
  → WorkflowRunner(step) → StepAdmission(ToolPolicy → SQLGuard → ApprovalGate)
  → ToolGateway → Evidence → Answerability → RenderPayload
```

- **能力与目标**：`CapabilitySpec` 声明、`SqlSurface` 列白名单、`StaticCapabilityRegistry`、
  规则式 `IntentInterpreter`、目标解析与 `target_fingerprint` 稳定性。
- **确定性 SQL**：`SlowQueryParams` schema 与时间窗规范化、两条 SQL 模板、
  唯一的目标范围谓词构造器 `_scope_predicates()`、`typed_arguments` 往返。
- **安全**：SQLGuard 的闭集 AST 节点白名单 + 14 条规则 + 重编译比对；ToolPolicy 闭集
  profile；`NeverGrantingApprovalGate`；六段 `StepAdmission`。攻击矩阵 A1–A36 逐条有用例。
- **执行与证据**：`PlanStore`、append-only `EvidenceLedger`、纯证据构造器、
  `DeterministicStepRunner`（租约、CAS、fencing、可选只读分支、预算、`WorkflowPaused`）。
- **消费侧**：Answerability 判定表、`RenderPayload` 投影、`StructuredLogTraceSink` 与阶段归因、
  `XiaoweiRuntime` facade、L0–L2 离线 eval 语料与驱动。
- **契约修正**：`WorkflowRunner` Protocol 由 M2 的窄签名改为含活输入的签名（见第 3 节）。

明确非目标（均已守住）：

- 不连接真实 StarRocks，不发起任何网络调用；`_E1_EXECUTION_ENABLED` 保持 `False`。
- 不实现真实写能力或 E1；不把业务逻辑放进 Web、CLI 或 adapter。
- 不镜像旧项目 `ivor_aiops` 的代码依赖——它只作为行为与安全 oracle。

## 3. 合并与审查事实

| 事项 | 事实 |
| --- | --- |
| M3 基线 | `4e3e30941f9102dd814283f3e3c956f5e24bc699` |
| 最终验收对象 | `64d295c8e4f38028527ec9a496262c7a660258b1` |
| 合入方式 | `git merge --ff-only`，**无合并提交**；未走 PR |
| M3 受审提交范围 | `git log --oneline --reverse 4e3e309..64d295c` |
| 差异统计 | 86 files changed, 11995 insertions(+), 42 deletions(-) |
| 合并后 CI | `main` 上 run [`33708913738`](https://github.com/shixian66/xiaowei-agent/actions/runs/33708913738)，六个 gate 全绿 |
| M3 工作分支 | `claude/m3-starrocks-slow-query` 已合入 `main`，保留备查 |
| M3 详细计划 | `docs/plans/M3-starrocks-slow-query.md` V3，经两轮 Codex 审核批准，已随实现分支带入 `main` |
| M3 验收报告 | [docs/handoff/M3-acceptance-report.md](../M3-acceptance-report.md)，含四段格式与 12 项计划偏差 |

该范围共有 19 个提交：

```text
68ccc1d docs(plan): M3 首条 StarRocks 只读闭环实施计划 V3
43c9781 T0(m3): 引入 sqlglot 依赖、方言实证基线与双向分层元断言
2c9b4c0 T1(m3): capability 声明、SqlSurface、静态 Registry 与规则式 IntentInterpreter
5720b4e T2(m3): 目标解析与 target_fingerprint 稳定性
eb4c078 T3(m3): 慢查询参数 schema、时间窗规范化与 typed_arguments 往返
5298c7c T4(m3): 确定性 SQL 编译器与唯一的目标范围谓词构造器
b72b2fc T5(m3): DeterministicCapabilityResolver 与 PlanCompiler
b2f98c2 T6(m3): SQLGuard——闭集 AST 校验与重编译比对
821ba3e T7(m3): ToolPolicy、ApprovalGate 与六段 StepAdmission
ddf2a3c T8(m3): PlanStore、EvidenceLedger 与纯证据构造器
eaed170 T9(m3): DeterministicStepRunner、WorkflowPaused 与按调用内容回放的 fake adapter
dab358c T10(m3): Answerability 判定表与 RenderPayload 投影
957eb9d T11(m3): StructuredLogTraceSink 与 Runner 侧的阶段归因
02a65f9 T12(m3): XiaoweiRuntime facade、绕过反证与生成式能力地图
6d630ee T13(m3): L0-L2 离线 eval 语料与驱动
9f1aa7b T14(m3): 文档与 handoff 收口
7b25621 T14(m3): 去掉 approval.py 末尾多余空行
d05fc2b docs(m3): M3 验收报告
64d295c fix(contracts,runners,application): 闭合 WorkflowRunner 契约，收掉整条应用层的类型擦除
```

### 首轮深档验收的打回与修复

首轮送审对象 `d05fc2b` 被判**打回**，阻断项一条：真实 Runner 未实现 `WorkflowRunner`
Protocol，`XiaoweiRuntime` 把 runner 标成 `object` 并用 `type: ignore[attr-defined]`
调它——Runtime→Runner 这条边在类型层完全没有契约，而 `mypy src` 依然全绿。

**根因**：M2 把接口定为 `start(task_id)`，隐含「Runner 只要 task_id 就能推进任务」。
而 ARCHITECTURE §5.6 同时要求 Runner 在恢复时做漂移检测——判断暂停期间 policy 或目标
是否变了，必须拿调用方**当下重新解析**出的 `target` 与 `policy_revision` 去比对存储里
那份。这两项按定义不可持久化；两边都从存储读则比较恒真。又因 Runner 不拥有领域安全
规则，它不能自己重解析。**窄签名与 Runner 自身职责不相容，M2 契约是错的**；
`object` + `type: ignore` 只是这个矛盾被静音后的表现。按 AGENTS.md「发现 M2 契约错误时
先修 M2」处置。

**未采纳**复审方给出的备选方案（保持窄接口、把 plan/target/context 经存储传入）：那会让
漂移检测恒真，等于把一条安全检查静默改成空操作。

沿同一路径主动排查并一并修复的同类问题（均非复审方指出）：

| 位置 | 问题 |
| --- | --- |
| `application/runtime.py` 另 6 处 | `target`/`plan`/`outcome`/`verdict` 同样被擦成 `object` 再 `type: ignore[arg-type]`；现 `src/` 内 `type: ignore` 归零 |
| `_terminal_status` | `getattr(outcome, "status", INDETERMINATE)`——outcome 形状变了会**静默降级**为 indeterminate 而非报错，是对自己人契约的 fail-soft |
| `runners/deterministic.py:resume` | `external_input` 收下后**从不读取**：M2 为恢复定义的跨边界输入被忽略，实际授权走并行的 `approval` 参数——两条通道、一条静默 |
| 审批 ref 格式 | `deterministic.py` 与 `approval.py` **各写一遍** `f"{task_id}:{step_id}"`。任一处改了分隔符，暂停发出的 ref 就匹配不上恢复校验的 ref，而两处各自「自洽」，无测试会自然失败 |

## 4. 已验证

- 本地 `main`、远程 `origin/main` 与 M3 工作分支同指
  `64d295c8e4f38028527ec9a496262c7a660258b1`（归档提交前）。
- `main` 独立 CI run `33708913738` 为 success，包含 lint、types、secret-scan、tests、
  deps-audit、security-gate 六项。
- 在 `64d295c` 的 `main` 上，本地执行 ADR-008 四条命令：
  - `python -m pytest -q` → 1229 passed / 3 warnings
  - `python -m pytest -m security -q` → 823 passed / 406 deselected / 3 warnings
  - `ruff check .` → All checks passed
  - `mypy src` → Success: no issues found in 74 source files
  - 对照 M2 验收对象 `319253a` 的 640 / 499：全量 +589，安全 gate +324。
- 硬边界独立复核：
  - `_E1_EXECUTION_ENABLED: Final[bool] = False`（`tools/gateway.py:44`）
  - `git diff --stat 4e3e309..64d295c -- .github/` → 无输出（`ci.yml` 未改动）
  - `git diff --check 4e3e309..64d295c` → 无输出
  - `src/` 下无网络与数据库驱动引入；测试内 socket/DNS 被 `pytest-socket` 拦截
  - 新增测试中 `skip` / `xfail` / 注释掉的测试均为 0
  - 攻击矩阵 A1–A36 每条在 `tests/` 有可定位的对应用例
- **每个任务都做了 TDD 反证**（撤掉承重保护确认转红、还原确认转绿），逐条写在各任务提交
  信息里。其中**四次反证首轮全绿，暴露了真实的覆盖缺口**并已各自补测试：`result.applied`
  检查、`gateway.invoke` 的属性访问、sink 的常量消息、L0 语料里 A26/A29 两条纸面条目。
- 契约收口一轮的反证同样逐条执行：撤掉 `_verify_external_input` → 2 条转红；把审批 ref
  改回手写 f-string → AST 扫描条转红；把 `object` + `type: ignore` + `getattr` 原样退回
  `runtime.py` → 3 条转红。三者还原后均转绿。
- **B1（count 模板必须复用目标范围）由三层共 5 条用例承重**：把 count 改成只带窗口后，
  T4 的集合等式 3 组、T6 的 A36、L0 的 A36 同时转红。

## 5. 只读推理

- `PlanStore` 与 `EvidenceLedger` 的 Protocol 面刻意做窄，M4 换 PostgreSQL 实现时应只换实现
  不动契约；这是设计推理，**尚无任何跨进程运行证据**。
- 审计表名与 13 个列名来自旧项目 `ivor_aiops` 的实际下发 SQL，**未在真实 StarRocks 上核对过**。
- 闭集节点白名单「挡得住尚未想到的绕过写法」是原理推理，不是证明；它是对当前两条模板
  实测节点集合的断言。
- 规则顺序使审计原因最具体，基于「重编译比对最宽泛，因此排最后」的推理与一条断言，
  未做穷尽的顺序组合验证。
- near-miss 语料的问法代表真实误路由风险——语料由本轮构造，**没有真实用户问法样本**支撑。

## 6. 未覆盖

- 未部署、未 canary、未用户验收；能力状态最强为 `tests`。
- 未连接任何外部系统：真实 StarRocks、真实模型 API、PostgreSQL、Docker Compose；
  **对被管运维目标的 E1 调用恒为 0**。
- **`StepConditionKind` 四个成员只消费了两个**：`ALWAYS` 与 `EVIDENCE_ROW_COUNT_BELOW`
  已被真实闭环消费；`EVIDENCE_FIELD_ABSENT` 与 `PRIOR_STEP_RESULT_IS` **未被消费、未被验证**。
- **「审批通过后恢复并真的执行副作用步骤」这条路径未验证**，且在 M0–M7 永远不会走通
  （Gateway 的 E1 硬闸）。M3 只验证了控制流：无审批时暂停、恢复时重算指纹、漂移即拒绝。
- 跨进程并发、崩溃恢复、lease 过期后的抢占未验证：三个 In-Memory 实现只有单进程保证。
- 未创建 `tests/integration/`、`docker-compose.yml`、API、Worker、数据库迁移。
- 未验证真实 StarRocks 的语法差异：全部 AST 结论基于 sqlglot 30.17.0 的 starrocks 方言实现。
- 未做性能、并发吞吐与大结果集验证；`row_limit` 上限 200 的合理性未经真实负载检验。
- ADR-001 至 ADR-006 尚未编写。

## 7. 残余风险

- **审计表结构未在真实 StarRocks 核对**——表名、13 个列名、`state`/`errorCode` 取值域，
  以及刻意排除的 `clientIp`/`digest`/`stmt` 是否存在。**M6b 首次连接时必须用
  `SHOW CREATE TABLE` 核对并回修**；核对结果可能推翻 `SqlSurface` 的整张列白名单。
- **`sqlglot` 的 AST 形状随版本可能变化**：节点闭集是对**当前锁定版本 30.17.0** 的断言。
  升级必须重跑 `tests/unit/test_sqlglot_baseline.py` 与整个安全 gate。
- 三个 In-Memory 实现只有单进程保证：跨进程原子性、崩溃恢复与隔离级别要到 M4 才可证。
- `WorkflowPaused` 用异常表达暂停是对 Protocol 的一种解释（已获复审裁定接受）。若将来
  Runner 需要表达更多非终态，应重新评估返回类型而不是继续加异常。
- 方向判断阈值来自旧项目，**未在本项目的真实工作负载上校准**；因此一律带「疑似」，
  且只进渲染说明段、不进 `facts`。
- `evidence/` 的纯度、Runtime→Runner 的契约、审批 ref 的单一来源均由单条测试承重：
  删除对应测试即等于取消该保证。相关禁止盲改点已写入 `AGENT_HANDOFF.md` 第 7 节。
- **分支保护仍缺失**（private + GitHub Free），红灯 PR 仍可被人工合并、可强推 `main`；
  这是 M1 已接受的残余风险。M3 本次为直接 `--ff-only` 推 `main`，未走 PR。

## 8. 后续入口

下一步是 M4（PostgreSQL TaskStore 的并发、恢复与终态保护）。开始 M4 前必须重新读取最新
`AGENTS.md`、`ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`，
重新核对 `main`、SHA、工作树、CI 与本归档事实；**不得依赖本归档摘要直接开工**。

M4 应优先消费 M3 留下的两个新 port（`PlanStore`、`EvidenceLedger`）并验证其 Protocol 面
是否真的只需换实现；同时把第 6 节「跨进程并发、崩溃恢复、lease 抢占」三项从未覆盖转为已验证。
