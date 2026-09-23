# W2 登录与多 shell 实施计划

> 状态：Review Draft V0.2。本文尚未获项目负责人和独立审查者批准；批准前不写 W2 产品测试、迁移或源码。V0.2 按首轮独立审核收口闭集扩展、角色×意图终态、飞书 Admin 脱敏投影与详情路径单真源。

**Goal:** 把现有 Web 入口收敛成成熟的 `/login`、`/app`、`/admin` 与只读任务详情四个壳；用一次性 OAuth state 绑定闭集 return intent；让 Admin、运维人员和普通用户只进入各自获准页面，同时保持 W1b 的“未知身份不签 Session、只创建激活申请”边界。

**Architecture:** 继续使用同一个 FastAPI Web 进程、digest-only Session、数据库身份目录、现有 Runtime/TaskStore/RenderPayload 和原生 HTML/CSS/ES module。新增且只新增 `web_oauth_login_contexts` 一张表，以 `state_digest` 绑定闭集导航意图；连接测试仍只写原 OAuth state。W2 不创建第二套业务路由、身份服务、前端框架或配置存储。

**Spec:** [Web 运维工作台、身份激活与未来结果访问边界总体设计](../specs/2026-09-19-web-operations-console-identity-activation-design.md) §5、§7.1–7.2、§9、§16、§17.1；[ADR-014](../../adr/ADR-014-real-feishu-oauth-and-web-activation.md) R3；[ADR-013](../../adr/ADR-013-m7-channel-boundary.md) 管理面权限边界。

**基线:** `main@2671a50c295085472fe5100f5716313e785993c5`（PR #70 已合入）。PR #70 只调整协作/文档真源与文档契约，没有修改 Web、身份、Session、激活、迁移或静态资源源码，因此 W1a/W1b 前置交付仍兼容；旧 handoff 的 `main@6e792755...` 与“流程待集成”已经过期。

---

## 0. 进入门与证据边界

### 已满足

- W1a 的用户目录、作用域角色、外部身份、本地 Admin 链接和 Admin 审计写内核已在 main。
- W1b 的 `activation_requests`、申请容量/CAS、数据库身份解析、未知 OAuth `activation_pending`、无 Session 和群内单次通用通知已在 main。
- ADR-014 R3 已精确允许 `web_oauth_login_contexts`，且禁止向 `web_oauth_states` 增列或增加第二张 OAuth context 表。
- 当前登录与连接测试已有不同 digest domain；连接测试成功不签 Session、不查身份目录、不建激活申请。
- 当前 Web 的 Cookie、Origin、CSRF、CSP、HSTS/no-store、body limit、静态资源闭集和只读详情 shell 已有测试基线。

### 尚未满足

- 仓库在本计划前没有 W2 详细计划，也没有“W2 计划已批准”的 handoff 记录。
- 本计划尚未经过独立精确 SHA 审查与项目负责人批准，因此不能开始实现。
- 真实飞书应用/凭据/网络、部署、canary 和用户验收均未授权；W2 只允许 fake port、隔离 PostgreSQL 与本地浏览器检查。

### 开工条件

1. 本计划以精确 SHA 独立审查，无未解决 P0/P1；涉及真源冲突的判断在批准记录中明确接受。
2. 计划与 handoff 基线合入最新 main，并记录“W2 详细计划已批准”。
3. 从届时最新 main 建实现分支。批准范围内按三个切片连续推进，不逐文件重复申请；新增范围、真实调用、合并、部署和 UAT 仍分别等待授权。

---

## 1. 范围、非目标与阶段解释

### 本阶段交付

1. 独立 `/login`：固定用户名 `admin` 的本地登录、飞书登录、强制改密、闭集激活状态。
2. `/app`：只服务 `ADMIN` / `OPERATOR` 的现有运维工作台；继续把所有输入提交到既有 Web 任务入口，由统一 Runtime 决定普通对话、澄清或 capability，不在前端按关键词分流。
3. `/admin`：只服务 `ADMIN` 的独立管理壳。本地 Admin 使用现有 RI5 AI/飞书配置表单和测试 API；飞书 Admin 只读 ADR-007 规定的独立脱敏状态投影，并看到“需使用本地管理员账号继续”。W2 不虚构尚未交付的用户、职责、审计或资源页面。
4. `/app/tasks/{task_id}`：保持只读详情 shell；`USER` 只可经具体链接进入，最终内容仍由现有任务 ACL 决定。
5. `web_oauth_login_contexts`、闭集 return intent、W2 所需的 `ActivationRequest.return_intent` 与 `SAFE_TASK_LINK` 来源。
6. 所有页面和 API 的服务端角色/来源判定；前端隐藏入口只改善体验，不承担授权。

