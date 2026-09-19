# 小维 Agent 2.0

小维 Agent 2.0 是从 0 开始建设的策略治理型运维工作流 Agent：模型负责理解和解释，确定性系统负责规划、授权、执行、取证和恢复。

> 当前状态：M0–M6a 已通过项目里程碑验收并归档。M6b 默认关闭的 StarRocks 测试环境只读
> adapter 已完成离线实现、审查并合入 `main`，真实验证已延期，最强证据仍为 `tests`。M7 PR 1–8
> 已全部审查并合入；PR #27 的最终受审 head `c50d820` 已以 squash commit `ba5ecfe5` 合入
> `main`，至此 M7 离线实现范围 8/8 完成。最终 PR 与合入后 main 的八项 CI 均全绿；本机有
> Docker client、standalone Compose 与可用的 Colima daemon，但未提供 PostgreSQL DSN；
> 隔离 PostgreSQL/Compose 运行证据仍来自旧版 GitHub CI。项目负责人已于
> 2026-09-09 按**离线范围**验收并授权归档，历史事实见
> [M7 离线范围归档](docs/handoff/archive/2026-09-09-M7-web-feishu-offline.md)；这不表示 M7 的
> 真实渠道退出标准已经通过。
> RI1 默认关闭的真实 OAuth adapter、Web 装配与 Compose 契约通过 PR #31 交付；最高证据仍为
> `tests`。它没有部署或连接真实飞书。
> RI3 的 Gemini 接入按 5 个 PR、2 次 migration 设计；ADR-015 与 V7.1 详细实施计划已于
> 2026-09-12 通过复审并获“开始 RI3”离线开工授权。PR 3A–3D 已合入；当前源码已有严格
> DTO、两个窄 port、固定 Gemini SDK adapter、默认关闭装配、worker-only Compose secret、
> durable Runtime、持久模型 artifact、MODEL trace、慢查询 advisory 和显式 Web 父任务上下文。
> PR #38 已以 `fb6718cc` 合入；真实 key 读取与 Gemini 网络调用均为 0。
> Independent review V7 has corrected the plan's timeout/idempotency/Compose facts,
> preserved Runner's one-shot grant renewal, fixed projector package ownership and
> completed its test surface. PR 3B is an offline SDK boundary only. Host
> Gemini key stays outside Settings and `.env.example`; it lives in a Git-ignored host
> file and is exposed only as a worker-only Compose secret when the explicit model
> override is used.
> 真实应用、凭据、网络连接、部署与 canary 仍被独立硬门阻塞。项目**尚未连接任何真实
> 运维系统或模型 API**，也未部署、未 canary、未取得产品用户验收。
> 智能交互入口 I0-DOC 已绑定 [ADR-017](docs/adr/ADR-017-intelligent-interaction-and-clarification.md)。
> 当前工作树正在实现 I1-A：已有 interaction classifier、insert-once interaction artifact、确定性
> Router 与 Runtime 接入切片、终态澄清存储与澄清子任务一次性消费；可信槽位、`ReadClass` 和执行披露屏障仍未实现，
> 不能把本地离线切片写成完整 I1、真实模型、部署或用户验收。
> 当前精确进度见 [AGENT_HANDOFF.md](AGENT_HANDOFF.md)。

未来产品可包含 Admin 配置治理与模型 API 等控制面，但这些不是 M7 交付物，也不是当前已实现
事实。M7 主工作台只适配桌面端，窄屏仅保证从飞书卡片进入的单任务安全详情可读。

## 先看什么

阅读顺序以 [AGENTS.md](AGENTS.md) 的「优先阅读」一节为唯一真源，本节与其保持一致：

1. [AGENTS.md](AGENTS.md)：Codex、Claude 和开发者必须遵守的开发纪律与安全边界。
2. [ARCHITECTURE.md](ARCHITECTURE.md)：目标架构、模块边界、核心数据契约和演进门槛。
3. [AGENT_HANDOFF.md](AGENT_HANDOFF.md)：当前阶段、已验证事实、风险与下一步。
4. 本文件：人类开发者的启动和导航信息。
5. [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)：已获项目负责人批准（2026-09-01）的实施路线、决策门与退出标准。

