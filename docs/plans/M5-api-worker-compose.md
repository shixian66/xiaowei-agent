# M5 API / CLI / Worker / Docker Compose 实施计划 V5.4.1

> 状态：已按本计划实施，并于 2026-09-05 验收归档。本文件保存获批实施规范，不是当前进度真源；最终对象、验证证据与残余风险见 [M5 归档与验收报告](../handoff/archive/2026-09-05-M5-api-worker-compose.md)。
>
> 实施后对账：§1.1、§6 与 Task 13 中“本机无 Docker”“须实机确认”等文字是计划获批时的证据快照，不再代表当前缺口。合并后 CI run `33952529021` 已通过不可跳过的 Compose smoke：实际采用“先 `postgres`、再前台 `migrate`、后启动 `api`/`worker`”的退路序列，容器内 Python healthcheck 与 Worker `--force-recreate --no-deps`/环境复位均成功；本机仍未执行 Docker。完整运行证据只在归档中维护，不在本计划逐段复制。
>
> 执行要求：后续实施必须使用 TDD，逐项完成红灯、最小实现、边界回归和真实 diff 审查；不得把本计划当成实现、运行或验收证据。
>
> **V5.4.1 是对 V5.4 的定向勘误，不追加第二套设计。** V4–V5 的逐版变更说明已全部删除：它们保留在正文顶部时，会把已被撤回的旧指令（唯一约束、adapter barrier、TIMEOUT 压成 FAILED）重新变成可被实施照抄的文字，也让同一条规则在文档里存在两份表述。本文件始终只有**一份当前规范**；历史决定的根因与代价写在它们所属的章节里，不另开版本区。§11 保留 AF1–AF11 的根因记录，本次勘误直接修正承重正文与验收项，不再增加版本流水表。

## 0. 目标、范围与结论

M5 只把已经验证的 Runtime 与 PostgreSQL TaskStore 组装为一个本地可运行闭环：客户端通过薄 API 或标准库 CLI 提交任务，Worker 从同一 TaskStore 领取并使用**确定性无模型 interpreter**（`RuleBasedIntentInterpreter`，M3 起就是正式实现，不是 fake）与 **fake ToolGateway adapter** 执行，客户端再查询任务投影；Compose 提供 API、Worker、PostgreSQL 与一次性 migration，并证明任务提交、并发领取、幂等与 Worker 重启恢复。

本计划采用模块化单体：API 与 Worker 使用同一个应用镜像和同一套应用层装配，不引入消息总线、微服务、真实模型、真实运维系统或 E1 写能力。

关键设计结论：

1. `begin_task_attempt` 是 M5 执行路径唯一的租约获取点；Runner 只接收并续租 `TaskAttemptGrant`，绝不再次 `acquire_lease`。
2. `attempt_number`、`task_failure_count` 和进程级基础设施故障窗口是三套不同语义；不能用一个 `max_attempts` 同时惩罚任务错误、审批暂停和数据库抖动。
3. 步骤结果、Evidence 与审计事件在 TaskStore 的同一事务提交；**生命周期迁移与它的审计事件同样如此**。Runner 不直接调用 `EvidenceLedger.append`。
4. API/CLI 只负责协议、受信身份上下文和 `RenderPayload` 投影；Resolver、Planner、Policy、审批与状态判断仍只有应用层/Runner 真源。
5. Compose 中 migration 是独立的一次性服务，API/Worker 只在 migration 成功且 PostgreSQL 就绪后启动；readiness 不扫描任务、不执行业务能力。
6. 恢复不是"一个 resume"：`CREATED/PLANNING/RUNNING` 三个崩溃点各有确定的 Runner 入口与推进路径，且条件求值所需的失败上下文必须从持久化 step executions 重建，而不是靠进程内存。
7. Worker 读取 submission 不是一次独立查询：它随 `begin_task_attempt` 的成功结果在同一事务内返回，因此 TaskStore 的写端口不会为了"读一份提交事实"而长出第 15 个方法。
8. 组装根是 `interfaces/local_stack.py`，不是 `application/`：`application/**` 被既有 AST 护栏禁止以任何形式够到 `tools.gateway`，组装根按定义属于入口层。
9. **每个操作的结果空间是一张真值表，不是一组彼此独立的枚举。** 拒绝码属于哪个操作、每种拒绝下哪些字段有值，全部在计划里定死；纸面上成立、落库时无解的组合是本轮修掉的主要缺陷类型。

## 1. M4 前置证据与开工门

### 1.1 当前只读核对结果

- 当前分支：`main`。
- 当前 `HEAD`、本地 `main`、本地 `origin/main`：`fa6039bfbce680c0720606b2c34e556ce06c18f4`。
- M4 验收提交：`714df07e8064154d6b576376fac23f8f569ae480`，`git merge-base --is-ancestor` 返回 0，是当前 HEAD 的祖先。
- `AGENT_HANDOFF.md` 记录 M4 已验收归档；记录的 CI run 为 `33842205710`，七个 jobs，integration 为 `1442 passed`、0 skipped。**这是仓库 handoff 中的既有证据，本轮未联网复查 CI 页面**，不得表述为本轮运行验证。
- 当前没有 tracked diff；有两个与 M5 无关的 untracked 计划文档，实施时必须继续保留且不得暂存：
  - `docs/plans/development-route-v3-proposal.md`
  - `docs/plans/legacy-capability-migration-matrix.md`
- 本机 `PATH` 上没有 `docker`。因此本计划中**所有 Compose 行为都是静态推理**，没有一条经过运行验证；§6 与 Task 12 明确标出必须实机确认的项。

### 1.2 真正开工前必须再次执行

收到明确“开始 M5”后，不能依赖本计划中的 SHA 或摘要，必须重新：

```bash
cd /Users/kloenguyen/Desktop/agent
sed -n '1,9999p' AGENTS.md
sed -n '1,9999p' ARCHITECTURE.md
sed -n '1,9999p' AGENT_HANDOFF.md
sed -n '1,9999p' README.md
sed -n '1,9999p' DEVELOPMENT_PLAN.md
git status --short --branch
git rev-parse HEAD
git rev-parse main
git rev-parse origin/main
git merge-base --is-ancestor 714df07e8064154d6b576376fac23f8f569ae480 HEAD
git diff --check
```

只有满足下列条件才创建 `claude/m5-api-worker-compose`：M4 仍处于已验收状态；当前基线包含 M4 验收 SHA；tracked 工作区干净或已有变更已被确认与隔离；最新文档没有改变 M5 边界；**本 V5.4.1 计划已获最终确认**；用户已明确说“开始 M5”。任一条不满足，停在只读核对阶段并报告证据，不创建分支、不做兼容补丁。

## 2. 进程关系与请求/任务时序

### 2.1 进程关系

```text
stdlib CLI
    |
    | HTTP/JSON
    v
FastAPI gateway ─────── readiness ──────> PostgreSQL / migration head
    |
    | TaskStore.create_task(submission=...)   # context 在 submission 内，不传第二份
    v
PostgreSQL <──── dispatch/claim/checkpoint ──── Worker process
                                                    |
                                                    v
                                           XiaoweiRuntime.execute_task
                                                    |
                         deterministic Resolver → Planner → Runner
                                                    |
                                             fake ToolGateway
                                                    |
                                  atomic step + evidence + audit commit
                                                    v
                                                PostgreSQL

API、Worker：同一个应用镜像、不同启动命令、同一模块化单体
migrate：同一个应用镜像的一次性迁移命令
postgres：唯一持久状态服务
```

### 2.2 提交时序

1. API 的 ASGI body-limit middleware 在 Pydantic 前读取受限字节数；缺少 Content-Length、伪造较小 Content-Length 或使用分块流都不能绕过上限。
2. API 使用严格 DTO（`extra="forbid"`）解析用户文本和幂等键。tenant、environment、actor、channel、request_id、trace_id、policy_revision 不接受请求体伪造。
3. 本地开发鉴权适配器从服务端固定/受信配置生成 `RequestContext`；`policy_revision` 来自活动 `PolicySnapshot`。
4. 服务端生成 `request_id` 与 `trace_id`，固定 `Channel.API`；CLI 只是 HTTP 客户端，不创造第二种运行协议。
5. Runtime 构造不可变 `TaskSubmission`，保留原始 `as_of`、请求信封和执行上下文；`TaskStore.create_task(submission=...)` 在一个事务内持久化 task 与 submission。**只传 submission，不额外传 context**：context 已在 submission 内，传两份就凭空制造了「两者不一致时以谁为准」这个本不该存在的问题。
6. 幂等语义摘要 `request_dedup_digest` 包含 tenant、environment、actor、channel、text、idempotency_key；排除 `as_of`、request_id、trace_id 和一切时间戳。相同作用域/相同语义返回同一任务，不重复执行；同键不同语义返回冲突。
7. API 返回 `202 Accepted` 和任务查询投影，不同步执行 Runtime。

### 2.3 提交事实的两层校验与 `submission_digest`

**submission 的校验分两层，两层的判据不同，混为一谈会各错一半。**

**(a) scope 一致性——复用 `context_matches_envelope`，不是"三元组逐字相等"。** `persistence/decisions.py::context_matches_envelope` 定义得很清楚：tenant 与 actor 必须相等，而 **`envelope.environment_id` 可以为 `None`，省略即不约束**；只有给出时才要求等于 context。`RequestEnvelope.environment_id` 的类型本身就是 `StrictStr | None`。要求"逐字相等"会让每一个省略 environment 的合法请求在**首次领取**时被判成 `submission_invariant_violation` 并原子写成 FAILED——而这类请求在 M4 的共享套件里已有正例。

因此 M5 的 scope 校验是：

- `context_matches_envelope(submission.envelope, submission.context) is True`（**直接调用**，不在 M5 复制一份判据——复制出来的那份迟早与原件漂移，而这正是 M4 用 `decisions.py` 消灭的分叉）；
- task row 只与**解析后的 context** 比：`winner.tenant_id / environment_id / actor == submission.context.*`。

**(b) 内容未漂移——scope 一致挡不住同租户内的内容替换。** 三元组相等只能证明"没跨租户错配"，证明不了 `text`、`channel`、`as_of`、`trace_id`、`request_id`、`idempotency_key` 没有被换过。而 `request_dedup_digest` 按设计**刻意排除** `request_id`、`trace_id` 与一切时间戳（那是幂等语义所必需的，见 `persistence/store.py` 该函数 docstring），所以它也证明不了这件事。两个摘要**职责不同、不可互相替代**。

因此新增 `submission_digest`。**它的可执行写法必须精确到函数调用，因为最自然的那种写法在本仓库跑不通**——三条已在本机实测确认：

| 写法 | 实测结果 |
| --- | --- |
| `canonical_json(TaskSubmission)`（直接传契约实例） | `TypeError: value type is not canonicalisable`——`_normalise` 的可规范化类型是闭集（None/bool/int/float/str/Mapping/Sequence），Pydantic 模型不在其中 |
| `content_digest(canonical_json(...))`（把 bytes 交给它） | Pydantic `ValidationError`——`content_digest(content: str)` 先经 `FreeText` TypeAdapter 在 `strict=True` 下校验，bytes 被拒 |
| `content_digest(canonical_json(dump_contract(...)).decode("utf-8"))` | **成功**，返回 64 位十六进制 |

正确形状因此与 `request_dedup_digest` 逐字同源（它同样是 `content_digest(canonical_json(payload).decode("utf-8"))`）：

```python
def submission_digest(submission: TaskSubmission) -> str:
    """完整提交事实的一致性 checksum。"""
    payload = {
        "envelope": dump_contract(submission.envelope),
        "context": dump_contract(submission.context),
        "as_of": submission.as_of.isoformat(),
    }
    return content_digest(canonical_json(payload).decode("utf-8"))
```

（`dump_contract` 在 `persistence/rows.py`，走 `model_dump_json` → `json.loads`，产出的正是 `canonical_json` 可接受的纯 JSON 结构。`as_of` 不是 `RequestEnvelope` 的字段——实测 `RequestEnvelope.model_fields` 为 `request_id/tenant_id/actor/channel/text/idempotency_key/environment_id`，`trace_id` 在 `RequestContext` 上——因此它必须由 `TaskSubmission` 单独携带并单独进摘要。）

**它是一致性 checksum，不是抗攻击者的完整性证明。** 摘要无密钥且与数据同库同事务；能改写 submission 行的人同样能改写摘要列。它挡住的是**代码缺陷与并发错配**（写入 A 的行、读回 B 的内容；部分写入；行映射漂移），不是数据库层面的主动篡改。计划、ADR 与测试注释一律按这个强度表述，不得升格。

**两层校验分别落在哪里，是同一个问题的另一半。** 契约层的 `model_validator` **拿不到数据库事实**，因此它只检查 DTO 内部可见的关联；存储层在行锁内用**刚读到的那一行**独立复核：

| 校验 | 契约层 validator | 存储层（行锁内，用刚读到的行） |
| --- | --- | --- |
| `context_matches_envelope(envelope, context)` | 是 | 是 |
| `winner.tenant_id/environment_id/actor == context.*` | 是（`applied` 为真时三者同时在手） | 是 |
| `submission_digest(submission) == 行上的 submission_digest` | **否**（DTO 不携带存储行的摘要） | **是** |
| `request_dedup_digest(envelope, context) == 行上的 request_digest` | 否（同上） | **是** |

`create_task` 因此只接收 `TaskSubmission` 一个参数。反例覆盖：逐字段篡改 submission 行（每个字段各一条，摘要必须不匹配并按损坏处理）、篡改 `request_digest`、以及**幂等重试保留首次 submission**（同键同语义的第二次提交不覆盖第一次的提交事实，返回首个任务）。

### 2.4 Worker 调度与领取时序

1. Worker 用自身注入 Clock 构造窄 DTO：

```python
class DispatchQuery(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    limit: StrictInt = Field(gt=0)
    # 没有 now：时间只有一个真源，见下
```

**命令里不带 `now`，也不带失败上限——两者都已经有真源，让调用方再报一份就是第二真源。** `persistence/store.py` 的模块 docstring 第 4 条已经写死"时钟经 `Clock` 注入"，两个实现都持有它；`list_stale_leases` 的过期判定用的就是它。若 `DispatchQuery` / `TaskAttemptCommand` 各带一个 `now`，同一个事务里就有两个"现在"：SQL 的 `WHERE next_attempt_at <= :now` 用调用方那份，而 `lease_is_live` 用存储那份，两者相差一个网络往返——恰好落在 §2.5 那张 winner 形状表里 `RETRY_NOT_DUE` 与 `LIVE_LEASE` 的边界上，产生只在竞态下出现的错误分类。**每个事务只读一次存储的 Clock**，整个事务内共用那一个值。

同理，`task_failure_limit` 不进命令：它作为 **TaskStore 构造参数**注入（与 `Clock` 同一位置、同一形态）。让调用方自报会让一个写错的 Worker 把上限报成 999 绕过预算；而让 `persistence` 直接 import `config` 又会打红分层（`persistence` 的允许集不含顶层 `config`）。构造参数是唯一同时满足两条的形状。

一条反证：给 `DispatchQuery` 加回 `now` 并让 SQL 用它，构造一个调用方时钟快于存储时钟的用例，`RETRY_NOT_DUE` 的边界断言必须转红。

2. `TaskStore.list_dispatchable_tasks(*, query: DispatchQuery)` 只返回该 tenant/environment 内、重试时间已到、非终态、非 `AWAITING_APPROVAL` 且没有 live lease 的任务。
3. SQL 必须先完成全部过滤和稳定排序，再应用 `LIMIT`；内存实现使用相同判定函数。通过资格过滤后按 `(created_seq, task_id)` 排序，其中 `created_seq` 只提供确定性近似公平，不宣称严格提交 FIFO；`next_attempt_at` 只决定是否到期，不改变原任务顺序。
4. 每个候选仍只是提示；Worker 必须调用 `begin_task_attempt` 竞争最终 winner。
5. `begin_task_attempt` 原子检查状态、重试时间、失败预算和 live lease。成功时递增 fencing token 与执行尝试序号，返回 grant；失败时返回结构化 winner/rejection。
6. Worker 总是采纳返回的 `winner`。未取得 grant 的 Worker 不调用 Runtime/Runner，不写终态。
7. `begin_task_attempt` 成功时**在同一事务内**把 `TaskSubmission` 一并返回，Worker 不做第二次查询、也不私读数据库。执行上下文保留首次提交时由服务端生成的 request/trace identity，但 `policy_revision` 必须从当前活动 `PolicySnapshot` 重建。§2.3 的任一层校验不过按损坏 submission 处理（§4.3 第 7 步），不能在 Worker 中补兼容字段。

### 2.5 执行、心跳与恢复时序

`begin_task_attempt` 是唯一租约获取点，避免 Worker 先取得 token N、Runner 再重入取得 N+1，使当前 grant 立刻自我失效。

```python
class TaskAttemptGrant(Contract):
    lease: LeaseGrant                 # 复用 M4 既有形状，不重列四个字段
    attempt_number: StrictInt = Field(gt=0)

    @property
    def task_id(self) -> str: ...     # 委托 self.lease.task_id
    @property
    def fencing_token(self) -> int: ...


class TaskAttemptResult(Contract):
    applied: bool
    winner: TaskRecord                              # 永远返回，调用方必须采纳
    grant: TaskAttemptGrant | None = None
    submission: TaskSubmission | None = None
    rejection: TaskAttemptRejection | None = None
    committed_audit_events: tuple[TraceEvent, ...] = ()

    # 存在性（三条）：
    #   applied <=> grant is not None
    #   applied <=> rejection is None
    #   applied <=> submission is not None
    #   committed_audit_events 非空 <=> rejection 属于
    #       {RETRY_EXHAUSTED, SUBMISSION_INVARIANT_VIOLATION}
    #   committed_audit_events[*].task_id == winner.task_id
    #   committed_audit_events[*].attempt_number == winner.attempt_number
    #
    # 跨对象关联（applied 为真时全部必须成立，全部在 DTO 内可判定）：
    #   grant.lease.task_id       == winner.task_id
    #   grant.lease.owner         == winner.lease_owner
    #   grant.lease.fencing_token == winner.fencing_token
    #   grant.lease.expires_at    == winner.lease_expires_at
    #   grant.attempt_number      == winner.attempt_number
    #   context_matches_envelope(submission.envelope, submission.context) is True
    #   winner.tenant_id / environment_id / actor == submission.context.*
    #
    # 两个 digest 的比对不在这里——DTO 不携带存储行的摘要（§2.3 表）。
```

`TaskAttemptGrant` 组合既有 `LeaseGrant` 而不是重列 task_id / owner / expires_at / fencing_token：租约形状在 M4 已定死并有 CHECK 约束与契约 validator 承重，重列一遍就是第二份表述，两份迟早漂移。`attempt_number` 是真正不属于租约的那一项，只有它单独声明。

**为什么存在性不够。** 三条 presence XOR 只证明"三个字段都在"，证明不了"它们说的是同一个任务"。一个错误实现完全可以返回任务 A 的 submission 配任务 B 的 grant，或返回一个 token 与 winner 当前租约不一致的 grant——前者会让 Worker 用别的租户的请求文本去执行，后者会让所有后续 fenced 写入静默失败。关联校验因此**两层独立表达**：契约层 validator（构造非法组合即失败）+ 存储层在行锁内用刚读到的那一行复核（不是用调用方递来的值自比，那是空洞检查）。`tests/security/test_vacuous_check_ban.py` 已经为 CAS 的两侧异源立过同样的规矩，这里沿用。

`submission` 由 `begin_task_attempt` 在**领取任务的同一个事务里**读出并返回，理由有三条，缺一条这个字段就该退回成独立方法：

1. **不制造第 15 个写端口方法。** §4.1 把 TaskStore 钉死为 14 个方法，由 `tests/contract/test_protocol_conformance.py` 的精确计数哨兵承重。哨兵的意义是"写端口又长大了一块必须有人看到"——让一次读取把它撑到 15，会让这个信号第一次就贬值。
2. **消除"领到了任务但读不到提交事实"的窗口。** 分两次调用时，两者之间任务可能被抢占、submission 行可能在另一事务中不可见。
3. **强制归属校验有对照物。** `applied` 为真时 `winner`、`grant`、`submission` 三者同时在手，Worker 才能在执行任何东西之前完成 §2.3 的比对。

`submission` 不进入 `TaskRecord`，也不写进 `TraceEvent.detail`：它含用户原文，只在进程内作为执行输入使用。

`committed_audit_events` 解决的是另一条归属链：`RETRY_EXHAUSTED` 与 `SUBMISSION_INVARIANT_VIOLATION` 会在 `begin_task_attempt` 内部直接写终态和审计，此时调用方没有 grant，也没有可传入的事件缓冲。存储层必须把**本次事务实际写入的那组事件原样返回**；其它四种非变更型 rejection 与成功领取都返回空元组。Worker 收到非空值后只以 `COMMAND_COMMITTED` 交给 sink 补结构化日志，绝不再 durable 写一次。这样正常返回路径既能让内部终态事件过 sink，又能保持 begin 事务原子；确认丢失时的例外与代价在 §5.1 / §13 单列，不靠调用方猜出一份第二事件。

**领取有两种意图，把它们压成一种会让审批恢复变成死路。** 上一版同时写下三条互斥的规则：`begin_task_attempt` 是唯一租约获取点；它拒绝 `AWAITING_APPROVAL`；Runner 不得自行 `acquire_lease`。三条同时成立时，一个已暂停的任务**没有任何合法途径拿到 grant**——而 `resume()` 的 `AWAITING_APPROVAL -> RUNNING` 边、以及 `tests/security/test_runner_lifecycle.py` 里那组承重用例（`test_resume_refuses_on_any_drift_with_zero_gateway_calls`、`test_resume_without_an_approval_pauses_again_rather_than_executing`、`test_resume_recomputes_both_fingerprints`）全都建立在"暂停的任务可以被恢复"之上。M5 不消费审批，不等于 M5 可以把已验收的恢复契约拆掉。

真正需要分开的是**两件不同的事**：「Worker 轮询时会不会自动挑中它」和「它能不能被显式恢复」。前者必须排除 `AWAITING_APPROVAL`（没有审批消费方，自动领取只会立刻再暂停一次，白白消耗 attempt 与租约周期）；后者必须保留。因此领取意图成为命令的一部分：

```python
class AttemptIntent(StrEnum):
    DISPATCH = "dispatch"                  # Worker 轮询；只接受 §2.6 表内的三个状态
    APPROVAL_RESUME = "approval_resume"    # 显式恢复；只接受 AWAITING_APPROVAL


class TaskAttemptCommand(Contract):
    task_id: StrictStr
    intent: AttemptIntent
    owner: StrictStr                # 含每次进程启动生成的随机 nonce
    ttl_seconds: StrictInt = Field(gt=0)
    trace_id: TraceId               # 无 grant 终态化时的审计归属来源，见 §4.3
    # 没有 now、没有 failure limit：都由 TaskStore 侧提供，理由见 §2.4
```

两种意图的可领取状态是**不相交**的闭集，因此不存在"某个状态两种意图都能领"的歧义：

| intent | 可领取状态 | 谁调用 |
| --- | --- | --- |
| `DISPATCH` | `CREATED` / `PLANNING` / `RUNNING` | `WorkerLoop`（候选来自 `list_dispatchable_tasks`） |
| `APPROVAL_RESUME` | 仅 `AWAITING_APPROVAL` | **M5 内只有 Runner 契约测试**；M6+ 的审批消费端接同一个口 |

`list_dispatchable_tasks` 继续排除 `AWAITING_APPROVAL`，一个字不改——它回答的是"Worker 该去看哪些"，与"能不能被恢复"无关。

**暂停任务的租约通常已经过期，恢复必须能在这种状态下领取。** `WorkflowPaused` 按 §3.5 会停止 heartbeat，因此租约在 TTL 之后自然到期。`APPROVAL_RESUME` 走的是与 `DISPATCH` 完全相同的 live-lease 检查（有 live lease 才返回 `LIVE_LEASE`），过期的租约不构成阻碍，新 grant 拿到更高 token。**必须有一条"暂停后租约已过期再恢复"的用例**，否则这条路径只在租约仍 live 的短窗口里被测过，而那恰恰是它会失败的那一种。

**调用方必须说准：`handle()` 不是它的调用方。** 上一版写"M5 内只有 `handle()` 与测试"，核对后是错的——`handle()` 的签名里既没有 `approval` 也没有 `ExternalInput`，它从头到尾不调用 `resume()`；实测 `src/` 下**没有任何** `.resume(` 调用点。也就是说 `AWAITING_APPROVAL` 在 M4 就已经是"生产不可达、仅由 `tests/security/test_runner_lifecycle.py` 等契约用例驱动"的状态。

**那为什么不按"删除无调用方分支"的规则把 `APPROVAL_RESUME` 也删掉？** 因为它与 `RetryClass.INFRASTRUCTURE`、`RetryReason` 的多余成员不是一回事，判据在于**这条分支是新造的未来功能，还是既有已验收契约的唯一入口**：

- `RetryClass.INFRASTRUCTURE` 是我在 M5 计划里新写的、从未有过调用方的分支 → 删。
- `resume()` 与 ApprovalGate 是 M3/M4 **已验收**的契约，有一组承重用例（漂移拒绝、无审批重新暂停、指纹重算）。M5 把 Runner 改成只收 grant 之后，若不给 `AWAITING_APPROVAL` 留一个领取入口，这组用例就**无法构造 grant**，等于用一次重构静默废掉一个已验收能力。

所以留，并且如实标注它在 M5 只有测试调用方。这条差异写进 ADR-010，避免下一轮又按同一条规则把它删掉。

取得 grant 之后 ApprovalGate 的既有校验一律不变：重算 plan / target 指纹、校验 policy revision、绑定审批 ref、无审批则重新暂停。恢复拿到的是执行权，不是免检权。

`TaskAttemptRejection` 是闭集：`LIVE_LEASE`、`NOT_DISPATCHABLE`、`RETRY_NOT_DUE`、`TERMINAL_PROTECTED`、`RETRY_EXHAUSTED`、`SUBMISSION_INVARIANT_VIOLATION`。**任何一种拒绝都不携带 submission**（否则未取得任务的 Worker 也拿到了用户原文）。`NOT_DISPATCHABLE` 现在的含义是"当前状态不属于本次 intent 的可领取集合"——用 `APPROVAL_RESUME` 去领一个 `RUNNING` 任务同样得到它。

六种拒绝下 `winner` 的形状也是闭表，不能只说"永远返回 winner"就算数：

| rejection | `winner.status` | 租约字段 | `next_attempt_at` |
| --- | --- | --- | --- |
| `LIVE_LEASE` | 非终态 | 三个字段同时有值且 `lease_expires_at > now` | **`None` 或 `<= now`**（否则先撞 `RETRY_NOT_DUE`） |
| `NOT_DISPATCHABLE` | 不在本 intent 集合内的任一状态 | 任意 | 任意 |
| `RETRY_NOT_DUE` | 非终态 | **三个字段仍完整，且 `lease_expires_at == next_attempt_at`** | **必非 `None` 且 `> now`** |
| `TERMINAL_PROTECTED` | 五个终态之一 | 任意（终态不清租约） | 任意 |
| `RETRY_EXHAUSTED` | 恰为 `FAILED` | 不分配新租约 | 任意 |
| `SUBMISSION_INVARIANT_VIOLATION` | 恰为 `FAILED` | 不分配新租约 | 任意 |

**`RETRY_NOT_DUE` 那一行上一版写的是"无 live lease"，而那与 `schedule_retry` 的设计直接冲突。** §4.4 明确把 `lease_expires_at` **精确移动到未来的 `next_attempt_at`**（那是"计划交接可领取边界"的表达方式，也是三个租约字段同置的 CHECK 约束所要求的）。于是在到期之前，这个租约按 `lease_is_live` 的定义**仍然是 live 的**——"无 live lease"这个形状根本不可能出现。照它写断言，每一条正常的重试等待期用例都会失败。

正确形状是三个租约字段仍完整、且 `lease_expires_at == next_attempt_at > now`。这也解释了 §4.3 的判定顺序为什么是**先第 4 步（retry 时间）后第 5 步（live lease）**：顺序反过来的话，等待重试的任务会返回 `LIVE_LEASE`——一个语义上误导的诊断（调用方会以为"别人在跑"，而实际是"还没到时间"）。这条顺序因此是承重的，一条用例断言等待期内拿到的恰是 `RETRY_NOT_DUE`，另一条**反证**把两步对调后它必须转红。

`LIVE_LEASE` 那一行同样要收紧：一个既有 live lease 又有未来 `next_attempt_at` 的任务先撞第 4 步，所以能走到 `LIVE_LEASE` 的 winner 其 `next_attempt_at` 必然是 `None` 或已过期。上一版写"任意"会让这两行的判据重叠而无法逐行断言。

边界用例三条：`now < next_attempt_at` → `RETRY_NOT_DUE`；`now == next_attempt_at` → **可领取**（与 `lease_is_live` 的严格 `>` 一致，到期即不再 live）；`now > next_attempt_at` → 可领取。

后两行（`RETRY_EXHAUSTED` / `SUBMISSION_INVARIANT_VIOLATION`）的 `winner` 是**本次调用刚写入的那一行**（version 已 +1、`terminal_reason` 已是对应闭集码），不是调用前读到的那份；一条用例逐字段断言这件事，否则"原子终态化"在返回值上看不出来。

Runner 契约修改为显式接收 grant：

```python
async def start(
    self,
    grant: TaskAttemptGrant,
    *,
    plan: ExecutionPlan,
    target: ResolvedTarget,
    context: RequestContext,
) -> TaskOutcome: ...

async def resume(
    self,
    grant: TaskAttemptGrant,
    external_input: ExternalInput | None = None,
    *,
    plan: ExecutionPlan,
    context: RequestContext,
    target: ResolvedTarget,
    approval: ApprovalRequest | None = None,
) -> TaskOutcome: ...
```

Runner 进入后先用相同 owner/token 续租验证 grant，再启动 heartbeat；不得调用 `acquire_lease`。`resume()` 的 `plan` 是从持久化 submission 当下重算出的活对照物，只用于与 stored plan 比较 `plan_hash`；验证通过后仍执行 stored plan。现有 `acquire_lease` 保留为 M4 存储原语和测试面，不再是 M5 执行调用链的一部分。

heartbeat 使用注入的异步 sleep callable 与结构化并发；任何续租失败都取消执行分支并进入 lease-loser 路径，旧持有者不得提交步骤、证据、审计或终态。

Worker 进程 owner 必须含每次启动生成的随机 nonce，不能来自静态配置。Worker 重启后，旧租约在到期前阻止新 owner；到期后新 Worker 取得更高 fencing token 并从持久化 submission/plan/step executions 恢复。

### 2.6 恢复分派闭集

"恢复"不是一个动作。当前 Runner 的两个入口各自只覆盖一段：`start()` 无条件依次推进 `PLANNING`、`RUNNING`；`resume()` 只在状态为 `AWAITING_APPROVAL` 时推进一次到 `RUNNING`。因此一个崩在 `PLANNING` 的任务，用 `start()` 会撞 `PLANNING -> PLANNING` 非法迁移，用 `resume()` 则会带着 `PLANNING` 状态跑完步骤、最后卡在 `PLANNING -> SUCCEEDED` 同样非法。三个可达崩溃点必须各有确定入口，且这个映射是**闭集**——不在表内的状态一律不领取。

`DISPATCH` 意图只领取 `CREATED`、`PLANNING`、`RUNNING` 三种状态；`AWAITING_APPROVAL` 只经 `APPROVAL_RESUME` 领取（§2.5）；其余状态两种意图都拒绝为 `NOT_DISPATCHABLE`。领取后的分派：

| winner.status | 是否已有 stored plan | Runner 入口 | 需要推进的状态边 |
| --- | --- | --- | --- |
| `CREATED` | 否 | `start(grant=...)` | `CREATED -> PLANNING -> RUNNING` |
| `CREATED` | 是（崩在 plan 已存、状态未推进之间） | `start(grant=...)` | 同上；`PlanStore.save` 内容一致即幂等，不一致为 `PlanConflictError` |
| `PLANNING` | 是 | `resume(grant=...)` | `PLANNING -> RUNNING` |
| `RUNNING` | 是 | `resume(grant=...)` | 无（已在 RUNNING） |
| `AWAITING_APPROVAL` | 是 | `resume(grant=..., approval=...)`，**grant 经 `APPROVAL_RESUME` 领取** | `AWAITING_APPROVAL -> RUNNING`（ApprovalGate 校验通过后） |
| 终态 | —— | 两种 intent 都不领取 | —— |

因此 `resume()` 的推进逻辑从"只处理 `AWAITING_APPROVAL`"改为**按当前状态查同一张闭表**：`PLANNING -> RUNNING`、`AWAITING_APPROVAL -> RUNNING`、`RUNNING -> 无操作`，其余状态直接 `LifecycleError`。这条推进表与 `ALLOWED_TRANSITIONS` 是两份表述，必须由测试交叉校验，不允许在 Worker 里写兼容分支绕过。

**恢复必须重建失败上下文，不能只重建计划。** `_condition_holds` 目前从 `_run_steps` 的进程内 `failed_steps` 集合判断被引用步骤是否失败；这个集合在重启后是空的。届时 `EVIDENCE_ROW_COUNT_BELOW` 会退回去读 ledger，而一个失败步骤留下的是零行证据——于是"取数失败"被读成"确认了范围内没有流量"，条件成立、后续步骤照跑。这正是 `_condition_holds` docstring 点名要避免的、"自信但错误"的结论，而且它**只在恢复路径上发生**，单进程用例永远看不到。

**`_run_steps` 有三份进程内状态，不是一份；只重建其中一份会换来一个更坏的缺陷。** 逐行核对 `runners/deterministic.py::_run_steps` 的局部变量，恰好三个：`tool_calls_used`（346 行）、`degraded`（347 行）、`failed_steps`（349 行）。上一版只说了 `failed_steps`，而 `degraded` 是**决定最终状态**的那一个：453 行 `status = TaskStatus.INDETERMINATE if degraded else TaskStatus.SUCCEEDED`，它在 418 / 440 行被置位。

后果比 `failed_steps` 那条严重：假设 s1 已提交 `FAILED`、s2 因条件 fail-closed 被正确跳过——只重建 `failed_steps` 时，s2 确实没跑、Gateway 调用次数确实是 0，**但循环走完之后 `degraded` 仍是 `False`，任务被写成 `SUCCEEDED`**。一次取数失败在重启之后变成一个干净的成功答案，而所有"后续步骤没有执行"的断言全部通过。这正是只断言"Gateway 调用为 0"会漏掉的那一类。

因此 `resume()` 在进入步骤循环前必须调用 `load_step_executions(task_id=...)`，用持久化的 `StepResultStatus` 重建**全部三份**：

| 进程内状态 | 重建来源 | 漏掉它的后果 |
| --- | --- | --- |
| `failed_steps` | 结果为 `FAILED` / `TIMEOUT` 的行的 `step_id` | 引用它的条件退化成"取到零行"，一次取数故障被读成"确认没有流量" |
| `degraded` | `any(row.result_status in {FAILED, TIMEOUT} for row in rows)` | **最终状态从 `INDETERMINATE` 变成 `SUCCEEDED`** |
| `tool_calls_used` | **删除这个局部变量**，改用 `StepAttemptResult.attempts_used` | 重启后计数归零，计划预算在进程内被重新从 0 计一遍，而存储层算的是另一份 |

第三行不是"也重建一下"，而是**删掉它**：§4.2.1 已经把预算判定移进 `begin_step_attempt`（由存储层从不可变的 stored plan 派生），进程内再留一个计数器就是第二份真源，两份在重启后必然分叉。Runner 只读 `attempts_used`，不自己加。`_run_steps` 里 356 行那条 `if tool_calls_used >= plan.budget.max_tool_calls` 的提前返回随之删除——`BUDGET_EXHAUSTED` 由 begin 的 decision 表达（§4.2.1 第 5 行），一处判定。

逐步骤的跳过规则：

- 已 `OK` 提交的步骤：跳过，不重复调用工具（由 `begin_step_attempt` 返回 `ALREADY_COMMITTED` 兜底，两层独立表达）；
- 已 `FAILED` 或 `TIMEOUT` 提交的步骤：同样跳过、放回 `failed_steps`、**并置 `degraded = True`**。

实现允许两条等价防线同时存在：循环前从完整 journal 快照预加载 `failed_steps/degraded`，以及逐步收到 `ALREADY_COMMITTED` 时再次采纳同一条不可变记录。在合法计划顺序与不可变 journal 下，删掉任意一条后外部行为仍然正确，因此不能写一个只为锁住私有结构而失败的测试；证明责任是**同时删掉两条等价防线时**，下述恢复行为测试必须转红。若以后删除其中一条冗余实现，剩余路径自然成为唯一承重点，不需要改外部契约。

`ToolCallStatus` 到 `StepResultStatus` 的映射是闭表，不允许在实现里合并：

| gateway 返回 | step result | Evidence |
| --- | --- | --- |
| `ToolCallStatus.OK` | `StepResultStatus.OK` | 有 |
| `ToolCallStatus.TIMEOUT` | `StepResultStatus.TIMEOUT` | 有（零行 + limitations） |
| `ToolCallStatus.ERROR` | `StepResultStatus.FAILED` | 有（零行 + 结构化 error） |
| `ToolCallStatus.INDETERMINATE` | `StepResultStatus.FAILED` | 有 |
| `MalformedAdapterResponseError`（adapter 返回非 `AdapterResponse`） | `StepResultStatus.FAILED` | **无** |

**journal 是超时事实唯一的持久化落点。** `EvidenceEnvelope` 没有 status/error 字段（它只有 facts / source / sampled / limitations 等），超时与工具错误在证据里长得完全一样——都是"零行 + 一段确定性 limitations"。区分只存在于 `ToolResult.status`，而 `ToolResult` 不持久化。因此如果 journal 把 `TIMEOUT` 压成 `FAILED`，这条结构化事实在进程结束的那一刻就永久丢失，事后从数据库**无法**重建。`DeterministicToolGateway.invoke` 对 `asyncio.wait_for` 的超时**已经**返回独立的 `ToolCallStatus.TIMEOUT`（带 `adapter timed out` 的 limitations 与 `AdapterStatus.TIMEOUT` 的结构化 error），fake adapter 完全能制造出可区分的超时。这是必须产生 `StepResultStatus.TIMEOUT` 的实质理由，不是"枚举有这个值所以要用"。

**步骤结果一次提交即终局。** M5 不在任务内重试单个步骤——现有 `_run_steps` 在一次运行内也不重试失败步骤（它只把 step_id 记入 `failed_steps` 然后继续），恢复路径必须与之一致，否则"重启"会变成一条隐蔽的重试通道，让同一个 fake 工具被调用两次而计划级预算只记一次。因此 `ALREADY_COMMITTED` 覆盖 `OK`、`FAILED`、`TIMEOUT` 三种已提交终局。

**`SKIPPED` 不持久化。** 它是派生量：`_condition_holds` 判定一个步骤是否执行只需要两样东西——被引用步骤是否在 `failed_steps` 里，以及 ledger 里该步骤的证据行数。两者都已持久化且不可变，因此"这一步当初被跳过了"可以在恢复时重新求值，且结果必然与首次一致（同一份 evidence、同一份 failed_steps、同一个闭集条件求值器）。持久化它反而引入真实缺陷：写一条 `SKIPPED` 行必须先 `begin_step_attempt`，而那是计划级工具预算的扣减点（§2.7），于是一个**从未调用任何工具**的跳过步骤会吃掉一次 `max_tool_calls` 配额；不 begin 又无法 commit。

