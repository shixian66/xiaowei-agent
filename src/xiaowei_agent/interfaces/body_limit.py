"""在 JSON/Pydantic 解析前执行的 ASGI body 与 media-type 边界。"""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from xiaowei_agent.interfaces.http_models import error_body


async def _send_error(send: Send, *, status: int, code: str) -> None:
    payload = json.dumps(error_body(code), separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class JsonBodyLimitMiddleware:
    """只缓冲 POST /v1/tasks，最大内存占用被 limit 硬限制。"""

    def __init__(self, app: ASGIApp, *, limit: int) -> None:
        self._app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and scope["path"] == "/v1/tasks"
        ):
            await self._app(scope, receive, send)
            return

        raw_headers = scope.get("headers", [])
        content_types = [value for key, value in raw_headers if key.lower() == b"content-type"]
        if len(content_types) != 1:
            await _send_error(send, status=415, code="unsupported_media_type")
            return
        content_type = content_types[0].decode("latin-1")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            await _send_error(send, status=415, code="unsupported_media_type")
            return

        content_lengths = [
            value for key, value in raw_headers if key.lower() == b"content-length"
        ]
        if len(content_lengths) > 1:
            await _send_error(send, status=400, code="invalid_request")
            return
        if content_lengths:
            try:
                declared = int(content_lengths[0])
            except ValueError:
                await _send_error(send, status=400, code="invalid_request")
                return
            if declared < 0:
                await _send_error(send, status=400, code="invalid_request")
                return
            if declared > self._limit:
                await _send_error(send, status=413, code="payload_too_large")
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await _send_error(send, status=400, code="invalid_request")
                return
            body.extend(message.get("body", b""))
            if len(body) > self._limit:
                await _send_error(send, status=413, code="payload_too_large")
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self._app(scope, replay, send)
