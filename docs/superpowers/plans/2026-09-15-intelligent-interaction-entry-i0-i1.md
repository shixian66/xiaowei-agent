# Intelligent Interaction Entry I0/I1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不削弱现有确定性执行链的前提下，为小维增加统一的智能交互分流、终态澄清、可信槽位升级、静态只读分类和执行披露屏障，使不明确或未实现的交互在任何 ToolGateway 调用前 fail-closed。

**Architecture:** interfaces 只创建任务并传递已认证上下文；Worker 中的 Runtime 先 load-or-create 一份 insert-once `InteractionArtifact`，再由纯 `DeterministicInteractionRouter` 决定 proceed/clarify/refuse。只有 capability+proceed 能进入 Resolver；选定 binding 后必须由其 `SlotVerifier` 生成专属不可变 Params，Planner 不再读取原始模型 slots。澄清本轮以 `CLARIFICATION_REQUIRED` 终结，回复创建严格一对一的新子任务。Plan/Target 保存后必须生成可重建的执行披露并成功写入审计，才能进入 Admission/Gateway。

**Tech Stack:** Python 3.11、Pydantic v2、frozen dataclass/generics、SQLAlchemy 2 + Alembic、PostgreSQL、pytest、Ruff、mypy、现有 Docker Compose 与 Gemini adapter seam。

**Spec:** [小维 Agent 2.0 智能交互入口 I0/I1 总体设计](../specs/2026-09-15-intelligent-interaction-entry-i0-i1-design.md)

## Global Constraints

- 本计划是实施说明，不授权读取真实 key、调用真实模型/飞书/StarRocks、部署、canary、用户验收或 E1。
- 开始每个 PR 前重新读取 `AGENTS.md → ARCHITECTURE.md → AGENT_HANDOFF.md → README.md → DEVELOPMENT_PLAN.md`，再检查最新 `main`、工作区和前序 PR 的精确合入 SHA。
- 每个 PR 从最新 `main` 新建 `claude/<topic>` 分支；不直接在 main 开发，不自行合并或归档。
- 一个 PR 只处理本节声明的垂直闭环。发现上游契约错误，先停下修上游，不在下游加兼容分支。
- 行为变更严格 TDD：先观察目标测试因缺失行为失败，再写最小实现；禁止只在实现后补 happy-path 测试。
- 所有用户/模型/日志/网页文本按不可信输入处理；测试中的伪 secret 字面量必须拆开构造，不能在源码形成连续可扫描字符串。
- 触及 `planning/`、`governance/` 或 `tools/` 的 PR 必须运行 `python -m pytest -m security -q`。
- 只有最终 I1 候选 SHA 才跑完整四门并形成 closure evidence；单个 PR 先跑相关测试，收尾再跑全量。
- 不同时保留 `IntentModelPort` 与 `InteractionClassifierPort`、`parent_task_id` 与 clarification parent、旧 Planner 与新 SlotVerifier 两套运行真源。
- 任何 migration 涉及旧非空父关系时必须 fail-closed；是否清理本地数据需项目负责人单独确认，开发者不得自行删除。

---

## 交付与 PR 顺序

| 顺序 | PR | 唯一目的 | 前置 |
| --- | --- | --- | --- |
| 0 | I0-DOC | 把已确认设计写入 ADR 与项目真相文档 | 本计划获批 |
| 1 | I1-A | 完成交互分类、确定性路由、终态澄清、一对一子任务，并淘汰通用父历史 | I0-DOC 合入 |
| 2 | I1-B | 把三个 capability 的原始 slots→Planner 路径替换为类型化 SlotVerifier | I1-A 合入 |
| 3 | I1-C | 增加静态 ReadClass、Policy 二次防线与 Plan schema V2 | I1-B 合入 |
| 4 | I1-D | 增加执行披露就绪屏障、TaskView/渠道投影和 I1 离线 Eval closure | I1-C 合入 |

不得把两行合成一个 PR；不得在 I1-D 才补前序 PR 本应具备的安全测试。

---

## PR 0 — I0-DOC：冻结文档真相

### Task 0.1：建立 I0 开工基线

**Files:**

- Read: `AGENTS.md`
- Read: `ARCHITECTURE.md`
- Read: `AGENT_HANDOFF.md`
- Read: `README.md`
- Read: `DEVELOPMENT_PLAN.md`
- Read: `docs/superpowers/specs/2026-09-15-intelligent-interaction-entry-i0-i1-design.md`
- Read: `docs/superpowers/plans/2026-09-15-intelligent-interaction-entry-i0-i1.md`

- [ ] **Step 1: 记录只读基线**

Run:

```bash
git status --short --branch
git rev-parse HEAD
git rev-parse main
git rev-parse origin/main
```

Expected: 工作区无未知改动；HEAD 基于最新 main。若三份 SHA 不同，先解释原因并按项目规则更新基线，
不能用本文的 `174fa13...` 冒充当前事实。

- [ ] **Step 2: 创建独立文档分支**

```bash
git switch main
git pull --ff-only
git switch -c claude/i0-intelligent-interaction-docs
```

Expected: 新分支只承载 I0 文档。

### Task 0.2：新增 ADR-017

**Files:**

- Create: `docs/adr/ADR-017-intelligent-interaction-and-clarification.md`
- Modify: `tests/contract/test_doc_fact_binding.py`
- Modify: `tests/security/test_docs_command_consistency.py`

- [ ] **Step 1: 写 RED 文档契约**

在 `tests/contract/test_doc_fact_binding.py` 增加断言，要求 ADR-017 明确出现并唯一绑定以下术语：

```python
required = {
    "InteractionKind",
    "RoutingDisposition",
    "CLARIFICATION_REQUIRED",
    "clarification_parent_task_id",
    "ClarificationRecordStore",
    "CapabilityInputBinding",
    "ReadClass",
    "ExecutionDisclosure",
}
```

Run:

```bash
python -m pytest tests/contract/test_doc_fact_binding.py tests/security/test_docs_command_consistency.py -q
```

Expected: ADR 尚不存在，测试失败；失败只指向缺失文档/术语。

- [ ] **Step 2: 写 ADR**

ADR 必须包含：背景、决策、调用链、状态语义、单次消费、模型 at-least-once、槽位 union/UTC/snapshot、
Capability input 与 operation arguments 分层、ReadClass、披露屏障、迁移、备选方案及变更门。

明确否决：

- 同名 `parent_task_id` 换义；
- `AWAITING_CLARIFICATION` 暂停态；
- conversation history 偷渡进 TaskStore；
- Planner 继续吃 `IntentDraft`；
- restricted read 复用 ApprovalGate；
- DisclosureStore/送达 ACK；
- 动态字段/风险 DSL、ReAct、多 Agent、LangGraph 接管安全链。

- [ ] **Step 3: 运行 ADR 契约**

```bash
python -m pytest tests/contract/test_doc_fact_binding.py tests/security/test_docs_command_consistency.py -q
```

Expected: PASS。

### Task 0.3：同步四份真相文档

**Files:**

