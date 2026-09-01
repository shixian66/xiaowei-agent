# 小维 Agent 2.0

小维 Agent 2.0 是从 0 开始建设的策略治理型运维工作流 Agent：模型负责理解和解释，确定性系统负责规划、授权、执行、取证和恢复。

> 当前状态：项目初始化阶段。当前目录只有设计与协作文档，尚未声明已有可运行服务、数据库迁移、容器镜像或线上能力。

## 先看什么

阅读顺序以 [AGENTS.md](AGENTS.md) 的「优先阅读」一节为唯一真源，本节与其保持一致：

1. [AGENTS.md](AGENTS.md)：Codex、Claude 和开发者必须遵守的开发纪律与安全边界。
2. [ARCHITECTURE.md](ARCHITECTURE.md)：目标架构、模块边界、核心数据契约和演进门槛。
3. [AGENT_HANDOFF.md](AGENT_HANDOFF.md)：当前阶段、已验证事实、风险与下一步。
4. 本文件：人类开发者的启动和导航信息。
5. [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)：已获项目负责人批准（2026-09-01）的实施路线、决策门与退出标准。

授权边界的真源是 [ADR-007](docs/adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)，工程与测试工具链的真源是 [ADR-008](docs/adr/ADR-008-engineering-and-test-baseline.md)。

## 目标能力

第一阶段不是追求“什么都能做”，而是先把一条可验证的只读闭环做扎实：

1. 用户通过 API/CLI 发起自然语言运维问题。
2. 系统提取结构化意图，并确定性解析 capability、环境和目标。
3. Planner 生成受约束的读取计划。
4. Policy 和 SQL AST Guard 检查工具调用。
5. ToolGateway 访问外部系统，生成带来源和限制的证据。
6. Reflection 只消费已生成的结构化证据，判断是否足以回答并说明限制、缺失和降级；它不追加步骤、不选工具、不改计划。确需额外取数时，只能是计划中预编译的预算内只读分支，由 Runner 按确定性条件执行。
7. Runtime 生成可审计、可复现的最终回答和任务结果。

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
 Context → IntentDraft → Resolver → PlanCompiler
                                      │
                              WorkflowRunner
                                      │
                       StepAdmission（每个步骤边界）
                    ToolPolicy → SQLGuard → ApprovalGate*
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

能力现状记录在 `docs/CAPABILITIES.md`，详细 handoff 历史记录在 `docs/handoff/archive/`。这两个路径属于项目治理骨架：`docs/handoff/archive/` 已收录 M0 收口归档；`docs/CAPABILITIES.md` 尚未创建，其条目由 Registry/代码生成，能力实现后才会出现。

## 初始技术基线

目标基线是模块化单体，统一 Docker Compose 开发环境：

- Python 3.11（首个且当前唯一强制验证的版本；其他版本未测试，不宣称支持）、FastAPI、Pydantic、SQLAlchemy、Alembic；依赖由 uv 解析并锁定在 `uv.lock`。
- pytest、pytest-asyncio；安全测试使用 `security` marker 作为显式 CI gate。
- Ruff 作为唯一 linter，mypy 作为唯一类型检查器；格式化方案待 Foundation 阶段单独决定。
- PostgreSQL 作为 TaskStore、审批、证据索引和运行审计的事实存储。
- 一个镜像同时支持 API Gateway 和 Worker，先以进程角色区分，不提前拆微服务。
- 官方模型 SDK 仅用于文本/JSON 生成；模型调用通过 adapter 隔离。
- `sqlglot` 用于 SQL AST 解析和安全校验。
- Redis、pgvector、消息队列、LangGraph 等均不是第一阶段的强依赖；只有评估证明需要时才引入。

当前这些是目标技术基线，不代表依赖已经安装或服务已经可启动。

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
│   ├── adr/                   # 架构决策记录（ADR-007、ADR-008 已建立）
│   └── handoff/archive/       # 历史交接和复盘
├── pyproject.toml              # Foundation 阶段建立
├── docker-compose.yml          # Compose 阶段建立
├── src/xiaowei_agent/          # 业务包
│   ├── contracts/              # 跨模块 DTO / Protocol
│   ├── application/            # Runtime / 用例编排
│   ├── capabilities/           # CapabilitySpec / Resolver；受限 DSL 达准入门槛后再加入
│   ├── planning/               # 意图到计划的确定性编译
│   ├── governance/             # Policy / Approval / SQLGuard
│   ├── runners/                # DeterministicRunner / LangGraph adapter
│   ├── tools/                  # ToolGateway / 外部适配器
│   ├── persistence/            # TaskStore / migrations
│   ├── evidence/               # Evidence / Memory
│   ├── reflection/             # Reflection / answerability
│   ├── rendering/              # RenderPayload 和通道投影
│   └── interfaces/             # API / CLI / 飞书 / Web
└── tests/
    ├── unit/
    ├── contract/
    ├── security/
    ├── integration/
    └── evals/
```

目录会在 Foundation 阶段按实际代码落地；不要为了匹配树状图提前创建空模块。

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
export XIAOWEI_ENVIRONMENT_ID=dev-local
export XIAOWEI_LOG_LEVEL=INFO
```

### 尚未完成

Docker Compose、API、Worker 和数据库迁移属于后续里程碑，当前不可运行。

## 旧项目关系

旧小维的安全链、任务生命周期和真实问题样本会被用作设计输入；新项目不直接 import 旧项目代码，不把旧项目当前的业务能力总表、关键词路由或历史状态复制为新系统的架构依赖。迁移能力时，必须重新通过本项目的 capability、policy、tool 和 eval 契约。

## 安全底线

- LLM 输出不能直接进入工具执行链路。
- 真实写操作必须经过具体步骤边界上的审批、恢复时重解析、一次写入和 readback。
- SQL 由确定性编译器生成，并经 Policy 与 AST Guard。
- 工具/日志/知识文本均视为不可信外部内容。
- 任何无法确认的外部结果都进入 `indeterminate`，不能伪装为成功。
- 文档、测试和日志不放 secret、token、密码、连接串或生产数据。
