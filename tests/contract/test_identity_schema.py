"""四张新表的结构契约 —— **离线判定，不需要数据库**。

两条写法上的硬约束，都是被上一轮复审钉下来的：

1. ``METADATA`` **没有命名约定**（``schema.py`` 顶部的 ``sa.MetaData()``），因此
   ``primary_key=True`` 声明出来的主键在 metadata 里 ``name is None``。任何"按约束名
   去查主键"的结构用例都会永久变红——所以主键一律按**列集合**判定。
2. 偏唯一索引和全局唯一约束在 SQLAlchemy 里住在两个地方：前者是
   ``table.indexes`` 里 ``unique=True`` 且带 ``postgresql_where`` 的 ``Index``，后者是
   ``table.constraints`` 里的 ``UniqueConstraint``。把两者混为一谈时，
   "``operation_id`` 不是全局唯一"这条断言会被那两条偏唯一索引满足而假绿——
   因此本文件自带 ``test_the_unique_helper_tells_partial_indexes_apart_from_global_ones``
   来测**自己的 helper**。
"""

import datetime as dt

import pytest
import sqlalchemy as sa

from xiaowei_agent.contracts.activation import ActivationSource, ActivationStatus
from xiaowei_agent.contracts.admin_audit import (
    DIRECTORY_ACTIONS,
    AdminAuditEffect,
    AdminAuditEvent,
)
from xiaowei_agent.contracts.enums import (
    AdminAuditAction,
    AdminAuditOutcome,
    AdminAuditReasonCode,
    AdminAuditTargetKind,
    IdentitySource,
    ProductRole,
    UserStatus,
)
from xiaowei_agent.persistence.admin_audit import (
    AdminAuditConflictError,
    AdminAuditMissingStartError,
)
from xiaowei_agent.persistence.errors import classify_persistence_exception
from xiaowei_agent.persistence.identity import (
    AdminAuditUnwritableError,
    UserDirectoryConflictError,
    UserDirectoryNotFoundError,
)
from xiaowei_agent.persistence.postgres import _persistence_boundary
from xiaowei_agent.persistence.rows import (
    admin_audit_event_to_row,
    row_to_admin_audit_event,
)
from xiaowei_agent.persistence.schema import (
    ACTIVATION_REQUESTS,
    ADMIN_AUDIT_EVENTS,
    ALL_TABLES,
    EXTERNAL_IDENTITIES,
    LOCAL_ADMINS,
    USER_ACCOUNTS,
    USER_ROLE_ASSIGNMENTS,
    WEB_OAUTH_LOGIN_CONTEXTS,
    WEB_OAUTH_STATES,
    WEB_OAUTH_TEST_CONTEXTS,
)

_DIGEST_LENGTH = 64


def _primary_key(table: sa.Table) -> tuple[str, ...]:
    """按**列集合**取主键；metadata 里它没有名字。"""
    return tuple(column.name for column in table.primary_key.columns)


def _global_unique_column_sets(table: sa.Table) -> set[frozenset[str]]:
    """只算**全局**唯一：具名 ``UniqueConstraint`` 与无谓词的唯一 ``Index``。

    带 ``postgresql_where`` 的唯一索引**不算**——它只在谓词命中的那些行之间唯一。
    """
    found = {
        frozenset(column.name for column in item.columns)
        for item in table.constraints
        if isinstance(item, sa.UniqueConstraint)
    }
    found |= {
        frozenset(column.name for column in index.columns)
        for index in table.indexes
        if index.unique and index.dialect_options["postgresql"].get("where") is None
    }
    return found


def _partial_unique_indexes(table: sa.Table) -> dict[str, tuple[tuple[str, ...], str]]:
    """具名偏唯一索引：名字 -> (列, 谓词的 SQL 文本)。"""
    found = {}
    for index in table.indexes:
        where = index.dialect_options["postgresql"].get("where")
        if not index.unique or where is None:
            continue
        assert index.name is not None
        found[index.name] = (
            tuple(column.name for column in index.columns),
            str(where.compile(compile_kwargs={"literal_binds": True})),
        )
    return found


