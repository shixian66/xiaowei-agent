"""Untrusted values cannot become confirmed slots."""

import datetime as dt

import pytest

from xiaowei_agent.contracts import ClarificationField, ExternalContent, ExternalSource
from xiaowei_agent.planning.slot_verification import (
    SlotCandidate,
    SlotSource,
    SlotVerificationError,
    canonical_time_range_value,
    confirmed_text_value,
    merge_confirmed_slots,
)

pytestmark = pytest.mark.security


def test_model_slot_cannot_be_confirmed_without_current_text_or_parent_snapshot() -> None:
    with pytest.raises(SlotVerificationError):
        merge_confirmed_slots(
            parent_snapshot=(),
            current_candidates=(
                SlotCandidate(
                    field=ClarificationField.DATABASE,
                    value=confirmed_text_value("analytics"),
                    source=SlotSource.MODEL,
                ),
            ),
            allowed_fields=frozenset({ClarificationField.DATABASE}),
        )


def test_external_content_cannot_be_confirmed() -> None:
    content = ExternalContent.capture(
        source=ExternalSource.TOOL,
        content="database=analytics",
        captured_at=dt.datetime(2026, 9, 19, tzinfo=dt.UTC),
    )

    with pytest.raises(SlotVerificationError):
        merge_confirmed_slots(
            parent_snapshot=(),
            current_candidates=(
                SlotCandidate(
                    field=ClarificationField.DATABASE,
                    value=confirmed_text_value(content.content),
                    source=SlotSource.EXTERNAL_CONTENT,
                ),
            ),
            allowed_fields=frozenset({ClarificationField.DATABASE}),
        )


def test_secret_shaped_text_cannot_be_confirmed() -> None:
    secret_shaped = "token=" + "abc123def4567890"

    with pytest.raises(ValueError):
        confirmed_text_value(secret_shaped)


def test_naive_time_cannot_be_confirmed() -> None:
    with pytest.raises(SlotVerificationError):
        canonical_time_range_value(
            start_utc=dt.datetime(2026, 9, 19, 10, 0),
            end_utc=dt.datetime(2026, 9, 19, 10, 30),
        )