### 两处已批准真源的解释

- **用户名不落第二份账号真源。** W1a 已批准计划与产品规格都规定首版用户名固定为 `admin`；`LocalCredential` 是规格名，不是当前源码类型。W2 在请求契约、服务和 UI 中校验固定用户名，不给 `local_admins` 增 `username` 列，也不支持第二个本地账号。这是已核对结论，不再作为实现期真源冲突重开。
- **配置 UI 与配置存储分阶段。** `DEVELOPMENT_PLAN.md` 是阶段归属真源：W2 把现有 RI5 表单/API 从工作台搬到 `/admin`；`.config/integrations.json`、Provider DTO、探针语义和 Compose 挂载保持不变。规格 §17.1 的 W4a 条目同步收窄为“迁移 W2 已放入 Admin shell 的配置存储与挂载”，W4a 仍独占三域文件、迁移、挂载矩阵与 `migration_required`。W2 不提前实现其中任何一项。

### 非目标

- W3 的激活待办审批 UI、用户/角色/职责管理、Admin 审计查询、激活通知人、私聊通知和可靠重试。
- W4a 的三域配置，W4b 的数据库/Prometheus 参数，W5 的 release/限流/部署证据。
- W4c 真实探针、R1 `/results/{result_ref}`、预览/导出与 requester/approver ACL。
- 普通用户工作台、任务列表、任务提交、“我的结果”或移动端后台。
- 新聊天 API、关键词路由、第二个 Runtime、React/Vue、打包器、微服务或任意 redirect URL。
- 真实飞书/模型/运维目标调用、真实凭据读取、部署、canary 或 UAT。

---

## 2. 当前接缝与改动落点

| 现有接缝 | 当前事实 | W2 动作 |
| --- | --- | --- |
| `persistence/web_session.py::WebSessionStore` | 登录与连接测试共用 `issue_oauth_state/consume_oauth_state`，靠 digest domain 隔离 | 增加**登录专用**原子签发/消费方法；现有方法继续只承载连接测试，不加可空 intent 参数 |
| `schema.py::WEB_OAUTH_STATES` | 一次性 state，不含业务列 | 列不变；新增 `WEB_OAUTH_LOGIN_CONTEXTS`，PK/FK 为 `state_digest`，删除 state 时级联清理 |
| `web_auth.py::WebAuthService` | `start_login()` 无意图；`complete_login()` 消费 state 后总回 `/app` | 登录签发必须带闭集 intent；消费返回同一 intent；目标角色不允许时在轮换 Session **之前**拒绝 |
| `web_auth.py::WebActivationPendingError` | 不携带申请引用，路由只能回 JSON 403 | 只携带随机 `request_id` 和闭集状态所需事实，不携带 subject、原目标正文或角色 |
| `contracts/activation.py` / `rev_0015` | W1b 来源只有 `WEB_LOGIN/FEISHU_GROUP`，且 source↔摘要与 status↔decision 都各有一份手写组合矩阵 | W2 增 `SAFE_TASK_LINK` 时同时更新契约、runtime schema 与新 revision；用全集性测试证明两份组合矩阵分别覆盖 `ActivationSource` / `ActivationStatus` 全集 |
| `IdentityActivationService.request_web()` | 只收 subject | 收已验证 return intent；安全任务深链派生 `SAFE_TASK_LINK`，其余 Web 登录为 `WEB_LOGIN` |
| `DirectoryFeishuIdentityDirectory` | 每次查库后只返回 `AuthenticatedPrincipal`，丢掉 `ProductRole` | 提供 Web 专用的窄解析结果 `principal + current role`；listener 继续消费原协议，不把角色塞入跨渠道 principal |
| `AuthenticatedWebSession` / `_WebPrincipalSession` | 只有 principal、CSRF、改密状态 | 每次请求从当前目录重算 role 与 `AdminCapability`；cookie/session 表不缓存角色或能力 |
| `LocalAdminAuthService` | 只收 password；本地主体是固定 ADMIN | `login()` 同时收固定 username，非 `admin` 与错密码走同一闭集错误；认证后合成 `ProductRole.ADMIN + IdentitySource.LOCAL_ADMIN`，不查飞书目录、不新增账号表或用户名列 |
| `_config_view()` / `WebConfigView` | raw 配置读 DTO 含 App ID、generation 与配置/测试细节，只允许本地 Admin | 另建 `WebIntegrationStatusView` 与只读 API，字段精确服从 ADR-007；不得复用 `WebConfigView` 再靠前端删字段 |
| `web_models.py::web_task_detail_path()` | 当前详情 API 的唯一同源路径生产者，六个模型构造/校验调用它 | `web_task_detail_path()` 从 `web_models.py` 移到 `interfaces/web_navigation.py`；`web_models.py` 只导入并复用该函数，必要时只做兼容 re-export，不保留第二个路径字面量 |
| `web_app.py` | `/app` 条件渲染登录/改密/工作台；config API 在 `/app/api/config*` | 注册 `/login`、`/app`、`/admin` 三个独立壳；B1 先建立登录/角色/壳，B2 再移动现有 config UI/API 到 `/admin`；更新 body-limit 与路由闭集 |
| `web_static/index.html/app.js` | 工作台与配置面混在一个 shell；桌面下界 1280 | 工作台删除配置代码并支持 1024+；新增 login/admin 资源；详情 shell 不动提交边界 |
| `WebCurrentUser` | 只返回 actor/environment/渠道权限 | 增当前 `ProductRole` 与派生的管理能力闭集；前端仅用于导航，API 仍独立授权 |
| 迁移头 | 文件 `rev_0015_activation_requests.py` / revision id `0015_activation_requests` | 新增文件 `rev_0016_web_login_contexts.py` / revision id `0016_web_login_contexts`，同步 runtime schema、row mapping、迁移路径与降级保护 |

