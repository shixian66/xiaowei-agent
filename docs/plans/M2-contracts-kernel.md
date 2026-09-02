# M2 契约内核详细实施计划（V2，已按 Codex 审核意见修订）

> 状态：**待复审草案**。V1 经 Codex 审核判定「方向对，但不能批准直接进入实现」，10 项阻断全部成立。本版逐条按**根因**修订，不做局部补丁。依据 [DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) §8，仍须经项目负责人与 Codex 审核批准后才能开工。**未批准不实现。**
>
> 依据基线：`main` = `d0971666b835a3728de4088fd67cb447733133e0`。真源为 [ARCHITECTURE.md](../../ARCHITECTURE.md)、[ADR-007](../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md)、[ADR-008](../adr/ADR-008-engineering-and-test-baseline.md)、[DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) §7 M2。与真源冲突一律以真源为准。

## 1. 目标

只实现第一条只读闭环（M3）必需的稳定 DTO、Protocol 和纯函数边界，使 M3 能在**不修改契约**的前提下完成 `IntentDraft → Resolver → PlanCompiler → Runner → StepAdmission → ToolGateway → Evidence → Outcome`。

判定标准三条：

1. 违反安全边界的构造在**契约层面不可表达**，而不是靠调用方自觉。
2. [ADR-007 D7](../adr/ADR-007-first-capabilities-execution-context-and-live-call-authorization.md) 的每条必须承重断言都有先红后绿的测试。
3. M4 换 PostgreSQL、M3 写 PlanCompiler 时，都不需要改这里冻结的形状。

## 2. 架构取向

五条贯穿全部任务的取向。V2 新增第 4、5 条，均由审核意见的根因导出。

1. **不可表达优于运行时校验。** 能用类型、`Literal`、闭集枚举、`extra="forbid"` 表达的约束，不写成 `if`。
2. **派生优于存储。** 可确定性推出的值不作字段持久化，避免"存了但不同步"的第二真源。
3. **单向依赖。** `contracts/` 是叶子，不 import 任何其他 `xiaowei_agent` 模块（含 `log`）；其余模块只依赖 `contracts/` 与标准库。由源码扫描测试承重。
4. **深不可变。** Pydantic 的 `frozen=True` 只挡属性重绑定，挡不住内部 `dict` 被原地改写。所有映射字段统一经 `FrozenMap` 复制并包装为 `MappingProxyType`；模块级映射常量同样包装。**否则"计划已冻结"是假的**——hash 算完后仍可改 `typed_arguments`。
5. **覆盖完备性由结构测试承重，而不是由人记得。** 凡"某个 DTO 的全部字段都必须进入某个 hash"这类要求，一律用「字段名 → hash 键」的显式映射表实现，并断言 `set(映射表) == set(model_fields)`。将来给 DTO 加字段却忘了加进 hash，测试立刻转红。V1 的 `plan_hash` 漏掉 `capability_id`/`capability_version`/`budget`，正是缺这一层导致的。

## 3. 技术栈

Python 3.11、Pydantic v2（已在 `uv.lock`）、标准库 `hashlib` / `json` / `unicodedata` / `enum.StrEnum` / `types.MappingProxyType`。**M2 不新增任何第三方依赖**——不引入 `sqlglot`、数据库驱动、FastAPI 或模型 SDK。

## 4. 全局约束

每个任务的隐含验收项，逐条抄自真源：

- 验证命令恒为四条，逐字不变：`python -m pytest -q`、`python -m pytest -m security -q`、`ruff check .`、`mypy src`。
- `mypy` 为 `strict = true`；`ruff` 选中 `E,F,W,I,N,UP,B,S,ANN,RUF`，行宽 100。
- 执行上下文字段名恒为 `tenant_id`、`actor`、`environment_id`，无别名（ADR-007 D3）。
- 安全测试置于 `tests/security/` 并 `pytestmark = pytest.mark.security`。
- **M2 不发起任何网络调用**；`pytest-socket` 已阻断 AF_INET/AF_INET6。
- **M0–M7 全程禁止 E1**，含非生产环境（ADR-007 D7）。不实现任何可触达被管运维目标的 adapter。
- 不得在代码、测试、文档、fixture 中出现 secret、token、密码、连接串或真实生产数据。
- 不为 M3 之后的场景提前创建空模块或占位接口。
- 不修改 `.github/workflows/ci.yml`。若确需修改，必须同步更新 `tests/security/test_workflow_policy.py` 的整文件 SHA-256 常量并单独人工审查。
- **`src/` 内不使用 `assert` 表达运行时不变量**（`python -O` 会移除）；一律显式 `raise`。

## 5. 对 V1 审核意见的处置

十项全部成立，逐条给出根因、修复与证明测试。**"证明测试"列的测试若被删除或放宽，对应保护即失效**，因此它们同时是回归防线。

| # | 根因 | 修复（非局部补丁） | 证明测试 |
| --- | --- | --- | --- |
| 1 | 照抄 §7.1 的计划级 canonical 形状，却又给 `PlanStep` 加了 step 级 capability 字段，两套设计相撞 | **移除 `PlanStep.capability_id` / `capability_version`**，`ExecutionPlan` 绑定单 capability；hash 改由「字段→键」映射表生成，并**补入此前同样遗漏的 `budget`** | `test_plan_hash_covers_every_execution_plan_field`、`test_step_hash_covers_every_plan_step_field`、`test_changing_budget_changes_plan_hash` |
| 2 | 准入凭证绑定的是**步骤身份**，而 `ToolCall` 承载的是**调用内容**；身份不等于内容 | `AdmissionCertificate` 增 `tool_call_hash`；新增 `compute_tool_call_hash()` 覆盖 `ToolCall` 全部六个字段；Gateway 重算比对 | `test_admission_with_tampered_args_is_refused`、`test_tool_call_hash_covers_every_field` |
| 3 | 误以为 `Mapping` 注解等于只读；`frozen=True` 只挡属性重绑定 | 新增 `FrozenMap` / `FrozenStrMap`：先 `dict()` 复制再 `MappingProxyType` 包装。**全部**映射字段与模块级映射常量统一使用 | `test_mapping_field_is_not_mutable`、`test_mutating_the_original_input_does_not_affect_the_contract` |
| 4 | 用"测试替身归 tests/"的通用习惯覆盖了 DEVELOPMENT_PLAN §7 M2 的明文落点 | fake 迁入 `tools/fake.py`、`persistence/fake.py`、`runners/fake.py`，各自声明 `IS_FAKE`；`tests/fakes/` 只留夹具 | `test_every_fake_module_declares_is_fake`、`test_no_production_module_imports_a_fake` |
| 5 | 循环依赖源于 #1 的 step 级 capability 字段 | #1 修复后循环消失。任务重排为**严格线性 T1→T11**，删除"编号≠实现顺序"补丁；**T1 一次性定义全部 18 个共享枚举** | `test_all_shared_enums_are_defined_in_one_module` |
| 6 | `fencing_token` 设成 `Optional` 以容纳"尚无租约"的迁移，导致"不传即跳过" | 改为**闭合规则**：有 live lease 必须带正确 token；无 live lease 不得带 token。同路径另修：幂等键按 `(tenant_id, environment_id, key)` 作用域、同键不同请求拒绝、终态任务不可 acquire lease、非持有者/过期后不可续租 | `test_write_without_token_under_live_lease_is_rejected` 等 8 条 |
| 7 | 把"未知 policy revision"当成必须有 ApprovalGate 才能测，实际它是纯函数可测的 | 新增 `governance/binding.py`：`PolicySnapshot` + `verify_policy_revision` + `verify_approval_binding`；`ApprovalRequest` 补 `policy_revision` | `test_plan_with_unknown_policy_revision_is_rejected` 等 5 条 |
| 8 | 先 `set()` 去重再规范化，顺序反了 | `ResolvedTarget` 构造时即 NFC 规范化、拒空、**拒绝规范化后碰撞**。同路径另修：`canonical_json` 的映射键此前也会 NFC 静默覆盖，改为碰撞即抛错 | `test_nfc_colliding_resource_ids_are_rejected`、`test_canonical_json_rejects_key_collision_after_nfc` |
| 9 | 把说明性片段当成了 TDD 测试 | 全部测试片段改为完整可运行代码（含 import 与 fixture）；固定向量改为**直接写出规范 JSON 字节串**再取摘要，消除"运行后回填"的占位符 | 计划内全部测试可原样落盘执行 |
| 10 | 按"整个 src"写禁令，而 AGENTS.md 的规则是"**领域层**不得持有客户端" | 扫描范围收敛为领域层白名单，显式排除 `tools/`（adapter 本职）与 `persistence/`（M4 起需 SQLAlchemy），并在测试内注明里程碑依据。同路径另修：`test_effect_source_is_single` 同样过宽，改为放行唯一构造器 `build_plan_step()` | `test_domain_layer_has_no_third_party_client_import`、`test_only_effect_module_constructs_classification` |

### 五项决策的最终形态（含审核裁定）

- **D1 采纳（有条件已满足）**：`effect_class` 与 `condition` 进入 `plan_hash`，且 #1 的覆盖缺口已修复，`budget` 一并纳入。`PLAN_SCHEMA_VERSION = 1`（首次定义，无存量迁移）。**写 ADR-009。**
- **D2 采纳，扫描口径按裁定修正**：保留「派生 + 准入前重算」，并新增**唯一构造器** `build_plan_step()`。源码扫描不再禁止"任何模块赋值分类字段"，而是「除 `capabilities/effect.py` 外，任何模块不得构造带分类字段的 `PlanStep`」——M3 的 PlanCompiler 调用 `build_plan_step()` 即合规，不会被卡住。
- **D3 采纳，`prior_step_status_is` 按裁定修正**：新增独立的 `StepResultStatus`（`OK`/`FAILED`/`TIMEOUT`/`SKIPPED`），不再复用 `TaskStatus`。任务状态与步骤结果是两个层级，混用会让"步骤失败"和"任务失败"在条件里不可区分。
- **D4 采纳**：`plan_hash` / `target_fingerprint` 不作 `ExecutionPlan` 字段，绑定值存于 `ApprovalRequest`。
- **D5 采纳，按裁定补 call digest**：`AdmissionCertificate` 增 `tool_call_hash`（见 #2）。
- **`assert` → 显式 `raise`**：采纳，见全局约束。
- **`contracts` 依赖 `log`：不白名单化**，按裁定下沉。新增 `src/xiaowei_agent/redaction.py`（与 `log.py`、`trace.py`、`config.py` 平级的叶子模块），`scrub_text` / `redact` 实现迁入；`log.py` 改为从该模块 import 并**原样 re-export**，因此 M1 的 389 行脱敏测试（import 自 `xiaowei_agent.log`）无需改动即应继续通过——这是本次重构的回归证据。
- **ADR-009：采纳。** D1/D3/D4/D5 都在冻结 hash canonicalization、审批绑定与工具准入形状，符合 `ARCHITECTURE.md` §15 的 ADR 门槛。
- **单分支单 PR：采纳。** 一条 `claude/m2-contract-kernel` 分支，11 个任务作为 TDD 提交与审查 checkpoint，不拆成 11 个 PR。理由与审核一致：这些 DTO 强耦合，按 PR 硬拆会人为制造循环依赖。

## 6. 文件结构

```text
src/xiaowei_agent/
├── redaction.py                # 新增叶子模块：scrub_text / redact（由 log.py 反向复用）
├── log.py                      # 修改：改为从 redaction 导入并 re-export，对外签名不变
├── contracts/                  # 叶子层：不 import 任何其他 xiaowei_agent 模块
│   ├── __init__.py             # 唯一公开导入面
│   ├── base.py                 # Contract 基类、JsonScalar、StrictStr、FrozenMap
│   ├── enums.py                # 全部 18 个共享闭集枚举，一次性定义
│   ├── errors.py               # AgentError
│   ├── external.py             # ExternalContent
│   ├── request.py              # RequestEnvelope / RequestContext
│   ├── intent.py               # IntentDraft
│   ├── capability.py           # OperationSpec / CapabilitySpec / CapabilitySnapshot
│   ├── candidates.py           # Candidate / Rejection / CandidateSet
│   ├── target.py               # ResolvedTarget（含 NFC 规范化与碰撞拒绝）
│   ├── plan.py                 # StepCondition / PlanStep / PlanBudget / ExecutionPlan
│   ├── policy.py               # PolicyDecision / PolicySnapshot
│   ├── approval.py             # ApprovalRequest / AdmissionCertificate
│   ├── tool.py                 # ToolCall / ToolResult（构造凭证）
│   ├── evidence.py             # EvidenceEnvelope
│   ├── answerability.py        # AnswerabilityVerdict / MissingItem
│   ├── task.py                 # TaskRecord / TransitionResult / LeaseGrant / TaskOutcome
│   ├── render.py               # RenderSection / RenderPayload
│   ├── external_input.py       # ExternalInput
│   └── trace_events.py         # TraceEvent
├── capabilities/
│   ├── effect.py               # derive_effect / build_plan_step / verify_plan_effects
│   └── resolver.py             # CapabilityResolver Protocol
├── planning/
│   └── canonical.py            # canonical_json 与三个 hash（plan / target / tool_call）
├── governance/
│   └── binding.py              # verify_policy_revision / verify_approval_binding
├── tools/
│   ├── adapter.py              # AdapterResponse / ToolAdapter Protocol
│   ├── gateway.py              # ToolGateway Protocol / DeterministicToolGateway / E1 硬闸
│   └── fake.py                 # RecordingToolAdapter（IS_FAKE）
├── persistence/
│   ├── store.py                # TaskStore Protocol / Clock / TaskNotFound / IdempotencyConflict
│   └── fake.py                 # InMemoryTaskStore（IS_FAKE）
├── runners/
│   ├── runner.py               # WorkflowRunner Protocol
│   └── fake.py                 # ScriptedRunner（IS_FAKE）
└── observability/
    └── sink.py                 # TraceSink Protocol

tests/
├── conftest.py                 # 修改：新增共享 fixture（context / envelope / snapshot / clock / store / gateway）
├── fakes/                      # 仅测试夹具，不再放可复用 fake 实现
│   ├── clock.py                # ManualClock
│   └── fixtures.py             # SNAPSHOT、FIXTURE_PLAN、FIXTURE_TARGET
├── unit/
├── contract/                   # 新建
└── security/
```

**为何 `redaction.py` 放顶层而非 `contracts/redaction.py`**：脱敏是通用工具而非跨模块契约，放进 `contracts/` 会让契约包承担非契约职责。顶层叶子模块同时满足"`contracts` 零对内依赖"与"脱敏规则单一真源"两个要求。

**为何仍保留 `tests/fakes/`**：`ManualClock` 与固定夹具只服务测试，不需要被生产代码复用；而 adapter / store / runner 的 fake 被 DEVELOPMENT_PLAN §7 M2 明文要求放在对应业务包下。

## 7. 任务拆分

一条分支 `claude/m2-contract-kernel`，11 个任务**严格线性**，每个任务一个 TDD 提交，同时是审查 checkpoint。每个任务结束都必须四条命令全绿才进入下一个。

---

### T1：脱敏规则下沉，`contracts` 得以零对内依赖

**根因背景**：V1 让 `contracts/trace_events.py` import `xiaowei_agent.log` 以复用脱敏。审核裁定不白名单化。真正的问题不是"谁 import 谁"，而是**脱敏实现的位置错了**——它是通用工具，却被放在 logging 模块里。

**文件**：
- 创建：`src/xiaowei_agent/redaction.py`
- 修改：`src/xiaowei_agent/log.py`
- 测试：`tests/unit/test_redaction_module.py`

**接口**：
- 产出：`scrub_text(text: str) -> str`、`redact(value: object, *, _depth: int = 0) -> JsonValue`、`JsonValue`、`REDACTED`

- [ ] **步骤 1：先确认 M1 现状为绿，作为重构的基准线**

```bash
python -m pytest tests/security/test_redaction.py tests/unit/test_log.py -q
```
预期：PASS。记录尾部输出——重构后必须逐字相同。

- [ ] **步骤 2：迁移实现**

把 `log.py` 中的 `JsonValue`、`REDACTED`、`_MAX_DEPTH`、`_KEY_WORDS`、`_KEY_RE`、`_TEXT_PAIR_RE`、`_AUTH_SCHEME_RE`、`_VALUE_SHAPE_RE`、`_safe_str`、`scrub_text`、`redact` **原样**移入 `src/xiaowei_agent/redaction.py`（不改一个字符的正则或逻辑），文件头 docstring 说明：

```python
"""脱敏规则的单一真源。

从 ``log.py`` 下沉为顶层叶子模块，使 ``contracts`` 可以复用同一套规则而不
依赖 logging。``log.py`` 反向 import 并 re-export，因此对外签名不变，M1 的
脱敏回归测试无需改动。

**过度脱敏优于泄漏**：未加引号的敏感值一律脱敏到分隔符或行尾。
"""
```

`log.py` 顶部改为：

```python
from xiaowei_agent.redaction import REDACTED, JsonValue, redact, scrub_text

__all__ = ["JsonFormatter", "RedactingFilter", "configure_logging",
           "REDACTED", "JsonValue", "redact", "scrub_text"]
```

保留 `_RESERVED`、`RedactingFilter`、`JsonFormatter`、`_install_filter`、`configure_logging` 在 `log.py`。

- [ ] **步骤 3：确认 M1 回归测试逐字不变仍全绿**

```bash
python -m pytest tests/security/test_redaction.py tests/unit/test_log.py -q
```
预期：与步骤 1 完全相同的输出。**这是本任务唯一的成功判据**：若需要修改 M1 任何一行测试，说明这不是纯迁移，必须停下重做。

- [ ] **步骤 4：补一条证明单一真源的测试**

`tests/unit/test_redaction_module.py`：

```python
"""脱敏规则只有一份实现。"""

from xiaowei_agent import log, redaction


def test_log_reexports_the_same_objects() -> None:
    assert log.scrub_text is redaction.scrub_text
    assert log.redact is redaction.redact
    assert log.REDACTED is redaction.REDACTED


def test_redaction_module_has_no_internal_dependency() -> None:
    """它是叶子：不 import 任何 xiaowei_agent 模块，否则 contracts 复用会再造依赖。"""
    import ast
    from pathlib import Path

    source = Path(redaction.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        assert not (module or "").startswith("xiaowei_agent")
```

- [ ] **步骤 5：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/redaction.py src/xiaowei_agent/log.py tests/unit/test_redaction_module.py
git commit -m "refactor(redaction): 脱敏规则下沉为叶子模块，log 反向复用，对外签名不变"
```

---

### T2：契约基座、深不可变映射、全部共享枚举

**根因背景**：V1 的两处根因在此一并解决——`Mapping` 注解不等于只读（审核 #3）；枚举按任务增量定义导致被使用时尚未定义（审核 #5）。

**文件**：
- 创建：`src/xiaowei_agent/contracts/__init__.py`、`base.py`、`enums.py`、`errors.py`、`external.py`
- 测试：`tests/unit/test_contracts_base.py`、`tests/security/test_deep_immutability.py`、`tests/security/test_external_content.py`

**接口**：
- 产出：`Contract`、`JsonScalar`、`StrictStr`、`FrozenMap`、`FrozenStrMap`、`frozen_map()`、`AgentError`、`ExternalContent`、`content_digest()`，以及 **18 个共享枚举**：`ExternalSource` `TrustLevel` `EffectClass` `ErrorCategory` `Channel` `IntentSource` `RiskLevel` `ApprovalState` `StepConditionKind` `StepResultStatus` `TaskStatus` `TransitionRejection` `AdapterStatus` `ToolCallStatus` `PipelineStage` `StageOutcome` `ExternalInputKind` `BindingRejection`

- [ ] **步骤 1：写失败测试 —— 深不可变**

`tests/security/test_deep_immutability.py`：

```python
"""映射字段必须深不可变。

Pydantic 的 frozen=True 只挡属性重绑定，挡不住内部 dict 被原地改写。
若不解决，"计划已冻结"就是假的——plan_hash 算完之后仍可改 typed_arguments。
"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import Contract, FrozenMap

pytestmark = pytest.mark.security


class _Sample(Contract):
    data: FrozenMap


def test_mapping_field_cannot_be_mutated_in_place() -> None:
    sample = _Sample(data={"a": 1})
    with pytest.raises(TypeError):
        sample.data["a"] = 2  # type: ignore[index]
    with pytest.raises(TypeError):
        del sample.data["a"]  # type: ignore[attr-defined]


def test_mutating_the_original_input_does_not_affect_the_contract() -> None:
    """必须是复制后包装，而不是给外部 dict 套一层视图。"""
    source = {"a": 1}
    sample = _Sample(data=source)
    source["a"] = 999
    source["b"] = 2
    assert dict(sample.data) == {"a": 1}


def test_attribute_rebinding_is_still_blocked() -> None:
    sample = _Sample(data={"a": 1})
    with pytest.raises(ValidationError):
        sample.data = {"b": 2}  # type: ignore[misc]


def test_nested_mapping_values_are_rejected_by_scalar_maps() -> None:
    """标量映射不接受嵌套容器；嵌套是夹带任意载荷的通道。"""
    with pytest.raises(ValidationError):
        _Sample(data={"a": {"nested": 1}})
```

`tests/security/test_external_content.py`：

```python
"""外部文本恒为不可信，摘要不可伪造。"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import ExternalContent, ExternalSource, TrustLevel