对价写明：`StepExecutionRecord` 因此只记录"曾经开始过工具调用"的步骤；`load_step_executions` 返回的行数**不等于**计划步骤数，缺行意味着"该步骤当初未执行"，而不是"数据丢了"。这条语义由一条专门的用例承重。

`start()` 不需要重建（它的前提就是没有任何已提交步骤），但必须断言 `load_step_executions` 返回空——非空说明状态与 journal 矛盾，属 §3.5 的不变量破坏桶。

### 2.7 步骤提交时序

Runner 不直接 `EvidenceLedger.append`。每个步骤经四个边界，顺序固定：

1. **StepAdmission 先跑**（六段准入，纯同步、无 I/O）。它在 `begin_step_attempt` **之前**：准入拒绝的步骤根本没有工具调用，不该留下 in-flight 行，更不该吃掉计划级预算——否则一次确定性的策略拒绝会在每次任务重试时都消耗一次工具配额。
2. `begin_step_attempt` 在当前 task grant 下检查 fencing、任务状态、step 归属、幂等状态与计划级工具预算，并写入一行 `result_status is None` 的 in-flight 记录。**它是预算扣减点与 step 准入点**，位置固定为"准入已过、`gateway.invoke` 之前"。
3. ToolGateway 执行已准入的调用；M5 只装配 fake adapter。
4. `commit_step_result` 在一个事务中写 step execution、Evidence 与该 attempt 的 audit events，然后更新可恢复 checkpoint；任何一部分失败都整体回滚。

**原子提交有两种形状，不是一种。** "step + Evidence + audit 三写全有或全无"对一条真实路径不可实现：gateway 在 adapter 返回值不是 `AdapterResponse` 时抛异常，此时**根本没有 `ToolResult`**，而 `build_evidence` 的入参就是 `ToolResult`。现有 Runner 对这条路径的处理是"记为失败、不写证据、继续下一步"（它明确不让这个异常冒泡，因为那会把一次可降级的取数失败变成没有证据也没有终态的崩溃）。因此：

| 形状 | 触发条件 | 同事务写入 |
| --- | --- | --- |
| A. 有 `ToolResult` | gateway 正常返回（OK / TIMEOUT / ERROR / INDETERMINATE） | step execution + Evidence + 该 attempt 的 audit events |
| B. 无 `ToolResult` | `MalformedAdapterResponseError` | step execution（`FAILED`）+ audit events；**Evidence 必须为空** |

形状 B 不是"缺了一块的 A"，它是一个完整的终局：步骤有确定结果、有审计、没有证据——正是 ARCHITECTURE 说的"被拒绝/失败的调用不产生证据但必须留下审计"。因此 `StepCommitCommand.evidence` 可空的真正理由是形状 B，不是 SKIPPED（SKIPPED 按 §2.6 根本不提交）。形状 B 同样是终局，因此也进 `ALREADY_COMMITTED`：malformed 之后重启，该步骤不重跑，且它留在 `failed_steps` 里让后续条件继续 fail-closed。

`max_tool_calls` 是整个 `ExecutionPlan` 的预算，在 `begin_step_attempt` 处按任务所有 step rows 的尝试总数判定，不按单步分别限制。**预算只在 begin 处检查一次，commit 处不再检查**——理由见 §4.2。

### 2.8 查询时序与投影

1. `GET /v1/tasks/{task_id}` 必须携带服务端受信的 tenant/environment scope 调用应用层查询。
2. 查询不能先按裸 task_id 取回再在 API 层判断；既有 `TaskStore.get` 收窄为 `get(*, lookup: TaskLookup)`，其中 `TaskLookup` 只含 task_id、tenant_id、environment_id。内存与 PostgreSQL 查询本身必须受 scope 约束，防止 task-id 枚举泄漏；内部事务读使用 adapter 私有 helper，不能公开无 scope 读取逃生口。
3. Runtime 统一把 TaskRecord、Evidence 与状态投影为响应。API/CLI 不自行判断 pending/terminal，也不复制 rendering 文案。

**"`GET` 纯读"与现有终态投影路径冲突，必须先把投影拆成纯函数。** `application/runtime.py::_render_recorded` 在返回投影之前会 `self._emit(stage=PipelineStage.RENDERING, ..., task_id=record.task_id)`——一条**带 `task_id`** 的事件。按 §5.1 的路由，带 `task_id` 且不属于任何命令缓冲的事件走 `LOG_AND_DURABLE`，于是每一次 `GET` 都会往 `task_audit_events` 里写一行。§3.1 却要求 `GET` 不写 audit、不写 evidence、不改 `version`。两者不能同时成立，而且这个冲突只有在断言 **audit 行数**时才会暴露——只比 `version` 的用例会全绿放行。

修法是把投影拆成两层：

- `project_terminal(record, evidences, verdict) -> RenderPayload`：**纯函数**，无 I/O、无 emit、无副作用。`query_task` 只调它。
- `_render_recorded`：保留现有形状（读 ledger → `assess` → emit RENDERING → 调 `project_terminal`）。它是 `handle()` 的请求处理路径，那条 RENDERING 事件是一次真实的请求阶段，本来就该被审计，一个字不改。

`query_task` 因此完全不产生 task-scoped 事件。**用例必须同时比较三样**：调用前后的 `version`、该任务的 audit 行数、evidence 行数——只比 `version` 是这条缺陷能一路绿到最后的原因。
4. 承载它的 `TaskView` 是严格契约，形状固定：

```python
class TaskView(Contract):
    task_id: StrictStr
    status: TaskStatus
    render: RenderPayload | None = None   # 见下方规则
    query_path: StrictStr                 # 相对路径，如 /v1/tasks/<id>

    # render is None  <=>  status is not terminal
```

**`AWAITING_APPROVAL` 在 M5 的异步查询路径上也返回 `render = null`——这是一个显式收窄，不是遗漏。** 原本写的是"`AWAITING_APPROVAL` 使用现有 `render_pending`"，但那条不可实现：`rendering/slow_query.py::render_pending(*, approval_ref, evidences)` 需要 `approval_ref`，而 TaskStore 只有 `record_approval` 这个**写**方法，没有任何读取审批记录的端口；M5 又明确不增加审批消费端（§12），因此"当前 pending 的是哪一条审批"在查询时无法可靠重建。可选的另一条路是新增一个 approval 读取方法（把 14 撑到 15，并要求 M5 定义"哪一条是当前 pending"的语义），代价与收益不成比例——M5 根本没有人去消费这个 render。

因此规则简化为一句可机械判定的话：**`render` 非空当且仅当任务处于终态**。`handle()` 的同步路径不受影响（它手里有 `ApprovalRequest`，继续调用 `render_pending`），两条路径共享的是**终态投影函数**，pending 投影仍只属于同步路径。代价写进 ADR-010 与残余风险，并由一条用例把它固定成已知行为：`AWAITING_APPROVAL` 的 `GET` 返回 `status="awaiting_approval"` 且 `render` 为 `null`。

`TaskView` **不含** `TaskRecord`、`terminal_reason`、`version`、租约字段、`attempt_number`、`task_failure_count`、Evidence 行或 plan——这些是内部任务事实，接口层没有它们的消费权。`query_path` 是**相对路径**，不由 `Host` / `X-Forwarded-Host` 拼绝对 URL：那是一条现成的 host-header 注入面，而客户端本来就知道自己连的是谁。

四种响应各有精确 JSON 形状并由契约用例钉死：`POST` 首次、`POST` 重复（同键同语义，返回同一 `task_id`，仍是 `202`）、非终态 `GET`（`render` 为 `null`）、终态 `GET`。

**`extra="forbid"` 只证明形状，证明不了"返回的是我请求的那个任务"。** 一个把 `task_id` 取错（读了别人的行、或返回了列表里的第一条）的服务端 bug，其响应在结构上**完全合法**，严格 DTO 一个字都挑不出。这不是理论洁癖：§2.8 刚把 `get` 收窄成带 scope 的查询，正是因为按裸 task_id 取回是可以拿到别人的行的；形状校验挡不住那条路径的回归。因此补三条**关联**不变量，每条都是可判定的等式：

1. `render is not None ⇒ render.status == status`（`RenderPayload` 有自己的 `status` 字段，两处不一致时投影与记录已经分叉；由 `TaskView` 的 `model_validator` 承重）；
2. `query_path == "/v1/tasks/" + quote(task_id, safe="")`——**确定性函数，不是自由字符串**。用 `quote(..., safe="")` 而不是裸拼接：`safe=""` 会把 `/` 也编码掉，因此一个含 `/` 的 task_id 不可能把响应里的路径撑成两段（那正好是路径遍历与"看起来指向别的资源"的形状）。由 validator 用同一个函数重算并比对，与 §4.2.1 的 `evidence_id` 同一手法：**重算比对，不另拼一份**；
3. CLI 侧：`xiaowei task get X` 拿到响应后断言 `body.task_id == X`，不等即退出 2（协议错误）。这是客户端唯一能自查的一条，成本一行。

反例逐条：在 `TaskView` 纯契约测试中用含 `/`、`?`、`#`、`%` 的 task_id 断言 `query_path` 使用百分号编码；在 CLI transport spy 上断言**实际发出的 URL**由 `quote(task_id, safe="")` 生成。生产 task_id 是服务端 UUID，因此不要求含斜杠的伪造 id 能成功路由；真实 ASGI 用例只要求该请求保持编码且 fail-closed 为闭集 404，不能穿成另一条资源路径。另构造一个**结构完全合法但 `task_id` 是别人的**响应喂给 CLI → 必须退出 2；构造 `render.status != status` → `TaskView` 构造即失败。

## 3. API、CLI、Worker 的薄边界

### 3.1 FastAPI gateway

允许：HTTP method/path/content-type/body-size 处理；严格请求 DTO；本地开发鉴权上下文适配；调用应用层 `submit_task` / `query_task`；把应用层响应序列化为 HTTP 状态码与 JSON；`/healthz` 进程存活、`/readyz` 依赖就绪。

禁止：capability 关键词路由；Resolver、Planner、Policy、Approval 或状态迁移判断；SQL/ToolCall 合成；直接读取 Evidence/Plan 表；直接 import fake tool 或 PostgreSQL driver；从请求体接收 tenant、environment、actor、trace_id、request_id、channel 或 policy revision。

**失败协议是闭集，且必须覆盖框架自己产生的响应。** FastAPI / Pydantic 的默认 422 会把 `input` 原样回显进响应体——那与仓库"拒绝路径不回显输入"的不变量直接冲突（`hide_input_in_errors` 只管 pydantic 侧，不管 FastAPI 的 handler）。只注册 `RequestValidationError` handler 不够：**405 与任何未捕获的 500 仍会走框架默认错误体**。因此必须注册三个 handler——`RequestValidationError`、`StarletteHTTPException`（覆盖 404/405/415 等框架自己抛的）、以及兜底的 `Exception`——三者共用同一个闭集响应构造函数：

| 状态 | 触发 | 响应体 |
| --- | --- | --- |
| 400 | 严格 DTO 校验失败、未知字段、非法 JSON | `{"error": {"code": "invalid_request"}}` |
| 404 | 不存在或不属于本 scope | `{"error": {"code": "not_found"}}` |
| 405 | 路径存在但方法不允许 | `{"error": {"code": "method_not_allowed"}}` |
| 409 | 同键异义 | `{"error": {"code": "idempotency_conflict"}}` |
| 413 | body 超限 | `{"error": {"code": "payload_too_large"}}` |
| 415 | Content-Type 非 JSON | `{"error": {"code": "unsupported_media_type"}}` |
| 500 | 未捕获异常 | `{"error": {"code": "internal_error"}}` |
| 503 | 持久化不可用（§4.7） | `{"error": {"code": "unavailable"}}` |

响应体只有闭集 code，没有 message、没有字段名、没有输入回显。一条用例枚举**全部八种状态**并逐条比对完整 JSON 形状；另一条用例注入一个在 handler 之后抛出的异常，断言 500 走的是闭集体而不是 Starlette 默认体。

**但三个 handler 仍漏掉一种框架自产的响应：trailing-slash 的 307。** Starlette 的路由默认开启 `redirect_slashes`，`/v1/tasks/` 会被**直接**返回一个 `307` 加 `Location` 头——它不经过任何异常，`StarletteHTTPException` handler 根本看不到它。于是"失败协议是八个状态的闭集"这句话在第一个多打了斜杠的请求上就不成立，而且这个 307 还会把客户端连同**请求体**一起重放到 `Location`（307 的语义就是保持方法与 body），与 §3.2 花力气在 CLI 侧关掉重定向是同一类风险的服务端一半。

处置：**关闭 slash redirect**，不把它纳入协议。理由是纳入的收益为零——两个端点都是我们自己的 CLI 与 smoke 在调，没有兼容负担；而关掉之后 `/v1/tasks/` 落进正常的 404 闭集体，语义一致。实施时按所选 FastAPI 版本确认设置点（构造参数或 `app.router.redirect_slashes = False`），用例**断言行为而不是断言设置点**：`/v1/tasks/`、`/healthz/`、`/readyz/` 在 `follow_redirects=False` 下都不得返回 3xx，且响应体是闭集 404 体。附一条反证：把 `redirect_slashes` 打开，这三条必须转红。

**Content-Type 的判据拍板：解析 media type，忽略参数。** `application/json` 与 `application/json; charset=utf-8` 都接受（后者是 `urllib` 与多数客户端的常见写法，拒绝它会让自家 CLI 打不通）；`application/json-patch+json`、`text/json`、缺失或空的 Content-Type 一律 415。判据是"media type 逐字等于 `application/json`，参数部分不参与判断"，不是子串匹配——子串匹配会放行 `application/jsonx`。

**`GET` 必须是纯读。** 重复调用不得改变 `version`、不得写 audit 或 evidence、不得触发 Resolver / Planner / Gateway。由一条用例用 spy 断言调用次数为 0，并**同时**比对前后的 `version`、audit 行数与 evidence 行数（三样都比，理由见 §2.8：只比 `version` 会漏掉投影路径上的 RENDERING 事件）。

**trace 上下文不得串线。** 每个请求生成独立 `trace_id`，并发请求之间互不可见；异常路径必须 reset，不能把上一请求的 trace 泄漏给下一个。用并发请求 + 异常注入两条用例承重。

最小端点：

| 端点 | 成功 | 失败语义 |
| --- | --- | --- |
| `POST /v1/tasks` | `202`，返回 `TaskView` | 400 / 409 / 413 / 415 / 503 |
| `GET /v1/tasks/{task_id}` | `200`，返回同 scope `TaskView` | 非本 scope 与不存在统一 404；503 |
| `GET /healthz` | 进程事件循环存活即 200 | 进程不可服务时由容器/服务器失败 |
| `GET /readyz` | DB ping、migration head、依赖装配均通过才 200 | 任一失败 503；不得扫描 task、不得执行能力 |

### 3.2 CLI

CLI 只使用 `argparse`、`urllib.request`、`json` 和标准库错误类型：

- `xiaowei task submit --text ... --idempotency-key ...`
- `xiaowei task get TASK_ID`
- `--base-url` 只决定 API 地址；CLI 不接受 tenant/env/actor/trace override。
- opener 作为 callable 注入，单元测试不打开 socket。

**退出码必须是 HTTP 状态码上的一个全函数，不能只覆盖列举过的那几个。** 上一版的七个码漏掉了 500——而 §3.1 刚刚为"未捕获异常"定义了 500，也就是说 API 会返回一个 CLI 不知道怎么处理的状态。同类的还有反向代理常见的 502 / 504，以及任何未来新增的状态。落到实现上，"没有映射"要么是一次 `KeyError`（堆栈打到 stderr，违反不回显约束），要么是一个默默的退出 0（把服务端错误报成成功）。两种都不能接受。

因此定义为**总映射**，八个码，任何 3 位状态码都落在某一格：

| 码 | 含义 | 触发（穷尽） |
| --- | --- | --- |
| 0 | 成功 | 2xx 且响应体通过 `TaskView` 严格校验 |
| 2 | 用法/协议错误 | 400、405、415、**其余全部 4xx**、3xx（重定向不跟随）、非 JSON Content-Type、JSON 解析失败、响应体超限、**2xx 但响应体不符合 `TaskView`** |
| 3 | 未找到 | 404 |
| 4 | 幂等冲突 | 409 |
| 5 | 请求过大 | 413 |
| 6 | 服务不可用 | 503、连接失败 |
| 7 | 超时 | 读写超时 |
| 8 | 服务端错误 | **500、502、其余全部 5xx**（504 归 7，语义是超时） |

最后两行的分法有实际后果：6 表示"稍后重试大概率能成"，8 表示"服务端坏了，重试不会变好"。把 500 并进 6 会让脚本无限重试一个确定性故障。

**2xx 也必须校验响应体。** 成功路径接受任意 JSON 时，一个返回 `{}` 或返回别人任务的服务端 bug 会被 CLI 当成成功并打印空结果。改为用 `TaskView` 的严格 DTO（`extra="forbid"`）解析，不通过即退出 2——与请求侧用严格 DTO 是同一条理由，只是方向相反。

**删除原先的退出码 8（"非终态"）语义**（那条路径不可达：触发条件写的是"调用方要求终态"，而 CLI 没有对应 flag；轮询属于调用方，smoke 脚本自己轮询）。非终态就是一次正常的 `0`，`render` 为 `null` 由调用方读。码 8 现在归"服务端错误"，是一条真实可达的路径。

**`urllib` 的默认行为必须被显式关闭三处，否则 CLI 本身就是一条外泄通道：**

1. **重定向。** `urlopen` 默认跟随 30x。一个被指向恶意 `--base-url` 或被中间人改写的响应可以把 `POST` 的**任务原文**重放到第三方主机。opener 用 `build_opener()` 显式构造并**不安装** `HTTPRedirectHandler`（子类化后让所有 `redirect_request` 抛出），30x 一律按协议错误退出 2。
2. **scheme。** 默认支持 `file:`、`ftp:` 等。`--base-url` 必须先解析并断言 scheme ∈ `{http, https}`，否则 `--base-url file:///etc/passwd` 会让 CLI 去读本地文件并把内容当响应处理。
3. **userinfo。** `http://user:pass@host/` 形态的 URL 会把凭证带进请求与日志；解析后 `netloc` 含 `@` 即拒绝。

其余边界：

- stderr 只输出闭集 code 与本地文案，**不回显** response body、headers、URL 或任何服务端文本。
- **请求体必须用 `ensure_ascii=False` 序列化并按 UTF-8 编码。** 这不是风格问题，是一条会让合法请求被自己的 API 拒绝的缺陷。实测：`json.dumps({"text": "😀" * 8192})` 在标准库默认的 `ensure_ascii=True` 下是 **98,316 字节**（每个非 BMP 字符被写成两个 `\uXXXX`，12 字节），而 `ensure_ascii=False` 下是 **32,780 字节**。`RequestEnvelope.text` 的上限确实是 8192 字符（`contracts/request.py`），所以这是一个**完全合法**的最大请求——用默认序列化就会撞上 body limit 并拿到 413，而字段级校验根本没机会跑。
- 响应体有大小上限（§4.7 的 1 MiB）：读取时按上限**截断式读取**（读 limit+1 字节，超出即拒），不是先读完再判断长度——后者在服务端返回超大响应时已经把内存吃掉了。
- 非 `application/json`（同 §3.1 的 media type 判据）或无法解析的 JSON 按协议错误处理，不尝试"尽力解析"。
- 输出到终端前对**所有**来自服务端的字符串做控制字符与 ANSI 转义序列转义（见 §5.2），不因为"它来自我们自己的 API"就信任它——那些值最初来自 fake adapter 返回的外部内容。

测试清单必须逐条覆盖：上表八个码各至少一条；500 / 502 / 504 / 一个未映射的 4xx（如 418）/ 一个未映射的 5xx 各一条，证明映射是全函数；连接被拒；读超时；响应体恰好上限与超一字节；非 JSON Content-Type；JSON 解析失败；**2xx 但响应体缺字段 / 多字段** → 退出 2；30x；非法 scheme；带 userinfo 的 `--base-url`；非终态 `GET` 返回 0；**8192 个 emoji 的请求经 CLI 提交成功**（与 API 侧那条边界用例配对，证明两端的编码约定一致）。

CLI 固定调用 API，信封 channel 仍由服务端标记为 `API`；不得把 CLI 做成直接 Runtime/TaskStore 的第二入口。

### 3.3 Worker

`interfaces/worker.py` 只负责加载设置、组装依赖、处理信号和进程退出码；轮询、退避、领取、执行和异常分类放在 `application/worker.py`。

**`WorkerLoop` 的依赖必须显式，不能是"只接收 Runtime"。** Worker 要调用 `list_dispatchable_tasks`、`begin_task_attempt` 与 `schedule_retry`，而 `XiaoweiRuntime` 的公开面只有 `submit_task` / `execute_task` / `query_task`——没有 dispatch。把 dispatch 塞进 Runtime 会让"只负责一次执行"的编排层长出调度职责；让 Worker 私读数据库更糟。因此依赖显式列出：

```python
@dataclass(frozen=True)
class LocalStack:
    """interfaces/local_stack.py 的装配产物。唯一 import fake 的地方。"""
    runtime: XiaoweiRuntime
    task_store: TaskStore
    plan_store: PlanStore
    evidence_ledger: EvidenceLedger
    clock: Clock                      # 注入的墙钟，用于任务时间语义
    monotonic: MonotonicClock         # time.monotonic 的可注入形态，见下
    settings: Settings
    readiness: ReadinessProbe         # /readyz 的唯一入口，见下
    aclose: Callable[[], Awaitable[None]]   # 释放 Engine/连接池


class WorkerLoop:
    def __init__(
        self, *, runtime: XiaoweiRuntime, task_store: TaskStore,
        clock: Clock, monotonic: MonotonicClock,
        settings: Settings, sleep: AsyncSleep,
    ) -> None: ...
```

**形状只有一种：`@dataclass(frozen=True)`。** 上一版在代码块里写 `class LocalStack(Contract)`、在紧随其后的散文里又论证应该用 frozen dataclass——两句话直接矛盾，实施时照哪一句都能自圆其说。定为 dataclass 的理由：它持有的是**对象引用**（Runtime、Store、可调用对象）而非可序列化数据，Pydantic 的 strict/frozen 校验对它们不产生任何约束，只带来一层无意义的验证。这与仓库"跨边界一律 Pydantic 契约"的惯例不同，因此写进 ADR-010 以免被当成违例——判据是它不跨进程也不跨信任边界，只是一次进程内装配。

**`readiness` 与 `aclose` 不是可选的补充字段，缺了它们两条路径无解：**

- **`/readyz` 需要 DB ping 与 migration revision（§6.4），但 `interfaces/api.py` 按 §5.4 的窄允许集不能碰 Engine，也不该碰。** 没有这个字段，实现时只有两条路：让 API 直接持有 Engine（撞穿薄边界），或者放弃这个端点。因此定义一个窄 Protocol `ReadinessProbe`（`async def check(self) -> ReadinessReport`，`ReadinessReport` 是含 `database_ok` / `revision_matches_head` / `assembled` 三个 bool 的严格契约）。**Protocol 与 report 都落在 `contracts/`，实现留在 `persistence/`**，装配时注入；这是与 TaskStore/PlanStore「Protocol 与实现同包」惯例的一次刻意分叉，因为 API 需要给 `check()` 写严格类型注解，却不能因此获得直接依赖 persistence 的权限。API 只调 `check()` 并把三个 bool 映射成 200/503，不知道下面是什么。
- **`aclose` 是连接池的唯一释放点**，三条路径都要它：API 的 lifespan 关闭、Worker 的 SIGTERM 路径（§3.3 说"直接退出"，但退出前必须 dispose，否则容器停止时连接留在 PostgreSQL 侧直到超时）、以及**装配中途失败**时的回滚（前半截已经建好 Engine，异常抛出后没人释放）。第三条最容易漏：`local_stack` 的构造函数必须自己 `try/except` 并在失败时 dispose 已建成的部分再向上抛。

**`monotonic` 显式注入 `WorkerLoop`。** §3.3 要求故障窗口用 `time.monotonic()` 计量，而"用 monotonic 而不是墙钟"这件事只有在能注入一个可控 monotonic 时才可测——直接调用模块级函数的实现，测试只能 sleep 真实时间或 monkeypatch `time.monotonic`（后者会污染全进程）。注入之后，那条"墙钟跳变不影响窗口"的用例才能同时推进两个时钟并断言差异。

Worker 允许：为配置的 tenant/environment 构造 `DispatchQuery`；竞争 `begin_task_attempt`；把 grant 与持久化 submission 交给 `runtime.execute_task`；根据闭集结果停止、调度任务重试或退出进程；维护进程级基础设施故障窗口。

三条运行契约：

- 故障窗口用 **monotonic clock**（`time.monotonic()`）计量而非墙钟——一次 NTP 调整或容器时间跳变就能让 15 分钟窗口永远不触发或立刻触发。
- **重置的粒度是"一次完整的 poll iteration 没有任何 infra 故障"，不是"任何一次成功的数据库操作"。** 后者是上一版的写法，它有一个能让 Worker 永不退出的洞：`list_dispatchable_tasks` 走的是只读路径，`begin_task_attempt` 走的是写路径（行锁、序列、事务），完全可能出现**前者次次成功、后者次次失败**的故障形态（写盘满、复制槽阻塞、某张表被锁）。按"任何一次成功都重置"，每一轮的 list 成功都会把窗口清零，于是那个 Worker 会永远轮询、永远领不到任务、也永远不退出——而进程级 fail-stop 存在的全部意义就是让编排层看见它坏了。改为：一轮里只要有任何一次 infra 故障就**不重置**，只有整轮无故障才清零。
  反例一条：构造 `list_dispatchable_tasks` 恒成功、`begin_task_attempt` 恒抛 `PersistenceUnavailableError` 的 store，断言 Worker 在窗口耗尽后**非零退出**；按"任何一次成功都重置"的实现，这条用例会一直跑到超时。
- `SIGTERM` 与 `CancelledError` 走同一条路径：停止领取新任务、取消当前执行分支、**不调用 `schedule_retry`、不写终态**，直接退出。在执行的任务靠租约自然到期后由别的 Worker 恢复——优雅停止不是任务失败，把它记成一次失败会让滚动重启白白消耗失败预算。

Worker 禁止：自己解释意图、规划、执行工具或判断审批；直接改状态字段；在 loss-of-lease 后补写结果；用 `except Exception: retry` 覆盖结构化并发 winner；从轮询上下文伪造 actor/trace/policy revision。

### 3.4 Runtime 与现有 `handle()` 兼容

新增异步拆分接口但保留 `handle()` 供已有契约/eval 使用：

- `submit_task(...) -> TaskView`：做 submission/task 的原子持久化，并**由应用层完成投影**后返回。
- `execute_task(*, grant, submission)`：重建确定性解释/解析/计划链并执行。
- `query_task(*, lookup: TaskLookup) -> TaskView`：读取受 scope 约束的任务并统一投影。

**`submit_task` 返回 `TaskView` 而不是 `TaskRecord`，否则 POST 的薄调用链缺一跳。** §3.1 禁止 API 判断 pending/terminal 或复制 rendering 文案，而 `TaskRecord` 是内部任务事实（含 `version`、租约字段、`terminal_reason`）——让 API 拿着它去构造 `202` 的响应体，它就必须自己决定"哪些字段能出去、`render` 该不该有"，那正是 §2.8 定义 `TaskView` 要消灭的事。两条可选路径里：让 API 连着调 `submit_task` 再调 `query_task` 多一次数据库往返、且在两次调用之间任务可能已被 Worker 推进（返回的投影与刚创建的任务不是同一时刻的事实）；让 `submit_task` 直接返回 `TaskView` 只有一跳，且投影与写入同源。选后者。

一条 spy 用例证明这件事：`interfaces/api.py` 的 POST 处理函数中，**没有任何对 `TaskRecord` 字段的读取**（AST 断言：不出现 `.status` / `.version` / `.terminal_reason` / `.lease_*` 属性访问），响应体逐字来自 `TaskView` 的序列化。
- `handle()` 复用相同 submit、execute、projection 函数；不能保留第二套生命周期逻辑。

M5 API/Worker 不调用 `handle()` 做同步请求。`RENDER_REF` 仍为兼容性决定，不新增无人消费的 render store。

**`handle()` 共享的是投影与 Runner，不是提交顺序。** 这一条必须写死，因为两者不能兼得：

`handle()` 现在的顺序是 interpret → resolve → plan → `create_task` → run，因此**确定性拒绝发生在任务被创建之前**，以 `RequestRejectedError` 抛给调用方，一条任务事实都不留。异步路径的顺序相反：submit 先落库，拒绝在 Worker 里发生，只能表达为 `REJECTED` 终态。这不是实现细节，它有四处承重方：

- `tests/fakes/runtime.py:167` 的 `except RequestRejectedError:`——`RuntimeHarness.run_injection()` 靠它吞掉确定性拒绝后只看 trace 归因；
- `tests/contract/test_runtime_facade.py::test_each_injected_failure_is_attributable_to_one_stage`（`unknown_capability`、`window_too_wide` 两个注入走的正是这条抛出路径）；
- `tests/contract/test_runtime_facade.py::test_a_successful_run_emits_all_nine_stages_in_order`（阶段序列被逐条钉死，submit-first 会在 INTENT 之前插入任务创建、并给拒绝路径补一条 LIFECYCLE 终态事件）；
- `tests/evals/test_l0_security.py` 与 `tests/evals/test_l2_readonly.py` 两个 ADR-008 的 eval gate，全部经 `harness.handle(...)` 取 `RenderPayload`。

因此 M5 **不改 `handle()` 的提交顺序**。它保留 M3/M4 已验收的同步语义，只把两样东西换成与异步路径同源的实现：

1. **终态投影函数**：终态走同一份最终 render——一份实现，两个调用方。非终态在异步路径恒为 `null`（§2.8），`render_pending` 因此只剩 `handle()` 一个调用方。
2. **Runner 调用形状**：`handle()` 同样必须先取得 `TaskAttemptGrant` 再调 `start()`，因为 Runner 契约已改为只接收 grant（§2.5）。`handle()` 因此会产生 `attempt_number = 1`，但**不消耗** `task_failure_count`（它的拒绝路径根本不进入任务）。

**`handle()` 遇到非终态/live lease 时必须让步，而让步的表达方式不能改它的返回类型。** 这条路径真实存在：`create_task` 是幂等的，一次同键重复调用会取回**既存任务**，而那个任务此刻完全可能正被某个 Worker 执行（live lease）。接着 `handle()` 去 `begin_task_attempt`，得到 `LIVE_LEASE`。它绝不能等待、重试或强行接管——那会让两个执行者同时跑同一个任务。

上一版说"返回 `render=null` 的非终态 `TaskView`"，那是**错的**：`handle()` 的签名是 `-> RenderPayload`（`application/runtime.py`），而 `RenderPayload` 与 `TaskView` 是两个不同的契约；`RenderPayload` 也没有"空投影"这种取值。要让它返回 `TaskView` 就得改返回类型，而那会打红全部经 `harness.handle(...)` 取 `RenderPayload` 的契约用例与两个 ADR-008 eval gate——正是 §3.4 花整节在避免的事。

正确形状是**保持既有返回契约，用异常表达让步**：新增 `TaskInProgressError`，按 `TaskIdCarryingError` 的既有写法只带 `task_id`、消息恒为常量、不含任何请求文本。`handle()` 在取回既存任务且它**非终态**时抛出它，Runner 调用次数为 0。已终态的既存任务照旧走 `_render_recorded`（这是 M4 已有的行为，一个字不改）。

**实施前必须先核对一件事**：当前 `handle()` 对"既存任务非终态"是直接 fall through 去 `self._runner.start(...)` 的。因此 Task 7 的第一步是搜出所有会走到这条分支的既有用例；若存在，它们改为断言 `TaskInProgressError`，并在提交信息里逐条列出。若不存在（预期如此，因为 M4 的幂等用例都用终态任务），也要写一条用例把这个新行为固定下来，而不是让它以"碰巧没人测"的形态存在。

用例三条：同键重复 `handle()`，第二次在任务被租出期间调用 → `TaskInProgressError`，Runner 调用次数为 0，`version` 不变；第二次在任务已终态后调用 → 返回终态 `RenderPayload`，Runner 调用次数为 0；`TaskInProgressError` 的 `str()` / `repr()` 不含 task_id 与任何请求文本。

**`execute_task` 的返回与异常协议**同样在此拍板：它返回 `TaskOutcome`（终态，`TaskOutcome` 的 validator 已强制这一点），可见异常面恰为：

| 异常 | 含义 | `WorkerLoop` 的动作 |
| --- | --- | --- |
| `WorkflowPaused` | 正常控制流 | 停止，不计失败、不调度重试 |
| **`RetryableTaskError`** | 未分类故障，本次尝试失败但任务应重试 | 携**原 grant** 调 `schedule_retry` |
| `LifecycleError`（带结构化 `TransitionRejection`） | CAS/租约结局 | 按 §3.5 的 rejection 闭集分派 |
| `PersistenceUnavailableError` | 不可用 | 进程级退避 |
| `PersistenceIntegrityError` | 系统性故障 | 进程 fail-stop，任务不改 |

其余一律已在 `execute_task` 内部被分类并落成终态。它**不返回** `None`、不返回非终态、不吞异常。

**`RetryableTaskError` 不是可选的封装，缺了它重试协议根本闭不上环。** §3.5 说未分类 `Exception` 走 `TASK_FAILURE` 重试、由 Worker 调 `schedule_retry`；而上一版又说 `execute_task` 的可见异常面里没有这一类、"其余都在内部落成终态"。两句同时成立时，未分类故障要么被 `execute_task` 直接写成 `FAILED`（**失败预算形同虚设，第一次故障就终态**），要么原始异常穿透到 Worker（**异常原文越过脱敏边界**，而那正是 gateway 花了大力气堵的口子）。

因此定义一个闭集异常：只带 `RetryReason`（§4.4 的闭集码）与 `task_id`，**不带原异常对象、不带 `__cause__`、消息恒为常量**，按 `config.py` 的既有做法在 `except` 块**之外**抛出（理由见 §4.7）。Worker 捕获它，用自己手里那份 grant 调 `schedule_retry`。

反例四条：前三次尝试**确实执行了工具**（Gateway 调用次数为 3，不是"看起来重试了"）；`task_failure_count` 逐次 1→2→3 单调递增；第 4 次 `begin_task_attempt` 在**执行任何东西之前**返回 `RETRY_EXHAUSTED` 并原子写 `FAILED`（Gateway 调用次数为 0）；`RetryableTaskError` 的 `str()` / `repr()` / `__cause__` / `__context__` 都不含原异常文本。

代价是"生命周期只有一处"这句话在字面上不成立：`handle()` 保留了一条与异步路径不同的**提交顺序**。这个代价被显式接受并写进 ADR-010，理由是另一条路——改 `handle()` 顺序——会静默改变两个 eval gate 的观测值，而 eval gate 是 ADR-008 定义的发布门，不能作为一次实施的副作用被改动。

Task 7 的红灯必须包含一条反证：**删除 `handle()` 中 `create_task` 之前的任一拒绝分支，`tests/fakes/runtime.py` 的注入用例必须转红**——证明这条顺序确实被测试承重，而不是一句注释。

### 3.5 异常与状态闭集

状态表补齐 `CREATED -> REJECTED` 与 `RUNNING -> REJECTED`。核对当前源码：`contracts/task.py` 的 `ALLOWED_TRANSITIONS` 里 `CREATED` 只有 `{PLANNING, CANCELED, FAILED}`、`RUNNING` 只有 `{AWAITING_APPROVAL, SUCCEEDED, FAILED, CANCELED, INDETERMINATE}`，两条都确实不存在。加入后 `RUNNING` 可以到达五个终态。

**这一改动横跨三处源码，必须在同一个 Task 里做完，否则中间态无法全绿。** `runners/fake.py::ScriptedRunner.__init__` 有一条 `if outcome_status not in ALLOWED_TRANSITIONS[TaskStatus.RUNNING]: raise ValueError(...)`——它在**构造时**就拒绝 `REJECTED`。改了 `ALLOWED_TRANSITIONS` 而不改它，这条检查会变成一句永远为假的死代码（虚构的"RUNNING 仍有不可达终态"）；只改它而不改状态表，构造出的 Runner 会跑到最后一步迁移才失败。加上 `tests/contract/test_runner_contract.py` 里断言这条拒绝的用例，三处必须同提交：

1. `contracts/task.py` 的 `ALLOWED_TRANSITIONS`；
2. `runners/fake.py` 的 `ScriptedRunner.__init__`（删掉该检查，保留紧邻的"必须是终态"那一层）；
3. `tests/contract/test_runner_contract.py`（删掉断言该检查的用例，**保留** `outcome_status=TaskStatus.RUNNING` 的非终态拒绝用例——它由第一层承重，删掉会丢覆盖）。

归属定为 **Task 5**（Runner 切片），因为三处里有两处是 Runner 的文件；Task 7 的异常分类只是这条边的消费方，不再重复改动。

**异常分类必须精确到异常类型，不能按"确定性 / 未分类"两桶粗分。** 粗分的两个方向都会出错：把确定性拒绝丢进重试桶会白跑三次尝试；把代码/schema 缺陷丢进基础设施桶会进入无限退避而没有人看见。完整闭表：

| 异常 / 来源 | 结果 | 是否消耗失败预算 |
| --- | --- | --- |
| `RequestRejectedError`、目标漂移（`TargetResolutionError`） | `REJECTED`，立即终态 | 否 |
| `PolicyDeniedError` / `SqlGuardError` / `BindingError` / **`SpecResolutionError`**（`admit_step` 声明的四类确定性准入拒绝） | `REJECTED`，立即终态 | 否 |
| **`DriftError`**（`resume()` 重算发现 plan_hash / target_fingerprint / policy_revision / 审批绑定与存储事实不一致）、**`PlanConflictError`**（`PlanStore.save` 发现已存计划与重算计划不同） | `FAILED`，立即终态，闭集 reason `recovery_drift` | 否 |
| **`PermissionError`（`gateway.invoke`：凭证不匹配、策略过期、policy denied、E1 硬闸）** | `REJECTED`，立即终态 | 否 |
| **`LookupError`（`gateway.invoke`：adapter 未注册）** | **进程 fail-stop，任务一个字不改**；同时由 readiness 在启动阶段拦截 | 否 |
| submission 缺失/损坏 | `FAILED`，由 `begin_task_attempt` 在**行锁内、发放 grant 之前**原子写入（§4.3 第 7 步） | 否 |
| `PlanNotFoundError`、确定性不变量破坏（如 `start()` 遇到非空 journal） | `FAILED`，由 grant 持有者在当前 owner/token 下写入 | 否 |
| `MalformedAdapterResponseError` | 不是任务级异常：走 §2.7 的形状 B 步骤提交 | —— |
| 不可信或未知工具结果 | `INDETERMINATE` | 否 |
| `WorkflowPaused` | 正常控制流，保持 `AWAITING_APPROVAL`，停止 heartbeat | 否 |
| **`PersistenceUnavailableError`**（§4.7：连接/超时/池、`DisconnectionError`、connection-invalidated DBAPIError） | 进程级基础设施退避，不伪造终态；写调用另带 `PersistenceWriteOutcome` | 否 |
| **`PersistenceIntegrityError`**（§4.7：`IntegrityError` / `ProgrammingError` / `DataError` / 其余非连接失效的 `StatementError` / `SQLAlchemyError` 兜底 / 行反序列化） | Worker 进程立即非零退出；API 侧 500。**任务一个字不改** | 否 |
| 未分类的普通 `Exception`（含非 malformed 的 `TypeError`） | `TASK_FAILURE` 重试；失败预算耗尽后 `FAILED` | **是** |
| `CancelledError`、`KeyboardInterrupt`、`SystemExit` | 清理 heartbeat 后原样抛出 | 否 |

