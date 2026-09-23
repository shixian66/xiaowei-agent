# W2 登录与多 shell 实施计划

> 状态：Review Draft V0.1。本文尚未获项目负责人和独立审查者批准；批准前不写 W2 测试、迁移或源码。

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
3. 从届时最新 main 建实现分支。批准范围内按两个切片连续推进，不逐文件重复申请；新增范围、真实调用、合并、部署和 UAT 仍分别等待授权。

---

## 1. 范围、非目标与阶段解释

### 本阶段交付

1. 独立 `/login`：固定用户名 `admin` 的本地登录、飞书登录、强制改密、闭集激活状态。
2. `/app`：只服务 `ADMIN` / `OPERATOR` 的现有运维工作台；继续把所有输入提交到既有 Web 任务入口，由统一 Runtime 决定普通对话、澄清或 capability，不在前端按关键词分流。
3. `/admin`：只服务 `ADMIN` 的独立管理壳。W2 只迁移**现有 RI5 AI/飞书配置表单**及其本地 Admin API，不虚构尚未交付的用户、职责、审计或资源页面。
4. `/app/tasks/{task_id}`：保持只读详情 shell；`USER` 只可经具体链接进入，最终内容仍由现有任务 ACL 决定。
5. `web_oauth_login_contexts`、闭集 return intent、W2 所需的 `ActivationRequest.return_intent` 与 `SAFE_TASK_LINK` 来源。
6. 所有页面和 API 的服务端角色/来源判定；前端隐藏入口只改善体验，不承担授权。

### 两处已批准真源的解释

- **用户名不落第二份账号真源。** W1a 已批准计划明确把 `LocalCredential.username` 推迟给 W2，并规定首版必须是固定常量 `admin`。W2 在请求契约、服务和 UI 中校验固定用户名，不给 `local_admins` 增 `username` 列，也不支持第二个本地账号；如审查者认为 ARCHITECTURE 中“W2 的 `LocalCredential.username`”要求持久化列，必须先修正文档冲突，不能在实现中自行选边。
- **配置 UI 与配置存储分阶段。** W2 按规格 §9.2 把系统配置移出工作台，并把现有 RI5 表单/API 搬到 `/admin`；`.config/integrations.json`、Provider DTO、探针语义和 Compose 挂载保持不变。W4a 仍独占“三域配置文件、迁移、挂载矩阵与 `migration_required`”；W2 不提前实现其中任何一项。

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
| `contracts/activation.py` / `rev_0015` | W1b 来源只有 `WEB_LOGIN/FEISHU_GROUP`，无 return intent | W2 增 `SAFE_TASK_LINK`；Web 来源必须有闭集 intent，群来源必须无 intent；迁移兼容既有行 |
| `IdentityActivationService.request_web()` | 只收 subject | 收已验证 return intent；安全任务深链派生 `SAFE_TASK_LINK`，其余 Web 登录为 `WEB_LOGIN` |
| `DirectoryFeishuIdentityDirectory` | 每次查库后只返回 `AuthenticatedPrincipal`，丢掉 `ProductRole` | 提供 Web 专用的窄解析结果 `principal + current role`；listener 继续消费原协议，不把角色塞入跨渠道 principal |
| `AuthenticatedWebSession` / `_WebPrincipalSession` | 只有 principal、CSRF、改密状态 | 每次请求从当前目录重算 role 与 `AdminCapability`；cookie/session 表不缓存角色或能力 |
| `LocalAdminAuthService` | 只收 password；本地主体是固定 ADMIN | `login()` 同时收固定 username，非 `admin` 与错密码走同一闭集错误；不新增账号表或用户名列 |
| `web_app.py` | `/app` 条件渲染登录/改密/工作台；config API 在 `/app/api/config*` | 注册 `/login`、`/app`、`/admin` 三个独立壳；移动现有 config UI/API 到 `/admin`；更新 body-limit 与路由闭集 |
| `web_static/index.html/app.js` | 工作台与配置面混在一个 shell；桌面下界 1280 | 工作台删除配置代码并支持 1024+；新增 login/admin 资源；详情 shell 不动提交边界 |
| `WebCurrentUser` | 只返回 actor/environment/渠道权限 | 增当前 `ProductRole` 与派生的管理能力闭集；前端仅用于导航，API 仍独立授权 |
| 迁移头 | `rev_0015_activation_requests` | 新增 `rev_0016_web_login_contexts`，同步 runtime schema、row mapping、迁移路径与降级保护 |

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

