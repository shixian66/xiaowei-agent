"""W2 Web return intent is a closed, data-only navigation contract."""

import pytest
from pydantic import ValidationError

from xiaowei_agent.contracts import (
    IdentitySource,
    ProductRole,
    WebReturnIntent,
    WebReturnIntentKind,
)
from xiaowei_agent.interfaces.web_navigation import (
    WebNavigationInputError,
    parse_web_return_intent,
    web_login_path,
    web_return_intent_allowed,
    web_return_path,
    web_task_detail_path,
)


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


@pytest.mark.parametrize(
    "reference",
    (
        "https://outside.example.test/app",
        "/login/activation-1",
        "activation-1?next=/admin",
        "activation-1#fragment",
        "activation-1\nheader:value",
    ),
)
def test_activation_request_reference_rejects_redirect_shapes(reference: str) -> None:
    with pytest.raises(ValidationError):
        WebReturnIntent(
            kind=WebReturnIntentKind.ACTIVATION_STATUS,
            request_id=reference,
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


@pytest.mark.parametrize(
    ("intent", "path"),
    (
        (WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH), "/app"),
        (
            WebReturnIntent(
                kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
                task_id="task:with-safe.chars_1",
            ),
            "/app/tasks/task%3Awith-safe.chars_1",
        ),
        (WebReturnIntent(kind=WebReturnIntentKind.ADMIN_CENTER), "/admin"),
        (
            WebReturnIntent(
                kind=WebReturnIntentKind.ACTIVATION_STATUS,
                request_id="activation-1",
            ),
            "/login?intent=activation_status&request_id=activation-1",
        ),
    ),
)
def test_return_paths_are_rebuilt_only_from_the_closed_intent(
    intent: WebReturnIntent, path: str
) -> None:
    assert web_return_path(intent) == path
    if intent.kind is WebReturnIntentKind.SAFE_TASK_DETAIL:
        assert web_return_path(intent) == web_task_detail_path(intent.task_id)


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ((), WebReturnIntent(kind=WebReturnIntentKind.WORKBENCH)),
        (
            (("intent", "safe_task_detail"), ("task_id", "task-1")),
            WebReturnIntent(
                kind=WebReturnIntentKind.SAFE_TASK_DETAIL,
                task_id="task-1",
            ),
        ),
        (
            (("intent", "admin_center"),),
            WebReturnIntent(kind=WebReturnIntentKind.ADMIN_CENTER),
        ),
        (
            (("intent", "activation_status"), ("request_id", "activation-1")),
            WebReturnIntent(
                kind=WebReturnIntentKind.ACTIVATION_STATUS,
                request_id="activation-1",
            ),
        ),
    ),
)
def test_query_parser_accepts_only_the_closed_intent_shape(
    query: tuple[tuple[str, str], ...], expected: WebReturnIntent
) -> None:
    assert parse_web_return_intent(query) == expected
    assert web_login_path(expected).startswith("/login?intent=")


@pytest.mark.parametrize(
    "query",
    (
        (("next", "https://outside.example.test"),),
        (("intent", "workbench"), ("intent", "admin_center")),
        (("intent", "safe_task_detail"),),
        (("intent", "workbench"), ("task_id", "task-1")),
        (("intent", "unknown"),),
    ),
)
def test_query_parser_rejects_unknown_duplicate_and_mismatched_fields(
    query: tuple[tuple[str, str], ...],
) -> None:
    with pytest.raises(WebNavigationInputError):
        parse_web_return_intent(query)


@pytest.mark.parametrize(
    ("source", "role", "allowed"),
    (
        (
            IdentitySource.LOCAL_ADMIN,
            ProductRole.ADMIN,
            {"workbench", "safe_task_detail", "admin_center"},
        ),
        (
            IdentitySource.FEISHU,
            ProductRole.ADMIN,
            {"workbench", "safe_task_detail", "admin_center"},
        ),
        (
            IdentitySource.FEISHU,
            ProductRole.OPERATOR,
            {"workbench", "safe_task_detail"},
        ),
        (
            IdentitySource.FEISHU,
            ProductRole.USER,
            {"safe_task_detail"},
        ),
    ),
)
def test_destination_matrix_is_total_and_activation_status_is_never_a_destination(
    source: IdentitySource, role: ProductRole, allowed: set[str]
) -> None:
    intents = {
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
    assert {
        kind.value
        for kind, intent in intents.items()
        if web_return_intent_allowed(source=source, role=role, intent=intent)
    } == allowed
