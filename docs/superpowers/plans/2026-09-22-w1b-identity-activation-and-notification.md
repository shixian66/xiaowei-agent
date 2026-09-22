# W1b 身份激活与通知 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 未登记的飞书身份进入系统时幂等创建激活申请、不签发任何业务 Session；Admin 明确批准或拒绝；批准复用 W1a 的身份目录与审计原子写路径，用户重新登录后才生效；并在群事件的当次响应内回一张通用激活卡片（私聊 Admin 通知与可恢复重试在 W3，见第 7 节）。

**Architecture:** 新增 `activation_requests` 一张工作流事实表与 `ActivationStore`（内存 + PostgreSQL 两实现 + 共享行为套件）。`ActivationStore` 只创建/复用 `PENDING` 并收割为 `EXPIRED`；批准/拒绝是 `UserDirectoryStore.apply()` 判别联合的两个成员，只有它能把申请写成 `APPROVED/REJECTED`，且批准时把账号、角色、绑定、申请终态与审计放在同一个事务。入口只改两处既有的“身份未知”分支；群通知复用既有 `ChannelMessagePort`，每次事件只发起一次带稳定幂等引用的发送，不新建重试框架或投递状态。

**Spec:** [Web 运维工作台、身份激活与未来结果访问边界总体设计](../specs/2026-09-19-web-operations-console-identity-activation-design.md) §7、§14.2；[ADR-014](../../adr/ADR-014-real-feishu-oauth-and-web-activation.md) §「仅为后续阶段的契约」。产品定位不在本计划重新讨论。

**基线:** `main@f92aa88b9016b52c35ebce4052a8609fd574d8fb`（PR #65 squash 合入，W1a 切片 A/B/C 全部在 `main`）。

---

## Global Constraints

- **不自动授予 Admin。** 任何 Web / 群激活路径的 `approved_role` 闭集只有 `USER` 与 `OPERATOR`，在契约层拒绝 `ADMIN`，不靠调用方自觉。
- **激活 ≠ 数据库结果访问权限。** 批准只建立账号、作用域角色与外部身份绑定，不创建任何结果 ACL、不注册结果路由。
- **Admin 不绕过申请人/审批人结果 ACL。** 本阶段没有结果域，这一条以否定守卫形式存在：不得出现任何"Admin 直读结果"的分支。
- **不建设普通用户"我的结果"门户。**
- **不接真实飞书、不读真实凭据、不部署、不做 canary/UAT。** 网络调用次数为 0；通知在测试中只对 fake port 发生。
- **两端闭合。** 新契约的上限必须对得上它要接收的真实取值范围——W1a 切片 C 复审的同一条纪律。
- **派生而非校验。** 能由命令派生的字段不让调用方传。
- 四门权威命令不可改：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。

## 非目标（只作为后续依赖列出，不在本阶段实现）

- **W2 登录与多 shell**：登录入口改造、`web_oauth_login_contexts` 表与闭集 `return_intent`。因此 `ActivationRequest` 本阶段**不落 `return_intent` 列**——它的唯一生产者与唯一消费者都在 W2，提前建列就是建一列没有写入者的列。
  这一条此前与详细规格 §17.1 冲突（规格把该表放在 W1b，`DEVELOPMENT_PLAN.md:182` 放在 W2）。现已按 `DEVELOPMENT_PLAN.md` 收敛真源并在规格 §17.1 留下署名修订；负责人在本轮收口中确认该归属。整体 W1b 计划仍须通过复审后才能开工。
- **W3 用户/职责/审计 UI**：Admin 待办页面、审计查询 API、激活通知人绑定，**以及私聊 Admin 通知与可恢复的投递重试**（见第 4 节不变量 8 的证据）。本阶段的批准/拒绝入口是与切片 C `migrate_static_identities` 同形的模块级一次性命令，不加路由、不加页面。
- **旧静态身份的部署迁移**：W1a 已交付并测试 `migrate_static_identities()`，W1b 只把运行时解析真源切到数据库，**不在每个进程启动时自动迁移**，也不以静态文件 fallback。旧文件及其配置/挂载按批准规格只读保留一个发布周期；真正部署前由 W5 的发布预检在停止消费者的窗口显式执行并核验迁移报告。未取得这份部署证据时不得启用 W1b 运行时链路。该运维动作不是本轮源码测试能够证明的事实。
- **终态申请保留策略**：1024 上限只约束有效 `PENDING`，不约束历史终态行。W1b 不凭空选择 30/90 天清理周期，也不预留通用 delete API；W5 部署前必须按数据保留要求另行定稿并验证 `APPROVED/REJECTED/EXPIRED`（含受控 PII）的清理机制。未完成时不得部署启用。
- R1 结果访问、W4a/W4b/W5、I3。

---

## 1. 当前源码接缝

以下每一处都是既有代码，本阶段只在这些位置接线，不重写它们的正确路径。

