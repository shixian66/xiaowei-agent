"""身份目录契约：字段集合、作用域归属、PII 遮蔽与命令的"写不出审计"。

这一层的全部承重点是一句话：**审计事实由 store 从命令派生，调用方不得组装**。
因此命令上不允许出现 `action`、`target_kind`、`outcome`、`effect`，也不允许出现
`created_at` / `created_by` / `event_id` —— 能被调用方指定，就能被调用方写错，而
"校验它有没有写错"永远弱于"它根本没有机会写"。

命令集合从 `DirectoryCommand` 联合里**解构出来**而不是在测试里手抄一份：手抄的
那份不会随新命令增长，于是第九个命令可以带着 `outcome` 字段安然通过。
"""

import datetime as dt
import typing

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.contracts.identity import (
    LOCAL_ADMIN_ACTOR,
    LOCAL_ADMIN_DISPLAY_NAME,
    LOCAL_ADMIN_ENVIRONMENT_ID,
    LOCAL_ADMIN_TENANT_ID,
    LOCAL_ADMIN_USER_ID,
    BindExternalIdentityCommand,
    BootstrapLocalAdminCommand,
    CreateUserCommand,
    DirectoryCommand,
    ExternalIdentity,
    LegacyIdentityMigrationEntry,
    MigrateLegacyIdentitiesCommand,
    UserAccount,
    UserRoleAssignment,
)

_NOW = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)
_OPEN_ID = "ou_" + "9f3c1d7b5a2e4680"
"""伪造的飞书 open_id。拆开写，避免在源码里构成连续可扫描串。"""

_PASSWORD_HASH = "scrypt$1$16384$8$1$" + "c2FsdHNhbHQ=" + "$" + "ZGVyaXZlZGtleQ=="


def _command_types() -> tuple[type, ...]:
    """把 `DirectoryCommand` 判别联合拆成成员类型。

    `DirectoryCommand` 是 `Annotated[A | B | ..., Field(discriminator=...)]`，因此
    先剥 `Annotated` 再取 `Union` 的成员。
    """
    inner = typing.get_args(DirectoryCommand)[0]
    return typing.get_args(inner)


def _fields(model: type) -> set[str]:
    return set(model.model_fields)