pytestmark = pytest.mark.security

_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)
_HELLO_SHA256 = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": ExternalSource.TOOL,
        "trust": TrustLevel.UNTRUSTED,
        "content": "hello",
        "digest": _HELLO_SHA256,
        "captured_at": _AT,
    }
    return base | overrides


def test_capture_computes_digest_and_pins_trust() -> None:
    got = ExternalContent.capture(source=ExternalSource.TOOL, content="hello", captured_at=_AT)
    assert got.digest == _HELLO_SHA256
    assert got.trust is TrustLevel.UNTRUSTED


def test_trust_cannot_be_set_to_anything_else() -> None:
    with pytest.raises(ValidationError):
        ExternalContent(**_payload(trust="trusted"))


def test_forged_digest_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExternalContent(**_payload(digest="0" * 64))


def test_undeclared_field_is_rejected() -> None:
    """外部文本不得夹带 policy / 权限 / 计划字段。"""
    with pytest.raises(ValidationError):
        ExternalContent(**_payload(policy_revision="r1"))


def test_capture_rejects_undeclared_keyword() -> None:
    """capture 是关键字受限的类方法，多传参数抛 TypeError（而非 ValidationError）。"""
    with pytest.raises(TypeError):
        ExternalContent.capture(  # type: ignore[call-arg]
            source=ExternalSource.WEB, content="x", captured_at=_AT, policy_revision="r1",
        )
```

> **V1 缺陷修正说明**：V1 此处写的是 `ExternalContent.capture(..., policy_revision=...)` 期待 `ValidationError`，实际会抛 `TypeError`（审核 #9）。上面拆成两条测试，分别覆盖模型层与类方法层，异常类型各自正确。

- [ ] **步骤 2：确认失败**

`python -m pytest tests/security/test_deep_immutability.py -q` → `ModuleNotFoundError: No module named 'xiaowei_agent.contracts'`

- [ ] **步骤 3：实现 `base.py`**

```python
"""全部跨模块契约的共同基座。

``frozen=True`` 只挡属性重绑定。映射字段一律用 ``FrozenMap``：先 ``dict()``
复制（切断与调用方原对象的联系），再用 ``MappingProxyType`` 包装（挡住原地
改写）。两步缺一不可——只包装不复制等于给外部 dict 套视图，外部一改内部就变。

**已知取舍**：``MappingProxyType`` 不可哈希，因此携带映射字段的契约实例不可
作为 dict 键或放入 set。本项目不依赖契约的可哈希性，比较一律用 ``==``。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict

JsonScalar: TypeAlias = None | bool | int | float | str


def _strict_str(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("must not be empty or padded with whitespace")
    return value


StrictStr = Annotated[str, AfterValidator(_strict_str)]


def frozen_map(value: Mapping[str, object]) -> Mapping[str, object]:
    """复制并包装为只读映射。"""
    return MappingProxyType(dict(value))


FrozenMap = Annotated[Mapping[StrictStr, JsonScalar], AfterValidator(frozen_map)]
FrozenStrMap = Annotated[Mapping[StrictStr, str], AfterValidator(frozen_map)]


class Contract(BaseModel):
    """不可变、拒绝未声明字段的契约基类。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
```

- [ ] **步骤 4：实现 `enums.py` —— 一次性定义全部 18 个共享枚举**

```python
"""跨契约的闭集枚举，全项目共享的唯一定义处。

**一次性全量定义**是刻意的：V1 按任务增量新增枚举，导致下游任务引用了尚未
定义的成员。枚举没有实现成本，集中定义可消除整类顺序错误。

**闭集本身就是安全边界**：``EffectClass`` 不设 ``UNKNOWN``，因此"分类未知"
在解析层即失败，不存在一个可被当作只读放行的取值（ADR-007 D7 规则 1）。
"""

from enum import StrEnum


class ExternalSource(StrEnum):
    TOOL = "tool"
    KNOWLEDGE = "knowledge"
    WEB = "web"
    USER = "user"
    LOG = "log"


class TrustLevel(StrEnum):
    """只有一个成员：外部内容永远不可信。"""

    UNTRUSTED = "untrusted"


class EffectClass(StrEnum):
    """不设 UNKNOWN：未声明即校验失败。"""

    READ = "read"
    MUTATE_TARGET = "mutate_target"


class ErrorCategory(StrEnum):
    VALIDATION = "validation"
    POLICY = "policy"
    APPROVAL = "approval"
    TARGET = "target"
    UPSTREAM = "upstream"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    INTERNAL = "internal"


class Channel(StrEnum):
    API = "api"
    CLI = "cli"
    WEB = "web"
    FEISHU = "feishu"