| 接缝 | 位置 | 本阶段动作 |
| --- | --- | --- |
| Web 登录未知身份 | `src/xiaowei_agent/interfaces/web_auth.py:490-495`（`identity_missing` 后直接 `raise WebAuthenticationError`） | 改为先幂等创建/复用申请，再抛一个可区分的"待激活"错误；**`rotate_session` 不得被调用** |
| Web callback 错误映射 | `interfaces/web_app.py:1220-1234` 把全部 `WebAuthenticationError` 统一映射为 `401 unauthorized`；`interfaces/http_models.py:25-40` 是错误码闭集 | 新的待激活错误必须先进入闭集，再在普通认证错误之前单独映射成 `403 activation_pending`；不设 Session Cookie。W2 才增加成熟 shell，本阶段只交付闭集错误语义 |
| 群内未知发送者 | `src/xiaowei_agent/interfaces/feishu_listener.py:176`（`IDENTITY_UNMAPPED` 拒绝） | 同上，另加通用激活卡片；群任务不排队、不重放 |
| 身份解析协议 | `FeishuIdentityDirectory.resolve()` 是**同步**方法（`interfaces/feishu_identity.py:34`），而目录的 `resolve_by_subject()` 是**异步**方法（`persistence/identity.py:243`） | 把协议、静态实现与全部调用点统一改成 `async def resolve()`。**不得**在实现里 `asyncio.run()` 或阻塞事件循环——三个调用点都已在 async 函数里，改造代价只是加 `await` |
| 同步调用点（共三处 + 两处测试替身） | `interfaces/feishu_listener.py:175`、`interfaces/web_auth.py:491`（登录）、`interfaces/web_auth.py:538`（Session 再认证）；静态实现 `feishu_identity.py:110`；conformance 锚点 `_conformance.py:214` | 逐处加 `await`，并同步改 `tests/contract/test_web_app_routes.py:103` 的替身与既有身份用例 |
| 身份解析来源 | `WebAuthService.__init__` 的 `identities: FeishuIdentityDirectory`（`web_auth.py:306`）与 listener 的同名依赖（`feishu_listener.py:94`） | 协议改异步后，新增一个由 `UserDirectoryStore` 支撑的 adapter 实现同一协议，并让 Web 与 listener **同时**切到它；只改 Web 会让已批准用户仍无法在群里使用小维。旧静态文件只作为一次性迁移输入保留，不再是运行时 fallback 真源 |
| “未登记”与“已撤权” | W1a 的 `resolve_by_subject()` 目前对无绑定、停用账号与缺当前 scope 角色都返回 `None`（共享套件把停用后的 `None` 钉死） | W1b 不能把三者都变成激活申请。修改共同 store 语义：真正无绑定仍返回 `None`；绑定存在但账号停用或当前 scope 无角色时抛新的闭集 `UserDirectorySubjectUnavailableError`。数据库与 fake 共享套件同时改；数据库 adapter 把 `None` 映射成 `FeishuIdentityNotFoundError`，把该错误映射成新闭集 `FeishuIdentityUnavailableError`。Web 将后者收敛为普通 `WebAuthenticationError`，listener 将后者收敛为既有 `IDENTITY_UNMAPPED` 诊断；两边都零申请、零 Session、无专用可枚举文案 |
| 授权写入口 | `UserDirectoryStore.apply()`（`persistence/identity.py:257`），命令联合 `DirectoryCommand`（`contracts/identity.py:245`），动作映射 `ACTION_FOR_COMMAND`（`persistence/identity.py` 同名常量） | 新增批准、拒绝两个命令作为联合的第九、十个成员；两者放在 `contracts/identity.py`，不让 `identity.py` 与 `activation.py` 互相导入 |
| 审计写面 | `AdminAuditStore`（`persistence/admin_audit.py:113`），动作闭集 `AdminAuditAction`（`contracts/enums.py:112`），目标闭集已含 `ACTIVATION`（`contracts/enums.py:131`） | 新增两个动作成员；`target_kind=ACTIVATION` 首次有生产者 |
| 审计契约 | `contracts/admin_audit.py:68-101` 定义 `DIRECTORY_ACTIONS` / effect 集合，`AdminAuditStart` 的拒绝也在该文件 | 十个目录动作改成显式闭集，新增显式 `STARTABLE_ACTIONS`（W1b 为空）；批准动作进入 role/status effect 集合，拒绝动作不进入。不能只改 `persistence/admin_audit.py` 的 Protocol 注释 |
| 出站通知 | `ChannelMessagePort.send_to_chat()`（`application/channel_projection.py:84`）与 `FeishuSdkMessageAdapter.send_to_chat()`（`interfaces/feishu_sdk.py:560`） | 每次事件只调用一次；`idempotency_ref` 由已验证的 event id 域分隔摘要派生。SDK 重投仍使用同一引用；不调用私有 `_retry_delay()`，不在 30 秒 callback 内自行 sleep/retry |
| 受控 PII 上限 | `CONTROLLED_PII_MAX_LENGTH`（`contracts/base.py`）、`ControlledPiiField`（`contracts/identity.py:50`） | 直接复用；`subject_ref` 不得再出现第二份上限 |
| 审计动作闭集的**数据库副本** | `rev_0014_identity_admin_audit.py:119` 的动作闭集、`:156`/`:162` 两条 effect CHECK、`:167` 的单阶段 CHECK | `rev_0015` 必须**替换四条约束**；Python 契约、运行时 schema 与迁移 DDL 三边集合相等 |
| 写入口 AST 守卫 | `tests/security/test_identity_write_path.py:42` 的 `AUTHZ_TABLES` 只治理授权表 | `activation_requests` 是工作流事实，**不得**混进 `AUTHZ_TABLES`。新增独立 `ACTIVATION_TABLES` 闭集：`ActivationStore` 只写 `PENDING/EXPIRED`，`UserDirectoryStore.apply()` 只写 `APPROVED/REJECTED`，两类 owner-qualified 写点分别做集合相等 |
| 内存原子性 | `persistence/memory.py:27` 是 fake Store 共用状态；`fake.py:746-779` 的目录快照目前不含激活事实 | `activation_requests` 加入共享状态、同一把锁和目录快照/恢复；不能让两个 fake Store 各持一份状态 |
| 行与契约互转 | `persistence/rows.py` 显式把数据库字符串还原为 strict `StrEnum`；直接 `model_validate(dict(row))` 不成立 | 新增 `activation_request_to_row()` / `row_to_activation_request()`，两实现与集成测试不得各写一份转换 |
| Protocol 锚点 | `_conformance.py` 与 `tests/contract/test_protocol_conformance.py` 同时钉住已有 Store 的静态与运行期表面 | `ActivationStore` 的内存/PostgreSQL 实现沿用同一机制；公开方法集合精确相等，不允许额外通用写入口 |
| 窄栈装配 | `build_postgres_web_stack()` 与 `build_postgres_feishu_listener_stack()` 当前仍装配静态身份目录；listener 也没有出站 `ChannelMessagePort` | 两个栈共用各自 engine 上的目录/激活 Store 与数据库身份 adapter；listener 额外显式注入或装配 message port。新增依赖必须是必填构造参数，不能用 `None` 默认值让生产装配静默漏掉激活链；两个进程仍不得获得 Runner/Gateway/目标 adapter |
| 串行化临界区 | `persistence/postgres.py:284` 已有固定常量的事务级 advisory lock 先例 | 容量与去重判定复用同一机制取一把**固定**的事务级 advisory lock，不自创第二套并发控制 |
| 迁移头 | `rev_0014_identity_admin_audit.py` | 新增 `rev_0015_activation_requests` |
| 迁移降级 | `rev_0014_identity_admin_audit.py:211-225` 用 `require_destructive_authorization` 保护有数据降级 | `rev_0015` 对申请行与激活动作审计行分别计数；无数据正常降级。只有申请行时可经显式破坏性授权删除后降级；一旦存在激活动作审计，降级始终拒绝，不能删除 append-only 审计后留下无审计的授权事实 |

