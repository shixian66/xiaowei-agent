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
    SERVICE_WEB,
    SERVICE_WORKER,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import ConfigDomain
from xiaowei_agent.interfaces import feishu_listener as listener_module
from xiaowei_agent.interfaces import feishu_worker as channel_worker_module
from xiaowei_agent.interfaces import local_stack as local_stack_module
from xiaowei_agent.interfaces import worker as worker_module

_GEMINI_KEY = "gemini-unit-" + "test-key"
_FEISHU_SECRET = "feishu-unit-" + "test-secret"


def _config_files(tmp_path: Path, generation: int = 6) -> dict[str, str]:
    """两域各一份固定形状文件；返回 ``load_provider_credentials`` 的路径实参。"""
    documents = {
        "ai": {"generation": generation, "gemini": {"enabled": True, "api_key": _GEMINI_KEY}},
        "feishu": {
            "generation": generation,
            "feishu": {"enabled": True, "app_id": "cli_unit", "app_secret": _FEISHU_SECRET},
        },
    }
    paths: dict[str, str] = {}
    for domain, document in documents.items():
        path = tmp_path / domain / "config.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        paths[f"{domain}_path"] = str(path)
    return paths


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
        (
            worker_module,
            "run_worker",
            "build_postgres_local_stack",
            SERVICE_WORKER,
            ConfigDomain.AI,
        ),
        (
            listener_module,
            "_run",
            "build_postgres_feishu_listener_stack",
            SERVICE_FEISHU_LISTENER,
            ConfigDomain.FEISHU,
        ),
        (
            channel_worker_module,
            "_run",
            "build_postgres_channel_worker_stack",
            SERVICE_CHANNEL_WORKER,
            ConfigDomain.FEISHU,
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
    provider: ConfigDomain,
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


def _every_switch_on(tmp_path: Path) -> Settings:
    """最不利的部署：一份 `.env` 把四条链路全打开。

    这正是跨进程背书唯一会暴露的形状——只开一条链路时，"本进程的服务集合"与
    "全局服务集合"恰好相等，错误的实现也看不出来。
    """
    return Settings(
        environment_id="dev",
        gemini_enabled=True,
        feishu_listener_enabled=True,
        channel_worker_enabled=True,
        feishu_oauth_enabled=True,
        web_app_enabled=True,
        feishu_tenant_key="tenant",
        feishu_bot_open_id="bot",
        feishu_identity_file=str(tmp_path / "identities.json"),
        web_public_origin="https://ops.example.test",
    )


@pytest.mark.parametrize(
    ("service_name", "provider"),
    [
        (SERVICE_WORKER, ConfigDomain.AI),
        (SERVICE_FEISHU_LISTENER, ConfigDomain.FEISHU),
        (SERVICE_CHANNEL_WORKER, ConfigDomain.FEISHU),
        (SERVICE_WEB, ConfigDomain.FEISHU),
    ],
    ids=["worker", "listener", "channel-worker", "web"],
)
def test_no_process_signs_a_receipt_for_a_sibling(
    tmp_path: Path, service_name: str, provider: ConfigDomain
) -> None:
    """反例：四个开关全开时，任何一个进程都不得写出兄弟进程的回执。

    回执是"这个服务正在跑第几代"的唯一证据。替别人签字的后果不是少一条记录，
    而是页面从"待应用"跳到"待测试/可用"——而那个服务可能没重启、没读过这一代，
    甚至没启动成功。
    """
    from xiaowei_agent.interfaces.integration_config_file import (
        DEFAULT_AI_CONFIG_PATH,
        DEFAULT_FEISHU_CONFIG_PATH,
    )
    from xiaowei_agent.interfaces.provider_consumption import load_provider_credentials

    paths = _config_files(tmp_path)
    assert {DEFAULT_AI_CONFIG_PATH, DEFAULT_FEISHU_CONFIG_PATH}.isdisjoint(paths.values())

    _, receipts = load_provider_credentials(
        settings=_every_switch_on(tmp_path),
        service_name=service_name,
        **paths,
    )

    assert set(receipts) == {(service_name, provider)}
    assert receipts[(service_name, provider)].generation == 6


def test_a_stack_that_attests_for_nobody_writes_no_receipt(tmp_path: Path) -> None:
    """``service_name=None`` 是"本次装配不作证"，不是"作证全部"。

    内存栈与注入凭据的离线证明走这条路。空回执在状态机里等价于"尚未加载当前
    generation"，页面显示"待应用"——保守方向，永远不会假绿。
    """
    from xiaowei_agent.interfaces.provider_consumption import load_provider_credentials

    _, receipts = load_provider_credentials(
        settings=_every_switch_on(tmp_path),
        service_name=None,
        **_config_files(tmp_path),
    )

    assert receipts == {}


def test_an_unknown_service_name_does_not_borrow_a_sibling_receipt(
    tmp_path: Path,
) -> None:
    """不在任何 Provider 消费者列表里的服务名，拿到的是空回执而不是全集。"""
    from xiaowei_agent.interfaces.provider_consumption import load_provider_credentials

    _, receipts = load_provider_credentials(
        settings=_every_switch_on(tmp_path),
        service_name="internal_api",
        **_config_files(tmp_path),
    )

    assert receipts == {}


class _FakeEngine:
    """只满足装配期签名的引擎替身；本用例不碰数据库。"""

    async def dispose(self) -> None:
        return None


def _identity_file(tmp_path: Path) -> Path:
    path = tmp_path / "identities.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tenant_id": "dev-local",
                "environment_id": "dev",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("builder", "expected_service", "extra"),
    [
        ("build_postgres_local_stack", SERVICE_WORKER, {}),
        (
            "build_postgres_feishu_listener_stack",
            SERVICE_FEISHU_LISTENER,
            {"transport": "inject"},
        ),
        (
            "build_postgres_channel_worker_stack",
            SERVICE_CHANNEL_WORKER,
            {"message_port": "inject"},
        ),
    ],
    ids=["worker", "listener", "channel-worker"],
)
def test_each_builder_declares_its_own_service_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builder: str,
    expected_service: str,
    extra: dict[str, str],
) -> None:
    """身份由**装配函数**填，不是调用方传进来的参数。

    ``load_provider_credentials`` 自己再正确，只要某个装配点报错身份，跨进程背书
    就会原样回来。这条用例钉住那一步：每个 builder 只能报自己那一个服务名。
    """
    from tests.fakes.feishu import RecordingFeishuInboundTransport

    from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

    declared: list[object] = []

    def record(**kwargs: object) -> tuple[ProviderCredentials, dict[str, object]]:
        declared.append(kwargs["service_name"])
        return (
            ProviderCredentials(
                feishu_app_id="cli_wiring",
                feishu_app_secret="wiring-" + "fixture-secret",
                gemini_api_key="AIza" + "w" * 35,
            ),
            {},
        )

    monkeypatch.setattr(local_stack_module, "load_provider_credentials", record)
    monkeypatch.setattr(
        local_stack_module, "create_database_engine", lambda _: _FakeEngine()
    )

    ports: dict[str, object] = {}
    if "transport" in extra:
        ports["transport"] = RecordingFeishuInboundTransport()
    if "message_port" in extra:
        ports["message_port"] = _NoMessages()

    settings = _every_switch_on(tmp_path)
    settings = settings.model_copy(
        update={"feishu_identity_file": str(_identity_file(tmp_path))}
    )
    stack = asyncio.run(
        getattr(local_stack_module, builder)(settings=settings, **ports)
    )
    asyncio.run(stack.aclose())

    assert declared == [expected_service]


