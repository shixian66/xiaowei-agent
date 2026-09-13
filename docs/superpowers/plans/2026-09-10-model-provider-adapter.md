# RI3 Gemini 真实模型最小接入实施计划

> **For Claude/Codex implementers:** 本文已获批准，并于 2026-09-12 收到“开始 RI3”离线开工口令。
> 按 PR 顺序逐个实施；每个 PR 都先写失败测试，再写最小代码，再做反证和独立审查。不得把后续 PR
> 偷渡进前一个 PR。真实 Gemini 网络调用仍须 PR 3E 的独立现场 GO。

**Goal:** 用 Gemini 增强自由语言理解、慢查询解释和 Web 连续追问，同时保持 capability、目标、SQL、
审批、工具和执行完全由现有确定性链路控制。

**Architecture:** 只增加两个窄模型端口和一个 Gemini adapter。模型输出经严格 Pydantic 复验后，作为
`IntentDraft` 或只读 `ModelAdvisory` 进入现有 Runtime；worker 复用现有 lease/fencing，并把已接受的
模型事实持久化。上下文使用显式 `parent_task_id`，Web 先行，不使用 provider 会话。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、asyncio、PostgreSQL/Alembic、Docker Compose、官方
`google-genai` SDK、pytest、Ruff、mypy。

**Spec:** [ADR-015](../../adr/ADR-015-real-model-provider-boundary.md)；总体边界见
[真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)。

## 状态与授权

- 状态：Approved V7.1（2026-09-12；PR 3B 复审澄清独立 nullable usage、派生字段字节上限与固定宿主路径）。
- PR 3A 只收口计划文档，没有源码实现、依赖安装、真实 key、网络调用、部署或测试环境证据。
- 项目负责人已下达“开始 RI3”；该口令只授权按本计划顺序离线实现，不授权真实网络调用。
- PR 3B–3D 只做离线实现；真实 Gemini 调用必须等 PR 3E 的独立现场 GO。
- ADR-007 的 E1 写闸保持关闭；RI3 不增加任何写运维能力。

## Independent review disposition (V7)

| ID | Root cause | Actual impact | Plan-level resolution and proof |
| ---: | --- | --- | --- |
| 1 | The draft confused RI4's proposed query budgets with current source facts. | It would change timeout assumptions before RI4 and make RI3 evidence false. | Preserve the current 25-second query-timeout upper bound and 30-second read-only policy cap; test those bounds remain unchanged. |
| 2 | The draft treated the full stored-submission checksum as the same-key conflict predicate. | Parent-aware retries could be specified against the wrong digest and silently break idempotency or later fail row-integrity readback. | Add non-null parent only to the semantic request digest and stored submission checksum, never the scope digest; update every create/readback recomputation site, freeze null-parent bytes and test same/different parent cases. |
| 3 | The host Compose key was mistaken for a Settings environment key. | Adding it to `.env.example` breaks the exact Settings-key contract and risks broader container exposure. | Keep plaintext only in the fixed Git-ignored host file; pass it through a file-backed Compose secret mounted only on worker; assert host-only key names are absent from Settings, `.env.example` and container environments. |
| 4 | Persistence work listed contracts but not every backend and row mapping. | One backend could pass while another loses artifacts or parent context. | PR 3C covers fake/PostgreSQL/memory artifact paths and mappings; PR 3D covers fake/PostgreSQL plus a memory-backend no-change inspection; bind shared backend suites. |
| 5 | New Protocols had no static/runtime conformance anchors. | Implementations could drift while tests exercised only one concrete object. | Add `_conformance.py` assignments and exact protocol-conformance tests for every new port/store. |
| 6 | The dependency plan named an SDK without the repository's exact-set baseline. | The lock, wheel identity, license, or typing marker could drift unnoticed. | Pin `google-genai==2.23.0`, audit the exact wheel/hash/license/`py.typed`, and extend dependency-baseline tests. |
| 7 | Existing module-layer tests cannot inspect imports inside a third-party SDK. | The SDK could bypass the configured domain/client boundary or expose unsupported features. | Add an SDK-boundary test over the pinned artifact and adapter AST/import surface. |
| 8 | The new shared persistence suite was not registered centrally. | Tests could exist but never run for memory or PostgreSQL. | Register the model-artifact suite and both bindings in `test_suite_bindings.py`; add a negative registry test. |
| 9 | Real intent was allowed on the compatibility `handle()` path before durable task creation. | Provider output could affect planning without a task, grant, or insert-once recovery record. | Allow real intent only in granted `execute_task()`; prove `handle()` makes zero provider/artifact calls. |
| 10 | The draft rendered before advisory and also after terminal state. | Display prose could enter the artifact digest and rendering could happen twice with different facts. | Digest only typed projection/revisions; persist advisory before terminal; render exactly once from terminal TaskView/`handle()`. |
| 11 | The recovery trade-off for a pre-terminal advisory was not stated. | A task can remain RUNNING up to 180 seconds longer, but moving it post-terminal would create a second delivery/outbox problem. | Keep pre-terminal advisory for RI3, explicitly accept the latency, and prove committed tool steps are adopted from the journal without Gateway replay; only an unsaved advisory may repeat. |
| 12 | The draft copied a fixed slow-query field tuple and did not assign ownership of the projector that consumes it. | RI4 surface changes could silently desynchronize model input from evidence contracts; placing the projector beside the renderer would also violate the existing `rendering` import closure. | Put the projector in `application/model_advisory.py`, derive names/order from `SLOW_QUERY_SURFACE.allowed_columns`, and keep `rendering/` limited to display with no `capabilities` import; pair surface revisions and module-layer tests. |
| 13 | The closed trace stages had no typed model observation. | Operators could not distinguish model latency/usage/fallback without unsafe free text, and weak numeric types could admit booleans or invalid counters. | Add `PipelineStage.MODEL` and strictly bounded typed aggregate metadata; keep prompt/response/error text and free-form model detail out. |
| 14 | Heartbeat extraction left periodic ownership in Runner, then incorrectly classified the shared lease TTL and one-shot grant renewal as heartbeat-only. | A long model stage could lose the lease or two periodic owners could race; deleting `lease_ttl_seconds` would also break the start/resume grant-validity renewal. | Move only periodic heartbeat ownership to one application helper; remove Runner's interval/sleep state and `_heartbeat`/`_run_with_heartbeat`, but retain `DEFAULT_LEASE_TTL_SECONDS`, `lease_ttl_seconds` and `_require_current_grant` with start/resume tests. |
| 15 | The draft invented a `scrub_text()` failure branch and bounded data too late. | Tests would target dead behavior while oversized/Unicode input could consume unbounded redaction/serialization work. | Bound raw characters/UTF-8/history before total scrubbing, then reserialize and recheck the final cap; test exact limit, +1, pathological Unicode, and zero-call rejection. |
| 16 | Compose merge, platform, CLI version, secret grants and feature-flag ownership were implicit. | Worker might lose the PostgreSQL secret, other services might gain the key or an enabled-without-secret state, or a static config check might be mistaken for a working mount. | Define an explicit model override whose worker-local environment owns the flag, require Compose 2.24.4+ on Linux containers, verify the merged declaration, run a split-fake mount preflight, and reject Swarm/`docker stack deploy`. |
| 17 | Assembly tests covered pieces but not Settings/local-stack wiring. | A correct adapter could still be disabled, misconfigured, or installed on the wrong process. | Add config happy/negative cases and local-stack enabled/disabled assembly tests, including zero secret access while disabled. |


## 大白话范围

这一轮只做三件用户能感知的事：

1. 用户说得更自由时，Gemini 帮忙整理成小维已有的结构化意图；
2. 小维查到慢查询证据后，Gemini 补一段更自然的分析；
3. Web 用户明确点“继续这个任务”时，小维带上经过检查的历史继续聊。

