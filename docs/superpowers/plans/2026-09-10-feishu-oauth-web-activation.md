# Feishu OAuth and Web Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** 在默认关闭、无真实调用的前提下，实现真实飞书 OAuth adapter，并让 `web-app` 能由 Docker Compose 正常启动。

**Architecture:** 复用现有 `FeishuOAuthPort`、`WebAuthService`、PostgreSQL OAuth state/session 和身份目录。新增的 adapter 只把 authorization code 换成 `open_id`，不解析本地权限；`web_app.main()` 只做 composition root 装配。Compose 内部保持 HTTP，OAuth public origin 继续使用已有 HTTPS SSO 域名。

**Tech Stack:** Python 3.11、FastAPI、Pydantic、PostgreSQL、标准库 HTTP 客户端、Docker Compose、pytest。

**Spec:** [真实接入总体设计](../specs/2026-09-10-real-integrations-design.md)

**Global Constraints:** 本 PR 不连接真实飞书、不读取真实 secret、不改变 E1、不把旧小维作为代码依赖。开关默认关闭；provider 原始正文和 secret 不进入日志、错误或测试夹具。

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

## Task 1：固定 OAuth 安全契约与 ADR

**Files:**

- Create: `docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md`
- Modify: `src/xiaowei_agent/interfaces/web_auth.py`
- Create: `tests/contract/test_feishu_oauth_adapter.py`
- Modify: `tests/security/test_web_auth_boundary.py`
- Modify: `tests/security/test_module_layering.py`

**Step 1: 写 RED 契约测试**

测试必须先证明：

- authorization URL 只允许飞书官方 HTTPS host，精确携带一次 `state` 和固定 callback；
- `exchange_code()` 只接受非空 code，只返回 `FeishuOAuthIdentity(subject_ref=open_id)`；
- provider 拒绝映射为 `FeishuOAuthCodeError`，超时/网络错误映射为 `FeishuOAuthUnavailableError`；
- 无效 JSON、未知字段、缺 `open_id`、超大响应和控制字符全部拒绝；
- 日志和异常不含 code、token、app secret、provider 正文或用户身份原文；
- adapter 无法构造 tenant、environment、actor 或 permissions。

Run: `python -m pytest tests/contract/test_feishu_oauth_adapter.py tests/security/test_web_auth_boundary.py -q`

Expected: 新 adapter 尚不存在，测试失败。

**Step 2: 写 ADR**

ADR 固定：官方 endpoint 为代码闭集而非可由用户/模型修改的任意 URL；secret 只从只读文件读取；state/session 继续由现有 PostgreSQL 契约承重；OAuth 身份只含 `open_id`；Compose HTTP 与已有 HTTPS SSO public origin 的职责分开。

**Step 3: 收紧现有端口说明**

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

**Step 4: 提交契约与 ADR**

```bash
git add docs/adr/ADR-014-real-feishu-oauth-and-web-activation.md src/xiaowei_agent/interfaces/web_auth.py tests/contract/test_feishu_oauth_adapter.py tests/security/test_web_auth_boundary.py tests/security/test_module_layering.py
git commit -m "docs(feishu): define real oauth activation boundary"
```

## Task 2：实现最小真实 OAuth adapter

**Files:**

- Create: `src/xiaowei_agent/interfaces/feishu_oauth.py`
- Modify: `src/xiaowei_agent/interfaces/__init__.py`
- Modify: `tests/contract/test_feishu_oauth_adapter.py`
- Modify: `tests/security/test_module_layering.py`

**Step 1: 注入离线 transport 并保持生产面单一**

adapter 对外只有一个类；测试通过私有 transport seam 提供录制响应，不启动 socket。生产实现用标准库 HTTP 调用并通过 `asyncio.to_thread` 隔离阻塞 I/O，避免新增运行依赖。

```python
class FeishuOAuthAdapter(FeishuOAuthPort):
    def __init__(
        self, *, app_id: str, app_secret_file: str, timeout_seconds: float
    ) -> None: ...
```

secret reader 必须拒绝 symlink、非普通文件、空文件和超限文件，且不在 `repr` 中保存 secret。

**Step 2: 实现最小 GREEN**

- URL 构造使用固定官方 host 和 `urllib.parse.urlencode`；
- code exchange 设置 5 秒上限，限制响应字节数并严格校验 JSON；
- 成功后只保留 `open_id`；access token 和原始响应在局部变量范围内消失；
- 所有错误转成既有安全异常，不记录 provider body。

Run: `python -m pytest tests/contract/test_feishu_oauth_adapter.py -q`

Expected: 全部通过，且测试无网络权限也能运行。

**Step 3: 做安全反证**

临时放宽官方 host 或把 provider body 放进异常，确认对应安全测试变红；恢复承重保护后重跑。反证只在独立临时副本执行，不提交变异代码。

Run: `python -m pytest tests/security/test_web_auth_boundary.py tests/security/test_module_layering.py -q`