三条容易写错的边界：

- **`admit_step` 的四类拒绝必须列全。** 它的 docstring 明确声明 `BindingError`、`SpecResolutionError`、`PolicyDeniedError`、`SqlGuardError`、`ApprovalRequiredError` 五个（最后一个是控制流，由 Runner 转 `WorkflowPaused`）。上一版漏了 `SpecResolutionError`（"分类无法派生，或与步骤标记冲突"），于是它会落进"未分类 `Exception` → 重试"桶——而它和另外三个一样是**确定性**的，重试三次必然得到同一结果，最后还以错误的 `FAILED` 语义收场。
- **`DriftError` 是恢复路径独有的，单进程用例看不到它。** `runners/deterministic.py` 在 `resume()` 里为五种漂移各抛一次（审批缺失、`approval_ref` 不匹配、plan_hash 不可复现、target_fingerprint 漂移、policy revision 漂移）。它既不是准入拒绝（`REJECTED` 语义不对：请求本身合法，是世界变了），也不该重试（重试同样会重算出同一份漂移）。归 `FAILED` + 闭集 reason `recovery_drift`，不消耗失败预算。
- **`LookupError`（adapter 未注册）从"任务 `FAILED`"改为进程 fail-stop。** 上一版一边把它认定为"装配缺陷"，一边又让它逐任务写 `FAILED`——这与 §4.7 刚立的原则直接冲突：一个装配缺陷对**每一个**任务都失败，逐任务终态化就是批量误杀，而且把根因（这个进程的 gateway 装错了）埋进一堆任务的 terminal_reason 里。正确处置与系统性持久化故障同类：**进程立即非零退出、任务不改**。同时把它前移一步——§6.4 的 readiness 第 3 项"应用依赖已完整装配"要真正检查 gateway 的 adapter 映射覆盖了当前 capability spec 所需的 gateway 名，让这个缺陷在容器起来时就暴露，而不是等第一个任务进来。运行时仍漏出的话按 fail-stop 处理，那是最后一道网。
- **`PlanConflictError` 不是请求拒绝，上一版把它归进 `REJECTED` 是错的。** `persistence/plans.py` 的定义是"同一 task_id 已存在**不同**的计划或目标"，而 `task_id` 是每个任务新铸的、`StoredPlan` 只含 plan 与 target、两者都由 (submission, target, policy) 确定性派生。因此这条异常在字面上只可能出现在**同一个任务被重新规划出了不同结果**的时候——`CREATED + stored plan` 的恢复路径（§2.6 第二行）重算计划、或两个 Worker 并发规划同一任务。两种都是"世界变了"或不变量破坏，不是"用户请求不该被接受"。归 `REJECTED` 的实际后果是把一次恢复漂移写成一个用户面的拒绝终态，根因（存的计划和现在算的不一样）被丢掉。

  **"按来源拆分"在这里只有一个来源，因此不拆。** 我核对了另一条可能的来源——首次 `save()`：那时表里没有该 task_id 的行，`save` 走的是插入分支，构造不出冲突。所以没有第二个分支可分；写一个只有一侧可达的 `if` 是在计划里造死代码。整条归 `FAILED` + `recovery_drift`，与 `DriftError` 同一格。

  承重用例两条：`CREATED + stored plan` 恢复时注入一个会产出不同计划的 planner → 任务落 `FAILED` + `recovery_drift`、**Gateway 调用次数为 0**（证明它在执行任何工具之前就被判定）、`task_failure_count` 不变（不消耗失败预算）；把它改回 `REJECTED` 时，"终态 reason 属闭集且为 `recovery_drift`"这条断言必须转红。
- **`PermissionError` 仍是 `REJECTED`。** 它是"策略说不行"，对这一个任务确定成立、对别的任务未必，因此是任务级结论，不重试。
- **`LookupError` 的捕获范围必须收窄到 `gateway.invoke` 调用点。** `TaskNotFoundError` 继承 `LookupError`，一条全局的 `except LookupError` 会把"任务不存在"误判成"adapter 未注册"。
- **`asyncio.TimeoutError` 是 `TimeoutError` 的别名，而 gateway 内部已经捕获它并转成 `ToolCallStatus.TIMEOUT`。** Runner 层再写一条 `except TimeoutError` 只会捕到**持久化层**的超时，那属于 `PersistenceUnavailableError` 的映射范围，不得单独成桶。

每一类都必须有一条断言**调用次数**的反例（确定性拒绝：`begin_task_attempt` 被调用 1 次而非 4 次；infra：任务 `task_failure_count` 保持 0 且被反复放回队列），仅断言"终态正确"不足以区分"重试了三次最后失败"和"第一次就正确终态"。

`LifecycleError` 不能按异常大类一刀切，必须检查其结构化 `TransitionRejection`：

- `STALE_FENCING_TOKEN` / `LEASE_NOT_HELD`：当前 Worker 是 loser，立即停止，不调度重试、不写终态；
- `TERMINAL_PROTECTED`：采纳存储层终态 winner；
- `VERSION_MISMATCH` / `ILLEGAL_TRANSITION`：不变量破坏；只有当前 grant 仍有效且迁移合法时才写 `FAILED`，否则停止并采纳 winner。

**终态 CAS 必须使用原始 grant 的 fencing token——这是 M5 要修的一个 `main` 上既有缺陷。** `application/runtime.py::_finish` 目前重新 `get(task_id)` 拿到 `current`，然后把 **`current.fencing_token`** 交给 `transition`。单 Worker 下看不出问题，但在 M5 的并发/恢复语义下这是一条完整的 fencing 绕过：旧 Worker 丢掉租约后，新 Worker 已取得更高 token 并写进 task row；旧 Worker 此时读到的 `current.fencing_token` 正是**新 Worker 的** token，于是它带着别人的 token 把自己的旧结果写成终态，`_check_fencing` 一路放行。

这与 `tests/security/test_vacuous_check_ban.py` 立规矩的那个缺陷是同一形态——"当前值从存储读出来再交回存储"，检查两侧同源因而恒真——只是当时只为 `expected_version` 立了规矩，没有为 token 立。M5 一并补上：

- `_finish` 接收 `TaskAttemptGrant`，`fencing_token` **永远**取 `grant.lease.fencing_token`，绝不取刚读回的行；
- `expected_version` 仍可取当前 winner 的 version（版本是乐观锁的对照物，不是权限凭据）；
- CAS 失败时采纳存储层 winner 并停止，不重试、不覆盖。

反证由两条承担：一条行为用例（旧 Worker 停在最终 CAS 前 → 新 Worker 接管并终态化 → 旧 Worker 恢复后必须失败且不改变 winner），一条 AST 用例（扩展 `test_vacuous_check_ban.py`，禁止 `_finish` 里出现 `current.fencing_token` / `record.fencing_token` 这类"从刚读回的行取 token"的属性链）。

## 4. 持久化与迁移设计

### 4.1 TaskStore 仍是一个端口

TaskStore 从 8 个方法扩为 14 个：现有 8 个，加 `list_dispatchable_tasks`、`begin_task_attempt`、`schedule_retry`、`begin_step_attempt`、`commit_step_result`、`load_step_executions`。`create_task` 改为接收 `TaskSubmission`，既有 `get` 改为接收 `TaskLookup`；两者都不增加方法数量。

14 个方法都围绕同一个 task aggregate，共同依赖 task row lock、lease/fencing、retry metadata 或原子 step/evidence/audit 事务。此时拆成多个写端口会重建跨端口事务空洞，因此不拆。

`PlanStore` 与 `EvidenceLedger` 继续保留独立读取/历史契约。`PostgresEvidenceLedger.append` 继续作为 M4 公共契约和测试面，但 M5 生产调用链没有调用者；Runner 必须通过 `TaskStore.commit_step_result` 提交证据。

**读取入口只有两个，且都不额外占方法位。** `TaskSubmission` 由 `begin_task_attempt` 的成功结果携带，`StepExecutionRecord` 由 `load_step_executions` 返回；因此不存在 `load_submission()` 这样的第 15 个方法。同理，§2.8 收窄 `AWAITING_APPROVAL` 投影的一个直接后果是**不新增审批读取方法**。

#### 4.1.1 十四个方法的精确签名

只列方法名不够——上一版就是因为只写了名字，才让"状态改动与审计同事务"变成一句无法调用的话（下面 (b) 展开）。全部签名在此固化，实施时不再临场决定：

```python
class TaskStore(Protocol):
    # --- M4 既有八个，其中两个收窄入参、一个改为命令 DTO ---
    async def create_task(self, *, submission: TaskSubmission) -> TaskRecord: ...
    async def get(self, *, lookup: TaskLookup) -> TaskRecord: ...
    async def transition(self, *, command: TransitionCommand) -> TransitionResult: ...
    async def acquire_lease(self, *, task_id: str, owner: str, ttl_seconds: int) -> LeaseGrant | None: ...
    async def renew_lease(self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int) -> LeaseGrant | None: ...
    async def list_stale_leases(self, *, query: StaleLeaseQuery) -> tuple[TaskRecord, ...]: ...
    async def record_approval(self, *, request: ApprovalRequest) -> int: ...
    async def record_audit_event(self, *, event: TraceEvent) -> int: ...

    # --- M5 新增六个 ---
    async def list_dispatchable_tasks(self, *, query: DispatchQuery) -> tuple[TaskRecord, ...]: ...
    async def begin_task_attempt(self, *, command: TaskAttemptCommand) -> TaskAttemptResult: ...
    async def schedule_retry(self, *, command: RetryCommand) -> RetryResult: ...
    async def begin_step_attempt(self, *, command: StepAttemptCommand) -> StepAttemptResult: ...
    async def commit_step_result(self, *, command: StepCommitCommand) -> StepCommitResult: ...
    async def load_step_executions(self, *, task_id: str) -> tuple[StepExecutionRecord, ...]: ...
```

三条形状说明：

**(a) `list_stale_leases` 改收 `StaleLeaseQuery`。** 这个 DTO 在 `store.py` 里**已经存在**且带着"`limit=True` 在运行时会被当作 1"的 docstring，却没有被方法签名使用——M4 留下的一处不一致。M5 顺手收口，不新增类型、不改语义。

**(b) `transition` 必须改成收 `TransitionCommand`，否则"状态与审计同事务"根本无法调用。** 上一版说"给 `TransitionCommand` 加一个 `audit_events` 字段"，但 `TransitionCommand` 是**内部 DTO**：公开的 `TaskStore.transition()` 是五个散开的关键字参数，两个实现都在方法体内部才把它们组装成命令（`fake.py` 与 `postgres.py` 各一处）。给内部 DTO 加字段，调用方**没有任何途径把审计事件传进去**——那个字段会永远是默认的 `()`，而计划却声称它承载了原子性。这是"改了一个没人能用的东西"，不是设计取舍。

改成命令 DTO 之后，`TransitionCommand` 的既有五个字段一字不动（M4 的 `classify_transition` / `apply_transition` 都直接吃它），只增加 `audit_events`，因此 `decisions.py` 不受影响。M4 的共享套件改的是调用形状而不是语义，与 `create_task` / `get` 的收窄同一次提交完成。

**(c) `audit_events` 默认 `()`，端口层面强制不了"必须非空"——这一点必须如实写，不能假装契约挡住了。** M4 的存储层用例合法地不带审计（它们测的是 CAS 与租约，不是生命周期），把字段设为必填会打红一批与本次改动无关的用例。因此强制落在两条**外部**机制上，并且说清各自能证明什么：

- 一条 AST 用例扫描 `application/**` 与 `runners/**` 里对 `transition(` 的全部调用点，断言每一处的 `audit_events` 实参都不是空字面量（`()` / `tuple()`）。它证明的是"生产代码里没有无审计的迁移"，证明不了运行时的取值。
- 一条行为用例走**公开 API**跑完一次 M5 生命周期，然后断言该任务的每一次 `version` 变化都对应至少一条 audit 行。它证明的是运行时结果。

两条都不是契约级封闭，写进残余风险。

`begin_task_attempt` 与 `schedule_retry` 不同：它们是 M5 独有的方法，没有历史调用方，因此**可以**在命令 DTO 上把审计要求写成必填不变量——`RetryCommand.audit_events` 与 `begin_task_attempt` 在第 6 / 7 步写终态时构造的事件，两处都强制非空。

**无 grant 终态化时，审计事实从哪里来。** `begin_task_attempt` 的第 6 步（`retry_exhausted`）与第 7 步（`submission_invariant_violation`）都在**发放 grant 之前**写终态，此时没有 attempt 在飞、也没有 Runner 的 trace 上下文。两个字段的来源因此必须钉死，不能留给实现：

- `trace_id` ← `TaskAttemptCommand.trace_id`，由 Worker 在**每一轮轮询**开始时生成一个，整轮复用。它标识的是"哪一次轮询做出了这个判断"，语义正确且可追溯；伪造一个属于用户请求的 trace_id 才是错的。
- `attempt_number` ← `winner.attempt_number`，即**最后一次成功尝试的序号**；从未被领取过的任务是 0。它标识的是"这个终态发生在第几次尝试之后"，而不是"它属于第几次尝试"——两者语义不同，后者在这里根本不存在。这条与 §5.1"grant 存在之后的每条事件 `attempt_number` 必填"不冲突：这两条事件产生于 grant **不存在**的时刻。

`tests/contract/test_protocol_conformance.py` 精确断言方法数、keyword-only 参数及内存/PostgreSQL 两实现签名一致。哨兵常量按 §7 的任务切分**分两步**从 8 → 11 → 14，每一步与对应实现同提交，并在同一提交更新注释说明这一步加了哪几个。

### 4.2 新/变更契约

`TaskRecord` 增加四个严格字段：

- `created_seq: StrictInt > 0`
- `attempt_number: StrictInt >= 0`：成功取得执行 grant 的累计次数；审批恢复也可以增加，不代表失败。
- `task_failure_count: StrictInt >= 0`：仅任务/应用层可重试故障增加。
- `next_attempt_at: AwareDatetime | None`：下一次允许调度时间。

新契约：`TaskSubmission`（原始 RequestEnvelope、RequestContext、原始 aware `as_of`；禁止共享可变 dict）、`TaskLookup`、`DispatchQuery`、`AttemptIntent` / `TaskAttemptCommand` / `TaskAttemptGrant` / `TaskAttemptResult` / `TaskAttemptRejection`、`GrantRejection`（§4.2.2）、`RetryCommand` / `RetryResult` / `RetryDecision` / `RetryReason`、`StepAttemptCommand` / `StepAttemptResult` / `StepAttemptDecision`、`StepOutcomeKind` / `StepCommitCommand` / `StepCommitResult` / `StepCommitRejection`、`StepExecutionRecord`、`TaskView`、`ReadinessReport`（§3.3）。

跨边界均使用 Pydantic frozen/strict contract，禁止在 API、Worker、Runner 之间传无结构 dict（`LocalStack` 是唯一例外，理由见 §3.3）。

#### 4.2.1 步骤 DTO 与两张真值表

**每个操作各有一张完整真值表，拒绝码不跨操作共享。** 上一版把 `UNKNOWN_STEP` 与 `BUDGET_EXHAUSTED` 同时写进 begin 的散文和 commit 的枚举，又只给 commit 画了 5 行表（枚举有 6 个成员），于是同一个码在两处有不同的字段要求。修法不是补一行，而是先回答"这个判断属于哪个操作"：

- **step 归属与预算是准入判断，只属于 `begin_step_attempt`。** 它决定"这一步能不能开始"。
- **commit 只负责给一行已存在的 in-flight 记录填终局。** 它不再重新做准入。

```python
class StepAttemptCommand(Contract):
    grant: TaskAttemptGrant
    step_id: StrictStr
    # 没有 max_tool_calls、没有 capability_id：预算与 step 身份一律由存储层
    # 在同事务内从不可变的 stored plan 派生，见下方。


class StepAttemptDecision(StrEnum):
    PROCEED = "proceed"
    ALREADY_COMMITTED = "already_committed"
    STALE_FENCING = "stale_fencing"
    NOT_RUNNABLE = "not_runnable"
    UNKNOWN_STEP = "unknown_step"
    BUDGET_EXHAUSTED = "budget_exhausted"


class StepAttemptResult(Contract):
    decision: StepAttemptDecision
    winner: TaskRecord                          # 永远返回，调用方必须采纳
    record: StepExecutionRecord | None = None   # 存储层该 (task_id, step_id) 的当前行
    attempts_used: StrictInt = Field(ge=0)      # 本次判定**之后**任务级已用总数


class StepOutcomeKind(StrEnum):
    """§2.7 两种原子提交形状的判别字段。"""
    TOOL_RESULT = "tool_result"              # 形状 A：gateway 返回了 ToolResult
    MALFORMED_ADAPTER = "malformed_adapter"  # 形状 B：MalformedAdapterResponseError


class StepCommitCommand(Contract):
    grant: TaskAttemptGrant
    step_id: StrictStr
    kind: StepOutcomeKind
    status: StepResultStatus                    # 复用既有枚举，不新造
    evidence: EvidenceEnvelope | None = None
    audit_events: tuple[TraceEvent, ...] = Field(min_length=1)   # 见下方说明

    # model_validator（两条，把两种形状变成互斥且各自完整）：
    #   kind is TOOL_RESULT        => evidence is not None
    #   kind is MALFORMED_ADAPTER  => evidence is None and status is FAILED


class StepCommitRejection(StrEnum):
    STALE_FENCING = "stale_fencing"
    NOT_RUNNABLE = "not_runnable"
    ALREADY_COMMITTED_DIFFERENT = "already_committed_different"
    NO_ATTEMPT_IN_FLIGHT = "no_attempt_in_flight"


class StepCommitResult(Contract):
    committed: bool
    winner: TaskRecord                          # 永远返回，调用方必须采纳
    record: StepExecutionRecord | None = None
    rejection: StepCommitRejection | None = None


class StepExecutionRecord(Contract):
    task_id: StrictStr
    step_id: StrictStr
    attempt_count: StrictInt = Field(gt=0)
    last_fencing_token: StrictInt = Field(gt=0)
    result_status: StepResultStatus | None = None   # None = 已 begin 未 commit
    kind: StepOutcomeKind | None = None             # 与 result_status 同生同灭
    evidence_id: StrictStr | None = None
    commit_digest: StrictStr | None = None          # 与 result_status 同生同灭，见下
    started_at: AwareDatetime
    committed_at: AwareDatetime | None = None
```

**`FAILED` + 无证据必须可机械判别，不能靠散文。** 上一版只有 `status` 与可空的 `evidence`，于是 `(FAILED, evidence=None)` 同时表示两件完全不同的事：一次合法的 malformed adapter 终局，和一次**调用方漏传了本该存在的证据**的错误提交。正文写着"只有形状 B 可以无证据"，但模型验证不了这句话，存储层也就只能照单全收——原子性会忠实地把一条缺证据的 `FAILED` 步骤永久写进 aggregate，而且事后从数据上分不出它属于哪一种。

因此加一个显式判别字段 `kind`，由 validator 让非法组合在构造时就失败。这样 `ERROR` 与 `INDETERMINATE` 走的普通 `FAILED`（形状 A，必须带零行证据 + 结构化 error）不可能被写成 `evidence=None`，反过来 malformed 也不可能被塞进一份伪造的证据。命令层的完整判据与记录层的对应关系统一写在 §4.2.1（那里逐行核对过五种合法状态），此处不另列一份——两处各写一份 validator 清单正是上一版让 `SKIPPED` 从命令层漏进存储事务的原因。

`StepExecutionRecord` 同样持久化 `kind`：让这一行**自描述**。否则恢复时读回一条 `FAILED` + 无 `evidence_id` 的行，仍然分不清"当初是 malformed"还是"当初漏写了证据"，§2.6 的重建就要靠猜。

**`StepCommitRejection` 从 6 个成员收到 4 个，两处删除各有理由：**

- **删 `UNKNOWN_STEP`。** commit 一个从未 begin 的 step，无论它在不在计划里，结果都是"没有对应的 in-flight 行"——`NO_ATTEMPT_IN_FLIGHT` 已经完整覆盖。保留它只会制造"同一状态两个码"。
- **删 `BUDGET_EXHAUSTED`。** 预算在 begin 处已经扣过，reservation 已经发出，工具**已经真的被调用了**。此时因为预算拒绝提交，会让这一步永远停在 in-flight：既拿不到终局，又按 §2.6 保守地永久占着一次配额，`failed_steps` 里也没有它。这是把一次成功的取数变成一条无法收敛的记录。**因此：begin 之后的 commit 一律不再检查预算**，即使这期间预算被其它并发路径用满。用例正例："begin 时恰好用满预算（`attempts_used == max_tool_calls`）→ 仍然 `PROCEED` → commit 成功"。

**`record` 与 `winner` 的规则各只有一条，不逐拒绝码列例外：**

- `winner` 在两个 Result 上都是**必填**——调用方在任何结局下都必须能采纳存储层的当前任务事实。上一版正文说 `StepCommitResult`"始终携带 task winner"而模型里没有这个字段，是纸面与模型的直接矛盾。
- `record` 的定义统一为：**存储层中该 `(task_id, step_id)` 的当前行；不存在则为 `None`**。它不是"调用方递来的那份"，也不因拒绝码不同而改变含义。

由这两条派生出 model_validator（全部可判定，无例外分支）：

```
committed  <=>  rejection is None
committed  =>   record is not None            # 刚写入的那一行必然存在
rejection is NO_ATTEMPT_IN_FLIGHT  =>  record is None
```

注意**反向不成立**：`record is None` 不蕴含 `NO_ATTEMPT_IN_FLIGHT`。一个已失效的 grant 对一个从未 begin 的 step 调 commit，按下面的优先级先撞 `STALE_FENCING`，此时同样没有行可返回。上一版把 `STALE_FENCING` / `NOT_RUNNABLE` 写成"record 必填"，正是漏掉了这条路径——与 `NO_ATTEMPT_IN_FLIGHT` 当初被写成必填是同一个错误，只是换了两行。反例用例逐条覆盖：**无 step row 的 `STALE_FENCING`**、**无 step row 的 `NOT_RUNNABLE`**。

`StepAttemptResult` 同理：`ALREADY_COMMITTED ⇒ record is not None`；`UNKNOWN_STEP` / `BUDGET_EXHAUSTED` / `STALE_FENCING` / `NOT_RUNNABLE` 下 `record` 可为 `None`（未 begin 过就没有行）；`PROCEED` 时 `record` 是刚写入或刚接管的那一行，必非 `None`。`attempts_used` 语义拍板为**判定之后**的任务级总数：`PROCEED` 时它已经含本次，`BUDGET_EXHAUSTED` 时它等于上限——这样 "`attempts_used <= max_tool_calls`" 是一条对全部 decision 都成立的不变量，不需要按分支解释。

**两个操作的完整真值表**（实施时逐行一条用例，缺一行即视为该契约未被证明可执行）：

`begin_step_attempt`，判定优先级 `STALE_FENCING` > `NOT_RUNNABLE` > `ALREADY_COMMITTED` > `UNKNOWN_STEP` > `BUDGET_EXHAUSTED` > `PROCEED`：

| # | 存储状态 | decision | `record` | `attempts_used` 变化 |
| --- | --- | --- | --- | --- |
| 1 | `grant_is_current` 返回 `LEASE_NOT_HELD` / `STALE_FENCING`（§4.2.2） | `STALE_FENCING` | 有行则给，无行为 `None` | 不变 |
| 2 | `grant_is_current` 返回 `TERMINAL_PROTECTED` / `STATUS_NOT_ALLOWED` | `NOT_RUNNABLE` | 同上 | 不变 |
| 3 | step 行已有 `result_status` | `ALREADY_COMMITTED` | **必填**（既存终局行） | 不变 |
| 4 | `step_id` 不在 stored plan 的 `steps` 里 | `UNKNOWN_STEP` | `None` | 不变 |
| 5 | 任务级已用 attempts == `plan.budget.max_tool_calls` | `BUDGET_EXHAUSTED` | `None` | 不变（等于上限） |
| 6 | 无 step 行 | `PROCEED` | 新写入的 in-flight 行 | +1 |
| 7 | 有 in-flight 行且 **step 行**的 `last_fencing_token == grant.fencing_token` | `PROCEED` | 原行，不改计数 | 不变 |
| 8 | 有 in-flight 行且 `grant.fencing_token` > step 行的 `last_fencing_token`（新 grant 接管） | `PROCEED` | 行，`last_fencing_token` 更新为新 token | +1 |

先判 fencing 是因为一个已经不是持有者的调用方，它看到的任何其它结论都不可信；与 `classify_transition` 先判终态保护、再判 fencing、最后判版本的排序逻辑同源。

**两个 token 的比较对象不同，不能混。** 持有者判定（第 1 / 2 行）走 §4.2.2 的 `grant_is_current`，它比的是 **task 行**的状态与三个租约字段——那是"谁是当前合法写入者"的唯一真源；计数规则（第 7 / 8 行）比的是 **step 行**的 `last_fencing_token`——那回答的是"这一行上次是谁碰的"。用 step 行的 token 做持有者判定会让一个从未 begin 过的 step 因为没有行而无从判定；用 task 行的 token 做计数会让同一次 grant 内的重入被当成接管。

第 7 / 8 行是计数规则：**同一 token 重入不计**（网络重试、协程重入），**新 grant 接管才计**（在 begin 后崩溃的尝试因此保守地消耗预算）。

**预算只约束会 +1 的那两行。** 第 5 行的优先级有一个必须写明的限定：`BUDGET_EXHAUSTED` **仅当本次判定会使 `attempts_used` 增加时才适用**（即第 6 行"无行"与第 8 行"新 grant 接管"）。第 7 行的同 token 重入不消耗预算，因此即使任务级 attempts 已经等于上限，它仍然返回 `PROCEED`——否则一次网络重试就会把一个已经合法开始、工具可能已经在跑的步骤打成预算拒绝，而它本来就不占额度。这与 §4.2.1 开头"commit 阶段不再检查预算"是同一条判据的两半：**已经付过账的动作不再被收第二次费**。用例两条：满预算下的同 token 重入 → `PROCEED`；满预算下的新 grant 接管 → `BUDGET_EXHAUSTED`。

`commit_step_result`，判定优先级 `STALE_FENCING` > `NOT_RUNNABLE` > `NO_ATTEMPT_IN_FLIGHT` > `ALREADY_COMMITTED_DIFFERENT` > 提交：

| # | 存储状态 | `committed` | `rejection` | `record` |
| --- | --- | --- | --- | --- |
| 1 | `grant_is_current` 返回 `LEASE_NOT_HELD` / `STALE_FENCING`（§4.2.2） | `False` | `STALE_FENCING` | 当前行或 `None` |
| 2 | `grant_is_current` 返回 `TERMINAL_PROTECTED` / `STATUS_NOT_ALLOWED` | `False` | `NOT_RUNNABLE` | 当前行或 `None` |
| 3 | 无 step 行 | `False` | `NO_ATTEMPT_IN_FLIGHT` | **`None`** |
| 4 | 行已有 `result_status`，内容不同 | `False` | `ALREADY_COMMITTED_DIFFERENT` | **既存**那一行 |
| 5 | 行已有 `result_status`，内容逐字节相同 | `True` | `None` | 既存那一行（幂等成功） |
| 6 | in-flight 行，token 相符 | `True` | `None` | 刚写入的终局行 |

第 5 行与 `PlanStore.save`、`EvidenceLedger.append` 的既有形状一致——重试不该被当成篡改。

**真值表还必须有唯一的 Runner 消费动作。** 只证明存储层能返回这些 decision/rejection 不够；如果 Runner 把 `BUDGET_EXHAUSTED` 当作普通跳过、把 `ALREADY_COMMITTED` 当作重新调用工具，存储契约全绿而系统行为仍然错误。消费闭表如下，任何未列分支都视为不变量破坏：

| 结果 | Runner 唯一动作 | 禁止发生 |
| --- | --- | --- |
| begin `PROCEED` | 使用返回的 winner/record，进入本步骤的 Gateway 调用；这是**唯一**允许调用 Gateway 的分支 | 预先写终态或重复 begin |
| begin `ALREADY_COMMITTED` | Gateway 0 次；从 record 重建本步骤结果：`FAILED` / `TIMEOUT` 加入 `failed_steps` 且令 `degraded=True`，`OK` 不加入；然后继续按既有条件求值 | 重跑工具、重复 Evidence/audit |
| begin `BUDGET_EXHAUSTED` | Gateway 0 次；用原 grant 终态化为 `FAILED` + `budget.tool_calls_exhausted`，只写对应生命周期审计 | 创建 step/Evidence 或把它当成功跳过 |
| begin `UNKNOWN_STEP` | Gateway 0 次；用原 grant 终态化为 `FAILED` + `recovery_drift`，只写对应生命周期审计 | 猜测步骤、兼容旧计划 |
| begin `STALE_FENCING` | 进入 loser 路径，停止当前执行树，采纳 winner | 任意 task/step/Evidence/audit 写入 |
| begin `NOT_RUNNABLE` | 采纳 winner 并停止；若 winner 已终态只返回其投影 | 强行迁移或继续执行 |
| commit 成功（含同命令重放） | 采纳 winner/record；按已提交 status 更新本次内存重建状态并继续 | 再写一份 Evidence/audit |
| commit `STALE_FENCING` | 与 begin 的 loser 路径相同 | 任意后到写入 |
| commit `NOT_RUNNABLE` | 与 begin 的 winner 路径相同 | 覆盖 winner |
| commit `NO_ATTEMPT_IN_FLIGHT` | 系统不变量 fail-stop；任务与三类子行保持原样 | 自动补 begin、任务级 retry 或终态化 |
| commit `ALREADY_COMMITTED_DIFFERENT` | 保留首次 commit，系统不变量 fail-stop；任务与三类子行保持原样 | 覆盖首次事实或把差异当幂等成功 |

这张消费表与两张存储真值表在同一个参数化 suite 中逐行绑定；每行都断言 Gateway、终态迁移、Evidence 写入和 audit 写入的精确调用次数。这样删除任一分派分支都会直接转红，不会等到 Compose 才暴露。

**但"逐字节相同"必须有可持久化的依据，否则第 4 / 5 两行无法区分。** 上一版要求存储层比较"内容是否逐字节相同"，而 `StepExecutionRecord` 上只有 `status`、`kind`、`evidence_id` 三样：`evidence_id` 是 `f"{task_id}:{step_id}"`（§4.2.1 关联不变量），对同一个步骤**恒定不变**，因此它证明不了证据内容没被换过；`audit_events` 则完全没有落在这一行上。想从 `task_audit_events` 反推更不行——M4 明确允许同一 task 重复 `event_id`（`test_audit_seq_is_scoped_per_task`，§5.1），审计表里分不出"哪几条属于这一次提交"。结果是：一次改了证据内容或审计集合的重放，会被判成幂等成功并静默丢弃。

因此在 step 行上持久化 `commit_digest`，与 §2.3 的两个摘要同一写法、同一强度声明（**一致性 checksum，不是抗攻击者的完整性证明**）：

```python
def step_commit_digest(command: StepCommitCommand) -> str:
    payload = {
        "status": command.status.value,
        "kind": command.kind.value,
        "evidence": None if command.evidence is None else dump_contract(command.evidence),
        # 审计集合与顺序无关，但成员必须完整：逐条摘要后排序，子集/超集都会改变结果
        "audit": sorted(
            content_digest(canonical_json(dump_contract(e)).decode("utf-8"))
            for e in command.audit_events
        ),
    }
    return content_digest(canonical_json(payload).decode("utf-8"))
```

审计那一项用**排序后的逐条摘要列表**而不是整体序列化：事件的产生顺序在重放时可能不同（协程调度），把顺序算进去会把合法重放误判为篡改；而用集合又会让"同一条事件出现两次"消失。排序的多重集两头都挡住。

`commit_digest` 与 `result_status` 同生同灭（in-flight 行为 `None`，第 1 条派生规则已经覆盖它）。第 4 / 5 行的判据因此变成一次定长字符串比较：相同 → 幂等成功，不同 → `ALREADY_COMMITTED_DIFFERENT`。

**五条反例逐条**（这是"响应丢失后重放"这类场景唯一的证明方式）：原命令原样重放 → 幂等成功且不产生第二份 Evidence；只改 `status` → `ALREADY_COMMITTED_DIFFERENT`；只改 Evidence 的一个 fact 值 → 同上；`audit_events` 取**真子集** → 同上；`audit_events` 取**超集**（多一条事件）→ 同上。后两条是排序多重集设计的直接承重点。

#### 4.2.2 所有 fenced 写入共用一条「当前有效 grant」判据

上面两张表把持有者判定写成了"比 token 大小"，这不够——它漏掉了 M4 已经解决过的两件事，而漏掉的方式恰好是 M4 在 `_check_fencing` 的 docstring 里点名要避免的那一种。

**漏掉的第一件：租约过期但尚未被接管。** `decisions.py::_check_fencing` 明确写着"按任务是否曾被租出判定，而非租约此刻是否 live"会留下缺口，因此它在 `ever_leased` 之后立刻调用 `lease_is_live`：**曾被租出但租约已过期时，无论是否带正确 token 一律 `LEASE_NOT_HELD`**。而我上一版的 step 表只比 token——在租约刚过期、新 Worker 还没来得及接管的那个窗口里，旧 grant 的 token 与行上 token **仍然相等**，于是它可以照常 begin、commit、写证据、写审计。这个窗口不是理论值：它正好等于"心跳停了到下一个 Worker 轮询到"的时长。

**漏掉的第二件：状态不是 `RUNNING`。** 我把 `NOT_RUNNABLE` 定义成"终态或不在 §2.6 闭表内"，而闭表含 `CREATED` / `PLANNING`——于是一个还没进入 `RUNNING` 的任务可以写 step 行和证据。步骤只在 `RUNNING` 中发生，这一条本该是准入的一部分。

因此在 `decisions.py` 增加一个纯函数，**三个 fenced 写入路径逐字共用**，不各写一份：

```python
def grant_is_current(
    record: TaskRecord,
    grant: TaskAttemptGrant,
    *,
    now: _dt.datetime,
    allowed_statuses: frozenset[TaskStatus],
) -> GrantRejection | None:
    """None 表示这个 grant 此刻仍是该任务的唯一合法写入者。"""
    if record.status in TERMINAL_STATUSES:
        return GrantRejection.TERMINAL_PROTECTED
    if record.status not in allowed_statuses:
        return GrantRejection.STATUS_NOT_ALLOWED
    if not lease_is_live(record, now=now):          # 复用 M4 既有判据
        return GrantRejection.LEASE_NOT_HELD
    if record.lease_owner != grant.lease.owner:
        return GrantRejection.STALE_FENCING
    if record.fencing_token != grant.fencing_token:
        return GrantRejection.STALE_FENCING
    if record.attempt_number != grant.attempt_number:
        return GrantRejection.STALE_FENCING
    return None
```

**`allowed_statuses` 是参数而不是写死的 `{RUNNING}`，因为三个调用点管的不是同一段生命周期。** 步骤只发生在 `RUNNING` 里，所以 `begin_step_attempt` / `commit_step_result` 传 `{RUNNING}`。但 `schedule_retry` 管的是**整次任务尝试**，而尝试从领取的那一刻就开始了——一个在 `start()` 的 `CREATED -> PLANNING` 之间失败的任务，此刻状态还不是 `RUNNING`。若把 `{RUNNING}` 写死，这类失败拿到的会是拒绝，Worker 无法安排重试，只能让租约自然到期；于是**它每次都被重新领取、每次都在同一处失败、而 `task_failure_count` 一次都不增加**——一个永不收敛且永不触发 `retry_exhausted` 的循环。因此 `schedule_retry` 传 `DISPATCH` 的可领取集合 `{CREATED, PLANNING, RUNNING}`。

| 调用点 | `allowed_statuses` | 理由 |
| --- | --- | --- |
| `begin_step_attempt` / `commit_step_result` | `{RUNNING}` | 步骤只在 `RUNNING` 中发生 |
| `schedule_retry` | `{CREATED, PLANNING, RUNNING}` | 尝试从领取即开始；规划期失败同样要消耗失败预算 |

一条用例专门覆盖这条差异：在 `PLANNING` 状态下调用 `schedule_retry` 必须成功且 `task_failure_count` +1，而同状态下调用 `begin_step_attempt` 必须 `NOT_RUNNABLE`。缺这条时，一个把 `{RUNNING}` 写死的实现只会让规划期失败悄悄变成无限重试，所有现有用例照常全绿。

**不比较 `grant.lease.expires_at`。** 这一条必须写明，否则实现时几乎一定会顺手加上：heartbeat 的 `renew_lease` **保持同一 token、只前移 `expires_at`**，因此 grant 手里那份 `expires_at` 在第一次续租之后就已经是旧值。拿它做判定会让每一个跑得比 TTL 长的任务在自己续租之后判自己失效。权威永远是**存储行**上的三个租约字段，grant 只提供 owner / token / attempt 三个对照物。

`owner` 与 `attempt_number` 的比较在数学上是冗余的（token 来自单调序列，token 相等蕴含同一次 acquire，因而 owner 与 attempt 也相同），但仍然要写：它让 grant 的这两个字段成为承重字段，一个把 owner 填错的实现会当场失败而不是等到某次并发才暴露。这与 §2.5"存在性证明不了说的是同一件事"是同一条理由。

调用点：`begin_step_attempt`、`commit_step_result`、`schedule_retry`。三者都在行锁内、用**刚读到的那一行**调用它，返回非 `None` 即按下表映射为各自的拒绝码：

| `GrantRejection` | begin 的 decision | commit 的 rejection | retry 的 decision |
| --- | --- | --- | --- |
| `TERMINAL_PROTECTED` | `NOT_RUNNABLE` | `NOT_RUNNABLE` | `TERMINAL_PROTECTED` |
| `STATUS_NOT_ALLOWED` | `NOT_RUNNABLE` | `NOT_RUNNABLE` | `STALE_FENCING` |
| `LEASE_NOT_HELD` | `STALE_FENCING` | `STALE_FENCING` | `STALE_FENCING` |
| `STALE_FENCING` | `STALE_FENCING` | `STALE_FENCING` | `STALE_FENCING` |

`_finish` 的终态迁移不走这张表：它经 `transition` → `classify_transition` → `_check_fencing`，M4 已经把 live-lease 与 token 判定做全了。但 `_finish` 必须传 `grant.lease.fencing_token`（§3.5），否则那条判定的两侧同源。

**六条反例，每条都必须写不进任何 step / evidence / audit 行**：任务在 `CREATED`；任务在 `PLANNING`；租约**恰好过期**（`lease_expires_at == now`，边界取"过期"，与 `lease_is_live` 的 `>` 严格一致）；owner 不同；旧 token；未来 token（比行上更大——这只可能来自伪造，必须与"旧 token"同样被拒，不能只判 `<`）。