**schema 门的实际范围**：ADR-014 §R3 冻结的是 **OAuth state 域**（`web_oauth_states`，只放开 `web_oauth_login_contexts` 一张表），不是全仓 schema 冻结——W1a 的 `rev_0014` 已经新建四张表而无需再修订 ADR。`activation_requests` 不属于 OAuth state 域，因此不触发该门。实现前仍须自行复核这一判断；若判断被推翻，先停下改 ADR，不得自行加表。

`ActivationStore` 的公开表面固定为两个方法：`create_or_reuse(command) -> ActivationRequest` 与
`load(query: ActivationLookup) -> ActivationRequest | None`。`ActivationLookup` 必含 `request_id + tenant_id + environment_id`；不可枚举 ID 不能替代显式 scope。它没有 `approve()`、`reject()`、通用 `update()` 或
`save()`；终态决策只能经 `UserDirectoryStore.apply()`。W3 若需要列表能力，届时按实际页面查询另扩窄读面，
不在 W1b 预留通用搜索接口。

两个实现都在构造时注入既有 `Clock`，24 小时 TTL、`expires_at <= now`、收割与审批时效判断只读这一个时钟；
契约命令不允许调用方传 `requested_at` / `expires_at` / `decided_at`。PostgreSQL 与 fake 若各自直接调用
`datetime.now()`，同一共享套件将无法证明边界一致。

`ActivationRequest` 的字段组合也在切片 1 一次定死，不能等到写 SQL 时临时决定：

- `request_id`、`requested_at`、`expires_at`、状态、决定时间与来源摘要全部由 store 派生；调用方只提供经 SDK/OAuth 验证的 `subject_ref`、scope、来源种类与群入口的 event/chat 引用。创建命令上的三项原始外部引用全部使用 `ControlledPiiField`（可空字段保留同一标注），同时关闭 `repr` / `model_dump`；只有 `subject_ref` 作为批准所需事实进入申请行，event/chat 在 store 边界立即摘要后丢弃原文。
- W1b 的来源闭集只有 `WEB_LOGIN | FEISHU_GROUP`；`SAFE_TASK_LINK` 与其 return intent 同属 W2。Web 行的 event/chat digest 必须都为空，群行必须两者都有；原始 event/chat 引用不落库。
- `subject_ref` 是完成批准后绑定所必需的 `ControlledPiiField`，允许持久化但不得出现在 `repr()` / `model_dump()` / 日志 / Admin 列表；去重索引使用独立域的 `subject_ref_digest`，不能复用 `external_identities` 的摘要域。
- `PENDING` 没有任何决定字段；`APPROVED` 必须同时有 `decided_at + decided_by + approved_role(USER|OPERATOR)`；`REJECTED` 必须有前两项且没有角色；`EXPIRED` 不伪造审批者或角色。Python validator、运行时 schema 与 migration CHECK 三边表达同一组约束。
- 有效期边界固定为 `expires_at <= now` 即过期。批准/拒绝的 CAS 必须把 `status=PENDING`、scope 与尚未过期放在同一条写条件里，不能先读后写。

listener 的未知身份分支还必须按已解析 channel 再分一次：只有 `FEISHU_GROUP` 创建申请并发送通用卡片；
`FEISHU_PRIVATE` 在 W1b 继续走既有 `IDENTITY_UNMAPPED` 拒绝，不得伪造为 `FEISHU_GROUP`，也不得新增计划外的
第三个来源成员。私聊入口何时进入激活流程，随 W3 的通知人/私聊产品链另行决定。

## 2. 批准时账号字段的来源

