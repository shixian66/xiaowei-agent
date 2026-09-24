"""身份目录与 Admin 审计两个 Store 的**结构**断言，以及内存绑定的共享行为套件。

结构断言不需要数据库：它们钉的是"调用方有没有机会写错"，而那是签名与表面的
性质，不是运行结果。行为断言在 ``tests/suites/identity_directory.py``，由本文件
（内存）与 ``tests/integration/test_identity_directory_postgres.py``（PostgreSQL）
各跑一遍。
"""

import inspect
import typing

import pytest
from tests.suites.identity_directory import IDENTITY_DIRECTORY_CASES, bind

from xiaowei_agent.contracts.admin_audit import AdminOperationContext
from xiaowei_agent.contracts.identity import DirectoryCommand
from xiaowei_agent.persistence import fake
from xiaowei_agent.persistence.admin_audit import AdminAuditStore
from xiaowei_agent.persistence.fake import (
    InMemoryActivationStore,
    InMemoryAdminAuditStore,
    InMemoryUserDirectoryStore,
)
from xiaowei_agent.persistence.identity import (
    ACTION_FOR_COMMAND,
    EFFECT_FOR_COMMAND,
    UserDirectoryStore,
)
from xiaowei_agent.persistence.local_admin import LocalAdminRecord

_FORBIDDEN_AUDIT_INPUTS = frozenset(
    {"action", "target_kind", "target_ref_digest", "outcome", "reason_code", "effect"}
)


def _protocol_surface(protocol: type) -> frozenset[str]:
    """Protocol 上**调用方能看见**的成员名。"""
    return frozenset(
        name
        for name in dir(protocol)
        if not name.startswith("_") and callable(getattr(protocol, name, None))
    )


def _command_types() -> frozenset[type]:
    annotated = typing.get_args(DirectoryCommand)[0]
    return frozenset(typing.get_args(annotated))


def test_directory_store_exposes_exactly_one_write_method() -> None:
    """目录 Protocol 的表面必须**精确**是三个成员，其中只有一个能写。

    用 ``==`` 而不是 ``>=``：``>=`` 对"又多出一个写方法"完全无感，而多出来的那个
    正好是绕过"授权改变必带同事务审计"的唯一途径。
    """
    assert _protocol_surface(UserDirectoryStore) == {
        "load_account",
        "resolve_by_subject",
        "list_admin_users",
        "apply",
    }


def test_apply_gives_the_caller_no_way_to_steer_the_audit() -> None:
    """``apply`` 只收命令与上下文，上下文里没有任何一项是审计结论。"""
    parameters = inspect.signature(UserDirectoryStore.apply).parameters
    assert set(parameters) == {"self", "command", "context"}
    assert all(
        parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("command", "context")
    )
    assert not (_FORBIDDEN_AUDIT_INPUTS & set(AdminOperationContext.model_fields))


def test_admin_audit_store_is_append_only_by_shape() -> None:
    """四个窄写方法之外没有任何东西；名字里也不许出现改写或清理的意思。"""
    surface = _protocol_surface(AdminAuditStore)
    assert surface == {
        "append_started",
        "append_terminal",
        "append_denied",
        "list_events",
        "load",
    }
    forbidden = ("update", "delete", "purge", "prune", "truncate", "retention", "clean")
    assert not [name for name in surface if any(bad in name for bad in forbidden)]


def test_every_command_has_a_declared_action_and_effect() -> None:
    """八个命令逐个都要有动作与 effect 的声明，且**没有多余项**。

    少一个：那个命令跑到派生层会 ``KeyError``，而那是写库前一刻才炸。
    多一个：说明某个命令被删掉了而声明没跟着删，下一个读者会以为它还在。
    """
    commands = _command_types()
    assert len(commands) == 12
    assert frozenset(ACTION_FOR_COMMAND) == commands
    assert frozenset(EFFECT_FOR_COMMAND) == commands


@pytest.fixture
def directory(clock, memory_state):
    return InMemoryUserDirectoryStore(clock=clock, state=memory_state)


@pytest.fixture
def audit(clock, memory_state):
    return InMemoryAdminAuditStore(clock=clock, state=memory_state)


@pytest.fixture
def activation_store(clock, memory_state):
    return InMemoryActivationStore(clock=clock, state=memory_state)


@pytest.fixture
def admin_probe(memory_state):
    """内存侧的旧凭据探针。

    它**绕过** ``apply()`` 直接摆状态，这是它的用途：制造一条 rev_0014 之前就
    存在的凭据行，而那种行在真实部署里正是由更早的版本写下的。
    """

    class _Probe:
        async def seed_legacy_credential(
            self, *, password_hash: str, user_id: str | None = None
        ) -> bool:
            memory_state.local_admin = LocalAdminRecord(
                password_hash=password_hash,
                must_change_password=True,
                user_id=user_id,
            )
            # 内存没有外键，悬空链接摆得出来，所以这一格必须由 store fail-closed。
            return user_id is not None

        async def linked_user_id(self) -> str | None:
            record = memory_state.local_admin
            return None if record is None else record.user_id

        async def stored_password_hash(self) -> str:
            record = memory_state.local_admin
            assert record is not None
            return record.password_hash

    return _Probe()


@pytest.fixture
def directory_probe(memory_state):
    """逐类事实的原始读。

    与 ``load_account`` 的组合读分开：组合读要账号与角色同时在才返回非空，因此
    "账号留下了、角色没留下"这种半状态它一个字都不会说，而回滚恰恰就是要排除半状态。
    """

    class _Probe:
        async def facts(self) -> dict[str, frozenset]:
            return {
                "accounts": frozenset(memory_state.user_accounts),
                "roles": frozenset(memory_state.user_role_assignments),
                "bindings": frozenset(memory_state.external_identities),
                "audits": frozenset(memory_state.admin_audit_events),
                "activations": frozenset(
                    (request_id, request.status.value)
                    for request_id, request in memory_state.activation_requests.items()
                ),
            }

    return _Probe()


@pytest.fixture
def broken_audit_derivation(monkeypatch):
    """把**内存实现模块里**的 ``derive_audit`` 换成一个与领域无关的爆炸。

    换的是 ``fake`` 模块上的那个名字，不是 ``identity`` 模块上的：两个实现各自
    ``from ... import derive_audit``，改源模块的属性对已经绑好的名字没有作用。
    """

    def activate() -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("audit derivation exploded")

        monkeypatch.setattr(fake, "derive_audit", boom)

    return activate


bind(globals(), IDENTITY_DIRECTORY_CASES)