授权边界的真源是 [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)，工程与测试工具链的真源是 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)，`plan_hash` 规范形状与工具准入的真源是 [ADR-009](docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md)，多能力 binding 与固定 PromQL 准入见 [ADR-011](docs/adr/ADR-011-m6a-capability-binding-and-promql-template-admission.md)，M6b 精确目标绑定见 [ADR-012](docs/adr/ADR-012-m6b-target-bound-starrocks-readonly-adapter.md)，M7 Web/飞书薄渠道边界见 [ADR-013](docs/adr/ADR-013-m7-channel-boundary.md)，真实飞书 OAuth/Web 激活见 [ADR-014](docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md)，已接受的 Gemini 窄端口与数据边界见 [ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md)。智能交互入口、澄清链、`ReadClass` 与执行披露屏障见 [ADR-017](docs/adr/ADR-017-intelligent-interaction-and-clarification.md)；它是 I1 的实施门，不是当前运行证据。

## 目标能力

第一阶段不是追求“什么都能做”，而是先把一条可验证的只读闭环做扎实：

1. 用户通过 API/CLI 发起自然语言运维问题。
2. I1 先形成 `InteractionArtifact`，由确定性 Router 判断普通对话、资料查询、日志分析、需要澄清或 capability 请求。
3. 只有明确的 capability 请求进入 Resolver；缺槽进入 `CLARIFICATION_REQUIRED` 终态澄清。
4. `SlotVerifier` 把本轮文本和澄清父记录中的可信槽位升级为专属 Params，Planner 生成受约束计划。
5. Policy 和对应的 SQL AST / 固定模板 PromQL Guard 检查工具调用，并在首次 Gateway 前完成执行披露。
6. ToolGateway 访问外部系统，生成带来源和限制的证据。
7. Reflection 只消费已生成的结构化证据，判断是否足以回答并说明限制、缺失和降级；它不追加步骤、不选工具、不改计划。确需额外取数时，只能是计划中预编译的预算内只读分支，由 Runner 按确定性条件执行。
8. Runtime 生成可审计、可复现的最终回答和任务结果。

后续再逐步加入审批写操作、飞书/Web 渲染、更多资源域和 LangGraph runner。能力扩展不应改变上述核心契约。

## 目标架构

```text
用户 / API / Web / 飞书 / CLI
              │
              ▼
       Agent Gateway
   auth · tenant · trace · env
              │
              ▼
        XiaoweiRuntime
 InteractionArtifact → Router → Resolver → SlotVerifier → PlanCompiler
                                      │
                              PlanStore / Disclosure
                                      │
                              WorkflowRunner
                                      │
                       StepAdmission（每个步骤边界）
                 ToolPolicy → QueryGuard → ApprovalGate*
                                      │
                                ToolGateway
                                      │
             ┌────────────────┬───────┴────────┐
             ▼                ▼                ▼
         Evidence          Memory           RenderPayload
             │                │                │
             └────────── TaskStore ───────────┘
                              │
                    PostgreSQL / Worker / Compose

* ApprovalGate 只在具体副作用步骤前触发；未批准时持久化暂停。
```

入口层不拥有业务判断；模型不拥有执行权；外部文本不拥有策略修改权。完整边界和状态语义见 [ARCHITECTURE.md](ARCHITECTURE.md)。

能力现状记录在 `docs/CAPABILITIES.md`，详细 handoff 历史记录在 `docs/handoff/archive/`。这两个路径属于项目治理骨架：**已归档的里程碑以 `docs/handoff/archive/` 目录内容为准**——本文件不维护会随每次验收增长的清单；`docs/CAPABILITIES.md` 已由 M3 建立，其条目由 Registry/代码生成并由测试检查，**不要手工编辑**。

## 初始技术基线

目标基线是模块化单体，统一 Docker Compose 开发环境：

- Python 3.11（首个且当前唯一强制验证的版本；其他版本未测试，不宣称支持）、FastAPI、Pydantic、SQLAlchemy、Alembic；依赖由 uv 解析并锁定在 `uv.lock`。
- pytest、pytest-asyncio；安全测试使用 `security` marker 作为显式 CI gate。
- Ruff 作为唯一 linter，mypy 作为唯一类型检查器。**本阶段不启用任何自动 formatter**：PEP 8 + Ruff lint 是唯一格式 gate。
- PostgreSQL 作为 TaskStore、审批、证据索引和运行审计的事实存储。
- 一个镜像同时支持 API Gateway 和 Worker，先以进程角色区分，不提前拆微服务。
- `google-genai==2.23.0` 仅用于固定 structured JSON 生成；生产 adapter 直接识别 `httpx`
  transport error，模型调用仍由两个窄端口隔离且默认关闭。
