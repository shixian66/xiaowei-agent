"""只通过 HTTP API 工作的标准库 CLI。"""

import argparse
import contextlib
import json
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from typing import IO, Never, Protocol, TextIO, TypeAlias, cast

from pydantic import ValidationError

from xiaowei_agent.contracts import TaskView, task_query_path
from xiaowei_agent.redaction import scrub_text

CLI_HTTP_TIMEOUT_SECONDS = 10.0
CLI_RESPONSE_LIMIT = 1_048_576
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class CliUsageError(RuntimeError):
    """不携带 argparse 原始错误文本的用法错误。"""


class RedirectRejectedError(RuntimeError):
    """不携带重定向响应内容的协议错误。"""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise CliUsageError("invalid command line")


class ResponseLike(Protocol):
    status: int
    headers: Message

    def read(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


OpenCall: TypeAlias = Callable[..., ResponseLike]


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """所有 30x 都作为协议错误返回给 CLI，不重放方法或 body。"""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise RedirectRejectedError("redirect rejected") from None


def build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(RejectRedirectHandler())


def _parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(prog="xiaowei", add_help=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    task = parser.add_subparsers(dest="group", required=True).add_parser("task")
    commands = task.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--text", required=True)
    submit.add_argument("--idempotency-key", required=True)
    get = commands.add_parser("get")
    get.add_argument("task_id")
    return parser


def _origin(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise CliUsageError("unsupported base URL")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise CliUsageError("invalid base URL authority")
    if parsed.query or parsed.fragment:
        raise CliUsageError("base URL cannot include query or fragment")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


def _request(args: argparse.Namespace) -> tuple[urllib.request.Request, str | None]:
    base = _origin(args.base_url)
    if args.command == "submit":
        payload = json.dumps(
            {"text": args.text, "idempotency_key": args.idempotency_key},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return (
            urllib.request.Request(  # noqa: S310 -- _origin 仅允许 http/https
                f"{base}/v1/tasks",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            None,
        )
    task_id: str = args.task_id
    path = task_query_path(task_id)
    return (
        urllib.request.Request(  # noqa: S310 -- _origin 仅允许 http/https
            f"{base}{path}", method="GET"
        ),
        task_id,
    )


def _exit_for_status(status: int) -> int:
    if 200 <= status < 300:
        return 0
    if 300 <= status < 400:
        return 2
    if status == 404:
        return 3
    if status == 409:
        return 4
    if status == 413:
        return 5
    if status == 503:
        return 6
    if status == 504:
        return 7
    if 500 <= status < 600:
        return 8
    return 2


_ERROR_LABELS: Mapping[int, str] = {
    2: "protocol_error",
    3: "not_found",
    4: "idempotency_conflict",
    5: "payload_too_large",
    6: "unavailable",
    7: "timeout",
    8: "server_error",
}


def _fail(code: int, stderr: TextIO) -> int:
    stderr.write(f"xiaowei: {_ERROR_LABELS[code]}\n")
    return code


def _content_type(response: ResponseLike) -> str:
    values = response.headers.get_all("content-type", failobj=[])
    if len(values) != 1:
        return ""
    return str(values[0]).split(";", 1)[0].strip().lower()


def _terminal_string(value: str) -> str:
    scrubbed = scrub_text(value)
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char).startswith("C") else char
        for char in scrubbed
    )


def _terminal_value(value: object) -> object:
    if isinstance(value, str):
        return _terminal_string(value)
    if isinstance(value, list):
        return [_terminal_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _terminal_value(item) for key, item in value.items()}
    return value


def _default_open(request: urllib.request.Request, *, timeout: float) -> ResponseLike:
    return cast(ResponseLike, build_opener().open(request, timeout=timeout))


def run_cli(
    argv: Sequence[str],
    *,
    opener: OpenCall,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """执行一次命令；所有错误都收敛为固定退出码与固定 stderr 标签。"""
    try:
        args = _parser().parse_args(list(argv))
        request, expected_task_id = _request(args)
    except (CliUsageError, SystemExit):
        return _fail(2, stderr)

    response: ResponseLike | None = None
    try:
        response = opener(request, timeout=CLI_HTTP_TIMEOUT_SECONDS)
        status = response.status
        code = _exit_for_status(status)
        if code:
            return _fail(code, stderr)
        if _content_type(response) != "application/json":
            return _fail(2, stderr)
        raw = response.read(CLI_RESPONSE_LIMIT + 1)
        if not isinstance(raw, bytes) or len(raw) > CLI_RESPONSE_LIMIT:
            return _fail(2, stderr)
        view = TaskView.model_validate_json(raw)
        if expected_task_id is not None and view.task_id != expected_task_id:
            return _fail(2, stderr)
    except RedirectRejectedError:
        return _fail(2, stderr)
    except urllib.error.HTTPError as exc:
        code = _exit_for_status(exc.code)
        with contextlib.suppress(Exception):
            exc.close()
        return _fail(code, stderr)
    except urllib.error.URLError as exc:
        code = 7 if isinstance(exc.reason, TimeoutError) else 6
        return _fail(code, stderr)
    except TimeoutError:
        return _fail(7, stderr)
    except OSError:
        return _fail(6, stderr)
    except (ValidationError, ValueError, TypeError):
        return _fail(2, stderr)
    finally:
        if response is not None:
            with contextlib.suppress(Exception):
                response.close()

    safe = _terminal_value(view.model_dump(mode="json"))
    stdout.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_cli(
        sys.argv[1:] if argv is None else argv,
        opener=_default_open,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
