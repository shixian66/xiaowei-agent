# 小维 Agent 2.0 真实接入总体设计

- 状态：Accepted V3；RI3 精简实现方案于 2026-09-12 获批离线开工，真实调用仍需独立现场 GO
  + **RI5 Proposed Amendment（2026-09-14，与 ADR-007/014/015 的 2026-09-13 修订成套，待一并重新接受）**
- 规划与 PR 3A 开工基线：`ab35b756b07aa1baa2f0eb3ac98dba24999c40b1`；后续 PR 基线由 `AGENT_HANDOFF.md` 记录
- 证据等级：源码事实 + 只读设计；没有真实调用、部署、canary 或用户验收
- 日期：2026-09-12；RI5 候选修订 2026-09-14

> **RI5 修订状态说明**：RI5 的实现方案已由
> [RI5 简化设计](../../plans/RI5-local-web-admin-simplified-design.md)取代。本文中所有 RI5/Admin
> 相关段落已按新方案同步，状态为 **Proposed**。
> ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订必须成套复核并重新接受；任一份未被接受，RI5 都不得开工。
> RI1–RI4、RI6 的口径不受本次同步影响。

## 1. 大白话结论

这次不重做小维，也不把模型变成“万能执行器”。我们只把已经搭好的安全主干，分成小闭环接上四类真实东西：飞书登录与机器人、模型 API、StarRocks 只读查询、管理员配置页面；最后再用 Docker Compose 部署和验收。

推荐路线是按依赖推进六个里程碑，每个里程碑一个或多个单一目的 PR、一个开关和一个回滚点：

1. RI1：飞书 OAuth 与 Web 启动；
2. RI2：测试环境真实飞书联调；
3. RI3：真实模型 API；
4. RI4：StarRocks 真实只读查询；
5. RI5：本地 Web Admin 简化配置；
6. RI6：正式部署、canary 与产品用户验收。

编号表达交付路线，不表示所有阶段机械串行。RI2 是飞书现场验证；RI3 和 RI4 的离线实现不依赖它，
项目负责人本轮已选择先评审 RI3。RI5 等待 RI1/RI3 的配置契约稳定，RI6 才汇总前序实际要启用能力的现场
证据。任何阶段都不能借另一阶段的证据宣布成功；测试环境联调只能叫 `test-env verified`，不能叫
canary。

## 2. 已确认前提

- 全部服务使用 Docker Compose；继续使用同一个应用镜像、多进程服务，不拆微服务。
- 容器中的 Web 服务监听普通 HTTP；**基础 `docker-compose.yml` 始终只发布 `127.0.0.1:8080`**，
  任何阶段都不改基础文件。项目不新增 TLS、Ingress 或反向代理。发布宿主端口只能由独立 override
  承担，且分两种互不混用的用途：
  - **RI5 局域网 override**：仅在本地体验场景，且必须在首次强制改密（loopback 阶段完成）之后
    才可启用，配合显式 `lan_http` 模式发布局域网端口；
  - **RI6 生产 override**：正式部署才可发布宿主 `IP:8080`，OAuth/session 仍只接受已批准的
    HTTPS SSO Host/Origin。
- 浏览器 OAuth 回调有两种模式，**HTTPS SSO 是正式模式**：公网回调继续使用用户已有的 HTTPS SSO
  域名；SSO 域名到 Compose Web 端口的既有转发属于环境前提，不在本项目中重建。`lan_http` 是
  **RI5 的本地体验例外**，只接受显式配置的 canonical loopback 或 RFC1918 IPv4 字面量加显式端口；
  飞书平台是否接受具体局域网 HTTP 回调，只能由飞书后台配置和真实 OAuth 回调测试确认，应用端
  只承诺兼容该模式。该例外不适用于 RI6 生产发布。
- 复用旧小维的飞书应用；切换时先停旧小维，再启动 2.0，绝不同时消费同一个机器人事件流。
- 飞书 `open_id` 只证明“是谁”；租户、环境和权限仍由 2.0 的服务端身份目录决定。
- 普通用户只能查看或提交只读任务；只有明确标记的 admin 可以管理配置。群成员身份不能自动推导 admin。
- Current source allows a StarRocks query-timeout value up to 25 seconds and has a
  30-second read-only Policy ceiling. RI4, not RI3, proposes the future
  180/190/195/200-second server/driver/ToolCall/Policy layers and must prove them.
- RI3 固定使用 Google Gemini Developer API `v1beta`、canonical origin
  `https://generativelanguage.googleapis.com`、`gemini-3-flash-preview` 和官方 `google-genai` 的
  structured-output API；意图理解用 low/60 秒/最多两次 request，证据诊断用 high/180 秒/一次
  request。StarRocks 现有超时保持不变，不新增任务总 deadline。本地不设费用硬上限，仍记录 usage
  并限制每任务调用次数。

### 2.1 当前 2.0 源码事实

- RI1 的真实 OAuth adapter 与 Web composition root 已默认关闭地合入；尚无真实飞书环境、部署或
  用户验收证据。
- 飞书长连接、消息发送/更新和群成员 adapter 已有 default-off 离线实现，但没有真实飞书环境证据。
- M6b 的 StarRocks target-bound readonly adapter 已合并离线代码和测试；真实目标、授权和现场验证仍缺失。
- 规则式 `IntentInterpreter` 仍是 Runtime 当前唯一路径；PR 3B 已离线实现固定 Gemini dependency、
  严格 DTO/窄 port 与 adapter seam，但尚未接入 durable Runtime，也没有真实调用或模型运行证据。
- 当前没有 Admin 配置中心；配置来自进程环境和只读文件引用。
- M6b/M7 的现有最强证据是离线测试，不是部署、canary 或用户验收。

### 2.2 旧小维只读参考

旧项目只用于确认用户已经跑通的行为：标准 OAuth code 换身份、已有 HTTPS SSO callback、应用本身可运行在 HTTP 后端。2.0 复用这条用户流程，不复制旧代码，也不依赖旧仓库。

2.0 明确不照搬旧实现中较弱的部分：明文 secret 环境变量、把 actor 放进签名 cookie、state 在过期前可重复使用、由浏览器脚本写非 HttpOnly session，以及把 provider 原始错误正文返回给上层。2.0 继续使用已经落地的一次性 state 摘要、服务端 session、当前身份目录和统一安全错误。