新增跨层类型只允许一个必要落点：`contracts/web_navigation.py` 定义闭集 `WebReturnIntent`。URL/path 重建放在 `interfaces/web_navigation.py`；契约层和数据库都不得出现任意 URL/path 字段。

---

## 3. 状态模型与不可破坏不变量

### 3.1 Return intent

闭集精确为：

- `WORKBENCH`
- `SAFE_TASK_DETAIL(task_id)`
- `ADMIN_CENTER`
- `ACTIVATION_STATUS(request_id)`

单一契约使用 `kind + task_id? + request_id?` 并做双向组合校验：详情仅允许 task ID，激活状态仅允许 request ID，其余两种不允许引用。所有 ID 使用既有有界 ID 契约。数据库 context 表用同样的 kind/nullable 列 CHECK；不存 path、host、scheme、query、fragment 或 `next` 字符串。

`web_return_path(intent)` 是意图到页面路径的唯一分派点，但不复制详情路径算法：`SAFE_TASK_DETAIL` 必须调用同模块的 `web_task_detail_path()`。实现时把 `web_task_detail_path()` 从 `web_models.py` 移到 `interfaces/web_navigation.py`，`web_models.py` 只导入并复用该函数；这样 `/app/tasks/{quote(task_id, safe='')}` 只有一个生产者。其余重建值为 `/app`、`/admin` 或 `/login` 的激活状态视图。两者都不接收 base URL，也不读取 `Forwarded`/`Host` 来生成跳转。

- `/login` 与本地密码 POST 只传同一个结构化契约，不传 redirect 字符串；本地登录没有 OAuth state，因此不写 context 表，但仍必须由服务端重建目标并执行同一角色门。
- 匿名访问 `/app`、`/admin` 或任务详情时，服务端只把对应闭集 intent 带到 `/login`；畸形 kind、额外 ID、重复 query 参数或任意 `next` 一律拒绝，不降级成猜测的默认路径。
- `ACTIVATION_STATUS` 是 OAuth 激活恢复意图，不是本地 Admin 查看申请的旁路；本地密码登录不得借该 intent 读取申请事实。服务端不注册匿名 request_id 状态查询：request ID 只能进入一次新的 OAuth 登录 context，回调验证 OAuth subject 与申请 subject 相等后才可读取固定四态并恢复原 intent；仅持 URL/request ID 得不到申请状态。