def _account() -> UserAccount:
    return UserAccount(
        user_id="u-1",
        actor="alice",
        display_name="Alice",
        status=UserStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_user_account_fields_are_exactly_the_spec_set() -> None:
    """规格 §6.1 的账号字段集合，逐字段相等。

    用 `==` 而不是 `>=`：多一个字段同样是缺陷——账号上多出来的作用域字段会让
    "作用域住在角色上"这条不变量出现第二个答案。
    """
    assert _fields(UserAccount) == {
        "user_id",
        "actor",
        "display_name",
        "status",
        "created_at",
        "updated_at",
    }


def test_user_account_is_frozen_and_rejects_unknown_fields() -> None:
    account = _account()
    with pytest.raises(ValidationError):
        account.user_id = "u-2"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        UserAccount(
            user_id="u-1",
            actor="alice",
            display_name="Alice",
            status=UserStatus.ACTIVE,
            created_at=_NOW,
            updated_at=_NOW,
            tenant_id="t-1",
        )


def test_role_assignment_carries_the_scope_not_the_account() -> None:
    """作用域住在角色上。

    这是与规格 §6.1 字段表的**有意偏离**（见计划「与规格的两处有意偏离」）：
    账号全局唯一，角色按租户/环境分授。断言两边——账号没有作用域，角色有——
    只断一边时，把作用域同时加到账号上仍会全绿。
    """
    assert {"tenant_id", "environment_id"} <= _fields(UserRoleAssignment)
    assert {"tenant_id", "environment_id"} & _fields(UserAccount) == set()


def test_external_identity_never_leaks_the_subject_ref() -> None:
    """`open_id` 两条外泄通道都要堵：`repr()` 与 `model_dump()`。

    只堵一条不会有任何反馈——这正是 RI5 `IntegrationConfig` 漏过一轮的形状。
    """
    identity = ExternalIdentity(
        user_id="u-1",
        provider=IdentitySource.FEISHU,
        tenant_id="t-1",
        environment_id="dev",
        subject_ref=_OPEN_ID,
        created_at=_NOW,
        last_seen_at=_NOW,
    )
    assert _OPEN_ID not in repr(identity)
    assert "subject_ref" not in identity.model_dump()
    assert _OPEN_ID not in identity.model_dump_json()


def test_external_identity_provider_is_restricted_to_feishu() -> None:
    """provider 是 `Literal`，不是开放枚举。

    W1a 只有飞书一个外部来源。留成整个 `IdentitySource` 就等于允许写进
    `local_admin`，而本地管理员不是"外部身份"，它没有 subject。
    """
    with pytest.raises(ValidationError):
        ExternalIdentity(
            user_id="u-1",
            provider=IdentitySource.LOCAL_ADMIN,
            tenant_id="t-1",
            environment_id="dev",
            subject_ref=_OPEN_ID,
            created_at=_NOW,
            last_seen_at=_NOW,
        )


@pytest.mark.parametrize("command", _command_types(), ids=lambda c: c.__name__)
def test_commands_cannot_stamp_their_own_time_or_identity(command: type) -> None:
    """时间与身份由 store 盖章，命令不带。

    命令自带 `created_at` 时，两次重放可以写出任意历史时间；自带 `created_by`
    时，一次调用可以把改动记在别人名下。
    """
    assert _fields(command) & {
        "created_at",
        "updated_at",
        "created_by",
        "event_id",
        "last_seen_at",
    } == set()


@pytest.mark.parametrize("command", _command_types(), ids=lambda c: c.__name__)
def test_no_command_can_carry_audit_fields(command: type) -> None:
    """审计事实全部由 store 派生，命令上一个也不能有。"""
    assert _fields(command) & {
        "action",
        "target_kind",
        "target_ref",
        "target_ref_digest",
        "outcome",
        "effect",
        "effect_role",
        "effect_status",
        "reason_code",
        "operation_id",
        "actor_user_id",
        "auth_source",
    } == set()


@pytest.mark.parametrize("command", _command_types(), ids=lambda c: c.__name__)
def test_every_command_carries_the_scope_the_store_derives_audit_from(
    command: type,
) -> None:
    """每个命令都带租户与环境。

    store 要把它们写进审计事件，而 store 不该去猜：猜出来的作用域会让一条管理
    动作记在错误的租户名下，且没有任何地方会报错。
    """
    assert {"tenant_id", "environment_id"} <= _fields(command)


def test_command_kind_discriminators_are_unique() -> None:
    """判别器互不相同，且覆盖全部成员。

    重复的判别值会让 pydantic 在联合里只保留一个分支——被吞掉的那个命令从此
    永远解析成另一个类型。
    """
    kinds = [
        typing.get_args(command.model_fields["kind"].annotation)[0]
        for command in _command_types()
    ]
    assert len(kinds) == len(set(kinds)) == 8


def test_bootstrap_command_never_exposes_the_password_hash() -> None:
    """哈希不是明文，但它是离线爆破的输入。两条通道都要关。"""
    command = BootstrapLocalAdminCommand(
        user_id=LOCAL_ADMIN_USER_ID,
        actor=LOCAL_ADMIN_ACTOR,
        display_name=LOCAL_ADMIN_DISPLAY_NAME,
        tenant_id=LOCAL_ADMIN_TENANT_ID,
        environment_id=LOCAL_ADMIN_ENVIRONMENT_ID,
        password_hash=_PASSWORD_HASH,
    )
    assert _PASSWORD_HASH not in repr(command)
    assert "password_hash" not in command.model_dump()
    assert _PASSWORD_HASH not in command.model_dump_json()


def test_migration_command_carries_the_whole_batch_and_hides_open_ids() -> None:
    """整批迁移是**一个**命令。

    一条一条发就没有"整批原子"可言：中途失败会留下一半迁移过的目录，而那一半
    已经生效的授权没有任何地方记着它该被撤销。
    """
    entry = LegacyIdentityMigrationEntry(
        user_id="u-legacy-1",
        actor="alice",
        display_name="alice",
        role=ProductRole.OPERATOR,
        subject_ref=_OPEN_ID,
    )
    command = MigrateLegacyIdentitiesCommand(
        tenant_id="t-1", environment_id="dev", entries=(entry,)
    )
    assert len(command.entries) == 1
    assert _OPEN_ID not in repr(command)
    assert _OPEN_ID not in command.model_dump_json()


def test_migration_command_rejects_an_empty_batch() -> None:
    """空批次是错误，不是 no-op。

    允许空批次，就会有一条"迁移成功"的审计事件对应零次写入——审计从此可以在
    什么都没发生时声称发生过。
    """
    with pytest.raises(ValidationError):
        MigrateLegacyIdentitiesCommand(
            tenant_id="t-1", environment_id="dev", entries=()
        )


@pytest.mark.parametrize(
    ("field", "limit"),
    [("user_id", 64), ("actor", 256), ("display_name", 128)],
    ids=["user_id-64", "actor-256", "display_name-128"],
)
def test_identifier_fields_are_bounded(field: str, limit: int) -> None:
    """三个上限的**两侧**都写死。

    只断"超一个字符被拒"不够：把上限改成 1 也照样绿。正对照（恰好等于上限必须
    被接受）是这条用例真正的承重点——它同时挡住"收得太紧"，而收得太紧会把一批
    真实旧数据永久挡在门外（见切片 C 的长度转换策略）。
    """
    base = {
        "user_id": "u-1",
        "actor": "alice",
        "display_name": "Alice",
        "tenant_id": "t-1",
        "environment_id": "dev",
        "role": ProductRole.USER,
    }
    CreateUserCommand(**{**base, field: "x" * limit})
    with pytest.raises(ValidationError):
        CreateUserCommand(**{**base, field: "x" * (limit + 1)})


def test_bind_command_requires_a_non_empty_subject_ref() -> None:
    """空 subject 不是一次合法绑定。

    允许空串就会有一行 `external_identities` 把某个账号绑到"没有主体"上，而它
    仍然满足主键与唯一约束——之后任何人只要也绑空串就会撞上它。
    """
    with pytest.raises(ValidationError):
        BindExternalIdentityCommand(
            user_id="u-1", tenant_id="t-1", environment_id="dev", subject_ref=""
        )


def test_the_local_admin_principal_consumes_the_contract_constants() -> None:
    """五个常量只有一份真源，`local_admin_auth.py` 里不再有那三个字面量。

    留着字面量就是两份真源，而两份真源迟早会在某次改租户名时只改一边——那时
    本地管理员的 principal 与它在目录里的角色行会指向两个不同的作用域。
    """
    import pathlib

    from xiaowei_agent.interfaces.local_admin_auth import LOCAL_ADMIN_PRINCIPAL

    assert LOCAL_ADMIN_PRINCIPAL.tenant_id == LOCAL_ADMIN_TENANT_ID
    assert LOCAL_ADMIN_PRINCIPAL.environment_id == LOCAL_ADMIN_ENVIRONMENT_ID
    assert LOCAL_ADMIN_PRINCIPAL.actor == LOCAL_ADMIN_ACTOR

    source = pathlib.Path(
        typing.cast(str, __import__(
            "xiaowei_agent.interfaces.local_admin_auth", fromlist=["__file__"]
        ).__file__)
    ).read_text(encoding="utf-8")
    for literal in (LOCAL_ADMIN_TENANT_ID, LOCAL_ADMIN_ENVIRONMENT_ID, LOCAL_ADMIN_ACTOR):
        assert f'"{literal}"' not in source, f"{literal} 仍以字面量留在 local_admin_auth.py"


# --- subject_ref 的上限必须只有一个 ----------------------------------------
#
# 同一个飞书 `open_id` 会经过三条链路：旧静态文档、飞书事件 DTO、OAuth 交换结果。
# 它们各写一份上限时，最窄的那一份就是一道**静默的兼容墙**：入口接得下的身份，
# 目录绑不进去，而两边单独看都言之成理。因此上限只允许有一处定义，其余全部引用它。


def _max_length(model: type, field: str) -> int | None:
    """从 pydantic 字段元数据里读出 `max_length`，而不是按拼写找字面量。"""
    for constraint in model.model_fields[field].metadata:
        bound = getattr(constraint, "max_length", None)
        if bound is not None:
            return int(bound)
    return None


_SUBJECT_REF_BOUND: int = 256
"""上限的值在这里**独立写死一次**。

只断"各处相等"是不够的：它们现在全都引用同一个常量，于是把那个常量改成 1 时它们
依然相等，而目录会再也绑不进任何一个真实 `open_id`。256 取自本次改动前飞书事件
DTO 与 OAuth 交换结果各自接受的取值——上限的职责是收得下每一个生产者。
"""


def test_every_subject_ref_on_the_identity_chain_shares_one_bound() -> None:
    from xiaowei_agent.contracts.base import CONTROLLED_PII_MAX_LENGTH
    from xiaowei_agent.interfaces.feishu_sdk import FeishuMention, FeishuMessageEvent
    from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity

    assert CONTROLLED_PII_MAX_LENGTH == _SUBJECT_REF_BOUND

    bounds = {
        "ExternalIdentity.subject_ref": _max_length(ExternalIdentity, "subject_ref"),
        "BindExternalIdentityCommand.subject_ref": _max_length(
            BindExternalIdentityCommand, "subject_ref"
        ),
        "LegacyIdentityMigrationEntry.subject_ref": _max_length(
            LegacyIdentityMigrationEntry, "subject_ref"
        ),
        "FeishuMention.subject_ref": _max_length(FeishuMention, "subject_ref"),
        "FeishuMessageEvent.sender_subject_ref": _max_length(
            FeishuMessageEvent, "sender_subject_ref"
        ),
        "FeishuOAuthIdentity.subject_ref": _max_length(
            FeishuOAuthIdentity, "subject_ref"
        ),
    }
    assert set(bounds.values()) == {_SUBJECT_REF_BOUND}, (
        f"身份链上的 subject_ref 上限不一致：{bounds}"
    )


def test_the_shared_subject_ref_bound_covers_every_producer() -> None:
    """正对照：上限必须**收得下**每一个生产者接受的取值，而不只是彼此相等。

    只断"三处相等"是不够的——把三处一起改成 1 也照样相等，而那样目录就再也绑不进
    任何一个真实 `open_id`。
    """
    from xiaowei_agent.interfaces.web_auth import FeishuOAuthIdentity

    widest = "o" * _SUBJECT_REF_BOUND
    # 入口收得下它。
    assert FeishuOAuthIdentity(subject_ref=widest).subject_ref == widest
    # 目录也收得下同一个值——这一对才是"两端闭合"。
    BindExternalIdentityCommand(
        user_id="u-1", tenant_id="t-1", environment_id="dev", subject_ref=widest
    )