模型出故障就回到现在的规则解释器和确定性回答。模型永远不能自己查库、写 SQL、选集群或调用工具。

## 不做什么

- 不支持 OpenAI、DeepSeek 或通用 provider registry；以后有第二个真实需求再加 adapter。
- 不使用 Gemini Interactions API、tool calling、search、code execution、file upload 或 provider session。
- 不新增 450 秒任务总 deadline、`TaskPhase`、`TaskAttemptSupervisor` 或第二套任务状态机。
- 不建设 model-call reservation、transport-attempt 明细账或统一 `TaskTerminalResult`。
- 不删除现有 `XiaoweiRuntime.handle()`，不重写整个 Runtime。
- 不统一重构飞书、StarRocks、PostgreSQL 和 Gemini 的凭证读取。
- 不做 Admin 模型选择器、任意 endpoint、key 上传/回显/版本化或 `worker_start_id` readback。
- 不顺带修 Web/飞书 task、binding、projection 的聚合事务。
- RI3 首版不做飞书回复上下文；等待 RI2 验证真实 reply/thread 语义。
- 不改 slow-query capability 版本，不把现有 `max_model_tokens=4000` 提升到 32768。
- 不做多 worker 扩容、微服务、队列、LangGraph、Multi-Agent 或向量数据库。

## 固定产品与技术口径

| 项目 | 首版固定值 |
| --- | --- |
| Provider | Google Gemini Developer API |
| Model | `gemini-3-flash-preview` |
| SDK | Official `google-genai==2.23.0`; implementation stops for plan re-review if the audited wheel differs |
| API | `v1beta` at canonical origin `https://generativelanguage.googleapis.com`; `client.aio.models.generate_content()` + structured JSON response |
| 意图 | low thinking；60 秒总预算；2,048 output tokens；仅指定错误最多重试 1 次 |
| 诊断 | high thinking；180 秒总预算；4,000 output tokens；不重试 |
| Key | 宿主 Git-ignored key 文件 → file-backed Compose secret → worker 固定文件 |
| 默认状态 | 关闭；只有显式 model Compose override 才启用 |
| 成本 | 不设本地费用硬封顶；记录安全 usage、次数、延迟和 fallback；PR 3E 核验供应商账户告警 |
| 上下文 | 显式父任务；最多 20 个父任务、64,000 字符；Web 先行 |
| 真实证据上限 | `test-env verified`；不等于 deployed/canary/user-accepted |

## 运行语义

### 意图理解

```text
durable worker execute_task(grant, submission)
  → load accepted intent
  ├─ 已存在 → 直接复用
  └─ 不存在
       ├─ 模型关闭/不可用/失败 → 规则解释器
       └─ 模型成功 → 严格本地复验
                    ↓
            insert-once accepted intent
                    ↓
          Resolver → Planner → Runner
```

模型返回 `capability_id`、`target`、`sql`、`tool`、`approval` 或任何 schema 外字段时整体拒绝，再走规则
解释器。不能“删掉坏字段后继续”。

The load/save path above exists only inside the granted durable `execute_task()`.
The compatibility `handle()` path stays deterministic, keeps interpretation and
planning before `create_task`, and never calls the real provider or model-artifact store.

### 慢查询分析

```text
Runner → Evidence → Answerability
                                ↓
                  up to 20 rows × SLOW_QUERY_SURFACE columns
                                ↓
                    Gemini advisory（可失败）
                                ↓
              保存 advisory → 提交原有任务终态
```

advisory 只能增加“模型分析/建议/未确认项”展示，不改确定性状态、事实、限制、证据引用和 `next_steps`。
没有证据、字段不合规、provider 失败或 timeout 时，直接返回现在的确定性答案。

The worker does not build a `RenderPayload` before the advisory call. Its input digest
binds capability/surface/evidence-contract revisions, the typed projection and sampled
flag; it never binds display prose or a second rendered summary.

### 崩溃与恢复

- 已保存 accepted intent/advisory：恢复后直接复用，不再次调用模型。
- provider 已收到但本地尚未保存就崩溃：恢复后允许再调一次；这是明确接受的 at-least-once。
- Advisory runs before the terminal transition, so the task can remain `RUNNING` for
  up to 180 additional seconds. A committed StarRocks step is adopted from the step
  journal after restart and is not replayed through Gateway; only an unsaved advisory
  may be called again.
- 模型失败不触发整任务 retry；数据库失败、lease 丢失、fencing 冲突继续走现有任务语义。
- worker heartbeat 覆盖整个 `runtime.execute_task()` 和 retry scheduling；全程只有一个 heartbeat owner。
- StarRocks 当前 query timeout 上限 25 秒、只读 Policy 上限 30 秒；RI3 不改。
- RI4, not RI3, proposes the future StarRocks-specific 180/190/195/200-second layers.
  Model-stage budgets do not consume or rewrite the query budget.

## PR 与 migration 边界

| 顺序 | PR | 单一目的 | Migration |
| ---: | --- | --- | --- |
| 1 | 3A | ADR、计划和跨文档口径收口 | 无 |
| 2 | 3B | 严格模型契约、Gemini SDK adapter、worker-only secret | 无 |
| 3 | 3C | durable 模型闭环、entry-scoped heartbeat、慢查询 advisory | `rev_0008_model_artifacts` |
| 4 | 3D | Web 显式父任务上下文 | `rev_0009_task_parent_context` |
| 5 | 3E | 固定测试环境 probe、runbook 和真实证据 | 无 |

PR 必须顺序实施。每个 PR 从当时最新 `main` 新建 `claude/<topic>` 分支；前一个未审查合入，不开始
下一个。RI5 migration 相应从 `rev_0010` 开始，不修改已合并的 0001–0007。

## 总体进入条件

- [x] 本计划、ADR-015 和 ADR-007 B2 的精简口径经项目负责人和 Claude/Codex 明确认可。
- [x] PR #30/#31 与 RI1 收口事实已从最新 `main` 重新核对。
- [x] 执行开工者按 `AGENTS.md` 顺序重新读取五份事实文档并检查 dirty worktree。
- [x] 项目负责人明确下达“开始 RI3”；这只授权离线实现，不授权真实网络调用。
- [ ] PR 3B 实现前重新核对 Google 官方模型和 SDK 文档，并审计精确 wheel。

---

## Task 1（PR 3A）：文档和授权口径收口

**Goal:** 删除过度设计，确保所有事实文档都只描述同一条 5 PR/2 migration 路线。

**Files:**

- Modify: `AGENT_HANDOFF.md`
- Modify: `ARCHITECTURE.md`
- Modify: `DEVELOPMENT_PLAN.md`
- Modify: `README.md`
- Modify: `docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md`
- Modify: `docs/adr/ADR-010-m5-durable-attempt-and-compose-boundary.md`
- Create/Modify: `docs/adr/ADR-015-real-model-provider-boundary.md`
- Modify: `docs/superpowers/specs/2026-09-10-real-integrations-design.md`
- Modify: `docs/superpowers/plans/2026-09-10-model-provider-adapter.md`
- Modify: `docs/superpowers/plans/2026-09-10-web-admin-config-center.md`
- Modify: `docs/superpowers/plans/2026-09-10-compose-deployment-canary-uat.md`

- [x] **Step 1: 用源码事实复核承重点**

核对 worker 是 `execute_task()` 的生产调用者、Runner 当前 heartbeat 范围、`handle()` 的调用位置、
TaskView 终态重建、channel submission 的两阶段现状、现有 migration 0001–0007 和 slow-query 4000 token
预算。文档不得把拟议行为写成当前事实。

- [x] **Step 2: 固定精简边界**