- Modify: `ARCHITECTURE.md`
- Modify: `DEVELOPMENT_PLAN.md`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`
- Modify: `tests/contract/test_doc_fact_binding.py`

- [ ] **Step 1: 更新 ARCHITECTURE**

精确修改：

- 稳定主链改为 `InteractionArtifact → DeterministicInteractionRouter → CapabilityResolver → SlotVerifier
  → Planner → PlanStore → Disclosure → Runner/Admission/Gateway`；
- 契约表加入本 spec 的新类型，并标出真源；
- Task 状态图加入终态 `CLARIFICATION_REQUIRED`；
- 删除通用父任务历史与 `ContextAssembler` 目标语义；
- 计划 schema 目标版本改为 V2，但注明尚未实现；
- 新增 I2–I4 非执行通道边界和 ExternalContent 规则。

- [ ] **Step 2: 更新 DEVELOPMENT_PLAN**

在现有路线中新增 I0–I5：

```text
I0 文档与契约
→ I1 安全分流/澄清/披露
→ I2 限定领域普通对话
→ I3 受治理资料查询
→ I4 用户提供日志分析
→ I5 真实模型 Eval/灰度/UAT
```

写明：M8 前只要求 I1；I2–I4 不是 M8 前置；主动日志连接为后续独立能力；I1 不改变 RI2/RI3/RI4
或 E1 的真实调用 GO。

- [ ] **Step 3: 更新 README 与 handoff**

README 只给人类开发者说明当前入口限制与文档导航，不复制完整 ADR。Handoff 记录精确分支/SHA、
“I0 文档是否已合入”和“I1 尚未实现”，不能把批准设计写成运行能力。

- [ ] **Step 4: 扫描文档漂移**

```bash
rg -n "parent_task_id|ContextAssembler|IntentModelPort|PLAN_SCHEMA_VERSION|InteractionKind|CLARIFICATION_REQUIRED" AGENTS.md ARCHITECTURE.md AGENT_HANDOFF.md README.md DEVELOPMENT_PLAN.md docs/adr docs/superpowers
python -m pytest tests/contract/test_doc_fact_binding.py tests/security/test_docs_command_consistency.py -q
git diff --check
```

Expected: 所有旧术语只存在于明确的“被淘汰/历史”上下文；无命令漂移和 whitespace 错误。

- [ ] **Step 5: 提交 I0-DOC**

```bash
git add docs/adr/ADR-017-intelligent-interaction-and-clarification.md ARCHITECTURE.md DEVELOPMENT_PLAN.md README.md AGENT_HANDOFF.md tests/contract/test_doc_fact_binding.py tests/security/test_docs_command_consistency.py
git commit -m "docs(architecture): define intelligent interaction entry"
```

提交后让 Codex 按精确 SHA 审查；项目负责人批准后才可合并并下达“开始 I1-A”。

---

## PR 1 — I1-A：交互路由与终态澄清闭环

本 PR 的单一结果是：任一渠道提交的新任务先经过同一个可信路由；不明确请求产生可安全补充的一对一
终态链，未实现通道稳定拒绝，旧通用父历史彻底退出。

### Task 1.1：定义交互与澄清契约

**Files:**

- Create: `src/xiaowei_agent/contracts/interaction.py`
- Create: `src/xiaowei_agent/contracts/clarification.py`
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/task.py`
- Modify: `src/xiaowei_agent/contracts/model.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Create: `tests/unit/test_interaction_contracts.py`
- Create: `tests/unit/test_clarification_contracts.py`
- Modify: `tests/unit/test_model_contracts.py`
- Modify: `tests/contract/test_contract_enum_references.py`
- Modify: `tests/security/test_deep_immutability.py`
- Modify: `tests/security/test_scalar_strictness.py`

- [ ] **Step 1: 写 RED 的 interaction union 测试**

覆盖：两个闭集枚举；`InteractionDraft` 只承载不可信候选并允许 Router 看见“不匹配 kind/draft”的恶意
组合；confidence 不能改变结构；Contract strict/frozen/extra-forbid。kind/draft 组合的最终拒绝必须由
Router 测试承重，不能在反序列化失败后悄悄改走 capability fallback。

示例：

```python
def test_interaction_draft_preserves_an_inconsistent_untrusted_candidate() -> None:
    draft = InteractionDraft(
        proposed_kind=InteractionKind.CONVERSATION,
        capability_draft=_slow_query_draft(),
        confidence=1.0,
        source=InteractionSource.MODEL,
    )
    assert draft.capability_draft is not None
```

- [ ] **Step 2: 写 RED 的 confirmed value/snapshot 测试**

覆盖：显式 `kind` 判别；`time_range` 不能带 text；UTC canonical；半开区间；受控时区；512 字符/2048
bytes；字段专属 kind；排序与重复拒绝；RouteSubject slots 恒空；CapabilitySubject 四字段完整；任意 dict
拒绝。

伪 secret 用拆分字符串：

```python
shaped = "token=" + "fake-value"
with pytest.raises(ValidationError):
    ConfirmedTextValue(kind="text", text=shaped)
```

- [ ] **Step 3: 实现最小契约**

关键形状：

```python
ConfirmedValue = Annotated[
    ConfirmedTextValue | ConfirmedTimeRangeValue,
    Field(discriminator="kind"),
]

class ClarificationRecord(Contract):
    record_version: Literal[1] = 1
    task_id: TaskId
    subject: ClarificationSubject
    reason_code: ClarificationReasonCode
    missing_fields: tuple[ClarificationField, ...]
    confirmed_slots: tuple[ConfirmedSlot, ...]
    created_at: AwareDatetime
    fencing_token: StrictInt = Field(gt=0)
```

使用 model validator **验证** canonical UTC/排序，不在 validator 内静默改写。`ConfirmedTextValue` 调用
`scrub_text` 比较原值；不同即拒绝。

- [ ] **Step 4: 增加终态**

`TaskStatus.CLARIFICATION_REQUIRED` 加入 `TERMINAL_STATUSES`，`CREATED/PLANNING` 允许转入，终态出边
为空。调整 TaskView 规则：该状态需要澄清投影，但不能用普通 capability terminal renderer 临时拼接。

- [ ] **Step 5: 运行契约测试**

```bash
python -m pytest tests/unit/test_interaction_contracts.py tests/unit/test_clarification_contracts.py tests/unit/test_model_contracts.py tests/contract/test_contract_enum_references.py tests/security/test_deep_immutability.py tests/security/test_scalar_strictness.py -q
```

Expected: PASS。

### Task 1.2：替换模型分类 Port 与 artifact 真源

**Files:**

- Create: `src/xiaowei_agent/application/model_interaction.py`
- Modify: `src/xiaowei_agent/application/model_ports.py`
- Modify: `src/xiaowei_agent/interfaces/gemini_model.py`
- Modify: `src/xiaowei_agent/persistence/model_artifacts.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/database.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0011_interaction_clarification.py`
- Delete: `src/xiaowei_agent/application/model_intent.py`
- Modify: `tests/fakes/model.py`
- Modify: `tests/suites/model_artifacts.py`
- Modify: `tests/contract/test_model_artifact_store.py`
- Modify: `tests/integration/test_model_artifact_store_postgres.py`
- Modify: `tests/contract/test_gemini_sdk_seam.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/security/test_model_context_boundary.py`
- Modify: `tests/security/test_gemini_sdk_boundary.py`

- [ ] **Step 1: 写 RED Port/调用次数测试**

证明：`InteractionClassifierPort.classify(request)` 是唯一分类端口；单 attempt 0..1；retryable Provider
错误立即 fallback；CancelledError、shutdown/grant loss 不 fallback；模型响应无法填写 provider/usage。

```python
class InteractionClassifierPort(Protocol):
    async def classify(
        self, request: InteractionClassifierRequest
    ) -> InteractionModelResult: ...
