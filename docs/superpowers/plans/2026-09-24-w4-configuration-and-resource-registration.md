# W4 配置迁移与资源参数登记实施计划

> 状态：Approved V0.1。项目负责人在 Codex 会话中批准本计划并下达 W4a 离线实现开工口令；
> 该批准没有公开的 GitHub 审批 permalink。计划 PR #80
> （<https://github.com/shixian66/xiaowei-agent/pull/80>，受审 head
> `24e01d69f6c89367eb4d27bd7d93d990af5824dc`）已由负责人 squash 合入
> `2cef6aa52e17eece8eb70ebce0057cfdddd8167a`；合入事实本身不是批准来源。
>
> 授权范围只有 Task 0–7（W4a）的离线实现。Task 8–11（W4b）须等 W4a exact-SHA 复审、CI 与
> 负责人合入后再从新的最新 `main` 开工；W4c、W5、真实 Provider、真实 Secret、联网、部署、
> canary 与 UAT 均未授权。
>
> 规划基线：`origin/main@43151623fe420cfe63581b9117ed3838c7f7cb6e`；W4a 开工基线为合入本计划后的
> `origin/main@2cef6aa52e17eece8eb70ebce0057cfdddd8167a`（两者之间只有本计划与文档契约变更）。
>
> 规格真源：[Web 运维工作台总体设计](../specs/2026-09-19-web-operations-console-identity-activation-design.md)
>
> **实施者必须使用 `superpowers:executing-plans` 按任务顺序执行，并对每项行为变更先走 TDD。**

## 1. 目标与交付方式

W4 用**一份总计划、两个独立实现 PR**完成：

1. **W4a 配置域迁移**：把当前 `.config/integrations.json` 拆为 AI、飞书、resources 三个固定目录；
   AI 与飞书各自拥有 generation、加载回执、状态与审计；旧单文件只允许一次性显式迁移，运行时不双读。
2. **W4b 资源参数登记**：在 resources 域保存 StarRocks 与 Prometheus 的闭集参数，只做本地语法和
   字段组合校验，任何 DNS、socket、登录、SQL、HTTP、Provider 或目标 adapter 调用都必须为 0。

两个 PR 依赖单向：**W4a 先合入并独立验收，W4b 才能开工**。不能把 W4a 做到一半就提前加入资源
DTO，也不能把 W4b 的页面先接一套临时文件格式。每个 PR 自己更新 handoff，不另造只为“收口”的第三个
实现 PR。

### 1.1 为什么可以在一份计划里写完

W4a 与 W4b 共用同一组已批准不变量：三域目录、Secret 可见性矩阵、原子文件写、generation、加载回执、
本地 Admin 写权限和两阶段 Admin 审计。拆成两份计划会复制这些事实并产生漂移；合成一个实现 PR 又会让
AI/飞书迁移与新资源模型无法独立回滚。故本计划统一设计，实施仍分两门。

### 1.2 明确非目标

- 不连接真实 Gemini、飞书、StarRocks、Prometheus 或其他外部目标；已有 AI/飞书显式探针只保留原门，
  不因本计划自动取得现场 GO。
- 不实现 W4c。数据库/Prometheus 的“测试连接”按钮、DNS、socket、HTTP、登录、SQL、adapter 与
  ToolGateway 装配一律不出现。
- 不实现 W5：不提供 release override、热加载、自动重启、`docker.sock`、正式部署、canary 或 UAT。
- 不实现 W3 后续增强：不创建 `RoleAndDutyService`，不保存 DBA/值班/激活通知人绑定，不做私聊/可靠
  通知或敏感内容查看。
- 不实现 R1：不创建 requester/approver、结果 artifact、结果预览、导出或结果 ACL。
- 不建设通用配置版本中心、历史回滚树、任意 JSON、动态 Provider、动态 model/origin、任意驱动、代理、
  SQL 模板或自定义策略表达式。

## 2. 进入门与总不变量

### 2.1 进入门

