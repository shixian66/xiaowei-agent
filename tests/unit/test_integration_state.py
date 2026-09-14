"""页面状态的五步顺序判定。

状态机是纯函数：它不读文件、不查库、不猜。所有事实都由调用方显式传入，
因此这里能把"顺序"本身钉死——顺序错了，同一组输入会落到另一个状态。
"""

from xiaowei_agent.application.integration_state import (
    LoadReceipt,
    TestResult,
    compute_display_state,
)
from xiaowei_agent.application.integration_state import (
    ProviderDisplayState as S,
)
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
        "unconfigured",
        "pending_restart",
        "pending_test",
        "available",
        "test_failed",
    ]


def test_a_required_service_without_any_receipt_is_pending_restart() -> None:
    """两个服务都需要它时，只有一个上报回执不算生效。"""
    assert (
        _state(
            required_service_names=frozenset({"worker", "feishu_listener"}),
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


def test_receipts_for_another_provider_do_not_count() -> None:
    """反例：回执的键是 (service, provider)，只看 service 名就会串台。"""
    assert (
        _state(receipts={("worker", "feishu"): _loaded()}) is S.PENDING_RESTART
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


def test_pending_restart_wins_over_a_passing_test() -> None:
    """顺序断言：2 优先于 3/4——没加载就不能因为旧代次测过而显示可用。"""
    assert (
        _state(receipts={}, test=TestResult(status="passed", generation=2))
        is S.PENDING_RESTART
    )