- `sqlglot` 用于 SQL AST 解析和安全校验（M3 引入，是 M3 唯一新增的运行依赖）。
- `sqlalchemy[asyncio]`、`alembic`、`asyncpg` 是 M4 新增且仅有的三个运行依赖。**用 SQLAlchemy Core，不用 ORM**：ORM 的 identity map 与 flush 时机会让「必须采纳存储层 winner」这条不变量更难断言，而并发语义正是 M4 的全部承重点。`asyncpg` **不带 `py.typed`**，因此业务代码不得直接 import 它——驱动只经 `postgresql+asyncpg://` 的 DSN 方言字符串由 SQLAlchemy 内部加载。
- PyMySQL 是 M6b 新增的 StarRocks MySQL 协议 driver，只允许在 `tools/starrocks.py` 的
  connection factory 被调用时延迟导入；默认 recording 装配、CI 和离线 eval 不导入它、也不发起网络连接。
- `lark-oapi==1.7.3` 是 M7 PR 4 唯一新增的直接运行依赖；它不带 `py.typed`，只有
  `interfaces/feishu_sdk.py` 可以延迟加载。默认关闭的 listener/worker、fake transport/message
  port 和离线测试均不加载 SDK 或连接飞书。
- Redis、pgvector、消息队列、LangGraph 等均不是第一阶段的强依赖；只有评估证明需要时才引入。

截至 M5 的基础依赖与 Compose 文件已合入 `main`，M6b 的 PyMySQL 与 M7 PR 4 的
`lark-oapi` 也已合入；RI3 PR 3B 离线增加锁版 `google-genai`，并把已解析的 `httpx` 从 dev
提升为生产直接依赖。隔离 Compose smoke 已在既有合并后 CI 实际通过，生产
兼容性仍需独立部署与运行证据。

## 预期目录

```text
agent/
├── AGENTS.md
├── README.md
├── ARCHITECTURE.md
├── AGENT_HANDOFF.md
├── DEVELOPMENT_PLAN.md
├── .gitignore
├── docs/
│   ├── CAPABILITIES.md        # 能力地图；由 Registry/代码生成并由 CI 检查
│   ├── adr/                   # 架构决策记录（当前至已接受 ADR-015）
│   ├── plans/                 # 里程碑详细实施计划（M2 已建立）
│   └── handoff/archive/       # 历史交接和复盘
├── pyproject.toml              # 已建立（M1）
├── docker-compose.yml          # API / Worker / 三个渠道角色 / migration / PostgreSQL
├── docker-compose.smoke.yml    # smoke 的短租约与轮询 override
├── docker-compose.barrier.yml  # 仅用于恢复测试的 barrier override
├── docker-compose.m6b-test.yml # M6b 临时测试环境显式 override；默认不可激活
├── src/xiaowei_agent/          # 业务包
│   ├── redaction.py            # 已建立（M2）：脱敏规则单一真源，无对内依赖的叶子
│   ├── contracts/              # 已建立（M2）：跨模块 DTO / Protocol
│   ├── capabilities/           # 已建立（M2）：分类派生与 Resolver Protocol
│   ├── planning/               # 已建立（M2）：canonical JSON 与三个指纹
│   ├── governance/             # 已建立（M2）：审批绑定与 policy revision 校验
│   ├── tools/                  # 已建立（M2）：ToolGateway / adapter 契约
│   ├── persistence/            # 已建立（M2）：TaskStore 交互形状
│   │                           # M4：decisions/rows/schema/postgres + migrations/
│   ├── runners/                # 已建立（M2）：WorkflowRunner 契约
│   ├── observability/          # 已建立（M2）：TraceSink Protocol
│   ├── application/            # 已建立（M3）：XiaoweiRuntime facade
│   ├── evidence/               # 已建立（M3）：纯证据构造器
│   ├── reflection/             # 已建立（M3）：可答性判定
│   ├── rendering/              # 已建立（M3）：RenderPayload 投影
│   └── interfaces/             # 已建立（M5）：API / CLI / Worker / migration 装配入口
└── tests/
    ├── unit/                   # 已建立
    ├── contract/               # 已建立（M2）
    ├── security/               # 已建立
    ├── fakes/                  # 已建立（M2/M3）：测试夹具与 recording
    ├── evals/                  # 已建立（M3）：L0-L2 语料与断言
    ├── suites/                 # 已建立（M4）：跨实现共享的行为用例（不被收集）
    └── integration/            # 已建立（M4）：需要真实 PostgreSQL，无 DSN 时跳过
```

