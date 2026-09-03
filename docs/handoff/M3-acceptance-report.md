# M3 第一条 StarRocks 只读垂直闭环 —— 验收报告

> 按 [DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) §9 的四段格式输出。
>
> **验收对象**：分支 `claude/m3-starrocks-slow-query` 的 HEAD，
> 相对 `main = 4e3e30941f9102dd814283f3e3c956f5e24bc699`。
>
> **精确 SHA 由交接时的 `git rev-parse HEAD` 提供，不写在本文件内。**
> 上一版把它写死成 `7b25621`，而本文件自身的提交必然在那之后——报告永远落后它所
> 描述的对象一个提交，验收方拿到的 SHA 与报告里写的对不上。这与 AGENT_HANDOFF.md
> 「本文件所在提交的 SHA 不写在文件内」是同一条纪律，此处补齐。
>
> **状态：已验收通过并合入 `main`（最终对象 `64d295c8e4f38028527ec9a496262c7a660258b1`）。**
> 本报告是当时提交给 Codex 深档审查的证据，保留原貌备查；合并后的完整事实以
> [archive/2026-09-03-M3-starrocks-slow-query.md](archive/2026-09-03-M3-starrocks-slow-query.md) 为准。
> 能力状态最强为 `tests`——**不是** `deployed SHA`、**不是** `canary`、**不是** `user-accepted`。

---

## 1. 已验证

### 1.1 四条命令的真实尾部输出

环境：本机 `.venv`，Python **3.11.15**（`uv sync --extra dev --frozen`），
`sqlglot` 锁定 **30.17.0**。在 `7b25621` 上原样执行 [ADR-008](../adr/ADR-008-engineering-and-test-baseline.md) 的四条命令：

```text
python -m pytest -q             → 1229 passed, 3 warnings
python -m pytest -m security -q → 823 passed, 406 deselected, 3 warnings
ruff check .                    → All checks passed!
mypy src                        → Success: no issues found in 74 source files
```

对照 M2 验收对象（`319253a`）的 640 / 499：全量 +589，安全 gate +324。
（首轮送审时为 1213 / 809；差额来自第 5 节所述的契约收口与其证明测试。）

补充证据：

```text
git diff --check main..HEAD                        → 无输出（diff 干净）
git diff --stat main..HEAD                         → 见交接时实跑；本文件不维护会随修订漂移的数字
git diff --stat main..HEAD -- .github/workflows/ci.yml → 无输出（ci.yml 未改）
grep _E1_EXECUTION_ENABLED src/xiaowei_agent/tools/gateway.py
                                                   → _E1_EXECUTION_ENABLED: Final[bool] = False
```

### 1.2 eval 分层计数（组件级与端到端分开记录）

| 层 | 范围 | 命令 | 结果 |
| --- | --- | --- | --- |
| **L0** | 安全与不变量（组件级，同时打 `security` marker） | `python -m pytest -q tests/evals/test_l0_security.py` | **57 passed** |
| **L1** | 意图与补槽（组件级） | `python -m pytest -q tests/evals/test_l1_intent.py` | **18 passed** |
| **L2** | 只读闭环（端到端） | `python -m pytest -q tests/evals/test_l2_readonly.py` | **17 passed** |
| 端到端 Runtime 契约与绕过反证 | 端到端 | `python -m pytest -q tests/contract/test_runtime_facade.py tests/security/test_runtime_bypass.py` | **26 passed** |

L0 语料 54 条覆盖攻击矩阵 A1–A36 全部行；覆盖完整性由
`test_corpus_covers_the_whole_attack_matrix` 与
`test_every_corpus_carrier_has_an_executing_driver` 两条机制性断言承重，而不是靠人记得。

### 1.3 提交清单（T0–T14，每个任务一个 TDD 提交）

