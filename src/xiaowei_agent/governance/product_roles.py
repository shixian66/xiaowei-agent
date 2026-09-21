"""产品角色 → 渠道权限与管理面能力的确定性映射。

**纯函数，没有存储依赖**：每次请求重算，不缓存在会话或 principal 上。缓存会让
一次降级或禁用要等到下一次登录才生效，而"降级立刻生效"正是身份目录存在的理由
之一。

映射同时看**角色**和**认证来源**：ADR-013 把 ``MANAGE_INTEGRATIONS`` 与
``RUN_CONNECTION_TESTS`` 限制在 ``LOCAL_ADMIN`` 上，因此同一个 ADMIN 角色从飞书来
和从本地管理面来，拿到的能力集合不同。把来源省掉就等于把这条限制交给调用方记得
再过滤一次——那是"校验它有没有写错"，弱于"它根本没有机会写"。
"""

from typing import Final

from xiaowei_agent.contracts import (
    AdminCapability,
    ChannelPermission,
    IdentitySource,
    ProductRole,
)

_CHANNEL_PERMISSIONS: Final[dict[ProductRole, frozenset[ChannelPermission]]] = {
    ProductRole.ADMIN: frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
            ChannelPermission.ADMIN_ALL_SAFE_TASKS,
        }
    ),
    ProductRole.OPERATOR: frozenset(
        {
            ChannelPermission.VIEW_SAFE_TASK,
            ChannelPermission.SUBMIT_READONLY_TASK,
        }
    ),
    ProductRole.USER: frozenset({ChannelPermission.VIEW_SAFE_TASK}),
}
"""规格 §6.2 的三行表。

``USER`` 拿到 ``VIEW_SAFE_TASK`` 不等于出现"我的任务"列表：它只让后端在具体深链
请求上继续计算 owner、群成员或其他既有 ACL。前端隐藏入口不替代后端判断。
"""

_LOCAL_ADMIN_ONLY: Final[frozenset[AdminCapability]] = frozenset(
    {
        AdminCapability.MANAGE_INTEGRATIONS,
        AdminCapability.RUN_CONNECTION_TESTS,
    }
)
"""只允许 ``LOCAL_ADMIN`` 实际使用的两项配置面能力（ADR-013）。"""


def channel_permissions(*, role: ProductRole) -> frozenset[ChannelPermission]:
    """角色对应的渠道权限。

    用 ``[]`` 而不是 ``.get(role, frozenset())``：兜底默认值会让"新增了一个角色却
    忘了补映射"变成一次静默的零权限，而零权限在别处会被读成"这个人什么都不该看"，
    于是缺陷以一次误拒的形式出现在真实请求上。缺成员就该在这里 ``KeyError``，并由
    ``test_mapping_is_total_over_the_role_enum`` 在 CI 上先抓到。
    """
    return _CHANNEL_PERMISSIONS[role]


def admin_capabilities(
    *, role: ProductRole, source: IdentitySource
) -> frozenset[AdminCapability]:
    """角色 + 认证来源对应的管理面能力。

    非 ADMIN 在**任何**来源下都是空集：认证来源不能提权，本地管理面登录的
    OPERATOR 仍然是 OPERATOR。
    """
    if role is not ProductRole.ADMIN:
        return frozenset()
    if source is IdentitySource.LOCAL_ADMIN:
        return frozenset(AdminCapability)
    return frozenset(AdminCapability) - _LOCAL_ADMIN_ONLY