Expected: 恢复后通过。

**Step 4: 提交 adapter**

```bash
git add src/xiaowei_agent/interfaces/feishu_oauth.py src/xiaowei_agent/interfaces/__init__.py tests/contract/test_feishu_oauth_adapter.py tests/security/test_module_layering.py
git commit -m "feat(feishu): add bounded oauth adapter"
```

## Task 3：装配 Web 启动入口和配置

**Files:**

- Modify: `src/xiaowei_agent/config.py`
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `tests/unit/test_config_happy.py`
- Modify: `tests/unit/test_feishu_config.py`
- Modify: `tests/unit/test_web_config.py`
- Modify: `tests/security/test_config_fail_fast.py`
- Modify: `tests/contract/test_web_app_routes.py`
- Modify: `tests/security/test_web_auth_boundary.py`

**Step 1: 写 RED 配置和启动测试**

新增明确开关 `XIAOWEI_FEISHU_OAUTH_ENABLED`，默认 `false`。测试以下组合：

- Web 开、OAuth 关：启动失败且无网络；
- OAuth 开但缺 app ID、secret file、身份文件或 HTTPS public origin：启动失败；
- 配置完整：composition root 创建 OAuth、成员 adapter 和 FastAPI app；
- app secret 文件非法：统一 `configuration_error`，不回显路径或值；
- 启动失败时已创建的数据库 engine 被关闭。

Run: `python -m pytest tests/unit/test_config_happy.py tests/unit/test_feishu_config.py tests/unit/test_web_config.py tests/security/test_config_fail_fast.py tests/contract/test_web_app_routes.py -q`

Expected: 新开关与装配尚不存在，测试失败。

**Step 2: 实现 composition root**

`web_app.main()` 依次加载配置、构造 OAuth adapter、构造现有 Feishu membership adapter、调用 `build_postgres_web_stack()`、创建 FastAPI app 并交给 uvicorn。任何半配置 fail-closed；handler 不直接持有 app secret。

**Step 3: 复核 HTTP 与 HTTPS 职责**

- `web_bind_host=0.0.0.0` 只表示容器监听；
- `web_detail_base_url` 仍要求用户已有的 HTTPS SSO origin；
- OAuth redirect、Secure cookie、SameSite、CSRF 和 HSTS 继续按 public origin 契约工作；
- 不在应用中实现 TLS 或代理。

Run: `python -m pytest tests/contract/test_web_app_routes.py tests/security/test_web_auth_boundary.py -q`

Expected: 完整配置可装配，缺配置安全失败。

**Step 4: 提交启动装配**

```bash
git add src/xiaowei_agent/config.py src/xiaowei_agent/interfaces/local_stack.py src/xiaowei_agent/interfaces/web_app.py tests/unit/test_config_happy.py tests/unit/test_feishu_config.py tests/unit/test_web_config.py tests/security/test_config_fail_fast.py tests/contract/test_web_app_routes.py tests/security/test_web_auth_boundary.py
git commit -m "feat(web): activate real oauth composition root"
```

## Task 4：加入 Compose 配置契约

**Files:**

- Modify: `docker-compose.yml`
- Modify: `tests/contract/test_compose_contract.py`
- Modify: `tests/contract/test_compose_smoke_script.py`
- Modify: `README.md`
- Modify: `AGENT_HANDOFF.md`

**Step 1: 写 RED Compose 契约**

断言：

- `web-app` 仍在 `m7-channels` profile，宿主发布 `8080:8080` 以支持 `IP:端口`；
- app secret 由 Compose secret 只读挂载，身份目录由只读 config/bind mount 提供；
- 所有真实飞书开关默认关闭；
- API 端口不因 Web 开放而自动暴露；
- image tag 可由非秘密环境变量选择，但默认值仍可本地启动。

Run: `python -m pytest tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py -q`

Expected: 新契约先失败。

**Step 2: 最小修改 Compose 和 README**

只增加渠道 profile 所需的环境变量、只读挂载和 Web 端口发布；不加入 nginx、Traefik、证书容器或第二套部署编排。README 只说明配置字段、开关和现有 SSO 前提，不写真实值。

**Step 3: 更新 handoff 证据口径**

只记录精确 PR SHA 和离线命令。明确写“未调用真实飞书、未部署、未 canary、未用户验收”。

**Step 4: 提交 Compose 契约**

```bash
git add docker-compose.yml tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py README.md AGENT_HANDOFF.md
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
- PR 的最高证据等级为 `tests`；不得写成 `test-env verified`。
- 精确 SHA 已交给 Codex 逐行审查；用户未明确下达阶段 2 现场 GO 前停止。

## 回滚

关闭 `XIAOWEI_FEISHU_OAUTH_ENABLED` 和 `XIAOWEI_WEB_APP_ENABLED`，重建 `web-app`；其他 worker、capability、Policy 和数据库 schema 不受影响。
