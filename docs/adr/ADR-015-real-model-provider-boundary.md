# ADR-015：RI3 Gemini 理解、诊断与上下文边界

- 状态：Accepted（2026-09-12；仅授权 RI3 按分 PR 顺序离线实现，真实网络调用仍需 D8 现场 GO）
- 日期：2026-09-12
- 适用阶段：RI3
- 关联：[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-010](ADR-010-m5-durable-attempt-and-compose-boundary.md)

## 背景

当前小维用确定性解释器把用户文本转成 `IntentDraft`，慢查询结论也完全由本地规则生成。这样安全、
可测试，但对自由表达、连续追问和解释复杂现象的体验有限。

RI3 要接入公司已有的 Gemini API key，但模型仍然只是“理解员”和“分析助手”：它可以把自然语言整理
成严格结构，也可以根据已经取得的只读证据给出补充分析；它不能选择最终 capability、目标、SQL、
审批、工具或执行顺序。

此前草案同时引入 9 个 PR、4 次迁移、调用预约、transport attempt、统一终态结果、持久任务 deadline、
新进度状态机、全项目凭证读取重构和 worker readback。它们并非接通首个真实模型闭环所必需，且会把
模型接入和任务平台重构绑在一起。本修订采用最小可维护方案：5 个小 PR、2 次迁移，先证明真实价值，
再按证据扩展。

## 决策

### D1 固定 Gemini 和官方 SDK，不建设通用供应商平台

首版只支持：

- 供应商：Google Gemini Developer API；
- 模型：`gemini-3-flash-preview`；
- SDK：官方 `google-genai`，使用异步 `client.aio.models.generate_content()`；
- API：固定 `v1beta` 与 canonical origin `https://generativelanguage.googleapis.com`；
- 结构化输出：`application/json` + 本地 Pydantic schema；
- 思考等级：意图理解 `low`，慢查询分析 `high`。

不使用 Interactions API，不建 provider registry，不允许从 Web 选择模型、API 版本、自定义 endpoint、
tool、search、code execution、file upload 或 provider session。The planned exact pin is
`google-genai==2.23.0`, never a floating version. Official package metadata identifies
`google_genai-2.23.0-py3-none-any.whl` with SHA-256
`1e63211d44d188b8069c2b354d92b9bde25c1e821513fdbe1948b7c0d9f6b922`.
The implementation PR must independently audit that exact wheel, verify its Apache-2.0
metadata and `google/genai/py.typed` marker, and stop for plan re-review on any mismatch.

未来支持 OpenAI、DeepSeek 或其他供应商时，必须先有第二个真实需求，再从两个窄端口后面增加 adapter；
不提前抽象“万能 generate(prompt, schema)”接口。

### D2 只提供两个窄模型端口

模型边界只有：

1. `IntentModelPort`：安全文本输入，输出 `IntentModelResult`（严格 `IntentDraft` + 同次可信
   `ModelUsage`）；
2. `SlowQueryAdvisoryPort`：固定的慢查询样本输入，输出 `AdvisoryModelResult`（严格
   `ModelAdvisory` + 同次可信 `ModelUsage`）。

两个端口都不接受自由 prompt 或 provider 配置。`SlowQueryAdvisoryPort` 只额外接受一个必填、仅限
1–4,000 的 keyword-only `max_output_tokens` 控制值，用来承接 D4 的当前 plan budget；它不是模型
内容，不能由用户、模型或环境变量提供。

`ModelUsage` 只接收锁定 SDK `usage_metadata` 的 nullable `prompt_token_count` 与
`candidates_token_count`，分别映射为 input/output tokens；不保存 total 或原始 metadata。metadata
整体缺失时两项均为 `None`；metadata 已出现时，以 SDK type 的 `model_fields_set` 要求两字段都实际
出现，再按本地 strict non-negative signed-64-bit 边界收窄，缺字段、负数或溢出按
`INVALID_RESPONSE` 拒绝。`google-genai==2.23.0` 会在 adapter 收到对象前把 raw JSON `bool` 与整数形状
float 归一为 `int`，本地不能声称恢复并拒绝该原始类型；这项 raw-envelope 风险由锁版事实测试与
供应商账户 usage/费用告警及现场 readback 兜底。usage 不进入 provider response schema，模型不能
生成或修改它。