class IntentSource(StrEnum):
    MODEL = "model"
    USER = "user"
    FAKE = "fake"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalState(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class StepConditionKind(StrEnum):
    ALWAYS = "always"
    EVIDENCE_FIELD_ABSENT = "evidence_field_absent"
    EVIDENCE_ROW_COUNT_BELOW = "evidence_row_count_below"
    PRIOR_STEP_RESULT_IS = "prior_step_result_is"


class StepResultStatus(StrEnum):
    """**步骤**结果，与 TaskStatus（**任务**状态）分属两个层级，不得混用。

    V1 让 ``prior_step_status_is`` 引用 TaskStatus，会使"某个步骤失败"与
    "整个任务失败"在条件中不可区分（审核 D3 裁定）。
    """

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELED = "canceled"
    INDETERMINATE = "indeterminate"


class TransitionRejection(StrEnum):
    VERSION_MISMATCH = "version_mismatch"
    ILLEGAL_TRANSITION = "illegal_transition"
    TERMINAL_PROTECTED = "terminal_protected"
    STALE_FENCING_TOKEN = "stale_fencing_token"
    LEASE_NOT_HELD = "lease_not_held"


class AdapterStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"


class ToolCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    INDETERMINATE = "indeterminate"


class PipelineStage(StrEnum):
    """与 ARCHITECTURE §13.1 的错误归因阶段逐一对应。"""

    INTENT = "intent"
    RESOLVER = "resolver"
    PLANNER = "planner"
    ADMISSION = "admission"
    GATEWAY = "gateway"
    EVIDENCE = "evidence"
    REFLECTION = "reflection"
    RENDERING = "rendering"
    LIFECYCLE = "lifecycle"


class StageOutcome(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExternalInputKind(StrEnum):
    APPROVAL_DECISION = "approval_decision"
    USER_SUPPLEMENT = "user_supplement"


class BindingRejection(StrEnum):
    PLAN_DRIFT = "plan_drift"
    TARGET_DRIFT = "target_drift"
    POLICY_REVISION_DRIFT = "policy_revision_drift"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_NOT_GRANTED = "approval_not_granted"
```

补一条防回归测试 `tests/unit/test_contracts_base.py::test_all_shared_enums_are_defined_in_one_module`：

```python
def test_all_shared_enums_are_defined_in_one_module() -> None:
    """共享枚举只能定义在 contracts.enums；散落定义会造成同名不同义。"""
    import ast
    import inspect
    from pathlib import Path

    from xiaowei_agent import contracts

    root = Path(inspect.getfile(contracts)).parent
    offenders: list[str] = []
    for path in root.glob("*.py"):
        if path.name == "enums.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
                if "StrEnum" in bases or "Enum" in bases:
                    offenders.append(f"{path.name}:{node.name}")
    assert not offenders, f"枚举必须定义在 enums.py: {offenders}"
```

- [ ] **步骤 5：实现 `errors.py` 与 `external.py`**

```python
# contracts/errors.py
"""结构化错误模型。

**不含自由文本消息**：``message_key`` 是 i18n 键，``cause_ref`` 指向
``ExternalContent.digest`` 或 trace 事件 id。第三方错误原文只能经
``ExternalContent`` 承载，再由确定性 mapper 归类，不作为控制信号。
"""

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import ErrorCategory


class AgentError(Contract):
    code: StrictStr
    category: ErrorCategory
    retryable: bool
    message_key: StrictStr
    cause_ref: str | None = None
```

```python
# contracts/external.py
"""外部文本的统一包装。

``trust`` 是 ``Literal[TrustLevel.UNTRUSTED]``，"把外部文本标为可信"在契约层
不可表达。``digest`` 在校验阶段重算比对，伪造摘要即失败。
"""

import datetime as _dt
import hashlib
from typing import Literal, Self

from pydantic import model_validator

from xiaowei_agent.contracts.base import Contract
from xiaowei_agent.contracts.enums import ExternalSource, TrustLevel


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ExternalContent(Contract):
    source: ExternalSource
    trust: Literal[TrustLevel.UNTRUSTED] = TrustLevel.UNTRUSTED
    content: str
    digest: str
    captured_at: _dt.datetime

    @model_validator(mode="after")
    def _digest_must_match(self) -> Self:
        if self.digest != content_digest(self.content):
            raise ValueError("digest does not match content")
        return self

    @classmethod
    def capture(
        cls, *, source: ExternalSource, content: str, captured_at: _dt.datetime
    ) -> Self:
        """唯一推荐构造入口：自动计算摘要，调用方无从伪造。"""
        return cls(
            source=source, content=content,
            digest=content_digest(content), captured_at=captured_at,
        )
```

- [ ] **步骤 6：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/contracts tests/unit/test_contracts_base.py tests/security/test_deep_immutability.py tests/security/test_external_content.py
git commit -m "feat(contracts): 深不可变基座、18 个共享枚举、错误模型与不可信外部文本"
```

---

### T3：执行上下文、意图草案与能力声明

**文件**：
- 创建：`src/xiaowei_agent/contracts/request.py`、`intent.py`、`capability.py`、`candidates.py`
- 创建：`tests/fakes/fixtures.py`（`SNAPSHOT`）
- 修改：`pyproject.toml` 增加 `pythonpath = ["."]`，使 `tests.fakes`（隐式命名空间包）可导入
- 测试：`tests/unit/test_capability_spec.py`、`tests/security/test_intent_pollution.py`

**接口**：
- 产出：`RequestEnvelope`、`RequestContext`、`TraceId`、`IntentDraft`、`OperationSpec`、`CapabilitySpec`、`CapabilitySnapshot`、`Candidate`、`Rejection`、`CandidateSet`

- [ ] **步骤 1：写失败测试**

`tests/security/test_intent_pollution.py`：

```python
"""IntentDraft 是不可信度最高的 DTO：不得携带任何执行决策字段。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import IntentDraft, IntentSource, RequestContext

pytestmark = pytest.mark.security

_FORBIDDEN = [
    "capability_id", "capability_version", "operation", "side_effect", "effect_class",
    "sql", "target", "resource_ids", "policy_revision", "approved", "plan_hash",
    "tool_call_hash", "environment_id", "tenant_id", "actor",
]


def _draft(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent": "diagnose_slow_query", "slots": {}, "missing": (),
        "confidence": 0.9, "source": IntentSource.MODEL,
    }
    return base | overrides


@pytest.mark.parametrize("field", _FORBIDDEN)
def test_execution_decision_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        IntentDraft(**_draft(**{field: "x"}))


def test_slots_reject_nested_structures() -> None:
    with pytest.raises(ValidationError):
        IntentDraft(**_draft(slots={"db": {"nested": "value"}}))


def test_slots_are_deeply_immutable() -> None:
    draft = IntentDraft(**_draft(slots={"db": "prod"}))
    with pytest.raises(TypeError):
        draft.slots["db"] = "other"  # type: ignore[index]


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_confidence_outside_unit_interval_is_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        IntentDraft(**_draft(confidence=bad))


def _context(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant_id": "dev-local", "actor": "alice", "environment_id": "dev",
        "trace_id": "0" * 32, "policy_revision": "policy-2026-09-01",
    }
    return base | overrides


@pytest.mark.parametrize(
    "missing",
    ["tenant_id", "actor", "environment_id", "trace_id", "policy_revision"],
)
def test_request_context_requires_all_five_fields(missing: str) -> None:
    payload = _context()
    del payload[missing]
    with pytest.raises(ValidationError):
        RequestContext(**payload)


@pytest.mark.parametrize("bad", ["", "  ", "ZZZ", "0" * 31, "0" * 33, "0" * 31 + "G"])
def test_trace_id_must_be_32_lowercase_hex(bad: str) -> None:
    with pytest.raises(ValidationError):
        RequestContext(**_context(trace_id=bad))
```

`tests/unit/test_capability_spec.py`：

```python
"""能力声明是 E1 分类的唯一来源，声明层就不许自相矛盾。"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    CapabilitySpec, CapabilitySnapshot, EffectClass, OperationSpec,
)


def _op(**overrides: object) -> OperationSpec:
    base: dict[str, object] = {
        "operation": "list_slow_queries", "effect_class": EffectClass.READ,
        "side_effect": False, "argument_schema_ref": "schema.v1",
    }
    return OperationSpec(**(base | overrides))


def test_write_declared_as_readonly_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _op(effect_class=EffectClass.MUTATE_TARGET, side_effect=False)


def test_read_declared_as_side_effecting_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _op(effect_class=EffectClass.READ, side_effect=True)


def test_capability_requires_at_least_one_operation() -> None:
    with pytest.raises(ValidationError):
        CapabilitySpec(
            capability_id="c", version="1.0.0", domain="d", operations=(),
            policy_profile="p", evidence_contract="e", eval_ref="v",
        )


def test_duplicate_operation_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CapabilitySpec(
            capability_id="c", version="1.0.0", domain="d",
            operations=(_op(), _op()),
            policy_profile="p", evidence_contract="e", eval_ref="v",
        )


def test_snapshot_rejects_duplicate_capability_version_pair() -> None:
    """同一 (capability_id, version) 出现两次会让分类派生结果不确定。"""
    spec = CapabilitySpec(
        capability_id="c", version="1.0.0", domain="d", operations=(_op(),),
        policy_profile="p", evidence_contract="e", eval_ref="v",
    )
    with pytest.raises(ValidationError):
        CapabilitySnapshot(snapshot_id="s", specs=(spec, spec))
```

> `test_snapshot_rejects_duplicate_capability_version_pair` 是 V1 遗漏的边界：`derive_effect` 遍历 `specs` 取第一个匹配项，若快照里有重复的 `(capability_id, version)` 且声明不同，分类结果就取决于顺序。**在快照层拒绝**比在派生层容错更 fail-closed。

- [ ] **步骤 2：确认失败**

`python -m pytest tests/security/test_intent_pollution.py -q` → `ImportError: cannot import name 'IntentDraft'`

- [ ] **步骤 3：实现 `request.py` 与 `intent.py`**

```python
# contracts/request.py
"""入口信封与执行上下文。

``RequestContext`` 五项全必填——环境无法解析出唯一值时 fail-closed，不取默认
环境（ADR-007 D2）。模块边界显式传递它，不从全局变量读取。
"""

from typing import Annotated

from pydantic import Field, StringConstraints

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import Channel

TraceId = Annotated[str, StringConstraints(pattern=r"\A[0-9a-f]{32}\Z")]


class RequestEnvelope(Contract):
    request_id: StrictStr
    tenant_id: StrictStr
    actor: StrictStr
    channel: Channel
    text: str = Field(max_length=8192)
    idempotency_key: StrictStr
    environment_id: str | None = None


class RequestContext(Contract):
    tenant_id: StrictStr
    actor: StrictStr
    environment_id: StrictStr
    trace_id: TraceId
    policy_revision: StrictStr
```

```python
# contracts/intent.py
"""模型产出的意图草案。

字段集刻意只有五项且 ``extra="forbid"``，模型无法通过多写字段影响 capability、
目标、SQL、权限或审批（ARCHITECTURE §4.1）。槽位取值限定为字符串标量，
嵌套结构会成为夹带任意载荷的通道。
"""

from pydantic import Field

from xiaowei_agent.contracts.base import Contract, FrozenStrMap, StrictStr
from xiaowei_agent.contracts.enums import IntentSource


class IntentDraft(Contract):
    intent: StrictStr
    slots: FrozenStrMap
    missing: tuple[StrictStr, ...]
    confidence: float = Field(ge=0.0, le=1.0)
    source: IntentSource
```

- [ ] **步骤 4：实现 `capability.py` 与 `candidates.py`**

```python
# contracts/capability.py
"""能力声明。

``CapabilitySpec`` 是 side_effect 与 effect_class 的唯一确定性来源（ADR-007 D7）。
``OperationSpec`` 在声明层就要求两者等价，自相矛盾的声明在注册时即被拒绝。
``CapabilitySnapshot`` 拒绝重复的 (capability_id, version)——否则分类派生结果
会依赖遍历顺序。
"""

from typing import Self

from pydantic import model_validator

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import EffectClass


class OperationSpec(Contract):
    operation: StrictStr
    effect_class: EffectClass
    side_effect: bool
    argument_schema_ref: StrictStr

    @model_validator(mode="after")
    def _side_effect_matches_effect_class(self) -> Self:
        if self.side_effect != (self.effect_class is not EffectClass.READ):
            raise ValueError("side_effect must equal (effect_class is not READ)")
        return self


class CapabilitySpec(Contract):
    capability_id: StrictStr
    version: StrictStr
    domain: StrictStr
    operations: tuple[OperationSpec, ...]
    policy_profile: StrictStr
    evidence_contract: StrictStr
    eval_ref: StrictStr

    @model_validator(mode="after")
    def _operations_are_unique_and_nonempty(self) -> Self:
        names = [op.operation for op in self.operations]
        if not names:
            raise ValueError("capability must declare at least one operation")
        if len(set(names)) != len(names):
            raise ValueError("duplicate operation in capability spec")
        return self


class CapabilitySnapshot(Contract):
    snapshot_id: StrictStr
    specs: tuple[CapabilitySpec, ...]

    @model_validator(mode="after")
    def _capability_versions_are_unique(self) -> Self:
        keys = [(s.capability_id, s.version) for s in self.specs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate (capability_id, version) in snapshot")
        return self
```

```python
# contracts/candidates.py
"""Resolver 的唯一输出。route_shadow 只消费同一份 CandidateSet，不自行 build。"""

from xiaowei_agent.contracts.base import Contract, StrictStr


class Candidate(Contract):
    capability_id: StrictStr
    capability_version: StrictStr
    operation: StrictStr
    score: float
    match_evidence: tuple[str, ...]
    required_context: tuple[StrictStr, ...]


class Rejection(Contract):
    capability_id: StrictStr
    capability_version: StrictStr
    reason_code: StrictStr


class CandidateSet(Contract):
    resolver_version: StrictStr
    snapshot_id: StrictStr
    items: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]
```

- [ ] **步骤 5：实现 `tests/fakes/fixtures.py`**

```python
"""测试夹具。写能力仅用于伪标反证，永不注册真实 adapter。"""

from xiaowei_agent.contracts import (
    CapabilitySnapshot, CapabilitySpec, EffectClass, OperationSpec,
)

READ_CAP = "starrocks.slow_query.diagnose"
READ_OP = "list_slow_queries"
WRITE_CAP = "test.synthetic.write"
WRITE_OP = "mutate_probe"
CAP_VERSION = "1.0.0"

SNAPSHOT = CapabilitySnapshot(
    snapshot_id="snap-m2-fixture",
    specs=(
        CapabilitySpec(
            capability_id=READ_CAP, version=CAP_VERSION, domain="starrocks",
            operations=(OperationSpec(
                operation=READ_OP, effect_class=EffectClass.READ, side_effect=False,
                argument_schema_ref="schema.starrocks.slow_query.v1",
            ),),
            policy_profile="readonly.default",
            evidence_contract="evidence.starrocks.slow_query.v1",
            eval_ref="evals.starrocks.slow_query",
        ),
        CapabilitySpec(
            capability_id=WRITE_CAP, version=CAP_VERSION, domain="test",
            operations=(OperationSpec(
                operation=WRITE_OP, effect_class=EffectClass.MUTATE_TARGET,
                side_effect=True, argument_schema_ref="schema.test.write.v1",
            ),),
            policy_profile="write.synthetic",
            evidence_contract="evidence.test.write.v1",
            eval_ref="evals.test.write",
        ),
    ),
)
```

- [ ] **步骤 6：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/contracts tests/fakes tests/unit/test_capability_spec.py tests/security/test_intent_pollution.py pyproject.toml
git commit -m "feat(contracts): 执行上下文、意图污染防护与能力声明快照唯一性"
```

---

### T4：目标、计划、策略与审批契约

**根因背景**：本任务落地审核 #1（移除 step 级 capability）、#8（目标规范化顺序）、D3 裁定（`StepResultStatus`）、D5 裁定（`tool_call_hash`）、#7（`ApprovalRequest.policy_revision`）。

**文件**：
- 创建：`src/xiaowei_agent/contracts/target.py`、`plan.py`、`policy.py`、`approval.py`
- 测试：`tests/unit/test_plan_contracts.py`、`tests/security/test_step_condition_closed_set.py`、`tests/security/test_target_stability.py`

**接口**：
- 产出：`ResolvedTarget`、`StepCondition`、`PlanStep`、`PlanBudget`、`ExecutionPlan`、`PLAN_SCHEMA_VERSION`、`PolicyDecision`、`PolicySnapshot`、`ApprovalRequest`、`AdmissionCertificate`

- [ ] **步骤 1：写失败测试 —— 目标稳定性**

`tests/security/test_target_stability.py`：

```python
"""目标必须稳定且非空，规范化后碰撞一律拒绝。

碰撞**拒绝**而非静默合并：两个规范化后相同的资源 ID 说明调用方传入了未确认的
别名，此时"目标解析不稳定"，不能生成可审批的指纹（ARCHITECTURE §7.2）。
"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import ResolvedTarget

pytestmark = pytest.mark.security


def _target(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant_id": "dev-local", "environment_id": "dev", "provider": "starrocks",
        "resource_kind": "cluster", "resource_ids": ("c1", "c2"),
        "selector_version": "1",
    }
    return base | overrides


def test_empty_resource_ids_are_rejected() -> None:
    """空目标无法生成可审批指纹。"""
    with pytest.raises(ValidationError):
        ResolvedTarget(**_target(resource_ids=()))


def test_exact_duplicate_resource_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedTarget(**_target(resource_ids=("c1", "c1")))


def test_nfc_colliding_resource_ids_are_rejected() -> None:
    """组合式与预组合式写法指向同一资源：这是未确认别名，拒绝而非合并。"""
    decomposed = "café"      # cafe + 组合尖音符
    precomposed = "café"      # café
    assert decomposed != precomposed
    with pytest.raises(ValidationError):
        ResolvedTarget(**_target(resource_ids=(decomposed, precomposed)))


def test_resource_ids_are_normalised_on_construction() -> None:
    """构造后取值即为 NFC 形式，后续 hash 无需再关心输入写法。"""
    target = ResolvedTarget(**_target(resource_ids=("café",)))
    assert target.resource_ids == ("café",)


def test_blank_resource_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResolvedTarget(**_target(resource_ids=("c1", "  ")))
```

`tests/security/test_step_condition_closed_set.py`：

```python
"""可选只读分支的条件必须是闭集，不能表达任意谓词。

这是 AGENT_HANDOFF §8 记录的残余风险"被实现成变相动态扩计划"的直接防线。
"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import StepCondition, StepConditionKind, StepResultStatus

pytestmark = pytest.mark.security


def test_condition_kinds_are_exactly_four() -> None:
    assert {m.value for m in StepConditionKind} == {
        "always", "evidence_field_absent", "evidence_row_count_below",
        "prior_step_result_is",
    }


def test_condition_references_step_result_not_task_status() -> None:
    """步骤结果与任务状态分属两个层级，混用会让两者不可区分（D3 裁定）。"""
    from xiaowei_agent.contracts import TaskStatus

    ok = StepCondition(
        kind=StepConditionKind.PRIOR_STEP_RESULT_IS, ref_step_id="s1",
        expected_result=StepResultStatus.FAILED,
    )
    assert ok.expected_result is StepResultStatus.FAILED
    with pytest.raises(ValidationError):
        StepCondition(
            kind=StepConditionKind.PRIOR_STEP_RESULT_IS, ref_step_id="s1",
            expected_result=TaskStatus.FAILED,
        )


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        StepCondition(kind="model_says_so")


def test_condition_cannot_carry_free_form_expression() -> None:
    with pytest.raises(ValidationError):
        StepCondition(kind=StepConditionKind.ALWAYS, expression="len(rows) > 0")


def test_always_forbids_all_operands() -> None:
    with pytest.raises(ValidationError):
        StepCondition(kind=StepConditionKind.ALWAYS, ref_step_id="s1")


def test_each_kind_requires_exactly_its_operands() -> None:
    with pytest.raises(ValidationError):
        StepCondition(kind=StepConditionKind.EVIDENCE_FIELD_ABSENT, ref_step_id="s1")
    with pytest.raises(ValidationError):
        StepCondition(
            kind=StepConditionKind.EVIDENCE_FIELD_ABSENT, ref_step_id="s1",
            field="db", threshold=1,
        )
    ok = StepCondition(
        kind=StepConditionKind.EVIDENCE_FIELD_ABSENT, ref_step_id="s1", field="db",
    )
    assert ok.field == "db"


def test_row_count_threshold_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        StepCondition(
            kind=StepConditionKind.EVIDENCE_ROW_COUNT_BELOW, ref_step_id="s1",
            threshold=-1,
        )
```

`tests/unit/test_plan_contracts.py` 关键用例：

```python
def test_plan_step_carries_no_capability_fields() -> None:
    """计划绑定单 capability；step 级 capability 字段会造成 hash 覆盖缺口（审核 #1）。"""
    assert not ({"capability_id", "capability_version"} & set(PlanStep.model_fields))


def test_execution_plan_has_no_self_declared_hash_fields() -> None:
    assert not ({"plan_hash", "target_fingerprint"} & set(ExecutionPlan.model_fields))


def test_duplicate_step_ids_are_rejected() -> None: ...
def test_forward_or_unknown_dependency_is_rejected() -> None: ...
def test_condition_referencing_a_later_step_is_rejected() -> None: ...
def test_step_count_exceeding_budget_is_rejected() -> None: ...
def test_typed_arguments_reject_nested_structures() -> None: ...
def test_typed_arguments_are_deeply_immutable() -> None: ...
def test_admission_certificate_requires_tool_call_hash() -> None:
    assert "tool_call_hash" in AdmissionCertificate.model_fields
def test_approval_request_carries_policy_revision() -> None:
    assert "policy_revision" in ApprovalRequest.model_fields
```

- [ ] **步骤 2：确认失败**

`python -m pytest tests/security/test_target_stability.py -q` → `ImportError: cannot import name 'ResolvedTarget'`

- [ ] **步骤 3：实现 `target.py`**

```python
"""已解析的执行目标。

**规范化在构造时完成，不在 hash 时完成**：V1 先 ``set()`` 去重再交给
canonical_json 做 NFC，顺序反了——规范化后才相同的两个 ID 会作为两项存活，
使同一目标产生不同指纹（审核 #8）。这里改为构造时先 NFC 规范化，再检测碰撞，
碰撞即拒绝：规范化后相同意味着调用方传入了未确认别名，目标解析不稳定，
不应生成可审批指纹（ARCHITECTURE §7.2）。

不含连接串、凭证、原始 SQL 或 display text——它们会让指纹随展示措辞漂移。
"""

import unicodedata
from typing import Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import Contract, StrictStr


class ResolvedTarget(Contract):
    tenant_id: StrictStr
    environment_id: StrictStr
    provider: StrictStr
    resource_kind: StrictStr
    resource_ids: tuple[StrictStr, ...] = Field(min_length=1)
    selector_version: StrictStr

    @model_validator(mode="after")
    def _normalise_and_reject_collisions(self) -> Self:
        normalised = tuple(
            unicodedata.normalize("NFC", rid) for rid in self.resource_ids
        )
        if len(set(normalised)) != len(normalised):
            raise ValueError(
                "resource_ids collide after NFC normalisation; "
                "target contains unconfirmed aliases"
            )
        if normalised != self.resource_ids:
            return self.model_copy(update={"resource_ids": normalised})
        return self
```

- [ ] **步骤 4：实现 `plan.py`**

```python
"""执行计划。

**计划绑定单一 capability**：``capability_id`` / ``capability_version`` 只出现在
计划级，不出现在步骤级。V1 让步骤各带一份，而 ``plan_hash`` 的 ordered_steps
不含它们，于是同一份计划可以改掉某步的 capability 而 hash 不变（审核 #1）。
将来若确实需要跨 capability 计划，须递增 PLAN_SCHEMA_VERSION 并单独立 ADR。

**闭集条件是可选只读分支的全部安全价值**：条件无法表达任意谓词、无法引用模型
输出、无法承载代码。新增一种条件必须改枚举并过评审。

``plan_hash`` / ``target_fingerprint`` 不是本类字段：存一份"自称的 hash"只会
制造可被篡改的第二真源（决策 D4）。
"""

from types import MappingProxyType
from typing import Final, Mapping, Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import Contract, FrozenMap, StrictStr
from xiaowei_agent.contracts.enums import (
    EffectClass, StepConditionKind, StepResultStatus,
)

PLAN_SCHEMA_VERSION: Final[int] = 1
"""含 effect_class、condition 与 budget 的首个规范形状（决策 D1/D3）。

计划字段新增或语义变化必须递增本常量，并同步更新 ARCHITECTURE.md §7.1。
"""

_OPERANDS: Final[Mapping[StepConditionKind, frozenset[str]]] = MappingProxyType({
    StepConditionKind.ALWAYS: frozenset(),
    StepConditionKind.EVIDENCE_FIELD_ABSENT: frozenset({"ref_step_id", "field"}),
    StepConditionKind.EVIDENCE_ROW_COUNT_BELOW: frozenset({"ref_step_id", "threshold"}),
    StepConditionKind.PRIOR_STEP_RESULT_IS: frozenset({"ref_step_id", "expected_result"}),
})

_OPERAND_NAMES: Final[tuple[str, ...]] = (
    "ref_step_id", "field", "threshold", "expected_result",
)


class StepCondition(Contract):
    kind: StepConditionKind = StepConditionKind.ALWAYS
    ref_step_id: StrictStr | None = None
    field: StrictStr | None = None
    threshold: int | None = Field(default=None, ge=0)
    expected_result: StepResultStatus | None = None

    @model_validator(mode="after")
    def _operands_match_kind_exactly(self) -> Self:
        present = {n for n in _OPERAND_NAMES if getattr(self, n) is not None}
        required = _OPERANDS[self.kind]
        if present != required:
            raise ValueError(
                f"condition {self.kind.value} requires exactly {sorted(required)}"
            )
        return self


class PlanStep(Contract):
    step_id: StrictStr
    operation: StrictStr
    typed_arguments: FrozenMap
    depends_on: tuple[StrictStr, ...]
    side_effect: bool
    effect_class: EffectClass
    condition: StepCondition = StepCondition()


class PlanBudget(Contract):
    max_steps: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_model_tokens: int = Field(gt=0)


class ExecutionPlan(Contract):
    plan_schema_version: int = PLAN_SCHEMA_VERSION
    capability_id: StrictStr
    capability_version: StrictStr
    steps: tuple[PlanStep, ...]
    policy_profile: StrictStr
    policy_revision: StrictStr
    budget: PlanBudget

    @model_validator(mode="after")
    def _steps_are_well_formed(self) -> Self:
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError(f"duplicate step_id: {step.step_id}")
            for dep in step.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"step {step.step_id} depends on unknown or later step {dep}"
                    )
            ref = step.condition.ref_step_id
            if ref is not None and ref not in seen:
                raise ValueError(
                    f"step {step.step_id} condition references unknown or later step"
                )
            seen.add(step.step_id)
        if len(self.steps) > self.budget.max_steps:
            raise ValueError("plan exceeds max_steps budget")
        return self
```

> **为何 `depends_on` 只能引用更早的步骤**：`seen` 在遍历中累积，因此前向引用与环在校验期即失败，无需事后拓扑排序，且让"步骤顺序即执行顺序"成为契约的一部分——`plan_hash` 的 `ordered_steps` 才有确定语义。可选分支的 `ref_step_id` 受同一约束：条件只能看已执行过的步骤。

- [ ] **步骤 5：实现 `policy.py` 与 `approval.py`**

```python
# contracts/policy.py
"""策略判定与策略快照。

``PolicySnapshot`` 是"当前生效的 policy revision 与允许的 profile 集合"，
使"未知 policy revision"成为可在 M2 用纯函数验收的检查（审核 #7）。
"""

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import RiskLevel


class PolicyDecision(Contract):
    allow: bool
    reason_code: StrictStr
    risk: RiskLevel
    policy_revision: StrictStr
    obligations: tuple[StrictStr, ...]


class PolicySnapshot(Contract):
    policy_revision: StrictStr
    profiles: tuple[StrictStr, ...]
```

```python
# contracts/approval.py
"""审批绑定与准入凭证。

``ApprovalRequest`` 保存审批**时刻**的 plan_hash、target_fingerprint 与
policy_revision；恢复时用重算值比对，不匹配即拒绝（ARCHITECTURE §5.7）。
V1 漏了 policy_revision，使"policy 变化不能静默让旧审批继续生效"无处校验
（审核 #7）。

``AdmissionCertificate`` 由 StepAdmission（M3）产出、ToolGateway 消费。
它必须同时绑定**步骤身份**与**调用内容**：V1 只绑定 step_id/operation，
于是拿合法凭证替换 typed_args 就能执行另一个调用（审核 #2）。``tool_call_hash``
覆盖 ToolCall 的全部字段，Gateway 重算比对。
"""

import datetime as _dt

from xiaowei_agent.contracts.base import Contract, StrictStr
from xiaowei_agent.contracts.enums import ApprovalState, EffectClass
from xiaowei_agent.contracts.policy import PolicyDecision


class ApprovalRequest(Contract):
    task_id: StrictStr
    step_id: StrictStr
    plan_hash: StrictStr
    target_fingerprint: StrictStr
    policy_revision: StrictStr
    subject: StrictStr
    expires_at: _dt.datetime
    state: ApprovalState


class AdmissionCertificate(Contract):
    step_id: StrictStr
    operation: StrictStr
    effect_class: EffectClass
    policy_decision: PolicyDecision
    approval_ref: str | None
    plan_hash: StrictStr
    target_fingerprint: StrictStr
    tool_call_hash: StrictStr
```

- [ ] **步骤 6：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/contracts tests/unit/test_plan_contracts.py tests/security/test_step_condition_closed_set.py tests/security/test_target_stability.py
git commit -m "feat(contracts): 单 capability 计划、闭集分支条件、目标规范化与审批/准入绑定"
```

---

### T5：规范化 JSON 与三个确定性指纹

**根因背景**：本任务落地审核 #1（覆盖完备性）、#2（call digest）、#8（canonical 键碰撞）、#9（消除占位常量）。

**文件**：
- 创建：`src/xiaowei_agent/planning/__init__.py`、`canonical.py`
- 修改：`tests/fakes/fixtures.py`（新增 `FIXTURE_PLAN`、`FIXTURE_TARGET`、`FIXTURE_TOOL_CALL`）
- 测试：`tests/unit/test_canonical_json.py`、`tests/unit/test_hash_vectors.py`、`tests/security/test_hash_coverage.py`

**接口**：
- 产出：`canonical_json(value) -> bytes`、`compute_plan_hash(plan) -> str`、`compute_target_fingerprint(target) -> str`、`compute_tool_call_hash(call) -> str`，以及三张显式映射表 `PLAN_FIELD_TO_HASH_KEY`、`STEP_FIELD_TO_HASH_KEY`、`TARGET_FIELD_TO_HASH_KEY`、`TOOL_CALL_FIELD_TO_HASH_KEY`

- [ ] **步骤 1：写失败测试 —— 规范化不变量**

`tests/unit/test_canonical_json.py`：

```python
"""canonical_json 的稳定性来自五条规则：
键序、无空白、NFC、拒绝不可表达值、**NFC 后键碰撞即抛错**。
"""

import math

import pytest

from xiaowei_agent.planning import canonical_json


def test_key_order_is_independent_of_insertion_order() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_no_whitespace_and_utf8() -> None:
    assert canonical_json({"k": "值"}) == '{"k":"值"}'.encode()


def test_strings_are_nfc_normalised() -> None:
    decomposed = "é"
    precomposed = "é"
    assert decomposed != precomposed
    assert canonical_json({"k": decomposed}) == canonical_json({"k": precomposed})


def test_canonical_json_rejects_key_collision_after_nfc() -> None:
    """两个不同键规范化后相同，若静默覆盖会让两份不同数据产生同一指纹。

    这是与 ResolvedTarget 的 resource_ids 同一类的缺陷（审核 #8），
    在 canonical_json 这一层也必须堵住。
    """
    with pytest.raises(ValueError, match="collision"):
        canonical_json({"é": 1, "é": 2})


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_are_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"k": bad})


def test_unsupported_type_is_rejected() -> None:
    """不做 str() 兜底：静默字符串化会让两个不同对象产生同一指纹。"""
    with pytest.raises(TypeError):
        canonical_json({"k": object()})


def test_non_string_key_is_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_json({1: "a"})


def test_integral_float_and_int_stay_distinct() -> None:
    assert canonical_json({"k": 1}) != canonical_json({"k": 1.0})


def test_bytes_are_rejected() -> None:
    with pytest.raises(TypeError):
        canonical_json({"k": b"raw"})
```

- [ ] **步骤 2：写失败测试 —— 固定向量（无占位符）**

V1 用 `"<首次实现后填入并冻结>"` 占位，不是可执行 TDD（审核 #9）。改为**直接写出规范 JSON 字节串**——它由 canonicalization 规则完全确定，可以手工推导——再由它导出摘要。这样测试自包含，且比一个不透明的十六进制串更强：它把序列化本身也钉住了。

`tests/unit/test_hash_vectors.py`：

```python
"""三个指纹的固定向量。

期望值写成规范 JSON 字节串而非不透明摘要：字节串把键序、分隔符、空值表示、
数字格式一并钉死，任何 canonicalization 改动都会在这里立刻可读地暴露出来。
"""

import hashlib

import pytest

from xiaowei_agent.contracts import (
    EffectClass, ExecutionPlan, PlanStep, StepCondition, StepConditionKind,
)
from xiaowei_agent.planning import (
    canonical_json, compute_plan_hash, compute_target_fingerprint, compute_tool_call_hash,
)
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET, FIXTURE_TOOL_CALL

_PLAN_CANONICAL = (
    b'{"budget":{"max_model_tokens":8000,"max_steps":4,"max_tool_calls":4},'
    b'"capability_id":"starrocks.slow_query.diagnose","capability_version":"1.0.0",'
    b'"ordered_steps":[{"condition":{"expected_result":null,"field":null,'
    b'"kind":"always","ref_step_id":null,"threshold":null},"depends_on":[],'
    b'"effect_class":"read","operation":"list_slow_queries","side_effect":false,'
    b'"step_id":"s1","typed_arguments":{"window_minutes":30}}],'
    b'"plan_schema_version":1,"policy_profile":"readonly.default",'
    b'"policy_revision":"policy-2026-09-01"}'
)

_TARGET_CANONICAL = (
    b'{"environment_id":"dev","provider":"starrocks","resource_ids":["c1","c2"],'
    b'"resource_kind":"cluster","selector_version":"1","tenant_id":"dev-local"}'
)

_TOOL_CALL_CANONICAL = (
    b'{"gateway":"starrocks","idempotency_key":"idem-1",'
    b'"operation":"list_slow_queries","step_id":"s1","timeout_seconds":30.0,'
    b'"typed_args":{"window_minutes":30}}'
)


def test_plan_hash_matches_the_frozen_canonical_form() -> None:
    assert compute_plan_hash(FIXTURE_PLAN) == hashlib.sha256(_PLAN_CANONICAL).hexdigest()


def test_target_fingerprint_matches_the_frozen_canonical_form() -> None:
    assert compute_target_fingerprint(FIXTURE_TARGET) == hashlib.sha256(
        _TARGET_CANONICAL
    ).hexdigest()


def test_tool_call_hash_matches_the_frozen_canonical_form() -> None:
    assert compute_tool_call_hash(FIXTURE_TOOL_CALL) == hashlib.sha256(
        _TOOL_CALL_CANONICAL
    ).hexdigest()


def test_hashes_are_stable_across_repeated_calls() -> None:
    assert compute_plan_hash(FIXTURE_PLAN) == compute_plan_hash(FIXTURE_PLAN)


def _plan_with_step(**overrides: object) -> ExecutionPlan:
    step = FIXTURE_PLAN.steps[0].model_copy(update=overrides)
    return FIXTURE_PLAN.model_copy(update={"steps": (step,)})


def test_changing_effect_class_changes_plan_hash() -> None:
    """决策 D1：分类漂移必须被 plan_hash 检出。"""
    mutated = _plan_with_step(effect_class=EffectClass.MUTATE_TARGET, side_effect=True)
    assert compute_plan_hash(mutated) != compute_plan_hash(FIXTURE_PLAN)


def test_changing_condition_changes_plan_hash() -> None:
    """决策 D3：可选分支的条件是计划的一部分。"""
    mutated = _plan_with_step(condition=StepCondition(
        kind=StepConditionKind.EVIDENCE_ROW_COUNT_BELOW, ref_step_id="s1", threshold=1,
    ))
    assert compute_plan_hash(mutated) != compute_plan_hash(FIXTURE_PLAN)


def test_changing_budget_changes_plan_hash() -> None:
    """审核 #1 的连带发现：budget 此前未入 hash，已批准计划可被换成更大预算。"""
    bigger = FIXTURE_PLAN.budget.model_copy(update={"max_tool_calls": 999})
    mutated = FIXTURE_PLAN.model_copy(update={"budget": bigger})
    assert compute_plan_hash(mutated) != compute_plan_hash(FIXTURE_PLAN)


def test_changing_capability_version_changes_plan_hash() -> None:
    mutated = FIXTURE_PLAN.model_copy(update={"capability_version": "1.0.1"})
    assert compute_plan_hash(mutated) != compute_plan_hash(FIXTURE_PLAN)


def test_step_reordering_changes_plan_hash(two_step_plan: ExecutionPlan) -> None:
    reversed_plan = two_step_plan.model_copy(
        update={"steps": tuple(reversed(two_step_plan.steps))}
    )
    assert compute_plan_hash(reversed_plan) != compute_plan_hash(two_step_plan)


def test_changing_tool_call_args_changes_tool_call_hash() -> None:
    """审核 #2：准入凭证必须能证明被执行的是已准入的那个调用。"""
    mutated = FIXTURE_TOOL_CALL.model_copy(update={"typed_args": {"window_minutes": 1440}})
    assert compute_tool_call_hash(mutated) != compute_tool_call_hash(FIXTURE_TOOL_CALL)


@pytest.mark.parametrize(
    ("field", "value"),
    [("gateway", "other"), ("operation", "other"), ("step_id", "s2"),
     ("timeout_seconds", 60.0), ("idempotency_key", "idem-2")],
)
def test_every_tool_call_field_affects_its_hash(field: str, value: object) -> None:
    mutated = FIXTURE_TOOL_CALL.model_copy(update={field: value})
    assert compute_tool_call_hash(mutated) != compute_tool_call_hash(FIXTURE_TOOL_CALL)


def test_resource_id_order_does_not_change_fingerprint() -> None:
    swapped = FIXTURE_TARGET.model_copy(update={"resource_ids": ("c2", "c1")})
    assert compute_target_fingerprint(swapped) == compute_target_fingerprint(FIXTURE_TARGET)
```

- [ ] **步骤 3：写失败测试 —— 覆盖完备性（防未来回归）**

`tests/security/test_hash_coverage.py`：

```python
"""每个 DTO 的**全部**字段都必须进入对应指纹。

V1 的 plan_hash 漏掉 capability_id / capability_version / budget，根因是靠人
记得同步（审核 #1）。这里改由显式映射表承重：给 DTO 加字段却没加进映射表，
本文件立刻转红。
"""

import pytest

from xiaowei_agent.contracts import ExecutionPlan, PlanStep, ResolvedTarget, ToolCall
from xiaowei_agent.planning import (
    PLAN_FIELD_TO_HASH_KEY, STEP_FIELD_TO_HASH_KEY,
    TARGET_FIELD_TO_HASH_KEY, TOOL_CALL_FIELD_TO_HASH_KEY,
)
from xiaowei_agent.planning.canonical import (
    _plan_payload, _target_payload, _tool_call_payload,
)
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET, FIXTURE_TOOL_CALL

pytestmark = pytest.mark.security

_CASES = [
    (ExecutionPlan, PLAN_FIELD_TO_HASH_KEY),
    (PlanStep, STEP_FIELD_TO_HASH_KEY),
    (ResolvedTarget, TARGET_FIELD_TO_HASH_KEY),
    (ToolCall, TOOL_CALL_FIELD_TO_HASH_KEY),
]


@pytest.mark.parametrize(("model", "mapping"), _CASES, ids=lambda x: getattr(x, "__name__", ""))
def test_every_model_field_maps_to_a_hash_key(model: type, mapping: dict) -> None:
    assert set(mapping) == set(model.model_fields), (
        f"{model.__name__} 的字段集与 hash 映射表不一致；"
        "新增字段必须同时决定它是否进入指纹"
    )


def test_payload_keys_equal_the_declared_hash_keys() -> None:
    """实现真的用了映射表声明的键，而不是另写一份。"""
    assert set(_plan_payload(FIXTURE_PLAN)) == set(PLAN_FIELD_TO_HASH_KEY.values())
    assert set(_target_payload(FIXTURE_TARGET)) == set(TARGET_FIELD_TO_HASH_KEY.values())
    assert set(_tool_call_payload(FIXTURE_TOOL_CALL)) == set(
        TOOL_CALL_FIELD_TO_HASH_KEY.values()
    )


def test_step_payload_keys_equal_the_declared_step_hash_keys() -> None:
    payload = _plan_payload(FIXTURE_PLAN)
    assert set(payload["ordered_steps"][0]) == set(STEP_FIELD_TO_HASH_KEY.values())


_VOLATILE_OR_SENSITIVE = {
    "request_id", "trace_id", "timestamp", "created_at", "occurred_at",
    "model_text", "prompt", "raw_sql", "connection_string", "token", "secret",
    "password", "display_text", "answer", "log",
}


def test_no_volatile_or_sensitive_key_enters_any_fingerprint() -> None:
    keys = set()
    for _, mapping in _CASES:
        keys |= set(mapping.values())
    assert not (keys & _VOLATILE_OR_SENSITIVE)
```

- [ ] **步骤 4：确认失败**

`python -m pytest tests/unit/test_canonical_json.py -q` → `ModuleNotFoundError: No module named 'xiaowei_agent.planning'`

- [ ] **步骤 5：实现 `planning/canonical.py`**

```python
"""规范化 JSON 与三个确定性指纹。

**不做 str() 兜底**：静默字符串化会让两个语义不同的对象产生同一指纹，而指纹的
全部价值就是"不同即不同"。未知类型一律 TypeError。

**NFC 规范化会造成键碰撞，必须抛错而非静默覆盖**：两个不同键规范化后相同，
若后者覆盖前者，两份不同数据就会产生同一指纹（审核 #8 的同类缺陷）。

**覆盖完备性由映射表承重**：三张「模型字段名 → 指纹键名」表既是实现的取键
依据，也是 tests/security/test_hash_coverage.py 的断言对象。给 DTO 加字段而
不更新映射表，测试立刻转红（审核 #1 的根因修复）。
"""

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

from xiaowei_agent.contracts import ExecutionPlan, PlanStep, ResolvedTarget, ToolCall

PLAN_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType({
    "plan_schema_version": "plan_schema_version",
    "capability_id": "capability_id",
    "capability_version": "capability_version",
    "steps": "ordered_steps",
    "policy_profile": "policy_profile",
    "policy_revision": "policy_revision",
    "budget": "budget",
})

STEP_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType({
    "step_id": "step_id",
    "operation": "operation",
    "typed_arguments": "typed_arguments",
    "depends_on": "depends_on",
    "side_effect": "side_effect",
    "effect_class": "effect_class",
    "condition": "condition",
})

TARGET_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType({
    "tenant_id": "tenant_id",
    "environment_id": "environment_id",
    "provider": "provider",
    "resource_kind": "resource_kind",
    "resource_ids": "resource_ids",
    "selector_version": "selector_version",
})

TOOL_CALL_FIELD_TO_HASH_KEY: Final[Mapping[str, str]] = MappingProxyType({
    "gateway": "gateway",
    "operation": "operation",
    "step_id": "step_id",
    "typed_args": "typed_args",
    "timeout_seconds": "timeout_seconds",
    "idempotency_key": "idempotency_key",
})


def _normalise(value: object) -> object:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not canonicalisable")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bytes | bytearray):
        raise TypeError("bytes are not canonicalisable")
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical mapping keys must be str")
            nkey = unicodedata.normalize("NFC", key)
            if nkey in out:
                raise ValueError(f"canonical key collision after NFC: {nkey!r}")
            out[nkey] = _normalise(item)
        return out
    if isinstance(value, Sequence):
        return [_normalise(v) for v in value]
    raise TypeError(f"type is not canonicalisable: {type(value).__name__}")


def canonical_json(value: object) -> bytes:
    """固定键序、无空白、UTF-8、NFC 的字节串。不可表达的取值一律抛错。"""
    return json.dumps(
        _normalise(value), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _step_payload(step: PlanStep) -> dict[str, Any]:
    k = STEP_FIELD_TO_HASH_KEY
    condition = step.condition
    return {
        k["step_id"]: step.step_id,
        k["operation"]: step.operation,
        k["typed_arguments"]: dict(step.typed_arguments),
        k["depends_on"]: list(step.depends_on),
        k["side_effect"]: step.side_effect,
        k["effect_class"]: step.effect_class.value,
        k["condition"]: {
            "kind": condition.kind.value,
            "ref_step_id": condition.ref_step_id,
            "field": condition.field,
            "threshold": condition.threshold,
            "expected_result": (
                condition.expected_result.value
                if condition.expected_result is not None else None
            ),
        },
    }


def _plan_payload(plan: ExecutionPlan) -> dict[str, Any]:
    k = PLAN_FIELD_TO_HASH_KEY
    return {
        k["plan_schema_version"]: plan.plan_schema_version,
        k["capability_id"]: plan.capability_id,
        k["capability_version"]: plan.capability_version,
        k["steps"]: [_step_payload(s) for s in plan.steps],
        k["policy_profile"]: plan.policy_profile,
        k["policy_revision"]: plan.policy_revision,
        k["budget"]: {
            "max_steps": plan.budget.max_steps,
            "max_tool_calls": plan.budget.max_tool_calls,
            "max_model_tokens": plan.budget.max_model_tokens,
        },
    }


def _target_payload(target: ResolvedTarget) -> dict[str, Any]:
    k = TARGET_FIELD_TO_HASH_KEY
    return {
        k["tenant_id"]: target.tenant_id,
        k["environment_id"]: target.environment_id,
        k["provider"]: target.provider,
        k["resource_kind"]: target.resource_kind,
        # ResolvedTarget 已在构造时完成 NFC 规范化并拒绝碰撞，此处只需排序。
        k["resource_ids"]: sorted(target.resource_ids),
        k["selector_version"]: target.selector_version,
    }


def _tool_call_payload(call: ToolCall) -> dict[str, Any]:
    k = TOOL_CALL_FIELD_TO_HASH_KEY
    return {
        k["gateway"]: call.gateway,
        k["operation"]: call.operation,
        k["step_id"]: call.step_id,
        k["typed_args"]: dict(call.typed_args),
        k["timeout_seconds"]: call.timeout_seconds,
        k["idempotency_key"]: call.idempotency_key,
    }


def compute_plan_hash(plan: ExecutionPlan) -> str:
    """ARCHITECTURE §7.1 的规范计划摘要。覆盖 ExecutionPlan 与 PlanStep 全部字段。"""
    return hashlib.sha256(canonical_json(_plan_payload(plan))).hexdigest()


def compute_target_fingerprint(target: ResolvedTarget) -> str:
    """ARCHITECTURE §7.2 的目标指纹。"""
    return hashlib.sha256(canonical_json(_target_payload(target))).hexdigest()


def compute_tool_call_hash(call: ToolCall) -> str:
    """调用内容摘要。使准入凭证能证明被执行的正是已准入的那个 ToolCall。"""
    return hashlib.sha256(canonical_json(_tool_call_payload(call))).hexdigest()
```

> **V1 的 `assert` 已按裁定移除**：结构不变量改由 `tests/security/test_hash_coverage.py` 承重，不依赖 `python -O` 会移除的断言。

- [ ] **步骤 6：TDD 反证 —— 撤掉四处承重规则**

每处改完立即执行，确认转红后还原：

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/unit tests/security -q
```

| 变异 | 预期转红 |
| --- | --- |
| 删除 `_normalise` 中的 `unicodedata.normalize` | `test_strings_are_nfc_normalised` |
| 删除映射键碰撞检测 | `test_canonical_json_rejects_key_collision_after_nfc` |
| 从 `_plan_payload` 移除 `budget` 键 | `test_payload_keys_equal_the_declared_hash_keys`、`test_changing_budget_changes_plan_hash` |
| 给 `PlanStep` 加一个字段但不更新 `STEP_FIELD_TO_HASH_KEY` | `test_every_model_field_maps_to_a_hash_key` |

第四条是本任务最重要的反证：它证明**将来的字段遗漏会被自动发现**，而不是靠评审看出来。

- [ ] **步骤 7：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/planning tests/fakes/fixtures.py tests/unit/test_canonical_json.py tests/unit/test_hash_vectors.py tests/security/test_hash_coverage.py
git commit -m "feat(planning): 三个确定性指纹、NFC 碰撞拒绝与字段覆盖完备性承重"
```

---

### T6：E1 分类唯一真源与唯一步骤构造器

**根因背景**：落地审核 D2 裁定——V1 的"只有 effect.py 可赋值分类字段"扫描会卡住 M3 的 PlanCompiler。改为提供**唯一构造器** `build_plan_step()`，扫描口径同步收敛。

**文件**：
- 创建：`src/xiaowei_agent/capabilities/__init__.py`、`effect.py`、`resolver.py`
- 测试：`tests/security/test_effect_classification.py`、`tests/security/test_effect_single_source.py`

**接口**：
- 产出：
  - `class SpecResolutionError(RuntimeError)`
  - `derive_effect(snapshot, *, capability_id, capability_version, operation) -> OperationSpec`
  - `build_plan_step(snapshot, *, capability_id, capability_version, operation, step_id, typed_arguments, depends_on=(), condition=StepCondition()) -> PlanStep` —— **全项目唯一被允许构造带分类字段 `PlanStep` 的入口**
  - `verify_plan_effects(snapshot, plan) -> None` —— 校验计划**每一个**步骤
  - `class CapabilityResolver(Protocol)`

- [ ] **步骤 1：写失败测试 —— ADR-007 D7 的四条 fail-closed 规则**

```python
"""side_effect / effect_class 只能由版本化 CapabilitySpec 确定性派生。

逐条对应 ADR-007 D7 的四条 fail-closed 规则与四条必须承重断言。
"""

import pytest

from xiaowei_agent.capabilities import (
    SpecResolutionError, build_plan_step, derive_effect, verify_plan_effects,
)
from xiaowei_agent.contracts import EffectClass, ExecutionPlan, PlanBudget, PlanStep
from tests.fakes.fixtures import (
    CAP_VERSION, READ_CAP, READ_OP, SNAPSHOT, WRITE_CAP, WRITE_OP,
)

pytestmark = pytest.mark.security

_BUDGET = PlanBudget(max_steps=4, max_tool_calls=4, max_model_tokens=8000)


def _plan(step: PlanStep, capability_id: str = READ_CAP) -> ExecutionPlan:
    return ExecutionPlan(
        capability_id=capability_id, capability_version=CAP_VERSION, steps=(step,),
        policy_profile="readonly.default", policy_revision="policy-2026-09-01",
        budget=_BUDGET,
    )


def _forged(**overrides: object) -> PlanStep:
    base: dict[str, object] = {
        "step_id": "s1", "operation": READ_OP, "typed_arguments": {},
        "depends_on": (), "side_effect": False, "effect_class": EffectClass.READ,
    }
    return PlanStep(**(base | overrides))


# --- 规则 1 与 4：分类未知、未声明、版本不可确定 ---------------------------
def test_undeclared_operation_fails_closed() -> None:
    with pytest.raises(SpecResolutionError):
        derive_effect(SNAPSHOT, capability_id=READ_CAP,
                      capability_version=CAP_VERSION, operation="never_declared")


def test_unknown_capability_fails_closed() -> None:
    with pytest.raises(SpecResolutionError):
        derive_effect(SNAPSHOT, capability_id="no.such.capability",
                      capability_version=CAP_VERSION, operation=READ_OP)


def test_unknown_capability_version_fails_closed() -> None:
    """版本不可确定不回退到任意版本（规则 4）。"""
    with pytest.raises(SpecResolutionError):
        derive_effect(SNAPSHOT, capability_id=READ_CAP,
                      capability_version="9.9.9", operation=READ_OP)


def test_effect_class_has_no_unknown_member() -> None:
    assert {m.value for m in EffectClass} == {"read", "mutate_target"}


# --- 规则 2 与 3：声明冲突、写操作误标只读 --------------------------------
def test_write_operation_forged_as_readonly_is_rejected() -> None:
    """ADR-007 D7 承重断言 1。"""
    plan = _plan(_forged(operation=WRITE_OP, side_effect=False,
                         effect_class=EffectClass.READ), capability_id=WRITE_CAP)
    with pytest.raises(SpecResolutionError):
        verify_plan_effects(SNAPSHOT, plan)


def test_effect_class_conflicting_with_spec_is_rejected() -> None:
    plan = _plan(_forged(operation=WRITE_OP, side_effect=True,
                         effect_class=EffectClass.READ), capability_id=WRITE_CAP)
    with pytest.raises(SpecResolutionError):
        verify_plan_effects(SNAPSHOT, plan)


def test_readonly_operation_forged_as_write_is_rejected() -> None:
    """反向伪造同样拒绝：提权不比降权更可接受。"""
    plan = _plan(_forged(side_effect=True, effect_class=EffectClass.MUTATE_TARGET))
    with pytest.raises(SpecResolutionError):
        verify_plan_effects(SNAPSHOT, plan)


def test_every_step_is_verified_not_just_the_first() -> None:
    """V1 的 verify_step_effect 只看单步，容易漏校验后续步骤。"""
    good = build_plan_step(
        SNAPSHOT, capability_id=READ_CAP, capability_version=CAP_VERSION,
        operation=READ_OP, step_id="s1", typed_arguments={},
    )
    bad = _forged(step_id="s2", side_effect=True,
                  effect_class=EffectClass.MUTATE_TARGET)
    plan = ExecutionPlan(
        capability_id=READ_CAP, capability_version=CAP_VERSION, steps=(good, bad),
        policy_profile="readonly.default", policy_revision="policy-2026-09-01",
        budget=_BUDGET,
    )
    with pytest.raises(SpecResolutionError):
        verify_plan_effects(SNAPSHOT, plan)


# --- 唯一构造器 -----------------------------------------------------------
def test_build_plan_step_derives_classification_from_the_snapshot() -> None:
    step = build_plan_step(
        SNAPSHOT, capability_id=READ_CAP, capability_version=CAP_VERSION,
        operation=READ_OP, step_id="s1", typed_arguments={"window_minutes": 30},
    )
    assert step.effect_class is EffectClass.READ
    assert step.side_effect is False
    verify_plan_effects(SNAPSHOT, _plan(step))


def test_build_plan_step_accepts_no_classification_override() -> None:
    """调用方无从传入分类：它不是参数（ADR-007 D7 承重断言 2）。"""
    import inspect

    params = inspect.signature(build_plan_step).parameters
    assert not ({"side_effect", "effect_class"} & set(params))
```

`tests/security/test_effect_single_source.py`：

```python
"""分类字段只能由唯一构造器产生。

V1 禁止"任何模块赋值分类字段"，这会在 M3 卡住 PlanCompiler——它本就必须构造
带分类的步骤（审核 D2 裁定）。正确口径是：除 capabilities/effect.py 外，
任何模块都不得**直接构造** PlanStep；PlanCompiler 调用 build_plan_step 即合规。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_ALLOWED = {_SRC / "capabilities" / "effect.py"}


def _constructs_plan_step(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "PlanStep":
                return True
    return False


def test_only_effect_module_constructs_plan_step() -> None:
    offenders = [
        p.relative_to(_SRC) for p in _SRC.rglob("*.py")
        if p not in _ALLOWED and p != _SRC / "contracts" / "plan.py"
        and _constructs_plan_step(p)
    ]
    assert not offenders, (
        f"以下模块不得直接构造 PlanStep，请改用 build_plan_step(): {offenders}"
    )
```

- [ ] **步骤 2：确认失败**

`python -m pytest tests/security/test_effect_classification.py -q` → `ModuleNotFoundError: No module named 'xiaowei_agent.capabilities'`

- [ ] **步骤 3：实现 `capabilities/effect.py`**

```python
"""E1 分类的唯一确定性来源（ADR-007 D7）。

三个函数分工：

- ``derive_effect`` 从版本化快照读出声明，是分类的唯一来源。
- ``build_plan_step`` 是全项目唯一被允许构造带分类字段 ``PlanStep`` 的入口；
  它**不接受**分类参数，调用方无从设置、覆盖或降级（承重断言 2）。
- ``verify_plan_effects`` 在准入前重算并核对**计划的每一个步骤**。

**为什么保证放在"每次使用前重算"而不是"构造时正确"**：计划会经 TaskStore
往返、进程重启与并发抢占，只有重算才能覆盖持久化之后被篡改的情形。

**为什么校验整个计划而不是单步**：V1 的单步版本要求调用方对每一步都记得调用，
漏掉一步就出现缺口；整计划版本没有这个漏项面。
"""

from collections.abc import Mapping

from xiaowei_agent.contracts import (
    CapabilitySnapshot, ExecutionPlan, JsonScalar, OperationSpec, PlanStep, StepCondition,
)


class SpecResolutionError(RuntimeError):
    """分类来源缺失、版本不可确定或声明与步骤标记冲突。一律 fail-closed。"""


def derive_effect(
    snapshot: CapabilitySnapshot, *,
    capability_id: str, capability_version: str, operation: str,
) -> OperationSpec:
    """从版本化快照确定性派生 operation 的分类声明。

    :raises SpecResolutionError: 能力、版本或 operation 任一无法唯一确定。
    """
    for spec in snapshot.specs:
        if spec.capability_id != capability_id or spec.version != capability_version:
            continue
        for op in spec.operations:
            if op.operation == operation:
                return op
        raise SpecResolutionError(
            f"operation not declared: {capability_id}@{capability_version}:{operation}"
        )
    raise SpecResolutionError(
        f"capability version not in snapshot: {capability_id}@{capability_version}"
    )


def build_plan_step(
    snapshot: CapabilitySnapshot, *,
    capability_id: str, capability_version: str, operation: str,
    step_id: str, typed_arguments: Mapping[str, JsonScalar],
    depends_on: tuple[str, ...] = (),
    condition: StepCondition | None = None,
) -> PlanStep:
    """构造计划步骤；分类字段由快照派生，**不接受调用方传入**。"""
    declared = derive_effect(
        snapshot, capability_id=capability_id,
        capability_version=capability_version, operation=operation,
    )
    return PlanStep(
        step_id=step_id, operation=operation, typed_arguments=typed_arguments,
        depends_on=depends_on, side_effect=declared.side_effect,
        effect_class=declared.effect_class,
        condition=condition if condition is not None else StepCondition(),
    )


def verify_plan_effects(snapshot: CapabilitySnapshot, plan: ExecutionPlan) -> None:
    """重算并核对计划中**每一个**步骤的分类标记；任一不符即拒绝。

    :raises SpecResolutionError: 分类无法派生，或与步骤标记不一致（双向皆拒）。
    """
    for step in plan.steps:
        declared = derive_effect(
            snapshot, capability_id=plan.capability_id,
            capability_version=plan.capability_version, operation=step.operation,
        )
        if step.effect_class is not declared.effect_class:
            raise SpecResolutionError(f"effect_class mismatch on step {step.step_id}")
        if step.side_effect != declared.side_effect:
            raise SpecResolutionError(f"side_effect mismatch on step {step.step_id}")
```

- [ ] **步骤 4：TDD 反证**

| 变异 | 预期转红 |
| --- | --- |
| 删除 `verify_plan_effects` 的 `side_effect` 比对 | `test_write_operation_forged_as_readonly_is_rejected` |
| 把 `verify_plan_effects` 改成只校验 `plan.steps[0]` | `test_every_step_is_verified_not_just_the_first` |
| 给 `build_plan_step` 加一个 `effect_class` 参数 | `test_build_plan_step_accepts_no_classification_override` |

- [ ] **步骤 5：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/capabilities tests/security/test_effect_classification.py tests/security/test_effect_single_source.py
git commit -m "feat(capabilities): E1 分类唯一真源、唯一步骤构造器与整计划准入前重算"
```

---

### T7：审批绑定与 policy revision 校验

**根因背景**：审核 #7。V1 承认"未知 policy revision"只靠字段必填承担，但 DEVELOPMENT_PLAN §7 M2 把它列为测试门。根因是我误判它必须有 ApprovalGate 才能测——实际它是纯函数可测的。M2 交付**纯校验函数**，M3 的 ApprovalGate 调用它们。

**文件**：
- 创建：`src/xiaowei_agent/governance/__init__.py`、`binding.py`
- 测试：`tests/security/test_approval_binding.py`

**接口**：
- 产出：
  - `class BindingError(RuntimeError)`：携带 `rejection: BindingRejection`
  - `verify_policy_revision(snapshot: PolicySnapshot, *, plan: ExecutionPlan) -> None`
  - `verify_approval_binding(*, approval: ApprovalRequest, plan: ExecutionPlan, target: ResolvedTarget, now: datetime) -> None`

- [ ] **步骤 1：写失败测试**

```python
"""审批绑定与 policy revision 的确定性校验。

覆盖 DEVELOPMENT_PLAN §7 M2 测试门中的"未知 policy revision"与"目标不稳定"，
以及 ARCHITECTURE §5.7 的恢复重解析顺序。
"""

import datetime as dt

import pytest

from xiaowei_agent.contracts import (
    ApprovalRequest, ApprovalState, BindingRejection, PolicySnapshot, ResolvedTarget,
)
from xiaowei_agent.governance import BindingError, verify_approval_binding, verify_policy_revision
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET

pytestmark = pytest.mark.security

_NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.UTC)
_SNAPSHOT = PolicySnapshot(
    policy_revision="policy-2026-09-01", profiles=("readonly.default",),
)


