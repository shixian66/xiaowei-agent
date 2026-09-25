# S0 首登与入口体验 实施计划（Approved V0.1）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **状态：Approved V0.1**。经独立复审批准，由 PR #89 合入 `main@86263e9`；负责人随后下达 S0 开工口令。
> 批准范围仅为本计划四项，不授权 W5-C、真实 Provider、部署、canary 或 UAT。
> 上位计划：[完整功能部署验收总控计划](2026-09-25-full-feature-deployment-acceptance.md) 切片 S0。

**Goal:** 让 Local Admin 在浏览器里不借助手输 URL 就能完成“登录 → 强制改密 → 工作台 → 管理中心”，并去掉 provider-off 下误导的飞书文案。

**Architecture:** 只改 Web 入口层（`interfaces/web_app.py`）与静态壳（`web_static/`）。三个 HTML 壳路由在“已登录但未改密”时改为 302 到登录入口（登录入口已会渲染改密壳）；JSON API 语义不变。认证服务、Session、CSRF、Origin/Host 校验与权限闸门均不改。

**Tech Stack:** Starlette/FastAPI、httpx `ASGITransport`、pytest、原生 ES module。

## Global Constraints

- 入口层保持薄（AGENTS 边界 3）：不在 handler 中新增权限判断，只把既有 `_PasswordChangeRequiredError` 在**壳路由**上换成重定向。
- `/app/api/*`、`/admin/api/*`、`/login/api/*` 的状态码与错误体**一律不变**（仍为 `403 {"error":{"code":"password_change_required"}}`）。
- 新增路由只有 `GET /`；它同样受 Host/Origin 可信校验中间件约束，不加入健康检查例外。
- 管理中心入口只是展示；可见性按 `/app/api/me` 返回的 `admin_capabilities` 判定，服务端 `/admin` 闸门不变。
- 不引入内联脚本/样式（CSP 与 `test_shells_use_only_external_styles_and_scripts` 承重）。
- 静态壳标记漂移时启动即失败（沿用 `_provider_off_workbench_shell` 的 exactly-once 替换模式）。

## 已核对的源码事实（基线 `main@d4e619a`）

| 问题 | 事实 | 位置 |
| --- | --- | --- |
| 首登后看到 JSON | `login()` 固定返回 `destination=/app`；`shell()` 只捕获认证错误，`workbench_session → session_allowed_to_work` 抛出的 `_PasswordChangeRequiredError` 被全局处理器转成 JSON 403 | `web_app.py` 1835–1843、1756–1765、1582 |
| 改密壳入口已存在 | `GET /login` 对 `must_change_password` 会话渲染 `_password_change_shell` | `web_app.py` 1812–1820 |
| `/admin`、`/app/tasks/{id}` 同样问题 | `admin_session`、`safe_task_session` 同经 `session_allowed_to_work` | `web_app.py` 1845–1870；`_TASK_ID_RE` 校验在 822 |
| `/` 为 404 | 路由闭集没有 `GET /` | `test_web_app_routes.py` 711 起的闭集断言 |
| 飞书文案 | `_login_shell` 只隐藏 `#oauth-entry`，`auth-intro` 仍写“其他成员使用飞书身份登录” | `web_app.py` 701–709，`login.html` 27 |
| 无管理中心入口 | `index.html` 顶栏只有环境、用户与退出；`app.js` 的 `loadIdentity()` 只设文本 | `index.html` 26–38，`app.js` 482–491 |
| 改密后跳转 | `change_password` 返回 `web_return_path(intent)`，改密通过后 `/app` 正常渲染 | `test_a_browser_completes_the_first_run_without_reading_the_cookie` |

`IssuedWebSession` 不携带 `must_change_password`，因此**不改** `login()` 的 destination：浏览器照常去 `/app`，由壳路由 302 回登录入口。这样不触碰认证服务与其契约。

## 文件结构

- Modify: `src/xiaowei_agent/interfaces/web_app.py` — 三个壳路由捕获 `_PasswordChangeRequiredError`；新增 `GET /`；`_login_shell` 在 OAuth 不可用时同时替换引导文案。
- Modify: `src/xiaowei_agent/interfaces/web_static/index.html` — 顶栏新增隐藏的管理中心链接。
- Modify: `src/xiaowei_agent/interfaces/web_static/app.js` — 按能力显示该链接。
- Modify: `tests/security/test_ri5_web_assembly.py` — 三条壳路由的首登重定向与根路由用例（复用该文件 `_app`/`_client`/`_seed`/`_login_body`）。
- Modify: `tests/contract/test_web_app_routes.py` — 路由闭集加入 `("GET", "/")`。
- Modify: `tests/contract/test_web_static_assets.py` — 管理中心链接与登录文案静态契约。
- Modify: `AGENT_HANDOFF.md` — 记录 S0 离线证据（实现 PR 内）。