## 3. 目标与非目标

### 3.1 目标

- 真实飞书 OAuth 登录、长连接收消息、消息发送/更新、群成员查询。
- 真实模型只做结构化理解与解释；模型失败时可回退确定性规则。
- 接入第一个真实系统 StarRocks，只复用已有两个只读 operation。
- 提供本地 Web Admin：单管理员在本机 Compose、局域网范围内配置 Gemini 与飞书两个闭集集成，
  并执行显式连接测试。不含 StarRocks 配置、不含通用配置中心、不含版本发布与回滚。
- 所有真实接入均具备超时、限流、审计、证据、开关和回滚。
- 用精确部署 SHA 分开记录测试环境、部署、canary 和用户验收证据。

### 3.2 明确不做

- 暂缓整个 M8；不实现 Dinky 查询、状态、start、stop 或 restart。
- 不实现 Alertmanager silence、Prometheus 规则修改或其他 E1 写操作。
- `_E1_EXECUTION_ENABLED` 继续关闭；接入真实渠道、模型或只读系统不改变它。
- 不引入 LangGraph、Multi-Agent、通用 capability DSL、向量数据库、消息总线或微服务拆分。
- 不顺带接 Jenkins、Kafka、Kubernetes 或独立 MySQL。
- 不让用户、模型或 Web 页面提交任意 SQL、任意 endpoint 或任意执行顺序。

## 4. 三种路线

| 路线 | 做法 | 优点 | 代价 | 结论 |
| --- | --- | --- | --- | --- |
| A 依赖化小闭环 | 一次实现一个垂直闭环；RI2/RI3/RI4 按明确口令排序，RI5/RI6 等待真实依赖 | 风险最低，出问题容易定位和回滚，也允许先做不依赖飞书现场的 RI3 | 总日历时间略长 | **推荐** |
| B 飞书与模型并行 | 两个团队并行做渠道和模型，再汇合 | 开发看似更快 | 配置、身份、故障语义未稳定时容易互相返工 | 不推荐当前采用 |
| C 先做 Admin | 先造通用配置中心，再接供应商 | UI 早出现 | 会为尚未稳定的配置契约造过度抽象 | 否决 |

## 5. 模块边界

```text
浏览器/飞书事件
      |
      v
interfaces/  协议解析、OAuth、身份上下文、RenderPayload 投影
      |
      v
application/ Runtime 与任务生命周期
      |
      +--> capabilities/ + planning/ + governance/  确定性解析、计划、Policy、SQLGuard
      |
      +--> runners/ -> ToolGateway -> tools/starrocks.py  唯一真实系统执行路径
      |
      +--> persistence/ + evidence/ + observability/  状态、接受意图、证据、诊断、审计

模型 API --> 只产出受 schema 约束的 IntentDraft/Advisory --> 回到确定性链路
Admin   --> 只管理 Gemini/飞书两个闭集配置 --> 宿主机重启后各服务上报加载回执
```

具体约束：

- `interfaces/` 只做渠道协议、OAuth、浏览器 session、身份传递和安全投影，不写 capability 路由或 SQL。
- 真实 OAuth adapter 实现现有 `FeishuOAuthPort`；只返回 `subject_ref`，不返回本地角色或执行权限。
- 应用层分别定义 `IntentModelPort` 与 `SlowQueryAdvisoryPort`，外层 Gemini adapter 在 `interfaces/`
  实现；`application/` 不反向 import `interfaces/`。两个窄端口没有通用 `generate(prompt, schema)`，
  模型 adapter 不进入 `ToolGateway`，也不能持有 Runner、Policy、SQLGuard 或基础设施客户端。
  严格复验后的 model/rule draft 作为 task 级 insert-once accepted intent 保存，随后重新经过
  `CapabilityResolver`；合法 advisory 另作 task 级 insert-once 展示事实。
  端口失败只抛 provider-neutral `ModelPortError`；意图重试闭集精确为 429、5xx 与明确 transport
  error，application 不 import Gemini concrete exception。
- StarRocks 仍只由 `ToolGateway` 调用现有 target-bound readonly adapter；真实连接配置不能出现在 `ToolCall`、模型输出或用户请求中。
- Admin 保存配置不等于激活真实调用。配置落盘后必须由宿主机重启 Compose，各服务在启动时自行
  加载并上报加载回执；Web 不自动重启，不挂载 `docker.sock`。
- Admin 不新增执行进程、不新增 dispatch lane、不纳入 StarRocks 配置或 StarRocks 测试请求。
  M7“只有 task worker 装配完整执行 Runtime”的口径不被修改。
- 连接测试是**控制面探针**：Web 在管理员明确点击且对应真实测试开关已打开时，执行
  `gemini_connection`、`feishu_credentials`、`feishu_oauth` 三个连通性探针。它们不创建 Task、
  TaskSubmission、Evidence 或 capability，不产生 `ToolResult`，不进入 Policy/SQLGuard/
  ApprovalGate/`ToolGateway`，也不改变任何服务的 readiness。Web 不得由此取得模型端口、任意
  endpoint/路径或任何运维目标访问权。边界见
  [ADR-007](../../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)
  §RI5 Proposed Amendment、[ADR-014](../../adr/ADR-014-real-feishu-oauth-and-web-activation.md) R4 与
  [ADR-015](../../adr/ADR-015-real-model-provider-boundary.md) R3。
- PostgreSQL 继续作为任务、session、accepted intent、advisory、证据和审计事实真源；secret 字节
  不进入 PostgreSQL。现有 heartbeat 提成 application helper：task-worker 包住
  `execute_task()`/retry scheduling，兼容 `handle()` 包自己的 attempt，每条入口一份；Runner 不再启动
  第二份。`handle()` 保留为不装配真实 provider 的离线测试便利入口。

## 6. 关键数据流

### 6.1 Web OAuth

下列步骤描述**正式模式（HTTPS SSO）**。RI5 的 `lan_http` 本地体验例外见本节末尾。