def _approval(**overrides: object) -> ApprovalRequest:
    base: dict[str, object] = {
        "task_id": "t1", "step_id": "s1",
        "plan_hash": compute_plan_hash(FIXTURE_PLAN),
        "target_fingerprint": compute_target_fingerprint(FIXTURE_TARGET),
        "policy_revision": FIXTURE_PLAN.policy_revision,
        "subject": "alice", "expires_at": _NOW + dt.timedelta(hours=1),
        "state": ApprovalState.GRANTED,
    }
    return ApprovalRequest(**(base | overrides))


# --- policy revision ------------------------------------------------------
def test_plan_with_unknown_policy_revision_is_rejected() -> None:
    """计划所依据的 revision 不是当前生效的，必须拒绝。"""
    stale = FIXTURE_PLAN.model_copy(update={"policy_revision": "policy-2026-01-01"})
    with pytest.raises(BindingError) as exc:
        verify_policy_revision(_SNAPSHOT, plan=stale)
    assert exc.value.rejection is BindingRejection.POLICY_REVISION_DRIFT


def test_plan_with_unregistered_policy_profile_is_rejected() -> None:
    unknown = FIXTURE_PLAN.model_copy(update={"policy_profile": "write.anything"})
    with pytest.raises(BindingError):
        verify_policy_revision(_SNAPSHOT, plan=unknown)