- W3 V1 已由 PR #76–#79 完成离线实施与收口；W3 后续增强不作为 W4a/W4b 的进入条件。
- 本计划经独立技术复审并由项目负责人明确批准后，才可从最新 `main` 创建 W4a 分支。
- W4a exact-SHA 复审、CI 和负责人合入完成后，才可从新的最新 `main` 创建 W4b 分支。
- 计划批准只授权离线实现。读取真实 Secret、联网、部署、canary、UAT 仍需各自独立许可。

### 2.2 不可放宽的不变量

1. Secret 只存在于固定宿主配置文件与获准消费进程的短暂内存中；不进数据库、环境变量、Compose
   渲染、响应、DOM、日志、trace、异常、审计、加载回执或测试夹具。
2. Web 是唯一配置写者。消费者只读挂载自己的域；API、migrate、postgres 看不到任何配置域。
3. 文件更新继续使用同目录临时文件、`0600`、`fsync`、`os.replace`、目录 `fsync`。存在但损坏、
   非正规文件、符号链接或权限异常都 fail-closed，不能降级成“未配置”。
4. 每个域独立 generation。一次 AI 保存不能推进飞书或 resources generation；一次资源修改也不能让
   AI/飞书测试结果失效。
5. 加载回执只记录 `(service_name, config_domain, generation, status)`；缺文件或无法取得可信 generation
   时不伪造回执。任何进程只能为自己签字。
6. 文件配置与 PostgreSQL 审计不假装成一个事务：先写 `STARTED`，再执行文件/探针动作，最后写
   `SUCCEEDED` 或 `FAILED`。只留下 `STARTED` 表示结果未知，不得回报成功。
7. 审计 `STARTED` 失败时不得修改文件或发起探针；终态审计失败时返回统一 unavailable，不回报成功。
   文件可能已经落盘的事实由遗留的 `STARTED` 明确表示，不能靠“补一条成功”掩盖。
8. W4b 的本地校验全程在纯函数/契约层完成。测试必须在 socket、DNS、HTTP、数据库连接器和
   ToolGateway 都装上反证时仍证明调用次数为 0。
9. 配置页面与 API 的写、清除、探针仍只接受 `IdentitySource.LOCAL_ADMIN`；飞书 Admin 只能读取现有
   脱敏集成状态。前端隐藏不构成授权。
10. 旧 `integrations.json` 只是一份迁移输入。运行时不得长期双读、回退或自动迁移；新旧同时存在必须
    由迁移/预检路径返回闭集 `migration_required`。

## 3. 最终文件与挂载形态

| 宿主真源 | 容器固定路径 | web-app | worker | feishu-listener | channel-worker | api/migrate/postgres |
| --- | --- | :---: | :---: | :---: | :---: | :---: |
| `.config/ai/config.json` | `/run/xiaowei-config/ai/config.json` | 读写 | 只读 | 无 | 无 | 无 |
| `.config/feishu/config.json` | `/run/xiaowei-config/feishu/config.json` | 读写 | 无 | 只读 | 只读 | 无 |
| `.config/resources/config.json` | `/run/xiaowei-config/resources/config.json` | 读写 | 只读 | 无 | 无 | 无 |

禁止继续给任何消费者挂载 `.config` 父目录。三个宿主目录均由运行手册创建为 `0700`；配置文件由
Web 原子写为 `0600`。W4a 的预检逐目录使用不同哨兵，不创建真实 `config.json`，且成功/失败都删除哨兵。

## 4. 契约与接口形状

### 4.1 W4a 配置契约

在 `contracts/integration_config.py` 中把当前组合契约拆为：

```python
class ConfigDomain(StrEnum):
    AI = "ai"
    FEISHU = "feishu"
    RESOURCES = "resources"

class AiConfig(Contract):
    generation: StrictInt = Field(gt=0)
    gemini: GeminiIntegration

class FeishuConfig(Contract):
    generation: StrictInt = Field(gt=0)
    feishu: FeishuIntegration
```

