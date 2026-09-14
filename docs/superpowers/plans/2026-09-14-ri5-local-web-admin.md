# RI5 本地 Web Admin 与第三方配置实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让本机 Compose 在局域网范围内跑通 `本地管理员登录 → Web 保存 Gemini/飞书配置 → 宿主机重启 → 服务加载回执 → 管理员手动测试` 这一条最小管理闭环。

**Architecture:** 新增一个宿主机单文件 `.config/integrations.json` 作为两个 Provider 凭据的唯一明文真源，取代现有两条 Compose secret 路径；新增本地管理员认证与三张 Provider 状态表；Web 增加显式 `lan_http` 模式与配置面板；连接测试是控制面探针，不进入 Task/Evidence/ToolGateway。

**Tech Stack:** Python 3.11、Pydantic v2、FastAPI/Starlette、SQLAlchemy + asyncpg、Alembic、`hashlib.scrypt`（标准库）、原生 HTML/CSS/JS、Docker Compose。

**Spec:** [RI5 简化设计](../../plans/RI5-local-web-admin-simplified-design.md)（2026-09-14 Accepted）。授权边界见 [ADR-007](../../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) §RI5 Amendment、[ADR-014](../../adr/ADR-014-real-feishu-oauth-and-web-activation.md) 与 [ADR-015](../../adr/ADR-015-real-model-provider-boundary.md) 的 §RI5 修订。

## Global Constraints

这些约束对每个 Task 都成立，逐条抄自已接受的设计与 ADR：

- **不新增第三方依赖。** 口令哈希用标准库 `hashlib.scrypt`（随机 16-byte salt，`n=2**14`、`r=8`、`p=1`、`dklen=32`），校验用 `hmac.compare_digest`。`tests/security/test_dependency_baseline.py` 的运行期依赖闭集不得改动。
- **验证入口固定为四条：** `python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。禁止裸 `pytest`。
- **探针默认关闭。** `XIAOWEI_GEMINI_REAL_TEST_ENABLED` / `XIAOWEI_FEISHU_REAL_TEST_ENABLED` 默认 `false`；关闭时接口返回闭集 `REAL_TEST_DISABLED`，**外部调用数必须为零**。本计划不授予 RI3 PR 3E 与 RI2 的真实调用 GO，全程不得读取真实凭据或联网。
- **不改 `web_oauth_states` 的表、列或索引。** 测试 state 与登录 state 只用不同 digest domain 隔离（登录沿用 `oauth-state:v1`）。
- **协议放开只作用于本方 public origin。** `interfaces/web_auth.py:162` 的 `_split_https_url()` 同时服务 `_public_origin_is_safe()` 与 `_authorization_url_is_safe()`，必须拆成两个 helper；provider 授权/token URL 永远 HTTPS-only。禁止直接放宽共用 helper。
- **基础 `docker-compose.yml` 始终只发布 `127.0.0.1:8080`**，任何阶段都不改基础文件；局域网发布只能由独立 override 承担，且必须在首次强制改密后启用。
- **Secret 永不回显。** 查询接口只返回「已配置/未配置」；保存请求未携带某 Secret 字段时保留原值，清除用显式动作，不得用空字符串暗示。
- **不新增执行进程、dispatch lane 或 capability。** 探针不创建 Task、TaskSubmission、Evidence，不经过数据面 `ToolGateway`，不改变任何服务 readiness。
- **任务模型调用仍 worker-only。** Web 不得取得 `IntentModelPort` / `SlowQueryAdvisoryPort`。
- **不做**：多用户、RBAC、找回密码、通用 provider registry、配置历史、热加载、自动回滚、自动重启、Docker 控制、心跳、多实例、公网部署、StarRocks Admin 配置。
- 伪造 secret 字面量一律拆开写（`"hunter" + "2-plain"`），避免 `secret-scan` 与 `tests/security/test_secret_shaped_literals.py` 拦截。
- 每个 Task 结束时工作区干净、可独立评审；不使用 `git add -A`。
- **测试规格约定**：计划中给出完整函数体的测试块，实现时逐字照抄。少数块以
  `def test_x() -> None:` 加 `# Given / When / Then` 注释给出**断言规格**——这些不是待补的
  占位符，而是必须先按规格写出完整失败测试、确认变红、再写实现。规格里的每个 Given/When/Then
  都必须落成至少一条断言，不得合并或省略。

---

### Task 0: 环境同步与基线复核

本机 `.venv` 缺 `google-genai` 与 `lark-oapi`（RI1/RI3 依赖，`pyproject.toml` 已钉），4 个测试文件
无法收集、8 个用例失败。后续每个 Task 都要求四条基线全绿，因此必须先把环境补齐，**不能**留到最后。

**Files:** 无（只同步环境并记录基线）

**Interfaces:**
- Consumes: 无
- Produces: 一份「开工前基线」记录，供后续 Task 判断某个失败是不是自己引入的

- [x] **Step 1: 同步依赖并激活虚拟环境**

本机**没有裸 `python` 命令**，`.venv` 也不会自动激活。四条基线命令里的 `python` 全部指
`.venv` 里那个，所以必须先激活（或全程用 `.venv/bin/python` 显式调用）：

```bash
uv sync --extra dev --frozen
source .venv/bin/activate
python -V            # 应为 Python 3.11.x，且路径在 .venv 下
which python         # 应指向 <repo>/.venv/bin/python
```

后续所有 Task 的 `python -m pytest ...` 都默认在这个已激活的 shell 里执行。新开终端要重新
`source`，否则会命中「command not found: python」而不是真正的测试失败。

- [x] **Step 2: 跑四条基线并记录**

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

Expected: 四条全绿。若仍有失败，**先修环境或停下来报告，不要开工**——后续 Task 的「全绿」判据建立在这一步之上。

- [x] **Step 3: 记录基线**

把四条命令的尾部输出记进本 Task 的执行记录。后续任一 Task 出现失败时，先与这份基线对比，确认是本轮引入而非既有。

**开工前基线（2026-09-14，`main@c9b3cae`，分支 `claude/ri5-implementation`）：**

```text
uv sync --extra dev --frozen   # 补入 google-genai==2.23.0、lark-oapi==1.7.3 等 14 个包
python -V                      Python 3.11.16  (<repo>/.venv/bin/python)

python -m pytest -q            3534 passed, 227 skipped, 5 warnings in 21.44s
python -m pytest -m security -q 1290 passed, 80 skipped, 2391 deselected, 5 warnings in 9.90s
ruff check .                   All checks passed!
mypy src                       Success: no issues found in 164 source files
```

同步前的 4 个收集错误（`test_gemini_sdk_seam.py`、`test_protocol_conformance.py`、
`test_gemini_credential_boundary.py` 等）与 8 个失败用例全部消失，确认它们只是缺依赖。
四条基线全绿，可以开工。

---

### Task 1: Web 模式、单一 public origin 与双层启用开关

把 `web_detail_base_url` 机械重命名为 `web_public_origin`，引入 `XIAOWEI_WEB_MODE`，并解开 `web_app_enabled == feishu_oauth_enabled` 的耦合。这是纯配置契约层，先落地才能让后续 Task 有稳定字段名可用。

**Files:**
- Modify: `src/xiaowei_agent/config.py`（`_https_origin` 附近新增模式化 origin 校验；`Settings` 字段；`_feishu_profiles_are_closed` 于 `config.py:366-418`；`_FIELD_TO_ENV` 于 `config.py:513-549`）
- Modify: `.env.example`
- Modify: 机械重命名波及的其余 20 个文件（见 Step 6 清单）
- Test: `tests/unit/test_web_config.py`
- Test: `tests/unit/test_feishu_config.py`

**Interfaces:**
- Consumes: 无（首个 Task）
- Produces:
  - `class WebMode(StrEnum): LAN_HTTP = "lan_http"; HTTPS = "https"`（置于 `contracts/enums.py`）
  - `Settings.web_mode: WebMode = WebMode.HTTPS`
  - `Settings.web_public_origin: StrictStr | None = None`（取代 `web_detail_base_url`）
  - `Settings.gemini_real_test_enabled: bool = False`
  - `Settings.feishu_real_test_enabled: bool = False`
  - `def canonical_web_public_origin(value: str, *, mode: WebMode) -> str`——按模式规范化并返回 origin，非法抛 `ValueError`

- [x] **Step 1: 写失败测试——模式化 origin 校验**

在 `tests/unit/test_web_config.py` 追加：

```python
import pytest
from xiaowei_agent.config import ConfigError, canonical_web_public_origin, load_settings
from xiaowei_agent.contracts import WebMode


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.1:8080",
        "http://192.168.1.20:8080",
        "http://10.0.0.5:8080",
        "http://172.16.0.9:8080",
    ],
)
def test_lan_http_accepts_loopback_and_rfc1918_with_explicit_port(value: str) -> None:
    assert canonical_web_public_origin(value, mode=WebMode.LAN_HTTP) == value


@pytest.mark.parametrize(
    "value",
    [
        "http://8.8.8.8:8080",          # 公网 IP
        "http://192.168.1.20",          # 缺显式端口
        "https://192.168.1.20:8080",    # 协议与模式错配
        "http://example.com:8080",      # lan_http 不接受主机名
        "http://192.168.1.20:8080/app", # 带路径
        "http://user@192.168.1.20:8080",
    ],
)
def test_lan_http_rejects_everything_else(value: str) -> None:
    with pytest.raises(ValueError):
        canonical_web_public_origin(value, mode=WebMode.LAN_HTTP)


@pytest.mark.parametrize(
    "value",
    ["http://192.168.1.20:8080", "https://192.168.1.20:8080", "https://127.0.0.1:8443"],
)
def test_https_mode_still_rejects_http_and_ip_literals(value: str) -> None:
    with pytest.raises(ValueError):
        canonical_web_public_origin(value, mode=WebMode.HTTPS)


def test_https_mode_accepts_the_existing_sso_hostname_shape() -> None:
    assert (
        canonical_web_public_origin("https://sso.example.com", mode=WebMode.HTTPS)
        == "https://sso.example.com"
    )
```

- [x] **Step 2: 写失败测试——解耦与双层开关**

同文件追加。`_env()` 沿用该文件既有的最小环境构造 helper；若不存在则新增一个返回必填 `XIAOWEI_ENVIRONMENT_ID=dev` 等键的字典函数。

```python
def test_web_app_no_longer_requires_feishu_oauth() -> None:
    settings = load_settings(
        _env(
            XIAOWEI_WEB_APP_ENABLED="true",
            XIAOWEI_FEISHU_OAUTH_ENABLED="false",
            XIAOWEI_WEB_MODE="lan_http",
            XIAOWEI_WEB_PUBLIC_ORIGIN="http://127.0.0.1:8080",
        )
    )
    assert settings.web_app_enabled is True
    assert settings.feishu_oauth_enabled is False


def test_real_test_switches_default_to_false() -> None:
    settings = load_settings(_env())
    assert settings.gemini_real_test_enabled is False
    assert settings.feishu_real_test_enabled is False


def test_web_app_still_requires_a_public_origin() -> None:
    with pytest.raises(ConfigError):
        load_settings(_env(XIAOWEI_WEB_APP_ENABLED="true", XIAOWEI_WEB_MODE="lan_http"))


def test_disabled_web_and_worker_must_not_carry_a_public_origin() -> None:
    with pytest.raises(ConfigError):
        load_settings(_env(XIAOWEI_WEB_PUBLIC_ORIGIN="https://sso.example.com"))
```