def _check_text(table: sa.Table, name: str) -> str:
    for item in table.constraints:
        if isinstance(item, sa.CheckConstraint) and item.name == name:
            return str(item.sqltext)
    raise AssertionError(f"{table.name} 上找不到 CHECK {name}")


def _check_names(table: sa.Table) -> set[str]:
    return {
        item.name
        for item in table.constraints
        if isinstance(item, sa.CheckConstraint) and item.name is not None
    }


def test_new_tables_are_registered_in_all_tables() -> None:
    """四张表必须进 ``ALL_TABLES``。

    漏掉的后果不是报错而是沉默：集成测试的 ``clean_database`` 按 ``ALL_TABLES``
    做 TRUNCATE，漏掉一张表就会让上一条用例的授权事实留给下一条用例。
    """
    assert {
        ACTIVATION_REQUESTS,
        USER_ACCOUNTS,
        USER_ROLE_ASSIGNMENTS,
        EXTERNAL_IDENTITIES,
        ADMIN_AUDIT_EVENTS,
        WEB_OAUTH_LOGIN_CONTEXTS,
        WEB_OAUTH_TEST_CONTEXTS,
    } <= set(ALL_TABLES)


def test_activation_request_table_has_closed_scope_and_state_constraints() -> None:
    assert _primary_key(ACTIVATION_REQUESTS) == ("request_id",)
    assert {
        "request_id",
        "tenant_id",
        "environment_id",
        "provider",
        "subject_ref",
        "subject_ref_digest",
        "source",
        "return_intent_kind",
        "return_intent_task_id",
        "return_intent_request_id",
        "source_event_digest",
        "source_chat_digest",
        "requested_at",
        "expires_at",
        "status",
        "decided_at",
        "decided_by",
        "approved_role",
    } == set(ACTIVATION_REQUESTS.c.keys())
    source_check = _check_text(
        ACTIVATION_REQUESTS, "ck_activation_requests_source_closed"
    )
    assert all(member.value in source_check for member in ActivationSource)
    status_check = _check_text(
        ACTIVATION_REQUESTS, "ck_activation_requests_status_closed"
    )
    assert all(member.value in status_check for member in ActivationStatus)
    assert {
        "ck_activation_requests_return_intent_shape",
        "ck_activation_requests_source_intent_match",
        "ck_activation_requests_source_digests_match",
    } <= _check_names(ACTIVATION_REQUESTS)


def test_login_context_is_a_one_to_one_cascading_extension_of_oauth_state() -> None:
    assert _primary_key(WEB_OAUTH_LOGIN_CONTEXTS) == ("state_digest",)
    assert set(WEB_OAUTH_LOGIN_CONTEXTS.c) == {
        WEB_OAUTH_LOGIN_CONTEXTS.c.state_digest,
        WEB_OAUTH_LOGIN_CONTEXTS.c.return_intent_kind,
        WEB_OAUTH_LOGIN_CONTEXTS.c.return_intent_task_id,
        WEB_OAUTH_LOGIN_CONTEXTS.c.return_intent_request_id,
    }
    foreign_keys = tuple(WEB_OAUTH_LOGIN_CONTEXTS.foreign_key_constraints)
    assert len(foreign_keys) == 1
    foreign_key = foreign_keys[0]
    assert tuple(element.target_fullname for element in foreign_key.elements) == (
        f"{WEB_OAUTH_STATES.name}.state_digest",
    )
    assert foreign_key.ondelete == "CASCADE"
    assert {
        "ck_web_oauth_login_contexts_kind_closed",
        "ck_web_oauth_login_contexts_intent_shape",
    } <= _check_names(WEB_OAUTH_LOGIN_CONTEXTS)


