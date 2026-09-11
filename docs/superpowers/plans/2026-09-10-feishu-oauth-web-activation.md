# RI1 Feishu OAuth and Web Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在默认关闭、无真实调用的前提下，实现真实飞书 OAuth adapter，并让 `web-app` 能由 Docker Compose 正常启动。

**Architecture:** 复用现有 `FeishuOAuthPort`、`WebAuthService`、PostgreSQL OAuth state/session 和身份目录。新增的 adapter 只把 authorization code 换成 `open_id`，不解析本地权限；`web_app.main()` 只做 composition root 装配。Compose 内部保持 HTTP，OAuth public origin 继续使用已有 HTTPS SSO 域名。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、PostgreSQL、标准库 HTTP 客户端、Docker Compose、pytest。

**Spec:** [真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)

## Global Constraints

本 PR 不连接真实飞书、不读取真实 secret、不改变 E1、不把旧小维作为代码依赖。开关默认关闭；
provider 原始正文和 secret 不进入日志、错误或测试夹具。基础 `docker-compose.yml` 继续只发布
`127.0.0.1:8080`；RI1 不把 Web 暴露到所有宿主网卡。

---

## 非目标

- 不做真实飞书联调、模型、StarRocks、Admin 或正式部署。
- 不增加 webhook、TLS/Ingress、第二种 OAuth session 或新的权限来源。
- 不修改 Runtime、Planner、Policy、SQLGuard 或任何 capability。

## ADR

新增 `docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md`，固定 OAuth adapter、secret 文件、已有 HTTPS SSO origin 和 Compose HTTP 的边界。

## PR 边界

- 一个 PR，只交付 OAuth adapter、Web composition root、Compose 配置契约和离线测试。
- 不包含真实飞书联调、模型、StarRocks 超时调整、Admin 或部署验收。
- ADR-014 与实现放在同一阶段，由独立提交先于代码。

## 进入条件

- 从当时最新 `main` 创建 `claude/feishu-oauth-web-activation`。
- 重新读取五份入职文档并记录精确 `origin/main`、HEAD 和 `git status`。
- 确认旧小维仅作行为参考，且本 PR 不操作旧服务。
- 用户批准本阶段实现计划；这不授权真实飞书调用。

### Task 1：固定 OAuth 安全契约与 ADR

**Files:**

- Create: `docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md`
- Modify: `src/xiaowei_agent/interfaces/web_auth.py`
- Modify: `src/xiaowei_agent/persistence/web_session.py`
- Modify: `src/xiaowei_agent/persistence/fake.py`
- Modify: `src/xiaowei_agent/persistence/postgres.py`
- Modify: `tests/suites/web_session_store.py`
- Modify: `tests/contract/test_web_session_store.py`
- Modify: `tests/integration/test_web_session_postgres.py`
- Modify: `tests/security/test_web_auth_boundary.py`
- Modify: `tests/security/test_module_layering.py`

- [ ] **Step 1: 写 RED state 生命周期测试**

测试必须先证明：

- OAuth state 有过期清理与全局未完成数量上限；超限时拒绝签发，保证数据库有界，但不把它称为
  滥用保护或请求限流。
- fake 与 PostgreSQL 在并发签发时都不能越过同一个上限；已消费或已过期 state 不占额度。
- 拒绝结果是闭集错误，日志不出现 state 原文、cookie 或数据库异常正文。

Run: `python -m pytest tests/contract/test_web_session_store.py tests/integration/test_web_session_postgres.py tests/security/test_web_auth_boundary.py -q`

Expected: 现有 store 尚无清理/容量上限，并发与超限反例失败。

- [ ] **Step 2: 写 ADR**

ADR 固定：官方 endpoint 为代码闭集而非可由用户/模型修改的任意 URL；secret 只从只读文件读取；
state/session 继续由现有 PostgreSQL 契约承重；OAuth 身份只含 `open_id`；Compose HTTP 与已有 HTTPS
SSO public origin 的职责分开；应用不信任任意 Host/forwarded header；state 清理/容量上限与 SSO
入口限流责任写清。

- [ ] **Step 3: 收紧现有端口说明**

保留现有端口形状，不新增权限字段：

```python
class FeishuOAuthPort(Protocol):
    def authorization_url(self, *, state: str, redirect_uri: str) -> str: ...

    async def exchange_code(
        self, *, code: str, redirect_uri: str
    ) -> FeishuOAuthIdentity: ...
```

Run: `python -m pytest tests/contract/test_web_auth.py tests/security/test_web_auth_boundary.py -q`

