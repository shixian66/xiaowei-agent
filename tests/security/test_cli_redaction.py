"""CLI 不跟随重定向、不回显错误，并转义终端控制字符。"""

import io
import json
import urllib.request
from email.message import Message
from typing import Any

import pytest

from xiaowei_agent.contracts import (
    RenderPayload,
    RenderSection,
    TaskStatus,
    TaskView,
    task_query_path,
)
from xiaowei_agent.interfaces.cli import (
    RedirectRejectedError,
    RejectRedirectHandler,
    build_opener,
    run_cli,
)

pytestmark = pytest.mark.security


class _Response:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers = Message()
        self.headers["content-type"] = "application/json"

    def read(self, size: int = -1) -> bytes:
        return self.body[:size]

    def close(self) -> None:
        return None


def _run(response: _Response) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()

    def opener(_: Any, *, timeout: float) -> _Response:
        return response

    code = run_cli(
        ["task", "get", "task-1"],
        opener=opener,
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_server_strings_are_redacted_and_control_characters_are_escaped() -> None:
    fake_secret = "token=" + "fake-private-value"
    body = f"line one\n\x1b[31m{fake_secret}\x1b[0m"
    render = RenderPayload(
        answer=body,
        sections=(RenderSection(title="evidence", body=body, refs=("e1",)),),
        next_steps=(),
        status=TaskStatus.SUCCEEDED,
        refs=("e1",),
    )
    view = TaskView(
        task_id="task-1",
        status=TaskStatus.SUCCEEDED,
        render=render,
        query_path=task_query_path("task-1"),
    )
    code, stdout, stderr = _run(_Response(view.model_dump_json().encode()))
    assert code == 0
    assert fake_secret not in stdout
    assert "\x1b" not in stdout
    assert "\n" not in stdout[:-1]
    assert "\\u001b" in stdout
    assert stderr == ""


def test_protocol_error_never_copies_the_response_body_to_stderr() -> None:
    fake_secret = "password=" + "fake-private-value"
    response = _Response(json.dumps({"error": fake_secret}).encode())
    response.status = 500
    code, _, stderr = _run(response)
    assert code == 8
    assert fake_secret not in stderr


def test_default_opener_uses_a_redirect_rejecting_handler() -> None:
    opener = build_opener()
    handler_names = {type(handler).__name__ for handler in opener.handlers}
    assert "RejectRedirectHandler" in handler_names
    assert "HTTPRedirectHandler" not in handler_names


def test_redirect_handler_refuses_to_construct_a_followup_request() -> None:
    fake_secret = "token=" + "redirect-private-value"
    with pytest.raises(RedirectRejectedError) as caught:
        RejectRedirectHandler().redirect_request(
            urllib.request.Request("http://localhost/v1/tasks", method="POST"),
            io.BytesIO(),
            307,
            fake_secret,
            Message(),
            f"http://other.invalid/collect?{fake_secret}",
        )
    assert str(caught.value) == "redirect rejected"
    assert fake_secret not in repr(caught.value)