把所有 `Interactions API`、450 秒总 deadline、32768 output、TaskPhase、reservation/attempt、统一终态
表、通用 credential reader、runtime readback、渠道原子重写和飞书上下文从 RI3 必做项移除。明确哪些
被否决、哪些延期、为什么不影响首个真实闭环。

- [x] **Step 3: 核对后续阶段依赖**

RI5 migration 改为从 0010 开始；RI6 只保留“模型启用时叠加 override、关闭时移除并重建 worker”的
部署事实，不依赖 RI3 未实现的 worker generation/readback。不得放松 RI2、RI4、RI6 的现场 GO。

- [x] **Step 4: 文档验证**

```bash
git diff --check
rg -n "450 秒|TaskPhase|TaskAttemptSupervisor|Interactions API|32768|worker_start_id|3D1|3D2|3F1|3F2" \
  AGENT_HANDOFF.md ARCHITECTURE.md DEVELOPMENT_PLAN.md README.md docs
git diff --stat
git diff -- AGENT_HANDOFF.md ARCHITECTURE.md DEVELOPMENT_PLAN.md README.md docs
```

命中的历史/否决说明必须逐条人工判断；不能为了让 `rg` 空输出而删掉必要的决策依据。

**PR 3A exit:** 只有文档变化；跨文档无冲突；明确“离线实现已授权，但尚无源码、真实调用或部署”。

---

## Task 2（PR 3B）：模型契约、Gemini adapter 与 Compose secret

**Goal:** 完成完全离线可测的 SDK 边界；默认不联网、不需要 key。

**Primary files:**

- Create: `src/xiaowei_agent/contracts/model.py`
- Create: `src/xiaowei_agent/application/model_ports.py`
- Create: `src/xiaowei_agent/interfaces/gemini_model.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `src/xiaowei_agent/interfaces/__init__.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.env.example` (application `XIAOWEI_*` settings only; never host-only
  `GEMINI_API_KEY` or `GEMINI_API_KEY_FILE`)
- Inspect, expected no direct change: `.dockerignore` (already excludes `.secrets` and
  `.env`/`.env.*`)
- Inspect, expected no direct change: `.gitignore` (already excludes `.secrets` and
  `.env`/`.env.*`)
- Inspect, expected no direct change: `docker-compose.yml`
- Create: `docker-compose.model.yml`
- Modify: `scripts/compose_smoke.py`
- Create: `tests/fakes/model.py`
- Create: `tests/unit/test_model_contracts.py`
- Create: `tests/unit/test_gemini_config.py`
- Create: `tests/contract/test_gemini_sdk_seam.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `tests/security/test_dependency_baseline.py`
- Create: `tests/security/test_gemini_sdk_boundary.py`
- Create: `tests/security/test_gemini_credential_boundary.py`
- Modify: `tests/security/test_no_network.py`
- Modify: `tests/security/test_module_layering.py`
- Modify: `tests/security/test_config_fail_fast.py`
- Modify: `tests/unit/test_config_happy.py`
- Modify: `tests/unit/test_local_stack.py`

- [ ] **Step 1: RED 严格 DTO 和权限反例**

先写失败测试，冻结两个请求/响应 schema。DTO 必须不可变、`extra="forbid"`、严格类型、有长度/行数
上限。反例覆盖未知字段、`bool` 冒充数字、NaN/Inf、超长文本、21 行证据，以及模型试图返回
capability、target、SQL、approval、tool 或 `next_steps`。

端口的内容参数只接受已经净化的 typed request，不能接受 `RequestEnvelope`、Evidence、任意 `dict` 或
通用 prompt。`SlowQueryAdvisoryPort` 另有一个必填 keyword-only
`max_output_tokens: StrictInt`，范围为 1–4,000；`bool`、0、负数和 4,001 必须在 adapter 调用前被
拒绝。该值只承接 application 计算出的 plan budget，不进入 prompt，也不能来自用户、模型、环境变量
或 Web 配置；intent port 不开放此参数，固定使用 2,048。

PR 3B may add model call/fallback enums needed by the DTOs, but it does not add
`PipelineStage.MODEL` yet. That closed trace-stage change lands atomically in PR 3C with
`TraceEvent`, exact-set tests and emitters, so PR 3B cannot leave the existing nine-stage
contract half-migrated.

The provider-response schema excludes `source`; the adapter stamps accepted output as
`IntentSource.MODEL`. A response containing a source field is therefore an extra-field
failure. If any returned text changes under shared redaction, reject the whole response
and use deterministic fallback instead of persisting or resolving it.

The two ports return concrete `IntentModelResult` / `AdvisoryModelResult` wrappers. Each
contains the accepted DTO plus trusted `ModelUsage`; the latter maps only nullable SDK
`prompt_token_count` / `candidates_token_count` to input/output tokens. Missing metadata
maps to two nulls. When metadata appears, each field remains independently optional:
omitted or explicit-null values map to the corresponding local null, while present
non-null values must pass local non-negative signed-64-bit bounds. The locked SDK
normalizes coercible integer-like raw values—bool, integral floats and numeric strings such
as `"1"` / `"1.0"`—to `int` before the adapter; tests characterize that fact rather than
claiming local raw-type rejection. Negative or overflow metadata
rejects the response. Usage is not part of the provider response schema and is never
carried through `last_usage`, a tuple, global state or callback.

- [ ] **Step 2: RED SDK seam，不发真实网络**

用 fake SDK client 验证：

- 调用 `client.aio.models.generate_content()`；
- client 显式使用从固定 secret 文件读取的 `api_key` 和 Developer API 模式，不从 SDK 环境变量自动
  选择 Vertex AI、凭证或 endpoint；
- the adapter fixes Developer API version `v1beta` and canonical provider origin
  `https://generativelanguage.googleapis.com`, accepts no
  caller-supplied base URL or proxy, and fails closed if ambient proxy configuration could
  redirect the SDK transport;
- 固定 model；
- `response_mime_type="application/json"` 且 response schema 对应本地 DTO；
- intent slots 使用 allowlist 并集的固定字段 DTO，使锁定 SDK Developer API schema transformer 不生成
  unsupported dynamic `additionalProperties`；provider schema 不宣告 null，显式 null 由本地
  `model_fields_set` 整体拒绝，真正省略仍合法；两种 response schema 都经真实 outer async path +
  `httpx.MockTransport` 离线通过，且每次恰好一个 transport request；
- intent 使用 low/2048；advisory 使用 high、固定 profile ceiling 4000，plan-bound application
  可以按当前 plan budget 请求更少，但绝不能请求更多；
- SDK 自身不额外重试；
- 显式 `AutomaticFunctionCallingConfig(disable=True)`，不产生 AFC warning；
- async client 正确关闭；
- 重复取消也只能延后传播到 tracked close task 已完成或被 close deadline 取消并收口之后；不得遗留
  detached close task；close 自身的 `CancelledError` 不能遮蔽既有 provider/schema 错误，也不能在
  provider 成功后伪装成外层取消；
- SDK/API 错误只映射为闭集本地错误码，不暴露原始正文。

实现前阅读精确 wheel 的 client、timeout、retry、structured response、usage 和 close 代码；结果写入 PR
证据。若 2.23.0 与官方文档不一致，先更新 ADR/计划并复审，不能现场换 API。

The dependency baseline must add exactly `google-genai==2.23.0`, lock the reviewed
`google_genai-2.23.0-py3-none-any.whl` SHA-256
`1e63211d44d188b8069c2b354d92b9bde25c1e821513fdbe1948b7c0d9f6b922`, verify
the Apache-2.0 metadata and the wheel's `google/genai/py.typed` marker, and keep
global mypy strictness unchanged. Any artifact mismatch stops the PR for re-review.

- [ ] **Step 3: 最小 adapter**

