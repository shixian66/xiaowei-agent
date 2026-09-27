"""F1 设计评审中发现的跨层安全缺口必须在规格里有单一闭环。"""

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
    assert positions == sorted(positions), f"F1 规格调用顺序错误：{terms}"


def _table_row(section: str, marker: str) -> tuple[str, ...]:
    for line in section.splitlines():
        if line.startswith("|") and marker in line:
            return tuple(cell.strip() for cell in line.strip("|").split("|"))
    raise AssertionError(f"F1 规格缺少决策表行：{marker}")


def test_f1_spec_defines_one_guarded_sql_and_streaming_result_path() -> None:
    text = _spec()

    for required in (
        "HydratedQuery",
        "ResultChunkSink",
        "SSCursor",
        "同名列",
        "ToolResult.data_view",
        "TaskSubmission",
    ):
        assert required in text, f"F1 规格缺少执行或结果链约束：{required}"

    _assert_terms(
        _section(text, "### 8.5 会话与流式读取"),
        "SSCursor.close",
        "耗尽未读结果",
        "直接关闭本次独占",
    )


def test_f1_spec_defines_non_replayable_recovery_and_durable_concurrency() -> None:
    text = _spec()

    for required in (
        "PREVIOUS_ATTEMPT_UNCERTAIN",
        "waiting_target_slot",
        "fencing",
        "连接关闭",
        "INDETERMINATE",
    ):
        assert required in text, f"F1 规格缺少恢复或并发约束：{required}"

    recovery = _section(text, "## 10. 一次尝试、崩溃恢复和取消")
    assert _table_row(recovery, "已有未提交 started") == (
        "已有未提交 started，无论 task fencing 是否变化",
        "无法证明 SQL 未发送或结果未产生",
        "inspect 在 slot 前返回 PREVIOUS_ATTEMPT_UNCERTAIN → "
        "TaskStatus.INDETERMINATE；Gateway 调用次数为 0",
    )
    assert _table_row(recovery, "已有 committed Step attempt") == (
        "已有 committed Step attempt",
        "结果已成为任务事实",
        "inspect 在 slot 前返回 ADOPT_COMMITTED_RESULT；sealed result 只做幂等 "
        "activation，不重新查询",
    )


def test_f1_spec_defers_a_busy_target_without_blocking_or_spending_attempt_budget() -> None:
    text = _spec()

    section = _section(text, "### 8.4 跨 worker slot")
    _assert_terms(
        section,
        "非阻塞",
        "schedule_deferral",
        "next_attempt_at",
        "task_failure_count",
        "同一轮继续",
        "处理其他任务",
        "run_with_task_heartbeat",
        "XiaoweiRuntime",
        "Gateway.invoke",
        "受信 keyword-only",
        "inspect_step_execution 相同的恢复/预算判定",
    )
    assert "第 2 步的恢复/预算" not in section
    sequence = section[section.index("WorkflowRunner 是唯一协调者") :]
    _assert_ordered_terms(
        sequence,
        "inspect_step_execution",
        "水合 ToolCall/HydratedQuery",
        "完成 StepAdmission",
        "ensure_slot_wait_window",
        "check_slot_wait_deadline",
        "TargetQueryLeaseStore.try_acquire",
        "begin_step_attempt",
        "Gateway.invoke",
    )
    assert _table_row(section, "BUSY schedule_deferral") == (
        "BUSY schedule_deferral",
        "不变",
        "不创建",
        "不消耗",
        "结束并轮换 fencing",
    )
    _assert_terms(
        _section(text, "## 10. 一次尝试、崩溃恢复和取消"),
        "begin_step_attempt",
        "Task attempt",
        "Step attempt",
        "没有 started",
        "不消耗 max_tool_calls",
    )