```text
43c9781 T0  sqlglot 依赖、方言实证基线与双向分层元断言
2c9b4c0 T1  capability 声明、SqlSurface、静态 Registry 与规则式 IntentInterpreter
5720b4e T2  目标解析与 target_fingerprint 稳定性
eb4c078 T3  慢查询参数 schema、时间窗规范化与 typed_arguments 往返
5298c7c T4  确定性 SQL 编译器与唯一的目标范围谓词构造器
b72b2fc T5  DeterministicCapabilityResolver 与 PlanCompiler
b2f98c2 T6  SQLGuard——闭集 AST 校验与重编译比对
821ba3e T7  ToolPolicy、ApprovalGate 与六段 StepAdmission
ddf2a3c T8  PlanStore、EvidenceLedger 与纯证据构造器
eaed170 T9  DeterministicStepRunner、WorkflowPaused 与按调用内容回放的 fake adapter
dab358c T10 Answerability 判定表与 RenderPayload 投影
957eb9d T11 StructuredLogTraceSink 与 Runner 侧的阶段归因
02a65f9 T12 XiaoweiRuntime facade、绕过反证与生成式能力地图
6d630ee T13 L0-L2 离线 eval 语料与驱动
9f1aa7b T14 文档与 handoff 收口
7b25621 T14 去掉 approval.py 末尾多余空行（使 git diff --check 干净）
```

### 1.4 TDD 反证逐条记录

全部反证在 `PYTHONDONTWRITEBYTECODE=1` 下执行：撤掉承重保护 → 确认转红 → 还原 → 确认转绿。