- [x] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_web_config.py -q`

Expected: FAIL —— `ImportError: cannot import name 'canonical_web_public_origin'`、`WebMode` 不存在。

- [x] **Step 4: 实现最小契约**

在 `contracts/enums.py` 追加 `WebMode`（并加入该模块 `__all__` 与 `contracts/__init__.py` 的导出，`tests/contract/test_contract_enum_references.py` 会机械核对）。

在 `config.py` 中，保留现有 `_https_origin` 不动（HTTPS 模式继续复用），新增：

```python
_RFC1918_NETWORKS: Final = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _lan_http_origin(value: str) -> str:
    """只接受显式 http + canonical loopback/RFC1918 IPv4 + 显式端口。"""
    if any(ord(c) < 0x20 or ord(c) == 0x7F or c.isspace() for c in value):
        raise ValueError("must be a lan_http origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("must be a lan_http origin") from None
    if (
        parsed.scheme != "http"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("must be a lan_http origin")
    literal = _canonical_ip_literal(parsed.hostname or "")
    if literal is None:
        raise ValueError("must be a lan_http origin")
    address = ipaddress.ip_address(literal)
    if not (
        address.version == 4
        and (address.is_loopback or any(address in net for net in _RFC1918_NETWORKS))
    ):
        raise ValueError("must be a lan_http origin")
    return f"http://{literal}:{port}"


def canonical_web_public_origin(value: str, *, mode: WebMode) -> str:
    """按 Web 模式规范化本方 public origin；模式与协议不能错配。"""
    if mode is WebMode.LAN_HTTP:
        return _lan_http_origin(value)
    origin = _https_origin(value)
    if canonical_non_ip_hostname(urlsplit(origin).hostname or "") is None:
        raise ValueError("https origin requires a hostname")
    return origin
```

`Settings` 改动：
- 删除 `web_detail_base_url`，新增 `web_mode: WebMode = WebMode.HTTPS` 与 `web_public_origin: StrictStr | None = None`；
- 新增 `gemini_real_test_enabled: bool = False`、`feishu_real_test_enabled: bool = False`；
- 新增 `@model_validator(mode="after")`，当 `web_public_origin` 非空时调用 `canonical_web_public_origin(..., mode=self.web_mode)` 并以规范化结果回写（用 `object.__setattr__` 前先构造，或改为在 `load_settings` 规范化后再构造 `Settings`——选后者，保持 `frozen=True` 不被绕过）。

`_feishu_profiles_are_closed`（`config.py:366`）改动：
- **删除**首行 `if self.web_app_enabled != self.feishu_oauth_enabled: raise ...`；
- `web_origin` 元组改用 `self.web_public_origin`；
- `if self.web_app_enabled:` 分支改为**只**要求 `web_origin` 非空，不再要求 `shared + identity`（飞书 OAuth 变可选插件）；
- 新增 `if self.feishu_oauth_enabled:` 分支，要求 `shared + identity + web_origin` 齐备；
- 删除原 `web_app_enabled` 分支里基于 `canonical_non_ip_hostname` 的 hostname 断言（已移入 `canonical_web_public_origin`）。

`_FIELD_TO_ENV` 改动：删除 `"web_detail_base_url": "XIAOWEI_WEB_DETAIL_BASE_URL"`，新增 `"web_mode": "XIAOWEI_WEB_MODE"`、`"web_public_origin": "XIAOWEI_WEB_PUBLIC_ORIGIN"`、`"gemini_real_test_enabled": "XIAOWEI_GEMINI_REAL_TEST_ENABLED"`、`"feishu_real_test_enabled": "XIAOWEI_FEISHU_REAL_TEST_ENABLED"`。

同步 `.env.example`（`tests/security/test_env_example_clean.py:38` 断言键集合与 `_FIELD_TO_ENV.values()` **全等**）：

```
XIAOWEI_WEB_MODE=https
XIAOWEI_WEB_PUBLIC_ORIGIN=
XIAOWEI_GEMINI_REAL_TEST_ENABLED=false
XIAOWEI_FEISHU_REAL_TEST_ENABLED=false
```

- [x] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_web_config.py tests/unit/test_feishu_config.py -q`

Expected: PASS。

- [x] **Step 6: 完成机械重命名并跑全量**

把剩余 `web_detail_base_url` / `XIAOWEI_WEB_DETAIL_BASE_URL` 引用全部改名。**不提供兼容别名**。用这条命令拿到完整清单，逐个改完后它必须返回空：

```bash
grep -rln "web_detail_base_url\|WEB_DETAIL_BASE_URL" --exclude-dir=__pycache__ --exclude-dir=.git .
```

已知波及（20 个，不含已在 Step 4 改过的 `config.py`/`.env.example`）：`README.md`、`docker-compose.smoke.yml`、`src/xiaowei_agent/application/channel_projection.py`、`src/xiaowei_agent/interfaces/web_app.py`、`src/xiaowei_agent/interfaces/local_stack.py`、`tests/unit/test_local_stack.py`、`tests/evals/test_m7_channel_safety.py`、`tests/contract/test_feishu_worker.py`、`tests/contract/test_compose_contract.py`、`tests/contract/test_web_app_routes.py`、`tests/contract/test_web_task_api.py`、`tests/contract/test_channel_projection_worker.py`、`tests/security/test_feishu_projection_safety.py`、`tests/security/test_web_auth_boundary.py`、`tests/security/test_projection_fencing.py`、`tests/integration/test_m7_channel_flow.py`、`docs/superpowers/plans/2026-09-10-feishu-oauth-web-activation.md`。

Run:

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

Expected: 全绿。

- [x] **Step 7: 提交**

```bash
git add \
  src/xiaowei_agent/config.py \
  src/xiaowei_agent/contracts/enums.py \
  src/xiaowei_agent/contracts/__init__.py \
  src/xiaowei_agent/application/channel_projection.py \
  src/xiaowei_agent/interfaces/web_app.py \
  src/xiaowei_agent/interfaces/local_stack.py \
  .env.example README.md docker-compose.smoke.yml \
  tests/unit/test_web_config.py \
  tests/unit/test_feishu_config.py \
  tests/unit/test_local_stack.py \
  tests/contract/test_feishu_worker.py \
  tests/contract/test_compose_contract.py \
  tests/contract/test_web_app_routes.py \
  tests/contract/test_web_task_api.py \
  tests/contract/test_channel_projection_worker.py \
  tests/security/test_feishu_projection_safety.py \
  tests/security/test_web_auth_boundary.py \
  tests/security/test_projection_fencing.py \
  tests/evals/test_m7_channel_safety.py \
  tests/integration/test_m7_channel_flow.py \
  docs/superpowers/plans/2026-09-10-feishu-oauth-web-activation.md
git commit -m "feat(ri5): add web mode and rename web origin to a single source"
```

**Task 1 执行记录（2026-09-14）：** 四条基线全绿——`3552 passed, 227 skipped`（基线 3534）、
`1290 security passed`、`ruff` 通过、`mypy` 164 files 通过。三处与计划文字不同，均已按根因处理：

1. **Step 6 的收敛命令按原文永远不会返回空。** 有三份文档写的正是「把 `web_detail_base_url`
   重命名为 `web_public_origin`」这句话本身——`docs/adr/ADR-014`、RI5 简化设计、以及本计划。
   改掉它们会让那句话失去含义。实际收敛判据为：

   ```bash
   grep -rl "web_detail_base_url\\|WEB_DETAIL_BASE_URL" --exclude-dir=__pycache__ \\
     --exclude-dir=.git --exclude-dir=.venv . \\
     | grep -v -e 'docs/adr/ADR-014' \\
               -e 'docs/plans/RI5-local-web-admin-simplified-design.md' \\
               -e 'docs/superpowers/plans/2026-09-14-ri5-local-web-admin.md'
   ```

   该命令已返回空。代码、配置、Compose、README 与旧里程碑计划共 20 个文件全部改名，无兼容别名。

2. **规范化落在 `@model_validator(mode="before")`，不在 `load_settings`。** 计划给了两条路并
   担心 `object.__setattr__` 绕过 `frozen=True`；before 校验器两个问题都没有：它在实例存在之前
   改的是入参 dict，且直接构造 `Settings(...)`（既有测试大量这么做）同样会走校验与规范化。
   只在 `load_settings` 规范化会让直接构造的路径失去校验。

3. **两条既有测试在字段统一后正面冲突，按「单一真源」收紧。**
   `test_valid_https_origin_host_forms_are_stored_canonically` 的 `ipv4`/`ipv6` 用例断言
   channel worker 侧接受 IP 字面量，而 `test_oauth_web_origin_requires_a_hostname` 用同样的取值
   断言必须拒绝——两者能共存只因为主机名断言写在 `web_app_enabled` 分支里。改名后只剩一个
   origin 字段，同一个值不可能在一个进程是主机名、在另一个进程是 IP。因此把主机名要求移进
   `canonical_web_public_origin`，删掉那两个用例并补上
   `test_https_public_origin_rejects_ip_literals_for_the_worker_too` 钉住新规则。
   同类：`test_gemini_setting_is_exactly_one_default_off_boolean` 与
   `test_model_configuration_surface_is_exactly_one_default_off_flag` 断言 `gemini_*` 恰好一个字段，
   新增 `gemini_real_test_enabled` 后改为断言闭集恰好是两个**布尔开关**（守的是「只有开关、
   没有凭据/端点/模型名」，不是「只有一个」）。

   实际改到的既有测试比计划清单多两个：`tests/unit/test_config_happy.py`、
   `tests/unit/test_gemini_config.py`，已按 `git status` 补进本 Task 的 `git add`。

---

### Task 2: `integrations.json` 契约、加固读取与原子写入

新增两个 Provider 凭据的唯一明文真源，以及一个与 `read_secret_file()` 并列、但面向严格 JSON schema 的加载器。本 Task 只做文件层，不接任何调用方。

**Files:**
- Create: `src/xiaowei_agent/contracts/integration_config.py`
- Create: `src/xiaowei_agent/interfaces/integration_config_file.py`
- Modify: `src/xiaowei_agent/contracts/__init__.py`
- Test: `tests/unit/test_integration_config.py`
- Test: `tests/security/test_integration_config_boundary.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class ProviderName(StrEnum): GEMINI = "gemini"; FEISHU = "feishu"`（`contracts/enums.py`）
  - `class GeminiIntegration(Contract): enabled: bool = False; api_key: SecretRef | None = None`
  - `class FeishuIntegration(Contract): enabled: bool = False; app_id: StrictStr | None = None; app_secret: SecretRef | None = None`
  - `class IntegrationConfig(Contract): generation: StrictInt = Field(gt=0); gemini: GeminiIntegration; feishu: FeishuIntegration`
  - `class IntegrationConfigError(RuntimeError)`
  - `class IntegrationConfigMissingError(IntegrationConfigError)`——**只**在路径本身不存在（`ENOENT`）
    时抛出。它是基类的子类，所以既有 `except IntegrationConfigError` 的调用方仍然 fail-closed；
    只有明确要区分「尚未配置」的调用方才捕获这一个子类。
  - `def read_integration_config(path: str) -> IntegrationConfig`
  - `def write_integration_config(path: str, config: IntegrationConfig) -> None`——同目录临时文件 + `0600` + `os.replace()`
  - `DEFAULT_INTEGRATION_CONFIG_PATH: Final[str] = "/run/xiaowei-config/integrations.json"`

`SecretRef` 是本 Task 新增的 `Annotated[StrictStr, AfterValidator(...)]`：非空、无控制字符、长度 ≤ 4096，且其 `__repr__`/序列化不参与 `model_dump()` 的默认输出（用 `Field(exclude=True)` 在 `IntegrationConfig.model_dump()` 时排除，另给显式 `secret_values()` 访问器）。

- [x] **Step 1: 写失败测试——契约与脱敏**

`tests/unit/test_integration_config.py`：

```python
import pytest
from pydantic import ValidationError
from xiaowei_agent.contracts import (
    FeishuIntegration,
    GeminiIntegration,
    IntegrationConfig,
)


def _config(**kwargs: object) -> IntegrationConfig:
    base = {
        "generation": 1,
        "gemini": GeminiIntegration(enabled=False),
        "feishu": FeishuIntegration(enabled=False),
    }
    base.update(kwargs)
    return IntegrationConfig(**base)  # type: ignore[arg-type]


def test_generation_must_be_a_positive_integer() -> None:
    with pytest.raises(ValidationError):
        _config(generation=0)
    with pytest.raises(ValidationError):
        _config(generation=True)


def test_model_dump_never_carries_secret_values() -> None:
    fake_key = "AIza" + "-not-a-real-key"
    config = _config(gemini=GeminiIntegration(enabled=True, api_key=fake_key))
    dumped = config.model_dump()
    assert fake_key not in repr(dumped)
    assert dumped["gemini"]["enabled"] is True
    assert "api_key" not in dumped["gemini"]


def test_secret_values_are_reachable_only_through_the_explicit_accessor() -> None:
    fake_secret = "app" + "-secret-placeholder"
    config = _config(feishu=FeishuIntegration(enabled=True, app_id="cli_x", app_secret=fake_secret))
    assert config.feishu.secret_value() == fake_secret


def test_secret_rejects_control_characters_and_oversize() -> None:
    with pytest.raises(ValidationError):
        GeminiIntegration(enabled=True, api_key="bad\nvalue")
    with pytest.raises(ValidationError):
        GeminiIntegration(enabled=True, api_key="x" * 4097)
```

- [x] **Step 2: 写失败测试——加固读取与原子写入**

`tests/security/test_integration_config_boundary.py`：

```python
import json
import os
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

from xiaowei_agent.contracts import FeishuIntegration, GeminiIntegration, IntegrationConfig
from xiaowei_agent.interfaces.integration_config_file import (
    IntegrationConfigError,
    IntegrationConfigMissingError,
    read_integration_config,
    write_integration_config,
)


def _write_raw(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_roundtrip_preserves_generation_and_flags(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    config = IntegrationConfig(
        generation=3,
        gemini=GeminiIntegration(enabled=True, api_key="k" * 8),
        feishu=FeishuIntegration(enabled=False),
    )
    write_integration_config(str(target), config)
    loaded = read_integration_config(str(target))
    assert loaded.generation == 3
    assert loaded.gemini.enabled is True
    assert loaded.gemini.secret_value() == "k" * 8


def test_written_file_is_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    write_integration_config(
        str(target),
        IntegrationConfig(generation=1, gemini=GeminiIntegration(), feishu=FeishuIntegration()),
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    write_integration_config(
        str(target),
        IntegrationConfig(generation=1, gemini=GeminiIntegration(), feishu=FeishuIntegration()),
    )
    assert [p.name for p in tmp_path.iterdir()] == ["integrations.json"]


def test_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    _write_raw(real, {"generation": 1, "gemini": {"enabled": False}, "feishu": {"enabled": False}})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(IntegrationConfigError) as caught:
        read_integration_config(str(link))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_a_broken_symlink_is_a_failure_not_an_absence(tmp_path: Path) -> None:
    """断链是本条的要害：os.path.exists() 会说它不存在，从而把它降级成「尚未配置」。"""
    link = tmp_path / "integrations.json"
    link.symlink_to(tmp_path / "nowhere.json")
    assert os.path.exists(link) is False
    assert os.path.lexists(link) is True
    with pytest.raises(IntegrationConfigError) as caught:
        read_integration_config(str(link))
    assert not isinstance(caught.value, IntegrationConfigMissingError)


def test_only_a_truly_absent_path_reports_missing(tmp_path: Path) -> None:
    with pytest.raises(IntegrationConfigMissingError):
        read_integration_config(str(tmp_path / "integrations.json"))


def test_missing_is_a_subclass_so_unaware_callers_still_fail_closed() -> None:
    """反例守护：如果把它改成独立异常，所有只捕获基类的调用方会漏网。"""
    assert issubclass(IntegrationConfigMissingError, IntegrationConfigError)


def test_non_regular_file_is_refused(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(IntegrationConfigError):
        read_integration_config(str(fifo))


def test_relative_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(IntegrationConfigError):
        read_integration_config("integrations.json")


def test_oversize_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "integrations.json"
    target.write_text("[" + "0," * 200_000 + "0]", encoding="utf-8")
    with pytest.raises(IntegrationConfigError):
        read_integration_config(str(target))


@pytest.mark.parametrize(
    "payload",
    [
        {"generation": 0, "gemini": {"enabled": False}, "feishu": {"enabled": False}},
        {"gemini": {"enabled": False}, "feishu": {"enabled": False}},
        {"generation": 1, "gemini": {"enabled": False}, "feishu": {"enabled": False}, "extra": 1},
        {"generation": 1, "gemini": {"enabled": False, "model": "x"}, "feishu": {"enabled": False}},
    ],
)
def test_schema_violations_are_refused(tmp_path: Path, payload: object) -> None:
    target = tmp_path / "integrations.json"
    _write_raw(target, payload)
    with pytest.raises(IntegrationConfigError):
        read_integration_config(str(target))


def test_error_text_never_repeats_file_content(tmp_path: Path) -> None:
    fake = "sentinel" + "-secret-value"
    target = tmp_path / "integrations.json"
    _write_raw(target, {"generation": 1, "gemini": {"enabled": True, "api_key": fake}, "feishu": {}})
    with pytest.raises(IntegrationConfigError) as excinfo:
        read_integration_config(str(target))
    assert fake not in str(excinfo.value)


def test_read_secret_file_still_refuses_this_json(tmp_path: Path) -> None:
    """旧 reader 不得成为 JSON 的第二条读取路径。"""
    from xiaowei_agent.interfaces.secret_file import SecretFileError, read_secret_file

    target = tmp_path / "integrations.json"
    write_integration_config(
        str(target),
        IntegrationConfig(generation=1, gemini=GeminiIntegration(), feishu=FeishuIntegration()),
    )
    with pytest.raises(SecretFileError):
        read_secret_file(str(target))
```

- [x] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_integration_config.py tests/security/test_integration_config_boundary.py -q`

Expected: FAIL —— 模块不存在。

- [x] **Step 4: 实现契约与文件层**

`contracts/integration_config.py` 按 Interfaces 定义模型，全部继承既有 `Contract` 基类（`extra="forbid"`、`frozen=True`、`hide_input_in_errors=True` 由基类提供）。Secret 字段用 `Field(default=None, exclude=True)`，并各自提供 `secret_value() -> str` 访问器，缺失时抛 `ValueError`。

`interfaces/integration_config_file.py`：

```python
_MAX_CONFIG_BYTES: Final[int] = 65_536
DEFAULT_INTEGRATION_CONFIG_PATH: Final[str] = "/run/xiaowei-config/integrations.json"


def read_integration_config(path: str) -> IntegrationConfig:
    """从绝对路径读取严格 schema 的 integration 配置。"""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise IntegrationConfigError("integration config unavailable")
    flags = os.O_RDONLY
    for name in ("O_NONBLOCK", "O_CLOEXEC", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    descriptor: int | None = None
    payload = b""
    failed = False
    missing = False
    try:
        descriptor = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            failed = True
        else:
            payload = os.read(descriptor, _MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        # 只有 ENOENT 才是「尚未配置」。必须排在下面这条之前，否则会被它吞掉。
        missing = True
    except (OSError, ValueError):
        failed = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if missing:
        raise IntegrationConfigMissingError("integration config absent")
    if failed or not payload or len(payload) > _MAX_CONFIG_BYTES:
        raise IntegrationConfigError("integration config unavailable")
    try:
        document = json.loads(payload.decode("utf-8"))
        return IntegrationConfig.model_validate(document)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise IntegrationConfigError("integration config invalid") from None
```

「不存在」与「存在但坏」的判定只能放在 `os.open` 这一处，因为只有这里拿得到 errno。
不要在调用方用 `os.path.exists()` 预检：它**跟随**符号链接，断链会报 `False`，于是一个指向不存在
目标的 `integrations.json` 符号链接会被误判成「尚未配置」，绕过「符号链接一律故障」的设计；
`os.path.lexists()` 能修掉这一例，但仍留着 TOCTOU，且无法区分 `EACCES`、`ELOOP` 这些同样
「存在但不可读」的情况。实测（darwin 22，Linux 同）：断链 `exists=False lexists=True`，
而带 `O_NOFOLLOW` 的 `os.open` 对任何符号链接都抛 `ELOOP`，只有真正不存在的路径才抛 `ENOENT`。
分流点因此唯一，且没有检查与使用之间的时间窗。

`write_integration_config()` 在**同目录**创建 `tempfile.mkstemp(dir=os.path.dirname(path))` 临时文件，`os.fchmod(fd, 0o600)`，写入 `json.dumps(..., ensure_ascii=False, sort_keys=True)`（含 secret，用一个内部 `_to_document(config)` 而非 `model_dump()`），`os.fsync(fd)`，关闭后 `os.replace(tmp, path)`，并 `os.fsync` 目录 fd。任何异常路径都 `os.unlink(tmp)`。异常文本固定为两条常量，不回填内容。

- [x] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_integration_config.py tests/security/test_integration_config_boundary.py -q`

Expected: PASS。

- [x] **Step 6: 反证承重**

临时把 `read_integration_config` 里的 `O_NOFOLLOW` 去掉，确认 `test_symlink_is_refused` 变红；把 `except FileNotFoundError` 那一支删掉（让断链与缺失都落到 `failed`），确认 `test_only_a_truly_absent_path_reports_missing` 变红；反过来把它挪到 `except (OSError, ValueError)` 之后，确认同一条仍然变红（Python 按顺序匹配，排在后面永远不会命中）；再把 `exclude=True` 去掉，确认 `test_model_dump_never_carries_secret_values` 变红。四处都恢复后重跑全绿。

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/security/test_integration_config_boundary.py -q -p no:cacheprovider
```

- [x] **Step 7: 提交**

```bash
git add src/xiaowei_agent/contracts/integration_config.py src/xiaowei_agent/contracts/enums.py src/xiaowei_agent/contracts/__init__.py src/xiaowei_agent/interfaces/integration_config_file.py tests/unit/test_integration_config.py tests/security/test_integration_config_boundary.py
git commit -m "feat(ri5): add the integration config contract and hardened file layer"
```

**Task 2 执行记录（2026-09-14）：** 四条基线全绿——`3573 passed, 227 skipped`（Task 1 后 3552）、
`1307 security passed`、`ruff` 通过、`mypy` 166 files 通过。四条反证全部按预期变红后恢复：

```text
去掉 O_NOFOLLOW                     -> test_symlink_is_refused                        1 failed
删掉 except FileNotFoundError       -> test_only_a_truly_absent_path_reports_missing  1 failed
把它挪到 except (OSError, ...) 之后 -> test_only_a_truly_absent_path_reports_missing  1 failed
去掉 exclude=True                   -> test_model_dump_never_carries_secret_values    1 failed
恢复后                                                                               20 passed
```

三处与计划文字不同：

1. **`test_error_text_never_repeats_file_content` 的 payload 原本是合法的**，
   `{"generation": 1, "gemini": {...}, "feishu": {}}` 会通过校验（`feishu: {}` 就是全默认值），
   于是 `pytest.raises` 直接 `DID NOT RAISE`——这条断言等于没跑。改成在同一棵 gemini 子树里
   多放一个 `model` 键触发 `extra="forbid"`，让 payload **既非法又携带 secret**，并追加断言
   `fake not in str(excinfo.value.__context__)`：`from None` 只设 `__suppress_context__`，
   原始 `ValidationError` 仍挂在异常上，真正挡住原文的是 `Contract` 基类的
   `hide_input_in_errors=True`。

2. **落盘用 `indent=2`。** 计划只写了 `ensure_ascii=False, sort_keys=True`，但那样是单行 JSON，
   `read_secret_file()` 会把它当成一个合法的单行 credential 原样读出来，
   `test_read_secret_file_still_refuses_this_json` 必然失败。多行文档必含换行，旧 reader 的
   控制字符检查因此一定拒绝它——「旧 reader 不得成为第二条读取路径」这条属性由格式本身保证。

3. **三份闭集守卫需要登记新模块**（都是刻意设计成「新增即转红」的人工审查点）：
   `tests/security/test_module_layering.py` 的 `_ALLOWED_INTERNAL_BY_FILE` 登记
   `interfaces/integration_config_file.py`（只依赖 `xiaowei_agent.contracts`）；
   `tests/security/test_task_view_runtime_authority.py` 的 `_TASK_VIEW_PROCESS_ALLOWED_MODULES`
   登记 `xiaowei_agent.contracts.integration_config`（经 `contracts/__init__` 进入全部进程，
   是纯契约模块，无 I/O 无 SDK）。后者被 web / listener / channel worker 三个 surface 测试共用，
   一处登记同时修好四个失败。这两个文件已按 `git status` 补进 `git add`。

---

### Task 3: schema `rev_0010`、本地管理员认证与 HTTPS helper 拆分

三张新表加 `web_sessions` 两列，以及写这两列的 session 命令与 store、本地管理员认证、
`_split_https_url()` 的拆分。

**本 Task 只允许一次提交，在最后一步。** 加两个非空新列之后、`RotateWebSessionCommand` /
`WebSessionLookup` / `postgres.py` 的行写入面改完之前，仓库必然处于既有 session 写入被打坏的
状态——中途 commit 就是往历史里留一个跑不过测试的提交。schema 与写它的代码因此合并在这里，
中间步骤一律不 `git add`。

**Files:**
- Create: `src/xiaowei_agent/persistence/migrations/versions/rev_0010_local_admin_and_provider_state.py`
- Modify: `src/xiaowei_agent/persistence/schema.py`（`WEB_SESSIONS` 于 `schema.py:456`；`ALL_TABLES` 于 `schema.py:475`）
- Test: `tests/contract/test_schema_matches_migration.py`（既有，无需改，必须仍绿）
- Test: `tests/contract/test_ri5_schema.py`
- Test: `tests/integration/test_ri5_schema_migration.py`
- Create: `src/xiaowei_agent/persistence/local_admin.py`
- Create: `src/xiaowei_agent/interfaces/local_admin_auth.py`
- Modify: `src/xiaowei_agent/interfaces/web_auth.py`（`_split_https_url:162`、`_public_origin_is_safe:181`、`_authorization_url_is_safe:186`、`WebAuthService.__init__:206`、`authenticate:363`、`complete_login:281`）
- Modify: `src/xiaowei_agent/persistence/web_session.py`（`RotateWebSessionCommand:96`、`WebSessionLookup:109`、`WebSessionStore:117`）
- Modify: `src/xiaowei_agent/persistence/postgres.py`（`web_sessions` 读写）
- Modify: `src/xiaowei_agent/contracts/enums.py`（`IdentitySource:61`）
- Test: `tests/unit/test_local_admin_auth.py`
- Test: `tests/security/test_ri5_origin_split.py`
- Test: `tests/security/test_local_admin_boundary.py`

**Interfaces:**
- Consumes: Task 2 的 `ProviderName`；Task 1 的 `WebMode`、`Settings.web_mode`、`Settings.web_public_origin`
- Produces:
  - `LOCAL_ADMINS`、`SERVICE_CONFIG_STATE`、`PROVIDER_TEST_STATE` 三个 `sa.Table`，以及 `WEB_SESSIONS` 的 `auth_source`、`public_origin_digest` 两列
  - `IdentitySource.LOCAL_ADMIN = "local_admin"`
  - `def hash_password(password: str) -> str` / `def verify_password(password: str, encoded: str) -> bool`（`local_admin_auth.py`）
  - `class LocalAdminRecord(Contract): password_hash: StrictStr; must_change_password: bool`
  - `class LocalAdminStore(Protocol)`：
    - `async def seed_if_absent(*, password_hash: str) -> bool`
    - `async def get() -> LocalAdminRecord`
    - `async def change_password_and_rotate_session(*, command: ChangePasswordCommand) -> LocalAdminRecord`
      ——**一次提交内**完成四件事：更新 `password_hash`、置 `must_change_password=false`、
      撤销**全部** `auth_source='local_admin'` 的既有 session、插入新 session 行。密码更新与
      session 变更因此不再跨两个 Store，不会出现「密码已改但旧 session 还活着」的中间态。
  - `class ChangePasswordCommand(Contract)`：`password_hash: StrictStr`、`new_session_digest: Sha256Hex`、`public_origin_digest: Sha256Hex`、`session_ttl_seconds: StrictInt = Field(gt=0, le=86_400)`
  - `class LocalAdminAuthService`：`async def login(*, password: str, previous_session_cookie: str | None) -> IssuedWebSession`、`async def change_password(*, session_cookie: str, current: str, new: str) -> IssuedWebSession`
  - `def _provider_https_url(value: str) -> SplitResult | None`（原 `_split_https_url` 语义，仅供 provider URL）
  - `def public_origin_is_safe(value: str, *, mode: WebMode) -> bool`
  - `RotateWebSessionCommand` 新增 `auth_source: IdentitySource`、`public_origin_digest: Sha256Hex`
  - `WebSessionLookup` 新增 `public_origin_digest: Sha256Hex`
  - `LOCAL_ADMIN_PRINCIPAL`：`tenant_id="dev-local"`、`environment_id="dev"`、`actor="admin"`、权限 `{VIEW_SAFE_TASK, SUBMIT_READONLY_TASK, ADMIN_ALL_SAFE_TASKS}`

表形状（严格照设计）：

```text
local_admins
  id                  smallint primary key, CHECK (id = 1)   -- 最多一行
  password_hash       text not null                          -- 带版本与参数的封装
  must_change_password boolean not null
  updated_at          timestamptz not null

service_config_state
  service_name        text
  provider            text
  loaded_generation   integer not null CHECK (loaded_generation > 0)
  load_status         text not null CHECK (load_status IN ('loaded','invalid'))
                      -- invalid 表示「读到了这一代但没读成」。它与「缺回执」一样落到
                      -- PENDING_RESTART，不产生第六个页面状态；该列只供页面显示原因提示。
                      -- 文件缺失/整体损坏时读不出 generation，此时不写行（见 Task 4）。
  loaded_at           timestamptz not null
  primary key (service_name, provider)

provider_test_state
  check_name          text primary key
                      CHECK (check_name IN ('gemini_connection','feishu_credentials','feishu_oauth'))
  tested_generation   integer not null CHECK (tested_generation > 0)
  test_status         text not null CHECK (test_status IN ('passed','failed'))
  tested_at           timestamptz not null
  duration_ms         integer not null CHECK (duration_ms >= 0 AND duration_ms <= 600000)
  error_code          text null
  error_message       text null
  CHECK ((test_status = 'passed') = (error_code IS NULL))

web_sessions  (新增两列)
  auth_source         text not null CHECK (auth_source IN ('local_admin','feishu'))
  public_origin_digest char(64) not null
```

- [x] **Step 1: 写失败契约测试（schema）**

`tests/contract/test_ri5_schema.py`：

```python
import sqlalchemy as sa
from xiaowei_agent.persistence import schema


def test_local_admins_allows_at_most_one_row() -> None:
    table = schema.LOCAL_ADMINS
    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
    assert any("id" in str(c.sqltext) for c in checks)
    assert [c.name for c in table.primary_key.columns] == ["id"]


def test_service_config_state_is_keyed_by_service_and_provider() -> None:
    assert [c.name for c in schema.SERVICE_CONFIG_STATE.primary_key.columns] == [
        "service_name",
        "provider",
    ]


def test_provider_test_state_is_keyed_by_check_name() -> None:
    assert [c.name for c in schema.PROVIDER_TEST_STATE.primary_key.columns] == ["check_name"]
    assert "duration_ms" in schema.PROVIDER_TEST_STATE.c


def test_web_sessions_carries_auth_source_and_origin_digest() -> None:
    columns = schema.WEB_SESSIONS.c
    assert "auth_source" in columns and not columns["auth_source"].nullable
    assert "public_origin_digest" in columns and not columns["public_origin_digest"].nullable


def test_no_new_column_was_added_to_web_oauth_states() -> None:
    """ADR-014 R3：解除 schema 冻结不包含 web_oauth_states。"""
    assert set(schema.WEB_OAUTH_STATES.c.keys()) == {
        "state_digest",
        "issued_at",
        "expires_at",
        "consumed_at",
    }


def test_new_tables_are_registered() -> None:
    names = {t.name for t in schema.ALL_TABLES}
    assert {"local_admins", "service_config_state", "provider_test_state"} <= names
```

- [x] **Step 2: 写失败测试——HTTPS helper 拆分（本 Task 的承重项）**

`tests/security/test_ri5_origin_split.py`：

```python
import pytest

pytestmark = pytest.mark.security

from xiaowei_agent.contracts import WebMode
from xiaowei_agent.interfaces import web_auth


@pytest.mark.parametrize("mode", [WebMode.LAN_HTTP, WebMode.HTTPS])
def test_provider_authorization_url_is_always_https_only(mode: WebMode) -> None:
    """协议放开只作用于本方 origin；飞书授权 URL 在任何模式下都必须是 HTTPS。"""
    http_url = "http://open.feishu.cn/open-apis/authen/v1/index?state=s"
    assert web_auth._authorization_url_is_safe(http_url, expected_state="s") is False


def test_provider_token_url_helper_rejects_http() -> None:
    assert web_auth._provider_https_url("http://open.feishu.cn/x") is None
    assert web_auth._provider_https_url("https://open.feishu.cn/x") is not None


def test_public_origin_helper_is_mode_aware() -> None:
    assert web_auth.public_origin_is_safe("http://192.168.1.20:8080", mode=WebMode.LAN_HTTP)
    assert not web_auth.public_origin_is_safe("http://192.168.1.20:8080", mode=WebMode.HTTPS)
    assert web_auth.public_origin_is_safe("https://sso.example.com", mode=WebMode.HTTPS)
    assert not web_auth.public_origin_is_safe("https://sso.example.com", mode=WebMode.LAN_HTTP)


def test_the_two_helpers_are_not_the_same_object() -> None:
    """反例：如果实现只是把共用 helper 放宽并起了个别名，这条会红。"""
    assert web_auth._provider_https_url is not web_auth.public_origin_is_safe
    assert "mode" not in web_auth._provider_https_url.__code__.co_varnames
```

- [x] **Step 3: 写失败测试——口令与管理员边界**

`tests/unit/test_local_admin_auth.py`：

```python
import pytest
from xiaowei_agent.interfaces.local_admin_auth import hash_password, verify_password


def test_hash_is_salted_so_two_hashes_differ() -> None:
    password = "correct-horse" + "-battery"
    assert hash_password(password) != hash_password(password)


def test_verify_accepts_the_original_and_rejects_others() -> None:
    password = "correct-horse" + "-battery"
    encoded = hash_password(password)
    assert verify_password(password, encoded) is True
    assert verify_password(password + "x", encoded) is False


def test_encoding_records_algorithm_and_parameters() -> None:
    encoded = hash_password("pw" + "-placeholder")
    assert encoded.startswith("scrypt$1$")
    assert "16384" in encoded  # n = 2**14


def test_malformed_encodings_are_rejected_without_raising() -> None:
    for bad in ["", "scrypt$1$", "bcrypt$x$y$z", "scrypt$9$16384$8$1$aa$bb"]:
        assert verify_password("pw", bad) is False


def test_encoded_parameters_are_checked_not_trusted() -> None:
    """封装里的 n/r/p 不得被当成计算输入，否则可被降成廉价运算或内存耗尽。"""
    password = "pw" + "-placeholder"
    encoded = hash_password(password)
    _, version, _, r, p, salt, dk = encoded.split("$")
    weakened = "$".join(["scrypt", version, "2", r, p, salt, dk])
    inflated = "$".join(["scrypt", version, "1048576", r, p, salt, dk])
    assert verify_password(password, weakened) is False
    assert verify_password(password, inflated) is False


def test_truncated_salt_or_hash_is_rejected() -> None:
    import base64

    password = "pw" + "-placeholder"
    kind, version, n, r, p, _, dk = hash_password(password).split("$")
    short_salt = base64.b64encode(b"\x00" * 4).decode("ascii")
    assert verify_password(password, "$".join([kind, version, n, r, p, short_salt, dk])) is False
```

`tests/security/test_local_admin_boundary.py`（标 `security`）：

```python
def test_local_admin_principal_is_fixed():
    # tenant_id=dev-local / environment_id=dev / actor=admin / 三项权限
    raise NotImplementedError("按规格写出断言后删除本行")

def test_password_hash_never_appears_in_any_api_or_log_payload():
    # 登录/改密/查询三条路径的响应体与结构化日志里都不得出现 password_hash 片段
    raise NotImplementedError("按规格写出断言后删除本行")

def test_session_is_bound_to_the_public_origin_that_created_it():
    # 用 origin A 签发的 session，在 origin B 下 authenticate 必须 WebAuthenticationError
    raise NotImplementedError("按规格写出断言后删除本行")

def test_change_password_revokes_every_other_local_admin_session():
    # 改密前签发两个 local_admin session；改密后旧的两个都 authenticate 失败，
    # 新轮换出的那个可用；同一事务内完成
    raise NotImplementedError("按规格写出断言后删除本行")

def test_before_first_change_only_login_change_password_and_logout_are_allowed():
    # must_change_password=true 时，其余 /app/api/* 一律 403 且闭集码为 password_change_required
    raise NotImplementedError("按规格写出断言后删除本行")

def test_feishu_principal_with_admin_permission_cannot_reach_config_routes():
    # 持 ADMIN_ALL_SAFE_TASKS 的 FEISHU principal 访问配置与探针路由一律 403
    raise NotImplementedError("按规格写出断言后删除本行")
```

每条用 `tests/fakes` 下既有的 in-memory session store fake 驱动；按注释里的规格写出完整断言后再写实现。

- [x] **Step 4: 跑三组测试确认全部失败**

Run: `python -m pytest tests/contract/test_ri5_schema.py tests/security/test_ri5_origin_split.py tests/unit/test_local_admin_auth.py -q`

Expected: FAIL —— `AttributeError: module 'xiaowei_agent.persistence.schema' has no attribute 'LOCAL_ADMINS'`；`_provider_https_url` / `public_origin_is_safe` / `local_admin_auth` 不存在。

- [x] **Step 5: 实现 schema 与 migration（写完不提交）**

`schema.py` 按上表追加三个 `sa.Table` 并加入 `ALL_TABLES`；给 `WEB_SESSIONS` 追加两列。

migration 文件头部：

```python
revision: str = "0010_local_admin_and_provider_state"
down_revision: str | None = "0009_task_parent_context"
```

`upgrade()` 建三表 + `op.add_column("web_sessions", ...)`。既有 `web_sessions` 行在加非空列时会失败，但本项目 migration 只跑在空库或本地库上；为保证幂等，两列用 `server_default` 建好后立即 `op.alter_column(..., server_default=None)`——`auth_source` 默认 `'feishu'`（RI1 时期只有飞书 session），`public_origin_digest` 默认 64 个 `'0'`（哨兵值，认证时必然与真实 origin digest 不匹配，等价于旧 session 全部失效，与设计「切换模式等同于全体登出」一致）。

`downgrade()` 必须走既有 `require_destructive_authorization(op.get_bind(), guarded=(...))` 守卫，参照 `rev_0007_web_sessions.py:65-78` 的写法，把三张新表都列入 `guarded`。

做完这一步**不要 commit、不要 git add**：`web_sessions` 已经多了两个非空列，而 `postgres.py:990`
的 `web_session_to_row()` 仍按旧列集插入，既有 session 测试此刻必红。直接进入 Step 6。

- [x] **Step 6: 实现拆分、认证与 session 写入面**

`web_auth.py` 改动（**这是承重改动，按此顺序做**）：

1. 把现有 `_split_https_url` **重命名**为 `_provider_https_url`，签名与实现一字不改（仍硬性 `scheme == "https"`）；
2. `_authorization_url_is_safe` 改为调用 `_provider_https_url`——它**不接受也不感知** `mode`；
3. 新增独立的 `public_origin_is_safe(value: str, *, mode: WebMode) -> bool`，内部调用 Task 1 的 `canonical_web_public_origin` 并捕获 `ValueError` 返回 `False`；
4. 删除旧 `_public_origin_is_safe`，`WebAuthService.__init__:220` 的 `if not _public_origin_is_safe(public_origin)` 改为 `if not public_origin_is_safe(public_origin, mode=mode)`，并给 `__init__` 增加必填 keyword-only `mode: WebMode`。

Session origin 绑定：`WebAuthService` 内新增 `self._origin_digest = _digest(domain="web-origin:v1", secret=self._public_origin)`；`complete_login` 的 `RotateWebSessionCommand` 带上 `auth_source=IdentitySource.FEISHU` 与 `public_origin_digest=self._origin_digest`；`authenticate` 的 `WebSessionLookup` 带上同一 digest，store 层按 digest 过滤，不匹配即 `WebSessionNotFoundError`。

`local_admin_auth.py`：

```python
_SCRYPT_N: Final[int] = 2**14
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1
_SCRYPT_DKLEN: Final[int] = 32
_ENCODING_VERSION: Final[str] = "1"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return "$".join(
        ["scrypt", _ENCODING_VERSION, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
         base64.b64encode(salt).decode("ascii"), base64.b64encode(dk).decode("ascii")]
    )


def verify_password(password: str, encoded: str) -> bool:
    """只接受本模块固定参数的封装；封装里的 n/r/p 不被信任为计算输入。"""
    try:
        kind, version, n, r, p, salt_b64, dk_b64 = encoded.split("$")
    except ValueError:
        return False
    # 参数是被**核对**的，不是被采纳的：不匹配直接拒绝，避免攻击者用
    # n=2 的封装把校验降成廉价运算，也避免用超大 n 做内存耗尽。
    if (
        kind != "scrypt"
        or version != _ENCODING_VERSION
        or (n, r, p) != (str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P))
    ):
        return False
    try:
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(dk_b64, validate=True)
    except (ValueError, binascii.Error):
        return False
    if len(salt) != 16 or len(expected) != _SCRYPT_DKLEN:
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return hmac.compare_digest(candidate, expected)
```

`LocalAdminAuthService.change_password` 先用 `verify_password` 核对 `current`，再生成新 session
secret，然后把四件事交给**同一个** `LocalAdminStore.change_password_and_rotate_session()`：

```python
async def change_password_and_rotate_session(
    self, *, command: ChangePasswordCommand
) -> LocalAdminRecord:
    async with self._engine.begin() as connection:          # 单个事务
        await connection.execute(
            sa.update(LOCAL_ADMINS)
            .where(LOCAL_ADMINS.c.id == 1)
            .values(
                password_hash=command.password_hash,
                must_change_password=False,
                updated_at=self._clock(),
            )
        )
        await connection.execute(                            # 先撤销全部旧的
            sa.update(WEB_SESSIONS)
            .where(
                WEB_SESSIONS.c.auth_source == IdentitySource.LOCAL_ADMIN.value,
                WEB_SESSIONS.c.revoked_at.is_(None),
            )
            .values(revoked_at=self._clock())
        )
        await connection.execute(                            # 再插入新的
            sa.insert(WEB_SESSIONS).values(...)
        )
    ...
```

顺序不能颠倒：先撤销再插入，才不会把刚签发的新 session 一起撤掉。`LocalAdminStore` 由此成为
唯一同时触及 `local_admins` 与 `web_sessions` 的写入口；`WebSessionStore` 不参与改密路径。

`contracts/enums.py` 的 `IdentitySource` 追加 `LOCAL_ADMIN = "local_admin"`。

同一步里把 session 写入面一起改完，才能让上一步的两个新列真正可写：

- `persistence/web_session.py`：`RotateWebSessionCommand:96` 增加 `auth_source: IdentitySource`、
  `public_origin_digest: Sha256Hex`；`WebSessionLookup:109` 增加 `public_origin_digest: Sha256Hex`。
  两者都是必填，不给默认值——给默认值等于允许调用方漏传而静默写入错误的绑定。
- `persistence/postgres.py:990`：`rotate_session` 用的 `web_session_to_row()` 补上这两列；
  按 `public_origin_digest` 过滤的条件加进 session 查询，digest 不匹配返回 `WebSessionNotFoundError`。
- 所有既有 `RotateWebSessionCommand(...)` 构造点（含 `tests/fakes` 下的 in-memory store）同步补参，
  `python -m pytest tests/ -q` 必须重新全绿——这正是本 Task 不能在 Step 5 断开提交的原因。

- [x] **Step 7: 跑 schema 契约测试与既有一致性测试**

Run: `python -m pytest tests/contract/test_ri5_schema.py tests/contract/test_schema_matches_migration.py tests/contract/test_migration_guard.py -q`

Expected: PASS。

- [x] **Step 8: 加集成测试并对真实 PostgreSQL 跑**

`tests/integration/test_ri5_schema_migration.py` 沿用该目录既有 fixture（`PYTEST_POSTGRES_DSN`），断言：升级到 head 后三张表存在、`local_admins` 插入第二行被 CHECK 拒绝、`provider_test_state` 插入未知 `check_name` 被拒绝、`test_status='passed'` 且 `error_code` 非空被拒绝。

Run: `python -m pytest tests/integration/test_ri5_schema_migration.py -q`

Expected: PASS（无 DSN 时该目录既有 fixture 会 skip，属正常）。

- [x] **Step 9: 跑认证与边界测试确认通过**

Run: `python -m pytest tests/security/test_ri5_origin_split.py tests/unit/test_local_admin_auth.py tests/security/test_local_admin_boundary.py tests/security/test_web_auth_boundary.py -q`

Expected: PASS，且既有 `test_web_auth_boundary.py` 不回归。

- [x] **Step 10: 反证承重（必须做，ADR-014 R2 明文要求）**

把第 2 步的拆分撤掉——让 `_authorization_url_is_safe` 改用 `public_origin_is_safe(..., mode=WebMode.LAN_HTTP)`——确认 `test_provider_authorization_url_is_always_https_only` 变红；恢复后重新全绿。

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/security/test_ri5_origin_split.py -q -p no:cacheprovider
```

- [x] **Step 11: 跑全量后一次性提交（本 Task 唯一一次提交）**

Run: `python -m pytest -q && python -m pytest -m security -q && ruff check . && mypy src`

Expected: 全绿。**只有全绿才提交**——schema 与 session 写入面必须在同一个提交里落地。

提交前先看一遍 `git status`：新增两个必填列会波及既有的 session fake 与共享测试套件（至少 `tests/suites/web_session_store.py`、`src/xiaowei_agent/persistence/fake.py`），这些文件不在下面的清单里但确实被改了，按 `git status` 精确补进 `git add`，不要用 `git add -A`，也不要留下未暂存的改动。

```bash
git add \
  src/xiaowei_agent/persistence/schema.py \
  src/xiaowei_agent/persistence/migrations/versions/rev_0010_local_admin_and_provider_state.py \
  src/xiaowei_agent/persistence/local_admin.py \
  src/xiaowei_agent/persistence/web_session.py \
  src/xiaowei_agent/persistence/postgres.py \
  src/xiaowei_agent/interfaces/local_admin_auth.py \
  src/xiaowei_agent/interfaces/web_auth.py \
  src/xiaowei_agent/contracts/enums.py \
  tests/contract/test_ri5_schema.py \
  tests/integration/test_ri5_schema_migration.py \
  tests/unit/test_local_admin_auth.py \
  tests/security/test_ri5_origin_split.py \
  tests/security/test_local_admin_boundary.py
git commit -m "feat(ri5): add local admin auth, provider state schema and the https gate split"
```

**Task 3 执行记录（2026-09-14）：** 四条基线全绿——`3606 passed, 237 skipped`（Task 2 后 3573）、
`1322 security passed`、`ruff` 通过、`mypy` 169 files 通过。**只有最后一次提交**，schema 与
session 写入面在同一个提交里。五条反证全部按预期变红后恢复：

```text
放宽 _provider_https_url 的协议门     -> test_ri5_origin_split                        3 failed
去掉 get_session 的 origin 过滤       -> 两条 origin 绑定用例                          2 failed
改密时先插入后撤销                    -> test_change_password_is_atomic...             1 failed
认证不检查 auth_source                -> test_a_feishu_session_cannot_authenticate...  1 failed
verify_password 采纳封装里的 n        -> test_encoded_parameters_are_checked_not...    1 failed
恢复后                                                                                47 passed
```

五处与计划文字不同：

1. **digest 域收敛成三个公开函数。** 计划只说给 `WebAuthService` 加 `_origin_digest`，但本地
   管理员登录写的是**同一张** `web_sessions` 表。两条认证路径各自写一遍 `domain="web-session:v1"`
   就会出现两处域字符串，一旦其中一处改动，同一个 cookie 会算出两个 digest，session 在另一条
   路径上直接查不到。因此在 `web_auth.py` 提取 `web_session_digest()` / `web_csrf_token()` /
   `web_origin_digest()`，三个域字符串在全仓库各出现**恰好一次**，`local_admin_auth` 直接复用。

2. **origin 绑定做成查询条件而不是返回后再比。** `WebSessionLookup` 增加必填
   `public_origin_digest`，store 层按它过滤——调用方不可能忘记比对。两个新字段都**不给默认值**：
   给默认值等于允许漏传而静默写入错误的绑定。

3. **`test_schema_matches_migration.py` 的跳过集与补偿检查原本可以各自漂移。** `WEB_SESSIONS`
   被 `rev_0010` 用 ALTER 加列，其 `CREATE TABLE` 是 rev_0007 的历史快照，逐字比对必然不等。
   仓库既有做法是把这类表放进硬编码跳过集（`TASKS`、`TASK_SUBMISSIONS`），但补偿用的
   head 检查只覆盖 `TASKS`——加进跳过集却忘补 head 检查就会静默失去覆盖。改为共用一个
   `_ALTERED_AFTER_CREATION` 元组，head 检查按它参数化，三张表都被覆盖。

4. **`InMemoryLocalAdminStore` 与 session store 共用同一把锁和同一份 `InMemoryPersistenceState`。**
   改密要在一次原子操作里同时改口令和撤销全部旧 session，两边各持一份状态就复现不出这条
   不变量，反证 3 也就钉不住。

5. **闭集守卫登记**（均为刻意设计的人工审查点）：`IdentitySource` 闭集加 `local_admin`；
   `_ALTERED_AFTER_CREATION`、`_ALLOWED_INTERNAL_BY_FILE`（两个新 interface 文件）、
   `_TASK_VIEW_PROCESS_ALLOWED_MODULES`（`persistence.local_admin`）；migration head 字面量
   `0009_task_parent_context` → `0010_local_admin_and_provider_state`（`test_readiness.py`
   与 `test_migration_paths.py` 共 6 处）。这些文件都按 `git status` 精确补进了 `git add`。

`tests/integration/test_ri5_schema_migration.py` 的 8 条在无 `PYTEST_POSTGRES_DSN` 时 skip，
与该目录既有 integration 用例一致（本机基线 237 skipped）。CHECK 行为的真实验证留到有
PostgreSQL 的环境。
---

### Task 4: 运行消费迁移——四个 composition root 改读 JSON

前面几个 Task 建了新真源、记了加载回执，但**没有任何真实消费链改过**：Gemini adapter 仍读
`gemini_model.py:41` 的 `/run/secrets/gemini_api_key`，飞书 adapter 仍从
`Settings.feishu_app_id` / `feishu_app_secret_file` 取值（`web_app.py:777-778`、`web_app.py:788-789`、
`local_stack.py:729-730`、`local_stack.py:734`、`local_stack.py:804-805`）。不做这一步，Task 9
一删旧 secret 挂载，两个 Provider 就全部断供——闭环跑不通。

本 Task 删除旧真源、让四个装配点从 JSON 读取，并按双层开关装配。

**Files:**
- Modify: `src/xiaowei_agent/interfaces/gemini_model.py`（删除 `GEMINI_SECRET_FILE:41`、改 `_build_client:198`、`__all__:328`）
- Modify: `src/xiaowei_agent/interfaces/feishu_oauth.py`（`:229` 签名、`:18` import、`:245` 构造期预读、`:315` 每请求重读）
- Modify: `src/xiaowei_agent/interfaces/feishu_sdk.py`（`:166` `_read_secret_file` 及调用点 `:208`/`:452`/`:568`；`:191`/`:433`/`:539` 三个签名）
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`（`local_stack.py:729-730`、`:734`、`:804-805`）
- Modify: `src/xiaowei_agent/interfaces/web_app.py`（`web_app.py:777-778`、`:788-789`）
- Modify: `src/xiaowei_agent/config.py`（删除 `feishu_app_id:228`、`feishu_app_secret_file:229`，同步 `shared` 元组 `:369` 与 `_FIELD_TO_ENV:517-518`）
- Modify: `.env.example`
- Test: `tests/unit/test_provider_consumption.py`
- Test: `tests/security/test_ri5_no_legacy_secret_path.py`

**Interfaces:**
- Consumes: Task 2 的 `IntegrationConfig`、`read_integration_config` 与 `IntegrationConfigMissingError`；
  Task 1 的 `Settings.gemini_enabled` 等装配开关；Task 3 的 `service_config_state` 表（写加载回执）。
  `read_or_absent` 在本 Task 引入，Task 6 复用
- Produces:
  - `@dataclass(frozen=True, slots=True) class ProviderCredentials`：
    `gemini_api_key: str | None = field(default=None, repr=False)`、
    `feishu_app_id: str | None = None`、
    `feishu_app_secret: str | None = field(default=None, repr=False)`
    ——**刻意不用 `Contract`**：Pydantic 模型默认 `repr` 与 `model_dump()` 会带上字段值，明文 Key
    一旦进异常链、日志或调试输出就泄露。frozen dataclass + `repr=False` 让 `repr()` 只显示
    `app_id`，两个 secret 不出现。`app_id` 不是 secret，保留在 `repr` 里便于排障。
  - `def load_provider_credentials(*, settings: Settings, path: str = DEFAULT_INTEGRATION_CONFIG_PATH) -> tuple[ProviderCredentials, Mapping[tuple[str, str], LoadReceipt]]`
    ——回执的键与 `service_config_state` 的联合主键、以及 Task 6 `compute_display_state` 的
    `receipts` 形状**完全一致**，都是 `(service_name, provider)` 元组；三处不得各用一套。
    ——一次读取，同时产出凭据与本进程要写的加载回执
  - `GeminiModelAdapter.__init__` 新增必填 keyword-only `api_key: str`，**不再自带路径常量**

- [x] **Step 1: 写失败测试——双层开关与断供行为**

`tests/unit/test_provider_consumption.py`：

```python
import pytest
from xiaowei_agent.application.integration_state import LoadReceipt
from xiaowei_agent.interfaces.provider_consumption import load_provider_credentials


def test_json_disabled_provider_yields_no_credential(tmp_path, settings_with_gemini_assembled):
    """.env 装配了，但 JSON 里 enabled=false —— 两层与关系，最终不启用。"""
    path = _write_config(tmp_path, gemini={"enabled": False, "api_key": "k" * 8})
    creds, _ = load_provider_credentials(settings=settings_with_gemini_assembled, path=str(path))
    assert creds.gemini_api_key is None


def test_json_cannot_enable_a_provider_the_env_did_not_assemble(tmp_path, settings_all_disabled):
    """JSON 不能反向启动 Compose 中未装配的进程。"""
    path = _write_config(tmp_path, gemini={"enabled": True, "api_key": "k" * 8})
    creds, receipts = load_provider_credentials(settings=settings_all_disabled, path=str(path))
    assert creds.gemini_api_key is None
    assert receipts == {}  # 未启用的服务不写回执，不会造成永久「待应用」


def test_both_layers_true_yields_the_credential(tmp_path, settings_with_gemini_assembled):
    path = _write_config(tmp_path, gemini={"enabled": True, "api_key": "k" * 8})
    creds, receipts = load_provider_credentials(settings=settings_with_gemini_assembled, path=str(path))
    assert creds.gemini_api_key == "k" * 8
    assert receipts[("worker", "gemini")].status == "loaded"


def test_absent_file_writes_no_receipt_and_does_not_crash(tmp_path, settings_with_gemini_assembled):
    """读不出文件就没有可信 generation，不得伪造一个写进回执。"""
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled, path=str(tmp_path / "missing.json")
    )
    assert creds.gemini_api_key is None
    assert receipts == {}


def test_corrupt_file_writes_no_receipt_and_does_not_crash(tmp_path, settings_with_gemini_assembled):
    bad = tmp_path / "integrations.json"
    bad.write_text("{not json", encoding="utf-8")
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled, path=str(bad)
    )
    assert creds.gemini_api_key is None
    assert receipts == {}


def test_readable_file_with_a_bad_provider_subtree_writes_an_invalid_receipt(
    tmp_path, settings_with_gemini_assembled
):
    """文件本身可读 -> generation 可信 -> 该 Provider 记 invalid。"""
    path = _write_config(tmp_path, generation=7, gemini={"enabled": True})  # 缺 api_key
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled, path=str(path)
    )
    assert creds.gemini_api_key is None
    assert receipts[("worker", "gemini")] == LoadReceipt(generation=7, status="invalid")


