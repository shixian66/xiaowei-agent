# M6b StarRocks 测试环境真实只读 adapter 实施计划（V1.1）

> 状态：Approved V1.1；2026-09-07 经复审后获项目负责人明确批准。
>
> 计划基线：`main` / `origin/main` =
> `e8128c8c364e1e5ba560c916044dfae0409dd490`（2026-09-06）。
>
> M6a 最终实现基线：
> `e2032fdff958d2309973458d5c762a54b3a4f855`，已是上述基线的祖先并已验收归档。
>
> 2026-09-07 项目负责人已说“开始开发”，本计划按“开始 M6b”处理：只授权进入
> M6b 计划与前置审计阶段。计划批准前不创建实现分支、不安装 driver；第 3.3 节真实连接
> 授权全部落定前，不读取真实 secret、不连接 StarRocks。计划批准后可以先做完全离线的
> TDD、normalizer 与 adapter 实现，不能把离线实现权解释为真实调用权。
>
> V1.1 采纳外部复审的两条 runbook 修订：expected digest 只能来自负责人/DBA 批准的
> 带外材料，禁止由同一次待验证连接自产自证；现场权限拒绝改为离线注入测试，真实窗口
> 不访问任何范围外对象。同时注明公共双资源 fixture 是合成测试值，不代表生产 target 形状。

## 0. 一句话结论

M6b 只把现有 `starrocks.slow_query.diagnose@1.0.0` 的两个固定模板接到一个获批的
StarRocks 测试集群。真实连接仍走现有
`IntentDraft → Resolver → Compiler → Runner → StepAdmission → ToolGateway → Evidence → Render`
链路；不会新增通用 SQL、导出、Web、飞书、TiDB、真实模型、E1 或生产连接。

这不是“把 fake adapter 换成 PyMySQL”这么简单。当前代码还有两个阻断缺口：

1. `test` 环境解析出两个资源，无法证明本次执行对应哪一个集群；
2. `DeterministicToolGateway` 只按 `gateway="starrocks"` 选 adapter，没有消费准入凭证中的
   `target_fingerprint`。

因此 M6b 必须先以 ADR 固定 target-aware binding，再实现只支持现有两个 operation 的
真实小结果 adapter。任何真实调用前还要核对物理集群身份、版本、权限和 AuditLoader DDL。

## 1. 目标、交付物与证据上限

### 1.1 目标

- 让现有慢查询能力在一个明确批准的非生产 StarRocks 数据源上完成真实只读闭环。
- 证明逻辑 `ResolvedTarget`、准入凭证、真实 connector 与服务端物理身份指向同一获批集群。
- 核对当前 AuditLoader 表、13 个输出字段、查询事件谓词、值域、时间单位和 count 分支语义。
- 证明未知 operation、错目标、错操作者、过期窗口、权限不足、DDL 漂移、响应畸形和超时均
  fail-closed，不能伪装成成功。
- 形成可复核的 `tests + test-env verified` 证据，并明确它不是部署、canary 或用户验收。

### 1.2 交付物

- `ADR-012`：M6b target-bound adapter selection、激活窗口、证据来源与兼容策略。
- `DeterministicToolGateway` 的 target-aware adapter 注册/选择接缝；现有 fake/recording
  gateway 行为保持兼容。
- 唯一测试目标目录条目与新的 selector version。
- 默认关闭、配置失败即关闭的 StarRocks 测试环境连接配置。
- 只支持 `list_slow_queries` 与 `count_queries_in_window` 的真实 StarRocks adapter。
- unit、contract、security、integration、L0-L2 eval 与变异反证。
- 不含业务 rows、host、账号、secret、连接串或原始上游错误的脱敏现场验证记录。
- `docs/handoff/M6b-acceptance-report.md`：实现和真实验证完成后才创建并填写。
- `AGENT_HANDOFF.md`：只在实际状态、SHA、证据和风险发生变化时更新。

### 1.3 最强允许结论

本里程碑最多写：

```text
starrocks.slow_query.diagnose: tests + test-env verified
```

不能写 deployed、canary、production-ready 或 user-accepted。真实验证只允许在批准窗口由
批准操作者人工触发；CI 永远不持有 StarRocks secret，也不连接真实运维目标。

## 2. 已核对源码事实与架构缺口

### 2.1 当前基线

- 当前分支：`claude/starrocks-query-diagnostics-plan`。
- `HEAD` 与 `origin/main` 均为
  `e8128c8c364e1e5ba560c916044dfae0409dd490`。
- 除本计划外，当前有一份用户原有的未跟踪文件：
  `docs/plans/starrocks-capability-development-roadmap.md`；M6b 不覆盖、不暂存该文件。
- M5 已验收基线为 `372c381f44ecfa1fa53961f137d0058033cbd805`；M6a 已验收基线为
  `e2032fdff958d2309973458d5c762a54b3a4f855`。
- M6a 归档记录的四门为：全量 2169 passed / 154 skipped，security 1012 passed /
  79 skipped / 1232 deselected，Ruff 通过，mypy 133 个源文件通过。这里引用的是历史
  精确 SHA 证据，本计划编写阶段没有把它冒充为当前分支新跑结果。

### 2.2 现有慢查询闭环

| 事实 | 当前落点 | M6b 含义 |
| --- | --- | --- |
| capability 为 `starrocks.slow_query.diagnose@1.0.0` | `capabilities/specs.py` | M6b 不新增 capability |
| 两个 operation 为 list/count | `capabilities/specs.py` | 真实 adapter 拒绝其他 operation |
| SQL 由 sqlglot AST 固定模板生成 | `planning/starrocks/compiler.py` | adapter 只执行已准入 SQL，不接收任意 SQL |
| SQL 表面为一个占位 AuditLoader 表和 13 列 | `capabilities/specs.py` | DDL 未核对前不能执行诊断 |
| list 默认 20 行、上限 200；count 恒 `LIMIT 1` | compiler/params/contracts | M6b 仍是小结果，不引入流式结果契约 |
| production StarRocks profile 上限 30 秒 | `governance/profiles.py` | 不抬高共享常量，不影响另两个能力 |
| Gateway 外层也按 ToolCall timeout 截止 | `tools/gateway.py` | driver 超时必须严格小于或受该上限覆盖 |
| Evidence 持久化完整白名单 facts | `evidence/builder.py`、`persistence/schema.py` | 真实 rows 会进入 append-only PostgreSQL Evidence |
| Render 只展示 queryId/queryTime/scanRows | `rendering/slow_query.py` | 不代表 Evidence 没有 db/user 等字段 |