| 任务 | 撤掉什么 | 转红结果 | 还原 |
| --- | --- | --- | --- |
| T0 | 往 `_ALLOWED_INTERNAL` 塞不存在的包 | `test_every_registered_package_exists` FAILED | 13 passed |
| T0 | 新建未登记的空包 | `test_every_existing_package_is_registered` FAILED | 13 passed |
| T1 | 槽位白名单改成原样透传 | 9 failed / 14 passed（7 条执行权槽位 + 2 条） | 23 passed |
| T2 | 让槽位 `environment_id` 覆盖上下文 | 7 failed | 18 passed |
| T2 | 未知环境改为回退默认环境 | 3 failed | 18 passed |
| T3 | 标识符正则放宽为"非空即可" | 47 failed / 42 passed | 89 passed |
| T3 | 撤掉 `as_of` 的整分钟取整 | 2 failed | 转绿 |
| T3 | 未知键拒绝改成"只取已知字段" | 2 failed | 113 passed |
| T4 | count 模板不调 `_scope_predicates` | `test_count_scope_is_exactly_list_scope_minus_the_threshold` 后 3 组 FAILED | 23 passed |
| T4 | 字面量改成 f-string 拼接 | `test_compiler_never_interpolates_raw_strings` FAILED | 23 passed |
| T5 | `build_plan_step()` 换成直接构造 `PlanStep` 并写死 READ | 3 failed | 38 passed |
| T5 | `typed_arguments` 去掉 `row_limit` | 2 failed | 22 passed |
| T6 | 节点闭集改成只拒 Union/Subquery | 7 failed（A5、A9、A10×3、A11、闭集断言） | 70 passed |
| T6 | 撤掉规则 8 的父节点约束 | 2 failed（A20 + 顺序断言） | 70 passed |
| T6 | 撤掉规则 9 的 `.comments` 遍历 | 3 failed（A3、A4×2） | 70 passed |
| T6 | 撤掉重编译比对 | 3 failed（A36、AST 全过用例、A22 反向用例） | 70 passed |
| T6 | 撤掉规则 6 的 catalog / 空 db 检查 | 1 failed（A31） | 70 passed |
| T6 | `table_key` 改回只比 `table.name` | **24 failed，含 happy path** | 70 passed |
| T6 | 撤掉规则 10 的 `is_string` 检查 | 1 failed（A34） | 70 passed |
| T7 | 撤掉 `verify_plan_effects` | 2 failed（伪标拒绝） | 22 passed |
| T7 | 撤掉 `ApprovalGate` 分支 | 5 failed | 22 passed |
| T7 | 撤掉 `verify_policy_revision` | 1 failed | 22 passed |
| T7 | 撤掉准入的 SQLGuard 调用 | 2 failed | 22 passed |
| T8 | 给 `evidence/builder.py` 加 `async def` | 纯度测试 FAILED | 52 passed |
| T8 | 让 `evidence` import `persistence` | 2 failed（纯度 + 分层） | 52 passed |
| T8 | `StoredPlan` 多一个 `status` 字段 | 字段集断言 FAILED | 52 passed |
| T8 | 撤掉 ledger 的 append-only 冲突检查 | `test_ledger_is_append_only` FAILED | 52 passed |
| T8 | 撤掉证据的列白名单过滤 | 列过滤断言 FAILED | 52 passed |
| T9 | `transition` 不传 `fencing_token` | 24 failed | 31 passed |
| T9 | 撤掉 `result.applied` 检查 | **首轮全绿 → 补测试后转红**（见 §1.5） | 31 passed |
| T9 | 撤掉 resume 的漂移重算 | 3 failed（plan / target / policy_revision） | 31 passed |
| T9 | 撤掉预算检查 | 预算耗尽用例 FAILED | 31 passed |
| T9 | 条件求值改读本地 dict | ledger 承重用例 FAILED | 31 passed |
| T10 | "范围内 count=0"也判成可答 | 2 failed（含 B1 回归） | 31 passed |
| T10 | 无证据时也判成可答 | 空证据用例 FAILED | 31 passed |
| T10 | 让 `reflection` import `tools` | 2 failed | 31 passed |
| T10 | 让渲染输出表名 | 表名泄漏用例 FAILED | 31 passed |
| T11 | 准入失败不再发 REJECTED 事件 | 归因用例 FAILED | 13 passed |
| T11 | adapter 失败也记成 GATEWAY OK | 归因用例 FAILED | 13 passed |
| T11 | sink 把 detail 拼进消息体 | **首轮全绿 → 改断言后转红**（见 §1.5） | 13 passed |
| T12 | Runtime 不读 ledger、从 refs 造证据 | ledger 读回用例 FAILED | 26 passed |
| T12 | 终态一律 SUCCEEDED | 4 failed（含 B1 端到端与超时降级） | 26 passed |
| T12 | Runtime 取 `gateway.invoke` 绑定方法 | **首轮全绿 → 补断言后转红**（见 §1.5） | 26 passed |
| T12 | 撤掉幂等短路 | 幂等用例 FAILED | 26 passed |
| T13 | 从 L0 语料删掉 A20 | 覆盖完整性断言 FAILED | 92 passed |
| T13 | 往语料加一条无驱动的载体 | 驱动覆盖断言 FAILED | 92 passed |
| T13 | count 模板丢掉目标过滤（B1） | **全量下 5 条转红**（见 §1.6） | 1213 passed |

### 1.5 四次"反证首轮全绿"暴露的真实覆盖缺口（已各自补齐）

这四条是本轮最有价值的产出：**测试是绿的，但绿的理由不对**。

1. **T9 `result.applied`**：原用例先把任务取消，于是"终态任务拿不到租约"先挡住，
   `applied` 检查从未单独承重。补 `test_rejected_transition_stops_the_run_with_zero_calls`
   （任务处于 `AWAITING_APPROVAL`，租约可取但 →`PLANNING` 是非法迁移），重跑变异后转红。
2. **T11 sink 常量消息**：原用例断言"canary 不出现在 `caplog.text`"，但 `detail` 在
   契约层就已被 `scrub_text` 处理，即使 sink 把取值拼进消息也检测不到。改为直接断言
   `caplog.records[0].getMessage() == "trace event"`，并另留一条用例说明契约层脱敏这层纵深。