adapter 只做 SDK DTO 转换和错误归一，不包含 Resolver、Planner、fallback、数据库或渠道逻辑。
The composition root injects one immutable, fixed `ModelInvocationProfile` into the
adapter and retains that same typed value for PR 3C's application service/artifact path;
application code must not import interface constants or inspect the concrete adapter.
`asyncio.timeout()` 在 application service 控制 60/180 秒 provider 总预算，adapter 不偷偷另建更长
timeout。预算从读取固定 secret、构造 client 之前开始，覆盖请求、允许的 backoff、本地响应复验与
client close；每次 SDK timeout 只能使用当时剩余预算。Artifact load/save 在该 timeout 外按现有
持久化错误语义处理，不能被误降级成模型 fallback。
显式 model override 但 secret 缺失/无效时不创建 client、不联网，并产生安全的 model-unavailable
状态；任务继续走规则解释器。运行中的 401/403 同样降级，但必须可观察，不能伪装成模型成功。
Provider timeout 走上述 fallback；外层 `CancelledError`/SIGTERM 在 bounded client close 后继续传播，
不得保存 accepted intent/advisory、继续 Resolver/Planner 或伪造终态。

The Settings surface adds exactly one application field,
`gemini_enabled: bool = False`, mapped to `XIAOWEI_GEMINI_ENABLED`. Provider, model,
API version `v1beta`, canonical origin `https://generativelanguage.googleapis.com`, thinking
levels, timeouts, output limits and
`/run/secrets/gemini_api_key` remain versioned code constants/profile values, not
environment-selectable settings. Config tests assert the exact new field/env set and
reject invented `XIAOWEI_GEMINI_MODEL`, endpoint, proxy, timeout or secret-path inputs.

- [ ] **Step 4: worker-only key**

复用 `interfaces.secret_file.read_secret_file()`。只允许固定
`/run/secrets/gemini_api_key`；不接受用户路径、Web 上传或配置中的明文 key。

The base `docker-compose.yml` remains model-disabled and has no Gemini secret mount.
Only `docker-compose.model.yml` sets `XIAOWEI_GEMINI_ENABLED=true` and introduces the secret.
The model override defines a top-level file-backed `gemini_api_key` and appends that
secret only to `worker`.
The previously planned environment-backed secret is rejected by runtime evidence:
Compose can render it but cannot materialize it for the existing read-only worker.
Do not fix that incompatibility by removing `read_only: true` or exposing the key as a
container environment variable.
The merged worker secret list must contain both `postgres_password` and
`gemini_api_key`; every other service must retain its current list.
The enable flag is declared only under `services.worker.environment`; it must not be
added to the shared `x-app-environment` anchor or any non-worker service. Rendered-config
tests assert that non-worker services have neither the flag nor the Gemini secret.

The only plaintext source is the fixed repository-relative path
`.secrets/gemini_api_key`. There is no host path environment-variable override, so
rendered Compose evidence cannot drift with ambient shell state. Neither
`GEMINI_API_KEY` nor `GEMINI_API_KEY_FILE` is an application setting, container
environment value or `.env.example` entry; `.env.example` must remain exactly equal to
`_FIELD_TO_ENV`. Setup is
documented only in README/runbook. Require Docker Compose
2.24.4 or newer (the project support floor shared with RI6's `!override` deployment
path), but treat the version floor as necessary and insufficient. Extend the
existing `scripts/compose_smoke.py` workflow instead of creating a second probe service:
with a split fake file secret and the model override, inspect the actual Linux
containers to prove the fixed worker mount exists and every non-worker container lacks
it, without opening or printing the file. `docker stack deploy` is unsupported. A
separate rendered-config test proves the declared secret union is correct and the fake
value is absent from service environments and rendered output. Neither proof substitutes
for the other.

- [ ] **Step 5: 依赖与边界审计**

检查 `google-genai` 直接/传递依赖、许可证、import 副作用和网络入口。运行时依赖集合等式增加
`google-genai`，并把已经解析存在、生产错误分类直接 import 的 `httpx` 从 dev 提升为 runtime；不新增
resolved package。锁文件对精确 SDK wheel/version/hash 的断言与项目依赖集合
等式同时承重。`tests/security/test_no_network.py`
必须证明 import、配置加载和默认 LocalStack 构造均为 0 网络。

`tests/security/test_gemini_sdk_boundary.py` must AST-scan all production imports
and dynamic import strings, allowing `google.genai` only in
`interfaces/gemini_model.py`. It also proves importing contracts, config, LocalStack,
runtime and worker does not load the SDK. The two model ports and every real/fake
implementation are assigned in `_conformance.py`; the protocol-conformance registry
and keyword-only signature checks must cover both ports.

`tests/security/test_gemini_credential_boundary.py` proves the host key names are never
Settings fields or container environment values, only worker
receives the fixed secret file, missing/invalid credential causes zero client/network
calls, and key-shaped values never enter errors, logs, traces or persisted model
artifacts. It also freezes the existing `.gitignore`/`.dockerignore` exclusion of
`.secrets`, `.env` and `.env.*`.

- [ ] **Step 6: focused 验证**

```bash
python -m pytest tests/unit/test_model_contracts.py tests/unit/test_gemini_config.py \
  tests/unit/test_config_happy.py tests/unit/test_local_stack.py \
  tests/contract/test_gemini_sdk_seam.py tests/contract/test_compose_contract.py \
  tests/contract/test_compose_smoke_script.py \
  tests/contract/test_protocol_conformance.py \
  tests/security/test_dependency_baseline.py tests/security/test_gemini_sdk_boundary.py \
  tests/security/test_gemini_credential_boundary.py \
  tests/security/test_no_network.py tests/security/test_module_layering.py \
  tests/security/test_config_fail_fast.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

**TDD 反证:** 临时允许额外字段、把 key mount 给 Web、启用 SDK 重试、改成任意 endpoint，相关测试必须
转红。恢复工作树后再跑 focused tests。
Also move one SDK import into `application/`, drop one typed conformance assignment,
remove `py.typed`, or remove either worker secret; each matching guard must fail.

**PR 3B exit:** 官方 SDK adapter 可由 fake 完整驱动；默认 Compose 0 key/0 网络；model override 只让
worker 取得固定 secret。还没有 Runtime 调用模型，更没有真实网络证据。

---

## Task 3（PR 3C）：durable 模型闭环与慢查询 advisory

**Goal:** 在 durable worker 主路径接入意图和慢查询分析，崩溃可恢复，provider 失败可降级。

**Primary files:**

- Create: `src/xiaowei_agent/application/model_intent.py`
- Create: `src/xiaowei_agent/application/model_advisory.py`
- Create: `src/xiaowei_agent/persistence/model_artifacts.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0008_model_artifacts.py`
- Create: `src/xiaowei_agent/rendering/model_advisory.py`
- Create: `src/xiaowei_agent/application/task_heartbeat.py`
- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/trace_events.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `src/xiaowei_agent/application/runtime.py`
- Modify: `src/xiaowei_agent/application/worker.py`
- Modify: `src/xiaowei_agent/application/capability_runtime.py`
- Modify: `src/xiaowei_agent/application/default_capabilities.py`
- Modify: `src/xiaowei_agent/application/task_view_runtime.py`
- Modify: `src/xiaowei_agent/runners/deterministic.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`
- Modify: `src/xiaowei_agent/persistence/store.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Inspect unchanged: `src/xiaowei_agent/rendering/slow_query.py`（既有最多三段的确定性投影无需改限额）
- Create: `tests/suites/model_artifacts.py`
- Create: `tests/unit/test_model_intent_service.py`
- Create: `tests/unit/test_slow_query_model_projection.py`
- Create: `tests/unit/test_model_advisory_render.py`
- Modify: `tests/unit/test_feishu_rendering.py`
- Create: `tests/unit/test_task_heartbeat.py`
- Create: `tests/contract/test_model_artifact_store.py`
- Create: `tests/integration/test_model_artifact_store_postgres.py`
- Modify: `tests/contract/test_suite_bindings.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/contract/test_row_mapping.py`
- Modify: `tests/contract/test_deterministic_runner.py`
- Modify: `tests/contract/test_trace_stages.py`
- Create: `tests/contract/test_runtime_model_intent.py`
- Create: `tests/contract/test_runtime_model_advisory.py`
- Modify: `tests/contract/test_worker_loop.py`
- Modify: `tests/contract/test_runtime_async_lifecycle.py`
- Modify: `tests/contract/test_task_view_runtime.py`
- Modify: `tests/contract/test_channel_render_parity.py`
- Modify: `tests/unit/test_local_stack.py`
- Modify: `tests/integration/test_migration_paths.py`
- Run unchanged: `tests/contract/test_schema_matches_migration.py`
- Create: `tests/evals/corpus/ri3_intent.json`
- Create: `tests/evals/corpus/ri3_slow_query_advisory.json`
- Create: `tests/evals/test_ri3_model_safety.py`
- Create: `tests/security/test_model_context_boundary.py`
- Modify: `tests/security/test_trace_event_redaction.py`
- Modify: `tests/security/test_temporal_and_trace_hygiene.py`
- Run unchanged: `tests/security/test_runtime_bypass.py`
- Run unchanged: `tests/security/test_intent_pollution.py`
- Run unchanged: `tests/security/test_reflection_has_no_authority.py`
- Modify: `tests/security/test_web_xss.py`
- Run unchanged: `tests/security/test_module_layering.py`