`GeminiIntegration`、`FeishuIntegration` 与 `SecretRef` 保留现有 Secret 双防线。`IntegrationConfig`
只保留为**迁移输入契约**，不得继续被运行时 consumer 或 Web 路由引用。域配置的内部 canonical digest
只用于迁移后重读比对，不写数据库、不进入 API/日志/审计，也不作为授权键。

`interfaces/integration_config_file.py` 复用现有文件安全 helper，并提供固定类型的窄入口，而不是另造一个
通用配置框架或返回任意 `dict` 的 JSON API：

```python
read_ai_config(path: str = DEFAULT_AI_CONFIG_PATH) -> AiConfig
write_ai_config(path: str, config: AiConfig) -> None
read_feishu_config(path: str = DEFAULT_FEISHU_CONFIG_PATH) -> FeishuConfig
write_feishu_config(path: str, config: FeishuConfig) -> None
```

共同的文件安全逻辑保持私有。公开方法不得接受 schema class、动态 domain 字符串或自定义 serializer。
预检若要验证目录写入能力，只能调用同一原子文件 primitive 的专用 probe 包装；不得另抄一套
`open/write/replace` 流程，也不得用任一真实 `config.json` 当哨兵。

### 4.2 显式一次性迁移

新模块 `interfaces/integration_config_migrate.py` 只做离线本地文件迁移，不被任何服务启动路径调用：

```python
migrate_legacy_integration_config(*, config_root: str) -> MigrationResult
```

算法固定为：

1. 以 `O_NOFOLLOW`/dir-fd 验证绝对 `config_root`、旧文件与三个子目录；不打印路径或内容。
2. 没有旧文件且新 AI/飞书文件都不存在：返回 `nothing_to_migrate`。
3. 没有旧文件且新 AI/飞书文件都有效：返回 `already_migrated`。
4. 有旧文件时，严格读取 `IntegrationConfig`，映射为同 generation 的 `AiConfig`/`FeishuConfig`。
5. 某个新文件已存在时，必须重读并与映射结果逐字段完全相等；不等或损坏即
   `migration_required`，不覆盖任何一边。
6. 缺少的新文件用正常原子 writer 写入。每写一份立即重读并比对；任一步失败保留旧文件。
7. 两个新文件均重读相等后才删除旧文件并 fsync 旧目录。进程在第 6 步崩溃后可重跑：已完成且相等的
   一域被接受，缺失域继续写；不产生长期双读。

CLI 只输出闭集结果码。它不接收 Secret 参数，不自动启动/停止消费者；运行手册必须先停止消费者。
W4a 离线交付只证明迁移器行为，W5 才负责正式部署编排。

### 4.3 加载回执与状态

`service_config_state.provider` 在 `rev_0018` 改名为 `config_domain`，旧 `gemini` 行映射为 `ai`，旧
`feishu` 行保持 `feishu`；同一 migration 用 CHECK 一次性允许 `ai/feishu/resources` 三域，避免 W4b
只为扩一个闭集再造 migration。加载回执的契约键、内存实现、PostgreSQL、状态机与 Web 投影统一使用
`ConfigDomain`，不保留“列叫 provider、值却是 domain”的过渡形状；`ProviderName` 仍只服务于现有
Gemini/飞书探针分类，不用它冒充配置域。

W4a 三类 consumer：

- worker 只读取 AI 域并为 `(worker, ai)` 写回执；
- feishu-listener/channel-worker 只读取飞书域并各自签 `(service, feishu)`；
- web-app 为 OAuth 装配读取飞书域，只在启用 OAuth 且确实消费时签 `(web, feishu)`。

AI 与飞书测试继续使用现有 `CheckName`，但测试 generation 从对应域取得。状态机的五态不增加成员。
W4b 才让 worker 验证 resources 域并签 `(worker, resources)`；它只证明“格式已被 worker 读取”，不证明
任何资源可达或 adapter 已装配。

### 4.4 两阶段 Admin 审计