def test_serve_web_declares_only_the_web_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Web 进程同样只为自己作证——它是最容易先启动的那个。"""
    from xiaowei_agent.interfaces import provider_consumption as provider_consumption_module
    from xiaowei_agent.interfaces import web_app as web_app_module
    from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

    declared: list[object] = []

    def record(**kwargs: object) -> tuple[ProviderCredentials, dict[str, object]]:
        declared.append(kwargs["service_name"])
        return ProviderCredentials(), {}

    async def stop_after_assembly(**_: object) -> object:
        raise local_stack_module.WebStackConfigurationError("stop after assembly")

    monkeypatch.setattr(
        provider_consumption_module, "load_provider_credentials", record
    )
    monkeypatch.setattr(
        local_stack_module, "build_postgres_web_stack", stop_after_assembly
    )
    monkeypatch.setattr(web_app_module, "configure_logging", lambda _: None)

    with pytest.raises(web_app_module._WebConfigurationError):
        asyncio.run(web_app_module.serve_web(_every_switch_on(tmp_path)))

    assert declared == [SERVICE_WEB]


def test_a_real_feishu_adapter_is_never_built_from_missing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反例：`integrations.json` 缺失时，``None`` 不得被 cast 成 ``str`` 交给 SDK。

    两个装配点原本写 ``cast(str, credentials.feishu_app_id)``。真正的失败会推迟到
    SDK 内部，异常可能带上 URL 或响应正文，而入口只允许记闭集诊断。
    """
    from xiaowei_agent.interfaces.provider_consumption import ProviderCredentials

    monkeypatch.setattr(
        local_stack_module, "create_database_engine", lambda _: _FakeEngine()
    )

    def refuse(**_: object) -> object:
        raise AssertionError("real feishu adapter must not be constructed")

    monkeypatch.setattr(
        "xiaowei_agent.interfaces.feishu_sdk.FeishuSdkInboundTransport", refuse
    )
    monkeypatch.setattr(
        "xiaowei_agent.interfaces.feishu_sdk.FeishuSdkMessageAdapter", refuse
    )

    settings = _every_switch_on(tmp_path).model_copy(
        update={"feishu_identity_file": str(_identity_file(tmp_path))}
    )
    for builder in (
        "build_postgres_feishu_listener_stack",
        "build_postgres_channel_worker_stack",
    ):
        with pytest.raises(ValueError, match="feishu credentials are not configured"):
            asyncio.run(
                getattr(local_stack_module, builder)(
                    settings=settings, credentials=ProviderCredentials()
                )
            )


class _NoMessages:
    """渠道投影端口替身；本用例只验证装配期的身份声明。"""

    async def send_to_chat(self, **_: object) -> str:  # pragma: no cover - 不被调用
        raise AssertionError("not used")

    async def send_to_user(self, **_: object) -> str:  # pragma: no cover
        raise AssertionError("not used")

    async def update_card(self, **_: object) -> None:  # pragma: no cover
        raise AssertionError("not used")
