"""Web OAuth state 与浏览器 session 的 digest-only 存储契约。"""

import contextlib
import datetime as dt
import io
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.suites.web_session_store import WEB_SESSION_STORE_CASES, bind

from xiaowei_agent.contracts import IdentitySource
from xiaowei_agent.contracts.web_navigation import (
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.persistence.fake import InMemoryWebSessionStore
from xiaowei_agent.persistence.postgres import PostgresWebSessionStore
from xiaowei_agent.persistence.rows import (
    oauth_state_to_row,
    row_to_oauth_state,
    row_to_web_return_intent,
    row_to_web_session,
    web_return_intent_to_row,
    web_session_to_row,
)
from xiaowei_agent.persistence.schema import (
    WEB_OAUTH_LOGIN_CONTEXTS,
    WEB_OAUTH_STATES,
    WEB_SESSIONS,
)
from xiaowei_agent.persistence.web_session import (
    DEFAULT_OAUTH_STATE_CAPACITY,
    IssueOAuthLoginStateCommand,
    IssueOAuthStateCommand,
    OAuthState,
    OAuthStateCapacityError,
    RotateWebSessionCommand,
    WebSession,
    WebSessionLookup,
)


@pytest.fixture
def web_sessions(clock, memory_state):
    return InMemoryWebSessionStore(clock=clock, state=memory_state)


@pytest.fixture
def bounded_web_sessions(clock, memory_state):
    return InMemoryWebSessionStore(
        clock=clock,
        state=memory_state,
        oauth_state_capacity=2,
    )


@pytest.fixture
def oauth_state_digests(memory_state):
    async def load() -> set[str]:
        return set(memory_state.oauth_states)

    return load


@pytest.fixture
def oauth_login_context_digests(memory_state):
    async def load() -> set[str]:
        return set(memory_state.oauth_login_contexts)

    return load


@pytest.fixture
def delete_oauth_login_context(memory_state):
    async def delete(state_digest: str) -> None:
        memory_state.oauth_login_contexts.pop(state_digest, None)

    return delete


bind(globals(), WEB_SESSION_STORE_CASES)

_NOW = dt.datetime(2026, 9, 9, 9, 0, tzinfo=dt.UTC)
_ORIGIN_DIGEST = "a1" * 32


def test_oauth_state_capacity_contract_is_fixed_and_non_sensitive() -> None:
    assert DEFAULT_OAUTH_STATE_CAPACITY == 1024
    error = OAuthStateCapacityError()
    assert str(error) == "oauth state capacity exhausted"
    assert error.args == ("oauth state capacity exhausted",)


def test_login_state_command_cannot_exist_without_a_return_context() -> None:
    with pytest.raises(ValidationError):
        IssueOAuthLoginStateCommand.model_validate(
            {"state_digest": "a" * 64, "ttl_seconds": 60}
        )


@pytest.mark.parametrize("invalid", [True, 0, -1, 1025, 10**9])
def test_web_session_stores_reject_capacity_outside_the_fixed_safety_bound(
    clock, memory_state, invalid: object
) -> None:
    factories = (
        lambda: InMemoryWebSessionStore(
            clock=clock,
            state=memory_state,
            oauth_state_capacity=cast(int, invalid),
        ),
        lambda: PostgresWebSessionStore(
            engine=cast(AsyncEngine, object()),
            clock=clock,
            oauth_state_capacity=cast(int, invalid),
        ),
    )
    for factory in factories:
        with pytest.raises(ValueError, match="oauth state capacity out of bounds"):
            factory()


async def test_default_capacity_rejects_the_1025th_pending_state(
    clock, memory_state
) -> None:
    store = InMemoryWebSessionStore(clock=clock, state=memory_state)
    for index in range(1024):
        await store.issue_oauth_state(
            command=IssueOAuthStateCommand(
                state_digest=f"{index:064x}",
                ttl_seconds=60,
            )
        )

    with pytest.raises(OAuthStateCapacityError):
        await store.issue_oauth_state(
            command=IssueOAuthStateCommand(
                state_digest=f"{1024:064x}",
                ttl_seconds=60,
            )
        )


def test_persisted_models_hold_only_digests_subject_and_time_facts() -> None:
    assert set(OAuthState.model_fields) == {
        "state_digest",
        "issued_at",
        "expires_at",
        "consumed_at",
    }
    assert set(WebSession.model_fields) == {
        "session_digest",
        "subject_ref",
        "issued_at",
        "expires_at",
        "revoked_at",
        # RI5：签发来源与 origin 绑定。两者都不是 secret——前者是闭集枚举，
        # 后者是 origin 的摘要，都不能反推出 cookie 或主体身份。
        "auth_source",
        "public_origin_digest",
    }
    forbidden = {
        "state",
        "session_cookie",
        "cookie",
        "actor",
        "tenant_id",
        "environment_id",
        "permissions",
        "csrf_token",
    }
    assert not (forbidden & set(OAuthState.model_fields))
    assert not (forbidden & set(WebSession.model_fields))


def test_rev_0007_has_the_expected_revision_chain() -> None:
    from xiaowei_agent.persistence.migrations.versions import rev_0007_web_sessions

    assert rev_0007_web_sessions.revision == "0007_web_sessions"
    assert rev_0007_web_sessions.down_revision == "0006_channels"


def test_web_session_schema_is_digest_only_and_mirrors_time_invariants() -> None:
    assert set(WEB_OAUTH_STATES.columns.keys()) == {
        "state_digest",
        "issued_at",
        "expires_at",
        "consumed_at",
    }
    assert set(WEB_SESSIONS.columns.keys()) == {
        "session_digest",
        "subject_ref",
        "issued_at",
        "expires_at",
        "revoked_at",
        "auth_source",
        "public_origin_digest",
    }
    assert set(WEB_OAUTH_LOGIN_CONTEXTS.columns.keys()) == {
        "state_digest",
        "return_intent_kind",
        "return_intent_task_id",
        "return_intent_request_id",
    }
    state_constraints = {
        item.name for item in WEB_OAUTH_STATES.constraints if item.name is not None
    }
    session_constraints = {
        item.name for item in WEB_SESSIONS.constraints if item.name is not None
    }
    assert state_constraints >= {
        "ck_web_oauth_states_expiry_after_issue",
        "ck_web_oauth_states_consumption_window",
    }
    assert session_constraints >= {
        "ck_web_sessions_auth_source_closed",
        "ck_web_sessions_expiry_after_issue",
        "ck_web_sessions_revocation_after_issue",
    }


def test_web_session_downgrade_drops_session_before_state() -> None:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(root / config.get_main_option("script_location", ""))
    )
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline/offline")
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        command.downgrade(config, "0007_web_sessions:0006_channels", sql=True)
    sql = buffer.getvalue()

    assert sql.index("DROP TABLE web_sessions") < sql.index(
        "DROP TABLE web_oauth_states"
    )