def test_matching_policy_revision_passes() -> None:
    verify_policy_revision(_SNAPSHOT, plan=FIXTURE_PLAN)


# --- 审批绑定 -------------------------------------------------------------
def test_matching_binding_passes() -> None:
    verify_approval_binding(
        approval=_approval(), plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW,
    )


def test_plan_drift_is_rejected() -> None:
    drifted = FIXTURE_PLAN.model_copy(update={"capability_version": "1.0.1"})
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=_approval(), plan=drifted, target=FIXTURE_TARGET, now=_NOW,
        )
    assert exc.value.rejection is BindingRejection.PLAN_DRIFT


def test_target_drift_is_rejected() -> None:
    drifted = FIXTURE_TARGET.model_copy(update={"resource_ids": ("c3",)})
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=_approval(), plan=FIXTURE_PLAN, target=drifted, now=_NOW,
        )
    assert exc.value.rejection is BindingRejection.TARGET_DRIFT


def test_policy_revision_drift_between_approval_and_plan_is_rejected() -> None:
    """policy 变化不能静默让旧审批继续生效（ARCHITECTURE §15）。"""
    approval = _approval(policy_revision="policy-2026-01-01")
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=approval, plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW,
        )
    assert exc.value.rejection is BindingRejection.POLICY_REVISION_DRIFT


def test_expired_approval_is_rejected() -> None:
    approval = _approval(expires_at=_NOW - dt.timedelta(seconds=1))
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=approval, plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW,
        )
    assert exc.value.rejection is BindingRejection.APPROVAL_EXPIRED


@pytest.mark.parametrize(
    "state",
    [ApprovalState.PENDING, ApprovalState.REJECTED, ApprovalState.EXPIRED],
)
def test_non_granted_approval_is_rejected(state: ApprovalState) -> None:
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=_approval(state=state), plan=FIXTURE_PLAN,
            target=FIXTURE_TARGET, now=_NOW,
        )
    assert exc.value.rejection is BindingRejection.APPROVAL_NOT_GRANTED


def test_expiry_is_checked_before_state_so_an_expired_grant_reads_as_expired() -> None:
    """拒绝原因必须指向最具体的违规，否则审计看不出真实原因。"""
    approval = _approval(
        state=ApprovalState.GRANTED, expires_at=_NOW - dt.timedelta(seconds=1),
    )
    with pytest.raises(BindingError) as exc:
        verify_approval_binding(
            approval=approval, plan=FIXTURE_PLAN, target=FIXTURE_TARGET, now=_NOW,
        )
    assert exc.value.rejection is BindingRejection.APPROVAL_EXPIRED
```

- [ ] **步骤 2：确认失败**，随后实现 `governance/binding.py`

```python
"""审批绑定与 policy revision 的确定性校验。

M2 只交付**纯校验函数**，不实现 ApprovalGate（属 M3）。把校验独立成纯函数
的好处是：它在 M2 就能被完整验收，且 M3 的 ApprovalGate 与 M4 的恢复路径
调用的是同一份实现，不会各写一套。

**校验顺序即拒绝原因的优先级**：先看审批本身是否有效（过期 → 未授予），
再看它绑定的三个指纹是否漂移。顺序固定后，审计日志里的拒绝原因才指向最具体
的违规，而不是被一个更宽泛的原因掩盖。
"""

import datetime as _dt

from xiaowei_agent.contracts import (
    ApprovalRequest, ApprovalState, BindingRejection, ExecutionPlan,
    PolicySnapshot, ResolvedTarget,
)
from xiaowei_agent.planning import compute_plan_hash, compute_target_fingerprint


class BindingError(RuntimeError):
    """审批绑定或 policy revision 校验失败。"""

    def __init__(self, rejection: BindingRejection, detail: str) -> None:
        super().__init__(f"{rejection.value}: {detail}")
        self.rejection = rejection


def verify_policy_revision(snapshot: PolicySnapshot, *, plan: ExecutionPlan) -> None:
    """计划依据的 policy revision 与 profile 必须是当前生效的。

    :raises BindingError: revision 不匹配，或 profile 未注册。
    """
    if plan.policy_revision != snapshot.policy_revision:
        raise BindingError(
            BindingRejection.POLICY_REVISION_DRIFT,
            "plan policy_revision is not the active revision",
        )
    if plan.policy_profile not in snapshot.profiles:
        raise BindingError(
            BindingRejection.POLICY_REVISION_DRIFT,
            "plan policy_profile is not registered in the active snapshot",
        )


def verify_approval_binding(
    *, approval: ApprovalRequest, plan: ExecutionPlan,
    target: ResolvedTarget, now: _dt.datetime,
) -> None:
    """恢复时重算并核对审批绑定（ARCHITECTURE §5.7）。

    :raises BindingError: 审批过期/未授予，或计划、目标、policy revision 漂移。
    """
    if approval.expires_at <= now:
        raise BindingError(BindingRejection.APPROVAL_EXPIRED, "approval has expired")
    if approval.state is not ApprovalState.GRANTED:
        raise BindingError(
            BindingRejection.APPROVAL_NOT_GRANTED,
            f"approval state is {approval.state.value}",
        )
    if approval.policy_revision != plan.policy_revision:
        raise BindingError(
            BindingRejection.POLICY_REVISION_DRIFT,
            "approval was granted under a different policy revision",
        )
    if approval.plan_hash != compute_plan_hash(plan):
        raise BindingError(BindingRejection.PLAN_DRIFT, "plan_hash mismatch")
    if approval.target_fingerprint != compute_target_fingerprint(target):
        raise BindingError(BindingRejection.TARGET_DRIFT, "target_fingerprint mismatch")
```

- [ ] **步骤 3：TDD 反证**

| 变异 | 预期转红 |
| --- | --- |
| 删除 `approval.policy_revision != plan.policy_revision` 检查 | `test_policy_revision_drift_between_approval_and_plan_is_rejected` |
| 把过期检查移到 state 检查之后 | `test_expiry_is_checked_before_state_so_an_expired_grant_reads_as_expired` |
| 删除 `verify_policy_revision` 的 profile 检查 | `test_plan_with_unregistered_policy_profile_is_rejected` |

- [ ] **步骤 4：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/governance tests/security/test_approval_binding.py
git commit -m "feat(governance): 审批绑定与 policy revision 的确定性校验函数"
```

---

### T8：工具边界 —— 私有工厂、调用内容绑定与 E1 硬闸

**根因背景**：落地审核 #2（call digest）、#4（fake 落点）、#10（扫描范围）。

**文件**：
- 创建：`src/xiaowei_agent/contracts/tool.py`
- 创建：`src/xiaowei_agent/tools/__init__.py`、`adapter.py`、`gateway.py`、`fake.py`
- 新建目录：`tests/contract/`
- 修改：`tests/conftest.py`（共享 fixture）
- 测试：`tests/contract/test_tool_result_factory.py`、`tests/security/test_gateway_boundary.py`

**接口**：
- 产出：`ToolCall`、`ToolResult`、`AdapterResponse`、`ToolAdapter`、`ToolGateway`、`DeterministicToolGateway`、`RecordingToolAdapter`

- [ ] **步骤 1：补 `tests/conftest.py` 的共享 fixture**

V1 的 Gateway 测试引用了 `gateway`、`ok_call`、`admission` 等未定义 fixture（审核 #9）。补齐：

```python
# 追加到现有 tests/conftest.py
import datetime as dt

import pytest

from xiaowei_agent.contracts import (
    AdapterStatus, AdmissionCertificate, EffectClass, PolicyDecision, RequestContext,
    RiskLevel, ToolCall,
)
from xiaowei_agent.planning import compute_plan_hash, compute_tool_call_hash, \
    compute_target_fingerprint
from xiaowei_agent.tools.adapter import AdapterResponse
from xiaowei_agent.tools.fake import RecordingToolAdapter
from xiaowei_agent.tools.gateway import DeterministicToolGateway
from tests.fakes.fixtures import FIXTURE_PLAN, FIXTURE_TARGET, FIXTURE_TOOL_CALL


@pytest.fixture
def context() -> RequestContext:
    return RequestContext(
        tenant_id="dev-local", actor="alice", environment_id="dev",
        trace_id="0" * 32, policy_revision="policy-2026-09-01",
    )


@pytest.fixture
def ok_call() -> ToolCall:
    return FIXTURE_TOOL_CALL


@pytest.fixture
def recording_adapter() -> RecordingToolAdapter:
    return RecordingToolAdapter(responses=(AdapterResponse(
        status=AdapterStatus.OK, payload=({"query_id": "q1"},),
        source="starrocks-fake", error=None, elapsed_ms=3,
    ),))


@pytest.fixture
def gateway(recording_adapter: RecordingToolAdapter) -> DeterministicToolGateway:
    return DeterministicToolGateway(adapters={"starrocks": recording_adapter})


def _certificate(call: ToolCall, **overrides: object) -> AdmissionCertificate:
    base: dict[str, object] = {
        "step_id": call.step_id, "operation": call.operation,
        "effect_class": EffectClass.READ,
        "policy_decision": PolicyDecision(
            allow=True, reason_code="readonly.allowed", risk=RiskLevel.LOW,
            policy_revision="policy-2026-09-01", obligations=(),
        ),
        "approval_ref": None,
        "plan_hash": compute_plan_hash(FIXTURE_PLAN),
        "target_fingerprint": compute_target_fingerprint(FIXTURE_TARGET),
        "tool_call_hash": compute_tool_call_hash(call),
    }
    return AdmissionCertificate(**(base | overrides))


@pytest.fixture
def admission(ok_call: ToolCall) -> AdmissionCertificate:
    return _certificate(ok_call)


@pytest.fixture
def make_certificate():
    return _certificate
```

- [ ] **步骤 2：写失败测试 —— 绕过工厂与绕过准入**

`tests/contract/test_tool_result_factory.py`：

```python
"""Gateway 之外不能用原始 adapter 响应伪造 ToolResult。

**诚实声明**：Python 没有语言级私有性。本文件断言的是**已文档化的边界**——
直接构造、model_validate、model_construct、model_copy 四条实用路径均被封堵。
``object.__setattr__`` 与重定义模块无法在语言层封堵，属已知残余风险，由评审
与源码扫描测试覆盖（ARCHITECTURE §5.8）。
"""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import ToolCallStatus, ToolResult


def _payload() -> dict[str, object]:
    return {
        "status": ToolCallStatus.OK, "data_view": (), "raw_ref": None,
        "source": "fake", "limitations": (), "trace_id": "0" * 32, "error": None,
    }


def test_direct_construction_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolResult(**_payload())


def test_model_validate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate(_payload())


def test_model_construct_is_rejected() -> None:
    """model_construct 绕过全部校验，必须显式封堵。"""
    with pytest.raises(NotImplementedError):
        ToolResult.model_construct(**_payload())


async def test_model_copy_on_a_legitimate_result_is_rejected(
    gateway, ok_call, context, admission,
) -> None:
    """否则可由一个合法结果 copy 出一个被篡改的结果。"""
    legit = await gateway.invoke(ok_call, context=context, admission=admission)
    with pytest.raises(NotImplementedError):
        legit.model_copy(update={"status": ToolCallStatus.ERROR})


async def test_gateway_can_construct(gateway, ok_call, context, admission) -> None:
    result = await gateway.invoke(ok_call, context=context, admission=admission)
    assert isinstance(result, ToolResult)
    assert result.status is ToolCallStatus.OK
    assert result.trace_id == context.trace_id


async def test_result_data_view_is_deeply_immutable(
    gateway, ok_call, context, admission,
) -> None:
    result = await gateway.invoke(ok_call, context=context, admission=admission)
    with pytest.raises(TypeError):
        result.data_view[0]["query_id"] = "tampered"  # type: ignore[index]
```

`tests/security/test_gateway_boundary.py`：