### 2.3 两个必须先关掉的真实执行缺口

1. `capabilities/target.py` 的 `test` 目录目前是
   `("starrocks-test-1", "starrocks-test-2")`，resolver 只拒绝空目录，不拒绝多资源。
2. `tools/gateway.py` 当前只做 `self._adapters.get(call.gateway)`；虽然
   `AdmissionCertificate` 已携带 `target_fingerprint`，Gateway 没有用它选择真实 adapter。

只修第 1 项仍不够：connector 配错到另一集群时，gateway 字符串仍然相同。只修第 2 项也
不够：逻辑 target 正确但服务端连接指向别处时仍会产生错误归属的证据。M6b 必须同时完成：

```text
唯一 canonical resource id
  → target_fingerprint
  → Gateway 精确 binding
  → connector profile revision
  → 服务端物理身份 preflight
```

### 2.4 Evidence 保留缺口

现有 PostgreSQL `task_evidence` 是 append-only，没有任务级删除/TTL API。真实慢查询 facts 会
持久化，而不是只存在 terminal 输出。M6b 不应顺手实现通用 Evidence TTL；那会扩大持久化
契约与后续 Result Artifact 的范围。

M6b 的最小安全做法是：真实验证使用独立、命名固定、只服务本次窗口的临时 PostgreSQL /
Compose project；按批准周期保留后销毁该 project 的明确 volume。长期只保存脱敏验收报告、
摘要 digest、版本、计数与结论，不保存业务 rows。若负责人不批准此删除方式，或基础设施
备份/WAL 会让承诺不成立，M6b 保持阻塞，不写“已删除”或“物理不可恢复”。

### 2.5 ADR 编号冲突处理

`ARCHITECTURE.md` §15 明确要求：修改 ToolGateway 语义必须先写 ADR。正式索引当前只到
ADR-011，因此 M6b 的 chronological ADR 应使用 ADR-012。

未跟踪的 V2.8 路线稿把 ADR-012/013/014 预留给后续通用 SQL、Artifact/stream 和 intent；
Review Draft 不占用正式编号。后续路线正式采纳时应顺延为 ADR-013/014/015。M6b 不在本轮
静默改那份用户未跟踪路线稿。

## 3. 进入门与待负责人明确批准项

### 3.1 已满足

- [x] M6a 已验收并归档。
- [x] 已按顺序完整读取最新 `AGENTS.md`、`ARCHITECTURE.md`、`AGENT_HANDOFF.md`、
  `README.md`、`DEVELOPMENT_PLAN.md`。
- [x] 已核对 ADR-007、M5/M6a 证据、当前源码、分支、SHA 和未提交文件。
- [x] 项目负责人已说“开始开发”；本计划按“开始 M6b”处理。

### 3.2 计划审核门

- [x] 项目负责人批准本计划的范围与拆分。
- [x] 架构评审批准 ADR-012 的 target-aware Gateway 语义与兼容策略。
- [ ] 安全评审批准第 3.3 节的真实连接清单具体值；未齐备只阻塞现场连接。
- [x] 测试负责人批准第 9 节的离线与现场证据矩阵。

### 3.3 真实连接必备授权清单

以下每项都要写成明确值或批准记录；“沿用以前”“测试环境没关系”不算：

1. 唯一环境：只能是 `test`，不得是 pre/prod 或任何 E1 目标。
2. 唯一 canonical resource ID：不含 host、port、账号或 secret 的稳定逻辑 ID。
3. connector：host、port、默认 catalog/database 和 TLS 模式；不得由用户请求或 ToolCall 覆盖。
4. 物理身份核对方法与 expected identity reference。
5. 只读账号名与 password **file reference**；不接受明文环境变量、DSN 或聊天粘贴 secret。
6. `SHOW GRANTS` 的批准预期/摘要；账号只允许批准的 AuditLoader 表、物理身份只读对象和
   必要只读 preflight，不得有 SYSTEM OPERATE、写权限或业务库泛读。
7. AuditLoader 精确表、允许列、字段类型、query event 谓词、时间单位和值域。
8. 允许的 preflight：`SELECT VERSION()`、`SHOW GRANTS`、`SHOW CREATE TABLE`、物理身份
   只读查询；不注册成用户 capability。
9. 调用窗口：开始/结束时间、时区、唯一批准操作者、最大场景数。
10. 超时：建议候选为 connect 5 秒、write 5 秒、read 25 秒、server `query_timeout` 20 秒、
    ToolCall 30 秒；最终以批准值为准，三个 driver socket timeout 都必须严格小于 ToolCall
    预算，connect/write 不得超过 read，且 ToolCall 不能超过现有 profile 的 30 秒。
11. 最大结果：list 仍默认 20、硬上限 200；count 一行；不得扩大。
12. 脱敏：13 个字段中哪些允许进入 Evidence、CLI 和验收摘要；未批准默认不落真实 rows。
13. 原始 recording：是否允许产生；建议“不生成 driver packet/raw-row recording”。若必须产生，
    要给出批准位置、负责人、删除命令、删除时点和删除后的 readback 证据。
14. Evidence 保留：临时 PostgreSQL project 的保留时长、volume 销毁方式、WAL/备份口径；
    sanitized acceptance report 的保留周期单独说明。
15. 网络：来源主机/容器、目标 IP/DNS allowlist、端口、是否经代理、TLS CA file reference。
16. 可选 driver 流式探针是否单独授权；未批准时记录“未授权”，不阻塞 M6b。

expected version/grants/DDL/identity 的来源在真实连接前固定为以下主路径：

1. 负责人或 DBA 在小维调用窗口之外，通过批准的安全带外渠道提供 `SELECT VERSION()`、
   `SHOW GRANTS`、`SHOW CREATE TABLE` 与权威 identity 原始值/文件；不进入仓库、聊天记录或
   普通日志。
2. 计划批准后先通过离线 TDD 实现 normalizer；操作者再使用运行时**同一个** normalizer
   离线计算 digest，只输出 digest 与获批的
   脱敏 schema 差异，不连接 StarRocks。
3. 负责人/DBA 复核并批准 digest、normalizer version 与 schema 结论后，才允许把 digest
   引用注入 `test_readonly` 配置并申请现场 verification。

