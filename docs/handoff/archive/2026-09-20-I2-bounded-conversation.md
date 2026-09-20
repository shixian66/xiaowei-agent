# I2 限定领域普通对话通道归档（2026-09-20）

> 本文件承载 I2 PR #53–#56 离线实现范围验收后的历史归档。当前有效状态仍以项目根目录的
> `AGENT_HANDOFF.md` 为准；设计口径见
> [ADR-017](../../adr/ADR-017-intelligent-interaction-and-clarification.md)。
>
> 文中的 SHA、run id 与测试数字是归档时已核对的历史事实，不随 `main` 后续前进而改变。

## 1. 归档结论

- 项目负责人于 2026-09-20 依次批准 PR #53、#54、#55、#56 合入，并在 I2 收口卫生完成后
  下达"归档 I2"。
- I2 的四个 PR 已全部审查并合入；归档基线是 `main@cc617f5e2d18c53b4a92528663edb1bd16aab557`。
- I2 当前最强证据是 `tests`：GitHub 隔离 runner 的 PostgreSQL integration 与 Compose smoke
  已通过，普通对话通道的路由、终态、投影、审计形状与跨渠道语义有离线闭环证据。
- 本次归档只关闭 I2 的**离线实施范围**，**不表示** `DEVELOPMENT_PLAN.md` 的 I2 完整退出标准
  已通过，也不解锁 I3 受治理资料查询或 I4 日志分析。
- 没有连接真实 Gemini、真实飞书、真实 StarRocks 或任何运维目标，没有部署、canary 或产品
  用户验收。`knowledge_lookup` 与 `log_analysis` 仍保持 `interaction.route_not_available`。

## 2. 归档范围

I2 把"普通对话"从一个 pre-plan rejection 变成一条**有边界、有来源、可审计**的终态通道：

- `RoutingDisposition` 增加 `respond`。`InteractionKind.CONVERSATION` 不再被拒绝，而是在无
  Plan、无 Evidence、无 Gateway、无 Approval、无 ExecutionDisclosure 的情况下按既有 TaskStore
  状态机收成 `SUCCEEDED / interaction.conversation_responded`。
- 回答是当前 `CapabilitySnapshot` 的确定性投影（能力目录）：逐条列出已注册能力、操作、
  `read_class`、`effect_class` 与所经 gateway，`refs` 带 `capability-snapshot:<id>` 与
  `capability:<id>@<version>`。
- 带 `clarification_parent_task_id` 的子任务**不得**走这条通道，收成
  `REJECTED / interaction.clarification_subject_incompatible`，并在 trace 上归因到 `INTENT`。
- Router 已算出的闭集拒绝码落到任务 `terminal_reason`；澄清的原因仍只属于
  `ClarificationRecord`，两个域不共用一个字段。
- 终态审计形状统一：每条终态任务恰好一条 `REFLECTION`，普通对话不再是例外。

## 3. 受审与合入事实

