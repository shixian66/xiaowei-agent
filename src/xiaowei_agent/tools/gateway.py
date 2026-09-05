"""数据面唯一工具入口。

七道闸依次生效，任一不过即拒绝且 **adapter 调用次数为 0**：

1. **凭证与调用同属一个步骤** —— 防止拿 A 步骤的凭证执行 B 步骤。
2. **凭证与调用内容一致** —— 重算 ``tool_call_hash`` 比对。凭证证明的是"**这一个**
   ToolCall 已准入"，不是"这个步骤已准入"；否则替换 ``typed_args`` 即可绕过。
3. **policy 判定为 allow**。
4. **E1 硬闸** —— 非 READ 分类一律拒绝。这把"M0-M7 对被管运维目标的 E1 调用次数
   恒为 0"从纪律变成代码事实（ADR-007 D7、ADR-009 D4）。
5. **adapter 已注册** —— 未注册即 fail-closed，不做回退。
6. **adapter 的任意异常被结构化** —— 异常文本是外部内容，原样冒泡会绕过
   ``ExternalContent``、``AgentError`` 与脱敏边界。
7. **adapter 返回值是 AdapterResponse** —— Protocol 只在静态检查时生效，运行时不拦
   任何东西；一个返回 dict 或鸭子类型对象的 adapter 能把未经契约约束的数据一路带进
   ``ToolResult``，因此必须在此显式校验。
"""

import asyncio
import datetime as _dt
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Protocol

from xiaowei_agent.contracts import (
    AdapterStatus,
    AdmissionCertificate,
    AgentError,
    EffectClass,
    ErrorCategory,
    ExternalContent,
    ExternalSource,
    FrozenMap,
    RequestContext,
    ToolCall,
    ToolCallStatus,
    ToolResult,
)
from xiaowei_agent.contracts.tool import _TOOL_RESULT_WITNESS, _TOOL_RESULT_WITNESS_KEY
from xiaowei_agent.planning import compute_tool_call_hash
from xiaowei_agent.redaction import safe_exception_text
from xiaowei_agent.tools.adapter import AdapterResponse, ToolAdapter

_E1_EXECUTION_ENABLED: Final[bool] = False
"""ADR-007 D7：M0-M7 全程禁止 E1，含非生产环境。M8 才可由独立评审开闸。"""

_STATUS_MAP: Final[Mapping[AdapterStatus, ToolCallStatus]] = MappingProxyType(
    {
        AdapterStatus.OK: ToolCallStatus.OK,
        AdapterStatus.ERROR: ToolCallStatus.ERROR,
        AdapterStatus.TIMEOUT: ToolCallStatus.TIMEOUT,
    }
)

# 确定性 error mapper：adapter 状态 → (code, category, retryable)。
# **第三方错误文本永远不进入 AgentError**——只有它的 ExternalContent 摘要作为
# cause_ref 被引用。这样错误既可追溯，又不会成为可信控制信号。
_ERROR_MAP: Final[Mapping[AdapterStatus, tuple[str, ErrorCategory, bool]]] = MappingProxyType(
    {
        AdapterStatus.ERROR: ("tool.upstream_error", ErrorCategory.UPSTREAM, True),
        AdapterStatus.TIMEOUT: ("tool.timeout", ErrorCategory.TIMEOUT, True),
    }
)


class MalformedAdapterResponseError(RuntimeError):
    """Adapter returned outside its runtime contract; message never reflects the value."""

    def __init__(self) -> None:
        super().__init__("adapter returned a malformed response")


def _map_error(status: AdapterStatus, cause: ExternalContent | None) -> AgentError | None:
    """把 adapter 状态归类为结构化错误；错误原文只留摘要引用。"""
    if status is AdapterStatus.OK:
        return None
    code, category, retryable = _ERROR_MAP[status]
    return AgentError(
        code=code,
        category=category,
        retryable=retryable,
        message_key=f"error.{code}",
        cause_ref=cause.digest if cause is not None else None,
    )


class ToolGateway(Protocol):
    """数据面唯一工具入口的**契约**。

    领域层只依赖本 Protocol，不依赖任何具体实现——这是"实现可以替换，契约不随框架
    变化"的落点。M6b 的真实 StarRocks adapter 仍经同一个 Gateway，不新增第二条
    工具路由。
    """

    async def invoke(
        self, call: ToolCall, *, context: RequestContext, admission: AdmissionCertificate
    ) -> ToolResult: ...