3. **T12 Gateway 绕过扫描**：原断言只扫 `ast.Call`，"先取绑定方法、再在别处调用"看不见。
   补 `test_application_never_touches_gateway_invoke`（扫全部 `ast.Attribute`）与
   `test_application_never_imports_the_gateway_module`。
4. **T13 纸面语料**：A26（外部文本夹带指令）与 A29（终态保护）只在语料里、没有驱动。
   各补一条真实用例，并新增 `test_every_corpus_carrier_has_an_executing_driver` 使这类
   纸面条目此后不可能存在。

### 1.6 B1（本计划修复的最严重缺陷）的承重情况

把 count 模板改回"只带窗口、不带目标过滤"，全量测试下 **5 条**用例转红，分布在三层：

```text
tests/unit/test_sql_compiler.py::test_count_scope_is_exactly_list_scope_minus_the_threshold[db]
tests/unit/test_sql_compiler.py::test_count_scope_is_exactly_list_scope_minus_the_threshold[db_user]
tests/unit/test_sql_compiler.py::test_count_scope_is_exactly_list_scope_minus_the_threshold[db_user_query]
tests/security/test_sqlguard.py::test_a36_count_sql_stripped_of_the_scope_filters_is_refused
tests/evals/test_l0_security.py::test_a36_count_without_scope_filters_is_refused
```

### 1.7 实现期发现并修复的领域缺陷（不在计划的任务清单里）

1. **s1 失败后仍执行可选分支**（T9）。`EVIDENCE_ROW_COUNT_BELOW` 在数值上无法区分
   "取到零行"与"根本没取到"，而两者含义相反。改为：被引用步骤执行失败时条件一律
   不成立（fail-closed）。
2. **幂等重复请求会崩溃**（T12）。第二次 `handle` 拿到同一个已终态任务，Runner 取不到
   租约直接抛 `LifecycleError`。改为终态任务不再执行，按已落库的证据与终态重新投影。
3. **一次故障被归因到两个阶段**（T12）。adapter 超时后证据必然不足，REFLECTION 也被
   标红。改为 REFLECTION 只在"执行本身没问题、却仍证据不足"时标红。

---

## 2. 只读推理（无运行时证据的判断）

1. **"本形状足以支撑 M4 的 PostgreSQL 实现"**：`PlanStore` 与 `EvidenceLedger` 的
   Protocol 面刻意做窄（`{save, load}` 与 `{append, load, get}`），M4 只换实现不动契约。
   这是设计推理——**尚无任何跨进程运行证据**。
2. **"审计表列名与真实 StarRocks 一致"**：表名与 13 个列名来自旧项目 `ivor_aiops` 的
   `_FIELD_ALIASES` / `_EVIDENCE_FIELD_ORDER` 与实际下发 SQL，**未在真实 StarRocks 上核对过**。
3. **"闭集节点白名单挡得住尚未想到的绕过写法"**：它是对当前两条模板实测节点集合的断言。
   闭集在原理上优于黑名单，但"没有想到的写法也会被拒"是推理，不是证明。
4. **"规则顺序使审计原因最具体"**：基于"重编译比对最宽泛，因此排最后"的推理与
   `test_recompile_comparison_runs_last_so_specific_reasons_survive` 一条断言，
   未做穷尽的顺序组合验证。
5. **near-miss 语料的 4 条问法代表真实的误路由风险**：语料由本轮构造，**没有真实用户
   问法样本**支撑其代表性。

---

## 3. 未覆盖

- **未连接任何外部系统**：真实 StarRocks、真实模型 API、PostgreSQL、Docker Compose 全部
  未连接。**对被管运维目标的 E1 调用次数恒为 0**，`_E1_EXECUTION_ENABLED` 保持 `False`。
- **`StepConditionKind` 四个成员只消费了两个**：`ALWAYS` 与 `EVIDENCE_ROW_COUNT_BELOW`
  已被真实闭环消费；**`EVIDENCE_FIELD_ABSENT` 与 `PRIOR_STEP_RESULT_IS` 未被消费、未被
  验证**。下一个里程碑不要误以为四个都已验证。