---

### Task 1：未改密会话访问 HTML 壳时回到登录入口

**Files:**
- Modify: `src/xiaowei_agent/interfaces/web_app.py`（`shell`、`admin_shell_route`、`detail_shell_route`）
- Test: `tests/security/test_ri5_web_assembly.py`

**Interfaces:**
- Consumes: `_PasswordChangeRequiredError`、`_login_redirect(intent)`、`WebReturnIntent`、`WebReturnIntentKind`（均已存在于 `web_app.py`）。
- Produces: 未改密时 `GET /app` → `302 /login?intent=workbench`；`GET /admin` → `302 /login?intent=admin_center`；`GET /app/tasks/{id}` → `302 /login?intent=safe_task_detail&task_id=…`。

- [ ] **Step 1: 写失败测试**（追加到 `tests/security/test_ri5_web_assembly.py`）

```python
@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/app", "/login?intent=workbench"),
        ("/admin", "/login?intent=admin_center"),
        ("/app/tasks/task-1", "/login?intent=safe_task_detail&task_id=task-1"),
    ],
)
async def test_unchanged_password_shells_redirect_to_the_change_form(
    clock, memory_state, path: str, location: str
) -> None:
    """首登后浏览器落在壳路由上，必须被带回能改密的页面，而不是一段 JSON。"""
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)

    async with _client(app) as client:
        await client.post(
            "/login/api/login",
            content=_login_body(initial),
            headers={"origin": origin, "content-type": "application/json"},
        )
        shell = await client.get(path, follow_redirects=False)
        api = await client.get("/app/api/me")

    assert (shell.status_code, shell.headers["location"]) == (302, location)
    # API 语义不变：脚本仍靠这个闭集码识别。
    assert api.status_code == 403
    assert api.json() == {"error": {"code": "password_change_required"}}


async def test_browser_login_lands_on_the_change_form(clock, memory_state) -> None:
    app, admins, origin, initial, hash_password = _app(clock, memory_state)
    await _seed(admins, hash_password, initial)

    async with _client(app) as client:
        signed_in = await client.post(
            "/login/api/login",
            content=_login_body(initial),
            headers={"origin": origin, "content-type": "application/json"},
        )
        landed = await client.get(signed_in.json()["destination"], follow_redirects=True)

    assert "change-password-form" in landed.text
```

详情壳在改密闸门处即返回，不触达 `task_access`（该夹具为 `_Unused()`），所以同一文件可覆盖三条壳路由；`task-1` 符合 `_TASK_ID_RE`。

- [ ] **Step 2:** 运行 `python -m pytest tests/security/test_ri5_web_assembly.py -q -k "change_form"`。
  预期：4 条失败，三条壳路由实得 `403`，`test_browser_login_lands_on_the_change_form` 页面中无 `change-password-form`。

- [ ] **Step 3: 最小实现**（`web_app.py`）

```python
    @app.get("/app")
    async def shell(request: Request) -> Response:
        intent = WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH)
        try:
            await workbench_session(request)
        except (WebAuthenticationError, LocalAdminAuthenticationError):
            return _login_redirect(intent)
        except _PasswordChangeRequiredError:
            # 登录入口已会为未改密会话渲染改密壳；壳路由只负责把浏览器带过去。
            return _login_redirect(intent)
        return HTMLResponse(index_shell)
```

`admin_shell_route` 同样处理（intent 为 `ADMIN_CENTER`）；`detail_shell_route` 在 `safe_task_session` 外加同一 `except`，先 `_task_id_or_not_found(task_id)` 再以 `SAFE_TASK_DETAIL` intent 重定向（与现有未登录分支同序，非法 task_id 仍按未登录语义处理）。

- [ ] **Step 4:** 重跑 Step 2 命令，预期全部通过；再跑 `python -m pytest tests/security/test_ri5_web_assembly.py tests/contract/test_web_app_routes.py tests/contract/test_web_detail_shell_scope.py -q` 全绿。

- [ ] **Step 5: 反证**：临时删掉 `shell` 中新增的 `except _PasswordChangeRequiredError` 分支（`PYTHONDONTWRITEBYTECODE=1`），确认 `/app` 用例与 `test_browser_login_lands_on_the_change_form` 转红；恢复。

- [ ] **Step 6: 提交** `fix(web): send unchanged-password shells back to the change form`

### Task 2：根路径进入登录入口