Expected: 现有 state 一次消费、cookie 绑定、session 轮换和撤权测试继续通过。

- [ ] **Step 4: 实现有界 OAuth state 保存**

`issue_oauth_state` 在同一事务中先清理过期/已消费 state，再检查全局未完成数量硬上限并插入新
digest；超限只返回闭集错误。仅有事务还不足以阻止 `count → insert` 竞态：PostgreSQL 必须用固定、
不含用户输入的 transaction advisory lock 或锁定的配额行，把清理/计数/插入串行化；不得依赖单
进程 Python lock。fake/PostgreSQL 共用同一 contract suite，并发屏障测试必须证明签发不能越过上限。
这只解决数据库无界增长。由于匿名 OAuth start 可在一个 TTL 内填满 1024 个全局 pending state，
并通过持续补位让全部正常登录保持 503，这个上限本身形成明确的全局可用性 DoS 风险。公网请求
速率仍由已有 SSO 入口负责；RI2/RI6 runbook 必须记录该设施的真实限流配置、监控/告警和运行反证，
缺少这些证据时不得激活公网 OAuth，不能把 state 数量上限冒充按 IP 限流。

Run: `python -m pytest tests/contract/test_web_session_store.py tests/contract/test_web_auth.py tests/integration/test_web_session_postgres.py tests/security/test_web_auth_boundary.py -q`

Expected: fake 与 PostgreSQL 对过期清理、并发上限和安全错误语义一致。

- [ ] **Step 5: 提交契约与 ADR**

```bash
git add docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md src/xiaowei_agent/interfaces/web_auth.py src/xiaowei_agent/persistence/web_session.py src/xiaowei_agent/persistence/fake.py src/xiaowei_agent/persistence/postgres.py tests/suites/web_session_store.py tests/contract/test_web_session_store.py tests/contract/test_web_auth.py tests/integration/test_web_session_postgres.py tests/security/test_web_auth_boundary.py tests/security/test_module_layering.py
git commit -m "feat(web): bound oauth activation state"
```

### Task 2：实现最小真实 OAuth adapter

**Files:**

- Create: `src/xiaowei_agent/interfaces/feishu_oauth.py`
- Modify: `src/xiaowei_agent/interfaces/__init__.py`
- Modify: `src/xiaowei_agent/interfaces/feishu_sdk.py`
- Modify: `src/xiaowei_agent/interfaces/secret_file.py`
- Modify: `src/xiaowei_agent/_conformance.py`
- Create: `tests/contract/test_feishu_oauth_adapter.py`
- Modify: `tests/contract/test_feishu_sdk_seam.py`
- Modify: `tests/contract/test_protocol_conformance.py`
- Modify: `tests/security/test_dependency_baseline.py`
- Modify: `tests/security/test_module_layering.py`

- [ ] **Step 1: 写 RED adapter 契约测试**

测试必须先证明：

- authorization URL 只允许飞书官方 HTTPS host，精确携带一次 `state` 和固定 callback；
- `exchange_code()` 只接受非空 code，只返回 `FeishuOAuthIdentity(subject_ref=open_id)`；
- provider 拒绝映射为 `FeishuOAuthCodeError`，超时/网络错误映射为 `FeishuOAuthUnavailableError`；
- 无效 JSON、未知字段、缺 `open_id`、超大响应和控制字符全部拒绝；
- 日志和异常不含 code、token、app secret、provider 正文或用户身份原文；
- adapter 无法构造 tenant、environment、actor 或 permissions。

Run: `python -m pytest tests/contract/test_feishu_oauth_adapter.py tests/security/test_web_auth_boundary.py -q`

Expected: 新 adapter 尚不存在，测试失败。

- [ ] **Step 2: 注入离线 transport 并保持生产面单一**

adapter 对外只有一个类；测试通过私有 transport seam 提供录制响应，不启动 socket。生产实现用标准库 HTTP 调用并通过 `asyncio.to_thread` 隔离阻塞 I/O，避免新增运行依赖。

```python
class FeishuOAuthAdapter(FeishuOAuthPort):
    def __init__(
        self, *, app_id: str, app_secret_file: str, timeout_seconds: float
    ) -> None: ...
```

secret reader 是 `interfaces.secret_file` 的公开窄 API，必须拒绝 symlink、非普通文件、空文件和
超限文件，且不在 `repr` 中保存 secret。provider adapter 的固定总预算为 5 秒；底层 socket 使用
剩余预算的更短切片，`WebAuthService` 的外层 watchdog 固定为 6 秒并严格大于 provider 预算。取消
外层 coroutine 只会丢弃晚到结果，不能宣称已杀死 `to_thread` 内的阻塞调用。进程关闭和重试不得
复用该晚到结果。