上表是**目标**目录。`pyproject.toml`、`uv.lock`、`src/xiaowei_agent/{__init__,config,trace,log}.py`、`tests/{unit,security}/` 与 `.github/workflows/ci.yml` 在 M1 建立；标注「已建立（M2）」「已建立（M3）」的包分别在对应里程碑建立。其余条目按实际代码落地时才创建，**不要为了匹配树状图提前创建空模块**。

`docs/CAPABILITIES.md` 由 `xiaowei_agent.capabilities.doc.render_capabilities_doc()` 生成，一致性由 `tests/security/test_capabilities_doc.py` 检查；**不要手工编辑**。

## 从 0 开始的开发顺序

1. **设计与 Git 基线**：批准总体计划，检查敏感信息，以当前文档集初始化本地 `main` 基线。
2. **工程与 CI 基线**：建立 Python 包、配置、日志、trace_id、测试和静态检查；CI 不持有真实环境凭证。
3. **契约与只读垂直闭环**：先冻结 DTO、TaskStore 并发交互和 Gateway 边界，再用 fake adapter 完成一个 StarRocks 场景。
4. **TaskStore**：落地 PostgreSQL 的幂等、CAS、lease、fencing、恢复和终态保护。
5. **API + Worker + Compose**：把同步请求与可恢复任务分开，补 PostgreSQL 集成验证。
6. **能力扩展与真实只读**：先独立完成两个 fake 能力，再在明确授权的测试环境验证真实 StarRocks adapter。
7. **渠道与受控写**：API/CLI 稳定后接 Web、飞书；TaskStore、身份和审批稳定后才进入测试环境受控写。
8. **框架评估**：用真实的多步、暂停、恢复和重试样本评估 LangGraph；通过准入门槛后，才实现 `LangGraphRunner` adapter。

每一步先有契约和测试，再增加真实外部调用；默认先读，不默认打开生产写操作。

## 本地开发约定

### 安装