### 3.2 登录 state 与 context

```text
登录开始
  └─ 同一事务：INSERT oauth_state + INSERT login_context
连接测试开始
  └─ INSERT oauth_state；context 行恒为 0

登录回调
  └─ 同一事务：原子消费未过期 state + 读取唯一 context
     缺 context / 多行不可能 / kind-field 错配 → 整体回滚并按 invalid state 拒绝
```

- 不给现有通用 `issue_oauth_state()` 加 `intent: Optional[...]`；这种签名允许登录漏 context、测试误带 context。
- 登录与测试继续用不同 digest domain，同一 callback 的“登录域优先、仅 state 不匹配才回落测试域”顺序不变。缺登录 context 是登录域内部不变量破坏，使用独立分类错误直接返回 400，不回落连接测试域。
- state 收割删除 context，不能留下孤儿；context 不能独立创建、更新、加载或复用。
- 容量仍按未消费/未过期 state 计算，不另设第二个额度。
- `rev_0016` upgrade 在建新表前清空现有 `web_oauth_states`：这些票据 TTL 不超过 600 秒，删除只要求迁移窗口内尚未完成的登录/连接测试重新发起，避免旧 state 没有 context 时被误分类。该删除写入 migration 测试与发布说明，不能静默发生。

### 3.3 角色、壳与 Session

#### 服务端终态矩阵

表中的 `destination_not_available` 是登录壳固定视图状态，不是客户端可传错误码：服务端返回 403、不签 Session、不自动改投其他页面。普通用户文案为“当前账号不能进入此页面，请从小维发送的具体结果链接进入”；Operator 请求 Admin 中心时只显示“当前账号不能进入管理中心”。两者都不暴露角色映射、申请、任务或资源是否存在。

| 主体 | return intent | Session 终态 | 页面/结果终态 |
| --- | --- | --- | --- |
| `LOCAL_ADMIN` | `WORKBENCH` | `issue_session` | `/app` |
| `LOCAL_ADMIN` | `SAFE_TASK_DETAIL` | `issue_session` | `task_acl` |
| `LOCAL_ADMIN` | `ADMIN_CENTER` | `issue_session` | `/admin_local` |
| `LOCAL_ADMIN` | `ACTIVATION_STATUS` | `deny_no_session` | `forbidden` |
| `FEISHU_ADMIN` | `WORKBENCH` | `issue_session` | `/app` |
| `FEISHU_ADMIN` | `SAFE_TASK_DETAIL` | `issue_session` | `task_acl` |
| `FEISHU_ADMIN` | `ADMIN_CENTER` | `issue_session` | `/admin_redacted` |
| `FEISHU_ADMIN` | `ACTIVATION_STATUS` | `subject_bound` | `restore_original_intent` |
| `FEISHU_OPERATOR` | `WORKBENCH` | `issue_session` | `/app` |
| `FEISHU_OPERATOR` | `SAFE_TASK_DETAIL` | `issue_session` | `task_acl` |
| `FEISHU_OPERATOR` | `ADMIN_CENTER` | `deny_no_session` | `destination_not_available` |
| `FEISHU_OPERATOR` | `ACTIVATION_STATUS` | `subject_bound` | `restore_original_intent` |
| `FEISHU_USER` | `WORKBENCH` | `deny_no_session` | `destination_not_available` |
| `FEISHU_USER` | `SAFE_TASK_DETAIL` | `issue_session` | `task_acl` |
| `FEISHU_USER` | `ADMIN_CENTER` | `deny_no_session` | `destination_not_available` |
| `FEISHU_USER` | `ACTIVATION_STATUS` | `subject_bound` | `restore_original_intent` |

`subject_bound` 不是已签 Session：回调必须先证明 OAuth subject 与申请 subject 相同，再取原 intent 并重新执行同一张表；只有最终行是 `issue_session` 才轮换 Session。引用不存在、主体不匹配、申请未批准或恢复后的角色不允许原 intent，均不签 Session。

#### 管理中心投影