`CreateUserCommand`（`contracts/identity.py:154`）必填 `user_id`、`actor`、`display_name`、`tenant_id`、`environment_id`、`role`，而 OAuth 回调与群事件**只提供 `open_id`**。批准与拒绝使用两个窄命令，避免一组可空字段制造无效组合：`ApproveActivationCommand(request_id, tenant_id, environment_id, actor, display_name, approved_role)` 与 `RejectActivationCommand(request_id, tenant_id, environment_id)`。store 必须用三字段一起定位申请；只按 `request_id` 命中属于跨作用域缺陷。三个建号字段的真源固定如下：

| 字段 | 来源 | 理由 |
| --- | --- | --- |
| `user_id` | store 先按全局唯一 `actor` 查已有账号：存在且仍为 `ACTIVE`、展示事实匹配时复用其 `user_id`；不存在时才从申请主体与 scope 做域分隔摘要派生，截断到 `BoundedId` 内 | `UserAccount` 不带 scope、`actor` 全局唯一；同一个人增加第二个环境角色时不能被迫创建第二个账号。新账号的 ID 仍由 store 派生，不由调用方指定 |
| `actor` | **模块级批准命令显式要求 Admin 提供**，`BoundedActor`（256） | `actor` 是身份、不可截断也不可猜；飞书 `open_id` 不是人类可读的 actor |
| `display_name` | 同上，`BoundedName`（128），是唯一允许截断的一类 | 纯显示字段，不参与任何授权判定 |

**不为了拿昵称去接飞书用户资料 API**：那会在本阶段扩大真实外部接口，越出"网络调用为 0"的边界。

命令里的 `actor/display_name` 是**被激活账号**的信息，不是审批者身份。审批者只能来自服务端认证上下文。
真正的授权判定必须在 `UserDirectoryStore.apply()` 的**同一事务/同一内存锁**内完成：按
`context.actor_user_id + command scope` 锁定并重读账号与角色，要求账号仍为 `ACTIVE`、角色仍为
`ADMIN`、账号 actor 与 context 相同，且本阶段 `auth_source=LOCAL_ADMIN`。只在
`IdentityActivationService` 里先 `load_account()` 会留下“校验后被并发撤权仍能批准”的 TOCTOU。
store 用闭集错误把拒绝原因返回给 service，service 再经 `AdminAuditStore.append_denied()` 写 `DENIED`；
这条拒绝路径没有授权改变，不伪装成与成功事务同一事务。不能信任调用方传来的角色，也不能让 UI
隐藏按钮承担授权。

必须覆盖的反例/对照：已有同 actor、同展示事实的 ACTIVE 账号被复用并只增加当前 scope 的角色/绑定；
当前 scope 尚无角色时才新增 `approved_role`；已有角色必须与 `approved_role` 精确一致才可继续绑定，
不允许用既有 `_upsert_role()` 静默覆盖。已有 `ADMIN` 一律冲突——保留它会让普通激活间接获得 Admin，
覆盖它又会静默降权；Admin 的飞书身份绑定属于独立高风险操作，不在 W1b。另需覆盖同 actor 但展示事实
冲突、已有账号已停用、subject 已绑定到另一账号时整批冲突；同一申请重放批准不产生
第二个账号；`actor`/`display_name` 超长整批零写入；同 `subject_ref` 跨 tenant/environment 的绑定与角色
不串 scope。

## 3. environment_id 边界

`Settings.environment_id` 当前是无上限的 `StrictStr`（`config.py:235`），而目录契约的 `BoundedId` 是 64（`contracts/identity.py:32`）。**本阶段在配置入口收紧到 64：64 字符接受、65 字符拒绝**，拒绝发生在 `Settings` 构造时，且沿用 `hide_input_in_errors=True`，不把环境变量原值回填进异常。

两端闭合证据（无反对意见）：仓库内全部真实取值为 `dev`（`docker-compose.yml:2`、`.env.example:7`）、`test`（`docker-compose.m6b-test.yml:4`）、`staging`，最长 7 字符；收紧到 64 不挡任何现有配置，也不改变任何现有进程的启动结果。这同时关掉 W1a 切片 C 复审留下的最后一处口径分歧：同一个 `environment_id` 从此只有一个上限。

`tenant_id` 不在本阶段改动：它不是可由环境覆盖的 `Settings` 字段。

## 4. 状态机与关键不变量

```text
（无申请）──创建──▶ PENDING ──CAS 批准──▶ APPROVED
                      │ └──CAS 拒绝──▶ REJECTED
                      └──同临界区收割──▶ EXPIRED
```

终态三个，均不可再迁移；重放审批不改变既有结果。

1. **未激活不签发 Session。** 创建申请的两条入口路径上都不存在通向 `rotate_session` 的分支。
2. **同 tenant+environment+provider+subject 至多一个有效 `PENDING`。** 由**部分唯一索引**保证，不是每个调用方自己记得先查一遍。
3. **全库有效 `PENDING` ≤ 1024**，到达容量 fail-closed，且不得先按 subject 查是否已有申请。同一锁内固定顺序为：先收割过期 → 计数判容量 → 容量未满才按 scope+subject 复用或创建。满载时已有/不存在的 subject 都得到同一个闭集容量错误，Web 都映射为 `503 unavailable`，群里都使用同一张“暂时无法提交”的通用卡片；不能用 `403/503` 差异泄露 subject 是否已有申请，也不能在未落库时谎称“已提交”。
4. **过期是收割，不是后台任务。** 24 小时过期项在创建/复用的同一串行化临界区内先转 `EXPIRED`，再判容量与判重。
5. **决策不跨事务。** `ApproveActivationCommand` 把账号、作用域角色、外部身份绑定、`APPROVED` 与审计事件放在 `UserDirectoryStore.apply()` 的同一个事务；`RejectActivationCommand` 在同一入口写 `REJECTED` 与审计。不出现“账号已建、身份未绑”“申请已批准、目录没写完”或“申请已拒绝、审计没写下”的半状态。
   *被拒绝的备选*：让 `ActivationStore` 自己写目录表——那是第二条授权写路径，直接推翻 W1a 的唯一写入口不变量。