禁止使用本次待验证 connector 第一次返回的值，现场抄回配置后再宣称“匹配”；那是
trust-on-first-use，不能证明物理目标、权限或 DDL。若 DBA 无法提供带外材料，本计划保持阻塞，
另行增加并审核只做 discovery、不产出诊断结论的 Phase A；不得在现场临时决定或直接调用
adapter 绕过 Gateway。

### 3.4 物理身份方法的推荐与阻断条件

不建议为 M6b 账号授予 `SYSTEM OPERATE` 只为执行 `SHOW FRONTENDS`：这会明显超过
AuditLoader 只读最小权限。优先级如下：

1. **推荐**：DBA 预先提供一个只读单行 identity view，返回不可变的集群标识；该 view 与
   AuditLoader 表一起进入批准 scope，adapter 只比较 expected digest，不持久化原值。
2. 若基础设施能证明 TLS 证书唯一绑定目标集群，可批准证书身份 reference；但只证明 LB/DNS
   身份而不能证明后端集群时仍不够。
3. 两者都不可用时，物理目标只能写“未证明”，本次运行不能成为指定集群的
   `test-env verified`，M6b 不退出。

identity view 如需新建，由 DBA 在小维之外另行变更；小维 M6b 不创建 view、不提权、不执行 E1。

## 4. 目标架构与最小设计

### 4.1 保持不变的主链

```text
用户固定慢查询问法
  → RuleBasedIntentInterpreter（现有规则，不新增关键词）
  → CapabilityResolver
  → 现有 SlowQuery PlanCompiler（固定模板）
  → StepAdmission（ToolPolicy + SQLGuard）
  → AdmissionCertificate(target_fingerprint)
  → DeterministicToolGateway
  → 精确 (gateway, target_fingerprint) binding
  → StarRocksReadonlyAdapter
  → AdapterResponse
  → ToolResult
  → Evidence / Answerability / Render
```

M6b 不重建 Runtime、Runner、Resolver、Compiler、Policy 或 Render，不引入 SDK facade、连接池、
repository、provider registry、插件系统或通用数据库抽象。

### 4.2 target-aware binding

在 `tools/gateway.py` 增加一个窄的 frozen 内部值对象，例如
`TargetBoundAdapterBinding`，至少包含：

- `adapter`；
- `authorized_tenant`、`authorized_environment`、`authorized_actor`；
- `active_from`、`active_until`；
- 脱敏 `evidence_source_ref`；
- `config_revision`；
- `physical_identity_ref`。

`DeterministicToolGateway` 构造参数演进为两类显式注册：

```python
generic_adapters: Mapping[str, ToolAdapter]
target_adapters: Mapping[tuple[str, str], TargetBoundAdapterBinding]
```

第二个 tuple 元素是 `target_fingerprint`。兼容与拒绝规则：

1. 现有 fake/recording adapter 继续使用 `generic_adapters`，其行为与 hash 不变。
2. production composition root 只允许把真实 StarRocks 注册到 `target_adapters`；由逐文件
   依赖/装配安全测试防止误接到 generic map。Python 的 Protocol 不能提供语言级绝对隔离，
   ADR 不作超出语言能力的承诺。
3. 同一个 gateway 不得同时出现在 generic 与 target-bound 注册中，避免错指纹时回退 fake 或
   另一真实 connector。
4. gateway 一旦是 target-bound，必须精确命中
   `(call.gateway, admission.target_fingerprint)`；不命中时 adapter/网络调用次数均为 0。
5. 命中后仍在联网前检查 tenant/environment/actor 和有效时窗。
6. binding 的 source/config/identity metadata 由 composition root 提供，不采信 adapter payload。
7. 其他 gateway 不因 StarRocks 变更获得 target-aware 激活或真实网络权限。

不把 target、host、账号、secret path 或环境覆盖字段加入 `ToolCall`；用户和模型没有选连接权。

### 4.3 唯一目标与 selector 演进

- 将 `test` 目录收窄为负责人批准的单个 canonical resource ID。
- 目录不是 connector：只保存稳定逻辑 ID，不保存 host/port/user/secret。
- `SELECTOR_VERSION` 从 `1` 递增为 `2`，使旧 target fingerprint 在恢复时确定性漂移并拒绝。
- capability version 不因单纯 target 目录迁移递增；若现场 DDL 导致 SQL/字段/谓词语义改变，
  按第 7 节单独递增 capability version。
- `dev` 仍是 fake/recording；`prod` 继续不存在。
- 目标解析先于 adapter 选择，所以目录仍歧义时 `test` 环境的 recording 与真实装配都会拒绝；
  这不是网络激活信号，也不影响 `dev` recording。

### 4.4 配置与默认关闭

配置仍由 `config.Settings` 唯一加载、未知 `XIAOWEI_*` fail-fast。新增字段只在
`starrocks_adapter_mode="test_readonly"` 时必填，默认模式为 `recording`。

建议字段组：

```text
XIAOWEI_STARROCKS_ADAPTER_MODE=recording|test_readonly
XIAOWEI_STARROCKS_HOST
XIAOWEI_STARROCKS_PORT
XIAOWEI_STARROCKS_DATABASE
XIAOWEI_STARROCKS_USER
XIAOWEI_STARROCKS_PASSWORD_FILE
XIAOWEI_STARROCKS_TLS_MODE
XIAOWEI_STARROCKS_CA_FILE
XIAOWEI_STARROCKS_SERVER_NAME
XIAOWEI_STARROCKS_RESOURCE_ID
XIAOWEI_STARROCKS_EXPECTED_VERSION_SHA256
XIAOWEI_STARROCKS_EXPECTED_GRANTS_SHA256
XIAOWEI_STARROCKS_EXPECTED_DDL_SHA256
XIAOWEI_STARROCKS_EXPECTED_IDENTITY_SHA256
XIAOWEI_STARROCKS_AUTHORIZED_ACTOR
XIAOWEI_STARROCKS_ACTIVE_FROM
XIAOWEI_STARROCKS_ACTIVE_UNTIL
XIAOWEI_STARROCKS_CONNECT_TIMEOUT_SECONDS
XIAOWEI_STARROCKS_READ_TIMEOUT_SECONDS
XIAOWEI_STARROCKS_WRITE_TIMEOUT_SECONDS
XIAOWEI_STARROCKS_QUERY_TIMEOUT_SECONDS
```

约束：

- 默认 `recording` 时不得因为缺少任何 StarRocks 真实配置而失败，也不得打开 socket。
- `test_readonly` 只允许 `environment_id="test"`，所选 TLS/identity 模式对应的字段成组必填；
  半配置直接启动失败。
