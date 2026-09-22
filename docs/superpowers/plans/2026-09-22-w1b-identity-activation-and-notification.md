# W1b 身份激活与通知 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 未登记的飞书身份进入系统时幂等创建激活申请、不签发任何业务 Session；Admin 明确批准或拒绝；批准复用 W1a 的身份目录与审计原子写路径，用户重新登录后才生效；并把待办通知到群聊/私聊。

**Architecture:** 新增 `activation_requests` 一张表与 `ActivationStore`（内存 + PostgreSQL 两实现 + 共享行为套件），审批本身不另开写路径——它是 `UserDirectoryStore.apply()` 判别联合的一个新成员，账号、角色、绑定、申请终态与审计在同一个事务里落地。入口只改两处既有的"身份未知"分支；通知复用既有 `ChannelMessagePort` 与既有退避策略，不新建投递框架。

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
- **W3 用户/职责/审计 UI**：Admin 待办页面、审计查询 API。本阶段的批准/拒绝入口是与切片 C `migrate_static_identities` 同形的模块级一次性命令，不加路由、不加页面。
- R1 结果访问、W4a/W4b/W5、I3。

---

## 1. 当前源码接缝

以下每一处都是既有代码，本阶段只在这些位置接线，不重写它们的正确路径。

| 接缝 | 位置 | 本阶段动作 |
| --- | --- | --- |
| Web 登录未知身份 | `src/xiaowei_agent/interfaces/web_auth.py:490-495`（`identity_missing` 后直接 `raise WebAuthenticationError`） | 改为先幂等创建/复用申请，再抛一个可区分的"待激活"错误；**`rotate_session` 不得被调用** |
| 群内未知发送者 | `src/xiaowei_agent/interfaces/feishu_listener.py:176`（`IDENTITY_UNMAPPED` 拒绝） | 同上，另加通用激活卡片；群任务不排队、不重放 |
| 身份解析来源 | `WebAuthService.__init__` 的 `identities: FeishuIdentityDirectory`（`web_auth.py:306`，协议在 `feishu_identity.py:31`） | 新增一个由 `UserDirectoryStore` 支撑的 adapter 实现同一协议——这是"批准后重新登录生效"唯一需要的改动，登录入口本身不动 |
| 授权写入口 | `UserDirectoryStore.apply()`（`persistence/identity.py:257`），命令联合 `DirectoryCommand`（`contracts/identity.py:245`），动作映射 `ACTION_FOR_COMMAND`（`persistence/identity.py` 同名常量） | 新增一个审批命令作为联合的第九个成员 |
| 审计写面 | `AdminAuditStore`（`persistence/admin_audit.py:113`），动作闭集 `AdminAuditAction`（`contracts/enums.py:112`），目标闭集已含 `ACTIVATION`（`contracts/enums.py:131`） | 新增两个动作成员；`target_kind=ACTIVATION` 首次有生产者 |
| 出站通知 | `ChannelMessagePort`（`application/channel_projection.py:84`）、`FeishuSdkMessageAdapter.send_to_chat/send_to_user`（`interfaces/feishu_sdk.py:560/575`）、群成员适配器（`feishu_sdk.py:609`） | 直接复用，不新建端口 |
| 退避策略 | `ChannelProjectionService._retry_delay`（`application/channel_projection.py:315`） | 复用同一策略计算下次投递时间，不写第二套退避 |
| 受控 PII 上限 | `CONTROLLED_PII_MAX_LENGTH`（`contracts/base.py`）、`ControlledPiiField`（`contracts/identity.py:50`） | 直接复用；`subject_ref` 不得再出现第二份上限 |
| 迁移头 | `rev_0014_identity_admin_audit.py` | 新增 `rev_0015_activation_requests` |

**schema 门的实际范围**：ADR-014 §R3 冻结的是 **OAuth state 域**（`web_oauth_states`，只放开 `web_oauth_login_contexts` 一张表），不是全仓 schema 冻结——W1a 的 `rev_0014` 已经新建四张表而无需再修订 ADR。`activation_requests` 不属于 OAuth state 域，因此不触发该门。实现前仍须自行复核这一判断；若判断被推翻，先停下改 ADR，不得自行加表。

## 2. environment_id 边界

`Settings.environment_id` 当前是无上限的 `StrictStr`（`config.py:235`），而目录契约的 `BoundedId` 是 64（`contracts/identity.py:32`）。**本阶段在配置入口收紧到 64：64 字符接受、65 字符拒绝**，拒绝发生在 `Settings` 构造时，且沿用 `hide_input_in_errors=True`，不把环境变量原值回填进异常。