1. 浏览器访问已有 HTTPS SSO 域名的登录入口。
2. 2.0 生成一次性 OAuth state，只在 PostgreSQL 保存摘要，并用临时 cookie 绑定当前浏览器。
3. 飞书回调后，adapter 用 code 换取 `open_id`；provider 原始响应不进入日志和页面。
4. 服务端按当前身份目录重新解析租户、环境、actor 和权限。
5. 成功后轮换随机 session；未知用户、重放 state、超时或身份已撤销均失败关闭。

Compose 内部仍是 HTTP；正式模式下浏览器安全 origin 与 OAuth redirect 使用现有 HTTPS SSO 域名。
计划不新增证书或代理服务。

**RI5 `lan_http` 例外**：本地体验模式下，public origin 是显式配置的局域网 HTTP origin，OAuth
callback、Origin 校验与 Cookie 决策统一消费该值，仍不动态信任 `Host`、`Forwarded` 或请求 URL。
该模式使用普通 Cookie 名且不设 `Secure`，不设 HSTS，Cookie 以明文 HTTP 经局域网传输，只用于
受信局域网。协议放开只作用于本方 public origin——飞书授权/token URL 的 HTTPS 约束不变。
Session 绑定 canonical public origin digest，切换模式或 origin 等同于全体登出。
RI5 还有一条不建立登录态的 `feishu_oauth` callback 连通性探针，见 §6.5。
OAuth state 的过期清理、容量检查和插入必须由 PostgreSQL 同一事务内的固定锁串行化；普通
`count → insert` 事务不能抵抗并发穿透，应用进程锁也不能承载多进程正确性。

### 6.2 飞书机器人

1. 长连接收到事件，先校验 app、tenant、sender、message 和事件唯一键。
2. 用事件 ID 去重；重复投递不会重复创建任务。
3. 入口只提交 `RequestEnvelope`，Runtime 决定 capability 和计划。
4. 渠道 worker 只消费 `RenderPayload`，发送初始卡片并在终态更新；失败进入有界重试或 dead letter。

### 6.3 模型

1. Typed builders enforce raw character limits and UTF-8 validity before the total
   `redaction.scrub_text()`. Per-field byte ceilings are mathematically derived from the
   character limits; the aggregate history byte ceiling is likewise derived from its
   64,000-character limit. Builders then
   reserialize and recheck the final byte cap; invalid or
   oversized input yields zero model calls.
   Model ports receive only closed `user_text/history/context_truncated` or
   `rows/sampled` DTOs, never `RequestEnvelope` or arbitrary context dictionaries.
2. worker 在完整 heartbeat 下先读 task 级 accepted intent。没有记录时，模型关闭/不可用直接使用规则
   解释器；模型启用时用 low thinking，在共享 60 秒内仅对 429/5xx/transport error 最多重试一次。
   合法 model/rule draft 在当前 grant/fencing 下按安全 input/result digest insert-once，随后才进入
   Resolver、Planner、Policy 和 SQLGuard；retry/restart 只有重建的 input digest 相同时才复用。
   Provider timeout/error falls back deterministically; outer `CancelledError`/SIGTERM
   propagates after bounded close with no artifact, downstream planning or terminal write.
3. provider 收到请求但本地保存前崩溃时，恢复后允许再次调用。模型无工具和执行副作用，RI3 按体验
   优先明确接受这项 at-least-once 语义，不建设调用预约或 transport-attempt 平台。调用次数、延迟、
   usage 和 fallback 仍进入安全 trace/audit；不保存 prompt、原始 response 或 provider error 正文。
4. Conversation continuity follows only persisted `TaskSubmission.parent_task_id`.
   Web requires the user to choose an explicit terminal task. The entry performs a
   scoped lookup, and Worker `ContextAssembler` revalidates every hop for actor, tenant,
   environment, channel and Web binding ownership; a new session ID does not break
   ownership. Recent-message guesses, root refs and provider chat state are forbidden.
   Before scrubbing, current text and each history text field are capped at 8,192
   characters, which implies at most 32 KiB UTF-8; selected complete history is capped
   at 20 parent tasks and 64,000 characters, which implies at most 256,000 UTF-8 bytes
   (less than 256 KiB). After scrubbing, the complete
   typed request is serialized again and must fit 512 KiB; oldest rounds are omitted
   whole, while an oversized current request yields zero calls. `scrub_text()` is total
   for typed strings, so no fake redaction-error path exists. Feishu reply/thread context waits
   for RI2 live-event evidence; RI3 does not rewrite channel aggregation.
   Null-parent canonical request/submission/scope digests remain byte-identical. A
   non-null parent enters the semantic request digest and stored submission digest,
   never the scope digest. Any semantic difference, including a different parent,
   conflicts; changes limited to request_id/trace_id/as_of remain a valid retry.
5. After slow-query Evidence/Answerability, the capability projector derives its exact
   names and order from `SLOW_QUERY_SURFACE.allowed_columns` and takes at most 20 rows.
   Closed type rules validate every derived field and the input digest binds
   surface/evidence schema revisions. Missing, extra or invalid fields reject the whole
   batch; SQL/stmt, clientIp, digest, secret, connection/target/internal Evidence fields
   are never sent. Zero rows means zero advisory calls. Any RI4 live schema correction
   changes the surface, projector schema, digest revision and pairing tests together.
   The projector runs only for successful deterministic execution with a sufficient
   Answerability verdict. It lives in `application/model_advisory.py`; `rendering/`
   receives validated display data only and never imports the capability surface.
6. 诊断以 high thinking 调用一次，最多 180 秒，输出不超过现有 slow-query plan 的 4000 token 预算。
   合法 advisory 按安全证据投影的 input digest 在当前 grant/fencing 下 insert-once 保存，随后走现有
   任务终态；TaskView 只在终态后展示。input digest 不同 fail-closed，不能把旧分析套给新证据。它只能
   增加“模型分析、建议、未确认项”，不能改事实、refs、status 或可执行 next steps。
   The worker does not render before advisory. TaskView/`handle()` render once after
   the terminal fact plus saved advisory are available. This can extend RUNNING by up
   to 180 seconds; committed StarRocks steps recover from the journal without Gateway
   replay, while an unsaved advisory may be called again. A stored advisory is displayed
   only on a `SUCCEEDED` task whose rebuilt typed input digest still matches.
