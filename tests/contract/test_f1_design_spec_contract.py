"""F1 设计评审中发现的跨层安全缺口必须在规格里有单一闭环。"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SPEC = (
    _ROOT
    / "docs/superpowers/specs/2026-09-27-f1-starrocks-readonly-query-design.md"
)


def _spec() -> str:
    return _SPEC.read_text(encoding="utf-8")


def test_f1_spec_defines_one_guarded_sql_and_streaming_result_path() -> None:
    text = _spec()

    for required in (
        "QueryEnvelope",
        "ResultChunkSink",
        "SSCursor",
        "SSCursor.close 会耗尽未读结果",
        "直接关闭本次独占 connection",
        "同名列",
        "ToolResult.data_view",
        "TaskSubmission",
    ):
        assert required in text, f"F1 规格缺少执行或结果链约束：{required}"


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