**Files:**
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Test: `tests/security/test_ri5_web_assembly.py`、`tests/contract/test_web_app_routes.py`

**Interfaces:**
- Produces: `GET /` → `302 /login?intent=workbench`（经 `_login_redirect`，不查 Session；登录入口自己决定渲染登录壳、改密壳或跳到 `/app`）。

- [ ] **Step 1: 写失败测试**

```python
async def test_root_is_the_login_entry(clock, memory_state) -> None:
    app, *_ = _app(clock, memory_state)
    async with _client(app) as client:
        response = await client.get("/", follow_redirects=False)
        direct = await client.get(
            "/", headers={"host": "127.0.0.1:8080"}, follow_redirects=False
        )
    assert (response.status_code, response.headers["location"]) == (
        302,
        "/login?intent=workbench",
    )
    # 根路径不是可信 Host 的例外。
    assert direct.status_code == 403
```

并在 `test_web_routes_and_internal_routes_are_mutually_closed` 的期望集合加入 `("GET", "/")`。

- [ ] **Step 2:** 运行两文件，预期新用例 `404`、闭集断言多出缺项而失败。
- [ ] **Step 3: 实现**：

```python
    @app.get("/")
    async def root_entry() -> Response:
        return _login_redirect(WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH))
```

注册位置放在 `@app.get("/login")` 之前。
- [ ] **Step 4:** 重跑，全绿。
- [ ] **Step 5: 反证**：删掉路由，确认两条断言转红；恢复。
- [ ] **Step 6: 提交** `fix(web): make the root path the login entry`

### Task 3：OAuth 不可用时登录页不提飞书

**Files:**
- Modify: `src/xiaowei_agent/interfaces/web_app.py`（`_login_shell`）
- Test: `tests/contract/test_web_static_assets.py`（纯函数用例，无需起 app）

**Interfaces:**
- Produces: `_login_shell(shell=…, oauth_available=False)` 同时隐藏 `#oauth-entry` 并把引导文案替换为 `请使用本地管理员账号登录。`；任一标记不是恰好出现一次时 `RuntimeError("login shell markup drifted")`。

- [ ] **Step 1: 写失败测试**

```python
def test_login_shell_without_oauth_does_not_mention_feishu() -> None:
    from xiaowei_agent.interfaces.web_app import _login_shell

    shell = (_STATIC / "login.html").read_text(encoding="utf-8")
    off = _login_shell(shell=shell, oauth_available=False)
    on = _login_shell(shell=shell, oauth_available=True)

    intro_off = re.search(r'<p class="auth-intro">(.*?)</p>', off).group(1)
    assert "飞书" not in intro_off
    assert intro_off == "请使用本地管理员账号登录。"
    assert '<div class="oauth-entry is-hidden" id="oauth-entry">' in off
    assert on == shell


def test_login_shell_markup_drift_fails_at_startup() -> None:
    import pytest

    from xiaowei_agent.interfaces.web_app import _login_shell

    with pytest.raises(RuntimeError, match="login shell markup drifted"):
        _login_shell(shell="<main></main>", oauth_available=False)
```

- [ ] **Step 2:** 运行 `python -m pytest tests/contract/test_web_static_assets.py -q -k login_shell`，预期两条失败。
- [ ] **Step 3: 实现**

```python
_LOGIN_OAUTH_ENTRY: Final[str] = '<div class="oauth-entry" id="oauth-entry">'
_LOGIN_INTRO_RE: Final[re.Pattern[str]] = re.compile(r'(<p class="auth-intro">)[^<]*(</p>)')
_LOCAL_ONLY_INTRO: Final[str] = "请使用本地管理员账号登录。"


def _login_shell(*, shell: str, oauth_available: bool) -> str:
    """按装配事实裁掉不可用的 OAuth 入口与文案，不生成第二套登录页面。"""
    if oauth_available:
        return shell
    if shell.count(_LOGIN_OAUTH_ENTRY) != 1:
        raise RuntimeError("login shell markup drifted")
    hidden = shell.replace(
        _LOGIN_OAUTH_ENTRY,
        '<div class="oauth-entry is-hidden" id="oauth-entry">',
        1,
    )
    replaced, count = _LOGIN_INTRO_RE.subn(rf"\g<1>{_LOCAL_ONLY_INTRO}\g<2>", hidden)
    if count != 1:
        raise RuntimeError("login shell markup drifted")
    return replaced
```

- [ ] **Step 4:** 重跑，全绿；再跑 `tests/security/test_ri5_web_assembly.py`（`oauth_available=False` 的整条首登链）全绿。
- [ ] **Step 5: 反证**：把 `subn` 替换恢复成原样返回，确认首条用例转红；恢复。
- [ ] **Step 6: 提交** `fix(web): drop the Feishu hint when OAuth is not assembled`