- 本地 Admin 明确合成 `ProductRole.ADMIN + IdentitySource.LOCAL_ADMIN`；不查飞书目录。它获得完整 `AdminCapability`，但任务详情仍过既有 ACL，真实连接测试仍过已有 flag/现场 GO。
- 飞书 Admin 只因 `VIEW_INTEGRATION_STATUS` 进入 `/admin`。W2 新增独立 `WebIntegrationStatusView`，字段精确限于域名 `ai/feishu/resources`、`configured`、`restart_required`、加载结果闭集码、最近测试结果闭集码与时间；不得复用 `WebConfigView` 后在前端删字段。
- 该投影不含 generation、App ID、Secret 的任何形态、原始配置值、路径、endpoint、账号标识、资源列表、错误码正文或异常正文。`resources` 尚未交付时只能显示未配置/未接入的闭集状态，不能伪造加载或测试成功。
- `/admin` 对飞书 Admin 显示脱敏概览，并在 AI/飞书配置区域显示“需使用本地管理员账号继续”。raw config GET/PUT/clear 和三个测试 POST 继续只接受 `IdentitySource.LOCAL_ADMIN`；飞书 Admin 调用均为 403，且不能靠已有 `ADMIN_ALL_SAFE_TASKS` 提权。

- role 与 `AdminCapability` 每次受保护请求从数据库目录重算，不写入 cookie/session，不从 `ChannelPermission` 反推。
- OAuth 回调在签 Session 前按上表检查 intent 与当前角色，不做“拒绝后自动回落 `/app`”的隐式导航。
- `/app/api/tasks` 列表和提交与 `/app` 使用同一个 Admin/Operator 服务端门；`USER` 不能绕过隐藏导航直调。`/app/api/tasks/{task_id}` 保留现有 owner/群/管理员任务 ACL。
- 初始本地 Admin 改密前仍只允许登录、改密和退出；不能访问任一壳、任务或配置 API。

### 3.4 激活状态与原目标

- 未登记 OAuth 身份：消费登录 state、验证 code 后创建/复用申请；不签 Session。申请保存**原始闭集 intent**，当前回调只用异常携带的随机 request ID 渲染一次固定 `pending` 结果；后续查看或恢复必须重新发起带 `ACTIVATION_STATUS(request_id)` 的 OAuth，服务端不得仅凭 request ID 查询 `ActivationStore`。
- `SAFE_TASK_DETAIL` 进入的申请使用 `ActivationSource.SAFE_TASK_LINK`；普通工作台/Admin/激活状态重新申请使用 `WEB_LOGIN`。两者都不保存 URL、任务正文或“目标是否存在”。
- 既有 W1b 行迁移：`WEB_LOGIN` 回填 `WORKBENCH`，`FEISHU_GROUP` 保持无 return intent；新契约以 source/intent 双向校验。不能把群申请伪装成 Web intent。
- `/login` 的激活状态只显示 `pending/approved/rejected/expired` 对应的固定文案，不返回 subject、角色、Admin 名单、task ID、结果存在性或审批人。
- 从 `ACTIVATION_STATUS(request_id)` 再次飞书登录时，OAuth subject 必须与申请 subject 精确相等。已批准则恢复申请中保存的原 intent；仍 pending 就复用原申请；rejected/expired 可按原 intent 新建申请。引用不存在或主体不匹配统一 fail-closed，不签 Session。
- 任何待激活/拒绝/过期页面都不是登录凭证，也不新增普通用户门户。

### 3.5 迁移与恢复

- 文件 `rev_0016_web_login_contexts.py` / revision id `0016_web_login_contexts` 新建 context 表，并给 `activation_requests` 增受 CHECK 约束的 intent 列/来源成员；runtime schema 与 migration DDL 集合相等。
- upgrade 必须兼容已有 W1b 行：Web 登录行回填 WORKBENCH，群行保持空 intent；再设约束，不假定表为空。历史 `rev_0015` 不原地改写：`rev_0016` 用 `DROP CONSTRAINT` + `ADD CONSTRAINT` 替换 `ck_activation_requests_source_digests_match` 和受新列影响的约束。
- source↔digest 映射必须是显式总函数：`WEB_LOGIN`、`SAFE_TASK_LINK` 都要求 event/chat digest 为 NULL，`FEISHU_GROUP` 要求两者非 NULL；`SAFE_TASK_LINK` + 任一群摘要必须拒绝。契约测试断言 source/digest 映射键集合与 `ActivationSource 全集相等`，共享 `activation_store` 套件让 memory/PostgreSQL 跑同一正反例。
- status↔decision 映射也做同类全集守卫：映射键集合与 `ActivationStatus 全集相等`。W2 不新增状态，但这条防止下次只扩枚举、漏改 Pydantic/DDL 的同根因重演。
- `tests/contract/test_schema_matches_migration.py` 把 `ACTIVATION_REQUESTS` 加入 `_ALTERED_AFTER_CREATION`；head 检查继续逐列、逐约束验证，不能因跳过历史 CREATE TABLE 比对而失去覆盖。
- downgrade 不得把 `SAFE_TASK_LINK` 或非 WORKBENCH intent 静默改成 WORKBENCH，也不得丢活跃 context。存在 W2-only 事实时用分类计数拒绝；空库/仅可无损还原的 W1b 形状才允许正常降级。若最终设计需要显式破坏性授权，必须在切片 A 审查中逐类写明被删除事实并用真库测试，不能沿用一个布尔总开关吞掉差异。
- 所有主动降级测试 `try/finally` 升回 head，并整文件运行，避免污染 session 级 schema fixture。