6. **只有当前有效 Admin 能作决定，也不自动授予 Admin。** 每次决定都按第 2 节在写事务里重新解析并锁定审批者，不能复用登录时角色快照；`approved_role` 由批准命令携带、默认 `USER`，提升为 `OPERATOR` 必须显式且可审计，`ADMIN` 在契约层拒绝。
7. **激活决策仍是单阶段审计。** 当前与 W1b 新增的十个目录动作全部进入显式 `DIRECTORY_ACTIONS`；`STARTABLE_ACTIONS` 在 W1b 仍为空，W4a 增加配置类动作时才显式扩展。`AdminAuditStart` 只接受 `STARTABLE_ACTIONS`，不得再用“非目录动作”的补集。矩阵冻结为：批准=`ACTIVATION_APPROVED + target=ACTIVATION/request_id + SUCCEEDED + role=approved_role + status=ACTIVE + reason=None`；拒绝=`ACTIVATION_REJECTED + target=ACTIVATION/request_id + SUCCEEDED + 空 effect + reason=None`；权限/作用域/目标失败=`对应 action + DENIED + 闭集 reason + 空 effect`。成功路径的 action/target/outcome/effect 全由 store 从命令派生；现有 `AdminAuditDenial` 仍由 service 指明“尝试的是批准还是拒绝”，但它在类型上写不出成功或 effect。
8. **通知只发生在群事件的当次响应内，而且只尝试一次。** 本阶段没有能力把通知投给具体的人：`external_identities` 只保存不可逆的 `subject_ref_digest`（`persistence/schema.py:706`），`UserDirectoryStore` 没有任何列出 Admin 的读路径（协议只有 `load_account` / `resolve_by_subject` / `apply`），`FeishuMembershipPort` 只能回答“某人是否在群里”（`application/channel_access.py:59`）而不能枚举成员，申请本身也只存群会话摘要——持久化重试时连真实 `chat_id` 都拿不回来。
   因此 W1b 只用当次事件里已有的明文 `chat_id` 立即回一张通用卡片：不 @ 具体 Admin；不调用私有 `_retry_delay()`，不在 30 秒 callback 内 sleep/retry。`idempotency_ref` 由 event id 的域分隔摘要确定性派生，SDK 重投仍使用同一引用；发送失败不回滚申请。私聊 Admin 通知与可恢复重试随激活通知人绑定在 W3 交付。
9. **申请不保存正文。** 无原始提问、SQL、结果行、Secret、任意 URL、群消息原文；事件与会话引用只存 domain-separated 摘要；`subject_ref` 是 `ControlledPiiField`，日志与列表只显示脱敏标识。
10. **W1b 不解析或 @ Admin**：群里有没有 Admin 都不影响申请创建；卡片不泄露目录，也不带尚未由 W2 交付的登录/状态链接。
11. **闭集的每一份副本都要一起改。** `AdminAuditAction` 的 Python 枚举、运行时 schema、迁移 DDL 与 `AdminAuditStart` 允许集必须同步。`rev_0015` 精确替换 `action_closed`、`role_effect_required`、`status_effect_required`、`directory_actions_are_single_phase` 四条 CHECK。漏一条时，真实 PostgreSQL 与内存实现会分叉。`activation_requests` 另建工作流写守卫，不能冒充授权表而把合法的 PENDING/EXPIRED 写入一并封死。
12. **降级不能靠碰运气，更不能删除授权审计。** `rev_0015` 无 W1b 数据时直接降级；只有申请行、尚无任何激活动作审计时，未取得既有破坏性迁移授权必须抛 `MigrationSafetyError`，取得授权后可删除申请表并还原四条旧 CHECK。只要存在 `ACTIVATION_APPROVED/REJECTED` 审计行，无论是否打开破坏性授权都必须拒绝降级：删除 append-only 审计会留下“授权事实存在但审计消失”，保留它又无法恢复旧动作闭集。所有拒绝路径都报告申请行/激活审计两类精确计数，revision 与数据不动；空库或仅申请行的获准降级随后必须能重新升级到 head。

## 5. 最小文件范围

**切片 1｜激活事实与审批写路径**

