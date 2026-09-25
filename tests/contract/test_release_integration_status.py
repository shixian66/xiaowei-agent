"""W5：配置已保存、但本次部署没有任何进程消费它时，管理面只能说"尚未接入"。

provider-off release 下 AI 与飞书两个域都没有消费者（Gemini、OAuth、listener、
channel-worker 全部关闭）。此时页面若显示"已加载"，管理员会以为凭据已经在用——
这正是计划要求避免的虚报。resources 域恒由 worker 读取并签回执，不受影响。
"""

from pathlib import Path

import pytest

from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import ConfigDomain
from xiaowei_agent.interfaces.web_app import _integration_domain_status
from xiaowei_agent.persistence.provider_state import (
    CheckName,
    LoadReceipt,
    ProviderStateSnapshot,
)

_EMPTY = ProviderStateSnapshot(receipts={}, tests={})


def _release() -> Settings:
    return Settings.model_validate(
        {
            "environment_id": "dev",
            "runtime_profile": "release",
            "starrocks_adapter_mode": "disabled",
            "web_app_enabled": True,
            "web_public_origin": "https://sso.example.invalid",
        }
    )


@pytest.mark.parametrize(
    ("domain", "checks"),
    [
        (ConfigDomain.AI, (CheckName.GEMINI_CONNECTION,)),
        (ConfigDomain.FEISHU, (CheckName.FEISHU_CREDENTIALS, CheckName.FEISHU_OAUTH)),
    ],
)
def test_saved_provider_without_a_consumer_is_not_applicable(
    domain: ConfigDomain, checks: tuple[CheckName, ...]
) -> None:
    status = _integration_domain_status(
        domain=domain,
        configured=True,
        generation=3,
        checks=checks,
        settings=_release(),
        snapshot=_EMPTY,
    )
    assert status.configured is True
    assert status.load_status == "not_applicable"
    assert status.restart_required is False


def test_unconfigured_provider_stays_unconfigured() -> None:
    status = _integration_domain_status(
        domain=ConfigDomain.AI,
        configured=False,
        generation=0,
        checks=(CheckName.GEMINI_CONNECTION,),
        settings=_release(),
        snapshot=_EMPTY,
    )
    assert status.load_status == "unconfigured"


def test_a_consumer_still_waits_for_and_reports_its_receipt() -> None:
    """对照组：有消费者时语义不变——没有回执是待重启，回执齐了才是已加载。"""
    settings = Settings.model_validate({"environment_id": "dev", "gemini_enabled": True})
    pending = _integration_domain_status(
        domain=ConfigDomain.AI,
        configured=True,
        generation=3,
        checks=(CheckName.GEMINI_CONNECTION,),
        settings=settings,
        snapshot=_EMPTY,
    )
    loaded = _integration_domain_status(
        domain=ConfigDomain.AI,
        configured=True,
        generation=3,
        checks=(CheckName.GEMINI_CONNECTION,),
        settings=settings,
        snapshot=ProviderStateSnapshot(
            receipts={("worker", ConfigDomain.AI): LoadReceipt(generation=3, status="loaded")},
            tests={},
        ),
    )
    assert (pending.load_status, pending.restart_required) == ("pending_restart", True)
    assert (loaded.load_status, loaded.restart_required) == ("loaded", False)


def test_release_resources_are_still_read_by_the_worker() -> None:
    status = _integration_domain_status(
        domain=ConfigDomain.RESOURCES,
        configured=True,
        generation=2,
        checks=(),
        settings=_release(),
        snapshot=_EMPTY,
        required=frozenset({"worker"}),
    )
    assert status.load_status == "pending_restart"


def test_admin_page_labels_not_applicable_as_not_connected() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "src/xiaowei_agent/interfaces/web_static/admin.js"
    ).read_text(encoding="utf-8")
    assert 'not_applicable: "尚未接入"' in script


# --------------------------------------------------------------------------
# 工作台壳：release 下不再展示三个 recording 能力的快捷项
# --------------------------------------------------------------------------

_CHIPS = ("慢查询证据", "Prometheus 告警证据", "资产精确查询")


async def _workbench_html(
    tmp_path: Path, clock: object, memory_state: object, **updates: object
) -> str:
    from tests.contract.test_ri5_config_api import _build, _client, _sign_in

    built = _build(tmp_path, clock, memory_state, **updates)
    async with _client(built.app) as client:
        await _sign_in(client, built.admins)
        response = await client.get("/app")
    assert response.status_code == 200
    return response.text


async def test_release_workbench_does_not_advertise_recording_capabilities(
    tmp_path: Path, clock: object, memory_state: object
) -> None:
    html = await _workbench_html(
        tmp_path,
        clock,
        memory_state,
        runtime_profile="release",
        starrocks_adapter_mode="disabled",
    )
    for chip in _CHIPS:
        assert chip not in html, chip
    assert "当前无可执行能力" in html
    assert "慢查询" not in html


async def test_offline_workbench_keeps_its_capability_examples(
    tmp_path: Path, clock: object, memory_state: object
) -> None:
    """对照组：offline_recording 下三个 recording 能力确实可用，快捷项保留。"""
    html = await _workbench_html(tmp_path, clock, memory_state)
    for chip in _CHIPS:
        assert chip in html, chip
    assert "当前无可执行能力" not in html


def test_provider_off_shell_fails_loudly_when_the_markup_drifts() -> None:
    from xiaowei_agent.interfaces.web_app import _provider_off_workbench_shell

    with pytest.raises(RuntimeError):
        _provider_off_workbench_shell("<html>no capability strip here</html>")