7. Intent/advisory use independent 60/180-second stage budgets and RI3 adds no whole-task
   deadline. RI3 preserves the current StarRocks 25-second query-timeout upper bound and 30-second
   read-only policy maximum; RI4 owns the future 180/190/195/200-second layers.
   Heartbeat moves completely to one application helper: worker covers execute/retry and
   `handle()` only its post-grant attempt. Runner removes only its periodic interval/sleep
   state plus `_heartbeat()`/`_run_with_heartbeat()`; it retains lease TTL and
   `_require_current_grant()` for the one-shot start/resume grant renewal.
8. Gemini 使用官方 SDK 的 `client.aio.models.generate_content()` 与 structured JSON response；固定无
   tools/search/code/files/function calling/provider session，并显式禁用 automatic function calling。
   intent 的 slots 使用 allowlist 并集的固定字段 frozen DTO，避免 Developer API 不支持的动态
   `additionalProperties` map；字段只能省略或给 string，显式 null 整体拒绝；intent-specific
   allowlist 仍由本地单一真源复验。intent/advisory 输出分别为 2,048/4,000
   tokens，仍需本地 Pydantic 语义复验。模型文本只以 escaped/plain text 展示，不能形成链接、按钮或
   可执行 next step。
   The adapter fixes Developer API `v1beta` and canonical origin
   `https://generativelanguage.googleapis.com`, exposes no endpoint or
   proxy input, and rejects ambient proxy redirection before client construction.
   Plan the exact pin `google-genai==2.23.0` and official package wheel SHA-256
   `1e63211d44d188b8069c2b354d92b9bde25c1e821513fdbe1948b7c0d9f6b922`;
   the implementation PR independently audits the artifact and dependency tests verify
   Apache-2.0 metadata and `google/genai/py.typed`.
   Offline conformance uses the locked SDK's outer async method and schema transformer over
   an `httpx.MockTransport`, proving one transport request without real network or AFC warning.
   SDK-normalized usage is then narrowed to nullable non-negative signed-64-bit local counts;
   coercible integer-like raw values (bool, integral float and numeric string) are no longer
   distinguishable after SDK normalization.
   A source-wide AST boundary permits `google.genai` only in the Gemini adapter seam.
   Trace adds one typed MODEL-stage aggregate observation, never prompt/response/error
   text; numeric fields are strict, non-negative and signed-64-bit bounded, with request
   count capped at two for intent and one for advisory.
9. 离线 provider 测试通过后仍需 RI3 独立现场 GO；只用固定 synthetic fixture 验证结构化成功、安全
   边界、usage、上下文和关闭回滚，最高标记 `test-env verified`。故障分支由明确标记的
   fake/fault-injection 证明，不能冒充真实 provider 故障。

### 6.4 StarRocks

1. 已认证用户提交只读诊断请求。
2. Resolver 和 Planner 只生成已有 `list_slow_queries` 或 `count_queries_in_window`。
3. `StepAdmission` 复核 policy、SQL AST、目标和调用超时。
4. Gateway 按 `(gateway, target_fingerprint)` 精确选择真实 adapter。
5. adapter 同连接做固定 preflight，再执行确定性 SQL；最多返回 200 行。
6. RI4's proposed target, not current source behavior: SQL 180 seconds, driver read 190
   seconds, normal Gateway call 195 seconds and Policy ceiling 200 seconds; connect and
   write socket timeouts remain 5 seconds.
7. 超时、权限/DDL/身份漂移、结果超量或连接不确定均结构化失败，不返回部分成功。
8. Gateway 的 `asyncio.wait_for` 只能让调用方按时返回；它不能终止
   `asyncio.to_thread` 中已经开始的 PyMySQL 调用。超时后不得消费迟到结果，runbook 必须把
   “后台线程/服务端会话可能继续到 driver/server timeout”列为残余风险；本阶段不增加 `KILL`。

### 6.5 Admin 配置

闭环是：本地管理员登录 → Web 保存第三方配置 → 宿主机重启 Compose → 服务加载回执 → 管理员手动
测试。

1. **本地管理员**：管理员表最多一行，数据库为空时由 Web 在 migration-head 检查通过后幂等 seed
   `admin/admin` 并置 `must_change_password=true`（migration 不写口令或哈希）。首次改密前只开放
   登录、改密和退出。口令用标准库 `hashlib.scrypt`，不新增认证依赖。身份来源闭集增加
   `LOCAL_ADMIN`，固定映射 `tenant_id=dev-local`、`environment_id=dev`、`actor=admin`。
   首启保护只由 Compose 端口绑定承重：基础 Compose 只发布 `127.0.0.1:8080`，改密完成后才由独立
   override 开放局域网。
2. **配置保存**：`.config/integrations.json` 是 Gemini API key 与飞书 App ID/Secret 的唯一宿主机
   明文真源，取代 `.secrets/gemini_api_key`、`.secrets/feishu_app_secret` 及其 Compose secret 类型
   与容器路径，不保留双读或回退。Web 读写挂载其所在目录，task worker 与实际需要飞书配置的进程
   只读挂载，**API 不挂载**。保存用同目录临时文件加原子替换，不保存历史版本。Secret 永不回显，
   页面只显示“已配置/未配置”。API 不接受 host、port、任意 endpoint、模型名或任意路径。
3. **generation 与加载回执**：文件顶层保存正整数 `generation`，每次成功替换写入 `previous + 1`。
   `service_config_state(service_name, provider)` 记录各服务启动时实际加载到的 generation 与闭集
   加载状态；它只是加载回执，不表示服务存活，也不是服务注册中心。`healthz` 只答进程存活，
   `readyz` 保留数据库、migration head 与装配检查，不把外部 Provider 纳入整体 readiness。
4. **连接测试**：`provider_test_state` 只保存每个测试项当前 generation 的最新结果（含
   `duration_ms`）。页面刷新不调用外部服务，只有管理员点击才发起真实请求；失败只持久化闭集
   `error_code`、本地安全提示与耗时，不保存 Provider 原始正文、Secret 或 Token。
   `gemini_connection` 用固定模型与固定最小 synthetic 输入，不创建任务、不保存对话或响应正文；
   `feishu_credentials` 只取应用凭证，不发消息、不操作群聊；`feishu_oauth` 是 callback 连通性
   测试，启动需 `LOCAL_ADMIN` 加 Origin/CSRF，callback 需仍持有有效 `LOCAL_ADMIN` Session，
   测试分支绝不签发或替换任何 Session。三者开关独立且默认关闭。