工具链由 [uv](https://docs.astral.sh/uv/) 管理，Python 版本固定 3.11（见 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)）：

```bash
uv sync --extra dev --frozen
source .venv/bin/activate
```

### 验证命令

激活 `.venv` 后，**原样执行** ADR-008 定义的四条命令：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

CI 在无交互 shell 中通过 `astral-sh/setup-uv` 的 `activate-environment: true` 激活同一个 `.venv`，因此执行的是**同样的四条命令**，不做 `uv run` 包装。

### 配置

`load_settings()` **只读进程环境变量，不读 `.env`**；`.env.example` 仅作变量名文档。未在其中列出的 `XIAOWEI_*` 变量会被 fail-fast 拒绝；固定开发租户是代码常量，没有对应环境变量。

```bash
export XIAOWEI_ENVIRONMENT_ID=dev
export XIAOWEI_LOG_LEVEL=INFO
```

`dev` 是当前确定性目标目录中已登记的本地 fake 环境；固定开发租户仍是
`dev-local`。两者是不同的安全维度，不应复用同一个标识。

飞书 listener、projection worker、Web app 与 OAuth 默认全部关闭：

```bash
export XIAOWEI_FEISHU_LISTENER_ENABLED=false
export XIAOWEI_CHANNEL_WORKER_ENABLED=false
export XIAOWEI_WEB_APP_ENABLED=false
export XIAOWEI_FEISHU_OAUTH_ENABLED=false
```

`docker-compose.yml` 以 `m7-channels` profile 声明 `feishu-listener` 与 `channel-worker`；
**`web-app` 自 RI5 起不在 profile 里**——它是本地管理面的主入口，普通 `up -d` 就应拉起它。
Web 相关的四个开关（`XIAOWEI_WEB_APP_ENABLED`、`XIAOWEI_FEISHU_OAUTH_ENABLED` 与两个
`*_REAL_TEST_ENABLED`）在基础文件里写成 `${...:-false}`：默认仍然关闭，但 `.env` 真的能覆盖它们。
写成字面量时 Compose 的 `environment:` 优先级高于 `.env`，改了也不会生效。
它们与 API/Worker 共用同一镜像；listener/worker 不发布宿主端口。Web 容器内使用 HTTP
`0.0.0.0:8080`，宿主只发布 `127.0.0.1:8080`，因此基础 Compose 不能通过服务器或局域网 IP 访问。

`http://127.0.0.1:8080/healthz` 和 `/readyz` 只供本机健康检查，不能用来完成浏览器登录。
OAuth 浏览器入口仍必须使用已经备案的 HTTPS SSO 域名，并由 Compose 外部的 TLS/反向代理转发到
Web 容器。本仓库没有提供证书、TLS/Ingress 或反向代理，也没有证明真实 callback 可以从飞书到达。

三个进程都关闭时，`XIAOWEI_FEISHU_TENANT_KEY`、`XIAOWEI_FEISHU_BOT_OPEN_ID`、
`XIAOWEI_FEISHU_IDENTITY_FILE` 与 `XIAOWEI_WEB_PUBLIC_ORIGIN` 必须全部留空。listener 开启时
前三项必须同时提供；Web app 开启时必须提供 `XIAOWEI_WEB_PUBLIC_ORIGIN`，飞书 OAuth 另外开启时
还需要身份文件。**App ID 与 App Secret 不再是环境变量**：自 RI5 起它们的唯一真源是
`.config/integrations.json`，由 Web 管理面写入、各进程启动时读取。不经 Compose 直接运行时，
Web 默认监听 `127.0.0.1:8080`；OAuth state 默认 300 秒、session 默认 3600 秒。当前 Web OAuth
code exchange 使用代码固定的 5 秒 provider 总预算，`WebAuthService` 使用严格更长的 6 秒外层
watchdog，且不重试；两者都没有读取
`XIAOWEI_FEISHU_API_TIMEOUT_SECONDS`；后者目前只装配给渠道消息发送路径。
详情 origin 会把 IDN hostname 规范化为 ASCII punycode 后再用于卡片链接，校验值与实际使用值一致。
两个文件字段必须是绝对路径。App secret 只接受文件引用，不接受环境变量中的明文。身份文件是版本化 JSON，按飞书
`open_id` 精确映射，不按姓名或群角色猜权限：

```json
{
  "version": 1,
  "tenant_id": "dev-local",
  "environment_id": "dev",
  "entries": [
    {
      "subject_ref": "user-open-id",
      "actor": "alice",
      "labels": ["operator"]
    }
  ]
}
```

`operator`、`dba`、`oncall` 可查看安全任务并发起只读任务；`viewer`、`approver` 只可查看；
`admin` 拥有当前渠道权限闭集。映射在 listener 或 Web app 装配时一次读取，修改后必须重启对应
进程才生效。

Compose 启动前只需要准备一个已被 Git 忽略的本地文件：

- `.secrets/postgres_password`

`.secrets/` 保持 `0700`，文件写完后保持 `0444`。

**Provider 凭据不再是 Docker secret。** 自 RI5 起 Gemini API Key 与飞书 App ID / App Secret 的
唯一真源是 `.config/integrations.json`，由本地管理面写入。`.config/` 已被 `.gitignore` 与
`.dockerignore` 忽略；`web-app` 以读写方式挂载它，`worker` / `feishu-listener` / `channel-worker`
只读挂载，`api` 完全不挂。模型名、endpoint、timeout 仍是代码固定值，页面上只读显示。

启用飞书 OAuth 时另外准备 `.secrets/feishu-identities.json`，并叠加 `docker-compose.feishu.yml`；
不启用飞书时**不需要**这个文件——它不在基础 Compose 里，干净部署不会因为缺它而起不来。

#### 首启顺序（RI5 本地管理面）

**这几步不能跳过，也不能换顺序。** 第 8 步之前套用 LAN override，初始口令 `admin/admin`
就会暴露给同网段。

1. 建目录。容器以 UID/GID `10001` 运行，属主对不上时 Web 起得来但**存不下配置**——
   管理员会在填完表单点保存时才发现：

   ```bash
   mkdir -p .config && chmod 700 .config
   # Linux Docker Engine 另需（macOS Docker Desktop 跳过）：
   sudo chown 10001:10001 .config
   ```

2. 在 `.env` 写入首启参数。这些键现在真的会被 Compose 插值消费：

   ```bash
   XIAOWEI_WEB_APP_ENABLED=true
   XIAOWEI_WEB_MODE=lan_http
   XIAOWEI_WEB_PUBLIC_ORIGIN=http://127.0.0.1:8080
   ```

   `lan_http` 接受 canonical loopback，因此首启阶段不必先架 HTTPS。

3. 探测本机可用的 Compose 命令。**不要假定 `docker compose` 存在**——本机实测只有
   `docker-compose 5.5.1`，`docker compose` 返回 `unknown command`：

   ```bash
   if docker compose version >/dev/null 2>&1; then
     compose() { docker compose "$@"; }
   elif docker-compose version >/dev/null 2>&1; then
     compose() { docker-compose "$@"; }
   else
     echo "no compose CLI" >&2; return 1
   fi
   ```

   **必须用 shell 函数，不能用 `COMPOSE="docker compose"` 加 `$COMPOSE`。** zsh 对未加引号的
   参数展开不做分词，`$COMPOSE` 会被当成一个名为 `docker compose`（含空格）的命令，直接
   `command not found`；bash 下碰巧能用，zsh 下必错。后续步骤统一写 `compose ...`。

4. **先跑预检，成功后才启动 Web**：

   ```bash
   compose run --rm --no-deps \
     -v "$PWD/.config:/run/xiaowei-config" \
     web-app python -m xiaowei_agent.interfaces.config_preflight
   ```

   只应输出 `preflight: ok`。预检写的是哨兵文件 `.preflight-probe.json` 并在退出前删除，
   不会生成也不会改动 `integrations.json`。失败时**不要**继续——目录属主或权限不对，
   Web 起来也存不下配置。

5. 启动。基础文件只发布 `127.0.0.1:8080`，`web-app` 已不在 profile 里：

   ```bash
   compose up -d
   ```

6. 宿主机浏览器打开 `http://127.0.0.1:8080`，用 `admin/admin` 登录并**完成强制改密**。

7. 改密完成后，再把 `.env` 的 public origin 改成局域网地址：

   ```bash
   XIAOWEI_WEB_PUBLIC_ORIGIN=http://192.168.1.20:8080
   ```

8. 叠加 LAN override 重建：

   ```bash
   compose -f docker-compose.yml -f docker-compose.lan.yml up -d --force-recreate web-app
   ```

   重建后核对渲染结果**只有一条**端口映射：

   ```bash
   compose -f docker-compose.yml -f docker-compose.lan.yml config | grep -A3 'ports:'
   ```

9. 从局域网地址用新密码重新登录。旧 Cookie 因 origin digest 变化已失效，属预期。

启用飞书时额外叠加 `-f docker-compose.feishu.yml` 并准备 `./.secrets/feishu-identities.json`。

**残余风险**：在第 6 步之前套用 LAN override，`admin/admin` 会暴露给同网段。补救是改密后
重建 Web 并撤销全部 `local_admin` session。

#### 飞书 OAuth

基础 Compose 不会自行打开 OAuth。取得 RI2 现场许可后，在 `.env` 打开
`XIAOWEI_FEISHU_OAUTH_ENABLED=true` 并设置 HTTPS SSO origin，然后叠加飞书 override：

```bash
compose -f docker-compose.yml -f docker-compose.feishu.yml \
  up -d --force-recreate web-app
```

App ID 与 App Secret 在管理面填写，不进 `.env`、不进命令行、不进任何已跟踪文件。回滚时先停止
`web-app`，把 `.env` 里的 Web/OAuth 两个开关都改回 `false`；再次创建容器前不得保留单边开启。
listener 与 channel-worker 仍保持关闭，直到各自取得独立现场许可。离线验证不构成真实渠道授权。

### 集成测试（M4）

需要真实 PostgreSQL 的用例位于 `tests/integration/`，由**既有** `testpaths` 自动收集，**没有第五条命令**——ADR-008 的四条命令一字不改。开关是一个环境变量：

```bash
export PYTEST_POSTGRES_DSN='postgresql+asyncpg://postgres@127.0.0.1:5432/postgres'
python -m pytest -q
```

- **变量名不带 `XIAOWEI_` 前缀**：它是测试 harness 配置，不是应用配置。`load_settings()` 对任何未知 `XIAOWEI_*` 变量 fail-fast，而 `tests/conftest.py` 每个用例前会清掉全部 `XIAOWEI_*`。
- **未设置时整组跳过**；已设置时 `tests/integration/` 里**不允许有任何跳过**，否则整次运行判失败——DSN 拼错、库没起来、迁移失败都会长成「全绿」的样子。
- 需要一个**隔离**的 PostgreSQL：用例会 `TRUNCATE` 全部表。本地可用单容器、本机已有实例或 M5 Compose；不要指向共享数据库。
- schema 由 Alembic 迁移建立，不由 `create_all` 建立。

### 本地 Compose（M5）

先生成仅供本地隔离数据库使用的随机 secret 文件；它被 `.gitignore` 排除，不得提交：

```bash
install -d -m 700 .secrets
(umask 077; python -c 'import pathlib,secrets; pathlib.Path(".secrets/postgres_password").write_text(secrets.token_urlsafe(32) + "\n")')
chmod 444 .secrets/postgres_password
docker compose build
docker compose up -d --wait postgres
docker compose up --no-deps migrate
docker compose up -d --wait --no-deps api
docker compose up -d --no-deps worker
```

父目录必须保持 `0700`；secret 写完后改为只读 `0444`，让以非 root 用户运行的应用容器可读取。Compose 的 file-backed secret 在本地实现中是 bind mount，不能依靠 Compose 的 `uid` / `gid` / `mode` 重映射；宿主侧的访问隔离由不可遍历的 `.secrets/` 目录承担，且 secret 只挂载给声明使用它的服务。

`/healthz` 只证明 API 进程存活；`/readyz` 还检查数据库、migration head 与装配状态：

```bash
python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/readyz").read().decode())'
xiaowei task submit --text '检查最近三十分钟慢查询' --idempotency-key local-demo-1
xiaowei task submit --text '查告警 HostHighCpu 在 node-1.example.com:9100 的证据' --idempotency-key local-prom-demo-1
xiaowei task submit --text '查资产 hostname=node-1.example.com' --idempotency-key local-asset-demo-1
xiaowei task get TASK_ID
```

第二条提交命令只命中内置 Alertmanager/Prometheus synthetic recording。当前本地样例
还支持 `InstanceDown` + `10.0.0.8:9100`；它不是 Grafana 展示、告警静默，也不是对
真实监控系统的兼容性证明。Prometheus metric recording 只覆盖默认 30 分钟窗口和
装配中心时刻前后 120 分钟的精确键；非默认窗口或超出该时段的请求会安全降级为
`indeterminate`，没有 fallback。长期运行或窗口泛化不属于本轮本地样例承诺。

第三条提交命令只命中内置资产 synthetic recording；同一资产还可用精确
`asset_id=asset-1` 或 `ip=10.0.0.8` 查询。它不支持列出全部、模糊/CIDR/range、跨环境、
拓扑、巡检或资产写入，也不能证明任何真实 CMDB/资产系统兼容性。

停止会保留数据库 volume；清理会删除本项目的本地数据库数据。执行清理前先确认当前目录与 Compose project：

```bash
docker compose stop
docker compose down --volumes --remove-orphans
```

完整恢复、并发与幂等验收由独立脚本创建随机 project、做碰撞预检并在 `finally` 中只清理该 project：

```bash
python -m scripts.compose_smoke
```

脚本要求 Docker Compose 2.24.4 或更新版本。自 RI5 起同一 workflow 的末段临时叠加
`docker-compose.model.yml`，只通过 Docker inspect 的 label/environment/mount 元数据证明
**配置目录按各进程的角色挂载**：`worker` / `feishu-listener` / `channel-worker` 只读、
`web-app` 可写、`api` / `migrate` / `postgres` 完全没有；脚本不打开或输出配置文件。
基础 Compose 仍默认关闭模型。

缺少 Docker、migration 失败、readiness 未就绪、Worker 恢复失败、默认关闭的渠道入口未静默
fail-closed、Web 容器边界不符或日志泄漏都会返回非零；脚本不允许 skip。脚本会在 `.secrets/`
下创建**两个**一次性的 `0700` UUID 私有目录：一个放 fake 的 postgres 口令、身份文件与不含
secret 值的 JSON Compose override，另一个只放一份合成的 `integrations.json`。两者分开是必要的
——配置目录是**整目录**挂进容器的，与口令同目录时那份口令会一起出现在 worker 的
`/run/xiaowei-config` 下。override 把配置目录与身份文件的引用都指向这两个私有目录，不读取或
覆盖上文供人工启动使用的固定文件，也不依赖宿主环境变量。清理时先原子隔离目录，再核对目录与
各自已知文件的 inode，且不递归删除未知内容。它只激活 Web，listener 与 channel-worker 仍关闭；Web 的飞书 API 域名被指向
loopback。脚本先访问 `/healthz`、`/readyz`，再用不读取代理、不能跟随重定向的本地
`HTTPConnection` 请求一次受信 Host 的 `/oauth/feishu/start`，核对 302、官方 Location、一次性
state 和安全 cookie；随后用直接 IP Host 证明固定 403，并把 state 加入日志泄漏扫描。它不请求
callback，也不跟随 Location 或调用 provider。镜像 build 仍可能访问镜像仓库或依赖源，因此这不是
“全程零外网”的证明。

这条清理边界防止正常并发 smoke、其他 UID 进程或人工固定文件被误删；它不承诺对抗主动恶意的
同 UID 本机进程——这类进程本来就能检查和修改同一用户的路径。发现目录身份漂移或未知内容时，
脚本会固定失败并保留现场，不会递归清理。

当前开发机有 Docker client、standalone Compose 5.5.1 与可用的 Colima daemon；但 Docker credential
helper 缺失，且用户已有容器占用 `127.0.0.1:8000`，所以本轮没有取得 PR 3B 完整 Compose/model
mount audit 证据。本任务未停止或修改用户容器；本机仍只有脚本测试与 Compose 静态合并证据。PR #31 的补修实现基线
`2d67b59` 已在 GitHub 隔离 runner 实际执行 Compose smoke 与隔离 PostgreSQL integration，八项
CI 全绿；这仍只是 `tests` 证据，不是飞书测试环境、部署、canary 或用户验收。

### 尚未完成与能力边界

当前默认装配仍使用确定性无模型 interpreter。[ADR-015](docs/adr/ADR-015-real-model-provider-boundary.md)
与 [RI3 详细计划](docs/superpowers/plans/2026-09-10-model-provider-adapter.md) 已获批准；PR 3B–3D 已离线新增
固定 Gemini SDK adapter、严格 DTO/窄 port、worker-only secret override、durable 模型编排、持久 artifact、
MODEL trace、慢查询 advisory 与显式 Web 父任务上下文，但未读取真实 key，也未进行真实 provider 调用。M6a 增加了
Alertmanager 告警读取、Prometheus 固定模板指标取证和资产精确查询。最终 [PR #14](https://github.com/shixian66/xiaowei-agent/pull/14)
已以 fast-forward 合入；合入后 main run `33976421909` 在 GitHub 隔离 runner 实跑
PostgreSQL integration 与三能力 Compose smoke，八个 job 全绿。该证据只能证明隔离
环境中的 fake 闭环，不能推出任何真实运维系统兼容、部署、canary 或产品用户验收。

M6b 已实现默认关闭的 StarRocks 测试环境只读 adapter、精确 target binding、固定 preflight、
证据归属与离线 Eval。当前测试目标仍故意保持歧义，生产 API/Worker 也未注入获批的物理身份探针，
所以 `test` 环境会在目标解析阶段拒绝 recording 和真实装配；`dev` recording 仍可用，且环境变量
不能单独激活真实连接。只有离线候选经审查、唯一目标、identity、secret reference、
证据处置与窗口全部获批、负责人再次明确“现场 GO”后，才允许补齐激活并运行
`docker-compose.m6b-test.yml`。当前仍没有真实 StarRocks 连接、真实模型 API 调用或任何 E1
（写）能力；`tools/gateway.py` 的 `_E1_EXECUTION_ENABLED` 保持 `False`。

## 旧项目关系

旧小维的安全链、任务生命周期和真实问题样本会被用作设计输入；新项目不直接 import 旧项目代码，不把旧项目当前的业务能力总表、关键词路由或历史状态复制为新系统的架构依赖。迁移能力时，必须重新通过本项目的 capability、policy、tool 和 eval 契约。

## 安全底线

- LLM 输出不能直接进入工具执行链路。
- 真实写操作必须经过具体步骤边界上的审批、恢复时重解析、一次写入和 readback。
- SQL 由确定性编译器生成，并经 Policy 与 AST Guard。
- 工具/日志/知识文本均视为不可信外部内容。
- 任何无法确认的外部结果都进入 `indeterminate`，不能伪装为成功。
- 文档、测试和日志不放 secret、token、密码、连接串或生产数据。