`AdminAuditAction` 新增最小动作：

- `CONFIG_SAVED`
- `CONFIG_CLEARED`
- `CONNECTION_TESTED`

三者进入显式 `STARTABLE_ACTIONS`，不得进入 `DIRECTORY_ACTIONS`。`AdminAuditReasonCode` 只增加实现
实际需要的闭集原因（配置无效、文件 I/O 失败、探针失败）；不写异常正文。target 固定为 `CONFIG`，
target digest 从 domain/check/resource 的稳定非 Secret 引用派生，不能从配置内容或 Secret 派生。

新增 `application/integration_config_service.py` 作为唯一写编排：Web handler 只完成认证/CSRF/body
解析并调用它。文件 repository Protocol 定义在 application 模块内，由
`interfaces/integration_config_file.py` 的文件 adapter 实现；application 不得反向 import
`interfaces`。W4a 的 Protocol 只列 AI/飞书显式方法，W4b 在 `ResourcesConfig` 真正存在时再加入
resources 方法；两阶段都不接收动态 schema/domain/path。服务依赖该 port 与 `AdminAuditStore`，统一
实现 STARTED → 动作 → 终态顺序，并保留当前单 Web 进程内的写串行化：
同一服务实例用一把异步锁覆盖每次 read-modify-write，避免两个并发保存读到同一 generation 后互相覆盖；
本阶段不建设多进程 CAS。operation id 复用现有服务端 `trusted_trace_id()`，加 `w4:` 域前缀；客户端
不能提供或改写审计 operation id。本阶段不新增配置 mutation 幂等协议，浏览器收到不确定结果后必须
先重读 generation，不能自动重放写请求。

mutation/probe 路由先取得已认证 session，再由服务同时校验 `LOCAL_ADMIN` 来源与对应 capability；已认证
但来源不允许时走 `append_denied` 后统一 403。拒绝审计不可写时同样 fail-closed，且文件、state 与网络
调用均保持 0。未认证请求仍统一 401，不为未知主体伪造审计 actor。

AI/飞书同步探针也由该编排包住。OAuth 测试跨回调，`rev_0018` 新建仅存 state digest 与 audit
operation id 的 `web_oauth_test_contexts`；签发与 state 同事务，消费与 state 同事务返回 operation id。
登录 state 继续只能使用 `web_oauth_login_contexts`，两个 context 表不能互相回退。回调完成后按同一
operation id 写终态；测试 state 消费成功后若本地 Admin session 已失效，则不交换 code，并按取回的
operation id 写 FAILED。state 过期、无效或浏览器未返回时拿不到可信 operation id，只保留 STARTED，
诚实表示未知。

### 4.5 W4b 资源契约

新建 `contracts/resource_config.py`，使用 discriminated union，`extra="forbid"`：

```text
ResourcesConfig
  generation          > 0
  resources           0..100，resource_id 全文档唯一

StarRocksResource
  kind                "starrocks"
  resource_id         服务端生成的 32 位小写十六进制串，不可编辑
  environment         dev | test | staging | prod
  display_name        1..128
  host                主机/IP 形状，不含 scheme/path/userinfo/control
  port                1..65535
  database            1..128
  username            1..128
  password            SecretRef，只写
  tls_mode            disabled | verify_ca | verify_identity
  enabled             bool

PrometheusResource
  kind                "prometheus"
  resource_id / environment / display_name / enabled
  base_url            仅 http/https；禁止 userinfo/query/fragment/control，允许受控 path prefix
  auth_mode           none | basic | bearer
  username            仅 basic 必填
  secret              basic/bearer 必填，none 禁止
  tls_mode            disabled | verify_ca | verify_identity；https/模式组合闭合
```

StarRocks `host` 只接受不带方括号和端口的 ASCII hostname、IPv4 或 IPv6 字面量；只做语法解析，绝不
解析 DNS。Prometheus 的 `http` 只能配 `disabled`，`https` 只能配 `verify_ca` 或
`verify_identity`；`basic` 必须同时有 username/secret，`bearer` 只能有 secret，`none` 两者都禁止。
这些组合在契约层一次定义，Web 请求模型不得另抄一份较宽规则。