def test_test_context_is_a_one_to_one_cascading_extension_of_oauth_state() -> None:
    """W4a：OAuth 测试 state 只保存 state digest、审计 operation id 与被测配置代次。

    它与登录 context 是两张表、两个外键：登录分支不能消费测试 state，测试分支也不能
    消费登录 state。``operation_id`` 全局唯一——一次 STARTED 最多绑定一个 state。
    """
    assert _primary_key(WEB_OAUTH_TEST_CONTEXTS) == ("state_digest",)
    assert {column.name for column in WEB_OAUTH_TEST_CONTEXTS.c} == {
        "state_digest",
        "operation_id",
        "config_generation",
    }
    assert WEB_OAUTH_TEST_CONTEXTS.c.operation_id.nullable is False
    assert WEB_OAUTH_TEST_CONTEXTS.c.config_generation.nullable is False
    assert (
        _check_text(
            WEB_OAUTH_TEST_CONTEXTS, "ck_web_oauth_test_contexts_config_generation_positive"
        )
        == "config_generation > 0"
    )
    foreign_keys = tuple(WEB_OAUTH_TEST_CONTEXTS.foreign_key_constraints)
    assert len(foreign_keys) == 1
    assert tuple(element.target_fullname for element in foreign_keys[0].elements) == (
        f"{WEB_OAUTH_STATES.name}.state_digest",
    )
    assert foreign_keys[0].ondelete == "CASCADE"
    assert frozenset({"operation_id"}) in _global_unique_column_sets(
        WEB_OAUTH_TEST_CONTEXTS
    )
    shape = _check_text(
        WEB_OAUTH_TEST_CONTEXTS, "ck_web_oauth_test_contexts_operation_id_shape"
    )
    assert "'w4:'" in shape and "64" in shape


def test_activation_request_has_one_pending_subject_per_scope() -> None:
    partial = _partial_unique_indexes(ACTIVATION_REQUESTS)
    columns, where = partial["uq_activation_requests_pending_subject"]
    assert columns == (
        "tenant_id",
        "environment_id",
        "provider",
        "subject_ref_digest",
    )
    assert "status" in where and "pending" in where


def test_actor_is_unique_so_two_accounts_cannot_claim_the_same_identity() -> None:
    """``actor`` 全局唯一（与规格 §6.1 的有意偏离，严格强于"同作用域内唯一"）。

    不唯一时，同一个人可以在两个账号下各拿一套角色，而撤销其中一个不影响另一个。
    """
    assert _primary_key(USER_ACCOUNTS) == ("user_id",)
    assert frozenset({"actor"}) in _global_unique_column_sets(USER_ACCOUNTS)


def test_role_assignment_is_keyed_by_user_and_scope() -> None:
    """角色主键是三元组：一个账号在一个作用域里最多一个角色。"""
    assert set(_primary_key(USER_ROLE_ASSIGNMENTS)) == {
        "user_id",
        "tenant_id",
        "environment_id",
    }


def test_one_subject_ref_binds_to_at_most_one_account_per_scope() -> None:
    """两个方向都要钉。

    主键保证"一个外部主体最多绑一个账号"；UNIQUE 保证"一个账号在同作用域最多绑
    一个主体"。只钉主键时，一个账号可以绑上任意多个 open_id，于是"撤销这个人"
    要撤几次没有定论。
    """
    assert set(_primary_key(EXTERNAL_IDENTITIES)) == {
        "provider",
        "tenant_id",
        "environment_id",
        "subject_ref_digest",
    }
    assert (
        frozenset({"provider", "tenant_id", "environment_id", "user_id"})
        in _global_unique_column_sets(EXTERNAL_IDENTITIES)
    )


def test_external_identity_stores_a_digest_not_the_open_id() -> None:
    """列名与定长都表明它存的是摘要。

    ``open_id`` 落库就等于给它开了一条同时进入备份、慢查询日志和运维截图的通道。
    """
    assert "subject_ref" not in EXTERNAL_IDENTITIES.c
    column = EXTERNAL_IDENTITIES.c.subject_ref_digest
    assert isinstance(column.type, sa.CHAR)
    assert column.type.length == _DIGEST_LENGTH


