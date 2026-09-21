"""产品角色 → 权限/能力的映射是纯函数，也是最容易被直觉写错的地方。

"Admin 就是全权"这个直觉在这里恰好是错的：ADR-013 把 `MANAGE_INTEGRATIONS` 与
`RUN_CONNECTION_TESTS` 限制在 `LOCAL_ADMIN` 认证来源上，因此**同一个 ADMIN 角色**
从飞书来和从本地管理面来，拿到的能力集合不同。映射函数因此必须同时看角色和来源。

本文件按**逐值相等**断言，不用 `>=`：能力闭集是安全边界，多一个成员和少一个成员
都是缺陷，而 `>=` 只能抓到后者。
"""

import pytest

from xiaowei_agent.contracts import (
    AdminCapability,
    ChannelPermission,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.governance.product_roles import (
    admin_capabilities,
    channel_permissions,
)

_CONFIG_PLANE_CAPABILITIES = frozenset(
    {
        AdminCapability.MANAGE_INTEGRATIONS,
        AdminCapability.RUN_CONNECTION_TESTS,
    }
)
"""ADR-013 只允许 `LOCAL_ADMIN` 使用的两项配置面能力。"""


def test_channel_permission_enum_is_still_exactly_three_members() -> None:
    """规格 §6.2：渠道权限闭集不因为新增管理面能力而被顺手扩展。

    扩展它会让 M7 渠道投影的既有断言全部失去意义（ADR-013）。
    """
    assert {member.value for member in ChannelPermission} == {
        "view_safe_task",
        "submit_readonly_task",
        "admin_all_safe_tasks",
    }


def test_admin_capability_enum_is_exactly_the_seven_adr_013_members() -> None:
    """逐字取自 ADR-013 的七成员清单。"""
    assert {member.value for member in AdminCapability} == {
        "manage_users",
        "manage_duty_bindings",
        "view_admin_audit",
        "view_private_task_content",
        "view_integration_status",
        "manage_integrations",
        "run_connection_tests",
    }


def test_product_role_and_user_status_are_closed() -> None:
    """两个新枚举都是闭集，没有 UNKNOWN 兜底成员。

    留一个兜底成员，就等于给"分类不出来"留了一条继续放行的路。
    """
    assert {member.value for member in ProductRole} == {"admin", "operator", "user"}
    assert {member.value for member in UserStatus} == {"active", "disabled"}


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            ProductRole.ADMIN,
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
                ChannelPermission.ADMIN_ALL_SAFE_TASKS,
            },
        ),
        (
            ProductRole.OPERATOR,
            {
                ChannelPermission.VIEW_SAFE_TASK,
                ChannelPermission.SUBMIT_READONLY_TASK,
            },
        ),
        (ProductRole.USER, {ChannelPermission.VIEW_SAFE_TASK}),
    ],
    ids=["admin", "operator", "user"],
)
def test_channel_permissions_match_the_spec_table_exactly(
    role: ProductRole, expected: set[ChannelPermission]
) -> None:
    """规格 §6.2 的三行表逐行钉死。"""
    assert channel_permissions(role=role) == frozenset(expected)


def test_local_admin_administrator_gets_every_admin_capability() -> None:
    """本地管理面的 ADMIN 是唯一能拿满七项的主体。"""
    assert admin_capabilities(
        role=ProductRole.ADMIN, source=IdentitySource.LOCAL_ADMIN
    ) == frozenset(AdminCapability)


def test_feishu_administrator_is_short_exactly_the_two_config_plane_capabilities() -> None:
    """飞书 ADMIN 恰好少两项，不多不少。

    断言"差集精确等于那两项"而不是"不含 MANAGE_INTEGRATIONS"：后者在实现顺手
    多砍一项时仍然绿。
    """
    feishu = admin_capabilities(role=ProductRole.ADMIN, source=IdentitySource.FEISHU)
    assert frozenset(AdminCapability) - feishu == _CONFIG_PLANE_CAPABILITIES
    assert feishu == frozenset(AdminCapability) - _CONFIG_PLANE_CAPABILITIES


@pytest.mark.parametrize(
    "role", [ProductRole.OPERATOR, ProductRole.USER], ids=["operator", "user"]
)
@pytest.mark.parametrize(
    "source",
    [IdentitySource.FEISHU, IdentitySource.LOCAL_ADMIN],
    ids=["feishu", "local_admin"],
)
def test_non_administrators_get_no_admin_capability_from_any_source(
    role: ProductRole, source: IdentitySource
) -> None:
    """规格 §6.3：只有作用域角色 ADMIN 能取得管理能力。

    认证来源不能把非 ADMIN 提权——本地管理面登录的 OPERATOR 仍然是 OPERATOR。
    """
    assert admin_capabilities(role=role, source=source) == frozenset()


@pytest.mark.parametrize(
    "source",
    [IdentitySource.FEISHU, IdentitySource.LOCAL_ADMIN],
    ids=["feishu", "local_admin"],
)
def test_mapping_is_total_over_the_role_enum(source: IdentitySource) -> None:
    """两个映射对角色枚举都是全函数。

    这条守的是**将来**：新增一个产品角色时，如果实现用的是 dict 查表而没有补
    新成员，这条会立刻变红；否则新角色会在某个调用点变成 KeyError，而那通常发生
    在一次真实请求上。
    """
    for role in ProductRole:
        assert isinstance(channel_permissions(role=role), frozenset)
        assert isinstance(admin_capabilities(role=role, source=source), frozenset)