- **"审批通过后恢复并真的执行副作用步骤"这条路径未验证**，且在 M0–M7 **永远不会走通**
  （Gateway 的 E1 硬闸）。M3 只验证了控制流：无审批时暂停、恢复时重算指纹、漂移即拒绝。
- **跨进程并发、崩溃恢复、lease 过期后的抢占未验证**：三个 In-Memory 实现只有单进程保证。
- **未创建** `tests/integration/`、`docker-compose.yml`、API、Worker、数据库迁移。
- **未验证真实 StarRocks 的语法差异**：全部 AST 结论基于 sqlglot 30.17.0 的 starrocks
  方言实现，不是真实服务端的解析结果。
- **未做性能、并发吞吐与大结果集验证**；`row_limit` 上限 200 的合理性未经真实负载检验。
- **CI 上未运行**：本报告的四条命令是本机 `.venv` 结果；分支尚未推起 CI 结论记录。

---

## 4. 残余风险

即使上述 gate 全绿，仍存在下列风险：

1. **审计表结构未在真实 StarRocks 核对**——表名、13 个列名、`state`/`errorCode` 取值域，
   以及刻意排除的 `clientIp`/`digest`/`stmt` 是否存在。**M6b 首次连接时必须用
   `SHOW CREATE TABLE` 核对并回修**。核对结果可能推翻 `SqlSurface` 的整张列白名单。
2. **`sqlglot` 的 AST 形状随版本可能变化**：节点闭集是对**当前锁定版本 30.17.0** 的断言。
   升级必须重跑 `tests/unit/test_sqlglot_baseline.py`。
3. **三个 In-Memory 实现只有单进程保证**：跨进程原子性、崩溃恢复与隔离级别要到 M4 才可证。
4. **`WorkflowPaused` 用异常表达暂停**是对 M2 Protocol 的一种解释（已获复审裁定接受）。
   若将来 Runner 需要表达更多非终态，应重新评估返回类型而不是继续加异常。
5. **方向判断阈值来自旧项目，未在本项目的真实工作负载上校准**：因此一律带"疑似"，
   且只进渲染说明段、不进 `facts`。
6. **`evidence/` 的纯度由一条 AST 测试承重**：删除 `test_evidence_layer_purity.py` 即等于
   默默取消 `runners → evidence` 这条依赖边的正当性。
7. **重编译比对在原理上无法捕获编译器自身的改动**（它用同一个编译器重算）。这正是 T4 的
   集合等式断言必须独立存在的理由——不要因为"已经有 A36 了"就删掉它。
8. **`application` 是依赖面最宽的一层**（除 `interfaces` 外的全部业务包）。其正当性完全
   由 `test_runtime_bypass.py` 的三条 AST 断言承重；删掉任何一条，绕过就重新可表达。
9. **规则式 IntentInterpreter 的匹配面很窄**：它只认"慢"相关问法。真实用户问法一旦超出
   语料，会落到 `unknown` 并被 Resolver 拒绝——这是 fail-closed 的正确方向，但意味着
   **可用性尚未经真实问法检验**。
10. **本轮有四处"测试绿的理由不对"是靠反证发现的**（§1.5）。同类缺陷在没有做反证的地方
    可能仍然存在——反证只覆盖了各任务承重保护，不是全量变异测试。

---

## 5. 与计划的偏差（逐条记录，供审查）

计划 [docs/plans/M3-starrocks-slow-query.md](../plans/M3-starrocks-slow-query.md) V3 已获批，
以下偏差均在对应提交信息中写明理由：

