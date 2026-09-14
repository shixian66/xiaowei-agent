"""三个控制面探针：只验证"这份凭据现在能不能用"，不产生任何业务效果。

三条纪律贯穿本模块：

1. **开关关闭时连 client 都不构造。** 不是"构造了但不发请求"——SDK 的构造期本身
   就可能读环境、起连接池、加载凭据。判定必须在任何对象诞生之前。
2. **Provider 的原始正文一律在 ``except`` 块里丢弃。** 探针结果会被管理面原样读出
   来显示，也会写进 ``provider_test_state``。响应正文、Token、Key 一个都不能进去。
3. **探针不是能力。** 它不写任务、不写 Evidence、不碰 ToolGateway、不改 readiness。
   一次"测试连接"失败只应该让页面上多一个红色徽章。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final, Self

from pydantic import Field, model_validator

from xiaowei_agent.contracts import (
    Contract,
    ModelErrorCode,
    StrictInt,
    TestStatus,
)
from xiaowei_agent.persistence.provider_state import (
    MAX_TEST_DURATION_MS,
    ProviderTestErrorCode,
)

ProbeErrorCode = ProviderTestErrorCode
"""探针失败码**就是**写进 ``provider_test_state`` 的那个闭集，不是第二份。

两份闭集迟早会漂：多出来的成员写不进表（CHECK 拒绝），少掉的成员让探针无法表达
一种真实失败。取值域只有一个，别名只是让本模块的读者不必跳到 persistence 去认名字。
"""

PROVIDER_PROBE_TIMEOUT_SECONDS: Final[float] = 5.0
"""一次探测的出站上限。

比页面上任何一次交互都短：管理员点"测试连接"时在等，一个挂住的 Provider 不该
把 Web 进程的一个 worker 拖到浏览器自己超时。
"""

GEMINI_PROBE_INPUT: Final[str] = "xiaowei connection check"
"""发给 Gemini 的固定合成输入。

固定成模块级常量而不是拼一个随请求变化的串：探针的输入若可被调用方影响，它就成了
一条绕过 ``IntentModelPort`` 的任意文本出站通道。内容本身不含任何本机事实。
"""


class ProbeTransportError(RuntimeError):
    """真实 transport 唯一被允许抛的异常：只带闭集码。

    Provider 的 SDK 知道 401 与 503 的区别，探针不该重新学一遍；但它也**只**能把
    这点区别带过来。异常文本固定为闭集码本身，即使有人把它记进日志也不泄露什么。
    """

    def __init__(self, code: ProbeErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


ProbeTransport = Callable[..., Awaitable[object]]
"""一次出站调用。关键字参数由各探针决定，返回值只用于判"有没有回应"。