- password 只读有界单行 file，错误只返回固定配置类别，不回显路径或内容。
- 配置中的 resource ID 必须与 `resolve_target()` 产生的唯一 canonical ID 完全一致；不一致在
  composition 阶段拒绝，不能由配置覆盖目录，也不能由目录静默覆盖配置。
- TLS 默认 fail-closed；若现场确实不能 TLS，必须由负责人明确批准 `disabled` 及网络隔离风险，
  不能代码自动降级。
- config revision 对非 secret 连接配置、secret reference、目标 ID、超时、TLS policy、driver
  版本和 schema surface 做 canonical SHA-256；不包含 password 字节，不展示 host/user。
- expected version/grants/DDL/identity digest 必须带有已批准的来源引用与 normalizer version；
  `test_readonly` 不接受“尚未知，首次连接后再填”的配置。
- 所有真实字段只可来自进程级受信配置，ToolCall/IntentDraft/外部文本不能覆盖。
- `.env.example` 只写空引用/安全示例，不写真实 endpoint、账号、identity 或 digest。

### 4.5 driver 选择

M6b 计划采用 PyMySQL 1.2.x：StarRocks 官方说明其查询接口兼容 MySQL 协议，并在官方 Python
集成示例中使用 PyMySQL；它也支持独立 connect/read/write timeout 和 unbuffered cursor。

实现阶段新增：

- runtime：`PyMySQL>=1.2,<1.3`；
- dev typing：`types-PyMySQL>=1.2,<1.3`；
- 通过 `uv` 更新 `uv.lock`，不使用未锁依赖。

M6b 小结果主路径使用 call-local connection 和有界 buffered dict cursor，因为 SQL 已硬限制
最多 200 行；不为此提前引入连接池或 Gateway streaming。同步 driver 调用放入受控 worker
thread，socket timeout 小于 Gateway 30 秒硬截止，`finally` 关闭连接。Gateway 截止后不接收
迟到结果，不自动重试，不发送 `KILL`、`KILL QUERY` 或 cancel API。

这里不能假装 Python future 取消就等于 StarRocks 服务端查询已经停止。现场只能由同一低权限
账号在独立连接用 own-user `SHOW PROCESSLIST` 被动观察；无法可靠关联时记录“未证明，风险
保持开放”。

### 4.6 连接与 SQL 约束

真实 adapter：

- 构造时不联网，只有通过 Gateway 精确 binding 后才打开连接；
- 每个 ToolCall 使用一个 call-local connection，`autocommit=True`，不启用 local infile、
  multi-statements 或自动重连；
- 只执行已经 StepAdmission 校验过的 `typed_arguments["sql"]`；
- 再检查 operation 是 list/count 闭集、SQL 类型为 strict string、expected row budget 与模板一致；
- 固定服务端 session `query_timeout`，不接受 ToolCall 覆盖；
- list 最多读取 `row_limit + 1` 行，count 只允许一行；超量、缺列、额外列、重复列、类型异常
  全部返回结构化错误，不把半截 payload 当成功；
- adapter 只返回内部 `AdapterResponse`；真实 driver 对象、cursor、connection 和异常原文不
  越过 Gateway。

adapter 内部需要发送的 `SET query_timeout` 与 preflight SQL 都必须在 ADR-012 中明确归类为
固定、只读会话/握手语句，并由 runtime 闭集校验；不能因为它们“写在 adapter 里”就绕过
“SQL 确定性生成与 AST 校验”的架构原则。除批准的固定集合外，adapter 不得自行发 SQL。

### 4.7 preflight 与 evidence metadata

每个 call-local connection 都在同一个 Gateway 受控执行内完成 preflight，不跨调用缓存
version、grants、DDL 或 identity 结论，避免短窗口内权限/目标漂移仍沿用旧结果。preflight
按固定顺序包含：

1. 设置 session `query_timeout` 并 readback；
2. `SELECT VERSION()`，规范化后与批准 digest 比较；
3. `SHOW GRANTS`，规范化后与批准 digest 比较；
4. `SHOW CREATE TABLE`，规范化后与批准 digest 及本地 `SqlSurface` 比较；
5. 固定 identity view 查询或批准的等价身份核对。

version、`SHOW CREATE TABLE`、identity 与 timeout readback 必须符合受审的精确列契约；
`SHOW GRANTS` 只接受恰一列并比对值 digest，其可能包含账号/版本差异的动态列标签不作为
身份或权限判断输入。

任何不匹配都在主查询前失败，不进入成功 Evidence。原始 grant、DDL、identity、host、user 和
异常文本只在内存中比较并立即丢弃，不写日志、TaskStore、Evidence、recording 或报告。

target-bound Gateway 在成功 `ToolResult` 中签发可信、脱敏 metadata：

- canonical target fingerprint；
- config revision；
- physical identity reference；
- driver version；
- preflight verdict。

服务端版本只在内存中规范化并与批准 digest 比较；原值不进入现场报告，也不冒充 Gateway
签发的可信配置。上述可信 metadata 使用现有 `source` /
`limitations` 可表达的安全引用，不给 `EvidenceEnvelope`
机械增加字段。慢查询 Evidence builder 必须消费同一个已准入 `ResolvedTarget`，验证 test +
单资源形状，并保留 Gateway 签发的安全限制；adapter payload 不能覆盖来源或目标。

### 4.8 真实 rows 与脱敏

目前 13 列都会进入 Evidence：

```text
queryId, timestamp, queryTime, scanRows, returnRows, scanBytes,
memCostBytes, pendingTimeMs, cpuCostNs, state, errorCode, db, user
```

因此不能只检查 Render 的三列。实现前按第 3.3(12) 固定以下之一：

- 批准这 13 列在隔离测试 Evidence 中短期原样保存，并在批准时点销毁整个临时存储；或
- 明确字段级 masking/profile，并让 Evidence 的 `redaction_ref` 非空；或
- 缩小 Evidence facts 白名单。若缩小会改变 capability evidence 语义，应递增 capability
  version，而不是同版本静默修改。

没有明确选择时，真实调用保持关闭。

## 5. ADR-012 要冻结的决策

新增：

`docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md`

至少写清：