---

## 4. 实施切片

### W2-A：导航契约、原子登录 context 与激活意图

**可独立验收结果：** 存储层能够原子签发/消费“登录 state + 闭集 intent”，连接测试证明零 context；ActivationRequest 能无损表达 W2 深链来源。该切片不注册新页面或路由，运行时产品行为不变。

**预计文件范围：**

- `src/xiaowei_agent/contracts/{enums.py,activation.py,__init__.py}`
- `src/xiaowei_agent/contracts/web_navigation.py`（新）
- `src/xiaowei_agent/persistence/{web_session.py,schema.py,rows.py,memory.py,fake.py,postgres.py}`
- `src/xiaowei_agent/_conformance.py`
- `src/xiaowei_agent/persistence/migrations/versions/rev_0016_web_login_contexts.py`（新）
- `src/xiaowei_agent/application/identity_activation.py`
- 对应 contracts/shared suites/integration/migration/schema/row/conformance/security tests；含 `tests/contract/test_schema_matches_migration.py`

**TDD 顺序：**

1. 先红：return intent 四个正例、额外/缺失引用、外部 URL/路径/控制字符/超长 ID 反例。
2. 先红：memory 与 PostgreSQL 共享套件证明登录签发/消费原子性、重放/过期、缺 context 回滚且不回落测试域、连接测试零 context、收割无孤儿；migration upgrade 清空旧 state 后登录/测试都只能重新发起。
3. 先红：ActivationRequest 的 source/intent 组合、source/digest 与 status/decision 两张映射的枚举全集性、既有行升级、深链来源、跨 scope 与 subject 不串联。
4. 先红：真实 PostgreSQL migration upgrade/downgrade、DDL/runtime schema 一致、主动降级恢复 head。
5. 最小实现；两种 Store 继续跑同一套件，公开 Protocol 表面与 `_conformance.py` 同步。

**承重反证：** 临时撤掉“登录必须有 context”、把 context 改成可空、让测试 state 走登录方法、或删除 source/intent CHECK，各自至少一条目标用例因预期原因转红。

### W2-B1：登录、角色路由与四个独立 shell

**可独立验收结果：** 本地 Admin、飞书 Admin/Operator/User、未知身份分别走完整正式入口；工作台、Admin 和详情后端权限与 UI 一致；飞书 Admin 只能读取脱敏集成状态。现有 config API 与工作台配置区域暂留原路径，但 B1 过渡期仍只向 `IdentitySource.LOCAL_ADMIN` 渲染并授权，飞书 Admin/Operator/User 既拿不到 raw DTO，也看不到可操作表单；B2 随后完成最终位置收口。这样避免把高风险登录/角色逻辑和大面积机械搬迁塞进一个 PR，同时不制造临时越权或 UI 假入口。

**预计文件范围：**

- `src/xiaowei_agent/interfaces/{directory_identity.py,feishu_identity.py,web_auth.py,web_navigation.py,local_admin_auth.py,web_models.py,web_app.py,local_stack.py}`
- `src/xiaowei_agent/interfaces/web_static/{login.html,login.js,index.html,app.js,admin.html,admin.js,detail.html,detail.js,app.css}`（只改登录/路由/壳所需者，配置搬迁留给 B2）
- Web auth/routes/task/static/XSS/authority/assembly 契约与安全测试；脱敏 integration status DTO/API 的逐字段安全测试
- wheel/静态资源闭集测试；仅当实测 wheel 漏资源时才改 `pyproject.toml`