```python
"""ToolGateway 是数据面唯一工具入口，且 M0-M7 的 E1 调用次数恒为 0。"""

import ast
from pathlib import Path

import pytest

from xiaowei_agent.contracts import EffectClass, RiskLevel, PolicyDecision
from xiaowei_agent.tools.gateway import _E1_EXECUTION_ENABLED

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"


def test_e1_execution_is_disabled() -> None:
    """ADR-007 D7：M0-M7 全程禁止 E1，含非生产环境。开闸必须改这一行并过评审。"""
    assert _E1_EXECUTION_ENABLED is False


async def test_side_effect_call_is_refused_and_adapter_is_never_called(
    gateway, ok_call, context, make_certificate, recording_adapter,
) -> None:
    """ADR-007 D7 承重断言 1 与 4。"""
    cert = make_certificate(ok_call, effect_class=EffectClass.MUTATE_TARGET)
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_for_another_step_is_refused(
    gateway, ok_call, context, make_certificate, recording_adapter,
) -> None:
    cert = make_certificate(ok_call, step_id="s99")
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_with_tampered_args_is_refused(
    gateway, ok_call, context, make_certificate, recording_adapter,
) -> None:
    """审核 #2：拿合法凭证替换参数执行同一步骤，必须拒绝。

    凭证是对**已准入的那个 ToolCall** 的证明，不是对步骤身份的证明。
    """
    cert = make_certificate(ok_call)
    tampered = ok_call.model_copy(update={"typed_args": {"window_minutes": 1440}})
    with pytest.raises(PermissionError):
        await gateway.invoke(tampered, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_admission_with_tampered_timeout_is_refused(
    gateway, ok_call, context, make_certificate, recording_adapter,
) -> None:
    cert = make_certificate(ok_call)
    tampered = ok_call.model_copy(update={"timeout_seconds": 300.0})
    with pytest.raises(PermissionError):
        await gateway.invoke(tampered, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_denied_policy_decision_is_refused(
    gateway, ok_call, context, make_certificate, recording_adapter,
) -> None:
    denied = PolicyDecision(
        allow=False, reason_code="policy.denied", risk=RiskLevel.HIGH,
        policy_revision="policy-2026-09-01", obligations=(),
    )
    cert = make_certificate(ok_call, policy_decision=denied)
    with pytest.raises(PermissionError):
        await gateway.invoke(ok_call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


async def test_unregistered_adapter_fails_closed(
    gateway, ok_call, context, make_certificate, recording_adapter,
) -> None:
    call = ok_call.model_copy(update={"gateway": "never_registered"})
    cert = make_certificate(call)
    with pytest.raises(LookupError):
        await gateway.invoke(call, context=context, admission=cert)
    assert recording_adapter.call_count == 0


def test_adapter_response_cannot_carry_effect_classification() -> None:
    """adapter 在类型层面就无法降级分类（ADR-007 D7 承重断言 2）。"""
    from xiaowei_agent.tools.adapter import AdapterResponse

    assert not ({"side_effect", "effect_class"} & set(AdapterResponse.model_fields))


def test_adapter_error_is_wrapped_as_untrusted_external_content() -> None:
    """第三方错误文本不得作为可信控制信号。"""
    from xiaowei_agent.contracts import ExternalContent
    from xiaowei_agent.tools.adapter import AdapterResponse

    annotation = AdapterResponse.model_fields["error"].annotation
    assert ExternalContent in getattr(annotation, "__args__", (annotation,))


def test_only_gateway_module_references_the_construction_witness() -> None:
    allowed = {_SRC / "tools" / "gateway.py", _SRC / "contracts" / "tool.py"}
    names = {"_TOOL_RESULT_WITNESS", "_WITNESS_KEY"}
    offenders = []
    for path in _SRC.rglob("*.py"):
        if path in allowed:
            continue
        if names & set(path.read_text(encoding="utf-8").split()):
            offenders.append(path.relative_to(_SRC))
        else:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            found |= {a.name for n in ast.walk(tree)
                      if isinstance(n, ast.ImportFrom) for a in n.names}
            if names & found:
                offenders.append(path.relative_to(_SRC))
    assert not offenders, f"以下模块不得引用构造凭证: {offenders}"


# --- 领域层禁令的正确作用域（审核 #10） -----------------------------------
_DOMAIN_PACKAGES = (
    "contracts", "capabilities", "planning", "governance", "runners", "observability",
)
"""只扫**领域层**。

刻意排除：
- ``tools/``：持有外部客户端正是 adapter 的本职（AGENTS.md 落点表）。
- ``persistence/``：M4 起必须引入 SQLAlchemy / 数据库驱动（DEVELOPMENT_PLAN §7 M4）。

V1 扫描整个 ``src``，会在 M4 与 M6b 误杀合法实现（审核 #10）。
"""

_BANNED_ROOTS = {
    "pymysql", "mysql", "psycopg", "psycopg2", "sqlalchemy", "alembic",
    "requests", "httpx", "aiohttp", "urllib3", "kafka", "kubernetes", "redis",
}


def test_domain_layer_has_no_third_party_client_import() -> None:
    offenders: list[tuple[str, str]] = []
    for package in _DOMAIN_PACKAGES:
        root = _SRC / package
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                if isinstance(node, ast.Import):
                    module = node.names[0].name
                if (module or "").split(".")[0] in _BANNED_ROOTS:
                    offenders.append((str(path.relative_to(_SRC)), module or ""))
    assert not offenders, f"领域层不得导入外部客户端: {offenders}"
```

- [ ] **步骤 3：实现 `contracts/tool.py`**

```python
"""工具调用与工具结果。

``ToolResult`` 的构造受**模块级凭证**保护：``model_validator(mode="before")``
要求 payload 携带只有 ``tools.gateway`` 能拿到的哨兵对象。``model_construct``
与 ``model_copy`` 另行封堵——前者绕过全部校验，后者能从一个合法结果 copy 出
被篡改的结果。

**诚实边界**：Python 无法提供语言级私有性。``object.__setattr__`` 与重定义
模块两条路径无法封堵，属已知残余风险，由评审与源码扫描测试覆盖。
"""

from typing import Any, Final, Never, Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts.base import Contract, FrozenMap, StrictStr
from xiaowei_agent.contracts.enums import ToolCallStatus
from xiaowei_agent.contracts.errors import AgentError

_TOOL_RESULT_WITNESS: Final[object] = object()
_WITNESS_KEY: Final[str] = "__gateway_witness__"


class ToolCall(Contract):
    gateway: StrictStr
    operation: StrictStr
    step_id: StrictStr
    typed_args: FrozenMap
    timeout_seconds: float = Field(gt=0.0, le=300.0)
    idempotency_key: StrictStr


class ToolResult(Contract):
    status: ToolCallStatus
    data_view: tuple[FrozenMap, ...]
    raw_ref: str | None
    source: StrictStr
    limitations: tuple[str, ...]
    trace_id: StrictStr
    error: AgentError | None = None

    @model_validator(mode="before")
    @classmethod
    def _require_gateway_witness(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise ValueError("ToolResult 只能由 ToolGateway 构造")
        if data.pop(_WITNESS_KEY, None) is not _TOOL_RESULT_WITNESS:
            raise ValueError("ToolResult 只能由 ToolGateway 构造")
        return data

    @classmethod
    def model_construct(cls, *args: object, **kwargs: object) -> Never:
        raise NotImplementedError("ToolResult 只能由 ToolGateway 构造")

    def model_copy(self, *args: object, **kwargs: object) -> Never:
        raise NotImplementedError("ToolResult 不可复制修改；请由 ToolGateway 重新生成")
```

- [ ] **步骤 4：实现 `tools/adapter.py`、`gateway.py`、`fake.py`**

```python
# tools/adapter.py
"""adapter 的内部返回类型与契约。

``AdapterResponse`` **不含** side_effect / effect_class：adapter 在类型层面
无法降级分类。第三方错误文本只能经 ``ExternalContent`` 承载，因此不会被当作
可信控制信号（ARCHITECTURE §9）。
"""

from typing import Protocol

from pydantic import Field

from xiaowei_agent.contracts import (
    AdapterStatus, Contract, ExternalContent, FrozenMap, RequestContext,
    StrictStr, ToolCall,
)


class AdapterResponse(Contract):
    status: AdapterStatus
    payload: tuple[FrozenMap, ...]
    source: StrictStr
    error: ExternalContent | None = None
    elapsed_ms: int = Field(ge=0)


class ToolAdapter(Protocol):
    async def execute(
        self, call: ToolCall, *, context: RequestContext
    ) -> AdapterResponse: ...
```

```python
# tools/gateway.py
"""数据面唯一工具入口。

五道闸依次生效，任一不过即拒绝且 **adapter 调用次数为 0**：

1. **凭证与调用同属一个步骤** —— 防止拿 A 步骤的凭证执行 B 步骤。
2. **凭证与调用内容一致** —— 重算 tool_call_hash 比对。凭证证明的是"这个
   ToolCall 已准入"，不是"这个步骤已准入"；否则替换 typed_args 即可绕过
   （审核 #2）。
3. **policy 判定为 allow**。
4. **E1 硬闸** —— 非 READ 分类一律拒绝。这把"M0-M7 E1 调用次数恒为 0"从
   纪律变成代码事实；M8 开闸必须改这一行并过评审（ADR-007 D7）。
5. **adapter 已注册** —— 未注册即 fail-closed，不做回退。
"""

import asyncio
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from xiaowei_agent.contracts import (
    AdapterStatus, AdmissionCertificate, EffectClass, FrozenMap, RequestContext,
    ToolCall, ToolCallStatus, ToolResult,
)
from xiaowei_agent.contracts.tool import _TOOL_RESULT_WITNESS, _WITNESS_KEY
from xiaowei_agent.planning import compute_tool_call_hash
from xiaowei_agent.tools.adapter import ToolAdapter

_E1_EXECUTION_ENABLED: Final[bool] = False
"""ADR-007 D7：M0-M7 全程禁止 E1，含非生产环境。M8 才可由独立评审开闸。"""

_STATUS_MAP: Final[Mapping[AdapterStatus, ToolCallStatus]] = MappingProxyType({
    AdapterStatus.OK: ToolCallStatus.OK,
    AdapterStatus.ERROR: ToolCallStatus.ERROR,
    AdapterStatus.TIMEOUT: ToolCallStatus.TIMEOUT,
})


class DeterministicToolGateway:
    def __init__(self, adapters: Mapping[str, ToolAdapter]) -> None:
        self._adapters = dict(adapters)

    async def invoke(
        self, call: ToolCall, *, context: RequestContext,
        admission: AdmissionCertificate,
    ) -> ToolResult:
        """执行一次已准入的工具调用。

        :raises PermissionError: 凭证与调用不匹配、策略拒绝或触发 E1 硬闸。
        :raises LookupError: adapter 未注册。
        """
        if admission.step_id != call.step_id or admission.operation != call.operation:
            raise PermissionError("admission certificate does not match this step")
        if admission.tool_call_hash != compute_tool_call_hash(call):
            raise PermissionError("admission certificate does not match this call")
        if not admission.policy_decision.allow:
            raise PermissionError("policy denied")
        if admission.effect_class is not EffectClass.READ and not _E1_EXECUTION_ENABLED:
            raise PermissionError("E1 execution is disabled for M0-M7")
        adapter = self._adapters.get(call.gateway)
        if adapter is None:
            raise LookupError(f"adapter not registered: {call.gateway}")

        try:
            response = await asyncio.wait_for(
                adapter.execute(call, context=context), call.timeout_seconds
            )
        except TimeoutError:
            return self._issue(
                context, status=ToolCallStatus.TIMEOUT, data_view=(),
                source=call.gateway, limitations=("adapter timed out",),
            )
        return self._issue(
            context, status=_STATUS_MAP[response.status], data_view=response.payload,
            source=response.source, limitations=(),
        )

    @staticmethod
    def _issue(
        context: RequestContext, *, status: ToolCallStatus,
        data_view: tuple[FrozenMap, ...], source: str, limitations: tuple[str, ...],
    ) -> ToolResult:
        """唯一的 ToolResult 构造点。"""
        return ToolResult(**{
            _WITNESS_KEY: _TOOL_RESULT_WITNESS,
            "status": status, "data_view": data_view, "raw_ref": None,
            "source": source, "limitations": limitations,
            "trace_id": context.trace_id, "error": None,
        })
```

```python
# tools/fake.py
"""fake / recording adapter（DEVELOPMENT_PLAN §7 M2 明文要求置于 tools/）。

**不触达任何被管运维目标**：只回放内存中的固定 payload。``call_count`` 使
"adapter 调用次数为 0"成为可断言事实（ADR-007 D7 承重断言 1 与 4）。
"""

from collections.abc import Sequence
from typing import Final

from xiaowei_agent.contracts import RequestContext, ToolCall
from xiaowei_agent.tools.adapter import AdapterResponse

IS_FAKE: Final[bool] = True
"""供 tests/security/test_fake_isolation.py 断言；生产模块不得导入本模块。"""


class RecordingToolAdapter:
    def __init__(self, responses: Sequence[AdapterResponse]) -> None:
        if not responses:
            raise ValueError("RecordingToolAdapter requires at least one response")
        self._responses = list(responses)
        self.call_count = 0
        self.calls: list[ToolCall] = []

    async def execute(
        self, call: ToolCall, *, context: RequestContext
    ) -> AdapterResponse:
        self.calls.append(call)
        self.call_count += 1
        index = min(self.call_count - 1, len(self._responses) - 1)
        return self._responses[index]
```

- [ ] **步骤 5：TDD 反证**

| 变异 | 预期转红 |
| --- | --- |
| `_E1_EXECUTION_ENABLED = True` | `test_e1_execution_is_disabled`、`test_side_effect_call_is_refused_and_adapter_is_never_called` |
| 删除 `tool_call_hash` 比对 | `test_admission_with_tampered_args_is_refused`、`test_admission_with_tampered_timeout_is_refused` |
| 删除 `step_id`/`operation` 比对 | `test_admission_for_another_step_is_refused` |
| 删除 `_require_gateway_witness` | 工厂契约四条测试 |
| 把 `_DOMAIN_PACKAGES` 改回整个 `src` 并在 `tools/` 放一条 `import httpx` | `test_domain_layer_has_no_third_party_client_import` 误杀（证明 V1 口径确实过宽） |

- [ ] **步骤 6：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/contracts/tool.py src/xiaowei_agent/tools tests/conftest.py tests/contract tests/security/test_gateway_boundary.py
git commit -m "feat(tools): 调用内容绑定的准入凭证、私有 ToolResult 工厂与 E1 硬闸"
```

---

### T9：证据与可答性

**文件**：
- 创建：`src/xiaowei_agent/contracts/evidence.py`、`answerability.py`
- 测试：`tests/unit/test_evidence.py`、`tests/security/test_reflection_boundary.py`

- [ ] **步骤 1：写失败测试**

```python
"""AnswerabilityVerdict 是 Reflection 的唯一输出契约。

**契约层面不存在让 Reflection 修改计划或写 TaskStore 的入口**：字段集只有五项
且 extra="forbid"，越权建议根本无法被表达，而不是被表达后再拒绝
（ARCHITECTURE §4.2、§6）。
"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    AnswerabilityVerdict, EvidenceEnvelope, ExternalSource, MissingItem,
)

pytestmark = pytest.mark.security

_FORBIDDEN = [
    "steps", "step_id", "add_step", "next_step", "plan", "plan_hash",
    "tool", "tool_call", "tool_call_hash", "gateway", "adapter", "operation",
    "target", "target_fingerprint", "resource_ids", "selector",
    "environment_id", "tenant_id", "actor",
    "permission", "effect_class", "side_effect", "approved", "approval_ref",
    "sql", "query", "task_status", "terminal_reason", "policy_revision",
]


def _verdict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "sufficient": True, "limitations": (), "missing": (),
        "downgrade_suggestion": False, "needs_user_input": False,
    }
    return base | overrides


def test_field_set_is_exactly_five() -> None:
    assert set(AnswerabilityVerdict.model_fields) == {
        "sufficient", "limitations", "missing",
        "downgrade_suggestion", "needs_user_input",
    }


@pytest.mark.parametrize("field", _FORBIDDEN)
def test_execution_authority_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        AnswerabilityVerdict(**_verdict(**{field: "x"}))


def test_verdict_is_immutable() -> None:
    verdict = AnswerabilityVerdict(**_verdict())
    with pytest.raises(ValidationError):
        verdict.sufficient = False  # type: ignore[misc]


def test_missing_items_carry_only_keys_and_reason_codes() -> None:
    """缺失项不得携带取数指令。"""
    assert set(MissingItem.model_fields) == {"key", "reason_key"}


_AT = dt.datetime(2026, 9, 2, tzinfo=dt.UTC)


def _evidence(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "evidence_id": "e1", "capability_id": "starrocks.slow_query.diagnose",
        "capability_version": "1.0.0", "facts": ({"query_id": "q1"},),
        "source": "starrocks-fake", "source_kind": ExternalSource.TOOL,
        "captured_at": _AT, "readonly": True, "sampled": True,
        "limitations": ("sampled window: 30m",), "redaction_ref": None,
    }
    return base | overrides


def test_evidence_is_always_readonly_in_m2_to_m7() -> None:
    with pytest.raises(ValidationError):
        EvidenceEnvelope(**_evidence(readonly=False))


def test_evidence_facts_are_deeply_immutable() -> None:
    envelope = EvidenceEnvelope(**_evidence())
    with pytest.raises(TypeError):
        envelope.facts[0]["query_id"] = "tampered"  # type: ignore[index]


def test_evidence_requires_explicit_sampling_and_limitations() -> None:
    """样本性与限制是必填：不能靠调用方记得填。"""
    for field in ("sampled", "limitations"):
        payload = _evidence()
        del payload[field]
        with pytest.raises(ValidationError):
            EvidenceEnvelope(**payload)
```

- [ ] **步骤 2：确认失败后实现**

```python
# contracts/evidence.py
"""证据信封。事实与解释分开：facts 只承载结构化事实，解释归 Advisory。

``readonly`` 钉为 ``Literal[True]``——M2-M7 不存在可写证据。``sampled`` 与
``limitations`` 必填，因为证据的样本性和时效性不能靠调用方记得填。
"""

import datetime as _dt
from typing import Literal

from xiaowei_agent.contracts.base import Contract, FrozenMap, StrictStr
from xiaowei_agent.contracts.enums import ExternalSource


class EvidenceEnvelope(Contract):
    evidence_id: StrictStr
    capability_id: StrictStr
    capability_version: StrictStr
    facts: tuple[FrozenMap, ...]
    source: StrictStr
    source_kind: ExternalSource
    captured_at: _dt.datetime
    readonly: Literal[True] = True
    sampled: bool
    limitations: tuple[str, ...]
    redaction_ref: str | None = None
```

```python
# contracts/answerability.py
"""Reflection 的唯一输出契约。

**这五个字段就是 Reflection 的全部权限**：证据是否充分、有哪些限制、缺了什么、
建议是否降级、建议是否需要用户补充。是否真的进入 indeterminate、是否真的向
用户提问，由 Runtime/Runner 依据本结论确定性决定并写 TaskStore；Reflection
不设终态、不写 TaskStore、不改计划（ARCHITECTURE §4.2）。