1. 为什么仅按 gateway 选择真实 adapter 不能证明目标归属。
2. generic fake 与 target-bound real 两类注册的兼容关系。
3. 同 gateway 禁止双注册、错 fingerprint 禁止 fallback。
4. tenant/environment/actor/window 在联网前的强制检查。
5. config revision 与 physical identity reference 的来源、hash 输入和不含字段。
6. adapter 固定 handshake/preflight SQL 的闭集、AST 校验和 E0/E1 分类。
7. ToolGateway/ToolAdapter/ToolResult/Evidence 的职责不变与最小语义变化。
8. 默认关闭、CI 零真实连接、真实 activation 的开始/失效/回滚。
9. selector version 迁移：旧 target/plan 在恢复时漂移拒绝，不做兼容回退。
10. 真实 Evidence 的隔离存储、逻辑删除/volume 销毁口径及无法承诺物理擦除的限制。
11. M6b 只支持现有两个 operation；后续通用 SQL/streaming 不从本 ADR 自动获得授权。
12. 回滚：移除 target-bound StarRocks 注册并恢复 `recording`；不改 E1 硬闸。

ADR 审核前不写 Gateway 实现。

## 6. 实现文件与责任边界

### 6.1 预计新增

| 文件 | 单一职责 |
| --- | --- |
| `docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md` | target-aware 语义与迁移真源 |
| `src/xiaowei_agent/tools/starrocks.py` | 真实 StarRocks 小结果 adapter 与内部 connection protocol |
| `tests/unit/test_starrocks_adapter.py` | 无 socket 的 driver/row/error 单测 |
| `tests/contract/test_starrocks_adapter_runtime.py` | 现有 Runtime 到真实 adapter 形状的离线闭环 |
| `tests/security/test_starrocks_live_boundary.py` | 默认关闭、目标/窗口/配置/secret/零网络安全矩阵 |
| `tests/evals/corpus/m6b_starrocks_l0.json` | M6b 专属安全/畸形响应语料 |
| `tests/evals/corpus/m6b_starrocks_l2.json` | golden/empty/failure 运行语料 |
| `tests/evals/test_m6b_starrocks_l0.py` | L0 执行器 |
| `tests/evals/test_m6b_starrocks_l2.py` | L2 执行器 |
| `scripts/m6b_starrocks_digest.py` | 从 stdin/受限文件离线调用同一 normalizer，只输出 digest/脱敏差异 |
| `tests/contract/test_m6b_starrocks_digest.py` | 离线 digest 工具与 runtime normalizer 同源、无网络/无原文输出 |
| `docker-compose.m6b-test.yml` | 无真实值的人工验证 override；默认不被普通 Compose 使用 |

`docs/handoff/M6b-acceptance-report.md` 只在真正完成验证后新增，不能先写空成功模板冒充证据。

### 6.2 预计修改

| 文件 | 修改目的 |
| --- | --- |
| `src/xiaowei_agent/tools/gateway.py` | target-bound registration 与联网前校验 |
| `src/xiaowei_agent/capabilities/target.py` | test 唯一资源与 selector v2 |
| `src/xiaowei_agent/config.py` | 默认关闭的条件配置与 fail-fast |
| `src/xiaowei_agent/interfaces/local_stack.py` | composition root 根据受信模式装配 exact binding |
| `src/xiaowei_agent/evidence/builder.py` | 保留 Gateway 签发的安全 metadata；不信 adapter rows 决定目标 |
| `src/xiaowei_agent/application/default_capabilities.py` | slow-query builder 使用已准入 target 做单资源验证 |
| `pyproject.toml`、`uv.lock` | 锁定 driver 与 typing stub |
| `.env.example` | 只记录字段名与默认关闭口径，不放真实配置 |
| `docker-compose.yml` | 默认仍 recording；仅在必须共享安全 anchor 时最小修改 |
| `tests/unit/test_target_resolver.py` | 唯一目标、selector v2 与指纹变化 |
| `tests/unit/test_config_happy.py` | 默认关闭与完整 test 配置 |
| `tests/unit/test_local_stack.py` | recording/real 装配分离、构造不联网 |
| `tests/fakes/fixtures.py` | 注明双资源 `FIXTURE_TARGET` 是 hash/契约合成值，不代表生产 target |
| `tests/security/test_gateway_boundary.py` | exact binding、无 fallback、窗口与操作者 |
| `tests/security/test_config_fail_fast.py` | 半配置/明文 secret/未知变量拒绝 |
| `tests/security/test_no_network.py` | import、构造、CI/default 零 socket |
| `tests/security/test_module_layering.py` | driver 只能位于 tools/composition 边界 |
| `tests/unit/test_evidence_builder.py` | target/config/identity 安全引用与字段 masking |
| `tests/evals/test_l0_security.py`、`test_l2_readonly.py` | 证明既有 fake 行为未回归 |
| `ARCHITECTURE.md` | 记录正式 Gateway target-bound 语义与 ADR 索引 |
| `README.md` | 只增加批准后的人工 M6b 导航，不复制完整安全设计 |
| `AGENT_HANDOFF.md` | 记录当前候选 SHA、验证状态、阻塞和禁止盲改点 |

### 6.3 明确不改

- 不增加 capability ID、operation、关键词、自然语言规则或菜单。
- 不修改通用 SQLGuard 为支持 JOIN/CTE/任意 SQL。
- 不修改 `ToolCall`、`ExecutionPlan`、plan schema 或 hash canonicalization。
- 不提高 `MAX_READONLY_TIMEOUT_SECONDS`，不影响 Prometheus/资产 profile。
- 不实现 Gateway streaming、Result Artifact、CSV、Web、飞书、审批下载或 TiDB。
- 不修改 `_E1_EXECUTION_ENABLED=False`。
- 不让领域层 import PyMySQL，不新增并列 `integrations/` 目录。

## 7. DDL/语义发现后的版本分支

真实 DDL 现在未知，计划不能预先假装 13 列和谓词正确。现场 preflight 先只做批准的只读核对，
然后严格分两类：

### 7.1 无语义变化

若表名、13 列、类型/单位、时间语义、query event 范围、count 分支均与现有声明一致，只补
真实 adapter，不改 capability version、模板 ID、surface ID 或 snapshot ID。

### 7.2 有语义变化

若任一项不一致：

1. 立即停止主查询，不在 adapter 中做动态列别名、静默类型转换或表名 fallback。
2. 把差异只以脱敏 schema 事实记录给评审，不保存业务 rows。
3. 修订本计划的精确 SQL 语义并再次审核。
4. 更新 `CapabilitySpec`、`SqlSurface`、compiler、SQLGuard、Evidence、Render/eval 所需部分。
5. capability 至少递增为 `1.1.0`，对应模板/surface/evidence/snapshot ID 一起迁移；旧版本不
   指向新 SQL。