5. **重启与回滚边界**：Web 只保存配置，不自动重启；宿主机统一执行 Compose 重启脚本。首版不做
   自动回滚，配置校验失败时不替换当前有效文件；连接测试不改变服务 readiness，也不自动改写配置。
   功能回滚是关闭相应 Compose 开关并重建服务，不需要回退数据库。
6. **不做**：不做 StarRocks Admin 配置、不可变配置版本、显式发布、审批、审计历史、
   readback/rollback 状态机、通用 provider registry、配置热加载、多用户与 RBAC。

## 7. 配置与 secret

### 7.1 RI1 到 RI4

- 普通配置使用 `XIAOWEI_*` 环境变量，并继续 `extra=forbid`、半配置拒绝和默认关闭。
- secret 只通过容器只读文件引用，例如 Docker Compose secret；只有受信 composition root 接收
  绝对挂载路径，不接收明文环境变量。
- Gemini plaintext may exist only in the fixed Git-ignored host key file
  `.secrets/gemini_api_key`. Neither `GEMINI_API_KEY` nor `GEMINI_API_KEY_FILE` is a
  Settings/`_FIELD_TO_ENV` key, container environment value or `.env.example` entry.
- The only new application setting is default-false `XIAOWEI_GEMINI_ENABLED`; provider,
  model, API, budgets, proxy policy and secret path are fixed in versioned code.
- The model override declares a file-backed `gemini_api_key` and
  grants it only to task worker. Merged worker secrets retain `postgres_password`;
  every other service keeps its current secret list. Container path is fixed at
  `/run/secrets/gemini_api_key`.
- The same override sets `XIAOWEI_GEMINI_ENABLED=true` only under
  `services.worker.environment`, not the shared `x-app-environment` anchor or any
  non-worker service.
- Require Docker Compose 2.24.4+ (the project support floor shared with RI6's
  `!override` deployment path) and Linux containers. Version/config checks are necessary
  but insufficient; a split-fake file secret must pass a functional mount preflight without
  exposing its value. This is not a `docker stack deploy` path.
- Gemini 复用现有 `interfaces.secret_file.read_secret_file()`；RI3 不统一重构飞书、StarRocks、
  PostgreSQL 的 reader。共同 hardening 由对应真实接入阶段单独负责。
- 错误、日志、trace 和测试快照只展示字段名、非秘密配置和脱敏状态，不展示 host、user、路径实值、
  token、prompt、response 或 provider 原始正文。

### 7.2 RI5 以后

- Gemini 与飞书的凭据只保存在宿主机 Git-ignored 的 `.config/integrations.json`（容器内
  `/run/xiaowei-config/integrations.json`），宿主目录 `0700`、文件 `0600`；secret 字节不进入
  PostgreSQL、日志、trace、异常或页面。
- PostgreSQL 只保存本地管理员行、Session、服务加载回执和连接测试结果摘要，不保存任何 secret。
- provider、model、API version、endpoint 与 SDK 仍是代码内固定常量；Web 只读展示当前固定模型，
  不提供模型、API 版本、endpoint、代理或任意路径的编辑入口。
- 启用规则只有两层：`.env`/Compose 决定进程是否装配该功能，JSON 中的 `enabled` 决定已装配功能
  是否消费对应配置，两者同时为真才启用；JSON 不能反向启动未装配的进程。真实测试还须额外满足
  对应 `*_REAL_TEST_ENABLED=true`，默认关闭。不引入第三层优先级，同一字段不得有两个隐式真源。
- 单 Web 写进程串行执行“读取当前 generation → 校验 → 原子替换”，不支持多写实例，不保存历史版本，
  不提供 CAS 发布或一键回滚。

## 8. 身份与权限

- OAuth `open_id` 是外部身份键，不是 actor、tenant、environment 或 role。
- 当前身份目录每次登录和每次受保护请求都重新解析；删除映射即撤权。
- RI5 不新增配置 RBAC 权限。配置读取、保存与连接测试接口**只接受 `IdentitySource.LOCAL_ADMIN`**；
  飞书 principal 即使持有 `ADMIN_ALL_SAFE_TASKS` 也不得读取配置状态、修改凭据或发起测试。
- 本地管理员只能管理上述两个闭集集成的配置，不能因此获得 E1、任意 SQL、越租户查看、跳过 Policy
  或访问任何运维目标的权限。
- 飞书群只用于上下文和成员确认；群主、群管理员或“在群里”都不自动成为系统 admin。

## 9. 超时、重试和限流

| 边界 | 超时/限制 | 重试 | 失败语义 |
| --- | --- | --- | --- |
| 飞书 OAuth/API | provider 总预算 5 秒；OAuth WebAuth 外层 watchdog 6 秒 | OAuth code 不重试；消息投递最多 3 次 | 安全错误，不回显 provider 正文；外层超时丢弃迟到结果 |
| Gemini 意图 | low；`asyncio.timeout()` 总计 60 秒；输出最多 2,048 tokens | 仅 429/5xx/transport 最多重试一次，两次共享 60 秒 | 使用规则解释器 fallback，不伪装模型成功 |
| Gemini 诊断 | high；`asyncio.timeout()` 最多 180 秒；输出最多 4,000 tokens | 不自动重试 | 保留确定性回答，advisory 为空 |
| RI4 StarRocks connect | 5 秒 | 不自动重试 | 调用失败 |
| RI4 StarRocks SQL | 180 秒 | 不自动重试 | TIMEOUT，不返回部分结果 |
| RI4 StarRocks driver read | 190 秒 | 不自动重试 | UPSTREAM/TIMEOUT |
| RI4 StarRocks ToolCall | 正常 195 秒；Policy 上限 200 秒 | Runner 按既有预算决定后续，不由 adapter 重试 | 任务失败或明确不确定 |
| RI4 StarRocks result | 200 行 | 不适用 | 超量整体失败 |
| 渠道消息 | 租户并发 2，最多 3 次 | 有界退避 | dead letter + 审计 |

RI4 必须同时修改 StarRocks profile、`POLICY_REVISION`、Settings、adapter 校验、execution
binding 的独立 `tool_timeout_seconds`、ADR 和测试。Runner 不得再从 profile 上限反推正常调用值；
否则“超过 Policy 上限”的拒绝分支对所有正常 binding 都失去区分力。

