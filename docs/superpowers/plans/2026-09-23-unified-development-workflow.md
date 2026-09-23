# 通用开发流程 V1 落地实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. 按下列复选框记录执行；本次由用户直接指定 Codex 实施，最终使用独立上下文复审。

**Goal:** 把已复审方案落实为现有协作规则，覆盖后续所有开发，并消除重复进度与过期口径。

**Architecture:** 协作规则归 AGENTS，架构与 ADR 保留稳定契约，路线保留阶段门，当前状态归 handoff。历史原文保存为有明确基线的快照；这不代表新的里程碑验收或风险关闭。

**Tech Stack:** Markdown、既有 pytest 文档契约；不增加依赖或运行平台。

**Spec:** [通用开发流程设计](../specs/2026-09-22-unified-development-workflow-design.md)。用户在本任务先要求复审，再明确“那你实施吧”，本计划只细化已批准落地范围。

## Global Constraints

- 基线 `6e7927559f72ddc2182c8d518e48312e17fd4c1a`，方案受审提交 `bebe9ca42b6b82c6ee81a30822fb9d0bf170d9fb`；实施前已回读远端 main，未漂移。
- 复用 `claude/development-workflow-v1` 隔离工作区；保留主工作区的用户修改。
- 不修改产品源码、ADR 安全语义、依赖、CI、Compose 或产品路线；本次不接真实服务、不部署、不自行合并。
- 四条权威命令与强制 security gate 保持原定义；文档测试保留授权、阶段归属、证据上限及凭据可见性保护。
- 新规则以获授权集成为生效点；已批准的在途计划有效，规则修改不自动增加其范围或权限。

## Review Focus

1. 当前 handoff 中的未解决风险、批准来源和真实调用缺口是否仍可直接找到；快照不是关闭风险。
2. README/ARCHITECTURE 改为引用后，是否仍能取得唯一的当前状态和证据边界，旧叙述是否残留。
3. 守卫从重复措辞改为真源/引用关系后，删除引用、破坏目标、恢复旧进度是否仍能被发现。
4. 文档轻档、普通能力中档、鉴权/迁移深档、缺环境和基线漂移能否得到明确且适度的执行路径。
5. 实施授权、审查通过、规则集成与实际提效是否仍分开；W2 及真实调用的独立门是否保留。

## 旧内容去向

| 原内容 | 落点与验证 |
| --- | --- |
| 日常协作、授权、分档、证据、复审 | 替换 AGENTS 既有章节；场景推演及独立复审 |
| README 顶部阶段流水 | handoff 当前摘要 + 原 handoff 历史快照；README 只链接当前入口 |
| ARCHITECTURE 的 RI5/W1 实施进度 | handoff；原有写内核、目录、审计、激活不变量仍在 ARCHITECTURE |
| handoff 的历次 SHA/CI/反证流水 | `docs/handoff/archive/2026-09-23-pre-workflow-v1.md` 保存基线原文；明确不作为当前指令 |
| 未解决风险、缺少的运行证据、批准来源 | handoff 继续维护，不移成只读历史后放任失联 |
| DEVELOPMENT_PLAN 的日常重复细则 | 引用 AGENTS；里程碑批准、退出与真实调用门仍保留 |

### Task 1: 交付可复审的流程规则与真源迁移

**Files:**
- Modify: `AGENTS.md`、`ARCHITECTURE.md`、`AGENT_HANDOFF.md`、`README.md`、`DEVELOPMENT_PLAN.md`。
- Modify: `tests/contract/test_doc_fact_binding.py`；只改受本次迁移影响的断言。
- Create: `docs/handoff/archive/2026-09-23-pre-workflow-v1.md`。
- Modify: 设计稿的状态说明；保留设计决策，执行结果记录于本计划。

**Interfaces:**
- Consumes: 方案 §2–11、ADR-007/008、当前文档事实守卫与基线 handoff。
- Produces: AGENTS 的唯一日常流程、handoff 的当前状态入口、指向它的有效导航，以及保留授权/阶段/凭据边界的契约测试。

- [x] **Step 1: 记录基线与增加针对性反例。** 已有文档契约先跑：`python -m pytest tests/contract/test_doc_fact_binding.py -q`，Expected: `59 passed`。给当前状态引用检查增加正常、断链、错目标、重复进度反例；先观察旧文档未收敛时失败。
- [x] **Step 2: 调整文档。** 按去向表保留原始 handoff 快照，修正当前摘要和两处已核实的过期状态；替换 AGENTS 日常流程，收敛 README/ARCHITECTURE/DEVELOPMENT_PLAN。保持源码、CI、ADR 与启动 runbook 不变。
- [x] **Step 3: 迁移既有断言。** 架构不变量仍在架构断言，当前实施与证据上限在 handoff 断言，README 绑定当前状态链接；保留所有未受影响的文档保护。运行 `python -m pytest tests/contract/test_doc_fact_binding.py -q`，Expected: 全部通过，且新反例能识别损坏。
- [x] **Step 4: 最终验证。** 激活既有 Python 3.11.16 工具链并令 `PYTHONPATH=src`（已确认 uv.lock 相同、加载本工作区源码）。依次执行 `python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`、`git diff --check`。Expected: exit 0；未设置 PostgreSQL DSN 的 skip 如实列明。另核对快照正文与基线字节相同、修改文档的相对链接可解析、风险去向与实际 diff。
- [ ] **Step 5: 提交并独立复审。** 只暂存本计划列明的文件，提交精确 SHA；独立审查者检查基线至候选的全部 diff、Review Focus 和验证记录。我核实并处理有证据的问题；已完成的测试不因写总结而无理由重跑。