6. 重新跑全部安全门和 hash/golden 回归后，再申请新的真实调用窗口。

不能以“让 adapter 兼容两套字段”绕过版本化。

## 8. TDD 实施顺序

计划批准后，从最新 `main` 创建 `claude/m6b-starrocks-test-readonly`，先完成完全离线的 TDD。
第 3.3 节尚未满足不阻塞 normalizer、配置、Gateway 接缝和 adapter 的 fake/injected 实现，
但读取真实 secret、打开真实 socket 及任何现场验证仍保持禁止。一个 PR 只处理 M6b；按以下
顺序小步提交。

### Task 1：ADR 与默认关闭配置

**先写失败测试**：

- 默认 `recording`，没有 StarRocks 变量也可启动且 socket 调用为 0。
- `test_readonly` 下缺任一必填字段、非 test 环境、窗口逆序/过期、超出 30 秒、明文 password
  变量、非法 digest/TLS 模式全部失败，错误不回显取值。
- config revision 同输入稳定；host、resource、TLS、timeout、secret reference 任一变化会变化；
  password 内容轮换不进入 revision 或日志。

**最小实现**：ADR-012、Settings 字段/validator、canonical config revision；不加入 driver。

**绿灯**：相关 unit/security + `ruff check .` + `mypy src`。

### Task 2：唯一 target 与 Gateway exact binding

**先写失败测试**：

- test 目录恰一个 approved resource，selector 为 v2；空/多目标真实装配拒绝。
- production composition root 把真实 StarRocks 注册到 generic map 时，逐文件装配安全测试失败。
- 同 gateway generic + target-bound 双注册被拒绝。
- 错/未知 target fingerprint、tenant、environment、actor、未开始/已过期窗口：adapter 调用 0、
  socket 调用 0。
- exact match 才调用一次；binding metadata 不能由 AdapterResponse 覆盖。
- Prometheus、Alertmanager、asset 与 StarRocks recording 既有用例逐字节行为不变。
- `tests/fakes/fixtures.py::FIXTURE_TARGET` 保留 `("c1", "c2")` 与 selector `"1"` 作为独立
  hash/契约合成值，但补注释说明它不是 `resolve_target()` 可产生的正常 production 形状。

**最小实现**：internal binding dataclass、两类映射、clock 注入、联网前检查、target selector v2。

**TDD 反证**：临时恢复 `self._adapters.get(call.gateway)`，确认错 fingerprint 安全测试变红；
临时允许 fallback，确认双注册/错目标测试变红。变体只在独立临时 worktree 和 pycache 中运行。

### Task 3：真实 adapter 的无网络单测

**先写失败测试**：

- 构造不连接；只有 Gateway exact match 后 connection factory 调用一次。
- list/count 之外 operation 拒绝且 connection factory 0 调用。
- ToolCall 试图塞 host/user/password/TLS/target 覆盖字段时拒绝或完全忽略，不能到 driver。
- password file 为空、过大、多行、NUL、不可读、symlink/非普通文件按批准规则失败且不泄漏。
- 每次调用只执行批准的 preflight/session/main SQL 闭集；main SQL 与已准入字符串一致。
- server query timeout 固定；local infile/multi-statements/reconnect 关闭；连接总在 finally 关闭。
- list 正常/空/200 行，201 行拒绝；count 正常/空/多行拒绝。
- 缺列、额外列、重复列、列顺序、NULL、datetime/Decimal/int/string 类型边界。
- permission/auth/connect/read/write/server timeout/断连/异常 `__str__` 均映射为结构化错误，
  原始文本只成 ExternalContent digest，不进可信 message/facts/log。
- version、grants、DDL、identity、query_timeout 任一漂移时主 SQL 执行次数 0。
- 离线 digest 工具与 adapter preflight 调用同一个 normalizer；相同输入/version 产生相同
  digest，stdout/stderr 不出现原文，执行期间 socket 调用为 0。

**最小实现**：`tools/starrocks.py` + injected connection/cursor Protocol + PyMySQL factory；
`scripts/m6b_starrocks_digest.py` 只复用其中的纯 normalizer，不复制算法、不包含网络客户端。

**TDD 反证**：分别拆掉 operation 闭集、row cap、identity compare、DDL digest compare 和 connection
close，确认对应安全/单元测试真实转红。

### Task 4：composition、Evidence 与离线全链

**先写失败测试**：

- default local/Compose stack 仍装配 recording；CI/env 无真实配置时真实 driver import/调用为 0。
- real mode 只把 StarRocks gateway 放入 exact target map；其他 gateway 仍 generic recording。
- API/Worker 相同配置产生同 config revision/target binding；readiness 不以启动时真实联网冒充健康。
- 真实 shaped adapter response 经 Runner 形成 Evidence/Outcome/Render；target/config/identity 安全
  metadata 可复核，host/user/secret/DDL/grants 不可见。
- generic Gateway 的固定 timeout/exception limitation 保持为工具失败证据；只有 target-bound
  保留前缀能触发装配错位拒绝，且 StarRocks/Prometheus/资产 builder 口径一致。
- Evidence target 不是 test、不是单资源或与 binding 不符时 fail-closed。
- golden、empty-with-traffic、empty-without-traffic、timeout、permission、malformed 各自维持现有
  succeeded/indeterminate 语义。
- 指定 masking profile 后 Evidence/CLI/trace/task/audit 均不出现受限字段。

**最小实现**：LocalStack exact registration、Evidence target 验证与 trusted limitations 传递、
无真实值的 Compose override。

### Task 5：依赖锁、文档和回归

- 用 `uv` 增加并锁定 PyMySQL/typing stub。
- `pip-audit` 仍消费同一 lock；不新增第二个依赖真源。
- 更新 ARCHITECTURE/README/AGENT_HANDOFF 与 ADR 索引；不手工编辑生成的
  `docs/CAPABILITIES.md`，因为 capability 没新增且 readiness 在完成前不能升级。
- 用 AST/依赖测试证明 PyMySQL 只在 `tools/` 和 composition root 边界出现。
- 扫描 repo、diff、日志和测试产物，确认没有真实 endpoint、账号、secret reference 实值、DDL、
  grant、identity 或业务 row。

### Task 6：离线验收后暂停

完成全部 fake/recording/注入 driver 测试和 ADR-008 四门后，提交精确候选 SHA 给 Codex/Claude
审查。此时结论仍只能是 `tests`。没有“现场 GO”不得连接 StarRocks。

## 9. 测试与 Eval 矩阵