def test_gemini_adapter_no_longer_owns_a_path_constant():
    """adapter 只接受注入的 key，不得自己去文件系统找。"""
    from xiaowei_agent.interfaces import gemini_model

    assert not hasattr(gemini_model, "GEMINI_SECRET_FILE")


def test_every_feishu_adapter_takes_an_in_memory_secret():
    """四个 adapter 都不得再收文件路径——否则会把明文 Secret 当路径 open()。"""
    import inspect

    from xiaowei_agent.interfaces.feishu_oauth import FeishuOAuthAdapter
    from xiaowei_agent.interfaces.feishu_sdk import (
        FeishuSdkInboundTransport,
        FeishuSdkMembershipAdapter,
        FeishuSdkMessageAdapter,
    )

    for adapter in (
        FeishuOAuthAdapter,
        FeishuSdkInboundTransport,
        FeishuSdkMessageAdapter,
        FeishuSdkMembershipAdapter,
    ):
        params = inspect.signature(adapter.__init__).parameters
        assert "app_secret" in params, adapter.__name__
        assert "app_secret_file" not in params, adapter.__name__


def test_credentials_repr_hides_the_secrets():
    from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

    gemini = "g" + "-fake-key"
    feishu = "f" + "-fake-secret"
    creds = ProviderCredentials(
        gemini_api_key=gemini, feishu_app_id="cli_x", feishu_app_secret=feishu
    )
    rendered = repr(creds)
    assert gemini not in rendered
    assert feishu not in rendered
    assert "cli_x" in rendered  # app_id 不是 secret，保留便于排障