`web_return_path(intent)` 是唯一服务端路径重建点：`/app`、`/admin`、`/app/tasks/{quote(task_id)}` 或 `/login` 的激活状态视图。它不接收 base URL，也不读取 `Forwarded`/`Host` 来生成跳转。

- `/login` 与本地密码 POST 只传同一个结构化契约，不传 redirect 字符串；本地登录没有 OAuth state，因此不写 context 表，但仍必须由服务端重建目标并执行同一角色门。
- 匿名访问 `/app`、`/admin` 或任务详情时，服务端只把对应闭集 intent 带到 `/login`；畸形 kind、额外 ID、重复 query 参数或任意 `next` 一律拒绝，不降级成猜测的默认路径。
- `ACTIVATION_STATUS` 是 OAuth 激活恢复意图，不是本地 Admin 查看申请的旁路；本地密码登录不得借该 intent 读取申请事实。

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
- 登录与测试继续用不同 digest domain，同一 callback 的“登录域优先、仅 state 不匹配才回落测试域”顺序不变。
- state 收割删除 context，不能留下孤儿；context 不能独立创建、更新、加载或复用。
- 容量仍按未消费/未过期 state 计算，不另设第二个额度。

### 3.3 角色、壳与 Session

| 当前角色/来源 | `/app` | `/admin` | 具体任务详情 | 配置读写/测试 |
| --- | :---: | :---: | :---: | :---: |
| 本地 Admin | 是 | 是 | 仍过任务 ACL | 是；真实测试还需既有 flag/现场 GO |
| 飞书 `ADMIN` | 是 | 是 | 仍过任务 ACL | 否；W2 不把现有 raw config DTO 暴露给它 |
| 飞书 `OPERATOR` | 是 | 否 | 仍过任务 ACL | 否 |
| 飞书 `USER` | 否 | 否 | 仅具体链接且符合现有 ACL | 否 |

- role 与 `AdminCapability` 每次受保护请求从数据库目录重算，不写入 cookie/session，不从 `ChannelPermission` 反推。
- OAuth 回调在签 Session 前检查 intent 与当前角色：`USER→WORKBENCH`、`OPERATOR→ADMIN_CENTER` 等直接拒绝且不轮换 Session。
- `/app/api/tasks` 列表和提交与 `/app` 使用同一个 Admin/Operator 服务端门；`USER` 不能绕过隐藏导航直调。`/app/api/tasks/{task_id}` 保留现有 owner/群/管理员任务 ACL。
- 初始本地 Admin 改密前仍只允许登录、改密和退出；不能访问任一壳、任务或配置 API。

### 3.4 激活状态与原目标

- 未登记 OAuth 身份：消费登录 state、验证 code 后创建/复用申请；不签 Session。申请保存**原始闭集 intent**，异常只把随机 request ID 交给 `/login` 激活状态视图。
- `SAFE_TASK_DETAIL` 进入的申请使用 `ActivationSource.SAFE_TASK_LINK`；普通工作台/Admin/激活状态重新申请使用 `WEB_LOGIN`。两者都不保存 URL、任务正文或“目标是否存在”。
- 既有 W1b 行迁移：`WEB_LOGIN` 回填 `WORKBENCH`，`FEISHU_GROUP` 保持无 return intent；新契约以 source/intent 双向校验。不能把群申请伪装成 Web intent。
- `/login` 的激活状态只显示 `pending/approved/rejected/expired` 对应的固定文案，不返回 subject、角色、Admin 名单、task ID、结果存在性或审批人。
- 从 `ACTIVATION_STATUS(request_id)` 再次飞书登录时，OAuth subject 必须与申请 subject 精确相等。已批准则恢复申请中保存的原 intent；仍 pending 就复用原申请；rejected/expired 可按原 intent 新建申请。引用不存在或主体不匹配统一 fail-closed，不签 Session。
- 任何待激活/拒绝/过期页面都不是登录凭证，也不新增普通用户门户。