- [ ] **Step 1: RED migration 与 store 契约**

`rev_0008` 只创建 `task_accepted_intents` 和 `task_model_advisories`。两表以 `task_id` 唯一，保存 version、
严格 JSON、origin、provider/model（可空）、input digest、result digest、端口 wrapper 返回的安全
usage、created_at 和写入
时 fencing token；禁止 prompt、原始 response、key、path、provider error 正文或完整 Evidence。

The model-origin intent input digest covers scrubbed current text, scrubbed explicit-parent
context, truncation flags and fixed profile/schema revisions. A rule-origin intent instead
binds only that scrubbed typed input plus `origin=rule`; it carries no provider identity and
must survive a Gemini prompt/schema revision change. The advisory input digest covers
the typed projection, sampled flag, capability version,
`SLOW_QUERY_SURFACE`/Evidence-contract revision and fixed profile/schema revisions.
Neither digest includes a `RenderPayload`, display prose, a second deterministic
summary, keys, raw provider text, timestamps or random IDs. Reads rebuild and compare
the same typed input before reusing a stored artifact.

store 的 save 必须要求当前 grant，数据库同事务验证 owner、lease token、fencing 和非终态。insert-once
winner 可返回已存在的同 input/result digest 事实；任一 digest 不同都 fail-closed。测试覆盖内存/PostgreSQL parity、
迟到 worker、重复写、崩溃点、upgrade/downgrade 和 schema/migration 一致性。

`tests/suites/model_artifacts.py` is registered in
`tests/contract/test_suite_bindings.py` with both memory and PostgreSQL bindings.
The artifact-store Protocol and both implementations are also anchored in
`_conformance.py` and `test_protocol_conformance.py`; adding a suite or implementation
without either registry must fail.

- [ ] **Step 2: RED entry-scoped heartbeat**

从 Runner 提取一个无业务判断的 `run_with_task_heartbeat()`。Worker 在取得 grant 后用它包住一次
`runtime.execute_task()` 以及随后 retry scheduling；兼容 `XiaoweiRuntime.handle()` 用同一 helper 包住
自己的 attempt。每条入口恰好启动一份，Runner 不再启动第二份，但每次持久写仍验证 grant/fencing。

测试分别把阻塞点放在意图模型、Resolver/Planner、StarRocks fake、advisory、retry scheduling 和
`handle()`，证明 lease 持续续租且每个 attempt 只有一份 heartbeat；取消、heartbeat 失败和 lease 被
抢走时 operation 被取消且旧 worker 无法落库。

The move is complete, not additive: `DeterministicStepRunner` drops
`heartbeat_interval_seconds`, the heartbeat-only `sleep` injection/state, and exactly
the `_heartbeat()` and `_run_with_heartbeat()` methods. It retains
`DEFAULT_LEASE_TTL_SECONDS`, the `lease_ttl_seconds` constructor argument/state and
`_require_current_grant()`: `start()` and `resume()` still call that method once to renew
and validate their grant before execution. That one-shot renewal is not a second periodic
heartbeat owner. Worker/runtime pass the existing lease TTL, interval and sleep to the
application helper. `test_deterministic_runner.py` loses only periodic-heartbeat ownership
assertions and explicitly retains start/resume grant-renewal, fencing and committed-step
recovery assertions; application/worker tests become the sole periodic-heartbeat-owner proof.
Add separate start and resume counterexamples: replacing `_require_current_grant()` with
a no-op must make the immediate renewal/expired-grant tests fail before plan, Gateway or
artifact work. Periodic heartbeat-loss cancellation moves to `test_task_heartbeat.py`;
it must not be kept alive by a second Runner loop.

不要新增 450 秒 deadline、TaskPhase 或 Supervisor 框架。现有 StarRocks timeout 测试必须原样通过。
The RI3 regression pins the current 25-second StarRocks query-timeout upper bound and 30-second
read-only policy maximum; future RI4 values must not appear in RI3 source changes.

- [ ] **Step 3: RED accepted intent**

`execute_task()`, after a valid grant has been issued, first calls a durable-only
load-or-accept helper. The provider/artifact branch must not be placed in the existing
shared `_interpret()` method, because `handle()` invokes that method before task creation
and grant acquisition.

1. 模型关闭/不可用，直接运行规则解释器；
2. 模型开启，typed builder 先做原始字符边界与 UTF-8 有效性检查，再执行 `scrub_text()`，在共享 60 秒内
   最多两次 SDK request；
3. 模型输出严格复验，失败走规则解释器；
4. 将最终 model/rule draft insert-once 保存；
5. 只把已保存 draft 交给现有 Resolver。

Tests cover model success, 429-then-success, timeout, 401, 5xx, malformed JSON,
authority-bearing fields, source spoofing, redaction-changing output, size rejection
and outer cancellation. Every provider/schema failure branch still passes through
Resolver/Planner/Gateway after deterministic fallback; outer cancellation instead
propagates with zero post-cancel artifact/downstream calls. Model failures do not schedule task retry, and a saved
draft yields zero provider calls on recovery. `handle()` must produce zero model and
artifact-store calls and preserve its current pre-create deterministic rejection order.

Before `scrub_text()`, typed builders enforce concrete work limits and UTF-8 validity:
the existing 8,192-character limit mathematically implies at most 32 KiB UTF-8 for the
current text and each history field; selected history is at most 20 complete tasks, 64,000
characters, which implies at most 256,000 UTF-8 bytes (less than 256 KiB). Oversized rounds are omitted whole and marked truncated,
never sliced through a possible secret. The final serialized model request is capped at
512 KiB only after every retained string has been scrubbed and the complete typed request
has been serialized again. The builder drops oldest complete rounds until that scrubbed
serialization fits; if the current request alone cannot fit, it rejects with zero provider
calls. `scrub_text()` is total for typed strings; there is no invented "redaction failure"
branch. The per-field byte ceiling is a derived guarantee, not a duplicate unreachable
validator; the history byte ceiling is derived for the same reason, while the 512 KiB
serialized-request guard remains independently reachable. Boundary tests cover
the exact character-derived byte ceiling, one-character excess and pathological
Unicode before proving provider calls stay zero.