## 10. 证据升级路径

| 等级 | 必须具备 | 不能宣称 |
| --- | --- | --- |
| 源码审查事实（非能力状态） | 精确 SHA 的真实 diff 审查 | 测试通过、可运行 |
| declared | capability/config 契约已声明且默认关闭 | 已配置、已测试或可调用 |
| configured | 获批配置已按该阶段既定方式持久化（不可变版本，或 RI5 的单配置文件加 `generation`），凭据引用可解析且不泄露 | 进程已加载或真实调用可用 |
| tests | 四条基线命令和相关契约/eval 通过 | 真实服务可用 |
| test-env verified | 获批测试环境、精确 SHA、真实调用清单与脱敏 evidence | 已部署、canary、生产可用 |
| deployed SHA | 指定环境运行指定镜像/SHA，健康检查与回滚材料齐全 | 产品行为已验收 |
| canary | 生产环境小范围真实用户/流量，明确窗口和观测结果 | 全量用户验收 |
| user-accepted | 产品负责人按验收清单确认 | 未列出的能力也可用 |

每阶段证据必须记录：代码 SHA、镜像 digest、配置标识（版本号或 RI5 的 `generation`）、启用开关、目标环境、执行时间、操作者、验证命令/场景、脱敏结果和回滚结果。PR 描述不能代替这些事实。

## 11. 阶段依赖与硬门

```text
RI1 OAuth/Web 代码门 ──> RI2 飞书 test-env 证据 ───────────────┐
        │                                                      │
        ├──> RI3 Gemini（独立 ADR/离线实现/现场 GO）──┐         │
        │                                             ├──> RI5 本地 Web Admin ──> RI6
        └──> RI4 StarRocks（独立 DBA/目标/现场 GO）────┘         │
                                                               │
RI6 只启用实际具备对应 test-env + H 层 GO 的能力 <──────────────┘
```

RI2、RI3、RI4 互不借用真实调用许可，离线实现次序由项目负责人逐项下令。RI3 已获顺序离线开工
授权并从纯文档 PR 3A 开始；RI2 没有被完成、取消或自动跳过。RI5 只等 RI1/RI3 的配置契约稳定
（不含 StarRocks，因此不依赖 RI4）；ADR-007、ADR-014、ADR-015 与总体 spec 的 RI5 修订必须成套复核并重新接受；任一份未被接受，RI5 都不得开工。
RI6 对每个拟启用 provider 分别检查其现场证据，未启用项可以保持关闭。

共同硬门：

- 每个阶段从当时最新 `main` 重新入职和取精确 SHA，不沿用本文 SHA 作为未来事实。
- 每个阶段默认关闭；缺配置、未知配置或权限不足均启动失败或调用失败。例外是 RI5 的 Provider
  配置：缺失或无效只标记该 Provider 不可用，不阻止 Web 启动、本地管理员登录和配置页访问。
  需要 readback 的后续部署阶段必须由自己的 ADR 和证据定义，不能假定 RI3 已提供。