def test_assignment_and_identity_reference_the_account_by_foreign_key() -> None:
    """两条外键都是 ``RESTRICT``。

    ``CASCADE`` 会让一次删账号静默带走它的角色行与绑定行，而那正是事后要查"当时
    这个人有什么权限"时唯一的依据。
    """
    for table in (USER_ROLE_ASSIGNMENTS, EXTERNAL_IDENTITIES):
        keys = list(table.c.user_id.foreign_keys)
        assert len(keys) == 1, table.name
        assert keys[0].target_fullname == "user_accounts.user_id"
        assert keys[0].ondelete == "RESTRICT"


def test_local_credential_is_linked_to_a_directory_account() -> None:
    """``local_admins.user_id`` 可空（旧库升级后先是空），外键仍是 ``RESTRICT``。"""
    assert "user_id" in LOCAL_ADMINS.c
    assert LOCAL_ADMINS.c.user_id.nullable is True
    keys = list(LOCAL_ADMINS.c.user_id.foreign_keys)
    assert len(keys) == 1
    assert keys[0].target_fullname == "user_accounts.user_id"
    assert keys[0].ondelete == "RESTRICT"
    assert "fk_local_admins_user_id" in {
        item.name for item in LOCAL_ADMINS.constraints if item.name is not None
    }


def test_operation_id_is_not_globally_unique() -> None:
    """规格 §14.2 的两阶段配置审计要求同一个 operation 写两条事件。

    全局唯一会让第二条终态事件必然撞约束；换一个 operation id 又无法证明两条事件
    属于同一次操作。
    """
    assert frozenset({"operation_id"}) not in _global_unique_column_sets(
        ADMIN_AUDIT_EVENTS
    )


def test_one_started_and_one_terminal_event_per_operation() -> None:
    """两条偏唯一索引，谓词各自正确。

    终态那条的谓词必须**列出三个终态**：只写 ``= 'succeeded'`` 时，一次操作可以
    同时留下一条 succeeded 和一条 failed。
    """
    partial = _partial_unique_indexes(ADMIN_AUDIT_EVENTS)
    assert set(partial) == {
        "uq_admin_audit_one_started_per_operation",
        "uq_admin_audit_one_terminal_per_operation",
    }
    started_columns, started_where = partial["uq_admin_audit_one_started_per_operation"]
    assert started_columns == ("operation_id",)
    assert "started" in started_where

    terminal_columns, terminal_where = partial[
        "uq_admin_audit_one_terminal_per_operation"
    ]
    assert terminal_columns == ("operation_id",)
    for value in ("succeeded", "denied", "failed"):
        assert value in terminal_where
    assert "'started'" not in terminal_where


def test_the_unique_helper_tells_partial_indexes_apart_from_global_ones() -> None:
    """测本文件**自己的 helper**。

    把偏唯一误算成全局唯一时，``test_operation_id_is_not_globally_unique`` 会被那两
    条偏唯一索引满足而假绿——那条用例从此再也抓不到"有人给 operation_id 加了全局
    UNIQUE"。
    """
    metadata = sa.MetaData()
    table = sa.Table(
        "helper_probe",
        metadata,
        sa.Column("a", sa.Text, primary_key=True),
        sa.Column("b", sa.Text),
        sa.Column("c", sa.Text),
        sa.UniqueConstraint("b", name="uq_probe_b"),
    )
    sa.Index("uq_probe_c_partial", table.c.c, unique=True, postgresql_where=table.c.b.is_(None))
    sa.Index("uq_probe_c_global", table.c.c, unique=True)

    assert _global_unique_column_sets(table) == {frozenset({"b"}), frozenset({"c"})}
    assert set(_partial_unique_indexes(table)) == {"uq_probe_c_partial"}