两端闭合证据（无反对意见）：仓库内全部真实取值为 `dev`（`docker-compose.yml:2`、`.env.example:7`）、`test`（`docker-compose.m6b-test.yml:4`）、`staging`，最长 7 字符；收紧到 64 不挡任何现有配置，也不改变任何现有进程的启动结果。这同时关掉 W1a 切片 C 复审留下的最后一处口径分歧：同一个 `environment_id` 从此只有一个上限。

`tenant_id` 不在本阶段改动：它不是可由环境覆盖的 `Settings` 字段。

## 3. 状态机与关键不变量

```text
（无申请）──创建──▶ PENDING ──CAS 批准──▶ APPROVED
                      │ └──CAS 拒绝──▶ REJECTED
                      └──同临界区收割──▶ EXPIRED
```

终态三个，均不可再迁移；重放审批不改变既有结果。

1. **未激活不签发 Session。** 创建申请的两条入口路径上都不存在通向 `rotate_session` 的分支。
2. **同 tenant+environment+provider+subject 至多一个有效 `PENDING`。** 由**部分唯一索引**保证，不是每个调用方自己记得先查一遍。
3. **全库有效 `PENDING` ≤ 1024**，到达容量 fail-closed，返回与"已存在申请"完全相同的统一文案，不泄露 subject 是否已存在。
4. **过期是收割，不是后台任务。** 24 小时过期项在创建/复用的同一串行化临界区内先转 `EXPIRED`，再判重与判容量。
5. **审批不跨事务。** 账号、作用域角色、外部身份绑定、申请终态与审计事件在 `UserDirectoryStore.apply()` 的同一个事务里提交；不出现"账号已建、身份未绑"或"身份已绑、角色未建"。
   *被拒绝的备选*：让 `ActivationStore` 自己写目录表——那是第二条授权写路径，直接推翻 W1a 的唯一写入口不变量。
6. **不自动授予 Admin**（见 Global Constraints），且 `approved_role` 由审批命令携带、默认 `USER`，提升为 `OPERATOR` 必须显式且可审计。
7. **激活审批仍是单阶段审计。** 新增的两个动作只产生 `SUCCEEDED` / `DENIED`。当前 `AdminAuditStart` 靠"拒绝目录动作"实现单阶段约束（`persistence/admin_audit.py` 注释明确说 `DIRECTORY_ACTIONS` 恰好等于全部八个动作），一旦闭集多出两个非目录动作，两阶段入口会**自动对它们敞开**。实现时必须把 `append_started` 的允许集改写成"配置类两阶段动作"的显式闭集，而不是"非目录动作"的补集。这是本阶段唯一一处会被新成员静默放宽的既有保护。
8. **通知失败不回滚审批，也不回滚申请创建。** 投递是审批之后的独立可重试事实；重试复用既有退避与 `idempotency_ref`，同一申请不产生第二张卡片。
9. **申请不保存正文。** 无原始提问、SQL、结果行、Secret、任意 URL、群消息原文；事件与会话引用只存 domain-separated 摘要；`subject_ref` 是 `ControlledPiiField`，日志与列表只显示脱敏标识。
10. **群里没有可 @ 的 Admin 不算错误**：不报错、不泄露目录，申请照常留存。

## 4. 最小文件范围

**切片 1｜激活事实与审批写路径**

- 新建 `src/xiaowei_agent/contracts/activation.py`：`ActivationRequest`、状态/来源闭集、创建与审批命令。
- 修改 `src/xiaowei_agent/contracts/enums.py`：`AdminAuditAction` 增加激活批准/拒绝两个成员。
- 修改 `src/xiaowei_agent/contracts/identity.py`、`contracts/__init__.py`：审批命令进入 `DirectoryCommand`。
- 新建 `src/xiaowei_agent/persistence/activation.py`：`ActivationStore` 协议与错误类型。
- 修改 `src/xiaowei_agent/persistence/schema.py`、新建 `migrations/versions/rev_0015_activation_requests.py`。
- 修改 `src/xiaowei_agent/persistence/fake.py`、`postgres.py`：两个实现 + `apply()` 承载审批命令。
- 修改 `src/xiaowei_agent/persistence/admin_audit.py`：收窄 `append_started` 的允许集（不变量 7）。
- 修改 `src/xiaowei_agent/config.py`：`environment_id` 上限 64。
- 测试：`tests/contract/test_activation_contracts.py`、`tests/contract/test_activation_store_suite.py`（共享行为套件，两实现各跑一遍）、`tests/contract/test_config.py`（边界）、既有 `tests/security/test_module_layering.py` 登记新文件。

**切片 2｜入口接线与通知**

