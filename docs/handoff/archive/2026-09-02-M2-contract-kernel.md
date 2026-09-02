# M2 契约内核归档（2026-09-02）

> 本文件是 M2 验收通过后的历史归档。当前有效状态仍以项目根目录的 `AGENT_HANDOFF.md` 为准。

## 1. 归档结论

- M2 已由项目负责人明确声明“验收通过，归档”。
- 最终验收对象：`319253aec7bbdda1bd4f7b661dc8938ae58ac18e`。
- M2 的实现与补修均已合入 `main`，合入方式均为 fast-forward，无合并提交、无 SHA 改写。
- M2 只交付契约内核、fake 与契约/安全测试；不声明任何真实业务能力、外部系统调用、部署、canary 或用户验收。

## 2. 范围

M2 已落地后续模块共同使用的契约内核：

- 核心 DTO / 错误模型：请求、上下文、意图草稿、能力与候选集、执行计划、策略/审批、工具调用与结果、外部内容、证据、任务结果、渲染载荷、AgentError、AnswerabilityVerdict。
- 稳定指纹：canonical JSON、`plan_hash`、`target_fingerprint`、`tool_call_hash`、请求去重摘要。
- 协议边界：`ToolGateway`、Capability Registry / Resolver、Workflow Runner、TaskStore 交互形状。
- fake 实现：fake ToolGateway、fake TaskStore、fake / scripted Runner。
- 安全契约：adapter 只能返回内部 `AdapterResponse`；`ToolResult` 与 `AdmissionCertificate` 只能由受控入口签发；E1 硬闸关闭；ExternalContent 永远不可信；Reflection 只判断可答性，不携带执行权限字段。

明确非目标：

- 不接入真实数据库、模型 SDK、外部工具或真实运维系统。
- 不实现 LangGraph、ReAct、多 Agent 或通用 Capability DSL。
- 不实现 M3 的 `CapabilityResolver`、`PlanCompiler`、`StepAdmission`、`ToolPolicy`、`SQLGuard`、`ApprovalGate`、`EvidenceBuilder`、Reflection 消费链或 `XiaoweiRuntime`。

## 3. 合并与审查事实