| # | 偏差 | 理由 |
| --- | --- | --- |
| 1 | `SlowQueryParams` **不含** `environment_id` | 它是执行上下文而非 SQL 参数，已由 `target_fingerprint` 绑定；ARCHITECTURE §6 明确"其他 DTO 不机械复制这三项"。计划 T5 的逐参数敏感性表也不含它 |
| 2 | `min_query_time_ms` 带声明默认值 | count 模板不消费阈值（§9.1 的键集明确），其步骤 `typed_arguments` 里没有该键，还原必须成立 |
| 3 | 上限常量定义在 `contracts/sql_surface.py` | `capabilities` 不得依赖 `planning`，而声明侧与消费侧必须看到同一取值；contracts 是唯一不产生第二真源的落点 |
| 4 | 分层白名单放宽 `planning → capabilities` | 计划 §7.3 与 §4"必须经 `build_plan_step()`"互相冲突；按 `test_effect_single_source` 的明文口径处置，方向无环 |
| 5 | Resolver 落在 T5 而非 T1 | 计划 §17 覆盖度表归 T1，但 T1 文件清单未列它；按"在第一个真正需要它的任务里交付"处置 |
| 6 | 重编译比对**排在 AST 规则之后**（不是"规则 0"） | 排最前会让每条攻击都得到最宽泛的 `RECOMPILE_MISMATCH`（与 §9.4 攻击矩阵不相容），并使规则 1–13 变成不可达死代码 |
| 7 | Runner **不写终态** | 计划同时要求"Runtime 依 verdict 判定终态并写 TaskStore"；Runner 先写会让终态保护堵死降级路径 |
| 8 | 幂等键由 `plan_hash` + `step_id` 派生，不含 `task_id` | 退出标准要求"重复运行产生逐字节相同的 `tool_call_hash`"，而 `task_id` 由存储层生成 |
| 9 | T11 只交付 Runner 侧四个阶段；九阶段端到端断言在 T12 | 另五个阶段由 Runtime 发射，而 Runtime 属 T12 |
| 10 | 九阶段**非严格线性**：LIFECYCLE 在两端各出现一次 | 任务状态迁移确实发生在执行前（→RUNNING）与执行后（→终态）。已改为断言八个单次阶段保序 + LIFECYCLE 括住执行区间 |
| 11 | 未单独创建 `tests/unit/test_error_attribution.py` | 归因断言与阶段顺序断言看同一批事件，拆两个文件会让同一组夹具各写一份 |
| 12 | **`WorkflowRunner` Protocol 由窄签名改为含活输入的签名**（修 M2 契约） | 见下方「首轮打回与修复」 |

### 首轮 Codex 深档验收的打回与修复

首轮送审对象被独立 Codex 判为**打回**，阻断项一条：真实 Runner 未实现
`WorkflowRunner` Protocol，`XiaoweiRuntime` 把 runner 标成 `object` 并用
`type: ignore[attr-defined]` 调它——Runtime→Runner 这条边在类型层完全没有契约，
而 `mypy src` 依然全绿。

**根因**（比"少更新了一处签名"更深一层）：M2 把接口定为 `start(task_id)`，隐含
"Runner 只要 task_id 就能推进任务"。而 ARCHITECTURE §5.6 同时要求 Runner 在恢复时
做漂移检测——判断"暂停期间 policy 或目标是否变了"，必须拿调用方**当下重新解析**出的
`target` 与 `policy_revision` 去比对存储里的那份。这两项按定义不可持久化；若两边都
从存储读，比的是同一个值，检查恒真。又因 Runner 不拥有领域安全规则，它不能自己去
重解析。**因此窄签名与 Runner 自身的职责不相容，M2 契约是错的**，`object` +
`type: ignore` 只是这个矛盾被静音后的表现。按 AGENTS.md「发现 M2 契约错误时先修
M2」处置：修契约，不打兼容补丁。

沿同一路径主动排查后一并修复的同类问题（均非 Codex 指出）：