def test_no_feishu_adapter_reads_the_filesystem_at_construction(monkeypatch):
    """反例：构造期或请求期都不得再 open() 任何文件。"""
    import builtins

    opened: list[str] = []
    real_open = builtins.open
    monkeypatch.setattr(
        builtins, "open", lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1]
    )
    from xiaowei_agent.interfaces.feishu_oauth import FeishuOAuthAdapter

    FeishuOAuthAdapter(app_id="cli_x", app_secret="s" * 8, timeout_seconds=5.0)
    assert opened == []


def test_read_or_absent_returns_none_only_for_a_truly_absent_path(tmp_path):
    from xiaowei_agent.interfaces.provider_consumption import read_or_absent

    assert read_or_absent(str(tmp_path / "integrations.json")) is None


def test_read_or_absent_does_not_swallow_a_broken_symlink(tmp_path):
    """断链必须继续 fail-closed；用 os.path.exists() 预检的实现会在这里返回 None。"""
    import os

    from xiaowei_agent.interfaces.integration_config_file import IntegrationConfigError
    from xiaowei_agent.interfaces.provider_consumption import read_or_absent

    link = tmp_path / "integrations.json"
    link.symlink_to(tmp_path / "nowhere.json")
    assert os.path.exists(link) is False
    with pytest.raises(IntegrationConfigError):
        read_or_absent(str(link))


def test_a_broken_symlink_does_not_produce_an_unconfigured_startup(
    tmp_path, settings_with_gemini_assembled
):
    """同一路径的上层后果：进程不能因为断链就当成「尚未配置」照常起来。"""
    link = tmp_path / "integrations.json"
    link.symlink_to(tmp_path / "nowhere.json")
    creds, receipts = load_provider_credentials(
        settings=settings_with_gemini_assembled, path=str(link)
    )
    assert creds.gemini_api_key is None
    assert receipts == {}  # 读不出可信 generation，不写回执
```

- [x] **Step 2: 写失败测试——旧真源彻底消失**

`tests/security/test_ri5_no_legacy_secret_path.py`（标 `security`）：

```python
import pathlib

import pytest

pytestmark = pytest.mark.security

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def test_no_source_file_references_the_legacy_provider_secret_paths() -> None:
    """只扫 src/：ADR、迁移说明和本测试自身当然会提到旧路径，那是历史记录。

    注意**不能**把 `feishu_app_id` 整体列入禁词——新的 `ProviderCredentials` 自己就有
    `feishu_app_id` 字段，那是迁移后的正当用法。禁的是**旧真源**：Settings 上的同名字段与
    文件路径读取，不是这个名字本身。
    """
    banned = (
        "/run/secrets/gemini_api_key",
        "app_secret_file",            # 任何仍收文件路径的 adapter 签名
        "settings.feishu_app_id",     # 旧 Settings 字段的读取点
        "GEMINI_SECRET_FILE",
    )
    offenders = [
        f"{path.relative_to(_SRC)}:{n}"
        for path in _SRC.rglob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for token in banned
        if token in line
    ]
    assert offenders == [], f"旧 Provider 凭据真源仍被引用：{offenders}"


def test_the_new_credentials_field_is_not_caught_by_the_ban() -> None:
    """反例：确认上一条不会误杀迁移后的正当字段名。"""
    from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

    assert "feishu_app_id" in ProviderCredentials.__dataclass_fields__