**预算与 step 身份不能由调用方自报。** 把 `max_tool_calls` 放进命令、且不要求 `step_id` 属于已持久化的计划，会打开两个洞：一个写错或被构造的调用方可以把预算从 2 报成 999，或为一个根本不存在于计划里的 step 建 journal 行，甚至把属于别的 capability 的 Evidence 写进当前任务的 aggregate；原子性会忠实地把这些一起提交。因此 `begin_step_attempt` 在**同一个事务里**读取该任务的 stored plan（`task_plans` 行在 `start()` 的第一步就已写入且此后不可变），自己派生：

- `max_tool_calls` ← `plan.budget.max_tool_calls`；
- step membership ← `step_id` 必须在 `plan.steps` 里，否则 `UNKNOWN_STEP`；
- `capability_id` / `capability_version` ← 取自 plan，用于校验随后 `commit_step_result` 递来的 Evidence 是否属于同一能力版本。

内存实现让 TaskStore 与 PlanStore 共享同一份内部 state（与 EvidenceLedger 同一处理，§4.6），但**公共端口仍然分开**——共享的是事务边界，不是契约。PostgreSQL 实现直接在同一事务内 `SELECT` `task_plans`，它本来就已 import 该表。计划尚未落库时（理论上不可达，因为 `start()` 先存计划）返回 `NOT_RUNNABLE`，不猜测预算。

**`StepExecutionRecord` 的合法状态是一张闭表，不是一组独立可空字段。** 只列字段类型会让"`TIMEOUT` 但没有 Evidence""有 `committed_at` 却没有 status""`audit_events` 为空"这些非法组合全部可构造，smoke 里 `attempt_count == 2` 的断言也就没有唯一实现：

| # | `result_status` | `kind` | `committed_at` | `commit_digest` | `evidence_id` | 同事务写入的 audit 事件数 | 含义 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `None` | `None` | `None` | `None` | `None` | —— | in-flight：begin 过、未 commit（崩溃点的持久证据） |
| 2 | `OK` | `TOOL_RESULT` | 必填 | 必填 | **必填** | >= 1 条 | 形状 A 成功 |
| 3 | `TIMEOUT` | `TOOL_RESULT` | 必填 | 必填 | **必填** | >= 1 条 | 形状 A 超时（零行证据 + limitations） |
| 4 | `FAILED` | `TOOL_RESULT` | 必填 | 必填 | **必填** | >= 1 条 | 工具返回 ERROR / INDETERMINATE（零行证据 + 结构化 error） |
| 5 | `FAILED` | `MALFORMED_ADAPTER` | 必填 | 必填 | **必须为 `None`** | >= 1 条 | malformed adapter，无 `ToolResult` |
| — | `SKIPPED` | —— | —— | —— | —— | —— | **禁止入库**（§2.6），不是第六种合法状态 |

**这是五种合法状态加一条明确禁止的状态，不是"六种合法组合"。** 全文按这个计数表述；把禁止行算进合法数会让"逐行可构造"的验收条目要求实现去构造一条它必须拒绝的行。

**这条不变量被我改错过两次，两次都是把双向蕴含挂在了错误的一侧，所以下面逐行核对而不是靠直觉。** 第一次写的是 `evidence_id is None ⟺ kind is MALFORMED_ADAPTER`，它把第 1 行（in-flight，两者同时为 `None`：左边成立、右边不成立）判成非法，于是 `begin_step_attempt` 写不出任何 in-flight 行——而那一行是崩溃点的唯一持久证据。第二次的修法把 `evidence_id` 拉进了"同生同灭"那条链（`result_status ⟺ committed_at ⟺ kind ⟺ evidence_id`），结果换成第 5 行（malformed 终局：前三项非空、`evidence_id` 必须为 `None`）不可构造——**malformed 是形状 B 的唯一终局形态**，它不可构造等于 §2.7 的形状 B 整条路径没有落点。

根因是把两件正交的事塞进了一条链：**"这一行有没有终局"** 与 **"这个终局带不带证据"**。前者是四项同生同灭，后者是一条独立的判据。`commit_digest` 属于前者（它是终局内容的摘要，没有终局就没有摘要），`evidence_id` 属于后者。分开写，两行都合法：

1. **终局链**：`result_status is None ⟺ committed_at is None ⟺ kind is None ⟺ commit_digest is None`（四项同时为 `None` 即第 1 行 in-flight；同时非空即第 2–5 行）。`evidence_id` **不在这条链上**；
2. **证据判据**：`evidence_id is not None ⟺ kind is TOOL_RESULT`；
3. `kind is TOOL_RESULT ⇒ result_status ∈ {OK, FAILED, TIMEOUT}`；
4. `kind is MALFORMED_ADAPTER ⇒ result_status is FAILED`（`evidence_id is None` 已由第 2 条蕴含，不重复表述——两处写同一条约束就是两份迟早漂移的表述）；
5. `result_status is SKIPPED` **一律拒绝**（§2.6：它根本不该进 journal，因此不能只靠"没人会写"，要显式拒绝）。

逐行核对：第 1 行（`None`×5）满足 1（四项皆 `None`）、2（两边皆假）、3/4（前件假）、5；第 2/3/4 行满足 1（四项皆非空）、2（两边皆真）、3；第 5 行满足 1、2（两边皆假）、4、5。五行全部合法，且**每一行都由至少一条规则与其余四行区分**。

**同一组约束必须同时落在 `StepCommitCommand` 上，否则非法组合只是晚一步进库。** 记录层的规则只在写行的那一刻生效；命令层不设防时，一个 `(kind=TOOL_RESULT, status=SKIPPED, evidence=<有>)` 的命令可以合法构造、通过存储层的 fencing 与 in-flight 检查、算出 `commit_digest`，直到构造 `StepExecutionRecord` 才炸——此时事务里已经写了 Evidence 与 audit，异常还会按 §4.7 被归成系统性故障，把一次调用方的形状错误升级成进程退出。因此 `StepCommitCommand` 带同样的三条 validator：`kind is TOOL_RESULT ⇒ status ∈ {OK, FAILED, TIMEOUT}`、`kind is TOOL_RESULT ⟺ evidence is not None`、`status is SKIPPED` 一律拒绝。**两层不是重复**：命令层挡调用方，记录层挡存储实现自己算错——`commit_digest` 与 `committed_at` 都是存储层现填的，命令层看不见它们。

**非法形状只有一个可执行真源。** 在 `tests/suites/task_store.py` 定义命名参数矩阵 `STEP_COMMIT_INVALID_CASES`，每项包含 `case_id`、目标层（command/record）与构造覆盖；所有本地/集成绑定和本计划其它章节只引用这个名字，不再手写“有 N 条”。矩阵至少覆盖：终局缺 `commit_digest`；in-flight 携带 `commit_digest`；`TOOL_RESULT + SKIPPED`；`TOOL_RESULT + evidence=None`；`MALFORMED_ADAPTER + evidence`；`MALFORMED_ADAPTER + status!=FAILED`，并在适用时同时落到 command/record 两层。正例矩阵单独包含第 5 行 malformed 终局**可构造**，不得混进 invalid cases。新增/删除 case 只改这一张矩阵，绑定元测试断言两实现消费的是同一对象，避免散落计数再次漂移。

**跨对象关联不变量**（与 §2.5 同理：存在性证明不了"说的是同一件事"）：

- 所有子对象的 `task_id` 必须同值：`grant.lease.task_id`、`evidence` 所属任务、每一条 `audit_events[i].task_id`；
- `evidence.evidence_id == evidence_id(task_id=grant.lease.task_id, step_id=command.step_id)`——用**同一个**确定性函数重算并比对，不另拼一份字符串。**但这个函数必须先下沉**：它现在在 `evidence/builder.py`，而 `test_module_layering.py` 给 `persistence` 的允许集是 `{contracts, planning, persistence}`，**不含 `evidence`**；契约层同理。照上一版写法实施，存储层第一行 import 就打红分层护栏，然后大概率有人去放宽 `persistence` 的允许集——用一条纯字符串拼接换掉一条分层边界。正确做法是把这个**纯 ID 派生函数**（`f"{task_id}:{step_id}"`，无 I/O、无依赖）移到已经存在的 `contracts/evidence.py`，`evidence/builder.py` 从那里 import 并继续对外导出（调用点一个不改，`evidence` → `contracts` 本来就是合法方向）。这样 builder、contracts validator、persistence 三处共用一份，谁都不需要新的白名单例外。一条用例断言 `evidence/builder.evidence_id is contracts.evidence.evidence_id`（同一对象，不是两份实现）；分层用例保持原样不放宽。
- 每条 `audit_events[i]` 的 `.step_id == command.step_id`、`.attempt_number == grant.attempt_number`；
- `audit_events` 内 `event_id` 互不相同（UUID，见 §5.1）。

存储层在行锁内**再复核一次**：用刚读到的 task row 与 step row 比对 grant 的 owner/token/attempt，而不是拿调用方递来的值自比。缺了这一层，一个错误实现可以把任务 A 的 Evidence 与 audit 原子写进任务 B 的 aggregate。

**`StepResultStatus` 在 M5 产生 `OK` / `FAILED` / `TIMEOUT`，只有 `SKIPPED` 不产生**（理由见 §2.6，按 `RENDER_REF` 先例显式钉死于 §12），并由用例分别断言"超时确实落成 `TIMEOUT` 而非 `FAILED`"与"任何路径都不写 `SKIPPED`"。

### 4.3 `begin_task_attempt` 与失败预算

默认配置（全部为 `Settings` 字段并带 validator，实施时不再临场取值）：

| 配置 | 默认值 | 约束 |
| --- | --- | --- |
| lease TTL | 60 秒 | > 0 |
| heartbeat interval | 10 秒 | > 0 且 `< lease_ttl / 2` |
| task failure limit | 3 | > 0 |
| infrastructure backoff | 指数 1–30 秒 | base > 0、cap >= base |
| continuous infrastructure failure window | 15 分钟 | > backoff cap |
| worker poll interval | 1.0 秒 | > 0，且 `<= lease_ttl / 4` |
| dispatch batch limit | 10 | > 0，且有上界 100 |
| API request body limit | 131072 字节（128 KiB） | 见下方推导，< 1 MiB |
| API 容器内监听 | `0.0.0.0:8000` | 合法 IP 字面量；见下方说明 |
| API 宿主发布 | `127.0.0.1:8000:8000` | Compose 契约测试断言每个 published port 都以 `127.0.0.1:` 开头 |

**body limit 必须容得下 `text` 上限的最坏合法编码，否则它会替字段级校验做出错误拒绝。** 推导：`RequestEnvelope.text` 上限 8192 **字符**；一个字符在 JSON 里的最坏合法编码是非 BMP 字符在 `ensure_ascii=True` 下的代理对转义 `\uXXXX\uXXXX` = **12 字节**；8192 × 12 = 98,304，加上 `idempotency_key`（≤ 200 字符，同样可被转义）与 JSON 包装，最坏约 100 KiB。上一版取的 64 KiB **低于这个值**，于是任何用默认 `json.dumps` 的第三方客户端提交一个满长度的 emoji 文本都会拿到 413——而那是一个应当被接受的请求。

因此取 **128 KiB**：严格大于最坏合法编码，且仍远低于 1 MiB。修 CLI 的 `ensure_ascii` 只解决我们自己这一个客户端，不解决 API 对其他 conformant 客户端的行为，两处都要改。上限的作用是挡住"在 Pydantic 之前把内存读爆"，不是替代字段级校验——两层各管一件事。

**边界用例两条**：8192 个非 BMP 字符 + `ensure_ascii=True` 序列化 → 必须 `202`（证明上限够用）；构造一个刚好超过 128 KiB 的 body → 必须 `413` 且在 Pydantic 之前被拒（证明上限仍然生效）。

**"容器内监听"与"宿主暴露面"是两件事，安全约束只能落在后者。** Docker 的端口发布通过 DNAT/userland proxy 把宿主端口转发到**容器的网络接口地址**，不经过容器内的 loopback；因此容器内的 uvicorn 若绑 `127.0.0.1`，`127.0.0.1:8000:8000` 这条发布规则会转发到一个没人监听的地址，从宿主访问必然连接被拒。这个错误只会在 Compose smoke 才暴露，而那时 API/CLI 的全部契约测试（走 ASGITransport，根本不开端口）已经全绿——是一条能一路绿到最后一步的缺陷。

正确的切分：容器内绑 `0.0.0.0:8000`（只在 Compose 私有网络内可达，postgres 不发布任何宿主端口，worker 没有入站端口）；宿主侧只发布 `127.0.0.1:8000:8000`。安全断言因此从 Settings 移到 `tests/contract/test_compose_contract.py`（该文件的完整断言清单见 §6.3）。Settings 侧只校验 bind host 是合法 IP 字面量。

`begin_task_attempt` 的原子行为：

1. 锁定 task row 并读取当前 winner；
2. 终态返回 `TERMINAL_PROTECTED`；
3. 当前状态不在 `command.intent` 的可领取集合内（§2.5 那张表）返回 `NOT_DISPATCHABLE`——`DISPATCH` 遇 `AWAITING_APPROVAL`、`APPROVAL_RESUME` 遇 `RUNNING` 都走这一条；
4. `next_attempt_at > now` 返回 `RETRY_NOT_DUE`；
5. live lease 返回 `LIVE_LEASE`；
6. 若 `task_failure_count >= limit`，在同一事务把 task 迁移为 `FAILED`，version + 1，写闭集 terminal reason `retry_exhausted`，**同事务写入对应的终态 audit 事件并通过 `committed_audit_events` 原样返回**（§5.1），不分配 lease，返回 `RETRY_EXHAUSTED` 和 `FAILED` winner；
7. 读取 submission 行并在锁内完成 §2.3 的**三项**校验：`submission_digest` 与行上摘要相等、`request_dedup_digest(envelope, context)` 与行上 `request_digest` 相等、以及 scope 关系（`context_matches_envelope` + winner 三元组）。任一不过：在同一事务把 task 迁移为 `FAILED`、写闭集 reason `submission_invariant_violation`、写终态 audit 事件并通过 `committed_audit_events` 原样返回、**不发放 grant**，返回 `SUBMISSION_INVARIANT_VIOLATION`；
8. 全部通过后递增 fencing token 与 `attempt_number`、写入由 `command.owner` / `command.ttl_seconds` 决定的租约、返回 `applied=True` 的完整结果（grant + winner + submission）。

第 6 / 7 步写终态时**必须同事务写入非空 audit 事件**，其 `trace_id` 取 `command.trace_id`、`attempt_number` 取 `winner.attempt_number`（理由见 §4.1.1 末尾），且返回值中的 `committed_audit_events` 必须与实际插入行逐字段相等；成功 grant 与其它 rejection 的该字段必须为空。

第 7 步放在锁内、grant 之前，是为了让"损坏 submission"有一个闭合出口：若要求"先有 fencing owner 才能终态化"，一条损坏任务就会永远在"不能执行也不能终态"之间循环，被反复 dispatch。**通则**：grant 之前能确定的终态由 `begin_task_attempt` 在锁内原子写入；只有取得 grant 后才能发现的终态由 grant 持有者用自己的 token 写入。

### 4.4 `schedule_retry` 与租约交接

现有数据库 CHECK 要求 `lease_owner`、`lease_expires_at`、`fencing_token` 全部为空或全部有值，且终态迁移没有清租约语义。M5 不增加模糊的 `release_lease`。

```python
class RetryReason(StrEnum):
    """闭集码，不是异常原文。当前**只有一个成员**，见下方说明。"""
    UNCLASSIFIED_ERROR = "unclassified_error"


class RetryCommand(Contract):
    grant: TaskAttemptGrant
    next_attempt_at: AwareDatetime
    reason: RetryReason
    audit_events: tuple[TraceEvent, ...] = Field(min_length=1)   # 与状态改动同事务，见 §5.1


class RetryDecision(StrEnum):
    SCHEDULED = "scheduled"                  # 本次调用写入了重试
    ALREADY_SCHEDULED = "already_scheduled"  # 本 attempt 已经调度过同一条命令
    COMMAND_MISMATCH = "command_mismatch"    # 同 attempt、不同命令内容
    STALE_FENCING = "stale_fencing"
    TERMINAL_PROTECTED = "terminal_protected"


class RetryResult(Contract):
    decision: RetryDecision
    winner: TaskRecord               # 永远返回，调用方必须采纳
```

**用单一 `RetryDecision` 闭集，不用 `scheduled: bool` + 可空 rejection。** 这两者一起出现时，`ALREADY_SCHEDULED` 的 `scheduled` 该是 `True` 还是 `False` 没有答案：它既不是"本次写入了"（`False`），也不是一次失败（`True` 的语义）。这与 §4.2.1 给 step commit 修掉的是同一个形状——我在那里删掉了模糊的布尔、在这里却把它留下了。一个枚举字段让每种结局各占一格，调用方按格分派，不需要在两个字段之间推理。

`SCHEDULED` 与 `ALREADY_SCHEDULED` 对调用方的后续动作相同（安心退出，任务已排好重试）；`COMMAND_MISMATCH` / `STALE_FENCING` / `TERMINAL_PROTECTED` 三者都要求停止并采纳 winner。这个分组由一条用例固定，避免实现把它们混成"成功 / 失败"两类。

`reason` 必须是闭集枚举而不是 `StrictStr`：它会进 `retry_command_digest`、进审计行、并被用来判断两次重放是不是同一条命令，一个自由字符串会让"把异常原文顺手塞进来"成为最省事的写法，而那正是 `detail` 恒为空这条规则要挡的东西。

**`RetryReason` 只有一个成员，这是刻意的。** 按 §3.5 的闭表，唯一会走到 `schedule_retry` 的分支是"未分类的普通 `Exception` → `TASK_FAILURE` 重试"：`INDETERMINATE` 是终态、准入拒绝是终态、基础设施故障不碰任务。多写一个成员就是多写一条没有调用方的分支——与下面删掉 `RetryClass` 是同一条判据。将来要加成员，必须先有真实调用链，不能反过来。

**删除 `RetryClass`。** 它只有两个成员，而 `INFRASTRUCTURE` 那一支**没有调用方**：按 §3.3 与 §3.5，基础设施故障时 Worker 只做进程级退避、不碰任务，让旧租约自然到期（§4.4 末尾那条"数据库不可用时 `schedule_retry` 可能根本提交不了"说的就是这件事）。保留一个没有调用链的枚举成员，等于在热路径上留一条永远无法被用例覆盖的分支，将来还会有人照着它去"补齐"一条不该存在的调用。删掉之后语义反而更清楚：**`schedule_retry` 的存在本身就意味着一次任务失败，它一定增加 `task_failure_count`**。三套计数（`attempt_number` / `task_failure_count` / 进程级故障窗口）的分离不受影响——基础设施故障根本不进这个方法。

**幂等成功必须可达，因此判定顺序必须改。** 上一版先校验 grant 仍是当前 token、再推进 token，于是同一条命令的重放必然撞 `STALE_FENCING`，`ALREADY_SCHEDULED` 是一条不可达的死码——调用方也就无法区分"我自己的调度已经落地"和"我被别人抢占了"，而这两者的正确后续动作不同（前者安心退出，后者要按 loser 路径停止一切写入）。同时，没有持久化的命令标识，也无法区分**完全相同的重放**与**被篡改的重放**。

因此在 task row 上新增两列并调整顺序：

- `retry_scheduled_by_attempt: BIGINT NULL` —— 哪一次 attempt 调度了当前这次重试；
- `retry_command_digest: TEXT NULL` —— 该命令的一致性 checksum，`content_digest(canonical_json({next_attempt_at, reason, attempt_number}).decode(...))`，与 §2.3 同一写法、同一强度声明。

行锁内的顺序（第 3 步之后的持有者判定走 §4.2.2 的统一判据，不另写一份）：

1. 终态 → `TERMINAL_PROTECTED`（终态保护永远最先，与 `classify_transition` 同源）；
2. **幂等分支，条件是两个等式同时成立**（在 fencing 判断之前）：`retry_scheduled_by_attempt == command.grant.attempt_number` **且** `winner.attempt_number == command.grant.attempt_number`。digest 相同 → `ALREADY_SCHEDULED` + 当前 winner，**不再次增加 `task_failure_count`**、不再推进 token；digest 不同 → `COMMAND_MISMATCH`，不做任何写入。

   **第二个等式不能省。** 只比 `retry_scheduled_by_attempt` 会漏掉一个真实窗口：重试到期后新 Worker 已经 `begin_task_attempt` 成功（`winner.attempt_number` 已经是 N+1、token 已推进），而 `retry_scheduled_by_attempt` 仍停在 N（它只在下一次 `schedule_retry` 时才被覆盖）。此时第 N 次尝试的一条迟到重放会命中第一个等式，拿到 `ALREADY_SCHEDULED`——一个**已经被接管的旧持有者被告知"你的调度已生效"**，而计划声称它应该得到 `STALE_FENCING`。加上第二个等式后，`winner.attempt_number` 已是 N+1，不相等，落回第 3 步按 fencing 拒绝。

   两条反例必须都写：**调度后立即重放**（两个等式都成立 → `ALREADY_SCHEDULED`）与**下一次 attempt 开始之后再重放**（第二个等式不成立 → `STALE_FENCING`）。只写第一条时，漏掉第二个等式的实现照样全绿。
3. 调用 `grant_is_current(winner, command.grant, now=..., allowed_statuses={CREATED, PLANNING, RUNNING})`（§4.2.2），非 `None` 即按那张映射表返回 `STALE_FENCING`。注意这一步会连带要求**租约仍 live**——一个租约已过期的 Worker 不能再为自己安排重试，它已经不是持有者了，该任务的重新调度由存储层的租约过期语义负责；状态集合比步骤路径宽，理由见 §4.2.2 那张表；
4. 写入 `next_attempt_at`、`retry_scheduled_by_attempt`、`retry_command_digest`；把 `lease_expires_at` 精确移动到同一个 `next_attempt_at`；`task_failure_count += 1`；同事务写入 `audit_events`（§5.1）；
5. **从 fencing 序列取一个新 token 写入 task row。**

**为什么必须推进 fencing token：只把租约到期时间前移是不够的。** 若 `next_attempt_at = now + 1s`，那么在这 1 秒内租约仍然 live、owner 仍是旧 Worker、token 仍是旧 token——旧 Worker 依然可以续租、提交步骤、追加审计甚至终态化。而 `schedule_retry` 的语义恰恰是"本次尝试到此为止"。推进 token 让旧 grant **立即**对所有 fenced 写入失效（`_check_fencing` 的规则是"旧持有者的 token < 当前 token"，序列单调保证这一点），三个租约字段仍同时置位、CHECK 约束满足、`fencing_token IS NOT NULL` 这个"曾被租出"的锚点也保住。调度侧仍然同时要求 `next_attempt_at <= now` 与不存在 live lease，因此新 Worker 也不会提前领取。

**这条推进是对既有稳定契约的一次修改，必须同步改文档，不能只改代码。** `persistence/store.py` 的模块 docstring 第 3 条现在写的是"token 在**取得**租约时递增、续租时不变"。M5 之后这句话不再完整——`schedule_retry` 是第二个推进点。三处必须在同一提交更新：该 docstring、`ARCHITECTURE.md` §7.3、ADR-010 的不可逆选择清单。漏改任一处，仓库里就同时存在两句互相矛盾的契约描述，而 `test_doc_fact_binding.py` 钉死的正是这个形态。

幂等分支放在持有者判定**之前**是安全的，因为它的两个等式已经蕴含"发起者正是当初调度这次重试的那一次尝试，且那次尝试仍是任务上最新的一次"——它不给任何更早或更晚的持有者放行，且这条分支**不做任何写入**，即使租约已过期也不产生新的任务事实。

必须覆盖 retry delay 小于、等于和大于原 TTL 三种情况，证明不会被原 60 秒租约意外拖住，也不会提前领取。幂等分支的专项用例：同一条命令连发两次 → 第二次 `ALREADY_SCHEDULED` 且 `task_failure_count` 只 +1、token 只推进一次；改一个字段再发 → `COMMAND_MISMATCH` 且 `version` 不变；**下一次 attempt 已开始后再重放旧命令 → `STALE_FENCING`**（AD10 的核心反例）；租约已过期的持有者调用 → `STALE_FENCING`。

若数据库已不可用，`schedule_retry` 可能无法提交；Worker 不伪造成功，旧 lease 自然到期，数据库恢复后任务重新可领取。

### 4.5 `created_seq`、幂等索引与迁移

**`created_seq`。** 迁移新增数据库 sequence 与 `tasks.created_seq`：创建 sequence → 加 nullable 列 → 对现有行按 `task_id` 稳定排序回填 → 把 sequence 当前值设置为严格大于回填后的最大值 → 设置 default、`NOT NULL` 和唯一索引 → schema metadata 与 Alembic DDL 完全一致。sequence 代表分配顺序，不代表 transaction commit 顺序；调度只承诺稳定且近似公平，不承诺严格 FIFO。

**幂等唯一索引改为定长 digest，不再对变长文本建索引。** 现有约束是 `UNIQUE (tenant_id, environment_id, idempotency_key)`，三列都是无长度上限的 `Text`。PostgreSQL 的 B-tree 索引行有约 2704 字节上限，一个超长键（或超长 environment_id）在契约层完全通过、到 `INSERT` 才炸成驱动错误——按 §4.7 还会被误分类。只给 `idempotency_key` 加 200 字符上限不够：约束是**整个索引元组**的字节数，而 Unicode 字符也不是固定字节数（一个 200 字符的四字节 emoji 键就是 800 字节）。

因此两件事一起做，缺一条都不闭合：

1. **输入卫生**：`RequestEnvelope.idempotency_key` 加 200 字符上限（远宽于任何真实用途），配"恰好上限"与"超一字符"两条边界用例。这挡住的是荒谬输入，不是索引问题。
2. **索引形状**：新增定长列 `idempotency_scope_digest CHAR(64) NOT NULL`，取值为 `content_digest(canonical_json({tenant_id, environment_id, idempotency_key}).decode("utf-8"))`，唯一约束改建在它上面（`uq_tasks_idempotency_scope_digest`）。三列明文**保留**，用于命中后的语义回查：digest 命中时必须再比对三列明文，不等则 fail-closed 报冲突而不是静默返回别人的任务。无密钥摘要的碰撞在这里不是安全边界，回查才是——表述上同样按"一致性 checksum"处理（§2.3）。

迁移里把旧约束替换为新约束，并对历史行回填 digest；`test_schema_matches_migration.py` 逐条比对。

**M5 的正确不变量是"每个可调度/可执行任务都有 submission"，不是"每个 task 都有 submission"。** 后者对保留下来的 M4 终态任务不成立（它们没有子行），把它写进计划会让每一处实现都得为终态任务开兼容分支。表述为：**非终态任务必须有 submission 行**。这条无法用 DDL 表达（PostgreSQL 不支持跨表 CHECK），因此由三层承重：`create_task` 在同一事务写两行；一条行为用例断言不可能构造出"非终态但无 submission"的存储状态；migration 内的验证查询在升级完成后断言该集合为空。

**M4 历史任务没有 submission，这个缺口在 migration 里闭合，不留给运行时。** `rev_0003`（引入 `task_submissions` 的那一个，见 §7 前言的 revision 表）无法还原一个 M4 任务的原始 `text`、`as_of` 或 trace identity——那些事实当时就没有被记录。三条不能做的事先钉死：不伪造历史 submission（编一个 `as_of` 会让恢复出来的计划窗口与用户当初问的完全不同）；不让重复提交去"补写"旧任务（同一 idempotency key 的新请求会命中既存的 M4 任务，允许它写入自己的 submission，用户 A 的旧任务就会带上用户 B 的请求原文）；不对历史行放宽 NOT NULL / 一一关系。

采取的策略是**在 migration 内部按状态确定性处理**：

| M4 任务状态 | 迁移后 |
| --- | --- |
| 五个终态之一 | 原样保留，可查询、可投影；永不再被 dispatch（终态本就被排除） |
| `CREATED` / `PLANNING` / `RUNNING` / `AWAITING_APPROVAL` | 在同一次迁移里写成 `FAILED`，`version + 1`，`terminal_reason` = 闭集码 `legacy_task_without_submission` |

放在 migration 而不是"首次 claim 时终态化"，是因为后者要求 `begin_task_attempt` 长出一条只在升级后短暂存在的分支，而这条分支永远无法被日常用例覆盖——它会以"看起来没用的兼容代码"的形态长期留在热路径上。

**downgrade 必须区分 schema 可逆与数据语义可逆——这是两件事。** downgrade 会删掉 `task_submissions` 与 step journal 表。此时若库里有**真实的 M5 任务**，再次 upgrade 时它们就变成了"非终态且无 submission"，正好落进上面那张表的第二行，被 `legacy_task_without_submission` 判死。也就是说：一次"可逆"的 schema 回滚，会静默杀掉所有在途的 M5 任务。上一版只用 M4 seed 数据验证 down/up，因此永远看不到这条路径。

修法：

- **守卫范围是"任何 M5 数据"，不只是"活跃任务"。** 上一版只查非终态任务，于是一个库里全是**已终态**的 M5 任务时，无 flag 的 downgrade 会顺利通过，然后静默删光它们的 `task_submissions` 与 step journal——那些是用户提交的原文与执行历史，比一个在途任务更不该被无声丢弃。守卫改为：`task_submissions` 或 step journal **存在任意一行**即拒绝。错误消息只含计数与类别，不含 task_id 或任何请求文本。
- **要强行降级必须显式声明破坏性，而这个开关必须真的读得到。** 上一版写的是 `alembic downgrade -x allow_destructive_downgrade=1`，这条命令在本仓库**不成立**，两个原因叠加：`-x` 是 Alembic 的全局参数（位置在子命令之前），更关键的是共享的 `_run_downgrade()` **自己构造 `Config`**（`Config(str(_ROOT / "alembic.ini"))`），`cmd_opts` 为空，因此迁移脚本里的 `context.get_x_argument()` 读到的永远是空——开关写了也不生效，降级会一路执行到删表。这与 §5.3 的迁移入口是同一个陷阱（程序化入口不走命令行，就不能用命令行的机制传参）。

  正确形状是走已经在用的那条通道 `config.attributes`（`connection` 就是这么注入的）：

  ```python
  def _run_downgrade(
      connection: Any, *, allow_destructive: bool = False, revision: str = "base"
  ) -> None:
      config = _alembic_config()
      config.attributes["connection"] = connection
      config.attributes["allow_destructive"] = allow_destructive
      command.downgrade(config, revision)
  ```

  迁移脚本用 `context.config.attributes.get("allow_destructive", False)` 读取，**默认 `False`**（读不到即拒绝，fail-closed）。`interfaces/migrate.py` 暴露一个显式的 `--allow-destructive` 命令行标志并把它传进来，而不是让调用方去猜 `-x` 语法。一条用例直接断言：不传该参数时守卫触发；传 `True` 时通过；以及**把 `get()` 的默认值改成 `True` 后守卫用例必须转红**（证明 fail-closed 的方向是被测过的，而不是碰巧）。
- **回到 `0001_initial` 前先证明旧 M4 唯一约束能够重建，且这一步早于任何 downgrade DDL/数据改动。** digest 索引允许 M5 接受一个明文三元组很长、但摘要固定 64 字节的合法任务；旧 M4 的 `UNIQUE (tenant_id, environment_id, idempotency_key)` 可能因 B-tree index row 上限无法重建。`--allow-destructive` 只授权丢弃 M5 子表，不授权执行一个注定中途失败的 schema 回滚。

  `interfaces/migrate.py` 在调用 `command.downgrade` 之前，使用**同一连接、同一外层事务**执行只读守卫与一次可回滚 DDL probe：创建 SAVEPOINT；用内部生成的 UUID hex 组成安全临时索引名；尝试 `CREATE UNIQUE INDEX <probe> ON tasks (tenant_id, environment_id, idempotency_key)`；无论成功或失败都先 `ROLLBACK TO SAVEPOINT` 再 `RELEASE SAVEPOINT`。创建成功表示当前行集兼容旧 M4 约束；创建失败则在回滚 savepoint 后抛只含常量码 `M4_DOWNGRADE_INCOMPATIBLE` 的迁移错误，**不调用 Alembic**。不打印索引值、SQL 参数、DSN 或驱动原文。调用前要求 API/Worker 已停止，因此 probe 与正式 downgrade 之间没有并发写窗口；若该前置不成立，不宣称消除竞态。

  两条 PostgreSQL 反例：兼容的真实 M5 数据在显式破坏性授权下可完成 downgrade；含超大但 M5 合法 scope 的数据在默认与 `--allow-destructive` 两种调用下都以 `M4_DOWNGRADE_INCOMPATIBLE` 失败，并逐项断言 schema、`alembic_version`、tasks、submission、journal 全部不变。只断言“抛异常”不够，因为一个先降到一半再失败的实现同样会抛。
- 带 flag 的 downgrade 先把全部非终态任务写成 `FAILED`、reason `m5_downgrade_discarded`，再删表——**在降级时就把代价兑现**，而不是留给下一次 upgrade 用一个语义完全不同的 reason 去杀它们。
- **测试必须用真实 M5 数据**：seed 一个带 submission、带 in-flight step 行的非终态任务 → 无 flag 的 downgrade 必须失败 → 带 flag 的 downgrade 成功且任务已 `FAILED` + `m5_downgrade_discarded` → 再 upgrade，断言**没有任何任务被写成 `legacy_task_without_submission`**（那个码只属于 M4 遗留行）。仅用 M4 seed 的 down/up 用例保留，但不再单独视为回滚证据。

**迁移期的终态化是"状态改动与审计同事务"（§5.1）的一个显式例外，必须在 ADR 里写明，不能留下两套口径。** 迁移在 Alembic 的连接里运行，没有 Runtime、没有 `TraceSink`、没有 attempt，也没有任何 trace_id 可归属；硬造一个只会得到一条谎称属于某次尝试的审计行。因此这两处（升级的 `legacy_task_without_submission`、降级的 `m5_downgrade_discarded`）**写 `terminal_reason` 但不写 audit 事件**，审计职责由迁移本身承担——`alembic_version` 记录了哪次迁移跑过，闭集 reason 码记录了每一行为什么被终态化，两者合起来可完整回答"这些任务是被哪次迁移、以什么理由终态化的"。

这条例外必须被**固定成已知行为**而不是留成缺口：一条用例断言迁移终态化的任务 `terminal_reason` 属于那两个闭集码之一、且该任务的 audit 行数**在迁移前后不变**。没有这条断言，将来有人"补上"迁移期审计时会与 ADR 冲突而无人察觉。

对价写明：升级会把当时仍非终态的 M4 任务判死；降级会把当时仍非终态的 M5 任务判死。M4 的所有任务都产生于测试环境、handoff 里没有需要保留的在途生产任务，因此这个代价在 M5 可接受；面对真实在途任务时正确做法是先排空再升降级，而不是让代码去猜历史。

### 4.6 行映射与内存共享状态

新增 TaskRecord 字段与新增列必须同步进入：`persistence/schema.py`、Alembic migration、`persistence/postgres.py` 的 `_TASK_COLUMNS`、`persistence/rows.py` 的 `record_to_row` / `row_to_record`、row-mapping / 深不可变 / 序列化卫生测试。

内存 TaskStore 与 EvidenceLedger 必须共享一个内部 `InMemoryPersistenceState`（step journal 与 stored plan 的同事务读取也依赖它）。`tests/conftest.py` 与 `tests/integration/conftest.py` 通过 typed bundle/factory 同时构造，并明确断言共享状态。不得把内部 state 提升为新的公共 Protocol。

内存/PostgreSQL 共跑同一个 TaskStore 行为 suite，包括原子 task+submission、dispatch、attempt、retry、step commit、Evidence 可见性、并发 winner 与终态保护。

### 4.7 持久化故障边界与有界超时

`PersistenceUnavailableError` 在此前的版本里只被异常表引用，`src/` 与 `tests/` 中**并不存在**。没有它，真实的 asyncpg / SQLAlchemy 异常会落进"未分类 `Exception` → `TASK_FAILURE` 重试"那一桶，于是**一次数据库重启会消耗每个在途任务的失败预算，三次就把健康任务写成 `FAILED`**——正好抵消掉三套计数分离的全部价值。

**但反过来，把所有驱动异常都当成 transient 同样是缺陷。** `IntegrityError`（唯一约束冲突、CHECK 违反）、`ProgrammingError`（列不存在、SQL 语法）、`DataError`（类型/长度越界）、以及行反序列化失败（`row_to_record` 的 `ValidationError`）全部是**schema 或代码缺陷**：它们每次重试都会以同样的方式失败，落进基础设施退避意味着一个真实 bug 变成一次 15 分钟的静默无限重试，最后以"Worker 因连续基础设施故障退出"的形态呈现，把根因彻底掩盖。

**但"不是 transient 就写任务 FAILED"同样是错的，这是上一版留下的第三个洞。** 把 `ProgrammingError`（列不存在、SQL 语法错）、`DataError`、行反序列化失败统一归成"任务不变量破坏 → 写 `FAILED`"，会同时踩两条：

1. **它们不是 task-local 的。** 一个缺列的 schema 对**每一个**任务都失败。按上一版的规则，Worker 会把它领到的每个任务逐一写成 `FAILED`——一次部署/迁移不匹配变成一场静默的数据损毁，而根因（schema 不对）一条都看不出来。
2. **它可能发生在纯读路径上。** §3.1 要求 `GET` 绝对纯读、不改 `version`、不写 audit。若 `query_task` 读到一条坏行就去终态化那个任务，`GET` 就有了副作用——而且是把用户查询的对象改坏。

因此分**三类**，判据是"故障是否属于这一个任务"和"数据库是否还能写"：

| 类别 | 驱动/内部异常 | 处置 | 是否改任务 |
| --- | --- | --- | --- |
| **不可用** | `OperationalError`、`InterfaceError`、`DisconnectionError`、`DBAPIError(connection_invalidated=True)`、`sqlalchemy.exc.TimeoutError`（连接池耗尽）、`asyncio.TimeoutError` | `PersistenceUnavailableError(category=CONNECT/TIMEOUT/POOL/TRANSIENT, write_outcome=...)` → 进程级退避 | **否** |
| **系统性故障** | `IntegrityError`、`ProgrammingError`、`DataError`、其余 `StatementError`、**兜底的 `SQLAlchemyError`**、`row_to_record` 的 `ValidationError` | `PersistenceIntegrityError(category=CONSTRAINT/SCHEMA/DATA/DECODE)` → Worker 侧**进程立即非零退出**（不退避、不重试：schema 不会因为等待而变对）；API 侧 500（§3.1），**任务一个字不改** | **否** |
| **取消** | `CancelledError` | 不吞（`BaseException`，`except Exception` 天然放行） | 否 |

**删除上一版的第三类 `PersistenceConstraintError`。** 它的判据写的是"`IntegrityError` 且此刻数据库仍可写 ⇒ 任务级不变量破坏，在 grant 下终态化"。这个判据**证不出它想证的东西**：一个系统性的 NOT NULL / CHECK / schema 缺陷同样会抛 `IntegrityError`，同样满足"数据库仍可写"。于是一次部署错误会被逐个任务写成 `FAILED`——正是这一节要防的批量误杀。要真正区分，得有一张 constraint name 白名单（"只有 `uq_tasks_idempotency_scope_digest` 冲突算任务局部"），而 M5 里那条约束的冲突根本走不到异常路径：`create_task` 用 `ON CONFLICT DO NOTHING` + digest 比对处理幂等。**没有真实调用链的分类不保留**——与删掉 `RetryClass` 是同一条判据。`IntegrityError` 因此并入系统性故障。