写成宽签名而不是 ``Protocol``：真实 transport 的关键字是各自固定的
（``api_key``/``contents`` 与 ``app_id``/``app_secret``），一个声明 ``**kwargs``
的 Protocol 反而与它们都不兼容。参数形状由各探针那一处 ``payload`` 字面量决定，
它就在调用点旁边，不需要类型再复述一遍。
"""


class ProbeOutcome(Contract):
    """一次探测的结果；``duration_ms`` 与写库用的上界同一个常量。

    ``passed`` 与 ``error_code`` 互斥的校验与 :class:`RecordTestCommand` 上的那条
    **是同一条不变量**，两边各写一遍是故意的：这一条挡住探针自己造出矛盾结果，
    那一条挡住任何绕过探针的写入路径。
    """

    status: TestStatus
    duration_ms: StrictInt = Field(ge=0, le=MAX_TEST_DURATION_MS)
    error_code: ProbeErrorCode | None = None

    @model_validator(mode="after")
    def _failure_facts_are_consistent(self) -> Self:
        if (self.status == "passed") != (self.error_code is None):
            raise ValueError("probe failure must carry exactly one closed error code")
        return self


def refused_outcome(code: ProbeErrorCode) -> ProbeOutcome:
    """没有发生任何出站调用，因此耗时恒为 0——不是"很快"，是"没做"。"""
    return ProbeOutcome(status="failed", duration_ms=0, error_code=code)


def _elapsed_ms(started: float) -> int:
    elapsed = int((time.monotonic() - started) * 1000)
    return min(max(elapsed, 0), MAX_TEST_DURATION_MS)


async def _timed_call(
    transport: ProbeTransport,
    *,
    timeout_seconds: float,
    payload: dict[str, object],
) -> ProbeOutcome:
    """发一次有超时的请求，把任何结局折成闭集码。

    ``except Exception`` 是**兜底而不是偷懒**：SDK 会抛出自己的异常层级，其文本常
    携带 URL、请求体或响应正文。这里既不读它、也不重新抛出，只留下 ``UNAVAILABLE``。
    """
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(transport(**payload), timeout=timeout_seconds)
    except TimeoutError:
        return ProbeOutcome(
            status="failed",
            duration_ms=_elapsed_ms(started),
            error_code=ProbeErrorCode.TIMEOUT,
        )
    except ProbeTransportError as error:
        return ProbeOutcome(
            status="failed", duration_ms=_elapsed_ms(started), error_code=error.code
        )
    except Exception:
        return ProbeOutcome(
            status="failed",
            duration_ms=_elapsed_ms(started),
            error_code=ProbeErrorCode.UNAVAILABLE,
        )
    if result is None:
        return ProbeOutcome(
            status="failed",
            duration_ms=_elapsed_ms(started),
            error_code=ProbeErrorCode.INVALID_RESPONSE,
        )
    return ProbeOutcome(status="passed", duration_ms=_elapsed_ms(started))


async def probe_gemini_connection(
    *,
    enabled: bool,
    api_key: str | None,
    transport: ProbeTransport,
    timeout_seconds: float,
) -> ProbeOutcome:
    """验证这把 Key 现在能不能换到一次真实回应。

    ``enabled`` 是 `.env` 的真实测试开关，``api_key`` 是 `integrations.json` 与
    JSON 侧开关共同决定的结果——两层都为真才会有出站。顺序不能反：开关关闭时连
    "有没有配 Key"都不该被回答，那已经是一次信息泄露。
    """
    if not enabled:
        return refused_outcome(ProbeErrorCode.REAL_TEST_DISABLED)
    if api_key is None:
        return refused_outcome(ProbeErrorCode.NOT_CONFIGURED)
    return await _timed_call(
        transport,
        timeout_seconds=timeout_seconds,
        payload={"api_key": api_key, "contents": GEMINI_PROBE_INPUT},
    )


async def probe_feishu_credentials(
    *,
    enabled: bool,
    app_id: str | None,
    app_secret: str | None,
    transport: ProbeTransport,
    timeout_seconds: float,
) -> ProbeOutcome:
    """用应用凭据换一次 token；两个字段缺一即未配置。

    飞书的开关与 Gemini 的**各自独立**：一个 Provider 的真实测试被打开，不蕴含
    另一个也被打开。合成一个开关会让管理员在只想测 Gemini 时顺带放开飞书出站。
    """
    if not enabled:
        return refused_outcome(ProbeErrorCode.REAL_TEST_DISABLED)
    if app_id is None or app_secret is None:
        return refused_outcome(ProbeErrorCode.NOT_CONFIGURED)
    return await _timed_call(
        transport,
        timeout_seconds=timeout_seconds,
        payload={"app_id": app_id, "app_secret": app_secret},
    )


GEMINI_ERROR_CODES: Final[dict[ModelErrorCode, ProbeErrorCode]] = {
    ModelErrorCode.CREDENTIAL_UNAVAILABLE: ProbeErrorCode.NOT_CONFIGURED,
    ModelErrorCode.AMBIENT_PROXY: ProbeErrorCode.UNAVAILABLE,
    ModelErrorCode.UNAUTHORIZED: ProbeErrorCode.UNAUTHORIZED,
    ModelErrorCode.FORBIDDEN: ProbeErrorCode.UNAUTHORIZED,
    ModelErrorCode.RATE_LIMITED: ProbeErrorCode.UNAVAILABLE,
    ModelErrorCode.SERVER_ERROR: ProbeErrorCode.UNAVAILABLE,
    ModelErrorCode.TRANSPORT_ERROR: ProbeErrorCode.UNAVAILABLE,
    ModelErrorCode.TIMEOUT: ProbeErrorCode.TIMEOUT,
    ModelErrorCode.INVALID_RESPONSE: ProbeErrorCode.INVALID_RESPONSE,
    ModelErrorCode.UNAVAILABLE: ProbeErrorCode.UNAVAILABLE,
}
"""模型端口闭集到探针闭集的**全映射**。

