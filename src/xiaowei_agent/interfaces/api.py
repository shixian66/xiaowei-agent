"""薄 FastAPI gateway：协议、受信上下文与 TaskView 序列化。"""

import asyncio
import sys
from typing import Protocol

import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from xiaowei_agent.application.task_view_runtime import (
    ApplicationFailure,
    classify_application_exception,
)
from xiaowei_agent.config import Settings
from xiaowei_agent.contracts import (
    ReadinessProbe,
    TaskId,
    TaskLookup,
    TaskSubmission,
    TaskView,
)
from xiaowei_agent.interfaces.auth import Clock, trusted_submission, trusted_trace_id
from xiaowei_agent.interfaces.body_limit import JsonBodyLimitMiddleware
from xiaowei_agent.interfaces.http_models import SubmitTaskRequest, error_body
from xiaowei_agent.interfaces.local_stack import build_postgres_task_view_stack
from xiaowei_agent.log import configure_logging
from xiaowei_agent.trace import bind_trace_id


class RuntimePort(Protocol):
    async def submit_task(self, *, submission: TaskSubmission) -> TaskView: ...

    async def query_task(self, *, lookup: TaskLookup) -> TaskView: ...


_TASK_ID_ADAPTER = TypeAdapter(TaskId)


def _task_id_or_not_found(task_id: str) -> str:
    try:
        return _TASK_ID_ADAPTER.validate_python(task_id, strict=True)
    except ValidationError:
        raise StarletteHTTPException(status_code=404) from None


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=error_body(code))


async def _validation_error(_: Request, __: Exception) -> Response:
    return _error(400, "invalid_request")


async def _http_error(_: Request, exc: Exception) -> Response:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    if status == 404:
        return _error(404, "not_found")
    if status == 405:
        return _error(405, "method_not_allowed")
    if status == 415:
        return _error(415, "unsupported_media_type")
    return _error(400, "invalid_request")


async def _application_error(_: Request, exc: Exception) -> Response:
    failure = classify_application_exception(exc)
    if failure is ApplicationFailure.CLARIFICATION_INTEGRITY:
        return _error(500, "clarification.integrity_error")
    if failure is ApplicationFailure.CONFLICT:
        return _error(409, "idempotency_conflict")
    if failure is ApplicationFailure.NOT_FOUND:
        return _error(404, "not_found")
    if failure is ApplicationFailure.UNAVAILABLE:
        return _error(503, "unavailable")
    return _error(500, "internal_error")


def create_app(
    *,
    runtime: RuntimePort,
    settings: Settings,
    readiness: ReadinessProbe,
    clock: Clock,
    policy_revision: str,
) -> FastAPI:
    app = FastAPI(
        redirect_slashes=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        JsonBodyLimitMiddleware, limit=settings.api_request_body_limit_bytes
    )
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _application_error)

    @app.post("/v1/tasks", status_code=202)
    async def submit(body: SubmitTaskRequest) -> dict[str, object]:
        trace_id = trusted_trace_id()
        submission = trusted_submission(
            request=body,
            settings=settings,
            clock=clock,
            policy_revision=policy_revision,
            trace_id=trace_id,
        )
        with bind_trace_id(trace_id):
            view = await runtime.submit_task(submission=submission)
        return view.model_dump(mode="json")

    @app.get("/v1/tasks/{task_id}")
    async def query(task_id: str) -> dict[str, object]:
        trace_id = trusted_trace_id()
        with bind_trace_id(trace_id):
            view = await runtime.query_task(
                lookup=TaskLookup(
                    task_id=_task_id_or_not_found(task_id),
                    tenant_id=settings.tenant_id,
                    environment_id=settings.environment_id,
                )
            )
        return view.model_dump(mode="json")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=None)
    async def ready() -> Response:
        report = await readiness.check()
        if not (
            report.database_ok and report.revision_matches_head and report.assembled
        ):
            return _error(503, "unavailable")
        return JSONResponse(content=report.model_dump(mode="json"))

    return app


async def serve_api(settings: Settings) -> int:
    """装配并运行 API 进程；退出时释放唯一 Engine。"""
    configure_logging(settings)
    stack = await build_postgres_task_view_stack(settings=settings)
    try:
        app = create_app(
            runtime=stack.runtime,
            settings=settings,
            readiness=stack.readiness,
            clock=stack.clock,
            policy_revision=stack.policy_revision,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.api_bind_host,
                port=settings.api_bind_port,
                access_log=False,
                log_config=None,
            )
        )
        await server.serve()
        return 0
    finally:
        await stack.aclose()


def main() -> int:
    from xiaowei_agent.config import ConfigError, load_settings

    try:
        return asyncio.run(serve_api(load_settings()))
    except ConfigError:
        sys.stderr.write("xiaowei-api: configuration_error\n")
        return 2
    except Exception:
        sys.stderr.write("xiaowei-api: startup_failed\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