- [ ] **Step 4: RED surface-derived 20-row projector**

`CapabilityRuntimeBinding` gains an optional `advisory_projector`; only
`starrocks.slow_query.diagnose` sets it. Runtime must not branch on capability ID or
bump the capability version in RI3. The projector validates task/capability/step/evidence
ownership, takes the first 20 rows in deterministic order, and uses the exact
surface-derived column set.

The projector is eligible only when deterministic execution succeeded and
Answerability is sufficient. Failed, indeterminate, rejected or insufficient outcomes
produce no advisory request even if some rows exist; the deterministic terminal answer
alone explains those cases.

The projector derives its exact column names and order from
`SLOW_QUERY_SURFACE.allowed_columns`; it must not own a copied 13-name tuple. A pairing
test asserts exact equality. Field type rules are keyed against that derived closed set,
and the input digest carries the surface/evidence schema revision. Any RI4 live
`SHOW CREATE TABLE` correction must update the surface, projector schema, digest
revision and tests together.

Both text and numeric partitions are explicit literals. Their disjoint union must equal
the derived surface exactly, so adding a field cannot silently classify it as numeric by
subtraction; changing only the surface must make the pairing test fail.

The typed projector is implemented in `application/model_advisory.py`, where the existing
module-layer contract permits access to `capabilities` and Evidence contracts.
`rendering/model_advisory.py` and `rendering/slow_query.py` only build display sections;
they must not import `xiaowei_agent.capabilities` or read `SLOW_QUERY_SURFACE`. Keep the
existing `rendering` package allowlist unchanged, and let its module-layer test fail if
this ownership boundary is crossed.

反例覆盖缺列、额外列、第二至二十行才出现坏类型、bool、NaN/Inf、date/datetime、指数/带空白数字、
错误 source，以及 `stmt`/SQL/clientIp/digest 泄漏。任一失败都返回 None 且模型调用为 0；0 行同样不调。
Text values have explicit character limits and UTF-8 validity checks; their byte ceiling
is derived from UTF-8's four-byte maximum. The raw typed advisory request has independent
aggregate limits before scrubbing; an oversized batch is rejected as a whole before a
provider call. After scrubbing, the complete typed request is serialized again and must
remain within its final byte cap; otherwise it is rejected with zero provider calls.

- [ ] **Step 5: RED advisory 与稳定终态展示**

After deterministic Evidence and Answerability, the typed projector runs and the worker
requests one advisory within 180 seconds. It does not render first. A legal result is
saved in `task_model_advisories` before the existing TaskStore terminal transition.
TaskView and the compatibility `handle()` render once from terminal facts plus the saved
advisory; a non-terminal task never displays it. Recovery reuses an existing matching
advisory.

The advisory service receives the prepared plan for the current attempt and sets the SDK
request's `max_output_tokens` to
`min(ADVISORY_OUTPUT_TOKEN_LIMIT, prepared.plan.budget.max_model_tokens)`. It must not read
a compiler default, capability Registry snapshot or a caller-supplied override instead.
`tests/contract/test_runtime_model_advisory.py` covers unequal limits in both directions:
a plan budget below 4,000 lowers the provider request, while a budget above 4,000 leaves
the fixed ceiling in force. In every case the requested value is positive and no greater
than the current plan's `max_model_tokens`.

Rendering includes that advisory only when the terminal status is `SUCCEEDED`, the
deterministic verdict is still sufficient and the rebuilt typed input digest matches the
stored artifact. A saved pre-terminal advisory is hidden if recovery later closes the task
as failed/rejected/indeterminate or detects input drift.

无 advisory 时，Web/飞书 payload 与当前版本逐字段相同。有 advisory 时只增加本地固定标题的文本段，
保持 status、原 answer、facts、limitations、refs 和 `next_steps` 不变。HTML、Markdown URL、SQL 或
shell 形状只能作为 escaped/plain text，不能变成链接、按钮或动作。
The slow-query renderer currently produces at most three deterministic sections and the
Feishu renderer admits four. Tests pin advisory as the fourth section, its existing
500-character card clipping and “查看完整结果” path, while Web retains the full locally
bounded text. No production Feishu renderer limit is widened for RI3.

- [ ] **Step 6: RED 故障窗口和降级**

故障注入至少覆盖：

- provider 返回后、artifact save 前崩溃：恢复可再次调用；
- artifact save 后、Resolver 或 terminal transition 前崩溃：恢复 0 模型调用；
- advisory save 失败：按数据库故障重试，不能伪造已展示；
- lease 丢失后的迟到模型响应：不能保存；
- provider timeout/错误：确定性任务正常收口，fallback 有安全 trace；
- local timeout/cancel 后任何迟到 SDK result 都被丢弃且不得落库；这不声称 provider 远端已停止处理；
- outer cancellation 继续传播，不能写 fallback artifact、进入 Resolver/Planner 或提交终态；
- TaskView 重复读取：0 模型调用，展示一致。
- advisory 已保存但恢复后进入非成功终态：不展示旧 advisory；0 新模型调用。

调用日志只记录 task/trace、call kind、闭集 outcome、latency、usage 和 fallback code。用拆分构造的
secret-shaped input 证明 prompt、response、key 和 provider 原始错误不进入日志/trace/数据库。

Extend the closed trace contract, not free-form `detail`: add
`PipelineStage.MODEL` and an optional typed `ModelCallObservation` containing call
kind, total elapsed milliseconds, request count, nullable input/output usage and a
closed fallback code. It is present iff stage is MODEL; model `detail` stays empty and
never contains prompt/response/error text. Update the exact stage-set, redaction and
temporal-hygiene tests. This remains one aggregate observation per model stage, not a
transport-attempt ledger.

All numeric observation fields use strict integers so `bool`, floats, negatives and
values above signed 64-bit range are rejected. Elapsed milliseconds and usage are
non-negative; request count is 0–2, with a cross-field rule limiting advisory to 0–1.
Nullable usage means “provider did not report it”, not zero. Boundary tests cover every
endpoint and MODEL/non-MODEL observation mismatches.

Trace-order tests keep the existing `INTENT` meaning: `MODEL` records provider/response
validation, while `INTENT` records the final accepted model-or-rule draft. A provider
failure followed by successful fallback therefore emits failed MODEL then successful
INTENT, not two root-cause failures. Advisory MODEL occurs only after a sufficient
REFLECTION decision and before terminal LIFECYCLE; a provider failure leaves the
deterministic terminal path successful.

Recovery tests also block advisory to prove the task remains RUNNING, crash after a
committed StarRocks step to prove Gateway call count does not increase, crash before and
after advisory save to distinguish permitted advisory replay from reuse, and timeout to
prove deterministic terminal fallback.

- [ ] **Step 7: 小型离线 eval**

固定 corpus 包含 10 个代表性意图、5 个慢查询 advisory 和恶意输入矩阵。安全边界必须 100%：模型不能
扩大 CandidateSet、target、SQL、tool、policy、状态或动作。质量结果分场景报告，不用平均分掩盖失败。

- [ ] **Step 8: focused 验证与承重反证**