def test_audit_closed_sets_are_expressed_as_database_checks() -> None:
    """闭集在数据库层也成立，不只在契约层。

    内存实现与 PostgreSQL 实现共享同一套行为套件，但只有后者有数据库；契约层的
    校验挡住的是本进程的写入，CHECK 挡住的是任何绕开本进程的写入（包括一次手工
    ``psql``）。两道都要有。
    """
    expected = {
        "ck_admin_audit_events_auth_source_closed": IdentitySource,
        "ck_admin_audit_events_action_closed": AdminAuditAction,
        "ck_admin_audit_events_target_kind_closed": AdminAuditTargetKind,
        "ck_admin_audit_events_outcome_closed": AdminAuditOutcome,
        "ck_admin_audit_events_reason_code_closed": AdminAuditReasonCode,
        "ck_admin_audit_events_effect_role_closed": ProductRole,
        "ck_admin_audit_events_effect_status_closed": UserStatus,
    }
    for name, enum in expected.items():
        text = _check_text(ADMIN_AUDIT_EVENTS, name)
        for member in enum:
            assert f"'{member.value}'" in text, f"{name} 缺少 {member.value}"

    assert _check_names(ADMIN_AUDIT_EVENTS) >= {
        "ck_admin_audit_events_reason_code_matches_outcome",
        "ck_admin_audit_events_effect_only_on_success",
        "ck_admin_audit_events_role_effect_required",
        "ck_admin_audit_events_status_effect_required",
        "ck_admin_audit_events_directory_actions_are_single_phase",
    }


def test_the_single_phase_check_lists_exactly_the_directory_actions() -> None:
    """CHECK 的 SQL 字面量逐值等于 ``DIRECTORY_ACTIONS``。

    这条挡的是漂移：将来新增一个非目录动作（W4a 的配置操作）时，如果只改了枚举
    而没有回头改这条 CHECK，那个动作会被数据库当成目录动作而拒绝写 STARTED。
    """
    text = _check_text(
        ADMIN_AUDIT_EVENTS, "ck_admin_audit_events_directory_actions_are_single_phase"
    )
    listed = {
        member.value for member in AdminAuditAction if f"'{member.value}'" in text
    }
    assert listed == {action.value for action in DIRECTORY_ACTIONS}


def test_audit_table_has_no_free_text_column() -> None:
    """不变量 1 在表层面也成立。

    只要留一个能容纳异常正文的列，第一个赶工的调用方就会把 ``str(exc)`` 塞进去，
    而那时它已经进了备份。
    """
    assert set(ADMIN_AUDIT_EVENTS.c.keys()) == {
        "event_id",
        "operation_id",
        "tenant_id",
        "environment_id",
        "actor_user_id",
        "actor",
        "auth_source",
        "action",
        "target_kind",
        "target_ref_digest",
        "outcome",
        "reason_code",
        "effect_role",
        "effect_status",
        "created_at",
    }
    for forbidden in ("message", "detail", "payload", "note", "context", "error"):
        assert forbidden not in ADMIN_AUDIT_EVENTS.c


def test_effect_columns_are_closed_enums_not_json() -> None:
    """effect 是两列枚举文本，不是 JSON——它塞不进正文。"""
    for name in ("effect_role", "effect_status"):
        column = ADMIN_AUDIT_EVENTS.c[name]
        assert isinstance(column.type, sa.Text)
        assert column.nullable is True


def test_user_status_and_role_are_checked_at_the_database_level() -> None:
    status_text = _check_text(USER_ACCOUNTS, "ck_user_accounts_status_closed")
    for member in UserStatus:
        assert f"'{member.value}'" in status_text
    role_text = _check_text(
        USER_ROLE_ASSIGNMENTS, "ck_user_role_assignments_role_closed"
    )
    for member in ProductRole:
        assert f"'{member.value}'" in role_text


