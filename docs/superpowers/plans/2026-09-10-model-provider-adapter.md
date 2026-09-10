# Real Model Provider Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** 接通一个明确选定的真实模型供应商，让模型只产出严格结构化的意图草案和解释建议；任何模型输出都没有执行权，失败时回退现有规则解释器。

**Architecture:** 新增单一 provider adapter，不做多供应商注册中心。Runtime 异步等待受限的 `IntentInterpreter`；组合解释器先调用模型，失败或无效时调用现有规则实现。解释建议只消费安全 `RenderPayload`，不能改变任务状态、Evidence、下一步执行或引用。

**Tech Stack:** Python 3.11、Pydantic strict schema、供应商官方 Python SDK 或标准 HTTP 客户端（二选一，由 ADR 固定）、pytest、Docker Compose。

**Spec:** [真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)

**Global Constraints:** 模型供应商、模型标识、API 协议、数据保留条款和预算未由负责人明确前，本计划停在 Task 1，禁止实现或真实调用。模型请求不得包含 secret、连接串、完整身份目录、原始工具错误、未经批准的历史 Evidence 或数据库原始行。

## 非目标

- 不做多供应商自动切换、模型训练、微调、向量库、Agent 框架或 prompt 管理平台。
- 不让模型生成可执行 SQL、选择工具、批准任务、调用 adapter 或改变任务终态。
- 不在本阶段接 StarRocks、Admin 或部署正式环境。

## ADR

新增 `docs/adr/ADR-015-real-model-provider-boundary.md`。它必须先由负责人填入并批准唯一供应商、模型、协议、数据保留和预算，随后才能实现 adapter。

## PR 边界

- 建议拆为两个 PR：
  - PR 3A：ADR、数据边界、成本/超时契约和离线 fake；
  - PR 3B：唯一 provider adapter、Runtime 装配、Compose 配置和离线测试。
- 不包含 StarRocks 真实连接、Admin、部署或模型训练/微调。
- ADR-015 是 PR 3A 的退出门，不以代码默认值代替负责人选择。

## 进入条件

- 阶段 2 已完成或项目负责人明确同意在飞书 test-env 证据前单独开发离线模型 PR。
- 从当时最新 `main` 创建 `claude/real-model-provider` 并重新记录 SHA/status。
- 项目负责人书面明确且一次只选一个：供应商、模型标识、API 协议、区域、数据保留/训练策略、每请求输入输出上限、供应商账号/项目硬预算、允许环境和 secret reference 形式。
- 对供应商官方文档做只读核对，固定支持的 JSON schema/structured-output 能力、超时和错误码；结果写入 ADR，不把网页宣传当运行证据。

## Task 1：完成供应商与数据边界 ADR

**Files:**

- Create: `docs/adr/ADR-015-real-model-provider-boundary.md`
- Modify: `ARCHITECTURE.md`
- Create: `docs/checklists/model-data-boundary.md`
- Create: `tests/contract/test_model_boundary_docs.py`

**Step 1: 写 RED 文档契约**

测试 ADR/清单必须明确：唯一供应商和模型、发送字段白名单、禁止字段、区域/保留策略、15 秒超时、输入/输出 token 上限、供应商项目硬预算、错误分类、fallback、审计字段和关闭开关。

Run: `python -m pytest tests/contract/test_model_boundary_docs.py -q`

Expected: ADR 尚不存在，测试失败。

**Step 2: 写 ADR 并更新模块边界**

ADR 必须否决：模型生成可执行 SQL、模型选择 capability/adapter、模型直接调用工具、动态 provider URL、多 provider 自动路由、保存完整 prompt/response。若新增 `modeling/` 模块，先在 `ARCHITECTURE.md` 固定其唯一责任；若不新增模块，adapter 放 `interfaces/model_provider.py`，应用组合放 `application/model_intent.py`。

**Step 3: 提交 PR 3A**

```bash
git add docs/adr/ADR-015-real-model-provider-boundary.md ARCHITECTURE.md docs/checklists/model-data-boundary.md tests/contract/test_model_boundary_docs.py
git commit -m "docs(model): fix provider and data boundary"
```

项目负责人批准 ADR 前停止，不开始 Task 2。

## Task 2：把解释器契约改成异步且保持确定性回退

**Files:**

