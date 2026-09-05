"""固定模板 PromQL 的参数重导出与逐字节重编译闸门。"""

from collections.abc import Mapping

from pydantic import ValidationError

from xiaowei_agent.contracts import JsonScalar, PromqlGuardRejection, PromqlSurface
from xiaowei_agent.planning.prometheus.compiler import compile_promql
from xiaowei_agent.planning.prometheus.params import PrometheusAlertParams


class PromqlGuardError(RuntimeError):
    """PromQL 准入失败；消息只包含闭集拒绝码。"""

    def __init__(self, rejection: PromqlGuardRejection) -> None:
        super().__init__(rejection.value)
        self.rejection = rejection


def verify_promql(
    *,
    promql: str,
    template_id: str,
    typed_arguments: Mapping[str, JsonScalar],
    surface: PromqlSurface,
) -> None:
    """确认查询属于声明闭集，且是参数的逐字节确定性产物。"""
    if template_id not in surface.allowed_template_ids:
        raise PromqlGuardError(PromqlGuardRejection.UNKNOWN_TEMPLATE)
    try:
        params = PrometheusAlertParams.from_typed_arguments(typed_arguments)
        expected = compile_promql(
            template_id=template_id, params=params, surface=surface
        )
    except ValidationError:
        raise PromqlGuardError(PromqlGuardRejection.INVALID_ARGUMENTS) from None
    except ValueError:
        raise PromqlGuardError(PromqlGuardRejection.INVALID_ARGUMENTS) from None
    if promql != expected:
        raise PromqlGuardError(PromqlGuardRejection.RECOMPILE_MISMATCH)