_CONFLICT_PROBED_TABLES = (USER_ACCOUNTS, EXTERNAL_IDENTITIES, ADMIN_AUDIT_EVENTS)
"""走 ``ON CONFLICT DO NOTHING ...  RETURNING`` 探测的三张表。

``user_role_assignments`` 不在这里：它走的是 ``DO UPDATE``，带明确的推断目标。
"""

_DECLARED_CONFLICT_TARGETS: dict[str, frozenset[tuple[frozenset[str], str | None]]] = {
    "user_accounts": frozenset(
        {
            (frozenset({"user_id"}), None),
            (frozenset({"actor"}), None),
        }
    ),
    "external_identities": frozenset(
        {
            (
                frozenset(
                    {"provider", "tenant_id", "environment_id", "subject_ref_digest"}
                ),
                None,
            ),
            (
                frozenset({"provider", "tenant_id", "environment_id", "user_id"}),
                None,
            ),
        }
    ),
    "admin_audit_events": frozenset(
        {
            (frozenset({"event_id"}), None),
            (
                frozenset({"operation_id"}),
                "admin_audit_events.outcome = 'started'",
            ),
            (
                frozenset({"operation_id"}),
                "admin_audit_events.outcome IN ('denied', 'failed', 'succeeded')",
            ),
        }
    ),
}
"""这三张表上**每一条可被 ``ON CONFLICT`` 吞掉的约束**，逐格写死。

``ON CONFLICT DO NOTHING`` 不带推断目标，因此它吞掉的是该表**任意**一条唯一/主键/
偏唯一冲突，而调用方拿到空结果之后统一抛一个业务冲突错误。这意味着：新加一条唯一
约束 = 新增一种"会被翻译成业务冲突"的失败。这条用例的作用不是防止加约束，而是逼
加约束的人先来这里回答"它撞了意味着什么"。

按**列集合 + 谓词**判定而不是按名字：``METADATA`` 没有命名约定，主键在 metadata 里
``name is None``；而两条偏唯一索引的列集合完全相同，只有谓词能把它们分开——只记列
集合时，删掉其中一条不会让这条用例变红。
"""


def _conflict_targets(
    table: sa.Table,
) -> frozenset[tuple[frozenset[str], str | None]]:
    """一张表上全部可被 ``ON CONFLICT`` 推断到的目标：主键 ∪ 唯一约束 ∪ 偏唯一索引。

    两种唯一性住在 SQLAlchemy 的两个地方（``constraints`` 与 ``indexes``），少看
    一处就会漏掉一整类目标。
    """
    targets: set[tuple[frozenset[str], str | None]] = set()
    for constraint in table.constraints:
        if isinstance(constraint, sa.PrimaryKeyConstraint | sa.UniqueConstraint):
            targets.add((frozenset(column.name for column in constraint.columns), None))
    for index in table.indexes:
        if not index.unique:
            continue
        predicate = index.dialect_options["postgresql"].get("where")
        targets.add(
            (
                frozenset(column.name for column in index.columns),
                None
                if predicate is None
                else str(predicate.compile(compile_kwargs={"literal_binds": True})),
            )
        )
    return frozenset(targets)


def test_the_conflict_probed_tables_have_exactly_the_declared_targets() -> None:
    for table in _CONFLICT_PROBED_TABLES:
        assert _conflict_targets(table) == _DECLARED_CONFLICT_TARGETS[table.name], (
            table.name
        )


def test_the_conflict_target_helper_sees_both_kinds_of_unique_index() -> None:
    """helper 的自证：两种唯一性它都认得，而且分得开。

    只认 ``constraints`` 时，``admin_audit_events`` 会只剩主键一个目标，于是上一条
    用例会在一张漏掉两条偏唯一索引的表上照样全绿。
    """
    audit_targets = _conflict_targets(ADMIN_AUDIT_EVENTS)
    predicates = {predicate for _, predicate in audit_targets}
    assert None in predicates, "没认出无谓词的主键"
    assert len([p for p in predicates if p is not None]) == 2, "没认出两条偏唯一索引"
    account_targets = _conflict_targets(USER_ACCOUNTS)
    assert all(predicate is None for _, predicate in account_targets)
    assert len(account_targets) == 2, "没认出 UniqueConstraint"