```

删除测试中对 `INTENT_RETRYABLE_ERROR_CODES` 的“两次请求”期待；不能只把上限从 2 改成 1 而保留循环。

- [ ] **Step 2: 写 RED artifact 恢复测试**

覆盖：

- artifact 已存在时 provider=0；
- provider 返回后、save 前崩溃，下一 attempt 可再次调用；
- save 后、Router 前崩溃，下一 attempt provider=0；
- grant 丢失时保存失败；
- 两 attempt 不同候选只有 Store winner 后续可见；
- Runtime 禁止使用 save 前本地 candidate。

- [ ] **Step 3: 实现 artifact V2 和迁移**

方法改为 `load_interaction/save_interaction`。表重命名为 `task_interaction_artifacts`；新写入 version=2；
保留既有 V1 行但新 Runtime 不解释、不升级。数据库约束允许历史 1 与新 2，应用写入口只产生 2。

`ClarificationContext` 的 subject+snapshot 必须进入 `interaction_input_digest()`；父原文和 Render/Evidence
不得进入 request 或摘要。

- [ ] **Step 4: 改 Gemini wire schema**

Provider response 显式包含 `proposed_kind` 与 optional nested capability draft。adapter 生成 source 和可信
metadata。provider schema 非法映射现有安全 error/fallback，不回显 provider body。

- [ ] **Step 5: 删除旧调用真源**

删除 `IntentModelPort`、`IntentModelResult`、`ModelIntentRequest.history`、旧 retry loop 和
`load_intent/save_intent`。Advisory Port/Store 保持原职责，不随分类重构改义。

- [ ] **Step 6: 验证**

```bash
python -m pytest tests/unit/test_model_ports.py tests/unit/test_model_contracts.py tests/unit/test_model_intent_service.py tests/contract/test_model_artifact_store.py tests/integration/test_model_artifact_store_postgres.py tests/contract/test_gemini_sdk_seam.py tests/security/test_model_context_boundary.py tests/security/test_gemini_sdk_boundary.py -q
```

若原测试文件 `test_model_intent_service.py` 不再描述真实对象，将其改名为
`test_model_interaction_service.py`，并同步明确 `git rm`/`git add`，不保留误导名称。

### Task 1.3：实现纯 Router 与统一 Runtime 路径

**Files:**

- Create: `src/xiaowei_agent/application/interaction_router.py`
- Modify: `src/xiaowei_agent/application/runtime.py`
- Modify: `src/xiaowei_agent/capabilities/intent.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `src/xiaowei_agent/contracts/trace_events.py`
- Modify: `tests/fakes/runtime.py`
- Create: `tests/unit/test_interaction_router.py`
- Create: `tests/contract/test_runtime_interaction_routing.py`
- Create: `tests/security/test_interaction_router_boundary.py`
- Modify: `tests/security/test_runtime_bypass.py`
- Modify: `tests/security/test_intent_interpreter_boundary.py`

- [ ] **Step 1: 写 RED 决策矩阵**

逐行覆盖 spec §5.1；spy 精确断言 Resolver/Planner/Gateway 次数。环境 mismatch 正常 attempt 断言最终
REJECTED、stable reason；崩溃恢复测试只断言每 attempt Router≤1 与全局下游=0，不伪造 Worker exactly-once。

- [ ] **Step 2: 实现纯 Router**

Router 输入是获胜 artifact draft、当前 Context、可选父 subject；输出只有 typed decision。环境 assertion
用封闭纯规则比较，禁止查 DB。Router 本身不 await、不持有 store/model/resolver。

- [ ] **Step 3: 收敛 Runtime**

`execute_task()` 顺序改为 artifact→Router→分支。`handle()` 不得在 Task 创建前 `_interpret/_resolve/_prepare`；
改为复用 `submit_task → begin_task_attempt → execute_task → project` 的同一路径。

规则 fallback：现有 `RuleBasedIntentInterpreter` 只在唯一识别现有 capability 时产生
`CAPABILITY_REQUEST + IntentDraft`，否则 `UNKNOWN`。删除把 Context environment 塞进 slots 的代码及 required
environment slot。

- [ ] **Step 4: 架构反证**

测试 import/AST 约束：interfaces 不导入 Router 具体规则或 capability keywords；Router 不导入
`persistence/`、`tools/`、model adapter、Resolver/Planner；所有入口最终调用 Runtime 共用方法。

- [ ] **Step 5: 验证**

```bash
python -m pytest tests/unit/test_interaction_router.py tests/contract/test_runtime_interaction_routing.py tests/security/test_interaction_router_boundary.py tests/security/test_runtime_bypass.py tests/security/test_intent_interpreter_boundary.py -q
```

### Task 1.4：实现 ClarificationRecordStore 与可靠终态化

**Files:**