| PR | 受审 head | squash 合入 | CI run | 内容 |
| --- | --- | --- | --- | --- |
| [#53](https://github.com/shixian66/xiaowei-agent/pull/53) | `a001f86044ed576a198a0758e238722ab28dd49f` | `26f39a9889c3d37034506bfb2ade3febb79303cb` | `35487688807` | I2-A：`RESPOND` 路由与无工具终态 |
| [#54](https://github.com/shixian66/xiaowei-agent/pull/54) | `5c4d11bac99ae984b87116b5c1d4396d21acfb7f` | `08aede5645cdf10f12cfee9e9914453765dc5b07` | `35490099111` | 复审 P2：澄清子任务不得被收成对话成功 |
| [#55](https://github.com/shixian66/xiaowei-agent/pull/55) | `74687eaed38f6b7a9c9bb631b7ead199a8c7834f` | `25c2a6e2c243efb6fd319a97cf494c2e7017fc03` | `35497165853` | I2-B：能力目录投影 |
| [#56](https://github.com/shixian66/xiaowei-agent/pull/56) | `5547dba3329738beb74bc6840d2626af65acb605` | `cc617f5e2d18c53b4a92528663edb1bd16aab557` | `35498028301` | I2 收口卫生：拒绝码归因、REFLECTION、固化用例 |

四个 PR 的 CI 均为 8/8 success，且 headSha 与受审对象逐字匹配。每次 squash 后的树与受审
提交 `git diff --stat` 均为空。

## 4. 归档基线的四门结果

在 `main@cc617f5` 上重跑：

| 门 | 结果 |
| --- | --- |
| `python -m pytest -q` | `3945 passed, 269 skipped` |
| `python -m pytest -m security -q` | `1447 passed, 83 skipped, 2684 deselected` |
| `ruff check .` | 通过 |
| `mypy src` | 189 个 source files 通过 |
| `tests/integration/`（隔离临时 PostgreSQL 容器） | `271 passed, 0 skipped` |
| `main` CI run `35509896248` | 8/8 success，headSha `cc617f5` |

## 5. 复审发现与根因修复

I2 的四轮复审共报出 4 个非 P0/P1 问题，全部已修并各带反证：

1. **PR #53 P2 — 澄清父链被烧掉却报成功。** 带澄清父链的子任务被判为 `CONVERSATION` 时，
   一次性父链已被消费且不可恢复，任务却以 `SUCCEEDED` 收口，固定回复正文还写着"不会把
   聊天内容当成澄清父链"。与 `main` 做 A/B 复现后于 PR #54 修复。父链的**一次性消费**是
   TaskStore 既有语义，本轮未改；修的是错误的终态语义。
2. **PR #54 P2 — 拒绝在 trace 上归因到零个阶段。** 新增拒绝路径以 `REJECTED` 收口，INTENT
   却记成 OK，整条链没有任何非 OK 阶段。根因是判定被算了两次；收成一个共享判定后修复。
3. **PR #55 P3 — README 目标能力段停在旧口径。** 根因是 doc fact binding 只钉了顶部状态块，
   README 中描述该通道的两段是互相独立的叙述。两段各钉一条并加 stale 短语防回潮，顺着
   根因扫出 ADR-017 枚举定义处的同类残留。
4. **I2 收口卫生（PR #56）** 一次性修掉四项已知 P3：拒绝码不可归因、对话是唯一不发
   REFLECTION 的终态、对话链路只有临时探针没有固化用例、TaskView 投影守卫不承重。

## 6. 隔离变异反证清单

I2 全程用"改坏源码 → 验证测试因预期原因变红 → 恢复"的方式确认关键保护承重。逐项结果：

| 变异 | 结果 |
| --- | --- |
| Router 的 `conversation → RESPOND` 分支回退 | 4 red |
| `RESPOND` 分支提到 `capability_draft` 守卫之前 | 1 red（夹带 capability draft 被放行） |
| Runtime 去掉 `RESPOND` 早返回 | 2 red |
| 去掉分类器入参 `scrub_text` | 4 red |
| 撤掉澄清守卫（回到 PR #53 行为） | 1 red，症状为 `SUCCEEDED / interaction.conversation_responded` |
| 回退 `RequestRejectedError` 的 except 拆分 | 1 red |
| 只撤掉 INTENT 归因修复、保留拒绝逻辑 | 1 red，症状为 `assert [] == [(INTENT, REJECTED)]` |
| 能力目录回到固定文案 | 5 red |
| 能力目录去掉 `read_class` | 5 red |
| 空快照也用非空文案 | 1 red |
| `refs` 丢掉 `snapshot_id` | 4 red |
| **把用户文本回显进对话回答（模拟注入面）** | **7 red** |
| 能力地图去掉 `read_class` 列 | 1 red |
| Runtime 重新丢掉 Router 拒绝码 | 6 red |
| 撤掉对话的 `REFLECTION` | 2 red |
| `REFLECTION` 改成事后单独补发 | 1 red |
| TaskView 去掉 `terminal_reason` 守卫 | 1 red |
| 对话迁移路径写死三跳 | 1 red（真实 PostgreSQL 崩溃恢复） |
| 把飞书卡片预算压到装不下能力目录 | 2 red |

### 6.1 没有直接变红、需要单独说明的一条

`_route_rejection_reason` 中"只接受 `InteractionRejectionReasonCode`"的收窄**当前不可达**：
CLARIFY 分支在到达拒绝语句之前就已终态化，所以放宽它时全量仍然全绿。该收窄没有被删除
（它编码的是"澄清原因不得进入 `terminal_reason`"这条真实域规则），而是补了一条直接测函数
本身的单测；放宽后该单测变红。

## 7. 明确不在本次归档内

- **I3 受治理资料查询与 I4 用户提供日志分析**未实现；`knowledge_lookup` 与 `log_analysis`
  仍稳定拒绝。
- **普通对话记忆**未实现。TaskStore 不保存 conversation history，provider chat/session 不是
  任务事实源。ADR-017 §5.3 要求：I2 若需要聊天记忆，必须用独立 `ConversationStore` 重新设计。
- **能力目录不代表能力可用于真实系统。** 它只是把 Registry 声明重排给用户看；
  `starrocks.slow_query.diagnose`、`prometheus.alert.evidence`、`asset.inventory.lookup`
  三者的证据等级均仍为 `tests`，都未连接对应真实运维系统。
- **未纳入 I2 收口的存量**：迁移不可变哈希登记 1/13 属 M/RI 系列；`_resolve`、`_prepare`
  与"clarification parent is missing"三处拒绝仍无闭集码——给它们发码属于新增设计，不是卫生。
- 真实 Gemini/飞书/StarRocks、部署、canary、UAT、RI3 test-env GO 与 E1 仍是各自独立硬门，
  没有被 I2 打开。