于是**只有 unavailable 与 systemic 两类**，两类都不改任务。`query_task` 的读路径遇到任何持久化异常都只能向上抛（映射为 503 或 500），**绝不终态化**。一条用例专门证明这件事：注入一个读时故障，断言 `version`、audit 行数、evidence 行数三样都不变。

写路径还必须区分“已确认未应用/已回滚”与“提交结果未知”；异常类型本身回答不了这个问题。增加最小闭集 `PersistenceWriteOutcome = ROLLED_BACK | NOT_CONFIRMED`，并让两个持久化闭集错误都携带可选 `write_outcome`：写方法中必填，读方法中为 `None`。连接/事务建立前失败、没有任何变更语句发出，或事务明确回滚成功时标 `ROLLED_BACK`；变更语句发出后在 flush/commit 确认前后断开、驱动报告 invalidated、或无法确认服务器是否提交时一律标 `NOT_CONFIRMED`。调用方不得从异常发生位置、异常消息或本地事务对象状态自行猜测。这个字段只描述**本次写是否有正面的“未应用”证明**，不宣称 `NOT_CONFIRMED` 一定写入或一定未写入。

写方法只有在 rollback 已确认时才允许抛 `PersistenceIntegrityError`；处理原始完整性/schema 异常期间若 rollback 本身失联，必须提升为 `PersistenceUnavailableError(write_outcome=NOT_CONFIRMED)`。因此调用方看到 systemic 写错误可以安全标 `COMMAND_ROLLED_BACK`，而不会把“先发生 SQL 错误、后丢失回滚确认”的组合误报为 failed。

**`except` 顺序必须写死，因为 SQLAlchemy 的层级会让粗心的顺序吞掉一切。** 实测继承链：`OperationalError` / `ProgrammingError` / `IntegrityError` / `DataError` 全部是 `DatabaseError` → `DBAPIError` → `StatementError` → `SQLAlchemyError`；而 `sqlalchemy.exc.TimeoutError`（连接池）**不是** `StatementError`，它直接挂在 `SQLAlchemyError` 下。因此：

1. 先捕连接失效：`DBAPIError(connection_invalidated=True)` 与 `DisconnectionError` → 不可用；这个判据必须在一般 `StatementError` / `SQLAlchemyError` 之前；
2. 再捕其它明确不可用类：`OperationalError`、`InterfaceError`、`sqlalchemy.exc.TimeoutError` 与 `asyncio.TimeoutError`；
3. 再捕系统性的具体子类：`IntegrityError` → `DataError` → `ProgrammingError`；
4. 再 `StatementError` 兜底进系统性故障；
5. **最后必须有一道 `SQLAlchemyError` 兜底，同样归系统性故障。** 它收拢 `InvalidRequestError`、`ResourceClosedError`、`PendingRollbackError` 等非连接失效的尾部；`DisconnectionError` 已在第 1 层被明确截走，不能再列作系统性例子。

一条用例逐个异常类构造并断言映射结果，并**断言顺序本身**：`DisconnectionError` 与 `DBAPIError(connection_invalidated=True)` 必须映射为不可用；普通 `DBAPIError(connection_invalidated=False)` 仍按具体子类/`StatementError` 规则分类；把 `StatementError` 移到最前、删掉连接失效分支或删掉第 5 道兜底时各自的承重用例必须转红。

**包装必须真正切断异常链，而 `from None` 做不到这件事。** `raise ... from None` 只设置 `__suppress_context__`，`__context__` **仍然持有**原始异常——这条 Python 事实仓库里已经踩过并写在 `config.py` 的模块 docstring 里（它正是为此把 `ConfigError` 挪到 `except` 块之外抛出的）。上一版写"一律 `raise ... from None`"是重复了那个已经被修过的错误：`StatementError.__str__` 会把 SQL 语句与参数拼进消息（`[SQL: ...] [parameters: ...]`），参数里就有 `text`、`idempotency_key` 这些用户内容，一份仍挂在 `__context__` 上的原始异常会随任何 traceback 打印出来。

因此照抄 `config.py` 的既有形状：在 `except` 块里只提取闭集 `category`，把 `PersistenceXxxError` 的抛出放在 `except` 块**之外**。同样的写法适用于 §3.4 的 `RetryableTaskError`。并且**任何一处都不得把原始异常对象交给 logging**（`logger.exception` / `%s` 传 exc 都不行）。共同的写法约束：消息恒为常量，结构化属性只带闭集 `category`，绝不携带 DSN、host、密码、SQL 文本或驱动原文——与 `TaskIdCarryingError` 同一写法。

用例：构造一个参数里含 secret 形状字面量的语句故障，断言异常的 `str()`、`repr()`、`__cause__` **与 `__context__`** 都不含该字面量；**反证**：改回在 `except` 块内 `raise ... from None`，`__context__` 断言必须转红。

映射发生在 adapter 内部（`persistence/postgres.py` 的每个公共方法），不在 Worker 里靠异常类型猜；内存实现不产生前两类（它没有连接、没有 schema），因此两实现的共享套件对这两条只在 PostgreSQL 侧绑定。

反例逐条：注入 `ProgrammingError` / `IntegrityError` / `InvalidRequestError` 且 rollback 成功 → **任务一个字不改**、Worker 进程非零退出、退避 sleep 0 次；先触发完整性错误再让 rollback 连接丢失 → 必须提升为 `PersistenceUnavailableError(NOT_CONFIRMED)`，不得仍报 systemic/rolled-back；注入 `OperationalError` → 任务保持非终态、失败预算不变、退避被调用；注入坏行 → `PersistenceIntegrityError` 且异常文本不含列值；读路径注入任一类 → version/audit/evidence 三样不变。

同时补齐有界超时——没有超时，一次挂死的调用会让 15 分钟故障窗口永远不触发，Worker 静默僵住：

| 配置 | 默认 | 约束 |
| --- | --- | --- |
| DB connect timeout | 5 秒 | > 0，< command timeout |
| DB command timeout | 15 秒 | > 0，< lease TTL |
| DB pool size | 5 | > 0 |
| DB pool max overflow | 0 | **>= 0**（0 是合法且是默认值：不允许溢出连接） |
| CLI HTTP timeout | 10 秒 | > 0 |
| CLI 响应体上限 | 1 MiB | > 0 |

（overflow 的约束写成 `> 0` 而默认值给 0，是上一版表格内部的直接矛盾：按那条 validator，进程用默认配置根本起不来。0 表示"连接池不允许溢出"，是这里想要的语义，因此改约束而不是改默认值。）

`migrate`、`/readyz`、API、Worker 共用**同一个 Engine factory**（同一处读 Settings、同一处读 password file、同一处设超时），不在四个地方各拼一份连接参数。

## 5. Trace、审计、配置与安全

### 5.1 事件投递与 durable audit

基础改动：

- `TraceSink.emit` 改为 async；所有调用点必须 `await`，禁止 `create_task` / fire-and-forget。
- Runtime/Runner 的 event id 使用 UUID；现有逻辑没有依赖事件 ID 的顺序性。
- `TraceEvent` 增加可选 `attempt_number`；M5 task execution events 必须带值，提交前/无任务事件可以为空。
- `detail` 继续保持空或结构化白名单，不放异常原文、外部内容或 secret-shaped 字符串。
- `observability` 定义最小结构 `AuditEventWriter` Protocol，durable sink 依赖该端口，不直接 import `persistence`；装配层注入 TaskStore writer。

#### 投递形状必须是一个显式参数，不能从 `TraceEvent` 的字段猜

上一版留下了一个自相矛盾：路由表说"`task_id` 非空 → sink 写 durable"，紧接着的散文又说"交给 step commit 的事件不得经过 sink"。两条规则针对同一条事件，键在不同东西上，而第二条的判据（"这条事件会不会被交给 commit"）**不是 `TraceEvent` 上的字段**。照第一条实现会在原子提交之前先 durable 写一次，破坏 §2.7 的三写原子性；照第二条实现，step 事件就完全没有结构化日志——恰好在失败路径上丢掉唯一的可读诊断。

修法是把投递意图变成一个**闭集参数**，而不是让实现去推断：

```python
class Delivery(StrEnum):
    LOG_ONLY = "log_only"                        # 事件不落库（无 task_id），只写日志
    LOG_AND_DURABLE = "log_and_durable"          # sink 自己调 record_audit_event，再写日志
    COMMAND_COMMITTED = "command_committed"      # 已随某个命令的 audit_events 落库，sink 只补日志
    COMMAND_ROLLED_BACK = "command_rolled_back"  # 该命令的事务失败，事件未落库，sink 只补日志
    COMMAND_NOT_CONFIRMED = "command_not_confirmed"  # 无法确认事务是否提交，sink 只补日志

async def emit(self, event: TraceEvent, *, delivery: Delivery) -> None: ...
```

它是 `emit` 的参数而不是 `TraceEvent` 的字段：投递路径是一次**传输决定**，不是任务事实，把它写进持久化契约会让同一条审计行在不同投递方式下长得不一样。

**后三个成员是命令结局的完整接口，不是新业务功能。** 只区分提交与回滚会漏掉分布式写入最关键的一格：数据库可能已经提交，而连接在确认返回前中断。把这种情况记成 `COMMAND_ROLLED_BACK` 会留下假话。§4.7 的 `PersistenceWriteOutcome` 是唯一判据：明确回滚才用 `COMMAND_ROLLED_BACK`；没有正面提交结果、也没有回滚确认时用 `COMMAND_NOT_CONFIRMED`。调用方禁止按 catch 位置猜测。

选的是另一条：**取消两阶段承诺，每条事件只写一条日志，且写在结局已知之后。** 于是"这条日志说的事到底成没成"由 `delivery` 这一个参数完全决定，不需要第二次调用：

| 产生点 | `delivery` | durable 落库方式 | 日志里的 `delivery_state` |
| --- | --- | --- | --- |
| `task_id is None`（任务创建之前的 INTENT / RESOLVER / PLANNER 拒绝） | `LOG_ONLY` | 不落库 | `not_applicable` |
| 其余带 `task_id` 且不属于任何命令缓冲的事件 | `LOG_AND_DURABLE` | sink 调 `record_audit_event` | `committed` / `failed` / `not_confirmed`，由持久化结果与 `PersistenceWriteOutcome` 决定 |
| 被缓冲进某个命令的 `audit_events`，且该命令的事务**已提交** | `COMMAND_COMMITTED` | 已随命令落库 | `committed` |
| 被缓冲进某个命令的 `audit_events`，且该命令已**明确确认未提交/已回滚** | `COMMAND_ROLLED_BACK` | 未落库 | `failed` |
| 被缓冲进某个命令的 `audit_events`，但提交结果无法确认 | `COMMAND_NOT_CONFIRMED` | 可能已落库，也可能未落库；只靠幂等重放/恢复收敛 | `not_confirmed` |

五行互斥且穷尽；`delivery_state` 是**派生值**，不是第六个参数——同一个事实没有两处输入。

**代价说准：进程被 SIGKILL 时，一条已缓冲但尚未走到 emit 的事件不会留下任何日志。** 两阶段日志本来是为这个场景准备的。但缓冲事件的持久诊断另有其人：§2.7 的 in-flight journal 行就是崩溃点的证据，它在 `begin_step_attempt` 时已经落库，不依赖日志。而两阶段的代价是确定的——一条 `attempted` 日志在事务随后回滚时**就是一条假话**，读日志的人会看到"任务已终态"而库里根本没变。用"崩溃时少一条日志"换"日志里没有假话"，方向是对的。这条写进残余风险。

**调用点的规则因此只有一句：命令的 `audit_events` 在数据库返回成功、确认回滚或报告未知结局之后才 emit。** 成功路径 `COMMAND_COMMITTED`；只有 `PersistenceWriteOutcome.ROLLED_BACK` 走 `COMMAND_ROLLED_BACK`；`NOT_CONFIRMED` 或写路径拿不到正面回滚证明时一律走 `COMMAND_NOT_CONFIRMED`。用例除“audit insert 明确回滚 → 全为 `failed`、无 `committed`、task 状态/version 不变”外，还要故障注入“数据库实际完成写入后连接断开”→ 唯一日志为 `not_confirmed`、绝不出现 `failed`；相同命令重放/恢复后只保留一份 Evidence 与应有的 audit 行并收敛到同一 winner。

`LOG_AND_DURABLE` 遇 `NOT_CONFIRMED` 时**不自动重放同一个 event_id**：M4 的 append-only 契约明确允许同 task 重复 event_id，盲重放无法去重。sink 写一条 `not_confirmed` 日志并把不可用结果交给调用方；若任务之后重新尝试，会生成新的事件/attempt 归属。命令缓冲路径则可以重放原命令，因为 transition CAS、retry digest、step commit digest 与 create-task idempotency 各自提供收敛判据。两类不能混成一个“统一重试 audit”的 helper。

**这条约束是 sink 入口的运行期 guard，不是"契约层不可构造"。** 上一版写的是一条 validator，但 `event` 与 `delivery` 是 `emit()` 的**两个独立参数**，没有任何一个 Pydantic 对象同时持有它们——validator 无处可挂。为它专门造一个 `EmitRequest` DTO 只是为了让一句话成立而增加一层，不符合最小实现。因此写成 sink 入口的一行断言：

```python
if delivery is not Delivery.LOG_ONLY and event.task_id is None:
    raise ValueError("task-scoped delivery requires a task-scoped event")   # 消息恒为常量
```

判据写成"不是 `LOG_ONLY`"而不是逐个列举另外四个成员：这样 `Delivery` 将来加成员时，新成员**默认落在受约束的一侧**，而不是默认绕过守卫。（同一条理由让 `test_dependency_baseline.py` 用集合相等而不是禁用清单。）四条受约束的成员各一条用例直接调用 `emit` 并断言抛出。

它是 `TraceSink` 实现的公共前置条件（两个实现共用同一个 helper，不各写一份），由一条用例直接调用 `emit` 并断言抛出——断言的是**运行期行为**，不是"构造不出来"。`TaskStore.record_audit_event` 对无归属事件抛 `UnscopedAuditEventError` 的既有守卫**一个字不改**：它仍然是写入端的最后一道拒绝，与 sink 的 guard 是两层独立表达。改的只是 sink 的分派——无归属事件的去处本来就是 `TraceSink`，`store.py` 的 docstring 早已写明"这类事件不是丢弃就行，只是不归 TaskStore 管"。

**正常返回且已交给调用方的每条事件都经过 sink 拿到结构化日志**，差别只在 durable 那条腿。`begin_task_attempt` 内部事件若已提交并正常返回，就通过 `committed_audit_events` 进入这条路径；若连接在结果返回前丢失，调用方只有 `NOT_CONFIRMED` 而不知道存储层最终选了哪种 rejection，禁止伪造一条具体终态事件，此时可能只有 durable 行而没有对应日志（列入残余风险）。这解决了正常路径“step/内部终态事件没有日志”的问题，也不需要在 Runner 里为日志再开一条旁路。

被策略拒绝的步骤（`PolicyDeniedError` / `SqlGuardError` / `BindingError`，此时按 §2.7 尚未 `begin_step_attempt`，因此没有 commit）其事件带 `task_id` 且不属于任何缓冲，走表中第二行由 sink 持久化——ARCHITECTURE 要求"一次被策略拒绝的调用不产生证据但必须留下审计"，这条路径因此仍然成立。

#### 状态改动与它的审计必须同事务

上一版的异常表承诺"durable audit 失败时不写终态"，但 Runtime 现在的时序（以及计划里的时序）是先 `transition` 再 emit 生命周期事件。两者不可能同时成立。实际影响是一个不可恢复的状态：终态 CAS 成功、随后 audit 写入失败 → 任务已受终态保护，既无法重新调度，也**永久缺少终态审计行**。`schedule_retry` 有同型窗口（重试已调度、"为什么重试"的记录丢失）。

修法不需要第 15 个方法，但**需要改公开签名**：`transition` 从散开的关键字参数改为 `transition(*, command: TransitionCommand)`，否则调用方没有任何途径把审计事件传进去（完整理由与三条形状说明见 §4.1.1(b)）。

**但"哪些状态改动必须事务审计"必须自己是一张闭表，否则这条承诺兑现不了。** 上一版把它写成"生命周期事件随**对应命令**的 `audit_events` 提交"，而 `TaskAttemptCommand` 里根本没有 `audit_events` 字段——成功领取同样改了 `lease_owner` / `lease_expires_at` / `fencing_token` 与 `attempt_number`，按那句话它应该带审计，可它没处带。两条出路：给 `TaskAttemptCommand` 加一个 `audit_events`，或者把承诺收窄到它真正需要覆盖的范围。选后者，判据是**这次改动丢了审计还能不能补救**：

| 写入 | 事务审计 | 事件从哪来 | 判据 |
| --- | --- | --- | --- |
| `transition`（状态迁移，含终态） | **必须** | 调用方经 `TransitionCommand.audit_events` 传入 | 终态受终态保护，任务再也不会被重新调度，缺的审计行**永远补不回来** |
| `schedule_retry` | **必须** | 调用方经 `RetryCommand.audit_events` 传入 | 重试标记一旦落库，"为什么重试"就没有第二个来源 |
| `commit_step_result` | **必须** | 调用方经 `StepCommitCommand.audit_events` 传入 | 步骤终局同理，且它与 Evidence 必须三写原子（§2.7） |
| `begin_task_attempt` 的**终态写入**（第 6 步 `RETRY_EXHAUSTED`、第 7 步 `SUBMISSION_INVARIANT_VIOLATION`） | **必须** | **存储层自己构造并经 `TaskAttemptResult.committed_audit_events` 原样返回**，归属来自 `TaskAttemptCommand.trace_id` | 此时调用方还没有 grant、也没有 `attempt_number`，不能从外部传事件；返回已提交事件后 Worker 才能以 `COMMAND_COMMITTED` 补日志且不重复 durable 写 |
| `begin_task_attempt` 的**成功领取** | **不需要** | 领到 grant 之后由 Worker 按第二行 `LOG_AND_DURABLE` 补一条带 `attempt_number` 的事件 | 租约有 TTL：审计写失败时租约会自然到期、任务被重新领取，**这次改动可自愈**。核对过 `postgres.py::acquire_lease`：它只改三个租约字段，**不动 `version`、不动 `status`**，因此也不存在"版本推进了却没有审计"的窗口 |

这张表本身由一条用例固定：`TaskAttemptCommand` 的字段集合**恰好**是列出的那几项（不含 `audit_events`、不含 `now`），加字段必须先改这张表。

- `TransitionCommand` 增加 `audit_events: tuple[TraceEvent, ...] = ()`（默认空，M4 既有存储层用例语义不变，只改调用形状）；端口层强制不了"必须非空"，强制落在 §4.1.1(c) 的一条 AST 用例与一条行为用例上，且这一点写进残余风险；
- `RetryCommand.audit_events` 与 `StepCommitCommand.audit_events` 都用 `Field(min_length=1)` **在契约上钉死非空**，而不是写一句"必须非空"再给一个 `= ()` 的默认值——上一版正是那样，散文与模型直接矛盾，照模型实现就是可以省略。它们是 M5 独有方法，没有历史调用方，因此可以直接钉死；`begin_task_attempt` 第 6 / 7 步的终态事件同理。两处的 `trace_id` 与 `attempt_number` 来源见 §4.1.1。
- **三个 DTO 的非空要求不对称，这是刻意的**：`TransitionCommand.audit_events` 保留 `= ()`（M4 的存储层用例合法地不带审计，见 §4.1.1(c)），另外两个必填非空。asymmetry 本身由一条用例固定，避免将来有人"统一"成其中任何一边。`TaskAttemptCommand` 不在这三个里——按上表它压根没有这个字段。

每条事件的 `task_id` 必须等于本次操作的 task；违反即拒绝命令。审计插入失败 → 整个事务回滚 → 状态没有改变 → 调用方看到的是一次未完成的操作，可以安全重试。**故障注入用例**：让 `task_audit_events` 的 insert 抛错，断言 `tasks` 行的 `status` 与 `version` 都没有变化（而不是只断言"抛了异常"）。

#### seq 分配必须复用同一把 advisory lock

两张 append-only 表用 `MAX(seq) + 1` 分配序号，这是读-改-写；`postgres.py::_serialise_on_task` 用 `pg_advisory_xact_lock(hashtext(task_id))` 把它们串行化。新的 step commit 与状态事务里的 audit 写入是 `task_audit_events` 的**第二、第三个写入来源**，若它们不取同一把锁，就会与公开的 `record_audit_event` 并发算出同一个 seq，其中一个撞唯一约束——而按 §4.7 那是 `IntegrityError`，会被分类成不变量破坏并把任务写死。因此：**所有写 `task_audit_events` / `task_approvals` 的路径，一律先 `_serialise_on_task`**，没有例外。用例：两个来源并发写同一 task，断言 seq 连续无缺口、无重复、无异常。

#### 去重靠调用形状，不靠数据库约束

**不引入 `UNIQUE (task_id, event_id)`。** `tests/suites/task_store.py::test_audit_seq_is_scoped_per_task` 对同一个 task 用 `make_event` 的**默认 `event_id="e1"` 写了两次**并断言 seq 递增到 2——这是 M4 append-only 语义的正例（序号由存储层分配，不由调用方的 event_id 派生）。加约束就是改 M4 契约，还要处理历史重复数据；更糟的是它把一次**路由 bug** 变成一次**数据库错误**，按 §4.7 会被分类成不变量破坏而不是让对应用例转红。

规则回到调用形状，一句话可执行：**被缓冲进某个命令的 `audit_events`，以及 `TaskAttemptResult.committed_audit_events` 返回的事件，不再以 `LOG_AND_DURABLE` 交给 sink。** 非重复由测试证明，而且是可判定的——`event_id` 已是 UUID，来源的集合可以直接比：一次任务跑完后，该任务在 `task_audit_events` 里的 `event_id` **多重集**，恰好等于「sink 实际 durable 写入的事件」⊎「调用方随命令提交的 `audit_events`」⊎「`begin_task_attempt` 内部提交并随结果返回的事件」，三方两两交集为空，且多重集内没有重复元素。用 spy sink + spy store 记录三侧实际调用——这比 AST 约定强（它检查的是落库结果），也不需要动 M4 的表。

#### 其余固化项

- **写入顺序：结局已知之后写一条日志，每条事件恰好一条。** 直觉上"先日志、后 durable"更安全（durable 失败时仍留下诊断），但它留下一个更坏的东西：一条在事务随后回滚的生命周期事件，日志里已经是一条看起来完成了的记录，事后读日志的人会看到"任务已终态"而库里根本没变。日志里的假话比日志的缺失更难排查。因此固化为：**先拿到结局，再写唯一那条日志**，`delivery_state` 取上面路由表里那一行的值。

  **`delivery_state` 不能写进 `outcome`。** `TraceEvent.outcome` 的类型是既有的 `StageOutcome`（`OK` / `REJECTED` / `FAILED` / `SKIPPED`，`contracts/enums.py`），它回答的是"**这个阶段**的判定结果"——一次被策略拒绝的 ADMISSION 是 `REJECTED`，一次成功的 GATEWAY 是 `OK`。把投递状态塞进同一个键，等于用"这条记录落到哪儿了"覆盖掉"这个阶段判定成什么"，两个正交的事实挤进一格，`test_trace_stages.py` 承重的阶段语义直接失真。因此它是一个**只存在于结构化日志记录里的**键，`TraceEvent` 一个字段都不加——投递去向是传输事实，不是任务事实，持久化它等于制造一个要维护的第二真源。

  取值恰好四个：`committed` / `failed` / `not_confirmed` / `not_applicable`（`LOG_ONLY` 那一行，事件本来就不落库，用 `failed` 会把"不需要落库"读成"落库失败"）。

  用例按四态逐条：`LOG_AND_DURABLE` 且 `record_audit_event` 明确回滚 → 该事件唯一日志是 `failed`；命令事务回滚 → 该批事件全为 `failed` 且**没有任何** `committed`；提交确认丢失 → `not_confirmed` 且绝不谎报 `failed`；`task_id is None` 的拒绝事件 → `not_applicable`；**反证**：把 `delivery_state` 的取值写进 `outcome`，`test_trace_stages.py` 的阶段断言必须转红。

  `StructuredLogTraceSink` 在契约上**不得抛异常**（它只依赖标准库 logging 与已脱敏的 `detail`），并由一条用例断言：注入一个会抛的 handler 时，失败不能被当成审计失败而改变任务归因。
- **`ToolResult.error` 必须落进对应 `TraceEvent.error`。** `EvidenceEnvelope` 没有 status/error 字段，`ToolResult` 又不持久化，因此如果 GATEWAY 事件继续硬编码 `error=None`，进程退出后就只剩 journal 里的 `FAILED` / `TIMEOUT` 两个字，具体的 `AgentError` 类别与 cause 摘要**永久丢失**。`TraceEvent.error` 字段本来就存在，M5 只需在 Runner 的 GATEWAY 事件里把它填上（`detail` 仍恒为空）。
- **malformed adapter 改用专用异常。** Runner 现在 `except TypeError:` 兜住"adapter 返回了非 `AdapterResponse`"，但 `TypeError` 是 Python 里最常见的内部编程错误之一——网关内部任何一处签名写错都会被这条 except 吸收并**伪装成一次可降级的 malformed 步骤**，于是一个真正的 bug 被写成 `FAILED` step 并消耗预算，从数据上看不出区别。`tools/gateway.py` 改抛 `MalformedAdapterResponseError`（消息恒为常量，不回显类型名——返回值来自 adapter，其类名是外部数据），Runner 只捕获它；其余 `TypeError` 按 §3.5 落"未分类 Exception"桶。gateway 的 docstring `:raises TypeError:` 同提交改掉。
- **audit 不做 fencing，但必须强制归属——这是显式决定，不是遗漏。** `record_audit_event` 只看 `task_id`，不收 grant，因此一个已经丢掉租约的 Worker 仍可以追加事件。这被**接受**：fencing 保护的是**任务事实**（状态迁移、步骤终局、证据、终态），而审计记录的是**观察**——一个 loser 在被抢占前后确实观察到了那些阶段，丢掉它们反而让"这次尝试发生过什么"不可追溯。代价用两条约束控住：(1) grant 存在之后发出的每一条事件，`attempt_number` **必填**，任何一条审计行都能明确归属到哪一次尝试；(2) loser 按 §3.5 在续租失败的那一刻停止执行分支（heartbeat 取消整棵结构化并发树），能追加事件的窗口有界。写进 ADR-010。替代方案（给 audit 加 fenced 变体）被否决：它要么把 TaskStore 撑到 15 个方法，要么改 `record_audit_event` 的既有签名而打红 M4 的共享套件。
- **结构化日志的键集必须精确列出并用集合相等钉死，不能写成一句"只允许固定键"。** 上一版那句话漏掉了 `trace_id`、`event_id`、`step_id`、`capability_id`、`policy_revision` 五个——而其中四个是 `observability/log_sink.py` **现在就已经在输出**的（`extra` 里逐个可查），`trace_id` 更是 M5 的明确交付物。照那句话实施只有两种结果：要么按字面删掉现有输出，把 M5 唯一的请求关联手段砍掉；要么把它当成"大意如此"的散文，于是白名单形同虚设，将来任何一个新键都能悄悄进来。

  精确键集共 **13 个**（`extra` 的键，一个不多一个不少）：

  | 键 | 来源 | 备注 |
  | --- | --- | --- |
  | `trace_id` | `TraceEvent.trace_id` | 请求/执行关联，M5 交付物 |
  | `task_id` | `TraceEvent.task_id` | 受控 ID，可空 |
  | `event_id` | `TraceEvent.event_id` | UUID；日志行与 `task_audit_events` 行的唯一对照键 |
  | `stage` / `outcome` | 枚举 `.value` | 阶段与阶段判定 |
  | `step_id` / `capability_id` / `policy_revision` | `TraceEvent` 同名字段 | 现有输出，可空 |
  | `attempt_number` | `TraceEvent`（M5 新增） | 归属到哪一次尝试 |
  | `delivery_state` | 派生，见上 | 只在日志记录里存在 |
  | `error` | `AgentError` 的**类别**摘要 | 不含原文、不含 SQL/参数 |
  | `detail` | 已在契约层脱敏并限长的结构化 dict | 原样透出，不二次拼装 |
  | `worker_instance` | Worker 进程 owner | 键始终存在；非 Worker 事件取 `None` |

  用例：断言实际 `extra` 的键集**恰好等于**这张表（不是包含关系——包含关系挡不住新增键，与 `test_dependency_baseline.py` 用集合相等而非禁用清单是同一条判据）；断言 `trace_id` 与 `event_id` 非空；并发跑两个任务，断言两组日志记录的 `trace_id` 互不混入、`event_id` 全局不重复；在一次 emit 中途注入异常，断言下一条记录的 `trace_id` 不是上一条的（异常路径已 reset）。所有外部字符串经过顶层 `redaction.py`，不另写一份脱敏规则。
- `tests/contract/test_trace_stages.py`、`tests/unit/test_trace.py` 改为检查唯一性、task/attempt 归属和阶段语义，不断言顺序型 event_id。

### 5.2 外部文本在 Evidence 与终端上的真实处置

**证据层不做脱敏，这是当前代码路径的事实。** `evidence/builder.py::build_evidence` 只做**列白名单**（`_filtered` 按 `surface.allowed_columns` 过滤键），值与 `result.source` 是**原样保存**的；`rendering/slow_query.py` 又把 `queryId` / `queryTime` / `scanRows` 直接拼进 section body。所以证据里确实存在未经改写的外部值。

M5 的选择是**保留原始事实、不在证据层脱敏**：证据的定义就是"看到了什么"——把 `queryId` 改写成 `***` 会让证据不再能支撑任何结论，也会让 `redaction_ref` 从"没有需要引用的脱敏记录"变成一个必须维护的第二真源。既然保留，就必须证明这些值**只出现在受控展示面**，永不进入决策面：

| 面 | 要求 |
| --- | --- |
| policy / plan / target / 审批 | 外部值永不参与；已由 `test_external_content.py`、`test_intent_pollution.py` 等承重，M5 只需保证新链路不新开口子 |
| 结构化日志 | 固定键白名单，**不含 facts**；`detail` 恒为空；`TraceEvent.error` 走 `AgentError` 的结构化摘要而非原文 |
| 错误响应 | 闭集 code，无回显（§3.1） |
| API JSON | JSON 编码天然转义控制字符；由用例断言含换行/ANSI/引号的值往返后不破坏结构 |
| CLI 终端 | **必须转义**控制字符与 ANSI 序列后再打印（§3.2）——这是当前唯一真正缺失的一环 |

覆盖用例：secret 形状的值（按仓库约定拆开字面量构造）、恶意 key、恶意 `source`、内嵌换行、ANSI 控制序列，分别验证五个面。

### 5.3 配置来源与迁移凭证入口

应用端不接受拼好的 credential DSN。Settings 接受 PostgreSQL host、port、database、user 与 password-file 路径，在配置模块一个位置读取文件并构造连接参数；密码不进镜像、仓库、`.env.example`、命令行或日志。

**迁移的连接入口：程序化注入，`alembic.ini` 与 `env.py` 都不改。** `migrations/env.py` 的模块 docstring 已经把连接来源钉成三条并按优先级排序：程序化设置的 `connection` attribute、`-x dsn=` 命令行参数、`sqlalchemy.url` 主选项；并明确写着它**不读 `.env`、不读 `XIAOWEI_*`**。后两条来源都会把凭证写进仓库文件或进程命令行，与 secret 约束直接冲突，因此 M5 只能走第一条。

新增 `src/xiaowei_agent/interfaces/migrate.py` 作为迁移入口（Compose 的 `migrate` 服务命令就是它）：用与 API/Worker 完全相同的 `load_settings()` 读配置、同一个 password-file 读取函数取密码；在进程内构造连接，凭证只存在于内存的 URL 对象里；注入 Alembic 的 `connection` attribute 后 `command.upgrade(config, "head")`；成功退出 0，任何失败退出非 0 并只打印字段/类别，不打印 URL、密码或路径内容。

**必须走 async→sync 桥接，不能直接构造同步 `Engine`。** 仓库的 direct dependency 只有 `asyncpg`（没有 psycopg），因此 `sqlalchemy.create_engine("postgresql://...")` 在这个镜像里根本没有可用驱动；而 Alembic 的 `command.upgrade` 是同步 API，要的是同步 `Connection`。仓库里已经有一条走通的桥，`tests/integration/conftest.py` 的 `migrated_engine` 正是它：

```python
engine = create_async_engine(url, poolclass=sa.pool.NullPool)
async with engine.begin() as connection:          # AsyncConnection
    await connection.run_sync(_run_upgrade)       # 交给 greenlet 里的同步 Connection
# _run_upgrade(connection) 内部：
#   config.attributes["connection"] = connection
#   command.upgrade(config, "head")
```

`interfaces/migrate.py` 必须复用**同一个** `_run_upgrade` 形状：把它连同 `_run_downgrade` 一起从 `tests/integration/conftest.py` 提升为 `src/xiaowei_agent/persistence/migrations/runner.py` 里的共享函数（两者必须一起移动，否则 upgrade 在 `src/`、downgrade 在 `tests/` 会成为一对不对称的实现，而 `test_migration_paths.py` 的可逆性用例正好同时用到它们）。conftest 里三个引用点全部改为 import。入口是 `asyncio.run(...)`，并在 `finally` 里 `await engine.dispose()`。这样"CI 与 Compose 走同一条代码路径"才是真的——只写"构造 Engine、注入 connection"的话，一个照字面实现的同步版本会在 Compose 的 migrate 服务上直接失败，而全部 integration 测试仍然全绿，因为它们走的是另一条（正确的）路径。

`alembic.ini` 保持现状：ASCII-only、不含 `sqlalchemy.url` 取值，一个字节都不改，因此 `tests/contract/test_config_encoding.py` 承重的 locale 解码约束不受影响。**明确否决**的两条替代：在 `alembic.ini` 写 URL（凭证进仓库）、在 Compose 命令里传 `-x dsn=...`（凭证进 `docker compose ps`、进程表和日志）。§4.5 的破坏性降级开关**不走 `-x`**（那条通道在程序化入口下读不到，理由见 §4.5），而是 `interfaces/migrate.py --allow-destructive` 经 `config.attributes` 传入；它是一个布尔标志、不含任何凭证，因此与这条否决无关。

CI integration 保留唯一 `PYTEST_POSTGRES_DSN` 测试入口和 `127.0.0.1:5432`；该变量一旦设置，integration 不允许 skip。Compose 内部使用 hostname `postgres`，不得复用 CI DSN。

`Settings`、`_FIELD_TO_ENV`、`.env.example` 与配置测试必须精确一一对应。unknown `XIAOWEI_` 变量继续 fail-fast。新增 validator 覆盖 §4.3 与 §4.7 两张表里的全部默认值与上下界，以及：password file 必须是普通可读文件，错误文本只报告字段/类别，不报告路径内容或 secret；tenant/environment 仍来自受信配置，不能由 request override。

**actor 与读取 ACL 拍板：**

1. **actor 来源。** `Settings` 目前只有 `environment_id` 与 `log_level`，`tenant_id` 是代码常量（`DEFAULT_TENANT_ID = "dev-local"`，ADR-007 D2 明确不接受环境入口）。M5 新增一个受信配置字段 `actor`（本地开发身份），与 tenant 同样**不接受任何请求侧覆盖**。它不是认证——ADR-010 必须写明 M5 只有固定本地身份 adapter，不构成生产授权。
2. **actor 不是读取 ACL。** `TaskLookup` 只含 `task_id` + `tenant_id` + `environment_id`，**不含 actor**：M5 里同一 tenant/environment 下的不同 actor 可以互相读取任务。这是一个真实的放宽，必须在 ADR-010 里写明理由（M5 是单租户本地闭环，per-actor ACL 需要先有真实身份体系，否则只是一个可被配置绕过的假边界）与代价（多 actor 部署下不成立），并由一条用例把它**固定成已知行为**——而不是让它以"忘了加"的形态存在。同时保留反例：**跨 tenant / 跨 environment 的读取必须是统一 404**。
3. **无请求侧入口的反证。** `request_id` / `trace_id` / `tenant_id` / `environment_id` / `actor` / `channel` / `policy_revision` 全部由服务端生成或来自受信配置，因此不加长度上限，但要有一条用例断言它们确实没有请求侧入口（`idempotency_key` 与 `text` 的上限见 §4.5 与既有 8192）。

### 5.4 fake 隔离、模块层级和依赖

- 只允许 `interfaces/local_stack.py` 直接 import fake ToolGateway adapter 与回放语料（interpreter 用的是正式的 `RuleBasedIntentInterpreter`，不在 fake 之列）。**组装根不能放在 `application/`**：`tests/security/test_runtime_bypass.py::test_application_never_imports_the_gateway_module` 扫描 `application/**` 的全部 AST import，断言没有任何文件 import `xiaowei_agent.tools.gateway`；而组装根必须 import 它才能构造 `DeterministicToolGateway`。同文件的 `test_application_never_touches_gateway_invoke` 还禁止 `application/**` 出现 `invoke` 这个属性名。组装根按定义属于入口层，放 `interfaces/` 既满足护栏，也不需要任何白名单例外。
- `application/worker.py` 只 import `application` / `persistence` / `contracts` / `observability` 层，不 import 任何 adapter、gateway 或 fake；它的依赖由 `interfaces/worker.py` 从 `LocalStack` 解包后注入（§3.3）。
- `interfaces/api.py` 与 `interfaces/worker.py` 不得 import `tools.fake`；它们只接收装配完成的应用服务。

**入口层必须用穷尽的文件级允许表，不能用一张包级并集。** 当前检查器按包扫描；若给整个 `interfaces` 同时放行 `application`、`persistence`、`observability`，`api.py` 直连 TaskStore、`cli.py` 直连 Runtime 都会全绿。另一个已核对的事实是 `_UNIVERSAL_LEAF` 当前**只有** `xiaowei_agent.redaction`；`config`、`trace`、`log` 不在其中，而且 `log.py` 自己还依赖 `config` / `trace` / `redaction`，不能被虚构成通用叶子。

因此把 `interfaces` 的规则改成一张**对磁盘文件穷尽**的 `_ALLOWED_INTERNAL_BY_FILE`。每个 `interfaces/*.py` 必须恰好登记一次；磁盘多文件或表中多路径都失败。值是该文件可 import 的项目内模块前缀，标准库和第三方依赖仍按既有检查器单独处理：

**文件级检查与现有包级检查一样，最终允许集必须是该文件条目与 `_UNIVERSAL_LEAF` 的并集。** `redaction` 只在通用叶子集合登记一次，不在每个文件条目重复列出；因此 CLI 可以直接复用顶层 `redaction.py`，而不会制造第二份脱敏规则。