```bash
python -m pytest tests/unit/test_model_intent_service.py tests/unit/test_task_heartbeat.py \
  tests/unit/test_slow_query_model_projection.py tests/unit/test_model_advisory_render.py \
  tests/unit/test_feishu_rendering.py \
  tests/contract/test_model_artifact_store.py tests/integration/test_model_artifact_store_postgres.py \
  tests/contract/test_suite_bindings.py tests/contract/test_protocol_conformance.py \
  tests/contract/test_row_mapping.py tests/contract/test_deterministic_runner.py \
  tests/contract/test_trace_stages.py tests/unit/test_local_stack.py \
  tests/contract/test_runtime_model_intent.py tests/contract/test_runtime_model_advisory.py \
  tests/contract/test_worker_loop.py tests/contract/test_runtime_async_lifecycle.py \
  tests/contract/test_task_view_runtime.py \
  tests/contract/test_channel_render_parity.py tests/contract/test_schema_matches_migration.py \
  tests/integration/test_migration_paths.py \
  tests/evals/test_ri3_model_safety.py tests/security/test_model_context_boundary.py \
  tests/security/test_trace_event_redaction.py tests/security/test_temporal_and_trace_hygiene.py \
  tests/security/test_runtime_bypass.py tests/security/test_intent_pollution.py \
  tests/security/test_reflection_has_no_authority.py tests/security/test_web_xss.py \
  tests/security/test_module_layering.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

临时移除 fencing、让模型输出直接进 Planner、发送完整 Evidence、让 TaskView 现调模型、让模型写
`next_steps`、删除 start/resume 的 `_require_current_grant()`，或把 surface projector/import 移入
`rendering/`，或绕过当前 plan budget 直接把 4,000 传给 provider，对应测试必须转红。每次变异使用
隔离 pycache 并确认加载的是变异代码。

**PR 3C exit:** durable worker 在 fake provider 下完成 model/rule intent → 确定性执行 → 可选 advisory →
终态 → Web/飞书重复读取；崩溃/重试语义有证据；默认配置仍 0 真实网络。

---

## Task 4（PR 3D）：Web 显式父任务上下文

**Goal:** 用户明确继续某个终态任务时带上安全历史，不靠“最近消息”猜上下文。

**Primary files:**

- Create: `src/xiaowei_agent/application/context.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0009_task_parent_context.py`
- Modify: `src/xiaowei_agent/contracts/task.py`
- Modify: `src/xiaowei_agent/contracts/channel.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`
- Modify: `src/xiaowei_agent/persistence/store.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Inspect, expected no direct change: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/persistence/channel.py`
- Modify: `src/xiaowei_agent/application/channel_submission.py`
- Modify: `src/xiaowei_agent/application/channel_access.py`
- Modify: `src/xiaowei_agent/application/runtime.py`
- Modify: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `src/xiaowei_agent/interfaces/http_models.py`
- Create: `tests/unit/test_context_assembler.py`
- Modify: `tests/unit/test_hash_vectors.py`
- Modify: `tests/contract/test_task_submission.py`
- Modify: `tests/contract/test_row_mapping.py`
- Modify: `tests/contract/test_task_store_contract.py`
- Run unchanged: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/contract/test_channel_store.py`
- Modify: `tests/contract/test_channel_submission.py`
- Modify: `tests/contract/test_channel_access.py`
- Modify: `tests/integration/test_task_store_contract_postgres.py`
- Modify: `tests/integration/test_channel_store_postgres.py`
- Modify: `tests/integration/test_migration_paths.py`
- Run unchanged: `tests/contract/test_schema_matches_migration.py`
- Create: `tests/security/test_model_parent_context.py`
- Modify: `tests/security/test_channel_access_control.py`

- [ ] **Step 1: RED parent contract 与 migration**

`TaskSubmission.parent_task_id` is `StrictStr | None`, matching the existing task-id
contract. `rev_0009` adds a nullable `Text` restricted FK to `tasks.task_id` plus
an index; existing rows remain null. It must not introduce PostgreSQL UUID conversion,
because current task IDs map to `Text`. For a null parent,
the canonical bytes and frozen vectors for `request_dedup_digest`,
`submission_digest` and `idempotency_scope_digest` remain byte-for-byte unchanged.

For a non-null parent, add `parent_task_id` to the semantic
`request_dedup_digest` and to the full stored `submission_digest`, but never to
`idempotency_scope_digest`. Both memory and PostgreSQL `create_task()` compare the
semantic request digest on a scope collision. The same semantic request with the same
parent reuses the task; any semantic difference, including a different parent, conflicts.
Changes limited to request_id, trace_id or `as_of` still count as a legitimate retry.
The full submission digest remains a stored-row
consistency checksum and is not used as the idempotency conflict predicate.

Make parent handling explicit by adding a required keyword-only
`parent_task_id: str | None` input to `request_dedup_digest(envelope, context)`; do not
give it a default that lets a new call site silently omit parent semantics. Existing
null-parent digest bytes remain compatible, while existing call sites are mechanically
updated to pass `None` or the persisted value.
Signature tests prove positional use and omission both fail, while explicit `None`
reproduces the frozen pre-RI3 vector.
Both `create_task()` implementations and the shared `submission_matches_record()`
readback verifier must pass `submission.parent_task_id`; omitting any one of those three
production recomputation sites is a required failing counterexample. `submission_digest()`
continues to consume the full submission, while `idempotency_scope_digest()` keeps its
current three-key canonical payload unchanged. Canonical payload builders emit the
`parent_task_id` key only when it is non-null; serializing an explicit null would drift
all existing frozen vectors.

The memory/PostgreSQL backend suites must create, retrieve, attempt and idempotently
resubmit a non-null-parent task to prove the row survives every integrity readback. They
also cover same-scope semantic mismatch, including changed text with an unchanged parent;
the parent test must not accidentally redefine every same-parent request as equivalent.

`persistence/memory.py` currently stores the complete immutable `TaskSubmission`
object, so PR 3D should not add a second parent map there. The file is an explicit
inspection point: if implementation introduces separate serialization, the plan must be
re-reviewed and the backend parity suite extended instead of silently changing shape.

- [ ] **Step 2: RED Web 显式归属检查**

Web 请求只接受用户明确提供的 `parent_task_id`。提交前通过窄 store 方法读取 parent task/submission/
binding，验证终态、actor、tenant、environment、channel 和 Web binding owner 均相同。不存在、越权、
非终态、跨环境、跨 owner 或循环都 4xx/fail-closed；重新登录不因 session ID 改变而丢失上下文，也
不能改成“忽略 parent 继续提交”。

本 PR 只增加必要的 scoped lookup/validation，不把现有两阶段 channel submission 重写成聚合事务。

- [ ] **Step 3: RED ContextAssembler**

Worker revalidates the persisted parent chain and never trusts the Web pre-check.
It loads at most 20 parents and only persisted user text plus the existing terminal
safe projection; SQL, full Evidence, identity details and configuration are excluded.
It applies the same per-field character limit and UTF-8 validity check before
`scrub_text()`; the 32 KiB field ceiling remains derived from the character limit. It then
selects complete newest-to-oldest rounds within 64,000 characters (therefore no more than
256,000 UTF-8 bytes, less than 256 KiB) and
reverses them for model order. It never cuts a round or an individual string.

超过容量只确定性截断并标记 `truncated`；越权、循环、损坏或非终态则拒绝，不退回最近消息。model
disabled 时 parent 仍可审计，但规则解释器不需要消费历史。

- [ ] **Step 4: 最小 Web 体验**

任务详情提供“继续这个任务”，新提交明确显示来源 task。没有 parent 的现有提交和页面完全不变。
首版不改飞书 listener、事件 schema 或 reply/thread 行为。

- [ ] **Step 5: focused 验证与反证**

```bash
python -m pytest tests/unit/test_context_assembler.py tests/unit/test_hash_vectors.py \
  tests/contract/test_task_submission.py tests/contract/test_row_mapping.py \
  tests/contract/test_task_store_contract.py tests/contract/test_protocol_conformance.py \
  tests/contract/test_channel_store.py \
  tests/contract/test_channel_submission.py tests/contract/test_channel_access.py \
  tests/integration/test_task_store_contract_postgres.py \
  tests/integration/test_channel_store_postgres.py tests/contract/test_schema_matches_migration.py \
  tests/integration/test_migration_paths.py \
  tests/security/test_model_parent_context.py tests/security/test_channel_access_control.py -q
python -m pytest -m security -q
ruff check .
mypy src
```