### 3.5 迁移与恢复

- `rev_0016` 新建 context 表，并给 `activation_requests` 增受 CHECK 约束的 intent 列/来源成员；runtime schema 与 migration DDL 集合相等。
- upgrade 必须兼容已有 W1b 行：Web 登录行回填 WORKBENCH，群行保持空 intent；再设约束，不假定表为空。
- downgrade 不得把 `SAFE_TASK_LINK` 或非 WORKBENCH intent 静默改成 WORKBENCH，也不得丢活跃 context。存在 W2-only 事实时用分类计数拒绝；空库/仅可无损还原的 W1b 形状才允许正常降级。若最终设计需要显式破坏性授权，必须在切片 A 审查中逐类写明被删除事实并用真库测试，不能沿用一个布尔总开关吞掉差异。
- 所有主动降级测试 `try/finally` 升回 head，并整文件运行，避免污染 session 级 schema fixture。

---

## 4. 实施切片

### W2-A：导航契约、原子登录 context 与激活意图

**可独立验收结果：** 存储层能够原子签发/消费“登录 state + 闭集 intent”，连接测试证明零 context；ActivationRequest 能无损表达 W2 深链来源。该切片不注册新页面或路由，运行时产品行为不变。

**预计文件范围：**

- `src/xiaowei_agent/contracts/{enums.py,activation.py,__init__.py}`
- `src/xiaowei_agent/contracts/web_navigation.py`（新）
- `src/xiaowei_agent/persistence/{web_session.py,schema.py,rows.py,memory.py,fake.py,postgres.py,_conformance.py}`
- `src/xiaowei_agent/persistence/migrations/versions/rev_0016_web_login_contexts.py`（新）
- `src/xiaowei_agent/application/identity_activation.py`
- 对应 contracts/shared suites/integration/migration/schema/row/conformance/security tests

**TDD 顺序：**

1. 先红：return intent 四个正例、额外/缺失引用、外部 URL/路径/控制字符/超长 ID 反例。
2. 先红：memory 与 PostgreSQL 共享套件证明登录签发/消费原子性、重放/过期、缺 context 回滚、连接测试零 context、收割无孤儿。
3. 先红：ActivationRequest 的 source/intent 组合、既有行升级、深链来源、跨 scope 与 subject 不串联。
4. 先红：真实 PostgreSQL migration upgrade/downgrade、DDL/runtime schema 一致、主动降级恢复 head。
5. 最小实现；两种 Store 继续跑同一套件，公开 Protocol 表面与 `_conformance.py` 同步。

**承重反证：** 临时撤掉“登录必须有 context”、把 context 改成可空、让测试 state 走登录方法、或删除 source/intent CHECK，各自至少一条目标用例因预期原因转红。

### W2-B：登录、角色路由与四个独立 shell

**可独立验收结果：** 本地 Admin、飞书 Admin/Operator/User、未知身份分别走完整正式入口；工作台、Admin 和详情后端权限与 UI 一致；现有配置功能只移动位置，不改变存储/探针语义。

**预计文件范围：**

- `src/xiaowei_agent/interfaces/{directory_identity.py,feishu_identity.py,web_auth.py,web_navigation.py,local_admin_auth.py,web_models.py,web_app.py,local_stack.py}`
- `src/xiaowei_agent/interfaces/web_static/{login.html,login.js,index.html,app.js,admin.html,admin.js,detail.html,detail.js,app.css}`（只改实际需要者）
- Web auth/routes/task/config/static/XSS/authority/assembly 契约与安全测试
- wheel/静态资源闭集测试；仅当实测 wheel 漏资源时才改 `pyproject.toml`
- `ARCHITECTURE.md`、`README.md`、`AGENT_HANDOFF.md` 只同步已经实现并验证的事实；`DEVELOPMENT_PLAN.md` 不写进度