- 新建 `src/xiaowei_agent/contracts/activation.py`：`ActivationRequest`、状态/来源闭集与创建/查询 DTO；不定义目录审批命令。
- 修改 `src/xiaowei_agent/contracts/enums.py`：`AdminAuditAction` 增加激活批准/拒绝两个成员。
- 修改 `src/xiaowei_agent/contracts/identity.py`、`contracts/admin_audit.py`、`contracts/__init__.py`：定义 `ApproveActivationCommand` / `RejectActivationCommand` 并进入 `DirectoryCommand`；把目录动作、可启动动作与 effect 动作改成显式闭集并导出，避免 `identity.py ↔ activation.py` 循环导入。
- 新建 `src/xiaowei_agent/persistence/activation.py`：`ActivationStore` 协议与错误类型。
- 修改 `src/xiaowei_agent/persistence/schema.py`、新建 `migrations/versions/rev_0015_activation_requests.py`：建表、加入 `ALL_TABLES`，**替换** `rev_0014` 的四条审计 CHECK，并按不变量 12 实现 downgrade。
- 修改 `src/xiaowei_agent/persistence/identity.py`：把两个新命令加入 `ACTION_FOR_COMMAND` / `EFFECT_FOR_COMMAND`；将 `derive_audit()` 从“所有命令都写 `target=USER`”改为按命令派生目标——激活决定必须写 `target=ACTIVATION/request_id`，其余既有命令仍写 `USER`。该映射是成功审计事实的唯一生产者，不能在 fake/PostgreSQL 两个实现里各写一份；同一文件新增 `UserDirectorySubjectUnavailableError`，使 `resolve_by_subject()` 不再把已绑定但停用/缺角色伪装成未登记。
- 修改 `src/xiaowei_agent/persistence/rows.py`、`memory.py`、`fake.py`、`postgres.py`：集中行映射、共享激活状态、两个实现、目录快照恢复，以及 `apply()` 承载批准/拒绝命令。
- 修改 `src/xiaowei_agent/_conformance.py`：为内存/PostgreSQL 两个 `ActivationStore` 增加静态 Protocol 锚点。
- 修改 `src/xiaowei_agent/persistence/admin_audit.py`：收窄 `append_started` 的允许集（不变量 7）。
- 修改 `src/xiaowei_agent/config.py`：`environment_id` 复用 `BoundedId` 的 64 字符边界，不另抄第二个数字。
- 测试：新建 `tests/suites/activation_store.py` 作为唯一共享行为套件，由 `tests/contract/test_activation_store.py` 绑定内存实现、`tests/integration/test_activation_store_postgres.py` 绑定 PostgreSQL 实现，并在既有 `tests/contract/test_suite_bindings.py` 登记两边；新增 `tests/contract/test_activation_contracts.py`，修改 `tests/contract/test_admin_audit_contracts.py`、`tests/contract/test_identity_schema.py`、`tests/contract/test_identity_store.py`、`tests/contract/test_protocol_conformance.py`、`tests/contract/test_schema_matches_migration.py` 与 `tests/suites/identity_directory.py`，覆盖两个新目录命令、命令到 action/effect/target 的派生、真正未绑定与已绑定但不可用的错误分流、十动作闭集、四条 CHECK 和 schema/migration 等价；修改 `tests/unit/test_config_happy.py` 与 `tests/security/test_config_fail_fast.py` 覆盖配置边界；修改 `tests/integration/test_identity_directory_postgres.py`、`tests/integration/test_migration_paths.py`。`tests/security/test_module_layering.py` 登记新文件，`tests/security/test_identity_write_path.py` 新增独立激活表写点闭集，不修改 `AUTHZ_TABLES` 的语义。`tests/security/test_controlled_pii_exposure.py` 和 `tests/security/test_string_alias_coverage.py` 已按类型/别名自动扫描新契约，不新增手写字段白名单；切片开工后的首个红灯必须确认 `ActivationRequest.subject_ref` 被这两条现有守卫真实收集，而不是默认它们会覆盖。

**切片 2｜入口接线与通知**

- 新建 `src/xiaowei_agent/application/identity_activation.py`：创建/复用、批准、拒绝的应用服务，以及与 `migrate_static_identities` 同形的一次性批准/拒绝命令。
- **身份解析协议改异步**（组 1 的共同位置，必须先于 adapter）：`interfaces/feishu_identity.py` 的协议与静态实现、三个调用点 `interfaces/feishu_listener.py:175`、`interfaces/web_auth.py:491`、`interfaces/web_auth.py:538`、`_conformance.py` 锚点，以及既有测试 `tests/contract/test_feishu_identity.py`、`tests/contract/test_legacy_identity_document.py`、`tests/contract/test_web_app_routes.py` 的同步替身；同一文件增加不暴露原因的 `FeishuIdentityUnavailableError`，不得复用 `NotFound` 触发激活。
- 新建 `src/xiaowei_agent/interfaces/directory_identity.py`：`UserDirectoryStore` 支撑的 `FeishuIdentityDirectory` adapter；角色到渠道权限只调用既有 `governance.product_roles.channel_permissions()`，不复制第二张映射表。
- 修改 `src/xiaowei_agent/interfaces/web_auth.py`、`interfaces/web_app.py`、`interfaces/http_models.py`、`interfaces/feishu_listener.py`：两处未知身份分支接线，把 `activation_pending` 加入 HTTP 错误码闭集，并给 OAuth callback 增加对应映射。
- 修改 `src/xiaowei_agent/rendering/feishu.py`：新增纯函数通用激活卡片投影；卡片文案与 JSON 预算继续归 rendering 层，application 不拼展示 JSON。
- 新建 `src/xiaowei_agent/application/activation_notification.py`：只协调纯 renderer 与单次 `send_to_chat()`；稳定幂等引用由 event id 派生，不新建持久化投递状态或重试器。只捕获端口契约的 `ChannelMessageError`，记录不含主体/群/正文的闭集诊断并 ACK 已经持久化的申请；编程错误不得被宽泛吞掉。
- 修改 `src/xiaowei_agent/interfaces/local_stack.py`：两个窄栈共用数据库目录 adapter 与激活服务，listener 显式持有 message port；所有新增构造依赖均为必填且更新全部构造调用点，避免 `None` 默认值形成静默旁路。
- 测试：`tests/contract/test_identity_activation_service.py`、`tests/contract/test_activation_notification.py`、`tests/unit/test_feishu_rendering.py`、`tests/security/test_activation_boundaries.py`，并更新既有 `tests/contract/test_api_contract.py`、`tests/contract/test_feishu_identity.py`、`tests/contract/test_feishu_listener.py`、`tests/contract/test_legacy_identity_document.py`、`tests/contract/test_web_auth.py`、`tests/contract/test_web_app_routes.py`、`tests/contract/test_ri5_config_api.py`、`tests/contract/test_ri5_probe_routes.py`、`tests/evals/test_m7_channel_safety.py`、`tests/integration/test_m7_channel_flow.py`、`tests/security/test_feishu_ingress.py`、`tests/security/test_feishu_log_redaction.py`、`tests/security/test_ri5_web_assembly.py`、`tests/security/test_web_auth_boundary.py`、`tests/unit/test_local_stack.py` 与 `tests/security/test_module_layering.py`。这些是当前 `FeishuListener`、`WebAuthService`、两个 PostgreSQL builder 与同步身份替身的直接构造/调用者；实现时仍须用 AST/全文搜索重新核对所有 `resolve()` await 和所有新增必填构造参数的调用点，集合有变化就按真实调用图更新，不把本清单当成永不变化的白名单。