- [ ] **Step 3: 实现最小 GREEN**

- URL 构造使用固定官方 host 和 `urllib.parse.urlencode`；
- code exchange 的 provider 总预算设置为 5 秒，WebAuth 外层 watchdog 设置为 6 秒；限制响应
  字节数并严格校验 JSON；
- 成功后只保留 `open_id`；access token 和原始响应在局部变量范围内消失；
- 所有错误转成既有安全异常，不记录 provider body。
- 外层取消、底层 socket 超时和晚到响应分别测试；任何晚到响应都不得进入身份映射或 session。

Run: `python -m pytest tests/contract/test_feishu_oauth_adapter.py -q`

Expected: 全部通过，且测试无网络权限也能运行。

- [ ] **Step 4: 做安全反证**

临时放宽官方 host 或把 provider body 放进异常，确认对应安全测试变红；恢复承重保护后重跑。反证只在独立临时副本执行，不提交变异代码。

Run: `python -m pytest tests/security/test_web_auth_boundary.py tests/security/test_module_layering.py -q`

Expected: 恢复后通过。

在 `src/xiaowei_agent/_conformance.py` 增加 `FeishuOAuthPort = FeishuOAuthAdapter` 的类型锚，并更新
`tests/contract/test_protocol_conformance.py` 的已实现 Protocol 集合/关键字参数断言；不得只依赖
运行时 duck typing。

- [ ] **Step 5: 提交 adapter**

```bash
git add src/xiaowei_agent/interfaces/feishu_oauth.py src/xiaowei_agent/interfaces/feishu_sdk.py src/xiaowei_agent/interfaces/secret_file.py src/xiaowei_agent/interfaces/__init__.py src/xiaowei_agent/_conformance.py tests/contract/test_feishu_oauth_adapter.py tests/contract/test_feishu_sdk_seam.py tests/contract/test_protocol_conformance.py tests/security/test_dependency_baseline.py tests/security/test_module_layering.py
git commit -m "feat(feishu): add bounded oauth adapter"
```

### Task 3：装配 Web 启动入口和配置

**Files:**

- Modify: `src/xiaowei_agent/config.py`
- Modify: `.env.example`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `tests/unit/test_config_happy.py`
- Modify: `tests/unit/test_feishu_config.py`
- Modify: `tests/unit/test_web_config.py`
- Modify: `tests/security/test_config_fail_fast.py`
- Modify: `tests/contract/test_web_app_routes.py`
- Modify: `tests/security/test_web_auth_boundary.py`

- [ ] **Step 1: 写 RED 配置和启动测试**

新增明确开关 `XIAOWEI_FEISHU_OAUTH_ENABLED`，默认 `false`。测试以下组合：

- Web 开、OAuth 关：启动失败且无网络；
- OAuth 开但缺 app ID、secret file、身份文件或 HTTPS public origin：启动失败；
- 配置完整：composition root 创建 OAuth、成员 adapter 和 FastAPI app；
- app secret 文件非法：统一 `configuration_error`，不回显路径或值；
- 启动失败时已创建的数据库 engine 被关闭。
- `.env.example` 的键集合与 `_FIELD_TO_ENV` 精确相等，新增开关值为空或安全默认值。
- 受保护 OAuth/session 路由只接受配置的 HTTPS SSO hostname；伪造 Host、直接 HTTP IP Host、
  未批准 Origin 和不可信 `X-Forwarded-*` 全部拒绝。健康检查可继续按单独闭集暴露。

Run: `python -m pytest tests/unit/test_config_happy.py tests/unit/test_feishu_config.py tests/unit/test_web_config.py tests/security/test_config_fail_fast.py tests/security/test_env_example_clean.py tests/contract/test_web_app_routes.py -q`

Expected: 新开关与装配尚不存在，测试失败。

- [ ] **Step 2: 实现 composition root**

`web_app.main()` 依次加载配置、构造 OAuth adapter、构造现有 Feishu membership adapter、调用 `build_postgres_web_stack()`、创建 FastAPI app 并交给 uvicorn。任何半配置 fail-closed；handler 不直接持有 app secret。

- [ ] **Step 3: 复核 HTTP 与 HTTPS 职责**

- 容器内 `web_bind_host=0.0.0.0` 只表示容器监听；基础 Compose 宿主发布仍是
  `127.0.0.1:8080:8080`；