字段集之外的任何键都会被 ``extra="forbid"`` 拒绝，越权建议在契约层**无法被
表达**——这比"表达后再拒绝"更强，也更容易长期维持。
"""

from xiaowei_agent.contracts.base import Contract, StrictStr


class MissingItem(Contract):
    key: StrictStr
    reason_key: StrictStr


class AnswerabilityVerdict(Contract):
    sufficient: bool
    limitations: tuple[str, ...]
    missing: tuple[MissingItem, ...]
    downgrade_suggestion: bool
    needs_user_input: bool
```

- [ ] **步骤 3：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/contracts/evidence.py src/xiaowei_agent/contracts/answerability.py tests/unit/test_evidence.py tests/security/test_reflection_boundary.py
git commit -m "feat(contracts): 只读证据信封与 Reflection 五字段边界"
```

---

### T10：TaskStore 交互形状与承重的单进程实现

**根因背景**：落地审核 #4（fake 落点）与 #6（lease/fencing 可绕过）。#6 的根因是把 `fencing_token` 设成 `Optional` 以容纳"尚无租约"的迁移，结果"不传即跳过"。沿同一路径另发现三个同类缺口：幂等键未按租户/环境隔离、同键不同请求会静默复用任务、终态任务仍可 acquire lease。

**这是 M2 对 M4 影响最大的任务。** 若 PostgreSQL adapter 在 M4 被迫改变这里冻结的形状，正确做法是回到 M2/M3 修正契约和测试，而不是在 adapter 内加兼容补丁（DEVELOPMENT_PLAN §7 M4）。

**文件**：
- 创建：`src/xiaowei_agent/contracts/task.py`
- 创建：`src/xiaowei_agent/persistence/__init__.py`、`store.py`、`fake.py`
- 创建：`tests/fakes/clock.py`
- 测试：`tests/contract/test_task_store_contract.py`、`tests/security/test_terminal_protection.py`、`tests/security/test_lease_fencing.py`

**冻结的 Protocol 形状**：

```python
Clock: TypeAlias = Callable[[], datetime]


class TaskNotFound(LookupError): ...
class IdempotencyConflict(RuntimeError):
    """同一幂等键被用于内容不同的请求。"""


class TaskStore(Protocol):
    async def create_task(
        self, *, envelope: RequestEnvelope, context: RequestContext
    ) -> TaskRecord:
        """按 (tenant_id, environment_id, idempotency_key) 幂等创建。

        :raises IdempotencyConflict: 同一作用域内同键但请求内容不同。
        """

    async def get(self, task_id: str) -> TaskRecord:
        """:raises TaskNotFound: 任务不存在。"""

    async def transition(
        self, *, task_id: str, expected_version: int, to_status: TaskStatus,
        fencing_token: int | None = None, terminal_reason: str | None = None,
    ) -> TransitionResult:
        """CAS 状态迁移。**永远返回存储层 winner**，调用方必须采纳它。"""

    async def acquire_lease(
        self, *, task_id: str, owner: str, ttl_seconds: int
    ) -> LeaseGrant | None:
        """取得租约并分配单调递增 fencing token；已被他人持有或任务已终态时返回 None。"""

    async def renew_lease(
        self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> LeaseGrant | None:
        """续租保持同一 token；非持有者、token 陈旧或租约已过期时返回 None。"""

    async def record_approval(self, *, request: ApprovalRequest) -> None: ...

    async def append_audit(self, *, event: TraceEvent) -> None: ...
```

**四条形状决定及理由**：

1. **`transition` 返回 `TransitionResult` 而非抛异常。** CAS 失败是正常并发结果，不是异常路径；返回 `winner` 强制调用方面对"别人赢了"。用异常则极易 `except: pass` 后继续用本地旧对象——正是 ARCHITECTURE §7.3 明令禁止的。
2. **`fencing_token` 的规则是闭合的**，不是"可选校验"：

   | 租约状态 | 传入 token | 结果 |
   | --- | --- | --- |
   | 有 live lease | 与当前 token 相同 | 放行 |
   | 有 live lease | 与当前 token 不同 | `STALE_FENCING_TOKEN` |
   | 有 live lease | 未传 | **`LEASE_NOT_HELD`**（V1 在此放行，是缺口） |
   | 无 live lease | 未传 | 放行（如 `created → planning`，尚未取租约） |
   | 无 live lease | 传了 | `LEASE_NOT_HELD`（持有一个不存在的租约） |

3. **token 在**取得**租约时递增，续租时不变。** 因此"旧持有者的 token < 当前 token"恒成立，被抢占的 worker 拿旧 token 写入必然被拒。
4. **时钟经 `Clock` 注入。** 租约过期是本任务唯一与时间相关的语义，注入时钟才能不 sleep 地测试过期与抢占。

- [ ] **步骤 1：写失败测试 —— 并发、租约与幂等**

`tests/contract/test_task_store_contract.py`：

```python
"""M3/M4 共用的 TaskStore 交互形状。M4 的 PostgreSQL 实现必须原样通过本文件。"""

import pytest

from xiaowei_agent.contracts import Channel, RequestEnvelope, TaskStatus, TransitionRejection
from xiaowei_agent.persistence import IdempotencyConflict, TaskNotFound
from xiaowei_agent.persistence.fake import InMemoryTaskStore
from tests.fakes.clock import ManualClock


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def store(clock: ManualClock) -> InMemoryTaskStore:
    return InMemoryTaskStore(clock=clock)


def _envelope(**overrides: object) -> RequestEnvelope:
    base: dict[str, object] = {
        "request_id": "r1", "tenant_id": "dev-local", "actor": "alice",
        "channel": Channel.CLI, "text": "why is the query slow",
        "idempotency_key": "idem-1", "environment_id": "dev",
    }
    return RequestEnvelope(**(base | overrides))


@pytest.fixture
async def task(store: InMemoryTaskStore, context):
    return await store.create_task(envelope=_envelope(), context=context)


async def test_create_is_idempotent_by_key(store, context) -> None:
    first = await store.create_task(envelope=_envelope(), context=context)
    second = await store.create_task(envelope=_envelope(), context=context)
    assert first.task_id == second.task_id
    assert first.version == second.version


async def test_idempotency_key_is_scoped_per_tenant(store, context) -> None:
    """审核 #6 同路径缺口：V1 用全局键表，跨租户同键会共用一个任务。"""
    a = await store.create_task(envelope=_envelope(), context=context)
    other = context.model_copy(update={"tenant_id": "other-tenant"})
    b = await store.create_task(
        envelope=_envelope(tenant_id="other-tenant"), context=other,
    )
    assert a.task_id != b.task_id


async def test_idempotency_key_is_scoped_per_environment(store, context) -> None:
    a = await store.create_task(envelope=_envelope(), context=context)
    other = context.model_copy(update={"environment_id": "staging"})
    b = await store.create_task(
        envelope=_envelope(environment_id="staging"), context=other,
    )
    assert a.task_id != b.task_id


async def test_same_key_with_a_different_request_is_rejected(store, context) -> None:
    """否则第二个不同的请求会静默搭上第一个任务的结果。"""
    await store.create_task(envelope=_envelope(), context=context)
    with pytest.raises(IdempotencyConflict):
        await store.create_task(
            envelope=_envelope(text="a completely different question"), context=context,
        )


async def test_get_unknown_task_raises(store) -> None:
    with pytest.raises(TaskNotFound):
        await store.get("no-such-task")


async def test_cas_success_bumps_version(store, task) -> None:
    result = await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.PLANNING,
    )
    assert result.applied is True
    assert result.rejection is None
    assert result.winner.version == task.version + 1


async def test_cas_failure_returns_the_storage_winner(store, task) -> None:
    await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.PLANNING,
    )
    stale = await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.RUNNING,
    )
    assert stale.applied is False
    assert stale.rejection is TransitionRejection.VERSION_MISMATCH
    assert stale.winner.status is TaskStatus.PLANNING
    assert stale.winner.version == task.version + 1


async def test_illegal_transition_is_rejected(store, task) -> None:
    result = await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.SUCCEEDED,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.ILLEGAL_TRANSITION
```

`tests/security/test_lease_fencing.py`：

```python
"""租约与 fencing 的闭合规则。

V1 只在"调用方传了 token"时才校验，等于不传即绕过（审核 #6）。这里把五种
组合全部钉死，其中两条是 V1 的缺口。
"""

import pytest

from xiaowei_agent.contracts import TaskStatus, TransitionRejection

pytestmark = pytest.mark.security


async def test_second_holder_is_refused_while_lease_is_live(store, task) -> None:
    assert await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30) is None


async def test_expired_lease_can_be_taken_over_with_a_higher_token(
    store, task, clock,
) -> None:
    first = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    second = await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    assert second is not None
    assert second.fencing_token > first.fencing_token


async def test_renew_keeps_the_same_token(store, task) -> None:
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    renewed = await store.renew_lease(
        task_id=task.task_id, owner="w1",
        fencing_token=granted.fencing_token, ttl_seconds=30,
    )
    assert renewed is not None
    assert renewed.fencing_token == granted.fencing_token


async def test_renew_by_a_non_owner_is_refused(store, task) -> None:
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    assert await store.renew_lease(
        task_id=task.task_id, owner="w2",
        fencing_token=granted.fencing_token, ttl_seconds=30,
    ) is None


async def test_renew_after_expiry_is_refused(store, task, clock) -> None:
    """过期后必须重新 acquire，否则旧 worker 可以复活一个已被抢占的租约。"""
    granted = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    assert await store.renew_lease(
        task_id=task.task_id, owner="w1",
        fencing_token=granted.fencing_token, ttl_seconds=30,
    ) is None


async def test_stale_token_write_is_rejected(store, task, clock) -> None:
    old = await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    clock.advance(seconds=31)
    await store.acquire_lease(task_id=task.task_id, owner="w2", ttl_seconds=30)
    current = await store.get(task.task_id)
    result = await store.transition(
        task_id=task.task_id, expected_version=current.version,
        to_status=TaskStatus.PLANNING, fencing_token=old.fencing_token,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.STALE_FENCING_TOKEN


async def test_write_without_token_under_live_lease_is_rejected(store, task) -> None:
    """V1 缺口：不传 token 即跳过 fencing 校验。"""
    await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30)
    current = await store.get(task.task_id)
    result = await store.transition(
        task_id=task.task_id, expected_version=current.version,
        to_status=TaskStatus.PLANNING,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


async def test_token_without_any_live_lease_is_rejected(store, task) -> None:
    """持有一个不存在的租约同样是违规。"""
    result = await store.transition(
        task_id=task.task_id, expected_version=task.version,
        to_status=TaskStatus.PLANNING, fencing_token=1,
    )
    assert result.applied is False
    assert result.rejection is TransitionRejection.LEASE_NOT_HELD


async def test_transition_without_any_lease_is_allowed(store, task) -> None:
    """created → planning 发生在取租约之前，必须放行，否则任务无法启动。"""
    result = await store.transition(
        task_id=task.task_id, expected_version=task.version,
        to_status=TaskStatus.PLANNING,
    )
    assert result.applied is True


async def test_terminal_task_cannot_acquire_lease(store, task) -> None:
    """已终态的任务不应再被任何 worker 领走。"""
    await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.CANCELED,
    )
    assert await store.acquire_lease(task_id=task.task_id, owner="w1", ttl_seconds=30) is None
```

`tests/security/test_terminal_protection.py`：

```python
"""终态不可被后到事件覆盖，且必须由存储层承重（ARCHITECTURE §7.3）。"""

import pytest

from xiaowei_agent.contracts import TERMINAL_STATUSES, TaskOutcome, TaskStatus, TransitionRejection

pytestmark = pytest.mark.security


def test_terminal_set_matches_architecture() -> None:
    assert {s.value for s in TERMINAL_STATUSES} == {
        "succeeded", "failed", "rejected", "canceled", "indeterminate",
    }


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES, key=lambda s: s.value))
async def test_no_transition_out_of_any_terminal_status(store, task, terminal) -> None:
    await _drive_to_terminal(store, task.task_id, terminal)
    current = await store.get(task.task_id)
    for target in TaskStatus:
        result = await store.transition(
            task_id=task.task_id, expected_version=current.version, to_status=target,
        )
        assert result.applied is False
        assert result.rejection is TransitionRejection.TERMINAL_PROTECTED
        assert result.winner.status is terminal


async def test_terminal_protection_wins_over_version_mismatch(store, task) -> None:
    """拒绝原因必须指向最具体的违规，否则真实原因会被掩盖。"""
    await store.transition(
        task_id=task.task_id, expected_version=task.version, to_status=TaskStatus.CANCELED,
    )
    result = await store.transition(
        task_id=task.task_id, expected_version=999, to_status=TaskStatus.RUNNING,
    )
    assert result.rejection is TransitionRejection.TERMINAL_PROTECTED


def test_task_outcome_rejects_non_terminal_status() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskOutcome(task_id="t1", status=TaskStatus.RUNNING, terminal_reason=None,
                    evidence_refs=(), render_ref=None)
```

- [ ] **步骤 2：确认失败后实现 `contracts/task.py`**

要点：`TERMINAL_STATUSES`、`ALLOWED_TRANSITIONS`（用 `MappingProxyType` 包装）、`TaskRecord`、`TransitionResult`（`applied` 与 `rejection` 互斥校验）、`LeaseGrant`、`TaskOutcome`（终态校验）。`TaskRecord` 新增 `request_digest: StrictStr`，用于幂等冲突检测。

- [ ] **步骤 3：实现 `persistence/fake.py`**

```python
"""单进程 TaskStore 实现。

**真实执行**：幂等作用域、expected-version 检查、返回存储层 winner、
lease/fencing 闭合规则、终态保护、迁移表闭集。
**不宣称**：跨进程原子性、崩溃恢复、PostgreSQL 级隔离——这些属于 M4。

用一把 asyncio.Lock 串行化全部写入，使单进程内的 CAS 语义确定；这不等于
分布式原子性，测试与文档都不这样表述。

**拒绝顺序是有语义的**：终态保护 → 租约/fencing → 版本 → 迁移合法性。
终态放最前，使"违反终态保护"永远以 TERMINAL_PROTECTED 报出，不会被版本
不匹配掩盖成一个看起来正常的并发结果；租约放在版本之前，是因为"拿着陈旧
token 的 worker"比"版本落后"更具体，审计需要看到前者。
"""

IS_FAKE: Final[bool] = True


class InMemoryTaskStore:
    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._records: dict[str, TaskRecord] = {}
        self._by_key: dict[tuple[str, str, str], str] = {}
        self._next_token = 1
        self._lock = asyncio.Lock()

    def _lease_is_live(self, record: TaskRecord) -> bool:
        return (
            record.lease_owner is not None
            and record.lease_expires_at is not None
            and record.lease_expires_at > self._clock()
        )

    async def create_task(
        self, *, envelope: RequestEnvelope, context: RequestContext
    ) -> TaskRecord:
        digest = content_digest(canonical_json(envelope.model_dump(mode="json")).decode())
        scope = (context.tenant_id, context.environment_id, envelope.idempotency_key)
        async with self._lock:
            existing_id = self._by_key.get(scope)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.request_digest != digest:
                    raise IdempotencyConflict(
                        "idempotency key reused for a different request"
                    )
                return existing
            record = TaskRecord(
                task_id=str(uuid.uuid4()), tenant_id=context.tenant_id,
                environment_id=context.environment_id, actor=context.actor,
                idempotency_key=envelope.idempotency_key, request_digest=digest,
                status=TaskStatus.CREATED, version=0,
            )
            self._records[record.task_id] = record
            self._by_key[scope] = record.task_id
            return record

    async def transition(
        self, *, task_id: str, expected_version: int, to_status: TaskStatus,
        fencing_token: int | None = None, terminal_reason: str | None = None,
    ) -> TransitionResult:
        async with self._lock:
            current = self._require(task_id)
            if current.status in TERMINAL_STATUSES:
                return TransitionResult(
                    applied=False, winner=current,
                    rejection=TransitionRejection.TERMINAL_PROTECTED)

            live = self._lease_is_live(current)
            if live and fencing_token is None:
                return TransitionResult(
                    applied=False, winner=current,
                    rejection=TransitionRejection.LEASE_NOT_HELD)
            if live and fencing_token != current.fencing_token:
                return TransitionResult(
                    applied=False, winner=current,
                    rejection=TransitionRejection.STALE_FENCING_TOKEN)
            if not live and fencing_token is not None:
                return TransitionResult(
                    applied=False, winner=current,
                    rejection=TransitionRejection.LEASE_NOT_HELD)

            if current.version != expected_version:
                return TransitionResult(
                    applied=False, winner=current,
                    rejection=TransitionRejection.VERSION_MISMATCH)
            if to_status not in ALLOWED_TRANSITIONS[current.status]:
                return TransitionResult(
                    applied=False, winner=current,
                    rejection=TransitionRejection.ILLEGAL_TRANSITION)

            updated = current.model_copy(update={
                "status": to_status, "version": current.version + 1,
                "terminal_reason": terminal_reason,
            })
            self._records[task_id] = updated
            return TransitionResult(applied=True, winner=updated, rejection=None)

    async def acquire_lease(
        self, *, task_id: str, owner: str, ttl_seconds: int
    ) -> LeaseGrant | None:
        async with self._lock:
            current = self._require(task_id)
            if current.status in TERMINAL_STATUSES:
                return None            # 已终态的任务不应再被任何 worker 领走
            if self._lease_is_live(current) and current.lease_owner != owner:
                return None
            token = self._next_token
            self._next_token += 1
            expires = self._clock() + dt.timedelta(seconds=ttl_seconds)
            self._records[task_id] = current.model_copy(update={
                "lease_owner": owner, "lease_expires_at": expires,
                "fencing_token": token,
            })
            return LeaseGrant(task_id=task_id, owner=owner,
                              expires_at=expires, fencing_token=token)

    async def renew_lease(
        self, *, task_id: str, owner: str, fencing_token: int, ttl_seconds: int
    ) -> LeaseGrant | None:
        async with self._lock:
            current = self._require(task_id)
            if not self._lease_is_live(current):
                return None            # 过期后必须重新 acquire
            if current.lease_owner != owner or current.fencing_token != fencing_token:
                return None
            expires = self._clock() + dt.timedelta(seconds=ttl_seconds)
            self._records[task_id] = current.model_copy(
                update={"lease_expires_at": expires})
            return LeaseGrant(task_id=task_id, owner=owner,
                              expires_at=expires, fencing_token=fencing_token)

    def _require(self, task_id: str) -> TaskRecord:
        try:
            return self._records[task_id]
        except KeyError as exc:
            raise TaskNotFound(task_id) from exc
```

- [ ] **步骤 4：实现 `tests/fakes/clock.py`**

```python
"""可手动推进的时钟；使租约过期测试无需 sleep。"""

import datetime as dt