临时改成“同会话最新任务”、只做入口检查、不校验 actor/environment、让 parent=null 也进入旧 digest，
相关测试必须转红。

**PR 3D exit:** Web 可明确继续自己的同环境终态任务；上下文有界、脱敏、可审计；旧任务 hash 不变；
飞书上下文仍明确未实现。

---

## Task 5（PR 3E）：测试环境真实 Gemini 验证

**Goal:** 用固定 synthetic 数据证明真实 SDK、key、timeout、usage、fallback 和上下文按设计工作。

**Primary files:**

- Create: `scripts/ri3_gemini_probe.py`
- Create: `tests/evals/corpus/ri3_live_intent.json`
- Create: `tests/evals/corpus/ri3_live_advisory.json`
- Create: `docs/runbooks/ri3-gemini-test-env.md`
- Modify: `AGENT_HANDOFF.md`
- Modify: `README.md`

脚本只提交仓库内固定 case ID，不接收任意 prompt、不读取 key、不直接调用 SDK；真正网络调用仍由正常
worker path 发起。脚本只输出 task ID、安全 outcome、latency/usage 汇总和 digest。

### 现场进入条件

- [ ] PR 3A–3D 已逐个审查并合入，最新 main 四项基线命令全绿。
- [ ] 公司负责人确认该 Gemini 项目的数据保留、训练使用、区域和日志政策允许 D3/D6 字段。
- [ ] 现场 owner 已在供应商账户层启用 usage/费用告警，并确认通知渠道、触发规则和本次 readback
  时间；runbook 只记录非敏感状态，缺少 readback 时现场 GO 保持关闭。
- [ ] 测试环境 `.env` 由操作者创建，未进入 Git、命令参数、shell history、日志或证据。
- [ ] Compose 证明 key 只挂给 worker，其他容器反证为 absent。
- [ ] 测试 corpus 全是 synthetic，不含真实 SQL、用户文本、host、账号或生产数据。
- [ ] 项目负责人明确下达一次性的“RI3 Gemini test-env GO”。

- [ ] **Step 1: 默认关闭反证**

只启动 base Compose，运行固定意图和慢查询 fake 数据；证明模型调用为 0、规则解释器和确定性回答正常。

- [ ] **Step 2: 启用 model override**

```bash
docker compose --env-file .env -f docker-compose.yml -f docker-compose.model.yml up --build migrate
docker compose --env-file .env -f docker-compose.yml -f docker-compose.model.yml up -d --build worker
docker compose --env-file .env -f docker-compose.yml -f docker-compose.model.yml ps
```

不得运行会回显环境值的命令。只核对 worker 内固定 secret 文件存在且其他服务不存在，不读取或输出
文件内容。

- [ ] **Step 3: smoke**

固定 3 个意图、1 个 advisory、1 个 Web parent follow-up。验证模型/model ID、结构化响应、latency、usage、
artifact digest、终态展示和重复 TaskView 0 新调用。

- [ ] **Step 4: quality**

运行 10 个意图场景各 3 次和 5 个 advisory 场景。逐 case 报告：结构通过、最终 capability 是否正确、
是否 fallback、advisory 是否基于给定字段、是否越权。安全项必须 100%；语言质量由负责人抽查。

- [ ] **Step 5: 故障与边界**

真实 timeout/401/429 不靠错误 key 或无界流量制造；用已审查的 fake/fault-injection 证据。现场只验证
一个受控取消和正常关闭，确认没有迟到落库、key/prompt/response 泄漏。

- [ ] **Step 6: secret 和日志扫描**

用操作者持有的 canary 摘要在容器日志、trace 导出和数据库安全字段中查找，必须 0 命中；证据只记录
“扫描对象/命令/0 命中”，不记录 canary 本身。

- [ ] **Step 7: 回滚演练**

移除 model override 并 force-recreate worker。证明 secret mount 消失、后续任务 0 provider 调用、规则
解释器可用；已完成任务仍可读取保存的安全 advisory。数据库 migration 不 downgrade。

- [ ] **Step 8: 证据收口**

记录精确 source SHA、image digest、Compose 文件集合、model ID、SDK 版本、case 结果、latency/usage、
fallback、供应商账户告警的非敏感 readback、日志扫描、回滚和 owner/time window。不得保存 key、
prompt、原始 response、完整账户/项目标识或真实数据。

**PR 3E exit:** 固定真实样本、供应商账户告警 readback 和回滚通过，证据只标记
`test-env verified`；生产网络调用仍为关闭，RI6 需要独立 GO。

---

## 每个实现 PR 的统一验证门

所有 focused tests 通过后，提交前执行：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

同时执行：

```bash
git diff --check
git status --short --branch
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- src tests pyproject.toml uv.lock docker-compose.yml \
  docker-compose.model.yml docs AGENT_HANDOFF.md ARCHITECTURE.md DEVELOPMENT_PLAN.md README.md
```

只暂存本 PR 明确文件，不使用 `git add -A`。触及安全边界必须留下 TDD 反证记录和精确 SHA；CI 绿只算
tests 证据，不算真实 provider、部署或用户验收。

## RI3 总体退出标准

- [ ] Gemini 只通过两个窄端口输出具体 typed result（严格 `IntentDraft`/`ModelAdvisory` + nullable
  bounded `ModelUsage`）；usage 不进入模型生成 schema。
- [ ] 模型不能影响 capability、target、SQL、approval、tool、plan、evidence、状态或动作。
- [ ] intent 60 秒/最多两次 request，advisory 180 秒/一次 request；StarRocks timeout 不变。
- [ ] plan-bound advisory 的 provider `max_output_tokens` 不超过当前 plan 的
  `budget.max_model_tokens`，且固定 4,000 ceiling 不被放大。
- [ ] provider 失败自动回退，确定性任务不被模型错误拖成失败。
- [ ] accepted intent/advisory insert-once、fenced、可恢复；明确接受保存前崩溃的重复模型调用。
- [ ] entry-scoped heartbeat 在 Worker 和兼容 `handle()` 两条入口分别覆盖完整 attempt，且每次只有一个 owner。
- [ ] Slow-query advisory sends at most 20 rows using the exact
  `SLOW_QUERY_SURFACE.allowed_columns` set/order; SQL/stmt/clientIp/full Evidence stay out.
- [ ] key 只从 Git-ignored 宿主文件经 file-backed Compose secret 进入 worker；默认 Compose 0 key/0 网络。
- [ ] Web 显式 parent 上下文有归属校验、20 轮/64,000 字符上限和旧 hash 冻结向量。
- [ ] 飞书上下文、Admin readback、多 worker、其他 provider 和生产调用保持明确未实现。
- [ ] 离线实现、test-env、部署、canary、用户验收证据严格分级。

## 每轮收口格式

### 已验证

只写本轮实际运行的命令、结果、精确 SHA 和真实现场证据等级。

### 只读推理

写基于源码、SDK wheel 或文档推导，但没有运行时证据的结论。

### 未覆盖

明确列出未做的 provider、飞书上下文、并发、生产网络、部署、canary 和 UAT。

### 残余风险

至少报告 provider 保存前崩溃可能重复调用、单 worker 吞吐、preview 模型漂移、宿主 key 文件无版本回滚和
供应商数据政策变化。

本计划已获项目负责人和 Claude/Codex 审核，并于 2026-09-12 收到“开始 RI3”离线开工口令。
仍须逐个 PR 审查合入；未经 PR 3E 独立现场 GO，不连接真实模型、不部署、不归档。