def test_settings_no_longer_exposes_the_legacy_feishu_fields() -> None:
    from xiaowei_agent.config import _FIELD_TO_ENV, Settings

    assert "feishu_app_id" not in Settings.model_fields
    assert "feishu_app_secret_file" not in Settings.model_fields
    assert "XIAOWEI_FEISHU_APP_ID" not in _FIELD_TO_ENV.values()
    assert "XIAOWEI_FEISHU_APP_SECRET_FILE" not in _FIELD_TO_ENV.values()
```

- [x] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_provider_consumption.py tests/security/test_ri5_no_legacy_secret_path.py -q`

Expected: FAIL —— `provider_consumption` 不存在；`GEMINI_SECRET_FILE` 仍在；`Settings` 仍有两个飞书字段。

- [x] **Step 4: 迁移四个装配点**

新增 `src/xiaowei_agent/interfaces/provider_consumption.py`，其中一并引入 `read_or_absent()`
（Task 6 的配置 API 会复用它）：

```python
def read_or_absent(path: str) -> IntegrationConfig | None:
    """只有 ENOENT 才算「尚未配置」；符号链接、权限、损坏一律 fail-closed 往上抛。"""
    try:
        return read_integration_config(path)
    except IntegrationConfigMissingError:
        return None
```

**不要**写成 `if not os.path.exists(path): return None`：`exists()` 跟随符号链接，指向不存在目标的
断链会返回 `False`，于是它被当成「尚未配置」，直接绕过 Task 2「符号链接一律故障」的设计，并让
`PUT` 用 `os.replace()` 把这个链接替换成普通文件。`lexists()` 只补上这一例，仍分不出 `EACCES` /
`ELOOP`，也仍有 TOCTOU。判定统一由 Task 2 的加载器在 `os.open` 的 errno 上做（见该 Task Step 4）。

`load_provider_credentials()` 用它读一次文件，按「`.env` 装配开关 AND JSON `enabled`」决定每个 Provider
是否产出凭据；**只为本进程实际启用且需要的 Provider** 考虑写回执（未启用的不写，避免永久
「待应用」）。回执写入分三种情况，**任何一种都不抛异常、不阻止进程启动**：

| 情况 | 凭据 | 回执 |
| --- | --- | --- |
| 文件可读、该 Provider 字段齐备 | 有值 | `(generation=当前, status="loaded")` |
| 文件可读、该 Provider 字段缺失或非法 | `None` | `(generation=当前, status="invalid")` |
| **文件缺失或整体损坏** | `None` | **不写回执** |

第三种是关键：读不出文件就没有可信 generation，而 `LoadReceipt.generation` 恒 `> 0`，
硬凑一个值等于伪造证据。缺回执在状态机里本就等价于「尚未加载当前 generation」，
页面仍会正确显示「待应用」，所以无需为它编一个代次。

四个装配点改法：

| 位置 | 改法 |
| --- | --- |
| `gemini_model.py:41,198,328` | 删除 `GEMINI_SECRET_FILE` 与 `_secret_reader` 默认路径读取；`__init__` 改收必填 `api_key: str`，`_build_client` 直接用它 |
| `local_stack.py:729-730,734` | feishu listener stack 的 `app_id`/`app_secret_file` 改为注入 `credentials.feishu_app_id` / `feishu_app_secret` |
| `local_stack.py:804-805` | channel worker stack 同上 |
| `web_app.py:777-778,788-789` | Web 自己的 OAuth adapter 与成员 adapter 同上；`oauth_available` 为假时不构造这两个 adapter |

**飞书 adapter 共有四个，全部收文件路径，必须一起改**——只改入站那一个，其余三个会把 JSON 里的
明文 Secret 当成路径去 `open()`，必然 `SecretFileError`：

| adapter | 位置 | 现签名 | 改为 |
| --- | --- | --- | --- |
| OAuth | `feishu_oauth.py:229` | `__init__(*, app_id, app_secret_file, timeout_seconds)` | `app_secret: str` |
| 入站长连接 | `feishu_sdk.py:191` | `__init__(*, app_id, app_secret_file)` | `app_secret: str` |
| 消息发送 | `feishu_sdk.py:433` | `__init__(*, app_id, app_secret_file, ...)` | `app_secret: str` |
| 成员查询 | `feishu_sdk.py:539` | `__init__(*, tenant_id, app_id, app_secret_file)` | `app_secret: str` |

同时删除两处路径读取 helper 及其全部调用点：`feishu_sdk.py:166` 的 `_read_secret_file`
（调用点 `:208`、`:452`、`:568`）与 `feishu_oauth.py:18` 对 `read_secret_file` 的 import
（调用点 `:245` 构造期预读、`:315` 每次请求重读）。

注意 `feishu_oauth.py:245` 现在会在**构造期**预读一次做校验，`:315` 每次请求**重读**文件；改成
内存 secret 后这两处都消失——secret 在装配时一次注入，adapter 不再触碰文件系统。这也顺带去掉了
「运行期文件被换掉」这一类不确定性。

`interfaces/secret_file.py` 的 `read_secret_file()` **保留不动**，它继续服务 PostgreSQL 与
StarRocks；本 Task 只是让飞书与 Gemini 不再走它。

`config.py` 删除 `feishu_app_id` 与 `feishu_app_secret_file` 两个字段、`shared` 元组对应项与
`_FIELD_TO_ENV` 两个条目，并同步 `.env.example`（`test_env_example_clean.py` 断言键集合全等）。

- [x] **Step 5: 跑测试确认通过**

Run:

```bash
python -m pytest tests/unit/test_provider_consumption.py tests/security/test_ri5_no_legacy_secret_path.py -q
python -m pytest -q
python -m pytest -m security -q
```

Expected: 全绿。既有飞书/Gemini 测试若因构造参数变化而失败，按新签名调整**测试的构造方式**，
不得为了让测试过而保留旧路径读取。这些被顺带改到的既有测试文件同样要按 `git status` 精确补进
下一步的 `git add`——四个 adapter 换签名必然波及它们的构造点。

- [x] **Step 6: 反证承重**

把「JSON `enabled` 与 `.env` 开关取 AND」改成只看 JSON，确认
`test_json_cannot_enable_a_provider_the_env_did_not_assemble` 变红；把 `read_or_absent` 改回
`if not os.path.exists(path): return None`，确认 `test_read_or_absent_does_not_swallow_a_broken_symlink`
变红；再把它改成 `os.path.lexists()`，确认同一条转绿——这说明测试钉的是行为而不是某一个函数名，
但 `lexists` 版本仍有 TOCTOU，最终实现保持捕获异常的写法。两处都恢复后全绿。

- [x] **Step 7: 提交**

```bash
git add src/xiaowei_agent/interfaces/provider_consumption.py src/xiaowei_agent/interfaces/gemini_model.py src/xiaowei_agent/interfaces/feishu_oauth.py src/xiaowei_agent/interfaces/feishu_sdk.py src/xiaowei_agent/interfaces/local_stack.py src/xiaowei_agent/interfaces/web_app.py src/xiaowei_agent/config.py .env.example docker-compose.smoke.yml tests/unit/test_provider_consumption.py tests/security/test_ri5_no_legacy_secret_path.py
git commit -m "feat(ri5): consume provider credentials from the integration config"
```

**Task 4 执行记录（2026-09-14）：** 四条基线全绿——`3619 passed, 237 skipped`（Task 3 后 3606）、
`1325 security passed`、`ruff` 通过、`mypy` 171 files 通过。反证：

```text
双层与关系改成只看 JSON        -> test_json_cannot_enable_a_provider...          1 failed
read_or_absent 改回 exists()   -> test_read_or_absent_does_not_swallow...        1 failed
同一实现改成 lexists()         -> 同一条                                          1 passed
读不出文件时伪造 generation    -> test_absent_file_writes_no_receipt...          1 failed
恢复后                                                                           17 passed
```

`lexists` 那一条转绿说明测试钉的是**行为**而不是某个函数名；最终实现仍用捕获异常的写法，
因为 `lexists` 版本还留着检查与使用之间的时间窗。

五处与计划文字不同：

1. **三个 SDK adapter 补上了注入 Secret 的构造期校验。** 凭据从文件搬进内存后，
   `_read_secret_file()` 那层边界检查（非空、单行、无控制字符、≤4096、可编码）跟着消失了——
   计划只说改签名，但照做会留下一条绕过路径：空串或带控制字符的取值会一路传到 SDK 才出问题，
   而那时异常里可能已经带上供应商侧的上下文。新增 `_validated_secret()`，三个 adapter 都过，
   并补 `test_adapters_reject_an_unusable_injected_secret` 逐个钉住（漏一个就红）。
   `FeishuOAuthAdapter` 原本就有构造期检查，同步改成校验取值。

2. **五条既有用例测的是已经不存在的 seam，按承重属性重写而不是删掉。**
   `test_each_exchange_rereads_secret_*` 的"重读文件"那一半没有了（轮换凭据的方式变成改 JSON
   后重启，这正是 generation/restart 模型本身），"不复用 app_access_token"那一半保留；
   `test_constructor_eagerly_rejects_unsafe_secret_and_exchange_rereads_it` 拆成构造期拒绝不可用
   取值；`test_constructor_maps_unrepresentable_secret_path_*` 改测不可编码的**取值**；
   `test_secret_reread_is_inside_total_deadline_*` 与
   `test_transport_rejects_unsafe_secret_files_before_loading_sdk` 删除并在原位留下说明——前者
   的预算属性已由 `test_both_posts_share_one_total_deadline` 覆盖，后者的五种文件形态对不再读
   文件的 transport 完全不起作用，留着就是一条永远绿的空跑用例。
   `test_transport_error_and_adapter_repr_never_expose_inputs` 反而更承重了：Secret 现在常驻内存。

3. **Gemini 的三条 reader 用例合并为取值校验 + 顺序校验。** `secret_reader` 不存在了，
   "reader 抛 SecretFileError / ValueError / BaseException"三条都是那个 seam 的。承重属性不变：
   任何不满足 `_validate_credential` 的取值都不得走到 client 构造，且拒绝路径不回填原文
   （新增 `test_the_rejected_key_never_appears_in_the_error`）。代理检查那条改为断言更强的
   顺序性质：合法 Key 与非法 Key 都必须报 `AMBIENT_PROXY`，而不是数"读了几次文件"。

4. **装配点按"缺凭据就不装配"处理，而不是构造一个注定失败的 adapter。** 断供是"这条链路
   不存在"，不是"每次调用都报错"。`_resolved_credentials()` 在注入优先的前提下从默认路径读一次，
   读失败不抛——否则一份坏配置会把已经在跑的服务一起拖死。`build_in_memory_local_stack`
   **不读文件**，凭据只能显式注入（离线证明用）。

5. **`docker-compose.smoke.yml` 与 README 同步删掉两个已不存在的 `XIAOWEI_*`。**
   `load_settings` 对未知 `XIAOWEI_*` 是 fail-fast 的，留着会让容器直接起不来。补了一条交叉
   验证：所有 `docker-compose*.yml` 里出现的 `XIAOWEI_*` 都必须在 `_FIELD_TO_ENV` 里（当前为空）。

比计划清单多改到的既有测试共 13 个文件，均已按 `git status` 精确补进 `git add`。

**Task 2/4 补修（2026-09-14，复审发现的 P1）：** `IntegrationConfig` 的两个 secret 字段只写了
`exclude=True`，`repr()` 与 `str()` 仍会打印完整的 Gemini Key 与飞书 App Secret。

根因不是漏了一个参数，而是我在 Task 4 的记录里已经写明「Pydantic 默认 `repr` 与 `model_dump()`
都会带上字段值」，却只给 `ProviderCredentials` 加了 `repr=False`，Task 2 的 `Contract` 字段只做了
一半。当时的用例断言的是 `repr(dumped)`——已经 dump 过的字典——看起来在测 `repr`，实际上
只写 `exclude=True` 的实现同样全绿（反证 1 复现了这一点：修复前的写法下那条用例仍 `1 passed`）。

沿同一路径扫描，`persistence/local_admin.py` 的两个 `password_hash` 是同类：`LocalAdminRecord`
两条通道都漏（连 `model_dump()` 都带），`ChangePasswordCommand` 漏 `repr`。哈希不是明文口令，
但它是离线爆破的输入，同样不能进日志或响应体。四处一起修。

**按字段名做守卫不成立**：实测名字模式会误杀 `idempotency_key` / `tenant_key` / `reason_key`，
又漏掉 `password_hash`。义务改挂在**类型**上——凡标注为 `Secret*` 的字段必须 `exclude=True`
且 `repr=False`，由 `tests/security/test_secret_field_exposure.py` 用 AST 机械核对，并带一条
反空跑用例（扫不到已知字段就变红）。新增 `SecretHash` 标记类型给两个 `password_hash`。

```text
去掉 repr=False（修复前写法）     -> repr 用例 + 结构守卫        3 failed
  同一状态下旧的 model_dump 用例 -> 仍然                        1 passed  ← 当初的假绿
去掉 exclude=True                -> 结构守卫                    2 failed
LocalAdminRecord 去掉两个标志    -> 口令哈希用例 + 结构守卫      3 failed
把 SecretHash 改回 StrictStr     -> 反空跑用例                  1 failed
恢复后                                                          20 passed
```

四条基线：`3626 passed, 237 skipped`、`1331 security passed`、`ruff` 通过、`mypy` 171 files 通过。

---

### Task 5: Web 装配解耦、模式化 Cookie 与登录路由

让 Web 在没有飞书配置时也能起来，Cookie 名按模式切换，并把登录/改密/退出接到路由。

**Files:**
- Modify: `src/xiaowei_agent/interfaces/local_stack.py`（`build_postgres_web_stack:835` 的两个端口参数改可选，其 `if not (settings.web_app_enabled and settings.feishu_oauth_enabled)` 于 `local_stack.py:855`；`identity_directory` 装配于 `:889`；`WebAuthService` 装配于 `local_stack.py:904`）
- Modify: `src/xiaowei_agent/interfaces/web_app.py`（`SESSION_COOKIE_NAME:69`、`OAUTH_STATE_COOKIE_NAME:70`、`_set_secret_cookie:399`、`_clear_secret_cookie:417`、`_WebRequestBoundaryMiddleware:260`、`readyz:741`、`serve_web:759` 的开关与 adapter 构造）
- Test: `tests/contract/test_web_app_routes.py`
- Test: `tests/unit/test_local_stack.py`
- Test: `tests/security/test_ri5_web_assembly.py`

**Interfaces:**
- Consumes: Task 3 的 `LocalAdminAuthService`、`public_origin_is_safe`；Task 4 的
  `ProviderCredentials` / `load_provider_credentials`（**只在 `serve_web` 里用**，见下方职责划分；
  因此 **Task 4 必须先于本 Task 完成**）；Task 1 的 `Settings.web_mode` 与 `Settings.web_public_origin`
- Produces:
  - `def session_cookie_name(mode: WebMode) -> str`——HTTPS 返回 `"__Host-xiaowei-session"`，`lan_http` 返回 `"xiaowei-session"`
  - `def oauth_state_cookie_name(mode: WebMode) -> str`——同理 `"__Host-xiaowei-oauth-state"` / `"xiaowei-oauth-state"`
  - `WebStack.local_admin_auth: LocalAdminAuthService`（必填）
  - `WebStack.oauth_available: bool`——飞书 OAuth 是否装配成功
  - **改为 Optional 的四个 `WebStack` 字段**（`local_stack.py:189-193`），其余字段不动：

    ```text
    auth: WebAuthService | None = None            # 飞书 OAuth 登录服务
    oauth_port: FeishuOAuthPort | None = None
    membership: FeishuMembershipPort | None = None
    identity_directory: FeishuIdentityDirectory | None = None
    ```

  - `TaskAccessService.__init__` 的 `membership` 改为 `FeishuMembershipPort | None`
    （`channel_access.py:125`）；为 `None` 时，任何**需要群成员校验**的读取路径一律
    **fail-closed 拒绝**（返回既有的 not-found 语义，不降级为放行）。本地管理员只看自己的任务
    与 admin 全量安全任务，不依赖群成员校验，因此该 fail-closed 不影响 RI5 闭环。
  - `create_app()` 最终签名（`web_app.py:540`）：

    ```python
    def create_app(
        *,
        auth: WebAuthService | None,            # 飞书 OAuth；不可用时为 None
        local_admin_auth: LocalAdminAuthService,  # 必填，本地登录始终可用
        oauth_available: bool,
        settings: Settings,
        readiness: ReadinessProbe,
        task_access: TaskAccessService,
        submissions: ChannelSubmissionService,
        clock: Clock,
        policy_revision: str,
    ) -> FastAPI: ...
    ```

    `auth is None` 时**不注册** `/oauth/feishu/start` 与 `/oauth/feishu/callback` 两条路由——
    不是注册后再返回 503，避免出现「路由在但永远失败」的假入口（M7 §2.5 禁止假入口）。
  - `def validate_unauthenticated_origin(*, origin: str | None, content_type: str | None) -> None`
    ——未登录状态变更的窄校验：只核对固定 public origin 与 `application/json`，**不**要求 CSRF token
  - 新路由：`POST /app/api/login`、`POST /app/api/change-password`（既有 `POST /app/api/logout` 复用）
  - `GET /app` 在未登录时返回登录壳（不再 401/302）

- [ ] **Step 1: 写失败测试**

`tests/security/test_ri5_web_assembly.py`（标 `security`）：

