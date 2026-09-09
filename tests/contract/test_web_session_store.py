"""Web OAuth state 与浏览器 session 的 digest-only 存储契约。"""

import contextlib
import datetime as dt
import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from tests.suites.web_session_store import WEB_SESSION_STORE_CASES, bind

from xiaowei_agent.persistence.fake import InMemoryWebSessionStore
from xiaowei_agent.persistence.rows import (
    oauth_state_to_row,
    row_to_oauth_state,
    row_to_web_session,
    web_session_to_row,
)
from xiaowei_agent.persistence.schema import WEB_OAUTH_STATES, WEB_SESSIONS
from xiaowei_agent.persistence.web_session import (
    OAuthState,
    RotateWebSessionCommand,
    WebSession,
)


@pytest.fixture
def web_sessions(clock, memory_state):
    return InMemoryWebSessionStore(clock=clock, state=memory_state)


bind(globals(), WEB_SESSION_STORE_CASES)

_NOW = dt.datetime(2026, 9, 9, 9, 0, tzinfo=dt.UTC)


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
            ttl_seconds=3600,
        )
    with pytest.raises(ValidationError):
        RotateWebSessionCommand(
            session_digest="a" * 64,
            subject_ref="subject-alice",
            ttl_seconds=86_401,
        )


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
    )

    state_row = oauth_state_to_row(state)
    session_row = web_session_to_row(session)

    assert set(state_row) == set(OAuthState.model_fields)
    assert set(session_row) == set(WebSession.model_fields)
    assert row_to_oauth_state(state_row) == state
    assert row_to_web_session(session_row) == session


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