- Create: `src/xiaowei_agent/persistence/clarification_records.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/database.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `src/xiaowei_agent/application/runtime.py`
- Modify: `src/xiaowei_agent/application/task_view_runtime.py`
- Modify: `src/xiaowei_agent/rendering/generic.py`
- Create: `tests/suites/clarification_records.py`
- Create: `tests/contract/test_clarification_record_store.py`
- Create: `tests/integration/test_clarification_record_store_postgres.py`
- Create: `tests/contract/test_clarification_task_view.py`
- Create: `tests/security/test_clarification_record_integrity.py`

- [ ] **Step 1: 写 RED Store suite**

同一 suite 跑内存/PostgreSQL：有效 grant insert；同内容幂等返回同记录；不同内容 conflict；无 grant/旧 fence/
终态写入拒绝；task_id/time/fence 不能由 candidate 传入；load not found 明确。

- [ ] **Step 2: 写 RED 崩溃顺序测试**

注入 save 后、transition 前崩溃；恢复 attempt 必须 load 原记录并终态化，classifier 可按 artifact 规则为 0；
禁止出现状态已 CLARIFICATION_REQUIRED 但 record 缺失。反向模拟损坏时 TaskView 返回稳定
`clarification.integrity_error`，不拼原文。

- [ ] **Step 3: 实现窄 Store**

内存 store 与 TaskStore 共享 state/lock；PostgreSQL 使用 `INSERT ... ON CONFLICT DO NOTHING` 后比对；表中
`task_id` PK/FK、version=1、fence>0，不加 trigger/consumed。

- [ ] **Step 4: Runtime 终态化**

Route clarify 时 Resolver=0。Runtime 先 load-or-save record，再用当前 grant CAS 到
CLARIFICATION_REQUIRED。record candidate 的 RouteSubject slots 必为空。

- [ ] **Step 5: TaskView 投影**

新增独立 `clarification` 字段或严格的 clarification RenderPayload 分支（按 ADR-017 选择的唯一契约），
只能读取 Store record。普通 REJECTED 继续走 preplan rejection；两者不能共用 terminal_reason 临时文案。

- [ ] **Step 6: 验证**

```bash
python -m pytest tests/contract/test_clarification_record_store.py tests/integration/test_clarification_record_store_postgres.py tests/contract/test_clarification_task_view.py tests/security/test_clarification_record_integrity.py -q
```

### Task 1.5：替换父任务关系并实现单次消费

**Files:**

- Delete: `src/xiaowei_agent/application/context.py`
- Modify: `src/xiaowei_agent/contracts/task.py`
- Modify: `src/xiaowei_agent/persistence/store.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/migrations/versions/rev_0011_interaction_clarification.py`
- Modify: `src/xiaowei_agent/application/channel_submission.py`
- Modify: `src/xiaowei_agent/application/channel_access.py`
- Modify: `src/xiaowei_agent/interfaces/api.py`
- Modify: `src/xiaowei_agent/interfaces/cli.py`
- Modify: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `src/xiaowei_agent/interfaces/web_static/app.js`
- Modify: `src/xiaowei_agent/interfaces/web_static/detail.js`
- Modify: `src/xiaowei_agent/interfaces/feishu_listener.py`
- Modify: `src/xiaowei_agent/rendering/feishu.py`
- Delete: `tests/unit/test_context_assembler.py`
- Delete: `tests/security/test_model_parent_context.py`
- Create: `tests/contract/test_clarification_child_submission.py`
- Create: `tests/integration/test_clarification_child_concurrency_postgres.py`
- Create: `tests/security/test_clarification_parent_access.py`
- Modify: `tests/contract/test_channel_submission.py`
- Modify: `tests/contract/test_web_task_api.py`
- Modify: `tests/contract/test_api_contract.py`
- Modify: `tests/contract/test_cli_contract.py`
- Modify: `tests/security/test_web_task_access.py`

- [ ] **Step 1: 写 RED 反契约/并发测试**

至少覆盖：

```text
SUCCEEDED/FAILED/REJECTED/CANCELED/INDETERMINATE 不能作父
同 tenant 但跨 env/actor/channel owner 不可用
不存在与未授权返回同一 not-found
相同 idempotency key 并发 → 同一 child
不同 key 同父并发 → 一胜一 parent_already_consumed
失败事务回滚 → 父仍可消费
失败方 task/worker/gateway = 0
child 再 clarify → 可作下一父
```

PostgreSQL 测试必须用两个独立连接/事务制造真实竞争，不能只跑 FakeStore。

- [ ] **Step 2: 实现 digest 与窄 create**

`request_dedup_digest`/`submission_digest` 用字段名 `clarification_parent_task_id`。TaskStore 新增窄方法：

```python
async def create_clarification_child(
    self,
    *,
    submission: TaskSubmission,
    authenticated_channel_owner: str,
) -> TaskRecord: ...
```

实现先查幂等，再在同事务校验父状态/作用域/actor/channel owner，并写 child/submission。非空父字段加唯一
约束；唯一冲突安全映射，不回显父 id。

- [ ] **Step 3: migration fail-closed**

upgrade 开头读取旧 `parent_task_id IS NOT NULL` 计数；非零抛确定错误并让事务回滚。零行时删除旧
FK/index/column，再创建新字段/FK/index/unique。downgrade 只能恢复 schema 形状，不能把 clarification
关系伪装成通用历史；若存在非空新关系则同样 fail-closed。

- [ ] **Step 4: 删除旧上下文与 UI**

删除 `ContextAssembler` imports、旧测试和所有终态“继续”。Web 只为 clarification 显示补充表单；API/CLI
显式传父字段；飞书只接受结构化卡片动作或显式 task ref，禁止“最近任务”推断。渠道 handler 不做
Router/slot 判断。

- [ ] **Step 5: schema/migration 与渠道验证**

```bash
python -m pytest tests/contract/test_schema_matches_migration.py tests/contract/test_migration_guard.py tests/integration/test_migration_paths.py tests/contract/test_clarification_child_submission.py tests/integration/test_clarification_child_concurrency_postgres.py tests/security/test_clarification_parent_access.py tests/contract/test_channel_submission.py tests/contract/test_web_task_api.py tests/contract/test_api_contract.py tests/contract/test_cli_contract.py -q
```

### Task 1.6：I1-A 汇总验证与提交

- [ ] **Step 1: 搜索双轨残留**

```bash
rg -n "IntentModelPort|generate_intent|load_intent|save_intent|parent_task_id|ContextAssembler|ModelIntentRequest|context_truncated" src tests
```

Expected: 旧名称为 0；`clarification_parent_task_id` 不因 substring 检查被误判，人工核对结果。

- [ ] **Step 2: 相关中档验证**

```bash
python -m pytest tests/unit tests/contract tests/integration/test_model_artifact_store_postgres.py tests/integration/test_clarification_record_store_postgres.py tests/integration/test_clarification_child_concurrency_postgres.py -q
python -m pytest -m security -q
ruff check .
mypy src
git diff --check
```

- [ ] **Step 3: 精确暂存并提交**

先用 `git status --short` 生成本 PR 文件清单；逐路径 `git add`，删除路径用 `git add -u <path>`，禁止
`git add -A`。

```bash
git commit -m "feat(interaction): add governed routing and clarification"
```

Codex 以提交 SHA 审查；未通过不得开始 I1-B。

---

## PR 2 — I1-B：类型化 SlotVerifier 与 capability 补槽

本 PR 的单一结果是：Resolver 后的所有 capability 输入必须经过 deterministic SlotVerifier，Planner 只
接受专属 Params；缺槽形成 CapabilitySubject 澄清，而不是 Planner 异常。

### Task 2.1：定义 capability input binding

**Files:**

- Modify: `src/xiaowei_agent/contracts/capability.py`
- Create: `src/xiaowei_agent/application/capability_input.py`
- Modify: `src/xiaowei_agent/application/capability_runtime.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/capabilities/specs.py`
- Modify: `src/xiaowei_agent/capabilities/prometheus_alert.py`
- Modify: `src/xiaowei_agent/capabilities/asset_inventory.py`
- Create: `tests/unit/test_capability_input_contracts.py`
- Modify: `tests/unit/test_capability_spec.py`
- Modify: `tests/unit/test_capability_runtime_registry.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Create: `tests/security/test_planner_input_boundary.py`

- [ ] **Step 1: 写 RED 泛型/运行时不变量测试**

覆盖：CapabilitySpec 必须有 input_schema_ref；binding 缺 verifier/allowlist/projector；三方 schema drift；
非法 Params 基类；verifier/planner type mismatch；精确类型而非 subclass；input ref 不等于任何 operation
argument ref 仍可装配；一个 input schema 对多个 argument schema 正常。

- [ ] **Step 2: 实现最小类型**

```python
class CapabilityParams(Contract):
    INPUT_SCHEMA_REF: ClassVar[str]

ParamsT = TypeVar("ParamsT", bound=CapabilityParams)

@dataclass(frozen=True)
class SlotReady(Generic[ParamsT]):
    params: ParamsT
    confirmed_slots: tuple[ConfirmedSlot, ...]
```

