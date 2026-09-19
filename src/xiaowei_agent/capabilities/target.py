"""目标解析：``RequestContext`` + ``SlowQueryParams`` → ``ResolvedTarget``。

两条承重规则：

1. **执行上下文优先于模型线索。** ``environment_id`` 一律取自 ``RequestContext``；
   StarRocks 参数不携带目标环境。模型能改目标，等于模型能把一份已批准的计划
   指向另一套集群。
2. **环境必须解析出唯一值，否则 fail-closed。** 不取默认环境、不猜、不回退
   （ADR-007 D2）。目录里没有的环境就是解析失败。

参数里的 ``database`` / ``user_name`` / ``query_id`` **不进目标**：它们是查询的
过滤条件，不是执行目标。把它们算进指纹会让同一套集群随过滤条件产生不同目标，
审批绑定随之失去意义。
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from xiaowei_agent.contracts import (
    RequestContext,
    ResolvedTarget,
    TargetRejection,
)

PROVIDER: Final[str] = "starrocks"
RESOURCE_KIND: Final[str] = "cluster"
SELECTOR_VERSION: Final[str] = "2"

_ENVIRONMENT_DIRECTORY: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        # 非生产环境目录。这些标识只是**目录条目**，M3 不连接任何真实系统
        # （ADR-007 D4：M0-M6a 禁止真实运维目标调用）。
        "dev": ("starrocks-dev-1",),
        "test": ("starrocks-test-1", "starrocks-test-2"),
    }
)
"""环境 → 该环境下的 StarRocks 集群资源 ID。

**生产环境刻意不在其中**：生产连接与生产写当前不授权（ADR-007 D6），因此"没有
这一项"就是拒绝的实现方式，而不是靠某个 ``if env == "prod"`` 分支。
``test_production_is_not_in_the_environment_directory`` 承重。
"""

KNOWN_ENVIRONMENT_IDS: Final[tuple[str, ...]] = tuple(sorted(_ENVIRONMENT_DIRECTORY))


class TargetResolutionError(RuntimeError):
    """目标解析失败。``rejection`` 给出闭集拒绝码。

    与 ``BindingError`` 同一模式：消息里只出现枚举成员，不出现被拒的环境标识
    ——它来自调用方，可能携带外部数据。
    """

    def __init__(self, rejection: TargetRejection) -> None:
        super().__init__(rejection.value)
        self.rejection = rejection


def resolve_context_target(*, context: RequestContext) -> ResolvedTarget:
    """只从执行上下文和代码目录解析唯一 StarRocks 目标。"""
    resource_ids = _ENVIRONMENT_DIRECTORY.get(context.environment_id)
    if resource_ids is None:
        raise TargetResolutionError(TargetRejection.UNKNOWN_ENVIRONMENT)
    if not resource_ids:
        raise TargetResolutionError(TargetRejection.EMPTY_ENVIRONMENT_DIRECTORY)
    if len(resource_ids) != 1:
        raise TargetResolutionError(TargetRejection.AMBIGUOUS_ENVIRONMENT_DIRECTORY)
    return ResolvedTarget(
        tenant_id=context.tenant_id,
        environment_id=context.environment_id,
        provider=PROVIDER,
        resource_kind=RESOURCE_KIND,
        resource_ids=resource_ids,
        selector_version=SELECTOR_VERSION,
    )


def resolve_target(*, context: RequestContext, params: object) -> ResolvedTarget:
    """把执行上下文解析成唯一目标；查询参数不参与目标构成。"""
    _ = params
    return resolve_context_target(context=context)
