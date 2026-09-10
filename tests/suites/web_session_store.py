"""``WebSessionStore`` 的跨实现行为用例。"""

import asyncio
from collections.abc import Callable, MutableMapping, Sequence
from typing import Any

import pytest

from xiaowei_agent.persistence.web_session import (
    ConsumeOAuthStateCommand,
    IssueOAuthStateCommand,
    OAuthStateCapacityError,
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


async def test_oauth_state_capacity_rejects_the_first_issue_above_the_limit(
    bounded_web_sessions: Any,
) -> None:
    for digest in ("1" * 64, "2" * 64):
        await bounded_web_sessions.issue_oauth_state(
            command=IssueOAuthStateCommand(state_digest=digest, ttl_seconds=60)
        )

    with pytest.raises(
        OAuthStateCapacityError, match=r"^oauth state capacity exhausted$"
    ):
        await bounded_web_sessions.issue_oauth_state(
            command=IssueOAuthStateCommand(
                state_digest="3" * 64,
                ttl_seconds=60,
            )
        )


async def test_consumed_and_expired_oauth_states_release_capacity(
    bounded_web_sessions: Any, clock: Any
) -> None:
    consumed_digest = "4" * 64
    expired_digest = "5" * 64
    await bounded_web_sessions.issue_oauth_state(
        command=IssueOAuthStateCommand(
            state_digest=consumed_digest,
            ttl_seconds=60,
        )
    )
    await bounded_web_sessions.issue_oauth_state(
        command=IssueOAuthStateCommand(
            state_digest=expired_digest,
            ttl_seconds=1,
        )
    )
    await bounded_web_sessions.consume_oauth_state(
        command=ConsumeOAuthStateCommand(state_digest=consumed_digest)
    )
    clock.advance(seconds=1)

    for digest in ("6" * 64, "7" * 64):
        await bounded_web_sessions.issue_oauth_state(
            command=IssueOAuthStateCommand(state_digest=digest, ttl_seconds=60)
        )
    with pytest.raises(OAuthStateCapacityError):
        await bounded_web_sessions.issue_oauth_state(
            command=IssueOAuthStateCommand(
                state_digest="8" * 64,
                ttl_seconds=60,
            )
        )


async def test_cleaned_oauth_state_digest_no_longer_collides(
    bounded_web_sessions: Any, clock: Any
) -> None:
    digest = "9" * 64
    command = IssueOAuthStateCommand(state_digest=digest, ttl_seconds=60)
    await bounded_web_sessions.issue_oauth_state(command=command)
    await bounded_web_sessions.consume_oauth_state(
        command=ConsumeOAuthStateCommand(state_digest=digest)
    )

    reissued = await bounded_web_sessions.issue_oauth_state(command=command)
    assert reissued.state_digest == digest
    clock.advance(seconds=60)
    reissued_after_expiry = await bounded_web_sessions.issue_oauth_state(
        command=command
    )
    assert reissued_after_expiry.state_digest == digest


async def test_concurrent_oauth_state_issuance_cannot_cross_capacity(
    bounded_web_sessions: Any,
) -> None:
    barrier = asyncio.Barrier(4)

    async def issue(index: int) -> object:
        await barrier.wait()
        return await bounded_web_sessions.issue_oauth_state(
            command=IssueOAuthStateCommand(
                state_digest=f"{index + 10:064x}",
                ttl_seconds=60,
            )
        )

    results = await asyncio.gather(
        *(issue(index) for index in range(4)),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in results) == 2
    assert sum(isinstance(item, OAuthStateCapacityError) for item in results) == 2


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
    test_oauth_state_capacity_rejects_the_first_issue_above_the_limit,
    test_consumed_and_expired_oauth_states_release_capacity,
    test_cleaned_oauth_state_digest_no_longer_collides,
    test_concurrent_oauth_state_issuance_cannot_cross_capacity,
    test_session_rotation_revokes_the_previous_cookie_digest,
    test_rotation_collision_does_not_revoke_the_current_session,
    test_expired_and_revoked_sessions_share_the_hidden_not_found_result,
)

ALL_GROUPS = {"web_session_store": WEB_SESSION_STORE_CASES}