同文件定义 `SlotIncomplete`、`SlotInvalid`、Protocols 与泛型工厂
`bind_capability_input(...)`。不要用注解反射或运行时容器“猜”泛型。

- [ ] **Step 3: 修改 Registry**

启动验证三方 input ref、entry operation、allowlist 和 projector 要求。Runtime 再做 `type(params) is
params_type` 防线。架构测试禁止 CapabilityPlanner import/接收 IntentDraft 或 Mapping。

- [ ] **Step 4: 验证**

```bash
python -m pytest tests/unit/test_capability_input_contracts.py tests/unit/test_capability_spec.py tests/unit/test_capability_runtime_registry.py tests/contract/test_protocol_conformance.py tests/security/test_planner_input_boundary.py -q
mypy src
```

### Task 2.2：实现唯一的 confirmed slot 校验公共件

**Files:**

- Create: `src/xiaowei_agent/planning/slot_verification.py`
- Create: `tests/unit/test_slot_verification.py`
- Create: `tests/security/test_confirmed_slot_safety.py`

- [ ] **Step 1: 写 RED 可信升级矩阵**

测试本轮明确文本、父值继承、明确改值、含糊多值、模型自补、ExternalContent、secret-shaped、非规范
时间/排序。相对时间用当前表达所在 Task 的 `submission.as_of`；父绝对时间不重算。

- [ ] **Step 2: 实现小型纯 helper**

只提供：canonical UTC time range、snapshot validate/merge、父/最终投影对比、scrub equality guard。不要建
动态字段注册表、通用 schema engine 或 SlotChange DTO。

- [ ] **Step 3: 验证**

```bash
python -m pytest tests/unit/test_slot_verification.py tests/security/test_confirmed_slot_safety.py tests/security/test_redaction.py -q
```

### Task 2.3：迁移 StarRocks slow-query binding

**Files:**

- Modify: `src/xiaowei_agent/planning/starrocks/params.py`
- Create: `src/xiaowei_agent/planning/starrocks/slots.py`
- Modify: `src/xiaowei_agent/planning/starrocks/compiler.py`
- Modify: `src/xiaowei_agent/capabilities/target.py`
- Modify: `src/xiaowei_agent/application/default_capabilities.py`
- Modify: `tests/unit/test_slow_query_params.py`
- Modify: `tests/unit/test_plan_compiler.py`
- Modify: `tests/unit/test_target_resolver.py`
- Modify: `tests/contract/test_runtime_async_lifecycle.py`
- Modify: `tests/evals/test_l1_intent.py`
- Modify: `tests/security/test_model_context_boundary.py`
- Modify: `tests/security/test_param_injection.py`

- [ ] **Step 1: RED：Planner 不再接 IntentDraft**

构造模型 slots 与用户原文冲突，验证模型值不能进入 Params/Target；missing/invalid 时 planner/gateway=0；
默认 30 分钟按 submission.as_of 生成绝对 UTC 区间。

- [ ] **Step 2: 实现 SlowQuerySlotVerifier**

字段限定 time_range/database/user_name/query_id；复用现有 Identifier 与 `normalise_window` 唯一逻辑；
`SlowQueryParams` 继承 CapabilityParams 并声明 `input.starrocks.slow_query.v1`。Target resolver 只接 Context+
Params。

- [ ] **Step 3: 实现 projector**

从 Plan step typed arguments 与 Target 还原 canonical disclosure slots；验证 stored confirmed snapshot 是其
一致子集；不读取 IntentDraft。

- [ ] **Step 4: 验证**

```bash
python -m pytest tests/unit/test_slow_query_params.py tests/unit/test_plan_compiler.py tests/unit/test_target_resolver.py tests/contract/test_runtime_async_lifecycle.py tests/evals/test_l1_intent.py tests/security/test_model_context_boundary.py tests/security/test_param_injection.py -q
```

### Task 2.4：迁移 Prometheus binding

**Files:**

- Modify: `src/xiaowei_agent/planning/prometheus/params.py`
- Create: `src/xiaowei_agent/planning/prometheus/slots.py`
- Modify: `src/xiaowei_agent/planning/prometheus/compiler.py`
- Modify: `src/xiaowei_agent/planning/prometheus/target.py`
- Modify: `src/xiaowei_agent/application/default_capabilities.py`
- Modify: `tests/unit/test_prometheus_alert_params.py`
- Modify: `tests/unit/test_prometheus_alert_plan.py`
- Modify: `tests/contract/test_prometheus_alert_runtime.py`
- Modify: `tests/evals/test_m6a_prometheus_l1.py`
- Modify: `tests/security/test_promql_guard.py`

- [ ] **Step 1: RED：required fields 进入 capability clarification**

缺 alert_name/instance → Resolver 可运行、CapabilitySubject 完整、Planner/Gateway=0；子任务补齐后重新
Router+Resolver，subject 完全一致才 ready。operation/input ref 篡改均 incompatible。

- [ ] **Step 2: 实现 verifier 与 projector**

使用 time_range/alert_name/instance/fingerprint allowlist；修正 `[start,end]` docstring 为半开区间；所有
时间保存 UTC。Planner 只接 `PrometheusAlertParams`。

- [ ] **Step 3: 验证**

```bash
python -m pytest tests/unit/test_prometheus_alert_params.py tests/unit/test_prometheus_alert_plan.py tests/contract/test_prometheus_alert_runtime.py tests/evals/test_m6a_prometheus_l1.py tests/security/test_promql_guard.py -q
```

### Task 2.5：迁移 Asset binding

**Files:**

- Modify: `src/xiaowei_agent/planning/assets/params.py`
- Create: `src/xiaowei_agent/planning/assets/slots.py`
- Modify: `src/xiaowei_agent/planning/assets/compiler.py`
- Modify: `src/xiaowei_agent/application/default_capabilities.py`
- Modify: `tests/unit/test_asset_lookup_params.py`
- Modify: `tests/unit/test_asset_plan.py`
- Modify: `tests/unit/test_asset_target.py`
- Modify: `tests/contract/test_asset_inventory_runtime.py`
- Modify: `tests/evals/test_m6a_asset_l1.py`
- Modify: `tests/security/test_asset_scope_isolation.py`

- [ ] **Step 1: RED：一项精确 selector**

无 selector → reason `capability.asset_selector_required` 与固定 alternative fields；两个 selector 或含糊值
→ invalid/clarify；hostname/IP/asset_id 复用现有 canonical parser；模型单独提供的 selector 不确认。

- [ ] **Step 2: 实现 verifier/projector**

`AssetLookupParams` 继承 CapabilityParams、input ref `input.asset.lookup.v1`。Planner 只接 Params。projector
从 step selector_kind/value 与 Target 交叉核对，任何不一致失败。

- [ ] **Step 3: 验证**

```bash
python -m pytest tests/unit/test_asset_lookup_params.py tests/unit/test_asset_plan.py tests/unit/test_asset_target.py tests/contract/test_asset_inventory_runtime.py tests/evals/test_m6a_asset_l1.py tests/security/test_asset_scope_isolation.py -q
```

### Task 2.6：接入 capability clarification 与移除旧 prepare

**Files:**