| 事项 | 事实 |
| --- | --- |
| M2 基线 | `d0971666` |
| M2 契约内核 PR | [#3](https://github.com/shixian66/xiaowei-agent/pull/3) |
| PR #3 合并对象 | `527cd7fc0a85570104647d89da5694fef0bcbbca` |
| 合并后补修 PR | [#4](https://github.com/shixian66/xiaowei-agent/pull/4) |
| PR #4 最终对象 | `319253aec7bbdda1bd4f7b661dc8938ae58ac18e` |
| M2 受审提交范围 | `git log --oneline --reverse d0971666..319253a` |
| M2 计划修订史 | `origin/claude/m2-plan`（`eac46f4`，4 个提交：V2 → V2.1 → V2.2 → V2.3，对应 Codex 三轮计划审查）。该分支**不并入 `main`**：计划终稿已随实现分支进入 `docs/plans/M2-contracts-kernel.md`（内容逐字节一致），合并只会产生一个无内容变化的合并提交，破坏本项目「无合并提交」的性质。它作为归档 ref 长期保留，**清理分支时不得删除** |

该范围共有 26 个提交：

```text
ce8a9d3 docs(adr): ADR-009 冻结 plan_hash、审批绑定与工具准入形状
93cb78d refactor(redaction): 脱敏规则下沉为叶子模块，log 反向复用，对外签名不变
1c8d0d5 feat(contracts): 深不可变基座、封死校验绕过通道、18 个共享枚举与不可信外部文本
a90b534 feat(contracts): 执行上下文五项必填、意图污染防护、能力声明与候选集去歧义
8f0b1c6 feat(contracts): 单 capability 计划、闭集分支条件、目标规范化与审批/准入绑定
84bfb06 feat(planning): 三个确定性指纹、NFC 碰撞拒绝与字段覆盖完备性承重
ce5fe32 feat(capabilities): E1 分类唯一真源、唯一步骤构造器与整计划准入前重算
942226d feat(governance): 审批绑定与 policy revision 的确定性校验函数
0efa30a feat(tools): 调用内容绑定的准入凭证、私有 ToolResult 工厂与 E1 硬闸
f65ece7 feat(contracts): 只读证据信封与 Reflection 五字段边界
dff7701 feat(persistence): 闭合的 lease/fencing 规则、作用域化幂等与终态保护
0d8d6b4 feat(runners): Runner 契约与 fake 替身；docs: 落点表、README 与 handoff 同步
63d3801 fix(contracts): 严格性提到基类，封堵 lax 转换、fencing 与 Gateway 异常三条泄漏
5cb71dc fix(contracts): 字符串类型按语义分三档，封堵可选字段的裸 str 与 capture 的先派生后校验
4319dd3 docs(handoff): 验证记录绑定到确切 SHA，修正已过期的提交数与测试数
c689d6c fix(contracts,governance): 封堵拒绝路径的原文泄漏与准入凭证的公开伪造
5d6b1d5 docs(handoff): 验证记录更新到 c689d6cd
e776c18 fix(gateway,planning,contracts): 拒绝路径不回显输入提升为全项目不变量
a98acc9 docs(handoff): 验证记录更新到 e776c180
ac38046 fix(redaction,persistence,runners): 渲染 helper 捕获 BaseException，raise 卫生扫描覆盖非插值参数
e8e75ae test(redaction): 让 safe_exception_text 的外层 BaseException 护栏真正承重
56ef599 docs(handoff): 验证记录更新到 e8e75ae
b07eb7f docs(handoff): 收尾 M2 合并后事实，记录合并 SHA 与准入边界
527cd7f fix(security): 修复 secret-scan 失败，并把「伪造字面量拆开写」从口头约定变成本地 gate
f605dda fix(log,contracts,planning,tools): 补齐三条拒绝路径泄漏；类型名不再当作常量
319253a docs(handoff): 修正验收状态不一致，合并对象改为实际的 527cd7f
```

## 4. 已验证

- 本地 `main`、远程 `origin/main` 与 M2 补修分支同指 `319253aec7bbdda1bd4f7b661dc8938ae58ac18e`（归档提交前）。
- PR #4 已合并；`headRefOid` 与 `mergeCommit.oid` 均为 `319253aec7bbdda1bd4f7b661dc8938ae58ac18e`。
- `main` 独立 CI run `33629416660` 为 success，包含 tests、security-gate、lint、types、deps-audit、secret-scan 六项。
- 在 `319253a` 上，本地执行 ADR-008 四条命令：
  - `python -m pytest -q` → 640 passed / 3 warnings
  - `python -m pytest -m security -q` → 499 passed / 141 deselected / 3 warnings
  - `ruff check .` → All checks passed
  - `mypy src` → Success: no issues found in 46 source files
- 重点安全补修复验：
  - hostile object / reject path / gateway 边界测试 → 44 passed
  - secret-shaped literal / workflow policy 测试 → 31 passed
  - `git diff --check d0971666..319253a` → 无输出

## 5. 只读推理

- M2 的契约、hash、fake 与 TaskStore 交互形状足以作为 M3 首条闭环的共同接口；该判断来自源码、测试和架构一致性审查，尚未被真实闭环消费验证。
- M4 PostgreSQL TaskStore 可沿 `expected_version`、CAS winner、lease、fencing 的调用形状实现，不需要改变 Runner 已写好的交互接口；该判断要到 M4 才能用跨进程/数据库级测试证实。

## 6. 未覆盖

- 未部署、未 canary、未用户验收。
- 未连接任何外部系统：StarRocks、Prometheus、资产系统、PostgreSQL、Docker Compose、任何模型 API；E1 调用恒为 0。
- 没有可执行业务能力：M2 的能力状态仍是 declared。
- `tests/integration/`、`tests/evals/`、`docs/CAPABILITIES.md`、Compose、API、Worker、数据库迁移均尚未建立。
- `governance/binding.py` 是纯校验函数，不是 `ApprovalGate`；真实审批中断/恢复链路属于 M3 及后续。

## 7. 残余风险

- Python 私有性不是语言级安全边界：`object.__setattr__` 与重定义模块仍可绕过 `ToolResult` / `AdmissionCertificate` 的私有构造约定，只能由评审、源码扫描和契约测试压住实用路径。
- `StepConditionKind` 四成员闭集是否覆盖 M3 的真实条件取数需求，要到 M3 才能验证；不足时必须评审改枚举，不得改成开放表达式。
- `InMemoryTaskStore` 的 CAS 只在单进程内成立；跨进程原子性、崩溃恢复与隔离级别要到 M4 PostgreSQL 实现才可证。
- M2 的安全保证目前停留在契约层与纯函数层，尚未被首条真实闭环消费。
- 分支保护仍缺失（private + GitHub Free），红灯 PR 仍可被人工合并；这是 M1 已接受的残余风险。

## 8. 后续入口

下一步是 M3。开始 M3 前必须重新读取最新 `AGENTS.md`、`ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`，重新核对 `main`、SHA、工作树、CI 与 M2 归档事实；不得依赖本归档摘要直接开工。