**每个切片的事实同步**

- 切片 1 合入前更新 `AGENT_HANDOFF.md`，只写“激活存储/写内核已离线实现、入口尚未接线”，不得提前写 W1b 完成。
- 切片 2 收口时修改 `ARCHITECTURE.md`、`README.md`、`AGENT_HANDOFF.md` 与 `tests/contract/test_doc_fact_binding.py`；`DEVELOPMENT_PLAN.md` 不写进度，也不因实现完成而改退出标准。

两个切片依赖单向、各自独立可复审可合入。若切片 1 复审暴露形状问题，切片 2 不得先行。

静态文件的 `Settings.feishu_identity_file`、Compose 只读挂载与解析/迁移函数本轮保留，是一个发布周期内的
迁移输入，不是第二个运行时身份真源；因此切片 2 不删除这些配置，也不从数据库 miss 回退读取文件。
W5 若未先运行并核验 W1a 的迁移入口，就不得部署启用切片 2。这样避免 W1b 合入后自动迁移权限，也避免
现有已登记用户在真实部署时被静默当成“未知用户”。

## 6. TDD 测试意图与退出标准

每条先写会红的用例、确认失败原因、再做最小实现；关键保护做隔离变异，确认移除保护后测试真正变红。**不写伪测试凑绿**，不放宽安全检查、不改断言迁就错误。

**切片 1 必须钉住的意图**

- 幂等：同 scope+subject 重复创建返回同一 `request_id`，`PENDING` 行数恒为 1；并发创建下仍为 1（由索引而非应用层判重保证——变异：去掉唯一索引必须转红）。
- 容量：第 1025 个有效 `PENDING` 被拒；满载时分别用“subject 已在 1024 行中”和“全新 subject”调用，必须得到相同的闭集容量错误且不得返回既有申请（反例：先判重再判容量必须转红）。**并且**在真实 PostgreSQL 上验证并发越界：已有 1023 条、两个**不同 subject** 同时提交，最终恰好 1024 条、恰好一个被拒（变异：去掉固定 advisory lock 必须转红）。只测顺序第 1025 条证明不了容量是临界区保证的。
- 过期：超过 24 小时的 `PENDING` 在下一次创建时先被收割为 `EXPIRED`，且新申请拿到新 `request_id`（旧 ID 不复活）。
- CAS：对已终态的申请再次批准/拒绝不改变既有结果，也不产生第二条审计。
- scope：用另一个 tenant 或 environment 携带同一 `request_id` 读取/批准/拒绝时，对调用方统一按不存在处理；申请与目录事实零变化，审批尝试只留一条 `DENIED + SCOPE_MISMATCH` 审计，不泄露正确 scope。
- 并发审批：两个 Admin 同时批准，恰好一个成功、一个得到冲突；账号、角色、绑定各恰好一行。
- 撤权竞态：批准事务锁住当前 Admin 的账号/作用域角色；并发撤销与批准按锁顺序线性化。撤销先提交则批准只能留下 `DENIED`，批准先取得锁则它在该线性化点确实仍是 Admin（变异：把事务内判定退回 service 预读，该用例必须转红）。
- 原子性：审计不可写时整个批准回滚，数据库零行——与 W1a 切片 B 同一条反证形状。
- fake 原子性：在申请已改终态、审计写入前注入失败，快照恢复后申请仍是 `PENDING`，账号/角色/绑定/审计均为零；从快照删掉激活状态时该用例必须转红。
- 不自动授予 Admin：`approved_role=ADMIN` 在契约层被拒。
- 既有角色不被激活改写：无当前 scope 角色时新增 USER/OPERATOR；已有同角色时只补绑定；已有不同角色（尤其 ADMIN）整批冲突，角色、绑定、申请与成功审计均不变（变异：直接复用 `_upsert_role()` 覆盖角色必须转红）。
- 决策授权：普通用户、已停用 Admin、actor 与目录不符、非 `LOCAL_ADMIN` 来源四类反例都必须进入 `apply()`，并由其事务内重读/锁定后拒绝；拒绝发生在任何申请/目录写入之前，service 接住闭集错误后各留一条 `DENIED` 审计。禁止用 service 预读后“不调用 apply”伪装授权——那会把校验重新搬到事务外。正常 ACTIVE Admin 对照必须成功；变异：删掉 store 内重验、只保留 service 预读时，撤权竞态与四类反例至少一条必须转红。
- 单阶段审计：对激活动作调用 `append_started` 被拒（变异：把允许集写回"非目录动作"的补集，该用例必须转红）。
- 审计矩阵：不变量 7 的三行组合逐条钉住，且**在真实 PostgreSQL 上**跑；把任一新动作写成 `STARTED` 必须由契约与数据库同时拒绝（变异：漏换四条 CHECK 中任意一条，真库用例必须转红）。
- 写入口：`ActivationStore` 之外新增 PENDING/EXPIRED 写点，或 `UserDirectoryStore.apply()` 之外新增 APPROVED/REJECTED 写点时，独立 AST 守卫转红；正常创建/收割路径保持绿。
- downgrade：空数据正常降级；仅申请行时默认拒绝、显式授权后可降级并重新升级；存在任一激活动作审计时即使打开破坏性授权也拒绝；所有拒绝路径报告两类精确计数且 revision/数据不变。
- `environment_id`：64 接受、65 拒绝，且异常文本不含原值。