- RI2 直接以既有 [M7 计划 §0.3.2](../../plans/M7-web-feishu-channels.md#032-真实渠道激活部署与-canary-门)
  为真实渠道授权真源；RI4 直接以既有 M6b 计划与 handoff 的延期现场条件为真源。分阶段计划只记录
  如何取得证据，不复制第二份授权清单。
- 真实调用前必须由项目负责人给出该阶段的明确 GO；计划批准不等于现场调用授权。
- RI3 的 Web parent 离线契约不依赖真实 OAuth。test-env 真实模型验证仍只通过 task-worker 发起；它
  不能证明飞书 reply/thread，后者等待 RI2 的真实事件语义。
- RI6 部署制品不等于允许生产联网。每个要在正式环境启用的 provider/只读目标还必须按 ADR-007
  H 层逐项批准；未批准项保持关闭，生产写仍不在范围内。
- RI5 的连接测试不取得任何真实调用许可：Gemini 探针仍属 ADR-007 B2 层，须 RI3 PR 3E 现场 GO；
  飞书两个探针仍属 F 层，须 RI2 现场 GO。RI5 的局域网 `lan_http` 只是本地体验模式，不适用于
  RI6 生产发布——生产仍只接受已批准 HTTPS SSO Host/Origin。
- RI6 的正式环境若经独立 GO 启用 Gemini，部署和普通镜像回滚都必须叠加
  `docker-compose.model.yml`；未取得 GO 时只用 base+production。紧急关闭 Gemini 改用
  base+production 并 `--force-recreate worker`，再证明 secret mount 已消失且后续调用数为 0。
- 用户未提交文件、真实 secret、生产数据和旧小维代码均不得被修改或复制进本仓库。
- 任一阶段失败均可只关闭本阶段开关，不要求回退整个系统。

## 12. 回滚

- 飞书：停止 2.0 的三个渠道进程；确认不再消费事件后恢复旧小维。禁止双跑。
- 模型：普通版本回滚若继续启用模型，使用 base+production+model 三文件组合；紧急关闭时改用
  base+production 强制重建 worker，确认 secret mount 消失且后续调用数为 0 后回到规则解释器。
- StarRocks：移除 target-bound 注册并恢复 recording；capability 和 E1 闸门不变。
- Admin：关闭局域网端口发布 override 与相关 Compose 开关并重建服务，回到 loopback + 默认关闭；
  本地管理员表与 Provider 状态表保留只读兼容，不需要回退数据库。首版没有配置版本回滚。
- 部署：Compose 使用上一镜像 digest 和上一配置版本重建；数据库迁移必须在各阶段计划中注明向前兼容或独立回退办法。

## 13. 分阶段计划索引

1. RI1：[飞书 OAuth 与 Web 启动](../plans/2026-09-10-feishu-oauth-web-activation.md)
2. RI2：[飞书测试环境真实验证](../plans/2026-09-10-feishu-test-environment-validation.md)
3. RI3：[真实模型供应商接入](../plans/2026-09-10-model-provider-adapter.md)
4. RI4：[StarRocks 只读真实接入](../plans/2026-09-10-starrocks-readonly-live-validation.md)
5. RI5：[本地 Web Admin 与第三方配置简化设计](../../plans/RI5-local-web-admin-simplified-design.md)
   （取代已废止的[最小 Web Admin 配置中心](../plans/2026-09-10-web-admin-config-center.md)）
6. RI6：[Compose 正式部署、canary 与用户验收](../plans/2026-09-10-compose-deployment-canary-uat.md)

## 14. 当前未决项

- RI3 的 provider/model/API、timeout、上下文与本地数据边界已经由项目负责人确认；仍未确认的是公司
  Gemini 项目的区域、保留/训练条款和真实现场 GO。未确认只阻止真实调用，不阻止计划审核和
  default-off 离线实现。
- RI4 的 StarRocks canonical resource ID、只读账号授权、TLS 材料、四类带外 digest、现场 actor 和激活窗口尚未提供；计划不读取这些值。
- RI6 的正式主机、canary 用户名单、观察窗口和验收人尚未指定；部署前再由负责人明确。
- ADR-007 从“生产连接与生产写均默认禁止”改为“生产只读可在 RI6 按 H 层逐项批准、生产写继续
  禁止”属于实质授权面变化，尚未得到项目负责人单独签认。未签认时只能部署 provider 全关闭的
  制品，或继续使用已授权 test-env 目标且不得宣称生产 StarRocks 能力。

这些未决项不阻止已批准总体设计和后续 default-off 离线实现，但会阻止对应真实调用或部署。
ADR-007 H 层的生产只读授权仍未签认；RI3 离线开工授权不允许读取真实 key 或发起 Gemini 网络调用。

## 15. 本轮复审问题的根因、影响与系统性修复

| 问题根因 | 实际影响 | 系统性修复与证明 |
| --- | --- | --- |
| Admin application 拟自行构造 plan/admission/Gateway | 产生第二条执行真源，并直接违反 application AST 硬门 | **RI5 修订后**：Admin 不再产生任何任务，风险机制被整体移除。连接测试是控制面探针，不创建 Task/Evidence、不构造 `PlanStep`/`ToolCall`/`AdmissionCertificate`、不进入 `ToolGateway`；`test_runtime_bypass.py` 反例保留并扩充到探针路径 |
| draft 可提交 host/port/任意 secret path | 配置管理员可借测试连接做 SSRF、端口探测或读取挂载文件 | **RI5 修订后**：配置面只暴露 Gemini/飞书两个闭集 Provider 的固定字段，不接受 host、port、endpoint、模型名或任意路径；provider/model/API version/endpoint 仍是代码内常量，探针目标不可由请求影响，未知或越界字段零网络调用 |
| 六阶段只存在于临时计划 | 总体里程碑、ADR 授权和实现计划会各说各话 | 先做纯文档 V2.4，把路线映射为 RI1–RI6，并同步修订 ADR-007；未批准前不实施 |
| RI2/RI4 复制已有真实调用清单 | 两份硬门会随时间漂移，执行者可能选择较弱版本 | RI2 直接引用 M7 §0.3.2；RI4 直接引用 M6b §3.3、ADR-012 与 handoff 未完成项，分计划只记录执行证据 |
| Treat host key material as an application setting | It would break the exact `.env.example` = `_FIELD_TO_ENV` contract and could leak into containers | Keep plaintext only in a Git-ignored host file; `.env.example` documents only actual `XIAOWEI_*` settings; merged-config tests prove the declaration/no-value boundary, while a split-fake Linux-container preflight separately proves worker-only mount and non-worker absence |
| Use a Compose environment-backed secret for the read-only worker | Rendered config advertises the secret, but actual container creation supplies no mount and startup rejects non-file secret sources for a read-only service | Use a file-backed secret; preserve the worker read-only rootfs and keep key material out of container environments |
| 为了入口形式统一而删除 `XiaoweiRuntime.handle()` | 它当前只被测试使用；删除会制造大范围测试迁移，却不增加真实模型安全性 | 保留该便利入口但不装配真实 provider；生产模型调用者静态限制为 worker durable path |
| 使用一个通用 `ModelProviderPort.generate(prompt, schema)` | application 可借任意 prompt/schema 外送字段，理解与诊断权限无法分开 | application 定义 intent/advisory 两个窄端口，Gemini adapter 分别实现；分层与方法等式测试禁止通用 generate |
| Send unbounded request/history text to the model | Redaction work becomes unbounded and pasted credentials may leave the process | Bound raw characters/UTF-8/history before total `scrub_text()`, then reserialize and enforce the final byte cap; omit whole oversized history rounds; invalid/oversized input yields zero model calls |
| 把全部 file credential hardening 塞进 RI3 | 会同时触碰飞书、StarRocks、PostgreSQL 和分层契约，把模型接入变成全项目安全重构 | Gemini 复用现有 `interfaces.secret_file`；共同 owner/mode/中间目录 hardening 由对应真实接入阶段单独修复并保留风险记录 |
| Normal ToolCall equals Policy maximum | The rejection boundary becomes unprovable | RI3 preserves the current query-timeout upper bound of 25 and read-only Policy ceiling of 30; RI4 separately introduces and tests 195/200 while other reads remain capped by their own profiles |
| Policy 语义变化但 revision 不变 | 旧 admission/approval 可能继续被视为有效 | RI4 强制递增 `POLICY_REVISION`，更新精确 fixture，并验证旧 certificate/approval 漂移拒绝 |
| 在 RI1 修改基础 Compose 为全网卡发布 | 提前扩大本地/离线攻击面，并混淆 HTTP 可达与 OAuth 安全 origin | base 始终保持 `127.0.0.1:8080`，任何阶段都不改基础文件；发布宿主端口只能由独立 override 承担——RI5 局域网 override 仅限本地体验且须在首次改密后启用，RI6 生产 override 才用于正式部署。正式 HTTPS 模式下直接 IP/伪 Host 不能完成 OAuth/session；`lan_http` 例外只接受显式配置的 loopback/RFC1918 origin，且不适用于 RI6 |
| 新 Protocol 实现只靠运行时 duck typing | `mypy src` 看不到实现与 Protocol 漂移 | 每个新生产实现同步加入 `src/xiaowei_agent/_conformance.py` 类型锚和 Protocol 方法/关键字参数测试 |
| Register a shared artifact suite or implementation incompletely | Tests can look present while one backend never runs them | Register model-artifact suite plus both memory/PostgreSQL bindings in `test_suite_bindings.py`; anchor model ports/store implementations in `_conformance.py` and runtime signature tests |
| Import `google.genai` outside the adapter seam | Application code could bypass the narrow data/permission boundary | AST-scan all production imports and dynamic imports; only `interfaces/gemini_model.py` may import the SDK |
| 把 `wait_for` 取消当成 PyMySQL/服务端已停止 | 超时后线程或服务端查询可能继续，占用连接与资源 | 迟到结果一律丢弃；现场记录线程/会话持续时间，依赖 driver/server timeout 收口；本轮不越权增加 `KILL` |
| 其他同步 provider 也可能使用 `to_thread` | OAuth/模型外层已超时后，线程或远端仍可能继续并产生迟到响应 | OAuth 与模型同样要求内层 deadline、取消/迟到反例和结果丢弃；不能把 coroutine 取消写成远端取消 |
| heartbeat 只覆盖 Runner | 60 秒意图或 180 秒诊断期间 lease 可过期，两个 worker 可能并行推进同一任务 | 提取 application helper；worker 包 execute/retry，兼容 `handle()` 包自己的 attempt，每条入口一份；Runner 不再启动第二份 |
| Add a 450-second whole-task deadline for model integration | It couples unrelated queue/query/model budgets without runtime SLO evidence | Use only 60/180-second model-stage budgets; preserve the current StarRocks query upper bound of 25 and Policy ceiling of 30, leaving future 180/190/195/200 to RI4 |
| 把 high thinking 误等同于 32768 output | 会无故升级 capability/snapshot、放大输出和迁移风险 | thinking 与输出分开；advisory 沿用 plan 的 4000 token，intent 固定 2048，不升级 slow-query capability |
| retry 重新调用非确定性模型 | 同一 task 在恢复后可能更换意图和计划 | 当前 fence 下先持久化唯一 accepted intent；retry 只读回并重跑确定性 Resolver/Planner |
| provider 返回后、本地保存前崩溃 | 恢复可能重复计费并得到不同草稿 | 明确接受无执行副作用的 at-least-once；保存成功后必须复用，保存前重复通过 usage 观测，不为此建设 reservation 平台 |
| Render before advisory or keep advisory only in memory | It creates two render points and inconsistent repeated/channel reads | Worker projects typed Evidence without rendering, saves advisory pre-terminal, and TaskView/`handle()` render exactly once from terminal facts |
| 为 advisory 新建全项目统一终态表 | 需要迁移所有终态和 Store 内部失败，回归面远超模型功能 | 保留现有终态 transition；只新增 accepted intent/advisory 两个模型事实，保存失败不得先标成功 |
| 为 Web 上下文顺带重写全部渠道提交事务 | 会把独立的 M7 半聚合债务和飞书语义绑进 RI3，扩大故障面 | 只加 scoped parent lookup 与 worker 二次核验；渠道原子性另立项，不作为首个模型闭环前置 |
| 按最近消息或同时猜 Web/飞书回复作为模型记忆 | 易串人、串环境或误把普通回复当 parent | 首版只支持 Web 显式终态 task id；逐跳校验作用域，20 轮/64,000 字符；飞书等待 RI2 真实事件语义 |
| Add nullable parent by dumping the whole object into every hash | Null parents drift old bytes; using full submission digest for conflict would reject normal retries because `as_of` changes | Preserve null-parent vectors; add non-null parent to semantic request and full stored submission digests only; conflict uses semantic request digest, while scope digest remains unchanged |
| 新 configuration-test worker 自行 import tools | 破坏“只有 local_stack 是工具装配根”的静态等式，最容易诱发修改安全测试白名单的 workaround | **RI5 修订后**：该 worker 不再存在，风险机制整体消失；`test_only_local_stack_can_import_the_tools_layer` 期望集合保持原样，不因 RI5 放宽 |
| 候选配置测试悄悄增加第二个完整执行进程 | 与 M7“只有 task-worker 装配完整 Runtime”冲突，且没有解释为何不能复用 active worker | **RI5 修订后**：不新增任何执行进程，因此 **ADR-016 不再需要**，M7“只有 task worker 装配完整执行 Runtime”的口径不被修改；结论写入 ADR-007 §RI5 Proposed Amendment R2 |
| 独立测试进程仍把候选任务放入普通 dispatch 池 | active task-worker 可能抢先用已发布配置执行，导致错误目标调用和伪造候选测试证据 | **RI5 修订后**：不存在候选任务，因此不新增 `configuration_test` dispatch lane、不改 TaskStore 窄方法、不改列表过滤与领取事务。配置生效改由宿主机重启 + `service_config_state` 加载回执承担 |
| 给幂等 scope payload 无条件加入 `lane` | 既有 USER digest 全部漂移，旧 key 会静默创建第二个任务 | **RI5 修订后**：不引入 lane，幂等 scope payload 完全不变，既有 USER canonical JSON 与向量原样保留 |
| 让独立 configuration-test worker 读取 Gemini key | 直接打破“Gemini secret 只挂 task-worker”的既定边界，并扩大 secret 暴露进程 | **RI5 修订后**：不再有 task-worker 的 Gemini probe control port。Web 读取 Key 仅用于配置管理与固定最小 synthetic 输入的 `gemini_connection` 探针；任务模型调用仍严格 worker-only，Web 不得取得模型端口。该窄例外由 ADR-007 §RI5 Proposed Amendment R1 承载，真实调用仍须 RI3 PR 3E 现场 GO |
| RI6 只叠 base+production 就声称启用模型，或紧急关闭时仍叠 model override | 前者会形成假启用证据；后者会让 key mount 留在重建后的 worker | runbook 分开“继续启用模型的三文件部署/版本回滚”和“移除模型的两文件紧急关闭”；后者 force-recreate 并反证 mount 消失、后续调用为 0 |
| 把生产连接禁令拆成 H 生产只读授权与 E2 生产写禁令 | 如果作为编号整理合并，可能在负责人未意识到时实质扩大生产网络权限 | ADR-007 增加单独签认框；未签认时 RI6 只能 provider 全关闭或沿用 test-env 目标，不能建立/宣称生产只读能力 |