| 位置 | 问题 | 处置 |
| --- | --- | --- |
| `application/runtime.py` 另 6 处 | `target` / `plan` / `outcome` / `verdict` 同样被擦成 `object`，再用 `type: ignore[arg-type]` 或 `getattr(obj, "attr", default)` 兜底 | 全部改为真实契约类型；`src/` 内 `type: ignore` 归零 |
| `_terminal_status` | `getattr(outcome, "status", INDETERMINATE)`——outcome 形状变了会**静默降级**为 indeterminate 而非报错，是对自己人契约的 fail-soft | 改为直接属性访问 |
| `runners/deterministic.py:resume` | `external_input` 收下后**从不读取**：M2 为恢复定义的跨边界输入是一段被忽略的文本，实际授权走并行的 `approval` 参数——两条通道、一条静默 | 新增 `_verify_external_input`：ref 与实际审批不一致即拒绝 |
| 审批 ref 格式 | `deterministic.py` 与 `approval.py` **各写一遍** `f"{task_id}:{step_id}"`。任一处改了分隔符，暂停发出的 ref 就匹配不上恢复校验的 ref，而两处各自"自洽"，无测试会自然失败 | 抽出唯一拼装点 `approval.approval_ref()` |

证明测试（含反例与边界，均已做 TDD 反证：撤掉保护转红、还原转绿）：

| 测试 | 承重内容 |
| --- | --- |
| `test_the_real_runner_keeps_the_protocol_signature` | 真实 Runner 逐参数等于契约，不只校 fake |
| `test_the_signature_check_rejects_the_old_narrow_runner` | **反例**：M2 窄签名必须不满足当前契约——否则上一条可能只是恰好为真 |
| `test_no_module_silences_a_type_error` | `src/` 内不得出现 `type: ignore`；用 `tokenize` 扫真实注释，避免被 docstring 里的说明自己触发 |
| `test_the_runtime_declares_its_runner_by_protocol` | 正面断言这条边**声明了契约**——只禁静音手段的话，标成 `Any` 同样能全绿 |
| `test_the_application_layer_does_not_guess_collaborator_shapes` | 应用层不得用 `getattr` 三参兜底读协作者属性 |
| 上述三条各自的 detector 自检 | 检测器先证明自己有效，再用它断言代码 |
| `test_resume_rejects_an_approval_ref_for_another_step` | 拿 B 步骤的 ref 恢复 A 步骤必须被拒，且 Gateway 调用次数为 0 |
| `test_resume_rejects_an_approval_decision_without_an_approval` | 声称"已批"却拿不出审批事实，不得当作"无外部输入"放行 |
| `test_a_matching_approval_ref_passes_the_external_input_check` | **边界**：匹配的 ref 必须通过——钉住它没退化成无差别拒绝 |
| `test_user_supplement_cannot_authorise_a_resume` | 不可信外部文本不构成恢复授权 |
| `test_no_module_spells_the_approval_ref_by_hand` | AST 扫 f-string（非文本），禁止第二处拼装 ref |

一并采纳的非阻断建议：`tools.starrocks_fake` 纳入 `_FAKE_MODULES`；本报告自引 SHA
的纪律问题按上方页首说明修正；AGENT_HANDOFF 的提交数表述改为不维护会漂移的计数。

**未采纳**：Codex 给的修复方案二（保持窄接口、把 plan/target/context 经存储传入）。
理由如上——`target` 与 `policy_revision` 的语义就是"现在的值"，从存储读会让漂移检测
恒真，等于把一条安全检查静默改成空操作。

---

## 6. 下一步

1. **Codex 按交接时提供的精确 SHA 做深档验收**：真实 diff 逐行、调用链、安全绕过面、
   测试充分性，以及本报告 §5 的 12 项偏差与「首轮打回与修复」一节是否可接受。
2. 验收通过后由**授权人员**合并（不由本分支自行合并），并把 T0–T14 的逐条提交历史
   归档到 `docs/handoff/archive/`。
3. 之后进入 M4：PostgreSQL TaskStore 的并发、恢复与终态保护。

**本报告不宣称任何部署、canary 或用户验收结论。**