class ManualClock:
    def __init__(self, start: dt.datetime | None = None) -> None:
        self._now = start or dt.datetime(2026, 9, 2, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self._now

    def advance(self, *, seconds: int) -> None:
        self._now += dt.timedelta(seconds=seconds)
```

- [ ] **步骤 5：TDD 反证 —— 撤掉五个承重保护**

| 变异 | 预期转红 |
| --- | --- |
| 删除终态保护分支 | `test_no_transition_out_of_any_terminal_status` |
| 把 `live and fencing_token is None` 改回"不传即跳过" | `test_write_without_token_under_live_lease_is_rejected` |
| 删除 `not live and fencing_token is not None` 分支 | `test_token_without_any_live_lease_is_rejected` |
| 幂等键表改回全局单键 | `test_idempotency_key_is_scoped_per_tenant` |
| 删除 `acquire_lease` 的终态检查 | `test_terminal_task_cannot_acquire_lease` |
| 删除 `request_digest` 比对 | `test_same_key_with_a_different_request_is_rejected` |

- [ ] **步骤 6：四条命令全绿后提交**

```bash
git add src/xiaowei_agent/contracts/task.py src/xiaowei_agent/persistence tests/fakes/clock.py tests/contract/test_task_store_contract.py tests/security/test_terminal_protection.py tests/security/test_lease_fencing.py
git commit -m "feat(persistence): 闭合的 lease/fencing 规则、作用域化幂等与终态保护"
```

---

### T11：Runner 契约、trace 事件、渲染投影与里程碑收口

**根因背景**：落地审核 #4 的最后一项（`runners/fake.py`）、#5 的枚举集中定义验证、依赖方向承重，以及 ADR-009。

**文件**：
- 创建：`src/xiaowei_agent/contracts/trace_events.py`、`render.py`、`external_input.py`
- 创建：`src/xiaowei_agent/runners/__init__.py`、`runner.py`、`fake.py`
- 创建：`src/xiaowei_agent/observability/__init__.py`、`sink.py`
- 创建：`docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md`
- 测试：`tests/unit/test_render_payload.py`、`tests/security/test_trace_event_redaction.py`、`tests/security/test_fake_isolation.py`、`tests/security/test_module_layering.py`
- 修改：`ARCHITECTURE.md`、`AGENTS.md`、`README.md`、`AGENT_HANDOFF.md`

- [ ] **步骤 1：写失败测试 —— trace 脱敏、fake 隔离、依赖方向**

`tests/security/test_trace_event_redaction.py`：

```python
"""trace/audit 事件的 detail 只放已脱敏短标签，不放原始 payload。

复用下沉后的 redaction 模块，避免第二套会漂移的脱敏规则。
"""

import datetime as dt

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import PipelineStage, StageOutcome, TraceEvent

pytestmark = pytest.mark.security


def _event(detail: dict[str, str]) -> TraceEvent:
    return TraceEvent(
        event_id="e1", trace_id="0" * 32, task_id="t1", stage=PipelineStage.GATEWAY,
        outcome=StageOutcome.OK, occurred_at=dt.datetime(2026, 9, 2, tzinfo=dt.UTC),
        capability_id=None, step_id="s1", policy_revision="policy-2026-09-01",
        error=None, detail=detail,
    )


def test_detail_values_are_redacted_on_construction() -> None:
    event = _event({"upstream": "password=hunter2 rows=3"})
    assert "hunter2" not in event.detail["upstream"]
    assert "***" in event.detail["upstream"]


def test_detail_rejects_oversized_values() -> None:
    """detail 是标签不是载荷；超长即拒绝，防止有人把整个响应塞进 trace。"""
    with pytest.raises(ValidationError):
        _event({"payload": "x" * 1024})


def test_detail_is_deeply_immutable() -> None:
    event = _event({"k": "v"})
    with pytest.raises(TypeError):
        event.detail["k"] = "tampered"  # type: ignore[index]


def test_stage_enum_covers_the_nine_architecture_stages() -> None:
    assert {s.value for s in PipelineStage} == {
        "intent", "resolver", "planner", "admission", "gateway",
        "evidence", "reflection", "rendering", "lifecycle",
    }


def test_trace_event_does_not_import_logging_module() -> None:
    """contracts 必须依赖下沉后的 redaction，而不是 log（审核裁定）。"""
    import ast
    import inspect
    from pathlib import Path

    from xiaowei_agent.contracts import trace_events

    source = Path(inspect.getfile(trace_events)).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        assert (module or "") != "xiaowei_agent.log"
```

`tests/security/test_fake_isolation.py`：

```python
"""fake 随包发布（DEVELOPMENT_PLAN §7 M2 明文要求），因此必须可被审计地隔离。

代价是"fake 可能被接进真实路径"，用三条断言抵消：每个 fake 模块自我标识、
不从包入口导出、且没有任何生产模块导入它。
"""

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"
_FAKE_MODULES = ("tools.fake", "persistence.fake", "runners.fake")


@pytest.mark.parametrize("name", _FAKE_MODULES)
def test_every_fake_module_declares_is_fake(name: str) -> None:
    module = importlib.import_module(f"xiaowei_agent.{name}")
    assert getattr(module, "IS_FAKE", False) is True


@pytest.mark.parametrize("name", _FAKE_MODULES)
def test_fake_is_not_exported_from_its_package_init(name: str) -> None:
    package, _, _ = name.partition(".")
    init = (_SRC / package / "__init__.py").read_text(encoding="utf-8")
    assert "fake" not in init, f"{package}/__init__.py 不得导出 fake 实现"


def test_no_production_module_imports_a_fake() -> None:
    fake_paths = {_SRC / f"{n.replace('.', '/')}.py" for n in _FAKE_MODULES}
    offenders: list[tuple[str, str]] = []
    for path in _SRC.rglob("*.py"):
        if path in fake_paths:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            if isinstance(node, ast.Import):
                module = node.names[0].name
            if (module or "").endswith(".fake"):
                offenders.append((str(path.relative_to(_SRC)), module or ""))
    assert not offenders, f"生产模块不得导入 fake: {offenders}"
```

`tests/security/test_module_layering.py`：

```python
"""模块依赖必须单向：contracts 是叶子，其余模块只依赖 contracts 与标准库。

依赖方向一旦反转，"契约不随框架变化"就失效。
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src" / "xiaowei_agent"

_ALLOWED_INTERNAL = {
    # contracts 是叶子：对内零依赖（脱敏已下沉为 xiaowei_agent.redaction）。
    "contracts": {"xiaowei_agent.contracts", "xiaowei_agent.redaction"},
    "capabilities": {"xiaowei_agent.contracts", "xiaowei_agent.capabilities"},
    "planning": {"xiaowei_agent.contracts", "xiaowei_agent.planning"},
    "governance": {"xiaowei_agent.contracts", "xiaowei_agent.planning",
                   "xiaowei_agent.governance"},
    "tools": {"xiaowei_agent.contracts", "xiaowei_agent.planning", "xiaowei_agent.tools"},
    "persistence": {"xiaowei_agent.contracts", "xiaowei_agent.planning",
                    "xiaowei_agent.persistence"},
    "runners": {"xiaowei_agent.contracts", "xiaowei_agent.runners"},
    "observability": {"xiaowei_agent.contracts", "xiaowei_agent.observability"},
}


def _internal_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        module = node.module if isinstance(node, ast.ImportFrom) else None
        if isinstance(node, ast.Import):
            module = node.names[0].name
        if (module or "").startswith("xiaowei_agent"):
            found.add(".".join((module or "").split(".")[:2]))
    return found


@pytest.mark.parametrize("package", sorted(_ALLOWED_INTERNAL))
def test_package_only_imports_allowed_internal_modules(package: str) -> None:
    allowed = _ALLOWED_INTERNAL[package]
    offenders: list[tuple[str, str]] = []
    for path in (_SRC / package).rglob("*.py"):
        for module in _internal_imports(path):
            if module not in allowed:
                offenders.append((str(path.relative_to(_SRC)), module))
    assert not offenders, f"{package} 出现非法内部依赖: {offenders}"


def test_contracts_never_depend_on_implementations() -> None:
    """契约反向依赖实现，会让替换实现牵动契约。"""
    banned = {"xiaowei_agent.runners", "xiaowei_agent.tools", "xiaowei_agent.persistence",
              "xiaowei_agent.capabilities", "xiaowei_agent.planning",
              "xiaowei_agent.governance", "xiaowei_agent.log"}
    for path in (_SRC / "contracts").rglob("*.py"):
        assert not (_internal_imports(path) & banned), path
```

- [ ] **步骤 2：实现 `contracts/trace_events.py`、`render.py`、`external_input.py`**

```python
# contracts/trace_events.py（关键部分）
from xiaowei_agent.contracts.base import Contract, FrozenStrMap, StrictStr, frozen_map
from xiaowei_agent.redaction import scrub_text   # 下沉后的叶子模块，不再是 log

DetailValue = Annotated[str, Field(max_length=256)]


class TraceEvent(Contract):
    event_id: StrictStr
    trace_id: StrictStr
    task_id: str | None
    stage: PipelineStage
    outcome: StageOutcome
    occurred_at: _dt.datetime
    capability_id: str | None
    step_id: str | None
    policy_revision: str | None
    error: AgentError | None
    detail: FrozenStrMap

    @model_validator(mode="after")
    def _redact_detail(self) -> Self:
        scrubbed = {k: scrub_text(v) for k, v in self.detail.items()}
        if scrubbed == dict(self.detail):
            return self
        # 必须显式 frozen_map()：model_copy 不重新校验，直接写回普通 dict 会
        # 绕过 FrozenMap 的包装，使 detail 重新变成可变对象。
        return self.model_copy(update={"detail": frozen_map(scrubbed)})
```

> **同类风险提示**：凡在 `model_validator(mode="after")` 中用 `model_copy(update=...)`
> 写回**映射**字段，都必须显式调用 `frozen_map()`——`model_copy` 不触发校验，
> `AfterValidator` 不会再跑一次。写回 `tuple` 字段（如 `ResolvedTarget.resource_ids`）
> 不受影响，因为 tuple 本身已不可变。T11 的 `test_detail_is_deeply_immutable`
> 就是这条规则的承重测试。

`render.py` 提供 `RenderSection`、`RenderPayload`（`answer`、`sections`、`next_steps`、`status`、`refs`）；`external_input.py` 提供 `ExternalInput`（`kind`、`approval_ref`、`user_text: ExternalContent | None`）。

- [ ] **步骤 3：实现 `runners/runner.py`、`runners/fake.py`、`observability/sink.py`**

```python
# runners/runner.py —— 签名与 ARCHITECTURE §5.6 逐字一致
class WorkflowRunner(Protocol):
    async def start(self, task_id: str) -> TaskOutcome: ...

    async def resume(
        self, task_id: str, external_input: ExternalInput | None = None
    ) -> TaskOutcome: ...
```

```python
# runners/fake.py
"""最小同步 fake runner 测试替身（DEVELOPMENT_PLAN §7 M2 明文要求置于 runners/）。

**它不执行任何步骤**——步骤执行是 M3 的 DeterministicStepRunner。它只按脚本
驱动 TaskStore 走到一个终态，用于验证 Runner 契约、终态语义与 TaskOutcome
形状。因此它既不持有 ToolGateway，也不接触 adapter：**adapter 调用次数恒为 0**。
"""

IS_FAKE: Final[bool] = True


class ScriptedRunner:
    def __init__(
        self, store: TaskStore, *, outcome_status: TaskStatus,
        terminal_reason: str | None = None,
    ) -> None:
        if outcome_status not in TERMINAL_STATUSES:
            raise ValueError("ScriptedRunner requires a terminal outcome status")
        self._store = store
        self._status = outcome_status
        self._reason = terminal_reason

    async def start(self, task_id: str) -> TaskOutcome: ...
    async def resume(
        self, task_id: str, external_input: ExternalInput | None = None
    ) -> TaskOutcome: ...
```

对应契约测试 `tests/contract/test_runner_contract.py`：`ScriptedRunner` 每次迁移都携带存储层返回的 `winner.version`；驱动到终态后再次 `resume` 必须得到 `TERMINAL_PROTECTED` 而不是覆盖终态。

- [ ] **步骤 4：写 ADR-009**

`docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md`，按既有 ADR 结构（背景 / 决策 / 后果 / 备选方案与否决理由 / 回滚）记录四项冻结：

- **D1** `plan_hash` 的规范输入集：含 `effect_class`、`condition`、`budget`，`PLAN_SCHEMA_VERSION = 1`；覆盖完备性由「字段→键」映射表与安全测试承重。
- **D2** `ExecutionPlan` 绑定单一 capability，步骤不携带 capability 字段；跨 capability 计划须递增 schema version 并另立 ADR。
- **D3** `plan_hash` / `target_fingerprint` 不作为 `ExecutionPlan` 字段，绑定值存于 `ApprovalRequest`（含 `policy_revision`）。
- **D4** 工具准入形状：`AdmissionCertificate` 同时绑定步骤身份与 `tool_call_hash`；`_E1_EXECUTION_ENABLED` 为 M0–M7 的硬闸。

回滚方式：每项均给出"改哪一个常量/字段 + 需要同步更新的测试与文档"。

- [ ] **步骤 5：同步四份文档**

- **`ARCHITECTURE.md` §7.1**：canonical 形状的 `ordered_steps` 元素增加 `effect_class` 与 `condition`，顶层增加 `budget`；注明 `plan_schema_version` 首个取值为 `1`；注明步骤不携带 capability 字段。
- **`ARCHITECTURE.md` §6**：`ExecutionPlan` 行字段改为 `plan_schema_version、capability_id、capability_version、steps、policy_profile、policy_revision、budget`，约束列注明两个指纹由 `planning` 按需计算；`ApprovalRequest` 行补 `policy_revision`；新增 `AdmissionCertificate` 行（含 `tool_call_hash`）。
- **`ARCHITECTURE.md` §16**：ADR 索引增加 ADR-009。
- **`AGENTS.md`** 落点表新增两行：`trace/audit 事件出口 | observability/ | 在业务模块内各自实现采集`；`脱敏规则 | redaction.py | 在 log/contracts 中各写一份`。
- **`README.md`** 目录树把 M2 已建立的包改注，新增 `redaction.py`、`governance/`、`observability/`、`tests/contract/`。**不改动架构图**（M0 已复核其与执行链一致）。
- **`AGENT_HANDOFF.md`**：阶段、M2 基线 SHA 与 CI run；§2 补入 ADR-009 的四项结论；§5 下一步改为"为 M3 单独编写详细实施计划并获批"；§7「不要盲改」新增五条（见下）；§8 更新验证记录与残余风险。

**新增的「不要盲改」条目**：

1. 不要给 `ExecutionPlan` 或 `PlanStep` 加字段却不更新 `planning` 的「字段→hash 键」映射表。
2. 不要把 `plan_hash` / `target_fingerprint` 改成 `ExecutionPlan` 的字段。
3. 不要在 `capabilities/effect.py` 之外直接构造 `PlanStep`；用 `build_plan_step()`。
4. 不要为了让某个 adapter 跑通而放宽 `_E1_EXECUTION_ENABLED`。
5. 不要把 `test_domain_layer_has_no_third_party_client_import` 的扫描范围扩大到 `tools/` 或 `persistence/`，也不要为了让某个模块通过而把它从领域层名单里删掉。

- [ ] **步骤 6：深档验收**

本里程碑触及 `governance/`、`planning/`、`tools/`，按 `AGENTS.md`「提交前验收」不得降档：

```bash
python -m pytest -q
python -m pytest -m security -q
ruff check .
mypy src
```

四条全部 exit 0，记录尾部输出与精确 SHA。

- [ ] **步骤 7：提交并开 PR**

```bash
git add src/xiaowei_agent/contracts src/xiaowei_agent/runners src/xiaowei_agent/observability docs/adr/ADR-009-plan-hash-approval-binding-and-tool-admission.md ARCHITECTURE.md AGENTS.md README.md AGENT_HANDOFF.md tests
git commit -m "feat(runners): Runner 契约与 fake 替身；docs: ADR-009 与四份真源同步"
```

PR 描述附四条命令真实尾部输出与全部 TDD 反证记录；**PR 描述不替代真实 diff**。

## 8. 非目标

M2 **明确不做**，出现即视为超范围：

- 不实现 `CapabilityResolver`、`PlanCompiler`、`StepAdmission`、`ToolPolicy`、`SQLGuard`、`ApprovalGate`、`DeterministicStepRunner`、`EvidenceBuilder`、Reflection 或 `XiaoweiRuntime` —— 全部属于 M3。M2 的 `governance/binding.py` 只交付**纯校验函数**，不是 ApprovalGate。
- 不引入 `sqlglot`、数据库驱动、`SQLAlchemy`、`Alembic`、FastAPI 或模型 SDK。
- 不创建 `docker-compose.yml`、`tests/integration/`、`tests/evals/`、`docs/CAPABILITIES.md`。
- 不实现 trace/audit 的**采集后端**（只定义事件形状与 `TraceSink` Protocol）。
- 不建立关键词总表、能力 DSL 框架或 shadow 路由。
- 不实现任何可触达被管运维目标的 adapter；不发起任何网络调用。
- 不把 `test.synthetic.write` 注册为真实能力——它只存在于测试夹具。
- 不在 CI 中引入 PostgreSQL 或 Compose（ADR-007 D8）。
- 不修改 `.github/workflows/ci.yml`。
- **不改动 M1 的任何一行脱敏测试**：T1 是纯迁移，若需要改测试就说明重构做错了。

## 9. 验收报告格式

按 [DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) §9 输出四段：

- **已验证**：四条命令的实际调用、exit code、精确 SHA、尾部输出；**全部 20 组 TDD 反证**（T1 回归基准 1 组、T5 四组、T6 三组、T7 三组、T8 五组、T10 六组）各自的转红与还原后转绿证据。
- **只读推理**：无运行证据的判断，例如"本形状足以支撑 M4 的 PostgreSQL 实现"——在 M4 之前这只是设计推理。
- **未覆盖**：未连接的环境、未实现的故障路径、未创建的测试目录。
- **残余风险**：即使全部 gate 通过仍存在的限制及升级条件，至少包含：
  1. `ToolResult` 的私有性只封堵四条实用路径，`object.__setattr__` 与模块重定义无法在语言层封堵。
  2. 闭集 `StepConditionKind` 是否覆盖 M3 实际需要的条件种类，要到 M3 才可证。
  3. `InMemoryTaskStore` 的 CAS 语义只在单进程内成立，跨进程原子性要到 M4 的 PostgreSQL 实现才可证。
  4. 全部架构约束（Reflection 边界、E1 分类、能力扩展成本）仍未经过真实闭环检验。

M2 结束时全部能力仍为 `declared`，**不得表述为 `tests` 之外的任何级别**——M2 没有可执行能力，只有契约。

## 10. 计划自审（V2）

**覆盖度**。DEVELOPMENT_PLAN §7 M2 的九项交付物与七条测试门逐条对应：

| 交付物 | 任务 |
| --- | --- |
| contracts 全部 DTO | T2/T3/T4/T8/T9/T10/T11 |
| `AnswerabilityVerdict` | T9 |
| 最小步骤级 trace/audit 事件契约 | T11 |
| `planning`：canonical JSON、两个指纹与固定向量 | T5 |
| `tools`：Gateway Protocol、`AdapterResponse`、私有工厂、fake/recording adapter | T8 |
| `capabilities`：Registry snapshot、Resolver Protocol、分类唯一来源 | T3/T6 |
| `runners`：`WorkflowRunner` 契约与最小 fake runner | T11 |
| `persistence`：CAS/lease/fencing 交互形状 | T10 |
| 承重的单进程 fake TaskStore | T10 |

| 测试门 | 任务 |
| --- | --- |
| 单元：不可变 DTO、枚举、规范化、hash 稳定性、敏感字段排除 | T2/T5 |
| 契约：Gateway 外部不能伪造 `ToolResult` | T8 |
| 安全：模型字段污染 | T3 |
| 安全：目标不稳定 | T4（构造层）+ T7（绑定层） |
| 安全：未知 policy revision | **T7**（V1 未覆盖，V2 已补齐） |
| 安全：`ExternalContent` 注入 | T2 |
| fake TaskStore：CAS 成功/失败采纳 winner/过期 lease/旧 token/终态后到 | T10 |
| hash 与敏感字段排除的 TDD 反证 | T5 |
| E1 分类承重 | T6 + T8 |
| Reflection 越权拒绝 | T9 |

**类型一致性**。跨任务引用的名称已统一并逐一核对：`EffectClass`、`side_effect`、`capability_version`（仅 `CapabilitySpec` 自身字段用 `version`）、`compute_plan_hash` / `compute_target_fingerprint` / `compute_tool_call_hash`、`AdmissionCertificate`、`TransitionResult.winner`、`StepResultStatus`（不再是 `TaskStatus`）、`PRIOR_STEP_RESULT_IS`（不再是 `prior_step_status_is`）、`expected_result`（不再是 `expected_status`）、`verify_plan_effects`（不再是 `verify_step_effect`）、`InMemoryTaskStore`（不再是 `FakeTaskStore`）。

**V1 遗留的三个缺口现状**：

1. 「未知 policy revision」—— **已解决**，T7 交付 `verify_policy_revision` 与 5 条测试。
2. 固定向量占位符 —— **已解决**，改为直接写出规范 JSON 字节串，测试自包含。
3. T3/T4 循环引用 —— **已解决**，随移除 step 级 capability 字段一并消失；任务恢复严格线性。

**V2 仍存在、需在复审时确认的两点**：

1. **`governance/binding.py` 超出 DEVELOPMENT_PLAN §7 M2 交付物的字面清单**。它是为满足同文件"未知 policy revision"测试门所必需的最小实现。若复审认为该测试门应整体移到 M3，则删除 T7 并在 DEVELOPMENT_PLAN 中显式修订该测试门——**但不能两者都不做**。
2. **`observability/` 是 `AGENTS.md` 落点表之外的新顶层包**。T11 步骤 5 会把它补进落点表。若复审认为不应新增顶层包，替代方案是把 `TraceSink` 并入 `contracts/trace_events.py`；请在批准时指明。

## 11. 执行说明

本计划**不自行进入实现**。按 [DEVELOPMENT_PLAN.md](../../DEVELOPMENT_PLAN.md) §8：

1. 项目负责人与 Codex 复审本版，确认十项处置与第 10 节两点。
2. 先写 ADR-009 并获批（本计划已把它作为 T11 的交付物，若要求前置可提到 T0）。
3. 批准后从最新 `main` 创建**单一分支** `claude/m2-contract-kernel`，按 **T1 → T11 的顺序**逐任务 TDD 落地。每个任务结束四条命令必须全绿才进入下一个。
4. 每个任务一个提交，同时是审查 checkpoint；全部完成后开**一个 PR**，附四条命令真实尾部输出与 20 组 TDD 反证记录。
5. 合入后按第 9 节输出 M2 验收报告，更新 `AGENT_HANDOFF.md`。

## 12. 版本记录

- **V1**（2026-09-02）：初版，10 个任务。Codex 审核判定不可开工，列出 10 项阻断与 8 项裁定。
- **V2**（2026-09-02，本版）：按根因重写。任务重排为严格线性 11 个、单分支单 PR；新增 `redaction.py` 下沉、`governance/binding.py`、`FrozenMap` 深不可变、三张 hash 覆盖映射表、`tool_call_hash`、闭合 fencing 规则、`StepResultStatus`；fake 迁入业务包；移除 `PlanStep` 的 capability 字段与 `src/` 内的 `assert`；扫描口径收敛到领域层。