本地校验只回答字符串与字段组合是否合法；不解析 DNS，不判断公网/私网，不探测端口，不发送 HTTP，
不登录，也不把 resource 注册成 capability target。W4c 才决定真实目标网络策略与唯一调用路径。

资源 Secret 与 Provider Secret 使用同一 `SecretRef`、`exclude=True`、`repr=False` 规则。安全投影只返回
resource_id、kind、environment、display_name、enabled、host/port 或脱敏 base URL、configured 布尔；
绝不返回 username、password/token、认证头或任意 Secret 长度/前后缀。

## 5. W4a 实施任务（PR 1）

### Task 0：冻结基线与先红证明

**Files**：

- Modify: `tests/contract/test_doc_fact_binding.py`
- Create: `tests/contract/test_w4_config_domain_contracts.py`
- Create: `tests/security/test_w4_config_domain_boundary.py`

**Steps**：

1. 从最新 `main` 创建 `claude/w4a-config-domains`，核对 HEAD、merge-base、dirty/untracked 文件。
2. 把本计划状态改为 Approved，并记录项目负责人批准的持久链接；只改计划/交接真源，不提前写源码。
3. 先写会失败的契约测试：三域固定路径、Secret 投影、AI/飞书 generation 独立、旧运行时引用必须消失。
4. 先写会失败的安全测试：精确挂载矩阵、consumer 不可见其他域、API/migrate 无挂载、legacy/mixed
   fail-closed、动态 schema/path API 不存在。
5. 运行聚焦测试确认 RED 的原因是生产形状尚未实现，而不是 import/fixture/锚点错误。

### Task 1：拆分契约与文件边界

**Files**：

- Modify: `src/xiaowei_agent/contracts/integration_config.py`
- Modify: `src/xiaowei_agent/contracts/provider_state.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/interfaces/integration_config_file.py`
- Modify: `tests/unit/test_integration_config.py`
- Modify: `tests/security/test_integration_config_boundary.py`
- Modify: `tests/security/test_secret_field_exposure.py`

**TDD**：先覆盖不存在/损坏/符号链接/FIFO/超限/原子替换失败/目录 fsync 失败，再实现两个窄 reader/writer。
正常对照必须证明 AI 写入不改飞书 inode/generation，反例证明任一 Secret 不进 `repr`、dump 或错误文本。

### Task 2：实现可重跑的一次性迁移

**Files**：

- Create: `src/xiaowei_agent/interfaces/integration_config_migrate.py`
- Create: `tests/contract/test_integration_config_migration.py`
- Modify: `tests/security/test_module_layering.py`
- Modify: `README.md`

**TDD 矩阵**：空目录、legacy-only、split-only、部分写后重跑、两份新文件已精确匹配、任一新文件冲突、
旧文件损坏、目标符号链接、第二份写失败、重读失败、unlink/fsync 失败。所有失败分支断言旧文件仍在且
已有新文件不被覆盖；成功才断言旧文件消失。做隔离变异：把“完全比对”降为“文件存在即跳过”，冲突
用例必须红。

### Task 3：迁移 schema、回执与审计闭集

**Files**：

- Modify: `src/xiaowei_agent/contracts/enums.py`
- Modify: `src/xiaowei_agent/contracts/admin_audit.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`
- Modify: `src/xiaowei_agent/persistence/provider_state.py`
- Modify: `src/xiaowei_agent/persistence/web_session.py`
- Modify: `src/xiaowei_agent/persistence/memory.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `src/xiaowei_agent/persistence/rows.py`
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0018_w4a_config_domains.py`
- Modify: `tests/contract/test_admin_audit_contracts.py`
- Modify: `tests/contract/test_identity_schema.py`
- Modify: `tests/contract/test_ri5_schema.py`
- Modify: `tests/contract/test_schema_matches_migration.py`
- Modify: `tests/integration/test_migration_paths.py`
- Create: `tests/integration/test_w4a_config_domain_postgres.py`