**切片 2 必须钉住的意图**

- 协议改异步后旧行为不变：已登记身份的登录、Session 再认证与群提交三条路径逐条对照（变异：任一调用点漏掉 `await`，该路径必须转红；返回 coroutine 而非主体是这次改造最可能的静默失败形态）。
- 未知身份登录后真实 `/oauth/feishu/callback` 返回 `403 activation_pending`，`rotate_session` 零次调用、无 Session Cookie、Session 表零行、申请恰好一行；普通认证失败仍为 `401 unauthorized`。
- 已绑定但账号停用或当前 scope 角色已撤销时，Web 仍返回普通 `401 unauthorized`，群仍走闭集身份拒绝；两条路径申请数、通知数、Session 数都为零。变异：把 `UserDirectorySubjectUnavailableError` 重新压成 `None` 时两条必须转红，证明撤权不会被误路由成重新激活。
- 激活容量耗尽返回闭集 `503 unavailable`，不得回落成 `500`；已有待办与全新 subject 在满载时逐字同响应，证明没有经 `403 activation_pending` 泄露存在性。OAuth state 仍按既有语义只消费一次、无 Session Cookie；群入口对应统一“暂时无法提交”卡片，申请行数不增加。
- 批准后同一 `open_id` 重新登录成功并拿到 `USER` 权限；批准前重新登录仍然拿不到 Session。
- 拒绝/过期后重新登录重新进入申请流程，且不复用旧 `request_id`。
- 群入口：未知发送者产生申请 + 一张通用卡片；卡片正文不含原始提问、SQL、库名、结果是否存在或 Admin 联系方式（反例：把提问塞进卡片必须转红）；任务不排队、不重放。正常私聊未知发送者对照保持既有 `IDENTITY_UNMAPPED`、零申请、零通知，证明实现没有把共用异常分支误扩成第三种来源。
- 卡片不 @ 具体 Admin、不含链接/按钮，也不因为群里没有 Admin 而报错或泄露目录；申请照常留存。
- 通知失败：单次投递异常不回滚申请创建；同一 event id 重放时传给端口的 `idempotency_ref` 逐字相同；**不 sleep、不做应用层重试、不新建待投递表**。
- 边界否定：全仓不存在"激活即授予结果访问"或"Admin 直读结果"的分支。

**退出标准**

1. 四门在本机与 CI 同时全绿，CI 逐 job 记录结论**与 step 数**；无 step 的 job 记为"未执行"。
2. 真实 PostgreSQL 全量跑通，激活相关用例零 skip。
3. 上述每一条变异都留下先红后绿证据。
4. 真源文档与实现同步：`ARCHITECTURE.md` / `README.md` / `AGENT_HANDOFF.md` 由 `tests/contract/test_doc_fact_binding.py` 守住；`DEVELOPMENT_PLAN.md` 不改（进度与证据只在 handoff）。
5. 证据等级到 `tests` 为止：未接真实飞书、未部署、未 canary、未用户验收。

## 7. 已确认取舍与剩余批准门

- **负责人已确认**：登录 context 表与闭集 return intent 属于 W2；私聊 Admin 通知与可恢复投递重试属于 W3。W1b 只保留群事件当次响应内的通用卡片。
- **批准/拒绝命令进入 `DirectoryCommand`**（不变量 5）：这是让目录、申请终态和审计共用一个事务的结构保证，不另开第二条写路径。
- **通知按最小可实现方案收口为单次幂等发送**：W1b 不复制私有退避函数，也不在 listener callback 里搭建临时重试器；W3 才交付可靠通知。
- **W1b 只把活跃待办做成有界**：终态申请保留/清理仍是 W5 部署硬门，不把 1024 pending 上限夸大为全表 retention 已解决。
- 上述取舍已经收口，但**整份 W1b 计划仍须通过 Claude 精确 SHA 复审并取得负责人最终开工批准**；本次修订不授权编写 W1b 测试、migration 或源码。
- 不变量 7 描述的既有保护是在写本计划时读源码发现的，不是审查意见。若实现时发现 `append_started` 的约束实际另有出处，以源码为准并报告冲突。
- 本轮的复现证据全部来自读源码与真源文档，不是新增测试：这是一份计划，除真源阶段归属那一处冲突外，没有可执行的失败路径可复现。那一处已用 `tests/contract/test_doc_fact_binding.py` 的新守卫先红后绿地钉住。