- Modify: `src/xiaowei_agent/application/runtime.py`
- Modify: `src/xiaowei_agent/application/default_capabilities.py`
- Modify: `src/xiaowei_agent/application/capability_runtime.py`
- Modify: `src/xiaowei_agent/persistence/clarification_records.py`
- Create: `tests/contract/test_runtime_capability_clarification.py`
- Create: `tests/security/test_clarification_subject_binding.py`

- [ ] **Step 1: RED 完整两条澄清链**

RouteSubject：Resolver=0、slots empty。CapabilitySubject：Resolver=1、SlotVerifier incomplete、Planner=0。
子任务不能直接用父 capability；reason/missing 篡改不能决定 capability；父 input ref 不兼容则拒绝新根任务。

- [ ] **Step 2: Runtime 最小编排**

```text
Resolver → select_entry → binding.slot_verifier
incomplete → save CapabilitySubject record → terminal
invalid → REJECTED
ready → exact params type check → planner(candidate, params, context, snapshot)
```

删除 `_prepare_*` 混合职责和 `CapabilityPreparationError` 用户补槽语义；保留真正编译/装配内部错误。

- [ ] **Step 3: 反向搜索 Planner 输入**

```bash
rg -n "draft: IntentDraft|IntentDraft.*planner|planner\(.*draft|_prepare_" src/xiaowei_agent/application src/xiaowei_agent/planning tests
```

Expected: capability planners 不再接收/导入 IntentDraft；没有旧兜底。

- [ ] **Step 4: PR 验证与提交**

```bash
python -m pytest tests/unit/test_capability_input_contracts.py tests/unit/test_slot_verification.py tests/contract/test_runtime_capability_clarification.py tests/contract/test_runtime_async_lifecycle.py tests/contract/test_prometheus_alert_runtime.py tests/contract/test_asset_inventory_runtime.py -q
python -m pytest -m security -q
ruff check .
mypy src
git diff --check
```

精确暂存本 PR 文件后：

```bash
git commit -m "refactor(planning): verify typed capability inputs"
```

Codex 按 SHA 审查通过后才能开始 I1-C。

---

## PR 3 — I1-C：ReadClass、Plan V2 与 Policy 二次防线

本 PR 的单一结果是：只读不再等于天然低风险；operation 静态声明 bounded/restricted，计划携带并纳入
hash，Admission 从当前 snapshot 重派生，Policy 只允许明确 read class。

### Task 3.1：定义 ReadClass 与声明约束

**Files:**

- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/capability.py`
- Modify: `src/xiaowei_agent/contracts/plan.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/capabilities/specs.py`
- Modify: `src/xiaowei_agent/capabilities/prometheus_alert.py`
- Modify: `src/xiaowei_agent/capabilities/asset_inventory.py`
- Modify: `tests/unit/test_capability_spec.py`
- Modify: `tests/unit/test_plan_contracts.py`
- Modify: `tests/security/test_effect_classification.py`

- [ ] **Step 1: RED 交叉约束**

READ+None、non-READ+class、未知 class 全拒绝；三个现有只读 operation 明确 BOUNDED；合成 restricted
operation 可构造；禁止按 SQL 文本自动判断 class。

- [ ] **Step 2: 最小实现**

添加 enum 和两个字段 validator。不要给 ExecutionPlan 添加汇总 read_class。

- [ ] **Step 3: 验证**

```bash
python -m pytest tests/unit/test_capability_spec.py tests/unit/test_plan_contracts.py tests/security/test_effect_classification.py -q
```

### Task 3.2：派生 PlanStep 并升级 plan hash

**Files:**

- Modify: `src/xiaowei_agent/capabilities/effect.py`
- Modify: `src/xiaowei_agent/planning/canonical.py`
- Modify: `src/xiaowei_agent/contracts/plan.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `tests/fakes/fixtures.py`
- Modify: `tests/fakes/admission.py`
- Modify: `tests/conftest.py`
- Modify: `tests/unit/test_hash_vectors.py`
- Modify: `tests/security/test_hash_coverage.py`
- Modify: `tests/security/test_effect_single_source.py`
- Modify: `tests/security/test_plan_determinism.py`
- Modify: `tests/integration/test_plan_drift_detection.py`

- [ ] **Step 1: RED 单一真源/hash 测试**

`build_plan_step` 不接受 read_class 参数；从 OperationSpec 派生。篡改 PlanStep class 改变 hash，但 Admission
仍会用声明重派生拒绝。canonical key map 必须覆盖字段。V1 raw JSON load 返回
`plan.schema_version_unsupported`，不得 Pydantic 默认升级。

- [ ] **Step 2: 实现 schema V2**

`PLAN_SCHEMA_VERSION=2`；Plan validator 必须要求传入版本等于 2。PostgreSQL raw load 在构造 Contract 前
检查版本并映射专用安全异常；不重算旧 plan/approval。

- [ ] **Step 3: 更新 golden vectors**

只根据新 canonical payload 重新生成期望 hash，并人工核对 diff 确实只新增 schema version/read_class
语义。不能用测试实际值机械覆盖 expected 而不查看 payload。

- [ ] **Step 4: 验证**

```bash
python -m pytest tests/unit/test_hash_vectors.py tests/security/test_hash_coverage.py tests/security/test_effect_single_source.py tests/security/test_plan_determinism.py tests/integration/test_plan_drift_detection.py -q
```

### Task 3.3：PolicyProfile 与 Admission

**Files:**

- Modify: `src/xiaowei_agent/contracts/policy.py`
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/governance/profiles.py`
- Modify: `src/xiaowei_agent/governance/policy.py`
- Modify: `src/xiaowei_agent/governance/step_admission.py`
- Modify: `tests/unit/test_governance_profiles.py`
- Modify: `tests/contract/test_step_admission.py`
- Modify: `tests/security/test_workflow_policy.py`
- Create: `tests/security/test_read_class_admission.py`

- [ ] **Step 1: RED profile/Admission 测试**

READ profile 必须有 allowed classes；non-READ profile 必须空。restricted operation + 合法 SELECT + bounded-only
profile → `policy.read_class_not_allowed`，SQLGuard 是否通过都不影响 Gateway=0。篡改 PlanStep bounded 仍按
snapshot restricted 拒绝。

- [ ] **Step 2: 实现最小 policy 参数**

Admission 从 `CapabilitySnapshot` 找 operation，重派生 effect/side/read 三项，再传 ToolPolicy。Policy 不读
用户/模型声明，不做动态评分。

- [ ] **Step 3: 上层 restricted 分流**

Runtime 在已编译计划的聚合函数发现 restricted 时稳定 refuse，Admission/Gateway=0。I1 当前 Registry 不
注册任何 restricted→bounded 替代映射。只在未来已有一个**独立声明为 BOUNDED 的 operation** 时，才能
用 RouteSubject clarify 要求用户重述完整的缩小请求；不能动态改变原 operation 的 ReadClass、不能让
CapabilitySubject 换 operation，也不调用 ApprovalGate。

- [ ] **Step 4: 验证并提交**

```bash
python -m pytest tests/unit/test_governance_profiles.py tests/contract/test_step_admission.py tests/security/test_read_class_admission.py tests/security/test_workflow_policy.py tests/unit/test_hash_vectors.py tests/integration/test_plan_drift_detection.py -q
python -m pytest -m security -q
ruff check .
mypy src
git diff --check
```

精确暂存后：

```bash
git commit -m "feat(governance): classify bounded readonly operations"
```

Codex 按 SHA 审查通过后才能开始 I1-D。

---

## PR 4 — I1-D：执行披露屏障与 I1 质量闭环

本 PR 的单一结果是：任何计划第一次 Admission/Gateway 前都已有可查询、可审计、从 Plan/Target 重建的
披露；所有渠道展示相同事实，并用 Eval 证明 I1 整体边界。

### Task 4.1：定义纯 ExecutionDisclosure 投影

**Files:**

- Create: `src/xiaowei_agent/contracts/disclosure.py`
- Create: `src/xiaowei_agent/planning/disclosure.py`
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/application/capability_runtime.py`
- Create: `tests/unit/test_execution_disclosure.py`
- Create: `tests/security/test_disclosure_projection.py`