composition root 将同一个不可变 `ModelInvocationProfile` 同时交给 adapter，并保留给 PR 3C 的
application service。profile 固定 provider/model/origin/API version、prompt/schema revision、
thinking、60/180 秒 stage budget 与 2048/4000 output ceiling；application 不反向 import adapter 常量，
也不靠窥探 concrete adapter 生成 artifact metadata/digest。

模型响应采用 `extra="forbid"`。出现未知字段、错类型、超长、非法枚举或解析失败时，整体拒绝，不能
删除越权字段后继续使用。即使模型返回合法 `IntentDraft`，仍必须经过现有：

The provider response schema does not let the model choose `IntentSource`; the adapter
stamps accepted provider output as `MODEL`. Any output field that changes under the
shared redaction rules is rejected as a whole and falls back deterministically rather
than being persisted or passed to Resolver.

```text
IntentDraft → CapabilityResolver → PlanCompiler → WorkflowRunner
            → StepAdmission → ToolGateway → Evidence → Outcome
```

模型不能输出或影响 capability ID、target、SQL、policy、approval、tool name、plan、evidence、
`next_steps` 或任务状态。慢查询 advisory 只增加标明“模型分析”的展示段，不能覆盖确定性事实、限制、
状态、引用和可执行动作。

### D3 出站数据使用两个固定白名单

Typed builders enforce raw character and UTF-8 byte limits before calling the total
`redaction.scrub_text()` function. After scrubbing, they reserialize the complete typed
request and recheck the final byte cap. Oversize or invalid input prevents a model call;
there is no fictional redaction-exception branch.

For the intent request, the current user text and each retained history text field are
each capped at 8,192 characters and 32 KiB of UTF-8 before scrubbing. Selected history is
capped at 20 complete parent tasks, 64,000 characters and 256 KiB of UTF-8. After all
retained strings have been scrubbed, the complete typed request is serialized again and
must fit 512 KiB. Old history is omitted only as complete rounds; if the current request
alone cannot satisfy its field limits or the final serialized cap, the provider call count
is zero.

意图请求只包含：

- 当前用户文本；
- 经 D6 验证和截断的父任务上下文；
- 固定 schema 和固定系统说明。

不得发送整个 `RequestEnvelope`、身份对象、凭证、连接配置、目标物理信息、Evidence 或历史日志。

Slow-query analysis consumes at most the first 20 rows of successful evidence. The exact
outbound names and serialization order are derived directly from
`SLOW_QUERY_SURFACE.allowed_columns`, not copied into a second field tuple in this ADR,
the projector or tests. Field-type rules remain closed and keyed by those derived names.
The projector belongs to `application/model_advisory.py`, because application may consume
the capability surface and Evidence contracts. `rendering/model_advisory.py` and
`rendering/slow_query.py` consume only already-validated display data and must not import
`xiaowei_agent.capabilities`; the existing rendering-layer allowlist remains unchanged.
Per-field and aggregate character/UTF-8 limits are checked before redaction; an
oversized advisory batch is rejected as a whole before any provider call. A later RI4
live schema correction must update the surface, typed projector, digest revision and
pairing tests together, and requires explicit outbound-data review under this ADR's
change gate before activation.

缺列、额外列、非法类型、非有限数值或证据归属不符时整批拒绝，不做“能发几行就发几行”。绝不发送
`stmt`、SQL、`clientIp`、digest、完整 Evidence、target fingerprint、config revision、物理身份、
driver 信息或 provider 原始错误。0 行结果不调用分析模型。

### D4 每阶段有界等待，不设置 450 秒任务总 deadline

使用 Python 3.11 `asyncio.timeout()` 控制本地阶段总时长：

| 阶段 | 思考 | 最长时间 | 最大输出 | 应用层重试 |
| --- | --- | ---: | ---: | --- |
| 意图理解 | low | 60 秒 | 2,048 tokens | 仅 429、5xx、transport error 最多重试 1 次，且仍共享 60 秒 |
| 慢查询分析 | high | 180 秒 | 4,000 tokens | 0 次 |

