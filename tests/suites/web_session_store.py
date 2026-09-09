"""``WebSessionStore`` 的跨实现行为用例。"""

import asyncio
from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import pytest

from xiaowei_agent.persistence.web_session import (
    ConsumeOAuthStateCommand,
    IssueOAuthStateCommand,
    OAuthStateNotFoundError,
    RevokeWebSessionCommand,
    RotateWebSessionCommand,
    WebSessionConflictError,
    WebSessionLookup,
    WebSessionNotFoundError,
)


def bind(namespace: MutableMapping[str, Any], cases: Sequence[Callable[..., Any]]) -> None:
    for case in cases:
        namespace[case.__name__] = case


async def test_oauth_state_is_single_use_and_expiry_is_fail_closed(
    web_sessions: Any, clock: Any
) -> None:
    command = IssueOAuthStateCommand(state_digest="a" * 64, ttl_seconds=60)
    issued = await web_sessions.issue_oauth_state(command=command)

    assert issued.state_digest == command.state_digest
    assert issued.issued_at == clock()
    assert (issued.expires_at - issued.issued_at).total_seconds() == 60
    assert issued.consumed_at is None

    consumed = await web_sessions.consume_oauth_state(
        command=ConsumeOAuthStateCommand(state_digest=command.state_digest)
    )
    assert consumed.consumed_at == clock()

    with pytest.raises(OAuthStateNotFoundError):
        await web_sessions.consume_oauth_state(
            command=ConsumeOAuthStateCommand(state_digest=command.state_digest)
        )

    expired_digest = "b" * 64
    await web_sessions.issue_oauth_state(
        command=IssueOAuthStateCommand(
            state_digest=expired_digest,
            ttl_seconds=60,
        )
    )
    clock.advance(seconds=60)
    with pytest.raises(OAuthStateNotFoundError):
        await web_sessions.consume_oauth_state(
            command=ConsumeOAuthStateCommand(state_digest=expired_digest)
        )


async def test_concurrent_oauth_state_consumption_has_exactly_one_winner(
    web_sessions: Any,
) -> None:
    digest = "c" * 64
    await web_sessions.issue_oauth_state(
        command=IssueOAuthStateCommand(state_digest=digest, ttl_seconds=60)
    )

    results = await asyncio.gather(
        *(
            web_sessions.consume_oauth_state(
                command=ConsumeOAuthStateCommand(state_digest=digest)
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, OAuthStateNotFoundError) for item in results) == 1


async def test_unknown_oauth_state_is_indistinguishable_from_replay(
    web_sessions: Any,
) -> None:
    with pytest.raises(OAuthStateNotFoundError, match="oauth state not found"):
        await web_sessions.consume_oauth_state(
            command=ConsumeOAuthStateCommand(state_digest="d" * 64)
        )


async def test_session_rotation_revokes_the_previous_cookie_digest(
    web_sessions: Any, clock: Any
) -> None:
    first = await web_sessions.rotate_session(
        command=RotateWebSessionCommand(
            session_digest="e" * 64,
            subject_ref="subject-alice",
            ttl_seconds=3600,
        )
    )
    assert first.issued_at == clock()
    assert first.revoked_at is None
    assert (
        await web_sessions.get_session(
            lookup=WebSessionLookup(session_digest=first.session_digest)
        )
        == first
    )

    second = await web_sessions.rotate_session(
        command=RotateWebSessionCommand(
            session_digest="f" * 64,
            previous_session_digest=first.session_digest,
            subject_ref="subject-alice",
            ttl_seconds=3600,
        )
    )

    with pytest.raises(WebSessionNotFoundError):
        await web_sessions.get_session(
            lookup=WebSessionLookup(session_digest=first.session_digest)
        )
    assert (
        await web_sessions.get_session(
            lookup=WebSessionLookup(session_digest=second.session_digest)
        )
        == second
    )


async def test_rotation_collision_does_not_revoke_the_current_session(
    web_sessions: Any,
) -> None:
    current_digest = "1" * 64
    occupied_digest = "2" * 64
    for digest, subject_ref in (
        (current_digest, "subject-alice"),
        (occupied_digest, "subject-bob"),
    ):
        await web_sessions.rotate_session(
            command=RotateWebSessionCommand(
                session_digest=digest,
                subject_ref=subject_ref,
                ttl_seconds=3600,
            )
        )

    with pytest.raises(WebSessionConflictError):
        await web_sessions.rotate_session(
            command=RotateWebSessionCommand(
                session_digest=occupied_digest,
                previous_session_digest=current_digest,
                subject_ref="subject-alice",
                ttl_seconds=3600,
            )
        )

    assert (
        await web_sessions.get_session(
            lookup=WebSessionLookup(session_digest=current_digest)
        )
    ).subject_ref == "subject-alice"


async def test_expired_and_revoked_sessions_share_the_hidden_not_found_result(
    web_sessions: Any, clock: Any
) -> None:
    expired_digest = "3" * 64
    revoked_digest = "4" * 64
    for digest in (expired_digest, revoked_digest):
        await web_sessions.rotate_session(
            command=RotateWebSessionCommand(
                session_digest=digest,
                subject_ref="subject-alice",
                ttl_seconds=60,
            )
        )

    assert await web_sessions.revoke_session(
        command=RevokeWebSessionCommand(session_digest=revoked_digest)
    )
    assert not await web_sessions.revoke_session(
        command=RevokeWebSessionCommand(session_digest=revoked_digest)
    )
    clock.advance(seconds=60)

    for digest in (expired_digest, revoked_digest, "5" * 64):
        with pytest.raises(WebSessionNotFoundError, match="web session not found"):
            await web_sessions.get_session(
                lookup=WebSessionLookup(session_digest=digest)
            )


WEB_SESSION_STORE_CASES = (
    test_oauth_state_is_single_use_and_expiry_is_fail_closed,
    test_concurrent_oauth_state_consumption_has_exactly_one_winner,
    test_unknown_oauth_state_is_indistinguishable_from_replay,
    test_session_rotation_revokes_the_previous_cookie_digest,
    test_rotation_collision_does_not_revoke_the_current_session,
    test_expired_and_revoked_sessions_share_the_hidden_not_found_result,
)

ALL_GROUPS = {"web_session_store": WEB_SESSION_STORE_CASES}