- 新建 `src/xiaowei_agent/application/identity_activation.py`：创建/复用、批准、拒绝的应用服务，以及与 `migrate_static_identities` 同形的一次性批准/拒绝命令。
- 新建 `src/xiaowei_agent/interfaces/directory_identity.py`：`UserDirectoryStore` 支撑的 `FeishuIdentityDirectory` adapter。
- 修改 `src/xiaowei_agent/interfaces/web_auth.py`、`interfaces/feishu_listener.py`：两处未知身份分支接线。
- 新建 `src/xiaowei_agent/application/activation_notification.py`：卡片渲染与投递重试。
- 修改 `src/xiaowei_agent/interfaces/local_stack.py`：装配。
- 测试：`tests/contract/test_identity_activation_service.py`、`tests/contract/test_activation_notification.py`、`tests/security/test_activation_boundaries.py`，并更新既有 `tests/security/test_module_layering.py`。

两个切片依赖单向、各自独立可复审可合入。若切片 1 复审暴露形状问题，切片 2 不得先行。

## 5. TDD 测试意图与退出标准

每条先写会红的用例、确认失败原因、再做最小实现；关键保护做隔离变异，确认移除保护后测试真正变红。**不写伪测试凑绿**，不放宽安全检查、不改断言迁就错误。

**切片 1 必须钉住的意图**

- 幂等：同 scope+subject 重复创建返回同一 `request_id`，`PENDING` 行数恒为 1；并发创建下仍为 1（由索引而非应用层判重保证——变异：去掉唯一索引必须转红）。
- 容量：第 1025 个有效 `PENDING` 被拒；拒绝文案与"已存在申请"逐字相同（反例：两条文案分叉必须转红）。
- 过期：超过 24 小时的 `PENDING` 在下一次创建时先被收割为 `EXPIRED`，且新申请拿到新 `request_id`（旧 ID 不复活）。
- CAS：对已终态的申请再次批准/拒绝不改变既有结果，也不产生第二条审计。
- 并发审批：两个 Admin 同时批准，恰好一个成功、一个得到冲突；账号、角色、绑定各恰好一行。
- 原子性：审计不可写时整个批准回滚，数据库零行——与 W1a 切片 B 同一条反证形状。
- 不自动授予 Admin：`approved_role=ADMIN` 在契约层被拒。
- 单阶段审计：对激活动作调用 `append_started` 被拒（变异：把允许集写回"非目录动作"的补集，该用例必须转红）。
- `environment_id`：64 接受、65 拒绝，且异常文本不含原值。

**切片 2 必须钉住的意图**

- 未知身份登录后 `rotate_session` 零次调用、Session 表零行、申请恰好一行（变异：让未知分支照常签发 Session，必须转红）。
- 批准后同一 `open_id` 重新登录成功并拿到 `USER` 权限；批准前重新登录仍然拿不到 Session。
- 拒绝/过期后重新登录重新进入申请流程，且不复用旧 `request_id`。
- 群入口：未知发送者产生申请 + 一张通用卡片；卡片正文不含原始提问、SQL、库名、结果是否存在或 Admin 联系方式（反例：把提问塞进卡片必须转红）；任务不排队、不重放。
- 群内无可 @ Admin：不抛错、不泄露目录、申请照常留存。
- 通知失败：投递异常不回滚审批，重试按既有退避执行，同一申请只产生一张卡片。
- 边界否定：全仓不存在"激活即授予结果访问"或"Admin 直读结果"的分支。

**退出标准**

1. 四门在本机与 CI 同时全绿，CI 逐 job 记录结论**与 step 数**；无 step 的 job 记为"未执行"。
2. 真实 PostgreSQL 全量跑通，激活相关用例零 skip。
3. 上述每一条变异都留下先红后绿证据。
4. 真源文档与实现同步：`ARCHITECTURE.md` / `README.md` / `AGENT_HANDOFF.md` 由 `tests/contract/test_doc_fact_binding.py` 守住；`DEVELOPMENT_PLAN.md` 不改（进度与证据只在 handoff）。
5. 证据等级到 `tests` 为止：未接真实飞书、未部署、未 canary、未用户验收。

## 6. 本计划不能替代的判断

- 审批命令进入 `DirectoryCommand` 是本计划的默认选择（不变量 5）。它把 `apply()` 的职责从"四类授权事实"扩到"授权事实 + 申请终态"。复审若认为这是越权扩张，替代方案是让 `ActivationStore` 与 `UserDirectoryStore` 共享同一事务边界——代价是多一条需要自证不会半写的路径。**这一点需要在切片 1 开工前拍板。**
- 不变量 7 描述的既有保护是在写本计划时读源码发现的，不是审查意见。若实现时发现 `append_started` 的约束实际另有出处，以源码为准并报告冲突。
