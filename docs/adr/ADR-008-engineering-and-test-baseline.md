# ADR-008：工程与测试基线（Python 3.11、pytest、security marker gate、Ruff、mypy）

- 状态：Accepted
- 日期：2026-09-01
- 决策人：项目负责人
- 相关：[AGENTS.md](../../AGENTS.md)、[ARCHITECTURE.md](../../ARCHITECTURE.md)、[README.md](../../README.md)、[ADR-007](ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)

## 背景

`AGENTS.md` 已把 pytest 和 `security` marker gate 定为基线，但四份文档中同时存在裸 `pytest -q` 与 `python -m pytest -q` 两种调用形式，违反单一真源原则；裸调用在解释器不确定时会取到错误环境。`README.md` 另有「Python 3.11+」的写法，隐含承诺了没有任何验证证据的 3.12/3.13 兼容性。

## 决策

### D1 Python 版本

**Python 3.11 是首个且当前唯一强制验证的版本。** M1 在 `pyproject.toml` 的 `requires-python` 固化。文档一律不宣称支持任何未经验证的版本，不使用 `3.11+` 之类的开放写法。

### D2 测试 runner

**pytest 为唯一测试 runner。** 理由是本项目的核心测试需求正是 pytest 的强项：参数化恶意输入矩阵、组合 fixture、异步测试和 marker gate。不并存第二套 runner。

### D3 security marker 为独立 CI gate

`python -m pytest -q` 与 `python -m pytest -m security -q` 必须**分别执行、分别记录**，不得以全量测试结果替代安全 gate。触及 `governance/`、`planning/`、`tools/` 的任何变更强制运行安全 gate，不得以轻档或中档验收为由豁免。

### D4 测试目录分层

固定为：

```text
tests/
├── unit/         # 纯函数和状态迁移
├── contract/     # 模块间 DTO / Protocol 契约
├── security/     # 必须标记 pytest.mark.security
├── integration/  # 本地隔离 PostgreSQL / Compose / fake adapter
└── evals/        # L0-L3 行为和安全评测
```

安全测试只放在 `tests/security/` 并标记 `@pytest.mark.security`。

### D5 静态检查工具

- **Ruff 是唯一 linter。** 本 ADR **不**把 Ruff 声明为 formatter；格式化方案留待 M1 单独决定。
- **mypy 是唯一类型检查器。**
- 不并存 flake8、isort、pyright 或其他同类工具。

### D6 验证命令单一真源

全项目验证入口固定为下列四条，README 与 CI 共用同一份定义：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

**禁止在任何文档、脚本或 CI 配置中使用裸 `pytest` 调用形式。**

### D7 M0 的范围限制

M0 只记录本决策。**不创建 `pyproject.toml`、不建立 Python 包、不建立测试目录、不安装任何依赖。** 上述内容属于 M1。

## 后果

- 四份既有文档中的 7 处裸命令需在 M0 同步修正，属本 ADR 的直接后果。
- CI 配置在 M1 建立时直接引用 D6 的四条命令，不再自行拼装。
- 引入格式化工具时需要一次独立决策，不会因为 Ruff 已在项目中而被默认启用。

## 备选方案与否决理由

- **`unittest`**：缺少参数化恶意输入矩阵、组合 fixture 和 marker gate，无法支撑安全测试作为独立 CI gate。
- **pyright**：与 mypy 并存会产生两套类型判定真源，冲突时无仲裁依据。
- **Ruff 同时兼任 formatter**：格式化会产生大面积机械 diff，影响按精确 SHA 的逐行审查；应在工程基线稳定后单独评估。

## 回滚

更换测试 runner、linter 或类型检查器，必须先修订本 ADR，并同步更新 `README.md`、`AGENTS.md`、`ARCHITECTURE.md` 和 CI 配置，不允许出现多个未说明的入口。