**TDD**：

- 证明旧 gemini/feishu receipt 无损映射；主键冲突、零 generation、未知 domain 由真库拒绝。
- 证明 OAuth test context 与 state 同事务签发/消费/过期/清理，登录 context 不能被测试分支消费。
- 证明三种新 action 只能两阶段，目录动作仍不能 STARTED；CHECK 与枚举逐值一致。
- downgrade 只在没有 W4a 审计事实和 test context 时恢复旧 schema。存在 append-only W4a 审计事实时
  即使 destructive flag 开启也拒绝，不删除审计换取降级；存在 `resources` 加载回执时也必须用闭集
  migration-safety 错误拒绝，因为旧 schema 无法表示该 domain，不能把原始 CHECK/DDL 错误留到降级中途。
  测试在 `finally` 升回 head。

### Task 4：切换 consumer、状态机与唯一配置服务

**Files**：

- Create: `src/xiaowei_agent/application/integration_config_service.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Modify: `src/xiaowei_agent/application/integration_state.py`
- Modify: `src/xiaowei_agent/interfaces/provider_consumption.py`
- Modify: `src/xiaowei_agent/interfaces/web_auth.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `tests/unit/test_integration_state.py`
- Modify: `tests/contract/test_ri5_load_receipts.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/unit/test_local_stack.py`
- Create: `tests/contract/test_w4a_integration_config_service.py`
- Modify: `tests/security/test_ri5_web_assembly.py`
- Modify: `tests/security/test_task_view_runtime_authority.py`

先写失败测试证明：每个进程只签自己的域、坏文件不伪造 generation、AI 与飞书独立重启、STARTED 失败
不触碰文件/网络、文件失败写 FAILED、终态审计失败不返回成功、客户端不能提供 operation id。同步 probe
与 OAuth probe 分别覆盖成功、闭集失败、超时、state 过期和回调 session 撤销；并发同域保存必须得到
两个连续 generation 且不丢更新，跨域保存不得改写另一域。
文件 adapter 必须在 `_conformance.py` 里静态赋给 application port，运行时测试再逐方法比对关键字签名；
不能只靠“测试里刚好调用成功”冒充结构兼容。

### Task 5：切换 Admin API/UI

**Files**：

- Modify: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `src/xiaowei_agent/interfaces/web_static/admin.html`
- Modify: `src/xiaowei_agent/interfaces/web_static/admin.js`
- Modify: `src/xiaowei_agent/interfaces/web_static/app.css`
- Modify: `tests/contract/test_ri5_config_api.py`
- Modify: `tests/contract/test_ri5_probe_routes.py`
- Modify: `tests/contract/test_web_app_routes.py`
- Modify: `tests/contract/test_web_static_assets.py`
- Modify: `tests/contract/test_web_admin_identity_api.py`

把组合 API 改为域 API：`GET/PUT /admin/api/config/ai`、`GET/PUT /admin/api/config/feishu` 与各域显式
clear 路径；旧 `GET/PUT /admin/api/config` 不保留兼容双写。所有 mutation 要有 CSRF/Origin/body limit、
Local Admin 与服务端审计。前端分别显示 generation、待重启服务与现有探针状态，
不得把一域保存渲染成“两域已保存”。PUT 延续既有严格语义：字段缺省表示保留，显式 `null`/空串
不能暗示清除，Secret 清除只能走独立确认动作；正常对照与反例都必须证明另一域文件、generation、
Secret 与 inode 未被改写。

### Task 6：切换 Compose、预检、smoke 与文档

**Files**：

- Modify: `docker-compose.yml`
- Modify: `src/xiaowei_agent/interfaces/config_preflight.py`
- Modify: `scripts/compose_smoke.py`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `tests/security/test_ri5_compose_boundary.py`
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `AGENT_HANDOFF.md`

