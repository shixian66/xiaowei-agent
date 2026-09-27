"""F1 设计 v8 的承重决定必须在规格里有单一闭环，且不复活 v8 已删除的机制。"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = (
    _ROOT
    / "docs/superpowers/specs/2026-09-27-f1-starrocks-readonly-query-design.md"
)


def _spec() -> str:
    return _SPEC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """按标题读取一个二/三级章节，避免把别处的同名术语当成闭环。"""
    start = text.index(heading)
    remainder = text[start + len(heading) :]
    next_heading = re.search(r"(?m)^#{2,3} ", remainder)
    end = len(text) if next_heading is None else start + len(heading) + next_heading.start()
    return text[start:end]


def _assert_terms(section: str, *terms: str) -> None:
    for term in terms:
        assert term in section, f"F1 规格章节缺少约束：{term}"


def _assert_ordered_terms(section: str, *terms: str) -> None:
    _assert_terms(section, *terms)
    positions = [section.index(term) for term in terms]
    assert positions == sorted(positions), f"F1 规格顺序错误：{terms}"


def _table_row(section: str, marker: str) -> tuple[str, ...]:
    for line in section.splitlines():
        if line.startswith("|") and marker in line:
            return tuple(cell.strip() for cell in line.strip("|").split("|"))
    raise AssertionError(f"F1 规格缺少决策表行：{marker}")


def _current_body(text: str) -> str:
    """去掉 §2 修订记录后的现行正文；修订记录会按名称引用 v7 机制。"""
    start = text.index("## 2. v8 修订记录")
    end = text.index("## 3. 产品范围")
    return text[:start] + text[end:]


def test_f1_spec_records_the_owner_minimal_decisions() -> None:
    record = _section(_spec(), "## 2. v8 修订记录")
    for row, v8_value in (
        ("target 排队", "全部取消"),
        ("提交方式", "一步提交"),
        ("SQL 上限", "65_536 bytes"),
        ("结果写入", "一次读完"),
        ("只读语句登记", "代码内只读语句清单"),
        ("worker", "最多同时处理 4 个任务"),
        ("连接前置校验", "F1 不做 digest 比对"),
    ):
        assert v8_value in _table_row(record, f"| {row} |")[2], row


def test_f1_spec_does_not_revive_removed_v7_machinery() -> None:
    body = _current_body(_spec())
    for retired in (
        "TargetQueryLeaseStore",
        "schedule_deferral",
        "WorkflowDeferred",
        "ThreadsafeResultChunkWriter",
        "ResultChunkSink",
        "confirm_sql_artifact",
        "ReadonlyStatementRegistry",
        "verified_min_version",
        "application/sql",
        "`staging`",
        "1_048_576",
    ):
        assert retired not in body, f"F1 规格现行正文仍含 v8 已删除的机制：{retired}"


def test_f1_spec_defines_one_guarded_sql_and_result_path() -> None:
    text = _spec()
    _assert_ordered_terms(
        _section(text, "## 1. 结论"),
        "TaskStore.submit_sql_query",
        "CapabilityResolver",
        "PlanCompiler",
        "WorkflowRunner",
        "StepAdmission(ToolPolicy → SQLGuard confirmed_readonly)",
        "ToolGateway",
        "commit_step_result",
    )
    _assert_terms(
        _section(text, "### 5.4 从引用到唯一 ToolCall"),
        "`ToolCall.typed_args` 仍只接受 JSON 标量",
        "HydratedQuery",
        "Gateway 调用次数为 0",
        "`asyncio.to_thread`",
    )
    _assert_terms(
        _section(text, "### 5.5 结果一次读完，随步骤结果同事务提交"),
        "`QueryResultBuffer`",
        "ColumnSpec(ordinal, name, type)",
        "迟到写入被丢弃",
        "**同一事务**",
        "AdapterResponse.payload 与 ToolResult.data_view 必须为空",
    )


def test_f1_spec_submits_in_one_transaction() -> None:
    submit = _section(_spec(), "### 5.1 Web 一步提交")
    _assert_ordered_terms(
        submit,
        "advisory lock",
        "同一幂等键已提交过相同内容",
        "写 SqlArtifact",
        "建 task",
        "`pending` 状态的结果行",
        "`requester_owner` grant",
    )
    _assert_terms(submit, "**同一个 PostgreSQL 事务**", "任一步失败整体回滚", "1..65_536 bytes")


def test_f1_spec_keeps_no_replay_on_the_existing_step_journal() -> None:
    recovery = _section(_spec(), "## 10. 不重放与崩溃恢复")
    _assert_terms(
        recovery,
        "`max_tool_calls=1`",
        "`BUDGET_EXHAUSTED`",
        "Gateway 调用次数为 0",
        "结果未知",
    )


def test_f1_spec_bounds_worker_concurrency() -> None:
    worker = _section(_spec(), "### 8.4 worker 有界并发")
    _assert_terms(
        worker,
        "`worker_max_concurrent_tasks`，默认 4，范围 1..16",
        "只在有空位时才调用 `begin_task_attempt`",
        "`run_with_task_heartbeat`",
        "停机时停止领取",
        "`db_pool_size + db_pool_max_overflow >= 2 × 并发数 + 1`",
    )


def test_f1_spec_proves_readonly_with_ast_and_an_in_code_list() -> None:
    proof = _section(_spec(), "### 7.3 只读证明")
    _assert_terms(
        proof,
        "`Select`、`Union`、`Intersect`、`Except`",
        "代码内只读语句清单",
        "不用“以 SHOW 开头”放行",
        "`error_code=READONLY_STATEMENT_NOT_SUPPORTED`",
        "零 SQL 发送",
        "登记支持不等于给账号增权",
        "黑名单**不提供元数据保密**",
    )
    _assert_terms(
        _section(_spec(), "### 7.2 原文 token 扫描"),
        "`/*+` 或 `/*!` 拒绝",
        "`INTO OUTFILE` 拒绝",
    )


def test_f1_spec_keeps_the_hard_limits() -> None:
    limits = _section(_spec(), "### 8.1 系统硬上限")
    for row, value in (
        ("保存预览行", "1000"),
        ("保存预览字节", "20 MiB"),
        ("server query timeout", "180 秒"),
        ("SQL 原文", "65_536 bytes UTF-8"),
        ("worker 同时处理任务数", "4"),
    ):
        assert _table_row(limits, f"| {row} |")[1] == value, row


def test_f1_spec_closes_channel_target_and_web_prerequisites() -> None:
    text = _spec()
    for required in (
        "F1-NL",
        "Web 显式 SQL 页面是 F1 唯一提交入口",
        "不构造 ResolvedTarget",
        "不热加载",
        "Web §11.2 前置",
        "不依赖 ADR-005",
    ):
        assert required in text, f"F1 规格缺少入口、target 或 Web 前置约束：{required}"
    for forbidden in (
        "SchemaContextStore",
        "等待中的任务保留 queued 状态",
        "安全的 StarRocks JOIN 分布提示",
        "未批准时 F1 不开放 /results",
    ):
        assert forbidden not in text, f"F1 规格仍保留未闭合设计：{forbidden}"