class DeterministicToolGateway:
    def __init__(self, adapters: Mapping[str, ToolAdapter]) -> None:
        if not adapters:
            raise ValueError("DeterministicToolGateway requires at least one adapter")
        self._adapters = dict(adapters)

    async def invoke(
        self, call: ToolCall, *, context: RequestContext, admission: AdmissionCertificate
    ) -> ToolResult:
        """执行一次已准入的工具调用。

        :raises PermissionError: 凭证与调用不匹配、策略拒绝或触发 E1 硬闸。
        :raises LookupError: adapter 未注册。
        :raises MalformedAdapterResponseError: adapter 返回值不是 ``AdapterResponse``。
        """
        if not isinstance(admission, AdmissionCertificate):
            raise PermissionError("admission certificate does not match this step")
        if admission.step_id != call.step_id or admission.operation != call.operation:
            raise PermissionError("admission certificate does not match this step")
        if admission.tool_call_hash != compute_tool_call_hash(call):
            raise PermissionError("admission certificate does not match this call")
        # policy revision 必须在 allow 之前检查：凭证一旦过期，它携带的 allow
        # 就不再代表任何有效判断——先看 allow 等于用一份已失效的策略结论放行。
        #
        # 这条校验此前缺失，凭证因此永久有效：policy 收紧后，旧凭证仍能调用
        # adapter，"policy 变化不能静默让旧授权继续生效"在执行边界上没有落实。
        if admission.policy_decision.policy_revision != context.policy_revision:
            raise PermissionError("admission certificate was issued under a stale policy revision")
        if not admission.policy_decision.allow:
            raise PermissionError("policy denied")
        if admission.effect_class is not EffectClass.READ and not _E1_EXECUTION_ENABLED:
            raise PermissionError("E1 execution is disabled for M0-M7")
        adapter = self._adapters.get(call.gateway)
        if adapter is None:
            # 不回显 call.gateway：网关名来自调用方，拒绝路径不得把外部输入
            # 拼进异常消息（与 ValidationError 不回显 input 同一条不变量）。
            raise LookupError("adapter not registered for this call")

        try:
            response = await asyncio.wait_for(
                adapter.execute(call, context=context), call.timeout_seconds
            )
        except TimeoutError:
            return self._issue(
                context,
                status=ToolCallStatus.TIMEOUT,
                data_view=(),
                source=call.gateway,
                limitations=("adapter timed out",),
                error=_map_error(AdapterStatus.TIMEOUT, None),
            )
        except Exception as exc:
            # adapter 的任意异常都不得原样冒泡：异常文本是**外部内容**，直接抛出
            # 会绕过 ExternalContent、AgentError 与脱敏边界，把上游的密码、连接串
            # 带进调用方的 traceback。
            #
            # 只捕 ``Exception``：``asyncio.CancelledError`` 继承自 ``BaseException``，
            # 因此协作式取消照常向上传播，不会被吞掉。
            #
            # M8 开放 E1 后需重新审视：写操作抛异常时目标状态不可知，应映射为
            # ``INDETERMINATE`` 而非 ``ERROR``。M2 只读，故此处用 ERROR。
            # 不直接 str(exc)：见 redaction.safe_exception_text —— __str__ 抛异常
            # 的错误对象会让这条 except 分支本身再抛，整条"异常必被结构化吸收"
            # 的承诺随之失效。安全原语项目里早就有，缺的是执行点调用它。
            cause = ExternalContent.capture(
                source=ExternalSource.TOOL,
                content=safe_exception_text(exc),
                captured_at=_dt.datetime.now(tz=_dt.UTC),
            )
            return self._issue(
                context,
                status=ToolCallStatus.ERROR,
                data_view=(),
                source=call.gateway,
                limitations=("adapter raised an unexpected exception",),
                error=_map_error(AdapterStatus.ERROR, cause),
            )
        if not isinstance(response, AdapterResponse):
            # 不回显类型名：返回值来自 adapter，其类可以是运行时构造的，类名因此
            # 是外部数据。理由同 canonical._normalise。
            raise MalformedAdapterResponseError
        # adapter 出错时不得返回空成功：状态与结构化错误一起传出去，且不把半截
        # payload 当作证据。
        return self._issue(
            context,
            status=_STATUS_MAP[response.status],
            data_view=response.payload if response.status is AdapterStatus.OK else (),
            source=response.source,
            limitations=(),
            error=_map_error(response.status, response.error),
        )

    @staticmethod
    def _issue(
        context: RequestContext,
        *,
        status: ToolCallStatus,
        data_view: tuple[FrozenMap, ...],
        source: str,
        limitations: tuple[str, ...],
        error: AgentError | None,
    ) -> ToolResult:
        """唯一的 ToolResult 构造点。

        用 ``model_validate`` 而非 ``ToolResult(**payload)``：后者的异构字典会被
        mypy 推断为 ``dict[str, object]``，逐字段报不兼容。凭证仍经同一个
        ``mode="before"`` 校验器，构造边界不变。
        """
        return ToolResult.model_validate(
            {
                _TOOL_RESULT_WITNESS_KEY: _TOOL_RESULT_WITNESS,
                "status": status,
                "data_view": data_view,
                "raw_ref": None,
                "source": source,
                "limitations": limitations,
                "trace_id": context.trace_id,
                "error": error,
            }
        )