静态契约逐格等于第 3 节矩阵；不能只断“有挂载”。smoke 使用三个独立私有宿主目录和合成假 Secret，
结束后逐目录安全清理。预检证明不会创建三个真实 config 文件。构建 wheel 确认新增模块/静态资源在包内。

### Task 7：W4a 总验收与反证

运行：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

有 PostgreSQL DSN 时全量必须 `0 skipped`；Compose smoke 必须在真实容器内证明精确挂载与默认关闭，
但不得联网。至少做四项隔离变异：恢复父目录挂载、恢复运行时 legacy fallback、允许 STARTED 失败后写文件、
让一个进程替兄弟签回执；每项必须红在对应保护。更新 handoff 为“W4a 离线候选”，不写成部署或迁移已执行。

## 6. W4b 实施任务（PR 2，仅在 W4a 合入后）

### Task 8：资源契约与 resources 文件

**Files**：

- Create: `src/xiaowei_agent/contracts/resource_config.py`
- Modify: `src/xiaowei_agent/contracts/integration_config.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Modify: `src/xiaowei_agent/interfaces/integration_config_file.py`
- Create: `tests/unit/test_resource_config.py`
- Modify: `tests/security/test_integration_config_boundary.py`
- Modify: `tests/security/test_secret_field_exposure.py`

先写完整正反表：每个字段的最小/最大边界、未知字段、重复 ID、100/101 个资源、认证方式组合、TLS/scheme
组合、URL userinfo/query/fragment/control、Secret 的 repr/dump。再实现契约与窄 reader/writer；不得引入 URL
请求库或数据库驱动依赖。

### Task 9：资源维护服务、审计与 Web API

**Files**：

- Modify: `src/xiaowei_agent/application/integration_config_service.py`
- Modify: `src/xiaowei_agent/interfaces/web_models.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Create: `tests/contract/test_w4b_resource_config_api.py`
- Modify: `tests/contract/test_web_app_routes.py`
- Modify: `tests/security/test_ri5_web_assembly.py`

路由为 Local Admin-only：读取列表、创建、按 ID 修改、显式清除 Secret、显式删除资源。resource_id 由注入的
安全 ID factory 在服务端生成；客户端提供或修改 ID、kind、任意参数 dict 都被拒。每次写整份 resources
文件并推进一次 generation，复用 W4a 两阶段审计。不存在/冲突/审计不可写/文件失败/终态审计失败均有
正常对照和文件指纹反例。ID factory 若撞上文档内已有 ID，返回闭集 conflict 且文件/generation/审计
终态按失败路径处理；不靠覆盖旧资源或无界重试“解决”碰撞。修改请求省略 Secret 表示保留，显式
`null`/空串被拒，清除必须走独立确认动作；更新一种资源不得重排、重写或清空其他资源的 Secret。

### Task 10：资源 UI、worker 回执与零网络证明

**Files**：

- Modify: `src/xiaowei_agent/interfaces/provider_consumption.py`
- Modify: `src/xiaowei_agent/interfaces/web_static/admin.html`
- Modify: `src/xiaowei_agent/interfaces/web_static/admin.js`
- Modify: `src/xiaowei_agent/interfaces/web_static/app.css`
- Create: `tests/security/test_w4b_zero_network.py`
- Modify: `tests/contract/test_ri5_load_receipts.py`
- Modify: `tests/contract/test_web_static_assets.py`
- Modify: `tests/security/test_module_layering.py`

worker 启动时只解析 resources 文件并写 `(worker, resources)` 回执，随即丢弃值；不把资源传给 Runtime、
Resolver、Registry、ToolGateway 或任何 adapter。页面显示“已保存，尚未接入”、generation、待重启与加载
状态，不出现“测试连接”。安全测试同时：

- 使用 pytest-socket 保持外部 socket 禁用；
- monkeypatch `socket.getaddrinfo`、`socket.create_connection`、HTTP transport、数据库 connect 与 Gateway，
  断言资源创建/修改/读取全部 0 调用；