### 9.1 Unit

- 配置条件校验与 config revision。
- target 目录/selector/fingerprint。
- Gateway exact binding 选择。
- adapter operation、preflight、row codec、type/NULL、budget、error mapping、cleanup。
- Evidence target/metadata/masking。

### 9.2 Contract

- `ToolGateway` Protocol 签名保持兼容；target binding 是具体 Gateway 构造能力，不把 connector
  暴露给领域层。
- `ToolAdapter.execute(call, context)` 与 `AdapterResponse` 形状不变。
- Runtime/Runner/StepAdmission/Gateway/adapter/Evidence 的 DTO 边界。
- API 与 Worker composition 对同一 profile 的 revision 一致。
- 现有 fake/recording adapters 继续通过共享 suite。

### 9.3 Security

- 所有目标、配置、窗口和权限拒绝路径均断言 adapter 与 socket 计数。
- SQLGuard 绕过、multi-statement、注释、未知模板、operation 替换、ToolCall 参数污染。
- target fingerprint 替换、证书重放、policy revision/target drift。
- secret-shaped literals、错误/trace/config 泄漏、恶意 driver 对象与异常。
- driver 只出现在 tools/composition，领域/入口不持有客户端。
- default/CI 无 StarRocks 网络；真实 activation 不能由一个布尔开关单独打开。
- E1 硬闸与 production 缺席保持原样。

### 9.4 Integration

- PostgreSQL TaskStore + target-bound shaped adapter 的完整任务生命周期；不连接 StarRocks。
- API submit → Worker execute → CLI get 的离线 exact-binding 闭环。
- 进程重启时 target/selector/config revision 漂移后的拒绝语义。
- 临时 PostgreSQL project 的明确销毁/readback 脚本先以空 fixture 验证目标范围，避免误删其他
  volume。

### 9.5 Eval

- L0：错 target、错 actor、过期窗口、权限/DDL/identity 漂移、畸形响应、SQL 污染。
- L1：不新增意图规则；现有慢查询问法与澄清语义不回归。
- L2：golden、空但有流量、空且无流量、scope 外有流量、timeout、permission、malformed。
- 不用 LLM-as-judge 裁决安全或正确性。

### 9.6 固定四门

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

触及 `tools/`，security gate 不得省略。若 PostgreSQL integration 因 DSN 缺失而 skip，必须在
获批隔离 PostgreSQL 环境重跑并证明 0 integration skip；不能把 skip 写成通过。

## 10. 受控真实验证 runbook

这一节只有在离线候选 SHA 通过审查、负责人再次明确“现场 GO”、第 3.3 节全部有值后执行。

### 10.1 启动前 readback

1. 确认 expected version/grants/DDL/identity 的原始材料来自第 3.3 节批准的带外来源，不来自本次
   connector；用实现中的同一 normalizer 离线计算 digest，记录 normalizer version。
2. 由负责人/DBA 复核 digest 与脱敏 schema 差异并留下批准引用；未批准不得继续。
3. 记录候选 commit SHA、parent、diff 和 lock 中 driver 精确版本。
4. 再次确认时间位于批准窗口、操作者一致、environment=`test`。
5. 对配置文件/secret file 只检查 owner、mode、大小和 reference；不输出内容/路径实值。
6. 比对 target fingerprint、config revision、已批准的 expected version/grants/DDL/identity digest。
7. 用唯一 Compose project name 启动临时 PostgreSQL/API/Worker；普通
   `docker-compose.yml` 默认模式仍 recording。
8. `/readyz` 只表示本地 DB/migration/assembly 就绪，不把它当 StarRocks 连通证明。

### 10.2 preflight

经相同 Gateway exact binding 触发一个受控慢查询任务，让 adapter 完成：

- version digest；
- grants digest；
- DDL digest/本地 surface；
- physical identity；
- query_timeout readback。

任一不符立即停止，不运行主查询。若 DDL 语义不同，走第 7.2 节重新计划，不现场热修。

### 10.3 真实场景

批准操作者通过现有 CLI 完成 3-5 个场景，建议最小集合：

1. golden：批准短时间窗，返回 1-20 条；
2. empty-with-traffic：目标过滤无慢查询但 count 有范围内流量；
3. empty-without-traffic：list/count 均空，结果必须 indeterminate；
4. 受控无结果：使用一个格式合法、位于批准查询范围内但不存在的 `query_id`，验证明确的
   indeterminate/重试提示；
5. 可选 timeout：只有负责人批准且不会造成集群压力时执行。

权限拒绝、账号认证失败和范围外对象访问只由 Task 3 的注入式离线测试承重。现场窗口不为
制造错误而访问未批准对象，也不把 fake/fixture 错误混入真实验证记录。

每个场景记录：task/evidence 安全引用、状态、计数、限制文案、trace digest、开始/结束时间、
operator observation。不得复制业务 rows、原始 SQL、host、账号、DDL/grants 或错误正文到报告。

### 10.4 超时与服务端残留观察

- 客户端/Gateway 超时后立即将任务收口为 timeout/indeterminate，丢弃连接和任何迟到 payload。
- 不发送 KILL/cancel。
- 仅在批准时用同一账号独立连接执行 own-user `SHOW PROCESSLIST`；能可靠关联才记录观察结果。
- 不能关联或权限不足就写“服务端停止未证明”，不提高权限、不猜。

### 10.5 可选 driver 流式探针

只有第 3.3(16) 单独批准才执行。它不新增 operation、不触碰业务库，只在相同 AuditLoader
scope 用 driver unbuffered cursor 做一次固定查询，记录：

- driver/服务端版本与 cursor 模式；
- execute 调用次数必须为 1；
- execute 前后、首批及逐批 RSS/内存增长；
- 首批延迟、总完成时间、总行数/字节的聚合值；
- 结论严格三态：`server-side streaming 已证明` / `客户端全缓冲已证明` / `未证明`。

不保存 rows。样本不足或信号冲突必须写“未证明”。该结果不改变 M6b 退出门；但未证明或
全缓冲时，不得在后续 ADR/SR-X1 把该 driver 宣称为完整结果流式实现，SR-Q2 仍须复核。

### 10.6 清理与保留 readback

1. 在批准时点停止 API/Worker，撤掉 real activation 和 secret mount。
2. 只针对启动前记录的唯一 Compose project/volume 执行批准的销毁命令；禁止 unresolved glob、
   broad directory、`docker volume prune` 或影响其他项目的命令。