```python
_ALLOWED_INTERNAL_BY_FILE = {
    "interfaces/__init__.py": {"xiaowei_agent.interfaces"},
    "interfaces/api.py": {
        "xiaowei_agent.application", "xiaowei_agent.contracts",
        "xiaowei_agent.interfaces", "xiaowei_agent.config",
        "xiaowei_agent.log", "xiaowei_agent.trace",
    },
    "interfaces/cli.py": {
        "xiaowei_agent.contracts", "xiaowei_agent.interfaces",
    },
    "interfaces/worker.py": {
        "xiaowei_agent.application", "xiaowei_agent.interfaces",
        "xiaowei_agent.config", "xiaowei_agent.log", "xiaowei_agent.trace",
    },
    "interfaces/migrate.py": {
        "xiaowei_agent.persistence", "xiaowei_agent.config",
        "xiaowei_agent.interfaces",
    },
    "interfaces/auth.py": {
        "xiaowei_agent.config", "xiaowei_agent.contracts",
        "xiaowei_agent.interfaces",
    },
    "interfaces/body_limit.py": {"xiaowei_agent.interfaces"},
    "interfaces/http_models.py": {
        "xiaowei_agent.contracts", "xiaowei_agent.interfaces",
    },
    "interfaces/local_stack.py": {
        "xiaowei_agent.application", "xiaowei_agent.capabilities",
        "xiaowei_agent.contracts", "xiaowei_agent.evidence",
        "xiaowei_agent.governance", "xiaowei_agent.interfaces",
        "xiaowei_agent.observability", "xiaowei_agent.persistence",
        "xiaowei_agent.planning", "xiaowei_agent.reflection",
        "xiaowei_agent.rendering", "xiaowei_agent.runners",
        "xiaowei_agent.tools", "xiaowei_agent.config",
        "xiaowei_agent.log", "xiaowei_agent.trace",
    },
}
```

`local_stack.py` 是唯一宽组装根，也是 fake 白名单唯一放行文件；其余文件没有“继承包级权限”的后门。配套元测试：(1) 表的 key 集合与磁盘 `interfaces/*.py` 集合相等；(2) 每个路径只出现一次且存在；(3) fake 白名单恰为 `interfaces/local_stack.py`；(4) 三条独立变异：给 `api.py` 加 `persistence` import、给 `cli.py` 加 `application` import、给任一 `tools/*.py` 加 `capabilities` import，分层测试都必须转红。最后一条同时证明现有 `tools` 包允许集仍是 `{contracts, planning, tools}`，没有为了 recording 放宽。

`interfaces` 包必须在**创建它的那个提交**里登记为“由文件级表承重”的包：`test_every_existing_package_is_registered` 与 `test_every_registered_package_exists` 继续检查包集合；新增的文件集合相等元测试负责阻止漏扫。方向性 AST 禁止清单保留为第二层独立保护，但不能代替正向 allowlist。

**装配内容拍板，不留给实施时选：** interpreter 用 `capabilities/intent.py` 的既有 `RuleBasedIntentInterpreter`（M5 不写第二个 interpreter）；resolver 用 `DeterministicCapabilityResolver`；gateway 是 `DeterministicToolGateway({GATEWAY_NAME: StarRocksRecordingAdapter(...)})`；runner 是 `DeterministicWorkflowRunner`。

**运行镜像里必须有一份 recording，否则 fake gateway 根本构造不出来。** 镜像按 §6.3 不包含 `tests/`，而目前唯一现成的回放语料在 `tests/fakes/recordings.py`；`StarRocksRecordingAdapter.__init__` 又在空映射时直接 `raise ValueError("StarRocksRecordingAdapter requires at least one entry")`。也就是说：照上一版的写法构建出的镜像，Worker 在装配阶段就会崩，而全部单元/契约测试仍然全绿（它们从 `tests/` 拿语料）。

因此新增一份**最小的、构造的**只读响应数据：list / count 各一条 OK 响应，行内容全部合成，不含任何真实主机名、IP、用户名或 SQL 原文，与 `tests/fakes/recordings.py` 的脱敏约定一致。

**它必须放进 `tools/` 并登记为 fake，不能放在 `interfaces/` 并以"它不是 adapter"为由绕过 fake 注册表。** 上一版正是这么写的，而那是一次纯粹的命名规避：这份数据的**全部作用**就是让一个 fake adapter 假装从 StarRocks 取到了行，它是伪造的外部响应本身。以"类型不是 adapter"把它排除在 `_FAKE_MODULES` 之外，等于给"生产模块可以直接 import 伪造的外部数据"开了一条按类型判定的口子——而 `test_fake_isolation.py` 三条断言（自我标识、不从包入口导出、无生产模块导入）防的恰好是这件事。

落点因此是 `src/xiaowei_agent/tools/starrocks_recording.py`，声明 `IS_FAKE: Final[bool] = True`，并加入 `_FAKE_MODULES`。该文件只定义合成 `AdapterResponse` 数据/工厂，**不得 import `capabilities` 或复制 operation 常量**；`interfaces/local_stack.py` 分别从 `capabilities` 读取既有 `OP_LIST` / `OP_COUNT`，从该 recording 模块读取响应，再组装映射。这样 fake 数据归 `tools`、operation 身份仍归 capability 真源，且 `tools → capabilities` 的反向依赖不会被引入。

承重用例三条：**（i）** `src/` 任何模块都不得 import `tests.fakes.recordings`；**（ii）** `_FAKE_MODULES` 包含 `tools.starrocks_recording`，因此它自动被三条既有 fake 断言覆盖；**（iii）** 构建产物中不含 `tests/` 目录时 `local_stack` 仍能装配（见 §3.3 对这条用例边界的说明）。

其余固化项：

- 修复 fake import 检测的 exact-match 边界。`_imports_a_fake` 现在只匹配 `module.endswith(".fake")` 与 `ImportFrom` 里 `alias.name == "fake"`，而 `xiaowei_agent.tools.starrocks_fake` 两条都不命中，`from xiaowei_agent.tools import starrocks_fake` 也不命中——**今天 `main` 上任何生产模块都可以无声地 import 它**（既有缺陷，非 M5 引入）。改为对 `_FAKE_MODULES` 做精确模块匹配，并覆盖 `from <pkg> import <name>` 形态；`test_the_detector_catches_both_import_spellings` 补一条 `starrocks_fake` 反例，保持"检测器自身先被证明有效"的结构。
- `test_no_production_module_imports_a_fake` 只放行 `interfaces/local_stack.py` 这一个路径，并补反向断言：除 `interfaces/worker.py`、`interfaces/api.py`、`interfaces/migrate.py` 的装配调用外，没有别的生产模块 import `local_stack`，且 `local_stack` 不从任何 `__init__.py` 导出。放行必须精确到文件，不得把 `interfaces/` 整个目录排除。
- `pyproject.toml` runtime 新增 `fastapi`、`uvicorn`；dev 新增 `httpx` 与只供 compose 契约测试使用的 `PyYAML`；更新 `uv.lock`。`tests/security/test_dependency_baseline.py` 的 `_EXPECTED_RUNTIME_DEPENDENCIES` 从 5 项变 7 项，并新增/更新**完整 dev 直接依赖集合相等**断言，使 `httpx` / `PyYAML` 的加入和任何额外 dev 依赖都必须被 review 看见；`fastapi` / `uvicorn` 仍不得从 dev 侧重复引入。`PyYAML` 不得出现在 runtime 依赖或应用镜像中。不引入 CLI 框架、任务队列或额外 HTTP 客户端。

## 6. Compose 服务、网络、volume、healthcheck 与 migration

> 本节全部为静态推理：本机 `PATH` 上没有 `docker`（§1.1）。标注「**须实机确认**」的三项在实施时必须由真实 `docker compose` 验证并把结果写回本节，不得凭文档口径通过。

### 6.1 服务

| 服务 | 镜像/命令 | depends_on | health/readiness |
| --- | --- | --- | --- |
| `postgres` | 固定 major/digest 的 PostgreSQL | 无 | `pg_isready`，使用非 secret 参数 |
| `migrate` | 应用镜像；`python -m xiaowei_agent.interfaces.migrate` | `postgres: service_healthy` | one-shot，退出 0 才算成功 |
| `api` | 同一应用镜像；uvicorn/FastAPI | `migrate: service_completed_successfully` | 容器 healthcheck 调 `/healthz`；编排/验收另查 `/readyz` |
| `worker` | 同一应用镜像；worker 命令 | `migrate: service_completed_successfully` | 进程存活；连续 infra 故障窗口超限后非零退出 |

API/Worker/migrate 由一个 Dockerfile 构建，禁止为了 Compose 方便拆仓库模块或复制依赖。Compose 文件名沿用路线图既定的 `docker-compose.yml`（`DEVELOPMENT_PLAN.md` §7 M5 交付物逐字如此），不改成 `compose.yaml`。

**须实机确认（1）：`up --wait` 与 one-shot `migrate` 的组合。** Compose 支持 `depends_on: {condition: service_completed_successfully}`，而 `docker compose up --wait` 的文档口径是"等待服务达到 running / healthy"。一个**必然退出**的 one-shot 服务在 `--wait` 下是被视为满足条件、还是被判为不健康，取决于具体 Compose 版本，静态推理给不出答案。实施时必须实测，并把实际行为与版本号写回本节。已备好的退路（若 `up --wait` 不接受退出的服务）：

```bash
docker compose -p "$P" up -d --wait postgres
docker compose -p "$P" up migrate            # 前台运行，保留日志
test "$(docker compose -p "$P" ps -a --format '{{.ExitCode}}' migrate)" = "0"
docker compose -p "$P" up -d --wait api worker
```

两条路都保留 `migrate` 容器（`ps -a` 可见 `Exited (0)`），因此退出码与日志这两样最该留档的证据都在。**不用 `run --rm migrate`**：它在迁移结束后删掉容器，证据消失，`service_completed_successfully` 也找不到那次成功记录。

### 6.2 网络和暴露

- 使用 Compose 默认私有网络；PostgreSQL 只在网络内暴露 5432，不发布宿主端口。
- API 容器内 uvicorn 监听 `0.0.0.0:8000`，宿主侧**只**发布 `127.0.0.1:8000:8000`。安全面在发布规则上，不在容器内监听地址上——理由见 §4.3。
- Worker 无入站端口。
- 不使用 host network、Docker socket、privileged、额外 capabilities。
- **不使用 `container_name`**——它会让 `--scale worker=2` 直接失败，而并发 Worker 正是 smoke 必须证明的一条。

### 6.3 volume、secret 与镜像形状

- PostgreSQL 数据放 **project-scoped named volume**，Worker 重启不能丢任务。

**"随机 project name 即资源隔离"是 §5.4 smoke 清理安全的前提，而这个前提由 compose 文件的形状决定，必须在文件上断言。** 随机 project 与碰撞预检约束的是**名字**；它们默认成立的是"这个 project 的容器、网络、volume 都归这个 project 所有，`down --volumes` 只会删到自己的东西"。有三种写法会让这个默认不成立，而它们在 compose 文件里都是一行：

| 写法 | 后果 |
| --- | --- |
| volume / network 带顶层 `name:` | 名字被写死成全局名，不再带 project 前缀；两个 project 共用同一个 volume，`down --volumes` 删掉的是共用的那个 |
| `external: true` | 资源由 project 之外的人创建和拥有；`down --volumes` 对它的处置取决于 Compose 版本，而它本来就不该被这个脚本碰 |
| PostgreSQL 数据用 bind mount | 数据落在**宿主目录**上，随机 project name 完全不构成隔离；一次 `down --volumes` 之外的误操作直接作用于宿主文件系统 |

因此 `tests/contract/test_compose_contract.py` 使用 `yaml.safe_load` 做语义解析（不调用 Docker）。Python 标准库没有 YAML 解析器，不能用正则或手写缩进解析器冒充；M5 明确新增 `PyYAML` 为**直接 dev 依赖**并锁入 `uv.lock`，运行镜像不安装它：

- **资源归属**：`volumes:` 与 `networks:` 下的每一项都**没有** `name:` 键、**没有** `external: true`；PostgreSQL 的数据目录挂载必须是 named volume 引用，其 `source` 出现在顶层 `volumes:` 里，且**不是**以 `.` 或 `/` 开头的路径（那是 bind mount 的形状）；
- **暴露面**：每个 `ports:` 条目以 `127.0.0.1:` 开头；`postgres` 服务没有 `ports:`；
- **宿主逃逸**：无 `network_mode: host`、无 `privileged`、无 `cap_add`、无 `/var/run/docker.sock` 挂载、无 `container_name`；
- **镜像同一性**：`api` / `worker` / `migrate` 三个服务的 `image` 逐字相同；
- **secret**：只出现 file reference，不出现内联值。

反例逐条（改一份临时 compose 文档喂给同一个解析器）：给 volume 加 `name:`、加 `external: true`、把数据目录换成 `./pgdata` bind mount、给 postgres 加 `ports:`、把 published port 的 `127.0.0.1:` 去掉——五条各自必须转红。

三份文件的契约不同，不能要求两个 override 独立拥有基线的完整服务/网络/volume：

- `docker-compose.yml` **独立**通过上面的全部安全与镜像断言；
- `docker-compose.smoke.yml` 独立只允许修改 `services.worker.environment` 下三个已登记 Settings 键：`XIAOWEI_LEASE_TTL_SECONDS`、`XIAOWEI_HEARTBEAT_INTERVAL_SECONDS`、`XIAOWEI_WORKER_POLL_INTERVAL_SECONDS`；不得新增服务、端口、volume、network、image、command、mount 或其它环境变量；
- `docker-compose.barrier.yml` 独立只允许增加 `services.worker.environment.XIAOWEI_SMOKE_STEP_BARRIER=true`，除此之外无任何节点；
- 有 Docker 时，再分别对 `base + smoke` 与 `base + smoke + barrier` 执行 `docker compose ... config`，把合并后的 YAML 喂给**同一个完整断言函数**。这样静态测试证明 override 没越权，Compose 实测证明合并语义也没有覆盖安全字段。
- 开发 secret 通过 Compose `secrets` 的 file reference 注入 `/run/secrets/...`；仓库只提交路径示例和 `.example`，不提交实际 secret 文件。
- **干净 checkout 里没有 secret 文件**，因此 smoke 第一步必须自己创建一个临时的：父目录精确为 `0700`，文件先以 `0600` 写入、关闭后改成只读 `0444`，路径在 `.gitignore` 覆盖范围内，并在 `finally` 删除。file-backed Compose secret 实际由 bind mount 提供，不能依赖 `uid` / `gid` / `mode` 重映射；非 root 应用进程的可读性由文件 `0444` 承担，宿主其他用户的隔离由不可遍历的 `0700` 父目录承担。父目录过宽、文件已存在或创建/改权限失败都必须在启动任何服务前 fail-closed。这与"Compose file reference 不存在时必须 fail-fast"是两件事：前者是 smoke 的前置，后者是一条独立负面用例。

`Dockerfile` 形状固化：基础镜像固定到 **digest**（实施时解析真实值，不提交占位）；依赖用 `uv sync --frozen --no-dev`（`--frozen` 保证与 `uv.lock` 一致，`--no-dev` 保证测试工具链不进运行镜像）；以**非 root** 用户运行；命令用 **exec form**（`CMD ["python", "-m", ...]`），否则信号被 shell 吞掉、SIGTERM 传不到进程，优雅停止失效；`alembic.ini` 与 `persistence/migrations/` 必须进镜像；`.dockerignore` 排除 `.git`、venv、缓存、报告、`.env`、本地 secret 目录**与 `tests/`**（后者正是 §5.4 那份 `tools/starrocks_recording.py` 存在的原因）。

`healthcheck` 不依赖 `curl`（镜像里没有，也不该为它装）：用同一 Python 解释器执行一条标准库单行请求，写成显式 argv 数组，并给出精确的 `interval` / `timeout` / `retries` / `start_period`。**须实机确认（2）**：healthcheck 的 argv 形式在目标基础镜像上确实可执行（无 shell 依赖）。

`.gitignore` 屏蔽本地 secret 文件、Compose override、coverage/pytest/mypy/ruff 缓存和 smoke 证据临时目录。

### 6.4 readiness 语义

API `/readyz` 仅验证三件事：PostgreSQL 可执行轻量 ping；当前 migration revision 等于 head；应用依赖已完整装配。

不扫描任务、不尝试 acquire lease、不调用 fake gateway、不把 orphan submission 当全局不就绪。单个任务 submission 缺失/损坏由 `begin_task_attempt` 在行锁内原子置 `FAILED`（§4.3 第 7 步），因此不存在"要先有 fencing owner 才能终态化"的循环；readiness 与它完全无关，一条损坏任务不能拖垮整个进程的就绪。

`/readyz` 读取当前 migration revision 与 head 比较时，必须走 §5.3 的同一条 async→sync 桥接（`connection.run_sync` 里用 `MigrationContext.get_current_revision()`）。直接构造同步 Connection 会因为仓库只有 asyncpg 驱动而失败——这与迁移入口是同一个陷阱，不能只在 migrate 那侧修。

## 7. 文件级 TDD 实施顺序

每个任务严格执行：先新增会因真实缺口失败的测试并记录红灯原因，再写最小实现，再跑本任务测试与相邻回归。不得先写实现后补绿灯测试；不得通过 monkeypatch 绕过真实调用链。

**每个纵向切片自带自己的 Alembic revision，不共用一个 `rev_0002`。** 上一版让 Task 1 创建唯一的 `rev_0002`，但 Task 1 的红灯只覆盖 `TaskRecord` 四字段；`task_submissions` 到 Task 2 才出现、retry 标记列到 Task 3、step journal 到 Task 4，而那三个 Task 的文件清单里**没有任何迁移或 schema 文件**。照它实施只有两种结果：要么 Task 1 就把后面三个 Task 的表全建了（那些表此刻没有任何代码使用，`test_schema_matches_migration.py` 会因 metadata 与 migration 不一致而红），要么后三个 Task 去改一个已经写完的 revision（改历史迁移，且前面 Task 的 upgrade/downgrade 用例失效）。两条都做不到"每个 Task 结束时全绿"。

因此按切片顺序编号，每个 Task 自带 schema 变更、migration、以及针对它自己那部分的 PostgreSQL 可逆性用例：

| Task | revision | 内容 |
| --- | --- | --- |
| 1 | `rev_0002_task_execution_columns` | `TaskRecord` 四字段 + fencing/created 序列 + `idempotency_scope_digest` 唯一索引替换 |
| 2 | `rev_0003_task_submissions` | `task_submissions` 表 + M4 历史任务的确定性终态化（§4.5：这里才是"非终态任务必须有 submission"这条不变量开始成立的时刻） |
| 3 | `rev_0004_retry_markers` | `retry_scheduled_by_attempt` / `retry_command_digest` 两列 |
| 4 | `rev_0005_step_journal` | step journal 表（含 `kind` / `commit_digest`） |

**`revision` / `down_revision` 的取值在这里写死，因为文件名不是 revision id。** 核对当前仓库：`rev_0001_initial_task_store_schema.py` 里 `revision = "0001_initial"`、`down_revision = None`——**文件名带 `rev_` 前缀，revision id 不带**。上一版全篇拿文件名当 revision id 用，于是 §9.4 的回滚命令指向一个不存在的 `rev_0002`。四个新 revision 一律沿用 M4 的命名法（id 不带 `rev_` 前缀）：

| 文件 | `revision` | `down_revision` |
| --- | --- | --- |
| `rev_0002_task_execution_columns.py` | `0002_task_execution_columns` | `0001_initial` |
| `rev_0003_task_submissions.py` | `0003_task_submissions` | `0002_task_execution_columns` |
| `rev_0004_retry_markers.py` | `0004_retry_markers` | `0003_task_submissions` |
| `rev_0005_step_journal.py` | `0005_step_journal` | `0004_retry_markers` |

一条用例逐条断言这八个取值，并断言 `alembic heads` 恰好一个（四个 revision 是一条链，不是四个 head）。

**`rev_0002.downgrade` 必须把旧唯一约束按**原名**建回来，否则回滚到 M4 代码契约后 `create_task` 会直接失败。** `postgres.py:254` 写的是 `.on_conflict_do_nothing(constraint="uq_tasks_idempotency_scope")`——PostgreSQL 的 `ON CONFLICT ON CONSTRAINT` 按**约束名**解析，而 `rev_0002.upgrade` 把它换成了 `uq_tasks_idempotency_scope_digest`。也就是说：把 schema 降到 `0001_initial` 后，M4 的任务创建 SQL 会因为"约束不存在"报错。这条缺陷在只检查列的可逆性用例里完全看不见。

因此 `rev_0002.downgrade` 的顺序是：建回 `UNIQUE (tenant_id, environment_id, idempotency_key)` 且名字**逐字**为 `uq_tasks_idempotency_scope` → 删掉 digest 唯一约束 → 删列。先建后删，中途不留"两条约束都没有"的窗口。

**降级守卫按 revision 各管各的**（§4.5）：`rev_0003.downgrade` 守 `task_submissions` 有无行，`rev_0005.downgrade` 守 step journal 有无行；两处共用同一个 helper 与同一个 `allow_destructive` 读取方式，不各写一份判据。`rev_0002` / `rev_0004` 只增删列与约束，无数据语义守卫。

**`rev_0003` 的降级不是数据可逆的，必须说清楚。** 它的 upgrade 把 M4 遗留的非终态任务写成 `FAILED` + `legacy_task_without_submission`（§4.5）。downgrade 删掉 `task_submissions` 表，但**不会**把那些任务改回原状态——原状态在升级时就没有被保存，而伪造一个"改回去"的猜测比留着终态更坏。所以"schema 可逆"与"数据语义可逆"在这条 revision 上是分开的，`--allow-destructive` 这个名字覆盖的正是这一半。

`test_schema_matches_migration.py` 因此在每个 Task 都成立：那一刻 metadata 里有的，migration 链跑到 head 之后也恰好有。

**任务切分的判据是"能不能在本任务结束时全绿"，不是"属于哪一层"。** 上一版按层切（先契约、再存储、再调用方），结果第一个任务就无法收敛：它把 TaskStore Protocol 改成 14 方法并运行 `test_protocol_conformance.py`，而两个实现要到三四个任务之后才补齐；同一任务还给 `TaskRecord` 加了无默认值的必填字段，而全部构造点与行映射留在后面。这类改动是**横切**的——Protocol、两个实现、fixture、行映射和全部构造点必须同一提交才能同时为真。因此下面按**纵向切片**重排，每个 Task 自身可编译、可测试、可全绿。

以下三项贯穿多个 Task，统一约定，各 Task 不再重复：

**(a) 共享行为套件的登记。** 新增的 TaskStore 行为必须写进 `tests/suites/task_store.py` 的 `*_CASES` 分组，并把四个新绑定模块登记进 `tests/contract/test_suite_bindings.py` 的 `_BINDINGS`：

| 绑定模块 | 套件 | 分组 | 实现 |
| --- | --- | --- | --- |
| `tests.contract.test_dispatch_and_attempts` | task_store | dispatch_attempts | memory |
| `tests.integration.test_dispatch_and_attempts_postgres` | task_store | dispatch_attempts | postgres |
| `tests.contract.test_step_execution_store` | task_store | step_execution | memory |
| `tests.integration.test_step_execution_postgres` | task_store | step_execution | postgres |

`test_suite_bindings.py` 防的是四条静默失效路径（用例漏进分组、绑定漏挂、元测试不知道新绑定、绑定挂错分组）。漏掉这一步的表现是"写了测试但只在一个实现上跑过"，而所有现有用例照常全绿。

**(b) Protocol 类型锚点。** `src/xiaowei_agent/_conformance.py` 必须新增 `AuditEventWriter` 与它的 TaskStore 实现锚点；`TraceSink` 从 `test_protocol_conformance.py` 的 `_FROZEN_WITHOUT_IMPLEMENTATION` 移入 `_ANCHORED`（它已经有 `StructuredLogTraceSink`，M5 再加 durable sink）。**不新增 ExecutionJournal Protocol**，因此没有对应锚点。全部锚点仍位于 `TYPE_CHECKING` 块内，锚点模块的运行时 import 集合必须保持只有 `typing`。

**(c) Ruff 处置。** `scripts/compose_smoke.py` 会被 `ruff check .` 扫到，而 `per-file-ignores` 目前只豁免 `tests/**`。处置固定为：用 `shutil.which("docker")` 解析出的**绝对路径**作为 argv[0]（不触发 `S607`）；对每一处 `subprocess.run(...)` 加**逐行** `# noqa: S603` 并在同一行注明"argv list + shell=False + 绝对路径已解析"；模块内全部函数补齐类型标注满足 `ANN`。**不给 `scripts/**` 开 per-file-ignore**。

同一条规则适用于 `S104`（`hardcoded_bind_all_interfaces`）：`src/` 里出现 `"0.0.0.0"` 字面量会直接转红。处置是**不在 `src/` 写这个字面量**：容器内监听地址是 `Settings` 的一个字段，默认值由 Compose 的环境变量提供，`src/` 侧只做"是合法 IP 字面量"的校验。若最终仍需在 `src/` 留默认值，则逐行 `# noqa: S104` 并注明"容器内监听，宿主暴露面由 Compose published port 约束（§4.3）"，不得放宽规则集。

### Task 0：ADR-010 与架构契约前置（文档，不改 `src/` 与 `tests/`）

`DEVELOPMENT_PLAN.md` §8 的执行协议第 2 条写死："涉及新契约或不可逆选择时先写 ADR。未批准不实现。"而 M5 的第一个代码任务就要改 `WorkflowRunner` 与 `TaskStore` 两个已验收的稳定契约，`ARCHITECTURE.md` §5.6 里还冻结着旧的 Runner 签名。ADR 写在中途、架构更新拖到最后，意味着**中间十几个任务里仓库文档描述的契约是假的**。

- 新增 `docs/adr/ADR-010-m5-durable-attempt-and-compose-boundary.md`（编号取自 `docs/adr/` 现有最大值 009 的下一个）。ADR 固化的不可逆选择：唯一领取点（`begin_task_attempt`）与 Runner 只收 grant；execution attempt / task failure / infrastructure window 三套计数分离，且基础设施故障不进 `schedule_retry`；fencing token 有两个推进点（取得租约、`schedule_retry`）；retry 用 `lease_expires_at = next_attempt_at` 表达交接而不新增 `release_lease`；TaskStore 14 方法仍是单一 aggregate 事务端口；状态改动与其审计同事务，begin 内部提交的终态事件经 result 返回补日志；持久化写明确区分 confirmed rollback 与 not-confirmed；audit 不做 fencing但 attempt_number 必填；`created_seq` 只承诺近似公平；幂等唯一索引改建在定长 digest 上，降回 M4 前必须通过旧索引 compatibility probe；`handle()` 保留 M3 提交顺序、只共享终态投影与 Runner；异步查询路径的 `AWAITING_APPROVAL` 返回 `render=null`；`TaskLookup` 不含 actor（同 scope 内跨 actor 可读）；组装根落在 `interfaces/` 且 `LocalStack` 用 frozen dataclass；**`ReadinessProbe` Protocol 落在 `contracts/`、实现落在 `persistence/`；文件级入口允许表继承唯一的 `_UNIVERSAL_LEAF`**；升级/降级各自会判死一批在途任务；`APPROVAL_RESUME` 在 M5 只有测试调用方而仍然保留；持久化故障只分不可用与系统性两类、两类都不改任务；`submit_task` 返回 `TaskView`；迁移期终态化不写审计。
- 更新 `ARCHITECTURE.md`：§5.6 的 `WorkflowRunner` 稳定接口改为带 `grant` 的新签名；§7.3 的 TaskStore 语义补上 dispatch/attempt/step-execution 三组能力、"发现与领取分离"以及 **fencing token 的第二个推进点**；§11 的 Compose 拓扑补上 `migrate` 一次性服务。
- 更新 `AGENT_HANDOFF.md` 的"下一步"条目指向实施时获批的**当前计划版本**，不得硬编码旧版号。这条在每次计划升版时必须同步，否则 handoff 会指向一份已被替换的设计。

验证（本任务只有文档，用既有文档门）：

```bash
python -m pytest tests/security/test_docs_command_consistency.py tests/contract/test_doc_fact_binding.py -q
ruff check .
```

### Task 1：`TaskRecord` 四字段 + schema + migration + 两实现 + 全部构造点

**这是一个横切提交，不能再拆。** 四个新字段中 `created_seq` 必须 `> 0` 且必填，因此加字段的那一刻，`TaskRecord(...)` 的每一个构造点、`record_to_row` / `row_to_record`、schema、migration、两个实现的插入语句都必须同时成立。

先改测试（红灯）：`tests/contract/test_schema_matches_migration.py`、`tests/contract/test_row_mapping.py`、`tests/security/test_deep_immutability.py`、`tests/security/test_serialisation_hygiene.py`、`tests/integration/test_migration_paths.py`、`tests/suites/task_store.py`。

红灯断言：四字段 strict 且出现在 metadata / migration / `_TASK_COLUMNS` / 行映射四处；sequence 回填按 `task_id` 稳定排序、`setval` 严格高于最大值；新的 `idempotency_scope_digest` 唯一约束替换旧的三列约束且历史行已回填（§4.5）；`rev_0002` 的 upgrade/downgrade/upgrade 在**只有 M4 数据**时可逆；`revision` / `down_revision` 恰为 `0002_task_execution_columns` / `0001_initial`（§7 前言）；**降级前 compatibility probe 先通过，降级后 `uq_tasks_idempotency_scope` 以原名回到 `pg_constraint`，且一条 M4 形状的 `ON CONFLICT ON CONSTRAINT` 插入真正执行成功**——这是 SQL 契约兼容测试，不冒充跨镜像运行。（`retry_scheduled_by_attempt` / `retry_command_digest` 属于 `rev_0004`，不在本任务。）

再改实现：`contracts/task.py`、`persistence/schema.py`、新增 `persistence/migrations/versions/rev_0002_task_execution_columns.py`（只含四字段、序列与幂等索引替换；M4 历史任务的终态化属于 `rev_0003`，见 §7 前言的 revision 表）、新增 `persistence/errors.py`（`PersistenceUnavailableError` / `PersistenceIntegrityError` 两个异常与驱动异常映射，含写死的五层 `except` 顺序，§4.7）、`persistence/rows.py`、`persistence/postgres.py`、`persistence/fake.py`、以及全部 `TaskRecord` 构造点（`tests/conftest.py`、`tests/fakes/**`）。

验证：`python -m pytest tests/contract/test_schema_matches_migration.py tests/contract/test_row_mapping.py tests/security/test_deep_immutability.py tests/security/test_serialisation_hygiene.py tests/integration/test_migration_paths.py -q`

### Task 2：`TaskSubmission` / `TaskLookup` / `submission_digest` 与两个签名收窄

方法数仍是 8，`test_protocol_conformance.py` 的哨兵**不动**——本任务只改两个既有方法的入参形状。

先改：`tests/conftest.py`、`tests/integration/conftest.py`、`tests/suites/task_store.py`、`tests/contract/test_task_store_contract.py`、`tests/integration/test_task_store_contract_postgres.py`。

红灯断言：store/ledger/plan-store 共享同一 state；task 和 submission 要么都可见要么都不可见；相同语义幂等返回同一 task 且**保留首次 submission**；不同语义冲突；原始 aware `as_of` 被持久化；`request_dedup_digest` 排除时间/trace/request id 但包含 channel；`submission_digest` 覆盖三者全部字段（逐字段篡改各一条反例）；裸 task_id 或错误 scope 不能读取 task；`idempotency_key` 恰好 200 与 201 字符两条边界。

再改：`contracts/request.py`（`idempotency_key` 上限）、`persistence/store.py`（`create_task` / `get` 签名、`submission_digest` 函数）、`persistence/schema.py`、新增 `persistence/migrations/versions/rev_0003_task_submissions.py`（含 §4.5 的 M4 历史任务确定性终态化与 `task_submissions` 的降级守卫）、`persistence/fake.py`、`persistence/postgres.py`、`application/runtime.py`（构造 `TaskSubmission` 的那一处）。

### Task 3：dispatch / begin_task_attempt / schedule_retry（8 → 11 方法）

先改/新增：`tests/suites/task_store.py`、`tests/contract/test_lease_decisions.py`、新增 `tests/contract/test_dispatch_and_attempts.py`、新增 `tests/integration/test_dispatch_and_attempts_postgres.py`、`tests/integration/test_concurrency_and_recovery.py`、`tests/security/test_lease_fencing.py`、`tests/contract/test_protocol_conformance.py`（哨兵 8 → 11，同提交更新注释说明加了哪三个）。

红灯矩阵：过滤发生在 LIMIT 前；scope 隔离；`AWAITING_APPROVAL` / 终态 / live lease / future retry 排除；并发只一个 grant；token 单调；`attempt_number` 只在成功 grant 增加；`task_failure_count` 只在 `schedule_retry` 增加；retry delay 小于/等于/大于 TTL；多个 worker 只一个写出 `retry_exhausted` FAILED winner。

`TaskAttemptResult` 携带 submission/内部终态审计的专项红灯（两实现各跑一遍）：三条存在性 XOR 各有一个构造非法值的反例；返回的 `submission` 与 `create_task` 时写入的**逐字段相等**（含原始 aware `as_of`）；**每一种 `TaskAttemptRejection` 都不携带 submission**；只有 `RETRY_EXHAUSTED` / `SUBMISSION_INVARIANT_VIOLATION` 返回非空 `committed_audit_events`，且逐字段等于同一事务实际写入的行，其余 rejection 与成功 grant 全为空；Worker 对返回事件只调用 sink 的 `COMMAND_COMMITTED` 日志腿。§2.3 三项校验各有一条人为损坏的存储行反例（篡改 `submission_digest` / 篡改 `request_digest` / 破坏 scope 关系），每条都必须在锁内原子写成 `FAILED` + `submission_invariant_violation` 且**不发放 grant**；反证：把 submission 读取从 `begin_task_attempt` 的事务里挪出去（改成领取后第二次查询），并发用例必须转红。

`AttemptIntent` 的专项红灯（§2.5）：`DISPATCH` 领 `AWAITING_APPROVAL` → `NOT_DISPATCHABLE`；`APPROVAL_RESUME` 领 `RUNNING` / `CREATED` / `PLANNING` → 同样 `NOT_DISPATCHABLE`（两个集合不相交）；`APPROVAL_RESUME` 领一个**租约已过期**的 `AWAITING_APPROVAL` 任务 → 成功发放 grant 且 token 严格递增（这是 M4 审批恢复契约在 M5 唯一的合法入口，缺这条它就是死路）；`list_dispatchable_tasks` 的结果集**永不**包含 `AWAITING_APPROVAL`。六种 rejection 的 winner 形状逐行断言（§2.5 那张表），其中 `RETRY_EXHAUSTED` / `SUBMISSION_INVARIANT_VIOLATION` 两行必须断言返回的是**本次刚写入**的行（`version` 已 +1、`terminal_reason` 已是闭集码）。

`schedule_retry` 的专项红灯（§4.4）：同一命令连发两次 → 第二次 `ALREADY_SCHEDULED` 且 `task_failure_count` 只 +1、token 只推进一次；改一个字段再发 → `COMMAND_MISMATCH` 且**无任何写入**（断言 `version` 不变）；更晚的 attempt 重放旧命令 → `STALE_FENCING`；调度后旧 grant 立即失效（用旧 grant 提交步骤 / 终态化必须被拒）；`RetryClass` 不存在（一条断言 `contracts` 里没有这个名字，防止它被"顺手补回来"）。

再改：`persistence/decisions.py`、`persistence/schema.py`、新增 `persistence/migrations/versions/rev_0004_retry_markers.py`、`persistence/fake.py`、`persistence/postgres.py`、`persistence/store.py`（含模块 docstring 第 3 条的 token 推进点更新，§4.4；以及 `task_failure_limit` 作为构造参数注入，§2.4）。

dispatch 谓词、排序键与 retry 判定必须是 `decisions.py` 的纯函数，内存与 PostgreSQL 逐字共用；SQL 侧完成过滤排序后，Python 侧仍调用同一函数做 fail-closed 兜底（与 `list_stale_leases` 现有形状一致）。

### Task 4：step journal + 状态改动的同事务审计（11 → 14 方法）

先改/新增：`tests/suites/task_store.py`、新增 `tests/contract/test_step_execution_store.py`、新增 `tests/integration/test_step_execution_postgres.py`、`tests/contract/test_evidence_ledger.py`、`tests/security/test_audit_ledger_guards.py`、`tests/contract/test_protocol_conformance.py`（哨兵 11 → 14）。

本任务同时完成 `transition` 的签名收窄（散开关键字参数 → `TransitionCommand`，§4.1.1(b)）与 `list_stale_leases` 收 `StaleLeaseQuery`；两者都改调用形状而不改语义，与 step journal 同提交是因为"状态改动带审计"必须在同一次里变成可调用的。

红灯断言：**§4.2.1 的两张真值表逐行一条**（begin 8 行、commit 6 行），缺一行即视为该契约未被证明可执行；其中必须包含 **无 step row 的 `STALE_FENCING`** 与 **无 step row 的 `NOT_RUNNABLE`**（`record` 为 `None`）、**满预算下的同 token 重入仍 `PROCEED` 且随后 commit 成功**、**满预算下的新 grant 接管为 `BUDGET_EXHAUSTED`**（证明预算只约束会 +1 的那两行）、以及 `record` 无条件必填的反证（改回必填后前两条用例必须变成无法构造）。

**§4.2.2 `grant_is_current` 的六条反例**（begin 与 commit 各跑一遍，两实现各跑一遍）：任务在 `CREATED`；任务在 `PLANNING`；租约**恰好过期**（`lease_expires_at == now`）；owner 不同；旧 token；**未来 token**（大于行上的值）。外加一条 `allowed_statuses` 差异用例：`PLANNING` 下 `schedule_retry` 成功且 `task_failure_count` +1，同状态下 `begin_step_attempt` 为 `NOT_RUNNABLE`。每条都断言 step 行、evidence 行、audit 行三者的计数在调用前后**完全不变**——只断言"返回了拒绝"不够，一个先写后判的实现照样返回拒绝。**反证**：把判据换成只比 token 大小，"恰好过期"与两条状态用例必须转红。

**§4.2.1 提交形状反例**：直接参数化复用 `STEP_COMMIT_INVALID_CASES`，不在本 Task 复制子集或维护第二个数量；正例单独证明 malformed 终局可构造。

还有：形状 A 三写全有或全无、形状 B 两写全有或全无且 Evidence 必空；旧 token 不能提交；同 token 幂等 commit 不重复 Evidence；`step_id` 不在 stored plan 里 → `UNKNOWN_STEP`；调用方无法自报预算（命令里根本没有该字段）；`load_step_executions` 返回行数**小于**计划步骤数是合法的（"缺行 ≠ 数据损坏"）；`SKIPPED` 不被写出。

同事务审计的专项红灯（§5.1）：`TransitionCommand.audit_events` 默认空时 M4 既有行为逐字不变；带事件时状态与审计同生共死——**注入 `task_audit_events` 的 insert 故障，断言 `tasks` 行的 `status` 与 `version` 都没有变化**；两个来源（`record_audit_event` 与命令内审计）并发写同一 task，seq 连续无缺口无重复（证明都取了 `_serialise_on_task`）；一条回归用例显式跑 M4 的 `test_audit_seq_is_scoped_per_task`（同 task、同默认 `event_id` 写两次、seq 递增到 2），证明 append-only 语义未被 M5 改动。

再改：`persistence/store.py`、`persistence/schema.py`、新增 `persistence/migrations/versions/rev_0005_step_journal.py`（含 `kind` / `commit_digest` 两列与 step journal 的降级守卫）、`contracts/evidence.py`（接收下沉的 `evidence_id` 纯函数，§4.2.1）、`evidence/builder.py`（改为 re-export）、`persistence/fake.py`、`persistence/postgres.py`、`persistence/evidence.py`（只抽共享 append helper，不改变公共 append 契约）。