两个闭集的粒度本来就不同：模型链路要区分 429 与 503 来决定退避，探针只要回答
"现在能不能用"。写成一张全表而不是一串 ``if``，是为了让"模型侧新增一个码"在
``tests/security/test_ri5_probe_boundary.py`` 里立刻变红，而不是悄悄落到某个
``else`` 分支里被说成 ``UNAVAILABLE``。
"""


async def gemini_probe_transport(*, api_key: str, contents: str) -> object:
    """真实 Gemini 出站：复用 adapter 那组被钉死的 origin / api_version / model。

    SDK 在函数内导入：Web 进程只有管理员真的点了"测试连接"才需要它，模块导入期
    把它拉进来会让每个引用 ``provider_probe`` 的进程都背上这份依赖。
    """
    from xiaowei_agent.application.model_ports import ModelPortError
    from xiaowei_agent.interfaces.gemini_model import probe_connection

    # 先记码、出了 ``except`` 块再抛：在块内抛会把原异常挂进 ``__context__``，
    # 于是一条本该只有闭集码的异常链重新带上了 Provider 侧的对象。
    probe_failure_code: ProbeErrorCode | None = None
    try:
        await probe_connection(api_key=api_key, contents=contents)
    except ModelPortError as error:
        probe_failure_code = GEMINI_ERROR_CODES[error.code]
    if probe_failure_code is not None:
        raise ProbeTransportError(probe_failure_code)
    return True


async def feishu_probe_transport(*, app_id: str, app_secret: str) -> object:
    """真实飞书出站：只换一次 tenant access token。

    刻意不碰消息与群聊接口——"凭据可用"不需要、也不该顺带证明"能发消息"。
    """
    from xiaowei_agent.interfaces.feishu_sdk import probe_app_credentials

    succeeded, status_code = await probe_app_credentials(
        app_id=app_id, app_secret=app_secret
    )
    if succeeded:
        return True
    raise ProbeTransportError(_feishu_error_code(status_code))


def _feishu_error_code(status_code: int) -> ProbeErrorCode:
    """HTTP 层通了但凭据被拒时，飞书返回的是 200 + 业务错误码。

    因此 "200 而未 success" 必须映射成 ``UNAUTHORIZED`` 而不是 ``INVALID_RESPONSE``：
    这个接口唯一的业务失败原因就是 App ID / Secret 不对，把它说成"响应不合法"
    会让管理员去查网络而不是去查凭据。
    """
    if status_code in {200, 401, 403}:
        return ProbeErrorCode.UNAUTHORIZED
    if status_code == 408:
        return ProbeErrorCode.TIMEOUT
    if 500 <= status_code <= 599:
        return ProbeErrorCode.UNAVAILABLE
    return ProbeErrorCode.INVALID_RESPONSE


__all__ = [
    "GEMINI_ERROR_CODES",
    "GEMINI_PROBE_INPUT",
    "PROVIDER_PROBE_TIMEOUT_SECONDS",
    "ProbeErrorCode",
    "ProbeOutcome",
    "ProbeTransport",
    "ProbeTransportError",
    "feishu_probe_transport",
    "gemini_probe_transport",
    "probe_feishu_credentials",
    "probe_gemini_connection",
    "refused_outcome",
]