The intent limit is a fixed pre-plan profile because no plan exists yet. For the
plan-bound advisory, the application layer derives the SDK request's
`max_output_tokens` as the smaller of the fixed 4,000-token advisory ceiling and the
current `ExecutionPlan.budget.max_model_tokens`. The adapter must never receive a value
above either limit. This comparison uses the prepared plan for the current attempt, not a
reconstructed default or Registry constant.

SDK 隐式重试必须关闭或纳入上述次数，不能与应用层重试叠加。Provider stage timeout、schema 错误、
鉴权错误、限流耗尽和 provider 故障都回退：意图使用现有规则解释器，分析段省略；确定性查询和回答
不因此失败。外层 `CancelledError`/SIGTERM 不属于 provider fallback：在 bounded close 后继续传播，
不保存模型事实、不进入 Resolver/Planner，也不伪造任务终态，由既有 lease/stale recovery 接管。
The provider budget starts before fixed-secret read/client construction and includes all
allowed requests, backoff, response validation and client close. Each SDK call receives
only the remaining budget. Artifact lookup/save stays outside this timeout and keeps the
existing persistence-failure semantics; a database failure must not be relabeled as a
model fallback. Local cancellation discards late SDK results but cannot prove that the
remote provider stopped processing.

Application exposes a provider-neutral `ModelPortError` carrying only the closed local
code. The exact intent retry set is `RATE_LIMITED` / `SERVER_ERROR` / `TRANSPORT_ERROR`:
429 maps to rate limited, 5xx to server error, and only explicit non-timeout
`httpx.TransportError` / `OSError` to transport error. 401/403, timeout, invalid response,
local construction failure and other 4xx are not retryable. `httpx` is therefore a direct
runtime dependency rather than a dev-only tool; no new resolved package is introduced.

RI3 does not add a persistent 450-second whole-task deadline. Current source uses a
25-second StarRocks query-timeout upper bound under a 30-second read-only policy maximum, and RI3
must preserve both. The future 180/190/195/200-second StarRocks-specific layers belong
to RI4 and require their own implementation and live evidence. A future whole-task SLO
must be based on measured queue and call data rather than coupled to model integration.

Advisory runs before the existing terminal transition so every channel's first terminal
view is stable. The accepted cost is up to 180 seconds of additional RUNNING time.
After a crash, a committed StarRocks step is adopted from the step journal without a
Gateway replay; only an advisory whose artifact was not saved may be called again.

### D5 复用现有任务生命周期，只补两个可恢复模型事实

现有 Runner heartbeat 只覆盖 Runner 内部。RI3 将同一套续租逻辑提成一个小的 application-level
`run_with_task_heartbeat()` 函数：生产 Worker 用它覆盖一次 `runtime.execute_task()` 和 retry scheduling；
现有 `XiaoweiRuntime.handle()` 也用同一函数包住自己的兼容路径。每个 attempt 只允许入口启动一份，
Runner 不再启动第二份。不新增 `TaskAttemptSupervisor` 类、`TaskPhase` 或第二套 lease 语义；Runner
继续在每次持久写校验 grant/fencing。
The extraction removes only Runner's `heartbeat_interval_seconds`, heartbeat-only sleep
injection/state, `_heartbeat()` and `_run_with_heartbeat()`. Runner retains
`DEFAULT_LEASE_TTL_SECONDS`, `lease_ttl_seconds` and `_require_current_grant()` because
`start()` and `resume()` use that one-shot renewal to validate the grant before execution;
it is not a second periodic heartbeat owner. `handle()` starts the helper only after its
existing `begin_task_attempt()` grant, while its pre-create interpret/resolve/prepare
sequence remains deterministic.

新增一次 migration，建立两张小表：

1. `task_accepted_intents`：每个 task 最多一条，保存首次被本地复验接受的 model/rule `IntentDraft`、
   `intent_input_digest`、来源和安全 metadata/result digest，以及端口返回的 nullable usage；进入
   Resolver 前落库；
2. `task_model_advisories`：每个 task 最多一条，保存被本地复验接受的 advisory、
   `advisory_input_digest`、安全 metadata/result digest 和端口返回的 nullable usage；终态提交前
   落库，TaskView 只在任务已终态时展示。