## 执行记录

- Pre-flight: 单一任务共同完成一套文档与引用迁移，无跨任务接口。基线文档契约 `59 passed in 0.11s`。
- Ruling: 用户“那你实施吧”是本次实施和实现者调整的直接授权；不为同一范围再次申请开工。合并、部署与验收签认不由该指令推导。
- Ruling: 本次不为每条流程措辞建立字符串测试。针对文档导航、真源与迁移做机械校验，流程执行效果通过场景复审判断；错误判断的代价是留下文档歧义，最终独立复审覆盖该风险。

### 本地验证（提交前候选工作树）

环境：Python 3.11.16；复用已安装的 dev 环境，已比较 uv.lock 相同；`PYTHONPATH=src` 指向本工作区，`PYTHONDONTWRITEBYTECODE=1`，未设置 `PYTEST_POSTGRES_DSN`。未安装新依赖。

| 实际命令 | exit code 与尾部输出 |
| --- | --- |
| `python -m pytest tests/contract/test_doc_fact_binding.py -k current_status -q`（修改文档前） | 1；`2 failed, 7 passed, 58 deselected`，失败点为旧 README/ARCHITECTURE 尚无唯一当前状态链接 |
| `python -m pytest tests/contract/test_doc_fact_binding.py -q`（迁移后） | 0；`67 passed in 0.12s` |
| `python -m pytest -q` | 0；`4333 passed, 349 skipped, 5 warnings in 40.15s` |
| `python -m pytest -m security -q` | 0；`1505 passed, 83 skipped, 3094 deselected, 5 warnings in 22.47s` |
| `ruff check .` | 0；`All checks passed!` |
| `mypy src` | 0；`Success: no issues found in 202 source files` |
| `git diff --check` | 0；无输出 |

五条 warning 均由 socket/DNS 禁用反例触发。跳过的 PostgreSQL 用例不算运行证据；本轮未执行数据库、Compose、浏览器、真实服务、部署或用户验收。

一次性迁移核对：快照标记后的 119,953 字节与基线 `git show <base>:AGENT_HANDOFF.md` 完全相同；98 个当前文档相对链接及锚点可解析；AGENTS 的安全边界/代码落点/契约规范/四命令章节与基线逐字相同；DEVELOPMENT_PLAN 阶段定义、退出标准和启动门逐字相同。产品源码、ADR、依赖、CI、Compose 未改。

### 冲突与风险去向核对

- `Task 1.4` 待实现、RI5 尚无源码、I1-D 正在实现、W1a/W1b 尚无载体等旧叙述与同一基线的已交付记录冲突；当前 handoff 统一到已合入的离线事实，原句留在历史快照。
- W1b 的 `DirectoryFeishuIdentityDirectory.resolve()` 与 `local_stack.py` 的 Web/listener 装配确认按请求查库，取代“静态 allowlist 只在启动加载、撤权须重启”；真实飞书缺口仍保留。
- `integration_config_file.py` 确认 Provider JSON 使用独立 reader；`read_secret_file` 已无生产调用者。架构与当前风险改为 JSON reader 的实际边界，仍保留 owner/mode/中间目录与真实挂载验收缺口。
- 旧 handoff 的“不要盲改”整节逐字保留；残余风险中仅去掉 Task 1.4 待落地与已划掉的 M2 旧引导句，更新上述静态身份与 credential reader 两项，其他段落逐字保留。旧未覆盖项中的本机环境障碍改为历史环境、恢复前重查，未声称已解决。
- Ruling: 不机械保留已被源码否定的风险叙述；保留仍成立的风险、未验证边界及历史原文。若来源判断错误会误导后续阶段，独立复审需重点核对以上三条调用链与替代关系。

### 证据上限与恢复

- 已验证：上表本地检查、快照完整性、有效引用与稳定门保留。
- 只读推理：轻/中/深任务、缺环境、授权复用和基线漂移可按 AGENTS 找到执行路径；待独立审查核对。
- 未覆盖：远端候选 CI、正式运行与实际长期提效；本次实施不授权 W2 或真实接入开工。
- 残余风险：仍有必须人工判断的规范语义，文档测试不证明使用者一定正确执行。新流程集成前主线仍采用旧规则；获准后可通过反向提交本次流程差异恢复旧文档，保留历史快照及本轮验证记录，不影响产品数据。
- 流程指标：首次正式路径结果用时未采集（本次为文档治理），复审返工根因数待独立复审，后期首次暴露问题数未采集；没有提速比例结论。