- [ ] **Step 1: RED 投影测试**

验证 capability/version/target/resource/read disposition/fields/external target access；bounded 多步聚合；
restricted；side effect；父 snapshot 旧→新即时展示；projector 非 allowlist、非 canonical、值不一致、无法
反投影都拒绝且不泄漏值。

- [ ] **Step 2: 实现纯 projection**

输入仅 StoredPlan 的 Plan/Target、当前精确 binding 和 Runtime 已重新验证后传入的可选父
ClarificationRecord。输出严格 Contract；不做 I/O、不读 ModelArtifact/IntentDraft/原文。
`external_target_access` 表示计划语义，不宣称 adapter 已联网。Runner 不自行查询 ClarificationRecordStore。

- [ ] **Step 3: 验证**

```bash
python -m pytest tests/unit/test_execution_disclosure.py tests/security/test_disclosure_projection.py -q
```

### Task 4.2：在 Runner 建立披露就绪屏障

**Files:**

- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/trace_events.py`
- Modify: `src/xiaowei_agent/runners/deterministic.py`
- Modify: `src/xiaowei_agent/runners/runner.py`
- Modify: `tests/fakes/runner.py`
- Modify: `tests/contract/test_deterministic_runner.py`
- Modify: `tests/contract/test_trace_stages.py`
- Create: `tests/security/test_disclosure_barrier.py`
- Modify: `tests/security/test_admission_bypass.py`

- [ ] **Step 1: RED 顺序/故障测试**

记录 call timeline，严格断言：

```text
plan_store.save
< disclosure.project
< durable DISCLOSURE/OK
< admit_step
< gateway.invoke
```

分别让 Plan save、projection、validation、durable audit 失败/未知，断言 Admission=0、Gateway=0。崩溃
恢复可产生多条 disclosure event，但每个有效 attempt 必须在本 attempt Admission 前有 OK。

- [ ] **Step 2: 最小实现**

在 Runner `_start` 保存 Plan/Target 后、任何 `_admit` 前调用 pure projector 并经 DurableTraceSink
`LOG_AND_DURABLE` 写 `PipelineStage.DISCLOSURE`。不新增 Task status、Store、unique event 或 ACK。

- [ ] **Step 3: 变异反证**

临时创建隔离 worktree/pycache，移除或交换 disclosure 与 admission 调用，运行
`tests/security/test_disclosure_barrier.py` 必须红；恢复真实代码后再绿。记录命令与尾部输出，临时变体不提交。

- [ ] **Step 4: 验证**

```bash
python -m pytest tests/contract/test_deterministic_runner.py tests/contract/test_trace_stages.py tests/security/test_disclosure_barrier.py tests/security/test_admission_bypass.py -q
```

### Task 4.3：TaskView 与渠道一致投影

**Files:**

- Modify: `src/xiaowei_agent/contracts/task.py`
- Modify: `src/xiaowei_agent/application/task_view_runtime.py`
- Modify: `src/xiaowei_agent/rendering/feishu.py`
- Modify: `src/xiaowei_agent/interfaces/web_static/detail.js`
- Modify: `src/xiaowei_agent/interfaces/web_static/app.js`
- Modify: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `tests/contract/test_task_view_runtime.py`
- Modify: `tests/contract/test_channel_render_parity.py`
- Modify: `tests/contract/test_feishu_worker.py`
- Modify: `tests/contract/test_web_static_assets.py`
- Modify: `tests/security/test_task_view_runtime_authority.py`
- Modify: `tests/security/test_web_xss.py`

- [ ] **Step 1: RED nonterminal/terminal 查询**

无 Plan 时 disclosure=None；Plan 已存时无论 PLANNING/RUNNING/AWAITING_APPROVAL/terminal 都从 Plan/Target
重建一致 disclosure；鉴权失败不暴露；clarification/preplan rejection 无 disclosure；Plan/projector 损坏
返回完整性失败而非旧摘要。

- [ ] **Step 2: 实现统一 TaskView**

TaskView 独立字段：`render`、`clarification`、`disclosure` 各有严格 presence invariant。Web/飞书只渲染
这些字段，不重算业务规则。文案不能使用“已送达/已阅读/已确认”；副作用页面继续显示正式审批状态。

- [ ] **Step 3: 安全渲染测试**

confirmed scalar、resource id 和变化展示必须走既有 escaping/redaction；secret-shaped 值理论上在 Store 前
已拒绝，UI 再保留防御性转义但不得把 redacted 文本存回事实。

- [ ] **Step 4: 验证**

```bash
python -m pytest tests/contract/test_task_view_runtime.py tests/contract/test_channel_render_parity.py tests/contract/test_feishu_worker.py tests/contract/test_web_static_assets.py tests/security/test_task_view_runtime_authority.py tests/security/test_web_xss.py -q
```

### Task 4.4：I1 Eval 与跨渠道闭环

**Files:**

- Create: `tests/evals/fixtures/i1_interaction_cases.json`
- Create: `tests/evals/test_i1_interaction_l0.py`
- Create: `tests/evals/test_i1_interaction_l1.py`
- Create: `tests/integration/test_i1_interaction_postgres.py`
- Modify: `tests/integration/test_m7_channel_flow.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `tests/security/test_external_content.py`
- Modify: `tests/security/test_intent_pollution.py`
- Modify: `tests/security/test_no_network.py`

- [ ] **Step 1: 建立版本化 Eval fixture**

包含：四种 kind+unknown、同义句/错别字、环境一致/冲突/含糊、当前三 capability、缺槽/补槽/改值、多轮
一对一、普通聊天、知识查询、用户粘贴日志、日志内注入、伪只读写操作、restricted SELECT、模型自补
槽位和 secret-shaped 输入。

fixture 不包含真实生产数据/连接/secret。I1 不设置总体准确率门；每个安全不变量使用逐例绝对期望。

- [ ] **Step 2: PostgreSQL 完整生命周期**

用真实 PostgreSQL 证明：root clarify→child ready→Plan→Disclosure→Gateway(fake)→Outcome；并发单次消费；
artifact/record recovery；环境 mismatch 下游全零；V1 plan fail-closed。

- [ ] **Step 3: 渠道/Compose smoke**