The durable worker never renders before this call. Advisory input binds the typed
projection plus capability/surface/evidence/profile/schema revisions and sampled flag,
not `RenderPayload` or display prose. TaskView/`handle()` render once after terminal
facts are available.

input digest 只覆盖对应的已脱敏 typed request 与固定 profile/schema revision，不含 key、原始文本或
时间；result digest 覆盖规范化结构化结果。写入必须绑定当前 task grant、lease token 和 fencing
token，采用 insert-once/CAS。恢复时重建 input digest 并与已保存值比较：相同才复用，不同则
fail-closed，不能把旧分析套给新证据，也不能覆盖原行。

如果进程在 provider 已收到请求、但本地尚未保存结果时崩溃，恢复后允许再次调用模型。这是明确接受的
at-least-once 语义：模型没有工具和执行副作用，用户已选择体验优先、暂不设置本地费用硬封顶。不能为
消除这类少量重复调用而建设 reservation/transport-attempt 平台。调用次数、延迟、fallback、token
usage 和安全错误码仍进入现有 trace/audit；不保存 prompt、原始响应或 provider 原始错误。
RI3 extends the closed trace contract with `PipelineStage.MODEL` and typed
`ModelCallObservation` metadata (call kind, total elapsed milliseconds, request count,
nullable input/output usage and closed fallback code). Model-stage free-form
`detail` remains empty.
Its numeric fields are strict, non-negative and bounded to signed 64-bit; request count is
0–2 and advisory is further limited to 0–1. Boolean/float/negative/out-of-range values and
MODEL/non-MODEL observation mismatches are rejected.

模型失败不是 task retryable error；数据库失败、lease loss 和 fencing conflict 仍按现有任务语义处理。
保留 `XiaoweiRuntime.handle()` 作为现有离线测试便利入口，真实模型只装配到 durable worker 路径，不为
形式统一删除已有 API。
Only the granted durable `execute_task()` path calls a dedicated load-or-accept helper,
loads/saves accepted intent and invokes the provider. Provider access must not be added
to the existing shared `_interpret()` method. `handle()` makes zero model/artifact-store calls.

Slow-query advisory is eligible only after deterministic execution succeeded,
Answerability is sufficient and the typed projector accepted the complete batch. It is
displayed only on a `SUCCEEDED` terminal task with the same rebuilt input digest; a saved
artifact is hidden if recovery later ends failed/rejected/indeterminate or detects drift.

### D6 上下文使用显式父任务，Web 先行

第二次 migration 给 `TaskSubmission` 增加可空 `parent_task_id`。上下文只能从显式父链构造，不能读取
“同会话最近一条”，不能使用 Gemini provider session。

首版只在 Web 提供“继续这个任务”：

- parent 必须存在且已经终态；
- actor、tenant、environment、channel 与 Web binding owner 必须一致；重新登录不会仅因 session ID
  变化而丢失上下文；
- 最多追溯 20 个完整父任务；每个历史文本字段最多 8,192 字符/32 KiB UTF-8；
- 只读取持久化用户文本和确定性渲染结果/已保存 advisory；
- 脱敏前选中的历史总量最多 64,000 字符/256 KiB UTF-8；超量按确定性规则从旧到新整轮丢弃；
- 脱敏后连同当前请求和固定 schema 重新序列化，完整 typed request 最多 512 KiB；
- 循环、越权、损坏或非终态 parent 一律拒绝，不退回“猜最近消息”。

With no parent, canonical JSON bytes for `request_dedup_digest`,
`submission_digest` and `idempotency_scope_digest` stay unchanged and are pinned by
frozen vectors. With a non-null parent, both the semantic request digest and full stored
submission digest include `parent_task_id`; the scope digest does not.

On an idempotency-scope collision, memory and PostgreSQL stores compare the semantic
request digest. The same semantic request with the same parent reuses the task; any
semantic difference, including a different parent, conflicts. Changes limited to
request_id/trace_id/`as_of` remain valid retries. The full submission
digest remains a stored-row consistency checksum, not the conflict predicate.
Both backend create paths and the shared `submission_matches_record()` verifier pass the
parent into semantic-digest recomputation; otherwise a successfully created parented row
could later be misclassified as corrupt.