**TDD 顺序：**

1. 固定 username `admin`：正确账号/口令成功；任一错误统一失败；无 username 列、无第二账号入口。
2. Web 专用身份解析：三角色与本地 Admin 的 role/capability；本地 Admin 合成来源/角色；停用/撤权即时生效；Session 不保存角色。
3. OAuth：四种 intent 签发/回调、十六格服务端终态矩阵、角色不允许时零 Session、unknown→申请→受主体约束的激活状态、批准后恢复原 intent；连接测试零 context/Session/身份解析/申请。
4. 路由：匿名重定向到 `/login`；Admin/Operator/User 正反例；列表/提交后端门；具体任务继续走现有 ACL；详情路径只有 `web_navigation.py` 一个生产者。
5. 管理中心：本地 Admin 与飞书 Admin 都能进入；飞书 Admin 只读 `WebIntegrationStatusView`，raw config/保存/清除/测试逐项 403；响应逐字段证明无 Secret、原始 DTO、App ID、generation、路径、endpoint、账号与错误正文。
6. 壳与静态资源：每个 shell 只加载自己的 JS；无 inline script/handler、`innerHTML`、Secret/外部正文插值；详情 shell 继续无 textarea/提交 JS/CSRF token。
7. 1024/1280/1440 工作台/Admin 视觉检查；手机宽度只检查登录、改密、激活状态和任务详情。

**承重反证：** 至少逐项验证：撤掉 USER 工作台门、撤掉 Operator Admin 门、让 callback 使用任意 `next`、给 pending 分支签 Session、让飞书 Admin 读取 raw config、或让详情 shell 加载工作台 JS时，对应测试转红。

### W2-B2：配置 UI/API 迁入 Admin shell

**可独立验收结果：** 现有 RI5 配置表单、保存/清除与三个连接测试入口从工作台机械迁到本地 Admin 区域；旧路径关闭，存储文件、DTO、探针、真实调用门和 Compose 挂载零变化。

**预计文件范围：**

- `src/xiaowei_agent/interfaces/{web_models.py,web_app.py}`
- `src/xiaowei_agent/interfaces/web_static/{index.html,app.js,admin.html,admin.js,app.css}`
- 既有 config route/body-limit/Origin/CSRF/static/XSS/RI5 assembly tests，以及路由闭集与 wheel 资源测试
- `ARCHITECTURE.md`、`README.md`、`AGENT_HANDOFF.md` 只同步三个切片已实现并验证的事实；`DEVELOPMENT_PLAN.md` 不写进度

**TDD 顺序：**

1. 先红：`/admin/api/config*` 仅本地 Admin 正例；飞书 Admin、Operator、User 与未认证主体全部拒绝。
2. 先红：旧 `/app/api/config*` 全部 404/405 闭集；body-limit、Origin/CSRF 与允许方法清单只登记新路径且集合相等。
3. 先红：`index.html/app.js` 不再含配置表单、配置请求或测试入口；`admin.html/admin.js` 本地 Admin 路径保持既有功能和安全文案。
4. 最小机械搬迁；OAuth 连接测试 callback 回 `/admin`，不改变测试域、Session、身份解析、申请或现场 GO 语义。
5. 构建 wheel 与浏览器回归，确认 B1 的脱敏投影和 B2 的本地配置区共存且服务端各自独立授权。

**承重反证：** 恢复任一旧 config 路径、让飞书 Admin 复用 raw DTO、或漏登记任一新 JSON 写路径时，对应路由/安全测试必须转红。

---

## 5. 正式用户路径与验收矩阵