3. readback：该 project service/volume 不再存在；secret reference 由负责人按外部流程撤销/轮换。
4. raw terminal/recording 如获准产生，按批准位置逐个删除并 readback；不声称 PostgreSQL/WAL/
   备份物理不可恢复。
5. 保留脱敏 acceptance report 和批准要求的安全摘要；其到期处理由批准策略决定。

删除属于破坏性动作，执行时必须再次取得用户对精确 project/volume/recording 目标的授权。

## 11. 完成条件

### 11.1 Offline implementation exit

- ADR-012 已批准。
- 计划内代码和测试全部完成，真实配置默认关闭。
- target/fingerprint/physical identity 三层都有 fail-closed 测试。
- unit/contract/security/integration/L0-L2 与 ADR-008 四门通过。
- 承重保护的变异反证真实转红并已还原。
- 精确候选 SHA diff 通过独立审查。
- repo/diff/log/fixtures 无真实敏感值或业务 rows。

此时只可标记 `tests`。

### 11.2 Test-env verified exit

- 第 3.3 节授权完整且现场仍在有效窗口。
- 目录、target fingerprint、connector config revision、physical identity 全部指向同一批准集群。
- version、grants、DDL、identity、query_timeout preflight 通过。
- golden、empty 和不存在 query_id 的受控无结果路径至少三类真实场景完成；权限/认证失败
  已由离线注入测试承重，异常路径未伪装成功。
- rows/字段/值域/count 语义已核对；不确定项原样记录。
- 原始 recording/Evidence 按批准周期清理并完成 readback；只保留批准的脱敏证据。
- acceptance report 按“已验证、只读推理、未覆盖、残余风险”填写。
- 项目负责人明确验收；没有这句话不归档、不自行合并。

## 12. 不做什么

- 不新增 Prometheus、资产或其他数据库诊断能力。
- 不实现 `starrocks.query.readonly`、通用 SQL、复杂 JOIN/CTE、Result Artifact 或导出。
- 不新增自然语言关键词，不启动 SR-I0/SR-I1，不调用真实模型。
- 不实现 Profile/Load/Task/MV/Tablet/分区等 SR-O 能力。
- 不接 Web/飞书，不把 CLI observation 写成渠道或用户验收。
- 不连接 E1/生产，不执行 KILL/cancel，不修改 StarRocks 对象/账号/权限。
- 不用真实结果训练、评测或构造长期 fixture。
- 不自行 merge、deploy、canary、archive。

## 13. 预期风险与停止条件

### 13.1 必须停止并重新审核

- 真实 DDL/字段/谓词与 1.0.0 不同。
- 账号权限比批准范围更宽，或需要 SYSTEM OPERATE/写权限才能完成。
- target/config/physical identity 无法闭合。
- TLS/网络边界与批准不一致。
- driver 需要额外 auth/crypto 依赖或必须关闭证书校验。
- 需要修改 DTO、plan schema、hash canonicalization、Runner 生命周期或 E1 硬闸。
- 原始 rows 无法按批准周期从临时存储清理。
- 现场窗口过期、operator 变化或 secret reference 漂移。
- expected version/grants/DDL/identity digest 不是来自获批带外来源或已批准的独立 discovery Phase A，
  或试图用本次连接的返回值自产自证。

### 13.2 即使通过仍存在的残余风险

- 一个测试集群、一个 AuditLoader 表、3-5 个场景不能代表生产版本、权限、数据量和负载。
- PyMySQL worker thread 的取消不等于服务端查询停止；socket/server timeout 只能限制风险，
  不能在未观察时证明无残留。
- `SHOW PROCESSLIST` 是否能在现场版本可靠关联 own-user query 尚未验证。
- PostgreSQL volume 删除不证明 WAL、快照、备份或宿主介质已物理擦除。
- AuditLoader 如果汇聚多个被观测集群，执行 target 与 row subject 仍需分别解释；没有可靠
  subject 字段时不能宣称诊断了某个来源集群。
- M6b 的 buffered 200-row adapter 不能证明后续 100,000 行完整结果/导出的真流式能力。

## 14. 计划审核后的下一步

审核人应明确返回：

1. “M6b 计划批准”或逐条修改意见；
2. 第 3.3 节 1-15 的批准值/引用；
3. 可选 driver 流式探针“批准”或“不批准”；
4. Evidence 13 列采用原样短期保存、字段 masking，还是缩小白名单；
5. 物理身份采用 identity view、TLS 唯一身份，还是当前无法证明。

计划批准后可以开始离线 TDD。进入真实 verification 前，还必须确认 expected digest 的批准
带外来源；若无法提供，则先修订并审核独立 discovery Phase A，不得直接连接后自产自证。

在明确收到“M6b 计划批准”前，本任务停在 Review Draft，不创建实现分支、不安装依赖。批准
计划但上述真实授权尚未齐备时，只做离线实现，不读取真实 secret、不连接测试环境。

## 15. 官方文档输入（不是测试环境实证）

- [StarRocks JupySQL/PyMySQL 集成](https://docs.starrocks.io/docs/integrations/IDE_integrations/jupysql/)：
  StarRocks 使用 MySQL 协议，官方 Python 示例采用 PyMySQL。
- [StarRocks system variables](https://docs.starrocks.io/docs/sql-reference/System_variable/)：
  `query_timeout` 为会话变量；M6b 仍须在现场版本 readback，不能只引用文档。
- [StarRocks SHOW GRANTS](https://docs.starrocks.io/docs/sql-reference/sql-statements/account-management/SHOW_GRANTS/)：
  当前文档说明用户可查看自身权限；现场账号/版本仍须验证。
- [StarRocks SHOW CREATE TABLE](https://docs.starrocks.io/docs/sql-reference/sql-statements/table_bucket_part_index/SHOW_CREATE_TABLE/)：
  当前文档要求目标表 SELECT 权限。
- [StarRocks SHOW FRONTENDS](https://docs.starrocks.io/docs/sql-reference/sql-statements/cluster-management/nodes_processes/SHOW_FRONTENDS/)：
  当前文档要求 SYSTEM OPERATE 或 `cluster_admin`，因此不作为低权限 M6b 的默认身份方案。
- [PyMySQL Connection API](https://pymysql.readthedocs.io/en/latest/modules/connections.html) 与
  [Cursor API](https://pymysql.readthedocs.io/en/latest/modules/cursors.html)：driver 超时与
  buffered/unbuffered cursor 能力的文档依据；真实取消/内存行为仍必须由现场证据确认。