飞书直接回复上下文不在 RI3 首版实现。它依赖 RI2 的真实事件 reply/thread 语义和渠道提交原子性证据；
RI2 明确后复用同一个 `ContextAssembler`，不另建飞书记忆。RI3 不顺带重写 Web/飞书 task、binding、
projection 的聚合事务，也不新增进度状态机。

### D7 Key 只通过 Compose secret 进入 worker

The Git-ignored host `.env` is the only plaintext source for `GEMINI_API_KEY`.
Compose defines `gemini_api_key: {environment: GEMINI_API_KEY}` only in the model
override and grants it only to `worker`, where it is mounted at
`/run/secrets/gemini_api_key`. The merged worker list must retain
`postgres_password`; Web, API, Feishu, migrate and PostgreSQL keep their existing
secret lists. `XIAOWEI_GEMINI_ENABLED=true` is declared only under
`services.worker.environment`, never in the shared `x-app-environment` anchor or another
service.

`GEMINI_API_KEY` is host-side Compose input, not a Settings key and never appears in
`.env.example`; that file remains exactly aligned with `_FIELD_TO_ENV`. README/runbook
documents the host-only entry. The supported path requires Docker Compose 2.24.4 or newer
(the project support floor shared with RI6's `!override` deployment path) and Linux
containers. Version and rendered-config checks are necessary but insufficient:
a split-fake environment secret must pass a functional mount preflight without printing
its value. `docker stack deploy` is not supported for environment-sourced secrets.

adapter 复用现有 `interfaces.secret_file.read_secret_file()`；RI3 不重构飞书、StarRocks、PostgreSQL
的凭证读取。若现有 reader 的 owner/mode/中间目录 symlink hardening 需要加强，应由对应真实接入阶段
单独修复，不和模型功能绑成一个 PR。

SDK client 必须显式使用读取出的 key 和 Developer API 模式，不允许靠 SDK 环境变量自动选择 Vertex AI、
其他凭证或 endpoint。The adapter fixes API version `v1beta` and canonical origin
`https://generativelanguage.googleapis.com`, exposes no base-URL/proxy input, and
rejects ambient proxy configuration before client construction unless the audited SDK
seam proves transport isolation another way.
The exact constructor and transport assertions are pinned only after reading the locked
wheel. 显式启用但 secret 缺失/无效时不创建 client、不联网，并以安全状态降级到规则
解释器；401/403 同样可降级但必须进入可观察的闭集错误码，不能伪装成模型成功。

默认 Compose 不启用模型；启用需要显式 model override。Settings only adds
`gemini_enabled: bool = False` / `XIAOWEI_GEMINI_ENABLED`. Provider/model/API,
two-stage timeout/output and the secret-file path are versioned constants, not
environment inputs. 明文 key、任意路径、自定义 endpoint/proxy 或 Web 模型选择器均不允许。
关闭 override 并重建 worker 后立即回到确定性解释器；无需数据库配置回滚。

RI3 不实现 Admin readback、`worker_start_id` 或运行时代次聚合。RI5 如确需这类能力，应按自己的配置
发布问题设计，不能反向扩大 RI3。

### D8 真实调用需要独立现场 GO

源码、fake 测试、Compose 配置和文档完成都不授权真实 Gemini 网络调用。现场调用前必须逐项确认：

- 公司允许把 D3 白名单数据及 D6 历史发给该 Gemini 项目；
- 供应商的数据保留、训练使用、区域和日志策略已由负责人确认；
- 供应商账户层 usage/费用告警已启用，并有明确 owner、通知渠道、触发规则和本次现场 readback
  时间；证据不得包含 key 或完整账户/项目标识。无法提供该非敏感 readback 时不得下达现场 GO；
- `.env` 与宿主文件权限符合现场要求，证据中不出现 key；
- 测试环境使用固定 synthetic corpus，trace/日志/数据库均通过 secret 扫描；
- 项目负责人下达本次 test-env Gemini GO。

首轮真实验收采用小而固定的样本：10 个意图场景各 3 次、5 个慢查询分析场景，以及 1 条显式父任务
连续追问。安全边界要求 100% 通过；语言质量由产品负责人抽查，不用一个平均分掩盖越权或 fallback。
真实故障分支优先用 fake/fault injection 验证，不故意制造错误 key 或无界调用。

完成后最高只能标记 `test-env verified`。它不等于 deployed、canary 或 user-accepted，也不自动授权
RI6 的生产模型调用。

## 后果

### 正面

- 首个真实模型闭环只需 5 个小 PR、2 次 migration；
- 真实模型增强自由表达和诊断解释，但完全不进入执行安全链；
- provider 故障自动回到现有确定性能力，主功能可用；
- 上下文是显式、可审计、可限长的数据，而不是 provider 黑盒会话；
- 后续真有第二个供应商时，两个窄端口足以承载新 adapter。

### 代价与残余风险

- provider 收到请求而本地未保存时可能重复调用一次；当前按体验优先接受，并通过 usage 观察；
- local timeout/cancel 只能丢弃迟到结果，不能证明 provider 远端已经停止或不会计费；
- Pre-terminal advisory can add up to 180 seconds of RUNNING time. Committed tool
  steps are recovered from the journal without Gateway replay, but an unsaved advisory
  can be requested again.
- 单 worker 下长模型调用会降低吞吐，但没有真实容量证据前不引入并发 worker 平台；
- Web 首版有连续上下文，飞书需等待 RI2 真实事件语义；
- `.env` key 没有数据库版本和一键回滚，关闭方式是移除 model override 并重建 worker；
- Gemini preview 模型和 SDK 仍可能演进，升级必须重新做 schema、timeout、usage 和数据边界测试。

## 备选方案与否决理由

- **9 PR/4 migration 的调用平台**：首个 adapter 尚未证明价值，投入和回归面过大；否决。
- **模型直接选 capability/SQL/tool**：破坏确定性安全链；永久否决。
- **建立通用 provider registry**：只有一个真实 provider，没有第二个变化样本；YAGNI。
- **沿用 v1beta Interactions API**：首版不需要 conversation/tool 等能力，生命周期更复杂；否决。
- **Persistent 450-second whole-task deadline**: it couples queue, query and model
  budgets and adds a new lifecycle timeout without measured runtime SLO evidence; rejected.
- **仅在内存保存模型结果**：worker 恢复会改变已接受意图或终态展示；否决。
- **现在同时支持飞书上下文**：真实 reply/thread 契约尚未由 RI2 验证；延期。
- **一次重构所有凭证 reader**：属于独立安全 hardening，不是 Gemini 接入的必要条件；延期。

## 回滚

功能回滚不需要回退数据库：移除 model override、重建 worker，模型调用即为 0，已有任务继续用规则
解释器；两张模型事实表和可空 parent 列保留只读兼容。代码回滚前确认新代码写出的 parent/artifact
仍能被旧版本安全忽略。任何 migration downgrade 只用于测试空库，不作为生产回滚手段。

## 变更门

以下任一变化必须修订本 ADR，而不是只改环境变量：

- 增加供应商、模型、endpoint、API 形态或工具调用；
- 扩大出站字段、任一字符/UTF-8/最终序列化字节上限、历史容量或支持新诊断 capability；
- 让模型影响 plan、target、SQL、approval、tool 或 next_steps；
- 修改调用次数、阶段 timeout、fallback 或持久化语义；
- 增加飞书上下文、后台并发、worker 扩容或 provider session；
- 将真实调用从 test-env 升级为部署/canary/生产用户验收。

## 参考资料

- [Gemini 3 Flash Preview 模型说明](https://ai.google.dev/gemini-api/docs/models/gemini-3-flash-preview)
- [Google Gen AI Python SDK](https://googleapis.github.io/python-genai/)
- [Google Gen AI Python SDK 源码仓库](https://github.com/googleapis/python-genai)
- [Docker Compose secrets](https://docs.docker.com/reference/compose-file/secrets/)
- [Docker Compose merge rules](https://docs.docker.com/reference/compose-file/merge/)