- 用 AST 禁止 W4b 契约、服务和 Web 资源路径 import `tools.*`、数据库驱动、HTTP client 或 Gateway；
- 变异加入一次 DNS 解析或测试按钮时，精确测试必须转红。

### Task 11：W4b Compose/降级保护/文档与总验收

**Files**：

- Modify: `scripts/compose_smoke.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `AGENT_HANDOFF.md`

W4b 不新增 migration 或资源表：`resources` 已由 W4a 的 config-domain CHECK 接纳，资源真源是固定文件且
Secret 禁止进数据库。复用 provider-state 共享套件与现有 PostgreSQL 绑定证明 resources receipt 真正落库，
不能只在内存 fake 上给出绿灯；并给 `rev_0018` 的 downgrade 守卫补真库反例，证明存在 resources receipt
时会在任何 DDL 前闭集拒绝且 revision/data 均不变。

随后运行四门、PostgreSQL 0-skip 全量、Compose smoke、wheel 检查与 1024/1280/1440 管理页面视觉检查。
至少做三项反证：资源保存触发 DNS、Secret 进入响应/审计、resources 挂给非 worker consumer；分别必须红。
handoff 只能写“W4b 离线参数登记完成”，未取得真实目标、部署、canary 或 UAT 证据。

## 7. 独立复审清单

复审者按 exact SHA 核对，不接受 PR 描述替代 diff：

1. `git merge-base` 确认每个 PR 从其当时最新 main 开始；工作区无夹带变更。
2. W4a diff 不含资源业务 DTO/CRUD；W4b diff 不含 target adapter、ToolGateway、真实探针或职责绑定。
3. 三域 mount 矩阵逐格相等，consumer 没有父目录挂载；Web 没有执行权。
4. legacy 迁移的每个 crash point 都可重跑，冲突绝不覆盖；运行时没有 fallback。
5. Secret 的写、读、错误、repr、dump、API、DOM、审计、回执与测试夹具路径全部扫描。
6. STARTED/terminal 审计覆盖同步保存、清除、同步探针与 OAuth 跨回调；撤掉保护后测试因目标原因转红。
7. generation/receipt/test 的 domain 归属一致，不会由 AI 保存失效飞书，也不会由 Web 替 worker 签字。
8. W4b 在所有反例中网络调用计数为 0，且没有“按钮禁用但后端仍可调用”的假边界。
9. migration/schema/fake/PostgreSQL 行为一致；本机 skip 与 CI 0-skip 分开报告。
10. 结论分开标注源码、离线测试、容器 PostgreSQL/Compose、真实环境、部署与用户验收。

## 8. 退出标准

### W4a 退出

- 单文件运行真源已被两个域文件取代，旧文件只剩显式、可重跑的一次性迁移入口；无长期双读。
- AI/飞书 generation、回执、状态和探针互不串扰；精确挂载矩阵成立。
- 配置保存/清除/探针的两阶段审计及失败语义可证伪。
- 四门、真实 PostgreSQL 0-skip、Compose smoke、wheel 与桌面视觉检查通过。
- 证据等级仍是 `tests`；没有真实迁移、Provider 现场、部署、canary 或 UAT 声称。

### W4b 退出

- StarRocks/Prometheus 闭集参数可由 Local Admin 新增、修改、清 Secret、删除并安全投影。
- resources generation/worker 加载回执成立，页面明确“已保存，尚未接入”。
- 资源保存、读取、页面与 smoke 的 DNS/socket/HTTP/登录/SQL/adapter/Gateway 调用均为 0。
- 没有职责绑定、requester/approver、真实连接、热加载或目标注册旁路。
- 四门、真实 PostgreSQL 0-skip、Compose smoke、wheel 与桌面视觉检查通过；handoff 如实记录未覆盖。

满足 W4a/W4b 退出标准仍**不等于 W4c、W5、真实迁移、部署、canary 或用户验收完成**。