- Modify: `src/xiaowei_agent/capabilities/intent.py`
- Modify: `src/xiaowei_agent/application/runtime.py`
- Create: `src/xiaowei_agent/application/model_intent.py`
- Modify: `tests/unit/test_intent_interpreter.py`
- Create: `tests/contract/test_runtime_model_intent.py`
- Create: `tests/security/test_model_authority_boundary.py`

**Step 1: 写 RED 异步契约测试**

测试：Runtime 必须 `await` 解释器；模型超时、限流、网络异常、无效 schema 和未知字段都精确回退规则解释器；fallback 在 trace 中只记录闭集原因，不记录 prompt/response；相同规则输入仍产生相同草案。

Run: `python -m pytest tests/unit/test_intent_interpreter.py tests/contract/test_runtime_model_intent.py tests/security/test_model_authority_boundary.py -q`

Expected: 现有同步协议不能满足测试。

**Step 2: 最小改成 async Protocol**

```python
class IntentInterpreter(Protocol):
    async def interpret(
        self, *, text: str, context: RequestContext
    ) -> IntentDraft: ...
```

把 `RuleBasedIntentInterpreter.interpret()` 改为 async，无 I/O；Runtime 只做一次 await。组合解释器负责 15 秒边界和 fallback，不把异常交给 Resolver。

**Step 3: 证明模型没有执行字段**

恶意模型响应包含 gateway、operation、SQL、target、permissions、approval 或 tool order 时，Pydantic `extra="forbid"` 拒绝整个响应并回退规则解释器；不得“删除危险字段后继续执行”。

Run: `python -m pytest tests/security/test_model_authority_boundary.py -q`

Expected: 全部通过。

**Step 4: 提交异步边界**

```bash
git add src/xiaowei_agent/capabilities/intent.py src/xiaowei_agent/application/runtime.py src/xiaowei_agent/application/model_intent.py tests/unit/test_intent_interpreter.py tests/contract/test_runtime_model_intent.py tests/security/test_model_authority_boundary.py
git commit -m "refactor(intent): add async model-safe boundary"
```

## Task 3：实现唯一 provider adapter

**Files:**

- Create: `src/xiaowei_agent/interfaces/model_provider.py`
- Create: `src/xiaowei_agent/contracts/model.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Create: `tests/contract/test_model_provider_adapter.py`
- Modify: `tests/security/test_model_authority_boundary.py`
- Modify: `tests/security/test_module_layering.py`
- Modify only if ADR selected an SDK: `pyproject.toml`
- Modify only if ADR selected an SDK: `uv.lock`

**Step 1: 写 RED provider 契约测试**

用离线 transport/fake 覆盖成功、15 秒超时、401/403/429/5xx、响应超限、无效 UTF-8/JSON、schema 多字段、token 用量缺失和取消传播。provider 原始正文永不进入异常或日志。

Run: `python -m pytest tests/contract/test_model_provider_adapter.py tests/security/test_model_authority_boundary.py -q`

Expected: adapter 尚不存在，测试失败。

**Step 2: 定义最小 DTO**

```python
class ModelIntentResponse(Contract):
    intent: StrictStr
    slots: FrozenStrMap
    missing: tuple[StrictStr, ...]
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