### Task 5：Runner grant / heartbeat / resume / loser + gateway 专用异常

**`MalformedAdapterResponseError` 与捕获它的 Runner 必须同一 Task**：只改 gateway 会让现有 `except TypeError` 立刻漏掉这条路径，只改 Runner 则没有异常可捕。

先改：`tests/contract/test_deterministic_runner.py`、`tests/contract/test_runner_contract.py`、`tests/security/test_gateway_boundary.py`（`main` 上断言 `TypeError` 的那条用例改为断言新异常）、`tests/security/test_runner_lifecycle.py`、`tests/security/test_target_drift.py`、`tests/security/test_workflow_policy.py`、`tests/integration/test_concurrency_and_recovery.py`、`tests/fakes/runner.py`。

红灯断言：Runner 永不 acquire；只 renew 同一个 token；grant 校验失败不执行 tool；heartbeat 丢失取消执行；`WorkflowPaused` 不进入 retry；resume 重解析 policy/target/plan binding；loser 不提交任何后到结果；gateway 对非 `AdapterResponse` 抛 `MalformedAdapterResponseError` 且消息不含类型名；**普通内部 `TypeError` 不再被吞成 malformed 步骤**（反证）。

同一 Task 参数化执行 §4.2.1 的 Runner 消费闭表：begin/commit 的每个 decision/rejection 都断言 Gateway、终态迁移、Evidence、audit 的精确调用次数；`PROCEED` 是唯一 Gateway 分支；`ALREADY_COMMITTED` 从 record 重建 `failed_steps/degraded` 而不重跑；预算耗尽与未知 step 分别落 `budget.tool_calls_exhausted` / `recovery_drift`；两个 commit 不变量破坏分支 fail-stop 且保留首次事实。该测试直接消费存储真值表的 case id，禁止另建一份手写枚举。

§2.6 恢复分派闭集的专项红灯（每个崩溃点一条，不允许合并）：`CREATED` 无 plan → `start()` 全程合法；`CREATED` 有 plan → `PlanStore.save` 幂等命中；`PLANNING` → `resume()` 只推进一条边，**反证**：同状态改用 `start()` 必须以 `ILLEGAL_TRANSITION` 失败；`RUNNING` → `resume()` 不做状态推进；`AWAITING_APPROVAL` → `NOT_DISPATCHABLE`，Runner 根本不被调用；终态 → `TERMINAL_PROTECTED`。

跨重启重建三份进程内状态的专项红灯（§2.6）。构造：两步计划，s1 已提交 `FAILED`、s2 的条件是 `EVIDENCE_ROW_COUNT_BELOW` 引用 s1；杀掉 Worker、租约过期后由新 Worker 恢复。

- **断言 s2 仍然不执行**、Gateway 调用次数为 0；
- **断言恢复后的最终状态恰为 `INDETERMINATE`**，不是只断言"s2 没跑"——这是 `degraded` 重建的唯一承重点，缺了它，一个只重建 `failed_steps` 的实现照样全绿；
- **失败上下文反证**：同时删掉快照预加载与 `ALREADY_COMMITTED` 采纳中的 `failed_steps` 恢复 → s2 会执行，Gateway 调用次数断言转红；两处是对同一不可变 journal 的等价防线，单删一处后行为仍正确，不要求测试锁住私有实现位置；
- **降级状态反证**：同时删掉快照预加载与 `ALREADY_COMMITTED` 采纳中的 `degraded` 恢复 → s2 仍不执行、Gateway 仍为 0，但**最终状态变成 `SUCCEEDED`**，上一条断言转红；同理只对两条等价防线同时退化承重；
- **工具预算反证**：在 Runner 里保留一个进程内 `tool_calls_used` 并用它做预算判定 → 构造"重启前已用满预算"的场景，重启后该步骤必须仍被 `BUDGET_EXHAUSTED` 拒绝；保留局部计数器时它会被放行；
- 已 `OK` 提交的步骤恢复后被判 `ALREADY_COMMITTED`；未执行步骤的条件重新求值与首次结果一致；`start()` 在 `load_step_executions` 非空时按不变量破坏失败。

`ToolCallStatus → StepResultStatus` 闭表逐行一条，含"超时落成 `TIMEOUT` 而非 `FAILED`"。

再改：`tools/gateway.py`（异常与 docstring）、`runners/runner.py`、`runners/deterministic.py`、`runners/fake.py`、`_conformance.py`。heartbeat 测试注入 async sleep，不依赖真实 sleep。

### Task 6：async TraceSink、`Delivery` 与 durable sink

先改：`tests/unit/test_trace.py`、`tests/contract/test_trace_stages.py`、`tests/fakes/sinks.py`、`tests/security/test_trace_event_redaction.py`、`tests/security/test_audit_ledger_guards.py`。

红灯断言：每个 emit 被 await；写失败 fail-closed；UUID 唯一；M5 execution event 有 `attempt_number`；detail 不含外部文本；AST 证明 Runner 不调用 `ledger.append`。

§5.1 的专项红灯：`Delivery` 五个成员的路由各一条；**sink 入口的运行期 guard 对四个非 `LOG_ONLY` 成员 + `task_id is None` 各抛一次**（判据写成"不是 `LOG_ONLY`"，因此新增成员默认受约束）；`delivery_state` 四个取值各一条，且**每条事件恰好一条日志记录**；明确回滚时该批事件全为 `failed`、`committed` 为 0；故障注入“数据库已应用写入但连接丢失”时为 `not_confirmed`、绝不为 `failed`，同命令重放/恢复后 Evidence/audit 不重复；**`LOG_AND_DURABLE` 的 `NOT_CONFIRMED` 事件不重放原 `event_id`**，spy 断言 `AuditEventWriter` 对该 id 恰好调用一次，后续任务尝试使用新事件与新 attempt 归属；`task_id is None` 的事件经 durable sink 时 `AuditEventWriter` 调用次数为 0；被策略拒绝的步骤仍经 sink 持久化；**多重集断言**：`task_audit_events.event_id` = 「sink durable 写入」⊎「随命令提交」⊎「`begin_task_attempt` 内部提交并随结果返回」，三方两两不相交、无重复；内部终态事件由 Worker 以 `COMMAND_COMMITTED` 补日志。两条既有路由反证继续保留；`ToolResult.error` 可从 audit 重建；loser 事件仍带自己的 attempt；logging handler 异常不得被误算成审计失败。

再改：`observability/sink.py`、`observability/log_sink.py`、新增 `observability/durable_sink.py`、`trace.py`、以及 Runtime/Runner 的全部调用点。

### Task 7：Runtime submit/execute/query、`_finish` 的 fencing 修复与闭集异常投影

先改/新增：`tests/contract/test_runtime_facade.py`、新增 `tests/contract/test_runtime_async_lifecycle.py`、`tests/security/test_runtime_bypass.py`、`tests/security/test_vacuous_check_ban.py`、`tests/security/test_reject_path_hygiene.py`、`tests/security/test_error_input_leakage.py`。

红灯断言：`handle()` 与拆分接口共享同一终态投影；异步 submit 不执行；Worker execute 使用持久化 `as_of`；**§3.5 异常闭表逐行一条，且每行断言调用次数**（确定性拒绝只尝试一次；`PermissionError` / `SpecResolutionError` → `REJECTED`；`DriftError` 与 `PlanConflictError` → `FAILED` + `recovery_drift` 且 Gateway 0 次；gateway `LookupError` → 进程 fail-stop 且 status/version/failure/audit/evidence 五样不变；`PersistenceIntegrityError`（含 `IntegrityError` 与非连接失效的 SQLAlchemy 尾部）→ 任务不改、进程非零退出、退避 0 次；`DisconnectionError` / `DBAPIError(connection_invalidated=True)` / 其它 `PersistenceUnavailableError` → 非终态且失败预算不变；写路径还要按 `PersistenceWriteOutcome` 区分 confirmed rollback 与 not-confirmed，不得从异常类猜；`RetryableTaskError` → Worker 调 `schedule_retry` 且异常不含原文；读路径故障三样行数不变）；scope query 不泄漏他人 task；`AWAITING_APPROVAL` 的 `query_task` 返回 `render=null`。

**`_finish` fencing 绕过的两条反证**：行为用例（旧 Worker 停在最终 CAS 前 → 新 Worker 接管并终态化 → 旧 Worker 恢复后必须失败且不改变 winner）；AST 用例（扩展 `test_vacuous_check_ban.py`，禁止 `_finish` 里出现"从刚读回的行取 token"的属性链）。

`handle()` 提交顺序承重的反证（§3.4）：删除 `handle()` 中 `create_task` 之前的任一确定性拒绝分支，`tests/fakes/runtime.py::RuntimeHarness.run_injection` 覆盖的注入用例必须转红；断言 `handle()` 与 `query_task()` 调用**同一个**终态投影函数；断言 `handle()` 的确定性拒绝路径**不创建任何 TaskRecord**。

再改：`application/runtime.py`、`tests/fakes/runtime.py`。

**不创建 `application/service.py`。** 判据是现成的：`submit_task` / `execute_task` / `query_task` 三者都只编排既有 port，依赖面与 `handle()` 完全重合，`test_module_layering.py` 的 `application` 白名单已覆盖它们需要的每一个包，不存在循环依赖。因此直接扩展 `XiaoweiRuntime`。若实施中确实无法在单文件内保持职责，那是**计划失效**，应停下来改计划并补 ADR，而不是就地新增文件。

### Task 8：Worker、LocalStack、fake 隔离与模块层级

先新增/改：新增 `tests/contract/test_worker_loop.py`、新增 `tests/unit/test_local_stack.py`、`tests/security/test_fake_isolation.py`、`tests/security/test_module_layering.py`。

红灯断言：Worker 显式接收 `runtime` + `task_store` + `clock` + `monotonic` + `settings` + `sleep`（构造时缺任一个即 `TypeError`，不是运行时才炸）；进程 owner 每次不同；unavailable / systemic / task-failure（`RetryableTaskError`）/ paused / loser 各进唯一桶（§4.7 的**两**分类逐条，含「系统性故障时任务一个字不改且进程非零退出」；`RetryableTaskError` 则携原 grant 调 `schedule_retry`）；infra 退避 1–30 秒且连续 15 分钟后非零退出；**故障窗口用注入的 monotonic 计量**（同时推进墙钟与 monotonic，断言墙钟跳变不影响窗口——只有 monotonic 可注入时这条才写得出来）；**只有整轮 poll iteration 无 infra 故障才重置窗口**，并有「`list` 恒成功 + `begin` 恒抛 → 窗口耗尽后非零退出」的反例（§3.3）；SIGTERM 不调用 `schedule_retry`、不写终态、`task_failure_count` 不变、**且调用了 `aclose`**；`local_stack` 装配失败时已建成的 Engine 被 dispose（注入一个在装配后半段抛错的依赖）；默认配置下装配出的 gateway **不是** barrier wrapper（类型断言）。

fake 与分层的红灯：fake 只由 `local_stack.py` 一个**文件**放行；`starrocks_fake` 与 `starrocks_recording` 都不能逃过扫描；`src/` 不 import `tests.fakes.recordings`；`_ALLOWED_INTERNAL_BY_FILE` 的 key 集合与磁盘 `interfaces/*.py` 精确相等，且只有 `local_stack.py` 拥有宽依赖/fake 权限；`tools/starrocks_recording.py` 不 import `capabilities`，operation 常量由 `local_stack.py` 从 capability 真源取得。三条变异分别给 API 加 persistence、CLI 加 application、tools 加 capabilities import，必须转红（§5.4）。

再新增/改：`application/worker.py`、`interfaces/__init__.py`、`interfaces/local_stack.py`、`interfaces/worker.py`、新增 `src/xiaowei_agent/tools/starrocks_recording.py`（`IS_FAKE=True`，登记进 `_FAKE_MODULES`）、以及两个安全测试的允许清单、文件级覆盖表与检测器。

### Task 9：FastAPI 薄入口与 pre-Pydantic body limit

先新增：`tests/contract/test_api_contract.py`、`tests/security/test_api_context_spoofing.py`、`tests/security/test_api_input_and_redaction.py`、`tests/security/test_api_body_limit.py`。

测试使用 `httpx.ASGITransport`，不开放真实 socket。红灯矩阵：合法 202/200；tenant/env/actor/request_id/trace_id/channel/policy_revision 伪造；extra field；恶意 Unicode/超长文本；无 Content-Length、虚假小 Content-Length、chunked/streaming body 超限；跨 scope task id 统一 404；**八种状态码逐条比对完整 JSON 形状**；405 与未捕获 500 走闭集体而非框架默认体；**`/v1/tasks/`、`/healthz/`、`/readyz/` 在 `follow_redirects=False` 下不得返回 3xx**（trailing-slash 307 是框架直接产出的、不经异常链的第九种响应，必须关掉而不是纳入协议），并有"打开 `redirect_slashes` 即转红"的反证；`application/json; charset=utf-8` 接受、`application/jsonx` 与缺失 Content-Type 为 415；**`GET` 纯读：spy 断言 Resolver / Planner / Gateway 调用次数为 0，并同时比对调用前后的 `version`、该任务的 audit 行数、evidence 行数三样**（只比 `version` 正是 §2.8 那条缺陷能一路绿到最后的原因）；`TaskView` 的三条关联不变量（`render.status` 一致、`query_path` 由 `quote(task_id, safe="")` 重算比对、含 `/ ? # %` 的 task_id 各一条）；并发请求 trace 不串线、异常后 trace 已 reset；日志 `extra` 的键集恰好等于 §5.1 那张表。

再新增：`interfaces/api.py`、`interfaces/http_models.py`、`interfaces/body_limit.py`、`interfaces/auth.py`。

### Task 10：标准库 CLI

先新增：`tests/contract/test_cli_contract.py`、`tests/security/test_cli_redaction.py`。

红灯断言：submit/get 请求形状与 API 契约一致；无身份/trace override；**八个退出码逐条**（§3.2 表；上一版此处写"七个"是 §3.2 改成全函数后漏改的计数）；连接被拒 → 6；读超时 → 7；响应体恰好上限与超一字节；非 JSON Content-Type 与 JSON 解析失败 → 2；**30x 不被跟随** → 2；`--base-url file:///...` 被拒；带 userinfo 的 `--base-url` 被拒；非终态 `GET` 返回 0；transport spy 直接断言 `task get X` 的 outgoing URL 使用 `quote(X, safe="")`；含 `/ ? # %` 的伪造 id 不要求成功，只要求保持编码并对真实 ASGI fail-closed 为闭集 404；**响应体 `task_id != X` → 退出 2**（结构完全合法但取错任务的响应，`extra="forbid"` 挡不住，见 §2.8）；伪造 secret-shaped 响应不进 stderr；服务端返回的 ANSI/控制字符在打印前被转义；注入 opener 时无 socket。

再新增/改：`interfaces/cli.py`、`pyproject.toml` console script entry。

### Task 11：配置、依赖、镜像与 Compose

先改/新增：`tests/unit/test_config_happy.py`、`tests/contract/test_config_encoding.py`、`tests/security/test_config_fail_fast.py`、`tests/security/test_env_example_clean.py`、`tests/security/test_dependency_baseline.py`、新增 `tests/contract/test_compose_contract.py`、新增 `tests/contract/test_wheel_runs_without_tests.py`。

红灯断言：Settings/env example 精确映射；DSN 单一构造；password file 不泄漏；§4.3 与 §4.7 两张表的全部默认值与上下界（含 **pool overflow 默认 0 且约束为 `>= 0`**）；依赖集合精确（direct runtime 由 5 变 7，`httpx` / `PyYAML` 只在 dev，`fastapi` / `uvicorn` 不在 dev，`PyYAML` 不在 runtime）；compose 静态测试用 `yaml.safe_load`，不写正则/手工 YAML parser；`docker-compose.yml` 独立通过 §6.3 的完整断言，smoke/barrier override 各自通过精确路径白名单；有 Docker 时，`base+smoke` 与 `base+smoke+barrier` 的 `docker compose config` 合并结果再过同一完整断言；五条改坏基线文件与两条给 override 增加越权节点的反例必须转红；secret 只 file reference 且缺文件时 fail-fast；migration gate；四个 revision 的 `revision` / `down_revision` 八个取值逐条断言且 `heads` 恰好一个。

`.env.example` 的专项红灯：`test_env_example_clean.py` 的 `_SECRET_SHAPES` 第一条正则就是 `scheme://user:pass@`，因此**任何内联凭证形态的 DSN 都会被判为"形似真实凭证"**。这不是要绕过的障碍，而是配置形状的判据：Settings 只接受 host/port/database/user 与 password-file 路径，`.env.example` 里不出现任何拼好的 DSN。同文件 `test_documents_only_known_variables` 还要求 example 的键 ⊆ `_FIELD_TO_ENV.values()`，所以每个新变量都要三处同步。

迁移入口的专项红灯：`interfaces/migrate.py` 构造连接时凭证不出现在任何 `argv`、环境变量或日志；`alembic.ini` 与 `migrations/env.py` 的文件摘要在本任务前后不变；失败路径只输出字段名与错误类别；降到 `0001_initial` 前的随机名 SAVEPOINT index probe 早于 `command.downgrade`；兼容数据可通过，超大但 M5 合法 scope 在默认/强制两种模式都返回 `M4_DOWNGRADE_INCOMPATIBLE`，且 schema、revision 与全部相关行逐项不变。

**wheel 无 `tests/` 的专项红灯**：`uv build` 产出 wheel → 装进干净 venv（无仓库源码、无 `tests/`）。这条不依赖 Docker，因此本机可跑，但**必须把它证明的东西说准**——干净 venv 里没有 PostgreSQL fixture，而生产 `LocalStack` 装配的是 `PostgresTaskStore`，所以"跑一次真实 submit→执行→终态"在那里根本无法运行。上一版就是这么写的，照字面实施会得到一条永远起不来的用例，然后被人改成 skip。

拆成两条，各自可执行：

1. **打包/装配证明（干净 venv，无 Docker）**：`import xiaowei_agent.interfaces.local_stack` 成功；`tools.starrocks_recording` 存在于 wheel 内且映射非空（这正是 `StarRocksRecordingAdapter` 空映射直接 `ValueError` 那条的正面证明）；用**内存 store**装配一次并跑完一个任务，断言拿到终态与证据。它证明的是"包内容完整、装配路径不依赖 `tests/`"。
2. **真实存储证明**：submit→执行→终态的 PostgreSQL 版本留在 integration（有 `PYTEST_POSTGRES_DSN` fixture），容器内的版本由 Compose smoke 第 21 步的真实 console script 承担。

两条合起来才等于上一版那句话想要的东西；分开写的好处是每一条都能真的跑。

`Settings` 本任务新增的字段除 §4.3 / §4.7 两张表的全部阈值外，还有 `actor`（§5.3）与 `smoke_step_barrier`（默认 `False`，§5.4），三处同步照旧。`ReadinessProbe` Protocol 与 `ReadinessReport` 落在 `contracts/`，具体实现落在 `persistence/`；`/readyz` 的接线在本任务完成，API 侧只消费 report 的三个 bool（§3.3）。

再改/新增：`config.py`、`.env.example`、`pyproject.toml`、`uv.lock`、`Dockerfile`、`.dockerignore`、`.gitignore`、`docker-compose.yml`、新增 `docker-compose.smoke.yml`（短 lease TTL / heartbeat / poll interval）、新增 `docker-compose.barrier.yml`（**只**放 `XIAOWEI_SMOKE_STEP_BARRIER=true` 这一个环境变量）、`interfaces/migrate.py`（含 `--allow-destructive` 标志，§4.5）、`persistence/migrations/runner.py`（`_run_downgrade` 经 `config.attributes` 传参）、`tests/integration/conftest.py`（三个引用点改为 import 共享实现）。`alembic.ini` 与 `migrations/env.py` **不改**。

### Task 12：Compose smoke、CI 与开发文档

先新增：`tests/contract/test_compose_smoke_script.py`；更新 CI shape/security 契约测试。再新增/改：`scripts/compose_smoke.py`、`.github/workflows/ci.yml`、`README.md`、`ARCHITECTURE.md`、`AGENT_HANDOFF.md`。

smoke script 的通用要求：用 `shutil.which("docker")` 检查 Docker，缺失即 hard fail、不 skip；所有 subprocess 使用 argv list、`shell=False`、`check=True`；**每一次等待都有显式超时上限，超时即硬失败**；不在 smoke job 内再次运行 pytest。

**project name 必须由脚本生成并做碰撞预检——这是一条会删掉用户数据的路径。** 上一版让 `--project-name` 由调用方自由指定，然后在 `finally` 里无条件 `down --volumes --remove-orphans`。这两条放在一起意味着：只要传入的名字与用户机器上某个**已存在的** Compose 项目重名（一次手滑、一次从文档复制、CI 与本地用了同一个默认值），脚本会在退出时删掉那个项目的容器、网络和 **named volume**——而 volume 里通常正是那个项目唯一不可重建的东西。这不是"测试环境噪音"，是不可逆的数据丢失，且发生在一条以 `finally` 保证一定会执行的路径上。

因此三条一起做，缺一条这个风险就还在：

1. **名字由脚本生成**：`xiaowei_m5_smoke_<uuid4().hex>`（128 位）。上一版写 8 位 hex，那只有 32 位熵；对一个会执行 `down --volumes` 的标识符，把碰撞概率压到可忽略是最省事的一道保险。`--project-name` 参数取消；若确需可读性，只保留 `--project-prefix` 且随机后缀不可省。
2. **启动前碰撞预检，发现任何同名残留即拒绝执行**：查询该 project label 下的容器、网络与 volume（`docker ps -aq --filter label=com.docker.compose.project=<name>`、`docker network ls -q --filter label=...`、`docker volume ls -q --filter label=...`），三者任一非空即**立刻硬失败并原样退出**——不清理、不覆盖、不"顺手接管"。
3. **只清理本次确认创建的资源**：`finally` 里的 `down --volumes` 仅在第 2 步预检通过**且**脚本确实执行过 `up` 之后才运行；预检失败或 `up` 从未开始时，`finally` **不发出任何变更型命令**（预检本身就是三条 docker 查询，所以"一个 docker 命令都不发"是说不通的；准确的说法是不发 `up` / `down` / `rm` 这类会改变状态的命令）。

**保证要说到位，不能说过头。** 预检与 `up` 之间存在 TOCTOU 窗口——理论上有人能在这两步之间创建同名项目。128 位随机名让这件事不可能被意外触发，但它不是互斥锁。因此这条机制的准确表述是：**不清理预检发现的既有项目；只清理本次已尝试创建的随机项目**。它不声称"绝不与任何既有项目冲突"。

承重用例（在 `tests/contract/test_compose_smoke_script.py` 里用注入的 runner 记录 argv，不需要真 Docker）：预先让预检查询返回非空 → 脚本必须以非零退出，且**记录到的 argv 序列里没有任何 `up` / `down` / `rm`**；预检通过但 `up` 抛错 → `finally` 仍执行 `down --volumes`（证明正常清理没被削弱）；生成的 project name 两次调用不相同且长度对应 128 位。

**中断点：barrier 在 local-stack 的 Gateway wrapper 里，不在 adapter 里。** `DeterministicToolGateway.invoke` 的形状是 `await asyncio.wait_for(adapter.execute(...), call.timeout_seconds)`——adapter 一旦阻塞就**还没有返回**，测的其实是"调用已发出、结果未返回"这个**另一个**中断点；更糟的是它会在 `timeout_seconds` 到点后自己变成 `ToolCallStatus.TIMEOUT`，于是这条用例悄悄改成了在测超时路径。

正确位置是 `interfaces/local_stack.py` 里一个**只在 barrier 开关打开时才装配**的 Gateway wrapper：它先 `await` 真正的 fake Gateway 拿到 `ToolResult`，**然后**在把结果交回 Runner 之前阻塞。此时 `ToolResult` 已存在、`wait_for` 已结束（不会被超时打断）、`commit_step_result` 尚未发生——正是计划宣称的那个点。`interfaces/` 不受 `test_runtime_bypass.py` 对 `application/**` 的 `invoke` 禁令约束，fake 隔离约束照旧适用。配套断言：marker 出现时能证明 `ToolResult` 已存在且该步骤尚无 committed 行；**等待时长超过 adapter timeout 也不得让该步骤变成 `TIMEOUT`**（反证 barrier 确实在 `wait_for` 之外）。

**中断点可观测。** wrapper 进入 barrier 时向 stdout 打印一个固定常量行（不含任何外部文本或参数）并 **`flush=True`**——容器内 Python 的 stdout 在非 tty 下是块缓冲的，不 flush 会让 marker 迟迟不出现在 `docker compose logs`，表现为一次随机超时假失败。smoke 轮询 `docker compose logs worker` 直到看见它。

**barrier 开关必须走正式配置面，不能用无前缀环境变量偷偷改 Worker 行为。** 上一版为了绕开 `load_settings()` 对未知 `XIAOWEI_*` 变量的 fail-fast，让它用无前缀名——那等于承认"这个开关不该出现在配置面"，然后用命名把它藏起来。一个**未登记、却能改变正常 Worker 执行路径**的变量，正是 `load_settings` 的 fail-fast 要消灭的东西；藏得越好，越没人知道生产容器里它是什么值。

改为一个正常的 `Settings` 字段 `smoke_step_barrier: bool = False`，三处同步（`Settings` 字段、`_FIELD_TO_ENV` 的 `XIAOWEI_SMOKE_STEP_BARRIER`、`.env.example` 并注明"仅供 Compose smoke，生产必须为 false"）。它因此获得类型校验、fail-fast、以及"被人看得见"这三样。`interfaces/local_stack.py` 在且仅在该字段为真时装配 barrier wrapper——**装配决定在组装根，不在 Worker 或 Runner 里**，热路径上没有任何 `if barrier` 分支。

对价是生产配置面上多了一个测试用途的字段。用两条用例把它钉住：默认值为 `False`，且缺省环境下 `local_stack` 装配出的 gateway **不是** wrapper（用类型断言，不是"看起来没生效"）；以及 `.env.example` 里该行带着 smoke-only 注释（`test_env_example_clean.py` 的键集合断言本来就会强制三处同步）。

完整命令序列共 **23 步**（每一步都有超时；`$P` 为脚本生成并已通过碰撞预检的 project name，`$BASE` = `-f docker-compose.yml -f docker-compose.smoke.yml`）：

```text
 1. 创建临时 secret 文件（父目录 `0700`；文件写入时 `0600`、关闭后只读 `0444`；路径在 .gitignore 覆盖范围内）
 2. docker compose -p $P $BASE build
 3. docker compose -p $P $BASE up -d --wait postgres            # 或按 §6.1 的实机结论
 4. docker compose -p $P $BASE up migrate ；断言 ExitCode == 0
 5. docker compose -p $P $BASE up -d --wait api ；随后轮询 /readyz 直到 200（超时 60s）
       ——healthcheck 只看 /healthz（进程存活）；/readyz 才验证 DB ping 与 migration head（§6.4），
         而验收输出要求的"API ready"指的是后者，必须显式轮询，不能拿 --wait 的结果代替
    ——【基线】不带 barrier 启动 worker：
 6. docker compose -p $P $BASE up -d --no-deps worker
 7. CLI submit 任务 A → 轮询至终态（超时 120s）→ 记录基线证据（见下方规范化规则）
 8. docker compose -p $P $BASE stop worker
    ——【中断】带 barrier 重建 worker：
 9. docker compose -p $P $BASE -f docker-compose.barrier.yml up -d --force-recreate --no-deps worker
10. docker inspect 该容器，断言 XIAOWEI_SMOKE_STEP_BARRIER=true **存在**
11. CLI submit 任务 B → 轮询 logs worker 直到出现 marker 常量行（超时 60s）
12. psql probe 断言：任务 B 的该 step 有 in-flight 行（result_status IS NULL）且无 evidence 行
13. docker compose -p $P $BASE kill worker
    ——【恢复】不带 barrier 重建 worker：
14. docker compose -p $P $BASE up -d --force-recreate --no-deps worker
15. docker inspect 该容器，断言 XIAOWEI_SMOKE_STEP_BARRIER **不存在或为 false**
16. 轮询任务 B 至终态（超时 180s，需覆盖租约到期）
17. 断言：证据与基线**等值**（规范化后）、该 step 的 attempt_count == 2、attempt_number 严格递增、终态唯一
    ——【并发与幂等】
18. docker compose -p $P $BASE up -d --scale worker=2；`ps --quiet worker` 断言恰有两个不同的运行容器
19. 重复提交任务 A 的幂等键 → 返回同一 task_id
20. 并发提交多个任务 → 断言每个任务只有一份 step/evidence
21. docker compose -p $P $BASE exec api xiaowei task submit ... / task get ...  # 容器内真实 console script，断言真实退出码
22. docker compose -p $P $BASE ps -a ；logs --no-color migrate api worker postgres
23. finally: docker compose -p $P $BASE down --volumes --remove-orphans
```

第 9 / 14 步必须是**显式重建**：`docker compose restart worker` 会原样重启同一个容器（连同它的 barrier 开关），`up -d worker` 在某些情况下也可能判定"无需变更"而复用它。第 10 / 15 步的 `docker inspect` 断言是这条机制的反证——没有它，一次静默的容器复用会让第 16 步永远超时，而原因看起来像"恢复不工作"。**须实机确认（3）**：`--force-recreate --no-deps` 在带/不带额外 `-f` override 时确实产生新容器且环境已更新。

**证据比较用规范化后的内容，不比标识符。** `evidence_id` 由 `evidence/builder.py::evidence_id` 确定性地构造为 `f"{task_id}:{step_id}"`，而两次运行是两个不同的任务，`task_id` 必然不同——"逐个相等"恒假。想让两次运行共用 task_id 也不成立：复用同一个 idempotency key 只会取回**已终态**的第一个任务。因此可比的是"结构与内容"：条数、step 后缀集合、以及**剥掉时间相关字段后**的 facts。必须剥掉的至少有 `evidence_id` 的 task 前缀、`captured_at`、以及 `limitations` 里内嵌的窗口起止时刻（`build_evidence` 把精确到分钟的窗口写进 limitations，两次运行跨分钟就会不等，这会表现为一次随机失败）。

第 12 步的 psql probe 是**容器内只读**查询（`docker compose exec postgres psql -c "SELECT ..."`），只读 journal 与 evidence 的计数，不写任何东西、不打印任务文本。它是 smoke 唯一的存储观测通道；没有它，"中断点确实在 commit 之前"这句话无法断言。

CI 从 7 jobs 增为 8 jobs，Compose smoke 是独立 job；checkout 8 次、setup-uv 7 次、`uv sync` 7 次，全量 `python -m pytest -q` 的精确出现次数仍为 2。同步更新 workflow digest/policy allowlist；禁止 `if` 跳过 Docker、`continue-on-error` 或容错吞错。

### Task 13：受影响模块自审、反证与收口

自审顺序：(1) 从 API submit 沿 task/submission transaction 到 DB；(2) 从 dispatch 过滤沿 begin grant 到 Runner renew；(3) 从 StepAdmission 沿 fake Gateway 到原子 checkpoint/evidence/audit；(4) 从异常源沿分类、retry、lease handoff 到 winner；(5) 从 GET scope 到统一 projection；(6) 从 Settings 到 Compose secret mount/DSN；(7) 从 logs/trace/error response 反查外部文本和 secret 泄漏；(8) 从 Worker crash/restart 反查后到写与重复执行。

对关键保护做隔离变异反证：临时撤掉 dispatch-before-limit、fencing 校验（含 `_finish` 的 grant token）、attempt result 关联不变量、scope query、pre-Pydantic body limit、atomic step commit、状态与审计同事务、`_serialise_on_task`、fake import detector 中任一承重点，确认对应测试变红；恢复源码后重新跑相关门。变异不得提交，pycache 使用独立路径。

## 8. 集成与安全测试矩阵

