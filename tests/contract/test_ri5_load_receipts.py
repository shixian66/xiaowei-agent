"""每个进程只为**自己**实际启用且需要的 Provider 写加载回执。

回执是"这个服务正在跑第几代配置"的唯一证据。写错归属比不写更糟：页面会显示
"已生效"，而那个服务其实从没读过这一代。
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from tests.fakes.web_auth import EmptyProviderState

from xiaowei_agent.application.integration_state import (
    SERVICE_CHANNEL_WORKER,
    SERVICE_FEISHU_LISTENER,
    SERVICE_WORKER,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.interfaces import feishu_listener as listener_module
from xiaowei_agent.interfaces import feishu_worker as channel_worker_module
from xiaowei_agent.interfaces import local_stack as local_stack_module
from xiaowei_agent.interfaces import worker as worker_module

_GEMINI_KEY = "gemini-unit-" + "test-key"
_FEISHU_SECRET = "feishu-unit-" + "test-secret"


def _config_file(tmp_path: Path, generation: int = 6) -> Path:
    path = tmp_path / "integrations.json"
    path.write_text(
        json.dumps(
            {
                "generation": generation,
                "gemini": {"enabled": True, "api_key": _GEMINI_KEY},
                "feishu": {
                    "enabled": True,
                    "app_id": "cli_unit",
                    "app_secret": _FEISHU_SECRET,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class _Stack:
    """只带回执写入所需字段的替身；其余装配与本用例无关。"""

    def __init__(self, receipts: dict[tuple[str, str], Any]) -> None:
        self.provider_state = EmptyProviderState()
        self.load_receipts = receipts
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("module", "entry", "builder", "service_name", "provider"),
    [
        (worker_module, "run_worker", "build_postgres_local_stack", SERVICE_WORKER, "gemini"),
        (
            listener_module,
            "_run",
            "build_postgres_feishu_listener_stack",
            SERVICE_FEISHU_LISTENER,
            "feishu",
        ),
        (
            channel_worker_module,
            "_run",
            "build_postgres_channel_worker_stack",
            SERVICE_CHANNEL_WORKER,
            "feishu",
        ),
    ],
    ids=["worker", "listener", "channel-worker"],
)
def test_each_process_records_its_own_receipt_before_serving(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    entry: str,
    builder: str,
    service_name: str,
    provider: str,
) -> None:
    from xiaowei_agent.contracts import LoadReceipt

    receipts = {(service_name, provider): LoadReceipt(generation=6, status="loaded")}
    stack = _Stack(receipts)
    served: list[object] = []

    async def build(**_: object) -> _Stack:
        return stack

    async def serve(**_: object) -> int:
        # 回执必须在**开始服务之前**落库：页面第一次打开就要看到这一代。
        served.append(stack.provider_state.recorded[:])
        return 0

    # worker 在模块顶层就绑定了装配函数，listener / channel worker 在 ``_run``
    # 里才导入；两种绑定都要打到。
    target = module if hasattr(module, builder) else local_stack_module
    monkeypatch.setattr(target, builder, build, raising=True)
    monkeypatch.setattr(module, "configure_logging", lambda _: None, raising=False)
    for name in ("serve_worker", "serve_listener", "serve_channel_worker"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, serve)

    settings = Settings(environment_id="dev")
    assert asyncio.run(getattr(module, entry)(settings)) == 0
    assert stack.provider_state.recorded == [receipts]
    assert served == [[receipts]]


def test_the_listener_stack_carries_only_its_own_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反例：同一份配置文件里 Gemini 也是开的，但 listener 不为它背书。"""
    from xiaowei_agent.interfaces.integration_config_file import (
        DEFAULT_INTEGRATION_CONFIG_PATH,
    )
    from xiaowei_agent.interfaces.provider_consumption import load_provider_credentials

    path = _config_file(tmp_path)
    settings = Settings(
        environment_id="dev",
        feishu_listener_enabled=True,
        feishu_tenant_key="tenant",
        feishu_bot_open_id="bot",
        feishu_identity_file=str(tmp_path / "identities.json"),
        gemini_enabled=True,
    )
    assert DEFAULT_INTEGRATION_CONFIG_PATH != str(path)

    _, receipts = load_provider_credentials(settings=settings, path=str(path))

    # 这个进程装配了 listener 与 worker 两条链路时才会有两条回执；
    # 只开 listener 时 gemini 那条不得出现在这里。
    assert set(receipts) == {
        (SERVICE_FEISHU_LISTENER, "feishu"),
        (SERVICE_WORKER, "gemini"),
    }
    assert all(receipt.generation == 6 for receipt in receipts.values())
