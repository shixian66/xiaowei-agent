"""W2 Web return intent is a closed, data-only navigation contract."""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import WebReturnIntent, WebReturnIntentKind


@pytest.mark.parametrize(
    "intent",
    (
        WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH),
        WebReturnIntent(
            kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
            task_id="task-1",
        ),
        WebReturnIntent(kind=WebReturnIntentKind.ADMIN_CENTER),
        WebReturnIntent(
            kind=WebReturnIntentKind.ACTIVATION_STATUS,
            request_id="activation-1",
        ),
    ),
)
def test_every_return_intent_has_one_valid_closed_shape(
    intent: WebReturnIntent,
) -> None:
    assert set(intent.model_dump(exclude_none=True)) <= {
        "kind",
        "task_id",
        "request_id",
    }


@pytest.mark.parametrize(
    "payload",
    (
        {"kind": WebReturnIntentKind.SAFE_TASK_DETAIL},
        {
            "kind": WebReturnIntentKind.SAFE_TASK_DETAIL,
            "task_id": "task-1",
            "request_id": "activation-1",
        },
        {
            "kind": WebReturnIntentKind.ACTIVATION_STATUS,
            "request_id": "activation-1",
            "task_id": "task-1",
        },
        {"kind": WebReturnIntentKind.ACTIVATION_STATUS},
        {"kind": WebReturnIntentKind.WORKBENCH, "task_id": "task-1"},
        {"kind": WebReturnIntentKind.ADMIN_CENTER, "request_id": "activation-1"},
    ),
)
def test_return_intent_requires_the_exact_reference_for_its_kind(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WebReturnIntent.model_validate(payload)


@pytest.mark.parametrize("field", ("url", "path", "host", "query", "fragment", "next"))
def test_return_intent_rejects_redirect_shaped_extra_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        WebReturnIntent.model_validate(
            {
                "kind": WebReturnIntentKind.WORKBENCH,
                field: "https://outside.example.test/app",
            }
        )


@pytest.mark.parametrize(
    "reference",
    (
        "https://outside.example.test/app",
        "/app/tasks/task-1",
        "task-1?next=/admin",
        "task-1#fragment",
        "task-1\nheader:value",
        "x" * 201,
    ),
)
def test_return_intent_rejects_path_url_control_and_unbounded_references(
    reference: str,
) -> None:
    with pytest.raises(ValidationError):
        WebReturnIntent(
            kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
            task_id=reference,
        )


def test_task_reference_reuses_the_shared_task_id_bounds() -> None:
    assert WebReturnIntent(
        kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
        task_id="x" * 200,
    ).task_id == "x" * 200


def test_activation_request_reference_remains_bounded_to_64_characters() -> None:
    assert WebReturnIntent(
        kind=WebReturnIntentKind.ACTIVATION_STATUS,
        request_id="x" * 64,
    ).request_id == "x" * 64
    with pytest.raises(ValidationError):
        WebReturnIntent(
            kind=WebReturnIntentKind.ACTIVATION_STATUS,
            request_id="x" * 65,
        )


def test_return_intent_kind_is_exhaustively_covered() -> None:
    examples = {
        WebReturnIntentKind.WORKBENCH: WebReturnIntent(
            kind=WebReturnIntentKind.WORKBENCH
        ),
        WebReturnIntentKind.SAFE_TASK_DETAIL: WebReturnIntent(
            kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
            task_id="task-1",
        ),
        WebReturnIntentKind.ADMIN_CENTER: WebReturnIntent(
            kind=WebReturnIntentKind.ADMIN_CENTER
        ),
        WebReturnIntentKind.ACTIVATION_STATUS: WebReturnIntent(
            kind=WebReturnIntentKind.ACTIVATION_STATUS,
            request_id="activation-1",
        ),
    }
    assert set(examples) == set(WebReturnIntentKind)