```python
async def test_web_starts_without_any_feishu_configuration():
    # Given web_app_enabled=true，且 oauth=None、membership=None
    # Then  build_postgres_web_stack 成功，stack.oauth_available is False，
    #       stack.auth / oauth_port / membership / identity_directory 均为 None
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_the_stack_builder_never_reads_the_integration_config():
    # 职责分界的承重断言：装配函数只收端口，不读文件。
    # Given monkeypatch 掉 read_integration_config / load_provider_credentials，
    #       任何一次调用都记一笔
    # When  build_postgres_web_stack(settings=..., oauth=None, membership=None)
    # Then  调用记录为空；把读配置挪回装配函数的实现会在这里变红
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_half_a_port_pair_does_not_assemble_feishu():
    # Given 只传 oauth 不传 membership（反过来同理）
    # When  build_postgres_web_stack
    # Then  oauth_available is False，且不构造 WebAuthService——
    #       半套端口不得走进「已装配」分支
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_serve_web_builds_no_feishu_adapter_when_the_switch_is_off():
    # Given feishu_oauth_enabled=false，但 integrations.json 里飞书字段齐备
    # When  serve_web 装配
    # Then  FeishuOAuthAdapter / FeishuSdkMembershipAdapter 一次都没被构造，
    #       传给 build_postgres_web_stack 的两个端口都是 None
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_serve_web_survives_missing_feishu_credentials():
    # Given feishu_oauth_enabled=true，但 JSON 里 app_secret 缺失（另一例：只有 id 没有 secret）
    # When  serve_web 装配
    # Then  不抛 _WebConfigurationError，进程照常起来，oauth_available 为 False
    raise NotImplementedError("按规格写出断言后删除本行")

async def test_feishu_oauth_assembly_failure_does_not_kill_the_process():
    # 身份目录加载失败 -> oauth_available False，但 stack 仍可用、readyz 仍 ready
    raise NotImplementedError("按规格写出断言后删除本行")

async def test_gemini_never_participates_in_web_readiness():
    # 无论 gemini 配置如何，readyz 只看 database/migration head/assembled
    raise NotImplementedError("按规格写出断言后删除本行")

async def test_local_admin_seed_failure_makes_the_web_not_ready():
    # seed 抛错 -> composition 不成立 -> readyz 非 200
    raise NotImplementedError("按规格写出断言后删除本行")

def test_cookie_names_and_flags_follow_the_web_mode():
    # https -> __Host- 前缀且 secure=True 且设置 HSTS
    # lan_http -> 普通名、secure 缺省、不设 HSTS；两种都 HttpOnly/SameSite=Lax/Path=/
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_oauth_routes_are_absent_when_feishu_is_unavailable():
    # Given feishu_oauth_enabled=false（另一例：身份目录加载抛异常）
    # When  请求 /oauth/feishu/start 与 /oauth/feishu/callback
    # Then  两条都 404（不是 503）——不留「路由在但永远失败」的假入口
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_membership_none_fails_closed_for_group_scoped_reads():
    # Given TaskAccessService 的 membership 为 None
    # When  读取一个需要群成员校验的任务
    # Then  按既有 not-found 语义拒绝；断言**没有**降级为放行
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_local_admin_paths_work_with_membership_none():
    # Given membership 为 None
    # When  本地管理员读「我的任务」与 admin 全量安全任务
    # Then  正常返回——该 fail-closed 不得波及 RI5 闭环
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_bad_origin_does_not_take_down_the_whole_web():
    # Given 两个端口都传入，但 WebAuthService 构造因 origin 非法抛 ValueError
    # Then  build_postgres_web_stack 仍成功，oauth_available 为 False，readyz 仍 ready
    raise NotImplementedError("按规格写出断言后删除本行")

async def test_before_first_change_password_other_routes_are_refused():
    # must_change_password=true 时 GET /app/api/config -> 403 闭集码 password_change_required
    raise NotImplementedError("按规格写出断言后删除本行")

async def test_first_login_succeeds_without_any_csrf_token() -> None:
    # Given 干净数据库（seed 出 admin/admin），浏览器无任何 cookie
    # When  POST /app/api/login，带正确 Origin 与 application/json，不带 CSRF token
    # Then  200，且响应 Set-Cookie 含 session；这条是首启闭环的入口，必须能过
    raise NotImplementedError("按规格写出断言后删除本行")

async def test_login_still_requires_the_exact_origin_and_json_content_type() -> None:
    # Given 无 cookie
    # When  Origin 为 http://evil.example（另一例：Content-Type 为 text/plain）
    # Then  403，且不签发任何 session
    raise NotImplementedError("按规格写出断言后删除本行")

async def test_change_password_still_requires_session_csrf() -> None:
    # Given 已登录且 must_change_password=true
    # When  POST /app/api/change-password 不带 CSRF token
    # Then  403，口令未改变
    raise NotImplementedError("按规格写出断言后删除本行")

async def test_unauthenticated_app_shell_is_a_login_page() -> None:
    # Given 无 cookie
    # When  GET /app
    # Then  200，正文含口令输入框；不含任务列表、配置面板；
    #       oauth_available 为假时不含飞书 OAuth 入口
    raise NotImplementedError("按规格写出断言后删除本行")


async def test_every_new_json_write_route_enforces_the_body_limit() -> None:
    # Given 一个超过 settings.api_request_body_limit_bytes 的 JSON body
    # When  依次 POST/PUT login、change-password、config、config/clear、
    #       config/test/{gemini_connection,feishu_credentials,feishu_oauth}
    # Then  每一条都被中间件拒绝（413 或既有闭集码），且处理函数从未被调用
    # 反例：把某条路径从 paths 白名单里去掉，这条必须变红
    raise NotImplementedError("按规格写出断言后删除本行")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/security/test_ri5_web_assembly.py -q`

Expected: FAIL —— `build_postgres_web_stack` 仍在 `local_stack.py:855` 抛 `ValueError("Web app is disabled")`。

- [ ] **Step 3: 实现装配解耦**

**先划清两层的职责，不要把读配置塞进装配函数。** `build_postgres_web_stack` 现在的
docstring 就是「用注入端口装配 Web 窄栈；不创建 Runner、Gateway 或真实 OAuth 客户端」
（`local_stack.py:842`），它从来不读文件、不构造真实飞书 client。本 Task 不改这条职责，
只把两个端口参数改成可选：

| 层 | 位置 | 本 Task 的职责 |
| --- | --- | --- |
| composition root | `web_app.py:759 serve_web` | 读 JSON、判双层开关、构造或不构造真实 adapter |
| 装配函数 | `local_stack.py:835 build_postgres_web_stack` | 只收端口；端口为 `None` 就不装配飞书那一组 |

`local_stack.py` 改动：

```python
async def build_postgres_web_stack(
    *,
    settings: Settings,
    oauth: FeishuOAuthPort | None = None,        # 原为必填
    membership: FeishuMembershipPort | None = None,  # 原为必填
    clock: Clock = _utc_now,
) -> WebStack:
    ...
```

1. `local_stack.py:855` 改为 `if not settings.web_app_enabled: raise ValueError("Web app is disabled")`；
2. 飞书那一组（`identity_directory` `:889` 与 `WebAuthService` `:904`）的判定条件是
   **`oauth is not None and membership is not None`**——不看 `settings.feishu_oauth_enabled`，
   不读 `ProviderCredentials`。谁传端口谁负责判断，这里只反映传进来的事实：

```python
auth: WebAuthService | None = None
identity_directory: FeishuIdentityDirectory | None = None
oauth_available = False

if oauth is not None and membership is not None:
    try:
        identity_directory = load_feishu_identity_directory(
            path=cast(str, settings.feishu_identity_file),
            tenant_id=settings.tenant_id,
            environment_id=settings.environment_id,
        )
        auth = WebAuthService(
            sessions=web_session_store,
            identities=identity_directory,
            oauth=oauth,
            public_origin=cast(str, settings.web_public_origin),
            mode=settings.web_mode,
            oauth_state_ttl_seconds=settings.web_oauth_state_ttl_seconds,
            session_ttl_seconds=settings.web_session_ttl_seconds,
            oauth_timeout_seconds=FEISHU_OAUTH_SERVICE_TIMEOUT_SECONDS,
        )
        oauth_available = True
    except (FeishuIdentityConfigurationError, ValueError):
        # 只标记不可用；不 dispose engine，不抛出，Web 进程照常起来
        auth = None
        identity_directory = None
        oauth_available = False
```

注意捕获集合要含 `ValueError`——`WebAuthService.__init__` 对 origin/ttl 非法就是抛它，不能让它
把整个 Web 打挂。`WebStack` 的 `oauth_port` / `membership` 直接回填传入值（可能是 `None`）；
`oauth_available` 恒等于 `auth is not None`，不要出现第二个真源。

`web_app.py:759 serve_web` 改动（Task 4 已把这里从读路径改成读 `ProviderCredentials`，本 Task
只把「读不出就报错」改成「读不出就不装配」）：

```python
if not settings.web_app_enabled:          # :761 原为 and feishu_oauth_enabled
    raise _WebConfigurationError
...
credentials, _ = load_provider_credentials(settings=settings)  # 回执见下
oauth: FeishuOAuthPort | None = None
membership: FeishuMembershipPort | None = None
if (
    settings.feishu_oauth_enabled
    and credentials.feishu_app_id is not None
    and credentials.feishu_app_secret is not None
):
    try:
        oauth = FeishuOAuthAdapter(
            app_id=credentials.feishu_app_id,
            app_secret=credentials.feishu_app_secret,
            timeout_seconds=FEISHU_OAUTH_PROVIDER_TIMEOUT_SECONDS,
        )
        membership = FeishuSdkMembershipAdapter(
            tenant_id=settings.tenant_id,
            app_id=credentials.feishu_app_id,
            app_secret=credentials.feishu_app_secret,
        )
    except ValueError:
        oauth = None          # 两个必须同进同退，否则 stack 会收到半套端口
        membership = None
stack = await build_postgres_web_stack(
    settings=settings, oauth=oauth, membership=membership
)
```

第二个返回值在本 Task 先丢弃；Task 6 会把它接到 `ProviderStateStore.record_load`，届时 `_` 改成 `receipts` 并落库，本 Task 不提前引入未使用变量（`ruff` 会报 `F841`）。

`:773-783` 原来的 `credential_invalid` → `raise _WebConfigurationError` 那一段**必须删掉**：
飞书凭据不可用是本 Task 要支持的正常状态，不能再打死 Web 进程。`app_id` 与 `app_secret`
要一起判断——只有 secret 没有 id 同样构造不出 adapter。

本地管理员那一组是**必填**：store 构造与 seed 失败仍走既有 `except Exception: await
engine.dispose(); raise`，使 composition 不成立、Web 不进入 ready（这正是设计要求的
「seed 失败时 Web 不 ready」）。

`TaskAccessService` 用 `membership=membership` 构造（现在它本来就可能是 `None`）；`create_app()` 按上面的最终签名调用。

本地管理员 seed 在 `build_postgres_web_stack` 内、migration-head 检查之后执行：`await local_admin_store.seed_if_absent(password_hash=hash_password("admin"))`。seed 抛错则按既有 `except Exception: await engine.dispose(); raise` 路径走，使 composition 不成立。

`web_app.py` 改动：把两个 `Final` cookie 常量替换为上述两个按模式取名的函数；`_set_secret_cookie`/`_clear_secret_cookie` 增加 keyword-only `secure: bool` 参数由调用方按模式传入；HSTS 响应头只在 `WebMode.HTTPS` 下设置。登录/退出路径必须**同时清理两个已知 cookie 名**，避免切模式后残留。

**登录必须能在没有 session 的情况下完成。** 现有 `web_auth.py:386` 的 `validate_state_change()`
用 `_digest(domain="csrf:v1", secret=session_cookie)` 派生 CSRF token——未登录用户拿不到 session
cookie，因此也不可能提供合法 token。若让 `POST /app/api/login` 复用它，首次登录**必然 403**。

因此分成两档，且只有登录这一条走窄档：

| 路由 | Origin | Content-Type | Session CSRF |
| --- | --- | --- | --- |
| `POST /app/api/login` | 必须精确等于固定 public origin | 必须 `application/json` | **不要求** |
| `POST /app/api/change-password` | 必须 | 必须 | **要求** |
| `POST /app/api/logout` | 必须 | 必须 | **要求** |
| `PUT/POST /app/api/config*` | 必须 | 必须 | **要求** |

新增 `validate_unauthenticated_origin()`，只做前两项校验：

```python
def validate_unauthenticated_origin(
    self, *, origin: str | None, content_type: str | None
) -> None:
    """未登录状态变更的窄校验；不派生也不检查 CSRF token。"""
    if not _constant_time_ascii_equal(origin, self._public_origin):
        raise WebOriginError
    if content_type is None or content_type.split(";")[0].strip() != "application/json":
        raise WebCsrfError
```

`Origin` 头精确匹配固定 public origin 已足以挡住跨站表单提交（跨站请求无法伪造 `Origin`），
而 `SameSite=Lax` 的 cookie 在此刻还不存在，所以登录这一步没有可被 CSRF 滥用的既有权限。

**所有新增写入口都必须进 body limit 白名单。** `web_app.py:562-566` 的
`JsonBodyLimitMiddleware` 现在只覆盖 `{"/app/api/logout", "/app/api/tasks"}`；新增的 login、
change-password、config、config/clear、config/test 全是 JSON 写入口，不加进去就没有大小上限。
`paths` 改为：

```python
paths=frozenset({
    "/app/api/logout",
    "/app/api/tasks",
    "/app/api/login",
    "/app/api/change-password",
    "/app/api/config",
    "/app/api/config/clear",
    "/app/api/config/test",     # 前缀匹配三个 check_name 子路径
}),
```

若 `JsonBodyLimitMiddleware` 只支持精确匹配，把三个 `config/test/{check_name}` 逐条列全，
不要为此改中间件语义。

**`GET /app` 未登录时必须返回登录壳**，不能 401 或跳转 OAuth——否则本地管理员没有任何入口能拿到
表单去登录。登录壳只含口令输入、提交脚本与 `Origin` 所需的同源资源，不加载任务列表、配置面板或
OAuth 入口；飞书 OAuth 入口只有在 `oauth_available` 为真时才渲染。

`must_change_password=true` 时，除 login / change-password / logout / `/healthz` / `/readyz` 外的
所有 `/app/api/*` 一律返回 403 闭集码 `password_change_required`；`GET /app` 返回改密壳。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/security/test_ri5_web_assembly.py tests/contract/test_web_app_routes.py tests/unit/test_local_stack.py -q`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/xiaowei_agent/interfaces/local_stack.py src/xiaowei_agent/interfaces/web_app.py tests/security/test_ri5_web_assembly.py tests/contract/test_web_app_routes.py tests/unit/test_local_stack.py
git commit -m "feat(ri5): decouple web assembly from feishu and add local admin routes"
```

---

### Task 6: 配置 API、加载回执与页面状态计算

**Files:**
- Create: `src/xiaowei_agent/application/integration_state.py`
- Create: `src/xiaowei_agent/persistence/provider_state.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `src/xiaowei_agent/interfaces/worker.py`、`src/xiaowei_agent/interfaces/feishu_listener.py`、`src/xiaowei_agent/interfaces/feishu_worker.py`（启动时写加载回执）
- Test: `tests/unit/test_integration_state.py`
- Test: `tests/contract/test_ri5_config_api.py`

**Interfaces:**
- Consumes: Task 2 的 `IntegrationConfig` / `write_integration_config`；Task 3 的三张表与 `LOCAL_ADMIN`；Task 4 的 `read_or_absent` 与 `LoadReceipt`
- Produces:
  - `class ProviderDisplayState(StrEnum)`：**恰好五个成员**，与已接受设计
    [RI5 设计 §页面状态](../../plans/RI5-local-web-admin-simplified-design.md) 一一对应，不得增减：
    `UNCONFIGURED="unconfigured"`、`PENDING_RESTART="pending_restart"`、
    `PENDING_TEST="pending_test"`、`AVAILABLE="available"`、`TEST_FAILED="test_failed"`
  - 完整签名（调用方必须显式告知「该等哪些服务」，否则无法判断某个已启用服务缺回执）：

    ```python
    def compute_display_state(
        *,
        provider: ProviderName,
        enabled: bool,
        required_fields_present: bool,
        current_generation: int,
        required_service_names: frozenset[str],
        receipts: Mapping[tuple[str, str], LoadReceipt],
        test: TestResult | None,
    ) -> ProviderDisplayState: ...
    ```

    - `required_service_names`：本次部署中**实际启用且需要该 Provider** 的服务名闭集，由调用方
      从 `Settings` 的装配开关算出（例如 Gemini 在只开 worker 时是 `frozenset({"worker"})`）。
      为空集表示没有服务需要它——此时不存在「待应用」，直接进入测试判定，避免永久卡在待应用。
    - `receipts`：`Mapping[(service_name, provider), LoadReceipt]`。判定「已加载」要求
      `required_service_names` 中**每一个**服务都有 `status == "loaded"` 且
      `generation == current_generation` 的回执。**缺回执**与**任一 `invalid`** 都算尚未加载，
      统一落到 `PENDING_RESTART`——「服务尚未加载当前 generation」本就涵盖「读了但没读成」。
      `load_status` 保留在表里供页面显示原因提示，但**不产生第六个状态**。
    - `test: TestResult | None`，`class TestResult(Contract): status: Literal["passed","failed"]; generation: StrictInt`
  - `class LoadReceipt(Contract): generation: StrictInt = Field(gt=0); status: Literal["loaded", "invalid"]`
    ——`generation` 恒 `> 0`，因此**只有在服务确实读到了一个可信 generation 时才会存在回执**。
    文件缺失或整体损坏时读不出 generation，此时**不写任何回执**（见 Task 4），缺回执自然等价于
    「尚未加载当前 generation」。这样 `generation > 0` 的约束与「配置缺失也要有反馈」不再冲突。
  - `UNCONFIGURED_GENERATION: Final[int] = 0`——文件不存在时的逻辑代次
  - （`read_or_absent` 由 Task 4 引入，本 Task 直接复用，不重复定义）
  - `class ProviderStateStore(Protocol)`：`record_load(...)`、`record_test(...)`、`snapshot() -> ProviderStateSnapshot`
  - 路由：`GET /app/api/config`、`PUT /app/api/config`、`POST /app/api/config/clear`

- [ ] **Step 1: 写失败测试——状态机（纯函数，先钉死顺序）**

`tests/unit/test_integration_state.py`：

```python
import pytest
from xiaowei_agent.application.integration_state import (
    ProviderDisplayState as S,
    compute_display_state,
)


from xiaowei_agent.application.integration_state import LoadReceipt, TestResult
from xiaowei_agent.contracts import ProviderName


def _loaded(generation: int = 2) -> LoadReceipt:
    return LoadReceipt(generation=generation, status="loaded")


def _state(**kw: object) -> S:
    base = dict(
        provider=ProviderName.GEMINI,
        enabled=True,
        required_fields_present=True,
        current_generation=2,
        required_service_names=frozenset({"worker"}),
        receipts={("worker", "gemini"): _loaded()},
        test=None,
    )
    base.update(kw)
    return compute_display_state(**base)  # type: ignore[arg-type]


def test_disabled_or_missing_config_is_unconfigured() -> None:
    assert _state(enabled=False) is S.UNCONFIGURED
    assert _state(required_fields_present=False) is S.UNCONFIGURED


def test_an_invalid_load_receipt_is_not_pending_test() -> None:
    """读到了当前代次但判定无效 —— 仍是「尚未加载」，绝不能冒充「待测试」。"""
    receipts = {("worker", "gemini"): LoadReceipt(generation=2, status="invalid")}
    assert _state(receipts=receipts) is S.PENDING_RESTART