def test_session_rotation_requires_a_new_digest_and_bounded_ttl() -> None:
    with pytest.raises(ValidationError, match="new digest"):
        RotateWebSessionCommand(
            session_digest="a" * 64,
            previous_session_digest="a" * 64,
            subject_ref="subject-alice",
            auth_source=IdentitySource.FEISHU,
            public_origin_digest=_ORIGIN_DIGEST,
            ttl_seconds=3600,
        )
    with pytest.raises(ValidationError):
        RotateWebSessionCommand(
            session_digest="a" * 64,
            subject_ref="subject-alice",
            auth_source=IdentitySource.FEISHU,
            public_origin_digest=_ORIGIN_DIGEST,
            ttl_seconds=86_401,
        )


def test_session_issuance_must_state_its_source_and_origin() -> None:
    """两个新字段必填：给默认值等于允许调用方漏传而静默写错绑定。"""
    for missing in ("auth_source", "public_origin_digest"):
        values = {
            "session_digest": "a" * 64,
            "subject_ref": "subject-alice",
            "auth_source": IdentitySource.FEISHU,
            "public_origin_digest": _ORIGIN_DIGEST,
            "ttl_seconds": 3600,
        }
        del values[missing]
        with pytest.raises(ValidationError):
            RotateWebSessionCommand(**values)
    with pytest.raises(ValidationError):
        WebSessionLookup(session_digest="a" * 64)


def test_oauth_state_and_session_row_mappings_are_exact_round_trips() -> None:
    state = OAuthState(
        state_digest="a" * 64,
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(minutes=5),
        consumed_at=None,
    )
    session = WebSession(
        session_digest="b" * 64,
        subject_ref="subject-alice",
        issued_at=_NOW,
        expires_at=_NOW + dt.timedelta(hours=1),
        revoked_at=None,
        auth_source=IdentitySource.FEISHU,
        public_origin_digest=_ORIGIN_DIGEST,
    )

    state_row = oauth_state_to_row(state)
    session_row = web_session_to_row(session)

    assert set(state_row) == set(OAuthState.model_fields)
    assert set(session_row) == set(WebSession.model_fields)
    assert row_to_oauth_state(state_row) == state
    assert row_to_web_session(session_row) == session


def test_login_context_row_mapping_is_an_exact_closed_intent_round_trip() -> None:
    intent = WebReturnIntent(
        kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
        task_id="task-1",
    )

    row = web_return_intent_to_row(intent)

    assert set(row) == {
        "return_intent_kind",
        "return_intent_task_id",
        "return_intent_request_id",
    }
    assert row_to_web_return_intent(row) == intent


@pytest.mark.parametrize(
    ("model", "values", "message"),
    [
        (
            OAuthState,
            {
                "state_digest": "a" * 64,
                "issued_at": _NOW,
                "expires_at": _NOW,
                "consumed_at": None,
            },
            "expiry must be after issuance",
        ),
        (
            OAuthState,
            {
                "state_digest": "a" * 64,
                "issued_at": _NOW,
                "expires_at": _NOW + dt.timedelta(minutes=5),
                "consumed_at": _NOW + dt.timedelta(minutes=5),
            },
            "consumption must precede expiry",
        ),
        (
            WebSession,
            {
                "session_digest": "b" * 64,
                "subject_ref": "subject-alice",
                "auth_source": IdentitySource.FEISHU,
                "public_origin_digest": _ORIGIN_DIGEST,
                "issued_at": _NOW,
                "expires_at": _NOW,
                "revoked_at": None,
            },
            "expiry must be after issuance",
        ),
        (
            WebSession,
            {
                "session_digest": "b" * 64,
                "subject_ref": "subject-alice",
                "auth_source": IdentitySource.FEISHU,
                "public_origin_digest": _ORIGIN_DIGEST,
                "issued_at": _NOW,
                "expires_at": _NOW + dt.timedelta(hours=1),
                "revoked_at": _NOW - dt.timedelta(seconds=1),
            },
            "revocation cannot predate issuance",
        ),
    ],
)
def test_persisted_time_facts_reject_impossible_states(
    model, values, message
) -> None:
    with pytest.raises(ValidationError, match=message):
        model(**values)