**TDD 顺序：**

1. 固定 username `admin`：正确账号/口令成功；任一错误统一失败；无 username 列、无第二账号入口。
2. Web 专用身份解析：三角色与本地 Admin 的 role/capability；停用/撤权即时生效；Session 不保存角色。
3. OAuth：四种 intent 签发/回调、角色不允许时零 Session、unknown→申请→激活状态、批准后恢复原 intent；连接测试零 context/Session/身份解析/申请。
4. 路由：匿名重定向到 `/login`；Admin/Operator/User 正反例；列表/提交后端门；具体任务继续走现有 ACL；旧 `/app/api/config*` 关闭，新 `/admin/api/config*` 仍仅本地 Admin。
5. 壳与静态资源：每个 shell 只加载自己的 JS；无 inline script/handler、`innerHTML`、Secret/外部正文插值；详情 shell 继续无 textarea/提交 JS/CSRF token。
6. 现有 `app.js` 只保留任务链；配置代码移入 `admin.js`；OAuth 连接测试 callback 回 `/admin`，不改变测试安全语义。
7. 1024/1280/1440 工作台/Admin 视觉检查；手机宽度只检查登录、改密、激活状态和任务详情。

**承重反证：** 至少逐项验证：撤掉 USER 工作台门、撤掉 Operator Admin 门、让 callback 使用任意 `next`、给 pending 分支签 Session、让飞书 Admin 读取 raw config、或让详情 shell 加载工作台 JS时，对应测试转红。

---

## 5. 正式用户路径与验收矩阵

| 路径 | 成功事实 | 关键失败对照 |
| --- | --- | --- |
| 首启本地 Admin | `/login` 输入 `admin/admin` → 只进强制改密 → Session 轮换 → 可选 `/app` 或 `/admin` | 错 username/口令同错；改密前所有业务/config API 403 |
| 飞书 Operator 工作台 | fake OAuth + WORKBENCH → 当前目录角色允许 → Session → `/app` | role 改 USER 后下一请求即拒绝并清页面，不等重新登录 |
| 飞书 Admin 管理中心 | fake OAuth + ADMIN_CENTER → `/admin` | 飞书 Admin 调 raw config/连接测试仍 403；不能伪装成本地来源 |
| 普通用户任务深链 | `/app/tasks/{id}` → `/login` → fake OAuth → 返回详情 shell → 详情 API 再算 ACL | 无 ACL、其他群或不存在统一 404；无任务列表/提交/Admin 导航 |
| 未登记深链 | fake OAuth → 创建/复用 SAFE_TASK_LINK 申请 → 无 Session → 登录壳显示通用待处理 | 不显示 task ID/存在性、角色、Admin、subject；主体不匹配不能接管申请 |
| 激活后重登 | activation status → fake OAuth 身份已获批 → 恢复申请保存的原 intent | rejected/expired 不复活旧申请；重放不改终态 |
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

- 两个切片均合入 main，handoff 记录精确合入 SHA、CI 与本地/真库/浏览器证据。
- 四个壳和角色矩阵由服务端测试承重；普通用户不存在列表/门户旁路。
- login context 与 state 原子、连接测试零 context、任意 URL/path 无法进入类型或数据库。
- W1b 激活边界无回退：unknown 无 Session、撤权不重新激活、群入口不受 W2 改动。
- 当前 RI5 配置功能已离开工作台但存储/真实调用门不变；W4a/W4b/W5 未提前实现。
- 未接真实飞书/模型/运维目标，未部署，未做 canary/UAT；这些必须在交付结论中继续写“未覆盖”。

### 回滚

- W2-B 可回退到 W1b 的单 `/app` 壳与旧路由，不删除 W2-A 数据。
- W2-A schema 只在满足 §3.5 的无损条件时降级；有 W2-only 事实时以前向修复为主，禁止为回滚静默改写 intent。
- 关闭 Web feature flag 只停止入口，不等于清理 Session、state、context 或激活事实；清理必须走各自已有时效/迁移规则。