def test_a_stale_invalid_receipt_is_also_pending_restart() -> None:
    receipts = {("worker", "gemini"): LoadReceipt(generation=1, status="invalid")}
    assert _state(receipts=receipts) is S.PENDING_RESTART


def test_the_state_enum_has_exactly_the_five_designed_members() -> None:
    """反例：任何人想加第六态，这条先红。已接受设计只承诺五态。"""
    assert [m.value for m in S] == [
        "unconfigured", "pending_restart", "pending_test", "available", "test_failed",
    ]


def test_a_required_service_without_any_receipt_is_pending_restart() -> None:
    """两个服务都需要飞书时，只有一个上报回执不算生效。"""
    assert (
        _state(
            required_service_names=frozenset({"worker", "feishu-listener"}),
            receipts={("worker", "gemini"): _loaded()},
        )
        is S.PENDING_RESTART
    )


def test_every_required_service_must_report_the_current_generation() -> None:
    assert (
        _state(
            required_service_names=frozenset({"worker", "web"}),
            receipts={("worker", "gemini"): _loaded(), ("web", "gemini"): _loaded()},
        )
        is S.PENDING_TEST
    )


def test_no_required_service_means_no_pending_restart() -> None:
    """没有服务需要它时不得永久停在待应用。"""
    assert _state(required_service_names=frozenset(), receipts={}) is S.PENDING_TEST


def test_receipts_from_unrelated_services_are_ignored() -> None:
    assert (
        _state(receipts={("api", "gemini"): _loaded(), ("worker", "gemini"): _loaded()})
        is S.PENDING_TEST
    )


def test_service_has_not_loaded_the_current_generation_is_pending_restart() -> None:
    assert _state(receipts={("worker", "gemini"): _loaded(1)}) is S.PENDING_RESTART
    assert _state(receipts={}) is S.PENDING_RESTART


def test_loaded_but_untested_is_pending_test() -> None:
    assert _state() is S.PENDING_TEST


def test_stale_test_result_does_not_count_as_tested() -> None:
    assert _state(test=TestResult(status="passed", generation=1)) is S.PENDING_TEST


def test_current_generation_results_decide_available_or_failed() -> None:
    assert _state(test=TestResult(status="passed", generation=2)) is S.AVAILABLE
    assert _state(test=TestResult(status="failed", generation=2)) is S.TEST_FAILED


def test_unconfigured_wins_over_every_later_rule() -> None:
    """顺序断言：1 优先于其余全部。"""
    assert (
        _state(
            enabled=False, receipts={}, test=TestResult(status="failed", generation=2)
        )
        is S.UNCONFIGURED
    )
```

- [ ] **Step 2: 写失败测试——配置 API 边界**

`tests/contract/test_ri5_config_api.py`：

```python
def test_get_config_never_returns_secret_values():
    # 响应里只有 enabled / configured(bool) / app_id / generation / 各项状态
    raise NotImplementedError("按规格写出断言后删除本行")

def test_put_without_a_secret_field_keeps_the_existing_value():
    # 先写入 secret，再 PUT 一个不含该字段的 body；重新读文件，secret 原值不变
    raise NotImplementedError("按规格写出断言后删除本行")

def test_put_with_a_new_secret_replaces_it_and_bumps_generation():
    # generation: n -> n+1
    raise NotImplementedError("按规格写出断言后删除本行")

def test_empty_string_never_clears_a_secret():
    # 必须用 POST /app/api/config/clear
    raise NotImplementedError("按规格写出断言后删除本行")

def test_clear_action_removes_the_secret_and_bumps_generation():
    # POST /app/api/config/clear 后该 secret 不存在，generation 递增
    raise NotImplementedError("按规格写出断言后删除本行")

def test_invalid_payload_does_not_replace_the_current_file():
    # 校验失败时文件内容与 mtime 不变
    raise NotImplementedError("按规格写出断言后删除本行")

def test_config_routes_reject_a_feishu_principal_with_admin_permission():
    # GET / PUT / clear / test 四条路由都只接受 IdentitySource.LOCAL_ADMIN
    raise NotImplementedError("按规格写出断言后删除本行")

def test_put_response_states_restart_required():
    # 成功响应含 {"generation": n+1, "restart_required": true}
    raise NotImplementedError("按规格写出断言后删除本行")

def test_model_and_endpoint_are_read_only():
    # 请求体带 model/endpoint/api_version -> 422，固定常量不可编辑
    raise NotImplementedError("按规格写出断言后删除本行")

def test_first_save_on_a_clean_deployment_creates_generation_1() -> None:
    # Given .config 目录存在但 integrations.json 不存在
    # When  GET /app/api/config
    # Then  200，两个 Provider 都是 unconfigured，generation 为 0
    # When  PUT 一份带 gemini.api_key 的合法 body
    # Then  文件被创建，generation == 1，restart_required 为 true
    raise NotImplementedError("按规格写出断言后删除本行")

def test_a_corrupt_file_is_not_treated_as_absent() -> None:
    # Given integrations.json 存在但 JSON 非法（另一例：是符号链接）
    # When  GET /app/api/config
    # Then  返回闭集故障码而不是 unconfigured；PUT 被拒绝且不替换该文件
    raise NotImplementedError("按规格写出断言后删除本行")
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/unit/test_integration_state.py tests/contract/test_ri5_config_api.py -q`

Expected: FAIL —— 模块与路由不存在。

- [ ] **Step 4: 实现**

`compute_display_state` 严格按设计的五步顺序短路返回，不做任何合并或猜测。

`ProviderStateStore` 的 `record_load` 用 `INSERT ... ON CONFLICT (service_name, provider) DO UPDATE`，`record_test` 用 `INSERT ... ON CONFLICT (check_name) DO UPDATE`。

Web 进程这一路：把 Task 5 在 `serve_web` 里丢弃的第二个返回值接起来——`credentials, _ = load_provider_credentials(...)` 改成 `credentials, receipts = ...`，再 `await provider_state_store.record_load(receipts)`。

各进程启动时写加载回执：只为**自己实际启用且需要**的 Provider 写——worker 写 `("worker","gemini")`，feishu listener / channel worker 写 `(..., "feishu")`，web 写自己实际消费的两项。**回执规则不在这里复述，一律以 Task 4 Step 4 的三行表为准**：只有读到可信 `generation` 之后、该 Provider 子树缺字段或非法时才写 `load_status='invalid'`；文件缺失或整体损坏时读不出 generation，**不写任何回执**。写成「读不到就写 invalid」会倒逼实现编一个 generation，而 `loaded_generation` 有 `> 0` 的 CHECK，编出来的就是伪造证据。三种情况**都不阻止进程启动**。

**干净部署必须能保存第一份配置。** runbook 只 `mkdir .config`，此时 `integrations.json`
**不存在**；而 `read_integration_config()` 要求文件存在且 `generation > 0`，`PUT` 又要先读当前
generation。若不处理，第一次保存没有起点，整条闭环卡死在第一步。

因此在应用层区分「文件不存在」与「文件非法」。`read_or_absent()` 已由 Task 4 引入，本 Task
直接复用，只新增一个把它折成代次的小函数：

```python
def current_generation(config: IntegrationConfig | None) -> int:
    return UNCONFIGURED_GENERATION if config is None else config.generation
```

`UNCONFIGURED_GENERATION = 0` 只是**逻辑**代次，不写进文件——文件里的 `generation` 仍恒 `> 0`，
Task 2 的契约不变。第一次成功保存写 `0 + 1 = 1`。

符号链接、非正规文件、超长、schema 非法这些情况**不**走这条路径，仍抛 `IntegrationConfigError`
并拒绝保存——「不存在」是未配置，「存在但坏」是故障，两者不能混。

`PUT /app/api/config` 在单个 Web 进程内用 `asyncio.Lock` 串行执行「`read_or_absent` → 合并未携带
字段（配置不存在时按全空起步）→ 校验 → `write_integration_config` 原子替换」，成功后返回
`{"generation": n+1, "restart_required": true}`。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/unit/test_integration_state.py tests/contract/test_ri5_config_api.py -q`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/xiaowei_agent/application/integration_state.py src/xiaowei_agent/persistence/provider_state.py src/xiaowei_agent/interfaces/web_app.py src/xiaowei_agent/interfaces/worker.py src/xiaowei_agent/interfaces/feishu_listener.py src/xiaowei_agent/interfaces/feishu_worker.py tests/unit/test_integration_state.py tests/contract/test_ri5_config_api.py
git commit -m "feat(ri5): add the config api, load receipts and provider display state"
```

---

### Task 7: 三个控制面探针

**Files:**
- Create: `src/xiaowei_agent/interfaces/provider_probe.py`
- Modify: `src/xiaowei_agent/interfaces/web_app.py`
- Modify: `src/xiaowei_agent/interfaces/web_auth.py`（测试 digest domain 与 callback 分流）
- Test: `tests/security/test_ri5_probe_boundary.py`
- Test: `tests/contract/test_ri5_probe_routes.py`

**Interfaces:**
- Consumes: Task 2 的配置读取；Task 3 的 `LOCAL_ADMIN` 与 `_digest`；Task 4 的 `read_or_absent`；Task 6 的 `ProviderStateStore.record_test`
- Produces:
  - `class ProbeOutcome(Contract): status: Literal["passed","failed"]; duration_ms: int; error_code: ProbeErrorCode | None`
  - `class ProbeErrorCode(StrEnum)`：闭集 `REAL_TEST_DISABLED`、`NOT_CONFIGURED`、`UNAUTHORIZED`、`TIMEOUT`、`UNAVAILABLE`、`INVALID_RESPONSE`
  - `async def probe_gemini_connection(...) -> ProbeOutcome`
  - `async def probe_feishu_credentials(...) -> ProbeOutcome`
  - `OAUTH_TEST_STATE_DOMAIN: Final[str] = "oauth-conn-test:v1"`
  - 路由：`POST /app/api/config/test/{check_name}`、`GET /oauth/feishu/callback` 的测试分支

- [ ] **Step 1: 写失败测试——开关关闭时零外部调用（最重要的一条）**

`tests/security/test_ri5_probe_boundary.py`：

```python
import pytest

pytestmark = pytest.mark.security

from xiaowei_agent.interfaces.provider_probe import (
    GEMINI_PROBE_INPUT,
    ProbeErrorCode,
    probe_feishu_credentials,
    probe_gemini_connection,
)


class _Spy:
    """记录出站调用次数；任何一次调用都会被计入。"""

    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._result = result
        self._error = error

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


async def test_disabled_switch_returns_the_closed_code_without_any_outbound_call() -> None:
    spy = _Spy()
    outcome = await probe_gemini_connection(
        enabled=False, api_key="k" * 8, transport=spy, timeout_seconds=5.0
    )
    assert outcome.status == "failed"
    assert outcome.error_code is ProbeErrorCode.REAL_TEST_DISABLED
    assert spy.calls == []


async def test_missing_configuration_is_refused_before_any_outbound_call() -> None:
    spy = _Spy()
    outcome = await probe_gemini_connection(
        enabled=True, api_key=None, transport=spy, timeout_seconds=5.0
    )
    assert outcome.error_code is ProbeErrorCode.NOT_CONFIGURED
    assert spy.calls == []


async def test_feishu_switch_is_independent_of_the_gemini_switch() -> None:
    spy = _Spy()
    outcome = await probe_feishu_credentials(
        enabled=False, app_id="cli_x", app_secret="s" * 8, transport=spy,
        timeout_seconds=5.0,
    )
    assert outcome.error_code is ProbeErrorCode.REAL_TEST_DISABLED
    assert spy.calls == []


async def test_gemini_probe_input_is_the_fixed_minimal_synthetic_text() -> None:
    spy = _Spy(result={"ok": True})
    await probe_gemini_connection(
        enabled=True, api_key="k" * 8, transport=spy, timeout_seconds=5.0
    )
    assert len(spy.calls) == 1
    assert spy.calls[0]["contents"] == GEMINI_PROBE_INPUT
    assert isinstance(GEMINI_PROBE_INPUT, str) and GEMINI_PROBE_INPUT


async def test_failure_never_returns_provider_body_or_secret() -> None:
    sentinel = "sentinel" + "-provider-body"
    fake_key = "k" * 8
    spy = _Spy(error=RuntimeError(sentinel))
    outcome = await probe_gemini_connection(
        enabled=True, api_key=fake_key, transport=spy, timeout_seconds=5.0
    )
    assert outcome.status == "failed"
    assert outcome.error_code in set(ProbeErrorCode)
    rendered = outcome.model_dump_json()
    assert sentinel not in rendered
    assert fake_key not in rendered


async def test_duration_is_recorded_and_bounded() -> None:
    spy = _Spy(result={"ok": True})
    outcome = await probe_gemini_connection(
        enabled=True, api_key="k" * 8, transport=spy, timeout_seconds=5.0
    )
    assert 0 <= outcome.duration_ms <= 600_000
```

同文件继续，路由与 OAuth 分流部分（用 `tests/fakes` 下既有的 in-memory session store 与 ASGI 测试客户端驱动）：

```python
def test_probe_never_creates_a_task_submission_or_evidence() -> None:
    # Given 一个记录调用次数的 fake TaskStore 与 evidence writer
    # When  依次触发三个探针路由
    # Then  两个 fake 的写入次数均为 0
    raise NotImplementedError("按规格写出断言后删除本行")

def test_probe_never_reaches_the_tool_gateway() -> None:
    # Given 一个记录调用次数的 ToolGateway spy
    # When  依次触发三个探针路由
    # Then  spy 调用次数为 0
    raise NotImplementedError("按规格写出断言后删除本行")

def test_probe_does_not_change_readiness() -> None:
    # Given readyz 在探针前返回 200
    # When  一个探针以 failed 收场
    # Then  readyz 仍返回 200
    raise NotImplementedError("按规格写出断言后删除本行")

def test_oauth_test_state_cannot_be_consumed_by_the_login_path() -> None:
    # Given 用 OAUTH_TEST_STATE_DOMAIN 签发的 state
    # When  把它送进登录 callback
    # Then  抛 WebOAuthStateError，且未签发任何 session
    raise NotImplementedError("按规格写出断言后删除本行")

def test_login_state_cannot_be_consumed_by_the_test_path() -> None:
    # Given 用 "oauth-state:v1" 签发的 state
    # When  把它送进测试 callback 分支
    # Then  被拒绝，且 provider_test_state 无写入
    raise NotImplementedError("按规格写出断言后删除本行")

def test_oauth_test_callback_requires_a_live_local_admin_session() -> None:
    # Given 一个有效测试 state，但请求不带 session cookie（另一例：session 已撤销）
    # When  命中测试 callback 分支
    # Then  返回拒绝，provider_test_state 行数为 0
    raise NotImplementedError("按规格写出断言后删除本行")

def test_oauth_test_branch_issues_no_cookie_and_no_session() -> None:
    # Given 有效测试 state + 有效 LOCAL_ADMIN session
    # When  测试 callback 成功完成 code exchange
    # Then  响应无任何 Set-Cookie 头，且 session store 的 rotate_session 调用次数为 0
    raise NotImplementedError("按规格写出断言后删除本行")

def test_oauth_test_requires_passing_feishu_credentials_first() -> None:
    # Given 当前 generation 的 feishu_credentials 不是 passed
    # When  POST /app/api/config/test/feishu_oauth
    # Then  返回闭集拒绝码，且未签发任何 state
    raise NotImplementedError("按规格写出断言后删除本行")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/security/test_ri5_probe_boundary.py -q`

Expected: FAIL —— `provider_probe` 不存在。

- [ ] **Step 3: 实现探针**

每个探针统一形状：先查开关（关则立刻返回 `REAL_TEST_DISABLED`，**不构造任何 client**），再查配置（缺则 `NOT_CONFIGURED`），再带超时发一次请求，用 `time.monotonic()` 量 `duration_ms` 并 clamp 到 `[0, 600000]`。异常一律映射到闭集码，原始正文只进 `except` 块即丢弃，不入日志、不入响应、不入数据库。

`gemini_connection` 复用 `interfaces/gemini_model.py` 已锁定的 provider/model/api_version/canonical origin 常量，`contents` 固定为一个模块级常量字符串，**不**经过 `IntentModelPort`。

`feishu_credentials` 只调 `tenant_access_token` 一类应用凭证接口，不碰消息与群聊 API。

`feishu_oauth`：`POST /app/api/config/test/feishu_oauth` 校验开关 + `LOCAL_ADMIN` + Origin/CSRF + 当前 generation 的 `feishu_credentials=passed`，然后用 `OAUTH_TEST_STATE_DOMAIN` 签发 state 并返回授权 URL。callback 先按登录域查 state，未命中再按测试域查；命中测试域时**必须**先 `authenticate()` 确认仍持有有效 `LOCAL_ADMIN` session，否则拒绝且不写结果；exchange 成功后读当时的当前配置与 generation 写入 `provider_test_state`，返回 302 回配置页，**不设置任何 cookie、不调用 `rotate_session`**。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/security/test_ri5_probe_boundary.py tests/contract/test_ri5_probe_routes.py -q`

Expected: PASS。

- [ ] **Step 5: 反证承重**

把「开关关闭立刻返回」那一行删掉，确认 `test_disabled_switch_returns_the_closed_code_without_any_outbound_call` 变红；把测试分支的 session 校验删掉，确认 `test_oauth_test_callback_requires_a_live_local_admin_session` 变红。两处恢复后全绿。

- [ ] **Step 6: 提交**

```bash
git add src/xiaowei_agent/interfaces/provider_probe.py src/xiaowei_agent/interfaces/web_app.py src/xiaowei_agent/interfaces/web_auth.py tests/security/test_ri5_probe_boundary.py tests/contract/test_ri5_probe_routes.py
git commit -m "feat(ri5): add the three control-plane provider probes"
```

---

### Task 8: Compose、Dockerfile、前端面板与 runbook

删掉两条旧 secret 路径，钉死 UID/GID，加 `.config` 挂载、LAN override 与预检脚本，补配置面板与首启顺序文档。