| 风险/行为 | 单元/契约反例 | PostgreSQL/Compose 证据 |
| --- | --- | --- |
| API/CLI 与 Runtime 契约一致 | ASGITransport + injected opener；共享 DTO/projection | submit → Worker → GET 终态 |
| 身份伪造 | body/header 中 tenant/env/actor/trace/request/channel/policy 被拒或忽略 | 两 scope 任务互不可见 |
| body 绕过 | 无/虚假 Content-Length、流式超限均 413 | API 容器同样限流 |
| API 失败协议闭集 | 八种状态逐条比对 JSON；405 与未捕获 500 不走框架默认体 | 容器内 CLI 真实退出码 |
| CLI 传输面 | 30x 不跟随、非 http(s) scheme 拒绝、userinfo 拒绝、响应体截断式上限 | 容器内 `xiaowei task submit/get` 真实进程 |
| 外部文本污染 | tool error、日志、粘贴文本不能改变 plan/policy | fake 返回恶意文本只成为**原样的结构化事实**，且不进入 policy/plan/log/error（§5.2） |
| 终端转义 | 含 ANSI/控制字符的服务端字符串在 CLI 打印前被转义 | —— |
| 日志脱敏 | secret-shaped 输入用拆分字面量构造，stdout/stderr/TraceEvent 无原文 | Compose logs 扫描无伪 secret 原文 |
| 幂等 | 同键同语义同 task；同键异义冲突；重试保留首次 submission | 定长 digest 唯一约束；重启后仍相同 |
| 索引边界 | `idempotency_key` 200/201 字符；超长 environment_id 不再撞 B-tree 上限 | 真实 PostgreSQL 插入超长键成功且唯一性成立 |
| 并发 Worker | `TaskAttemptResult` 只有一个 grant | 两 Worker 同时 poll，仅一份 step/evidence |
| begin 内部终态审计 | 仅两种 mutating rejection 返回 `committed_audit_events`，其它 rejection/grant 为空；返回事件与落库行一致 | Worker 只补 `COMMAND_COMMITTED` 日志；三来源多重集无重复 |
| Worker 崩溃恢复 | begin 后取消；旧 token 后到提交被拒 | kill/restart，租约到期后高 token 恢复 |
| 恢复分派闭集 | `CREATED`（有/无 plan）、`PLANNING`、`RUNNING` 各一条；`PLANNING` 用 `start()` 必须非法 | **Compose 只覆盖 RUNNING 一个中断点**（barrier 所在处）；`CREATED` / `PLANNING` 两个崩溃点由 `test_concurrency_and_recovery.py` 在真实 PostgreSQL 上直接构造存储状态覆盖 |
| 跨重启失败上下文 | s1 失败后 s2 条件仍 fail-closed；删除重建逻辑必须转红 | 重启后 Gateway 调用次数不增加 |
| retry handoff | delay <、=、> TTL；同命令重放 `ALREADY_SCHEDULED` 只 +1；改字段 `COMMAND_MISMATCH` 无写入 | 实际领取时刻遵守 `next_attempt_at`；调度后旧 grant 立即失效 |
| task failure budget | 只有 `schedule_retry` 递增；paused/infra/整性错误都不增 | 第 3 次失败后下次 begin 原子 FAILED |
| 异常分类精确性 | `PermissionError` / `SpecResolutionError`→REJECTED；`DriftError`→FAILED+`recovery_drift`；gateway `LookupError`→**进程 fail-stop、任务不改**；`ProgrammingError` / `IntegrityError` / `InvalidRequestError`→**任务不改**+进程退出+退避次数 0；`OperationalError`→退避不计数。**每类断言调用次数** | DB 停/启后任务仍非终态且可恢复 |
| except 顺序与脱敏 | `DisconnectionError` 与 `DBAPIError(connection_invalidated=True)` 必须先映射 unavailable；普通 invalidated=False 保持系统性；把 `StatementError` 移到最前、删连接失效分支、删最终 `SQLAlchemyError` 兜底各有转红反证；含 secret 形状参数的语句故障，`str`/`repr`/日志/`__cause__`/**`__context__`** 均无原文 | —— |
| 读路径无副作用 | 读时注入任一持久化异常 → `version` 与 audit 行数不变 | `GET` 重复调用不改任何行 |
| retry 结果模型 | `RetryDecision` 单枚举；下一 attempt 开始后重放旧命令 → `STALE_FENCING`（不是 `ALREADY_SCHEDULED`） | 真实并发下的交接窗口 |
| 分层文件级允许表 | 表 key 与磁盘 `interfaces/*.py` 集合相等；只有 local_stack 宽；API→persistence、CLI→application、tools→capabilities 三条变异必须转红 | fake 白名单仍只放行 `interfaces/local_stack.py` |
| 状态表三处同提交 | `ALLOWED_TRANSITIONS` 新增两条边、`ScriptedRunner` 删检查、对应用例同一 Task；非终态拒绝用例保留 | —— |
| POST 薄调用链 | `submit_task -> TaskView`；AST 断言 POST 处理函数无 `TaskRecord` 字段访问 | 容器内 `202` 响应体形状 |
| 窗口重置粒度 | `list` 恒成功 + `begin` 恒抛 → 窗口耗尽后非零退出 | —— |
| 装配与生命周期 | 缺任一依赖即构造失败；装配中途失败会 dispose；SIGTERM 调用 `aclose`；默认配置不装 barrier wrapper | 容器停止后无残留连接 |
| smoke 资源归属 | 预检查询非空 → 非零退出且 argv 里**没有** `down`；预检通过但 `up` 抛错 → 仍清理；两次生成的 project name 不同 | 不触碰任何既有 project 的 volume |
| 迁移期审计例外 | 迁移终态化的任务 `terminal_reason` 属闭集码，且 audit 行数在迁移前后不变 | 升级/降级各一次 |
| 请求编码边界 | 8192 个非 BMP 字符 + `ensure_ascii=True` → 202；刚过 128 KiB → 413 且在 Pydantic 之前 | 容器内 CLI 提交同一请求成功 |
| CLI 状态全函数 | 八个码各一条 + 500/502/504/418/未映射 5xx；2xx 但响应体不合 `TaskView` → 退出 2 | 容器内真实退出码 |
| infra 故障窗口 | 1–30 秒退避；15 分钟退出；monotonic 计量；**只有整轮 poll iteration 无 infra 故障才重置** | DB 停/启后 Worker 不被误杀 |
| 优雅停止 | SIGTERM 不调 `schedule_retry`、不写终态、不计失败 | 滚动重启后任务由别的 Worker 恢复 |
| step 行合法状态 | **五种**合法状态逐行可构造，其中 in-flight（`result_status` / `committed_at` / `kind` / `commit_digest` 四项同时为 `None`）与 **malformed 终局（前三项非空、`commit_digest` 非空、`evidence_id` 为 `None`）** 都必须成立；`TOOL_RESULT + SKIPPED`、in-flight 携带 `commit_digest`、终局缺 `commit_digest` 在**命令层与记录层各一条**构造即失败 | 两实现共跑 |
| commit 幂等依据 | 原命令重放幂等；改 status / 改证据 fact / audit 真子集 / audit 超集四条各 `ALREADY_COMMITTED_DIFFERENT` | `commit_digest` 落库并在恢复后仍可比 |
| 重试等待期形状 | `RETRY_NOT_DUE` 时租约字段完整且 `lease_expires_at == next_attempt_at`；判定顺序对调必须转红；`< / == / >` 三条边界 | 真实时钟下的等待与到期 |
| 任务重试闭环 | `RetryableTaskError` 只带闭集 reason；前三次**确实调用工具**；`task_failure_count` 1→2→3；第四次在执行前 `RETRY_EXHAUSTED` | 真实 PostgreSQL 预算耗尽 |
| `handle()` 让步 | 既存任务非终态 → `TaskInProgressError`，Runner 调用 0、`version` 不变；已终态 → 终态 `RenderPayload` | —— |
| 日志投递状态 | `delivery_state` 四态；确认回滚为 `failed`，提交确认丢失为 `not_confirmed` 且绝不谎报回滚；写进 `outcome` 必须打红阶段断言 | apply 后断链再重放，Evidence/audit 不重复 |
| 读路径纯净 | `query_task` 前后 `version` / audit 行数 / evidence 行数三样均不变 | 重复 `GET` 不写任何行 |
| 单一时间真源 | 命令里无 `now`；给 `DispatchQuery` 加回 `now` 并让 SQL 用它，边界断言必须转红 | 事务内只读一次 store Clock |
| 分层与 ID 派生 | `evidence/builder.evidence_id is contracts.evidence.evidence_id`；`persistence` 允许集**不放宽** | —— |
| 逐切片迁移 | 每个 Task 结束时 `test_schema_matches_migration.py` 成立 | `rev_0002..0005` 各自 up/down/up |
| 有效 grant 判据 | 六条反例（CREATED / PLANNING / 恰好过期 / owner 不同 / 旧 token / 未来 token）三类行计数均不变；改成只比 token 后必须转红 | 两实现共跑；真实租约过期窗口 |
| 审批恢复入口 | `DISPATCH` 与 `APPROVAL_RESUME` 的可领取集合不相交；租约已过期的暂停任务可被 `APPROVAL_RESUME` 领取 | M4 的三组审批恢复用例在 grant 契约下仍绿 |
| 恢复重建三份状态 | 最终状态恰为 `INDETERMINATE`；`failed_steps/degraded` 各自同时删两条等价防线时转红，工具预算反证独立转红 | 重启后 Gateway 调用不增加且状态不是 SUCCEEDED |
| 提交形状可判别 | 唯一命名矩阵 `STEP_COMMIT_INVALID_CASES` 同时驱动 command/record 两层；malformed 正例单列；`StepExecutionRecord.kind` 使行自描述 | 两实现绑定同一矩阵，恢复时无需猜"当初是不是 malformed" |
| step 真值表 | begin 8 行、commit 6 行逐行一条；含无 step row 的 stale/not-runnable、满预算下重入可 PROCEED 与接管为 BUDGET_EXHAUSTED、fencing 比 task 行而计数比 step 行 | 两实现共跑同一 suite |
| step 结果消费 | 存储 case id 直接驱动 Runner 消费闭表；逐行断言 Gateway/terminal/Evidence/audit 次数；ALREADY_COMMITTED 重建 failed/degraded | 重启读取既存终局不重复调用工具或写子行 |
| plan-wide tool budget | 跨 step 总数；同 token 重入不多计；新 grant 接管才 +1 | crash after begin 仍保守计数 |
| 状态与审计原子性 | 注入 audit insert 故障 → `status` 与 `version` 都不变 | 终态、retry、`retry_exhausted` 三条路径各一次 |
| audit seq 串行化 | 两个来源并发写同一 task，seq 连续无缺口无重复 | 真实 PostgreSQL 并发 |
| 事件投递闭集 | 五个成员的路由各一条 + 路由反证；四个非 `LOG_ONLY` 成员 + 无 task_id 各被 sink guard 拒绝；每条事件恰好一条日志且 `delivery_state` 四取值各一条；M4 append-only 回归 | sink durable、命令 audit、begin 内部 audit 三方多重集两两不相交；`ToolResult.error` 可重建 |
| 事务审计的适用范围 | §5.1 那张表逐行；`TaskAttemptCommand` 字段集合恰为列出项（无 `audit_events`、无 `now`）；成功领取不写事务审计而终态写入必写 | 领取审计丢失后租约自然到期、任务被重新领取 |
| 日志键集 | `extra` 键集**恰好等于** §5.1 那张表（含 `trace_id` / `event_id` / `step_id` / `capability_id` / `policy_revision`）；并发 trace 不串线；异常后已 reset | 容器日志里可按 `event_id` 对上 audit 行 |
| 响应关联 | `render.status` 一致；TaskView 的 `query_path` 重算；CLI transport spy 断言 outgoing URL 使用 `quote(task_id, safe="")`；结构合法但 task_id 错误 → 退出 2 | `/ ? # %` 伪造 id 保持编码并闭集 404，不要求成功路由；正常 UUID get 命中自身任务 |
| 框架自产响应 | `/v1/tasks/`、`/healthz/`、`/readyz/` 在 `follow_redirects=False` 下不得 3xx；打开 `redirect_slashes` 即转红 | 宿主经发布端口访问时同样无 307 |
| M4 SQL 契约回滚兼容 | 入口先做 SAVEPOINT index probe；兼容数据可降到 `0001_initial`，旧约束原名恢复且 M4 形状 insert 成功；不称跨镜像 | 超大合法 scope 在默认/强制模式都于任何变更前失败，schema/revision/data 不变 |
| compose 资源归属 | base 独立过完整断言；smoke/barrier 各过精确路径白名单；PyYAML 语义解析；越权节点与五类基线破坏均转红 | `docker compose config` 的 base+smoke、base+smoke+barrier 合并结果过同一完整断言；清理只作用本项目 |
| SKIPPED 不持久化 | 缺行 ≠ 损坏；`SKIPPED` 不被产生；`TIMEOUT` 必须被产生 | 恢复后条件重新求值结果与首次一致 |
| 损坏 submission | 六种 rejection 各一条；三项校验各一条损坏行；锁内原子 FAILED、不发 grant | 损坏任务不再被反复 dispatch，readiness 不受影响 |
| migration 升级 | metadata/revision 一致；M4 非终态任务被 `legacy_task_without_submission` 终态化 | 空库 upgrade；有 M4 数据 upgrade |
| migration 降级 | 兼容的真实 M5 数据：无 flag 拒绝；带 flag 后任务为 `m5_downgrade_discarded`；再 upgrade 无任何 `legacy_*`；不兼容旧索引的数据两种模式均拒绝且全库事实不变 | down/up 与失败前置性均在真实 PostgreSQL 验证 |
| 镜像无 `tests/` | wheel 装进干净 venv 仍能装配并完成一次任务 | 容器内同样成立 |
| readiness | 不调用业务、不扫描 task | DB/migration 失败 503；恢复后 200 |
| fake 隔离 | AST/import graph 含 `starrocks_fake` 反例；只放行 `interfaces/local_stack.py`；`src/` 不 import `tests.fakes.recordings` | Compose 无真实外部访问 |
| 容器/宿主暴露面 | Settings 只校验 IP 字面量 | 每个 published port 以 `127.0.0.1:` 开头；postgres 无 ports；无 `container_name` |
| smoke 中断点 | —— | marker 常量行可见后 kill；probe 证明 in-flight 且无 evidence；恢复后证据与基线等值、`attempt_count == 2` |

集成测试一旦配置 `PYTEST_POSTGRES_DSN` 必须执行，不能因连接/迁移/Docker 不可用而 skip；环境缺失就是该验收层未覆盖或失败，不能伪装通过。

## 9. 本地启动、停止、清理与回滚

### 9.1 启动

真实 secret 文件由开发者在仓库外或被 `.gitignore` 覆盖的位置创建；命令不把密码放进 shell history。

```bash
cd /Users/kloenguyen/Desktop/agent
docker compose -p xiaowei_m5 up --build -d --wait
docker compose -p xiaowei_m5 ps -a
docker compose -p xiaowei_m5 logs --no-color migrate
curl --fail --silent http://127.0.0.1:8000/healthz
curl --fail --silent http://127.0.0.1:8000/readyz
```

若 §6.1「须实机确认（1）」的结论是 `up --wait` 不接受已退出的 one-shot 服务，改用该节给出的四行退路序列。两种形状都保留 `migrate` 容器与其退出码/日志。

### 9.2 停止与保留数据

```bash
docker compose -p xiaowei_m5 stop api worker postgres
docker compose -p xiaowei_m5 down --remove-orphans
```

不带 `--volumes`，named PostgreSQL volume 保留，下一次启动可验证恢复。

### 9.3 明确清理测试数据

仅在用户明确要清理 M5 本地 smoke 数据时执行，且必须使用精确 project name：

```bash
docker compose -p xiaowei_m5 down --volumes --remove-orphans
```

该操作删除 `xiaowei_m5` Compose 项目的容器、网络和 named volume，数据库测试数据不可恢复；不得使用未解析变量或模糊 project name。

### 9.4 代码与 schema 回滚

- 未提交实现：只恢复本任务逐文件修改，不使用 `git reset --hard` 或 `git checkout -- .`。
- 已提交未合并：在 feature branch 上追加 revert commit；不改写共享历史。
- schema：先停止 API/Worker，只对 M5 revision 执行降级，且**只经 `interfaces/migrate.py` 这一个入口**（它是唯一能把 `allow_destructive` 送进 `config.attributes` 的地方）：

  ```bash
  python -m xiaowei_agent.interfaces.migrate downgrade --revision 0001_initial            # 默认拒绝
  python -m xiaowei_agent.interfaces.migrate downgrade --revision 0001_initial --allow-destructive
  ```

  **目标是 `0001_initial`，不是 `rev_0002`。** 两处都要说准：`0001_initial` 是 M4 那个 revision 的真实 id（文件名是 `rev_0001_...`，id 不带前缀）；而"回到 M4"必须**退到 M4 的 head 本身**——停在 `0002_task_execution_columns` 只会把 `uq_tasks_idempotency_scope` 留在被替换的状态，换回 M4 镜像后每一次 `create_task` 都失败（理由见 §7 前言）。

  第一条在库里存在任何 M5 submission / journal 行时**会被拒绝**（§4.5），这是设计而非故障。第二条也必须先通过旧 M4 唯一约束的 SAVEPOINT compatibility probe；`M4_DOWNGRADE_INCOMPATIBLE` **不受** `--allow-destructive` 覆盖。确实要降级时必须先备份，再用第二条，并接受两个不可逆代价：当时非终态的 M5 任务被写成 `m5_downgrade_discarded`；`rev_0003` 在升级时终态化的 M4 遗留任务**不会**被改回原状态。**不使用裸 `alembic downgrade`**：它绕过入口预检与授权通道。不删除 M4 表。

  **这条路径只证明 M4 SQL 契约兼容，不宣称跨镜像验证。** integration 用例的完整序列是：M5 head + 兼容的真实 M5 数据 → 无 flag 降级**被拒** → `--allow-destructive` 降到 `0001_initial`；断言 `uq_tasks_idempotency_scope` 以**原名**存在于 `pg_constraint` 且 digest 约束已不存在；用 **M4 那段 `ON CONFLICT ON CONSTRAINT` 的 SQL 形状**真正执行一次插入并成功；再 upgrade 回 head 并断言 schema 与 metadata 一致。另一条用例用超大但 M5 合法 scope 证明默认/强制两种降级都在任何变更前失败且所有 schema/data/revision 不变。没有 checkout/build/run M4 SHA，就不能把上述 SQL 形状测试叫作“跨镜像”。
- Compose：回退到上一应用镜像/SHA，并运行与该 SHA 匹配的 migration revision。
- 不自行 merge；由用户完成最终合并/部署/验收决策。

## 10. 精确验收命令

### 10.1 TDD 分层门

各 Task 的命令见第 7 节；触及 `governance/`、`planning/` 或 `tools/` 时必须额外立即运行：

```bash
python -m pytest -m security -q
```

### 10.2 M5 相关契约与真实 PostgreSQL

```bash
python -m pytest tests/contract tests/integration -q
python -m pytest tests/security -m security -q
python -m pytest tests/contract/test_compose_contract.py -q
docker compose -f docker-compose.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.smoke.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.smoke.yml -f docker-compose.barrier.yml config --quiet
```

PostgreSQL integration 通过 CI 或本地测试服务设置 `PYTEST_POSTGRES_DSN`；设置后 0 skip 是硬要求。`test_compose_contract.py` 对 base 做完整断言、对 override 做路径白名单；检测到 Docker 时还解析上面两种合并配置并复用完整断言。

### 10.3 Compose smoke

```bash
python scripts/compose_smoke.py            # project name 由脚本生成并做碰撞预检（Task 12）
```

脚本输出必须包含：build/image identity、migration 完成与退出码、PostgreSQL healthy、API ready、基线任务 id 与证据摘要、barrier marker 出现时刻、probe 结果（in-flight 且无 evidence）、恢复后证据与基线等值的比对结果、`attempt_count`、重复提交同 id、并发 claim/evidence 数量、容器内 CLI 的真实退出码、最终 `compose ps -a` 和四服务日志；然后在 `finally` 清理 project 与 volume。

排障时可在仍运行的精确 project 上读取：

```bash
docker compose -p xiaowei_m5 ps -a
docker compose -p xiaowei_m5 logs --no-color migrate api worker postgres
```

### 10.4 全项目四条规范门

必须逐条真实执行并保留退出码与尾部输出：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

### 10.5 Git 与 secret 收口

```bash
git status --short --branch
git diff --check
git diff --stat main...HEAD
git diff main...HEAD
git rev-parse HEAD
git log -1 --format='%H%n%P%n%ad%n%s' --date=iso-strict
```

只暂存本计划列出的 M5 文件，不使用 `git add -A`；两份 untracked 计划文档（§1.1）必须保持未暂存。提交前运行仓库已有 secret-shaped literal/security gate；不得在终端输出或交接中展示 secret 值。

## 11. 本轮复审处置表

> 早前轮次（W / X / Y / Z / AA / AB / AC / AD / AE 各系列）的结论已并入正文对应章节，不再单独列条——保留它们只会让同一条规则在文档里有两份表述，而其中一份迟早变成假话。**本表只覆盖本轮（AF1–AF11）**，外加三条本轮自查发现、评审未提的同路径问题。

| 编号 | 根因 | 实际影响 | 当前规范的修复 |
| --- | --- | --- | --- |
| AF1 | 上一轮修 AE1 时把 `evidence_id` 拉进了"同生同灭"那条链 | 链变成 `result_status ⟺ committed_at ⟺ kind ⟺ evidence_id`，于是 malformed 终局（前三项非空、`evidence_id` 必为 `None`）不可构造——而它是 §2.7 形状 B 的**唯一**落点；`commit_digest` 声称同生同灭却根本不在不变量里；全文把禁止行算进"六种合法组合" | 拆成两条正交判据：终局链 `result_status ⟺ committed_at ⟺ kind ⟺ commit_digest`，证据判据 `evidence_id is not None ⟺ kind is TOOL_RESULT`；五条规则对五行逐行核对；`StepCommitCommand` 带同一组 validator（含 `TOOL_RESULT ⇒ status ∈ {OK, FAILED, TIMEOUT}`）；计数统一为"五种合法状态 + 一条禁止状态"（§2.7、§4.2.1、§8、§14） |
| AF2 | Task 7 的红灯清单里 `LookupError` 同时写了两种相反结果 | 一行内既要 `FAILED + gateway_adapter_missing` 又要 fail-stop，而正文闭表选的是后者；实现者任选一条都能声称遵循计划，旧的错误终态化分支很可能被保留 | 删掉终态化那半句，只留 fail-stop，并逐项断言 `status` / `version` / `task_failure_count` / audit / evidence 五样不变；全文只剩一处 `gateway_adapter_missing`，且是显式否定（§3.5、Task 7） |
| AF3 | `PlanConflictError` 被无条件归进 `REJECTED` | 它的定义是"同一 task_id 已存在**不同**的计划"，只可能出现在恢复重算或并发规划时——那是漂移/不变量破坏，不是用户请求被拒；归 `REJECTED` 会把恢复漂移写成用户面拒绝并丢掉根因 | 移进 `DriftError` 那一格：`FAILED` + 闭集 reason `recovery_drift`、不消耗失败预算；并写明"按来源拆分"在这里**只有一个来源**（首次 `save` 走插入分支，构造不出冲突），因此不拆；补"stored plan 不一致 → `FAILED` 且 Gateway 0 次"用例（§3.5） |
| AF4 | `delivery_state` 承诺两阶段写入，而 `emit(event, delivery)` 没有 state 参数、receipt 或 finalize | 第二次写入**根本无法表达**；只分 committed/rolled-back 又会把“提交确认丢失”谎报成回滚 | 取消两阶段承诺：每条事件恰好一条日志；`Delivery` 完整覆盖 `COMMAND_COMMITTED` / `COMMAND_ROLLED_BACK` / `COMMAND_NOT_CONFIRMED`，后两者由 `PersistenceWriteOutcome` 区分；`delivery_state` 四态，apply 后断链有重放收敛反例（§4.7、§5.1） |
| AF5 | "生命周期事件随**对应命令**的 `audit_events` 提交"，但 `TaskAttemptCommand` 没有这个字段 | 成功领取同样改租约与 `attempt_number`，却没有审计归属方案——这条承诺兑现不了 | 把"哪些状态改动必须事务审计"写成闭表，判据是**丢了还能不能补救**：`transition` / `schedule_retry` / `commit_step_result` / `begin_task_attempt` 的**终态写入**必须（终态受保护、补不回来），**成功领取不必**（租约有 TTL 可自愈；核对 `postgres.py::acquire_lease` 确认它不动 `version` 与 `status`）；`TaskAttemptCommand` 字段集合由用例钉死（§5.1） |
| AF6 | 降级目标写成 `rev_0002`，且全篇拿文件名当 revision id | M4 的真实 id 是 `0001_initial`（文件名带 `rev_` 前缀、id 不带）；更严重的是 `rev_0002` 已把 `uq_tasks_idempotency_scope` 换成 digest 约束，而 M4 的 `postgres.py:254` 按**约束名**做 `ON CONFLICT ON CONSTRAINT`——停在 `rev_0002` 再换回 M4 镜像，每一次 `create_task` 都会失败 | 写死四个 revision 的 `revision` / `down_revision` 八个取值并断言 `heads` 恰好一个；`rev_0002.downgrade` 先按**原名**建回旧约束再删 digest 约束；回滚目标改为 `0001_initial`；integration 证明序列含"降级后用 M4 形状的 SQL 真正插入成功"，只查 schema 不算数（§7 前言、§9.4、Task 1、Task 11） |
| AF7 | `TaskView` 只有 `extra="forbid"`，没有任何关联不变量 | 一个结构完全合法但 `task_id` 取错的响应挑不出毛病——而 §2.8 刚因为"按裸 task_id 取回能拿到别人的行"收窄了 `get`，形状校验挡不住那条路径的回归 | 补 `render.status` / `query_path` 关联与 CLI 响应 id 校验；CLI transport spy 另断言 outgoing URL 使用 `quote(..., safe="")`。特殊字符伪造 id 只要求保持编码并闭集 404，不虚构成功路由（§2.8、§3.2、Task 9、Task 10） |
| AF8 | 八种失败状态的闭集只覆盖异常链 | Starlette 的 trailing-slash 307 是**直接响应**，不经 `StarletteHTTPException`，因此 `/v1/tasks/` 上闭集立刻不成立；307 还会连同请求体重放到 `Location`，与 CLI 侧关掉重定向是同一类风险的服务端一半 | 关闭 slash redirect（不纳入协议：两个端点只有自家客户端在调，纳入收益为零）；用例断言**行为**而非设置点：三条路径在 `follow_redirects=False` 下不得 3xx，附"打开即转红"的反证（§3.1、Task 9） |
| AF9 | 结构化日志写成一句"只允许固定键" | 漏掉既有关联字段或允许任意新增键都会削弱追踪/脱敏边界 | 精确列出 **13 个** custom extra 键并集合相等；`worker_instance` 在非 Worker 事件为 `None`，不因来源不同改变键集（§5.1、Task 9） |
| AF10 | smoke 清理安全只约束 project name，没约束资源本身 | "随机 project 即隔离"的前提由 compose 文件形状决定；同时 override 本来就是片段，要求三份文件独立过完整基线断言不可执行 | base 独立过完整断言；smoke/barrier 各过精确路径白名单；Docker 可用时两个合并配置再过完整断言。静态解析使用 dev-only PyYAML，不用脆弱手写解析器（§6.3、Task 11） |
| AF11 | 三处验收口径与正文脱节 | Task 9 的 GET 红灯只比 `version`（§2.8 明说只比 version 会全绿放行）；infra 矩阵仍写"成功即重置"（§3.3 已改）；退出标准仍写"六种合法 step 组合" | 三处逐条改齐：GET 同时比 `version` / audit 行数 / evidence 行数；改为"整轮 poll iteration 无 infra 故障才重置"；改为"五种合法状态"并显式要求 **malformed 终局可构造**（Task 9、§8、§14） |

本轮自查发现、评审未提的同路径问题（一并修在上表所属章节里）：

- **命令层缺同一组 validator 的后果比"晚一步拒绝"更重。** 只在 `StepExecutionRecord` 上设防时，一个 `(kind=TOOL_RESULT, status=SKIPPED, evidence=<有>)` 的命令会通过 fencing 与 in-flight 检查、算出 `commit_digest`，直到构造记录才炸——**此时事务里已经写了 Evidence 与 audit**，而这个异常按 §4.7 会被归成系统性故障，把一次调用方的形状错误升级成进程非零退出。所以两层不是重复：命令层挡调用方，记录层挡存储实现自己算错（`commit_digest` 与 `committed_at` 是存储层现填的，命令层看不见）。
- **sink guard 的判据写成"不是 `LOG_ONLY`"而不是逐个列举。** 逐个列举时，将来给 `Delivery` 加成员，新成员**默认绕过**守卫；写成否定式则默认受约束。同一条理由让 `test_dependency_baseline.py` 用集合相等而不是禁用清单。
- **`rev_0003` 的降级不是数据可逆的。** 它的 upgrade 把 M4 遗留的非终态任务写成 `FAILED` + `legacy_task_without_submission`，downgrade 删表但**不会**把状态改回去——原状态在升级时就没被保存，伪造一个"改回去"比留着终态更坏。"schema 可逆"与"数据语义可逆"在这条 revision 上必须分开说（§7 前言、§9.4）。

## 12. 明确非目标

- 不连接真实 LLM、StarRocks、Prometheus、Kafka、Kubernetes 或其他运维系统。
- 不实现真实 E1 写操作、readback、人工审批 API/UI 或审批消费；只保持已有 pause/resume 契约不回归。因此 M5 也**不新增审批读取端口**，异步查询路径的 `AWAITING_APPROVAL` 返回 `render=null`（§2.8）。
- 不实现飞书/Web UI、WebSocket、SSE、任务取消端点、批量任务、队列、调度器平台或 autoscaling。
- 不把 API、Worker、Runtime、TaskStore 拆成微服务。
- 不实现尚未消费的 `PRIOR_STEP_RESULT_IS` / `EVIDENCE_FIELD_ABSENT` condition 语义；继续 fail-closed。
- 不产生 `StepResultStatus.SKIPPED`：它是可从 evidence + `failed_steps` 重新求值的派生量，持久化它会让一个从未调用工具的步骤吃掉计划级预算（§2.6）。理由按 `RENDER_REF` 先例钉死，并由用例断言 M5 路径不会写出它。**`TIMEOUT` 相反，M5 必须产生**：gateway 已经把超时结构化为独立的 `ToolCallStatus.TIMEOUT`，压成 `FAILED` 是主动丢弃已有事实。
- 不在任务内重试单个步骤：步骤结果一次提交即终局，`ALREADY_COMMITTED` 覆盖 `OK` / `FAILED` / `TIMEOUT`。重试的粒度是**任务尝试**，不是步骤。
- 不为基础设施故障调用 `schedule_retry`：它只做进程级退避，因此 M5 没有 `RetryClass`（§4.4）。
- 不实现 per-actor 读取 ACL：同 tenant/environment 内跨 actor 可读，写进 ADR-010（§5.3）。
- 不调整 M6 及以后真实工具/能力路线。
- 不自行 merge、deploy 或归档任务。

## 13. 完成时的四类收口

### 已验证

完成时只列真实执行过的命令、退出码、测试数量/skip、Compose 实际服务状态与日志证据、精确 branch/SHA/diff。四条全项目门和 Compose smoke 缺一不可称 M5 代码闭环完成。

### 只读推理

本 V5.4.1 当前只证明设计与仓库现有契约对齐。**本轮真实执行过的只有源码核对与既有测试。** 历轮实测三项仍然有效：§2.3 那张表里三行 digest 写法；§3.2 / §4.3 里 emoji 请求体两种序列化的字节数（98,316 与 32,780）；SQLAlchemy 异常继承链（`sqlalchemy.exc.TimeoutError` 不在 `StatementError` 那条链上，§4.7）。前几轮回源码核对里改变过设计结论的四条依然成立：`src/` 下没有任何 `.resume(` 调用点；`ScriptedRunner` 在**构造时**就拒绝 `REJECTED`；`persistence` 的分层允许集不含 `evidence`；`_run_downgrade` 自建 `Config` 导致 `-x` 不可达。

本轮又有四条源码核对**改变了设计结论**（不是措辞调整）：

1. `rev_0001_initial_task_store_schema.py` 里 `revision = "0001_initial"`——**文件名带 `rev_` 前缀，revision id 不带**，因此全篇原先的回滚目标是一个不存在的 revision（AF6）；
2. `postgres.py:254` 是 `.on_conflict_do_nothing(constraint="uq_tasks_idempotency_scope")`，按**约束名**解析——所以停在 `rev_0002` 回滚 M4 镜像会让每一次 `create_task` 失败（AF6）；
3. `postgres.py::acquire_lease` 只写三个租约字段，**不动 `version` 与 `status`**——这是"成功领取不必事务审计"能成立的判据（AF5）；
4. `observability/log_sink.py` 的 `extra` **现在就在输出** `trace_id` / `step_id` / `capability_id` / `policy_revision`——因此上一版那句"只允许固定键"的清单照字面实施会删掉现有关联手段（AF9）。

其余全部为源码阅读与设计推理；计划文件不是任何代码、运行、部署或用户验收证据。§6 的 Compose 结论**全部**属于此类，其中三项已显式标注"须实机确认"。§3.1 关闭 slash redirect 的**设置点**同样属于此类：FastAPI 未安装，用例因此断言行为而不是断言设置点。

### 未覆盖

在未收到"开始 M5"前，所有实现、依赖安装、PostgreSQL migration、API/CLI/Worker、Compose、测试和运行均未执行。即使 M5 完成，真实模型、真实运维系统、生产部署、容量、长时间稳定性和人工审批端到端仍不在本里程碑覆盖范围。

### 残余风险

- PostgreSQL sequence 只近似公平；长事务下不保证 commit FIFO。
- crash-after-tool-call-before-commit 对未来真实副作用工具需要幂等/readback；M5 fake 可以证明恢复协议，不能证明真实系统 exactly-once。
- 15 分钟 infra window 与 1–30 秒 backoff 是本地基线，生产阈值需后续按观测调优。
- Compose health/readiness 证明单机本地闭环，不等同高可用、部署或用户验收；**且本轮完全没有 Docker 可用**，§6 的每一条都可能在实机上不同。
- `EvidenceLedger.append` 作为兼容公共面仍存在被未来代码误用的可能，当前通过 AST/调用链测试约束而非语言级封闭。
- `handle()` 保留了与异步路径不同的提交顺序（拒绝在建任务前抛出）。这是为了不把 ADR-008 的两个 eval gate 当作实施副作用改动而**主动接受**的重复；统一它需要一个带 eval 基线重录的独立里程碑。
- `interfaces/local_stack.py` 是唯一 fake 组装点，其隔离由 AST 检测器 + 精确白名单保证。检测器本身在 M5 之前存在漏检 `starrocks_fake` 的缺陷（既有缺陷，非 M5 引入），修复后仍属静态检查，不构成语言级封闭。
- smoke 的中断点只覆盖"工具已返回、结果未提交"这一个位置；另一个点（调用发出但未返回）在 M5 只有 begin 保守扣预算这一层保护。`CREATED` / `PLANNING` 两个崩溃点只有 integration 层的存储态构造证据，没有 Compose 级证据。
- `SKIPPED` 改为恢复时重新求值，其正确性依赖"evidence 与 failed_steps 不可变且完备"。这在 M5 的闭集条件下成立（只有 `ALWAYS` 与 `EVIDENCE_ROW_COUNT_BELOW` 被消费），一旦将来启用 `PRIOR_STEP_RESULT_IS`，需要重新评估。
- 容器内监听 `0.0.0.0` 的安全性完全依赖 Compose 的发布规则与私有网络。这条约束由静态契约测试承重，不由运行时强制。
- **进程被 SIGKILL 时，已缓冲进某个命令但尚未走到 emit 的事件不会留下任何日志**（§5.1 取消两阶段投递日志的直接代价）。换来的是"日志里没有假话"——两阶段的 `attempted` 记录在事务随后回滚时就是一条谎报。崩溃点的持久证据由 §2.7 的 in-flight journal 行承担，它不依赖日志。
- **`not_confirmed` 是传输结局，不是数据库事实。** 后续幂等重放可能证明原写已经提交，也可能真正补做；原日志仍保留 `not_confirmed`，不会被回写成 `committed`。最终事实以 TaskStore winner/Evidence/audit 为准，不能用单条日志推断提交状态。
- **`begin_task_attempt` 的内部终态事件可能只有 durable 行、没有日志行。** 这只发生在事务结果确认返回前连接丢失：调用方不知道实际 rejection，不能编造事件再送 sink；恢复后以 TaskStore 终态与 audit 为准。正常返回路径仍由 `committed_audit_events` 保证全部补日志。
- **`rev_0003` 的 upgrade 对 M4 遗留非终态任务的终态化是数据不可逆的**：downgrade 只回退 schema，不会把状态改回去（原状态在升级时没有被保存）。`--allow-destructive` 覆盖的正是这一半，回滚前必须备份。
- **`TransitionCommand.audit_events` 的非空要求在端口层强制不了**（M4 既有存储层用例合法地不带审计），只由一条 AST 用例与一条行为用例承重；它比 `RetryCommand` / `StepCommitCommand` 的 `Field(min_length=1)` 弱一档。
- **audit 不做 fencing 是一个被接受的放宽**（§5.1）：丢掉租约的 Worker 在停止之前仍可能追加事件。归属由必填的 `attempt_number` 保证，窗口由 heartbeat 取消执行分支限制。
- **同 tenant/environment 的不同 actor 可以互读任务**（§5.3），多 actor 部署时不成立。
- **`AWAITING_APPROVAL` 在异步查询路径上没有 pending 投影**（§2.8）。M5 无审批消费方，因此这是一个不影响任何可达用户流程的收窄；一旦 M6+ 引入审批消费端，必须同时补一个可恢复的 pending approval 引用，而不是让 API 去猜"当前是哪一步"。
- **升级会把当时仍非终态的 M4 任务判死；降级会把当时仍非终态的 M5 任务判死**（§4.5）。两者都是显式选择并有闭集 reason 码区分，但都不可逆。
- **`submission_digest` 与 `idempotency_scope_digest` 都是无密钥摘要**，与数据同库同事务。它们挡代码缺陷与并发错配，不挡能写库的攻击者；任何把它们表述为完整性证明的措辞都是错的。
- **`TransitionCommand.audit_events` 的非空要求在端口层强制不了**（§4.1.1(c)）：M4 的存储层用例合法地不带审计，把字段设为必填会打红一批与本次改动无关的用例。强制因此落在一条 AST 用例（生产调用点不传空字面量）与一条行为用例（每次 `version` 变化都有对应 audit 行）上，两者都是外部机制而非契约级封闭。
- **`smoke_step_barrier` 是一个进入了生产配置面的测试用途字段**（§5.4）。它换来的是"可被 fail-fast 校验、可被看见"，代价是配置面上多了一个只在 smoke 里为真的开关；默认关闭与"默认装配不是 wrapper"两条用例把它钉住，但它仍然是一个如果被误设为 true 就会让 Worker 卡住的旋钮。
- **迁移期的终态化不写审计**（§4.5），是"状态改动与审计同事务"的显式例外。审计职责由 `alembic_version` 加闭集 reason 码承担；这是一个被接受的口径分歧，不是遗漏。
- **步骤路径的 `grant_is_current` 要求任务恰为 `RUNNING`**（§4.2.2），因此一个在 `CREATED` / `PLANNING` 阶段崩溃的 Worker 不会留下任何 step 行。这是刻意的，但也意味着"计划已存、状态未推进"那个崩溃点在 journal 里不可见，只能靠 `PlanStore` 的幂等命中来识别（§2.6 表第二行）。
- **四个摘要（`submission_digest` / `commit_digest` / `idempotency_scope_digest` / `retry_command_digest`）都是无密钥的一致性 checksum**，与数据同库同事务。它们挡代码缺陷与并发错配，不挡能写库的攻击者。
- **`APPROVAL_RESUME` 在 M5 没有生产调用方**（§2.5），正确性只由 Runner 契约测试承重。M6+ 接入真实审批消费端时必须重新审视「谁有权发起恢复」。
- **持久化故障不再有「任务级」一类**（§4.7）：任何 `IntegrityError` 都会让 Worker 进程 fail-stop。若将来出现真实的任务局部约束冲突，它会表现为一次进程重启而不是一个任务失败——这是刻意选的保守方向（宁可停机也不批量误杀），但它把一类局部问题放大成了进程级事件。
- **smoke 的碰撞预检与 `up` 之间存在 TOCTOU 窗口**（Task 12）。128 位随机名让意外碰撞不可能发生，但它不是互斥锁；保证只到「不清理预检发现的既有项目」为止。
- **M4 降级 compatibility probe 假设 API/Worker 已停止。** probe 与真正重建旧约束之间没有跨进程锁；若仍有写流量，新行可在两者之间进入。§9.4 的停服是硬前置，测试只证明停服条件下的原子失败/成功。
- `_finish` 的 fencing 绕过是 M5 之前就存在于 `main` 的缺陷（§3.5）。M5 修掉它并补 AST 反证，但在 M5 合入之前，任何基于当前 `main` 的并发实验都可能观察到旧 Worker 覆盖新 winner。

## 14. 退出标准

M5 只有同时满足以下条件才可以提交给用户验收：

1. 本计划最终确认后，从最新 main 创建的 `claude/m5-api-worker-compose` 上实现；
2. ADR-010 与 `ARCHITECTURE.md` 的契约更新在第一个代码提交**之前**完成（Task 0），至少包含 fencing token 第二个推进点、状态与审计同事务及内部事件回传、confirmed rollback/not-confirmed、M4 降级 compatibility probe、`AWAITING_APPROVAL` 投影收窄与跨 actor 可读；
3. M4 入口证据未被破坏，`test_audit_seq_is_scoped_per_task` 等 M4 承重用例逐条仍绿；
4. API/CLI/Worker 薄边界与模块层级安全测试通过，`interfaces/*.py` 与 §5.4 的文件级允许表集合相等，API→persistence、CLI→application、tools→capabilities 三条变异均转红；fake 只由 `interfaces/local_stack.py` 组装；
5. TaskStore 两实现共用 14 方法契约与行为 suite，哨兵按 8→11→14 两步推进且每步与实现同提交，四个新绑定已登记进 `_BINDINGS`；
6. §4.2.1 的两张存储真值表及 Runner 消费闭表逐行有用例；`PROCEED` 是唯一 Gateway 分支，`ALREADY_COMMITTED` 不重跑且重建失败上下文，预算/未知 step 的终态 reason 正确，commit 两个不变量破坏分支 fail-stop；§4.2.2 的 grant 反例三类行计数均不变；提交非法形状只由 `STEP_COMMIT_INVALID_CASES` 驱动且两实现绑定同一矩阵；
7. task/submission、step/evidence/audit、**状态改动与其审计**三处原子性各有真实 PostgreSQL 证据，其中第三处有 insert 故障注入反证；step 行**五种**合法状态逐行可构造（含 in-flight 四项同时为 `None`，以及 **malformed 终局可构造**——它是形状 B 的唯一落点），`commit_digest` 的四条差异反例（含 audit 子集与超集）全部通过；
8. `schedule_retry` 的幂等重放、命令不匹配与旧 grant 立即失效各有反例；`RetryClass` 不存在于代码中；
9. `CREATED`（有/无 plan）、`PLANNING`、`RUNNING` 三个崩溃点各有恢复反例；跨重启时，`failed_steps/degraded` 的快照预加载与 `ALREADY_COMMITTED` 采纳是等价防线，分别对两处同时退化做行为反证，不用测试锁死任一私有实现位置；工具预算仍有独立反证；其中**恢复后最终状态恰为 `INDETERMINATE`** 必须被断言；`AWAITING_APPROVAL` 经 `APPROVAL_RESUME` 可在租约已过期后恢复，M4 的三组审批恢复用例仍绿；
10. 重复提交、并发 Worker、Worker 重启、retry handoff 与 fencing loser 均有反例；`_finish` 的既有 fencing 绕过已修复且有行为反例与 AST 反证；
11. §3.5 的异常闭表逐行有用例且每行断言调用次数；§4.7 的两分类各有反例，`DisconnectionError` 与 invalidated DBAPIError 先于 `StatementError` 映射为 unavailable，非连接失效 SQLAlchemy 尾部归 systemic；写路径的 `PersistenceWriteOutcome` 能区分 confirmed rollback/not-confirmed，异常与日志均不含 SQL/参数/DSN；
12. 四个有序 revision 各自有 up/down/up 用例，且每个 Task 结束时 schema 与 migration 一致；降到 `0001_initial` 前先做 SAVEPOINT old-index compatibility probe：兼容数据完成 M4 SQL 契约插入，不兼容的超大合法 scope 在默认/强制两种模式都于任何变更前失败且 schema/revision/data 不变；该证据不冒充跨镜像；
13. wheel 装进不含 `tests/` 的干净环境可完成打包/装配证明（内存 store），真实存储证明由 integration 与 Compose smoke 承担；`tools/starrocks_recording.py` 已登记为 fake、不得 import capabilities，operation key 只由 `local_stack.py` 从 capability 真源组装；
14. Compose API/Worker/migrate 使用同一镜像，migration 经程序化 connection 注入且凭证不出现在 argv/env/日志，health/readiness 与 secret file reference 生效；base 独立过完整契约、两个 override 各过精确路径白名单、两个 Docker 合并配置过同一完整断言；PyYAML 仅为 dev 依赖；§6 的三项"须实机确认"已实机验证并写回文档；
15. Compose smoke 无 skip、无真实外部调用，RUNNING 中断由 local-stack wrapper 的 barrier 制造并有 probe 证据，每次等待有超时上限；**project name 由脚本生成且碰撞预检有"拒绝执行且不发任何 `down`"的用例**；`/readyz` 被显式轮询而不是拿 `--wait` 结果代替；
16. API 的失败协议真的是闭集，trailing-slash 不产生 3xx；`TaskView` 关联不变量成立，CLI 的实际 outgoing URL 证明 `quote(..., safe="")`，特殊字符伪造 id 保持编码并闭集 404，不要求成功路由；错误 task_id 响应退出 2；`GET` 纯读同时比对 version/audit/evidence；
17. §5.1 的事件投递闭表五个成员各有用例，四个非 `LOG_ONLY` 成员 + 无 task_id 被 guard 拒绝，每条事件恰好一条日志且 `delivery_state` 四态；sink durable、命令审计、begin 内部审计三方多重集两两不相交；结构化日志 custom extra 键集恰好等于 13 项表，非 Worker 的 `worker_instance` 为 `None`；
18. 四条全项目规范门全部为 0；
19. 给出精确 SHA 和"已验证、只读推理、未覆盖、残余风险"，不把本地测试称为部署或用户验收；
20. 不自行合并、不自行归档，等待用户明确验收。