API、CLI、Web、飞书都提交同一 case 并得到同一 kind/disposition/reason；interfaces 无独立关键词规则。
Compose 只用 fake/recording，网络窄化测试继续证明不访问真实目标或模型。

- [ ] **Step 4: Eval 验证**

```bash
python -m pytest tests/evals/test_i1_interaction_l0.py tests/evals/test_i1_interaction_l1.py tests/integration/test_i1_interaction_postgres.py tests/integration/test_m7_channel_flow.py tests/contract/test_compose_smoke_script.py tests/security/test_external_content.py tests/security/test_intent_pollution.py tests/security/test_no_network.py -q
```

### Task 4.5：最终 I1 closure

**Files:**

- Modify: `AGENT_HANDOFF.md`
- Modify: `README.md`（仅当实际使用/状态与 I0 文案不同）
- Modify: `src/xiaowei_agent/capabilities/doc.py`
- Modify: `docs/CAPABILITIES.md`（内容必须等于既有 renderer 对当前 Registry 的输出）
- Modify: `tests/security/test_capabilities_doc.py`

- [ ] **Step 1: 全量四门**

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

Expected: 四条都 exit 0。保留每条命令尾部真实输出；任何失败都先修根因并重跑该门，不能只重跑一个
挑选后的用例宣布完成。

- [ ] **Step 2: migration/Compose 深档验证**

```bash
python -m pytest tests/integration/test_migration_paths.py tests/integration/test_clarification_child_concurrency_postgres.py tests/integration/test_i1_interaction_postgres.py -q
docker compose config
python -m pytest tests/contract/test_compose_smoke_script.py -q
```

Expected: PostgreSQL 与 Compose fake 闭环通过；仍不构成真实模型/渠道/目标证据。

- [ ] **Step 3: 静态边界扫描**

```bash
rg -n "IntentModelPort|generate_intent|load_intent|save_intent|ContextAssembler|parent_task_id|AWAITING_CLARIFICATION|DisclosureStore|consumed" src tests
rg -n "IntentDraft" src/xiaowei_agent/planning src/xiaowei_agent/application/capability_runtime.py
git diff --check
git status --short
```

Expected: 无旧运行真源、无 Planner→IntentDraft、无消费状态或披露存储。substring 命中的
`clarification_parent_task_id` 人工区分并记录。

- [ ] **Step 4: TDD 反证汇总**

至少对以下承重保护各做一次隔离变异并证明对应测试变红：

- Router bypass；
- artifact winner 不经 Store 返回；
- clarification parent unique constraint；
- model slot 直接进入 Params；
- Admission 信任 PlanStep read_class；
- disclosure audit 后置于 Gateway。

每次恢复真代码后重跑相应测试；不提交变异代码或 pycache。

- [ ] **Step 5: 更新 handoff**

分四段写：

1. 已验证：命令、结果、PR/head/merge SHA、CI run（若实际存在）；
2. 只读推理：未做运行实验的结论；
3. 未覆盖：真实模型、真实渠道、真实目标、部署、canary、UAT；
4. 残余风险：Provider at-least-once 窗口、披露非送达、I2–I4 未实现、restricted 授权流程延期。

不得把“源码/测试完成”写成“用户已能正常使用完整智能入口”。

- [ ] **Step 6: 精确提交**

```bash
git add src/xiaowei_agent/contracts/disclosure.py src/xiaowei_agent/contracts/enums.py src/xiaowei_agent/contracts/trace_events.py src/xiaowei_agent/contracts/task.py src/xiaowei_agent/contracts/__init__.py
git add src/xiaowei_agent/planning/disclosure.py src/xiaowei_agent/application/capability_runtime.py src/xiaowei_agent/application/task_view_runtime.py src/xiaowei_agent/runners/deterministic.py src/xiaowei_agent/runners/runner.py
git add src/xiaowei_agent/rendering/feishu.py src/xiaowei_agent/interfaces/web_static/detail.js src/xiaowei_agent/interfaces/web_static/app.js src/xiaowei_agent/interfaces/web_models.py src/xiaowei_agent/capabilities/doc.py docs/CAPABILITIES.md AGENT_HANDOFF.md
git add tests/unit/test_execution_disclosure.py tests/security/test_disclosure_projection.py tests/fakes/runner.py tests/contract/test_deterministic_runner.py tests/contract/test_trace_stages.py tests/security/test_disclosure_barrier.py tests/security/test_admission_bypass.py
git add tests/contract/test_task_view_runtime.py tests/contract/test_channel_render_parity.py tests/contract/test_feishu_worker.py tests/contract/test_web_static_assets.py tests/security/test_task_view_runtime_authority.py tests/security/test_web_xss.py tests/security/test_capabilities_doc.py
git add tests/evals/fixtures/i1_interaction_cases.json tests/evals/test_i1_interaction_l0.py tests/evals/test_i1_interaction_l1.py tests/integration/test_i1_interaction_postgres.py tests/integration/test_m7_channel_flow.py tests/contract/test_compose_smoke_script.py tests/security/test_external_content.py tests/security/test_intent_pollution.py tests/security/test_no_network.py
git commit -m "feat(interaction): enforce execution disclosure barrier"
```

提交前仍需用 `git status --short` 核对：上列路径不能夹带其他人的修改，且本 PR 实际改动不能漏暂存。

- [ ] **Step 7: 审查与退出**

Claude 提供精确 head SHA、完整 diff、验证尾部；Codex 基于该 SHA 逐行审查。所有 I1 PR 合入后，在最新
main 重跑 closure 四门并记录 main SHA/CI。项目负责人明确批准 I1 后才归档；不自动开始 I2、M8 或真实
联调。

---

## 实施者自审清单

在把计划交给审查者前逐项确认：

- [ ] spec 的每条冻结决策至少有一个实现 task 和一个验收断言。
- [ ] 没有待定占位、延后测试或模糊的类比实现指令。
- [ ] 所有测试命令都使用 `python -m pytest`，没有裸 `pytest`。
- [ ] 所有 git 暂存都明确禁止 `git add -A`。
- [ ] I0 与 I1 不在同一 PR；I1-A/B/C/D 的依赖方向单向。
- [ ] 没有让 model/router/interface/parent subject 获得 capability、目标、授权或执行权。
- [ ] `confirmed_slots` 是 canonical full snapshot，RouteSubject 永远空。
- [ ] 时间为 UTC 半开区间，父绝对时间不重算。
- [ ] `input_schema_ref` 与 `argument_schema_ref` 从未比较或相互替代。
- [ ] Planner 只接 capability Params，Runtime 做精确类型令牌检查。
- [ ] restricted read 不复用 ApprovalGate，写操作仍必须正式审批。
- [ ] Plan V1、旧 parent、旧 intent artifact 都没有静默升级路径。
- [ ] disclosure 是持久事实可查询屏障，不是送达/理解/审批证明。
- [ ] 最终结论分清测试、真实运行、部署、canary 和用户验收。

## 交接方式

本计划通过审查后，把 **PR 0 / I0-DOC** 单独发给 Claude 执行。I0 合入并获批准后，再逐个发送 I1-A、
I1-B、I1-C、I1-D；每个 PR 完成后由 Codex 按精确 SHA 审查，项目负责人确认后归档，再开始下一个。