**Files:**
- Modify: `Dockerfile`（`Dockerfile:13` 的 `useradd --system`）
- Modify: `docker-compose.yml`（`secrets:` 于 `docker-compose.yml:154-158`；各服务 `secrets` 列表）
- Modify: `docker-compose.model.yml`（删除 `gemini_api_key` secret）
- Modify: `docker-compose.smoke.yml`（`:13` `XIAOWEI_FEISHU_APP_ID`、`:14` `XIAOWEI_FEISHU_APP_SECRET_FILE` —— 这两个 Settings 字段已在 Task 4 删除，不改会让 smoke 栈起不来）
- Create: `docker-compose.lan.yml`
- Create: `docker-compose.feishu.yml`
- Create: `src/xiaowei_agent/interfaces/config_preflight.py`（**不能放 `scripts/`**——`Dockerfile:8` 只 `COPY src ./src` 与 `alembic.ini`，`scripts/` 不进镜像，容器内执行必然 `ModuleNotFoundError`）
- Modify: `src/xiaowei_agent/interfaces/web_static/index.html`、`app.js`、`app.css`
- Modify: `scripts/compose_smoke.py`（`compose_smoke.py:48` 的 `_GEMINI_SECRET_DESTINATION`、`:393-411` 的 secrets 与 identity mount、`:507-508` 的两个 secret 路径）
- Modify: `README.md`、`.gitignore`、`.dockerignore`
- Modify: `tests/contract/test_compose_contract.py`（`:48` `_yaml()` 注册 `!override` loader；新增渲染期端口断言）
- Test: `tests/security/test_ri5_compose_boundary.py`

**Interfaces:**
- Consumes: 前 7 个 Task 的全部产出
- Produces: `docker-compose.lan.yml`（**替换**而非追加 `web-app` 的 `ports`）、`docker-compose.feishu.yml`、`python -m xiaowei_agent.interfaces.config_preflight`

- [ ] **Step 1: 写失败契约测试**

`tests/security/test_ri5_compose_boundary.py`（标 `security`，用 `PyYAML` 静态解析，沿用 `test_compose_contract.py` 既有读取方式）：

```python
def test_base_compose_publishes_only_loopback():
    # web-app ports == ["127.0.0.1:8080:8080"]；任何服务都不得出现 0.0.0.0
    raise NotImplementedError("按规格写出断言后删除本行")

def test_lan_override_only_changes_the_web_port():
    # docker-compose.lan.yml 顶层只含 services.web-app.ports
    raise NotImplementedError("按规格写出断言后删除本行")

def test_web_app_is_not_gated_behind_a_profile():
    # web-app 无 profiles 键；feishu-listener / channel-worker 仍在 m7-channels
    raise NotImplementedError("按规格写出断言后删除本行")

def test_web_switches_are_interpolated_not_hardcoded():
    # web-app.environment 里这些键的值形如 ${VAR:-false}，而非字面量 "false"；
    # 默认值必须仍是关闭
    raise NotImplementedError("按规格写出断言后删除本行")

def test_base_compose_does_not_require_the_feishu_identity_file():
    # 基础文件里没有 feishu-identities.json 的 bind；它只出现在 docker-compose.feishu.yml
    # 反例：干净 checkout（无 .secrets/）下 `docker compose config` 仍可渲染
    raise NotImplementedError("按规格写出断言后删除本行")

def test_the_old_provider_secrets_are_gone():
    # 两个 compose 文件里都不再有 gemini_api_key / feishu_app_secret
    raise NotImplementedError("按规格写出断言后删除本行")

def test_api_does_not_mount_the_integration_config():
    # api 服务的 volumes 中不含 .config 或 /run/xiaowei-config
    raise NotImplementedError("按规格写出断言后删除本行")

def test_web_mounts_the_config_directory_read_write():
    # web-app 的挂载项无 :ro 后缀
    raise NotImplementedError("按规格写出断言后删除本行")

def test_worker_and_feishu_mount_it_read_only():
    # worker / feishu-listener / channel-worker 的挂载项都以 :ro 结尾
    raise NotImplementedError("按规格写出断言后删除本行")

def test_container_target_path_is_fixed():
    # /run/xiaowei-config
    raise NotImplementedError("按规格写出断言后删除本行")

def test_dockerfile_pins_the_numeric_uid_and_gid():
    # --uid 10001 --gid 10001
    raise NotImplementedError("按规格写出断言后删除本行")

def test_read_only_rootfs_and_dropped_caps_are_unchanged():
    # read_only: true、cap_drop: [ALL]、no-new-privileges:true 三项逐服务仍在
    raise NotImplementedError("按规格写出断言后删除本行")

def test_lan_override_replaces_rather_than_appends_the_port():
    # 用 compose config 渲染 base + lan（无 compose CLI 时 skip）
    # 断言 web-app.ports 长度恰为 1，且不含 127.0.0.1:8080
    raise NotImplementedError("按规格写出断言后删除本行")

def test_existing_compose_yaml_reader_survives_the_override_tag():
    # 反例：用 yaml.safe_load 直接读 docker-compose.lan.yml 必须抛 ConstructorError；
    # 用本 Task 注册的 _ComposeLoader 读则成功。证明 loader 确实是承重的
    raise NotImplementedError("按规格写出断言后删除本行")

def test_preflight_module_lives_inside_the_packaged_source():
    # Dockerfile 只 COPY src；断言 config_preflight 在 src/xiaowei_agent/ 下且不在 scripts/
    raise NotImplementedError("按规格写出断言后删除本行")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/security/test_ri5_compose_boundary.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现 Compose 与 Dockerfile**

`Dockerfile:13` 改为：

```dockerfile
RUN groupadd --system --gid 10001 xiaowei \
 && useradd --system --uid 10001 --gid 10001 --home /app xiaowei
```

`docker-compose.yml` 有**三处**会让 runbook 直接跑不起来，必须一并改：

**(a) `web-app` 在 `m7-channels` profile 里**（`docker-compose.yml:119`）。普通 `docker compose up -d`
根本不会启动它。RI5 的 Web 是本里程碑的主入口，把 `web-app` 移出 profile，成为默认服务；
`feishu-listener` 与 `channel-worker` 保持在 `m7-channels`，因为飞书仍是可选插件。

**(b) 开关是硬编码字面量**（`docker-compose.yml:123-124` 的 `XIAOWEI_WEB_APP_ENABLED: "false"`、
`XIAOWEI_FEISHU_OAUTH_ENABLED: "false"`）。`.env` 里改这两个值**不会**覆盖它们——Compose 的
`environment:` 字面量优先。改为带默认值的插值，默认仍是关闭：

```yaml
  web-app:
    <<: *app-service
    command: ["python", "-m", "xiaowei_agent.interfaces.web_app"]
    environment:
      <<: *app-environment
      XIAOWEI_WEB_APP_ENABLED: "${XIAOWEI_WEB_APP_ENABLED:-false}"
      XIAOWEI_FEISHU_OAUTH_ENABLED: "${XIAOWEI_FEISHU_OAUTH_ENABLED:-false}"
      XIAOWEI_WEB_MODE: "${XIAOWEI_WEB_MODE:-https}"
      XIAOWEI_WEB_PUBLIC_ORIGIN: "${XIAOWEI_WEB_PUBLIC_ORIGIN:-}"
      XIAOWEI_GEMINI_REAL_TEST_ENABLED: "${XIAOWEI_GEMINI_REAL_TEST_ENABLED:-false}"
      XIAOWEI_FEISHU_REAL_TEST_ENABLED: "${XIAOWEI_FEISHU_REAL_TEST_ENABLED:-false}"
      XIAOWEI_WEB_BIND_HOST: "0.0.0.0"
      XIAOWEI_WEB_BIND_PORT: "8080"
```

默认值一律保持关闭，所以「默认关闭」的既有承诺不变；只是现在 `.env` 真的能覆盖。

**(c) 身份文件是硬挂载且 `create_host_path: false`**（`docker-compose.yml:129-135`）。飞书 OAuth
已降级为可选插件，但这个 bind 仍然必需——干净部署没有 `./.secrets/feishu-identities.json` 时
**容器直接起不来**，runbook 第 2 步必然失败。把它移出基础文件，挪进新的
`docker-compose.feishu.yml` override，只在启用飞书时叠加。

其余照旧：顶层 `secrets:` 删除 `feishu_app_secret`，各服务 `secrets` 列表只留 `postgres_password`；
`web-app` 加 `volumes: - ./.config:/run/xiaowei-config`，`worker` 与两个飞书服务加
`- ./.config:/run/xiaowei-config:ro`，`api` 不加。`read_only: true`、`cap_drop: ALL`、
`no-new-privileges` 一律不动。

`docker-compose.model.yml` 删除 `gemini_api_key` secret 与 worker 的对应挂载，只保留 `XIAOWEI_GEMINI_ENABLED=true`。

`docker-compose.lan.yml` 必须**替换**基础映射，不能追加。Compose 对 `ports` 默认是**合并**语义，
直接写一条新端口会渲染出 `127.0.0.1:8080` 与 `0.0.0.0:8080` **两条**，基础 loopback 映射并没有被
换掉——这既不是设计要的效果，也让「基础只发 loopback」的断言形同虚设。用 `!override`：

```yaml
services:
  web-app:
    ports: !override
      - "0.0.0.0:8080:8080"
```

`!override` 需要 Compose ≥ 2.24.4（与 ADR-015 D7 记录的项目支持下限一致）。

**它会打坏现有的 YAML 读法**：`tests/contract/test_compose_contract.py:48` 的 `_yaml()` 用
`yaml.safe_load`，遇到未注册的 `!override` 标签会抛 `ConstructorError`。因此本 Task 必须同时
给该 helper 注册一个忽略该标签的 loader，否则 override 一落地，既有 compose 契约测试立刻全红：

```python
class _ComposeLoader(yaml.SafeLoader):
    """Compose 的 !override / !reset 是渲染期指令，静态读取时按普通序列对待。"""


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(
        _tag, lambda loader, node: loader.construct_sequence(node, deep=True)
    )


def _yaml(name: str) -> dict[str, Any]:
    value = yaml.load((_ROOT / name).read_text(encoding="utf-8"), Loader=_ComposeLoader)
    assert isinstance(value, dict)
    return value
```

「渲染结果只有一条映射」不能靠静态 YAML 断言——静态读只能看到 override 文件里那一条。真正的
判据是 `compose config` 的**渲染输出**，因此该断言放在 `tests/contract/test_compose_contract.py`
里以子进程调用 `compose config` 完成（无 compose CLI 时 skip，与该文件既有做法一致）。

`.gitignore` / `.dockerignore` 追加 `.config/`。

`src/xiaowei_agent/interfaces/config_preflight.py` 以镜像内用户执行「建目录 → 建同目录哨兵文件 → chmod 0600 → 原子替换 → 重新读取 → **删除哨兵文件**」，只打印 `preflight: ok` 或闭集失败码，**绝不打印文件内容**。它复用 Task 2 的 `write_integration_config` / `read_integration_config`，不另写一份文件逻辑。

**哨兵文件名固定为 `.preflight-probe.json`，绝不能用 `integrations.json`。** 预检的目的是证明这个目录可建、可写、可原子替换、可回读，不是生成配置。写到真实路径会留下一份 `generation=1` 的配置，于是首次保存从 1 递增到 2，页面显示的代次与「第一份配置」对不上，且 `read_or_absent` 再也读不到「尚未配置」这个状态——干净部署的起点被预检自己污染了。成功与失败路径都要 `os.unlink` 哨兵文件；退出前断言 `read_or_absent(真实路径) is None`（已存在配置的重复预检除外，此时它必须原样不动）。

- [ ] **Step 4: 实现前端面板**

`index.html` / `app.js` 增加配置区：Gemini 与飞书各一块，显示 enabled 开关、`已配置/未配置`、只读的固定模型与 callback URL、三项状态徽章、测试按钮（开关关闭时 `disabled`）、保存后的「需重启生效」提示。所有服务端文本用 `textContent` 渲染，**禁止 `innerHTML`**；不放任何 Secret 到 DOM。

- [ ] **Step 5: 写 runbook 并跑测试**

`README.md` 增加不可跳过的首启顺序：

1. 建目录：

   ```bash
   mkdir -p .config && chmod 700 .config
   # Linux Docker Engine 另需（macOS Docker Desktop 跳过）：
   sudo chown 10001:10001 .config
   ```

2. 在 `.env` 写入首启参数（这些键现在真的会被 Compose 插值消费）：

   ```bash
   XIAOWEI_WEB_APP_ENABLED=true
   XIAOWEI_WEB_MODE=lan_http
   XIAOWEI_WEB_PUBLIC_ORIGIN=http://127.0.0.1:8080
   ```

   `lan_http` 接受 canonical loopback，所以首启阶段不必先用 HTTPS 模式。

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
   参数展开**不做分词**，`$COMPOSE` 会被当成一个名为 `docker compose`（含空格）的命令，直接
   `command not found`。bash 下碰巧能用，zsh 下必错——本项目宿主 shell 是 zsh。
   后续步骤统一写 `compose ...`。

4. **先跑预检，成功后才启动 Web**：

   ```bash
   compose run --rm --no-deps \
     -v "$PWD/.config:/run/xiaowei-config" \
     web-app python -m xiaowei_agent.interfaces.config_preflight
   ```

   只应输出 `preflight: ok`。失败时**不要**继续——目录属主或权限不对，Web 起来也存不下配置。

5. 启动（基础文件只发布 `127.0.0.1:8080`；`web-app` 已不在 profile 里，普通 up 即可拉起）：

   ```bash
   compose up -d
   ```

6. 宿主机浏览器打开 `http://127.0.0.1:8080`，用 `admin/admin` 登录并**完成强制改密**。

7. 改密完成后，再改 `.env` 的 public origin 为局域网地址：

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

9. 从局域网地址用新密码重新登录（旧 Cookie 因 origin digest 变化已失效，属预期）。

启用飞书时额外叠加 `-f docker-compose.feishu.yml` 并准备 `./.secrets/feishu-identities.json`；
不启用飞书时无需该文件。

并写明**残余风险**：第 3 步之前套用 LAN override，`admin/admin` 会暴露给同网段；补救是改密后重建 Web 并撤销全部 `local_admin` session。

**`scripts/compose_smoke.py` 必须一起迁移**，否则它会继续构造已被删除的 `gemini_api_key` /
`feishu_app_secret` secret 并挂到 `/run/secrets/...`，本 Task 结束时必然失败。改动点：`:48` 删
`_GEMINI_SECRET_DESTINATION`；`:393-411` 的 `secrets` 段删两个 Provider secret，改为生成一份
**合成的** `.config/integrations.json`（值用拆开写的假串，见 Global Constraints）并按 `web-app`
读写、其余只读挂载；`:507-508` 删 `feishu_secret` 路径。identity mount 随
`docker-compose.feishu.yml` 走，只在启用飞书的 smoke 分支叠加。

Run:

```bash
python -m pytest tests/security/test_ri5_compose_boundary.py tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py -q
python -m scripts.compose_smoke
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add Dockerfile docker-compose.yml docker-compose.model.yml docker-compose.smoke.yml docker-compose.lan.yml docker-compose.feishu.yml src/xiaowei_agent/interfaces/config_preflight.py scripts/compose_smoke.py src/xiaowei_agent/interfaces/web_static README.md .gitignore .dockerignore tests/security/test_ri5_compose_boundary.py tests/contract/test_compose_contract.py tests/contract/test_compose_smoke_script.py
git commit -m "feat(ri5): move provider credentials to a mounted config directory"
```

---

### Task 9: 全量验收与交接

**Files:**
- Modify: `AGENT_HANDOFF.md`
- Review: Task 0–8 改动的全部文件

- [ ] **Step 1: 跑完整基线**

依赖已在 Task 0 同步过，这里不重复 `uv sync`。

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

Expected: 四条全绿，且与 Task 0 记录的开工前基线对比无新增失败。若出现失败，先判断是本轮引入
还是既有——必要时用 `origin/main` 的树在同一 venv 复跑对比失败集合。

- [ ] **Step 2: 本地闭环验证**

按 Task 8 Step 5 的 runbook 实跑一遍，记录：

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/healthz
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/readyz
```

Expected: `200`、`200`；`admin/admin` 登录后被强制改密；改密前访问 `/app/api/config` 返回 403；保存配置后 generation 递增且页面显示「待应用」；重启后变「待测试」。

**两个真实测试开关保持 `false`，全程不点任何测试按钮、不读取真实凭据、不联网。**

- [ ] **Step 3: 逐条核对边界**

确认改动中**没有**：任何 Secret 值进入响应/日志/数据库/页面、`docker.sock` 挂载、任意 endpoint 或路径输入、新执行进程或 dispatch lane、Web 取得模型端口、探针创建 Task/Evidence、`web_oauth_states` 的新列、基础 compose 的非 loopback 发布。

再确认旧真源已从**运行代码**中移除。注意：ADR、migration 说明、handoff 与迁移测试自身**必然**
会提到旧路径（那是历史记录与反例），所以**不能**以「全仓 grep 零输出」作为验收条件。判据是：

```bash
# 判据一：src/ 下零命中（这一条由 Task 4 的 test_no_source_file_references_the_legacy_provider_secret_paths 承重）
grep -rn "web_detail_base_url\|/run/secrets/gemini_api_key\|feishu_app_secret_file" src/ --exclude-dir=__pycache__

# 判据二：compose 与 .env.example 下零命中
grep -n "gemini_api_key\|feishu_app_secret" docker-compose*.yml .env.example
```

两条都应无输出。其余目录的命中逐条人工确认属于「历史记录」或「反例断言」，不得机械清零。

- [ ] **Step 4: 更新 handoff 并提交**

在 `AGENT_HANDOFF.md` 记录 RI5 实现基线 SHA、证据等级 `tests`、以及**未验证项**：真实 Gemini 调用、真实飞书调用、部署、canary、用户验收全部未做，PR 3E 与 RI2 现场 GO 仍未下达。

```bash
git add AGENT_HANDOFF.md
git commit -m "docs(ri5): record the local web admin implementation evidence"
```

---

## Explicitly Out of Scope

- 真实 Gemini 网络调用（须 RI3 PR 3E 现场 GO）与真实飞书调用（须 RI2 现场 GO）。
- StarRocks 的 Admin 配置或测试请求。
- 不可变配置版本、显式发布、审批、审计历史、readback/rollback 状态机、通用 provider registry。
- 配置热加载、自动重启、自动回滚、Docker 控制、心跳、多实例、多 Web 写实例。
- 多用户、RBAC、找回密码、第二个账号、角色或租户编辑。
- TLS、Ingress、证书管理、公网部署（属 RI6）。
- 模型、endpoint、API 版本的可编辑化。