| 路径 | 成功事实 | 关键失败对照 |
| --- | --- | --- |
| 首启本地 Admin | `/login` 输入 `admin/admin` → 只进强制改密 → Session 轮换 → 可选 `/app` 或 `/admin` | 错 username/口令同错；改密前所有业务/config API 403 |
| 飞书 Operator 工作台 | fake OAuth + WORKBENCH → 当前目录角色允许 → Session → `/app` | role 改 USER 后下一请求即拒绝并清页面，不等重新登录 |
| 飞书 Operator 管理中心 | fake OAuth + ADMIN_CENTER → 固定“不能进入管理中心” → 无 Session | 不自动回落 `/app`，不暴露角色或页面存在性 |
| 飞书 Admin 管理中心 | fake OAuth + ADMIN_CENTER → `/admin` → 只读独立脱敏状态投影 | raw config/保存/清除/连接测试均 403；响应无 App ID/generation/Secret/endpoint/异常正文，不能伪装成本地来源 |
| 普通用户直接登录 | fake OAuth + WORKBENCH → 固定“请从具体结果链接进入” → 无 Session | 不归类成登录失败或身份待激活，不重定向循环，不出现工作台/列表 |
| 普通用户任务深链 | `/app/tasks/{id}` → `/login` → fake OAuth → 返回详情 shell → 详情 API 再算 ACL | 无 ACL、其他群或不存在统一 404；无任务列表/提交/Admin 导航 |
| 未登记深链 | fake OAuth → 创建/复用 SAFE_TASK_LINK 申请 → 无 Session → 登录壳显示通用待处理 | 不显示 task ID/存在性、角色、Admin、subject；主体不匹配不能接管申请 |
| 激活后重登 | request ID 只进入新的 OAuth context → fake OAuth subject 与申请一致且已获批 → 恢复原 intent | 无匿名状态 GET；主体不匹配、rejected/expired、恢复后角色不允许时均无 Session，重放不改终态 |
| 飞书 OAuth 连接测试 | 本地 Admin 从 `/admin` 发起 → 测试域 callback → 记录闭集结果 | context/Session/身份查询/激活申请均为 0；无本地 Session 时拒绝 |

自动化测试用 ASGI + fake OAuth/port 证明功能链；一次性 PostgreSQL 证明迁移与事务；本机真实浏览器只证明指定尺寸下的首启/导航/视觉与键盘路径。三者都不是部署、真实飞书或 UAT。

---

## 6. 验证、独立复审与退出标准

### 每个切片

- 聚焦红灯 → 最小实现 → 正反边界 → 相关安全/集成测试。
- `git diff --check`、真实 diff、工作区状态；只暂存计划列出的文件，注册表/打包/文档真源若被现有守卫迫使调整须在 PR 中单列理由。
- 关键保护做隔离变异，确认加载的是变异代码并在还原后复绿。

### W2 最终四门与深档证据

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

- 使用一次性 PostgreSQL 16 跑全量与 migration 路径，必须 0 skip；CI integration 同样 0 skip。
- 构建 wheel，核对 login/app/admin/detail 全部静态资源真实入包；先实测，缺失才改打包配置。
- 全仓新增 `allow_hosts` 为 0；fake OAuth 不能放开任何外部 socket。
- 浏览器视觉：桌面 1024/1280/1440 的工作台与 Admin；窄屏登录/改密/激活状态/详情。记录截图对应的精确 SHA；不把截图写成产品 UAT。
- 独立审查固定 base、merge-base、HEAD、真实 diff、命令输出与 skip；沿登录→state/context→身份/角色→Session→shell/API，以及 unknown→ActivationRequest→重登恢复两条链审查。

### 退出标准

- 三个切片均合入 main，handoff 记录精确合入 SHA、CI 与本地/真库/浏览器证据。
- 四个壳和角色矩阵由服务端测试承重；普通用户不存在列表/门户旁路。
- login context 与 state 原子、连接测试零 context、任意 URL/path 无法进入类型或数据库。
- W1b 激活边界无回退：unknown 无 Session、撤权不重新激活、群入口不受 W2 改动。
- 当前 RI5 配置功能已离开工作台但存储/真实调用门不变；W4a/W4b/W5 未提前实现。
- 未接真实飞书/模型/运维目标，未部署，未做 canary/UAT；这些必须在交付结论中继续写“未覆盖”。

### 回滚

- W2-B2 可独立回退到旧 config 路径而不撤销 B1 的登录/角色/壳；W2-B1 可回退到 W1b 的单 `/app` 壳与旧路由，不删除 W2-A 数据。
- W2-A schema 只在满足 §3.5 的无损条件时降级；有 W2-only 事实时以前向修复为主，禁止为回滚静默改写 intent。
- 关闭 Web feature flag 只停止入口，不等于清理 Session、state、context 或激活事实；清理必须走各自已有时效/迁移规则。