def test_f1_spec_keeps_query_bytes_outside_scalar_tool_call() -> None:
    section = _section(_spec(), "### 5.4 从引用到唯一 ToolCall")

    assert "把 QueryEnvelope 放入本次 ToolCall.typed_args" not in section
    assert _table_row(section, "`ToolCall.typed_args`") == (
        "`ToolCall.typed_args`",
        "否，只允许 JSON 标量",
        "引用、hash、target_fingerprint、config_revision 和预算标量",
    )
    assert _table_row(section, "`HydratedQuery`") == (
        "`HydratedQuery`",
        "是",
        "仅进程内；bytes 的 SHA-256 必须等于 ToolCall.sql_hash",
    )
    _assert_ordered_terms(
        section,
        "HydratedQuery；",
        "构造只含标量的 ToolCall",
        "StepAdmission 分别接收 ToolCall 与 HydratedQuery",
        "ToolGateway 重算 tool_call_hash，并再次校验 HydratedQuery",
    )


def test_f1_spec_uses_the_locked_parser_and_a_raw_sql_transport() -> None:
    text = _spec()

    _assert_terms(_section(text, "### 2.1 十项阻断意见"), "sqlglot 30.17.0")
    _assert_terms(
        _section(text, "### 5.1 Web SQL 草稿与确认"),
        "application/sql",
        "原始请求体",
        "1_048_576",
        "api_request_body_limit_bytes",
        "JSON 转义",
    )


def test_f1_spec_allows_internal_read_sql_without_a_database_allowlist() -> None:
    text = _spec()

    ast_section = _section(text, "### 7.3 AST 闭集")
    allowed, rejected = ast_section.split("拒绝：", maxsplit=1)
    _assert_terms(
        allowed,
        "SHOW DATABASES",
        "SHOW",
        "DESC",
        "EXPLAIN",
        "default_catalog.db.table",
        "information_schema",
    )
    assert "SHOW DATABASES 及其别名" not in rejected
    assert "allowed_database_names" not in text
    _assert_terms(
        rejected,
        "DDL",
        "写入",
        "session",
        "副作用",
        "外部 Catalog",
        "SHOW DATABASES FROM",
    )
    _assert_terms(
        _section(text, "### 2.5 负责人确认的策略调整"),
        "未来 DDL",
        "新 capability",
        "审批",
        "readback",
    )


def test_f1_spec_defines_an_exact_relation_denylist_and_permission_boundary() -> None:
    text = _spec()

    snapshot = _section(text, "### 6.1 ResourceSnapshot")
    _assert_terms(
        snapshot,
        "blocked_relation_names",
        "默认空",
        "database.object",
        "表、视图和物化视图",
        "SELECT 权限",
    )

    ast_section = _section(text, "### 7.3 AST 闭集")
    _assert_terms(
        ast_section,
        "精确命中",
        "SELECT",
        "EXPLAIN",
        "DESC",
        "SHOW CREATE",
        "SHOW COLUMNS",
        "SHOW TABLES",
        "只列名称",
        "不递归展开视图定义",
        "数据库 credential",
    )

    admin = _section(text, "### 8.2 Admin 可调值")
    _assert_terms(
        admin,
        "Web Admin",
        "blocked_relation_names",
        "默认空",
        "database.object",
        "通配符",
        "正则",
        "restart_required",
        "删除条目",
        "二次确认",
    )


def test_f1_spec_fences_late_chunk_writes() -> None:
    text = _spec()

    _assert_terms(
        _section(text, "### 5.5 结果只走 Gateway 管理的流式 sink"),
        "run_coroutine_threadsafe",
        "背压",
        "abort",
        "迟到写入",
        "fencing",
    )


def test_f1_spec_closes_channel_target_and_web_prerequisites() -> None:
    text = _spec()

    for required in (
        "F1-NL",
        "Web 显式 SQL 模式",
        "入口只传 `resource_id`",
        "不热加载",
        "Web 规格 §11.2 六项",
    ):
        assert required in text, f"F1 规格缺少入口、target 或 Web 前置约束：{required}"

    for forbidden in (
        "SchemaContextStore",
        "等待中的任务保留 queued 状态",
        "安全的 StarRocks JOIN 分布提示",
    ):
        assert forbidden not in text, f"F1 规格仍保留未闭合设计：{forbidden}"