### Task 4：工作台显示管理中心入口

**Files:**
- Modify: `src/xiaowei_agent/interfaces/web_static/index.html`、`src/xiaowei_agent/interfaces/web_static/app.js`
- Test: `tests/contract/test_web_static_assets.py`

**Interfaces:**
- Consumes: `/app/api/me` 的 `admin_capabilities`（字符串数组，含 `view_integration_status` 时 `/admin` 可进）。
- Produces: `index.html` 中 `<a class="button button-quiet is-hidden" id="admin-center-link" href="/admin">管理中心</a>`；`app.js` 在 `loadIdentity()` 中按能力移除 `is-hidden`。

- [ ] **Step 1: 写失败测试**

```python
def test_workbench_links_to_the_admin_center_only_by_capability() -> None:
    index = (_STATIC / "index.html").read_text(encoding="utf-8")
    script = (_STATIC / "app.js").read_text(encoding="utf-8")

    assert (
        '<a class="button button-quiet is-hidden" id="admin-center-link" '
        'href="/admin">管理中心</a>'
    ) in index
    assert 'document.querySelector("#admin-center-link")' in script
    assert 'admin_capabilities.includes("view_integration_status")' in script
    # 入口只是展示，不绕过服务端：脚本不直接读取任何 /admin/api 路由。
    assert "/admin/api/" not in script
```

- [ ] **Step 2:** 运行 `python -m pytest tests/contract/test_web_static_assets.py -q -k admin_center`，预期失败。
- [ ] **Step 3: 实现**：在 `index.html` 的 `.topbar-actions` 中、`#logout-button` 之前插入上面的链接；`app.js` 的 `elements` 增加 `adminLink: document.querySelector("#admin-center-link")`，`loadIdentity()` 末尾加：

```javascript
  const capabilities = Array.isArray(me.admin_capabilities) ? me.admin_capabilities : [];
  elements.adminLink.classList.toggle(
    "is-hidden",
    !capabilities.includes("view_integration_status"),
  );
```

- [ ] **Step 4:** 重跑静态契约全文件，全绿；`node --check src/xiaowei_agent/interfaces/web_static/app.js` 通过。
- [ ] **Step 5: 反证**：删掉 `index.html` 中的链接，确认用例转红；恢复。
- [ ] **Step 6: 提交** `feat(web): link the workbench to the admin center for admins`

### Task 5：集成验证、视觉核对与交付

- [ ] 四门：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`，全绿并记录尾部输出。
- [ ] 本机 Chrome headless（真实 `create_app`、内存存储、合成凭据）在 1440/1280 核对：登录页（OAuth 关闭时无飞书文案）、首登后自动出现改密页、改密后工作台顶栏有“管理中心”、点击进入 `/admin`；operator 会话不显示该链接。
- [ ] 更新 `AGENT_HANDOFF.md`：S0 离线证据（命令、结果、反证、视觉核对范围）；注明未部署、未 canary、未 UAT。
- [ ] 开 PR，等待 exact-SHA 独立复审；不自行合并。合入后如需在本机体验，重新构建 release 镜像并按 W5 runbook 更新（仍只是本地体验，不是 W5-C 证据）。

## 验收标准

| 项 | 判据 |
| --- | --- |
| 首登 | 浏览器登录后直接看到改密表单；改密后进入工作台 |
| API 语义 | 三类 API 前缀的状态码与错误体与基线一致 |
| 根路径 | `/` → `302 /login?intent=workbench`；不可信 Host 仍 403 |
| 文案 | OAuth 未装配时登录页无“飞书”；装配时页面与基线逐字一致 |
| 管理入口 | 仅含 `view_integration_status` 的会话可见；`/admin` 服务端闸门不变 |
| 反证 | Task 1–4 各一项撤除后对应用例转红 |

## 不做

- 不改 `LocalAdminAuthService`、`IssuedWebSession`、Session/CSRF/Origin 逻辑或任何 API 错误体。
- 不改管理中心页面内容、配置/资源行为或 release 能力（均属后续切片）。
- 不部署、不改 Compose、不改数据库。

## 风险

- `/` 新路由被其他测试的路由闭集枚举：唯一的路由闭集在 `test_web_app_routes.py`（Task 2 更新）；`test_ri5_web_assembly.py` 720 附近是写路由 body-limit 清单，`GET /` 不需加入。
- 管理中心链接依赖前端判断：只影响可见性，越权访问仍由 `/admin` 服务端闸门拒绝。