class ModelUsage(Contract):
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)
```

供应商 adapter 只返回这两个受限对象；不向下游暴露通用 SDK response。

**Step 3: 实现 adapter 与预算护栏**

- endpoint 和 API 形态按 ADR 固定；不接受任意 base URL；
- API key 只从只读文件读取；
- 请求前做字符/字段/token 估算上限；响应后记录 token 计数和成本估算；
- 达到单请求上限时 fail-closed；供应商项目必须另设硬预算，供应商返回配额/预算耗尽时立即回退规则；本阶段不自建本地计费账本；
- 不持久化完整输入输出，只记录 trace_id、provider/model、耗时、token 数、成本估算、fallback 原因和配置版本。

**Step 4: 提交 adapter**

```bash
git add src/xiaowei_agent/interfaces/model_provider.py src/xiaowei_agent/contracts/model.py src/xiaowei_agent/contracts/__init__.py tests/contract/test_model_provider_adapter.py tests/security/test_model_authority_boundary.py tests/security/test_module_layering.py
git commit -m "feat(model): add bounded provider adapter"
```

若 ADR 选择 SDK，依赖文件单独精确暂存并在同一 PR 中接受供应链审计。

## Task 4：增加解释建议但不改事实

**Files:**

- Modify: `src/xiaowei_agent/contracts/model.py`
- Create: `src/xiaowei_agent/application/model_advisory.py`
- Modify: `src/xiaowei_agent/application/runtime.py`
- Modify: `src/xiaowei_agent/rendering/generic.py`
- Create: `tests/contract/test_model_advisory.py`
- Modify: `tests/security/test_model_authority_boundary.py`

**Step 1: 写 RED 不可越权测试**

模型解释只能返回一个长度受限的 `advisory_text` 和模型配置版本。测试证明它不能改变 `RenderPayload.status`、facts、refs、next_steps、task owner 或执行结果；无效解释直接丢弃，原确定性 RenderPayload 保持不变。

**Step 2: 只发送安全投影**

输入只含确定性渲染后的标题、结论和已批准的摘要字段，不含原始 Evidence row、provider 错误、secret、连接信息或完整用户历史。

**Step 3: 实现最小合并规则**

解释建议只追加一个明确标记为“模型解释”的 section；失败时不影响原回复。模型不能生成 refs 或 next steps。

Run: `python -m pytest tests/contract/test_model_advisory.py tests/security/test_model_authority_boundary.py -q`

Expected: 全部通过。

**Step 4: 提交 advisory**

```bash
git add src/xiaowei_agent/contracts/model.py src/xiaowei_agent/application/model_advisory.py src/xiaowei_agent/application/runtime.py src/xiaowei_agent/rendering/generic.py tests/contract/test_model_advisory.py tests/security/test_model_authority_boundary.py
git commit -m "feat(model): add non-authoritative advisory"
```

## Task 5：配置、装配与 Compose 默认关闭

**Files:**

- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `docker-compose.yml`
- Modify: `tests/unit/test_config_happy.py`
- Create: `tests/unit/test_config_model_provider.py`
- Modify: `tests/contract/test_compose_contract.py`
- Create: `tests/evals/test_model_fallback_l0.py`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`

**Step 1: 写 RED 配置矩阵**

开关默认关闭；关闭时出现任何 provider 配置均拒绝，开启时缺模型标识、key file、预算、token 上限或允许环境均拒绝。测试环境之外默认不能启用。

**Step 2: 实现唯一装配**

只有 task worker 的 composition root 可以创建模型 adapter；Web、飞书 listener、channel worker 和内部 API 不持有模型客户端。关闭时装配纯规则解释器。

**Step 3: 离线 eval**

覆盖已知意图、补槽、歧义、prompt injection、外部文本越权、provider timeout 和成本耗尽。评分分开记录“模型命中”和“安全回退”，不能用平均准确率掩盖一次越权。

**Step 4: 提交装配**

```bash
git add src/xiaowei_agent/config.py src/xiaowei_agent/interfaces/local_stack.py docker-compose.yml tests/unit/test_config_happy.py tests/unit/test_config_model_provider.py tests/contract/test_compose_contract.py tests/evals/test_model_fallback_l0.py README.md AGENT_HANDOFF.md
git commit -m "chore(model): wire disabled provider profile"
```

## 验证命令

```bash
python -m pytest tests/contract/test_model_provider_adapter.py tests/contract/test_model_advisory.py tests/evals/test_model_fallback_l0.py -q
python -m pytest -m security -q
python -m pytest -q
ruff check .
mypy src
docker compose config
git diff origin/main...HEAD --check
```

真实模型 test-env 验证必须另获现场 GO，使用小规模脱敏样本；没有真实调用时最高证据为 `tests`。

## 退出标准

- 供应商、模型、数据保留和预算均由负责人批准并写入 ADR。
- 模型结构化输出越权时整体拒绝；Runtime、Resolver、Planner、Policy、SQLGuard 的权威顺序不变。
- 15 秒超时、fallback、token/成本护栏、脱敏审计和默认关闭均有测试。
- 解释建议不能改变事实、状态、引用、next steps 或执行。
- 未经现场 GO 不标记 `test-env verified`，也不开始阶段 4。

## 回滚

关闭模型 feature flag 并重建 task worker，composition root 回到 `RuleBasedIntentInterpreter`。任务、计划、证据和 capability 版本不变。