- `web_detail_base_url` 仍要求用户已有的 HTTPS SSO origin；
- OAuth redirect、Secure cookie、SameSite、CSRF 和 HSTS 继续按 public origin 契约工作；
- 不在应用中实现 TLS 或代理。
- OAuth start/callback 和受保护 `/app` 路由验证受信 Host；直接访问
  `http://IP:8080` 不能建立或使用 OAuth session。只有 `https://<已批准 SSO 域名>` 的
  Host/Origin 组合可以完成浏览器认证。

Run: `python -m pytest tests/contract/test_web_app_routes.py tests/security/test_web_auth_boundary.py -q`

Expected: 完整配置可装配，缺配置安全失败。

- [ ] **Step 4: 提交启动装配**

```bash
git add .env.example src/xiaowei_agent/config.py src/xiaowei_agent/interfaces/local_stack.py src/xiaowei_agent/interfaces/web_app.py tests/unit/test_config_happy.py tests/unit/test_feishu_config.py tests/unit/test_web_config.py tests/security/test_config_fail_fast.py tests/contract/test_web_app_routes.py tests/security/test_web_auth_boundary.py
git commit -m "feat(web): activate real oauth composition root"
```

### Task 4：加入 Compose 配置契约

**Files:**

- Modify: `docker-compose.yml`
- Modify: `docker-compose.smoke.yml`
- Modify: `scripts/compose_smoke.py`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`

- [ ] **Step 1: 写 RED Compose 契约**

断言：

- `web-app` 仍在 `m7-channels` profile，基础 Compose 宿主发布保持
  `127.0.0.1:8080:8080`；`IP:8080` 只由 RI6 生产 override 提供；
- app secret 由 Compose secret 只读挂载，身份目录由只读 config/bind mount 提供；
- 所有真实飞书开关默认关闭；
- API 端口不因 Web 开放而自动暴露；
- image tag 可由非秘密环境变量选择，但默认值仍可本地启动。
- OAuth endpoint、SDK REST/WS 显式 origin 与 Compose smoke 的 provider 黑洞 host 由同一项目
  origin 契约机械绑定，不能靠三处恰好相同的字符串。
- canonical Compose smoke 在 Web ready 后，以本地 `HTTPConnection` 访问一次受信 Host 的
  `/oauth/feishu/start`，核对 302、官方 Location、一次性 state 与安全 cookie；再以直接 IP Host
  证明固定 403。客户端不使用代理、不跟随 Location、不请求 callback，也不触发 provider。

Run: `python -m pytest tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py -q`

Expected: 新契约先失败。

- [ ] **Step 2: 最小修改 Compose 和 README**

只增加渠道 profile 所需的环境变量、只读挂载和 Web 端口发布；不加入 nginx、Traefik、证书容器或第二套部署编排。README 只说明配置字段、开关和现有 SSO 前提，不写真实值。smoke 的 OAuth
start 只证明本地真实进程、Host 边界、PostgreSQL state 持久化和响应契约，不升级为 provider 或
测试环境证据。

- [ ] **Step 3: 更新 handoff 证据口径**

只记录精确 PR SHA 和离线命令。明确写“未调用真实飞书、未部署、未 canary、未用户验收”。

- [ ] **Step 4: 提交 Compose 契约**

```bash
git add docker-compose.yml docker-compose.smoke.yml scripts/compose_smoke.py tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py README.md AGENT_HANDOFF.md
git commit -m "chore(compose): wire disabled feishu web profile"
```

## 验证命令

```bash
python -m pytest tests/contract/test_feishu_oauth_adapter.py tests/contract/test_web_auth.py tests/contract/test_web_app_routes.py -q
python -m pytest -m security -q
python -m pytest -q
ruff check .
mypy src
docker compose config
git diff origin/main...HEAD --check
```

## 退出标准

- OAuth adapter 离线契约、安全反证、Web composition root 和 Compose 静态契约全部通过。
- 开关默认关闭；无真实配置时不会尝试网络。
- Web 容器可监听 HTTP `0.0.0.0:8080`，OAuth public origin 仍为已有 HTTPS SSO 域名。
- 基础 Compose 仍只发布 loopback；Host/Origin 反例证明直接 HTTP IP 不能完成 OAuth/session。
- PR 的最高证据等级为 `tests`；不得写成 `test-env verified`。
- 精确 SHA 已交给 Codex 逐行审查；用户未明确下达 RI2 现场 GO 前停止。

## 回滚

关闭 `XIAOWEI_FEISHU_OAUTH_ENABLED` 和 `XIAOWEI_WEB_APP_ENABLED`，重建 `web-app`；其他 worker、capability、Policy 和数据库 schema 不受影响。