_DOMAIN_ERRORS = (
    UserDirectoryConflictError("conflict"),
    UserDirectoryNotFoundError("missing"),
    AdminAuditUnwritableError(),
    AdminAuditConflictError(),
    AdminAuditMissingStartError(),
)


@pytest.mark.parametrize("error", _DOMAIN_ERRORS, ids=lambda e: type(e).__name__)
async def test_domain_errors_pass_through_the_persistence_boundary(
    error: Exception,
) -> None:
    """闭集领域错误必须**原样**穿过两层收敛，不被改写成 ``PersistenceIntegrityError``。

    它们都是 ``RuntimeError`` 子类，因此 ``classify_persistence_exception`` 返回
    ``None``，两层都走 ``raise`` 重抛。改成继承 ``Exception`` 就会在这里被翻译成
    一个笼统的持久化错误——调用方于是分不清"这个人已经存在"和"数据库出问题了"。
    """
    assert classify_persistence_exception(error, write_outcome=None) is None

    @_persistence_boundary(write=True)
    async def boom() -> None:
        raise error

    with pytest.raises(type(error)) as raised:
        await boom()
    assert raised.value is error


def _sample_event() -> AdminAuditEvent:
    return AdminAuditEvent(
        event_id="e" * 64,
        operation_id="op-1",
        tenant_id="tenant-a",
        environment_id="env-a",
        actor_user_id="operator-1",
        actor="carol@example.com",
        auth_source=IdentitySource.LOCAL_ADMIN,
        action=AdminAuditAction.USER_CREATED,
        target_kind=AdminAuditTargetKind.USER,
        target_ref_digest="a" * 64,
        outcome=AdminAuditOutcome.SUCCEEDED,
        reason_code=None,
        effect=AdminAuditEffect(role=ProductRole.USER, status=UserStatus.ACTIVE),
        created_at=dt.datetime(2026, 9, 21, 12, tzinfo=dt.UTC),
    )


def test_an_audit_event_survives_a_round_trip_through_its_row() -> None:
    """往返无损，且行的键**恰好**是表的列。

    多一个键：``sa.insert`` 会在真库上炸；少一个键：那一列会静默变成 ``NULL``，
    而 ``reason_code`` / ``effect_*`` 都可空，静默之后没有任何东西会报错。
    """
    event = _sample_event()
    row = admin_audit_event_to_row(event)

    assert set(row) == set(ADMIN_AUDIT_EVENTS.columns.keys())
    assert row_to_admin_audit_event(row) == event


def test_the_row_uses_plain_strings_so_the_database_sees_its_own_types() -> None:
    """行里是 ``str``，不是 ``StrEnum`` 成员。

    ``StrEnum`` 是 ``str`` 的子类，驱动照样写得进去——所以这条差别**在真库上也不会
    报错**，只会让"存进去的到底是哪个值"取决于枚举的 ``__str__``。一旦某个枚举将来
    换成非 ``str`` 的基类，写入会在那一刻才炸，而那时表里已经有一批旧值。
    """
    row = admin_audit_event_to_row(_sample_event())

    for column in ("auth_source", "action", "target_kind", "outcome"):
        assert type(row[column]) is str, column
    denied = _sample_event().model_copy(
        update={
            "outcome": AdminAuditOutcome.DENIED,
            "reason_code": AdminAuditReasonCode.CONFLICT,
            "effect": AdminAuditEffect(),
        }
    )
    denied_row = admin_audit_event_to_row(denied)
    assert type(denied_row["reason_code"]) is str
    assert denied_row["effect_role"] is None
    assert denied_row["effect_status"] is None
